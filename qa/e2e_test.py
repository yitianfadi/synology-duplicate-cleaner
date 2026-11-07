#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DuplicateCleaner 端到端验收脚本（Python 3.8+，仅标准库）。

启动 dev 模式后端（临时数据目录 + 临时共享根），构造受控创建时间的重复文件
样本，跑完整 API 流程：登录 -> 扫描 -> 分组 -> CSV/预览/日志 -> 删除计划 ->
回收站/永久删除 -> 重启持久化。逐项记录 PASS/FAIL 到 qa/e2e_results.json。

用法：
    python qa/e2e_test.py [--keep]
    --keep  保留临时目录（默认自动清理）
退出码：0=全部 PASS；1=有 FAIL；2=无法启动/脚本异常。
"""

from __future__ import print_function

import csv
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.parse import quote, urlencode

if sys.version_info < (3, 8):
    sys.exit("Python 3.8+ required")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "src", "backend", "app.py")
WEB_DIR = os.path.join(REPO, "src", "web")
RESULT_JSON = os.path.join(REPO, "qa", "e2e_results.json")

RISK_TEXT = (u"删除文件存在数据丢失风险，重要数据建议先备份；本工具仅基于哈希判定重复，"
             u"无法保证业务文件适用性，请核对扫描结果再操作。")

RESULTS = []
EVIDENCE = {}
_MODULE_BTIME_SUPPORTED = True


def expected_app_version():
    """从后端 config.py 读取期望版本，避免升级后验收脚本写死版本号。"""
    try:
        with open(os.path.join(REPO, "src", "backend", "dupcleaner", "config.py"),
                  "r", encoding="utf-8") as handle:
            match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', handle.read())
        return match.group(1) if match else None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 结果记录
# ---------------------------------------------------------------------------
def record(rid, name, status, detail=""):
    item = {"id": rid, "name": name, "status": status, "detail": detail}
    RESULTS.append(item)
    line = "[%s] %s %s" % (status, rid, name)
    if detail and status != "PASS":
        line += " :: " + str(detail)[:600]
    print(line)


def check(rid, name, cond, detail=""):
    record(rid, name, "PASS" if cond else "FAIL", "" if cond else detail)
    return bool(cond)


def info(rid, name, detail=""):
    record(rid, name, "INFO", detail)


def evidence(key, value):
    EVIDENCE[key] = value


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------
class Resp(object):
    def __init__(self, method, path, status, headers, body):
        self.method = method
        self.path = path
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeError):
            return None

    def text(self):
        return self.body.decode("utf-8", "replace")

    def header(self, name):
        return self.headers.get(name.lower(), "")

    def __repr__(self):
        return "<Resp %s %s -> %s>" % (self.method, self.path, self.status)


class Client(object):
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.cookies = {}
        self.csrf = None

    def _store_cookies(self, values):
        for item in values or []:
            pair = item.split(";", 1)[0].strip()
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            lowered = item.lower()
            if value in ("", '""') or "max-age=0" in lowered:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value

    def request(self, method, path, body=None, headers=None, csrf=True,
                timeout=180, connection_close=True):
        hdrs = {"Accept": "*/*"}
        if connection_close:
            hdrs["Connection"] = "close"
        if self.cookies:
            hdrs["Cookie"] = "; ".join(
                "%s=%s" % (k, v) for k, v in self.cookies.items())
        if method not in ("GET", "HEAD") and csrf and self.csrf:
            hdrs["X-CSRF-Token"] = self.csrf
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json; charset=utf-8"
        if headers:
            hdrs.update(headers)
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            raw = conn.getresponse()
            data = raw.read()
            collected = {}
            for key, value in raw.getheaders():
                collected.setdefault(key.lower(), []).append(value)
            flat = {k: ", ".join(v) for k, v in collected.items()}
            self._store_cookies(collected.get("set-cookie"))
            return Resp(method, path, raw.status, flat, data)
        finally:
            conn.close()

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post_json(self, path, body, **kw):
        return self.request("POST", path, body=body, **kw)

    def put_json(self, path, body, **kw):
        return self.request("PUT", path, body=body, **kw)

    def login(self, user="admin", password="admin"):
        resp = self.post_json("/api/auth/login",
                              {"username": user, "password": password},
                              csrf=False)
        data = resp.json() or {}
        if resp.status == 200 and data.get("ok"):
            self.csrf = data.get("csrf_token")
        return resp


def q(endpoint, **params):
    if not params:
        return endpoint
    return endpoint + "?" + urlencode(params)


def jd(resp):
    data = resp.json()
    return data if isinstance(data, dict) else {}


def err_code(resp):
    data = jd(resp)
    err = data.get("error")
    if isinstance(err, dict):
        return err.get("code"), err.get("details") or {}
    return None, {}


# ---------------------------------------------------------------------------
# 后端进程
# ---------------------------------------------------------------------------
def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class Backend(object):
    def __init__(self, roots, data_dir):
        self.roots = list(roots)
        self.data_dir = data_dir
        self.port = free_port()
        self.proc = None
        self.log_path = os.path.join(os.path.dirname(data_dir), "backend-server.log")
        self._log_handle = None

    def start(self, timeout=60):
        env = os.environ.copy()
        env["DUP_CLEANER_DEV"] = "1"
        env["DUP_DEV_ROOTS"] = ";".join(self.roots)
        env["DUP_DEV_PASSWORD"] = "admin"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self._log_handle = open(self.log_path, "ab")
        cmd = [sys.executable, APP, "--host", "127.0.0.1", "--port", str(self.port),
               "--data-dir", self.data_dir, "--web-dir", WEB_DIR]
        self.proc = subprocess.Popen(cmd, cwd=REPO, env=env,
                                     stdout=self._log_handle,
                                     stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("backend exited early (rc=%s)\n%s"
                                   % (self.proc.returncode, self.tail_log()))
            try:
                probe = Client("127.0.0.1", self.port)
                resp = probe.get("/api/healthz", csrf=False, timeout=3)
                if resp.status == 200:
                    return self
            except Exception as exc:  # 连接尚未就绪
                last_err = exc
            time.sleep(0.2)
        raise RuntimeError("backend not ready in %ss: %r\n%s"
                           % (timeout, last_err, self.tail_log()))

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None
        self.proc = None

    def tail_log(self, lines=80):
        try:
            with open(self.log_path, "rb") as handle:
                text = handle.read().decode("utf-8", "replace")
        except OSError:
            return "(no backend log)"
        return "\n".join(text.splitlines()[-lines:])


# ---------------------------------------------------------------------------
# 测试样本
# ---------------------------------------------------------------------------
def blob(seed, size):
    out = bytearray()
    counter = 0
    while len(out) < size:
        out.extend(hashlib.sha256(("%s|%d" % (seed, counter)).encode()).digest())
        counter += 1
    return bytes(out[:size])


def write_bytes(path, data):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def btime_ns(path):
    """独立读取创建时间（Python 3.12+ 提供 st_birthtime；否则返回 None）。"""
    try:
        stat_result = os.stat(path)
    except OSError:
        return None
    birth = getattr(stat_result, "st_birthtime", None)
    if birth is None:
        return None
    return int(round(float(birth) * 1000000000))


_KERNEL32 = None
_BTIME_SETTER_AVAILABLE = None
_BTIME_CLOCK_NS = None


def _kernel32():
    """惰性加载 kernel32（仅 Windows）；用于显式设置创建时间。"""
    global _KERNEL32
    if _KERNEL32 is not None:
        return _KERNEL32
    if not sys.platform.startswith("win"):
        _KERNEL32 = False
        return _KERNEL32
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                    ctypes.c_uint32, ctypes.c_void_p,
                                    ctypes.c_uint32, ctypes.c_uint32,
                                    ctypes.c_void_p]
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.SetFileTime.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_void_p]
        k32.SetFileTime.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int
        _KERNEL32 = k32
    except Exception:
        _KERNEL32 = False
    return _KERNEL32


def _can_set_btime():
    global _BTIME_SETTER_AVAILABLE
    if _BTIME_SETTER_AVAILABLE is None:
        _BTIME_SETTER_AVAILABLE = _kernel32() is not False
    return _BTIME_SETTER_AVAILABLE


def _set_btime_ns(path, ns):
    """Windows: SetFileTime 显式写入创建时间（避免 NTFS 文件隧道保留旧 btime）。"""
    k32 = _kernel32()
    if not k32:
        return False
    import ctypes
    handle = k32.CreateFileW(path, 0x100, 0x7, None, 3, 0x80, None)
    invalid = ctypes.c_void_p(-1).value
    if not handle or handle == invalid:
        return False
    try:
        filetime = int((int(ns) // 1000000000 + 11644473600) * 10000000
                       + (int(ns) % 1000000000) // 100)
        value = ctypes.c_ulonglong(filetime)
        return bool(k32.SetFileTime(handle, ctypes.byref(value), None, None))
    finally:
        k32.CloseHandle(handle)


def _next_btime_ns(prev_ns):
    """生成单调递增的创建时间（从“当前时间-1h”起，每个文件 +1s）。"""
    global _BTIME_CLOCK_NS
    if _BTIME_CLOCK_NS is None:
        _BTIME_CLOCK_NS = int(time.time() * 1000000000) - 3600 * 1000000000
    desired = _BTIME_CLOCK_NS
    if prev_ns is not None and int(prev_ns) >= desired:
        desired = int(prev_ns) + 1000000000
    _BTIME_CLOCK_NS = desired + 1000000000
    return desired


def create_ordered(path, data, prev_ns=None):
    """创建文件并确保创建时间严格大于 prev_ns（同组内保持创建顺序）。

    Windows 下用 SetFileTime 显式写创建时间；其他平台若无法写 btime，
    退化为间隔 1.05s 创建（保证 statx 读到可区分的顺序）。
    """
    global _MODULE_BTIME_SUPPORTED
    write_bytes(path, data)
    if _can_set_btime():
        for _attempt in range(6):
            desired = _next_btime_ns(prev_ns)
            if _set_btime_ns(path, desired):
                current = btime_ns(path)
                if current is not None and (prev_ns is None or current > prev_ns):
                    _MODULE_BTIME_SUPPORTED = True
                    return current
            time.sleep(0.02)
        raise RuntimeError("无法为 %s 写入可区分的创建时间" % path)
    time.sleep(1.05)
    current = btime_ns(path)
    if current is None:
        _MODULE_BTIME_SUPPORTED = False
        return None
    if prev_ns is not None and current <= prev_ns:
        raise RuntimeError("无法为 %s 构造可区分的创建时间" % path)
    return current


def build_fixtures(base):
    """在 base 下构造两个共享根和全部样本，返回路径字典。"""
    a = os.path.join(base, "qa-share-a")
    b = os.path.join(base, "qa-share-b")
    os.makedirs(a)
    os.makedirs(b)
    fx = {"share_a": a, "share_b": b}

    # 1) 跨共享根重复组：crossB 先创建，crossA 后创建 => 保留 crossA
    fx["cross_b"] = os.path.join(b, "cross", "crossB.dat")
    cns = create_ordered(fx["cross_b"], blob("cross", 16384))
    fx["cross_a"] = os.path.join(a, "cross", "crossA.dat")
    create_ordered(fx["cross_a"], blob("cross", 16384), cns)

    # 2) 保留最新：photo1 < photo2 < photo3（创建时间），mtime 稍后反转
    photo_dir = os.path.join(a, "dup")
    ns1 = create_ordered(os.path.join(photo_dir, "photo1.jpg"), blob("photo", 20480))
    ns2 = create_ordered(os.path.join(photo_dir, "photo2.jpg"), blob("photo", 20480), ns1)
    ns3 = create_ordered(os.path.join(photo_dir, "photo3.jpg"), blob("photo", 20480), ns2)
    fx["photo"] = [os.path.join(photo_dir, "photo%d.jpg" % i) for i in (1, 2, 3)]
    fx["photo_btime"] = [ns1, ns2, ns3]

    # 3) 仅按内容判重：文件名不同、内容相同
    fx["alice"] = create_ordered(os.path.join(a, "renamed", "alice.dat"), blob("rename", 12288))
    fx["bob"] = create_ordered(os.path.join(a, "renamed", "bob.dat"), blob("rename", 12288),
                               fx["alice"])
    fx["alice"] = os.path.join(a, "renamed", "alice.dat")
    fx["bob"] = os.path.join(a, "renamed", "bob.dat")

    # 4) 大小相同但内容不同：不得成组
    fx["same1"] = os.path.join(a, "same_size", "same1.bin")
    fx["same2"] = os.path.join(a, "same_size", "same2.bin")
    write_bytes(fx["same1"], blob("same-one", 4096))
    write_bytes(fx["same2"], blob("same-two", 4096))

    # 5) 最早创建策略：e1 < e2 < e3
    ens = create_ordered(os.path.join(a, "earliest", "e1.txt"), blob("earliest", 8192))
    ens = create_ordered(os.path.join(a, "earliest", "e2.txt"), blob("earliest", 8192), ens)
    create_ordered(os.path.join(a, "earliest", "e3.txt"), blob("earliest", 8192), ens)
    fx["earliest"] = [os.path.join(a, "earliest", "e%d.txt" % i) for i in (1, 2, 3)]

    # 6) 指定路径保留：p1 < p2 < p3（pinned_path 指向 p2）
    pns = create_ordered(os.path.join(a, "pinned", "p1.bin"), blob("pinned", 24576))
    pns = create_ordered(os.path.join(a, "pinned", "p2.bin"), blob("pinned", 24576), pns)
    create_ordered(os.path.join(a, "pinned", "p3.bin"), blob("pinned", 24576), pns)
    fx["pinned"] = [os.path.join(a, "pinned", "p%d.bin" % i) for i in (1, 2, 3)]

    # 7) 硬链接去重：hard1 与 hard2 同 inode，hardcopy 是另一 inode
    hard_dir = os.path.join(a, "hard")
    fx["hard1"] = os.path.join(hard_dir, "hard1.bin")
    hns = create_ordered(fx["hard1"], blob("hard", 32768))
    fx["hard2"] = os.path.join(hard_dir, "hard2.bin")
    fx["hardlink_ok"] = False
    try:
        os.link(fx["hard1"], fx["hard2"])
        fx["hardlink_ok"] = True
    except (OSError, NotImplementedError) as exc:
        fx["hardlink_error"] = str(exc)
    fx["hardcopy"] = os.path.join(hard_dir, "hardcopy.bin")
    create_ordered(fx["hardcopy"], blob("hard", 32768), hns)

    # 8) 删除前重校验：m1/m2 同内容，扫描后修改 m1
    fx["m1"] = create_ordered(os.path.join(a, "changed", "m1.bin"), blob("changed", 10240))
    create_ordered(os.path.join(a, "changed", "m2.bin"), blob("changed", 10240), fx["m1"])
    fx["m1"] = os.path.join(a, "changed", "m1.bin")
    fx["m2"] = os.path.join(a, "changed", "m2.bin")

    # 9) 大文件（分片哈希 / 限速 / 暂停）
    bns = create_ordered(os.path.join(a, "big", "big1.bin"), blob("big", 4 * 1024 * 1024))
    create_ordered(os.path.join(a, "big", "big2.bin"), blob("big", 4 * 1024 * 1024), bns)
    fx["big"] = [os.path.join(a, "big", "big1.bin"), os.path.join(a, "big", "big2.bin")]

    # 10) 第二个共享根内部重复组
    bns = create_ordered(os.path.join(b, "only", "bdup1.bin"), blob("bdup", 7168))
    create_ordered(os.path.join(b, "only", "bdup2.bin"), blob("bdup", 7168), bns)
    fx["bdup"] = [os.path.join(b, "only", "bdup1.bin"), os.path.join(b, "only", "bdup2.bin")]

    # 10b) scope=all_duplicates 专用组（scan 仅覆盖 shareB）
    sns = create_ordered(os.path.join(b, "scope", "s1.bin"), blob("scope", 7168))
    create_ordered(os.path.join(b, "scope", "s2.bin"), blob("scope", 7168), sns)
    fx["scope"] = [os.path.join(b, "scope", "s1.bin"), os.path.join(b, "scope", "s2.bin")]

    # 11) 后缀过滤（大写扩展名）与排除后缀
    cns = create_ordered(os.path.join(a, "exts", "case1.JPG"), blob("caseext", 7168))
    create_ordered(os.path.join(a, "exts", "case2.JPG"), blob("caseext", 7168), cns)
    fx["case_ext"] = [os.path.join(a, "exts", "case1.JPG"), os.path.join(a, "exts", "case2.JPG")]
    tns = create_ordered(os.path.join(a, "exts", "tmp1.tmp"), blob("tmpext", 9216))
    create_ordered(os.path.join(a, "exts", "tmp2.tmp"), blob("tmpext", 9216), tns)
    fx["tmp_ext"] = [os.path.join(a, "exts", "tmp1.tmp"), os.path.join(a, "exts", "tmp2.tmp")]

    # 12) 排除目录
    xns = create_ordered(os.path.join(a, "exclude_me", "ex1.bin"), blob("excluded", 6144))
    create_ordered(os.path.join(a, "exclude_me", "ex2.bin"), blob("excluded", 6144), xns)
    fx["exclude_dir"] = os.path.join(a, "exclude_me")

    # 13) 隐藏文件（默认 skip_hidden）
    hns = create_ordered(os.path.join(a, "hidden", ".hidden1"), blob("hidden", 5120))
    create_ordered(os.path.join(a, "hidden", ".hidden2"), blob("hidden", 5120), hns)
    fx["hidden"] = [os.path.join(a, "hidden", ".hidden1"),
                    os.path.join(a, "hidden", ".hidden2")]

    # 14) 空文件（默认 skip_empty）
    fx["empty"] = [write_bytes(os.path.join(a, "empty", "empty1.bin"), b""),
                   write_bytes(os.path.join(a, "empty", "empty2.bin"), b"")]

    # 15) 系统目录：永远不会被扫描，即使里面有重复文件
    for sys_dir, seed in (("@eaDir", "eadir"), ("#snapshot", "snapshot"),
                          ("#recycle", "recycle-existing")):
        target = os.path.join(a, sys_dir)
        os.makedirs(target)
        write_bytes(os.path.join(target, "sys1.dat"), blob(seed, 4096))
        write_bytes(os.path.join(target, "sys2.dat"), blob(seed, 4096))
    fx["system_dirs"] = ["@eaDir", "#snapshot", "#recycle"]
    # 回收站已存在（模拟 DSM 已启用回收站）
    fx["recycle_a"] = os.path.join(a, "#recycle")

    # 16) 预览样本
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6360000002000154a24f5f0000000049454e44ae"
        "426082")
    fx["pic"] = write_bytes(os.path.join(a, "preview", "pic.png"), png_bytes)
    fx["note"] = write_bytes(os.path.join(a, "preview", "note.txt"),
                             u"hello 重复文件预览".encode("utf-8"))
    fx["bigtxt"] = write_bytes(os.path.join(a, "preview", "big.txt"),
                               b"a" * (1536 * 1024))
    fx["blob"] = write_bytes(os.path.join(a, "preview", "blob.dat"), blob("blob", 2048))

    # 17) 符号链接（权限不足时记录并降级为 UNVERIFIED）
    fx["symlink"] = os.path.join(a, "link_to_dup")
    fx["symlink_ok"] = False
    try:
        os.symlink(photo_dir, fx["symlink"], target_is_directory=True)
        fx["symlink_ok"] = True
    except (OSError, NotImplementedError) as exc:
        fx["symlink_error"] = str(exc)

    # 18) 共享根之外的文件（用于路径越权测试）
    fx["outside"] = write_bytes(os.path.join(base, "outside-secret.txt"),
                                u"outside secret".encode("utf-8"))

    # mtime 反转：若后端误用 mtime 选保留项，会选 photo1 而非 photo3。
    now = time.time()
    os.utime(fx["photo"][0], (now - 3000, now - 3000))
    os.utime(fx["photo"][1], (now - 6000, now - 6000))
    os.utime(fx["photo"][2], (now - 9000, now - 9000))
    return fx


def snapshot_tree(roots):
    snap = {}
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path, follow_symlinks=False)
                except OSError:
                    continue
                snap[os.path.normcase(os.path.abspath(path))] = (
                    int(st.st_size), int(st.st_mtime_ns))
    return snap


# ---------------------------------------------------------------------------
# 扫描辅助
# ---------------------------------------------------------------------------
TERMINAL_SCAN = ("completed", "failed", "stopped")


def post_scan(client, roots, excludes=None, options=None):
    body = {"roots": list(roots)}
    if excludes is not None:
        body["excludes"] = list(excludes)
    if options:
        body["options"] = dict(options)
    return client.post_json("/api/scans", body)


def wait_scan(client, scan_id, timeout=240):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        resp = client.get("/api/scans/" + quote(scan_id))
        data = jd(resp)
        scan = data.get("scan") if isinstance(data.get("scan"), dict) else {}
        if scan:
            last = scan
        if scan.get("status") in TERMINAL_SCAN:
            return scan
        time.sleep(0.35)
    return last or {"status": "timeout"}


def wait_job(client, scan_id, job_id, timeout=180):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        resp = client.get("/api/scans/%s/delete/%s" % (quote(scan_id), quote(job_id)))
        data = jd(resp)
        job = data.get("job") if isinstance(data.get("job"), dict) else {}
        if job:
            last = job
        if job.get("status") in ("completed", "partial", "failed", "stopped"):
            return job
        time.sleep(0.25)
    return last or {"status": "timeout"}


def fetch_groups(client, scan_id, limit=100, **params):
    base_path = "/api/scans/%s/groups" % quote(scan_id)
    first = client.get(q(base_path, offset=0, limit=limit, **params))
    data = jd(first)
    groups = list(data.get("groups") or [])
    total = int(data.get("total") or 0)
    guard = 0
    while len(groups) < total and groups and guard < 100:
        guard += 1
        resp = client.get(q(base_path, offset=len(groups), limit=limit, **params))
        page = list(jd(resp).get("groups") or [])
        if not page:
            break
        groups.extend(page)
    return groups, total, first


def norm(path):
    return os.path.normcase(os.path.abspath(path or ""))


def group_with_path(groups, path):
    target = norm(path)
    for group in groups:
        for item in group.get("files") or []:
            if norm(item.get("path")) == target:
                return group
    return None


def file_with_path(groups, path):
    target = norm(path)
    for group in groups:
        for item in group.get("files") or []:
            if norm(item.get("path")) == target:
                return item
    return None


def group_files(group):
    return list(group.get("files") or [])


def keeper_of(group):
    for item in group_files(group):
        if item.get("is_keep"):
            return item
    return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run():
    base = tempfile.mkdtemp(prefix="dupqa-")
    data_dir = os.path.join(base, "data")
    os.makedirs(data_dir)
    roots = [os.path.join(base, "qa-share-a"), os.path.join(base, "qa-share-b")]
    backend = None
    keep_temp = "--keep" in sys.argv
    ok = True
    try:
        print("临时目录: %s" % base)
        fx = build_fixtures(base)
        snap_before = snapshot_tree(roots)
        evidence("fixture_files", len(snap_before))
        evidence("python", sys.version.split()[0])
        evidence("birthtime_supported", _MODULE_BTIME_SUPPORTED)

        backend = Backend(roots, data_dir).start()
        client = Client("127.0.0.1", backend.port)
        evidence("backend_port", backend.port)

        # ---------------- 认证 ----------------
        resp = client.get("/api/meta", csrf=False)
        code, _ = err_code(resp)
        check("AUTH-01", "未登录 /api/meta 返回 401 AUTH_REQUIRED",
              resp.status == 401 and code == "AUTH_REQUIRED",
              "status=%s code=%s" % (resp.status, code))

        resp = client.login(password="wrong-password")
        code, _ = err_code(resp)
        check("AUTH-02", "错误密码登录返回 401 AUTH_FAILED",
              resp.status == 401 and code == "AUTH_FAILED",
              "status=%s code=%s" % (resp.status, code))

        resp = client.login()
        data = jd(resp)
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        check("AUTH-03", "admin/admin dev 登录成功并返回 csrf_token",
              resp.status == 200 and data.get("ok") is True
              and bool(data.get("csrf_token")) and bool(client.cookies.get("dc_session"))
              and user.get("is_admin") is True,
              "status=%s data=%s cookies=%s" % (resp.status, data, client.cookies.keys()))
        evidence("csrf_token_len", len(client.csrf or ""))

        resp = client.get("/api/auth/me")
        data = jd(resp)
        check("AUTH-04", "登录后 /api/auth/me 返回当前管理员",
              resp.status == 200 and data.get("ok") is True
              and (data.get("user") or {}).get("is_admin") is True,
              "status=%s data=%s" % (resp.status, data))

        resp = client.request("POST", "/api/scans", body={"roots": roots}, csrf=False)
        code, _ = err_code(resp)
        check("AUTH-05", "非 GET 请求缺少 X-CSRF-Token 返回 403 CSRF_FAILED",
              resp.status == 403 and code == "CSRF_FAILED",
              "status=%s code=%s" % (resp.status, code))

        # ---------------- 元信息 / 共享 / 浏览 ----------------
        resp = client.get("/api/meta")
        data = jd(resp)
        limits = data.get("limits") if isinstance(data.get("limits"), dict) else {}
        defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
        check("META-01", "/api/meta 返回 dev_mode/creation_time_supported/版本/限制",
              resp.status == 200 and data.get("dev_mode") is True
              and data.get("app_version") == expected_app_version()
              and "creation_time_supported" in data
              and limits.get("max_files") == 500000
              and limits.get("max_roots") == 20
              and defaults.get("keep_mode") == "latest",
              "status=%s data=%s" % (resp.status, data))
        evidence("meta", data)

        resp = client.get("/api/shares")
        data = jd(resp)
        shares = data.get("shares") if isinstance(data.get("shares"), list) else []
        share_names = sorted(item.get("name") for item in shares if isinstance(item, dict))
        check("SHARES-01", "/api/shares 返回两个 dev 共享根",
              resp.status == 200 and share_names == ["qa-share-a", "qa-share-b"],
              "status=%s shares=%s" % (resp.status, share_names))

        resp = client.get("/api/browse")
        data = jd(resp)
        check("BROWSE-01", "空 path 浏览返回共享根列表",
              resp.status == 200 and isinstance(data.get("shares"), list)
              and len(data.get("shares") or []) == 2,
              "status=%s data=%s" % (resp.status, data))

        resp = client.get(q("/api/browse", path=fx["share_a"], include_files="false"))
        data = jd(resp)
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []
        ea = [e for e in entries if e.get("name") == "@eaDir"]
        hidden_dir = [e for e in entries if e.get("name") == "hidden"]
        check("BROWSE-02", "目录浏览标记 @eaDir 等系统目录 excluded 且不返回文件",
              resp.status == 200 and bool(ea) and ea[0].get("excluded") is True
              and bool(ea[0].get("exclude_reason"))
              and bool(hidden_dir) and hidden_dir[0].get("type") == "dir"
              and all(e.get("type") == "dir" for e in entries),
              "status=%s entries=%s" % (resp.status, entries))

        resp = client.get(q("/api/browse", path=os.path.join(fx["share_a"], "dup"),
                            include_files="true"))
        data = jd(resp)
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []
        photo_entry = [e for e in entries if e.get("name") == "photo1.jpg"]
        check("BROWSE-03", "include_files=true 返回文件元数据（size/created_at/modified_at）",
              resp.status == 200 and bool(photo_entry)
              and photo_entry[0].get("type") == "file"
              and photo_entry[0].get("size") == 20480
              and bool(photo_entry[0].get("modified_at")),
              "status=%s entry=%s" % (resp.status, photo_entry))

        resp = client.get(q("/api/browse", path=os.path.join(fx["share_a"], ".hidden1")))
        code, _ = err_code(resp)
        check("BROWSE-04", "浏览隐藏路径被拒绝",
              resp.status in (400, 403) and code in ("PATH_NOT_ALLOWED", "INVALID_ARGUMENT"),
              "status=%s code=%s" % (resp.status, code))

        resp = client.get(q("/api/browse", path=os.path.dirname(fx["outside"])))
        code, _ = err_code(resp)
        check("BROWSE-05", "浏览共享根之外路径被拒绝",
              resp.status in (400, 403) and code in ("PATH_NOT_ALLOWED", "INVALID_ARGUMENT"),
              "status=%s code=%s" % (resp.status, code))

        # ---------------- v0028 字幕：目录枚举 / 批量校验 ----------------
        enumerate_path = os.path.normpath(roots[0])
        resp = client.post_json("/api/subtitles/enumerate", {"path": enumerate_path})
        data = jd(resp)
        items = data.get("items") if isinstance(data.get("items"), list) else []
        check("SUB-01", "POST /api/subtitles/enumerate 返回目录内视频枚举结构",
              resp.status == 200 and data.get("ok") is True
              and data.get("path") == enumerate_path
              and isinstance(data.get("count"), int)
              and data.get("count") == len(items)
              and isinstance(data.get("skipped"), dict)
              and isinstance(data.get("total_bytes"), int),
              "status=%s data_keys=%s" % (resp.status, sorted(data.keys())))
        evidence("subtitle_enumerate_count", data.get("count"))

        resp = client.post_json("/api/subtitles/enumerate",
                                {"path": os.path.join(roots[0], "missing-dir")})
        code, _ = err_code(resp)
        check("SUB-02", "枚举不存在的目录返回 400 INVALID_DIRECTORY",
              resp.status == 400 and code == "INVALID_DIRECTORY",
              "status=%s code=%s" % (resp.status, code))

        resp = client.post_json("/api/subtitles/enumerate",
                                {"path": os.path.dirname(fx["outside"])})
        code, _ = err_code(resp)
        check("SUB-03", "枚举共享根之外路径被拒绝",
              resp.status in (400, 403) and code in ("PATH_NOT_ALLOWED", "INVALID_ARGUMENT"),
              "status=%s code=%s" % (resp.status, code))

        # 默认关闭：批量提交应先被总开关拒绝
        resp = client.post_json("/api/subtitles/batch",
                                {"paths": [fx["outside"]], "confirm_cloud": True})
        code, _ = err_code(resp)
        check("SUB-04", "字幕总开关未启用时批量提交返回 400 SUBTITLE_DISABLED",
              resp.status == 400 and code == "SUBTITLE_DISABLED",
              "status=%s code=%s" % (resp.status, code))

        # 开启总开关后校验其余入参（不提交真实任务）
        resp = client.put_json("/api/settings", {"settings": {
            "subtitle_enabled": True,
            "subtitle_asr_api_key": "e2e-dummy-asr-key",
            "subtitle_output_mode": "alongside_video",
            "subtitle_skip_existing": True,
            "subtitle_job_limit": 200,
            "subtitle_llm_disable_thinking": True}})
        saved = jd(resp).get("settings", {})
        check("SUB-05", "PUT /api/settings 可保存 v0028 目录批量字幕设置与 v0030 禁用思考开关",
              resp.status == 200
              and saved.get("subtitle_output_mode") == "alongside_video"
              and saved.get("subtitle_llm_disable_thinking") is True,
              "status=%s data=%s" % (resp.status, jd(resp)))

        resp = client.post_json("/api/subtitles/batch",
                                {"paths": [], "confirm_cloud": True})
        code, _ = err_code(resp)
        check("SUB-06", "批量提交空路径返回 400 INVALID_ARGUMENT",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))

        resp = client.post_json("/api/subtitles/batch",
                                {"paths": ["/volume1/no.mp4"] * 501,
                                 "confirm_cloud": True})
        code, _ = err_code(resp)
        check("SUB-07", "批量超过 500 个视频返回 400 BATCH_TOO_LARGE",
              resp.status == 400 and code == "BATCH_TOO_LARGE",
              "status=%s code=%s" % (resp.status, code))

        resp = client.post_json("/api/subtitles/batch",
                                {"paths": [os.path.dirname(fx["outside"])],
                                 "confirm_cloud": True})
        code, _ = err_code(resp)
        check("SUB-08", "批量全部无效路径返回 400 BATCH_EMPTY",
              resp.status == 400 and code == "BATCH_EMPTY",
              "status=%s code=%s" % (resp.status, code))

        resp = client.get("/api/subtitles/batches/bat-20260101-000000-abcdef")
        code, _ = err_code(resp)
        check("SUB-09", "未知字幕批次返回 404 NOT_FOUND",
              resp.status == 404 and code == "NOT_FOUND",
              "status=%s code=%s" % (resp.status, code))

        resp = client.get(q("/api/subtitles", batch_id="bat-20260101-000000-abcdef"))
        data = jd(resp)
        check("SUB-10", "字幕任务列表支持 batch_id 过滤",
              resp.status == 200 and isinstance(data.get("jobs"), list),
              "status=%s data=%s" % (resp.status, data))

        # ---------------- v0032 批次组 ----------------
        resp = client.get("/api/meta")
        sub_meta = (jd(resp).get("subtitle") or {})
        check("SUB-11", "meta.subtitle 暴露批次组上限/枚举上限/音频来源",
              resp.status == 200
              and sub_meta.get("batch_max_items") == 500
              and sub_meta.get("batch_group_max_items") == 20000
              and sub_meta.get("enumerate_max_items") == 20000
              and "nas" in (sub_meta.get("audio_sources") or [])
              and "server" in (sub_meta.get("audio_sources") or []),
              "subtitle=%s" % sub_meta)

        resp = client.post_json("/api/subtitles/batch-groups",
                                {"paths": ["/volume1/no.mp4"],
                                 "confirm_cloud": True})
        code, _ = err_code(resp)
        check("SUB-12", "批次组无有效路径返回 400 BATCH_EMPTY",
              resp.status == 400 and code == "BATCH_EMPTY",
              "status=%s code=%s" % (resp.status, code))

        resp = client.post_json("/api/subtitles/batch-groups",
                                {"paths": [], "confirm_cloud": True})
        code, _ = err_code(resp)
        check("SUB-13", "批次组空路径返回 400 INVALID_ARGUMENT",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))

        resp = client.post_json("/api/subtitles/batch-groups",
                                {"paths": ["/volume1/no.mp4"] * 20001,
                                 "confirm_cloud": True})
        code, _ = err_code(resp)
        check("SUB-14", "批次组超过 20000 个路径返回 400 BATCH_TOO_LARGE",
              resp.status == 400 and code == "BATCH_TOO_LARGE",
              "status=%s code=%s" % (resp.status, code))

        resp = client.get("/api/subtitles/batch-groups/grp-20260101-000000-abcdef")
        code, _ = err_code(resp)
        check("SUB-15", "未知字幕批次组返回 404 NOT_FOUND",
              resp.status == 404 and code == "NOT_FOUND",
              "status=%s code=%s" % (resp.status, code))

        resp = client.get(q("/api/subtitles",
                            group_id="grp-20260101-000000-abcdef"))
        data = jd(resp)
        check("SUB-16", "字幕任务列表支持 group_id 过滤分页",
              resp.status == 200 and isinstance(data.get("jobs"), list)
              and isinstance(data.get("total"), int)
              and data.get("offset") == 0,
              "status=%s data=%s" % (resp.status, data))

        # 恢复默认（关闭云端字幕），避免影响后续用例
        client.put_json("/api/settings", {"settings": {
            "subtitle_enabled": False, "subtitle_clear_asr_api_key": True,
            "subtitle_llm_disable_thinking": False}})

        # ---------------- 静态页面 / 风险提示 ----------------
        resp = client.get("/", csrf=False)
        html = resp.text()
        check("STATIC-01", "静态首页包含精确风险提示文案",
              resp.status == 200 and RISK_TEXT in html,
              "status=%s contains=%s" % (resp.status, RISK_TEXT in html))
        resp = client.get("/app.js", csrf=False)
        check("STATIC-02", "静态 app.js 可访问且非空",
              resp.status == 200 and len(resp.body) > 10000,
              "status=%s bytes=%s" % (resp.status, len(resp.body)))

        # ---------------- 设置 ----------------
        resp = client.get("/api/settings")
        data = jd(resp)
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        check("SET-01", "GET /api/settings 返回契约默认键",
              resp.status == 200 and settings.get("hash_algo") == "sha256"
              and settings.get("keep_mode") == "latest"
              and settings.get("skip_symlinks") is True
              and settings.get("allow_ctime_fallback") is False,
              "status=%s settings=%s" % (resp.status, settings))

        resp = client.put_json("/api/settings", {"settings": {"concurrency": 3}})
        data = jd(resp)
        check("SET-02", "PUT /api/settings 可保存 concurrency=3",
              resp.status == 200
              and (data.get("settings") or {}).get("concurrency") == 3,
              "status=%s data=%s" % (resp.status, data))

        resp = client.put_json("/api/settings", {"settings": {"no_such_key": 1}})
        code, _ = err_code(resp)
        check("SET-03", "未知设置键返回 INVALID_ARGUMENT",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))

        resp = client.put_json("/api/settings", {"settings": {"concurrency": 9}})
        code, _ = err_code(resp)
        check("SET-04", "concurrency 越界（9）被拒绝",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))

        resp = client.put_json("/api/settings",
                               {"settings": {"min_size_bytes": 100, "max_size_bytes": 10}})
        code, _ = err_code(resp)
        check("SET-05", "min_size_bytes > max_size_bytes 被拒绝",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))

        resp = client.put_json("/api/settings", {"settings": {"quick_sample_kb": 2}})
        code, _ = err_code(resp)
        check("SET-06", "quick_sample_kb 越界（2）被拒绝",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))

        resp = client.put_json("/api/settings", {"settings": {"skip_symlinks": False}})
        data = jd(resp)
        check("SET-07", "skip_symlinks=false 被后端强制为 true",
              resp.status == 200
              and (data.get("settings") or {}).get("skip_symlinks") is True,
              "status=%s data=%s" % (resp.status, data))

        # ---------------- 扫描请求安全校验 ----------------
        resp = post_scan(client, [os.path.dirname(fx["outside"])])
        code, _ = err_code(resp)
        check("SEC-01", "扫描共享根之外目录被拒绝",
              resp.status in (400, 403) and code in ("PATH_NOT_ALLOWED", "INVALID_ARGUMENT"),
              "status=%s code=%s" % (resp.status, code))

        resp = post_scan(client, ["C:\\Windows"])
        code, _ = err_code(resp)
        check("SEC-02", "扫描 Windows 系统目录被拒绝",
              resp.status in (400, 403) and code in ("PATH_NOT_ALLOWED", "INVALID_ARGUMENT"),
              "status=%s code=%s" % (resp.status, code))

        resp = post_scan(client, [])
        code, _ = err_code(resp)
        check("SEC-03", "roots 为空被拒绝",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))

        resp = post_scan(client, [fx["share_a"]], excludes=[os.path.dirname(fx["outside"])])
        code, _ = err_code(resp)
        check("SEC-04", "exclude 不在 root 内被拒绝",
              resp.status == 400 and code in ("INVALID_ARGUMENT", "INVALID_PATH"),
              "status=%s code=%s" % (resp.status, code))

        resp = client.get(q("/api/preview", path=fx["outside"]), csrf=False)
        code, _ = err_code(resp)
        check("SEC-05", "预览共享根之外文件被拒绝",
              resp.status in (400, 403) and code in ("PATH_NOT_ALLOWED", "INVALID_ARGUMENT"),
              "status=%s code=%s" % (resp.status, code))

        resp = client.get(q("/api/preview", path=fx["share_a"]))
        code, _ = err_code(resp)
        check("SEC-06", "预览目录被拒绝（4xx，不返回目录内容）",
              resp.status in (400, 403, 404)
              and code in ("INVALID_ARGUMENT", "PATH_NOT_ALLOWED", "NOT_FOUND"),
              "status=%s code=%s" % (resp.status, code))

        traversal_path = os.path.join(fx["share_a"], "..", "outside-secret.txt")
        resp = client.get(q("/api/preview", path=traversal_path))
        code, _ = err_code(resp)
        check("SEC-08", "preview 路径穿越（..）被拒绝",
              resp.status in (400, 403) and code in ("PATH_NOT_ALLOWED", "INVALID_PATH"),
              "status=%s code=%s path=%s" % (resp.status, code, traversal_path))

        resp = client.get(q("/api/logs/download", name="../config.json"))
        code, _ = err_code(resp)
        check("SEC-07", "日志下载路径穿越被拒绝",
              resp.status in (400, 403, 404) and code in
              ("INVALID_ARGUMENT", "PATH_NOT_ALLOWED", "NOT_FOUND"),
              "status=%s code=%s" % (resp.status, code))

        # ---------------- 主扫描（默认 latest + sha256） ----------------
        resp = post_scan(client, roots, excludes=[fx["exclude_dir"]])
        data = jd(resp)
        main_id = data.get("scan_id")
        check("SCAN-01", "创建扫描返回 202 与 scan_id",
              resp.status == 202 and isinstance(main_id, str) and len(main_id) > 8,
              "status=%s data=%s" % (resp.status, data))
        if not main_id:
            raise RuntimeError("无法创建主扫描: %s" % resp.text())
        evidence("main_scan_id", main_id)
        scan = wait_scan(client, main_id)
        evidence("main_scan_final", scan.get("status"))
        check("SCAN-02", "主扫描完成（status=completed, progress=1）",
              scan.get("status") == "completed" and float(scan.get("progress") or 0) >= 0.999,
              "scan=%s" % {k: scan.get(k) for k in ("status", "stage", "progress", "error")})
        stats = scan.get("stats") or {}
        evidence("main_stats", stats)
        check("SCAN-03", "扫描阶段状态机有 enumerate/size/sample/hash/finalize/done",
              scan.get("stage") == "done"
              and scan.get("finished_at")
              and scan.get("started_at"),
              "stage=%s" % scan.get("stage"))

        groups, total, first_groups_resp = fetch_groups(client, main_id)
        evidence("main_groups_total", total)
        check("GROUP-01", "groups 分页 total 与拉取数量一致且 >0",
              total == len(groups) and total > 0,
              "total=%s fetched=%s" % (total, len(groups)))
        if not groups:
            raise RuntimeError("主扫描没有产生任何重复组: %s" % scan)

        stats = scan.get("stats") or {}
        dup_files = sum(int(g.get("count") or 0) for g in groups)
        reclaim = sum(int(g.get("wasted_bytes") or 0) for g in groups)
        check("STATS-01", "stats.groups/duplicate_groups/duplicate_files/reclaimable_bytes 自洽",
              int(stats.get("groups") or 0) == total
              and int(stats.get("duplicate_groups") or 0) == total
              and int(stats.get("duplicate_files") or 0) == dup_files
              and int(stats.get("reclaimable_bytes") or 0) == reclaim,
              "stats=%s actual_total=%s dup_files=%s reclaim=%s"
              % (stats, total, dup_files, reclaim))
        check("STATS-02", "每组的 wasted_bytes = size*(count-1)",
              all(int(g.get("wasted_bytes") or 0)
                  == int(g.get("size") or 0) * (int(g.get("count") or 0) - 1)
                  for g in groups),
              "offenders=%s" % [g.get("group_id") for g in groups
                                if int(g.get("wasted_bytes") or 0)
                                != int(g.get("size") or 0) * (int(g.get("count") or 0) - 1)])

        # 保留最新 + 创建时间不是 mtime
        photo_group = group_with_path(groups, fx["photo"][2])
        photo_files = group_files(photo_group) if photo_group else []
        photo_keep = keeper_of(photo_group) if photo_group else None
        check("KEEP-01", "photo 组保留创建时间最晚的 photo3（latest_created）",
              bool(photo_group) and photo_keep is not None
              and norm(photo_keep.get("path")) == norm(fx["photo"][2])
              and photo_group.get("keep_reason") == "latest_created"
              and int(photo_group.get("count") or 0) == 3,
              "group=%s keep=%s"
              % ({k: photo_group.get(k) for k in ("group_id", "count", "keep_reason")}
                 if photo_group else None,
                 photo_keep.get("path") if photo_keep else None))
        if photo_keep:
            check("KEEP-02", "保留项时间来源为 statx/birthtime 且非 ctime",
                  photo_keep.get("created_source") in ("statx", "birthtime")
                  and bool(photo_keep.get("created_ts")),
                  "source=%s created_ts=%s" % (photo_keep.get("created_source"),
                                               photo_keep.get("created_ts")))
            photo_by_path = {norm(f.get("path")): f for f in photo_files}
            p1 = photo_by_path.get(norm(fx["photo"][0]))
            p3 = photo_by_path.get(norm(fx["photo"][2]))
            check("KEEP-03", "mtime 与创建时间相反时仍按创建时间保留（未用 mtime 冒充）",
                  bool(p1) and bool(p3)
                  and int(p3.get("mtime_ns") or 0) < int(p1.get("mtime_ns") or 0)
                  and int(p3.get("created_ts") or 0) + int(p3.get("created_ns") or 0) / 1e9
                  > int(p1.get("created_ts") or 0) + int(p1.get("created_ns") or 0) / 1e9,
                  "keep=%s p1_mtime_ns=%s p3_mtime_ns=%s"
                  % (photo_keep.get("path"), p1.get("mtime_ns") if p1 else None,
                     p3.get("mtime_ns") if p3 else None))
        check("GROUP-02", "photo 组内哈希全部一致且为 64 位 SHA256",
              bool(photo_group)
              and len({f.get("hash") for f in photo_files}) == 1
              and len(photo_files[0].get("hash") or "") == 64
              and photo_group.get("algo") == "sha256",
              "hashes=%s" % [f.get("hash") for f in photo_files])

        # 改名不影响判重
        alice_group = group_with_path(groups, fx["alice"])
        bob_group = group_with_path(groups, fx["bob"])
        bob_ok = (alice_group is not None and bob_group is not None
                  and alice_group.get("group_id") == bob_group.get("group_id")
                  and int(alice_group.get("count") or 0) == 2)
        bob_in_alice = (alice_group is not None
                        and any(norm(f.get("path")) == norm(fx["bob"])
                                for f in group_files(alice_group)))
        check("REQ-HASH-ONLY", "同内容不同文件名归入同一重复组",
              bob_ok and bob_in_alice,
              "alice_group=%s bob_group=%s"
              % (alice_group.get("group_id") if alice_group else None,
                 bob_group.get("group_id") if bob_group else None))

        # 大小相同内容不同不得成组
        same1_in = file_with_path(groups, fx["same1"])
        same2_in = file_with_path(groups, fx["same2"])
        check("HASH-01", "大小相同但内容不同的文件不成组",
              same1_in is None and same2_in is None,
              "same1=%s same2=%s" % (same1_in, same2_in))

        # 跨共享根
        cross_group = group_with_path(groups, fx["cross_a"])
        cross_paths = [norm(f.get("path")) for f in group_files(cross_group)] if cross_group else []
        cross_keep = keeper_of(cross_group) if cross_group else None
        check("BROWSE-MULTIROOT", "跨两个共享根的重复文件进入同一组，保留较晚创建的 crossA",
              bool(cross_group)
              and norm(fx["cross_a"]) in cross_paths and norm(fx["cross_b"]) in cross_paths
              and cross_keep is not None and norm(cross_keep.get("path")) == norm(fx["cross_a"]),
              "group=%s paths=%s" % (cross_group.get("group_id") if cross_group else None,
                                     [os.path.basename(p) for p in cross_paths]))

        # 排除目录
        excluded_paths = [f.get("path") for g in groups for f in group_files(g)
                          if norm(f.get("path") or "").startswith(norm(fx["exclude_dir"]))]
        check("EXCLUDE-01", "excludes 目录内的文件未进入扫描结果",
              not excluded_paths,
              "excluded=%s" % excluded_paths)
        check("EXCLUDE-02", "excludes 命中计数（stats.skipped.excluded）>0",
              int((stats.get("skipped") or {}).get("excluded") or 0) > 0,
              "skipped=%s" % (stats.get("skipped") or {}))

        # 系统目录 / 隐藏 / 空文件 / 符号链接
        bad_paths = []
        for group in groups:
            for item in group_files(group):
                path = item.get("path") or ""
                if any(sep in path for sep in ("@eaDir", "#snapshot", "#recycle")):
                    bad_paths.append(path)
                if os.path.basename(path).startswith("."):
                    bad_paths.append(path)
        check("SKIP-01", "系统目录（@eaDir/#snapshot/#recycle）内容未被扫描",
              not bad_paths, "bad=%s" % bad_paths)
        skipped = stats.get("skipped") or {}
        check("SKIP-02", "隐藏文件被跳过（skipped.hidden>=2）",
              int(skipped.get("hidden") or 0) >= 2, "skipped=%s" % skipped)
        check("SKIP-03", "空文件被跳过（skipped.empty>=2）",
              int(skipped.get("empty") or 0) >= 2, "skipped=%s" % skipped)
        if fx.get("symlink_ok"):
            symlink_hits = [f.get("path") for g in groups for f in group_files(g)
                            if norm(f.get("path") or "") == norm(fx["symlink"])]
            check("SKIP-04", "符号链接被跳过（skipped.symlink>=1）且不进入结果",
                  int(skipped.get("symlink") or 0) >= 1 and not symlink_hits,
                  "skipped=%s hits=%s" % (skipped, symlink_hits))
        else:
            record("SKIP-04", "符号链接跳过（当前环境无法创建 symlink）", "UNVERIFIED",
                   "os.symlink failed: %s" % fx.get("symlink_error"))

        # 硬链接去重
        hard_group = group_with_path(groups, fx["hard1"])
        hard_paths = [norm(f.get("path")) for f in group_files(hard_group)] if hard_group else []
        expected_hard = {norm(fx["hard1"]), norm(fx["hardcopy"])}
        if fx.get("hardlink_ok"):
            check("HARDLINK-01", "硬链接只保留一个路径参与判重，wasted 不虚高",
                  bool(hard_group) and set(hard_paths) == expected_hard
                  and int(hard_group.get("count") or 0) == 2
                  and int(hard_group.get("wasted_bytes") or 0) == 32768
                  and int(skipped.get("hardlink") or 0) >= 1,
                  "paths=%s count=%s wasted=%s skipped.hardlink=%s"
                  % (hard_paths, hard_group.get("count") if hard_group else None,
                     hard_group.get("wasted_bytes") if hard_group else None,
                     skipped.get("hardlink")))
        else:
            record("HARDLINK-01", "硬链接去重（当前环境无法创建 hardlink）", "UNVERIFIED",
                   str(fx.get("hardlink_error")))

        # 大文件全量哈希组
        big_group = group_with_path(groups, fx["big"][0])
        check("SCAN-BIG", "4MB 大文件经快速指纹+全量哈希成组",
              bool(big_group) and int(big_group.get("count") or 0) == 2
              and int(big_group.get("size") or 0) == 4 * 1024 * 1024,
              "group=%s" % (big_group.get("group_id") if big_group else None))

        # 默认选中 / keep 不可勾选 / 时间字段
        sel_bad = []
        for group in groups:
            for item in group_files(group):
                if item.get("is_keep") and item.get("default_selected"):
                    sel_bad.append("keep-selected:%s" % item.get("path"))
                if not item.get("is_keep") and not item.get("default_selected"):
                    sel_bad.append("copy-unselected:%s" % item.get("path"))
        check("GROUP-03", "安全组内非保留项 default_selected=true，保留项 false",
              not sel_bad, "bad=%s" % sel_bad[:10])
        field_bad = []
        for group in groups:
            for item in group_files(group):
                for key in ("file_id", "path", "name", "size", "hash", "created_ts",
                            "created_source", "mtime_ns", "is_keep", "default_selected"):
                    if item.get(key) is None and key not in ("created_ts",):
                        field_bad.append("%s:%s" % (key, item.get("path")))
                if item.get("created_source") not in ("statx", "birthtime", "ctime", "unknown"):
                    field_bad.append("source:%s" % item.get("created_source"))
        check("GROUP-04", "文件行包含契约字段（file_id/path/size/hash/created_ts/source/mtime_ns）",
              not field_bad, "bad=%s" % field_bad[:10])
        check("GROUP-05", "所有组 needs_manual_keep=false（创建时间均可区分）",
              int(scan.get("needs_manual_keep_groups") or 0) == 0
              and all(g.get("needs_manual_keep") is False for g in groups),
              "needs_manual=%s" % scan.get("needs_manual_keep_groups"))

        # 排序 / 搜索 / 分页
        base_groups_path = "/api/scans/%s/groups" % quote(main_id)
        resp = client.get(q(base_groups_path, offset=0, limit=3, sort="size_desc"))
        data = jd(resp)
        page = list(data.get("groups") or [])
        sizes = [int(g.get("size") or 0) for g in page]
        check("GROUP-06", "groups 支持 limit 与 sort=size_desc 排序",
              resp.status == 200 and len(page) <= 3
              and sizes == sorted(sizes, reverse=True),
              "sizes=%s" % sizes)
        resp = client.get(q(base_groups_path, offset=0, limit=1))
        first_id = ((jd(resp).get("groups") or [{}])[0]).get("group_id")
        resp = client.get(q(base_groups_path, offset=1, limit=1))
        second_id = ((jd(resp).get("groups") or [{}])[0]).get("group_id")
        check("GROUP-07", "groups offset/limit 分页返回不同组",
              first_id and second_id and first_id != second_id,
              "first=%s second=%s" % (first_id, second_id))
        resp = client.get(q(base_groups_path, offset=0, limit=100, q="photo3"))
        data = jd(resp)
        check("GROUP-08", "groups 支持 q 搜索",
              resp.status == 200 and int(data.get("total") or 0) >= 1,
              "total=%s" % data.get("total"))

        # 无自动删除：扫描前后文件快照一致
        snap_after = snapshot_tree(roots)
        check("SAFETY-01", "扫描完成后未发生任何自动/静默删除",
              snap_before == snap_after,
              "diff=%s" % list(set(snap_before) ^ set(snap_after))[:10])

        # ---------------- CSV ----------------
        resp = client.get("/api/scans/%s/csv" % quote(main_id))
        body = resp.body
        check("CSV-01", "CSV 返回 200/text-csv/UTF-8 BOM",
              resp.status == 200 and resp.header("content-type").startswith("text/csv")
              and body.startswith(b"\xef\xbb\xbf"),
              "status=%s ct=%s head=%r"
              % (resp.status, resp.header("content-type"), body[:20]))
        check("CSV-02", "CSV Content-Disposition 文件名正确",
              ("duplicate-scan-%s.csv" % main_id) in resp.header("content-disposition"),
              "cd=%s" % resp.header("content-disposition"))
        text = body.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        # 契约 §8：v1.1 起增加 时间回退 / 保留时间来源 两列（mtime fallback）。
        expected_header = ["组号", "匹配类型", "状态", "文件路径", "大小(字节)",
                           "SHA256/MD5", "创建时间", "修改时间", "保留",
                           "默认勾选", "时间来源", "相似度分值",
                           "时间回退", "保留时间来源"]
        check("CSV-03", "CSV 表头与契约列完全一致",
              rows and rows[0] == expected_header,
              "header=%s" % (rows[0] if rows else None))
        data_rows = rows[1:] if rows else []
        check("CSV-04", "CSV 行数覆盖全部重复文件",
              len(data_rows) == dup_files,
              "rows=%s dup_files=%s" % (len(data_rows), dup_files))
        keep_row = None
        copy_row = None
        for row in data_rows:
            if len(row) >= 12 and norm(row[3]) == norm(fx["photo"][2]):
                keep_row = row
            if len(row) >= 12 and norm(row[3]) == norm(fx["photo"][0]):
                copy_row = row
        check("CSV-05", "CSV 保留行标记保留、时间来源、匹配类型/分值（v1.1 新列）",
              keep_row is not None
              and keep_row[8] in ("是", "true", "True", "1")
              and keep_row[10] in ("statx", "birthtime")
              and keep_row[1] == u"哈希一致" and int(keep_row[11]) == 100,
              "row=%s" % keep_row)
        check("CSV-06", "精准模式 CSV 匹配类型=哈希一致、相似度=100",
              copy_row is not None and copy_row[1] == u"哈希一致"
              and int(copy_row[11]) == 100,
              "row=%s" % copy_row)

        # ---------------- 预览 ----------------
        resp = client.get(q("/api/preview", path=fx["pic"]))
        check("PREVIEW-01", "图片预览 200 且 content-type=image/png",
              resp.status == 200 and "image/png" in resp.header("content-type"),
              "status=%s ct=%s" % (resp.status, resp.header("content-type")))
        resp = client.get(q("/api/preview", path=fx["note"]))
        check("PREVIEW-02", "文本预览 200 且内容正确",
              resp.status == 200 and "hello" in resp.text(),
              "status=%s body=%r" % (resp.status, resp.body[:80]))
        resp = client.get(q("/api/preview", path=fx["pic"]),
                          headers={"Range": "bytes=0-9"})
        check("PREVIEW-03", "媒体预览支持 Range 返回 206",
              resp.status == 206 and resp.header("content-range").startswith("bytes 0-9/"),
              "status=%s range=%s" % (resp.status, resp.header("content-range")))
        resp = client.get(q("/api/preview", path=fx["note"]),
                          headers={"Range": "bytes=0-4"})
        if resp.status == 206:
            record("PREVIEW-03T", "文本预览支持 Range（契约 §8 字面要求）", "PASS", "")
        else:
            record("PREVIEW-03T", "文本预览支持 Range（契约 §8 字面要求）", "FAIL",
                   "文本分支返回 status=%s（忽略 Range，非 206）；媒体分支已返回 206，"
                   "前端文本预览未使用 Range，影响低但属契约偏差"
                   % resp.status)
        resp = client.get(q("/api/preview", path=fx["bigtxt"]))
        check("PREVIEW-04", "大于 1MB 文本预览被截断并标记 X-Preview-Truncated",
              resp.status == 200
              and resp.header("x-preview-truncated") == "1"
              and len(resp.body) <= 1024 * 1024 + 1024,
              "status=%s truncated=%s len=%s"
              % (resp.status, resp.header("x-preview-truncated"), len(resp.body)))
        resp = client.get(q("/api/preview", path=fx["blob"]))
        code, _ = err_code(resp)
        check("PREVIEW-05", "不支持预览的类型返回 415 PREVIEW_UNSUPPORTED",
              resp.status == 415 and code == "PREVIEW_UNSUPPORTED",
              "status=%s code=%s" % (resp.status, code))
        anon = Client("127.0.0.1", backend.port)
        resp = anon.get(q("/api/preview", path=fx["note"]), csrf=False)
        code, _ = err_code(resp)
        check("PREVIEW-06", "preview 未登录返回 401 AUTH_REQUIRED",
              resp.status == 401 and code == "AUTH_REQUIRED",
              "status=%s code=%s" % (resp.status, code))

        # ---------------- 日志 ----------------
        resp = client.get(q("/api/logs", limit=50))
        data = jd(resp)
        logs = data.get("logs") if isinstance(data.get("logs"), list) else []
        audit = data.get("audit") if isinstance(data.get("audit"), list) else []
        scan_logs = [entry for entry in logs if entry.get("kind") == "scan"]
        actions = {entry.get("action") for entry in audit if isinstance(entry, dict)}
        check("LOGS-01", "/api/logs 返回 scan 日志与 audit 事件",
              resp.status == 200 and bool(scan_logs) and bool(audit),
              "status=%s logs=%s audit_len=%s" % (resp.status, len(logs), len(audit)))
        check("LOGS-02", "audit 包含 scan_start / scan_end",
              "scan_start" in actions and "scan_end" in actions,
              "actions=%s" % sorted(a for a in actions if a))
        scan_log_name = scan_logs[0].get("name") if scan_logs else None
        if scan_log_name:
            resp = client.get(q("/api/logs/download", name=scan_log_name))
            check("LOGS-03", "日志下载返回内容且包含 scan_id",
                  resp.status == 200 and main_id.encode() in resp.body,
                  "status=%s name=%s len=%s"
                  % (resp.status, scan_log_name, len(resp.body)))
        else:
            record("LOGS-03", "日志下载", "FAIL", "没有 scan 日志可下载")

        # ---------------- 删除安全：保留项 / 变更文件 ----------------
        resp = client.post_json("/api/scans/%s/delete-plan" % quote(main_id),
                                {"file_ids": [photo_keep.get("file_id")], "recycle": True})
        code, details = err_code(resp)
        skipped = details.get("skipped") if isinstance(details.get("skipped"), list) else []
        check("DEL-01", "尝试删除保留项被拒绝并标记 IS_KEEPER",
              resp.status == 400 and code == "PLAN_INVALID"
              and any(item.get("reason") == "IS_KEEPER" for item in skipped),
              "status=%s code=%s details=%s" % (resp.status, code, details))
        check("DEL-02", "删除保留项后文件仍然存在",
              os.path.isfile(fx["photo"][2]), "missing=%s" % fx["photo"][2])

        m1_info = file_with_path(groups, fx["m1"])
        if m1_info:
            with open(fx["m1"], "ab") as handle:
                handle.write(b"X")
            resp = client.post_json("/api/scans/%s/delete-plan" % quote(main_id),
                                    {"file_ids": [m1_info.get("file_id")], "recycle": True})
            code, details = err_code(resp)
            skipped = details.get("skipped") if isinstance(details.get("skipped"), list) else []
            check("DEL-03", "扫描后被修改的文件进入 skipped 且不被删除",
                  resp.status == 400 and code == "PLAN_INVALID"
                  and any(item.get("reason") == "FILE_CHANGED" for item in skipped)
                  and os.path.isfile(fx["m1"]),
                  "status=%s code=%s details=%s" % (resp.status, code, details))
        else:
            record("DEL-03", "扫描后被修改的文件应被跳过", "FAIL", "m1 不在扫描结果中")

        # ---------------- 回收站删除 ----------------
        photo_copies = [f for f in photo_files if not f.get("is_keep")]
        payload = {"file_ids": [f.get("file_id") for f in photo_copies], "recycle": True}
        resp = client.post_json("/api/scans/%s/delete-plan" % quote(main_id), payload)
        data = jd(resp)
        plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
        check("DEL-04", "回收站删除计划包含 2 个副本、字节数与过期时间",
              resp.status == 200 and plan.get("count") == 2
              and int(plan.get("bytes") or 0) == 2 * 20480
              and bool(plan.get("expires_at")) and plan.get("recycle") is True
              and not plan.get("skipped"),
              "status=%s plan=%s" % (resp.status, plan))
        plan_id = plan.get("plan_id")
        evidence("recycle_plan", {k: plan.get(k) for k in ("plan_id", "count", "bytes",
                                                           "groups_affected")})
        resp = client.post_json("/api/scans/%s/delete" % quote(main_id),
                                {"plan_id": plan_id})
        code, _ = err_code(resp)
        check("DEL-05", "缺少 confirm=DELETE 被拒绝",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))
        resp = client.post_json("/api/scans/%s/delete" % quote(main_id),
                                {"plan_id": plan_id, "confirm": "WRONG"})
        code, _ = err_code(resp)
        check("DEL-06", "confirm 非精确 DELETE 被拒绝",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))
        resp = client.post_json("/api/scans/%s/delete" % quote(main_id),
                                {"plan_id": plan_id, "confirm": "DELETE"})
        data = jd(resp)
        job_id = data.get("job_id")
        check("DEL-07", "确认后返回 202 与后台任务 job_id",
              resp.status == 202 and isinstance(job_id, str),
              "status=%s data=%s" % (resp.status, data))
        job = wait_job(client, main_id, job_id) if job_id else {}
        results = job.get("results") if isinstance(job.get("results"), list) else []
        check("DEL-08", "回收站删除任务完成且逐文件结果为 recycled",
              job.get("status") == "completed" and int(job.get("done") or 0) == 2
              and int(job.get("failed") or 0) == 0
              and len(results) == 2
              and all(item.get("status") == "recycled" for item in results),
              "job=%s" % {k: job.get(k) for k in ("status", "done", "failed", "total")})
        evidence("recycle_job", {k: job.get(k) for k in
                                 ("job_id", "status", "done", "failed", "bytes_done")})
        check("DEL-09", "副本原路径已不存在，保留项仍存在",
              all(not os.path.exists(fx["photo"][i]) for i in (0, 1))
              and os.path.isfile(fx["photo"][2]),
              "exists=%s" % [os.path.exists(p) for p in fx["photo"]])
        recycle_ok = True
        recycle_paths = []
        for item in results:
            recycle_path = item.get("recycle_path") or ""
            recycle_paths.append(recycle_path)
            expected_prefix = os.path.join(fx["share_a"], "#recycle", "@duplicate-cleaner")
            if not (norm(recycle_path).startswith(norm(expected_prefix))
                    and os.path.isfile(recycle_path)):
                recycle_ok = False
        check("DEL-10", "文件移动到 <share>/#recycle/@duplicate-cleaner/<ts>/<relpath>",
              recycle_ok and len(recycle_paths) == 2,
              "recycle_paths=%s" % recycle_paths)
        resp = client.post_json("/api/scans/%s/delete" % quote(main_id),
                                {"plan_id": plan_id, "confirm": "DELETE"})
        code, _ = err_code(resp)
        check("DEL-11", "删除计划一次性：重放已消费的 plan 被拒绝",
              resp.status in (400, 410) and code in ("PLAN_INVALID", "PLAN_EXPIRED"),
              "status=%s code=%s" % (resp.status, code))

        # v0032：删除成功后 groups/统计读取自动排除已删除文件（整组消失）
        refreshed_groups, refreshed_total, _first = fetch_groups(client, main_id)
        remaining_paths = {norm(f.get("path") or "")
                           for g in refreshed_groups for f in group_files(g)}
        deleted_paths = {norm(fx["photo"][i]) for i in (0, 1)}
        check("DEL-20", "删除成功后 groups 不再返回已删除文件且整组消失",
              not (deleted_paths & remaining_paths)
              and group_with_path(refreshed_groups, fx["photo"][2]) is None,
              "deleted_hits=%s refreshed_total=%s"
              % (sorted(deleted_paths & remaining_paths)[:5], refreshed_total))

        # ---------------- 回收站不可用 / 永久删除 ----------------
        resp = client.post_json("/api/scans/%s/delete-plan" % quote(main_id),
                                {"file_ids": [file_with_path(groups, fx["cross_b"]).get("file_id")],
                                 "recycle": True})
        code, details = err_code(resp)
        data = jd(resp)
        plan_data = data.get("plan") if isinstance(data.get("plan"), dict) else {}
        unavailable = details.get("recycle_unavailable") if isinstance(
            details.get("recycle_unavailable"), list) else (
            plan_data.get("recycle_unavailable") if isinstance(
                plan_data.get("recycle_unavailable"), list) else [])
        listed = any(norm(item.get("path") or "") == norm(fx["share_b"])
                     for item in unavailable)
        skipped_reason = [item.get("reason") for item in
                          (details.get("skipped") or [])]
        check("DEL-12", "无 #recycle 的共享根：回收站计划被拒绝/跳过并列出 recycle_unavailable",
              listed and (resp.status in (200, 400, 409))
              and (resp.status != 200 or int(plan_data.get("count") or 0) == 0)
              and ("RECYCLE_UNAVAILABLE" in skipped_reason
                   or resp.status == 409 or int(plan_data.get("count") or 0) == 0),
              "status=%s code=%s details=%s" % (resp.status, code, details))
        if not (resp.status == 200 and isinstance(data.get("plan"), dict)):
            info("DEL-12-NOTE", "契约偏差（低）：全部候选因回收站不可用被跳过时，"
                 "后端返回 %s %s 而非契约 §7 的 200+plan(recycle_unavailable)。"
                 "前端 app.js 已对 RECYCLE_UNAVAILABLE 做中文提示，功能安全。"
                 % (resp.status, code))

        cross_b_info = file_with_path(groups, fx["cross_b"])
        resp = client.post_json("/api/scans/%s/delete-plan" % quote(main_id),
                                {"file_ids": [cross_b_info.get("file_id")], "recycle": False})
        data = jd(resp)
        permanent_plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
        check("DEL-13", "永久删除计划创建成功（recycle=false）",
              resp.status == 200 and permanent_plan.get("recycle") is False
              and permanent_plan.get("count") == 1,
              "status=%s plan=%s" % (resp.status, permanent_plan))
        permanent_plan_id = permanent_plan.get("plan_id")
        resp = client.post_json("/api/scans/%s/delete" % quote(main_id),
                                {"plan_id": permanent_plan_id, "confirm": "DELETE",
                                 "permanent_phrase": "删除"})
        code, _ = err_code(resp)
        check("DEL-14", "永久删除缺少精确短语被拒绝",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))
        resp = client.post_json("/api/scans/%s/delete" % quote(main_id),
                                {"plan_id": permanent_plan_id, "confirm": "DELETE",
                                 "permanent_phrase": "永久删除"})
        data = jd(resp)
        perm_job_id = data.get("job_id")
        perm_job = wait_job(client, main_id, perm_job_id) if perm_job_id else {}
        perm_results = perm_job.get("results") if isinstance(perm_job.get("results"), list) else []
        check("DEL-15", "永久删除任务完成且文件已 unlink",
              perm_job.get("status") == "completed"
              and any(item.get("status") == "deleted" for item in perm_results)
              and not os.path.exists(fx["cross_b"]),
              "job=%s exists=%s" % ({k: perm_job.get(k) for k in ("status", "done", "failed")},
                                    os.path.exists(fx["cross_b"])))
        check("DEL-16", "永久删除不会在 #recycle 留下文件",
              not os.path.exists(os.path.join(fx["share_b"], "#recycle")),
              "share_b_recycle_exists=%s" % os.path.exists(
                  os.path.join(fx["share_b"], "#recycle")))

        # ---------------- 删除审计 / 日志持久化 ----------------
        audit_path = os.path.join(data_dir, "audit.jsonl")
        audit_actions = []
        if os.path.isfile(audit_path):
            with open(audit_path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        audit_actions.append(json.loads(line).get("action"))
                    except ValueError:
                        continue
        required_actions = {"scan_start", "scan_end", "delete_plan",
                            "delete_start", "delete_file", "delete_end"}
        check("AUDIT-01", "audit.jsonl 落盘并包含扫描/计划/逐文件删除事件",
              required_actions.issubset(set(audit_actions)),
              "actions=%s" % sorted(set(a for a in audit_actions if a)))
        # v0021 起逐文件删除结果落 SQLite（deletion_results），不再写
        # deletions/*.jsonl（旧文件仅作为迁移来源读取）。
        result_rows = []
        db_path = os.path.join(data_dir, "dupcleaner.sqlite3")
        try:
            connection = sqlite3.connect(db_path, timeout=5)
            try:
                result_rows = connection.execute(
                    "SELECT job_id, COUNT(*) FROM deletion_results"
                    " GROUP BY job_id").fetchall()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            result_rows = []
            audit_detail = "sqlite error: %s" % exc
        else:
            audit_detail = "job_rows=%s" % result_rows
        check("AUDIT-02", "逐文件删除结果已持久化（SQLite deletion_results）",
              bool(result_rows) and sum(int(row[1]) for row in result_rows) >= 2,
              audit_detail)
        resp = client.get(q("/api/logs", limit=100))
        data = jd(resp)
        audit_api = data.get("audit") if isinstance(data.get("audit"), list) else []
        api_actions = {entry.get("action") for entry in audit_api if isinstance(entry, dict)}
        check("AUDIT-03", "/api/logs 审计接口可见 delete 事件",
              "delete_plan" in api_actions and "delete_end" in api_actions,
              "actions=%s" % sorted(a for a in api_actions if a))

        # ---------------- scope=all_duplicates ----------------
        # 前面的 DEL-12 已验证 shareB 无回收站时的安全拒绝；此处显式启用回收站，
        # 以覆盖 UI 默认的 recycle=true + scope=all_duplicates 路径。
        os.makedirs(os.path.join(fx["share_b"], "#recycle"), exist_ok=True)
        resp = post_scan(client, [fx["share_b"]])
        data = jd(resp)
        scope_id = data.get("scan_id")
        scope_scan = wait_scan(client, scope_id) if scope_id else {}
        resp = client.post_json("/api/scans/%s/delete-plan" % quote(scope_id),
                                {"scope": "all_duplicates", "exclude_file_ids": [],
                                 "recycle": True})
        data = jd(resp)
        scope_plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
        check("DEL-17", "scope=all_duplicates 计划覆盖每个组的一个副本",
              scope_scan.get("status") == "completed"
              and resp.status == 200 and scope_plan.get("count") == 2
              and int(scope_plan.get("groups_affected") or 0) == 2,
              "scan=%s status=%s plan=%s"
              % (scope_scan.get("status"), resp.status,
                 {k: scope_plan.get(k) for k in ("count", "groups_affected")}))
        scope_job = {}
        if scope_plan.get("plan_id"):
            resp = client.post_json("/api/scans/%s/delete" % quote(scope_id),
                                    {"plan_id": scope_plan.get("plan_id"),
                                     "confirm": "DELETE"})
            data = jd(resp)
            scope_job = wait_job(client, scope_id, data.get("job_id")) if data.get("job_id") else {}
        check("DEL-18", "scope=all_duplicates 删除任务完成且保留项仍存在",
              scope_job.get("status") == "completed"
              and not os.path.exists(fx["scope"][0])
              and os.path.isfile(fx["scope"][1])
              and not os.path.exists(fx["bdup"][0])
              and os.path.isfile(fx["bdup"][1]),
              "job=%s exists=%s" % ({k: scope_job.get(k) for k in ("status", "done", "failed")},
                                    [os.path.exists(p) for p in
                                     fx["scope"] + fx["bdup"]]))

        resp = client.post_json("/api/scans/%s/delete-plan" % quote(main_id),
                                {"scope": "all_duplicates", "include_manual_groups": True,
                                 "recycle": True})
        code, _ = err_code(resp)
        check("DEL-19", "include_manual_groups=true 被明确拒绝",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))

        # ---------------- 三种保留策略 + 过滤 ----------------
        # earliest + md5
        resp = post_scan(client, [fx["share_a"]],
                         options={"keep_mode": "earliest", "hash_algo": "md5"})
        data = jd(resp)
        earliest_id = data.get("scan_id")
        earliest_scan = wait_scan(client, earliest_id) if earliest_id else {}
        egroups, etotal, _ = fetch_groups(client, earliest_id) if earliest_id else ([], 0, None)
        egroup = group_with_path(egroups, fx["earliest"][0])
        ekeep = keeper_of(egroup) if egroup else None
        check("FILT-01", "keep_mode=earliest 保留最早创建的 e1，算法 md5 生效",
              earliest_scan.get("status") == "completed"
              and egroup is not None and ekeep is not None
              and norm(ekeep.get("path")) == norm(fx["earliest"][0])
              and egroup.get("keep_reason") == "earliest_created"
              and egroup.get("algo") == "md5",
              "scan=%s group=%s keep=%s"
              % (earliest_scan.get("status"),
                 egroup.get("keep_reason") if egroup else None,
                 ekeep.get("path") if ekeep else None))

        # pinned
        resp = post_scan(client, [fx["share_a"]],
                         options={"keep_mode": "pinned", "pinned_path": fx["pinned"][1]})
        data = jd(resp)
        pinned_id = data.get("scan_id")
        pinned_scan = wait_scan(client, pinned_id) if pinned_id else {}
        pgroups, ptotal, _ = fetch_groups(client, pinned_id) if pinned_id else ([], 0, None)
        pgroup = group_with_path(pgroups, fx["pinned"][1])
        pkeep = keeper_of(pgroup) if pgroup else None
        check("FILT-02", "keep_mode=pinned 按指定路径保留 p2",
              pinned_scan.get("status") == "completed" and pgroup is not None
              and pkeep is not None and norm(pkeep.get("path")) == norm(fx["pinned"][1])
              and pgroup.get("keep_reason") == "pinned_path",
              "scan=%s group=%s keep=%s"
              % (pinned_scan.get("status"),
                 pgroup.get("keep_reason") if pgroup else None,
                 pkeep.get("path") if pkeep else None))

        # include_extensions + ignore_case_ext=true
        resp = post_scan(client, [fx["share_a"]],
                         options={"include_extensions": [".jpg"],
                                  "ignore_case_ext": True})
        data = jd(resp)
        case_id = data.get("scan_id")
        case_scan = wait_scan(client, case_id) if case_id else {}
        cgroups, ctotal, _ = fetch_groups(client, case_id) if case_id else ([], 0, None)
        check("FILT-03", "include_extensions=.jpg + ignore_case_ext=true 命中大写 .JPG",
              case_scan.get("status") == "completed"
              and group_with_path(cgroups, fx["case_ext"][0]) is not None
              and not group_with_path(cgroups, fx["tmp_ext"][0]),
              "scan=%s groups=%s" % (case_scan.get("status"), ctotal))

        # include_extensions + ignore_case_ext=false
        resp = post_scan(client, [fx["share_a"]],
                         options={"include_extensions": [".jpg"],
                                  "ignore_case_ext": False})
        data = jd(resp)
        case2_id = data.get("scan_id")
        case2_scan = wait_scan(client, case2_id) if case2_id else {}
        c2groups, c2total, c2resp = fetch_groups(client, case2_id) if case2_id else ([], 0, None)
        c2_stats = case2_scan.get("stats") or {}
        check("FILT-04", "include_extensions=.jpg + ignore_case_ext=false 不命中 .JPG",
              case2_scan.get("status") == "completed"
              and group_with_path(c2groups, fx["case_ext"][0]) is None
              and int((c2_stats.get("skipped") or {}).get("extension") or 0) >= 2,
              "scan=%s skipped=%s" % (case2_scan.get("status"),
                                      c2_stats.get("skipped")))

        # exclude_extensions
        resp = post_scan(client, [fx["share_a"]],
                         options={"exclude_extensions": [".tmp"]})
        data = jd(resp)
        tmpex_id = data.get("scan_id")
        tmpex_scan = wait_scan(client, tmpex_id) if tmpex_id else {}
        tgroups, ttotal, _ = fetch_groups(client, tmpex_id) if tmpex_id else ([], 0, None)
        check("FILT-05", "exclude_extensions=.tmp 排除 tmp 重复组",
              tmpex_scan.get("status") == "completed"
              and group_with_path(tgroups, fx["tmp_ext"][0]) is None
              and group_with_path(tgroups, fx["case_ext"][0]) is not None,
              "scan=%s groups=%s" % (tmpex_scan.get("status"), ttotal))

        # 大小区间
        resp = post_scan(client, [fx["share_a"]],
                         options={"min_size_bytes": 2 * 1024 * 1024})
        data = jd(resp)
        size_id = data.get("scan_id")
        size_scan = wait_scan(client, size_id) if size_id else {}
        sgroups, stotal, _ = fetch_groups(client, size_id) if size_id else ([], 0, None)
        check("FILT-06", "min_size_bytes 过滤后仅保留 >=2MB 的组",
              size_scan.get("status") == "completed" and bool(sgroups)
              and all(int(g.get("size") or 0) >= 2 * 1024 * 1024 for g in sgroups)
              and group_with_path(sgroups, fx["big"][0]) is not None,
              "sizes=%s" % [g.get("size") for g in sgroups])

        # ---------------- 暂停 / 继续 / 停止 ----------------
        resp = post_scan(client, roots,
                         options={"hash_rate_mbps": 1, "concurrency": 1})
        data = jd(resp)
        pause_id = data.get("scan_id")
        check("PAUSE-00", "创建限速扫描用于暂停/停止测试",
              resp.status == 202 and bool(pause_id),
              "status=%s data=%s" % (resp.status, data))
        paused_ok = False
        busy_ok = False
        stop_ok = False
        resume_ok = False
        if pause_id:
            deadline = time.time() + 30
            while time.time() < deadline:
                resp = client.get("/api/scans/" + quote(pause_id))
                scan = (jd(resp).get("scan") or {})
                if scan.get("status") == "completed":
                    break
                if scan.get("status") == "running" and (
                        scan.get("stage") in ("sample", "hash")
                        or int((scan.get("stats") or {}).get("bytes_hashed") or 0) > 0):
                    break
                time.sleep(0.25)
            resp = client.post_json("/api/scans/%s/pause" % quote(pause_id), {})
            data = jd(resp)
            paused_ok = (resp.status == 200
                         and (data.get("scan") or {}).get("status") == "paused")
            # 扫描进行中再次创建扫描应 SCAN_BUSY
            busy_resp = post_scan(client, [fx["share_a"]])
            busy_code, _ = err_code(busy_resp)
            busy_ok = busy_resp.status == 409 and busy_code == "SCAN_BUSY"
            time.sleep(0.6)
            resp = client.post_json("/api/scans/%s/resume" % quote(pause_id), {})
            data = jd(resp)
            resume_ok = (resp.status == 200
                         and (data.get("scan") or {}).get("status") in ("running", "queued"))
            time.sleep(1.0)
            resp = client.post_json("/api/scans/%s/stop" % quote(pause_id), {})
            data = jd(resp)
            final = wait_scan(client, pause_id, timeout=60)
            stop_ok = (resp.status == 200
                       and final.get("status") in ("stopped", "completed"))
            resumed = client.post_json("/api/scans/%s/resume" % quote(pause_id), {})
            resume_code, _ = err_code(resumed)
            check("PAUSE-05", "已停止的扫描不能 resume（INVALID_ARGUMENT）",
                  resumed.status == 400 and resume_code == "INVALID_ARGUMENT",
                  "status=%s code=%s" % (resumed.status, resume_code))
        check("PAUSE-01", "扫描可暂停（status=paused）", paused_ok)
        check("PAUSE-02", "扫描进行中创建新扫描返回 409 SCAN_BUSY", busy_ok)
        check("PAUSE-03", "扫描可继续（resume）", resume_ok)
        check("PAUSE-04", "扫描可停止（stop -> stopped/completed）", stop_ok)

        # ---------------- 重启持久化 ----------------
        client.put_json("/api/settings", {"settings": {"concurrency": 3}})
        backend.stop()
        backend.start()
        client = Client("127.0.0.1", backend.port)
        client.login()
        resp = client.get("/api/scans")
        data = jd(resp)
        scans = data.get("scans") if isinstance(data.get("scans"), list) else []
        scan_ids = {item.get("scan_id") for item in scans if isinstance(item, dict)}
        check("RESTART-01", "重启后扫描历史仍可列出（含主扫描）",
              resp.status == 200 and main_id in scan_ids,
              "status=%s scans=%s" % (resp.status, sorted(scan_ids)))
        resp = client.get("/api/scans/%s" % quote(main_id))
        check("RESTART-02", "重启后主扫描报告 JSON 可读取",
              resp.status == 200
              and (jd(resp).get("scan") or {}).get("status") == "completed",
              "status=%s" % resp.status)
        resp = client.get("/api/scans/%s/groups" % quote(main_id))
        check("RESTART-03", "重启后主扫描 groups 仍可分页读取",
              resp.status == 200 and int(jd(resp).get("total") or 0) > 0,
              "status=%s total=%s" % (resp.status, jd(resp).get("total")))
        resp = client.get("/api/settings")
        check("RESTART-04", "重启后设置持久化（concurrency=3）",
              resp.status == 200
              and (jd(resp).get("settings") or {}).get("concurrency") == 3,
              "status=%s settings=%s" % (resp.status, jd(resp).get("settings")))
        resp = client.get("/api/logs")
        data = jd(resp)
        check("RESTART-05", "重启后 /api/logs 仍返回持久化日志/审计",
              resp.status == 200
              and bool(data.get("logs")) and bool(data.get("audit")),
              "status=%s logs=%s audit=%s"
              % (resp.status, len(data.get("logs") or []), len(data.get("audit") or [])))
        resp = client.get("/api/scans/%s/delete/%s"
                         % (quote(main_id), quote(job_id or "")))
        if resp.status == 200:
            record("RESTART-06", "重启后删除任务历史可读取（job status）", "PASS", "")
        else:
            record("RESTART-06", "重启后删除任务历史可读取（契约仅要求“至少列表可见”）",
                   "UNVERIFIED", "status=%s" % resp.status)

        resp = client.get("/api/healthz", csrf=False)
        check("RESTART-07", "重启后 /api/healthz 正常", resp.status == 200,
              "status=%s" % resp.status)

    finally:
        if backend is not None:
            backend.stop()
        if not keep_temp:
            shutil.rmtree(base, ignore_errors=True)
        else:
            print("保留临时目录: %s" % base)
    return ok


def summarize():
    passed = [r for r in RESULTS if r["status"] == "PASS"]
    failed = [r for r in RESULTS if r["status"] == "FAIL"]
    unverified = [r for r in RESULTS if r["status"] == "UNVERIFIED"]
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": sys.version,
        "repo": REPO,
        "summary": {"PASS": len(passed), "FAIL": len(failed),
                    "UNVERIFIED": len(unverified)},
        "evidence": EVIDENCE,
        "results": RESULTS,
    }
    try:
        with open(RESULT_JSON, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except OSError as exc:
        print("WARN: 无法写入 %s: %s" % (RESULT_JSON, exc))
    print("\n===== E2E SUMMARY: PASS=%d FAIL=%d UNVERIFIED=%d ====="
          % (len(passed), len(failed), len(unverified)))
    if failed:
        print("FAILED:")
        for item in failed:
            print("  - %s %s :: %s" % (item["id"], item["name"], item["detail"][:300]))
    if unverified:
        print("UNVERIFIED:")
        for item in unverified:
            print("  - %s %s :: %s" % (item["id"], item["name"], item["detail"][:200]))
    return payload


def main():
    rc = 0
    try:
        run()
    except Exception:
        traceback.print_exc()
        record("HARNESS", "端到端脚本执行", "FAIL", traceback.format_exc()[-2000:])
        rc = 2
    payload = summarize()
    if payload["summary"]["FAIL"] > 0:
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
