# -*- coding: utf-8 -*-
import hashlib
import os
import time
import unittest

from tests.helpers import Harness, write_bytes

from dupcleaner import deletion as deletion_module
from dupcleaner import fsutil
from dupcleaner.errors import ApiError

CONTENT_X = b"X" * 100
CONTENT_Y = b"Y" * 250


class DeletionBase(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root
        os.makedirs(os.path.join(self.root, "#recycle"), exist_ok=True)
        self.session = self.harness.login()
        self.deletion = self.harness.services.deletion

    def tearDown(self):
        self.harness.cleanup()

    def prepare_scan(self):
        root = self.root
        a1 = write_bytes(os.path.join(root, "a1.txt"), CONTENT_X)
        a2 = write_bytes(os.path.join(root, "sub", "a2.txt"), CONTENT_X)
        b1 = write_bytes(os.path.join(root, "b", "b1.txt"), CONTENT_Y)
        b2 = write_bytes(os.path.join(root, "b", "b2.txt"), CONTENT_Y)
        b3 = write_bytes(os.path.join(root, "c", "b3.txt"), CONTENT_Y)
        self.harness.set_creation(a1, 1000)
        self.harness.set_creation(a2, 2000)
        self.harness.set_creation(b1, 100)
        self.harness.set_creation(b2, 200)
        self.harness.set_creation(b3, 300)
        scan_id, meta = self.harness.run_scan()
        self.assertEqual(meta["status"], "completed")
        self.paths = {"a1": a1, "a2": a2, "b1": b1, "b2": b2, "b3": b3}
        return scan_id

    def wait_job(self, scan_id, job_id, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.deletion.get_job(scan_id, job_id)
            if job.get("status") in ("completed", "partial", "failed", "stopped"):
                return job
            time.sleep(0.02)
        raise AssertionError("delete job did not finish")

    def plan(self, scan_id, payload):
        return self.deletion.create_plan(self.session, scan_id, payload)

    def keep_ids(self, scan_id):
        result = {}
        for group in self.harness.groups(scan_id):
            for file_info in group["files"]:
                if file_info["is_keep"]:
                    result[file_info["name"]] = file_info
        return result


class PlanAndDeleteTest(DeletionBase):
    def test_plan_then_recycle_delete(self):
        scan_id = self.prepare_scan()
        plan = self.plan(scan_id, {"scope": "all_duplicates", "recycle": True})
        self.assertEqual(plan["count"], 3)
        self.assertEqual(plan["bytes"], 100 + 250 * 2)
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["recycle_unavailable"], [])
        self.assertEqual(plan["groups_affected"], 2)
        self.assertTrue(plan["hash_verified"])
        planned_paths = {item["path"] for item in plan["files"]}
        self.assertNotIn(self.paths["a2"], planned_paths)
        self.assertNotIn(self.paths["b3"], planned_paths)
        self.assertIn(self.paths["a1"], planned_paths)
        self.assertEqual(len(plan["files"]), plan["count"])
        self.assertIsNotNone(plan["expires_at"])

        with self.assertRaises(ApiError) as ctx:
            self.deletion.start_delete(self.session, scan_id, {
                "plan_id": plan["plan_id"], "confirm": "delete"})
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

        job_id = self.deletion.start_delete(self.session, scan_id, {
            "plan_id": plan["plan_id"], "confirm": "DELETE"})
        self.assertTrue(job_id.startswith("del-"))
        job = self.wait_job(scan_id, job_id)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["total"], 3)
        self.assertEqual(job["done"], 3)
        self.assertEqual(job["failed"], 0)
        self.assertEqual(job["bytes_done"], plan["bytes"])
        self.assertEqual(len(job["results"]), 3)
        for result in job["results"]:
            self.assertEqual(result["status"], "recycled")
            self.assertTrue(result["recycle_path"])
            self.assertTrue(under_recycle(self.root, result["recycle_path"]))
            self.assertFalse(os.path.exists(result["path"]))
        for name in ("a2", "b3"):
            self.assertTrue(os.path.exists(self.paths[name]))
        self.assertTrue(os.path.exists(os.path.join(self.root, "a2.txt")) is False)
        # 审计日志包含逐文件删除记录
        audit = self.harness.services.store.read_audit(limit=100)
        actions = [entry["action"] for entry in audit]
        self.assertIn("delete_plan", actions)
        self.assertIn("delete_start", actions)
        self.assertIn("delete_file", actions)
        self.assertIn("delete_end", actions)

    def test_plan_is_one_time(self):
        scan_id = self.prepare_scan()
        plan = self.plan(scan_id, {"scope": "all_duplicates", "recycle": True})
        self.deletion.start_delete(self.session, scan_id, {
            "plan_id": plan["plan_id"], "confirm": "DELETE"})
        with self.assertRaises(ApiError) as ctx:
            self.deletion.start_delete(self.session, scan_id, {
                "plan_id": plan["plan_id"], "confirm": "DELETE"})
        self.assertEqual(ctx.exception.code, "PLAN_INVALID")

    def test_plan_expires(self):
        scan_id = self.prepare_scan()
        plan = self.plan(scan_id, {"scope": "all_duplicates", "recycle": True})
        stored = self.deletion._plans[plan["plan_id"]]
        stored["expires_epoch"] = time.time() - 1
        with self.assertRaises(ApiError) as ctx:
            self.deletion.start_delete(self.session, scan_id, {
                "plan_id": plan["plan_id"], "confirm": "DELETE"})
        self.assertEqual(ctx.exception.code, "PLAN_EXPIRED")

    def test_plan_bound_to_session(self):
        scan_id = self.prepare_scan()
        plan = self.plan(scan_id, {"scope": "all_duplicates", "recycle": True})
        other = self.harness.services.auth.login("admin", "devpass")
        with self.assertRaises(ApiError) as ctx:
            self.deletion.start_delete(other, scan_id, {
                "plan_id": plan["plan_id"], "confirm": "DELETE"})
        self.assertEqual(ctx.exception.code, "FORBIDDEN")

    def test_keeper_cannot_be_deleted(self):
        scan_id = self.prepare_scan()
        keepers = self.keep_ids(scan_id)
        keeper = keepers["a2.txt"]
        other = None
        for group in self.harness.groups(scan_id):
            for file_info in group["files"]:
                if not file_info["is_keep"] and file_info["name"] == "a1.txt":
                    other = file_info
        plan = self.plan(scan_id, {
            "file_ids": [keeper["file_id"], other["file_id"]],
            "recycle": True,
        })
        self.assertEqual(plan["count"], 1)
        reasons = [item["reason"] for item in plan["skipped"]]
        self.assertIn("IS_KEEPER", reasons)
        with self.assertRaises(ApiError):
            self.plan(scan_id, {"file_ids": [keeper["file_id"]],
                                "recycle": True})

    def test_file_changed_is_skipped(self):
        scan_id = self.prepare_scan()
        # 扫描后修改候选文件（大小不变、mtime 变化）
        with open(self.paths["a1"], "wb") as handle:
            handle.write(b"Z" * 100)
        plan = self.plan(scan_id, {"scope": "all_duplicates", "recycle": True})
        reasons = [item["reason"] for item in plan["skipped"]]
        self.assertIn("FILE_CHANGED", reasons)
        self.assertEqual(plan["count"], 2)
        planned_paths = {item["path"] for item in plan["files"]}
        self.assertNotIn(self.paths["a1"], planned_paths)
        # bytes 只统计通过校验的文件（2 个 Y 文件）
        self.assertEqual(plan["bytes"], 250 * 2)

    def test_recycle_unavailable_then_create(self):
        scan_id = self.prepare_scan()
        os.rmdir(os.path.join(self.root, "#recycle"))
        with self.assertRaises(ApiError) as ctx:
            self.plan(scan_id, {"scope": "all_duplicates", "recycle": True})
        self.assertEqual(ctx.exception.code, "RECYCLE_UNAVAILABLE")
        self.assertTrue(ctx.exception.details["recycle_unavailable"])

        # 重新扫描并开启 create_recycle_dir，删除时自动创建回收站目录
        scan_id2, _meta = self.harness.run_scan(
            options={"create_recycle_dir": True})
        plan = self.plan(scan_id2, {"scope": "all_duplicates", "recycle": True})
        self.assertEqual(plan["count"], 3)
        job_id = self.deletion.start_delete(self.session, scan_id2, {
            "plan_id": plan["plan_id"], "confirm": "DELETE"})
        job = self.wait_job(scan_id2, job_id)
        self.assertEqual(job["status"], "completed")
        self.assertTrue(os.path.isdir(os.path.join(self.root, "#recycle")))

    def test_permanent_delete_requires_phrase(self):
        scan_id = self.prepare_scan()
        plan = self.plan(scan_id, {"scope": "all_duplicates", "recycle": False})
        for phrase in (None, "", u"删除"):
            with self.assertRaises(ApiError) as ctx:
                self.deletion.start_delete(self.session, scan_id, {
                    "plan_id": plan["plan_id"], "confirm": "DELETE",
                    "permanent_phrase": phrase})
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        job_id = self.deletion.start_delete(self.session, scan_id, {
            "plan_id": plan["plan_id"], "confirm": "DELETE",
            "permanent_phrase": u"永久删除"})
        job = self.wait_job(scan_id, job_id)
        self.assertEqual(job["status"], "completed")
        for result in job["results"]:
            self.assertEqual(result["status"], "deleted")
            self.assertFalse(os.path.exists(result["path"]))
        self.assertTrue(os.path.exists(self.paths["a2"]))
        self.assertTrue(os.path.exists(self.paths["b3"]))

    def test_manual_groups_never_auto_deleted(self):
        files = {}
        files["a1"] = write_bytes(os.path.join(self.root, "a1.txt"), CONTENT_X)
        files["a2"] = write_bytes(os.path.join(self.root, "a2.txt"), CONTENT_X)
        self.harness.set_creation(files["a1"], 5000)
        self.harness.set_creation(files["a2"], 5000)
        scan_id, meta = self.harness.run_scan()
        self.assertEqual(meta["needs_manual_keep_groups"], 1)
        with self.assertRaises(ApiError) as ctx:
            self.plan(scan_id, {"scope": "all_duplicates", "recycle": True})
        self.assertEqual(ctx.exception.code, "PLAN_INVALID")
        reasons = [item["reason"] for item in ctx.exception.details["skipped"]]
        self.assertIn("MANUAL_KEEP_REQUIRED", reasons)
        with self.assertRaises(ApiError) as ctx2:
            self.plan(scan_id, {"scope": "all_duplicates", "recycle": True,
                                "include_manual_groups": True})
        self.assertEqual(ctx2.exception.code, "INVALID_ARGUMENT")
        # 显式 file_ids 也不能删除人工确认组里的文件
        group = self.harness.groups(scan_id)[0]
        non_keeper = [item for item in group["files"]
                      if not item["is_keep"]][0]
        with self.assertRaises(ApiError) as ctx3:
            self.plan(scan_id, {"file_ids": [non_keeper["file_id"]],
                                "recycle": True})
        self.assertEqual(ctx3.exception.code, "PLAN_INVALID")
        self.assertIn("MANUAL_KEEP_REQUIRED",
                      [item["reason"] for item in ctx3.exception.details["skipped"]])

    def test_mtime_fallback_group_deletable(self):
        root = self.root
        first = write_bytes(os.path.join(root, "fb1.txt"), CONTENT_X)
        second = write_bytes(os.path.join(root, "fb2.txt"), CONTENT_X)
        self.harness.set_creation(first, None, source="unknown")
        self.harness.set_creation(second, None, source="unknown")
        os.utime(first, ns=(1700000100 * 1000000000, 1700000100 * 1000000000))
        os.utime(second, ns=(1700000200 * 1000000000, 1700000200 * 1000000000))
        scan_id, meta = self.harness.run_scan()
        self.assertEqual(meta["stats"]["mtime_fallback_groups"], 1)
        group = self.harness.groups(scan_id)[0]
        self.assertTrue(group["time_fallback"])
        self.assertFalse(group["needs_manual_keep"])
        # mtime 最新的 fb2.txt 为保留项，fb1.txt 默认勾选
        keeper = [item for item in group["files"] if item["is_keep"]][0]
        self.assertEqual(keeper["name"], "fb2.txt")
        non_keeper = [item for item in group["files"] if not item["is_keep"]][0]
        self.assertTrue(non_keeper["default_selected"])
        plan = self.plan(scan_id, {"scope": "all_duplicates", "recycle": True})
        self.assertEqual(plan["count"], 1)
        self.assertEqual(plan["skipped"], [])
        job_id = self.deletion.start_delete(self.session, scan_id, {
            "plan_id": plan["plan_id"], "confirm": "DELETE"})
        job = self.wait_job(scan_id, job_id)
        self.assertEqual(job["status"], "completed")
        self.assertFalse(os.path.exists(non_keeper["path"]))
        self.assertTrue(os.path.exists(keeper["path"]))


