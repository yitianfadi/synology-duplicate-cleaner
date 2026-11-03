# 重复文件清理与字幕 · Duplicate Cleaner & Subtitles（群晖 DSM 7 SPK）

一款为 Synology DSM 7.x 编写的重复文件清理套件：提供**哈希精准匹配**、**图片/文档内容相似**与**视频视觉相似**三种扫描模式，默认只保留**创建时间最晚**的一份；先出报告，人工核对、两次确认后才删除，默认移入群晖回收站，并持久化操作日志。图片/文档相似结果必须人工复核、禁止一键批量删除；视频相似组默认不勾选，允许手动全选批量删除。

> ## ⚠️ 风险提示
> **删除文件存在数据丢失风险，重要数据建议先备份；本工具仅基于哈希判定重复，无法保证业务文件适用性，请核对扫描结果再操作。**
>
> **图片/文档相似为特征比对结果，存在误匹配概率，必须人工复核、禁止一键批量删除。视频视觉相似误判率更高：默认不勾选，即使使用“全选视频相似组副本”批量删除，也必须人工核对保留视频并完成两次确认。**
>
> 套件以 DSM 系统内部用户 `duplicatecleaner`（非 root）运行。首次使用需在“控制面板 → 共享文件夹 → 权限 → 系统内部用户”中给该用户授予目标文件夹读取/写入权限；仅扫描可只给读取，删除副本需要写入。请只在可信局域网使用，不要将 8787 端口暴露到公网。

## 功能

- 📁 文件夹选择器：共享文件夹/子目录多选，排除指定目录
- 🔍 **精准模式（默认）**：大小 → 头尾采样指纹 → 全量 SHA-256/MD5，完全一致才算重复（文件名不参与）
- 🧠 **内容相似模式**：文本/PDF/DOCX/ODT/PNG 等特征向量 + MinHash/感知哈希检索，可调 0–100% 阈值；精准组与相似组分开展示
- 🎬 **视频视觉相似（默认关闭）**：SPK 内置 FFmpeg（x86_64/aarch64），按总时长动态抽取 5–100 帧，生成时序 pHash 指纹并用 DTW 比较；独立分区，默认不勾选，支持组级/全局全选视频副本批量删除；指纹 SQLite 缓存、独立并发、硬解探测与显式硬解模式（auto/off/vaapi/qsv/nvdec/videotoolbox），可配置外部 FFmpeg 绝对路径（留空=内置/自动探测）
- 🗣️ **云端字幕（实验，默认关闭）**：OpenAI 兼容 ASR（`/audio/transcriptions`）+ OpenAI 兼容 LLM（`/chat/completions`）翻译；FFmpeg 抽音频、自动分块、SRT/VTT/双语字幕下载；音频会发送到所配置的第三方服务，必须显式勾选确认
- 🕒 保留策略：最晚创建（默认）/ 最早创建 / 指定路径；优先读取 `statx` btime；无创建时间时整组按修改时间自动选择并标记，拒绝用 `st_ctime` 冒充
- 📊 分组报告：路径、大小、完整哈希、创建时间、修改时间、匹配类型、相似度；保留项高亮；可预览、导出 CSV
- ⚡ 大库性能：结果分页/搜索/排序走 SQLite；`scan_files` 路径搜索在 SQLite 支持时使用 FTS5 trigram（100k 明细 P95 ≈ 80ms），不支持或搜索词 <3 字符自动回退 LIKE 子串（100k P95 ≈ 164ms）
- 🛡️ 强安全删除：报告 → 手动确认 → 二次弹窗 → 服务端逐文件重校验 → 默认进回收站 → 审计日志
- 🚧 图片/文档相似组默认不勾选、禁止组级全选/一键批量删除；必须勾选“人工复核”后逐条选择
- 🎬 视频组默认不勾选；允许“全选本组/全选视频相似组副本”批量进入删除计划，仍需两次确认；ASR 为可选外部命令（默认关闭，不内置模型、不联网）
- ⏯️ 扫描可暂停/继续/停止；精准/相似并发、哈希限速、特征缓存、低优先级（nice/ionice）
- 🧩 前端为 ES modules（`app.js` 入口 + `js/` 下 8 个模块），无构建步骤、无第三方依赖；静态检查含模块图与无头浏览器冒烟
- 🚫 跳过符号链接、空文件、隐藏/系统文件、自定义后缀；相似模式默认跳过压缩包/媒体/二进制大文件（它们仍参与哈希精准匹配）
- 📜 扫描历史与操作日志持久化，可下载
- ⚙️ 可选：大小区间、大小写后缀、保留指定路径、回收站/永久删除

