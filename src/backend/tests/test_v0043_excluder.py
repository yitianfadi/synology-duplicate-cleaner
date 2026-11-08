# -*- coding: utf-8 -*-
"""v0043 排除规则模块（dupcleaner.excluder）单元测试。

覆盖：正则命中/不命中/非法/超长/超条数，通配符命中（含目录名）、大小写，
.ignore 基本规则、目录结尾、锚定、否定、注释、无文件、读取失败、作用域、
越界、统计与 errors 结构。
"""

from __future__ import absolute_import

import io
import os
import shutil
import tempfile
import unittest

from dupcleaner.excluder import ExcludeRules


class ExcludeRulesTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-v0043-excluder-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmpdir, *parts)

    def make_dir(self, *parts):
        path = self.path(*parts)
        os.makedirs(path)
        return path

    def write_ignore(self, directory, text):
        path = os.path.join(directory, ".ignore")
        with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return path


class ExcludeRulesRegexTest(ExcludeRulesTestBase):
    def test_regex_matches_basename_ignoring_case(self):
        rules = ExcludeRules(name_regex=[r"^thumb.*\.jpg$"])
        self.assertEqual(rules.match(self.path("payload", "Thumb-01.JPG")),
                         "regex")
        self.assertIsNone(rules.match(self.path("payload", "logo.png")))
        self.assertEqual(rules.stats()["regex"], 1)
        self.assertEqual(rules.stats()["rules"], 1)

    def test_regex_only_looks_at_basename(self):
        rules = ExcludeRules(name_regex=[r"cache"])
        # search 语义：name 中包含 cache 即命中
        self.assertEqual(rules.match("/data/my-cache.bin"), "regex")
        # 正则只看 basename，父目录名不参与匹配
        self.assertIsNone(rules.match("/data/cache-dir/keep.bin"))

    def test_invalid_regex_records_error_and_is_skipped(self):
        rules = ExcludeRules(name_regex=["(", r"^secret\.txt$"])
        self.assertEqual(len(rules.errors), 1)
        error = rules.errors[0]
        self.assertEqual(error["source"], "regex")
        self.assertEqual(error["pattern"], "(")
        self.assertIn(u"编译", error["message"])
        # 非法规则被忽略，合法规则继续生效
        self.assertEqual(rules.match(self.path("x", "secret.txt")), "regex")

    def test_regex_longer_than_200_chars_is_rejected(self):
        too_long = "a" * 201
        rules = ExcludeRules(name_regex=[too_long])
        self.assertEqual(len(rules.errors), 1)
        self.assertEqual(rules.errors[0]["source"], "regex")
        self.assertIn("200", rules.errors[0]["message"])
        self.assertIsNone(rules.match(self.path("x", too_long)))
        # 恰好 200 字符允许
        allowed = ExcludeRules(name_regex=["b" * 200])
        self.assertEqual(allowed.errors, [])
        self.assertEqual(allowed.stats()["rules"], 1)

    def test_regex_list_capped_at_50(self):
        patterns = ["^f%02d$" % index for index in range(50)] + ["^overflow$"]
        rules = ExcludeRules(name_regex=patterns)
        self.assertEqual(len(rules.errors), 1)
        self.assertEqual(rules.errors[0]["source"], "regex")
        self.assertIn("50", rules.errors[0]["message"])
        self.assertEqual(rules.stats()["rules"], 50)
        self.assertEqual(rules.match(self.path("x", "f00")), "regex")
        self.assertIsNone(rules.match(self.path("x", "overflow")))

    def test_empty_config_is_noop(self):
        rules = ExcludeRules()
        self.assertIsNone(rules.match(self.path("x", "anything.txt")))
        self.assertEqual(rules.errors, [])
        self.assertEqual(rules.stats(),
                         {"regex": 0, "glob": 0, "ignore": 0,
                          "rules": 0, "errors": 0})


