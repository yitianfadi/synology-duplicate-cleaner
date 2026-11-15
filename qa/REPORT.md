# DuplicateCleaner SPK 质量验收报告

| 项 | 值 |
|---|---|
| 项目 | 重复文件清理与字幕 / DuplicateCleaner（DSM 7 SPK） |
| 套件 ID | `DuplicateCleaner` |
| 当前版本 | 1.1.0-0050 |
| 最近更新 | 2026-11-10 |
| 验收主机 | Windows 本地 |
| 验收结论 | PASS（发布基线） |
| 覆盖说明 | v0050 已于 2026-11-10 在 DSM 真机完成 1.1.0-0042 → 1.1.0-0050 覆盖升级；v0043–v0050 功能项仍待逐条真机验证 |

> 本文件只保留当前有效结论与证据索引。逐版本需求复述、实现细节与踩坑长文已删除；
> 需要追溯时见 `docs/README.md`（文档索引）、`docs/*_CONTRACT.md`、`docs/REQUIREMENTS_V2_ASSESSMENT.md` 与 `git log`。

## 1. 当前基线

最新一次全量验收集：1.1.0-0053（**源码级**，按用户要求未打包；`SPK-DIST` 为 UNVERIFIED，`SPK-SYNC` 等构建后补跑）。

| 套件 | 命令 | 结果 |
|---|---|---|
| 后端单元测试 | `cd src/backend && python -m unittest discover -s tests` | **732 OK（5 skipped）** |
| 静态检查 | `python qa/static_checks.py` | **199 PASS / 0 FAIL / 1 UNVERIFIED**（UNVERIFIED = SPK-DIST 未构建） |
| 端到端 | `python qa/e2e_test.py` | **221 PASS / 0 FAIL / 1 UNVERIFIED**（Windows symlink 历史项；含 DL-00…DL-12、RESTART-08） |
| 前端模块图 | `node qa/check_web_modules.mjs` | **25/25 PASS** |
| 无头浏览器冒烟 | `python qa/web_smoke.py` | 8/8 PASS（最近一次 static 内联执行） |
| 真实 Chromium 探针 | `.uiprobe/probe_v0043..v0050.mjs` | v0043–v0050 全通过；v0051–v0053 未加新探针 |

| 产物项 | 值 |
|---|---|
| 当前源码版本 | `1.1.0-0053`（未打包） |
| dist 中的产物 | `dist/DuplicateCleaner-1.1.0-0051.spk` 39,168,000 B，SHA256 `c1103858…bce33d37f`（= INFO.checksum `80a42f24…`） |
| SQLite schema | v9（v0051 新增 download_jobs / download_groups；v0052/v0053 无迁移） |
| 版本同步 | 1.1.0-0053 已同步 8 处源码/文档；待打包后由 static 兜底校验 |

判定口径：

- PASS：有可复现证据（脚本 + JSON 记录 + 产物）。
- FAIL：有具体复现步骤与失败记录；当前基线为 0。
- UNVERIFIED：当前环境无法构造（仅 Windows symlink 一项）。
- 探针、冒烟、模块图不替代 e2e/static，仅作为浏览器层补充证据。

### 1.1 基线变化（v0042 → v0053）

| 指标 | v0042 | v0050 | v0051 | v0052 | v0053（未打包） |
|---|---|---|---|---|---|
| unittest | 459（skip 6） | 666（skip 5） | 694（skip 5） | 713（skip 5） | 732（skip 5） |
| static | 169 PASS | 197 PASS | 207 PASS | 未执行 | 199 PASS / 1 UNVERIFIED |
| e2e | 157/1 U | 207/1 U | 214/1 U | 217/1 U | 221/1 U |
| SQLite schema | v7 | v8 | v9 | v9 | v9（无迁移） |
| SPK 大小 | 39,014,400 B | 39,127,040 B | 39,168,000 B | 未打包 | 未打包 |
| 浏览器探针 | 真机 1 组 | 本地 8 组（v0043–v0050） | 同左 | 同左 | 同左（模块图 + e2e 覆盖） |

v0043 是分界点：schema 升到 v8、引入增量哈希缓存；v0044–v0050 均为增量能力；v0051 新增下载中心并升到 v9；v0052 新增播放网页解析；v0053 下载增强（均无 schema 变化）。

### 1.2 验收范围

