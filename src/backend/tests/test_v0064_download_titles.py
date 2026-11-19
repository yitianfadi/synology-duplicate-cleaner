# -*- coding: utf-8 -*-
"""v0064 下载：压缩响应解压 + 按页面标题命名（docs/DOWNLOAD_CONTRACT.md §14/§17）。

覆盖两个真实世界的坑：
1. 站点对 `Accept-Encoding: identity` 也回压缩正文、却不带 `Content-Encoding`
   → 解析到的是乱码，看起来「解析不到地址」；
2. 从 m3u8 地址推出来的文件名是 `index.mp4` → 应按页面标题命名。
"""

from __future__ import absolute_import

import gzip
import http.server
import threading
import unittest
import zlib

from tests.test_v0051_downloads import DownloadHarness, FakeCdn

from dupcleaner import downloads, pageparse

# 实测站点标题形态：正在播放_<片名>- <站名>_<站名长尾>
SAMPLE_TITLE = (u"正在播放_【东北往事】国内禁播，尺度最大的国产合集- "
                u"一本道视频_不用下载就能看的在线吃瓜稀有黑料视频网站")
SAMPLE_NAME = u"【东北往事】国内禁播，尺度最大的国产合集"


class CleanTitleTest(unittest.TestCase):
    def test_site_noise_stripped(self):
        self.assertEqual(pageparse.clean_title(SAMPLE_TITLE), SAMPLE_NAME)

    def test_suffixes_and_prefixes(self):
        self.assertEqual(
            pageparse.clean_title(u"在线观看 某剧 1080p - 某视频网"), u"某剧 1080p")
        self.assertEqual(
            pageparse.clean_title(u"某片 全集 在线观看"), u"某片")
        self.assertEqual(
            pageparse.clean_title(u"Movie Name (2019) - Official Site"),
            u"Movie Name (2019)")

    def test_plain_and_empty(self):
        self.assertEqual(pageparse.clean_title(u"Plain Title"), u"Plain Title")
        self.assertEqual(pageparse.clean_title(u""), u"")
        self.assertEqual(pageparse.clean_title(u"   "), u"")

    def test_meta_title_priority(self):
        html = (u'<title>站点标题 - 某视频网</title>'
                u'<meta property="og:title" content="卡片标题">')
        self.assertEqual(pageparse.extract_title(html), u"卡片标题")
        self.assertEqual(
            pageparse.extract_title(u"<title>片名 - 某视频网</title>"), u"片名")
        self.assertEqual(pageparse.extract_title(u"<h1>一级标题</h1>"), u"一级标题")


class DecompressBodyTest(unittest.TestCase):
    def setUp(self):
        self.text = u"<html><title>压缩页面</title></html>"
        self.raw = self.text.encode("utf-8")

    def test_gzip_with_header(self):
        blob = gzip.compress(self.raw)
        self.assertEqual(downloads.decompress_body(blob, "gzip"), self.raw)
        self.assertEqual(downloads.decompress_body(blob, "GZIP"), self.raw)

    def test_gzip_without_header(self):
        # 站点回了 gzip 但漏掉 Content-Encoding：按魔数嗅探
        blob = gzip.compress(self.raw)
        self.assertEqual(downloads.decompress_body(blob, u""), self.raw)

    def test_deflate(self):
        self.assertEqual(
            downloads.decompress_body(zlib.compress(self.raw), "deflate"),
            self.raw)
        # 裸 deflate（无 zlib 头）
        compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
        bare = compressor.compress(self.raw) + compressor.flush()
        self.assertEqual(downloads.decompress_body(bare, "deflate"), self.raw)

    def test_identity_and_broken(self):
        self.assertEqual(downloads.decompress_body(self.raw, u""), self.raw)
        self.assertEqual(downloads.decompress_body(b"", "gzip"), b"")
        broken = b"\x1f\x8bnot-really-gzip"
        self.assertEqual(downloads.decompress_body(broken, "gzip"), broken)


class _PageHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        server.requests.append((self.path, self.headers.get("Accept-Encoding")))
        path = self.path.split("?")[0]
        payload = server.pages.get(path)
        if payload is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body, content_type, encoding = payload
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if encoding:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


class _GzipPageServer(object):
    """极简假站点：同一页面分别按「带头 gzip / 不带头 gzip / deflate」返回。"""

    def __init__(self):
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _PageHandler)
        self.httpd.pages = {}
        self.httpd.requests = []
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

    def put(self, path, body, content_type="application/octet-stream",
            encoding=None):
        self.httpd.pages[path] = (body, content_type, encoding)
        return self.url(path)


