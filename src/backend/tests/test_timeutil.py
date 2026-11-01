# -*- coding: utf-8 -*-
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from tests.helpers import BACKEND_DIR  # noqa: F401

from dupcleaner import timeutil


class TimeUtilTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-time-")
        self.path = os.path.join(self.tmpdir, "a.txt")
        with open(self.path, "wb") as handle:
            handle.write(b"hello")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_creation_time_shape(self):
        result = timeutil.read_creation_time(self.path)
        self.assertIn("created_ts", result)
        self.assertIn("created_ns", result)
        self.assertIn("created_source", result)
        self.assertIn(result["created_source"],
                      ("statx", "birthtime", "ctime", "unknown"))
        if timeutil.creation_time_supported():
            # 支持 btime 的平台必须拿到真实创建时间，而不是 ctime 降级
            self.assertIsNotNone(result["created_ts"])
            self.assertIn(result["created_source"], ("statx", "birthtime"))
            self.assertFalse(result["created_unsafe"])

    def test_no_silent_ctime_fallback(self):
        with mock.patch.object(timeutil, "_read_birthtime", return_value=None):
            result = timeutil.read_creation_time(self.path, allow_ctime_fallback=False)
            self.assertEqual(result["created_source"], "unknown")
            self.assertIsNone(result["created_ts"])
            self.assertTrue(result["created_unsafe"])
            explicit = timeutil.read_creation_time(self.path, allow_ctime_fallback=True)
            self.assertEqual(explicit["created_source"], "ctime")
            self.assertIsNotNone(explicit["created_ts"])
            self.assertTrue(explicit["created_unsafe"])

    def test_iso_local_has_offset(self):
        text = timeutil.iso_local(1700000000.25)
        self.assertIsNotNone(text)
        self.assertIn("T", text)
        self.assertRegex(text, r"[+-]\d{2}:\d{2}$")
        self.assertIsNone(timeutil.iso_local(None))

    def test_parse_iso_roundtrip(self):
        epoch = 1700000000.0
        parsed = timeutil.parse_iso(timeutil.iso_local(epoch))
        self.assertIsNotNone(parsed)
        self.assertAlmostEqual(parsed, epoch, delta=1)
        self.assertEqual(timeutil.parse_iso("bogus"), None)

    def test_creation_time_supported_type(self):
        self.assertIsInstance(timeutil.creation_time_supported(), bool)
        self.assertIsInstance(timeutil.statx_supported(), bool)
