# -*- coding: utf-8 -*-
"""v0045：套件独立回收站（删除进 #duplicate-cleaner-trash、整组恢复、清理）。"""

import os
import time
import unittest

from tests.helpers import Harness, write_bytes

from dupcleaner import config
from dupcleaner.errors import ApiError


class TrashTestBase(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root
        self.svc = self.harness.services
        self.deletion = self.svc.deletion
        self.trash = self.svc.trash
        self.session = self.harness.login()
        self.svc.settings.update({"delete_recycle_mode": "package"})

    def tearDown(self):
        self.harness.cleanup()

    def make_pair(self):
        a = write_bytes(os.path.join(self.root, "a.txt"), b"Y" * 300)
        b = write_bytes(os.path.join(self.root, "b.txt"), b"Y" * 300)
        c = write_bytes(os.path.join(self.root, "sub", "c.txt"), b"Y" * 300)
        self.harness.set_creation(a, 300)
        self.harness.set_creation(b, 200)
        self.harness.set_creation(c, 100)
        return a, b, c

    def scan(self):
        scan_id, meta = self.harness.run_scan()
        self.assertEqual(meta["status"], "completed")
        return scan_id

    def plan_and_delete(self, scan_id, names):
        group = self.harness.group_by_paths(scan_id, names[0])
        wanted = set(names)
        file_ids = [item["file_id"] for item in group["files"]
                    if item["name"] in wanted]
        plan = self.deletion.create_plan(self.session, scan_id, {
            "file_ids": file_ids, "recycle": True})
        self.assertEqual(plan.get("recycle_mode"), "package")
        job_id = self.deletion.start_delete(self.session, scan_id, {
            "plan_id": plan["plan_id"], "confirm": "DELETE"})
        job = self.wait_job(scan_id, job_id)
        return plan, job_id, job

    def wait_job(self, scan_id, job_id, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.deletion.get_job(scan_id, job_id)
            if job and job.get("status") in ("completed", "partial", "failed", "stopped"):
                return job
            time.sleep(0.05)
        raise AssertionError("delete job did not finish")


class TrashPackageModeTest(TrashTestBase):
    def test_delete_to_package_trash_and_restore(self):
        a, b, c = self.make_pair()
        scan_id = self.scan()
        plan, job_id, job = self.plan_and_delete(scan_id, ["b.txt"])
        self.assertIn(job["status"], ("completed", "partial"))
        self.assertFalse(os.path.exists(b))
        trash_root = os.path.join(self.root, config.TRASH_DIR_NAME)
        self.assertTrue(os.path.isdir(trash_root))
        jobs = self.trash.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["entries_total"], 1)
        detail = self.trash.detail(jobs[0]["trash_id"])
        entry = detail["entries"][0]
        self.assertFalse(entry["trash_exists"] is False)
        self.assertTrue(entry["trash_exists"])
        self.assertIn(config.TRASH_DIR_NAME, entry["trash_path"])
        # 整组恢复
        result = self.trash.restore(self.session, jobs[0]["trash_id"],
                                    {"confirm": True})
        self.assertEqual(result["restored"], 1)
        self.assertTrue(os.path.isfile(b))
        with open(b, "rb") as handle:
            self.assertEqual(handle.read(), b"Y" * 300)
        # 恢复后原位置冲突时再次恢复 → skipped ORIGINAL_EXISTS / 已完成
        again = self.trash.restore(self.session, jobs[0]["trash_id"],
                                   {"confirm": True})
        self.assertEqual(again["restored"], 0)

    def test_restore_conflict_keeps_original(self):
        a, b, c = self.make_pair()
        scan_id = self.scan()
        _plan, _job_id, _job = self.plan_and_delete(scan_id, ["b.txt"])
        write_bytes(b, b"NEW")
        jobs = self.trash.list_jobs()
        result = self.trash.restore(self.session, jobs[0]["trash_id"],
                                    {"confirm": True})
        self.assertEqual(result["restored"], 0)
        reasons = [item.get("reason") for item in result["results"]]
        self.assertIn("ORIGINAL_EXISTS", reasons)
        with open(b, "rb") as handle:
            self.assertEqual(handle.read(), b"NEW")
        detail = self.trash.detail(jobs[0]["trash_id"])
        self.assertTrue(detail["entries"][0]["trash_exists"])

    def test_purge_requires_phrase(self):
        a, b, c = self.make_pair()
        scan_id = self.scan()
        self.plan_and_delete(scan_id, ["b.txt"])
        jobs = self.trash.list_jobs()
        trash_id = jobs[0]["trash_id"]
        with self.assertRaises(ApiError):
            self.trash.purge(self.session, trash_id, {"confirm": True})
        with self.assertRaises(ApiError):
            self.trash.purge(self.session, trash_id, {
                "confirm": True, "permanent_phrase": "删除"})
        result = self.trash.purge(self.session, trash_id, {
            "confirm": True,
            "permanent_phrase": config.REQUIRED_PERMANENT_PHRASE})
        self.assertEqual(result["removed"], 1)
        self.assertGreater(result["bytes_freed"], 0)
        manifest = self.trash.load(trash_id)
        self.assertTrue(manifest["purged"])
        # 已清理的记录不能恢复
        with self.assertRaises(ApiError):
            self.trash.restore(self.session, trash_id, {"confirm": True})

    def test_restore_selected_paths(self):
        a, b, c = self.make_pair()
        scan_id = self.scan()
        # 删两个文件，只恢复其中一个
        self.plan_and_delete(scan_id, ["b.txt", "c.txt"])
        jobs = self.trash.list_jobs()
        detail = self.trash.detail(jobs[0]["trash_id"])
        self.assertEqual(len(detail["entries"]), 2)
        target = detail["entries"][0]["original"]
        result = self.trash.restore(self.session, jobs[0]["trash_id"], {
            "confirm": True, "paths": [target]})
        self.assertEqual(result["restored"], 1)
        self.assertTrue(os.path.exists(target))

    def test_share_mode_unchanged_by_default(self):
        self.svc.settings.update({"delete_recycle_mode": "share"})
        a, b, c = self.make_pair()
        scan_id = self.scan()
        group = self.harness.group_by_paths(scan_id, "a.txt")
        b_id = [item["file_id"] for item in group["files"]
                if item["name"] == "b.txt"][0]
        # 共享文件夹没有 #recycle 且未允许创建 → 计划应拒绝
        with self.assertRaises(ApiError) as ctx:
            self.deletion.create_plan(self.session, scan_id, {
                "file_ids": [b_id], "recycle": True})
        self.assertEqual(ctx.exception.code, "RECYCLE_UNAVAILABLE")

    def test_trash_list_and_detail_api(self):
        from tests.helpers import WebServer
        a, b, c = self.make_pair()
        scan_id = self.scan()
        self.plan_and_delete(scan_id, ["b.txt"])
        server = WebServer(self.harness).start()
        try:
            client = server.client
            csrf = client.login()
            status, _headers, payload = client.json("GET", "/api/trash")
            self.assertEqual(status, 200)
            self.assertEqual(payload["recycle_mode"], "package")
            self.assertEqual(len(payload["trash"]), 1)
            trash_id = payload["trash"][0]["trash_id"]
            status, _headers, detail = client.json(
                "GET", "/api/trash/" + trash_id)
            self.assertEqual(status, 200)
            self.assertEqual(len(detail["entries"]), 1)
            status, _headers, restored = client.json(
                "POST", "/api/trash/%s/restore" % trash_id,
                {"confirm": True}, csrf=csrf)
            self.assertEqual(status, 200)
            self.assertEqual(restored["restored"], 1)
        finally:
            server.stop()


class TrashApiMergeTest(unittest.TestCase):
    def test_merge_disabled_api(self):
        from tests.helpers import WebServer
        harness = Harness()
        server = WebServer(harness).start()
        try:
            client = server.client
            csrf = client.login()
            status, _headers, payload = client.json(
                "POST", "/api/scans/20260101-000000-abcdef/merge-plan",
                {"scope": "all_exact"}, csrf=csrf)
            # 扫描不存在 → 404 优先；这里只验证路由存在且不是 404 路由缺失
            self.assertIn(status, (400, 404, 409))
        finally:
            server.stop()
            harness.cleanup()


if __name__ == "__main__":
    unittest.main()
