# DuplicateCleaner SPK —— 独立 QA 验收报告

- 项目：重复文件清理 / DuplicateCleaner 1.0.0-0001（DSM 7 SPK）
- 验收人：qa-verifier（task-3，独立于 backend-dev / frontend-dev）
- 验收时间：2026-11-01 20:47–20:55 (+08:00)
- 验收主机：Windows（Python 3.12.9、Node v24.15.0、Git for Windows bash 5.3.15）
- 最终产物：`dist/DuplicateCleaner-1.0.0-0001.spk`
  - SHA256：`b16d36224af6db5d4c89681daab45c695de2ba97681324d3a4337033b389fc8f`
  - package.tgz MD5：`e8eeb5a62fbee63b47b13e32453b401a`
  - 大小：184,320 bytes；package.tgz 内 40 个条目
- 结论：**PASS（可交付）**，附带 1 项低危契约偏差、若干环境限制导致的 UNVERIFIED 与残余风险，详见 §6–§7。

## 1. 结果总览

| 套件 | 命令 | 结果 |
|---|---|---|
| 端到端（真实 dev 后端 + temp 共享根） | `python qa/e2e_test.py` | **PASS=112 / FAIL=0 / UNVERIFIED=1**（另 1 条 INFO） |
| 静态检查（后端/前端/SPK 源码+产物） | `python qa/static_checks.py` | **PASS=77 / FAIL=0 / UNVERIFIED=0** |
| 后端单元测试 | `cd src/backend && python -m unittest discover -s tests -v` | **109 tests PASS（2 skipped），连续 3 次稳定** |
| 前端语法 | `node --check src/web/app.js` | PASS |
| SPK 解包/权限/checksum/洁净性/同步 | 见 §5 | 全部 PASS |
| 构建可复现性 | 独立构建 2 次对比 SHA256 | 两次均与 dist 完全一致（bit-identical） |

产物：`qa/e2e_results.json`（114 条记录）、`qa/static_results.json`（77 条 PASS + 3 条 INFO）、
本报告；可复用脚本 `qa/e2e_test.py`、`qa/static_checks.py`；追溯清单 `qa/TRACEABILITY.md`。

## 2. 需求追溯（用户显式需求 → 证据）

状态口径：PASS=有可复现证据；FAIL=有具体复现；UNVERIFIED=环境无法验证。证据 ID 对应两份 JSON 中的 `id`。

| ID | 需求 | 状态 | 关键证据 |
|---|---|---|---|
| REQ-01 | 仅按内容哈希判重，不按文件名/大小/时间 | PASS | E2E REQ-HASH-ONLY（改名同内容成组）、HASH-01（同大小不同内容不成组）、GROUP-02；后端单测 |
| REQ-02 | 默认只保留创建时间最晚 | PASS | E2E KEEP-01（photo1/2/3 中 keep=photo3，keep_reason=latest_created） |
| REQ-03 | 创建时间不能用 mtime/ctime 冒充 | PASS | E2E KEEP-02/03（source=birthtime；mtime 反转后仍按创建时间保留）；static BE-STATX、BE-CTIME |
| REQ-04 | 大文件分片 + 限速 + 并发 | PASS（功能） | static 审阅 hashing.py：1 MiB 分片、TokenBucket 全局限速、取消/进度；E2E SCAN-BIG（4MB×2 全量哈希成组）、PAUSE 限速扫描；未做大文件吞吐基准 |
| REQ-05 | 文件夹多选 + 排除 | PASS | E2E SHARES-01、BROWSE-MULTIROOT（跨 2 根成组）、EXCLUDE-01/02；static FE-FOLDER-PICKER |
| REQ-06 | 分组展示/保留高亮/预览/全选/CSV | PASS | E2E GROUP-01..08、CSV-01..05、PREVIEW-*；static FE-KEEP-HIGHLIGHT/FE-PREVIEW/FE-CSV/FE-SELECT-ALL |
| REQ-07 | 绝不静默/自动删除 | PASS | E2E SAFETY-01（扫描前后 46 个文件快照完全一致）、DEL-01/02；static BE-NO-AUTODELETE、FE-NO-AUTODELETE |
| REQ-08 | 报告统计 | PASS | E2E STATS-01（groups/duplicate_groups/duplicate_files/reclaimable 自洽）、STATS-02（wasted=size*(count-1)）；实测 groups=12、duplicate_files=27、reclaimable=4,403,200 B |
| REQ-09 | 删除两次确认 | PASS | static FE-CONFIRM-1/2（“第 1 步/共 2 步”“第 2 步：最终确认”、永久删除短语、按钮防重复）；E2E DEL-05/06（缺 confirm/错误 confirm 均 400） |
| REQ-10 | 默认回收站，可选永久删除 | PASS | E2E DEL-04/08/10（recycled → `<share>/#recycle/@duplicate-cleaner/<ts>/<relpath>`）、DEL-13/15/16（永久删除 unlink，无 #recycle 残留） |
| REQ-11 | 操作日志持久化 | PASS | E2E AUDIT-01/02/03（audit.jsonl 含 scan/plan/delete 事件、SQLite deletion_results 逐文件结果）、LOGS-01..03、RESTART-01..07（重启后历史/报告/设置/日志仍可读） |
| REQ-12 | DSM 7 / Btrfs / EXT4 兼容 | PASS（静态）/ UNVERIFIED（真机） | static SPK-INFO-VALUES（os_min_ver=7.0）、BE-PY38（ast feature_version=3.8）、BE-STATX；无 DSM 真机可测 |
| REQ-13 | 跳过系统目录/符号链接/空/隐藏/后缀 | PASS / 1 项 UNVERIFIED | E2E SKIP-01..03、EXCLUDE-02、FILT-04/05；SKIP-04 因 Windows 无 symlink 权限 UNVERIFIED（后端单测在支持平台覆盖 symlink 拒绝） |
| REQ-14 | 暂停/继续/停止 | PASS | E2E PAUSE-01..05：paused、SCAN_BUSY、resume、stop、停止后 resume 返回 400 |
| REQ-15 | 保留最早 / 指定路径 | PASS | E2E FILT-01（earliest→e1，md5 算法生效）、FILT-02（pinned→p2，keep_reason=pinned_path） |
| REQ-16 | 大小区间 | PASS | E2E FILT-06（min_size=2MB 仅返回 >=2MB 组） |
| REQ-17 | 后缀忽略大小写 | PASS | E2E FILT-03/04（.jpg 过滤在 ignore_case=true/false 下分别命中/不命中 .JPG） |
| REQ-18 | 顶部常驻风险提示（精确文案） | PASS | E2E STATIC-01（运行中的页面含逐字文案）；static FE-RISK-TEXT、FE-RISK-PERSISTENT（无关闭按钮/隐藏逻辑）、SPK-PKG-RISK（打包后仍在） |
| REQ-19 | 仅管理员登录，不保存密码 | PASS | E2E AUTH-01..05（401/CSRF/cookie/admin）；static SPK-NO-DEV、SPK-NO-DEV-SET；dev 模式仅显式开启 |
| REQ-20 | 删除前逐文件重校验 | PASS | E2E DEL-01（keeper→IS_KEEPER 拒绝）、DEL-03（扫描后被改文件→FILE_CHANGED，文件保留）、DEL-11（plan 一次性）、DEL-12（回收站不可用→跳过并列出原因） |
| REQ-21 | 路径白名单/黑名单/防遍历 | PASS | E2E SEC-01..08（系统路径、共享根外、隐藏路径、`..` 穿越、日志下载穿越、preview 越权）、BROWSE-04/05 |
| REQ-22 | 硬链接按 (dev,ino) 去重 | PASS | E2E HARDLINK-01（hard1+硬链接 hard2+副本 hardcopy → 组 count=2，skipped.hardlink=1，wasted 不虚高） |
| REQ-23 | SPK 打包正确、无 dev/测试/pycache | PASS | static SPK-* 共 36 项；§5 解包证据；构建可复现 |
| REQ-24 | 低影响（nice/ionice/单任务/分页） | PASS | E2E PAUSE-02（SCAN_BUSY）、GROUP-06/07（分页/排序）；static SPK-NICE、SPK-HEALTH |
| REQ-25 | 设置持久化 + 白名单校验 | PASS | E2E SET-01..07（默认键、保存、未知键/越界/区间 400、skip_symlinks 强制 true）、RESTART-04 |
| REQ-26 | 前端无 CDN/无构建/ES2019/全中文/安全 DOM | PASS | static FE-NODECHECK、FE-NO-CDN、FE-ES2019（无 ?. ?? BigInt 等）、FE-TEXTCONTENT（textContent/createTextNode=8，innerHTML=0）、FE-CHINESE、FE-NAV |
| REQ-27 | 预览安全（共享内普通文件、Range、文本截断、415） | PASS | E2E PREVIEW-01..06、SEC-05/06/08；媒体 Range=206、文本分支 Range 修复后=206、>1MB 文本带 X-Preview-Truncated=1 |
| REQ-28 | 日志下载白名单/防穿越 | PASS | E2E LOGS-03（正常下载含 scan_id）、SEC-07（name=../config.json 400） |
| REQ-29 | 后台删除任务、逐文件结果、重启可查 | PASS | E2E DEL-07/08/15（202+job 轮询、逐文件 recycled/deleted）、RESTART-06（重启后 job 状态可读） |
| REQ-30 | 卸载/升级保留 var 日志 | PASS | static SPK-KEEP-VAR（preuninst/postuninst 无 rm -rf）、SPK-POSTINST；postupgrade 保留 var |
| REQ-31 | 回收站不可用时不静默降级 | PASS（含偏差说明） | E2E DEL-12：所有候选因无 #recycle 被跳过并列出 recycle_unavailable，未删除任何文件；响应码偏差见 §6 D1 |
| REQ-32 | 扫描只出报告、不触发删除 | PASS | E2E SAFETY-01；static BE-NO-AUTODELETE（scanner.py 无 unlink/remove/rename/move） |

## 3. 端到端（E2E）验收方法与证据

命令：`python qa/e2e_test.py`（Python 3.8+ 标准库；Windows 下用 `SetFileTime` 精确设置创建时间，
保证 keep 策略/时间排序可判定；脚本自动创建/清理临时共享根与数据目录，不保留服务或临时目录）。

样本：2 个共享根、46 个文件。覆盖同内容不同名、同大小不同内容、跨根重复、3 副本创建顺序、mtime 反转、
硬链接、扫描后修改、空/隐藏/系统目录（@eaDir/#snapshot/#recycle）、.JPG 大小写、.tmp 排除、大小区间、
4MB 大文件、排除目录、预览/日志样本。是否可创建 symlink 由平台权限决定（本机无权限→UNVERIFIED）。

实测主扫描（scan_id `20261101-204715-946fa3`）关键统计：
`files_seen=38, dirs_scanned=18, candidate_files=29, groups=12, duplicate_files=27,
reclaimable_bytes=4,403,200, bytes_hashed=8,753,152, errors=0,
skipped={hidden:2, empty:2, hardlink:1, excluded:4}`。
全部 114 条记录中 112 PASS、1 UNVERIFIED、1 INFO（DEL-12-NOTE），无 FAIL。

关键流程证据（详见 qa/e2e_results.json）：
- 登录/CSRF：未登录 401、错误密码 401、admin/admin 成功+HttpOnly Cookie、缺 CSRF 403、logout 生效。
- 保留策略：photo 组 keep=photo3（created 最晚，source=birthtime），且 photo3 的 mtime 最小 → 证明未用 mtime 冒充创建时间。
- 结果：跨 2 根同组、排除目录不出现、系统目录/隐藏/空文件计数、硬链接去重、分页/排序/q 搜索。
- CSV：UTF-8 BOM、表头与契约逐字一致、行数=27、保留行“保留/时间来源”正确。
- 预览：PNG 200、文本 200、媒体 Range 206、文本 Range 206、1.5MB 文本截断头、.dat 415、未登录 401。
- 删除：keeper 被拒(IS_KEEPER)、被修改文件跳过(FILE_CHANGED)、回收站 plan(2 文件/40,960B)+两次确认+job completed、
  回收路径 `#recycle/@duplicate-cleaner/<ts>/` 且原路径消失、keeper 仍在、plan 重放被拒、永久删除需短语且无回收站残留。
- scope=all_duplicates：shareB 显式启用回收站后 plan count=2/groups_affected=2，任务 completed，keeper 保留。
- 日志：audit.jsonl 含 scan_start/scan_end/delete_plan/delete_start/delete_file/delete_end；SQLite deletion_results 逐文件结果 ≥2 行。
- 暂停/停止：限速 1MB/s 扫描中 pause→paused、并发扫描 409 SCAN_BUSY、resume→running、stop→stopped。
- 重启：扫描历史/报告/groups/设置 concurrency=3/日志/删除任务仍可读，healthz OK。

## 4. 静态检查方法与证据

命令：`python qa/static_checks.py`（结果 qa/static_results.json，**77 PASS / 0 FAIL / 0 UNVERIFIED**）。

- 后端：30 个 .py 全部 py_compile + `ast feature_version=(3,8)` 通过；unittest 109 通过（2 skipped）；
  scanner 无删除调用；扫描审计点存在；statx 332/291 + AT_SYMLINK_NOFOLLOW；st_ctime 仅在 allow_ctime_fallback 分支；
  删除模块含 #recycle/@duplicate-cleaner/confirm=DELETE/永久删除/revalidate；路由覆盖契约；仅标准库导入；
  src/backend 无调试/测试输出残留。
- 前端：`node --check` 通过；风险文案逐字且不可关闭；契约 17 类端点全覆盖；delete-plan→delete 两步；
  两次确认（“第 1 步/共 2 步”“第 2 步：最终确认”、永久删除短语）；无定时/自动删除；按钮防重复；
  textContent/createTextNode=8、innerHTML=0；无 CDN/外链；ES2019（无可选链/空值合并/BigInt 等）；中文导航；
  文件夹选择器/多选/排除；保留高亮/徽标；预览/CSV/全选/清空；进度轮询；设置键全覆盖。
- SPK 源码：INFO 必需字段/取值；conf JSON + run-as=package + username/groupname duplicatecleaner；port_conf 8787/tcp；ui/config 合法；
  ui/index.html 占位符；6 个生命周期脚本齐全且 `#!/bin/sh`；start-stop-status 显式 unset dev 变量、
  nice/ionice、等待 /api/healthz；preinst 检测 Python3.8+；卸载脚本不删 var；Git Bash 对全部脚本 `bash -n` 通过（LF）。

后端单元测试状态说明：QA 期间曾发现 `test_api.ApiTest.test_scan_groups_csv_preview_delete_flow` 在 Windows
间歇失败（reclaimable 500 vs 600，约 1/5；真实 btime 相同导致 needs_manual_keep，safe 组才计入 reclaimable）。
backend-dev 已改为注入固定创建时间；最终连续 3 次运行 109 tests 全通过。

## 5. 最终 SPK 产物解包验收

独立复核（Python tarfile 解包，不依赖第三方工具）：

