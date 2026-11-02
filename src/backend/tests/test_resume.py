# -*- coding: utf-8 -*-
"""v0014 暂停断点 / 升级后继续 / 升级 drain 测试（docs/RESUME_CONTRACT.md）。

覆盖：暂停写 records 快照、同进程继续、进程重启后从 DB 断点继续并跳过已完成
hash、size/hash 阶段恢复、文件变化使记录失效、drain 空闲立即 done、
drain 暂停活动扫描并写 done、drain 超时不写 done、paused 断点在启动时保留。
"""

from __future__ import absolute_import

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from tests.helpers import BACKEND_DIR, Harness, write_bytes  # noqa: F401

from dupcleaner import config as app_config
from dupcleaner import fsutil, hashing
from dupcleaner import scanner as scanner_module
from dupcleaner.errors import ApiError
from dupcleaner.services import build_services
from dupcleaner.store import Store
from dupcleaner.upgrade import UpgradeDrainMonitor


def wait_until(predicate, timeout=20.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def wait_scan_status(scanner, scan_id, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        meta = scanner.get_scan(scan_id)
        if meta.get("status") in ("completed", "failed", "stopped", "interrupted"):
            return meta
        time.sleep(0.02)
    raise AssertionError("scan %s did not finish in %.1fs" % (scan_id, timeout))


class _FakeScanner(object):
    def __init__(self, result=False):
        self.result = result
        self.calls = 0

    def pause_for_upgrade(self, timeout=0):
        self.calls += 1
        return self.result


class ResumeTestBase(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.scanner = self.harness.services.scanner
        self.store = self.harness.services.store

    def tearDown(self):
        self.harness.cleanup()

    # -- 工具 -----------------------------------------------------------
    def make_scan_files(self, count=3, size_kb=700, prefix="big"):
        root = self.harness.dev_root
        content = os.urandom(size_kb * 1024)
        for index in range(count):
            write_bytes(os.path.join(root, "%s%d.txt" % (prefix, index)),
                        content)
        return root

    def start_slow_scan(self, options=None):
        root = self.make_scan_files()
        merged = {"concurrency": 1, "hash_rate_mbps": 1}
        if options:
            merged.update(options)
        return self.scanner.start_scan([root], [], merged, "admin")

    def wait_stage(self, scan_id, stages=("sample", "hash"), timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            meta = self.scanner.get_scan(scan_id)
            if meta.get("status") in ("completed", "failed", "stopped"):
                return meta
            if meta.get("stage") in stages:
                return meta
            time.sleep(0.02)
        raise AssertionError("stage %s not reached" % (stages,))

    def wait_paused_snapshot(self, scan_id, timeout=30.0):
        self.assertTrue(wait_until(
            lambda: self.scanner.get_scan(scan_id).get("status") == "paused"
            and self.store.load_resumable_checkpoint(scan_id) is not None,
            timeout), "paused resume checkpoint not written")
        with self.scanner._lock:
            state = self.scanner._states.get(scan_id)
        if state is not None and state.thread is not None:
            self.assertTrue(wait_until(
                lambda: not state.thread.is_alive(), timeout),
                "pause thread did not exit")
        return self.store.load_resumable_checkpoint(scan_id)

    def stop_paused_scan(self, scan_id):
        self.scanner.stop(scan_id)
        return wait_scan_status(self.scanner, scan_id, timeout=20)

    def new_services_same_data_dir(self):
        restart_config = app_config.RuntimeConfig(
            data_dir=self.harness.data_dir,
            web_dir=self.harness.web_dir,
            dev_mode=True,
            dev_roots=[self.harness.dev_root],
            dev_password="devpass",
            environ={})
        return build_services(restart_config)


class PauseSnapshotTest(ResumeTestBase):
    def test_pause_writes_checkpoint_with_records(self):
        scan_id = self.start_slow_scan()
        self.wait_stage(scan_id, ("sample", "hash"))
        paused = self.scanner.pause(scan_id)
        self.assertEqual(paused["status"], "paused")
        payload = self.wait_paused_snapshot(scan_id)
        self.assertIn(payload.get("stage"),
                      ("size", "sample", "hash"))
        records = payload.get("records")
        self.assertIsInstance(records, list)
        self.assertGreaterEqual(len(records), 1)
        for field in ("path", "size", "mtime_ns", "inode", "dev", "nlink",
                      "quick_hash", "hash"):
            self.assertIn(field, records[0])
        self.assertIsInstance(payload.get("stats"), dict)
        meta = self.scanner.get_scan(scan_id)
        self.assertEqual(meta["status"], "paused")
        self.assertTrue(meta["resumable"])
        self.assertEqual(meta["pause_reason"], "user")
        final = self.stop_paused_scan(scan_id)
        self.assertEqual(final["status"], "stopped")

    def test_paused_scan_blocks_new_scan(self):
        scan_id = self.start_slow_scan()
        self.wait_stage(scan_id, ("sample", "hash"))
        self.scanner.pause(scan_id)
        self.wait_paused_snapshot(scan_id)
        with self.assertRaises(ApiError) as ctx:
            self.scanner.start_scan([self.harness.dev_root], [], {}, "admin")
        self.assertEqual(ctx.exception.code, "SCAN_BUSY")
        final = self.stop_paused_scan(scan_id)
        self.assertEqual(final["status"], "stopped")

    def test_pause_then_stop_converges_to_stopped(self):
        scan_id = self.start_slow_scan()
        self.wait_stage(scan_id, ("sample", "hash"))
        self.scanner.pause(scan_id)
        self.wait_paused_snapshot(scan_id)
        stopped = self.scanner.stop(scan_id)
        self.assertIn(stopped["status"], ("stopping", "stopped"))
        final = wait_scan_status(self.scanner, scan_id, timeout=20)
        self.assertEqual(final["status"], "stopped")
        self.assertIsNone(self.store.load_resumable_checkpoint(scan_id))

    def test_resume_same_process_completes(self):
        scan_id = self.start_slow_scan()
        self.wait_stage(scan_id, ("sample", "hash"))
        self.scanner.pause(scan_id)
        self.wait_paused_snapshot(scan_id)
        resumed = self.scanner.resume(scan_id)
        self.assertEqual(resumed["status"], "running")
        final = wait_scan_status(self.scanner, scan_id, timeout=90)
        self.assertEqual(final["status"], "completed")
        self.assertGreaterEqual(final["stats"]["groups"], 1)
        self.assertIsNone(self.store.load_resumable_checkpoint(scan_id))


class ResumeRejectionTest(ResumeTestBase):
    def test_resume_interrupted_scan_is_rejected(self):
        scan_id = "20261102-210000-ab12cd"
        self.store.save_scan_meta(scan_id, {
            "scan_id": scan_id, "status": "interrupted",
            "created_epoch": time.time(), "config": {"roots": []},
            "options": {}, "user": "admin", "stats": {},
            "resumable": False,
        })
        with self.assertRaises(ApiError) as ctx:
            self.scanner.resume(scan_id)
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_resume_paused_without_checkpoint_is_rejected(self):
        scan_id = "20261102-210000-ab12cd"
        self.store.save_scan_meta(scan_id, {
            "scan_id": scan_id, "status": "paused",
            "created_epoch": time.time(), "config": {"roots": []},
            "options": {}, "user": "admin", "stats": {},
            "resumable": False,
        })
        with self.assertRaises(ApiError) as ctx:
            self.scanner.resume(scan_id)
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")


    def test_stop_persisted_paused_scan_discards_checkpoint(self):
        scan_id = "20261102-210000-ab12cd"
        self.store.save_scan_meta(scan_id, {
            "scan_id": scan_id, "status": "paused", "stage": "size",
            "created_epoch": time.time(), "pause_reason": "upgrade",
            "config": {"roots": []}, "options": {}, "user": "admin",
            "stats": {}, "resumable": True,
        })
        self.store.save_checkpoint(scan_id, "size", {
            "stage": "size", "progress": 0.18, "stats": {},
            "records": [], "paused_epoch": time.time(),
        })
        stopped = self.scanner.stop(scan_id)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(self.scanner.get_scan(scan_id)["status"], "stopped")
        self.assertIsNone(self.store.load_checkpoint(scan_id))
        with self.assertRaises(ApiError) as ctx:
            self.scanner.resume(scan_id)
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")


class RestartResumeTest(ResumeTestBase):
    def test_resume_after_restart_skips_completed_hashes(self):
        scan_id = self.start_slow_scan()
        self.wait_stage(scan_id, ("hash",))
        # 等至少一个文件哈希完成，保证断点里有部分 hash
        self.assertTrue(wait_until(
            lambda: int(self.scanner.get_scan(scan_id).get("stats", {}).get(
                "bytes_hashed") or 0) > 0, 20.0), "no hash progress before pause")
        self.scanner.pause(scan_id)
        payload = self.wait_paused_snapshot(scan_id)
        prehashed = sum(1 for item in payload.get("records") or []
                        if item.get("hash"))
        candidates = sum(1 for item in payload.get("records") or []
                         if item.get("quick_hash"))
        self.assertGreaterEqual(prehashed, 1)
        self.assertGreater(candidates, 0)

        services2 = self.new_services_same_data_dir()
        try:
            meta = services2.scanner.get_scan(scan_id)
            self.assertEqual(meta["status"], "paused")
            self.assertTrue(meta["resumable"])

            real_hash = hashing.full_hash_file
            real_quick = hashing.quick_sample_hash
            calls = {"hash": 0, "quick": 0}

            def counted_hash(*args, **kwargs):
                calls["hash"] += 1
                return real_hash(*args, **kwargs)

            def counted_quick(*args, **kwargs):
                calls["quick"] += 1
                return real_quick(*args, **kwargs)

            with mock.patch.object(hashing, "full_hash_file",
                                   side_effect=counted_hash), \
                    mock.patch.object(hashing, "quick_sample_hash",
                                      side_effect=counted_quick):
                resumed = services2.scanner.resume(scan_id)
                self.assertEqual(resumed["status"], "running")
                final = wait_scan_status(services2.scanner, scan_id, timeout=90)
            self.assertEqual(final["status"], "completed")
            self.assertGreaterEqual(final["stats"]["groups"], 1)
            # 断点恢复不再重算快速指纹，且只补算未完成的 hash
            self.assertEqual(calls["quick"], 0)
            self.assertLess(calls["hash"], candidates)
        finally:
            services2.shutdown(timeout=2)

    def test_startup_keeps_paused_scan_with_resumable_checkpoint(self):
        root = self.make_scan_files(count=2, size_kb=4)
        records = []
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            identity = fsutil.file_identity(path)
            records.append({
                "path": path,
                "size": identity["size"],
                "mtime_ns": identity["mtime_ns"],
                "inode": identity["inode"],
                "dev": identity["dev"],
                "nlink": identity["nlink"],
                "quick_hash": None,
                "hash": None,
            })
        scan_id = scanner_module.make_scan_id()
        self.store.save_scan_meta(scan_id, {
            "scan_id": scan_id, "status": "paused", "stage": "size",
            "created_epoch": time.time(), "pause_reason": "upgrade",
            "config": {"roots": [root], "excludes": [],
                       "keep_mode": "latest", "hash_algo": "sha256"},
            "options": {"keep_mode": "latest", "hash_algo": "sha256"},
            "user": "admin", "stats": {},
        })
        self.store.save_checkpoint(scan_id, "size", {
            "stage": "size", "progress": 0.18, "stats": {},
            "records": records, "paused_epoch": time.time(),
        })
        services2 = self.new_services_same_data_dir()
        try:
            meta = services2.scanner.get_scan(scan_id)
            self.assertEqual(meta["status"], "paused")
            self.assertTrue(meta["resumable"])
            self.assertEqual(meta["pause_reason"], "upgrade")
        finally:
            services2.shutdown(timeout=2)


class StageResumeTest(ResumeTestBase):
    def _seed_paused_scan(self, paths, stage, records):
        scan_id = scanner_module.make_scan_id()
        root = self.harness.dev_root
        self.store.save_scan_meta(scan_id, {
            "scan_id": scan_id, "status": "paused", "stage": stage,
            "created_epoch": time.time() - 5, "progress": 0.2,
            "config": {"roots": [root], "excludes": [],
                       "keep_mode": "latest", "hash_algo": "sha256"},
            "options": {"keep_mode": "latest", "hash_algo": "sha256",
                        "concurrency": 1},
            "user": "admin", "stats": {},
        })
        self.store.save_checkpoint(scan_id, stage, {
            "stage": stage, "progress": 0.2, "stats": {},
            "records": records, "paused_epoch": time.time(),
        })
        return scan_id

    @staticmethod
    def _record(path, quick_hash=None, digest=None):
        identity = fsutil.file_identity(path)
        return {
            "path": path, "size": identity["size"],
            "mtime_ns": identity["mtime_ns"], "inode": identity["inode"],
            "dev": identity["dev"], "nlink": identity["nlink"],
            "quick_hash": quick_hash, "hash": digest,
        }

    def test_resume_from_size_stage(self):
        root = self.harness.dev_root
        content = b"size-stage-" + b"x" * 4096
        paths = [write_bytes(os.path.join(root, "size%d.txt" % index), content)
                 for index in range(3)]
        records = [self._record(path) for path in paths]
        scan_id = self._seed_paused_scan(paths, "size", records)
        resumed = self.scanner.resume(scan_id)
        self.assertEqual(resumed["status"], "running")
        final = wait_scan_status(self.scanner, scan_id, timeout=60)
        self.assertEqual(final["status"], "completed")
        groups = list(self.store.iter_scan_groups(scan_id))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 3)

    def test_resume_from_hash_stage_skips_hashing(self):
        root = self.harness.dev_root
        content = b"hash-stage-" + b"y" * 8192
        paths = [write_bytes(os.path.join(root, "hash%d.txt" % index), content)
                 for index in range(3)]
        quick = hashing.quick_sample_hash(paths[0])
        digest, _bytes = hashing.full_hash_file(paths[0])
        records = [self._record(path, quick_hash=quick, digest=digest)
                   for path in paths]
        scan_id = self._seed_paused_scan(paths, "hash", records)
        calls = {"hash": 0, "quick": 0}
        real_hash = hashing.full_hash_file
        real_quick = hashing.quick_sample_hash

        def counted_hash(*args, **kwargs):
            calls["hash"] += 1
            return real_hash(*args, **kwargs)

        def counted_quick(*args, **kwargs):
            calls["quick"] += 1
            return real_quick(*args, **kwargs)

        with mock.patch.object(hashing, "full_hash_file",
                               side_effect=counted_hash), \
                mock.patch.object(hashing, "quick_sample_hash",
                                  side_effect=counted_quick):
            self.scanner.resume(scan_id)
            final = wait_scan_status(self.scanner, scan_id, timeout=60)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(calls["hash"], 0)
        self.assertEqual(calls["quick"], 0)
        groups = list(self.store.iter_scan_groups(scan_id))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 3)

    def test_changed_file_invalidates_old_hash(self):
        root = self.harness.dev_root
        content = b"changed-file-" + b"z" * 4096
        first = write_bytes(os.path.join(root, "chg_a.txt"), content)
        second = write_bytes(os.path.join(root, "chg_b.txt"), content)
        quick = hashing.quick_sample_hash(first)
        digest, _bytes = hashing.full_hash_file(first)
        records = [self._record(first, quick_hash=quick, digest=digest),
                   self._record(second, quick_hash=quick, digest=digest)]
        scan_id = self._seed_paused_scan([first, second], "hash", records)
        # 暂停后修改其中一个文件（同大小，内容/mtime 变化）
        time.sleep(0.05)
        with open(first, "r+b") as handle:
            handle.write(b"Z" * 16)
        os.utime(first, None)
        self.scanner.resume(scan_id)
        final = wait_scan_status(self.scanner, scan_id, timeout=60)
        self.assertEqual(final["status"], "completed")
        self.assertGreaterEqual(
            int(final["stats"].get("resume_invalidated") or 0), 1)
        # 绝不沿用旧 hash：变化文件被丢弃后只剩 1 个记录，不构成重复组
        self.assertEqual(list(self.store.iter_scan_groups(scan_id)), [])


class DrainTest(ResumeTestBase):
    def test_drain_no_active_scan_writes_done(self):
        monitor = self.harness.services.upgrade_monitor
        self.assertIsNotNone(monitor)
        with open(monitor.drain_path, "w", encoding="utf-8") as handle:
            handle.write("drain")
        self.assertTrue(wait_until(
            lambda: monitor.done_written
            and os.path.isfile(monitor.done_path), 10.0),
            "upgrade.done not written")
        self.assertFalse(os.path.exists(monitor.drain_path))

    def test_drain_pauses_active_scan_and_writes_done(self):
        scan_id = self.start_slow_scan()
        self.wait_stage(scan_id, ("sample", "hash"))
        monitor = self.harness.services.upgrade_monitor
        with open(monitor.drain_path, "w", encoding="utf-8") as handle:
            handle.write("drain")
        self.assertTrue(wait_until(
            lambda: os.path.isfile(monitor.done_path), 30.0),
            "upgrade.done not written")
        self.assertTrue(wait_until(
            lambda: self.scanner.get_scan(scan_id).get("status") == "paused",
            10.0))
        payload = self.store.load_resumable_checkpoint(scan_id)
        self.assertIsNotNone(payload)
        self.assertGreaterEqual(len(payload.get("records") or []), 1)
        self.assertEqual(
            self.scanner.get_scan(scan_id).get("pause_reason"), "upgrade")
        # 升级后手动继续，避免测试残留未完成扫描
        self.scanner.resume(scan_id)
        final = wait_scan_status(self.scanner, scan_id, timeout=90)
        self.assertEqual(final["status"], "completed")

    def test_drain_timeout_does_not_write_done(self):
        tmpdir = tempfile.mkdtemp(prefix="dc-drain-")
        try:
            fake = _FakeScanner(result=False)
            monitor = UpgradeDrainMonitor(tmpdir, fake, timeout=0.2,
                                          poll_interval=0.02)
            monitor.start()
            try:
                with open(monitor.drain_path, "w", encoding="utf-8") as handle:
                    handle.write("drain")
                self.assertTrue(wait_until(
                    lambda: monitor.timed_out
                    and not os.path.exists(monitor.drain_path), 5.0))
                self.assertFalse(os.path.exists(monitor.done_path))
                self.assertEqual(monitor.last_result, "timeout")
            finally:
                monitor.stop(timeout=2)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_drain_timeout_leaves_scan_interrupted_on_restart(self):
        tmpdir = tempfile.mkdtemp(prefix="dc-drain-")
        store = None
        monitor = None
        try:
            store = Store(tmpdir)
            scan_id = "20261102-210000-ab12cd"
            store.save_scan_meta(scan_id, {
                "scan_id": scan_id, "status": "running", "stage": "hash",
                "created_epoch": time.time(), "config": {"roots": ["/x"]},
                "options": {}, "user": "admin", "stats": {},
            })
            monitor = UpgradeDrainMonitor(tmpdir, _FakeScanner(result=False),
                                          timeout=0.1, poll_interval=0.02)
            monitor.start()
            with open(monitor.drain_path, "w", encoding="utf-8") as handle:
                handle.write("drain")
            self.assertTrue(wait_until(lambda: monitor.timed_out, 5.0))
            self.assertFalse(os.path.exists(monitor.done_path))
            monitor.stop(timeout=2)
            store.mark_interrupted_scans()
            meta = store.load_scan_meta(scan_id)
            self.assertEqual(meta["status"], "interrupted")
            self.assertFalse(meta["resumable"])
        finally:
            if monitor is not None:
                monitor.stop(timeout=2)
            if store is not None:
                store.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_monitor_start_cleans_stale_handshake_files(self):
        tmpdir = tempfile.mkdtemp(prefix="dc-drain-")
        try:
            drain = os.path.join(tmpdir, "upgrade.drain")
            done = os.path.join(tmpdir, "upgrade.done")
            for path in (drain, done):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("stale")
            monitor = UpgradeDrainMonitor(tmpdir, _FakeScanner(result=True),
                                          timeout=0.2, poll_interval=0.02)
            monitor.start()
            try:
                self.assertFalse(os.path.exists(drain))
                self.assertFalse(os.path.exists(done))
            finally:
                monitor.stop(timeout=2)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class ResumableCheckpointValidationTest(ResumeTestBase):
    def test_legacy_or_incomplete_checkpoint_is_not_resumable(self):
        scan_id = "20261102-210000-ab12cd"
        self.store.save_checkpoint(scan_id, "hash", {"files": 10})
        self.assertIsNone(self.store.load_resumable_checkpoint(scan_id))
        self.store.save_checkpoint(scan_id, "bogus", {
            "stage": "bogus", "records": []})
        self.assertIsNone(self.store.load_resumable_checkpoint(scan_id))
        self.store.save_checkpoint(scan_id, "hash", {
            "stage": "hash", "stats": {}, "records": []})
        self.assertIsNotNone(self.store.load_resumable_checkpoint(scan_id))


if __name__ == "__main__":
    unittest.main()
