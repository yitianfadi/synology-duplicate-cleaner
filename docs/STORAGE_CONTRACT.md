# 存储层契约（v1.4，SQLite 元数据）

用户已确认：**业务元数据统一 SQLite**，但不把所有东西塞进数据库：
- SQLite：扫描、组、文件元数据、删除任务、特征缓存、checkpoint、字幕任务元数据。
- 文件：settings.json（启动兜底）、audit.jsonl、运行日志、SRT/VTT、导出文件。
- 视频指纹缓存已有 SQLite，可保留独立库或后续合并；本版不强制合并。
- JSON 只作为旧数据导入/导出兼容；不直接删除旧文件。

## 1. 数据库

路径：`<var>/dupcleaner.sqlite3`（权限 600，duplicatecleaner 所有）

- Python 3.8 标准库 sqlite3。
- `PRAGMA journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`、`foreign_keys=ON`。
- 单写多读；所有事务短小；写锁串行。
- schema_version 表；迁移按版本。

表：
```sql
schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

scans(
  scan_id TEXT PRIMARY KEY,
  meta_json TEXT NOT NULL,
  created_epoch REAL NOT NULL,
  updated_epoch REAL NOT NULL,
  status TEXT NOT NULL,
  interrupted INTEGER NOT NULL DEFAULT 0
);

scan_groups(
  scan_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  group_id TEXT NOT NULL,
  match_type TEXT,
  wasted_bytes INTEGER DEFAULT 0,
  size INTEGER DEFAULT 0,
  similarity_score INTEGER DEFAULT 0,
  created_epoch REAL DEFAULT 0,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (scan_id, ordinal)
);
-- v5：scan_files 只存路径/分组/保留/勾选，不再冗余保存 payload_json
-- （group payload 才是结果真源）。
CREATE TABLE scan_files(
  scan_id TEXT NOT NULL,
  group_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  path TEXT NOT NULL,
  size INTEGER NOT NULL DEFAULT 0,
  is_keep INTEGER NOT NULL DEFAULT 0,
  default_selected INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (scan_id, group_id, file_id)
);
CREATE INDEX idx_scan_files_path ON scan_files(scan_id,path);
CREATE INDEX idx_scan_files_group ON scan_files(scan_id,group_id);
CREATE INDEX idx_scan_groups_type ON scan_groups(scan_id, match_type);

-- v5：FTS5 外部内容索引（仅当 SQLite 支持 FTS5 + trigram tokenizer 时创建；
-- content_rowid 指向 scan_files.rowid，三个触发器保证增删改同步）。
CREATE VIRTUAL TABLE scan_files_fts USING fts5(
  path, content='scan_files', content_rowid='rowid', tokenize='trigram'
);
CREATE TRIGGER scan_files_fts_ai AFTER INSERT ON scan_files ...
CREATE TRIGGER scan_files_fts_ad AFTER DELETE ON scan_files ...
CREATE TRIGGER scan_files_fts_au AFTER UPDATE ON scan_files ...

deletion_jobs(
  job_id TEXT PRIMARY KEY,
  meta_json TEXT NOT NULL,
  started_epoch REAL NOT NULL,
  status TEXT
);

deletion_results(
  job_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (job_id, ordinal)
);

feature_cache(
  cache_key TEXT PRIMARY KEY,
  kind TEXT,
  payload_json TEXT NOT NULL,
  last_used REAL NOT NULL
);

scan_checkpoints(
  scan_id TEXT PRIMARY KEY,
  stage TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  updated_epoch REAL NOT NULL
);

scan_checkpoint_records(
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
);

subtitle_jobs(
  job_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  updated_epoch REAL NOT NULL,
  status TEXT
);

-- v7（v0032）
subtitle_batch_groups(
  group_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  created_epoch REAL NOT NULL,
  updated_epoch REAL NOT NULL
);

scan_file_deletions(
  scan_id TEXT NOT NULL,
  path TEXT NOT NULL,
  job_id TEXT,
  mode TEXT,
  deleted_epoch REAL NOT NULL,
  PRIMARY KEY (scan_id, path)
);
CREATE INDEX idx_scan_file_deletions_scan ON scan_file_deletions(scan_id);
```

