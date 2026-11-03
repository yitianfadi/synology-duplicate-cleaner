# -*- coding: utf-8 -*-
"""云端字幕：OpenAI 兼容 ASR + LLM 翻译，异步任务（docs/SUBTITLE_CONTRACT.md）。

安全与隐私要点：
- 默认关闭；管理员显式开启并勾选“音频上传第三方”才可提交。
- API Key 只保存在 settings.json（DSM 权限保护），绝不进入日志/审计/错误/任务 JSON。
- 不记录转写原文或译文；字幕文件只落在任务目录供管理员下载。
- 音频只写临时目录，完成/失败/取消后删除。
- 外部 HTTP 请求使用标准库 urllib，无第三方依赖；默认不做任何网络请求（需显式开启）。
"""

from __future__ import absolute_import

import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque

from . import config, settings as settings_module, timeutil, video_similarity
from .errors import ApiError, invalid, not_found

JOB_ID_RE = re.compile(r"^sub-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
SUBTITLE_FILE_RE = re.compile(
    r"^(?:original\.(?:srt|vtt)|bilingual\.[A-Za-z0-9_-]{2,16}\.srt"
    r"|[A-Za-z0-9_-]{2,16}\.(?:srt|vtt))$")
LANGUAGE_RE = re.compile(r"^(?:auto|[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?)$")
MAX_JOBS = 20

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_TRANSLATING = "translating"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_PAUSED = "paused"
FINISHED_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_TRANSLATING)

# ASR 分片缓存状态（subtitle_chunks.status）
CHUNK_PENDING = "pending"
CHUNK_DONE = "done"
CHUNK_FAILED = "failed"

_FENCE = chr(96) * 3

# v0026 云端字幕探测（模型列表 / 连接测试）：固定超时、不重试。
MODELS_TIMEOUT_SEC = 20.0
CONNECTION_TEST_TIMEOUT_SEC = 60.0
MODELS_MAX = 500
MODEL_ID_MAX_LEN = 200
_ERROR_TEXT_MAX = 300
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


def mask_secret(text, api_key=""):
    """错误消息脱敏：替换 api_key 为 ***、去控制字符、截断 300。

    响应/日志/审计/任务 JSON 绝不出现 api_key；错误 body 里的回显也一并处理。
    """
    if text is None:
        return ""
    value = str(text)
    if api_key:
        value = value.replace(str(api_key), "***")
    value = _CONTROL_CHARS_RE.sub(" ", value)
    return value.strip()[:_ERROR_TEXT_MAX]


def provider_error_message(prefix, status, body, api_key=""):
    """构造脱敏后的 Provider 错误消息（可含响应片段，但先去密钥/控制字符）。"""
    text = u"%s（HTTP %s）" % (prefix, status)
    if body:
        try:
            snippet = body.decode("utf-8", "replace") \
                if isinstance(body, (bytes, bytearray)) else str(body)
        except Exception:
            snippet = ""
        snippet = mask_secret(snippet, api_key)
        if snippet:
            text += u"：" + snippet[:200]
    return mask_secret(text, api_key)


class SubtitleError(Exception):
    """任务级错误：code + 可安全展示的中文消息（绝不含密钥/转写内容）。"""

    def __init__(self, code, message):
        super(SubtitleError, self).__init__(message)
        self.code = code or "SUBTITLE_ERROR"
        self.message = message or u"字幕生成失败"


class SubtitleCancelled(Exception):
    pass


class SubtitlePaused(Exception):
    """在 ASR 分片边界/翻译批次边界请求暂停（可继续）。"""


# ---------------------------------------------------------------------------
# 文本 / 字幕工具
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(u"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff]")
_SRT_BLOCK_RE = re.compile(
    r"(?:^|\n)\s*\d+\s*\n\s*"
    r"([0-9]{1,2}):([0-9]{2}):([0-9]{2})[,.](\d{1,3})\s*-->\s*"
    r"([0-9]{1,2}):([0-9]{2}):([0-9]{2})[,.](\d{1,3})[^\n]*\n"
    r"(.*?)(?=\n\s*\n|\n\s*\d+\s*\n|\Z)",
    re.S)


def sanitize_cue_text(text):
    if text is None:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "".join(ch for ch in text if ch in ("\n", "\t") or ord(ch) >= 32)
    cleaned = cleaned.replace("-->", u"\u2192")
    return "\n".join(line.strip() for line in cleaned.split("\n") if line.strip())


def _is_cjk(text):
    return bool(_CJK_RE.search(text or ""))


def wrap_text(text, cjk_width=42, latin_width=80):
    text = sanitize_cue_text(text)
    if not text:
        return ""
    lines = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if _is_cjk(paragraph):
            for start in range(0, len(paragraph), cjk_width):
                lines.append(paragraph[start:start + cjk_width])
        else:
            current = ""
            for word in paragraph.split():
                if not current:
                    current = word
                elif len(current) + 1 + len(word) <= latin_width:
                    current += " " + word
                else:
                    lines.append(current)
                    current = word
            if current:
                lines.append(current)
    return "\n".join(lines)


