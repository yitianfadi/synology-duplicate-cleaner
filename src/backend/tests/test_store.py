# -*- coding: utf-8 -*-
import json
import os
import shutil
import tempfile
import unittest

from tests.helpers import BACKEND_DIR  # noqa: F401

from dupcleaner.errors import ApiError
from dupcleaner.store import Store

SCAN_ID = "20261101-201600-ab12cd"
JOB_ID = "del-20261101-201600-abc123"


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-store-")
        self.store = Store(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_json_atomic_roundtrip(self):
        path = os.path.join(self.tmpdir, "nested", "data.json")
        payload = {"中文": "值", "list": [1, 2, 3]}
        self.store.write_json(path, payload)
        self.assertEqual(self.store.read_json(path), payload)
        self.assertEqual(self.store.read_json(path + ".missing", {"x": 1}), {"x": 1})
        leftovers = [name for name in os.listdir(os.path.dirname(path))
                     if name.endswith(".part")]
        self.assertEqual(leftovers, [])

    def test_settings_roundtrip(self):
        self.store.save_settings({"hash_algo": "md5"})
        self.assertEqual(self.store.load_settings(), {"hash_algo": "md5"})

    def test_audit_append_and_tail(self):
        for index in range(5):
            self.store.audit("test_action", "admin", {"index": index})
        entries = self.store.read_audit(limit=3)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[-1]["detail"]["index"], 4)
        self.assertEqual(entries[-1]["action"], "test_action")
        self.assertNotIn("password", json.dumps(entries))

    def test_scan_meta_and_groups(self):
        meta = {"scan_id": SCAN_ID, "status": "completed", "created_epoch": 1.0,
                "stats": {"groups": 2}}
        self.store.save_scan_meta(SCAN_ID, meta)
        self.assertEqual(self.store.load_scan_meta(SCAN_ID)["status"], "completed")
        listed = self.store.list_scan_metas()
        self.assertEqual(len(listed), 1)
        groups = [{"group_id": "g-000001", "files": []},
                  {"group_id": "g-000002", "files": []}]
        count = self.store.write_scan_groups(SCAN_ID, groups)
        self.assertEqual(count, 2)
        loaded = list(self.store.iter_scan_groups(SCAN_ID))
        self.assertEqual([item["group_id"] for item in loaded],
                         ["g-000001", "g-000002"])
        self.store.delete_scan(SCAN_ID)
        self.assertEqual(list(self.store.iter_scan_groups(SCAN_ID)), [])

    def test_invalid_ids_rejected(self):
        for bad in ("../etc/passwd", "x", "20260101-000000-zzzzzz"):
            with self.assertRaises(ApiError):
                self.store.scan_dir(bad)
        with self.assertRaises(ApiError):
            self.store._job_meta_path("../../evil")

    def test_jobs_meta_and_results(self):
        self.store.save_job_meta(JOB_ID, {"job_id": JOB_ID, "started_epoch": 1.0,
                                          "scan_id": SCAN_ID})
        self.assertEqual(self.store.load_job_meta(JOB_ID)["job_id"], JOB_ID)
        for index in range(3):
            self.store.append_job_result(JOB_ID, {"path": "/p/%d" % index,
                                                  "status": "deleted"})
        results = list(self.store.iter_job_results(JOB_ID))
        self.assertEqual(len(results), 3)
        limited = list(self.store.iter_job_results(JOB_ID, limit=2))
        self.assertEqual(len(limited), 2)
        metas = self.store.list_job_metas(scan_id=SCAN_ID)
        self.assertEqual(len(metas), 1)

    def test_logs(self):
        scan_log = "scan-%s.log" % SCAN_ID
        delete_log = "delete-%s.log" % JOB_ID
        self.store.write_log(scan_log, u"scan line\n")
        self.store.write_log(delete_log, u"delete line\n")
        logs = self.store.list_logs(limit=10)
        kinds = {item["name"]: item["kind"] for item in logs}
        self.assertEqual(kinds[scan_log], "scan")
        self.assertEqual(kinds[delete_log], "delete")
        self.assertEqual(self.store.resolve_log(scan_log),
                         os.path.join(self.store.logs_dir, scan_log))
        for bad in ("../audit.jsonl", "a.log/../b", "", "bad name.log"):
            with self.assertRaises(ApiError):
                self.store.resolve_log(bad)

    def test_temp_path_inside_data_dir(self):
        path = self.store.temp_path()
        self.assertTrue(path.startswith(self.store.tmp_dir))
        self.assertTrue(os.path.isfile(path))
        os.unlink(path)
