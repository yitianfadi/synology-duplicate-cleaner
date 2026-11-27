# 文档索引

> 全部文档的入口。文档只写**当前生效的行为**；历史版本看 [CHANGELOG](CHANGELOG.md) 与 GitHub Releases，细节流水看 `git log`。

## 1. 30 秒找到你要的

| 我是…… | 先读 | 再查 |
|---|---|---|
| 第一次用 / 想装 | [README](../README.md) | [打包部署](SPK_PACKAGING.md) |
| 想按功能或关键词搜 | [功能总览（按功能/关键词检索）](FEATURES.md) | 对应 `*_CONTRACT.md` |
| 想知道某版本改了什么 | [CHANGELOG](CHANGELOG.md) | GitHub Releases |
| 改后端接口 / 前端字段 | [CONTRACT.md](CONTRACT.md) | 各特性契约 |
| 改扫描 / 判重 / 删除 | [CONTRACT.md](CONTRACT.md) §4–§7 | [增量扫描](INCREMENTAL_SCAN_CONTRACT.md)、[存储](STORAGE_CONTRACT.md)、[断点](RESUME_CONTRACT.md) |
| 改相似度 / 视频识别 | [SIMILARITY_CONTRACT.md](SIMILARITY_CONTRACT.md) | [VIDEO_CONTRACT.md](VIDEO_CONTRACT.md)、[VIDEO_MATCHING_V2_CONTRACT.md](VIDEO_MATCHING_V2_CONTRACT.md) |
| 改字幕（ASR / 翻译 / 批次） | [SUBTITLE_CONTRACT.md](SUBTITLE_CONTRACT.md) | [批量字幕](SUBTITLE_BATCH_CONTRACT.md)、[ASR v2](SUBTITLE_ASR_V2_CONTRACT.md) |
| 改下载（m3u8 / 直链 / 解析 / 订阅） | [DOWNLOAD_CONTRACT.md](DOWNLOAD_CONTRACT.md) | [ARCHITECTURE.md](ARCHITECTURE.md) §21 |
| 改格式转换 | [CONVERT_CONTRACT.md](CONVERT_CONTRACT.md) | [ARCHITECTURE.md](ARCHITECTURE.md) §22 |
| 改音视频合成 | [AV_MERGE_CONTRACT.md](AV_MERGE_CONTRACT.md) | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 看架构与数据流 | [ARCHITECTURE.md](ARCHITECTURE.md) | [STORAGE_CONTRACT.md](STORAGE_CONTRACT.md) |
| 看验收结论与证据 | [qa/REPORT.md](../qa/REPORT.md) | [qa/TRACEABILITY.md](../qa/TRACEABILITY.md)、`qa/*_results.json` |

## 2. 按功能 / 关键词搜

完整的功能→关键词→接口→设置→源码映射在 **[FEATURES.md](FEATURES.md)**（含中英文同义词，如「自动重试 / retry / backoff / 退避」「播放网页解析 / page parse / m3u8」）。
在 GitHub 上直接搜中文功能名或英文术语都能命中该文件与对应契约；仓库的 Description / Topics 也覆盖了主要关键词。

## 3. 文档清单

状态：**现行** = 描述当前行为；**现行 + 版本增量** = 主体现行、末尾附版本章节；**历史** = 仅追溯。

### 3.1 入口与总体

| 文档 | 内容 |
|---|---|
| [README.md](../README.md) | 功能概览、安装、开发联调、测试、安全摘要、已知限制 |
| [FEATURES.md](FEATURES.md) | **按功能/关键词检索的功能总览**：入口、接口、设置键、契约、源码位置 |
| [CHANGELOG.md](CHANGELOG.md) | 全部版本历史（当前 1.1.0-0068） |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 组件与目录、扫描/哈希/时间/删除设计、群晖 API 调用点、性能、按版本演进 |
| [SPK_PACKAGING.md](SPK_PACKAGING.md) | 依赖、构建、安装、权限、TLS、日志、升级/卸载、排错 |

### 3.2 接口与存储契约

