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
- v0035 起支持**补翻译**：已完成但未翻译（或部分译文回退）的历史任务，可单独派生一条仅翻译任务，文本取自源任务目录 `original.srt`（详见 §15）。
- **不在范围**：`custom_dir` 输出模式；`.vtt` 落盘到视频旁；字幕编辑/手工校对；说话人分离。

## 2. 新增配置

| key | 默认 | 约束 |
|---|---|---|
| subtitle_output_mode | "alongside_video" | `alongside_video` / `download_only`；其它值拒绝 |
| subtitle_skip_existing | true | 布尔；目标 srt 已存在时跳过该视频 |
| subtitle_job_limit | 200 | 20..1000；已完成任务保留上限（LRU 只删任务目录，不删视频旁 srt）；**修改后需重启套件生效**（服务启动时读取） |
| subtitle_concurrency | 1 | 1..4；同时执行的字幕任务数（默认 1 串行）；**保存设置后立即生效**（v0037 起动态调整 worker，无需重启套件） |
| subtitle_translate_outputs | both | v0036：译文输出组合 `both`/`target_only`/`bilingual_only`；控制任务目录与视频旁写出的译文文件数量；`original.srt` 始终保留 |
| subtitle_audio_prefetch | true | v0040：分片音频预取（布尔）。识别当前分片时后台先抽下一分片音频，FFmpeg 与 ASR 重叠；关闭后严格串行。保存后只影响新任务（任务开始时读取） |

以下为常量（不做成设置项，避免配置面膨胀）：

| 常量 | 值 | 说明 |
|---|---|---|
| SUBTITLE_BATCH_MAX_ITEMS | 500 | 单次批量提交路径上限 |
| SUBTITLE_ENUMERATE_MAX_ITEMS | 20000 | 单次目录枚举返回上限；超出截断并置 `truncated=true`（v0032 起 5000→20000） |
| SUBTITLE_ALONGSIDE_EXT | ".srt" | 允许写回视频旁的后缀 |

`subtitle_output_mode` 只影响「是否额外复制到视频同目录」；任务目录默认只生成 `original.srt`（v0033 起；`subtitle_write_vtt=true` 时额外生成 `original.vtt`，可下载）。

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

`GET /api/subtitles?limit=&offset=&batch_id=&group_id=&status=` 支持按批次/批次组过滤并分页；
带 `group_id` 或 `offset` 时返回 `{"ok":true,"jobs":[...],"total":N,"offset":N,"limit":N}`。

v0034 新增 `status`：`all`（默认）/`active`（= running+translating）/`queued`/`running`/`translating`/
`paused`/`done`/`failed`/`cancelled`；多个状态可用逗号组合（如 `failed,cancelled`，用于“失败”筛选包含已取消任务）；
非法值 400 `INVALID_ARGUMENT`；分页 `total` 为过滤后数量。
历史任务按创建时间倒序返回（稳定分页）；"执行中"置顶由前端置顶区块/状态筛选呈现，不改列表排序。

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

- 任务目录（始终）：`<var>/subtitles/<job_id>/original.srt`；v0033 起默认不再生成 `.vtt`，
  `subtitle_write_vtt=true` 时额外生成 `original.vtt`、`<lang>.vtt`；翻译时始终有 `<lang>.srt`、`bilingual.<lang>.srt`。
- 视频同目录（`output_mode=alongside_video` 时复制）：

