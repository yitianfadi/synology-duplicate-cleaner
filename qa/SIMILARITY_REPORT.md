# DuplicateCleaner v1.1 内容相似度模式 —— 独立 QA 验收报告

- 项目：重复文件清理 / DuplicateCleaner **1.1.0-0002**（DSM 7 SPK）
- 对应契约：[docs/SIMILARITY_CONTRACT.md](docs/SIMILARITY_CONTRACT.md)（binding）、[docs/CONTRACT.md](docs/CONTRACT.md)
- 验收人：qa-verifier（task-6，独立于 backend-dev / frontend-dev）
- 验收时间：2026-11-02 07:31–07:45 (+08:00)
- 最终产物：`dist/DuplicateCleaner-1.1.0-0002.spk`
  - SHA256：`62a54ed48d20f9255ac9f0788f808a210e4e9121e3d2b6019aeabf88a57cc351`
  - package.tgz MD5：`14fc3f6c6a4f925e0ac4237a2c7df89b`（= INFO.checksum）
  - 大小：204,800 B；package.tgz 内 41 条目；外层 15 条目
- 结论：**PASS（可交付）**。相似模式核心行为、人工复核门控、CSV/API 扩展、前端静态门控、旧精准模式回归、SPK 产物均通过；
  已知限制与未验证项见 §7。

## 1. 结果总览

| 套件 | 命令 | 结果 |
|---|---|---|
| 相似度端到端（真实 dev 后端 + 临时样本） | `python qa/similarity_e2e.py` | **PASS=44 / FAIL=0 / UNVERIFIED=0** |
| 旧精准模式回归 E2E | `python qa/e2e_test.py` | **PASS=113 / FAIL=0 / UNVERIFIED=1**（唯一 UNVERIFIED=Windows 无 symlink 权限） |
| 静态检查（后端/前端/SPK 源码+产物） | `python qa/static_checks.py` | **PASS=104 / FAIL=0 / UNVERIFIED=0** |
| 后端单元测试 | `cd src/backend && python -m unittest discover -s tests -v` | **139 tests PASS（2 skipped）**；Lead 报告连续 3 次通过，QA 独立运行亦通过 |
| 前端语法 | `node --check src/web/app.js` | PASS |
| SPK 解包/checksum/权限/洁净性/同步 | 见 §6 | 全部 PASS |
| 构建可复现性 | 独立构建 2 次对比 SHA256 | 两次均 = dist（bit-identical） |

产物：`qa/similarity_results.json`、`qa/e2e_results.json`、`qa/static_results.json`、本报告；
可复用脚本 `qa/similarity_e2e.py`、`qa/e2e_test.py`、`qa/static_checks.py`。

## 2. 相似度需求追溯（SIMILARITY_CONTRACT → 证据）

证据 ID 对应 `qa/similarity_results.json` / `qa/static_results.json` 中的 `id`；状态 PASS 均有可复现断言。

