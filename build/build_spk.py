#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建群晖 DSM 7 SPK 套件（跨平台，无需 DSM 工具链）。

用法：
    python build/build_spk.py                 # 默认 HTTP 后端，生成 dist/DuplicateCleaner-1.1.0-0045.spk
    python build/build_spk.py --scheme https  # 后端启用 TLS 时，让 DSM 入口跳转 https://nas:8787/
    python build/build_spk.py --no-icons      # 不重新生成图标（使用仓库内已生成 PNG）

产物结构（外层 SPK）：
    INFO
    package.tgz          # 内含 app/ web/ ui/ port_conf/
    PACKAGE_ICON.PNG
    PACKAGE_ICON_256.PNG
    LICENSE
    conf/privilege
    conf/resource
    scripts/{preinst,postinst,postupgrade,preuninst,postuninst,start-stop-status}

依赖：Python 3.8+；仅构建图标需要 Pillow（可选，仓库已带 PNG）。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.1.0-0045"
PKG_NAME = "DuplicateCleaner"
SPK_FILENAME = "%s-%s.spk" % (PKG_NAME, VERSION)

REQUIRED_APP = [
    ROOT / "src" / "backend" / "app.py",
    ROOT / "src" / "backend" / "dupcleaner" / "__init__.py",
    ROOT / "src" / "web" / "index.html",
    ROOT / "src" / "web" / "app.js",
    ROOT / "spk" / "INFO",
]
EXCLUDE_NAMES = {"__pycache__", ".DS_Store", "Thumbs.db", "desktop.ini", ".pytest_cache", "test_output.txt"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".swp"}
FFMPEG_VENDOR = ROOT / "vendor" / "ffmpeg"
FFMPEG_BINS = {
    "ffmpeg-x86_64": FFMPEG_VENDOR / "ffmpeg-x86_64",
    "ffmpeg-aarch64": FFMPEG_VENDOR / "ffmpeg-aarch64",
}
ARGS = None


def log(msg: str) -> None:
    print("[build] " + msg)


def assert_required() -> None:
    missing = [str(p.relative_to(ROOT)) for p in REQUIRED_APP if not p.exists()]
    if missing:
        raise SystemExit("缺少构建所需文件:\n  " + "\n  ".join(missing))
    missing_ffmpeg = [str(p.relative_to(ROOT)) for p in FFMPEG_BINS.values() if not p.exists()]
    if missing_ffmpeg:
        raise SystemExit(
            "缺少捆绑 FFmpeg 二进制，请先运行:\n"
            "  python build/fetch_ffmpeg.py\n"
            "缺少:\n  " + "\n  ".join(missing_ffmpeg)
        )


def norm_text(data: bytes) -> bytes:
    # DSM 的 shell 必须是 LF；Windows/git 可能带入 CRLF。
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def write_text_lf(path: Path, text: str) -> None:
    # Python 3.8 的 Path.write_text 没有 newline 参数。
    with open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def copy_tree(src: Path, dst: Path, normalize_text: bool = False) -> None:
    if not src.exists():
        raise SystemExit("源目录不存在: %s" % src)
    for base, dirs, files in os.walk(str(src)):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_NAMES and not d.startswith("."))
        rel = Path(base).relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for fname in sorted(files):
            if fname in EXCLUDE_NAMES or Path(fname).suffix.lower() in EXCLUDE_SUFFIXES:
                continue
            s = Path(base) / fname
            t = target_dir / fname
            is_script = "scripts" in rel.parts
            if normalize_text and (is_script or fname.endswith(".sc") or fname in {"INFO", "config", "resource", "index.html", "strings"}):
                t.write_bytes(norm_text(s.read_bytes()))
            else:
                shutil.copy2(s, t)


def add_tar_file(tar: tarfile.TarFile, path: Path, arcname: str, mode: int = 0o644) -> None:
    info = tar.gettarinfo(str(path), arcname=arcname)
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 1700000000  # 固定时间，便于复现
    with open(path, "rb") as fh:
        tar.addfile(info, fh)


def add_tar_dir(tar: tarfile.TarFile, arcname: str, mode: int = 0o755) -> None:
    info = tarfile.TarInfo(arcname)
    info.type = tarfile.DIRTYPE
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 1700000000
    tar.addfile(info)


