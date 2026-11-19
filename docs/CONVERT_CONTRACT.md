# 格式转换契约（v0056，v0061 修订）

> 状态：**现行**（v0056 起，v0061 调整默认值）。实现：`src/backend/dupcleaner/convert.py` + `src/web/js/convert-page.js`。
> 本契约定义「转换」页的行为、接口、预设、设置键、错误码与安全边界；改实现前先改这里。

## 1. 范围

做：

- 视频 → 视频：换容器、换编码（H.264 / H.265 / VP9 / MPEG-4）；
- 视频 → 音频：提取并转码，或原样提取；
- 音频 → 音频：MP3 / AAC / FLAC / WAV / Vorbis / Opus；
- 只换封装（`-c copy`）：MKV / MP4，秒级、不改画质；
- 批量：目录递归枚举或多路径提交，单个任务队列串行（默认并发 1）。

不做（明确拒绝，不是"以后再说"）：

- 音频 → 视频（凭空生成画面没有意义）；
- 图片、字幕文件转换；字幕烧录（v1 不做，需要 libass + 字体环境）；
- 硬编（v1 只用 CPU；NAS 上 VAAPI 不稳定）；DRM、直播、BT；
- HDR tonemap：检测到 HDR 只告警，不保证色彩正确；
- 单任务断点续转：FFmpeg 无此能力，只支持取消/重试（重试从头开始）。

## 2. 支持矩阵

预设 = 容器 + 视频编码 + 音频编码 + 附加参数。UI 不暴露"任意编码 × 容器"的自由组合，
避免一半组合无效。`copy=true` 的预设只重写容器。

### 2.1 视频预设

| id | 输出 | 视频 | 音频 | copy | 说明 |
|---|---|---|---|---|---|
| `mp4_h264` | `.mp4` | libx264 CRF 18/23/28 | AAC 256/192/128k | 否 | 通用/手机/网页（默认） |
| `mp4_h265` | `.mp4` | libx265 CRF 20/25/30 | AAC | 否 | 同画质省空间；`-tag:v hvc1` |
| `mkv_h264` | `.mkv` | libx264 | AAC | 否 | 复制字幕轨（`0:s?`） |
| `mkv_copy` | `.mkv` | copy | copy | **是** | 任意编码可封装，秒级 |
| `mp4_copy` | `.mp4` | copy | copy | **是** | 仅 H.264/HEVC/AV1 + AAC/MP3/AC3/EAC3/ALAC；`+faststart` |
| `webm_vp9` | `.webm` | libvpx-vp9 CRF 28/33/38 | Opus | 否 | `-b:v 0 -row-mt 1 -cpu-used 2` |
| `mov_h264` | `.mov` | libx264 | AAC | 否 | 剪辑软件 |
| `ts_h264` | `.ts` | libx264 | AAC | 否 | 老电视/机顶盒 |
| `avi_mpeg4` | `.avi` | mpeg4 q 3/5/8 | MP3 VBR q0/q2/q5 | 否 | 极老设备 |

### 2.2 音频预设

| id | 输出 | 编码 | 质量三档 |
|---|---|---|---|
| `mp3` | `.mp3` | libmp3lame | `-q:a 0/2/5` |
| `m4a` | `.m4a` | aac | 256/192/128k |
| `flac` | `.flac` | flac | `-compression_level 8/5/2` |
| `wav` | `.wav` | pcm_s16le | 忽略质量 |
| `ogg` | `.ogg` | libvorbis | `-q:a 6/5/3` |
| `opus` | `.opus` | libopus | 192/128/96k |
| `audio_copy` | `.mka` | copy | 取第一条音轨，原样封装 |

### 2.3 公共参数

- 只取首条视频 + 首条音频（`-map 0:v:0 -map 0:a:0?`）；MKV 预设额外 `-map 0:s?`；
- `-map_metadata 0 -map_chapters 0` 保留元数据与章节；
- 分辨率上限：`scale=-2:min(H\,ih)`，只缩小不放大（仅重编码生效）；
- 声道：`keep` / `stereo`（`-ac 2`）；
- 速度档：`ultrafast…medium`，只对 libx264/libx265 生效；
- 质量档：`high/standard/small`，映射见上表；
- 编码器能力在运行时用 `ffmpeg -encoders` 探测，缺失编码器的预设在 UI 置灰。

## 3. API

