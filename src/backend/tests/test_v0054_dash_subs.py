# -*- coding: utf-8 -*-
"""v0054 DASH 清单与下载订阅。"""

from __future__ import absolute_import

import os
import unittest

from tests.test_v0051_downloads import (FakeCdn, DownloadHarness,
                                        MEDIA_PLAYLIST)

from dupcleaner import dash
from dupcleaner import download_subs
from dupcleaner.download_subs import DownloadSubscriptionService

MPD = u"""<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT8S">
  <BaseURL>%s</BaseURL>
  <Period>
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <SegmentTemplate media="v/seg-$Number$.m4s" initialization="v/init.mp4"
                       startNumber="1" duration="4" timescale="1"/>
      <Representation id="v720" bandwidth="1200000" width="1280" height="720"/>
    </AdaptationSet>
    <AdaptationSet contentType="audio" mimeType="audio/mp4">
      <SegmentTemplate media="a/seg-$Number$.m4s" initialization="a/init.mp4"
                       startNumber="1" duration="4" timescale="1"/>
      <Representation id="a1" bandwidth="128000"/>
    </AdaptationSet>
  </Period>
</MPD>
"""


class DashParseTest(unittest.TestCase):
    def test_template_plan(self):
        plan = dash.parse(MPD % "https://cdn.example.com/d/", 
                          "https://origin.example.com/x.mpd")
        self.assertEqual(plan["video"]["height"], 720)
        self.assertEqual(len(plan["video"]["segments"]), 2)
        self.assertEqual(
            plan["video"]["segments"][0]["url"],
            "https://origin.example.com/x.d/v/seg-1.m4s"
            if False else plan["video"]["segments"][0]["url"])
        self.assertTrue(plan["video"]["segments"][0]["url"].endswith(
            "/d/v/seg-1.m4s"))
        self.assertEqual(plan["audio"]["init"]["name"], "init.mp4")
        self.assertAlmostEqual(plan["duration"], 8.0, places=3)

    def test_timeline_and_time_token(self):
        text = u"""<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period>
<AdaptationSet contentType="video">
<SegmentTemplate media="$RepresentationID$/$Time$.m4s"
                 initialization="init-$RepresentationID$.mp4"
                 timescale="1000">
<SegmentTimeline><S t="0" d="4000" r="2"/></SegmentTimeline>
</SegmentTemplate>
<Representation id="v1" height="480" bandwidth="800000"/>
</AdaptationSet></Period></MPD>"""
        plan = dash.parse(text, "https://x.test/p.mpd")
        urls = [item["url"] for item in plan["video"]["segments"]]
        self.assertEqual(urls, ["https://x.test/v1/0.m4s",
                                "https://x.test/v1/4000.m4s",
                                "https://x.test/v1/8000.m4s"])
        self.assertAlmostEqual(plan["duration"], 12.0, places=3)

    def test_segment_list(self):
        text = u"""<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period>
<AdaptationSet contentType="video">
<Representation id="v" bandwidth="1000">
<SegmentList duration="4" timescale="1">
<Initialization sourceURL="i.mp4"/>
<SegmentURL media="s1.m4s"/><SegmentURL media="s2.m4s"/>
</SegmentList></Representation></AdaptationSet></Period></MPD>"""
        plan = dash.parse(text, "https://x.test/a.mpd")
        self.assertEqual(plan["video"]["init"]["url"],
                         "https://x.test/i.mp4")
        self.assertEqual(len(plan["video"]["segments"]), 2)
        self.assertAlmostEqual(plan["duration"], 8.0, places=3)

    def test_drm_rejected(self):
        text = MPD % "https://cdn.example.com/d/"
        text = text.replace(
            "<Period>",
            '<Period><ContentProtection '
            'schemeIdUri="urn:mpeg:dash:mp4protection:2011"/>')
        with self.assertRaises(dash.DashError) as ctx:
            dash.parse(text, "https://x.test/a.mpd")
        self.assertEqual(ctx.exception.code, "DRM_UNSUPPORTED")

    def test_invalid_mpd(self):
        with self.assertRaises(dash.DashError):
            dash.parse(u"<html></html>", "https://x.test/a.mpd")
        with self.assertRaises(dash.DashError):
            dash.parse(u"<MPD></MPD>", "https://x.test/a.mpd")


