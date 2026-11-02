# -*- coding: utf-8 -*-
"""可选 ASR 语音辅助 + 短视频相似匹配测试（fake ffmpeg + fake ASR，无真实模型）。"""

import json
import os
import sys
import time
import unittest
from unittest import mock

from tests.helpers import Harness, write_bytes, write_text
from tests.test_video import FAKE_FFMPEG, FakeFFmpegBase, make_video

from dupcleaner import config, settings as settings_module, similarity
from dupcleaner import video_similarity

FAKE_ASR = r'''
import json
import os
import sys
import time

args = sys.argv[1:]
log_path = os.environ.get("FAKE_ASR_LOG")

def find(name):
    if name in args:
        index = args.index(name)
        if index + 1 < len(args):
            return args[index + 1]
    return None

audio = find("--audio")
outdir = find("--outdir") or "."
if log_path:
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"args": args,
                                     "audio_exists": bool(audio and os.path.isfile(audio))}) + "\n")
    except OSError:
        pass

mode = os.environ.get("FAKE_ASR_MODE", "ok")
sleep_for = float(os.environ.get("FAKE_ASR_SLEEP", "0") or 0)
if sleep_for > 0:
    time.sleep(sleep_for)
if mode == "fail":
    sys.stderr.write("fake asr: failed\n")
    sys.exit(2)
if mode == "empty":
    try:
        with open(os.path.join(outdir, "speech.txt"), "w", encoding="utf-8") as handle:
            handle.write("")
    except OSError:
        sys.exit(3)
    sys.exit(0)
text = os.environ.get("FAKE_ASR_TEXT") or (
    "UNIQUE_TRANSCRIPT_XYZ hello world this is the same spoken content "
    "for asr auxiliary similarity testing")
if mode == "stdout":
    sys.stdout.write(text)
    sys.exit(0)
try:
    with open(os.path.join(outdir, "speech.txt"), "w", encoding="utf-8") as handle:
        handle.write(text)
except OSError:
    sys.exit(3)
sys.exit(0)
'''


def asr_command(asr_path, python=None):
    python = python or sys.executable
    template = ('"%s" "%s" --audio "{audio}" --outdir "{outdir}" '
                '--stem "{stem}"') % (python, asr_path)
    return template


def signature_for(text):
    return similarity.minhash_signature(
        similarity.text_shingles(similarity.normalize_text(text)))


class AsrTestBase(FakeFFmpegBase):
    def setUp(self):
        FakeFFmpegBase.setUp(self)
        self.asr_path = os.path.join(self.harness.tmpdir, "fake_asr.py")
        write_text(self.asr_path, FAKE_ASR)
        self.asr_log = os.path.join(self.harness.tmpdir, "asr.log")
        self.asr_cmd = asr_command(self.asr_path)

    def _asr_options(self, **overrides):
        options = self._video_options()
        options.update({
            "video_asr_enabled": True,
            "video_asr_command": self.asr_cmd,
            "video_asr_weight": 0.15,
            "video_asr_timeout_sec": 300,
            "video_asr_max_sec": 600,
        })
        options.update(overrides)
        return options

    def _engine(self, asr_timeout_cap=None):
        return video_similarity.VideoEngine(
            self.harness.config, ffmpeg_cmd=self.ffmpeg_cmd,
            asr_timeout_cap=asr_timeout_cap)

    def _asr_log_lines(self):
        try:
            with open(self.asr_log, "r", encoding="utf-8") as handle:
                return [json.loads(line) for line in handle if line.strip()]
        except OSError:
            return []


