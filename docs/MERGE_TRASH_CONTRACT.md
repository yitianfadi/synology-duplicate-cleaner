# 硬链接合并与套件独立回收站契约（v0045）

> 版本：`1.1.0-0045` · SQLite schema 仍 **v8**（清单是 `<var>/` 下的 JSON，不进数据库）
> 关联：[CONTRACT.md](CONTRACT.md) §6/§7、[ARCHITECTURE.md](ARCHITECTURE.md) §15、
> [REQUIREMENTS_V2_ASSESSMENT.md](REQUIREMENTS_V2_ASSESSMENT.md) §2.5

## 0. 一句话

- **硬链接合并**：把精准重复组的非保留项替换成指向保留项的硬链接——路径都还在、内容共享、
  空间真正释放。默认关闭（不可逆），逐组确认 + `confirm="MERGE"`；提供“恢复为独立副本”。
- **套件独立回收站**：删除时可选择把文件移动到 `<共享文件夹根>/#duplicate-cleaner-trash/<时间戳>/<相对路径>`
  （同卷 rename，不复制数据），按删除批次写 manifest，任务中心可**整组恢复**或**永久清理**。

## 1. 硬链接合并

### 1.1 语义与限制（必须先读）

- 只处理**精准重复组**（`match_type=exact`，内容哈希一致）；相似/视频组内容不同，禁止合并。
- 同一文件系统才能硬链接；跨卷（volume1↔volume2）在计划阶段就跳过（`CROSS_DEVICE`）。
- 合并后多个路径共享同一 inode：**任一路径的内容修改会影响所有路径**；权限/属主自然一致
  （同一 inode）；回收站无法恢复“原来的第二个 inode”，但可用“恢复为独立副本”撤销共享。
- 已在同一 inode 的路径直接跳过（`ALREADY_LINKED`）；受删除白名单保护的文件不可合并（`PROTECTED`）。
- 需要人工确认保留项的组（`needs_manual_keep`）不可合并。

### 1.2 开关与计划

- 设置 `merge_enabled`（默认 **false**）。未开启时 `POST /api/scans/{id}/merge-plan` 返回 400。
- `POST /api/scans/{id}/merge-plan` 请求二选一：
  - `{"file_ids": ["f-...", ...]}`（结果页逐组按钮使用）；
  - `{"scope": "all_exact"}`（全部精准组的非保留项）。
  - 可选 `{"group_ids": ["g-..."]}` 限定组。
- 计划阶段校验：扫描记录仍一致（size/mtime_ns/inode/dev）、受保护/保留项跳过、
  同一文件系统、非已链接；返回 `count`/`bytes`/`groups_affected`/`skipped[]`/`warning`，
  计划 5 分钟过期、一次性、绑定会话。
- `POST /api/scans/{id}/merge` 请求 `{"plan_id": "...", "confirm": "MERGE"}` → 202 `{job_id}`。
  错误码：`MERGE_DISABLED` 用 400 `INVALID_ARGUMENT`；`PLAN_INVALID` / `PLAN_EXPIRED`。

### 1.3 执行

- 每个候选再跑一次 `revalidate(..., hash_check=True)`（含与保留项的全量哈希一致校验），然后
  `link(master, tmp)` + `rename(tmp, candidate)` 原子替换；失败进 `failed`，不中断整体。
- 结果：`merged`/`skipped`/`failed`，`freed_bytes`；任务状态 `completed`（全部成功）/
  `partial`（有跳过或失败）/`failed`。
- manifest：`<var>/merges/<job_id>.json`，条目 `{path, master, size, group_id, merged_at, restored_at}`。
- 审计：`merge_plan` / `merge_start` / `merge_end` / `merge_restore`。

### 1.4 查询与恢复

- `GET /api/merges?scan_id=&limit=` → 合并任务列表（内存任务 + manifest 历史）。
- `GET /api/scans/{id}/merge/{job_id}` → 任务进度（运行中内存快照）。
- `GET /api/merges/{job_id}` → 合并明细（每条路径当前 `state=linked|independent|missing`）。
- `POST /api/merges/{job_id}/restore` 请求 `{"confirm": true, "paths"?: [...]}`：
  - 校验路径仍与源共享 inode（否则 `NOT_LINKED` 跳过）、文件存在（`MISSING`）；
  - 优先 `FICLONE`（Btrfs 秒级复制），不可用时退普通复制；保持 mtime；写回 manifest；
  - 返回 `restored` / `reflink` / 逐条结果；审计 `merge_restore`。

