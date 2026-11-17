# -*- coding: utf-8 -*-
"""v0056 格式转换（docs/CONVERT_CONTRACT.md）：预设参数金标 + 服务流程 + 真 FFmpeg。

分三层：
1) 纯函数：预设表、探测解析、进度解析、参数构造、兼容性、体积预估；
2) 服务：提交/队列/输出命名/覆盖策略/取消/持久化/失败清理（FFmpeg 用假实现，
   不依赖本机是否装了 ffmpeg）；
3) 真转码：本机有 ffmpeg 时才跑（生成 2 秒 testsrc 样本，覆盖 copy/转码/提取音频）。
"""

from __future__ import absolute_import

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from tests.helpers import BACKEND_DIR  # noqa: F401

from dupcleaner import config, convert
from dupcleaner import settings as settings_module
from dupcleaner.errors import ApiError
from dupcleaner.store import Store
from dupcleaner.video_similarity import FFmpegTool

PROBE_TEXT = u"""Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'sample.mp4':
  Metadata:
    major_brand     : isom
  Duration: 00:01:00.00, start: 0.000000, bitrate: 1200 kb/s
  Stream #0:0(und): Video: h264 (High), yuv420p(tv, bt709, progressive), 1920x1080 [SAR 1:1 DAR 16:9], 25 fps, 25 tbr, 90k tbn
  Stream #0:1(eng): Audio: aac (LC), 48000 Hz, stereo, fltp, 128 kb/s (default)
  Stream #0:2(chi): Subtitle: subrip (default)
"""

AUDIO_ONLY_PROBE = u"""Input #0, mp3, from 'song.mp3':
  Duration: 00:03:00.00, start: 0.000000, bitrate: 128 kb/s
  Stream #0:0: Audio: mp3, 44100 Hz, stereo, fltp, 128 kb/s
"""


def _probe_info(text=PROBE_TEXT):
    return convert.parse_probe_text(text)


class PresetTableTest(unittest.TestCase):
    def test_ids_unique_and_lookup(self):
        self.assertEqual(len(convert.PRESET_IDS), len(set(convert.PRESET_IDS)))
        self.assertEqual(len(convert.PRESET_IDS), 16)
        self.assertEqual(len(convert.PRESETS), len(convert.PRESET_MAP))
        for preset in convert.PRESETS:
            self.assertIn(preset["group"], ("video", "audio"))
            self.assertTrue(preset["ext"].startswith("."))
            self.assertIn(preset["id"], convert.PRESET_MAP)

    def test_presets_cover_expected_formats(self):
        for preset_id in ("mp4_h264", "mp4_h265", "mkv_h264", "mkv_copy",
                          "mp4_copy", "webm_vp9", "mov_h264", "ts_h264",
                          "avi_mpeg4", "mp3", "m4a", "flac", "wav", "ogg",
                          "opus", "audio_copy"):
            self.assertIn(preset_id, convert.PRESET_MAP)
        self.assertEqual(convert.PRESET_MAP["mp4_h264"]["ext"], ".mp4")
        self.assertTrue(convert.PRESET_MAP["mkv_copy"]["copy"])
        self.assertTrue(convert.PRESET_MAP["mp4_copy"]["faststart"])
        self.assertEqual(convert.PRESET_MAP["wav"]["a_encoder"], "pcm_s16le")

    def test_default_preset_exists(self):
        self.assertIn(config.CONVERT_PRESET_DEFAULT, convert.PRESET_MAP)

    def test_quality_tables_cover_encoders(self):
        for preset in convert.PRESETS:
            if preset["copy"]:
                continue
            quality = convert._VIDEO_QUALITY_ARGS.get(preset["v_encoder"])
            audio_quality = convert._AUDIO_QUALITY_ARGS.get(preset["a_encoder"])
            if preset["v_encoder"]:
                self.assertIsNotNone(quality, preset["id"])
                for level in config.CONVERT_QUALITIES:
                    self.assertIn(level, quality, preset["id"])
            if preset["a_encoder"]:
                self.assertIsNotNone(audio_quality, preset["id"])
                self.assertIsInstance(audio_quality, dict)


