# -*- coding: utf-8 -*-
"""数据持久化：原子 JSON、扫描历史、删除任务、日志、审计。

所有写入使用“临时文件 + fsync + os.replace”原子替换，避免进程崩溃留下半个文件。
扫描结果（groups）与删除结果使用 JSONL 流式读写，避免一次性载入大结果集。
"""

from __future__ import absolute_import

import json
import os
import re
import tempfile
import threading
import time

from . import config
from .errors import ApiError, invalid, not_found
from .timeutil import iso_local, now_iso

AUDIT_MAX_BYTES = 5 * 1024 * 1024


class Store(object):
    def __init__(self, data_dir):
        self.data_dir = os.path.abspath(data_dir)
        self.scans_dir = os.path.join(self.data_dir, "scans")
        self.deletions_dir = os.path.join(self.data_dir, "deletions")
        self.plans_dir = os.path.join(self.data_dir, "plans")
        self.logs_dir = os.path.join(self.data_dir, "logs")
        self.tmp_dir = os.path.join(self.data_dir, "tmp")
        for path in (self.data_dir, self.scans_dir, self.deletions_dir,
                     self.plans_dir, self.logs_dir, self.tmp_dir):
            os.makedirs(path, exist_ok=True)
        self._settings_path = os.path.join(self.data_dir, "settings.json")
        self._audit_path = os.path.join(self.data_dir, "audit.jsonl")
        self._audit_lock = threading.Lock()
        self._job_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 基础原子读写
    # ------------------------------------------------------------------
    def _atomic_write(self, path, data):
        directory = os.path.dirname(path) or self.data_dir
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def write_json(self, path, obj):
        payload = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
        self._atomic_write(path, payload)

    def read_json(self, path, default=None):
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            return default
        try:
            return json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeError):
            return default

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------
    def load_settings(self):
        return self.read_json(self._settings_path, None)

    def save_settings(self, settings):
        self.write_json(self._settings_path, settings)

    # ------------------------------------------------------------------
    # 审计日志（append-only JSONL）
    # ------------------------------------------------------------------
    def audit(self, action, user, detail=None):
        entry = {
            "ts": now_iso(),
            "epoch": time.time(),
            "action": action,
            "user": user or "",
            "detail": detail or {},
        }
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        with self._audit_lock:
            try:
                self._rotate_audit_if_needed(len(line))
                with open(self._audit_path, "ab") as handle:
                    handle.write(line)
                    handle.flush()
            except OSError:
                pass
        return entry

    def _rotate_audit_if_needed(self, incoming):
        try:
            size = os.path.getsize(self._audit_path)
        except OSError:
            return
        if size + incoming <= AUDIT_MAX_BYTES:
            return
        backup = self._audit_path + ".1"
        try:
            os.replace(self._audit_path, backup)
        except OSError:
            pass

    def read_audit(self, limit=100, max_bytes=512 * 1024):
        limit = max(1, min(int(limit or 100), 1000))
        lines = self._tail_lines(self._audit_path, max_bytes)
        entries = []
        for raw in lines:
            try:
                entries.append(json.loads(raw.decode("utf-8")))
            except (ValueError, UnicodeError):
                continue
        return entries[-limit:]

    @staticmethod
    def _tail_lines(path, max_bytes):
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        with open(path, "rb") as handle:
            truncated = False
            if size > max_bytes:
                handle.seek(size - max_bytes)
                truncated = True
            data = handle.read()
        lines = [line for line in data.split(b"\n") if line.strip()]
        if truncated and lines:
            lines = lines[1:]
        return lines

    # ------------------------------------------------------------------
    # 运行日志（logs/ 目录）
    # ------------------------------------------------------------------
    def log_path(self, name):
        if not isinstance(name, str) or not config.SAFE_LOG_NAME_RE.match(name):
            raise invalid(u"日志文件名非法")
        return os.path.join(self.logs_dir, name)

    def write_log(self, name, text, append=False):
        path = self.log_path(name)
        mode = "ab" if append else "wb"
        data = text.encode("utf-8") if isinstance(text, str) else text
        with open(path, mode) as handle:
            handle.write(data)
            handle.flush()
        return path

    def list_logs(self, limit=200):
        result = []
        try:
            names = os.listdir(self.logs_dir)
        except OSError:
            return result
        for name in names:
            if not config.SAFE_LOG_NAME_RE.match(name):
                continue
            path = os.path.join(self.logs_dir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if name.startswith("scan-"):
                kind = "scan"
            elif name.startswith("delete-"):
                kind = "delete"
            else:
                kind = "other"
            scan_id = None
            job_id = None
            if kind == "scan":
                scan_id = name[len("scan-"):-len(".log")]
            elif kind == "delete":
                job_id = name[len("delete-"):-len(".log")]
            result.append({
                "name": name,
                "kind": kind,
                "size": int(st.st_size),
                "modified_at": iso_local(st.st_mtime),
                "modified_epoch": st.st_mtime,
                "scan_id": scan_id,
                "job_id": job_id,
            })
        result.sort(key=lambda item: item["modified_epoch"], reverse=True)
        return result[:max(1, int(limit or 200))]

    def resolve_log(self, name):
        path = self.log_path(name)
        if not os.path.isfile(path):
            raise not_found(u"日志不存在: %s" % name)
        return path

    # ------------------------------------------------------------------
    # 扫描结果
    # ------------------------------------------------------------------
    def _check_scan_id(self, scan_id):
        if not isinstance(scan_id, str) or not config.SCAN_ID_RE.match(scan_id):
            raise invalid(u"扫描 ID 非法")
        return scan_id

    def scan_dir(self, scan_id):
        return os.path.join(self.scans_dir, self._check_scan_id(scan_id))

    def _scan_meta_path(self, scan_id):
        return os.path.join(self.scan_dir(scan_id), "meta.json")

    def _scan_groups_path(self, scan_id):
        return os.path.join(self.scan_dir(scan_id), "groups.jsonl")

    def save_scan_meta(self, scan_id, meta):
        directory = self.scan_dir(scan_id)
        os.makedirs(directory, exist_ok=True)
        self.write_json(os.path.join(directory, "meta.json"), meta)

    def load_scan_meta(self, scan_id):
        return self.read_json(os.path.join(self.scan_dir(scan_id), "meta.json"), None)

    def list_scan_metas(self, limit=100):
        result = []
        try:
            names = os.listdir(self.scans_dir)
        except OSError:
            return result
        for name in names:
            if not config.SCAN_ID_RE.match(name):
                continue
            meta = self.read_json(os.path.join(self.scans_dir, name, "meta.json"), None)
            if isinstance(meta, dict):
                result.append(meta)
        result.sort(key=lambda item: item.get("created_epoch") or 0, reverse=True)
        return result[:max(1, int(limit or 100))]

    def write_scan_groups(self, scan_id, groups):
        """流式写 groups.jsonl（原子替换），返回写入组数。"""
        directory = self.scan_dir(scan_id)
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".groups-", suffix=".part", dir=directory)
        count = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                for group in groups:
                    handle.write((json.dumps(group, ensure_ascii=False) + "\n").encode("utf-8"))
                    count += 1
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, self._scan_groups_path(scan_id))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return count

    def append_scan_groups(self, scan_id, groups):
        """追加写（用于超大批量分批落盘），调用方保证只写一次或顺序写。"""
        directory = self.scan_dir(scan_id)
        os.makedirs(directory, exist_ok=True)
        count = 0
        path = self._scan_groups_path(scan_id)
        mode = "ab" if os.path.exists(path) else "wb"
        with open(path, mode) as handle:
            for group in groups:
                handle.write((json.dumps(group, ensure_ascii=False) + "\n").encode("utf-8"))
                count += 1
            handle.flush()
        return count

    def iter_scan_groups(self, scan_id):
        path = self._scan_groups_path(scan_id)
        try:
            handle = open(path, "rb")
        except OSError:
            return
        try:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    continue
        finally:
            handle.close()

    def delete_scan(self, scan_id):
        directory = self.scan_dir(scan_id)
        for name in ("groups.jsonl", "meta.json"):
            try:
                os.unlink(os.path.join(directory, name))
            except OSError:
                pass
        try:
            os.rmdir(directory)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 删除任务（meta + JSONL 结果）
    # ------------------------------------------------------------------
    def _check_job_id(self, job_id):
        if not isinstance(job_id, str) or not config.JOB_ID_RE.match(job_id):
            raise invalid(u"删除任务 ID 非法")
        return job_id

    def _job_meta_path(self, job_id):
        return os.path.join(self.deletions_dir, self._check_job_id(job_id) + ".meta.json")

    def _job_results_path(self, job_id):
        return os.path.join(self.deletions_dir, self._check_job_id(job_id) + ".jsonl")

    def save_job_meta(self, job_id, meta):
        self.write_json(self._job_meta_path(job_id), meta)

    def load_job_meta(self, job_id):
        return self.read_json(self._job_meta_path(job_id), None)

    def append_job_result(self, job_id, result):
        path = self._job_results_path(job_id)
        line = (json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8")
        with self._job_lock:
            with open(path, "ab") as handle:
                handle.write(line)
                handle.flush()

    def iter_job_results(self, job_id, limit=None):
        path = self._job_results_path(job_id)
        try:
            handle = open(path, "rb")
        except OSError:
            return
        produced = 0
        try:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    continue
                produced += 1
                if limit is not None and produced >= limit:
                    return
        finally:
            handle.close()

    def delete_job(self, job_id):
        for path in (self._job_meta_path(job_id), self._job_results_path(job_id)):
            try:
                os.unlink(path)
            except OSError:
                pass

    def list_job_metas(self, scan_id=None, limit=100):
        result = []
        try:
            names = os.listdir(self.deletions_dir)
        except OSError:
            return result
        for name in names:
            if not name.endswith(".meta.json"):
                continue
            meta = self.read_json(os.path.join(self.deletions_dir, name), None)
            if not isinstance(meta, dict):
                continue
            if scan_id is not None and meta.get("scan_id") != scan_id:
                continue
            result.append(meta)
        result.sort(key=lambda item: item.get("started_epoch") or 0, reverse=True)
        return result[:max(1, int(limit or 100))]

    # ------------------------------------------------------------------
    def temp_path(self, prefix="dc-", suffix=".tmp"):
        fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=self.tmp_dir)
        os.close(fd)
        return path
