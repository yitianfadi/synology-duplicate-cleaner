# 发布说明 1.1.0-0053（下载增强）

> **状态：未打包、未发布。** 按用户要求本轮仍不生成 SPK；`dist/` 仍是上一版 `1.1.0-0051` 的产物。
> 需要出包时执行 `python build/build_spk.py`，再跑 `python qa/static_checks.py` 让 `SPK-SYNC`/`SPK-BUILD-VERSION` 归位。

- 版本：`1.1.0-0053`
- 日期：2026-11-15
- 基线：`1.1.0-0050`（最近一次正式发布）
- 重点：在 v0051 下载中心 / v0052 网页解析之上，补齐「A 组」增强：去重、自动重试、分时限速、命名模板、批量与导出、空间看板、通知、解析缓存与 iframe、书签脚本。

## 1. 新增功能（v0053）

### 1.1 下载前去重

- 最终文件名已存在且（直链）体积一致时，任务直接完成并标记「已跳过」，不写临时文件、不消耗流量。
- 体积不同视为不同文件，按重名规则输出 `名称 (2).ext`；设置 `download_skip_existing` 可关闭，`overwrite` 优先。

### 1.2 任务级自动重试

- 网络/分片类错误（`HTTP_STATUS`/`SEGMENT_FAILED`/`INTERNAL_ERROR`）自动退避重排，退避 15s × 次数，次数上限 0–5（默认 2）。
- DRM、直播、地址非法、空间不足、FFmpeg 不可用等确定性错误不重试。

### 1.3 分时限速

- 低谷窗口（`download_offpeak_start/end`，支持 `23:00–07:00` 跨零点）内使用 `download_offpeak_rate_limit_kbps`，窗口外使用常规 `download_rate_limit_kbps`。
- 生效判定在任务开始时，长任务不会中途换档。

### 1.4 文件名模板

- `download_name_template` 支持 `{title} {site} {date} {yyyy} {mm} {dd} {height} {season} {episode} {ext}`。
- 自动识别 `S01E02` 与「第二季第三集」；不允许路径分隔符（目录仍用子目录模板）。

### 1.5 队列与导出

- 任务置顶（`POST /api/downloads/{id}/priority`，`{top:true}` 或数值）。
- 批量操作（`POST /api/downloads/bulk`）：全部暂停/继续/重试失败，scope = all/queued/running/paused/failed。
- 导出 CSV（`GET /api/downloads/export`，UTF-8 BOM，Excel 直接打开）。
- 任务列表显示下载目录剩余空间（低于 `download_min_free_gb` 标红）。

### 1.6 通知联动

- 新增 `download_done` / `download_failed` 事件，走原有 DSM/Webhook/Telegram 渠道与订阅开关；详情含任务名/大小/地址（查询串已清洗）。

### 1.7 解析增强

- 同一网页 URL 解析结果缓存 10 分钟（返回 `cached=true`），`html`/`cURL` 输入不缓存。
- 跟随一层 `<iframe>`（最多 3 个）继续提取，iframe 内是 m3u8 时直接作为候选。
- 新增书签脚本：在播放页一键打开 `<NAS>/#/download?parse=<当前页>`，下载页自动填入并解析。

## 2. 兼容性

| 项 | 变化 |
|---|---|
| SQLite schema | **v9 不变**（v0053 只加设置键与运行时能力，无迁移） |
| 设置 | 新增 8 个键：`download_skip_existing`、`download_auto_retry`、`download_auto_retry_max`、`download_offpeak_enabled/start/end/rate_limit_kbps`、`download_name_template`（均有默认值） |
| 通知事件 | `notify_events` 枚举新增 2 项；已保存的旧列表不含它们，需要时在设置里勾选 |
| API | 新增 `/api/downloads/bulk`、`/api/downloads/export`、`/api/downloads/{id}/priority`；`/api/downloads/queue` 增加 `storage` |
| 前端 | 下载页新增空间看板、批量按钮、置顶、书签脚本；`?v=` 更新为 1.1.0-0053 |

## 3. 校验值

出包后填写（`dist/DuplicateCleaner-1.1.0-0053.spk`）：

```
SHA256: 待打包
package.tgz MD5: 待打包（= spk/INFO checksum）
大小: 待打包
```

## 4. QA 结果（源码级，未打包）

以本次交付实际运行为准，数字见 [`qa/REPORT.md`](../qa/REPORT.md) §1/§6 的 v0053 行：

- 后端 unittest：新增 `tests/test_v0053_download_extras.py`（19 例）；
- 端到端：新增 DL-09…DL-12（空间看板 / CSV 导出 / 批量暂停 / 置顶）；
- 静态检查：未执行（依赖最新 SPK；出包后统一跑）。

## 5. 回退

- 回退到已发布的 `DuplicateCleaner-1.1.0-0050.spk` 即可；下载产物与 `.dc-download-*` 临时目录不会被回退删除。
- 若只是想关掉新行为：关闭 `download_skip_existing` / `download_auto_retry` / `download_offpeak_enabled`，或把 `download_name_template` 恢复为 `{title}`。