全部需要管理员会话。v0058 起转换是独立功能，**不再有启用开关**（`convert_enabled` 键仅为兼容保留、不再生效）；`capabilities.enabled` 恒为 `true`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/convert/capabilities` | ffmpeg 版本/路径、16 个预设（含 `available`/`reason`）、默认值、并发、空闲时段、计数、`source_extensions`（video/audio/all/selected）、`limits`（`batch_max_items`/`enumerate_max_items`/`job_limit`/`job_limit_max`） |
| GET | `/api/convert?status=&offset=&limit=&q=` | 任务列表（`status` 逗号分隔、`q` 匹配源路径/产物名/预设） |
| POST | `/api/convert` | 提交；`{paths:[], preset, options:{}, confirm}` → 202 |
| POST | `/api/convert/enumerate` | `{path, recursive, extensions?}` → 候选文件 + `skipped` 统计。`extensions` 为后缀数组（v0061：可多选子集；只接受登记过的音视频后缀，空数组/非法后缀 → 400；不传则用设置 `convert_source_extensions`，为空表示全部支持后缀） |
| GET | `/api/convert/queue` | 队列快照：`active/queued/recent/counts/concurrency/offpeak/storage` |
| GET | `/api/convert/{job_id}` | 单任务详情（含 `timeline.steps`、`options`、`source_info`） |
| POST | `/api/convert/{job_id}/cancel` | 取消（排队直接删；运行中 kill 进程并清理 `.dcpart`） |
| POST | `/api/convert/{job_id}/retry` | 已结束任务重新排队 |
| DELETE | `/api/convert/{job_id}?delete_output=1` | 删除记录；`delete_output=1` 时同时删除产物 |
| POST | `/api/convert/bulk` | `{action: cancel_all|retry_failed|clear_finished}` |
| GET | `/api/convert/export` | CSV（UTF-8 BOM + 中文表头） |

提交体：

```json
{
  "paths": ["/volume1/video/a.mkv"],
  "preset": "mp4_h264",
  "options": {
    "quality": "standard", "speed": "veryfast", "max_resolution": 0,
    "channels": "keep", "output_mode": "alongside", "output_dir": "",
    "output_subdir": "", "overwrite": "trash", "name_template": "{name}",
    "keep_source": false, "copy_subtitles": true, "delete_source": true
  },
  "confirm": "DELETE SOURCE"
}
```

- `delete_source` 默认 `true`（v0061，跟随 `convert_delete_source`）；显式传 `keep_source=true` 或
  `delete_source=false` 则保留源文件。
- `confirm` 只在**本次显式要求删除**（`delete_source: true` 或 `keep_source: false`）时必须等于
  `DELETE SOURCE`，否则 400；沿用默认删源不再强制（前端会自动带上该 token）。

## 4. 任务与队列语义

- 状态：`queued / running / done / failed / cancelled`（没有 paused：FFmpeg 不能续转）。
- 队列：FIFO（按 `created_epoch`），并发上限 `convert_concurrency`（1–2，默认 1）。
- 提交上限：单请求 `CONVERT_BATCH_MAX_ITEMS = 200` 个文件（超了 400）；v0062 起前端按
  `capabilities.limits.batch_max_items` 自动分批连续提交，整目录可一次提完。
- 活动任务上限 `convert_job_limit`（10–5000，v0062 默认 2000）：活动数 + 本次提交数超限 → 409
  `JOB_LIMIT_REACHED`；分批提交时若中途撞上限，已成功的批次保留、剩余文件仍在「已选」里。
- 空闲时段：`convert_offpeak_enabled=true` 时，窗口外的任务保持 `queued`，调度器不取；
  窗口按本地时间、支持跨零点。窗口内才开跑。
- 进度：FFmpeg `-progress pipe:1` 的 `out_time_us` / 源时长 → 5–95%，完成 100%；
  `speed`/`fps` 直接透传给 UI 估算 ETA。
- 重启：`running → queued`（FFmpeg 不能续转，重新从头开始），残留 `.dcpart` 由下一次执行覆盖。
- 失败：一律删除 `.dcpart`（v0061 起 `convert_keep_temp` 降级为兼容键、不再生效）。
- 任务快照新增 `overwritten_path`（被覆盖的旧产物路径）与 `trashed_count`（移入回收站的条目数）。
- 超时：`max(600, 源时长 × 系数)`，copy 系数 1、转码系数 8。
- 产物校验：体积 ≥ 1024B、音/视频轨存在、时长偏差 ≤ 5s（超出只告警，不判失败）。

## 5. 数据模型（schema v10）

```sql
CREATE TABLE convert_jobs (
    job_id TEXT PRIMARY KEY,
    status TEXT,
    payload_json TEXT NOT NULL,
    created_epoch REAL NOT NULL,
    updated_epoch REAL NOT NULL
);
CREATE INDEX idx_convert_jobs_created ON convert_jobs(created_epoch);
CREATE INDEX idx_convert_jobs_status ON convert_jobs(status, created_epoch);
```

- v9 → v10 为**纯新增表**，幂等迁移，不动历史数据；升级后旧版本仍可按兼容模式打开。
- `payload_json` 保存任务快照（含 `options`、`source_info`、`timeline`、`verify`）。
- 任务 ID 形如 `cv-<20 位 hex>`；Store 侧用 `SAFE_ID_RE` 校验。

## 6. 设置键（默认值 / 校验）

| 键 | 默认 | 校验 |
|---|---|---|
| `convert_enabled` | `true` | 兼容键，v0058 起不再生效（功能恒可用） |
| `convert_output_dir` | `""` | 绝对路径或空 |
| `convert_output_mode` | `alongside` | `alongside/fixed/subdir` |
| `convert_output_subdir` | `converted` | 单层目录名，无分隔符/`..`，≤60 字符 |
| `convert_concurrency` | `1` | 1–2 |
| `convert_default_preset` | `mp4_h264` | 预设 ID |
| `convert_quality` | `standard` | `high/standard/small` |
| `convert_speed` | `veryfast` | `ultrafast/superfast/veryfast/faster/medium` |
| `convert_overwrite` | `trash` | `trash/rename/skip/overwrite`（v0061：默认 `trash`；旧值 `rename` 首次启动一次性迁移） |
| `convert_delete_source` | `true` | bool（v0061：转换成功后删除源文件） |
| `convert_source_extensions` | `[]` | 后缀数组；空 = 全部支持后缀；只接受登记过的音视频后缀，去重，≤40 项 |
| `convert_keep_source` | `true` | **兼容键**（v0061 起不再生效） |
| `convert_copy_subtitles` | `true` | bool（MKV 预设复制字幕轨） |
| `convert_max_resolution` | `0` | 0/2160/1080/720/480 |
| `convert_job_limit` | `2000` | 10–5000（v0062：默认由 500 提到 2000，旧默认值一次性迁移） |
| `convert_min_free_gb` | `20` | 0–102400 |
| `convert_keep_temp` | `false` | **兼容键**（v0061 起失败一律清 `.dcpart`） |
| `convert_offpeak_enabled` | `false` | bool |
| `convert_offpeak_start` | `01:00` | HH:MM |
| `convert_offpeak_end` | `07:00` | HH:MM（可跨零点） |
| `convert_name_template` | `{name}` | 无路径分隔符；占位符 `{name}{preset}{date}` |
| `convert_auto_scan` | `false` | bool |
| `convert_auto_subtitle` | `false` | bool |

> 新增设置键必须同步三处：`settings.DEFAULT_SETTINGS` + `_normalize_value` 分支 + 前端
> fill/read（`app.js` + `js/constants.js`）。漏任一处会让「保存设置」整单 400（v0054 事故），
> `BE-SETTINGS-KEYS` / `BE-SETTINGS-NORMALIZE` 会兜底。

## 7. 错误码

| 码 | HTTP | 场景 |
|---|---|---|
| `CONVERT_DISABLED` | 403 | 未启用 / 服务未装配 |
| `PRESET_UNKNOWN` | 400 | 未知预设 ID |
| `PRESET_UNSUPPORTED` | 400 | 源编码与预设不兼容，或 FFmpeg 缺编码器 |
| `SOURCE_INVALID` | 400 | 文件不存在/非普通文件/无音视频轨/探针超时 |
| `OUTPUT_EXISTS` | 409 | 同名文件过多，无法自动编号 |
| `OUTPUT_DIR_INVALID` | 400 | 输出目录不在共享内/不可写/创建失败 |
| `INSUFFICIENT_SPACE` | 409 | 剩余空间低于 `convert_min_free_gb` |
| `CONVERT_BUSY` | 409 | 状态不允许该操作（已结束再取消等） |
| `JOB_LIMIT_REACHED` | 409 | 活动任务超过 `convert_job_limit` |
| `CONVERT_FAILED` | 500 | FFmpeg 非 0 退出 / 产物校验失败 |
| `CONVERT_TIMEOUT` | 504 | 超过 `max(600, 时长×系数)` |

## 8. 落盘与清理

- 输出位置：`alongside`（源目录，默认）/ `fixed`（`convert_output_dir`）/
  `subdir`（`convert_output_dir/convert_output_subdir`）；目录必须可写，必要时自动创建。
- 命名：`convert_name_template` 渲染 + 预设扩展名。
- 同名策略（v0061）：`trash`（默认）= 覆盖，旧产物先移入
  `<共享文件夹>/#duplicate-cleaner-trash/<时间戳>/<相对路径>` 并登记回收站清单；
  `rename` = `名称 (2).ext`；`skip` = 跳过；`overwrite` = 直接覆盖（旧的不可恢复）。
