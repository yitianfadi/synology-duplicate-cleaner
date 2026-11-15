#!/usr/bin/env python3
# -*- coding: utf-8 -*-
u"""设置键一致性审计（一次性排查脚本，可重复运行）。

方向 1（致命）：前端 readSettingsForm 会 ``settings.<key> = ...`` 提交给
    PUT /api/settings；后端 validate_patch 只接受 DEFAULT_SETTINGS 里的键，
    多一个键整个请求就 400。本脚本把前端赋值的键与后端白名单做差集。
方向 2：后端代码里 ``settings.get("<key>"`` / ``settings["<key>"]`` 读取的键若
    不在 DEFAULT_SETTINGS，永远是 None（静默失效）。
方向 3：DEFAULT_SETTINGS 里从未被读取的键（死设置，仅提示）。
方向 4：前端 DEFAULT_SETTINGS（js/constants.js）与后端 DEFAULT_SETTINGS 的键差异。
"""
from __future__ import absolute_import, print_function, unicode_literals

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "backend"))

from dupcleaner import settings as settings_module  # noqa: E402


def read(path):
    with io.open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def frontend_sent_keys():
    u"""前端会提交的设置键：app.js 里 settings.<key> = 赋值 + 模块化 js 里的同名写法。"""
    keys = set()
    targets = [os.path.join(ROOT, "src", "web", "app.js")]
    js_dir = os.path.join(ROOT, "src", "web", "js")
    for name in sorted(os.listdir(js_dir)):
        if name.endswith(".js"):
            targets.append(os.path.join(js_dir, name))
    pattern = re.compile(r"\bsettings\.([A-Za-z_][A-Za-z0-9_]*)\s*=[^=]")
    for path in targets:
        text = read(path)
        for match in pattern.finditer(text):
            keys.add(match.group(1))
    return keys, targets


def frontend_default_keys():
    u"""前端 DEFAULT_SETTINGS 对象里的键（js/constants.js）。"""
    text = read(os.path.join(ROOT, "src", "web", "js", "constants.js"))
    start = text.find("DEFAULT_SETTINGS")
    if start < 0:
        return set()
    brace = text.find("{", start)
    if brace < 0:
        return set()
    depth = 0
    end = brace
    for index in range(brace, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    body = text[brace + 1:end]
    # 只取顶层键：跳过嵌套对象体
    keys = set()
    depth = 0
    for line in body.splitlines():
        stripped = line.strip()
        depth += stripped.count("{") - stripped.count("}")
        if depth != 0:
            continue
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", stripped)
        if match:
            keys.add(match.group(1))
    return keys


def backend_read_keys():
    u"""后端从 settings 字典读取的键。"""
    keys = {}
    pattern = re.compile(
        r"""(?:settings|self\.settings|state\.settings|opts\.settings)\s*"""
        r"""(?:\.get\(\s*["']([a-z0-9_]+)["']|\[\s*["']([a-z0-9_]+)["']\s*\])""")
    for base in ("src/backend/dupcleaner", "src/backend/tests"):
        directory = os.path.join(ROOT, *base.split("/"))
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(directory, name)
            text = read(path)
            for match in pattern.finditer(text):
                key = match.group(1) or match.group(2)
                keys.setdefault(key, name)
    return keys


def mentioned_keys():
    u"""后端源码里以字符串字面量出现过的键（settings.py 之外）。

    用于判断某个默认设置是否真的被引用；比 ``settings.get(...)`` 正则宽松，
    能覆盖 options/cfg/self._settings 等别名与 ``x["key"]`` 写法。
    """
    mentions = {}
    directory = os.path.join(ROOT, "src", "backend", "dupcleaner")
    literal = re.compile(r"""["']([a-z0-9_]{2,40})["']""")
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".py") or name == "settings.py":
            continue
        for key in literal.findall(read(os.path.join(directory, name))):
            mentions.setdefault(key, name)
    return mentions


def main():
    backend_keys = set(settings_module.SETTINGS_KEYS)
    aliases = set(settings_module.CLEAR_SECRET_ALIASES)
    sent, targets = frontend_sent_keys()
    defaults_fe = frontend_default_keys()
    reads = backend_read_keys()
    mentions = mentioned_keys()

    print(u"后端 DEFAULT_SETTINGS：%d 个键" % len(backend_keys))
    print(u"前端提交键（settings.<key> =）：%d 个（含 %d 个 clear 别名）"
          % (len(sent), len(sent & aliases)))
    print(u"前端 DEFAULT_SETTINGS：%d 个键" % len(defaults_fe))
    print(u"后端读取键：%d 个" % len(reads))
    print("")

    # clear_* 别名由 prepare_settings_patch 在校验前剥离，不算未知键。
    bad = sorted(key for key in sent
                 if key not in backend_keys and key not in aliases)
    print(u"[1] 前端会提交、后端不认识的键（会 400）：%d" % len(bad))
    for key in bad:
        print(u"    - %s" % key)
    print("")

    unknown_reads = sorted(key for key in reads if key not in backend_keys
                           and not key.endswith("_set")
                           and re.match(r"^[a-z0-9_]+$", key))
    print(u"[2] 后端读取、但不在 DEFAULT_SETTINGS 的键（静默 None）：%d"
          % len(unknown_reads))
    for key in unknown_reads:
        print(u"    - %s (first seen in %s)" % (key, reads[key]))
    print("")

    fe_only = sorted(defaults_fe - backend_keys)
    be_only = sorted(backend_keys - defaults_fe)
    print(u"[3] 前端 DEFAULT_SETTINGS 有、后端没有：%d" % len(fe_only))
    for key in fe_only:
        print(u"    - %s" % key)
    print(u"    后端有、前端 DEFAULT_SETTINGS 没有（不影响，仅提示）：%d" % len(be_only))
    for key in be_only:
        print(u"    - %s" % key)
    print("")

    never_read = sorted(key for key in backend_keys if key not in mentions)
    print(u"[4] DEFAULT_SETTINGS 里后端从未引用过的键（死设置，仅提示）：%d"
          % len(never_read))
    for key in never_read:
        print(u"    - %s" % key)
    print("")
    return 1 if (bad or unknown_reads) else 0


if __name__ == "__main__":
    sys.exit(main())