| 范围 | 内容 |
|---|---|
| 纳入 | 浏览器可访问的全部 API 与页面逻辑（dev 后端 + 真实 Chromium） |
| 纳入 | 扫描、判重、保留、删除、回收站、报告主链路 |
| 纳入 | 字幕流水线（本地假 ASR，字段与状态机同真实服务） |
| 纳入 | v2 能力：增量缓存、白名单、合并、套件回收站、通知、计划扫描、相似视频、任务检索、批量取消 |
| 纳入 | SPK 包结构、权限位、生命周期脚本、checksum 与源码同步 |
| 不纳入 | DSM 真机运行时（v0042、v0050 各做过一次覆盖升级；功能逐项未系统验证） |
| 不纳入 | Btrfs/FICLONE 实际行为与空间回收 |
| 不纳入 | 真实 ASR/LLM 服务压测与出网通知 |
| 不纳入 | 50 万文件 / 10GB 级规模与性能曲线 |
| 不纳入 | 浏览器兼容矩阵（只用 Chromium 系） |

## 2. 验收方法

所有套件在 Windows 本地执行：e2e 启动真实 dev 后端进程并使用临时共享根；static 同时检查源码与 SPK 产物；
浏览器类用例使用真实 Chromium。DSM 真机在 v0042、v0050 做过覆盖升级实测；v0043–v0049 未单独覆盖升级。

### 2.1 端到端（e2e）

- 环境：真实 dev 后端 + 临时共享根；Windows 用 `SetFileTime` 精确设置创建时间（btime），保证保留策略与时间排序可判定。
- 样本：两个临时共享根，覆盖同内容不同名、同大小不同内容、跨根重复、3 副本创建顺序、mtime 反转、硬链接、
  扫描后修改、空/隐藏/系统目录（`@eaDir`/`#snapshot`/`#recycle`）、`.JPG` 大小写、`.tmp` 排除、大小区间、4MB 大文件。
- 生命周期：脚本自动创建/清理临时目录与服务，不残留后台进程或临时目录。
- 判重与保留：断言 keep 结果、`keep_reason`、`source=birthtime`，并用 mtime 反转证明未用 mtime 冒充。
- 删除：回收站路径、永久删除无残留、keeper 保护、计划重放拒绝、回收站不可用跳过。
- 字幕：批次组/拆分/补翻译/失败清单/取消，均用本地假 ASR 或 dev 后端，不依赖外网。
- v2 能力：合并与套件回收站、通知（本地 Webhook 接收器）、计划扫描、任务检索、权限体检。
- 失败注入：删除前修改文件、目标已不存在、回收站不可写（构造 409 RECYCLE_UNAVAILABLE）。
- 并发：扫描中删除、扫描中暂停/停止、进程重启后任务恢复。
- 通知与计划：Webhook 接收器断言 payload；计划任务手工触发 tick，不真等周期。
- 记录：`qa/e2e_results.json` 逐条含 `id`、结论与说明；`1 UNVERIFIED` 固定为 symlink 用例。

### 2.2 静态检查（static）

- 后端：全部 `.py` 做 `py_compile` 与 `ast feature_version=(3,8)`；仅标准库；扫描路径无删除调用；路由与端点覆盖。
- 前端：`node --check`；无 CDN、ES2019、全中文、`textContent` 安全 DOM、风险文案常驻且不可关闭、两次确认。
- 端点覆盖：契约端点与前端调用逐一比对（v0048 起含字幕 v2、通知、调度、缓存、日志检索）。
- SPK 结构：INFO 必需字段/取值、`package.tgz` 条目、权限位、生命周期脚本、`run-as=package`。
- SPK 洁净性：无 `tests/`、`__pycache__`、`.pyc`、`.log`、`rerun.txt`、dev 变量与密钥。
- 一致性：包内 app/web/ui/port_conf 与工作区 SHA256 一致（SPK-SYNC）；gzip/tar 元数据可复现。
- 失败即整轮 FAIL，不跳过、不降级为警告；缺端点必须显式报 UNVERIFIED 而非默认通过。
- SPK 检查基于解包后的真实文件，不读构建输入，避免“源码正确但包内过期”。
- 记录：`qa/static_results.json`。

### 2.3 后端单元测试

`cd src/backend && python -m unittest discover -s tests`，覆盖范围：

- 存储与迁移：schema 版本、表/索引、历史数据兼容。
- 扫描：分片哈希、缓存命中、保留规则链、排除规则、暂停/停止、增量标记。
- 删除：重校验、回收站/永久、合并、套件回收站恢复、计划过期、失败分类。
- 字幕与自动化：各阶段执行、补翻译、批量取消、通知、调度、任务检索。
- 断言风格：时间注入而非真实等待，避免时间相关 flaky。

### 2.4 前端模块图与无头冒烟