class ProbeParseTest(unittest.TestCase):
    def test_parse_full_probe(self):
        info = _probe_info()
        self.assertAlmostEqual(info["duration"], 60.0, places=2)
        self.assertTrue(info["has_video"])
        self.assertTrue(info["has_audio"])
        self.assertTrue(info["has_subtitle"])
        self.assertEqual(info["video_codec"], "h264")
        self.assertEqual(info["audio_codec"], "aac")
        self.assertEqual(info["width"], 1920)
        self.assertEqual(info["height"], 1080)
        self.assertEqual(info["pix_fmt"], "yuv420p")
        self.assertFalse(info["hdr"])
        self.assertEqual(len(info["audio_streams"]), 1)
        self.assertEqual(len(info["subtitle_streams"]), 1)

    def test_parse_audio_only(self):
        info = _probe_info(AUDIO_ONLY_PROBE)
        self.assertAlmostEqual(info["duration"], 180.0, places=2)
        self.assertFalse(info["has_video"])
        self.assertTrue(info["has_audio"])
        self.assertEqual(info["audio_codec"], "mp3")

    def test_parse_hdr_detected(self):
        text = PROBE_TEXT.replace(
            "yuv420p(tv, bt709, progressive)",
            "yuv420p10le(tv, bt2020nc, smpte2084, progressive)")
        info = _probe_info(text)
        self.assertTrue(info["hdr"])
        self.assertEqual(info["pix_fmt"], "yuv420p10le")

    def test_parse_empty(self):
        info = _probe_info(u"")
        self.assertIsNone(info["duration"])
        self.assertFalse(info["has_video"])
        self.assertFalse(info["has_audio"])


class ProgressParseTest(unittest.TestCase):
    def test_out_time_us(self):
        self.assertEqual(convert.parse_progress_line("out_time_us=1500000"),
                         {"out_time_sec": 1.5})

    def test_out_time_ms_is_microseconds(self):
        self.assertEqual(convert.parse_progress_line("out_time_ms=2000000"),
                         {"out_time_sec": 2.0})

    def test_out_time_timecode(self):
        self.assertEqual(convert.parse_progress_line("out_time=00:01:02.500000"),
                         {"out_time_sec": 62.5})

    def test_speed_and_fps_and_size(self):
        self.assertEqual(convert.parse_progress_line("speed=1.23x"),
                         {"speed": 1.23})
        self.assertEqual(convert.parse_progress_line("speed=N/A"), None)
        self.assertEqual(convert.parse_progress_line("fps=24.5"), {"fps": 24.5})
        self.assertEqual(convert.parse_progress_line("total_size=4096"),
                         {"total_size": 4096})

    def test_done_flag(self):
        self.assertEqual(convert.parse_progress_line("progress=end"),
                         {"done": True})
        self.assertEqual(convert.parse_progress_line("progress=continue"),
                         {"done": False})

    def test_unknown_lines_ignored(self):
        self.assertIsNone(convert.parse_progress_line("frame=10"))
        self.assertIsNone(convert.parse_progress_line(""))
        self.assertIsNone(convert.parse_progress_line("garbage"))