## 快速开始

### 1. 构建 SPK（开发机）

```bash
python build/fetch_ffmpeg.py   # 首次构建：下载两个架构的静态 FFmpeg 到 vendor/ffmpeg/
python build/build_spk.py
# 生成 dist/DuplicateCleaner-1.1.0-0025.spk
```

构建机需要 Python 3.8+；FFmpeg 二进制不提交到 Git，首次构建需运行 `build/fetch_ffmpeg.py`。重新生成图标需要 Pillow（仓库已带 PNG，可 `--no-icons`）。

### 2. 安装到 DSM

1. DSM 套件中心先安装 **Python3**（3.8+）。
2. 套件中心 → 手动安装 → 选择 `dist/DuplicateCleaner-1.1.0-0025.spk`。
3. 启动套件；从 DSM 主菜单打开“重复文件清理与字幕”，或访问 `http://<NAS>:8787/`。
4. 使用 administrators 组账号登录。
5. 按页面黄色“权限提示”给 `duplicatecleaner` 系统内部用户授权目标共享文件夹（读取；删除副本需读取/写入）。

详细步骤、权限、TLS、排错见 [打包与部署](docs/SPK_PACKAGING.md)。

### 3. 本地开发联调（Windows/Linux/macOS）

不需要 DSM 也可以跑通完整流程（开发模式，仅用于本机测试）：

```bash
# PowerShell
$env:DUP_CLEANER_DEV="1"
$env:DUP_DEV_ROOTS="D:\tmp\dup-test;D:\tmp\dup-test2"
$env:DUP_DEV_PASSWORD="admin"
python src/backend/app.py --host 127.0.0.1 --port 8787 --data-dir ./data --web-dir ./src/web
```

浏览器打开 `http://127.0.0.1:8787/`，账号 `admin` / 密码 `admin`（或 `DUP_DEV_PASSWORD`）。
开发模式只在环境变量显式开启时生效，SPK 启动脚本会主动 `unset` 相关变量。

### 4. 测试

```bash
cd src/backend
python -m unittest discover -s tests -v

# 前端模块语法/图与静态验收
node qa/check_web_modules.mjs
python qa/static_checks.py --skip-tests

# 浏览器冒烟（无头 Chromium；本机无浏览器时自动 SKIP）
python qa/web_smoke.py

# 100k 明细路径搜索基准（FTS5 vs 强制 LIKE）
python qa/bench_search.py
```

验收脚本与报告见 `qa/`（集成测试、需求逐项对照、SPK 结构检查）。

## 文档

- [整体方案架构](docs/ARCHITECTURE.md)：组件、扫描/哈希/时间/删除设计、群晖 API 调用点、性能与限制
- [前端页面原型说明](docs/UI_PROTOTYPE.md)：页面线框、文件夹选择器、结果页、两次确认流程、状态与验收
- [打包、部署与运维](docs/SPK_PACKAGING.md)：依赖、构建、安装、权限、TLS、日志、升级/卸载、排错
- [前后端实现契约](docs/CONTRACT.md)：接口、字段、错误码、安全规则（精准模式）
- [内容相似度扩展契约](docs/SIMILARITY_CONTRACT.md)：相似模式接口、特征算法、人工复核门禁
- [视频视觉相似契约](docs/VIDEO_CONTRACT.md)：FFmpeg、动态采样、指纹、DTW、缓存、视频批量删除与 Excel
- [云端字幕契约](docs/SUBTITLE_CONTRACT.md)：OpenAI 兼容 ASR/LLM、分块、SRT/VTT、双语字幕、密钥与隐私
- [存储层契约](docs/STORAGE_CONTRACT.md)：SQLite 元数据、旧 JSON 迁移、检查点、WAL 与备份
- [扫描暂停/升级 drain 契约](docs/RESUME_CONTRACT.md)：暂停断点、升级前 drain、重启后手动继续
- [QA 验收报告](qa/REPORT.md)：需求逐项对照与测试证据

## 仓库结构

```
src/backend/        Python 3.8+ 标准库后端（HTTP API、扫描、哈希、statx、删除、审计）
src/web/            纯静态 SPA（无构建步骤、无 CDN）
spk/                DSM 套件外壳（INFO、scripts、conf、ui、port_conf、图标）
build/              SPK 构建脚本与图标生成
docs/               方案、原型、打包部署、接口契约
qa/                 集成验收脚本与报告
dist/               构建产物（.spk 与 SHA256）
```

## 安全设计摘要