def _page_html(media_url):
    return (u"<!doctype html><html><head><title>%s</title></head><body>"
            u"<script>var player_aaaa={\"flag\":\"play\",\"encrypt\":0,"
            u"\"url\":\"%s\"}</script></body></html>" % (SAMPLE_TITLE, media_url))


class ReadRawTest(unittest.TestCase):
    def setUp(self):
        self.site = _GzipPageServer().start()
        self.h = DownloadHarness()

    def tearDown(self):
        self.h.cleanup()
        self.site.stop()

    def _publish(self):
        playlist = (u"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\n"
                    u"seg0.ts\n#EXT-X-ENDLIST\n")
        media_url = self.site.put(
            "/2026/hls/index.m3u8", playlist.encode("utf-8"),
            "application/vnd.apple.mpegurl")
        html = _page_html(media_url).encode("utf-8")
        self.site.put("/p/gzip.html", gzip.compress(html), "text/html", "gzip")
        # 没有 Content-Encoding 的压缩正文（实测 CDN 的坏行为）
        self.site.put("/p/raw.html", gzip.compress(html), "text/html")
        self.site.put("/p/deflate.html", zlib.compress(html), "text/html",
                      "deflate")
        return media_url

    def test_parse_gzip_page(self):
        media_url = self._publish()
        result = self.h.service.parse_target(
            "admin", {"url": self.site.url("/p/gzip.html")})
        self.assertEqual(result["title"], SAMPLE_NAME)
        urls = [item["url"] for item in result["candidates"]]
        self.assertIn(media_url, urls)
        # 文本类请求确实声明了 gzip（站点据此回压缩体）
        encodings = [item[1] for item in self.site.httpd.requests
                     if item[0].startswith("/p/")]
        self.assertTrue(encodings)
        self.assertTrue(all("gzip" in (value or "") for value in encodings))

    def test_parse_gzip_without_header(self):
        media_url = self._publish()
        result = self.h.service.parse_target(
            "admin", {"url": self.site.url("/p/raw.html")})
        self.assertEqual(result["title"], SAMPLE_NAME)
        self.assertIn(media_url,
                      [item["url"] for item in result["candidates"]])

    def test_parse_deflate_page(self):
        media_url = self._publish()
        result = self.h.service.parse_target(
            "admin", {"url": self.site.url("/p/deflate.html")})
        self.assertEqual(result["title"], SAMPLE_NAME)
        self.assertIn(media_url,
                      [item["url"] for item in result["candidates"]])


class TitleNamingTest(unittest.TestCase):
    def setUp(self):
        self.cdn = FakeCdn().start()
        self.h = DownloadHarness()

    def tearDown(self):
        self.h.cleanup()
        self.cdn.stop()

    def test_title_from_parse_used_for_filename(self):
        url = self.cdn.put("/media/clip.mp4", b"V" * (512 * 1024))
        created = self.h.service.create("admin", {
            "urls": [url],
            "titles": {url: SAMPLE_TITLE},
        })
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "done", job.error_message)
        self.assertEqual(job.title, SAMPLE_NAME)
        self.assertEqual(job.output_name, SAMPLE_NAME + ".mp4")

    def test_titles_ignore_unknown_and_bad_values(self):
        url = self.cdn.put("/media/clip.mp4", b"C" * (512 * 1024))
        created = self.h.service.create("admin", {
            "urls": [url],
            "titles": {
                "http://127.0.0.1:1/other.mp4": u"别的地址",
                url: u"",
                "x": 123,
            },
        })
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "done", job.error_message)
        # 没有可用标题 → 回退 URL 文件名（clip.mp4）
        self.assertEqual(job.title, u"clip")
        self.assertEqual(job.output_name, u"clip.mp4")

    def test_title_keeps_template_tokens(self):
        url = self.cdn.put("/media/clip.mp4", b"T" * (512 * 1024))
        created = self.h.service.create("admin", {
            "urls": [url],
            "titles": {url: u"  正在播放_片名A - 某视频网  "},
            "options": {"name_template": u"{title}-{site}"},
        })
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "done", job.error_message)
        self.assertEqual(job.title, u"片名A")
        self.assertTrue(job.output_name.startswith(u"片名A-"))
