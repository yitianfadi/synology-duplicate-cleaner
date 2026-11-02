# -*- coding: utf-8 -*-
"""SQLite schema v2 结果索引 / SQL 分页、搜索、排序、统计测试（v0016 plan A）。

覆盖：v1→v2 迁移与回填（幂等）、SQL 路径不遍历全部 group payload、
与旧内存实现的分页/搜索/排序/计数/统计 parity、10k 组合性能边界、
旧 JSON 回退。
"""

from __future__ import absolute_import

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest

from tests.helpers import BACKEND_DIR, Harness, write_text  # noqa: F401

from dupcleaner import config as app_config  # noqa: F401
from dupcleaner import db as db_module
from dupcleaner import fsutil
from dupcleaner import scanner as scanner_module
from dupcleaner.store import Store

SORT_KEYS = ("wasted_desc", "size_desc", "created_desc", "similarity_desc")


def _file(file_id, path, size, created=0, is_keep=False, selected=False):
    return {
        "file_id": file_id,
        "path": path,
        "name": os.path.basename(path),
        "size": size,
        "created_ts": created,
        "is_keep": is_keep,
        "default_selected": selected,
        "hash": "sha-" + file_id,
    }


def _group(group_id, match_type, wasted, size, score, hash_, files):
    return {
        "group_id": group_id,
        "match_type": match_type,
        "wasted_bytes": wasted,
        "size": size,
        "similarity_score": score,
        "hash": hash_,
        "files": files,
    }


def _sample_groups():
    groups = []
    for index in range(6):
        base = "/share/docs/exact-%02d" % index
        size = 100 + index * 10
        groups.append(_group(
            "g-%06d" % (index + 1), "exact", size * 2, size, 100,
            "ecb%02d" % index + "f" * 56,
            [_file("f-%06d" % (index * 2 + 1),
                   base + "/alpha-%02d.txt" % index, size, 1000 + index * 10,
                   True, False),
             _file("f-%06d" % (index * 2 + 2),
                   base + "/beta-%02d.txt" % index, size, 2000 + index * 10,
                   False, True)]))
    groups.append(_group(
        "s-000001", "similar", 300, 150, 88, "",
        [_file("sf-000001", "/share/中文/文档-a.txt", 150, 3000, True, False),
         _file("sf-000002", "/share/中文/文档-b.txt", 120, 3100, False, True),
         _file("sf-000003", "/share/中文/文档-c.txt", 90, 3200, False, False)]))
    groups.append(_group(
        "v-000001", "video", 500, 500, 93, "",
        [_file("vf-000001", "/share/movie/video-alpha.mp4", 500, 4000, True, False),
         _file("vf-000002", "/share/movie/video-beta.mp4", 500, 4100, False, True)]))
    # match_type 缺失（旧数据）按 exact 计数/过滤
    groups.append({
        "group_id": "g-000099",
        "wasted_bytes": 777,
        "size": 77,
        "similarity_score": 100,
        "hash": "",
        "files": [_file("f-000099", "/share/legacy/percent%_under.txt", 77, 5000)],
    })
    return groups


def _reference_page(groups, offset, limit, needle, sort, match_type):
    """旧 groups_page 内存算法（parity 基准）。"""
    def group_match_type(group):
        return group.get("match_type") or "exact"

    def matches(group):
        if not needle:
            return True
        if needle in (group.get("hash") or "").lower():
            return True
        for file_info in group.get("files") or []:
            if needle in (file_info.get("path") or "").lower():
                return True
            if needle in (file_info.get("name") or "").lower():
                return True
        return False

    def sort_key(group):
        wasted = int(group.get("wasted_bytes") or 0)
        size = int(group.get("size") or 0)
        if sort == "size_desc":
            return (size, wasted, group.get("group_id") or "")
        if sort == "created_desc":
            created = 0
            for file_info in group.get("files") or []:
                created = max(created, int(file_info.get("created_ts") or 0))
            return (created, wasted, group.get("group_id") or "")
        if sort == "similarity_desc":
            similarity = 100
            if group_match_type(group) in ("similar", "video"):
                similarity = int(group.get("similarity_score") or 0)
            return (similarity, wasted, size, group.get("group_id") or "")
        return (wasted, size, group.get("group_id") or "")

    exact_total = similar_total = video_total = total = 0
    kept = []
    for group in groups:
        group_type = group_match_type(group)
        if group_type == "similar":
            similar_total += 1
        elif group_type == "video":
            video_total += 1
        else:
            exact_total += 1
        if match_type is not None and group_match_type(group) != match_type:
            continue
        if not matches(group):
            continue
        total += 1
        kept.append((sort_key(group), group.get("group_id"), group))
    kept.sort(key=lambda item: item[0], reverse=True)
    page = [item[2] for item in kept[offset:offset + limit]]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "match_type": match_type,
        "exact_total": exact_total,
        "similar_total": similar_total,
        "video_total": video_total,
        "groups": page,
    }