1. **绝不自动删除**：扫描完成只生成报告；删除需要用户选文件 → 生成计划 → 第一次清单确认 → 第二次最终确认。
2. **服务端二次校验**：删除前重新检查文件存在、大小/mtime/inode/dev 未变、保留文件仍在；精准组要求候选与保留文件全量哈希一致，相似组要求各自内容哈希与扫描记录一致。
3. **路径白名单/黑名单**：只允许共享文件夹子树；禁止系统目录、`@*`、`#recycle`、`#snapshot`、`@eaDir` 等。
4. **不跟随符号链接**；硬链接按 inode 去重，避免“删了不释放空间”。
5. **默认回收站**：移动到 `<共享文件夹>/#recycle/@duplicate-cleaner/<时间>/<相对路径>`；永久删除需额外输入“永久删除”。
6. **相似模式门禁**：图片/文档相似组默认不勾选、无组级全选、必须人工逐组复核并逐条选择；删除计划需 `confirm_similar=true` + 已复核组 ID。视频组默认不勾选，但允许手动全选批量进入计划；视频删除不走相似复核门禁，仍需两次确认。
7. **认证与审计**：DSM 管理员登录（`SYNO.API.Auth`），HttpOnly Cookie + CSRF；审计日志持久化。

## 已知限制

- DSM 7 不预装 Python 3，必须安装 Python3 套件。
- 创建时间依赖 `statx` btime；不支持 btime 时整组按修改时间（mtime）自动选择保留项，并显示“无创建时间 · 按修改时间保留”警示，仍需人工核对。
- 单次扫描默认最多 500,000 个文件；超大库建议分目录扫描。
- 默认 HTTP 仅适合可信局域网；公网/远程请使用 VPN 或 DSM 反向代理 + TLS。
- 非 root 运行：必须给系统内部用户 `duplicatecleaner` 授权共享文件夹；未授权时扫描/删除会提示权限不足。
- 加密共享文件夹需先在 DSM 解锁；套件不处理加密解锁。
- 本工具不判断业务语义；精准组只保证内容哈希一致，相似组只保证特征相似，均需人工确认可删。
- 相似模式不依赖云端 AI/网络，不上传文件；JPEG 感知哈希在未安装 Pillow 时降级为字节特征（低置信标记）。
- 图片/文档相似计算默认跳过压缩包、媒体、可执行文件、超大文件；这些文件仍参与哈希精准匹配。
- 视频相似需要开启总开关，单视频解码较慢；SPK 内置 FFmpeg 为 4.1 静态构建，硬解能力取决于 DSM 机型/内核；无硬解会自动回退软件解码。外部 FFmpeg 路径必须是存在且可执行的绝对路径；显式硬解模式若不被该 FFmpeg 支持会回退软件并标记 `hwaccel_fallback`。
- 视频相似不支持剧情语义、剪辑乱序、换脸/重绘伪原创；片段 vs 完整长视频不承诺。
- ASR 为可选外部命令（默认关闭）：需用户自行配置 whisper 等本地命令与模型；未配置时视频扫描仍按视觉指纹完成，不失败。
- 云端字幕默认关闭：启用后音频和字幕文本会发送到所配置的 ASR/LLM 服务；API Key 保存在 `var/settings.json`，GET 接口不回传明文；请只在可信网络/合规场景使用。
- 云端字幕为 OpenAI 兼容接口，不绑定厂商；ASR 模型必须返回带时间戳的 `verbose_json` 片段或 SRT，纯文本模型无法生成字幕。
- v0012 起扫描/组/文件/删除任务/特征缓存/字幕任务元数据存入 `<var>/dupcleaner.sqlite3`；设置、审计、运行日志、字幕文件仍保留文件形式。升级/备份时请连同 `dupcleaner.sqlite3` 与 `var/` 一起备份。
- v0014 起支持升级前暂停扫描并写断点；升级后需在扫描历史中手动点“继续扫描”，不会自动续跑。字幕任务仍会因重启失败，需要重新提交；删除任务不会续跑。
- v0025 起 SQLite schema v5：`scan_files` 不再冗余保存 payload（组 payload 为真源）；FTS5 trigram 路径搜索仅在 FTS5 + trigram 都可用时启用，否则自动 LIKE 回退；搜索词 <3 字符始终走 LIKE。
- ES modules 需要通过后端 HTTP 访问页面（`http://<NAS>:8787/`），不能直接双击 `web/index.html`（浏览器 `file://` 模块 CORS 限制）。

## License

MIT（见 `spk/LICENSE`）；数据安全声明同样包含在 LICENSE 中。
