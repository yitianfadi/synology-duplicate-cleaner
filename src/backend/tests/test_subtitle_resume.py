# -*- coding: utf-8 -*-
"""v0019 plan C：字幕任务历史/暂停继续/分片缓存测试。

覆盖：list 历史、pause 在分片边界生效、resume 跳过已完成分片（不重复上传）、
进程重启后有分片缓存的 running/translating 任务转 paused 可继续、无分片任务
仍 SERVICE_RESTART failed、翻译暂停重跑但 ASR 不重复、API 鉴权与 pause/resume。
"""

from __future__ import absolute_import

import json
import os
import time
import unittest
from unittest import mock

from tests.helpers import Harness, WebServer, write_text
from tests.test_subtitles import FAKE_FFMPEG, SubtitleTestBase
from tests.test_video import make_video

from dupcleaner import subtitles, video_similarity


def _wait_until(predicate, timeout=20.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _wait_status(service, job_id, statuses, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.get(job_id)
        if job.get("status") in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError("job %s did not reach %r" % (job_id, statuses))


class SubtitleResumeTest(SubtitleTestBase):
    def _slow_transcribe(self, delay=0.15):
        real = self.service.client.transcribe

        def wrapper(*args, **kwargs):
            time.sleep(delay)
            return real(*args, **kwargs)

        self.service.client.transcribe = wrapper
        return real

    def _submit_long(self, duration=1800.0, chunk_sec=600, translate=False,
                     **overrides):
        self._configure_settings(subtitle_asr_chunk_sec=chunk_sec,
                                 **overrides)
        video = self.make_video(name="long-%d.mp4" % int(time.time() * 1000),
                                duration=duration)
        payload = {"path": video, "language": "auto",
                   "translate": bool(translate), "confirm_cloud": True}
        if translate:
            payload["target_lang"] = "zh"
        return self.service.submit("admin", payload)["job_id"]

    def _pause_after_first_chunk(self, job_id, timeout=30.0):
        self.assertTrue(_wait_until(
            lambda: int(self.service.get(job_id).get("chunk_index") or 0) >= 1,
            timeout), "first chunk did not finish")
        paused = self.service.pause(job_id)
        self.assertEqual(paused["status"], "paused")
        _wait_status(self.service, job_id, ("paused",), timeout=30.0)
        self.assertTrue(_wait_until(
            lambda: not self.service._active_ids, 20.0),
            "worker still active after pause")
        # 暂停边界落盘后再取一次快照（chunk_index 已包含刚完成的分片）
        return self.service.get(job_id)

    def test_pause_resume_skips_completed_chunks(self):
        self._slow_transcribe(0.2)
        job_id = self._submit_long(duration=3600.0, chunk_sec=600)
        job = self._pause_after_first_chunk(job_id)
        self.assertTrue(job["paused"])
        self.assertTrue(job["resumable"])
        chunks = self.service.store.load_subtitle_chunks(job_id)
        self.assertEqual(len(chunks), 6)
        done = [item for item in chunks if item["status"] == "done"]
        self.assertGreaterEqual(len(done), 1)
        self.assertLess(len(done), 6)
        self.assertGreaterEqual(job["chunk_index"], len(done))
        self.assertEqual(job["chunk_total"], 6)
        calls_at_pause = self.cloud.httpd.asr_calls
        self.assertEqual(calls_at_pause, len(done))
        resumed = self.service.resume(job_id)
        self.assertIn(resumed["status"], ("queued", "running"))
        final = _wait_status(self.service, job_id, ("done", "failed"), 60.0)
        self.assertEqual(final["status"], "done")
        # 每个分片只上传一次：总数等于分片数，已完成分片没有重复上传
        self.assertEqual(self.cloud.httpd.asr_calls, 6)
        self.assertEqual(final["chunk_index"], 6)
        self.assertFalse(final["resumable"])
        chunks = self.service.store.load_subtitle_chunks(job_id)
        self.assertTrue(all(item["status"] == "done" for item in chunks))
        cues = []
        for item in chunks:
            cues.extend(item["cues"])
        self.assertTrue(cues)
        self.assertTrue(self.service.get_file(job_id, "original.srt"))
        text = self._job_file_text(job_id, "original.srt")
        self.assertIn("chunk-cue-1", text)

    def _job_file_text(self, job_id, name):
        path = os.path.join(self.service._job_dir(job_id), name)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_restart_marks_cached_job_paused_and_resume_no_duplicate_asr(self):
        self._slow_transcribe(0.2)
        job_id = self._submit_long(duration=3600.0, chunk_sec=600)
        paused = self._pause_after_first_chunk(job_id)
        done_before = sum(1 for item in
                          self.service.store.load_subtitle_chunks(job_id)
                          if item["status"] == "done")
        calls_before = self.cloud.httpd.asr_calls
        self.assertGreaterEqual(done_before, 1)

        reloaded = subtitles.SubtitleService(
            self.harness.config, self.harness.services.store,
            self.harness.services.shares,
            self.harness.services.settings.get,
            client=subtitles.OpenAIClient(retry_delay=0.01),
            ffmpeg=video_similarity.FFmpegTool(cmd=self.ffmpeg_cmd))
        self.addCleanup(reloaded.shutdown, 2)
        restored = reloaded.get(job_id)
        self.assertEqual(restored["status"], "paused")
        self.assertTrue(restored["resumable"])
        self.assertEqual(restored["chunk_total"], 6)
        self.assertEqual(restored["chunk_index"], done_before)
        resumed = reloaded.resume(job_id)
        self.assertIn(resumed["status"], ("queued", "running"))
        final = _wait_status(reloaded, job_id, ("done", "failed"), 60.0)
        self.assertEqual(final["status"], "done")
        # 缓存分片不重复上传
        self.assertEqual(self.cloud.httpd.asr_calls, 6)
        self.assertGreaterEqual(self.cloud.httpd.asr_calls, calls_before)

    def test_restart_without_chunks_keeps_service_restart_failure(self):
        video = self.make_video(name="stale.mp4", duration=120.0)
        job_id = "sub-20260101-000000-abcdef"
        record = {
            "job_id": job_id,
            "status": "running",
            "stage": "asr",
            "progress": 30,
            "message": "识别中",
            "path": video,
            "language": "auto",
            "translate": False,
            "target_lang": "zh",
            "user": "admin",
            "created_epoch": time.time(),
            "updated_epoch": time.time(),
            "error": None,
            "files": [],
            "translation_partial": False,
            "chunk_index": 0,
            "chunk_total": 0,
            "resumable": False,
        }
        self.harness.services.store.save_subtitle_job(job_id, record)
        reloaded = subtitles.SubtitleService(
            self.harness.config, self.harness.services.store,
            self.harness.services.shares,
            self.harness.services.settings.get,
            client=subtitles.OpenAIClient(retry_delay=0.01),
            ffmpeg=video_similarity.FFmpegTool(cmd=self.ffmpeg_cmd))
        self.addCleanup(reloaded.shutdown, 2)
        job = reloaded.get(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "SERVICE_RESTART")
        self.assertFalse(job["resumable"])
        with self.assertRaises(Exception) as ctx:
            reloaded.resume(job_id)
        self.assertEqual(getattr(ctx.exception, "code", None),
                         "INVALID_ARGUMENT")

    def _reloaded_service(self):
        reloaded = subtitles.SubtitleService(
            self.harness.config, self.harness.services.store,
            self.harness.services.shares,
            self.harness.services.settings.get,
            client=subtitles.OpenAIClient(retry_delay=0.01),
            ffmpeg=video_similarity.FFmpegTool(cmd=self.ffmpeg_cmd))
        self.addCleanup(reloaded.shutdown, 2)
        return reloaded

    def _save_queued_job(self, job_id, video):
        now = time.time()
        record = {
            "job_id": job_id,
            "status": "queued",
            "stage": "audio",
            "progress": 0,
            "message": u"排队等待",
            "path": video,
            "language": "auto",
            "translate": False,
            "target_lang": "",
            "user": "admin",
            "created_epoch": now,
            "updated_epoch": now,
            "error": None,
            "files": [],
            "translation_partial": False,
            "chunk_index": 0,
            "chunk_total": 0,
            "resumable": False,
        }
        self.harness.services.store.save_subtitle_job(job_id, record)
        return record

    def test_restart_keeps_queued_job_and_auto_starts_worker(self):
        """v0034 P0：重启时 queued 不判失败，保留状态并自动拉起 worker。"""
        video = self.make_video(name="queued-mock.mp4", duration=60.0)
        job_id = "sub-20260101-000000-abcdef"
        self._save_queued_job(job_id, video)
        seen = []

        def fake_process(service_self, job):
            seen.append(job.job_id)

        with mock.patch.object(subtitles.SubtitleService, "_process",
                               fake_process):
            reloaded = self._reloaded_service()
            self.assertTrue(_wait_until(lambda: seen, 5.0),
                            "queued job was not auto-enqueued/started")
        job = reloaded.get(job_id)
        self.assertEqual(job["status"], "queued")
        self.assertIsNone(job["error"])
        stored = self.harness.services.store.load_subtitle_job(job_id)
        self.assertEqual(stored["status"], "queued")
        self.assertNotEqual((stored.get("error") or {}).get("code"),
                            "SERVICE_RESTART")

    def test_restart_queued_job_runs_to_done(self):
        """v0034 P0：重启后排队任务自动续跑并产出字幕文件。"""
        video = self.make_video(name="queued-run.mp4", duration=60.0)
        job_id = "sub-20260101-000001-abcdef"
        self._save_queued_job(job_id, video)
        reloaded = self._reloaded_service()
        final = _wait_status(reloaded, job_id, ("done", "failed", "cancelled"),
                             60.0)
        self.assertEqual(final["status"], "done", final.get("error"))
        self.assertIsNone(final["error"])
        self.assertTrue(reloaded.get_file(job_id, "original.srt"))
        self.assertEqual(list(reloaded._queue), [])

    def test_pause_during_translation_keeps_asr_chunks(self):
        self._configure_settings(subtitle_translate_batch_size=1)
        real_translate = self.service.client.translate

        def slow_translate(*args, **kwargs):
            time.sleep(0.2)
            return real_translate(*args, **kwargs)

        self.service.client.translate = slow_translate
        self._slow_transcribe(0.01)
        job_id = self._submit_long(duration=120.0, chunk_sec=600,
                                   translate=True)
        self.assertTrue(_wait_until(
            lambda: self.service.get(job_id).get("status") == "translating",
            20.0), "job did not reach translating")
        self.service.pause(job_id)
        paused = _wait_status(self.service, job_id, ("paused",), 30.0)
        self.assertTrue(paused["resumable"])
        self.assertEqual(self.cloud.httpd.asr_calls, 1)
        calls_at_pause = self.cloud.httpd.asr_calls
        llm_at_pause = self.cloud.httpd.llm_calls
        self.service.resume(job_id)
        final = _wait_status(self.service, job_id, ("done", "failed"), 60.0)
        self.assertEqual(final["status"], "done")
        self.assertEqual(self.cloud.httpd.asr_calls, calls_at_pause)
        self.assertGreater(self.cloud.httpd.llm_calls, llm_at_pause)
        self.assertTrue(self.service.get_file(job_id, "zh.srt"))

    def test_snapshot_chunk_fields_and_history_list(self):
        job_id = self._submit_long(duration=120.0, chunk_sec=600)
        final = _wait_status(self.service, job_id, ("done", "failed"), 60.0)
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["chunk_total"], 1)
        self.assertEqual(final["chunk_index"], 1)
        self.assertIn("paused", final)
        self.assertIn("resumable", final)
        listed = self.service.list(limit=10)
        self.assertTrue(any(item["job_id"] == job_id for item in listed))


class SubtitleChunkStoreTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.store = self.harness.services.store
        self.job_id = "sub-20260101-000000-abcdef"

    def tearDown(self):
        self.harness.cleanup()

    def test_chunk_crud_and_schema_v4(self):
        from dupcleaner import db as db_module
        self.assertEqual(self.store.db.schema_version(),
                         db_module.CURRENT_SCHEMA_VERSION)
        # schema 会继续演进（当前 v5），此处只要求 v4 引入的 subtitle_chunks
        # 仍存在，不再硬编码具体版本号。
        self.assertGreaterEqual(db_module.CURRENT_SCHEMA_VERSION, 4)
        names = {row[0] for row in self.store.db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("subtitle_chunks", names)
        self.store.save_subtitle_chunk(
            self.job_id, 1, 600.0, 1200.0, "done",
            [{"start": 601.0, "end": 605.0, "text": "b"}])
        self.store.save_subtitle_chunk(
            self.job_id, 0, 0.0, 600.0, "done",
            [{"start": 1.0, "end": 2.0, "text": "a"}])
        chunks = self.store.load_subtitle_chunks(self.job_id)
        self.assertEqual([item["chunk_index"] for item in chunks], [0, 1])
        self.assertEqual(chunks[0]["cues"][0]["text"], "a")
        self.assertEqual(chunks[1]["cues"][0]["start"], 601.0)
        listed = self.store.list_subtitle_chunks(self.job_id)
        self.assertEqual(len(listed), 2)
        self.assertEqual(len(self.store.list_subtitle_chunks()), 2)
        self.store.delete_subtitle_chunks(self.job_id, 1)
        self.assertEqual(
            [item["chunk_index"] for item in
             self.store.load_subtitle_chunks(self.job_id)], [0])
        self.store.delete_subtitle_chunks(self.job_id)
        self.assertEqual(self.store.load_subtitle_chunks(self.job_id), [])

    def test_v3_to_v4_migration_creates_table(self):
        import shutil
        import sqlite3
        import tempfile
        from dupcleaner import db as db_module
        from dupcleaner.store import Store
        tmpdir = tempfile.mkdtemp(prefix="dc-sub-v4-")
        try:
            conn = sqlite3.connect(os.path.join(tmpdir, "dupcleaner.sqlite3"))
            try:
                conn.execute(
                    "CREATE TABLE schema_meta ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                conn.execute(
                    "INSERT INTO schema_meta(key, value)"
                    " VALUES('schema_version', '3')")
                conn.execute("PRAGMA user_version = 3")
                conn.commit()
            finally:
                conn.close()
            store = Store(tmpdir)
            try:
                self.assertEqual(store.db.schema_version(),
                                 db_module.CURRENT_SCHEMA_VERSION)
                names = {row[0] for row in store.db.query(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("subtitle_chunks", names)
            finally:
                store.close()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class SubtitleApiTest(SubtitleTestBase):
    def test_list_and_pause_resume_api(self):
        web = WebServer(self.harness).start()
        self.addCleanup(web.stop)
        anon_status, _headers, _body = web.client.request(
            "GET", "/api/subtitles")
        self.assertEqual(anon_status, 401)
        csrf = web.client.login()
        real = self.service.client.transcribe

        def slow(*args, **kwargs):
            time.sleep(0.2)
            return real(*args, **kwargs)

        self.service.client.transcribe = slow
        video = self.make_video(name="api.mp4", duration=3600.0)
        status, _headers, payload = web.client.json(
            "POST", "/api/subtitles",
            {"path": video, "language": "auto", "translate": False,
             "confirm_cloud": True}, csrf=csrf)
        self.assertEqual(status, 202)
        job_id = payload["job"]["job_id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            _s, _h, body = web.client.json(
                "GET", "/api/subtitles/%s" % job_id)
            if int(body["job"].get("chunk_index") or 0) >= 1:
                break
            time.sleep(0.02)
        status, _headers, body = web.client.json(
            "POST", "/api/subtitles/%s/pause" % job_id, {}, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertEqual(body["job"]["status"], "paused")
        _wait_until(lambda: not self.service._active_ids, 20.0)
        status, _headers, body = web.client.json(
            "GET", "/api/subtitles?limit=10")
        self.assertEqual(status, 200)
        self.assertTrue(any(item["job_id"] == job_id
                            for item in body["jobs"]))
        status, _headers, body = web.client.json(
            "POST", "/api/subtitles/%s/resume" % job_id, {}, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertIn(body["job"]["status"], ("queued", "running"))
        final = _wait_status(self.service, job_id, ("done", "failed"), 90.0)
        self.assertEqual(final["status"], "done")


if __name__ == "__main__":
    unittest.main()
