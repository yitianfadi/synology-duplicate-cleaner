# -*- coding: utf-8 -*-
"""v0067：下载失败自动重试的指数退避（一次比一次长）。

覆盖：退避序列/封顶/设置覆盖/抖动单调、Retry-After、
错误分类（4xx 不重试）、手动重试复位、排期字段与持久化。
"""

from __future__ import absolute_import

import email.utils
import os
import time
import unittest

from tests.test_v0051_downloads import DownloadHarness, FakeCdn

from dupcleaner import config
from dupcleaner import downloads as downloads_module
from dupcleaner.downloads import DownloadError, DownloadJob


class _BackoffBase(unittest.TestCase):
    def setUp(self):
        self.h = DownloadHarness()
        self._jitter = config.DOWNLOAD_AUTO_RETRY_JITTER
        downloads_module.config.DOWNLOAD_AUTO_RETRY_JITTER = 0

    def tearDown(self):
        downloads_module.config.DOWNLOAD_AUTO_RETRY_JITTER = self._jitter
        self.h.cleanup()

    def delay(self, attempts, retry_after=0):
        return self.h.service._auto_retry_delay(attempts, retry_after)


class BackoffSequenceTest(_BackoffBase):
    def test_default_schedule_is_exponential(self):
        seq = [self.delay(n) for n in range(1, 6)]
        self.assertEqual(seq, [60.0, 180.0, 540.0, 1620.0, 1800.0])

    def test_schedule_reads_current_settings(self):
        self.h.settings["download_auto_retry_base_sec"] = 10
        self.h.settings["download_auto_retry_factor"] = 2
        self.h.settings["download_auto_retry_cap_sec"] = 25   # 低于下限 → 夹到 60
        self.assertEqual([self.delay(n) for n in range(1, 6)],
                         [10.0, 20.0, 40.0, 60.0, 60.0])

    def test_setting_out_of_range_falls_back(self):
        self.h.settings["download_auto_retry_base_sec"] = 0
        self.h.settings["download_auto_retry_factor"] = 0
        self.h.settings["download_auto_retry_cap_sec"] = 0
        self.assertEqual(self.delay(1), 60.0)

    def test_retry_after_wins_but_is_capped(self):
        self.assertEqual(self.delay(1, 300), 300.0)
        self.assertEqual(self.delay(1, 5000), 1800.0)
        self.assertEqual(self.delay(3, 30), 540.0)   # 小于指数值时不用它

    def test_jitter_keeps_growing(self):
        downloads_module.config.DOWNLOAD_AUTO_RETRY_JITTER = 0.2
        seq = [self.delay(n) for n in range(1, 5)]
        for index in range(1, len(seq)):
            self.assertGreater(seq[index], seq[index - 1], seq)
        self.assertLessEqual(seq[3], 1800.0 * 1.2)


class RetryAfterParseTest(_BackoffBase):
    def test_parse_seconds_and_date(self):
        self.assertEqual(downloads_module._parse_retry_after("5"), 5)
        self.assertEqual(downloads_module._parse_retry_after(""), 0)
        self.assertEqual(downloads_module._parse_retry_after("later"), 0)
        when = email.utils.formatdate(time.time() + 30, usegmt=True)
        value = downloads_module._parse_retry_after(when)
        self.assertTrue(25 <= value <= 35, value)


class RetryClassificationTest(_BackoffBase):
    def _job(self, **options):
        base = {"auto_retry": True, "auto_retry_max": 2}
        base.update(options)
        return DownloadJob("dl-test-1", "http://example.com/a.mp4",
                           options=base)

    def _schedule(self, exc):
        job = self._job()
        job.attempts = 1
        return job, self.h.service._schedule_retry(job, exc)

    def test_network_and_server_errors_retried(self):
        for exc in (DownloadError("HTTP_STATUS", u"网络请求失败", {}),
                    DownloadError("HTTP_STATUS", u"HTTP 503", {"status": 503}),
                    DownloadError("HTTP_STATUS", u"HTTP 429", {"status": 429}),
                    DownloadError("HTTP_STATUS", u"HTTP 408", {"status": 408}),
                    DownloadError("SEGMENT_FAILED", u"分片失败"),
                    DownloadError("INTERNAL_ERROR", u"内部错误")):
            job, ok = self._schedule(exc)
            self.assertTrue(ok, exc.code)
            self.assertEqual(job.status, "queued")

    def test_deterministic_errors_not_retried(self):
        for exc in (DownloadError("HTTP_STATUS", u"HTTP 404", {"status": 404}),
                    DownloadError("HTTP_STATUS", u"HTTP 403", {"status": 403}),
                    DownloadError("HTTP_STATUS", u"HTTP 401", {"status": 401}),
                    DownloadError("PLAYLIST_INVALID", u"播放列表无效"),
                    DownloadError("OUTPUT_DIR_INVALID", u"目录不可写"),
                    DownloadError("INSUFFICIENT_SPACE", u"空间不足"),
                    DownloadError("LIVE_UNSUPPORTED", u"不支持直播"),
                    DownloadError("DRM_UNSUPPORTED", u"DRM"),
                    DownloadError("FFMPEG_UNAVAILABLE", u"没有 FFmpeg")):
            job, ok = self._schedule(exc)
            self.assertFalse(ok, exc.code)
            self.assertEqual(job.status, "queued")  # 未被改动

    def test_retry_disabled_by_option(self):
        job = self._job(auto_retry=False)
        job.attempts = 1
        self.assertFalse(self.h.service._schedule_retry(
            job, DownloadError("HTTP_STATUS", u"HTTP 503", {"status": 503})))

    def test_attempts_limit(self):
        job = self._job(auto_retry_max=2)
        job.attempts = 3          # 已执行 3 次 > 上限 2
        self.assertFalse(self.h.service._schedule_retry(
            job, DownloadError("HTTP_STATUS", u"HTTP 503", {"status": 503})))

    def test_snapshot_exposes_retry_schedule(self):
        job, ok = self._schedule(
            DownloadError("HTTP_STATUS", u"HTTP 503", {"status": 503}))
        self.assertTrue(ok)
        snap = job.snapshot()
        self.assertEqual(snap["status"], "queued")
        self.assertTrue(snap["auto_retry"])
        self.assertEqual(snap["retry_max"], 2)
        self.assertEqual(snap["attempts"], 1)
        self.assertIsNotNone(snap["retry_at"])
        self.assertTrue(0 < snap["retry_in_sec"] <= 61, snap["retry_in_sec"])
        self.assertIn(u"自动重试", job.message)

    def test_retry_schedule_survives_record_roundtrip(self):
        job, _ok = self._schedule(
            DownloadError("HTTP_STATUS", u"HTTP 503", {"status": 503}))
        clone = DownloadJob.from_record(job.to_record())
        self.assertEqual(clone.attempts, 1)
        self.assertAlmostEqual(clone.retry_at, job.retry_at, delta=1.0)
        self.assertEqual(clone.options.get("auto_retry_max"), 2)


