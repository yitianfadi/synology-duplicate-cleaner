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
import json
import logging
import os
import re
import sqlite3
import threading
import time

logger = logging.getLogger("dupcleaner.db")

DB_FILENAME = "dupcleaner.sqlite3"
# v2（v0016 优化 plan A）：scan_groups 增加结果索引列 + scan_files 明细表，
# 让结果分页/搜索/排序/统计走 SQL，不再整份 JSON 遍历。
# v3（v0017 优化 plan B）：暂停断点的 FileRecord 列表拆到 scan_checkpoint_records，
# checkpoint payload 只保留 stage/progress/stats/record_count，避免单个巨大 JSON。
# v4（v0019 优化 plan C）：字幕任务 ASR 分片结果落 subtitle_chunks，支持暂停/续跑。
# v5（v0025 优化 P1）：scan_files 去掉冗余 payload_json；FTS5(trigram) 路径索引
# + 触发器同步，trigram 不可用时自动回退 LIKE 子串搜索。
# v6（v0028 目录批量字幕）：新增 subtitle_batches 批次表
# （docs/SUBTITLE_BATCH_CONTRACT.md §7.3）。
CURRENT_SCHEMA_VERSION = 8
FTS_TABLE = "scan_files_fts"
FTS_TRIGGER_NAMES = (
    "scan_files_fts_ai",
    "scan_files_fts_ad",
    "scan_files_fts_au",
)
FTS_MIN_NEEDLE = 3
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
        wasted_bytes INTEGER NOT NULL DEFAULT 0,
        size INTEGER NOT NULL DEFAULT 0,
        similarity_score INTEGER NOT NULL DEFAULT 0,
        created_epoch REAL NOT NULL DEFAULT 0,
        group_hash TEXT,
        new_files INTEGER NOT NULL DEFAULT 0,
        changed_files INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (scan_id, ordinal)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_scan_groups_type
        ON scan_groups(scan_id, match_type)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_scan_groups_gid
        ON scan_groups(scan_id, group_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS scan_files (
        scan_id TEXT NOT NULL,
        group_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        path TEXT NOT NULL,
        size INTEGER NOT NULL DEFAULT 0,
        is_keep INTEGER NOT NULL DEFAULT 0,
        default_selected INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (scan_id, group_id, file_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_scan_files_path
        ON scan_files(scan_id, path)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_scan_files_group
        ON scan_files(scan_id, group_id)
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
    CREATE TABLE IF NOT EXISTS scan_checkpoint_records (
        scan_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        path TEXT NOT NULL,
        size INTEGER,
        mtime_ns INTEGER,
        inode INTEGER,
        dev INTEGER,
        nlink INTEGER,
        quick_hash TEXT,
        hash TEXT,
        PRIMARY KEY (scan_id, ordinal)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_scan_checkpoint_records_path
        ON scan_checkpoint_records(scan_id, path)
    """,
    """
    CREATE TABLE IF NOT EXISTS subtitle_jobs (
        job_id TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        updated_epoch REAL NOT NULL,
        status TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subtitle_chunks (
        job_id TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        start_sec REAL NOT NULL,
        end_sec REAL NOT NULL,
        status TEXT NOT NULL,
        cues_json TEXT NOT NULL,
        updated_epoch REAL NOT NULL,
        PRIMARY KEY (job_id, chunk_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subtitle_batches (
        batch_id TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        created_epoch REAL NOT NULL,
        updated_epoch REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subtitle_batch_groups (
        group_id TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        created_epoch REAL NOT NULL,
        updated_epoch REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scan_file_deletions (
        scan_id TEXT NOT NULL,
        path TEXT NOT NULL,
        job_id TEXT,
        mode TEXT,
        deleted_epoch REAL NOT NULL,
        PRIMARY KEY (scan_id, path)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_scan_file_deletions_scan
        ON scan_file_deletions(scan_id)
    """,
    # v8（v0043）：跨扫描哈希缓存 + 媒体探测缓存（增量扫描/保留策略用）
    """
    CREATE TABLE IF NOT EXISTS hash_cache (
        dev INTEGER NOT NULL,
        inode INTEGER NOT NULL,
        path TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        quick_hash TEXT,
        full_hash TEXT,
        algo TEXT NOT NULL,
        sample_kb INTEGER NOT NULL,
        sample_version INTEGER NOT NULL,
        first_scan_id TEXT,
        last_scan_id TEXT,
        first_seen_epoch REAL,
        last_seen_epoch REAL,
        last_used REAL NOT NULL,
        PRIMARY KEY (dev, inode)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hash_cache_used
        ON hash_cache(last_used)
    """,
    """
    CREATE TABLE IF NOT EXISTS media_probe_cache (
        dev INTEGER NOT NULL,
        inode INTEGER NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        probed_epoch REAL NOT NULL,
        last_used REAL NOT NULL,
        PRIMARY KEY (dev, inode)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_media_probe_cache_used
        ON media_probe_cache(last_used)
    """,
)

# v4：plan C 字幕 ASR 分片缓存表（v3 升级时只建表；chunk 行由任务运行时写入）。
_V4_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS subtitle_chunks (
        job_id TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        start_sec REAL NOT NULL,
        end_sec REAL NOT NULL,
        status TEXT NOT NULL,
        cues_json TEXT NOT NULL,
        updated_epoch REAL NOT NULL,
        PRIMARY KEY (job_id, chunk_index)
    )
    """,
)

# v6（v0028 目录批量字幕）：subtitle_batches 批次表（幂等；行由批次提交时写入）。
_V6_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS subtitle_batches (
        batch_id TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        created_epoch REAL NOT NULL,
        updated_epoch REAL NOT NULL
    )
    """,
)

# v7（v0032）：字幕批次组 + 扫描删除标记（幂等；行由运行期写入）。
# - subtitle_batch_groups：一次提交可拆成多批，组记录聚合展示/恢复（docs/SUBTITLE_BATCH_CONTRACT.md）。
# - scan_file_deletions：删除成功后的文件标记，分组/统计/CSV 读取时排除，
#   解决“删除完成后结果页仍显示已删除文件”（docs/STORAGE_CONTRACT.md）。
_V7_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS subtitle_batch_groups (
        group_id TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        created_epoch REAL NOT NULL,
        updated_epoch REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scan_file_deletions (
        scan_id TEXT NOT NULL,
        path TEXT NOT NULL,
        job_id TEXT,
        mode TEXT,
        deleted_epoch REAL NOT NULL,
        PRIMARY KEY (scan_id, path)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_scan_file_deletions_scan
        ON scan_file_deletions(scan_id)
    """,
)

# v8（v0043）：增量扫描所需的跨扫描哈希缓存 + 媒体探测缓存（幂等；行由扫描期写入）。
# - hash_cache：键 (dev, inode)，内容身份由 size/mtime_ns 校验；命中即复用
#   quick_hash/full_hash，避免每次全量重算 SHA-256（docs/INCREMENTAL_SCAN_CONTRACT.md）。
# - media_probe_cache：保留策略“优先带内嵌字幕/音轨”需要 FFmpeg 探测结果缓存。
_V8_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS hash_cache (
        dev INTEGER NOT NULL,
        inode INTEGER NOT NULL,
        path TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        quick_hash TEXT,
        full_hash TEXT,
        algo TEXT NOT NULL,
        sample_kb INTEGER NOT NULL,
        sample_version INTEGER NOT NULL,
        first_scan_id TEXT,
        last_scan_id TEXT,
        first_seen_epoch REAL,
        last_seen_epoch REAL,
        last_used REAL NOT NULL,
        PRIMARY KEY (dev, inode)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hash_cache_used
        ON hash_cache(last_used)
    """,
    """
    CREATE TABLE IF NOT EXISTS media_probe_cache (
        dev INTEGER NOT NULL,
        inode INTEGER NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        probed_epoch REAL NOT NULL,
        last_used REAL NOT NULL,
        PRIMARY KEY (dev, inode)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_media_probe_cache_used
        ON media_probe_cache(last_used)
    """,
)

# v5：scan_files 重建后需要重新创建的索引（DROP TABLE 会一并删除旧索引）。
_V5_SCAN_FILES_INDEX_STATEMENTS = (
    """
    CREATE INDEX IF NOT EXISTS idx_scan_files_path
        ON scan_files(scan_id, path)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_scan_files_group
        ON scan_files(scan_id, group_id)
    """,
)

# v5：FTS5 外部内容表 + 触发器（仅在探测到 FTS5 + trigram 时创建）。
_FTS_CREATE_STATEMENT = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS scan_files_fts USING fts5("
    "path, content='scan_files', content_rowid='rowid',"
    " tokenize='trigram')"
)

_FTS_TRIGGER_STATEMENTS = (
    "CREATE TRIGGER IF NOT EXISTS scan_files_fts_ai"
    " AFTER INSERT ON scan_files BEGIN"
    " INSERT INTO scan_files_fts(rowid, path) VALUES (new.rowid, new.path);"
    " END",
    "CREATE TRIGGER IF NOT EXISTS scan_files_fts_ad"
    " AFTER DELETE ON scan_files BEGIN"
    " INSERT INTO scan_files_fts(scan_files_fts, rowid, path)"
    " VALUES ('delete', old.rowid, old.path);"
    " END",
    "CREATE TRIGGER IF NOT EXISTS scan_files_fts_au"
    " AFTER UPDATE ON scan_files BEGIN"
    " INSERT INTO scan_files_fts(scan_files_fts, rowid, path)"
    " VALUES ('delete', old.rowid, old.path);"
    " INSERT INTO scan_files_fts(rowid, path) VALUES (new.rowid, new.path);"
    " END",
)

# v3：plan B 断点记录明细表（v2 升级时只建表，旧 payload 内 records 惰性回退读取）。
_V3_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS scan_checkpoint_records (
        scan_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        path TEXT NOT NULL,
        size INTEGER,
        mtime_ns INTEGER,
        inode INTEGER,
        dev INTEGER,
        nlink INTEGER,
        quick_hash TEXT,
        hash TEXT,
        PRIMARY KEY (scan_id, ordinal)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_scan_checkpoint_records_path
        ON scan_checkpoint_records(scan_id, path)
    """,
)

# v2：v1 数据库升级时需要补的 scan_groups 结果索引列。
V2_SCAN_GROUP_COLUMNS = (
    ("wasted_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("size", "INTEGER NOT NULL DEFAULT 0"),
    ("similarity_score", "INTEGER NOT NULL DEFAULT 0"),
    ("created_epoch", "REAL NOT NULL DEFAULT 0"),
    ("group_hash", "TEXT"),
)

# v8（v0043）：增量扫描所需的 scan_groups 索引列（组内新增/变化文件数），
# 供“只看本次新增/变化的重复组”筛选与徽标；历史组回填 0。
V8_SCAN_GROUP_COLUMNS = (
    ("new_files", "INTEGER NOT NULL DEFAULT 0"),
    ("changed_files", "INTEGER NOT NULL DEFAULT 0"),
)

# v2 迁移兜底建表/建索引（幂等；正常新建库已在 _SCHEMA_STATEMENTS 中创建）。
_V2_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS scan_files (
        scan_id TEXT NOT NULL,
        group_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        path TEXT NOT NULL,
        size INTEGER NOT NULL DEFAULT 0,
        is_keep INTEGER NOT NULL DEFAULT 0,
        default_selected INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (scan_id, group_id, file_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_scan_files_path
        ON scan_files(scan_id, path)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_scan_files_group
        ON scan_files(scan_id, group_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_scan_groups_gid
        ON scan_groups(scan_id, group_id)
    """,
)


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _loads(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError, UnicodeError):
        return None


def group_identity(payload, ordinal):
    """分组稳定 ID：payload 内 group_id 优先，否则按 ordinal 生成。"""
    group_id = payload.get("group_id") if isinstance(payload, dict) else None
    if not group_id:
        group_id = "g-%06d" % (_as_int(ordinal) + 1)
    return str(group_id)


def group_index_columns(payload, ordinal):
    """从 group payload 提取 v2 索引列；写入与 v1→v2 回填共用。"""
    if not isinstance(payload, dict):
        payload = {}
    match_type = payload.get("match_type")
    files = payload.get("files")
    created = 0
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict):
                created = max(created, _as_int(item.get("created_ts")))
    similarity = 100
    if match_type in ("similar", "video"):
        similarity = _as_int(payload.get("similarity_score"))
    group_hash = payload.get("hash")
    # v0043：组内“本次新增 / 内容变化”的文件数（增量扫描筛选与徽标用）。
    new_files = payload.get("new_files")
    changed_files = payload.get("changed_files")
    if not isinstance(new_files, int) or not isinstance(changed_files, int):
        computed_new = 0
        computed_changed = 0
        if isinstance(files, list):
            for item in files:
                if not isinstance(item, dict):
                    continue
                if item.get("new_since_last_scan"):
                    computed_new += 1
                if item.get("changed_since_last_scan"):
                    computed_changed += 1
        if not isinstance(new_files, int):
            new_files = computed_new
        if not isinstance(changed_files, int):
            changed_files = computed_changed
    return {
        "group_id": group_identity(payload, ordinal),
        "match_type": match_type,
        "wasted_bytes": _as_int(payload.get("wasted_bytes")),
        "size": _as_int(payload.get("size")),
        "similarity_score": similarity,
        "created_epoch": float(created),
        "group_hash": group_hash if isinstance(group_hash, str) else "",
        "new_files": max(0, int(new_files)),
        "changed_files": max(0, int(changed_files)),
    }


def group_file_rows(scan_id, ordinal, payload):
    """从 group payload 提取 scan_files 明细行（搜索/统计用）。"""
    if not isinstance(payload, dict):
        payload = {}
    group_id = group_identity(payload, ordinal)
    files = payload.get("files")
    rows = []
    if not isinstance(files, list):
        return rows
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            continue
        file_id = item.get("file_id")
        if not file_id:
            file_id = "f-%06d" % (index + 1)
        rows.append((
            scan_id,
            group_id,
            str(file_id),
            str(item.get("path") or ""),
            _as_int(item.get("size")),
            1 if item.get("is_keep") else 0,
            1 if item.get("default_selected") else 0,
        ))
    return rows


def fts_path_phrase(needle):
    """把搜索词转成 FTS5 短语查询（双引号转义，trigram 子串语义）。"""
    text = str(needle or "")
    return '"' + text.replace('"', '""') + '"'


class Database(object):
    """单连接 SQLite 封装：线程安全（RLock）、短事务、可恢复。"""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.recovered_from = None
        self._lock = threading.RLock()
        self._conn = None
        self._tx_depth = 0
        self._closed = False
        # v5：FTS5(trigram) 路径索引可用性（不支持/运行时报错时回退 LIKE）
        self.fts_enabled = False
        self.fts_note = ""
        self.fts_probe_error = ""
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
        # v5：建表/迁移后初始化 FTS5(trigram) 路径索引（不支持则回退 LIKE）。
        self._ensure_search_index()

    def _apply_migrations(self, from_version):
        """按版本迁移；迁移与版本号写入在同一事务内（失败整体回滚）。"""
        version = int(from_version)
        while version < CURRENT_SCHEMA_VERSION:
            next_version = version + 1
            if next_version == 2:
                self._migrate_v2()
            elif next_version == 3:
                self._migrate_v3()
            elif next_version == 4:
                self._migrate_v4()
            elif next_version == 5:
                self._migrate_v5()
            elif next_version == 6:
                self._migrate_v6()
            elif next_version == 7:
                self._migrate_v7()
            elif next_version == 8:
                self._migrate_v8()
            version = next_version
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) "
                "VALUES('schema_version', ?)", (str(version),))
            self._conn.execute("PRAGMA user_version = %d" % version)

    def _migrate_v2(self):
        """v1→v2：scan_groups 增加结果索引列，新建 scan_files 并回填历史数据。

        幂等：列/表/索引按需创建；回填用 UPDATE + INSERT OR IGNORE，
        重复执行不会产生重复行。历史 JSON 源文件不动。
        """
        conn = self._conn
        existing = {str(row[1]) for row in
                    conn.execute("PRAGMA table_info(scan_groups)").fetchall()}
        for column, declaration in V2_SCAN_GROUP_COLUMNS:
            if column not in existing:
                conn.execute("ALTER TABLE scan_groups ADD COLUMN %s %s"
                             % (column, declaration))
        for statement in _V2_MIGRATION_STATEMENTS:
            conn.execute(statement)
        groups, files = self._backfill_scan_group_columns()
        logger.info(u"SQLite schema 迁移到 v2 完成：回填 %d 组 / %d 文件",
                    groups, files)

    def _migrate_v3(self):
        """v2→v3：只新建 scan_checkpoint_records（幂等）。

        旧库 scan_checkpoints.payload_json 里可能仍内嵌 records；不重写历史行，
        读取时由 Store.load_checkpoint 惰性回退（docs/plan B）。
        """
        conn = self._conn
        for statement in _V3_MIGRATION_STATEMENTS:
            conn.execute(statement)
        logger.info(u"SQLite schema 迁移到 v3 完成：已创建 checkpoint records 表")

    def _migrate_v4(self):
        """v3→v4：只新建 subtitle_chunks（幂等；字幕分片行运行时写入）。"""
        conn = self._conn
        for statement in _V4_MIGRATION_STATEMENTS:
            conn.execute(statement)
        logger.info(u"SQLite schema 迁移到 v4 完成：已创建 subtitle_chunks 表")

    def _migrate_v5(self):
        """v4→v5：重建 scan_files 去掉冗余 payload_json（不用 DROP COLUMN）。

        兼容旧 SQLite：新表→INSERT SELECT→DROP 旧表→RENAME→重建索引；
        行数/路径/分组/保留/勾选数据保持不变。FTS 派生对象在重建前清理，
        迁移完成后由 _ensure_search_index 重新创建并 rebuild。
        """
        conn = self._conn
        if not self._table_exists("scan_files"):
            return
        columns = {str(row[1]) for row in
                   conn.execute("PRAGMA table_info(scan_files)").fetchall()}
        if "payload_json" in columns:
            self._drop_search_index()
            conn.execute("DROP TABLE IF EXISTS scan_files_v5")
            conn.execute(
                "CREATE TABLE scan_files_v5 ("
                "scan_id TEXT NOT NULL,"
                " group_id TEXT NOT NULL,"
                " file_id TEXT NOT NULL,"
                " path TEXT NOT NULL,"
                " size INTEGER NOT NULL DEFAULT 0,"
                " is_keep INTEGER NOT NULL DEFAULT 0,"
                " default_selected INTEGER NOT NULL DEFAULT 0,"
                " PRIMARY KEY (scan_id, group_id, file_id))")
            conn.execute(
                "INSERT INTO scan_files_v5"
                "(scan_id, group_id, file_id, path, size, is_keep,"
                " default_selected)"
                " SELECT scan_id, group_id, file_id, path, size, is_keep,"
                " default_selected FROM scan_files")
            before = int(conn.execute(
                "SELECT COUNT(*) FROM scan_files_v5").fetchone()[0])
            conn.execute("DROP TABLE scan_files")
            conn.execute("ALTER TABLE scan_files_v5 RENAME TO scan_files")
            logger.info(
                u"SQLite schema v5：scan_files 去掉 payload_json，保留 %d 行",
                before)
        for statement in _V5_SCAN_FILES_INDEX_STATEMENTS:
            conn.execute(statement)

    def _migrate_v6(self):
        """v5→v6：只新建 subtitle_batches（幂等；批次行提交时写入）。"""
        conn = self._conn
        for statement in _V6_MIGRATION_STATEMENTS:
            conn.execute(statement)
        logger.info(u"SQLite schema 迁移到 v6 完成：已创建 subtitle_batches 表")

    def _migrate_v7(self):
        """v6→v7：新建 subtitle_batch_groups 与 scan_file_deletions（幂等）。"""
        conn = self._conn
        for statement in _V7_MIGRATION_STATEMENTS:
            conn.execute(statement)
        logger.info(u"SQLite schema 迁移到 v7 完成：已创建 subtitle_batch_groups /"
                    u" scan_file_deletions 表")

    def _migrate_v8(self):
        """v7→v8：增量扫描哈希缓存 + 扫描组“新增/变化文件数”索引列（幂等）。"""
        conn = self._conn
        for statement in _V8_MIGRATION_STATEMENTS:
            conn.execute(statement)
        existing = {str(row[1]) for row in
                    conn.execute("PRAGMA table_info(scan_groups)").fetchall()}
        for column, declaration in V8_SCAN_GROUP_COLUMNS:
            if column not in existing:
                conn.execute("ALTER TABLE scan_groups ADD COLUMN %s %s"
                             % (column, declaration))
        logger.info(u"SQLite schema 迁移到 v8 完成：已创建 hash_cache /"
                    u" media_probe_cache 表并补齐 scan_groups 增量列")

    def _backfill_scan_group_columns(self):
        """按 rowid 分批回填 scan_groups 索引列与 scan_files（不整表载入）。"""
        conn = self._conn
        last_rowid = 0
        groups = 0
        files = 0
        while True:
            rows = conn.execute(
                "SELECT rowid AS rid, scan_id, ordinal, payload_json"
                " FROM scan_groups WHERE rowid > ? ORDER BY rowid ASC LIMIT ?",
                (last_rowid, BATCH_SIZE)).fetchall()
            if not rows:
                break
            updates = []
            file_rows = []
            for row in rows:
                last_rowid = int(row["rid"])
                payload = _loads(row["payload_json"])
                meta = group_index_columns(payload, row["ordinal"])
                updates.append((
                    meta["wasted_bytes"], meta["size"],
                    meta["similarity_score"], meta["created_epoch"],
                    meta["group_hash"], last_rowid,
                ))
                file_rows.extend(group_file_rows(
                    row["scan_id"], row["ordinal"], payload))
                groups += 1
            conn.executemany(
                "UPDATE scan_groups SET wasted_bytes=?, size=?,"
                " similarity_score=?, created_epoch=?, group_hash=?"
                " WHERE rowid=?", updates)
            if file_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO scan_files"
                    "(scan_id, group_id, file_id, path, size, is_keep,"
                    " default_selected) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    file_rows)
                files += len(file_rows)
        return groups, files

    # ------------------------------------------------------------------
    # v5：FTS5(trigram) 路径索引（不可用时回退 LIKE 子串搜索）
    # ------------------------------------------------------------------
    def _table_exists(self, name):
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (str(name),)).fetchone()
        return row is not None

    def _fts_triggers(self):
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
            " AND name IN (?, ?, ?)", FTS_TRIGGER_NAMES).fetchall()
        return [str(row[0]) for row in rows]

    def _probe_fts_trigram(self):
        """在临时表上真实创建 fts5(trigram)：仅 FTS5 可用但无 trigram 时返回 False。"""
        try:
            self._conn.execute("DROP TABLE IF EXISTS temp.dc_fts_probe")
            self._conn.execute(
                "CREATE VIRTUAL TABLE temp.dc_fts_probe USING fts5("
                "path, tokenize='trigram')")
            self._conn.execute("DROP TABLE temp.dc_fts_probe")
            return True
        except sqlite3.Error as exc:
            try:
                self._conn.execute("DROP TABLE IF EXISTS temp.dc_fts_probe")
            except sqlite3.Error:
                pass
            self.fts_probe_error = str(exc)
            logger.info(u"FTS5 trigram 探测失败（将回退 LIKE）: %s", exc)
            return False

    def _drop_search_index(self):
        """只清理 FTS 派生表/触发器，不触碰 scan_files 主数据。"""
        for name in FTS_TRIGGER_NAMES:
            try:
                self._conn.execute("DROP TRIGGER IF EXISTS %s" % name)
            except sqlite3.Error:
                pass
        try:
            self._conn.execute("DROP TABLE IF EXISTS scan_files_fts")
        except sqlite3.Error:
            pass
        self.fts_enabled = False
        self.fts_note = "dropped"

    def _rebuild_search_index(self):
        """重建外部内容 FTS 索引，并校验行数与 scan_files 一致（写日志）。"""
        conn = self._conn
        conn.execute(
            "INSERT INTO scan_files_fts(scan_files_fts) VALUES('rebuild')")
        rows = int(conn.execute("SELECT COUNT(*) FROM scan_files").fetchone()[0])
        fts_rows = int(conn.execute(
            "SELECT COUNT(*) FROM scan_files_fts").fetchone()[0])
        if rows != fts_rows:
            logger.warning(
                u"FTS5 路径索引一致性校验失败: scan_files=%d, fts=%d",
                rows, fts_rows)
        else:
            logger.info(
                u"FTS5 路径索引 rebuild 完成且一致: %d 行", rows)

    def _verify_search_index(self):
        conn = self._conn
        try:
            rows = int(conn.execute(
                "SELECT COUNT(*) FROM scan_files").fetchone()[0])
            fts_rows = int(conn.execute(
                "SELECT COUNT(*) FROM scan_files_fts").fetchone()[0])
        except sqlite3.Error as exc:
            logger.warning(u"FTS5 路径索引校验失败，重建索引: %s", exc)
            self._rebuild_search_index()
            return
        if rows != fts_rows:
            logger.warning(
                u"FTS5 路径索引行数不一致（scan_files=%d, fts=%d），重建",
                rows, fts_rows)
            self._rebuild_search_index()

    def _ensure_search_index(self):
        """打开数据库时初始化 FTS5(trigram) 路径索引；失败安全回退 LIKE。"""
        with self._lock:
            if not self._table_exists("scan_files"):
                self.fts_enabled = False
                return
            probe_ok = self._probe_fts_trigram()
            table_exists = self._table_exists(FTS_TABLE)
            triggers = self._fts_triggers()
            if not probe_ok:
                self.fts_enabled = False
                self.fts_note = "like-fallback"
                if table_exists or triggers:
                    # 同一 DB 曾在支持 FTS5 的环境创建过索引：清理派生对象，
                    # 避免 scan_files 触发器在不支持 FTS5 的 SQLite 上写失败。
                    self._drop_search_index()
                    logger.warning(
                        u"当前 SQLite 不支持 FTS5 trigram；已清理 "
                        u"scan_files FTS 派生表/触发器，路径搜索回退 LIKE")
                else:
                    logger.info(
                        u"当前 SQLite 不支持 FTS5 trigram，路径搜索使用 LIKE")
                return
            missing_triggers = set(FTS_TRIGGER_NAMES) - set(triggers)
            try:
                with self.transaction():
                    self._conn.execute(_FTS_CREATE_STATEMENT)
                    for statement in _FTS_TRIGGER_STATEMENTS:
                        self._conn.execute(statement)
                    if not table_exists or missing_triggers:
                        self._rebuild_search_index()
                    else:
                        self._verify_search_index()
                self.fts_enabled = True
                self.fts_note = "fts5-trigram"
            except sqlite3.Error as exc:
                self.fts_enabled = False
                self.fts_note = "like-fallback: %s" % exc
                logger.warning(
                    u"创建 FTS5 路径索引失败，降级 LIKE 子串搜索: %s", exc)
                try:
                    self._drop_search_index()
                except sqlite3.Error:
                    pass

    def disable_fts(self, reason=""):
        """运行时 FTS 查询报错：仅本连接降级 LIKE（不崩、不改主数据）。"""
        with self._lock:
            if self.fts_enabled:
                logger.warning(
                    u"FTS5 路径查询运行时报错，本连接降级 LIKE: %s", reason)
            self.fts_enabled = False
            self.fts_note = "runtime-fallback"

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
    # 诊断 / 备份 / 维护（v0021 P0）
    # ------------------------------------------------------------------
    _TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def file_stats(self):
        """数据库主文件/WAL/SHM 尺寸（诊断与维护前后对比用）。"""
        with self._lock:
            self._require_open()
            result = {"path": self.path, "size": 0, "wal_size": 0,
                      "shm_size": 0, "total_size": 0}
            for key, suffix in (("size", ""), ("wal_size", "-wal"),
                                ("shm_size", "-shm")):
                try:
                    result[key] = int(os.path.getsize(self.path + suffix))
                except OSError:
                    result[key] = 0
            result["total_size"] = (result["size"] + result["wal_size"]
                                    + result["shm_size"])
            return result

    def table_row_counts(self, tables):
        """指定表的行数（表名白名单校验，避免拼接注入）。"""
        counts = {}
        with self._lock:
            self._require_open()
            for table in tables or ():
                table = str(table)
                if not self._TABLE_NAME_RE.match(table):
                    continue
                try:
                    row = self._conn.execute(
                        "SELECT COUNT(*) FROM " + table).fetchone()
                    counts[table] = int(row[0]) if row is not None else 0
                except sqlite3.Error:
                    counts[table] = 0
        return counts

    def backup_to(self, target_path):
        """sqlite3 在线备份 API（WAL 中已提交内容一并写入）。

        备份文件是独立可打开的数据库，不依赖源库的 WAL/SHM。
        """
        target_path = os.path.abspath(target_path)
        directory = os.path.dirname(target_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._lock:
            self._require_open()
            destination = sqlite3.connect(target_path)
            try:
                self._conn.backup(destination)
                destination.commit()
            finally:
                destination.close()
            try:
                os.chmod(target_path, 0o600)
            except OSError:
                pass
        return target_path

    def optimize(self):
        """PRAGMA optimize：更新查询计划统计（轻量、可在运行期执行）。"""
        with self._lock:
            self._require_open()
            self._conn.execute("PRAGMA optimize")

    def vacuum(self):
        """VACUUM 重建数据库（不能在事务内执行；调用方需先检查磁盘空间）。"""
        with self._lock:
            self._require_open()
            self._conn.execute("VACUUM")

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
