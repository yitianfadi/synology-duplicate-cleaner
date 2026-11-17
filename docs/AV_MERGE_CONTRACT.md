# 音视频合成契约（v0059）

> 模块：`src/backend/dupcleaner/avmerge.py`（服务）、`src/web/js/merge-page.js`（页面）
> 存储：SQLite schema **v11**，`avmerge_jobs`（payload_json 文档式）
> 定位：**内置功能，无启用开关**；API 前缀 `/api/avmerge`

## 1. 语义

- **按选择顺序拼接**：`video_paths[]` 与 `audio_paths[]` 的数组顺序就是输出顺序；前端两个有序列表（每行一个路径）与数组一一对应。
- **视频轨**：所有视频片段按顺序拼成一条视频；**音频轨**：所有音频片段按顺序拼成一条音频；最后混流为一个文件。
- 只选 1 个视频 + 1 个音频 = 普通混流；`audio_paths` 为空 = 输出无声视频；至少需要 1 个视频。
- **时长为王**：以拼接后的**视频总时长**为准。音频不足 → 静音补齐（`apad`）；超出 → 截断（`-t`）；容差 ±5 秒内直接拷贝。
- **两条路径**：
  - `copy`（仅换封装）：视频/音频片段编码、分辨率、像素格式、帧率（音频：采样率/声道）一致时 `-c copy` 拼接 + 混流，秒级；
  - `encode`（统一转码）：不一致或用户强制时，视频用 `concat` 滤镜 + `scale/pad/fps` 统一到首段参数（受分辨率上限约束），音频统一 48kHz 立体声 AAC，再混流。
  - 默认 `auto`：兼容则 copy，否则自动 encode（UI 会提示“直接拼接/统一转码”）。
- **容器**：`mp4`（video 需 h264/hevc/av1/mpeg4，audio 需 aac/mp3/ac3/eac3/alac 才能 copy）或 `mkv`（任意编码；推荐 copy）。
- **不做**：字幕轨搬运、烧录、多音轨保留、DRM/直播、HDR tonemap、GPU 编码（v1）。

## 2. API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/avmerge/capabilities` | FFmpeg 状态/版本、容器/模式/质量/分辨率上限、并发、默认值 |
| POST | `/api/avmerge` | 提交（202）：`{video_paths[], audio_paths[], options{}, confirm?}` |
| POST | `/api/avmerge/enumerate` | 目录枚举 `{path, recursive}` → `items[{path,kind,size}]` |
| GET | `/api/avmerge` | 任务列表（status/q/offset/limit） |
| GET | `/api/avmerge/queue` | 队列快照（running/queued/recent + counts + concurrency） |
| GET | `/api/avmerge/{job_id}` | 任务详情（含 steps、verify、options） |
| POST | `/api/avmerge/{job_id}/cancel`、`/retry` | 取消 / 重新排队 |
| DELETE | `/api/avmerge/{job_id}?delete_output=1` | 删除终态任务（可选删产物） |
| POST | `/api/avmerge/bulk` | `{action: cancel_all\|retry_failed\|clear_finished}` |
| GET | `/api/avmerge/export` | CSV（UTF-8 BOM） |

- 全部接口仅管理员；写操作需 CSRF；**无启用开关**（不存在 `avmerge_enabled`）。
- 任务 ID 形如 `am-<12 hex>`（`config.AVMERGE_JOB_ID_RE`）。

## 3. 请求 / 选项

```json
{
  "video_paths": ["/volume1/video/part1.mp4", "/volume1/video/part2.mp4"],
  "audio_paths": ["/volume1/music/bgm.m4a"],
  "options": {
    "container": "mp4",
    "mode": "auto",
    "quality": "standard",
    "max_resolution": 0,
    "output_mode": "alongside",
    "output_dir": "",
    "output_subdir": "merged",
    "name_template": "{name}_merged",
    "delete_source": false
  },
  "confirm": ""
}
```

- `delete_source=true` 必须带 `confirm="DELETE SOURCE"`（可在顶层或 options 里），且只在产物校验通过后执行。
- `output_mode`：`alongside`（第一个视频所在目录）/`fixed`/`subdir`；`fixed`/`subdir` 必须给 `output_dir`。
- `name_template` 支持 `{name}`（第一个视频的文件名，不含扩展名）与 `{date}`；非法字符替换为 `_`。

## 4. 校验与安全

