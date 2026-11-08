# -*- coding: utf-8 -*-
"""数据持久化：SQLite 元数据（docs/STORAGE_CONTRACT.md）+ 文件型数据。

存 SQLite（<data>/dupcleaner.sqlite3）：
- 扫描 meta / 扫描分组（按 ordinal 流式读取）/ 删除任务 meta 与结果 /
  内容特征缓存 / 扫描 checkpoint / 字幕任务元数据。

仍存文件（保持启动兜底与运维可读）：
- settings.json、audit.jsonl、logs/、plans/、SRT/VTT 与导出文件。

兼容旧版本 JSON：
- 启动时把 scans/*/meta.json、groups.jsonl、deletions/*.meta.json、
  deletions/*.jsonl、feature_cache.json 幂等导入（INSERT OR IGNORE），
  不删除源文件；读取时 DB 优先，DB 为空才回退旧 JSON。
- 显式删除扫描/任务时同时清理对应旧 JSON 文件。
"""

from __future__ import absolute_import

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time

from . import config
from . import db as db_module
from .errors import ApiError, invalid, not_found
from .timeutil import iso_local, now_iso

logger = logging.getLogger("dupcleaner.store")

AUDIT_MAX_BYTES = 5 * 1024 * 1024

# 进程启动时需要处理的未完成扫描状态（不自动续跑）。
INTERRUPTED_STATUSES = ("queued", "running", "paused", "stopping")

# 可恢复暂停的断点阶段（docs/RESUME_CONTRACT.md §1/§3）。
RESUME_STAGES = ("enumerate", "size", "sample", "hash", "similarity",
                 "video", "finalize")

_MIGRATED_KEY = "migrated_v1"
_GROUP_BATCH = 200
_RESULT_BATCH = 200
_CACHE_BATCH = 500
_CHECKPOINT_RECORD_BATCH = 500