class ExcludeRulesGlobTest(ExcludeRulesTestBase):
    def test_glob_matches_case_insensitively(self):
        rules = ExcludeRules(name_glob=["*.tmp", "Thumbs.DB"])
        self.assertEqual(rules.match(self.path("x", "A.TMP")), "glob")
        self.assertEqual(rules.match(self.path("x", "thumbs.db")), "glob")
        self.assertIsNone(rules.match(self.path("x", "a.tmp.bak")))
        self.assertEqual(rules.stats()["glob"], 2)
        self.assertEqual(rules.stats()["rules"], 2)

    def test_glob_question_mark(self):
        rules = ExcludeRules(name_glob=["img?.png"])
        self.assertEqual(rules.match(self.path("x", "img1.png")), "glob")
        self.assertIsNone(rules.match(self.path("x", "img12.png")))

    def test_glob_matches_directory_name(self):
        rules = ExcludeRules(name_glob=["build", "node_modules"])
        self.assertEqual(rules.match(self.path("repo", "build"), is_dir=True),
                         "glob")
        # 通配符只看名字，不区分文件/目录
        self.assertEqual(rules.match(self.path("repo", "build")), "glob")
        # 只比 basename：目录下的文件本身不命中（扫描器命中目录即整棵跳过）
        self.assertIsNone(rules.match(self.path("repo", "build", "out.txt")))

    def test_glob_list_capped_at_50(self):
        patterns = ["f%02d" % index for index in range(50)] + ["overflow"]
        rules = ExcludeRules(name_glob=patterns)
        self.assertEqual(len(rules.errors), 1)
        self.assertEqual(rules.errors[0]["source"], "glob")
        self.assertEqual(rules.stats()["rules"], 50)
        self.assertEqual(rules.match(self.path("x", "f00")), "glob")
        self.assertIsNone(rules.match(self.path("x", "overflow")))

    def test_regex_takes_precedence_over_glob(self):
        rules = ExcludeRules(name_regex=[r"\.tmp$"], name_glob=["*tmp*"])
        self.assertEqual(rules.match(self.path("x", "a.tmp")), "regex")
        self.assertEqual(rules.stats()["regex"], 1)
        self.assertEqual(rules.stats()["glob"], 0)


