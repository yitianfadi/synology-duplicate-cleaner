# -*- coding: utf-8 -*-
"""共享文件夹浏览（结果页/选择器使用）。"""

from __future__ import absolute_import

import os

from . import timeutil
from .errors import ApiError, invalid, not_found, path_not_allowed
from .fsutil import (check_path_safety, has_symlink_component, is_safe_dir_name,
                     is_under, is_system_name, path_key)
from . import fsutil


class BrowseService(object):
    def __init__(self, runtime_config, share_service, settings_provider=None):
        self.config = runtime_config
        self.shares = share_service
        self._settings_provider = settings_provider

    def _allow_ctime_fallback(self):
        if self._settings_provider is None:
            return False
        try:
            return bool(self._settings_provider().get("allow_ctime_fallback", False))
        except Exception:
            return False

    def shares_payload(self, dsm_sid=None, force=False):
        result = []
        for share in self.shares.list_shares(dsm_sid=dsm_sid, force=force):
            result.append({
                "name": share["name"],
                "path": share["path"],
                "volume": share.get("volume") or "",
                "filesystem": share.get("filesystem") or "unknown",
                "is_usb": bool(share.get("is_usb")),
                "is_encrypted": bool(share.get("is_encrypted")),
                "is_readable": bool(share.get("is_readable", True)),
                "is_writable": bool(share.get("is_writable")),
            })
        return result

    def browse(self, path, include_files=False, dsm_sid=None):
        if path is None:
            path = ""
        if not isinstance(path, str):
            raise invalid(u"path 参数非法")
        if not path.strip():
            return {
                "path": "",
                "parent": None,
                "breadcrumbs": [],
                "shares": self.shares_payload(dsm_sid=dsm_sid),
                "entries": [],
            }

        normalized = check_path_safety(path)
        share = self.shares.validate_root(normalized)
        if not os.path.isdir(normalized):
            raise invalid(u"目录不存在: %s" % normalized)

        allow_ctime = self._allow_ctime_fallback()
        entries = []
        try:
            with os.scandir(normalized) as iterator:
                raw_entries = list(iterator)
        except PermissionError:
            raise ApiError("PATH_NOT_ALLOWED",
                           u"没有权限访问该目录；请给系统内部用户 duplicatecleaner 授予读取权限: %s" % normalized)
        except OSError as exc:
            raise ApiError("INVALID_PATH", u"无法读取目录: %s" % exc)

        for entry in raw_entries:
            name = entry.name
            is_symlink = entry.is_symlink()
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if not is_dir and not include_files:
                continue
            reason = None
            if is_symlink:
                reason = "symlink"
            elif not is_dir and not fsutil.is_regular_file(entry.path):
                reason = "other"
            elif not is_safe_dir_name(name):
                reason = "hidden" if fsutil.is_hidden_name(name) else "system"
            elif not os.access(entry.path, os.R_OK):
                reason = "permission"

            size = 0
            modified_at = None
            if not is_dir:
                try:
                    st = entry.stat(follow_symlinks=False)
                    size = int(st.st_size)
                    modified_at = timeutil.iso_local(st.st_mtime)
                except OSError:
                    reason = reason or "permission"
            created_at = None
            if not is_dir and reason is None:
                created = timeutil.read_creation_time(entry.path, allow_ctime_fallback=allow_ctime)
                created_at = timeutil.iso_local(created["created_ts"])
            entries.append({
                "name": name,
                "path": entry.path,
                "type": "dir" if is_dir else "file",
                "size": size,
                "created_at": created_at,
                "modified_at": modified_at,
                "hidden": fsutil.is_hidden_name(name),
                "is_symlink": bool(is_symlink),
                "excluded": reason is not None,
                "exclude_reason": reason,
            })

        entries.sort(key=lambda item: (0 if item["type"] == "dir" else 1,
                                       item["name"].lower()))
        parent = os.path.dirname(normalized)
        if not parent or parent == normalized:
            parent = None
        elif not os.path.isdir(parent):
            parent = None
        return {
            "path": normalized,
            "parent": parent,
            "breadcrumbs": self._breadcrumbs(normalized, share["path"]),
            "entries": entries,
        }

    @staticmethod
    def _breadcrumbs(path, share_root):
        try:
            rel = os.path.relpath(path, share_root)
        except ValueError:
            return [{"name": os.path.basename(path), "path": path}]
        if rel in (".", ""):
            return [{"name": os.path.basename(share_root) or share_root,
                     "path": share_root}]
        crumbs = [{"name": os.path.basename(share_root) or share_root,
                   "path": share_root}]
        current = share_root
        for part in rel.split(os.sep):
            if not part:
                continue
            current = os.path.join(current, part)
            crumbs.append({"name": part, "path": current})
        return crumbs
