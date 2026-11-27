# 下载中心契约（v0051 / v0052）

> 适用范围：`src/backend/dupcleaner/downloads.py`、`hls.py`、`pageparse.py`、`httpapi.py` 的 `/api/downloads*`、`src/web/js/download-page.js` 与设置页「下载」分组。
> 本文是下载功能的**唯一事实来源**：接口、字段、错误码、默认值、落盘与清理规则以此为准；README 只做概览。

## 1. 范围

把网络上的**点播**视频/文件保存到 NAS 的共享目录：

- 直链（mp4/mkv/zip/……任意 http(s) 文件），支持 HTTP Range 断点续传；
- HLS（`.m3u8`）：解析主播放列表 → 选清晰度 → 并发下载分片 → 本地化播放列表 → FFmpeg 无损封装为 MP4；
- 批量：一次提交多条地址，自动成组，可整组暂停/继续/取消/重试；
- **播放网页解析（v0052）**：给一个网页地址（或粘贴网页源码 / 浏览器 cURL），静态提取出里面的 m3u8 / 直链候选；
- 完成后可选联动：自动入队扫描该目录 / 自动提交字幕任务。

## 2. 支持矩阵

| 输入 | 处理方式 | 结果 |
|---|---|---|
| `http(s)` 直链 | 单流下载 + `Range` 续传 | 保留原始后缀（`.mp4/.mkv/...`） |
| `.m3u8` 媒体列表，无加密（TS） | Python 并发拉分片 → `ffmpeg -f concat -c copy` | `.mp4` |
| `.m3u8`，`#EXT-X-KEY:METHOD=AES-128` | 分片 + `key.bin` 落地 → 本地化 m3u8 → `ffmpeg -allowed_extensions ALL -c copy`（FFmpeg 原生 AES 解密） | `.mp4` |
| `.m3u8`，fMP4（`#EXT-X-MAP`） | init 段一并本地化 | `.mp4` |
| 主播放列表（多码率） | 按 `download_max_height` 选变体（0=最高） | `.mp4` |
| 主播放列表含独立音轨（`#EXT-X-MEDIA TYPE=AUDIO`） | 视频/音频两条播放列表分别本地化，`-map 0:v:0 -map 1:a:0?` 合并 | `.mp4` |
| `#EXT-X-BYTERANGE` 分片 | 同一 URI 多次按字节范围拉取 | `.mp4` |
| `SAMPLE-AES` / `CENC` / Widevine / FairPlay | **不做任何绕过** | `DRM_UNSUPPORTED` |
| 直播 / 滚动列表（无 `#EXT-X-ENDLIST`） | 拒绝 | `LIVE_UNSUPPORTED` |
| `file://`、`ftp://`、`data:` | 拒绝 | `URL_UNSUPPORTED_SCHEME` |
| 内网/回环地址 | 默认拒绝（SSRF） | `URL_PRIVATE_ADDRESS` |
| 播放网页 URL / 源码 / cURL | 静态提取（不执行 JS） | 候选列表（见 §14） |

## 3. API

