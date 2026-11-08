# -*- coding: utf-8 -*-
"""v0043：增量扫描（跨扫描哈希缓存）/ 保留规则链 / 排除规则 / 影视附属文件。

覆盖 docs/INCREMENTAL_SCAN_CONTRACT.md 的验收点：
- hash_cache / media_probe_cache 的存储读写与清理
- 第二次扫描复用哈希（不重复读盘），new/changed/same 判定
- 保留规则链（体积/路径/文件名/字幕/音轨）与平局转人工
- 文件名正则/通配符/.ignore/附属文件过滤
- 结果页“只看本次新增/变化”（new_only）与 /api/cache 接口
"""

import os
import unittest

from tests.helpers import Harness, WebServer, write_bytes, write_text

from dupcleaner import config, scanner as scanner_module
from dupcleaner import settings as settings_module
from dupcleaner.errors import ApiError


def _dup_pair(root, prefix, data):
    a = write_bytes(os.path.join(root, prefix + "-a.bin"), data)
    b = write_bytes(os.path.join(root, "sub", prefix + "-b.bin"), data)
    return a, b


class HashCacheStoreTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.store = self.harness.services.store

    def tearDown(self):
        self.harness.cleanup()

    def test_hash_cache_roundtrip(self):
        rows = [{
            "dev": 1, "inode": 100, "path": "/volume1/a.bin", "size": 2048,
            "mtime_ns": 1700000000123456789, "quick_hash": "q" * 64,
            "full_hash": "f" * 64, "algo": "sha256", "sample_kb": 64,
            "sample_version": config.QUICK_SAMPLE_VERSION,
            "first_scan_id": "20260101-000000-aaaaaa",
            "last_scan_id": "20260101-000000-aaaaaa",
        }, {
            "dev": 1, "inode": 101, "path": "/volume1/b.bin", "size": 4096,
            "mtime_ns": 1700000001123456789, "quick_hash": "qq" * 32,
            "full_hash": "ff" * 32, "algo": "sha256", "sample_kb": 64,
            "sample_version": config.QUICK_SAMPLE_VERSION,
        }]
        self.assertEqual(self.store.hash_cache_put_many(rows), 2)
        loaded = self.store.hash_cache_get([(1, 100), (1, 101), (1, 999)])
        self.assertEqual(set(loaded.keys()), {(1, 100), (1, 101)})
        self.assertEqual(loaded[(1, 100)]["full_hash"], "f" * 64)
        self.assertEqual(loaded[(1, 100)]["sample_version"],
                         config.QUICK_SAMPLE_VERSION)
        self.assertEqual(loaded[(1, 100)]["size"], 2048)
        stats = self.store.hash_cache_stats()
        self.assertEqual(stats["entries"], 2)
        self.assertEqual(stats["bytes"], 2048 + 4096)
        # 覆盖写（同一 dev+inode）
        rows[0]["full_hash"] = "g" * 64
        self.store.hash_cache_put_many(rows[:1])
        loaded = self.store.hash_cache_get([(1, 100)])
        self.assertEqual(loaded[(1, 100)]["full_hash"], "g" * 64)
        self.assertEqual(self.store.hash_cache_stats()["entries"], 2)

    def test_hash_cache_large_inode_does_not_overflow(self):
        # Windows 的 st_ino 是 64 位无符号，可能 > 2^63-1；缓存键必须能存。
        big_inode = 0xFFFFFFFFFFFFFFF0
        rows = [{
            "dev": 3, "inode": big_inode, "path": "/volume1/x.bin",
            "size": 10, "mtime_ns": 1, "quick_hash": "q" * 8,
            "full_hash": "f" * 8, "algo": "sha256", "sample_kb": 64,
            "sample_version": config.QUICK_SAMPLE_VERSION,
        }]
        self.assertEqual(self.store.hash_cache_put_many(rows), 1)
        loaded = self.store.hash_cache_get([(3, big_inode)])
        self.assertEqual(len(loaded), 1)
        self.assertEqual(list(loaded.values())[0]["size"], 10)

    def test_hash_cache_touch_prune_clear(self):
        rows = [{
            "dev": 1, "inode": index, "path": "/v/%d.bin" % index,
            "size": 1, "mtime_ns": 1, "quick_hash": "q", "full_hash": "f",
            "algo": "sha256", "sample_kb": 64,
            "sample_version": config.QUICK_SAMPLE_VERSION,
        } for index in range(1, 6)]
        self.store.hash_cache_put_many(rows)
        self.assertEqual(self.store.hash_cache_touch([(1, 1)]), 1)
        self.store.hash_cache_prune(2)
        self.assertEqual(self.store.hash_cache_stats()["entries"], 2)
        self.store.hash_cache_clear()
        self.assertEqual(self.store.hash_cache_stats()["entries"], 0)

    def test_media_probe_cache_roundtrip(self):
        items = [{
            "dev": 2, "inode": 300, "size": 1024, "mtime_ns": 77,
            "payload": {"audio_stream_count": 2,
                        "subtitle_stream_count": 1,
                        "audio_langs": ["jpn", "chi"]},
        }]
        self.assertEqual(self.store.media_probe_cache_put_many(items), 1)
        loaded = self.store.media_probe_cache_get([(2, 300), (2, 301)])
        self.assertEqual(set(loaded.keys()), {(2, 300)})
        row = loaded[(2, 300)]
        self.assertEqual(row["size"], 1024)
        self.assertEqual(row["mtime_ns"], 77)
        self.assertEqual(row["payload"]["audio_stream_count"], 2)
        self.assertEqual(self.store.media_probe_cache_count(), 1)
        self.store.media_probe_cache_clear()
        self.assertEqual(self.store.media_probe_cache_count(), 0)


class IncrementalScanTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root
        # 两组重复（同大小同内容），外加一个尺寸唯一的文件
        self.a1, self.a2 = _dup_pair(self.root, "p1", b"P1" * 512)
        self.b1, self.b2 = _dup_pair(self.root, "p2", b"P2" * 512)
        self.unique = write_bytes(os.path.join(self.root, "unique.bin"),
                                  b"U" * 300)

    def tearDown(self):
        self.harness.cleanup()

    def test_second_scan_reuses_hashes(self):
        scan1, meta1 = self.harness.run_scan()
        self.assertEqual(meta1["status"], "completed")
        stats1 = meta1["stats"]
        self.assertEqual(stats1["hash_cache_hits"], 0)
        self.assertGreater(stats1["bytes_hashed"], 0)
        self.assertGreaterEqual(stats1["new_files"], 4)  # 4 个候选文件
        self.assertEqual(stats1["changed_files"], 0)

        scan2, meta2 = self.harness.run_scan()
        stats2 = meta2["stats"]
        self.assertGreater(stats2["hash_cache_hits"], 0)
        # 内容未变：不再读盘算哈希
        self.assertEqual(stats2["bytes_hashed"], 0)
        self.assertGreater(stats2["bytes_hashed_reused"], 0)
        self.assertEqual(stats2["new_files"], 0)
        self.assertEqual(stats2["changed_files"], 0)
        self.assertEqual(stats2["unchanged_files"], stats1["candidate_files"])
        # 两次扫描的重复组一致
        self.assertEqual(stats1["exact_groups"], stats2["exact_groups"])
        self.assertEqual(
            sorted(scan_record["path"] for scan_record in
                   [info for group in self.harness.groups(scan1)
                    for info in group["files"]]),
            sorted(scan_record["path"] for scan_record in
                   [info for group in self.harness.groups(scan2)
                    for info in group["files"]]))

    def test_new_and_changed_files_are_flagged(self):
        self.harness.run_scan()
        # 新增一个与 p1 组同大小同内容的文件 → 下次扫描应标“新增”
        added = write_bytes(os.path.join(self.root, "p1-c.bin"), b"P1" * 512)
        scan2, meta2 = self.harness.run_scan()
        stats2 = meta2["stats"]
        self.assertGreaterEqual(stats2["new_files"], 1)
        group = self.harness.group_by_paths(scan2, "p1-c.bin")
        added_info = [info for info in group["files"]
                      if info["name"] == "p1-c.bin"][0]
        self.assertTrue(added_info["new_since_last_scan"])
        self.assertFalse(added_info["changed_since_last_scan"])
        self.assertGreaterEqual(group["new_files"], 1)

        # 原地改写（同大小、不同内容）→ 下次扫描应标“变化”
        write_bytes(added, b"P9" * 512)
        scan3, meta3 = self.harness.run_scan()
        self.assertGreaterEqual(meta3["stats"]["changed_files"], 1)

    def test_incremental_disabled_recomputes(self):
        self.harness.run_scan()
        _scan_id, meta = self.harness.run_scan(
            options={"incremental_scan": False})
        self.assertEqual(meta["stats"]["hash_cache_hits"], 0)
        self.assertGreater(meta["stats"]["bytes_hashed"], 0)

    def test_new_only_filter(self):
        self.harness.run_scan()
        write_bytes(os.path.join(self.root, "p1-c.bin"), b"P1" * 512)
        scan2, _meta = self.harness.run_scan()
        page = self.harness.services.scanner.groups_page(
            scan2, offset=0, limit=100, new_only=True)
        self.assertGreaterEqual(page["total"], 1)
        self.assertGreaterEqual(page["new_total"], 1)
        for group in page["groups"]:
            self.assertTrue(int(group.get("new_files") or 0) > 0
                            or int(group.get("changed_files") or 0) > 0)
        # 不带 new_only 时组数更多（另一组没有新增/变化）
        all_page = self.harness.services.scanner.groups_page(
            scan2, offset=0, limit=100)
        self.assertGreater(all_page["total"], page["total"])