| 文件 | 内容 | 写入条件 |
|---|---|---|
| `<stem>.srt` | 原文 | `source=asr` 且不存在 |
| `<stem>.<target>.srt` | 译文 | 有译文且不存在；v0036 起 `subtitle_translate_outputs=bilingual_only` 时不写 |
| `<stem>.bilingual.<target>.srt` | 双语 | 有译文且不存在；v0036 起 `subtitle_translate_outputs=target_only` 时不写 |

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
  "source_job_id": null,
  "batch_id": "bat-...",
  "group_id": "grp-...",
  "audio_source": "nas",
  "output_mode": "alongside_video",
  "skipped": false,
  "warning": null,
  "outputs": [{"name": "a.srt", "path": "...", "size": 123, "kind": "original"}],
  "output_errors": []
}
```

- `group_id`：v0032 批次组 ID（单批提交/单任务为 null）。
- `audio_source`：提交时的音频来源快照（`nas` | `server`），运行期不随设置变化。
- `source_job_id`：v0035 补翻译任务指回源任务（`sub-...`）；普通任务为 null。仅存 payload_json，无 schema 变更。

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
- v0032 批次组：组内任务**不参与**单任务 LRU；组按 `SUBTITLE_BATCH_GROUP_LIMIT`（默认 20）整体保留，
  超出时只清理已终态（done/failed/cancelled）的最老组，暂停/运行中的组不清理。
- 重启恢复：`queued/running` → `paused`（沿用 v1.3）；批次/批次组状态由子任务实时聚合，不存独立状态机。
- 任务与批次 ID：`sub-<时间戳>-<随机>` / `bat-<时间戳>-<随机>`，字符集 `[A-Za-z0-9._-]`。

## 9. 前端

- 导航新增 **「字幕」**（顺序：扫描 / 字幕 / 历史记录 / 操作日志 / 设置），`<section id="view-subtitles" data-view="subtitles">`。
- 页面结构：
  1. **选择目录**：复用 `Picker` 目录选择器（`/api/browse`），「包含子目录」开关，「枚举视频」按钮；显示 `N 个视频 / X GB / 已有字幕 M 个`。
  2. **视频列表**：勾选/全选、按「全部 / 缺少字幕 / 已有字幕」过滤；行内显示路径、大小、已有字幕徽标；超过 200 条分页「加载更多」（前端截断展示，不重新请求）。
  3. **选项**：识别语言、生成译文 + 目标语言、输出说明「字幕将写入视频同目录（只写 .srt）」、跳过已有字幕开关（默认开）。
  4. **提交**：必须勾选「音频/字幕文本将上传到第三方服务」；超过 20 条二次确认；提交后展示批次进度卡（总数/完成/失败/剩余 + 暂停/继续/取消/重试）。v0031 起提交按钮**禁用原因常显**（未选视频/未勾确认项/超单批上限/配置缺失），「全选」只选到单批上限并提示分批，避免“全选 5000 个后按钮点不动”的误解。
- 独立 ES module `src/web/js/subtitle-page.js`，由 `app.js` 导入；不新增第三方依赖、不用 `innerHTML`、符合 ES2019。
- 设置页「云端字幕」分组提供 `subtitle_output_mode`（下拉）、`subtitle_skip_existing`（勾选）、`subtitle_job_limit`（数字，20–1000）；页面进度/批次状态/错误码在 `src/web/js/constants.js` 统一维护。
- 原「视频结果行 → 生成字幕」入口保留，行为统一走 `POST /api/subtitles`（默认 `output_mode` 取自设置）。
- 任务中心「历史记录 → 字幕任务」列表与详情展示来源、写回路径、跳过原因、`OUTPUT_NOT_WRITABLE/OUTPUT_WRITE_PARTIAL` 警告、`translation_partial`（部分译文回退原文）与 `SERVER_AUDIO_FALLBACK`（服务器解码回退 NAS）；任务目录下载入口保留。
- v0032 批次组：一次提交可含 1..20000 个视频，前端**全选不截断**；提交后显示“将分为 M 批（每批 ≤500）”，进度卡展示组聚合进度、批次列表（前 50 可展开）与任务明细分页（每页 100，加载更多）；暂停/继续/取消/重试作用于整组；组 ID 存 `localStorage['dupcleaner.lastSubtitleGroup.v1']` 以便切页/刷新恢复。
- 设置页新增「音频来源」（`nas` 本地解码 / `server` NAS 只流复制音轨、服务器解码；默认 nas）。
- 批次完成提醒、切换视图后轮询恢复、「在任务中心查看」直达字幕任务标签；目录/递归/模式沿用浏览器本地记忆。

## 10. 错误码

新增映射（`errors.ERROR_STATUS`）：

| code | HTTP | 场景 |
|---|---|---|
| INVALID_DIRECTORY | 400 | 枚举路径不是目录 |
| ENUMERATE_LIMIT | 400 | `max_items` 超上限 |
| BATCH_EMPTY | 400 | 批次没有任何有效任务 |
| BATCH_TOO_LARGE | 400 | 路径数超 `SUBTITLE_BATCH_GROUP_MAX_ITEMS`（v0032：默认 20000；批次内自动拆成 ≤500） |
| SOURCE_SRT_MISSING | 400 | 只翻译但视频旁无 `<stem>.srt` |
| OUTPUT_NOT_WRITABLE | 400 | 视频旁目录不可写（任务级 warning，不作为接口错误抛出） |
| TRANSLATE_DISABLED | 400 | `subtitle_translate_enabled=false`（v0035 补翻译同样适用） |
| TRANSLATE_NOT_CONFIGURED | 400 | 补翻译时 `subtitle_llm_api_key` 为空 |
| TRANSLATION_EXISTS | 400 | 源任务已有完整译文，无需补翻译 |
| JOB_NOT_DONE | 400 | 源任务未完成，不能补翻译 |

任务失败码（写入 job.error.code，不新增 HTTP 语义）：`SOURCE_SRT_MISSING`（运行期源字幕丢失）、`NO_SPEECH`（已有字幕无可用内容）。`OUTPUT_NOT_WRITABLE` 只作为任务 `warning`，不判失败。

## 11. 测试

- 枚举：递归/非递归、系统与隐藏目录、符号链接、后缀大小写、权限错误、`max_items` 截断、已有字幕识别。
- 批次：提交/去重/部分拒绝/上限、暂停/继续/取消/重试、按批次过滤列表、非管理员 403。
- 落盘：三种文件命名、跳过已存在、不可写回退（`OUTPUT_NOT_WRITABLE`）、原子写、视频旁 srt 在 LRU 清理后仍存在。
- 只翻译：不启动 FFmpeg/不调 ASR（用假 ASR 服务断言零请求）、`SOURCE_SRT_MISSING`、译文写入。
- 回归：v1.3 现有 57 例全部保持通过；`qa/static_checks.py`、`qa/check_web_modules.mjs`、web smoke。
- 端到端：假云服务 + 临时目录 3 个视频 → enumerate → batch → 断言视频旁 `.srt` 存在且内容可解析。

## 12. 版本与兼容

- 版本 **1.1.0-0046**（v0039 起含任务队列视图与批次组总览，v0040 起含分片音频预取，v0041 起任务行补 `eta_seconds` 与进度显示修复，v0042 起启动加载提速与升级启动稳定性，v0043 起重复扫描侧增量缓存/保留规则/排除规则，v0044 起预览缩略图/并排对比与删除白名单，v0045 起硬链接合并与套件独立回收站，v0046 起计划扫描与通知系统，字幕侧行为不变），需同步 8 处：`README.md`、`build/build_spk.py`、`docs/ARCHITECTURE.md`、`docs/CONTRACT.md`、`docs/SPK_PACKAGING.md`、`spk/INFO`、`src/backend/dupcleaner/config.py`、`src/web/index.html`（含 `app.js?v=` 查询串）。
- 现有 `POST /api/subtitles` 行为不变（新字段均为可选）；旧任务记录可读；旧 SPK 升级后数据目录与包 ID 不变。
- 文档同步：本文件、`docs/SUBTITLE_CONTRACT.md`（新增 §13 指向本文件）、`docs/ARCHITECTURE.md`（字幕页与批次服务）。

## 13. 批次组（v0032：全选与自动拆批）

### 13.1 目标

「全选」选中当前枚举结果的全部视频（不再限制 500），提交时后端自动拆成每批 ≤`SUBTITLE_BATCH_MAX_ITEMS`（500）的多个批次，
共享一个 `group_id` 顺序执行；前端/后端都以“组”为进度与控制单位。

### 13.2 API

- `POST /api/subtitles/batch-groups`（管理员 + CSRF）：请求体同 §4.1（`paths` 改为 1..20000）。
  - 202：`{"ok":true,"group":{...}}`；`BATCH_TOO_LARGE`（>20000）、`BATCH_EMPTY`、`INVALID_ARGUMENT`。
- `GET /api/subtitles/batch-groups/{group_id}` → 组快照；未知/已清理 → 404 `NOT_FOUND`。
- `POST /api/subtitles/batch-groups/{group_id}/{pause|resume|cancel|retry}`：作用于组内所有批次，返回组快照；终态组为幂等空操作。
- `GET /api/subtitles?group_id=&offset=&limit=&status=`：任务明细分页（limit 1..500，`status` 见 §4.4），返回 `total/offset/limit`。
- `/api/meta.subtitle` 新增 `batch_group_max_items`（20000）与 `audio_sources`（["nas","server"]）；v0034 增加 `concurrency_max`（4）。

组快照：

```json
{
  "group_id": "grp-20261107-091500-abc123",
  "status": "queued|running|paused|done|failed|cancelled",
  "total": 5000, "batch_count": 10, "finished": 1200, "progress": 24,
  "counts": {"queued": 0, "running": 1, "translating": 0, "paused": 0,
              "done": 1199, "failed": 0, "cancelled": 0},
  "missing": 0,
  "active_jobs": [{"job_id": "sub-...", "name": "a.mp4", "path": "/volume2/video/a.mp4",
                   "status": "running", "stage": "asr", "progress": 42,
                   "message": "正在识别语音", "chunk_index": 2, "chunk_total": 6,
                   "updated_at": "..."}],
  "eta_seconds": 432000,
  "concurrency": 1,
  "translatable": {"count": 1200, "partial": 3},
  "created_at": "...", "updated_at": "...",
  "options": {"source": "asr", "language": "auto", "translate": false,
               "target_lang": "zh", "output_mode": "alongside_video",
               "skip_existing": true, "audio_source": "nas",
               "recursive": true, "root": "/volume2/video"},
  "rejected": [], "duplicates": [],
  "batches": [{"batch_id": "bat-...", "index": 0, "total": 500,
                "status": "done", "counts": {"...": 0}}]
}
```

- `counts` 为 7 个状态键求和；`finished` = done+failed+cancelled；`progress` = round(100×finished/total)。
- v0034：`active_jobs` 为正在执行的任务（running/translating，最多 10 条），前端用于“当前执行”与置顶区块；
  `eta_seconds` 为估算剩余秒数（按已处理非 skipped 任务的平均耗时 × 剩余 ÷ 并发；样本不足为 `null`）；`concurrency` 为当前生效并发。
- v0035：`translatable` 为补翻译按钮计数（不做逐任务文件 stat，避免 20000 任务组快照变慢）：
  `count` = 已完成且 `translate=false` 的任务数；`partial` = 已完成且 `translate=true && translation_partial=true` 的任务数。
- 状态推导与批次一致：全部终态时 failed 优先，其次 cancelled（且无 done），否则 done；未全部终态时 running/translating > paused > queued。
- 组记录持久化到 SQLite `subtitle_batch_groups`（schema v7），并镜像 JSON 到 `<var>/subtitles/groups/`。
- 保留：`SUBTITLE_BATCH_GROUP_LIMIT`（默认 20）个组；只清理已终态最老组及其批次/任务；组内任务不参与单任务 LRU。

### 13.3 前端

- 全选 = 当前筛选结果全部（≤20000），提示“已选择 N 个视频（将分为 M 批，每批最多 500 个）”。
- 进度卡：组状态/聚合进度条/计数/剩余/批次数；批次列表默认前 50 可展开；任务明细分页加载。
- v0034 可见性：进度卡显示“当前执行：<文件名> · <阶段> · <进度>%”与“预计剩余 约 …”；任务明细顶部固定“执行中”区块（取自 `active_jobs`，不受筛选/分页影响），并提供状态筛选（全部/执行中/排队/已完成/失败，计数来自 `counts`，失败含已取消，请求用 `status=failed,cancelled`）。
- 任务中心「字幕任务」：先取 `status=active` 置顶“进行中”分区，再展示最近任务；状态行显示进行中数量。
- 提交按钮禁用原因常显（未选视频/超 20000/未勾确认项/字幕未启用或缺凭据）。
- 恢复：`localStorage['dupcleaner.lastSubtitleGroup.v1']` 保存 `{group_id,saved_at}`；进入字幕视图时恢复组快照与轮询，404 清 key。

## 14. v0034 队列恢复、并发与可见性

- **重启恢复**：服务启动时 `queued` 子任务保留并自动重新入队（不再全部判 `SERVICE_RESTART`），大批次组升级套件后不会丢队列；`running/translating` 有分片转 `paused` 可继续，无分片仍失败。详见 [SUBTITLE_CONTRACT.md](SUBTITLE_CONTRACT.md) §14。
- **并发**：`subtitle_concurrency`（1..4，默认 1，v0037 起保存后立即生效、无需重启）为全局同时执行任务数；批次组/批次状态仍按子任务实时聚合，`eta_seconds` 会按并发折算。
- **可见性**：组快照 `active_jobs` + 任务列表 `status` 过滤，解决“任务明细/任务中心只看得到排队任务”的问题（列表排序保持按创建时间倒序稳定分页，执行中由置顶区块展示）。

## 15. 补翻译（v0035：已完成的仅 ASR 任务）

### 15.1 目标与范围

历史任务（或提交时未勾选“同时生成译文”的批次）完成 ASR 后，可以**单独补翻译**，不重新抽音频、不重新 ASR：

- 原任务记录不变；新增一条**派生翻译任务**（`source="existing_srt"` + `source_job_id` 指回源任务），原任务与派生任务都可下载/查看/审计。
- 文本来源优先级：源任务目录 `original.srt` → 视频同目录 `<stem>.srt` → 都没有则 `SOURCE_SRT_MISSING`。视频文件已删除也可补翻译（任务目录可下载；写回视频旁按不可写/部分失败降级为 `warning`）。
- 目标语言：默认 `subtitle_translate_target`，请求体可覆盖（不得 `auto`）。
- 复用现有队列/并发/暂停/取消/重试；`translating` 计入“执行中”；重启时运行中的翻译任务转 `paused` 可继续。
- 新批次组不混入源组，便于单独查看补翻译进度；`skip_existing=false`（部分译文回退任务允许重新翻译，视频旁已有译文仍遵守“从不覆盖”）。

### 15.2 API

`POST /api/subtitles/{job_id}/translate`（管理员 + CSRF）

```json
{"confirm_cloud": true, "target_lang": "zh"}
```

- 202 新建：`{"ok":true,"created":true,"source_job_id":"sub-...","job":{...},"group":{...}}`
- 200 幂等：同一源任务已有排队/执行中的翻译任务 → `created=false` 并返回已有任务。
- 400：`SUBTITLE_DISABLED` / `TRANSLATE_DISABLED` / `TRANSLATE_NOT_CONFIGURED` / `JOB_NOT_DONE` / `TRANSLATION_EXISTS` / `SOURCE_SRT_MISSING` / `CLOUD_CONFIRM_REQUIRED` / `INVALID_ARGUMENT`；404：任务不存在。
- 源任务 `translation_partial=true` 允许重新翻译；已有完整译文返回 `TRANSLATION_EXISTS`。

`POST /api/subtitles/batch-groups/{group_id}/translate`（管理员 + CSRF）

```json
{"confirm_cloud": true, "target_lang": "zh"}
```

- 202：`{"ok":true,"created":N,"group":{新组快照}|null,"summary":{
    "eligible":N,"created":N,"already_translated":0,"missing_source":0,
    "duplicates":0,"rejected":[{"path":"...","code":"...","message":"..."}]}}`
- 逐任务独立：缺源文本 / 已完整译文只计入 `summary`，不使整组失败；`created=0` 时不创建组，`group=null`。
- `summary` 口径：`eligible` = 满足资格（done 且 `translate=false` 或 `partial=true`）且源文本可解析的任务数（含随后因已有排队翻译任务而计入 `duplicates` 的任务）；`created` = 实际新建任务数；`already_translated`/`missing_source`/`duplicates`/`rejected` 为分类计数。
- 任务按创建时间升序、按 `SUBTITLE_BATCH_MAX_ITEMS`（500）拆批；新组 `options` 带
  `{"source":"existing_srt","translate":true,"target_lang":...,"output_mode":<源任务输出方式>,"skip_existing":false,"translated_from_group":"grp-..."}`，每个 job 带 `source_job_id`。
- 审计：`subtitle_start`（`operation=translate`、`source_job_id`）与 `subtitle_translate_group`（源组、创建数）。

组快照新增（见 §13.2）：

```json
"translatable": {"count": 1200, "partial": 3}
```

### 15.3 前端

- 批次组卡片：`translatable.count>0` 时显示「补翻译（N）」，确认弹窗说明“只上传字幕文本、不上传音频，将新建翻译批次组”；成功后切换到新组并 toast 汇总（已创建 / 已有译文 / 源缺失 / 重复）。
- 任务明细行与任务中心列表/详情：`done` 且可补翻译的任务显示「补翻译」，`translation_partial=true` 显示「重新翻译」；创建后刷新列表。
- 任务来源标签显示「补翻译（源任务 <短 id>）」；翻译关闭或未配置 LLM Key 时按钮禁用并提示。

### 15.4 测试与版本

- 测试：单任务补翻译（含视频已删除）、已有译文拒绝、部分译文重译、幂等、开关/凭据缺失、组批量分类计数、组快照 `translatable`、重启转 `paused`、两个接口的状态码/响应形状；前端 node 模块检查 + web smoke + e2e（SUB-22 起）。
- 版本 **1.1.0-0035**；SQLite schema 不变（仍 v7），旧任务/旧组记录可读。

## 16. v0036 语言码扩展与译文输出组合

- **语言码**：设置页 ASR 语言/翻译目标语言下拉扩展到常见 19 种；后端 `LANGUAGE_RE` 仍接受任意 2–3 位合法语言码，API 设置的非下拉语言码在设置页保存时不再被静默重置（会保留并临时补一项）。
- **译文输出组合**：新设置 `subtitle_translate_outputs`（`both` 默认 / `target_only` / `bilingual_only`），作用于任务目录与视频同目录：
  - `both`：`<target>.srt` + `bilingual.<target>.srt`（历史行为）；
  - `target_only`：只写 `<target>.srt`（开启 VTT 时仍写 `<target>.vtt`）；
  - `bilingual_only`：只写 `bilingual.<target>.srt`（不生成 `<target>.srt`/`<target>.vtt`）。
  - `original.srt` 始终保留（补翻译/只翻译的源文本）；已有文件从不覆盖，修改设置不删除旧文件。
- 普通翻译、只翻译已有字幕、v0035 补翻译三类任务都遵循该设置。
- 版本 1.1.0-0036；SQLite schema 不变（仍 v7）；前端设置页新增「译文输出组合」下拉。

## 17. v0037 字幕并发设置保存后立即生效

- **行为变更**：`subtitle_concurrency`（1..4，默认 1）不再需要重启套件——`PUT /api/settings` 保存成功后立即按新值调整 worker 池。
- **调大**：立即补 worker，排队任务马上按新并发开始执行；**调小**：多余 worker 标记待退出，正在执行的任务跑完当前任务后退出（不中断 ASR/翻译）。
- **惰性**：没有 worker/排队任务时不会提前起线程，下一次提交按新并发启动。
- **前端**：设置页提示改为“保存后立即生效，无需重启套件”；`/api/meta.subtitle.concurrency_max`（4）与校验范围不变。
- 版本 1.1.0-0037；SQLite schema 不变（仍 v7），无数据迁移。

## 18. v0038 字幕任务步骤时间线与任务中心菜单调整

- **任务详情时间线**：`GET /api/subtitles/{job_id}` 新增 `started_at` / `finished_at` / `duration_sec` 与 `timeline.steps` / `asr_chunks` / `translate_batches`（字段口径见 [SUBTITLE_CONTRACT.md](SUBTITLE_CONTRACT.md) §18）；列表接口只带轻量时间字段。
- **前端**：任务详情新增「步骤耗时」表与可展开的分片/批次明细；任务列表显示总耗时。
- **菜单调整（方案 A）**：顶部导航改为「扫描 / 字幕 / 任务中心」+ 右上角设置入口；操作日志并入任务中心标签页（扫描历史 / 字幕任务 / 删除任务 / 操作日志）；「存储诊断」从任务中心移到设置页「系统 · 存储与运行诊断」；旧 `#/logs` 书签自动跳转到任务中心操作日志。
- 版本 1.1.0-0038；SQLite schema 不变（仍 v7）。

