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

