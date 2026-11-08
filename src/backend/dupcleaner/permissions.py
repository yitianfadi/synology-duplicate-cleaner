# -*- coding: utf-8 -*-
"""权限与路径体检（v0049）：检测 + 指引，不做任何提权/自动改 ACL。

需求里的“一键配置共享文件夹读写权限”在 DSM 非 root 套件里**做不到**（评估结论 §3.1）：
只提供一键**体检**：列出配置中涉及的路径与共享文件夹，检查存在性、可读/可写/可进入，
再汇总“扫描/字幕/删除”任务里出现过的权限异常，给出在 DSM 控制面板操作的指引。
"""

from __future__ import absolute_import

import os
import time

from . import fsutil

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_ERROR = "error"

GUIDANCE = {
    "missing": u"路径不存在：请确认共享文件夹已挂载/路径拼写正确。",
    "not_dir": u"路径不是目录：请改为目录路径。",
    "outside_share": u"路径不在已识别的共享文件夹内：请把任务限制在共享文件夹内。",
    "not_readable": u"当前套件用户无法读取/进入该目录：在 DSM「控制面板 → 共享文件夹 → 编辑 → 权限」"
                    u"给套件用户（duplicatecleaner）读写权限，或把任务目录移到已授权共享文件夹。",
    "not_writable": u"当前套件用户无法写入：字幕写回/回收站/删除需要写权限；"
                    u"请在共享文件夹权限中授予读写，或改用“仅下载”输出模式。",
    "ok": u"权限正常。",
}