- 移动旧产物的时机是「产物校验通过之后、`os.replace` 之前」；`os.replace` 失败会把旧文件还原。
  回收站移动失败则降级为 `rename`，绝不静默丢弃。回收站 id 形如转换任务 `cv-…`，可在回收站页恢复/清理。
- 源 = 目标（同目录同格式）时**强制改名**，任何策略都不允许覆盖源文件。
- 写 `.dcpart` → 校验 → `os.replace` 原子改名（同目录，不跨卷）。
- `delete_source`：默认开启（`convert_delete_source`），只在产物校验通过并原子改名后删除，
  遵循 `is_regular_file`（拒符号链接），记审计 `convert_delete_source`。
- 失败 / 取消 / 超时：一律删除 `.dcpart`。

## 9. 前端

- 导航「转换」→ `#/convert`；`VIEWS`/`VIEW_HEADINGS` 注册 `convert`/`convertHeading`。
- 模块 `js/convert-page.js`：能力探测、源文件后缀多选（v0061，勾选即保存）、目录枚举、
  手动粘贴路径、已选列表、预设/质量/分辨率、输出位置、**自动分批提交（v0062，
  每批 ≤ `limits.batch_max_items`，显示「x/y 批」进度，只把真正入队的文件移出已选列表）**、
  队列轮询（2s）、取消/重试/删除/批量、CSV 导出。
