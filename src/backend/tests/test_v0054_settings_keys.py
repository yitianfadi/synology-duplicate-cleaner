# -*- coding: utf-8 -*-
"""v0054 设置键一致性回归（download_subscribe_enabled 曾漏加后端白名单）。

背景：v0054 新增下载订阅，前端 readSettingsForm 会整体提交 settings 对象，
后端 validate_patch 只接受 DEFAULT_SETTINGS 里的键；漏加一个键会让「保存设置」
整单 400（未知的设置键: download_subscribe_enabled）。本文件把这类漂移钉死：

1) 每个默认键都必须有校验分支（默认值能被 validate_patch 接受）；
2) 前端会提交的键必须都在后端白名单或 clear_* 别名里；
3) 订阅总开关能存能读、类型校验严格、且随 GET /api/settings 回显。
"""

from __future__ import absolute_import

import copy
import os
import re
import shutil
import tempfile
import unittest

from tests.helpers import BACKEND_DIR  # noqa: F401

from dupcleaner import settings as settings_module
from dupcleaner.errors import ApiError
from dupcleaner.settings import SettingsManager, default_settings
from dupcleaner.store import Store

SRC_DIR = os.path.dirname(BACKEND_DIR)
WEB_DIR = os.path.join(SRC_DIR, "web")

SUBSCRIBE_KEY = "download_subscribe_enabled"


def _read(path):
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8", "replace")


def _frontend_settings_keys():
    """前端提交的设置键：app.js/js/*.js 里的 ``settings.<key> = ...``。"""
    paths = [os.path.join(WEB_DIR, "app.js")]
    js_dir = os.path.join(WEB_DIR, "js")
    if os.path.isdir(js_dir):
        paths.extend(os.path.join(js_dir, name)
                     for name in sorted(os.listdir(js_dir))
                     if name.endswith(".js"))
    keys = set()
    for path in paths:
        if not os.path.isfile(path):
            continue
        keys.update(re.findall(r"\bsettings\.([a-z0-9_]+)\s*=[^=]",
                               _read(path)))
    return keys


def _frontend_default_keys():
    """前端 js/constants.js 的 DEFAULT_SETTINGS 顶层键（缺文件时返回空集）。"""
    path = os.path.join(WEB_DIR, "js", "constants.js")
    if not os.path.isfile(path):
        return set()
    text = _read(path)
    start = text.find("DEFAULT_SETTINGS")
    brace = text.find("{", start) if start >= 0 else -1
    if brace < 0:
        return set()
    depth = 0
    end = len(text)
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    keys = set()
    depth = 0
    for line in text[brace + 1:end].splitlines():
        stripped = line.strip()
        depth += stripped.count("{") - stripped.count("}")
        if depth != 0:
            continue
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", stripped)
        if match:
            keys.add(match.group(1))
    return keys


class SettingsKeyConsistencyTest(unittest.TestCase):
    def test_every_default_key_validates(self):
        broken = []
        for key, value in sorted(default_settings().items()):
            try:
                settings_module.validate_patch({key: copy.deepcopy(value)})
            except ApiError as exc:
                broken.append("%s(%s)" % (key, exc))
        self.assertEqual(broken, [],
                         "以下默认设置键没有校验分支，保存设置会 400：%s" % broken)

    def test_full_default_patch_is_accepted(self):
        defaults = default_settings()
        patch = settings_module.validate_patch(copy.deepcopy(defaults))
        self.assertEqual(sorted(patch), sorted(defaults))

    def test_frontend_keys_are_known_to_backend(self):
        known = (set(settings_module.SETTINGS_KEYS)
                 | set(settings_module.CLEAR_SECRET_ALIASES))
        missing = sorted(_frontend_settings_keys() - known)
        self.assertEqual(
            missing, [],
            "前端提交了后端不认识的设置键（会返回未知的设置键 400）：%s" % missing)

    def test_frontend_defaults_are_known_to_backend(self):
        missing = sorted(_frontend_default_keys() - set(settings_module.SETTINGS_KEYS))
        self.assertEqual(missing, [])


class SubscribeEnabledSettingTest(unittest.TestCase):
    def test_default_off(self):
        self.assertIn(SUBSCRIBE_KEY, settings_module.SETTINGS_KEYS)
        self.assertIs(default_settings()[SUBSCRIBE_KEY], False)

    def test_accepts_bool(self):
        for value in (True, False):
            patch = settings_module.validate_patch({SUBSCRIBE_KEY: value})
            self.assertIs(patch[SUBSCRIBE_KEY], value)

    def test_rejects_non_bool(self):
        for value in (0, 1, "true", None, [], {}):
            with self.assertRaises(ApiError, msg=repr(value)) as ctx:
                settings_module.validate_patch({SUBSCRIBE_KEY: value})
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
            self.assertEqual(ctx.exception.details.get("key"), SUBSCRIBE_KEY)

    def test_public_settings_exposes_switch(self):
        public = settings_module.public_settings(default_settings())
        self.assertIn(SUBSCRIBE_KEY, public)
        self.assertIs(public[SUBSCRIBE_KEY], False)


class SubscribeEnabledPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-settings-subscribe-")
        self.store = Store(self.tmpdir)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_persist_and_reload(self):
        SettingsManager(self.store).update({SUBSCRIBE_KEY: True})
        self.assertIs(SettingsManager(self.store).get()[SUBSCRIBE_KEY], True)
        SettingsManager(self.store).update({SUBSCRIBE_KEY: False})
        self.assertIs(SettingsManager(self.store).get()[SUBSCRIBE_KEY], False)


if __name__ == "__main__":
    unittest.main()
