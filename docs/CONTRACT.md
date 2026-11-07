# 实现契约（前后端共享，版本 1.0）

> 本文件是前后端并行开发的唯一接口约定。任何一方需要改动字段，先改本文件并在团队任务中说明。
> 产品名：**DuplicateCleaner**；显示名：**重复文件清理与字幕**；SPK 版本：**1.1.0-0030**。
> v1.1 新增内容相似度模式；相似模式字段与行为见 [SIMILARITY_CONTRACT.md](SIMILARITY_CONTRACT.md)，**精准模式规则不变**。

## 1. 仓库与写入边界

```
src/backend/            Python 3.8+ 标准库后端（无第三方运行依赖）
  app.py                启动入口（参数、静态文件服务、HTTP(S)）
  dupcleaner/           业务模块
  tests/                后端单元测试（unittest）
src/web/                纯静态 SPA（无 CDN 依赖）
spk/                    SPK 外壳：INFO、scripts、conf、ui、图标
docs/                   方案/原型/打包/部署文档
build/                  构建脚本
qa/                     独立验收脚本与报告
dist/                   构建产物 .spk（不入库也不提交给用户以外的人）
```

- 后端 owner 只写 `src/backend/**`。
- 前端 owner 只写 `src/web/**`。
- Lead 写 `spk/**`、`docs/**`、`build/**`、`README.md`。
- QA 写 `qa/**`，只读其余目录；发现问题通过任务/消息反馈，不直接改他人代码。

## 2. 服务与部署

- 后端默认监听 `127.0.0.1:8787`（开发/反代友好；SPK 的 `start-stop-status` 显式传 `--host 0.0.0.0`），可用 `--port` 改；同时提供静态 UI 与 REST API，同源，无 CORS。
- DSM 套件桌面入口 `ui/index.html` 负责跳转到
  `http://<当前DSM主机名>:8787/`（若后端 TLS 启用则 https），避免 DSM HTTPS 页面内混合内容。
- 数据目录：环境变量 `DUP_DATA_DIR`，SPK 内为 `/var/packages/DuplicateCleaner/var`；开发时默认 `./data`。
- 后端必须兼容 Python 3.8（不能用 `X | Y` 类型、`match`、`tomllib`、`str.removeprefix` 等 3.9+ 语法）。
- 开发模式（仅本机/测试）：`DUP_CLEANER_DEV=1` 时允许 `DUP_DEV_ROOTS`（分号分隔目录）作为虚拟共享文件夹，
  并允许账号 `admin` / 环境变量 `DUP_DEV_PASSWORD`（默认 `admin`）登录。生产 SPK 的启动脚本不得设置该变量。

## 3. 通用响应

成功：HTTP 200/202，直接返回约定对象。
失败：HTTP 4xx/5xx，JSON：

```json
{"ok": false, "error": {"code": "INVALID_PATH", "message": "中文可读信息", "details": {}}}
```

登录后所有 `/api/*` 请求需 Cookie `dc_session`；所有非 GET 请求还必须带
`X-CSRF-Token`（登录响应返回）。服务端校验 Origin/Host 同源（开发模式允许无 Origin 的 curl）。
Cookie：`HttpOnly; SameSite=Strict; Path=/; Max-Age=28800`，TLS 时加 `Secure`。

常见错误码：`AUTH_REQUIRED`、`AUTH_FAILED`、`FORBIDDEN`、`CSRF_FAILED`、`NOT_FOUND`、`INVALID_ARGUMENT`、
`INVALID_PATH`、`PATH_NOT_ALLOWED`、`SCAN_BUSY`、`SCAN_NOT_DONE`、`PLAN_EXPIRED`、`PLAN_INVALID`、
`HASH_MISMATCH`、`FILE_CHANGED`、`RECYCLE_UNAVAILABLE`、`INTERNAL_ERROR`。

字幕探测类错误码（`PROVIDER_AUTH_FAILED`、`PROVIDER_REJECTED`、`MODELS_UNSUPPORTED`、
`PROVIDER_HTTP_ERROR`、`PROVIDER_NETWORK_ERROR`、`PROVIDER_TIMEOUT`、`AUDIO_FAILED`、
`FFMPEG_UNAVAILABLE`、`PROBE_BUSY`）见 [SUBTITLE_CONTRACT.md](SUBTITLE_CONTRACT.md)。