class BuildArgsTest(unittest.TestCase):
    def setUp(self):
        self.info = _probe_info()
        self.options = {"quality": "standard", "speed": "veryfast",
                        "max_resolution": 0, "channels": ""}

    def _args(self, preset_id, options=None):
        return convert.build_args(convert.PRESET_MAP[preset_id],
                                  self.info, options or self.options,
                                  "/src/a.mp4", "/out/a.mp4")

    def test_mp4_h264(self):
        args = self._args("mp4_h264")
        self.assertEqual(args[:6], ["-nostdin", "-hide_banner", "-loglevel",
                                    "error", "-progress", "pipe:1"])
        self.assertIn("-nostats", args)
        self.assertEqual(args[args.index("-i") + 1], "/src/a.mp4")
        self.assertIn("-map", args)
        self.assertIn("0:v:0", args)
        self.assertIn("0:a:0?", args)
        self.assertIn("-c:v", args)
        self.assertIn("libx264", args)
        self.assertIn("-crf", args)
        self.assertIn("23", args)
        self.assertIn("-preset", args)
        self.assertIn("veryfast", args)
        self.assertIn("yuv420p", args)
        self.assertIn("-b:a", args)
        self.assertIn("192k", args)
        self.assertIn("+faststart", args)
        self.assertEqual(args[-2:], ["-y", "/out/a.mp4"])
        # 没有分辨率上限时不应该出现 -vf
        self.assertNotIn("-vf", args)

    def test_mp4_h265_tag(self):
        args = self._args("mp4_h265")
        self.assertIn("libx265", args)
        self.assertIn("hvc1", args)

    def test_mp4_copy(self):
        args = self._args("mp4_copy")
        self.assertIn("-c:v", args)
        self.assertIn("copy", args)
        self.assertIn("+faststart", args)
        self.assertNotIn("libx264", args)

    def test_mkv_copy_maps_subtitles(self):
        args = self._args("mkv_copy")
        self.assertIn("0:s?", args)
        self.assertIn("-c:s", args)
        self.assertEqual(args.count("copy"), 3)

    def test_mkv_h264_copies_subs_only(self):
        args = self._args("mkv_h264")
        self.assertIn("0:s?", args)
        self.assertIn("-c:s", args)
        self.assertIn("libx264", args)

    def test_mp3_from_video(self):
        args = self._args("mp3")
        self.assertIn("-vn", args)
        self.assertIn("-sn", args)
        self.assertIn("-dn", args)
        self.assertIn("0:a:0", args)
        self.assertIn("libmp3lame", args)
        self.assertIn("-q:a", args)
        self.assertEqual(args[-2:], ["-y", "/out/a.mp4"])

    def test_wav_has_no_quality_args(self):
        options = dict(self.options)
        args = self._args("wav", options)
        self.assertIn("pcm_s16le", args)
        self.assertNotIn("-q:a", args)
        self.assertNotIn("-b:a", args)

    def test_audio_copy(self):
        args = self._args("audio_copy")
        self.assertIn("-c:a", args)
        self.assertIn("copy", args)
        self.assertNotIn("libmp3lame", args)

    def test_scale_applied_when_resolution_capped(self):
        options = dict(self.options, max_resolution=720)
        args = self._args("mp4_h264", options)
        self.assertIn("-vf", args)
        value = args[args.index("-vf") + 1]
        self.assertIn("scale=-2:min(720\\,ih)", value)

    def test_stereo_downmix(self):
        options = dict(self.options, channels="stereo")
        args = self._args("mp4_h264", options)
        self.assertIn("-ac", args)
        self.assertNotIn("-ac", self._args("mp4_h264"))

    def test_no_audio_source_adds_an(self):
        info = dict(self.info)
        info["has_audio"] = False
        args = convert.build_args(convert.PRESET_MAP["mp4_h264"], info,
                                  self.options, "/src/a.mp4", "/out/a.mp4")
        self.assertIn("-an", args)
        self.assertNotIn("-b:a", args)

    def test_explicit_muxer_for_every_preset(self):
        # 产物先写 .dcpart，FFmpeg 无法按扩展名推断容器 → 必须显式 -f
        for preset_id in convert.PRESET_IDS:
            args = self._args(preset_id)
            self.assertIn("-f", args, preset_id)
            self.assertEqual(args[args.index("-f") + 1],
                             convert._MUXERS[preset_id], preset_id)
        self.assertEqual(convert._MUXERS["mkv_copy"], "matroska")
        self.assertEqual(convert._MUXERS["mp3"], "mp3")
        self.assertEqual(convert._MUXERS["m4a"], "ipod")


