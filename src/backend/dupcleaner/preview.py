# -*- coding: utf-8 -*-
"""文件预览：类型识别（图片/文本/PDF/音视频），文本截断 1MB。"""

from __future__ import absolute_import

import os

from . import fsutil
from .errors import ApiError, invalid, not_found, path_not_allowed

TEXT_LIMIT_BYTES = 1024 * 1024
SNIFF_BYTES = 1024

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".tif", ".tiff",
}
AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".wma", ".ape", ".opus",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg",
    ".3gp", ".ts", ".rmvb",
}
TEXT_EXTENSIONS = {
    ".txt", ".log", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml",
    ".yml", ".ini", ".conf", ".cfg", ".py", ".js", ".mjs", ".css", ".html", ".htm",
    ".sh", ".bash", ".bat", ".ps1", ".sql", ".c", ".h", ".cpp", ".hpp", ".java",
    ".go", ".rs", ".rb", ".php", ".pl", ".lua", ".toml", ".properties", ".gitignore",
    ".srt", ".ass", ".vtt", ".nfo", ".cfg", ".env",
}
TEXT_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".svg": "image/svg+xml",
    ".csv": "text/csv; charset=utf-8",
}

MIME_BY_EXTENSION = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".ico": "image/x-icon",
    ".pdf": "application/pdf",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".wma": "audio/x-ms-wma", ".ape": "audio/x-ape", ".opus": "audio/opus",
    ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".mov": "video/quicktime", ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv",
    ".webm": "video/webm", ".m4v": "video/mp4", ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg", ".3gp": "video/3gpp", ".ts": "video/mp2t",
    ".rmvb": "video/x-realmedia",
}


def _sniff(path):
    """按文件头识别图片/PDF/文本（无扩展名或扩展名不可靠时）。"""
    try:
        with open(path, "rb") as handle:
            head = handle.read(SNIFF_BYTES)
    except OSError:
        return None
    if not head:
        return "text"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if head.startswith(b"\xff\xd8\xff"):
        return "image"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image"
    if head.startswith(b"BM"):
        return "image"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head[4:8] == b"ftyp":
        return "video"
    if b"\x00" not in head:
        return "text"
    return None


class PreviewService(object):
    def __init__(self, share_service):
        self.shares = share_service

    def resolve(self, path):
        if not isinstance(path, str) or not path.strip():
            raise invalid(u"path 参数非法")
        normalized = fsutil.check_path_safety(path)
        share = self.shares.find_share(normalized)
        if share is None:
            raise path_not_allowed(u"只允许预览共享文件夹内的文件")
        if fsutil.has_symlink_component(normalized, share["path"]):
            raise path_not_allowed(u"不允许预览符号链接文件")
        if not fsutil.is_regular_file(normalized):
            raise not_found(u"文件不存在或不是普通文件")
        try:
            size = os.path.getsize(normalized)
        except OSError:
            raise not_found(u"文件不存在")
        name = os.path.basename(normalized)
        extension = fsutil.extension_of(name).lower()
        kind = None
        content_type = None
        if extension in IMAGE_EXTENSIONS:
            kind = "image"
        elif extension in AUDIO_EXTENSIONS:
            kind = "audio"
        elif extension in VIDEO_EXTENSIONS:
            kind = "video"
        elif extension in TEXT_EXTENSIONS:
            kind = "text"
        elif extension == ".pdf":
            kind = "pdf"
        else:
            sniffed = _sniff(normalized)
            if sniffed == "image":
                kind = "image"
            elif sniffed == "pdf":
                kind = "pdf"
            elif sniffed == "text":
                kind = "text"
            elif sniffed == "video":
                kind = "video"
        if kind is None:
            raise ApiError("PREVIEW_UNSUPPORTED", u"该文件类型不支持预览",
                           details={"name": name})
        if kind == "text":
            content_type = TEXT_CONTENT_TYPES.get(extension, "text/plain; charset=utf-8")
        else:
            content_type = MIME_BY_EXTENSION.get(extension)
            if not content_type:
                content_type = {
                    "image": "application/octet-stream",
                    "audio": "application/octet-stream",
                    "video": "application/octet-stream",
                    "pdf": "application/pdf",
                }.get(kind, "application/octet-stream")
        return {
            "path": normalized,
            "name": name,
            "size": size,
            "kind": kind,
            "content_type": content_type,
            "extension": extension,
        }

    def read_text(self, info):
        """读取文本预览内容（最多 1MB），返回 (bytes, truncated)。"""
        limit = TEXT_LIMIT_BYTES
        try:
            with open(info["path"], "rb") as handle:
                data = handle.read(limit + 1)
        except OSError as exc:
            raise ApiError("INVALID_PATH", u"读取文件失败: %s" % exc)
        truncated = len(data) > limit
        if truncated:
            data = data[:limit]
        return data, truncated
