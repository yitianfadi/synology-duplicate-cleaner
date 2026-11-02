# -*- coding: utf-8 -*-
"""v0018 plan D：视频内容快速哈希删除安全 + FFmpeg 批量抽帧测试。

覆盖：content_quick_hash 稳定性；扫描结果 file_info.content_quick_hash；
计划 content_hash_verified（正常/缺失历史哈希）；内容变化但 size/mtime/inode
不变时计划与删除阶段均 HASH_MISMATCH；批量抽帧顺序/失败回退/进程数下降。
"""

from __future__ import absolute_import

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.helpers import Harness, write_bytes, write_text
from tests.test_video import FAKE_FFMPEG, FakeFFmpegBase, make_video

from dupcleaner import config, video_similarity


ORDERED_BATCH_FAKE = r'''
import sys
args = sys.argv[1:]
outputs = []
for index, token in enumerate(args):
    if token == "-y" and index + 1 < len(args):
        outputs.append(args[index + 1])
frame_bytes = 1024
if outputs:
    for position, out_path in enumerate(outputs):
        with open(out_path, "wb") as handle:
            handle.write(bytes([position % 256]) * frame_bytes)
    sys.exit(0)
sys.stderr.write("no outputs\n")
sys.exit(1)
'''

STDOUT_BATCH_FAKE = r'''
import sys
args = sys.argv[1:]
count = args.count("-frames:v")
if count <= 0:
    count = 1
sys.stdout.buffer.write(bytes([7]) * (1024 * count))
sys.stdout.buffer.flush()
sys.exit(0)
'''

FAIL_BATCH_FAKE = r'''
import sys
sys.stderr.write("fake batch failure\n")
sys.exit(1)
'''


def _video_options(**overrides):
    options = {
        "video_similarity_enabled": True,
        "video_sample_frames": 5,
        "video_similarity_threshold": 90,
        "video_concurrency": 1,
        "video_hwaccel": "off",
        "video_timeout_sec": 60,
        "video_max_file_mb": 64,
        "video_cache": False,
    }
    options.update(overrides)
    return options


def _overwrite_keep_identity(path, replacement_byte):
    """同大小改写文件内容并恢复 mtime_ns（inode/size/mtime 不变）。"""
    st = os.stat(path)
    with open(path, "rb") as handle:
        raw = bytearray(handle.read())
    if not raw:
        raw = bytearray(b"x")
    raw[0] = replacement_byte
    with open(path, "wb") as handle:
        handle.write(bytes(raw))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


class ContentQuickHashTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-quick-hash-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_helper_deterministic_and_content_sensitive(self):
        directory = self.tmpdir
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "video.bin")
        write_bytes(path, b"HEAD" + b"A" * 4096 + b"TAIL")
        first = video_similarity.content_quick_hash(path)
        second = video_similarity.content_quick_hash(path)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        _overwrite_keep_identity(path, ord("Z"))
        changed = video_similarity.content_quick_hash(path)
        self.assertNotEqual(first, changed)

    def test_engine_returns_content_quick_hash_and_cache_hit(self):
        harness = Harness()
        try:
            root = harness.dev_root
            fake_path = os.path.join(harness.tmpdir, "fake_ffmpeg.py")
            write_text(fake_path, FAKE_FFMPEG)
            cmd = [sys.executable, fake_path]
            engine = video_similarity.VideoEngine(
                harness.config, ffmpeg_cmd=cmd)
            video = make_video(os.path.join(root, "hash.mp4"), seed=3)
            options = _video_options(video_sample_frames=5,
                                     video_cache=True)
            first = engine.analyze(video, options)
            expected = video_similarity.content_quick_hash(video)
            self.assertEqual(first["content_quick_hash"], expected)
            second = engine.analyze(video, options)
            self.assertTrue(second["cache_hit"])
            self.assertEqual(second["content_quick_hash"], expected)
        finally:
            harness.cleanup()


