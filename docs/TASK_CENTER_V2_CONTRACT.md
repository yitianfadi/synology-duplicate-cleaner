# 任务中心 / 权限体检 / 日志增强契约（v0049）

> 版本：`1.1.0-0049` · SQLite schema 仍 **v8**
> 关联：[CONTRACT.md](CONTRACT.md) §6/§8、[ARCHITECTURE.md](ARCHITECTURE.md) §19、
> [REQUIREMENTS_V2_ASSESSMENT.md](REQUIREMENTS_V2_ASSESSMENT.md) §3.1、§4

## 0. 一句话

- **任务检索筛选 + 批量操作**：字幕任务支持按状态 / 路径关键字 / 目录 / 后缀 / 失败分类筛选，
  支持批量取消 / 重试 / 置顶；
- **权限与路径向导**：一键**只读体检**配置涉及的目录与共享文件夹（存在/可读/可写），汇总
  扫描/字幕任务里出现过的权限异常，给出 DSM 操作指引——**不自动提权**（评估结论：非 root 无法改 ACL）；
- **日志增强**：按任务 ID / 关键字检索日志与审计、可读文本导出、任务行可视化错误信息。

## 1. 任务检索与批量操作

### 1.1 检索参数（`GET /api/subtitles`）

| 参数 | 说明 |
|---|---|
| `q` | 路径 / 任务 ID / 批次组 / 消息 关键字（不区分大小写） |
| `status` | `all/active/queued/running/translating/paused/done/failed/cancelled` |
| `suffix` | 文件后缀（`.mkv` 或 `mkv`） |
| `directory` | 目录前缀（按 path_key 归一化比较） |
| `category` | 失败分类（v0048 的 9 类；仅对 failed 生效） |
| `group_id` / `batch_id` / `offset` / `limit` | 原有分页/批次过滤 |

有筛选条件时前端直接调用该接口（limit 200），否则沿用队列视图（进行中/即将执行/已暂停/最近完成）。
筛选结果支持“全选当前列表”与“已选 N 个”计数。

### 1.2 批量操作（`POST /api/subtitles/batch-actions`）

请求 `{job_ids: [...], action: "cancel"|"pause"|"resume"|"retry"|"priority"}`：

- 最多 200 个、自动去重；单任务失败不影响其它（逐条结果）；
- `retry` = 对 failed 任务强制重试（等价 failures/retry force）；
- `priority` = 置顶（1000）；
- 返回 `{action, requested, succeeded, failed, results:[{job_id, ok, message}]}`；
- 审计 `subtitle_bulk_action`。

## 2. 权限与路径体检

### 2.1 接口

- `GET /api/permissions/report`：
  - 检查范围：设置里的计划扫描目录、字幕监控目录、删除白名单目录、字幕目录优先级、
    最近完成扫描的 roots、所有共享文件夹根；
  - 每项返回 `{source, path, share, exists, is_dir, readable, writable, status, code, guidance}`，
    `status` 为 `ok/warn/error`；
  - `summary {total, ok, warn, error, issues}`；
  - `issues`：最近扫描日志中的权限关键字行（最多 20 条）+ 字幕失败分类 permission 的任务；
  - `guidance`：DSM 控制面板操作建议；`note` 明确“只读检测、不提供一键提权”。
- `POST /api/permissions/check` `{paths:[...]}`：手动检查最多 50 条路径（同一套规则）。

### 2.2 限制（与评估一致）

- 套件以非 root 用户运行，**无法自动修改共享文件夹 ACL/属主**；“一键配置”改为“一键体检 + 指引”。
- `os.access` 反映套件用户的实际权限；DSM 上通过 `#recycle`/写回失败也能在任务错误中看到。

## 3. 日志增强

- `GET /api/logs/search?q=&job_id=&limit=`：
  - 遍历日志文件（最多 500 个，单文件读 512KB），按文件名/scan_id/job_id 与内容匹配；
  - 返回 `{logs:[{name, kind, size, modified_at, match_count, matches[≤30], truncated, content_preview}],
    audit:[…], scanned_logs}`；
  - 审计条目同时按 `q`/`job_id` 匹配（JSON 全文）。
- `GET /api/logs/export?q=&job_id=`：可读文本（含过滤条件、日志匹配行、审计 JSON），
  `text/plain; charset=utf-8` 下载；
- 前端「操作日志」页新增检索框（关键字/任务 ID）、搜索/清空/导出按钮、匹配结果 `<pre>` 展示；
- 任务行/详情已有错误信息（v0041/0048），失败分类与错误码可见。

## 4. 验收

- unittest：`tests/test_v0049_taskcenter.py`（检索 5 例、批量 5 例、权限 3 例、日志 API 4 例）。
- e2e：`SUBJ-01/02`（筛选/批量校验）、`PERM-01`（体检报告）、`LOGS-01/02`（检索/导出）。
- 静态：`BE-TASKCENTER-V2`、`FE-TASKCENTER-V2` + 路由/端点覆盖。
- 真机：DSM 共享文件夹权限体检结果与手工 `ls -l` 一致性；日志检索在大量日志下的耗时；
  批量操作 200 个任务的响应与审计。