class AsrSettingsTest(unittest.TestCase):
    def test_defaults(self):
        defaults = settings_module.default_settings()
        self.assertFalse(defaults["video_asr_enabled"])
        self.assertEqual(defaults["video_asr_command"], "")
        self.assertAlmostEqual(defaults["video_asr_weight"], 0.15)
        self.assertEqual(defaults["video_asr_timeout_sec"], 300)
        self.assertEqual(defaults["video_asr_max_sec"], 600)

    def test_validation(self):
        patch = settings_module.validate_patch({
            "video_asr_enabled": True,
            "video_asr_command": "whisper --input {audio}",
            "video_asr_weight": 0.3,
            "video_asr_timeout_sec": 60,
            "video_asr_max_sec": 120,
        })
        self.assertTrue(patch["video_asr_enabled"])
        self.assertAlmostEqual(patch["video_asr_weight"], 0.3)
        for item in [
            {"video_asr_weight": -0.01},
            {"video_asr_weight": 0.51},
            {"video_asr_weight": "x"},
            {"video_asr_command": 123},
            {"video_asr_timeout_sec": 29},
            {"video_asr_timeout_sec": 1801},
            {"video_asr_max_sec": 29},
            {"video_asr_max_sec": 3601},
            {"video_asr_enabled": "yes"},
        ]:
            with self.assertRaises(Exception, msg=str(item)):
                settings_module.validate_patch(item)

    def test_capabilities_availability(self):
        harness = Harness()
        try:
            disabled = video_similarity.video_capabilities(harness.config, {})
            self.assertFalse(disabled["asr"]["enabled"])
            self.assertEqual(disabled["asr"]["reason"], "disabled")
            self.assertAlmostEqual(disabled["asr"]["weight"], 0.15)
            no_command = video_similarity.video_capabilities(
                harness.config, {"video_asr_enabled": True})
            self.assertTrue(no_command["asr"]["enabled"])
            self.assertFalse(no_command["asr"]["configured"])
            self.assertEqual(no_command["asr"]["reason"], "no_command")
            missing = video_similarity.video_capabilities(
                harness.config, {"video_asr_enabled": True,
                                 "video_asr_command": "/definitely/not/exist"})
            self.assertEqual(missing["asr"]["reason"], "command_not_found")
        finally:
            harness.cleanup()


class AsrMathTest(unittest.TestCase):
    def test_combine_scores(self):
        combine = video_similarity.combine_scores
        self.assertEqual(combine(90, 100, 0.15), 92)   # 91.5 → round 92
        self.assertEqual(combine(100, 0, 0.3), 70)
        self.assertEqual(combine(80, 80, 0.15), 80)
        self.assertEqual(combine(100, 0, 0.0), 100)
        # 权重上限 0.5
        self.assertEqual(combine(100, 0, 0.9), 50)

    def test_duration_ratio_allowed(self):
        allowed = video_similarity.duration_ratio_allowed
        self.assertTrue(allowed(100, 160))    # 短视频放宽 1.6
        self.assertTrue(allowed(60, 100))     # 0.6
        self.assertTrue(allowed(120, 180))
        self.assertTrue(allowed(160, 100))
        self.assertFalse(allowed(100, 40))    # 0.4 < 0.5
        self.assertFalse(allowed(200, 320))   # 长视频仍 0.7..1.4
        self.assertFalse(allowed(0, 100))
        self.assertTrue(allowed(200, 260))    # 1.3 长视频允许

    def test_short_video_grouping(self):
        frame = 0xAAAAAAAAAAAAAAAA
        def item(duration):
            return {"analysis": {"duration": duration,
                                 "frame_hashes": [frame, frame, frame]}}
        short = video_similarity.build_video_groups(
            [item(100), item(160)], 90)
        self.assertEqual(len(short), 1)
        long = video_similarity.build_video_groups(
            [item(200), item(320)], 90)
        self.assertEqual(long, [])

    def test_asr_weight_changes_final_score(self):
        frame = 0xAAAAAAAAAAAAAAAA
        same_text = "we will meet at nine tomorrow morning in the office"
        other_text = "completely different words about cooking pasta tonight"
        sig_same_a = signature_for(same_text)
        sig_same_b = signature_for(same_text)
        sig_other = signature_for(other_text)

        def item(signature):
            return {"analysis": {"duration": 120,
                                 "frame_hashes": [frame] * 5,
                                 "asr_signature": signature}}
        aligned = video_similarity.build_video_groups(
            [item(sig_same_a), item(sig_same_b)], 90, asr_weight=0.15)
        self.assertEqual(len(aligned), 1)
        self.assertTrue(aligned[0]["details"][(0, 1)]["asr_used"])
        self.assertEqual(aligned[0]["details"][(0, 1)]["text_score"], 100)
        # 视觉相同但文本完全不同：weight 0.3 时 final≈70 < 90 → 不成组
        mismatched = video_similarity.build_video_groups(
            [item(sig_same_a), item(sig_other)], 90, asr_weight=0.3)
        self.assertEqual(mismatched, [])
        # 同 pair 关掉权重则仍按视觉成组
        visual_only = video_similarity.build_video_groups(
            [item(sig_same_a), item(sig_other)], 90, asr_weight=0.0)
        self.assertEqual(len(visual_only), 1)
        self.assertFalse(visual_only[0]["details"][(0, 1)]["asr_used"])


