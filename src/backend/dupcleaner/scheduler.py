# -*- coding: utf-8 -*-
"""计划扫描 + 通知调度（v0046）。

- 规则格式（每行一条，保存在 `scheduled_scan_rules`）：
  - `daily HH:MM`：每天固定时间；
  - `weekly mon,wed,fri HH:MM`：每周指定星期（mon..sun 或 1..7）；
  - `interval MIN`：每隔 MIN 分钟（最小 5 分钟）。
- 低负载保护：`scheduled_scan_max_load` > 0 时，1 分钟负载超过阈值则跳过本次（记审计）。
- 扫描范围：`scheduled_scan_roots`；为空则复用“上次完成的扫描”的 roots。
- 同一时间只允许一个扫描；已有扫描在跑时跳过本次并记审计，避免堆积。
- 状态持久化 `<var>/scheduler_state.json`（每条规则 last_run/last_scan_id），重启后不会重复触发。
- 通知 watcher：每 `NOTIFY_WATCH_SEC` 秒检查字幕批次组是否结束，按组去重后发“字幕批次结束/
  批量失败超阈值”通知（组 id 记录在 `<var>/notify_state.json`）。
"""

from __future__ import absolute_import

import json
import logging
import os
import re
import threading
import time

from . import config, timeutil

logger = logging.getLogger("dupcleaner.scheduler")

_DAILY_RE = re.compile(r"^daily\s+([01]?\d|2[0-3]):([0-5]\d)$", re.I)
_WEEKLY_RE = re.compile(
    r"^weekly\s+([a-z0-9,\s]+?)\s+([01]?\d|2[0-3]):([0-5]\d)$", re.I)
_INTERVAL_RE = re.compile(r"^interval\s+(\d{1,5})$", re.I)


class ScheduleRuleError(ValueError):
    pass


def parse_rule(raw):
    """解析一条计划规则，返回 dict；非法抛 ScheduleRuleError。"""
    if not isinstance(raw, str):
        raise ScheduleRuleError(u"规则必须是字符串")
    text = raw.strip()
    if not text or len(text) > config.SCHEDULE_RULE_MAX_LEN:
        raise ScheduleRuleError(u"规则为空或过长")
    match = _INTERVAL_RE.match(text)
    if match:
        minutes = int(match.group(1))
        if minutes < 5 or minutes > 7 * 24 * 60:
            raise ScheduleRuleError(u"interval 分钟数需在 5..10080 之间")
        return {"raw": text, "kind": "interval", "interval_sec": minutes * 60}
    match = _DAILY_RE.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        return {"raw": text, "kind": "daily",
                "minute_of_day": hour * 60 + minute,
                "hour": hour, "minute": minute}
    match = _WEEKLY_RE.match(text)
    if match:
        token_text = match.group(1).replace(" ", "")
        weekdays = []
        for token in token_text.split(","):
            if not token:
                raise ScheduleRuleError(u"weekly 的星期列表为空")
            if token.lower() not in config.SCHEDULE_WEEKDAY_ALIASES:
                raise ScheduleRuleError(u"无法识别的星期: %s" % token)
            weekdays.append(config.SCHEDULE_WEEKDAY_ALIASES[token.lower()])
        if not weekdays:
            raise ScheduleRuleError(u"weekly 需要至少一个星期")
        hour, minute = int(match.group(2)), int(match.group(3))
        return {"raw": text, "kind": "weekly",
                "weekdays": sorted(set(weekdays)),
                "minute_of_day": hour * 60 + minute,
                "hour": hour, "minute": minute}
    raise ScheduleRuleError(
        u"无法解析规则（示例：daily 03:30 / weekly mon,wed,fri 04:00 / interval 360）")


def parse_rules(rules):
    parsed = []
    if not isinstance(rules, list):
        return parsed
    for raw in rules[:config.SCHEDULE_RULES_MAX]:
        try:
            parsed.append(parse_rule(raw))
        except ScheduleRuleError:
            continue
    return parsed


def next_run_epoch(rule, after_epoch):
    """返回该规则在 after_epoch 之后的下一次执行时刻（本地时区）。"""
    if rule["kind"] == "interval":
        return int(after_epoch) + int(rule["interval_sec"])
    after = time.localtime(after_epoch)
    base = time.mktime((after.tm_year, after.tm_mon, after.tm_mday,
                        0, 0, 0, 0, 0, -1))
    if rule["kind"] == "daily":
        candidate = base + rule["minute_of_day"] * 60
        while candidate <= after_epoch:
            candidate += 86400
        return int(candidate)
    # weekly：最多向后找 8 天
    for offset in range(0, 8):
        candidate = base + offset * 86400 + rule["minute_of_day"] * 60
        weekday = time.localtime(candidate).tm_wday
        if weekday in rule["weekdays"] and candidate > after_epoch:
            return int(candidate)
    return int(after_epoch + 7 * 86400)


