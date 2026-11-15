# 发布说明 1.1.0-0054（DASH + 订阅下载 + 连接复用）

> **状态：未打包、未发布。** 按用户要求不生成 SPK；`dist/` 仍是 `1.1.0-0051` 的产物。
> 出包时执行 `python build/build_spk.py`，再跑 `python qa/static_checks.py` 让 `SPK-DIST`/`SPK-SYNC` 归位。

- 版本：`1.1.0-0054`
- 日期：2026-11-15
- 基线：`1.1.0-0050`（最近一次正式发布）
- 重点：完成「A 组」剩余三项 —— DASH（`.mpd`）支持、订阅下载、连接复用与自适应并发。

## 1. 新增功能

### 1.1 DASH（MPD）支持

- 支持 `SegmentTemplate`（`$Number$`、`$Number%05d$`、`$Time$`、`$RepresentationID$`、`$Bandwidth$`）、`SegmentTimeline`（`S@t/d/r`）、`SegmentList`、`SegmentBase`（整文件）、MPD/Period/AdaptationSet/Representation 四级 `BaseURL`。
- 视频/音频各取一档（视频按高度→码率，受「清晰度上限」约束），下载后逐轨 `init + 分片` 拼接，再由 FFmpeg `-c copy` 封装为 MP4（失败回退一次音频 AAC）。
- 识别方式：`.mpd` 后缀、`Content-Type: */dash+xml`、或正文 `<?xml` 且含 `<MPD`；也可以把 `.mpd` 地址直接填进「解析播放网页」。
- 含 `ContentProtection` 的 DRM 清单直接报 `DRM_UNSUPPORTED`（不尝试绕过）。
- 限制：只支持单 Period（多 Period 取第一个）、不含字幕轨。

### 1.2 订阅下载

- 支持 RSS 2.0 / Atom 源与播放网页两类订阅：按间隔（300–86400 秒）检查，新条目自动入队。
- 已处理地址持久化去重（每项最多 200 条），单次最多入队 5 条；网页订阅可选「只下最高分候选」或「全部新候选」。
- 总开关 `download_subscribe_enabled`（默认关）：关闭时不自动轮询，但可在下载页对单个订阅「立即检查」。
- 下载页新增订阅卡片：添加（地址/间隔/策略）、立即检查、暂停/启用、删除，并显示上次检查结果（检查数/新增/入队）。
- 状态保存在 `<var>/download_subscriptions.json`，不新增数据库表；Cookie 类选项只回传 `cookie_set`。

### 1.3 连接复用与自适应并发

- 分片下载在有/无代理两种路径下自动选择：无代理时使用按 host 的 keep-alive 连接池（HTTPS 校验证书），重定向或连接异常自动回退到原 urllib 路径。
- 自适应并发：持续出现慢分片（≥8 秒且非大文件）时把单任务分片并发减半（最少 1），快速分片积累后自动恢复；自动重试时也会降低并发再试。

## 2. 兼容性

| 项 | 变化 |
|---|---|
| SQLite schema | **v9 不变**（订阅用 JSON 状态文件） |
| 设置 | 新增 `download_subscribe_enabled`（默认 `false`） |
| API | 新增 `/api/downloads/subscriptions` 系列；`/api/meta` 的 `download` 增加 `dash/subscribe` 能力标记 |
| 前端 | 下载页新增「订阅下载」卡片；`?v=` 更新为 `1.1.0-0054` |

## 3. 校验值

出包后填写（`dist/DuplicateCleaner-1.1.0-0054.spk`）：

```
SHA256: 待打包
package.tgz MD5: 待打包（= spk/INFO checksum）
大小: 待打包
```

## 4. QA 结果（源码级，未打包）

数字见 [`qa/REPORT.md`](../qa/REPORT.md) §1/§6 的 v0054 行；本轮新增 `tests/test_v0054_dash_subs.py`（16 例）与 e2e DL-13…DL-18（DASH、订阅添加/检查/列表/删除）。

## 5. 回退

- 回退到已发布的 `DuplicateCleaner-1.1.0-0050.spk`；下载产物、`.dc-download-*` 临时目录与 `download_subscriptions.json` 不会被回退删除。
- 只想关掉新行为：关闭 `download_subscribe_enabled`；DASH 无单独开关（按地址识别）。