- `node qa/check_web_modules.mjs` 校验模块 import/导出关系，防止拆分后引用断裂（22/22）。
- `python qa/web_smoke.py` 用无头浏览器加载页面并断言关键 DOM（导航、风险提示、主要面板），共 8 项。
- 两者只做结构与加载检查，交互逻辑由 Playwright 探针与 e2e 覆盖。

### 2.5 Playwright 探针

- 真实 Chromium（CDP）连 dev 后端，必要时连 NAS 套件；v0038 起每版至少一组探针。
- 覆盖：设置回填/保存、扫描选项、结果页徽标、任务中心、日志、权限体检、批次取消与滚动加载。
- 探针会持久化设置，重复执行时断言需兼容“已启用”与“未启用”两种初始状态。
- 脚本与截图在 `.uiprobe/`；v0043–v0050 的通过数见第 6 节版本表。
- 探针不等同于人工验收：不覆盖真实 DSM 内嵌浏览器与真实媒体库。

### 2.6 Python 3.8 与 DSM 兼容性

- 后端按 Python 3.8 语法校验（v0043 时对 `dupcleaner/` 与 `tests/` 共 52 个 `.py` 做 `ast.parse(feature_version=(3,8))`）。
- 生命周期脚本 `#!/bin/sh`，Git Bash 下 `bash -n` 全通过，行尾 LF。
- SPK 以 `run-as=package` 运行，用户/组为 `duplicatecleaner`，不依赖 root。
- Windows 无 `os.getloadavg` 时负载阈值视为不限制（真机 DSM 有效）。

### 2.7 通用工程约束（历史教训，仍需遵守）

- 批量取消必须在同一把锁内一次性重建队列并只唤醒一次，否则 worker 会抢跑未取消任务。
- `el(tag, opts, children)` 不支持 `style`；进度条宽度须用 `node.style.width` 设置。
- e2e 不要提前往共享根写测试文件，会污染后续扫描的 STATS/SAFETY 统计。
- Playwright 点确认框按钮要限定 `#modalRoot button`，避免命中被遮罩的同名按钮。
- 合并/回收站清单必须先落盘再暴露终态，否则轮询到 completed 立即恢复会 404。

### 2.8 各套件的证明边界

| 套件 | 能证明 | 不能证明 |
|---|---|---|
| unittest | 分支逻辑、错误码、存储与迁移 | 真实 HTTP/浏览器行为 |
| static | 代码规范、端点覆盖、SPK 包正确性 | 运行时行为 |
| e2e | 真实后端全链路、文件系统副作用 | 真机 DSM 与真实媒体库 |
| 模块图 | 前端模块引用完整 | 交互正确性 |
| web_smoke | 页面可加载、关键 DOM 存在 | 业务流程 |
| Playwright 探针 | 真实浏览器交互与请求形状 | 长时稳定性与真机差异 |

单套件通过不等于整体 PASS；本报告结论取六层同时通过。

## 3. 需求追溯

状态口径：PASS=有可复现证据；FAIL=有具体复现；UNVERIFIED=环境无法验证。证据 ID 对应两份结果 JSON 的 `id`。
REQ 编号沿用 v1.0 基线，状态按当前版本事实更新。