全部接口**仅管理员**；写操作需要 `X-CSRF-Token`。v0058 起下载是独立功能，**不再有启用开关**，`download_enabled` 键仅为兼容保留、不再生效（旧配置里的 `false` 也不会再 403）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/downloads` | 创建任务（202）。body：`{urls: [], group_title?, options?, titles?}` |
| POST | `/api/downloads/parse` | 解析播放网页（v0052）。body：`{url}` / `{html}` / `{curl}`，返回候选列表（不建任务） |
| GET | `/api/downloads` | 列表：`?status=queued,running&group_id=&offset=&limit=&q=` |
| GET | `/api/downloads/queue` | 队列快照：`active/queued/paused/recent` + `counts` + `concurrency` |
| GET | `/api/downloads/{job_id}` | 任务详情（含 `timeline.steps` 与 `options`，Cookie 只给 `cookie_set`） |
| POST | `/api/downloads/{job_id}/{pause\|resume\|cancel\|retry}` | 单任务控制 |
| DELETE | `/api/downloads/{job_id}?delete_files=true` | 删除终态任务（可选删除产物） |
| GET | `/api/downloads/{job_id}/file` | 下载产物（`Content-Disposition: attachment`） |
| GET | `/api/downloads/groups` | 批量组列表 |
| GET | `/api/downloads/groups/{group_id}` | 组详情（含 `counts/progress/is_done`） |
| POST | `/api/downloads/groups/{group_id}/{pause\|resume\|cancel\|retry}` | 组级控制（跳过不可执行项） |

### 3.1 创建请求

```json
{
  "urls": ["https://example.com/v/index.m3u8", "https://example.com/a.mp4"],
  "group_title": "可选组名",
  "titles": {
    "https://example.com/v/index.m3u8": "解析得到的页面标题"
  },
  "options": {
    "title": "", "output_dir": "/volume2/video/downloads",
    "subdir_mode": "none|site|date|site_date|custom",
    "subdir_template": "{site}/{date}",
    "segment_concurrency": 4, "retry": 3, "timeout_sec": 30,
    "rate_limit_kbps": 0, "max_height": 0, "min_free_gb": 20,
    "verify_tolerance_sec": 5, "user_agent": "", "referer": "",
    "cookie": "", "proxy": "", "overwrite": false, "keep_temp": false
  }
}
```

- `urls` 去重、上限 `DOWNLOAD_BATCH_MAX_ITEMS=200`；
- `titles`（v0064，可选）：`{地址: 页面标题}`，只认本次 `urls` 里的地址；标题先走 `pageparse.clean_title` 清洗（去「正在播放」前缀与站名后缀）、再过文件名非法字符过滤，写入任务 `title`，供 `{title}` 模板与文件名使用。缺失/对不上/清洗后为空都回退原行为（URL 名）；
- 未提供的字段取**提交时**的设置快照（之后改设置不影响已排队任务）；
- `output_dir` 必须是共享文件夹内的目录；留空用 `download_output_dir`，两者都空 → `OUTPUT_DIR_INVALID`。

### 3.2 任务对象（节选）

`job_id / group_id / url / title / kind(hls|direct) / status / stage / stage_label / message / progress / variant / encryption / segments_total / segments_done / bytes_total / bytes_done / speed_bps / eta_seconds / duration_sec / output_dir / output_name / output_path / output_size / error_code / error_message / attempts / verify{duration,expected,tolerance,ok,detail} / temp_kept / created_at / started_at / finished_at`。

详情额外返回 `timeline.steps[]`（`resolve/download/merge/verify`，字段与字幕任务同构）与 `options`。

## 4. HLS 解析规则

支持标签：`EXT-X-VERSION`、`EXT-X-TARGETDURATION`、`EXT-X-MEDIA-SEQUENCE`、`EXT-X-PLAYLIST-TYPE`、`EXT-X-ENDLIST`、`EXTINF`、`EXT-X-KEY`、`EXT-X-MAP`、`EXT-X-BYTERANGE`、`EXT-X-DISCONTINUITY`、`EXT-X-STREAM-INF`、`EXT-X-MEDIA`、`EXT-X-SESSION-KEY`（仅做 DRM 校验）。其余标签忽略。

- URI 一律 `urljoin` 成绝对地址，相对路径按所在播放列表解析；
- `EXT-X-BYTERANGE` 省略 offset 时接上一段结束位置；
- `EXT-X-KEY:METHOD=NONE` 清除当前密钥；密钥轮换按分片记录；
- 本地化文件名固定：`seg_%06d<ext>`、`init_%04d<ext>`、`key_%04d.bin`，重写后的 `local.m3u8` 只引用本地文件名（含 `IV=`），交给 FFmpeg 解密与封装；
- 媒体后缀白名单：`.ts/.m4s/.mp4/.m4v/.m4a/.aac/.cmfv/.cmfa/.mp3/.ac3/.ec3`，其余按 `.ts`。

## 5. 数据模型（引入时 schema v9；当前全局 schema v11）

```sql
download_jobs(job_id PK, group_id, status, payload_json, created_epoch, updated_epoch)
download_groups(group_id PK, payload_json, created_epoch, updated_epoch)
-- 索引：created_epoch / (status, created_epoch) / (group_id, created_epoch)
```

- 只存汇总与状态；分片进度以磁盘文件是否存在为准（重启后天然续传），不写 `download_segments` 行；
- 状态机：`queued → running → (done|failed|cancelled)`，`running ⇄ paused`；
- 阶段：`resolve → download → merge → verify → done`（直链无 `merge`/`verify`）；
- 重启恢复：`running` 任务回到 `queued` 重新排队；`paused` 保持；临时目录仍在即可续传；
- 进度落库节流：`DOWNLOAD_PROGRESS_FLUSH_SEC=2s`，状态切换强制落库。

## 6. 设置键（默认值 / 校验）

| 键 | 默认 | 范围 |
|---|---|---|
| `download_enabled` | `true` | 兼容键，v0058 起不再生效（功能恒可用） |
| `download_output_dir` | `""` | 空或绝对路径（提交时校验共享目录归属） |
| `download_subdir_mode` | `none` | `none/site/date/site_date/custom` |
| `download_subdir_template` | `{site}/{date}` | ≤200 字符、相对、占位符 `{site}{date}{yyyy}{mm}{dd}{hour}{min}{title}` |
| `download_concurrency` | `2` | 1–4（任务级并发，超出排队） |
| `download_segment_concurrency` | `4` | 1–16 |
| `download_retry` | `3` | 0–10（4xx 除 408/429 不重试） |
| `download_timeout_sec` | `30` | 5–300 |
| `download_rate_limit_kbps` | `0` | 0–1048576（按运行中任务均分） |
| `download_max_height` | `0` | 0–4320 |
| `download_user_agent` | 内置 Chrome UA | ≤512 字符 |
| `download_referer` | `""` | 空或 http(s) URL（不带凭据） |
| `download_cookie` | `""` | **密钥键**，只写不读 |
| `download_proxy` | `""` | `http/https/socks5`，不带账号密码 |
| `download_allow_private` | `false` | bool（SSRF 放行开关） |
| `download_keep_temp` | `false` | bool |
| `download_min_free_gb` | `20` | 0–102400 |
| `download_verify_tolerance_sec` | `5` | 0–600（0=不校验） |
| `download_job_limit` | `500` | 10–5000（活动任务上限 + 终态 LRU） |
| `download_auto_scan` | `false` | bool（同目录 10 分钟去抖） |
| `download_auto_subtitle` | `false` | bool（需 `subtitle_enabled`） |

## 7. 错误码

`DOWNLOAD_DISABLED(403)`、`URL_INVALID(400)`、`URL_UNSUPPORTED_SCHEME(400)`、`URL_PRIVATE_ADDRESS(403)`、`URL_TOO_LONG(400)`、`HTTP_STATUS(502)`、`PLAYLIST_INVALID(400)`、`DRM_UNSUPPORTED(415)`、`LIVE_UNSUPPORTED(400)`、`SEGMENT_FAILED(502)`、`MERGE_FAILED(500)`、`OUTPUT_DIR_INVALID(400)`、`OUTPUT_EXISTS(409)`、`INSUFFICIENT_SPACE(409)`、`JOB_LIMIT_REACHED(409)`、`DOWNLOAD_BUSY(409)`、`DOWNLOAD_NOT_DONE(409)`、`FFMPEG_UNAVAILABLE(400)`、`INTERNAL_ERROR(500)`。

错误文本经过清洗：URL 查询串替换为 `?***`、Cookie 明文抹为 `***`，不写入任务详情与日志。

## 8. 落盘与清理

- 临时目录：`<输出目录>/.dc-download-<job_id>/`（**与最终文件同卷**，避免跨卷搬运）；
- 分片先写 `*.dcpart` 再 `os.replace`；合并产物同样先 `.dcpart` 后改名；
- 完成：删除临时目录（`download_keep_temp=true` 时保留并置 `temp_kept=true`）；
- 失败/取消：保留临时目录便于重试续传；`cancel` 排队中任务会立即清理；
- 终态任务超过 `download_job_limit` 按完成时间 LRU 清理 DB 行与临时目录（**绝不删除产物文件**）；
- `.dcpart` 与临时目录都在共享目录内，DSM 文件列表可见（点开头，默认隐藏）。

## 9. 前端

- 主导航新增「下载」（`扫描 / 字幕 / 下载 / 任务中心`），`#/download` 可直达；
- 页面：地址输入（每行一个）+ 目录选择（复用共享文件夹选择器，单选）+ 本次任务高级选项（清晰度上限 / 子目录 / 覆盖 / 保留临时）+ 任务列表；
- 任务行：状态徽标、阶段、`分片 x/y`、速度、ETA、错误原因、产物路径、操作按钮（暂停/继续/取消/重试/下载文件/删除）；
- 轮询 2s，离开页面停止；不使用 SSE；
- 进度条宽度用 `node.style.width`（禁止 `el(..., {style})`），全程无 `innerHTML`。

