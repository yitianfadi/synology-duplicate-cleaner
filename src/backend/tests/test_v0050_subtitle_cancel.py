# -*- coding: utf-8 -*-
"""v0050：字幕批次组批量取消 / 强制删除 / 任务明细筛选测试。"""

import json
import os
import time
import unittest

from tests.helpers import Harness

from dupcleaner import subtitles


def make_job(service, path, status=subtitles.STATUS_QUEUED, group_id=None,
             batch_id=None):
    job = subtitles.SubtitleJob(
        subtitles.make_job_id(), path, "auto", False, "zh", "admin")
    job.status = status
    job.group_id = group_id
    job.batch_id = batch_id
    if status == subtitles.STATUS_RUNNING:
        job.stage = "asr"
    return job


def make_group(service, count=5, status=subtitles.STATUS_QUEUED,
               suffix=".mp4", base=None):
    group_id = subtitles.make_group_id()
    batch_id = subtitles.make_batch_id()
    base = base or service.config.data_dir
    jobs = []
    for index in range(count):
        path = os.path.join(base, "g-%d%s" % (index, suffix))
        job = make_job(service, path, status=status, group_id=group_id,
                       batch_id=batch_id)
        service._register_job(
            job, enqueue=status in (subtitles.STATUS_QUEUED,
                                    subtitles.STATUS_PAUSED))
        jobs.append(job)
    service._save_batch_record({
        "batch_id": batch_id, "group_id": group_id,
        "created_epoch": time.time(), "updated_epoch": time.time(),
        "user": "admin", "options": {},
        "job_ids": [job.job_id for job in jobs],
        "rejected": [], "duplicates": [],
    })
    service._save_group_record({
        "group_id": group_id, "created_epoch": time.time(),
        "updated_epoch": time.time(), "user": "admin", "options": {},
        "batch_ids": [batch_id],
        "job_ids": [job.job_id for job in jobs],
        "rejected": [], "duplicates": [],
    })
    return group_id, batch_id, jobs


class BatchCancelTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.service = self.harness.services.subtitles

    def tearDown(self):
        self.harness.cleanup()

    def test_cancel_small_group_is_sync_and_queue_empty(self):
        group_id, _, jobs = make_group(self.service, count=5)
        self.assertEqual(len(self.service._queue), 5)
        snapshot = self.service.cancel_batch_group(group_id)
        self.assertEqual(snapshot["group_id"], group_id)
        self.assertEqual(
            [job.status for job in jobs],
            [subtitles.STATUS_CANCELLED] * 5)
        self.assertEqual(len(self.service._queue), 0)
        self.assertEqual(snapshot["counts"].get("cancelled"), 5)
        self.assertTrue(snapshot["cancel_state"]["done"])

    def test_cancel_is_idempotent_while_running(self):
        group_id, _, jobs = make_group(self.service, count=200)
        original = self.service._persist
        calls = {"count": 0}

        def slow_persist(job):
            calls["count"] += 1
            time.sleep(0.02)
            original(job)

        self.service._persist = slow_persist
        try:
            first = self.service.cancel_batch_group(group_id)
            self.assertTrue(first.get("cancelling"))
            second = self.service.cancel_batch_group(group_id)
            self.assertTrue(second.get("cancelling"))
            self.assertEqual(first["requested"], second["requested"])
            deadline = time.time() + 10
            while time.time() < deadline:
                state = self.service._cancel_states.get(group_id) or {}
                if state.get("done"):
                    break
                time.sleep(0.02)
        finally:
            self.service._persist = original
        state = self.service._cancel_states[group_id]
        self.assertTrue(state["done"])
        self.assertEqual(state["persisted"], 200)
        self.assertEqual(len(self.service._queue), 0)
        self.assertTrue(all(job.status == subtitles.STATUS_CANCELLED
                            for job in jobs))

    def test_running_job_marks_cancelling_then_cancelled(self):
        group_id, _, jobs = make_group(self.service, count=1,
                                       status=subtitles.STATUS_RUNNING)
        job = jobs[0]
        result = self.service.cancel_group_remaining(group_id, {"scope": "all"})
        self.assertTrue(result["cancelling"])
        deadline = time.time() + 5
        while time.time() < deadline:
            state = self.service._cancel_states.get(group_id) or {}
            if state.get("done"):
                break
            time.sleep(0.02)
        self.assertEqual(job.status, subtitles.STATUS_RUNNING)
        snapshot = job.snapshot()
        self.assertTrue(snapshot["cancelling"])
        self.assertEqual(
            self.service.get_batch_group(group_id)["counts"].get("cancelling"),
            1)
        # 模拟 worker 在分片边界完成取消（真实 worker 由 _handle 的取消路径落地）
        self.service.cancel(job.job_id)
        with self.service._lock:
            job.status = subtitles.STATUS_CANCELLED
            job.finished_epoch = time.time()
        self.service._persist(job)
        self.assertEqual(job.status, subtitles.STATUS_CANCELLED)

    def test_cancel_remaining_filter_only_matching(self):
        group_id, _, jobs = make_group(self.service, count=2, suffix=".mp4")
        # 追加一个 .mkv 任务到同一个组
        batch_record = self.service._load_batch_record(
            self.service._load_group_record(group_id)["batch_ids"][0])
        extra = make_job(self.service,
                         os.path.join(self.harness.dev_root, "extra.mkv"),
                         group_id=group_id,
                         batch_id=batch_record["batch_id"])
        self.service._register_job(extra, enqueue=True)
        batch_record["job_ids"].append(extra.job_id)
        self.service._save_batch_record(batch_record)
        group_record = self.service._load_group_record(group_id)
        group_record["job_ids"].append(extra.job_id)
        self.service._save_group_record(group_record)
        result = self.service.cancel_group_remaining(
            group_id, {"scope": "filter", "status": "queued", "suffix": ".mp4"})
        self.assertEqual(result["requested"], 2)
        self.assertEqual(extra.status, subtitles.STATUS_QUEUED)
        self.assertTrue(all(job.status == subtitles.STATUS_CANCELLED
                            for job in jobs))

    def test_delete_terminal_group_removes_records_and_jobs(self):
        group_id, _, jobs = make_group(self.service, count=3)
        self.service.cancel_batch_group(group_id)
        deadline = time.time() + 5
        while time.time() < deadline:
            if (self.service._cancel_states.get(group_id) or {}).get("done"):
                break
            time.sleep(0.02)
        result = self.service.delete_batch_group(group_id)
        self.assertTrue(result["deleted"])
        with self.assertRaises(Exception):
            self.service._load_group_record(group_id)
        self.assertFalse(any(job.job_id in self.service._jobs for job in jobs))
        self.assertFalse(os.path.isfile(self.service._job_json(jobs[0].job_id)))

    def test_delete_active_group_requires_force_and_then_deletes(self):
        group_id, _, jobs = make_group(self.service, count=3)
        from dupcleaner.errors import ApiError
        with self.assertRaises(ApiError) as ctx:
            self.service.delete_batch_group(group_id)
        self.assertEqual(ctx.exception.status, 409)
        result = self.service.delete_batch_group(group_id, force=True)
        self.assertTrue(result.get("deleting") or result.get("deleted"))
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                self.service._load_group_record(group_id)
            except Exception:
                break
            time.sleep(0.05)
        with self.assertRaises(Exception):
            self.service._load_group_record(group_id)
        self.assertFalse(any(job.job_id in self.service._jobs for job in jobs))

    def test_recover_pending_cancel_after_restart(self):
        group_id, _, jobs = make_group(self.service, count=4)
        state = {"group_id": group_id, "source": "cancel", "requested": 4,
                 "cancelling": 0, "persisted": 0, "done": False}
        with open(self.service._cancel_file, "w", encoding="utf-8") as handle:
            json.dump({"cancels": {group_id: state}, "deletes": {}},
                      handle)
        self.service._recover_pending_cancels()
        self.assertTrue(all(job.status == subtitles.STATUS_CANCELLED
                            for job in jobs))
        self.assertFalse(os.path.isfile(self.service._cancel_file))
        snapshot = self.service.get_batch_group(group_id)
        self.assertTrue(snapshot["cancel_state"]["done"])


if __name__ == "__main__":
    unittest.main()
