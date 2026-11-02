# DuplicateCleaner v1.1 内容相似度模式 —— 独立 QA 验收报告（v1.1.0-0003）

- 项目：重复文件清理 / DuplicateCleaner **1.1.0-0003**（DSM 7 SPK）
- 对应契约：docs/SIMILARITY_CONTRACT.md（binding）、docs/CONTRACT.md
- 验收人：qa-verifier（task-6 / task-7，独立于 backend-dev / frontend-dev）
- 验收时间：2026-11-02 08:05–08:12 (+08:00)
- 最终产物：dist/DuplicateCleaner-1.1.0-0003.spk
  - SHA256：bf52609860b1a6b477fa4194597baafc2c22a54055b066ff6eea2f902e763a8d
  - package.tgz MD5：33b31053b5546a743f03401c83730ec6（= INFO.checksum）
  - 大小：215,040 B；package.tgz 内 41 条目；外层 15 条目
- 结论：**PASS（可交付）**。相似模式、人工复核门控、CSV/API 扩展、旧精准模式回归、SPK 产物、
  **非 root 安装权限修复（v1.1.0-0003）** 均通过；已知限制、ACL 前置条件与未验证项见 §6.1、§7。

## 1. 结果总览（v1.1.0-0003 最终复验）

| 套件 | 命令 | 结果 |
|---|---|---|
| 相似度端到端（真实 dev 后端 + 临时样本） | python qa/similarity_e2e.py | **PASS=44 / FAIL=0 / UNVERIFIED=0** |
| 旧精准模式回归 E2E | python qa/e2e_test.py | **PASS=113 / FAIL=0 / UNVERIFIED=1**（唯一 UNVERIFIED=Windows 无 symlink 权限） |
| 静态检查（后端/前端/SPK 源码+产物+权限） | python qa/static_checks.py | **PASS=111 / FAIL=0 / UNVERIFIED=0** |
| 后端单元测试 | cd src/backend; python -m unittest discover -s tests -v | **139 tests PASS（2 skipped）**；Lead 连续 3 次 + QA 独立运行均通过 |
| 前端语法 | node --check src/web/app.js | PASS |
| SPK：checksum/权限/非 root/ACL 提示/洁净性/同步 | 见 §6 | 全部 PASS |
| 构建可复现性 | 独立构建 2 次对比 SHA256 | 两次均 = dist（bit-identical） |

产物：qa/similarity_results.json、qa/e2e_results.json、qa/static_results.json、本报告；
可复用脚本 qa/similarity_e2e.py、qa/e2e_test.py、qa/static_checks.py。

## 2. 相似度与安装权限需求追溯（契约 → 证据）

证据 ID 对应 qa/similarity_results.json / qa/static_results.json 中的 id；状态 PASS 均有可复现断言。

