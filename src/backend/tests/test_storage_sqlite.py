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
                      "subtitle_jobs"):
            self.assertIn(table, names)

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
