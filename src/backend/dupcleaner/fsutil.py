# -*- coding: utf-8 -*-
"""路径安全、文件系统辅助函数。

安全规则（契约 §9）：
- 只允许共享文件夹根及其子目录；禁止 /etc、/usr、/var、/root、/volumeX 根、
  /volumeX/@*、#recycle、#snapshot、@eaDir、@tmp、@appstore 等系统目录。
- 不跟随符号链接；隐藏 / 系统文件默认跳过。
"""

from __future__ import absolute_import

import os
import re
import stat as stat_module

from . import config
from .errors import ApiError, invalid, path_not_allowed

_SPLIT_RE = re.compile(r"[\\/]+")


def norm_path(path):
    """规范化绝对路径；非法输入抛 INVALID_PATH。"""
    if not isinstance(path, str) or not path.strip():
        raise ApiError("INVALID_PATH", u"路径不能为空")
    if "\x00" in path:
        raise ApiError("INVALID_PATH", u"路径包含非法字符")
    if not os.path.isabs(path):
        raise ApiError("PATH_NOT_ALLOWED", u"必须使用绝对路径")
    normalized = os.path.normpath(path)
    if not normalized:
        raise ApiError("INVALID_PATH", u"路径非法")
    return normalized


def path_key(path):
    return os.path.normcase(os.path.normpath(path))


def _posix(path):
    return path.replace("\\", "/")


def is_under(path, root, allow_equal=True):
    """path 是否位于 root 之下（按 os.path.normcase 比较，兼容 NAS 大小写）。"""
    if not path or not root:
        return False
    p = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(os.path.abspath(root))
    if p == r:
        return allow_equal
    if not r.endswith(os.sep):
        r = r + os.sep
    return p.startswith(r)


def split_components(path):
    drive, tail = os.path.splitdrive(path)
    return drive, [c for c in _SPLIT_RE.split(tail) if c]


def is_hidden_name(name):
    return bool(name) and name.startswith(".")


def is_system_name(name):
    if not name:
        return False
    if name in config.SYSTEM_DIR_NAMES:
        return True
    return name.startswith(config.SYSTEM_NAME_PREFIXES)


def is_safe_dir_name(name):
    return bool(name) and not is_hidden_name(name) and not is_system_name(name)


def _matches_prefix(path, prefixes, sep):
    for prefix in prefixes:
        if path == prefix:
            return True
        if path.startswith(prefix + sep):
            return True
    return False


def check_path_safety(path, allow_components=None, reject_hidden=True):
    """校验路径是否在安全白名单内，返回规范化路径。

    allow_components：允许出现的系统目录名集合（例如回收站目标里的 #recycle）。
    """
    allowed = frozenset(allow_components or ())
    normalized = norm_path(path)
    posix_norm = _posix(normalized)
    drive, parts = split_components(normalized)

    if os.name == "nt":
        key = os.path.normcase(normalized)
        for prefix in config.WINDOWS_BLOCKED_PREFIXES:
            if key == prefix or key.startswith(prefix + "\\"):
                raise path_not_allowed(u"禁止访问系统目录: %s" % normalized)
    # POSIX 系统目录在开发环境（如 Windows 上带 / 开头的测试路径）也必须拒绝
    for prefix in config.POSIX_BLOCKED_PREFIXES:
        if posix_norm == prefix or posix_norm.startswith(prefix + "/"):
            raise path_not_allowed(u"禁止访问系统目录: %s" % normalized)

    if config.VOLUME_ROOT_RE.match(posix_norm):
        raise path_not_allowed(u"不能选择存储空间根目录: %s" % normalized)
    if os.name == "nt" and drive and normalized.rstrip("\\/").lower() == drive.lower():
        raise path_not_allowed(u"不能选择磁盘根目录: %s" % normalized)

    for comp in parts:
        if comp in (".", ".."):
            raise ApiError("INVALID_PATH", u"路径包含非法片段: %s" % normalized)
        if comp in allowed:
            continue
        if is_system_name(comp):
            raise path_not_allowed(u"禁止访问系统目录/文件: %s" % comp,
                                   {"component": comp})
        if reject_hidden and is_hidden_name(comp):
            raise path_not_allowed(u"不能访问隐藏目录/文件: %s" % comp,
                                   {"component": comp})
    return normalized


def is_system_path(path):
    try:
        check_path_safety(path, reject_hidden=False)
        return False
    except ApiError:
        return True


def ensure_within(path, root):
    if not is_under(path, root):
        raise path_not_allowed(u"路径不在允许的共享文件夹内: %s" % path)
    return path


def has_symlink_component(path, root):
    """从 root 到 path 的任意中间层是否为符号链接（含 path 本身）。"""
    rel = os.path.relpath(path, root)
    if rel in (".", ""):
        return False
    current = root
    for part in rel.split(os.sep):
        current = os.path.join(current, part)
        try:
            if os.path.islink(current):
                return True
        except OSError:
            return True
    return False


def lstat_or_none(path):
    try:
        return os.lstat(path)
    except OSError:
        return None


def stat_or_none(path):
    try:
        return os.stat(path)
    except OSError:
        return None


def is_symlink(path):
    try:
        return os.path.islink(path)
    except OSError:
        return True


def is_regular_file(path):
    st = lstat_or_none(path)
    if st is None:
        return False
    if stat_module.S_ISLNK(st.st_mode):
        return False
    return stat_module.S_ISREG(st.st_mode)


def is_dir_nofollow(path):
    st = lstat_or_none(path)
    if st is None:
        return False
    if stat_module.S_ISLNK(st.st_mode):
        return False
    return stat_module.S_ISDIR(st.st_mode)


def file_identity(path):
    """返回 (size, mtime_ns, inode, dev, nlink)；不存在返回 None。"""
    st = lstat_or_none(path)
    if st is None:
        return None
    if stat_module.S_ISLNK(st.st_mode):
        return None
    return {
        "size": int(st.st_size),
        "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1000000000))),
        "inode": int(getattr(st, "st_ino", 0)),
        "dev": int(getattr(st, "st_dev", 0)),
        "nlink": int(getattr(st, "st_nlink", 1)),
        "mode": int(st.st_mode),
    }


def same_device(path_a, path_b):
    st_a = lstat_or_none(path_a)
    st_b = lstat_or_none(path_b)
    if st_a is None or st_b is None:
        return False
    return int(getattr(st_a, "st_dev", 0)) == int(getattr(st_b, "st_dev", 0))


def extension_of(name):
    return os.path.splitext(name)[1]


def size_group_key(size):
    return int(size)


def readable_dir(path):
    try:
        return os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)
    except OSError:
        return False


def writable_dir(path):
    st = lstat_or_none(path)
    if st is None or stat_module.S_ISLNK(st.st_mode) or not stat_module.S_ISDIR(st.st_mode):
        return False
    return os.access(path, os.W_OK)


def has_permission(path, mode):
    try:
        return os.access(path, mode)
    except OSError:
        return False


def nearest_existing_ancestor(path):
    current = path
    while True:
        st = lstat_or_none(current)
        if st is not None:
            return current, st
        parent = os.path.dirname(current)
        if not parent or parent == current:
            return None, None
        current = parent


def validate_plain_filename(name):
    """校验从日志/文件列表里拿到的文件名（防路径穿越）。"""
    if not name or not isinstance(name, str):
        raise invalid(u"文件名不能为空")
    if name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise invalid(u"文件名非法: %s" % name)
    return name