## 19. v0039 队列视图与批次总览

- **任务中心「字幕任务」**改为四段：**进行中**（running/translating）、**即将执行**（queued，按内部 FIFO 队列顺序，最多 20 条，带 `queue_position` 与排队总数）、**已暂停**、**最近完成**（done/failed/cancelled，按 `finished_at` 倒序）；有排队任务时也会自动轮询（无运行中任务时降为 3s）。
- **新增 `GET /api/subtitles/queue`**：`active/queued/paused/recent` + `active_total/queued_total/paused_total/recent_total` + `single_active_count`。队列顺序来自后端内部 `_queue` 的 FIFO 顺序——暂停继续、失败重试都会追加到队尾，因此不能按 `created_at` 排序。
- **新增 `GET /api/subtitles/batch-groups?active_limit=10&completed_limit=3`**（与 POST 同路径不同方法）：返回进行中/暂停组（按组内最早队列位置 `next_queue_position` 升序）与最近完成组；`options.translate` 区分“只生成字幕 / 生成+翻译”；`single_active_count` 为非批次组任务数。
- **字幕页「批次总览」**：进行中（含暂停）与最近完成批次组列表；点击切换当前组（保存 localStorage），新提交自动选中新组但旧组仍可切回并暂停/取消/查看。
- 版本 1.1.0-0039；SQLite schema 不变（仍 v7），无数据迁移。