## 2. Store 兼容 API

`Store` 对外方法保持签名，不改 scanner/deletion 调用方：
- save_scan_meta / load_scan_meta / list_scan_metas / delete_scan
- write_scan_groups / append_scan_groups / iter_scan_groups
- save_job_meta / load_job_meta / append_job_result / iter_job_results / list_job_metas / delete_job
- feature cache 方法（新增，FeatureCache 改用）
- checkpoint 方法（新增）
- settings/audit/log 保持文件。

旧 JSON 兼容：
- v2 起 scan_groups 增加 wasted_bytes/size/similarity_score/created_epoch，scan_files 保存路径/保留/勾选明细，供 SQL 分页/搜索/排序。
- v3 起暂停断点 FileRecord 拆到 scan_checkpoint_records，scan_checkpoints 只存 stage/progress/stats；旧 payload records 仍可回退读取。
- v4 起字幕任务 ASR 分片缓存落 subtitle_chunks，支持暂停/续跑。
- v5 起 scan_files 去掉 payload_json；v4→v5 用“建新表→INSERT SELECT→DROP→RENAME→重建索引”迁移（兼容旧 SQLite，不依赖 DROP COLUMN），行数/路径/分组数据不变。
- v6（v0028）新增 subtitle_batches；v7（v0032）新增 subtitle_batch_groups 与 scan_file_deletions（只建表/索引，幂等）。
- **删除标记过滤（v7）**：删除任务成功（`recycled`/`deleted`）或发现文件已不存在（`NOT_FOUND`）时写入 `scan_file_deletions`；
  `iter_scan_groups` / `query_scan_groups_page` / `scan_file_stats` / CSV / XLSX 读取时剔除这些文件，组内剩余文件 <2 的组不再返回，
  keeper 被删时按剩余文件重算保留项与 `wasted_bytes`。无删除标记时保持旧行为（不做剩余数过滤）。
- 删除扫描时一并删除 `scan_file_deletions` 行。
- 启动时 `scans/*/meta.json`、`groups.jsonl`、`deletions/*.meta.json`、`*.jsonl`、`feature_cache.json`，导入 SQLite；v1→v2 从 payload 回填。
- 导入使用 INSERT OR IGNORE，幂等；不删除源文件；写 `schema_meta('migrated_v1')=1`。
- 读取时 DB 优先；空则回退旧 JSON。
- 删除扫描/任务时同时清理旧 JSON 文件（如果存在）。

## 3. 启动状态

- 进程启动时把 DB 中 status 为 queued/running/paused/stopping 的扫描标记为 `interrupted`，并写 checkpoint 状态；不自动续跑。
- 完成/失败/停止状态保留。
- 字幕任务沿用现有 SERVICE_RESTART 策略；本版将其元数据写入 subtitle_jobs（如果实现），不改变失败语义。

## 4. 分页与性能

- `iter_scan_groups` 按 ordinal 流式查询，不一次性加载全部。
- API 分页/搜索可基于 DB，但本版至少保证迭代不读全文件。
- 每 N 组批量事务；WAL checkpoint 在退出/低峰执行。
- 数据库损坏：启动时备份 `dupcleaner.sqlite3.corrupt-<ts>` 并重建空库；旧 JSON 可重新导入。

### 4.1 FTS5(trigram) 路径搜索（schema v5）