class CompatTest(unittest.TestCase):
    def setUp(self):
        self.info = _probe_info()

    def test_mp4_copy_rejects_vp9(self):
        info = dict(self.info, video_codec="vp9")
        problem = convert.check_compat(convert.PRESET_MAP["mp4_copy"], info)
        self.assertIsNotNone(problem)
        self.assertEqual(problem[0], "PRESET_UNSUPPORTED")

    def test_mp4_copy_rejects_flac_audio(self):
        info = dict(self.info, audio_codec="flac")
        problem = convert.check_compat(convert.PRESET_MAP["mp4_copy"], info)
        self.assertIsNotNone(problem)

    def test_mp4_copy_accepts_h264_aac(self):
        self.assertIsNone(
            convert.check_compat(convert.PRESET_MAP["mp4_copy"], self.info))

    def test_mkv_copy_accepts_anything(self):
        info = dict(self.info, video_codec="vp9", audio_codec="vorbis")
        self.assertIsNone(
            convert.check_compat(convert.PRESET_MAP["mkv_copy"], info))

    def test_video_preset_requires_video_stream(self):
        info = dict(self.info, has_video=False)
        problem = convert.check_compat(convert.PRESET_MAP["mp4_h264"], info)
        self.assertEqual(problem[0], "SOURCE_INVALID")

    def test_audio_preset_requires_audio_stream(self):
        info = dict(self.info, has_audio=False)
        problem = convert.check_compat(convert.PRESET_MAP["mp3"], info)
        self.assertEqual(problem[0], "SOURCE_INVALID")

    def test_missing_encoder(self):
        problem = convert.check_compat(
            convert.PRESET_MAP["webm_vp9"], self.info,
            encoders=frozenset(["libx264", "aac"]))
        self.assertEqual(problem[0], "PRESET_UNSUPPORTED")
        self.assertIn("libvpx-vp9", problem[1])

    def test_preset_view_marks_unavailable(self):
        view = convert.preset_view(convert.PRESET_MAP["mp4_h265"],
                                   frozenset(["libx264"]))
        self.assertFalse(view["available"])
        self.assertIn("libx265", view["reason"])


class EstimateTest(unittest.TestCase):
    def test_audio_estimate_uses_bitrate(self):
        info = _probe_info()
        value = convert.estimate_bytes(convert.PRESET_MAP["mp3"], info,
                                       {}, 50 * 1024 * 1024)
        # 190kbps × 60s ≈ 1.4MB
        self.assertTrue(1_000_000 < value < 2_000_000, value)

    def test_copy_estimate_equals_source(self):
        info = _probe_info()
        self.assertEqual(
            convert.estimate_bytes(convert.PRESET_MAP["mkv_copy"], info, {},
                                   123456),
            123456)

    def test_video_estimate_scales_with_quality(self):
        info = _probe_info()
        high = convert.estimate_bytes(convert.PRESET_MAP["mp4_h264"], info,
                                      {"quality": "high"}, 1000)
        small = convert.estimate_bytes(convert.PRESET_MAP["mp4_h264"], info,
                                       {"quality": "small"}, 1000)
        self.assertGreater(high, small)


# ----------------------------------------------------------------------
# 服务层：假 FFmpeg，验证队列/输出/覆盖/取消/持久化
# ----------------------------------------------------------------------
class _FakeShares(object):
    def __init__(self, root):
        self.root = os.path.abspath(root)

    def validate_root(self, path):
        root = os.path.normcase(self.root)
        target = os.path.normcase(os.path.abspath(path))
        if target != root and not target.startswith(root + os.sep):
            raise ApiError("PATH_NOT_ALLOWED", u"路径不在共享文件夹内")
        return {"name": "share", "path": self.root}