class KeepRuleChainTest(unittest.TestCase):
    @staticmethod
    def _info(path, size, created=1000, mtime_ns=1000,
              audio=0, subtitle=0):
        return {
            "path": path, "name": os.path.basename(path), "size": size,
            "created_ts": created, "created_ns": 0, "created_source": "statx",
            "created_unsafe": False, "mtime_ns": mtime_ns,
            "audio_stream_count": audio, "subtitle_stream_count": subtitle,
        }

    def test_size_largest(self):
        files = [self._info("/v/a.bin", 100), self._info("/v/b.bin", 300),
                 self._info("/v/c.bin", 200)]
        keeper, reason, manual, _suspect, fallback, source = (
            scanner_module.choose_keeper_rules(files, {"keep_rules": ["size_largest"]}))
        self.assertEqual(files[keeper]["name"], "b.bin")
        self.assertEqual(reason, "size_largest")
        self.assertFalse(manual)
        self.assertFalse(fallback)
        self.assertEqual(source, "rule")

    def test_path_priority_then_size(self):
        files = [self._info("/volume1/video/keep/a.bin", 100),
                 self._info("/volume1/video/other/b.bin", 300)]
        keeper, reason, manual, _s, _f, _src = (
            scanner_module.choose_keeper_rules(
                files, {"keep_rules": ["path_priority:/volume1/video/keep",
                                       "size_largest"]}))
        self.assertEqual(files[keeper]["name"], "a.bin")
        self.assertEqual(reason, "path_priority")
        self.assertFalse(manual)

    def test_filename_priority_glob_and_regex(self):
        files = [self._info("/v/movie-1080p.mkv", 100),
                 self._info("/v/movie-2160p.mkv", 100)]
        keeper, reason, _m, _s, _f, _src = (
            scanner_module.choose_keeper_rules(
                files, {"keep_rules": ["filename_priority:*2160p*"]}))
        self.assertEqual(files[keeper]["name"], "movie-2160p.mkv")
        self.assertEqual(reason, "filename_priority")
        files2 = [self._info("/v/movie-国语.mkv", 100),
                  self._info("/v/movie-英语.mkv", 100)]
        keeper2, reason2, _m2, _s2, _f2, _src2 = (
            scanner_module.choose_keeper_rules(
                files2, {"keep_rules": ["filename_priority:/\u56fd\u8bed/"]}))
        self.assertEqual(files2[keeper2]["name"], "movie-国语.mkv")
        self.assertEqual(reason2, "filename_priority")

    def test_chain_tie_breaks_to_next_rule(self):
        files = [self._info("/v/a.bin", 100, created=1000),
                 self._info("/v/b.bin", 100, created=900)]
        keeper, reason, manual, _s, _f, _src = (
            scanner_module.choose_keeper_rules(
                files, {"keep_rules": ["size_largest", "created_earliest"]}))
        self.assertEqual(files[keeper]["name"], "b.bin")
        self.assertEqual(reason, "earliest_created")
        self.assertFalse(manual)

    def test_all_tie_requires_manual(self):
        files = [self._info("/v/a.bin", 100, created=1000),
                 self._info("/v/b.bin", 100, created=1000)]
        _k, reason, manual, _s, _f, _src = (
            scanner_module.choose_keeper_rules(
                files, {"keep_rules": ["size_largest", "created_latest"]}))
        self.assertTrue(manual)
        self.assertEqual(reason, "tie")

    def test_media_rules(self):
        files = [self._info("/v/a.mkv", 100, audio=1, subtitle=0),
                 self._info("/v/b.mkv", 100, audio=2, subtitle=1)]
        keeper, reason, _m, _s, _f, _src = (
            scanner_module.choose_keeper_rules(
                files, {"keep_rules": ["has_subtitle_stream"]}))
        self.assertEqual(files[keeper]["name"], "b.mkv")
        self.assertEqual(reason, "has_subtitle_stream")
        keeper2, reason2, _m2, _s2, _f2, _src2 = (
            scanner_module.choose_keeper_rules(
                files, {"keep_rules": ["has_audio_streams"]}))
        self.assertEqual(files[keeper2]["name"], "b.mkv")
        self.assertEqual(reason2, "has_audio_streams")

    def test_empty_rules_falls_back_to_legacy(self):
        files = [self._info("/v/a.bin", 100, created=1000),
                 self._info("/v/b.bin", 100, created=2000)]
        # keep_rules 为空 → 返回 None，由 choose_keeper 走历史语义
        self.assertIsNone(scanner_module.choose_keeper_rules(
            files, {"keep_rules": []}))

    def test_keep_rules_in_scan(self):
        harness = Harness()
        try:
            root = harness.dev_root
            # 精准重复组内所有文件大小必然相同 → 体积规则必然平局，
            # 由下一条规则（创建时间）决定保留项（规则链示例）。
            first = write_bytes(os.path.join(root, "first.bin"), b"L" * 400)
            second = write_bytes(os.path.join(root, "sub", "second.bin"),
                                 b"L" * 400)
            harness.set_creation(first, 1000)
            harness.set_creation(second, 500)
            scan_id, meta = harness.run_scan(options={
                "keep_rules": ["size_largest", "created_latest"]})
            self.assertEqual(meta["status"], "completed")
            group = harness.group_by_paths(scan_id, "first.bin")
            keepers = [info for info in group["files"] if info["is_keep"]]
            self.assertEqual(len(keepers), 1)
            self.assertEqual(keepers[0]["name"], "first.bin")
            self.assertEqual(group["keep_reason"], "latest_created")
            self.assertEqual(group["keep_time_source"], "btime")
            self.assertEqual(meta["stats"]["keep_rule_hits"].get("latest_created"), 1)
        finally:
            harness.cleanup()


class ExcludeRulesScanTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root

    def tearDown(self):
        self.harness.cleanup()

    def test_regex_glob_and_ignore_file(self):
        write_bytes(os.path.join(self.root, "a.txt"), b"A" * 100)
        write_bytes(os.path.join(self.root, "sub", "b.txt"), b"A" * 100)
        write_bytes(os.path.join(self.root, "sample.tmp"), b"sample")
        write_bytes(os.path.join(self.root, "foo.log"), b"log")
        write_bytes(os.path.join(self.root, "build", "c.txt"), b"build")
        write_text(os.path.join(self.root, ".ignore"),
                   u"# comment\nbuild/\n*.log\n")
        scan_id, meta = self.harness.run_scan(options={
            "exclude_name_regex": [r"^sample\."],
            "exclude_name_glob": [],
        })
        self.assertEqual(meta["status"], "completed")
        self.assertEqual(meta["stats"]["files_scanned"], 2)
        self.assertGreaterEqual(meta["stats"]["skipped"]["rule"], 2)
        names = sorted(info["name"] for group in self.harness.groups(scan_id)
                       for info in group["files"])
        self.assertEqual(names, ["a.txt", "b.txt"])

    def test_ignore_can_be_disabled(self):
        write_bytes(os.path.join(self.root, "a.txt"), b"A" * 100)
        write_bytes(os.path.join(self.root, "sub", "b.txt"), b"A" * 100)
        write_bytes(os.path.join(self.root, "drop.tmp"), b"drop")
        write_text(os.path.join(self.root, ".ignore"), u"*.tmp\n")
        _scan_id, meta = self.harness.run_scan(options={
            "ignore_file_enabled": False})
        self.assertEqual(meta["stats"]["files_scanned"], 3)

    def test_media_sidecar_filter(self):
        write_bytes(os.path.join(self.root, "movie", "movie.mkv"),
                    b"M" * 1000)
        write_bytes(os.path.join(self.root, "movie", "movie.srt"),
                    b"subtitle")
        write_bytes(os.path.join(self.root, "movie", "movie-poster.jpg"),
                    b"poster")
        write_bytes(os.path.join(self.root, "photo.jpg"), b"photo")
        _scan_id, meta = self.harness.run_scan(options={
            "media_sidecar_mode": "skip"})
        self.assertEqual(meta["stats"]["files_scanned"], 2)
        self.assertEqual(meta["stats"]["sidecar_files"], 2)
        self.assertEqual(meta["stats"]["skipped"]["sidecar"], 2)

    def test_media_sidecar_include_mode(self):
        write_bytes(os.path.join(self.root, "movie", "movie.mkv"),
                    b"M" * 1000)
        write_bytes(os.path.join(self.root, "movie", "movie.srt"), b"sub")
        _scan_id, meta = self.harness.run_scan(options={
            "media_sidecar_mode": "include"})
        self.assertEqual(meta["stats"]["files_scanned"], 2)
        self.assertEqual(meta["stats"]["sidecar_files"], 0)