## 10. 安全

- SSRF：解析宿主名后拒绝 private/loopback/link-local/reserved/multicast/unspecified 地址；`download_allow_private` 显式放开；
- URL 内不允许 `user:pass@`（凭据走 Cookie 设置）；
- 输出目录必须通过 `check_path_safety` + `ShareService.share_root`，禁止系统/隐藏目录与卷根；
- Cookie 走 `SECRET_SETTING_KEYS`：GET 不回传、日志/审计不写入、任务详情只给 `cookie_set`；
- 合并命令不经过 shell（参数数组），FFmpeg 输出只取尾部 400 字符并清洗查询串。

## 11. 非目标

DRM 解密、直播录制、内置浏览器/JS 渲染、BT/磁力、站点专用提取器（YouTube 等）、转码（只做 `-c copy`；失败时仅尝试音频转 AAC 一次）。

## 12. 与扫描/字幕联动

- `download_auto_scan`：任务完成后对产物目录 `start_scan`，同一目录 600 秒内最多一次，用户名为 `downloader`；
- `download_auto_subtitle`：任务完成后以 `create_batch({paths:[产物]})` 提交字幕，`translate` 跟随是否配置了 LLM API Key（v0060 起不再看已废弃的 `subtitle_translate_enabled`）；未启用字幕只记日志；
- 两个联动都只做「尽力而为」：失败只写日志，不影响下载任务状态。

