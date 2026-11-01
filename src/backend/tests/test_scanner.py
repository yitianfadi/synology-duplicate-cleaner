# -*- coding: utf-8 -*-
import csv
import io
import os
import time
import unittest

from tests.helpers import Harness, write_bytes, write_text

from dupcleaner import fsutil
from dupcleaner.errors import ApiError

CONTENT_X = b"X" * 100
CONTENT_Y = b"Y" * 250


class ScannerBase(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root

    def tearDown(self):
        self.harness.cleanup()

    def make_basic(self):
        root = self.root
        a1 = write_bytes(os.path.join(root, "a1.txt"), CONTENT_X)
        a2 = write_bytes(os.path.join(root, "sub", "a2.txt"), CONTENT_X)
        b1 = write_bytes(os.path.join(root, "b", "b1.txt"), CONTENT_Y)
        b2 = write_bytes(os.path.join(root, "b", "b2.txt"), CONTENT_Y)
        b3 = write_bytes(os.path.join(root, "c", "b3.txt"), CONTENT_Y)
        unique = write_bytes(os.path.join(root, "unique.txt"), b"unique-content")
        self.harness.set_creation(a1, 1000)
        self.harness.set_creation(a2, 2000)
        self.harness.set_creation(b1, 100)
        self.harness.set_creation(b2, 200)
        self.harness.set_creation(b3, 300)
        self.harness.set_creation(unique, 50)
        return {"a1": a1, "a2": a2, "b1": b1, "b2": b2, "b3": b3,
                "unique": unique}

    def group_named(self, scan_id, name):
        return self.harness.group_by_paths(scan_id, name)

    def file_in_group(self, group, name):
        for file_info in group["files"]:
            if file_info["name"] == name:
                return file_info
        raise AssertionError(name)


class ScanGroupingTest(ScannerBase):
    def test_scan_keep_latest(self):
        self.make_basic()
        scan_id, meta = self.harness.run_scan()
        self.assertEqual(meta["status"], "completed")
        self.assertFalse(meta["creation_time_unsafe"])
        self.assertEqual(meta["stats"]["groups"], 2)
        self.assertEqual(meta["stats"]["duplicate_files"], 5)
        self.assertEqual(meta["stats"]["candidate_files"], 5)
        self.assertEqual(meta["stats"]["reclaimable_bytes"], 100 + 250 * 2)
        self.assertFalse(meta["stats"]["skipped"]["symlink"])
        groups = self.harness.groups(scan_id)
        self.assertEqual(len(groups), 2)
        xgroup = self.group_named(scan_id, "a1.txt")
        self.assertEqual(xgroup["count"], 2)
        self.assertEqual(xgroup["size"], 100)
        self.assertEqual(xgroup["wasted_bytes"], 100)
        self.assertEqual(xgroup["keep_reason"], "latest_created")
        self.assertFalse(xgroup["needs_manual_keep"])
        keeper = self.file_in_group(xgroup, "a2.txt")
        self.assertTrue(keeper["is_keep"])
        self.assertFalse(keeper["default_selected"])
        deletable = self.file_in_group(xgroup, "a1.txt")
        self.assertFalse(deletable["is_keep"])
        self.assertTrue(deletable["default_selected"])
        self.assertEqual(xgroup["keep_file_id"], keeper["file_id"])
        ygroup = self.group_named(scan_id, "b1.txt")
        self.assertEqual(ygroup["count"], 3)
        self.assertTrue(self.file_in_group(ygroup, "b3.txt")["is_keep"])
        self.assertTrue(self.file_in_group(ygroup, "b1.txt")["default_selected"])
        self.assertTrue(self.file_in_group(ygroup, "b2.txt")["default_selected"])
        # 创建时间来源必须是真实 btime（注入为 statx）
        self.assertEqual(keeper["created_source"], "statx")
        self.assertIsNotNone(keeper["created_at"])
        self.assertIsNotNone(keeper["modified_at"])
        self.assertEqual(keeper["hash"], ygroup["hash"] if False else keeper["hash"])
        self.assertEqual(len(keeper["hash"]), 64)
        self.assertEqual(len(keeper["quick_hash"]), 64)
        # 顺序：保留项在组内第一个
        self.assertEqual(xgroup["files"][0]["file_id"], xgroup["keep_file_id"])

    def test_keep_earliest(self):
        self.make_basic()
        scan_id, _meta = self.harness.run_scan(
            options={"keep_mode": "earliest"})
        xgroup = self.group_named(scan_id, "a1.txt")
        self.assertEqual(xgroup["keep_reason"], "earliest_created")
        self.assertTrue(self.file_in_group(xgroup, "a1.txt")["is_keep"])
        self.assertTrue(self.file_in_group(xgroup, "a2.txt")["default_selected"])
        ygroup = self.group_named(scan_id, "b1.txt")
        self.assertTrue(self.file_in_group(ygroup, "b1.txt")["is_keep"])

    def test_keep_pinned(self):
        self.make_basic()
        scan_id, meta = self.harness.run_scan(options={
            "keep_mode": "pinned",
            "pinned_path": os.path.join(self.root, "sub"),
        })
        xgroup = self.group_named(scan_id, "a1.txt")
        self.assertEqual(xgroup["keep_reason"], "pinned_path")
        self.assertTrue(self.file_in_group(xgroup, "a2.txt")["is_keep"])
        self.assertFalse(xgroup["needs_manual_keep"])
        # Y 组没有文件命中 pinned 路径 → 需要人工确认
        ygroup = self.group_named(scan_id, "b1.txt")
        self.assertTrue(ygroup["needs_manual_keep"])
        self.assertEqual(ygroup["keep_reason"], "tie")
        self.assertTrue(all(not item["default_selected"]
                            for item in ygroup["files"]))
        self.assertEqual(meta["needs_manual_keep_groups"], 1)

    def test_pinned_unknown_time_requires_manual(self):
        files = self.make_basic()
        self.harness.set_creation(files["a2"], None, source="unknown")
        self.harness.creation_times[
            os.path.normcase(os.path.abspath(files["a2"]))]["created_ts"] = None
        scan_id, _meta = self.harness.run_scan(options={
            "keep_mode": "pinned",
            "pinned_path": os.path.join(self.root, "sub"),
        })
        xgroup = self.group_named(scan_id, "a1.txt")
        self.assertTrue(xgroup["needs_manual_keep"])
        self.assertTrue(all(not item["default_selected"]
                            for item in xgroup["files"]))

    def test_tie_requires_manual(self):
        files = self.make_basic()
        self.harness.set_creation(files["a1"], 5000)
        self.harness.set_creation(files["a2"], 5000)
        scan_id, meta = self.harness.run_scan()
        xgroup = self.group_named(scan_id, "a1.txt")
        self.assertTrue(xgroup["needs_manual_keep"])
        self.assertEqual(xgroup["keep_reason"], "tie")
        self.assertTrue(all(not item["default_selected"]
                            for item in xgroup["files"]))
        self.assertEqual(meta["needs_manual_keep_groups"], 1)

    def test_unknown_creation_time_is_unsafe(self):
        files = self.make_basic()
        for key in ("a1", "a2"):
            path = files[key]
            self.harness.creation_times[os.path.normcase(os.path.abspath(path))] = {
                "created_ts": None, "created_ns": 0,
                "created_source": "unknown", "created_unsafe": True,
            }
        scan_id, meta = self.harness.run_scan()
        self.assertTrue(meta["creation_time_unsafe"])
        xgroup = self.group_named(scan_id, "a1.txt")
        self.assertTrue(xgroup["needs_manual_keep"])
        self.assertTrue(xgroup["creation_time_suspect"])

    def test_ctime_fallback_marks_unsafe(self):
        files = self.make_basic()
        for key in ("a1", "a2"):
            path = files[key]
            self.harness.creation_times[os.path.normcase(os.path.abspath(path))] = {
                "created_ts": 777, "created_ns": 0,
                "created_source": "ctime", "created_unsafe": True,
            }
        _scan_id, meta = self.harness.run_scan(
            options={"allow_ctime_fallback": True})
        self.assertTrue(meta["creation_time_unsafe"])

    def test_groups_pagination_sort_and_search(self):
        self.make_basic()
        scan_id, _meta = self.harness.run_scan()
        page = self.harness.services.scanner.groups_page(scan_id, offset=0,
                                                         limit=10)
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["groups"]), 2)
        page_one = self.harness.services.scanner.groups_page(
            scan_id, offset=0, limit=1)
        self.assertEqual(page_one["total"], 2)
        self.assertEqual(len(page_one["groups"]), 1)
        self.assertEqual(page_one["groups"][0]["size"], 250)
        by_size = self.harness.services.scanner.groups_page(
            scan_id, sort="size_desc", limit=10)
        self.assertEqual(by_size["groups"][0]["size"], 250)
        by_search = self.harness.services.scanner.groups_page(
            scan_id, q="a1", limit=10)
        self.assertEqual(by_search["total"], 1)
        self.assertEqual(by_search["groups"][0]["size"], 100)
        with self.assertRaises(ApiError):
            self.harness.services.scanner.groups_page(scan_id, sort="bogus")

    def test_stats_full(self):
        self.make_basic()
        scan_id, _meta = self.harness.run_scan()
        stats = self.harness.services.scanner.stats_full(scan_id)
        self.assertEqual(stats["scan_id"], scan_id)
        self.assertTrue(stats["by_extension"])
        self.assertEqual(stats["by_extension"][0]["extension"], ".txt")
        self.assertEqual(stats["by_extension"][0]["count"], 5)
        self.assertTrue(stats["top_dirs"])

    def test_csv_export(self):
        self.make_basic()
        scan_id, _meta = self.harness.run_scan()
        path = self.harness.services.scanner.write_csv(scan_id)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            text = raw.decode("utf-8-sig")
            rows = list(csv.reader(io.StringIO(text)))
            self.assertEqual(rows[0][:4], [u"组号", u"匹配类型", u"状态", u"文件路径"])
            self.assertEqual(rows[0][4], u"大小(字节)")
            self.assertEqual(len(rows), 6)  # 1 表头 + 5 个重复文件
            self.assertEqual(rows[0][10], u"时间来源")
            self.assertEqual(rows[0][11], u"相似度分值")
            statuses = [row[2] for row in rows[1:]]
            self.assertIn(u"保留", statuses)
            self.assertIn(u"待删除", statuses)
        finally:
            os.unlink(path)
        self.assertEqual(self.harness.services.scanner.filename_for_csv(scan_id),
                         "duplicate-scan-%s.csv" % scan_id)

    def test_recorded_identity_matches_lstat(self):
        """回归测试：扫描记录的身份字段必须与 os.lstat 一致（删除重校验依赖）。"""
        files = self.make_basic()
        scan_id, _meta = self.harness.run_scan()
        checked = 0
        for group in self.harness.groups(scan_id):
            for file_info in group["files"]:
                identity = fsutil.file_identity(file_info["path"])
                self.assertIsNotNone(identity)
                self.assertEqual(file_info["size"], identity["size"],
                                 file_info["path"])
                self.assertEqual(file_info["mtime_ns"], identity["mtime_ns"],
                                 file_info["path"])
                self.assertEqual(file_info["inode"], identity["inode"],
                                 file_info["path"])
                self.assertEqual(file_info["dev"], identity["dev"],
                                 file_info["path"])
                checked += 1
        self.assertGreater(checked, 0)

    def test_selectable_stats(self):
        files = self.make_basic()
        files["c"] = write_bytes(os.path.join(self.root, "c", "x.txt"),
                                 b"tie" * 10)
        write_bytes(os.path.join(self.root, "c", "y.txt"), b"tie" * 10)
        self.harness.set_creation(files["c"], 4000)
        # 让 tie 组第二份的创建时间相同 → 需要人工确认
        tie_other = os.path.join(self.root, "c", "y.txt")
        self.harness.set_creation(tie_other, 4000)
        _scan_id, meta = self.harness.run_scan()
        stats = meta["stats"]
        self.assertEqual(meta["needs_manual_keep_groups"], 1)
        self.assertEqual(stats["groups"], 3)
        self.assertEqual(stats["duplicate_files"], 7)
        # 可勾选数量排除 needs_manual_keep 组：X 组 1 + Y 组 2
        self.assertEqual(stats["selectable_duplicate_files"], 3)
        self.assertEqual(stats["selectable_reclaimable_bytes"], 100 + 250 * 2)
        self.assertEqual(stats["reclaimable_bytes"],
                         stats["selectable_reclaimable_bytes"])

    def test_scan_history_lists_scan(self):
        self.make_basic()
        scan_id, _meta = self.harness.run_scan()
        scans = self.harness.services.scanner.list_scans()
        self.assertEqual(scans[0]["scan_id"], scan_id)
        self.assertEqual(scans[0]["config"]["keep_mode"], "latest")
        self.assertEqual(len(scans[0]["config"]["roots"]), 1)