| 检查 | 结论 | 证据 |
|---|---|---|
| 外层 tar 结构 | PASS | INFO/package.tgz/PACKAGE_ICON.PNG/PACKAGE_ICON_256.PNG/LICENSE/conf/{privilege,resource}/scripts/{preinst,postinst,preuninst,postuninst,postupgrade,start-stop-status} |
| INFO checksum | PASS | INFO `checksum="e8eeb5a62fbee63b47b13e32453b401a"` = package.tgz 实际 MD5 |
| SHA256 校验文件 | PASS | `.spk.sha256` 内容 = 实际 SHA256 `b16d3622…fc8f` |
| 脚本可执行位 | PASS | 外层 scripts 全部 0755；conf 0644；package 内 app/app.py 0755 |
| package.tgz 结构 | PASS | app/（仅 .py）、web/、ui/、port_conf/、VERSION，共 40 条目 |
| 不含测试/缓存/调试输出 | PASS | 无 tests/、__pycache__、.pyc、.log、test_output、err_report、_debug、rerun.txt |
| 生产无 dev 变量 | PASS | 包内无 `DUP_CLEANER_DEV=1` / `DUP_DEV_ROOTS=`；启动脚本显式 unset |
| 密钥/凭据 | PASS | 未发现私钥/API key；后端 dev 默认密码仅在 dev 分支，生产不启用 dev |
| 前端资源完整 | PASS | web/index.html+app.js+styles.css+assets/favicon.svg；risk 文案逐字存在；cache-bust `?v=1.0.0-0001` |
| ui 占位符 | PASS | `__DUP_BACKEND_SCHEME__`/`__DUP_HOST__` 均已替换（http；当前 DSM 主机） |
| 与当前源码一致 | PASS | 所有 app/web/ui/port_conf 文件 SHA256 与工作区一致（SPK-SYNC） |
| gzip 可复现 | PASS | gzip header mtime=0；tar 固定 mtime/uid/gid |
| 构建可复现 | PASS | 独立 `build_spk.py --no-icons` 两次构建 SHA256 均 = dist 的 b16d3622…，bit-identical |

> 说明：QA 过程中首次最终构建曾包含 `app/rerun.txt`（10,609B，unittest 输出+本地路径）。
> 已由 Lead 删除源文件并在 build_spk.py 增加 app/ 仅 .py 白名单；当前产物已复核无此文件，
> `SPK-PKG-CLEAN`/`SPK-PKG-WHITELIST` 均 PASS。该事件说明构建白名单检查是必要防线。

## 6. 契约偏差与过程性发现

**D1（低危，需知悉）回收站不可用时的响应码**
- 契约 §7 写 delete-plan 返回 200 + plan（含 `recycle_unavailable`）；实现中若“所有候选均因回收站不可用被跳过”，
  返回 **409 RECYCLE_UNAVAILABLE**，details 中带 `recycle_unavailable` 与 skipped 原因。
- 影响评估：不删除任何文件（安全）；前端 `app.js` 已映射 RECYCLE_UNAVAILABLE 中文提示，功能可用；仅与契约字面不符。
  未改动源码，记录为已知偏差。

**D2（环境限制）Windows 符号链接无法构造**
- 本机 `os.symlink` 报 WinError 1314（无 symlink 权限），E2E 的“符号链接被跳过”UNVERIFIED。
- 后端已有实现（scanner 先 `entry.is_symlink()` → skipped.symlink；fsutil/删除重校验拒绝 symlink 路径），
  后端单测在支持平台覆盖 symlink 拒绝，本机该单测自动 skip。建议在 Linux/DSM 上补测。

**D3（设计确认，非缺陷）需人工确认组**
- `needs_manual_keep=true` 的组：后端 v1 一律拒绝自动删除（含 scope=all_duplicates）；
  前端整组禁用勾选并提示“本组不参与删除”。这是比“让用户手选 keeper”更保守的安全策略；
  契约 §10“必须先选保留项”在 v1 由“排除整组”实现，未发现误删路径。

**D4（已修复）文本预览 Range**
- QA 首轮发现文本分支忽略 Range（200 而非 206）；backend-dev 已修复，最终 E2E PREVIEW-03T PASS，
  媒体与文本 Range 均 206。

**D5（已修复）unittest 时间相关 flaky**
- 见 §4 末尾；最终 109 tests 连续 3 次通过。

**D6（已修复）SPK 混入 rerun.txt**
- 见 §5 说明；最终产物洁净。

**D7（契约歧义，已验证自洽）统计字段**
- `reclaimable_bytes` 当前只累计可安全删除的组，并新增 `selectable_duplicate_files/selectable_reclaimable_bytes`；
  E2E STATS-01/02 验证其与 groups/wasted 自洽。契约 §7 未定义 unsafe 组是否计入，建议在契约中补充说明。

## 7. 残余风险与未验证项

1. **真机 DSM 7 / Btrfs / EXT4 未验证**：无 NAS 环境；INFO/statx/路径规则均为静态与 Windows dev 证据，建议真机安装冒烟（安装、启动、登录、扫描、回收站删除、重启后日志）。
2. **浏览器端未做自动化 DOM/E2E**：前端仅 node 语法检查、HTTP 静态页面加载与代码静态审查；未在真实 DSM Chromium/Firefox 中点击走查（两次确认、文件夹选择器、预览弹窗）。建议人工冒烟。
3. **性能/规模未压测**：E2E 仅 ~8.7MB 哈希、46 文件；未验证 50 万文件上限、内存占用、10GB 级大文件限速曲线。
4. **删除计划 5 分钟过期**：E2E 未等待 5 分钟，仅验证一次性/重放拒绝；后端单测 `test_plan_expires` 覆盖过期逻辑。
5. **跨文件系统回收站**：单卷测试，未构造跨设备 rename 失败场景；代码有 CROSS_DEVICE 拒绝分支。
6. **小 TOCTOU 窗口**：plan 与后台删除各重校验一次，但 revalidate 与 rename/unlink 之间仍有极短竞态；已通过二次校验+审计将风险降到很低。
7. **非 root 权限前置**：SPK 以系统内部用户 duplicatecleaner 运行，需管理员在 DSM 共享文件夹 ACL 中授权；默认 HTTP 8787，仅限内网。无独立只读模式。
8. **dev 默认密码**：`DUP_DEV_PASSWORD` 默认 admin 仅 dev 模式；生产启动脚本已 unset，未在产物中发现开启 dev 的路径。
9. **环境限制项**：symlink（D2）、WSL/无 git bash 时 shell 检查会降级；本次已使用 Git for Windows bash。
10. **手工改动风险**：最终产物为 Lead 构建；QA 已用 bit-identical 独立复现证明 dist 与当前源码一致。

## 8. 复现命令

```powershell
# 端到端（自动清理临时目录/服务）
python qa/e2e_test.py

# 静态检查（含 unittest、node --check、风险文案、端点覆盖、SPK 解包/校验/同步）
python qa/static_checks.py

# 后端单测
cd src/backend; python -m unittest discover -s tests -v

# 产物校验
Get-FileHash dist\DuplicateCleaner-1.0.0-0001.spk -Algorithm SHA256
Get-Content dist\DuplicateCleaner-1.0.0-0001.spk.sha256

# 构建可复现性（独立输出目录，清理后不影响 dist）
python build/build_spk.py --no-icons --out-dir $env:TEMP\qa-build1
python build/build_spk.py --no-icons --out-dir $env:TEMP\qa-build2
```

## 9. 签收结论

- 需求覆盖：32 项显式需求中 31 项 PASS、1 项（REQ-13 的 symlink 子项）因 Windows 权限 UNVERIFIED；
  无 FAIL。
- 安全关键路径（哈希判重、创建时间、保留策略、不自动删除、二次确认、服务端重校验、路径白名单、
  回收站/永久删除、审计日志）均有端到端证据。
- 最终 SPK：结构/checksum/权限/洁净性/源码同步/构建可复现全部 PASS，SHA256
  `b16d36224af6db5d4c89681daab45c695de2ba97681324d3a4337033b389fc8f`。
- 交付建议：可交付；上线前按 §7 做一次 DSM 真机 + 浏览器冒烟测试；D1 可在后续版本与契约对齐（409→200+plan 或修订契约）。


---

## 10. 附：v1.1.0-0003 安装权限修复 + 内容相似度复验（2026-11-02）

> 本节为 v1.1 增量验收摘要；完整报告见 [qa/SIMILARITY_REPORT.md](SIMILARITY_REPORT.md)。v1.0 历史结论保留在上文。

- 最终产物：`dist/DuplicateCleaner-1.1.0-0003.spk`
  - SHA256 `bf52609860b1a6b477fa4194597baafc2c22a54055b066ff6eea2f902e763a8d`
  - package.tgz MD5 `33b31053b5546a743f03401c83730ec6`（= INFO.checksum）
  - 大小 215,040 B；package.tgz 41 条目
- 相似模式端到端：`python qa/similarity_e2e.py` → **PASS=44 / FAIL=0 / UNVERIFIED=0**
  （文本相似/低于阈值、精准组隔离、binary/size 跳过、缓存二次命中 8/8、similarity 阶段停止、
  groups match_type 过滤、CSV 12 列、scope 排除相似、无复核 400、复核后回收站删除、keeper 保护、混合删除）。
- 旧精准模式回归：`python qa/e2e_test.py` → **PASS=113 / FAIL=0 / UNVERIFIED=1**（Windows symlink 权限限制）。
- 静态检查：`python qa/static_checks.py` → **PASS=111 / FAIL=0 / UNVERIFIED=0**（v1.1 后端/前端/构建检查 + v1.1.0-0003 非 root 权限/ACL 专项）。
- 后端 unittest：139 tests PASS（2 skipped）。
- 非 root 安装权限修复（v1.1.0-0003）：conf/privilege 改为 run-as=package + username/groupname=duplicatecleaner；
  产物与源码中已无 root 运行/root 属主配置（static SPK-PRIVILEGE/USER/NO-ROOT/DIST-PRIVILEGE 等 PASS）；
  启动脚本校验 var 可写并给出 chown 修复提示，非 root 下 ionice 自动降级。
- **管理员必做 ACL 授权**：首次使用前，在 DSM「控制面板 → 共享文件夹 → 编辑 → 权限 → 系统内部用户」
  给 duplicatecleaner 授予目标共享文件夹的读取/写入权限（需删除副本时；扫描可只给读取），
  并须应用到子文件夹/文件；回收站删除要求对共享文件夹根与 #recycle 也有写权限。
  未授权时目录/扫描会提示权限不足，删除会安全跳过，不会静默删除。旧 root 版本升级需执行
  chown -R duplicatecleaner:duplicatecleaner /var/packages/DuplicateCleaner/var。
- SPK：checksum/权限/ACL 提示/洁净性/源码同步全部 PASS；独立构建 2 次 SHA256 均与 dist bit-identical。
- 已知风险：真实 DSM 安装与 ACL 授权流程未真机验证（环境限制，上线前必做）；相似度质量/性能未大规模评估；
  图片/PDF/DOCX 相似仅单测覆盖；CONTRACT §7 CSV 列已同步为 12 列；Windows symlink UNVERIFIED。
- 结论：v1.1.0-0003 **PASS（可交付）**，详见 [qa/SIMILARITY_REPORT.md](SIMILARITY_REPORT.md)。

---

## 11. 附：v0028 目录批量字幕 + 写回结果可见性复验（2026-11-05）

> 本节为 v0028/v0029 增量验收摘要；接口与行为契约见 [docs/SUBTITLE_BATCH_CONTRACT.md](../docs/SUBTITLE_BATCH_CONTRACT.md)。v1.0/v1.1 历史结论保留在上文。

### 11.1 最终产物

- `dist/DuplicateCleaner-1.1.0-0029.spk`
  - SHA256 `c2729457c18f8bbf2d7d9f7c870c42ee0a02afb9d205bd20fd3bf5af81373e87`
  - package.tgz MD5 `538f9bb9ea5b6df7c4cb296427d9fbfc`（= INFO.checksum）
  - 大小 38,963,200 B
- 版本 1.1.0-0029 同步 8 处；SQLite schema v6（新增 `subtitle_batches`，v5→v6 只建表）。

### 11.2 结果总览

| 套件 | 命令 | 结果 |
|---|---|---|
| 后端单测 | `cd src/backend && python -m unittest discover -s tests` | **372 tests OK（5 skipped）** |
| 端到端 | `python qa/e2e_test.py` | **PASS=123 / FAIL=0 / UNVERIFIED=1**（Windows symlink 权限限制） |
| 静态检查（含 unittest/前端/SPK） | `python qa/static_checks.py` | **PASS=126 / FAIL=0 / UNVERIFIED=0** |
| 前端模块图 | `node qa/check_web_modules.mjs` | **22/22 PASS** |
| 无头浏览器冒烟 | `python qa/web_smoke.py` | **8/8 PASS** |

### 11.3 本轮新增能力与验收要点

- 独立「字幕」页：目录枚举（递归/跳过系统与隐藏/符号链接）、筛选、勾选、批量提交、批次进度（暂停/继续/取消/重试失败项）；目录选择复用「扫描范围」选择器。
- 字幕写回视频同目录：`.srt` 原子写、不覆盖已有文件；不可写回退为任务目录下载（`OUTPUT_NOT_WRITABLE` / `OUTPUT_WRITE_PARTIAL`）。
- `source=existing_srt` 只翻译已有字幕：不启动 FFmpeg、不调用 ASR、不上传音频。
- 历史任务列表/详情、视频结果行完成弹窗展示来源、写回路径、跳过原因与写回警告；设置页可配置输出方式/默认跳过/任务保留上限；前端限额取自 `/api/meta.subtitle`。
- e2e 新增 SUB-01…SUB-10：枚举结构、非法目录/跨共享根拒绝、总开关关闭拒绝、设置保存、空/超限/全无效批量、未知批次、批次过滤。

### 11.4 残余风险与未验证项

- 真实 DSM 安装、ACL 授权与视频目录写回未真机验证（上线前必做）；只读目录应表现为警告 + 任务目录可下载。
- `subtitle_job_limit` 修改需重启套件生效（已在设置项与契约注明）。
- 写回视频目录的 srt 不提供网页下载（仅任务目录副本可下载），避免构造任意路径下载接口。
- Windows 无 symlink 权限导致 SKIP-04 仍为 UNVERIFIED（与历史一致）。

- 结论：v0028 **PASS（可交付）**。

---

## 12. 附：v0030 本地 ASR/LLM 接入核对与“禁用思考”复验（2026-11-07）

> 本节为 v0030 增量验收摘要；契约见 [docs/SUBTITLE_CONTRACT.md](../docs/SUBTITLE_CONTRACT.md) §1/§5。背景：对照 `DEPLOYMENT_SUMMARY.md` 核对群晖套件与 zyserver 本地服务（FunASR `10.10.10.233:8090`、Strata `10.10.10.233:8080`）的接入方式。

### 12.1 接入核对结论

