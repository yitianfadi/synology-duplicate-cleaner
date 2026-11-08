# -*- coding: utf-8 -*-
"""v0048：字幕失败分类/自动重试/失败清单/优先级/音轨/监控/ETA 测试。"""

import json
import os
import time
import unittest

from tests.helpers import Harness, write_bytes

from dupcleaner import config, settings as settings_module, subtitles
from dupcleaner.subtitle_watch import SubtitleWatchService


def make_failed_job(service, code, message="", path=None, group_id=None):
    job = subtitles.SubtitleJob(
        subtitles.make_job_id(),
        path or os.path.join(service.config.data_dir, "movie.mp4"),
        "auto", False, "zh", "admin", group_id=group_id)
    job.status = subtitles.STATUS_FAILED
    job.error = {"code": code, "message": message}
    job.failure_category = subtitles.failure_category(code, message)
    job.retryable = subtitles.failure_retryable(job.failure_category)
    job.finished_epoch = time.time()
    job.started_epoch = job.created_epoch
    service._register_job(job, enqueue=False)
    return job


class FailureCategoryTest(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(subtitles.failure_category("NO_AUDIO"), "audio_extract")
        self.assertEqual(subtitles.failure_category("NO_SPEECH"), "no_speech")
        self.assertEqual(subtitles.failure_category("TIMEOUT"), "timeout")
        self.assertEqual(subtitles.failure_category("PROVIDER_NETWORK_ERROR"),
                         "network")
        self.assertEqual(subtitles.failure_category("PERMISSION_DENIED"),
                         "permission")
        self.assertEqual(subtitles.failure_category("AUDIO_TRACK_NOT_FOUND"),
                         "unsupported_stream")
        self.assertEqual(subtitles.failure_category("SOMETHING_NEW"), "unknown")

    def test_message_keywords(self):
        self.assertEqual(
            subtitles.failure_category("X", u"Permission denied"), "permission")
        self.assertEqual(
            subtitles.failure_category("X", u"未识别到可用字幕内容"), "no_speech")
        self.assertEqual(
            subtitles.failure_category("X", u"视频没有音轨"), "audio_extract")

    def test_retryable(self):
        for category in ("timeout", "network", "provider", "internal"):
            self.assertTrue(subtitles.failure_retryable(category))
        for category in ("no_speech", "audio_extract", "permission",
                         "unsupported_stream", "unknown"):
            self.assertFalse(subtitles.failure_retryable(category))

    def test_audio_track_normalize(self):
        self.assertIsNone(subtitles.normalize_audio_track("auto"))
        self.assertIsNone(subtitles.normalize_audio_track(""))
        self.assertEqual(subtitles.normalize_audio_track("2"), 2)
        self.assertEqual(subtitles.normalize_audio_track(0), 0)
        for bad in ("-1", "99", "a", True):
            with self.assertRaises(ValueError):
                subtitles.normalize_audio_track(bad)
        self.assertIsNone(subtitles.normalize_audio_language("auto"))
        self.assertEqual(subtitles.normalize_audio_language("jpn"), "jpn")
        with self.assertRaises(ValueError):
            subtitles.normalize_audio_language("not a language!")


class FailureRetryTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.service = self.harness.services.subtitles
        self.session = self.harness.login()

    def tearDown(self):
        self.harness.cleanup()

    def test_auto_retry_requeues_retryable(self):
        self.harness.services.settings.update({
            "subtitle_auto_retry_enabled": True,
            "subtitle_auto_retry_max": 1,
        })
        job = subtitles.SubtitleJob(
            subtitles.make_job_id(),
            os.path.join(self.harness.dev_root, "movie.mp4"),
            "auto", False, "zh", "admin")
        job.status = subtitles.STATUS_RUNNING
        self.service._register_job(job, enqueue=False)
        self.service._handle_failure(job, "TIMEOUT", u"云端请求超时")
        self.assertEqual(job.status, subtitles.STATUS_QUEUED)
        self.assertEqual(job.attempt, 1)
        self.assertEqual(job.failure_category, "timeout")
        with self.service._lock:
            self.assertIn(job.job_id, self.service._queue)

    def test_auto_retry_stops_at_max(self):
        self.harness.services.settings.update({
            "subtitle_auto_retry_enabled": True,
            "subtitle_auto_retry_max": 0,
        })
        job = subtitles.SubtitleJob(
            subtitles.make_job_id(),
            os.path.join(self.harness.dev_root, "movie.mp4"),
            "auto", False, "zh", "admin")
        job.status = subtitles.STATUS_RUNNING
        self.service._register_job(job, enqueue=False)
        self.service._handle_failure(job, "NETWORK_ERROR", u"无法连接云端服务")
        self.assertEqual(job.status, subtitles.STATUS_FAILED)
        self.assertEqual(job.failure_category, "network")
        self.assertTrue(job.retryable)

    def test_no_speech_not_retried_and_skipped(self):
        self.harness.services.settings.update({
            "subtitle_auto_retry_enabled": True,
            "subtitle_auto_retry_max": 3,
            "subtitle_no_speech_skip": True,
        })
        job = subtitles.SubtitleJob(
            subtitles.make_job_id(),
            os.path.join(self.harness.dev_root, "movie.mp4"),
            "auto", False, "zh", "admin")
        job.status = subtitles.STATUS_RUNNING
        self.service._register_job(job, enqueue=False)
        self.service._handle_failure(job, "NO_SPEECH", u"未识别到可用字幕内容")
        self.assertEqual(job.status, subtitles.STATUS_FAILED)
        self.assertTrue(job.skipped)
        self.assertEqual(job.attempt, 0)
        self.assertEqual(job.failure_category, "no_speech")
        self.assertIn(u"已跳过", job.message)

    def test_failures_list_and_export(self):
        make_failed_job(self.service, "NO_AUDIO", u"没有音轨")
        make_failed_job(self.service, "TIMEOUT", u"云端请求超时")
        data = self.service.failures(include_skipped=True)
        self.assertEqual(data["total"], 2)
        self.assertIn("audio_extract", data["categories"])
        self.assertIn("timeout", data["categories"])
        filtered = self.service.failures(category="timeout",
                                          include_skipped=True)
        self.assertEqual(filtered["total"], 1)
        rows = self.service.failures_export_rows()
        self.assertEqual(len(rows), 2)
        self.assertIn("category", rows[0])

    def test_batch_retry_skips_non_retryable_unless_force(self):
        no_audio = make_failed_job(self.service, "NO_AUDIO", u"没有音轨")
        timeout = make_failed_job(self.service, "TIMEOUT", u"超时")
        result = self.service.retry_failures("admin")
        self.assertEqual(result["requeued"], 1)
        self.assertEqual(no_audio.status, subtitles.STATUS_FAILED)
        self.assertEqual(timeout.status, subtitles.STATUS_QUEUED)
        reasons = [item.get("reason") for item in result["skipped"]]
        self.assertIn("NOT_RETRYABLE", reasons)
        # force 重试 no_audio
        result = self.service.retry_failures("admin", job_ids=[no_audio.job_id],
                                             force=True)
        self.assertEqual(result["requeued"], 1)
        self.assertEqual(no_audio.status, subtitles.STATUS_QUEUED)

    def test_priority_top(self):
        with self.service._lock:
            self.service._queue.clear()
        first = subtitles.SubtitleJob(
            subtitles.make_job_id(), os.path.join(self.harness.dev_root, "a.mp4"),
            "auto", False, "zh", "admin", priority=0)
        first.status = subtitles.STATUS_QUEUED
        self.service._register_job(first, enqueue=True)
        second = subtitles.SubtitleJob(
            subtitles.make_job_id(), os.path.join(self.harness.dev_root, "b.mp4"),
            "auto", False, "zh", "admin", priority=0)
        second.status = subtitles.STATUS_QUEUED
        self.service._register_job(second, enqueue=True)
        snapshot = self.service.set_priority(second.job_id, top=True)
        self.assertEqual(snapshot["priority"], config.SUBTITLE_PRIORITY_MANUAL_TOP)
        with self.service._lock:
            queue = list(self.service._queue)
        self.assertEqual(queue[0], second.job_id)

    def test_audio_tracks_and_resolution(self):
        class FakeProbeFFmpeg(object):
            def available(self):
                return True

            def probe_media(self, path, timeout, stop_check=None):
                return {
                    "duration": 100.0,
                    "has_audio": True,
                    "audio_streams": [
                        {"index": 1, "codec": "aac", "language": "jpn",
                         "title": "Japanese", "channels": 2, "default": True},
                        {"index": 2, "codec": "aac", "language": "chi",
                         "title": "Mandarin", "channels": 2, "default": False},
                    ],
                }

        service = self.service
        original = service.ffmpeg
        service.ffmpeg = FakeProbeFFmpeg()
        try:
            path = os.path.join(self.harness.dev_root, "movie.mp4")
            write_bytes(path, b"fake")
            tracks = service.audio_tracks(path)
            self.assertEqual(len(tracks["tracks"]), 2)
            self.assertEqual(tracks["tracks"][1]["language"], "chi")
            job = subtitles.SubtitleJob(
                subtitles.make_job_id(), path, "auto", False, "zh", "admin",
                audio_track_language="chi")
            self.assertEqual(service._resolve_audio_map(job, {
                "audio_streams": [{"language": "jpn"}, {"language": "chi"}]}),
                "0:a:1")
            job2 = subtitles.SubtitleJob(
                subtitles.make_job_id(), path, "auto", False, "zh", "admin",
                audio_track=1)
            self.assertEqual(service._resolve_audio_map(job2, {
                "audio_streams": [{"language": "jpn"}, {"language": "chi"}]}),
                "0:a:1")
            bad = subtitles.SubtitleJob(
                subtitles.make_job_id(), path, "auto", False, "zh", "admin",
                audio_track=5)
            with self.assertRaises(subtitles.SubtitleError):
                service._resolve_audio_map(bad, {
                    "audio_streams": [{"language": "jpn"}]})
        finally:
            service.ffmpeg = original

    def test_eta_uses_duration_samples(self):
        jobs = []
        base = time.time() - 10
        for index in range(4):
            job = subtitles.SubtitleJob(
                subtitles.make_job_id(),
                os.path.join(self.harness.dev_root, "m%d.mp4" % index),
                "auto", False, "zh", "admin")
            job.created_epoch = base + index
            job.finished_epoch = base + index + 2
            job.started_epoch = base + index
            job.status = subtitles.STATUS_DONE
            job.skipped = False
            jobs.append(job)
        running = subtitles.SubtitleJob(
            subtitles.make_job_id(),
            os.path.join(self.harness.dev_root, "r.mp4"),
            "auto", False, "zh", "admin")
        running.status = subtitles.STATUS_RUNNING
        running.progress = 50
        running.started_epoch = time.time() - 4
        jobs.append(running)
        for _ in range(4):
            self.service._duration_samples.append(2.0)
        eta = self.service._eta_seconds(jobs, 6)
        self.assertIsInstance(eta, int)
        self.assertGreaterEqual(eta, 0)


class SubtitleWatchTest(unittest.TestCase):
    class FakeSubtitleService(object):
        def __init__(self):
            self.payloads = []

        def _active_path_keys(self):
            return set()

        def create_batch(self, user, payload):
            self.payloads.append({"user": user, "payload": payload})
            return {"jobs": len(payload.get("paths") or [])}

    def setUp(self):
        self.harness = Harness()
        self.settings = self.harness.services.settings
        self.store = self.harness.services.store
        self.videos = os.path.join(self.harness.dev_root, "videos")
        os.makedirs(self.videos, exist_ok=True)
        write_bytes(os.path.join(self.videos, "a.mp4"), b"v1")
        write_bytes(os.path.join(self.videos, "b.mkv"), b"v2")
        write_bytes(os.path.join(self.videos, "ignore.txt"), b"x")
        self.settings.update({
            "subtitle_enabled": True,
            "subtitle_watch_enabled": True,
            "subtitle_watch_dirs": [self.videos],
            "subtitle_watch_interval_sec": 60,
        })
        self.fake = self.FakeSubtitleService()
        self.watch = SubtitleWatchService(
            self.harness.config, self.store, self.settings.get,
            subtitle_service=self.fake)

    def tearDown(self):
        self.harness.cleanup()

    def test_scan_enqueues_once_and_tracks_state(self):
        result = self.watch.scan_once()
        self.assertEqual(result["enqueued"], 2)
        self.assertEqual(len(self.fake.payloads), 1)
        payload = self.fake.payloads[0]["payload"]
        self.assertEqual(payload["user"] if "user" in payload else None, None)
        self.assertEqual(len(payload["paths"]), 2)
        self.assertTrue(payload["source_watch"])
        # 第二次扫描：状态已记录，不重复提交
        result = self.watch.scan_once()
        self.assertEqual(result["enqueued"], 0)
        self.assertEqual(len(self.fake.payloads), 1)
        status = self.watch.status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["watched_files"], 2)
        # 文件变化后重新入队
        time.sleep(0.01)
        path = os.path.join(self.videos, "a.mp4")
        with open(path, "ab") as handle:
            handle.write(b"changed")
        result = self.watch.scan_once()
        self.assertEqual(result["enqueued"], 1)
        self.assertEqual(len(self.fake.payloads), 2)

    def test_disabled_and_no_dirs(self):
        self.settings.update({"subtitle_watch_enabled": False})
        result = self.watch.scan_once()
        self.assertEqual(result["enqueued"], 0)
        self.settings.update({"subtitle_watch_enabled": True,
                              "subtitle_watch_dirs": []})
        result = self.watch.scan_once()
        self.assertTrue(result["errors"])