| ID | 需求 | 状态 | 关键证据 |
|---|---|---|---|
| REQ-01 | 仅按内容哈希判重，不按文件名/大小/时间 | PASS | E2E REQ-HASH-ONLY、HASH-01、GROUP-02 |
| REQ-02 | 默认只保留创建时间最晚 | PASS | E2E KEEP-01（keep=photo3，latest_created） |
| REQ-03 | 创建时间不能用 mtime/ctime 冒充 | PASS | E2E KEEP-02/03；static BE-STATX、BE-CTIME |
| REQ-04 | 大文件分片 + 限速 + 并发 | PASS（功能） | E2E SCAN-BIG、PAUSE；`hashing.py` 1 MiB 分片 + TokenBucket；未做吞吐基准 |
| REQ-05 | 文件夹多选 + 排除 | PASS | E2E SHARES-01、BROWSE-MULTIROOT、EXCLUDE-01/02 |
| REQ-06 | 分组展示/保留高亮/预览/全选/CSV | PASS | E2E GROUP-01..08、CSV-01..05；static FE-KEEP-HIGHLIGHT/FE-PREVIEW/FE-CSV |
| REQ-07 | 绝不静默/自动删除 | PASS | E2E SAFETY-01（扫描前后 46 文件一致）、DEL-01/02 |
| REQ-08 | 报告统计 | PASS | E2E STATS-01/02（wasted=size*(count-1)） |
| REQ-09 | 删除两次确认 | PASS | static FE-CONFIRM-1/2；E2E DEL-05/06 |
| REQ-10 | 默认回收站，可选永久删除 | PASS | E2E DEL-04/08/10、DEL-13/15/16 |
| REQ-11 | 操作日志持久化 | PASS | E2E AUDIT-01..03、LOGS-01..03、RESTART-01..07 |
| REQ-12 | DSM 7 / Btrfs / EXT4 兼容 | PASS（静态）/ UNVERIFIED（真机） | static SPK-INFO-VALUES、BE-PY38；真机仅完成 v0050 覆盖升级，兼容性未逐项验证 |
| REQ-13 | 跳过系统目录/符号链接/空/隐藏/后缀 | PASS / 1 UNVERIFIED | E2E SKIP-01..03、FILT-04/05；SKIP-04 见 D2 |
| REQ-14 | 暂停/继续/停止 | PASS | E2E PAUSE-01..05（停止后 resume 返回 400） |
| REQ-15 | 保留最早 / 指定路径 | PASS | E2E FILT-01/02 |
| REQ-16 | 大小区间 | PASS | E2E FILT-06 |
| REQ-17 | 后缀忽略大小写 | PASS | E2E FILT-03/04 |
| REQ-18 | 顶部常驻风险提示（精确文案） | PASS | E2E STATIC-01；static FE-RISK-TEXT、FE-RISK-PERSISTENT、SPK-PKG-RISK |
| REQ-19 | 仅管理员登录，不保存密码 | PASS | E2E AUTH-01..05；static SPK-NO-DEV、SPK-NO-DEV-SET |
| REQ-20 | 删除前逐文件重校验 | PASS | E2E DEL-01/03/11/12 |
| REQ-21 | 路径白名单/黑名单/防遍历 | PASS | E2E SEC-01..08、BROWSE-04/05 |
| REQ-22 | 硬链接按 (dev,ino) 去重 | PASS | E2E HARDLINK-01 |
| REQ-23 | SPK 打包正确、无 dev/测试/pycache | PASS | static SPK-* 36 项；解包与独立构建可复现 |
| REQ-24 | 低影响（nice/ionice/单任务/分页） | PASS | E2E PAUSE-02；static SPK-NICE、SPK-HEALTH |
| REQ-25 | 设置持久化 + 白名单校验 | PASS | E2E SET-01..07、RESTART-04 |
| REQ-26 | 前端无 CDN/无构建/ES2019/全中文/安全 DOM | PASS | static FE-NODECHECK、FE-ES2019、FE-TEXTCONTENT、FE-CHINESE |
| REQ-27 | 预览安全（共享内文件/Range/截断/415） | PASS | E2E PREVIEW-01..06、SEC-05/06/08（文本 Range 已修复） |
| REQ-28 | 日志下载白名单/防穿越 | PASS | E2E LOGS-03、SEC-07 |
| REQ-29 | 后台删除任务、逐文件结果、重启可查 | PASS | E2E DEL-07/08/15、RESTART-06 |
| REQ-30 | 卸载/升级保留 var 日志 | PASS | static SPK-KEEP-VAR、SPK-POSTINST |
| REQ-31 | 回收站不可用时不静默降级 | PASS（偏差 D1） | E2E DEL-12（全部跳过并列出 recycle_unavailable） |
| REQ-32 | 扫描只出报告、不触发删除 | PASS | E2E SAFETY-01；static BE-NO-AUTODELETE |

合计：30 项 PASS；REQ-12 真机部分、REQ-13 symlink 子项为 UNVERIFIED；REQ-31 含已记录偏差；无 FAIL。

### 3.1 v2 需求 → 交付版本

