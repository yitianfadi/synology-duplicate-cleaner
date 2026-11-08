# 预览增强与删除白名单契约（v0044）

> 版本：`1.1.0-0044` · SQLite schema 仍 **v8**（本版无 DB 迁移）
> 关联：[CONTRACT.md](CONTRACT.md) §6/§8、[ARCHITECTURE.md](ARCHITECTURE.md) §14、
> [REQUIREMENTS_V2_ASSESSMENT.md](REQUIREMENTS_V2_ASSESSMENT.md) §2.4/2.5

## 0. 一句话

- **预览增强**：结果页视频可看**缩略图条**（点缩略图跳到对应时间点播放）、可读**媒体信息**
  （时长/分辨率/音轨数/字幕轨数）、可**并排对比同组两个文件**（同步播放可开关），用于区分
  “不同压制版本 / 剪辑版本 / 花絮”。
- **删除白名单**：可保护**后缀 / 目录 / 文件名正则 / 视频附属文件**；命中的文件在结果页
  **不可勾选**，且**删除计划与删除执行两个阶段都会按当前设置重新校验**（不只是 UI 限制）。

## 1. 预览增强

### 1.1 缩略图

- `GET /api/preview/thumb?path=<绝对路径>&t=<秒>&w=<宽=320>` → `image/jpeg`。
- 实现：FFmpeg 抽 1 帧（`-ss` 定位 + `-frames:v 1`）后缩放为宽度 `w` 的 JPEG；
  输出落 `<var>/cache/thumbs/<sha1(path|size|mtime_ns|t|w)>.jpg`，同一文件同一时间点**命中缓存不再解码**。
- 缓存上限：`THUMB_MAX_ENTRIES=3000` 条 / `THUMB_MAX_BYTES=300MB`，写入后按 mtime LRU 裁剪。
- 并发：进程内信号量 2；取不到（20s）返回 `429 PREVIEW_BUSY`。
- 只允许共享文件夹内普通文件（与 `/api/preview` 相同的路径安全规则）；非视频 → `415 PREVIEW_UNSUPPORTED`。
- 响应头：`Cache-Control: private, max-age=3600`、`X-Thumb-Cache: hit|miss`、`X-Thumb-Timestamp`。
- 失败：FFmpeg 解码失败 → `415`；超时 → `504 PROVIDER_TIMEOUT`；FFmpeg 缺失 → `400 FFMPEG_UNAVAILABLE`。

### 1.2 媒体信息

- `GET /api/preview/media?path=…` → 
  ```json
  {"ok":true,"path":"…","name":"movie.mkv","size":123456,
   "media":{"duration":7560.5,"size":123456,"has_audio":true,"has_subtitle":true,
            "video_stream_count":1,"audio_stream_count":2,"subtitle_stream_count":1,
            "video":{"stream_index":0,"codec":"h264 (High)","width":1920,"height":1080,"language":""},
            "audio_streams":[{"stream_index":1,"codec":"aac (LC)","language":"jpn","channels":"stereo","default":true,"title":""}],
            "subtitle_streams":[…]}}
  ```
- 实现：`ffmpeg -i` 的 stderr 文本解析（不依赖 ffprobe、不读取视频内容），内存缓存 200 条
  （按 path+size+mtime_ns 区分）。**只返回流元数据，不返回任何转写/剧情内容。**
- 非音视频 → `415`。

### 1.3 缩略图时间点清单

- `GET /api/preview/thumbs?path=…&count=6&w=320` → 
  ```json
  {"ok":true,"path":"…","name":"…","size":123,
   "media":{...同上...},
   "width":320,
   "timestamps":[360.2,720.4,…],
   "thumbs":[{"t":360.2,"url":"/api/preview/thumb?path=…&t=360.20&w=320"}]}
  ```
- 时间点在时长内均匀取 `count` 个（默认 6，最多 12）；未知时长时用固定候选（5/30/60/180/300/600…）兜底。
- 该接口**只给时间点与 URL，不预渲染**；前端用 `<img>` 按需加载（服务端有并发与缓存控制）。

### 1.4 前端行为（app.js）

- 视频预览从“下载整个视频成 blob 再播放”改为**直接 URL + Range 流式播放**（大文件可立即拖动/片段播放）。
  为拿到类型，先发一次 `Range: bytes=0-0` 探测请求，再决定是否重新取全文（图片/文本/PDF）。
- 视频预览弹窗增加：媒体信息行、6 张缩略图（点击 `currentTime = t` 并播放）、
  「与同组文件并排对比」按钮（同组 ≥2 个文件时出现）。
- 并排对比弹窗：左右各一个 `<video>` + 文件下拉（同组文件）+ 媒体信息；
  「同步播放」勾选后两侧 play/pause/seek 互相带动（0.5s 容差避免抖动），另有「对齐到左侧时间点」。