- 能力探测：打开 DB 时在临时表真实执行 `CREATE VIRTUAL TABLE ... fts5(path, tokenize='trigram')` 并删除；**只有 FTS5 与 trigram tokenizer 都可用才启用**（仅 FTS5 而 trigram 不可用时 unicode61 不满足子串语义，继续 LIKE）。
- 启用时创建 `scan_files_fts(content='scan_files', content_rowid='rowid', tokenize='trigram')` + 3 个 AFTER INSERT/DELETE/UPDATE 触发器，使写入/重写/删除自动同步；打开时若索引缺失/触发器缺失/行数不一致会 rebuild，并记录 `scan_files 行数 == FTS 行数` 一致性日志。
- 查询语义（与旧 LIKE 子串、ASCII 大小写不敏感一致）：
  - `needle` 长度 >= 3 且 FTS 可用：路径走 `scan_files_fts MATCH "短语"`（双引号转义），命中 group_id 物化到连接级 TEMP 表后再参与 COUNT/分页，避免 IN 子查询被优化器逐组重算；
  - `needle` 长度 < 3、或 FTS 不可用/运行时查询报错：自动回退 `lower(path) LIKE '%needle%' ESCAPE '\'`（%/_/\ 按字面转义）；
  - group hash 搜索条件始终 LIKE，不受 FTS 影响。
- 运行时 FTS 查询报错只在本连接降级 LIKE 并记 warning，不崩溃、不改主数据；若 DB 曾在支持 FTS 的环境建过索引、之后在不支持 FTS5/trigram 的环境打开，启动时安全 DROP 触发器与 FTS 派生表（不动 scan_files 主数据）并回退 LIKE，避免触发器写失败。
- 基准（`python qa/bench_search.py`，10k 组 × 10 文件 = 100k scan_files，本机 Python 3.12/SQLite 3.45）：
  | 场景 | P50 | P95 |
  |---|---|---|
  | FTS 命中（100 组） | 77.2 ms | 79.1 ms |
  | FTS 未命中 | 62.7 ms | 70.9 ms |
  | 强制 LIKE 命中 | 162.0 ms | 164.4 ms |
  | 强制 LIKE 未命中 | 151.4 ms | 155.7 ms |
  目标 FTS P95 < 500ms：PASS。写入 100k 行（含 FTS 触发器）约 7.1s（一次性扫描入库成本；查询收益显著）。
  注：不同机器/负载下实测 P95 约在 80~400ms 之间波动，仍满足目标。

## 5. 诊断/备份/维护

- GET /api/diagnostics（管理员）：DB/WAL/SHM、schema、表行数、缓存、FFmpeg/ASR、活动任务、密钥是否已设置；不返回密钥值。
- POST /api/storage/backup（管理员）：sqlite3 backup API 生成 <var>/backups/dupcleaner-<ts>.sqlite3，权限 600。
- POST /api/storage/maintenance（管理员，confirm=true）：WAL checkpoint(TRUNCATE) + PRAGMA optimize；vacuum=true 需空间检查。
- 设置/TLS 文件 chmod 600；全局异常只返回固定文案+request_id。

## 5. 备份/升级

- 升级/停止前执行 `PRAGMA wal_checkpoint(TRUNCATE)`；关闭连接。
- 数据库文件与 var 一起备份；WAL/SHM 不单独依赖。
- 包 ID/数据目录/ACL/URL 不变。
- 旧版本回滚：旧代码仍能读旧 JSON；SQLite 是新增文件，不破坏旧数据。

## 6. 测试

- 迁移：旧 scans/deletions/feature_cache JSON 导入且幂等；DB 优先。
- CRUD：meta/groups/jobs/results/feature cache/checkpoint。
- v4→v5 迁移（scan_files 去 payload_json）、FTS5 trigram 与 LIKE 搜索/转义等价、短 needle/无 FTS 回退、触发器写入/重写/删除同步、100k 搜索基准。
- 并发读写、WAL、损坏恢复、schema version。
- 中断状态标记。
- 现有 219 测试继续通过。
- QA 更新 BE-SIM-CACHE 等检查从 JSON 路径改为 SQLite。
