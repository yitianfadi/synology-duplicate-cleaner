# -*- coding: utf-8 -*-
"""v0035 补翻译测试：派生仅翻译任务 / 两个接口 / 组快照 / 重启恢复。

覆盖契约（docs/SUBTITLE_TRANSLATE_CONTRACT.md）：
- 已完成 ASR（translate=false）任务补翻译 → 新任务 source=existing_srt、
  source_job_id 指向源任务、只调用 LLM、任务目录生成 <target>.srt 与
  bilingual.<target>.srt，原任务快照/文件不变；
- 已有完整译文 → TRANSLATION_EXISTS；translation_partial=true 可重译；
- 开关/密钥/确认/目标语言/源任务状态等错误码；
- 源文本缺失 → SOURCE_SRT_MISSING；视频已删除仍可从任务目录翻译；
- 幂等：同源已有排队/执行中的翻译任务 → created=False 复用同一 job；
- 组批量补翻译：分类统计、新组快照/options、源组只读；
- 组快照 translatable（count/partial，不做逐任务文件 stat）；
- 重启恢复：running/translating 且带 source_job_id → paused+resumable；
- 两个 HTTP 路由的状态码与响应形状。
"""

from __future__ import absolute_import

import os
import time
import unittest
from unittest import mock

from tests.helpers import WebServer, write_text
from tests.test_subtitles import SubtitleTestBase

from dupcleaner import subtitles, video_similarity
from dupcleaner.errors import ApiError

SRT_TEXT = (
    "1\n00:00:01,000 --> 00:00:03,000\nHello world\n\n"
    "2\n00:00:04,000 --> 00:00:06,000\nSecond line\n")