class PermissionInspector(object):
    def __init__(self, runtime_config, store, share_service,
                 settings_provider, subtitle_service=None):
        self.config = runtime_config
        self.store = store
        self.shares = share_service
        self.settings = settings_provider
        self.subtitles = subtitle_service

    def _settings(self):
        try:
            return self.settings() or {}
        except Exception:  # pragma: no cover
            return {}

    def _candidate_paths(self):
        """(source, path) 去重列表：设置里的路径 + 最近完成扫描范围 + 共享根。"""
        seen = set()
        result = []

        def add(source, path):
            if not isinstance(path, str) or not path.strip():
                return
            key = fsutil.path_key(path)
            if key in seen:
                return
            seen.add(key)
            result.append((source, path))

        settings = self._settings()
        for source, key in (
                ("计划扫描目录", "scheduled_scan_roots"),
                ("字幕监控目录", "subtitle_watch_dirs"),
                ("删除白名单目录", "protect_dirs"),
                ("字幕目录优先级", "subtitle_dir_priorities")):
            for path in settings.get(key) or []:
                add(source, path)
        try:
            metas = self.store.list_scan_metas(limit=20)
        except Exception:  # pragma: no cover
            metas = []
        for meta in metas:
            if (meta or {}).get("status") != "completed":
                continue
            for path in ((meta.get("config") or {}).get("roots") or []):
                add("上次扫描目录", path)
            break
        try:
            shares = self.shares.list_shares() or []
        except Exception:  # pragma: no cover
            shares = []
        for share in shares:
            add("共享文件夹", (share or {}).get("path"))
        return result

    def check_path(self, source, path):
        item = {
            "source": source,
            "path": path,
            "share": "",
            "exists": False,
            "is_dir": False,
            "readable": False,
            "writable": False,
            "status": STATUS_ERROR,
            "code": "",
            "guidance": "",
        }
        try:
            normalized = fsutil.check_path_safety(path)
        except Exception as exc:
            item["code"] = getattr(exc, "code", "PATH_NOT_ALLOWED")
            item["guidance"] = getattr(exc, "message", u"路径非法")
            return item
        item["path"] = normalized
        share = self.shares.find_share(normalized)
        if share is not None:
            item["share"] = share.get("name") or ""
        item["exists"] = os.path.exists(normalized)
        item["is_dir"] = fsutil.is_dir_nofollow(normalized)
        if not item["exists"]:
            item["code"] = "missing"
            item["guidance"] = GUIDANCE["missing"]
            return item
        if share is None:
            item["code"] = "outside_share"
            item["guidance"] = GUIDANCE["outside_share"]
            return item
        if not item["is_dir"]:
            item["code"] = "not_dir"
            item["guidance"] = GUIDANCE["not_dir"]
            item["status"] = STATUS_WARN
            return item
        readable = os.access(normalized, os.R_OK | os.X_OK)
        writable = os.access(normalized, os.W_OK)
        item["readable"] = bool(readable)
        item["writable"] = bool(writable)
        if not readable:
            item["code"] = "not_readable"
            item["guidance"] = GUIDANCE["not_readable"]
            item["status"] = STATUS_ERROR
            return item
        if not writable:
            item["code"] = "not_writable"
            item["guidance"] = GUIDANCE["not_writable"]
            item["status"] = STATUS_WARN
            return item
        item["status"] = STATUS_OK
        item["code"] = "ok"
        item["guidance"] = GUIDANCE["ok"]
        return item

    def check_paths(self, paths):
        items = []
        for path in (paths or [])[:50]:
            items.append(self.check_path(u"手动检查", path))
        return {"items": items, "count": len(items)}

    def _scan_permission_issues(self):
        issues = []
        try:
            metas = self.store.list_scan_metas(limit=5)
        except Exception:  # pragma: no cover
            metas = []
        for meta in metas:
            scan_id = (meta or {}).get("scan_id")
            stats = (meta or {}).get("stats") or {}
            skipped = stats.get("skipped") or {}
            permission = int(skipped.get("permission") or 0)
            if permission:
                issues.append({
                    "source": "scan",
                    "scan_id": scan_id,
                    "message": u"最近扫描有 %d 个文件因权限被跳过" % permission,
                    "count": permission,
                })
            if not scan_id:
                continue
            try:
                path = self.store.resolve_log("scan-%s.log" % scan_id)
            except Exception:
                continue
            try:
                with open(path, "rb") as handle:
                    text = handle.read(256 * 1024).decode("utf-8", "replace")
            except OSError:
                continue
            lines = [line.strip() for line in text.splitlines()
                     if any(key in line.lower() for key in
                            ("permission", "denied", u"权限", u"拒绝"))]
            for line in lines[:10]:
                issues.append({
                    "source": "scan_log", "scan_id": scan_id,
                    "message": line[:300],
                })
            if len(issues) >= 20:
                break
        return issues[:20]

    def _subtitle_permission_issues(self):
        if self.subtitles is None:
            return []
        try:
            data = self.subtitles.failures(limit=50, category="permission",
                                           include_skipped=True)
        except Exception:  # pragma: no cover
            return []
        issues = []
        for item in data.get("items") or []:
            issues.append({
                "source": "subtitle",
                "job_id": item.get("job_id"),
                "message": item.get("message") or u"字幕任务权限异常",
            })
        return issues

    def report(self):
        items = [self.check_path(source, path)
                 for source, path in self._candidate_paths()]
        by_status = {STATUS_OK: 0, STATUS_WARN: 0, STATUS_ERROR: 0}
        for item in items:
            by_status[item["status"]] = by_status.get(item["status"], 0) + 1
        issues = self._scan_permission_issues()
        issues.extend(self._subtitle_permission_issues())
        guidance = []
        if by_status[STATUS_ERROR] or issues:
            guidance.append(
                u"本向导只做检测与指引，不会自动修改权限/ACL（套件非 root 无法改 ACL）。")
            guidance.append(
                u"在 DSM「控制面板 → 共享文件夹 → 编辑 → 权限」为套件用户 "
                u"duplicatecleaner 授予“读写”；或用 File Station/SSH 修正目录属主。")
            guidance.append(
                u"若文件来自其他用户/套件写入，先确认该目录允许套件用户访问；"
                u"也可把任务目录移到已有权限的共享文件夹。")
        return {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "items": items,
            "summary": {
                "total": len(items),
                "ok": by_status.get(STATUS_OK, 0),
                "warn": by_status.get(STATUS_WARN, 0),
                "error": by_status.get(STATUS_ERROR, 0),
                "issues": len(issues),
            },
            "issues": issues,
            "guidance": guidance,
            "note": u"权限体检为只读检测；不提供一键提权（评估结论：非 root 无法改 ACL）。",
        }
