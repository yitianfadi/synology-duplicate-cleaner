# -*- coding: utf-8 -*-
"""v0044：删除白名单（保护附属文件/目录/后缀/文件名）测试。"""

import os
import time
import unittest

from tests.helpers import Harness, write_bytes

from dupcleaner import config, protection
from dupcleaner import settings as settings_module
from dupcleaner.errors import ApiError


def _options(**overrides):
    base = {
        "protect_enabled": True,
        "protect_extensions": list(config.PROTECT_DEFAULT_EXTENSIONS),
        "protect_dirs": [],
        "protect_name_regex": [],
        "protect_sidecar_files": True,
        "video_extensions": list(config.DEFAULT_VIDEO_EXTENSIONS),
    }
    base.update(overrides)
    return base


class ProtectorUnitTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root

    def tearDown(self):
        self.harness.cleanup()

    def test_extension_protection(self):
        protector = protection.Protector(_options())
        state = protector.check(os.path.join(self.root, "movie.srt"))
        self.assertIsNotNone(state)
        self.assertEqual(state["source"], "extension")
        self.assertIsNone(protector.check(os.path.join(self.root, "movie.mkv")))
        # 大小写不敏感
        state = protector.check(os.path.join(self.root, "MOVIE.NFO"))
        self.assertEqual(state["source"], "extension")

    def test_disabled(self):
        protector = protection.Protector(_options(protect_enabled=False))
        self.assertIsNone(protector.check(os.path.join(self.root, "a.srt")))

    def test_directory_protection(self):
        keep_dir = os.path.join(self.root, "keep")
        os.makedirs(keep_dir, exist_ok=True)
        protector = protection.Protector(_options(protect_dirs=[keep_dir]))
        state = protector.check(os.path.join(keep_dir, "deep", "a.mkv"))
        self.assertIsNotNone(state)
        self.assertEqual(state["source"], "directory")
        self.assertIsNone(protector.check(os.path.join(self.root, "a.mkv")))

    def test_name_regex(self):
        protector = protection.Protector(_options(protect_name_regex=[r"^keep-"]))
        self.assertEqual(protector.check(os.path.join(self.root, "keep-a.mkv"))["source"],
                         "name")
        self.assertIsNone(protector.check(os.path.join(self.root, "other.mkv")))

    def test_sidecar_detection(self):
        media_dir = os.path.join(self.root, "media")
        write_bytes(os.path.join(media_dir, "movie.mkv"), b"M" * 100)
        write_bytes(os.path.join(media_dir, "movie.srt"), b"sub")
        write_bytes(os.path.join(media_dir, "movie.en.srt"), b"sub2")
        write_bytes(os.path.join(media_dir, "movie-poster.jpg"), b"poster")
        write_bytes(os.path.join(media_dir, "poster.jpg"), b"poster")
        write_bytes(os.path.join(media_dir, "unrelated-photo.jpg"), b"photo")
        # 图片默认不在后缀保护列表里，因此只有“同源”关系会命中
        protects = {}
        ext_options = _options(protect_extensions=[])
        protector = protection.Protector(ext_options)
        for name in ("movie.srt", "movie.en.srt", "movie-poster.jpg",
                     "poster.jpg", "unrelated-photo.jpg"):
            state = protector.check(os.path.join(media_dir, name))
            protects[name] = state["source"] if state else None
        self.assertEqual(protects["movie.srt"], "sidecar")
        self.assertEqual(protects["movie.en.srt"], "sidecar")
        self.assertEqual(protects["movie-poster.jpg"], "sidecar")
        self.assertEqual(protects["poster.jpg"], "sidecar")
        self.assertIsNone(protects["unrelated-photo.jpg"])
        # 关闭附属文件保护后不再命中
        off = protection.Protector(_options(protect_extensions=[],
                                            protect_sidecar_files=False))
        self.assertIsNone(off.check(os.path.join(media_dir, "movie.srt")))

    def test_photo_library_without_video_not_protected(self):
        photo_dir = os.path.join(self.root, "photos")
        write_bytes(os.path.join(photo_dir, "IMG_0001.jpg"), b"jpg1")
        write_bytes(os.path.join(photo_dir, "IMG_0001.png"), b"png1")
        protector = protection.Protector(_options(protect_extensions=[]))
        self.assertIsNone(protector.check(os.path.join(photo_dir, "IMG_0001.jpg")))
        self.assertIsNone(protector.check(os.path.join(photo_dir, "IMG_0001.png")))

    def test_describe(self):
        protector = protection.Protector(_options())
        state = protector.check(os.path.join(self.root, "a.srt"))
        text = protector.describe(state)
        self.assertIn(u"后缀", text)


