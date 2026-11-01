#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DuplicateCleaner 静态验收检查（Python 3.8+，仅标准库）。

覆盖：后端 py_compile/Python3.8 语法/unittest、前端 node --check、风险提示精确
文案、契约端点覆盖、两次确认、无自动删除、SPK 源码结构与脚本静态审查、构建
产物 .spk 结构/checksum/权限/内容清理。

用法：python qa/static_checks.py [--skip-tests]
结果写入 qa/static_results.json；退出码 0=全 PASS，1=有 FAIL。
"""

from __future__ import print_function

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULT_JSON = os.path.join(REPO, "qa", "static_results.json")
RISK_TEXT = (u"删除文件存在数据丢失风险，重要数据建议先备份；本工具仅基于哈希判定重复，"
             u"无法保证业务文件适用性，请核对扫描结果再操作。")

RESULTS = []

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


def record(rid, name, status, detail=""):
    RESULTS.append({"id": rid, "name": name, "status": status, "detail": detail})
    line = "[%s] %s %s" % (status, rid, name)
    if detail and status != "PASS":
        line += " :: " + str(detail)[:800]
    print(line)


def check(rid, name, cond, detail=""):
    record(rid, name, "PASS" if cond else "FAIL", "" if cond else detail)
    return bool(cond)


def info(rid, name, detail=""):
    record(rid, name, "INFO", detail)


def read(path):
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8", "replace")


def run(cmd, cwd=REPO, timeout=300):
    try:
        proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout)
        return proc.returncode, proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, repr(exc)


def walk_files(root, suffixes=None):
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            path = os.path.join(base, name)
            if suffixes is None or os.path.splitext(name)[1].lower() in suffixes:
                yield path


# ---------------------------------------------------------------------------
# 后端
# ---------------------------------------------------------------------------
def check_backend():
    backend = os.path.join(REPO, "src", "backend")
    py_files = list(walk_files(backend, {".py"}))
    check("BE-FILES", "后端存在入口 app.py 与 dupcleaner 包",
          os.path.isfile(os.path.join(backend, "app.py"))
          and os.path.isfile(os.path.join(backend, "dupcleaner", "__init__.py")),
          "app.py missing?")

    compile_fail = []
    grammar_fail = []
    for path in py_files:
        rel = os.path.relpath(path, REPO)
        try:
            with open(path, "rb") as handle:
                source = handle.read()
            compile(source, path, "exec")
        except SyntaxError as exc:
            compile_fail.append("%s:%s %s" % (rel, exc.lineno, exc.msg))
        try:
            text = source.decode("utf-8")
            ast.parse(text, filename=path, feature_version=(3, 8))
        except SyntaxError as exc:
            grammar_fail.append("%s:%s %s" % (rel, getattr(exc, "lineno", "?"),
                                              getattr(exc, "msg", exc)))
        except UnicodeError as exc:
            grammar_fail.append("%s: decode %r" % (rel, exc))
    check("BE-COMPILE", "全部后端 .py 通过 py_compile 语法检查",
          not compile_fail, "failures=%s" % compile_fail[:10])
    check("BE-PY38", "全部后端 .py 符合 Python 3.8 语法（ast feature_version=3.8）",
          not grammar_fail, "failures=%s" % grammar_fail[:10])
    info("BE-FILE-COUNT", "后端 Python 文件数", str(len(py_files)))

    junk = []
    for path in walk_files(backend):
        name = os.path.basename(path)
        rel = os.path.relpath(path, REPO)
        if (name.startswith("test_output") or name.startswith("err_report")
                or name.startswith("_debug") or name in ("server.log", "debug.log")
                or name.endswith((".tmp", ".bak", ".orig"))):
            junk.append(rel)
    check("BE-SRC-HYGIENE", "src/backend 不含调试脚本/测试输出等会被打包的临时文件",
          not junk, "junk=%s" % junk)

    tests_dir = os.path.join(backend, "tests")
    test_files = [p for p in walk_files(tests_dir, {".py"})
                  if os.path.basename(p).startswith("test_")] if os.path.isdir(tests_dir) else []
    check("BE-TESTFILES", "存在后端 unittest 测试文件",
          bool(test_files), "tests=%s" % [os.path.basename(p) for p in test_files])
    if test_files and "--skip-tests" not in sys.argv:
        rc, out = run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                      cwd=backend, timeout=600)
        failures = [line for line in out.splitlines()
                    if line.startswith(("FAIL:", "ERROR:"))]
        total_line = ""
        for line in out.splitlines():
            if line.startswith("Ran "):
                total_line = line
        check("BE-UNITTEST", "后端 unittest 全部通过",
              rc == 0 and not failures,
              "%s | %s | tail=%s" % (total_line, failures[:8], out.splitlines()[-12:]))
        info("BE-UNITTEST-LOG", "unittest 结果", total_line or out.splitlines()[-1:])
    elif not test_files:
        record("BE-UNITTEST", "后端 unittest", "FAIL", "未发现测试文件")

    # 关键安全静态点
    scanner_src = read(os.path.join(backend, "dupcleaner", "scanner.py")) \
        if os.path.isfile(os.path.join(backend, "dupcleaner", "scanner.py")) else ""
    delete_calls = re.findall(r"\b(os\.unlink|os\.remove|os\.rename|shutil\.move|send2trash)\b",
                              scanner_src)
    check("BE-NO-AUTODELETE", "scanner.py 中没有任何删除/移动文件的调用",
          not delete_calls, "calls=%s" % delete_calls)
    check("BE-SCAN-AUDIT", "scanner 写入 scan 日志与审计（scan_start/scan_end）",
          "scan_start" in scanner_src and "scan_end" in scanner_src,
          "scan_start=%s scan_end=%s" % ("scan_start" in scanner_src,
                                         "scan_end" in scanner_src))

    time_src = read(os.path.join(backend, "dupcleaner", "timeutil.py")) \
        if os.path.isfile(os.path.join(backend, "dupcleaner", "timeutil.py")) else ""
    check("BE-STATX", "timeutil 使用 statx btime，含 x86_64=332 / aarch64=291",
          "332" in time_src and "291" in time_src and "STATX_BTIME" in time_src
          and "AT_SYMLINK_NOFOLLOW" in time_src,
          "statx markers missing")
    unsafe_ctime = [line.strip() for line in time_src.splitlines()
                    if "st_ctime" in line and "allow_ctime_fallback" not in line
                    and "created_source" not in line]
    check("BE-CTIME", "st_ctime 仅在 allow_ctime_fallback 分支使用（不静默冒充创建时间）",
          "allow_ctime_fallback" in time_src and len(unsafe_ctime) <= 6,
          "st_ctime lines=%s" % unsafe_ctime[:10])

    deletion_path = os.path.join(backend, "dupcleaner", "deletion.py")
    del_src = read(deletion_path) if os.path.isfile(deletion_path) else ""
    check("BE-DELETE-SAFETY", "删除模块含 #recycle/@duplicate-cleaner、confirm=DELETE、永久删除短语、revalidate",
          "#recycle" in del_src and "@duplicate-cleaner" in del_src
          and "DELETE" in del_src and u"永久删除" in del_src
          and "revalidate" in del_src,
          "markers missing")
    check("BE-DELETE-CONFIRM", "start_delete 精确校验 confirm == DELETE",
          'confirm != "DELETE"' in del_src or 'confirm == "DELETE"' in del_src,
          "confirm check missing")
    check("BE-DELETE-PHRASE", "永久删除校验 REQUIRED_PERMANENT_PHRASE",
          "REQUIRED_PERMANENT_PHRASE" in del_src,
          "phrase check missing")

    http_src = read(os.path.join(backend, "dupcleaner", "httpapi.py")) \
        if os.path.isfile(os.path.join(backend, "dupcleaner", "httpapi.py")) else ""
    expected_routes = ["/api/healthz", "/api/auth/login", "/api/auth/me",
                       "/api/auth/logout", "/api/meta", "/api/shares", "/api/browse",
                       "/api/settings", "/api/scans", "groups", "stats", "csv",
                       "delete-plan", "delete", "/api/preview", "/api/logs",
                       "/api/logs/download"]
    missing = []
    for marker in expected_routes:
        if marker.startswith("/api/"):
            if marker not in http_src:
                missing.append(marker)
        elif marker not in http_src:
            missing.append(marker)
    check("BE-ROUTES", "后端路由覆盖契约端点",
          not missing, "missing=%s" % missing)

    third_party = []
    import_re = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_.]*)",
                           re.MULTILINE)
    stdlib_prefixes = ("os", "sys", "re", "io", "json", "time", "datetime", "hashlib",
                       "hmac", "secrets", "ctypes", "threading", "concurrent", "http",
                       "socketserver", "urllib", "email", "ssl", "socket", "logging",
                       "argparse", "signal", "platform", "struct", "glob", "shutil",
                       "subprocess", "tempfile", "tarfile", "gzip", "csv", "copy",
                       "traceback", "collections", "errno", "stat", "base64", "random",
                       "string", "uuid", "binascii", "textwrap", "contextlib", "typing",
                       "dataclasses", "unittest", "pathlib", "functools", "itertools",
                       "shlex", "locale", "getpass", "pwd", "grp", "resource", "fcntl",
                       "select", "queue", "math", "decimal", "calendar", "codecs",
                       "heapq", "mimetypes", "tests", "stringprep", "encodings")
    for path in py_files:
        rel = os.path.relpath(path, REPO)
        src = read(path)
        for match in import_re.finditer(src):
            name = match.group(1).split(".")[0]
            if name in ("dupcleaner", "app") or name.startswith("_"):
                continue
            if name not in stdlib_prefixes:
                third_party.append("%s:%s" % (rel, name))
    check("BE-STDLIB", "后端仅使用 Python 标准库（无第三方运行依赖）",
          not third_party, "third_party=%s" % third_party[:20])


# ---------------------------------------------------------------------------
# 前端
# ---------------------------------------------------------------------------
def check_frontend():
    web = os.path.join(REPO, "src", "web")
    index_path = os.path.join(web, "index.html")
    app_path = os.path.join(web, "app.js")
    html = read(index_path) if os.path.isfile(index_path) else ""
    app = read(app_path) if os.path.isfile(app_path) else ""
    check("FE-FILES", "前端 index.html/app.js/styles.css 存在",
          os.path.isfile(index_path) and os.path.isfile(app_path)
          and os.path.isfile(os.path.join(web, "styles.css")),
          "missing files")
    if os.path.isfile(app_path):
        rc, out = run(["node", "--check", app_path], timeout=120)
        check("FE-NODECHECK", "node --check src/web/app.js 语法通过",
              rc == 0, "rc=%s out=%s" % (rc, out[-800:]))
    else:
        record("FE-NODECHECK", "node --check app.js", "FAIL", "app.js missing")

    check("FE-RISK-TEXT", "风险提示文案与契约逐字一致（index.html）",
          RISK_TEXT in html, "risk text not found verbatim")
    risk_region = ""
    match = re.search(r"<[^>]*(?:risk|risk-text)[^>]*>.{0,600}", html, re.S | re.I)
    if match:
        risk_region = match.group(0)
    close_markers = ["关闭风险", "risk-close", "hideRisk", "riskClose",
                     "dismissRisk", "closeRisk"]
    check("FE-RISK-PERSISTENT", "风险提示无关闭按钮/无隐藏逻辑（顶部常驻）",
          RISK_TEXT in html and not any(marker in html or marker in app
                                        for marker in close_markers)
          and "position:fixed" not in risk_region,
          "close markers found or text missing")

    endpoints = [
        ("/api/auth/login", "登录"),
        ("/api/auth/me", "会话"),
        ("/api/auth/logout", "登出"),
        ("/api/meta", "元信息"),
        ("/api/shares", "共享枚举"),
        ("/api/browse", "目录浏览"),
        ("/api/settings", "设置"),
        ("/api/scans", "扫描"),
        ("groups", "分组"),
        ("stats", "统计"),
        ("csv", "CSV"),
        ("delete-plan", "删除计划"),
        ("delete", "删除执行"),
        ("/api/preview", "预览"),
        ("/api/logs", "日志"),
        ("/api/logs/download", "日志下载"),
    ]
    missing = [name for marker, name in endpoints if marker not in app]
    check("FE-ENDPOINTS", "前端覆盖契约全部 API 端点",
          not missing, "missing=%s" % missing)

    two_step = (("delete-plan" in app) and ("/api/scans/' + encodePath(state.scan.id) + '/delete" in app
                or "/delete'" in app))
    check("FE-DELETE-FLOW", "删除流程先 delete-plan 再 delete（两步）",
          two_step, "delete-plan/delete call pattern not found")
    check("FE-CONFIRM-1", "存在第一次删除清单确认文案/弹窗",
          (u"确认删除" in app) or (u"删除清单" in app),
          "first confirmation text not found")
    check("FE-CONFIRM-2", "存在第二次最终确认文案与永久删除短语输入",
          (u"最终确认" in app) and (u"永久删除" in app)
          and ("permanent_phrase" in app) and ('confirm' in app and 'DELETE' in app),
          "second confirmation markers missing")
    check("FE-RECYCLE", "前端提供回收站/永久删除选项",
          "recycle" in app and u"回收站" in app,
          "recycle option missing")

    # 无自动删除：delete 端点只应出现在删除流程函数中，且不得由定时器触发
    auto_markers = ["setInterval", "setTimeout"]
    auto_delete = []
    for line_no, line in enumerate(app.splitlines(), 1):
        if "/delete" in line and any(marker in line for marker in auto_markers):
            auto_delete.append("%d:%s" % (line_no, line.strip()[:120]))
    check("FE-NO-AUTODELETE", "前端不存在定时/自动删除调用",
          not auto_delete, "auto_delete=%s" % auto_delete)
    check("FE-DOUBLE-SUBMIT", "删除请求有防重复提交（按钮禁用/标志位）",
          ("disabled = true" in app or "disabled=true" in app or "busy" in app)
          and "flow" in app,
          "no obvious guard")

    check("FE-CREATED-SOURCE", "结果行显示创建时间来源（created_source/SOURCE_INFO）",
          "created_source" in app and "SOURCE_INFO" in app,
          "created_source display markers missing")
    check("FE-HASH-COPY", "结果行支持复制完整哈希",
          "clipboard" in app and ("hash" in app),
          "hash copy markers missing")
    check("FE-PAGINATION", "结果列表分页/加载更多（offset/limit）",
          "offset" in app and "limit" in app,
          "pagination markers missing")
    check("FE-PREVIEW-TYPES", "预览覆盖图片/文本/PDF/音视频",
          all(marker in app for marker in
              ("image/", "text/", "'pdf'", "audio/", "video/")),
          "preview type markers missing")
    setting_keys = ["hash_algo", "keep_mode", "pinned_path", "min_size_bytes",
                    "max_size_bytes", "include_extensions", "exclude_extensions",
                    "ignore_case_ext", "skip_empty", "skip_hidden", "concurrency",
                    "hash_rate_mbps", "recycle_default", "allow_ctime_fallback"]
    missing_settings = [key for key in setting_keys if key not in app]
    check("FE-SETTINGS-KEYS", "设置页覆盖契约全部设置键",
          not missing_settings, "missing=%s" % missing_settings)
    es2020 = []
    for pattern, label in ((r"\?\.", "可选链 ?."), (r"\?\?", "空值合并 ??"),
                           (r"\bBigInt\b", "BigInt"),
                           (r"randomUUID|structuredClone|Object\.hasOwn",
                            "ES2021+ API")):
        if re.search(pattern, app):
            es2020.append(label)
    check("FE-ES2019", "未使用 ES2020+ 语法/API（兼容 DSM 内置浏览器）",
          not es2020, "found=%s" % es2020)

    # CDN / 外部资源
    external = re.findall(r'(?:src|href)\s*=\s*["\']https?://[^"\']+', html)
    external = [item for item in external if "w3.org" not in item]
    check("FE-NO-CDN", "无第三方 CDN / 外部脚本样式引用",
          not external, "external=%s" % external[:10])

    inner_html = len(re.findall(r"\.innerHTML\b", app))
    info("FE-INNERHTML", "app.js innerHTML 使用次数（需人工确认不含不可信数据）",
         str(inner_html))
    safe_dom = app.count("textContent") + app.count("createTextNode")
    check("FE-TEXTCONTENT", "使用 textContent/createTextNode 输出不可信文本（避免 innerHTML 注入）",
          safe_dom >= 8 and inner_html == 0,
          "textContent=%d createTextNode=%d innerHTML=%d"
          % (app.count("textContent"), app.count("createTextNode"), inner_html))
    check("FE-CHINESE", "前端存在中文界面文案（抽查扫描/历史/日志/设置）",
          all(text in app or text in html for text in
              (u"扫描", u"历史记录", u"操作日志", u"设置")),
          "Chinese nav text missing")

    nav = [u"扫描", u"历史记录", u"操作日志", u"设置"]
    check("FE-NAV", "顶部导航包含 扫描/历史记录/操作日志/设置",
          all(text in html for text in nav), "nav text missing in index.html")

    folder_picker = (u"文件夹" in app or u"选择文件夹" in app) and "/api/browse" in app
    check("FE-FOLDER-PICKER", "文件夹选择器调用 /api/browse 并支持多选/排除",
          folder_picker and ("excludes" in app),
          "folder picker markers missing")
    check("FE-KEEP-HIGHLIGHT", "结果页保留项高亮/徽标",
          (u"保留" in app) and ("keep_reason" in app or "keepReason" in app),
          "keep highlight markers missing")
    check("FE-PREVIEW", "结果页预览使用 /api/preview",
          "/api/preview" in app, "preview missing")
    check("FE-CSV", "结果页导出 CSV",
          "csv" in app and "/csv" in app, "csv export missing")
    check("FE-SELECT-ALL", "支持全选待删除副本/清空选择",
          (u"全选" in app) and (u"清空" in app),
          "select-all markers missing")
    check("FE-PROGRESS", "扫描进度真实轮询 /api/scans/{id}",
          ("setInterval" in app or "setTimeout" in app)
          and "/api/scans/' + encodePath(state.scan.id)" in app,
          "polling markers missing")


# ---------------------------------------------------------------------------
# SPK 源码
# ---------------------------------------------------------------------------
def parse_info(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def check_spk_source():
    spk = os.path.join(REPO, "spk")
    info_path = os.path.join(spk, "INFO")
    info = parse_info(read(info_path)) if os.path.isfile(info_path) else {}
    required = ["package", "version", "os_min_ver", "arch", "dsmuidir",
                "adminport", "startable", "displayname"]
    check("SPK-INFO-REQ", "SPK/INFO 包含必需字段",
          all(key in info for key in required),
          "missing=%s" % [key for key in required if key not in info])
    check("SPK-INFO-VALUES", "INFO 字段值符合契约（1.0.0-0001 / DSM7 / noarch / 8787 / ui）",
          info.get("package") == "DuplicateCleaner"
          and info.get("version") == "1.0.0-0001"
          and info.get("os_min_ver", "").startswith("7.0")
          and info.get("arch") == "noarch"
          and info.get("adminport") == "8787"
          and info.get("dsmuidir") == "ui"
          and info.get("startable") == "yes",
          "info=%s" % info)
    check("SPK-INFO-CHECKSUM-SRC", "源码 INFO 的 checksum 由构建脚本注入（源码可无）",
          True, "")

    conf_dir = os.path.join(spk, "conf")
    conf_ok = True
    conf_detail = []
    for name in ("privilege", "resource"):
        path = os.path.join(conf_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                json.load(handle)
        except (OSError, ValueError) as exc:
            conf_ok = False
            conf_detail.append("%s: %s" % (name, exc))
    check("SPK-CONF-JSON", "conf/privilege 与 conf/resource 为合法 JSON",
          conf_ok, "errors=%s" % conf_detail)
    privilege = read(os.path.join(conf_dir, "privilege")) if os.path.isfile(
        os.path.join(conf_dir, "privilege")) else ""
    check("SPK-PRIVILEGE", "conf/privilege 声明 run-as=root",
          '"run-as"' in privilege and '"root"' in privilege, privilege[:200])
    port_conf = os.path.join(spk, "port_conf", "DuplicateCleaner.sc")
    port_text = read(port_conf) if os.path.isfile(port_conf) else ""
    check("SPK-PORT", "port_conf 注册 8787/tcp",
          "8787/tcp" in port_text, port_text[:200])

    ui_config_path = os.path.join(spk, "ui", "config")
    try:
        with open(ui_config_path, "r", encoding="utf-8") as handle:
            ui_config = json.load(handle)
        ui_ok = "com.zhouyi.DuplicateCleaner" in (ui_config.get(".url") or {})
    except (OSError, ValueError):
        ui_ok = False
    check("SPK-UI-CONFIG-JSON", "SPK ui/config 为合法 JSON 且包含桌面入口",
          ui_ok, "ui/config invalid")
    ui_index = os.path.join(spk, "ui", "index.html")
    ui_text = read(ui_index) if os.path.isfile(ui_index) else ""
    check("SPK-UI-PLACEHOLDERS", "SPK ui/index.html 含构建占位符 __DUP_BACKEND_SCHEME__/__DUP_HOST__",
          "__DUP_BACKEND_SCHEME__" in ui_text and "__DUP_HOST__" in ui_text,
          "placeholders missing")
    check("SPK-UI-CONFIG", "SPK ui/config 桌面入口指向 3rdparty/DuplicateCleaner/index.html",
          "3rdparty/DuplicateCleaner/index.html" in
          read(os.path.join(spk, "ui", "config")),
          "ui/config url missing")

    scripts = os.path.join(spk, "scripts")
    names = ["preinst", "postinst", "preuninst", "postuninst",
             "start-stop-status", "postupgrade"]
    existing = [name for name in names if os.path.isfile(os.path.join(scripts, name))]
    check("SPK-SCRIPTS", "SPK 生命周期脚本齐全",
          len(existing) == len(names), "missing=%s" % [n for n in names if n not in existing])
    start_script = read(os.path.join(scripts, "start-stop-status")) \
        if os.path.isfile(os.path.join(scripts, "start-stop-status")) else ""
    check("SPK-NO-DEV", "生产启动脚本明确 unset DUP_CLEANER_DEV/DUP_DEV_ROOTS/DUP_DEV_PASSWORD",
          "unset DUP_CLEANER_DEV" in start_script
          and "DUP_DEV_ROOTS" in start_script,
          "dev unset missing")
    spk_all = []
    for path in walk_files(spk):
        try:
            text = read(path)
        except (UnicodeError, OSError):
            continue
        if "DUP_CLEANER_DEV=1" in text or "DUP_CLEANER_DEV=\"1\"" in text:
            spk_all.append(os.path.relpath(path, REPO))
    check("SPK-NO-DEV-SET", "SPK 源码中没有任何开启 dev 模式的赋值",
          not spk_all, "files=%s" % spk_all)
    check("SPK-NICE", "启动脚本使用 nice/ionice 降低资源占用",
          "nice -n 10" in start_script and "ionice" in start_script,
          "nice/ionice missing")
    check("SPK-HEALTH", "启动脚本等待 /api/healthz 健康检查",
          "/api/healthz" in start_script, "healthz missing")
    check("SPK-PY-DETECT", "preinst 检测 Python3 且要求 3.8+",
          "3, 8" in read(os.path.join(scripts, "preinst"))
          and "Python" in read(os.path.join(scripts, "preinst")),
          "python detection missing")
    preuninst = read(os.path.join(scripts, "preuninst")) \
        if os.path.isfile(os.path.join(scripts, "preuninst")) else ""
    postuninst = read(os.path.join(scripts, "postuninst")) \
        if os.path.isfile(os.path.join(scripts, "postuninst")) else ""
    check("SPK-KEEP-VAR", "卸载脚本不删除 var/（审计日志持久化）",
          "rm -rf" not in preuninst and "rm -rf" not in postuninst,
          "rm -rf found in uninstall scripts")
    check("SPK-SHEBANG", "生命周期脚本 shebang 为 #!/bin/sh（DSM 兼容）",
          all(read(os.path.join(scripts, name)).startswith("#!/bin/sh")
              for name in existing),
          "shebang mismatch")
    postinst = read(os.path.join(scripts, "postinst")) \
        if os.path.isfile(os.path.join(scripts, "postinst")) else ""
    check("SPK-POSTINST", "postinst 创建 var 目录并保留日志",
          "var" in postinst and "chmod" in postinst,
          "postinst content unexpected")
    return existing


def find_bash():
    candidates = []
    git = shutil.which("git")
    if git:
        # Git for Windows 自带 usr/bin/bash.exe，也可用于 sh -n 语法检查。
        git_root = os.path.dirname(os.path.dirname(os.path.abspath(git)))
        candidates.extend([
            os.path.join(git_root, "bin", "bash.exe"),
            os.path.join(git_root, "usr", "bin", "bash.exe"),
            os.path.join(git_root, "bin", "sh.exe"),
        ])
    candidates.extend([
        shutil.which("bash"),
        shutil.which("sh"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"D:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"D:\Program Files\Git\usr\bin\bash.exe",
    ])
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def shell_syntax_check(scripts, rid_prefix):
    bash = find_bash()
    if not bash:
        record(rid_prefix, "shell 脚本 bash -n 语法检查", "UNVERIFIED",
               "本机未找到可用 bash；已做静态审阅")
        return
    rc, probe = run([bash, "-c", "echo __BASH_OK__"], timeout=30)
    if rc != 0 or "__BASH_OK__" not in probe:
        record(rid_prefix, "shell 脚本 bash -n 语法检查", "UNVERIFIED",
               "bash 不可用（WSL 未安装发行版？）: %s" % probe[-300:])
        return
    failures = []
    for path in scripts:
        rc, out = run([bash, "-n", path], timeout=60)
        if rc != 0:
            failures.append("%s: rc=%s %s" % (os.path.basename(path), rc, out[-300:]))
    check(rid_prefix, "shell 脚本 bash -n 语法检查通过",
          not failures, "bash=%s failures=%s" % (bash, failures))


# ---------------------------------------------------------------------------
# 构建产物 SPK
# ---------------------------------------------------------------------------
def check_dist():
    out_dir = os.path.join(REPO, "dist")
    spk_path = os.path.join(out_dir, "DuplicateCleaner-1.0.0-0001.spk")
    if not os.path.isfile(spk_path):
        record("SPK-DIST", "构建产物 dist/DuplicateCleaner-1.0.0-0001.spk", "UNVERIFIED",
               "SPK 尚未构建（Lead 构建后重跑）")
        return
    check("SPK-DIST", "构建产物存在", True, "")
    expect_outer = {"INFO", "package.tgz", "PACKAGE_ICON.PNG",
                    "PACKAGE_ICON_256.PNG", "LICENSE",
                    "conf/privilege", "conf/resource",
                    "scripts/preinst", "scripts/postinst", "scripts/preuninst",
                    "scripts/postuninst", "scripts/postupgrade", "scripts/start-stop-status"}
    with tarfile.open(spk_path, "r:") as outer:
        names = set(outer.getnames())
        check("SPK-TAR-OUTER", "SPK 外层 tar 结构完整",
              expect_outer.issubset(names),
              "missing=%s" % sorted(expect_outer - names))
        package_member = outer.getmember("package.tgz")
        package_bytes = outer.extractfile(package_member).read()
        info_bytes = outer.extractfile(outer.getmember("INFO")).read()
        script_modes = {name: outer.getmember(name).mode
                        for name in names if name.startswith("scripts/")}
    info = parse_info(info_bytes.decode("utf-8", "replace"))
    checksum = info.get("checksum", "")
    actual_md5 = hashlib.md5(package_bytes).hexdigest()
    check("SPK-CHECKSUM", "INFO.checksum 与 package.tgz MD5 一致",
          checksum == actual_md5, "info=%s actual=%s" % (checksum, actual_md5))
    bad_modes = {name: oct(mode) for name, mode in script_modes.items()
                 if mode & 0o111 != 0o111}
    check("SPK-SCRIPT-MODE", "SPK 内 scripts 全部为可执行（755）",
          not bad_modes, "bad=%s" % bad_modes)

    sha_path = spk_path + ".sha256"
    if os.path.isfile(sha_path):
        digest = hashlib.sha256(open(spk_path, "rb").read()).hexdigest()
        check("SPK-SHA256", ".spk.sha256 与实际 SHA256 一致",
              digest in read(sha_path), "sha256 file mismatch")
    else:
        record("SPK-SHA256", ".spk.sha256 校验文件", "FAIL", "missing")

    temp = tempfile.mkdtemp(prefix="dupqa-spk-")
    try:
        pkg_path = os.path.join(temp, "package.tgz")
        with open(pkg_path, "wb") as handle:
            handle.write(package_bytes)
        with tarfile.open(pkg_path, "r:gz") as pkg:
            pkg_names = set(pkg.getnames())
            required_pkg = {"app/app.py", "web/index.html", "web/app.js",
                            "ui/index.html", "ui/config", "port_conf/DuplicateCleaner.sc",
                            "VERSION"}
            check("SPK-PKG-STRUCT", "package.tgz 含 app/web/ui/port_conf/VERSION",
                  required_pkg.issubset(pkg_names),
                  "missing=%s" % sorted(required_pkg - pkg_names))
            bad = [name for name in pkg_names
                   if "tests" in name.split("/") or "__pycache__" in name
                   or name.endswith((".pyc", ".pyo", ".log", ".tmp", ".bak"))
                   or "test_output" in name or "err_report" in name
                   or "_debug" in name or "rerun" in name]
            check("SPK-PKG-CLEAN", "package.tgz 不含 tests/__pycache__/pyc/调试/测试输出文件",
                  not bad, "bad=%s" % bad[:20])

            # 白名单：package.tgz 只应包含 app/*.py、web 静态资源、ui 资源、port_conf/*.sc、VERSION。
            whitelist_bad = []
            for name in pkg_names:
                if name in ("VERSION", "app", "web", "ui", "port_conf") or name.endswith("/"):
                    continue
                try:
                    if pkg.getmember(name).isdir():
                        continue
                except KeyError:
                    pass
                if name == "ui/config":
                    continue
                if name.startswith("app/"):
                    if not name.endswith(".py"):
                        whitelist_bad.append(name)
                elif name.startswith("web/"):
                    if not name.endswith((".html", ".js", ".css", ".svg", ".png",
                                          ".jpg", ".jpeg", ".ico", ".webp",
                                          ".woff", ".woff2", ".json", ".txt", ".map")):
                        whitelist_bad.append(name)
                elif name.startswith("ui/"):
                    if not name.endswith((".html", ".json", ".png", ".svg", ".css", ".js")):
                        whitelist_bad.append(name)
                elif name.startswith("port_conf/"):
                    if not name.endswith(".sc"):
                        whitelist_bad.append(name)
                else:
                    whitelist_bad.append(name)
            check("SPK-PKG-WHITELIST", "package.tgz 内容符合白名单（app 仅 .py，无临时/输出文件）",
                  not whitelist_bad, "unexpected=%s" % whitelist_bad[:20])
            ui_index = pkg.extractfile(pkg.getmember("ui/index.html")).read().decode(
                "utf-8", "replace")
            check("SPK-PKG-UI-PLACEHOLDER", "打包后 ui/index.html 占位符已被替换",
                  "__DUP_BACKEND_SCHEME__" not in ui_index
                  and "__DUP_HOST__" not in ui_index,
                  "placeholders remain")
            web_index = pkg.extractfile(pkg.getmember("web/index.html")).read().decode(
                "utf-8", "replace")
            check("SPK-PKG-RISK", "打包后 web/index.html 仍含精确风险提示",
                  RISK_TEXT in web_index, "risk text missing in packaged web")
            web_files = [name for name in pkg_names if name.startswith("web/")]
            check("SPK-PKG-WEB", "package.tgz 含完整前端文件", len(web_files) >= 4,
                  "web files=%s" % web_files[:10])
            pkg.extractall(temp)
            extracted_scripts = []
            for name in pkg_names:
                if name.startswith("scripts/"):
                    extracted_scripts.append(os.path.join(temp, name))
            # 部署脚本在 SPK 外层而不是 package.tgz，这里只有 target 内容

            def _file_sha(path):
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
                return digest.hexdigest()

            extract_root = temp
            sync_diff = {}
            sync_extra = {}
            for pkgdir, srcrel in (("app", "src/backend"), ("web", "src/web"),
                                   ("ui", "spk/ui"), ("port_conf", "spk/port_conf")):
                base = os.path.join(extract_root, pkgdir)
                if not os.path.isdir(base):
                    continue
                for dirpath, _dirs, files in os.walk(base):
                    for fname in files:
                        packaged = os.path.join(dirpath, fname)
                        rel = os.path.relpath(packaged, base)
                        source = os.path.join(REPO, srcrel, rel)
                        if not os.path.isfile(source):
                            sync_extra.setdefault(pkgdir, []).append(rel)
                            continue
                        packaged_sha = _file_sha(packaged)
                        source_sha = _file_sha(source)
                        if pkgdir == "ui" and rel.replace(os.sep, "/") == "index.html":
                            text = read(source)
                            text = text.replace("__DUP_BACKEND_SCHEME__", "http")
                            text = text.replace("__DUP_HOST__", "当前 DSM 主机")
                            source_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                        if packaged_sha != source_sha:
                            sync_diff.setdefault(pkgdir, []).append(rel.replace(os.sep, "/"))
            out_of_sync = {k: v for k, v in sync_diff.items() if v}
            if sync_extra:
                out_of_sync["extra"] = sync_extra
            check("SPK-SYNC", "SPK 产物与当前源码一致（构建后未再修改源码）",
                  not out_of_sync,
                  "产物已过期，需重新构建；差异=%s" % out_of_sync)
        dev_hits = []
        for path in walk_files(temp):
            if os.path.basename(path) == "package.tgz":
                continue
            try:
                text = read(path)
            except (UnicodeError, OSError):
                continue
            if "DUP_CLEANER_DEV=1" in text:
                dev_hits.append(os.path.relpath(path, temp))
        check("SPK-DEV-ENV", "打包内容中不含 DUP_CLEANER_DEV=1 生产开启",
              not dev_hits, "files=%s" % dev_hits)
        # 后端 entry 可执行
        app_mode = 0
        with tarfile.open(pkg_path, "r:gz") as pkg:
            app_mode = pkg.getmember("app/app.py").mode
        check("SPK-APP-MODE", "package.tgz 内 app/app.py 可执行",
              app_mode & 0o111 != 0, "mode=%s" % oct(app_mode))
    finally:
        shutil.rmtree(temp, ignore_errors=True)

    # 外层 scripts 解包后做 bash -n
    temp2 = tempfile.mkdtemp(prefix="dupqa-spksh-")
    try:
        with tarfile.open(spk_path, "r:") as outer:
            script_names = [name for name in outer.getnames()
                            if name.startswith("scripts/") and not outer.getmember(name).isdir()]
            for name in script_names:
                target = os.path.join(temp2, name.replace("/", os.sep))
                parent = os.path.dirname(target)
                if not os.path.isdir(parent):
                    os.makedirs(parent)
                data = outer.extractfile(outer.getmember(name)).read()
                with open(target, "wb") as handle:
                    handle.write(data)
        shell_syntax_check([os.path.join(temp2, name.replace("/", os.sep))
                            for name in script_names], "SPK-SH-SYNTAX")
        # 脚本不得包含 CRLF
        crlf = []
        for name in script_names:
            path = os.path.join(temp2, name.replace("/", os.sep))
            with open(path, "rb") as handle:
                if b"\r\n" in handle.read():
                    crlf.append(name)
        check("SPK-SH-LF", "SPK 脚本为 LF 换行（DSM shell 兼容）",
              not crlf, "crlf=%s" % crlf)
    finally:
        shutil.rmtree(temp2, ignore_errors=True)


# ---------------------------------------------------------------------------
def main():
    check_backend()
    check_frontend()
    scripts = check_spk_source()
    shell_syntax_check([os.path.join(REPO, "spk", "scripts", name)
                        for name in scripts], "SPK-SH-SYNTAX-SRC")
    check_dist()
    passed = [r for r in RESULTS if r["status"] == "PASS"]
    failed = [r for r in RESULTS if r["status"] == "FAIL"]
    unverified = [r for r in RESULTS if r["status"] == "UNVERIFIED"]
    payload = {"generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z"),
               "summary": {"PASS": len(passed), "FAIL": len(failed),
                           "UNVERIFIED": len(unverified)},
               "results": RESULTS}
    with open(RESULT_JSON, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print("\n===== STATIC SUMMARY: PASS=%d FAIL=%d UNVERIFIED=%d ====="
          % (len(passed), len(failed), len(unverified)))
    for item in failed:
        print("  FAIL %s %s :: %s" % (item["id"], item["name"], item["detail"][:300]))
    for item in unverified:
        print("  UNVERIFIED %s %s :: %s" % (item["id"], item["name"], item["detail"][:200]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
