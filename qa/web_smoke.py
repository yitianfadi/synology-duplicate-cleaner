#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DuplicateCleaner 前端浏览器冒烟检查（Python 3.8+，仅标准库）。

用途：ES modules 拆分后，静态语法检查无法发现“模块 import/export 不匹配、
运行时顶层异常、静态资源 404”等问题。本脚本：

1. 以 --dev 模式临时启动后端（随机端口、临时数据目录），只服务 src/web；
2. 递归解析 index.html -> app.js -> js/*.js 的 import 图，逐个 HTTP GET 校验
   200 且 Content-Type 为 JavaScript；
3. 用本机 headless Chromium 打开页面，dump DOM 并收集控制台错误，断言登录页
   已渲染、启动脚本无 Uncaught / module 加载失败。

用法：
    python qa/web_smoke.py            # 找不到浏览器时 SKIP（退出码 0）
    python qa/web_smoke.py --require  # 找不到浏览器视为 FAIL
退出码：0=通过/跳过；1=失败。
"""

from __future__ import print_function

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "src", "backend", "app.py")
WEB_DIR = os.path.join(REPO, "src", "web")
RISK_TEXT = (u"删除文件存在数据丢失风险，重要数据建议先备份；本工具仅基于哈希判定重复，"
             u"无法保证业务文件适用性，请核对扫描结果再操作。")

RESULTS = []


def record(name, status, detail=""):
    RESULTS.append({"name": name, "status": status, "detail": detail})
    line = "[%s] %s" % (status, name)
    if detail and status != "PASS":
        line += " :: " + str(detail)[:500]
    print(line)


def check(name, cond, detail=""):
    record(name, "PASS" if cond else "FAIL", "" if cond else detail)
    return bool(cond)


def find_chromium():
    candidates = []
    env = os.environ.get("DUP_CHROMIUM")
    if env:
        candidates.append(env)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        root = os.path.join(local, "ms-playwright")
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                if name.startswith("chromium-"):
                    for exe in ("chrome.exe", os.path.join("chrome-win64", "chrome.exe")):
                        candidates.append(os.path.join(root, name, exe))
    home = os.path.expanduser("~")
    cache = os.path.join(home, ".cache", "ms-playwright")
    if os.path.isdir(cache):
        for name in sorted(os.listdir(cache)):
            if name.startswith("chromium-"):
                candidates.append(os.path.join(cache, name, "chrome-linux", "chrome"))
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def wait_healthz(base_url, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url + "api/healthz", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def http_get(base_url, rel_path):
    url = base_url + rel_path.lstrip("/")
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read()
            return resp.status, resp.headers.get("Content-Type") or "", body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type") or "", b""
    except Exception as exc:
        return 0, "", repr(exc).encode("utf-8")


IMPORT_RE = re.compile(
    r"""(?:from|import)\s*\(?\s*['"]([^'"]+)['"]""", re.MULTILINE)


def module_graph():
    """从 index.html 的 module 入口递归收集相对模块路径。"""
    html_path = os.path.join(WEB_DIR, "index.html")
    with open(html_path, "r", encoding="utf-8") as handle:
        html = handle.read()
    entries = re.findall(
        r'<script[^>]+type=["\']module["\'][^>]+src=["\']([^"\']+)["\']', html)
    if not entries:
        entries = re.findall(r'<script[^>]+src=["\']([^"\']+app\.js[^"\']*)["\']', html)
    seen = []
    queue = [path.split("?")[0].lstrip("./") if path.startswith("./")
             else path.split("?")[0] for path in entries]
    while queue:
        rel = queue.pop(0)
        if rel in seen or not rel.endswith(".js"):
            continue
        seen.append(rel)
        path = os.path.join(WEB_DIR, rel.replace("/", os.sep))
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        for spec in IMPORT_RE.findall(text):
            spec = spec.split("?")[0]
            if not spec.startswith("."):
                continue
            base = os.path.dirname(rel).replace(os.sep, "/")
            joined = os.path.normpath(os.path.join(base, spec)).replace(os.sep, "/")
            if joined not in seen:
                queue.append(joined)
    return seen


def start_backend(workdir):
    data_dir = os.path.join(workdir, "data")
    share_dir = os.path.join(workdir, "share")
    os.makedirs(share_dir, exist_ok=True)
    env = dict(os.environ)
    env["DUP_DEV_ROOTS"] = share_dir
    env["DUP_CLEANER_DEV"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, APP, "--host", "127.0.0.1", "--port", "0",
         "--data-dir", data_dir, "--web-dir", WEB_DIR, "--dev",
         "--log-level", "INFO"],
        cwd=os.path.dirname(APP), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    url = None
    deadline = time.time() + 30.0
    pending = b""
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        pending += line
        match = re.search(rb"(https?://127\.0\.0\.1:\d+/)", line)
        if match:
            url = match.group(1).decode("utf-8")
            break
    if url is None or not wait_healthz(url):
        tail = pending.decode("utf-8", "replace")[-800:]
        proc.kill()
        return None, None, tail
    return proc, url, ""


def run_browser(chrome, base_url, workdir):
    profile = os.path.join(workdir, "chrome-profile")
    args = [
        chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--no-first-run", "--no-default-browser-check",
        "--disable-dev-shm-usage", "--user-data-dir=" + profile,
        "--virtual-time-budget=8000", "--enable-logging=stderr",
        "--log-level=0", "--dump-dom", base_url,
    ]
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=120)
    return (proc.stdout or b"").decode("utf-8", "replace"), \
        (proc.stderr or b"").decode("utf-8", "replace")


ERROR_PATTERNS = (
    "Uncaught",
    "SyntaxError",
    "Failed to load module script",
    "does not provide an export named",
    "Cannot find module",
    "Requested module",
    "net::ERR_",
    "404 (Not Found)",
)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    require = "--require" in argv
    chrome = find_chromium()
    if chrome is None:
        if require:
            record("WEB-SMOKE-BROWSER", "FAIL", "未找到 Chromium（可用 DUP_CHROMIUM 指定）")
            return 1
        record("WEB-SMOKE-BROWSER", "SKIP", "未找到 Chromium，跳过浏览器冒烟")
        return 0
    record("WEB-SMOKE-BROWSER", "PASS", chrome)

    graph = module_graph()
    check("WEB-SMOKE-GRAPH", len(graph) >= 2,
          "模块图只找到 %s 个文件（预期 app.js + js/*.js）" % len(graph))
    print("     模块图：%s" % ", ".join(graph))

    workdir = tempfile.mkdtemp(prefix="dupqa-web-")
    proc = None
    try:
        proc, base_url, tail = start_backend(workdir)
        if proc is None:
            record("WEB-SMOKE-SERVER", "FAIL", "后端未能启动：%s" % tail)
            return 1
        record("WEB-SMOKE-SERVER", "PASS", base_url)

        bad_mime = []
        missing = []
        for rel in graph:
            status, content_type, body = http_get(base_url, rel)
            if status != 200 or not body:
                missing.append("%s(status=%s)" % (rel, status))
                continue
            if "javascript" not in content_type.lower():
                bad_mime.append("%s(%s)" % (rel, content_type))
        check("WEB-SMOKE-ASSETS", not missing, "缺失/空响应：%s" % missing)
        check("WEB-SMOKE-MIME", not bad_mime,
              "模块 Content-Type 非 JavaScript（DSM 浏览器会拒绝执行）：%s" % bad_mime)

        dom, log = run_browser(chrome, base_url, workdir)
        check("WEB-SMOKE-DOM", "loginForm" in dom and u"登录" in dom,
              "登录页未渲染，DOM 长度=%s" % len(dom))
        check("WEB-SMOKE-RISK", RISK_TEXT in dom, "风险提示文案未出现在渲染后 DOM")
        errors = [pattern for pattern in ERROR_PATTERNS if pattern in log]
        check("WEB-SMOKE-CONSOLE", not errors,
              "控制台出现错误标记 %s；日志尾部：%s" % (errors, log[-1200:]))
        if errors:
            print("     ---- browser log ----")
            print(log[-4000:])
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
        shutil.rmtree(workdir, ignore_errors=True)

    failed = [item for item in RESULTS if item["status"] == "FAIL"]
    passed = [item for item in RESULTS if item["status"] == "PASS"]
    skipped = [item for item in RESULTS if item["status"] == "SKIP"]
    print("\n===== WEB SMOKE: PASS=%d FAIL=%d SKIP=%d =====" %
          (len(passed), len(failed), len(skipped)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