## 13. 测试与验收

- 单测 `tests/test_v0051_downloads.py`：播放列表全 tag 组合（master/media/AES/fMP4/BYTERANGE/直播/DRM）、URL 与命名清洗、直链与 HLS 全链路（本地假 CDN，含 Range 续传）、失败保留临时目录 + 重试、任务上限、组总览；
- 单测 `tests/test_v0052_pageparse.py`：网页解析与 cURL 解析（详见 §14.8）；
- 静态检查新增 `BE-DOWNLOAD-*`、`FE-DOWNLOAD-*`、端点与设置键覆盖；
- e2e 见 `qa/e2e_test.py` 的 DL-00…DL-07（本地假 CDN，直链/直播拒绝/SSRF/网页解析）。

## 14. 播放网页解析（v0052）

### 14.1 输入

`POST /api/downloads/parse`，三种形态（可同时给，按以下优先级）：

1. `{"html": "…"}` —— 直接解析粘贴的网页源码（不联网）；
2. `{"curl": "curl 'https://…' -H 'referer: …' -H 'cookie: …'"}` —— 从浏览器「Copy as cURL」提取地址与 Referer/Cookie/UA；
3. `{"url": "https://…"}` —— 服务器抓取页面（带设置里的 UA/Referer/Cookie/代理，遵守 SSRF 规则，最多读 4 MB）。

缺省时 `url` 与 `curl` 都为空 → `INVALID_ARGUMENT`；抓取失败 → `HTTP_STATUS` / `URL_*`。

**响应压缩（v0064）**：文本类请求（网页 / 播放列表 / RSS）统一发 `config.DOWNLOAD_TEXT_ACCEPT_ENCODING = "gzip, deflate"`，读到正文后按 `Content-Encoding` 解压（gzip / x-gzip / deflate，deflate 兼容裸流）；**没有 `Content-Encoding` 但正文以 gzip 魔数 `1f 8b` 开头时也按 gzip 解**——实测有站点对 `identity` 请求回压缩体却不带头，此前会解成乱码、表现为“解析不到地址”。解压后仍受 4 MB 上限约束；页面按 `Content-Type`/`meta charset` 解码（utf-8 → gb18030 回退）。媒体分片下载仍用 `identity` + 连接复用，不受影响。

### 14.2 提取规则

`pageparse.extract_candidates(html, page_url)` 纯函数，按顺序合并、去重（同一绝对地址只留一次）：