| ID | 要求 | 状态 | 关键证据 |
|---|---|---|---|
| SIM-REQ-01 | scan_mode=exact/similar，默认 exact；旧请求兼容 | PASS | E2E SIM-META-01/02、SIM-SCAN-01/02；static BE-SIM-SETTINGS、BE-SIM-COMPAT；旧 E2E 113 PASS |
| SIM-REQ-02 | 先精准哈希；精准组不重复出现在相似组 | PASS | E2E SIM-EXACT-ISOLATED、SIM-GROUP-02；static BE-SIM-EXACT-ISOLATED |
| SIM-REQ-03 | 文本相似 ≥ 阈值成组；< 阈值不成组 | PASS | E2E 相似组 3 组，分数 94/91/88（≥85）；不相关文本未成组（SIM-THRESHOLD） |
| SIM-REQ-04 | MinHash 64×32bit / 16×4 bands；确定性哈希；pHash | PASS（实现/单测） | static BE-SIM-ALGO；E2E 文本 minhash |
| SIM-REQ-05 | 相似组 keep 最新创建、default_selected=false、manual_review_required=true | PASS | E2E SIM-GROUP-03/04/05 |
| SIM-REQ-06 | 相似组 schema：match_type/similarity_score/method/low_confidence/hash 空/algo 空/content SHA256 | PASS | E2E SIM-GROUP-03/04/05；static BE-SIM-GROUP-SCHEMA |
| SIM-REQ-07 | 二进制/压缩包/媒体/超限文件跳过相似，但精准哈希仍识别重复 | PASS | E2E SIM-SKIP-BINARY（4）、SIM-SKIP-SIZE（2）、SIM-EXACT-BINARY（zip count=2） |
| SIM-REQ-08 | 特征缓存持久化；第二次扫描命中 | PASS | E2E 第一次 hits=0/misses=8；第二次 hits=8/misses=0 |
| SIM-REQ-09 | 相似阶段支持暂停/停止 | PASS | E2E SIM-STOP：stage=similarity 后 stop → stopped |
| SIM-REQ-10 | groups 接口 match_type 过滤 + exact_total/similar_total + similarity_desc | PASS | E2E SIM-GROUPS-FILTER-*；static BE-SIM-GROUPS-API |
| SIM-REQ-11 | CSV 新增 匹配类型/相似度分值 两列 | PASS | E2E SIM-CSV-01..04；static BE-SIM-CSV |
| SIM-REQ-12 | scope=all_duplicates 永不包含相似组 | PASS | E2E SIM-DEL-SCOPE：count=2/exact 2/similar 0；static BE-SIM-SCOPE |
| SIM-REQ-13 | 缺少 confirm_similar/reviewed 组 → 400 SIMILAR_REVIEW_REQUIRED | PASS | E2E SIM-DEL-REVIEW-01/02 |
| SIM-REQ-14 | 带复核可计划并回收站删除；keeper 不可删除 | PASS | E2E SIM-DEL-PLAN/EXEC、SIM-DEL-KEEPER |
| SIM-REQ-15 | 精准+相似混合计划分别统计并执行 | PASS | E2E SIM-DEL-MIXED-PLAN/EXEC |
| SIM-REQ-16 | 相似删除按各自 content_sha256 重校验，不要求二者相等 | PASS | static BE-SIM-REVALIDATE；E2E SIM-DEL-CHANGED |
| SIM-REQ-17 | 计划响应 similar_count/similar_bytes/exact_count/exact_bytes/manual_review/reviewed | PASS | E2E SIM-DEL-PLAN/MIXED-PLAN；static BE-SIM-REVIEW |
| SIM-REQ-18 | 审计含 review 信息；扫描日志含 mode/threshold/similar_groups/cache | PASS | E2E SIM-AUDIT、SIM-LOGS-01/02 |
| SIM-REQ-19 | 前端：模式切换 + 相似选项 + 进度计数 | PASS | static FE-SIM-MODE/OPTIONS/CACHE-COUNTERS |
| SIM-REQ-20 | 前端：分区展示 + 固定警示文案 + 摘要分列 | PASS | static FE-SIM-WARNING（逐字）、FE-SIM-PARTITION、FE-SIM-SUMMARY |
| SIM-REQ-21 | 前端：相似组无组级全选；人工复核 checkbox；未复核禁用；删除门控 | PASS | static FE-SIM-REVIEW/NO-BULK/DELETE-GATE/DELETE-SUMMARY |
| SIM-REQ-22 | 前端：提交 confirm_similar/reviewed_similar_group_ids；两步确认保留 | PASS | static FE-SIM-DELETE-GATE + 旧 E2E DEL-05/06 + FE-CONFIRM-1/2 |
| SIM-REQ-23 | 历史显示扫描模式；CSV 新列可下载 | PASS | static FE-SIM-HISTORY；E2E CSV |
| SIM-REQ-24 | 向后兼容：旧结果无 match_type 视为 exact | PASS | static BE-SIM-COMPAT；旧 E2E 全绿 |
| SIM-REQ-25 | v1.1.0-0003：安装不再以 root 运行（DSM7 安装阻塞修复） | PASS | static SPK-PRIVILEGE/USER/NO-ROOT/DIST-PRIVILEGE/VAR-WRITABLE/IONICE-GUARD；§6.1 |
| SIM-REQ-26 | v1.1.0-0003：UI/文档明确系统内部用户 duplicatecleaner 读/写 ACL（含 #recycle） | PASS | static SPK-ACL-HINT、SPK-PKG-ACL；打包 web/index.html 含提示；§6.1 |

## 3. 相似度 E2E 实测证据（2026-11-02 08:07 复验）

命令：python qa/similarity_e2e.py（真实 dev HTTP 后端、临时共享根、Windows SetFileTime 受控创建时间；
自动清理服务与临时目录）。样本 18 个文件：3 对高相似文本（94/91/88）、低相似对、文本/zip 精准组、
zip/媒体“相似但非字节一致”、2 个超 1MB 相似文本、4 个 1MB 级文本（停止测试）。

主扫描（scan_id 20261102-080702-6a6fdc）：
files_seen=18, exact_groups=2, exact_files=4, similar_groups=3, similar_files=6,
similarity_candidates=8, similarity_features=8, similarity_cache_misses=8,
similarity_pairs_checked=3, similarity_skipped={binary:4, size:2}, errors=0。
- 缓存：第二次相同扫描 cache_hits=8, misses=0，相似组数 3。
- 停止：慢速扫描观察到 stage=similarity 后 stop → stopped。
- 删除：scope=all_duplicates 计划 count=2/exact 2/similar 0；无复核 → 400 SIMILAR_REVIEW_REQUIRED；
  带复核 → similar_count=1/manual_review=true → 回收站删除成功、keeper 保留；keeper 被 IS_KEEPER 拒绝；
  混合 exact+similar（exact_count=1/similar_count=1）删除成功；修改相似文件 FILE_CHANGED 跳过；
  审计 delete_plan 含 similar_count/manual_review/reviewed。
