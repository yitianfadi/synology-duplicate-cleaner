# -*- coding: utf-8 -*-
"""扫描/删除设置：默认值、白名单校验、持久化。"""

from __future__ import absolute_import

import copy
import os
import re
import threading

from . import config
from .errors import ApiError

# 契约 §6 的设置键与默认值（顺序即前端展示顺序）
DEFAULT_SETTINGS = {
    "hash_algo": "sha256",
    "keep_mode": "latest",
    "pinned_path": "",
    "skip_empty": True,
    "skip_hidden": True,
    # 符号链接必须始终跳过：后端强制为 True（契约要求）
    "skip_symlinks": True,
    "include_extensions": [],
    "exclude_extensions": [],
    "ignore_case_ext": True,
    "min_size_bytes": 0,
    "max_size_bytes": 0,
    "quick_sample_kb": 64,
    "concurrency": 2,
    "hash_rate_mbps": 0,
    "recycle_default": True,
    "create_recycle_dir": False,
    "max_files": config.MAX_FILES_DEFAULT,
    "allow_ctime_fallback": False,
    # --- 内容相似度模式（docs/SIMILARITY_CONTRACT.md §2）---
    "scan_mode": "exact",
    "similarity_threshold": 85,
    "similarity_extensions": list(config.DEFAULT_SIMILARITY_EXTENSIONS),
    "similarity_exclude_extensions": [],
    "similarity_concurrency": 2,
    "similarity_cache": True,
    "similarity_max_file_mb": 32,
    "similarity_allow_binary": False,
    # --- 视频视觉相似度（docs/VIDEO_CONTRACT.md §1）---
    # 外部 FFmpeg 路径：空=内置/自动探测；非空必须是可执行的绝对路径。
    "ffmpeg_path": "",
    "video_similarity_enabled": False,
    "video_extensions": list(config.DEFAULT_VIDEO_EXTENSIONS),
    "video_sample_frames": config.VIDEO_DEFAULT_SAMPLE_FRAMES,
    "video_max_sample_interval_sec": config.VIDEO_DEFAULT_MAX_INTERVAL,
    "video_similarity_threshold": config.VIDEO_DEFAULT_THRESHOLD,
    "video_concurrency": 1,
    "video_hwaccel": "auto",
    "video_max_file_mb": config.VIDEO_DEFAULT_MAX_FILE_MB,
    "video_timeout_sec": config.VIDEO_DEFAULT_TIMEOUT_SEC,
    "video_cache": True,
    # --- 可选 ASR 语音辅助（外部命令；默认关闭，不内置模型/不联网）---
    "video_asr_enabled": False,
    "video_asr_command": "",
    "video_asr_weight": config.ASR_DEFAULT_WEIGHT,
    "video_asr_timeout_sec": config.ASR_DEFAULT_TIMEOUT_SEC,
    "video_asr_max_sec": config.ASR_DEFAULT_MAX_SEC,
    # --- 云端字幕（OpenAI 兼容 ASR + LLM，docs/SUBTITLE_CONTRACT.md §1）---
    "subtitle_enabled": False,
    "subtitle_asr_base_url": "https://api.openai.com/v1",
    "subtitle_asr_api_key": "",
    "subtitle_asr_model": "whisper-1",
    "subtitle_asr_language": "auto",
    "subtitle_asr_timeout_sec": 300,
    "subtitle_asr_chunk_sec": 600,
    "subtitle_asr_max_audio_mb": 24,
    # v0032：音频来源 nas（本机抽音，默认）/ server（上传视频段由 ASR 服务器抽音）
    "subtitle_audio_source": config.SUBTITLE_DEFAULT_AUDIO_SOURCE,
    # v0033：默认只生成 SRT；开启后任务目录额外生成 VTT（写回视频目录始终只写 srt）
    "subtitle_write_vtt": False,
    "subtitle_translate_enabled": False,
    "subtitle_llm_base_url": "https://api.openai.com/v1",
    "subtitle_llm_api_key": "",
    "subtitle_llm_model": "gpt-4.1-mini",
    "subtitle_translate_target": "zh",
    "subtitle_translate_timeout_sec": 120,
    "subtitle_translate_batch_size": 20,
    # v0030：本地 Qwen3/llama.cpp 类服务可关闭“思考”以大幅提速（默认关，
    # 避免 OpenAI 官方等不支持 chat_template_kwargs 的服务拒收）。
    "subtitle_llm_disable_thinking": False,
    # --- 目录批量字幕（docs/SUBTITLE_BATCH_CONTRACT.md §2）---
    # 字幕默认写回视频同目录（只写 .srt）；download_only 仅保留任务目录下载。
    "subtitle_output_mode": config.SUBTITLE_DEFAULT_OUTPUT_MODE,
    "subtitle_skip_existing": True,
    "subtitle_job_limit": config.SUBTITLE_JOB_LIMIT_DEFAULT,
    # v0036：译文输出组合（both/target_only/bilingual_only），默认与历史一致。
    "subtitle_translate_outputs": config.SUBTITLE_DEFAULT_TRANSLATE_OUTPUTS,
    # v0034/v0037：同时执行的字幕任务数（默认 1 = 串行，与历史行为一致）；
    # 保存设置后立即生效（动态调整 worker，无需重启套件）。
    "subtitle_concurrency": config.SUBTITLE_DEFAULT_CONCURRENCY,
}

