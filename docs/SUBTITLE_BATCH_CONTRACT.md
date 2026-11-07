# 目录批量字幕契约（v1.4 binding）

> 在 v1.3 云端字幕（OpenAI 兼容 ASR + LLM）基础上新增 **目录批量生成 / 翻译字幕** 能力。
> 用户已确认三项决策：
> 1. 字幕只写 **`.srt`** 到视频同目录，视频旁不写 `.vtt`；
> 2. 目标文件已存在时 **默认跳过**，不覆盖；
> 3. 只做 **「同目录 + 下载回退」**，不做统一字幕目录（`custom_dir`）。
>
> 本契约是 v0028 的实现依据，与 `docs/SUBTITLE_CONTRACT.md`（v1.3，单文件任务与提供商）互补；冲突处以本文件为准。

## 1. 目标与范围

- 新增独立「字幕」页：选目录 → 递归枚举视频 → 勾选 → 批量提交；与查重（扫描）完全解耦。
- 字幕写入视频同目录：`movie.mp4` → `movie.srt` / `movie.zh.srt` / `movie.bilingual.zh.srt`。
- 写回失败（只读共享、ACL 不足）时**不使整批失败**：文件保留在任务目录供网页下载，任务标记 `warning`。
- 支持两种来源：
  - `source="asr"`：云端 ASR 生成字幕（可顺带翻译）；
  - `source="existing_srt"`：对视频同目录已有的 `movie.srt` **只翻译**，不调 ASR、不上传音频。
- **不在范围**：`custom_dir` 输出模式；`.vtt` 落盘到视频旁；字幕编辑/手工校对；说话人分离；翻译结果重新生成。

## 2. 新增配置

| key | 默认 | 约束 |
|---|---|---|
| subtitle_output_mode | "alongside_video" | `alongside_video` / `download_only`；其它值拒绝 |
| subtitle_skip_existing | true | 布尔；目标 srt 已存在时跳过该视频 |
| subtitle_job_limit | 200 | 20..1000；已完成任务保留上限（LRU 只删任务目录，不删视频旁 srt）；**修改后需重启套件生效**（服务启动时读取） |

以下为常量（不做成设置项，避免配置面膨胀）：

| 常量 | 值 | 说明 |
|---|---|---|
| SUBTITLE_BATCH_MAX_ITEMS | 500 | 单次批量提交路径上限 |
| SUBTITLE_ENUMERATE_MAX_ITEMS | 5000 | 单次目录枚举返回上限；超出截断并置 `truncated=true` |
| SUBTITLE_ALONGSIDE_EXT | ".srt" | 允许写回视频旁的后缀 |

`subtitle_output_mode` 只影响「是否额外复制到视频同目录」；任务目录内始终生成 `original.srt` / `original.vtt`（可下载），与 v1.3 一致。

## 3. 目录枚举 API

`POST /api/subtitles/enumerate`（管理员 + CSRF）

请求：

```json
{"path": "/volume1/video", "recursive": true, "max_items": 5000}
```

- `path` 必须是**共享文件夹内的真实目录**（`shares.validate_root`）；`recursive` 默认 true；`max_items` 取 `1..SUBTITLE_ENUMERATE_MAX_ITEMS`，缺省用上限。
- 遍历规则（与扫描器一致）：
  - 跳过系统目录/文件（`@eaDir`、`#recycle`、`#snapshot`、`@docker`、`Thumbs.db`、`desktop.ini` 等）；
  - 跳过隐藏目录/文件（`.` 开头）；
  - 跳过符号链接；
  - 跳过非普通文件；
  - 只保留后缀在 `settings.video_extensions`（缺省 `config.DEFAULT_VIDEO_EXTENSIONS`）内的文件，大小写不敏感。
- 权限错误计入 `skipped.permission` 并继续，不使整个枚举失败。
- 不做 ffprobe、不读时长（保证大目录枚举速度）。

响应 200：

```json
{
  "ok": true,
  "path": "/volume1/video",
  "recursive": true,
  "video_extensions": [".mp4", ".mkv"],
  "count": 123,
  "total_bytes": 123456789,
  "truncated": false,
  "skipped": {"system": 0, "hidden": 0, "symlink": 0,
               "permission": 0, "other": 0, "extension": 0},
  "items": [
    {
      "path": "/volume1/video/a.mp4",
      "name": "a.mp4",
      "size": 1234567,
      "modified_at": "2026-11-01T10:00:00+08:00",
      "existing_subtitles": ["a.srt", "a.zh.srt"],
      "has_subtitle": true,
      "has_source_srt": true
    }
  ]
}
```

