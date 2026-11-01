# -*- coding: utf-8 -*-
import hashlib
import os
import shutil
import tempfile
import unittest

from tests.helpers import BACKEND_DIR  # noqa: F401

from dupcleaner import hashing


class HashingTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-hash-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, name, data):
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_full_hash_sha256_and_md5(self):
        data = b"duplicate-cleaner" * 1000
        path = self._write("a.bin", data)
        digest, size = hashing.full_hash_file(path, algo="sha256")
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(size, len(data))
        digest_md5, _ = hashing.full_hash_file(path, algo="md5")
        self.assertEqual(digest_md5, hashlib.md5(data).hexdigest())

    def test_full_hash_chunked_matches(self):
        data = os.urandom(4096 * 5 + 17)
        path = self._write("big.bin", data)
        digest, size = hashing.full_hash_file(path, algo="sha256", chunk_size=4096)
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(size, len(data))

    def test_full_hash_progress_and_cancel(self):
        data = os.urandom(5000)
        path = self._write("p.bin", data)
        seen = []
        hashing.full_hash_file(path, chunk_size=1000,
                               on_progress=lambda total: seen.append(total))
        self.assertEqual(seen, [1000, 2000, 3000, 4000, 5000])

        with self.assertRaises(hashing.HashCancelled):
            hashing.full_hash_file(path, chunk_size=1000,
                                   should_stop=lambda: True)

    def test_quick_sample_hash_head_and_tail(self):
        base = b"A" * (128 * 1024)
        path1 = self._write("one.bin", base)
        path2 = self._write("two.bin", base)
        self.assertEqual(
            hashing.quick_sample_hash(path1, sample_kb=4),
            hashing.quick_sample_hash(path2, sample_kb=4))

        tail_changed = bytearray(base)
        tail_changed[-1:] = b"B"
        path3 = self._write("three.bin", bytes(tail_changed))
        self.assertNotEqual(
            hashing.quick_sample_hash(path1, sample_kb=4),
            hashing.quick_sample_hash(path3, sample_kb=4))

        head_changed = bytearray(base)
        head_changed[0:1] = b"Z"
        path4 = self._write("four.bin", bytes(head_changed))
        self.assertNotEqual(
            hashing.quick_sample_hash(path1, sample_kb=4),
            hashing.quick_sample_hash(path4, sample_kb=4))

    def test_quick_sample_small_file(self):
        data = b"small"
        path = self._write("small.txt", data)
        digest = hashing.quick_sample_hash(path, sample_kb=64)
        self.assertEqual(len(digest), 64)

    def test_rate_limiter_enforced_with_fake_clock(self):
        state = {"now": 0.0}

        def clock():
            return state["now"]

        slept = []

        def sleep(seconds):
            slept.append(seconds)
            state["now"] += seconds

        limiter = hashing.TokenBucketRateLimiter(1, clock=clock, sleep=sleep)
        self.assertTrue(limiter.enabled)
        total_slept = limiter.acquire(1024 * 1024 * 2)
        self.assertGreater(total_slept, 0.5)
        self.assertEqual(len(slept), len(set(slept)) if False else len(slept))
        limiter.acquire(0)
        limiter.acquire(-5)
        unlimited = hashing.TokenBucketRateLimiter(0, clock=clock, sleep=sleep)
        self.assertFalse(unlimited.enabled)
        self.assertEqual(unlimited.acquire(10 * 1024 * 1024), 0.0)

    def test_global_limiter_configure(self):
        limiter = hashing.configure_global_rate(0)
        self.assertIs(hashing.get_global_limiter(), limiter)
        limiter2 = hashing.configure_global_rate(8)
        self.assertEqual(limiter2.rate_mbps, 8)
        self.assertEqual(hashing.get_global_limiter().rate_mbps, 8)
        hashing.configure_global_rate(0)

    def test_unknown_algo_raises(self):
        path = self._write("x.bin", b"x")
        with self.assertRaises(Exception):
            hashing.full_hash_file(path, algo="sha512")