- **已选列表分页渲染（v0063）**：选择集 `state.convert.selected` 始终是全量（提交不受分页影响），
  列表只渲染当前页（默认 60 条，可选 60/120/240），并提供按名称/路径筛选、上一页/下一页、
  摘要（总数/总大小/当前区间/按后缀计数）。取消勾选导致页数减少时页码自动夹紧；
  重新枚举或提交清空后回到第一页。`paginate()` 为导出纯函数，供模块探针测试。
- 删源项默认勾选且不再弹二次确认（v0061）；取消勾选即保留源文件。
- 只用 `el()`/`textContent`，不写 `innerHTML`；进度条宽度用 `node.style.width`。
- 预设卡明确区分「仅换封装（秒级、无损）」与「需要重新编码（CPU 高）」。
- 未启用 / FFmpeg 不可用 / 空闲时段未到，都在页头徽标与提交按钮上体现。

## 10. 安全

- 源文件必须在共享文件夹内、是普通文件、拒绝任一层的符号链接；
- 输出目录同样走 `check_path_safety` + `share.validate_root`；
- 同名策略默认 `trash`（覆盖但旧产物进回收站）；`overwrite` 只针对产物，源文件永不被原地覆盖；
- 「删除源文件」默认开启，但必须产物校验通过后执行；请求显式要求删除时仍需
  `confirm="DELETE SOURCE"`；失败不删源；
- 后缀筛选只允许登记过的音视频后缀，避免被用来扫描任意文件；
- FFmpeg 用参数数组调用（无 shell），`-nostdin`，取消时 kill 进程；
- stderr 只保留最后一行（≤400 字符），不包含 Cookie 等敏感值。

## 11. 非目标

见 §1；另外不做：字幕烧录、HDR tonemap、转换后自动替换 Jellyfin 元数据、
定时批量转换（用空闲时段 + 队列即可）。GPU 硬编自 v0065 起**支持 Intel VAAPI**
（见 §14），仍不做 NVIDIA NVENC / Apple VideoToolbox。

## 12. 与扫描/字幕/下载联动

- `convert_auto_scan`：转换完成后把产物目录入队扫描（同一目录 10 分钟去抖）；
- `convert_auto_subtitle`：转换完成后自动提交字幕任务（要求 `subtitle_enabled`）；
- 通知：新增事件 `convert_done` / `convert_failed`（Webhook / Telegram）；
- 下载中心产物可以直接作为转换输入（都是共享内文件）。

## 13. 测试与验收

- 单元：`src/backend/tests/test_v0056_convert.py`（预设金标、探测/进度解析、兼容性、
  命名/覆盖/跳过/覆盖进回收站、取消/重试、持久化、失败清理、多后缀枚举、真 FFmpeg 转码）、
  `tests/test_v0061_convert_defaults.py`（默认值迁移 + 后缀校验）；
