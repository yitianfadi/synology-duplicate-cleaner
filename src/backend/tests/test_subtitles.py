# -*- coding: utf-8 -*-
"""云端字幕测试：fake ffmpeg + fake OpenAI 兼容 HTTP 服务（无真实网络）。"""

import http.server
import json
import os
import sys
import threading
import time
import unittest
import urllib.parse

from tests.helpers import Harness, WebServer, write_text
from tests.test_video import FAKE_FFMPEG, make_video

from dupcleaner import settings as settings_module, subtitles
from dupcleaner import video_similarity
from dupcleaner.errors import ApiError

FENCE = chr(96) * 3
SRT_RESPONSE = (
    "1\n00:00:01,000 --> 00:00:03,500\nSRT response cue\n\n"
    "2\n00:00:04,000 --> 00:00:06,000\nSecond SRT cue\n")


class FakeCloudHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _send_raw(self, status, text, content_type):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_POST(self):
        server = self.server
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        body = self.rfile.read(length)
        path = self.path.split("?")[0]
        if path.endswith("/audio/transcriptions"):
            server.asr_calls += 1
            server.audio_sizes.append(len(body))
            server.authorizations.append(self.headers.get("Authorization") or "")
            mode = server.asr_mode
            if mode == "401":
                self._send(401, {"error": {"message": "invalid key"}})
                return
            if mode == "429_twice" and server.asr_calls <= 2:
                self._send(429, {"error": {"message": "rate limit"}})
                return
            if mode == "500_twice" and server.asr_calls <= 2:
                self._send(500, {"error": {"message": "server error"}})
                return
            if mode == "timeout":
                time.sleep(2.0)
                self._send(200, {"segments": []})
                return
            if mode == "srt":
                self._send_raw(200, SRT_RESPONSE, "text/plain")
                return
            if mode == "no_timestamps":
                self._send(200, {"text": "plain transcript without timeline"})
                return
            prefix = getattr(server, "asr_text_prefix", "chunk-cue")
            payload = {"segments": [
                {"start": 0.0, "end": 5.0,
                 "text": "%s-%d" % (prefix, server.asr_calls)},
                {"start": 5.0, "end": 9.0,
                 "text": "%s-%d-b" % (prefix, server.asr_calls)},
            ]}
            self._send(200, payload)
            return
        if path.endswith("/chat/completions"):
            server.llm_calls += 1
            try:
                request_payload = json.loads(body.decode("utf-8"))
                cues = json.loads(
                    request_payload["messages"][-1]["content"])["cues"]
            except (ValueError, KeyError, TypeError, IndexError):
                cues = []
            mode = server.llm_mode
            if mode == "fenced":
                content = (FENCE + "json\n"
                           + json.dumps([u"T:" + cue for cue in cues],
                                        ensure_ascii=False)
                           + "\n" + FENCE)
            elif mode == "wrong_then_ok" and server.llm_calls == 1:
                content = json.dumps(["only-one"])
            elif mode == "always_wrong":
                content = json.dumps(["only-one"])
            else:
                content = json.dumps([u"T:" + cue for cue in cues],
                                     ensure_ascii=False)
            self._send(200, {"choices": [{"message": {"content": content}}]})
            return
        self._send(404, {"error": "not found"})


class FakeCloudServer(object):
    def __init__(self):
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeCloudHandler)
        self.httpd.asr_mode = "verbose"
        self.httpd.llm_mode = "ok"
        self.httpd.asr_calls = 0
        self.httpd.llm_calls = 0
        self.httpd.audio_sizes = []
        self.httpd.authorizations = []
        self.httpd.asr_text_prefix = "chunk-cue"
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.02},
                                       daemon=True)

    @property
    def base_url(self):
        return "http://127.0.0.1:%d/v1" % self.httpd.server_address[1]

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
            self.thread.join(timeout=5)