- `existing_subtitles`：与视频同目录、同 stem 的 `.srt` 文件（大小写不敏感），最多列 10 个，用于界面提示。
- `has_source_srt`：存在 `<stem>.srt`，可用于 `source="existing_srt"` 只翻译。
- `items` 按 `path` 排序，结果稳定可复现。

错误：`PATH_NOT_ALLOWED`（不在共享文件夹/系统目录）、`INVALID_PATH`（目录不存在）、`FORBIDDEN`（非管理员）。

## 4. 批量任务 API

### 4.1 提交批次

`POST /api/subtitles/batch`（管理员 + CSRF）

```json
{
  "paths": ["/volume1/video/a.mp4", "/volume1/video/b.mkv"],
  "source": "asr",
  "language": "auto",
  "translate": true,
  "target_lang": "zh",
  "output_mode": "alongside_video",
  "skip_existing": true,
  "confirm_cloud": true
}
```

- `paths`：1..`SUBTITLE_BATCH_MAX_ITEMS`；重复路径去重（保留首次出现顺序）。
- `source`：`asr`（默认）或 `existing_srt`。
- `confirm_cloud` 必须为 `true`（两种来源都要求：ASR 上传音频，只翻译上传字幕文本）。
- 逐条独立校验，**部分无效不整批失败**：无效项进 `rejected`，有效项正常创建 job。
- 同一路径若已有 `queued/running/translating` 的 job，进 `duplicates`（不重复创建）。
- 响应 202：

```json
{
  "ok": true,
  "batch": {
    "batch_id": "bat-20261105-...",
    "status": "queued",
    "total": 10,
    "counts": {"queued": 10, "running": 0, "translating": 0, "paused": 0,
                "done": 0, "failed": 0, "cancelled": 0},
    "rejected": [{"path": "/volume1/video/x.txt",
                   "code": "INVALID_VIDEO", "message": "只支持视频文件生成字幕"}],
    "duplicates": ["/volume1/video/a.mp4"],
    "jobs": [{"job_id": "sub-...", "path": "/volume1/video/a.mp4", "status": "queued"}]
  }
}
```

- 若 `jobs` 为空（全部被拒/重复）→ 400 `BATCH_EMPTY`。

### 4.2 查询批次

`GET /api/subtitles/batches/{batch_id}` → `{"ok":true,"batch":{...}}`，`counts` 由子任务实时聚合。

### 4.3 整批控制

- `POST /api/subtitles/batches/{batch_id}/pause`：运行中任务在分片/批次边界暂停；排队任务转 `paused`。
- `POST /api/subtitles/batches/{batch_id}/resume`：恢复 `paused` 子任务重新入队。
- `POST /api/subtitles/batches/{batch_id}/cancel`：取消所有未完成子任务（已完成保留）。
- `POST /api/subtitles/batches/{batch_id}/retry`：把 `failed` / `cancelled` 子任务重新入队（不重复已完成）。

均返回更新后的 batch 快照；对已结束批次执行控制为幂等空操作。

### 4.4 任务列表过滤

`GET /api/subtitles?limit=&batch_id=` 支持按批次过滤；不传保持 v1.3 行为。

## 5. 单任务扩展（向后兼容）

`POST /api/subtitles` 新增可选字段：

| 字段 | 默认 | 说明 |
|---|---|---|
| source | "asr" | `asr` \| `existing_srt` |
| output_mode | settings.subtitle_output_mode | `alongside_video` \| `download_only` |
| skip_existing | settings.subtitle_skip_existing | 目标 srt 已存在则跳过 |
| batch_id | null | 仅内部使用，由批次创建 |

语义：

- `source="asr"`：行为与 v1.3 一致；若 `skip_existing=true` 且视频旁已存在 `<stem>.srt` 且 `translate=false`，或已存在 `<stem>.srt` 与 `<stem>.<target>.srt`（`translate=true`），则任务直接 `done`，`skipped=true`，`message="已存在字幕，已跳过"`。
- `source="existing_srt"`：
  - `path` 仍传视频路径；后端解析 `<stem>.srt`，**提交时**不存在则拒绝（400 `SOURCE_SRT_MISSING`；批次提交时为 `rejected` 项）；任务执行时源文件被删除/不可读则任务 `failed`（`SOURCE_SRT_MISSING`）。
  - **不启动 FFmpeg、不调用 ASR**；`parse_srt` → LLM 翻译 → 写 `<stem>.<target>.srt` 与 `<stem>.bilingual.<target>.srt`。
  - `translate` 必须为 `true`（默认 true），否则 400 `INVALID_ARGUMENT`。
  - `subtitle_translate_enabled` 必须已开启，否则 `TRANSLATE_DISABLED`。
  - 目标译文文件均已存在且 `skip_existing=true` → `done` + `skipped=true`。