class RecyclePathTest(unittest.TestCase):
    def test_recycle_destination(self):
        destination = deletion_module.recycle_destination(
            os.path.join("C:\\", "volume1", "photo") if os.name == "nt"
            else "/volume1/photo",
            os.path.join("C:\\", "volume1", "photo") if os.name == "nt"
            else "/volume1/photo",
            "20260101-120000")
        self.assertIn("#recycle", destination)
        self.assertIn(os.path.join("@duplicate-cleaner", "20260101-120000"),
                      destination)

    def test_recycle_collision_suffix(self):
        tmpdir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "_tmp_recycle_test")
        root = tmpdir
        try:
            os.makedirs(root, exist_ok=True)
            source = os.path.join(root, "a.jpg")
            write_bytes(source, b"x")
            first = deletion_module.recycle_destination(root, source, "S1")
            os.makedirs(os.path.dirname(first), exist_ok=True)
            write_bytes(first, b"existing")
            second = deletion_module.recycle_destination(root, source, "S1")
            self.assertTrue(second.endswith(".dup-1"))
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class RevalidateTest(DeletionBase):
    def _records(self):
        root = self.root
        candidate = write_bytes(os.path.join(root, "cand.txt"), CONTENT_X)
        keeper = write_bytes(os.path.join(root, "keeper.txt"), CONTENT_X)
        digest = hashlib.sha256(CONTENT_X).hexdigest()
        st_candidate = os.stat(candidate)
        st_keeper = os.stat(keeper)
        record = {
            "path": candidate, "size": st_candidate.st_size,
            "mtime_ns": st_candidate.st_mtime_ns,
            "inode": st_candidate.st_ino, "dev": st_candidate.st_dev,
            "hash": digest, "group_id": "g-000001",
        }
        keeper_record = {
            "path": keeper, "size": st_keeper.st_size,
            "mtime_ns": st_keeper.st_mtime_ns,
            "inode": st_keeper.st_ino, "dev": st_keeper.st_dev,
            "hash": digest, "group_id": "g-000001",
        }
        return record, keeper_record, candidate, keeper, digest

    def test_revalidate_ok(self):
        record, keeper_record, _c, _k, _digest = self._records()
        result = deletion_module.revalidate(
            record, keeper_record, self.harness.services.shares,
            hash_check=True)
        self.assertTrue(result["ok"], result)

    def test_revalidate_changed_size(self):
        record, keeper_record, candidate, _k, _digest = self._records()
        with open(candidate, "ab") as handle:
            handle.write(b"more")
        result = deletion_module.revalidate(record, keeper_record,
                                            self.harness.services.shares,
                                            hash_check=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "FILE_CHANGED")

    def test_revalidate_hash_mismatch(self):
        record, keeper_record, _c, _k, _digest = self._records()
        record["hash"] = "0" * 64
        result = deletion_module.revalidate(record, keeper_record,
                                            self.harness.services.shares,
                                            hash_check=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "HASH_MISMATCH")

    def test_revalidate_path_not_allowed(self):
        record, keeper_record, _c, _k, _digest = self._records()
        record["path"] = "/etc/passwd"
        result = deletion_module.revalidate(record, keeper_record,
                                            self.harness.services.shares,
                                            hash_check=True)
        self.assertEqual(result["code"], "PATH_NOT_ALLOWED")

    def test_revalidate_keeper_missing(self):
        record, keeper_record, _c, keeper, _digest = self._records()
        os.unlink(keeper)
        result = deletion_module.revalidate(record, keeper_record,
                                            self.harness.services.shares,
                                            hash_check=True)
        self.assertEqual(result["code"], "KEEPER_MISSING")

    def test_revalidate_symlink_rejected(self):
        record, keeper_record, candidate, _k, _digest = self._records()
        target = write_bytes(os.path.join(self.root, "target.txt"), CONTENT_X)
        os.unlink(candidate)
        try:
            os.symlink(target, candidate)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlink not supported")
        result = deletion_module.revalidate(record, keeper_record,
                                            self.harness.services.shares,
                                            hash_check=True)
        self.assertFalse(result["ok"])
        self.assertIn(result["code"], ("PATH_NOT_ALLOWED", "FILE_CHANGED",
                                       "NOT_FOUND"))


def under_recycle(share_root, path):
    expected = os.path.join(share_root, "#recycle", "@duplicate-cleaner")
    return fsutil.is_under(path, expected)
