# -*- coding: utf-8 -*-
"""v0046：计划扫描（规则解析 / 到点触发 / 负载与忙碌跳过 / 字幕批次通知 watcher）测试。"""

import os
import time
import unittest

from tests.helpers import Harness

from dupcleaner import config
from dupcleaner.scheduler import (ScheduleRuleError, SchedulerService,
                                  next_run_epoch, parse_rule, parse_rules)


class FakeScanner(object):
    def __init__(self, busy=0):
        self.busy = busy
        self.calls = []
        self.scan_id = "scan-20260101-000000-abcdef"

    def active_stats(self):
        return {"scans": self.busy}

    def start_scan(self, roots, excludes, options, user):
        self.calls.append({"roots": list(roots), "excludes": excludes,
                           "options": options, "user": user})
        return self.scan_id


class FakeNotifier(object):
    def __init__(self):
        self.groups = []

    def subtitle_batch_finished(self, group):
        self.groups.append(dict(group))
        return True

    def scan_finished(self, snapshot):
        return True

    def deletion_finished(self, snapshot):
        return True


class FakeSubtitles(object):
    def __init__(self, groups):
        self.groups = groups

    def batch_group_overview(self, active_limit=10, completed_limit=3):
        return {"active": [], "completed": list(self.groups)}


class FixedClock(object):
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class RuleParseTest(unittest.TestCase):
    def test_daily(self):
        rule = parse_rule("daily 03:30")
        self.assertEqual(rule["kind"], "daily")
        self.assertEqual(rule["minute_of_day"], 210)

    def test_weekly(self):
        rule = parse_rule("weekly mon,wed,fri 04:05")
        self.assertEqual(rule["kind"], "weekly")
        self.assertEqual(rule["weekdays"], [0, 2, 4])

    def test_interval(self):
        rule = parse_rule("interval 360")
        self.assertEqual(rule["kind"], "interval")
        self.assertEqual(rule["interval_sec"], 360 * 60)

    def test_invalid(self):
        for raw in ("", "daily 24:00", "weekly xyz 03:00",
                    "interval 1", "cron * * * * *", None):
            with self.assertRaises(ScheduleRuleError):
                parse_rule(raw)

    def test_parse_rules_skips_invalid(self):
        parsed = parse_rules(["daily 03:00", "bad", "interval 60"])
        self.assertEqual(len(parsed), 2)


class NextRunTest(unittest.TestCase):
    def test_daily_next(self):
        # 2026-01-01 10:00 本地时间
        base = time.mktime((2026, 1, 1, 10, 0, 0, 0, 0, -1))
        rule = parse_rule("daily 03:30")
        nxt = next_run_epoch(rule, base)
        self.assertGreater(nxt, base)
        local = time.localtime(nxt)
        self.assertEqual((local.tm_hour, local.tm_min), (3, 30))
        self.assertEqual(local.tm_mday, 2)

    def test_daily_same_day(self):
        base = time.mktime((2026, 1, 1, 1, 0, 0, 0, 0, -1))
        rule = parse_rule("daily 03:30")
        nxt = next_run_epoch(rule, base)
        self.assertEqual(time.localtime(nxt).tm_mday, 1)

    def test_interval_next(self):
        rule = parse_rule("interval 30")
        self.assertEqual(next_run_epoch(rule, 1000), 1000 + 1800)

    def test_weekly_next_matches_weekday(self):
        base = time.mktime((2026, 1, 1, 10, 0, 0, 0, 0, -1))  # 周四
        rule = parse_rule("weekly mon 03:30")
        nxt = next_run_epoch(rule, base)
        self.assertEqual(time.localtime(nxt).tm_wday, 0)
        self.assertEqual(time.localtime(nxt).tm_mday, 5)


class SchedulerTestBase(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.settings = self.harness.services.settings
        self.store = self.harness.services.store
        self.scanner = FakeScanner()
        self.notifier = FakeNotifier()
        self.clock = FixedClock(time.time())
        self.scheduler = SchedulerService(
            self.harness.config, self.store, self.settings.get,
            self.scanner, notifier=self.notifier, clock=self.clock)

    def tearDown(self):
        self.harness.cleanup()


class SchedulerStatusTest(SchedulerTestBase):
    def test_status_defaults(self):
        status = self.scheduler.status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["rules"], [])
        self.assertIsNone(status["last_scan_id"])

    def test_status_lists_next_run(self):
        self.settings.update({
            "scheduled_scan_enabled": True,
            "scheduled_scan_rules": ["daily 03:30", "interval 120"],
        })
        status = self.scheduler.status()
        self.assertTrue(status["enabled"])
        self.assertEqual(len(status["rules"]), 2)
        for item in status["rules"]:
            self.assertTrue(item["next_run"])

    def test_bad_rule_rejected_by_settings(self):
        from dupcleaner.errors import ApiError
        with self.assertRaises(ApiError):
            self.settings.update({"scheduled_scan_rules": ["every day 3am"]})

    def test_scheduled_roots_validated(self):
        from dupcleaner.errors import ApiError
        with self.assertRaises(ApiError):
            self.settings.update({"scheduled_scan_roots": ["relative/path"]})