class SettingsValidationTest(unittest.TestCase):
    def test_keep_rules_validation(self):
        merged = settings_module.merge_settings(
            {}, {"keep_rules": ["size_largest",
                                "path_priority:/volume1/video/keep",
                                "filename_priority:*2160p*"]})
        self.assertEqual(merged["keep_rules"][0], "size_largest")
        self.assertTrue(merged["keep_rules"][1].startswith("path_priority:"))
        with self.assertRaises(ApiError) as ctx:
            settings_module.validate_patch({"keep_rules": ["unknown_rule"]})
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(ApiError):
            settings_module.validate_patch({"keep_rules": ["path_priority"]})
        with self.assertRaises(ApiError):
            settings_module.validate_patch(
                {"keep_rules": ["filename_priority:/[/"]})
        with self.assertRaises(ApiError):
            settings_module.validate_patch({"keep_rules": ["size_largest:arg"]})

    def test_exclude_pattern_validation(self):
        merged = settings_module.merge_settings(
            {}, {"exclude_name_regex": [r"^sample\."],
                 "exclude_name_glob": ["*.tmp"],
                 "media_sidecar_mode": "include"})
        self.assertEqual(merged["exclude_name_regex"], [r"^sample\."])
        self.assertEqual(merged["exclude_name_glob"], ["*.tmp"])
        self.assertEqual(merged["media_sidecar_mode"], "include")
        with self.assertRaises(ApiError):
            settings_module.validate_patch({"exclude_name_regex": ["["]})
        with self.assertRaises(ApiError):
            settings_module.validate_patch({"media_sidecar_mode": "drop"})
        with self.assertRaises(ApiError):
            settings_module.validate_patch(
                {"exclude_name_regex": ["x" * (config.EXCLUDE_PATTERN_MAX_LEN + 1)]})

    def test_defaults(self):
        merged = settings_module.merge_settings({}, {})
        self.assertTrue(merged["incremental_scan"])
        self.assertEqual(merged["keep_rules"], [])
        self.assertEqual(merged["media_sidecar_mode"], "skip")
        self.assertTrue(merged["ignore_file_enabled"])


class CacheApiTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.server = WebServer(self.harness).start()
        self.client = self.server.client
        self.csrf = self.client.login()

    def tearDown(self):
        self.server.stop()
        self.harness.cleanup()

    def test_cache_stats_and_clear(self):
        status, _headers, payload = self.client.json("GET", "/api/cache")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("hash_cache", payload)
        self.assertIn("media_probe_cache", payload)
        self.assertIn("incremental_scan", payload)

        status, _headers, payload = self.client.json(
            "POST", "/api/cache/clear", {}, csrf=self.csrf)
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])

        status, _headers, payload = self.client.json(
            "POST", "/api/cache/clear", {"confirm": True}, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["cleared"].get("hash"))

    def test_meta_exposes_keep_rules(self):
        status, _headers, payload = self.client.json("GET", "/api/meta")
        self.assertEqual(status, 200)
        scan = payload.get("scan") or {}
        names = [item["name"] for item in (scan.get("keep_rules") or [])]
        self.assertIn("size_largest", names)
        self.assertIn("path_priority", names)
        self.assertIn("skip", scan.get("sidecar_modes") or [])
        self.assertEqual(scan.get("quick_sample_version"),
                         config.QUICK_SAMPLE_VERSION)


if __name__ == "__main__":
    unittest.main()
