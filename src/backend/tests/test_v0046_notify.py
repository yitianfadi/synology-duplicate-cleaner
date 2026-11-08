# -*- coding: utf-8 -*-
"""v0046：通知系统（DSM / Webhook / Telegram + 事件触发）测试。"""

import json
import time
import unittest

from tests.helpers import Harness

from dupcleaner import config
from dupcleaner.notifications import NotificationService


class FakeTransport(object):
    def __init__(self, ok=True, detail="HTTP 200"):
        self.ok = ok
        self.detail = detail
        self.calls = []

    def __call__(self, url, payload, timeout):
        self.calls.append({"url": url, "payload": payload, "timeout": timeout})
        return self.ok, self.detail, b"{}"


class FakeDsmRunner(object):
    def __init__(self, ok=True, detail="synodsmnotify ok"):
        self.ok = ok
        self.detail = detail
        self.calls = []

    def __call__(self, task, timeout):
        self.calls.append({"task": task, "timeout": timeout})
        return self.ok, self.detail


class NotificationTestBase(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.transport = FakeTransport()
        self.dsm = FakeDsmRunner()
        self.service = NotificationService(
            self.harness.config, self.harness.services.store,
            self.harness.services.settings.get,
            transport=self.transport, dsm_runner=self.dsm)
        self.settings = self.harness.services.settings
        self.store = self.harness.services.store

    def tearDown(self):
        self.service.stop()
        self.harness.cleanup()

    def enable(self, **overrides):
        patch = {
            "notify_enabled": True,
            "notify_channels": ["webhook"],
            "notify_events": list(config.NOTIFY_EVENTS),
            "notify_webhook_url": "https://example.com/hook?token=abc",
            "notify_timeout_sec": 5,
        }
        patch.update(overrides)
        self.settings.update(patch)

    def wait_calls(self, count=1, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.transport.calls) >= count:
                return True
            time.sleep(0.02)
        return False


class NotificationSendTest(NotificationTestBase):
    def test_disabled_by_default(self):
        self.assertFalse(self.service.notify("scan_complete", "t", "m"))
        self.assertFalse(self.wait_calls(1, 0.2))

    def test_unsubscribed_event_ignored(self):
        self.enable(notify_events=["scan_failed"])
        self.assertFalse(self.service.notify("scan_complete", "t", "m"))
        self.assertFalse(self.wait_calls(1, 0.2))

    def test_webhook_payload(self):
        self.enable()
        self.assertTrue(self.service.notify(
            "scan_complete", u"标题", u"正文", "info", {"scan_id": "s1"}))
        self.assertTrue(self.wait_calls(1))
        call = self.transport.calls[0]
        self.assertEqual(call["url"], "https://example.com/hook?token=abc")
        self.assertEqual(call["payload"]["event"], "scan_complete")
        self.assertEqual(call["payload"]["title"], u"标题")
        self.assertEqual(call["payload"]["message"], u"正文")
        self.assertEqual(call["payload"]["version"], config.APP_VERSION)
        self.assertEqual(call["payload"]["detail"], {"scan_id": "s1"})
        self.assertEqual(call["timeout"], 5)

    def test_webhook_failure_recorded(self):
        self.transport.ok = False
        self.transport.detail = "HTTP 500"
        self.enable()
        self.assertTrue(self.service.notify("scan_complete", "t", "m"))
        self.assertTrue(self.wait_calls(1))
        audit = self.store.list_audit(limit=20) if hasattr(self.store, "list_audit") else None
        if audit is not None:
            actions = [item.get("action") for item in audit]
            self.assertIn("notify_failed", actions)

    def test_telegram_uses_bot_api(self):
        self.enable(notify_channels=["telegram"],
                    notify_telegram_bot_token="123:secret-token",
                    notify_telegram_chat_id="42")
        self.assertTrue(self.service.notify("scan_complete", "t", "m"))
        self.assertTrue(self.wait_calls(1))
        call = self.transport.calls[0]
        self.assertIn("api.telegram.org/bot123:secret-token/sendMessage",
                      call["url"])
        self.assertEqual(call["payload"]["chat_id"], "42")
        status = self.service.status()
        telegram = [item for item in status["channels"]
                    if item["channel"] == "telegram"][0]
        self.assertTrue(telegram["configured"])
        self.assertNotIn("secret-token", json.dumps(status))

    def test_dsm_skipped_without_synodsmnotify(self):
        self.enable(notify_channels=["dsm"])
        status = self.service.status()
        dsm = [item for item in status["channels"] if item["channel"] == "dsm"][0]
        self.assertFalse(dsm["available"])
        # 无可用渠道 → 不入队、不发送
        self.assertFalse(self.service.notify("scan_complete", "t", "m"))

    def test_test_channel_sync(self):
        self.enable()
        result = self.service.test_channel("webhook")
        self.assertTrue(result["ok"])
        self.assertEqual(self.transport.calls[0]["payload"]["event"], "test")

    def test_test_channel_unknown(self):
        with self.assertRaises(ValueError):
            self.service.test_channel("email")


class NotificationEventTest(NotificationTestBase):
    def test_scan_finished_completed(self):
        self.enable()
        self.assertTrue(self.service.scan_finished({
            "scan_id": "s1", "status": "completed",
            "stats": {"groups": 3, "duplicate_files": 7,
                      "reclaimable_bytes": 1024 * 1024},
        }))
        self.assertTrue(self.wait_calls(1))
        payload = self.transport.calls[0]["payload"]
        self.assertEqual(payload["event"], "scan_complete")
        self.assertEqual(payload["detail"]["groups"], 3)
        self.assertIn(u"完成", payload["title"])

    def test_scan_finished_failed(self):
        self.enable()
        self.assertTrue(self.service.scan_finished({
            "scan_id": "s1", "status": "failed",
            "error": {"code": "X", "message": u"磁盘不可读"},
        }))
        self.assertTrue(self.wait_calls(1))
        self.assertEqual(self.transport.calls[0]["payload"]["event"],
                         "scan_failed")
        self.assertIn(u"磁盘不可读",
                      self.transport.calls[0]["payload"]["message"])

    def test_deletion_freed_threshold(self):
        self.enable(notify_freed_bytes_threshold=1000)
        self.assertTrue(self.service.deletion_finished({
            "job_id": "del-1", "bytes_done": 2048, "done": 2, "failed": 0,
        }))
        self.assertTrue(self.wait_calls(1))
        self.assertEqual(self.transport.calls[0]["payload"]["event"],
                         "freed_threshold")

    def test_deletion_below_threshold_silent(self):
        self.enable(notify_freed_bytes_threshold=10000)
        self.assertFalse(self.service.deletion_finished({
            "job_id": "del-1", "bytes_done": 10, "done": 1, "failed": 0,
        }))

    def test_deletion_failure_burst(self):
        self.enable(notify_fail_threshold=3, notify_freed_bytes_threshold=0)
        self.assertTrue(self.service.deletion_finished({
            "job_id": "del-1", "bytes_done": 0, "done": 0, "failed": 5,
        }))
        self.assertTrue(self.wait_calls(1))
        self.assertEqual(self.transport.calls[0]["payload"]["event"],
                         "failure_burst")

    def test_subtitle_batch_finished(self):
        self.enable(notify_fail_threshold=3)
        self.assertTrue(self.service.subtitle_batch_finished({
            "group_id": "sbg-1", "total": 10,
            "counts": {"done": 10, "failed": 0, "skipped": 0},
        }))
        self.assertTrue(self.wait_calls(1))
        self.assertEqual(self.transport.calls[0]["payload"]["event"],
                         "subtitle_batch_done")
        self.assertTrue(self.service.subtitle_batch_finished({
            "group_id": "sbg-2", "total": 10,
            "counts": {"done": 2, "failed": 8, "skipped": 0},
        }))
        self.assertTrue(self.wait_calls(2))
        self.assertEqual(self.transport.calls[1]["payload"]["event"],
                         "failure_burst")


if __name__ == "__main__":
    unittest.main()