| 交付 | 主题 | 范围 | 关键测试证据 |
|---|---|---|---|
| v0043 | 增量/保留规则/排除规则 | 跨扫描哈希缓存 `hash_cache`（schema v8）、增量标记、保留规则链、正则/glob/`.ignore`/附属文件 | `test_v0043_incremental.py` 25 例；INC-01..04、KEEP-01、CACHE-01..04 |
| v0044 | 预览增强 + 删除白名单 | Range 流式预览、缩略图/媒体信息/并排对比、白名单扫描期/计划期/执行期三层生效 | `test_v0044_preview.py` 12 例、`test_v0044_protection.py` 24 例；PRT-01..04、THUMB-01..04 |
| v0045 | 硬链接合并 + 套件回收站 | 精准组合并（默认关）、`#duplicate-cleaner-trash/` 独立回收站与整组恢复 | `test_v0045_merge.py` 10 例、`test_v0045_trash.py` 7 例；MERGE-01..04、TRASH-00..03 |
| v0046 | 计划扫描 + 通知 | daily/weekly/interval 规则、负载与忙碌跳过、DSM/Webhook/Telegram 五类事件 | `test_v0046_notify.py` 22 例、`test_v0046_scheduler.py` 13 例；NOTIFY-00..03、SCHED-00..04 |
| v0047 | 相似视频 v2 | 内容裁剪指纹、±3 帧偏移对齐、可选音频签名、置信度分档 | `test_v0047_video.py` 25 例；VIDEO-01/02 |
| v0048 | 字幕 ASR 优化 + 监控 | 失败分类/自动重试/失败清单/负载阈值/优先级/ETA/音轨选择/目录监控 | `test_v0048_subtitles_v2.py` 16 例；SUBF-01..06 |
| v0049 | 任务检索/权限体检/日志 | 筛选 + 批量操作、只读权限体检与 DSM 指引、日志检索/导出 | `test_v0049_taskcenter.py` 17 例；SUBJ-01/02、PERM-01、LOGS-01/02 |
| v0050 | 批次取消/删除/明细增强 | 两阶段批量取消、组删除与强制删除、滚动自动加载、`cancelled` 独立页签 | `test_v0050_subtitle_cancel.py` 7 例；CANC-01/02、DETAIL-01 |

### 3.2 评估为“做不到 / 必须改法”的项

| 原始诉求 | 评估结论 | 当前落地 |
|---|---|---|
| 权限一键配置（改 ACL） | 套件非 root，无法改 ACL | 只读体检 + DSM 操作指引（v0049） |
| 增量扫描“仅对比新增/修改文件” | 会漏检跨时间重复 | 哈希缓存复用 + `new/changed` 标记（v0043） |
| 相似视频“不同剪辑版本” | 不可承诺 | 置信度分档 + 人工复核（v0047） |
| 缩略图/片段播放 | 受浏览器编解码限制 | 服务端 FFmpeg 抽帧 JPEG、原生 video 播放（v0044） |
| 硬链接合并 | 第二份 inode 不可逆 | 默认关闭、逐组确认、可恢复为独立副本（v0045） |
| 实时监控 | 标准库无 inotify | 轮询（30s tick + 配置间隔）（v0048） |
| 跨设备移动进回收站 | 只能同卷 rename | `CROSS_DEVICE` 拒绝分支 |

## 4. 契约偏差与已知限制

| 编号 | 类型 | 状态 | 说明 |
|---|---|---|---|
| D1 | 契约偏差 | 有效（低危） | 全部候选均因回收站不可用被跳过时，delete-plan 返回 **409 RECYCLE_UNAVAILABLE**（契约 §7 写 200 + plan）；不删除任何文件，前端已映射中文提示，安全不受影响。 |
| D2 | 环境限制 | 有效 | Windows 无 symlink 权限（`WinError 1314`），e2e `SKIP-04` 为 1 条 UNVERIFIED；后端拒绝逻辑与单测已在支持平台覆盖，需 Linux/DSM 补测。 |
| D3 | 设计确认 | 有效（非缺陷） | `needs_manual_keep=true` 的组整组禁删（含 `scope=all_duplicates`），前端整组禁用勾选并提示；比“让用户手选 keeper”更保守。 |
| D4–D6 | 已修复 | 关闭 | 文本预览 Range（200→206）、unittest 时间相关 flaky、SPK 混入 `app/rerun.txt`；最终回归均通过。 |
| D7 | 契约歧义（已验证自洽） | 有效（低危） | 统计字段：`reclaimable_bytes` 只累计可安全删除的组（`unsafe` 组不计入），另有 `selectable_duplicate_files`/`selectable_reclaimable_bytes`；E2E STATS-01/02 验证其与 groups/wasted 自洽，契约未写明 unsafe 组口径，建议补充说明。 |

处理建议：

- D1：下个版本二选一——实现 200 + plan（跳过原因放 `details`），或修订契约 §7 明确 409；当前保持实现不变。
- D2：在 Linux/DSM 上补跑 `SKIP-04`，并确认删除重校验对 symlink 路径仍然拒绝。
- D3：保持现状；若后续开放“手选 keeper”，需同时放开组级删除并补 e2e 防止误删。
- D7：在契约 §7 补一句统计口径（unsafe 组是否计入）即可，实现无需改动。

## 5. 残余风险与未验证项

