# -*- coding: utf-8 -*-
"""v0051 下载中心：播放列表解析 + 服务行为（本地假 HTTP 服务，无真实网络）。"""

from __future__ import absolute_import

import http.server
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest

from tests.helpers import write_bytes, write_text

from dupcleaner import config, hls
from dupcleaner import downloads as downloads_module
from dupcleaner.downloads import DownloadError, DownloadService
from dupcleaner.errors import ApiError, path_not_allowed
from dupcleaner.store import Store

MEDIA_PLAYLIST = u"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:4.000,
seg0.ts
#EXTINF:4.000,
seg1.ts
#EXTINF:2.000,
seg2.ts
#EXT-X-ENDLIST
"""

MASTER_PLAYLIST = u"""#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=640x360
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080
high/index.m3u8
"""

ENCRYPTED_PLAYLIST = u"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x00000000000000000000000000000001
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4.000,
seg0.m4s
#EXTINF:4.000,
seg1.m4s
#EXT-X-ENDLIST
"""


# ----------------------------------------------------------------------
# 假 CDN：支持 Range、播放列表、密钥与多码率
# ----------------------------------------------------------------------
class FakeCdnHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _body_for(self, path):
        server = self.server
        if path in server.texts:
            return server.texts[path].encode("utf-8"), "application/vnd.apple.mpegurl"
        if path in server.blobs:
            return server.blobs[path], "application/octet-stream"
        return None, None

    def do_GET(self):
        server = self.server
        server.requests.append(self.path)
        path = self.path.split("?")[0]
        if server.fail_remaining > 0:
            server.fail_remaining -= 1
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body, content_type = self._body_for(path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        range_header = self.headers.get("Range") or ""
        server.ranges.append((path, range_header))
        match = re.match(r"^bytes=(\d*)-(\d*)$", range_header.strip())
        start = 0
        end = len(body) - 1
        status = 200
        if match and server.range_support:
            if match.group(1):
                start = int(match.group(1))
            if match.group(2):
                end = int(match.group(2))
            if start >= len(body):
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        chunk = body[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes" if server.range_support
                         else "none")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d"
                             % (start, end, len(body)))
        self.end_headers()
        try:
            self.wfile.write(chunk)
        except OSError:
            pass


class FakeCdn(object):
    def __init__(self):
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeCdnHandler)
        self.httpd.texts = {}
        self.httpd.blobs = {}
        self.httpd.requests = []
        self.httpd.ranges = []
        self.httpd.fail_remaining = 0
        self.httpd.range_support = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.02},
                                       daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
            self.thread.join(timeout=5)

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def put(self, path, data, text=False):
        if text:
            self.httpd.texts[path] = data
        else:
            self.httpd.blobs[path] = data
        return self.url(path)


