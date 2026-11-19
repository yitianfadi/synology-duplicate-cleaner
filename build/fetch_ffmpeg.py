#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch bundled static FFmpeg binaries (x86_64 / aarch64) for the SPK.

Binaries are intentionally not committed (.gitignore). Run once before building a
release if vendor/ffmpeg/ffmpeg-x86_64 / ffmpeg-aarch64 are missing.

v0065 起两个架构不再同源：

- **x86_64**：FFmpeg 8.1 静态构建（BtbN，`--enable-vaapi --enable-libvpl`），
  用于 VAAPI 硬件编解码。归档约 146 MB，从
  https://github.com/BtbN/FFmpeg-Builds/releases 取
  `ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz`，解出 `bin/ffmpeg`。
  国内可走镜像（例如 `https://gh-proxy.com/https://github.com/...`）。
- **aarch64**：继续用 ffmpeg-installer 的 4.1（ARM 机型没有 Intel 核显）。

VAAPI 运行库与驱动另外放在 `vendor/vaapi/`（`libva*.so.2`、`libigdgmm.so.12`、
`dri/iHD_drv_video.so`、`dri/i965_drv_video.so`），见 docs/SPK_PACKAGING.md §6.1；
可从未加密的 Emby/CodecPack 安装目录复制（都是 MIT 组件），或从 Intel 官方发布物
获取。本脚本不下载它们。
"""
from __future__ import annotations

import hashlib
import lzma
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "vendor" / "ffmpeg"

# x86_64：BtbN n8.1 静态构建（内含 FFmpeg 8.1 + VAAPI）
X86_URLS = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz",
    "https://gh-proxy.com/https://github.com/BtbN/FFmpeg-Builds/releases/"
    "download/latest/ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz",
)
X86_MIN_BYTES = 100 * 1024 * 1024  # 解压后的 ffmpeg 必须 > 100 MB（带 VAAPI 的大构建）

# aarch64：ffmpeg-installer 4.1（无硬件编码，够用即可）
ARM64_URL = ("https://registry.npmmirror.com/@ffmpeg-installer/linux-arm64/"
             "-/linux-arm64-4.1.4.tgz")
ARM64_SHA256 = ("115a825e246078acf820ea2afcaf1b3ff87f1b93f7035ecad38d3223cca55297")

GPL_URL = "https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/COPYING.GPLv3"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(str(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, target: Path) -> None:
    tmp = target.with_suffix(target.suffix + ".part")
    print("[ffmpeg] downloading %s" % url)
    with urllib.request.urlopen(url, timeout=120) as src, \
            open(str(tmp), "wb") as dst:
        while True:
            block = src.read(1024 * 1024)
            if not block:
                break
            dst.write(block)
    os.replace(str(tmp), str(target))


def fetch_x86_64(target: Path) -> None:
    if target.exists() and target.stat().st_size >= X86_MIN_BYTES:
        return
    with tempfile.TemporaryDirectory(prefix="ffmpeg-btbn-") as tmpdir:
        archive = Path(tmpdir) / "ffmpeg-linux64.tar.xz"
        last_error = None
        for url in X86_URLS:
            try:
                download(url, archive)
                last_error = None
                break
            except Exception as exc:  # 网络问题换镜像重试
                last_error = exc
                print("[ffmpeg] 下载失败（%s）: %s" % (url, exc))
        if last_error is not None:
            raise SystemExit(
                "[ffmpeg] 无法下载 x86_64 FFmpeg 8.1。可手动下载\n"
                "  ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz\n"
                "解出 bin/ffmpeg 放到 vendor/ffmpeg/ffmpeg-x86_64 后重试。")
        with lzma.open(str(archive), "rb") as fh:
            tf = tarfile.open(fileobj=fh, mode="r|")
            for member in tf:
                if not member.name.endswith("/bin/ffmpeg"):
                    continue
                data = tf.extractfile(member).read()
                if len(data) < X86_MIN_BYTES:
                    raise SystemExit("[ffmpeg] x86_64 二进制异常小：%d" % len(data))
                target.write_bytes(data)
                break
        if not target.exists():
            raise SystemExit("[ffmpeg] 归档里没找到 bin/ffmpeg")


def fetch_aarch64(target: Path) -> None:
    if target.exists() and target.stat().st_size > 0:
        return
    with tempfile.TemporaryDirectory(prefix="ffmpeg-npm-") as tmpdir:
        archive = Path(tmpdir) / "pkg.tgz"
        download(ARM64_URL, archive)
        with tarfile.open(str(archive), "r:gz") as tf:
            member = tf.getmember("package/ffmpeg")
            with tf.extractfile(member) as src, open(str(target), "wb") as dst:
                shutil.copyfileobj(src, dst)
    actual = sha256(target)
    if actual != ARM64_SHA256:
        target.unlink()
        raise SystemExit("[ffmpeg] aarch64 校验失败: %s" % actual)


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    x86 = DEST / "ffmpeg-x86_64"
    arm = DEST / "ffmpeg-aarch64"
    fetch_x86_64(x86)
    fetch_aarch64(arm)
    for path in (x86, arm):
        try:
            os.chmod(str(path), 0o755)
        except OSError:
            pass
        print("[ffmpeg] %s sha256=%s size=%d"
              % (path.name, sha256(path), path.stat().st_size))
    gpl = DEST / "GPL-3.0.txt"
    if not gpl.exists() or gpl.stat().st_size < 1000:
        download(GPL_URL, gpl)
    vaapi = ROOT / "vendor" / "vaapi"
    if not (vaapi / "dri" / "iHD_drv_video.so").exists():
        print("[ffmpeg] 提示：vendor/vaapi/ 缺少驱动，硬件编码在 NAS 上不可用；"
              "见 docs/SPK_PACKAGING.md §6.1")
    print("[ffmpeg] ready: %s" % DEST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
