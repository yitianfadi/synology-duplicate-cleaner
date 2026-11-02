# -*- coding: utf-8 -*-
"""SQLite 元数据库（docs/STORAGE_CONTRACT.md §1）。

职责：
- 打开 <data>/dupcleaner.sqlite3，设置 WAL / synchronous=NORMAL /
  busy_timeout=5000 / foreign_keys=ON。
- 建表与 schema 版本（schema_meta.schema_version），迁移按版本执行。
- 单写多读：一个连接 + RLock 串行化，所有事务短小；写使用 BEGIN IMMEDIATE。
- 损坏恢复：启动时 quick_check 失败则备份 <db>.corrupt-<ts> 并重建空库。
- 退出/低峰执行 PRAGMA wal_checkpoint(TRUNCATE)。

本模块只负责 SQLite 连接/事务/表结构；业务语义（扫描、删除任务、特征缓存、
checkpoint、字幕任务）由 store.Store 封装。Python 3.8 标准库 sqlite3。
"""

from __future__ import absolute_import

import contextlib
import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger("dupcleaner.db")

DB_FILENAME = "dupcleaner.sqlite3"
CURRENT_SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
BATCH_SIZE = 200

# 损坏识别：只把这些 DatabaseError 当作"文件损坏"处理，避免把锁冲突/
# 权限问题误判成损坏而丢掉正常数据。
_CORRUPT_MARKERS = (
    "not a database",
    "malformed",
    "corrupt",
    "invalid page",
    "encrypted",
    "error in view",
    "error in index",
    "error in trigger",
)

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scans (
        scan_id TEXT PRIMARY KEY,
        meta_json TEXT NOT NULL,
        created_epoch REAL NOT NULL,
        updated_epoch REAL NOT NULL,
        status TEXT NOT NULL,
        interrupted INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scan_groups (
        scan_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        group_id TEXT NOT NULL,
        match_type TEXT,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (scan_id, ordinal)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_scan_groups_type
        ON scan_groups(scan_id, match_type)
    """,
    """
    CREATE TABLE IF NOT EXISTS deletion_jobs (
        job_id TEXT PRIMARY KEY,
        meta_json TEXT NOT NULL,
        started_epoch REAL NOT NULL,
        status TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deletion_results (
        job_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (job_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feature_cache (
        cache_key TEXT PRIMARY KEY,
        kind TEXT,
        payload_json TEXT NOT NULL,
        last_used REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scan_checkpoints (
        scan_id TEXT PRIMARY KEY,
        stage TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        updated_epoch REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subtitle_jobs (
        job_id TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        updated_epoch REAL NOT NULL,
        status TEXT
    )
    """,
)


class Database(object):
    """单连接 SQLite 封装：线程安全（RLock）、短事务、可恢复。"""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.recovered_from = None
        self._lock = threading.RLock()
        self._conn = None
        self._tx_depth = 0
        self._closed = False
        self._open_with_recovery()

    # ------------------------------------------------------------------
    # 打开 / 恢复
    # ------------------------------------------------------------------
    def _open_with_recovery(self):
        with self._lock:
            try:
                self._conn = self._connect()
                self._configure()
                self._check_integrity()
                self._ensure_schema()
            except sqlite3.DatabaseError as exc:
                conn, self._conn = self._conn, None
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                if not self._is_corruption(exc):
                    raise
                backup = self._backup_corrupt()
                logger.warning(u"SQLite 数据库损坏，已备份到 %s 并重建空库: %s",
                               backup, exc)
                self.recovered_from = backup
                self._conn = self._connect()
                self._configure()
                self._ensure_schema()
            self._secure_permissions()

    def _connect(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _configure(self):
        conn = self._conn
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=%d" % BUSY_TIMEOUT_MS)
        conn.execute("PRAGMA foreign_keys=ON")

    def _check_integrity(self):
        row = self._conn.execute("PRAGMA quick_check(1)").fetchone()
        if row is not None and str(row[0]).lower() != "ok":
            raise sqlite3.DatabaseError(
                "database disk image is malformed: %s" % (row[0],))

    @staticmethod
    def _is_corruption(exc):
        if not isinstance(exc, sqlite3.DatabaseError):
            return False
        message = str(exc).lower()
        return any(marker in message for marker in _CORRUPT_MARKERS)

    def _backup_corrupt(self):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = "%s.corrupt-%s" % (self.path, stamp)
        target = base
        index = 1
        while os.path.exists(target):
            target = "%s-%d" % (base, index)
            index += 1
        try:
            os.replace(self.path, target)
        except OSError:
            target = None
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass
        return target

    def _secure_permissions(self):
        # 契约：数据库文件权限 600（WAL/SHM 同属该库的临时文件）。
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 建表 / schema 版本
    # ------------------------------------------------------------------
    def _ensure_schema(self):
        conn = self._conn
        with self.transaction():
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) "
                    "VALUES('schema_version', ?)",
                    (str(CURRENT_SCHEMA_VERSION),))
                conn.execute(
                    "PRAGMA user_version = %d" % CURRENT_SCHEMA_VERSION)
            else:
                try:
                    version = int(row[0])
                except (TypeError, ValueError):
                    version = 0
                if version < CURRENT_SCHEMA_VERSION:
                    self._apply_migrations(version)
                elif version > CURRENT_SCHEMA_VERSION:
                    logger.warning(
                        u"数据库 schema 版本 %s 高于程序支持的 %s，按兼容模式打开",
                        version, CURRENT_SCHEMA_VERSION)

    def _apply_migrations(self, from_version):
        """按版本迁移；v1 为首个 SQLite 版本，无历史迁移步骤。"""
        version = int(from_version)
        while version < CURRENT_SCHEMA_VERSION:
            version += 1
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) "
                "VALUES('schema_version', ?)", (str(version),))
            self._conn.execute("PRAGMA user_version = %d" % version)

    # ------------------------------------------------------------------
    # 事务 / 查询
    # ------------------------------------------------------------------
    def _require_open(self):
        if self._closed or self._conn is None:
            raise sqlite3.ProgrammingError("database is closed")

    @contextlib.contextmanager
    def transaction(self):
        """短事务（BEGIN IMMEDIATE）；同线程嵌套时复用最外层事务。"""
        with self._lock:
            self._require_open()
            outer = self._tx_depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield self._conn
            except BaseException:
                self._tx_depth -= 1
                if outer:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
            else:
                self._tx_depth -= 1
                if outer:
                    self._conn.execute("COMMIT")

    def execute(self, sql, params=()):
        with self._lock:
            self._require_open()
            cursor = self._conn.execute(sql, params)
            return cursor.rowcount

    def executemany(self, sql, rows):
        with self._lock:
            self._require_open()
            cursor = self._conn.executemany(sql, rows)
            return cursor.rowcount

    def query(self, sql, params=()):
        with self._lock:
            self._require_open()
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        with self._lock:
            self._require_open()
            return self._conn.execute(sql, params).fetchone()

    def scalar(self, sql, params=(), default=None):
        row = self.query_one(sql, params)
        if row is None:
            return default
        return row[0]

    # ------------------------------------------------------------------
    # schema_meta
    # ------------------------------------------------------------------
    def schema_version(self):
        value = self.scalar(
            "SELECT value FROM schema_meta WHERE key='schema_version'")
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def schema_meta_get(self, key, default=None):
        row = self.query_one(
            "SELECT value FROM schema_meta WHERE key=?", (str(key),))
        if row is None:
            return default
        return row[0]

    def schema_meta_set(self, key, value):
        self.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
            (str(key), str(value)))

    # ------------------------------------------------------------------
    # WAL checkpoint / 关闭
    # ------------------------------------------------------------------
    def wal_checkpoint(self, mode="TRUNCATE"):
        with self._lock:
            self._require_open()
            try:
                self._conn.execute("PRAGMA wal_checkpoint(%s)" % mode)
            except sqlite3.Error:
                pass

    def close(self):
        with self._lock:
            if self._closed:
                return
            try:
                if self._conn is not None:
                    try:
                        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except sqlite3.Error:
                        pass
            finally:
                self._closed = True
                conn, self._conn = self._conn, None
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