def _escape_vtt(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def format_timestamp(seconds, use_comma=True):
    try:
        total_ms = int(round(float(seconds) * 1000.0))
    except (TypeError, ValueError):
        total_ms = 0
    if total_ms < 0:
        total_ms = 0
    hours = total_ms // 3600000
    minutes = (total_ms % 3600000) // 60000
    secs = (total_ms % 60000) // 1000
    millis = total_ms % 1000
    return "%02d:%02d:%02d%s%03d" % (
        hours, minutes, secs, "," if use_comma else ".", millis)


def parse_srt(text):
    cues = []
    if not isinstance(text, str):
        return cues
    for match in _SRT_BLOCK_RE.finditer(text):
        try:
            start = (int(match.group(1)) * 3600 + int(match.group(2)) * 60
                     + int(match.group(3))
                     + int(match.group(4).ljust(3, "0")) / 1000.0)
            end = (int(match.group(5)) * 3600 + int(match.group(6)) * 60
                   + int(match.group(7))
                   + int(match.group(8).ljust(3, "0")) / 1000.0)
        except (TypeError, ValueError):
            continue
        cue_text = sanitize_cue_text(match.group(9))
        if cue_text and end > start:
            cues.append({"start": start, "end": end, "text": cue_text})
    return cues


def parse_asr_body(text):
    """解析 OpenAI 兼容 ASR 响应：优先 verbose_json segments，其次 SRT。"""
    stripped = (text or "").strip()
    payload = None
    try:
        payload = json.loads(stripped)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        segments = payload.get("segments")
        if isinstance(segments, list):
            cues = []
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                try:
                    start = float(segment.get("start"))
                    end = float(segment.get("end"))
                except (TypeError, ValueError):
                    continue
                cue_text = sanitize_cue_text(segment.get("text"))
                if cue_text and end > start:
                    cues.append({"start": max(0.0, start),
                                 "end": max(0.0, end), "text": cue_text})
            if cues:
                return cues
        extra_srt = payload.get("srt") or payload.get("subtitle")
        if isinstance(extra_srt, str) and parse_srt(extra_srt):
            return parse_srt(extra_srt)
        if isinstance(payload.get("text"), str):
            raise SubtitleError(
                "NO_TIMESTAMPS",
                u"ASR 未返回时间轴；请使用支持 verbose_json/segments 的模型（如 whisper-1）")
    cues = parse_srt(stripped)
    if cues:
        return cues
    raise SubtitleError(
        "NO_TIMESTAMPS",
        u"ASR 未返回时间轴；请使用支持 verbose_json/segments 的模型（如 whisper-1）")


def dedupe_cues(cues):
    """去掉重复 cue（相同 start/end/text，或块边界首尾重叠的相同文本）。"""
    cleaned = []
    seen = set()
    for cue in sorted(cues, key=lambda item: (item["start"], item["end"])):
        if cue["end"] <= cue["start"]:
            continue
        key = (round(cue["start"] * 1000), round(cue["end"] * 1000),
               cue["text"].strip())
        if key in seen:
            continue
        if cleaned:
            previous = cleaned[-1]
            same_text = cue["text"].strip() == previous["text"].strip()
            if same_text and (
                    abs(cue["start"] - previous["start"]) < 0.5
                    or (cue["start"] < previous["end"]
                        and cue["end"] <= previous["end"] + 0.5)):
                continue
        seen.add(key)
        cleaned.append(cue)
    return cleaned


def build_srt(cues, translated=None, bilingual=False):
    blocks = []
    for index, cue in enumerate(cues, 1):
        original = wrap_text(cue["text"])
        if translated is not None:
            target = wrap_text(translated[index - 1])
            body = (original + "\n" + target) if bilingual else target
        else:
            body = original
        if not body:
            continue
        blocks.append("%d\n%s --> %s\n%s\n" % (
            len(blocks) + 1,
            format_timestamp(cue["start"], True),
            format_timestamp(cue["end"], True),
            body))
    return "\n".join(blocks)


def build_vtt(cues, translated=None):
    lines = ["WEBVTT", ""]
    for index, cue in enumerate(cues, 1):
        if translated is not None:
            body = wrap_text(translated[index - 1])
        else:
            body = wrap_text(cue["text"])
        if not body:
            continue
        lines.append("%s --> %s" % (
            format_timestamp(cue["start"], False),
            format_timestamp(cue["end"], False)))
        lines.append(_escape_vtt(body))
        lines.append("")
    return "\n".join(lines) + "\n"


def plan_chunks(duration, chunk_sec):
    chunks = []
    cursor = 0.0
    chunk_sec = max(1.0, float(chunk_sec))
    while cursor < duration - 0.001:
        end = min(float(duration), cursor + chunk_sec)
        if end - cursor < 0.001:
            break
        chunks.append((cursor, end - cursor))
        cursor = end
    return chunks


# ---------------------------------------------------------------------------
# OpenAI 兼容 HTTP 客户端
# ---------------------------------------------------------------------------

def encode_multipart(fields, file_field, file_path, filename):
    boundary = "----dcsub" + uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, key, value)).encode("utf-8"))
    with open(file_path, "rb") as handle:
        audio = handle.read()
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                  "Content-Type: audio/mpeg\r\n\r\n"
                  % (boundary, file_field, filename)).encode("utf-8"))
    parts.append(audio)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(parts), "multipart/form-data; boundary=%s" % boundary


class OpenAIClient(object):
    """最小 OpenAI 兼容客户端（urllib，无第三方依赖；可注入超时上限用于测试）。"""

    def __init__(self, retry_delay=1.0, timeout_cap=None):
        self.retry_delay = float(retry_delay)
        self.timeout_cap = timeout_cap

    def _effective_timeout(self, timeout):
        value = float(timeout or 60)
        if self.timeout_cap:
            value = min(value, float(self.timeout_cap))
        return max(0.5, value)

    def _sleep(self, seconds, cancel_event):
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise SubtitleCancelled()
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _request_raw(self, method, url, api_key, data, content_type, timeout,
                     cancel_event=None):
        """单次 HTTP 请求（线程 + 硬超时），返回 (kind, status, body)。

        kind: ok / http / timeout / network。不重试；供原有重试逻辑与 v0026
        探测接口共用（探测接口不调用 _post_with_retry）。
        """
        request = urllib.request.Request(url, data=data, method=method)
        if content_type:
            request.add_header("Content-Type", content_type)
        request.add_header("Accept", "application/json")
        if api_key:
            request.add_header("Authorization", "Bearer " + api_key)
        outcome = {}

        def worker():
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    outcome["result"] = (response.status, response.read())
            except urllib.error.HTTPError as exc:
                try:
                    body = exc.read()
                except Exception:
                    body = b""
                outcome["error"] = ("http", int(exc.code), body)
            except (socket.timeout, TimeoutError) as exc:
                outcome["error"] = ("timeout", type(exc).__name__, b"")
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, (socket.timeout, TimeoutError)):
                    outcome["error"] = ("timeout", "timeout", b"")
                else:
                    outcome["error"] = ("network", type(reason).__name__, b"")
            except OSError as exc:
                outcome["error"] = ("network", type(exc).__name__, b"")

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        deadline = time.monotonic() + timeout + 0.5
        while thread.is_alive():
            if cancel_event is not None and cancel_event.is_set():
                raise SubtitleCancelled()
            if time.monotonic() > deadline:
                raise SubtitleError("TIMEOUT", u"云端请求超时")
            time.sleep(0.05)
        error = outcome.get("error")
        if error is not None:
            if error[0] == "http":
                return ("http", error[1], error[2])
            if error[0] == "timeout":
                return ("timeout", error[1], b"")
            return ("network", error[1], b"")
        status, body = outcome.get("result") or (0, b"")
        return ("ok", status, body or b"")

    def _post(self, url, api_key, data, content_type, timeout, cancel_event):
        return self._request_raw("POST", url, api_key, data, content_type,
                                 timeout, cancel_event)

    def _post_with_retry(self, url, api_key, data, content_type, timeout,
                         cancel_event):
        attempts = 0
        while True:
            kind, status, body = self._post(
                url, api_key, data, content_type, timeout, cancel_event)
            if kind == "ok":
                return body
            if kind == "timeout":
                raise SubtitleError("TIMEOUT", u"云端请求超时")
            if kind == "http":
                if status in (401, 403):
                    raise SubtitleError(
                        "AUTH_FAILED", u"云端服务认证失败，请检查 API Key 配置")
                if status in (429, 500, 502, 503, 504) and attempts < 2:
                    attempts += 1
                    self._sleep(self.retry_delay * (2 ** (attempts - 1)),
                                cancel_event)
                    continue
                raise SubtitleError("HTTP_ERROR",
                                    u"云端服务请求失败（HTTP %d）" % status)
            if attempts < 2:
                attempts += 1
                self._sleep(self.retry_delay * (2 ** (attempts - 1)),
                            cancel_event)
                continue
            raise SubtitleError("NETWORK_ERROR", u"无法连接云端服务")

    def transcribe(self, base_url, api_key, model, language, audio_path,
                   timeout, cancel_event):
        url = (base_url or "").rstrip("/") + "/audio/transcriptions"
        fields = {
            "model": model,
            "response_format": "verbose_json",
            "temperature": "0",
        }
        if language and language != "auto":
            fields["language"] = language
        data, content_type = encode_multipart(fields, "file", audio_path,
                                              "audio.mp3")
        body = self._post_with_retry(
            url, api_key, data, content_type,
            self._effective_timeout(timeout), cancel_event)
        return parse_asr_body(body.decode("utf-8", "replace"))

    def translate(self, base_url, api_key, model, texts, target_lang,
                  timeout, cancel_event):
        url = (base_url or "").rstrip("/") + "/chat/completions"
        system_prompt = (
            u"You are a professional subtitle translator. Translate every cue "
            u"into the target language (%s). Do not merge, split, reorder or "
            u"change timing. Return ONLY a JSON array of strings with exactly "
            u"the same number of items, preserving order. No explanations."
            % target_lang)
        user_content = json.dumps(
            {"target_language": target_lang, "cues": texts},
            ensure_ascii=False)
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        body = self._post_with_retry(
            url, api_key, data, "application/json",
            self._effective_timeout(timeout), cancel_event)
        return self._parse_chat_content(body.decode("utf-8", "replace"))

    @staticmethod
    def _parse_chat_content(text):
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            return None
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        if not isinstance(content, str):
            return None
        stripped = content.strip()
        if stripped.startswith(_FENCE):
            stripped = re.sub(r"^" + _FENCE + r"[A-Za-z0-9_-]*\s*", "",
                              stripped)
            stripped = re.sub(r"\s*" + _FENCE + r"$", "", stripped)
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            return None
        if not isinstance(parsed, list):
            return None
        if not all(isinstance(item, str) for item in parsed):
            return None
        return parsed

    # ------------------------------------------------------------------
    # v0026 探测用低层方法（单次请求、不重试；错误码由 Service 映射）
    # ------------------------------------------------------------------
    def _get_raw(self, url, api_key, timeout=MODELS_TIMEOUT_SEC):
        """GET 原始响应：返回 (kind, status, body)，不重试。"""
        return self._request_raw("GET", url, api_key, None, None,
                                 self._effective_timeout(timeout))

    def _probe_request(self, method, url, api_key, data, content_type,
                       timeout):
        kind, status, body = self._request_raw(
            method, url, api_key, data, content_type,
            self._effective_timeout(timeout))
        if kind == "timeout":
            raise SubtitleError("PROVIDER_TIMEOUT", u"云端请求超时")
        if kind == "network":
            raise SubtitleError("PROVIDER_NETWORK_ERROR", u"无法连接云端服务")
        if kind == "http":
            if status in (401, 403):
                raise SubtitleError(
                    "PROVIDER_AUTH_FAILED",
                    u"云端服务认证失败，请检查 API Key 配置")
            if status in (400, 404, 405, 409, 422):
                raise SubtitleError(
                    "PROVIDER_REJECTED",
                    provider_error_message(u"云端服务拒绝了请求", status,
                                           body, api_key))
            raise SubtitleError(
                "PROVIDER_HTTP_ERROR",
                provider_error_message(u"云端服务请求失败", status, body,
                                       api_key))
        return status, body

    def probe_transcribe(self, base_url, api_key, model, language,
                         audio_path, timeout=CONNECTION_TEST_TIMEOUT_SEC):
        """单次 ASR 探测（静音音频，不重试）。成功返回 (status, body)。"""
        url = (base_url or "").rstrip("/") + "/audio/transcriptions"
        fields = {
            "model": model,
            "response_format": "verbose_json",
            "temperature": "0",
        }
        if language and language != "auto":
            fields["language"] = language
        data, content_type = encode_multipart(fields, "file", audio_path,
                                              "silence.mp3")
        return self._probe_request("POST", url, api_key, data, content_type,
                                   timeout)

    def probe_chat(self, base_url, api_key, model, texts, target_lang,
                   timeout=CONNECTION_TEST_TIMEOUT_SEC):
        """单次 LLM 探测（固定测试文本，不重试）。成功返回 (status, body)。"""
        url = (base_url or "").rstrip("/") + "/chat/completions"
        system_prompt = (
            u"You are a subtitle translation connectivity test. Translate "
            u"every item into the target language (%s). Return ONLY a JSON "
            u"array of strings with exactly the same number of items, "
            u"preserving order. No explanations." % target_lang)
        user_content = json.dumps(
            {"target_language": target_lang, "cues": list(texts or [])},
            ensure_ascii=False)
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._probe_request("POST", url, api_key, data,
                                   "application/json", timeout)


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------