| 文档 | 内容 |
|---|---|
| [CONTRACT.md](CONTRACT.md) | 服务/认证、通用响应、错误码、API、字段、安全规则（唯一接口事实来源） |
| [SIMILARITY_CONTRACT.md](SIMILARITY_CONTRACT.md) | 内容相似模式：特征算法、阈值、人工复核门禁 |
| [VIDEO_CONTRACT.md](VIDEO_CONTRACT.md) | 视频视觉相似：FFmpeg、采样、指纹、DTW、缓存、批量删除 |
| [VIDEO_MATCHING_V2_CONTRACT.md](VIDEO_MATCHING_V2_CONTRACT.md) | 视频指纹 v2、裁剪、时间轴对齐、音频签名、置信度分档 |
| [STORAGE_CONTRACT.md](STORAGE_CONTRACT.md) | SQLite 元数据、迁移、checkpoint、WAL、备份 |
| [RESUME_CONTRACT.md](RESUME_CONTRACT.md) | 扫描暂停/继续、升级前 drain、重启后继续 |
| [INCREMENTAL_SCAN_CONTRACT.md](INCREMENTAL_SCAN_CONTRACT.md) | 哈希缓存、增量标记、保留规则链、`.ignore`/附属文件排除 |
| [PREVIEW_PROTECTION_CONTRACT.md](PREVIEW_PROTECTION_CONTRACT.md) | 缩略图/媒体信息/并排对比、删除白名单 |
| [MERGE_TRASH_CONTRACT.md](MERGE_TRASH_CONTRACT.md) | 硬链接合并语义与限制、套件回收站与整组恢复 |
| [NOTIFICATION_SCHEDULE_CONTRACT.md](NOTIFICATION_SCHEDULE_CONTRACT.md) | 计划扫描规则、负载跳过、Webhook/Telegram 通知 |

### 3.3 业务模块契约

| 文档 | 内容 |
|---|---|
| [SUBTITLE_CONTRACT.md](SUBTITLE_CONTRACT.md) | 云端字幕核心：OpenAI 兼容 ASR/LLM、分块、SRT/VTT/双语、密钥与隐私 |
| [SUBTITLE_BATCH_CONTRACT.md](SUBTITLE_BATCH_CONTRACT.md) | 目录批量字幕、批次组、补翻译、预览、取消与删除 |
| [SUBTITLE_ASR_V2_CONTRACT.md](SUBTITLE_ASR_V2_CONTRACT.md) | 失败分类/自动重试、负载与优先级、音轨选择、目录监控 |
| [DOWNLOAD_CONTRACT.md](DOWNLOAD_CONTRACT.md) | 直链/HLS/DASH 下载、解析、订阅、去重、重试、命名、队列 |
| [CONVERT_CONTRACT.md](CONVERT_CONTRACT.md) | 格式转换：预设、队列、覆盖/删源、分批、分页、硬件编码 |
| [AV_MERGE_CONTRACT.md](AV_MERGE_CONTRACT.md) | 音视频合成：有序拼接、混流、时长规则、安全 |

### 3.4 验收

| 文档 | 内容 |
|---|---|
| [qa/REPORT.md](../qa/REPORT.md) | 当前 QA 基线、验收方法、需求追溯、残余风险、逐版本验收记录 |
| [qa/TRACEABILITY.md](../qa/TRACEABILITY.md) | REQ-01…REQ-32 的验收口径与证据位 |

## 4. 维护约定

1. **单一事实来源**：接口/字段/错误码只在 `CONTRACT.md` 与各特性契约定义；README 只做概览；QA 报告只放基线与结论。
2. **新功能先登记**：先在 `FEATURES.md` 增加一行（功能 / 关键词 / 接口 / 设置 / 契约），再写契约章节。
3. **版本增量写法**：不重写旧章节，在对应契约末尾追加 `## N. vXXXX <主题>`；同时在 `CHANGELOG.md` 加一条。
4. **版本号同步 8 处**：`README.md`、`build/build_spk.py`、`docs/ARCHITECTURE.md`、`docs/CONTRACT.md`、`docs/SPK_PACKAGING.md`、`spk/INFO`、`src/backend/dupcleaner/config.py`、`src/web/index.html`（含 `app.js?v=`）。
5. **删除原则**：被新契约完全覆盖、且无代码/测试/docstring 引用的文档可以直接删除；删除前 `git grep <文件名>` 确认，删后同步本索引与 `FEATURES.md` 链接。
6. **一次性材料**（评估、排查、临时方案）只保留结论，过程交给 `git log` 与 GitHub Releases，不合入 `docs/`。

## 5. 文档变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-26 | 文档重构：新增 [FEATURES.md](FEATURES.md)（按功能/关键词检索）与 [CHANGELOG.md](CHANGELOG.md)；删除 `UI_PROTOTYPE.md`、`TASK_CENTER_V2_CONTRACT.md`（v0057 已退役）、`REQUIREMENTS_V2_ASSESSMENT.md` 与 17 篇逐版本 `RELEASE_NOTES_*.md`（要点并入 CHANGELOG）；同步修正 README 的过期描述（FFmpeg 8.1 / schema v11 / 转换默认值 / 导航含「合成」） |
| 2026-11-22 | v0067：下载指数退避重试；`DOWNLOAD_CONTRACT.md` §15.2、`ARCHITECTURE.md` §21.2 更新 |
| 2026-11-20 | v0066：字幕队列设置回到任务页；`SUBTITLE_*.md` 更新 |
| 2026-11-19 | v0064/v0065：`DOWNLOAD_CONTRACT.md` 压缩响应与标题命名、`CONVERT_CONTRACT.md` 硬件编码 |
