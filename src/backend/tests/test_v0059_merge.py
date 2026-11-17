# -*- coding: utf-8 -*-
"""v0059 音视频合成（docs/AV_MERGE_CONTRACT.md）：纯函数 + 服务流程 + 真 FFmpeg。

分层：
1) 纯函数：探测解析、concat 清单转义、拷贝兼容性、策略选择、滤镜参数；
2) 服务：提交校验、顺序保持、队列/取消/重试/删除/持久化（FFmpeg 用假实现）；
3) 真合成：本机有 ffmpeg 时生成 1 秒样本，验证“多段拼接 + 音频补齐”产物。
"""

from __future__ import absolute_import

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from tests.helpers import BACKEND_DIR  # noqa: F401

from dupcleaner import avmerge, config
from dupcleaner import settings as settings_module
from dupcleaner.errors import ApiError
from dupcleaner.store import Store
from dupcleaner.video_similarity import FFmpegTool

VIDEO_PROBE = u"""Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'a.mp4':
  Duration: 00:00:10.00, start: 0.000000, bitrate: 1200 kb/s
  Stream #0:0(und): Video: h264 (High), yuv420p(tv, bt709, progressive), 1280x720 [SAR 1:1 DAR 16:9], 25 fps, 25 tbr, 90k tbn
  Stream #0:1(eng): Audio: aac (LC), 48000 Hz, stereo, fltp, 128 kb/s (default)
"""

AUDIO_PROBE = u"""Input #0, mp4, from 'b.m4a':
  Duration: 00:00:04.00, start: 0.000000, bitrate: 128 kb/s
  Stream #0:0: Audio: aac (LC), 48000 Hz, stereo, fltp, 128 kb/s
"""


def _info(text):
    return avmerge.probe_media(text)


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
    def __init__(self, probe_text=VIDEO_PROBE, exit_code=0,
                 stderr=b"", output_bytes=4096):
        self._probe_text = probe_text
        self.exit_code = exit_code
        self.stderr = stderr
        self.output_bytes = output_bytes
        self.calls = []

    def available(self):
        return True

    def resolve(self):
        return ["ffmpeg"]

    @property
    def path(self):
        return "ffmpeg"

    def version(self):
        return "fake"

    def run(self, args, timeout, stop_check=None):
        self.calls.append(list(args))
        return 0, b"", self._probe_text.encode("utf-8")

    def run_stream(self, args, on_line, timeout, stop_check=None):
        self.calls.append(list(args))
        for fraction in (0.25, 0.5, 0.75):
            on_line("out_time_us=%d" % int(60 * fraction * 1000000))
            on_line("speed=1.50x")
            on_line("progress=continue")
        on_line("progress=end")
        target = args[-1]
        with open(target, "wb") as handle:
            handle.write(b"x" * self.output_bytes)
        return self.exit_code, self.stderr


class ProbeParseTest(unittest.TestCase):
    def test_video_probe_params(self):
        info = _info(VIDEO_PROBE)
        self.assertTrue(info["has_video"])
        self.assertEqual(info["video_codec"], "h264")
        self.assertEqual((info["width"], info["height"]), (1280, 720))
        self.assertAlmostEqual(info["fps"], 25.0, places=2)
        self.assertEqual(info["audio_codec"], "aac")
        self.assertEqual(info["sample_rate"], 48000)
        self.assertEqual(info["channels"], "stereo")

    def test_audio_only_probe(self):
        info = _info(AUDIO_PROBE)
        self.assertFalse(info["has_video"])
        self.assertTrue(info["has_audio"])
        self.assertAlmostEqual(info["duration"], 4.0, places=2)


class ConcatListTest(unittest.TestCase):
    def test_order_and_escape(self):
        text = avmerge.build_concat_list([u"/a/it's.mp4", u"/b/2.mp4"])
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], u"file '/a/it'\\''s.mp4'")
        self.assertEqual(lines[1], u"file '/b/2.mp4'")