def add_tree(tar: tarfile.TarFile, root: Path, top: str, exec_all: bool = False) -> None:
    add_tar_dir(tar, top)
    for p in sorted(root.rglob("*")):
        arc = top + "/" + str(p.relative_to(root)).replace(os.sep, "/")
        if p.is_dir():
            add_tar_dir(tar, arc)
        else:
            mode = 0o755 if (exec_all or p.suffix == ".sh" or p.name == "app.py") else 0o644
            add_tar_file(tar, p, arc, mode)


def stage_ffmpeg(target: Path) -> None:
    """把两个架构的静态 FFmpeg 和第三方声明放进 package.tgz。"""
    bindir = target / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    for name, src in FFMPEG_BINS.items():
        dst = bindir / name
        shutil.copy2(str(src), str(dst))
        try:
            dst.chmod(0o755)
        except OSError:
            pass
    notices = [
        "DuplicateCleaner bundles FFmpeg 4.1 static binaries for x86_64 and aarch64.",
        "Source: https://registry.npmmirror.com/@ffmpeg-installer/linux-x64 and linux-arm64 (ffmpeg-installer).",
        "FFmpeg is licensed under the GNU General Public License v3; see bin/LICENSE-FFMPEG-GPLv3.txt.",
        "The bundled binaries are used only for local video frame extraction.",
    ]
    gpl = FFMPEG_VENDOR / "GPL-3.0.txt"
    if gpl.exists():
        shutil.copy2(str(gpl), str(bindir / "LICENSE-FFMPEG-GPLv3.txt"))
    write_text_lf(target / "THIRD_PARTY_NOTICES.txt", "\n".join(notices) + "\n")