class SubtitleJob(object):
    def __init__(self, job_id, path, language, translate, target_lang, user):
        self.job_id = job_id
        self.path = path
        self.language = language
        self.translate = bool(translate)
        self.target_lang = target_lang
        self.user = user
        self.status = STATUS_QUEUED
        self.stage = "audio"
        self.progress = 0
        self.message = u"排队等待"
        self.error = None
        self.files = []
        self.translation_partial = False
        self.created_epoch = time.time()
        self.updated_epoch = self.created_epoch
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        # plan C：ASR 分片进度 / 是否可继续
        self.chunk_index = 0
        self.chunk_total = 0
        self.resumable = False
        self.lock = threading.RLock()

    def snapshot(self):
        with self.lock:
            return {
                "job_id": self.job_id,
                "status": self.status,
                "stage": self.stage,
                "progress": int(self.progress),
                "message": self.message,
                "path": self.path,
                "language": self.language,
                "translate": self.translate,
                "target_lang": self.target_lang,
                "created_at": timeutil.iso_local(self.created_epoch),
                "updated_at": timeutil.iso_local(self.updated_epoch),
                "error": dict(self.error) if isinstance(self.error, dict) else None,
                "files": list(self.files),
                "translation_partial": bool(self.translation_partial),
                "paused": self.status == STATUS_PAUSED,
                "resumable": bool(self.resumable),
                "chunk_index": int(self.chunk_index),
                "chunk_total": int(self.chunk_total),
            }

    def to_record(self):
        return {
            "job_id": self.job_id,
            "status": self.status,
            "stage": self.stage,
            "progress": int(self.progress),
            "message": self.message,
            "path": self.path,
            "language": self.language,
            "translate": self.translate,
            "target_lang": self.target_lang,
            "user": self.user,
            "created_epoch": self.created_epoch,
            "updated_epoch": self.updated_epoch,
            "error": self.error,
            "files": list(self.files),
            "translation_partial": bool(self.translation_partial),
            "chunk_index": int(self.chunk_index),
            "chunk_total": int(self.chunk_total),
            "resumable": bool(self.resumable),
        }

    @classmethod
    def from_record(cls, record):
        job = cls(record.get("job_id"), record.get("path") or "",
                  record.get("language") or "auto",
                  record.get("translate") is True,
                  record.get("target_lang") or "",
                  record.get("user") or "")
        job.status = record.get("status") or STATUS_FAILED
        job.stage = record.get("stage") or "audio"
        job.progress = int(record.get("progress") or 0)
        job.message = record.get("message") or ""
        job.error = record.get("error") if isinstance(record.get("error"), dict) else None
        job.files = record.get("files") or []
        job.translation_partial = bool(record.get("translation_partial"))
        job.created_epoch = float(record.get("created_epoch") or time.time())
        job.updated_epoch = float(record.get("updated_epoch") or job.created_epoch)
        job.chunk_index = int(record.get("chunk_index") or 0)
        job.chunk_total = int(record.get("chunk_total") or 0)
        job.resumable = bool(record.get("resumable"))
        return job


def make_job_id():
    return "sub-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


