# -*- coding: utf-8 -*-
"""通知系统（v0046）：DSM 系统通知 / Webhook / Telegram。

设计要点：
- 设置里 `notify_enabled` 总开关 + `notify_channels` 渠道 + `notify_events` 事件订阅；
- 发送走后台单线程队列（不阻塞扫描/删除/字幕线程），失败只记审计不影响主流程；
- 四个触发点：扫描完成/失败、字幕批次结束、批量失败超阈值、单次删除释放空间达阈值；
- 测试接口 `POST /api/notifications/test` 便于在设置页逐个渠道验证；
- 任何日志/审计/响应都不包含 Telegram token 与 Webhook 完整 URL（只留 host）。
"""

from __future__ import absolute_import

import json
import logging
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, timeutil

logger = logging.getLogger("dupcleaner.notify")

LEVEL_LABEL = {"info": u"信息", "warn": u"警告", "error": u"错误"}
DSM_LEVEL = {"info": "info", "warn": "warn", "error": "error"}


def _http_post_json(url, payload, timeout):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    request.add_header("User-Agent", "DuplicateCleaner/" + config.APP_VERSION)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(2048)
            status = getattr(response, "status", 200)
        return status >= 200 and status < 300, "HTTP %s" % status, body
    except urllib.error.HTTPError as exc:
        return False, "HTTP %s" % exc.code, b""
    except (urllib.error.URLError, OSError) as exc:
        return False, u"网络错误: %s" % exc, b""


def _safe_url(url):
    """日志里只保留 scheme+host+path，去掉 query/userinfo（Webhook 常带 token）。"""
    try:
        parsed = urllib.parse.urlsplit(url or "")
        host = parsed.hostname or ""
        return "%s://%s%s" % (parsed.scheme, host, parsed.path)
    except ValueError:
        return "(invalid url)"


