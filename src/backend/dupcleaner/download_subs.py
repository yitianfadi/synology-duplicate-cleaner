# -*- coding: utf-8 -*-
"""下载订阅（v0054，docs/DOWNLOAD_CONTRACT.md §17）。

给一个 RSS/Atom 源或播放网页，按固定间隔检查更新，把新条目自动加入下载队列：

- 状态持久化 `<var>/download_subscriptions.json`（不建表，避免 schema 迁移）；
- 订阅项：`{sub_id, url, kind(rss|page), interval_sec, mode(best|all), enabled,
  options, seen[], last_check, last_result, created_epoch}`；
- RSS/Atom 用标准库 `xml.etree` 解析 `<item><link>/<enclosure>` 与 `<entry><link href>`；
- 网页订阅复用 `DownloadService.parse_target`（静态解析），按 `mode` 取最高分候选或全部；
- 每项最多记 `MAX_SEEN=200` 条已下载地址，单次最多入队 `MAX_ENQUEUE=5` 条；
- 与 `download_subscribe_enabled` 总开关联动（关闭时仍可手动「立即检查」）。

不做：多级页面爬取、登录态持久化（cURL 的 parse_session 只在单次检查内使用）、去重以外的条目排序。
"""

from __future__ import absolute_import

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ElementTree

from . import config

logger = logging.getLogger("dupcleaner.download_subs")

TICK_SEC = 30
MIN_INTERVAL_SEC = 300
MAX_INTERVAL_SEC = 86400
MAX_SEEN = 200
MAX_ENQUEUE = 5
MAX_ITEMS = 50
STATE_FILE = "download_subscriptions.json"


class SubError(Exception):
    def __init__(self, code, message):
        super(SubError, self).__init__(message)
        self.code = code
        self.message = message


def rss_items(text):
    """从 RSS 2.0 / Atom 文本里取条目地址（去重保序）。"""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    body = (text or "").lstrip("\ufeff \r\n\t")
    if not body:
        return []
    try:
        root = ElementTree.fromstring(body.encode("utf-8"))
    except ElementTree.ParseError:
        return []
    items = []

    def add(url):
        value = (url or "").strip()
        if value and value not in items:
            items.append(value)

    if _local(root.tag) == "rss" or _first_local(root, "channel") is not None:
        channel = _first_local(root, "channel")
        if channel is None:
            channel = root
        for item in _children_local(channel, "item"):
            add(_text(item, "link"))
            enclosure = _first_local(item, "enclosure")
            if enclosure is not None:
                add(enclosure.get("url"))
            guid = _text(item, "guid")
            if guid and guid.lower().startswith(("http://", "https://")):
                add(guid)
    elif _local(root.tag) == "feed":
        for entry in _children_local(root, "entry"):
            for link in _children_local(entry, "link"):
                rel = (link.get("rel") or "alternate").lower()
                if rel in ("alternate", "enclosure", ""):
                    add(link.get("href"))
            add(_text(entry, "id"))
    return items[:MAX_ITEMS]