| ID | 契约要求 | 状态 | 关键证据 |
|---|---|---|---|
| SIM-REQ-01 | scan_mode=exact/similar，默认 exact；旧请求兼容 | PASS | E2E SIM-META-01/02、SIM-SCAN-01/02；static BE-SIM-SETTINGS、BE-SIM-COMPAT；旧 E2E 113 PASS |
| SIM-REQ-02 | 先精准哈希；精准组不重复出现在相似组 | PASS | E2E SIM-EXACT-ISOLATED（exact_paths ∩ similar_paths 为空）、SIM-GROUP-02；static BE-SIM-EXACT-ISOLATED |
| SIM-REQ-03 | 文本相似 ≥ 阈值成组；< 阈值不成组 | PASS | E2E 主扫描相似组 3 组，分数 94/91/88（≥85）；不相关 one/two 未成组（SIM-THRESHOLD） |
| SIM-REQ-04 | 相似算法：MinHash 64×32bit / 16×4 bands；确定性哈希；pHash | PASS（实现/单测） | static BE-SIM-ALGO（signature=64/bands=16/rows=4、sha256 置换、dc-sim-perm、dhash）；E2E 文本 minhash 覆盖 |
| SIM-REQ-05 | 相似组 keep 最新创建、default_selected=false、manual_review_required=true | PASS | E2E SIM-GROUP-03/04/05（keep=beta/delta，全部 default_selected=false，manual_review_required=true，副本分数≥阈值） |
| SIM-REQ-06 | 相似组 schema：match_type/similarity_score/method/low_confidence/hash 空/algo 空/文件内容 SHA256 | PASS | E2E SIM-GROUP-03/04/05（hash=""、algo=""、content_sha256=文件 SHA256）；static BE-SIM-GROUP-SCHEMA |
| SIM-REQ-07 | 二进制/压缩包/媒体/超限文件跳过相似计算，但精准哈希仍识别重复 | PASS | E2E SIM-SKIP-BINARY（binary=4）、SIM-SKIP-SIZE（size=2）、SIM-EXACT-BINARY（zip 精准组 count=2） |
| SIM-REQ-08 | 特征缓存持久化；第二次扫描命中 | PASS | E2E 第一次 hits=0/misses=8；第二次 hits=8/misses=0（SIM-CACHE-01/02/03）；feature_cache.json 落盘 |
| SIM-REQ-09 | 相似阶段支持暂停/停止；阶段 progress 85→97→100 | PASS | E2E SIM-STOP：轮询到 stage=similarity 后 stop → stopped；static BE-SIM-STAGE |
| SIM-REQ-10 | groups 接口 match_type 过滤 + exact_total/similar_total + similarity_desc | PASS | E2E SIM-GROUPS-FILTER-EXACT/SIMILAR、SIM-GROUPS-FILTER-BAD；static BE-SIM-GROUPS-API |
| SIM-REQ-11 | CSV 新增 匹配类型/相似度分值 两列，标签 哈希一致/内容相似 | PASS | E2E SIM-CSV-01..04（12 列表头逐字、标签、分值 100/≥85、保留行）；static BE-SIM-CSV |
| SIM-REQ-12 | scope=all_duplicates 永不包含相似组 | PASS | E2E SIM-DEL-SCOPE：计划 count=2/exact_count=2/similar_count=0，文件全为 g-/非 sf-；static BE-SIM-SCOPE |
| SIM-REQ-13 | 相似文件缺少 confirm_similar 或 reviewed_similar_group_ids → 400 SIMILAR_REVIEW_REQUIRED | PASS | E2E SIM-DEL-REVIEW-01/02（两种缺失均 400）；static BE-SIM-REVIEW |
| SIM-REQ-14 | 带人工复核可生成计划并回收站删除；keeper 不可删除 | PASS | E2E SIM-DEL-PLAN/EXEC（recycled，keeper 仍在）、SIM-DEL-KEEPER（IS_KEEPER，400 PLAN_INVALID） |
| SIM-REQ-15 | 精准+相似混合计划可分别统计并执行 | PASS | E2E SIM-DEL-MIXED-PLAN（exact_count=1/similar_count=1）、SIM-DEL-MIXED-EXEC（均 recycled，keeper 保留） |
| SIM-REQ-16 | 相似删除前按各自 content_sha256 重校验，不要求二者哈希相等 | PASS | static BE-SIM-REVALIDATE；E2E SIM-DEL-CHANGED（扫描后修改相似文件 → FILE_CHANGED 跳过，不删除） |
| SIM-REQ-17 | 删除计划响应 similar_count/similar_bytes/exact_count/exact_bytes/manual_review/reviewed 组 | PASS | E2E SIM-DEL-PLAN/MIXED-PLAN；static BE-SIM-SCHEMA/BE-SIM-REVIEW |
| SIM-REQ-18 | 审计含相似 review 信息；扫描日志含 mode/threshold/similar_groups/cache | PASS | E2E SIM-AUDIT、SIM-LOGS-01/02 |
| SIM-REQ-19 | 前端：模式切换 + 相似选项 + 进度计数 | PASS | static FE-SIM-MODE/FE-SIM-OPTIONS/FE-SIM-CACHE-COUNTERS；E2E 静态页面加载 |
| SIM-REQ-20 | 前端：分区展示 + 固定警示文案 + 摘要分列 | PASS | static FE-SIM-WARNING（逐字）、FE-SIM-PARTITION、FE-SIM-SUMMARY |
| SIM-REQ-21 | 前端：相似组无组级全选；人工复核 checkbox；未复核禁用选择；删除按钮门控 | PASS | static FE-SIM-REVIEW/FE-SIM-NO-BULK/FE-SIM-DELETE-GATE/DELETE-SUMMARY |
| SIM-REQ-22 | 前端：提交 confirm_similar/reviewed_similar_group_ids；两步确认仍保留 | PASS | static FE-SIM-DELETE-GATE + 旧 E2E DEL-05/06 回归 + FE-CONFIRM-1/2 |
| SIM-REQ-23 | 历史记录显示扫描模式；CSV 新列可下载 | PASS | static FE-SIM-HISTORY；E2E CSV |
| SIM-REQ-24 | 向后兼容：旧结果无 match_type 视为 exact；精准组字段不变 | PASS | static BE-SIM-COMPAT；旧精准模式 E2E 全绿（含旧删除/CSV 新列适配） |

## 3. 相似度 E2E 实测证据