class NotificationService(object):
    CHANNELS = config.NOTIFY_CHANNELS
    EVENTS = config.NOTIFY_EVENTS

    def __init__(self, runtime_config, store, settings_provider,
                 transport=None, dsm_runner=None):
        self.config = runtime_config
        self.store = store
        self.settings = settings_provider
        self._transport = transport or _http_post_json
        self._dsm_runner = dsm_runner or self._run_dsm_notify
        self._queue = queue.Queue(maxsize=config.NOTIFY_QUEUE_MAX)
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 状态 / 配置
    # ------------------------------------------------------------------
    def _settings(self):
        try:
            return self.settings() or {}
        except Exception:  # pragma: no cover
            return {}

    def status(self):
        settings = self._settings()
        channels = []
        dsm_available = os.path.isfile(config.DSM_NOTIFY_PATH)
        channels.append({
            "channel": "dsm",
            "label": u"DSM 系统通知",
            "available": dsm_available,
            "configured": dsm_available,
            "detail": (u"通过 synodsmnotify 发送给 @administrators"
                       if dsm_available else u"当前系统没有 synodsmnotify（非 DSM 环境）"),
        })
        webhook_url = (settings.get("notify_webhook_url") or "").strip()
        channels.append({
            "channel": "webhook",
            "label": "Webhook",
            "available": True,
            "configured": bool(webhook_url),
            "detail": _safe_url(webhook_url) if webhook_url else u"未填写 Webhook 地址",
        })
        token_set = bool(settings.get("notify_telegram_bot_token"))
        chat_id = (settings.get("notify_telegram_chat_id") or "").strip()
        channels.append({
            "channel": "telegram",
            "label": "Telegram",
            "available": True,
            "configured": bool(token_set and chat_id),
            "detail": (u"已配置 Bot Token 与 chat_id" if token_set and chat_id
                       else u"需要 Bot Token 与 chat_id"),
        })
        return {
            "enabled": bool(settings.get("notify_enabled")),
            "channels": channels,
            "subscribed_channels": list(settings.get("notify_channels") or []),
            "events": list(settings.get("notify_events") or []),
            "event_labels": dict(config.NOTIFY_EVENT_LABELS),
            "fail_threshold": int(settings.get("notify_fail_threshold") or 0),
            "freed_bytes_threshold": int(
                settings.get("notify_freed_bytes_threshold") or 0),
            "timeout_sec": int(settings.get("notify_timeout_sec")
                               or config.NOTIFY_TIMEOUT_DEFAULT),
            "queue_size": self._queue.qsize(),
        }

    def _subscribed(self, event, settings=None):
        settings = settings if settings is not None else self._settings()
        if not settings.get("notify_enabled"):
            return []
        if event not in (settings.get("notify_events") or []):
            return []
        channels = []
        for channel in settings.get("notify_channels") or []:
            if channel not in self.CHANNELS:
                continue
            if channel == "dsm" and not os.path.isfile(config.DSM_NOTIFY_PATH):
                continue
            if channel == "webhook" and not (
                    settings.get("notify_webhook_url") or "").strip():
                continue
            if channel == "telegram" and not (
                    settings.get("notify_telegram_bot_token")
                    and (settings.get("notify_telegram_chat_id") or "").strip()):
                continue
            channels.append(channel)
        return channels

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    def notify(self, event, title, message, level="info", detail=None):
        """异步入队；返回是否成功入队（未启用/未订阅/队列满 → False）。"""
        settings = self._settings()
        channels = self._subscribed(event, settings)
        if not channels:
            return False
        task = {
            "event": event,
            "title": title,
            "message": message,
            "level": level if level in LEVEL_LABEL else "info",
            "detail": detail or {},
            "channels": channels,
            "settings": {
                "notify_webhook_url": settings.get("notify_webhook_url") or "",
                "notify_telegram_bot_token":
                    settings.get("notify_telegram_bot_token") or "",
                "notify_telegram_chat_id":
                    settings.get("notify_telegram_chat_id") or "",
                "notify_timeout_sec": int(settings.get("notify_timeout_sec")
                                          or config.NOTIFY_TIMEOUT_DEFAULT),
            },
        }
        self._ensure_worker()
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            self.store.audit("notify_dropped", "system",
                             {"event": event, "reason": "queue_full"})
            return False
        return True

    def _ensure_worker(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._stop.is_set():
                return
            self._thread = threading.Thread(target=self._worker,
                                            name="dc-notify", daemon=True)
            self._thread.start()

    def _worker(self):
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._dispatch(task)
            except Exception:  # pragma: no cover - 兜底
                logger.warning(u"通知发送异常", exc_info=True)

    def _dispatch(self, task):
        for channel in task["channels"]:
            ok, detail = self._send(channel, task)
            if ok:
                self.store.audit("notify_sent", "system", {
                    "event": task["event"], "channel": channel,
                    "level": task["level"]})
            else:
                self.store.audit("notify_failed", "system", {
                    "event": task["event"], "channel": channel,
                    "error": detail})

    def _send(self, channel, task):
        settings = task["settings"]
        timeout = max(2, min(int(settings.get("notify_timeout_sec") or 5), 30))
        text = task["title"] if not task["message"] else (
            task["title"] + u"\n" + task["message"])
        if channel == "dsm":
            return self._dsm_runner(task, timeout)
        if channel == "webhook":
            url = (settings.get("notify_webhook_url") or "").strip()
            payload = {
                "event": task["event"],
                "title": task["title"],
                "message": task["message"],
                "level": task["level"],
                "timestamp": timeutil.iso_local(time.time()),
                "app": "DuplicateCleaner",
                "version": config.APP_VERSION,
                "detail": task["detail"],
            }
            ok, detail, _body = self._transport(url, payload, timeout)
            return ok, detail
        if channel == "telegram":
            token = (settings.get("notify_telegram_bot_token") or "").strip()
            chat_id = (settings.get("notify_telegram_chat_id") or "").strip()
            url = "https://api.telegram.org/bot%s/sendMessage" % token
            payload = {"chat_id": chat_id, "text": text[:4000],
                       "disable_web_page_preview": True}
            ok, detail, _body = self._transport(url, payload, timeout)
            return ok, detail
        return False, u"未知渠道: %s" % channel

    def _run_dsm_notify(self, task, timeout):
        path = config.DSM_NOTIFY_PATH
        if not os.path.isfile(path):
            return False, u"synodsmnotify 不存在"
        command = [path, "-l", DSM_LEVEL.get(task["level"], "info"),
                   "-c", task["title"][:120],
                   "-t", task["message"][:1000],
                   "@administrators"]
        try:
            completed = subprocess.run(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, u"DSM 通知失败: %s" % exc
        if completed.returncode != 0:
            err = (completed.stderr or b"").decode("utf-8", "replace")[:200]
            return False, u"DSM 通知返回 %s: %s" % (completed.returncode, err)
        return True, "synodsmnotify ok"

    # ------------------------------------------------------------------
    # 测试
    # ------------------------------------------------------------------
    def test_channel(self, channel):
        if channel not in self.CHANNELS:
            raise ValueError(u"未知渠道: %s" % channel)
        settings = self._settings()
        task = {
            "event": "test",
            "title": u"【重复文件清理】通知测试",
            "message": u"这是一条测试通知，说明「%s」渠道配置可用。" % channel,
            "level": "info",
            "detail": {"test": True},
            "channels": [channel],
            "settings": {
                "notify_webhook_url": settings.get("notify_webhook_url") or "",
                "notify_telegram_bot_token":
                    settings.get("notify_telegram_bot_token") or "",
                "notify_telegram_chat_id":
                    settings.get("notify_telegram_chat_id") or "",
                "notify_timeout_sec": int(settings.get("notify_timeout_sec")
                                          or config.NOTIFY_TIMEOUT_DEFAULT),
            },
        }
        ok, detail = self._send(channel, task)
        self.store.audit("notify_test", "admin", {
            "channel": channel, "ok": bool(ok), "detail": detail})
        return {"channel": channel, "ok": bool(ok), "detail": detail}

    # ------------------------------------------------------------------
    # 事件触发
    # ------------------------------------------------------------------
    def scan_finished(self, snapshot):
        try:
            status = (snapshot or {}).get("status")
            stats = (snapshot or {}).get("stats") or {}
            if status == "completed":
                return self.notify(
                    "scan_complete",
                    u"重复文件扫描完成",
                    u"扫描 %s 完成：分组 %s 个，重复文件 %s 个，可释放 %s。"
                    % (snapshot.get("scan_id"),
                       stats.get("groups", 0),
                       stats.get("duplicate_files", 0),
                       _format_bytes(stats.get("reclaimable_bytes", 0))),
                    "info",
                    {"scan_id": snapshot.get("scan_id"),
                     "groups": stats.get("groups", 0),
                     "duplicate_files": stats.get("duplicate_files", 0),
                     "reclaimable_bytes": stats.get("reclaimable_bytes", 0)})
            if status == "failed":
                error = (snapshot or {}).get("error") or {}
                return self.notify(
                    "scan_failed",
                    u"重复文件扫描失败",
                    u"扫描 %s 失败：%s" % (snapshot.get("scan_id"),
                                       error.get("message") or u"未知错误"),
                    "error",
                    {"scan_id": snapshot.get("scan_id"),
                     "code": error.get("code")})
            return False
        except Exception:  # pragma: no cover
            logger.warning(u"扫描通知失败", exc_info=True)
            return False

    def deletion_finished(self, snapshot, threshold_bytes=None):
        try:
            settings = self._settings()
            threshold = int(threshold_bytes if threshold_bytes is not None
                            else (settings.get("notify_freed_bytes_threshold")
                                  or 0))
            freed = int((snapshot or {}).get("bytes_done") or 0)
            failed = int((snapshot or {}).get("failed") or 0)
            event = None
            if freed >= threshold > 0:
                event = "freed_threshold"
                title = u"重复文件清理已释放空间"
                message = u"删除任务 %s 释放了 %s（%s 个文件，失败 %s 个）。" % (
                    (snapshot or {}).get("job_id"), _format_bytes(freed),
                    (snapshot or {}).get("done", 0), failed)
                level = "info"
                detail = {"job_id": (snapshot or {}).get("job_id"),
                          "freed_bytes": freed, "done": (snapshot or {}).get("done", 0)}
            elif failed >= int(settings.get("notify_fail_threshold") or 0) > 0:
                event = "failure_burst"
                title = u"删除任务出现大量失败"
                message = u"删除任务 %s 失败 %s 个文件，请到任务中心查看明细。" % (
                    (snapshot or {}).get("job_id"), failed)
                level = "error"
                detail = {"job_id": (snapshot or {}).get("job_id"),
                          "failed": failed}
            if event is None:
                return False
            return self.notify(event, title, message, level, detail)
        except Exception:  # pragma: no cover
            logger.warning(u"删除通知失败", exc_info=True)
            return False

    def subtitle_batch_finished(self, group, threshold=None):
        try:
            settings = self._settings()
            threshold = int(threshold if threshold is not None
                            else (settings.get("notify_fail_threshold") or 0))
            counts = (group or {}).get("counts") or {}
            failed = int(counts.get("failed") or 0)
            total = int((group or {}).get("total") or 0)
            group_id = (group or {}).get("group_id")
            if failed >= threshold > 0:
                return self.notify(
                    "failure_burst",
                    u"字幕批次出现大量失败",
                    u"批次组 %s 共 %s 个任务，失败 %s 个，请到任务中心查看失败原因。"
                    % (group_id, total, failed),
                    "error",
                    {"group_id": group_id, "failed": failed, "total": total})
            return self.notify(
                "subtitle_batch_done",
                u"字幕批次执行结束",
                u"批次组 %s 已结束：共 %s 个任务，完成 %s，失败 %s，跳过 %s。"
                % (group_id, total, counts.get("done", 0), failed,
                   counts.get("skipped", 0)),
                "info",
                {"group_id": group_id, "failed": failed, "total": total})
        except Exception:  # pragma: no cover
            logger.warning(u"字幕批次通知失败", exc_info=True)
            return False

    def stop(self, timeout=3.0):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)


def _format_bytes(value):
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        value = 0
    if value < 1024:
        return u"%d B" % value
    units = ["KB", "MB", "GB", "TB", "PB"]
    size = float(value)
    for unit in units:
        size /= 1024.0
        if size < 1024 or unit == units[-1]:
            return u"%.2f %s" % (size, unit)
    return u"%d B" % value