目录批量字幕错误码（v0028）：`INVALID_DIRECTORY`、`ENUMERATE_LIMIT`、`BATCH_EMPTY`、
`BATCH_TOO_LARGE`、`SOURCE_SRT_MISSING`、`OUTPUT_NOT_WRITABLE`；接口与字段见
[SUBTITLE_BATCH_CONTRACT.md](SUBTITLE_BATCH_CONTRACT.md)。

## 4. 认证

### POST /api/auth/login（免登录）
请求：`{"username": "admin", "password": "...", "otp": "123456"（可选）}`
响应 200：
```json
{"ok": true, "user": {"name": "admin", "is_admin": true},
 "csrf_token": "random", "expires_at": "2026-11-01T23:00:00+08:00"}
```
失败 401 `AUTH_FAILED`。

### GET /api/auth/me
```json
{"ok": true, "user": {"name": "admin", "is_admin": true}, "csrf_token": "..."}
```
未登录 401。

### POST /api/auth/logout
成功后清 Cookie。

## 5. 元信息与文件浏览

### GET /api/meta
```json
{
 "app_version": "1.1.0-0030",
 "dsm_version": "7.2.2-72806" | "dev",
 "python_version": "3.8.16",
 "creation_time_supported": true,
 "statx_supported": true,
 "platform": "x86_64",
 "dev_mode": false,
 "defaults": { ...扫描默认值，见 §6 },
 "limits": {"max_files": 500000, "max_roots": 20, "max_excludes": 200}
}
```

### GET /api/shares
```json
{"ok": true, "shares": [
  {"name": "photo", "path": "/volume1/photo", "volume": "/volume1",
   "filesystem": "btrfs", "is_usb": false, "is_encrypted": false, "is_writable": true}
]}
```
必须过滤系统共享/隐藏共享；路径必须真实存在且位于 `/volume*` 下（开发模式例外）。

### GET /api/browse?path=/volume1/photo&include_files=false
- `path` 可省略：返回 `{"ok":true,"path":"","parent":null,"breadcrumbs":[],"shares":[...]}`。
- 目录模式只返回目录；`include_files=true` 时也返回普通文件（用于“指定保留路径”选择）。
- 响应：
```json
{"ok": true, "path": "/volume1/photo", "parent": "/volume1",
 "breadcrumbs": [{"name":"photo","path":"/volume1/photo"}],
 "entries": [
   {"name":"2024", "path":"/volume1/photo/2024", "type":"dir", "size":0,
    "created_at": null, "modified_at": "2024-01-01T10:00:00+08:00",
    "hidden": false, "is_symlink": false,
    "excluded": false, "exclude_reason": null}
 ]}
```
系统目录、符号链接、无权限目录必须标记 `excluded`/原因，不能进入。

## 6. 设置

### GET /api/settings
```json
{"ok": true, "settings": { ... }, "defaults": { ... }}
```

### PUT /api/settings
请求：`{"settings": { ... }}`（只接受下列键，未知键报 INVALID_ARGUMENT）
响应：`{"ok": true, "settings": { ... }}`

设置键与默认值：
| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| hash_algo | "sha256"\|"md5" | "sha256" | 全量哈希算法 |
| keep_mode | "latest"\|"earliest"\|"pinned" | "latest" | 保留策略 |
| pinned_path | string | "" | pinned 模式的路径或目录前缀 |
| skip_empty | bool | true | 跳过 0 字节 |
| skip_hidden | bool | true | 跳过隐藏/系统文件 |
| skip_symlinks | bool | true | 跳过符号链接（必须始终为 true，后端强制） |
| include_extensions | string[] | [] | 空=全部；形如 `[".jpg",".png"]` |
| exclude_extensions | string[] | [] | 形如 `[".tmp",".part"]` |
| ignore_case_ext | bool | true | 后缀比较忽略大小写 |
| min_size_bytes | int | 0 | 0=不限 |
| max_size_bytes | int | 0 | 0=不限 |
| quick_sample_kb | int | 64 | 大文件快速指纹采样（头/尾各 N KB） |
| concurrency | int | 2 | 哈希并发 1..8 |
| hash_rate_mbps | int | 0 | 全局限速 MB/s，0=不限 |
| recycle_default | bool | true | UI 默认勾选“移动到回收站” |
| create_recycle_dir | bool | false | 共享文件夹无 #recycle 时是否创建（默认否） |
| max_files | int | 500000 | 超过即停止并报错，防止拖垮 NAS |
| allow_ctime_fallback | bool | false | 无 btime 时是否允许用 ctime 作为“创建时间”的降级值（默认否；开启会标记风险） |

