# -*- coding: utf-8 -*-
"""v0052 播放网页解析：pageparse 纯函数 + /api/downloads/parse 服务行为。"""

from __future__ import absolute_import

import os
import unittest

from tests.helpers import write_bytes
from tests.test_v0051_downloads import FakeCdn, DownloadHarness

from dupcleaner import pageparse
from dupcleaner.errors import ApiError

PAGE_HTML = u"""<!doctype html>
<html><head><title>示例播放页 - 测试</title>
<meta property="og:video" content="https://cdn.example.com/preview.mp4">
<meta property="og:title" content="示例">
<script type="application/ld+json">
{"@type": "VideoObject", "contentUrl": "https://cdn.example.com/ld.json.mp4",
 "embedUrl": "https://example.com/embed/1"}
</script>
</head><body>
<video id="v1" src="/media/plain.mp4" poster="p.jpg"></video>
<video id="v2" data-src="https://cdn.example.com/data-src.m3u8"></video>
<source src="//cdn.example.com/protocol-relative.mp4" type="video/mp4">
<link rel="preload" as="video" href="https://cdn.example.com/preload.mp4">
<script>
var player = {file: "/hls/master.m3u8", poster: "poster.jpg"};
hls.loadSource('https:\\u002F\\u002Fcdn.example.com\\u002Fhls\\u002Fother.m3u8');
var direct = "https://cdn.example.com/a/b\\/c.mp4?token=1&amp;x=2";
</script>
</body></html>"""


class PageParseTest(unittest.TestCase):
    def test_extract_from_various_sources(self):
        candidates = pageparse.extract_candidates(
            PAGE_HTML, "https://example.com/watch/1")
        urls = [item["url"] for item in candidates]
        self.assertIn("https://cdn.example.com/preview.mp4", urls)      # meta
        self.assertIn("https://cdn.example.com/ld.json.mp4", urls)      # json-ld
        self.assertIn("https://example.com/media/plain.mp4", urls)      # video src
        self.assertIn("https://cdn.example.com/data-src.m3u8", urls)    # data-src
        self.assertIn("https://cdn.example.com/protocol-relative.mp4",
                      urls)                                             # //
        self.assertIn("https://cdn.example.com/preload.mp4", urls)      # preload
        self.assertIn("https://example.com/hls/master.m3u8", urls)      # 相对绝对路径
        self.assertIn("https://cdn.example.com/hls/other.m3u8", urls)   # \u002F 转义
        self.assertIn("https://cdn.example.com/a/b/c.mp4?token=1&x=2",
                      urls)                                             # \/ 与实体
        kinds = {item["url"]: item["kind"] for item in candidates}
        self.assertEqual(kinds["https://cdn.example.com/preview.mp4"], "file")
        self.assertEqual(kinds["https://cdn.example.com/data-src.m3u8"], "hls")
        # m3u8 分数高于普通直链，排序在前
        self.assertTrue(candidates[0]["kind"] in ("hls", "dash"))

    def test_embed_urls_marked(self):
        candidates = pageparse.extract_candidates(
            PAGE_HTML, "https://example.com/watch/1")
        embed = [item for item in candidates
                 if item["url"] == "https://example.com/embed/1"]
        self.assertTrue(embed)
        self.assertEqual(embed[0]["kind"], "embed")

    def test_dedupe_and_limit(self):
        html = u"".join(
            u'<script>var a%d="https://cdn.example.com/x.m3u8";</script>' % i
            for i in range(5))
        candidates = pageparse.extract_candidates(html,
                                                  "https://example.com/")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["url"],
                         "https://cdn.example.com/x.m3u8")

    def test_drops_ignored_schemes(self):
        html = (u'<video src="data:video/mp4;base64,AAA"></video>'
                u'<script>var u="blob:https://example.com/1";</script>'
                u'<script>var v="javascript:void(0)";</script>')
        self.assertEqual(pageparse.extract_candidates(
            html, "https://example.com/"), [])

    def test_title_and_empty(self):
        self.assertEqual(pageparse.extract_title(PAGE_HTML),
                         u"示例播放页 - 测试")
        self.assertEqual(pageparse.extract_title(u"<html></html>"), u"")
        self.assertEqual(pageparse.extract_candidates(
            u"<html>nothing</html>", "https://x.test/"), [])

    def test_decode_body_charsets(self):
        text = u"中文页面"
        self.assertEqual(
            pageparse.decode_body(text.encode("gb18030"), "text/html"), text)
        self.assertEqual(
            pageparse.decode_body(text.encode("utf-8"),
                                  "text/html; charset=utf-8"), text)
        self.assertEqual(
            pageparse.decode_body(
                (u'<meta charset="gbk">' + text).encode("gb18030"),
                "text/html"), u'<meta charset="gbk">' + text)

    def test_summarize(self):
        candidates = pageparse.extract_candidates(
            PAGE_HTML, "https://example.com/watch/1")
        summary = pageparse.summarize(candidates)
        self.assertIn(u"HLS", summary)
        self.assertIn(u"直链", summary)


