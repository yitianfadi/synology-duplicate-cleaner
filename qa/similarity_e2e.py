#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DuplicateCleaner v1.1 内容相似度模式端到端验收（Python 3.8+，仅标准库）。

真实 dev HTTP 后端 + 临时共享根；覆盖 docs/SIMILARITY_CONTRACT.md 的核心验收项：
文本相似/低于阈值、精准组不重复进入相似组、二进制/压缩包/媒体/超限跳过、
特征缓存命中、相似阶段停止、组 schema、groups match_type 过滤、CSV 新列、
相似删除人工复核门控、keeper 保护、混合精确+相似删除计划。

用法：python qa/similarity_e2e.py [--keep]
退出码：0=全 PASS；1=有 FAIL；2=无法启动/脚本异常。
结果写入 qa/similarity_results.json。
"""

from __future__ import print_function

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import e2e_test as base  # 复用 v1 验收脚本中的 dev 后端/HTTP 客户端/临时样本工具

RESULT_JSON = os.path.join(HERE, "similarity_results.json")
RESULTS = []
EVIDENCE = {}


def record(rid, name, status, detail=""):
    RESULTS.append({"id": rid, "name": name, "status": status, "detail": detail})
    line = "[%s] %s %s" % (status, rid, name)
    if detail and status != "PASS":
        line += " :: " + str(detail)[:700]
    print(line)


def check(rid, name, cond, detail=""):
    record(rid, name, "PASS" if cond else "FAIL", "" if cond else detail)
    return bool(cond)


def info(rid, name, detail=""):
    record(rid, name, "INFO", detail)


def evidence(key, value):
    EVIDENCE[key] = value


def expected_app_version():
    try:
        with open(os.path.join(REPO, "src", "backend", "dupcleaner", "config.py"),
                  "r", encoding="utf-8") as handle:
            match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', handle.read())
        return match.group(1) if match else None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 文本样本（确定性，避免随机性导致阈值抖动）
# ---------------------------------------------------------------------------
def make_text(seed, lines=420, width=96):
    chunks = []
    for index in range(lines):
        digest = hashlib.sha256(("%s|%d" % (seed, index)).encode()).hexdigest()
        body = (digest * 8)[:max(8, width - 8)]
        chunks.append("%06d %s" % (index, body))
    return ("\n".join(chunks) + "\n").encode("utf-8")


def make_variant(data, mod_every=40, seed="variant"):
    lines = data.decode("utf-8").splitlines()
    out = []
    for index, line in enumerate(lines):
        if index % mod_every == 0:
            digest = hashlib.sha256(("%s|%d" % (seed, index)).encode()).hexdigest()
            out.append("%06d %s" % (index, (digest * 8)[:max(8, len(line) - 8)]))
        else:
            out.append(line)
    return ("\n".join(out) + "\n").encode("utf-8")


def build_fixtures(base_dir):
    main = os.path.join(base_dir, "qa-sim-share")
    slow = os.path.join(base_dir, "qa-sim-slow")
    os.makedirs(main)
    os.makedirs(slow)
    fx = {"main": main, "slow": slow}

    # --- 相似对 1（高相似、非字节一致）---
    hi1 = make_text("hi1", lines=420, width=96)
    a = os.path.join(main, "text", "alpha.txt")
    b = os.path.join(main, "text", "beta.txt")
    a_ns = base.create_ordered(a, hi1)
    fx["alpha"], fx["beta"] = a, b
    base.create_ordered(b, make_variant(hi1, mod_every=40, seed="hi1v"), a_ns)

    # --- 相似对 2 ---
    hi2 = make_text("hi2", lines=420, width=96)
    g = os.path.join(main, "text", "gamma.txt")
    d = os.path.join(main, "text", "delta.txt")
    g_ns = base.create_ordered(g, hi2)
    fx["gamma"], fx["delta"] = g, d
    base.create_ordered(d, make_variant(hi2, mod_every=40, seed="hi2v"), g_ns)

    # --- 相似对 3：用于“扫描后修改 -> FILE_CHANGED”---
    hi3 = make_text("hi3", lines=420, width=96)
    t = os.path.join(main, "text", "theta.txt")
    i = os.path.join(main, "text", "iota.txt")
    t_ns = base.create_ordered(t, hi3)
    fx["theta"], fx["iota"] = t, i
    base.create_ordered(i, make_variant(hi3, mod_every=40, seed="hi3v"), t_ns)

    # --- 低于阈值：内容完全无关 ---
    low1 = os.path.join(main, "text", "one.txt")
    low2 = os.path.join(main, "text", "two.txt")
    l1 = base.create_ordered(low1, make_text("low-a", lines=420, width=96))
    fx["one"], fx["two"] = low1, low2
    base.create_ordered(low2, make_text("low-b", lines=420, width=96), l1)

    # --- 精准组（不应重复出现在相似组）---
    exact = os.path.join(main, "exact", "dup1.txt")
    exact2 = os.path.join(main, "exact", "dup2.txt")
    payload = b"EXACT-DUPLICATE-CONTENT\n" + b"x" * 4096
    e_ns = base.create_ordered(exact, payload)
    fx["dup1"], fx["dup2"] = exact, exact2
    base.create_ordered(exact2, payload, e_ns)

    # --- 压缩包精准组（哈希命中；相似计算跳过 binary）---
    za = os.path.join(main, "binary", "exact_a.zip")
    zb = os.path.join(main, "binary", "exact_b.zip")
    zpayload = b"PK\x03\x04" + base.blob("zip-exact", 8192)
    z_ns = base.create_ordered(za, zpayload)
    fx["zip_exact"] = [za, zb]
    base.create_ordered(zb, zpayload, z_ns)

    # --- 压缩包“相似但不相同”（不得进入相似组）---
    za2 = os.path.join(main, "binary", "sim_a.zip")
    zb2 = os.path.join(main, "binary", "sim_b.zip")
    z2 = b"PK\x03\x04" + base.blob("zip-sim", 16384)
    z2_ns = base.create_ordered(za2, z2)
    fx["zip_sim"] = [za2, zb2]
    base.create_ordered(zb2, z2[:8192] + base.blob("zip-sim2", 8192), z2_ns)

    # --- 媒体（相似计算跳过）---
    m1 = os.path.join(main, "binary", "m1.mp4")
    m2 = os.path.join(main, "binary", "m2.mp4")
    m = b"\x00\x00\x00\x18ftypmp42" + base.blob("mp4", 20000)
    m_ns = base.create_ordered(m1, m)
    fx["media"] = [m1, m2]
    base.create_ordered(m2, m[:12000] + base.blob("mp4b", 8000), m_ns)

    # --- 超过 similarity_max_file_mb 的相似文本（应跳过 size）---
    big = make_text("big", lines=9000, width=160)
    big1 = os.path.join(main, "big", "big1.md")
    big2 = os.path.join(main, "big", "big2.md")
    b_ns = base.create_ordered(big1, big)
    fx["big"] = [big1, big2]
    base.create_ordered(big2, make_variant(big, mod_every=200, seed="bigv"), b_ns)

    # --- 相似阶段停止测试专用（较大文本，后续扫描使用低限速）---
    slow_base = make_text("slow", lines=5200, width=200)
    slow_paths = []
    prev = None
    for index in range(4):
        path = os.path.join(slow, "s%d.txt" % (index + 1))
        prev = base.create_ordered(
            path, make_variant(slow_base, mod_every=300, seed="slow-%d" % index), prev)
        slow_paths.append(path)
    fx["slow"] = slow_paths
    fx["slow_root"] = slow

    # 回收站（模拟 DSM 已启用），相似/精准删除都使用它
    os.makedirs(os.path.join(main, "#recycle"), exist_ok=True)
    return fx


# ---------------------------------------------------------------------------
# 扫描辅助
# ---------------------------------------------------------------------------
def start_scan(client, roots, options=None, excludes=None):
    return base.post_scan(client, roots, excludes=excludes, options=options)


def run_scan(client, roots, options=None, excludes=None, timeout=300):
    resp = start_scan(client, roots, options=options, excludes=excludes)
    data = base.jd(resp)
    scan_id = data.get("scan_id")
    if resp.status != 202 or not scan_id:
        return None, {}, resp
    scan = base.wait_scan(client, scan_id, timeout=timeout)
    return scan_id, scan, resp


def all_groups(client, scan_id, extra=None):
    groups, total, resp = base.fetch_groups(client, scan_id, limit=100)
    return groups, total, resp


def paths_of(groups, match_type=None):
    result = set()
    for group in groups:
        if match_type and (group.get("match_type") or "exact") != match_type:
            continue
        for item in group.get("files") or []:
            result.add(base.norm(item.get("path")))
    return result


def group_with(group_list, path):
    target = base.norm(path)
    for group in group_list:
        for item in group.get("files") or []:
            if base.norm(item.get("path")) == target:
                return group
    return None


def file_in(group_list, path):
    target = base.norm(path)
    for group in group_list:
        for item in group.get("files") or []:
            if base.norm(item.get("path")) == target:
                return item
    return None


def keeper(group):
    for item in group.get("files") or []:
        if item.get("is_keep"):
            return item
    return None


def copied_paths(groups):
    return {base.norm(item.get("path")) for group in groups
            for item in group.get("files") or [] if not item.get("is_keep")}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run():
    base_dir = tempfile.mkdtemp(prefix="dupqa-sim-")
    data_dir = os.path.join(base_dir, "data")
    os.makedirs(data_dir)
    fx = build_fixtures(base_dir)
    roots = [fx["main"], fx["slow_root"]]
    backend = None
    keep = "--keep" in sys.argv
    try:
        print("临时目录: %s" % base_dir)
        backend = base.Backend(roots, data_dir).start()
        client = base.Client("127.0.0.1", backend.port)
        client.login()
        version = expected_app_version()
        evidence("app_version_expected", version)
        evidence("backend_port", backend.port)

        # ---------------- 元信息 / 设置 ----------------
        resp = client.get("/api/meta")
        meta = base.jd(resp)
        sim = meta.get("similarity") if isinstance(meta.get("similarity"), dict) else {}
        defaults = meta.get("defaults") if isinstance(meta.get("defaults"), dict) else {}
        check("SIM-META-01", "/api/meta 报告 v1.1 版本与 similarity 能力",
              resp.status == 200 and meta.get("app_version") == version
              and sim.get("pillow_available") is not None
              and isinstance(sim.get("default_extensions"), list)
              and sim.get("default_threshold") == 85
              and defaults.get("scan_mode") == "exact",
              "status=%s version=%s sim=%s" % (resp.status, meta.get("app_version"), sim))
        check("SIM-META-02", "/api/meta defaults 含 similarity 默认键",
              defaults.get("similarity_threshold") == 85
              and defaults.get("similarity_concurrency") == 2
              and defaults.get("similarity_cache") is True
              and defaults.get("similarity_max_file_mb") == 32
              and defaults.get("similarity_allow_binary") is False,
              "defaults=%s" % {k: defaults.get(k) for k in defaults
                               if k.startswith("similarity") or k == "scan_mode"})

        resp = client.put_json("/api/settings", {"settings": {"scan_mode": "bad"}})
        code, _ = base.err_code(resp)
        check("SIM-SET-01", "非法 scan_mode 返回 INVALID_ARGUMENT",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))
        resp = client.put_json("/api/settings", {"settings": {"similarity_threshold": 101}})
        code, _ = base.err_code(resp)
        check("SIM-SET-02", "similarity_threshold 越界被拒绝",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))
        resp = client.put_json("/api/settings", {"settings": {"similarity_concurrency": 5}})
        code, _ = base.err_code(resp)
        check("SIM-SET-03", "similarity_concurrency 越界被拒绝",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))
        resp = client.put_json("/api/settings", {"settings": {"similarity_max_file_mb": 0}})
        code, _ = base.err_code(resp)
        check("SIM-SET-04", "similarity_max_file_mb 越界被拒绝",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))

        # ---------------- 相似扫描（主样本） ----------------
        options = {
            "scan_mode": "similar",
            "similarity_threshold": 85,
            "similarity_concurrency": 2,
            "similarity_cache": True,
            "similarity_max_file_mb": 1,
        }
        scan_id, scan, resp = run_scan(client, [fx["main"]], options=options)
        check("SIM-SCAN-01", "similar 模式扫描创建并完成",
              resp.status == 202 and bool(scan_id)
              and scan.get("status") == "completed" and scan.get("stage") == "done",
              "status=%s scan=%s" % (resp.status,
                                     {k: scan.get(k) for k in ("status", "stage", "error")}))
        if not scan_id:
            raise RuntimeError("无法创建相似扫描: %s" % resp.text())
        evidence("main_scan_id", scan_id)
        stats = scan.get("stats") or {}
        evidence("main_stats", stats)
        check("SIM-SCAN-02", "扫描 options 记录 scan_mode=similar / threshold=85",
              (scan.get("options") or {}).get("scan_mode") == "similar"
              and int((scan.get("options") or {}).get("similarity_threshold") or 0) == 85,
              "options=%s" % {k: (scan.get("options") or {}).get(k) for k in
                              ("scan_mode", "similarity_threshold",
                               "similarity_cache", "similarity_max_file_mb")})
        check("SIM-STATS-01", "stats 含 exact/similar 分类统计且自洽",
              int(stats.get("exact_groups") or 0) >= 2
              and int(stats.get("similar_groups") or 0) >= 2
              and int(stats.get("exact_files") or 0) >= 4
              and int(stats.get("similar_files") or 0) >= 4
              and int(stats.get("groups") or 0) ==
              int(stats.get("exact_groups") or 0) + int(stats.get("similar_groups") or 0)
              and int(stats.get("duplicate_files") or 0) ==
              int(stats.get("exact_files") or 0) + int(stats.get("similar_files") or 0),
              "stats=%s" % {k: stats.get(k) for k in
                            ("exact_groups", "similar_groups", "exact_files",
                             "similar_files", "groups", "duplicate_files")})
        check("SIM-STATS-02", "similarity_candidates/features/cache/skipped 计数存在",
              int(stats.get("similarity_candidates") or 0) >= 6
              and int(stats.get("similarity_features") or 0) >= 6
              and isinstance(stats.get("similarity_skipped"), dict)
              and int(stats.get("similarity_skipped", {}).get("binary") or 0) >= 4
              and int(stats.get("similarity_skipped", {}).get("size") or 0) >= 2,
              "sim stats=%s" % {k: stats.get(k) for k in
                                ("similarity_candidates", "similarity_features",
                                 "similarity_cache_hits", "similarity_cache_misses",
                                 "similarity_skipped")})
        check("SIM-STATS-03", "首次扫描缓存均为 miss、相似跳过 binary/size 分类正确",
              int(stats.get("similarity_cache_hits") or 0) == 0
              and int(stats.get("similarity_cache_misses") or 0) >= 6,
              "hits=%s misses=%s" % (stats.get("similarity_cache_hits"),
                                     stats.get("similarity_cache_misses")))

        groups, total, _ = all_groups(client, scan_id)
        evidence("groups_total", total)
        exact_groups = [g for g in groups if (g.get("match_type") or "exact") == "exact"]
        similar_groups = [g for g in groups if g.get("match_type") == "similar"]
        evidence("similar_group_scores", [
            {"group_id": g.get("group_id"),
             "score": g.get("similarity_score"),
             "method": g.get("similarity_method"),
             "count": g.get("count"),
             "paths": [os.path.basename(f.get("path") or "")
                       for f in g.get("files") or []]}
            for g in similar_groups])
        check("SIM-GROUP-01", "结果按 exact/similar 分区且数量与 stats 一致",
              len(exact_groups) == int(stats.get("exact_groups") or 0)
              and len(similar_groups) == int(stats.get("similar_groups") or 0)
              and total == len(groups),
              "exact=%s similar=%s stats=%s"
              % (len(exact_groups), len(similar_groups),
                 (stats.get("exact_groups"), stats.get("similar_groups"))))

        # 组 schema：精准组
        dup_group = group_with(groups, fx["dup1"])
        check("SIM-GROUP-02", "精准组 match_type=exact/similarity_score=100/hash 非空",
              dup_group is not None and dup_group.get("match_type") == "exact"
              and int(dup_group.get("similarity_score") or 0) == 100
              and bool(dup_group.get("hash"))
              and all((f.get("match_type") or "exact") == "exact"
                      and int(f.get("similarity_score") or 0) == 100
                      for f in dup_group.get("files") or []),
              "group=%s" % {k: dup_group.get(k) for k in
                            ("group_id", "match_type", "similarity_score", "hash")})

        # 组 schema：相似组（alpha/beta）
        sim_group = group_with(groups, fx["alpha"])
        sim_keep = keeper(sim_group) if sim_group else None
        alpha_sha = hashlib.sha256(open(fx["alpha"], "rb").read()).hexdigest()
        check("SIM-GROUP-03", "相似组 schema：hash/algo 为空、match_type/similarity/manual_review 正确",
              sim_group is not None and sim_group.get("match_type") == "similar"
              and sim_group.get("hash") == "" and sim_group.get("algo") == ""
              and int(sim_group.get("similarity_score") or 0) >= 85
              and sim_group.get("manual_review_required") is True
              and sim_group.get("similarity_method") in ("minhash", "phash", "bytes")
              and sim_group.get("low_confidence") is False,
              "group=%s" % {k: sim_group.get(k) for k in
                            ("group_id", "match_type", "hash", "algo",
                             "similarity_score", "similarity_method",
                             "manual_review_required", "low_confidence")}
              if sim_group else "no similar group for alpha.txt")
        check("SIM-GROUP-04", "相似组保留最新创建项、文件 hash 为内容 SHA256、默认不勾选",
              sim_group is not None
              and base.norm(sim_keep.get("path")) == base.norm(fx["beta"])
              and sim_group.get("keep_reason") == "latest_created"
              and len(sim_group.get("files") or []) == 2
              and all(f.get("default_selected") is False
                      for f in sim_group.get("files") or [])
              and (file_in([sim_group], fx["alpha"]).get("hash") or "").lower()
              == alpha_sha,
              "keep=%s reason=%s files=%s"
              % (sim_keep.get("path") if sim_keep else None,
                 sim_group.get("keep_reason") if sim_group else None,
                 [f.get("path") for f in (sim_group or {}).get("files") or []]))
        check("SIM-GROUP-05", "相似组文件 similarity_score：保留=100、副本>=阈值",
              sim_group is not None
              and sim_keep is not None and int(sim_keep.get("similarity_score") or 0) == 100
              and all(int(f.get("similarity_score") or 0) >= 85
                      for f in sim_group.get("files") or [] if not f.get("is_keep")),
              "scores=%s" % [(f.get("path"), f.get("similarity_score"))
                             for f in (sim_group or {}).get("files") or []])

        # 精准组不重复出现在相似组
        exact_paths = paths_of(groups, "exact")
        similar_paths = paths_of(groups, "similar")
        check("SIM-EXACT-ISOLATED", "精准组文件不再出现在相似组中",
              not (exact_paths & similar_paths)
              and base.norm(fx["dup1"]) not in similar_paths
              and base.norm(fx["zip_exact"][0]) not in similar_paths,
              "overlap=%s" % sorted(exact_paths & similar_paths)[:10])

        # 低于阈值不成组
        check("SIM-THRESHOLD", "低于阈值的文本对不成相似组",
              base.norm(fx["one"]) not in similar_paths
              and base.norm(fx["two"]) not in similar_paths,
              "one/two in similar groups?")

        # 二进制/压缩包/媒体/超限跳过
        skipped = stats.get("similarity_skipped") or {}
        binary_paths = [fx["zip_sim"][0], fx["zip_sim"][1],
                        fx["media"][0], fx["media"][1]]
        check("SIM-SKIP-BINARY", "压缩包/媒体不进入相似组且计入 binary 跳过",
              all(base.norm(p) not in similar_paths for p in binary_paths)
              and int(skipped.get("binary") or 0) >= 4,
              "skipped=%s paths=%s" % (skipped, binary_paths))
        check("SIM-SKIP-SIZE", "超过 similarity_max_file_mb 的文件跳过相似计算",
              base.norm(fx["big"][0]) not in similar_paths
              and base.norm(fx["big"][1]) not in similar_paths
              and int(skipped.get("size") or 0) >= 2,
              "skipped=%s" % skipped)
        # 压缩包/媒体仍可精准哈希命中
        zip_group = group_with(groups, fx["zip_exact"][0])
        check("SIM-EXACT-BINARY", "压缩包虽跳过相似，但精准哈希仍识别重复",
              zip_group is not None and zip_group.get("match_type") == "exact"
              and int(zip_group.get("count") or 0) == 2,
              "zip group=%s" % (zip_group.get("group_id") if zip_group else None))

        # groups match_type 过滤 / exact_total / similar_total / 排序
        resp = client.get(base.q("/api/scans/%s/groups" % scan_id,
                                 offset=0, limit=100, match_type="exact"))
        data = base.jd(resp)
        filtered_exact = data.get("groups") or []
        check("SIM-GROUPS-FILTER-EXACT", "groups?match_type=exact 只返回精准组并给出 totals",
              resp.status == 200
              and bool(data.get("match_type") == "exact")
              and int(data.get("exact_total") or 0) == len(exact_groups)
              and int(data.get("similar_total") or 0) == len(similar_groups)
              and int(data.get("total") or 0) == len(exact_groups)
              and all((g.get("match_type") or "exact") == "exact"
                      for g in filtered_exact),
              "status=%s data=%s" % (resp.status,
                                     {k: data.get(k) for k in
                                      ("total", "match_type", "exact_total",
                                       "similar_total")}))
        resp = client.get(base.q("/api/scans/%s/groups" % scan_id,
                                 offset=0, limit=100, match_type="similar",
                                 sort="similarity_desc"))
        data = base.jd(resp)
        filtered_similar = data.get("groups") or []
        scores = [int(g.get("similarity_score") or 0) for g in filtered_similar]
        check("SIM-GROUPS-FILTER-SIMILAR", "groups?match_type=similar 只返回相似组且按相似度降序",
              resp.status == 200 and int(data.get("total") or 0) == len(similar_groups)
              and int(data.get("similar_total") or 0) == len(similar_groups)
              and all(g.get("match_type") == "similar" for g in filtered_similar)
              and scores == sorted(scores, reverse=True),
              "total=%s scores=%s" % (data.get("total"), scores))
        resp = client.get(base.q("/api/scans/%s/groups" % scan_id,
                                 offset=0, limit=100, match_type="bogus"))
        code, _ = base.err_code(resp)
        check("SIM-GROUPS-FILTER-BAD", "非法 match_type 返回 INVALID_ARGUMENT",
              resp.status == 400 and code == "INVALID_ARGUMENT",
              "status=%s code=%s" % (resp.status, code))

        # ---------------- CSV 新列 ----------------
        resp = client.get("/api/scans/%s/csv" % scan_id)
        body = resp.body
        text = body.decode("utf-8-sig")
        import csv as csv_mod
        import io as io_mod
        rows = list(csv_mod.reader(io_mod.StringIO(text)))
        expected_header = ["组号", "匹配类型", "状态", "文件路径", "大小(字节)",
                           "SHA256/MD5", "创建时间", "修改时间", "保留",
                           "默认勾选", "时间来源", "相似度分值"]
        check("SIM-CSV-01", "CSV 新表头（匹配类型/相似度分值）逐字正确且带 BOM",
              resp.status == 200 and body.startswith(b"\xef\xbb\xbf")
              and rows and rows[0] == expected_header,
              "header=%s" % (rows[0] if rows else None))
        data_rows = rows[1:] if rows else []
        match_labels = {row[1] for row in data_rows if len(row) >= 12}
        similar_row = None
        exact_row = None
        for row in data_rows:
            if len(row) < 12:
                continue
            if base.norm(row[3]) == base.norm(fx["alpha"]):
                similar_row = row
            if base.norm(row[3]) == base.norm(fx["dup1"]):
                exact_row = row
        check("SIM-CSV-02", "CSV 匹配类型标签为 哈希一致/内容相似",
              u"哈希一致" in match_labels and u"内容相似" in match_labels
              and similar_row is not None and exact_row is not None,
              "labels=%s similar_row=%s exact_row=%s"
              % (match_labels, similar_row, exact_row))
        check("SIM-CSV-03", "CSV 相似度分值：精准=100、相似副本>=85、相似文件默认勾选=否",
              exact_row is not None and int(exact_row[11]) == 100
              and similar_row is not None and int(similar_row[11]) >= 85
              and similar_row[9] == u"否",
              "exact=%s similar=%s" % (exact_row, similar_row))
        keep_rows = [row for row in data_rows
                     if len(row) >= 12 and row[8] == u"是"
                     and base.norm(row[3]) == base.norm(fx["beta"])]
        check("SIM-CSV-04", "CSV 相似组保留行标记保留/分值 100",
              bool(keep_rows) and int(keep_rows[0][11]) == 100
              and keep_rows[0][10] in ("statx", "birthtime"),
              "keep_rows=%s" % keep_rows)

        # ---------------- 特征缓存第二次扫描命中 ----------------
        scan2_id, scan2, resp2 = run_scan(client, [fx["main"]], options=options)
        stats2 = (scan2 or {}).get("stats") or {}
        check("SIM-CACHE-01", "同一内容第二次扫描特征缓存命中（cache_hits>0）",
              resp2.status == 202 and (scan2 or {}).get("status") == "completed"
              and int(stats2.get("similarity_cache_hits") or 0) >= 6,
              "status=%s hits=%s misses=%s"
              % ((scan2 or {}).get("status"), stats2.get("similarity_cache_hits"),
                 stats2.get("similarity_cache_misses")))
        cache_count = 0
        try:
            cache_conn = sqlite3.connect(os.path.join(data_dir, "dupcleaner.sqlite3"))
            try:
                cache_count = int(cache_conn.execute(
                    "SELECT COUNT(*) FROM feature_cache").fetchone()[0])
            finally:
                cache_conn.close()
        except sqlite3.Error:
            cache_count = 0
        check("SIM-CACHE-02", "feature_cache 持久化在 SQLite 数据目录",
              cache_count > 0, "cache rows missing")
        groups2, total2, _ = all_groups(client, scan2_id) if scan2_id else ([], 0, None)
        similar2 = [g for g in groups2 if g.get("match_type") == "similar"]
        check("SIM-CACHE-03", "第二次扫描相似组数量稳定",
              len(similar2) == len(similar_groups)
              and int(stats2.get("similar_groups") or 0) == len(similar_groups),
              "first=%s second=%s" % (len(similar_groups), len(similar2)))
        evidence("cache_scan_stats", {k: stats2.get(k) for k in
                                      ("similar_groups", "similarity_candidates",
                                       "similarity_features", "similarity_cache_hits",
                                       "similarity_cache_misses")})

        # ---------------- 扫描后修改相似文件 -> 删除前重校验跳过 ----------------
        with open(fx["theta"], "ab") as handle:
            handle.write(b"\nMODIFIED-AFTER-SCAN\n")
        theta_info = file_in(groups, fx["theta"])
        theta_group = group_with(groups, fx["theta"])
        resp = client.post_json(
            "/api/scans/%s/delete-plan" % scan_id,
            {"file_ids": [theta_info.get("file_id")], "recycle": True,
             "confirm_similar": True,
             "reviewed_similar_group_ids": [theta_group.get("group_id")]})
        code, details = base.err_code(resp)
        skipped2 = details.get("skipped") if isinstance(details.get("skipped"), list) else []
        check("SIM-DEL-CHANGED", "扫描后被修改的相似文件在计划阶段被跳过（FILE_CHANGED）",
              resp.status == 400 and code == "PLAN_INVALID"
              and any(item.get("reason") == "FILE_CHANGED" for item in skipped2)
              and os.path.isfile(fx["theta"]),
              "status=%s code=%s details=%s" % (resp.status, code, details))

        # ---------------- scope=all_duplicates 排除相似组 ----------------
        resp = client.post_json(
            "/api/scans/%s/delete-plan" % scan_id,
            {"scope": "all_duplicates", "exclude_file_ids": [], "recycle": True})
        data = base.jd(resp)
        plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
        plan_files = plan.get("files") if isinstance(plan.get("files"), list) else []
        expected_exact_copies = sum(int(g.get("count") or 0) - 1 for g in exact_groups)
        check("SIM-DEL-SCOPE", "scope=all_duplicates 只含精准组、不含相似组",
              resp.status == 200 and plan.get("count") == expected_exact_copies
              and int(plan.get("similar_count") or 0) == 0
              and int(plan.get("exact_count") or 0) == expected_exact_copies
              and all((item.get("match_type") or "exact") == "exact"
                      for item in plan_files)
              and all(not str(item.get("group_id") or "").startswith("s-")
                      for item in plan_files)
              and all(not str(item.get("file_id") or "").startswith("sf-")
                      for item in plan_files),
              "status=%s plan=%s files=%s"
              % (resp.status, {k: plan.get(k) for k in
                               ("count", "exact_count", "similar_count")},
                 [(f.get("match_type"), f.get("file_id")) for f in plan_files[:5]]))
        evidence("scope_plan", {k: plan.get(k) for k in
                                ("plan_id", "count", "exact_count", "similar_count")})

        # ---------------- 相似删除：无人工复核 -> 400 ----------------
        alpha_info = file_in(groups, fx["alpha"])
        alpha_group = group_with(groups, fx["alpha"])
        alpha_gid = alpha_group.get("group_id")
        resp = client.post_json(
            "/api/scans/%s/delete-plan" % scan_id,
            {"file_ids": [alpha_info.get("file_id")], "recycle": True})
        code, _ = base.err_code(resp)
        check("SIM-DEL-REVIEW-01", "相似文件未带 confirm_similar 返回 400 SIMILAR_REVIEW_REQUIRED",
              resp.status == 400 and code == "SIMILAR_REVIEW_REQUIRED",
              "status=%s code=%s" % (resp.status, code))
        resp = client.post_json(
            "/api/scans/%s/delete-plan" % scan_id,
            {"file_ids": [alpha_info.get("file_id")], "recycle": True,
             "confirm_similar": True})
        code, _ = base.err_code(resp)
        check("SIM-DEL-REVIEW-02", "confirm_similar=true 但缺少 reviewed 组仍返回 400",
              resp.status == 400 and code == "SIMILAR_REVIEW_REQUIRED",
              "status=%s code=%s" % (resp.status, code))

        # ---------------- 相似删除：带复核 -> plan + 回收站删除 ----------------
        resp = client.post_json(
            "/api/scans/%s/delete-plan" % scan_id,
            {"file_ids": [alpha_info.get("file_id")], "recycle": True,
             "confirm_similar": True, "reviewed_similar_group_ids": [alpha_gid]})
        data = base.jd(resp)
        plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
        check("SIM-DEL-PLAN", "带人工复核的相似删除计划生成（similar_count/评分/manual_review）",
              resp.status == 200 and plan.get("count") == 1
              and int(plan.get("similar_count") or 0) == 1
              and int(plan.get("exact_count") or 0) == 0
              and plan.get("manual_review") is True
              and alpha_gid in (plan.get("reviewed_similar_group_ids") or [])
              and any(item.get("match_type") == "similar"
                      and int(item.get("similarity_score") or 0) >= 85
                      for item in plan.get("files") or []),
              "status=%s plan=%s" % (resp.status, plan))
        plan_id = plan.get("plan_id")
        resp = client.post_json(
            "/api/scans/%s/delete" % scan_id,
            {"plan_id": plan_id, "confirm": "DELETE"})
        job_id = base.jd(resp).get("job_id")
        job = base.wait_job(client, scan_id, job_id) if job_id else {}
        results = job.get("results") if isinstance(job.get("results"), list) else []
        recycle_paths = [item.get("recycle_path") for item in results]
        check("SIM-DEL-EXEC", "相似文件经回收站删除成功且保留项仍在",
              resp.status == 202 and job.get("status") == "completed"
              and not os.path.exists(fx["alpha"]) and os.path.isfile(fx["beta"])
              and all(str(p or "").replace("\\", "/").find("/#recycle/@duplicate-cleaner/") >= 0
                      for p in recycle_paths),
              "status=%s job=%s recycle=%s exists=%s"
              % (resp.status, {k: job.get(k) for k in ("status", "done", "failed")},
                 recycle_paths, [os.path.exists(fx["alpha"]), os.path.exists(fx["beta"])]))
        evidence("similar_delete_job", {k: job.get(k) for k in
                                        ("job_id", "status", "done", "failed")})

        # ---------------- 相似 keeper 不可删除 ----------------
        delta_info = file_in(groups, fx["delta"])
        delta_group = group_with(groups, fx["delta"])
        delta_gid = delta_group.get("group_id")
        resp = client.post_json(
            "/api/scans/%s/delete-plan" % scan_id,
            {"file_ids": [delta_info.get("file_id")], "recycle": True,
             "confirm_similar": True,
             "reviewed_similar_group_ids": [delta_gid]})
        code, details = base.err_code(resp)
        skipped3 = details.get("skipped") if isinstance(details.get("skipped"), list) else []
        check("SIM-DEL-KEEPER", "相似组保留项提交删除被拒（IS_KEEPER）",
              resp.status == 400 and code == "PLAN_INVALID"
              and any(item.get("reason") == "IS_KEEPER" for item in skipped3)
              and os.path.isfile(fx["delta"]),
              "status=%s code=%s details=%s" % (resp.status, code, details))

        # ---------------- 混合：精准+相似 删除计划 ----------------
        dup_copy = file_in(groups, fx["dup1"])
        dup_keep = file_in(groups, fx["dup2"])
        gamma_info = file_in(groups, fx["gamma"])
        gamma_group = group_with(groups, fx["gamma"])
        gamma_gid = gamma_group.get("group_id")
        resp = client.post_json(
            "/api/scans/%s/delete-plan" % scan_id,
            {"file_ids": [dup_copy.get("file_id"), gamma_info.get("file_id")],
             "recycle": True, "confirm_similar": True,
             "reviewed_similar_group_ids": [gamma_gid]})
        data = base.jd(resp)
        plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
        check("SIM-DEL-MIXED-PLAN", "混合精准+相似计划分别统计 exact/similar",
              resp.status == 200 and plan.get("count") == 2
              and int(plan.get("exact_count") or 0) == 1
              and int(plan.get("similar_count") or 0) == 1
              and {item.get("match_type") for item in plan.get("files") or []}
              == {"exact", "similar"},
              "status=%s plan=%s" % (resp.status, plan))
        mixed_plan_id = plan.get("plan_id")
        resp = client.post_json(
            "/api/scans/%s/delete" % scan_id,
            {"plan_id": mixed_plan_id, "confirm": "DELETE"})
        mixed_job_id = base.jd(resp).get("job_id")
        mixed_job = base.wait_job(client, scan_id, mixed_job_id) if mixed_job_id else {}
        check("SIM-DEL-MIXED-EXEC", "混合删除任务完成，精准/相似副本均移入回收站，保留项保留",
              mixed_job.get("status") == "completed"
              and not os.path.exists(fx["dup1"]) and os.path.isfile(fx["dup2"])
              and not os.path.exists(fx["gamma"]) and os.path.isfile(fx["delta"]),
              "job=%s exists=%s" % ({k: mixed_job.get(k) for k in
                                     ("status", "done", "failed")},
                                    [os.path.exists(p) for p in
                                     [fx["dup1"], fx["dup2"], fx["gamma"], fx["delta"]]]))

        # ---------------- 审计记录包含相似 review 信息 ----------------
        audit_path = os.path.join(data_dir, "audit.jsonl")
        plan_audit = []
        if os.path.isfile(audit_path):
            with open(audit_path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if item.get("action") == "delete_plan":
                        plan_audit.append(item.get("detail") or {})
        check("SIM-AUDIT", "审计 delete_plan 记录 similar_count/manual_review 信息",
              any(int(detail.get("similar_count") or 0) >= 1
                  and detail.get("manual_review") is True
                  for detail in plan_audit),
              "plan audit=%s" % plan_audit[:3])

        # ---------------- 相似阶段停止 ----------------
        stop_options = {
            "scan_mode": "similar",
            "similarity_threshold": 85,
            "similarity_concurrency": 1,
            "similarity_cache": False,
            "similarity_max_file_mb": 4,
            "hash_rate_mbps": 1,
        }
        resp = start_scan(client, [fx["slow_root"]], options=stop_options)
        stop_id = base.jd(resp).get("scan_id")
        observed_stage = None
        stopped_ok = False
        completed_early = False
        final_status = None
        if stop_id:
            deadline = time.time() + 120
            while time.time() < deadline:
                cur = client.get("/api/scans/" + stop_id)
                scan_cur = base.jd(cur).get("scan") or {}
                status = scan_cur.get("status")
                observed_stage = scan_cur.get("stage") or observed_stage
                if status == "completed":
                    completed_early = True
                    break
                if status == "running" and scan_cur.get("stage") == "similarity":
                    resp = client.post_json("/api/scans/%s/stop" % stop_id, {})
                    final = base.wait_scan(client, stop_id, timeout=60)
                    final_status = final.get("status")
                    stopped_ok = final_status == "stopped"
                    break
                if status in ("failed", "stopped"):
                    break
                time.sleep(0.1)
        if completed_early:
            record("SIM-STOP", "相似阶段可停止", "UNVERIFIED",
                   "扫描在观察到 similarity 阶段前已完成（可能样本偏小）；observed_stage=%s"
                   % observed_stage)
        else:
            check("SIM-STOP", "相似阶段可停止（观察 similarity 阶段后 stop -> stopped）",
                  stopped_ok and observed_stage == "similarity",
                  "stop_id=%s observed_stage=%s stopped=%s"
                  % (stop_id, observed_stage, stopped_ok))
            evidence("stop_scan", {"scan_id": stop_id,
                                   "observed_stage": observed_stage,
                                   "final_status": final_status,
                                   "stopped": stopped_ok})

        # ---------------- 日志包含相似扫描信息 ----------------
        resp = client.get(base.q("/api/logs", limit=100))
        data = base.jd(resp)
        logs = data.get("logs") if isinstance(data.get("logs"), list) else []
        log_names = [item.get("name") for item in logs if item.get("name")]
        check("SIM-LOGS-01", "扫描日志列表包含本次相似扫描日志",
              resp.status == 200 and any(scan_id in (name or "") for name in log_names),
              "status=%s logs=%s" % (resp.status, log_names[:10]))
        target_log = next((name for name in log_names
                           if scan_id in (name or "")), None)
        if target_log:
            resp = client.get(base.q("/api/logs/download", name=target_log))
            log_text = resp.text()
            check("SIM-LOGS-02", "扫描日志记录 scan_mode/threshold/similar_groups",
                  resp.status == 200 and "scan_mode: similar" in log_text
                  and "similarity_threshold: 85" in log_text
                  and "similar_groups:" in log_text,
                  "status=%s head=%r" % (resp.status, log_text[:300]))
        else:
            record("SIM-LOGS-02", "扫描日志内容", "FAIL", "未找到扫描日志")

    finally:
        if backend is not None:
            backend.stop()
        if keep:
            print("保留临时目录: %s" % base_dir)
        else:
            shutil.rmtree(base_dir, ignore_errors=True)


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
    with open(RESULT_JSON, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print("\n===== SIMILARITY E2E SUMMARY: PASS=%d FAIL=%d UNVERIFIED=%d ====="
          % (len(passed), len(failed), len(unverified)))
    for item in failed:
        print("  FAIL %s %s :: %s" % (item["id"], item["name"], item["detail"][:400]))
    for item in unverified:
        print("  UNVERIFIED %s %s :: %s" % (item["id"], item["name"], item["detail"][:300]))
    return payload


def main():
    rc = 0
    try:
        run()
    except Exception:
        traceback.print_exc()
        record("SIM-HARNESS", "相似度端到端脚本执行", "FAIL",
               traceback.format_exc()[-2000:])
        rc = 2
    payload = summarize()
    if payload["summary"]["FAIL"] > 0:
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