## 2. 套件独立回收站

### 2.1 开关与路径

- 设置 `delete_recycle_mode`：`share`（默认，`<share>/#recycle/@duplicate-cleaner/...`）/
  `package`（`<share>/#duplicate-cleaner-trash/<YYYYMMDD-HHMMSS>/<相对路径>`）。
- 两种模式都是**同卷 rename**（秒级、不复制）；`#` 前缀目录会被扫描器当作系统目录跳过。
- 计划时快照模式（`plan.recycle_mode`），执行期间不跟随设置变化；计划响应带 `recycle_mode`。
- package 模式不要求共享文件夹启用 `#recycle`，只要求共享根可写、同文件系统。

### 2.2 manifest 与恢复

- 删除任务结束后写 `<var>/trash/<job_id>.json`：
  `{trash_id, scan_id, user, recycle_mode, created_at, purged, entries:[{original, trash_path, size,
  group_id, keeper_path, file_id, match_type, restored_at, purged_at}]}`。
- `GET /api/trash?limit=` → 批次列表（`entries_total/restored/remaining`、`bytes_remaining`、`purged`）
  + 当前 `recycle_mode` 与模式标签。
- `GET /api/trash/{trash_id}` → 明细（每条 `trash_exists` / `original_exists` / 恢复状态）。
- `POST /api/trash/{trash_id}/restore` 请求 `{"confirm": true, "paths"?: [...]}`：
  - 原位置已存在同名文件 → `ORIGINAL_EXISTS` 跳过（永不覆盖）；
  - 回收站内文件缺失 → `TRASH_MISSING`；跨设备复制回退（同卷一般不会）；
  - 返回 `restored` + 逐条结果；审计 `trash_restore`。
- `POST /api/trash/{trash_id}/purge` 请求 `{"confirm": true, "permanent_phrase": "永久删除"}`：
  - 只删除 manifest 中且位于 `<share>/#duplicate-cleaner-trash/` 下的文件；清理空目录；
  - 标记 `purged=true`，之后不可恢复；审计 `trash_purge`。

## 3. 接口 / 能力汇总

| 接口 | 说明 |
|---|---|
| `POST /api/scans/{id}/merge-plan` | 生成合并计划 |
| `POST /api/scans/{id}/merge` | 执行合并（`confirm=MERGE`） |
| `GET /api/scans/{id}/merge/{job_id}` | 合并任务进度 |
| `GET /api/merges` / `GET /api/merges/{job_id}` | 合并历史 / 明细 |
| `POST /api/merges/{job_id}/restore` | 恢复为独立副本 |
| `GET /api/trash` / `GET /api/trash/{id}` | 套件回收站列表 / 明细 |
| `POST /api/trash/{id}/restore` | 整组/部分恢复 |
| `POST /api/trash/{id}/purge` | 永久清理（需短语） |

`GET /api/meta` 新增 `merge`（`enabled`/`enabled_default`/`plan_ttl_seconds`）与
`recycle`（`mode`/`modes`/`labels`/`trash_dir_name`）。

## 4. 安全边界

- 两操作都必须管理员会话 + CSRF；合并计划一次性 + 5 分钟过期；合并/清理都要二次确认。
- 恢复只使用 manifest 记录的路径，重新做共享目录/符号链接校验；绝不覆盖已存在的原文件。
- 合并只对“计划时扫描记录一致 + 执行时再校验（含全量哈希）”的文件执行；任何不一致跳过。
- 回收站清理只删 `<share>/#duplicate-cleaner-trash/` 内、manifest 记录的文件。

## 5. 验收

- unittest：`tests/test_v0045_merge.py`（默认关闭、计数/跳过、已链接、合并后 inode 相同、
  confirm 校验、恢复独立副本、列表）与 `tests/test_v0045_trash.py`（package 模式移动到独立回收站、
  manifest、整组恢复、冲突不覆盖、部分恢复、清理短语、API）。
- e2e：`MERGE-01..04`、`TRASH-00..03`。
- 静态：`BE-MERGE`、`BE-TRASH`、`FE-MERGE-UI`、`FE-TRASH-UI` + 路由/端点覆盖。
- 真机（Btrfs）：合并后 `df` 释放空间与 `stat` nlink；跨卷跳过；`FICLONE` 恢复是否秒级；
  套件回收站在 File Station 的可见性与整组恢复。