# 密钥键：GET/日志/审计/扫描结果中绝不允许出现明文
SECRET_SETTING_KEYS = ("subtitle_asr_api_key", "subtitle_llm_api_key")
# 清空密钥的控制标记（PUT 时允许从 settings 或顶层同时传入）
CLEAR_SECRET_ALIASES = {
    "subtitle_clear_asr_api_key": "subtitle_asr_api_key",
    "subtitle_asr_api_key_clear": "subtitle_asr_api_key",
    "clear_asr_api_key": "subtitle_asr_api_key",
    "subtitle_clear_llm_api_key": "subtitle_llm_api_key",
    "subtitle_llm_api_key_clear": "subtitle_llm_api_key",
    "clear_llm_api_key": "subtitle_llm_api_key",
}

SETTINGS_KEYS = tuple(DEFAULT_SETTINGS)

HASH_ALGOS = ("sha256", "md5")
KEEP_MODES = ("latest", "earliest", "pinned")
SCAN_MODES = ("exact", "similar")


def default_settings():
    return copy.deepcopy(DEFAULT_SETTINGS)


def _err(message, details=None):
    raise ApiError("INVALID_ARGUMENT", message, details=details or {})


def _check_bool(key, value):
    if not isinstance(value, bool):
        _err(u"设置 %s 必须是布尔值" % key, {"key": key})
    return value