- **视频大小/时长无需改接入方式**：套件从不上传原视频，始终用内置 FFmpeg 抽 `16kHz 单声道 32kbps MP3` 分块（默认 600s / ≤24MB）后上传；ASR 的「单文件 ≤800MB、音频 ≤2h、并发 1 路」三条限制因此不构成约束（实测每片约 1.9MB、44.6s，RTF 0.074）。
- **LLM 本地推理模型需关闭“思考”**：Strata 的 Qwen3.6-35B-A3B 默认输出大量 reasoning，20 条 cue 翻译耗时 47.8s（completion 3475 tokens）；附加 `chat_template_kwargs.enable_thinking=false` 后 3.4s，输出仍为合法 20 条 JSON 数组（约 14 倍）。v0030 做成可选设置（默认关，兼容 OpenAI 官方等不支持该参数的服务）。
- **译文回退需可见**：`translation_partial` 此前只在接口返回，列表/详情不提示；v0030 在字幕任务列表与任务中心详情显示“部分译文回退原文”。

### 12.2 最终产物

- `dist/DuplicateCleaner-1.1.0-0030.spk`
  - SHA256 `b5cf8a6c96d7f3cd7ed5ea60d068c5199f460c5335ffb251288824782acc8aac`
  - package.tgz MD5 `530f821539ea7b14bfe8ef733e65719e`（= INFO.checksum）
  - 大小 38,963,200 B
- 版本 1.1.0-0030 同步 8 处；SQLite schema v6 不变（无新增迁移）。

### 12.3 结果总览

| 套件 | 命令 | 结果 |
|---|---|---|
| 后端单测 | `cd src/backend && python -m unittest discover -s tests` | **373 tests OK（5 skipped）** |
| 端到端 | `python qa/e2e_test.py` | **PASS=123 / FAIL=0 / UNVERIFIED=1**（Windows symlink 权限限制） |
| 静态检查（含 unittest/前端/SPK） | `python qa/static_checks.py` | **PASS=129 / FAIL=0 / UNVERIFIED=0** |
| 前端模块图 | `node qa/check_web_modules.mjs` | **22/22 PASS** |
| 无头浏览器冒烟 | `python qa/web_smoke.py` | **8/8 PASS** |

### 12.4 真实服务联调（开发模式套件 → 局域网本地服务）

| 场景 | 结果 |
|---|---|
| 20 分钟视频（1200s）→ 本地 ASR | 2 个 600s 分片，**92.7s** done，SRT 写回视频旁（61 条 cue） |
| 45s 视频 → ASR + 翻译（禁用思考关） | 全链路 **75.1s**（翻译约 70s），`original`/`.en`/`bilingual.en` 三个 SRT 正确 |
| 同一视频 → 开启“禁用思考” | 全链路 **7.1s**（翻译约 2s），输出内容一致，`translation_partial=false` |
| ASR 单分片 | 600s 音频 44.6s（RTF 0.074），远低于 300s 默认超时 |

### 12.5 新增/变更

- 设置项 `subtitle_llm_disable_thinking`（默认 false）：翻译与「测试连接」请求附带 `chat_template_kwargs.enable_thinking=false`；设置页新增复选框与说明。
- 字幕任务列表/任务中心详情显示“部分译文回退原文”（`translation_partial`），不再静默。
- e2e SUB-05 覆盖新设置保存/回读；新增静态检查 `BE-SUBTITLE-THINKING`、`FE-SUBTITLE-THINKING`、`FE-SUBTITLE-PARTIAL`。

### 12.6 残余风险与未验证项

- “禁用思考”是 llama.cpp / vLLM / Strata 等扩展参数；不兼容的服务保持默认关闭，失败时按契约回退原文并标记。
- 真实 DSM 安装、ACL 授权与视频目录写回仍未真机验证；本次本地联调使用开发模式套件与局域网真实 ASR/LLM。
- NAS 套件当前 `subtitle_translate_enabled=true` 但 LLM 未配置（Key 未设置、Base URL 仍为公网默认）：需配置本地 LLM（并建议勾选“禁用思考”）或先关闭翻译开关，否则译文会全部回退原文（v0030 起界面会提示）。

- 结论：v0030 **PASS（可交付）**。

---

## 13. 附：v0031 字幕页「提交批量字幕任务」按钮可用性修复（2026-11-07）

> 现场反馈：NAS（1.1.0-0030）在「字幕」页勾选视频后「提交批量字幕任务」按钮点不动。用无头 Chromium（CDP）直连 NAS 页面复现并定位。

### 13.1 定位结论

- 按钮逻辑本身正常：勾选视频 + 勾选「我了解音频/字幕文本将上传…」后按钮即可点击（复现中 `submitDisabled=false`）。
- 真正的 UX 缺陷：
  1. 禁用原因只写在按钮 `title` 里，界面不可见（不悬停就以为按钮坏了）；
  2. 「全选」会把全部筛选结果一次性加入——该目录枚举到 5000 个，全选后远超单批上限 500 → 按钮一直禁用，提示只有一行小字；
  3. 最常见误操作是未勾选确认项，同样只有 tooltip 说明。

### 13.2 修复内容

- 按钮下方新增常显原因 `#subtitleSubmitReason`：未选视频 / 已选超单批上限 / 未勾确认项 / 云端字幕未启用或缺凭据 / 处理中；按钮重新可用时清空。
- 「全选」只选中前 `batch_max_items`（500）个并 toast 提示「其余请分批提交或用筛选缩小范围」，不再一次选中 5000 个锁死按钮。
- 选择计数统一千分位（`fmtInt`）。
- 静态检查新增 `FE-SUBTITLE-SUBMIT-REASON`。

### 13.3 验证

- 无头 Chromium 直连 NAS 复现：按钮在「1 个视频 + 勾选确认项」时可点击；「全选 5000 个」时禁用且原因可见（修复前仅 tooltip）。
- 本地 dev 模式（520 个视频）验证修复：全选 → 选中 500（不再超限），原因提示“请勾选确认项”；勾选确认项后按钮可用（`submitDisabled=false`）。
- 回归：unittest 373 OK（5 skipped）；static **PASS=130 / FAIL=0**；e2e **PASS=123 / FAIL=0 / UNVERIFIED=1**（Windows symlink）；web modules 22/22；web smoke 8/8。

### 13.4 最终产物

- `dist/DuplicateCleaner-1.1.0-0031.spk`
  - SHA256 `a4f040ba0f06a0c7328c1d53d18ab3c7e1e0f674a7bd767cba33beb5e8a8046f`
  - package.tgz MD5 `91c6c37e113784c72fbf7b02459c4a6f`（= INFO.checksum）
  - 大小 38,963,200 B
- 版本 1.1.0-0031 同步 8 处；后端无行为变更（仅前端/样式/文档/QA）。

- 结论：v0031 **PASS（可交付）**。

---

## 14. 附：v0032 批次组 / 删除后刷新 / 服务器解码（2026-11-07）

> 现场反馈三项：①「全选」只能选 500 不合理；② 删除文件后结果页仍显示已删除；③ 希望用服务器算力转音频。本节为 v0032 增量验收；契约见 `docs/SUBTITLE_BATCH_CONTRACT.md` §13、`docs/SUBTITLE_CONTRACT.md` §3/§12、`docs/STORAGE_CONTRACT.md` §2。

### 14.1 能力与设计

- **批次组**：`POST /api/subtitles/batch-groups` 一次最多 20000 个视频，后端按每批 ≤500 自动拆批并共享 `group_id`；组快照聚合进度/计数/批次列表，`/pause|resume|cancel|retry` 作用于整组；任务明细分页 `GET /api/subtitles?group_id=&offset=&limit=`。组按 `SUBTITLE_BATCH_GROUP_LIMIT=20` 保留，组内任务不参与单任务 LRU。枚举上限提升到 20000，前端「全选」不再截断。
- **删除后刷新**：schema v7 新增 `scan_file_deletions`；删除成功（recycled/deleted）或文件已不存在（NOT_FOUND）时写入标记，`iter_scan_groups` / `query_scan_groups_page` / `scan_file_stats` / CSV / XLSX 读取时剔除；组内剩余 <2 个文件整组消失；keeper 被删时重算保留项与 `wasted_bytes`。前端删除结束后重置分页/选择并重拉，空页自动回退上一页。
- **服务器解码**：新设置 `subtitle_audio_source`（`nas` 默认 / `server`）。server 模式下 NAS 只用 FFmpeg **流复制**抽音轨（`-map 0:a:0 -c copy`，不解码），上传 `.mka` 音频容器给 ASR 服务器解码转音频（仍只上传音频、不上传视频）；单段超 780MB 继续对半切；流复制失败回退 NAS 解码并置 `warning=SERVER_AUDIO_FALLBACK`。音频来源在提交时快照到 job。

### 14.2 最终产物

- `dist/DuplicateCleaner-1.1.0-0032.spk`
  - SHA256 `cf8fcef316b4613aac38a6c51c24fdc30170f4b22bb21900ff79bca723f4c305`
  - package.tgz MD5 `4ff5e5fdef2a7f844402480e6629db6c`（= INFO.checksum）
  - 大小 38,973,440 B
- 版本 1.1.0-0032 同步 8 处；SQLite schema v7（新增 `subtitle_batch_groups`、`scan_file_deletions`，只建表/索引）。

### 14.3 结果总览

| 套件 | 命令 | 结果 |
|---|---|---|
| 后端单测 | `cd src/backend && python -m unittest discover -s tests` | **382 tests OK（5 skipped）** |
| 端到端 | `python qa/e2e_test.py` | **PASS=130 / FAIL=0 / UNVERIFIED=1**（Windows symlink 权限限制） |
| 静态检查（含 unittest/前端/SPK） | `python qa/static_checks.py` | **PASS=136 / FAIL=0 / UNVERIFIED=0** |
| 前端模块图 | `node qa/check_web_modules.mjs` | **22/22 PASS** |
| 无头浏览器冒烟 | `python qa/web_smoke.py` | **8/8 PASS** |

### 14.4 新增/更新测试

- 单测：批次组拆批/聚合快照/任务分页/组控制/保留清理（5 例）；server 音频模式 remux、流复制失败回退、超大分块对半切（3 例）；`scan_file_deletions` 过滤分组/统计/迭代（1 例）；schema v7。
- e2e：SUB-11..SUB-16（meta 上限/批次组空路径/无有效路径/>20000/未知组 404/group_id 分页）、DEL-20（删除成功后 groups 不再返回已删除文件且整组消失）。
- 静态：新增 `BE-SUBTITLE-GROUP`、`BE-SCANDEL-FILTER`、`BE-SUBTITLE-AUDIO`、`FE-SUBTITLE-GROUP`、`FE-SUBTITLE-AUDIO`、`FE-DELETE-REFRESH`；更新字幕页端点/写回/轮询/提交原因断言为批次组语义。

### 14.5 真实服务联调（开发模式套件 → 局域网 ASR）

| 场景 | 结果 |
|---|---|
| 3 个 30s 视频批次组（nas） | 组 done 3/3、8.1s；任务分页 3 条、SRT 写回 OK；组控制终态幂等 |
| 3 个 30s 视频批次组（server） | 组 done 3/3；NAS 流复制 `.mka` → 服务器解码 + ASR，`warning=null`、SRT 写回 OK |
| 无头浏览器（字幕页） | 全选 2 个 → 提示“将分为 1 批”、禁用原因常显；勾选确认项后可提交；组卡 排队中→已完成 2/2，任务行 2、localStorage 保存 group_id、无控制台错误 |

### 14.6 残余风险与未验证项

- 真实 DSM 上的 5000 级全选（串行处理时长/磁盘增长）与删除后刷新仍未真机验证；建议先在 NAS 用 50–200 个视频验证。
- server 音频模式依赖原音轨可被流复制（AAC/AC3/DTS/PCM 等容器内常见编码均可）；少数编码需回退 NAS 解码（已有 warning）。
- 升级前已删除的旧数据没有标记记录，仍会显示；如需清理请重新扫描。

- 结论：v0032 **PASS（可交付）**。

---

## 15. 附：v0033 字幕默认只生成 SRT（VTT 可选）（2026-11-07）

### 15.1 变更

- 新设置 `subtitle_write_vtt`（默认 **false**）：任务目录默认只生成 `original.srt`（翻译时 `<lang>.srt`、`bilingual.<lang>.srt`）；
  开启后才额外生成 `original.vtt`、`<lang>.vtt`。视频同目录始终只写 `.srt`（原有约束不变）。
- 单视频弹窗「首选下载格式」仅在开启 VTT 时显示 VTT 选项；未开启时提示“默认只生成 SRT”。
- 历史任务已生成的 VTT 仍可下载（下载白名单不变）；如需释放空间由任务 LRU 清理。

### 15.2 最终产物

- `dist/DuplicateCleaner-1.1.0-0033.spk`
  - SHA256 `e683ef80ade22479f052b1597f1a2936cdadd427eae22a8fda7e56a4bdd4c8e7`
  - package.tgz MD5 `acae407a6390096e7dc36c5c104faa2a`（= INFO.checksum）
  - 大小 38,973,440 B
- 版本 1.1.0-0033 同步 8 处；SQLite schema 仍为 v7（无新增迁移）。

### 15.3 结果总览

| 套件 | 命令 | 结果 |
|---|---|---|
| 后端单测 | `cd src/backend && python -m unittest discover -s tests` | **383 tests OK（5 skipped）** |
| 端到端 | `python qa/e2e_test.py` | **PASS=130 / FAIL=0 / UNVERIFIED=1**（Windows symlink 权限限制） |
| 静态检查（含 unittest/前端/SPK） | `python qa/static_checks.py` | **PASS=138 / FAIL=0 / UNVERIFIED=0** |
| 前端模块图 | `node qa/check_web_modules.mjs` | **22/22 PASS** |
| 无头浏览器冒烟 | `python qa/web_smoke.py` | **8/8 PASS** |

### 15.4 验收要点

- 默认行为：`test_happy_path_with_translation` 断言任务目录文件为 `original.srt/zh.srt/bilingual.zh.srt`，无 `.vtt`。
- 可选行为：`test_write_vtt_optional` 开启设置后生成 `original.vtt/zh.vtt`，且视频同目录仍无 `.vtt`。
- 设置：默认 false、布尔校验；e2e SUB-05 覆盖保存/回读，恢复默认时置回 false。
- 静态：新增 `BE-SUBTITLE-VTT`、`FE-SUBTITLE-VTT`；设置键清单加入 `subtitle_write_vtt`。

- 结论：v0033 **PASS（可交付）**。

## 16. 附：v0034 队列恢复 / 并发 / 任务可见性（2026-11-07）

### 16.1 现象与根因（NAS 实测，1.1.0-0033）

用户反馈：“字幕任务明细看不到执行中任务，全部排队中；任务中心字幕任务也全是排队中。”

只读探测（10.10.10.236:8787，批次组 `grp-20261107-100806-a40ce8`）：

- 组共 7359 个任务；已完成 19、执行中 1、排队 7340；组状态 `running`（任务实际在跑，约 1–2 分钟/个）。
- 任务列表接口按 `created_epoch` 倒序分页（100/页），而 worker 按 FIFO 从最早注册的任务开始执行：执行中任务是“最老”的任务，永远落在最后一页（74/74）。
- 任务中心 `GET /api/subtitles?limit=20` 同样倒序，最新 20 个全是排队，进行中计数为 0。
- 另发现：旧版启动时把 `queued`（无分片）任务判为 `SERVICE_RESTART` failed，升级套件会丢掉整个排队队列（7340 个）。

