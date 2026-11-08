# -*- coding: utf-8 -*-
"""删除白名单（附属文件保护，v0044）。

设计要点：
- **删除侧终局防线**：`DeletionManager` 在生成计划与真正删除前都会用**当前设置**
  重新计算保护状态；扫描期的 `protected` 标记只用于 UI（不可勾选、显示徽标）。
- 规则（命中任一即受保护）：
  1. `protect_dirs`：路径等于或位于给定目录前缀下；
  2. `protect_extensions`：后缀命中（默认仅字幕/元数据，不含图片）；
  3. `protect_name_regex`：basename 命中正则（IGNORECASE）；
  4. `protect_sidecar_files`（默认开）：同目录存在**同源视频**的附属文件
     （同名不同后缀、`X.<lang>.srt`、`X-poster.jpg`、常见固定名如 `poster.jpg`）。
     这一条覆盖海报/剧照，但不影响照片库中与视频无关的图片判重。
- 目录内视频 stem 按目录缓存（一次 `scandir`），避免大批量删除时反复列目录。
"""

from __future__ import absolute_import

import os
import re
import threading

from . import config, fsutil

_KNOWN_SIDECAR_NAMES = config.KNOWN_SIDECAR_NAMES


def _options_video_extensions(options):
    extensions = (options or {}).get("video_extensions")
    if isinstance(extensions, (list, tuple)) and extensions:
        return set(str(item).lower() for item in extensions)
    return set(config.DEFAULT_VIDEO_EXTENSIONS)


def protect_extension_set(options):
    value = (options or {}).get("protect_extensions")
    if not isinstance(value, (list, tuple)):
        value = config.PROTECT_DEFAULT_EXTENSIONS
    result = set()
    for item in value:
        if not isinstance(item, str):
            continue
        extension = item.strip().lower()
        if not extension:
            continue
        if not extension.startswith("."):
            extension = "." + extension
        result.add(extension)
    return result


def protect_dir_list(options):
    value = (options or {}).get("protect_dirs")
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            continue
        result.append(os.path.normpath(item.strip()))
    return result


def protect_regex_list(options):
    value = (options or {}).get("protect_name_regex")
    if not isinstance(value, (list, tuple)):
        return []
    compiled = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            continue
        try:
            compiled.append(re.compile(item.strip(), re.IGNORECASE))
        except re.error:
            continue
    return compiled


class Protector(object):
    """按设置计算文件是否受保护（线程安全：目录缓存带锁）。"""

    def __init__(self, options, video_extensions=None):
        options = options or {}
        self.enabled = bool(options.get("protect_enabled", True))
        self.extensions = protect_extension_set(options)
        self.dirs = protect_dir_list(options)
        self.regexes = protect_regex_list(options)
        self.sidecar = bool(options.get("protect_sidecar_files", True))
        self.video_extensions = video_extensions or _options_video_extensions(options)
        self._lock = threading.Lock()
        self._dir_stems = {}
        self.directory_scans = 0

    # -- 目录缓存 -------------------------------------------------------
    def media_stems(self, directory):
        """返回该目录下视频文件的 stem 集合（一次 scandir，失败返回 None）。"""
        key = fsutil.path_key(directory) if directory else ""
        with self._lock:
            if key in self._dir_stems:
                return self._dir_stems[key]
        stems = None
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError:
            entries = None
        if entries is not None:
            stems = set()
            for entry in entries:
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                name = entry.name
                extension = fsutil.extension_of(name).lower()
                if extension in self.video_extensions:
                    stems.add(os.path.splitext(name)[0])
        with self._lock:
            self._dir_stems[key] = stems
            self.directory_scans += 1
        return stems

    @staticmethod
    def _stem_matches_video(stem, media_stems):
        if not stem or not media_stems:
            return False
        if stem in media_stems:
            return True
        for media_stem in media_stems:
            if not media_stem:
                continue
            for separator in (".", "-", "_", " "):
                if stem.startswith(media_stem + separator):
                    return True
        return False

    def sidecar_state(self, path):
        """判断是否为“同目录同源视频的附属文件”。

        注意：视频文件本身不是自己的附属文件（否则 .mkv 会保护自己）。
        """
        name = os.path.basename(path or "")
        if not name:
            return None
        extension = fsutil.extension_of(name).lower()
        if extension in self.video_extensions:
            return None
        directory = os.path.dirname(path)
        stems = self.media_stems(directory)
        if not stems:
            return None
        if name.lower() in _KNOWN_SIDECAR_NAMES:
            return {"source": "sidecar",
                    "message": u"与同目录视频同名的附属文件（%s）" % name}
        stem = os.path.splitext(name)[0]
        if self._stem_matches_video(stem, stems):
            return {"source": "sidecar",
                    "message": u"与同目录视频同源（%s）" % name}
        return None

    # -- 对外检查 -------------------------------------------------------
    def check(self, path):
        """返回 None（可删除）或 {source, message}。"""
        if not self.enabled or not path:
            return None
        normalized = path
        name = os.path.basename(normalized)
        for prefix in self.dirs:
            if normalized == prefix or fsutil.is_under(normalized, prefix):
                return {"source": "directory",
                        "message": u"文件位于受保护目录（%s）" % prefix}
        extension = fsutil.extension_of(name).lower()
        if extension and extension in self.extensions:
            return {"source": "extension",
                    "message": u"后缀 %s 受保护" % extension}
        for pattern in self.regexes:
            if pattern.search(name):
                return {"source": "name",
                        "message": u"文件名命中保护规则（%s）" % pattern.pattern}
        if self.sidecar:
            state = self.sidecar_state(normalized)
            if state is not None:
                return state
        return None

    def describe(self, state):
        if not state:
            return ""
        label = config.PROTECT_REASON_LABELS.get(state.get("source"), u"受保护")
        message = state.get("message") or ""
        return (u"%s：%s" % (label, message)) if message else label