def _reference_stats(groups):
    extension_counts = {}
    extension_bytes = {}
    dir_counts = {}
    for group in groups:
        size = int(group.get("size") or 0)
        for file_info in group.get("files") or []:
            name = file_info.get("name") or ""
            extension = fsutil.extension_of(name).lower() or u"(无后缀)"
            extension_counts[extension] = extension_counts.get(extension, 0) + 1
            extension_bytes[extension] = extension_bytes.get(extension, 0) + size
            directory = os.path.dirname(file_info.get("path") or "")
            dir_counts[directory] = dir_counts.get(directory, 0) + 1
    by_extension = sorted(
        [{"extension": key, "count": value,
          "bytes": extension_bytes.get(key, 0)}
         for key, value in extension_counts.items()],
        key=lambda item: (-item["count"], item["extension"]))[:20]
    top_dirs = sorted(
        [{"path": key, "count": value} for key, value in dir_counts.items()],
        key=lambda item: (-item["count"], item["path"]))[:20]
    return by_extension, top_dirs


def _make_v1_db(path, groups, scan_id):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE schema_meta ("
                     "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE scans ("
                     "scan_id TEXT PRIMARY KEY, meta_json TEXT NOT NULL,"
                     " created_epoch REAL NOT NULL, updated_epoch REAL NOT NULL,"
                     " status TEXT NOT NULL,"
                     " interrupted INTEGER NOT NULL DEFAULT 0)")
        conn.execute("CREATE TABLE scan_groups ("
                     "scan_id TEXT NOT NULL, ordinal INTEGER NOT NULL,"
                     " group_id TEXT NOT NULL, match_type TEXT,"
                     " payload_json TEXT NOT NULL,"
                     " PRIMARY KEY (scan_id, ordinal))")
        conn.execute("CREATE INDEX idx_scan_groups_type"
                     " ON scan_groups(scan_id, match_type)")
        conn.execute("INSERT INTO schema_meta(key, value)"
                     " VALUES('schema_version', '1')")
        conn.execute("PRAGMA user_version = 1")
        meta = {"scan_id": scan_id, "status": "completed",
                "created_epoch": 1000.0, "stats": {}}
        conn.execute(
            "INSERT INTO scans(scan_id, meta_json, created_epoch,"
            " updated_epoch, status, interrupted) VALUES(?, ?, ?, ?, ?, 0)",
            (scan_id, json.dumps(meta), 1000.0, 1000.0, "completed"))
        for ordinal, group in enumerate(groups):
            conn.execute(
                "INSERT INTO scan_groups(scan_id, ordinal, group_id,"
                " match_type, payload_json) VALUES(?, ?, ?, ?, ?)",
                (scan_id, ordinal, group["group_id"],
                 group.get("match_type"),
                 json.dumps(group, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


class SchemaV2MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-v2-")
        self.scan_id = scanner_module.make_scan_id()
        self.groups = _sample_groups()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_v1_to_v2_migration_backfills_and_is_idempotent(self):
        db_path = os.path.join(self.tmpdir, "dupcleaner.sqlite3")
        _make_v1_db(db_path, self.groups, self.scan_id)
        store = Store(self.tmpdir)
        try:
            self.assertEqual(store.db.schema_version(),
                             db_module.CURRENT_SCHEMA_VERSION)
            columns = {row[1] for row in store.db.query(
                "PRAGMA table_info(scan_groups)")}
            for column in ("wasted_bytes", "size", "similarity_score",
                           "created_epoch", "group_hash"):
                self.assertIn(column, columns)
            self.assertEqual(store.count_scan_groups(self.scan_id),
                             len(self.groups))
            expected_files = sum(len(group.get("files") or [])
                                 for group in self.groups)
            self.assertEqual(store.count_scan_files(self.scan_id),
                             expected_files)
            # scan_groups 索引列与 payload 一致
            row = store.db.query_one(
                "SELECT wasted_bytes, size, similarity_score, created_epoch,"
                " group_hash FROM scan_groups WHERE scan_id=? AND ordinal=?",
                (self.scan_id, 0))
            first = self.groups[0]
            self.assertEqual(int(row["wasted_bytes"]),
                             int(first["wasted_bytes"]))
            self.assertEqual(int(row["size"]), int(first["size"]))
            self.assertEqual(int(row["similarity_score"]), 100)
            self.assertEqual(int(row["created_epoch"]),
                             max(int(item["created_ts"])
                                 for item in first["files"]))
            self.assertEqual(row["group_hash"], first["hash"])
            # migration 再跑一次也不产生重复文件行（幂等）
            with store.db.transaction():
                store.db._migrate_v2()
            self.assertEqual(store.count_scan_files(self.scan_id),
                             expected_files)
            # 迁移后 SQL 分页与旧实现一致
            got = store.query_scan_groups_page(
                self.scan_id, offset=0, limit=50, needle="",
                sort="wasted_desc", match_type=None)
            expected = _reference_page(self.groups, 0, 50, "", "wasted_desc", None)
            self.assertEqual(got["total"], expected["total"])
            self.assertEqual([item["group_id"] for item in got["groups"]],
                             [item["group_id"] for item in expected["groups"]])
        finally:
            store.close()

    def test_reopen_after_migration_does_not_duplicate(self):
        db_path = os.path.join(self.tmpdir, "dupcleaner.sqlite3")
        _make_v1_db(db_path, self.groups, self.scan_id)
        store = Store(self.tmpdir)
        store.close()
        reopened = Store(self.tmpdir)
        try:
            self.assertEqual(reopened.db.schema_version(),
                             db_module.CURRENT_SCHEMA_VERSION)
            self.assertEqual(
                reopened.count_scan_files(self.scan_id),
                sum(len(group.get("files") or []) for group in self.groups))
        finally:
            reopened.close()


class SqlGroupsPageParityTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.store = self.harness.services.store
        self.scanner = self.harness.services.scanner
        self.scan_id = scanner_module.make_scan_id()
        self.groups = _sample_groups()
        self.store.save_scan_meta(self.scan_id, {
            "scan_id": self.scan_id, "status": "completed",
            "created_epoch": time.time(),
            "config": {"roots": [self.harness.dev_root]},
            "options": {}, "user": "admin", "stats": {},
        })
        self.store.write_scan_groups(self.scan_id, self.groups)

    def tearDown(self):
        self.harness.cleanup()

    def _assert_page_parity(self, offset=0, limit=100, needle="",
                            sort="wasted_desc", match_type=None):
        needle_norm = (needle or "").strip().lower()
        got = self.scanner.groups_page(
            self.scan_id, offset=offset, limit=limit, q=needle, sort=sort,
            match_type=match_type)
        expected = _reference_page(self.groups, offset, limit, needle_norm,
                                   sort, match_type)
        for key in ("total", "offset", "limit", "match_type", "exact_total",
                    "similar_total", "video_total"):
            self.assertEqual(got[key], expected[key],
                             "%s needle=%r sort=%s match=%r"
                             % (key, needle, sort, match_type))
        self.assertEqual(
            [item["group_id"] for item in got["groups"]],
            [item["group_id"] for item in expected["groups"]],
            "order needle=%r sort=%s match=%r" % (needle, sort, match_type))
        return got

    def test_pagination_sort_counts_parity(self):
        for sort in SORT_KEYS:
            for match_type in (None, "exact", "similar", "video"):
                for offset, limit in ((0, 3), (2, 4), (0, 100), (9, 2),
                                      (100, 10)):
                    self._assert_page_parity(offset=offset, limit=limit,
                                             sort=sort,
                                             match_type=match_type)

    def test_search_parity(self):
        needles = ("alpha", "BETA", "ecb01", "video-", "文档", "percent%",
                   "under_", "..", ".txt", "no-such-file")
        for needle in needles:
            for sort in SORT_KEYS:
                self._assert_page_parity(needle=needle, limit=100, sort=sort)
        # LIKE 特殊字符按字面匹配
        got = self._assert_page_parity(needle="%", limit=100)
        self.assertGreaterEqual(got["total"], 1)
        got = self._assert_page_parity(needle="_", limit=100)
        self.assertGreaterEqual(got["total"], 1)

    def test_group_hash_search_parity(self):
        got = self._assert_page_parity(needle="ecb03", limit=100)
        self.assertEqual(got["total"], 1)
        self.assertEqual(got["groups"][0]["group_id"], "g-000004")

    def test_stats_full_parity(self):
        stats = self.scanner.stats_full(self.scan_id)
        expected_by_ext, expected_dirs = _reference_stats(self.groups)
        self.assertEqual(stats["by_extension"], expected_by_ext)
        self.assertEqual(stats["top_dirs"], expected_dirs)

    def test_no_full_group_iteration_on_sql_path(self):
        original = self.store.iter_scan_groups

        def forbidden(_scan_id):
            raise AssertionError("groups_page/stats_full must not iterate groups")
        self.store.iter_scan_groups = forbidden
        try:
            page = self.scanner.groups_page(self.scan_id, limit=5, q="alpha")
            self.assertGreater(page["total"], 0)
            stats = self.scanner.stats_full(self.scan_id)
            self.assertTrue(stats["by_extension"])
        finally:
            self.store.iter_scan_groups = original

    def test_write_append_delete_keep_scan_files_consistent(self):
        scan_id = scanner_module.make_scan_id()
        self.store.save_scan_meta(scan_id, {
            "scan_id": scan_id, "status": "completed",
            "created_epoch": time.time(), "config": {"roots": []},
            "options": {}, "user": "admin", "stats": {}})
        self.store.write_scan_groups(scan_id, self.groups[:3])
        expected = sum(len(group.get("files") or [])
                       for group in self.groups[:3])
        self.assertEqual(self.store.count_scan_files(scan_id), expected)
        self.store.append_scan_groups(scan_id, self.groups[3:5])
        expected += sum(len(group.get("files") or [])
                        for group in self.groups[3:5])
        self.assertEqual(self.store.count_scan_files(scan_id), expected)
        page = self.store.query_scan_groups_page(scan_id, limit=100)
        self.assertEqual(page["total"], 5)
        self.store.delete_scan(scan_id)
        self.assertEqual(self.store.count_scan_files(scan_id), 0)


class LegacyJsonImportV2Test(unittest.TestCase):
    def test_legacy_json_import_populates_scan_files(self):
        tmpdir = tempfile.mkdtemp(prefix="dc-v2-legacy-")
        try:
            scan_id = scanner_module.make_scan_id()
            scan_dir = os.path.join(tmpdir, "scans", scan_id)
            os.makedirs(scan_dir, exist_ok=True)
            meta = {"scan_id": scan_id, "status": "completed",
                    "created_epoch": 1000.0, "stats": {}}
            with open(os.path.join(scan_dir, "meta.json"), "w",
                      encoding="utf-8") as handle:
                json.dump(meta, handle)
            groups = _sample_groups()[:4]
            groups_path = os.path.join(scan_dir, "groups.jsonl")
            with open(groups_path, "w", encoding="utf-8",
                      newline="\n") as handle:
                for group in groups:
                    handle.write(json.dumps(group, ensure_ascii=False) + "\n")
            store = Store(tmpdir)
            try:
                self.assertEqual(store.db.schema_version(),
                                 db_module.CURRENT_SCHEMA_VERSION)
                self.assertEqual(store.count_scan_groups(scan_id), 4)
                self.assertEqual(
                    store.count_scan_files(scan_id),
                    sum(len(group.get("files") or []) for group in groups))
                page = store.query_scan_groups_page(
                    scan_id, limit=10, needle="alpha", sort="wasted_desc")
                expected = _reference_page(groups, 0, 10, "alpha",
                                           "wasted_desc", None)
                self.assertEqual(page["total"], expected["total"])
                # 源 JSON 不删除（旧版本回滚可读）
                self.assertTrue(os.path.isfile(groups_path))
                self.assertTrue(os.path.isfile(
                    os.path.join(scan_dir, "meta.json")))
            finally:
                store.close()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class LegacyJsonFallbackTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.store = self.harness.services.store
        self.scanner = self.harness.services.scanner

    def tearDown(self):
        self.harness.cleanup()

    def test_groups_page_and_stats_fall_back_to_legacy_json(self):
        groups = _sample_groups()[:4]
        scan_id = scanner_module.make_scan_id()
        self.store.save_scan_meta(scan_id, {
            "scan_id": scan_id, "status": "completed",
            "created_epoch": time.time(), "config": {"roots": []},
            "options": {}, "user": "admin", "stats": {},
        })
        scan_dir = self.store.scan_dir(scan_id)
        os.makedirs(scan_dir, exist_ok=True)
        with open(os.path.join(scan_dir, "groups.jsonl"), "w",
                  encoding="utf-8", newline="\n") as handle:
            for group in groups:
                handle.write(json.dumps(group, ensure_ascii=False) + "\n")
        # 模拟 v1 未回填：删掉 DB 分组行，仅保留 meta + 旧 JSON
        with self.store.db.transaction():
            self.store.db.execute(
                "DELETE FROM scan_groups WHERE scan_id=?", (scan_id,))
            self.store.db.execute(
                "DELETE FROM scan_files WHERE scan_id=?", (scan_id,))
        page = self.scanner.groups_page(scan_id, limit=10,
                                        q="alpha", sort="size_desc")
        expected = _reference_page(groups, 0, 10, "alpha", "size_desc", None)
        self.assertEqual(page["total"], expected["total"])
        self.assertEqual([item["group_id"] for item in page["groups"]],
                         [item["group_id"] for item in expected["groups"]])
        stats = self.scanner.stats_full(scan_id)
        expected_by_ext, expected_dirs = _reference_stats(groups)
        self.assertEqual(stats["by_extension"], expected_by_ext)
        self.assertEqual(stats["top_dirs"], expected_dirs)


class LargeScaleSqlPagingTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.store = self.harness.services.store
        self.scanner = self.harness.services.scanner
        self.scan_id = scanner_module.make_scan_id()
        self.store.save_scan_meta(self.scan_id, {
            "scan_id": self.scan_id, "status": "completed",
            "created_epoch": time.time(), "config": {"roots": []},
            "options": {}, "user": "admin", "stats": {},
        })
        groups = []
        for index in range(10000):
            size = 1000 + index
            groups.append({
                "group_id": "g-%06d" % (index + 1),
                "match_type": "exact" if index % 2 == 0 else "similar",
                "wasted_bytes": size * (index % 5 + 1),
                "size": size,
                "similarity_score": 100 if index % 2 == 0 else 80 + index % 19,
                "hash": "h%08d" % index,
                "files": [
                    _file("f-a-%06d" % index,
                          "/share/bench/g%05d/alpha-%05d.txt"
                          % (index, index), size, 1000 + index, True, False),
                    _file("f-b-%06d" % index,
                          "/share/bench/g%05d/beta-%05d.txt"
                          % (index, index), size, 2000 + index, False, True),
                ],
            })
        started = time.time()
        self.store.write_scan_groups(self.scan_id, groups)
        self.write_elapsed = time.time() - started
        self.assertEqual(self.store.count_scan_groups(self.scan_id), 10000)

    def tearDown(self):
        self.harness.cleanup()

    def test_10k_groups_bounded_sql_paging(self):
        self.assertLess(self.write_elapsed, 30.0)
        checks = [
            (dict(offset=0, limit=50), 0.0),
            (dict(offset=5000, limit=50), 0.0),
            (dict(offset=0, limit=100, sort="similarity_desc"), 0.0),
            (dict(offset=0, limit=100, q="alpha-09999"), 0.0),
            (dict(offset=0, limit=100, match_type="similar"), 0.0),
        ]
        for kwargs, _ in checks:
            started = time.time()
            page = self.scanner.groups_page(self.scan_id, **kwargs)
            elapsed = time.time() - started
            self.assertEqual(page["total"] > 0 or kwargs.get("q"), True)
            self.assertLess(elapsed, 5.0,
                            "slow SQL page %r: %.3fs" % (kwargs, elapsed))
        # 计数正确且无需遍历全部 payload
        page = self.scanner.groups_page(self.scan_id, limit=10)
        self.assertEqual(page["total"], 10000)
        self.assertEqual(page["exact_total"], 5000)
        self.assertEqual(page["similar_total"], 5000)
        self.assertEqual(page["video_total"], 0)
        last = self.scanner.groups_page(self.scan_id, offset=9990, limit=50)
        self.assertEqual(len(last["groups"]), 10)
        started = time.time()
        stats = self.scanner.stats_full(self.scan_id)
        stats_elapsed = time.time() - started
        self.assertLess(stats_elapsed, 10.0)
        self.assertTrue(stats["by_extension"])
        self.assertEqual(
            sum(item["count"] for item in stats["by_extension"]), 20000)


if __name__ == "__main__":
    unittest.main()