def _wait_until(predicate, timeout=20.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TranslateTestBase(SubtitleTestBase):
    """补翻译测试公共构造：已完成 ASR 任务、等待翻译任务/批次组结束。"""

    def done_asr_job(self, name="a.mp4", duration=60.0, translate=False,
                     **extra):
        video = self.make_video(name=name, duration=duration)
        payload = {"language": "auto", "translate": bool(translate),
                   "confirm_cloud": True}
        if translate:
            payload["target_lang"] = "zh"
        payload.update(extra)
        job = self.submit(video, **payload)
        done = self.wait_job(job["job_id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        return video, done

    def wait_translate_job(self, job_id, timeout=30.0):
        return self.wait_job(job_id, timeout=timeout)

    def wait_group(self, group_id, timeout=40.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = self.service.get_batch_group(group_id)
            if snapshot["status"] in ("done", "failed", "cancelled"):
                return snapshot
            time.sleep(0.02)
        raise AssertionError("subtitle translate group did not finish")

    def slow_translate(self, delay=0.3):
        real = self.service.client.translate

        def wrapper(*args, **kwargs):
            time.sleep(delay)
            return real(*args, **kwargs)

        self.service.client.translate = wrapper
        return real


class TranslateJobTest(TranslateTestBase):
    """单任务补翻译：派生任务、译文文件、原任务只读、幂等与错误码。"""

    def test_translate_asr_only_job_generates_files(self):
        video, source = self.done_asr_job(name="a.mp4")
        source_before = self.service.get(source["job_id"])
        asr_before = self.cloud.httpd.asr_calls
        llm_before = self.cloud.httpd.llm_calls
        # 只翻译任务不能调用 FFmpeg：available=False 时若调用会失败
        with mock.patch.object(self.service.ffmpeg, "available",
                               return_value=False):
            result = self.service.translate_job(
                "admin", source["job_id"], {"confirm_cloud": True})
            self.assertTrue(result["created"])
            self.assertEqual(result["source_job_id"], source["job_id"])
            new_job = result["job"]
            self.assertEqual(new_job["source"], "existing_srt")
            self.assertEqual(new_job["source_job_id"], source["job_id"])
            self.assertTrue(new_job["translate"])
            self.assertFalse(new_job["skip_existing"])
            self.assertEqual(new_job["target_lang"], "zh")
            self.assertEqual(new_job["output_mode"], source["output_mode"])
            group = result["group"]
            self.assertEqual(group["total"], 1)
            self.assertEqual(group["options"]["source"], "existing_srt")
            self.assertEqual(group["options"]["translated_from_job"],
                             source["job_id"])
            self.assertFalse(group["options"]["skip_existing"])
            done = self.wait_translate_job(new_job["job_id"])

        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertFalse(done["translation_partial"])
        self.assertEqual(self.cloud.httpd.asr_calls, asr_before,
                         "补翻译不能重新跑 ASR")
        self.assertGreater(self.cloud.httpd.llm_calls, llm_before)
        job_dir = self.service._job_dir(done["job_id"])
        self.assertTrue(os.path.isfile(os.path.join(job_dir, "original.srt")))
        zh_text = self.file_text(done["job_id"], "zh.srt")
        self.assertIn("T:chunk-cue", zh_text)
        self.assertTrue(os.path.isfile(
            os.path.join(job_dir, "bilingual.zh.srt")))
        # 写回视频同目录（源任务 output_mode=alongside_video）
        self.assertTrue(os.path.isfile(os.path.join(self.root, "a.zh.srt")))
        # 原任务 status/文件/消息/时间戳完全不变
        source_after = self.service.get(source["job_id"])
        for key in ("status", "files", "outputs", "message", "updated_at",
                    "translation_partial", "target_lang", "translate"):
            self.assertEqual(source_after[key], source_before[key], key)
        self.assertEqual(source_after["source_job_id"], None)
        # 审计：subtitle_start 带 operation=translate + source_job_id
        audits = self.harness.services.store.read_audit(limit=200)
        translate_audits = [
            item for item in audits
            if item.get("action") == "subtitle_start"
            and (item.get("detail") or {}).get("operation") == "translate"
            and (item.get("detail") or {}).get("job_id")
            == done["job_id"]]
        self.assertEqual(len(translate_audits), 1)
        self.assertEqual(translate_audits[0]["detail"]["source_job_id"],
                         source["job_id"])

    def test_translation_exists_rejected(self):
        _video, source = self.done_asr_job(name="full.mp4", translate=True)
        self.assertFalse(source["translation_partial"])
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            self.service.translate_job(
                "admin", source["job_id"], {"confirm_cloud": True})
        self.assertEqual(ctx.exception.code, "TRANSLATION_EXISTS")

    def test_partial_translation_can_retranslate(self):
        self.cloud.httpd.llm_mode = "always_wrong"
        _video, source = self.done_asr_job(name="partial.mp4", translate=True)
        self.assertTrue(source["translation_partial"])
        self.cloud.httpd.llm_mode = "ok"
        result = self.service.translate_job(
            "admin", source["job_id"],
            {"confirm_cloud": True, "target_lang": "en"})
        self.assertTrue(result["created"])
        self.assertEqual(result["job"]["target_lang"], "en")
        done = self.wait_translate_job(result["job"]["job_id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertFalse(done["translation_partial"])
        self.assertTrue(self.service.get_file(done["job_id"], "en.srt"))
        self.assertTrue(os.path.isfile(
            os.path.join(self.service._job_dir(done["job_id"]),
                         "bilingual.en.srt")))

    def test_translate_switch_key_and_confirm_errors(self):
        _video, source = self.done_asr_job(name="switch.mp4")
        payload = {"confirm_cloud": True}
        self._configure_settings(subtitle_enabled=False)
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            self.service.translate_job("admin", source["job_id"], payload)
        self.assertEqual(ctx.exception.code, "SUBTITLE_DISABLED")
        self._configure_settings(subtitle_translate_enabled=False)
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            self.service.translate_job("admin", source["job_id"], payload)
        self.assertEqual(ctx.exception.code, "TRANSLATE_DISABLED")
        # 密钥为空：空字符串=保持原值，需 clear 标记才能真正清空
        self._configure_settings(subtitle_llm_api_key="",
                                 subtitle_clear_llm_api_key=True)
        self.assertFalse((self.harness.services.settings.get()
                          or {}).get("subtitle_llm_api_key"))
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            self.service.translate_job("admin", source["job_id"], payload)
        self.assertEqual(ctx.exception.code, "TRANSLATE_NOT_CONFIGURED")
        self._configure_settings()
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            self.service.translate_job("admin", source["job_id"],
                                       {"confirm_cloud": False})
        self.assertEqual(ctx.exception.code, "CLOUD_CONFIRM_REQUIRED")
        for bad_target in ("auto", "中文", 123):
            with self.assertRaises(ApiError) as ctx:
                self.service.translate_job(
                    "admin", source["job_id"],
                    {"confirm_cloud": True, "target_lang": bad_target})
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        # 非法 payload 类型
        with self.assertRaises(ApiError) as ctx:
            self.service.translate_job("admin", source["job_id"], None)
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_not_done_and_unknown_source(self):
        real = self.service.client.transcribe

        def slow(*args, **kwargs):
            time.sleep(0.3)
            return real(*args, **kwargs)

        self.service.client.transcribe = slow
        video = self.make_video(name="slow.mp4", duration=60)
        job = self.submit(video)
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            self.service.translate_job("admin", job["job_id"],
                                       {"confirm_cloud": True})
        self.assertEqual(ctx.exception.code, "JOB_NOT_DONE")
        with self.assertRaises(ApiError) as ctx:
            self.service.translate_job(
                "admin", "sub-20260101-000000-abcdef", {"confirm_cloud": True})
        self.assertEqual(ctx.exception.code, "NOT_FOUND")
        self.assertEqual(self.wait_job(job["job_id"])["status"], "done")

    def test_source_srt_missing(self):
        video, source = self.done_asr_job(name="gone.mp4",
                                         output_mode="download_only")
        # 删除源任务目录 original.srt（download_only 没写视频同目录），再删视频
        os.unlink(os.path.join(self.service._job_dir(source["job_id"]),
                               "original.srt"))
        os.unlink(video)
        with self.assertRaises(subtitles.SubtitleError) as ctx:
            self.service.translate_job("admin", source["job_id"],
                                       {"confirm_cloud": True})
        self.assertEqual(ctx.exception.code, "SOURCE_SRT_MISSING")
        self.assertEqual(ctx.exception.message, u"源字幕已不存在，请重新生成")

    def test_video_deleted_still_translates_from_job_dir(self):
        video, source = self.done_asr_job(name="nofile.mp4")
        os.unlink(video)
        result = self.service.translate_job(
            "admin", source["job_id"], {"confirm_cloud": True})
        self.assertTrue(result["created"])
        done = self.wait_translate_job(result["job"]["job_id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertTrue(self.service.get_file(done["job_id"], "zh.srt"))
        self.assertTrue(os.path.isfile(
            os.path.join(self.service._job_dir(done["job_id"]),
                         "bilingual.zh.srt")))

    def test_translate_idempotent_while_active(self):
        _video, source = self.done_asr_job(name="idem.mp4")
        self.slow_translate(0.3)
        first = self.service.translate_job(
            "admin", source["job_id"], {"confirm_cloud": True})
        self.assertTrue(first["created"])
        second = self.service.translate_job(
            "admin", source["job_id"], {"confirm_cloud": True})
        self.assertFalse(second["created"])
        self.assertEqual(second["source_job_id"], source["job_id"])
        self.assertEqual(second["job"]["job_id"], first["job"]["job_id"])
        self.assertIsNotNone(second["group"])
        done = self.wait_translate_job(first["job"]["job_id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        # 完成后不再幂等：再次补翻译会派生新任务（源任务仍是 ASR-only）
        third = self.service.translate_job(
            "admin", source["job_id"], {"confirm_cloud": True})
        self.assertTrue(third["created"])
        self.assertNotEqual(third["job"]["job_id"], first["job"]["job_id"])
        self.assertEqual(
            self.wait_translate_job(third["job"]["job_id"])["status"], "done")


class TranslateBatchGroupTest(TranslateTestBase):
    """整组补翻译：分类统计、新组快照/options、源组只读、translatable 快照。"""

    GROUP_ID = "grp-20260101-000000-abcdef"
    BATCH_ID = "bat-20260101-000000-abcdef"

    def make_source_job(self, index, status, translate=False, partial=False,
                        has_text=True, output_mode=None):
        job_id = "sub-20260101-%06d-%06x" % (index, index + 1)
        job = subtitles.SubtitleJob(
            job_id, os.path.join(self.root, "m%02d.mp4" % index), "auto",
            bool(translate), "zh" if translate else "", "admin",
            group_id=self.GROUP_ID, skip_existing=False,
            output_mode=output_mode)
        job.status = status
        job.message = u""
        job.translation_partial = bool(partial)
        job.created_epoch = 1000.0 + index
        job.updated_epoch = job.created_epoch + 1
        self.service._register_job(job, enqueue=False)
        if has_text:
            write_text(os.path.join(self.service._job_dir(job_id),
                                    "original.srt"), SRT_TEXT)
        return job

    def save_source_group(self, jobs):
        now = time.time()
        self.service._save_batch_record({
            "batch_id": self.BATCH_ID,
            "group_id": self.GROUP_ID,
            "created_epoch": now,
            "updated_epoch": now,
            "user": "admin",
            "options": {"source": "asr", "language": "auto",
                        "translate": False, "skip_existing": False},
            "job_ids": [job.job_id for job in jobs],
            "rejected": [],
            "duplicates": [],
        })
        record = {
            "group_id": self.GROUP_ID,
            "created_epoch": now,
            "updated_epoch": now,
            "user": "admin",
            "options": {"source": "asr", "language": "auto",
                        "translate": False, "skip_existing": False},
            "batch_ids": [self.BATCH_ID],
            "rejected": [],
            "duplicates": [],
        }
        self.service._save_group_record(record)
        return record

    def new_group_jobs(self, group_snapshot, batch_index=0):
        batch_id = group_snapshot["batches"][batch_index]["batch_id"]
        return self.service.get_batch(batch_id)["jobs"]

    def test_batch_translate_mixed_summary_and_group(self):
        asr_a = self.make_source_job(0, "done", translate=False)
        asr_b = self.make_source_job(1, "done", translate=False,
                                     output_mode="download_only")
        partial = self.make_source_job(2, "done", translate=True, partial=True)
        full = self.make_source_job(3, "done", translate=True, partial=False)
        no_text = self.make_source_job(4, "done", translate=False,
                                       has_text=False)
        failed = self.make_source_job(5, "failed")
        queued = self.make_source_job(6, "queued")
        self.save_source_group([asr_a, asr_b, partial, full, no_text,
                                failed, queued])

        result = self.service.translate_batch_group(
            "admin", self.GROUP_ID, {"confirm_cloud": True})
        summary = result["summary"]
        self.assertEqual(result["created"], 3)
        self.assertEqual(summary["eligible"], 3)
        self.assertEqual(summary["created"], 3)
        self.assertEqual(summary["already_translated"], 1)
        self.assertEqual(summary["missing_source"], 1)
        self.assertEqual(summary["duplicates"], 0)
        self.assertEqual(summary["rejected"], [])
        group = result["group"]
        self.assertIsNotNone(group)
        self.assertEqual(group["total"], 3)
        self.assertEqual(group["batch_count"], 1)
        options = group["options"]
        self.assertEqual(options["source"], "existing_srt")
        self.assertTrue(options["translate"])
        self.assertEqual(options["target_lang"], "zh")
        self.assertFalse(options["skip_existing"])
        self.assertFalse(options["recursive"])
        self.assertEqual(options["translated_from_group"], self.GROUP_ID)
        self.assertNotIn("translated_from_job", options)
        # 新组任务 source_job_id 逐条指向源任务，且 task 内只翻译
        new_jobs = self.new_group_jobs(group)
        self.assertEqual([item["source"] for item in new_jobs],
                         ["existing_srt"] * 3)
        self.assertEqual(
            sorted(item["source_job_id"] for item in new_jobs),
            sorted([asr_a.job_id, asr_b.job_id, partial.job_id]))
        self.assertTrue(all(item["translate"] for item in new_jobs))
        # 源组只读：任务数/计数/options 不变
        source_group = self.service.get_batch_group(self.GROUP_ID)
        self.assertEqual(source_group["total"], 7)
        self.assertEqual(source_group["counts"]["done"], 5)
        self.assertEqual(source_group["counts"]["failed"], 1)
        self.assertEqual(source_group["counts"]["queued"], 1)
        self.assertEqual(source_group["options"]["source"], "asr")
        # translatable 快照：done+translate=false 3 个，done+partial 1 个
        self.assertEqual(source_group["translatable"],
                         {"count": 3, "partial": 1})
        # 新组跑完
        done = self.wait_group(group["group_id"])
        self.assertEqual(done["status"], "done", done.get("counts"))
        self.assertEqual(done["counts"]["done"], 3)
        # 审计：subtitle_translate_group（源组、创建数）
        audits = self.harness.services.store.read_audit(limit=200)
        group_audits = [item for item in audits
                        if item.get("action") == "subtitle_translate_group"
                        and (item.get("detail") or {}).get(
                            "source_group_id") == self.GROUP_ID]
        self.assertEqual(len(group_audits), 1)
        self.assertEqual(group_audits[0]["detail"]["group_id"],
                         group["group_id"])
        self.assertEqual(group_audits[0]["detail"]["created"], 3)

    def test_batch_translate_duplicates_and_noop(self):
        asr_only = self.make_source_job(0, "done", translate=False)
        self.save_source_group([asr_only])
        self.slow_translate(0.3)
        first = self.service.translate_batch_group(
            "admin", self.GROUP_ID, {"confirm_cloud": True})
        self.assertEqual(first["created"], 1)
        second = self.service.translate_batch_group(
            "admin", self.GROUP_ID, {"confirm_cloud": True})
        self.assertEqual(second["created"], 0)
        self.assertIsNone(second["group"])
        self.assertEqual(second["summary"]["eligible"], 1)
        self.assertEqual(second["summary"]["duplicates"], 1)
        self.assertEqual(second["summary"]["created"], 0)
        self.assertEqual(first["group"]["total"], 1)
        self.assertEqual(self.wait_group(first["group"]["group_id"])["status"],
                         "done")

        # 组内只有 failed/queued → 没有可翻译任务，仍返回 created=0
        other_group = "grp-20260101-000001-abcdef"
        other_batch = "bat-20260101-000001-abcdef"
        failed = self.make_source_job(7, "failed")
        queued = self.make_source_job(8, "queued")
        now = time.time()
        self.service._save_batch_record({
            "batch_id": other_batch, "group_id": other_group,
            "created_epoch": now, "updated_epoch": now, "user": "admin",
            "options": {}, "job_ids": [failed.job_id, queued.job_id],
            "rejected": [], "duplicates": [],
        })
        self.service._save_group_record({
            "group_id": other_group, "created_epoch": now,
            "updated_epoch": now, "user": "admin", "options": {},
            "batch_ids": [other_batch], "rejected": [], "duplicates": [],
        })
        noop = self.service.translate_batch_group(
            "admin", other_group, {"confirm_cloud": True})
        self.assertEqual(noop["created"], 0)
        self.assertIsNone(noop["group"])
        self.assertEqual(noop["summary"]["eligible"], 0)
        self.assertEqual(noop["summary"]["created"], 0)

    def test_group_snapshot_translatable_real_groups(self):
        videos = [self.make_video("t%d.mp4" % index, duration=60)
                  for index in range(3)]
        group = self.service.create_batch_group("admin", {
            "paths": videos, "language": "auto", "translate": False,
            "confirm_cloud": True})
        done = self.wait_group(group["group_id"])
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["translatable"], {"count": 3, "partial": 0})
        # translate=true 且 LLM 持续失败 → 3 个 partial 任务可重译
        self.cloud.httpd.llm_mode = "always_wrong"
        group2 = self.service.create_batch_group("admin", {
            "paths": [self.make_video("p%d.mp4" % index, duration=60)
                      for index in range(3)],
            "language": "auto", "translate": True, "target_lang": "zh",
            "confirm_cloud": True})
        done2 = self.wait_group(group2["group_id"])
        self.assertEqual(done2["status"], "done")
        self.assertEqual(done2["translatable"], {"count": 0, "partial": 3})


class TranslateRestartTest(TranslateTestBase):
    """服务重启：带 source_job_id 的 running/translating 任务转 paused 可继续。"""

    def reloaded_service(self):
        reloaded = subtitles.SubtitleService(
            self.harness.config, self.harness.services.store,
            self.harness.services.shares,
            self.harness.services.settings.get,
            client=subtitles.OpenAIClient(retry_delay=0.01),
            ffmpeg=video_similarity.FFmpegTool(cmd=self.ffmpeg_cmd))
        self.addCleanup(reloaded.shutdown, 2)
        return reloaded

    def save_record(self, job_id, status, source_job_id=None):
        now = time.time()
        record = {
            "job_id": job_id,
            "status": status,
            "stage": "translate",
            "progress": 80,
            "message": u"正在翻译字幕",
            "path": os.path.join(self.root, "restart.mp4"),
            "language": "auto",
            "translate": True,
            "target_lang": "zh",
            "user": "admin",
            "source": "existing_srt",
            "output_mode": "download_only",
            "skip_existing": False,
            "audio_source": "nas",
            "source_job_id": source_job_id,
            "created_epoch": now,
            "updated_epoch": now,
            "error": None,
            "files": [],
            "outputs": [],
            "output_errors": [],
            "skipped": False,
            "warning": None,
            "translation_partial": False,
            "chunk_index": 0,
            "chunk_total": 0,
            "resumable": False,
        }
        self.harness.services.store.save_subtitle_job(job_id, record)
        return record

    def test_restart_pauses_translate_jobs(self):
        ids = []
        for index, status in enumerate(("running", "translating")):
            job_id = "sub-20260101-00000%d-abcdef" % index
            ids.append(job_id)
            self.save_record(job_id, status,
                             source_job_id="sub-20260101-0000ff-abcdef")
        reloaded = self.reloaded_service()
        for job_id in ids:
            job = reloaded.get(job_id)
            self.assertEqual(job["status"], "paused")
            self.assertTrue(job["resumable"])
            self.assertIsNone(job["error"])
            self.assertEqual(job["message"], u"服务重启导致翻译暂停，可继续")
            self.assertEqual(job["source_job_id"],
                             "sub-20260101-0000ff-abcdef")
            stored = self.harness.services.store.load_subtitle_job(job_id)
            self.assertEqual(stored["status"], "paused")
            self.assertTrue(stored["resumable"])
            self.assertIsNone(stored["error"])
            self.assertEqual(stored["message"],
                             u"服务重启导致翻译暂停，可继续")

    def test_restart_non_translate_running_still_fails(self):
        job_id = "sub-20260101-000010-abcdef"
        self.save_record(job_id, "running", source_job_id=None)
        reloaded = self.reloaded_service()
        job = reloaded.get(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "SERVICE_RESTART")
        self.assertFalse(job["resumable"])
        self.assertIsNone(job["source_job_id"])


class TranslateApiTest(TranslateTestBase):
    """两个补翻译 HTTP 路由：状态码、响应形状、错误码映射。"""

    def setUp(self):
        TranslateTestBase.setUp(self)
        self.web = WebServer(self.harness).start()
        self.client = self.web.client
        self.csrf = self.client.login()

    def tearDown(self):
        try:
            self.web.stop()
        finally:
            TranslateTestBase.tearDown(self)

    def test_translate_job_api(self):
        _video, source = self.done_asr_job(name="api.mp4")
        self.slow_translate(0.3)
        status, _headers, payload = self.client.json(
            "POST", "/api/subtitles/%s/translate" % source["job_id"],
            {"confirm_cloud": True}, csrf=self.csrf)
        self.assertEqual(status, 202, payload)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["created"])
        self.assertEqual(payload["source_job_id"], source["job_id"])
        self.assertEqual(payload["job"]["source_job_id"], source["job_id"])
        self.assertEqual(payload["group"]["total"], 1)
        # 同源 active → 200 幂等，同一 job
        status, _headers, payload2 = self.client.json(
            "POST", "/api/subtitles/%s/translate" % source["job_id"],
            {"confirm_cloud": True}, csrf=self.csrf)
        self.assertEqual(status, 200, payload2)
        self.assertFalse(payload2["created"])
        self.assertEqual(payload2["job"]["job_id"], payload["job"]["job_id"])
        # 缺 CSRF → 403
        status, _headers, csrf_payload = self.client.json(
            "POST", "/api/subtitles/%s/translate" % source["job_id"],
            {"confirm_cloud": True})
        self.assertEqual(status, 403)
        self.assertEqual(csrf_payload["error"]["code"], "CSRF_FAILED")
        # 翻译开关关闭 → 400 TRANSLATE_DISABLED
        self._configure_settings(subtitle_translate_enabled=False)
        status, _headers, disabled = self.client.json(
            "POST", "/api/subtitles/%s/translate" % source["job_id"],
            {"confirm_cloud": True}, csrf=self.csrf)
        self.assertEqual(status, 400, disabled)
        self.assertEqual(disabled["error"]["code"], "TRANSLATE_DISABLED")
        self._configure_settings()
        # 任务不存在 → 404
        status, _headers, missing = self.client.json(
            "POST", "/api/subtitles/sub-20260101-000000-abcdef/translate",
            {"confirm_cloud": True}, csrf=self.csrf)
        self.assertEqual(status, 404, missing)
        self.assertEqual(missing["error"]["code"], "NOT_FOUND")
        self.assertEqual(
            self.wait_translate_job(payload["job"]["job_id"])["status"], "done")

    def test_batch_group_translate_api(self):
        videos = [self.make_video("api%d.mp4" % index, duration=60)
                  for index in range(2)]
        group = self.service.create_batch_group("admin", {
            "paths": videos, "language": "auto", "translate": False,
            "confirm_cloud": True})
        self.wait_group(group["group_id"])
        # 组快照带 translatable
        status, _headers, snapshot = self.client.json(
            "GET", "/api/subtitles/batch-groups/%s" % group["group_id"])
        self.assertEqual(status, 200, snapshot)
        self.assertEqual(snapshot["group"]["translatable"],
                         {"count": 2, "partial": 0})

        status, _headers, payload = self.client.json(
            "POST",
            "/api/subtitles/batch-groups/%s/translate" % group["group_id"],
            {"confirm_cloud": True}, csrf=self.csrf)
        self.assertEqual(status, 202, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["created"], 2)
        self.assertEqual(payload["summary"]["eligible"], 2)
        self.assertEqual(payload["summary"]["created"], 2)
        self.assertEqual(payload["summary"]["already_translated"], 0)
        self.assertEqual(payload["summary"]["rejected"], [])
        self.assertEqual(payload["group"]["total"], 2)
        self.assertEqual(payload["group"]["options"]["translated_from_group"],
                         group["group_id"])
        self.assertEqual(self.wait_group(payload["group"]["group_id"])["status"],
                         "done")
        # 第二次（新任务已完成）不再幂等；但对已翻译来源组再次整组翻译
        # 仍会派生新任务：这里改为验证“无可翻译任务”组返回 202 created=0。
        full_group = self.service.create_batch_group("admin", {
            "paths": [self.make_video("api-full%d.mp4" % index,
                                      duration=60) for index in range(2)],
            "language": "auto", "translate": True, "target_lang": "zh",
            "confirm_cloud": True})
        self.wait_group(full_group["group_id"])
        status, _headers, noop = self.client.json(
            "POST",
            "/api/subtitles/batch-groups/%s/translate"
            % full_group["group_id"],
            {"confirm_cloud": True}, csrf=self.csrf)
        self.assertEqual(status, 202, noop)
        self.assertEqual(noop["created"], 0)
        self.assertIsNone(noop["group"])
        self.assertEqual(noop["summary"]["already_translated"], 2)
        # 不存在的组 → 404
        status, _headers, missing = self.client.json(
            "POST",
            "/api/subtitles/batch-groups/grp-20260101-000000-abcdef/translate",
            {"confirm_cloud": True}, csrf=self.csrf)
        self.assertEqual(status, 404, missing)
        self.assertEqual(missing["error"]["code"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