### 16.2 修复内容

- **P0 重启恢复**：`_load_jobs` 对 `queued` 不再判失败，保留状态并按创建时间重新入队；`__init__` 队列非空即自动拉起 worker。`running/translating` 有分片仍转 `paused` 可继续、无分片仍 `SERVICE_RESTART` failed；`paused` 不变。
- **P1 可见性**：`GET /api/subtitles` 新增 `status` 过滤（all/active/queued/…/failed，支持逗号组合如 `failed,cancelled`；均非法→400）；批次组快照新增 `active_jobs`（≤10 条执行中任务明细）、`eta_seconds`、`concurrency`；前端组卡片显示“当前执行：文件 · 阶段 · 进度%”与“预计剩余”，任务明细固定“执行中”置顶区块 + 状态筛选页签（全部/执行中/排队/已完成/失败，失败含取消），任务中心字幕标签先取 `status=active` 置顶“进行中”分区再列最近任务。
- **P2 预计剩余**：按墙钟吞吐估算（组内最早创建时间 → 平均每个已处理任务耗时 × 剩余 ÷ 并发），样本不足为 `null`，避免用含排队时间的 lifetime 放大 ETA。
- **P3 并发**：新增设置 `subtitle_concurrency`（1..4，默认 1，服务启动时读取、重启生效），多 worker 共享 FIFO 队列；默认 1 与旧串行行为完全一致，`/api/meta.subtitle.concurrency_max=4`。
- 无数据库迁移（SQLite schema 仍 v7）；文档同步 SUBTITLE_CONTRACT §1/§2/§14、SUBTITLE_BATCH_CONTRACT §2/§4.4/§13/§14、UI_PROTOTYPE、ARCHITECTURE §12.4、CONTRACT v0034 增量。

### 16.3 最终产物

- `dist/DuplicateCleaner-1.1.0-0034.spk`
  - SHA256 `70730e2a8395a8dafa575d3bba570e5ff7be9b6588b4b066257eaf44577c45a1`
  - package.tgz MD5 `5a3c3d3b1ac50e2b1c0c3d937a1593ce`（= INFO.checksum）
  - 大小 38,983,680 B
- 版本 1.1.0-0034 同步 8 处；`qa/static_checks.py` SPK-SYNC 通过。

### 16.4 结果总览

| 套件 | 命令 | 结果 |
|---|---|---|
| 后端单测 | `cd src/backend && python -m unittest discover -s tests` | **397 tests OK（5 skipped）** |
| 端到端 | `python qa/e2e_test.py` | **PASS=135 / FAIL=0 / UNVERIFIED=1**（Windows symlink 权限限制） |
| 静态检查（含 unittest/前端/SPK） | `python qa/static_checks.py` | **PASS=144 / FAIL=0 / UNVERIFIED=0** |
| 前端模块图 | `node qa/check_web_modules.mjs` | **22/22 PASS** |
| 无头浏览器冒烟 | `python qa/web_smoke.py` | **8/8 PASS** |

### 16.5 验收要点

- 后端新增：并发默认 1 串行（running 峰值 1）/ 设 2 时两任务同时 running（状态峰值与 fake ASR in-flight 峰值均 2）、`shutdown` 停全部 worker、`status` 过滤（active/queued/done/failed/逗号组合/非法 400）、组快照 `active_jobs`/`eta_seconds`（样本不足 null）、queued 重启自动续跑并产出 srt、旧 `{ok,jobs}` 形状兼容、meta `concurrency_max/default_concurrency`。
- 设置：默认 1、范围 1..4，拒绝 bool/浮点/字符串；e2e SUB-19/SUB-20 覆盖保存与越界；恢复默认置回 1。
- 前端：新增 `FE-SUBTITLE-CONCURRENCY`、`FE-SUBTITLE-VISIBILITY`、`FE-SUBTITLE-CENTER-ACTIVE` 静态检查；DOM-stub 实测 7359 任务单执行中场景：当前执行行、ETA、筛选计数、置顶区块、`&status=active` 请求、任务中心“进行中/最近任务”分区均正确。
- e2e 新增 SUB-17（status=active 只返回执行中）、SUB-18（逗号组合）、SUB-19（非法 status 400）、SUB-20/21（并发设置保存/越界）。

- 结论：v0034 **PASS（可交付）**；升级 1.1.0-0034 后 NAS 上 7340 个排队任务会保留并自动续跑。

## 17. 附：v0035 补翻译（只做了 ASR、没翻译的任务单独翻译，2026-11-07）

### 17.1 需求

用户：“如果只进行了 ASR 没有翻译，则需要能单独进行翻译”。即历史任务（或提交时未勾选“同时生成译文”的批次）跑完 ASR 后，能**只补翻译**，不重新抽音频、不重新 ASR、不重复上传音频。

现有 `source=existing_srt` 只翻译模式不满足：它要求**视频同目录存在 `<stem>.srt`**、需重新枚举目录，且在 `download_only` 输出、视频已删除、任务目录是唯一副本时全部失效；任务明细/任务中心也没有任何补翻译入口。

### 17.2 实现

- **派生任务，不改源任务**：补翻译新建一条仅翻译任务（`source=existing_srt` + `source_job_id` 指向源任务），源任务 status/文件/审计/updated_at 完全不变。
- **源文本解析**（`_resolve_source_text`）：源任务目录 `original.srt` → 视频同目录 `<stem>.srt` → `SOURCE_SRT_MISSING`。视频文件已删除也能翻译（只写任务目录，写回视频旁按既有 warning 降级）；不调用 `shares.validate_file`。
- **接口**：
  - `POST /api/subtitles/{job_id}/translate`（202 新建 / 200 同源幂等），body `{"confirm_cloud":true,"target_lang":"zh"}`；
  - `POST /api/subtitles/batch-groups/{group_id}/translate`（恒 202），服务端分类统计 eligible/created/already_translated/missing_source/duplicates/rejected，按 ≤500 拆批新建翻译批次组（不混入源组，options 带 `translated_from_group`/`translated_from_job`）。
  - 批次组快照新增 `translatable={count, partial}`（纯内存计数、零逐任务 stat），供“补翻译（N）”按钮。
- **资格与错误码**：done 且（`translate=false` 或 `translation_partial=true`）；已有完整译文 `TRANSLATION_EXISTS`、未完成 `JOB_NOT_DONE`、开关关 `TRANSLATE_DISABLED`、LLM Key 空 `TRANSLATE_NOT_CONFIGURED`、缺云端确认 `CLOUD_CONFIRM_REQUIRED`（均 400）。
- **重启恢复**：带 `source_job_id` 的 running/translating 任务启动时转 `paused` 可继续（无 ASR 分片损失）；queued 沿用 v0034 自动续跑。
- **前端**：批次组卡片「补翻译（N）」/仅 partial 时「重新翻译（M）」；任务明细行/任务中心列表/详情弹窗「补翻译/重新翻译」；来源标签「补翻译（源任务 sub-…）」；三级能力门控（总开关/翻译开关/LLM Key），确认弹窗明确“只上传字幕文本、不上传音频/视频”。
- **无 schema 变更**（SQLite 仍 v7，`source_job_id` 只在 payload_json）；文档同步 SUBTITLE_CONTRACT §15、SUBTITLE_BATCH_CONTRACT §1/§7.1/§10/§13.2/§15、UI_PROTOTYPE、ARCHITECTURE §12.5、CONTRACT v0035 增量。

### 17.3 最终产物

- `dist/DuplicateCleaner-1.1.0-0035.spk`
  - SHA256 `b8fd35be001bc6785bf3bf74c898b20a5b2cbdd17cc6ad4fa3c84a0840fa9e6c`
  - package.tgz MD5 `2dcef5cbd2ae87bf9d11838fd92e244c`（= INFO.checksum）
  - 大小 38,993,920 B
- 版本 1.1.0-0035 同步 8 处；`qa/static_checks.py` SPK-SYNC 通过。

### 17.4 结果总览

| 套件 | 命令 | 结果 |
|---|---|---|
| 后端单测 | `cd src/backend && python -m unittest discover -s tests` | **412 tests OK（5 skipped）** |
| 端到端 | `python qa/e2e_test.py` | **PASS=142 / FAIL=0 / UNVERIFIED=1**（Windows symlink 权限限制） |
| 静态检查（含 unittest/前端/SPK） | `python qa/static_checks.py` | **PASS=149 / FAIL=0 / UNVERIFIED=0** |
| 前端模块图 | `node qa/check_web_modules.mjs` | **22/22 PASS** |
| 无头浏览器冒烟 | `python qa/web_smoke.py` | **8/8 PASS** |

### 17.5 验收要点

- 后端新增 15 例（`tests/test_subtitle_translate.py`）：ASR-only 补翻译生成 `<target>.srt`/`bilingual.<target>.srt` 且源任务快照不变、无 FFmpeg/无重复 ASR、`TRANSLATION_EXISTS`、partial 重译、开关/密钥/confirm/非法参数/404/JOB_NOT_DONE、`SOURCE_SRT_MISSING`、**视频已删除仍可从任务目录翻译**、同源幂等（active 200 同 job、完成后可再派生）、组批量混合分类计数+新组 total/options+源组只读、`translatable` 计数、重启 paused+resumable（普通任务仍 SERVICE_RESTART）、两个 API 状态码/响应形状/CSRF。
- e2e 新增 SUB-22（翻译未启用 400）、SUB-23（保存翻译开关/LLM Key 且不回显密钥）、SUB-24（未知任务 404）、SUB-25（缺 confirm 400）、SUB-26（`target_lang=auto` 400）、SUB-27（未知组 404）、SUB-28（未配 LLM Key 400）。
- 静态新增 `BE-SUBTITLE-TRANSLATE-ONLY`、`BE-SUBTITLE-TRANSLATE-STATE`、`BE-SUBTITLE-TRANSLATE-TESTS`、`FE-SUBTITLE-TRANSLATE`、`FE-SUBTITLE-TRANSLATE-GATE`。
- 前端 DOM-stub 实测：组按钮/明细行按钮→确认→POST body→摘要 toast→切换新组并加载明细；任务中心列表/footer 按钮、来源标签、点击后刷新均正常。

### 17.6 已知边界

- `summary.eligible` = 满足资格且源文本可解析（含随后计入 `duplicates` 的任务），`created` 为实际新建数（已在契约 §15.2 注明）。
- 单任务补翻译也会创建一个仅含 1 个任务的翻译批次组（便于任务被组保留策略保护、并在批次组视图查看）；组保留上限 20 的既有清理规则不变。
- 旧版本已判失败的历史任务不会被自动复活；组级补翻译会逐任务读取源 `original.srt`（20000 任务规模有一次性 IO 成本，组快照计数不受影响）。
- 真机验证待办：NAS 安装 1.1.0-0035 后，对“仅 ASR”的已完成批次组点击「补翻译」，确认新组转 `translating`、产生译文并写回/可下载。

- 结论：v0035 **PASS（可交付）**；升级 1.1.0-0035 后可用「补翻译」处理历史 ASR-only 任务，且不重新消耗 ASR 算力。

## 18. 附：v0036 设置检查与两处修复（语言码 / 译文输出组合，2026-11-07）

### 18.1 设置审计结论

对 64 个后端设置键做全量核对（`settings.default_settings()` × 校验分支 × 设置页填充/保存 × `constants.DEFAULT_SETTINGS` 镜像 × 文档 × 后端消费点）：

- 校验分支 64/64、设置页保存 64/64、前端默认值镜像 64/64、后端均有消费点（`recycle_default` 仅作前端删除默认值、`skip_symlinks` 后端强制 true，均属设计）。
- 设置页填充仅 3 个例外且都合理：`skip_symlinks`（强制，禁用勾选）、两个 API Key（只显示“已配置/未配置”，不回显明文）。
- **无漏接线设置**；但发现两个功能选项缺口，v0036 修复。

### 18.2 修复内容

- **语言码**：设置页 ASR 识别语言/翻译目标语言下拉由 4 种扩展为常见 19 种（zh/en/ja/ko/es/fr/de/it/pt/ru/ar/hi/th/vi/id/ms/tr/nl/pl/uk/sv）；`readSubtitleSettings` 改为按后端同一 `LANGUAGE_RE` 校验，API 设置的其它合法语言码不再被静默重置为 `auto`/`zh`，并在下拉中临时补项显示（`fillLanguageSelect`/`ensureLanguageOption`）。
- **译文输出组合**：新增设置 `subtitle_translate_outputs`（`both` 默认 / `target_only` 只写译文 / `bilingual_only` 只写双语），`_write_outputs` 与 `_write_alongside` 统一裁剪任务目录与视频同目录的译文文件；`original.srt` 始终保留（补翻译源文本）；`target_only` 仍写 `<target>.vtt`，`bilingual_only` 不写 `<target>.vtt`；已有文件从不覆盖、修改设置不删除旧文件。普通翻译、只翻译已有字幕、v0035 补翻译三类任务都遵循该设置。
- 设置页新增「译文输出组合」下拉与错误位；`state.IDS` 补 id；前端默认值镜像补键。

### 18.3 最终产物

- `dist/DuplicateCleaner-1.1.0-0036.spk`
  - SHA256 `bf849585c1dced09390e598689e5aa676680a370bc6adcd4a7aa15a294d29903`
  - package.tgz MD5 `230ab54d6b67b1328bc28529c2f3f336`（= INFO.checksum）
  - 大小 38,993,920 B
- 版本 1.1.0-0036 同步 8 处；SQLite schema 不变（仍 v7）。

### 18.4 结果总览

| 套件 | 命令 | 结果 |
|---|---|---|
| 后端单测 | `cd src/backend && python -m unittest discover -s tests` | **419 tests OK（5 skipped）** |
| 端到端 | `python qa/e2e_test.py` | **PASS=146 / FAIL=0 / UNVERIFIED=1**（Windows symlink 权限限制） |
| 静态检查（含 unittest/前端/SPK） | `python qa/static_checks.py` | **PASS=152 / FAIL=0 / UNVERIFIED=0** |
| 前端模块图 | `node qa/check_web_modules.mjs` | **22/22 PASS** |
| 无头浏览器冒烟 | `python qa/web_smoke.py` | **8/8 PASS** |

### 18.5 验收要点

- 后端新增 7 例（`tests/test_subtitle_outputs.py`）：默认 both 产出两个译文文件；`target_only` 不写双语；`bilingual_only` 不写单语；VTT 随组合裁剪（bilingual_only 不写 `<target>.vtt`）；只翻译已有字幕与 v0035 补翻译任务同样遵循设置；设置默认值与非法值校验。
- e2e 新增 SUB-29（保存 target_only）、SUB-30（回读）、SUB-31（非法值 400）、SUB-32（API 保存 asr=es/target=fr）。
- 静态新增 `BE-SUBTITLE-TRANSLATE-OUTPUT`、`FE-SUBTITLE-TRANSLATE-OUTPUT`、`FE-SUBTITLE-LANGUAGES`；`FE-SETTINGS-KEYS` 覆盖新键。
- 前端模块检查/web smoke 通过；语言下拉与输出组合设置在设置页可见、可保存、回读正确。

