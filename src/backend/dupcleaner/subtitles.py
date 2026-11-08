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

from . import config, fsutil, settings as settings_module, timeutil, video_similarity
from .errors import ApiError, invalid, not_found

JOB_ID_RE = re.compile(r"^sub-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
SUBTITLE_FILE_RE = re.compile(
    r"^(?:original\.(?:srt|vtt)|bilingual\.[A-Za-z0-9_-]{2,16}\.srt"
    r"|[A-Za-z0-9_-]{2,16}\.(?:srt|vtt))$")
LANGUAGE_RE = re.compile(r"^(?:auto|[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?)$")
# 已完成任务保留上限（可被设置 subtitle_job_limit 覆盖；docs/SUBTITLE_BATCH_CONTRACT.md §2）
MAX_JOBS = config.SUBTITLE_JOB_LIMIT_DEFAULT
BATCH_ID_RE = config.SUBTITLE_BATCH_ID_RE
BATCH_GROUP_ID_RE = config.SUBTITLE_BATCH_GROUP_ID_RE

# 来源：asr=云端语音识别生成；existing_srt=对视频同目录已有 .srt 只翻译
SOURCE_ASR = "asr"
SOURCE_EXISTING_SRT = "existing_srt"
SOURCES = (SOURCE_ASR, SOURCE_EXISTING_SRT)
# 写回失败的 warning（任务仍 done，可下载）
WARNING_OUTPUT_NOT_WRITABLE = "OUTPUT_NOT_WRITABLE"
WARNING_OUTPUT_PARTIAL = "OUTPUT_WRITE_PARTIAL"
# v0032：server 音频模式流复制失败时回退 NAS 抽音频（任务仍 done）
WARNING_SERVER_AUDIO_FALLBACK = "SERVER_AUDIO_FALLBACK"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_TRANSLATING = "translating"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_PAUSED = "paused"
FINISHED_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_TRANSLATING)
# v0034：任务列表状态过滤。all/空=全部；active=running+translating（不含 queued，
# 便于“执行中”视图把真正在跑的任务排在前面）。
LIST_STATUS_FILTERS = ("all", "active", STATUS_QUEUED, STATUS_RUNNING,
                       STATUS_TRANSLATING, STATUS_PAUSED, STATUS_DONE,
                       STATUS_FAILED, STATUS_CANCELLED)
ACTIVE_LIST_STATUSES = (STATUS_RUNNING, STATUS_TRANSLATING)

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
# v0048：失败分类 / 音轨选择 / 优先级 工具函数
# ---------------------------------------------------------------------------
def failure_category(code, message=u""):
    """把错误码/消息归入失败分类（audio_extract/no_speech/timeout/...）。"""
    code = str(code or "").upper()
    category = config.SUBTITLE_FAILURE_CODE_CATEGORY.get(code)
    if category:
        return category
    text = (message or u"").lower()
    rules = (
        (u"permission", "permission"),
        (u"denied", "permission"),
        (u"权限", "permission"),
        (u"no audio", "audio_extract"),
        (u"无音轨", "audio_extract"),
        (u"没有音轨", "audio_extract"),
        (u"timeout", "timeout"),
        (u"timed out", "timeout"),
        (u"超时", "timeout"),
        (u"network", "network"),
        (u"connect", "network"),
        (u"网络", "network"),
        (u"no speech", "no_speech"),
        (u"未识别", "no_speech"),
        (u"无语音", "no_speech"),
    )
    for needle, candidate in rules:
        if needle in text:
            return candidate
    return "unknown"


def failure_retryable(category):
    return (category or "").lower() in config.SUBTITLE_RETRYABLE_CATEGORIES


