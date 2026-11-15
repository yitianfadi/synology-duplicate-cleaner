# 文档索引与维护约定

> 本文是全部文档的入口。新增文档先在此登记；文档只写「当前生效的行为」，历史过程用结论 + 版本增量记录，详细流水交给 `git log` 和发布说明。

## 1. 按目标找文档

| 我想…… | 先读 | 再查 |
|---|---|---|
| 了解功能、安装、跑起来 | [README](../README.md) | [发布说明](RELEASE_NOTES_v1.1.0-0054.md) |
| 部署 / 升级 / 排错 | [SPK_PACKAGING.md](SPK_PACKAGING.md) | [ARCHITECTURE.md](ARCHITECTURE.md) §9、`<var>/log/start.log` |
| 看懂整体设计与数据流 | [ARCHITECTURE.md](ARCHITECTURE.md) §1–10 | [STORAGE_CONTRACT.md](STORAGE_CONTRACT.md) |
| 看懂页面与交互 | [UI_PROTOTYPE.md](UI_PROTOTYPE.md) | [CONTRACT.md](CONTRACT.md) §10 |
| 改后端接口 / 前端字段 | [CONTRACT.md](CONTRACT.md) | 对应特性的 `*_CONTRACT.md` |
| 改扫描、判重、保留、删除 | [CONTRACT.md](CONTRACT.md) §4–§7 | [增量扫描](INCREMENTAL_SCAN_CONTRACT.md)、[存储](STORAGE_CONTRACT.md)、[断点](RESUME_CONTRACT.md) |
| 改相似度 / 视频识别 | [SIMILARITY_CONTRACT.md](SIMILARITY_CONTRACT.md)、[VIDEO_CONTRACT.md](VIDEO_CONTRACT.md) | [相似视频 v2](VIDEO_MATCHING_V2_CONTRACT.md) |
| 改字幕（ASR / 翻译 / 批次） | [SUBTITLE_CONTRACT.md](SUBTITLE_CONTRACT.md) | [批量字幕](SUBTITLE_BATCH_CONTRACT.md)、[ASR v2](SUBTITLE_ASR_V2_CONTRACT.md) |
| 改下载（m3u8 / 直链 → MP4 / 网页解析） | [DOWNLOAD_CONTRACT.md](DOWNLOAD_CONTRACT.md) | [ARCHITECTURE.md](ARCHITECTURE.md) §21 |
| 改任务中心 / 权限 / 日志 | [TASK_CENTER_V2_CONTRACT.md](TASK_CENTER_V2_CONTRACT.md) | [CONTRACT.md](CONTRACT.md) §6 |
| 了解 v2 需求为什么这样做 | [REQUIREMENTS_V2_ASSESSMENT.md](REQUIREMENTS_V2_ASSESSMENT.md) | 各特性契约 |
| 看验收结论与证据 | [qa/REPORT.md](../qa/REPORT.md) | [qa/TRACEABILITY.md](../qa/TRACEABILITY.md)、`qa/*_results.json` |
| 看历史版本改了什么 | `git log` / GitHub Releases | [ARCHITECTURE.md](ARCHITECTURE.md) §11–21（演进附录） |

## 2. 文档清单

状态：**现行** = 描述当前行为，可直接依赖；**版本增量** = 某版本的追加契约，仍有效；**现行 + 版本增量** = 主体描述现行行为、末尾附版本增量章节；**历史** = 仅供追溯，不再更新。

### 2.1 入口与总体设计

| 文档 | 内容 | 状态 |
|---|---|---|
| [README.md](../README.md) | 功能概览、构建安装、开发联调、测试命令、已知限制 | 现行 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 组件与目录、扫描/哈希/时间/删除设计、群晖 API 调用点、性能；§11–20 为按版本的设计演进 | 现行 |
| [UI_PROTOTYPE.md](UI_PROTOTYPE.md) | 页面线框、文件夹选择器、结果页、两次确认流程、状态与验收要点 | 现行 |
| [SPK_PACKAGING.md](SPK_PACKAGING.md) | 依赖与前置条件、构建、安装、权限、TLS、日志、升级/卸载、排错 | 现行 |