| 来源 | 内容 |
|---|---|
| `video-tag` | `<video>/<audio>/<source>` 的 `src`、`data-src`、`data-video`、`data-url`、`data-file`、`data-stream`、`data-source`、`data-mp4`、`data-hls`、`data-live`、`data-playurl` |
| `link-preload` | `<link rel="preload" as="video|audio|fetch">` 的 href |
| `meta` | `og:video`、`og:video:url`、`og:video:secure_url`、`twitter:player:stream` |
| `json-ld` | `application/ld+json` 里的 `contentUrl`、`embedUrl`、`videoUrl`（递归，深度 ≤8） |
| `script` | 引号内以 `.m3u8/.mpd/.mp4/.webm/.mkv/.mov/.flv/.ts/.m4s` 结尾的地址（含 `\\u002F`、`\\/`、HTML 实体解码与相对路径补全），并记录上下文键名（file/source/url/src/loadSource…） |

- 丢弃 `data:` / `blob:` / `javascript:` / `about:` / `mailto:` 与非 http(s) 协议；
- 评分排序：HLS 100 > DASH 80 > 直链 60，页面标签/JSON-LD/meta 加权，路径含 `master/index/playlist` 再 +5，同分按出现顺序；
- `embedUrl` 等嵌入式播放页标记为 `kind=embed`（不是媒体文件，仅供打开），默认不勾选；
- 结果上限 `DOWNLOAD_PARSE_CANDIDATE_LIMIT=50`。

若抓到的正文本身就是播放列表（直接把 m3u8 地址填进解析框），返回单个 `direct` 候选，并在 `playlist` 里给出摘要：master → `variants[]`（清晰度/码率/url），media → `segments`/`duration`/`encryption`。

### 14.3 候选校验

对前 `DOWNLOAD_PARSE_VALIDATE_MAX=5` 个 `hls`/`dash` 候选发一次 `Range: bytes=0-65535` 请求：

- HLS：正文以 `#EXTM3U` 开头 → `valid=true`，否则 `valid=false` + `note`；
- DASH：正文含 `<MPD` → `valid=true`；
- 网络失败 → `valid=false` + `note`（错误文本经查询串/Cookie 清洗）；
- 直链与 `embed` 不做校验（`valid` 缺省）。

### 14.4 登录态暂存

解析出的 Referer/Cookie/UA **不返回给浏览器**，而是暂存在服务端：

- 响应返回 `parse_session`（`ps-` + 20 位 hex，单次解析一个）；
- 提交任务时把该 token 放进 `options.parse_session`，服务端合并进任务选项快照（`options.referer/cookie/user_agent`），Cookie 仍按密钥处理（详情只给 `cookie_set`）；
- TTL `DOWNLOAD_PARSE_SESSION_TTL_SEC=900` 秒，最多 `DOWNLOAD_PARSE_SESSION_MAX=32` 条，超限按创建时间淘汰；不写设置、不落盘、不进日志。

### 14.5 响应

```json
{
  "ok": true, "page_url": "…", "title": "清洗后的页面标题",
  "summary": "HLS 2 · 直链 4",
  "candidates": [{"url": "…", "kind": "hls|dash|file|embed", "ext": ".m3u8",
                  "source": "video-tag|link-preload|meta|json-ld|script|direct",
                  "label": "file", "score": 105, "valid": true, "note": "播放列表"}],
  "playlist": {"kind": "master", "variants": []},
  "parse_session": "ps-…",
  "hint": "没找到媒体地址时的中文提示"
}
```

`title` 已由 `pageparse.extract_title` 清洗（v0064）：优先 `og:title` / `twitter:title`，其次 `<title>`、`<h1>`；按 `_ | ｜ -` 切片后丢掉「正在播放」「视频网站」这类站名片段、取最长一段，再去掉首部「正在播放」与尾部「在线观看/完整版/全集/下载」等后缀。清洗后为空则返回空串。前端把它随地址放进创建请求的 `titles`，任务名与文件名都用它。

未找到任何候选时返回 200 + 空数组 + `hint`（不报错，前端提示改用源码/cURL）。

### 14.6 前端

下载页顶部「解析播放网页」卡片：网页地址 + 「解析网页」；折叠「手动粘贴」放网页源码与 cURL（解析粘贴内容）；候选列表每行 = 勾选框 + 类型徽标 + 地址 + 校验徽标 + 来源说明；「全选 / 清空 / 加入下载列表」把勾选地址追加到下方地址框（自动去重），随后的提交会带上 `parse_session`。