- 每个源文件：`check_path_safety` → 普通文件 → 所在目录 `ShareService.validate_root()` → `is_under` → 拒绝含符号链接的路径。
- 输出目录：必须位于共享文件夹内、可创建、可写；剩余空间 ≥ `avmerge_min_free_gb`。
- 产物：先写 `<目标>.dcpart`，校验（存在、≥1KB、有视频轨、需要时有音频轨、时长与预期差 ≤5s）通过后 `os.replace` 原子改名。
- 覆盖策略 `rename`（默认，`名称 (2).ext`）/`skip`/`overwrite`；源文件与目标同名时强制改名，绝不覆盖源。
- 删除源文件：仅在产物校验通过、且任务带 `confirm="DELETE SOURCE"` 时执行，逐文件 `unlink`，失败只记 warning。

## 5. 状态与存储

- 状态：`queued → running → done|failed|cancelled`；阶段 `probe → video → audio → mux → verify → done`。
- 重启：`running` 任务回到 `queued` 从头开始（FFmpeg 不能断点续）。
- 任务记录存 `avmerge_jobs`（schema v11）：`job_id/status/payload_json/created_epoch/updated_epoch`，进度按 2 秒节流落库。

## 6. 设置键（无启用开关）

| 键 | 默认 | 说明 |
|---|---|---|
| `avmerge_output_dir` | `""` | 固定/子目录模式的输出目录（绝对路径） |
| `avmerge_output_mode` | `alongside` | `alongside/fixed/subdir` |
| `avmerge_output_subdir` | `merged` | 单层目录名 |
| `avmerge_container` | `mp4` | `mp4/mkv` |
| `avmerge_mode` | `auto` | `auto/copy/encode` |
| `avmerge_overwrite` | `rename` | `rename/skip/overwrite` |
| `avmerge_keep_source` | `true` | 默认保留源文件（任务级删除仍需 confirm） |
| `avmerge_name_template` | `{name}_merged` | 输出名模板 |
| `avmerge_concurrency` | `1` | 同时合成任务数 1–2 |
| `avmerge_max_resolution` | `0` | 转码时的分辨率上限（0=原样） |
| `avmerge_quality` | `standard` | `high/standard/small`（CRF/码率） |
| `avmerge_job_limit` | `200` | 保留任务数 20–1000 |
| `avmerge_min_free_gb` | `1` | 输出目录最少剩余空间（GB） |

## 7. 错误码

| 码 | HTTP | 说明 |
|---|---|---|
| `MERGE_INCOMPATIBLE` | 400 | `copy` 模式下素材参数不一致 |
| `SOURCE_INVALID` | 400 | 文件不存在/非普通文件/无对应轨/含符号链接 |
| `OUTPUT_DIR_INVALID` | 400 | 输出目录不可创建或不可写 |
| `OUTPUT_EXISTS` | 409 | `skip` 策略命中同名文件 |
| `MERGE_BUSY` | 409 | 任务未结束却取消/重试/删除 |
| `MERGE_NOT_DONE` | 409 | 产物未完成（预留） |
| `MERGE_TIMEOUT` | 504 | FFmpeg 超时（按素材时长动态计算） |
| `MERGE_FAILED` | 500 | FFmpeg 非 0 退出或产物校验失败 |
| `JOB_LIMIT_REACHED` | 409 | 活动任务超过 `avmerge_job_limit` |
| `INSUFFICIENT_SPACE` | 409 | 输出目录剩余空间不足 |
| `FFMPEG_UNAVAILABLE` | 400 | FFmpeg 不可用 |

## 8. 验收要点

1. 视频/音频两个列表的顺序 = 输出顺序；调换顺序后产物的画面/声音顺序随之变化。
2. 同一参数的两段视频 + 一段短音频：`auto` 选择 `copy`，生成 MP4，时长 = 视频总时长，音频尾部静音补齐。
3. 分辨率不一致的两段视频：`auto` 选择 `encode`，输出可播放；`copy` 模式返回 `MERGE_INCOMPATIBLE`。
4. 音频为空 → 输出无声视频；音频过长 → 截断到视频总时长。
5. 删除源文件未带 `confirm="DELETE SOURCE"` → 400；带了且产物校验通过才删除。
6. `mkv` 容器 + 任意编码可 copy；`mp4` 容器遇到 vp9 自动转码（auto）或报错（copy）。
7. 任务页可见进度/阶段/取消/重试/删除；重启后 running 回到 queued。