class AsrEngineTest(AsrTestBase):
    def test_asr_success_and_privacy(self):
        video = make_video(os.path.join(self.root, "a.mp4"), seed=7)
        ffmpeg_log = os.path.join(self.harness.tmpdir, "ffmpeg.log")
        options = self._asr_options(video_asr_max_sec=120)
        engine = self._engine()
        with mock.patch.dict(os.environ, {
                "FAKE_ASR_LOG": self.asr_log,
                "FAKE_FFMPEG_LOG": ffmpeg_log,
                "FAKE_ASR_TEXT": "UNIQUE_TRANSCRIPT_XYZ spoken words here"}):
            analysis = engine.analyze(video, options)
        self.assertEqual(analysis["asr_status"], "ok")
        self.assertEqual(len(analysis["asr_signature"]), 64)
        self.assertNotIn("text", analysis)
        self.assertNotIn("transcript", analysis)
        self.assertNotIn("UNIQUE_TRANSCRIPT_XYZ", json.dumps(analysis))
        self.assertEqual(engine.asr_used, 1)
        log_lines = self._asr_log_lines()
        self.assertEqual(len(log_lines), 1)
        self.assertTrue(log_lines[0]["audio_exists"])
        with open(ffmpeg_log, "r", encoding="utf-8") as handle:
            ffmpeg_lines = [line for line in handle.read().splitlines() if line]
        audio_lines = [line for line in ffmpeg_lines if "wav" in line]
        self.assertTrue(audio_lines)
        self.assertIn("-t 120", audio_lines[-1])
        self.assertIn("-ar 16000", audio_lines[-1])
        self.assertIn("-ac 1", audio_lines[-1])
        self.assertIn("-vn", audio_lines[-1])

    def test_asr_stdout_text(self):
        video = make_video(os.path.join(self.root, "b.mp4"), seed=3)
        engine = self._engine()
        with mock.patch.dict(os.environ, {
                "FAKE_ASR_LOG": self.asr_log,
                "FAKE_ASR_MODE": "stdout",
                "FAKE_ASR_TEXT": "spoken through stdout for signature"}):
            analysis = engine.analyze(video, self._asr_options())
        self.assertEqual(analysis["asr_status"], "ok")
        self.assertEqual(len(analysis["asr_signature"]), 64)

    def test_asr_unavailable_fail_open(self):
        video = make_video(os.path.join(self.root, "c.mp4"), seed=3)
        engine = self._engine()
        no_command = engine.analyze(
            video, self._asr_options(video_asr_command=""))
        self.assertEqual(no_command["asr_status"], "no_command")
        self.assertIsNone(no_command["asr_signature"])
        missing = engine.analyze(
            video, self._asr_options(video_asr_command="/no/such/asr"))
        self.assertEqual(missing["asr_status"], "command_not_found")
        disabled = engine.analyze(
            video, self._asr_options(video_asr_enabled=False))
        self.assertEqual(disabled["asr_status"], "disabled")

    def test_asr_failure_and_no_speech(self):
        video = make_video(os.path.join(self.root, "d.mp4"), seed=3)
        other = make_video(os.path.join(self.root, "d2.mp4"), seed=4)
        engine = self._engine()
        with mock.patch.dict(os.environ, {
                "FAKE_ASR_LOG": self.asr_log,
                "FAKE_ASR_MODE": "fail"}):
            failed = engine.analyze(video, self._asr_options())
        self.assertEqual(failed["asr_status"], "failed")
        self.assertIsNone(failed["asr_signature"])
        # 失败标记缓存会阻止同一文件重复解码，因此用另一个文件验证 no_speech
        with mock.patch.dict(os.environ, {
                "FAKE_ASR_LOG": self.asr_log,
                "FAKE_ASR_MODE": "empty"}):
            empty = engine.analyze(other, self._asr_options())
        self.assertEqual(empty["asr_status"], "no_speech")
        self.assertIsNone(empty["asr_signature"])

    def test_asr_audio_extract_failure(self):
        video = make_video(os.path.join(self.root, "e.mp4"), seed=3)
        engine = self._engine()
        with mock.patch.dict(os.environ, {"FAKE_AUDIO_FAIL": "1"}):
            analysis = engine.analyze(video, self._asr_options())
        self.assertEqual(analysis["asr_status"], "extract_failed")
        self.assertIsNone(analysis["asr_signature"])

    def test_asr_timeout_fail_open(self):
        video = make_video(os.path.join(self.root, "f.mp4"), seed=3)
        engine = self._engine(asr_timeout_cap=0.4)
        started = time.time()
        with mock.patch.dict(os.environ, {
                "FAKE_ASR_LOG": self.asr_log,
                "FAKE_ASR_SLEEP": "2"}):
            analysis = engine.analyze(video, self._asr_options())
        self.assertLess(time.time() - started, 2.0)
        self.assertEqual(analysis["asr_status"], "timeout")

    def test_asr_cache_avoids_redecode(self):
        video = make_video(os.path.join(self.root, "g.mp4"), seed=3)
        options = self._asr_options()
        engine = self._engine()
        with mock.patch.dict(os.environ, {
                "FAKE_ASR_LOG": self.asr_log,
                "FAKE_ASR_MODE": "ok"}):
            first = engine.analyze(video, options)
            second = engine.analyze(video, options)
        self.assertEqual(first["asr_status"], "ok")
        self.assertEqual(second["asr_status"], "ok")
        self.assertFalse(first["asr_cache_hit"])
        self.assertTrue(second["asr_cache_hit"])
        self.assertEqual(len(self._asr_log_lines()), 1)
        # 文件身份变化后缓存失效
        with open(video, "ab") as handle:
            handle.write(b"\nchanged")
        with mock.patch.dict(os.environ, {"FAKE_ASR_LOG": self.asr_log}):
            third = engine.analyze(video, options)
        self.assertFalse(third["asr_cache_hit"])
        self.assertEqual(len(self._asr_log_lines()), 2)