class SubtitleService(object):
    """异步字幕任务服务：同一时间只跑 1 个任务，其余排队。"""

    def __init__(self, runtime_config, store, share_service, settings_provider,
                 client=None, ffmpeg=None, job_limit=MAX_JOBS,
                 retry_delay=1.0):
        self.config = runtime_config
        self.store = store
        self.shares = share_service
        self._settings = settings_provider
        self.client = client or OpenAIClient(retry_delay=retry_delay)
        # 外部 FFmpeg 路径来自 settings.ffmpeg_path（空=内置/自动探测）。
        self.ffmpeg = ffmpeg or video_similarity.FFmpegTool(
            settings_provider=settings_provider)
        try:
            self.ffmpeg.resolve(runtime_config)
        except Exception:
            pass
        self.job_limit = int(job_limit or MAX_JOBS)
        self._root = os.path.join(runtime_config.data_dir, "subtitles")
        self._jobs_dir = os.path.join(self._root, "jobs")
        os.makedirs(self._jobs_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        # v0026：同一时间只允许一个“模型列表/连接测试”探测（占线返回 PROBE_BUSY）。
        self._probe_lock = threading.Lock()
        self._jobs = {}
        self._queue = deque()
        self._active_id = None
        self._worker = None
        self._stopping = False
        self._load_jobs()

    # ------------------------------------------------------------------
    # 持久化 / 查询
    # ------------------------------------------------------------------
    def _job_dir(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
            raise invalid(u"字幕任务 ID 非法")
        return os.path.join(self._root, job_id)

    def _job_json(self, job_id):
        return os.path.join(self._jobs_dir, job_id + ".json")

    def _load_jobs(self):
        """DB 优先加载；字幕 JSON 文件保留为任务目录事实源/兜底。"""
        records = {}
        try:
            for record in self.store.load_subtitle_jobs(limit=1000):
                job_id = record.get("job_id")
                if isinstance(job_id, str) and JOB_ID_RE.match(job_id):
                    records[job_id] = record
        except Exception:
            pass
        try:
            names = sorted(os.listdir(self._jobs_dir))
        except OSError:
            names = []
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(self._jobs_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    record = json.load(handle)
            except (OSError, ValueError, UnicodeError):
                continue
            if not isinstance(record, dict):
                continue
            job_id = record.get("job_id")
            if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
                continue
            if job_id in records:
                continue
            records[job_id] = record
            try:
                self.store.save_subtitle_job(job_id, record)
            except Exception:
                pass
        for job_id, record in records.items():
            job = SubtitleJob.from_record(record)
            try:
                chunks = self.store.load_subtitle_chunks(job.job_id)
            except Exception:
                chunks = []
            done_chunks = sum(1 for item in chunks
                              if item.get("status") == CHUNK_DONE)
            if chunks:
                job.chunk_total = max(int(job.chunk_total or 0), len(chunks))
                job.chunk_index = max(int(job.chunk_index or 0), done_chunks)
            if job.status in ACTIVE_STATUSES:
                if chunks:
                    # plan C：有分片缓存/计划 → 暂停可继续，而不是失败
                    job.status = STATUS_PAUSED
                    job.resumable = True
                    job.error = None
                    job.message = u"服务重启导致任务暂停，可继续"
                else:
                    job.status = STATUS_FAILED
                    job.resumable = False
                    job.error = {"code": "SERVICE_RESTART",
                                 "message": u"服务重启导致任务中断，请重新提交"}
                    job.message = job.error["message"]
                self._persist(job)
            elif job.status == STATUS_PAUSED:
                # 用户暂停/升级暂停的任务：保留可继续状态
                job.resumable = True
            else:
                job.resumable = False
            self._jobs[job.job_id] = job

    def _persist(self, job):
        with job.lock:
            record = job.to_record()
        os.makedirs(self._jobs_dir, exist_ok=True)
        temporary = os.path.join(self._jobs_dir, "." + job.job_id + ".tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False)
                handle.flush()
            os.replace(temporary, self._job_json(job.job_id))
        except OSError:
            pass
        # 元数据镜像到 SQLite subtitle_jobs（旧 JSON 文件仍是任务目录事实源）。
        try:
            self.store.save_subtitle_job(job.job_id, record)
        except Exception:
            pass

    def _list_files(self, job_id):
        try:
            names = sorted(os.listdir(self._job_dir(job_id)))
        except OSError:
            return []
        files = []
        for name in names:
            if not SUBTITLE_FILE_RE.match(name):
                continue
            path = os.path.join(self._job_dir(job_id), name)
            try:
                files.append({"name": name, "size": os.path.getsize(path)})
            except OSError:
                continue
        return files

    def _get_job(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
            raise not_found(u"字幕任务不存在")
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise not_found(u"字幕任务不存在")
        return job

    def get(self, job_id):
        job = self._get_job(job_id)
        snapshot = job.snapshot()
        snapshot["files"] = self._list_files(job.job_id)
        return snapshot

    def list(self, limit=50):
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda item: item.created_epoch, reverse=True)
        return [item.snapshot() for item in jobs[:max(1, int(limit or 50))]]

    def counts(self):
        """任务状态计数（诊断用；不返回任务内容）。"""
        with self._lock:
            jobs = list(self._jobs.values())
        by_status = {}
        for job in jobs:
            status = job.status or "unknown"
            by_status[status] = by_status.get(status, 0) + 1
        active = sum(by_status.get(status, 0) for status in ACTIVE_STATUSES)
        return {"active": active, "total": len(jobs),
                "by_status": by_status}

    def get_file(self, job_id, name):
        self._get_job(job_id)
        if not isinstance(name, str) or not SUBTITLE_FILE_RE.match(name):
            raise invalid(u"字幕文件名非法")
        allowed = {item["name"] for item in self._list_files(job_id)}
        if name not in allowed:
            raise not_found(u"字幕文件不存在")
        path = os.path.join(self._job_dir(job_id), name)
        if not os.path.isfile(path):
            raise not_found(u"字幕文件不存在")
        return path

    # ------------------------------------------------------------------
    # v0026 云端探测：模型列表 / 连接测试（管理员；单次请求、不重试）
    # ------------------------------------------------------------------
    _PROBE_PREFIXES = {"asr": "subtitle_asr_", "llm": "subtitle_llm_"}

    def _probe_settings(self):
        try:
            return self._settings() or {}
        except Exception:
            return {}

    def _resolve_provider(self, kind, overrides=None):
        """解析探测用的 kind/base_url/api_key/model（显式覆盖 > settings）。"""
        overrides = overrides if isinstance(overrides, dict) else {}
        kind = str(kind or "").strip().lower()
        prefix = self._PROBE_PREFIXES.get(kind)
        if prefix is None:
            raise invalid(u"kind 必须是 asr 或 llm", details={"key": "kind"})
        settings = self._probe_settings()
        for field in ("base_url", "api_key", "model"):
            if (field in overrides and overrides.get(field) is not None
                    and not isinstance(overrides.get(field), str)):
                raise invalid(u"参数 %s 必须是字符串" % field,
                              details={"key": field})
        base_url = overrides.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            base_url = settings.get(prefix + "base_url") or ""
        base_url = settings_module.validate_base_url_value(
            "subtitle_%s_base_url" % kind, base_url)
        api_key = overrides.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            api_key = settings.get(prefix + "api_key") or ""
        api_key = api_key.strip() if isinstance(api_key, str) else ""
        model = overrides.get("model")
        if not isinstance(model, str) or not model.strip():
            model = settings.get(prefix + "model") or ""
        model = str(model).strip()
        if model:
            model_key = prefix + "model"
            model = settings_module.validate_patch(
                {model_key: model})[model_key]
        return {"kind": kind, "base_url": base_url, "api_key": api_key,
                "model": model, "settings": settings}

    def _acquire_probe(self):
        if not self._probe_lock.acquire(False):
            raise SubtitleError("PROBE_BUSY",
                                u"已有云端探测请求正在进行，请稍后重试")

    def _release_probe(self):
        try:
            self._probe_lock.release()
        except RuntimeError:
            pass

    @staticmethod
    def _parse_models(body, api_key=""):
        """解析 /models 响应：兼容 data/models/顶层数组；去重排序过滤后取前 500。"""
        if isinstance(body, (bytes, bytearray)):
            text = bytes(body).decode("utf-8", "replace")
        else:
            text = str(body or "")
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            raise SubtitleError(
                "MODELS_UNSUPPORTED",
                u"模型列表响应不是合法 JSON（该服务可能不支持 /models）")
        items = None
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                items = data
            elif isinstance(payload.get("models"), list):
                items = payload.get("models")
        elif isinstance(payload, list):
            items = payload
        if items is None:
            raise SubtitleError(
                "MODELS_UNSUPPORTED",
                u"模型列表响应格式不受支持（缺少 data/models 数组）")
        seen = {}
        for item in items:
            if isinstance(item, str):
                model_id = item
                owned_by = ""
            elif isinstance(item, dict):
                model_id = item.get("id")
                owned_by = item.get("owned_by")
                if not isinstance(owned_by, str):
                    owned_by = ""
            else:
                continue
            if not isinstance(model_id, str):
                continue
            model_id = model_id.strip()
            if not model_id or len(model_id) > MODEL_ID_MAX_LEN:
                continue
            key = model_id.lower()
            if key in seen:
                continue
            seen[key] = {"id": model_id,
                         "owned_by": owned_by.strip()[:100]}
        ordered = sorted(seen.values(),
                         key=lambda entry: (entry["id"].lower(), entry["id"]))
        truncated = len(ordered) > MODELS_MAX
        return ordered[:MODELS_MAX], truncated

    def list_models(self, kind, overrides=None):
        """GET {base_url}/models（20s 超时、不重试；密钥绝不回传）。

        模型列表只依赖 base_url/api_key；忽略 model/language 等覆盖值，
        避免用户模型输入框暂态非法时连列表都取不到。
        """
        overrides = overrides if isinstance(overrides, dict) else {}
        provider = self._resolve_provider(kind, {
            "base_url": overrides.get("base_url"),
            "api_key": overrides.get("api_key"),
        })
        base_url = provider["base_url"]
        api_key = provider["api_key"]
        url = base_url.rstrip("/") + "/models"
        self._acquire_probe()
        started = time.monotonic()
        try:
            raw_kind, status, body = self.client._get_raw(
                url, api_key, MODELS_TIMEOUT_SEC)
            latency_ms = int(round((time.monotonic() - started) * 1000))
            if raw_kind == "timeout":
                raise SubtitleError("PROVIDER_TIMEOUT", u"获取模型列表超时")
            if raw_kind == "network":
                raise SubtitleError("PROVIDER_NETWORK_ERROR",
                                    u"无法连接云端服务")
            if raw_kind == "http":
                if status in (401, 403):
                    raise SubtitleError(
                        "PROVIDER_AUTH_FAILED",
                        u"云端服务认证失败，请检查 API Key 配置")
                if status in (404, 405, 501):
                    raise SubtitleError(
                        "MODELS_UNSUPPORTED",
                        u"该服务不支持模型列表接口（HTTP %d）" % status)
                raise SubtitleError(
                    "PROVIDER_HTTP_ERROR",
                    provider_error_message(u"获取模型列表失败", status, body,
                                           api_key))
            models, truncated = self._parse_models(body, api_key)
            warning = ""
            if not models:
                warning = u"未获取到任何模型（服务可能不支持 /models 或账号无权限）"
            return {
                "kind": provider["kind"],
                "base_url": base_url,
                "count": len(models),
                "truncated": bool(truncated),
                "latency_ms": latency_ms,
                "models": models,
                "warning": warning,
            }
        finally:
            self._release_probe()

    def test_connection(self, kind, overrides=None):
        """连接测试：ASR 用 1s 静音音频、LLM 翻译固定短句（60s 超时、不重试）。"""
        provider = self._resolve_provider(kind, overrides)
        overrides = overrides if isinstance(overrides, dict) else {}
        if provider["kind"] == "asr":
            return self._test_connection_asr(provider, overrides)
        return self._test_connection_llm(provider, overrides)

    def _make_probe_tempdir(self):
        root = os.path.join(self.config.data_dir, "tmp")
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            root = None
        return tempfile.mkdtemp(prefix="dc-subtitle-probe-", dir=root)

    def _test_connection_asr(self, provider, overrides):
        settings = provider["settings"]
        api_key = provider["api_key"]
        model = provider["model"] or "whisper-1"
        language = overrides.get("language") \
            or settings.get("subtitle_asr_language") or "auto"
        if not isinstance(language, str) or not LANGUAGE_RE.match(
                language.strip()):
            raise invalid(u"语言代码非法", details={"key": "language"})
        language = language.strip()
        self._acquire_probe()
        started = time.monotonic()
        tempdir = None
        try:
            if not self.ffmpeg.available():
                raise SubtitleError("FFMPEG_UNAVAILABLE",
                                    u"未找到 FFmpeg，无法生成测试音频")
            try:
                tempdir = self._make_probe_tempdir()
                audio_path = os.path.join(tempdir, "silence.mp3")
                self.ffmpeg.extract_silence_mp3(audio_path, 1.0, 15.0)
            except video_similarity.VideoTimeout:
                raise SubtitleError("AUDIO_FAILED", u"测试音频生成超时")
            except video_similarity.VideoSkip as exc:
                raise SubtitleError(
                    "AUDIO_FAILED",
                    mask_secret(u"测试音频生成失败：%s" % (exc.detail or ""),
                                api_key))
            except OSError:
                raise SubtitleError("AUDIO_FAILED",
                                    u"测试音频生成失败（临时目录不可用）")
            status, body = self.client.probe_transcribe(
                provider["base_url"], api_key, model, language, audio_path,
                CONNECTION_TEST_TIMEOUT_SEC)
            latency_ms = int(round((time.monotonic() - started) * 1000))
            detail = self._asr_probe_detail(body, api_key)
            return {
                "kind": "asr",
                "mode": "asr",
                "model": model,
                "base_url": provider["base_url"],
                "latency_ms": latency_ms,
                # 顶层 warning 便于前端统一按“成功但有告警”展示；
                # detail.warning 保留给需要细节的调用方。
                "warning": detail.get("warning") or "",
                "detail": detail,
            }
        finally:
            if tempdir:
                shutil.rmtree(tempdir, ignore_errors=True)
            self._release_probe()

    @staticmethod
    def _asr_probe_detail(body, api_key=""):
        """按响应结构判定 format/segments/has_timestamps/text_chars/warning。"""
        if isinstance(body, (bytes, bytearray)):
            text = bytes(body).decode("utf-8", "replace")
        else:
            text = str(body or "")
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            segments = payload.get("segments")
            if isinstance(segments, list) and segments:
                valid = [item for item in segments if isinstance(item, dict)]
                text_chars = 0
                for item in valid:
                    value = item.get("text")
                    if isinstance(value, str):
                        text_chars += len(value)
                return {"format": "verbose_json", "segments": len(valid),
                        "has_timestamps": True, "text_chars": text_chars,
                        "warning": ""}
            if isinstance(segments, list):
                value = payload.get("text")
                text_chars = len(value) if isinstance(value, str) else 0
                # 静音测试音频在兼容服务（如自建 WhisperLiveKit）上通常返回
                # 空 segments；只要响应带 verbose_json 结构就说明接口兼容，
                # 不应让用户误以为模型不支持时间轴。
                verbose_shape = (
                    isinstance(payload.get("task"), str)
                    or "duration" in payload or "language" in payload)
                warning = (
                    u"测试音频为静音，未产生转写（服务已返回 verbose_json "
                    u"结构，接口兼容）" if verbose_shape
                    else u"响应 segments 为空，未返回时间轴")
                return {"format": "verbose_json", "segments": 0,
                        "has_timestamps": False, "text_chars": text_chars,
                        "warning": warning}
            extra_srt = payload.get("srt") or payload.get("subtitle")
            if isinstance(extra_srt, str):
                cues = parse_srt(extra_srt)
                if cues:
                    return {"format": "srt", "segments": len(cues),
                            "has_timestamps": True,
                            "text_chars": sum(len(cue["text"])
                                              for cue in cues),
                            "warning": ""}
            if isinstance(payload.get("text"), str):
                return {"format": "text", "segments": 0,
                        "has_timestamps": False,
                        "text_chars": len(payload["text"]),
                        "warning": u"响应只有纯文本，没有时间轴；"
                                   u"请使用支持 verbose_json/segments 的模型"}
        cues = parse_srt(text.strip())
        if cues:
            return {"format": "srt", "segments": len(cues),
                    "has_timestamps": True,
                    "text_chars": sum(len(cue["text"]) for cue in cues),
                    "warning": ""}
        return {"format": "unknown", "segments": 0,
                "has_timestamps": False, "text_chars": len(text.strip()),
                "warning": u"无法识别 ASR 响应格式（连接成功，但可能不返回字幕时间轴）"}

    def _test_connection_llm(self, provider, overrides):
        settings = provider["settings"]
        api_key = provider["api_key"]
        model = provider["model"] or "gpt-4.1-mini"
        target_lang = overrides.get("target_lang") \
            or settings.get("subtitle_translate_target") or "zh"
        if (not isinstance(target_lang, str)
                or not LANGUAGE_RE.match(target_lang.strip())
                or target_lang.strip().lower() == "auto"):
            raise invalid(u"目标语言非法", details={"key": "target_lang"})
        target_lang = target_lang.strip()
        self._acquire_probe()
        started = time.monotonic()
        try:
            status, body = self.client.probe_chat(
                provider["base_url"], api_key, model, ["Hello world"],
                target_lang, CONNECTION_TEST_TIMEOUT_SEC)
            latency_ms = int(round((time.monotonic() - started) * 1000))
            text = (bytes(body).decode("utf-8", "replace")
                    if isinstance(body, (bytes, bytearray)) else str(body or ""))
            parsed = OpenAIClient._parse_chat_content(text)
            if (not isinstance(parsed, list) or len(parsed) != 1
                    or not isinstance(parsed[0], str)
                    or not parsed[0].strip()):
                raise SubtitleError(
                    "PROVIDER_REJECTED",
                    u"LLM 返回格式不正确：需要长度 1 的字符串数组")
            return {
                "kind": "llm",
                "mode": "chat",
                "model": model,
                "base_url": provider["base_url"],
                "latency_ms": latency_ms,
                "warning": "",
                "detail": {"items": 1, "text_chars": len(parsed[0]),
                           "preview": parsed[0][:200],
                           "warning": ""},
            }
        finally:
            self._release_probe()

    # ------------------------------------------------------------------
    # 提交 / 取消
    # ------------------------------------------------------------------
    def submit(self, user, payload):
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        settings = self._settings() or {}
        if not settings.get("subtitle_enabled"):
            raise SubtitleError("SUBTITLE_DISABLED", u"云端字幕功能未启用")
        if payload.get("confirm_cloud") is not True:
            raise SubtitleError("CLOUD_CONFIRM_REQUIRED",
                                u"请先确认音频将上传到所配置的第三方服务")
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            raise invalid(u"path 不能为空")
        self.shares.validate_file(path)
        extensions = (settings.get("video_extensions")
                      or list(config.DEFAULT_VIDEO_EXTENSIONS))
        allowed_ext = frozenset(str(item).lower() for item in extensions)
        extension = os.path.splitext(path)[1].lower()
        if extension not in allowed_ext:
            raise SubtitleError("INVALID_VIDEO", u"只支持视频文件生成字幕")

        language = (payload.get("language")
                    or settings.get("subtitle_asr_language") or "auto")
        if not isinstance(language, str) or not LANGUAGE_RE.match(language.strip()):
            raise invalid(u"语言代码非法")
        language = language.strip()
        translate = payload.get("translate", False)
        if not isinstance(translate, bool):
            raise invalid(u"translate 必须是布尔值")
        if translate and not settings.get("subtitle_translate_enabled"):
            raise SubtitleError("TRANSLATE_DISABLED", u"字幕翻译功能未启用")
        target_lang = (payload.get("target_lang")
                       or settings.get("subtitle_translate_target") or "zh")
        if translate:
            if (not isinstance(target_lang, str)
                    or not LANGUAGE_RE.match(target_lang.strip())
                    or target_lang.strip().lower() == "auto"):
                raise invalid(u"目标语言非法")
            target_lang = target_lang.strip()
        else:
            target_lang = settings.get("subtitle_translate_target") or "zh"

        job_id = make_job_id()
        job = SubtitleJob(job_id, os.path.normpath(path), language, translate,
                          target_lang, user)
        with self._lock:
            self._jobs[job_id] = job
            self._queue.append(job_id)
            self._trim_jobs_locked()
        self._persist(job)
        self.store.audit("subtitle_start", user, {
            "job_id": job_id,
            "path": job.path,
            "language": language,
            "translate": bool(translate),
            "target_lang": target_lang,
        })
        self._ensure_worker()
        return job.snapshot()

    def cancel(self, job_id):
        job = self._get_job(job_id)
        with self._lock:
            if job.status in FINISHED_STATUSES:
                return job.snapshot()
            job.cancel_event.set()
            job.pause_event.clear()
            if job.status in (STATUS_QUEUED, STATUS_PAUSED):
                job.status = STATUS_CANCELLED
                job.message = u"任务已取消"
                job.resumable = False
                job.updated_epoch = time.time()
                try:
                    self._queue.remove(job_id)
                except ValueError:
                    pass
                self._cond.notify_all()
                self._persist(job)
            else:
                job.message = u"正在取消任务"
                self._persist(job)
        self.store.audit("subtitle_cancel", job.user, {"job_id": job_id})
        return job.snapshot()

    def pause(self, job_id):
        """暂停任务：当前 ASR 分片/翻译批次完成后停在 paused，可继续。"""
        job = self._get_job(job_id)
        with self._lock:
            if job.status in FINISHED_STATUSES:
                raise invalid(u"已完成任务不能暂停")
            if job.status == STATUS_PAUSED:
                return job.snapshot()
            previous = job.status
            job.pause_event.set()
            job.status = STATUS_PAUSED
            job.resumable = True
            job.message = u"任务已暂停，可继续" if previous != STATUS_QUEUED else u"任务已暂停"
            job.updated_epoch = time.time()
            if previous == STATUS_QUEUED:
                try:
                    self._queue.remove(job_id)
                except ValueError:
                    pass
                self._cond.notify_all()
            self._persist(job)
        self.store.audit("subtitle_pause", job.user, {
            "job_id": job_id,
            "chunk_index": int(job.chunk_index),
            "chunk_total": int(job.chunk_total),
        })
        return job.snapshot()

    def resume(self, job_id):
        """继续 paused 任务：跳过已完成 ASR 分片（DB 缓存），从下一个未完成分片继续。"""
        job = self._get_job(job_id)
        with self._lock:
            if job.status != STATUS_PAUSED:
                raise invalid(u"只有已暂停的任务可以继续")
            job.pause_event.clear()
            job.error = None
            job.resumable = True
            job.updated_epoch = time.time()
            if self._active_id == job.job_id:
                job.status = STATUS_RUNNING
                job.message = u"任务继续"
            else:
                job.status = STATUS_QUEUED
                job.message = u"排队等待继续"
                if job.job_id not in self._queue:
                    self._queue.append(job.job_id)
                self._cond.notify_all()
            self._persist(job)
        self.store.audit("subtitle_resume", job.user, {
            "job_id": job_id,
            "chunk_index": int(job.chunk_index),
            "chunk_total": int(job.chunk_total),
        })
        self._ensure_worker()
        return job.snapshot()

    def shutdown(self, timeout=5.0):
        """优雅退出：在分片边界暂停可继续任务（计划 C），而不是取消。"""
        with self._lock:
            self._stopping = True
            jobs = list(self._jobs.values())
            for job in jobs:
                if job.status in (STATUS_RUNNING, STATUS_TRANSLATING):
                    # 运行中任务在分片边界暂停；重启后有分片缓存 → 可继续
                    job.pause_event.set()
                # queued 任务没有分片/计划：保持 queued，重启后按
                # SERVICE_RESTART 失败（只有有计划的 queued 才可继续）
            self._cond.notify_all()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(0.1, float(timeout)))

    # ------------------------------------------------------------------
    # 队列 worker
    # ------------------------------------------------------------------
    def _ensure_worker(self):
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            if self._stopping:
                return
            self._worker = threading.Thread(target=self._worker_loop,
                                            name="subtitles", daemon=True)
            self._worker.start()

    def _worker_loop(self):
        while True:
            with self._lock:
                while not self._queue and not self._stopping:
                    self._cond.wait(timeout=0.5)
                if self._stopping:
                    return
                job_id = self._queue.popleft()
                self._active_id = job_id
            job = self._jobs.get(job_id)
            try:
                if job is not None and job.status == STATUS_QUEUED:
                    self._process(job)
            finally:
                with self._lock:
                    self._active_id = None
                    self._cond.notify_all()

    # ------------------------------------------------------------------
    # 处理流程
    # ------------------------------------------------------------------
    def _update(self, job, **changes):
        with job.lock:
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_epoch = time.time()
            # 在释放 job.lock 前完成持久化，避免其它线程读到新状态时
            # JSON/SQLite 里还是旧状态（重载会把 done 误判为 SERVICE_RESTART）。
            self._persist(job)

    def _check_cancel(self, job):
        if job.cancel_event.is_set():
            raise SubtitleCancelled()

    def _stop_check(self, job):
        return job.cancel_event.is_set()

    def _check_control(self, job):
        """取消优先；暂停只在 ASR 分片/翻译批次边界生效（计划 C）。"""
        with job.lock:
            if job.cancel_event.is_set():
                raise SubtitleCancelled()
            if job.pause_event.is_set():
                if job.status == STATUS_PAUSED:
                    raise SubtitlePaused()
                # resume 与暂停请求竞争：resume 已抢先恢复运行，继续即可
                job.pause_event.clear()

    def _handle_paused(self, job):
        with self._lock:
            if job.status != STATUS_PAUSED:
                # resume 恰好发生在暂停边界之前：重新入队继续，任务不丢
                if (job.status in ACTIVE_STATUSES
                        and job.job_id not in self._queue):
                    job.status = STATUS_QUEUED
                    self._queue.append(job.job_id)
                    self._cond.notify_all()
                return
            job.resumable = True
            job.message = u"任务已暂停，当前分片已完成"
            job.updated_epoch = time.time()
            self._persist(job)
        self.store.audit("subtitle_pause", job.user, {
            "job_id": job.job_id,
            "chunk_index": int(job.chunk_index),
            "chunk_total": int(job.chunk_total),
        })

    def _process(self, job):
        settings = self._settings() or {}
        try:
            with job.lock:
                if job.status in FINISHED_STATUSES or job.status == STATUS_PAUSED:
                    # 队列 pop 与 pause 竞争：暂停请求优先，不启动执行
                    return
                job.status = STATUS_RUNNING
                job.error = None
                job.updated_epoch = time.time()
            self._persist(job)
            self._check_control(job)
            if not self.ffmpeg.available():
                raise SubtitleError("FFMPEG_UNAVAILABLE", u"未找到 FFmpeg，无法提取音频")
            timeout = float(settings.get("subtitle_asr_timeout_sec") or 300)
            probe = self.ffmpeg.probe_media(job.path, timeout,
                                            lambda: self._stop_check(job))
            if not probe.get("has_audio"):
                raise SubtitleError("NO_AUDIO",
                                    u"视频没有音轨（no_audio），无法生成字幕")
            duration = probe.get("duration")
            if not duration or duration <= 0:
                raise SubtitleError("NO_DURATION", u"无法读取视频时长")
            chunk_sec = int(settings.get("subtitle_asr_chunk_sec") or 600)
            max_audio_mb = int(settings.get("subtitle_asr_max_audio_mb") or 24)
            max_bytes = max_audio_mb * 1024 * 1024
            temp_root = os.path.join(self.config.data_dir, "tmp")
            try:
                os.makedirs(temp_root, exist_ok=True)
            except OSError:
                temp_root = None
            tempdir = tempfile.mkdtemp(prefix="dc-subtitle-", dir=temp_root)
            try:
                plan = self._chunk_plan(job, duration, chunk_sec)
                if not plan:
                    raise SubtitleError("NO_AUDIO", u"视频没有可用音轨")
                resuming = int(job.chunk_index or 0) > 0
                self._update(
                    job, status=STATUS_RUNNING,
                    stage="asr" if resuming else "audio",
                    progress=20 if resuming else 2,
                    message=u"正在识别语音" if resuming else u"正在提取音频")
                cues = self._run_chunked_asr(job, settings, plan, max_bytes,
                                             tempdir)
                cues = dedupe_cues(cues)
                if not cues:
                    raise SubtitleError("NO_SPEECH", u"未识别到可用字幕内容")
                translations = None
                partial = False
                if job.translate:
                    self._update(job, status=STATUS_TRANSLATING,
                                 stage="translate", progress=70,
                                 message=u"正在翻译字幕")
                    translations, partial = self._translate_cues(
                        job, settings, cues)
                # 最后一个翻译批次结束时也要响应暂停（resume 会重跑翻译批次，
                # 但 ASR 分片缓存保证不重复上传音频）
                self._check_control(job)
                self._update(job, stage="write", progress=91,
                             message=u"正在写入字幕文件")
                files = self._write_outputs(job, cues, translations, partial)
                with job.lock:
                    total = int(job.chunk_total or 0)
                self._update(job, status=STATUS_DONE, stage="write",
                             progress=100, message=u"字幕生成完成",
                             files=files, error=None, resumable=False,
                             chunk_index=total)
                self.store.audit("subtitle_done", job.user, {
                    "job_id": job.job_id,
                    "files": [item["name"] for item in files],
                    "translation_partial": bool(partial),
                })
            finally:
                shutil.rmtree(tempdir, ignore_errors=True)
        except SubtitlePaused:
            self._handle_paused(job)
        except SubtitleCancelled:
            with job.lock:
                if job.status != STATUS_CANCELLED:
                    job.status = STATUS_CANCELLED
                    job.message = u"任务已取消"
                    job.error = None
                    job.resumable = False
                    job.updated_epoch = time.time()
                self._persist(job)
            self.store.audit("subtitle_cancel", job.user, {"job_id": job.job_id})
        except video_similarity.VideoStopped:
            self.cancel(job.job_id)
        except video_similarity.VideoTimeout:
            self._fail(job, "TIMEOUT", u"音频提取超时")
        except SubtitleError as exc:
            self._fail(job, exc.code, exc.message)
        except Exception:
            if os.environ.get("DUP_SUBTITLE_DEBUG") == "1":
                import traceback
                traceback.print_exc()
            self._fail(job, "INTERNAL_ERROR", u"字幕生成失败，请稍后重试")

    def _fail(self, job, code, message):
        with job.lock:
            job.resumable = False
        self._update(job, status=STATUS_FAILED, message=message,
                     error={"code": code, "message": message})
        self.store.audit("subtitle_failed", job.user, {
            "job_id": job.job_id, "code": code})

    def _chunk_plan(self, job, duration, chunk_sec):
        """读取已有分片计划；没有则为整个任务创建 pending 分片行（可重启恢复）。"""
        stored = self.store.load_subtitle_chunks(job.job_id)
        if stored:
            plan = []
            done = 0
            for item in stored:
                start = float(item.get("start_sec") or 0.0)
                end = float(item.get("end_sec") or 0.0)
                plan.append((start, max(0.0, end - start)))
                if item.get("status") == CHUNK_DONE:
                    done += 1
            with job.lock:
                job.chunk_total = len(plan)
                job.chunk_index = max(int(job.chunk_index or 0), done)
            return plan
        plan = plan_chunks(duration, chunk_sec)
        for index, (start, length) in enumerate(plan):
            try:
                self.store.save_subtitle_chunk(
                    job.job_id, index, start, start + length,
                    CHUNK_PENDING, [])
            except Exception:
                pass
        with job.lock:
            job.chunk_total = len(plan)
            job.chunk_index = 0
        return plan

    def _extract_chunk_audio(self, job, settings, start, length, max_bytes,
                             tempdir, counter):
        """抽取单个逻辑分片的音频；超过 max_audio_mb 时继续对半切。"""
        timeout = float(settings.get("subtitle_asr_timeout_sec") or 300)
        queue = [(float(start), float(length))]
        results = []
        while queue:
            self._check_cancel(job)
            chunk_start, chunk_length = queue.pop(0)
            path = os.path.join(tempdir, "chunk-%05d.mp3" % counter[0])
            counter[0] += 1
            try:
                self.ffmpeg.extract_audio_mp3(
                    job.path, path, chunk_start, chunk_length, timeout,
                    lambda: self._stop_check(job))
            except video_similarity.VideoStopped:
                raise SubtitleCancelled()
            try:
                size = os.path.getsize(path)
            except OSError:
                raise SubtitleError("NO_AUDIO", u"无法生成音频分片")
            if size <= max_bytes or chunk_length <= 0.5:
                results.append((chunk_start, chunk_length, path))
                continue
            try:
                os.unlink(path)
            except OSError:
                pass
            half = chunk_length / 2.0
            if half <= 0.001:
                raise SubtitleError("AUDIO_TOO_LARGE",
                                    u"音频分块后仍超过大小上限")
            queue.insert(0, (chunk_start + half, chunk_length - half))
            queue.insert(0, (chunk_start, half))
            if len(queue) + len(results) > 500:
                raise SubtitleError("AUDIO_TOO_LARGE", u"音频分块数量过多")
        results.sort(key=lambda item: item[0])
        return results

    def _run_chunked_asr(self, job, settings, plan, max_bytes, tempdir):
        """按分片顺序 ASR；已完成分片直接读 DB 缓存，不重复上传音频。"""
        base_url = settings.get("subtitle_asr_base_url") or ""
        api_key = settings.get("subtitle_asr_api_key") or ""
        model = settings.get("subtitle_asr_model") or "whisper-1"
        language = job.language or "auto"
        timeout = float(settings.get("subtitle_asr_timeout_sec") or 300)
        cached = {item["chunk_index"]: item
                  for item in self.store.load_subtitle_chunks(job.job_id)}
        total = max(1, len(plan))
        all_cues = []
        counter = [0]
        for index, (start, length) in enumerate(plan):
            self._check_control(job)
            done_row = cached.get(index)
            if done_row is not None and done_row.get("status") == CHUNK_DONE:
                all_cues.extend(done_row.get("cues") or [])
                with job.lock:
                    job.chunk_index = max(int(job.chunk_index or 0), index + 1)
                continue
            audio = self._extract_chunk_audio(
                job, settings, start, length, max_bytes, tempdir, counter)
            chunk_cues = []
            try:
                for sub_start, _sub_length, path in audio:
                    self._check_cancel(job)
                    parsed = self.client.transcribe(
                        base_url, api_key, model, language, path, timeout,
                        job.cancel_event)
                    for cue in parsed:
                        chunk_cues.append({
                            "start": max(0.0, float(cue["start"]) + sub_start),
                            "end": max(0.0, float(cue["end"]) + sub_start),
                            "text": cue["text"],
                        })
            finally:
                for _sub_start, _sub_length, path in audio:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            chunk_cues = dedupe_cues(chunk_cues)
            try:
                self.store.save_subtitle_chunk(
                    job.job_id, index, start, start + length,
                    CHUNK_DONE, chunk_cues)
            except Exception:
                pass
            all_cues.extend(chunk_cues)
            progress = 20 + int(50.0 * (index + 1) / total)
            self._update(
                job, stage="asr", progress=progress,
                message=u"正在识别语音 %d/%d" % (index + 1, total),
                chunk_index=index + 1, chunk_total=total)
            # 暂停只在完整分片保存后生效
            self._check_control(job)
        return all_cues

    def _translate_cues(self, job, settings, cues):
        base_url = settings.get("subtitle_llm_base_url") or ""
        api_key = settings.get("subtitle_llm_api_key") or ""
        model = settings.get("subtitle_llm_model") or "gpt-4.1-mini"
        timeout = float(settings.get("subtitle_translate_timeout_sec") or 120)
        batch_size = max(1, int(settings.get("subtitle_translate_batch_size") or 20))
        translations = []
        partial = False
        total = max(1, len(cues))
        for start in range(0, len(cues), batch_size):
            # 翻译批次是暂停安全点：当前批完成后暂停；resume 重新翻译当前批次即可
            self._check_control(job)
            batch = cues[start:start + batch_size]
            texts = [sanitize_cue_text(item["text"]) for item in batch]
            result = None
            attempt = 0
            while attempt < 2:
                attempt += 1
                try:
                    candidate = self.client.translate(
                        base_url, api_key, model, texts, job.target_lang,
                        timeout, job.cancel_event)
                except SubtitleCancelled:
                    raise
                except SubtitleError:
                    candidate = None
                if (isinstance(candidate, list)
                        and len(candidate) == len(texts)
                        and all(isinstance(item, str) and item.strip()
                                for item in candidate)):
                    result = [sanitize_cue_text(item) for item in candidate]
                    break
            if result is None:
                partial = True
                result = texts
            translations.extend(result)
            progress = 70 + int(20.0 * (start + len(batch)) / total)
            self._update(job, stage="translate", progress=progress,
                         message=u"正在翻译字幕 %d/%d" % (
                             min(start + len(batch), len(cues)), len(cues)))
        return translations, partial

    def _write_outputs(self, job, cues, translations, partial):
        directory = self._job_dir(job.job_id)
        os.makedirs(directory, exist_ok=True)
        self._write_file(directory, "original.srt", build_srt(cues))
        self._write_file(directory, "original.vtt", build_vtt(cues))
        if translations is not None:
            target = job.target_lang
            self._write_file(directory, "%s.srt" % target,
                             build_srt(cues, translations))
            self._write_file(directory, "%s.vtt" % target,
                             build_vtt(cues, translations))
            self._write_file(directory, "bilingual.%s.srt" % target,
                             build_srt(cues, translations, bilingual=True))
        job.translation_partial = bool(partial)
        return self._list_files(job.job_id)

    @staticmethod
    def _write_file(directory, name, content):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)

    # ------------------------------------------------------------------
    # LRU 清理
    # ------------------------------------------------------------------
    def _trim_jobs_locked(self):
        if len(self._jobs) <= self.job_limit:
            return
        finished = [job for job in self._jobs.values()
                    if job.status in FINISHED_STATUSES]
        finished.sort(key=lambda item: item.created_epoch)
        while len(self._jobs) > self.job_limit and finished:
            job = finished.pop(0)
            self._jobs.pop(job.job_id, None)
            self._remove_job_files(job.job_id)
            try:
                os.unlink(self._job_json(job.job_id))
            except OSError:
                pass
            try:
                self.store.delete_subtitle_job(job.job_id)
            except Exception:
                pass

    def _remove_job_files(self, job_id):
        try:
            shutil.rmtree(self._job_dir(job_id), ignore_errors=True)
        except Exception:
            pass