class CurlParseTest(unittest.TestCase):
    def test_basic_headers(self):
        text = ("curl 'https://example.com/watch/1' \\\n"
                "  -H 'Referer: https://example.com/' \\\n"
                "  -H 'cookie: sid=abc; theme=dark' \\\n"
                "  -H 'User-Agent: Mozilla/5.0 Test'")
        parsed = pageparse.parse_curl(text)
        self.assertEqual(parsed["url"], "https://example.com/watch/1")
        self.assertEqual(parsed["headers"]["referer"],
                         "https://example.com/")
        self.assertEqual(parsed["headers"]["cookie"], "sid=abc; theme=dark")
        self.assertEqual(parsed["headers"]["user_agent"], "Mozilla/5.0 Test")

    def test_double_quotes_and_user_agent_flag(self):
        text = ('curl "https://example.com/a" --header "Referer: https://r/" '
                '-A "Custom/1.0"')
        parsed = pageparse.parse_curl(text)
        self.assertEqual(parsed["url"], "https://example.com/a")
        self.assertEqual(parsed["headers"]["referer"], "https://r/")
        self.assertEqual(parsed["headers"]["user_agent"], "Custom/1.0")

    def test_powershell_continuation(self):
        text = ("curl 'https://example.com/p' `\n"
                "  -H 'referer: https://x/'")
        parsed = pageparse.parse_curl(text)
        self.assertEqual(parsed["url"], "https://example.com/p")
        self.assertEqual(parsed["headers"]["referer"], "https://x/")

    def test_missing_url(self):
        with self.assertRaises(pageparse.PageParseError) as ctx:
            pageparse.parse_curl("curl -H 'a: b'")
        self.assertEqual(ctx.exception.code, "URL_INVALID")