def _check_float(key, value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _err(u"设置 %s 必须是数字" % key, {"key": key})
    if float(value) < low or float(value) > high:
        _err(u"设置 %s 超出范围 %s..%s" % (key, low, high), {"key": key})
    return float(value)


def _check_int(key, value, low, high):
    if isinstance(value, bool) or not isinstance(value, int):
        _err(u"设置 %s 必须是整数" % key, {"key": key})
    if value < low or value > high:
        _err(u"设置 %s 超出范围 %d..%d" % (key, low, high), {"key": key})
    return int(value)


def _check_str(key, value, max_len=4096):
    if not isinstance(value, str):
        _err(u"设置 %s 必须是字符串" % key, {"key": key})
    if len(value) > max_len:
        _err(u"设置 %s 过长" % key, {"key": key})
    if "\x00" in value:
        _err(u"设置 %s 包含非法字符" % key, {"key": key})
    return value


def _check_ext_list(key, value):
    if not isinstance(value, list):
        _err(u"设置 %s 必须是字符串数组" % key, {"key": key})
    out = []
    for item in value:
        if not isinstance(item, str):
            _err(u"设置 %s 的元素必须是字符串" % key, {"key": key})
        ext = item.strip()
        if not ext.startswith("."):
            _err(u"后缀 %r 必须以 . 开头" % ext, {"key": key, "value": ext})
        if len(ext) < 2 or len(ext) > 16:
            _err(u"后缀 %r 长度必须在 2..16 之间" % ext, {"key": key, "value": ext})
        if "/" in ext or "\\" in ext or "*" in ext or "?" in ext:
            _err(u"后缀 %r 包含非法字符" % ext, {"key": key, "value": ext})
        out.append(ext)
    return out


def _check_base_url(key, value):
    from urllib.parse import urlsplit
    text = _check_str(key, value, max_len=2048).strip()
    if not text:
        _err(u"设置 %s 不能为空" % key, {"key": key})
    parsed = urlsplit(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        _err(u"设置 %s 必须是 http/https URL" % key, {"key": key})
    # v0026 安全：拒绝 URL 内嵌 userinfo（user:pass@host），避免密钥进入 URL/
    # 日志/错误消息，也避免把凭据发给非预期主机。
    if ("@" in parsed.netloc or parsed.username is not None
            or parsed.password is not None):
        _err(u"设置 %s 不能包含账号密码（userinfo）" % key, {"key": key})
    if parsed.query or parsed.fragment:
        _err(u"设置 %s 不能包含查询串或片段" % key, {"key": key})
    return text.rstrip("/")


def validate_base_url_value(key, value):
    """公共 base URL 校验（云端字幕探测/设置复用；返回去尾斜杠的 URL）。"""
    return _check_base_url(key, value)


_LANGUAGE_RE = re.compile(r"^(?:[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?)$")
# v0026：模型名放宽到常见 OpenAI 兼容命名（允许 @ 与 +，如
# "meta-llama/Llama-3.1-8B@latest"、"model+fp16"）；仍拒绝空白与 shell 元字符。
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._/:@+\-]{1,100}$")


def _check_language(key, value, allow_auto=False):
    text = _check_str(key, value, max_len=32).strip()
    if allow_auto and text.lower() == "auto":
        return "auto"
    if not _LANGUAGE_RE.match(text):
        _err(u"设置 %s 语言代码非法" % key, {"key": key})
    return text


def _check_model_name(key, value):
    text = _check_str(key, value, max_len=100).strip()
    if not _MODEL_NAME_RE.match(text):
        _err(u"设置 %s 模型名非法" % key, {"key": key})
    return text


def public_settings(settings):
    """对外可见的设置：密钥置空并附 has_*_key 标记（绝不返回明文）。"""
    result = copy.deepcopy(settings)
    for key in SECRET_SETTING_KEYS:
        result[key] = ""
        result[key + "_set"] = bool((settings or {}).get(key))
    return result


def without_secrets(settings):
    """用于写入扫描结果/响应等可能被回读的结构的设置副本（直接移除密钥）。"""
    result = copy.deepcopy(settings)
    for key in SECRET_SETTING_KEYS:
        result.pop(key, None)
    return result


def prepare_settings_patch(patch):
    """剥离只读标记与清空标记，返回 (可校验 patch, 需清空的密钥集合)。

    - 空字符串表示“保持原值”（前端密码框不回填明文）；
    - clear_*_api_key=true 才真正删除。
    """
    if not isinstance(patch, dict):
        return patch, set()
    clean = dict(patch)
    clears = set()
    for alias, target in CLEAR_SECRET_ALIASES.items():
        flag = clean.pop(alias, None)
        if flag is True:
            clears.add(target)
    for key in SECRET_SETTING_KEYS:
        clean.pop(key + "_set", None)
        if clean.get(key) == "":
            clean.pop(key, None)
    return clean, clears


def _normalize_value(key, value):
    """按白名单规范单个设置值；未知键由 validate_patch 负责拒绝。"""
    if key in ("hash_algo",):
        if value not in HASH_ALGOS:
            _err(u"hash_algo 只能是 sha256 或 md5", {"key": key})
        return value
    if key == "keep_mode":
        if value not in KEEP_MODES:
            _err(u"keep_mode 只能是 latest/earliest/pinned", {"key": key})
        return value
    if key == "pinned_path":
        return _check_str(key, value)
    if key == "scan_mode":
        if value not in SCAN_MODES:
            _err(u"scan_mode 只能是 exact 或 similar", {"key": key})
        return value
    if key == "similarity_threshold":
        return _check_int(key, value, 0, 100)
    if key in ("similarity_extensions", "similarity_exclude_extensions"):
        return _check_ext_list(key, value)
    if key == "similarity_concurrency":
        return _check_int(key, value, 1, 4)
    if key == "similarity_cache":
        return _check_bool(key, value)
    if key == "similarity_max_file_mb":
        return _check_int(key, value, 1, 1024)
    if key == "similarity_allow_binary":
        return _check_bool(key, value)
    if key in ("video_similarity_enabled", "video_cache"):
        return _check_bool(key, value)
    if key == "video_extensions":
        return _check_ext_list(key, value)
    if key == "video_sample_frames":
        return _check_int(key, value, config.VIDEO_MIN_SAMPLE_FRAMES,
                          config.VIDEO_MAX_SAMPLE_FRAMES)
    if key == "video_max_sample_interval_sec":
        return _check_int(key, value, 60, 600)
    if key == "video_similarity_threshold":
        return _check_int(key, value, 0, 100)
    if key == "video_concurrency":
        return _check_int(key, value, 1, 2)
    if key == "ffmpeg_path":
        # 空=自动探测（内置/环境变量/PATH）；非空必须是存在且可执行的绝对路径。
        text = _check_str(key, value, max_len=512).strip()
        if not text:
            return ""
        if not os.path.isabs(text):
            _err(u"设置 ffmpeg_path 必须是绝对路径", {"key": key})
        if not os.path.isfile(text):
            _err(u"设置 ffmpeg_path 指向的文件不存在", {"key": key})
        if not os.access(text, os.X_OK):
            _err(u"设置 ffmpeg_path 指向的文件不可执行", {"key": key})
        return text
    if key == "video_hwaccel":
        if value not in config.VIDEO_HWACCEL_MODES:
            _err(u"video_hwaccel 只能是 auto/off/vaapi/qsv/nvdec/videotoolbox",
                 {"key": key})
        return value
    if key == "video_max_file_mb":
        return _check_int(key, value, 1, 102400)
    if key == "video_timeout_sec":
        return _check_int(key, value, 30, 1800)
    if key == "video_asr_enabled":
        return _check_bool(key, value)
    if key == "video_asr_command":
        # 命令模板（{audio}/{outdir}/{stem} 占位符）；只做基础字符串校验，
        # 实际执行使用 shlex.split + 参数数组，绝不经过 shell。
        return _check_str(key, value, max_len=4096)
    if key == "video_asr_weight":
        return _check_float(key, value, 0.0, 0.5)
    if key == "video_asr_timeout_sec":
        return _check_int(key, value, 30, 1800)
    if key == "video_asr_max_sec":
        return _check_int(key, value, 30, 3600)
    if key in ("subtitle_enabled", "subtitle_translate_enabled"):
        return _check_bool(key, value)
    if key in ("subtitle_asr_base_url", "subtitle_llm_base_url"):
        return _check_base_url(key, value)
    if key in ("subtitle_asr_api_key", "subtitle_llm_api_key"):
        return _check_str(key, value, max_len=4096).strip()
    if key == "subtitle_asr_model":
        return _check_model_name(key, value)
    if key == "subtitle_asr_language":
        return _check_language(key, value, allow_auto=True)
    if key == "subtitle_asr_timeout_sec":
        return _check_int(key, value, 30, 1800)
    if key == "subtitle_asr_chunk_sec":
        return _check_int(key, value, 60, 1800)
    if key == "subtitle_asr_max_audio_mb":
        return _check_int(key, value, 1, 100)
    if key == "subtitle_audio_source":
        if value not in config.SUBTITLE_AUDIO_SOURCES:
            _err(u"subtitle_audio_source 只能是 nas/server", {"key": key})
        return value
    if key == "subtitle_write_vtt":
        return _check_bool(key, value)
    if key == "subtitle_llm_model":
        return _check_model_name(key, value)
    if key == "subtitle_translate_target":
        return _check_language(key, value, allow_auto=False)
    if key == "subtitle_translate_timeout_sec":
        return _check_int(key, value, 30, 900)
    if key == "subtitle_translate_batch_size":
        return _check_int(key, value, 1, 40)
    if key == "subtitle_llm_disable_thinking":
        return _check_bool(key, value)
    if key == "subtitle_output_mode":
        if value not in config.SUBTITLE_OUTPUT_MODES:
            _err(u"subtitle_output_mode 只能是 alongside_video/download_only",
                 {"key": key})
        return value
    if key == "subtitle_skip_existing":
        return _check_bool(key, value)
    if key == "subtitle_job_limit":
        return _check_int(key, value, 20, 1000)
    if key == "subtitle_translate_outputs":
        if value not in config.SUBTITLE_TRANSLATE_OUTPUTS:
            _err(u"subtitle_translate_outputs 只能是 both/target_only/bilingual_only",
                 {"key": key})
        return value
    if key == "subtitle_concurrency":
        return _check_int(key, value, 1, config.SUBTITLE_CONCURRENCY_MAX)
    if key == "skip_symlinks":
        # 无论前端传什么，符号链接一律跳过（安全强制）
        if not isinstance(value, bool):
            _err(u"设置 skip_symlinks 必须是布尔值", {"key": key})
        return True
    if key in ("skip_empty", "skip_hidden", "ignore_case_ext", "recycle_default",
               "create_recycle_dir", "allow_ctime_fallback"):
        return _check_bool(key, value)
    if key in ("include_extensions", "exclude_extensions"):
        return _check_ext_list(key, value)
    if key == "min_size_bytes":
        return _check_int(key, value, 0, 10 ** 15)
    if key == "max_size_bytes":
        return _check_int(key, value, 0, 10 ** 15)
    if key == "quick_sample_kb":
        return _check_int(key, value, 4, 1024)
    if key == "concurrency":
        return _check_int(key, value, 1, 8)
    if key == "hash_rate_mbps":
        return _check_int(key, value, 0, 2000)
    if key == "max_files":
        return _check_int(key, value, 1, config.MAX_FILES_LIMIT)
    _err(u"未知的设置键: %s" % key, {"unknown_keys": [key]})


def _check_size_range(min_value, max_value):
    if max_value != 0 and min_value > max_value:
        _err(u"min_size_bytes 不能大于 max_size_bytes",
             {"min_size_bytes": min_value, "max_size_bytes": max_value})


def validate_patch(payload):
    """校验部分设置（PUT /api/settings、扫描 options 覆盖项）。"""
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        _err(u"settings 必须是 JSON 对象")
    unknown = sorted([str(k) for k in payload.keys() if k not in DEFAULT_SETTINGS])
    if unknown:
        raise ApiError("INVALID_ARGUMENT", u"未知的设置键: %s" % ", ".join(unknown),
                       details={"unknown_keys": unknown})
    out = {}
    for key in SETTINGS_KEYS:
        if key in payload:
            out[key] = _normalize_value(key, payload[key])
    if "min_size_bytes" in out and "max_size_bytes" in out:
        _check_size_range(out["min_size_bytes"], out["max_size_bytes"])
    return out


def validate_effective(settings):
    """校验并返回完整设置（用于磁盘加载 / 默认值补齐）。"""
    if not isinstance(settings, dict):
        settings = {}
    out = default_settings()
    for key in SETTINGS_KEYS:
        if key in settings:
            out[key] = _normalize_value(key, settings[key])
    _check_size_range(out["min_size_bytes"], out["max_size_bytes"])
    out["skip_symlinks"] = True
    return out


def merge_settings(current, patch):
    """在已持久化设置上应用一次校验过的补丁。"""
    base = validate_effective(current)
    validated = validate_patch(patch)
    merged = dict(base)
    merged.update(validated)
    _check_size_range(merged["min_size_bytes"], merged["max_size_bytes"])
    return merged


class SettingsManager(object):
    """线程安全的设置读写 + 落盘。"""

    def __init__(self, store):
        self._store = store
        self._lock = threading.RLock()
        self._settings = self._load()

    def _load(self):
        raw = None
        try:
            raw = self._store.load_settings()
        except Exception:
            raw = None
        if not raw:
            return default_settings()
        try:
            return validate_effective(raw)
        except ApiError:
            # 磁盘上的设置被破坏时回到默认值，避免后端无法启动
            return default_settings()

    def get(self):
        with self._lock:
            return copy.deepcopy(self._settings)

    def update(self, patch):
        """更新设置；空密钥保持原值，clear_*_api_key=true 才清空密钥。"""
        with self._lock:
            clean, clears = prepare_settings_patch(patch or {})
            merged = merge_settings(self._settings, clean or {})
            for key in clears:
                merged[key] = ""
            self._store.save_settings(merged)
            self._settings = merged
            return copy.deepcopy(self._settings)

    def reset(self):
        with self._lock:
            defaults = default_settings()
            self._store.save_settings(defaults)
            self._settings = defaults
            return copy.deepcopy(defaults)