- 评分：minhash 94/91/88，全部 ≥ 阈值 85；低相似文本、zip/媒体、超限文件均未进入相似组。

## 4. 旧精准模式回归

python qa/e2e_test.py → **PASS=113 / FAIL=0 / UNVERIFIED=1**（v1.0 全套用例，已适配 v1.1 版本号与 CSV 12 列）。
覆盖认证/CSRF、扫描安全、hash-only、latest/earliest/pinned、创建时间非 mtime、系统目录/隐藏/空/硬链接、
分页/搜索、CSV、预览/Range/截断/415、keeper 拒绝、FILE_CHANGED、回收站/永久删除、plan 一次性、
scope=all_duplicates、audit/deletions、暂停/停止/SCAN_BUSY、重启持久化。
唯一 UNVERIFIED：Windows 无 symlink 权限（WinError 1314）。

## 5. 静态检查

python qa/static_checks.py → **PASS=111 / FAIL=0 / UNVERIFIED=0**（2026-11-02 08:06，0003 源码+产物）。
除既有项（py_compile、Python 3.8 语法、139 unittest、node --check、风险文案、端点覆盖、两次确认、
无自动删除、相似算法/缓存/门控、SPK 结构/同步/可复现）外，新增 v1.1.0-0003 专项：
- SPK-PRIVILEGE：conf/privilege run-as=package（非 root）；
- SPK-USER：username/groupname = duplicatecleaner；
- SPK-NO-ROOT：SPK 源码/脚本中无 run-as:root、chown root、root:root、sudo；
- SPK-VAR-WRITABLE / SPK-IONICE-GUARD：启动脚本校验 var 可写、提示 chown duplicatecleaner；non-root 下 ionice 失败可降级；
- SPK-ACL-HINT / SPK-PKG-ACL：UI 与文档提示系统内部用户读/写 ACL；打包后仍在；
- SPK-DIST-PRIVILEGE：产物内 conf/privilege 为非 root + duplicatecleaner。

## 6. 最终 SPK 产物验收（v1.1.0-0003）

| 检查 | 结论 | 证据 |
|---|---|---|
| 外层结构 | PASS | 15 条目：INFO/package.tgz/图标/LICENSE/conf/{privilege,resource}/scripts/{6 个} |
| checksum | PASS | INFO.checksum = package.tgz MD5 = 33b31053b5546a743f03401c83730ec6 |
| SHA256 文件 | PASS | .sha256 = 实际 bf526098…a8d |
| 权限 | PASS | scripts 全 755、conf 644；package 内 app/app.py 755 |
| **非 root 权限模型** | PASS | conf/privilege = {"defaults":{"run-as":"package"},"username":"duplicatecleaner","groupname":"duplicatecleaner"}；产物内无 root 运行/root 属主设置 |
| 数据目录 | PASS | var 目录 700、属套件用户；启动脚本写权限校验 + 修复提示（chown duplicatecleaner） |
| package 结构 | PASS | 41 条目：VERSION + app/（仅 .py，含 similarity.py）+ web/ + ui/ + port_conf/ |
| 洁净性 | PASS | 无 tests/__pycache__/pyc/log/调试输出/rerun；无 dev 变量/私钥 |
| 文案 | PASS | 风险文案 + 相似警示 + **系统内部用户 duplicatecleaner 读/写 ACL 提示** 均在打包 web/index.html |
| cache-bust | PASS | favicon.svg/styles.css/app.js 均 ?v=1.1.0-0003 |
| ui | PASS | 占位符已替换；ui/config 为最终描述；图标齐全 |
| 与源码同步 | PASS | app/web/ui/port_conf 所有文件 SHA256 与工作区一致 |
| 可复现 | PASS | build_spk.py --no-icons 独立构建 2 次，SHA256 均 = dist（bit-identical） |

### 6.1 非 root 安装权限修复（v1.1.0-0003）与管理员必做授权

**根因**：DSM 7 拒绝安装 conf/privilege 中 run-as: root 的第三方套件（v1.1.0-0002 安装阻塞）。

**修复**：套件改为 DSM 系统内部用户 duplicatecleaner 运行（run-as: package + username/groupname），
生命周期脚本与数据目录适配；启动脚本校验 var 可写、非 root 下 ionice 自动降级；UI/文档新增 ACL 提示；
版本号 1.1.0-0003。QA 已静态核对：产物与源码均无 root 运行/root 属主配置（SPK-NO-ROOT 等）。