## 6. 输出落盘

- 任务目录（始终）：`<var>/subtitles/<job_id>/original.srt`、`original.vtt`、（翻译时）`<lang>.srt`、`<lang>.vtt`、`bilingual.<lang>.srt`。
- 视频同目录（`output_mode=alongside_video` 时复制）：

| 文件 | 内容 | 写入条件 |
|---|---|---|
| `<stem>.srt` | 原文 | `source=asr` 且不存在 |
| `<stem>.<target>.srt` | 译文 | 有译文且不存在 |
| `<stem>.bilingual.<target>.srt` | 双语 | 有译文且不存在 |

- 只写 `.srt`；`.vtt` 不复制到视频旁。
- 写入方式：先写任务目录，再以**临时文件 + `os.replace`** 原子复制到视频旁；复制失败逐一记入 `output_errors`。
- 视频旁写入前检查目标目录 `os.access(dir, W_OK)`；不可写时跳过全部写回，`warning="OUTPUT_NOT_WRITABLE"`，任务仍为 `done`，可下载。
- 覆盖规则：**从不覆盖**；目标存在即跳过（本版不提供覆盖开关）。
- job 记录 `outputs`：

```json
"outputs": [
  {"name": "a.srt", "path": "/volume1/video/a.srt",
   "size": 1234, "kind": "original"},
  {"name": "a.zh.srt", "path": "/volume1/video/a.zh.srt",
   "size": 1300, "kind": "translation"}
]
```

- `warning` 取值：`null` / `OUTPUT_NOT_WRITABLE` / `OUTPUT_WRITE_PARTIAL`。
- 审计：`subtitle_start`（含 source/output_mode）、`subtitle_output`（写回成功/失败条数）。

## 7. 数据模型

### 7.1 job 新增字段

```json
{
  "source": "asr",
  "batch_id": "bat-...",
  "output_mode": "alongside_video",
  "skipped": false,
  "warning": null,
  "outputs": [{"name": "a.srt", "path": "...", "size": 123, "kind": "original"}],
  "output_errors": []
}
```

旧 job JSON 缺这些字段时按默认值读取（`_load_jobs` 容错），保证升级后可读。

### 7.2 batch 记录

`<var>/subtitles/batches/<batch_id>.json`：

```json
{
  "batch_id": "bat-...",
  "created_epoch": 1762300000.0,
  "updated_epoch": 1762300000.0,
  "user": "admin",
  "options": {"source": "asr", "language": "auto", "translate": true,
               "target_lang": "zh", "output_mode": "alongside_video",
               "skip_existing": true, "recursive": true, "root": "/volume1/video"},
  "job_ids": ["sub-...", "sub-..."],
  "rejected": [],
  "duplicates": []
}
```

### 7.3 SQLite schema v6

```sql
CREATE TABLE IF NOT EXISTS subtitle_batches (
    batch_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_epoch REAL NOT NULL,
    updated_epoch REAL NOT NULL
);
```

- `CURRENT_SCHEMA_VERSION` 5 → 6；v5→v6 迁移只建表（幂等）。
- store 新增：`save_subtitle_batch` / `load_subtitle_batch` / `load_subtitle_batches` / `delete_subtitle_batch`。
- 清理：批次记录随 `subtitle_job_limit` 的 LRU 一起清理（删除 job 时若批次内已无 job，则删批次记录与 json）。

## 8. 队列与容量

- 执行模型不变：**单 worker 串行**，同一时间 1 个字幕任务；批次内按提交顺序执行。
- `subtitle_job_limit` 默认 20 → **200**；LRU 只删除已完成任务的任务目录、DB 记录与 job json；**视频旁 srt 永不删除**。
- 重启恢复：`queued/running` → `paused`（沿用 v1.3）；批次状态由子任务实时聚合，不存独立状态机。
- 任务与批次 ID：`sub-<时间戳>-<随机>` / `bat-<时间戳>-<随机>`，字符集 `[A-Za-z0-9._-]`。

## 9. 前端