### 2.2 前后端接口契约

| 文档 | 内容 | 状态 |
|---|---|---|
| [CONTRACT.md](CONTRACT.md) | 精准模式的唯一接口约定：服务/认证、通用响应、错误码、API、字段、安全规则 | 现行 |
| [SIMILARITY_CONTRACT.md](SIMILARITY_CONTRACT.md) | 内容相似模式：接口、特征算法、阈值、人工复核门禁 | 现行 |
| [VIDEO_CONTRACT.md](VIDEO_CONTRACT.md) | 视频视觉相似：FFmpeg、动态采样、指纹、DTW、缓存、批量删除 | 现行 |
| [STORAGE_CONTRACT.md](STORAGE_CONTRACT.md) | SQLite 元数据、旧 JSON 迁移、checkpoint、WAL、备份 | 现行 |
| [RESUME_CONTRACT.md](RESUME_CONTRACT.md) | 扫描暂停/继续、升级前 drain、重启后手动继续 | 现行 |

### 2.3 扫描增强（v0043–v0046）

| 文档 | 内容 | 状态 |
|---|---|---|
| [INCREMENTAL_SCAN_CONTRACT.md](INCREMENTAL_SCAN_CONTRACT.md) | 跨扫描哈希缓存、增量标记、保留规则链、`.ignore`/附属文件排除（v0043） | 版本增量 |
| [PREVIEW_PROTECTION_CONTRACT.md](PREVIEW_PROTECTION_CONTRACT.md) | 缩略图/媒体信息/并排对比、删除白名单（v0044） | 版本增量 |
| [MERGE_TRASH_CONTRACT.md](MERGE_TRASH_CONTRACT.md) | 硬链接合并的语义与限制、套件独立回收站与整组恢复（v0045） | 版本增量 |
| [NOTIFICATION_SCHEDULE_CONTRACT.md](NOTIFICATION_SCHEDULE_CONTRACT.md) | 计划扫描规则、负载跳过、DSM/Webhook/Telegram 通知（v0046） | 版本增量 |

### 2.4 相似视频与字幕

| 文档 | 内容 | 状态 |
|---|---|---|
| [VIDEO_MATCHING_V2_CONTRACT.md](VIDEO_MATCHING_V2_CONTRACT.md) | 指纹 v2、边缘裁剪、时间轴对齐、音频签名、置信度分档（v0047） | 版本增量 |
| [SUBTITLE_CONTRACT.md](SUBTITLE_CONTRACT.md) | 云端字幕核心：OpenAI 兼容 ASR/LLM、分块、SRT/VTT/双语、密钥与隐私；§9 自建 WhisperLiveKit；§12–22 版本增量 | 现行 + 版本增量 |
| [SUBTITLE_BATCH_CONTRACT.md](SUBTITLE_BATCH_CONTRACT.md) | 目录批量字幕、批次组、补翻译、预览/进度、取消与删除（v0032–v0050） | 现行 + 版本增量 |
| [SUBTITLE_ASR_V2_CONTRACT.md](SUBTITLE_ASR_V2_CONTRACT.md) | 失败分类/自动重试/失败清单、负载与优先级、音轨选择、目录监控（v0048） | 版本增量 |

### 2.5 任务中心、下载与运维（v0049–v0054）