class SchedulerService(object):
    def __init__(self, runtime_config, store, settings_provider, scan_manager,
                 notifier=None, subtitles_provider=None, clock=None):
        self.config = runtime_config
        self.store = store
        self.settings = settings_provider
        self.scans = scan_manager
        self.notifier = notifier
        self.subtitles_provider = subtitles_provider
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._last_tick = 0.0
        self._last_notify_watch = 0.0
        self._state = self._load_state()
        self._notify_state = self._load_notify_state()

    # ------------------------------------------------------------------
    # 持久化状态
    # ------------------------------------------------------------------
    def _state_path(self):
        return os.path.join(self.config.data_dir, config.SCHEDULE_STATE_FILE)

    def _notify_state_path(self):
        return os.path.join(self.config.data_dir,
                            config.SCHEDULE_NOTIFY_STATE_FILE)

    @staticmethod
    def _load_json(path):
        try:
            with open(path, "rb") as handle:
                data = json.loads(handle.read().decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, UnicodeError):
            return {}

    def _load_state(self):
        state = self._load_json(self._state_path())
        if not isinstance(state.get("rules"), dict):
            state["rules"] = {}
        return state

    def _load_notify_state(self):
        state = self._load_json(self._notify_state_path())
        if not isinstance(state.get("groups"), dict):
            state["groups"] = {}
        return state

    @staticmethod
    def _write_json(path, data):
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(tmp, path)

    def _save_state(self):
        with self._lock:
            data = dict(self._state)
        try:
            self._write_json(self._state_path(), data)
        except OSError:  # pragma: no cover
            logger.warning(u"保存计划扫描状态失败", exc_info=True)

    def _save_notify_state(self):
        with self._lock:
            data = dict(self._notify_state)
        try:
            self._write_json(self._notify_state_path(), data)
        except OSError:  # pragma: no cover
            logger.warning(u"保存通知去重状态失败", exc_info=True)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="dc-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout=3.0):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # pragma: no cover - 兜底
                logger.warning(u"计划扫描 tick 异常", exc_info=True)
            self._stop.wait(config.SCHEDULE_TICK_SEC)

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    def _settings(self):
        try:
            return self.settings() or {}
        except Exception:  # pragma: no cover
            return {}

    def status(self):
        settings = self._settings()
        now = self._clock()
        rules = []
        parsed = parse_rules(settings.get("scheduled_scan_rules") or [])
        for rule in parsed:
            raw = rule["raw"]
            last = float((self._state.get("rules") or {}).get(raw, {})
                         .get("last_run") or 0)
            next_epoch = next_run_epoch(rule, max(now, last) if last else now)
            rules.append({
                "rule": raw,
                "kind": rule["kind"],
                "last_run": timeutil.iso_local(last) if last else None,
                "next_run": timeutil.iso_local(next_epoch),
                "next_run_epoch": int(next_epoch),
            })
        return {
            "enabled": bool(settings.get("scheduled_scan_enabled")),
            "rules": rules,
            "roots": list(settings.get("scheduled_scan_roots") or []),
            "max_load": float(settings.get("scheduled_scan_max_load") or 0),
            "current_load": self._load1(),
            "last_run": self._state.get("last_run"),
            "last_scan_id": self._state.get("last_scan_id"),
            "last_status": self._state.get("last_status"),
            "last_message": self._state.get("last_message"),
            "min_interval_sec": config.SCHEDULE_MIN_INTERVAL_SEC,
            "tick_sec": config.SCHEDULE_TICK_SEC,
        }

    @staticmethod
    def _load1():
        try:
            return round(float(os.getloadavg()[0]), 2)
        except (AttributeError, OSError):
            return None

    # ------------------------------------------------------------------
    # 触发
    # ------------------------------------------------------------------
    def run_now(self, user="admin"):
        """立即执行一次计划扫描（仍走范围解析与负载/忙碌检查）。"""
        return self._trigger(user=user, trigger="manual", record_rule=None)

    def _resolve_roots(self):
        settings = self._settings()
        roots = [item for item in (settings.get("scheduled_scan_roots") or [])
                 if isinstance(item, str) and item.strip()]
        if roots:
            return roots
        # 复用上次完成的扫描范围
        try:
            metas = self.store.list_scan_metas(limit=20)
        except Exception:
            metas = []
        for meta in metas:
            if (meta or {}).get("status") != "completed":
                continue
            config_block = (meta or {}).get("config") or {}
            reused = [item for item in (config_block.get("roots") or [])
                      if isinstance(item, str) and item.strip()]
            if reused:
                return reused
        return []

    def _trigger(self, user, trigger, record_rule=None):
        with self._lock:
            if self._is_busy():
                self._mark_skipped(record_rule, "busy")
                return {"ok": False, "reason": "busy",
                        "message": u"已有扫描正在执行，本次跳过"}
            settings = self._settings()
            max_load = float(settings.get("scheduled_scan_max_load") or 0)
            load1 = self._load1()
            if max_load > 0 and load1 is not None and load1 > max_load:
                self._mark_skipped(record_rule, "load")
                return {"ok": False, "reason": "load",
                        "message": u"当前负载 %.2f 超过阈值 %.2f，本次跳过"
                                   % (load1, max_load)}
            roots = self._resolve_roots()
            if not roots:
                self._mark_skipped(record_rule, "no_roots")
                return {"ok": False, "reason": "no_roots",
                        "message": u"没有可扫描的范围（请配置计划扫描目录或先手动扫描一次）"}
            try:
                scan_id = self.scans.start_scan(roots, [], {}, user=user)
            except Exception as exc:
                message = getattr(exc, "message", str(exc))
                self._mark_failed(record_rule, message)
                return {"ok": False, "reason": "start_failed",
                        "message": message}
            now = self._clock()
            self._state["last_run"] = timeutil.iso_local(now)
            self._state["last_epoch"] = now
            self._state["last_scan_id"] = scan_id
            self._state["last_status"] = "started"
            self._state["last_message"] = u"已启动"
            if record_rule:
                entry = self._state["rules"].setdefault(record_rule, {})
                entry["last_run"] = now
                entry["last_scan_id"] = scan_id
            self._save_state()
            self.store.audit("scheduled_scan_start", user, {
                "scan_id": scan_id, "trigger": trigger,
                "roots": roots, "rule": record_rule,
                "load": load1, "max_load": max_load,
            })
            return {"ok": True, "scan_id": scan_id, "roots": roots}

    def _mark_skipped(self, rule, reason):
        now = self._clock()
        message = {
            "busy": u"已有扫描在执行，跳过",
            "load": u"负载超过阈值，跳过",
            "no_roots": u"没有可扫描范围，跳过",
        }.get(reason, reason)
        self._state["last_status"] = "skipped"
        self._state["last_message"] = message
        if rule:
            entry = self._state["rules"].setdefault(rule, {})
            entry["last_run"] = now
            entry["last_status"] = "skipped"
            entry["last_reason"] = reason
        self._save_state()
        self.store.audit("scheduled_scan_skipped", "scheduler", {
            "rule": rule, "reason": reason})

    def _mark_failed(self, rule, message):
        self._state["last_status"] = "failed"
        self._state["last_message"] = message
        if rule:
            entry = self._state["rules"].setdefault(rule, {})
            entry["last_status"] = "failed"
        self._save_state()
        self.store.audit("scheduled_scan_failed", "scheduler", {
            "rule": rule, "error": message})

    def _is_busy(self):
        try:
            return int((self.scans.active_stats() or {}).get("scans") or 0) > 0
        except Exception:  # pragma: no cover
            return False

    # ------------------------------------------------------------------
    # 定时循环
    # ------------------------------------------------------------------
    def _tick(self):
        now = self._clock()
        settings = self._settings()
        self._watch_subtitle_groups(now, settings)
        if not settings.get("scheduled_scan_enabled"):
            return
        parsed = parse_rules(settings.get("scheduled_scan_rules") or [])
        if not parsed:
            return
        rules_state = self._state.get("rules") or {}
        for rule in parsed:
            raw = rule["raw"]
            last = float((rules_state.get(raw) or {}).get("last_run") or 0)
            due_epoch = next_run_epoch(rule, last) if last else next_run_epoch(rule, now)
            if now < due_epoch:
                continue
            # 防止同一轮重复触发同一条规则
            if now - last < config.SCHEDULE_MIN_INTERVAL_SEC:
                continue
            self._trigger(user="scheduler", trigger="schedule", record_rule=raw)
            return

    def _watch_subtitle_groups(self, now, settings):
        if self.notifier is None or self.subtitles_provider is None:
            return
        if not settings.get("notify_enabled"):
            return
        if now - self._last_notify_watch < config.NOTIFY_WATCH_SEC:
            return
        self._last_notify_watch = now
        try:
            service = self.subtitles_provider()
        except Exception:  # pragma: no cover
            return
        if service is None:
            return
        try:
            overview = service.batch_group_overview(active_limit=50,
                                                    completed_limit=50)
        except Exception:  # pragma: no cover
            return
        groups = {}
        for item in (overview.get("completed") or []):
            group_id = (item or {}).get("group_id")
            if group_id:
                groups[group_id] = item
        if not groups:
            return
        recorded = self._notify_state.get("groups") or {}
        changed = False
        for group_id, group in groups.items():
            if group_id in recorded:
                continue
            if int(group.get("total") or 0) <= 0:
                continue
            try:
                self.notifier.subtitle_batch_finished(group)
            except Exception:  # pragma: no cover
                continue
            recorded[group_id] = {
                "notified_epoch": now,
                "status": group.get("status"),
                "total": group.get("total"),
            }
            changed = True
        # 只保留最近 500 条，防止无限增长
        if len(recorded) > 500:
            ordered = sorted(recorded.items(),
                             key=lambda pair: -(pair[1] or {}).get(
                                 "notified_epoch", 0))
            recorded = dict(ordered[:500])
            changed = True
        if changed:
            self._notify_state["groups"] = recorded
            self._save_notify_state()
