# -*- coding: utf-8 -*-
"""字幕目录监控（v0048）：监测目录内新增视频 → 自动加入字幕队列。

- 设置：`subtitle_watch_enabled`、`subtitle_watch_dirs`、`subtitle_watch_interval_sec`；
- 轮询（标准库无 inotify）：每 30 秒检查一次是否到扫描间隔；
- 只处理“设置里 video_extensions 内的视频”，按 (size, mtime_ns) 记录状态，已入队/处理过的不重复提交；
- 已启用 `skip_existing` 时，已有字幕的任务由字幕服务标记 skipped，不会重复消耗云端额度；
- 任务以 `user="watcher"` 提交，`source_watch=true` 供 UI 标识；翻译开关使用当前
  `subtitle_translate_enabled`，音轨默认使用 `subtitle_audio_track` / `..._language`。
- 状态持久化 `<var>/subtitle_watch_state.json`（最多 5000 条，超限按时间裁剪）。
"""

from __future__ import absolute_import

import json
import logging
import os
import threading
import time

from . import config, fsutil, timeutil

logger = logging.getLogger("dupcleaner.subtitle_watch")

WATCH_USER = "watcher"
TICK_SEC = 30
MAX_ENQUEUE_PER_SCAN = 20
MAX_STATE_ENTRIES = config.SUBTITLE_WATCH_STATE_MAX


