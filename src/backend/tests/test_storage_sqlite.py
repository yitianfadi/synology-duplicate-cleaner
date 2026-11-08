# -*- coding: utf-8 -*-
"""v0012 SQLite 元数据存储测试（docs/STORAGE_CONTRACT.md）。

覆盖：schema/pragma/版本、扫描 meta CRUD、分组按 ordinal 流式迭代、
删除任务/结果、特征缓存（LRU）、checkpoint、旧 JSON 迁移幂等与 DB 优先、
启动 interrupted 标记、并发读写、WAL checkpoint、损坏备份重建、字幕任务元数据。
"""

from __future__ import absolute_import

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from tests.helpers import BACKEND_DIR  # noqa: F401

from dupcleaner import db as db_module
from dupcleaner import similarity
from dupcleaner.store import Store

SCAN_A = "20261101-201600-aaaaaa"
SCAN_B = "20261101-201700-bbbbbb"
SCAN_C = "20261101-201800-cccccc"
SCAN_D = "20261101-201900-dddddd"
SCAN_E = "20261101-202000-eeeeee"
JOB_ID = "del-20261101-201600-abc123"
JOB_B = "del-20261101-201700-def456"
SUB_JOB_ID = "sub-20261101-201600-abcdef"


def _write_json(path, obj):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, ensure_ascii=False)


def _write_lines(path, items):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def _v5_groups():
    """v4→v5 迁移测试数据（含大小写/特殊字符/中文路径）。"""
    return [
        {
            "group_id": "g-000001", "match_type": "exact",
            "wasted_bytes": 20, "size": 10, "similarity_score": 100,
            "hash": "abcdef" * 8,
            "files": [
                {"file_id": "f-1", "path": "/share/Docs/Alpha-01.txt",
                 "name": "Alpha-01.txt", "size": 10, "is_keep": False,
                 "default_selected": True, "created_ts": 1000},
                {"file_id": "f-2", "path": "/share/docs/beta.txt",
                 "name": "beta.txt", "size": 10, "is_keep": True,
                 "default_selected": False, "created_ts": 2000},
            ],
        },
        {
            "group_id": "s-000001", "match_type": "similar",
            "wasted_bytes": 5, "size": 12, "similarity_score": 88,
            "hash": "",
            "files": [
                {"file_id": "sf-1", "path": "/share/中文/文档-a.txt",
                 "name": "文档-a.txt", "size": 12, "is_keep": False,
                 "default_selected": False, "created_ts": 3000},
            ],
        },
    ]