约束：`min_size_bytes <= max_size_bytes`（max=0 除外）；`quick_sample_kb` 4..1024；`concurrency` 1..8；
`hash_rate_mbps` 0..2000；扩展名必须以 `.` 开头且长度 <= 16。

## 7. 扫描

### POST /api/scans
请求：
```json
{
 "roots": ["/volume1/photo/2024", "/volume1/backup"],
 "excludes": ["/volume1/photo/2024/private"],
 "options": { ...与 settings 相同的可覆盖项... }
}
```
- 同一时间只允许一个扫描运行（`SCAN_BUSY`）。
- 后端必须重新校验 roots/excludes：必须是已登记共享文件夹下的真实目录，不能是系统/隐藏目录；
  exclude 必须在某个 root 内。
- 返回 202：`{"ok": true, "scan_id": "20261101-201600-ab12cd"}`

### GET /api/scans
```json
{"ok": true, "scans": [{"scan_id":"...","status":"completed","started_at":"...","finished_at":"...",
 "stats":{...},"config":{"roots":[...],"keep_mode":"latest"}}]}
```

### GET /api/scans/{scan_id}
```json
{
 "ok": true,
 "scan": {
  "scan_id": "...",
  "status": "queued|running|paused|stopping|stopped|completed|failed",
  "stage": "enumerate|size|sample|hash|finalize|done",
  "progress": 0.42,
  "message": "正在计算哈希 120/500",
  "started_at": "...", "finished_at": null, "elapsed_sec": 12.3,
  "current_path": "/volume1/photo/...",
  "stats": {
    "files_seen": 100, "dirs_scanned": 10, "files_scanned": 90, "candidate_files": 30,
    "groups": 0, "duplicate_groups": 0, "duplicate_files": 0, "reclaimable_bytes": 0,
    "bytes_hashed": 0, "bytes_per_sec": 0, "errors": 0,
    "skipped": {"symlink":0,"hidden":0,"empty":0,"extension":0,"size":0,"hardlink":0,
                "excluded":0,"permission":0,"other":0}
  },
  "creation_time_unsafe": false,
  "needs_manual_keep_groups": 0,
  "options": { ... },
  "error": null
 }
}
```

### POST /api/scans/{scan_id}/pause | /resume | /stop
返回 `{"ok":true,"scan":{...}}`；状态必须合法，否则 `INVALID_ARGUMENT`。

### GET /api/scans/{scan_id}/groups?offset=0&limit=100&q=&sort=wasted_desc
```json
{"ok": true, "total": 12, "offset": 0, "limit": 100, "groups": [{
  "group_id": "g-000001", "hash": "ab12...", "algo": "sha256", "size": 1024,
  "count": 3, "wasted_bytes": 2048, "keep_file_id": "f-...",
  "keep_reason": "latest_created|earliest_created|pinned_path|manual|tie",
  "needs_manual_keep": false, "creation_time_suspect": false,
  "time_fallback": false, "keep_time_source": "btime"
  "files": [{
    "file_id":"f-000001","path":"/volume1/photo/a.jpg","name":"a.jpg","size":1024,
    "hash":"ab12...","quick_hash":"...",
    "created_at":"2026-11-01T20:00:00+08:00","created_ts":1762084800,"created_ns":0,
    "created_source":"statx|birthtime|ctime|unknown",
    "modified_at":"...","mtime_ns":1762084800123456789,
    "inode":123,"dev":45,"is_keep":true,"default_selected":false
  }]
}]}
```
- 默认排序：wasted_bytes 降序；支持 `size_desc`, `created_desc`, `wasted_desc`。
- `default_selected`：安全组内非保留项为 true；`needs_manual_keep=true` 的组全部 false。
- `created_at` 为 NAS 本地时区 ISO8601；同时给 epoch 供前端排序。

