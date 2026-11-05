# -*- coding: utf-8 -*-
"""v0028 目录批量字幕测试：枚举 / 批次 / 同目录落盘 / 只翻译（fake 云服务）。"""

import os
import time
import unittest
from unittest import mock

from tests.helpers import WebServer, write_text
from tests.test_subtitles import SubtitleTestBase
from tests.test_video import make_video

from dupcleaner import settings as settings_module, subtitles
from dupcleaner.errors import ApiError


class SubtitleBatchTest(SubtitleTestBase):
    """枚举目录、批量任务、写回视频同目录、已有 srt 只翻译。"""

    def _wait_batch(self, batch_id, timeout=40.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            batch = self.service.get_batch(batch_id)
            if batch["status"] in ("done", "failed", "cancelled"):
                return batch
            time.sleep(0.02)
        raise AssertionError("subtitle batch did not finish")

    def test_enumerate_directory(self):
        root = self.root
        os.makedirs(os.path.join(root, "sub"), exist_ok=True)
        os.makedirs(os.path.join(root, "@eaDir"), exist_ok=True)
        video_a = make_video(os.path.join(root, "a.mp4"), seed=3, duration=60)
        video_b = make_video(os.path.join(root, "sub", "b.MKV"), seed=4,
                             duration=60)
        make_video(os.path.join(root, "sub", "note.txt"), seed=5, duration=60)
        make_video(os.path.join(root, ".hidden.mp4"), seed=6, duration=60)
        make_video(os.path.join(root, "@eaDir", "c.mp4"), seed=7, duration=60)
        write_text(os.path.join(root, "a.srt"),
                   "1\n00:00:01,000 --> 00:00:02,000\nold\n")

        result = self.service.enumerate_videos(root, recursive=True)
        paths = [item["path"] for item in result["items"]]
        self.assertIn(video_a, paths)
        self.assertIn(video_b, paths)
        self.assertEqual(result["count"], 2)
        self.assertGreaterEqual(result["skipped"]["system"], 1)
        self.assertGreaterEqual(result["skipped"]["hidden"], 1)
        self.assertGreaterEqual(result["skipped"]["extension"], 1)
        item_a = [item for item in result["items"]
                  if item["path"] == video_a][0]
        self.assertTrue(item_a["has_source_srt"])
        self.assertIn("a.srt", item_a["existing_subtitles"])

        # 非递归：只返回顶层视频
        flat = self.service.enumerate_videos(root, recursive=False)
        self.assertEqual([item["path"] for item in flat["items"]], [video_a])

        # 截断：max_items=1 时 truncated=True 且只返回 1 条
        truncated = self.service.enumerate_videos(root, max_items=1)
        self.assertTrue(truncated["truncated"])
        self.assertEqual(truncated["count"], 1)

        # 目录不存在 → INVALID_DIRECTORY
        with self.assertRaises(ApiError) as ctx:
            self.service.enumerate_videos(os.path.join(root, "missing"))
        self.assertEqual(ctx.exception.code, "INVALID_DIRECTORY")
        # 共享文件夹之外 → 拒绝
        with self.assertRaises(ApiError):
            self.service.enumerate_videos(self.harness.tmpdir, recursive=False)

    def test_batch_creates_adjacent_srt(self):
        video_a = self.make_video(name="a.mp4", duration=60)
        video_b = self.make_video(name="b.mp4", duration=60)
        batch = self.service.create_batch("admin", {
            "paths": [video_a, video_b, video_a],
            "translate": False, "confirm_cloud": True})
        self.assertEqual(batch["total"], 2)
        self.assertEqual(len(batch["jobs"]), 2)
        batch_id = batch["batch_id"]
        finished = self._wait_batch(batch_id)
        self.assertEqual(finished["counts"]["done"], 2)
        self.assertEqual(finished["status"], "done")

        # 字幕写回视频同目录（只写 srt，不写 vtt）
        self.assertTrue(os.path.isfile(os.path.join(self.root, "a.srt")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "b.srt")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "a.vtt")))

        job = self.service.get(batch["jobs"][0]["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["batch_id"], batch_id)
        self.assertEqual(job["source"], "asr")
        self.assertEqual(job["output_mode"], "alongside_video")
        kinds = {item["kind"]: item for item in job["outputs"]}
        self.assertIn("original", kinds)
        self.assertTrue(kinds["original"]["path"].endswith("a.srt"))
        self.assertTrue(os.path.isfile(kinds["original"]["path"]))
        self.assertEqual(len(self.service.list(batch_id=batch_id)), 2)

    def test_batch_partial_rejection_and_empty(self):
        video = self.make_video(name="a.mp4", duration=60)
        bad = os.path.join(self.root, "note.txt")
        write_text(bad, "not a video")
        missing = os.path.join(self.root, "missing.mp4")
        batch = self.service.create_batch("admin", {
            "paths": [video, bad, missing], "confirm_cloud": True})
        self.assertEqual(len(batch["jobs"]), 1)
        self.assertEqual(len(batch["rejected"]), 2)
        codes = {item["code"] for item in batch["rejected"]}
        self.assertIn("INVALID_VIDEO", codes)

        with self.assertRaises(ApiError) as ctx:
            self.service.create_batch("admin", {
                "paths": [bad], "confirm_cloud": True})
        self.assertEqual(ctx.exception.code, "BATCH_EMPTY")

        with self.assertRaises(subtitles.SubtitleError) as ctx:
            self.service.create_batch("admin", {
                "paths": [video], "confirm_cloud": False})
        self.assertEqual(ctx.exception.code, "CLOUD_CONFIRM_REQUIRED")

        with self.assertRaises(ApiError) as ctx:
            self.service.create_batch("admin", {
                "paths": [video] * 501, "confirm_cloud": True})
        self.assertEqual(ctx.exception.code, "BATCH_TOO_LARGE")

    def test_skip_existing_does_not_call_asr(self):
        video = self.make_video(name="a.mp4", duration=60)
        source = os.path.join(self.root, "a.srt")
        original_text = "1\n00:00:01,000 --> 00:00:02,000\nold\n"
        write_text(source, original_text)
        before = self.cloud.httpd.asr_calls
        job = self.submit(video)
        self.assertEqual(job["status"], "done")
        self.assertTrue(job["skipped"])
        self.assertEqual(self.cloud.httpd.asr_calls, before)
        self.assertEqual(job["outputs"], [])

        # 显式 skip_existing=False 时正常生成，但不覆盖已有 srt
        job2 = self.submit(video, skip_existing=False)
        done = self.wait_job(job2["job_id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertFalse(done["skipped"])
        self.assertEqual(done["outputs"], [])
        with open(source, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original_text)

    def test_translate_existing_srt_only(self):
        video = self.make_video(name="a.mp4", duration=60)
        source = os.path.join(self.root, "a.srt")
        original_text = "1\n00:00:01,000 --> 00:00:03,000\nHello world\n"
        write_text(source, original_text)
        before_asr = self.cloud.httpd.asr_calls
        # existing_srt 分支不启动 FFmpeg：把 available() 置 False，
        # 若被调用会得到 FFMPEG_UNAVAILABLE 失败。
        with mock.patch.object(self.service.ffmpeg, "available",
                               return_value=False):
            job = self.submit(video, source="existing_srt", translate=True,
                              target_lang="zh")
            done = self.wait_job(job["job_id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual(self.cloud.httpd.asr_calls, before_asr)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "a.zh.srt")))
        self.assertTrue(os.path.isfile(
            os.path.join(self.root, "a.bilingual.zh.srt")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "a.vtt")))
        with open(source, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original_text)
        kinds = {item["kind"] for item in done["outputs"]}
        self.assertEqual(kinds, {"translation", "bilingual"})

        # 没有源 srt → 提交时拒绝
        other = self.make_video(name="b.mp4", duration=60)
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            self.submit(other, source="existing_srt", translate=True)
        self.assertEqual(ctx.exception.code, "SOURCE_SRT_MISSING")

    def test_output_not_writable_keeps_job_downloadable(self):
        video = self.make_video(name="a.mp4", duration=60)
        with mock.patch.object(subtitles.fsutil, "writable_dir",
                               return_value=False):
            job = self.submit(video)
            done = self.wait_job(job["job_id"])
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["warning"], "OUTPUT_NOT_WRITABLE")
        self.assertEqual(done["outputs"], [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "a.srt")))
        self.assertTrue(done["files"])

    def test_batch_controls_and_unknown(self):
        with self.assertRaises(ApiError) as ctx:
            self.service.get_batch("bat-20260101-000000-abcdef")
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

        video = self.make_video(name="a.mp4", duration=60)
        batch = self.service.create_batch("admin", {
            "paths": [video], "confirm_cloud": True})
        batch_id = batch["batch_id"]
        self._wait_batch(batch_id)
        paused = self.service.pause_batch(batch_id)
        self.assertEqual(paused["counts"]["done"], 1)
        resumed = self.service.resume_batch(batch_id)
        self.assertEqual(resumed["counts"]["done"], 1)
        cancelled = self.service.cancel_batch(batch_id)
        self.assertEqual(cancelled["counts"]["done"], 1)
        retried = self.service.retry_batch(batch_id)
        self.assertEqual(retried["counts"]["done"], 1)

    def test_batch_settings_validation(self):
        with self.assertRaises(ApiError):
            settings_module.validate_patch(
                {"subtitle_output_mode": "custom_dir"})
        with self.assertRaises(ApiError):
            settings_module.validate_patch({"subtitle_job_limit": 19})
        validated = settings_module.validate_patch({
            "subtitle_output_mode": "download_only",
            "subtitle_skip_existing": False,
            "subtitle_job_limit": 50,
        })
        self.assertEqual(validated["subtitle_output_mode"], "download_only")
        self.assertFalse(validated["subtitle_skip_existing"])
        self.assertEqual(validated["subtitle_job_limit"], 50)


class SubtitleBatchApiTest(SubtitleTestBase):
    def setUp(self):
        SubtitleTestBase.setUp(self)
        self.web = WebServer(self.harness).start()
        self.client = self.web.client
        self.csrf = self.client.login()

    def tearDown(self):
        try:
            self.web.stop()
        finally:
            SubtitleTestBase.tearDown(self)

    def test_enumerate_batch_and_batch_get(self):
        video = self.make_video(name="a.mp4", duration=60)
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles/enumerate", {"path": self.root},
            csrf=self.csrf)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["path"], video)

        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles/batch", {
                "paths": [video], "translate": False,
                "confirm_cloud": True}, csrf=self.csrf)
        self.assertEqual(status, 202, payload)
        batch = payload["batch"]
        batch_id = batch["batch_id"]
        self.assertEqual(batch["total"], 1)

        status, _headers, payload = self.client.json(
            "GET", "/api/subtitles/batches/%s" % batch_id)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["batch"]["batch_id"], batch_id)

        status, _headers, payload = self.client.json(
            "GET", "/api/subtitles?batch_id=%s" % batch_id)
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(payload["jobs"]), 1)

        for action in ("pause", "resume", "cancel"):
            status, _headers, payload = self.client.json(
                "POST", "/api/subtitles/batches/%s/%s" % (batch_id, action),
                csrf=self.csrf)
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["batch"]["batch_id"], batch_id)

        # CSRF 缺失 → 403
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles/batch",
            {"paths": [video], "confirm_cloud": True})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "CSRF_FAILED")

    def test_enumerate_validation_errors(self):
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles/enumerate", {}, csrf=self.csrf)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")

        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles/enumerate",
            {"path": os.path.join(self.root, "missing")}, csrf=self.csrf)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "INVALID_DIRECTORY")

        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles/enumerate",
            {"path": self.root, "max_items": 999999}, csrf=self.csrf)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "ENUMERATE_LIMIT")


if __name__ == "__main__":
    unittest.main()