**部署必做（管理员）**：首次使用前，必须在 DSM 给系统内部用户 duplicatecleaner 授权目标共享文件夹：
1. 控制面板 → 共享文件夹 → 目标共享文件夹 → 编辑 → 权限；
2. 权限对象选择 **系统内部用户**，找到 **duplicatecleaner**；
3. 至少授予 **读取**（仅扫描）；需要删除副本时必须授予 **读取/写入**，并**应用到子文件夹/文件**；
4. **回收站删除**要求该用户对共享文件夹根及 #recycle（实际写入 #recycle/@duplicate-cleaner/...）也有写权限；
   如使用“自动创建回收站目录”，同样需要共享根写权限。
未授权时：目录选择/扫描返回“没有权限…请给系统内部用户 duplicatecleaner 授予读取权限”，
删除计划会按路径/回收站不可用安全跳过，**不会静默跳过或删除**。
从旧 root 版本升级时，文档要求管理员执行：
    sudo chown -R duplicatecleaner:duplicatecleaner /var/packages/DuplicateCleaner/var

## 7. 偏差、未验证项与残余风险

1. **ACL 前置条件是运维必做项（新增，重要）**：非 root 后，套件只能访问管理员显式授权的共享文件夹；
   未授权时扫描/删除会安全失败。部署文档与 UI 已提示，但无法由安装程序自动授权（DSM ACL 需管理员操作）。
2. **真实 DSM 安装未验证（环境限制）**：无 DSM 7 真机；非 root 权限模型、DSM 生命周期脚本执行身份、
   安装/启动/回收站 ACL 行为仅能静态与文档核对，上线前必须真机安装冒烟（含授权前后行为）。
3. **Windows 符号链接 UNVERIFIED**：无法创建 symlink（WinError 1314）；代码/单测在支持平台覆盖。
4. **文档一致性（已修复）**：CONTRACT §7 CSV 列已同步为 12 列（匹配类型/相似度分值），与 SIMILARITY_CONTRACT §4.2 一致；
   建议后续同步文档（不影响功能）。
5. **相似质量/性能未大规模评估**：E2E 仅 18 文件；未评估真实库误匹配率、CPU/IO、50k shingle/文件上限、
   32MB 大文件、1–4 并发曲线；图片/PDF/DOCX 相似仅由 139 单测覆盖，未做真实样本 E2E。
6. **特征缓存失效未 E2E 直测**：size/mtime_ns/inode/dev key 由单测覆盖；E2E 验证命中与删除前重校验。
7. **5 分钟 plan 过期未在 E2E 等待**：由后端单测覆盖；E2E 验证一次性/重放拒绝。
8. **浏览器 DOM 未自动化**：前端为 node 语法 + 静态门控 + HTTP 页面加载；建议真实 DSM 浏览器冒烟。
9. **网络/默认 HTTP**：8787 默认 HTTP，仅限可信内网；公网需 VPN/反代+TLS；非 root 不改变网络暴露面。
10. **相似度为辅助**：不保证业务语义；已强制人工复核、禁止相似组一键删除。

## 8. 复现命令

    # 相似度端到端 / 旧精准模式回归
    python qa/similarity_e2e.py
    python qa/e2e_test.py

    # 静态检查（含 139 unittest、node --check、相似门控、非 root 权限、SPK 解包/同步）
    python qa/static_checks.py

    # 后端单测
    cd src/backend; python -m unittest discover -s tests -v

    # 最终产物校验 + 可复现构建
    Get-FileHash dist\DuplicateCleaner-1.1.0-0003.spk -Algorithm SHA256
    python build/build_spk.py --no-icons --out-dir $env:TEMPqa13-build1
    python build/build_spk.py --no-icons --out-dir $env:TEMPqa13-build2

## 9. 签收结论

- 相似模式 24 类要求 + **非 root 安装权限修复（2 类专项）** 全部 PASS；旧精准模式全量回归 PASS。
- 安全核心：相似组必须人工复核、scope 不自动包含、keeper 不可删、删除前内容哈希重校验、PLAN 一次性/
  回收站默认、审计持久化，均有端到端证据。
- 安装权限：最终产物已无非 root 之外的 root 配置；管理员须按 §6.1 授权系统内部用户 duplicatecleaner
  读取/写入（删除含 #recycle）。
- 最终 SPK：bf52609860b1a6b477fa4194597baafc2c22a54055b066ff6eea2f902e763a8d，
  checksum/权限/洁净性/同步/可复现全部 PASS。
- 建议：可交付；上线前必须完成 DSM 7 真机安装 + ACL 授权 + 浏览器冒烟（含授权/未授权两种路径）。