- 静态：`BE-CONVERT-MODULE/PRESETS/ARGS/SAFETY/TRASH-OVERWRITE/DELETE-DEFAULT/EXT-FILTER/LIMITS/SCHEMA`、
  `FE-CONVERT-UI/NAV/PROGRESS/EXTS/DELETE-DEFAULT/BATCH/LIST-PAGER`、`BE-SETTINGS-KEYS/NORMALIZE`；
- e2e：`CV-00`…`CV-20`（未启用 403、能力、枚举、多后缀子集、非法/空后缀、未知预设、
  删源确认、提交、队列、详情、筛选、取消/重试/删除、CSV、批量取消、真实仅换封装、
  默认值元数据、真实转码覆盖进回收站、分批/排队/枚举上限）；
- 验收：MP4→MKV copy 秒级且时长一致；AVI→MP4 H.264 可播放；MKV→MP3 可播放；
  取消 2 秒内生效且不留 `.dcpart`；重启后队列恢复；设置保存不 400。

## 14. 硬件编解码（v0065）

### 14.1 能力与门槛

- 硬件路径 = Intel iGPU + **VAAPI**；DS920+（Gemini Lake / UHD 600）实测：
  `libx264 medium` 1080p ≈1.0×、i965 硬编 H.264 2.02×、iHD 硬编 H.264 3.49×、
  iHD 硬编 HEVC 2.24×、iHD 硬解 HEVC + 硬编 H.264（全 GPU）**9.07×**。
- 三道门槛：`/dev/dri/renderD128` 属 `root:videodriver 660`（`spk/conf/privilege`
  的 `join-groupname: videodriver`）；驱动目录必须显式给 `LIBVA_DRIVERS_PATH`；
  内置 FFmpeg（BtbN 静态 8.1）对 libva 用 implib 运行时 dlopen，`libva.so.2` /
  `libva-drm.so.2` / iHD 依赖的 `libigdgmm.so.12` 必须随包（`target/lib/vaapi`）。

### 14.2 预设

| id | 编码器 | 说明 |
|---|---|---|
| `mp4_h264_hw` | `h264_vaapi` | MP4·H.264（硬件），`-rc_mode CQP -qp 20/24/28` |
| `mp4_h265_hw` | `hevc_vaapi` | MP4·H.265（硬件），`-qp 22/26/30`，`-tag:v hvc1` |
| `mkv_h264_hw` | `h264_vaapi` | MKV·H.264（硬件）+ 复制字幕轨 |

- 解码策略：源编码在驱动能力表（iHD: h264/hevc/vp9；i965: h264/vp8/vp9）→
  全 GPU：`-hwaccel vaapi -hwaccel_device <dev> -hwaccel_output_format vaapi`，
  缩放用 `scale_vaapi=w=-2:h=N:format=nv12`；否则软解 + `format=nv12,hwupload`。
- 质量用 CQP（`-qp`），不是 CRF；`config.VAAPI_QP` 是唯一事实来源。
- `convert_hwaccel`（`auto`/`off`）控制是否启用；`off` 时硬件预设置灰。

### 14.3 探测与回退

- `dupcleaner/hwaccel.py`：设备 → 驱动（iHD 优先，缺 `libigdgmm.so.12` 退 i965）→
  环境变量；带缓存与 `reset_cache()`。`FFmpegTool.probe_vaapi()` 跑一次最小
  `-init_hw_device vaapi[:dev]` 确认可用（缓存）。
- `GET /api/convert/capabilities` 新增 `hardware: {available, device, driver,
  drivers_path, libs_path, reason, mode, encoders, codecs}`；硬件预设不可用时
  `available=false` + `reason`。
- 执行失败（驱动/像素格式不支持等）→ 自动回退同容器 CPU 预设重跑一次
  （`HW_FALLBACK`），写审计 `convert_hw_fallback`，任务快照 `hw_fallback=true`。

### 14.4 测试

- 单元 `tests/test_v0065_convert_hw.py`（15 例）：参数金标（全 GPU / 软解硬编 /
  缩放 / CQP / 无 `-pix_fmt`）、预设置灰、探测与环境变量、CPU 回退；
- 静态 `BE-CONVERT-HWACCEL` / `BE-CONVERT-HW-PRESETS` / `SPK-PRIVILEGE-VIDEODRIVER` /
  `SPK-FFMPEG-VAAPI` / `SPK-VAAPI-BUNDLE` / `FE-CONVERT-HW`；
- e2e `CV-21`：无核显环境下硬件预设置灰并给出原因。
