# -*- coding: utf-8 -*-
"""v0.0.4.3：quick_sample_hash v2（大文件头 + 中 + 尾三段采样）测试。"""

from __future__ import absolute_import

import hashlib
import os
import shutil
import struct
import tempfile
import unittest

from tests.helpers import BACKEND_DIR  # noqa: F401

from dupcleaner import hashing


def reference_digest(path, sample_kb=4, large_threshold=None, algo="sha256"):
    """按任务契约独立重算一遍摘要输入，用于固定格式。"""
    sample = int(sample_kb) * 1024
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        head = handle.read(min(sample, size)) if sample > 0 else b""
        tail = b""
        if size > len(head):
            handle.seek(max(len(head), size - sample))
            tail = handle.read(sample)
        mid = None
        threshold = large_threshold or hashing.LARGE_FILE_SAMPLE_THRESHOLD
        if sample > 0 and size >= int(threshold):
            lower = len(head)
            upper = size - len(tail) - sample
            if upper >= lower:
                offset = size // 2 - sample // 2
                offset = max(lower, min(offset, upper))
                handle.seek(offset)
                mid = handle.read(sample)
    parts = [b"dcq2", struct.pack("<Q", size), b"h", head]
    if mid is not None:
        parts.extend([b"m", mid])
    parts.extend([b"t", tail])
    hasher = hashlib.new(algo)
    for part in parts:
        hasher.update(part)
    return hasher.hexdigest()


class QuickSampleHashV2Test(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-hash-v2-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, name, data):
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    @staticmethod
    def _payload(size=16 * 1024):
        return bytes(bytearray((i * 7 + 3) % 251 for i in range(size)))

    def test_module_constants(self):
        self.assertEqual(hashing.QUICK_SAMPLE_VERSION, 2)
        self.assertEqual(hashing.LARGE_FILE_SAMPLE_THRESHOLD,
                         256 * 1024 * 1024)

    def test_same_content_same_hash_matches_format(self):
        data = self._payload()
        path1 = self._write("a.bin", data)
        path2 = self._write("b.bin", data)
        # large_threshold=1 强制走三段采样路径
        digest1 = hashing.quick_sample_hash(path1, sample_kb=4,
                                            large_threshold=1)
        digest2 = hashing.quick_sample_hash(path2, sample_kb=4,
                                            large_threshold=1)
        self.assertEqual(digest1, digest2)
        self.assertEqual(digest1,
                         reference_digest(path1, sample_kb=4,
                                          large_threshold=1))

    def test_head_difference_changes_hash(self):
        base = self._payload()
        changed = bytearray(base)
        changed[0:1] = b"Z"
        path1 = self._write("head-a.bin", base)
        path2 = self._write("head-b.bin", bytes(changed))
        self.assertNotEqual(
            hashing.quick_sample_hash(path1, sample_kb=4, large_threshold=1),
            hashing.quick_sample_hash(path2, sample_kb=4, large_threshold=1))

    def test_tail_difference_changes_hash(self):
        base = self._payload()
        changed = bytearray(base)
        changed[-1:] = b"Z"
        path1 = self._write("tail-a.bin", base)
        path2 = self._write("tail-b.bin", bytes(changed))
        self.assertNotEqual(
            hashing.quick_sample_hash(path1, sample_kb=4, large_threshold=1),
            hashing.quick_sample_hash(path2, sample_kb=4, large_threshold=1))

    def test_middle_difference_changes_hash(self):
        base = self._payload()
        changed = bytearray(base)
        changed[8 * 1024:8 * 1024 + 1] = b"Z"
        path1 = self._write("mid-a.bin", base)
        path2 = self._write("mid-b.bin", bytes(changed))
        self.assertNotEqual(
            hashing.quick_sample_hash(path1, sample_kb=4, large_threshold=1),
            hashing.quick_sample_hash(path2, sample_kb=4, large_threshold=1))

    def test_large_threshold_controls_middle_sampling(self):
        base = self._payload()
        changed = bytearray(base)
        changed[8 * 1024:8 * 1024 + 1] = b"Z"
        path1 = self._write("thr-a.bin", base)
        path2 = self._write("thr-b.bin", bytes(changed))

        huge = 1024 * 1024 * 1024
        # 大阈值：只采样头尾，中间差异被忽略
        self.assertEqual(
            hashing.quick_sample_hash(path1, sample_kb=4, large_threshold=huge),
            hashing.quick_sample_hash(path2, sample_kb=4, large_threshold=huge))
        self.assertEqual(
            hashing.quick_sample_hash(path1, sample_kb=4, large_threshold=huge),
            reference_digest(path1, sample_kb=4, large_threshold=huge))

        # 小阈值：采样中间段，能发现只改中间的文件
        self.assertNotEqual(
            hashing.quick_sample_hash(path1, sample_kb=4, large_threshold=1),
            hashing.quick_sample_hash(path2, sample_kb=4, large_threshold=1))
        self.assertEqual(
            hashing.quick_sample_hash(path1, sample_kb=4, large_threshold=1),
            reference_digest(path1, sample_kb=4, large_threshold=1))

        # 默认阈值（256 MiB）对小文件等价于大阈值
        self.assertEqual(
            hashing.quick_sample_hash(path1, sample_kb=4),
            hashing.quick_sample_hash(path2, sample_kb=4))

    def test_zero_byte_file(self):
        path = self._write("empty.bin", b"")
        digest = hashing.quick_sample_hash(path)
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, reference_digest(path))
        self.assertEqual(digest, hashing.quick_sample_hash(path))

    def test_small_file_head_only_format(self):
        path = self._write("small.bin", b"small")
        digest = hashing.quick_sample_hash(path, sample_kb=64)
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, reference_digest(path, sample_kb=64))
        # size <= sample 时没有尾段内容（仍保留 b"t" 标记）
        expected = hashlib.sha256(
            b"dcq2" + struct.pack("<Q", 5) + b"h" + b"small" + b"t").hexdigest()
        self.assertEqual(digest, expected)

    def test_missing_file_raises_oserror(self):
        missing = os.path.join(self.tmpdir, "does-not-exist.bin")
        with self.assertRaises(OSError):
            hashing.quick_sample_hash(missing)
        with self.assertRaises(OSError):
            hashing.quick_sample_hash(missing, size=0)

    def test_md5_and_determinism(self):
        data = self._payload()
        path = self._write("md5.bin", data)
        first = hashing.quick_sample_hash(path, algo="md5", sample_kb=4,
                                          large_threshold=1)
        second = hashing.quick_sample_hash(path, algo="md5", sample_kb=4,
                                           large_threshold=1)
        self.assertEqual(len(first), 32)
        self.assertEqual(first, second)
        self.assertEqual(first,
                         reference_digest(path, sample_kb=4,
                                          large_threshold=1, algo="md5"))

    def test_size_parameter_is_consistent(self):
        data = self._payload()
        path = self._write("size.bin", data)
        size = os.path.getsize(path)
        self.assertEqual(
            hashing.quick_sample_hash(path, sample_kb=4, large_threshold=1),
            hashing.quick_sample_hash(path, sample_kb=4, large_threshold=1,
                                      size=size))
        self.assertEqual(
            hashing.quick_sample_hash(path, sample_kb=4),
            hashing.quick_sample_hash(path, sample_kb=4, size=size))
        # size 传入时对缺失文件仍按 OSError 抛出（不会静默返回摘要）
        missing = os.path.join(self.tmpdir, "no-size.bin")
        with self.assertRaises(OSError):
            hashing.quick_sample_hash(missing, size=1234)


if __name__ == "__main__":
    unittest.main()
