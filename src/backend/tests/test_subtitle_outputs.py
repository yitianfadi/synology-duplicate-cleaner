# -*- coding: utf-8 -*-
"""v0036 译文输出组合：both（默认）/ target_only / bilingual_only。"""

from __future__ import absolute_import

import os
import unittest

from tests.test_subtitles import SRT_RESPONSE, SubtitleTestBase

from dupcleaner import settings as settings_module
from dupcleaner.errors import ApiError


def _job_dir_files(service, job_id):
    return set(os.listdir(service._job_dir(job_id)))


class TranslateOutputsSettingsTest(unittest.TestCase):
    def test_default_and_validation(self):
        defaults = settings_module.default_settings()
        self.assertEqual(defaults["subtitle_translate_outputs"], "both")
        for value in ("both", "target_only", "bilingual_only"):
            cleaned = settings_module.validate_patch(
                {"subtitle_translate_outputs": value})
            self.assertEqual(cleaned["subtitle_translate_outputs"], value)
        with self.assertRaises(ApiError):
            settings_module.validate_patch(
                {"subtitle_translate_outputs": "target-and-bilingual"})
        with self.assertRaises(ApiError):
            settings_module.validate_patch(
                {"subtitle_translate_outputs": "BOTH"})


class TranslateOutputsWriteTest(SubtitleTestBase):
    def test_default_both_outputs(self):
        video = self.make_video("both.mp4")
        job = self.submit(video, translate=True)
        result = self.wait_job(job["job_id"])
        self.assertEqual(result["status"], "done", result.get("message"))
        self.assertEqual(result["target_lang"], "zh")
        names = _job_dir_files(self.service, job["job_id"])
        self.assertIn("original.srt", names)
        self.assertIn("zh.srt", names)
        self.assertIn("bilingual.zh.srt", names)
        directory = os.path.dirname(video)
        self.assertTrue(os.path.isfile(os.path.join(directory, "both.srt")))
        self.assertTrue(os.path.isfile(
            os.path.join(directory, "both.zh.srt")))
        self.assertTrue(os.path.isfile(
            os.path.join(directory, "both.bilingual.zh.srt")))

    def test_target_only_outputs(self):
        self._configure_settings(subtitle_translate_outputs="target_only")
        video = self.make_video("target-only.mp4")
        job = self.submit(video, translate=True)
        result = self.wait_job(job["job_id"])
        self.assertEqual(result["status"], "done", result.get("message"))
        names = _job_dir_files(self.service, job["job_id"])
        self.assertIn("zh.srt", names)
        self.assertNotIn("bilingual.zh.srt", names)
        directory = os.path.dirname(video)
        self.assertTrue(os.path.isfile(
            os.path.join(directory, "target-only.zh.srt")))
        self.assertFalse(os.path.isfile(
            os.path.join(directory, "target-only.bilingual.zh.srt")))

    def test_bilingual_only_outputs(self):
        self._configure_settings(subtitle_translate_outputs="bilingual_only")
        video = self.make_video("bilingual-only.mp4")
        job = self.submit(video, translate=True)
        result = self.wait_job(job["job_id"])
        self.assertEqual(result["status"], "done", result.get("message"))
        names = _job_dir_files(self.service, job["job_id"])
        self.assertIn("original.srt", names)
        self.assertIn("bilingual.zh.srt", names)
        self.assertNotIn("zh.srt", names)
        directory = os.path.dirname(video)
        self.assertTrue(os.path.isfile(
            os.path.join(directory, "bilingual-only.bilingual.zh.srt")))
        self.assertFalse(os.path.isfile(
            os.path.join(directory, "bilingual-only.zh.srt")))

    def test_vtt_follows_translate_outputs(self):
        self._configure_settings(subtitle_write_vtt=True,
                                 subtitle_translate_outputs="target_only")
        video = self.make_video("vtt-target.mp4")
        job = self.submit(video, translate=True)
        result = self.wait_job(job["job_id"])
        self.assertEqual(result["status"], "done", result.get("message"))
        names = _job_dir_files(self.service, job["job_id"])
        self.assertIn("original.vtt", names)
        self.assertIn("zh.vtt", names)
        self.assertNotIn("bilingual.zh.srt", names)

        self._configure_settings(subtitle_write_vtt=True,
                                 subtitle_translate_outputs="bilingual_only")
        video2 = self.make_video("vtt-bilingual.mp4")
        job2 = self.submit(video2, translate=True)
        result2 = self.wait_job(job2["job_id"])
        self.assertEqual(result2["status"], "done", result2.get("message"))
        names2 = _job_dir_files(self.service, job2["job_id"])
        self.assertIn("original.vtt", names2)
        self.assertIn("bilingual.zh.srt", names2)
        # 没有 target srt 时不生成 target vtt，避免孤立文件
        self.assertNotIn("zh.vtt", names2)
        self.assertNotIn("zh.srt", names2)

    def test_existing_srt_translate_follows_output_mode(self):
        self._configure_settings(subtitle_translate_outputs="target_only")
        video = self.make_video("srt-source.mp4")
        srt_path = os.path.join(os.path.dirname(video), "srt-source.srt")
        with open(srt_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(SRT_RESPONSE)
        job = self.submit(video, source="existing_srt", translate=True)
        result = self.wait_job(job["job_id"])
        self.assertEqual(result["status"], "done", result.get("message"))
        names = _job_dir_files(self.service, job["job_id"])
        self.assertIn("zh.srt", names)
        self.assertNotIn("bilingual.zh.srt", names)
        directory = os.path.dirname(video)
        self.assertTrue(os.path.isfile(
            os.path.join(directory, "srt-source.zh.srt")))
        self.assertFalse(os.path.isfile(
            os.path.join(directory, "srt-source.bilingual.zh.srt")))

    def test_supplement_translate_follows_output_mode(self):
        # v0035 补翻译派生任务同样遵循当前译文输出组合设置
        video = self.make_video("supplement.mp4")
        source = self.submit(video, translate=False)
        source_result = self.wait_job(source["job_id"])
        self.assertEqual(source_result["status"], "done")
        self._configure_settings(subtitle_translate_outputs="bilingual_only")
        result = self.service.translate_job(
            "admin", source["job_id"], {"confirm_cloud": True})
        self.assertTrue(result["created"])
        translated = self.wait_job(result["job"]["job_id"])
        self.assertEqual(translated["status"], "done",
                         translated.get("message"))
        names = _job_dir_files(self.service, translated["job_id"])
        self.assertIn("bilingual.zh.srt", names)
        self.assertNotIn("zh.srt", names)


if __name__ == "__main__":
    unittest.main()