class SubtitleTestBase(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root
        self.fake_ffmpeg = os.path.join(self.harness.tmpdir, "fake_ffmpeg.py")
        write_text(self.fake_ffmpeg, FAKE_FFMPEG)
        self.ffmpeg_cmd = [sys.executable, self.fake_ffmpeg]
        self.cloud = FakeCloudServer().start()
        self.service = self.harness.services.subtitles
        self.service.ffmpeg = video_similarity.FFmpegTool(cmd=self.ffmpeg_cmd)
        self.service.ffmpeg.resolve(self.harness.config)
        self.service.client = subtitles.OpenAIClient(retry_delay=0.01)
        self._configure_settings()

    def tearDown(self):
        try:
            self.service.shutdown(timeout=5)
        finally:
            self.cloud.stop()
            self.harness.cleanup()

    def _configure_settings(self, **overrides):
        settings = {
            "subtitle_enabled": True,
            "subtitle_asr_base_url": self.cloud.base_url,
            "subtitle_asr_api_key": "SECRET_ASR_KEY_XYZ",
            "subtitle_asr_model": "whisper-1",
            "subtitle_asr_language": "auto",
            "subtitle_asr_timeout_sec": 60,
            "subtitle_asr_chunk_sec": 600,
            "subtitle_asr_max_audio_mb": 24,
            "subtitle_translate_enabled": True,
            "subtitle_llm_base_url": self.cloud.base_url,
            "subtitle_llm_api_key": "SECRET_LLM_KEY_XYZ",
            "subtitle_llm_model": "gpt-4.1-mini",
            "subtitle_translate_target": "zh",
            "subtitle_translate_timeout_sec": 60,
            "subtitle_translate_batch_size": 20,
        }
        settings.update(overrides)
        self.harness.services.settings.update(settings)

    def make_video(self, name="a.mp4", duration=120.0, extra=""):
        return make_video(os.path.join(self.root, name), seed=3,
                          duration=duration, extra=extra)

    def wait_job(self, job_id, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.service.get(job_id)
            if job["status"] in ("done", "failed", "cancelled"):
                return job
            time.sleep(0.02)
        raise AssertionError("subtitle job did not finish")

    def submit(self, video, **overrides):
        payload = {"path": video, "language": "auto", "translate": False,
                   "confirm_cloud": True}
        payload.update(overrides)
        return self.service.submit("admin", payload)

    def file_text(self, job_id, name):
        path = os.path.join(self.service._job_dir(job_id), name)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    SKIP_FILES = {"settings.json", "video_cache.sqlite3",
                  "feature_cache.json", "dupcleaner.sqlite3",
                  "dupcleaner.sqlite3-wal", "dupcleaner.sqlite3-shm"}

    def data_files_text(self):
        chunks = []
        for root, dirs, files in os.walk(self.harness.data_dir):
            dirs[:] = [item for item in dirs if item != "subtitles"]
            for name in files:
                if name in self.SKIP_FILES:
                    continue
                path = os.path.join(root, name)
                try:
                    with open(path, "rb") as handle:
                        chunks.append(handle.read().decode("utf-8", "replace"))
                except OSError:
                    continue
        return "\n".join(chunks)


class SubtitleSettingsTest(unittest.TestCase):
    def test_defaults_and_validation(self):
        defaults = settings_module.default_settings()
        self.assertFalse(defaults["subtitle_enabled"])
        self.assertEqual(defaults["subtitle_asr_base_url"],
                         "https://api.openai.com/v1")
        self.assertEqual(defaults["subtitle_asr_model"], "whisper-1")
        self.assertEqual(defaults["subtitle_asr_language"], "auto")
        self.assertEqual(defaults["subtitle_asr_timeout_sec"], 300)
        self.assertEqual(defaults["subtitle_asr_chunk_sec"], 600)
        self.assertEqual(defaults["subtitle_asr_max_audio_mb"], 24)
        self.assertEqual(defaults["subtitle_llm_model"], "gpt-4.1-mini")
        self.assertEqual(defaults["subtitle_translate_target"], "zh")
        self.assertEqual(defaults["subtitle_translate_timeout_sec"], 120)
        self.assertEqual(defaults["subtitle_translate_batch_size"], 20)
        patch = settings_module.validate_patch({
            "subtitle_enabled": True,
            "subtitle_asr_base_url": "http://127.0.0.1:9000/v1/",
            "subtitle_asr_api_key": "abc",
            "subtitle_asr_model": "openai/whisper-1",
            "subtitle_asr_language": "zh",
            "subtitle_asr_timeout_sec": 600,
            "subtitle_asr_chunk_sec": 1200,
            "subtitle_asr_max_audio_mb": 50,
            "subtitle_translate_enabled": True,
            "subtitle_llm_base_url": "https://example.com/v1",
            "subtitle_llm_api_key": "def",
            "subtitle_llm_model": "gpt-4o-mini",
            "subtitle_translate_target": "en",
            "subtitle_translate_timeout_sec": 300,
            "subtitle_translate_batch_size": 40,
        })
        self.assertTrue(patch["subtitle_enabled"])
        self.assertEqual(patch["subtitle_asr_base_url"],
                         "http://127.0.0.1:9000/v1")
        for item in [
            {"subtitle_asr_base_url": "ftp://x/y"},
            {"subtitle_asr_base_url": "not-a-url"},
            {"subtitle_asr_timeout_sec": 29},
            {"subtitle_asr_timeout_sec": 1801},
            {"subtitle_asr_chunk_sec": 59},
            {"subtitle_asr_chunk_sec": 1801},
            {"subtitle_asr_max_audio_mb": 0},
            {"subtitle_asr_max_audio_mb": 101},
            {"subtitle_translate_timeout_sec": 29},
            {"subtitle_translate_timeout_sec": 901},
            {"subtitle_translate_batch_size": 0},
            {"subtitle_translate_batch_size": 41},
            {"subtitle_asr_language": "auto-auto-auto"},
            {"subtitle_translate_target": "auto"},
            {"subtitle_asr_model": "bad model name"},
        ]:
            with self.assertRaises(Exception, msg=str(item)):
                settings_module.validate_patch(item)

    def test_public_settings_masks_secrets(self):
        current = {"subtitle_asr_api_key": "ABC", "subtitle_llm_api_key": ""}
        public = settings_module.public_settings(current)
        self.assertEqual(public["subtitle_asr_api_key"], "")
        self.assertTrue(public["subtitle_asr_api_key_set"])
        self.assertFalse(public["subtitle_llm_api_key_set"])
        self.assertNotIn("ABC", json.dumps(public))

    def test_prepare_patch_secret_semantics(self):
        patch, clears = settings_module.prepare_settings_patch({
            "subtitle_asr_api_key": "", "subtitle_llm_api_key": "NEW",
            "subtitle_asr_api_key_set": True,
            "subtitle_clear_llm_api_key": True,
        })
        self.assertNotIn("subtitle_asr_api_key", patch)
        self.assertEqual(patch["subtitle_llm_api_key"], "NEW")
        self.assertNotIn("subtitle_asr_api_key_set", patch)
        self.assertIn("subtitle_llm_api_key", clears)


class SubtitleHelperTest(unittest.TestCase):
    def test_parse_srt_and_asr_body(self):
        cues = subtitles.parse_srt(SRT_RESPONSE)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0]["start"], 1.0)
        self.assertAlmostEqual(cues[0]["end"], 3.5)
        self.assertEqual(cues[0]["text"], "SRT response cue")
        verbose = json.dumps({"segments": [
            {"start": 0, "end": 2, "text": " hello "},
            {"start": 2, "end": 3, "text": "world"}]})
        parsed = subtitles.parse_asr_body(verbose)
        self.assertEqual([item["text"] for item in parsed], ["hello", "world"])
        srt_parsed = subtitles.parse_asr_body(SRT_RESPONSE)
        self.assertEqual(len(srt_parsed), 2)
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            subtitles.parse_asr_body(json.dumps({"text": "plain"}))
        self.assertEqual(ctx.exception.code, "NO_TIMESTAMPS")
        with self.assertRaises(subtitles.SubtitleError):
            subtitles.parse_asr_body("not a subtitle payload")

    def test_timestamps_and_wrap(self):
        self.assertEqual(subtitles.format_timestamp(3661.5, True),
                         "01:01:01,500")
        self.assertEqual(subtitles.format_timestamp(3661.5, False),
                         "01:01:01.500")
        self.assertEqual(subtitles.format_timestamp(-1, True),
                         "00:00:00,000")
        latin = "word " * 40
        wrapped = subtitles.wrap_text(latin)
        self.assertTrue(max(len(line) for line in wrapped.split("\n")) <= 80)
        cjk = u"\u4e2d\u6587" * 60
        wrapped_cjk = subtitles.wrap_text(cjk)
        self.assertTrue(max(len(line) for line in wrapped_cjk.split("\n")) <= 42)
        vtt = subtitles.build_vtt(
            [{"start": 1.0, "end": 2.0, "text": "a & <b>"}])
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn("&amp;", vtt)

    def test_dedupe_and_chunks(self):
        cues = [
            {"start": 0.0, "end": 2.0, "text": "same"},
            {"start": 0.0, "end": 2.0, "text": "same"},
            {"start": 2.1, "end": 4.0, "text": "next"},
            {"start": 4.0, "end": 3.0, "text": "invalid"},
        ]
        cleaned = subtitles.dedupe_cues(cues)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(subtitles.plan_chunks(1200, 600),
                         [(0.0, 600.0), (600.0, 600.0)])
        self.assertEqual(subtitles.plan_chunks(0, 600), [])

    def test_chat_content_parsing(self):
        self.assertEqual(
            subtitles.OpenAIClient._parse_chat_content(json.dumps({
                "choices": [{"message": {"content": "[\"a\",\"b\"]"}}]})),
            ["a", "b"])
        self.assertEqual(
            subtitles.OpenAIClient._parse_chat_content(json.dumps({
                "choices": [{"message": {
                    "content": FENCE + "json\n[\"a\"]\n" + FENCE}}]})),
            ["a"])
        self.assertIsNone(
            subtitles.OpenAIClient._parse_chat_content("not json"))
        self.assertIsNone(
            subtitles.OpenAIClient._parse_chat_content(json.dumps({
                "choices": [{"message": {"content": "{\"a\":1}"}}]})))


