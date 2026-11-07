# -*- coding: utf-8 -*-
"""云端字幕测试：fake ffmpeg + fake OpenAI 兼容 HTTP 服务（无真实网络）。"""

import http.server
import json
import os
import re
import sys
import threading
import time
import unittest
import urllib.parse
from unittest import mock

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

    def do_GET(self):
        server = self.server
        path = self.path.split("?")[0]
        if path.endswith("/models"):
            server.models_calls += 1
            authorization = self.headers.get("Authorization") or ""
            server.models_authorizations.append(authorization)
            server.authorizations.append(authorization)
            mode = server.models_mode
            if mode == "401":
                self._send(401, {"error": {
                    "message": "invalid key %s" % server.error_echo}})
                return
            if mode == "404":
                self._send(404, {"error": {"message": "not supported"}})
                return
            if mode == "500":
                self._send(500, {"error": {"message": "server error"}})
                return
            if mode == "html":
                self._send_raw(200, "<html>not json</html>", "text/html")
                return
            if mode == "timeout":
                time.sleep(2.0)
                self._send(200, {"data": []})
                return
            if mode == "empty":
                self._send(200, {"data": []})
                return
            if mode == "toplevel":
                self._send(200, [{"id": "b-model"}, "a-model"])
                return
            if mode == "models_key":
                self._send(200, {"models": [{"id": "m3", "owned_by": "o"},
                                            "m1"]})
                return
            if mode == "many":
                self._send(200, {"data": [
                    {"id": "model-%04d" % index} for index in range(600)]})
                return
            if mode == "bad_shape":
                self._send(200, {"data": {"not": "a list"}})
                return
            if mode == "wlk":
                # WhisperLiveKit GET /v1/models（OpenAI 兼容，单模型）
                self._send(200, {"object": "list", "data": [
                    {"id": "whisper-base", "object": "model",
                     "owned_by": "whisperlivekit"}]})
                return
            self._send(200, {"data": [
                {"id": "zeta", "owned_by": "openai"},
                "Alpha",
                {"id": "whisper-1", "owned_by": "openai"},
                {"id": "whisper-1"},
                {"id": ""},
                {"id": 123},
                {"id": "x" * 201},
                {"id": "beta+fp16", "owned_by": "org"},
            ]})
            return
        self._send(404, {"error": "not found"})

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
            filename_match = re.search(rb'filename="([^"]*)"', body[:4000])
            server.upload_filenames.append(
                filename_match.group(1).decode("utf-8", "replace")
                if filename_match else "")
            type_match = re.search(rb"Content-Type:\s*([^\r\n]+)", body[:4000])
            server.upload_content_types.append(
                type_match.group(1).decode("utf-8", "replace").strip()
                if type_match else "")
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
            if mode == "wlk":
                # WhisperLiveKit REST verbose_json（含 task/duration/text，多余字段
                # 我们的解析应忽略；segments 用于生成字幕）。
                self._send(200, {
                    "task": "transcribe", "language": "en",
                    "duration": 7.16, "text": "Hello world",
                    "segments": [
                        {"id": 0, "start": 0.0, "end": 1.0,
                         "text": "Hello world"},
                        {"id": 1, "start": 1.0, "end": 2.0,
                         "text": "Second line"}],
                    "usage": {"type": "duration", "seconds": 7},
                })
                return
            if mode == "wlk_empty":
                # 静音音频在 WLK 上返回空 segments，但仍是 verbose_json 结构。
                self._send(200, {
                    "task": "transcribe", "language": "en",
                    "duration": 1.0, "text": "", "segments": [],
                    "usage": {"type": "duration", "seconds": 1},
                })
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
            server.authorizations.append(
                self.headers.get("Authorization") or "")
            try:
                request_payload = json.loads(body.decode("utf-8"))
                cues = json.loads(
                    request_payload["messages"][-1]["content"])["cues"]
                server.llm_payloads.append(request_payload)
            except (ValueError, KeyError, TypeError, IndexError):
                cues = []
            mode = server.llm_mode
            if mode == "401":
                self._send(401, {"error": {
                    "message": "invalid key %s" % server.error_echo}})
                return
            if mode == "invalid":
                self._send(200, {"choices": [{"message": {
                    "content": json.dumps({"bad": "not a string array"})}}]})
                return
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
        self.httpd.upload_filenames = []
        self.httpd.upload_content_types = []
        self.httpd.llm_payloads = []
        self.httpd.asr_text_prefix = "chunk-cue"
        # v0026 模型列表 / 连接测试
        self.httpd.models_mode = "ok"
        self.httpd.models_calls = 0
        self.httpd.models_authorizations = []
        self.httpd.error_echo = "ECHO_SECRET_PLACEHOLDER"
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
        self.assertFalse(defaults["subtitle_llm_disable_thinking"])
        self.assertFalse(defaults["subtitle_write_vtt"])
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
            "subtitle_llm_disable_thinking": True,
            "subtitle_write_vtt": True,
        })
        self.assertTrue(patch["subtitle_enabled"])
        self.assertTrue(patch["subtitle_llm_disable_thinking"])
        self.assertTrue(patch["subtitle_write_vtt"])
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
            {"subtitle_llm_disable_thinking": "yes"},
            {"subtitle_write_vtt": "yes"},
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


    def test_base_url_rejects_userinfo(self):
        for value in ("http://user:pass@example.com/v1",
                      "https://token@example.com/v1",
                      "http://:pw@example.com/v1"):
            with self.assertRaises(ApiError, msg=value) as ctx:
                settings_module.validate_patch(
                    {"subtitle_asr_base_url": value})
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(
            settings_module.validate_base_url_value(
                "subtitle_asr_base_url", "http://127.0.0.1:9000/v1/"),
            "http://127.0.0.1:9000/v1")
        with self.assertRaises(ApiError):
            settings_module.validate_base_url_value(
                "subtitle_asr_base_url", "http://a:b@host/v1")
        with self.assertRaises(ApiError):
            settings_module.validate_patch(
                {"subtitle_llm_base_url": "http://a:b@host/v1"})

    def test_model_name_charset(self):
        for value in ("openai/whisper-1", "org/model@v1", "model+fp16",
                      "meta-llama/Llama-3.1-8B@latest"):
            patch = settings_module.validate_patch(
                {"subtitle_asr_model": value})
            self.assertEqual(patch["subtitle_asr_model"], value)
        for value in ("bad model name", "model\tname", "x" * 101,
                      "mo del"):
            with self.assertRaises(ApiError, msg=repr(value)):
                settings_module.validate_patch(
                    {"subtitle_llm_model": value})


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
        # v0033：默认只生成 SRT，任务目录不再生成 VTT
        names = {item["name"] for item in done["files"]}
        self.assertEqual(names, {"original.srt", "zh.srt", "bilingual.zh.srt"})
        self.assertFalse(any(name.endswith(".vtt") for name in names))
        original = self.file_text(job["job_id"], "original.srt")
        self.assertIn("chunk-cue-1", original)
        self.assertIn("00:10:00,000", original)
        translated = self.file_text(job["job_id"], "zh.srt")
        self.assertIn("T:", translated)
        self.assertIn("00:10:00,000", translated)
        self.assertGreaterEqual(self.cloud.httpd.asr_calls, 2)
        job_dir = self.service._job_dir(job["job_id"])
        self.assertFalse(os.path.exists(os.path.join(job_dir, "original.vtt")))
        for name in os.listdir(job_dir):
            self.assertFalse(name.endswith(".mp3") or name.endswith(".wav"))

    def test_write_vtt_optional(self):
        # v0033：开启 subtitle_write_vtt 后才生成 VTT（原文 + 译文）
        self._configure_settings(subtitle_write_vtt=True)
        video = self.make_video("vtt.mp4", duration=120)
        job = self.submit(video, translate=True, target_lang="zh")
        done = self.wait_job(job["job_id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        names = {item["name"] for item in done["files"]}
        self.assertEqual(names, {"original.srt", "original.vtt", "zh.srt",
                                 "zh.vtt", "bilingual.zh.srt"})
        vtt = self.file_text(job["job_id"], "original.vtt")
        self.assertTrue(vtt.startswith("WEBVTT"))
        # 视频同目录仍只写 srt（不写 vtt）
        alongside = os.path.join(self.root, "vtt.vtt")
        self.assertFalse(os.path.exists(alongside))

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

    def test_disable_thinking_payload(self):
        # v0030：默认不发送 chat_template_kwargs（兼容 OpenAI 官方等）
        video = self.make_video(duration=60)
        job = self.submit(video, translate=True, target_lang="zh")
        done = self.wait_job(job["job_id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertTrue(self.cloud.httpd.llm_payloads)
        self.assertNotIn("chat_template_kwargs",
                         self.cloud.httpd.llm_payloads[-1])
        # 开启后翻译请求附带 enable_thinking=false
        self._configure_settings(subtitle_llm_disable_thinking=True)
        video2 = self.make_video("b.mp4", duration=60)
        job2 = self.submit(video2, translate=True, target_lang="zh")
        done2 = self.wait_job(job2["job_id"])
        self.assertEqual(done2["status"], "done", done2.get("error"))
        self.assertEqual(
            self.cloud.httpd.llm_payloads[-1].get("chat_template_kwargs"),
            {"enable_thinking": False})
        self.assertNotIn("SECRET_LLM_KEY_XYZ",
                         json.dumps(self.cloud.httpd.llm_payloads[-1]))

    def test_server_audio_mode_remux(self):
        # v0032：server 模式用流复制抽音轨（.mka），NAS 不解码
        self._configure_settings(subtitle_audio_source="server")
        video = self.make_video(duration=120)
        job = self.submit(video)
        self.assertEqual(job["audio_source"], "server")
        done = self.wait_job(job["job_id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertTrue(self.cloud.httpd.upload_filenames)
        for name in self.cloud.httpd.upload_filenames:
            self.assertTrue(name.endswith(".mka"), name)
        for content_type in self.cloud.httpd.upload_content_types:
            self.assertIn("matroska", content_type)
        self.assertIsNone(done["warning"])

    def test_server_audio_mode_fallback(self):
        # v0032：流复制失败回退 NAS 抽音频，任务仍 done 并带 warning
        self._configure_settings(subtitle_audio_source="server")
        with mock.patch.dict(os.environ, {"FAKE_REMUX_FAIL": "1"}):
            video = self.make_video(duration=60)
            job = self.submit(video)
            done = self.wait_job(job["job_id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual(done["warning"], "SERVER_AUDIO_FALLBACK")
        for name in self.cloud.httpd.upload_filenames:
            self.assertTrue(name.endswith(".mp3"), name)

    def test_server_audio_mode_size_split(self):
        from dupcleaner import config as config_module
        self._configure_settings(subtitle_audio_source="server",
                                 subtitle_asr_chunk_sec=600)
        with mock.patch.object(config_module,
                               "SUBTITLE_SERVER_UPLOAD_MAX_MB", 1):
            with mock.patch.dict(os.environ,
                                 {"FAKE_REMUX_BYTES_PER_SEC": "30000"}):
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


class SubtitleBatchGroupTest(SubtitleTestBase):
    """v0032：批次组（一次提交自动拆批、聚合进度、控制与保留策略）。"""

    def make_group(self, paths, **overrides):
        payload = {
            "paths": list(paths),
            "language": "auto",
            "translate": False,
            "confirm_cloud": True,
        }
        payload.update(overrides)
        return self.service.create_batch_group("admin", payload)

    def wait_group(self, group_id, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = self.service.get_batch_group(group_id)
            if snapshot["status"] in ("done", "failed", "cancelled"):
                return snapshot
            time.sleep(0.02)
        raise AssertionError("subtitle group did not finish")

    def test_create_group_splits_batches(self):
        from dupcleaner import config as config_module
        videos = [self.make_video("g%d.mp4" % index, duration=60)
                  for index in range(3)]
        with mock.patch.object(config_module, "SUBTITLE_BATCH_MAX_ITEMS", 2):
            group = self.make_group(videos)
        self.assertEqual(group["total"], 3)
        self.assertEqual(group["batch_count"], 2)
        self.assertEqual([item["total"] for item in group["batches"]],
                         [2, 1])
        self.assertEqual(group["options"]["source"], "asr")
        done = self.wait_group(group["group_id"])
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["counts"]["done"], 3)
        self.assertEqual(done["progress"], 100)
        # 任务分页可按 group_id 过滤
        page = self.service.list_page(group_id=group["group_id"], limit=2)
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["jobs"]), 2)
        self.assertEqual(page["limit"], 2)
        self.assertTrue(all(item["group_id"] == group["group_id"]
                            for item in page["jobs"]))
        page2 = self.service.list_page(group_id=group["group_id"], limit=2,
                                       offset=2)
        self.assertEqual(len(page2["jobs"]), 1)

    def test_group_too_large_and_empty(self):
        from dupcleaner import config as config_module
        with mock.patch.object(config_module,
                               "SUBTITLE_BATCH_GROUP_MAX_ITEMS", 2):
            with self.assertRaises(ApiError) as ctx:
                self.make_group(["a.mp4", "b.mp4", "c.mp4"])
        self.assertEqual(ctx.exception.code, "BATCH_TOO_LARGE")
        with self.assertRaises(ApiError) as ctx:
            self.make_group([os.path.join(self.root, "missing.mp4")])
        self.assertEqual(ctx.exception.code, "BATCH_EMPTY")

    def test_group_unknown_404(self):
        with self.assertRaises(ApiError) as ctx:
            self.service.get_batch_group("grp-20260101-000000-abcdef")
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_group_control_after_done(self):
        video = self.make_video("ctl.mp4", duration=60)
        group = self.make_group([video])
        self.wait_group(group["group_id"])
        paused = self.service.pause_batch_group(group["group_id"])
        self.assertEqual(paused["status"], "done")
        cancelled = self.service.cancel_batch_group(group["group_id"])
        self.assertEqual(cancelled["status"], "done")
        retried = self.service.retry_batch_group(group["group_id"])
        self.assertEqual(retried["status"], "done")
        resumed = self.service.resume_batch_group(group["group_id"])
        self.assertEqual(resumed["status"], "done")

    def test_group_retention_limit(self):
        from dupcleaner import config as config_module
        first_video = self.make_video("r1.mp4", duration=60)
        first = self.make_group([first_video])
        self.wait_group(first["group_id"])
        with mock.patch.object(config_module, "SUBTITLE_BATCH_GROUP_LIMIT", 1):
            second_video = self.make_video("r2.mp4", duration=60)
            second = self.make_group([second_video])
            self.wait_group(second["group_id"])
        with self.assertRaises(ApiError) as ctx:
            self.service.get_batch_group(first["group_id"])
        self.assertEqual(ctx.exception.code, "NOT_FOUND")
        self.assertEqual(self.service.get_batch_group(
            second["group_id"])["status"], "done")


class SubtitleProbeTest(SubtitleTestBase):
    """v0026：模型列表 / 连接测试（fake HTTP + fake ffmpeg，无真实网络）。"""

    def test_list_models_sort_dedupe_filter_and_key_fallback(self):
        result = self.service.list_models("asr", {})
        self.assertEqual(result["kind"], "asr")
        self.assertEqual(result["base_url"], self.cloud.base_url)
        self.assertFalse(result["truncated"])
        self.assertGreaterEqual(result["latency_ms"], 0)
        ids = [item["id"] for item in result["models"]]
        self.assertEqual(ids, ["Alpha", "beta+fp16", "whisper-1", "zeta"])
        owned = {item["id"]: item["owned_by"] for item in result["models"]}
        self.assertEqual(owned["whisper-1"], "openai")
        self.assertEqual(owned["Alpha"], "")
        self.assertEqual(result["warning"], "")
        self.assertEqual(self.cloud.httpd.models_calls, 1)
        self.assertEqual(self.cloud.httpd.models_authorizations,
                         ["Bearer SECRET_ASR_KEY_XYZ"])
        self.assertNotIn("SECRET_ASR_KEY_XYZ", json.dumps(result))

    def test_list_models_explicit_override(self):
        result = self.service.list_models(
            "llm", {"base_url": self.cloud.base_url,
                    "api_key": "OVERRIDE_KEY_123"})
        self.assertEqual(result["kind"], "llm")
        self.assertEqual(self.cloud.httpd.models_authorizations[-1],
                         "Bearer OVERRIDE_KEY_123")
        self.assertNotIn("OVERRIDE_KEY_123", json.dumps(result))

    def test_list_models_ignores_model_and_language_overrides(self):
        # 模型/语言输入框暂态非法不能阻塞“获取模型列表”。
        result = self.service.list_models("asr", {
            "model": "bad model name", "language": "not a language",
            "target_lang": "auto"})
        self.assertEqual(result["count"], 4)
        self.assertEqual(self.cloud.httpd.models_calls, 1)

    def test_list_models_shapes_and_limit(self):
        self.cloud.httpd.models_mode = "toplevel"
        result = self.service.list_models("asr", {})
        self.assertEqual([item["id"] for item in result["models"]],
                         ["a-model", "b-model"])
        self.cloud.httpd.models_mode = "models_key"
        result = self.service.list_models("asr", {})
        self.assertEqual([item["id"] for item in result["models"]],
                         ["m1", "m3"])
        self.cloud.httpd.models_mode = "many"
        result = self.service.list_models("asr", {})
        self.assertEqual(result["count"], 500)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["models"][0]["id"], "model-0000")
        self.assertEqual(result["models"][-1]["id"], "model-0499")

    def test_list_models_empty_and_bad_shape(self):
        self.cloud.httpd.models_mode = "empty"
        result = self.service.list_models("asr", {})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["models"], [])
        self.assertTrue(result["warning"])
        self.cloud.httpd.models_mode = "bad_shape"
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            self.service.list_models("asr", {})
        self.assertEqual(ctx.exception.code, "MODELS_UNSUPPORTED")

    def test_list_models_error_mapping_and_no_retry(self):
        self.cloud.httpd.models_mode = "401"
        self.cloud.httpd.error_echo = "SECRET_ASR_KEY_XYZ"
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            self.service.list_models("asr", {})
        self.assertEqual(ctx.exception.code, "PROVIDER_AUTH_FAILED")
        self.assertNotIn("SECRET_ASR_KEY_XYZ", ctx.exception.message)

        for mode, code in (("404", "MODELS_UNSUPPORTED"),
                           ("html", "MODELS_UNSUPPORTED"),
                           ("500", "PROVIDER_HTTP_ERROR")):
            self.cloud.httpd.models_mode = mode
            before = self.cloud.httpd.models_calls
            with self.assertRaises(subtitles.SubtitleError) as ctx:
                self.service.list_models("asr", {})
            self.assertEqual(ctx.exception.code, code, mode)
            self.assertEqual(self.cloud.httpd.models_calls, before + 1,
                             mode)

    def test_list_models_timeout_and_network(self):
        self.service.client = subtitles.OpenAIClient(
            retry_delay=0.01, timeout_cap=0.3)
        self.cloud.httpd.models_mode = "timeout"
        started = time.time()
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            self.service.list_models("asr", {})
        self.assertEqual(ctx.exception.code, "PROVIDER_TIMEOUT")
        self.assertLess(time.time() - started, 5.0)

        with mock.patch.object(self.service.client, "_get_raw",
                               return_value=("network", "x", b"")):
            with self.assertRaises(subtitles.SubtitleError) as ctx:
                self.service.list_models("asr", {})
        self.assertEqual(ctx.exception.code, "PROVIDER_NETWORK_ERROR")

    def test_probe_busy(self):
        self.assertTrue(self.service._probe_lock.acquire(False))
        try:
            for call in (lambda: self.service.list_models("asr", {}),
                         lambda: self.service.test_connection("llm", {})):
                with self.assertRaises(subtitles.SubtitleError) as ctx:
                    call()
                self.assertEqual(ctx.exception.code, "PROBE_BUSY")
        finally:
            self.service._probe_lock.release()

    def test_invalid_kind_and_base_url(self):
        for kind in ("bad", "", None):
            with self.assertRaises(ApiError) as ctx:
                self.service.list_models(kind, {})
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        for overrides in ({"base_url": "ftp://host/v1"},
                          {"base_url": "http://user:pass@host/v1"},
                          {"base_url": "not-a-url"}):
            with self.assertRaises(ApiError) as ctx:
                self.service.test_connection("asr", overrides)
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_asr_connection_success_verbose(self):
        result = self.service.test_connection("asr", {})
        self.assertEqual(result["kind"], "asr")
        self.assertEqual(result["mode"], "asr")
        self.assertEqual(result["model"], "whisper-1")
        self.assertEqual(result["base_url"], self.cloud.base_url)
        detail = result["detail"]
        self.assertEqual(detail["format"], "verbose_json")
        self.assertEqual(detail["segments"], 2)
        self.assertTrue(detail["has_timestamps"])
        self.assertGreater(detail["text_chars"], 0)
        self.assertEqual(detail["warning"], "")
        self.assertEqual(result["warning"], detail["warning"])
        self.assertEqual(self.cloud.httpd.asr_calls, 1)
        self.assertEqual(self.cloud.httpd.authorizations[-1],
                         "Bearer SECRET_ASR_KEY_XYZ")
        self.assertNotIn("SECRET_ASR_KEY_XYZ", json.dumps(result))
        # 静音测试音频与临时目录必须清理
        tmp_root = os.path.join(self.harness.data_dir, "tmp")
        leftovers = []
        if os.path.isdir(tmp_root):
            leftovers = [name for name in os.listdir(tmp_root)
                         if name.startswith("dc-subtitle-probe-")]
        self.assertEqual(leftovers, [])

    def test_asr_connection_no_timestamps_and_srt(self):
        self.cloud.httpd.asr_mode = "no_timestamps"
        result = self.service.test_connection("asr", {})
        detail = result["detail"]
        self.assertEqual(detail["format"], "text")
        self.assertFalse(detail["has_timestamps"])
        self.assertGreater(detail["text_chars"], 0)
        self.assertTrue(detail["warning"])
        # 顶层 warning 与 detail.warning 一致，前端统一按告警展示。
        self.assertEqual(result["warning"], detail["warning"])
        self.cloud.httpd.asr_mode = "srt"
        detail = self.service.test_connection("asr", {})["detail"]
        self.assertEqual(detail["format"], "srt")
        self.assertTrue(detail["has_timestamps"])
        self.assertEqual(detail["segments"], 2)

    def test_asr_connection_auth_failure_no_retry(self):
        self.cloud.httpd.asr_mode = "401"
        self.cloud.httpd.error_echo = "SECRET_ASR_KEY_XYZ"
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            self.service.test_connection("asr", {})
        self.assertEqual(ctx.exception.code, "PROVIDER_AUTH_FAILED")
        self.assertNotIn("SECRET_ASR_KEY_XYZ", ctx.exception.message)
        self.assertEqual(self.cloud.httpd.asr_calls, 1)

    def test_asr_connection_ffmpeg_failures(self):
        with mock.patch.object(self.service.ffmpeg, "available",
                               return_value=False):
            with self.assertRaises(subtitles.SubtitleError) as ctx:
                self.service.test_connection("asr", {})
        self.assertEqual(ctx.exception.code, "FFMPEG_UNAVAILABLE")
        with mock.patch.dict(os.environ,
                             {"FAKE_FFMPEG_SILENCE_FAIL": "1"}):
            with self.assertRaises(subtitles.SubtitleError) as ctx:
                self.service.test_connection("asr", {})
        self.assertEqual(ctx.exception.code, "AUDIO_FAILED")
        self.assertEqual(self.cloud.httpd.asr_calls, 0)

    def test_llm_connection_success_and_errors(self):
        result = self.service.test_connection("llm", {})
        self.assertEqual(result["kind"], "llm")
        self.assertEqual(result["mode"], "chat")
        self.assertEqual(result["model"], "gpt-4.1-mini")
        self.assertEqual(result["detail"]["items"], 1)
        self.assertGreater(result["detail"]["text_chars"], 0)
        self.assertEqual(result["warning"], "")
        self.assertTrue(result["detail"]["preview"])
        self.assertNotIn("SECRET_LLM_KEY_XYZ", json.dumps(result))
        self.assertEqual(self.cloud.httpd.authorizations[-1],
                         "Bearer SECRET_LLM_KEY_XYZ")
        self.cloud.httpd.llm_mode = "invalid"
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            self.service.test_connection("llm", {})
        self.assertEqual(ctx.exception.code, "PROVIDER_REJECTED")
        self.cloud.httpd.llm_mode = "401"
        self.cloud.httpd.error_echo = "SECRET_LLM_KEY_XYZ"
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            self.service.test_connection("llm", {})
        self.assertEqual(ctx.exception.code, "PROVIDER_AUTH_FAILED")
        self.assertNotIn("SECRET_LLM_KEY_XYZ", ctx.exception.message)
        # v0030：连接测试同样遵循“禁用思考”开关
        self.assertNotIn("chat_template_kwargs",
                         self.cloud.httpd.llm_payloads[-1])
        self.cloud.httpd.llm_mode = "ok"
        self._configure_settings(subtitle_llm_disable_thinking=True)
        self.service.test_connection("llm", {})
        self.assertEqual(
            self.cloud.httpd.llm_payloads[-1].get("chat_template_kwargs"),
            {"enable_thinking": False})

    def test_no_secret_persisted_after_probes(self):
        self.cloud.httpd.error_echo = "SECRET_ASR_KEY_XYZ"
        self.cloud.httpd.models_mode = "401"
        with self.assertRaises(subtitles.SubtitleError):
            self.service.list_models("asr", {})
        self.cloud.httpd.models_mode = "ok"
        self.service.list_models("asr", {})
        self.service.test_connection("asr", {})
        self.service.test_connection("llm", {})
        data_text = self.data_files_text()
        for secret in ("SECRET_ASR_KEY_XYZ", "SECRET_LLM_KEY_XYZ"):
            self.assertNotIn(secret, data_text)


class WhisperLiveKitCompatTest(SubtitleTestBase):
    """v0027：自建 WhisperLiveKit（OpenAI 兼容 REST）兼容性回归。

    参考 https://github.com/QuentinFuxa/WhisperLiveKit 的 /v1/models 与
    /v1/audio/transcriptions(verbose_json) 契约，确保我们的云端字幕客户端
    可直接指向自建 WLK 服务（音频不出局域网）。
    """

    def test_models_and_verbose_json_shape(self):
        self.cloud.httpd.models_mode = "wlk"
        result = self.service.list_models("asr", {})
        self.assertEqual([item["id"] for item in result["models"]],
                         ["whisper-base"])
        self.assertEqual(result["models"][0]["owned_by"], "whisperlivekit")
        self.assertEqual(self.cloud.httpd.models_authorizations[-1],
                         "Bearer SECRET_ASR_KEY_XYZ")

        self.cloud.httpd.asr_mode = "wlk"
        audio = os.path.join(self.harness.tmpdir, "wlk-audio.mp3")
        write_text(audio, "fake audio bytes")
        cues = self.service.client.transcribe(
            self.cloud.base_url, "WLK_TOKEN", "whisper-base", "en",
            audio, 30, None)
        self.assertEqual([cue["text"] for cue in cues],
                         ["Hello world", "Second line"])
        self.assertEqual(cues[0]["start"], 0.0)
        self.assertEqual(self.cloud.httpd.authorizations[-1],
                         "Bearer WLK_TOKEN")

    def test_job_uses_wlk_verbose_json(self):
        self.cloud.httpd.asr_mode = "wlk"
        job = self.submit(self.make_video(duration=60), translate=False)
        final = self.wait_job(job["job_id"])
        self.assertEqual(final["status"], "done", final.get("error"))
        srt = self.file_text(job["job_id"], "original.srt")
        self.assertIn("Hello world", srt)
        self.assertIn("Second line", srt)

    def test_silence_probe_warning_is_compatible(self):
        self.cloud.httpd.asr_mode = "wlk_empty"
        result = self.service.test_connection("asr", {})
        self.assertFalse(result["detail"]["has_timestamps"])
        self.assertEqual(result["detail"]["format"], "verbose_json")
        self.assertTrue(result["warning"])
        self.assertIn(u"静音", result["warning"])


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

    def test_models_and_test_api(self):
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles/models", {"kind": "asr"},
            csrf=self.csrf)
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["kind"], "asr")
        self.assertGreater(payload["count"], 0)
        self.assertFalse(payload["truncated"])
        self.assertIn("models", payload)
        self.assertIn("latency_ms", payload)
        self.assertNotIn("SECRET_ASR_KEY_XYZ", json.dumps(payload))

        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles/test", {"kind": "llm"},
            csrf=self.csrf)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["mode"], "chat")
        self.assertIn("detail", payload)
        self.assertNotIn("SECRET_LLM_KEY_XYZ", json.dumps(payload))

        # 非法 kind / base URL / userinfo → 400 INVALID_ARGUMENT
        for path, body in (
                ("/api/subtitles/models", {"kind": "bad"}),
                ("/api/subtitles/models",
                 {"kind": "asr", "base_url": "ftp://host/v1"}),
                ("/api/subtitles/models",
                 {"kind": "asr",
                  "base_url": "http://user:pass@127.0.0.1/v1"}),
                ("/api/subtitles/test",
                 {"kind": "asr", "base_url": "not-a-url"})):
            status, _headers, payload = self.client.json(
                "POST", path, body, csrf=self.csrf)
            self.assertEqual(status, 400, (path, body, payload))
            self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")

        # 未登录 → 401
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles/models", {"kind": "asr"},
            use_cookie=False)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "AUTH_REQUIRED")

    def test_probe_api_error_mapping(self):
        # Provider 401 必须映射为 400（不能返回 401 让前端登出）
        self.cloud.httpd.models_mode = "401"
        self.cloud.httpd.error_echo = "SECRET_ASR_KEY_XYZ"
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles/models", {"kind": "asr"},
            csrf=self.csrf)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "PROVIDER_AUTH_FAILED")
        self.assertNotIn("SECRET_ASR_KEY_XYZ", json.dumps(payload))

        self.cloud.httpd.models_mode = "404"
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles/models", {"kind": "asr"},
            csrf=self.csrf)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "MODELS_UNSUPPORTED")

        self.cloud.httpd.models_mode = "500"
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles/models", {"kind": "asr"},
            csrf=self.csrf)
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "PROVIDER_HTTP_ERROR")

        # 探测锁占用 → 409 PROBE_BUSY
        self.assertTrue(self.service._probe_lock.acquire(False))
        try:
            status, _headers, payload = self.client.json(
                "POST", "/api/subtitles/models", {"kind": "asr"},
                csrf=self.csrf)
        finally:
            self.service._probe_lock.release()
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "PROBE_BUSY")

        # CSRF 缺失 → 403（非 GET 接口）
        self.cloud.httpd.models_mode = "ok"
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles/test", {"kind": "llm"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "CSRF_FAILED")