def _local(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def _children_local(node, name):
    return [child for child in list(node) if _local(child.tag) == name]


def _first_local(node, name):
    for child in list(node):
        if _local(child.tag) == name:
            return child
    return None


def _text(node, name):
    child = _first_local(node, name)
    if child is None or child.text is None:
        return u""
    return child.text.strip()


class DownloadSubscriptionService(object):
    def __init__(self, runtime_config, store, settings_provider,
                 download_service, clock=None):
        self.config = runtime_config
        self.store = store
        self.settings = settings_provider
        self.downloads = download_service
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._subs = self._load_state()
        self._last_tick = 0.0

    # -- 生命周期 -------------------------------------------------------
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="dc-download-subs", daemon=True)
        self._thread.start()

    def stop(self, timeout=3.0):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # pragma: no cover - 兜底
                logger.exception(u"下载订阅轮询异常")
            self._stop.wait(TICK_SEC)

    # -- 状态 -----------------------------------------------------------
    def _state_path(self):
        return os.path.join(self.config.data_dir, STATE_FILE)

    def _load_state(self):
        try:
            with open(self._state_path(), "rb") as handle:
                data = json.loads(handle.read().decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        subs = data.get("subs")
        if not isinstance(subs, dict):
            return {}
        return subs

    def _save_state(self):
        with self._lock:
            payload = {"subs": dict(self._subs),
                       "updated_epoch": self._clock()}
        path = self._state_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            temporary = path + ".tmp"
            with open(temporary, "wb") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False)
                             .encode("utf-8"))
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            os.replace(temporary, path)
        except OSError:  # pragma: no cover
            logger.warning(u"保存下载订阅失败", exc_info=True)

    def _settings(self):
        try:
            return self.settings() or {}
        except Exception:  # pragma: no cover
            return {}

    # -- 查询 / 维护 ----------------------------------------------------
    def status(self):
        settings = self._settings()
        with self._lock:
            items = [self._view(item) for item in self._subs.values()]
        items.sort(key=lambda item: item.get("created_epoch") or 0,
                   reverse=True)
        return {
            "enabled": settings.get("download_subscribe_enabled") is True,
            "tick_sec": TICK_SEC,
            "min_interval_sec": MIN_INTERVAL_SEC,
            "max_interval_sec": MAX_INTERVAL_SEC,
            "max_items": MAX_ITEMS,
            "state_file": STATE_FILE,
            "items": items,
        }

    def _view(self, sub):
        entry = dict(sub)
        entry.pop("seen", None)
        entry["seen_count"] = len(sub.get("seen") or [])
        # 不把 Cookie 等敏感值回传给前端
        options = dict(entry.get("options") or {})
        if "cookie" in options:
            options["cookie_set"] = bool(options.pop("cookie"))
        entry["options"] = options
        return entry

    def add(self, user, payload):
        if not isinstance(payload, dict):
            raise SubError("INVALID_ARGUMENT", u"请求体必须是 JSON 对象")
        url = str(payload.get("url") or u"").strip()
        if not url:
            raise SubError("URL_INVALID", u"订阅地址不能为空")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in config.DOWNLOAD_SCHEMES or not parsed.hostname:
            raise SubError("URL_INVALID", u"订阅地址必须是 http(s) URL")
        kind = str(payload.get("kind") or "").lower()
        if kind not in ("rss", "page"):
            kind = "rss" if re.search(r"(\.rss|\.xml|/feed|/rss)(\?|$)", url,
                                      re.IGNORECASE) else "page"
        interval = self._interval(payload.get("interval_sec"))
        mode = str(payload.get("mode") or "").lower()
        if mode not in ("best", "all"):
            mode = "best" if kind == "page" else "all"
        options = payload.get("options")
        if options is not None and not isinstance(options, dict):
            raise SubError("INVALID_ARGUMENT", u"options 必须是 JSON 对象")
        with self._lock:
            if len(self._subs) >= MAX_ITEMS:
                raise SubError("SUBSCRIPTION_LIMIT",
                               u"订阅数量已达上限（%d）" % MAX_ITEMS)
            sub_id = "ds-" + os.urandom(8).hex()
            sub = {
                "sub_id": sub_id,
                "url": url,
                "kind": kind,
                "mode": mode,
                "interval_sec": interval,
                "enabled": payload.get("enabled") is not False,
                "user": user or "",
                "options": dict(options or {}),
                "seen": [],
                "last_check": 0.0,
                "last_result": {},
                "created_epoch": self._clock(),
            }
            self._subs[sub_id] = sub
        self._save_state()
        return self._view(sub)

    @staticmethod
    def _interval(value):
        try:
            interval = int(value)
        except (TypeError, ValueError):
            interval = 3600
        return max(MIN_INTERVAL_SEC, min(interval, MAX_INTERVAL_SEC))

    def update(self, sub_id, payload):
        with self._lock:
            sub = self._subs.get(sub_id)
            if sub is None:
                raise SubError("NOT_FOUND", u"订阅不存在: %s" % sub_id)
            if "enabled" in payload:
                sub["enabled"] = payload.get("enabled") is not False
            if "interval_sec" in payload:
                sub["interval_sec"] = self._interval(payload.get("interval_sec"))
            if "mode" in payload:
                mode = str(payload.get("mode") or "").lower()
                if mode in ("best", "all"):
                    sub["mode"] = mode
            view = self._view(sub)
        self._save_state()
        return view

    def delete(self, sub_id):
        with self._lock:
            if sub_id not in self._subs:
                raise SubError("NOT_FOUND", u"订阅不存在: %s" % sub_id)
            self._subs.pop(sub_id, None)
        self._save_state()
        return {"ok": True, "deleted": sub_id}

    # -- 轮询 -----------------------------------------------------------
    def tick(self, force=False):
        settings = self._settings()
        enabled = settings.get("download_subscribe_enabled") is True
        now = self._clock()
        results = []
        with self._lock:
            items = list(self._subs.values())
        for sub in items:
            if not sub.get("enabled", True):
                continue
            if not force and not enabled:
                continue
            last = float(sub.get("last_check") or 0)
            if not force and now - last < float(sub.get("interval_sec")
                                               or MIN_INTERVAL_SEC):
                continue
            try:
                results.append(self.check_now(sub["sub_id"], force=True))
            except SubError as exc:
                results.append({"sub_id": sub["sub_id"],
                                "error": exc.message})
            except Exception:
                logger.exception(u"下载订阅检查失败: %s", sub["sub_id"])
        return results

    def check_now(self, sub_id, force=False):
        settings = self._settings()
        with self._lock:
            sub = self._subs.get(sub_id)
            if sub is None:
                raise SubError("NOT_FOUND", u"订阅不存在: %s" % sub_id)
            snapshot = dict(sub)
        if not force and not settings.get("download_subscribe_enabled"):
            raise SubError("DOWNLOAD_DISABLED", u"下载订阅未启用")
        kind = snapshot.get("kind") or "page"
        options = dict(snapshot.get("options") or {})
        items = []
        parse_session = u""
        if kind == "rss":
            items = self._rss_items(snapshot.get("url"))
        else:
            result = self.downloads.parse_target(
                snapshot.get("user") or "subscriber",
                {"url": snapshot.get("url")})
            parse_session = result.get("parse_session") or u""
            candidates = [item for item in result.get("candidates") or []
                          if item.get("kind") in ("hls", "dash", "file")]
            if snapshot.get("mode") != "all" and candidates:
                candidates = candidates[:1]
            items = [item["url"] for item in candidates]
        seen = set(snapshot.get("seen") or [])
        new_items = [item for item in items if item not in seen]
        enqueued = 0
        job_ids = []
        if new_items:
            batch = new_items[:MAX_ENQUEUE]
            submit_options = dict(options)
            if parse_session:
                submit_options["parse_session"] = parse_session
            try:
                created = self.downloads.create(
                    snapshot.get("user") or "subscriber",
                    {"urls": batch,
                     "group_title": u"订阅：%s" % self._short_url(
                         snapshot.get("url")),
                     "options": submit_options})
                job_ids = [job["job_id"] for job in created.get("jobs") or []]
                enqueued = len(job_ids)
            except Exception as exc:
                message = getattr(exc, "message", str(exc))
                with self._lock:
                    sub = self._subs.get(sub_id)
                    if sub is not None:
                        sub["last_check"] = self._clock()
                        sub["last_result"] = {
                            "checked": len(items), "new": len(new_items),
                            "enqueued": 0, "error": message}
                self._save_state()
                raise SubError("SUBSCRIPTION_FAILED",
                              u"入队失败：%s" % message)
            merged = list(snapshot.get("seen") or []) + batch
            if len(merged) > MAX_SEEN:
                merged = merged[-MAX_SEEN:]
        else:
            merged = list(snapshot.get("seen") or [])
        result = {
            "checked": len(items),
            "new": len(new_items),
            "enqueued": enqueued,
            "job_ids": job_ids,
            "at": self._clock(),
        }
        with self._lock:
            sub = self._subs.get(sub_id)
            if sub is not None:
                sub["seen"] = merged
                sub["last_check"] = self._clock()
                sub["last_result"] = dict(result)
        self._save_state()
        return {"sub_id": sub_id, "result": result}

    @staticmethod
    def _short_url(url):
        text = (url or u"").split("?", 1)[0]
        return text[-60:]

    def _rss_items(self, url):
        settings = self._settings()
        options = self.downloads._options_for(settings, {})
        client = self.downloads._client(_RssJob(url, options))
        try:
            body, _headers, _final = client.read_text(
                url, limit=config.DOWNLOAD_PAGE_MAX_BYTES)
        except Exception as exc:
            message = getattr(exc, "message", str(exc))
            raise SubError("SUBSCRIPTION_FAILED",
                           u"抓取订阅源失败：%s" % message)
        finally:
            try:
                client.close()
            except Exception:
                pass
        return rss_items(body)


class _RssJob(object):
    """给 `_HttpClient` 用的最小任务壳（只需要 options / _limiter）。"""

    def __init__(self, url, options=None):
        self.url = url
        self.options = dict(options or {})
        self._limiter = None
