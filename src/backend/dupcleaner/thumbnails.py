# -*- coding: utf-8 -*-
"""预览增强（v0044）：视频缩略图 / 媒体信息 / 时间点列表。

- 缩略图：FFmpeg 抽 1 帧 → JPEG（默认 320px 宽），落在
  `<var>/cache/thumbs/<sha1(path|size|mtime|t|w)>.jpg`；同文件同时间点重复请求
  直接命中缓存（不重复解码）。缓存按 LRU（条数 + 总字节）裁剪。
- 媒体信息：`ffmpeg -i` 解析时长/视频轨/音轨/字幕轨，内存缓存 200 条；
  用于并排对比时判断“不同压制版本”。
- 只读：绝不修改用户文件；只对共享文件夹内的普通文件工作（由调用方先解析路径）。
"""

from __future__ import absolute_import

import hashlib
import os
import threading
import time
import urllib.parse

from . import config, fsutil, video_similarity
from .errors import ApiError

# 默认时间点（秒）：未知时长时的兜底，仍会按视频实际时长裁剪
DEFAULT_TIMESTAMPS = (5.0, 30.0, 60.0, 180.0, 300.0, 600.0,
                      900.0, 1200.0, 1800.0, 2400.0, 3000.0, 3600.0)


class ThumbnailService(object):
    def __init__(self, runtime_config, settings_provider=None,
                 ffmpeg_factory=None):
        self.config = runtime_config
        self._settings = settings_provider or (lambda: {})
        self._ffmpeg_factory = ffmpeg_factory or (
            lambda: video_similarity.FFmpegTool(
                settings_provider=self._settings))
        self._ffmpeg = None
        self._lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._semaphore = threading.BoundedSemaphore(2)
        self._media_cache = {}
        self._media_cache_order = []
        self._writes_since_prune = 0

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def cache_dir(self):
        return os.path.join(self.config.data_dir,
                            config.THUMB_CACHE_SUBDIR.replace("/", os.sep))

    def _ffmpeg_tool(self):
        with self._lock:
            if self._ffmpeg is None:
                self._ffmpeg = self._ffmpeg_factory()
            return self._ffmpeg

    def identity_key(self, path):
        identity = fsutil.file_identity(path)
        if identity is None:
            raise ApiError("NOT_FOUND", u"文件不存在或不是普通文件")
        return identity

    def _thumb_path(self, path, identity, timestamp, width):
        raw = "%s|%s|%s|%.3f|%d" % (
            path, identity.get("size"), identity.get("mtime_ns"),
            float(timestamp), int(width))
        digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()
        return os.path.join(self.cache_dir(), digest[:32] + ".jpg")

    def _timeout(self):
        try:
            settings = self._settings() or {}
        except Exception:
            settings = {}
        try:
            base = float(settings.get("video_timeout_sec") or 180)
        except (TypeError, ValueError):
            base = 180.0
        return max(10.0, min(config.THUMB_TIMEOUT_SEC, base))

    def normalize_width(self, width):
        try:
            value = int(width)
        except (TypeError, ValueError):
            value = config.THUMB_DEFAULT_WIDTH
        return max(64, min(config.THUMB_MAX_WIDTH, value))

    def normalize_timestamp(self, timestamp, duration=None):
        try:
            value = float(timestamp)
        except (TypeError, ValueError):
            value = 0.0
        if value < 0:
            value = 0.0
        if duration and duration > 0:
            value = min(value, max(0.0, float(duration) - 0.5))
        return round(value, 3)

    # ------------------------------------------------------------------
    # 缩略图
    # ------------------------------------------------------------------
    def thumbnail(self, path, timestamp=0.0, width=None, duration=None):
        width = self.normalize_width(width)
        identity = self.identity_key(path)
        timestamp = self.normalize_timestamp(timestamp, duration)
        target = self._thumb_path(path, identity, timestamp, width)
        with self._lock:
            cached = os.path.isfile(target) and os.path.getsize(target) > 0
        if cached:
            return {"path": target, "cached": True, "width": width,
                    "timestamp": timestamp}
        directory = os.path.dirname(target)
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise ApiError("INTERNAL_ERROR", u"创建缩略图缓存目录失败: %s" % exc)
        tool = self._ffmpeg_tool()
        if not tool.available():
            raise ApiError("FFMPEG_UNAVAILABLE", u"未找到 FFmpeg，无法生成缩略图")
        temp_path = target + ".tmp-%d" % os.getpid()
        acquired = self._semaphore.acquire(timeout=20)
        if not acquired:
            raise ApiError("PREVIEW_BUSY", u"缩略图生成繁忙，请稍后重试")
        try:
            tool.extract_thumbnail(path, temp_path, timestamp, width,
                                   timeout=self._timeout())
            if not os.path.isfile(temp_path) or os.path.getsize(temp_path) <= 0:
                raise ApiError("PICTURE_FAILED", u"缩略图生成失败（空文件）")
            with self._write_lock:
                os.replace(temp_path, target)
                self._writes_since_prune += 1
                should_prune = self._writes_since_prune >= 50
                if should_prune:
                    self._writes_since_prune = 0
        except video_similarity.VideoSkip as exc:
            raise ApiError("PREVIEW_UNSUPPORTED",
                           u"无法生成缩略图：%s" % (exc.detail or exc.reason))
        except video_similarity.VideoTimeout:
            raise ApiError("PROVIDER_TIMEOUT", u"生成缩略图超时")
        finally:
            try:
                if os.path.isfile(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            self._semaphore.release()
        if should_prune:
            try:
                self.prune()
            except Exception:  # pragma: no cover - 裁剪失败不影响本次结果
                pass
        return {"path": target, "cached": False, "width": width,
                "timestamp": timestamp}

    # ------------------------------------------------------------------
    # 媒体信息
    # ------------------------------------------------------------------
    def media_info(self, path):
        identity = self.identity_key(path)
        key = (path, identity.get("size"), identity.get("mtime_ns"))
        with self._lock:
            cached = self._media_cache.get(key)
        if cached is not None:
            return dict(cached)
        tool = self._ffmpeg_tool()
        if not tool.available():
            raise ApiError("FFMPEG_UNAVAILABLE", u"未找到 FFmpeg，无法读取媒体信息")
        try:
            probe = tool.probe_media(path, timeout=self._timeout())
        except video_similarity.VideoSkip as exc:
            raise ApiError("PREVIEW_UNSUPPORTED",
                           u"无法读取媒体信息：%s" % (exc.detail or exc.reason))
        except video_similarity.VideoTimeout:
            raise ApiError("PROVIDER_TIMEOUT", u"读取媒体信息超时")
        video_streams = probe.get("video_streams") or []
        audio_streams = probe.get("audio_streams") or []
        subtitle_streams = probe.get("subtitle_streams") or []
        info = {
            "duration": (round(float(probe["duration"]), 3)
                         if probe.get("duration") else None),
            "size": identity.get("size"),
            "has_audio": bool(probe.get("has_audio")),
            "has_subtitle": bool(probe.get("has_subtitle")),
            "video_stream_count": len(video_streams),
            "audio_stream_count": len(audio_streams),
            "subtitle_stream_count": len(subtitle_streams),
            "video": (video_streams[0] if video_streams else None),
            "audio_streams": audio_streams,
            "subtitle_streams": subtitle_streams,
        }
        with self._lock:
            if key not in self._media_cache:
                self._media_cache_order.append(key)
            self._media_cache[key] = info
            while len(self._media_cache_order) > config.MEDIA_INFO_CACHE_MAX:
                oldest = self._media_cache_order.pop(0)
                self._media_cache.pop(oldest, None)
        return dict(info)

    def timestamps_for(self, media, count):
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = config.THUMB_COUNT_DEFAULT
        count = max(1, min(config.THUMB_COUNT_MAX, count))
        duration = (media or {}).get("duration")
        if duration and duration > 1:
            step = float(duration) / float(count + 1)
            return [round(step * (index + 1), 3) for index in range(count)]
        selected = []
        for index, value in enumerate(DEFAULT_TIMESTAMPS):
            if index >= count:
                break
            selected.append(value)
        return selected or [0.0]

    def thumb_url(self, path, timestamp, width):
        query = urllib.parse.urlencode({
            "path": path,
            "t": "%.2f" % float(timestamp),
            "w": str(self.normalize_width(width)),
        })
        return "/api/preview/thumb?" + query

    def preview_plan(self, path, count=None, width=None):
        media = self.media_info(path)
        width = self.normalize_width(width)
        timestamps = self.timestamps_for(media, count
                                         if count is not None
                                         else config.THUMB_COUNT_DEFAULT)
        return {
            "media": media,
            "width": width,
            "timestamps": timestamps,
            "thumbs": [{"t": item,
                        "url": self.thumb_url(path, item, width)}
                       for item in timestamps],
        }

    # ------------------------------------------------------------------
    # 缓存维护
    # ------------------------------------------------------------------
    def prune(self):
        directory = self.cache_dir()
        try:
            names = os.listdir(directory)
        except OSError:
            return {"deleted": 0, "remaining": 0}
        entries = []
        total = 0
        for name in names:
            if not name.endswith(".jpg"):
                continue
            path = os.path.join(directory, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            entries.append((info.st_mtime, info.st_size, path))
            total += int(info.st_size)
        entries.sort()
        deleted = 0
        while entries and (len(entries) > config.THUMB_MAX_ENTRIES
                           or total > config.THUMB_MAX_BYTES):
            _mtime, size, path = entries.pop(0)
            try:
                os.remove(path)
                deleted += 1
                total -= size
            except OSError:
                continue
        return {"deleted": deleted, "remaining": len(entries)}

    def stats(self):
        directory = self.cache_dir()
        count = 0
        total = 0
        try:
            for name in os.listdir(directory):
                if not name.endswith(".jpg"):
                    continue
                try:
                    size = os.path.getsize(os.path.join(directory, name))
                except OSError:
                    continue
                count += 1
                total += int(size)
        except OSError:
            pass
        return {"entries": count, "bytes": total,
                "max_entries": config.THUMB_MAX_ENTRIES,
                "max_bytes": config.THUMB_MAX_BYTES,
                "path": directory}