## 20. v0040 分片音频预取

- **设置**：新增 `subtitle_audio_prefetch`（布尔，默认 `true`）；保存设置后对**新开始执行**的任务生效（任务启动时读取，已在执行的任务不切换）。
- **行为**：每个任务内只预读 **1 片**——主线程上传/识别第 N 片时，预取线程抽第 N+1 片音频；取用时已抽好则零等待。分片顺序、时间轴、字幕内容、写回文件与串行实现一致。
- **语义不变**：暂停仍在分片边界生效（预取结果丢弃）；取消/任务结束让在跑 FFmpeg 尽快退出；抽音频异常原样抛回主线程（错误码不变）；已完成分片不重复抽取；临时音频最多多占 1 片，任务结束整目录删除。
- **前端**：设置页「云端字幕」新增「分片音频预取（默认开启）」复选框；任务详情「ASR 分片明细」的「抽音频 / 识别」耗时改为记录真实时间窗（预取时两段重叠）。
- 版本 1.1.0-0040；SQLite schema 不变（仍 v7），无数据迁移。

## 21. v0041 任务行进度信息与预计剩余

- **修复**：任务中心「进行中」任务行与任务详情弹窗的进度条此前恒为空条——`el()` 不支持 `style` 选项，`width:..%` 被静默丢弃。现统一用 `subtitleProgressBar(pct)`（创建后设置 `fill.style.width`）。
- **修复**：分块文案多算一片（前端对已是 1-based 的 `chunk_index` 又 +1）；现在只有 0-based 备选字段才 +1。
- **新增 `eta_seconds`**：字幕任务视图（列表 / 队列 / 详情）新增预计剩余秒数，仅 `running`/`translating` 有值：ASR 用已完成分片均值 × 剩余分片数，翻译用已完成批次均值 × 由进度带反推的剩余批次；其它情况为 `null`。
- **展示**：任务行顺序为 `阶段 · 分块 x/y · 当前步骤消息 · 已耗时 · 预计剩余 · 译文 · 创建`，「来源」移到行尾弱化（`来源：云端 ASR 生成`），避免与阶段“云端语音识别”混淆；详情网格新增「耗时」「预计剩余」。
- **批次组**：组卡片「当前执行」与组内任务行沿用同一视图字段，无需额外改动；组快照 `active_jobs` 仍只暴露安全字段。
- 版本 1.1.0-0041；SQLite schema 不变（仍 v7），无数据迁移。

