# 发布说明 1.1.0-0067（下载失败自动重试：指数退避）

> 版本：**1.1.0-0067** · 日期：2026-11-22
> 上一版：[1.1.0-0065](RELEASE_NOTES_v1.1.0-0065.md)（转换 VAAPI 硬编硬解）；**本版同时包含未单独发版的 v0066**（[字幕队列设置回到「任务」标签](RELEASE_NOTES_v1.1.0-0066.md)）
> 主题：下载失败后**自动重试，且一次比一次等得久**；只重试临时故障，界面能看到"在等第几次重试"

## 1. 问题：自动重试存在，但几乎没用

v0053 就有任务级自动重试，但它**默认只重试 2 次、间隔固定为 15s × 次数**（即 15s、30s），
遇到限流（429）、服务器重启（503）或家宽抖动时，一分钟内就把重试额度花完了；
而且它是**线性**增长，不是"一次比一次长"。使用中还发现三个具体问题：

1. **4xx 也在瞎重试**：任务级只判断错误码是 `HTTP_STATUS`，404/403 也会等 15s+30s 才失败（分片级早已排除 4xx，任务级没排除）。
2. **界面上看不见**：`retry_at`/`message`（"自动重试 1/2（15 秒后）"）都在快照里，但任务行只显示 `stage_label`，
   等待重试的任务在页面上就是「排队中」——用户以为卡住了。
3. **手动重试不复位计数**：自动重试额度用完后点「重试」，再失败一次就直接 `failed`，拿不到新的自动重试。

## 2. 新行为

### 2.1 指数退避（一次比一次长）

```
delay(n) = min(cap, base × factor^(n-1)) × (1 + 0..20% 正向抖动)
```

| 第几次失败 | 默认等待 | 累计 |
|---|---|---|
| 1 | 1 分钟 | 1 分钟 |
| 2 | 3 分钟 | 4 分钟 |
| 3 | 9 分钟 | 13 分钟 |
| 4 | 27 分钟 | 40 分钟 |
| 5 | 30 分钟（封顶） | 70 分钟 |

- 默认 `base=60s`、`factor=3`、`cap=1800s`、最多 **5** 次（共 6 次执行机会，上限 10 次）；
- 抖动**只增不减**（0–20%），保证"一次比一次长"，同时避免一批任务同一秒重试把站点打爆；
- 429/503 带 `Retry-After` 时取 `max(指数退避, Retry-After)`，仍受 `cap` 与次数限制；
- 退避三项**每次重试时实时读设置**：改完设置，正在等待的任务下一次就按新节奏走；
- 重试走断点续传（`.dcpart` / 已下载分片还在），不会重复下载已完成部分。

### 2.2 只重试"可能是临时"的错误

| 重试 | 立即失败 |
|---|---|
| `HTTP_STATUS` 无 status（断网/超时） | 4xx 除 408/425/429（400/401/403/404/405/410…） |
| 5xx、408、425、429 | `PLAYLIST_INVALID`、`URL_INVALID`、`URL_TOO_LONG` |
| `SEGMENT_FAILED` | `OUTPUT_DIR_INVALID`、`OUTPUT_EXISTS`、`INSUFFICIENT_SPACE` |
| `INTERNAL_ERROR` | `LIVE_UNSUPPORTED`、`DRM_UNSUPPORTED`、`FFMPEG_UNAVAILABLE`、`MERGE_FAILED` |

### 2.3 界面可见

- 等待重试的任务行：**「等待重试（第 2/5 次，约 3 分钟后）」** + 上次失败原因；
- 失败任务行：追加「已自动重试 N 次」；
- 设置 → 下载 新增 5 个控件：自动重试开关、最多重试次数、首次等待（秒）、等待递增倍数、单次等待上限（秒），
  下方实时预览"当前节奏：1 分钟 → 3 分钟 → 9 分钟 → 27 分钟 → 30 分钟（共 6 次执行机会）"；
- 下载组摘要显示「失败重试 N 次 / 不自动重试」。

### 2.4 手动重试

`POST /api/downloads/{id}/retry` 现在会**复位 `attempts=0`、`retry_at=0`**：手动重试重新获得完整的自动重试额度。

## 3. 后端改动

- `config.py`：`DOWNLOAD_AUTO_RETRY_BASE_SEC/FACTOR/CAP_SEC/JITTER` + 取值范围常量 + `DOWNLOAD_RETRYABLE_HTTP_STATUS`；
  `DOWNLOAD_AUTO_RETRY_MAX_DEFAULT` 2→5、`MAX_LIMIT` 5→10。
- `downloads.py`：新增 `_is_retryable_error`、`_auto_retry_delay`、`_parse_retry_after`、`_human_eta`；
  `_schedule_retry` 重写；`open()` 在 HTTPError 上记录 `details["retry_after"]`；
  `snapshot()` 新增 `auto_retry/retry_max/retry_in_sec`；`retry()` 复位计数。
- `settings.py`：新增 `download_auto_retry_base_sec`（10–600）、`download_auto_retry_factor`（2–5）、
  `download_auto_retry_cap_sec`（60–7200）默认值与校验；`download_auto_retry_max` 上限 10。
- 不新增表、不改 schema（复用 `attempts` / `retry_at`，重启后等待中的任务继续按 `retry_at` 排队）。

## 4. 测试与 QA

- 单元测试 **913 OK（6 skipped）**；新增 `tests/test_v0067_download_retry.py`（15 例：指数序列/封顶/设置覆盖/抖动单调、
  `Retry-After` 解析、可重试分类（404/403/401 立即失败、503/429/断网重试）、次数上限、快照字段、
  记录往返、404 集成、500 重试成功、手动重试复位）。
- 静态检查 **260 PASS / 0 FAIL**；`BE-DOWNLOAD-RETRY` 收紧，新增 `BE-DOWNLOAD-RETRY-BACKOFF` 与 `FE-DOWNLOAD-RETRY-UI`。
- e2e **275 PASS / 0 FAIL / 1 UNVERIFIED**（Windows 符号链接用例）；新增 `DL-RETRY-01..05`：设置回传/保存/越界 400、404 不重试（attempts=1）、快照含退避字段。
- 真机 Playwright 探针（dev 后端 + 真实 `index.html` + 永远 500 的假 CDN，**8/8 PASS**）：
  - 设置页 5 个控件回填正确（5 次 / 10 秒 / ×2 / 60 秒封顶），节奏预览「10 秒 → 20 秒 → 40 秒 → 1 分钟」随输入实时变化；
  - 点“保存设置”后 `GET /api/settings` 立即回读 base=30，其余参数不变；
  - 下载页任务行显示「**等待重试（第 2/5 次，约 47 秒后）**」+「上次失败：服务器返回 HTTP 500」，接口快照 `retry_in_sec/retry_max/attempts/auto_retry` 齐全；
  - 无 console 报错。

## 5. 兼容性与升级

- 无数据库迁移；老任务记录（`attempts` 已大于新默认上限）会在下次失败时直接结束，符合预期。
- 设置键只增不减；默认值有变化（15s/2 次 → 60s/5 次），需要旧行为可在设置页调回（首次等待 10 秒起、次数 0–10）。
- 产物：`dist/DuplicateCleaner-1.1.0-0067.spk`（97,160 KB，SHA256 `23c2ebe8bcb063f61e5a322e30beb5a0623707e15289889040644314db760772`，package.tgz MD5 `52edc8df0847d7cc8214ec90887353b4`）。
