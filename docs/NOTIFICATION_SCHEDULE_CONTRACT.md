# 通知系统与计划扫描契约（v0046）

> 版本：`1.1.0-0046` · SQLite schema 仍 **v8**（调度/通知去重状态是 `<var>/` 下的 JSON）
> 关联：[CONTRACT.md](CONTRACT.md) §6/§8、[ARCHITECTURE.md](ARCHITECTURE.md) §16、
> [REQUIREMENTS_V2_ASSESSMENT.md](REQUIREMENTS_V2_ASSESSMENT.md) §2.1/§2.2、§4.1/§4.2

## 0. 一句话

- **计划扫描**：按 `daily HH:MM` / `weekly mon,wed,fri HH:MM` / `interval MIN` 规则在后台定时扫描；
  可设 1 分钟负载阈值，负载超标或已有扫描在跑时跳过本次；扫描范围可固定，留空则复用上次完成扫描。
- **通知系统**：扫描完成/失败、字幕批次结束、批量失败超阈值、单次删除释放空间达阈值时，
  推送到 **DSM 系统通知 / Webhook / Telegram**（可订阅事件与渠道）。

## 1. 计划扫描

### 1.1 设置

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| scheduled_scan_enabled | bool | false | 总开关 |
| scheduled_scan_rules | string[] | [] | 每行一条，最多 10 条；格式见下 |
| scheduled_scan_roots | string[] | [] | 绝对路径；留空 = 复用“上次完成的扫描”的 roots |
| scheduled_scan_max_load | float | 0 | 1 分钟 loadavg 阈值；0 = 不检查；超阈值跳过本次 |

规则格式（大小写不敏感）：
- `daily 03:30` —— 每天 03:30（本地时区）；
- `weekly mon,wed,fri 04:00` —— 每周一/三/五 04:00（mon..sun 或 1..7）；
- `interval 360` —— 每 360 分钟（5..10080）。

非法规则在保存设置时返回 400 `INVALID_ARGUMENT`（details 带第几条）。

### 1.2 调度语义

- 后台线程每 `SCHEDULE_TICK_SEC=20` 秒检查一次；每条规则记录 `last_run`（持久化
  `<var>/scheduler_state.json`），重启不会重复触发同一时间点。
- 到点时：
  1. 已有扫描在跑 → 本次跳过（`scheduled_scan_skipped` 审计，reason=busy），该规则本轮不再补跑；
  2. `max_load>0` 且 1 分钟负载 > 阈值 → 跳过（reason=load）；
  3. 没有可用 roots → 跳过（reason=no_roots）；
  4. 启动扫描（`user="scheduler"`），记录 `last_scan_id`，审计 `scheduled_scan_start`。
- 同一规则两次触发间隔至少 `SCHEDULE_MIN_INTERVAL_SEC=300` 秒，避免 interval 抖动叠加。
- 手动“立即执行”`POST /api/scheduler/run-now` 走同样的检查（busy/load/roots）。

### 1.3 接口

- `GET /api/scheduler` → `{enabled, rules:[{rule,kind,last_run,next_run,next_run_epoch}],
  roots, max_load, current_load, last_run, last_scan_id, last_status, last_message}`；
- `POST /api/scheduler/run-now` → 202 `{ok:true, result:{scan_id, roots}}`；
  被跳过时 200 `{ok:false, result:{reason, message}}`（`busy`/`load`/`no_roots`）。

## 2. 通知系统

### 2.1 设置

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| notify_enabled | bool | false | 总开关 |
| notify_channels | string[] | ["dsm"] | `dsm` / `webhook` / `telegram` |
| notify_events | string[] | 全部 | `scan_complete` / `scan_failed` / `subtitle_batch_done` / `failure_burst` / `freed_threshold` |
| notify_webhook_url | string | "" | http(s)；POST JSON |
| notify_telegram_bot_token | secret | "" | 保存后不回显；`clear_telegram_bot_token=true` 清空 |
| notify_telegram_chat_id | string | "" | Telegram chat_id |
| notify_fail_threshold | int | 20 | 批量失败阈值（失败任务数） |
| notify_freed_bytes_threshold | int | 10 GiB | 释放空间阈值（字节）；0 = 不通知 |
| notify_timeout_sec | int | 5 | 单渠道发送超时（2..30） |

渠道可用性：
- `dsm`：`/usr/syno/bin/synodsmnotify` 存在才可用（非 DSM 环境自动跳过）；
- `webhook`：URL 非空；
- `telegram`：Bot Token + chat_id 都非空。