class SubtitleSettingsV2Test(unittest.TestCase):
    def test_defaults(self):
        defaults = settings_module.default_settings()
        self.assertTrue(defaults["subtitle_auto_retry_enabled"])
        self.assertEqual(defaults["subtitle_auto_retry_max"], 1)
        self.assertTrue(defaults["subtitle_no_speech_skip"])
        self.assertEqual(defaults["subtitle_max_load"], 0)
        self.assertEqual(defaults["subtitle_audio_track"], "")
        self.assertFalse(defaults["subtitle_watch_enabled"])
        self.assertEqual(defaults["subtitle_watch_interval_sec"], 300)

    def test_validation(self):
        patch = settings_module.validate_patch({
            "subtitle_auto_retry_enabled": False,
            "subtitle_auto_retry_max": 3,
            "subtitle_max_load": 6.5,
            "subtitle_dir_priorities": ["/volume1/media/movies"],
            "subtitle_audio_track": "2",
            "subtitle_audio_track_language": "jpn",
            "subtitle_watch_enabled": True,
            "subtitle_watch_dirs": ["/volume1/media/tv"],
            "subtitle_watch_interval_sec": 120,
        })
        self.assertFalse(patch["subtitle_auto_retry_enabled"])
        self.assertEqual(patch["subtitle_auto_retry_max"], 3)
        self.assertAlmostEqual(patch["subtitle_max_load"], 6.5)
        self.assertEqual(patch["subtitle_audio_track"], "2")
        self.assertEqual(patch["subtitle_audio_track_language"], "jpn")
        for invalid in ({"subtitle_auto_retry_max": 4},
                        {"subtitle_audio_track": "32"},
                        {"subtitle_audio_track": "abc"},
                        {"subtitle_audio_track_language": "not a lang!"},
                        {"subtitle_watch_interval_sec": 30},
                        {"subtitle_max_load": -1}):
            with self.assertRaises(Exception):
                settings_module.validate_patch(invalid)


if __name__ == "__main__":
    unittest.main()