v0064 起：结果标题显示在候选列表标题上；「加入下载列表」把标题按地址记进 `titleMap` 并提示“文件名将使用标题「…」”，提交时只带本批地址的标题（`payload.titles`），提交成功即清空。文件名模板默认 `{title}`，因此产物名就是清洗后的片名。

### 14.7 非目标

不执行 JavaScript、不内置浏览器、不解决 Cloudflare 五秒盾、不做站点专用提取器；JS 渲染页请用「粘贴源码」或「Copy as cURL」兜底。正文只回 brotli（不理会 gzip 协商）的站点仍解不出来——Python 标准库没有 brotli，用 cURL 形态解析即可。

### 14.8 测试

`tests/test_v0052_pageparse.py`（19 例）：五类来源提取、相对/协议相对/转义/实体地址、embed 标记、去重与排序、忽略协议、标题/空页、编码回退（gb18030）、cURL 三种写法；服务级：真实页面抓取 + m3u8 校验、直接 m3u8 摘要、粘贴源码、cURL 登录态随任务下发、未启用/内网/缺参错误。

`tests/test_v0064_download_titles.py`（14 例，v0064）：标题清洗/优先级、gzip（带头/无头）/deflate/无压缩、压缩页面走完整解析链路、`titles` 命名与模板 token、未知/非法标题回退。
## 15. 下载增强（v0053）

### 15.1 下载前去重（`download_skip_existing`，默认开）

- 解析出最终文件名后（直链用 URL 后缀、HLS 用 `.mp4`），若目标路径已存在：
  - 直链且已知总大小：**体积一致**才跳过，体积不同视为不同文件，按重名规则输出 `名称 (2).ext`；
  - HLS 或大小未知：直接跳过；
- 命中后任务直接置 `done` 且 `skipped=true`、`message` 说明「已存在同名文件，跳过下载」，不写临时目录、不消耗流量（探测请求除外）；
- `overwrite=true` 或关闭该设置时不跳过；任务选项是提交时快照。

### 15.2 任务级自动重试（`download_auto_retry` / `download_auto_retry_max`，v0067 指数退避）

- 只对**可能是临时故障**的错误重试：
  - `HTTP_STATUS` 且无 `status`（断网/超时）、5xx、408、425、429 → 重试；
  - 4xx 里除 408/425/429（400/401/403/404/405/410 …）→ **不重试**，直接失败；
  - `SEGMENT_FAILED`、`INTERNAL_ERROR` → 重试；
  - `LIVE_/DRM_/PLAYLIST_INVALID/URL_*/OUTPUT_*/INSUFFICIENT_SPACE/FFMPEG_UNAVAILABLE/MERGE_FAILED` → 不重试。
- 退避（v0067，一次比一次长）：
  `delay(n) = min(cap, base × factor^(n-1))`，再叠加 **0–20% 正向抖动**（只增不减，避免一批任务同时重试）。
  - 默认 `base=60s`、`factor=3`、`cap=1800s`、`max=5` → **1 分钟 → 3 分钟 → 9 分钟 → 27 分钟 → 30 分钟（封顶）**，共 6 次执行机会；
  - 429/503 带 `Retry-After` 时取 `max(指数退避, Retry-After)`，仍受 `cap` 与次数限制；
  - 退避三项（`download_auto_retry_base_sec` / `_factor` / `_cap_sec`）在**每次重试时实时读设置**，改完对等待中的任务立即生效；开关与次数是提交时快照（任务选项）。
- 任务回队列并带 `retry_at`（重启后仍保留），调度器跳过未到期任务；`attempts` 超过 `auto_retry_max`（0–10，默认 5）后标 `failed`。
- 快照新增 `retry_in_sec` / `retry_max` / `auto_retry`；任务行显示「等待重试（第 N/M 次，约 X 后）」，失败行显示「已自动重试 N 次」。
- 手动「重试」会**复位 `attempts=0`**（重新给满自动重试额度）。
- 队列顺序仍按「优先级 → 创建时间」，重试任务不抢占。

### 15.3 分时限速（`download_offpeak_*`）

- `download_offpeak_enabled` 打开后，`[start, end)` 窗口内使用 `download_offpeak_rate_limit_kbps`，窗口外使用 `download_rate_limit_kbps`；
- 窗口支持跨零点（如 `23:00–07:00`）；非法/空窗口视为不生效；
- 生效判定在**任务开始时**（长任务不会中途切换档位）。

