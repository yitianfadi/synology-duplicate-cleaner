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

全部接口**仅管理员**；写操作需要 `X-CSRF-Token`；`download_enabled=false` 时统一返回 `DOWNLOAD_DISABLED`（403）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/downloads` | 创建任务（202）。body：`{urls: [], group_title?, options?}` |
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

## 5. 数据模型（schema v9）

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
| `download_enabled` | `false` | bool |
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
- `download_auto_subtitle`：任务完成后以 `create_batch({paths:[产物]})` 提交字幕，`translate` 沿用 `subtitle_translate_enabled`；未启用字幕只记日志；
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
  "ok": true, "page_url": "…", "title": "页面标题",
  "summary": "HLS 2 · 直链 4",
  "candidates": [{"url": "…", "kind": "hls|dash|file|embed", "ext": ".m3u8",
                  "source": "video-tag|link-preload|meta|json-ld|script|direct",
                  "label": "file", "score": 105, "valid": true, "note": "播放列表"}],
  "playlist": {"kind": "master", "variants": []},
  "parse_session": "ps-…",
  "hint": "没找到媒体地址时的中文提示"
}
```

未找到任何候选时返回 200 + 空数组 + `hint`（不报错，前端提示改用源码/cURL）。

### 14.6 前端

下载页顶部「解析播放网页」卡片：网页地址 + 「解析网页」；折叠「手动粘贴」放网页源码与 cURL（解析粘贴内容）；候选列表每行 = 勾选框 + 类型徽标 + 地址 + 校验徽标 + 来源说明；「全选 / 清空 / 加入下载列表」把勾选地址追加到下方地址框（自动去重），随后的提交会带上 `parse_session`。

### 14.7 非目标

不执行 JavaScript、不内置浏览器、不解决 Cloudflare 五秒盾、不做站点专用提取器；JS 渲染页请用「粘贴源码」或「Copy as cURL」兜底。

### 14.8 测试

`tests/test_v0052_pageparse.py`（19 例）：五类来源提取、相对/协议相对/转义/实体地址、embed 标记、去重与排序、忽略协议、标题/空页、编码回退（gb18030）、cURL 三种写法；服务级：真实页面抓取 + m3u8 校验、直接 m3u8 摘要、粘贴源码、cURL 登录态随任务下发、未启用/内网/缺参错误。