# -*- coding: utf-8 -*-
"""排除规则：文件名正则 / 通配符 / .ignore 文件子集（v0043）。

扫描器在枚举阶段对每个目录/文件调用 :meth:`ExcludeRules.match`，命中即跳过该
目录/文件（见 docs/REQUIREMENTS_V2_ASSESSMENT.md §2.8）。

规则优先级：正则 → 通配符 → .ignore（返回第一个命中的来源字符串）。

- 正则 ``name_regex``：``re.compile(pattern, re.IGNORECASE)``，对 basename 做
  ``search`` 匹配（不隐式锚定，需要整名匹配时自行写 ``^...$``）；单条长度
  ≤ 200；最多 50 条；编译失败 / 超长记 error 并跳过该条。
- 通配符 ``name_glob``：``fnmatch.fnmatchcase(name.lower(), pattern.lower())``，
  对 basename；最多 50 条（长度同样限制 ≤ 200，与正则保持一致）。
- ``.ignore``：gitignore 常用子集——``#`` 注释、空行、行尾未转义空格去除、
  ``!`` 否定（按加载顺序匹配，最后命中者胜）、``/`` 开头锚定到所在目录、
  结尾 ``/`` 只匹配目录、不含 ``/`` 匹配任意层级 basename、含 ``/`` 相对
  base_dir 匹配、``**`` 折叠成 ``*``（不做完整 gitignore 语义）；最多读取
  1000 行；规则只在自身 base_dir 之下生效（越界路径不命中）；大小写敏感。
  目录命中后其下文件同样视为命中（gitignore 惯例，扫描器跳过目录时也不会
  再枚举子项）。

纯标准库、Python 3.8+。
"""

from __future__ import absolute_import

import fnmatch
import io
import os
import re

# 与扫描器共享的硬上限（防止用户配置把枚举拖慢/拖爆内存）
MAX_REGEX_RULES = 50
MAX_GLOB_RULES = 50
MAX_PATTERN_LEN = 200
MAX_IGNORE_LINES = 1000

_SPLIT_RE = re.compile(r"[\\/]+")


def _safe_text(value):
    """把任意值转成适合放进 errors 的短字符串。"""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:  # pragma: no cover - 防御性兜底
        text = u""
    return text[:MAX_PATTERN_LEN]