class BatchExtractionTest(FakeFFmpegBase):
    def _tool_with_script(self, source, name):
        path = os.path.join(self.harness.tmpdir, name)
        write_text(path, source)
        return video_similarity.FFmpegTool(cmd=[sys.executable, path])

    def test_extract_frames_batch_order_preserved(self):
        video = make_video(os.path.join(self.root, "batch.mp4"), seed=1)
        tool = self._tool_with_script(ORDERED_BATCH_FAKE, "ordered_fake.py")
        frames = tool.extract_frames_batch(video, [1.0, 2.5, 4.0], timeout=30)
        self.assertEqual(len(frames), 3)
        self.assertEqual([frame[0] for frame in frames], [0, 1, 2])
        for frame in frames:
            self.assertEqual(len(frame), config.VIDEO_FRAME_BYTES)

    def test_extract_frames_batch_stdout_fallback(self):
        video = make_video(os.path.join(self.root, "stdout.mp4"), seed=1)
        tool = self._tool_with_script(STDOUT_BATCH_FAKE, "stdout_fake.py")
        frames = tool.extract_frames_batch(video, [1.0, 2.0, 3.0], timeout=30)
        self.assertEqual(len(frames), 3)
        self.assertTrue(all(frame[0] == 7 for frame in frames))

    def test_extract_frames_batch_error_raises_video_skip(self):
        video = make_video(os.path.join(self.root, "fail.mp4"), seed=1)
        tool = self._tool_with_script(FAIL_BATCH_FAKE, "fail_fake.py")
        with self.assertRaises(video_similarity.VideoSkip) as ctx:
            tool.extract_frames_batch(video, [1.0, 2.0], timeout=30)
        self.assertEqual(ctx.exception.reason, "decode")

    def test_engine_uses_batch_processes(self):
        video = make_video(os.path.join(self.root, "procs.mp4"), seed=4)
        log_path = os.path.join(self.harness.tmpdir, "batch.log")
        engine = self._engine()
        options = _video_options(video_sample_frames=8)
        with mock.patch.dict(os.environ, {"FAKE_FFMPEG_LOG": log_path}):
            analysis = engine.analyze(video, options)
        self.assertEqual(len(analysis["frame_hashes"]), analysis["n_eff"])
        self.assertEqual(analysis["n_eff"], 8)
        with open(log_path, "r", encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines()
                     if "-f rawvideo" in line]
        # 8 帧按 4 帧/进程 → 2 个 ffmpeg 进程（远低于逐帧 8 个）
        self.assertEqual(len(lines), 2)
        self.assertEqual(sum(line.count("-y ") for line in lines), 8)

    def test_engine_batch_failure_falls_back_per_frame(self):
        video = make_video(os.path.join(self.root, "fallback.mp4"), seed=6)
        engine = self._engine()
        options = _video_options(video_sample_frames=8)
        ok_log = os.path.join(self.harness.tmpdir, "ok.log")
        with mock.patch.dict(os.environ, {"FAKE_FFMPEG_LOG": ok_log}):
            ok = engine.analyze(video, options)
        fail_log = os.path.join(self.harness.tmpdir, "fail.log")
        with mock.patch.dict(os.environ, {
                "FAKE_FFMPEG_LOG": fail_log,
                "FAKE_FFMPEG_BATCH_FAIL": "1"}):
            fallback = engine.analyze(video, options)
        self.assertEqual(len(fallback["frame_hashes"]),
                         fallback["n_eff"])
        self.assertEqual(fallback["frame_hashes"], ok["frame_hashes"])
        with open(ok_log, "r", encoding="utf-8") as handle:
            ok_lines = [line for line in handle.read().splitlines()
                        if "-f rawvideo" in line]
        with open(fail_log, "r", encoding="utf-8") as handle:
            fail_lines = [line for line in handle.read().splitlines()
                          if "-f rawvideo" in line]
        self.assertEqual(len(ok_lines), 2)
        # 批处理失败 2 次 + 逐帧回退 8 次
        self.assertEqual(len(fail_lines), 10)


