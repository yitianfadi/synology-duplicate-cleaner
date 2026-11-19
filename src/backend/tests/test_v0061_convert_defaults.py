# -*- coding: utf-8 -*-
"""v0061 转换默认值迁移与后缀筛选（docs/CONVERT_CONTRACT.md §6/§8）。

覆盖三件事：
1) 旧默认 `convert_overwrite: rename` 在升级后被一次性迁到 `trash`，并写 DB 标记；
   之后用户主动选回 rename 不会被再次改写。
2) `convert_source_extensions` 的校验（只接受受支持后缀、去重、空数组 = 全部）。
3) `convert_delete_source` 默认 `true`，`convert_keep_source` 降级为兼容键。
"""

from __future__ import absolute_import

import os
import shutil
import tempfile
import unittest

from tests.helpers import BACKEND_DIR  # noqa: F401

from dupcleaner import config
from dupcleaner import settings as settings_module
from dupcleaner.errors import ApiError
from dupcleaner.settings import SettingsManager, default_settings
from dupcleaner.store import Store


class ConvertDefaultsMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-cv61-")
        self.store = Store(os.path.join(self.tmpdir, "data"))

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed(self, **overrides):
        settings = default_settings()
        settings.update(overrides)
        self.store.save_settings(settings)
        return settings

    def test_defaults(self):
        settings = default_settings()
        self.assertEqual(settings["convert_overwrite"],
                         config.CONVERT_OVERWRITE_DEFAULT)
        self.assertEqual(settings["convert_overwrite"], "trash")
        self.assertTrue(settings["convert_delete_source"])
        self.assertEqual(settings["convert_source_extensions"], [])

    def test_legacy_rename_migrated_to_trash(self):
        self._seed(convert_overwrite="rename")
        manager = SettingsManager(self.store)
        self.assertEqual(manager.get()["convert_overwrite"], "trash")
        # 标记已落地：再加载不会重复迁移
        self.assertEqual(
            str(self.store.db.schema_meta_get(
                SettingsManager.MIGRATION_0061_KEY)), "1")
        again = SettingsManager(self.store)
        self.assertEqual(again.get()["convert_overwrite"], "trash")

    def test_user_choice_after_migration_is_kept(self):
        self._seed(convert_overwrite="skip")
        manager = SettingsManager(self.store)
        self.assertEqual(manager.get()["convert_overwrite"], "skip")
        # 迁移完成后用户主动选回 rename 应保留
        manager.update({"convert_overwrite": "rename"})
        reloaded = SettingsManager(self.store)
        self.assertEqual(reloaded.get()["convert_overwrite"], "rename")

    def test_keep_source_is_compat_key(self):
        self._seed()
        manager = SettingsManager(self.store)
        # 兼容键仍可保存，但不再是删源的判定依据
        manager.update({"convert_keep_source": False})
        self.assertFalse(manager.get()["convert_keep_source"])
        self.assertTrue(manager.get()["convert_delete_source"])


class ConvertSourceExtensionsTest(unittest.TestCase):
    def test_accepts_supported_list(self):
        value = settings_module.validate_patch(
            {"convert_source_extensions": [".mkv", "mp4", ".flac"]})
        self.assertEqual(value["convert_source_extensions"],
                         [".mkv", ".mp4", ".flac"])

    def test_empty_list_means_all(self):
        value = settings_module.validate_patch(
            {"convert_source_extensions": []})
        self.assertEqual(value["convert_source_extensions"], [])

    def test_rejects_unknown_extension(self):
        with self.assertRaises(ApiError):
            settings_module.validate_patch(
                {"convert_source_extensions": [".exe"]})

    def test_rejects_non_list(self):
        with self.assertRaises(ApiError):
            settings_module.validate_patch(
                {"convert_source_extensions": ".mkv"})

    def test_dedupes(self):
        value = settings_module.validate_patch(
            {"convert_source_extensions": [".mkv", ".MKV"]})
        self.assertEqual(value["convert_source_extensions"], [".mkv"])


if __name__ == "__main__":
    unittest.main()
