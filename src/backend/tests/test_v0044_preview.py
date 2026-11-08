# -*- coding: utf-8 -*-
"""v0044：预览增强（缩略图 / 媒体信息 / 缩略图清单）测试。

依赖系统 ffmpeg 生成测试视频；没有 ffmpeg 时相关用例 skip。
"""

import os
import shutil
import subprocess
import unittest

from tests.helpers import Harness, WebServer, write_bytes, write_text

from dupcleaner import config, video_similarity
from dupcleaner.thumbnails import ThumbnailService

FFMPEG = shutil.which("ffmpeg")


def make_test_video(path, seconds=3, size="320x240"):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    command = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i",
        "testsrc=size=%s:rate=15:duration=%s" % (size, seconds),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", path,
    ]
    subprocess.run(command, check=True, timeout=120,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path


class MediaStreamParseTest(unittest.TestCase):
    def test_parse_streams_with_resolution_and_languages(self):
        text = (
            "Input #0, matroska,webm, from 'movie.mkv':\n"
            "  Duration: 00:42:10.50, start: 0.000000, bitrate: 4321 kb/s\n"
            "  Stream #0:0: Video: h264 (High), yuv420p, 1920x1080 [SAR 1:1 DAR 16:9], 23.98 fps\n"
            "  Stream #0:1(jpn): Audio: aac (LC), 48000 Hz, stereo, fltp, 128 kb/s (default)\n"
            "  Stream #0:2(chi): Audio: ac3, 48000 Hz, 5.1, fltp, 384 kb/s\n"
            "  Stream #0:3(eng): Subtitle: subrip (srt)\n"
        )
        parsed = video_similarity.parse_media_streams(text)
        self.assertEqual(len(parsed["video_streams"]), 1)
        self.assertEqual(parsed["video_streams"][0]["width"], 1920)
        self.assertEqual(parsed["video_streams"][0]["height"], 1080)
        self.assertEqual(parsed["video_streams"][0]["codec"], "h264 (High)")
        self.assertEqual(len(parsed["audio_streams"]), 2)
        self.assertEqual(parsed["audio_streams"][0]["language"], "jpn")
        self.assertTrue(parsed["audio_streams"][0]["default"])
        self.assertEqual(parsed["audio_streams"][1]["channels"], "5.1")
        self.assertEqual(len(parsed["subtitle_streams"]), 1)
        self.assertEqual(parsed["subtitle_streams"][0]["language"], "eng")

    def test_parse_empty_text(self):
        parsed = video_similarity.parse_media_streams("")
        self.assertEqual(parsed, {"audio_streams": [], "subtitle_streams": [],
                                  "video_streams": []})


@unittest.skipUnless(FFMPEG, "需要系统 ffmpeg")
class ThumbnailServiceTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.settings = {}
        self.service = ThumbnailService(
            self.harness.config, settings_provider=lambda: self.settings)
        self.video = make_test_video(
            os.path.join(self.harness.dev_root, "clip.mp4"), seconds=3)

    def tearDown(self):
        self.harness.cleanup()

    def test_thumbnail_render_and_cache(self):
        result = self.service.thumbnail(self.video, timestamp=1.0, width=200)
        self.assertTrue(os.path.isfile(result["path"]))
        self.assertFalse(result["cached"])
        self.assertGreater(os.path.getsize(result["path"]), 0)
        with open(result["path"], "rb") as handle:
            head = handle.read(3)
        self.assertEqual(head, b"\xff\xd8\xff")  # JPEG 魔数
        second = self.service.thumbnail(self.video, timestamp=1.0, width=200)
        self.assertEqual(second["path"], result["path"])
        self.assertTrue(second["cached"])
        other = self.service.thumbnail(self.video, timestamp=2.0, width=200)
        self.assertNotEqual(other["path"], result["path"])

    def test_media_info(self):
        info = self.service.media_info(self.video)
        self.assertAlmostEqual(float(info["duration"]), 3.0, delta=0.6)
        self.assertEqual(info["video"].get("width"), 320)
        self.assertEqual(info["video"].get("height"), 240)
        self.assertEqual(info["audio_stream_count"], 0)
        self.assertEqual(info["subtitle_stream_count"], 0)
        # 第二次命中内存缓存（不再起 ffmpeg）：直接复用同一对象内容
        again = self.service.media_info(self.video)
        self.assertEqual(again, info)

    def test_preview_plan(self):
        plan = self.service.preview_plan(self.video, count=4, width=160)
        self.assertEqual(len(plan["thumbs"]), 4)
        self.assertEqual(len(plan["timestamps"]), 4)
        for item in plan["thumbs"]:
            self.assertTrue(item["url"].startswith("/api/preview/thumb?"))
            self.assertGreater(item["t"], 0)
            self.assertLessEqual(item["t"], 3.0)
        self.assertEqual(plan["width"], 160)

    def test_missing_file(self):
        from dupcleaner.errors import ApiError
        with self.assertRaises(ApiError) as ctx:
            self.service.media_info(os.path.join(self.harness.dev_root, "nope.mp4"))
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_prune(self):
        directory = self.service.cache_dir()
        os.makedirs(directory, exist_ok=True)
        for index in range(6):
            write_bytes(os.path.join(directory, "probe-%d.jpg" % index), b"x" * 16)
        old_max = config.THUMB_MAX_ENTRIES
        try:
            config.THUMB_MAX_ENTRIES = 3
            result = self.service.prune()
        finally:
            config.THUMB_MAX_ENTRIES = old_max
        self.assertGreaterEqual(result["deleted"], 1)
        self.assertLessEqual(result["remaining"], 3)
        stats = self.service.stats()
        self.assertLessEqual(stats["entries"], 3)


@unittest.skipUnless(FFMPEG, "需要系统 ffmpeg")
class PreviewApiTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.server = WebServer(self.harness).start()
        self.client = self.server.client
        self.csrf = self.client.login()
        self.video = make_test_video(
            os.path.join(self.harness.dev_root, "clip.mp4"), seconds=2)
        self.text = write_text(
            os.path.join(self.harness.dev_root, "note.txt"), u"hello")

    def tearDown(self):
        self.server.stop()
        self.harness.cleanup()

    def test_thumb_endpoint_and_cache_header(self):
        path = "/api/preview/thumb?path=%s&t=1.0&w=200" % _quote(self.video)
        status, headers, data = self.client.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type", [""])[0], "image/jpeg")
        self.assertEqual(headers.get("x-thumb-cache", [""])[0], "miss")
        self.assertTrue(data.startswith(b"\xff\xd8\xff"))
        status, headers, _data = self.client.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-thumb-cache", [""])[0], "hit")

    def test_media_and_thumbs_endpoints(self):
        status, _headers, payload = self.client.json(
            "GET", "/api/preview/media?path=%s" % _quote(self.video))
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertAlmostEqual(float(payload["media"]["duration"]), 2.0, delta=0.6)
        status, _headers, payload = self.client.json(
            "GET", "/api/preview/thumbs?path=%s&count=3&w=160" % _quote(self.video))
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["thumbs"]), 3)
        self.assertTrue(payload["thumbs"][0]["url"].startswith("/api/preview/thumb?"))

    def test_thumb_rejects_non_video_and_outside_share(self):
        status, _headers, payload = self.client.json(
            "GET", "/api/preview/thumb?path=%s" % _quote(self.text))
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "PREVIEW_UNSUPPORTED")
        status, _headers, _payload = self.client.request(
            "GET", "/api/preview/thumb?path=%s" % _quote("/etc/passwd"))
        self.assertEqual(status, 403)


def _quote(path):
    import urllib.parse
    return urllib.parse.quote(path, safe="")


if __name__ == "__main__":
    unittest.main()
