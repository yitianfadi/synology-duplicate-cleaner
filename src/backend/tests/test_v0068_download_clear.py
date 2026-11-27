# -*- coding: utf-8 -*-
"""v0068：清空「已完成」下载任务（记录删除，文件保留）。

覆盖：默认范围（全部已结束）、done/failed/cancelled 细分范围、
活动任务不受影响、已下载文件保留、下载组 job_ids 同步裁剪、
非法范围报 400、未知动作仍被拒绝。
"""

from __future__ import absolute_import

import os
import time
import unittest

from tests.test_v0051_downloads import DownloadHarness, FakeCdn

from dupcleaner.errors import ApiError
from dupcleaner.downloads import DownloadJob


class ClearFinishedTest(unittest.TestCase):
    def setUp(self):
        self.cdn = FakeCdn().start()
        self.h = DownloadHarness()

    def tearDown(self):
        self.h.cleanup()
        self.cdn.stop()

    # -- 工具 -----------------------------------------------------------
    def _done_job(self, name):
        payload = b"CLEAR-" * 512
        url = self.cdn.put("/files/%s.bin" % name, payload)
        created = self.h.service.create(
            "admin", {"urls": [url], "options": {"title": name}})
        job = self.h.wait(created["jobs"][0]["job_id"])
        self.assertEqual(job.status, "done", job.error_message)
        return job

    def _fake_job(self, job_id, status):
        job = DownloadJob(job_id, "http://127.0.0.1/none", options={})
        job.status = status
        job.stage = status
        job.finished_epoch = time.time()
        self.h.service._jobs[job.job_id] = job
        return job

    def _queue_ids(self, snapshot):
        ids = []
        for key in ("active", "queued", "paused", "recent"):
            for item in snapshot.get(key) or []:
                ids.append(item.get("job_id"))
        return ids

    # -- 范围 -----------------------------------------------------------
    def test_clear_done_keeps_file(self):
        job = self._done_job("keep")
        path = job.output_path
        result = self.h.service.bulk_action("admin", "clear_finished", "done")
        self.assertEqual(result["action"], "clear_finished")
        self.assertEqual(result["scope"], "done")
        self.assertEqual(result["matched"], result["affected"])
        self.assertGreaterEqual(result["affected"], 1)
        self.assertEqual(result["counts"].get("done"), 0)
        self.assertEqual(result["counts"].get("total"), 0)
        # 只删记录：产物文件仍在磁盘上
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.access(path, os.R_OK))
        with self.assertRaises(ApiError):
            self.h.service.get(job.job_id)
        self.assertEqual(self.h.store.count_download_jobs(), 0)

    def test_clear_default_removes_all_finished(self):
        self._done_job("all")
        self._fake_job("dl-failed-1", "failed")
        self._fake_job("dl-cancelled-1", "cancelled")
        active = self._fake_job("dl-queued-1", "queued")
        result = self.h.service.bulk_action("admin", "clear_finished")
        self.assertEqual(result["scope"], "all")
        self.assertEqual(result["affected"], 3)
        self.assertEqual(result["counts"].get("done"), 0)
        self.assertEqual(result["counts"].get("failed"), 0)
        self.assertEqual(result["counts"].get("cancelled"), 0)
        # 排队中的任务不受影响
        self.assertIsNotNone(self.h.service.get(active.job_id))
        self.assertEqual(self._queue_ids(self.h.service.queue_snapshot()),
                         [active.job_id])

    def test_scope_failed_only(self):
        self._done_job("keep-done")
        self._fake_job("dl-failed-2", "failed")
        self._fake_job("dl-cancelled-2", "cancelled")
        result = self.h.service.bulk_action("admin", "clear_finished",
                                            "failed")
        self.assertEqual(result["affected"], 1)
        counts = result["counts"]
        self.assertEqual(counts.get("failed"), 0)
        self.assertEqual(counts.get("done"), 1)
        self.assertEqual(counts.get("cancelled"), 1)

    def test_scope_failed_cancelled(self):
        self._done_job("keep-done2")
        self._fake_job("dl-failed-3", "failed")
        self._fake_job("dl-cancelled-3", "cancelled")
        result = self.h.service.bulk_action("admin", "clear_finished",
                                            "failed_cancelled")
        self.assertEqual(result["affected"], 2)
        counts = result["counts"]
        self.assertEqual(counts.get("failed"), 0)
        self.assertEqual(counts.get("cancelled"), 0)
        self.assertEqual(counts.get("done"), 1)

    def test_invalid_scope_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            self.h.service.bulk_action("admin", "clear_finished", "bogus")
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_unknown_action_still_rejected(self):
        with self.assertRaises(ApiError):
            self.h.service.bulk_action("admin", "explode", "all")

    def test_clear_without_finished_is_noop(self):
        self._fake_job("dl-queued-2", "queued")
        result = self.h.service.bulk_action("admin", "clear_finished", "done")
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["affected"], 0)
        self.assertEqual(result["counts"].get("queued"), 1)

    # -- 下载组 ---------------------------------------------------------
    def test_clear_prunes_group_job_ids(self):
        job = self._done_job("grouped")
        group_id = job.group_id
        self.assertTrue(group_id)
        self.h.service.bulk_action("admin", "clear_finished", "done")
        group = self.h.service.get_group(group_id)
        self.assertEqual(group.get("job_ids"), [])
        self.assertEqual(group.get("finished") or 0, 0)