def normalize_audio_track(value):
    """音轨序号：None/''/'auto' → None；0..31 的整数或数字串。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("audio_track must be an integer")
    if isinstance(value, int):
        track = value
    else:
        text = str(value).strip().lower()
        if not text or text in ("auto", "default"):
            return None
        if not text.isdigit():
            raise ValueError("audio_track must be an integer")
        track = int(text)
    if track < config.SUBTITLE_AUDIO_TRACK_HEADER or track > config.SUBTITLE_AUDIO_TRACK_MAX_INDEX:
        raise ValueError("audio_track out of range")
    return track


def normalize_audio_language(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("auto", "default"):
        return None
    if len(text) > 16 or not re.match(r"^[A-Za-z]{2,8}([_-][A-Za-z0-9]{1,8})?$", text):
        raise ValueError("invalid audio language")
    return text


def _median(values):
    values = sorted(float(item) for item in (values or []) if item is not None)
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


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
# 视频同目录字幕文件（docs/SUBTITLE_BATCH_CONTRACT.md §6）
# ---------------------------------------------------------------------------

def split_video_stem(path):
    """返回 (目录, 去扩展名文件名)；目录可能为空，调用方需保证绝对路径。"""
    directory = os.path.dirname(path or "")
    stem = os.path.splitext(os.path.basename(path or ""))[0]
    return directory, stem


def subtitle_output_names(stem, target_lang):
    """视频同目录字幕文件名（只写 .srt；vtt 只留在任务目录）。"""
    return {
        "original": u"%s.srt" % stem,
        "translation": u"%s.%s.srt" % (stem, target_lang),
        "bilingual": u"%s.bilingual.%s.srt" % (stem, target_lang),
    }


def find_existing_in_dir(directory, filename):
    """在目录中按大小写不敏感查找普通文件；返回真实路径或 None。"""
    if not directory or not filename:
        return None
    target = filename.lower()
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                if entry.name.lower() != target:
                    continue
                try:
                    if entry.is_file(follow_symlinks=False):
                        return entry.path
                except OSError:
                    continue
    except OSError:
        return None
    return None


def find_source_srt(video_path):
    """视频同目录的 `<stem>.srt`（大小写不敏感）；不存在返回 None。"""
    directory, stem = split_video_stem(video_path)
    return find_existing_in_dir(directory, u"%s.srt" % stem)


def list_related_subtitles(video_path, limit=10):
    """视频同目录下同 stem 的 .srt 文件名列表（用于枚举接口提示，最多 limit 个）。"""
    directory, stem = split_video_stem(video_path)
    prefix = (stem + ".").lower()
    found = []
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                name = entry.name
                lowered = name.lower()
                if not (lowered.startswith(prefix) and lowered.endswith(".srt")):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                found.append(name)
                if len(found) >= limit:
                    break
    except OSError:
        return []
    found.sort()
    return found


def atomic_write_text(path, content):
    """UTF-8/LF 原子写入：同目录临时文件 + os.replace。返回字节数。"""
    directory = os.path.dirname(path)
    temporary = os.path.join(directory, "." + os.path.basename(path) + ".tmp")
    data = content if isinstance(content, str) else str(content)
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    os.replace(temporary, path)
    return len(data.encode("utf-8"))


# ---------------------------------------------------------------------------
# OpenAI 兼容 HTTP 客户端
# ---------------------------------------------------------------------------

def encode_multipart(fields, file_field, file_path, filename,
                     content_type="audio/mpeg"):
    boundary = "----dcsub" + uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, key, value)).encode("utf-8"))
    with open(file_path, "rb") as handle:
        audio = handle.read()
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                  "Content-Type: %s\r\n\r\n"
                  % (boundary, file_field, filename, content_type)).encode("utf-8"))
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
                   timeout, cancel_event, filename="audio.mp3",
                   content_type="audio/mpeg"):
        url = (base_url or "").rstrip("/") + "/audio/transcriptions"
        fields = {
            "model": model,
            "response_format": "verbose_json",
            "temperature": "0",
        }
        if language and language != "auto":
            fields["language"] = language
        data, multipart_type = encode_multipart(
            fields, "file", audio_path, filename, content_type=content_type)
        body = self._post_with_retry(
            url, api_key, data, multipart_type,
            self._effective_timeout(timeout), cancel_event)
        return parse_asr_body(body.decode("utf-8", "replace"))

    def translate(self, base_url, api_key, model, texts, target_lang,
                  timeout, cancel_event, disable_thinking=False):
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
        if disable_thinking:
            # llama.cpp / vLLM / Strata 等支持的模板参数；关闭思考后本地
            # Qwen3 类推理模型翻译 20 条由约 48s 降到约 3s（v0030 实测）。
            payload["chat_template_kwargs"] = {"enable_thinking": False}
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
                   timeout=CONNECTION_TEST_TIMEOUT_SEC, disable_thinking=False):
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
        if disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._probe_request("POST", url, api_key, data,
                                   "application/json", timeout)


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------

class SubtitleJob(object):
    def __init__(self, job_id, path, language, translate, target_lang, user,
                 source=SOURCE_ASR, output_mode=None, skip_existing=True,
                 batch_id=None, group_id=None, audio_source=None,
                 source_job_id=None, audio_track=None,
                 audio_track_language=None, priority=0, source_watch=False):
        self.job_id = job_id
        self.path = path
        self.language = language
        self.translate = bool(translate)
        self.target_lang = target_lang
        self.user = user
        self.source = source if source in SOURCES else SOURCE_ASR
        self.output_mode = (output_mode
                            if output_mode in config.SUBTITLE_OUTPUT_MODES
                            else config.SUBTITLE_DEFAULT_OUTPUT_MODE)
        self.skip_existing = bool(skip_existing)
        self.batch_id = batch_id
        self.group_id = group_id
        # v0032：音频来源快照（提交时确定，运行期不随设置变化）
        self.audio_source = (audio_source
                             if audio_source in config.SUBTITLE_AUDIO_SOURCES
                             else config.SUBTITLE_DEFAULT_AUDIO_SOURCE)
        # v0048：音轨选择（None=自动；运行时解析成 ffmpeg -map）
        self.audio_track = audio_track
        self.audio_track_language = audio_track_language
        self.audio_map = None
        # v0048：队列优先级（越大越先执行）/ 自动重试次数 / 失败分类
        self.priority = int(priority or 0)
        self.attempt = 0
        self.failure_category = None
        self.retryable = False
        # v0048：目录监控自动加入的任务（只用于 UI 标识）
        self.source_watch = bool(source_watch)
        # v0035 补翻译：仅翻译任务记录源任务 ID（源任务目录 original.srt 为源文本）
        self.source_job_id = (source_job_id
                              if isinstance(source_job_id, str) and source_job_id
                              else None)
        self.status = STATUS_QUEUED
        self.stage = "audio"
        self.progress = 0
        self.message = u"排队等待"
        self.error = None
        self.files = []
        self.translation_partial = False
        # v0028 目录批量：写回视频同目录的结果 / 跳过 / 警告
        self.outputs = []
        self.output_errors = []
        self.skipped = False
        self.warning = None
        self.created_epoch = time.time()
        self.updated_epoch = self.created_epoch
        # v0038：执行/结束时间与步骤时间线（随任务 payload 持久化，无需 DB 迁移）
        self.started_epoch = None
        self.finished_epoch = None
        self.timeline_steps = []      # [{key,status,attempts,start_epoch,end_epoch,active_sec,detail}]
        self.asr_chunks = []          # [{index,status,start_epoch,end_epoch,extract_sec,asr_sec,fallback}]
        self.translate_batches = []   # [{index,status,start_epoch,end_epoch,duration_sec,cues,attempts,partial}]
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        # plan C：ASR 分片进度 / 是否可继续
        self.chunk_index = 0
        self.chunk_total = 0
        self.resumable = False
        self.lock = threading.RLock()

    # ---- v0038：步骤时间线 ----
    def _find_step(self, key):
        for step in self.timeline_steps:
            if step.get("key") == key:
                return step
        return None

    def step_start(self, key, detail=None):
        """开始/继续一个步骤：首次记录开始时间，恢复/重试时累加 attempts。"""
        now = time.time()
        with self.lock:
            if self.started_epoch is None:
                self.started_epoch = now
            step = self._find_step(key)
            if step is None:
                step = {
                    "key": key, "status": "running", "attempts": 1,
                    "start_epoch": now, "end_epoch": None,
                    "active_sec": 0.0, "_active_start": now,
                    "detail": dict(detail or {}),
                }
                self.timeline_steps.append(step)
            else:
                step["attempts"] = int(step.get("attempts") or 0) + 1
                step["status"] = "running"
                step["_active_start"] = now
                if step.get("start_epoch") is None:
                    step["start_epoch"] = now
                if detail:
                    step.setdefault("detail", {}).update(detail)
            return step

    def step_end(self, key, status="done", detail=None):
        now = time.time()
        with self.lock:
            step = self._find_step(key)
            if step is None:
                return None
            active = step.pop("_active_start", None)
            if active is not None:
                step["active_sec"] = round(
                    float(step.get("active_sec") or 0.0)
                    + max(0.0, now - float(active)), 3)
            step["end_epoch"] = now
            step["status"] = status
            if detail:
                step.setdefault("detail", {}).update(detail)
            return step

    def add_asr_chunk(self, entry):
        with self.lock:
            self.asr_chunks.append(dict(entry))

    def add_translate_batch(self, entry):
        with self.lock:
            self.translate_batches.append(dict(entry))

    @staticmethod
    def _iso(ts):
        return timeutil.iso_local(ts)

    def _step_view(self, step):
        now = time.time()
        start = step.get("start_epoch")
        active = float(step.get("active_sec") or 0.0)
        active_start = step.get("_active_start")
        if active_start is not None:
            active += max(0.0, now - float(active_start))
        return {
            "key": step.get("key"),
            "status": step.get("status") or "unknown",
            "attempts": int(step.get("attempts") or 1),
            "started_at": self._iso(start),
            "finished_at": self._iso(step.get("end_epoch")),
            "duration_sec": round(active, 3) if start is not None else None,
            "detail": dict(step.get("detail") or {}),
        }

    def _chunk_view(self, item):
        extract = float(item.get("extract_sec") or 0.0)
        asr = float(item.get("asr_sec") or 0.0)
        return {
            "index": int(item.get("index") or 0),
            "status": item.get("status") or "done",
            "started_at": self._iso(item.get("start_epoch")),
            "finished_at": self._iso(item.get("end_epoch")),
            "extract_sec": round(extract, 3),
            "asr_sec": round(asr, 3),
            "duration_sec": round(extract + asr, 3),
            "fallback": bool(item.get("fallback")),
        }

    def _translate_batch_view(self, item):
        return {
            "index": int(item.get("index") or 0),
            "status": item.get("status") or "done",
            "started_at": self._iso(item.get("start_epoch")),
            "finished_at": self._iso(item.get("end_epoch")),
            "duration_sec": round(float(item.get("duration_sec") or 0.0), 3),
            "cues": int(item.get("cues") or 0),
            "attempts": int(item.get("attempts") or 1),
            "partial": bool(item.get("partial")),
        }

    # ---- v0041：预计剩余时间（只读内存，不做 IO；样本不足返回 None）----
    def eta_seconds(self):
        """估算任务剩余秒数（仅 running/translating 有值）。

        - ASR 阶段：已完成分片（抽音频 + 识别）平均耗时 × 剩余分片数；
        - 翻译阶段：已完成批次平均耗时 × 由进度带（70..90）推算的剩余批次数；
        - 其它阶段 / 样本不足 → None（前端不显示，避免假精度）。

        只基于内存里的分片/批次记录，不额外 stat 文件，列表轮询也不增加 IO。
        """
        with self.lock:
            status = self.status
            stage = self.stage
            progress = int(self.progress or 0)
            total = int(self.chunk_total or 0)
            done_chunks = max(0, int(self.chunk_index or 0))
            chunk_secs = []
            for item in self.asr_chunks:
                if item.get("status") != "done":
                    continue
                sec = (float(item.get("extract_sec") or 0.0)
                       + float(item.get("asr_sec") or 0.0))
                if sec > 0:
                    chunk_secs.append(sec)
            batch_secs = []
            for item in self.translate_batches:
                if item.get("status") not in ("done", "partial"):
                    continue
                sec = float(item.get("duration_sec") or 0.0)
                if sec > 0:
                    batch_secs.append(sec)
        if status not in (STATUS_RUNNING, STATUS_TRANSLATING):
            return None
        if stage in ("audio", "asr") and total > 1:
            remaining = total - min(done_chunks, total)
            if remaining > 0 and chunk_secs:
                avg = sum(chunk_secs) / float(len(chunk_secs))
                return int(round(avg * remaining))
            return None
        if stage == "translate" and batch_secs:
            # 翻译进度带 70..90（见 _translate_cues），据此反推批次数与剩余批次
            fraction = (progress - 70) / 20.0
            if fraction <= 0.0 or fraction >= 1.0:
                return None
            done = len(batch_secs)
            total_batches = done / fraction
            remaining = total_batches - done
            if remaining <= 0.0:
                return None
            avg = sum(batch_secs) / float(done)
            return int(round(avg * remaining))
        return None

    def snapshot(self, include_timeline=False):
        with self.lock:
            started = self.started_epoch
            finished = self.finished_epoch
            end = finished if finished is not None else time.time()
            data = {
                "job_id": self.job_id,
                "status": self.status,
                "stage": self.stage,
                "progress": int(self.progress),
                "message": self.message,
                "path": self.path,
                "language": self.language,
                "translate": self.translate,
                "target_lang": self.target_lang,
                "source": self.source,
                "output_mode": self.output_mode,
                "skip_existing": bool(self.skip_existing),
                "batch_id": self.batch_id,
                "group_id": self.group_id,
                "audio_source": self.audio_source,
                # v0048：音轨 / 优先级 / 自动重试 / 失败分类 / 监控来源
                "audio_track": self.audio_track,
                "audio_track_language": self.audio_track_language,
                "audio_map": self.audio_map,
                "priority": int(self.priority or 0),
                "attempt": int(self.attempt or 0),
                "failure_category": self.failure_category,
                "retryable": bool(self.retryable),
                "source_watch": bool(self.source_watch),
                # v0035：补翻译任务指向源任务；普通任务为 None
                "source_job_id": self.source_job_id,
                "created_at": timeutil.iso_local(self.created_epoch),
                "updated_at": timeutil.iso_local(self.updated_epoch),
                "error": dict(self.error) if isinstance(self.error, dict) else None,
                "files": list(self.files),
                "outputs": list(self.outputs),
                "output_errors": list(self.output_errors),
                "skipped": bool(self.skipped),
                "warning": self.warning,
                "translation_partial": bool(self.translation_partial),
                "paused": self.status == STATUS_PAUSED,
                "resumable": bool(self.resumable),
                "chunk_index": int(self.chunk_index),
                "chunk_total": int(self.chunk_total),
                # v0038：执行/结束时间与总耗时（列表也用，保持轻量）
                "started_at": timeutil.iso_local(started),
                "finished_at": timeutil.iso_local(finished),
                "duration_sec": (round(max(0.0, float(end) - float(started)), 3)
                                 if started is not None else None),
                # v0041：预计剩余秒数（仅 running/translating，样本不足为 None）
                "eta_seconds": self.eta_seconds(),
            }
            if include_timeline:
                data["timeline"] = {
                    "queued_at": timeutil.iso_local(self.created_epoch),
                    "started_at": data["started_at"],
                    "finished_at": data["finished_at"],
                    "steps": [self._step_view(step) for step in self.timeline_steps],
                    "asr_chunks": [self._chunk_view(item) for item in self.asr_chunks],
                    "translate_batches": [self._translate_batch_view(item)
                                          for item in self.translate_batches],
                }
            return data

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
            "source": self.source,
            "output_mode": self.output_mode,
            "skip_existing": bool(self.skip_existing),
            "batch_id": self.batch_id,
            "group_id": self.group_id,
            "audio_source": self.audio_source,
            "source_job_id": self.source_job_id,
            # v0048：音轨 / 优先级 / 自动重试 / 失败分类
            "audio_track": self.audio_track,
            "audio_track_language": self.audio_track_language,
            "priority": int(self.priority or 0),
            "attempt": int(self.attempt or 0),
            "failure_category": self.failure_category,
            "retryable": bool(self.retryable),
            "source_watch": bool(self.source_watch),
            "created_epoch": self.created_epoch,
            "updated_epoch": self.updated_epoch,
            "error": self.error,
            "files": list(self.files),
            "outputs": list(self.outputs),
            "output_errors": list(self.output_errors),
            "skipped": bool(self.skipped),
            "warning": self.warning,
            "translation_partial": bool(self.translation_partial),
            "chunk_index": int(self.chunk_index),
            "chunk_total": int(self.chunk_total),
            "resumable": bool(self.resumable),
            # v0038：步骤时间线 / 分片 / 翻译批次（JSON payload 持久化，无需迁移）
            "started_epoch": self.started_epoch,
            "finished_epoch": self.finished_epoch,
            "timeline_steps": [
                dict((key, value) for key, value in step.items()
                     if not str(key).startswith("_"))
                for step in self.timeline_steps],
            "asr_chunks": [dict(item) for item in self.asr_chunks],
            "translate_batches": [dict(item) for item in self.translate_batches],
        }

    @classmethod
    def from_record(cls, record):
        job = cls(record.get("job_id"), record.get("path") or "",
                  record.get("language") or "auto",
                  record.get("translate") is True,
                  record.get("target_lang") or "",
                  record.get("user") or "",
                  source=record.get("source") or SOURCE_ASR,
                  output_mode=record.get("output_mode"),
                  skip_existing=record.get("skip_existing") is not False,
                  batch_id=record.get("batch_id"),
                  group_id=record.get("group_id"),
                  audio_source=record.get("audio_source"),
                  source_job_id=record.get("source_job_id"),
                  audio_track=record.get("audio_track"),
                  audio_track_language=record.get("audio_track_language"),
                  priority=int(record.get("priority") or 0),
                  source_watch=bool(record.get("source_watch")))
        job.attempt = int(record.get("attempt") or 0)
        job.failure_category = record.get("failure_category")
        job.retryable = bool(record.get("retryable"))
        job.status = record.get("status") or STATUS_FAILED
        job.stage = record.get("stage") or "audio"
        job.progress = int(record.get("progress") or 0)
        job.message = record.get("message") or ""
        job.error = record.get("error") if isinstance(record.get("error"), dict) else None
        job.files = record.get("files") or []
        job.outputs = record.get("outputs") or []
        job.output_errors = record.get("output_errors") or []
        job.skipped = bool(record.get("skipped"))
        job.warning = record.get("warning")
        job.translation_partial = bool(record.get("translation_partial"))
        job.created_epoch = float(record.get("created_epoch") or time.time())
        job.updated_epoch = float(record.get("updated_epoch") or job.created_epoch)
        job.chunk_index = int(record.get("chunk_index") or 0)
        job.chunk_total = int(record.get("chunk_total") or 0)
        job.resumable = bool(record.get("resumable"))
        # v0038：恢复步骤时间线；服务重启时仍为 running 的步骤按 paused 处理
        try:
            job.started_epoch = (float(record.get("started_epoch"))
                                 if record.get("started_epoch") else None)
        except (TypeError, ValueError):
            job.started_epoch = None
        try:
            job.finished_epoch = (float(record.get("finished_epoch"))
                                  if record.get("finished_epoch") else None)
        except (TypeError, ValueError):
            job.finished_epoch = None
        job.timeline_steps = []
        for item in record.get("timeline_steps") or []:
            if not isinstance(item, dict):
                continue
            step = dict((key, value) for key, value in item.items()
                        if not str(key).startswith("_"))
            if step.get("status") == "running":
                step["status"] = "paused"
                if not step.get("end_epoch"):
                    step["end_epoch"] = job.updated_epoch
            job.timeline_steps.append(step)
        job.asr_chunks = [dict(item) for item in (record.get("asr_chunks") or [])
                          if isinstance(item, dict)]
        job.translate_batches = [dict(item)
                                 for item in (record.get("translate_batches") or [])
                                 if isinstance(item, dict)]
        return job


def make_job_id():
    return "sub-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def make_batch_id():
    return "bat-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def make_group_id():
    return "grp-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _clamp_concurrency(value):
    """把设置里的 subtitle_concurrency 规范到 1..MAX（非法值回退默认 1）。"""
    try:
        concurrency = int(value or config.SUBTITLE_DEFAULT_CONCURRENCY)
    except (TypeError, ValueError):
        concurrency = config.SUBTITLE_DEFAULT_CONCURRENCY
    return max(1, min(concurrency, config.SUBTITLE_CONCURRENCY_MAX))


class _AudioPrefetch(object):
    """v0040：分片音频预取（只预读 1 片，让 NAS 的 FFmpeg 与云端 ASR 重叠）。

    串行流程原本是「抽音频 → 上传 → 识别 → 下一片」，其中抽音频（FFmpeg）和
    识别（网络/GPU）互等。开启预取后，主线程开始识别第 N 片时，后台线程已经
    在抽第 N+1 片的音频，FFmpeg 与网络重叠，单任务吞吐提升。

    语义约束（与串行实现保持一致）：
    - 同时只保留 1 个未消费的预取结果，临时文件最多多占 1 片空间；
    - 取消（cancel_event）与关闭（close）都会让在跑的 FFmpeg 尽快退出，
      暂停只丢弃预取结果，不改变“分片边界才暂停”的既有语义；
    - 抽音频异常原样保存，在主线程 take() 时抛出，错误码与串行时完全相同；
    - 缓存里已完成的分片不预取（断点续传/重启恢复不重复抽音频）。
    """

    def __init__(self, service, job, settings, plan, cached, max_bytes,
                 tempdir, counter, mode="nas", server_max_bytes=None,
                 enabled=True):
        self.service = service
        self.job = job
        self.settings = settings
        self.plan = list(plan)
        self.cached = cached or {}
        self.max_bytes = max_bytes
        self.tempdir = tempdir
        self.counter = counter
        self.mode = mode
        self.server_max_bytes = server_max_bytes
        self.enabled = bool(enabled)
        self._cond = threading.Condition()
        self._results = {}       # index -> 抽取结果（成功/异常）
        self._pending = None     # 已产出但主线程还没消费的分片号
        self._closed = False
        self._thread = None
        if self.enabled:
            self._thread = threading.Thread(
                target=self._run, name="subtitle-audio-prefetch", daemon=True)
            self._thread.start()

    def _should_stop(self):
        """传给 FFmpeg 的停止条件：取消或预取关闭（暂停/任务收尾）都尽快退出。"""
        return self._closed or self.job.cancel_event.is_set()

    def _extract(self, index):
        start, length = self.plan[index]
        started = time.time()
        try:
            audio, fallback = self.service._extract_chunk_audio(
                self.job, self.settings, start, length, self.max_bytes,
                self.tempdir, self.counter, mode=self.mode,
                server_max_bytes=self.server_max_bytes,
                stop_check=self._should_stop)
        except BaseException as exc:  # noqa: BLE001 - 原样抛回主线程
            return {"error": exc, "started_epoch": started}
        return {
            "audio": audio,
            "fallback": bool(fallback),
            "extract_sec": round(time.time() - started, 3),
            "started_epoch": started,
        }

    @staticmethod
    def _unlink(result):
        for entry in result.get("audio") or []:
            try:
                os.unlink(entry["path"])
            except OSError:
                pass

    def _run(self):
        try:
            for index in range(len(self.plan)):
                with self._cond:
                    while self._pending is not None and not self._closed:
                        self._cond.wait(timeout=0.2)
                if self._closed:
                    break
                row = self.cached.get(index)
                if row is not None and row.get("status") == CHUNK_DONE:
                    continue
                result = self._extract(index)
                with self._cond:
                    if self._closed:
                        self._unlink(result)
                        break
                    self._results[index] = result
                    self._pending = index
                    self._cond.notify_all()
        finally:
            with self._cond:
                self._cond.notify_all()

    def take(self, index):
        """取第 index 片音频；返回 {audio, fallback, extract_sec, started_epoch}。"""
        if not self.enabled:
            result = self._extract(index)
            self._raise(result)
            return result
        with self._cond:
            while index not in self._results:
                if self._closed:
                    break
                if self.job.cancel_event.is_set():
                    raise SubtitleCancelled()
                self._cond.wait(timeout=0.2)
            result = self._results.pop(index, None)
            if result is not None or self._closed:
                self._pending = None
            self._cond.notify_all()
        if result is None:
            raise SubtitleCancelled()
        self._raise(result)
        return result

    @staticmethod
    def _raise(result):
        error = result.get("error")
        if error is not None:
            raise error

    def close(self):
        """停止预取线程并清理未消费的临时音频（重复调用安全）。"""
        with self._cond:
            self._closed = True
            leftovers = dict(self._results)
            self._results = {}
            self._pending = None
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        for result in leftovers.values():
            self._unlink(result)


class SubtitleService(object):
    """异步字幕任务服务：默认同一时间跑 1 个任务，其余排队。

    v0034：并发数由设置 subtitle_concurrency（1..4）控制；默认 1 保持历史
    串行行为。多个 worker 共享同一个任务队列，暂停/取消语义不变。
    v0037：设置保存后由 apply_settings() 立即调整 worker 数，无需重启套件；
    调小并发不打断正在执行的任务，多余 worker 跑完当前任务后自动退出。
    """

    def __init__(self, runtime_config, store, share_service, settings_provider,
                 client=None, ffmpeg=None, job_limit=None,
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
        current_settings = {}
        try:
            current_settings = settings_provider() or {}
        except Exception:
            current_settings = {}
        if not isinstance(current_settings, dict):
            current_settings = {}
        # 保留上限：显式参数 > 设置 subtitle_job_limit > 默认 200
        # （设置变更需重启服务生效，与其它设置保持一致）。
        if job_limit is None:
            job_limit = current_settings.get("subtitle_job_limit")
        try:
            self.job_limit = max(20, int(job_limit or MAX_JOBS))
        except (TypeError, ValueError):
            self.job_limit = MAX_JOBS
        # v0034/v0037：并发数（1..MAX）从设置读取并 clamp；之后设置保存时
        # 由 apply_settings() 立即调整（无需重启）。
        self.concurrency = _clamp_concurrency(
            current_settings.get("subtitle_concurrency"))
        self._root = os.path.join(runtime_config.data_dir, "subtitles")
        self._jobs_dir = os.path.join(self._root, "jobs")
        self._batches_dir = os.path.join(self._root, "batches")
        self._groups_dir = os.path.join(self._root, "groups")
        os.makedirs(self._jobs_dir, exist_ok=True)
        os.makedirs(self._batches_dir, exist_ok=True)
        os.makedirs(self._groups_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        # v0026：同一时间只允许一个“模型列表/连接测试”探测（占线返回 PROBE_BUSY）。
        self._probe_lock = threading.Lock()
        self._jobs = {}
        self._queue = deque()
        self._active_ids = set()
        # v0048：最近完成任务的实际耗时（用于 ETA 校准）
        self._duration_samples = deque(maxlen=config.SUBTITLE_ETA_SAMPLE_LIMIT)
        self._workers = []       # 当前生效的 worker 线程
        self._retiring = set()   # 调小并发后待退出的线程（跑完当前任务退出）
        self._stopping = False
        # v0032：单任务 LRU 的惰性开关（大批次组注册时避免 O(n²) 扫描）。
        self._trim_needed = False
        self._load_jobs()
        try:
            with self._lock:
                if len(self._jobs) > self.job_limit:
                    self._trim_needed = True
                self._trim_groups_locked()
        except Exception:
            pass
        # v0034：重启后只要还有排队任务就自动拉起 worker（升级/重启不丢队列）。
        if self._queue:
            self._ensure_worker()

    def apply_settings(self, settings=None):
        """v0037：立即应用 subtitle_concurrency（保存设置后生效，无需重启套件）。

        调大并发 → 立即补 worker 消化排队任务；调小 → 只把多余 worker 标记为
        待退出，正在执行的任务不中断，跑完当前任务后线程自行结束。
        """
        if settings is None:
            try:
                settings = self._settings() or {}
            except Exception:
                settings = {}
        if not isinstance(settings, dict):
            settings = {}
        concurrency = _clamp_concurrency(settings.get("subtitle_concurrency"))
        with self._lock:
            changed = concurrency != self.concurrency
            self.concurrency = concurrency
            # 没有 worker/排队任务时保持惰性：下一次 submit 会按新并发起线程。
            if changed and (self._workers or self._retiring or self._queue):
                self._sync_workers_locked()
        return concurrency

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
        """DB 优先加载；字幕 JSON 文件保留为任务目录事实源/兜底。

        v0042：DB 已确认存在的任务不再打开/解析对应 JSON。NAS 上 7500+ 任务时，
        逐个 json.load 要 20s 以上，而升级后 DSM 只等十几秒健康检查，会把升级
        判成“启动失败”（现场表现为每次升级后都要点一次【修复】）。
        """
        records = {}
        try:
            # v0032：批次组可达 20000 个任务，启动时需全部恢复（组快照依赖完整 job 集合）
            for record in self.store.load_subtitle_jobs(limit=20000):
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
            # 文件名即 job_id（_job_json），DB 已有该任务时无需读取文件内容。
            stem = name[:-5]
            if stem in records and JOB_ID_RE.match(stem):
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
        queued_jobs = []
        for job_id, record in records.items():
            job = SubtitleJob.from_record(record)
            # v0042：只有可能继续执行的任务才需要查 ASR 分片缓存；
            # 终态任务（done/failed/cancelled）的分片信息不再影响任何行为。
            chunks = []
            if job.status in (STATUS_QUEUED, STATUS_RUNNING,
                              STATUS_TRANSLATING, STATUS_PAUSED):
                try:
                    chunks = self.store.load_subtitle_chunks(job.job_id)
                except Exception:
                    chunks = []
            done_chunks = sum(1 for item in chunks
                              if item.get("status") == CHUNK_DONE)
            if chunks:
                job.chunk_total = max(int(job.chunk_total or 0), len(chunks))
                job.chunk_index = max(int(job.chunk_index or 0), done_chunks)
            if job.status == STATUS_QUEUED:
                # v0034 P0：排队任务重启后保留 queued 并重新入队（auto worker），
                # 不再像旧版那样判成 SERVICE_RESTART failed 而丢掉整个队列。
                # 不重复 persist：状态未变化，避免升级启动时产生大量写盘。
                job.resumable = bool(chunks)
                if job.job_id not in self._queue:
                    queued_jobs.append(job)
            elif job.status in (STATUS_RUNNING, STATUS_TRANSLATING):
                if chunks:
                    # plan C：有分片缓存/计划 → 暂停可继续，而不是失败
                    job.status = STATUS_PAUSED
                    job.resumable = True
                    job.error = None
                    job.message = u"服务重启导致任务暂停，可继续"
                elif job.source_job_id:
                    # v0035：补翻译任务没有 ASR 分片缓存，重启后仍可继续
                    # （源文本解析与翻译都是可重入的，不产生重复上传）
                    job.status = STATUS_PAUSED
                    job.resumable = True
                    job.error = None
                    job.message = u"服务重启导致翻译暂停，可继续"
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
        # 按优先级 + 创建时间恢复队列（v0048：目录优先级生效）
        for job in sorted(queued_jobs, key=lambda item: (
                -int(getattr(item, "priority", 0) or 0),
                item.created_epoch)):
            if job.job_id in self._queue:
                continue
            with self._lock:
                self._enqueue_locked(job)

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

    # ------------------------------------------------------------------
    # v0038：步骤时间线（排队/探测/识别/翻译/写盘 + 分片/批次明细）
    # ------------------------------------------------------------------
    def _timeline_begin(self, job):
        """任务被 worker 取到：结束排队段并记录开始执行时间（只做一次）。"""
        with job.lock:
            if job.started_epoch is not None:
                return
            now = time.time()
            job.started_epoch = now
            step = job._find_step("queue")
            if step is None:
                job.timeline_steps.insert(0, {
                    "key": "queue", "status": "done", "attempts": 1,
                    "start_epoch": job.created_epoch, "end_epoch": now,
                    "active_sec": round(max(0.0, now - job.created_epoch), 3),
                    "detail": {},
                })
            else:
                step["end_epoch"] = now
                step["status"] = "done"
                step["active_sec"] = round(
                    float(step.get("active_sec") or 0.0)
                    + max(0.0, now - float(step.get("start_epoch") or now)), 3)

    def _timeline_close(self, job, status, finished=True):
        """收尾：结束仍处于 running 的步骤；finished=True 时记录结束时间。"""
        now = time.time()
        with job.lock:
            if not job.timeline_steps and job.started_epoch is None:
                # 从未开始执行（排队中取消/暂停）：只补一条排队段
                job.timeline_steps.append({
                    "key": "queue", "status": status, "attempts": 1,
                    "start_epoch": job.created_epoch, "end_epoch": now,
                    "active_sec": round(max(0.0, now - job.created_epoch), 3),
                    "detail": {},
                })
            for step in job.timeline_steps:
                if step.get("status") == "running":
                    active = step.pop("_active_start", None)
                    if active is not None:
                        step["active_sec"] = round(
                            float(step.get("active_sec") or 0.0)
                            + max(0.0, now - float(active)), 3)
                    step["end_epoch"] = now
                    step["status"] = status
            if finished:
                job.finished_epoch = now

    def _timeline_skip(self, job):
        """跳过已有字幕的任务：只记录排队段，立即结束。"""
        self._timeline_begin(job)
        self._timeline_close(job, "done")

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
        snapshot = job.snapshot(include_timeline=True)
        snapshot["files"] = self._list_files(job.job_id)
        return snapshot

    def _status_set(self, status):
        """v0034：把 status 过滤参数规范成状态集合；None/空/all → None（不筛选）。

        active 只包含真正执行中的 running+translating（不含 queued），供字幕页
        “执行中任务”视图使用；其余必须是合法状态，多个状态可用逗号组合
        （如 failed,cancelled，供“失败”筛选包含已取消任务）。
        """
        if status is None:
            return None
        if not isinstance(status, str):
            raise invalid(u"status 参数非法", details={"key": "status"})
        value = status.strip().lower()
        if not value or value == "all":
            return None
        if "," in value:
            statuses = set()
            for part in value.split(","):
                part = part.strip()
                if not part:
                    continue
                if part == "active":
                    statuses.update(ACTIVE_LIST_STATUSES)
                elif part in LIST_STATUS_FILTERS and part != "all":
                    statuses.add(part)
                else:
                    raise invalid(u"status 参数非法", details={"key": "status"})
            if not statuses:
                raise invalid(u"status 参数非法", details={"key": "status"})
            return tuple(sorted(statuses))
        if value == "active":
            return ACTIVE_LIST_STATUSES
        if value in LIST_STATUS_FILTERS:
            return (value,)
        raise invalid(u"status 参数非法", details={"key": "status"})

    @staticmethod
    def _matches_filters(job, q=None, suffix=None, directory=None,
                         category=None):
        """v0049：任务检索过滤（路径关键字/后缀/目录前缀/失败分类）。"""
        if suffix:
            extension = os.path.splitext(job.path or "")[1].lower()
            wanted = str(suffix).strip().lower()
            if wanted and not wanted.startswith("."):
                wanted = "." + wanted
            if extension != wanted:
                return False
        if directory:
            prefix = fsutil.path_key(directory)
            if not fsutil.path_key(job.path or "").startswith(prefix):
                return False
        if category and job.status == STATUS_FAILED:
            if (job.failure_category or "unknown") != category:
                return False
        elif category:
            return False
        if q:
            needle = str(q).strip().lower()
            if not needle:
                return True
            haystack = " ".join([
                job.path or "", job.job_id or "", job.group_id or "",
                job.batch_id or "", job.message or "",
                (job.error or {}).get("message") or ""]).lower()
            if needle not in haystack:
                return False
        return True

    def list(self, limit=50, batch_id=None, status=None, q=None,
             suffix=None, directory=None, category=None):
        statuses = self._status_set(status)
        with self._lock:
            jobs = list(self._jobs.values())
        if batch_id is not None:
            jobs = [item for item in jobs if item.batch_id == batch_id]
        if statuses is not None:
            jobs = [item for item in jobs if item.status in statuses]
        jobs = [item for item in jobs
                if self._matches_filters(item, q=q, suffix=suffix,
                                         directory=directory,
                                         category=category)]
        jobs.sort(key=lambda item: item.created_epoch, reverse=True)
        return [item.snapshot() for item in jobs[:max(1, int(limit or 50))]]

    def list_page(self, limit=50, batch_id=None, group_id=None, offset=0,
                  status=None, q=None, suffix=None, directory=None,
                  category=None):
        """任务分页（v0032 批次组 / v0034 status / v0049 检索筛选）。"""
        statuses = self._status_set(status)
        with self._lock:
            jobs = list(self._jobs.values())
        if batch_id is not None:
            jobs = [item for item in jobs if item.batch_id == batch_id]
        if group_id is not None:
            jobs = [item for item in jobs if item.group_id == group_id]
        if statuses is not None:
            jobs = [item for item in jobs if item.status in statuses]
        jobs = [item for item in jobs
                if self._matches_filters(item, q=q, suffix=suffix,
                                         directory=directory,
                                         category=category)]
        jobs.sort(key=lambda item: item.created_epoch, reverse=True)
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = max(1, min(int(limit or 50), 500))
        except (TypeError, ValueError):
            limit = 50
        total = len(jobs)
        return {
            "jobs": [item.snapshot() for item in jobs[offset:offset + limit]],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    # ------------------------------------------------------------------
    # v0039：执行队列视图（任务中心「即将执行」/ 批次组总览共用）
    # ------------------------------------------------------------------
    @staticmethod
    def _queue_job_entry(job, position=None):
        data = job.snapshot()
        data["queue_position"] = position
        return data

    def queue_snapshot(self, active_limit=10, queued_limit=20,
                       paused_limit=10, recent_limit=10):
        """按真实执行顺序返回字幕任务视图。

        - active：running/translating，按开始执行时间升序
        - queued：按内部 FIFO 队列顺序（重试/继续会追加到队尾，不能用 created_at 排序）
        - paused：已暂停（需人工继续）
        - recent：done/failed/cancelled，按结束时间倒序
        """
        def clamp(value, default, maximum=200):
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = default
            return max(0, min(value, maximum))

        active_limit = clamp(active_limit, 10)
        queued_limit = clamp(queued_limit, 20)
        paused_limit = clamp(paused_limit, 10)
        recent_limit = clamp(recent_limit, 10)
        with self._lock:
            jobs = dict(self._jobs)
            queue_ids = list(self._queue)
        active = []
        paused = []
        recent = []
        for job in jobs.values():
            status = job.status
            if status in ACTIVE_LIST_STATUSES:
                active.append(job)
            elif status == STATUS_PAUSED:
                paused.append(job)
            elif status in FINISHED_STATUSES:
                recent.append(job)
        active.sort(key=lambda item: (
            float(item.started_epoch or item.created_epoch or 0),
            float(item.created_epoch or 0)))
        paused.sort(key=lambda item: float(
            item.updated_epoch or item.created_epoch or 0), reverse=True)
        recent.sort(key=lambda item: float(
            item.finished_epoch or item.updated_epoch or item.created_epoch or 0),
            reverse=True)
        queued = []
        for position, job_id in enumerate(queue_ids):
            if len(queued) >= queued_limit:
                break
            job = jobs.get(job_id)
            if job is None or job.status != STATUS_QUEUED:
                continue
            queued.append(self._queue_job_entry(job, position))
        return {
            "active": [self._queue_job_entry(item) for item in active[:active_limit]],
            "queued": queued,
            "paused": [self._queue_job_entry(item) for item in paused[:paused_limit]],
            "recent": [self._queue_job_entry(item) for item in recent[:recent_limit]],
            "active_total": len(active),
            "queued_total": len(queue_ids),
            "paused_total": len(paused),
            "recent_total": len(recent),
            # 单视频 / 非批次组任务（批次总览里的“单任务排队”入口）
            "single_active_count": sum(1 for item in active
                                       if not getattr(item, "group_id", None)),
            # v0048：负载阈值管控状态（队列因负载暂停时给出原因与当前负载）
            "load_guard": self._load_guard_state(),
        }

    def _load_guard_state(self):
        try:
            settings = self._settings() or {}
            threshold = float(settings.get("subtitle_max_load") or 0)
        except Exception:  # pragma: no cover
            threshold = 0.0
        try:
            load1 = round(float(os.getloadavg()[0]), 2)
        except (AttributeError, OSError):
            load1 = None
        blocked = bool(threshold > 0 and load1 is not None and load1 > threshold)
        return {
            "enabled": threshold > 0,
            "threshold": threshold,
            "load1": load1,
            "blocked": blocked,
            "reason": u"当前负载 %.2f 超过阈值 %.2f，字幕队列已暂停取任务"
                      % (load1, threshold) if blocked else "",
        }

    def counts(self):
        """任务状态计数（诊断用；不返回任务内容）。"""
        with self._lock:
            jobs = list(self._jobs.values())
        by_status = {}
        by_category = {}
        for job in jobs:
            status = job.status or "unknown"
            by_status[status] = by_status.get(status, 0) + 1
            if status == STATUS_FAILED:
                category = job.failure_category or "unknown"
                by_category[category] = by_category.get(category, 0) + 1
        active = sum(by_status.get(status, 0) for status in ACTIVE_STATUSES)
        return {"active": active, "total": len(jobs),
                "by_status": by_status,
                "failed_by_category": by_category,
                "load_guard": self._load_guard_state()}

    # ------------------------------------------------------------------
    # v0048：音轨选择 / 失败清单 / 批量重试 / 优先级
    # ------------------------------------------------------------------
    def _resolve_audio_map(self, job, probe):
        """把 job.audio_track / audio_track_language 解析为 ffmpeg -map。"""
        streams = probe.get("audio_streams") or []
        track = getattr(job, "audio_track", None)
        language = getattr(job, "audio_track_language", None)
        if track is None and not language:
            job.audio_map = None
            return None
        if not streams:
            raise SubtitleError("AUDIO_TRACK_NOT_FOUND", u"未找到可用音轨")
        ordinal = None
        if language:
            needle = str(language).lower().replace("_", "-")
            for index, stream in enumerate(streams):
                stream_lang = str((stream or {}).get("language") or "").lower()
                stream_lang = stream_lang.replace("_", "-")
                if stream_lang and (stream_lang == needle
                                    or stream_lang.startswith(needle + "-")):
                    ordinal = index
                    break
            if ordinal is None:
                raise SubtitleError(
                    "AUDIO_TRACK_NOT_FOUND",
                    u"未找到语言为 %s 的音轨（共 %d 条）"
                    % (language, len(streams)))
        else:
            if isinstance(track, bool) or not isinstance(track, int):
                raise SubtitleError("AUDIO_TRACK_NOT_FOUND", u"音轨序号非法")
            if track < 0 or track >= len(streams):
                raise SubtitleError(
                    "AUDIO_TRACK_NOT_FOUND",
                    u"音轨序号 %d 超出范围（共 %d 条）" % (track, len(streams)))
            ordinal = track
        job.audio_map = "0:a:%d" % int(ordinal)
        return job.audio_map

    def audio_tracks(self, path):
        """列出视频音轨（供“手动指定音轨”选择器使用；不读取音频内容）。"""
        if not isinstance(path, str) or not path.strip():
            raise invalid(u"path 不能为空")
        self.shares.validate_file(path)
        settings = self._settings() or {}
        if not self.ffmpeg.available():
            raise SubtitleError("FFMPEG_UNAVAILABLE", u"未找到 FFmpeg")
        timeout = float(settings.get("subtitle_asr_timeout_sec") or 300)
        probe = self.ffmpeg.probe_media(path, min(timeout, 60.0))
        streams = probe.get("audio_streams") or []
        tracks = []
        for index, stream in enumerate(streams):
            stream = stream or {}
            tracks.append({
                "index": index,
                "stream_index": stream.get("index"),
                "codec": stream.get("codec") or "",
                "language": stream.get("language") or "",
                "title": stream.get("title") or "",
                "channels": stream.get("channels"),
                "default": bool(stream.get("default")),
            })
        return {
            "path": path,
            "has_audio": bool(streams),
            "duration": probe.get("duration"),
            "tracks": tracks,
        }

    def failures(self, limit=200, group_id=None, category=None,
                 include_skipped=False):
        """失败清单（v0048）：按分类/批次组过滤，附尝试次数与可重试标记。"""
        try:
            limit = max(1, min(int(limit or 200),
                               config.SUBTITLE_FAILURE_LIST_LIMIT))
        except (TypeError, ValueError):
            limit = 200
        if category is not None and category not in config.SUBTITLE_FAILURE_CATEGORIES:
            raise invalid(u"失败分类非法")
        with self._lock:
            jobs = list(self._jobs.values())
        items = []
        for job in jobs:
            if job.status != STATUS_FAILED:
                continue
            if category is not None and (job.failure_category or "unknown") != category:
                continue
            if group_id is not None and job.group_id != group_id:
                continue
            if job.skipped and not include_skipped:
                continue
            items.append(job.snapshot())
        items.sort(key=lambda item: item.get("finished_at") or "",
                   reverse=True)
        categories = {}
        for job in jobs:
            if job.status != STATUS_FAILED:
                continue
            key = job.failure_category or "unknown"
            categories[key] = categories.get(key, 0) + 1
        return {
            "total": len(items),
            "items": items[:limit],
            "categories": categories,
            "category_labels": dict(config.SUBTITLE_FAILURE_CATEGORY_LABELS),
            "retryable_categories": list(config.SUBTITLE_RETRYABLE_CATEGORIES),
        }

    def failures_export_rows(self, limit=None):
        """导出失败清单（CSV 用）：只含可安全展示的字段。"""
        data = self.failures(limit=limit or config.SUBTITLE_FAILURE_LIST_LIMIT,
                             include_skipped=True)
        rows = []
        for item in data["items"]:
            error = item.get("error") or {}
            rows.append({
                "job_id": item.get("job_id"),
                "path": item.get("path"),
                "status": item.get("status"),
                "category": item.get("failure_category") or "unknown",
                "code": error.get("code") or "",
                "message": item.get("message") or "",
                "attempt": item.get("attempt") or 0,
                "retryable": bool(item.get("retryable")),
                "skipped": bool(item.get("skipped")),
                "created_at": item.get("created_at") or "",
                "finished_at": item.get("finished_at") or "",
                "group_id": item.get("group_id") or "",
                "batch_id": item.get("batch_id") or "",
            })
        return rows

    def retry_failures(self, user, job_ids=None, group_id=None, category=None,
                       force=False):
        """批量重试失败任务（v0048）。

        - 显式 job_ids：默认只重试可恢复分类；force=true 时连 no_speech/权限类也重试；
        - group_id/category：按过滤条件批量重试（自动跳过不可重试项）。
        """
        with self._lock:
            jobs = list(self._jobs.values())
        selected = []
        if isinstance(job_ids, list) and job_ids:
            wanted = set(str(item) for item in job_ids)
            for job in jobs:
                if job.job_id in wanted:
                    selected.append(job)
        else:
            if category is not None and category not in config.SUBTITLE_FAILURE_CATEGORIES:
                raise invalid(u"失败分类非法")
            for job in jobs:
                if job.status != STATUS_FAILED:
                    continue
                if group_id is not None and job.group_id != group_id:
                    continue
                if category is not None and (job.failure_category or "unknown") != category:
                    continue
                selected.append(job)
        requeued = 0
        skipped = []
        for job in selected:
            if job.status != STATUS_FAILED:
                skipped.append({"job_id": job.job_id, "reason": "NOT_FAILED"})
                continue
            if job.skipped and not force:
                skipped.append({"job_id": job.job_id, "reason": "SKIPPED_NO_SPEECH"})
                continue
            if not job.retryable and not force:
                skipped.append({"job_id": job.job_id,
                                "reason": "NOT_RETRYABLE",
                                "category": job.failure_category or "unknown"})
                continue
            with self._lock:
                job.cancel_event.clear()
                job.pause_event.clear()
                job.status = STATUS_QUEUED
                job.stage = "audio"
                job.progress = 0
                job.message = u"重新排队等待执行"
                job.error = None
                job.skipped = False
                job.warning = None
                job.resumable = True
                job.attempt = 0
                job.failure_category = None
                job.updated_epoch = time.time()
                self._enqueue_locked(job)
                self._cond.notify_all()
            self._persist(job)
            self.store.audit("subtitle_retry", user or "", {
                "job_id": job.job_id, "batch": "failures",
                "force": bool(force)})
            requeued += 1
        if requeued:
            self._ensure_worker()
        return {"requeued": requeued, "skipped": skipped,
                "selected": len(selected)}

    def set_priority(self, job_id, top=False, priority=None):
        """调整队列优先级：top=true 置顶（1000），或显式 0..1000。"""
        job = self._get_job(job_id)
        if top:
            value = config.SUBTITLE_PRIORITY_MANUAL_TOP
        else:
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise invalid(u"priority 必须是整数")
            value = max(0, min(priority, config.SUBTITLE_PRIORITY_MANUAL_TOP))
        with self._lock:
            job.priority = value
            job.updated_epoch = time.time()
            if job.status == STATUS_QUEUED and job.job_id in self._queue:
                try:
                    self._queue.remove(job.job_id)
                except ValueError:
                    pass
                self._enqueue_locked(job)
                self._cond.notify_all()
            self._persist(job)
        self.store.audit("subtitle_priority", job.user, {
            "job_id": job.job_id, "priority": value, "top": bool(top)})
        return job.snapshot()

    def bulk_action(self, user, job_ids, action):
        """v0049：批量取消/暂停/继续/重试/置顶（单任务失败不影响其它）。"""
        actions = ("cancel", "pause", "resume", "retry", "priority")
        if action not in actions:
            raise invalid(u"action 只能是 %s" % "/".join(actions))
        if not isinstance(job_ids, list) or not job_ids:
            raise invalid(u"job_ids 必须是非空数组")
        if len(job_ids) > 200:
            raise ApiError("BATCH_TOO_LARGE", u"一次最多操作 200 个任务")
        unique = []
        seen = set()
        for item in job_ids:
            if not isinstance(item, str) or not item:
                continue
            if item in seen:
                continue
            seen.add(item)
            unique.append(item)
        results = []
        succeeded = 0
        failed = 0
        for job_id in unique:
            entry = {"job_id": job_id, "ok": False, "message": ""}
            try:
                if action == "cancel":
                    self.cancel(job_id)
                elif action == "pause":
                    self.pause(job_id)
                elif action == "resume":
                    self.resume(job_id)
                elif action == "priority":
                    self.set_priority(job_id, top=True)
                else:  # retry
                    result = self.retry_failures(user, job_ids=[job_id],
                                                 force=True)
                    if not result.get("requeued"):
                        skipped = (result.get("skipped") or [{}])[0]
                        raise invalid(u"任务无法重试：%s"
                                      % (skipped.get("reason") or "未知原因"))
                entry["ok"] = True
                succeeded += 1
            except (ApiError, SubtitleError) as exc:
                entry["message"] = getattr(exc, "message", str(exc))
                failed += 1
            except Exception as exc:  # pragma: no cover - 防御性兜底
                entry["message"] = str(exc)
                failed += 1
            results.append(entry)
        self.store.audit("subtitle_bulk_action", user or "", {
            "action": action, "requested": len(unique),
            "succeeded": succeeded, "failed": failed})
        return {"action": action, "requested": len(unique),
                "succeeded": succeeded, "failed": failed,
                "results": results}

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
    # v0028 目录枚举 / 批量字幕（docs/SUBTITLE_BATCH_CONTRACT.md）
    # ------------------------------------------------------------------
    def _batch_json(self, batch_id):
        return os.path.join(self._batches_dir, batch_id + ".json")

    def _load_batch_record(self, batch_id):
        if not isinstance(batch_id, str) or not BATCH_ID_RE.match(batch_id):
            raise not_found(u"字幕批次不存在")
        record = None
        try:
            record = self.store.load_subtitle_batch(batch_id)
        except Exception:
            record = None
        if record is None:
            path = self._batch_json(batch_id)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    candidate = json.load(handle)
            except (OSError, ValueError, UnicodeError):
                candidate = None
            if isinstance(candidate, dict):
                record = candidate
                record.setdefault("batch_id", batch_id)
                try:
                    self.store.save_subtitle_batch(batch_id, record)
                except Exception:
                    pass
        if record is None:
            raise not_found(u"字幕批次不存在")
        return record

    def _save_batch_record(self, record):
        batch_id = record.get("batch_id")
        if not isinstance(batch_id, str) or not BATCH_ID_RE.match(batch_id):
            raise invalid(u"字幕批次 ID 非法")
        record["updated_epoch"] = time.time()
        temporary = os.path.join(self._batches_dir, "." + batch_id + ".tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False)
                handle.flush()
            os.replace(temporary, self._batch_json(batch_id))
        except OSError:
            pass
        try:
            self.store.save_subtitle_batch(batch_id, record)
        except Exception:
            pass

    def _batch_snapshot(self, record):
        job_ids = record.get("job_ids") or []
        counts = {}
        for status in (STATUS_QUEUED, STATUS_RUNNING, STATUS_TRANSLATING,
                       STATUS_PAUSED, STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED):
            counts[status] = 0
        jobs = []
        missing = 0
        with self._lock:
            for job_id in job_ids:
                job = self._jobs.get(job_id)
                if job is None:
                    missing += 1
                    continue
                jobs.append(job.snapshot())
                counts[job.status] = counts.get(job.status, 0) + 1
        total = len(job_ids)
        finished = (counts[STATUS_DONE] + counts[STATUS_FAILED]
                    + counts[STATUS_CANCELLED])
        if total and finished >= total:
            if counts[STATUS_FAILED]:
                status = STATUS_FAILED
            elif counts[STATUS_CANCELLED] and not counts[STATUS_DONE]:
                status = STATUS_CANCELLED
            else:
                status = STATUS_DONE
        elif counts[STATUS_RUNNING] or counts[STATUS_TRANSLATING]:
            status = STATUS_RUNNING
        elif counts[STATUS_PAUSED]:
            status = STATUS_PAUSED
        else:
            status = STATUS_QUEUED
        return {
            "batch_id": record.get("batch_id"),
            "group_id": record.get("group_id"),
            "status": status,
            "total": total,
            "counts": counts,
            "missing": missing,
            "created_at": timeutil.iso_local(
                float(record.get("created_epoch") or 0)),
            "updated_at": timeutil.iso_local(
                float(record.get("updated_epoch") or 0)),
            "options": dict(record.get("options") or {}),
            "rejected": list(record.get("rejected") or []),
            "duplicates": list(record.get("duplicates") or []),
            "jobs": jobs,
        }

    def get_batch(self, batch_id):
        return self._batch_snapshot(self._load_batch_record(batch_id))

    def _active_path_keys(self):
        with self._lock:
            jobs = list(self._jobs.values())
        keys = set()
        for job in jobs:
            if job.status in ACTIVE_STATUSES:
                keys.add(fsutil.path_key(job.path))
        return keys

    def enumerate_videos(self, path, recursive=True, max_items=None):
        """递归枚举目录下的视频文件；不读时长、不调 FFmpeg。"""
        if not isinstance(path, str) or not path.strip():
            raise invalid(u"path 不能为空")
        normalized = fsutil.check_path_safety(path)
        if not fsutil.is_dir_nofollow(normalized):
            raise ApiError("INVALID_DIRECTORY",
                           u"目录不存在或不是目录: %s" % normalized)
        self.shares.validate_root(normalized)
        if not isinstance(recursive, bool):
            raise invalid(u"recursive 必须是布尔值")
        limit = config.SUBTITLE_ENUMERATE_MAX_ITEMS
        if max_items is not None:
            if isinstance(max_items, bool) or not isinstance(max_items, int):
                raise invalid(u"max_items 必须是整数")
            if max_items < 1:
                raise ApiError("ENUMERATE_LIMIT", u"max_items 必须大于 0")
            if max_items > config.SUBTITLE_ENUMERATE_MAX_ITEMS:
                raise ApiError(
                    "ENUMERATE_LIMIT",
                    u"max_items 不能超过 %d" % config.SUBTITLE_ENUMERATE_MAX_ITEMS)
            limit = max_items
        settings = self._settings() or {}
        extensions = (settings.get("video_extensions")
                      or list(config.DEFAULT_VIDEO_EXTENSIONS))
        allowed = frozenset(str(item).lower() for item in extensions)
        skipped = {"system": 0, "hidden": 0, "symlink": 0,
                   "permission": 0, "other": 0, "extension": 0}
        items = []
        truncated = False
        stack = [normalized]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as iterator:
                    entries = list(iterator)
            except PermissionError:
                skipped["permission"] += 1
                continue
            except OSError:
                skipped["other"] += 1
                continue
            entries.sort(key=lambda item: item.name.lower())
            # 同目录的 .srt 名单（避免每个视频再扫一次目录）
            srt_names = []
            for entry in entries:
                try:
                    if (entry.name.lower().endswith(".srt")
                            and entry.is_file(follow_symlinks=False)):
                        srt_names.append(entry.name)
                except OSError:
                    continue
            for entry in entries:
                name = entry.name
                try:
                    is_symlink = entry.is_symlink()
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    skipped["other"] += 1
                    continue
                if is_symlink:
                    skipped["symlink"] += 1
                    continue
                if fsutil.is_system_name(name):
                    skipped["system"] += 1
                    continue
                if fsutil.is_hidden_name(name):
                    skipped["hidden"] += 1
                    continue
                if is_dir:
                    if recursive:
                        stack.append(entry.path)
                    continue
                extension = fsutil.extension_of(name).lower()
                if extension not in allowed:
                    skipped["extension"] += 1
                    continue
                if not fsutil.is_regular_file(entry.path):
                    skipped["other"] += 1
                    continue
                try:
                    stat_result = entry.stat(follow_symlinks=False)
                except OSError:
                    skipped["permission"] += 1
                    continue
                if len(items) >= limit:
                    truncated = True
                    stack = []
                    break
                stem = os.path.splitext(name)[0]
                prefix = (stem + ".").lower()
                related = sorted(
                    item_name for item_name in srt_names
                    if item_name.lower().startswith(prefix))
                items.append({
                    "path": entry.path,
                    "name": name,
                    "size": int(stat_result.st_size),
                    "modified_at": timeutil.iso_local(stat_result.st_mtime),
                    "existing_subtitles": related[:10],
                    "has_subtitle": bool(related),
                    "has_source_srt": any(
                        item_name.lower() == prefix + "srt"
                        for item_name in related),
                })
        items.sort(key=lambda item: item["path"])
        return {
            "ok": True,
            "path": normalized,
            "recursive": bool(recursive),
            "video_extensions": sorted(allowed),
            "count": len(items),
            "total_bytes": sum(item["size"] for item in items),
            "truncated": truncated,
            "skipped": skipped,
            "items": items,
        }

    def _prepare_job(self, user, payload, settings, batch_id=None):
        """校验单条路径并构造任务对象（不注册/不入队）。"""
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            raise invalid(u"path 不能为空")
        source = payload.get("source") or SOURCE_ASR
        if not isinstance(source, str) or source not in SOURCES:
            raise invalid(u"source 只能是 asr 或 existing_srt")
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
        if source == SOURCE_EXISTING_SRT:
            if not translate:
                raise invalid(u"只翻译已有字幕时 translate 必须为 true")
            if not settings.get("subtitle_translate_enabled"):
                raise SubtitleError("TRANSLATE_DISABLED", u"字幕翻译功能未启用")
            if find_source_srt(path) is None:
                raise SubtitleError(
                    "SOURCE_SRT_MISSING",
                    u"视频同目录未找到 %s.srt，无法只翻译"
                    % os.path.splitext(os.path.basename(path))[0])
        elif translate and not settings.get("subtitle_translate_enabled"):
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

        output_mode = (payload.get("output_mode")
                       or settings.get("subtitle_output_mode")
                       or config.SUBTITLE_DEFAULT_OUTPUT_MODE)
        if output_mode not in config.SUBTITLE_OUTPUT_MODES:
            raise invalid(u"output_mode 只能是 alongside_video/download_only")
        skip_existing = payload.get("skip_existing")
        if skip_existing is None:
            skip_existing = bool(settings.get("subtitle_skip_existing", True))
        elif not isinstance(skip_existing, bool):
            raise invalid(u"skip_existing 必须是布尔值")

        job_id = make_job_id()
        # v0048：音轨选择（payload 覆盖设置默认）+ 目录优先级
        try:
            audio_track = normalize_audio_track(
                payload.get("audio_track", settings.get("subtitle_audio_track")))
        except ValueError:
            raise invalid(u"音轨序号非法（0–31 或 auto）")
        try:
            audio_language = normalize_audio_language(
                payload.get("audio_track_language",
                            settings.get("subtitle_audio_track_language")))
        except ValueError:
            raise invalid(u"音轨语言标签非法（如 jpn/chi/eng）")
        priority = payload.get("priority")
        if priority is None:
            priority = self._dir_priority(os.path.normpath(path))
        else:
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise invalid(u"priority 必须是整数")
            priority = max(0, min(priority, config.SUBTITLE_PRIORITY_MANUAL_TOP))
        return SubtitleJob(job_id, os.path.normpath(path), language, translate,
                           target_lang, user, source=source,
                           output_mode=output_mode,
                           skip_existing=skip_existing, batch_id=batch_id,
                           audio_source=settings.get("subtitle_audio_source"),
                           audio_track=audio_track,
                           audio_track_language=audio_language,
                           priority=priority,
                           source_watch=payload.get("source_watch") is True)

    def _dir_priority(self, path):
        """目录优先级：settings.subtitle_dir_priorities 中越靠前优先级越高。"""
        if not path:
            return 0
        try:
            settings = self._settings() or {}
        except Exception:  # pragma: no cover
            return 0
        directories = [item for item in
                       (settings.get("subtitle_dir_priorities") or [])
                       if isinstance(item, str) and item.strip()]
        if not directories:
            return 0
        target = fsutil.path_key(path)
        for index, directory in enumerate(directories):
            try:
                key = fsutil.path_key(directory.rstrip("/\\"))
            except (ApiError, OSError):
                continue
            if target == key or target.startswith(key + os.sep):
                return len(directories) - index
        return 0

    def _enqueue_locked(self, job):
        """按“优先级降序 + 提交时间升序”插入队列（调用方需持有 self._lock）。"""
        job_id = job.job_id
        if job_id in self._queue:
            return
        priority = -int(getattr(job, "priority", 0) or 0)
        created = float(getattr(job, "created_epoch", 0) or 0)
        position = len(self._queue)
        for index, existing_id in enumerate(self._queue):
            existing = self._jobs.get(existing_id)
            if existing is None:
                continue
            if priority < -int(getattr(existing, "priority", 0) or 0):
                position = index
                break
            if (priority == -int(getattr(existing, "priority", 0) or 0)
                    and created < float(getattr(existing, "created_epoch", 0) or 0)):
                position = index
                break
        self._queue.insert(position, job_id)

    def _enqueue(self, job):
        with self._lock:
            self._enqueue_locked(job)
            self._cond.notify_all()

    def _skip_message(self, job):
        """已有目标 srt 且 skip_existing 时返回提示文案；否则 None。"""
        if not job.skip_existing:
            return None
        directory, stem = split_video_stem(job.path)
        names = subtitle_output_names(stem, job.target_lang)
        if job.source == SOURCE_ASR:
            if find_existing_in_dir(directory, names["original"]) is None:
                return None
            if not job.translate:
                return u"已存在字幕，已跳过"
            if (find_existing_in_dir(directory, names["translation"]) is not None
                    or find_existing_in_dir(directory, names["bilingual"]) is not None):
                return u"已存在字幕与译文，已跳过"
            return None
        if (find_existing_in_dir(directory, names["translation"]) is not None
                or find_existing_in_dir(directory, names["bilingual"]) is not None):
            return u"已存在译文，已跳过"
        return None

    def _register_job(self, job, enqueue=True):
        # v0038：跳过任务（submit/batch/group 直接置 done）也记录排队与结束时间
        if job.status in FINISHED_STATUSES and job.started_epoch is None:
            self._timeline_skip(job)
        if enqueue:
            with self._lock:
                self._jobs[job.job_id] = job
                self._enqueue_locked(job)
                self._trim_jobs_locked()
        else:
            with self._lock:
                self._jobs[job.job_id] = job
                self._trim_jobs_locked()
        self._persist(job)

    def create_batch(self, user, payload):
        """一次提交一组视频；部分无效不整批失败（docs/SUBTITLE_BATCH_CONTRACT.md §4.1）。"""
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        settings = self._settings() or {}
        if not settings.get("subtitle_enabled"):
            raise SubtitleError("SUBTITLE_DISABLED", u"云端字幕功能未启用")
        if payload.get("confirm_cloud") is not True:
            raise SubtitleError("CLOUD_CONFIRM_REQUIRED",
                                u"请先确认音频/字幕文本将上传到所配置的第三方服务")
        paths = payload.get("paths")
        if not isinstance(paths, list) or not paths:
            raise invalid(u"paths 必须是非空数组")
        if len(paths) > config.SUBTITLE_BATCH_MAX_ITEMS:
            raise ApiError(
                "BATCH_TOO_LARGE",
                u"一次最多提交 %d 个视频，请分批提交"
                % config.SUBTITLE_BATCH_MAX_ITEMS)
        unique = []
        seen = set()
        for item in paths:
            if not isinstance(item, str) or not item.strip():
                raise invalid(u"paths 元素必须是路径字符串")
            key = fsutil.path_key(item)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)

        base = {}
        for key in ("language", "translate", "target_lang", "source",
                    "output_mode", "skip_existing", "audio_track",
                    "audio_track_language", "source_watch"):
            if key in payload:
                base[key] = payload[key]
        active_keys = self._active_path_keys()
        duplicates = []
        rejected = []
        prepared = []
        for path in unique:
            key = fsutil.path_key(path)
            if key in active_keys:
                duplicates.append(path)
                continue
            item_payload = dict(base)
            item_payload["path"] = path
            item_payload["confirm_cloud"] = True
            try:
                prepared.append(self._prepare_job(user, item_payload, settings))
            except (ApiError, SubtitleError) as exc:
                rejected.append({
                    "path": path,
                    "code": getattr(exc, "code", "INVALID_ARGUMENT"),
                    "message": getattr(exc, "message", u"提交失败"),
                })
        if not prepared:
            raise ApiError("BATCH_EMPTY", u"没有可提交的字幕任务",
                           details={"rejected": rejected,
                                    "duplicates": duplicates})

        batch_id = make_batch_id()
        for job in prepared:
            job.batch_id = batch_id
        record = {
            "batch_id": batch_id,
            "created_epoch": time.time(),
            "updated_epoch": time.time(),
            "user": user,
            "options": {
                "source": prepared[0].source,
                "language": prepared[0].language,
                "translate": bool(prepared[0].translate),
                "target_lang": prepared[0].target_lang,
                "output_mode": prepared[0].output_mode,
                "skip_existing": bool(prepared[0].skip_existing),
                "recursive": payload.get("recursive") is True,
                "root": payload.get("root") if isinstance(
                    payload.get("root"), str) else "",
            },
            "job_ids": [job.job_id for job in prepared],
            "rejected": rejected,
            "duplicates": duplicates,
        }
        self._save_batch_record(record)
        for job in prepared:
            skip_message = self._skip_message(job)
            if skip_message:
                job.status = STATUS_DONE
                job.stage = "write"
                job.progress = 100
                job.message = skip_message
                job.skipped = True
                self._register_job(job, enqueue=False)
                self.store.audit("subtitle_skip", user, {
                    "job_id": job.job_id,
                    "batch_id": batch_id,
                    "path": job.path,
                    "reason": skip_message,
                })
                continue
            self._register_job(job, enqueue=True)
            self.store.audit("subtitle_start", user, {
                "job_id": job.job_id,
                "batch_id": batch_id,
                "path": job.path,
                "source": job.source,
                "language": job.language,
                "translate": bool(job.translate),
                "target_lang": job.target_lang,
                "output_mode": job.output_mode,
            })
        if any(job.status == STATUS_QUEUED for job in prepared):
            self._ensure_worker()
        self._save_batch_record(record)
        return self._batch_snapshot(record)

    def _batch_jobs(self, batch_id):
        record = self._load_batch_record(batch_id)
        job_ids = record.get("job_ids") or []
        with self._lock:
            return record, [self._jobs[job_id] for job_id in job_ids
                            if job_id in self._jobs]

    def pause_batch(self, batch_id):
        record, jobs = self._batch_jobs(batch_id)
        for job in jobs:
            if job.status in FINISHED_STATUSES or job.status == STATUS_PAUSED:
                continue
            try:
                self.pause(job.job_id)
            except (ApiError, SubtitleError):
                continue
        self._save_batch_record(record)
        return self._batch_snapshot(record)

    def resume_batch(self, batch_id):
        record, jobs = self._batch_jobs(batch_id)
        for job in jobs:
            if job.status != STATUS_PAUSED:
                continue
            try:
                self.resume(job.job_id)
            except (ApiError, SubtitleError):
                continue
        self._save_batch_record(record)
        return self._batch_snapshot(record)

    def cancel_batch(self, batch_id):
        record, jobs = self._batch_jobs(batch_id)
        for job in jobs:
            if job.status in FINISHED_STATUSES:
                continue
            try:
                self.cancel(job.job_id)
            except (ApiError, SubtitleError):
                continue
        self._save_batch_record(record)
        return self._batch_snapshot(record)

    def retry_batch(self, batch_id):
        record, jobs = self._batch_jobs(batch_id)
        requeued = 0
        for job in jobs:
            if job.status not in (STATUS_FAILED, STATUS_CANCELLED):
                continue
            with self._lock:
                job.cancel_event.clear()
                job.pause_event.clear()
                job.status = STATUS_QUEUED
                job.stage = "audio"
                job.progress = 0
                job.message = u"重新排队等待执行"
                job.error = None
                job.skipped = False
                job.warning = None
                job.resumable = True
                job.finished_epoch = None
                job.updated_epoch = time.time()
                if job.job_id not in self._queue:
                    self._enqueue_locked(job)
                self._cond.notify_all()
            self._persist(job)
            requeued += 1
        if requeued:
            self.store.audit("subtitle_retry", "", {
                "batch_id": batch_id, "count": requeued})
            self._ensure_worker()
        self._save_batch_record(record)
        return self._batch_snapshot(record)

    # ------------------------------------------------------------------
    # v0032 批次组：一次提交自动拆成多个 <=SUBTITLE_BATCH_MAX_ITEMS 的批次
    # （docs/SUBTITLE_BATCH_CONTRACT.md §13）
    # ------------------------------------------------------------------
    def _group_json(self, group_id):
        return os.path.join(self._groups_dir, group_id + ".json")

    def _load_group_record(self, group_id):
        if not isinstance(group_id, str) or not BATCH_GROUP_ID_RE.match(group_id):
            raise not_found(u"字幕批次组不存在")
        record = None
        try:
            record = self.store.load_subtitle_batch_group(group_id)
        except Exception:
            record = None
        if record is None:
            path = self._group_json(group_id)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    candidate = json.load(handle)
            except (OSError, ValueError, UnicodeError):
                candidate = None
            if isinstance(candidate, dict):
                record = candidate
                record.setdefault("group_id", group_id)
                try:
                    self.store.save_subtitle_batch_group(group_id, record)
                except Exception:
                    pass
        if record is None:
            raise not_found(u"字幕批次组不存在")
        return record

    def _save_group_record(self, record):
        group_id = record.get("group_id")
        if not isinstance(group_id, str) or not BATCH_GROUP_ID_RE.match(group_id):
            raise invalid(u"字幕批次组 ID 非法")
        record["updated_epoch"] = time.time()
        temporary = os.path.join(self._groups_dir, "." + group_id + ".tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False)
                handle.flush()
            os.replace(temporary, self._group_json(group_id))
        except OSError:
            pass
        try:
            self.store.save_subtitle_batch_group(group_id, record)
        except Exception:
            pass

    @staticmethod
    def _status_counts():
        return dict((status, 0) for status in (
            STATUS_QUEUED, STATUS_RUNNING, STATUS_TRANSLATING,
            STATUS_PAUSED, STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED))

    @staticmethod
    def _derive_status(counts, total):
        """按批次/组的任务计数推导聚合状态（与 v0028 批次语义一致）。"""
        finished = (counts.get(STATUS_DONE, 0) + counts.get(STATUS_FAILED, 0)
                    + counts.get(STATUS_CANCELLED, 0))
        if total and finished >= total:
            if counts.get(STATUS_FAILED):
                return STATUS_FAILED
            if counts.get(STATUS_CANCELLED) and not counts.get(STATUS_DONE):
                return STATUS_CANCELLED
            return STATUS_DONE
        if counts.get(STATUS_RUNNING) or counts.get(STATUS_TRANSLATING):
            return STATUS_RUNNING
        if counts.get(STATUS_PAUSED):
            return STATUS_PAUSED
        return STATUS_QUEUED

    @staticmethod
    def _active_job_entry(job):
        """v0034：批次组“执行中任务”快照条目（只暴露安全字段）。"""
        with job.lock:
            return {
                "job_id": job.job_id,
                "name": os.path.basename(job.path or ""),
                "path": job.path,
                "status": job.status,
                "stage": job.stage,
                "progress": int(job.progress or 0),
                "message": job.message,
                "chunk_index": int(job.chunk_index or 0),
                "chunk_total": int(job.chunk_total or 0),
                "updated_at": timeutil.iso_local(
                    float(job.updated_epoch or job.created_epoch or 0)),
            }

    def _eta_seconds(self, jobs, total):
        """任务剩余时间估算（v0034 墙钟吞吐 + v0048 校准）。

        v0034：per_job=(now-earliest)/processed，eta=per_job*remaining/concurrency。
        v0048：当有 ≥3 个完成任务的“实际执行耗时”样本时，改用样本中位耗时估计
        排队任务，并对执行中任务按 `elapsed*(100-progress)/progress` 修正，
        避免长任务/慢任务把整体 ETA 拉偏。
        """
        try:
            processed = 0
            finished = 0
            earliest = None
            active = []
            for job in jobs:
                with job.lock:
                    created = float(job.created_epoch or 0)
                    status = job.status
                    progress = int(job.progress or 0)
                    started = job.started_epoch
                if created <= 0:
                    return None
                if earliest is None or created < earliest:
                    earliest = created
                if status in FINISHED_STATUSES:
                    finished += 1
                    if getattr(job, "skipped", False):
                        continue
                    processed += 1
                elif status in (STATUS_RUNNING, STATUS_TRANSLATING):
                    active.append((started, progress))
            remaining = int(total) - finished
            concurrency = max(1, int(self.concurrency or 1))
            baseline = _median(list(self._duration_samples))
            if baseline is not None and processed >= 1 and len(
                    self._duration_samples) >= 3:
                if remaining <= 0:
                    return 0
                now = time.time()
                active_remaining = 0.0
                for started, progress in active:
                    if started and progress > 0:
                        elapsed = max(0.0, now - float(started))
                        active_remaining += max(
                            0.0, elapsed * (100.0 - progress) / progress)
                    else:
                        active_remaining += baseline
                queued_remaining = max(0, remaining - len(active))
                eta = active_remaining + (
                    baseline * queued_remaining / concurrency)
                return int(round(max(0.0, eta)))
            if earliest is None or processed < 3:
                return None
            elapsed = time.time() - earliest
            if elapsed <= 0:
                return None
            if remaining <= 0:
                return 0
            if elapsed <= 0:
                return None
            eta = int(round(elapsed / processed * remaining / concurrency))
            return eta if eta >= 0 else None
        except (TypeError, ValueError, OverflowError):
            return None

    def _group_snapshot(self, record):
        batch_ids = list(record.get("batch_ids") or [])
        counts = self._status_counts()
        batches = []
        group_jobs = []
        total = 0
        missing = 0
        # v0035：组内“可补翻译”任务计数（只读内存快照，不做逐任务文件 stat）
        translatable = {"count": 0, "partial": 0}
        with self._lock:
            jobs_by_id = dict(self._jobs)
        for batch_id in batch_ids:
            try:
                batch_record = self._load_batch_record(batch_id)
            except (ApiError, SubtitleError):
                continue
            job_ids = list(batch_record.get("job_ids") or [])
            batch_counts = self._status_counts()
            for job_id in job_ids:
                job = jobs_by_id.get(job_id)
                if job is None:
                    missing += 1
                    continue
                group_jobs.append(job)
                status = job.status
                batch_counts[status] = batch_counts.get(status, 0) + 1
                counts[status] = counts.get(status, 0) + 1
                if status == STATUS_DONE:
                    if not job.translate:
                        translatable["count"] += 1
                    elif job.translation_partial:
                        translatable["partial"] += 1
            batch_total = len(job_ids)
            total += batch_total
            batches.append({
                "batch_id": batch_id,
                "index": len(batches),
                "total": batch_total,
                "status": self._derive_status(batch_counts, batch_total),
                "counts": batch_counts,
            })
        finished = sum(counts.get(status, 0) for status in FINISHED_STATUSES)
        status = self._derive_status(counts, total)
        if batch_ids and not batches:
            status = STATUS_FAILED
        active_jobs = [item for item in group_jobs
                       if item.status in ACTIVE_LIST_STATUSES]
        active_jobs.sort(key=lambda item: item.created_epoch)
        return {
            "group_id": record.get("group_id"),
            "status": status,
            "total": total,
            "batch_count": len(batches),
            "finished": finished,
            "progress": int(round(100.0 * finished / total)) if total else 0,
            "counts": counts,
            "missing": missing,
            # v0035：可补翻译统计（count=仅 ASR 的 done；partial=部分译文可重译）
            "translatable": translatable,
            # v0034：执行中任务明细（最多 10 条）+ 剩余时间估算 + 当前并发数。
            "active_jobs": [self._active_job_entry(item)
                            for item in active_jobs[:10]],
            "eta_seconds": self._eta_seconds(group_jobs, total),
            "concurrency": int(self.concurrency or 1),
            "created_at": timeutil.iso_local(
                float(record.get("created_epoch") or 0)),
            "updated_at": timeutil.iso_local(
                float(record.get("updated_epoch") or 0)),
            "options": dict(record.get("options") or {}),
            "rejected": list(record.get("rejected") or []),
            "duplicates": list(record.get("duplicates") or []),
            "batches": batches,
        }

    def get_batch_group(self, group_id):
        return self._group_snapshot(self._load_group_record(group_id))

    def batch_group_overview(self, active_limit=10, completed_limit=3):
        """v0039：批次组总览（进行中优先按“下一任务队列位置”排序 + 最近完成）。

        只基于内存任务按 group_id 聚合（近期组任务都在内存），不逐批读文件；
        队列位置使用内部 _queue 顺序，重试/继续追加到队尾也能正确反映先后。
        """
        def clamp(value, default, maximum=50):
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = default
            return max(0, min(value, maximum))

        active_limit = clamp(active_limit, 10)
        completed_limit = clamp(completed_limit, 3)
        with self._lock:
            jobs = list(self._jobs.values())
            queue_ids = list(self._queue)
        positions = dict((job_id, index)
                         for index, job_id in enumerate(queue_ids))
        groups = {}
        for job in jobs:
            group_id = getattr(job, "group_id", None)
            if not group_id:
                continue
            entry = groups.get(group_id)
            if entry is None:
                entry = {"counts": self._status_counts(), "total": 0,
                         "next": None, "updated": 0.0}
                groups[group_id] = entry
            entry["counts"][job.status] = entry["counts"].get(job.status, 0) + 1
            entry["total"] += 1
            entry["updated"] = max(
                entry["updated"],
                float(job.updated_epoch or job.created_epoch or 0))
            position = positions.get(job.job_id)
            if position is not None and (entry["next"] is None
                                         or position < entry["next"]):
                entry["next"] = position
        active = []
        completed = []
        for group_id, entry in groups.items():
            try:
                record = self._load_group_record(group_id)
            except (ApiError, SubtitleError):
                record = {"group_id": group_id}
            counts = entry["counts"]
            total = entry["total"]
            status = self._derive_status(counts, total)
            finished = sum(counts.get(item, 0) for item in FINISHED_STATUSES)
            options = dict(record.get("options") or {})
            summary = {
                "group_id": group_id,
                "status": status,
                "total": total,
                "finished": finished,
                "progress": int(round(100.0 * finished / total)) if total else 0,
                "counts": counts,
                "options": {
                    "translate": options.get("translate") is True,
                    "source": options.get("source") or SOURCE_ASR,
                },
                "batch_count": len(record.get("batch_ids") or []),
                "next_queue_position": entry["next"],
                "created_at": timeutil.iso_local(
                    float(record.get("created_epoch") or 0)),
                "updated_at": timeutil.iso_local(entry["updated"]),
            }
            if status in FINISHED_STATUSES:
                completed.append((entry["updated"], summary))
            else:
                active.append((entry["next"], entry["updated"], summary))
        active.sort(key=lambda item: (
            1 if item[0] is None else 0,
            item[0] if item[0] is not None else 0,
            -item[1]))
        completed.sort(key=lambda item: -item[0])
        return {
            "active": [item[2] for item in active[:active_limit]],
            "completed": [item[1] for item in completed[:completed_limit]],
            "active_total": len(active),
            "completed_total": len(completed),
            # 单视频任务 / 非批次组任务（排队或执行中都算）
            "single_active_count": sum(
                1 for item in jobs
                if not getattr(item, "group_id", None)
                and item.status in ACTIVE_STATUSES),
        }

    def create_batch_group(self, user, payload):
        """一次提交多批：<=20000 个路径，自动按 SUBTITLE_BATCH_MAX_ITEMS 拆批。"""
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        settings = self._settings() or {}
        if not settings.get("subtitle_enabled"):
            raise SubtitleError("SUBTITLE_DISABLED", u"云端字幕功能未启用")
        if payload.get("confirm_cloud") is not True:
            raise SubtitleError("CLOUD_CONFIRM_REQUIRED",
                                u"请先确认音频/字幕文本将上传到所配置的第三方服务")
        paths = payload.get("paths")
        if not isinstance(paths, list) or not paths:
            raise invalid(u"paths 必须是非空数组")
        group_max = config.SUBTITLE_BATCH_GROUP_MAX_ITEMS
        if len(paths) > group_max:
            raise ApiError(
                "BATCH_TOO_LARGE",
                u"一次最多提交 %d 个视频，请分批提交" % group_max)
        unique = []
        seen = set()
        for item in paths:
            if not isinstance(item, str) or not item.strip():
                raise invalid(u"paths 元素必须是路径字符串")
            key = fsutil.path_key(item)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)

        base = {}
        for key in ("language", "translate", "target_lang", "source",
                    "output_mode", "skip_existing", "audio_track",
                    "audio_track_language", "source_watch"):
            if key in payload:
                base[key] = payload[key]
        active_keys = self._active_path_keys()
        duplicates = []
        rejected = []
        prepared = []
        for path in unique:
            key = fsutil.path_key(path)
            if key in active_keys:
                duplicates.append(path)
                continue
            item_payload = dict(base)
            item_payload["path"] = path
            item_payload["confirm_cloud"] = True
            try:
                prepared.append(self._prepare_job(user, item_payload, settings))
            except (ApiError, SubtitleError) as exc:
                rejected.append({
                    "path": path,
                    "code": getattr(exc, "code", "INVALID_ARGUMENT"),
                    "message": getattr(exc, "message", u"提交失败"),
                })
        if not prepared:
            raise ApiError("BATCH_EMPTY", u"没有可提交的字幕任务",
                           details={"rejected": rejected,
                                    "duplicates": duplicates})

        group_id = make_group_id()
        batch_ids = []
        per_batch = max(1, int(config.SUBTITLE_BATCH_MAX_ITEMS))
        options = {
            "source": prepared[0].source,
            "language": prepared[0].language,
            "translate": bool(prepared[0].translate),
            "target_lang": prepared[0].target_lang,
            "output_mode": prepared[0].output_mode,
            "skip_existing": bool(prepared[0].skip_existing),
            "audio_source": prepared[0].audio_source,
            "recursive": payload.get("recursive") is True,
            "root": payload.get("root") if isinstance(
                payload.get("root"), str) else "",
        }
        now = time.time()
        for start in range(0, len(prepared), per_batch):
            chunk = prepared[start:start + per_batch]
            batch_id = make_batch_id()
            batch_ids.append(batch_id)
            for job in chunk:
                job.batch_id = batch_id
                job.group_id = group_id
            self._save_batch_record({
                "batch_id": batch_id,
                "group_id": group_id,
                "created_epoch": now,
                "updated_epoch": now,
                "user": user,
                "options": dict(options),
                "job_ids": [job.job_id for job in chunk],
                "rejected": [],
                "duplicates": [],
            })
        group_record = {
            "group_id": group_id,
            "created_epoch": now,
            "updated_epoch": now,
            "user": user,
            "options": options,
            "batch_ids": batch_ids,
            "rejected": rejected,
            "duplicates": duplicates,
        }
        self._save_group_record(group_record)
        for job in prepared:
            skip_message = self._skip_message(job)
            if skip_message:
                job.status = STATUS_DONE
                job.stage = "write"
                job.progress = 100
                job.message = skip_message
                job.skipped = True
                self._register_job(job, enqueue=False)
                self.store.audit("subtitle_skip", user, {
                    "job_id": job.job_id,
                    "batch_id": job.batch_id,
                    "group_id": group_id,
                    "path": job.path,
                    "reason": skip_message,
                })
                continue
            self._register_job(job, enqueue=True)
            self.store.audit("subtitle_start", user, {
                "job_id": job.job_id,
                "batch_id": job.batch_id,
                "group_id": group_id,
                "path": job.path,
                "source": job.source,
                "language": job.language,
                "translate": bool(job.translate),
                "target_lang": job.target_lang,
                "output_mode": job.output_mode,
                "audio_source": job.audio_source,
            })
        if any(job.status == STATUS_QUEUED for job in prepared):
            self._ensure_worker()
        self._save_group_record(group_record)
        with self._lock:
            self._trim_groups_locked()
        return self._group_snapshot(group_record)

    def _group_batch_ids(self, group_id):
        record = self._load_group_record(group_id)
        return record, list(record.get("batch_ids") or [])

    def pause_batch_group(self, group_id):
        record, batch_ids = self._group_batch_ids(group_id)
        for batch_id in batch_ids:
            try:
                self.pause_batch(batch_id)
            except (ApiError, SubtitleError):
                continue
        self._save_group_record(record)
        return self._group_snapshot(record)

    def resume_batch_group(self, group_id):
        record, batch_ids = self._group_batch_ids(group_id)
        for batch_id in batch_ids:
            try:
                self.resume_batch(batch_id)
            except (ApiError, SubtitleError):
                continue
        self._save_group_record(record)
        return self._group_snapshot(record)

    def cancel_batch_group(self, group_id):
        record, batch_ids = self._group_batch_ids(group_id)
        for batch_id in batch_ids:
            try:
                self.cancel_batch(batch_id)
            except (ApiError, SubtitleError):
                continue
        self._save_group_record(record)
        return self._group_snapshot(record)

    def retry_batch_group(self, group_id):
        record, batch_ids = self._group_batch_ids(group_id)
        for batch_id in batch_ids:
            try:
                self.retry_batch(batch_id)
            except (ApiError, SubtitleError):
                continue
        self._save_group_record(record)
        return self._group_snapshot(record)

    # ------------------------------------------------------------------
    # v0035 补翻译：已完成 ASR（或部分译文）任务派生“仅翻译”任务
    # 只调 LLM（源文本来自源任务目录 original.srt），不跑 FFmpeg、不重新 ASR，
    # 源任务的 status/文件/审计完全不动。
    # ------------------------------------------------------------------
    @staticmethod
    def _read_text_file(path):
        """读取 UTF-8（容忍 BOM/非法字节）；失败返回 None。"""
        try:
            with open(path, "r", encoding="utf-8-sig",
                      errors="replace") as handle:
                return handle.read()
        except OSError:
            return None

    def _resolve_source_text(self, source_job_id, video_path):
        """解析补翻译源文本：源任务目录 original.srt → 视频同目录 <stem>.srt。

        返回 (源字幕路径, 文本)；都不可用时抛 SOURCE_SRT_MISSING。
        只翻译任务不要求视频文件存在（不调用 shares.validate_file）。
        """
        if source_job_id:
            with self._lock:
                source_exists = source_job_id in self._jobs
            if source_exists:
                try:
                    source_dir = self._job_dir(source_job_id)
                except ApiError:
                    source_dir = None
                if source_dir:
                    path = os.path.join(source_dir, "original.srt")
                    if os.path.isfile(path):
                        text = self._read_text_file(path)
                        if text is not None:
                            return path, text
        source_srt = find_source_srt(video_path)
        if source_srt is not None:
            text = self._read_text_file(source_srt)
            if text is not None:
                return source_srt, text
        raise SubtitleError("SOURCE_SRT_MISSING", u"源字幕已不存在，请重新生成")

    def _active_translate_by_source(self):
        """构建 source_job_id → 排队/执行中翻译任务的索引。

        组批量最多 20000 个任务，逐任务全量扫描 _jobs 会退化为 O(n²)。
        """
        index = {}
        with self._lock:
            jobs = list(self._jobs.values())
        for item in jobs:
            source_id = getattr(item, "source_job_id", None)
            if (source_id and item.status in ACTIVE_STATUSES
                    and item.translate):
                index.setdefault(source_id, item)
        return index

    def _find_active_translate_job(self, source_job_id):
        """同源已有排队/执行中的翻译任务则返回它（幂等去重）。"""
        return self._active_translate_by_source().get(source_job_id)

    def _translate_precondition(self, payload):
        """补翻译公共前置校验：功能/翻译开关、LLM 密钥、云端上传确认。"""
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        settings = self._settings() or {}
        if not settings.get("subtitle_enabled"):
            raise SubtitleError("SUBTITLE_DISABLED", u"云端字幕功能未启用")
        if not settings.get("subtitle_translate_enabled"):
            raise SubtitleError("TRANSLATE_DISABLED", u"字幕翻译功能未启用")
        if not str(settings.get("subtitle_llm_api_key") or "").strip():
            raise SubtitleError(
                "TRANSLATE_NOT_CONFIGURED",
                u"未配置字幕翻译模型密钥，请先在设置中填写")
        if payload.get("confirm_cloud") is not True:
            raise SubtitleError(
                "CLOUD_CONFIRM_REQUIRED",
                u"请先确认字幕文本将上传到所配置的第三方服务")
        return settings

    @staticmethod
    def _translate_target_lang(payload, settings):
        target_lang = (payload.get("target_lang")
                       or settings.get("subtitle_translate_target") or "zh")
        if (not isinstance(target_lang, str)
                or not LANGUAGE_RE.match(target_lang.strip())
                or target_lang.strip().lower() == "auto"):
            raise invalid(u"目标语言非法")
        return target_lang.strip()

    @staticmethod
    def _check_translate_source(source_job):
        """源任务必须 done；已有完整译文时拒绝（部分译文允许重译）。"""
        if source_job.status != STATUS_DONE:
            raise SubtitleError("JOB_NOT_DONE", u"源任务尚未完成，无法补翻译")
        if source_job.translate and not source_job.translation_partial:
            raise SubtitleError("TRANSLATION_EXISTS", u"源任务已有完整译文")

    def _build_translate_job(self, user, source_job, target_lang):
        """按 v0035 契约构造“仅翻译”任务（skip_existing 必须为 False）。"""
        return SubtitleJob(
            make_job_id(), source_job.path, source_job.language, True,
            target_lang, user, source=SOURCE_EXISTING_SRT,
            output_mode=source_job.output_mode, skip_existing=False,
            audio_source=source_job.audio_source,
            source_job_id=source_job.job_id)

    @staticmethod
    def _translate_options(source_job, target_lang, extra=None):
        options = {
            "source": SOURCE_EXISTING_SRT,
            "language": source_job.language,
            "translate": True,
            "target_lang": target_lang,
            "output_mode": source_job.output_mode,
            "skip_existing": False,
            "audio_source": source_job.audio_source,
            "recursive": False,
            "root": "",
        }
        if extra:
            options.update(extra)
        return options

    def _create_translate_group(self, user, jobs, options):
        """把已构造好的仅翻译任务登记为一个新批次组（按上限自动拆批）。"""
        group_id = make_group_id()
        batch_ids = []
        per_batch = max(1, int(config.SUBTITLE_BATCH_MAX_ITEMS))
        now = time.time()
        for start in range(0, len(jobs), per_batch):
            chunk = jobs[start:start + per_batch]
            batch_id = make_batch_id()
            batch_ids.append(batch_id)
            for job in chunk:
                job.batch_id = batch_id
                job.group_id = group_id
            self._save_batch_record({
                "batch_id": batch_id,
                "group_id": group_id,
                "created_epoch": now,
                "updated_epoch": now,
                "user": user,
                "options": dict(options),
                "job_ids": [job.job_id for job in chunk],
                "rejected": [],
                "duplicates": [],
            })
        group_record = {
            "group_id": group_id,
            "created_epoch": now,
            "updated_epoch": now,
            "user": user,
            "options": dict(options),
            "batch_ids": batch_ids,
            "rejected": [],
            "duplicates": [],
        }
        self._save_group_record(group_record)
        for job in jobs:
            self._register_job(job, enqueue=True)
            self.store.audit("subtitle_start", user, {
                "job_id": job.job_id,
                "batch_id": job.batch_id,
                "group_id": group_id,
                "path": job.path,
                "source": job.source,
                "language": job.language,
                "translate": True,
                "target_lang": job.target_lang,
                "output_mode": job.output_mode,
                "audio_source": job.audio_source,
                # v0035：补翻译审计可追溯到源任务
                "operation": "translate",
                "source_job_id": job.source_job_id,
            })
        self._save_group_record(group_record)
        with self._lock:
            self._trim_groups_locked()
        return group_record

    def translate_job(self, user, job_id, payload):
        """v0035：为已完成任务单独补翻译（返回 created/source_job_id/job/group）。"""
        settings = self._translate_precondition(payload)
        target_lang = self._translate_target_lang(payload, settings)
        source_job = self._get_job(job_id)
        self._check_translate_source(source_job)
        existing = self._find_active_translate_job(job_id)
        if existing is not None:
            group = None
            if existing.group_id:
                try:
                    group = self._group_snapshot(
                        self._load_group_record(existing.group_id))
                except (ApiError, SubtitleError):
                    group = None
            return {
                "created": False,
                "source_job_id": job_id,
                "job": existing.snapshot(),
                "group": group,
            }
        # 提交时源文本必须可用；运行期会重新解析（源文件可能变化）
        self._resolve_source_text(job_id, source_job.path)
        job = self._build_translate_job(user, source_job, target_lang)
        options = self._translate_options(
            source_job, target_lang, {"translated_from_job": job_id})
        group_record = self._create_translate_group(user, [job], options)
        self._ensure_worker()
        return {
            "created": True,
            "source_job_id": job_id,
            "job": job.snapshot(),
            "group": self._group_snapshot(group_record),
        }

    def _group_source_jobs(self, record):
        """按创建时间升序返回组内仍存在的任务对象（跳过已被清理的任务）。"""
        jobs = []
        seen = set()
        with self._lock:
            jobs_by_id = dict(self._jobs)
        for batch_id in record.get("batch_ids") or []:
            try:
                batch_record = self._load_batch_record(batch_id)
            except (ApiError, SubtitleError):
                continue
            for job_id in batch_record.get("job_ids") or []:
                if job_id in seen:
                    continue
                seen.add(job_id)
                job = jobs_by_id.get(job_id)
                if job is not None:
                    jobs.append(job)
        jobs.sort(key=lambda item: item.created_epoch)
        return jobs

    def translate_batch_group(self, user, group_id, payload):
        """v0035：整组补翻译，派生新组并返回分类统计。"""
        settings = self._translate_precondition(payload)
        target_lang = self._translate_target_lang(payload, settings)
        record = self._load_group_record(group_id)
        source_jobs = self._group_source_jobs(record)
        summary = {
            "eligible": 0,
            "created": 0,
            "already_translated": 0,
            "missing_source": 0,
            "duplicates": 0,
            "rejected": [],
        }
        options_source = None
        jobs = []
        active_translate = self._active_translate_by_source()
        for source_job in source_jobs:
            if source_job.status != STATUS_DONE:
                continue
            if source_job.translate and not source_job.translation_partial:
                summary["already_translated"] += 1
                continue
            try:
                self._resolve_source_text(source_job.job_id, source_job.path)
            except SubtitleError:
                summary["missing_source"] += 1
                continue
            summary["eligible"] += 1
            if source_job.job_id in active_translate:
                summary["duplicates"] += 1
                continue
            try:
                jobs.append(self._build_translate_job(
                    user, source_job, target_lang))
            except Exception as exc:  # 构造失败只记录，不整组失败
                summary["rejected"].append({
                    "path": source_job.path,
                    "code": getattr(exc, "code", "INVALID_ARGUMENT"),
                    "message": getattr(exc, "message", u"补翻译任务创建失败"),
                })
                continue
            if options_source is None:
                options_source = source_job
        group_snapshot = None
        new_group_id = None
        if jobs:
            options = self._translate_options(
                options_source, target_lang,
                {"translated_from_group": group_id})
            group_record = self._create_translate_group(user, jobs, options)
            new_group_id = group_record.get("group_id")
            summary["created"] = len(jobs)
            group_snapshot = self._group_snapshot(group_record)
            self._ensure_worker()
        self.store.audit("subtitle_translate_group", user, {
            "group_id": new_group_id,
            "source_group_id": group_id,
            "created": summary["created"],
        })
        return {
            "created": summary["created"],
            "group": group_snapshot,
            "summary": summary,
        }

    def _trim_groups_locked(self):
        """批次组保留上限：只清理已终态的老组（组内任务不参与单任务 LRU）。"""
        try:
            records = self.store.load_subtitle_batch_groups(limit=500)
        except Exception:
            return
        limit = max(1, int(config.SUBTITLE_BATCH_GROUP_LIMIT))
        for record in records[limit:]:
            try:
                snapshot = self._group_snapshot(record)
            except Exception:
                continue
            if snapshot.get("status") in ACTIVE_STATUSES:
                continue
            if snapshot.get("status") == STATUS_PAUSED:
                continue
            self._remove_group_locked(record)

    def _remove_group_locked(self, record):
        group_id = record.get("group_id")
        for batch_id in record.get("batch_ids") or []:
            try:
                batch_record = self._load_batch_record(batch_id)
            except (ApiError, SubtitleError):
                batch_record = None
            for job_id in (batch_record or {}).get("job_ids") or []:
                self._jobs.pop(job_id, None)
                self._remove_job_files(job_id)
                try:
                    os.unlink(self._job_json(job_id))
                except OSError:
                    pass
                try:
                    self.store.delete_subtitle_job(job_id)
                except Exception:
                    pass
            try:
                os.unlink(self._batch_json(batch_id))
            except OSError:
                pass
            try:
                self.store.delete_subtitle_batch(batch_id)
            except Exception:
                pass
        try:
            os.unlink(self._group_json(group_id))
        except OSError:
            pass
        try:
            self.store.delete_subtitle_batch_group(group_id)
        except Exception:
            pass

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
                target_lang, CONNECTION_TEST_TIMEOUT_SEC,
                disable_thinking=bool(
                    settings.get("subtitle_llm_disable_thinking")))
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
                                u"请先确认音频/字幕文本将上传到所配置的第三方服务")
        job = self._prepare_job(user, payload, settings)
        skip_message = self._skip_message(job)
        if skip_message:
            job.status = STATUS_DONE
            job.stage = "write"
            job.progress = 100
            job.message = skip_message
            job.skipped = True
            self._register_job(job, enqueue=False)
            self.store.audit("subtitle_skip", user, {
                "job_id": job.job_id,
                "path": job.path,
                "reason": skip_message,
            })
            return job.snapshot()
        self._register_job(job, enqueue=True)
        self.store.audit("subtitle_start", user, {
            "job_id": job.job_id,
            "path": job.path,
            "source": job.source,
            "language": job.language,
            "translate": bool(job.translate),
            "target_lang": job.target_lang,
            "output_mode": job.output_mode,
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
                self._timeline_close(job, "cancelled")
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
            if previous in (STATUS_RUNNING, STATUS_TRANSLATING):
                self._timeline_close(job, "paused", finished=False)
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
            if job.job_id in self._active_ids:
                job.status = STATUS_RUNNING
                job.message = u"任务继续"
            else:
                job.status = STATUS_QUEUED
                job.message = u"排队等待继续"
                if job.job_id not in self._queue:
                    self._enqueue_locked(job)
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
        """优雅退出：在分片边界暂停可继续任务（计划 C），而不是取消。

        v0034：queued 任务保持 queued（DB/JSON 中不置失败），重启后由
        _load_jobs 重新入队；这里只需停止所有 worker 并等待分片边界。
        """
        with self._lock:
            self._stopping = True
            jobs = list(self._jobs.values())
            for job in jobs:
                if job.status in (STATUS_RUNNING, STATUS_TRANSLATING):
                    # 运行中任务在分片边界暂停；重启后有分片缓存 → 可继续
                    job.pause_event.set()
                # queued 任务保持排队：重启后自动重新入队执行（不再判失败）
            self._cond.notify_all()
            workers = list(self._workers)
            workers.extend(item for item in self._retiring
                           if item not in workers)
        for worker in workers:
            if worker is not None and worker.is_alive():
                worker.join(timeout=max(0.1, float(timeout)))
        with self._lock:
            self._workers = [item for item in self._workers
                             if item.is_alive()]
            self._retiring = {item for item in self._retiring
                              if item.is_alive()}

    # ------------------------------------------------------------------
    # 队列 worker
    # ------------------------------------------------------------------
    def _ensure_worker(self):
        """把 worker 数调整到 self.concurrency（剔除已退出线程、补新线程）。"""
        with self._lock:
            if self._stopping:
                return
            self._sync_workers_locked()

    def _sync_workers_locked(self):
        """按 self.concurrency 调整 worker 池（调用方需持有 self._lock）。

        v0037：多余的线程移入 self._retiring，由线程自身在任务边界退出；
        调小并发不会打断正在执行的 ASR/翻译任务。
        """
        self._workers = [item for item in self._workers
                         if item is not None and item.is_alive()]
        self._retiring = {item for item in self._retiring
                          if item is not None and item.is_alive()}
        if len(self._workers) > self.concurrency:
            for worker in self._workers[self.concurrency:]:
                self._retiring.add(worker)
            self._workers = self._workers[:self.concurrency]
        while len(self._workers) < self.concurrency:
            worker = threading.Thread(target=self._worker_loop,
                                      name="subtitles", daemon=True)
            self._workers.append(worker)
            worker.start()
        self._cond.notify_all()

    def _worker_loop(self):
        me = threading.current_thread()
        while True:
            with self._lock:
                while (not self._queue and not self._stopping
                       and me not in self._retiring):
                    self._cond.wait(timeout=0.5)
                if self._stopping or me in self._retiring:
                    # v0037：调小并发后，多余 worker 在任务边界退出
                    self._retiring.discard(me)
                    self._cond.notify_all()
                    return
                # v0048：资源管控——负载超过阈值时保留队列不取任务
                while self._queue and not self._stopping:
                    guard = self._load_guard_state()
                    if not guard.get("blocked"):
                        break
                    self._cond.wait(timeout=2.0)
                    if me in self._retiring:
                        self._retiring.discard(me)
                        self._cond.notify_all()
                        return
                if not self._queue:
                    continue
                job_id = self._queue.popleft()
                self._active_ids.add(job_id)
            job = self._jobs.get(job_id)
            try:
                if job is not None and job.status == STATUS_QUEUED:
                    self._process(job)
            finally:
                with self._lock:
                    self._active_ids.discard(job_id)
                    self._cond.notify_all()

    # ------------------------------------------------------------------
    # 处理流程
    # ------------------------------------------------------------------
    def _update(self, job, **changes):
        with job.lock:
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_epoch = time.time()
            # v0032：只有“非组任务”完成时才需要做单任务 LRU，避免组注册时
            # 每次登记都全量扫描（20000 任务会退化为 O(n²)）。
            if (job.status in FINISHED_STATUSES
                    and not getattr(job, "group_id", None)):
                self._trim_needed = True
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
                    self._enqueue_locked(job)
                    self._cond.notify_all()
                return
            job.resumable = True
            job.message = u"任务已暂停，当前分片已完成"
            job.updated_epoch = time.time()
            self._timeline_close(job, "paused", finished=False)
            self._persist(job)
        self.store.audit("subtitle_pause", job.user, {
            "job_id": job.job_id,
            "chunk_index": int(job.chunk_index),
            "chunk_total": int(job.chunk_total),
        })

    def _process(self, job):
        settings = self._settings() or {}
        self._timeline_begin(job)
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
            if job.source == SOURCE_EXISTING_SRT:
                self._process_existing_srt(job, settings)
            else:
                self._process_asr(job, settings)
        except SubtitlePaused:
            self._handle_paused(job)
        except SubtitleCancelled:
            # v0038：取消也写入步骤结束时间（进行中的步骤标记 cancelled）
            self._timeline_close(job, "cancelled")
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
            self._handle_failure(job, "TIMEOUT", u"音频提取超时")
        except SubtitleError as exc:
            self._handle_failure(job, exc.code, exc.message)
        except Exception:
            if os.environ.get("DUP_SUBTITLE_DEBUG") == "1":
                import traceback
                traceback.print_exc()
            self._handle_failure(job, "INTERNAL_ERROR",
                                 u"字幕生成失败，请稍后重试")

    # ------------------------------------------------------------------
    # v0048：失败分类 / 智能重试 / 会话级资源统计
    # ------------------------------------------------------------------
    def _handle_failure(self, job, code, message):
        """统一失败处理：分类 → 可恢复错误自动重试 → 否则标记失败。"""
        category = failure_category(code, message)
        retryable = failure_retryable(category)
        settings = self._settings() or {}
        auto = settings.get("subtitle_auto_retry_enabled") is True
        try:
            max_attempts = int(settings.get("subtitle_auto_retry_max") or 0)
        except (TypeError, ValueError):
            max_attempts = 0
        max_attempts = max(0, min(max_attempts, config.SUBTITLE_AUTO_RETRY_MAX_LIMIT))
        with job.lock:
            attempt = int(job.attempt or 0)
            status = job.status
        if (auto and retryable and attempt < max_attempts
                and status not in (STATUS_CANCELLED, STATUS_DONE)):
            with self._lock:
                job.attempt = attempt + 1
                job.status = STATUS_QUEUED
                job.stage = "audio"
                job.progress = 0
                job.message = u"自动重试（%d/%d）：%s" % (
                    job.attempt, max_attempts, message)
                job.error = None
                job.resumable = True
                job.failure_category = category
                job.retryable = True
                job.updated_epoch = time.time()
                self._timeline_close(job, "retry", finished=False)
                self._enqueue_locked(job)
                self._cond.notify_all()
            self._persist(job)
            self.store.audit("subtitle_retry_auto", job.user, {
                "job_id": job.job_id, "code": code, "category": category,
                "attempt": int(job.attempt), "max": max_attempts})
            return
        skipped = (category == "no_speech"
                   and settings.get("subtitle_no_speech_skip") is not False)
        display = message
        if skipped:
            display = u"%s（无语音，已跳过，不参与自动重试）" % message
        self._fail(job, code, display, category=category,
                   retryable=retryable, skipped=skipped)

    def _finish_done(self, job, files, outputs, output_errors, warning,
                     partial, progress_message, extra_audit=None):
        """统一收尾：任务成功 + 写回审计（写回失败只记 warning，不判失败）。"""
        with job.lock:
            total = int(job.chunk_total or 0)
        audit = {
            "job_id": job.job_id,
            "files": [item["name"] for item in files],
            "translation_partial": bool(partial),
            "outputs": len(outputs or []),
            "output_errors": len(output_errors or []),
        }
        if job.batch_id:
            audit["batch_id"] = job.batch_id
        if extra_audit:
            audit.update(extra_audit)
        self._timeline_close(job, "done")
        self._update(job, status=STATUS_DONE, stage="write", progress=100,
                     message=progress_message, files=files, error=None,
                     resumable=False, chunk_index=total,
                     outputs=list(outputs or []),
                     output_errors=list(output_errors or []),
                     warning=warning)
        self._record_duration(job)
        self.store.audit("subtitle_done", job.user, audit)
        for item in (outputs or []):
            self.store.audit("subtitle_output", job.user, {
                "job_id": job.job_id,
                "name": item.get("name"),
                "path": item.get("path"),
                "kind": item.get("kind"),
            })
        if output_errors:
            self.store.audit("subtitle_output", job.user, {
                "job_id": job.job_id,
                "errors": len(output_errors),
                "warning": warning,
            })

    def _process_asr(self, job, settings):
        job.step_start("probe")
        if not self.ffmpeg.available():
            raise SubtitleError("FFMPEG_UNAVAILABLE", u"未找到 FFmpeg，无法提取音频")
        timeout = float(settings.get("subtitle_asr_timeout_sec") or 300)
        probe = self.ffmpeg.probe_media(job.path, timeout,
                                        lambda: self._stop_check(job))
        if not probe.get("has_audio"):
            raise SubtitleError("NO_AUDIO",
                                u"视频没有音轨（no_audio），无法生成字幕")
        # v0048：解析音轨选择（序号/语言标签 → ffmpeg -map）
        self._resolve_audio_map(job, probe)
        duration = probe.get("duration")
        if not duration or duration <= 0:
            raise SubtitleError("NO_DURATION", u"无法读取视频时长")
        chunk_sec = int(settings.get("subtitle_asr_chunk_sec") or 600)
        max_audio_mb = int(settings.get("subtitle_asr_max_audio_mb") or 24)
        max_bytes = max_audio_mb * 1024 * 1024
        mode = job.audio_source or config.SUBTITLE_DEFAULT_AUDIO_SOURCE
        server_max_bytes = config.SUBTITLE_SERVER_UPLOAD_MAX_MB * 1024 * 1024
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
            job.step_end("probe", detail={
                "duration_sec": round(float(duration), 3),
                "chunks": len(plan),
            })
            resuming = int(job.chunk_index or 0) > 0
            self._update(
                job, status=STATUS_RUNNING,
                stage="asr" if resuming else "audio",
                progress=20 if resuming else 2,
                message=u"正在识别语音" if resuming else u"正在提取音频")
            job.step_start("asr")
            cues, fallback_used = self._run_chunked_asr(
                job, settings, plan, max_bytes, tempdir, mode=mode,
                server_max_bytes=server_max_bytes)
            cues = dedupe_cues(cues)
            if not cues:
                raise SubtitleError("NO_SPEECH", u"未识别到可用字幕内容")
            job.step_end("asr", detail={
                "chunks": len(plan),
                "cues": len(cues),
                "fallback": bool(fallback_used),
            })
            translations = None
            partial = False
            if job.translate:
                job.step_start("translate")
                self._update(job, status=STATUS_TRANSLATING,
                             stage="translate", progress=70,
                             message=u"正在翻译字幕")
                translations, partial = self._translate_cues(
                    job, settings, cues)
                job.step_end("translate", detail={
                    "cues": len(cues),
                    "batches": len(job.translate_batches),
                    "partial": bool(partial),
                })
            # 最后一个翻译批次结束时也要响应暂停（resume 会重跑翻译批次，
            # 但 ASR 分片缓存保证不重复上传音频）
            self._check_control(job)
            job.step_start("write")
            self._update(job, stage="write", progress=91,
                         message=u"正在写入字幕文件")
            files, outputs, output_errors, warning = self._write_outputs(
                job, cues, translations, partial,
                write_vtt=bool(settings.get("subtitle_write_vtt")),
                translate_outputs=settings.get("subtitle_translate_outputs"))
            job.step_end("write", detail={
                "files": len(files),
                "outputs": len(outputs or []),
                "output_errors": len(output_errors or []),
            })
            if fallback_used and not warning:
                warning = WARNING_SERVER_AUDIO_FALLBACK
            self._finish_done(job, files, outputs, output_errors, warning,
                              partial, u"字幕生成完成")
        finally:
            shutil.rmtree(tempdir, ignore_errors=True)

    def _process_existing_srt(self, job, settings):
        """只翻译已有字幕：不启动 FFmpeg、不调用 ASR。

        v0035：补翻译任务优先读源任务目录 original.srt；源任务目录缺失时
        回退到视频同目录 <stem>.srt（不要求视频文件仍存在）。
        """
        source_srt, text = self._resolve_source_text(
            getattr(job, "source_job_id", None), job.path)
        cues = parse_srt(text)
        if not cues:
            raise SubtitleError("NO_SPEECH", u"已有字幕文件没有可用内容")
        job.step_start("translate")
        self._update(job, status=STATUS_TRANSLATING, stage="translate",
                     progress=10, message=u"正在翻译已有字幕")
        translations, partial = self._translate_cues(job, settings, cues)
        job.step_end("translate", detail={
            "cues": len(cues),
            "batches": len(job.translate_batches),
            "partial": bool(partial),
        })
        self._check_control(job)
        job.step_start("write")
        self._update(job, stage="write", progress=91,
                     message=u"正在写入字幕文件")
        files, outputs, output_errors, warning = self._write_outputs(
            job, cues, translations, partial, existing_source=source_srt,
            write_vtt=bool(settings.get("subtitle_write_vtt")),
            translate_outputs=settings.get("subtitle_translate_outputs"))
        job.step_end("write", detail={
            "files": len(files),
            "outputs": len(outputs or []),
            "output_errors": len(output_errors or []),
        })
        self._finish_done(job, files, outputs, output_errors, warning,
                          partial, u"字幕翻译完成",
                          extra_audit={"source_srt": source_srt})

    def _record_duration(self, job):
        """v0048：记录完成任务的实际耗时样本（用于 ETA 校准）。"""
        try:
            with job.lock:
                started = job.started_epoch
                finished = job.finished_epoch or time.time()
            if started is None:
                return
            duration = float(finished) - float(started)
            if duration > 0:
                self._duration_samples.append(duration)
        except (TypeError, ValueError, OverflowError):  # pragma: no cover
            pass

    def _fail(self, job, code, message, category=None, retryable=None,
              skipped=False):
        with job.lock:
            job.resumable = False
            job.failure_category = category or failure_category(code, message)
            job.retryable = (failure_retryable(job.failure_category)
                             if retryable is None else bool(retryable))
        self._timeline_close(job, "failed")
        self._update(job, status=STATUS_FAILED, message=message,
                     error={"code": code, "message": message},
                     skipped=bool(skipped or job.skipped))
        self._record_duration(job)
        self.store.audit("subtitle_failed", job.user, {
            "job_id": job.job_id, "code": code,
            "category": job.failure_category,
            "retryable": bool(job.retryable),
            "skipped": bool(job.skipped)})

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
                             tempdir, counter, mode="nas",
                             server_max_bytes=None, stop_check=None):
        """抽取单个逻辑分片的音频；超过上限时继续对半切。

        v0032：mode=server 时用 FFmpeg 流复制抽音轨（不解码），失败后回退
        到 NAS 解码（mp3）。返回 (entries, fallback_used)；entry 为 dict：
        {start, length, path, filename, content_type}。
        v0040：stop_check 由分片音频预取传入（取消/预取关闭时尽快杀掉 FFmpeg）。
        """
        if mode == "server":
            try:
                return (self._extract_chunk_remux(
                    job, settings, start, length,
                    server_max_bytes or config.SUBTITLE_SERVER_UPLOAD_MAX_MB
                    * 1024 * 1024, tempdir, counter,
                    stop_check=stop_check), False)
            except SubtitleError:
                raise
            except video_similarity.VideoStopped:
                raise SubtitleCancelled()
            except (video_similarity.VideoSkip, OSError):
                pass  # 容器/编码不支持流复制 → 回退 NAS 解码
        results, _ = self._extract_chunk_audio_nas(
            job, settings, start, length, max_bytes, tempdir, counter,
            fallback=mode == "server", stop_check=stop_check)
        return results, mode == "server"

    def _extract_chunk_audio_nas(self, job, settings, start, length, max_bytes,
                                 tempdir, counter, fallback=False,
                                 stop_check=None):
        timeout = float(settings.get("subtitle_asr_timeout_sec") or 300)
        check = stop_check or (lambda: self._stop_check(job))
        queue = [(float(start), float(length))]
        results = []
        while queue:
            self._check_cancel(job)
            chunk_start, chunk_length = queue.pop(0)
            path = os.path.join(tempdir, "chunk-%05d.mp3" % counter[0])
            counter[0] += 1
            try:
                self.ffmpeg.extract_audio_mp3(
                    job.path, path, chunk_start, chunk_length, timeout, check,
                    track_map=job.audio_map)
            except video_similarity.VideoStopped:
                raise SubtitleCancelled()
            try:
                size = os.path.getsize(path)
            except OSError:
                raise SubtitleError("NO_AUDIO", u"无法生成音频分片")
            if size <= max_bytes or chunk_length <= 0.5:
                results.append({
                    "start": chunk_start,
                    "length": chunk_length,
                    "path": path,
                    "filename": "audio-%05d.mp3" % counter[0],
                    "content_type": "audio/mpeg",
                })
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
        results.sort(key=lambda item: item["start"])
        return results, fallback

    def _extract_chunk_remux(self, job, settings, start, length, max_bytes,
                             tempdir, counter, stop_check=None):
        """流复制抽音轨（v0032 “服务器解码”模式）：不解码，速度快。"""
        timeout = float(settings.get("subtitle_asr_timeout_sec") or 300)
        check = stop_check or (lambda: self._stop_check(job))
        queue = [(float(start), float(length))]
        results = []
        while queue:
            self._check_cancel(job)
            chunk_start, chunk_length = queue.pop(0)
            path = os.path.join(tempdir, "segment-%05d.mka" % counter[0])
            counter[0] += 1
            try:
                self.ffmpeg.extract_audio_remux(
                    job.path, path, chunk_start, chunk_length, timeout, check,
                    track_map=job.audio_map)
            except video_similarity.VideoStopped:
                raise SubtitleCancelled()
            try:
                size = os.path.getsize(path)
            except OSError:
                raise video_similarity.VideoSkip(
                    "decode", "remuxed segment missing")
            if size <= max_bytes or chunk_length <= 1.0:
                results.append({
                    "start": chunk_start,
                    "length": chunk_length,
                    "path": path,
                    "filename": "segment-%05d.mka" % counter[0],
                    "content_type": "audio/x-matroska",
                })
                continue
            try:
                os.unlink(path)
            except OSError:
                pass
            half = chunk_length / 2.0
            if half <= 0.5:
                raise SubtitleError("AUDIO_TOO_LARGE",
                                    u"音频分块后仍超过大小上限")
            queue.insert(0, (chunk_start + half, chunk_length - half))
            queue.insert(0, (chunk_start, half))
            if len(queue) + len(results) > 500:
                raise SubtitleError("AUDIO_TOO_LARGE", u"音频分块数量过多")
        results.sort(key=lambda item: item["start"])
        return results

    def _run_chunked_asr(self, job, settings, plan, max_bytes, tempdir,
                         mode="nas", server_max_bytes=None):
        """按分片顺序 ASR；已完成分片直接读 DB 缓存，不重复上传音频。

        返回 (cues, fallback_used)：fallback_used 表示 server 音频模式因
        流复制失败回退到 NAS 解码（任务 warning=SERVER_AUDIO_FALLBACK）。
        """
        base_url = settings.get("subtitle_asr_base_url") or ""
        api_key = settings.get("subtitle_asr_api_key") or ""
        model = settings.get("subtitle_asr_model") or "whisper-1"
        language = job.language or "auto"
        timeout = float(settings.get("subtitle_asr_timeout_sec") or 300)
        cached = {item["chunk_index"]: item
                  for item in self.store.load_subtitle_chunks(job.job_id)}
        total = max(1, len(plan))
        all_cues = []
        fallback_used = False
        counter = [0]
        # v0040：分片音频预取（设置 subtitle_audio_prefetch，默认开）。只预读
        # 下一片：主线程识别第 N 片时后台已抽好第 N+1 片音频，FFmpeg 与网络
        # 重叠。关闭时 take() 退化为原地串行抽取，与历史行为完全一致。
        prefetch = _AudioPrefetch(
            self, job, settings, plan, cached, max_bytes, tempdir, counter,
            mode=mode, server_max_bytes=server_max_bytes,
            enabled=bool(settings.get("subtitle_audio_prefetch", True)))
        try:
            for index, (start, length) in enumerate(plan):
                self._check_control(job)
                done_row = cached.get(index)
                if done_row is not None and done_row.get("status") == CHUNK_DONE:
                    all_cues.extend(done_row.get("cues") or [])
                    with job.lock:
                        job.chunk_index = max(int(job.chunk_index or 0), index + 1)
                    continue
                chunk = prefetch.take(index)
                audio = chunk["audio"]
                chunk_fallback = chunk["fallback"]
                extract_sec = chunk["extract_sec"]
                chunk_started = chunk["started_epoch"]
                fallback_used = fallback_used or chunk_fallback
                chunk_cues = []
                asr_started = time.time()
                try:
                    for entry in audio:
                        self._check_cancel(job)
                        parsed = self.client.transcribe(
                            base_url, api_key, model, language, entry["path"],
                            timeout, job.cancel_event,
                            filename=entry["filename"],
                            content_type=entry["content_type"])
                        sub_start = float(entry["start"])
                        for cue in parsed:
                            chunk_cues.append({
                                "start": max(0.0, float(cue["start"]) + sub_start),
                                "end": max(0.0, float(cue["end"]) + sub_start),
                                "text": cue["text"],
                            })
                finally:
                    for entry in audio:
                        try:
                            os.unlink(entry["path"])
                        except OSError:
                            pass
                chunk_cues = dedupe_cues(chunk_cues)
                asr_sec = round(time.time() - asr_started, 3)
                try:
                    self.store.save_subtitle_chunk(
                        job.job_id, index, start, start + length,
                        CHUNK_DONE, chunk_cues)
                except Exception:
                    pass
                all_cues.extend(chunk_cues)
                job.add_asr_chunk({
                    "index": index, "status": "done",
                    "start_epoch": round(chunk_started, 3),
                    "end_epoch": round(time.time(), 3),
                    "extract_sec": extract_sec, "asr_sec": asr_sec,
                    "fallback": bool(chunk_fallback),
                })
                progress = 20 + int(50.0 * (index + 1) / total)
                self._update(
                    job, stage="asr", progress=progress,
                    message=u"正在识别语音 %d/%d" % (index + 1, total),
                    chunk_index=index + 1, chunk_total=total)
                # 暂停只在完整分片保存后生效
                self._check_control(job)
        finally:
            prefetch.close()
        return all_cues, fallback_used

    def _translate_cues(self, job, settings, cues):
        base_url = settings.get("subtitle_llm_base_url") or ""
        api_key = settings.get("subtitle_llm_api_key") or ""
        model = settings.get("subtitle_llm_model") or "gpt-4.1-mini"
        timeout = float(settings.get("subtitle_translate_timeout_sec") or 120)
        batch_size = max(1, int(settings.get("subtitle_translate_batch_size") or 20))
        disable_thinking = bool(settings.get("subtitle_llm_disable_thinking"))
        translations = []
        partial = False
        total = max(1, len(cues))
        for start in range(0, len(cues), batch_size):
            # 翻译批次是暂停安全点：当前批完成后暂停；resume 重新翻译当前批次即可
            self._check_control(job)
            batch = cues[start:start + batch_size]
            batch_index = start // batch_size
            batch_started = time.time()
            texts = [sanitize_cue_text(item["text"]) for item in batch]
            result = None
            attempt = 0
            while attempt < 2:
                attempt += 1
                try:
                    candidate = self.client.translate(
                        base_url, api_key, model, texts, job.target_lang,
                        timeout, job.cancel_event,
                        disable_thinking=disable_thinking)
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
            batch_partial = result is None
            if batch_partial:
                partial = True
                result = texts
            translations.extend(result)
            batch_finished = time.time()
            job.add_translate_batch({
                "index": batch_index,
                "status": "partial" if batch_partial else "done",
                "start_epoch": round(batch_started, 3),
                "end_epoch": round(batch_finished, 3),
                "duration_sec": round(batch_finished - batch_started, 3),
                "cues": len(batch), "attempts": attempt,
                "partial": bool(batch_partial),
            })
            progress = 70 + int(20.0 * (start + len(batch)) / total)
            self._update(job, stage="translate", progress=progress,
                         message=u"正在翻译字幕 %d/%d" % (
                             min(start + len(batch), len(cues)), len(cues)))
        return translations, partial

    @staticmethod
    def _translate_outputs_mode(value):
        """v0036：译文输出组合兜底；非法/缺失回退 both（与历史行为一致）。"""
        if value in config.SUBTITLE_TRANSLATE_OUTPUTS:
            return value
        return config.SUBTITLE_DEFAULT_TRANSLATE_OUTPUTS

    def _write_outputs(self, job, cues, translations, partial,
                       existing_source=None, write_vtt=False,
                       translate_outputs=None):
        directory = self._job_dir(job.job_id)
        os.makedirs(directory, exist_ok=True)
        # v0033：默认只生成 SRT；VTT 仅当设置 subtitle_write_vtt=true 时生成。
        # v0036：译文输出组合由 subtitle_translate_outputs 控制；original.srt
        # 始终写入（补翻译/只翻译的源文本，也是任务目录下载兜底）。
        self._write_file(directory, "original.srt", build_srt(cues))
        if write_vtt:
            self._write_file(directory, "original.vtt", build_vtt(cues))
        outputs_mode = self._translate_outputs_mode(translate_outputs)
        if translations is not None:
            target = job.target_lang
            if outputs_mode != "bilingual_only":
                self._write_file(directory, "%s.srt" % target,
                                 build_srt(cues, translations))
                if write_vtt:
                    self._write_file(directory, "%s.vtt" % target,
                                     build_vtt(cues, translations))
            if outputs_mode != "target_only":
                self._write_file(directory, "bilingual.%s.srt" % target,
                                 build_srt(cues, translations, bilingual=True))
        job.translation_partial = bool(partial)
        files = self._list_files(job.job_id)
        outputs, output_errors, warning = self._write_alongside(
            job, cues, translations, existing_source,
            translate_outputs=outputs_mode)
        return files, outputs, output_errors, warning

    def _write_alongside(self, job, cues, translations, existing_source=None,
                         translate_outputs=None):
        """把 srt 复制到视频同目录（只写 .srt，从不覆盖已有文件）。

        返回 (outputs, output_errors, warning)；写回失败只记 warning，
        任务仍为 done，任务目录里的文件可继续下载。
        v0036：译文输出组合（both/target_only/bilingual_only）与任务目录一致。
        """
        outputs = []
        errors = []
        if job.output_mode != "alongside_video":
            return outputs, errors, None
        directory, stem = split_video_stem(job.path)
        if not directory or not fsutil.writable_dir(directory):
            return outputs, errors, WARNING_OUTPUT_NOT_WRITABLE
        names = subtitle_output_names(stem, job.target_lang)
        outputs_mode = self._translate_outputs_mode(translate_outputs)
        targets = []
        # 原文：只有 ASR 生成才写；只翻译模式下视频旁本来就有 <stem>.srt
        if existing_source is None:
            targets.append(("original", names["original"], build_srt(cues)))
        if translations is not None:
            if outputs_mode != "bilingual_only":
                targets.append(("translation", names["translation"],
                                build_srt(cues, translations)))
            if outputs_mode != "target_only":
                targets.append(("bilingual", names["bilingual"],
                                build_srt(cues, translations, bilingual=True)))
        for kind, name, content in targets:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                continue
            try:
                size = atomic_write_text(path, content)
            except OSError:
                errors.append({"name": name, "kind": kind,
                               "message": u"写入失败"})
                continue
            outputs.append({"name": name, "path": path,
                            "size": int(size), "kind": kind})
        warning = WARNING_OUTPUT_PARTIAL if errors else None
        return outputs, errors, warning

    @staticmethod
    def _write_file(directory, name, content):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)

    # ------------------------------------------------------------------
    # LRU 清理
    # ------------------------------------------------------------------
    def _trim_jobs_locked(self):
        if not getattr(self, "_trim_needed", False):
            return
        if len(self._jobs) <= self.job_limit:
            self._trim_needed = False
            return
        finished = [job for job in self._jobs.values()
                    if job.status in FINISHED_STATUSES
                    and not getattr(job, "group_id", None)]
        finished.sort(key=lambda item: item.created_epoch)
        while len(self._jobs) > self.job_limit and finished:
            job = finished.pop(0)
            self._jobs.pop(job.job_id, None)
            # 只删任务目录/元数据；写回视频旁的 srt 永不删除。
            self._remove_job_files(job.job_id)
            try:
                os.unlink(self._job_json(job.job_id))
            except OSError:
                pass
            try:
                self.store.delete_subtitle_job(job.job_id)
            except Exception:
                pass
            self._trim_batch_locked(job.batch_id)
        self._trim_needed = False

    def _trim_batch_locked(self, batch_id):
        """批次不再有任何任务时删除批次记录（JSON + DB）。"""
        if not batch_id:
            return
        if any(item.batch_id == batch_id for item in self._jobs.values()):
            return
        try:
            os.unlink(self._batch_json(batch_id))
        except OSError:
            pass
        try:
            self.store.delete_subtitle_batch(batch_id)
        except Exception:
            pass

    def _remove_job_files(self, job_id):
        try:
            shutil.rmtree(self._job_dir(job_id), ignore_errors=True)
        except Exception:
            pass