## 22. v0042 启动加载性能（大批次任务不再拖垮升级）

- **背景**：批次组可一次提交 20000 个任务，NAS 上实测 7540 个任务/4672 排队时，启动恢复要 27s，超过 DSM 升级时 `start` 的 15s 健康检查窗口 → 每次升级都被判“启动失败”并要求【修复】。
- **修复**：`_load_jobs` 在 DB 已命中时跳过 JSON 解析（20s → 0.5s 量级）；终态任务不查分片缓存；`start-stop-status` 健康检查放宽到 90s 并重试一次，`postupgrade` 补 `chown`，启动过程写 `var/log/start.log`。
- 版本 1.1.0-0042；SQLite schema 不变（仍 v7），无数据迁移。
- 版本 1.1.0-0043；本次改动集中在重复扫描模块（增量扫描哈希缓存、保留规则链、排除规则/附属文件、schema v8），**字幕侧接口/行为/数据结构不变**。
- 版本 1.1.0-0044；本次改动为预览缩略图/媒体信息/并排对比与删除白名单，**字幕侧接口/行为/数据结构不变**（schema 仍 v8）。
- 版本 1.1.0-0045；本次改动为硬链接合并与套件独立回收站，**字幕侧接口/行为/数据结构不变**（schema 仍 v8）。
- 版本 1.1.0-0046；本次改动为计划扫描与通知系统（新增扫描/字幕批次事件通知钩子），**字幕任务接口/行为/数据结构不变**（schema 仍 v8）。



