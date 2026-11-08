# -*- coding: utf-8 -*-
"""v0049：任务检索/批量操作 + 权限体检 + 日志检索 测试。"""

import json
import os
import time
import unittest

from tests.helpers import Harness, write_bytes

from dupcleaner import config, permissions, subtitles


def make_job(service, name="movie.mkv", status=subtitles.STATUS_DONE,
             category=None, path=None):
    job = subtitles.SubtitleJob(
        subtitles.make_job_id(),
        path or os.path.join(service.config.data_dir, name),
        "auto", False, "zh", "admin")
    job.status = status
    job.skipped = False
    job.finished_epoch = time.time()
    if category:
        job.failure_category = category
        job.error = {"code": category.upper(), "message": "x"}
    service._register_job(job, enqueue=False)
    return job


class SubtitleFilterTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.service = self.harness.services.subtitles
        self.mkv = make_job(self.service, "movie-x265.mkv")
        self.mp4 = make_job(self.service, "clip.mp4",
                            path="/volume1/media/tv/clip.mp4")
        self.failed = make_job(self.service, "broken.mkv",
                               status=subtitles.STATUS_FAILED,
                               category="timeout")

    def tearDown(self):
        self.harness.cleanup()

    def test_filter_by_q(self):
        result = self.service.list(q="movie")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["job_id"], self.mkv.job_id)

    def test_filter_by_suffix(self):
        result = self.service.list(suffix=".mp4")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["job_id"], self.mp4.job_id)
        # 不带点也接受
        self.assertEqual(len(self.service.list(suffix="mkv")), 2)

    def test_filter_by_directory(self):
        result = self.service.list(directory="/volume1/media/tv")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["job_id"], self.mp4.job_id)

    def test_filter_by_category(self):
        result = self.service.list(status="failed", category="timeout")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["job_id"], self.failed.job_id)
        self.assertEqual(self.service.list(status="done", category="timeout"), [])

    def test_list_page_combined(self):
        page = self.service.list_page(limit=10, status="done", suffix=".mkv")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["jobs"][0]["job_id"], self.mkv.job_id)


class BulkActionTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.service = self.harness.services.subtitles
        self.jobs = []
        for index in range(3):
            job = subtitles.SubtitleJob(
                subtitles.make_job_id(),
                os.path.join(self.harness.dev_root, "m%d.mp4" % index),
                "auto", False, "zh", "admin")
            job.status = subtitles.STATUS_QUEUED
            self.service._register_job(job, enqueue=True)
            self.jobs.append(job)

    def tearDown(self):
        self.harness.cleanup()

    def test_bulk_cancel(self):
        result = self.service.bulk_action(
            "admin", [job.job_id for job in self.jobs], "cancel")
        self.assertEqual(result["succeeded"], 3)
        for job in self.jobs:
            self.assertEqual(job.status, subtitles.STATUS_CANCELLED)

    def test_bulk_priority(self):
        result = self.service.bulk_action(
            "admin", [self.jobs[2].job_id], "priority")
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(self.jobs[2].priority,
                         config.SUBTITLE_PRIORITY_MANUAL_TOP)

    def test_bulk_retry_failed(self):
        failed = make_job(self.service, "broken.mkv",
                          status=subtitles.STATUS_FAILED, category="network")
        result = self.service.bulk_action("admin", [failed.job_id], "retry")
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(failed.status, subtitles.STATUS_QUEUED)

    def test_bulk_invalid_action_and_limits(self):
        from dupcleaner.errors import ApiError
        with self.assertRaises(ApiError):
            self.service.bulk_action("admin", [self.jobs[0].job_id], "explode")
        with self.assertRaises(ApiError):
            self.service.bulk_action("admin", [], "cancel")
        with self.assertRaises(ApiError):
            self.service.bulk_action("admin",
                                     ["x"] * 201, "cancel")

    def test_bulk_partial_failure(self):
        result = self.service.bulk_action(
            "admin", [self.jobs[0].job_id, "sub-not-exist"], "cancel")
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        failed_entry = [item for item in result["results"] if not item["ok"]][0]
        self.assertTrue(failed_entry["message"])


class PermissionInspectorTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.videos = os.path.join(self.harness.dev_root, "videos")
        os.makedirs(self.videos, exist_ok=True)
        write_bytes(os.path.join(self.videos, "a.mp4"), b"x")
        self.harness.services.settings.update({
            "scheduled_scan_roots": [self.videos],
            "subtitle_watch_dirs": [self.videos],
        })
        self.inspector = self.harness.services.permissions

    def tearDown(self):
        self.harness.cleanup()

    def test_report_shape(self):
        report = self.inspector.report()
        self.assertIn("items", report)
        self.assertIn("summary", report)
        self.assertGreaterEqual(report["summary"]["total"], 1)
        item = [entry for entry in report["items"]
                if entry["path"] == self.videos][0]
        self.assertTrue(item["exists"])
        self.assertTrue(item["readable"])
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["guidance"], permissions.GUIDANCE["ok"])

    def test_report_missing_path_warns(self):
        missing = os.path.join(self.harness.dev_root, "nope")
        report = self.inspector.check_paths([missing])
        item = report["items"][0]
        self.assertFalse(item["exists"])
        self.assertEqual(item["code"], "missing")

    def test_report_detects_scan_permission_issues(self):
        self.harness.services.store.save_scan_meta("20260101-000000-abcdef", {
            "scan_id": "20260101-000000-abcdef",
            "status": "completed",
            "config": {"roots": [self.videos]},
            "stats": {"skipped": {"permission": 3}},
            "options": {},
        })
        report = self.inspector.report()
        self.assertTrue(any(issue.get("source") == "scan"
                            for issue in report["issues"]))
        self.assertTrue(report["guidance"])
        self.assertIn(u"只做检测", u" ".join(report["guidance"]))


class LogSearchApiTest(unittest.TestCase):
    def setUp(self):
        from tests.helpers import WebServer
        self.harness = Harness()
        self.server = WebServer(self.harness).start()
        self.client = self.server.client
        self.csrf = self.client.login()
        # 写两条日志 + 审计
        self.harness.services.store.write_log(
            "scan-20260101-000000-abcdef.log",
            u"scan log\nPERMISSION denied on /volume1/x\nok\n")
        self.harness.services.store.audit(
            "scan_end", "admin",
            {"scan_id": "20260101-000000-abcdef", "status": "completed"})

    def tearDown(self):
        self.server.stop()
        self.harness.cleanup()

    def test_search_by_job_id(self):
        status, _headers, payload = self.client.json(
            "GET", "/api/logs/search?job_id=20260101-000000-abcdef")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["logs"]), 1)
        self.assertGreaterEqual(payload["audit"].__len__(), 1)

    def test_search_by_keyword(self):
        status, _headers, payload = self.client.json(
            "GET", "/api/logs/search?q=PERMISSION")
        self.assertEqual(status, 200)
        self.assertEqual(payload["logs"][0]["match_count"], 1)
        self.assertIn("PERMISSION", payload["logs"][0]["matches"][0])

    def test_search_requires_query(self):
        status, _headers, payload = self.client.json("GET", "/api/logs/search")
        self.assertEqual(status, 400)

    def test_export_readable(self):
        status, headers, body = self.client.request(
            "GET", "/api/logs/export?q=PERMISSION")
        self.assertEqual(status, 200)
        header_text = repr(headers)
        self.assertIn("text/plain", header_text)
        text = body.decode("utf-8")
        self.assertIn(u"日志导出", text)
        self.assertIn("PERMISSION", text)


if __name__ == "__main__":
    unittest.main()
