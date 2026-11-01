# -*- coding: utf-8 -*-
import os
import shutil
import tempfile
import unittest

from tests.helpers import BACKEND_DIR  # noqa: F401

from dupcleaner import settings as settings_module
from dupcleaner.errors import ApiError
from dupcleaner.settings import SettingsManager, default_settings
from dupcleaner.store import Store


class SettingsValidationTest(unittest.TestCase):
    def test_defaults_valid(self):
        defaults = default_settings()
        self.assertEqual(settings_module.validate_effective(defaults), defaults)
        self.assertEqual(defaults["hash_algo"], "sha256")
        self.assertEqual(defaults["keep_mode"], "latest")
        self.assertEqual(defaults["quick_sample_kb"], 64)
        self.assertEqual(defaults["concurrency"], 2)
        self.assertEqual(defaults["max_files"], 500000)
        self.assertTrue(defaults["skip_symlinks"])
        self.assertFalse(defaults["allow_ctime_fallback"])

    def test_patch_valid(self):
        patch = settings_module.validate_patch({
            "hash_algo": "md5",
            "keep_mode": "earliest",
            "min_size_bytes": 10,
            "max_size_bytes": 100,
            "include_extensions": [".jpg", ".png"],
            "concurrency": 8,
            "hash_rate_mbps": 2000,
        })
        self.assertEqual(patch["hash_algo"], "md5")
        self.assertEqual(patch["concurrency"], 8)

    def test_unknown_key_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            settings_module.validate_patch({"nope": 1})
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertIn("nope", ctx.exception.details.get("unknown_keys", []))

    def test_invalid_values_rejected(self):
        invalid_patches = [
            {"hash_algo": "sha512"},
            {"keep_mode": "random"},
            {"quick_sample_kb": 3},
            {"quick_sample_kb": 2048},
            {"concurrency": 0},
            {"concurrency": 9},
            {"hash_rate_mbps": 2001},
            {"min_size_bytes": 100, "max_size_bytes": 10},
            {"include_extensions": ["jpg"]},
            {"include_extensions": ["."]},
            {"include_extensions": [".waytoolongextension"]},
            {"exclude_extensions": "not-a-list"},
            {"skip_empty": "yes"},
            {"max_files": 0},
            {"keep_mode": 1},
        ]
        for patch in invalid_patches:
            with self.assertRaises(ApiError, msg=str(patch)) as ctx:
                settings_module.validate_patch(patch)
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_skip_symlinks_forced_true(self):
        patch = settings_module.validate_patch({"skip_symlinks": False})
        self.assertTrue(patch["skip_symlinks"])

    def test_merge_keeps_other_values(self):
        merged = settings_module.merge_settings(default_settings(),
                                               {"keep_mode": "pinned",
                                                "pinned_path": "/volume1/photo"})
        self.assertEqual(merged["keep_mode"], "pinned")
        self.assertEqual(merged["hash_algo"], "sha256")


class SettingsPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-settings-")
        self.store = Store(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_persist_and_reload(self):
        manager = SettingsManager(self.store)
        manager.update({"keep_mode": "earliest", "concurrency": 4})
        reloaded = SettingsManager(self.store)
        current = reloaded.get()
        self.assertEqual(current["keep_mode"], "earliest")
        self.assertEqual(current["concurrency"], 4)
        self.assertEqual(current["hash_algo"], "sha256")

    def test_corrupt_settings_file_falls_back(self):
        with open(os.path.join(self.tmpdir, "settings.json"), "wb") as handle:
            handle.write(b"{not json")
        manager = SettingsManager(self.store)
        self.assertEqual(manager.get()["keep_mode"], "latest")
