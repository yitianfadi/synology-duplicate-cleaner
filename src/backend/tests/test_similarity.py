# -*- coding: utf-8 -*-
"""内容相似度模式测试（docs/SIMILARITY_CONTRACT.md §7）。"""

import hashlib
import html
import io
import json
import os
import struct
import time
import unittest
import zipfile
import zlib

from tests.helpers import Harness, write_bytes, write_text

from dupcleaner import config, similarity
from dupcleaner import deletion as deletion_module
from dupcleaner import settings as settings_module
from dupcleaner.errors import ApiError

BASE_TEXT = (
    "The quick brown fox jumps over the lazy dog. "
    "Synology duplicate cleaner similarity minhash feature extraction. "
    "Content similarity is a heuristic and must be reviewed by a human. "
) * 30
VARIANT_TEXT = BASE_TEXT.replace("quick", "slow").replace("lazy", "active")
UNRELATED_TEXT = (
    "完全不同的主题：星空、宇宙、星系与黑洞的科普文字，用于制造无关内容。"
    "This paragraph shares no meaningful sentences with the other sample. "
) * 40
REPORT_TEXT = (
    "Quarterly revenue increased by twelve percent compared to last year. "
    "Operating expenses remained stable across all regions and products. "
) * 30
REPORT_VARIANT = REPORT_TEXT.replace("twelve", "eleven")


def make_png(width, height, pixel_fn):
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            value = pixel_fn(x, y) & 0xFF
            raw += bytes([value, value, value])

    def chunk(kind, payload):
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", crc))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def make_docx(text):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            "<w:document><w:body><w:p><w:r><w:t>%s</w:t></w:r></w:p></w:body></w:document>"
            % html.escape(text))
    return buffer.getvalue()


def make_pdf(text):
    stream = (b"BT /F1 12 Tf (" + text.encode("latin-1", "replace")
              + b") Tj ET")
    compressed = zlib.compress(stream, 6)
    return (b"%PDF-1.4\n1 0 obj\n<< /Length " + str(len(compressed)).encode()
            + b" >>\nstream\n" + compressed + b"\nendstream\nendobj\n"
            + b"trailer<<>>\n%%EOF")


def make_empty_pdf():
    compressed = zlib.compress(b"0 0 m 10 10 l S", 6)
    return (b"%PDF-1.4\n1 0 obj\n<< /Length " + str(len(compressed)).encode()
            + b" >>\nstream\n" + compressed + b"\nendstream\nendobj\n"
            + b"trailer<<>>\n%%EOF")


class SimilaritySettingsTest(unittest.TestCase):
    def test_defaults(self):
        defaults = settings_module.default_settings()
        self.assertEqual(defaults["scan_mode"], "exact")
        self.assertEqual(defaults["similarity_threshold"], 85)
        self.assertEqual(defaults["similarity_concurrency"], 2)
        self.assertTrue(defaults["similarity_cache"])
        self.assertEqual(defaults["similarity_max_file_mb"], 32)
        self.assertFalse(defaults["similarity_allow_binary"])
        self.assertIn(".txt", defaults["similarity_extensions"])
        self.assertEqual(defaults["similarity_exclude_extensions"], [])
        self.assertEqual(settings_module.validate_effective(defaults), defaults)

    def test_patch_and_validation(self):
        patch = settings_module.validate_patch({
            "scan_mode": "similar",
            "similarity_threshold": 70,
            "similarity_concurrency": 4,
            "similarity_cache": False,
            "similarity_max_file_mb": 8,
            "similarity_allow_binary": True,
            "similarity_extensions": [".txt", ".dat"],
            "similarity_exclude_extensions": [".pdf"],
        })
        self.assertEqual(patch["scan_mode"], "similar")
        self.assertEqual(patch["similarity_threshold"], 70)

        invalid = [
            {"scan_mode": "fuzzy"},
            {"similarity_threshold": -1},
            {"similarity_threshold": 101},
            {"similarity_concurrency": 0},
            {"similarity_concurrency": 5},
            {"similarity_max_file_mb": 0},
            {"similarity_max_file_mb": 2048},
            {"similarity_cache": "yes"},
            {"similarity_allow_binary": 1},
            {"similarity_extensions": ["txt"]},
            {"similarity_exclude_extensions": [".waytoolongextension"]},
        ]
        for item in invalid:
            with self.assertRaises(ApiError, msg=str(item)) as ctx:
                settings_module.validate_patch(item)
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_backward_compatible_settings(self):
        old = {"hash_algo": "md5", "keep_mode": "latest"}
        effective = settings_module.validate_effective(old)
        self.assertEqual(effective["hash_algo"], "md5")
        self.assertEqual(effective["scan_mode"], "exact")
        self.assertEqual(effective["similarity_threshold"], 85)


class FeatureExtractionTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()

    def tearDown(self):
        self.harness.cleanup()

    def _options(self, **overrides):
        options = self.harness.services.settings.get()
        options.update(overrides)
        return options

    def test_text_minhash_similarity_and_determinism(self):
        root = self.harness.dev_root
        first = write_text(os.path.join(root, "a.txt"), BASE_TEXT)
        second = write_text(os.path.join(root, "b.txt"), VARIANT_TEXT)
        third = write_text(os.path.join(root, "c.txt"), UNRELATED_TEXT)
        options = self._options()
        feature_a = similarity.compute_feature(first, "text", ".txt", options)
        feature_b = similarity.compute_feature(second, "text", ".txt", options)
        feature_c = similarity.compute_feature(third, "text", ".txt", options)
        self.assertEqual(feature_a["method"], "minhash")
        self.assertEqual(len(feature_a["signature"]), 64)
        # 同内容不同进程/重复计算必须一致（确定性）
        again = similarity.compute_feature(first, "text", ".txt", options)
        self.assertEqual(feature_a["signature"], again["signature"])
        self.assertEqual(feature_a["content_sha256"],
                         hashlib.sha256(BASE_TEXT.encode("utf-8")).hexdigest())
        similar_score = similarity.pair_similarity_percent(feature_a, feature_b)
        unrelated_score = similarity.pair_similarity_percent(feature_a, feature_c)
        self.assertGreater(similar_score, unrelated_score)
        self.assertGreaterEqual(similar_score, 60)
        self.assertLess(unrelated_score, 30)

    def test_png_dhash(self):
        root = self.harness.dev_root
        png1 = write_bytes(os.path.join(root, "img1.png"),
                           make_png(32, 32, lambda x, y: x * 8 + y * 3))
        png2 = write_bytes(os.path.join(root, "img2.png"),
                           make_png(32, 32, lambda x, y: x * 8 + y * 3 + (x * y) % 3))
        png3 = write_bytes(os.path.join(root, "img3.png"),
                           make_png(32, 32, lambda x, y: 255 - (x * 8 + y * 3)))
        options = self._options()
        f1 = similarity.compute_feature(png1, "image", ".png", options)
        f2 = similarity.compute_feature(png2, "image", ".png", options)
        f3 = similarity.compute_feature(png3, "image", ".png", options)
        self.assertEqual(f1["method"], "phash")
        self.assertFalse(f1["low_confidence"])
        self.assertGreaterEqual(similarity.pair_similarity_percent(f1, f2), 90)
        self.assertLess(similarity.pair_similarity_percent(f1, f3), 50)

    def test_jpeg_without_pillow_low_confidence(self):
        if similarity.PILLOW_AVAILABLE:
            self.skipTest("pillow available; fallback path not exercised")
        root = self.harness.dev_root
        jpeg = write_bytes(os.path.join(root, "photo.jpg"),
                           b"\xff\xd8\xff\xe0" + os.urandom(4096))
        feature = similarity.compute_feature(jpeg, "image", ".jpg",
                                             self._options())
        self.assertEqual(feature["method"], "bytes")
        self.assertTrue(feature["low_confidence"])
        with open(jpeg, "rb") as handle:
            expected = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(feature.get("content_sha256"), expected)

    def test_docx_and_pdf_text(self):
        root = self.harness.dev_root
        docx1 = write_bytes(os.path.join(root, "doc1.docx"), make_docx(BASE_TEXT))
        docx2 = write_bytes(os.path.join(root, "doc2.docx"), make_docx(VARIANT_TEXT))
        pdf1 = write_bytes(os.path.join(root, "doc1.pdf"), make_pdf(BASE_TEXT[:300]))
        pdf2 = write_bytes(os.path.join(root, "doc2.pdf"), make_pdf(VARIANT_TEXT[:300]))
        options = self._options()
        f_docx1 = similarity.compute_feature(docx1, "document", ".docx", options)
        f_docx2 = similarity.compute_feature(docx2, "document", ".docx", options)
        self.assertEqual(f_docx1["method"], "minhash")
        self.assertGreaterEqual(
            similarity.pair_similarity_percent(f_docx1, f_docx2), 60)
        f_pdf1 = similarity.compute_feature(pdf1, "document", ".pdf", options)
        f_pdf2 = similarity.compute_feature(pdf2, "document", ".pdf", options)
        self.assertGreaterEqual(
            similarity.pair_similarity_percent(f_pdf1, f_pdf2), 50)

    def test_no_text_documents_are_skipped(self):
        root = self.harness.dev_root
        empty_docx = write_bytes(os.path.join(root, "empty.docx"),
                                 make_docx(""))
        empty_pdf = write_bytes(os.path.join(root, "empty.pdf"), make_empty_pdf())
        options = self._options()
        with self.assertRaises(similarity.SimilaritySkip) as ctx:
            similarity.compute_feature(empty_docx, "document", ".docx", options)
        self.assertEqual(ctx.exception.reason, "no_text")
        with self.assertRaises(similarity.SimilaritySkip) as ctx:
            similarity.compute_feature(empty_pdf, "document", ".pdf", options)
        self.assertEqual(ctx.exception.reason, "no_text")

    def test_bytes_feature_requires_allow_binary(self):
        options = self._options()
        kind, reason, _detail = similarity.classify_extension(".dat", options)
        self.assertIsNone(kind)
        self.assertEqual(reason, "type")
        allowed = self._options(similarity_allow_binary=True,
                                similarity_extensions=[".dat"])
        kind, reason, _detail = similarity.classify_extension(".dat", allowed)
        self.assertEqual(kind, "bytes")
        self.assertIsNone(reason)
        data = os.urandom(100 * 1024)
        root = self.harness.dev_root
        first = write_bytes(os.path.join(root, "one.dat"), data)
        second = write_bytes(os.path.join(root, "two.dat"),
                             data[:50000] + b"CHANGED" + data[50007:])
        feature_a = similarity.compute_feature(first, "bytes", ".dat", allowed)
        feature_b = similarity.compute_feature(second, "bytes", ".dat", allowed)
        self.assertEqual(feature_a["method"], "bytes")
        self.assertGreaterEqual(
            similarity.pair_similarity_percent(feature_a, feature_b), 80)

    def test_binary_categories_always_skipped(self):
        options = self._options(similarity_allow_binary=True,
                                similarity_extensions=[".zip", ".mp4"])
        for extension in (".zip", ".7z", ".mp4", ".exe", ".iso"):
            kind, reason, _detail = similarity.classify_extension(extension, options)
            self.assertIsNone(kind, extension)
            self.assertEqual(reason, "binary", extension)

    def test_feature_cache_persistence_and_lru(self):
        path = os.path.join(self.harness.data_dir, "test_feature_cache.json")
        cache = similarity.FeatureCache(path, max_entries=2)
        cache.put("k1", {"feature": {"signature": [1]}})
        cache.put("k2", {"feature": {"signature": [2]}})
        self.assertTrue(cache.save())
        reloaded = similarity.FeatureCache(path, max_entries=2)
        self.assertIsNotNone(reloaded.get("k1"))
        reloaded.put("k3", {"feature": {"signature": [3]}})
        reloaded.put("k4", {"feature": {"signature": [4]}})
        self.assertLessEqual(len(reloaded), 2)
        self.assertIsNone(reloaded.get("k1"))


class SimilarScanTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root
        os.makedirs(os.path.join(self.root, "#recycle"), exist_ok=True)

    def tearDown(self):
        self.harness.cleanup()

    def _mixed_fixture(self):
        root = self.root
        exact_a = write_text(os.path.join(root, "exact_a.txt"),
                             "exact duplicate content " * 40)
        exact_b = write_text(os.path.join(root, "sub", "exact_b.txt"),
                             "exact duplicate content " * 40)
        similar_a = write_text(os.path.join(root, "sim_a.txt"), BASE_TEXT)
        similar_b = write_text(os.path.join(root, "sim_b.txt"), VARIANT_TEXT)
        unrelated = write_text(os.path.join(root, "other.txt"), UNRELATED_TEXT)
        self.harness.set_creation(exact_a, 1000)
        self.harness.set_creation(exact_b, 1100)
        self.harness.set_creation(similar_a, 2000)
        self.harness.set_creation(similar_b, 2100)
        self.harness.set_creation(unrelated, 2200)
        return {"exact_a": exact_a, "exact_b": exact_b,
                "similar_a": similar_a, "similar_b": similar_b,
                "unrelated": unrelated}

    def test_exact_mode_has_no_similar_groups(self):
        self._mixed_fixture()
        scan_id, meta = self.harness.run_scan()
        self.assertEqual(meta["status"], "completed")
        groups = self.harness.groups(scan_id)
        self.assertTrue(groups)
        for group in groups:
            self.assertEqual(group["match_type"], "exact")
            self.assertEqual(group["similarity_score"], 100)
        self.assertEqual(meta["stats"]["similar_groups"], 0)
        self.assertEqual(meta["stats"]["groups"], 1)
        self.assertEqual(meta["stats"]["exact_groups"], 1)

    def test_similar_mode_groups_and_fields(self):
        fixture = self._mixed_fixture()
        scan_id, meta = self.harness.run_scan(
            options={"scan_mode": "similar", "similarity_threshold": 85})
        self.assertEqual(meta["status"], "completed")
        stats = meta["stats"]
        self.assertEqual(stats["exact_groups"], 1)
        self.assertEqual(stats["similar_groups"], 1)
        self.assertEqual(stats["exact_files"], 2)
        self.assertEqual(stats["similar_files"], 2)
        self.assertGreater(stats["similar_reclaimable_bytes"], 0)
        self.assertEqual(stats["similarity_candidates"], 3)
        self.assertGreater(stats["similarity_features"], 0)
        self.assertGreater(stats["similarity_pairs_checked"], 0)
        self.assertEqual(stats["groups"], 2)
        self.assertEqual(stats["duplicate_files"], 4)

        groups = self.harness.groups(scan_id)
        exact = [group for group in groups if group["match_type"] == "exact"]
        similar = [group for group in groups if group["match_type"] == "similar"]
        self.assertEqual(len(exact), 1)
        self.assertEqual(len(similar), 1)
        for file_info in exact[0]["files"]:
            self.assertEqual(file_info["match_type"], "exact")
            self.assertEqual(file_info["similarity_score"], 100)
        similar_group = similar[0]
        self.assertTrue(similar_group["group_id"].startswith("s-"))
        self.assertEqual(similar_group["hash"], "")
        self.assertEqual(similar_group["algo"], "")
        self.assertGreaterEqual(similar_group["similarity_score"], 85)
        self.assertEqual(similar_group["similarity_method"], "minhash")
        self.assertFalse(similar_group["low_confidence"])
        self.assertTrue(similar_group["manual_review_required"])
        self.assertEqual(similar_group["keep_reason"], "latest_created")
        self.assertEqual(similar_group["count"], 2)
        keeper = [item for item in similar_group["files"] if item["is_keep"]]
        self.assertEqual(len(keeper), 1)
        self.assertEqual(keeper[0]["file_id"], similar_group["keep_file_id"])
        self.assertTrue(keeper[0]["file_id"].startswith("sf-"))
        self.assertEqual(keeper[0]["similarity_score"], 100)
        for file_info in similar_group["files"]:
            self.assertFalse(file_info["default_selected"])
            self.assertEqual(file_info["match_type"], "similar")
            self.assertEqual(len(file_info["hash"]), 64)
            self.assertEqual(file_info["similarity_method"], "minhash")
            self.assertFalse(file_info["low_confidence"])
        non_keeper = [item for item in similar_group["files"]
                      if not item["is_keep"]]
        self.assertTrue(non_keeper)
        self.assertGreaterEqual(non_keeper[0]["similarity_score"], 85)
        # 相似组文件名必须来自相似文本对，而不是精准重复组
        names = {item["name"] for item in similar_group["files"]}
        self.assertEqual(names, {"sim_a.txt", "sim_b.txt"})
        self.assertNotIn(fixture["unrelated"],
                         [item["path"] for item in similar_group["files"]])

    def test_threshold_controls_grouping(self):
        self._mixed_fixture()
        high_id, high_meta = self.harness.run_scan(
            options={"scan_mode": "similar", "similarity_threshold": 99})
        self.assertEqual(high_meta["stats"]["similar_groups"], 0)
        low_id, low_meta = self.harness.run_scan(
            options={"scan_mode": "similar", "similarity_threshold": 50})
        self.assertEqual(low_meta["stats"]["similar_groups"], 1)
        self.assertTrue(any(group["match_type"] == "similar"
                            for group in self.harness.groups(low_id)))

    def test_groups_filter_sort_and_totals(self):
        self._mixed_fixture()
        scan_id, _meta = self.harness.run_scan(
            options={"scan_mode": "similar", "similarity_threshold": 85})
        page = self.harness.services.scanner.groups_page(scan_id)
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["exact_total"], 1)
        self.assertEqual(page["similar_total"], 1)
        exact_page = self.harness.services.scanner.groups_page(
            scan_id, match_type="exact")
        self.assertEqual(exact_page["total"], 1)
        self.assertEqual(exact_page["groups"][0]["match_type"], "exact")
        similar_page = self.harness.services.scanner.groups_page(
            scan_id, match_type="similar")
        self.assertEqual(similar_page["total"], 1)
        self.assertEqual(similar_page["match_type"], "similar")
        self.assertEqual(similar_page["groups"][0]["match_type"], "similar")
        sorted_page = self.harness.services.scanner.groups_page(
            scan_id, sort="similarity_desc", limit=10)
        self.assertEqual(sorted_page["groups"][0]["match_type"], "exact")
        with self.assertRaises(ApiError):
            self.harness.services.scanner.groups_page(scan_id,
                                                      match_type="bogus")

    def test_csv_has_match_type_and_score_columns(self):
        self._mixed_fixture()
        scan_id, _meta = self.harness.run_scan(
            options={"scan_mode": "similar", "similarity_threshold": 85})
        path = self.harness.services.scanner.write_csv(scan_id)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            import csv as csv_module
            rows = list(csv_module.reader(
                io.StringIO(raw.decode("utf-8-sig"))))
            self.assertEqual(rows[0], [u"组号", u"匹配类型", u"状态", u"文件路径",
                                       u"大小(字节)", u"SHA256/MD5", u"创建时间",
                                       u"修改时间", u"保留", u"默认勾选",
                                       u"时间来源", u"相似度分值"])
            match_labels = {row[1] for row in rows[1:]}
            self.assertEqual(match_labels, {u"哈希一致", u"内容相似"})
            similar_rows = [row for row in rows[1:] if row[1] == u"内容相似"]
            self.assertTrue(similar_rows)
            scores = [int(row[11]) for row in similar_rows]
            self.assertTrue(all(0 <= score <= 100 for score in scores))
            self.assertTrue(any(score < 100 for score in scores))
        finally:
            os.unlink(path)

    def test_binary_and_oversize_are_skipped(self):
        root = self.root
        zip_a = write_bytes(os.path.join(root, "a.zip"),
                            b"PK\x03\x04" + os.urandom(2048))
        zip_b = write_bytes(os.path.join(root, "b.zip"),
                            b"PK\x03\x04" + os.urandom(2048))
        big = write_text(os.path.join(root, "big.txt"), "x" * (2 * 1024 * 1024))
        scan_id, meta = self.harness.run_scan(options={
            "scan_mode": "similar",
            "similarity_max_file_mb": 1,
        })
        skipped = meta["stats"]["similarity_skipped"]
        self.assertGreaterEqual(skipped["binary"], 2)
        self.assertGreaterEqual(skipped["size"], 1)
        self.assertEqual(meta["stats"]["similar_groups"], 0)

    def test_degraded_image_falls_back_to_bytes(self):
        root = self.root
        broken_a = write_bytes(os.path.join(root, "broken_a.png"),
                               b"\x89PNG\r\n\x1a\n" + os.urandom(4096))
        broken_b = write_bytes(os.path.join(root, "broken_b.png"),
                               b"\x89PNG\r\n\x1a\n" + os.urandom(4096))
        scan_id, meta = self.harness.run_scan(options={"scan_mode": "similar"})
        self.assertGreaterEqual(meta["stats"]["similarity_skipped"]["decode"], 2)
        candidates = [item for group in self.harness.groups(scan_id)
                      for item in group["files"]]
        for item in candidates:
            if item["name"].startswith("broken_"):
                self.assertTrue(item["low_confidence"])
                self.assertEqual(item["similarity_method"], "bytes")

    def test_exact_duplicates_not_in_similar_groups(self):
        root = self.root
        first = write_text(os.path.join(root, "dup1.txt"), BASE_TEXT)
        second = write_text(os.path.join(root, "dup2.txt"), BASE_TEXT)
        self.harness.set_creation(first, 1000)
        self.harness.set_creation(second, 1100)
        scan_id, meta = self.harness.run_scan(options={"scan_mode": "similar"})
        self.assertEqual(meta["stats"]["exact_groups"], 1)
        self.assertEqual(meta["stats"]["similar_groups"], 0)
        group = self.harness.groups(scan_id)[0]
        self.assertEqual(group["match_type"], "exact")
        paths = [item["path"] for item in group["files"]]
        self.assertEqual(sorted(paths), sorted([first, second]))

    def test_feature_cache_hits_on_second_scan(self):
        self._mixed_fixture()
        options = {"scan_mode": "similar", "similarity_threshold": 85,
                   "similarity_cache": True}
        _first_id, first_meta = self.harness.run_scan(options=options)
        self.assertEqual(first_meta["stats"]["similarity_cache_hits"], 0)
        self.assertGreater(first_meta["stats"]["similarity_cache_misses"], 0)
        cache_file = os.path.join(self.harness.data_dir,
                                  config.SIMILARITY_CACHE_FILENAME)
        self.assertTrue(os.path.isfile(cache_file))
        _second_id, second_meta = self.harness.run_scan(options=options)
        self.assertGreater(second_meta["stats"]["similarity_cache_hits"], 0)
        # 修改文件后缓存 key 变化，必须重新计算
        target = os.path.join(self.root, "sim_b.txt")
        with open(target, "ab") as handle:
            handle.write(b"\nappended change")
        _third_id, third_meta = self.harness.run_scan(options=options)
        self.assertGreater(third_meta["stats"]["similarity_cache_misses"], 0)

    def test_stop_during_similarity_stage(self):
        root = self.root
        for index in range(14):
            text = (u"相似度停止测试文件 %d。" % index) * 5000 + ("filler-%d " % index) * 500
            write_text(os.path.join(root, "stop_%02d.txt" % index), text)
        scanner = self.harness.services.scanner
        scan_id = scanner.start_scan(
            [root], [], {"scan_mode": "similar", "similarity_concurrency": 1},
            "admin")
        deadline = time.time() + 60
        seen_similarity = False
        while time.time() < deadline:
            meta = scanner.get_scan(scan_id)
            if meta["stage"] == "similarity":
                seen_similarity = True
                break
            if meta["status"] in ("completed", "failed", "stopped"):
                break
            time.sleep(0.02)
        stopped = scanner.stop(scan_id)
        self.assertIn(stopped["status"], ("stopping", "stopped", "completed"))
        final_meta = self.harness.wait_scan(scan_id, timeout=60)
        self.assertIn(final_meta["status"], ("stopped", "completed"))
        if seen_similarity:
            self.assertEqual(final_meta["status"], "stopped")


class SimilarDeletionTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root
        os.makedirs(os.path.join(self.root, "#recycle"), exist_ok=True)
        self.session = self.harness.login()
        self.deletion = self.harness.services.deletion

    def tearDown(self):
        self.harness.cleanup()

    def _scan_similar_pair(self, with_exact=False):
        root = self.root
        first = write_text(os.path.join(root, "sim_a.txt"), BASE_TEXT)
        second = write_text(os.path.join(root, "sim_b.txt"), VARIANT_TEXT)
        self.harness.set_creation(first, 1000)
        self.harness.set_creation(second, 2000)
        fixture = {"similar_a": first, "similar_b": second}
        if with_exact:
            exact_a = write_text(os.path.join(root, "exact_a.txt"),
                                 "exact duplicate content " * 40)
            exact_b = write_text(os.path.join(root, "exact_b.txt"),
                                 "exact duplicate content " * 40)
            self.harness.set_creation(exact_a, 3000)
            self.harness.set_creation(exact_b, 3100)
            fixture.update({"exact_a": exact_a, "exact_b": exact_b})
        scan_id, meta = self.harness.run_scan(options={
            "scan_mode": "similar", "similarity_threshold": 85})
        self.assertEqual(meta["status"], "completed")
        return scan_id, fixture

    def _similar_group(self, scan_id):
        for group in self.harness.groups(scan_id):
            if group["match_type"] == "similar":
                return group
        raise AssertionError("no similar group")

    def _wait_job(self, scan_id, job_id, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.deletion.get_job(scan_id, job_id)
            if job.get("status") in ("completed", "partial", "failed", "stopped"):
                return job
            time.sleep(0.02)
        raise AssertionError("delete job did not finish")

    def _non_keeper(self, group):
        for file_info in group["files"]:
            if not file_info["is_keep"]:
                return file_info
        raise AssertionError("no non-keeper")

    def test_review_required_for_similar_deletion(self):
        scan_id, _fixture = self._scan_similar_pair()
        group = self._similar_group(scan_id)
        non_keeper = self._non_keeper(group)
        with self.assertRaises(ApiError) as ctx:
            self.deletion.create_plan(self.session, scan_id, {
                "file_ids": [non_keeper["file_id"]], "recycle": True})
        self.assertEqual(ctx.exception.code, "SIMILAR_REVIEW_REQUIRED")
        with self.assertRaises(ApiError) as ctx:
            self.deletion.create_plan(self.session, scan_id, {
                "file_ids": [non_keeper["file_id"]], "recycle": True,
                "confirm_similar": True,
                "reviewed_similar_group_ids": ["s-999999"]})
        self.assertEqual(ctx.exception.code, "SIMILAR_REVIEW_REQUIRED")
        plan = self.deletion.create_plan(self.session, scan_id, {
            "file_ids": [non_keeper["file_id"]], "recycle": True,
            "confirm_similar": True,
            "reviewed_similar_group_ids": [group["group_id"]]})
        self.assertEqual(plan["count"], 1)
        self.assertEqual(plan["similar_count"], 1)
        self.assertEqual(plan["exact_count"], 0)
        self.assertEqual(plan["bytes"], plan["similar_bytes"])
        self.assertTrue(plan["manual_review"])
        self.assertEqual(plan["reviewed_similar_group_ids"],
                         [group["group_id"]])
        self.assertEqual(plan["files"][0]["match_type"], "similar")
        self.assertGreaterEqual(plan["files"][0]["similarity_score"], 85)

    def test_similar_delete_with_recycle_and_keeper_safe(self):
        scan_id, fixture = self._scan_similar_pair()
        group = self._similar_group(scan_id)
        non_keeper = self._non_keeper(group)
        keeper = [item for item in group["files"] if item["is_keep"]][0]

        # keeper 永远不能被删除（与合法候选混合时只跳过 keeper）
        keeper_plan = self.deletion.create_plan(self.session, scan_id, {
            "file_ids": [keeper["file_id"], non_keeper["file_id"]],
            "recycle": True, "confirm_similar": True,
            "reviewed_similar_group_ids": [group["group_id"]]})
        self.assertEqual(keeper_plan["count"], 1)
        self.assertIn("IS_KEEPER",
                      [item["reason"] for item in keeper_plan["skipped"]])

        plan = self.deletion.create_plan(self.session, scan_id, {
            "file_ids": [non_keeper["file_id"]], "recycle": True,
            "confirm_similar": True,
            "reviewed_similar_group_ids": [group["group_id"]]})
        job_id = self.deletion.start_delete(self.session, scan_id, {
            "plan_id": plan["plan_id"], "confirm": "DELETE"})
        job = self._wait_job(scan_id, job_id)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["results"][0]["match_type"], "similar")
        self.assertEqual(job["results"][0]["status"], "recycled")
        self.assertFalse(os.path.exists(non_keeper["path"]))
        self.assertTrue(os.path.exists(keeper["path"]))
        audit = self.harness.services.store.read_audit(limit=200)
        actions = [entry["action"] for entry in audit]
        self.assertIn("delete_plan", actions)
        delete_plans = [entry for entry in audit
                        if entry["action"] == "delete_plan"]
        self.assertTrue(delete_plans)
        self.assertEqual(delete_plans[-1]["detail"]["similar_count"], 1)

    def test_mixed_exact_and_similar_file_ids(self):
        scan_id, _fixture = self._scan_similar_pair(with_exact=True)
        similar_group = self._similar_group(scan_id)
        exact_group = None
        for group in self.harness.groups(scan_id):
            if group["match_type"] == "exact":
                exact_group = group
                break
        self.assertIsNotNone(exact_group)
        similar_candidate = self._non_keeper(similar_group)
        exact_candidate = self._non_keeper(exact_group)
        plan = self.deletion.create_plan(self.session, scan_id, {
            "file_ids": [exact_candidate["file_id"],
                         similar_candidate["file_id"]],
            "recycle": True, "confirm_similar": True,
            "reviewed_similar_group_ids": [similar_group["group_id"]]})
        self.assertEqual(plan["count"], 2)
        self.assertEqual(plan["exact_count"], 1)
        self.assertEqual(plan["similar_count"], 1)
        self.assertTrue(plan["manual_review"])
        match_types = sorted(item["match_type"] for item in plan["files"])
        self.assertEqual(match_types, ["exact", "similar"])
        # 未提供 review 时，即使混有精准文件也整体拒绝
        with self.assertRaises(ApiError) as ctx:
            self.deletion.create_plan(self.session, scan_id, {
                "file_ids": [exact_candidate["file_id"],
                             similar_candidate["file_id"]],
                "recycle": True})
        self.assertEqual(ctx.exception.code, "SIMILAR_REVIEW_REQUIRED")

    def test_scope_all_duplicates_never_includes_similar(self):
        scan_id, _fixture = self._scan_similar_pair(with_exact=True)
        plan = self.deletion.create_plan(self.session, scan_id, {
            "scope": "all_duplicates", "recycle": True})
        self.assertEqual(plan["similar_count"], 0)
        self.assertEqual(plan["exact_count"], 1)
        self.assertFalse(plan["manual_review"])
        for item in plan["files"]:
            self.assertEqual(item["match_type"], "exact")

    def test_similar_file_changed_skipped(self):
        root = self.root
        first = write_text(os.path.join(root, "a1.txt"), BASE_TEXT)
        second = write_text(os.path.join(root, "a2.txt"), VARIANT_TEXT)
        third = write_text(os.path.join(root, "b1.txt"), REPORT_TEXT)
        fourth = write_text(os.path.join(root, "b2.txt"), REPORT_VARIANT)
        for index, path in enumerate((first, second, third, fourth)):
            self.harness.set_creation(path, 1000 + index * 100)
        scan_id, meta = self.harness.run_scan(options={
            "scan_mode": "similar", "similarity_threshold": 85})
        similar_groups = [group for group in self.harness.groups(scan_id)
                          if group["match_type"] == "similar"]
        self.assertEqual(len(similar_groups), 2)
        changed_group = similar_groups[0]
        valid_group = similar_groups[1]
        changed = self._non_keeper(changed_group)
        valid = self._non_keeper(valid_group)
        with open(changed["path"], "ab") as handle:
            handle.write(b"\nchanged after scan")
        plan = self.deletion.create_plan(self.session, scan_id, {
            "file_ids": [changed["file_id"], valid["file_id"]],
            "recycle": True, "confirm_similar": True,
            "reviewed_similar_group_ids": [changed_group["group_id"],
                                           valid_group["group_id"]]})
        self.assertEqual(plan["similar_count"], 1)
        self.assertEqual(plan["count"], 1)
        self.assertIn("FILE_CHANGED",
                      [item["reason"] for item in plan["skipped"]])
        self.assertEqual(plan["files"][0]["file_id"], valid["file_id"])

    def test_revalidate_similar_uses_own_hashes(self):
        root = self.root
        candidate = write_text(os.path.join(root, "cand.txt"), BASE_TEXT)
        keeper = write_text(os.path.join(root, "keeper.txt"), VARIANT_TEXT)
        st_candidate = os.stat(candidate)
        st_keeper = os.stat(keeper)
        record = {
            "path": candidate, "size": st_candidate.st_size,
            "mtime_ns": st_candidate.st_mtime_ns,
            "inode": st_candidate.st_ino, "dev": st_candidate.st_dev,
            "hash": hashlib.sha256(BASE_TEXT.encode("utf-8")).hexdigest(),
            "match_type": "similar", "group_id": "s-000001",
        }
        keeper_record = {
            "path": keeper, "size": st_keeper.st_size,
            "mtime_ns": st_keeper.st_mtime_ns,
            "inode": st_keeper.st_ino, "dev": st_keeper.st_dev,
            "hash": hashlib.sha256(VARIANT_TEXT.encode("utf-8")).hexdigest(),
            "match_type": "similar", "group_id": "s-000001",
        }
        ok = deletion_module.revalidate(
            record, keeper_record, self.harness.services.shares,
            algo="sha256", hash_check=True)
        self.assertTrue(ok["ok"], ok)

        tampered = dict(record)
        tampered["hash"] = "0" * 64
        mismatch = deletion_module.revalidate(
            tampered, keeper_record, self.harness.services.shares,
            algo="sha256", hash_check=True)
        self.assertEqual(mismatch["code"], "HASH_MISMATCH")

        with open(keeper, "ab") as handle:
            handle.write(b"\nkeeper changed")
        changed = deletion_module.revalidate(
            record, keeper_record, self.harness.services.shares,
            algo="sha256", hash_check=True)
        self.assertEqual(changed["code"], "FILE_CHANGED")

    def test_manual_keep_similar_group_still_blocked(self):
        root = self.root
        first = write_text(os.path.join(root, "tie_a.txt"), BASE_TEXT)
        second = write_text(os.path.join(root, "tie_b.txt"), VARIANT_TEXT)
        self.harness.set_creation(first, 5000)
        self.harness.set_creation(second, 5000)
        scan_id, _meta = self.harness.run_scan(options={
            "scan_mode": "similar", "similarity_threshold": 85})
        group = self._similar_group(scan_id)
        self.assertTrue(group["needs_manual_keep"])
        non_keeper = self._non_keeper(group)
        with self.assertRaises(ApiError) as ctx:
            self.deletion.create_plan(self.session, scan_id, {
                "file_ids": [non_keeper["file_id"]], "recycle": True,
                "confirm_similar": True,
                "reviewed_similar_group_ids": [group["group_id"]]})
        self.assertEqual(ctx.exception.code, "PLAN_INVALID")
        self.assertIn("MANUAL_KEEP_REQUIRED",
                      [item["reason"] for item in ctx.exception.details["skipped"]])


class SimilarityMergeSafetyTest(unittest.TestCase):
    def test_safe_clique_requires_all_pairs(self):
        base = [100] * 64
        near = list(base)
        near[0] = 1
        far = [7] * 64
        features = [
            {"method": "minhash", "kind": "text", "signature": base},
            {"method": "minhash", "kind": "text", "signature": near},
            {"method": "minhash", "kind": "text", "signature": far},
        ]
        # base vs near 100%，near vs far 低分，base vs far 低分 → 只能组成 {base, near}
        clusters, stats = similarity.build_groups(features, 85)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(sorted(clusters[0]["indices"]), [0, 1])
        self.assertGreater(stats["pairs_checked"], 0)
        self.assertEqual(clusters[0]["method"], "minhash")


if __name__ == "__main__":
    unittest.main()