## 19. 附：v0037 字幕并发设置保存后立即生效（2026-11-07）

### 19.1 需求与行为变更

- 需求：设置页「字幕并发任务数」改为**保存后立即生效**，不再要求重启套件。
- 后端：`SubtitleService.apply_settings(settings)` 读取并规范化 `subtitle_concurrency` 后立即调整 worker 池（`_sync_workers_locked`）：
  - **调大** → 立即补 worker，排队任务马上按新并发开始；
  - **调小** → 多余 worker 进 `_retiring`，在任务边界退出，**正在执行的 ASR/翻译不中断**；
  - 没有 worker/排队任务时保持惰性，下一次提交按新并发启动；
  - `PUT /api/settings` 保存成功后调用（补丁含 `subtitle_concurrency` 时）。
- 前端：设置页提示改为“默认 1 为串行、最稳；可设 2–4 并行。保存后立即生效，无需重启套件。”
- 无新增接口/错误码；请求/响应结构不变；SQLite schema 仍 v7，无迁移。

### 19.2 测试证据

- 后端单测 **422 tests OK（5 skipped）**，新增 3 例：
  - `SubtitleConcurrencyTest.test_apply_settings_raises_concurrency_without_restart`：并发 1 时第二个任务排队；保存设置调到 2 后**不重启**立即并行（ASR 并发峰值 2）；
  - `test_apply_settings_lowers_concurrency_and_retires_workers`：调小到 1 时多余 worker 在任务边界退出、剩余 worker 存活；调回 2 立即补回；
  - `SubtitleApiTest.test_settings_concurrency_applies_without_restart`：`PUT /api/settings` 后服务内 `concurrency` 立即变为 2/1。
- 静态检查：`BE-SUBTITLE-CONCURRENCY` 校验 `apply_settings`/`_sync_workers_locked`/`self._retiring`/`apply_settings(updated)`；`FE-SUBTITLE-CONCURRENCY` 校验设置页“保存后立即生效”提示。
- e2e **146 PASS / 0 FAIL / 1 UNVERIFIED**（Windows symlink）；静态 **152/152**；前端模块 **22/22**；web smoke **8/8**。

### 19.3 最终产物

- `dist/DuplicateCleaner-1.1.0-0037.spk`
  - SHA256 `566b50551713740a99a840c166259149f086b3cf9de7bfff7af8d7c98966c8fd`
  - package.tgz MD5 `02aa7d5bd0d3c758312b5c3d4c069b33`（= INFO.checksum）
  - 大小 38,993,920 B
- 版本 1.1.0-0037 同步 8 处；SQLite schema 不变（仍 v7）。

### 19.4 真机验证待办

- NAS 安装 1.1.0-0037 后：设置页把「字幕并发任务数」从 1 改为 2 → 保存 → **不重启套件**，任务明细应同时出现 2 个 `running/translating`；改回 1 后不再新增并行，正在跑的 2 个任务跑完当前任务后不再继续并行。


- 结论：v0036 **PASS（可交付）**；设置项无遗漏，语言码与译文输出组合缺口已修复。

## 20. 附：v0038 步骤时间线 / 操作日志删除 / 菜单调整（2026-11-07）

### 20.1 需求与实现

- **字幕步骤时间线**：任务详情记录排队（queue）、媒体探测（probe）、音频提取+识别（asr）、翻译（translate）、写盘（write）各步骤的开始/结束/耗时（暂停时间不计入）与明细；另记录每个 ASR 分片（抽音频/识别耗时）与每个翻译批次（字幕条数/尝试次数/是否回退原文）。随任务 JSON payload 持久化，旧任务无记录时前端提示；列表接口只带轻量时间字段。
- **操作日志删除**：审计事件新增稳定 id（原始行 sha1 前 16 位，旧记录可直接删除），`POST /api/logs/audit/delete` 支持按 id 选中删除或 `keep_days` 只保留最近 N 天；`POST /api/logs/files/delete` 支持按文件名或保留天数删除运行日志；均为管理员 + CSRF，删除动作本身写入 `audit_delete` / `log_delete` 审计。
- **菜单调整（方案 A）**：顶部导航改为「扫描 / 字幕 / 任务中心」+ 右上角设置；操作日志并入任务中心标签页；存储诊断从任务中心移到设置页「系统 · 存储与运行诊断」；旧 `#/logs` 书签自动跳转。

### 20.2 测试证据

- 后端单测 **444 tests OK（5 skipped）**，新增：`SubtitleTimelineTest`（全步骤/只翻译无 ASR 步骤/失败步骤）、`LogsDeleteApiTest`（选中删除 + audit_delete、keep_days 校验、日志文件删除与路径穿越拒绝、CSRF）。
- 静态检查 **PASS=157 / FAIL=0 / UNVERIFIED=0**：新增 `BE-SUBTITLE-TIMELINE`、`BE-LOGS-DELETE`、`FE-SUBTITLE-TIMELINE`、`FE-LOGS-DELETE`、`FE-NAV-V0038`。
- 端到端 **PASS=152 / FAIL=0 / UNVERIFIED=1**（Windows symlink 限制）：新增 LOGS-04~09（id 返回、选中删除、audit_delete、keep_days 校验、路径穿越、保留最近 N 天不误删）。
- 前端模块 **22/22**、无头浏览器冒烟 **8/8**。
- 额外无头 Chromium UI 探针 **13/13**：导航 3 项 + 右上设置、任务中心 4 个标签页、操作日志勾选/二次确认/删除（audit_delete 可见）、设置页存储诊断加载、旧 `#/logs` 跳转、无控制台错误。

### 20.3 最终产物

- `dist/DuplicateCleaner-1.1.0-0038.spk`
  - SHA256 `17dc49d81cff920f0dbae9f3a72cdb77a2020b0425ba634d05175bfa450ab344`
  - package.tgz MD5 `42e061743fd427e7975897a646636f87`（= INFO.checksum）
  - 大小 38,993,920 B
- 版本 1.1.0-0038 同步 8 处；SQLite schema 不变（仍 v7），无数据迁移。

### 20.4 真机验证待办

- NAS 安装 1.1.0-0038 后：刷新页面确认导航为 扫描/字幕/任务中心 + 右上设置；任务中心 → 操作日志 勾选删除 / 保留最近 N 天；打开任意已完成字幕任务详情查看「步骤耗时」与分片/批次明细；设置页底部「系统 · 存储与运行诊断」可加载；旧 `#/logs` 链接可正常跳转。

## 21. 附：v0039 队列视图与批次总览（2026-11-07）

### 21.1 需求与实现

- **批次进度被新提交覆盖**：字幕页批次进度只维护一个 `page.group`，提交新批次组后旧排队组不可见。新增 `GET /api/subtitles/batch-groups?active_limit=10&completed_limit=3`（与 POST 同路径不同方法）与批次总览：列出进行中（含暂停，按组内最早队列位置 `next_queue_position` 排序）与最近 3 个完成组；点击切换当前组，新提交自动选中新组但旧组保留；无 localStorage 可恢复组时自动选中总览第一组。
- **「最近任务」不是即将执行**：原逻辑取创建时间倒序，而队列 FIFO 执行、重试/继续会追加到队尾。新增 `GET /api/subtitles/queue` 返回 `active/queued/paused/recent`：`queued` 严格按内部 `_queue` 顺序并带 `queue_position`，另有 `queued_total` 等总数与 `single_active_count`。任务中心字幕任务改为 进行中 / 即将执行（前 20 条 + 队列位置 + 总数）/ 已暂停 / 最近完成（按结束时间倒序）；有排队任务时自动轮询（无运行中时 3s）。
- 无 DB schema 变更（仍 v7），无数据迁移；旧后端/旧数据兼容。

### 21.2 测试证据

- 后端单测 **448 tests OK（5 skipped）**：新增队列视图（FIFO 顺序/queue_position/限制与总数/暂停/最近完成排序）、批次总览（按下一队列位置排序/完成组/类型/single_active_count）、API 形状。
- 静态检查 **PASS=160 / FAIL=0 / UNVERIFIED=0**：新增 `BE-QUEUE-VIEW`、`FE-QUEUE-VIEW`、`FE-GROUP-OVERVIEW`；端点清单补 queue / batch-groups 总览。
- 端到端 **PASS=154 / FAIL=0 / UNVERIFIED=1**（Windows symlink）：新增 QUEUE-01、GROUPVIEW-01。
- 前端模块 **22/22**、web smoke **8/8**。
- 无头 Chromium UI 探针 **10/10**（真实 dev 后端 + 本地假 ASR，提交 6 个视频、并发 1）：任务中心可见「进行中（1）」「即将执行（5）」与「队列位置 #1」，API 队列首项与 UI 一致；字幕页批次总览显示「只生成字幕 / 队首位置 #1 / 当前查看」，自动选中并显示批次卡，无控制台错误。

### 21.3 最终产物

- `dist/DuplicateCleaner-1.1.0-0039.spk`
  - SHA256 `fad950c577a81d5b8c17b76647456cf66f67cb6baeddc19a56f2a1a751f5226d`
  - package.tgz MD5 `6c5aecfeb661f1889db5aa334128a901`（= INFO.checksum）
  - 大小 39,004,160 B
- 版本 1.1.0-0039 同步 8 处；SQLite schema 不变（仍 v7），无数据迁移。

### 21.4 真机验证待办

- NAS 安装 1.1.0-0039 后：先提交一个“只生成字幕”批次组 A（排队/执行中），再提交批次组 B：字幕页「批次总览」应同时列出 A/B，A 可切回并暂停/取消；任务中心「字幕任务」的「即将执行」应按真实队列顺序显示 A 队头任务（队列位置 #1、#2…），与后端 `GET /api/subtitles/queue` 一致。

## 22. 附：v0040 分片音频预取（2026-11-07）

### 22.1 需求与实现

- **问题**：单任务内是「抽第 N 片音频（FFmpeg）→ 上传 → 识别 → 下一片」严格串行，FFmpeg 与云端 ASR 互等；`subtitle_concurrency` 只解决**任务之间**并行，所以在 NAS 上常见「并发设了 2、`top` 里 ffmpeg 却只有 1 个」——两个任务的抽取窗口短暂且互相错开，并不是并发没生效。
- **实现**：新增 `subtitles._AudioPrefetch`。每个任务 1 个预取线程、**只预读 1 片**：主线程上传/识别第 N 片时后台线程抽第 N+1 片，`take(index)` 取用时已就绪则零等待；`_extract_chunk_audio/_extract_chunk_audio_nas/_extract_chunk_remux` 新增 `stop_check` 参数，取消或任务收尾时能让在跑的 FFmpeg 尽快退出。
- **语义保持**：分片顺序、时间轴偏移、字幕内容、写回文件与串行实现一致；暂停仍在**分片边界**生效（丢弃预取结果）；抽音频异常原样抛回主线程（错误码不变）；缓存分片不重复抽取；未消费的临时音频在 `close()` 删除、任务临时目录整体 `rmtree`；最多多占 1 片临时空间。
- **新设置** `subtitle_audio_prefetch`（布尔，默认 `true`）：设置页「云端字幕」新增「分片音频预取（默认开启）」，保存后对**新开始执行**的任务生效；关闭即回到严格串行。
- 无 DB schema 变更（仍 v7），无数据迁移。

### 22.2 测试证据

- 后端单测 **452 tests OK（5 skipped）**：新增 `SubtitleAudioPrefetchTest` 4 例——预取时第 2/3 片抽取与上一片 ASR 时间窗重叠、关闭预取后严格串行无重叠、预取抽取异常仍返回 `NO_AUDIO`、运行中取消成功且临时目录无残留音频；设置默认值/布尔校验/非法值用例补入 `SubtitleSettingsTest`。
- 静态检查 **PASS=162 / FAIL=0 / UNVERIFIED=0**：新增 `BE-SUBTITLE-AUDIO-PREFETCH`、`FE-SUBTITLE-AUDIO-PREFETCH`。
- 端到端 **PASS=157 / FAIL=0 / UNVERIFIED=1**（Windows symlink）：新增 SUB-33/34/35（保存关闭 / 回读 / 非法值 400）。
- 前端模块 **22/22**、web smoke **8/8**。

### 22.3 最终产物

- `dist/DuplicateCleaner-1.1.0-0040.spk`
  - SHA256 `6611ee6513f1179efb3b94d2b733a3dff331fb8f7058c6cd9a69621289837fd2`
  - package.tgz MD5 `4b02f6d0267734c2f41b46d2aa3b7cd2`（= INFO.checksum）
  - 大小 39,014,400 B
- 版本 1.1.0-0040 同步 8 处；SQLite schema 不变（仍 v7），无数据迁移。

### 22.4 真机验证待办

- 安装 1.1.0-0040 后跑一个 ≥2 片的字幕任务：任务详情 →「步骤耗时」→ 展开「ASR 分片明细」，第 2/3 片的「抽音频」开始时间应早于上一片「识别」结束（两段时间窗重叠）；同时在 NAS 上 `ps -eo args | grep -c '[f]fmpeg'` 连续采样，出现 2 个 ffmpeg 的比例应比升级前明显上升。
- 关闭「分片音频预取」再跑一个同规格任务：抽音频与识别应恢复严格串行（无重叠），用于对比提速幅度。

## 23. 附：v0041 任务行进度信息与预计剩余（2026-11-08）

### 23.1 现场问题

- 用户反馈：任务中心「字幕任务 → 进行中」的任务行进度条**一直是空条**（只有右侧 53% 文字）；同一行「分块 7/9」与 53% 对不上；行首「云端语音识别」与「云端 ASR 生成」两个标签都带“云端…”，来源被误读成当前阶段。

### 23.2 定位

- **进度条空条（前端 bug）**：截图像素采样确认条内只有轨道色 `#E8EEF7`、无蓝色填充。根因是 `js/dom.js` 的 `el(tag, opts, children)` 只支持 `className/text/attrs/dataset/on`，**不支持 `style`**；任务行（app.js `renderSubtitleJobItem`）与任务详情弹窗（`buildSubtitleJobDetailBody`）却用 `el('div', {style: 'width:..%'})` 传宽度，`style` 被静默丢弃后回落到 `.progress-fill{width:0}`。其余 5 处进度条（扫描、单视频字幕弹窗、删除、字幕组、批次组）都走 `node.style.width`，因此只有这两处受影响。
- **分块数字多一片（前端 off-by-one）**：后端 `chunk_index = index + 1` 已是 1-based 的已完成分片数（`_run_chunked_asr` 收尾 `progress = 20 + int(50*(index+1)/total)`），前端 `subtitleJobChunkText` 又对 `chunk_index` +1 → 9 片完成 6 片时显示 7/9，而百分比 53% 正确。
- **来源与阶段撞词（UX）**：`来源` 取值「云端 ASR 生成」、ASR 阶段名「云端语音识别」，两个标签相邻显示，容易被读成一句状态。

### 23.3 实现

