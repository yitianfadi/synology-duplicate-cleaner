# -*- coding: utf-8 -*-
"""视频视觉相似度 + 视频批量删除 + XLSX 导出测试（fake ffmpeg，不依赖真实视频）。"""

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
import zlib
from unittest import mock
from xml.etree import ElementTree

from tests.helpers import Harness, WebServer, write_bytes, write_text

from dupcleaner import config, settings as settings_module, video_similarity

FAKE_FFMPEG = r'''
import os
import sys
import time

args = sys.argv[1:]
log_path = os.environ.get("FAKE_FFMPEG_LOG")
if log_path:
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(" ".join(args) + "\n")
    except OSError:
        pass


def fail(message):
    sys.stderr.write("fake ffmpeg: " + message + "\n")
    sys.stderr.flush()
    sys.exit(1)


if "-version" in args:
    sys.stdout.write("ffmpeg version 6.1.1-fake Copyright (c) fake\n")
    sys.exit(0)
if "-hwaccels" in args:
    sys.stdout.write("Hardware acceleration methods:\nvaapi\nqsv\n")
    sys.exit(0)
if "-f" in args and "lavfi" in args:
    # v0026：云端字幕连接测试用的静音 MP3（anullsrc），不读取输入文件。
    if os.environ.get("FAKE_FFMPEG_SILENCE_FAIL") == "1":
        fail("silence generation failed")
    seconds = 1.0
    if "-t" in args:
        try:
            seconds = float(args[args.index("-t") + 1])
        except (IndexError, ValueError):
            seconds = 1.0
    per_sec = float(os.environ.get("FAKE_MP3_BYTES_PER_SEC", "2000") or 2000)
    size = max(16, int(seconds * per_sec))
    if "-y" in args and args.index("-y") + 1 < len(args):
        target = args[args.index("-y") + 1]
    else:
        target = args[-1]
    try:
        with open(target, "wb") as handle:
            handle.write(b"ID3" + b"\x00" * (size - 3))
    except OSError:
        fail("cannot write silence mp3")
    sys.exit(0)
if "-i" not in args:
    fail("missing input")
path = args[args.index("-i") + 1]
try:
    with open(path, "rb") as handle:
        raw = handle.read()
except OSError:
    fail("cannot open input")
text = raw.decode("utf-8", "replace")
sleep_for = float(os.environ.get("FAKE_FFMPEG_SLEEP", "0") or 0)
if sleep_for > 0:
    time.sleep(sleep_for)
if "-f" in args and "wav" in args:
    if os.environ.get("FAKE_AUDIO_FAIL") == "1":
        fail("audio extract failed")
    target = args[-1]
    try:
        with open(target, "wb") as handle:
            handle.write(b"RIFF" + b"\x00" * 64)
    except OSError:
        fail("cannot write wav")
    sys.exit(0)
if "-f" in args and "mp3" in args:
    if os.environ.get("FAKE_AUDIO_FAIL") == "1":
        fail("audio extract failed")
    chunk_seconds = 0.0
    if "-t" in args:
        try:
            chunk_seconds = float(args[args.index("-t") + 1])
        except (IndexError, ValueError):
            chunk_seconds = 0.0
    per_sec = float(os.environ.get("FAKE_MP3_BYTES_PER_SEC", "2000") or 2000)
    size = max(16, int(chunk_seconds * per_sec))
    target = args[-1]
    try:
        with open(target, "wb") as handle:
            handle.write(b"ID3" + b"\x00" * (size - 3))
    except OSError:
        fail("cannot write mp3")
    sys.exit(0)
if "-f" in args and "matroska" in args:
    # v0032 server 音频模式：流复制抽音轨（不解码）输出 .mka
    if os.environ.get("FAKE_REMUX_FAIL") == "1":
        fail("remux failed")
    chunk_seconds = 0.0
    if "-t" in args:
        try:
            chunk_seconds = float(args[args.index("-t") + 1])
        except (IndexError, ValueError):
            chunk_seconds = 0.0
    per_sec = float(os.environ.get("FAKE_REMUX_BYTES_PER_SEC", "2000") or 2000)
    size = max(16, int(chunk_seconds * per_sec))
    target = args[-1]
    try:
        with open(target, "wb") as handle:
            handle.write(b"\x1aE\xdf\xa3" + b"\x00" * (size - 4))
    except OSError:
        fail("cannot write mka")
    sys.exit(0)
rawvideo = ("-f" in args and "rawvideo" in args)
# v0018 批处理抽帧：单进程多个 "-y <frame.raw>" 输出文件。
outputs = []
for _index, _token in enumerate(args):
    if _token == "-y" and _index + 1 < len(args):
        outputs.append(args[_index + 1])
if not rawvideo:
    if "NODURATION" in text:
        fail("no duration info")
    duration = 120.0
    for line in text.splitlines():
        if line.startswith("DURATION="):
            duration = float(line.split("=", 1)[1])
    hours = int(duration // 3600)
    minutes = int((duration % 3600) // 60)
    seconds = duration - hours * 3600 - minutes * 60
    sys.stderr.write("  Duration: %02d:%02d:%05.2f, start: 0.000000, bitrate: 1000 kb/s\n"
                     % (hours, minutes, seconds))
    sys.stderr.write("    Stream #0:0: Video: h264, yuv420p, 640x480\n")
    if "NOAUDIO" not in text:
        sys.stderr.write("    Stream #0:1: Audio: aac (LC), 48000 Hz, stereo, fltp\n")
    sys.stderr.flush()
    sys.exit(1)
if "CORRUPT" in text:
    fail("corrupt input")
if os.environ.get("FAKE_FFMPEG_HWACCEL_FAIL") == "1" and "-hwaccel" in args:
    fail("hwaccel init failed")
seed = 1
for line in text.splitlines():
    if line.startswith("SEED="):
        seed = int(line.split("=", 1)[1])
frame = bytearray(1024)
for y in range(32):
    for x in range(32):
        block = (seed * 37 + (x // 4) * 5 + (y // 4) * 11 + (x // 8) * 3) % 13
        frame[y * 32 + x] = (block * 19) & 0xFF
if rawvideo and len(outputs) > 1:
    if os.environ.get("FAKE_FFMPEG_BATCH_FAIL") == "1":
        fail("batch decode failed")
    for out_path in outputs:
        try:
            with open(out_path, "wb") as handle:
                handle.write(bytes(frame))
        except OSError:
            fail("cannot write batch frame")
    sys.exit(0)
sys.stdout.buffer.write(bytes(frame))
sys.stdout.buffer.flush()
sys.exit(0)
'''