### 2.2 发送语义

- 所有通知走**后台单线程队列**（`NOTIFY_QUEUE_MAX=200`），不阻塞扫描/删除/字幕线程；
  队列满丢弃并审计 `notify_dropped`。
- 每个渠道发送后审计 `notify_sent` / `notify_failed`（只记 event/channel/level/error，
  **不含 token、不含 Webhook 完整 URL**）。
- Webhook payload：
  ```json
  {"event":"scan_complete","title":"…","message":"…","level":"info",
   "timestamp":"2026-11-08T21:30:00+08:00","app":"DuplicateCleaner",
   "version":"1.1.0-0046","detail":{…}}
  ```
- Telegram：`POST https://api.telegram.org/bot<token>/sendMessage`
  `{chat_id, text, disable_web_page_preview:true}`。
- DSM：`synodsmnotify -l info|warn|error -c <title> -t <message> @administrators`。

### 2.3 触发点

| 事件 | 触发位置 | 说明 |
|---|---|---|
| scan_complete / scan_failed | `ScanManager._audit_finish` | 完成后统计分组/重复文件/可释放空间；失败带错误信息 |
| subtitle_batch_done / failure_burst | `SchedulerService` watcher（每 60s） | 轮询批次组总览，按 group_id 去重（`<var>/notify_state.json`）；失败数 ≥ 阈值时发 failure_burst |
| freed_threshold | `DeletionManager._run_job` 结束 | `bytes_done ≥ notify_freed_bytes_threshold` |
| failure_burst（删除） | 同上 | 失败数 ≥ `notify_fail_threshold` |

### 2.4 接口

- `GET /api/notifications` → `{enabled, channels:[{channel,label,available,configured,detail}],
  subscribed_channels, events, event_labels, fail_threshold, freed_bytes_threshold, timeout_sec}`；
- `POST /api/notifications/test` `{channel}` → `{result:{channel,ok,detail}}`（同步发送一条测试）。

## 3. 前端

设置页新增「自动化 · 计划扫描与通知」分组：
- 计划扫描开关、规则文本域（每行一条）、目录文本域、负载阈值，下方显示
  “下次执行 / 上次执行 / 当前负载”（`GET /api/scheduler`）；
- 通知开关、渠道/事件复选框、Webhook 地址、Telegram Token（password，永不回填）、chat_id、
  失败阈值、释放空间阈值（GB）、发送超时，以及「测试 DSM / Webhook / Telegram」按钮
  （点击直接调用测试接口并显示结果/错误）；
- 保存时前端做基础格式校验（规则正则、绝对路径、http(s) URL、数值范围），后端仍为权威校验。

## 4. 安全与限制

- 通知内容只含任务统计/错误摘要，不含文件内容；Telegram token 不写日志/审计/响应。
- 计划扫描以管理员会话之外的后台身份运行（`user="scheduler"`），仍走同一套扫描权限与路径校验。
- 低负载判断使用 `os.getloadavg()`（Windows 不可用时不检查）；DSM 上为 1 分钟负载。
- 实时监控/计划任务依赖轮询（标准库无 inotify/cron 集成）；套件停机期间错过的时间点，
  下次启动后按规则的下一次时间执行（不会补跑全部历史时间点）。
- DSM 通知需要 `synodsmnotify`（DSM 7 自带，setuid root）；套件用户无权限时发送失败并记录审计。

## 5. 验收

- unittest：`tests/test_v0046_notify.py`（默认关闭、订阅过滤、Webhook payload、失败审计、
  Telegram URL、DSM 不可用跳过、测试接口、四类事件触发）与 `tests/test_v0046_scheduler.py`
  （规则解析/下次时间、设置校验、立即执行、root 复用、忙碌/负载跳过、tick 去重、字幕 watcher 去重）。
- e2e：`NOTIFY-00..03`（真实本地 Webhook 接收器收到 scan_complete 与测试消息）、
  `SCHED-00..04`（设置、状态、立即执行、非法规则、root 复用）。
- 静态：`BE-NOTIFY`、`BE-SCHEDULER`、`FE-AUTOMATION-UI` + 路由/端点覆盖。
- 真机：DSM 通知实际弹出（`@administrators`）、负载阈值在 4 核 NAS 上的合理取值、
  Telegram/Webhook 出网与超时行为、计划扫描与字幕任务并发时的资源占用。
