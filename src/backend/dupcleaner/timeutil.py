# -*- coding: utf-8 -*-
"""创建时间读取与时间格式化。

创建时间读取点（重要）：
- Linux：优先 statx(2) 的 stx_btime（syscall 号 x86_64=332，aarch64=291），
  使用 AT_SYMLINK_NOFOLLOW 避免跟随符号链接；statx 不可用时尝试 st_birthtime。
- macOS / BSD / 新版 CPython：os.stat 的 st_birthtime。
- Windows：CreateFileW(FILE_FLAG_OPEN_REPARSE_POINT) +
  GetFileInformationByHandleEx(FileBasicInfo) 的 CreationTime（原生创建时间）。
- 绝不静默使用 st_ctime：只有 allow_ctime_fallback=True 才降级为 ctime，
  并明确返回 created_source="ctime"、created_unsafe=True。
"""

from __future__ import absolute_import

import ctypes
import ctypes.util
import os
import platform
import struct
import sys
import time
from datetime import datetime, timezone

# statx 常量（Linux）
STATX_BTIME = 0x0800
AT_FDCWD = -100
AT_SYMLINK_NOFOLLOW = 0x100

# syscall 号：x86_64 / aarch64 见内核 arch 头文件（statx 在 4.11+ 引入）
STATX_SYSCALL_NUMBERS = {
    "x86_64": 332,
    "amd64": 332,
    "aarch64": 291,
    "arm64": 291,
    "i386": 383,
    "i686": 383,
    "armv7l": 397,
    "ppc64le": 383,
    "s390x": 379,
}

_EPOCH_1601 = 116444736000000000  # 1601-01-01 到 1970-01-01 的 100ns 数


class _StatxTimestamp(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_int64),
        ("tv_nsec", ctypes.c_uint32),
        ("__reserved", ctypes.c_int32),
    ]


class _Statx(ctypes.Structure):
    # 与内核 struct statx 前 160 字节对齐；调用时使用 256 字节缓冲区。
    _fields_ = [
        ("stx_mask", ctypes.c_uint32),
        ("stx_blksize", ctypes.c_uint32),
        ("stx_attributes", ctypes.c_uint64),
        ("stx_nlink", ctypes.c_uint32),
        ("stx_uid", ctypes.c_uint32),
        ("stx_gid", ctypes.c_uint32),
        ("stx_mode", ctypes.c_uint16),
        ("__spare0", ctypes.c_uint16 * 1),
        ("stx_ino", ctypes.c_uint64),
        ("stx_size", ctypes.c_uint64),
        ("stx_blocks", ctypes.c_uint64),
        ("stx_attributes_mask", ctypes.c_uint64),
        ("stx_atime", _StatxTimestamp),
        ("stx_btime", _StatxTimestamp),
        ("stx_ctime", _StatxTimestamp),
        ("stx_mtime", _StatxTimestamp),
        ("stx_rdev_major", ctypes.c_uint32),
        ("stx_rdev_minor", ctypes.c_uint32),
        ("stx_dev_major", ctypes.c_uint32),
        ("stx_dev_minor", ctypes.c_uint32),
        ("stx_mnt_id", ctypes.c_uint64),
        ("__spare3", ctypes.c_uint64 * 10),
    ]


class _WinFileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("FileAttributes", ctypes.c_uint32),
    ]


_libc = None
_libc_ready = False
_statx_probe = {"done": False, "supported": False, "error": None, "number": None}
_win_kernel32 = None
_win_ready = False


def _get_libc():
    global _libc, _libc_ready
    if _libc_ready:
        return _libc
    _libc_ready = True
    try:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.syscall.restype = ctypes.c_long
    except Exception:
        _libc = None
    return _libc


def statx_syscall_number():
    machine = platform.machine().lower()
    return STATX_SYSCALL_NUMBERS.get(machine)


def _probe_statx():
    """探测当前内核/架构是否支持 statx(btime)。结果缓存。"""
    if _statx_probe["done"]:
        return _statx_probe
    _statx_probe["done"] = True
    if not sys.platform.startswith("linux"):
        _statx_probe["error"] = "not linux"
        return _statx_probe
    number = statx_syscall_number()
    _statx_probe["number"] = number
    if number is None:
        _statx_probe["error"] = "unknown arch: %s" % platform.machine()
        return _statx_probe
    libc = _get_libc()
    if libc is None:
        _statx_probe["error"] = "libc unavailable"
        return _statx_probe
    try:
        buf = _Statx()
        target = os.path.abspath(__file__)
        ret = libc.syscall(ctypes.c_long(number), ctypes.c_int(AT_FDCWD),
                           ctypes.c_char_p(os.fsencode(target)),
                           ctypes.c_int(AT_SYMLINK_NOFOLLOW),
                           ctypes.c_uint(STATX_BTIME), ctypes.byref(buf))
        if ret == 0 and (buf.stx_mask & STATX_BTIME):
            _statx_probe["supported"] = True
        else:
            errno_value = ctypes.get_errno()
            _statx_probe["error"] = "errno=%s ret=%s" % (errno_value, ret)
    except Exception as exc:  # pragma: no cover - 平台相关
        _statx_probe["error"] = repr(exc)
    return _statx_probe


def statx_supported():
    return bool(_probe_statx()["supported"])


