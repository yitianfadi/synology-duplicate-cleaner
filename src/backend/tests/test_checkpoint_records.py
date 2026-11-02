# -*- coding: utf-8 -*-
"""v0017 plan B：暂停断点 records 拆分到 scan_checkpoint_records 测试。

覆盖：暂停快照 payload 不再含 records（record_count + 明细行）、
load_checkpoint 兼容 hydrate、resume 从明细表恢复、legacy payload 回退、
v2→v3 迁移、删除清理、轻量 checkpoint 清理、10k/100k 上限与流式批量写。
"""

from __future__ import absolute_import

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from tests.helpers import BACKEND_DIR, Harness, write_bytes  # noqa: F401

from dupcleaner import db as db_module
from dupcleaner import fsutil
from dupcleaner import hashing
from dupcleaner import scanner as scanner_module
from dupcleaner.store import Store

SCAN_PATTERN_ID = "20261102-210000-ab12cd"


def _wait_until(predicate, timeout=20.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _record(path, size=10, mtime_ns=100, inode=7, dev=1, nlink=1,
            quick_hash=None, digest=None):
    return {
        "path": path,
        "size": int(size),
        "mtime_ns": int(mtime_ns),
        "inode": int(inode),
        "dev": int(dev),
        "nlink": int(nlink),
        "quick_hash": quick_hash,
        "hash": digest,
    }


def _sample_records(count):
    return [_record("/share/f-%05d.txt" % index, size=100 + index,
                    mtime_ns=1000 + index, inode=index + 1,
                    quick_hash="q%05d" % index,
                    digest=("h%05d" % index) if index % 2 == 0 else None)
            for index in range(count)]


def _raw_checkpoint(store, scan_id):
    row = store.db.query_one(
        "SELECT payload_json FROM scan_checkpoints WHERE scan_id=?", (scan_id,))
    if row is None:
        return None
    return json.loads(row["payload_json"])


def _make_v2_db(path, scan_id, legacy_payload):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE schema_meta ("
                     "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE scan_checkpoints ("
                     "scan_id TEXT PRIMARY KEY, stage TEXT NOT NULL,"
                     " payload_json TEXT NOT NULL,"
                     " updated_epoch REAL NOT NULL)")
        conn.execute("INSERT INTO schema_meta(key, value)"
                     " VALUES('schema_version', '2')")
        conn.execute("PRAGMA user_version = 2")
        conn.execute(
            "INSERT INTO scan_checkpoints(scan_id, stage, payload_json,"
            " updated_epoch) VALUES(?, ?, ?, ?)",
            (scan_id, "hash",
             json.dumps(legacy_payload, ensure_ascii=False), 1000.0))
        conn.commit()
    finally:
        conn.close()


class SplitSaveTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-cp-")
        self.store = Store(self.tmpdir)
        self.scan_id = scanner_module.make_scan_id()

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_split_payload_and_records(self):
        records = _sample_records(7)
        self.store.save_checkpoint(self.scan_id, "hash", {
            "stage": "hash", "progress": 0.5,
            "stats": {"files_seen": 7}, "records": records,
            "paused_epoch": 123.0,
        })
        raw = _raw_checkpoint(self.store, self.scan_id)
        self.assertNotIn("records", raw)
        self.assertEqual(int(raw["record_count"]), 7)
        self.assertEqual(raw["stage"], "hash")
        self.assertEqual(raw["progress"], 0.5)
        self.assertEqual(int(self.store.checkpoint_record_count(self.scan_id)), 7)
        columns = {row[1] for row in self.store.db.query(
            "PRAGMA table_info(scan_checkpoint_records)")}
        for column in ("scan_id", "ordinal", "path", "size", "mtime_ns",
                       "inode", "dev", "nlink", "quick_hash", "hash"):
            self.assertIn(column, columns)
        indexes = {row[1] for row in self.store.db.query(
            "PRAGMA index_list(scan_checkpoint_records)")}
        self.assertTrue(indexes)
        loaded = self.store.load_checkpoint(self.scan_id)
        # plan B：load_checkpoint 只返回元数据（不 hydrate）
        self.assertNotIn("records", loaded["payload"])
        self.assertEqual(int(loaded["payload"]["record_count"]), 7)
        resumable = self.store.load_resumable_checkpoint(self.scan_id)
        self.assertIsNotNone(resumable)
        self.assertEqual(resumable["records"], records)
        # list_checkpoints 只返回元数据，不 hydrate 大数组
        listed = self.store.list_checkpoints()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("records", listed[0]["payload"])
        self.assertEqual(int(listed[0]["payload"]["record_count"]), 7)

    def test_records_row_fields_preserved(self):
        records = [_record("/share/a.txt", size=11, mtime_ns=22, inode=33,
                           dev=44, nlink=2, quick_hash="qq", digest="hh")]
        self.store.save_pause_checkpoint(
            self.scan_id, "size",
            {"stage": "size", "progress": 0.18, "stats": {}}, iter(records))
        rows = self.store.load_checkpoint_records(self.scan_id)
        self.assertEqual(len(rows), 1)
        for key, value in records[0].items():
            self.assertEqual(rows[0][key], value)

    def test_lightweight_checkpoint_clears_old_records(self):
        self.store.save_checkpoint(self.scan_id, "hash", {
            "stage": "hash", "records": _sample_records(3)})
        self.assertEqual(self.store.checkpoint_record_count(self.scan_id), 3)
        self.store.save_checkpoint(self.scan_id, "finalize",
                                   {"stage": "finalize", "files": 1})
        self.assertEqual(self.store.checkpoint_record_count(self.scan_id), 0)
        raw = _raw_checkpoint(self.store, self.scan_id)
        self.assertNotIn("records", raw)
        self.assertNotIn("record_count", raw)
        self.assertIsNone(self.store.load_resumable_checkpoint(self.scan_id))
        self.assertEqual(self.store.load_checkpoint(self.scan_id)["payload"],
                         {"stage": "finalize", "files": 1})

    def test_delete_checkpoint_cleans_records(self):
        self.store.save_checkpoint(self.scan_id, "hash", {
            "stage": "hash", "records": _sample_records(5)})
        self.assertEqual(self.store.checkpoint_record_count(self.scan_id), 5)
        self.store.delete_checkpoint(self.scan_id)
        self.assertEqual(self.store.checkpoint_record_count(self.scan_id), 0)
        self.assertIsNone(self.store.load_checkpoint(self.scan_id))

    def test_delete_scan_cleans_checkpoint_records(self):
        self.store.save_scan_meta(self.scan_id, {
            "scan_id": self.scan_id, "status": "paused",
            "created_epoch": time.time(), "config": {"roots": []},
            "options": {}, "user": "admin", "stats": {},
        })
        self.store.save_checkpoint(self.scan_id, "hash", {
            "stage": "hash", "records": _sample_records(4)})
        self.store.delete_scan(self.scan_id)
        self.assertEqual(self.store.checkpoint_record_count(self.scan_id), 0)
        self.assertIsNone(self.store.load_checkpoint(self.scan_id))

    def test_record_count_mismatch_is_not_resumable(self):
        self.store.save_checkpoint(self.scan_id, "hash", {
            "stage": "hash", "records": _sample_records(3)})
        self.store.db.execute(
            "DELETE FROM scan_checkpoint_records WHERE scan_id=? AND ordinal=?",
            (self.scan_id, 1))
        loaded = self.store.load_checkpoint(self.scan_id)
        self.assertNotIn("records", loaded["payload"])
        self.assertEqual(int(loaded["payload"]["record_count"]), 3)
        self.assertEqual(
            len(self.store.load_checkpoint_records(self.scan_id)), 2)
        self.assertIsNone(self.store.load_resumable_checkpoint(self.scan_id))

    def test_10k_records_payload_bounded(self):
        records = _sample_records(10000)
        started = time.time()
        self.store.save_checkpoint(self.scan_id, "hash", {
            "stage": "hash", "progress": 0.5, "stats": {}, "records": records})
        elapsed = time.time() - started
        raw_json = self.store.db.query_one(
            "SELECT payload_json FROM scan_checkpoints WHERE scan_id=?",
            (self.scan_id,))["payload_json"]
        self.assertLess(len(raw_json.encode("utf-8")), 4096)
        self.assertNotIn("records", raw_json)
        self.assertEqual(self.store.checkpoint_record_count(self.scan_id),
                         10000)
        self.assertLess(elapsed, 30.0)
        loaded = self.store.load_resumable_checkpoint(self.scan_id)
        self.assertEqual(len(loaded["records"]), 10000)
        self.assertLess(len(json.dumps(loaded["stats"])), 4096)

    def test_100k_records_streaming_batches(self):
        started = time.time()
        count = self.store.save_pause_checkpoint(
            self.scan_id, "hash", {"stage": "hash", "progress": 0.4, "stats": {}},
            (_record("/share/big-%06d.bin" % index, size=index % 4096)
             for index in range(100000)))
        elapsed = time.time() - started
        self.assertEqual(count, 100000)
        self.assertEqual(self.store.checkpoint_record_count(self.scan_id),
                         100000)
        raw_json = self.store.db.query_one(
            "SELECT payload_json FROM scan_checkpoints WHERE scan_id=?",
            (self.scan_id,))["payload_json"]
        self.assertLess(len(raw_json.encode("utf-8")), 4096)
        self.assertLess(elapsed, 60.0)


class LegacyFallbackAndMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-cp-v3-")
        self.scan_id = scanner_module.make_scan_id()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_legacy_payload_records_fallback(self):
        legacy = {"stage": "hash", "progress": 0.5, "stats": {},
                  "records": _sample_records(3), "paused_epoch": 1.0}
        _make_v2_db(os.path.join(self.tmpdir, "dupcleaner.sqlite3"),
                    self.scan_id, legacy)
        store = Store(self.tmpdir)
        try:
            self.assertEqual(store.db.schema_version(),
                             db_module.CURRENT_SCHEMA_VERSION)
            self.assertEqual(store.checkpoint_record_count(self.scan_id), 0)
            loaded = store.load_checkpoint(self.scan_id)
            self.assertEqual(loaded["payload"]["records"],
                             legacy["records"])
            resumable = store.load_resumable_checkpoint(self.scan_id)
            self.assertIsNotNone(resumable)
            self.assertEqual(resumable["records"], legacy["records"])
            # 下一次暂停保存改为拆分行，payload 不再含 records
            store.save_checkpoint(self.scan_id, "hash", {
                "stage": "hash", "progress": 0.5, "stats": {},
                "records": _sample_records(3)})
            raw = _raw_checkpoint(store, self.scan_id)
            self.assertNotIn("records", raw)
            self.assertEqual(int(raw["record_count"]), 3)
            self.assertEqual(store.checkpoint_record_count(self.scan_id), 3)
        finally:
            store.close()

    def test_v2_to_v3_migration_creates_table_only(self):
        legacy_payload = {"stage": "size", "progress": 0.18, "stats": {},
                          "records": _sample_records(2)}
        _make_v2_db(os.path.join(self.tmpdir, "dupcleaner.sqlite3"),
                    self.scan_id, legacy_payload)
        store = Store(self.tmpdir)
        try:
            self.assertEqual(store.db.schema_version(),
                             db_module.CURRENT_SCHEMA_VERSION)
            names = {row[0] for row in store.db.query(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("scan_checkpoint_records", names)
            # migration 不重写历史 payload：legacy records 仍可回退读取
            self.assertEqual(store.checkpoint_record_count(self.scan_id), 0)
            loaded = store.load_checkpoint(self.scan_id)
            self.assertEqual(len(loaded["payload"]["records"]), 2)
            # 旧 payload 原样保留（不被改写）
            raw = _raw_checkpoint(store, self.scan_id)
            self.assertIn("records", raw)
        finally:
            store.close()


class ResumeFromRecordsTableTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.store = self.harness.services.store
        self.scanner = self.harness.services.scanner

    def tearDown(self):
        self.harness.cleanup()

    def _seed(self, stage, records, root):
        scan_id = scanner_module.make_scan_id()
        self.store.save_scan_meta(scan_id, {
            "scan_id": scan_id, "status": "paused", "stage": stage,
            "created_epoch": time.time() - 5, "progress": 0.2,
            "config": {"roots": [root], "excludes": [],
                       "keep_mode": "latest", "hash_algo": "sha256"},
            "options": {"keep_mode": "latest", "hash_algo": "sha256",
                        "concurrency": 1},
            "user": "admin", "stats": {},
        })
        self.store.save_checkpoint(scan_id, stage, {
            "stage": stage, "progress": 0.2, "stats": {},
            "records": records, "paused_epoch": time.time(),
        })
        return scan_id

    def _identity_record(self, path, quick_hash=None, digest=None):
        identity = fsutil.file_identity(path)
        return _record(path, size=identity["size"],
                       mtime_ns=identity["mtime_ns"],
                       inode=identity["inode"], dev=identity["dev"],
                       nlink=identity["nlink"],
                       quick_hash=quick_hash, digest=digest)

    def test_resume_from_records_table_skips_hashing(self):
        root = self.harness.dev_root
        content = b"plan-b-resume-" + b"y" * 8192
        paths = [write_bytes(os.path.join(root, "pb%d.txt" % index), content)
                 for index in range(3)]
        quick = hashing.quick_sample_hash(paths[0])
        digest, _bytes = hashing.full_hash_file(paths[0])
        records = [self._identity_record(path, quick_hash=quick,
                                        digest=digest)
                   for path in paths]
        scan_id = self._seed("hash", records, root)
        # payload 已拆分：resume 必须从明细表读取
        self.assertNotIn("records", _raw_checkpoint(self.store, scan_id))
        self.assertEqual(self.store.checkpoint_record_count(scan_id), 3)
        calls = {"hash": 0}

        def counted_hash(*args, **kwargs):
            calls["hash"] += 1
            return hashing.full_hash_file(*args, **kwargs)

        with mock.patch.object(hashing, "full_hash_file",
                               side_effect=counted_hash):
            self.scanner.resume(scan_id)
            deadline = time.time() + 60
            final = None
            while time.time() < deadline:
                final = self.scanner.get_scan(scan_id)
                if final.get("status") in ("completed", "failed", "stopped"):
                    break
                time.sleep(0.02)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(calls["hash"], 0)
        self.assertEqual(self.store.checkpoint_record_count(scan_id), 0)
        self.assertIsNone(self.store.load_checkpoint(scan_id))

    def test_real_pause_splits_records_and_resumes(self):
        root = self.harness.dev_root
        shared = os.urandom(700 * 1024)
        for index in range(6):
            write_bytes(os.path.join(root, "slow%d.txt" % index), shared)
        scan_id = self.scanner.start_scan(
            [root], [], {"concurrency": 1, "hash_rate_mbps": 1}, "admin")
        deadline = time.time() + 30
        while time.time() < deadline:
            meta = self.scanner.get_scan(scan_id)
            if meta.get("stage") in ("sample", "hash"):
                break
            time.sleep(0.02)
        self.scanner.pause(scan_id)
        self.assertTrue(_wait_until(
            lambda: self.scanner.get_scan(scan_id).get("status") == "paused"
            and self.store.load_resumable_checkpoint(scan_id) is not None,
            30.0), "paused split checkpoint not written")
        raw = _raw_checkpoint(self.store, scan_id)
        self.assertNotIn("records", raw)
        record_count = int(raw["record_count"])
        self.assertGreaterEqual(record_count, 1)
        self.assertEqual(self.store.checkpoint_record_count(scan_id),
                         record_count)
        self.assertLess(len(json.dumps(raw)), 64 * 1024)
        listed = self.store.list_checkpoints()
        self.assertNotIn("records", listed[0]["payload"])
        resumed = self.scanner.resume(scan_id)
        self.assertEqual(resumed["status"], "running")
        deadline = time.time() + 90
        final = None
        while time.time() < deadline:
            final = self.scanner.get_scan(scan_id)
            if final.get("status") in ("completed", "failed", "stopped"):
                break
            time.sleep(0.02)
        self.assertEqual(final["status"], "completed")
        self.assertGreaterEqual(final["stats"]["groups"], 1)


if __name__ == "__main__":
    unittest.main()
