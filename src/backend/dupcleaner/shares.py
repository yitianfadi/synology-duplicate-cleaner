# -*- coding: utf-8 -*-
"""共享文件夹枚举与路径白名单。

群晖 API 调用点：
- SYNO.Core.Share（method=list）在有 DSM 会话时优先调用；
- 其次调用 synoshare --enum ADM 命令行；
- 最后回退到 /volume*/ 文件系统探测（开发模式使用 DUP_DEV_ROOTS）。
"""

from __future__ import absolute_import

import glob
import json
import os
import subprocess
import threading
import time

from . import config
from .errors import ApiError, invalid, path_not_allowed
from .fsutil import (check_path_safety, has_symlink_component, is_dir_nofollow,
                     is_regular_file, is_system_name, is_under, norm_path,
                     path_key, writable_dir)


def _decode(data):
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data or ""


def _compute_filesystem(path):
    """从 /proc/mounts 猜测文件系统类型（仅 Linux，失败返回 unknown）。"""
    best = ("", "unknown")
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount_point = parts[1].replace("\\040", " ")
                if path == mount_point or path.startswith(mount_point.rstrip("/") + "/"):
                    if len(mount_point) > len(best[0]):
                        best = (mount_point, parts[2])
    except (OSError, IOError):
        return "unknown"
    return best[1]