### GET /api/scans/{scan_id}/stats
完整统计（摘要 + 按后缀/目录 TopN），供结果页头部使用；失败不阻塞。

### GET /api/scans/{scan_id}/csv
`text/csv; charset=utf-8`，带 UTF-8 BOM；列：
`组号,匹配类型,状态,文件路径,大小(字节),SHA256/MD5,创建时间,修改时间,保留,默认勾选,时间来源,相似度分值,时间回退,保留时间来源`

- 匹配类型：`哈希一致` / `内容相似`（相似模式见 [SIMILARITY_CONTRACT.md](SIMILARITY_CONTRACT.md)）；相似度分值 0–100，精准组为 100。
- 时间回退：该组是否因缺少创建时间而降级按修改时间保留（是/否）；保留时间来源：btime/mtime（仅诊断用途）。
Content-Disposition: attachment; filename="duplicate-scan-<id>.csv"

### POST /api/scans/{scan_id}/delete-plan
请求（二选一）：
```json
{"scope": "all_duplicates", "exclude_file_ids": [], "recycle": true}
```
或
```json
{"file_ids": ["f-1","f-2"], "recycle": true}
```
- 服务端必须重新校验：文件仍存在、仍非保留项、未被修改、与保留文件哈希一致、在允许路径内。
  候选总量 ≤512 MB 时 plan 阶段立即重哈希；超过预算时哈希复核推迟到删除任务中逐文件执行，
  但删除前始终会重新哈希候选和保留文件。
- 任何无法校验的组/文件进入 `skipped`，绝不静默删除。
- **v1 不支持 `include_manual_groups`**：`needs_manual_keep=true` 的组一律跳过，并在 `skipped` 中说明
  `MANUAL_KEEP_REQUIRED`；`include_manual_groups=true` 返回 `INVALID_ARGUMENT`。
  原因是创建时间不可靠时，用户认为的保留项可能与服务端 `keep_file_id` 不同，允许删除有误删风险。
- `plan.count` / `plan.bytes` 只统计最终进入计划且通过校验的文件；`skipped` 不计入。
- 返回 200：
```json
{"ok": true, "plan": {
 "plan_id":"plan-...", "expires_at":"...", "recycle": true,
 "count": 10, "bytes": 12345, "files": [{"file_id","path","size","group_id"}],
 "skipped": [{"path":"...","reason":"FILE_CHANGED","message":"..."}],
 "groups_affected": 5, "recycle_unavailable": [{"share":"photo","path":"/volume1/photo"}]
}}
```
- plan 只对创建它的会话有效，5 分钟过期，一次性；服务端保存完整待删列表和校验快照。
- 响应明细数组有上限（内存保护）：`files` 最多 5000 条（`files_total/files_truncated`），
  `skipped` 最多 2000 条（`skipped_total/skipped_truncated`）；前端用总数展示。
- 若所有候选都因回收站不可用被跳过，接口返回 **409 `RECYCLE_UNAVAILABLE`**，
  `details.recycle_unavailable` 列出共享文件夹；前端提示用户启用回收站或改用永久删除，绝不静默降级。

### POST /api/scans/{scan_id}/delete
请求：
```json
{"plan_id":"plan-...", "confirm":"DELETE", "permanent_phrase":"永久删除"（recycle=false 时必填）}
```
- `confirm` 必须精确为 `"DELETE"`。
- 返回 202：`{"ok":true,"job_id":"del-..."}`
- 实际删除在后台线程执行；必须记录审计日志与每个文件结果。

### GET /api/scans/{scan_id}/delete/{job_id}
```json
{"ok":true,"job":{
 "job_id":"del-...","status":"running|completed|partial|failed|stopped",
 "progress":0.5,"total":10,"done":5,"failed":2,
 "bytes_total":12345,"bytes_done":6000,
 "results":[{"path":"...","status":"recycled|deleted|skipped|failed",
             "reason":"","message":"","recycle_path":null}],
 "started_at":"...","finished_at":null,"log_file":"deletions/....jsonl"
}}
```
- v0021 起逐文件结果持久化在 SQLite `deletion_results` 表（重启后可读）；`log_file` 字段仅为兼容历史命名的标识，不再单独写 `deletions/*.jsonl` 文件。
- 关闭页面后仍可通过该接口查看；重启后历史从磁盘恢复（至少列表可见）。