- 导航新增 **「字幕」**（顺序：扫描 / 字幕 / 历史记录 / 操作日志 / 设置），`<section id="view-subtitles" data-view="subtitles">`。
- 页面结构：
  1. **选择目录**：复用 `Picker` 目录选择器（`/api/browse`），「包含子目录」开关，「枚举视频」按钮；显示 `N 个视频 / X GB / 已有字幕 M 个`。
  2. **视频列表**：勾选/全选、按「全部 / 缺少字幕 / 已有字幕」过滤；行内显示路径、大小、已有字幕徽标；超过 200 条分页「加载更多」（前端截断展示，不重新请求）。
  3. **选项**：识别语言、生成译文 + 目标语言、输出说明「字幕将写入视频同目录（只写 .srt）」、跳过已有字幕开关（默认开）。
  4. **提交**：必须勾选「音频/字幕文本将上传到第三方服务」；超过 20 条二次确认；提交后展示批次进度卡（总数/完成/失败/剩余 + 暂停/继续/取消/重试）。
- 独立 ES module `src/web/js/subtitle-page.js`，由 `app.js` 导入；不新增第三方依赖、不用 `innerHTML`、符合 ES2019。
- 设置页「云端字幕」分组提供 `subtitle_output_mode`（下拉）、`subtitle_skip_existing`（勾选）、`subtitle_job_limit`（数字，20–1000）；页面进度/批次状态/错误码在 `src/web/js/constants.js` 统一维护。
- 原「视频结果行 → 生成字幕」入口保留，行为统一走 `POST /api/subtitles`（默认 `output_mode` 取自设置）。
- 任务中心「历史记录 → 字幕任务」列表与详情展示来源、写回路径、跳过原因、`OUTPUT_NOT_WRITABLE/OUTPUT_WRITE_PARTIAL` 警告；任务目录下载入口保留。视频结果行完成弹窗同样展示写回/跳过/警告。
- 批次完成提醒、切换视图后轮询恢复、「在任务中心查看」直达字幕任务标签；目录/递归/模式沿用浏览器本地记忆。

## 10. 错误码

新增映射（`errors.ERROR_STATUS`）：

| code | HTTP | 场景 |
|---|---|---|
| INVALID_DIRECTORY | 400 | 枚举路径不是目录 |
| ENUMERATE_LIMIT | 400 | `max_items` 超上限 |
| BATCH_EMPTY | 400 | 批次没有任何有效任务 |
| BATCH_TOO_LARGE | 400 | 路径数超 `SUBTITLE_BATCH_MAX_ITEMS` |
| SOURCE_SRT_MISSING | 400 | 只翻译但视频旁无 `<stem>.srt` |
| OUTPUT_NOT_WRITABLE | 400 | 视频旁目录不可写（任务级 warning，不作为接口错误抛出） |

任务失败码（写入 job.error.code，不新增 HTTP 语义）：`SOURCE_SRT_MISSING`（运行期源字幕丢失）、`NO_SPEECH`（已有字幕无可用内容）。`OUTPUT_NOT_WRITABLE` 只作为任务 `warning`，不判失败。

## 11. 测试

- 枚举：递归/非递归、系统与隐藏目录、符号链接、后缀大小写、权限错误、`max_items` 截断、已有字幕识别。
- 批次：提交/去重/部分拒绝/上限、暂停/继续/取消/重试、按批次过滤列表、非管理员 403。
- 落盘：三种文件命名、跳过已存在、不可写回退（`OUTPUT_NOT_WRITABLE`）、原子写、视频旁 srt 在 LRU 清理后仍存在。
- 只翻译：不启动 FFmpeg/不调 ASR（用假 ASR 服务断言零请求）、`SOURCE_SRT_MISSING`、译文写入。
- 回归：v1.3 现有 57 例全部保持通过；`qa/static_checks.py`、`qa/check_web_modules.mjs`、web smoke。
- 端到端：假云服务 + 临时目录 3 个视频 → enumerate → batch → 断言视频旁 `.srt` 存在且内容可解析。

## 12. 版本与兼容

- 版本 **1.1.0-0030**，需同步 8 处：`README.md`、`build/build_spk.py`、`docs/ARCHITECTURE.md`、`docs/CONTRACT.md`、`docs/SPK_PACKAGING.md`、`spk/INFO`、`src/backend/dupcleaner/config.py`、`src/web/index.html`（含 `app.js?v=` 查询串）。
- 现有 `POST /api/subtitles` 行为不变（新字段均为可选）；旧任务记录可读；旧 SPK 升级后数据目录与包 ID 不变。
- 文档同步：本文件、`docs/SUBTITLE_CONTRACT.md`（新增 §13 指向本文件）、`docs/ARCHITECTURE.md`（字幕页与批次服务）。
