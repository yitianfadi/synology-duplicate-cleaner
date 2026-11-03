# -*- coding: utf-8 -*-
"""P1 benchmark: 100k scan_files 路径搜索（FTS5 trigram vs 强制 LIKE）。

用法（仓库根目录）:
    python qa/bench_search.py

构造临时 SQLite（10k 组 × 10 文件 = 100k scan_files 行），度量：
- 写入 100k 行（含 FTS 触发器同步）耗时；
- FTS 路径搜索 P50/P95（命中 needle 与未命中 needle）；
- 强制 LIKE '%needle%' 的 P50/P95 对比；
- FTS 一致性（scan_files 行数 == FTS 行数）。
目标：FTS P95 < 500ms。仅标准库，Python 3.8 语法。
"""

from __future__ import absolute_import

import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "backend"))

from dupcleaner.store import Store  # noqa: E402

SCAN_ID = "20261103-120000-beef00"
GROUP_COUNT = 10000
FILES_PER_GROUP = 10
ITERATIONS = 30
WARMUP = 3
P95_TARGET_MS = 500.0


def percentile(values, ratio):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * ratio))
    return ordered[max(0, min(len(ordered) - 1, index))]


def build_groups():
    groups = []
    for group_index in range(GROUP_COUNT):
        size = 1024 + group_index
        bucket = group_index % 100
        files = []
        for file_index in range(FILES_PER_GROUP):
            path = ("/share/bench/group-%05d/bucket-%02d/file-%02d-%05d.txt"
                    % (group_index, bucket, file_index, group_index))
            files.append({
                "file_id": "f-%05d-%02d" % (group_index, file_index),
                "path": path,
                "name": os.path.basename(path),
                "size": size,
                "is_keep": file_index == 0,
                "default_selected": file_index != 0,
            })
        groups.append({
            "group_id": "g-%06d" % (group_index + 1),
            "match_type": "exact" if group_index % 2 == 0 else "similar",
            "wasted_bytes": size * (FILES_PER_GROUP - 1),
            "size": size,
            "similarity_score": 100 if group_index % 2 == 0 else 90,
            "hash": "h%08d" % group_index,
            "files": files,
        })
    return groups


def measure(store, needle, use_fts, iterations=ITERATIONS):
    # 预热
    for _ in range(WARMUP):
        store.query_scan_groups_page(
            SCAN_ID, offset=0, limit=50, needle=needle, use_fts=use_fts)
    timings = []
    total = 0
    for _ in range(iterations):
        started = time.perf_counter()
        page = store.query_scan_groups_page(
            SCAN_ID, offset=0, limit=50, needle=needle, use_fts=use_fts)
        timings.append((time.perf_counter() - started) * 1000.0)
        total = page["total"]
    return {
        "total": total,
        "p50_ms": round(percentile(timings, 0.50), 2),
        "p95_ms": round(percentile(timings, 0.95), 2),
        "max_ms": round(max(timings), 2),
    }


def main():
    tmpdir = tempfile.mkdtemp(prefix="dc-bench-search-")
    store = None
    try:
        store = Store(tmpdir)
        store.save_scan_meta(SCAN_ID, {
            "scan_id": SCAN_ID, "status": "completed",
            "created_epoch": time.time(), "stats": {},
        })
        groups = build_groups()
        started = time.perf_counter()
        store.write_scan_groups(SCAN_ID, groups)
        write_seconds = time.perf_counter() - started
        file_rows = int(store.db.scalar(
            "SELECT COUNT(*) FROM scan_files", (), 0) or 0)
        fts_rows = int(store.db.scalar(
            "SELECT COUNT(*) FROM scan_files_fts", (), 0) or 0
        ) if store.db.fts_enabled else 0
        hit_needle = "bucket-42"
        miss_needle = "zz-no-such-token"
        fts_hit = measure(store, hit_needle, True)
        fts_miss = measure(store, miss_needle, True)
        like_hit = measure(store, hit_needle, False)
        like_miss = measure(store, miss_needle, False)
        result = {
            "rows": file_rows,
            "groups": GROUP_COUNT,
            "fts_available": bool(store.db.fts_enabled),
            "fts_note": store.db.fts_note,
            "fts_rows": fts_rows,
            "write_100k_seconds": round(write_seconds, 3),
            "iterations": ITERATIONS,
            "needle_hit": hit_needle,
            "needle_miss": miss_needle,
            "fts_hit": fts_hit,
            "fts_miss": fts_miss,
            "like_hit": like_hit,
            "like_miss": like_miss,
            "p95_target_ms": P95_TARGET_MS,
            "fts_hit_p95_ok": (not store.db.fts_enabled
                               or fts_hit["p95_ms"] < P95_TARGET_MS),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("")
        print("[bench] rows=%d groups=%d fts=%s write=%.3fs"
              % (file_rows, GROUP_COUNT, store.db.fts_enabled,
                 write_seconds))
        print("[bench] FTS  hit: total=%d p50=%.2fms p95=%.2fms"
              % (fts_hit["total"], fts_hit["p50_ms"], fts_hit["p95_ms"]))
        print("[bench] FTS  miss: total=%d p50=%.2fms p95=%.2fms"
              % (fts_miss["total"], fts_miss["p50_ms"], fts_miss["p95_ms"]))
        print("[bench] LIKE hit: total=%d p50=%.2fms p95=%.2fms"
              % (like_hit["total"], like_hit["p50_ms"], like_hit["p95_ms"]))
        print("[bench] LIKE miss: total=%d p50=%.2fms p95=%.2fms"
              % (like_miss["total"], like_miss["p50_ms"],
                 like_miss["p95_ms"]))
        print("[bench] target P95 < %.0fms: %s"
              % (P95_TARGET_MS,
                 "PASS" if result["fts_hit_p95_ok"] else "FAIL (fallback/LIKE)"))
        return 0
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