def _pattern_list(value, source, errors):
    """把配置值规范化成 pattern 列表（None → []，单个字符串按 1 条）。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)):
        errors.append({
            "source": source,
            "pattern": _safe_text(value),
            "message": u"排除规则必须是字符串或字符串数组",
        })
        return []
    patterns = []
    for item in value:
        if isinstance(item, str):
            patterns.append(item)
        else:
            errors.append({
                "source": source,
                "pattern": _safe_text(item),
                "message": u"排除规则的元素必须是字符串",
            })
    return patterns


def _cap_patterns(patterns, limit, source, label, errors):
    """列表超上限时记一条汇总 error 并截断（多余的静默忽略）。"""
    if len(patterns) <= limit:
        return patterns
    errors.append({
        "source": source,
        "pattern": _safe_text(patterns[limit]),
        "message": u"%s规则最多 %d 条，已忽略后面的 %d 条"
                   % (label, limit, len(patterns) - limit),
    })
    return patterns[:limit]


def _strip_trailing_unescaped_spaces(line):
    """去掉行尾未转义的空格（gitignore：``foo\\ `` 的空格是字面量）。"""
    end = len(line)
    while end > 0 and line[end - 1] == " ":
        backslashes = 0
        index = end - 2
        while index >= 0 and line[index] == "\\":
            backslashes += 1
            index -= 1
        if backslashes % 2 == 1:
            break
        end -= 1
    return line[:end]


def _parse_ignore_line(raw):
    """解析一行 .ignore；返回规则 dict，注释/空行/非法行返回 None。"""
    line = raw.rstrip("\r\n")
    line = _strip_trailing_unescaped_spaces(line)
    if not line:
        return None
    if line.startswith("#"):
        return None
    negated = False
    # 转义的行首 # / ! 按字面量处理（"\#foo"、"\!foo"）。
    if line.startswith("\\#") or line.startswith("\\!"):
        line = line[1:]
    elif line.startswith("!"):
        negated = True
        line = line[1:]
    if not line:
        return None
    # 行内转义空格（"foo\ bar" → "foo bar"）。
    line = line.replace("\\ ", " ")
    # ** 折叠成 *（不做完整 gitignore 的 ** 语义）。
    while "**" in line:
        line = line.replace("**", "*")
    dir_only = line.endswith("/")
    if dir_only:
        line = line.rstrip("/")
        if not line:
            return None
    anchored = line.startswith("/")
    if anchored:
        line = line.lstrip("/")
    if not line:
        return None
    return {
        "pattern": line,
        "match": line,
        "anchored": anchored,
        "dir_only": dir_only,
        "has_slash": "/" in line,
        "negated": negated,
    }


def _relative_parts(path, base):
    """path 相对 base 的路径分量；不在 base 之下返回 None，等于 base 返回 []。"""
    path_abs = os.path.normpath(os.path.abspath(path))
    base_abs = os.path.normpath(os.path.abspath(base))
    path_key = os.path.normcase(path_abs)
    base_key = os.path.normcase(base_abs)
    if path_key == base_key:
        return []
    if not base_key.endswith(os.sep):
        base_key = base_key + os.sep
        base_for_slice = base_abs + os.sep
    else:
        base_for_slice = base_abs
    if not path_key.startswith(base_key):
        return None
    rel = path_abs[len(base_for_slice):]
    return [part for part in _SPLIT_RE.split(rel) if part]


def _rule_matches_parts(rule, parts, is_dir):
    """按 gitignore 子集语义判断单条规则是否命中给定路径分量。

    祖先目录命中也算命中（目录被排除时其内容同样被排除；扫描器跳过目录后
    不会枚举子项，这里只是让直接调用 match() 时语义也正确）。
    """
    pattern = rule["match"]
    anchored = rule["anchored"]
    dir_only = rule["dir_only"]
    has_slash = rule["has_slash"]
    last_index = len(parts) - 1
    for index, candidate in enumerate(parts):
        candidate_is_dir = (index < last_index) or bool(is_dir)
        if dir_only and not candidate_is_dir:
            continue
        if has_slash or anchored:
            relative = "/".join(parts[:index + 1])
            if fnmatch.fnmatchcase(relative, pattern):
                return True
        elif fnmatch.fnmatchcase(candidate, pattern):
            return True
    return False


class ExcludeRules(object):
    """排除规则集合：正则 + 通配符 + 多个目录的 .ignore 规则。"""

    def __init__(self, name_regex=None, name_glob=None, use_ignore_files=True):
        self._errors = []
        self._regex_rules = []
        self._glob_rules = []
        self._ignore_rules = []
        # base 规范化键 → 该目录加载的规则列表；仅索引祖先目录，避免每条路径
        # 遍历全量规则（NAS 上目录多、.ignore 多时退化）。
        self._ignore_bases = {}
        self._ignore_seq = 0
        self._hits = {"regex": 0, "glob": 0, "ignore": 0}
        self._use_ignore_files = bool(use_ignore_files)

        patterns = _pattern_list(name_regex, "regex", self._errors)
        patterns = _cap_patterns(patterns, MAX_REGEX_RULES, "regex",
                                 u"正则", self._errors)
        for pattern in patterns:
            if len(pattern) > MAX_PATTERN_LEN:
                self._errors.append({
                    "source": "regex",
                    "pattern": _safe_text(pattern),
                    "message": u"正则长度超过 %d 字符" % MAX_PATTERN_LEN,
                })
                continue
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                self._errors.append({
                    "source": "regex",
                    "pattern": _safe_text(pattern),
                    "message": u"正则编译失败：%s" % exc,
                })
                continue
            self._regex_rules.append(compiled)

        patterns = _pattern_list(name_glob, "glob", self._errors)
        patterns = _cap_patterns(patterns, MAX_GLOB_RULES, "glob",
                                 u"通配符", self._errors)
        for pattern in patterns:
            if len(pattern) > MAX_PATTERN_LEN:
                self._errors.append({
                    "source": "glob",
                    "pattern": _safe_text(pattern),
                    "message": u"通配符长度超过 %d 字符" % MAX_PATTERN_LEN,
                })
                continue
            self._glob_rules.append(pattern.lower())

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------
    @property
    def errors(self):
        """解析/加载阶段的问题列表（元素：source/pattern/message）。"""
        return [dict(item) for item in self._errors]

    def reset_stats(self):
        """清零命中计数（rules/errors 不受影响）。"""
        self._hits = {"regex": 0, "glob": 0, "ignore": 0}

    def stats(self):
        """返回 {"regex": int, "glob": int, "ignore": int, "rules": int, "errors": int}。"""
        return {
            "regex": self._hits["regex"],
            "glob": self._hits["glob"],
            "ignore": self._hits["ignore"],
            "rules": (len(self._regex_rules) + len(self._glob_rules)
                      + len(self._ignore_rules)),
            "errors": len(self._errors),
        }

    # ------------------------------------------------------------------
    # .ignore
    # ------------------------------------------------------------------
    def load_ignore(self, directory):
        """读取 <directory>/.ignore，返回本次加载的规则条数。

        - 文件不存在：返回 0，不记 error；
        - 读取失败：记 error 并返回已解析的条数（不抛异常）；
        - 最多读取 MAX_IGNORE_LINES 行，多余行忽略；
        - use_ignore_files=False 时直接返回 0（不读文件）。
        """
        if not self._use_ignore_files:
            return 0
        if not isinstance(directory, str) or not directory.strip():
            self._errors.append({
                "source": "ignore",
                "pattern": _safe_text(directory),
                "message": u".ignore 所在目录参数非法",
            })
            return 0
        base = os.path.normpath(os.path.abspath(directory))
        path = os.path.join(base, ".ignore")
        try:
            handle = io.open(path, "r", encoding="utf-8-sig", errors="replace")
        except FileNotFoundError:
            return 0
        except OSError as exc:
            self._errors.append({
                "source": "ignore",
                "pattern": path[:MAX_PATTERN_LEN],
                "message": u"读取 .ignore 文件失败：%s" % exc,
            })
            return 0
        lines = []
        try:
            with handle:
                for line in handle:
                    lines.append(line)
                    if len(lines) >= MAX_IGNORE_LINES:
                        break
        except OSError as exc:
            # 已读到的行仍然解析（解析失败告警但继续）。
            self._errors.append({
                "source": "ignore",
                "pattern": path[:MAX_PATTERN_LEN],
                "message": u"读取 .ignore 文件失败：%s" % exc,
            })
        loaded = 0
        base_key = os.path.normcase(base)
        for raw in lines:
            rule = _parse_ignore_line(raw)
            if rule is None:
                continue
            rule["base"] = base
            rule["seq"] = self._ignore_seq
            self._ignore_seq += 1
            self._ignore_rules.append(rule)
            self._ignore_bases.setdefault(base_key, []).append(rule)
            loaded += 1
        return loaded

    # ------------------------------------------------------------------
    # 匹配
    # ------------------------------------------------------------------
    def match(self, path, name=None, is_dir=False):
        """返回命中来源（"regex" / "glob" / "ignore"），未命中返回 None。

        name 缺省取 ``os.path.basename(os.path.normpath(path))``；正则与通配符
        只看 name，.ignore 规则还要结合 path 判断作用域。
        """
        if not isinstance(path, str) or not path:
            return None
        if name is None:
            name = os.path.basename(os.path.normpath(path))
        elif not isinstance(name, str):
            name = u""
        if name:
            for compiled in self._regex_rules:
                if compiled.search(name):
                    return self._hit("regex")
            lower_name = name.lower()
            for pattern in self._glob_rules:
                if fnmatch.fnmatchcase(lower_name, pattern):
                    return self._hit("glob")
        if self._use_ignore_files and self._ignore_rules:
            if self._match_ignore(path, is_dir):
                return self._hit("ignore")
        return None

    def _hit(self, source):
        self._hits[source] += 1
        return source

    def _match_ignore(self, path, is_dir):
        """按加载顺序匹配“祖先目录”的 .ignore 规则：最后一条命中的规则决定结果。

        只取 path 的祖先目录对应的规则（不再遍历全量规则），未加载 .ignore 的
        目录自然没有规则；越界路径不会命中任何规则。
        """
        try:
            current = os.path.normcase(os.path.dirname(
                os.path.normpath(os.path.abspath(path))))
        except (OSError, ValueError):  # pragma: no cover - 防御性兜底
            return False
        candidates = []
        while True:
            rules = self._ignore_bases.get(current)
            if rules:
                candidates.extend(rules)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        if not candidates:
            return False
        candidates.sort(key=lambda rule: rule["seq"])
        cached = {}
        excluded = None
        for rule in candidates:
            base = rule["base"]
            if base in cached:
                parts = cached[base]
            else:
                parts = _relative_parts(path, base)
                cached[base] = parts
            if not parts:
                # 越界路径或规则目录自身：本条规则不适用
                continue
            if _rule_matches_parts(rule, parts, is_dir):
                excluded = not rule["negated"]
        return bool(excluded)