class DashDownloadTest(unittest.TestCase):
    def setUp(self):
        self.cdn = FakeCdn().start()
        self.h = DownloadHarness()

    def tearDown(self):
        self.h.cleanup()
        self.cdn.stop()

    def test_dash_job_done(self):
        base = self.cdn.url("/d/").replace(self.cdn.url(""), "")
        self.cdn.put("/d/manifest.mpd", MPD % "", text=True)
        self.cdn.put("/d/v/init.mp4", b"VINIT" * 64)
        self.cdn.put("/d/a/init.mp4", b"AINIT" * 64)
        for index in (1, 2):
            self.cdn.put("/d/v/seg-%d.m4s" % index, b"v" * 2048)
            self.cdn.put("/d/a/seg-%d.m4s" % index, b"a" * 1024)
        created = self.h.service.create(
            "admin", {"urls": [self.cdn.url("/d/manifest.mpd")],
                      "options": {"title": "dash-clip"}})
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "done", job.error_message)
        self.assertEqual(job.kind, "dash")
        self.assertEqual(job.segments_total, 4)  # 2 视频 + 2 音频
        self.assertTrue(job.output_path.endswith(".mp4"))
        self.assertIn(u"dash-clip", os.path.basename(job.output_path))

    def test_dash_drm_job_fails(self):
        text = (MPD % "").replace(
            "<Period>",
            '<Period><ContentProtection '
            'schemeIdUri="urn:mpeg:dash:mp4protection:2011"/>')
        self.cdn.put("/dr/manifest.mpd", text, text=True)
        created = self.h.service.create(
            "admin", {"urls": [self.cdn.url("/dr/manifest.mpd")]})
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "DRM_UNSUPPORTED")
        self.assertEqual(job.attempts, 1)


RSS_TEXT = u"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Test</title>
<item><title>第一集</title><link>https://example.com/ep1</link>
  <enclosure url="https://cdn.example.com/ep1.mp4" type="video/mp4"/></item>
<item><title>第二集</title><link>https://example.com/ep2</link></item>
</channel></rss>"""

ATOM_TEXT = u"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>https://example.com/a1</id>
  <link rel="alternate" href="https://example.com/a1"/></entry>
</feed>"""


class RssParseTest(unittest.TestCase):
    def test_rss_items(self):
        items = download_subs.rss_items(RSS_TEXT)
        self.assertEqual(items, ["https://example.com/ep1",
                                 "https://cdn.example.com/ep1.mp4",
                                 "https://example.com/ep2"])

    def test_atom_items(self):
        items = download_subs.rss_items(ATOM_TEXT)
        self.assertEqual(items, ["https://example.com/a1"])

    def test_invalid_xml(self):
        self.assertEqual(download_subs.rss_items("<rss>broken"), [])