def make_video(path, seed, duration=120.0, extra=""):
    text = "DURATION=%s\nSEED=%d\n%s" % (duration, seed, extra)
    return write_bytes(path, text.encode("utf-8"))


class FakeFFmpegBase(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root
        os.makedirs(os.path.join(self.root, "#recycle"), exist_ok=True)
        self.fake_path = os.path.join(self.harness.tmpdir, "fake_ffmpeg.py")
        write_text(self.fake_path, FAKE_FFMPEG)
        self.ffmpeg_cmd = [sys.executable, self.fake_path]
        self.harness.services.scanner._video_engine_factory = (
            lambda cfg: video_similarity.VideoEngine(
                cfg, ffmpeg_cmd=self.ffmpeg_cmd))
        self.session = self.harness.login()

    def tearDown(self):
        self.harness.cleanup()

    def _engine(self, timeout_cap=None, cache=None):
        return video_similarity.VideoEngine(
            self.harness.config, ffmpeg_cmd=self.ffmpeg_cmd,
            timeout_cap=timeout_cap, cache=cache)

    def _video_options(self, **overrides):
        options = self.harness.services.settings.get()
        options.update({
            "video_similarity_enabled": True,
            "video_sample_frames": 5,
            "video_similarity_threshold": 90,
            "video_concurrency": 1,
        })
        options.update(overrides)
        return options

    def _scan(self, **overrides):
        return self.harness.run_scan(options=self._video_options(**overrides),
                                     timeout=120)


class VideoSettingsTest(unittest.TestCase):
    def test_defaults(self):
        defaults = settings_module.default_settings()
        self.assertFalse(defaults["video_similarity_enabled"])
        self.assertEqual(defaults["video_sample_frames"], 20)
        self.assertEqual(defaults["video_max_sample_interval_sec"], 300)
        self.assertEqual(defaults["video_similarity_threshold"], 90)
        self.assertEqual(defaults["video_concurrency"], 1)
        self.assertEqual(defaults["video_hwaccel"], "auto")
        self.assertEqual(defaults["video_max_file_mb"], 8192)
        self.assertEqual(defaults["video_timeout_sec"], 180)
        self.assertTrue(defaults["video_cache"])
        self.assertIn(".mp4", defaults["video_extensions"])

    def test_validation(self):
        patch = settings_module.validate_patch({
            "video_similarity_enabled": True,
            "video_sample_frames": 50,
            "video_max_sample_interval_sec": 600,
            "video_similarity_threshold": 80,
            "video_concurrency": 2,
            "video_hwaccel": "off",
            "video_max_file_mb": 4096,
            "video_timeout_sec": 600,
            "video_cache": False,
            "video_extensions": [".mp4", ".mkv"],
        })
        self.assertTrue(patch["video_similarity_enabled"])
        self.assertEqual(patch["video_sample_frames"], 50)
        invalid = [
            {"video_sample_frames": 4},
            {"video_sample_frames": 101},
            {"video_max_sample_interval_sec": 59},
            {"video_max_sample_interval_sec": 601},
            {"video_similarity_threshold": 101},
            {"video_concurrency": 3},
            {"video_hwaccel": "cuda"},
            {"video_max_file_mb": 0},
            {"video_max_file_mb": 102401},
            {"video_timeout_sec": 29},
            {"video_timeout_sec": 1801},
            {"video_cache": "yes"},
            {"video_extensions": ["mp4"]},
        ]
        for item in invalid:
            with self.assertRaises(Exception, msg=str(item)):
                settings_module.validate_patch(item)

    def test_meta_video_capabilities(self):
        harness = Harness()
        try:
            harness.services.scanner._video_engine_factory = (
                lambda cfg: video_similarity.VideoEngine(cfg, ffmpeg_cmd=[]))
            web = WebServer(harness).start()
            try:
                client = web.client
                client.login()
                status, _headers, payload = client.json("GET", "/api/meta")
                self.assertEqual(status, 200)
                video = payload["video"]
                for key in ("enabled", "ffmpeg_path", "ffmpeg_version",
                            "hwaccels", "gpu_hint", "defaults"):
                    self.assertIn(key, video)
                self.assertFalse(video["enabled"])
                self.assertEqual(video["defaults"]["video_sample_frames"], 20)
                # v0021 P1：外部 FFmpeg 路径与硬解模式能力（旧键保留）
                ffmpeg = video["ffmpeg"]
                for key in ("available", "path", "version", "source",
                            "configured_path", "hwaccels"):
                    self.assertIn(key, ffmpeg)
                self.assertIn(ffmpeg["source"],
                              ("settings", "env", "which", "unavailable"))
                for key in ("hwaccel_modes", "hwaccel_mode",
                            "hwaccel_effective", "hwaccel_fallback"):
                    self.assertIn(key, video)
                self.assertEqual(video["hwaccel_modes"],
                                 list(config.VIDEO_HWACCEL_MODES))
                self.assertIn("vaapi", video["hwaccel_modes"])
                self.assertIn("videotoolbox", video["hwaccel_modes"])
                self.assertEqual(video["defaults"]["ffmpeg_path"], "")
                self.assertIn("asr", video)
                for key in ("enabled", "configured", "available",
                            "reason", "weight"):
                    self.assertIn(key, video["asr"])
                self.assertFalse(video["asr"]["enabled"])
                self.assertAlmostEqual(video["asr"]["weight"], 0.15)
            finally:
                web.stop()
        finally:
            harness.cleanup()


class PhashAndDtwTest(unittest.TestCase):
    def test_phash_stability(self):
        gray = bytes((x * 7 + y * 3) % 256 for y in range(32) for x in range(32))
        inverted = bytes(255 - value for value in gray)
        same = video_similarity.phash_from_gray(gray)
        self.assertEqual(same, video_similarity.phash_from_gray(gray))
        self.assertAlmostEqual(video_similarity.frame_similarity(same, same), 1.0)
        different = video_similarity.phash_from_gray(inverted)
        self.assertLess(video_similarity.frame_similarity(same, different), 0.9)

    def test_dynamic_sample_formula(self):
        compute = video_similarity.compute_n_eff
        self.assertEqual(compute(120, 20, 300), 20)      # 20 > ceil(0.4)
        self.assertEqual(compute(6000, 20, 300), 20)     # ceil(20) == 20
        self.assertEqual(compute(9000, 20, 300), 30)     # ceil(30)
        self.assertEqual(compute(60000, 20, 300), 100)   # 超长截断 100
        self.assertEqual(compute(0, 20, 300), 20)

    def test_dtw_and_score(self):
        frame_a = 0xAAAAAAAAAAAAAAAA
        frame_b = 0x5555555555555555
        frame_c = 0xFFFFFFFFFFFFFFFF
        avg, coverage = video_similarity.dtw_stats(
            [frame_a, frame_b, frame_c], [frame_a, frame_b, frame_c])
        self.assertAlmostEqual(avg, 1.0)
        self.assertAlmostEqual(coverage, 1.0)
        avg2, coverage2 = video_similarity.dtw_stats(
            [frame_a, frame_a, frame_a], [frame_a, frame_b, frame_c])
        self.assertLess(avg2, 1.0)
        result = video_similarity.pair_score([frame_a, frame_a, frame_a],
                                             [frame_a, frame_b, frame_c], 90)
        self.assertIsNone(result)
        # 完全不同帧 → 分数远低于阈值
        self.assertIsNone(video_similarity.pair_score([frame_a], [frame_b], 90))
        # 相同单帧：DTW 路径完整对齐，coverage 取 min(nA,nB) 归一（=1.0）
        same = video_similarity.pair_score([frame_a], [frame_a], 90)
        self.assertIsNotNone(same)
        self.assertEqual(same[0], 100)
        # 安全团：A~B 高、B~C 低 → C 不能加入
        items = [
            {"analysis": {"duration": 120, "frame_hashes": [frame_a]}},
            {"analysis": {"duration": 120, "frame_hashes": [frame_a]}},
            {"analysis": {"duration": 120, "frame_hashes": [frame_b]}},
        ]
        clusters = video_similarity.build_video_groups(items, 90)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(sorted(clusters[0]["indices"]), [0, 1])


class FFmpegSettingsPriorityTest(unittest.TestCase):
    """v0021 P1：外部 FFmpeg 路径解析优先级 / 来源 / 设置缓存失效。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-ffmpeg-priority-")
        self.settings_exe = self._copy_executable("settings-ffmpeg")
        self.env_exe = self._copy_executable("env-ffmpeg")
        self.which_exe = self._copy_executable("which-ffmpeg")
        self.runtime_config = config.RuntimeConfig(
            data_dir=os.path.join(self.tmpdir, "data"),
            dev_mode=True, dev_roots=[], environ={})
        video_similarity._PROBE_CACHE.clear()

    def tearDown(self):
        video_similarity._PROBE_CACHE.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _copy_executable(self, name):
        path = os.path.join(self.tmpdir, name)
        shutil.copyfile(sys.executable, path)
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass
        return path

    def test_settings_over_env(self):
        with mock.patch.dict(os.environ, {"DUP_FFMPEG_PATH": self.env_exe}):
            tool = video_similarity.FFmpegTool(
                settings_provider=lambda: {"ffmpeg_path": self.settings_exe})
            self.assertEqual(tool.resolve(self.runtime_config),
                             [self.settings_exe])
            self.assertEqual(tool.source, "settings")
            self.assertEqual(tool.configured_path, self.settings_exe)

    def test_env_when_settings_empty(self):
        with mock.patch.dict(os.environ, {"DUP_FFMPEG_PATH": self.env_exe}):
            tool = video_similarity.FFmpegTool(
                settings_provider=lambda: {"ffmpeg_path": ""})
            self.assertEqual(tool.resolve(self.runtime_config), [self.env_exe])
            self.assertEqual(tool.source, "env")
            self.assertEqual(tool.configured_path, "")

    def test_runtime_config_path_used_as_env_level(self):
        cfg_exe = self._copy_executable("cfg-ffmpeg")
        runtime_config = config.RuntimeConfig(
            data_dir=os.path.join(self.tmpdir, "data2"),
            dev_mode=True, dev_roots=[],
            environ={"DUP_FFMPEG_PATH": cfg_exe})
        with mock.patch.dict(os.environ, {"DUP_FFMPEG_PATH": ""}):
            tool = video_similarity.FFmpegTool()
            self.assertEqual(tool.resolve(runtime_config), [cfg_exe])
            self.assertEqual(tool.source, "env")

    def test_which_fallback(self):
        with mock.patch.dict(os.environ, {"DUP_FFMPEG_PATH": ""}):
            with mock.patch.object(video_similarity.shutil, "which",
                                   return_value=self.which_exe):
                tool = video_similarity.FFmpegTool()
                self.assertEqual(tool.resolve(self.runtime_config),
                                 [self.which_exe])
                self.assertEqual(tool.source, "which")

    def test_explicit_cmd_wins(self):
        tool = video_similarity.FFmpegTool(cmd=[self.which_exe, "-fake"])
        with mock.patch.dict(os.environ, {"DUP_FFMPEG_PATH": self.env_exe}):
            self.assertEqual(tool.resolve(self.runtime_config),
                             [self.which_exe, "-fake"])
            self.assertEqual(tool.source, "explicit")
            self.assertEqual(tool.configured_path, "")

    def test_settings_change_clears_probe_cache(self):
        settings = {"ffmpeg_path": self.settings_exe}
        tool = video_similarity.FFmpegTool(
            settings_provider=lambda: settings)
        tool.resolve(self.runtime_config)
        self.assertEqual(tool.path, self.settings_exe)
        # 模拟已探测到 version/hwaccels，settings 路径变化后必须清空。
        tool._version = "6.1.1-fake"
        tool._hwaccels = ["vaapi"]
        settings["ffmpeg_path"] = self.env_exe
        self.assertEqual(tool.resolve(self.runtime_config), [self.env_exe])
        self.assertIsNone(tool._version)
        self.assertIsNone(tool._hwaccels)
        self.assertEqual(tool.configured_path, self.env_exe)
        self.assertEqual(tool.source, "settings")

    def test_unavailable_source(self):
        with mock.patch.dict(os.environ, {"DUP_FFMPEG_PATH": ""}):
            with mock.patch.object(video_similarity.shutil, "which",
                                   return_value=None):
                tool = video_similarity.FFmpegTool()
                self.assertIsNone(tool.resolve(self.runtime_config))
                self.assertFalse(tool.available())
                self.assertEqual(tool.source, "unavailable")
                self.assertEqual(tool.configured_path, "")

    def test_hwaccel_mode_resolution(self):
        resolve = video_similarity.hwaccel_for_mode
        self.assertEqual(resolve("off", ["vaapi"]), ("none", "none", False))
        self.assertEqual(resolve("auto", ["vaapi", "qsv"]),
                         ("vaapi", "auto", False))
        self.assertEqual(resolve("auto", []), ("none", "none", False))
        self.assertEqual(resolve("vaapi", ["vaapi"]),
                         ("vaapi", "vaapi", False))
        self.assertEqual(resolve("qsv", ["vaapi"]), ("none", "none", True))
        # NVIDIA 的 -hwaccels 通常列出 cuda/cuvid，而非 nvdec：需映射为实际名称。
        self.assertEqual(resolve("nvdec", ["cuda"]), ("nvdec", "cuda", False))
        self.assertEqual(resolve("nvdec", ["cuvid"]), ("nvdec", "cuvid", False))
        self.assertEqual(resolve("nvdec", ["nvdec"]), ("nvdec", "nvdec", False))
        self.assertEqual(resolve("nvdec", ["vaapi"]), ("none", "none", True))
        self.assertEqual(resolve("videotoolbox", ["videotoolbox"]),
                         ("videotoolbox", "videotoolbox", False))
        self.assertEqual(video_similarity.hwaccel_supported([]), [])
        self.assertEqual(video_similarity.hwaccel_supported(["cuda"]), ["nvdec"])
        self.assertEqual(
            video_similarity.hwaccel_supported(["vaapi", "qsv", "cuda"]),
            ["vaapi", "qsv", "nvdec"])

    def test_probe_ffmpeg_reports_settings_source(self):
        with mock.patch.object(video_similarity.FFmpegTool, "version",
                               return_value="6.1.1-fake"):
            with mock.patch.object(video_similarity.FFmpegTool, "hwaccels",
                                   return_value=["vaapi"]):
                info = video_similarity.probe_ffmpeg(
                    self.runtime_config, {"ffmpeg_path": self.settings_exe})
        self.assertTrue(info["available"])
        self.assertEqual(info["path"], self.settings_exe)
        self.assertEqual(info["source"], "settings")
        self.assertEqual(info["configured_path"], self.settings_exe)
        self.assertEqual(info["version"], "6.1.1-fake")
        self.assertEqual(info["hwaccels"], ["vaapi"])

    def test_capabilities_hwaccel_fallback(self):
        fake_info = {
            "available": True, "path": "/fake/ffmpeg", "version": "6.0",
            "hwaccels": ["vaapi"], "source": "settings",
            "configured_path": "/fake/ffmpeg",
        }
        with mock.patch.object(video_similarity, "probe_ffmpeg",
                               return_value=fake_info):
            caps = video_similarity.video_capabilities(
                self.runtime_config, {"video_hwaccel": "qsv"})
            self.assertEqual(caps["ffmpeg"]["source"], "settings")
            self.assertEqual(caps["ffmpeg"]["configured_path"], "/fake/ffmpeg")
            self.assertIn("qsv", caps["hwaccel_modes"])
            self.assertEqual(caps["hwaccel_mode"], "qsv")
            self.assertEqual(caps["hwaccel_effective"], "none")
            self.assertTrue(caps["hwaccel_fallback"])
            caps2 = video_similarity.video_capabilities(
                self.runtime_config, {"video_hwaccel": "vaapi"})
            self.assertEqual(caps2["hwaccel_effective"], "vaapi")
            self.assertFalse(caps2["hwaccel_fallback"])

    def test_engine_and_subtitle_settings_provider(self):
        harness = Harness()
        try:
            engine = harness.services.scanner._video_engine_factory(
                harness.config)
            provider = engine.ffmpeg._settings_provider
            self.assertTrue(callable(provider))
            self.assertIn("ffmpeg_path", provider())
            subtitle_provider = harness.services.subtitles.ffmpeg._settings_provider
            self.assertTrue(callable(subtitle_provider))
            self.assertIn("ffmpeg_path", subtitle_provider())
        finally:
            harness.cleanup()


class FFmpegLocateTest(unittest.TestCase):
    def test_dup_ffmpeg_path_priority(self):
        harness = Harness()
        try:
            with mock.patch.dict(os.environ,
                                 {"DUP_FFMPEG_PATH": sys.executable}):
                tool = video_similarity.FFmpegTool()
                self.assertEqual(tool.resolve(harness.config),
                                 [sys.executable])
            with mock.patch.dict(os.environ, {"DUP_FFMPEG_PATH": ""}):
                with mock.patch.object(video_similarity.shutil, "which",
                                       return_value=None):
                    tool2 = video_similarity.FFmpegTool()
                    resolved = tool2.resolve(harness.config)
                    self.assertIsNone(resolved)
                    self.assertFalse(tool2.available())
        finally:
            harness.cleanup()


REAL_FFMPEG = shutil.which("ffmpeg")


class RealFFmpegIntegrationTest(unittest.TestCase):
    """可选真实 ffmpeg 集成测试：没有 ffmpeg 时自动跳过。"""

    @unittest.skipUnless(REAL_FFMPEG, "real ffmpeg not installed")
    def test_real_video_analysis(self):
        import subprocess
        tmpdir = tempfile.mkdtemp(prefix="dc-real-video-")
        try:
            source = os.path.join(tmpdir, "clip.mp4")
            command = [REAL_FFMPEG, "-y", "-nostdin", "-hide_banner",
                       "-loglevel", "error", "-f", "lavfi",
                       "-i", "testsrc=duration=2:size=64x64:rate=5",
                       "-pix_fmt", "yuv420p", source]
            subprocess.run(command, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
            if not os.path.isfile(source):
                self.skipTest("ffmpeg cannot generate sample video")
            runtime_config = config.RuntimeConfig(
                data_dir=os.path.join(tmpdir, "data"),
                dev_mode=True, dev_roots=[tmpdir], environ={})
            os.makedirs(os.path.join(tmpdir, "data"), exist_ok=True)
            engine = video_similarity.VideoEngine(runtime_config)
            options = {
                "video_sample_frames": 5,
                "video_max_sample_interval_sec": 300,
                "video_hwaccel": "off",
                "video_timeout_sec": 60,
                "video_cache": True,
                "video_max_file_mb": 64,
            }
            analysis = engine.analyze(source, options)
            self.assertGreater(analysis["duration"], 0)
            self.assertEqual(len(analysis["frame_hashes"]),
                             analysis["n_eff"])
            self.assertTrue(analysis["fingerprint"].startswith("v2:"))
            self.assertEqual(len(analysis["frame_hashes_center"]),
                             analysis["n_eff"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class FFmpegSilenceMp3Test(FakeFFmpegBase):
    """v0026：云端字幕连接测试的 1s 静音 MP3 生成（fake ffmpeg）。"""

    def test_extract_silence_mp3_command_and_file(self):
        out_path = os.path.join(self.harness.tmpdir, "silence.mp3")
        log_path = os.path.join(self.harness.tmpdir, "silence.log")
        tool = video_similarity.FFmpegTool(cmd=self.ffmpeg_cmd)
        with mock.patch.dict(os.environ, {"FAKE_FFMPEG_LOG": log_path}):
            tool.extract_silence_mp3(out_path, seconds=1.0, timeout=15.0)
        self.assertTrue(os.path.isfile(out_path))
        self.assertGreater(os.path.getsize(out_path), 0)
        with open(out_path, "rb") as handle:
            self.assertEqual(handle.read(3), b"ID3")
        with open(log_path, "r", encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line]
        command = [line for line in lines if "-f lavfi" in line][-1]
        self.assertIn("anullsrc=r=16000:cl=mono", command)
        self.assertIn("-t 1.000", command)
        self.assertIn("-f mp3", command)
        self.assertIn(out_path, command)

    def test_extract_silence_mp3_failure(self):
        out_path = os.path.join(self.harness.tmpdir, "silence-fail.mp3")
        tool = video_similarity.FFmpegTool(cmd=self.ffmpeg_cmd)
        with mock.patch.dict(os.environ,
                             {"FAKE_FFMPEG_SILENCE_FAIL": "1"}):
            with self.assertRaises(video_similarity.VideoSkip) as ctx:
                tool.extract_silence_mp3(out_path)
        self.assertEqual(ctx.exception.reason, "audio")
        self.assertFalse(os.path.isfile(out_path))


class FFmpegProbeTest(FakeFFmpegBase):
    def test_probe_duration_uses_info_loglevel(self):
        log_path = os.path.join(self.harness.tmpdir, "ffmpeg.log")
        video = make_video(os.path.join(self.root, "a.mp4"), seed=1,
                           duration=125.5)
        with mock.patch.dict(os.environ, {"FAKE_FFMPEG_LOG": log_path}):
            tool = video_similarity.FFmpegTool(cmd=self.ffmpeg_cmd)
            duration = tool.probe_duration(video, timeout=30)
        self.assertAlmostEqual(duration, 125.5, places=2)
        with open(log_path, "r", encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line]
        self.assertTrue(lines)
        self.assertIn("-loglevel info", lines[-1])
        self.assertNotIn("-f rawvideo", lines[-1])

    def test_probe_no_duration(self):
        video = write_bytes(os.path.join(self.root, "bad.mp4"),
                            b"NODURATION=1\n")
        tool = video_similarity.FFmpegTool(cmd=self.ffmpeg_cmd)
        self.assertIsNone(tool.probe_duration(video, timeout=30))

    def test_version_and_hwaccels(self):
        tool = video_similarity.FFmpegTool(cmd=self.ffmpeg_cmd)
        self.assertEqual(tool.version(), "6.1.1-fake")
        self.assertIn("vaapi", tool.hwaccels())
        self.assertEqual(tool.hwaccel_label(), "vaapi")

    def test_hwaccel_fallback_to_software(self):
        video = make_video(os.path.join(self.root, "hw.mp4"), seed=5)
        log_path = os.path.join(self.harness.tmpdir, "hw.log")
        engine = self._engine()
        with mock.patch.dict(os.environ, {
                "FAKE_FFMPEG_HWACCEL_FAIL": "1",
                "FAKE_FFMPEG_LOG": log_path}):
            analysis = engine.analyze(video, self._video_options())
        self.assertEqual(analysis["hwaccel"], "none")
        self.assertEqual(len(analysis["frame_hashes"]),
                         analysis["n_eff"])
        with open(log_path, "r", encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line]
        # 第一帧带 -hwaccel 失败，随后自动回退为不带 -hwaccel 的软件解码
        hw_lines = [line for line in lines if "-hwaccel auto" in line]
        sw_lines = [line for line in lines
                    if "-f rawvideo" in line and "-hwaccel" not in line]
        self.assertTrue(hw_lines)
        self.assertTrue(sw_lines)

    def test_corrupt_and_cache_failed_marker(self):
        video = write_bytes(os.path.join(self.root, "corrupt.mp4"),
                            b"DURATION=120\nCORRUPT=1\n")
        engine = self._engine()
        cache = engine.ensure_cache(self._video_options())
        with self.assertRaises(video_similarity.VideoSkip) as ctx:
            engine.analyze(video, self._video_options())
        self.assertEqual(ctx.exception.reason, "decode")
        from dupcleaner import fsutil
        identity = fsutil.file_identity(video)
        key = video_similarity.cache_key(identity, 5, 300)
        cached = cache.get(key)
        self.assertTrue(cached.get("failed"))
        self.assertEqual(cached.get("reason"), "decode")

    def test_timeout(self):
        video = make_video(os.path.join(self.root, "slow.mp4"), seed=1)
        engine = self._engine(timeout_cap=0.4)
        started = time.time()
        with mock.patch.dict(os.environ, {"FAKE_FFMPEG_SLEEP": "2"}):
            with self.assertRaises(video_similarity.VideoSkip) as ctx:
                engine.analyze(video, self._video_options())
        self.assertLess(time.time() - started, 2.0)
        self.assertEqual(ctx.exception.reason, "timeout")

    def test_cache_hit_and_invalidation(self):
        video = make_video(os.path.join(self.root, "cached.mp4"), seed=2)
        engine = self._engine()
        first = engine.analyze(video, self._video_options())
        self.assertFalse(first["cache_hit"])
        second = engine.analyze(video, self._video_options())
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        with open(video, "ab") as handle:
            handle.write(b"\n# changed")
        third = engine.analyze(video, self._video_options())
        self.assertFalse(third["cache_hit"])

    def test_missing_ffmpeg(self):
        video = make_video(os.path.join(self.root, "x.mp4"), seed=1)
        engine = video_similarity.VideoEngine(
            self.harness.config, ffmpeg_cmd=["definitely-not-ffmpeg-xyz"])
        with self.assertRaises(video_similarity.VideoSkip) as ctx:
            engine.analyze(video, self._video_options())
        self.assertEqual(ctx.exception.reason, "error")


class HwaccelExtractionTest(FakeFFmpegBase):
    """v0021 P1：显式硬解模式传参/降级（fake ffmpeg，不依赖真实视频）。"""

    def _run_with_log(self, video, hwaccel):
        log_path = os.path.join(self.harness.tmpdir, "hw-%s.log" % hwaccel)
        with mock.patch.dict(os.environ, {"FAKE_FFMPEG_LOG": log_path}):
            analysis = self._engine().analyze(
                video, self._video_options(video_hwaccel=hwaccel))
        with open(log_path, "r", encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line]
        return analysis, lines

    def test_explicit_mode_passes_named_hwaccel(self):
        video = make_video(os.path.join(self.root, "explicit.mp4"), seed=9)
        analysis, lines = self._run_with_log(video, "vaapi")
        self.assertEqual(analysis["hwaccel"], "vaapi")
        self.assertEqual(analysis["hwaccel_mode"], "vaapi")
        self.assertFalse(analysis["hwaccel_fallback"])
        self.assertTrue(any("-hwaccel vaapi" in line for line in lines))
        self.assertFalse(any("-hwaccel auto" in line for line in lines))

    def test_unsupported_explicit_mode_falls_back_none(self):
        # fake ffmpeg 的 -hwaccels 只有 vaapi/qsv，videotoolbox 不存在 → 回退 none
        video = make_video(os.path.join(self.root, "unsupported.mp4"), seed=10)
        analysis, lines = self._run_with_log(video, "videotoolbox")
        self.assertEqual(analysis["hwaccel"], "none")
        self.assertTrue(analysis["hwaccel_fallback"])
        # -hwaccels 是能力探测参数，这里只检查真正的 -hwaccel 抽帧参数。
        self.assertFalse(any("-hwaccel" in line.split() for line in lines))

    def test_auto_mode_reports_detected_label(self):
        video = make_video(os.path.join(self.root, "auto.mp4"), seed=11)
        analysis, lines = self._run_with_log(video, "auto")
        self.assertEqual(analysis["hwaccel"], "vaapi")
        self.assertFalse(analysis["hwaccel_fallback"])
        self.assertTrue(any("-hwaccel auto" in line for line in lines))

    def test_off_mode_never_passes_hwaccel(self):
        video = make_video(os.path.join(self.root, "off.mp4"), seed=12)
        analysis, lines = self._run_with_log(video, "off")
        self.assertEqual(analysis["hwaccel"], "none")
        self.assertFalse(analysis["hwaccel_fallback"])
        self.assertFalse(any("-hwaccel" in line.split() for line in lines))


class VideoScanTest(FakeFFmpegBase):
    def _fixture(self):
        first = make_video(os.path.join(self.root, "v1.mp4"), seed=7,
                           duration=120)
        second = make_video(os.path.join(self.root, "sub", "v2.mp4"), seed=7,
                            duration=121)
        third = make_video(os.path.join(self.root, "v3.mp4"), seed=3,
                           duration=120)
        self.harness.set_creation(first, 1000)
        self.harness.set_creation(second, 2000)
        self.harness.set_creation(third, 3000)
        # 精准重复的文本文件用于验证分组共存
        write_text(os.path.join(self.root, "dup1.txt"), "exact " * 20)
        write_text(os.path.join(self.root, "dup2.txt"), "exact " * 20)
        return {"first": first, "second": second, "third": third}

    def test_video_groups_and_fields(self):
        fixture = self._fixture()
        scan_id, meta = self.harness.run_scan(
            options=self._video_options(), timeout=120)
        self.assertEqual(meta["status"], "completed")
        stats = meta["stats"]
        self.assertEqual(stats["video_groups"], 1)
        self.assertEqual(stats["video_files"], 2)
        self.assertEqual(stats["video_candidates"], 3)
        self.assertEqual(stats["exact_groups"], 1)
        self.assertGreaterEqual(stats["video_cache_misses"], 2)
        self.assertGreaterEqual(stats["video_ffmpeg_used"], 1)
        self.assertEqual(stats["video_skipped"]["decode"], 0)
        groups = self.harness.groups(scan_id)
        video_groups = [item for item in groups if item["match_type"] == "video"]
        self.assertEqual(len(video_groups), 1)
        group = video_groups[0]
        self.assertTrue(group["group_id"].startswith("v-"))
        self.assertEqual(group["hash"], "")
        self.assertEqual(group["algo"], "")
        self.assertGreaterEqual(group["similarity_score"], 90)
        self.assertEqual(group["video_similarity_score"],
                         group["similarity_score"])
        self.assertTrue(group["video_fingerprint"].startswith("v2:"))
        self.assertEqual(group["video_sample_frames"], 5)
        self.assertEqual(group["video_hwaccel"], "vaapi")
        self.assertEqual(group["video_duration"], 121)
        self.assertFalse(group["video_low_confidence"])
        self.assertFalse(group["manual_review_required"])
        self.assertTrue(group["allow_batch_delete"])
        self.assertEqual(group["keep_reason"], "latest_created")
        self.assertFalse(group["time_fallback"])
        self.assertTrue(group["files"][0]["file_id"].startswith("vf-"))
        for file_info in group["files"]:
            self.assertEqual(file_info["match_type"], "video")
            self.assertFalse(file_info["default_selected"])
            self.assertTrue(file_info["video_fingerprint"].startswith("v2:"))
            self.assertGreater(file_info["video_sample_frames"], 0)
            self.assertEqual(file_info["video_hwaccel"], "vaapi")
            self.assertEqual(file_info["hash"], "")
        keeper = [item for item in group["files"] if item["is_keep"]][0]
        self.assertEqual(keeper["name"], "v2.mp4")
        non_keeper = [item for item in group["files"] if not item["is_keep"]][0]
        self.assertEqual(non_keeper["name"], "v1.mp4")
        # 不相关视频与精准重复组都不在视频组里
        paths = {item["path"] for item in group["files"]}
        self.assertNotIn(fixture["third"], paths)

        page = self.harness.services.scanner.groups_page(
            scan_id, match_type="video")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["video_total"], 1)
        self.assertEqual(page["exact_total"], 1)
        page_all = self.harness.services.scanner.groups_page(scan_id)
        self.assertIn("video_total", page_all)
        self.assertEqual(page_all["video_total"], 1)

        # CSV 标记视频视觉相似
        csv_path = self.harness.services.scanner.write_csv(scan_id)
        try:
            with open(csv_path, "rb") as handle:
                raw = handle.read()
            self.assertIn(u"视频视觉相似".encode("utf-8"), raw)
        finally:
            os.unlink(csv_path)

        # 列宽/字段：视频组在 similarity_desc 排序中可排序
        sorted_page = self.harness.services.scanner.groups_page(
            scan_id, sort="similarity_desc", limit=10)
        self.assertTrue(sorted_page["groups"])

    def test_video_cache_hits_on_second_scan(self):
        self._fixture()
        _scan_id, first_meta = self.harness.run_scan(
            options=self._video_options(), timeout=120)
        self.assertEqual(first_meta["stats"]["video_cache_hits"], 0)
        _scan_id2, second_meta = self.harness.run_scan(
            options=self._video_options(), timeout=120)
        self.assertGreater(second_meta["stats"]["video_cache_hits"], 0)
        cache_file = os.path.join(self.harness.data_dir,
                                  config.VIDEO_CACHE_FILENAME)
        self.assertTrue(os.path.isfile(cache_file))

    def test_video_skipped_no_duration_and_corrupt(self):
        write_bytes(os.path.join(self.root, "nodur.mp4"),
                    b"DURATION=120\nNODURATION=1\n")
        write_bytes(os.path.join(self.root, "corrupt.mp4"),
                    b"DURATION=120\nCORRUPT=1\n")
        _scan_id, meta = self.harness.run_scan(
            options=self._video_options(), timeout=120)
        skipped = meta["stats"]["video_skipped"]
        self.assertGreaterEqual(skipped["no_duration"], 1)
        self.assertGreaterEqual(skipped["decode"], 1)
        self.assertEqual(meta["stats"]["video_groups"], 0)

    def test_video_disabled_by_default(self):
        self._fixture()
        _scan_id, meta = self.harness.run_scan(timeout=120)
        self.assertEqual(meta["stats"]["video_groups"], 0)
        self.assertEqual(meta["stats"]["video_candidates"], 0)

    def test_video_size_limit(self):
        write_bytes(os.path.join(self.root, "big.mp4"),
                    b"DURATION=120\nSEED=1\n" + b"x" * (1200 * 1024))
        _scan_id, meta = self.harness.run_scan(
            options=self._video_options(video_max_file_mb=1), timeout=120)
        self.assertGreaterEqual(meta["stats"]["video_skipped"]["size"], 1)

    def test_stop_during_video_stage(self):
        make_video(os.path.join(self.root, "s1.mp4"), seed=1)
        make_video(os.path.join(self.root, "s2.mp4"), seed=1,
                   duration=121)
        scanner = self.harness.services.scanner
        with mock.patch.dict(os.environ, {"FAKE_FFMPEG_SLEEP": "0.3"}):
            scan_id = scanner.start_scan(
                [self.root], [], self._video_options(
                    video_sample_frames=20), "admin")
            deadline = time.time() + 60
            seen_video = False
            while time.time() < deadline:
                meta = scanner.get_scan(scan_id)
                if meta["stage"] == "video":
                    seen_video = True
                    break
                if meta["status"] in ("completed", "failed", "stopped"):
                    break
                time.sleep(0.05)
            stopped = scanner.stop(scan_id)
            self.assertIn(stopped["status"], ("stopping", "stopped"))
            final = self.harness.wait_scan(scan_id, timeout=120)
        self.assertIn(final["status"], ("stopped", "completed"))
        if seen_video:
            self.assertEqual(final["status"], "stopped")

    def test_xlsx_export(self):
        self._fixture()
        scan_id, _meta = self.harness.run_scan(
            options=self._video_options(), timeout=120)
        path = self.harness.services.scanner.write_xlsx(scan_id)
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                for required in ("[Content_Types].xml", "_rels/.rels",
                                 "xl/workbook.xml",
                                 "xl/worksheets/sheet1.xml"):
                    self.assertIn(required, names)
                sheet = archive.read("xl/worksheets/sheet1.xml")
            root = ElementTree.fromstring(sheet)
            text = sheet.decode("utf-8")
            self.assertIn(u"匹配类型", text)
            self.assertIn(u"视频指纹", text)
            self.assertIn(u"视频视觉相似", text)
            self.assertIn(u"哈希一致", text)
            self.assertIn(u"采样帧数", text)
            self.assertIn(u"硬解", text)
            self.assertIn(u"视频时长(秒)", text)
            rows = root.findall(
                "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheetData/"
                "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row")
            self.assertGreaterEqual(len(rows), 5)
        finally:
            os.unlink(path)
        self.assertEqual(self.harness.services.scanner.filename_for_xlsx(scan_id),
                         "duplicate-scan-%s.xlsx" % scan_id)

    def test_xlsx_api_endpoint(self):
        self._fixture()
        web = WebServer(self.harness).start()
        try:
            client = web.client
            csrf = client.login()
            status, _headers, payload = client.json(
                "POST", "/api/scans",
                {"roots": [self.root],
                 "options": {"video_similarity_enabled": True,
                             "video_sample_frames": 5,
                             "video_similarity_threshold": 90}},
                csrf=csrf)
            self.assertEqual(status, 202)
            scan_id = payload["scan_id"]
            deadline = time.time() + 120
            while time.time() < deadline:
                _s, _h, scan_payload = client.json(
                    "GET", "/api/scans/%s" % scan_id)
                if scan_payload["scan"]["status"] == "completed":
                    break
                time.sleep(0.05)
            status, headers, raw = client.request(
                "GET", "/api/scans/%s/xlsx" % scan_id)
            self.assertEqual(status, 200)
            self.assertIn("spreadsheetml", headers["content-type"][0])
            self.assertIn("attachment", headers["content-disposition"][0])
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())
        finally:
            web.stop()


class VideoDeletionTest(FakeFFmpegBase):
    def _scan_pair(self):
        first = make_video(os.path.join(self.root, "d1.mp4"), seed=9,
                           duration=120)
        second = make_video(os.path.join(self.root, "d2.mp4"), seed=9,
                            duration=121)
        self.harness.set_creation(first, 1000)
        self.harness.set_creation(second, 2000)
        scan_id, meta = self.harness.run_scan(
            options=self._video_options(), timeout=120)
        self.assertEqual(meta["stats"]["video_groups"], 1)
        group = [item for item in self.harness.groups(scan_id)
                 if item["match_type"] == "video"][0]
        keeper = [item for item in group["files"] if item["is_keep"]][0]
        non_keeper = [item for item in group["files"]
                      if not item["is_keep"]][0]
        return scan_id, group, keeper, non_keeper

    def _wait_job(self, scan_id, job_id, timeout=60.0):
        deletion = self.harness.services.deletion
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = deletion.get_job(scan_id, job_id)
            if job.get("status") in ("completed", "partial", "failed",
                                     "stopped"):
                return job
            time.sleep(0.02)
        raise AssertionError("delete job did not finish")

    def test_video_batch_delete_scope(self):
        scan_id, group, keeper, non_keeper = self._scan_pair()
        deletion = self.harness.services.deletion
        plan = deletion.create_plan(self.session, scan_id, {
            "scope": "video_similar", "recycle": True})
        self.assertEqual(plan["count"], 1)
        self.assertEqual(plan["video_count"], 1)
        self.assertEqual(plan["video_bytes"], non_keeper["size"])
        self.assertEqual(plan["exact_count"], 0)
        self.assertEqual(plan["similar_count"], 0)
        self.assertFalse(plan["manual_review"])
        self.assertEqual(plan["files"][0]["match_type"], "video")
        job_id = deletion.start_delete(self.session, scan_id, {
            "plan_id": plan["plan_id"], "confirm": "DELETE"})
        job = self._wait_job(scan_id, job_id)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["results"][0]["match_type"], "video")
        self.assertFalse(os.path.exists(non_keeper["path"]))
        self.assertTrue(os.path.exists(keeper["path"]))

    def test_video_explicit_file_ids_no_review(self):
        scan_id, group, keeper, non_keeper = self._scan_pair()
        deletion = self.harness.services.deletion
        plan = deletion.create_plan(self.session, scan_id, {
            "file_ids": [non_keeper["file_id"]], "recycle": True})
        self.assertEqual(plan["count"], 1)
        self.assertEqual(plan["video_count"], 1)
        self.assertFalse(plan["manual_review"])
        # keeper 仍不可删除
        plan2 = deletion.create_plan(self.session, scan_id, {
            "file_ids": [keeper["file_id"], non_keeper["file_id"]],
            "recycle": True})
        self.assertEqual(plan2["count"], 1)
        self.assertIn("IS_KEEPER",
                      [item["reason"] for item in plan2["skipped"]])

    def test_video_changed_file_skipped(self):
        scan_id, group, keeper, non_keeper = self._scan_pair()
        with open(non_keeper["path"], "ab") as handle:
            handle.write(b"\nchanged")
        deletion = self.harness.services.deletion
        with self.assertRaises(Exception) as ctx:
            deletion.create_plan(self.session, scan_id, {
                "scope": "video_similar", "recycle": True})
        self.assertEqual(ctx.exception.code, "PLAN_INVALID")
        reasons = [item["reason"] for item in ctx.exception.details["skipped"]]
        self.assertIn("FILE_CHANGED", reasons)

    def test_all_duplicates_excludes_video(self):
        scan_id, _group, _keeper, _non_keeper = self._scan_pair()
        deletion = self.harness.services.deletion
        with self.assertRaises(Exception) as ctx:
            deletion.create_plan(self.session, scan_id, {
                "scope": "all_duplicates", "recycle": True})
        # 只有视频组时 scope=all_duplicates 不应包含视频 → 无计划
        self.assertEqual(ctx.exception.code, "PLAN_INVALID")