- 前端新增 `subtitleProgressBar(pct)`：创建 `progress-bar` 节点后设置 `fill.style.width` 并同步 `aria-valuenow`；任务行与任务详情弹窗统一使用它。
- `subtitleJobChunkText` 修复为：`chunk_index`/`chunks_done` 直接使用（1-based），只有 0-based 的 `current_chunk`/`chunk` 才 +1。
- 任务行 meta 顺序调整为 `阶段 · 分块 x/y · 当前步骤消息（message）· 已耗时 · 预计剩余 · 译文 · 创建`；`message` 与分块数字重复时只保留 `message`（`subtitleJobDedupeChunkText`）。
- 「来源」移到行尾并弱化为小字（`.subtitle-job-source`，11px / opacity .72），文案改为 `来源：云端 ASR 生成`。
- 后端 `SubtitleJob.eta_seconds()`：仅 `running`/`translating` 有值；ASR 阶段 = 已完成分片 `extract_sec + asr_sec` 均值 × `chunk_total - chunk_index`；翻译阶段 = 已完成批次 `duration_sec` 均值 × 由进度带（70..90）反推的剩余批次数；其它情况 `None`。只读内存、不 stat 文件、不持久化，随 `snapshot()` 暴露为 `eta_seconds`（列表/队列/详情都能拿到）。
- 任务详情网格新增「耗时」「预计剩余」两行（有值才显示）。
- 无 DB schema 变更（仍 v7），无数据迁移；无新增接口/错误码。

### 23.4 测试证据

- 后端单测 **456 tests OK（6 skipped）**：新增 `SubtitleEtaTest` 4 例——ASR 分片均值估算 + `snapshot` 暴露、无样本/单分片/已完成时返回 `None`、翻译批次均值与进度带边界（70/90 不猜）、非执行中状态一律 `None`。
- 静态检查 **PASS=167 / FAIL=0 / UNVERIFIED=0**：新增 `BE-SUBTITLE-ETA`、`FE-SUBTITLE-PROGRESS-BAR`（并断言 `src/web` 内不再出现 `el(..., {style:...})` 写法）、`FE-SUBTITLE-CHUNK-LABEL`、`FE-SUBTITLE-SOURCE-TAIL`、`FE-SUBTITLE-ETA`。
- 端到端 **PASS=157 / FAIL=0 / UNVERIFIED=1**（Windows symlink 历史项，无新增用例）。
- 前端模块 **22/22**、web smoke **8/8**。

### 23.5 最终产物

- `dist/DuplicateCleaner-1.1.0-0041.spk`
  - SHA256 `7345c642072ba3dcb21860a7ffb6e65bfd6c3a92b8f54a904c4b1c9e6b4e41b9`
  - package.tgz MD5 `367914ac22c877efdca55e83425bb4c5`（= INFO.checksum）
  - 大小 39,014,400 B
- 版本 1.1.0-0041 同步 8 处；SQLite schema 不变（仍 v7），无数据迁移。

### 23.6 真机验证待办

- 安装 1.1.0-0041 后打开任务中心「字幕任务 → 进行中」：进度条应按百分比填充，且与右侧百分比一致（不再是空条）。
- 分块数与百分比应一致：9 片任务完成 6 片时显示「分块 6/9」+ 53%（旧版显示 7/9）。
- 运行中任务行应出现「已耗时 10 分 49 秒 · 预计剩余 X」，`来源：云端 ASR 生成` 在行尾以灰色小字显示。
- 任务详情弹窗进度条同样应为填充状态，网格里出现「耗时」「预计剩余」两行。

## 24. 附：v0042 升级后「无法启动 → 点修复」根治（2026-11-08）

### 24.1 现场现象（用户截图 + NAS 实测）

- 套件中心「需要注意的事项」：*无法启动“重复文件清理与字幕”。请稍后再试一次，或联系套件开发人员。*，状态「停用」，需点【修复】才能恢复。
- NAS（DSM 7.4.1 / x86_64 / Python 3.8.15）实测：
  - `synopkg status` → `start_failed`（status_code 272，"failed to start on previous startup"）。
  - `/var/log/packages/DuplicateCleaner.log`：`Begin start-stop-status start` → `failed to become healthy` → `End start-stop-status start ret=[1]`（14:08:02→14:08:23，约 21s）。
  - 数据规模：字幕任务 **7540** 个（JSON 文件 7540 个）、排队 **4672** 个；`subtitles/jobs` 解析实测 **20.9s**。
  - `build_services` 实测 **27.0s**（DB 读取仅 0.5s，其余为 JSON 解析 + 逐任务分片查询 + 队列恢复）。
  - `start-stop-status` 健康检查窗口 ≈ **18–21s**（30 次 × 0.5s + 每次探测子进程开销）→ 永远等不到健康 → 返回 1 → DSM 判定升级失败。

### 24.2 根因

1. `_load_jobs` 即使 DB 里已有全部任务记录，仍会 `json.load` **每一个** 任务 JSON（解析发生在“是否已在 records”判断之前）→ 7500 个文件约 21s。
2. `start-stop-status` 健康检查只有 15s，慢盘/大库（数千任务）的首次启动必然超时。
3. 升级时 `postinst` 删除 `__pycache__`，且字节码缓存写在套件目录，首次启动还要全量编译。

### 24.3 修复（v0042）

- `_load_jobs`：文件名即 job_id（`_job_json`），**DB 已命中时跳过文件读取/解析**；只有 DB 缺失的任务才读 JSON 并回写 DB（兜底语义不变）。终态任务（done/failed/cancelled）启动时不再查询 ASR 分片缓存。
- `start-stop-status`：健康检查 **15s → 90s**；失败清理残留进程/端口后**重试一次**；进程存活但未通过检查时返回成功（避免把升级判成失败）；`PYTHONPYCACHEPREFIX=<var>/pycache`；全过程写 `<var>/log/start.log`。
- `postupgrade`：root 身份下补 `chown -R duplicatecleaner:duplicatecleaner <var>`，并创建 `pycache` 目录。

### 24.4 验证

