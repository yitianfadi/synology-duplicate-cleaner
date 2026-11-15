# -*- coding: utf-8 -*-
"""v0053 下载增强：去重跳过 / 自动重试 / 分时限速 / 命名模板 /
优先级 / 批量操作 / 导出 / 空间看板 / 解析缓存与 iframe / 通知。"""

from __future__ import absolute_import

import os
import time
import unittest

from tests.helpers import write_bytes
from tests.test_v0051_downloads import (FakeCdn, DownloadHarness,
                                        MEDIA_PLAYLIST, MASTER_PLAYLIST)

from dupcleaner import config
from dupcleaner import downloads as downloads_module
from dupcleaner.errors import ApiError
from dupcleaner.downloads import DownloadService


class FakeNotifier(object):
    def __init__(self):
        self.calls = []

    def notify(self, event, title, message, level="info", detail=None):
        self.calls.append({"event": event, "title": title,
                           "message": message, "level": level,
                           "detail": detail or {}})


class PageParseSpaceTest(unittest.TestCase):
    def setUp(self):
        self.cdn = FakeCdn().start()
        self.h = DownloadHarness()

    def tearDown(self):
        self.h.cleanup()
        self.cdn.stop()

    # -- 去重跳过 -------------------------------------------------------
    def test_skip_existing_same_size(self):
        payload = b"SKIP-ME-" * 512
        url = self.cdn.put("/files/clip.mp4", payload)
        final = os.path.join(self.h.downloads, "clip.mp4")
        write_bytes(final, payload)
        before = len(self.cdn.httpd.requests)
        created = self.h.service.create(
            "admin", {"urls": [url], "options": {"title": "clip"}})
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "done", job.error_message)
        self.assertTrue(job.skipped)
        self.assertEqual(job.output_path, final)
        self.assertIn(u"已存在", job.message)
        # 只发生了探测请求，没有下载整份文件（请求次数很少）
        self.assertLessEqual(len(self.cdn.httpd.requests) - before, 2)

    def test_skip_existing_mismatched_size_downloads(self):
        payload = b"MISMATCH-" * 256
        url = self.cdn.put("/files/clip2.mp4", payload)
        final = os.path.join(self.h.downloads, "clip2.mp4")
        write_bytes(final, b"old")
        created = self.h.service.create(
            "admin", {"urls": [url], "options": {"title": "clip2"}})
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "done", job.error_message)
        self.assertFalse(job.skipped)
        # 同名已存在且不覆盖 → 自动加序号
        self.assertIn(u"(2)", os.path.basename(job.output_path))

    def test_skip_existing_disabled(self):
        self.h.settings["download_skip_existing"] = False
        payload = b"NOSKIP-" * 256
        url = self.cdn.put("/files/clip3.mp4", payload)
        write_bytes(os.path.join(self.h.downloads, "clip3.mp4"), payload)
        created = self.h.service.create(
            "admin", {"urls": [url], "options": {"title": "clip3"}})
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "done", job.error_message)
        self.assertFalse(job.skipped)

    # -- 命名模板 -------------------------------------------------------
    def test_name_template_with_height(self):
        self.cdn.put("/m2/master.m3u8", MASTER_PLAYLIST, text=True)
        self.cdn.put("/m2/high/index.m3u8", MEDIA_PLAYLIST, text=True)
        self.cdn.put("/m2/low/index.m3u8", MEDIA_PLAYLIST, text=True)
        for index in range(3):
            self.cdn.put("/m2/high/seg%d.ts" % index, b"h" * 64)
            self.cdn.put("/m2/low/seg%d.ts" % index, b"l" * 64)
        self.h.settings["download_name_template"] = "{title}_{height}"
        created = self.h.service.create(
            "admin", {"urls": [self.cdn.url("/m2/master.m3u8")],
                      "options": {"title": "示例片"}})
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "done", job.error_message)
        self.assertIn(u"1080p", os.path.basename(job.output_path))
        self.assertTrue(job.output_path.endswith(".mp4"))

    def test_name_template_rejects_bad_tokens(self):
        from dupcleaner import settings as settings_module
        with self.assertRaises(ApiError):
            settings_module._normalize_value(
                "download_name_template", "{title}-{bogus}")
        with self.assertRaises(ApiError):
            settings_module._normalize_value(
                "download_name_template", "a/b")

    def test_season_episode(self):
        self.assertEqual(downloads_module._split_season_episode("Show S01E02"),
                         ("S01", "E02"))
        self.assertEqual(
            downloads_module._split_season_episode(u"剧名 第二季 第3集"),
            ("S02", "E03"))
        self.assertEqual(
            downloads_module._split_season_episode("plain title"), ("", ""))

    # -- 分时限速 -------------------------------------------------------
    def test_offpeak_limiter(self):
        class FakeTime(object):
            def __init__(self, hour, minute):
                self.tm_hour = hour
                self.tm_min = minute

        self.assertTrue(DownloadService._in_offpeak(
            "01:00", "08:00", FakeTime(3, 0)))
        self.assertFalse(DownloadService._in_offpeak(
            "01:00", "08:00", FakeTime(9, 0)))
        # 跨零点窗口
        self.assertTrue(DownloadService._in_offpeak(
            "23:00", "07:00", FakeTime(2, 30)))
        self.assertTrue(DownloadService._in_offpeak(
            "23:00", "07:00", FakeTime(23, 30)))
        self.assertFalse(DownloadService._in_offpeak(
            "23:00", "07:00", FakeTime(12, 0)))
        # 非法/空窗口不生效
        self.assertFalse(DownloadService._in_offpeak(
            "xx", "08:00", FakeTime(3, 0)))

    def test_effective_rate_limit_offpeak(self):
        self.h.settings["download_offpeak_enabled"] = True
        self.h.settings["download_offpeak_start"] = "00:00"
        self.h.settings["download_offpeak_end"] = "23:59"
        self.h.settings["download_offpeak_rate_limit_kbps"] = 64
        self.h.settings["download_rate_limit_kbps"] = 0
        url = self.cdn.put("/files/rate.bin", b"r" * 4096)
        created = self.h.service.create("admin", {"urls": [url]})
        job = self.h.service.get(created["jobs"][0]["job_id"])
        self.assertEqual(self.h.service._effective_rate_limit(job), 64 * 1024)
        # 任务选项是提交时的快照：改设置不影响已创建任务
        job.options["offpeak_enabled"] = False
        self.assertEqual(self.h.service._effective_rate_limit(job), 0)

    # -- 自动重试 -------------------------------------------------------
    def _patched_backoff(self):
        import contextlib
        service = self

        @contextlib.contextmanager
        def _ctx():
            original = downloads_module.config.DOWNLOAD_AUTO_RETRY_BACKOFF_SEC
            downloads_module.config.DOWNLOAD_AUTO_RETRY_BACKOFF_SEC = 0.2
            try:
                yield
            finally:
                downloads_module.config.DOWNLOAD_AUTO_RETRY_BACKOFF_SEC = \
                    original
        return _ctx()

    def test_auto_retry_after_server_errors(self):
        payload = b"RETRY-" * 4096
        url = self.cdn.put("/files/flaky.mp4", payload)
        self.cdn.httpd.fail_remaining = 2
        self.h.settings["download_auto_retry"] = True
        self.h.settings["download_auto_retry_max"] = 3
        self.h.settings["download_retry"] = 0
        with self._patched_backoff():
            created = self.h.service.create(
                "admin", {"urls": [url], "options": {"title": "flaky"}})
            job = self.h.wait(created["jobs"][0]["job_id"], timeout=40)
        self.assertEqual(job.status, "done", job.error_message)
        self.assertGreaterEqual(job.attempts, 3)
        self.assertEqual(os.path.getsize(job.output_path), len(payload))

    def test_auto_retry_gives_up(self):
        url = self.cdn.put("/files/down.mp4", b"x" * 4096)
        self.cdn.httpd.fail_remaining = 100
        self.h.settings["download_auto_retry"] = True
        self.h.settings["download_auto_retry_max"] = 1
        self.h.settings["download_retry"] = 0
        with self._patched_backoff():
            created = self.h.service.create("admin", {"urls": [url]})
            job = self.h.wait(created["jobs"][0]["job_id"], timeout=40)
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.attempts, 2)

    def test_deterministic_error_not_retried(self):
        self.h.settings["download_auto_retry"] = True
        self.h.settings["download_auto_retry_max"] = 3
        # 直播列表 → LIVE_UNSUPPORTED，不可重试
        self.cdn.put("/live2/index.m3u8",
                     u"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nseg.ts\n",
                     text=True)
        created = self.h.service.create(
            "admin", {"urls": [self.cdn.url("/live2/index.m3u8")]})
        job = self.h.wait(created["jobs"][0]["job_id"], timeout=40)
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.error_code, "LIVE_UNSUPPORTED")

    # -- 优先级 / 批量 / 导出 / 空间 ------------------------------------
    def test_priority_and_queue_order(self):
        self.h.settings["download_concurrency"] = 1
        urls = [self.cdn.put("/files/p%d.bin" % i, b"p" * 4096)
                for i in range(3)]
        created = self.h.service.create("admin", {"urls": urls})
        ids = [item["job_id"] for item in created["jobs"]]
        # 最后一条置顶
        self.h.service.set_priority(ids[-1], top=True)
        queued = self.h.service.queue_snapshot()["queued"]
        queued_ids = [item["job_id"] for item in queued]
        self.assertEqual(queued_ids[0], ids[-1])
        self.assertEqual(queued[0]["priority"], 1)

    def test_bulk_pause_all(self):
        self.h.settings["download_concurrency"] = 1
        urls = [self.cdn.put("/files/b%d.bin" % i, b"b" * 4096)
                for i in range(4)]
        self.h.service.create("admin", {"urls": urls})
        result = self.h.service.bulk_action("admin", "pause", "all")
        self.assertGreaterEqual(result["affected"], 1)
        counts = self.h.service.counts()
        self.assertGreaterEqual(counts.get("paused", 0), 1)
        # 恢复
        result = self.h.service.bulk_action("admin", "resume", "paused")
        self.assertGreaterEqual(result["affected"], 1)

    def test_export_rows(self):
        url = self.cdn.put("/files/e.bin", b"e" * 4096)
        created = self.h.service.create(
            "admin", {"urls": [url], "options": {"title": "e"}})
        self.h.wait(created["jobs"][0]["job_id"])
        rows = self.h.service.export_rows()
        self.assertEqual(rows[0][1], u"名称")
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(len(rows[0]), len(rows[1]))

    def test_storage_stats(self):
        stats = self.h.service.storage_stats(self.h.downloads)
        self.assertIsNotNone(stats)
        self.assertGreater(stats["total_bytes"], 0)
        self.assertGreaterEqual(stats["free_bytes"], 0)
        self.assertIn("min_free_gb", stats)
        self.assertIsNone(self.h.service.storage_stats("/nonexistent-dir-xyz"))

    # -- 解析缓存 / iframe ---------------------------------------------
    def test_parse_cache(self):
        self.cdn.put("/pc/page.html",
                     u'<html><body><video src="/pc/v.m3u8"></video></body></html>',
                     text=True)
        first = self.h.service.parse_target(
            "admin", {"url": self.cdn.url("/pc/page.html")})
        self.assertFalse(first.get("cached"))
        second = self.h.service.parse_target(
            "admin", {"url": self.cdn.url("/pc/page.html")})
        self.assertTrue(second.get("cached"))
        self.assertEqual(len(first["candidates"]), len(second["candidates"]))
        # 缓存命中后仍会发放新的登录态 token
        self.assertTrue(second.get("parse_session"))

    def test_iframe_follow(self):
        self.cdn.put("/if/inner.html",
                     u'<html><body><source src="/if/media.mp4"></body></html>',
                     text=True)
        self.cdn.put("/if/outer.html",
                     u'<html><body><iframe src="/if/inner.html"></iframe>'
                     u'</body></html>', text=True)
        result = self.h.service.parse_target(
            "admin", {"url": self.cdn.url("/if/outer.html")})
        urls = [item["url"] for item in result["candidates"]]
        self.assertIn(self.cdn.url("/if/media.mp4"), urls)
        item = [c for c in result["candidates"]
                if c["url"] == self.cdn.url("/if/media.mp4")][0]
        self.assertEqual(item["source"], "iframe")

    # -- 通知 -----------------------------------------------------------
    def test_notify_on_done_and_fail(self):
        notifier = FakeNotifier()
        harness = DownloadHarness(notifier=notifier)
        try:
            url = self.cdn.put("/files/n.bin", b"n" * 4096)
            created = harness.service.create(
                "admin", {"urls": [url], "options": {"title": "n"}})
            harness.wait(created["jobs"][0]["job_id"])
            events = [item["event"] for item in notifier.calls]
            self.assertIn("download_done", events)

            harness.settings["download_auto_retry"] = False
            bad = harness.service.create(
                "admin", {"urls": [self.cdn.url("/missing.bin")]})
            harness.wait(bad["jobs"][0]["job_id"], timeout=30)
            events = [item["event"] for item in notifier.calls]
            self.assertIn("download_failed", events)
        finally:
            harness.cleanup()

    def test_notify_events_registered(self):
        events = config.NOTIFY_EVENTS
        self.assertIn("download_done", events)
        self.assertIn("download_failed", events)
        self.assertEqual(config.NOTIFY_EVENT_LABELS["download_done"],
                         u"下载完成")


if __name__ == "__main__":
    unittest.main()