| 项 | 状态 | 说明 |
|---|---|---|
| 真机 DSM 升级/启动 | 已验证（v0050） | 2026-11-10 由 1.1.0-0042 覆盖升级到 1.1.0-0050（用户确认）；v0043–v0049 未单独覆盖升级；排查入口 `<var>/log/start.log`。 |
| 真机功能覆盖 v0043–v0050 | 未验证（已有 v0050 环境） | 功能证据仍来自 Windows 本地 + dev 后端；2026-11-10 起可在 NAS 上逐项走查。 |
| Btrfs 合并/FICLONE | 未验证 | 合并默认关闭；恢复优先 `FICLONE`，非 Btrfs 回退普通复制。 |
| DSM 通知/计划扫描 | 未验证 | `synodsmnotify` 弹窗、Webhook/Telegram 出网、负载阈值取值。 |
| 性能规模 | 未压测 | 50 万文件上限、10GB 级大文件限速曲线、首扫指纹 CPU 开销。 |
| 浏览器真机走查 | 部分覆盖 | v0038 起每版有 Playwright 探针，但未在 DSM 内嵌浏览器人工走查。 |
| 跨文件系统回收站 | 未验证 | 单卷测试；代码有 `CROSS_DEVICE` 拒绝分支。 |
| 权限前置 | 需管理员操作 | `duplicatecleaner` 系统用户需在 DSM 共享文件夹 ACL 授权，未授权时扫描/删除安全跳过。 |

### 5.1 上线前建议

1. 在已升级的 1.1.0-0050 上确认套件中心无“无法启动”，`start.log` 含 `started in Ns`。
2. 真机走查 v0043–v0050：增量缓存、保留规则、白名单、合并、回收站、通知、计划扫描、失败面板、批量取消。
3. Btrfs 上确认合并释放空间、`FICLONE` 恢复耗时、跨卷合并被跳过。
4. 配置 DSM 通知/Webhook/Telegram 并各发一条测试消息，确认中文标题与截断。
5. 用真实媒体库采样 50 万文件与 10GB 文件的扫描耗时、内存与限速曲线。
6. 用 DSM 内置浏览器人工走查两次确认、文件夹选择器、预览弹窗、滚动加载。
7. 确认 ACL 授权（含 `#recycle`/套件回收站写权限）与只读目录的警告降级。
8. 记录首扫 `bytes_hashed` 与耗时，作为后续增量扫描收益的对照基线。

### 5.2 已知低风险项

| 项 | 说明 |
|---|---|
| 删除计划过期 | 5 分钟过期仅由 `test_plan_expires` 单测覆盖，e2e 未真等。 |
| TOCTOU | `revalidate` 与 `rename/unlink` 之间仍有极短窗口，单用户套件场景可接受。 |
| 构建来源 | 最终产物由 Lead 构建；QA 通过独立双构建 bit-identical 证明 dist 与源码一致。 |
| 开发遗留 | dev 默认口令与调试开关仅在开发环境启用，SPK 检查项 `SPK-NO-DEV*` 兜底。 |

## 6. 版本验收记录