class _FakeTool(object):
    """假 FFmpeg：probe 返回固定文本，run_stream 写产物并回报进度。"""

    def __init__(self, probe_text=PROBE_TEXT, exit_code=0,
                 stderr=b"", output_bytes=4096):
        self._probe_text = probe_text
        self.exit_code = exit_code
        self.stderr = stderr
        self.output_bytes = output_bytes
        self.calls = []
        self.stream_calls = []

    def available(self):
        return True

    def resolve(self):
        return ["ffmpeg"]

    @property
    def path(self):
        return "ffmpeg"

    def version(self):
        return "fake"

    def encoders(self):
        return ["libx264", "libx265", "libvpx-vp9", "aac", "libmp3lame",
                "libopus", "libvorbis", "flac", "pcm_s16le", "mpeg4"]

    def run(self, args, timeout, stop_check=None):
        self.calls.append(list(args))
        return 0, b"", self._probe_text.encode("utf-8")

    def run_stream(self, args, on_line, timeout, stop_check=None):
        self.stream_calls.append(list(args))
        duration = 60.0
        for fraction in (0.25, 0.5, 0.75):
            on_line("out_time_us=%d" % int(duration * fraction * 1000000))
            on_line("speed=1.50x")
            on_line("fps=30.0")
            on_line("progress=continue")
        on_line("progress=end")
        target = args[-1]
        with open(target, "wb") as handle:
            handle.write(b"x" * self.output_bytes)
        return self.exit_code, self.stderr


class ConvertServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-cv-")
        self.share = os.path.join(self.tmpdir, "share")
        os.makedirs(self.share)
        self.store = Store(os.path.join(self.tmpdir, "data"))
        self.settings = settings_module.default_settings()
        self.settings.update({
            "convert_enabled": True,
            "convert_min_free_gb": 0,
            "convert_output_mode": "alongside",
            "convert_concurrency": 1,
        })
        self.tool = _FakeTool()
        self.service = convert.ConvertService(
            None, self.store, _FakeShares(self.share),
            lambda: dict(self.settings), ffmpeg_tool=self.tool)

    def tearDown(self):
        self.service.shutdown(timeout=0.2)
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _source(self, name="movie.mp4", size=2048):
        path = os.path.join(self.share, name)
        with open(path, "wb") as handle:
            handle.write(b"v" * size)
        return path

    def _job(self, job_id):
        return self.service._jobs[job_id]

    def test_create_enqueues_job(self):
        source = self._source()
        result = self.service.create("admin", {"paths": [source],
                                               "preset": "mp4_h264"})
        self.assertEqual(result["count"], 1)
        job = self._job(result["jobs"][0]["job_id"])
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.preset, "mp4_h264")
        self.assertEqual(job.source_path, source)
        self.assertEqual(self.service.counts()["queued"], 1)

    def test_create_ignores_disabled_setting(self):
        # v0058：转换是独立功能，convert_enabled 键保留但不再生效。
        self.settings["convert_enabled"] = False
        source = self._source()
        result = self.service.create("admin", {"paths": [source]})
        self.assertEqual(len(result["jobs"]), 1)
        job = self._job(result["jobs"][0]["job_id"])
        self.assertEqual(job.status, "queued")

    def test_create_rejects_unknown_preset(self):
        source = self._source()
        with self.assertRaises(ApiError) as ctx:
            self.service.create("admin", {"paths": [source],
                                          "preset": "nope"})
        self.assertEqual(ctx.exception.code, "PRESET_UNKNOWN")

    def test_create_rejects_relative_output_dir(self):
        source = self._source()
        with self.assertRaises(ApiError):
            self.service.create("admin", {
                "paths": [source],
                "options": {"output_dir": "relative/path"}})

    def test_delete_source_requires_confirm(self):
        source = self._source()
        with self.assertRaises(ApiError):
            self.service.create("admin", {
                "paths": [source],
                "options": {"delete_source": True}})
        result = self.service.create("admin", {
            "paths": [source],
            "confirm": "DELETE SOURCE",
            "options": {"delete_source": True}})
        self.assertEqual(result["count"], 1)

    def test_create_dedupes_paths(self):
        source = self._source()
        result = self.service.create("admin", {"paths": [source, source]})
        self.assertEqual(result["count"], 1)

    def test_rejects_directory_source(self):
        with self.assertRaises(ApiError) as ctx:
            self.service.create("admin", {"paths": [self.share]})
        self.assertEqual(ctx.exception.code, "SOURCE_INVALID")

    def test_cancel_queued_job(self):
        source = self._source()
        created = self.service.create("admin", {"paths": [source]})
        job_id = created["jobs"][0]["job_id"]
        view = self.service.cancel(job_id)
        self.assertEqual(view["status"], "cancelled")
        self.assertEqual(self.service.counts()["queued"], 0)
        with self.assertRaises(ApiError):
            self.service.cancel(job_id)

    def test_retry_finished_job(self):
        source = self._source()
        created = self.service.create("admin", {"paths": [source]})
        job_id = created["jobs"][0]["job_id"]
        self.service.cancel(job_id)
        view = self.service.retry(job_id)
        self.assertEqual(view["status"], "queued")
        self.assertEqual(self.service.counts()["queued"], 1)

    def test_delete_finished_job(self):
        source = self._source()
        created = self.service.create("admin", {"paths": [source]})
        job_id = created["jobs"][0]["job_id"]
        self.service.cancel(job_id)
        result = self.service.delete(job_id)
        self.assertTrue(result["ok"])
        self.assertEqual(self.service.counts()["total"], 0)
        self.assertIsNone(self.store.load_convert_job(job_id))

    def test_execute_success(self):
        source = self._source()
        created = self.service.create("admin", {"paths": [source]})
        job = self._job(created["jobs"][0]["job_id"])
        self.service._execute(job)
        self.assertEqual(job.status, "done")
        self.assertTrue(os.path.isfile(job.output_path))
        self.assertTrue(job.output_path.endswith(".mp4"))
        self.assertEqual(job.progress, 100)
        self.assertEqual(job.output_size, 4096)
        self.assertFalse(os.path.exists(job.output_path
                                        + config.CONVERT_PART_EXT))
        self.assertTrue(job.verify["duration_ok"])
        self.assertEqual(job.verify["video_codec"], "h264")
        # 源文件默认保留
        self.assertTrue(os.path.isfile(source))

    def test_execute_writes_expected_args(self):
        source = self._source()
        created = self.service.create("admin", {"paths": [source],
                                                "preset": "mkv_copy"})
        job = self._job(created["jobs"][0]["job_id"])
        self.service._execute(job)
        args = self.tool.stream_calls[-1]
        self.assertIn("copy", args)
        self.assertTrue(args[-1].endswith(".mkv" + config.CONVERT_PART_EXT))
        self.assertTrue(job.output_path.endswith(".mkv"))

    def test_execute_delete_source(self):
        source = self._source()
        created = self.service.create("admin", {
            "paths": [source], "confirm": "DELETE SOURCE",
            "options": {"delete_source": True}})
        job = self._job(created["jobs"][0]["job_id"])
        self.service._execute(job)
        self.assertEqual(job.status, "done")
        self.assertFalse(os.path.exists(source))
        self.assertTrue(job.deleted_source)

    def test_execute_same_name_gets_renamed(self):
        source = self._source()
        existing = os.path.join(self.share, "movie.mp4")
        # 目标同名（mp4 预设、源也是 mp4）：必须自动加序号，绝不覆盖
        created = self.service.create("admin", {"paths": [source]})
        job = self._job(created["jobs"][0]["job_id"])
        self.service._execute(job)
        self.assertNotEqual(os.path.normcase(job.output_path),
                            os.path.normcase(existing))
        self.assertIn("(2)", os.path.basename(job.output_path))
        with open(source, "rb") as handle:
            self.assertEqual(handle.read(8), b"v" * 8)

    def test_execute_skip_existing(self):
        # 源 .mkv → 目标 .mp4：先跑一次生成产物 movie.mp4，再提交第二次走 skip
        source = self._source("movie.mkv")
        self.settings["convert_overwrite"] = "skip"
        target = os.path.join(self.share, "movie.mp4")
        created = self.service.create("admin", {"paths": [source]})
        job = self._job(created["jobs"][0]["job_id"])
        self.service._run_job(job)
        self.assertTrue(os.path.isfile(target))
        # 再提交一次：skip 策略应直接 done 且不覆盖
        created = self.service.create("admin", {"paths": [source]})
        job2 = self._job(created["jobs"][0]["job_id"])
        before = os.path.getmtime(target)
        self.service._run_job(job2)
        self.assertEqual(job2.status, "done")
        self.assertEqual(os.path.getmtime(target), before)

    def test_execute_ffmpeg_failure(self):
        source = self._source()
        self.tool.exit_code = 1
        self.tool.stderr = b"Invalid data found when processing input"
        created = self.service.create("admin", {"paths": [source]})
        job = self._job(created["jobs"][0]["job_id"])
        with self.assertRaises(convert.ConvertError):
            self.service._execute(job)

    def test_run_job_marks_failed_and_cleans_part(self):
        source = self._source()
        self.tool.exit_code = 1
        self.tool.stderr = b"boom"
        created = self.service.create("admin", {"paths": [source]})
        job = self._job(created["jobs"][0]["job_id"])
        self.service._run_job(job)
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "CONVERT_FAILED")
        self.assertIn("boom", job.error_message)
        self.assertFalse(os.path.exists(job.output_path
                                        + config.CONVERT_PART_EXT))

    def test_keep_temp_on_failure(self):
        source = self._source()
        self.settings["convert_keep_temp"] = True
        self.tool.exit_code = 1
        self.tool.stderr = b"boom"
        created = self.service.create("admin", {"paths": [source]})
        job = self._job(created["jobs"][0]["job_id"])
        # 假 tool 已写出部分产物后返回失败：keep_temp 时应保留 .dcpart
        self.service._run_job(job)
        self.assertEqual(job.status, "failed")
        self.assertTrue(job.part_kept)
        self.assertTrue(os.path.isfile(job.output_path
                                       + config.CONVERT_PART_EXT))

    def test_execute_missing_audio_rejected(self):
        self.tool._probe_text = AUDIO_ONLY_PROBE
        source = self._source("song.mp3")
        created = self.service.create("admin", {"paths": [source],
                                                "preset": "mp4_h264"})
        job = self._job(created["jobs"][0]["job_id"])
        with self.assertRaises(convert.ConvertError) as ctx:
            self.service._execute(job)
        self.assertEqual(ctx.exception.code, "SOURCE_INVALID")

    def test_persist_and_reload(self):
        source = self._source()
        created = self.service.create("admin", {"paths": [source]})
        job_id = created["jobs"][0]["job_id"]
        record = self.store.load_convert_job(job_id)
        self.assertIsNotNone(record)
        self.assertEqual(record["preset"], "mp4_h264")
        service2 = convert.ConvertService(
            None, self.store, _FakeShares(self.share),
            lambda: dict(self.settings), ffmpeg_tool=self.tool)
        self.assertIn(job_id, service2._jobs)
        self.assertIn(job_id, service2._queue)

    def test_reload_running_becomes_queued(self):
        source = self._source()
        created = self.service.create("admin", {"paths": [source]})
        job = self._job(created["jobs"][0]["job_id"])
        job.status = "running"
        job.progress = 42
        self.service._persist(job, force=True)
        service2 = convert.ConvertService(
            None, self.store, _FakeShares(self.share),
            lambda: dict(self.settings), ffmpeg_tool=self.tool)
        reloaded = service2._jobs[job.job_id]
        self.assertEqual(reloaded.status, "queued")
        self.assertEqual(reloaded.progress, 0)
        self.assertIn("重新排队", reloaded.message)

    def test_enumerate_directory(self):
        self._source("a.mp4")
        self._source("b.mkv")
        self._source("c.txt")
        result = self.service.enumerate_files({"path": self.share})
        names = [item["name"] for item in result["items"]]
        self.assertEqual(names, ["a.mp4", "b.mkv"])
        self.assertEqual(result["skipped"]["extension"], 1)

    def test_enumerate_extension_filter(self):
        self._source("a.mp4")
        self._source("b.mkv")
        result = self.service.enumerate_files(
            {"path": self.share, "extensions": [".mp4"]})
        self.assertEqual([item["name"] for item in result["items"]],
                         ["a.mp4"])

    def test_capabilities(self):
        caps = self.service.capabilities()
        self.assertTrue(caps["ffmpeg"]["available"])
        self.assertEqual(len(caps["presets"]), 16)
        self.assertTrue(all(item["available"] for item in caps["presets"]))
        self.assertEqual(caps["defaults"]["quality"], "standard")

    def test_offpeak_blocks_dispatch(self):
        now = time.localtime()
        start = "%02d:00" % ((now.tm_hour + 1) % 24)
        end = "%02d:00" % ((now.tm_hour + 2) % 24)
        self.settings.update({
            "convert_offpeak_enabled": True,
            "convert_offpeak_start": start,
            "convert_offpeak_end": end,
        })
        source = self._source()
        created = self.service.create("admin", {"paths": [source]})
        job = self._job(created["jobs"][0]["job_id"])
        # 直接调用派发：窗口未到 → 任务保持 queued
        self.service._dispatch_once()
        self.assertEqual(job.status, "queued")

    def test_queue_snapshot_shape(self):
        source = self._source()
        self.service.create("admin", {"paths": [source]})
        snapshot = self.service.queue_snapshot()
        self.assertIn("active", snapshot)
        self.assertIn("queued", snapshot)
        self.assertIn("counts", snapshot)
        self.assertEqual(snapshot["concurrency"], 1)
        self.assertEqual(snapshot["queued"][0]["queue_position"], 1)

    def test_bulk_actions(self):
        sources = [self._source("a.mp4"), self._source("b.mp4")]
        self.service.create("admin", {"paths": sources})
        result = self.service.bulk_action("admin", "cancel_all")
        self.assertEqual(result["affected"], 2)
        self.assertEqual(self.service.counts()["cancelled"], 2)
        result = self.service.bulk_action("admin", "clear_finished")
        self.assertEqual(result["affected"], 2)
        self.assertEqual(self.service.counts()["total"], 0)