### 15.4 文件名模板（`download_name_template`，默认 `{title}`）

- 占位符：`{title} {site} {date} {yyyy} {mm} {dd} {height} {season} {episode} {ext}`；
- `{title}` 来源优先级（v0064）：创建请求 `titles[url]`（解析得到的页面标题）> `options.title` > `Content-Disposition` 文件名 > URL 文件名；都没有则 `download-<时间戳>`；
- `{height}` 来自所选清晰度（主列表变体高度）；`{season}/{episode}` 从标题识别 `S01E02` 或 `第2季 第3集`，识别不到为空；
- 不允许路径分隔符（目录用 `download_subdir_template`）；渲染结果做文件名清洗并保证带上扩展名；同名文件按 `名称 (2)` 递增。

### 15.5 优先级 / 批量 / 导出

- `POST /api/downloads/{job_id}/priority`：`{priority: 0–100}` 或 `{top: true}`（当前最大值 +1）；
- `POST /api/downloads/bulk`：`{action: pause|resume|cancel|retry, scope: all|queued|running|paused|failed}`，单次最多 `DOWNLOAD_BULK_MAX=500`，返回 `matched/affected`；
- `GET /api/downloads/export?status=&group_id=`：CSV（UTF-8 BOM，Excel 直接打开），列见 `DownloadService.EXPORT_HEADERS`。

### 15.6 空间看板

`GET /api/downloads/queue` 新增 `storage`：输出目录所在卷的 `path/free_bytes/used_bytes/total_bytes/used_pct/min_free_gb`；前端在任务列表标题旁显示「剩余 X（已用 Y%）」，低于 `download_min_free_gb` 标红提示。

### 15.7 通知联动

- 新增事件 `download_done` / `download_failed`（`config.NOTIFY_EVENTS` + 标签），复用 `NotificationService`（DSM/Webhook/Telegram 与 `notify_events` 订阅）；
- 详情只带 `job_id/name/size/url`（URL 查询串清洗）；发送失败只记 debug 日志，不影响任务状态。

### 15.8 解析增强

- 同一 `url` 解析结果缓存 `DOWNLOAD_PARSE_CACHE_TTL_SEC=600` 秒（最多 32 条），命中返回 `cached=true` 并重新发放 `parse_session`；`html`/`curl` 不缓存；
- 跟随一层 `<iframe>/<frame>`（最多 `DOWNLOAD_PARSE_IFRAME_MAX=3` 个）：iframe 内是 m3u8 时直接作为候选，否则继续静态提取并标记 `source=iframe`；
- 前端新增书签脚本：`javascript:` 打开 `<NAS>/#/download?parse=<当前页>`，下载页读取 `state.hashQuery.parse` 自动填入并解析。

### 15.9 测试

`tests/test_v0053_download_extras.py`（19 例）：去重跳过（同体积/不同体积/关闭）、文件名模板与季集识别、分时限速判定（含跨零点）、自动重试成功/放弃/不可重试、优先级排序、批量暂停恢复、CSV 导出、空间看板、解析缓存与 iframe、完成/失败通知。
## 16. DASH 清单（v0054）

- `dash.py` 纯解析：MPD → 「初始化段 + 分片」计划；下载仍走 `_fetch_all`，然后逐轨 `init + 分片` 字节拼接成 `*_track.mp4`，最后 FFmpeg `-c copy` 封装（失败回退音频 AAC）。
- 支持：单 Period、`SegmentTemplate`（`$Number$`/`$Number%05d$`/`$Time$`/`$RepresentationID$`/`$Bandwidth$`）、`SegmentTimeline`（`S@t/d/r`）、`SegmentList`（`Initialization` + `SegmentURL`）、`SegmentBase`（整文件）、四级 `BaseURL` 拼接、视频/音频各取一档（视频按高度→码率，受 `download_max_height` 约束）。
- 拒绝：含 `ContentProtection` → `DRM_UNSUPPORTED`；非 MPD → `PLAYLIST_INVALID`。
- 识别：`.mpd` 后缀、`Content-Type: */dash+xml`、或正文 `<?xml` 且前 8KB 含 `<MPD`；解析器也接受直接把 `.mpd` 地址填进「解析播放网页」。
- 非目标：多 Period 拼接、字幕轨、DRM/加密 DASH。

## 17. 订阅下载（v0054）

### 17.1 模型与持久化