class SubtitleWatchService(object):
    def __init__(self, runtime_config, store, settings_provider,
                 subtitle_service=None, clock=None):
        self.config = runtime_config
        self.store = store
        self.settings = settings_provider
        self.subtitles = subtitle_service
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._state = self._load_state()
        self._last_scan = 0.0
        self._last_result = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="dc-subtitle-watch", daemon=True)
        self._thread.start()

    def stop(self, timeout=3.0):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # pragma: no cover - 兜底
                logger.warning(u"字幕目录监控异常", exc_info=True)
            self._stop.wait(TICK_SEC)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def _state_path(self):
        return os.path.join(self.config.data_dir,
                            config.SUBTITLE_WATCH_STATE_FILE)

    def _load_state(self):
        try:
            with open(self._state_path(), "rb") as handle:
                data = json.loads(handle.read().decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            return {"files": {}}
        if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
            return {"files": {}}
        return data

    def _save_state(self):
        with self._lock:
            data = dict(self._state)
        path = self._state_path()
        directory = os.path.dirname(path)
        try:
            os.makedirs(directory, exist_ok=True)
            temporary = path + ".tmp"
            with open(temporary, "wb") as handle:
                handle.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            os.replace(temporary, path)
        except OSError:  # pragma: no cover
            logger.warning(u"保存字幕监控状态失败", exc_info=True)

    def _settings(self):
        try:
            return self.settings() or {}
        except Exception:  # pragma: no cover
            return {}

    def status(self):
        settings = self._settings()
        with self._lock:
            files = len(self._state.get("files") or {})
        return {
            "enabled": settings.get("subtitle_watch_enabled") is True,
            "dirs": list(settings.get("subtitle_watch_dirs") or []),
            "interval_sec": int(settings.get("subtitle_watch_interval_sec")
                                or config.SUBTITLE_WATCH_INTERVAL_DEFAULT),
            "last_scan_at": timeutil.iso_local(self._last_scan)
                            if self._last_scan else None,
            "last_result": dict(self._last_result or {}),
            "watched_files": files,
            "state_file": config.SUBTITLE_WATCH_STATE_FILE,
            "user": WATCH_USER,
        }

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------
    def _tick(self):
        settings = self._settings()
        if settings.get("subtitle_watch_enabled") is not True:
            return
        interval = int(settings.get("subtitle_watch_interval_sec")
                       or config.SUBTITLE_WATCH_INTERVAL_DEFAULT)
        now = self._clock()
        if now - self._last_scan < max(config.SUBTITLE_WATCH_MIN_INTERVAL,
                                       min(interval, 86400)):
            return
        self.scan_once()

    def scan_once(self, force=False):
        """执行一次监控扫描并自动提交字幕任务；返回结果摘要。

        force=True 时即使监控开关关闭也执行一次（供设置页“立即扫描”按钮验证）。
        """
        settings = self._settings()
        result = {"enqueued": 0, "skipped": 0, "batches": 0,
                  "errors": [], "scan_at": timeutil.iso_local(self._clock())}
        self._last_scan = self._clock()
        if settings.get("subtitle_watch_enabled") is not True and not force:
            result["errors"].append(u"字幕目录监控未启用")
            self._last_result = result
            return result
        if self.subtitles is None:
            result["errors"].append(u"字幕服务不可用")
            self._last_result = result
            return result
        if not settings.get("subtitle_enabled"):
            result["errors"].append(u"云端字幕功能未启用，监控不提交任务")
            self._last_result = result
            return result
        directories = [item for item in (settings.get("subtitle_watch_dirs") or [])
                       if isinstance(item, str) and item.strip()]
        if not directories:
            result["errors"].append(u"未配置监控目录")
            self._last_result = result
            return result
        extensions = set(
            str(item).lower() for item in
            (settings.get("video_extensions")
             or config.DEFAULT_VIDEO_EXTENSIONS))
        active = self.subtitles._active_path_keys()
        with self._lock:
            state_files = dict(self._state.get("files") or {})
        pending = []
        for directory in directories:
            for path, size, mtime_ns in self._walk(directory, extensions):
                key = fsutil.path_key(path)
                if key in active:
                    result["skipped"] += 1
                    continue
                recorded = state_files.get(key)
                if (isinstance(recorded, dict)
                        and int(recorded.get("size") or -1) == int(size)
                        and int(recorded.get("mtime_ns") or -1) == int(mtime_ns)):
                    result["skipped"] += 1
                    continue
                pending.append((key, path, size, mtime_ns))
                if len(pending) >= MAX_ENQUEUE_PER_SCAN:
                    break
            if len(pending) >= MAX_ENQUEUE_PER_SCAN:
                break
        if not pending:
            self._last_result = result
            return result
        paths = [item[1] for item in pending]
        payload = {
            "paths": paths,
            "confirm_cloud": True,
            "source_watch": True,
            "translate": settings.get("subtitle_translate_enabled") is True,
            "audio_track": settings.get("subtitle_audio_track") or None,
            "audio_track_language": settings.get("subtitle_audio_track_language") or None,
            "output_mode": settings.get("subtitle_output_mode"),
        }
        try:
            snapshot = self.subtitles.create_batch(WATCH_USER, payload)
            result["batches"] = 1
            submitted = snapshot.get("jobs") if isinstance(snapshot, dict) else None
            if isinstance(submitted, int):
                result["enqueued"] = submitted
            elif isinstance(submitted, (list, tuple)):
                result["enqueued"] = len(submitted)
            else:
                result["enqueued"] = len(paths)
        except Exception as exc:
            message = getattr(exc, "message", str(exc))
            result["errors"].append(message)
            self.store.audit("subtitle_watch_failed", WATCH_USER,
                             {"error": message})
            self._last_result = result
            return result
        now = self._clock()
        with self._lock:
            for key, path, size, mtime_ns in pending:
                self._state.setdefault("files", {})[key] = {
                    "path": path, "size": int(size),
                    "mtime_ns": int(mtime_ns), "queued_epoch": now,
                }
            self._prune_state_locked()
        self._save_state()
        self.store.audit("subtitle_watch_enqueue", WATCH_USER, {
            "count": len(pending), "paths": paths[:20]})
        self._last_result = result
        return result

    def _walk(self, directory, extensions):
        try:
            normalized = fsutil.check_path_safety(directory)
        except Exception:
            return
        stack = [normalized]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                name = entry.name
                if name.startswith("."):
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    extension = os.path.splitext(name)[1].lower()
                    if extension not in extensions:
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.st_size <= 0:
                    continue
                yield entry.path, stat.st_size, stat.st_mtime_ns

    def _prune_state_locked(self):
        files = self._state.get("files") or {}
        if len(files) <= MAX_STATE_ENTRIES:
            return
        ordered = sorted(files.items(),
                         key=lambda pair: -(pair[1] or {}).get("queued_epoch", 0))
        self._state["files"] = dict(ordered[:MAX_STATE_ENTRIES])