| 文档 | 内容 | 状态 |
|---|---|---|
| [TASK_CENTER_V2_CONTRACT.md](TASK_CENTER_V2_CONTRACT.md) | 任务检索筛选、批量操作、权限只读体检、日志检索与导出（v0049） | 版本增量 |
| [SUBTITLE_BATCH_CONTRACT.md](SUBTITLE_BATCH_CONTRACT.md) §23 + [ARCHITECTURE.md](ARCHITECTURE.md) §20 | 批次组异步取消、取消剩余、强制删除、明细自动加载、已取消页签（v0050） | 版本增量 |
| [DOWNLOAD_CONTRACT.md](DOWNLOAD_CONTRACT.md) + [ARCHITECTURE.md](ARCHITECTURE.md) §21 | 直链/HLS 下载、AES-128 本地化解密、合并 MP4、批量组、队列与并发、设置键（v0051）；播放网页解析与 cURL 登录态暂存（v0052）；去重/自动重试/分时限速/命名模板/优先级/导出/空间看板/通知（v0053）；DASH 清单与订阅下载、连接复用（v0054） | 版本增量 |

### 2.6 交付、验收与历史

| 文档 | 内容 | 状态 |
|---|---|---|
| [REQUIREMENTS_V2_ASSESSMENT.md](REQUIREMENTS_V2_ASSESSMENT.md) | v2 需求 → 版本交付映射、关键取舍、明确不做、默认值、真机清单 | 现行（结论） |
| [RELEASE_NOTES_v1.1.0-0054.md](RELEASE_NOTES_v1.1.0-0054.md) | 未发布增量（v0051 下载中心 + v0052 网页解析 + v0053 下载增强 + v0054 DASH/订阅）、兼容性、打包与回退 | 现行（未打包） |
| [RELEASE_NOTES_v1.1.0-0053.md](RELEASE_NOTES_v1.1.0-0053.md) | 上一版未发布增量说明（v0051–v0053） | 历史 |
| [RELEASE_NOTES_v1.1.0-0050.md](RELEASE_NOTES_v1.1.0-0050.md) | 上一版正式发布说明（v0043–v0050 累计） | 历史 |
| [qa/REPORT.md](../qa/REPORT.md) | 当前 QA 基线、验收方法、需求追溯、偏差与残余风险、版本验收记录 | 现行 |
| [qa/TRACEABILITY.md](../qa/TRACEABILITY.md) | REQ-01…REQ-32 的验收口径与证据位（逐条） | 现行 |

## 3. 维护约定

1. **单一事实来源**：接口/字段/错误码只在 `CONTRACT.md` 与各特性契约里定义；README 只做概览，QA 报告只放基线与结论，不重复抄契约细节。
2. **版本增量写法**：新版本不重写旧章节，在对应契约末尾追加 `## N. vXXXX <主题>`，写清新增接口、默认值、兼容性与限制；同时更新 `docs/README.md` 的状态列。
3. **版本号同步 8 处**：`README.md`、`build/build_spk.py`、`docs/ARCHITECTURE.md`、`docs/CONTRACT.md`、`docs/SPK_PACKAGING.md`、`spk/INFO`、`src/backend/dupcleaner/config.py`、`src/web/index.html`（含 `app.js?v=`）。
4. **一次性文档**：需求评估、问题排查、临时方案只在交付后保留「结论 + 取舍 + 不做清单」，过程细节以 git 提交信息和发布说明为准，不再长期维护。
5. **验证文档**：`qa/REPORT.md` 只保留当前基线 + 历史版本记录表；逐条证据放 `qa/*_results.json`，重跑命令写在报告里。
6. **删除原则**：被新契约完全覆盖、且无引用的一版一文档可以直接删除；删除前先 `git grep` 确认没有代码/测试/docstring 引用路径。

## 4. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-10 | 建立本文档索引；压缩 `qa/REPORT.md` 与 `REQUIREMENTS_V2_ASSESSMENT.md`；重写会话交接 `SESSION_SUMMARY.md`；合并 README 功能/限制条目；归档并删除只对 `1.1.0-0003` 有效的旧 `qa/SIMILARITY_REPORT.md` |
| 2026-11-15 | 新增下载中心、网页解析、下载增强、DASH/订阅：`DOWNLOAD_CONTRACT.md`（§1–§17）+ `ARCHITECTURE.md` §21–§21.3 + `CONTRACT.md` §12；索引状态列更新（v0051–v0054） |