def _make_v4_db(path, scan_id, groups):
    """构造 v4 形态数据库：scan_files 含 payload_json、schema_version=4。"""
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
                     " wasted_bytes INTEGER NOT NULL DEFAULT 0,"
                     " size INTEGER NOT NULL DEFAULT 0,"
                     " similarity_score INTEGER NOT NULL DEFAULT 0,"
                     " created_epoch REAL NOT NULL DEFAULT 0,"
                     " group_hash TEXT,"
                     " PRIMARY KEY (scan_id, ordinal))")
        conn.execute("CREATE INDEX idx_scan_groups_type"
                     " ON scan_groups(scan_id, match_type)")
        conn.execute("CREATE TABLE scan_files ("
                     "scan_id TEXT NOT NULL, group_id TEXT NOT NULL,"
                     " file_id TEXT NOT NULL, path TEXT NOT NULL,"
                     " size INTEGER NOT NULL DEFAULT 0,"
                     " is_keep INTEGER NOT NULL DEFAULT 0,"
                     " default_selected INTEGER NOT NULL DEFAULT 0,"
                     " payload_json TEXT NOT NULL,"
                     " PRIMARY KEY (scan_id, group_id, file_id))")
        conn.execute("CREATE INDEX idx_scan_files_path"
                     " ON scan_files(scan_id, path)")
        conn.execute("CREATE INDEX idx_scan_files_group"
                     " ON scan_files(scan_id, group_id)")
        conn.execute("INSERT INTO schema_meta(key, value)"
                     " VALUES('schema_version', '4')")
        conn.execute("PRAGMA user_version = 4")
        meta = {"scan_id": scan_id, "status": "completed",
                "created_epoch": 1000.0, "stats": {}}
        conn.execute(
            "INSERT INTO scans(scan_id, meta_json, created_epoch,"
            " updated_epoch, status, interrupted) VALUES(?, ?, ?, ?, ?, 0)",
            (scan_id, json.dumps(meta), 1000.0, 1000.0, "completed"))
        for ordinal, group in enumerate(groups):
            row = db_module.group_index_columns(group, ordinal)
            conn.execute(
                "INSERT INTO scan_groups(scan_id, ordinal, group_id,"
                " match_type, payload_json, wasted_bytes, size,"
                " similarity_score, created_epoch, group_hash)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (scan_id, ordinal, row["group_id"], row["match_type"],
                 json.dumps(group, ensure_ascii=False),
                 row["wasted_bytes"], row["size"],
                 row["similarity_score"], row["created_epoch"],
                 row["group_hash"]))
            for index, file_info in enumerate(group.get("files") or []):
                conn.execute(
                    "INSERT INTO scan_files(scan_id, group_id, file_id, path,"
                    " size, is_keep, default_selected, payload_json)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (scan_id, row["group_id"], file_info.get("file_id"),
                     file_info.get("path"), int(file_info.get("size") or 0),
                     1 if file_info.get("is_keep") else 0,
                     1 if file_info.get("default_selected") else 0,
                     json.dumps(file_info, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-sqlite-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def new_store(self):
        store = Store(self.tmpdir)
        self.addCleanup(store.close)
        return store


class SchemaTest(StoreTestCase):
    def test_schema_pragmas_and_version(self):
        store = self.new_store()
        self.assertTrue(os.path.isfile(store.db_path))
        self.assertEqual(store.db.schema_version(), db_module.CURRENT_SCHEMA_VERSION)
        self.assertEqual(store.db.schema_meta_get("schema_version"),
                         str(db_module.CURRENT_SCHEMA_VERSION))
        self.assertEqual(store.db.schema_meta_get("migrated_v1"), "1")
        self.assertEqual(
            str(store.db.scalar("PRAGMA journal_mode")).lower(), "wal")
        self.assertEqual(int(store.db.scalar("PRAGMA synchronous")), 1)
        self.assertEqual(int(store.db.scalar("PRAGMA foreign_keys")), 1)
        self.assertEqual(int(store.db.scalar("PRAGMA busy_timeout")), 5000)
        names = {row[0] for row in store.db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("schema_meta", "scans", "scan_groups", "deletion_jobs",
                      "deletion_results", "feature_cache", "scan_checkpoints",
                      "subtitle_chunks", "subtitle_jobs", "subtitle_batches",
                      "subtitle_batch_groups", "scan_file_deletions",
                      "hash_cache", "media_probe_cache"):
            self.assertIn(table, names)
        # v5：scan_files 不再冗余保存 payload_json（group payload 才是真源）
        # v6（v0028）：新增 subtitle_batches 批次表
        # v7（v0032）：新增 subtitle_batch_groups + scan_file_deletions
        # v8（v0043）：新增 hash_cache + media_probe_cache，scan_groups 补增量列
        self.assertEqual(db_module.CURRENT_SCHEMA_VERSION, 8)
        scan_group_columns = {row[1] for row in store.db.query(
            "PRAGMA table_info(scan_groups)")}
        for column in ("new_files", "changed_files"):
            self.assertIn(column, scan_group_columns)
        scan_files_columns = {row[1] for row in store.db.query(
            "PRAGMA table_info(scan_files)")}
        self.assertNotIn("payload_json", scan_files_columns)
        for column in ("scan_id", "group_id", "file_id", "path", "size",
                       "is_keep", "default_selected"):
            self.assertIn(column, scan_files_columns)
        # FTS5 表/触发器与探测结果保持一致（不支持时绝不残留触发写失败的对象）
        fts_table = "scan_files_fts" in names
        self.assertEqual(fts_table, bool(store.db.fts_enabled))
        triggers = {row[0] for row in store.db.query(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
        if store.db.fts_enabled:
            for trigger in db_module.FTS_TRIGGER_NAMES:
                self.assertIn(trigger, triggers)
        else:
            self.assertFalse(
                set(db_module.FTS_TRIGGER_NAMES) & triggers)

    def test_scan_file_deletions_filter_reads(self):
        """v0032：删除成功后，分组/统计/迭代读取都不再返回已删除文件。"""
        groups = _v5_groups()
        _make_v4_db(os.path.join(self.tmpdir, "dupcleaner.sqlite3"),
                    SCAN_A, groups)
        store = Store(self.tmpdir)
        try:
            self.assertEqual(store.count_scan_files(SCAN_A), 3)
            delete_path = "/share/Docs/Alpha-01.txt"
            self.assertEqual(store.mark_scan_files_deleted(
                SCAN_A, [{"path": delete_path, "job_id": "del-test-1",
                          "mode": "recycled"}]), 1)
            self.assertEqual(store.count_scan_file_deletions(SCAN_A), 1)
            self.assertEqual(store.load_deleted_paths(SCAN_A), {delete_path})
            self.assertEqual(store.count_scan_files(SCAN_A), 2)
            # 组内只剩 1 个文件 → 该组从分组结果中消失
            self.assertEqual(store.count_scan_groups(SCAN_A), 0)
            page = store.query_scan_groups_page(SCAN_A, limit=10)
            self.assertIsNotNone(page)
            self.assertEqual(page["groups"], [])
            self.assertEqual(page["total"], 0)
            self.assertEqual(list(store.iter_scan_groups(SCAN_A)), [])
            stats_paths = [path for path, _size in store.scan_file_stats(SCAN_A)]
            self.assertNotIn(delete_path, stats_paths)
            self.assertEqual(stats_paths, [])
            # 幂等：重复标记不报错、行数不变
            store.mark_scan_files_deleted(SCAN_A, [delete_path])
            self.assertEqual(store.count_scan_file_deletions(SCAN_A), 1)
        finally:
            store.close()

    def test_v4_to_v5_migration_drops_payload_and_preserves_rows(self):
        groups = _v5_groups()
        _make_v4_db(os.path.join(self.tmpdir, "dupcleaner.sqlite3"),
                    SCAN_A, groups)
        store = Store(self.tmpdir)
        try:
            self.assertEqual(store.db.schema_version(),
                             db_module.CURRENT_SCHEMA_VERSION)
            columns = {row[1] for row in store.db.query(
                "PRAGMA table_info(scan_files)")}
            self.assertNotIn("payload_json", columns)
            expected_files = sum(len(group.get("files") or [])
                                 for group in groups)
            self.assertEqual(store.count_scan_files(SCAN_A), expected_files)
            self.assertEqual(store.count_scan_groups(SCAN_A), len(groups))
            # 行数据（路径/组/大小/保留/勾选）不变
            rows = {row["path"]: row for row in store.db.query(
                "SELECT group_id, file_id, path, size, is_keep,"
                " default_selected FROM scan_files WHERE scan_id=?", (SCAN_A,))}
            self.assertIn("/share/Docs/Alpha-01.txt", rows)
            alpha = rows["/share/Docs/Alpha-01.txt"]
            self.assertEqual(alpha["group_id"], "g-000001")
            self.assertEqual(int(alpha["size"]), 10)
            self.assertEqual(int(alpha["is_keep"]), 0)
            self.assertEqual(int(alpha["default_selected"]), 1)
            self.assertIn("/share/中文/文档-a.txt", rows)
            # scan_groups payload 真源不变
            stored_groups = list(store.iter_scan_groups(SCAN_A))
            self.assertEqual([item["group_id"] for item in stored_groups],
                             [group["group_id"] for group in groups])
            page = store.query_scan_groups_page(
                SCAN_A, limit=10, needle="alpha", use_fts=True)
            self.assertEqual(page["total"], 1)
            if store.db.fts_enabled:
                names = {row[0] for row in store.db.query(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("scan_files_fts", names)
                # rebuild 一致性：scan_files 行数 == FTS 行数
                scan_rows = int(store.db.scalar(
                    "SELECT COUNT(*) FROM scan_files", (), 0) or 0)
                fts_rows = int(store.db.scalar(
                    "SELECT COUNT(*) FROM scan_files_fts", (), 0) or 0)
                self.assertEqual(scan_rows, fts_rows)
                matched = int(store.db.scalar(
                    "SELECT COUNT(*) FROM scan_files_fts"
                    " WHERE scan_files_fts MATCH ?",
                    (db_module.fts_path_phrase("alpha"),), 0) or 0)
                self.assertGreaterEqual(matched, 1)
        finally:
            store.close()
        # 重开幂等：不重复建表/不丢行
        reopened = Store(self.tmpdir)
        try:
            self.assertEqual(reopened.count_scan_files(SCAN_A), expected_files)
            self.assertEqual(reopened.db.schema_version(),
                             db_module.CURRENT_SCHEMA_VERSION)
        finally:
            reopened.close()

    def test_schema_version_upgrade_from_zero(self):
        store = self.new_store()
        store.db.schema_meta_set("schema_version", "0")
        store.close()
        reopened = self.new_store()
        self.assertEqual(reopened.db.schema_version(),
                         db_module.CURRENT_SCHEMA_VERSION)

    def test_close_checkpoints_wal_and_reopen(self):
        store = Store(self.tmpdir)
        store.save_scan_meta(SCAN_A, {"scan_id": SCAN_A, "status": "completed",
                                      "created_epoch": 1000.0})
        store.close()
        store.close()  # 幂等
        wal = store.db_path + "-wal"
        if os.path.exists(wal):
            self.assertEqual(os.path.getsize(wal), 0)
        reopened = self.new_store()
        self.assertEqual(reopened.load_scan_meta(SCAN_A)["status"], "completed")


class FtsFallbackOpenTest(StoreTestCase):
    def test_open_without_fts_cleans_derived_objects_and_writes_work(self):
        store = Store(self.tmpdir)
        if not store.db.fts_enabled:
            store.close()
            self.skipTest("本机 SQLite 不支持 FTS5 trigram")
        try:
            store.save_scan_meta(SCAN_A, {
                "scan_id": SCAN_A, "status": "completed",
                "created_epoch": 1000.0})
            groups = _v5_groups()
            store.write_scan_groups(SCAN_A, groups)
            expected_files = sum(len(group.get("files") or [])
                                 for group in groups)
        finally:
            store.close()

        original_probe = db_module.Database._probe_fts_trigram
        db_module.Database._probe_fts_trigram = lambda self: False
        reopened = None
        try:
            with self.assertLogs("dupcleaner.db", level="WARNING"):
                reopened = Store(self.tmpdir)
            self.assertFalse(reopened.db.fts_enabled)
            names = {row[0] for row in reopened.db.query(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("scan_files_fts", names)
            triggers = {row[0] for row in reopened.db.query(
                "SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.assertFalse(set(db_module.FTS_TRIGGER_NAMES) & triggers)
            # 主数据不丢
            self.assertEqual(reopened.count_scan_files(SCAN_A),
                             expected_files)
            # 派生对象清理后，写 scan_files 不再因缺失 FTS 表失败
            reopened.save_scan_meta(SCAN_B, {
                "scan_id": SCAN_B, "status": "completed",
                "created_epoch": 2000.0})
            reopened.write_scan_groups(SCAN_B, _v5_groups())
            page = reopened.query_scan_groups_page(
                SCAN_B, needle="alpha", use_fts=True)
            self.assertEqual(page["total"], 1)
        finally:
            db_module.Database._probe_fts_trigram = original_probe
            if reopened is not None:
                reopened.close()


class FtsRebuildTest(StoreTestCase):
    def test_missing_trigger_rebuild_indexes_existing_rows(self):
        store = Store(self.tmpdir)
        if not store.db.fts_enabled:
            store.close()
            self.skipTest("本机 SQLite 不支持 FTS5 trigram")
        try:
            store.save_scan_meta(SCAN_A, {
                "scan_id": SCAN_A, "status": "completed",
                "created_epoch": 1000.0})
            store.write_scan_groups(SCAN_A, _v5_groups())
            # 模拟索引/触发器损坏：删掉 INSERT 触发器后直接写一行（FTS 不会收到），
            # 关闭后重新打开应检测到触发器缺失并 rebuild。
            store.db.execute("DROP TRIGGER IF EXISTS scan_files_fts_ai")
            store.db.execute(
                "INSERT OR REPLACE INTO scan_files"
                "(scan_id, group_id, file_id, path, size, is_keep,"
                " default_selected) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (SCAN_A, "g-000001", "f-stale", "/share/stale/rebuild-token.txt",
                 10, 0, 1))
        finally:
            store.close()

        reopened = Store(self.tmpdir)
        try:
            self.assertTrue(reopened.db.fts_enabled)
            triggers = {row[0] for row in reopened.db.query(
                "SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.assertIn("scan_files_fts_ai", triggers)
            matched = int(reopened.db.scalar(
                "SELECT COUNT(*) FROM scan_files_fts"
                " WHERE scan_files_fts MATCH ?",
                (db_module.fts_path_phrase("rebuild-token"),), 0) or 0)
            self.assertGreaterEqual(matched, 1)
            page = reopened.query_scan_groups_page(
                SCAN_A, needle="rebuild-token", use_fts=True)
            self.assertEqual(page["total"], 1)
            scan_rows = int(reopened.db.scalar(
                "SELECT COUNT(*) FROM scan_files", (), 0) or 0)
            fts_rows = int(reopened.db.scalar(
                "SELECT COUNT(*) FROM scan_files_fts", (), 0) or 0)
            self.assertEqual(scan_rows, fts_rows)
        finally:
            reopened.close()


class ScanStoreTest(StoreTestCase):
    def test_scan_meta_crud_db_backed(self):
        store = self.new_store()
        meta = {"scan_id": SCAN_A, "status": "running", "created_epoch": 1000.0,
                "stats": {"groups": 2}}
        store.save_scan_meta(SCAN_A, meta)
        # 新写入只进 SQLite，不再生成旧 meta.json。
        self.assertFalse(os.path.exists(
            os.path.join(store.scans_dir, SCAN_A, "meta.json")))
        loaded = store.load_scan_meta(SCAN_A)
        self.assertEqual(loaded["status"], "running")
        self.assertEqual(loaded["stats"], {"groups": 2})
        self.assertIsNone(store.load_scan_meta(SCAN_B))

        meta["status"] = "completed"
        store.save_scan_meta(SCAN_A, meta)
        self.assertEqual(store.load_scan_meta(SCAN_A)["status"], "completed")

        store.save_scan_meta(SCAN_B, {"scan_id": SCAN_B, "status": "completed",
                                      "created_epoch": 2000.0})
        listed = store.list_scan_metas()
        self.assertEqual([item["scan_id"] for item in listed], [SCAN_B, SCAN_A])
        self.assertEqual(len(store.list_scan_metas(limit=1)), 1)

        store.delete_scan(SCAN_A)
        self.assertIsNone(store.load_scan_meta(SCAN_A))
        self.assertEqual(list(store.iter_scan_groups(SCAN_A)), [])
        self.assertEqual([item["scan_id"] for item in store.list_scan_metas()],
                         [SCAN_B])

    def test_groups_iterate_in_ordinal_order_and_append(self):
        store = self.new_store()
        groups = [{"group_id": "g-%06d" % (index + 1),
                   "match_type": "exact", "size": index,
                   "files": [{"path": "/p/%d" % index}]}
                  for index in range(450)]
        count = store.write_scan_groups(SCAN_A, groups)
        self.assertEqual(count, 450)
        self.assertEqual(store.count_scan_groups(SCAN_A), 450)
        loaded = list(store.iter_scan_groups(SCAN_A))
        self.assertEqual(len(loaded), 450)
        self.assertEqual([item["group_id"] for item in loaded],
                         [item["group_id"] for item in groups])
        row = store.db.query_one(
            "SELECT group_id, match_type FROM scan_groups"
            " WHERE scan_id=? AND ordinal=? ORDER BY ordinal", (SCAN_A, 0))
        self.assertEqual(row["group_id"], "g-000001")
        self.assertEqual(row["match_type"], "exact")

        # 追加写：ordinal 从当前最大值继续。
        extra = [{"group_id": "s-000451", "match_type": "similar"},
                 {"group_id": "v-000452", "match_type": "video"}]
        self.assertEqual(store.append_scan_groups(SCAN_A, extra), 2)
        loaded = list(store.iter_scan_groups(SCAN_A))
        self.assertEqual(len(loaded), 452)
        self.assertEqual(loaded[-1]["group_id"], "v-000452")
        ordinals = [row[0] for row in store.db.query(
            "SELECT ordinal FROM scan_groups WHERE scan_id=? ORDER BY ordinal",
            (SCAN_A,))]
        self.assertEqual(ordinals, list(range(452)))

        # 覆盖写替换整组。
        self.assertEqual(store.write_scan_groups(SCAN_A, groups[:2]), 2)
        self.assertEqual(store.count_scan_groups(SCAN_A), 2)

    def test_groups_iter_is_paged(self):
        store = self.new_store()
        groups = [{"group_id": "g-%06d" % (index + 1), "match_type": "exact"}
                  for index in range(450)]
        store.write_scan_groups(SCAN_A, groups)
        sizes = []
        original = store.db.query

        def counting(sql, params=()):
            rows = original(sql, params)
            sizes.append(len(rows))
            return rows

        store.db.query = counting
        iterator = store.iter_scan_groups(SCAN_A)
        first = [next(iterator) for _ in range(5)]
        self.assertEqual([item["group_id"] for item in first],
                         ["g-000001", "g-000002", "g-000003", "g-000004",
                          "g-000005"])
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), 200)
        iterator.close()

    def test_jobs_meta_and_results_crud(self):
        store = self.new_store()
        store.save_job_meta(JOB_ID, {"job_id": JOB_ID, "scan_id": SCAN_A,
                                     "started_epoch": 1000.0,
                                     "status": "running"})
        self.assertEqual(store.load_job_meta(JOB_ID)["job_id"], JOB_ID)
        for index in range(5):
            store.append_job_result(JOB_ID, {"path": "/p/%d" % index,
                                             "status": "deleted"})
        results = list(store.iter_job_results(JOB_ID))
        self.assertEqual([item["path"] for item in results],
                         ["/p/%d" % index for index in range(5)])
        self.assertEqual(len(list(store.iter_job_results(JOB_ID, limit=2))), 2)
        self.assertEqual(len(list(store.iter_job_results(JOB_ID, limit=0))), 0)

        store.save_job_meta(JOB_B, {"job_id": JOB_B, "scan_id": SCAN_B,
                                    "started_epoch": 2000.0})
        self.assertEqual(len(store.list_job_metas()), 2)
        by_scan = store.list_job_metas(scan_id=SCAN_A)
        self.assertEqual([item["job_id"] for item in by_scan], [JOB_ID])

        store.delete_job(JOB_ID)
        self.assertIsNone(store.load_job_meta(JOB_ID))
        self.assertEqual(list(store.iter_job_results(JOB_ID)), [])
        self.assertEqual([item["job_id"] for item in store.list_job_metas()],
                         [JOB_B])

    def test_feature_cache_sqlite_backend_and_lru(self):
        store = self.new_store()
        cache = similarity.FeatureCache(store, max_entries=2)
        cache.put("k1", {"feature": {"signature": [1]}, "kind": "text"})
        cache.put("k2", {"feature": {"signature": [2]}, "kind": "text"})
        self.assertTrue(cache.save())
        self.assertGreaterEqual(store.feature_cache_len(), 2)
        self.assertEqual(store.feature_cache_get("k1")["feature"]["signature"],
                         [1])
        row = store.db.query_one(
            "SELECT kind FROM feature_cache WHERE cache_key=?", ("k1",))
        self.assertEqual(row["kind"], "text")

        reloaded = similarity.FeatureCache(store, max_entries=2)
        self.assertIsNotNone(reloaded.get("k1"))
        reloaded.put("k3", {"feature": {"signature": [3]}, "kind": "text"})
        reloaded.put("k4", {"feature": {"signature": [4]}, "kind": "text"})
        self.assertTrue(reloaded.save())
        self.assertLessEqual(store.feature_cache_len(), 2)
        self.assertIsNone(reloaded.get("k2"))
        self.assertIsNotNone(reloaded.get("k1"))
        stored = {row[0] for row in store.db.query(
            "SELECT cache_key FROM feature_cache")}
        self.assertEqual(stored, {"k1", "k4"})

    def test_feature_cache_json_backward_compatible(self):
        path = os.path.join(self.tmpdir, "feature_cache.json")
        cache = similarity.FeatureCache(path, max_entries=10)
        cache.put("legacy", {"feature": {"signature": [9]}})
        self.assertTrue(cache.save())
        reloaded = similarity.FeatureCache(path, max_entries=10)
        self.assertEqual(reloaded.get("legacy")["feature"]["signature"], [9])

    def test_checkpoints_crud(self):
        store = self.new_store()
        self.assertIsNone(store.load_checkpoint(SCAN_A))
        store.save_checkpoint(SCAN_A, "hash", {"files": 10, "progress": 0.5})
        checkpoint = store.load_checkpoint(SCAN_A)
        self.assertEqual(checkpoint["stage"], "hash")
        self.assertEqual(checkpoint["payload"], {"files": 10, "progress": 0.5})
        store.save_checkpoint(SCAN_A, "finalize", {"files": 20})
        self.assertEqual(store.load_checkpoint(SCAN_A)["stage"], "finalize")
        self.assertEqual(len(store.list_checkpoints()), 1)
        store.delete_checkpoint(SCAN_A)
        self.assertIsNone(store.load_checkpoint(SCAN_A))

    def test_subtitle_jobs_metadata_crud(self):
        store = self.new_store()
        record = {"job_id": SUB_JOB_ID, "status": "done", "path": "/v/a.mp4",
                  "updated_epoch": 1234.0, "files": ["original.srt"]}
        self.assertTrue(store.save_subtitle_job(SUB_JOB_ID, record))
        self.assertEqual(store.load_subtitle_job(SUB_JOB_ID)["status"], "done")
        self.assertEqual(len(store.load_subtitle_jobs()), 1)
        store.delete_subtitle_job(SUB_JOB_ID)
        self.assertIsNone(store.load_subtitle_job(SUB_JOB_ID))


class MigrationTest(StoreTestCase):
    def _write_legacy(self):
        scans_dir = os.path.join(self.tmpdir, "scans", SCAN_A)
        _write_json(os.path.join(scans_dir, "meta.json"),
                    {"scan_id": SCAN_A, "status": "completed",
                     "created_epoch": 1000.0, "stats": {"groups": 2}})
        _write_lines(os.path.join(scans_dir, "groups.jsonl"), [
            {"group_id": "g-000001", "match_type": "exact", "size": 7},
            {"group_id": "g-000002", "match_type": "exact", "size": 8},
        ])
        deletions_dir = os.path.join(self.tmpdir, "deletions")
        _write_json(os.path.join(deletions_dir, JOB_ID + ".meta.json"),
                    {"job_id": JOB_ID, "scan_id": SCAN_A,
                     "started_epoch": 1100.0, "status": "completed"})
        _write_lines(os.path.join(deletions_dir, JOB_ID + ".jsonl"), [
            {"path": "/p/a", "status": "deleted"},
            {"path": "/p/b", "status": "recycled"},
        ])
        _write_json(os.path.join(self.tmpdir, "feature_cache.json"), {
            "version": 1,
            "entries": {
                "v1|1|2|3|4|text": {"kind": "text",
                                    "feature": {"signature": [1, 2]}},
            },
        })

    def test_legacy_import_idempotent_db_priority_no_delete(self):
        self._write_legacy()
        legacy_paths = [
            os.path.join(self.tmpdir, "scans", SCAN_A, "meta.json"),
            os.path.join(self.tmpdir, "scans", SCAN_A, "groups.jsonl"),
            os.path.join(self.tmpdir, "deletions", JOB_ID + ".meta.json"),
            os.path.join(self.tmpdir, "deletions", JOB_ID + ".jsonl"),
            os.path.join(self.tmpdir, "feature_cache.json"),
        ]
        store = Store(self.tmpdir)
        try:
            self.assertEqual(store.load_scan_meta(SCAN_A)["status"], "completed")
            groups = list(store.iter_scan_groups(SCAN_A))
            self.assertEqual([item["group_id"] for item in groups],
                             ["g-000001", "g-000002"])
            job = store.load_job_meta(JOB_ID)
            self.assertEqual(job["status"], "completed")
            self.assertEqual([item["path"] for item in
                              store.iter_job_results(JOB_ID)],
                             ["/p/a", "/p/b"])
            self.assertEqual(store.feature_cache_len(), 1)
            self.assertEqual(
                store.feature_cache_get("v1|1|2|3|4|text")["feature"]
                ["signature"], [1, 2])
            self.assertEqual(store.db.schema_meta_get("migrated_v1"), "1")
            first_counts = (len(store.list_scan_metas()),
                            store.count_scan_groups(SCAN_A),
                            len(store.list_job_metas()),
                            store.feature_cache_len())
            self.assertEqual(first_counts, (1, 2, 1, 1))
        finally:
            store.close()

        # 源文件不删除。
        for path in legacy_paths:
            self.assertTrue(os.path.isfile(path), path)

        # DB 优先：随后改动旧 JSON 不影响读取。
        _write_json(os.path.join(self.tmpdir, "scans", SCAN_A, "meta.json"),
                    {"scan_id": SCAN_A, "status": "failed",
                     "created_epoch": 9999.0})
        reopened = Store(self.tmpdir)
        try:
            self.assertEqual(reopened.load_scan_meta(SCAN_A)["status"],
                             "completed")
            self.assertEqual(
                (len(reopened.list_scan_metas()),
                 reopened.count_scan_groups(SCAN_A),
                 len(reopened.list_job_metas()),
                 reopened.feature_cache_len()),
                (1, 2, 1, 1))
        finally:
            reopened.close()

    def test_legacy_import_picks_up_files_written_after_flag(self):
        # 旧版本回滚可能重新写 JSON；再次升级时增量导入，不丢数据。
        store = Store(self.tmpdir)
        store.save_scan_meta(SCAN_A, {"scan_id": SCAN_A,
                                      "status": "completed",
                                      "created_epoch": 1000.0})
        store.close()
        scan_dir = os.path.join(self.tmpdir, "scans", SCAN_B)
        _write_json(os.path.join(scan_dir, "meta.json"),
                    {"scan_id": SCAN_B, "status": "completed",
                     "created_epoch": 2000.0})
        _write_lines(os.path.join(scan_dir, "groups.jsonl"), [
            {"group_id": "g-000001", "match_type": "exact"},
            {"group_id": "g-000002", "match_type": "exact"},
        ])
        reopened = Store(self.tmpdir)
        try:
            self.assertEqual(reopened.load_scan_meta(SCAN_B)["status"],
                             "completed")
            self.assertEqual(reopened.count_scan_groups(SCAN_B), 2)
            self.assertIsNotNone(reopened.load_scan_meta(SCAN_A))
        finally:
            reopened.close()

    def test_legacy_feature_cache_reimport_when_file_changes(self):
        _write_json(os.path.join(self.tmpdir, "feature_cache.json"), {
            "version": 1,
            "entries": {"k1": {"kind": "text", "feature": {"signature": [1]}}},
        })
        store = Store(self.tmpdir)
        self.assertEqual(store.feature_cache_len(), 1)
        store.close()
        path = os.path.join(self.tmpdir, "feature_cache.json")
        _write_json(path, {
            "version": 1,
            "entries": {
                "k1": {"kind": "text", "feature": {"signature": [1]}},
                "k2": {"kind": "text", "feature": {"signature": [2]}},
            },
        })
        os.utime(path, (time.time() + 5, time.time() + 5))
        reopened = Store(self.tmpdir)
        try:
            self.assertEqual(reopened.feature_cache_len(), 2)
            self.assertIsNotNone(reopened.feature_cache_get("k2"))
        finally:
            reopened.close()

    def test_startup_marks_interrupted_scans(self):
        store = Store(self.tmpdir)
        statuses = [(SCAN_A, "running"), (SCAN_B, "paused"),
                    (SCAN_C, "queued"), (SCAN_D, "stopping"),
                    (SCAN_E, "completed")]
        for scan_id, status in statuses:
            store.save_scan_meta(scan_id, {
                "scan_id": scan_id, "status": status, "stage": "hash",
                "created_epoch": 1000.0, "progress": 0.3,
            })
        store.close()

        reopened = Store(self.tmpdir)
        try:
            for scan_id, previous in statuses[:4]:
                meta = reopened.load_scan_meta(scan_id)
                self.assertEqual(meta["status"], "interrupted")
                self.assertTrue(meta["interrupted"])
                self.assertEqual(meta["interrupted_previous_status"], previous)
                self.assertIsNotNone(meta["finished_epoch"])
                checkpoint = reopened.load_checkpoint(scan_id)
                self.assertIsNotNone(checkpoint)
                self.assertEqual(checkpoint["payload"]["status"], "interrupted")
                self.assertEqual(checkpoint["payload"]["previous_status"],
                                 previous)
            completed = reopened.load_scan_meta(SCAN_E)
            self.assertEqual(completed["status"], "completed")
            self.assertFalse(completed.get("interrupted"))
            self.assertIsNone(reopened.load_checkpoint(SCAN_E))
        finally:
            reopened.close()


class RecoveryAndConcurrencyTest(StoreTestCase):
    def test_corrupt_db_backed_up_and_recreated(self):
        _write_json(os.path.join(self.tmpdir, "feature_cache.json"), {
            "version": 1,
            "entries": {"k-legacy": {"kind": "text",
                                     "feature": {"signature": [7]}}},
        })
        store = Store(self.tmpdir)
        store.save_scan_meta(SCAN_A, {"scan_id": SCAN_A,
                                      "status": "completed",
                                      "created_epoch": 1000.0})
        store.close()
        with open(store.db_path, "wb") as handle:
            handle.write(b"this is definitely not a sqlite database" * 64)
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(store.db_path + suffix)
            except OSError:
                pass

        recovered = Store(self.tmpdir)
        try:
            self.assertIsNotNone(recovered.db.recovered_from)
            self.assertTrue(os.path.isfile(recovered.db.recovered_from))
            self.assertIn(".corrupt-", recovered.db.recovered_from)
            self.assertEqual(recovered.db.schema_version(),
                             db_module.CURRENT_SCHEMA_VERSION)
            # 新库为空，但旧 JSON 会被重新导入（migrated 标记随旧库丢失）。
            self.assertEqual(recovered.list_scan_metas(), [])
            self.assertEqual(recovered.feature_cache_len(), 1)
            self.assertIsNotNone(recovered.feature_cache_get("k-legacy"))
        finally:
            recovered.close()

    def test_concurrent_writes_and_reads(self):
        store = self.new_store()
        store.save_job_meta(JOB_ID, {"job_id": JOB_ID, "scan_id": SCAN_A,
                                     "started_epoch": 1000.0})
        total_writers = 6
        per_writer = 20
        errors = []
        observed = []
        stop = threading.Event()

        def writer(worker_id):
            try:
                for index in range(per_writer):
                    store.append_job_result(JOB_ID, {
                        "path": "/t/%d-%d" % (worker_id, index),
                        "status": "deleted"})
                    store.save_scan_meta(SCAN_A, {
                        "scan_id": SCAN_A, "status": "running",
                        "created_epoch": 1000.0,
                        "stats": {"groups": worker_id * per_writer + index}})
            except Exception as exc:  # pragma: no cover - 失败断言用
                errors.append(exc)

        def reader():
            try:
                while not stop.is_set():
                    observed.append(len(list(store.iter_job_results(JOB_ID))))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        threads = [threading.Thread(target=writer, args=(index,))
                   for index in range(total_writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        stop.set()
        reader_thread.join(timeout=60)

        self.assertEqual(errors, [])
        results = list(store.iter_job_results(JOB_ID))
        self.assertEqual(len(results), total_writers * per_writer)
        self.assertEqual(len({item["path"] for item in results}),
                         total_writers * per_writer)
        self.assertTrue(observed)
        self.assertEqual(store.load_scan_meta(SCAN_A)["status"], "running")


if __name__ == "__main__":
    unittest.main()