class ExcludeRulesIgnoreTest(ExcludeRulesTestBase):
    def setUp(self):
        super(ExcludeRulesIgnoreTest, self).setUp()
        self.root = self.make_dir("root")

    def test_missing_ignore_file_returns_zero(self):
        rules = ExcludeRules()
        self.assertEqual(rules.load_ignore(self.root), 0)
        self.assertEqual(rules.errors, [])
        self.assertEqual(rules.stats()["rules"], 0)
        self.assertIsNone(rules.match(self.path("root", "a.log")))

    def test_basic_pattern_comments_and_blank_lines(self):
        self.write_ignore(self.root, u"# 注释行\n\n*.log\n   \n# 末尾注释\n")
        rules = ExcludeRules()
        self.assertEqual(rules.load_ignore(self.root), 1)
        self.assertEqual(rules.match(self.path("root", "a.log")), "ignore")
        self.assertIsNone(rules.match(self.path("root", "a.txt")))
        self.assertEqual(rules.stats()["ignore"], 1)
        self.assertEqual(rules.stats()["rules"], 1)

    def test_ignore_is_case_sensitive(self):
        self.write_ignore(self.root, u"*.LOG\n")
        rules = ExcludeRules()
        rules.load_ignore(self.root)
        self.assertEqual(rules.match(self.path("root", "a.LOG")), "ignore")
        self.assertIsNone(rules.match(self.path("root", "a.log")))

    def test_dir_only_rule_matches_directories(self):
        self.write_ignore(self.root, u"cache/\n")
        rules = ExcludeRules()
        self.assertEqual(rules.load_ignore(self.root), 1)
        self.assertEqual(rules.match(self.path("root", "cache"), is_dir=True),
                         "ignore")
        self.assertIsNone(rules.match(self.path("root", "cache"), is_dir=False))
        # 目录已命中时其下内容同样被排除（祖先目录命中，gitignore 惯例）
        self.assertEqual(rules.match(self.path("root", "cache", "x.txt")),
                         "ignore")

    def test_anchored_rule_matches_base_level_only(self):
        self.write_ignore(self.root, u"/only-top.txt\n")
        rules = ExcludeRules()
        self.assertEqual(rules.load_ignore(self.root), 1)
        self.assertEqual(rules.match(self.path("root", "only-top.txt")),
                         "ignore")
        self.assertIsNone(rules.match(self.path("root", "sub", "only-top.txt")))

    def test_unanchored_pattern_matches_any_depth(self):
        self.write_ignore(self.root, u"thumbs.db\n")
        rules = ExcludeRules()
        rules.load_ignore(self.root)
        self.assertEqual(rules.match(self.path("root", "thumbs.db")), "ignore")
        self.assertEqual(
            rules.match(self.path("root", "a", "b", "thumbs.db")), "ignore")

    def test_pattern_with_slash_is_relative_to_base(self):
        self.write_ignore(self.root, u"sub/*.log\n")
        rules = ExcludeRules()
        rules.load_ignore(self.root)
        self.assertEqual(rules.match(self.path("root", "sub", "a.log")),
                         "ignore")
        self.assertIsNone(rules.match(self.path("root", "other", "a.log")))
        # fnmatch 的 * 可跨 /（明确不做完整 gitignore 语义）
        self.assertEqual(
            rules.match(self.path("root", "sub", "deep", "a.log")), "ignore")

    def test_negation_last_match_wins(self):
        self.write_ignore(self.root, u"*.log\n!keep.log\n")
        rules = ExcludeRules()
        self.assertEqual(rules.load_ignore(self.root), 2)
        self.assertEqual(rules.match(self.path("root", "other.log")), "ignore")
        self.assertIsNone(rules.match(self.path("root", "keep.log")))

    def test_negation_order_matters(self):
        self.write_ignore(self.root, u"!keep.log\n*.log\n")
        rules = ExcludeRules()
        rules.load_ignore(self.root)
        self.assertEqual(rules.match(self.path("root", "keep.log")), "ignore")

    def test_subdir_ignore_scoped_to_subdir(self):
        sub = self.make_dir("root", "sub")
        self.write_ignore(self.root, u"toponly.log\n")
        self.write_ignore(sub, u"subonly.log\n")
        rules = ExcludeRules()
        self.assertEqual(rules.load_ignore(self.root), 1)
        self.assertEqual(rules.load_ignore(sub), 1)
        # 父目录的规则对子目录生效
        self.assertEqual(
            rules.match(self.path("root", "sub", "toponly.log")), "ignore")
        # 子目录的规则在子目录（含更深层）生效
        self.assertEqual(
            rules.match(self.path("root", "sub", "deep", "subonly.log")),
            "ignore")
        # 子目录的规则不反向影响父目录
        self.assertIsNone(rules.match(self.path("root", "subonly.log")))

    def test_path_outside_base_dir_not_matched(self):
        other = self.make_dir("other")
        self.write_ignore(self.root, u"*.log\n")
        rules = ExcludeRules()
        rules.load_ignore(self.root)
        self.assertEqual(rules.match(self.path("root", "a.log")), "ignore")
        self.assertIsNone(rules.match(self.path("other", "a.log")))

    def test_read_failure_recorded_without_raising(self):
        bad = self.make_dir("bad")
        # 把 .ignore 建成目录：打开会抛 OSError（非 FileNotFoundError）
        os.makedirs(os.path.join(bad, ".ignore"))
        rules = ExcludeRules()
        self.assertEqual(rules.load_ignore(bad), 0)
        self.assertEqual(len(rules.errors), 1)
        error = rules.errors[0]
        self.assertEqual(error["source"], "ignore")
        self.assertIn(".ignore", error["pattern"])
        self.assertTrue(error["message"])

    def test_use_ignore_files_false_disables_reading(self):
        self.write_ignore(self.root, u"*.log\n")
        rules = ExcludeRules(use_ignore_files=False)
        self.assertEqual(rules.load_ignore(self.root), 0)
        self.assertIsNone(rules.match(self.path("root", "a.log")))
        self.assertEqual(rules.stats()["rules"], 0)

    def test_max_1000_lines(self):
        lines = ["f%04d.log" % index for index in range(1005)]
        self.write_ignore(self.root, u"\n".join(lines) + u"\n")
        rules = ExcludeRules()
        self.assertEqual(rules.load_ignore(self.root), 1000)
        self.assertEqual(rules.match(self.path("root", "f0999.log")),
                         "ignore")
        # 第 1001 行起不再读取
        self.assertIsNone(rules.match(self.path("root", "f1004.log")))

    def test_trailing_space_trim_and_escaped_chars(self):
        # 行尾未转义空格被去除；"\#"/"\ " 按字面量解析
        self.write_ignore(self.root, u"\\#literal.txt\nfoo\\ bar.txt\nbaz   \n")
        rules = ExcludeRules()
        self.assertEqual(rules.load_ignore(self.root), 3)
        self.assertEqual(rules.match(self.path("root", "#literal.txt")),
                         "ignore")
        self.assertEqual(rules.match(self.path("root", "foo bar.txt")),
                         "ignore")
        self.assertEqual(rules.match(self.path("root", "baz")), "ignore")
        self.assertIsNone(rules.match(self.path("root", "#literal")))