class SchedulerRunTest(SchedulerTestBase):
    def test_run_now_starts_scan(self):
        self.settings.update({"scheduled_scan_roots": [self.harness.dev_root]})
        result = self.scheduler.run_now(user="admin")
        self.assertTrue(result["ok"])
        self.assertEqual(result["scan_id"], self.scanner.scan_id)
        self.assertEqual(len(self.scanner.calls), 1)
        call = self.scanner.calls[0]
        self.assertEqual(call["roots"], [self.harness.dev_root])
        self.assertEqual(call["user"], "admin")
        status = self.scheduler.status()
        self.assertEqual(status["last_scan_id"], self.scanner.scan_id)
        self.assertEqual(status["last_status"], "started")

    def test_run_now_reuses_last_completed_scan_roots(self):
        roots = [self.harness.dev_root]
        self.store.save_scan_meta("20260101-000000-aaaaaa", {
            "scan_id": "20260101-000000-aaaaaa",
            "status": "completed",
            "config": {"roots": roots, "excludes": []},
            "options": {},
            "stats": {},
        })
        result = self.scheduler.run_now()
        self.assertTrue(result["ok"])
        self.assertEqual(self.scanner.calls[0]["roots"], roots)

    def test_run_now_without_roots(self):
        result = self.scheduler.run_now()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_roots")
        self.assertEqual(self.scanner.calls, [])
        self.assertEqual(self.scheduler.status()["last_status"], "skipped")

    def test_run_now_busy_skips(self):
        self.scanner.busy = 1
        self.settings.update({"scheduled_scan_roots": [self.harness.dev_root]})
        result = self.scheduler.run_now()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "busy")
        self.assertEqual(self.scanner.calls, [])

    def test_run_now_load_guard(self):
        self.scheduler._load1 = lambda: 99.0
        self.settings.update({
            "scheduled_scan_roots": [self.harness.dev_root],
            "scheduled_scan_max_load": 4.0,
        })
        result = self.scheduler.run_now()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "load")
        self.assertEqual(self.scanner.calls, [])

    def test_tick_fires_interval_rule(self):
        self.settings.update({
            "scheduled_scan_enabled": True,
            "scheduled_scan_rules": ["interval 5"],
            "scheduled_scan_roots": [self.harness.dev_root],
        })
        self.clock.value = time.time()
        self.scheduler._state["rules"] = {
            "interval 5": {"last_run": self.clock.value - 600}
        }
        self.scheduler._tick()
        self.assertEqual(len(self.scanner.calls), 1)
        # 第二轮：刚跑过（<SCHEDULE_MIN_INTERVAL_SEC）→ 不重复触发
        self.scheduler._state["rules"]["interval 5"]["last_run"] = (
            self.clock.value)
        self.scheduler._tick()
        self.assertEqual(len(self.scanner.calls), 1)

    def test_tick_disabled(self):
        self.settings.update({
            "scheduled_scan_rules": ["interval 5"],
            "scheduled_scan_roots": [self.harness.dev_root],
        })
        self.scheduler._state["rules"] = {
            "interval 5": {"last_run": time.time() - 600}
        }
        self.scheduler._tick()
        self.assertEqual(self.scanner.calls, [])


class SubtitleWatcherTest(SchedulerTestBase):
    def test_notifies_finished_group_once(self):
        groups = [{
            "group_id": "sbg-20260101-000000-abcdef",
            "status": "completed", "total": 4,
            "counts": {"done": 4, "failed": 0, "skipped": 0},
        }]
        self.scheduler.subtitles_provider = lambda: FakeSubtitles(groups)
        self.settings.update({"notify_enabled": True})
        self.scheduler._last_notify_watch = 0
        self.scheduler._watch_subtitle_groups(time.time(), self.settings.get())
        self.assertEqual(len(self.notifier.groups), 1)
        self.assertEqual(self.notifier.groups[0]["group_id"],
                         "sbg-20260101-000000-abcdef")
        # 第二次不再重复通知
        self.scheduler._last_notify_watch = 0
        self.scheduler._watch_subtitle_groups(time.time(), self.settings.get())
        self.assertEqual(len(self.notifier.groups), 1)


if __name__ == "__main__":
    unittest.main()
