# -*- coding: utf-8 -*-
"""扫描/删除设置：默认值、白名单校验、持久化。"""

from __future__ import absolute_import

import copy
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
    if key == "video_hwaccel":
        if value not in config.VIDEO_HWACCEL_MODES:
            _err(u"video_hwaccel 只能是 auto 或 off", {"key": key})
        return value
    if key == "video_max_file_mb":
        return _check_int(key, value, 1, 102400)
    if key == "video_timeout_sec":
        return _check_int(key, value, 30, 1800)
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
        with self._lock:
            merged = merge_settings(self._settings, patch or {})
            self._store.save_settings(merged)
            self._settings = merged
            return copy.deepcopy(self._settings)

    def reset(self):
        with self._lock:
            defaults = default_settings()
            self._store.save_settings(defaults)
            self._settings = defaults
            return copy.deepcopy(defaults)