命令：`python qa/similarity_e2e.py`（真实 dev HTTP 后端、临时共享根、Windows `SetFileTime` 受控创建时间；
自动清理服务与临时目录）。样本 18 个文件：3 对高相似文本（分数 94/91/88）、1 对低相似文本、
文本/zip 精准组、zip/媒体“相似但非字节一致”、2 个超 1MB 相似文本、4 个 1MB 级文本（停止测试）。

主扫描（scan_id `20261102-074005-2492f8`）关键数据：
`files_seen=18, exact_groups=2, exact_files=4, similar_groups=3, similar_files=6,
similarity_candidates=8, similarity_features=8, similarity_cache_hits=0, similarity_cache_misses=8,
similarity_pairs_checked=3, similarity_skipped={binary:4, size:2}, errors=0`。
相似组评分 94/91/88（minhash），全部 ≥ 阈值 85。

- 缓存：第二次相同扫描 `similarity_cache_hits=8, misses=0`，相似组数仍为 3（feature_cache.json 持久化）。
- 停止：慢速扫描（similarity_concurrency=1、hash_rate_mbps=1、cache=false）观察到 `stage=similarity` 后停止，
  最终 `status=stopped`。
- 删除：scope=all_duplicates 计划 count=2（全部 exact，similar_count=0）；
  无复核的相似文件 → 400 SIMILAR_REVIEW_REQUIRED；带复核 → 计划 count=1/similar_count=1/manual_review=true，
  删除 job completed，副本进入 `#recycle/@duplicate-cleaner/`，保留项仍在；
  keeper 提交删除被 IS_KEEPER 拒绝；混合 exact+similar 计划 exact_count=1/similar_count=1，删除后两类副本均 recycled、
  keeper 全部保留；审计 `delete_plan` 记录 similar_count/manual_review/reviewed。
- 扫描后修改相似文件：计划阶段进入 skipped（FILE_CHANGED），文件未被删除。

## 4. 旧精准模式回归

命令：`python qa/e2e_test.py`（v1.0 全套用例已适配 v1.1 版本号与 CSV 新列）。

结果：**PASS=113 / FAIL=0 / UNVERIFIED=1**。覆盖与 v1.0 相同：认证/CSRF、扫描与安全校验、hash-only、
latest/earliest/pinned、创建时间非 mtime、系统目录/隐藏/空/硬链接、分页/搜索、CSV（12 列、匹配类型=哈希一致、
分值=100）、预览/Range/截断/415、保留项删除拒绝、FILE_CHANGED、回收站/永久删除、plan 一次性、
scope=all_duplicates、audit/deletions、暂停/停止/SCAN_BUSY、重启持久化。
唯一 UNVERIFIED：Windows 无 symlink 权限（WinError 1314），无法构造符号链接样本（后端单测在支持平台覆盖）。

## 5. 静态检查（v1.1）

`python qa/static_checks.py`：**PASS=104 / FAIL=0 / UNVERIFIED=0**。除 v1.0 既有项（py_compile、
Python 3.8 语法、unittest、node --check、风险文案、端点覆盖、两次确认、无自动删除、SPK 结构/权限/同步）外，
新增 v1.1 项：
- 后端：BE-SIM-ALGO/CACHE/SETTINGS/STAGE/GROUP-SCHEMA/EXACT-ISOLATED/GROUPS-API/CSV/REVIEW/SCOPE/
  REVALIDATE/META/COMPAT（算法参数、缓存 key、8 个设置键、similarity 阶段与分类统计、相似组 schema、
  groups 过滤、CSV 新列、复核门控、scope 排除、重校验、meta 能力、旧数据兼容）。
- 前端：FE-SIM-WARNING（逐字警示）、MODE、OPTIONS、PARTITION、SUMMARY、REVIEW、NO-BULK、
  DELETE-GATE、DELETE-SUMMARY、HISTORY、CACHE-COUNTERS。
- 构建：SPK-BUILD-VERSION/WHITELIST（build_spk.py VERSION 与 config 一致，app/ 仅打包 .py）。

## 6. 最终 SPK 产物验收

独立解包（Python tarfile）：

