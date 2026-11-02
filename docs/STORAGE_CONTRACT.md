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
CREATE TABLE scan_files(
  scan_id TEXT NOT NULL,
  group_id TEXT NOT NULL,
  file_id TEXT,
  path TEXT NOT NULL,
  size INTEGER,
  is_keep INTEGER,
  default_selected INTEGER,
  payload_json TEXT
);
CREATE INDEX idx_scan_files_path ON scan_files(scan_id,path);
CREATE INDEX idx_scan_groups_type ON scan_groups(scan_id, match_type);

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

subtitle_jobs(
  job_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  updated_epoch REAL NOT NULL,
  status TEXT
);
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
- 启动时检测 `scans/*/meta.json`、`groups.jsonl`、`deletions/*.meta.json`、`*.jsonl`、`feature_cache.json`，导入 SQLite；v1→v2 从 payload 回填。
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

## 5. 备份/升级

- 升级/停止前执行 `PRAGMA wal_checkpoint(TRUNCATE)`；关闭连接。
- 数据库文件与 var 一起备份；WAL/SHM 不单独依赖。
- 包 ID/数据目录/ACL/URL 不变。
- 旧版本回滚：旧代码仍能读旧 JSON；SQLite 是新增文件，不破坏旧数据。

## 6. 测试

- 迁移：旧 scans/deletions/feature_cache JSON 导入且幂等；DB 优先。
- CRUD：meta/groups/jobs/results/feature cache/checkpoint。
- 并发读写、WAL、损坏恢复、schema version。
- 中断状态标记。
- 现有 219 测试继续通过。
- QA 更新 BE-SIM-CACHE 等检查从 JSON 路径改为 SQLite。