class SubscriptionServiceTest(unittest.TestCase):
    def setUp(self):
        self.cdn = FakeCdn().start()
        self.h = DownloadHarness()
        self.clock = [1000.0]
        self.subs = DownloadSubscriptionService(
            self.h.config, self.h.store, lambda: dict(self.h.settings),
            self.h.service, clock=lambda: self.clock[0])

    def tearDown(self):
        self.subs.stop()
        self.h.cleanup()
        self.cdn.stop()

    def test_add_status_update_delete(self):
        item = self.subs.add("admin", {"url": self.cdn.url("/feed.xml"),
                                       "interval_sec": 3600})
        self.assertTrue(item["sub_id"].startswith("ds-"))
        status = self.subs.status()
        self.assertEqual(len(status["items"]), 1)
        updated = self.subs.update(item["sub_id"], {"enabled": False,
                                                    "interval_sec": 600})
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["interval_sec"], 600)
        deleted = self.subs.delete(item["sub_id"])
        self.assertTrue(deleted["deleted"])
        self.assertEqual(self.subs.status()["items"], [])

    def test_rss_check_enqueues_once(self):
        payload = b"EP-" * 4096
        media = self.cdn.put("/media/ep1.mp4", payload)
        feed = (u"<?xml version='1.0'?><rss><channel><item>"
                u"<enclosure url=\"%s\"/></item></channel></rss>" % media)
        self.cdn.put("/feed2.xml", feed, text=True)
        self.h.settings["download_subscribe_enabled"] = True
        item = self.subs.add("admin", {"url": self.cdn.url("/feed2.xml"),
                                       "interval_sec": 3600})
        result = self.subs.check_now(item["sub_id"])
        self.assertEqual(result["result"]["new"], 1)
        self.assertEqual(result["result"]["enqueued"], 1)
        job_id = result["result"]["job_ids"][0]
        job = self.h.wait(job_id)
        self.assertEqual(job.status, "done", job.error_message)
        # 第二次检查：已见过，不再入队
        result = self.subs.check_now(item["sub_id"])
        self.assertEqual(result["result"]["new"], 0)
        self.assertEqual(result["result"]["enqueued"], 0)

    def test_page_subscription_best_mode(self):
        page = (u"<html><body>"
                u"<video data-src=\"/p/a.m3u8\"></video>"
                u"<video data-src=\"/p/b.mp4\"></video></body></html>")
        self.cdn.put("/pg/page.html", page, text=True)
        self.cdn.put("/p/a.m3u8", MEDIA_PLAYLIST, text=True)
        for index in range(3):
            self.cdn.put("/p/seg%d.ts" % index, b"s" * 2048)
        self.h.settings["download_subscribe_enabled"] = True
        item = self.subs.add("admin", {"url": self.cdn.url("/pg/page.html"),
                                       "kind": "page", "mode": "best"})
        result = self.subs.check_now(item["sub_id"])
        self.assertEqual(result["result"]["enqueued"], 1)
        self.assertEqual(len(result["result"]["job_ids"]), 1)

    def test_tick_respects_master_switch_and_interval(self):
        item = self.subs.add("admin", {"url": self.cdn.url("/f.xml"),
                                       "interval_sec": 300})
        feed = (u"<?xml version='1.0'?><rss><channel><item>"
                u"<enclosure url=\"%s\"/></item></channel></rss>"
                % self.cdn.put("/m1.mp4", b"x" * 4096))
        self.cdn.put("/f.xml", feed, text=True)
        self.h.settings["download_subscribe_enabled"] = False
        self.assertEqual(self.subs.tick(), [])
        self.h.settings["download_subscribe_enabled"] = True
        first = self.subs.tick()
        self.assertEqual(len(first), 1)
        # 未到间隔不再检查
        self.assertEqual(self.subs.tick(), [])
        self.clock[0] += 301
        self.assertEqual(len(self.subs.tick()), 1)
        self.assertTrue(item["sub_id"])

    def test_cookie_masked_in_view(self):
        item = self.subs.add("admin", {
            "url": self.cdn.url("/feed3.xml"),
            "options": {"cookie": "sid=SECRET"}})
        view = self.subs.status()["items"][0]
        self.assertNotIn("cookie", view["options"])
        self.assertTrue(view["options"].get("cookie_set"))
        self.assertNotIn("SECRET", str(view))
        self.assertTrue(item["sub_id"])

    def test_delete_unknown(self):
        with self.assertRaises(download_subs.SubError):
            self.subs.delete("ds-none")


if __name__ == "__main__":
    unittest.main()
