# 功能总览（按功能 / 关键词检索）

> 基线版本 **1.1.0-0068**。本文是「按功能找东西」的总入口：每一节先给**搜索关键词**（中英文同义词，GitHub 搜索、`Ctrl+F` 都能命中），再给入口、接口、设置键、契约文档与源码位置。
> 只看安装与使用：[README](../README.md)；看历史版本：[CHANGELOG](CHANGELOG.md)；看接口细节：各 `*_CONTRACT.md`。

## 0. 关键词速查表

| 想找的功能 | 搜索这些词 | 去哪看 |
|---|---|---|
| 重复文件扫描 | 重复文件 / 去重 / dedupe / duplicate / 精准哈希 / SHA-256 / MD5 | [§1](#1-扫描与判重) · [CONTRACT](CONTRACT.md) |
| 内容相似（图片/文档） | 内容相似 / similarity / MinHash / LSH / dHash / 感知哈希 / 特征 | [§1.2](#12-内容相似模式) · [SIMILARITY_CONTRACT](SIMILARITY_CONTRACT.md) |
| 视频相似 | 视频相似 / video similarity / pHash / DTW / 抽帧 / 硬解 | [§1.3](#13-视频视觉相似) · [VIDEO_CONTRACT](VIDEO_CONTRACT.md) |
| 增量扫描 / 哈希缓存 | 增量 / incremental / hash cache / 跳过重新读盘 | [§1.4](#14-增量扫描与哈希缓存) · [INCREMENTAL_SCAN_CONTRACT](INCREMENTAL_SCAN_CONTRACT.md) |
| 保留规则 | 保留策略 / keep rules / 最晚创建 / 创建时间 / 平局人工 | [§1.5](#15-保留策略与保留规则链) · [CONTRACT §5](CONTRACT.md) |
| 排除规则 / `.ignore` | 排除 / exclude / ignore 文件 / 影视附属文件 / nfo / 海报 | [§1.6](#16-排除规则) · [CONTRACT §6](CONTRACT.md) |
| 预览 / 删除 / 回收站 | 预览 / 播放 / 缩略图 / 删除 / 回收站 / recycle / 永久删除 / 恢复 | [§2](#2-结果预览与删除) · [PREVIEW_PROTECTION_CONTRACT](PREVIEW_PROTECTION_CONTRACT.md) |
| 删除白名单 | 白名单 / protect / 保护后缀 / 附属文件 | [§2.2](#22-删除白名单) |
| 硬链接合并 | 硬链接 / hardlink / 合并 / Btrfs / FICLONE / 释放空间 | [MERGE_TRASH_CONTRACT](MERGE_TRASH_CONTRACT.md) |
| 计划扫描 | 计划 / 定时 / schedule / cron / 负载 | [NOTIFICATION_SCHEDULE_CONTRACT](NOTIFICATION_SCHEDULE_CONTRACT.md) |
| 通知 | 通知 / Webhook / Telegram / 告警 | [NOTIFICATION_SCHEDULE_CONTRACT](NOTIFICATION_SCHEDULE_CONTRACT.md) |
| 字幕 / ASR | 字幕 / subtitle / ASR / 语音识别 / Whisper / OpenAI 兼容 / srt / vtt | [§4](#4-字幕asr--翻译--批量) · [SUBTITLE_CONTRACT](SUBTITLE_CONTRACT.md) |
| 翻译 | 翻译 / translate / LLM / 双语 / 译文 | [SUBTITLE_CONTRACT](SUBTITLE_CONTRACT.md) |
| 批量字幕 / 目录监控 | 批量 / batch / 目录监控 / watcher / 失败重试 | [SUBTITLE_BATCH_CONTRACT](SUBTITLE_BATCH_CONTRACT.md) · [SUBTITLE_ASR_V2_CONTRACT](SUBTITLE_ASR_V2_CONTRACT.md) |
| 下载（直链 / m3u8 / DASH） | 下载 / download / HLS / m3u8 / DASH / mpd / AES-128 / 合并 MP4 | [§5](#5-下载中心) · [DOWNLOAD_CONTRACT](DOWNLOAD_CONTRACT.md) |
| 网页解析 / cURL | 解析 / parse / 播放网页 / cURL / 防盗链 / referer / cookie | [§5.4](#54-播放网页解析与登录态) |
| 订阅下载 | 订阅 / RSS / Atom / 自动下载 / seen | [§5.5](#55-订阅下载) |
| 下载去重 / 自动重试 | 去重跳过 / skip existing / 自动重试 / 退避 / backoff / Retry-After | [§5.6](#56-下载去重与失败重试) |
| 格式转换 | 转换 / convert / 换容器 / remux / 重编码 / transcode / VAAPI / 硬件编码 | [§6](#6-格式转换) · [CONVERT_CONTRACT](CONVERT_CONTRACT.md) |
| 音视频合成 | 合成 / merge / 拼接 / concat / 混流 / mux | [§7](#7-音视频合成) · [AV_MERGE_CONTRACT](AV_MERGE_CONTRACT.md) |
| 任务中心 / 日志 / 体检 | 任务 / 日志 / log / 审计 / audit / 权限体检 / 深链 | [§8](#8-任务与运维) · [CONTRACT §8](CONTRACT.md) |
| 安装 / 部署 / 排错 | 安装 / SPK / DSM / 打包 / 权限 / TLS / 升级 / 日志 | [SPK_PACKAGING](SPK_PACKAGING.md) |
| 存储 / 数据库 | SQLite / schema / WAL / 迁移 / 备份 / checkpoint | [STORAGE_CONTRACT](STORAGE_CONTRACT.md) |
| 安全 / 鉴权 | 登录 / CSRF / Origin / SSRF / 路径校验 / 密钥 | [CONTRACT §2](CONTRACT.md) · [README 安全摘要](../README.md#安全设计摘要) |

---

## 1. 扫描与判重

**关键词**：重复文件、去重、dedupe、duplicate files、精准哈希、SHA-256、MD5、三级漏斗、采样指纹、排除目录、批量扫描。

| 功能 | 入口 / 接口 | 说明 | 文档 |
|---|---|---|---|
| 精准模式（默认） | `POST /api/scans` · 扫描页「精准哈希」 | 大小 → 头/尾/中段快速采样 → 全量 SHA-256/MD5；按 `(size, hash)` 成组，文件名不参与判重 | [CONTRACT §4](CONTRACT.md) |
| 扫描范围 | `POST /api/scans` 的 `roots` / `excludes` | 共享文件夹子树多选、排除目录；枚举上限默认 500,000 文件 | [CONTRACT §4](CONTRACT.md) |
| 并发与限速 | 扫描页选项 / 设置 | 并发 1–4、全局限速 `hash_rate_mbps`（0=不限）、快速采样大小、跳过空文件/隐藏/系统文件 | [CONTRACT §5](CONTRACT.md) |
| 暂停 / 继续 / 停止 | `POST /api/scans/{id}/pause|resume|stop` | 断点持久化；继续时逐条校验 `size/mtime_ns/inode/dev` | [RESUME_CONTRACT](RESUME_CONTRACT.md) |
| 结果分组查询 | `GET /api/scans/{id}/groups` | 分页、搜索、排序、`match_type`、`new_only` | [CONTRACT §7](CONTRACT.md) |
| 导出 | `GET /api/scans/{id}/csv|xlsx` | CSV（BOM）/ Excel | [CONTRACT §7](CONTRACT.md) |

### 1.2 内容相似模式

**关键词**：内容相似、similarity、MinHash、LSH、dHash、感知哈希、特征向量、PDF/DOCX、人工复核。

- 入口：扫描页「内容相似」；`scan_mode=similar`。先跑精准，再对允许类型抽特征（文本 5-gram MinHash、PNG dHash、PDF/DOCX/ODT 文本、可选二进制分片），LSH 取候选，按阈值分组。
- 设置：`similarity_threshold`（默认 85）、`similarity_extensions`、`similarity_exclude_extensions`、`similarity_concurrency`、`similarity_max_file_mb`、`similarity_allow_binary`、`similarity_cache`。
- 门禁：相似组默认不勾选、禁止组级全选，删除需 `confirm_similar=true` + 已复核组 ID。
- 文档：[SIMILARITY_CONTRACT.md](SIMILARITY_CONTRACT.md)。

### 1.3 视频视觉相似

**关键词**：视频相似、video similarity、pHash、DTW、抽帧、硬解、VAAPI、QSV、NVDEC、置信度。

- 入口：设置 / 扫描页视频相似开关（默认关）；`video_similarity_enabled=true`。
- 算法：按总时长抽 5–100 帧 → 32×32 灰度 pHash → DTW 比较；v2 增加中心裁剪、±3 帧时间轴对齐、可选音频签名/ASR 文本签名；置信度分档 `same_source / likely_same / possible`。
- 设置：`video_extensions`、`video_similarity_threshold`、`video_concurrency`、`video_hwaccel`（auto/off/vaapi/qsv/nvdec/videotoolbox）、`video_max_file_mb`、`video_timeout_sec`、`video_cache`、`video_center_crop_enabled`、`video_offset_align_enabled`、`video_audio_signature_enabled`、`video_asr_*`、`ffmpeg_path`。
- 文档：[VIDEO_CONTRACT.md](VIDEO_CONTRACT.md)、[VIDEO_MATCHING_V2_CONTRACT.md](VIDEO_MATCHING_V2_CONTRACT.md)。

### 1.4 增量扫描与哈希缓存

**关键词**：增量扫描、incremental、hash cache、指纹缓存、new_only、changed_since_last_scan。

- 缓存键 `(dev, inode)`，校验 `size + mtime_ns + 算法 + 采样配置`；命中直接复用 quick/full 哈希。
- 结果标记 `new_since_last_scan` / `changed_since_last_scan` / `hash_cached`；`groups?new_only=1` 只看增量。
- 设置：`incremental_scan`（默认 true）；缓存上限 `HASH_CACHE_MAX_ENTRIES=300000`。
- 文档：[INCREMENTAL_SCAN_CONTRACT.md](INCREMENTAL_SCAN_CONTRACT.md)。

### 1.5 保留策略与保留规则链

**关键词**：保留策略、keep_mode、最晚创建、最早创建、指定路径、保留规则链、平局人工。

- `keep_mode`：`latest`（默认）/ `earliest` / `pinned`；`keep_rules` 有序规则链（12 种规则：创建/修改/访问时间、体积、路径优先级、文件名通配符或正则、内嵌字幕/音轨）。
- 创建时间优先 `statx` btime；不支持时整组按 mtime 并标警示。
- 文档：[CONTRACT §5](CONTRACT.md)。

### 1.6 排除规则

**关键词**：排除、exclude、`.ignore`、gitignore、影视附属文件、nfo、海报、sidecar。

- basename 正则 / 通配符 → 目录内 `.ignore`（gitignore 常用子集）→ `excludes` 整棵子树；影视附属文件默认只跳过「与同目录视频同名」的字幕/海报/nfo。
- 设置：`exclude_name_regex`、`exclude_name_glob`、`ignore_file_enabled`、`media_sidecar_mode=skip|include`。
- 文档：[INCREMENTAL_SCAN_CONTRACT.md](INCREMENTAL_SCAN_CONTRACT.md)、[CONTRACT §6](CONTRACT.md)。

---

## 2. 结果预览与删除

**关键词**：预览、preview、缩略图、播放、对比、删除、二次确认、回收站、recycle、恢复、永久删除、硬链接合并。

| 功能 | 入口 / 接口 | 说明 |
|---|---|---|
| 分组报告 | 扫描结果页 | 路径、大小、哈希、时间、匹配类型、相似度；保留项高亮 |
| 预览增强 | `GET /api/preview` | 视频缩略图条、媒体信息、并排对比；Range 流式播放 |
| 删除计划 | `POST /api/scans/{id}/delete-plan` | 生成计划（保留/删除清单），不执行 |
| 执行删除 | `POST /api/scans/{id}/delete` | 服务端逐文件重校验；默认移入回收站；两次确认 |
| 套件回收站 | `GET /api/trash`、`restore`、`purge` | 独立回收站 `#duplicate-cleaner-trash`；整组恢复；永久删除需输入口令 |
| 硬链接合并 | `POST /api/scans/{id}/merge` | 精准组、同文件系统；可「恢复为独立副本」（Btrfs FICLONE） |

### 2.2 删除白名单

**关键词**：白名单、protect、保护后缀、保护目录、文件名正则、附属文件。

- 设置：`protect_enabled`、`protect_extensions`、`protect_dirs`、`protect_name_regex`、`protect_sidecar_files`；命中项不可勾选，删除计划与执行前各再校验一次。

---

## 3. 自动化

**关键词**：计划扫描、定时、schedule、Webhook、Telegram、通知、告警。

- 计划扫描：`scheduled_scan_enabled`、`scheduled_scan_rules`（`daily 03:30`、`weekly mon,wed 04:00`、`interval 360`）、`scheduled_scan_roots`、`scheduled_scan_max_load`。
- 通知：`notify_enabled`、`notify_channels`（webhook/telegram）、`notify_events`、凭据键；失败/完成/释放空间阈值触发；DSM 系统通知自 v0060 移除。
- 文档：[NOTIFICATION_SCHEDULE_CONTRACT.md](NOTIFICATION_SCHEDULE_CONTRACT.md)。

---

## 4. 字幕（ASR / 翻译 / 批量）

**关键词**：字幕、subtitle、ASR、语音识别、Whisper、WhisperLiveKit、OpenAI 兼容、LLM、翻译、双语、srt、vtt、批量、目录监控、失败重试、音轨选择。

| 功能 | 入口 / 接口 | 说明 | 文档 |
|---|---|---|---|
| 单文件字幕 | `POST /api/subtitles` | 选音轨 → 抽音频分块 → ASR → SRT（可写 VTT） | [SUBTITLE_CONTRACT](SUBTITLE_CONTRACT.md) |
| 翻译 | `POST /api/subtitles/{id}/translate` | 默认译文；LLM 翻译；双语输出 | 同上 |
| 批量 / 目录 | `POST /api/subtitles/batch` | 目录递归、批次组、补翻译、进度、取消剩余、强制删除 | [SUBTITLE_BATCH_CONTRACT](SUBTITLE_BATCH_CONTRACT.md) |
| 队列与并发 | `GET /api/subtitles/queue`、任务页设置 | 并发 1–4（默认 1）、已完成保留上限 20–1000（默认 200） | 同上 §23 |
| 失败分类与重试 | 任务明细 | 分类 `timeout/network/provider/internal` 等；自动重试次数 0–3 | [SUBTITLE_ASR_V2_CONTRACT](SUBTITLE_ASR_V2_CONTRACT.md) |
| 音轨 / 负载 / 监控 | `subtitle_audio_*`、`subtitle_max_load`、`subtitle_watch_*` | 指定音轨、目录监控（轮询）、负载阈值 | 同上 |
| 连接测试 | `POST /api/subtitles/test` | ASR/LLM 连通性与模型列表 | [SUBTITLE_CONTRACT](SUBTITLE_CONTRACT.md) |

- 关键设置：`subtitle_enabled`、`subtitle_asr_base_url/model/api_key/timeout_sec/chunk_sec`、`subtitle_llm_*`、`subtitle_concurrency`、`subtitle_job_limit`、`subtitle_translate_enabled`、`subtitle_skip_existing`、`subtitle_output_mode`、`subtitle_write_vtt`。

---

## 5. 下载中心

**关键词**：下载、download、HLS、m3u8、DASH、mpd、AES-128、合并 MP4、分片、断点续传、解析、cURL、RSS、订阅、限速、命名模板、自动重试、退避、清空已完成。

| 功能 | 入口 / 接口 | 说明 | 文档 |
|---|---|---|---|
| 直链下载 | `POST /api/downloads` | HTTP Range 断点续传 | [DOWNLOAD_CONTRACT](DOWNLOAD_CONTRACT.md) |
| HLS / DASH | 同上 | TS/fMP4 多码率、AES-128 本地解密、音视频轨合并为 MP4 | 同上 |
| 队列与控制 | `GET /api/downloads/queue`、`{id}/pause|resume|cancel|retry` | 并发 1–4（默认 2）、优先级、批量操作、CSV 导出 | 同上 |
| 清空已完成 / 失败 | `POST /api/downloads/bulk`（`action=clear_finished`） | 删除已完成或失败/已取消任务记录，**保留已下载文件** | 同上 §18 |
| 本地化目录 | 任务设置 | 输出目录必须在共享文件夹内；子目录模板；`.dcpart` 原子改名 | 同上 |
| 命名模板 | `download_name_template` | `{title}{site}{date}{height}{season}{episode}…` | 同上 §15.4 |
| 分时限速 | `download_offpeak_*` | 低谷窗口换档，支持跨零点 | 同上 §15.3 |

### 5.4 播放网页解析与登录态

**关键词**：网页解析、page parse、m3u8 提取、防盗链、referer、cookie、cURL、bookmarklet。

- `POST /api/downloads/parse`：静态提取 `<video>/<source>/data-*`、JSON-LD、播放器脚本中的 m3u8/直链；可选粘贴页面源码或浏览器 Copy as cURL（`parse_session` 15 分钟，仅用于随后提交）。
- 文档：[DOWNLOAD_CONTRACT §14](DOWNLOAD_CONTRACT.md)。

### 5.5 订阅下载

**关键词**：订阅、RSS、Atom、自动下载、定时检查、seen 去重。

- `GET/POST /api/downloads/subscriptions`、`{sub_id}/check`；同一订阅最近 200 条地址不重复入队；总开关 `download_subscribe_enabled`。
- 文档：[DOWNLOAD_CONTRACT §16](DOWNLOAD_CONTRACT.md)。

### 5.6 下载去重与失败重试

**关键词**：下载前去重、skip existing、自动重试、指数退避、backoff、Retry-After、等待重试、清空已完成、clear finished。

- 去重：目标文件已存在且体积一致 → 任务直接完成（`skipped=true`），不消耗流量；`download_skip_existing` 默认开。
- 退避重试：`min(cap, base×factor^(n-1))` + 0–20% 正向抖动；默认 60s → 180s → 540s → 1620s → 1800s（封顶），最多 5 次；只重试 5xx/408/425/429/断网/分片/内部错误，4xx 立即失败；支持 `Retry-After`。
- 清空已完成：下载页「下载任务」工具栏点「清空已完成」→ 二次确认后删除已完成任务记录（不删除已下载文件）；「清空失败/取消」同理清失败与已取消任务；接口 `POST /api/downloads/bulk` + `{"action":"clear_finished","scope":"done"}`，范围另支持 `failed/cancelled/failed_cancelled/finished`。
- 设置：`download_auto_retry`、`download_auto_retry_max`、`download_auto_retry_base_sec`、`download_auto_retry_factor`、`download_auto_retry_cap_sec`。
- 文档：[DOWNLOAD_CONTRACT §15.1–15.2](DOWNLOAD_CONTRACT.md)、[§18](DOWNLOAD_CONTRACT.md)。

---

## 6. 格式转换

**关键词**：格式转换、convert、转码、transcode、换容器、remux、重编码、预设、VAAPI、QSV、NVDEC、硬件编码、批量分批、覆盖进回收站、删除源文件。

- 入口：转换页；`POST /api/convert`（单条）、`/api/convert/bulk`、`GET /api/convert/queue`；`GET /api/convert/capabilities`。
- 16 个预设：视频互转（MP4 H.264/H.265、MKV、WEBM VP9、MOV、TS、AVI）、音频互转（MP3/AAC/FLAC/WAV/OGG/OPUS）、仅换封装（copy，秒级无损）。
- 队列：并发 1–2（默认 1）、实时进度/速度/fps/ETA、取消/重试/删除、重启回队列；单请求 ≤200 个自动分批，活动任务上限 2000。
- 覆盖/删源：默认 `convert_overwrite=trash`（同名旧产物移入套件回收站，可恢复）、`convert_delete_source=true`（成功后删源）；源后缀可多选（`convert_source_extensions`）。
- 硬件：`convert_hwaccel=auto|off`；VAAPI 失败自动回退 CPU 并标 `hw_fallback`。
- 文档：[CONVERT_CONTRACT.md](CONVERT_CONTRACT.md)、[ARCHITECTURE §22](ARCHITECTURE.md)。

---

## 7. 音视频合成

**关键词**：合成、merge、拼接、concat、混流、mux、有序列表、时长对齐。

- 入口：合成页；`POST /api/merge`、`GET /api/merge/queue`。
- 按选择顺序拼接视频与音频；`copy`（同参数，秒级）或 `encode`（统一 scale/fps/48k AAC）；以视频时长为准，音频不足静音补齐/超出截断；`mp4/mkv` 可选。
- 设置：`avmerge_overwrite`（默认 rename）、`avmerge_keep_source`、默认容器等。
- 文档：[AV_MERGE_CONTRACT.md](AV_MERGE_CONTRACT.md)。

---

## 8. 任务与运维

**关键词**：任务中心（已退役）、内嵌标签、深链、日志、审计、检索、导出、权限体检、批量操作。

- 导航：扫描 / 字幕 / 下载 / 转换 / 合成；设置右上角。深链 `#/scan?tab=history`、`#/subtitles?tab=jobs`、`#/settings?tab=logs`。
- 日志与审计：`GET /api/logs`、`/api/logs/search`、`/api/logs/export`；支持按任务 ID/关键字检索、保留天数、勾选删除。
- 权限体检：`GET /api/permissions`（只读检测 + DSM 操作指引，不自动提权）。
- 通知与计划扫描见 [§3](#3-自动化)。
- 文档：[CONTRACT §8–§9](CONTRACT.md)、[ARCHITECTURE](ARCHITECTURE.md)。

---

## 9. 平台、部署与数据

**关键词**：SPK、DSM 7、套件、安装、升级、TLS、SQLite、schema、备份、WAL、迁移、开发模式。

- 安装/构建/排错：[SPK_PACKAGING.md](SPK_PACKAGING.md)；版本历史：[CHANGELOG.md](CHANGELOG.md)。
- 数据：SQLite schema **v11**（`<var>/dupcleaner.sqlite3`），自动迁移不支持降级；设置/审计/日志是文件；备份整个 `var/`。
- 文档：[STORAGE_CONTRACT.md](STORAGE_CONTRACT.md)、[ARCHITECTURE.md](ARCHITECTURE.md)。
- 安全：DSM 管理员登录、HttpOnly Cookie + CSRF、Origin 校验、路径白名单、SSRF 防护、密钥只写不读。见 [CONTRACT §2](CONTRACT.md) 与 [README 安全设计摘要](../README.md#安全设计摘要)。

---

## 10. 明确不做（避免误期待）

- 不判断业务语义：只保证哈希一致/特征相似，删除永远需要人工确认。
- 不绕过 DRM（Widevine/FairPlay/PlayReady/SAMPLE-AES）；不支持直播/滚动播放列表。
- 不内置浏览器、不执行 JS：需 JS 渲染/云flare 挑战的页面只能靠粘贴源码或 cURL。
- 不做跨文件系统硬链接；不做云同步、不上传文件（字幕/翻译除外，且发往你配置的服务）。
- 视频相似不承诺识别"不同剪辑版本"、剧情语义、剪辑乱序。
- 不支持 DSM 系统通知（`sysnotify` 仅 Synology 自家套件可用）。

> 更细的边界见 [README「已知限制」](../README.md#已知限制)。
