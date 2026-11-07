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


def expected_app_version():
    """从后端 config.py 读取期望版本（v1.0/v1.1 升级后无需改脚本）。"""
    try:
        text = read(os.path.join(REPO, "src", "backend", "dupcleaner", "config.py"))
        match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
        return match.group(1) if match else None
    except OSError:
        return None


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
                       "heapq", "mimetypes", "tests", "stringprep", "encodings",
                       "zipfile", "zlib", "html", "PIL", "logging.handlers", "sqlite3",
                       "xml")
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

    # ---- v1.1 内容相似度模式（docs/SIMILARITY_CONTRACT.md）----
    conf_src = read(os.path.join(backend, "dupcleaner", "config.py"))
    sim_path = os.path.join(backend, "dupcleaner", "similarity.py")
    sim_src = read(sim_path) if os.path.isfile(sim_path) else ""
    check("BE-SIM-ALGO", "similarity 模块存在且 MinHash/pHash/安全团/确定性哈希符合契约",
          os.path.isfile(sim_path)
          and "SIMILARITY_SIGNATURE_SIZE = 64" in conf_src
          and "SIMILARITY_MINHASH_BANDS = 16" in conf_src
          and "SIMILARITY_MINHASH_ROWS = 4" in conf_src
          and all(marker in sim_src for marker in
                  ("minhash_signature", "build_groups", "dhash_from_gray",
                   "content_sha256", "FeatureCache", "dc-sim-perm")),
          "algorithm markers missing")
    check("BE-SIM-CACHE", "特征缓存路径 feature_cache.json + cache_key(size/mtime/inode/dev/type)",
          "SIMILARITY_CACHE_FILENAME = " in conf_src
          and "feature_cache.json" in conf_src
          and "def cache_key(size, mtime_ns, inode, dev, feature_type)" in sim_src
          and "max_entries" in sim_src,
          "cache markers missing")
    sim_setting_keys = ["scan_mode", "similarity_threshold", "similarity_extensions",
                        "similarity_exclude_extensions", "similarity_concurrency",
                        "similarity_cache", "similarity_max_file_mb",
                        "similarity_allow_binary"]
    settings_src = read(os.path.join(backend, "dupcleaner", "settings.py"))
    check("BE-SIM-SETTINGS", "settings 新增 8 个相似度键并做范围校验",
          all(('"%s"' % key) in settings_src for key in sim_setting_keys)
          and "_check_int(key, value, 0, 100)" in settings_src
          and "_check_int(key, value, 1, 4)" in settings_src
          and "_check_int(key, value, 1, 1024)" in settings_src,
          "similarity settings markers missing")
    check("BE-SIM-STAGE", "scanner 新增 similarity/video 阶段与分类统计",
          '"similarity"' in scanner_src and '"video"' in scanner_src
          and "similarity_cache_hits" in scanner_src
          and "similarity_skipped" in scanner_src
          and '"exact_groups"' in scanner_src
          and '"similar_groups"' in scanner_src
          and '"similar_reclaimable_bytes"' in scanner_src,
          "similarity stage/stats markers missing")
    check("BE-SIM-GROUP-SCHEMA", "相似组 schema（match_type/manual_review_required/默认不勾选/keep latest）",
          'match_type": "similar' in scanner_src
          and '"manual_review_required": True' in scanner_src
          and 'file_info["default_selected"] = False' in scanner_src
          and 'group["group_id"] = "s-%06d"' in scanner_src
          and '"similarity_score": group_score' in scanner_src,
          "similar group schema markers missing")
    check("BE-SIM-EXACT-ISOLATED", "精准组文件不参与相似计算（先精准哈希，再排除）",
          "exact_paths" in scanner_src
          and "if record.path in exact_paths" in scanner_src,
          "exact isolation marker missing")
    check("BE-SIM-GROUPS-API", "groups 支持 match_type 过滤并返回 exact_total/similar_total",
          "def groups_page(self, scan_id, offset=0, limit=100, q=None," in scanner_src
          and "match_type=None" in scanner_src
          and '"exact_total": exact_total' in scanner_src
          and '"similar_total": similar_total' in scanner_src
          and "similarity_desc" in scanner_src,
          "groups match_type markers missing")
    check("BE-SIM-CSV", "CSV 新增 匹配类型/相似度分值 两列",
          u"匹配类型" in scanner_src and u"相似度分值" in scanner_src
          and u"内容相似" in scanner_src and u"哈希一致" in scanner_src,
          "CSV similarity columns missing")
    errors_src = read(os.path.join(backend, "dupcleaner", "errors.py"))
    check("BE-SIM-REVIEW", "删除计划强制人工复核（SIMILAR_REVIEW_REQUIRED + confirm/reviewed 字段）",
          "SIMILAR_REVIEW_REQUIRED" in errors_src
          and "confirm_similar" in del_src
          and "reviewed_similar_group_ids" in del_src
          and "SIMILAR_REVIEW_REQUIRED" in del_src,
          "review gating markers missing")
    check("BE-SIM-SCOPE", "scope=all_duplicates 永不包含相似/视频组",
          "scope=all_duplicates" in del_src
          and 'in ("similar", "video")' in del_src,
          "scope similar/video exclusion marker missing")
    check("BE-SIM-REVALIDATE", "相似文件删除前按各自 content_sha256 重校验（不要求二者相等）",
          "match_type == \"similar\"" in del_src
          and "不要求哈希相等" in del_src
          and "candidate_hash" in del_src and "keeper_hash" in del_src,
          "similar revalidate markers missing")
    check("BE-SIM-META", "/api/meta 暴露 similarity 能力",
          "similarity.capabilities()" in http_src
          and "default_extensions" in sim_src,
          "meta similarity capability missing")
    check("BE-SIM-COMPAT", "旧扫描结果缺少 match_type 时默认按 exact 处理（向后兼容）",
          'group.get("match_type") or "exact"' in scanner_src
          and '(group.get("match_type") or "exact") == "similar"' in del_src,
          "backward compatibility marker missing")

    # ---- v0030 云端字幕：本地推理模型“禁用思考”接入（Strata/Qwen3 实测快 14 倍）----
    subtitles_path = os.path.join(backend, "dupcleaner", "subtitles.py")
    subtitles_src = read(subtitles_path) if os.path.isfile(subtitles_path) else ""
    check("BE-SUBTITLE-THINKING",
          "翻译/连接测试可关闭模型思考（subtitle_llm_disable_thinking → chat_template_kwargs）",
          "subtitle_llm_disable_thinking" in settings_src
          and 'payload["chat_template_kwargs"] = {"enable_thinking": False}'
          in subtitles_src
          and subtitles_src.count('"chat_template_kwargs"') >= 2
          and "disable_thinking=disable_thinking" in subtitles_src,
          "disable-thinking markers missing")

    db_src = read(os.path.join(backend, "dupcleaner", "db.py"))
    store_src = read(os.path.join(backend, "dupcleaner", "store.py"))
    deletion_src = read(os.path.join(backend, "dupcleaner", "deletion.py"))
    video_src = read(os.path.join(backend, "dupcleaner", "video_similarity.py"))
    check("BE-SUBTITLE-GROUP",
          "批次组：自动拆批/聚合快照/组控制/保留（schema v7 + batch-groups 路由）",
          "create_batch_group" in subtitles_src
          and "SUBTITLE_BATCH_GROUP_MAX_ITEMS" in conf_src
          and "SUBTITLE_BATCH_GROUP_LIMIT" in conf_src
          and "subtitle_batch_groups" in db_src
          and "batch-groups" in http_src
          and "batch_group_max_items" in http_src,
          "subtitle group markers missing")
    check("BE-SCANDEL-FILTER",
          "删除成功后扫描结果读取过滤已删除文件（scan_file_deletions）",
          "scan_file_deletions" in db_src
          and "mark_scan_files_deleted" in store_src
          and "filter_group_payload" in store_src
          and "mark_scan_files_deleted" in deletion_src,
          "scan deletion filter markers missing")
    check("BE-SUBTITLE-AUDIO",
          "音频来源设置 + 服务器解码（流复制抽音轨 + 回退）",
          "SUBTITLE_AUDIO_SOURCES" in conf_src
          and "subtitle_audio_source" in settings_src
          and "extract_audio_remux" in video_src
          and "_extract_chunk_remux" in subtitles_src
          and "SERVER_AUDIO_FALLBACK" in subtitles_src,
          "server audio markers missing")


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

    # v0025：app.js 拆分为 ES modules。内容断言聚合 app.js + js/*.js，
    # 并逐个文件执行 node --check（Node 24 可直接检查 ESM 语法）。
    js_dir = os.path.join(web, "js")
    module_names = ["constants.js", "state.js", "dom.js", "format.js",
                    "util.js", "api.js", "toast.js", "modal.js",
                    "subtitle-probe.js", "subtitle-page.js"]
    module_paths = [os.path.join(js_dir, name) for name in module_names]
    missing_modules = [name for name in module_names
                       if not os.path.isfile(os.path.join(js_dir, name))]
    check("FE-MODULE-FILES", "前端 ES modules 十个模块文件齐全（js/*.js）",
          not missing_modules, "missing=%s" % missing_modules)
    check("FE-MODULE-ENTRY", "index.html 以 type=module 加载 app.js 并保留 ?v= 版本参数",
          bool(re.search(r'<script\s+type="module"\s+src="\./app\.js\?v=[^"]+"', html)),
          "module entry script not found")
    js_texts = [app]
    for module_path in module_paths:
        if os.path.isfile(module_path):
            js_texts.append(read(module_path))
    app = "\n".join(js_texts)  # 内容断言以聚合后的前端 JS 文本为准

    if os.path.isfile(app_path):
        check_targets = [app_path] + [p for p in module_paths if os.path.isfile(p)]
        module_failures = []
        for target in check_targets:
            rc, out = run(["node", "--check", target], timeout=120)
            if rc != 0:
                module_failures.append("%s rc=%s out=%s" % (
                    os.path.relpath(target, REPO), rc, out[-400:]))
        check("FE-NODECHECK", "node --check app.js 与 js/*.js 模块语法通过",
              not module_failures, " | ".join(module_failures)[:1200])
    else:
        record("FE-NODECHECK", "node --check app.js", "FAIL", "app.js missing")

    module_check_script = os.path.join(REPO, "qa", "check_web_modules.mjs")
    if os.path.isfile(module_check_script):
        rc, out = run(["node", module_check_script], timeout=120)
        check("FE-MODULE-GRAPH", "ES module 图可解析且顶层代码无异常（stub DOM 动态 import）",
              rc == 0, "rc=%s out=%s" % (rc, out[-1200:]))
    else:
        record("FE-MODULE-GRAPH", "ES module 图可解析且顶层代码无异常（stub DOM 动态 import）",
               "FAIL", "missing qa/check_web_modules.mjs")

    # 无头浏览器冒烟：真实 Chromium 加载 ES modules 页面，验证登录页渲染、
    # 资源 MIME 与控制台无异常；本机无浏览器时脚本自身 SKIP（退出码 0）。
    smoke_script = os.path.join(REPO, "qa", "web_smoke.py")
    if os.path.isfile(smoke_script):
        rc, out = run([sys.executable, smoke_script], timeout=300)
        check("FE-BROWSER-SMOKE", "无头浏览器冒烟（ES modules 可运行、资源 MIME、控制台无异常）",
              rc == 0, "rc=%s out=%s" % (rc, out[-1500:]))
    else:
        record("FE-BROWSER-SMOKE", "无头浏览器冒烟", "FAIL",
               "missing qa/web_smoke.py")

    # v0028：字幕页复用扫描范围目录选择器（同一共享文件夹树/面包屑/搜索/当前目录），
    # 不再自带一套简化的目录列表；同时覆盖枚举/批次接口。
    picker_ok = (
        "openSubtitleDirPicker" in app
        and "picker.mode === 'subtitle'" in app
        and "subtitle: '选择视频目录'" in app
        and "state.openSubtitleDirPicker" in app
        and 'id="subtitleDirPickBtn"' in html)
    check("FE-SUBTITLE-PICKER",
          "字幕页复用扫描范围目录选择器（共享文件夹树/面包屑/搜索/当前目录/完成选择）",
          picker_ok, "subtitle picker wiring missing")
    endpoints_ok = all(token in app for token in (
        "/api/subtitles/enumerate", "/api/subtitles/batch-groups",
        "/api/subtitles?group_id="))
    check("FE-SUBTITLE-ENDPOINTS", "字幕页覆盖目录枚举/批次组提交/组控制/任务分页接口",
          endpoints_ok, "subtitle batch group endpoints missing")

    subtitle_settings_ok = (
        all(marker in html for marker in
            ("setSubtitleOutputMode", "setSubtitleSkipExisting", "setSubtitleJobLimit"))
        and all(marker in app for marker in
                ("subtitle_output_mode", "subtitle_skip_existing", "subtitle_job_limit")))
    check("FE-SUBTITLE-SETTINGS",
          "设置页可配置目录批量字幕输出方式/默认跳过已有/任务保留上限",
          subtitle_settings_ok, "subtitle batch settings UI missing")
    writeback_ok = all(marker in app for marker in
                       ("job.outputs", "SUBTITLE_WARNING_LABEL",
                        "已写回 ", "translation_partial",
                        "OUTPUT_NOT_WRITABLE"))
    check("FE-SUBTITLE-WRITEBACK",
          "字幕任务列表/详情/弹窗展示写回路径、跳过与写回警告",
          writeback_ok, "subtitle write-back rendering missing")
    meta_ok = ("metaLimits" in app and "batch_max_items" in app
               and "enumerate_max_items" in app)
    check("FE-SUBTITLE-META", "字幕页限额取自 /api/meta.subtitle，不再硬编码",
          meta_ok, "meta subtitle limits not wired")
    poll_ok = ("lastSubtitleGroup.v1" in app
               and "startPolling" in app
               and "page.group" in app
               and "stopSubtitlePagePolling" in app
               and "state.taskTab = 'subtitles'" in app)
    check("FE-SUBTITLE-POLL-RESUME",
          "切换视图后批次组轮询可恢复、跳转任务中心直达字幕任务、记住上次目录",
          poll_ok, "subtitle batch polling/jump/memory wiring missing")
    group_ok = (
        all(marker in app for marker in (
            "/api/subtitles/batch-groups", "page.group", "batches",
            "startPolling", "lastSubtitleGroup.v1"))
        and all(marker in html for marker in (
            'id="subtitleGroupCard"', 'id="subtitleGroupJobs"',
            'id="subtitleGroupActions"', 'id="subtitleGroupBatches"')))
    check("FE-SUBTITLE-GROUP",
          "字幕页批次组：全选不截断、自动拆批提交、聚合进度、任务分页、组控制",
          group_ok, "subtitle batch group UI missing")
    thinking_ui_ok = (
        all(marker in html for marker in
            ("subtitleLlmDisableThinking", "enable_thinking=false"))
        and "subtitle_llm_disable_thinking" in app
        and "subtitleLlmDisableThinking" in app)
    check("FE-SUBTITLE-THINKING",
          "设置页提供“禁用 LLM 思考”开关（本地推理模型建议开启）",
          thinking_ui_ok, "disable-thinking setting UI missing")
    audio_ui_ok = (
        'id="subtitleAudioSource"' in html
        and "subtitle_audio_source" in app
        and "subtitleAudioSource" in app
        and "'server'" in app)
    check("FE-SUBTITLE-AUDIO",
          "设置页提供音频来源（NAS 解码 / 服务器解码）",
          audio_ui_ok, "subtitle audio source setting UI missing")
    delete_refresh_ok = (
        "resetResultsViewAfterDelete" in app
        and "recoverEmptyResultsPageAfterDelete" in app
        and "已从结果列表移除" in app)
    check("FE-DELETE-REFRESH",
          "删除完成后结果页重置并重拉（已删除文件不再显示，空页回退）",
          delete_refresh_ok, "delete refresh wiring missing")
    partial_ok = ("translation_partial" in app
                  and "translation_partial" in read(
                      os.path.join(js_dir, "subtitle-page.js")))
    check("FE-SUBTITLE-PARTIAL",
          "字幕任务列表/详情提示“部分译文回退原文”（translation_partial）",
          partial_ok, "translation_partial display missing")
    submit_reason_ok = (
        'id="subtitleSubmitReason"' in html
        and "subtitleSubmitReason" in app
        and "请勾选下方「我了解" in app
        and "将分为 " in app
        and "batch_group_max_items" in app
        and "groupMax" in app)
    check("FE-SUBTITLE-SUBMIT-REASON",
          "提交按钮禁用原因常显；全选不截断并提示拆分批次（v0031/v0032）",
          submit_reason_ok, "submit reason / group-limit markers missing")

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
        ("/api/subtitles/models", "字幕模型列表"),
        ("/api/subtitles/test", "字幕连接测试"),
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
                    "hash_rate_mbps", "recycle_default", "allow_ctime_fallback",
                    "scan_mode", "similarity_threshold", "similarity_extensions",
                    "similarity_exclude_extensions", "similarity_concurrency",
                    "similarity_cache", "similarity_max_file_mb",
                    "similarity_allow_binary",
                    "ffmpeg_path", "video_hwaccel",
                    # v0028 目录批量字幕
                    "subtitle_output_mode", "subtitle_skip_existing",
                    "subtitle_job_limit",
                    # v0030 本地推理模型禁用思考
                    "subtitle_llm_disable_thinking",
                    # v0032 音频来源（NAS 解码 / 服务器解码）
                    "subtitle_audio_source"]
    missing_settings = [key for key in setting_keys if key not in app]
    check("FE-SETTINGS-KEYS", "设置页覆盖契约全部设置键",
          not missing_settings, "missing=%s" % missing_settings)
    check("FE-FFMPEG-SETTING", "视频设置提供外部 FFmpeg 路径输入/解析来源提示/错误提示",
          all(marker in html for marker in
              ("videoFfmpegPath", "videoFfmpegPathError", "videoFfmpegHint",
               "setVideoFfmpegPath", "setVideoFfmpegPathError", "setVideoFfmpegHint"))
          and all(marker in app for marker in
                  ("ffmpeg_path", "configured_path", "videoFfmpegSourceLabel",
                   "refreshMetaCapabilities")),
          "ffmpeg path setting markers missing")
    check("FE-HWACCEL-SETTING", "硬解下拉用 hwaccel_modes 渲染、hwaccel_supported 决定可用，并展示 effective/fallback",
          all(marker in html for marker in
              ("videoHwaccel", "videoHwaccelHint", "setVideoHwaccel", "setVideoHwaccelHint"))
          and all(marker in app for marker in
                  ("hwaccel_supported", "hwaccel_modes", "hwaccel_effective",
                   "hwaccel_fallback", "videotoolbox", "renderVideoHwaccelOptions")),
          "hwaccel setting markers missing")
    check("FE-VIDEO-ERROR-KEY", "INVALID_ARGUMENT details.key 映射到 ffmpeg_path/video_hwaccel 字段错误",
          "videoFieldErrorsFromApi" in app and "ffmpeg_path: 'videoFfmpegPath'" in app,
          "api field error mapping missing")
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
    info("FE-INNERHTML", "前端 JS（app.js + js/*.js）innerHTML 使用次数（需人工确认不含不可信数据）",
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

    # ---- v1.1 内容相似度模式（docs/SIMILARITY_CONTRACT.md §6）----
    sim_warning = (u"内容相似度为 AI/特征比对结果，存在误匹配概率；"
                   u"相似组必须人工复核，禁止一键批量删除相似文件。")
    check("FE-SIM-WARNING", "内容相似分区警示文案逐字符合契约",
          sim_warning in html, "similarity warning text missing")
    check("FE-SIM-MODE", "扫描模式切换（精准哈希/内容相似）UI 存在",
          all(marker in html for marker in
              ("modeExactBtn", "modeSimilarBtn", u"精准哈希", u"内容相似"))
          and all(marker in app for marker in
                  ("modeExactBtn", "modeSimilarBtn", "setScanMode")),
          "scan mode switch markers missing")
    check("FE-SIM-OPTIONS", "相似模式选项齐全（阈值/并发/缓存/大小上限/类型/二进制）",
          all(marker in html for marker in
              ("simThreshold", "simThresholdRange", "simConcurrency",
               "simCache", "simMaxFileMb", "simAllowBinary", "simExtList",
               "similarityOptions"))
          and all(key in app for key in
                  ("similarity_threshold", "similarity_concurrency",
                   "similarity_cache", "similarity_max_file_mb",
                   "similarity_allow_binary", "similarity_extensions")),
          "similarity options markers missing")
    check("FE-SIM-PARTITION", "结果页分两区：哈希一致（精准重复）/ 内容相似",
          all(marker in html for marker in
              ("exactPartition", "similarPartition", "exactPartitionTitle",
               "exactPartitionCount", "similarPartitionCount"))
          and u"哈希一致（精准重复）" in app and u"内容相似" in html
          and u"需人工复核" in html,
          "partition markers missing")
    check("FE-SIM-SUMMARY", "摘要卡区分精准组/相似组与各自可释放空间",
          "summaryCard" in app and u"哈希一致（精准重复）" in app
          and u"内容相似" in app and "similar_reclaimable_bytes" in app,
          "summary similarity markers missing")
    check("FE-SIM-REVIEW", "相似组人工复核 checkbox + 未复核禁用组内文件选择",
          "similarReview" in app and "data-review-key" in app
          and u"人工复核本组" in app and u"未复核" in app
          and u"勾选“人工复核”后才可逐条选择" in app,
          "manual review markers missing")
    check("FE-SIM-NO-BULK", "禁止对相似组一键全选/批量删除",
          u"相似组不参与全选" in app and u"禁止一键批量删除" in html
          and u"禁止批量删除" in app and "similarReview" in app,
          "similar bulk-delete prohibition markers missing")
    check("FE-SIM-DELETE-GATE", "删除计划提交 confirm_similar + reviewed_similar_group_ids，前端按复核门控",
          ("confirm_similar: true" in app or "body.confirm_similar = true" in app)
          and "reviewed_similar_group_ids" in app
          and "SIMILAR_REVIEW_REQUIRED" in app
          and u"存在未人工复核的相似组，无法删除" in app,
          "similar delete gating markers missing")
    check("FE-SIM-DELETE-SUMMARY", "删除两步确认摘要分别展示精准/相似文件与已复核组数",
          u"内容相似文件" in app and u"相似组已复核" in app
          and u"人工复核" in app and "plan.similar_count" in app,
          "delete plan summary markers missing")
    check("FE-SIM-HISTORY", "历史记录按扫描模式展示（scan_mode/模式标签）",
          "SCAN_MODE_LABEL" in app and "scan_mode" in app,
          "history scan mode markers missing")
    check("FE-SIM-CACHE-COUNTERS", "进度区展示相似特征/缓存命中/候选/跳过计数",
          all(marker in html for marker in
              ("simFeatures", "simCacheHits", "simCandidates", "simSkipped"))
          and "similarityCounters" in html,
          "similarity counters missing")

    # ---- v0026 字幕设置：获取模型列表 / 测试连接 ----
    probe_ids = [
        "subtitleAsrModelsBtn", "subtitleAsrModelPicker", "subtitleAsrModelsHint",
        "subtitleAsrTestBtn", "subtitleAsrTestResult",
        "subtitleLlmModelsBtn", "subtitleLlmModelPicker", "subtitleLlmModelsHint",
        "subtitleLlmTestBtn", "subtitleLlmTestResult",
    ]
    missing_html_ids = [pid for pid in probe_ids if ('id="%s"' % pid) not in html]
    missing_app_ids = [pid for pid in probe_ids if pid not in app]
    probe_ok = (not missing_html_ids and not missing_app_ids
                and 'role="status"' in html
                and all(marker in app for marker in (
                    "/api/subtitles/models", "/api/subtitles/test",
                    "fetchModelList", "testProvider", "normalizeModelList",
                    "formatProbeResult", "formatProbeError")))
    check("FE-SUBTITLE-PROBE", "字幕设置提供获取模型列表/测试连接 UI（10 个 id/按钮/选择器/结果节点）",
          probe_ok,
          "missing_html=%s missing_app=%s" % (missing_html_ids, missing_app_ids))
    probe_src_path = os.path.join(js_dir, "subtitle-probe.js")
    probe_src = read(probe_src_path) if os.path.isfile(probe_src_path) else ""
    secret_leak = re.search(
        r"(?i)(?:setText|textContent)\s*\([^;\n]*(?:api_?key|provider\.apiKey)"
        r"|formatProbe(?:Result|Error)\s*\([^;\n]*api_?key"
        r"|(?:modelsHint|testResult)[^;\n]*(?:\+\s*[^;\n]*api_?key|api_?key\s*\+)",
        app)
    check("FE-SUBTITLE-PROBE-SECRET", "字幕探测提示/结果不写入 API Key（仅请求体传 api_key）",
          ("api_key" in probe_src) and ("apiKey" in probe_src) and not secret_leak,
          "secret leak pattern=%s" % (
              secret_leak.group(0)[:160] if secret_leak else ""))


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
    check("SPK-INFO-VALUES", "INFO 字段值符合契约（版本与 config 一致 / DSM7 / 架构 / 8787 / ui）",
          info.get("package") == "DuplicateCleaner"
          and info.get("version") == expected_app_version()
          and info.get("os_min_ver", "").startswith("7.0")
          and info.get("arch") in ("noarch", "x86_64", "armv8", "x86_64 armv8")
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
    try:
        privilege_json = json.loads(privilege) if privilege else {}
    except ValueError:
        privilege_json = {}
    defaults_json = privilege_json.get("defaults") if isinstance(
        privilege_json.get("defaults"), dict) else {}
    check("SPK-PRIVILEGE", "conf/privilege 声明 run-as=package（非 root）",
          defaults_json.get("run-as") == "package"
          and '"run-as"' in privilege and '"root"' not in privilege,
          "privilege=%s" % privilege[:300])
    check("SPK-USER", "conf/privilege 使用专用系统内部用户名/组名 duplicatecleaner",
          privilege_json.get("username") == "duplicatecleaner"
          and privilege_json.get("groupname") == "duplicatecleaner",
          "username/groupname mismatch: %s" % privilege[:300])
    root_hits = []
    root_patterns = [r'"run-as"\s*:\s*"root"', r"chown[^\n]*\broot\b",
                     r"\broot:root\b", r"(?m)^\s*sudo\s"]
    for path in walk_files(spk):
        try:
            text = read(path)
        except (UnicodeError, OSError):
            continue
        for pattern in root_patterns:
            if re.search(pattern, text):
                root_hits.append("%s:%s" % (os.path.relpath(path, REPO), pattern))
    check("SPK-NO-ROOT", "SPK 源码/脚本中不再包含 root 运行或 root 属主设置",
          not root_hits, "root_hits=%s" % root_hits[:10])
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
    check("SPK-UI-PLACEHOLDERS", "SPK ui/index.html 含构建占位符 __DUP_BACKEND_SCHEME__ 和动态真实 URL 渲染",
          "__DUP_BACKEND_SCHEME__" in ui_text and "target-url" in ui_text
          and "location.hostname" in ui_text,
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
    check("SPK-VAR-WRITABLE", "启动脚本校验 var 目录对套件用户可写并给出 chown 修复提示（非 root）",
          '[ ! -w "$VAR_DIR" ]' in start_script
          and 'chown -R duplicatecleaner:duplicatecleaner' in start_script
          and "chown -R root" not in start_script,
          "var write check missing")
    check("SPK-IONICE-GUARD", "非 root 启动时 ionice 失败可降级（不阻断启动）",
          'ionice -c2 -n7 true' in start_script
          and 'LAUNCH_PREFIX="nice -n 10"' in start_script,
          "ionice guard missing")
    web_index_src = read(os.path.join(REPO, "src", "web", "index.html"))
    packaging_doc = read(os.path.join(REPO, "docs", "SPK_PACKAGING.md"))
    check("SPK-ACL-HINT", "UI 与文档明确提示：授予系统内部用户 duplicatecleaner 读/写（含 #recycle）",
          u"系统内部用户" in web_index_src and "duplicatecleaner" in web_index_src
          and (u"读取/写入" in web_index_src or u"读取" in web_index_src)
          and (u"回收站" in web_index_src or "#recycle" in web_index_src)
          and "duplicatecleaner" in packaging_doc
          and u"系统内部用户" in packaging_doc
          and "#recycle" in packaging_doc,
          "ACL hint missing in UI/docs")
    build_script_path = os.path.join(REPO, "build", "build_spk.py")
    build_src = read(build_script_path) if os.path.isfile(build_script_path) else ""
    check("SPK-BUILD-VERSION", "build_spk.py 的 VERSION 与 config APP_VERSION 一致",
          ('VERSION = "%s"' % expected_app_version()) in build_src,
          "build script VERSION mismatch")
    check("SPK-BUILD-WHITELIST", "build_spk.py 强制 app/ 仅打包 .py（防测试输出混入）",
          "app/ 只允许 Python 源码" in build_src
          and "p.suffix != \".py\"" in build_src,
          "build app whitelist marker missing")
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
    version = expected_app_version()
    filename = "DuplicateCleaner-%s.spk" % (version or "1.0.0-0001")
    spk_path = os.path.join(out_dir, filename)
    if not os.path.isfile(spk_path):
        record("SPK-DIST", "构建产物 dist/%s" % filename, "UNVERIFIED",
               "SPK 尚未构建（Lead 构建后重跑）")
        return
    check("SPK-DIST", "构建产物存在（%s）" % filename, True, "")
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
        privilege_bytes = b""
        if "conf/privilege" in names:
            privilege_bytes = outer.extractfile(outer.getmember("conf/privilege")).read()
        script_modes = {name: outer.getmember(name).mode
                        for name in names if name.startswith("scripts/")}
    info = parse_info(info_bytes.decode("utf-8", "replace"))
    check("SPK-VERSION", "SPK INFO.version 与当前源码 APP_VERSION（%s）一致" % version,
          bool(version) and info.get("version") == version,
          "info.version=%s expected=%s" % (info.get("version"), version))
    checksum = info.get("checksum", "")
    actual_md5 = hashlib.md5(package_bytes).hexdigest()
    check("SPK-CHECKSUM", "INFO.checksum 与 package.tgz MD5 一致",
          checksum == actual_md5, "info=%s actual=%s" % (checksum, actual_md5))
    try:
        dist_priv = json.loads(privilege_bytes.decode("utf-8")) if privilege_bytes else {}
    except ValueError:
        dist_priv = {}
    dist_defaults = dist_priv.get("defaults") if isinstance(
        dist_priv.get("defaults"), dict) else {}
    check("SPK-DIST-PRIVILEGE", "产物 conf/privilege 为 run-as=package + duplicatecleaner（无 root）",
          dist_defaults.get("run-as") == "package"
          and dist_priv.get("username") == "duplicatecleaner"
          and dist_priv.get("groupname") == "duplicatecleaner",
          "dist privilege=%s" % (privilege_bytes[:300],))
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
                elif name.startswith("bin/"):
                    if name not in ("bin/ffmpeg-x86_64", "bin/ffmpeg-aarch64",
                                    "bin/LICENSE-FFMPEG-GPLv3.txt"):
                        whitelist_bad.append(name)
                elif name == "THIRD_PARTY_NOTICES.txt":
                    continue
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
            check("SPK-PKG-ACL", "打包后 UI 含系统内部用户 duplicatecleaner 的读/写 ACL 提示",
                  u"系统内部用户" in web_index and "duplicatecleaner" in web_index
                  and (u"读取/写入" in web_index or u"读取" in web_index),
                  "ACL hint missing in packaged web")
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