class SubtitleServiceTest(SubtitleTestBase):
    def test_happy_path_with_translation(self):
        video = self.make_video(duration=1200)
        job = self.submit(video, translate=True, target_lang="zh")
        self.assertEqual(job["status"], "queued")
        done = self.wait_job(job["job_id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual(done["progress"], 100)
        self.assertFalse(done["translation_partial"])
        names = {item["name"] for item in done["files"]}
        self.assertEqual(names, {"original.srt", "original.vtt", "zh.srt",
                                 "zh.vtt", "bilingual.zh.srt"})
        original = self.file_text(job["job_id"], "original.srt")
        self.assertIn("chunk-cue-1", original)
        self.assertIn("00:10:00,000", original)
        translated = self.file_text(job["job_id"], "zh.srt")
        self.assertIn("T:", translated)
        self.assertIn("00:10:00,000", translated)
        vtt = self.file_text(job["job_id"], "original.vtt")
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertGreaterEqual(self.cloud.httpd.asr_calls, 2)
        job_dir = self.service._job_dir(job["job_id"])
        for name in os.listdir(job_dir):
            self.assertFalse(name.endswith(".mp3") or name.endswith(".wav"))

    def test_asr_srt_response(self):
        self.cloud.httpd.asr_mode = "srt"
        video = self.make_video(duration=60)
        job = self.submit(video)
        done = self.wait_job(job["job_id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertIn("SRT response cue",
                      self.file_text(job["job_id"], "original.srt"))

    def test_no_timestamps_fails(self):
        self.cloud.httpd.asr_mode = "no_timestamps"
        video = self.make_video(duration=60)
        job = self.submit(video)
        done = self.wait_job(job["job_id"])
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error"]["code"], "NO_TIMESTAMPS")

    def test_auth_error_no_retry(self):
        self.cloud.httpd.asr_mode = "401"
        video = self.make_video(duration=60)
        job = self.submit(video)
        done = self.wait_job(job["job_id"])
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error"]["code"], "AUTH_FAILED")
        self.assertEqual(self.cloud.httpd.asr_calls, 1)
        self.assertNotIn("SECRET_ASR_KEY_XYZ", json.dumps(done))

    def test_rate_limit_retries_twice(self):
        self.cloud.httpd.asr_mode = "429_twice"
        video = self.make_video(duration=60)
        job = self.submit(video)
        done = self.wait_job(job["job_id"], timeout=30)
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual(self.cloud.httpd.asr_calls, 3)

    def test_server_error_retries(self):
        self.cloud.httpd.asr_mode = "500_twice"
        video = self.make_video(duration=60)
        job = self.submit(video)
        done = self.wait_job(job["job_id"], timeout=30)
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual(self.cloud.httpd.asr_calls, 3)

    def test_asr_timeout(self):
        self.cloud.httpd.asr_mode = "timeout"
        self.service.client = subtitles.OpenAIClient(retry_delay=0.01,
                                                     timeout_cap=0.5)
        video = self.make_video(duration=60)
        job = self.submit(video)
        started = time.time()
        done = self.wait_job(job["job_id"], timeout=20)
        self.assertLess(time.time() - started, 8.0)
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error"]["code"], "TIMEOUT")

    def test_no_audio(self):
        video = self.make_video(duration=60, extra="NOAUDIO=1\n")
        job = self.submit(video)
        done = self.wait_job(job["job_id"])
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error"]["code"], "NO_AUDIO")
        self.assertEqual(self.cloud.httpd.asr_calls, 0)

    def test_cancel_running_job(self):
        self.cloud.httpd.asr_mode = "timeout"
        self.service.client = subtitles.OpenAIClient(retry_delay=0.01)
        video = self.make_video(duration=120)
        job = self.submit(video)
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.cloud.httpd.asr_calls > 0:
                break
            time.sleep(0.02)
        started = time.time()
        cancelled = self.service.cancel(job["job_id"])
        self.assertIn(cancelled["status"], ("running", "cancelled"))
        final = self.wait_job(job["job_id"], timeout=10)
        self.assertLess(time.time() - started, 5.0)
        self.assertEqual(final["status"], "cancelled")

    def test_max_audio_mb_splits_chunks(self):
        self._configure_settings(subtitle_asr_max_audio_mb=1,
                                 subtitle_asr_chunk_sec=600)
        import unittest.mock as mock
        with mock.patch.dict(os.environ,
                             {"FAKE_MP3_BYTES_PER_SEC": "30000"}):
            video = self.make_video(duration=600)
            job = self.submit(video)
            done = self.wait_job(job["job_id"], timeout=30)
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertGreater(self.cloud.httpd.asr_calls, 1)
        limit = 1024 * 1024 + 4096
        for size in self.cloud.httpd.audio_sizes:
            self.assertLessEqual(size, limit)

    def test_llm_fenced_and_retry(self):
        for mode in ("fenced", "wrong_then_ok"):
            self.cloud.httpd.llm_mode = mode
            self.cloud.httpd.llm_calls = 0
            video = self.make_video(name="%s.mp4" % mode, duration=60)
            job = self.submit(video, translate=True, target_lang="zh",
                              language="en")
            done = self.wait_job(job["job_id"], timeout=30)
            self.assertEqual(done["status"], "done", done.get("error"))
            self.assertFalse(done["translation_partial"])
            self.assertIn("T:", self.file_text(job["job_id"], "zh.srt"))

    def test_llm_partial_fallback(self):
        self.cloud.httpd.llm_mode = "always_wrong"
        video = self.make_video(duration=60)
        job = self.submit(video, translate=True, target_lang="zh")
        done = self.wait_job(job["job_id"], timeout=30)
        self.assertEqual(done["status"], "done")
        self.assertTrue(done["translation_partial"])
        self.assertIn("chunk-cue",
                      self.file_text(job["job_id"], "zh.srt"))
        self.assertNotIn("T:", self.file_text(job["job_id"], "zh.srt"))
        self.assertEqual(self.cloud.httpd.llm_calls, 2)

    def test_no_secrets_or_transcripts_in_logs(self):
        self.cloud.httpd.asr_text_prefix = "TRANSCRIPT_UNIQUE_ABC"
        video = self.make_video(duration=60)
        job = self.submit(video, translate=True, target_lang="zh")
        self.wait_job(job["job_id"], timeout=30)
        logs_text = self.data_files_text()
        for secret in ("SECRET_ASR_KEY_XYZ", "SECRET_LLM_KEY_XYZ"):
            self.assertNotIn(secret, logs_text)
        self.assertNotIn("TRANSCRIPT_UNIQUE_ABC", logs_text)
        job_json = os.path.join(self.service._jobs_dir,
                                job["job_id"] + ".json")
        with open(job_json, "r", encoding="utf-8") as handle:
            stored = handle.read()
        self.assertNotIn("SECRET_ASR_KEY_XYZ", stored)
        self.assertNotIn("TRANSCRIPT_UNIQUE_ABC", stored)
        self.assertIn("TRANSCRIPT_UNIQUE_ABC",
                      self.file_text(job["job_id"], "original.srt"))

    def test_persistence_and_lru(self):
        video = self.make_video(duration=60)
        job = self.submit(video)
        done = self.wait_job(job["job_id"], timeout=30)
        self.assertEqual(done["status"], "done")
        reloaded = subtitles.SubtitleService(
            self.harness.config, self.harness.services.store,
            self.harness.services.shares, self.harness.services.settings.get,
            client=subtitles.OpenAIClient(retry_delay=0.01),
            ffmpeg=video_similarity.FFmpegTool(cmd=self.ffmpeg_cmd))
        try:
            restored = reloaded.get(job["job_id"])
            self.assertEqual(restored["status"], "done")
            self.assertTrue(restored["files"])
        finally:
            reloaded.shutdown(timeout=2)
        self.service.job_limit = 2
        video2 = self.make_video(name="b.mp4", duration=60)
        job2 = self.submit(video2)
        self.wait_job(job2["job_id"], timeout=30)
        video3 = self.make_video(name="c.mp4", duration=60)
        job3 = self.submit(video3)
        self.wait_job(job3["job_id"], timeout=30)
        self.assertEqual(len(self.service._jobs), 2)
        with self.assertRaises(ApiError):
            self.service.get(job["job_id"])
        self.assertFalse(os.path.exists(self.service._job_dir(job["job_id"])))

    def test_download_whitelist(self):
        video = self.make_video(duration=60)
        job = self.submit(video)
        self.wait_job(job["job_id"], timeout=30)
        path = self.service.get_file(job["job_id"], "original.srt")
        self.assertTrue(os.path.isfile(path))
        for bad in ("../settings.json", "jobs/1.json", "unknown.srt",
                    "original.exe", "/etc/passwd"):
            with self.assertRaises(ApiError, msg=bad):
                self.service.get_file(job["job_id"], bad)


class SubtitleApiTest(SubtitleTestBase):
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

    def test_settings_masking_and_clear(self):
        status, _headers, payload = self.client.json("GET", "/api/settings")
        self.assertEqual(status, 200)
        settings = payload["settings"]
        self.assertEqual(settings["subtitle_asr_api_key"], "")
        self.assertTrue(settings["subtitle_asr_api_key_set"])
        self.assertNotIn("SECRET_ASR_KEY_XYZ", json.dumps(payload))
        status, _headers, payload = self.client.json(
            "PUT", "/api/settings", {
                "settings": {"subtitle_asr_api_key": "",
                             "subtitle_llm_api_key": ""}}, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.assertTrue(payload["settings"]["subtitle_llm_api_key_set"])
        status, _headers, payload = self.client.json(
            "PUT", "/api/settings", {
                "settings": {"subtitle_clear_asr_api_key": True},
                "subtitle_clear_llm_api_key": True}, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.assertFalse(payload["settings"]["subtitle_asr_api_key_set"])
        self.assertFalse(payload["settings"]["subtitle_llm_api_key_set"])
        status, _headers, payload = self.client.json("GET", "/api/settings")
        self.assertFalse(payload["settings"]["subtitle_asr_api_key_set"])

    def test_submit_and_download_flow(self):
        video = self.make_video(duration=60)
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles",
            {"path": video, "language": "auto", "translate": True,
             "target_lang": "zh", "confirm_cloud": True},
            csrf=self.csrf)
        self.assertEqual(status, 202, payload)
        job_id = payload["job"]["job_id"]
        deadline = time.time() + 30
        final = None
        while time.time() < deadline:
            status, _headers, payload = self.client.json(
                "GET", "/api/subtitles/%s" % job_id)
            self.assertEqual(status, 200)
            if payload["job"]["status"] in ("done", "failed", "cancelled"):
                final = payload["job"]
                break
            time.sleep(0.03)
        self.assertIsNotNone(final)
        self.assertEqual(final["status"], "done", final.get("error"))
        status, headers, raw = self.client.request(
            "GET", "/api/subtitles/%s/file/original.srt" % job_id)
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["content-disposition"][0])
        self.assertIn(b"chunk-cue", raw)
        status, _headers, payload = self.client.json(
            "GET", "/api/subtitles/%s/file/unknown.srt" % job_id)
        self.assertEqual(status, 404)
        status, _headers, payload = self.client.json(
            "GET", "/api/subtitles/%s/file/%s"
            % (job_id, urllib.parse.quote("../settings.json")))
        self.assertIn(status, (400, 404))

    def test_submit_validation_errors(self):
        video = self.make_video(duration=60)
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles",
            {"path": video, "confirm_cloud": False}, csrf=self.csrf)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "CLOUD_CONFIRM_REQUIRED")
        self._configure_settings(subtitle_enabled=False)
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles",
            {"path": video, "confirm_cloud": True}, csrf=self.csrf)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "SUBTITLE_DISABLED")
        self._configure_settings()
        bad = os.path.join(self.root, "note.txt")
        write_text(bad, "not a video")
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles",
            {"path": bad, "confirm_cloud": True}, csrf=self.csrf)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_VIDEO")
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles",
            {"path": "/etc/passwd", "confirm_cloud": True}, csrf=self.csrf)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "PATH_NOT_ALLOWED")

    def test_cancel_api(self):
        self.cloud.httpd.asr_mode = "timeout"
        self.service.client = subtitles.OpenAIClient(retry_delay=0.01)
        video = self.make_video(duration=120)
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles",
            {"path": video, "confirm_cloud": True}, csrf=self.csrf)
        self.assertEqual(status, 202)
        job_id = payload["job"]["job_id"]
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.cloud.httpd.asr_calls > 0:
                break
            time.sleep(0.02)
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles/%s/cancel" % job_id, {}, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.assertIn(payload["job"]["status"], ("running", "cancelled"))