## 8. 预览与日志

### GET /api/preview?path=/volume1/photo/a.jpg
- 仅允许共享目录内、非系统路径、普通文件；校验 Cookie；支持 Range（206）。
- 图片/文本/PDF/音视频 inline 返回；其他类型 415 `PREVIEW_UNSUPPORTED`。
- 文本最多返回 1MB，返回头 `X-Preview-Truncated: 1`。

### GET /api/logs?limit=50
```json
{"ok":true,"logs":[{"name":"scan-20261101-....log","kind":"scan","size":1234,
 "modified_at":"...","scan_id":"..."}],"audit":[{"ts":"...","action":"scan_start","user":"admin","detail":{}}]}
```

### GET /api/logs/download?name=scan-....log
鉴权后下载；name 必须匹配白名单正则，禁止路径穿越。

## 9. 文件/目录安全规则（后端强制）

- 只允许共享文件夹根及其子目录；禁止 `/`、`/etc`、`/usr`、`/var`、`/root`、`/volumeX` 根、
  `/volumeX/@*`、`/volumeX/#recycle`、`#snapshot`、`@eaDir`、`@tmp`、`@appstore`、`@docker`、
  `@synologydrive`、`.SynoFSFolder` 等。
- 默认跳过符号链接、隐藏文件、系统缩略图/索引文件；不跟随 symlink。
- 默认 `#recycle`、`@eaDir` 等目录永不扫描。
- 同一 inode 的硬链接只保留一个路径参与重复判定，避免“删了不释放空间”。
- 删除前必须对比扫描时记录的 size / mtime_ns / inode / dev / 全量哈希；保留文件必须仍存在。
- 绝不自动删除：必须 plan + 二次确认 + `confirm=DELETE`；永久删除额外要求短语。
- 回收站移动目标：`<共享文件夹根>/#recycle/@duplicate-cleaner/<YYYYMMDD-HHMMSS>/<相对路径>`，
  同名冲突加 `.dup-<n>` 后缀；跨文件系统时禁止 move，改为失败报告。
- 审计日志必须持久化，包含扫描开始/结束、计划、逐文件删除结果。

## 10. 前端页面要求（验收口径）

- 顶部常驻风险提示（不可关闭），文案包含：
  “删除文件存在数据丢失风险，重要数据建议先备份；本工具仅基于哈希判定重复，无法保证业务文件适用性，请核对扫描结果再操作。”
- 登录页 → 主页面（扫描范围/选项/进度/结果/历史日志）。
- 文件夹选择器：共享文件夹树，支持多选根目录、添加排除目录；只展示后端返回的目录。
- 扫描选项：哈希算法、保留模式（最晚/最早/指定路径）、大小区间、后缀包含/排除、忽略大小写、跳过空文件/隐藏文件、
  并发、限速；暂停/继续/停止；扫描进度真实轮询。
- 结果：按组展示，保留项高亮；每行路径、大小、完整/可复制哈希、创建时间（附来源）、修改时间；预览；
  组内/全局勾选；全选待删除副本；导出 CSV。
- 删除：第一步清单确认弹窗（显示数量、可释放空间、回收站/永久删除、跳过项），第二步最终确认弹窗
  （永久删除需输入“永久删除”）；按钮在请求中禁用防重复。
- 历史/日志：扫描历史、审计事件、下载日志；操作日志持久化提示。
- 无第三方 CDN、无构建步骤；ES2019 语法，兼容 DSM 内置 Chromium/Firefox。
- 所有用户可见文案为简体中文；时间显示 NAS 本地时间（后端给带时区 ISO）。

## 11. 测试与验收

- 后端 unittest 覆盖：statx 时间读取降级、哈希分片/限速、重复分组与三种保留策略、
  路径黑名单、删除前重校验、回收站路径生成、CSV、API 鉴权。
- 前端可用静态检查 + 本地 dev 后端联调；QA 用 dev 模式在临时目录生成重复文件跑完整 API 流程。
- 打包产物 `dist/DuplicateCleaner-1.1.0-0030.spk` 必须能通过 `tar -tf` 检查，包含 INFO/package.tgz/scripts/conf/ui。