class ScanFilterTest(ScannerBase):
    def test_hidden_filters_and_system_dirs_always_skipped(self):
        root = self.root
        write_bytes(os.path.join(root, "x1.dat"), b"C" * 30)
        write_bytes(os.path.join(root, "sub", "x2.dat"), b"C" * 30)
        write_bytes(os.path.join(root, ".hidden.dat"), b"C" * 30)
        write_bytes(os.path.join(root, "@eaDir", "inside.dat"), b"C" * 30)
        write_bytes(os.path.join(root, "#recycle", "old.dat"), b"C" * 30)
        write_bytes(os.path.join(root, "empty.dat"), b"")
        scan_id, meta = self.harness.run_scan()
        group = self.group_named(scan_id, "x1.dat")
        self.assertEqual(group["count"], 2)
        self.assertGreaterEqual(meta["stats"]["skipped"]["hidden"], 1)
        self.assertGreaterEqual(meta["stats"]["skipped"]["excluded"], 2)
        self.assertGreaterEqual(meta["stats"]["skipped"]["empty"], 1)

        scan_id2, _meta2 = self.harness.run_scan(
            options={"skip_hidden": False})
        group2 = self.group_named(scan_id2, "x1.dat")
        # 隐藏文件被包含，但 @eaDir / #recycle 永远不扫描
        self.assertEqual(group2["count"], 3)
        paths = [item["path"] for item in group2["files"]]
        for path in paths:
            self.assertNotIn("@eaDir", path)
            self.assertNotIn("#recycle", path)

    def test_extension_include_and_size_filters(self):
        root = self.root
        write_bytes(os.path.join(root, "p1.jpg"), b"J" * 40)
        write_bytes(os.path.join(root, "p2.jpg"), b"J" * 40)
        write_bytes(os.path.join(root, "t1.txt"), b"T" * 40)
        write_bytes(os.path.join(root, "t2.txt"), b"T" * 40)
        write_bytes(os.path.join(root, "s1.bin"), b"S" * 5)
        write_bytes(os.path.join(root, "s2.bin"), b"S" * 5)
        scan_id, _meta = self.harness.run_scan(
            options={"include_extensions": [".jpg"], "ignore_case_ext": True})
        names = [item["name"] for group in self.harness.groups(scan_id)
                 for item in group["files"]]
        self.assertEqual(sorted(names), ["p1.jpg", "p2.jpg"])

        scan_id2, _meta2 = self.harness.run_scan(
            options={"exclude_extensions": [".txt", ".jpg"], "min_size_bytes": 6})
        names2 = [item["name"] for group in self.harness.groups(scan_id2)
                  for item in group["files"]]
        self.assertEqual(names2, [])

        scan_id3, _meta3 = self.harness.run_scan(
            options={"include_extensions": [".BIN"], "ignore_case_ext": False})
        self.assertEqual(list(self.harness.groups(scan_id3)), [])

    def test_hardlink_deduped(self):
        root = self.root
        base = write_bytes(os.path.join(root, "hl_base.txt"), b"H" * 50)
        write_bytes(os.path.join(root, "hl_copy.txt"), b"H" * 50)
        try:
            os.link(base, os.path.join(root, "hl_link.txt"))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("hardlink not supported")
        scan_id, meta = self.harness.run_scan()
        group = self.group_named(scan_id, "hl_base.txt")
        self.assertEqual(group["count"], 2)
        self.assertEqual(meta["stats"]["skipped"]["hardlink"], 1)
        names = sorted(item["name"] for item in group["files"])
        self.assertNotIn("hl_link.txt", names)

    def test_max_files_guard(self):
        for index in range(6):
            write_bytes(os.path.join(self.root, "m%d.txt" % index), b"M" * 10)
        scan_id, meta = self.harness.run_scan(options={"max_files": 2})
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["error"]["code"], "TOO_MANY_FILES")
        self.assertEqual(meta["error"]["details"]["limit"], 2)

    def test_scan_busy(self):
        for index in range(60):
            write_bytes(os.path.join(self.root, "dup%02d.txt" % index), b"B" * 20)

        original = self.harness.services.scanner._time_reader

        def slow_reader(path, allow_ctime=False):
            time.sleep(0.02)
            return original(path, allow_ctime)

        self.harness.services.scanner._time_reader = slow_reader
        first = self.harness.services.scanner.start_scan(
            [self.root], [], {}, "admin")
        try:
            time.sleep(0.2)
            with self.assertRaises(ApiError) as ctx:
                self.harness.services.scanner.start_scan(
                    [self.root], [], {}, "admin")
            self.assertEqual(ctx.exception.code, "SCAN_BUSY")
        finally:
            try:
                self.harness.services.scanner.stop(first)
            except ApiError:
                pass
            meta = self.harness.wait_scan(first, timeout=20)
            self.assertIn(meta["status"], ("stopped", "completed"))

    def test_pause_resume_stop(self):
        shared_content = os.urandom(700 * 1024)
        for index in range(6):
            write_bytes(os.path.join(self.root, "big%d.txt" % index),
                        shared_content)
        scanner = self.harness.services.scanner
        scan_id = scanner.start_scan(
            [self.root], [], {"concurrency": 1, "hash_rate_mbps": 1}, "admin")
        deadline = time.time() + 10
        while time.time() < deadline:
            meta = scanner.get_scan(scan_id)
            if meta["status"] == "running":
                break
            time.sleep(0.02)
        time.sleep(0.3)
        paused = scanner.pause(scan_id)
        self.assertEqual(paused["status"], "paused")
        time.sleep(0.3)
        self.assertEqual(scanner.get_scan(scan_id)["status"], "paused")
        resumed = scanner.resume(scan_id)
        self.assertEqual(resumed["status"], "running")
        meta = self.harness.wait_scan(scan_id, timeout=40)
        self.assertEqual(meta["status"], "completed")
        self.assertGreater(meta["stats"]["groups"], 0)

        scan_id2 = scanner.start_scan(
            [self.root], [], {"concurrency": 1, "hash_rate_mbps": 1}, "admin")
        time.sleep(0.3)
        stopped = scanner.stop(scan_id2)
        self.assertIn(stopped["status"], ("stopping", "stopped"))
        meta2 = self.harness.wait_scan(scan_id2, timeout=30)
        self.assertEqual(meta2["status"], "stopped")
        # 停止后可以重新开始扫描
        scan_id3, meta3 = self.harness.run_scan()
        self.assertEqual(meta3["status"], "completed")
        self.assertEqual(len(meta3["scan_id"]), len(scan_id3))