| 检查 | 结论 | 证据 |
|---|---|---|
| 外层结构 | PASS | 15 条目：INFO/package.tgz/图标/LICENSE/conf/{privilege,resource}/scripts/{6 个} |
| checksum | PASS | INFO.checksum = package.tgz MD5 = `14fc3f6c6a4f925e0ac4237a2c7df89b` |
| SHA256 文件 | PASS | `.sha256` = 实际 `62a54ed4…c351` |
| 权限 | PASS | 外层 scripts 全 755、conf 644；package 内 app/app.py 755 |
| package 结构 | PASS | 41 条目：VERSION + app/（仅 .py，含 similarity.py）+ web/ + ui/ + port_conf/ |
| 洁净性 | PASS | 无 tests/__pycache__/pyc/log/调试输出/rerun；无 dev 变量/私钥；无 extra 文件 |
| 文案 | PASS | 精准模式风险文案 + 相似模式警示文案均在打包 web/index.html 中逐字存在 |
| cache-bust | PASS | favicon.svg/styles.css/app.js 均 `?v=1.1.0-0002` |
| ui | PASS | 占位符已替换；ui/config 为最终描述；图标齐全 |
| 与源码同步 | PASS | app/web/ui/port_conf 所有文件 SHA256 与工作区一致（SPK-SYNC） |
| 可复现 | PASS | `build_spk.py --no-icons` 独立构建 2 次，SHA256 均 = dist（bit-identical） |

## 7. 偏差、未验证项与残余风险

1. **文档小不一致（低危）**：`docs/SIMILARITY_CONTRACT.md` §4.2 要求 CSV 增加 匹配类型/相似度分值，
   实现与验收均按新契约 12 列；`docs/CONTRACT.md` §7 的 CSV 列仍保留 v1.0 的 10 列描述。
   该文档已在验收后由 Lead 同步为 12 列（本报告保留验收时点的发现记录）。
2. **v1.0 已知偏差已被契约吸收**：回收站不可用时 delete-plan 返回 409 `RECYCLE_UNAVAILABLE`，
   v1.1 CONTRACT §7 已明确写入该行为（不再是偏差）。
3. **Windows 符号链接 UNVERIFIED**：无法创建 symlink（WinError 1314）；代码路径与后端单测（支持平台）覆盖。
4. **相似质量/性能未做大规模评估**：E2E 仅 18 文件、~24KB 哈希 + 数 MB 停止样本；未评估真实照片/文档库的
   MinHash 误匹配率、CPU/IO 开销、50k shingle/文件上限、32MB 大文件、1–4 并发曲线。建议真机抽样验证。
5. **图片/PDF/DOCX 相似未做 E2E**：E2E 覆盖文本 MinHash；pHash（PNG/JPEG）、PDF 文本层、docx/odt 由后端
   139 个单测覆盖（含降级/无文本跳过），但未在真实样本上端到端验证；Pillow 为可选依赖，缺失时 JPEG 走降级。
6. **特征缓存失效未 E2E 直测**：缓存 key 含 size/mtime_ns/inode/dev；文件修改后理论失效，单测覆盖 key 逻辑；
   E2E 只验证命中与删除前重校验。
7. **5 分钟 plan 过期未在 E2E 等待**：由后端单测 `test_plan_expires` 覆盖；E2E 验证一次性/重放拒绝。
8. **真实 DSM/浏览器 DOM 未验证**：未在 DSM 7 真机（Btrfs/EXT4）安装，也未做浏览器自动化点击；
   前端为 node 语法 + 静态门控 + HTTP 静态页面加载证据。上线前建议做一次真机 + 浏览器冒烟。
9. **高权限/网络**：套件仍以 root 运行、默认 HTTP 8787；仅限可信内网，公网需 VPN/反代+TLS。
10. **非目标提醒**：相似度只作辅助，不保证业务语义；产品已强制人工复核，不提供相似组一键删除。

## 8. 复现命令

```powershell
# 相似度端到端
python qa/similarity_e2e.py

# 旧精准模式回归
python qa/e2e_test.py

# 静态检查（含 unittest、node --check、相似前端/后端门控、SPK 解包/同步）
python qa/static_checks.py

# 后端单测
cd src/backend; python -m unittest discover -s tests -v

# 最终产物校验 + 可复现构建
Get-FileHash dist\DuplicateCleaner-1.1.0-0002.spk -Algorithm SHA256
python build/build_spk.py --no-icons --out-dir $env:TEMP\qa11-build1
python build/build_spk.py --no-icons --out-dir $env:TEMP\qa11-build2
```

## 9. 签收结论

- 相似模式 24 类验收要求全部 PASS（44 项 E2E 断言 + 27 项 v1.1 静态断言），无 FAIL；旧精准模式全量回归 PASS。
- 安全核心：相似组默认不勾选、必须人工复核、scope 不自动包含、keeper 不可删、删除前内容哈希重校验、
  PLAN 一次性/回收站默认、审计持久化，均有端到端证据。
- 最终 SPK：`62a54ed48d20f9255ac9f0788f808a210e4e9121e3d2b6019aeabf88a57cc351`，
  checksum/权限/洁净性/同步/可复现全部 PASS。
- 建议：可交付；上线前按 §7 做 DSM 真机 + 浏览器 + 真实相似样本冒烟，并同步 CONTRACT §7 CSV 列文档。
