# 发布说明 1.1.0-0052（下载中心 + 播放网页解析）

> **状态：未打包、未发布。** 按用户要求本次只完成源码与验收，不生成 SPK；`dist/` 仍是上一版 `1.1.0-0051` 的产物。
> 需要出包时执行 `python build/build_spk.py`，再跑 `python qa/static_checks.py` 让 `SPK-SYNC`/`SPK-BUILD-VERSION` 归位。

- 版本：`1.1.0-0052`
- 日期：2026-11-15
- 基线：`1.1.0-0050`（最近一次正式发布）
- 重点：新增「下载」页（直链 / HLS(m3u8) 批量下载并合并 MP4）+「解析播放网页」（静态提取 + cURL 兜底）。

## 1. 新增功能

### 1.1 下载中心（v0051）

- 主导航新增「下载」（`#/download`）：粘贴地址（每行一个）批量入队，也可单个下载。
- **直链**：任意 `http(s)` 文件，支持 HTTP Range 断点续传。
- **HLS（m3u8）**：主/媒体播放列表、TS、fMP4（`EXT-X-MAP`）、`EXT-X-BYTERANGE`、多码率自动选档、独立音轨（`EXT-X-MEDIA`）合并。
- **AES-128 加密流**：分片与密钥先落地，重写成本地播放列表，交给内置 FFmpeg 用原生 AES 解密并无损封装为 MP4。
- 队列与并发：任务级并发 1–4（默认 2）、单任务分片并发 1–16（默认 4）、全局限速、剩余空间闸门；超出并发 FIFO 排队；暂停/继续/取消/重试/删除，批量组可整组操作。
- 断点续传：分片级（已存在即跳过）与直链 Range；套件重启后「运行中」任务重新排队继续。
- 输出：共享目录用选择器挑选（不写死）；可按站点 / 日期 / 站点+日期 / 自定义模板分子目录；同名默认不覆盖。
- 防盗链/登录态：User-Agent、Referer、Cookie、代理；Cookie 为密钥键，只写不读。
- 安全默认：拒绝内网/回环地址（SSRF）、拒绝带账号密码的 URL、输出目录必须在共享文件夹内。
- 完成联动（可选）：自动入队扫描该目录（同目录 10 分钟去抖）/ 自动提交字幕任务。

### 1.2 播放网页解析（v0052）

- 下载页新增「解析播放网页」：填网页地址 → 静态提取 `<video>/<source>/data-*`、`<link rel=preload>`、社交卡片 meta、JSON-LD、播放器脚本文本里的 m3u8 / 直链候选。
- 候选列表带类型徽标（HLS/DASH/直链/嵌入页）与「是否真是播放列表」校验（前 5 个 m3u8 各做一次 `Range 64KB` 请求）。
- JS 渲染或需登录的页面：可「粘贴网页源码」，或从浏览器 DevTools「Copy as cURL」粘贴整条命令；地址、Referer、Cookie、UA 会自动提取。
- cURL 得到的登录态**不回传浏览器**：服务端暂存 15 分钟（上限 32 条），只用于随后提交的下载任务。
- 直接把 m3u8 地址填进解析框也可以：返回清晰度/时长/加密方式摘要。

### 1.3 明确不做

DRM（Widevine/FairPlay/PlayReady）与 SAMPLE-AES 不做任何绕过；不支持直播/滚动播放列表；不执行 JavaScript、不内置浏览器、不解决 Cloudflare 挑战；不做站点专用提取器；不做转码（只做无损封装）。

## 2. 兼容性

| 项 | 变化 |
|---|---|
| SQLite schema | v8 → **v9**（新增 `download_jobs` / `download_groups`，自动迁移、幂等） |
| 设置 | 新增 21 个 `download_*` 键，全部有默认值（`download_enabled` 默认关闭） |
| API | 新增 `/api/downloads*`；既有接口不变 |
| 前端 | 新增导航「下载」；`?v=` 版本号更新为 `1.1.0-0052` |
| 覆盖升级 | 支持从 1.1.0-0050 直接覆盖安装（v0051/v0052 尚无已发布包，实际出包后再核对） |

## 3. 安装步骤（出包后）

1. `python build/build_spk.py` 生成 `dist/DuplicateCleaner-1.1.0-0052.spk`。
2. 套件中心 → 手动安装 → 选择该 SPK（覆盖安装，保留数据）。
3. 打开 `http://<NAS>:8787` → 「设置 → 下载」→ 勾选「启用下载中心」并选择默认下载目录（共享文件夹内、`duplicatecleaner` 可写）。
4. 打开「下载」页：粘贴地址或先「解析播放网页」→ 勾选 → 加入下载列表 → 开始下载。

## 4. 校验值

出包后填写（`dist/DuplicateCleaner-1.1.0-0052.spk`）：

```
SHA256: 待打包
package.tgz MD5: 待打包（= spk/INFO checksum）
大小: 待打包
```

## 5. QA 结果（源码级，未打包）

| 套件 | 结果 |
|---|---|
| 后端 unittest | **713 OK（5 skipped）** |
| 端到端 | **217 PASS / 0 FAIL / 1 UNVERIFIED**（Windows symlink 历史项；新增 DL-06…DL-08） |
| 前端模块图 | **25/25 PASS** |
| 静态检查 | 未执行：`SPK-SYNC` / `SPK-BUILD-VERSION` 依赖最新 SPK，按用户要求本次不打包，出包后统一跑 |

详细基线与偏差见 [`qa/REPORT.md`](../qa/REPORT.md)；下载与解析契约见 [`docs/DOWNLOAD_CONTRACT.md`](DOWNLOAD_CONTRACT.md)（§14 为网页解析）。

## 6. 回退

- 回退到已发布的 `DuplicateCleaner-1.1.0-0050.spk` 即可；schema v9 对旧版属于「更高版本」，旧版按兼容模式打开（建议回退前 `POST /api/storage/backup` 备份数据库）。
- 下载产物与 `.dc-download-*` 临时目录不会被回退删除，需手动清理。