def _statx_btime(path):
    """创建时间读取点：statx(2) 的 stx_btime。返回 (ts, ns) 或 None。"""
    probe = _probe_statx()
    if not probe["supported"]:
        return None
    libc = _get_libc()
    number = probe["number"]
    if libc is None or number is None:
        return None
    try:
        buf = ctypes.create_string_buffer(256)
        ret = libc.syscall(ctypes.c_long(number), ctypes.c_int(AT_FDCWD),
                           ctypes.c_char_p(os.fsencode(path)),
                           ctypes.c_int(AT_SYMLINK_NOFOLLOW),
                           ctypes.c_uint(STATX_BTIME),
                           ctypes.cast(buf, ctypes.POINTER(_Statx)))
        if ret != 0:
            return None
        stx = ctypes.cast(buf, ctypes.POINTER(_Statx)).contents
        if not (stx.stx_mask & STATX_BTIME):
            return None
        if stx.stx_btime.tv_sec <= 0:
            return None
        return (int(stx.stx_btime.tv_sec), int(stx.stx_btime.tv_nsec))
    except Exception:
        return None


def _get_kernel32():
    global _win_kernel32, _win_ready
    if _win_ready:
        return _win_kernel32
    _win_ready = True
    if not sys.platform.startswith("win"):
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.GetFileInformationByHandleEx.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k32.GetFileInformationByHandleEx.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int
        _win_kernel32 = k32
    except Exception:
        _win_kernel32 = None
    return _win_kernel32


def _win_creation_time(path):
    """创建时间读取点（Windows）：GetFileInformationByHandleEx(FileBasicInfo)。"""
    k32 = _get_kernel32()
    if k32 is None:
        return None
    invalid_handle = ctypes.c_void_p(-1).value
    handle = None
    try:
        handle = k32.CreateFileW(
            path,
            0x80000000,  # GENERIC_READ
            0x00000007,  # FILE_SHARE_READ | WRITE | DELETE
            None,
            3,           # OPEN_EXISTING
            0x02000000 | 0x00200000,  # FILE_FLAG_BACKUP_SEMANTICS | OPEN_REPARSE_POINT
            None)
        if handle is None or handle == invalid_handle:
            return None
        info = _WinFileBasicInfo()
        ok = k32.GetFileInformationByHandleEx(
            handle, 0, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            return None
        raw = int(info.CreationTime)
        if raw <= 0:
            return None
        delta = raw - _EPOCH_1601
        if delta <= 0:
            return None
        seconds = delta // 10000000
        nanos = (delta % 10000000) * 100
        return (int(seconds), int(nanos))
    except Exception:
        return None
    finally:
        if handle is not None and handle != invalid_handle:
            try:
                k32.CloseHandle(handle)
            except Exception:
                pass


def _read_birthtime(path):
    """按优先级尝试各平台的真实创建时间，返回 (ts, ns, source) 或 None。"""
    # 1) Linux statx
    btime = _statx_btime(path)
    if btime and btime[0] > 0:
        return (btime[0], btime[1], "statx")

    # 2) st_birthtime（macOS / BSD / 部分 CPython 版本）
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        st = None
    if st is not None:
        birth = getattr(st, "st_birthtime", None)
        if birth:
            sec = int(birth)
            ns = int(round((float(birth) - sec) * 1000000000))
            if ns < 0 or ns >= 1000000000:
                ns = 0
            if sec > 0:
                return (sec, ns, "birthtime")

    # 3) Windows 原生创建时间
    win = _win_creation_time(path)
    if win and win[0] > 0:
        return (win[0], win[1], "birthtime")
    return None


def read_creation_time(path, allow_ctime_fallback=False):
    """读取文件创建时间。

    返回 dict：created_ts / created_ns / created_source / created_unsafe。
    source 取值：statx | birthtime | ctime | unknown。
    """
    result = {
        "created_ts": None,
        "created_ns": 0,
        "created_source": "unknown",
        "created_unsafe": True,
    }
    bt = _read_birthtime(path)
    if bt and bt[0] > 0:
        result["created_ts"] = int(bt[0])
        result["created_ns"] = int(bt[1])
        result["created_source"] = bt[2]
        result["created_unsafe"] = False
        return result
    if allow_ctime_fallback:
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            return result
        ctime = getattr(st, "st_ctime", None)
        if ctime is not None and ctime > 0:
            result["created_ts"] = int(ctime)
            try:
                result["created_ns"] = int(getattr(st, "st_ctime_ns", int(ctime) * 1000000000) % 1000000000)
            except Exception:
                result["created_ns"] = 0
            result["created_source"] = "ctime"
            result["created_unsafe"] = True
    return result


def creation_time_supported():
    """是否可以在当前平台读取真实创建时间。"""
    if sys.platform.startswith("linux"):
        return statx_supported()
    if sys.platform.startswith("win"):
        return _get_kernel32() is not None
    return hasattr(os.stat_result, "st_birthtime")


# ---------------------------------------------------------------------------
# 时间格式化（NAS 本地时区 ISO8601，带偏移）
# ---------------------------------------------------------------------------

def iso_local(ts):
    if ts is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone()
    except (OverflowError, OSError, ValueError):
        return None
    return dt.isoformat()


def iso_from_epoch(ts):
    return iso_local(ts)


def now_iso():
    return iso_local(time.time())


def parse_iso(value):
    """解析 ISO8601 为 epoch 秒；失败返回 None。"""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def read_dsm_version():
    """读取 DSM 版本号（/etc/VERSION 或 /etc.defaults/VERSION）。"""
    for path in ("/etc/VERSION", "/etc.defaults/VERSION"):
        try:
            values = {}
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if "=" not in line:
                        continue
                    key, _, val = line.strip().partition("=")
                    values[key.strip()] = val.strip().strip('"')
            major = values.get("majorversion") or values.get("productversion")
            build = values.get("buildnumber") or values.get("build")
            if major and build:
                return "%s-%s" % (major, build)
            if major:
                return major
        except (OSError, UnicodeError):
            continue
    return None


def epoch_ns(ts, ns):
    if ts is None:
        return None
    return int(ts) * 1000000000 + int(ns or 0)


def struct_pack_size(size):
    return struct.pack("<Q", int(size))