class RetryIntegrationTest(_BackoffBase):
    def _patched(self, base=0.2, factor=2, cap=1.0):
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            keys = ("DOWNLOAD_AUTO_RETRY_BASE_SEC", "DOWNLOAD_AUTO_RETRY_FACTOR",
                    "DOWNLOAD_AUTO_RETRY_CAP_SEC")
            originals = {key: getattr(downloads_module.config, key)
                         for key in keys}
            downloads_module.config.DOWNLOAD_AUTO_RETRY_BASE_SEC = base
            downloads_module.config.DOWNLOAD_AUTO_RETRY_FACTOR = factor
            downloads_module.config.DOWNLOAD_AUTO_RETRY_CAP_SEC = cap
            try:
                yield
            finally:
                for key, value in originals.items():
                    setattr(downloads_module.config, key, value)
        return _ctx()

    def test_http_404_fails_without_retry(self):
        cdn = FakeCdn().start()
        try:
            url = cdn.url("/files/absent-v0067.mp4")
            self.h.settings["download_auto_retry"] = True
            self.h.settings["download_auto_retry_max"] = 3
            with self._patched():
                created = self.h.service.create("admin", {"urls": [url]})
                job = self.h.wait(created["jobs"][0]["job_id"], timeout=30)
            self.assertEqual(job.status, "failed")
            self.assertEqual(job.attempts, 1)
            self.assertEqual(job.error_code, "HTTP_STATUS")
        finally:
            cdn.stop()

    def test_500_retried_then_done(self):
        cdn = FakeCdn().start()
        try:
            payload = b"RETRY67-" * 2048
            url = cdn.put("/files/flaky67.mp4", payload)
            cdn.httpd.fail_remaining = 1
            self.h.settings["download_auto_retry"] = True
            self.h.settings["download_auto_retry_max"] = 2
            self.h.settings["download_retry"] = 0
            with self._patched():
                created = self.h.service.create(
                    "admin", {"urls": [url], "options": {"title": "flaky67"}})
                job = self.h.wait(created["jobs"][0]["job_id"], timeout=40)
            self.assertEqual(job.status, "done", job.error_message)
            self.assertEqual(job.attempts, 2)
            self.assertEqual(os.path.getsize(job.output_path), len(payload))
        finally:
            cdn.stop()

    def test_manual_retry_resets_attempts(self):
        cdn = FakeCdn().start()
        try:
            url = cdn.put("/files/reset67.mp4", b"R" * 8192)
            cdn.httpd.fail_remaining = 10
            self.h.settings["download_auto_retry"] = True
            self.h.settings["download_auto_retry_max"] = 1
            self.h.settings["download_retry"] = 0
            with self._patched():
                created = self.h.service.create("admin", {"urls": [url]})
                job_id = created["jobs"][0]["job_id"]
                job = self.h.wait(job_id, timeout=40)
            self.assertEqual(job.status, "failed")
            self.assertEqual(job.attempts, 2)

            snap = self.h.service.retry(job_id)
            self.assertEqual(snap["status"], "queued")
            self.assertEqual(snap["attempts"], 0)
            self.assertEqual(snap["retry_in_sec"], 0)

            cdn.httpd.fail_remaining = 0
            with self._patched():
                job2 = self.h.wait(job_id, timeout=40)
            self.assertEqual(job2.status, "done", job2.error_message)
            self.assertEqual(job2.attempts, 1)
        finally:
            cdn.stop()


if __name__ == "__main__":
    unittest.main()