class ExcludeRulesStatsTest(ExcludeRulesTestBase):
    def test_stats_and_reset(self):
        root = self.make_dir("root")
        self.write_ignore(root, u"*.log\n")
        rules = ExcludeRules(name_regex=[r"^secret$"], name_glob=["*.tmp"])
        rules.load_ignore(root)
        self.assertEqual(rules.stats(),
                         {"regex": 0, "glob": 0, "ignore": 0,
                          "rules": 3, "errors": 0})
        self.assertEqual(rules.match("/x/secret"), "regex")
        self.assertEqual(rules.match("/x/a.tmp"), "glob")
        self.assertEqual(rules.match(self.path("root", "a.log")), "ignore")
        self.assertIsNone(rules.match("/x/other.bin"))
        self.assertEqual(rules.stats(),
                         {"regex": 1, "glob": 1, "ignore": 1,
                          "rules": 3, "errors": 0})
        rules.reset_stats()
        self.assertEqual(rules.stats(),
                         {"regex": 0, "glob": 0, "ignore": 0,
                          "rules": 3, "errors": 0})

    def test_error_entry_shape(self):
        rules = ExcludeRules(name_regex=["("])
        self.assertEqual(len(rules.errors), 1)
        self.assertEqual(sorted(rules.errors[0].keys()),
                         ["message", "pattern", "source"])
        self.assertIsInstance(rules.errors[0]["message"], str)
        self.assertIsInstance(rules.errors[0]["pattern"], str)
        # errors 返回副本，外部修改不影响内部状态
        rules.errors[0]["message"] = u"changed"
        self.assertNotEqual(rules.errors[0]["message"], u"changed")

    def test_name_argument_overrides_basename(self):
        rules = ExcludeRules(name_glob=["*.tmp"])
        self.assertEqual(rules.match("/x/anything.bin", name="A.TMP"), "glob")
        self.assertIsNone(rules.match("/x/anything.tmp", name="keep.bin"))

    def test_invalid_inputs_are_ignored(self):
        rules = ExcludeRules(name_regex=123, name_glob=[1, "*.tmp"])
        self.assertEqual(len(rules.errors), 2)
        self.assertEqual(sorted(error["source"] for error in rules.errors),
                         ["glob", "regex"])
        self.assertEqual(rules.match("/x/a.tmp"), "glob")
        self.assertIsNone(rules.match(""))
        self.assertIsNone(rules.match(None))


if __name__ == "__main__":
    unittest.main()