class ProtectionSettingsTest(unittest.TestCase):
    def test_defaults_and_validation(self):
        merged = settings_module.merge_settings({}, {})
        self.assertTrue(merged["protect_enabled"])
        self.assertTrue(merged["protect_sidecar_files"])
        self.assertIn(".srt", merged["protect_extensions"])
        with self.assertRaises(ApiError):
            settings_module.validate_patch({"protect_extensions": ["srt"]})
        with self.assertRaises(ApiError):
            settings_module.validate_patch({"protect_dirs": ["relative/path"]})
        with self.assertRaises(ApiError):
            settings_module.validate_patch({"protect_name_regex": ["["]})
        merged = settings_module.merge_settings({}, {
            "protect_dirs": ["/volume1/photo/keep"],
            "protect_name_regex": [r"^keep-"],
            "protect_enabled": False,
        })
        self.assertEqual(merged["protect_dirs"], ["/volume1/photo/keep"])
        self.assertFalse(merged["protect_enabled"])


class ProtectionScanTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root

    def tearDown(self):
        self.harness.cleanup()

    def test_scan_marks_protected_files(self):
        media = os.path.join(self.root, "media")
        # 两组精准重复：mkv 组（不保护）、srt 组（默认后缀保护）
        mkv_a = write_bytes(os.path.join(media, "movie.mkv"), b"M" * 4096)
        mkv_b = write_bytes(os.path.join(media, "movie2.mkv"), b"M" * 4096)
        srt_a = write_bytes(os.path.join(media, "movie.srt"), b"S" * 512)
        srt_b = write_bytes(os.path.join(media, "movie2.srt"), b"S" * 512)
        # 注入可区分的创建时间，避免 btime 相同 → 转人工组（会让可选数量断言抖动）
        self.harness.set_creation(mkv_a, 400)
        self.harness.set_creation(mkv_b, 300)
        self.harness.set_creation(srt_a, 200)
        self.harness.set_creation(srt_b, 100)
        scan_id, meta = self.harness.run_scan(options={
            "media_sidecar_mode": "include"})
        self.assertEqual(meta["status"], "completed")
        stats = meta["stats"]
        self.assertGreaterEqual(stats["protected_files"], 2)
        srt_group = self.harness.group_by_paths(scan_id, "movie.srt")
        for item in srt_group["files"]:
            self.assertTrue(item["protected"])
            self.assertEqual(item["protect_reason"], "extension")
            self.assertFalse(item["default_selected"])
        self.assertGreaterEqual(srt_group["protected_files"], 2)
        mkv_group = self.harness.group_by_paths(scan_id, "movie.mkv")
        self.assertEqual(mkv_group["protected_files"], 0)
        # 保护文件不计入“可全选删除”的数量
        self.assertEqual(stats["selectable_duplicate_files"], 1)

    def test_scan_sidecar_protection_without_extension_rule(self):
        media = os.path.join(self.root, "media")
        mkv_a = write_bytes(os.path.join(media, "movie.mkv"), b"M" * 4096)
        mkv_b = write_bytes(os.path.join(media, "movie2.mkv"), b"M" * 4096)
        srt_a = write_bytes(os.path.join(media, "movie.srt"), b"S" * 512)
        srt_b = write_bytes(os.path.join(media, "movie2.srt"), b"S" * 512)
        self.harness.set_creation(mkv_a, 400)
        self.harness.set_creation(mkv_b, 300)
        self.harness.set_creation(srt_a, 200)
        self.harness.set_creation(srt_b, 100)
        scan_id, meta = self.harness.run_scan(options={
            "media_sidecar_mode": "include",
            "protect_extensions": [],
            "protect_sidecar_files": True,
        })
        srt_group = self.harness.group_by_paths(scan_id, "movie.srt")
        for item in srt_group["files"]:
            self.assertTrue(item["protected"])
            self.assertEqual(item["protect_reason"], "sidecar")


class ProtectionDeletionTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root
        # 回收站模式计划要求共享根存在 #recycle（与 test_deletion 一致）
        os.makedirs(os.path.join(self.root, "#recycle"), exist_ok=True)
        self.deletion = self.harness.services.deletion
        self.session = self.harness.login()

    def tearDown(self):
        self.harness.cleanup()

    def _make_three(self):
        for name in ("a.txt", "b.txt", "keep.txt"):
            write_bytes(os.path.join(self.root, name), b"X" * 300)
        # a 最新 → 保留项 a；keep.txt 作为可删除副本且被白名单保护
        self.harness.set_creation(os.path.join(self.root, "a.txt"), 300)
        self.harness.set_creation(os.path.join(self.root, "b.txt"), 200)
        self.harness.set_creation(os.path.join(self.root, "keep.txt"), 100)

    def wait_job(self, scan_id, job_id, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.deletion.get_job(scan_id, job_id)
            if job and job.get("status") in ("completed", "partial", "failed", "stopped"):
                return job
            time.sleep(0.05)
        raise AssertionError("delete job did not finish")

    def test_plan_skips_protected_files(self):
        self._make_three()
        scan_id, _meta = self.harness.run_scan()
        group = self.harness.group_by_paths(scan_id, "a.txt")
        file_ids = [item["file_id"] for item in group["files"]]
        self.harness.services.settings.update({
            "protect_name_regex": [r"^keep\."]})
        plan = self.deletion.create_plan(self.session, scan_id, {
            "file_ids": file_ids, "recycle": True})
        self.assertEqual(plan["count"], 1)
        skipped = plan["skipped"]
        protected = [item for item in skipped if item["reason"] == "PROTECTED"]
        self.assertEqual(len(protected), 1)
        self.assertIn("keep.txt", protected[0]["path"])
        self.assertEqual(protected[0]["protect_source"], "name")
        self.assertGreaterEqual(int(plan.get("protected_skipped") or 0), 1)

    def test_all_protected_plan_rejected(self):
        self._make_three()
        scan_id, _meta = self.harness.run_scan()
        group = self.harness.group_by_paths(scan_id, "a.txt")
        keep_id = [item["file_id"] for item in group["files"]
                   if item["path"].endswith("keep.txt")][0]
        self.harness.services.settings.update({"protect_name_regex": [r"^keep\."]})
        with self.assertRaises(ApiError) as ctx:
            self.deletion.create_plan(self.session, scan_id, {
                "file_ids": [keep_id], "recycle": True})
        self.assertEqual(ctx.exception.code, "PLAN_INVALID")
        reasons = [item.get("reason") for item in
                   (ctx.exception.details or {}).get("skipped") or []]
        self.assertIn("PROTECTED", reasons)

    def test_execution_rechecks_protection(self):
        self._make_three()
        scan_id, _meta = self.harness.run_scan()
        group = self.harness.group_by_paths(scan_id, "a.txt")
        target = [item for item in group["files"]
                  if item["path"].endswith("b.txt")][0]
        plan = self.deletion.create_plan(self.session, scan_id, {
            "file_ids": [target["file_id"]], "recycle": True})
        self.assertEqual(plan["count"], 1)
        # 计划生成后把 b.txt 也纳入保护 → 执行时必须再次跳过
        self.harness.services.settings.update({"protect_name_regex": [r"\.txt$"]})
        job_id = self.deletion.start_delete(self.session, scan_id, {
            "plan_id": plan["plan_id"], "confirm": "DELETE"})
        job = self.wait_job(scan_id, job_id)
        # 计划内文件全部被保护跳过 → 现有语义为 partial（skipped 不为 0）
        self.assertIn(job["status"], ("completed", "partial"))
        results = job.get("results") or []
        self.assertTrue(any(item.get("reason") == "PROTECTED" for item in results))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "b.txt")))

    def test_protected_sidecar_not_deleted(self):
        media = os.path.join(self.root, "media")
        write_bytes(os.path.join(media, "movie.mkv"), b"M" * 4096)
        write_bytes(os.path.join(media, "movie2.mkv"), b"M" * 4096)
        write_bytes(os.path.join(media, "movie.srt"), b"S" * 512)
        write_bytes(os.path.join(media, "movie2.srt"), b"S" * 512)
        scan_id, _meta = self.harness.run_scan(options={
            "media_sidecar_mode": "include"})
        srt_group = self.harness.group_by_paths(scan_id, "movie.srt")
        file_ids = [item["file_id"] for item in srt_group["files"]]
        with self.assertRaises(ApiError) as ctx:
            self.deletion.create_plan(self.session, scan_id, {
                "file_ids": file_ids, "recycle": True})
        self.assertEqual(ctx.exception.code, "PLAN_INVALID")
        # 永久删除也一样被拦截（保护优先于 recycle 模式）
        with self.assertRaises(ApiError):
            self.deletion.create_plan(self.session, scan_id, {
                "file_ids": file_ids, "recycle": False})
        self.assertTrue(os.path.isfile(os.path.join(media, "movie.srt")))


if __name__ == "__main__":
    unittest.main()