class ParseServiceTest(unittest.TestCase):
    def setUp(self):
        self.cdn = FakeCdn().start()
        self.h = DownloadHarness()

    def tearDown(self):
        self.h.cleanup()
        self.cdn.stop()

    def test_parse_page_url(self):
        media = (u"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\n"
                 u"seg0.ts\n#EXT-X-ENDLIST\n")
        base = self.cdn.url("").rstrip("/")
        self.cdn.put("/data-src.m3u8", media, text=True)
        self.cdn.put("/p/page.html",
                     PAGE_HTML.replace("https://cdn.example.com", base),
                     text=True)
        result = self.h.service.parse_target(
            "admin", {"url": self.cdn.url("/p/page.html")})
        self.assertTrue(result["ok"])
        self.assertIn(u"示例播放页", result["title"])
        urls = [item["url"] for item in result["candidates"]]
        self.assertIn(base + "/data-src.m3u8", urls)
        hls_item = [item for item in result["candidates"]
                    if item["url"] == base + "/data-src.m3u8"][0]
        self.assertTrue(hls_item.get("valid"))
        self.assertTrue(result["parse_session"])

    def test_parse_pasted_html(self):
        result = self.h.service.parse_target(
            "admin", {"html": PAGE_HTML,
                      "url": "https://example.com/watch/1"})
        self.assertEqual(result["page_url"], "https://example.com/watch/1")
        self.assertGreaterEqual(len(result["candidates"]), 5)
        # 粘贴源码没有网络校验，valid 字段缺省
        self.assertNotIn("valid", result["candidates"][0])

    def test_parse_direct_playlist_url(self):
        self.cdn.put("/m/master.m3u8",
                     u"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,"
                     u"RESOLUTION=1280x720\n720/index.m3u8\n"
                     u"#EXT-X-STREAM-INF:BANDWIDTH=3000000,"
                     u"RESOLUTION=1920x1080\n1080/index.m3u8\n", text=True)
        result = self.h.service.parse_target(
            "admin", {"url": self.cdn.url("/m/master.m3u8")})
        self.assertEqual(result["candidates"][0]["kind"], "hls")
        self.assertEqual(result["playlist"]["kind"], "master")
        self.assertEqual(len(result["playlist"]["variants"]), 2)
        self.assertEqual(result["playlist"]["variants"][0]["label"], "1080p")

    def test_parse_curl_carries_headers_into_job(self):
        self.cdn.put("/c/page.html",
                     u'<html><body><video src="/c/v.m3u8"></video></body></html>',
                     text=True)
        self.cdn.put("/c/v.m3u8",
                     u"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\n"
                     u"seg0.ts\n#EXT-X-ENDLIST\n", text=True)
        self.cdn.put("/c/seg0.ts", b"s" * 128)
        curl = ("curl '%s' -H 'Referer: https://ref.example.com/' "
                "-H 'Cookie: sid=SECRET-TOKEN'" % self.cdn.url("/c/page.html"))
        parsed = self.h.service.parse_target("admin", {"curl": curl})
        candidates = [item for item in parsed["candidates"]
                      if item["url"].endswith("/c/v.m3u8")]
        self.assertTrue(candidates)
        created = self.h.service.create("admin", {
            "urls": [candidates[0]["url"]],
            "options": {"parse_session": parsed["parse_session"]},
        })
        job = self.h.service.get(created["jobs"][0]["job_id"])
        self.assertEqual(job.options.get("cookie"), "sid=SECRET-TOKEN")
        self.assertEqual(job.options.get("referer"),
                         "https://ref.example.com/")
        view = self.h.service.get_view(job.job_id)
        self.assertTrue(view["options"].get("cookie_set"))
        self.assertNotIn("cookie", view["options"])

    def test_parse_ignores_disabled_setting(self):
        # v0058：download_enabled 键保留但不再生效，解析照常工作。
        media = (u"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\n"
                 u"seg0.ts\n#EXT-X-ENDLIST\n")
        base = self.cdn.url("").rstrip("/")
        self.cdn.put("/data-src.m3u8", media, text=True)
        self.cdn.put("/p/page.html",
                     PAGE_HTML.replace("https://cdn.example.com", base),
                     text=True)
        self.h.settings["download_enabled"] = False
        result = self.h.service.parse_target(
            "admin", {"url": self.cdn.url("/p/page.html")})
        self.assertTrue(result["ok"])

    def test_parse_empty_page_hint(self):
        self.cdn.put("/e/page.html", u"<html><body>nothing</body></html>",
                     text=True)
        result = self.h.service.parse_target(
            "admin", {"url": self.cdn.url("/e/page.html")})
        self.assertEqual(result["candidates"], [])
        self.assertTrue(result["hint"])

    def test_parse_private_url_rejected(self):
        self.h.settings["download_allow_private"] = False
        with self.assertRaises(ApiError) as ctx:
            self.h.service.parse_target(
                "admin", {"url": "http://127.0.0.1:9/page.html"})
        self.assertEqual(ctx.exception.code, "URL_PRIVATE_ADDRESS")

    def test_parse_requires_input(self):
        with self.assertRaises(ApiError) as ctx:
            self.h.service.parse_target("admin", {})
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")


if __name__ == "__main__":
    unittest.main()