# ----------------------------------------------------------------------
# 真 FFmpeg：本机有 ffmpeg 才跑
# ----------------------------------------------------------------------
def _ffmpeg_available():
    try:
        return bool(FFmpegTool().available())
    except Exception:
        return False


@unittest.skipUnless(_ffmpeg_available(), "本机没有 ffmpeg")
class RealFfmpegTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="dc-cv-real-")
        cls.source = os.path.join(cls.tmpdir, "sample.mp4")
        tool = FFmpegTool()
        command = list(tool.resolve()) + [
            "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=15:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", cls.source]
        cls.generated = subprocess.call(
            command, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL) == 0 and os.path.isfile(cls.source)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        if not self.generated:
            self.skipTest("无法生成测试样本")
        self.outdir = tempfile.mkdtemp(prefix="out-", dir=self.tmpdir)

    def _run(self, preset_id, extra=None):
        tool = FFmpegTool()
        info = convert.parse_probe_text(
            tool.run(["-nostdin", "-hide_banner", "-loglevel", "info",
                      "-i", self.source], timeout=30)[2].decode("utf-8",
                                                                "replace"))
        options = {"quality": "small", "speed": "ultrafast",
                   "max_resolution": 0, "channels": ""}
        options.update(extra or {})
        preset = convert.PRESET_MAP[preset_id]
        target = os.path.join(self.outdir, "out" + preset["ext"])
        args = convert.build_args(preset, info, options, self.source, target)
        code, stderr = tool.run_stream(args, lambda line: None, timeout=120)
        self.assertEqual(code, 0, stderr.decode("utf-8", "replace"))
        self.assertTrue(os.path.isfile(target))
        self.assertGreater(os.path.getsize(target), 1024)
        return target

    def test_mp4_copy_remux(self):
        target = self._run("mp4_copy")
        self.assertTrue(target.endswith(".mp4"))

    def test_mp4_h264_transcode(self):
        target = self._run("mp4_h264")
        self.assertGreater(os.path.getsize(target), 0)

    def test_mkv_copy(self):
        target = self._run("mkv_copy")
        self.assertTrue(target.endswith(".mkv"))

    def test_extract_mp3(self):
        target = self._run("mp3")
        self.assertTrue(target.endswith(".mp3"))

    def test_extract_wav(self):
        target = self._run("wav")
        self.assertTrue(target.endswith(".wav"))


if __name__ == "__main__":
    unittest.main()