class VideoDeletionSafetyTest(FakeFFmpegBase):
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

    def test_scan_stores_quick_hash_and_plan_verified(self):
        scan_id, group, keeper, non_keeper = self._scan_pair()
        for item in group["files"]:
            expected = video_similarity.content_quick_hash(item["path"])
            self.assertEqual(item["content_quick_hash"], expected)
        plan = self.harness.services.deletion.create_plan(
            self.session, scan_id, {"scope": "video_similar", "recycle": True})
        self.assertEqual(plan["count"], 1)
        self.assertTrue(plan["content_hash_verified"])
        self.assertEqual(plan["content_hash_missing_videos"], 0)
        self.assertEqual(plan["files"][0]["match_type"], "video")

    def test_plan_rejects_content_change_with_same_identity(self):
        scan_id, group, keeper, non_keeper = self._scan_pair()
        _overwrite_keep_identity(non_keeper["path"], ord("Z"))
        deletion = self.harness.services.deletion
        with self.assertRaises(Exception) as ctx:
            deletion.create_plan(self.session, scan_id,
                                 {"scope": "video_similar", "recycle": True})
        self.assertEqual(ctx.exception.code, "PLAN_INVALID")
        reasons = [item["reason"] for item in ctx.exception.details["skipped"]]
        self.assertIn("HASH_MISMATCH", reasons)

    def test_missing_historical_hash_falls_back_with_warning(self):
        scan_id, group, keeper, non_keeper = self._scan_pair()
        groups = self.harness.services.store.iter_scan_groups(scan_id)
        updated = []
        for item in groups:
            item = dict(item)
            if item.get("match_type") == "video":
                item["files"] = [dict(file_info) for file_info in item["files"]]
                for file_info in item["files"]:
                    file_info.pop("content_quick_hash", None)
            updated.append(item)
        self.harness.services.store.write_scan_groups(scan_id, updated)
        deletion = self.harness.services.deletion
        plan = deletion.create_plan(self.session, scan_id,
                                    {"scope": "video_similar", "recycle": True})
        self.assertEqual(plan["count"], 1)
        self.assertFalse(plan["content_hash_verified"])
        self.assertGreaterEqual(plan["content_hash_missing_videos"], 1)
        job_id = deletion.start_delete(self.session, scan_id, {
            "plan_id": plan["plan_id"], "confirm": "DELETE"})
        job = self._wait_job(scan_id, job_id)
        self.assertIn(job["status"], ("completed", "partial"))
        self.assertFalse(os.path.exists(non_keeper["path"]))
        self.assertTrue(os.path.exists(keeper["path"]))

    def test_delete_rechecks_quick_hash_after_plan(self):
        scan_id, group, keeper, non_keeper = self._scan_pair()
        deletion = self.harness.services.deletion
        plan = deletion.create_plan(self.session, scan_id,
                                    {"scope": "video_similar", "recycle": True})
        _overwrite_keep_identity(non_keeper["path"], ord("Q"))
        job_id = deletion.start_delete(self.session, scan_id, {
            "plan_id": plan["plan_id"], "confirm": "DELETE"})
        job = self._wait_job(scan_id, job_id)
        self.assertEqual(job["status"], "partial")
        reasons = [item.get("reason") for item in job["results"]]
        self.assertIn("HASH_MISMATCH", reasons)
        self.assertTrue(os.path.exists(non_keeper["path"]))
        self.assertTrue(os.path.exists(keeper["path"]))

    def test_exact_plan_content_hash_verified_true(self):
        first = write_text(os.path.join(self.root, "e1.txt"), "exact " * 30)
        second = write_text(os.path.join(self.root, "e2.txt"), "exact " * 30)
        self.harness.set_creation(first, 1000)
        self.harness.set_creation(second, 2000)
        scan_id, _meta = self.harness.run_scan()
        deletion = self.harness.services.deletion
        plan = deletion.create_plan(self.session, scan_id, {
            "scope": "all_duplicates", "recycle": True})
        self.assertEqual(plan["count"], 1)
        self.assertTrue(plan["content_hash_verified"])
        self.assertEqual(plan["content_hash_missing_videos"], 0)


if __name__ == "__main__":
    unittest.main()