def build_package_tgz(stage: Path) -> Path:
    """package.tgz = 安装到 /var/packages/DuplicateCleaner/target/ 的内容。"""
    target = stage / "target"
    if target.exists():
        shutil.rmtree(target)
    (target / "app").mkdir(parents=True)
    copy_tree(ROOT / "src" / "backend", target / "app")
    for junk in (target / "app" / "tests",):
        if junk.exists():
            shutil.rmtree(junk)
    for cache in (target / "app").rglob("__pycache__"):
        shutil.rmtree(str(cache), ignore_errors=True)
    # app/ 只允许 Python 源码：防止测试输出/临时文件/日志混入 SPK。
    for p in sorted((target / "app").rglob("*")):
        if p.is_file() and p.suffix != ".py":
            p.unlink()
    copy_tree(ROOT / "src" / "web", target / "web", normalize_text=True)
    copy_tree(ROOT / "spk" / "ui", target / "ui", normalize_text=True)
    copy_tree(ROOT / "spk" / "port_conf", target / "port_conf", normalize_text=True)
    stage_ffmpeg(target)

    # 把 DSM 桌面入口里的协议/主机占位符替换为构建参数。
    scheme = "https" if ARGS.scheme == "https" else "http"
    ui_index = target / "ui" / "index.html"
    if ui_index.exists():
        text = ui_index.read_text(encoding="utf-8")
        text = text.replace("__DUP_BACKEND_SCHEME__", scheme)
        text = text.replace("__DUP_HOST__", "当前 DSM 主机")
        write_text_lf(ui_index, text)
    write_text_lf(target / "VERSION", VERSION + "\n")

    pkg = stage / "package.tgz"
    with open(str(pkg), "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                add_tree(tar, target / "app", "app")
                add_tree(tar, target / "web", "web")
                add_tree(tar, target / "ui", "ui")
                add_tree(tar, target / "port_conf", "port_conf")
                add_tar_dir(tar, "bin")
                for name in sorted(FFMPEG_BINS):
                    add_tar_file(tar, target / "bin" / name, "bin/" + name, 0o755)
                for name in ("LICENSE-FFMPEG-GPLv3.txt",):
                    p = target / "bin" / name
                    if p.exists():
                        add_tar_file(tar, p, "bin/" + name, 0o644)
                add_tar_file(tar, target / "THIRD_PARTY_NOTICES.txt", "THIRD_PARTY_NOTICES.txt", 0o644)
                add_tar_file(tar, target / "VERSION", "VERSION", 0o644)
    log("package.tgz: %.1f KB" % (pkg.stat().st_size / 1024.0))
    return pkg


def inject_info(stage: Path, checksum: str) -> Path:
    info = norm_text((ROOT / "spk" / "INFO").read_bytes()).decode("utf-8")
    lines = []
    found = False
    for line in info.splitlines():
        if line.startswith("checksum="):
            lines.append('checksum="%s"' % checksum)
            found = True
        else:
            lines.append(line)
    if not found:
        lines.append('checksum="%s"' % checksum)
    p = stage / "INFO"
    write_text_lf(p, "\n".join(lines) + "\n")
    log("INFO checksum=%s" % checksum)
    return p


def stage_outer(stage: Path, package_tgz: Path) -> Path:
    outer = stage / "spk_root"
    if outer.exists():
        shutil.rmtree(outer)
    outer.mkdir(parents=True)
    checksum = hashlib.md5(package_tgz.read_bytes()).hexdigest()
    shutil.copy2(str(package_tgz), str(outer / "package.tgz"))
    inject_info(outer, checksum)
    for name in ("PACKAGE_ICON.PNG", "PACKAGE_ICON_256.PNG", "LICENSE"):
        shutil.copy2(str(ROOT / "spk" / name), str(outer / name))
    copy_tree(ROOT / "spk" / "conf", outer / "conf", normalize_text=True)
    copy_tree(ROOT / "spk" / "scripts", outer / "scripts", normalize_text=True)
    for p in (outer / "scripts").rglob("*"):
        if p.is_file():
            p.chmod(0o755)
    for p in (outer / "conf").rglob("*"):
        if p.is_file():
            p.chmod(0o644)
    return outer


def make_spk(outer: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    order = ["INFO", "package.tgz", "PACKAGE_ICON.PNG", "PACKAGE_ICON_256.PNG", "LICENSE",
             "conf", "scripts"]
    with tarfile.open(str(out), "w", format=tarfile.PAX_FORMAT) as tar:
        for name in order:
            p = outer / name
            if not p.exists():
                raise SystemExit("外层缺少: %s" % name)
            if p.is_dir():
                # DSM 生命周期脚本没有扩展名，必须全部 755。
                add_tree(tar, p, name, exec_all=(name == "scripts"))
            else:
                add_tar_file(tar, p, name, 0o644)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (out.parent / (out.name + ".sha256")).write_text(digest + "  " + out.name + "\n", encoding="utf-8")
    log("SPK: %s (%.1f KB)" % (out, out.stat().st_size / 1024.0))
    log("SHA256: %s" % digest)


def main() -> int:
    global ARGS
    parser = argparse.ArgumentParser(description="Build Synology DSM 7 SPK for DuplicateCleaner")
    parser.add_argument("--scheme", choices=["http", "https"], default="http",
                        help="后端协议；默认 http。启用 TLS 后端时传 https。")
    parser.add_argument("--no-icons", action="store_true", help="跳过 Pillow 图标生成")
    parser.add_argument("--out-dir", default=str(ROOT / "dist"))
    ARGS = parser.parse_args()
    assert_required()
    if not ARGS.no_icons:
        try:
            import PIL  # noqa: F401
            subprocess.check_call([sys.executable, str(ROOT / "build" / "gen_icons.py")])
        except ImportError:
            log("Pillow 未安装，使用仓库已有图标")
    stage = Path(tempfile.mkdtemp(prefix="dupcleaner-spk-"))
    try:
        pkg = build_package_tgz(stage)
        outer = stage_outer(stage, pkg)
        out = Path(ARGS.out_dir) / SPK_FILENAME
        make_spk(outer, out)
        with tarfile.open(str(out)) as tar:
            names = set(tar.getnames())
            need = {"INFO", "package.tgz", "PACKAGE_ICON.PNG", "PACKAGE_ICON_256.PNG",
                    "conf/privilege", "conf/resource", "scripts/start-stop-status",
                    "scripts/preinst", "scripts/postinst"}
            if not need.issubset(names):
                raise SystemExit("SPK 结构不完整，缺少: %s" % (need - names))
        log("Done.")
        return 0
    finally:
        shutil.rmtree(str(stage), ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