| 版本 | 日期 | 主题 | unittest | static | e2e（P/F/U） | 探针 | SPK SHA256 前缀 |
|---|---|---|---|---|---|---|---|
| v0003 | 2026-11-02 | 安装权限修复 + 相似度复验 | 139（skip 2） | 111 | 113/0/1 | — | `bf526098…2e763a8d` |
| v0028/0029 | 2026-11-05 | 目录批量字幕 + 写回可见性 | 372（skip 5） | 126 | 123/0/1 | — | `c2729457…81373e87` |
| v0030 | 2026-11-07 | 本地 ASR/LLM 接入 + 禁用思考 | 373（skip 5） | 129 | 123/0/1 | — | `b5cf8a6c…2acc8aac` |
| v0031 | 2026-11-07 | 字幕提交按钮可用性修复 | 373（skip 5） | 130 | 123/0/1 | 无头复现 | `a4f040ba…e8a8046f` |
| v0032 | 2026-11-07 | 批次组/删除后刷新/服务器解码 | 382（skip 5） | 136 | 130/0/1 | — | `cf8fcef3…23f4c305` |
| v0033 | 2026-11-07 | 默认只生成 SRT（VTT 可选） | 383（skip 5） | 138 | 130/0/1 | — | `e683ef80…bdd4c8e7` |
| v0034 | 2026-11-07 | 队列恢复/并发/任务可见性 | 397（skip 5） | 144 | 135/0/1 | — | `70730e2a…577c45a1` |
| v0035 | 2026-11-07 | 补翻译（ASR-only 任务） | 412（skip 5） | 149 | 142/0/1 | — | `b8fd35be…40fa9e6c` |
| v0036 | 2026-11-07 | 语言码/译文输出组合修复 | 419（skip 5） | 152 | 146/0/1 | — | `bf849585…94d29903` |
| v0037 | 2026-11-07 | 字幕并发保存后立即生效 | 422（skip 5） | 152 | 146/0/1 | — | `566b5055…8966c8fd` |
| v0038 | 2026-11-07 | 步骤时间线/日志删除/菜单 | 444（skip 5） | 157 | 152/0/1 | 13/13 | `17dc49d8…450ab344` |
| v0039 | 2026-11-07 | 队列视图与批次总览 | 448（skip 5） | 160 | 154/0/1 | 10/10 | `fad950c5…51f5226d` |
| v0040 | 2026-11-07 | 分片音频预取 | 452（skip 5） | 162 | 157/0/1 | — | `6611ee65…89837fd2` |
| v0041 | 2026-11-08 | 任务行进度与预计剩余 | 456（skip 6） | 167 | 157/0/1 | — | `7345c642…6b4e41b9` |
| v0042 | 2026-11-08 | 升级无法启动根治 | 459（skip 6） | 169 | 157/0/1 | 真机（无计数） | `f769f08e…b50866a1` |
| v0043 | 2026-11-08 | 增量扫描/保留规则/排除规则 | 525（skip 5） | 178 | 166/0/1 | 10/10 | `4984be13…64f32437` |
| v0044 | 2026-11-08 | 预览增强 + 删除白名单 | 549（skip 5） | 182 | 174/0/1 | 8/8 | `97a69074…ab38b35f` |
| v0045 | 2026-11-08 | 硬链接合并 + 套件回收站 | 566（skip 5） | 186 | 182/0/1 | 13/13 | `c5458027…f649f822` |
| v0046 | 2026-11-08 | 计划扫描 + 通知系统 | 601（skip 5） | 189 | 191/0/1 | 8/8 | `26893c3f…6f4a1ce3` |
| v0047 | 2026-11-08 | 相似视频识别增强 | 626（skip 5） | 191 | 193/0/1 | 7/7 | `ce03b857…b54519be` |
| v0048 | 2026-11-08 | 字幕 ASR 优化 + 目录监控 | 642（skip 5） | 193 | 199/0/1 | 6/6 | `aa13edb4…5caae491` |
| v0049 | 2026-11-08 | 任务检索/权限体检/日志 | 659（skip 5） | 195 | 204/0/1 | 11/11 | `000cd592…3e2aab54` |
| v0050 | 2026-11-08 | 批次取消/删除/明细增强 | 666（skip 5） | 197 | 207/0/1 | 8/8 | `3c7b8a68…8a4c3c94` |
| v0051 | 2026-11-15 | 下载中心（直链 / HLS → MP4） | 694（skip 5） | 207 | 214/0/1 | — | `c1103858…bce33d37f` |
| v0052 | 2026-11-15 | 播放网页解析（未打包） | 713（skip 5） | —（未打包） | 217/0/1 | — | 未打包 |
| v0053 | 2026-11-15 | 下载增强（去重/重试/分时限速/命名/批量/看板，未打包） | 732（skip 5） | 199/0/1 | 221/0/1 | — | 未打包 |

说明：

- 表内数字均为对应版本最终产物的验收结果；SHA256 前缀为前 8 位 + `…` + 后 8 位。
- v0004–v0027 无独立验收记录（原文未逐版本记录），不从推断补值。
- v0028 与产物版本号不一致：原文标题写 v0028、产物为 `1.1.0-0029`、结论写“v0028 PASS”，本表并列记录。
- 探针列的“—”表示原文无独立 Playwright 探针记录，不代表未做浏览器验证。
- v1.0.0-0001（2026-11-01）基线为 e2e 112/0/1、static 77/0/0、unittest 109（skip 2），仅供参考，已被上表覆盖。

### 6.1 版本演进要点

- v0042：修复大库首启超时（健康检查 15s→90s、DB 命中跳过 JSON 解析），真机覆盖升级成功。
- v0043：schema 升 v8，扫描从“每次全量读盘”转为哈希缓存复用，并引入保留规则链。
- v0045：合并默认关闭；删除去向新增套件独立回收站（默认仍为共享回收站）。
- v0048：字幕进入“可运营”阶段（失败分类、重试、监控、优先级、ETA）。
- v0050：批量取消改两阶段，几千任务不再触发前端 20s 超时。
- v0051：新增下载中心（直链 Range 续传 / HLS 分片 + 本地化播放列表交给 FFmpeg 解密封装）；schema 升 v9；e2e 新增 DL-00…DL-05、RESTART-08。
- v0052：新增播放网页解析（静态提取 + cURL 登录态暂存）；e2e 新增 DL-06…DL-08；按用户要求本次不打包。
- v0053：下载增强（去重跳过、自动重试、分时限速、命名模板、优先级/批量/导出、空间看板、通知、解析缓存与 iframe、书签脚本）；e2e 新增 DL-09…DL-12；仍未打包。