`<var>/download_subscriptions.json`（不建表，schema 仍 v9）：

```json
{"subs": {"ds-xxxxxxxx": {
  "sub_id": "ds-…", "url": "…", "kind": "rss|page", "mode": "best|all",
  "interval_sec": 3600, "enabled": true, "user": "admin",
  "options": {"output_dir": "…", "subdir_mode": "site_date"},
  "seen": ["…"], "last_check": 0, "last_result": {}, "created_epoch": 0}}}
```

- 单次检查最多入队 `MAX_ENQUEUE=5`，每项最多记 `MAX_SEEN=200` 条已处理地址（超出丢最旧），订阅上限 `MAX_ITEMS=50`；
- 间隔 300–86400 秒；总开关 `download_subscribe_enabled`（默认关）只影响自动轮询，手动「立即检查」不受限。

### 17.2 条目来源

- `rss`：标准库解析 RSS 2.0（`item/link`、`enclosure@url`、http(s) `guid`）与 Atom（`entry/link@href`、`id`），去重保序，最多 50 条；
- `page`：复用 `DownloadService.parse_target`（静态解析 + cURL 无关），只取 `hls/dash/file` 候选；`mode=best` 只取最高分一条，`all` 取全部；解析得到的 `parse_session` 会随入队地址一起提交（Referer/Cookie 生效）。

### 17.3 轮询

- 服务内线程每 30 秒 tick 一次；只有 `enabled` 且距上次检查 ≥ `interval_sec` 的订阅才检查；
- 抓取/解析失败不影响其他订阅，错误写入 `last_result.error`；
- 套件重启后从状态文件恢复；`Services.shutdown()` 会先停订阅线程再停下载服务。

### 17.4 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/downloads/subscriptions` | 列表 + 总开关状态 + 上限 |
| POST | `/api/downloads/subscriptions` | 新增（201）：`{url, kind?, interval_sec?, mode?, options?}` |
| POST | `/api/downloads/subscriptions/{sub_id}/check` | 立即检查（同步执行，返回 checked/new/enqueued/job_ids） |
| PATCH | `/api/downloads/subscriptions/{sub_id}` | `{enabled?, interval_sec?, mode?}` |
| DELETE | `/api/downloads/subscriptions/{sub_id}` | 删除 |

Cookie 等敏感选项在返回时只给 `cookie_set`。

### 17.5 测试

`tests/test_v0054_dash_subs.py`（16 例）：DASH 模板/时间轴/列表/DRM/非法清单与真实下载链路；RSS/Atom 解析；订阅新增/更新/删除、RSS 只入队一次、网页 best 模式、总开关与间隔门控、Cookie 掩码。

## 18. 清空已完成（v0068）

- 入口：下载页「下载任务」工具栏的「清空已完成」（`#downloadClearDoneBtn`，scope `done`）与「清空失败/取消」（`#downloadClearFailedBtn`，scope `failed_cancelled`）；对应状态数量为 0 时按钮禁用，点击后二次确认。
- API：`POST /api/downloads/bulk`，请求体 `{"action": "clear_finished", "scope": "done"}`。`scope` 取值：

| scope | 清除范围 | 界面入口 |
|---|---|---|
| `done` | 已完成 | 清空已完成 |
| `failed` | 失败 | — |
| `cancelled` | 已取消 | — |
| `failed_cancelled` | 失败 + 已取消 | 清空失败/取消 |
| `finished` / 省略 | 全部已结束（已完成 + 失败 + 已取消） | — |

- 行为：只删除任务记录（内存任务表 + `download_jobs` 行）与残留临时文件；**不删除已下载的产物文件**。逐条复用单任务删除语义，下载组的 `job_ids` 同步裁剪。活动任务（排队/运行/暂停）不受影响；单次最多 `DOWNLOAD_BULK_MAX`（500）条。
- 响应：`{"ok": true, "action": "clear_finished", "scope": "...", "matched": n, "affected": n, "counts": {...}}`；未知 scope → 400 `INVALID_ARGUMENT`。
- 前端：成功后刷新队列；对应状态为 0 时按钮禁用并提示。失败/已取消任务在被清空前仍可用「重试失败」或逐条「删除」处理。
- 测试：`src/backend/tests/test_v0068_download_clear.py`（8 例）；e2e `DL-CLEAR-01..04`。