def sqlite_int(value):
    """把可能超出 SQLite INTEGER（有符号 64 位）的整数安全转换。

    Windows 的 st_ino 是 64 位无符号数，可能 > 2^63-1；直接绑定会抛
    OverflowError。这里按二进制补码映射到有符号区间，读写两侧使用同一映射，
    因此缓存键仍然稳定。
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    if number > 0x7FFFFFFFFFFFFFFF:
        number -= 1 << 64
    elif number < -0x8000000000000000:
        return 0
    return number


class Store(object):
    def __init__(self, data_dir):
        self.data_dir = os.path.abspath(data_dir)
        self.scans_dir = os.path.join(self.data_dir, "scans")
        self.deletions_dir = os.path.join(self.data_dir, "deletions")
        self.plans_dir = os.path.join(self.data_dir, "plans")
        self.logs_dir = os.path.join(self.data_dir, "logs")
        self.tmp_dir = os.path.join(self.data_dir, "tmp")
        self.backups_dir = os.path.join(self.data_dir, "backups")
        for path in (self.data_dir, self.scans_dir, self.deletions_dir,
                     self.plans_dir, self.logs_dir, self.tmp_dir):
            os.makedirs(path, exist_ok=True)
        self._settings_path = os.path.join(self.data_dir, "settings.json")
        self._audit_path = os.path.join(self.data_dir, "audit.jsonl")
        self._audit_lock = threading.Lock()
        self._job_lock = threading.Lock()
        # v5 FTS 查询会把命中的 group_id 物化到 TEMP 表（避免 IN 子查询被
        # 优化器改写成每行重算）；锁保证 TEMP 表在“物化+统计+分页”期间不被
        # 其它线程覆盖。
        self._fts_lock = threading.RLock()
        self._fts_temp_ready = False

        # 业务元数据统一 SQLite（WAL/短事务/RLock/损坏恢复见 db.Database）。
        self.db_path = os.path.join(self.data_dir, db_module.DB_FILENAME)
        self.db = db_module.Database(self.db_path)
        self._import_legacy()
        try:
            self.mark_interrupted_scans()
        except Exception:  # pragma: no cover - 启动标记失败不应阻断服务
            logger.warning(u"扫描中断标记失败", exc_info=True)

    # ------------------------------------------------------------------
    # 基础原子读写（settings/审计等文件数据）
    # ------------------------------------------------------------------
    def _atomic_write(self, path, data, mode=None):
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
            # 敏感文件在替换前就设好权限，避免出现短暂的宽权限窗口。
            if mode is not None:
                try:
                    os.chmod(tmp_path, mode)
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
        # settings.json 可能包含云端 ASR/LLM 密钥：读取时收紧为 600。
        config.secure_permissions(self._settings_path)
        return self.read_json(self._settings_path, None)

    def save_settings(self, settings):
        payload = json.dumps(settings, ensure_ascii=False, indent=1).encode("utf-8")
        self._atomic_write(self._settings_path, payload,
                           mode=config.SECURE_FILE_MODE)
        config.secure_permissions(self._settings_path)

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
                entry = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                continue
            if not isinstance(entry, dict):
                continue
            # v0038：稳定 id（原始行 sha1 前 16 位），旧记录也能用于删除
            entry["id"] = self._audit_line_id(raw)
            entries.append(entry)
        return entries[-limit:]

    @staticmethod
    def _audit_line_id(raw):
        return hashlib.sha1(raw).hexdigest()[:16]

    @staticmethod
    def _read_audit_lines(path):
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            return []
        return [line for line in data.split(b"\n") if line.strip()]

    def _write_audit_lines(self, path, lines):
        """原子重写审计文件（保留 600 权限）。"""
        directory = os.path.dirname(path)
        fd, temporary = tempfile.mkstemp(prefix=".audit-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                for line in lines:
                    handle.write(line + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def delete_audit_entries(self, user, ids=None, before_epoch=None,
                             keep_days=None):
        """v0038：删除审计事件（按 id 选中，或删除 before_epoch 之前的）。

        同时处理轮转文件 audit.jsonl.1；删除动作本身写一条 audit_delete 审计。
        返回删除条数。
        """
        wanted = set(str(item) for item in (ids or [])
                     if isinstance(item, (str, int)))
        if not wanted and before_epoch is None:
            raise invalid(u"缺少要删除的审计事件")
        removed = 0
        with self._audit_lock:
            for path in (self._audit_path, self._audit_path + ".1"):
                lines = self._read_audit_lines(path)
                if not lines:
                    continue
                kept = []
                for line in lines:
                    drop = False
                    if wanted:
                        drop = self._audit_line_id(line) in wanted
                    elif before_epoch is not None:
                        try:
                            entry = json.loads(line.decode("utf-8"))
                        except (ValueError, UnicodeError):
                            entry = None
                        try:
                            epoch = float(entry.get("epoch"))
                        except (AttributeError, TypeError, ValueError):
                            epoch = None
                        drop = (epoch is not None
                                and epoch < float(before_epoch))
                    if not drop:
                        kept.append(line)
                removed += len(lines) - len(kept)
                if not kept:
                    if path == self._audit_path:
                        self._write_audit_lines(path, [])
                    else:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                elif len(kept) != len(lines):
                    self._write_audit_lines(path, kept)
        if removed:
            detail = {"deleted": removed,
                      "mode": "ids" if wanted else "before_epoch"}
            if keep_days is not None:
                detail["keep_days"] = int(keep_days)
            self.audit("audit_delete", user, detail)
        return removed

    def delete_log_files(self, user, names=None, before_epoch=None,
                         keep_days=None):
        """v0038：删除运行日志文件（按文件名列表或 before_epoch 之前的）。

        文件名必须通过 SAFE_LOG_NAME_RE 校验（防目录穿越）；删除动作记审计。
        """
        names = [item for item in (names or []) if isinstance(item, str)]
        if not names and before_epoch is None:
            raise invalid(u"缺少要删除的日志文件")
        removed = 0
        deleted = []
        if names:
            for name in names:
                path = self.log_path(name)
                try:
                    if os.path.isfile(path):
                        os.unlink(path)
                        removed += 1
                        deleted.append(name)
                except OSError:
                    continue
        else:
            for item in self.list_logs(limit=10000):
                try:
                    older = float(item.get("modified_epoch") or 0) < float(before_epoch)
                except (TypeError, ValueError):
                    older = False
                if not older:
                    continue
                path = self.log_path(item.get("name"))
                try:
                    os.unlink(path)
                    removed += 1
                    deleted.append(item.get("name"))
                except OSError:
                    continue
        if removed:
            detail = {"deleted": removed,
                      "mode": "names" if names else "before_epoch",
                      "names": deleted[:20]}
            if keep_days is not None:
                detail["keep_days"] = int(keep_days)
            self.audit("log_delete", user, detail)
        return removed

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
    # ID 校验 / 旧 JSON 路径（兼容清理用）
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

    def _check_job_id(self, job_id):
        if not isinstance(job_id, str) or not config.JOB_ID_RE.match(job_id):
            raise invalid(u"删除任务 ID 非法")
        return job_id

    def _job_meta_path(self, job_id):
        return os.path.join(self.deletions_dir, self._check_job_id(job_id) + ".meta.json")

    def _job_results_path(self, job_id):
        return os.path.join(self.deletions_dir, self._check_job_id(job_id) + ".jsonl")

    # ------------------------------------------------------------------
    # 旧 JSON → SQLite 幂等增量迁移
    # ------------------------------------------------------------------
    @staticmethod
    def _legacy_marker_key(kind, item_id):
        return "legacy:%s:%s" % (kind, item_id)

    def _legacy_marker_get(self, kind, item_id, default=None):
        try:
            return self.db.schema_meta_get(
                self._legacy_marker_key(kind, item_id), default)
        except Exception:  # pragma: no cover
            return default

    def _legacy_marker_set(self, kind, item_id, value):
        try:
            self.db.schema_meta_set(
                self._legacy_marker_key(kind, item_id), str(value))
        except Exception:  # pragma: no cover
            pass

    def _legacy_imported(self, kind, item_id, size):
        return self._legacy_marker_get(kind, item_id) == str(int(size))

    def _mark_legacy_imported(self, kind, item_id, size):
        self._legacy_marker_set(kind, item_id, str(int(size)))

    def _import_legacy(self):
        """启动增量导入旧 JSON：幂等、不删源文件、DB 已存在的数据优先。

        旧版本回滚后可能重新写入 JSON，因此每次启动都做「便宜的差异检查」：
        只在扫描/任务不在 DB、或 JSON 文件大小与上次导入标记不同时才读取。
        """
        try:
            self._import_legacy_scans()
            self._import_legacy_deletions()
            self._import_legacy_feature_cache()
            self.db.schema_meta_set(_MIGRATED_KEY, "1")
        except Exception:  # pragma: no cover - 下次启动重试（INSERT OR IGNORE）
            logger.warning(u"旧 JSON 数据导入失败，将在下次启动重试", exc_info=True)

    def _import_legacy_scans(self):
        try:
            names = sorted(os.listdir(self.scans_dir))
        except OSError:
            return
        known = {row["scan_id"] for row in self.db.query(
            "SELECT scan_id FROM scans")}
        for name in names:
            if not config.SCAN_ID_RE.match(name):
                continue
            directory = os.path.join(self.scans_dir, name)
            if not os.path.isdir(directory):
                continue
            meta_path = os.path.join(directory, "meta.json")
            if os.path.isfile(meta_path) and name not in known:
                meta = self.read_json(meta_path, None)
                if isinstance(meta, dict):
                    meta = dict(meta)
                    meta.setdefault("scan_id", name)
                    self._insert_scan_meta_ignore(name, meta)
            groups_path = os.path.join(directory, "groups.jsonl")
            if not os.path.isfile(groups_path):
                continue
            try:
                size = os.path.getsize(groups_path)
            except OSError:
                continue
            if self._legacy_imported("groups", name, size):
                continue
            # 断点续导：从当前最大 ordinal+1 继续，已导入的行不会重复。
            self._import_scan_groups_file(name, groups_path)
            self._mark_legacy_imported("groups", name, size)

    def _insert_scan_meta_ignore(self, scan_id, meta):
        try:
            created = float(meta.get("created_epoch") or time.time())
        except (TypeError, ValueError):
            created = time.time()
        self.db.execute(
            "INSERT OR IGNORE INTO scans"
            "(scan_id, meta_json, created_epoch, updated_epoch, status, interrupted)"
            " VALUES(?, ?, ?, ?, ?, 0)",
            (scan_id, json.dumps(meta, ensure_ascii=False), created, time.time(),
             str(meta.get("status") or "unknown")))

    def _import_scan_groups_file(self, scan_id, path):
        start = int(self.db.scalar(
            "SELECT COALESCE(MAX(ordinal), -1) FROM scan_groups WHERE scan_id=?",
            (scan_id,), -1)) + 1
        try:
            handle = open(path, "rb")
        except OSError:
            return
        try:
            ordinal = 0
            batch = []
            file_batch = []
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                index = ordinal
                ordinal += 1
                if index < start:
                    continue
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                batch.append(self._group_row(scan_id, index, payload))
                file_batch.extend(self._group_file_rows(scan_id, index, payload))
                if len(batch) >= _GROUP_BATCH:
                    self._insert_scan_group_batch(batch, file_batch)
                    batch = []
                    file_batch = []
            if batch:
                self._insert_scan_group_batch(batch, file_batch)
        finally:
            handle.close()

    def _import_legacy_deletions(self):
        try:
            names = sorted(os.listdir(self.deletions_dir))
        except OSError:
            return
        known = {row["job_id"] for row in self.db.query(
            "SELECT job_id FROM deletion_jobs")}
        for name in names:
            if name.endswith(".meta.json"):
                job_id = name[:-len(".meta.json")]
                if not config.JOB_ID_RE.match(job_id):
                    continue
                if job_id in known:
                    continue
                meta = self.read_json(os.path.join(self.deletions_dir, name), None)
                if not isinstance(meta, dict):
                    continue
                meta = dict(meta)
                meta.setdefault("job_id", job_id)
                self._insert_job_meta_ignore(job_id, meta)
            elif name.endswith(".jsonl"):
                job_id = name[:-len(".jsonl")]
                if not config.JOB_ID_RE.match(job_id):
                    continue
                path = os.path.join(self.deletions_dir, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if self._legacy_imported("results", job_id, size):
                    continue
                self._import_job_results_file(job_id, path)
                self._mark_legacy_imported("results", job_id, size)

    def _insert_job_meta_ignore(self, job_id, meta):
        try:
            started = float(meta.get("started_epoch") or time.time())
        except (TypeError, ValueError):
            started = time.time()
        self.db.execute(
            "INSERT OR IGNORE INTO deletion_jobs"
            "(job_id, meta_json, started_epoch, status) VALUES(?, ?, ?, ?)",
            (job_id, json.dumps(meta, ensure_ascii=False), started,
             meta.get("status")))

    def _import_job_results_file(self, job_id, path):
        start = int(self.db.scalar(
            "SELECT COALESCE(MAX(ordinal), -1) FROM deletion_results WHERE job_id=?",
            (job_id,), -1)) + 1
        try:
            handle = open(path, "rb")
        except OSError:
            return
        try:
            ordinal = 0
            batch = []
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                index = ordinal
                ordinal += 1
                if index < start:
                    continue
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                batch.append((job_id, index,
                              json.dumps(payload, ensure_ascii=False)))
                if len(batch) >= _RESULT_BATCH:
                    self._insert_job_result_batch(batch)
                    batch = []
            if batch:
                self._insert_job_result_batch(batch)
        finally:
            handle.close()

    def _import_legacy_feature_cache(self):
        path = os.path.join(self.data_dir, config.SIMILARITY_CACHE_FILENAME)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        marker = self._legacy_marker_get("feature_cache", "json", "")
        try:
            if marker and float(marker) >= float(mtime):
                return
        except (TypeError, ValueError):
            pass
        payload = self.read_json(path, None)
        if not isinstance(payload, dict):
            return
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            return
        base = time.time()
        batch = []
        for index, key in enumerate(entries):
            value = entries[key]
            if not isinstance(value, dict):
                continue
            kind = value.get("kind")
            if not kind:
                feature = value.get("feature")
                if isinstance(feature, dict):
                    kind = feature.get("kind")
            batch.append((str(key), kind,
                          json.dumps(value, ensure_ascii=False),
                          base + index * 1e-6))
            if len(batch) >= _CACHE_BATCH:
                self._insert_feature_cache_batch(batch)
                batch = []
        if batch:
            self._insert_feature_cache_batch(batch)
        self._legacy_marker_set("feature_cache", "json", repr(float(mtime)))

    def _insert_feature_cache_batch(self, rows):
        with self.db.transaction():
            self.db.executemany(
                "INSERT OR IGNORE INTO feature_cache"
                "(cache_key, kind, payload_json, last_used) VALUES(?, ?, ?, ?)",
                rows)

    # ------------------------------------------------------------------
    # 扫描 meta（SQLite，读 DB 优先、空则回退旧 JSON）
    # ------------------------------------------------------------------
    def save_scan_meta(self, scan_id, meta):
        scan_id = self._check_scan_id(scan_id)
        if not isinstance(meta, dict):
            raise invalid(u"扫描元数据必须是 JSON 对象")
        try:
            created = float(meta.get("created_epoch") or time.time())
        except (TypeError, ValueError):
            created = time.time()
        status = str(meta.get("status") or "unknown")
        interrupted = 1 if meta.get("interrupted") else 0
        self.db.execute(
            "INSERT OR REPLACE INTO scans"
            "(scan_id, meta_json, created_epoch, updated_epoch, status, interrupted)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (scan_id, json.dumps(meta, ensure_ascii=False), created,
             time.time(), status, interrupted))

    def load_scan_meta(self, scan_id):
        scan_id = self._check_scan_id(scan_id)
        row = self.db.query_one(
            "SELECT meta_json FROM scans WHERE scan_id=?", (scan_id,))
        if row is not None:
            meta = self._loads(row["meta_json"], None)
            if isinstance(meta, dict):
                meta.setdefault("scan_id", scan_id)
                return meta
        return self.read_json(self._scan_meta_path(scan_id), None)

    def list_scan_metas(self, limit=100):
        limit = max(1, int(limit or 100))
        rows = self.db.query(
            "SELECT scan_id, meta_json FROM scans"
            " ORDER BY created_epoch DESC, scan_id DESC LIMIT ?", (limit,))
        result = []
        for row in rows:
            meta = self._loads(row["meta_json"], None)
            if isinstance(meta, dict):
                meta.setdefault("scan_id", row["scan_id"])
                result.append(meta)
        if result:
            return result
        # DB 为空：回退旧 JSON（首次导入失败/极旧数据目录）。
        legacy = []
        try:
            names = os.listdir(self.scans_dir)
        except OSError:
            return legacy
        for name in names:
            if not config.SCAN_ID_RE.match(name):
                continue
            meta = self.read_json(os.path.join(self.scans_dir, name, "meta.json"),
                                  None)
            if isinstance(meta, dict):
                legacy.append(meta)
        legacy.sort(key=lambda item: item.get("created_epoch") or 0, reverse=True)
        return legacy[:limit]

    def delete_scan(self, scan_id):
        scan_id = self._check_scan_id(scan_id)
        with self.db.transaction():
            self.db.execute("DELETE FROM scan_groups WHERE scan_id=?", (scan_id,))
            self.db.execute("DELETE FROM scan_files WHERE scan_id=?", (scan_id,))
            self.db.execute(
                "DELETE FROM scan_file_deletions WHERE scan_id=?", (scan_id,))
            self.db.execute("DELETE FROM scan_checkpoints WHERE scan_id=?", (scan_id,))
            self.db.execute(
                "DELETE FROM scan_checkpoint_records WHERE scan_id=?",
                (scan_id,))
            self.db.execute("DELETE FROM scans WHERE scan_id=?", (scan_id,))
        # 同时清理旧版 JSON 文件（如存在）。
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
    # 扫描分组（按 ordinal 流式读取；v2 索引列 + scan_files 支撑 SQL 分页）
    # ------------------------------------------------------------------
    _GROUP_COLUMNS = ("scan_id, ordinal, group_id, match_type, payload_json,"
                      " wasted_bytes, size, similarity_score, created_epoch,"
                      " group_hash, new_files, changed_files")
    _GROUP_INSERT_SQL = ("INSERT OR IGNORE INTO scan_groups("
                         "scan_id, ordinal, group_id, match_type, payload_json,"
                         " wasted_bytes, size, similarity_score, created_epoch,"
                         " group_hash, new_files, changed_files)"
                         " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
    _GROUP_REPLACE_SQL = ("INSERT OR REPLACE INTO scan_groups("
                          "scan_id, ordinal, group_id, match_type, payload_json,"
                          " wasted_bytes, size, similarity_score, created_epoch,"
                          " group_hash, new_files, changed_files)"
                          " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
    # v5：scan_files 不再保存 payload_json（group payload 才是真源）。
    _FILE_INSERT_SQL = ("INSERT OR IGNORE INTO scan_files("
                        "scan_id, group_id, file_id, path, size, is_keep,"
                        " default_selected) VALUES(?, ?, ?, ?, ?, ?, ?)")
    _PAGE_SORTS = {
        "wasted_desc": "g.wasted_bytes DESC, g.size DESC, g.group_id DESC",
        "size_desc": "g.size DESC, g.wasted_bytes DESC, g.group_id DESC",
        "created_desc": ("g.created_epoch DESC, g.wasted_bytes DESC,"
                         " g.group_id DESC"),
        "similarity_desc": ("g.similarity_score DESC, g.wasted_bytes DESC,"
                            " g.size DESC, g.group_id DESC"),
    }

    @staticmethod
    def _group_row(scan_id, ordinal, payload):
        meta = db_module.group_index_columns(payload, ordinal)
        return (scan_id, int(ordinal), meta["group_id"], meta["match_type"],
                json.dumps(payload, ensure_ascii=False),
                meta["wasted_bytes"], meta["size"], meta["similarity_score"],
                meta["created_epoch"], meta["group_hash"],
                int(meta.get("new_files") or 0),
                int(meta.get("changed_files") or 0))

    @staticmethod
    def _group_file_rows(scan_id, ordinal, payload):
        return db_module.group_file_rows(scan_id, ordinal, payload)

    def _insert_scan_group_batch(self, rows, file_rows=None, replace=False):
        with self.db.transaction():
            sql = self._GROUP_REPLACE_SQL if replace else self._GROUP_INSERT_SQL
            self.db.executemany(sql, rows)
            if file_rows:
                self.db.executemany(self._FILE_INSERT_SQL, file_rows)

    def write_scan_groups(self, scan_id, groups):
        """整组原子替换（事务内 DELETE + 分批 INSERT），返回写入组数。"""
        scan_id = self._check_scan_id(scan_id)
        count = 0
        batch = []
        file_batch = []
        with self.db.transaction():
            self.db.execute("DELETE FROM scan_groups WHERE scan_id=?", (scan_id,))
            self.db.execute("DELETE FROM scan_files WHERE scan_id=?", (scan_id,))
            for group in groups:
                if not isinstance(group, dict):
                    continue
                batch.append(self._group_row(scan_id, count, group))
                file_batch.extend(self._group_file_rows(scan_id, count, group))
                count += 1
                if len(batch) >= _GROUP_BATCH:
                    self.db.executemany(self._GROUP_REPLACE_SQL, batch)
                    if file_batch:
                        self.db.executemany(self._FILE_INSERT_SQL, file_batch)
                    batch = []
                    file_batch = []
            if batch:
                self.db.executemany(self._GROUP_REPLACE_SQL, batch)
            if file_batch:
                self.db.executemany(self._FILE_INSERT_SQL, file_batch)
        return count

    def append_scan_groups(self, scan_id, groups):
        """追加写（超大批量分批落盘），ordinal 从当前最大值继续。"""
        scan_id = self._check_scan_id(scan_id)
        with self._job_lock:
            start = int(self.db.scalar(
                "SELECT COALESCE(MAX(ordinal), -1) FROM scan_groups WHERE scan_id=?",
                (scan_id,), -1)) + 1
            count = 0
            batch = []
            file_batch = []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                batch.append(self._group_row(scan_id, start + count, group))
                file_batch.extend(
                    self._group_file_rows(scan_id, start + count, group))
                count += 1
                if len(batch) >= _GROUP_BATCH:
                    self._insert_scan_group_batch(batch, file_batch)
                    batch = []
                    file_batch = []
            if batch:
                self._insert_scan_group_batch(batch, file_batch)
            return count

    def iter_scan_groups(self, scan_id):
        """按 ordinal 升序流式迭代；DB 为空时回退旧 groups.jsonl。

        v0032：删除成功标记过的文件会从组内剔除；剩余文件 <2 的组跳过。
        """
        scan_id = self._check_scan_id(scan_id)
        deleted = self.load_deleted_paths(scan_id)
        first = self.db.query(
            "SELECT ordinal, payload_json FROM scan_groups"
            " WHERE scan_id=? ORDER BY ordinal ASC LIMIT ?",
            (scan_id, _GROUP_BATCH))
        if not first:
            for group in self._iter_legacy_scan_groups(scan_id, deleted):
                yield group
            return
        rows = first
        last = -1
        while rows:
            for row in rows:
                payload = self._loads(row["payload_json"], None)
                last = int(row["ordinal"])
                if isinstance(payload, dict):
                    if deleted:
                        payload = self.filter_group_payload(payload, deleted)
                        if payload is None:
                            continue
                    yield payload
            if len(rows) < _GROUP_BATCH:
                return
            rows = self.db.query(
                "SELECT ordinal, payload_json FROM scan_groups"
                " WHERE scan_id=? AND ordinal>? ORDER BY ordinal ASC LIMIT ?",
                (scan_id, last, _GROUP_BATCH))

    def _iter_legacy_scan_groups(self, scan_id, deleted=None):
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
                payload = self._loads(raw, None)
                if isinstance(payload, dict):
                    if deleted:
                        payload = self.filter_group_payload(payload, deleted)
                        if payload is None:
                            continue
                    yield payload
        finally:
            handle.close()

    @staticmethod
    def filter_group_payload(payload, deleted_paths):
        """剔除已删除文件并重算保留项/浪费字节；剩余 <2 个文件返回 None。

        payload 是本次加载的独立 dict（调用方不复用），文件 dict 可原地更新。
        """
        if not isinstance(payload, dict):
            return None
        files = []
        for info in payload.get("files") or []:
            if not isinstance(info, dict):
                continue
            path = info.get("path")
            if isinstance(path, str) and path in deleted_paths:
                continue
            files.append(info)
        if len(files) < 2:
            return None
        group = dict(payload)
        group["files"] = files
        keeper = None
        for info in files:
            if info.get("is_keep") is True:
                keeper = info
                break
        if keeper is None:
            # 保留文件被删 → 从剩余文件中重新指定保留项（不默认勾选删除）
            keeper = files[0]
            for info in files:
                info["is_keep"] = info is keeper
                if info is keeper:
                    info["default_selected"] = False
        group["keep_file_id"] = keeper.get("file_id")
        group["count"] = len(files)
        sizes = []
        for info in files:
            try:
                sizes.append(int(info.get("size") or 0))
            except (TypeError, ValueError):
                sizes.append(0)
        wasted = 0
        for info, size in zip(files, sizes):
            if info is keeper:
                continue
            wasted += size
        group["size"] = max(sizes) if sizes else 0
        group["wasted_bytes"] = wasted
        return group

    @staticmethod
    def _remaining_files_sql(alias="g"):
        """组内剩余文件数（排除删除标记）；用于过滤只剩 <2 个文件的组。"""
        return ("(SELECT COUNT(*) FROM scan_files f2"
                " WHERE f2.scan_id = " + alias + ".scan_id"
                " AND f2.group_id = " + alias + ".group_id"
                " AND NOT EXISTS (SELECT 1 FROM scan_file_deletions d"
                " WHERE d.scan_id = f2.scan_id AND d.path = f2.path))")

    def load_deleted_paths(self, scan_id):
        """已删除文件路径集合（v0032：删除成功后写入，读取时剔除）。"""
        scan_id = self._check_scan_id(scan_id)
        rows = self.db.query(
            "SELECT path FROM scan_file_deletions WHERE scan_id=?", (scan_id,))
        return {str(row["path"]) for row in rows if row["path"]}

    def count_scan_file_deletions(self, scan_id):
        scan_id = self._check_scan_id(scan_id)
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM scan_file_deletions WHERE scan_id=?",
            (scan_id,), 0) or 0)

    def mark_scan_files_deleted(self, scan_id, entries):
        """记录删除成功（或已不存在）的文件，供分组/统计/导出读取时剔除。

        entries: dict/{path, job_id?, mode?} 或 (path, job_id, mode) 序列。
        """
        scan_id = self._check_scan_id(scan_id)
        now = time.time()
        rows = []
        for entry in entries or []:
            job_id = mode = None
            if isinstance(entry, dict):
                path = entry.get("path")
                job_id = entry.get("job_id")
                mode = entry.get("mode")
            elif isinstance(entry, (tuple, list)) and entry:
                path = entry[0]
                if len(entry) > 1:
                    job_id = entry[1]
                if len(entry) > 2:
                    mode = entry[2]
            else:
                continue
            if not isinstance(path, str) or not path:
                continue
            rows.append((scan_id, path, job_id, mode, now))
        if not rows:
            return 0
        with self.db.transaction():
            self.db.executemany(
                "INSERT OR REPLACE INTO scan_file_deletions"
                "(scan_id, path, job_id, mode, deleted_epoch)"
                " VALUES(?, ?, ?, ?, ?)", rows)
        return len(rows)

    def count_scan_groups(self, scan_id):
        scan_id = self._check_scan_id(scan_id)
        if not self.load_deleted_paths(scan_id):
            return int(self.db.scalar(
                "SELECT COUNT(*) FROM scan_groups WHERE scan_id=?",
                (scan_id,), 0) or 0)
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM scan_groups g WHERE g.scan_id=? AND "
            + self._remaining_files_sql("g") + " >= 2", (scan_id,), 0) or 0)

    def count_scan_files(self, scan_id):
        scan_id = self._check_scan_id(scan_id)
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM scan_files f WHERE f.scan_id=?"
            " AND NOT EXISTS (SELECT 1 FROM scan_file_deletions d"
            " WHERE d.scan_id = f.scan_id AND d.path = f.path)",
            (scan_id,), 0) or 0)

    @staticmethod
    def _escape_like(value):
        return (value.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_"))

    def query_scan_groups_page(self, scan_id, offset=0, limit=100, needle="",
                               sort="wasted_desc", match_type=None,
                               use_fts=None, new_only=False):
        """SQL 分页/搜索/排序（docs/STORAGE_CONTRACT.md §4/§6）。

        路径搜索：needle >= 3 且 FTS5(trigram) 可用时走 scan_files_fts MATCH
        短语查询；否则沿用 LIKE '%needle%'（短词/不支持 FTS 时的兼容语义）。
        group_hash 条件始终 LIKE。运行时 FTS 查询报错只在本连接降级 LIKE，
        不崩溃、不影响主数据。

        use_fts=False 可强制 LIKE（基准脚本对比用）；默认 None 自动选择。
        返回与旧内存实现相同的响应字段；DB 无分组或未回填 scan_files 时返回
        None，由 scanner 回退旧 JSON/内存实现（兼容 v1 数据与旧 JSON）。
        """
        scan_id = self._check_scan_id(scan_id)
        groups_total = self.count_scan_groups(scan_id)
        raw_files = int(self.db.scalar(
            "SELECT COUNT(*) FROM scan_files WHERE scan_id=?", (scan_id,), 0) or 0)
        if groups_total == 0 and raw_files == 0:
            return None
        try:
            offset = max(0, int(offset or 0))
            limit = max(0, int(limit if limit is not None else 100))
        except (TypeError, ValueError):
            return None
        deleted = self.load_deleted_paths(scan_id)
        remaining_sql = self._remaining_files_sql("g")
        # 仅在有删除标记时按“剩余文件 >=2”过滤组，保持旧扫描结果语义不变。
        group_filter = (" AND " + remaining_sql + " >= 2") if deleted else ""
        type_expr = "COALESCE(NULLIF(g.match_type, ''), 'exact')"
        counts = {}
        for row in self.db.query(
                "SELECT " + type_expr + " AS mt, COUNT(*) AS count"
                " FROM scan_groups g WHERE g.scan_id=?"
                + group_filter + " GROUP BY mt", (scan_id,)):
            counts[str(row["mt"] or "exact")] = int(row["count"])
        similar_total = int(counts.get("similar", 0))
        video_total = int(counts.get("video", 0))
        exact_total = max(0, groups_total - similar_total - video_total)

        needle = (needle or "").strip().lower()
        fts_mode = False
        if needle and len(needle) >= db_module.FTS_MIN_NEEDLE:
            if use_fts is False:
                fts_mode = False
            elif self.db.fts_enabled:
                fts_mode = True

        def build_where(with_fts):
            where = ["g.scan_id = ?"]
            if deleted:
                where.append(remaining_sql + " >= 2")
            params = [scan_id]
            if match_type == "exact":
                where.append(type_expr + " NOT IN ('similar', 'video')")
            elif match_type in ("similar", "video"):
                where.append(type_expr + " = ?")
                params.append(match_type)
            if new_only:
                # v0043：只看“本次扫描新增/内容变化”的重复组
                where.append("(g.new_files > 0 OR g.changed_files > 0)")
            if needle:
                pattern = "%" + self._escape_like(needle) + "%"
                hash_like = ("lower(COALESCE(g.group_hash, ''))"
                             " LIKE ? ESCAPE '\\'")
                if with_fts:
                    # FTS5 外部内容索引：MATCH 短语（trigram 子串），
                    # 只用于 scan_files.path；group hash 仍用 LIKE。
                    # 命中 group_id 已由 _materialize_fts_matches 物化到
                    # TEMP 表，避免 IN 子查询被改写成逐组重算（10k 组会卡住）。
                    where.append(
                        "(" + hash_like
                        + " OR g.group_id IN ("
                        "  SELECT group_id FROM dc_fts_matches))")
                    params.append(pattern)
                else:
                    where.append(
                        "(" + hash_like
                        + " OR g.group_id IN ("
                        "  SELECT f.group_id FROM scan_files f"
                        "  WHERE f.scan_id = ?"
                        "  AND lower(f.path) LIKE ? ESCAPE '\\'))")
                    params.extend([pattern, scan_id, pattern])
            return " AND ".join(where), tuple(params)

        def run_queries(with_fts):
            where_sql, query_params = build_where(with_fts)
            total = int(self.db.scalar(
                "SELECT COUNT(*) FROM scan_groups g WHERE " + where_sql,
                query_params, 0) or 0)
            rows = self.db.query(
                "SELECT g.payload_json FROM scan_groups g WHERE " + where_sql
                + " ORDER BY " + order_sql + " LIMIT ? OFFSET ?",
                query_params + (limit, offset))
            return total, rows

        order_sql = self._PAGE_SORTS.get(sort) or self._PAGE_SORTS["wasted_desc"]
        try:
            if fts_mode:
                with self._fts_lock:
                    self._materialize_fts_matches(scan_id, needle)
                    total, rows = run_queries(True)
            else:
                total, rows = run_queries(False)
        except sqlite3.Error as exc:
            if not fts_mode:
                raise
            # 运行时 FTS 异常：仅本连接降级 LIKE，不影响主数据/其它查询。
            self.db.disable_fts(str(exc))
            total, rows = run_queries(False)
        groups = []
        for row in rows:
            payload = self._loads(row["payload_json"], None)
            if isinstance(payload, dict):
                if deleted:
                    payload = self.filter_group_payload(payload, deleted)
                    if payload is None:
                        continue
                groups.append(payload)
        try:
            new_total = int(self.db.scalar(
                "SELECT COUNT(*) FROM scan_groups g WHERE g.scan_id=?"
                " AND (g.new_files > 0 OR g.changed_files > 0)",
                (scan_id,), 0) or 0)
        except sqlite3.Error:  # pragma: no cover - 旧库列缺失时忽略
            new_total = 0
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "match_type": match_type,
            "exact_total": exact_total,
            "similar_total": similar_total,
            "video_total": video_total,
            "new_only": bool(new_only),
            "new_total": new_total,
            "groups": groups,
        }

    def _materialize_fts_matches(self, scan_id, needle):
        """用 FTS5 MATCH 一次性取出命中的 group_id，写入连接级 TEMP 表。

        直接在主查询里写 IN (SELECT ... MATCH ...) 会被 SQLite 优化器改写成
        按 group 逐行重算（10k 组时非常慢）；物化后 COUNT/分页都走索引。
        """
        # 连接级 TEMP 表可能因数据库损坏恢复（重开连接）而丢失，每次都做
        # 一次 IF NOT EXISTS 兜底；代价可忽略，避免恢复后 FTS 永久降级。
        self.db.execute(
            "CREATE TEMP TABLE IF NOT EXISTS dc_fts_matches"
            " (group_id TEXT PRIMARY KEY)")
        self._fts_temp_ready = True
        phrase = db_module.fts_path_phrase(needle)
        with self.db.transaction():
            self.db.execute("DELETE FROM dc_fts_matches")
            self.db.execute(
                "INSERT OR IGNORE INTO dc_fts_matches(group_id)"
                " SELECT f.group_id FROM scan_files f"
                " WHERE f.scan_id = ? AND f.rowid IN ("
                "  SELECT rowid FROM scan_files_fts"
                "  WHERE scan_files_fts MATCH ?)"
                " AND NOT EXISTS (SELECT 1 FROM scan_file_deletions d"
                "  WHERE d.scan_id = f.scan_id AND d.path = f.path)",
                (scan_id, phrase))
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM dc_fts_matches", (), 0) or 0)

    def scan_file_stats(self, scan_id):
        """扩展名/目录统计用明细（不加载 group payload；group size 语义同旧版）。"""
        scan_id = self._check_scan_id(scan_id)
        base = ("SELECT f.path AS path, g.size AS group_size FROM scan_files f"
                " JOIN scan_groups g ON g.scan_id = f.scan_id"
                " AND g.group_id = f.group_id WHERE f.scan_id = ?")
        if not self.load_deleted_paths(scan_id):
            rows = self.db.query(base, (scan_id,))
        else:
            rows = self.db.query(
                base
                + " AND NOT EXISTS (SELECT 1 FROM scan_file_deletions d"
                " WHERE d.scan_id = f.scan_id AND d.path = f.path)"
                + " AND (SELECT COUNT(*) FROM scan_files f2"
                " WHERE f2.scan_id = f.scan_id AND f2.group_id = f.group_id"
                " AND NOT EXISTS (SELECT 1 FROM scan_file_deletions d2"
                "  WHERE d2.scan_id = f2.scan_id AND d2.path = f2.path)) >= 2",
                (scan_id,))
        return [(row["path"], int(row["group_size"] or 0)) for row in rows]

    # ------------------------------------------------------------------
    # 删除任务 meta + 结果
    # ------------------------------------------------------------------
    def save_job_meta(self, job_id, meta):
        job_id = self._check_job_id(job_id)
        if not isinstance(meta, dict):
            raise invalid(u"删除任务元数据必须是 JSON 对象")
        try:
            started = float(meta.get("started_epoch") or time.time())
        except (TypeError, ValueError):
            started = time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO deletion_jobs"
            "(job_id, meta_json, started_epoch, status) VALUES(?, ?, ?, ?)",
            (job_id, json.dumps(meta, ensure_ascii=False), started,
             meta.get("status")))

    def load_job_meta(self, job_id):
        job_id = self._check_job_id(job_id)
        row = self.db.query_one(
            "SELECT meta_json FROM deletion_jobs WHERE job_id=?", (job_id,))
        if row is not None:
            meta = self._loads(row["meta_json"], None)
            if isinstance(meta, dict):
                meta.setdefault("job_id", job_id)
                return meta
        return self.read_json(self._job_meta_path(job_id), None)

    def append_job_result(self, job_id, result):
        job_id = self._check_job_id(job_id)
        payload = json.dumps(result, ensure_ascii=False)
        with self._job_lock:
            with self.db.transaction():
                self.db.execute(
                    "INSERT INTO deletion_results(job_id, ordinal, payload_json)"
                    " VALUES(?, COALESCE((SELECT MAX(ordinal)+1 FROM deletion_results"
                    " WHERE job_id=?), 0), ?)", (job_id, job_id, payload))

    def iter_job_results(self, job_id, limit=None):
        job_id = self._check_job_id(job_id)
        remaining = None if limit is None else max(0, int(limit))
        if remaining is not None and remaining <= 0:
            return
        first = self.db.query(
            "SELECT ordinal, payload_json FROM deletion_results"
            " WHERE job_id=? ORDER BY ordinal ASC LIMIT ?",
            (job_id, _RESULT_BATCH))
        if not first:
            for result in self._iter_legacy_job_results(job_id, remaining):
                yield result
            return
        rows = first
        last = -1
        produced = 0
        while rows:
            for row in rows:
                last = int(row["ordinal"])
                payload = self._loads(row["payload_json"], None)
                if not isinstance(payload, dict):
                    continue
                yield payload
                produced += 1
                if remaining is not None and produced >= remaining:
                    return
            if len(rows) < _RESULT_BATCH:
                return
            want = _RESULT_BATCH
            if remaining is not None:
                want = min(want, remaining - produced)
                if want <= 0:
                    return
            rows = self.db.query(
                "SELECT ordinal, payload_json FROM deletion_results"
                " WHERE job_id=? AND ordinal>? ORDER BY ordinal ASC LIMIT ?",
                (job_id, last, want))

    def _iter_legacy_job_results(self, job_id, limit=None):
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
                payload = self._loads(raw, None)
                if not isinstance(payload, dict):
                    continue
                yield payload
                produced += 1
                if limit is not None and produced >= limit:
                    return
        finally:
            handle.close()

    def _insert_job_result_batch(self, rows):
        with self.db.transaction():
            self.db.executemany(
                "INSERT OR IGNORE INTO deletion_results"
                "(job_id, ordinal, payload_json) VALUES(?, ?, ?)", rows)

    def delete_job(self, job_id):
        job_id = self._check_job_id(job_id)
        with self.db.transaction():
            self.db.execute("DELETE FROM deletion_results WHERE job_id=?", (job_id,))
            self.db.execute("DELETE FROM deletion_jobs WHERE job_id=?", (job_id,))
        for path in (self._job_meta_path(job_id), self._job_results_path(job_id)):
            try:
                os.unlink(path)
            except OSError:
                pass

    def list_job_metas(self, scan_id=None, limit=100):
        limit = max(1, int(limit or 100))
        rows = self.db.query(
            "SELECT job_id, meta_json, started_epoch FROM deletion_jobs"
            " ORDER BY started_epoch DESC, job_id DESC")
        result = []
        for row in rows:
            meta = self._loads(row["meta_json"], None)
            if not isinstance(meta, dict):
                continue
            meta.setdefault("job_id", row["job_id"])
            if scan_id is not None and meta.get("scan_id") != scan_id:
                continue
            result.append(meta)
            if len(result) >= limit:
                break
        if result:
            return result
        # DB 为空：回退旧 deletions/*.meta.json。
        legacy = []
        try:
            names = os.listdir(self.deletions_dir)
        except OSError:
            return legacy
        for name in names:
            if not name.endswith(".meta.json"):
                continue
            meta = self.read_json(os.path.join(self.deletions_dir, name), None)
            if not isinstance(meta, dict):
                continue
            if scan_id is not None and meta.get("scan_id") != scan_id:
                continue
            legacy.append(meta)
        legacy.sort(key=lambda item: item.get("started_epoch") or 0, reverse=True)
        return legacy[:limit]

    # ------------------------------------------------------------------
    # 诊断 / 备份 / 维护（v0021 P0）
    # ------------------------------------------------------------------
    DIAGNOSTIC_TABLES = ("scans", "scan_groups", "scan_files",
                         "feature_cache", "subtitle_jobs", "subtitle_batches")

    def database_info(self):
        """数据库路径/尺寸/schema 版本（只读；不做 checkpoint 以免影响运行）。"""
        stats = self.db.file_stats()
        return {
            "path": stats["path"],
            "size": stats["size"],
            "wal_size": stats["wal_size"],
            "shm_size": stats["shm_size"],
            "total_size": stats["total_size"],
            "schema_version": self.db.schema_version(),
        }

    def table_row_counts(self, tables=None):
        return self.db.table_row_counts(tables or self.DIAGNOSTIC_TABLES)

    def backup_database(self):
        """用 sqlite3 在线备份 API 生成 <var>/backups/dupcleaner-<ts>.sqlite3。

        备份文件权限 600，是独立可打开的数据库（含 WAL 中已提交内容）。
        """
        config.ensure_private_dir(self.backups_dir)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = "dupcleaner-%s" % stamp
        filename = base + ".sqlite3"
        path = os.path.join(self.backups_dir, filename)
        index = 1
        while os.path.exists(path):
            filename = "%s-%d.sqlite3" % (base, index)
            path = os.path.join(self.backups_dir, filename)
            index += 1
        self.db.backup_to(path)
        try:
            size = int(os.path.getsize(path))
        except OSError:
            size = 0
        return {
            "filename": filename,
            "path": path,
            "size": size,
            "schema_version": self.db.schema_version(),
            "created_at": now_iso(),
        }

    def maintenance(self, vacuum=False):
        """WAL checkpoint(TRUNCATE) + PRAGMA optimize；可选 VACUUM（先检查空间）。"""
        before = self.db.file_stats()
        if vacuum:
            try:
                free = int(shutil.disk_usage(self.data_dir).free)
            except OSError:
                free = 0
            required = max(int(before["total_size"]) * 2,
                           16 * 1024 * 1024)
            if free < required:
                raise ApiError(
                    "INSUFFICIENT_SPACE",
                    u"磁盘剩余空间不足，无法执行 VACUUM",
                    details={"free_bytes": free, "required_bytes": required})
        self.db.wal_checkpoint("TRUNCATE")
        self.db.optimize()
        vacuumed = False
        if vacuum:
            self.db.vacuum()
            vacuumed = True
            self.db.wal_checkpoint("TRUNCATE")
        after = self.db.file_stats()
        return {
            "checkpoint": "truncate",
            "optimized": True,
            "vacuumed": vacuumed,
            "before": {
                "db_size": before["size"],
                "wal_size": before["wal_size"],
                "shm_size": before["shm_size"],
                "total_size": before["total_size"],
            },
            "after": {
                "db_size": after["size"],
                "wal_size": after["wal_size"],
                "shm_size": after["shm_size"],
                "total_size": after["total_size"],
            },
            "reclaimed_bytes": max(0, int(before["total_size"])
                                   - int(after["total_size"])),
        }

    # ------------------------------------------------------------------
    # 内容特征缓存（SQLite；FeatureCache 通过这里持久化）
    # ------------------------------------------------------------------
    def feature_cache_get(self, cache_key):
        row = self.db.query_one(
            "SELECT payload_json FROM feature_cache WHERE cache_key=?",
            (str(cache_key),))
        if row is None:
            return None
        return self._loads(row["payload_json"], None)

    def feature_cache_put(self, cache_key, kind, payload, epoch=None):
        self.feature_cache_put_many([(cache_key, kind, payload)], epoch=epoch)
        return True

    def _next_cache_epoch(self, epoch=None):
        """保证 last_used 严格递增（Windows 时钟粒度约 15ms，不能只靠 time()）。"""
        try:
            current = float(self.db.scalar(
                "SELECT COALESCE(MAX(last_used), 0) FROM feature_cache",
                (), 0.0) or 0.0)
        except (TypeError, ValueError):
            current = 0.0
        seed = time.time() if epoch is None else float(epoch)
        return max(seed, current + 1e-6)

    def feature_cache_put_many(self, items, epoch=None):
        items = list(items)
        if not items:
            return 0
        with self.db.transaction():
            base = self._next_cache_epoch(epoch)
            rows = []
            for index, item in enumerate(items):
                cache_key, kind, payload = item
                rows.append((str(cache_key), kind,
                             json.dumps(payload, ensure_ascii=False),
                             base + index * 1e-6))
            self.db.executemany(
                "INSERT OR REPLACE INTO feature_cache"
                "(cache_key, kind, payload_json, last_used) VALUES(?, ?, ?, ?)",
                rows)
        return len(rows)

    def feature_cache_touch(self, cache_keys, epoch=None):
        keys = [str(key) for key in cache_keys if key is not None]
        if not keys:
            return 0
        with self.db.transaction():
            base = self._next_cache_epoch(epoch)
            rows = [(base + index * 1e-6, key) for index, key in enumerate(keys)]
            self.db.executemany(
                "UPDATE feature_cache SET last_used=? WHERE cache_key=?", rows)
        return len(rows)

    def feature_cache_len(self):
        return int(self.db.scalar("SELECT COUNT(*) FROM feature_cache", (), 0) or 0)

    def feature_cache_prune(self, max_entries):
        limit = max(1, int(max_entries or 1))
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM feature_cache WHERE cache_key NOT IN"
                " (SELECT cache_key FROM feature_cache"
                "  ORDER BY last_used DESC, cache_key DESC LIMIT ?)", (limit,))

    def feature_cache_clear(self):
        self.db.execute("DELETE FROM feature_cache")

    def feature_cache_keys(self, limit=None):
        if limit is None:
            rows = self.db.query(
                "SELECT cache_key FROM feature_cache ORDER BY last_used ASC")
        else:
            rows = self.db.query(
                "SELECT cache_key FROM feature_cache"
                " ORDER BY last_used ASC LIMIT ?", (max(0, int(limit)),))
        return [row["cache_key"] for row in rows]

    def feature_cache_items(self, limit=None):
        """按 last_used 升序返回 (cache_key, payload)，供导出/测试。"""
        sql = "SELECT cache_key, payload_json FROM feature_cache ORDER BY last_used ASC"
        params = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (max(0, int(limit)),)
        result = []
        for row in self.db.query(sql, params):
            payload = self._loads(row["payload_json"], None)
            if payload is not None:
                result.append((row["cache_key"], payload))
        return result

    # ------------------------------------------------------------------
    # 跨扫描哈希缓存 / 媒体探测缓存（v0043 增量扫描）
    # ------------------------------------------------------------------
    _HASH_CACHE_KEYS = ("dev", "inode", "path", "size", "mtime_ns", "quick_hash",
                        "full_hash", "algo", "sample_kb", "sample_version",
                        "first_scan_id", "last_scan_id", "first_seen_epoch",
                        "last_seen_epoch", "last_used")
    # SQLite 旧版本的绑定变量上限为 999；每批 400 对键 = 800 个参数，留足余量。
    _HASH_CACHE_LOOKUP_BATCH = 400

    def hash_cache_get(self, entries):
        """批量读取哈希缓存。

        entries: [(dev, inode), ...] → 返回 {(dev, inode): dict}（dict 键见
        _HASH_CACHE_KEYS）。同一 (dev, inode) 只查一次。
        """
        wanted = []
        seen = set()
        for item in entries or []:
            if item is None or len(item) != 2:
                continue
            try:
                key = (sqlite_int(item[0]), sqlite_int(item[1]))
            except (TypeError, ValueError):
                continue
            if key in seen or key[1] == 0:
                continue
            seen.add(key)
            wanted.append(key)
        result = {}
        if not wanted:
            return result
        columns = ", ".join(self._HASH_CACHE_KEYS)
        batch = self._HASH_CACHE_LOOKUP_BATCH
        for start in range(0, len(wanted), batch):
            chunk = wanted[start:start + batch]
            clause = " OR ".join(["(dev=? AND inode=?)"] * len(chunk))
            params = []
            for dev, inode in chunk:
                params.extend([dev, inode])
            rows = self.db.query(
                "SELECT %s FROM hash_cache WHERE %s" % (columns, clause),
                tuple(params))
            for row in rows:
                result[(int(row["dev"]), int(row["inode"]))] = {
                    key: row[key] for key in self._HASH_CACHE_KEYS}
        return result

    def _next_last_used(self, epoch=None):
        try:
            current = float(self.db.scalar(
                "SELECT COALESCE(MAX(last_used), 0) FROM hash_cache", (), 0.0) or 0.0)
        except (TypeError, ValueError):
            current = 0.0
        seed = time.time() if epoch is None else float(epoch)
        return max(seed, current + 1e-6)

    def hash_cache_put_many(self, rows, epoch=None):
        """批量写入/更新哈希缓存（按 (dev, inode) 覆盖）。返回写入行数。"""
        rows = list(rows or [])
        if not rows:
            return 0
        with self.db.transaction():
            base = self._next_last_used(epoch)
            payload = []
            for index, row in enumerate(rows):
                payload.append((
                    sqlite_int(row.get("dev")),
                    sqlite_int(row.get("inode")),
                    str(row.get("path") or ""),
                    sqlite_int(row.get("size")),
                    sqlite_int(row.get("mtime_ns")),
                    row.get("quick_hash"),
                    row.get("full_hash"),
                    str(row.get("algo") or "sha256"),
                    sqlite_int(row.get("sample_kb")),
                    sqlite_int(row.get("sample_version")),
                    row.get("first_scan_id"),
                    row.get("last_scan_id"),
                    float(row.get("first_seen_epoch") or base),
                    float(row.get("last_seen_epoch") or base),
                    float(row.get("last_used") or (base + index * 1e-6)),
                ))
            self.db.executemany(
                "INSERT OR REPLACE INTO hash_cache"
                "(dev, inode, path, size, mtime_ns, quick_hash, full_hash, algo,"
                " sample_kb, sample_version, first_scan_id, last_scan_id,"
                " first_seen_epoch, last_seen_epoch, last_used)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", payload)
        return len(payload)

    def hash_cache_touch(self, keys, epoch=None):
        keys = [item for item in (keys or []) if item is not None]
        if not keys:
            return 0
        base = self._next_last_used(epoch)
        payload = [(base + index * 1e-6, sqlite_int(dev), sqlite_int(inode))
                   for index, (dev, inode) in enumerate(keys)]
        with self.db.transaction():
            self.db.executemany(
                "UPDATE hash_cache SET last_used=? WHERE dev=? AND inode=?", payload)
        return len(payload)

    def hash_cache_count(self):
        return int(self.db.scalar("SELECT COUNT(*) FROM hash_cache", (), 0) or 0)

    def hash_cache_prune(self, max_entries):
        limit = max(1, int(max_entries or 1))
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM hash_cache WHERE (dev, inode) NOT IN"
                " (SELECT dev, inode FROM hash_cache"
                "  ORDER BY last_used DESC, dev DESC, inode DESC LIMIT ?)", (limit,))

    def hash_cache_clear(self):
        with self.db.transaction():
            self.db.execute("DELETE FROM hash_cache")

    def hash_cache_stats(self):
        row = self.db.query_one(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(size), 0) AS bytes,"
            " MIN(last_used) AS oldest, MAX(last_used) AS newest FROM hash_cache")
        if row is None:
            return {"entries": 0, "bytes": 0, "oldest_epoch": None,
                    "newest_epoch": None}
        return {
            "entries": int(row["entries"] or 0),
            "bytes": int(row["bytes"] or 0),
            "oldest_epoch": (float(row["oldest"]) if row["oldest"] else None),
            "newest_epoch": (float(row["newest"]) if row["newest"] else None),
        }

    _MEDIA_PROBE_KEYS = ("dev", "inode", "size", "mtime_ns", "payload",
                         "probed_epoch", "last_used")

    def media_probe_cache_get(self, entries):
        """批量读取媒体探测缓存：entries=[(dev,inode)] → {(dev,inode): dict}。"""
        wanted = []
        seen = set()
        for item in entries or []:
            if item is None or len(item) != 2:
                continue
            try:
                key = (sqlite_int(item[0]), sqlite_int(item[1]))
            except (TypeError, ValueError):
                continue
            if key in seen or key[1] == 0:
                continue
            seen.add(key)
            wanted.append(key)
        result = {}
        if not wanted:
            return result
        batch = self._HASH_CACHE_LOOKUP_BATCH
        for start in range(0, len(wanted), batch):
            chunk = wanted[start:start + batch]
            clause = " OR ".join(["(dev=? AND inode=?)"] * len(chunk))
            params = []
            for dev, inode in chunk:
                params.extend([dev, inode])
            rows = self.db.query(
                "SELECT dev, inode, size, mtime_ns, payload_json, probed_epoch"
                " FROM media_probe_cache WHERE " + clause, tuple(params))
            for row in rows:
                payload = self._loads(row["payload_json"], None)
                if not isinstance(payload, dict):
                    continue
                result[(int(row["dev"]), int(row["inode"]))] = {
                    "dev": int(row["dev"]),
                    "inode": int(row["inode"]),
                    "size": int(row["size"] or 0),
                    "mtime_ns": int(row["mtime_ns"] or 0),
                    "payload": payload,
                    "probed_epoch": float(row["probed_epoch"] or 0.0),
                }
        return result

    def media_probe_cache_put_many(self, items, epoch=None):
        """items: [{dev,inode,size,mtime_ns,payload}, ...]；返回写入行数。"""
        items = list(items or [])
        if not items:
            return 0
        seed = time.time() if epoch is None else float(epoch)
        payload = []
        for index, item in enumerate(items):
            payload.append((
                sqlite_int(item.get("dev")),
                sqlite_int(item.get("inode")),
                sqlite_int(item.get("size")),
                sqlite_int(item.get("mtime_ns")),
                json.dumps(item.get("payload") or {}, ensure_ascii=False),
                float(item.get("probed_epoch") or seed),
                seed + index * 1e-6,
            ))
        with self.db.transaction():
            self.db.executemany(
                "INSERT OR REPLACE INTO media_probe_cache"
                "(dev, inode, size, mtime_ns, payload_json, probed_epoch, last_used)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)", payload)
        return len(payload)

    def media_probe_cache_count(self):
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM media_probe_cache", (), 0) or 0)

    def media_probe_cache_prune(self, max_entries):
        limit = max(1, int(max_entries or 1))
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM media_probe_cache WHERE (dev, inode) NOT IN"
                " (SELECT dev, inode FROM media_probe_cache"
                "  ORDER BY last_used DESC, dev DESC, inode DESC LIMIT ?)", (limit,))

    def media_probe_cache_clear(self):
        with self.db.transaction():
            self.db.execute("DELETE FROM media_probe_cache")

    # ------------------------------------------------------------------
    # 扫描 checkpoint（plan B：FileRecord 明细拆到 scan_checkpoint_records）
    # ------------------------------------------------------------------
    _CHECKPOINT_RECORD_INSERT_SQL = (
        "INSERT INTO scan_checkpoint_records"
        "(scan_id, ordinal, path, size, mtime_ns, inode, dev, nlink,"
        " quick_hash, hash) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")

    def _replace_checkpoint_records(self, scan_id, records):
        """在调用方事务内批量替换断点记录；records 可为迭代器（流式分批）。"""
        self.db.execute(
            "DELETE FROM scan_checkpoint_records WHERE scan_id=?", (scan_id,))
        batch = []
        count = 0
        for item in records:
            if not isinstance(item, dict):
                continue
            quick_hash = item.get("quick_hash")
            digest = item.get("hash")
            batch.append((
                scan_id, count, str(item.get("path") or ""),
                db_module._as_int(item.get("size")),
                db_module._as_int(item.get("mtime_ns")),
                db_module._as_int(item.get("inode")),
                db_module._as_int(item.get("dev")),
                db_module._as_int(item.get("nlink"), 1),
                quick_hash if isinstance(quick_hash, str) else None,
                digest if isinstance(digest, str) else None,
            ))
            count += 1
            if len(batch) >= _CHECKPOINT_RECORD_BATCH:
                self.db.executemany(
                    self._CHECKPOINT_RECORD_INSERT_SQL, batch)
                batch = []
        if batch:
            self.db.executemany(self._CHECKPOINT_RECORD_INSERT_SQL, batch)
        return count

    def save_checkpoint(self, scan_id, stage, payload):
        """保存 checkpoint；payload 内 records 列表拆到明细表（原子）。"""
        scan_id = self._check_scan_id(scan_id)
        data = dict(payload) if isinstance(payload, dict) else {}
        records = data.pop("records", None)
        if isinstance(records, (list, tuple)):
            return self.save_pause_checkpoint(scan_id, stage, data, records)
        with self.db.transaction():
            # 轻量 checkpoint：不携带 records，清除可能残留的旧断点明细。
            self.db.execute(
                "DELETE FROM scan_checkpoint_records WHERE scan_id=?", (scan_id,))
            data.pop("record_count", None)
            self.db.execute(
                "INSERT OR REPLACE INTO scan_checkpoints"
                "(scan_id, stage, payload_json, updated_epoch)"
                " VALUES(?, ?, ?, ?)",
                (scan_id, str(stage or ""),
                 json.dumps(data, ensure_ascii=False), time.time()))
        return None

    def save_pause_checkpoint(self, scan_id, stage, payload, records):
        """暂停断点：records 流式分批写入明细表，payload 仅元数据 + record_count。

        单事务保证 checkpoint 与 records 一致；records 可为生成器，避免在内存里
        再复制一份完整 FileRecord dict 列表。
        """
        scan_id = self._check_scan_id(scan_id)
        data = dict(payload) if isinstance(payload, dict) else {}
        data.pop("records", None)
        with self.db.transaction():
            count = self._replace_checkpoint_records(scan_id, records)
            data["record_count"] = count
            self.db.execute(
                "INSERT OR REPLACE INTO scan_checkpoints"
                "(scan_id, stage, payload_json, updated_epoch)"
                " VALUES(?, ?, ?, ?)",
                (scan_id, str(stage or ""),
                 json.dumps(data, ensure_ascii=False), time.time()))
        return count

    def checkpoint_record_count(self, scan_id):
        scan_id = self._check_scan_id(scan_id)
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM scan_checkpoint_records WHERE scan_id=?",
            (scan_id,), 0) or 0)

    def load_checkpoint_records(self, scan_id):
        """按 ordinal 读取断点 FileRecord 明细（v3 表）。"""
        scan_id = self._check_scan_id(scan_id)
        rows = self.db.query(
            "SELECT path, size, mtime_ns, inode, dev, nlink, quick_hash, hash"
            " FROM scan_checkpoint_records WHERE scan_id=? ORDER BY ordinal ASC",
            (scan_id,))
        records = []
        for row in rows:
            records.append({
                "path": row["path"],
                "size": int(row["size"] or 0),
                "mtime_ns": int(row["mtime_ns"] or 0),
                "inode": int(row["inode"] or 0),
                "dev": int(row["dev"] or 0),
                "nlink": int(row["nlink"] or 0),
                "quick_hash": row["quick_hash"],
                "hash": row["hash"],
            })
        return records

    def load_checkpoint(self, scan_id):
        scan_id = self._check_scan_id(scan_id)
        row = self.db.query_one(
            "SELECT stage, payload_json, updated_epoch FROM scan_checkpoints"
            " WHERE scan_id=?", (scan_id,))
        if row is None:
            return None
        payload = self._loads(row["payload_json"], None)
        if not isinstance(payload, dict):
            payload = {}
        # plan B：v3 落库 payload 只有 stage/progress/stats/record_count；
        # FileRecord 明细在 scan_checkpoint_records（由 load_resumable_checkpoint
        # 按需 hydrate；旧 v2/v1 payload 内嵌 records 保持原样兼容）。
        return {
            "scan_id": scan_id,
            "stage": row["stage"],
            "payload": payload,
            "updated_epoch": row["updated_epoch"],
        }

    @staticmethod
    def checkpoint_payload_resumable(payload):
        """暂停断点是否可恢复：阶段合法、records 列表完整且数量匹配。"""
        if not isinstance(payload, dict):
            return False
        stage = payload.get("stage")
        if not isinstance(stage, str) or stage not in RESUME_STAGES:
            return False
        records = payload.get("records")
        if not isinstance(records, list):
            return False
        if "record_count" in payload:
            try:
                expected = int(payload.get("record_count"))
            except (TypeError, ValueError):
                return False
            if expected != len(records):
                return False
        return True

    def load_resumable_checkpoint(self, scan_id):
        """返回可恢复断点 payload（records 从明细表 hydrate；旧 payload 回退）。"""
        checkpoint = self.load_checkpoint(scan_id)
        if checkpoint is None:
            return None
        payload = checkpoint.get("payload")
        if not isinstance(payload, dict):
            return None
        payload = dict(payload)
        if not isinstance(payload.get("records"), list) and "record_count" in payload:
            # plan B：明细在 scan_checkpoint_records；数量不匹配则不可恢复。
            payload["records"] = self.load_checkpoint_records(scan_id)
        if not self.checkpoint_payload_resumable(payload):
            return None
        payload["updated_epoch"] = checkpoint.get("updated_epoch")
        return payload

    def delete_checkpoint(self, scan_id):
        scan_id = self._check_scan_id(scan_id)
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM scan_checkpoint_records WHERE scan_id=?",
                (scan_id,))
            self.db.execute(
                "DELETE FROM scan_checkpoints WHERE scan_id=?", (scan_id,))

    def list_checkpoints(self, limit=100):
        """列出 checkpoint 元数据（不 hydrate records，避免加载大数组）。"""
        rows = self.db.query(
            "SELECT scan_id, stage, payload_json, updated_epoch"
            " FROM scan_checkpoints ORDER BY updated_epoch DESC LIMIT ?",
            (max(1, int(limit or 100)),))
        result = []
        for row in rows:
            result.append({
                "scan_id": row["scan_id"],
                "stage": row["stage"],
                "payload": self._loads(row["payload_json"], None),
                "updated_epoch": row["updated_epoch"],
            })
        return result

    # ------------------------------------------------------------------
    # 字幕任务元数据（文件仍是任务目录事实源；此处为 SQLite 镜像）
    # ------------------------------------------------------------------
    @staticmethod
    def _check_generic_job_id(job_id):
        if not isinstance(job_id, str) or not config.SAFE_ID_RE.match(job_id):
            raise invalid(u"任务 ID 非法")
        return job_id

    def save_subtitle_job(self, job_id, record):
        job_id = self._check_generic_job_id(job_id)
        if not isinstance(record, dict):
            return False
        try:
            updated = float(record.get("updated_epoch") or time.time())
        except (TypeError, ValueError):
            updated = time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO subtitle_jobs"
            "(job_id, payload_json, updated_epoch, status) VALUES(?, ?, ?, ?)",
            (job_id, json.dumps(record, ensure_ascii=False), updated,
             record.get("status")))
        return True

    def load_subtitle_job(self, job_id):
        job_id = self._check_generic_job_id(job_id)
        row = self.db.query_one(
            "SELECT payload_json FROM subtitle_jobs WHERE job_id=?", (job_id,))
        if row is None:
            return None
        record = self._loads(row["payload_json"], None)
        if not isinstance(record, dict):
            return None
        record.setdefault("job_id", job_id)
        return record

    def load_subtitle_jobs(self, limit=200):
        rows = self.db.query(
            "SELECT job_id, payload_json FROM subtitle_jobs"
            " ORDER BY updated_epoch DESC, job_id DESC LIMIT ?",
            (max(1, int(limit or 200)),))
        result = []
        for row in rows:
            record = self._loads(row["payload_json"], None)
            if not isinstance(record, dict):
                continue
            record.setdefault("job_id", row["job_id"])
            result.append(record)
        return result

    def delete_subtitle_job(self, job_id):
        job_id = self._check_generic_job_id(job_id)
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM subtitle_chunks WHERE job_id=?", (job_id,))
            self.db.execute(
                "DELETE FROM subtitle_jobs WHERE job_id=?", (job_id,))

    # ------------------------------------------------------------------
    # 字幕批次（schema v6 / docs/SUBTITLE_BATCH_CONTRACT.md §7.3）
    # ------------------------------------------------------------------
    @staticmethod
    def _check_batch_id(batch_id):
        if (not isinstance(batch_id, str)
                or not config.SUBTITLE_BATCH_ID_RE.match(batch_id)):
            raise invalid(u"字幕批次 ID 非法")
        return batch_id

    def save_subtitle_batch(self, batch_id, record):
        batch_id = self._check_batch_id(batch_id)
        if not isinstance(record, dict):
            return False
        try:
            created = float(record.get("created_epoch") or time.time())
        except (TypeError, ValueError):
            created = time.time()
        try:
            updated = float(record.get("updated_epoch") or created)
        except (TypeError, ValueError):
            updated = created
        self.db.execute(
            "INSERT OR REPLACE INTO subtitle_batches"
            "(batch_id, payload_json, created_epoch, updated_epoch)"
            " VALUES(?, ?, ?, ?)",
            (batch_id, json.dumps(record, ensure_ascii=False), created, updated))
        return True

    def load_subtitle_batch(self, batch_id):
        batch_id = self._check_batch_id(batch_id)
        row = self.db.query_one(
            "SELECT payload_json FROM subtitle_batches WHERE batch_id=?",
            (batch_id,))
        if row is None:
            return None
        record = self._loads(row["payload_json"], None)
        if not isinstance(record, dict):
            return None
        record.setdefault("batch_id", batch_id)
        return record

    def load_subtitle_batches(self, limit=200):
        rows = self.db.query(
            "SELECT batch_id, payload_json FROM subtitle_batches"
            " ORDER BY created_epoch DESC, batch_id DESC LIMIT ?",
            (max(1, int(limit or 200)),))
        result = []
        for row in rows:
            record = self._loads(row["payload_json"], None)
            if not isinstance(record, dict):
                continue
            record.setdefault("batch_id", row["batch_id"])
            result.append(record)
        return result

    def delete_subtitle_batch(self, batch_id):
        batch_id = self._check_batch_id(batch_id)
        self.db.execute(
            "DELETE FROM subtitle_batches WHERE batch_id=?", (batch_id,))

    # ------------------------------------------------------------------
    # 字幕批次组（schema v7 / docs/SUBTITLE_BATCH_CONTRACT.md §7.4）
    # ------------------------------------------------------------------
    @staticmethod
    def _check_batch_group_id(group_id):
        if (not isinstance(group_id, str)
                or not config.SUBTITLE_BATCH_GROUP_ID_RE.match(group_id)):
            raise invalid(u"字幕批次组 ID 非法")
        return group_id

    def save_subtitle_batch_group(self, group_id, record):
        group_id = self._check_batch_group_id(group_id)
        if not isinstance(record, dict):
            return False
        try:
            created = float(record.get("created_epoch") or time.time())
        except (TypeError, ValueError):
            created = time.time()
        try:
            updated = float(record.get("updated_epoch") or created)
        except (TypeError, ValueError):
            updated = created
        self.db.execute(
            "INSERT OR REPLACE INTO subtitle_batch_groups"
            "(group_id, payload_json, created_epoch, updated_epoch)"
            " VALUES(?, ?, ?, ?)",
            (group_id, json.dumps(record, ensure_ascii=False), created, updated))
        return True

    def load_subtitle_batch_group(self, group_id):
        group_id = self._check_batch_group_id(group_id)
        row = self.db.query_one(
            "SELECT payload_json FROM subtitle_batch_groups WHERE group_id=?",
            (group_id,))
        if row is None:
            return None
        record = self._loads(row["payload_json"], None)
        if not isinstance(record, dict):
            return None
        record.setdefault("group_id", group_id)
        return record

    def load_subtitle_batch_groups(self, limit=50):
        rows = self.db.query(
            "SELECT group_id, payload_json FROM subtitle_batch_groups"
            " ORDER BY created_epoch DESC, group_id DESC LIMIT ?",
            (max(1, int(limit or 50)),))
        result = []
        for row in rows:
            record = self._loads(row["payload_json"], None)
            if not isinstance(record, dict):
                continue
            record.setdefault("group_id", row["group_id"])
            result.append(record)
        return result

    def delete_subtitle_batch_group(self, group_id):
        group_id = self._check_batch_group_id(group_id)
        self.db.execute(
            "DELETE FROM subtitle_batch_groups WHERE group_id=?", (group_id,))

    # ------------------------------------------------------------------
    # 字幕 ASR 分片缓存（schema v4 / plan C）
    # ------------------------------------------------------------------
    def save_subtitle_chunk(self, job_id, chunk_index, start_sec, end_sec,
                            status, cues, updated_epoch=None):
        """写入/更新一个 ASR 分片结果（cues 为绝对时间轴 cue 列表）。"""
        job_id = self._check_generic_job_id(job_id)
        try:
            chunk_index = int(chunk_index)
            start = float(start_sec)
            end = float(end_sec)
        except (TypeError, ValueError):
            raise invalid(u"字幕分片参数非法")
        if not isinstance(cues, list):
            cues = []
        try:
            updated = float(updated_epoch or time.time())
        except (TypeError, ValueError):
            updated = time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO subtitle_chunks"
            "(job_id, chunk_index, start_sec, end_sec, status, cues_json,"
            " updated_epoch) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (job_id, chunk_index, start, end, str(status or ""),
             json.dumps(cues, ensure_ascii=False), updated))
        return True

    def load_subtitle_chunks(self, job_id):
        """按 chunk_index 升序返回任务的分片缓存。"""
        job_id = self._check_generic_job_id(job_id)
        rows = self.db.query(
            "SELECT chunk_index, start_sec, end_sec, status, cues_json,"
            " updated_epoch FROM subtitle_chunks WHERE job_id=?"
            " ORDER BY chunk_index ASC", (job_id,))
        result = []
        for row in rows:
            cues = self._loads(row["cues_json"], None)
            result.append({
                "job_id": job_id,
                "chunk_index": int(row["chunk_index"]),
                "start_sec": float(row["start_sec"] or 0.0),
                "end_sec": float(row["end_sec"] or 0.0),
                "status": row["status"],
                "cues": cues if isinstance(cues, list) else [],
                "updated_epoch": row["updated_epoch"],
            })
        return result

    def list_subtitle_chunks(self, job_id=None, limit=500):
        """列出分片：给定 job_id 时按分片顺序；否则按最近更新时间倒序。"""
        if job_id is not None:
            return self.load_subtitle_chunks(job_id)
        rows = self.db.query(
            "SELECT job_id, chunk_index, start_sec, end_sec, status,"
            " cues_json, updated_epoch FROM subtitle_chunks"
            " ORDER BY updated_epoch DESC, job_id DESC, chunk_index ASC"
            " LIMIT ?", (max(1, int(limit or 500)),))
        result = []
        for row in rows:
            cues = self._loads(row["cues_json"], None)
            result.append({
                "job_id": row["job_id"],
                "chunk_index": int(row["chunk_index"]),
                "start_sec": float(row["start_sec"] or 0.0),
                "end_sec": float(row["end_sec"] or 0.0),
                "status": row["status"],
                "cues": cues if isinstance(cues, list) else [],
                "updated_epoch": row["updated_epoch"],
            })
        return result

    def delete_subtitle_chunks(self, job_id, chunk_index=None):
        job_id = self._check_generic_job_id(job_id)
        if chunk_index is None:
            self.db.execute(
                "DELETE FROM subtitle_chunks WHERE job_id=?", (job_id,))
        else:
            try:
                chunk_index = int(chunk_index)
            except (TypeError, ValueError):
                raise invalid(u"字幕分片序号非法")
            self.db.execute(
                "DELETE FROM subtitle_chunks WHERE job_id=? AND chunk_index=?",
                (job_id, chunk_index))

    # ------------------------------------------------------------------
    # 启动状态：活动扫描标记 interrupted（不自动续跑）
    # ------------------------------------------------------------------
    def mark_interrupted_scans(self, epoch=None):
        """启动状态收敛（docs/RESUME_CONTRACT.md §1）。

        - paused 且有可恢复断点（records 快照）：保持 paused，供手动继续；
        - queued/running/stopping，或 paused 但无可用断点：标记 interrupted；
        - 已完成/失败/停止状态不动；绝不自动继续。
        """
        now = time.time() if epoch is None else float(epoch)
        placeholders = ", ".join(["?"] * len(INTERRUPTED_STATUSES))
        rows = self.db.query(
            "SELECT scan_id, meta_json, status FROM scans"
            " WHERE status IN (%s)" % placeholders, tuple(INTERRUPTED_STATUSES))
        changed = []
        if not rows:
            return changed
        with self.db.transaction():
            for row in rows:
                scan_id = row["scan_id"]
                previous = row["status"]
                meta = self._loads(row["meta_json"], None)
                if not isinstance(meta, dict):
                    meta = {"scan_id": scan_id}
                meta["scan_id"] = scan_id
                if previous == "paused" and self._checkpoint_resumable(scan_id):
                    meta["status"] = "paused"
                    meta["interrupted"] = False
                    meta["resumable"] = True
                    if not meta.get("pause_reason"):
                        meta["pause_reason"] = "paused"
                    if not meta.get("message"):
                        meta["message"] = u"扫描已暂停，升级后可继续"
                    self.db.execute(
                        "UPDATE scans SET meta_json=?, updated_epoch=?,"
                        " status='paused', interrupted=0 WHERE scan_id=?",
                        (json.dumps(meta, ensure_ascii=False), now, scan_id))
                    continue
                meta["status"] = "interrupted"
                meta["interrupted"] = True
                meta["resumable"] = False
                meta["interrupted_previous_status"] = previous
                meta["interrupted_epoch"] = now
                meta["finished_epoch"] = now
                meta["finished_at"] = iso_local(now)
                meta["message"] = u"服务重启导致扫描中断，请重新发起扫描"
                self.db.execute(
                    "UPDATE scans SET meta_json=?, updated_epoch=?,"
                    " status='interrupted', interrupted=1 WHERE scan_id=?",
                    (json.dumps(meta, ensure_ascii=False), now, scan_id))
                self.save_checkpoint(scan_id, meta.get("stage") or "", {
                    "status": "interrupted",
                    "previous_status": previous,
                    "interrupted_epoch": now,
                })
                changed.append(scan_id)
        return changed

    def _checkpoint_resumable(self, scan_id):
        try:
            return self.load_resumable_checkpoint(scan_id) is not None
        except Exception:  # pragma: no cover
            return False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self):
        try:
            self.db.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # ------------------------------------------------------------------
    # JSON 辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _loads(raw, default=None):
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except (TypeError, ValueError, UnicodeError):
            return default

    # ------------------------------------------------------------------
    def temp_path(self, prefix="dc-", suffix=".tmp"):
        fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=self.tmp_dir)
        os.close(fd)
        return path
