# -*- coding: utf-8 -*-
"""v0047：相似视频识别增强（中心裁剪指纹 / 时间轴偏移 / 音频签名 / 置信度）。"""

import os
import sys
import tempfile
import unittest

from tests.helpers import Harness, write_bytes
from tests.test_video import FAKE_FFMPEG, make_video, write_text

from dupcleaner import config, settings as settings_module
from dupcleaner import video_similarity


def gray_frame(pattern):
    """32×32 灰度帧；pattern 为 "bars" / "center" 等。"""
    size = config.VIDEO_FRAME_SIZE
    frame = bytearray(size * size)
    for y in range(size):
        for x in range(size):
            if pattern == "center":
                value = 255 if 8 <= x < 24 and 8 <= y < 24 else 0
            elif pattern == "checker":
                value = 255 if ((x // 8) + (y // 8)) % 2 == 0 else 0
            else:
                value = (x * 7 + y * 3) % 256
            frame[y * size + x] = value
    return bytes(frame)


def letterboxed(frame):
    """把中心 24×24 内容放进 32×32 黑边（模拟上下黑边）。"""
    size = config.VIDEO_FRAME_SIZE
    out = bytearray(size * size)
    for y in range(size):
        for x in range(size):
            if 4 <= x < 28 and 4 <= y < 28:
                out[y * size + x] = frame[(y - 4) * size + (x - 4)]
    return bytes(out)


def frame_hash_sequence(seed, count=8):
    base = gray_frame("bars" if seed % 2 == 0 else "checker")
    return [video_similarity.phash_from_gray(
        bytes((value + seed + index) % 256 for value in base))
        for index in range(count)]


class CenterCropTest(unittest.TestCase):
    def test_center_crop_removes_black_bars(self):
        content = gray_frame("center")
        padded = letterboxed(content)
        full_padded = video_similarity.phash_from_gray(padded)
        full_plain = video_similarity.phash_from_gray(content)
        self.assertLess(
            video_similarity.frame_similarity(full_padded, full_plain), 0.99)
        cropped_padded = video_similarity.phash_from_gray(
            video_similarity.gray_content_crop(padded))
        cropped_plain = video_similarity.phash_from_gray(
            video_similarity.gray_content_crop(content))
        self.assertAlmostEqual(
            video_similarity.frame_similarity(cropped_padded, cropped_plain),
            1.0, places=2)

    def test_content_crop_falls_back_on_blank(self):
        blank = bytes(config.VIDEO_FRAME_BYTES)
        cropped = video_similarity.gray_content_crop(blank)
        self.assertEqual(len(cropped), config.VIDEO_FRAME_BYTES)

    def test_center_crop_ratio_bounds(self):
        gray = gray_frame("bars")
        for ratio in (0.1, 1.0):
            cropped = video_similarity.gray_center_crop(gray, ratio)
            self.assertEqual(len(cropped), config.VIDEO_FRAME_BYTES)

    def test_phash_pair(self):
        full, content = video_similarity.phash_from_gray_pair(
            letterboxed(gray_frame("bars")))
        self.assertIsInstance(full, int)
        self.assertIsInstance(content, int)
        self.assertNotEqual(full, content)

    def test_bad_frame_size(self):
        with self.assertRaises(ValueError):
            video_similarity.gray_center_crop(b"short")


class OffsetAlignTest(unittest.TestCase):
    def test_shifted_sequence_aligned(self):
        frames = frame_hash_sequence(0, 10)
        shifted = [frames[0]] + frames[:-1]
        avg, overlap, shift = video_similarity.offset_aligned_stats(
            frames, shifted)
        self.assertGreaterEqual(avg, 0.9)
        self.assertGreaterEqual(overlap, 0.8)
        self.assertNotEqual(shift, 0)

    def test_unrelated_sequence_low(self):
        first = frame_hash_sequence(0, 10)
        second = frame_hash_sequence(1, 10)
        avg, overlap, _shift = video_similarity.offset_aligned_stats(
            first, second)
        self.assertLess(avg, 0.8)

    def test_empty_sequence(self):
        self.assertEqual(video_similarity.offset_aligned_stats([], [])[0], 0.0)


class EnhancedPairScoreTest(unittest.TestCase):
    def test_center_variant_rescues_letterbox(self):
        content = gray_frame("center")
        plain = video_similarity.phash_from_gray(content)
        padded = video_similarity.phash_from_gray(letterboxed(content))
        full_a = [plain] * 8
        full_b = [padded] * 8
        center_a = [video_similarity.phash_from_gray(
            video_similarity.gray_center_crop(letterboxed(content)))] * 8
        center_b = [video_similarity.phash_from_gray(
            video_similarity.gray_center_crop(content))] * 8
        legacy = video_similarity.pair_score(full_a, full_b, 0)
        enhanced = video_similarity.pair_score_enhanced(
            full_a, center_a, full_b, center_b, 0)
        self.assertIsNotNone(enhanced)
        self.assertGreater(enhanced[0], legacy[0] if legacy else 0)
        self.assertTrue(enhanced[4])
        self.assertIn(enhanced[3], ("dtw", "offset"))

    def test_identical_sequences(self):
        frames = frame_hash_sequence(0, 8)
        result = video_similarity.pair_score_enhanced(
            frames, frames, frames, frames, 0)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result[0], 95)

    def test_oviously_different(self):
        first = frame_hash_sequence(0, 8)
        second = frame_hash_sequence(1, 8)
        self.assertIsNone(video_similarity.pair_score_enhanced(
            first, first, second, second, 90))

    def test_confidence_bands(self):
        self.assertEqual(video_similarity.confidence_band(100)[0], "same_source")
        self.assertEqual(video_similarity.confidence_band(95)[0], "same_source")
        self.assertEqual(video_similarity.confidence_band(90)[0], "likely_same")
        self.assertEqual(video_similarity.confidence_band(80)[0], "possible")
        key, label = video_similarity.confidence_band(96)
        self.assertIn(u"同源", label)


class AudioSignatureTest(unittest.TestCase):
    @staticmethod
    def pcm_wave(frequency, seconds=2.0, rate=8000, amplitude=12000):
        import math
        import struct
        count = int(seconds * rate)
        data = bytearray()
        for index in range(count):
            value = int(amplitude * math.sin(
                2.0 * math.pi * frequency * index / rate))
            data += struct.pack("<h", value)
        return bytes(data)

    def test_signature_shape(self):
        signature = video_similarity.audio_signature_from_pcm(
            self.pcm_wave(440), 32)
        self.assertEqual(len(signature), 32)
        self.assertTrue(all(0 <= value <= 255 for value in signature))

    def test_same_wave_high_similarity(self):
        first = video_similarity.audio_signature_from_pcm(
            self.pcm_wave(440), 64)
        second = video_similarity.audio_signature_from_pcm(
            self.pcm_wave(440), 64)
        self.assertGreaterEqual(
            video_similarity.audio_signature_similarity(first, second), 95)

    def test_different_wave_lower_similarity(self):
        import math
        import struct
        first_pcm = bytearray()
        second_pcm = bytearray()
        rate = 8000
        for index in range(rate * 2):
            envelope = 1.0 if (index // 2000) % 2 == 0 else 0.05
            first_pcm += struct.pack(
                "<h", int(12000 * envelope * math.sin(
                    2 * math.pi * 440 * index / rate)))
            # 第二段用渐变包络（有方差、但包络形状不同）
            ramp = 0.1 + 0.9 * (index / float(rate * 2))
            second_pcm += struct.pack(
                "<h", int(12000 * ramp * math.sin(
                    2 * math.pi * 880 * index / rate)))
        first = video_similarity.audio_signature_from_pcm(bytes(first_pcm), 64)
        second = video_similarity.audio_signature_from_pcm(bytes(second_pcm), 64)
        similarity = video_similarity.audio_signature_similarity(first, second)
        self.assertIsNotNone(similarity)
        self.assertLess(similarity, 95)

    def test_silence_returns_none(self):
        silence = b"\x00\x00" * 8000
        signature = video_similarity.audio_signature_from_pcm(silence, 32)
        self.assertEqual(video_similarity.audio_signature_similarity(
            signature, signature), None)

    def test_short_pcm_empty(self):
        self.assertEqual(
            video_similarity.audio_signature_from_pcm(b"\x00\x00", 32), [])


class BuildGroupsV2Test(unittest.TestCase):
    def _item(self, frames, center, duration=120.0, audio=None):
        analysis = {
            "duration": duration,
            "frame_hashes": frames,
            "frame_hashes_center": center,
            "fingerprint": "v2:1:x",
        }
        if audio is not None:
            analysis["audio_signature"] = audio
        return {"analysis": analysis, "path": "x"}

    def test_groups_with_center_and_audio(self):
        content = gray_frame("center")
        plain = video_similarity.phash_from_gray(content)
        padded = video_similarity.phash_from_gray(letterboxed(content))
        center_hash = video_similarity.phash_from_gray(
            video_similarity.gray_center_crop(content))
        audio_a = [100] * 60 + [20] * 60
        audio_b = [98] * 60 + [22] * 60
        items = [
            self._item([plain] * 8, [center_hash] * 8, audio=audio_a),
            self._item([padded] * 8, [center_hash] * 8, audio=audio_b),
        ]
        clusters = video_similarity.build_video_groups(
            items, 85, audio_weight=0.2)
        self.assertEqual(len(clusters), 1)
        cluster = clusters[0]
        self.assertGreaterEqual(cluster["min_score"], 85)
        self.assertIn(cluster["confidence"], ("same_source", "likely_same"))
        self.assertTrue(cluster["confidence_label"])
        self.assertTrue(cluster["audio_used"])
        detail = list(cluster["details"].values())[0]
        self.assertTrue(detail["audio_used"])
        self.assertGreaterEqual(int(detail["audio_score"] or 0), 90)

    def test_without_audio_weight_no_audio(self):
        frames = frame_hash_sequence(0, 8)
        items = [self._item(frames, frames, audio=[1] * 60),
                 self._item(frames, frames, audio=[1] * 60)]
        clusters = video_similarity.build_video_groups(items, 90, audio_weight=0)
        self.assertFalse(clusters[0]["audio_used"])


class AudioSignatureEngineTest(unittest.TestCase):
    class FakeAudioFFmpeg(object):
        def __init__(self, pcm=None, fail=False, rate=8000):
            self.pcm = pcm
            self.fail = fail
            self.rate = rate
            self.calls = 0

        def available(self):
            return True

        def extract_audio_pcm(self, path, seconds, rate, timeout,
                              stop_check=None):
            self.calls += 1
            if self.fail:
                raise video_similarity.VideoSkip("audio", u"无音轨")
            return self.pcm

    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root
        self.video = os.path.join(self.root, "clip.mp4")
        write_bytes(self.video, b"fake video")

    def tearDown(self):
        self.harness.cleanup()

    def _engine(self, ffmpeg, cache=None):
        return video_similarity.VideoEngine(
            self.harness.config, ffmpeg=ffmpeg, cache=cache)

    def test_signature_ok_and_cached(self):
        import math
        import struct
        pcm = bytearray()
        for index in range(8000):
            pcm += struct.pack("<h", int(
                10000 * math.sin(2 * math.pi * 440 * index / 8000)))
        ffmpeg = self.FakeAudioFFmpeg(pcm=bytes(pcm))
        cache_path = os.path.join(self.harness.tmpdir, "video-cache.sqlite")
        cache = video_similarity.VideoCache(cache_path)
        try:
            engine = self._engine(ffmpeg, cache)
            identity = video_similarity.fsutil_identity(self.video)
            options = {"video_audio_signature_enabled": True}
            result = engine._analyze_audio_signature(
                self.video, identity, options, cache)
            self.assertEqual(result["audio_signature_status"], "ok")
            self.assertGreaterEqual(len(result["audio_signature"]), 8)
            self.assertEqual(ffmpeg.calls, 1)
            again = engine._analyze_audio_signature(
                self.video, identity, options, cache)
            self.assertEqual(again["audio_signature_status"], "ok")
            self.assertTrue(again["audio_cache_hit"])
            self.assertEqual(ffmpeg.calls, 1)
        finally:
            cache.close()

    def test_signature_disabled(self):
        ffmpeg = self.FakeAudioFFmpeg(pcm=b"")
        engine = self._engine(ffmpeg)
        identity = video_similarity.fsutil_identity(self.video)
        result = engine._analyze_audio_signature(
            self.video, identity, {}, None)
        self.assertEqual(result["audio_signature_status"], "disabled")
        self.assertEqual(ffmpeg.calls, 0)

    def test_signature_failure_is_open(self):
        ffmpeg = self.FakeAudioFFmpeg(fail=True)
        engine = self._engine(ffmpeg)
        identity = video_similarity.fsutil_identity(self.video)
        result = engine._analyze_audio_signature(
            self.video, identity, {"video_audio_signature_enabled": True}, None)
        self.assertEqual(result["audio_signature_status"], "failed")
        self.assertIsNone(result["audio_signature"])


class VideoSettingsV2Test(unittest.TestCase):
    def test_defaults(self):
        defaults = settings_module.default_settings()
        self.assertTrue(defaults["video_center_crop_enabled"])
        self.assertTrue(defaults["video_offset_align_enabled"])
        self.assertFalse(defaults["video_audio_signature_enabled"])
        self.assertAlmostEqual(defaults["video_audio_weight"], 0.2)

    def test_validation(self):
        patch = settings_module.validate_patch({
            "video_center_crop_enabled": False,
            "video_offset_align_enabled": False,
            "video_audio_signature_enabled": True,
            "video_audio_weight": 0.35,
        })
        self.assertFalse(patch["video_center_crop_enabled"])
        self.assertFalse(patch["video_offset_align_enabled"])
        self.assertTrue(patch["video_audio_signature_enabled"])
        self.assertAlmostEqual(patch["video_audio_weight"], 0.35)
        for invalid in ({"video_audio_weight": 0.6},
                        {"video_audio_weight": -0.1},
                        {"video_center_crop_enabled": "yes"}):
            with self.assertRaises(Exception):
                settings_module.validate_patch(invalid)


class VideoScanFieldsTest(unittest.TestCase):
    """扫描结果里的视频组带置信度/方法/音频字段（fake ffmpeg）。"""

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

    def test_group_confidence_fields(self):
        first = make_video(os.path.join(self.root, "v1.mp4"), seed=5,
                           duration=120)
        second = make_video(os.path.join(self.root, "sub", "v2.mp4"), seed=5,
                            duration=121)
        self.harness.set_creation(first, 1000)
        self.harness.set_creation(second, 2000)
        options = self.harness.services.settings.get()
        options.update({
            "video_similarity_enabled": True,
            "video_sample_frames": 5,
            "video_similarity_threshold": 90,
            "video_concurrency": 1,
            "video_audio_signature_enabled": False,
        })
        scan_id, meta = self.harness.run_scan(options=options, timeout=120)
        self.assertEqual(meta["status"], "completed")
        groups = self.harness.groups(scan_id)
        video_groups = [item for item in groups
                        if item["match_type"] == "video"]
        self.assertEqual(len(video_groups), 1)
        group = video_groups[0]
        self.assertIn(group["video_confidence"],
                      ("same_source", "likely_same", "possible"))
        self.assertTrue(group["video_confidence_label"])
        self.assertGreaterEqual(group["video_max_score"], 90)
        self.assertGreaterEqual(group["video_min_score"], 90)
        self.assertIn(group["video_match_method"], ("dtw", "offset"))
        self.assertFalse(group["video_audio_used"])
        self.assertEqual(group["video_audio_score"], 0)
        self.assertGreaterEqual(group["video_duration_delta_sec"], 1)
        for file_info in group["files"]:
            self.assertIn(file_info["video_audio_status"],
                          ("disabled", "unavailable", "failed", "ok"))
            self.assertIn(file_info["video_match_method"],
                          ("dtw", "offset", "keeper"))


if __name__ == "__main__":
    unittest.main()