class CompatibilityTest(unittest.TestCase):
    def test_copy_compatible_same_params(self):
        infos = [_info(VIDEO_PROBE), _info(VIDEO_PROBE)]
        ok, _reason = avmerge.video_copy_compatible(infos, "mp4")
        self.assertTrue(ok)

    def test_copy_rejects_resolution_mismatch(self):
        other = _info(VIDEO_PROBE.replace("1280x720", "1920x1080"))
        ok, reason = avmerge.video_copy_compatible(
            [_info(VIDEO_PROBE), other], "mp4")
        self.assertFalse(ok)
        self.assertIn(u"分辨率", reason)

    def test_copy_rejects_mp4_incompatible_codec(self):
        info = dict(_info(VIDEO_PROBE))
        info["video_codec"] = "vp9"
        ok, _reason = avmerge.video_copy_compatible([info], "mp4")
        self.assertFalse(ok)
        ok_mkv, _ = avmerge.video_copy_compatible([info], "mkv")
        self.assertTrue(ok_mkv)

    def test_audio_copy_requires_same_params(self):
        info = _info(VIDEO_PROBE)
        self.assertTrue(avmerge.audio_copy_compatible([info], "mp4")[0])
        other = _info(VIDEO_PROBE.replace("48000 Hz", "44100 Hz"))
        self.assertFalse(avmerge.audio_copy_compatible([info, other], "mp4")[0])

    def test_choose_strategy_auto_falls_back_to_encode(self):
        infos = [_info(VIDEO_PROBE)]
        other = _info(VIDEO_PROBE.replace("1280x720", "1920x1080"))
        self.assertEqual(
            avmerge.choose_strategy("auto", [infos[0], other], [], "mp4")[0],
            "encode")
        self.assertEqual(
            avmerge.choose_strategy("copy", infos, [], "mp4")[0], "copy")

    def test_choose_strategy_copy_raises_on_mismatch(self):
        infos = [_info(VIDEO_PROBE)]
        other = _info(VIDEO_PROBE.replace("1280x720", "1920x1080"))
        with self.assertRaises(avmerge.AVMergeError) as ctx:
            avmerge.choose_strategy("copy", [infos[0], other], [], "mp4")
        self.assertEqual(ctx.exception.code, "MERGE_INCOMPATIBLE")

    def test_target_size_caps_height(self):
        width, height = avmerge.target_size([_info(VIDEO_PROBE)], 720)
        self.assertEqual(height, 720)
        self.assertEqual(width % 2, 0)

    def test_filter_graphs_keep_input_order(self):
        graph = avmerge._video_filter_graph(3, 1280, 720, 25)
        self.assertIn("[0:v:0]", graph)
        self.assertIn("[2:v:0]", graph)
        self.assertIn("concat=n=3:v=1:a=0", graph)
        audio = avmerge._audio_filter_graph(2)
        self.assertIn("concat=n=2:v=0:a=1", audio)


class MergeServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-av-")
        self.share = os.path.join(self.tmpdir, "share")
        os.makedirs(self.share)
        self.store = Store(os.path.join(self.tmpdir, "data"))
        self.settings = settings_module.default_settings()
        self.settings.update({
            "avmerge_min_free_gb": 0,
            "avmerge_output_mode": "alongside",
            "avmerge_concurrency": 1,
        })
        self.tool = _FakeTool()
        self.service = avmerge.AVMergeService(
            None, self.store, _FakeShares(self.share),
            lambda: dict(self.settings), ffmpeg_tool=self.tool)

    def tearDown(self):
        self.service.shutdown(timeout=0.2)
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _source(self, name, size=2048):
        path = os.path.join(self.share, name)
        with open(path, "wb") as handle:
            handle.write(b"v" * size)
        return path

    def _job(self, job_id):
        return self.service._jobs[job_id]

    def test_create_keeps_selection_order(self):
        v1 = self._source("v1.mp4")
        v2 = self._source("v2.mp4")
        a1 = self._source("a1.m4a")
        result = self.service.create("admin", {
            "video_paths": [v2, v1], "audio_paths": [a1],
            "options": {"container": "mp4", "mode": "auto"},
        })
        job = self._job(result["job_id"])
        self.assertEqual(job.video_paths, [v2, v1])
        self.assertEqual(job.audio_paths, [a1])
        self.assertEqual(job.status, "queued")

    def test_create_requires_video(self):
        with self.assertRaises(ApiError):
            self.service.create("admin", {"video_paths": [],
                                          "audio_paths": []})

    def test_create_rejects_bad_container(self):
        v1 = self._source("v1.mp4")
        with self.assertRaises(ApiError):
            self.service.create("admin", {
                "video_paths": [v1], "options": {"container": "avi"}})

    def test_delete_source_requires_confirm(self):
        v1 = self._source("v1.mp4")
        with self.assertRaises(ApiError):
            self.service.create("admin", {
                "video_paths": [v1],
                "options": {"delete_source": True}})
        result = self.service.create("admin", {
            "video_paths": [v1],
            "options": {"delete_source": True},
            "confirm": "DELETE SOURCE"})
        job = self._job(result["job_id"])
        self.assertTrue(job.options.get("delete_source_confirmed"))

    def test_create_rejects_symlink_component(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlink support")
        target = self._source("real.mp4")
        link = os.path.join(self.share, "link.mp4")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not permitted")
        with self.assertRaises(avmerge.AVMergeError):
            self.service.create("admin", {"video_paths": [link]})

    def test_cancel_queued_job(self):
        v1 = self._source("v1.mp4")
        result = self.service.create("admin", {"video_paths": [v1]})
        view = self.service.cancel(result["job_id"])
        self.assertEqual(view["status"], "cancelled")
        self.assertEqual(self.service.counts()["cancelled"], 1)

    def test_retry_and_delete_terminal_job(self):
        v1 = self._source("v1.mp4")
        result = self.service.create("admin", {"video_paths": [v1]})
        self.service.cancel(result["job_id"])
        retried = self.service.retry(result["job_id"])
        self.assertEqual(retried["status"], "queued")
        self.service.cancel(result["job_id"])
        self.service.delete(result["job_id"])
        with self.assertRaises(ApiError):
            self.service.get(result["job_id"])

    def test_record_roundtrip(self):
        v1 = self._source("v1.mp4")
        result = self.service.create("admin", {
            "video_paths": [v1], "options": {"container": "mkv"}})
        job = self._job(result["job_id"])
        record = job.to_record()
        restored = avmerge.AVMergeJob.from_record(record)
        self.assertEqual(restored.job_id, job.job_id)
        self.assertEqual(restored.video_paths, job.video_paths)
        self.assertEqual(restored.options.get("container"), "mkv")


class RealMergeTest(unittest.TestCase):
    """真 FFmpeg：1 段视频 + 1 段音频（音频短于视频，验证 apad 补齐）。"""

    @classmethod
    def setUpClass(cls):
        cls.tool = FFmpegTool()
        try:
            if not cls.tool.available():
                raise RuntimeError("no ffmpeg")
        except Exception:
            raise unittest.SkipTest("ffmpeg 不可用，跳过真合成")

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-avreal-")
        self.share = os.path.join(self.tmpdir, "share")
        os.makedirs(self.share)
        self.store = Store(os.path.join(self.tmpdir, "data"))
        self.settings = settings_module.default_settings()
        self.settings.update({
            "avmerge_min_free_gb": 0,
            "avmerge_output_mode": "alongside",
            "avmerge_concurrency": 1,
            "avmerge_mode": "auto",
            "avmerge_container": "mp4",
        })
        self.service = avmerge.AVMergeService(
            None, self.store, _FakeShares(self.share),
            lambda: dict(self.settings), ffmpeg_tool=self.tool)

    def tearDown(self):
        self.service.shutdown(timeout=0.5)
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_video(self, name, color):
        path = os.path.join(self.share, name)
        command = list(self.tool.resolve()) + [
            "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            "color=c=%s:s=320x240:d=0.8:r=25" % color,
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt",
            "yuv420p", "-an", path]
        if subprocess.call(command) != 0:
            raise RuntimeError("生成测试视频失败")
        return path

    def _make_audio(self, name, duration=0.5):
        path = os.path.join(self.share, name)
        command = list(self.tool.resolve()) + [
            "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=48000:duration=%s" % duration,
            "-c:a", "aac", "-b:a", "96k", path]
        if subprocess.call(command) != 0:
            raise RuntimeError("生成测试音频失败")
        return path

    def test_merge_two_videos_and_short_audio(self):
        v1 = self._make_video("v1.mp4", "red")
        v2 = self._make_video("v2.mp4", "blue")
        audio = self._make_audio("a.m4a")
        result = self.service.create("admin", {
            "video_paths": [v1, v2], "audio_paths": [audio],
            "options": {"container": "mp4", "mode": "auto"},
        })
        self.service.start()
        deadline = time.time() + 90
        job = self.service.get(result["job_id"])
        while time.time() < deadline and not job.finished:
            time.sleep(0.2)
        self.assertTrue(job.finished, "合成未在超时内结束")
        self.assertEqual(job.status, "done", job.error_message)
        self.assertTrue(os.path.isfile(job.output_path))
        info = self.service._probe(job, job.output_path)
        self.assertTrue(info["has_video"])
        self.assertTrue(info["has_audio"])
        self.assertGreater(float(info["duration"] or 0), 1.2)
        self.assertLess(float(info["duration"] or 0), 2.4)
        self.assertFalse(os.path.exists(job.output_path + ".dcpart"))
