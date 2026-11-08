# -*- coding: utf-8 -*-
"""v0045：硬链接合并（计划 / 执行 / 恢复）测试。"""

import json
import os
import time
import unittest

from tests.helpers import Harness, write_bytes

from dupcleaner.errors import ApiError


class MergeTestBase(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root
        self.merge = self.harness.services.merges
        self.session = self.harness.login()

    def tearDown(self):
        self.harness.cleanup()

    def make_three(self):
        paths = {}
        for name, created in (("a.txt", 300), ("b.txt", 200), ("c.txt", 100)):
            path = write_bytes(os.path.join(self.root, name), b"X" * 400)
            self.harness.set_creation(path, created)
            paths[name] = path
        self.harness.set_creation(os.path.join(self.root, "keep.txt"), 50)
        return paths

    def scan(self):
        scan_id, meta = self.harness.run_scan()
        self.assertEqual(meta["status"], "completed")
        return scan_id

    def wait_job(self, job_id, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.merge.get_job(self.scan_id, job_id)
            if job.get("status") in ("completed", "partial", "failed", "stopped"):
                return job
            time.sleep(0.05)
        raise AssertionError("merge job did not finish")


class MergePlanTest(MergeTestBase):
    def test_disabled_by_default(self):
        self.make_three()
        self.scan_id = self.scan()
        with self.assertRaises(ApiError) as ctx:
            self.merge.create_plan(self.session, self.scan_id, {"scope": "all_exact"})
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertIn(u"未启用", ctx.exception.message)

    def test_plan_counts_and_skips(self):
        paths = self.make_three()
        self.harness.services.settings.update({"merge_enabled": True})
        self.scan_id = self.scan()
        group = self.harness.group_by_paths(self.scan_id, "a.txt")
        file_ids = [item["file_id"] for item in group["files"]]
        plan = self.merge.create_plan(self.session, self.scan_id,
                                      {"file_ids": file_ids})
        self.assertEqual(plan["count"], 2)  # b/c（a 为保留项）
        self.assertEqual(plan["bytes"], 800)
        self.assertEqual(plan["groups_affected"], 1)
        self.assertTrue(plan["warning"])
        self.assertEqual(len(plan["files"]), 2)

    def test_plan_skips_protected(self):
        self.make_three()
        self.harness.services.settings.update({
            "merge_enabled": True, "protect_name_regex": [r"^b\."]})
        self.scan_id = self.scan()
        group = self.harness.group_by_paths(self.scan_id, "a.txt")
        file_ids = [item["file_id"] for item in group["files"]]
        plan = self.merge.create_plan(self.session, self.scan_id,
                                      {"file_ids": file_ids})
        self.assertEqual(plan["count"], 1)  # c
        reasons = [item.get("reason") for item in plan["skipped"]]
        self.assertIn("PROTECTED", reasons)
        self.assertIn("IS_KEEPER", reasons)

    def test_plan_requires_explicit_scope(self):
        self.harness.services.settings.update({"merge_enabled": True})
        self.make_three()
        self.scan_id = self.scan()
        with self.assertRaises(ApiError):
            self.merge.create_plan(self.session, self.scan_id, {})


class MergeExecuteTest(MergeTestBase):
    def setUp(self):
        super(MergeExecuteTest, self).setUp()
        self.harness.services.settings.update({"merge_enabled": True})
        self.paths = self.make_three()
        self.scan_id = self.scan()
        group = self.harness.group_by_paths(self.scan_id, "a.txt")
        self.file_ids = [item["file_id"] for item in group["files"]]

    def test_merge_creates_hardlinks_and_manifest(self):
        plan = self.merge.create_plan(self.session, self.scan_id,
                                      {"file_ids": self.file_ids})
        job_id = self.merge.start_merge(self.session, self.scan_id, {
            "plan_id": plan["plan_id"], "confirm": "MERGE"})
        job = self.wait_job(job_id)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["done"], 2)
        stat_a = os.stat(self.paths["a.txt"])
        for name in ("b.txt", "c.txt"):
            stat = os.stat(self.paths[name])
            self.assertEqual(stat.st_ino, stat_a.st_ino)
            self.assertEqual(stat.st_dev, stat_a.st_dev)
        self.assertGreaterEqual(stat_a.st_nlink, 3)
        # 内容未变
        for name in ("a.txt", "b.txt", "c.txt"):
            with open(self.paths[name], "rb") as handle:
                self.assertEqual(handle.read(), b"X" * 400)
        manifest = self.merge.load_manifest(job_id)
        self.assertIsNotNone(manifest)
        self.assertEqual(len(manifest["entries"]), 2)
        self.assertEqual(manifest["entries"][0]["master"], self.paths["a.txt"])

    def test_merge_requires_confirm(self):
        plan = self.merge.create_plan(self.session, self.scan_id,
                                      {"file_ids": self.file_ids})
        with self.assertRaises(ApiError) as ctx:
            self.merge.start_merge(self.session, self.scan_id, {
                "plan_id": plan["plan_id"], "confirm": "DELETE"})
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_second_plan_reports_already_linked(self):
        plan = self.merge.create_plan(self.session, self.scan_id,
                                      {"file_ids": self.file_ids})
        job_id = self.merge.start_merge(self.session, self.scan_id, {
            "plan_id": plan["plan_id"], "confirm": "MERGE"})
        self.wait_job(job_id)
        with self.assertRaises(ApiError) as ctx:
            self.merge.create_plan(self.session, self.scan_id,
                                   {"file_ids": self.file_ids})
        self.assertEqual(ctx.exception.code, "PLAN_INVALID")
        reasons = [item.get("reason") for item in
                   (ctx.exception.details or {}).get("skipped") or []]
        self.assertIn("ALREADY_LINKED", reasons)

    def test_restore_independent_copies(self):
        plan = self.merge.create_plan(self.session, self.scan_id,
                                      {"file_ids": self.file_ids})
        job_id = self.merge.start_merge(self.session, self.scan_id, {
            "plan_id": plan["plan_id"], "confirm": "MERGE"})
        self.wait_job(job_id)
        before = os.stat(self.paths["b.txt"])
        result = self.merge.restore(self.session, job_id, {"confirm": True})
        self.assertEqual(result["restored"], 2)
        self.assertEqual(len(result["results"]), 2)
        stat_a = os.stat(self.paths["a.txt"])
        stat_b = os.stat(self.paths["b.txt"])
        self.assertNotEqual(stat_a.st_ino, stat_b.st_ino)
        self.assertEqual(stat_b.st_size, before.st_size)
        self.assertEqual(stat_b.st_mtime_ns, before.st_mtime_ns)
        with open(self.paths["b.txt"], "rb") as handle:
            self.assertEqual(handle.read(), b"X" * 400)
        # 恢复后重新扫描：a/b/c 都是独立 inode，重新成为普通重复组
        scan2, meta2 = self.harness.run_scan()
        self.assertEqual(meta2["status"], "completed")
        group2 = self.harness.group_by_paths(scan2, "a.txt")
        names = sorted(item["name"] for item in group2["files"])
        self.assertEqual(names, ["a.txt", "b.txt", "c.txt"])
        inodes = {os.stat(self.paths[name]).st_ino for name in names}
        self.assertEqual(len(inodes), 3)

    def test_restore_requires_confirm(self):
        plan = self.merge.create_plan(self.session, self.scan_id,
                                      {"file_ids": self.file_ids})
        job_id = self.merge.start_merge(self.session, self.scan_id, {
            "plan_id": plan["plan_id"], "confirm": "MERGE"})
        self.wait_job(job_id)
        with self.assertRaises(ApiError):
            self.merge.restore(self.session, job_id, {})
        # 未知 job id
        with self.assertRaises(ApiError):
            self.merge.restore(self.session, "mrg-nope", {"confirm": True})

    def test_list_jobs(self):
        plan = self.merge.create_plan(self.session, self.scan_id,
                                      {"file_ids": self.file_ids})
        job_id = self.merge.start_merge(self.session, self.scan_id, {
            "plan_id": plan["plan_id"], "confirm": "MERGE"})
        self.wait_job(job_id)
        jobs = self.merge.list_jobs(scan_id=self.scan_id)
        self.assertTrue(any(item.get("job_id") == job_id for item in jobs))


if __name__ == "__main__":
    unittest.main()