- 已知限制：浏览器解码器不支持 HEVC/MKV/DTS 等编码时无法播放（缩略图仍可见）；不提供在线转码。

## 2. 删除白名单（附属文件保护）

### 2.1 规则（命中任一即受保护）

| 顺序 | 规则 | 设置键 | 说明 |
|---|---|---|---|
| 1 | 目录 | `protect_dirs` | 绝对路径；等于或位于该前缀下（含子目录） |
| 2 | 后缀 | `protect_extensions` | 默认 `.srt .ass .ssa .sub .vtt .idx .sup .smi .nfo .sfv .md5 .sha256`（**不含图片**） |
| 3 | 文件名正则 | `protect_name_regex` | 对 basename，IGNORECASE，≤50 条 |
| 4 | 视频附属文件 | `protect_sidecar_files`（默认开） | 同目录存在同源视频的附属文件：同名不同后缀、`X.<lang>.srt`、`X-poster.jpg`、常见固定名（poster/folder/fanart/backdrop/banner/logo/clearlogo/landscape/thumb/movie.nfo/tvshow.nfo/season.nfo 等） |

- 总开关 `protect_enabled`（默认开）；关闭后所有规则失效。
- **视频文件本身不是自己的附属文件**（`.mkv` 不会保护自身）；照片库里没有同名视频的
  `jpg/png` **不受影响**（仍参与判重），海报只在“同目录有同源视频”时被保护。
- 目录内视频 stem 按目录缓存（一次 `scandir`），批量计划/删除不会反复列目录。

### 2.2 生效路径（三层）

1. **扫描期标记**：`file_info.protected` / `protect_reason` / `protect_message`；组级 `protected_files`；
   `stats.protected_files` / `stats.protected_groups`。受保护文件 `default_selected=false`，
   **不计入** `selectable_duplicate_files`（“全选待删除副本”不会包含它们）。
2. **删除计划期**：`POST /api/scans/{id}/delete-plan` 用**当前设置**重新计算，命中者进
   `skipped[{reason:"PROTECTED", protect_source:"extension|directory|name|sidecar", message}]`；
   计划字段 `protected_skipped`；若计划内没有任何可删文件 → `400 PLAN_INVALID`（details 带 PROTECTED 原因）。
3. **删除执行期**：`_delete_one` 在重校验之后、真正 `rename/unlink` 之前**再算一次**；
   命中 → 该文件 `status=skipped, reason=PROTECTED`，文件保持不动（计划生成后改设置/改名同样被拦住）。

### 2.3 设置键与校验

| key | 类型 | 默认 | 校验 |
|---|---|---|---|
| protect_enabled | bool | true | — |
| protect_extensions | string[] | 见上 | 每条以 `.` 开头、2–16 字符、最多 200 条 |
| protect_dirs | string[] | [] | 每条必须是**绝对路径**、≤1024 字符、最多 200 条 |
| protect_name_regex | string[] | [] | 保存时 `re.compile` 校验失败 400、≤50 条、≤200 字符 |
| protect_sidecar_files | bool | true | — |

### 2.4 安全性说明

- 白名单是**删除侧终局防线**，不是“保留策略”：受保护文件仍会出现在结果里并参与分组，
  只是不能被选中删除；保留项仍由 `keep_mode`/`keep_rules` 决定。
- 计划生成后、执行前设置/文件名变化 → 执行期复查会跳过（宁可少删）。
- 关闭保护或删除规则后，历史受保护标记不影响新的计划（计划/执行都按当时设置重算）。

## 3. 接口 / 能力清单

- `GET /api/preview/thumb`、`GET /api/preview/media`、`GET /api/preview/thumbs`（登录用户，路径安全同预览）。
- `GET /api/diagnostics` 新增 `thumbnail_cache`（条目/大小/上限/路径）。
- `GET /api/meta` 新增 `preview`（缩略图宽/数量/缓存上限/同步容差）与 `scan.protect_reasons`（中文标签）。
- 删除计划响应新增 `protected_skipped`；`skipped[].protect_source`。

## 4. 验收

- unittest：`tests/test_v0044_preview.py`（媒体流解析、缩略图渲染/缓存/裁剪、媒体信息、时间点清单、
  三个接口、415/403）与 `tests/test_v0044_protection.py`（规则、设置校验、扫描标记、
  计划跳过、全保护拒绝、执行期复查、附属文件保护）。
- e2e：`PRT-01..04`、`THUMB-01..04`（无 ffmpeg 时 THUMB 记 UNVERIFIED）。
- 静态：`BE-PREVIEW-THUMB`、`BE-PROTECT`、`FE-PREVIEW-THUMB`、`FE-PROTECT-UI`、路由/端点覆盖。
- 真机：4K/MKV 缩略图生成耗时与失败率；并排对比在 DSM 内置浏览器可用性；
  白名单对真实媒体库（srt/海报/nfo）的保护效果；受保护目录对照片库的影响。