# ----------------------------------------------------------------------
# 假 FFmpeg：只验证参数与产物，不做真实编码
# ----------------------------------------------------------------------
class FakeMergeTool(object):
    def __init__(self):
        self.calls = []
        self.probe_duration = 10.0

    def available(self):
        return True

    def run(self, args, timeout=180.0, stop_check=None):
        self.calls.append(list(args))
        out = args[-1]
        size = 0
        for item in args:
            if not isinstance(item, str) or not item.endswith(".m3u8"):
                continue
            base = os.path.dirname(item)
            with open(item, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    candidate = os.path.join(base, line)
                    if os.path.isfile(candidate):
                        size += os.path.getsize(candidate)
        with open(out, "wb") as handle:
            handle.write(b"FAKEMP4" + b"0" * 1024)
            handle.write(b"1" * min(size, 4096))
        return 0, b"", b""

    def probe_media(self, path, timeout, stop_check=None):
        return {"duration": self.probe_duration, "has_audio": True,
                "has_subtitle": False, "audio_streams": [],
                "subtitle_streams": [], "video_streams": []}


class ShareStub(object):
    def __init__(self, root):
        self.root = os.path.abspath(root)

    def share_root(self, path):
        base = os.path.abspath(path)
        if base != self.root and not base.startswith(self.root + os.sep):
            raise path_not_allowed(u"不在共享目录内: %s" % path)
        return {"path": self.root}


class DownloadHarness(object):
    def __init__(self, notifier=None):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-dl-")
        self.share = os.path.join(self.tmpdir, "share")
        self.downloads = os.path.join(self.share, "downloads")
        os.makedirs(self.downloads, exist_ok=True)
        self.web = os.path.join(self.tmpdir, "web")
        write_text(os.path.join(self.web, "index.html"), u"x")
        self.config = config.RuntimeConfig(
            data_dir=os.path.join(self.tmpdir, "data"),
            web_dir=self.web,
            dev_mode=True,
            dev_roots=[self.share],
            dev_password="devpass",
            environ={},
        )
        self.store = Store(self.config.data_dir)
        self.tool = FakeMergeTool()
        self.settings = {
            "download_enabled": True,
            "download_output_dir": self.downloads,
            "download_concurrency": 2,
            "download_segment_concurrency": 3,
            "download_retry": 1,
            "download_timeout_sec": 10,
            "download_rate_limit_kbps": 0,
            "download_max_height": 0,
            "download_user_agent": "test-agent",
            "download_referer": "",
            "download_cookie": "",
            "download_proxy": "",
            "download_allow_private": True,
            "download_keep_temp": False,
            "download_min_free_gb": 0,
            "download_verify_tolerance_sec": 5,
            "download_job_limit": 20,
            "download_subdir_mode": "none",
            "download_subdir_template": "{site}/{date}",
            "download_auto_scan": False,
            "download_auto_subtitle": False,
            # v0053 下载增强
            "download_skip_existing": True,
            "download_auto_retry": False,
            "download_auto_retry_max": 2,
            "download_offpeak_enabled": False,
            "download_offpeak_start": "01:00",
            "download_offpeak_end": "08:00",
            "download_offpeak_rate_limit_kbps": 0,
            "download_name_template": "{title}",
        }
        self.service = DownloadService(
            self.config, self.store, ShareStub(self.share),
            lambda: dict(self.settings), ffmpeg_tool=self.tool,
            notifier=notifier)
        self.service.start()

    def wait(self, job_id, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.service.get(job_id)
            if job.status in downloads_module.FINAL_STATUSES:
                return job
            time.sleep(0.02)
        raise AssertionError(u"download %s 未在 %.1fs 内结束" %
                             (job_id, timeout))

    def cleanup(self):
        try:
            self.service.shutdown(timeout=2)
        finally:
            try:
                self.store.close()
            except Exception:
                pass
            shutil.rmtree(self.tmpdir, ignore_errors=True)


# ----------------------------------------------------------------------
# 纯函数：播放列表
# ----------------------------------------------------------------------
class HlsParseTest(unittest.TestCase):
    def test_media_playlist(self):
        playlist = hls.parse(MEDIA_PLAYLIST,
                             "https://cdn.example.com/a/index.m3u8")
        self.assertEqual(playlist["kind"], "media")
        self.assertEqual(len(playlist["segments"]), 3)
        self.assertAlmostEqual(playlist["duration"], 10.0, places=3)
        self.assertEqual(playlist["segments"][0]["uri"],
                         "https://cdn.example.com/a/seg0.ts")
        self.assertEqual(playlist["encryption"], "none")

    def test_master_playlist_and_variant_pick(self):
        playlist = hls.parse(MASTER_PLAYLIST,
                             "https://cdn.example.com/m.m3u8")
        self.assertEqual(playlist["kind"], "master")
        self.assertEqual(len(playlist["variants"]), 2)
        best = hls.pick_variant(playlist)
        self.assertEqual(best["height"], 1080)
        self.assertEqual(hls.variant_label(best), "1080p")
        capped = hls.pick_variant(playlist, 720)
        self.assertEqual(capped["height"], 360)

    def test_encrypted_playlist_plan(self):
        playlist = hls.parse(ENCRYPTED_PLAYLIST,
                             "https://cdn.example.com/x/i.m3u8")
        self.assertEqual(playlist["encryption"], "aes-128")
        plan = hls.plan_local_files(playlist)
        kinds = [item["kind"] for item in plan["fetches"]]
        self.assertEqual(kinds, ["key", "map", "segment", "segment"])
        text = hls.build_local_playlist(playlist, plan)
        self.assertIn(u'#EXT-X-KEY:METHOD=AES-128,URI="key_0000.bin"', text)
        self.assertIn(u"IV=0x00000000000000000000000000000001", text)
        self.assertIn(u'#EXT-X-MAP:URI="init_0000.mp4"', text)
        self.assertIn(u"seg_000001.m4s", text)

    def test_live_and_drm_rejected(self):
        with self.assertRaises(hls.HlsError) as ctx:
            hls.parse(MEDIA_PLAYLIST.replace(u"#EXT-X-ENDLIST", u""),
                      "https://x.example.com/i.m3u8")
        self.assertEqual(ctx.exception.code, "LIVE_UNSUPPORTED")
        with self.assertRaises(hls.HlsError) as ctx:
            hls.parse(ENCRYPTED_PLAYLIST.replace(u"AES-128", u"SAMPLE-AES"),
                      "https://x.example.com/i.m3u8")
        self.assertEqual(ctx.exception.code, "DRM_UNSUPPORTED")

    def test_byte_range_offsets(self):
        text = u"""#EXTM3U
#EXT-X-TARGETDURATION:4
#EXT-X-ENDLIST
#EXTINF:4.0,
#EXT-X-BYTERANGE:100@0
all.ts
#EXTINF:4.0,
#EXT-X-BYTERANGE:50
all.ts
"""
        playlist = hls.parse(text, "https://x.example.com/i.m3u8")
        self.assertEqual(playlist["segments"][0]["byte_range"], (100, 0))
        self.assertEqual(playlist["segments"][1]["byte_range"], (50, 100))

    def test_bad_playlist(self):
        with self.assertRaises(hls.HlsError):
            hls.parse(u"#EXTM3U\n#EXTINF:4,\n#EXT-X-ENDLIST\n",
                      "https://x.example.com/i.m3u8")


# ----------------------------------------------------------------------
# URL / 命名 / 选项
# ----------------------------------------------------------------------
class DownloadUrlTest(unittest.TestCase):
    def test_scheme_rejected(self):
        with self.assertRaises(DownloadError) as ctx:
            downloads_module.assert_url_allowed("ftp://example.com/a.mp4")
        self.assertEqual(ctx.exception.code, "URL_UNSUPPORTED_SCHEME")

    def test_userinfo_rejected(self):
        with self.assertRaises(DownloadError) as ctx:
            downloads_module.assert_url_allowed(
                "https://user:pass@example.com/a.mp4")
        self.assertEqual(ctx.exception.code, "URL_INVALID")

    def test_loopback_rejected_by_default(self):
        with self.assertRaises(DownloadError) as ctx:
            downloads_module.assert_url_allowed("http://127.0.0.1:9/a.mp4")
        self.assertEqual(ctx.exception.code, "URL_PRIVATE_ADDRESS")

    def test_loopback_allowed_when_configured(self):
        result = downloads_module.assert_url_allowed(
            "http://127.0.0.1:9/a.mp4", allow_private=True)
        self.assertTrue(result.startswith("http://127.0.0.1:9/"))

    def test_safe_name(self):
        self.assertEqual(downloads_module._safe_name(u'a/b:c*"d?'), u"a_b_c__d_")
        self.assertEqual(downloads_module._safe_name(u"   "), u"video")

    def test_sanitize_error_strips_query(self):
        text = downloads_module._sanitize_error(
            u"GET https://x.test/a.m3u8?token=SECRET failed",
            cookie=u"session=ABC")
        self.assertNotIn(u"SECRET", text)


# ----------------------------------------------------------------------
# 服务行为
# ----------------------------------------------------------------------
class DownloadServiceTest(unittest.TestCase):
    def setUp(self):
        self.cdn = FakeCdn().start()
        self.h = DownloadHarness()

    def tearDown(self):
        self.h.cleanup()
        self.cdn.stop()

    def _wait_idle(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = self.h.service.queue_snapshot()
            if not snapshot["active"] and not snapshot["queued"]:
                return
            time.sleep(0.02)
        raise AssertionError(u"队列未在 %.1fs 内空闲" % timeout)

    def test_direct_download(self):
        payload = b"0123456789" * 4096
        url = self.cdn.put("/files/movie.mp4", payload)
        created = self.h.service.create("admin", {"urls": [url]})
        job_id = created["jobs"][0]["job_id"]
        job = self.h.wait(job_id)
        self.assertEqual(job.status, "done", job.error_message)
        self.assertTrue(os.path.isfile(job.output_path))
        self.assertEqual(os.path.getsize(job.output_path), len(payload))
        self.assertEqual(job.progress, 100)
        # 临时文件不残留
        leftovers = [name for name in os.listdir(self.h.downloads)
                     if name.endswith(config.DOWNLOAD_PART_EXT)]
        self.assertEqual(leftovers, [])

    def test_hls_download_and_merge(self):
        self.cdn.put("/hls/index.m3u8", MEDIA_PLAYLIST, text=True)
        for index in range(3):
            self.cdn.put("/hls/seg%d.ts" % index,
                         (u"SEG%d" % index).encode("utf-8") * 200)
        created = self.h.service.create(
            "admin", {"urls": [self.cdn.url("/hls/index.m3u8")],
                      "options": {"title": "测试视频"}})
        job_id = created["jobs"][0]["job_id"]
        job = self.h.wait(job_id)
        self.assertEqual(job.status, "done", job.error_message)
        self.assertEqual(job.kind, "hls")
        self.assertEqual(job.segments_total, 3)
        self.assertEqual(job.segments_done, 3)
        self.assertTrue(job.output_path.endswith(".mp4"))
        self.assertIn(u"测试视频", os.path.basename(job.output_path))
        # 合并参数：本地化播放列表 + 无损封装
        self.assertTrue(self.h.tool.calls)
        args = self.h.tool.calls[0]
        self.assertIn("-allowed_extensions", args)
        self.assertIn("-c", args)
        # 临时目录已清理
        self.assertFalse(os.path.isdir(job.temp_dir) and not job.temp_kept)

    def test_master_playlist_uses_best_variant(self):
        self.cdn.put("/m/master.m3u8", MASTER_PLAYLIST, text=True)
        self.cdn.put("/m/high/index.m3u8", MEDIA_PLAYLIST, text=True)
        self.cdn.put("/m/low/index.m3u8", MEDIA_PLAYLIST, text=True)
        for index in range(3):
            self.cdn.put("/m/high/seg%d.ts" % index, b"h" * 128)
            self.cdn.put("/m/low/seg%d.ts" % index, b"l" * 128)
        created = self.h.service.create(
            "admin", {"urls": [self.cdn.url("/m/master.m3u8")]})
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "done", job.error_message)
        self.assertEqual(job.variant, "1080p")
        self.assertIn("/m/high/seg0.ts", self.cdn.httpd.requests)

    def test_max_height_caps_variant(self):
        self.cdn.put("/m/master.m3u8", MASTER_PLAYLIST, text=True)
        self.cdn.put("/m/high/index.m3u8", MEDIA_PLAYLIST, text=True)
        self.cdn.put("/m/low/index.m3u8", MEDIA_PLAYLIST, text=True)
        for index in range(3):
            self.cdn.put("/m/high/seg%d.ts" % index, b"h" * 128)
            self.cdn.put("/m/low/seg%d.ts" % index, b"l" * 128)
        created = self.h.service.create(
            "admin", {"urls": [self.cdn.url("/m/master.m3u8")],
                      "options": {"max_height": 720}})
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "done", job.error_message)
        self.assertEqual(job.variant, "360p")
        self.assertIn("/m/low/seg0.ts", self.cdn.httpd.requests)

    def test_resume_direct_with_range(self):
        payload = b"abcdefgh" * 2048
        url = self.cdn.put("/files/big.bin", payload)
        # 预先放一个半成品：服务应续传（Range 206）
        final_name = "big.bin"
        part = os.path.join(self.h.downloads,
                            final_name + config.DOWNLOAD_PART_EXT)
        write_bytes(part, payload[:4096])
        created = self.h.service.create(
            "admin", {"urls": [url], "options": {"title": "big"}})
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "done", job.error_message)
        self.assertEqual(os.path.getsize(job.output_path), len(payload))
        resumed = [item for item in self.cdn.httpd.ranges
                   if item[0] == "/files/big.bin" and item[1].startswith(
                       "bytes=4096-")]
        self.assertTrue(resumed, u"应当用 Range 从 4096 字节续传")
        self.assertIn("/files/big.bin", self.cdn.httpd.requests)

    def test_insufficient_space_rejected(self):
        self.h.settings["download_min_free_gb"] = 10 ** 6
        url = self.cdn.put("/files/x.mp4", b"x" * 4096)
        created = self.h.service.create("admin", {"urls": [url]})
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "INSUFFICIENT_SPACE")

    def test_output_dir_outside_share_rejected(self):
        url = self.cdn.put("/files/x.mp4", b"x" * 4096)
        created = self.h.service.create(
            "admin", {"urls": [url],
                      "options": {"output_dir": self.h.tmpdir}})
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "OUTPUT_DIR_INVALID")

    def test_subdir_site_date(self):
        self.h.settings["download_subdir_mode"] = "site_date"
        url = self.cdn.put("/files/x.mp4", b"x" * 4096)
        created = self.h.service.create("admin", {"urls": [url]})
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "done", job.error_message)
        rel = os.path.relpath(job.output_dir, self.h.downloads)
        parts = rel.replace("\\", "/").split("/")
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0], "127.0.0.1")
        self.assertRegex(parts[1], r"^\d{4}-\d{2}-\d{2}$")

    def test_failure_keeps_temp_and_retry(self):
        self.cdn.put("/hls2/index.m3u8", MEDIA_PLAYLIST, text=True)
        self.cdn.put("/hls2/seg0.ts", b"a" * 64)
        # seg1/seg2 缺失 → 404，任务失败
        created = self.h.service.create(
            "admin", {"urls": [self.cdn.url("/hls2/index.m3u8")]})
        job_id = created["jobs"][0]["job_id"]
        job = self.h.wait(job_id)
        self.assertEqual(job.status, "failed")
        self.assertIn(job.error_code, ("SEGMENT_FAILED", "HTTP_STATUS"))
        self.assertTrue(job.temp_kept)
        # 补齐分片后重试应成功
        self.cdn.put("/hls2/seg1.ts", b"b" * 64)
        self.cdn.put("/hls2/seg2.ts", b"c" * 64)
        self.h.service.retry(job_id)
        job = self.h.wait(job_id)
        self.assertEqual(job.status, "done", job.error_message)

    def test_pause_resume_cancel(self):
        self.cdn.put("/hls3/index.m3u8", MEDIA_PLAYLIST, text=True)
        for index in range(3):
            self.cdn.put("/hls3/seg%d.ts" % index, b"z" * 256)
        self.h.settings["download_concurrency"] = 1
        created = self.h.service.create(
            "admin", {"urls": [self.cdn.url("/hls3/index.m3u8")]})
        job_id = created["jobs"][0]["job_id"]
        self.h.wait(job_id)
        # 终态不可再暂停/取消
        with self.assertRaises(ApiError):
            self.h.service.pause(job_id)
        with self.assertRaises(ApiError):
            self.h.service.cancel(job_id)
        # 重试后立即取消
        self.h.service.retry(job_id)
        self.h.service.cancel(job_id)
        job = self.h.wait(job_id)
        self.assertIn(job.status, ("cancelled", "done"))

    def test_job_limit(self):
        self.h.settings["download_job_limit"] = 10
        urls = [self.cdn.put("/files/slow%d.bin" % index, b"s" * 1024)
                for index in range(11)]
        with self.assertRaises(ApiError) as ctx:
            self.h.service.create("admin", {"urls": urls})
        self.assertEqual(ctx.exception.code, "JOB_LIMIT_REACHED")

    def test_group_overview_and_control(self):
        urls = [self.cdn.put("/files/g%d.bin" % i, b"g" * 2048)
                for i in range(2)]
        created = self.h.service.create(
            "admin", {"urls": urls, "group_title": "批量测试"})
        group_id = created["group"]["group_id"]
        for job in created["jobs"]:
            self.h.wait(job["job_id"])
        group = self.h.service.get_group(group_id)
        self.assertEqual(group["total"], 2)
        self.assertEqual(group["counts"]["done"], 2)
        self.assertTrue(group["is_done"])

    def test_disabled_setting_ignored(self):
        # v0058：下载是独立功能，download_enabled 键保留但不再生效。
        self.h.settings["download_enabled"] = False
        created = self.h.service.create("admin", {"urls": ["http://x.test/a"]})
        self.assertTrue(created.get("jobs"))
        self.assertTrue(created["jobs"][0].get("job_id"))

    def test_private_url_rejected_at_submit(self):
        self.h.settings["download_allow_private"] = False
        with self.assertRaises(ApiError) as ctx:
            self.h.service.create("admin",
                                  {"urls": ["http://127.0.0.1:9/x.mp4"]})
        self.assertEqual(ctx.exception.code, "URL_PRIVATE_ADDRESS")
        self.assertEqual(ctx.exception.status, 403)

    def test_bad_scheme_rejected_at_submit(self):
        with self.assertRaises(ApiError) as ctx:
            self.h.service.create("admin", {"urls": ["ftp://example.com/a"]})
        self.assertEqual(ctx.exception.code, "URL_UNSUPPORTED_SCHEME")

    def test_queue_snapshot_counts(self):
        urls = [self.cdn.put("/files/q%d.bin" % i, b"q" * 512)
                for i in range(3)]
        self.h.settings["download_concurrency"] = 1
        self.h.service.create("admin", {"urls": urls})
        snapshot = self.h.service.queue_snapshot()
        self.assertGreaterEqual(snapshot["counts"]["active"], 1)
        self.assertEqual(snapshot["counts"]["total"], 3)
        self.assertIn("concurrency", snapshot)


if __name__ == "__main__":
    unittest.main()
