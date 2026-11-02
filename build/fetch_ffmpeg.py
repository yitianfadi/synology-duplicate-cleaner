#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch bundled static FFmpeg binaries (x86_64 / aarch64) for the SPK.

Binaries are intentionally not committed to the repository. Run this once before
building a release if vendor/ffmpeg/ffmpeg-x86_64 / ffmpeg-aarch64 are missing.

This uses the npm ffmpeg-installer platform packages (fast mirror), extracting
only package/ffmpeg. FFmpeg is GPLv3; GPL text is fetched from upstream.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "vendor" / "ffmpeg"
PACKAGES = [
    ("https://registry.npmmirror.com/@ffmpeg-installer/linux-x64/-/linux-x64-4.1.0.tgz",
     "ffmpeg-x86_64", "6d0c625dbd3f604ed2618981ba6fee7e29678855ab741d654cfc9effb0226624"),
    ("https://registry.npmmirror.com/@ffmpeg-installer/linux-arm64/-/linux-arm64-4.1.4.tgz",
     "ffmpeg-aarch64", "115a825e246078acf820ea2afcaf1b3ff87f1b93f7035ecad38d3223cca55297"),
]
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
    with urllib.request.urlopen(url, timeout=60) as src, open(str(tmp), "wb") as dst:
        while True:
            block = src.read(1024 * 1024)
            if not block:
                break
            dst.write(block)
    os.replace(str(tmp), str(target))


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    for url, name, expected in PACKAGES:
        target = DEST / name
        if target.exists() and target.stat().st_size > 0:
            continue
        with tempfile.TemporaryDirectory(prefix="ffmpeg-npm-") as tmpdir:
            archive = Path(tmpdir) / "pkg.tgz"
            download(url, archive)
            with tarfile.open(str(archive), "r:gz") as tf:
                member = tf.getmember("package/ffmpeg")
                with tf.extractfile(member) as src, open(str(target), "wb") as dst:
                    shutil.copyfileobj(src, dst)
        try:
            os.chmod(str(target), 0o755)
        except OSError:
            pass
        actual = sha256(target)
        if expected and actual != expected:
            target.unlink()
            raise SystemExit("[ffmpeg] checksum mismatch for %s: %s" % (name, actual))
        print("[ffmpeg] %s sha256=%s" % (name, actual))
    gpl = DEST / "GPL-3.0.txt"
    if not gpl.exists() or gpl.stat().st_size < 1000:
        download(GPL_URL, gpl)
    print("[ffmpeg] ready: %s" % DEST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