- 本地：unittest **459 OK（6 skipped）**；static **PASS=169 / FAIL=0**（新增 `BE-SUBTITLE-LOAD-FAST`、`SPK-HEALTH-TIMEOUT`）；e2e **157 PASS / 0 FAIL / 1 UNVERIFIED**；web modules **22/22**；web smoke **8/8**。
- 新增单测 `SubtitleLoadJobsTest` 3 例：DB 命中不再打开 JSON、仅 JSON 任务仍恢复并回写 DB、终态任务不查分片缓存。
- **NAS 真机**（直接 `synopkg install` 覆盖升级到 0042）：
  - `synopkg install` 返回 `success=true`，脚本 preupgrade/preinst/postinst/postupgrade/**start 全部 code 0**，`status=running`，安装总耗时 82s。
  - `start.log`：首次 `started in 47s (attempt 1)`（冷缓存首启）；`synopkg restart` 第二次 **`started in 3s`**。
  - `build_services` 由 **27.0s → 2.5s**。
  - 真机 UI 探针（Playwright 登录 NAS 套件）：任务行进度条 fill/bar 比例 = `aria-valuenow`（2% 场景实测一致）、meta 顺序「提取音频 · 分块 0/9 · 正在提取音频 · 已耗时 59 秒 · 创建 … · 来源：云端 ASR 生成」（来源在行尾灰色小字）、无页面异常（仅登录前 `/api/auth/me` 401，属正常引导）。

### 24.5 产物

- `dist/DuplicateCleaner-1.1.0-0042.spk`：39,014,400 B
  - SHA256 `f769f08e74868a8037220003cff1c153aea09940ac4518715c09e349b50866a1`
  - package.tgz MD5 `9e18308eb7731ac2fa90c6ba51f0645a`
- 版本 1.1.0-0042 同步 8 处；SQLite schema 不变（仍 v7），无数据迁移。

### 24.6 后续观察

- 下次升级安装后确认：套件中心不再出现「无法启动」，`/var/packages/DuplicateCleaner/var/log/start.log` 有 `started in Ns` 记录。
- 若某次首启仍 >90s（例如数据量继续增长），`start.log` 会记录失败前后端日志尾部；届时可再评估把任务 JSON 目录或分片查询进一步批量化。

## 25. 附：v0043 增量扫描 / 保留规则链 / 排除规则（2026-11-08）

### 25.1 需求与评估结论

用户给出 v2 需求文档（扫描 5 优化 + 3 新增、字幕 3 优化 + 视频监控、全局 4 项），要求先判断“有没有做不到或者不合适的内容”。
逐条评估写在 [docs/REQUIREMENTS_V2_ASSESSMENT.md](../docs/REQUIREMENTS_V2_ASSESSMENT.md)：

- **做不到 / 必须改法**：权限“一键配置”（非 root 无法改 ACL → 体检+指引）、
  增量扫描“仅对比新增/修改文件”（会漏检跨时间重复 → 哈希复用 + 新增标记）、
  相似视频“不同剪辑版本”（不可承诺 → 分档置信度+人工复核）、
  缩略图/片段播放的浏览器编解码限制、硬链接合并不可逆、实时监控（标准库无 inotify → 轮询）。
- 本版（v0043）实现扫描模块的三条：**增量扫描（哈希缓存）**、**保留策略规则链**、**排除规则增强**。

### 25.2 实现

- **跨扫描哈希缓存**（schema v8 `hash_cache`）：键 `(dev, inode)`，`size`/`mtime_ns`/`algo`/采样配置校验；
  命中即复用 `quick_hash`/`full_hash`，不读文件内容。只缓存候选文件；上限 30 万条 + LRU 裁剪。
  Windows 64 位无符号 `st_ino` 会超出 SQLite 有符号范围 → 统一按补码映射（`store.sqlite_int`）。
- **增量标记**：文件级 `new_since_last_scan` / `changed_since_last_scan` / `hash_cached`；
  组级 `new_files` / `changed_files`/`scan_groups` 索引列；`groups?new_only=1` 只看增量组。
- **快速指纹 v2**：大文件（≥256 MiB）在头/尾之外加采样中间段；`QUICK_SAMPLE_VERSION` 参与缓存校验。
- **保留规则链**（`keep_rules`）：创建/修改/访问时间、体积、路径优先级、文件名优先级（通配符或 `/正则/`）、
  带内嵌字幕、内嵌音轨多者优先；某条分出唯一胜者即停止，平局继续，仍平局转人工。
  后两条规则需要 FFmpeg 探测，结果进 `media_probe_cache`。`keep_rules=[]` 时历史语义逐字不变。
- **排除规则**：文件名正则/通配符（保存时校验）、`.ignore`（gitignore 常用子集、base_dir 作用域）、
  影视附属文件只跳过“与同目录视频同源”的字幕/海报/nfo。
- **缓存管理**：`GET /api/cache`（状态）、`POST /api/cache/clear`（`confirm=true`，只清缓存）；
  设置页存储诊断显示条目并提供清理入口。
- **前端**：扫描选项/设置页的保留规则与排除规则表单、结果页“只看本次新增/变化”与组徽标、
  摘要“增量缓存复用”“排除规则/附属文件”卡片。

### 25.3 验证

- 新增 `tests/test_v0043_incremental.py` **25 例**：缓存读写/大 inode/裁剪/清空、第二次扫描 `bytes_hashed=0`、
  新增与变化标记、`incremental_scan=false`、`new_only` 过滤、规则链各规则与平局、
  正则/通配符/`.ignore`/附属文件、设置校验、`/api/cache` 接口。
- 既有 `ScanFilterTest.test_pause_resume_stop` 调整为显式 `incremental_scan=false`
  （否则第二次扫描因缓存瞬间完成，无法测暂停/停止——这本身反证缓存生效）。
- e2e 新增 `INC-01..04`、`KEEP-01`、`CACHE-01..04`；`PAUSE` 场景显式关闭缓存以保留运行窗口。
- 静态检查新增 `BE-INC-HASH-CACHE`、`BE-INC-SAMPLE-V2`、`BE-KEEP-RULES`、`BE-EXCLUDE-RULES`、
  `BE-CACHE-API`、`FE-KEEP-RULES`、`FE-EXCLUDE-UI`、`FE-INCREMENTAL-UI`、`FE-CACHE-UI`。
- 结果（本地）：unittest **525 OK（5 skipped）**；static **PASS=178 / FAIL=0 / UNVERIFIED=0**；
  e2e **166 PASS / 0 FAIL / 1 UNVERIFIED**（Windows symlink 历史项）；web modules 22/22；smoke 8/8。
- **真实浏览器探针**（Playwright + dev 后端，`.uiprobe/probe_v0043.mjs`）**10/10 PASS**：
  登录/dev meta 暴露规则、`PUT /api/settings` 保存、扫描选项控件齐全且按设置回填、
  设置页把 `keep_rules` 反解回控件、页面保存后规则完整保留、诊断含增量缓存字段、
  「清理增量缓存」按钮存在、无 console 错误；截图见 `.uiprobe/v0043-scan.png` / `v0043-settings.png`。
- Python 3.8 兼容性：对 `dupcleaner/` 与 `tests/` 共 52 个 py 文件做 `ast.parse(feature_version=(3,8))`，无语法错误。

### 25.4 产物

- `dist/DuplicateCleaner-1.1.0-0043.spk`：**39,034,880 B**
  - SHA256 `4984be135d2ef9b3590ee3d37f9b5bdcbf09e594bc7727e40bccbd0064f32437`
  - package.tgz MD5 `91e9d6d00c90cc1fae2bc90bedb3ce53`
- 版本 1.1.0-0043 同步 8 处；SQLite schema **v8**（`hash_cache`、`media_probe_cache`、
  `scan_groups.new_files/changed_files`），只建表/加列，不重写历史数据。

### 25.5 真机待验证

- 真实媒体库第二次扫描的耗时与 `bytes_hashed` 对比；`.ignore`/附属文件后结果是否更干净；
  规则链是否按预期选保留项；缓存清理后是否重新读盘。

## 26. 附：v0044 预览增强与删除白名单（2026-11-08）

### 26.1 需求与方案

- 需求（v2 文档 扫描 优化 4/5）：结果页提供**视频缩略图预览、片段播放**，避免误删不同压制版本/花絮/剪辑版本；
  **安全删除增强**中要求“白名单保护外挂字幕 / nfo / 海报等附属文件”。
- 评估结论（[REQUIREMENTS_V2_ASSESSMENT.md](../docs/REQUIREMENTS_V2_ASSESSMENT.md) §2.4/2.5）：
  缩略图/片段播放**可行但有编码限制**；附属文件保护**可行**，且应做成“删除侧终局防线”。

### 26.2 实现

- **预览不再整段下载**：原实现 `fetch → blob` 后播放，大视频要先下载完整文件；现在先 `Range: bytes=0-0`
  探测类型，视频/音频直接用 URL 作为 `<video>/<audio>` 源（Range 流式、可立即拖动/片段定位）。
- **缩略图**（`thumbnails.py`）：FFmpeg 抽 1 帧 → JPEG（默认宽 320），缓存
  `<var>/cache/thumbs/<sha1(path|size|mtime|t|w)>.jpg`；上限 3000 条 / 300MB，LRU；并发 2；
  `GET /api/preview/thumb`（`X-Thumb-Cache: hit|miss`）。
- **媒体信息**：解析 `ffmpeg -i` Stream 行得到时长/分辨率/编码/音轨/字幕轨（含语言），内存缓存 200 条；
  `GET /api/preview/media`；时间点清单 `GET /api/preview/thumbs`（只给 t 与 URL，`<img>` 按需加载）。
- **前端**：视频预览弹窗显示媒体信息 + 6 张可点击缩略图（跳到对应时间点播放）；同组 ≥2 文件时提供
  「并排对比」弹窗（左右播放器 + 同组文件下拉 + 同步播放开关 + 对齐时间点）。
- **删除白名单**（`protection.py`）：目录（绝对路径前缀）/ 后缀（默认字幕+元数据，不含图片）/
  文件名正则 / 视频附属文件（同目录同源视频的字幕/海报/nfo；视频本身与“无视频目录里的图片”不受影响）。
  **三层生效**：扫描期标记（UI 不可勾选、不计入可选数量）→ 计划期按当前设置重算
  （`skipped[].reason=PROTECTED` + `protect_source`，计划响应 `protected_skipped`）→ 执行期再算一次。
- 设置页新增白名单表单（启用/后缀/目录/正则/附属文件）；结果行显示「受保护 · 原因」徽标并禁用勾选，
  组卡片显示「受保护 N」，摘要新增受保护统计；诊断页显示缩略图缓存条目/大小。

### 26.3 验证

- unittest：**549 OK（5 skipped）**（新增 `test_v0044_preview.py` 12 例、`test_v0044_protection.py` 24 例）。
- static：**PASS=182 / FAIL=0 / UNVERIFIED=0**（新增 `BE-PREVIEW-THUMB`、`BE-PROTECT`、
  `FE-PREVIEW-THUMB`、`FE-PROTECT-UI`，路由/前端端点覆盖补齐 3 个预览接口）。
- e2e：**174 PASS / 0 FAIL / 1 UNVERIFIED**（新增 `PRT-01..04`、`THUMB-01..04`；Windows symlink 历史项）。
- 真实 Chromium 探针（`.uiprobe/probe_v0044.mjs` + `harness_v0044.py`）**8/8 PASS**：
  白名单设置回填/保存、`/api/meta` 能力、浏览器会话下缩略图/媒体/时间点接口、
  扫描结果 `protected` 标记、无 console 错误；截图 `.uiprobe/v0044-settings.png`。
- 媒体流解析单测覆盖 `1920x1080`、`jpn/chi/eng` 语言、5.1 声道、default 标记。

### 26.4 踩坑

- 探针的临时共享目录不能放在隐藏目录（`.uiprobe`）下：后端路径安全会拒绝隐藏路径组件
  （`PATH_NOT_ALLOWED: 不能访问隐藏目录/文件: .uiprobe`）→ 改用系统临时目录。
- 附属文件判定必须先排除“视频文件自身”，否则 `.mkv` 会把自己识别成自己的附属文件；
  已加单测 `test_scan_marks_protected_files` 防止回归。
- 删除计划里 keeper 先被 `IS_KEEPER` 跳过，所以“受保护数”只统计非 keeper 候选；
  e2e 断言按此语义修正。
- 计划中全部文件都因保护被跳过时返回 `PLAN_INVALID`（details 含 PROTECTED），执行后任务状态为
  `partial`（既有语义：skipped>0 即 partial）。

### 26.5 产物

- `dist/DuplicateCleaner-1.1.0-0044.spk`：**39,055,360 B**
  - SHA256 `97a69074a1a5ff8da370141e425dcb5749c6c62359668cabb6a9e6efab38b35f`
  - package.tgz MD5 `2236c1f6e0689f1b0dcd4783cc252cfa`
- 版本 1.1.0-0044 同步 8 处；SQLite schema 仍 **v8**（本版无 DB 迁移；缩略图缓存是文件缓存）。

### 26.6 真机待验证

- 4K/MKV 首次缩略图耗时与失败率（HEVC 软解一帧）；DSM 内置浏览器里并排对比/同步播放可用性；
  白名单对真实媒体库（srt/海报/nfo）的保护效果；受保护目录对照片库判重的影响。

## 27. 附：v0045 硬链接合并与套件独立回收站（2026-11-08）

### 27.1 需求与方案

- 需求（v2 文档 扫描 优化 5）：“支持硬链接替代删除，适配 Btrfs；套件删除文件独立进入回收站，
  支持整组重复文件一键恢复”。
- 评估结论（[REQUIREMENTS_V2_ASSESSMENT.md](../docs/REQUIREMENTS_V2_ASSESSMENT.md) §2.5、§3.4）：
  硬链接**可行但不可逆**（第二份 inode 无法恢复）→ 做成独立操作、默认关闭、逐组确认、提供
  “恢复为独立副本”；独立回收站可行，但**不能直接把文件塞进 DSM 的 `#recycle`**（DSM UI 不认），
  改为共享根下 `#duplicate-cleaner-trash/` + manifest 支撑整组恢复。

### 27.2 实现

- **硬链接合并**（`merge.py`）：只处理精准重复组的非保留项；计划阶段校验扫描记录/同文件系统/受保护/
  已链接，执行阶段再跑 `revalidate(hash_check=True)` 后 `link(master, tmp)` + `rename(tmp, candidate)`
  原子替换；`confirm="MERGE"`、计划 5 分钟过期一次性；manifest `<var>/merges/*.json`；
  “恢复为独立副本”优先 `FICLONE`（Btrfs），回退普通复制并保持 mtime。
- **套件独立回收站**（`trash.py`）：`delete_recycle_mode=package` 时删除移动到
  `<share>/#duplicate-cleaner-trash/<时间戳>/<相对路径>`（同卷 rename、不复制）；每个删除任务写
  `<var>/trash/<job_id>.json`；任务中心「回收站与合并」支持整组/部分恢复（原位置已存在则跳过、
  绝不覆盖）与“永久删除”清理（需短语，只删清单记录文件）。
- 设置：`merge_enabled`（默认 false）、`delete_recycle_mode`（默认 share）；`/api/meta` 暴露能力与标签。
- 前端：结果页精准组「合并为硬链接」按钮（两次确认 + 跳过原因）；设置页合并开关与删除去向；
  删除引导按 `recycle_mode` 显示去向；任务中心新增「回收站与合并」标签页。

### 27.3 验证

- unittest：**566 OK（5 skipped）**（新增 `test_v0045_merge.py` 10 例、`test_v0045_trash.py` 7 例）。
- static：**PASS=186 / FAIL=0 / UNVERIFIED=0**（新增 `BE-MERGE`、`BE-TRASH`、`FE-MERGE-UI`、`FE-TRASH-UI`
  与合并/回收站路由）。
- e2e：**182 PASS / 0 FAIL / 1 UNVERIFIED**（新增 `MERGE-01..04`、`TRASH-00..03`；Windows symlink 历史项）。
- 真实 Chromium 探针（`.uiprobe/probe_v0045.mjs` + `harness_v0045.py`）**13/13 PASS**：
  设置回填/保存、扫描、合并计划与执行（同 inode/nlink）、恢复独立副本、套件回收站计划/批次/恢复、
  任务中心「回收站与合并」渲染、无 console 错误；截图 `.uiprobe/v0045-trash.png`。

### 27.4 踩坑

- 合并清单必须**先落盘再对外暴露终态**：否则 e2e 轮询到 completed 立刻调恢复会 404（清单还没写）。
- 合并后扫描器按 inode 去重，重复组会收缩；恢复后需要重新扫描才反映新的分组（结果页提示刷新）。
- 恢复/合并测试不能假设“哪个是候选”：keeper 由创建时间决定，恢复后复制出来的文件 btime 会变，
  下次扫描 keeper 可能翻转 —— 测试要按“两路径 inode 是否不同”判断，而不是固定文件名。
- 反复执行探针时设置会持久化（merge_enabled=true），断言默认值不能写死“当前必须 false”。

### 27.5 产物

- `dist/DuplicateCleaner-1.1.0-0045.spk`：**39,075,840 B**
  - SHA256 `c54580277300086dbce5bed8f4d7df40b1328701a7043e8731c4ab99f649f822`
  - package.tgz MD5 `988ddda60b97b7f08994436e5dbc83ef`
- 版本 1.1.0-0045 同步 8 处；SQLite schema 仍 **v8**（本版无 DB 迁移；合并/回收站清单是 `<var>/` JSON）。

### 27.6 真机待验证（Btrfs）

- 合并后 `df` 释放空间与 `stat` nlink；跨卷（volume1↔volume2）合并是否被正确跳过；
- `FICLONE` 恢复是否秒级、非 Btrfs 文件系统回退复制是否正常；
- 套件回收站在 File Station 的可见性、整组恢复后文件/权限/mtime 是否正常；
- 合并组在下次扫描中是否正确收缩（不应再算“可释放空间”）。

## 28. 附：v0046 计划扫描与通知系统（2026-11-08）

### 28.1 需求与方案

- 需求（v2 文档 新增功能 1 + 全局 3）：“支持定时后台执行重复扫描，可安排低负载时段运行；
  扫描完成后推送通知，支持 Webhook、Telegram、DSM 系统通知”；“扫描完成、字幕批次执行结束、
  大量任务失败、释放空间达到阈值时推送 DSM 通知与 Webhook”。
- 方案：`SchedulerService`（规则解析 + 20s tick + 负载/忙碌跳过 + 状态持久化）+
  `NotificationService`（渠道/事件订阅 + 后台队列 + 四类触发点 + 逐渠道测试）。
  都是标准库实现（`urllib` / `subprocess` / `threading`），不引入第三方依赖。

### 28.2 实现

- **计划扫描**（新 `scheduler.py`）：
  - 规则 `daily HH:MM` / `weekly mon,wed,fri HH:MM` / `interval MIN`（5..10080 分钟），
    保存设置时用同一解析器校验，非法返回 400；
  - tick 到点后依次检查“已有扫描在跑 / 1 分钟 loadavg 超过阈值 / 无可用 roots”，
    任一不满足即跳过并审计 `scheduled_scan_skipped`（不补跑）；roots 为空时复用最近一条
    completed 扫描的 `config.roots`；
  - 每条规则 `last_run` 持久化 `<var>/scheduler_state.json`，重启不重复触发；
    `GET /api/scheduler` 显示下次执行/上次执行/当前负载，`POST /api/scheduler/run-now` 手动执行。
- **通知系统**（新 `notifications.py`）：
  - 渠道：DSM `synodsmnotify`（非 DSM 自动跳过）/ Webhook（POST JSON）/ Telegram Bot API；
  - 事件：`scan_complete`、`scan_failed`、`subtitle_batch_done`、`failure_burst`、`freed_threshold`；
  - 触发点：扫描收尾（`ScanManager._audit_finish`）、删除任务结束（释放空间/失败阈值）、
    `SchedulerService` 每 60s 轮询字幕批次组并按 group_id 去重；
  - 发送走后台单线程队列（200 条），失败只记审计不影响主流程；Telegram token 与 Webhook
    完整 URL 不进入日志/审计/响应；`GET /api/notifications`、`POST /api/notifications/test`。
- 设置：新增 13 个键（通知开关/渠道/事件/URL/token/chat_id/失败阈值/释放空间阈值/超时 +
  计划开关/规则/目录/负载阈值），Telegram token 纳入 `SECRET_SETTING_KEYS`。
- 前端：设置页新增「自动化 · 计划扫描与通知」分组（规则/目录/负载 + 渠道/事件/阈值 +
  逐渠道测试按钮 + 状态文案），保存后与打开设置页时刷新 `/api/scheduler`、`/api/notifications`。

### 28.3 验证

- unittest：**601 OK（5 skipped）**（新增 `test_v0046_notify.py` 22 例、`test_v0046_scheduler.py` 13 例）。
- static：**189 PASS / 0 FAIL / 0 UNVERIFIED**（新增 `BE-NOTIFY`、`BE-SCHEDULER`、`FE-AUTOMATION-UI`
  + 通知/调度路由与端点覆盖）。
- e2e：**191 PASS / 0 FAIL / 1 UNVERIFIED**（新增 `NOTIFY-00..03`（本地 Webhook 接收器真实收到
  `scan_complete` 与测试消息）、`SCHED-00..04`（设置/状态/立即执行/非法规则/root 复用）；
  Windows symlink 为历史 UNVERIFIED）。
- 真实 Chromium 探针（`.uiprobe/probe_v0046.mjs` + `harness_v0046.py`）**8/8 PASS**：
  字段齐全、状态文案、保存持久化、调度状态、测试 Webhook 真实发送并显示成功、无 console 错误；
  截图 `.uiprobe/v0046-settings.png`。

### 28.4 踩坑

- e2e 的 `Client.post_json` 必须带 body，`POST /api/scheduler/run-now` 传 `{}` 才能跑；
  第一次漏传导致 e2e 中途异常（HARNESS 项失败）。
- 设置页 `<details>` 默认折叠，探针点击「测试 Webhook」前必须先 `group.open = true`，
  否则 Playwright 报 element not visible。
- 探针会持久化设置：重复跑时状态文案从“计划扫描未启用”变为“下次：…”，断言要兼容两种形态。
- 通知 watcher 依赖字幕批次组在内存中；套件重启后已结束的旧组不会补发（避免重启即刷屏）。

### 28.5 产物

- `dist/DuplicateCleaner-1.1.0-0046.spk`：**39,086,080 B**
  - SHA256 `26893c3f12976037fb4423cb0e9706b8f8272d3f4352e9fc22f009306f4a1ce3`
  - package.tgz MD5 `66811a2f56721f4e6e3f62c6a142f989`
- 版本 1.1.0-0046 同步 8 处；SQLite schema 仍 **v8**（调度/通知去重状态为 `<var>/` JSON）。

### 28.6 真机待验证（DSM）

- `synodsmnotify` 实际弹窗（套件用户权限、`@administrators` 目标、中文标题/正文截断）；
- Webhook/Telegram 出网与超时（NAS 防火墙/代理）、Telegram chat_id 负数群组；
- 负载阈值在 4 核 NAS 的合理取值（当前字幕任务并发 4 时 load ~9），避免计划扫描被永久跳过；
- 计划扫描与字幕任务并发时的磁盘/CPU 占用；套件停机期间错过时间点的补跑策略。

## 29. 附：v0047 相似视频识别增强（2026-11-08）

### 29.1 需求与方案

- 需求（v2 文档 新增功能 2）：“强化视频帧相似比对能力，识别源片一致、压制版本不同的视频文件”。
- 评估结论（[REQUIREMENTS_V2_ASSESSMENT.md](../docs/REQUIREMENTS_V2_ASSESSMENT.md) §2.4/§3.3）：
  “不同剪辑版本”不可承诺；可实现的是对**同源不同压制**更稳（黑边/分辨率/编码/片头剪切），
  并给出**可解释的置信度分档**，把“可能相似”交人工复核。
- 方案：帧指纹 v2（全帧 + 内容裁剪）、多组合 DTW + ±3 帧时间轴偏移对齐、可选音频包络签名、
  置信度分档（same_source/likely_same/possible）。

### 29.2 实现

- **边缘裁剪指纹**（`gray_content_crop`）：按行/列亮度均值检测内容 bbox 裁掉黑边后放大回 32×32
  再算 pHash；纯黑/内容过小时回退中心 80% 裁剪。每帧同时保留全帧与裁剪两个指纹
  （`frame_hashes` / `frame_hashes_center`），指纹版本 v2，`VIDEO_ALGO_VERSION=video-v2` 使旧缓存失效。
- **时间轴偏移对齐**（`offset_aligned_stats`）：±3 帧、重叠 ≥0.8，按 `0.7*avg+0.3*overlap` 打分；
  与全帧/裁剪/交叉 DTW 一起取最优（`pair_score_enhanced`）。
- **音频包络签名**（可选、默认关）：ffmpeg 解 180s 单声道 8kHz PCM → 120 桶 RMS → 0..255 签名；
  相似度=零均值归一化互相关；静音/采样不足不参与加权；失败 open；缓存独立 key。
- **置信度分档**（`confidence_band`）：组内取最低配对分数（保守）→ ≥95 same_source、≥85 likely_same、
  其余 possible；结果带 `video_confidence_label`、`video_max/min/avg_score`、`video_match_method`、
  `video_center_used`、`video_audio_used/score`、`video_duration_delta_sec`。
- 设置新增 4 键：`video_center_crop_enabled`、`video_offset_align_enabled`、
  `video_audio_signature_enabled`、`video_audio_weight`（0–0.5，与 ASR 权重合计 ≤0.5）。
- 前端：视频设置新增开关/权重（音频权重随开关置灰）；结果页视频组新增置信度（蓝/绿/黄）、
  `边缘裁剪`、`时间轴对齐`、`音频辅助 · N%` 徽标与“组内时长差”。

### 29.3 验证

- unittest：**626 OK（5 skipped）**（新增 `test_v0047_video.py` 25 例：内容裁剪去黑边、偏移对齐、
  增强配对、置信度分档、音频签名生成/相似度/静音、分组音频加权、引擎级签名缓存与失败 open、
  设置校验、扫描结果字段）。
- static：**191 PASS / 0 FAIL / 0 UNVERIFIED**（新增 `BE-VIDEO-V2`、`FE-VIDEO-V2`）。
- e2e：**193 PASS / 0 FAIL / 1 UNVERIFIED**（新增 `VIDEO-01`：真实 ffmpeg 生成“同源重编码 +
  带黑边”样例，验证组字段与命中；`VIDEO-02`：音频权重越界 400；Windows symlink 为历史 UNVERIFIED）。
- 真实 Chromium 探针（`.uiprobe/probe_v0047.mjs` + `harness_v0047.py`）**7/7 PASS**：
  真实 ffmpeg 扫描、组字段、设置字段/置灰、结果页徽标渲染、无 console 错误；
  截图 `.uiprobe/v0047-settings.png`。
- 实测样例（64×64 testsrc 3s，抽样 5 帧）：同源重编码 96–97 分；带黑边版本全帧仅 66 分，
  边缘裁剪后 93–94 分；音频签名 100 分（同源正弦）→ 分组置信度 `likely_same`。

### 29.4 踩坑

- **不能用 `_run` 把 PCM 写 stdout**：`_run` 是“轮询后 communicate”，Windows 匿名管道缓冲区小，
  ffmpeg 写满即被阻塞 → 每次都超时。改为写临时文件再读入（180s@8kHz 约 2.8MB）。
- 合成“同源不同压制”e2e 样例时，若两次编码参数相同会得到**字节一致**文件 → 被精准组先吃掉，
  视频候选只剩 1 个 → 0 组；必须显式改 `-crf/-preset` 生成不同字节。
- 增益指标验证：原全帧方案对黑边版本只有 66 分（<80 阈值），边缘裁剪后 93 分——这正是本版要解决的场景。
- 音频签名对纯正弦/静音样本区分度有限；真实内容仍需真机验证（见 29.6）。

### 29.5 产物

- `dist/DuplicateCleaner-1.1.0-0047.spk`：**39,096,320 B**
  - SHA256 `ce03b857bde5f5f6969934954c8c7e7a37affda53e33aa9605768309b54519be`
  - package.tgz MD5 `504a7e9c74946682c5ca9fd544bd91d4`
- 版本 1.1.0-0047 同步 8 处；SQLite schema 仍 **v8**（视频指纹/音频签名缓存为独立 SQLite 文件）。

### 29.6 真机待验证

- 真实影视库：H.264/H.265、不同码率/分辨率、上下黑边、片头片尾差几秒的命中率与耗时；
  音频签名对“国配/原声/无音轨”版本的区分与误判率；置信度分档与人工复核体验。
- 首扫重算所有视频指纹的一次性 CPU 开销（建议低峰时段）；`<var>/cache/video.sqlite` 体积增长。
- 极端误判场景：大段黑屏/片头字幕卡、同模板不同内容视频。

## 30. 附：v0048 字幕 ASR 模块优化（2026-11-08）

### 30.1 需求与方案

- 需求（v2 文档 字幕 ASR 模块）：失败智能重试与归类、无语音过滤、失败清单导出/批量重识别、
  CPU/负载阈值暂停队列、队列优先级（手动置顶/目录优先级）、ETA 优化、音轨选择，以及
  **视频目录监控自动入队**。
- 方案：在既有 `SubtitleService` 上增加失败分类/自动重试/失败清单；worker 取任务前负载检查；
  队列改为“优先级降序 + 创建时间”插入；ETA 用实际耗时中位数校准；音轨选择贯穿
  probe → `-map` → mp3/remux/预取；新增 `SubtitleWatchService` 轮询监控目录并自动提交任务。

### 30.2 实现

- **失败分类**（`failure_category`）：9 类（audio_extract/no_speech/timeout/unsupported_stream/
  permission/network/provider/internal/unknown），错误码映射 + 消息关键字兜底；`_handle_failure`
  统一处理自动重试与失败标记；`no_speech` 在开关开启时 `skipped=true`（不重试、不占队列）。
- **自动重试**：`subtitle_auto_retry_enabled`/`subtitle_auto_retry_max`（0–3）；可恢复分类且
  未达上限时回 `queued`（保留 ASR 分片缓存），审计 `subtitle_retry_auto`。
- **失败清单**：`GET /api/subtitles/failures`、`.../failures/export`（CSV BOM）、
  `POST .../failures/retry`（job_ids/group_id/category/force）；任务中心「失败任务」面板
  支持分类过滤、单条/批量/强制重试。
- **资源管控**：`subtitle_max_load` 超过阈值时 worker 保留队列（每 2 秒复查），`load_guard`
  状态随 `counts()`/`queue_snapshot()` 返回。
- **优先级**：任务 `priority`；`subtitle_dir_priorities` 提交时计算；`POST .../priority {top:true}`
  置顶；队列与重启恢复均按优先级排序。
- **ETA**：最近 20 个非跳过任务实际耗时中位数；执行中任务按进度估算剩余，再叠加排队任务。
- **音轨选择**：任务 `audio_track`/`audio_track_language` → `0:a:<ordinal>`；`audio_tracks()`
  查询接口 + 任务详情「查询音轨」；抽音频三处（NAS mp3 / 服务器 remux / 预取）统一带 `-map`。
- **目录监控**（新 `subtitle_watch.py`）：30s tick + 配置间隔；按 `(size,mtime_ns)` 去重
  （`<var>/subtitle_watch_state.json`）；每轮 ≤20 个；`user="watcher"` 自动提交，翻译开关沿用
  `subtitle_translate_enabled`；`GET /api/subtitles/watch` + `POST .../watch/scan`。
- 设置/前端：10 个新键；设置页「云端字幕」新增自动重试/跳过/负载/目录优先级/音轨/监控；
  任务中心新增失败面板与置顶按钮。

### 30.3 验证

- unittest：**642 OK（5 skipped）**（新增 `test_v0048_subtitles_v2.py` 16 例）。
- static：**193 PASS / 0 FAIL / 0 UNVERIFIED**（新增 `BE-SUBTITLE-V2`、`FE-SUBTITLE-V2` + 路由）。
- e2e：**199 PASS / 0 FAIL / 1 UNVERIFIED**（新增 `SUBF-01..06`：失败清单/CSV/监控状态与扫描/
  非法音轨/监控间隔；Windows symlink 历史项）。
- 真实 Chromium 探针（`.uiprobe/probe_v0048.mjs`）**6/6 PASS**：设置字段齐全、保存持久化、
  失败面板可见且含导出入口、无 console 错误；截图 `.uiprobe/v0048-settings.png`。
- 兼容性：旧任务 JSON 无新字段时按默认值加载（audio_track=None/priority=0/attempt=0）。

### 30.4 踩坑

- `_eta_seconds` 新分支必须保持旧语义：样本不足/创建时间在未来时返回 None（单元测试抓到两次）。
- 失败清单 CSV 表头是中文，e2e 最初按 `job_id` 断言失败，改为校验「任务ID/失败分类」。
- 目录监控 `scan_once` 默认尊重开关；设置页「立即扫描」用 `force=True`，否则用户会误以为按钮无效。
- 监控提交结果的 `jobs` 可能是整数（计数）也可能是列表，入队数需要兼容两种形状。
- 负载阈值在 Windows 调试环境无 `os.getloadavg` → 视为不限制（真机 DSM 有效）。

### 30.5 产物

- `dist/DuplicateCleaner-1.1.0-0048.spk`：**39,106,560 B**
  - SHA256 `aa13edb449eb14e3e5dc65297739e241de6b136682519a46b681ede75caae491`
  - package.tgz MD5 `8e638d186ac8eef947baceb04891da85`
- 版本 1.1.0-0048 同步 8 处；SQLite schema 仍 **v8**（监控状态为 `<var>/` JSON）。

### 30.6 真机待验证

- DSM 负载阈值与字幕并发（4 核 NAS 当前字幕并发 4 时 load ~9）的实际取舍；
- 音轨选择对国配/原声/多音轨 mkv 的识别与 `-map` 兼容性（服务器 remux 模式）；
- 目录监控自动入队 + 翻译流程、`skip_existing` 去重、误报（新文件拷贝/重命名）处理；
- 失败清单 CSV 在 Excel 中的中文显示（UTF-8 BOM）；自动重试对网络抖动的实际收益。

## 31. 附：v0049 任务中心 / 权限体检 / 日志增强（2026-11-08）

### 31.1 需求与方案

- 需求（v2 文档 全局 1/2/4）：“任务检索筛选（状态/目录/后缀/失败类型，批量选中/取消/重试）”、
  “权限与路径向导（一键配置共享文件夹读写权限；汇总无法读取/权限异常清单）”、“日志增强
  （可视化报错、按任务 ID 筛选、导出易读格式）”。
- 评估结论（REQUIREMENTS_V2_ASSESSMENT §3.1/§4）：**一键配置 ACL 做不到**（套件非 root），
  改为“一键**只读体检** + DSM 操作指引”；检索/批量/日志均可行。

### 31.2 实现

- **任务检索**（`SubtitleService._matches_filters`）：`q`（路径/任务ID/批次组/消息）、
  `suffix`（.mkv 或 mkv）、`directory`（path_key 前缀）、`category`（失败分类），与
  `status/group_id/batch_id/offset/limit` 组合；前端有筛选条件时走 `/api/subtitles?limit=200`，
  否则沿用队列视图。
- **批量操作**（`bulk_action`）：cancel/pause/resume/retry/priority，最多 200 个、逐条结果、
  审计 `subtitle_bulk_action`；前端「全选当前列表 / 已选 N 个 / 批量取消/重试/置顶」。
- **权限体检**（新 `permissions.py`）：收集设置路径（计划扫描/监控/白名单/目录优先级）+
  最近完成扫描 roots + 所有共享根，检查存在/目录/可读/可写（`os.access` = 套件用户实际权限），
  汇总扫描日志权限关键字与字幕 permission 失败，输出 `ok/warn/error` + DSM 指引；
  明确“只读检测、不自动改 ACL”。
- **日志增强**：`/api/logs/search`（关键字/任务 ID，最多 500 个日志文件、单文件 512KB，
  同时过滤审计）、`/api/logs/export`（可读文本）；前端日志页新增检索/清空/导出与 `<pre>` 匹配行。
- 版本 1.1.0-0049；schema 仍 v8，无数据库迁移。

### 31.3 验证

- unittest：**659 OK（5 skipped）**（新增 `test_v0049_taskcenter.py` 17 例：检索 5、批量 5、
  权限 3、日志 API 4）。
- static：**195 PASS / 0 FAIL / 0 UNVERIFIED**（新增 `BE-TASKCENTER-V2`、`FE-TASKCENTER-V2`
  + 路由/端点覆盖）。
- e2e：**204 PASS / 0 FAIL / 1 UNVERIFIED**（新增 `SUBJ-01/02`、`PERM-01`、`LOGS-01/02`；
  Windows symlink 历史项）。
- 真实 Chromium 探针（`.uiprobe/probe_v0049.mjs`）**11/11 PASS**：任务筛选栏/批量栏、日志检索
  （先扫描产生日志）、权限体检渲染、无 console 错误；截图 `.uiprobe/v0049-permission.png`。
- 真机待验证：DSM 共享文件夹权限体检结果与 `ls -l` 一致性；大量日志下检索耗时；
  200 个任务批量操作的响应与审计。

### 31.4 踩坑

- 测试客户端 `request()` 返回 headers 为 `[(name, value)]` 列表，导出检查需 `repr` 匹配。
- 前端审计条目是普通对象（`entry.action`）而非 Map（`entry.get`）。
- 权限体检只读：不要实现“自动改权限”承诺；评估结论已明确非 root 无法改 ACL。
- 检索接口在 `limit=0/offset` 分支下走 `list_page`，要保持旧 `{ok, jobs}` 形状兼容。

### 31.5 产物

- `dist/DuplicateCleaner-1.1.0-0049.spk`：**39,116,800 B**
  - SHA256 `000cd59274c8f55a9a2bdea7e66c8c5d9a0e77599988e736711394b93e2aab54`
  - package.tgz MD5 `43d150e673ec935a6f3eb1d5a20e2a43`
- 版本 1.1.0-0049 同步 8 处；SQLite schema 仍 **v8**。

### 31.6 v2 需求闭环总结

| 需求 | 版本 | 说明 |
|---|---|---|
| 增量扫描/哈希缓存/快速过滤/保留策略/排除规则 | v0043 | 哈希缓存、保留规则链、正则/glob/.ignore/附属文件 |
| 预览/安全删除（硬链接/独立回收站/白名单） | v0044/v0045 | 缩略图/并排对比；合并+回收站+删除白名单 |
| 计划扫描 + 通知 | v0046 | DSM/Webhook/Telegram、负载阈值 |
| 相似视频增强 | v0047 | 指纹 v2/裁剪/偏移/音频签名/置信度 |
| 字幕 ASR 优化 + 视频监控 | v0048 | 失败分类/自动重试/资源管控/优先级/ETA/音轨/目录监控 |
| 任务检索/权限向导/日志增强 | v0049 | 筛选批量、只读体检+指引、日志检索导出 |
| 评估结论中“做不到/需改法”的项 | — | ACL 一键配置（改体检+指引）；增量“仅比对新增”改缓存复用；不同剪辑版本不承诺；实时监控用轮询 |









