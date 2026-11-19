# -*- coding: utf-8 -*-
"""VAAPI 硬件编解码探测（v0065，docs/CONVERT_CONTRACT.md §14）。

NAS（DS920+ 等 Intel Gemini Lake / Apollo Lake）有 iGPU，但默认有三道门槛：

1. `/dev/dri/renderD128` 属于 `root:videodriver 660`，套件用户必须在
   `videodriver` 组里（`spk/conf/privilege` 的 `join-groupname`）；
2. libva 的驱动目录不在系统默认路径（`/usr/lib/dri` 是空的），必须显式给
   `LIBVA_DRIVERS_PATH`；
3. 内置 FFmpeg（BtbN 静态版）对 libva 用「implib 运行时 dlopen」，所以
   `libva.so.2` / `libva-drm.so.2` 必须在 `LD_LIBRARY_PATH` 里；iHD 还依赖
   `libigdgmm.so.12`。这些随包放在 `target/lib/vaapi`。

注意：**不能**把 `/volume1/@appstore/EmbyServer/lib` 整个塞进
`LD_LIBRARY_PATH`——那里有 Emby 自带的旧 `libc.so.6`，会让 FFmpeg 直接报
`GLIBC_2.28 not found`。

本模块只做探测与拼装环境变量；真正可用性由 `FFmpegTool.probe_vaapi()` 跑一次
最小初始化确认（带缓存）。
"""

from __future__ import absolute_import

import os
import threading

from . import config

_lock = threading.RLock()
_cached = None

DRIVER_NAMES = ("iHD", "i965")
# 驱动实际支持的编解码器（保守表：宁可少写，也不要让任务跑到一半失败）。
DRIVER_CODECS = {
    "iHD": {"h264", "hevc", "vp9"},
    "i965": {"h264", "vp8", "vp9"},
}
DRIVER_ENCODERS = {
    "iHD": {"h264_vaapi", "hevc_vaapi"},
    "i965": {"h264_vaapi", "vp8_vaapi", "vp9_vaapi"},
}
_DRIVER_FILES = {"iHD": "iHD_drv_video.so", "i965": "i965_drv_video.so"}


def _device_path():
    for device in config.VAAPI_DEVICE_CANDIDATES:
        if os.path.exists(device):
            return device
    return ""


def bundle_dirs():
    """随包 VAAPI 目录候选（按优先级）。

    优先级：`DUP_VAAPI_DIR` 环境变量（自检/排障）→ 由 `DUP_FFMPEG_PATH`
    推导出的 `<套件目录>/lib/vaapi` → 仓库内开发布局。
    """
    roots = []
    explicit = (os.environ.get("DUP_VAAPI_DIR") or "").strip()
    if explicit:
        roots.append(explicit)
    exe = (os.environ.get("DUP_FFMPEG_PATH") or "").strip()
    if exe:
        package_root = os.path.dirname(os.path.dirname(os.path.abspath(exe)))
        roots.append(os.path.join(package_root, "lib", "vaapi"))
    here = os.path.dirname(os.path.abspath(__file__))
    roots.append(os.path.join(here, "..", "..", "..", "build", "stage",
                              "package", "target", "lib", "vaapi"))
    return [os.path.abspath(item) for item in roots]


def _bundle_root():
    for root in bundle_dirs():
        if os.path.isdir(root) and os.path.isdir(os.path.join(root, "dri")):
            return root
    return ""


def _extra_driver_dirs():
    return [path for path in config.VAAPI_FALLBACK_DRIVER_DIRS
            if os.path.isdir(path)]


def _pick_driver(bundle, extra_dirs):
    """按 iHD → i965 找驱动；返回 (driver, drivers_path, libs_path)。"""
    candidates = []
    if bundle:
        candidates.append((os.path.join(bundle, "dri"), bundle))
    for directory in extra_dirs:
        candidates.append((directory, ""))
    for name in DRIVER_NAMES:
        filename = _DRIVER_FILES[name]
        for driver_dir, lib_dir in candidates:
            if not os.path.isfile(os.path.join(driver_dir, filename)):
                continue
            libs_path = ""
            if name == "iHD":
                # iHD 需要 libigdgmm：随包目录或该驱动目录的上一级
                for candidate in (lib_dir, os.path.dirname(driver_dir)):
                    if candidate and os.path.isfile(
                            os.path.join(candidate, "libigdgmm.so.12")):
                        libs_path = candidate
                        break
                if not libs_path:
                    continue
            else:
                for candidate in (lib_dir, bundle):
                    if candidate and os.path.isfile(
                            os.path.join(candidate, "libva.so.2")):
                        libs_path = candidate
                        break
            return name, driver_dir, libs_path
    return "", "", ""


def detect(force=False):
    """探测并缓存：{available, device, driver, drivers_path, libs_path, reason}。"""
    global _cached
    with _lock:
        if _cached is not None and not force:
            return dict(_cached)
    result = _detect_uncached()
    with _lock:
        _cached = dict(result)
    return dict(result)


def _detect_uncached():
    device = _device_path()
    if not device:
        return _result(False, u"未找到 /dev/dri/renderD128（该机型无可用核显）")
    if not os.access(device, os.R_OK | os.W_OK):
        return _result(
            False, u"无权访问 %s（需要把套件用户加入 videodriver 组）" % device,
            device=device)
    bundle = _bundle_root()
    driver, drivers_path, libs_path = _pick_driver(bundle, _extra_driver_dirs())
    if not driver:
        return _result(
            False, u"未找到 VAAPI 驱动（iHD_drv_video.so / i965_drv_video.so）",
            device=device)
    return _result(True, u"", device=device, driver=driver,
                   drivers_path=drivers_path, libs_path=libs_path)


def _result(available, reason, **extra):
    return {
        "available": bool(available),
        "device": extra.get("device") or "",
        "driver": extra.get("driver") or "",
        "drivers_path": extra.get("drivers_path") or "",
        "libs_path": extra.get("libs_path") or "",
        "reason": reason,
    }


def env(info=None):
    """调用 ffmpeg 时需要追加的环境变量（只含 VAAPI 相关键）。"""
    data = info or detect()
    if not data.get("available"):
        return {}
    environ = {
        "LIBVA_DRIVER_NAME": data["driver"],
        "LIBVA_DRIVERS_PATH": data["drivers_path"],
    }
    libs = data.get("libs_path") or ""
    if libs:
        existing = os.environ.get("LD_LIBRARY_PATH") or ""
        parts = [libs]
        if existing:
            parts.append(existing)
        environ["LD_LIBRARY_PATH"] = os.pathsep.join(parts)
    return environ


def codec_supported(codec, info=None):
    """该驱动能否硬解这个视频编码（保守表；未知编码返回 False）。"""
    data = info or detect()
    if not data.get("available"):
        return False
    return (codec or "").lower() in DRIVER_CODECS.get(data["driver"], set())


def encoder_supported(encoder, info=None):
    data = info or detect()
    if not data.get("available"):
        return False
    return (encoder or "") in DRIVER_ENCODERS.get(data["driver"], set())


def reset_cache():
    global _cached
    with _lock:
        _cached = None