### 6.2 原报告记录异常（保留说明，不再重复展开）

- §11 标题与结论写 v0028，但产物版本为 `1.1.0-0029`，小节自称“v0028/v0029”；本表并列。
- §19 标题为 v0037，段末结论却写“结论：v0036 PASS”，属笔误。
- §24.1 实测健康检查窗口 18–21s，§24.2 又写“健康检查只有 15s”；15s 为代码超时值，实际窗口更大（v0042 已放宽到 90s）。
- v0004–v0027 无逐版本验收记录，本表不补。

## 7. 复现与产物

### 7.1 命令

```powershell
# 端到端（自动创建/清理临时目录与服务）
python qa/e2e_test.py

# 静态检查（含 unittest、node --check、SPK 解包/校验/同步）
python qa/static_checks.py

# 后端单元测试
cd src/backend; python -m unittest discover -s tests

# 前端模块图 / 无头浏览器冒烟
node qa/check_web_modules.mjs
python qa/web_smoke.py

# 产物校验
Get-FileHash dist\DuplicateCleaner-1.1.0-0051.spk -Algorithm SHA256
Get-Content dist\DuplicateCleaner-1.1.0-0051.spk.sha256

# 构建可复现性（独立输出目录，不影响 dist）
python build/build_spk.py --no-icons --out-dir $env:TEMP\qa-build1
```

### 7.2 产物位置

| 产物 | 位置 | 说明 |
|---|---|---|
| e2e 结果 | `qa/e2e_results.json` | 逐条 `id`/结论/说明 |
| static 结果 | `qa/static_results.json` | 逐条 `id`/结论 |
| SPK | `dist/DuplicateCleaner-1.1.0-0051.spk` | 39,168,000 B（当前源码为 0053，尚未打包） |
| 校验文件 | `dist/DuplicateCleaner-1.1.0-0051.spk.sha256` | 与产物 SHA256 一致 |
| 探针脚本/截图 | `.uiprobe/probe_v0043..v0050.mjs`、`.uiprobe/*.png` | 真实 Chromium |
| 契约 | `docs/*_CONTRACT.md` | 逐版本接口与行为 |

### 7.3 证据链

本报告是当前基线的汇总入口；每个版本的需求背景、实现取舍与踩坑细节保留在
`docs/SUBTITLE_CONTRACT.md`、`docs/SUBTITLE_BATCH_CONTRACT.md`、`docs/STORAGE_CONTRACT.md`、
`docs/REQUIREMENTS_V2_ASSESSMENT.md` 与 `docs/README.md`，逐次提交记录见 `git log`。

### 7.4 构建可复现性

- gzip 头 `mtime=0`，tar 固定 mtime/uid/gid，避免构建时间进入产物。
- 独立构建 2 次 SHA256 与 `dist` bit-identical：v1.0 与 v0003 有明确记录；后续以 SPK-SYNC + checksum 持续校验。
- `INFO.checksum` 必须等于 `package.tgz` 实际 MD5；`.spk.sha256` 必须等于 SPK 实际 SHA256。
- 版本号同步 8 处（INFO、`VERSION`、前端 cache-bust、`package.json` 等），由 static 检查兜底。

### 7.5 环境与依赖

| 项 | 值 |
|---|---|
| 验收主机 | Windows（Python 3.12.9、Node v24.15.0、Git for Windows bash 5.3.15） |
| 目标运行时 | DSM 7.4.1 / x86_64 / Python 3.8.15（v0042 真机记录） |
| 浏览器 | Chromium（Playwright + CDP），无 Firefox/Safari 覆盖 |
| 媒体工具 | 套件内置 FFmpeg：抽帧缩略图、抽取/流复制音轨 |
| 数据库 | SQLite，schema v9，无外部服务依赖 |
| 网络 | 本地回环即可；通知类用例用本地 Webhook 接收器，不出网 |

### 7.6 复跑注意事项

- static 会解包 `dist` 内 SPK，复跑前需先有对应版本产物，否则相关检查为 FAIL。
- e2e 会启动 dev 后端并占用端口，跑前确认无残留进程，跑后确认临时共享根已清理。
- 探针会写入并持久化设置，重复执行时断言兼容“已启用/未启用”两种初始状态。
- Windows 上 symlink 用例恒为 UNVERIFIED，属环境限制而非回归。
- 结果 JSON 每次整轮覆盖，历史结果以第 6 节版本表与 `git log` 为准。