class AsrScanTest(AsrTestBase):
    def _two_videos(self, duration_a=120, duration_b=121, seed=7):
        first = make_video(os.path.join(self.root, "s1.mp4"), seed=seed,
                           duration=duration_a)
        second = make_video(os.path.join(self.root, "s2.mp4"), seed=seed,
                            duration=duration_b)
        self.harness.set_creation(first, 1000)
        self.harness.set_creation(second, 2000)
        return first, second

    def test_scan_asr_group_fields_and_privacy(self):
        self._two_videos()
        transcript = "UNIQUE_TRANSCRIPT_XYZ same speech for both clips"
        with mock.patch.dict(os.environ, {
                "FAKE_ASR_LOG": self.asr_log,
                "FAKE_ASR_MODE": "ok",
                "FAKE_ASR_TEXT": transcript}):
            scan_id, meta = self.harness.run_scan(
                options=self._asr_options(), timeout=120)
        self.assertEqual(meta["status"], "completed")
        self.assertEqual(meta["stats"]["video_groups"], 1)
        self.assertGreaterEqual(meta["stats"]["video_asr_used"], 2)
        groups = self.harness.groups(scan_id)
        video_group = [item for item in groups
                       if item["match_type"] == "video"][0]
        self.assertTrue(video_group["video_asr_used"])
        self.assertGreaterEqual(video_group["video_text_score"], 0)
        for file_info in video_group["files"]:
            self.assertIn("video_asr_used", file_info)
            self.assertIn("video_text_score", file_info)
            self.assertNotIn("asr_signature", file_info)
            self.assertNotIn("transcript", file_info)
        serialized = json.dumps({"meta": meta, "groups": groups},
                                ensure_ascii=False)
        self.assertNotIn("UNIQUE_TRANSCRIPT_XYZ", serialized)
        self.assertNotIn("asr_signature", serialized)

    def test_scan_asr_failure_falls_back_to_visual(self):
        self._two_videos()
        with mock.patch.dict(os.environ, {
                "FAKE_ASR_LOG": self.asr_log,
                "FAKE_ASR_MODE": "fail"}):
            scan_id, meta = self.harness.run_scan(
                options=self._asr_options(), timeout=120)
        self.assertEqual(meta["status"], "completed")
        self.assertEqual(meta["stats"]["video_groups"], 1)
        self.assertGreaterEqual(meta["stats"]["video_asr_skipped"]["failed"], 2)
        self.assertEqual(meta["stats"]["video_asr_used"], 0)

    def test_scan_short_video_ratio_widened(self):
        # 100s vs 160s：比例 1.6，长视频规则不允许；短视频规则允许
        self._two_videos(duration_a=100, duration_b=160)
        _scan_id, meta = self.harness.run_scan(
            options=self._video_options(), timeout=120)
        self.assertEqual(meta["stats"]["video_groups"], 1)

    def test_scan_long_video_ratio_still_strict(self):
        self._two_videos(duration_a=240, duration_b=384)  # 1.6
        _scan_id, meta = self.harness.run_scan(
            options=self._video_options(), timeout=120)
        self.assertEqual(meta["stats"]["video_groups"], 0)