class ShareService(object):
    def __init__(self, runtime_config, dsm_client=None, cache_ttl=30.0,
                 clock=None, cli_runner=None, fs_enumerator=None):
        self.config = runtime_config
        self._dsm = dsm_client
        self.cache_ttl = cache_ttl
        self._clock = clock or time.time
        self._cli_runner = cli_runner or self._run_cli
        self._fs_enumerator = fs_enumerator or self._enumerate_filesystem
        self._lock = threading.RLock()
        self._cache = None
        self._cache_at = 0.0

    # ------------------------------------------------------------------
    # 枚举
    # ------------------------------------------------------------------
    def list_shares(self, dsm_sid=None, force=False):
        with self._lock:
            now = self._clock()
            if (not force and self._cache is not None
                    and now - self._cache_at < self.cache_ttl):
                return [dict(item) for item in self._cache]
        shares = self._discover(dsm_sid)
        with self._lock:
            self._cache = shares
            self._cache_at = self._clock()
        return [dict(item) for item in shares]

    def invalidate(self):
        with self._lock:
            self._cache = None
            self._cache_at = 0.0

    def _discover(self, dsm_sid):
        if self.config.dev_mode:
            return self._dev_shares()
        shares = []
        if dsm_sid and self._dsm is not None:
            try:
                shares = self._api_shares(dsm_sid)
            except Exception:
                shares = []
        if not shares:
            try:
                shares = self._cli_shares()
            except Exception:
                shares = []
        if not shares:
            try:
                shares = self._fs_enumerator()
            except Exception:
                shares = []
        return self._normalize(shares)

    def _dev_shares(self):
        shares = []
        used_names = set()
        for index, root in enumerate(self.config.dev_roots):
            root = os.path.abspath(root)
            if not os.path.isdir(root):
                continue
            name = os.path.basename(root.rstrip("\\/")) or ("dev%d" % (index + 1))
            base = name
            suffix = 1
            while name in used_names:
                suffix += 1
                name = "%s-%d" % (base, suffix)
            used_names.add(name)
            shares.append({
                "name": name,
                "path": os.path.normpath(root),
                "volume": os.path.dirname(os.path.normpath(root)) or root,
                "filesystem": "dev",
                "is_usb": False,
                "is_encrypted": False,
                "is_writable": writable_dir(root),
            })
        return shares

    def _api_shares(self, dsm_sid):
        """群晖 API 调用点：SYNO.Core.Share list。"""
        if self._dsm is None:
            return []
        additional = json.dumps(["volume", "is_writable", "is_encrypted", "vol_path"])
        data = self._dsm.call("SYNO.Core.Share", 1, "list",
                              {"shareType": "all", "additional": additional},
                              path="/webapi/entry.cgi", sid=dsm_sid)
        raw = []
        if isinstance(data, dict):
            for key in ("shares", "share", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    raw = value
                    break
                if isinstance(value, dict):
                    for inner in ("shares", "share"):
                        if isinstance(value.get(inner), list):
                            raw = value[inner]
                            break
                    if raw:
                        break
        result = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("share_name")
            if not name:
                continue
            path = self._resolve_share_path(name, item)
            if not path:
                continue
            volume = self._volume_of(path) or os.path.dirname(path)
            result.append({
                "name": str(name),
                "path": os.path.normpath(path),
                "volume": str(volume),
                "filesystem": _compute_filesystem(path),
                "is_usb": bool(item.get("is_usb") or str(name).startswith("usbshare")),
                "is_encrypted": bool(item.get("is_encrypted")),
                "is_writable": bool(item.get("is_writable", True)),
            })
        return result

    def _resolve_share_path(self, name, item):
        """从 SYNO.Core.Share 条目中解析真实的共享文件夹路径。

        DSM 某些版本/接口组合返回的 path/vol_path 可能是存储空间根（如
        /volume2），而不是 /volume2/<share>。若直接采用，UI 会显示一个
        无法选择的“存储空间根”，并触发“不能选择存储空间根目录”错误。
        这里优先用 name 在各卷下拼出真实共享根，再回退到非卷根候选路径。
        """
        candidates = []
        for key in ("path", "real_path", "vol_path"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        volume_candidates = []
        for key in ("volume", "vol_path"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                value = value.strip()
                if config.VOLUME_ROOT_RE.match(value.replace("\\", "/")):
                    volume_candidates.append(value)
        for volume in volume_candidates:
            candidate = os.path.join(volume, name)
            if os.path.isdir(candidate):
                candidates.insert(0, candidate)
        # 卷根本身绝不能作为共享文件夹路径；只接受 basename 与共享名一致的路径。
        for candidate in candidates:
            posix = candidate.replace("\\", "/")
            if config.VOLUME_ROOT_RE.match(posix):
                continue
            base = os.path.basename(os.path.normpath(candidate))
            if base != name:
                continue
            if os.path.isdir(candidate):
                return os.path.normpath(candidate)
        return self._guess_share_path(name)

    def _cli_shares(self):
        """群晖 CLI 兜底：synoshare --enum ADM。"""
        result = self._cli_runner()
        if not result:
            return []
        shares = []
        for name in result:
            if not name or is_system_name(name):
                continue
            path = self._guess_share_path(name)
            if not path:
                continue
            shares.append({
                "name": name,
                "path": os.path.normpath(path),
                "volume": self._volume_of(path),
                "filesystem": _compute_filesystem(path),
                "is_usb": name.startswith("usbshare"),
                "is_encrypted": False,
                "is_writable": writable_dir(path),
            })
        return shares

    def _run_cli(self):
        try:
            proc = subprocess.run(["synoshare", "--enum", "ADM"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=5.0)
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0:
            return []
        names = []
        for line in _decode(proc.stdout).splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if not parts:
                continue
            name = parts[0]
            if name in ("Share", "Name", "ADM", "---"):
                continue
            names.append(name)
        return names

    def _enumerate_filesystem(self):
        shares = []
        for volume in sorted(glob.glob("/volume*")):
            if not os.path.isdir(volume):
                continue
            try:
                entries = list(os.scandir(volume))
            except OSError:
                continue
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if is_system_name(entry.name) or entry.name.startswith("."):
                    continue
                shares.append({
                    "name": entry.name,
                    "path": entry.path,
                    "volume": volume,
                    "filesystem": _compute_filesystem(entry.path),
                    "is_usb": entry.name.startswith("usbshare"),
                    "is_encrypted": False,
                    "is_writable": writable_dir(entry.path),
                })
        return shares

    def _guess_share_path(self, name):
        for volume in sorted(glob.glob("/volume*")):
            candidate = os.path.join(volume, name)
            if os.path.isdir(candidate):
                return candidate
        return None

    @staticmethod
    def _volume_of(path):
        posix = path.replace("\\", "/")
        parts = posix.split("/")
        if len(parts) > 2 and parts[1].startswith("volume"):
            return "/" + parts[1]
        return os.path.dirname(path)

    def _normalize(self, shares):
        """过滤系统共享、不存在的路径与非 /volume* 路径。"""
        result = []
        seen = set()
        for item in shares or []:
            name = str(item.get("name") or "").strip()
            path = item.get("path")
            if not name or not path:
                continue
            if is_system_name(name) or name.startswith("."):
                continue
            try:
                path = os.path.normpath(os.path.abspath(path))
            except (TypeError, ValueError):
                continue
            if not os.path.isdir(path):
                continue
            if not self.config.dev_mode:
                posix = path.replace("\\", "/")
                if not config.VOLUME_RE.match(posix):
                    continue
                if config.VOLUME_ROOT_RE.match(posix):
                    # 防止把存储空间根当成共享文件夹显示给用户
                    corrected = self._guess_share_path(name)
                    if not corrected:
                        continue
                    path = os.path.normpath(corrected)
            key = path_key(path)
            if key in seen:
                continue
            seen.add(key)
            item = dict(item)
            item["name"] = name
            item["path"] = path
            item.setdefault("volume", self._volume_of(path))
            item.setdefault("filesystem", "unknown")
            item.setdefault("is_usb", False)
            item.setdefault("is_encrypted", False)
            item.setdefault("is_writable", writable_dir(path))
            result.append(item)
        result.sort(key=lambda entry: entry["name"].lower())
        return result

    # ------------------------------------------------------------------
    # 查询 / 校验
    # ------------------------------------------------------------------
    def find_share(self, path, shares=None):
        if not path:
            return None
        candidates = shares if shares is not None else self.list_shares()
        best = None
        for share in candidates:
            if is_under(path, share["path"]):
                if best is None or len(share["path"]) > len(best["path"]):
                    best = share
        return best

    def share_root(self, path):
        share = self.find_share(path)
        if share is None:
            raise path_not_allowed(u"路径不在任何共享文件夹内: %s" % path)
        return share

    def validate_root(self, path, shares=None):
        """校验扫描根目录：真实目录、在共享文件夹内、非系统/隐藏、无符号链接。"""
        normalized = check_path_safety(path)
        share = self.find_share(normalized, shares)
        if share is None:
            raise path_not_allowed(u"扫描目录必须位于共享文件夹内: %s" % normalized)
        if not is_dir_nofollow(normalized):
            raise invalid(u"扫描目录不存在或不是目录: %s" % normalized)
        if has_symlink_component(normalized, share["path"]):
            raise ApiError("PATH_NOT_ALLOWED",
                           u"扫描目录包含符号链接，已拒绝: %s" % normalized)
        if not os.access(normalized, os.R_OK | os.X_OK):
            raise ApiError("PATH_NOT_ALLOWED",
                           u"没有权限读取扫描目录；请给系统内部用户 duplicatecleaner 授予读取权限: %s" % normalized)
        if is_system_name(os.path.basename(normalized)):
            raise path_not_allowed(u"禁止扫描系统目录: %s" % normalized)
        return share

    def validate_file(self, path, shares=None):
        """校验文件路径：普通文件、在共享文件夹内、非系统/隐藏、无符号链接。"""
        normalized = check_path_safety(path)
        share = self.find_share(normalized, shares)
        if share is None:
            raise path_not_allowed(u"文件必须位于共享文件夹内: %s" % normalized)
        if not is_regular_file(normalized):
            raise invalid(u"文件不存在或不是普通文件: %s" % normalized)
        if has_symlink_component(normalized, share["path"]):
            raise ApiError("PATH_NOT_ALLOWED",
                           u"文件路径包含符号链接，已拒绝: %s" % normalized)
        return share

    def recycle_dir(self, share):
        return os.path.join(share["path"], "#recycle")

    def has_recycle_dir(self, share):
        """#recycle 必须是真实目录（非符号链接）且当前套件用户可写。

        非 root 运行时，仅目录存在不够；无写权限时应走“回收站不可用”
        分支，让用户选择永久删除或先授权，而不是等删除任务失败。
        """
        candidate = self.recycle_dir(share)
        if os.path.islink(candidate):
            return False
        return os.path.isdir(candidate) and os.access(candidate, os.W_OK)

    def is_path_allowed(self, path):
        try:
            normalized = check_path_safety(path)
        except ApiError:
            return False
        return self.find_share(normalized) is not None
