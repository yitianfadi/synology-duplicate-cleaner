# 字幕 ASR 模块优化契约（v0048）

> 版本：`1.1.0-0048` · SQLite schema 仍 **v8**（监控状态为 `<var>/subtitle_watch_state.json`）
> 关联：[SUBTITLE_BATCH_CONTRACT.md](SUBTITLE_BATCH_CONTRACT.md)、[CONTRACT.md](CONTRACT.md) §6/§8、
> [ARCHITECTURE.md](ARCHITECTURE.md) §18、[REQUIREMENTS_V2_ASSESSMENT.md](REQUIREMENTS_V2_ASSESSMENT.md) §5

## 0. 一句话

字幕任务增加**失败分类 + 可恢复错误自动重试 + 失败清单导出/批量重试**、
**负载阈值自动暂停队列 + 队列优先级（手动置顶/目录优先级）+ ETA 校准**、
**音轨序号/语言选择**，以及**视频目录监控自动入队**（新增视频自动生成字幕/翻译）。

## 1. 失败分类与自动重试

### 1.1 分类

| category | 含义 | 可自动重试 |
|---|---|---|
| audio_extract | 音频提取失败/无音轨/音频过大 | 否 |
| no_speech | 无语音、无可识别字幕内容 | 否（标记“已跳过”） |
| timeout | 云端请求/抽音频超时 | **是** |
| unsupported_stream | 码流/音轨/容器不支持、音轨不存在 | 否 |
| permission | 权限异常 | 否 |
| network | 网络异常 | **是** |
| provider | 云端服务返回错误 | **是** |
| internal | 内部错误/FFmpeg 不可用 | **是** |
| unknown | 其它 | 否 |

错误码映射见 `config.SUBTITLE_FAILURE_CODE_CATEGORY`；未知码再按消息关键字归类。

### 1.2 自动重试

- 设置：`subtitle_auto_retry_enabled`（默认 true）、`subtitle_auto_retry_max`（0–3，默认 1）；
- 失败时若分类可重试且 `attempt < max`：任务回到 `queued`（保留分片缓存）、消息
  “自动重试（N/M）”、审计 `subtitle_retry_auto`；否则标记失败；
- `no_speech` 在 `subtitle_no_speech_skip=true`（默认）时 `skipped=true`，消息注明
  “无语音，已跳过，不参与自动重试”，避免无效排队；
- 自动重试不消耗用户操作，失败清单里显示 `attempt` 与可重试标记。

### 1.3 失败清单 / 导出 / 批量重试

- `GET /api/subtitles/failures?limit=&group_id=&category=&include_skipped=` →
  `{total, items[], categories{}, category_labels, retryable_categories}`；
- `GET /api/subtitles/failures/export?limit=` → CSV（UTF-8 BOM，字段：任务ID/路径/分类/错误码/
  信息/重试次数/可重试/已跳过/时间/批次组）；
- `POST /api/subtitles/failures/retry` `{job_ids?[], group_id?, category?, force?}`：
  - 默认只重试可恢复分类；`force=true` 时连 no_speech/权限类也重试；
  - 返回 `{requeued, skipped:[{job_id,reason,category}], selected}`；
- 任务中心「失败任务」面板支持分类过滤、包含无语音、单条/批量重试与强制重试。

## 2. 资源管控 / 优先级 / ETA

- `subtitle_max_load`（默认 0 = 不限制）：worker 取任务前检查 1 分钟 loadavg，
  超过阈值时**保留队列不取新任务**（每 2 秒复查）；`counts()`/`queue_snapshot()` 返回
  `load_guard {enabled, threshold, load1, blocked, reason}`，任务中心展示。
- 优先级：任务 `priority`（越大越先）；队列按“优先级降序 + 创建时间升序”插入。
  - `POST /api/subtitles/{job_id}/priority` `{top:true}`（置顶 1000）或 `{priority:0..1000}`；
  - `subtitle_dir_priorities`：目录列表，越靠前优先级越高（len-index），提交时自动带优先级；
  - 重启恢复队列时同样按优先级排序。
- ETA 校准：记录最近 20 个非跳过任务的实际执行耗时中位数；有 ≥3 个样本时
  `eta = 执行中任务剩余估算 + 中位耗时 × 排队数 / 并发`，否则回退原墙钟吞吐算法。

## 3. 音轨选择

- 任务字段 `audio_track`（0–31 音频序号或 null）与 `audio_track_language`（如 jpn/chi/eng）；
  提交时可按任务覆盖，默认取设置 `subtitle_audio_track` / `subtitle_audio_track_language`；
- 运行时 `probe_media` 解析音轨列表 → `audio_map="0:a:<ordinal>"`，抽音频（NAS/服务器模式、
  分片预取）统一带 `-map`；序号/语言不存在 → `AUDIO_TRACK_NOT_FOUND`（unsupported_stream）；
- `POST /api/subtitles/audio-tracks` `{path}` → 音轨列表（index/language/title/codec/channels/default）；
  任务详情「查询音轨」按钮调用；前端设置页可配默认音轨。

## 4. 视频目录监控

- 设置：`subtitle_watch_enabled`（默认 false）、`subtitle_watch_dirs[]`、`subtitle_watch_interval_sec`
  （60–86400，默认 300）；
- `SubtitleWatchService` 每 30 秒 tick，到达间隔后扫描目录（递归、跳过隐藏/符号链接），
  按 `video_extensions` 过滤；按 `(size, mtime_ns)` 记录 `<var>/subtitle_watch_state.json`，
  已提交过的文件不重复提交；每轮最多入队 20 个；
- 以 `user="watcher"` 调用批量提交（`source_watch=true`），翻译开关用
  `subtitle_translate_enabled`，音轨用默认设置；`skip_existing` 已有的任务会被标记跳过；
- `GET /api/subtitles/watch`（状态：enabled/dirs/interval/last_scan/result/watched_files）、
  `POST /api/subtitles/watch/scan`（立即扫描，force=true 即使监控关闭也执行一次）；
- 限制：轮询实现（标准库无 inotify）；文件改动（size/mtime 变化）才会重新入队。

## 5. 验收

- unittest：`tests/test_v0048_subtitles_v2.py`（分类/可重试、自动重试与上限、无语音跳过、
  失败清单/导出、批量重试 force、置顶、音轨解析、ETA 校准、目录监控去重/文件变化/未启用、
  设置校验）。
- e2e：`SUBF-01..06`（失败清单/CSV/监控状态与扫描/非法音轨/监控间隔）。
- 静态：`BE-SUBTITLE-V2`、`FE-SUBTITLE-V2` + 路由/端点覆盖。
- 真机：DSM 上负载阈值与并发任务的实际表现、音轨选择对国配/原声的识别、监控目录自动入队与
  翻译流程、失败清单导出在 Excel 中的中文显示。
