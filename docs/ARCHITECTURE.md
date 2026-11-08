# 整体方案架构

> 项目：**重复文件清理与字幕（Duplicate Cleaner & Subtitles）** · 群晖 DSM 7.x SPK 套件
> 版本：1.1.0-0049
> 原则：**先扫描、出报告、人工确认、二次确认、可回收、全程审计**。任何情况下都不自动静默删除。

## 1. 目标与边界

### 1.1 目标

- 在 DSM 7.x（Btrfs / EXT4 存储池）中扫描一个或多个共享文件夹/子目录，提供**哈希精准匹配**与**内容相似度匹配**两种模式。
- 同一重复组只保留**创建时间最晚**的一份（默认策略），其余作为待处理副本。
- 结果分组展示，保留项高亮；可预览、勾选、全选、导出 CSV。
- 删除必须经过报告 → 人工确认 → 二次弹窗 → 服务端二次校验；默认移动到群晖回收站。
- 扫描限速、可暂停/继续/停止，尽量少占 CPU 与磁盘 IO。
- 操作日志持久化，可追溯扫描与删除记录。

### 1.2 明确不做

- 不自动删除、不静默删除、不在扫描完成时“顺手清理”。
- 不跟随符号链接，不扫描系统/隐藏目录，不进入 `@eaDir`、`#recycle`、`#snapshot`、`@docker` 等目录。
- 不保证业务语义上的“重复即可删”；只保证内容哈希一致。是否可删由用户判断。
- 不通过网络发送文件内容；哈希在 NAS 本地计算。

## 2. 总体架构

```
┌──────────────────────────────────────────────────────────────────────┐
│ 浏览器（DSM 桌面 → 套件图标 → 跳转）                                  │
│  http(s)://<NAS>:8787/                                                │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ SPA（src/web）：登录 / 文件夹选择 / 扫描选项 / 进度 / 结果组    │  │
│  │ 预览 / 两次删除确认 / CSV / 任务中心（含操作日志）/ 设置        │  │
│  └───────────────────────────┬────────────────────────────────────┘  │
└──────────────────────────────┼───────────────────────────────────────┘
                               │ 同源 HTTPS/HTTP + Cookie + CSRF
┌──────────────────────────────▼───────────────────────────────────────┐
│ Python 3.8+ 后端（src/backend，标准库，无第三方运行依赖）             │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────────────┐  │
│  │ HTTP 路由   │  │ 会话/DSM 认证│  │ 静态文件服务（web/）        │  │
│  │ /api/*      │  │ CSRF/限流    │  │ 健康检查/错误处理           │  │
│  └──────┬──────┘  └──────┬───────┘  └─────────────────────────────┘  │
│         │                │                                            │
│  ┌──────▼────────────────▼─────────────────────────────────────────┐  │
│  │ 业务层                                                          │  │
│  │  shares/browse  │  scanner  │  hashing  │  deletion  │  store   │  │
│  │  共享枚举/树    │  遍历/分组│  分片哈希 │  安全删除  │ 持久化   │  │
│  └──────┬───────────────────────────────────────────────┬──────────┘  │
└─────────┼───────────────────────────────────────────────┼─────────────┘
          │                                               │
┌─────────▼──────────────┐                    ┌──────────▼─────────────┐
│ DSM 系统能力            │                    │ /volumeX/<共享文件夹>  │
│ SYNO.API.Auth 登录      │                    │ 文件系统遍历 / 读取     │
│ SYNO.Core.Share 共享    │                    │ statx 创建时间          │
│ synoshare CLI 回退      │                    │ rename → #recycle       │
│ synogroup 管理员校验    │                    │ unlink 永久删除         │
└────────────────────────┘                    └────────────────────────┘
          │
┌─────────▼───────────────────────────────────────────────────────────┐
│ 数据目录 /var/packages/DuplicateCleaner/var（duplicatecleaner 700）│
│  settings.json  dupcleaner.sqlite3(+WAL)  plans/*.json  logs/*.log  │
│  audit.jsonl（旧 scans/*.json、deletions/*.jsonl 仅作迁移来源）    │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.1 为什么后端用 Python 标准库

- DSM 7 原生不带 Python 3，需要用户在套件中心安装官方 **Python3**；本套件在 `preinst` 明确检测并给出提示。
- 使用标准库（`http.server`、`ctypes`、`hashlib`、`threading`、`tarfile` 等）可避免第三方 pip 依赖、离线安装失败和 ABI 兼容问题。
- Python 的 `hashlib` 在释放 GIL 后可用多线程重叠磁盘读取，配合全局限速器即可在低占用与吞吐之间平衡。
- 扫描/删除都受线程池和事件控制，适合 NAS 这种需要“让路”的场景。

### 2.2 部署形态

- 套件安装到 `/var/packages/DuplicateCleaner/target`；数据在 `/var/packages/DuplicateCleaner/var`。
- `start-stop-status` 以系统内部用户 `duplicatecleaner` 启动后端；管理员需在 DSM 共享文件夹权限中给该用户授予读取/写入。日志/数据目录归套件用户所有。
- 监听 `8787/tcp`；DSM 桌面入口使用 `ui/config` 注册，点击后由 `ui/index.html` 跳转到当前主机名的 8787 端口。
- 认证使用 DSM 账号 + `SYNO.API.Auth`，只允许管理员组；后端不保存密码，只保存 DSM sid 对应的本地会话（内存）。

## 3. 关键设计决策

### 3.1 重复判定：三级漏斗，最终必须全量哈希

不做“大小 + 时间”判定。判定流程：

1. **大小分组**（`st_size`）：大小唯一的文件直接排除。
2. **快速指纹**（`quick_hash`）：对大小相同的文件，采样“头 64 KiB + 尾 64 KiB + 文件大小”（可配置 4–1024 KiB），只对指纹相同的文件继续。快速指纹只用于剪枝，不能作为最终依据。
3. **全量哈希**（`SHA-256` 默认，可选 `MD5`）：按 1 MiB 分片流式计算，只有全量哈希完全相同才归为同一重复组。

这样既能避免对大文件做无意义的全量读取（大部分候选在快速指纹阶段被排除），又能保证最终判定严格基于完整内容。大小相同但内容不同的文件不会误判。

> 为什么大文件仍必须全量哈希：任何采样都可能漏掉中间差异。分片读取 + 限速 + 并发让全量哈希的 IO/CPU 可控，但正确性不能打折。

### 3.2 创建时间：statx(STATX_BTIME)，绝不用 st_ctime 冒充

Linux 的 `st_ctime` 是 **inode 状态变更时间**，不是创建时间，复制/改权限/改属主都会变；在群晖上直接使用会违反“以创建时间为准”的核心规则。

后端通过 `ctypes` 调用 `statx` 系统调用（`AT_SYMLINK_NOFOLLOW`，请求 `STATX_BTIME`）：

- x86_64：syscall 332；aarch64：syscall 291。
- Btrfs：原生保存 btime；EXT4：inode 大小 ≥ 256 且内核支持时返回 btime。
- Python 若提供 `st_birthtime`（非 Linux 平台/开发机）则作为第二优先级。
- 若无法取得 btime：默认**不猜测**，标记时间来源为 `unknown`；该重复组不自动选择保留项，要求用户手动指定保留文件（否则整组不进入删除计划）。
- 只有显式开启 `allow_ctime_fallback` 才用 `st_ctime` 降级，并在 UI 中明确标注风险；生产默认关闭。

时间同时保存 epoch 秒 + 纳秒与带时区 ISO，避免时区/精度问题；比较时使用完整精度，创建时间完全相同则标记 `tie`，不自动保留。

### 3.3 保留策略

| 模式 | 规则 | 安全处理 |
|---|---|---|
| `latest`（默认） | 组内创建时间最大者保留 | 时间来源不可信/相同 → 不自动选择，需人工 |
| `earliest` | 组内创建时间最小者保留 | 同上 |
| `pinned` | 路径等于指定文件，或位于指定目录前缀下者保留 | 多个命中取其中创建最晚；无命中 → 人工 |

非保留文件默认 `default_selected=true`；需要人工的组全部 `false`，UI 必须先选保留项。

### 3.4 文件删除：默认回收站，永久删除需额外短语

- 默认 `recycle=true`：`rename()` 到
  `<共享文件夹根>/#recycle/@duplicate-cleaner/<YYYYMMDD-HHMMSS>/<原相对路径>`。
  同名冲突追加 `.dup-<n>`；只在同一文件系统内 rename，跨设备则失败并报告，绝不复制后删除源文件。
- `recycle=false`：必须由用户输入“永久删除”短语，后端确认后才 `unlink()`。
- 共享文件夹未启用回收站（不存在 `#recycle`）时默认不创建、不降级为永久删除；UI 显示 `RECYCLE_UNAVAILABLE`，用户可改为永久删除（需再次确认）或取消。
- **删除前逐文件重新校验**：存在性、普通文件、realpath 在允许共享目录内、不是保留项、size/mtime_ns/inode/dev 未变、保留文件仍存在；精准组要求候选与保留文件全量哈希一致，相似组要求各自内容哈希与扫描记录一致。任何一项失败 → 跳过并记录，不删除。
- 删除计划 5 分钟过期、一次性、绑定会话；`confirm` 必须精确为 `DELETE`。

### 3.5 安全边界

- 只允许共享文件夹根及其子目录；禁止 `/etc`、`/usr`、`/var`、`/root`、`/volumeX` 根、`@*`、`#recycle`、`#snapshot`、`@eaDir` 等。
- 扫描不跟随符号链接；删除前用 `realpath` + 父路径检查，拒绝 symlink 替换。
- 同一 inode 的硬链接只保留一个路径参与判定（否则“删除”不释放空间，还会误报可释放空间）。
- Web 端：Cookie 会话 + HttpOnly + SameSite=Strict；非 GET 请求校验 CSRF Token 与 Origin；登录限流；仅管理员。
- 后端默认监听 0.0.0.0 便于 DSM 桌面访问，但应只暴露在可信局域网；公网访问必须走 VPN 或 DSM 反向代理 + TLS。支持 `--tls-cert/--tls-key` 启用 HTTPS。

## 4. 前端页面结构

（详细线框图与交互见 `docs/UI_PROTOTYPE.md`）

静态 SPA 采用 ES modules：`web/app.js` 为入口，常量/状态/DOM 工具/格式化/API/Toast/弹窗/字幕页拆到 `web/js/` 下模块；无构建步骤、无第三方依赖。后端对 `.js/.css` 响应 `Cache-Control: no-cache`，升级后浏览器会重新校验模块文件。

1. **登录页**：DSM 账号、密码、可选 2FA；说明仅管理员可用。
2. **主框架**：顶部常驻风险提示条 + 顶部导航 + 内容区。
3. **扫描页**：
   - 扫描范围卡片：已选根目录 chips、排除目录 chips、打开文件夹选择器。
   - 扫描选项卡片：哈希算法、保留策略、大小区间、后缀过滤、忽略大小写、跳过规则、性能限速、回收站默认值。
   - 进度卡片：阶段、进度条、文件/目录计数、当前路径、暂停/继续/停止。
4. **结果页**：
   - 摘要：重复组数、重复文件数、可释放空间、已扫描文件、需人工组数、失败数。
   - 工具栏：搜索、排序、全选待删除副本、清空、导出 CSV、删除选中。
   - 组卡片：每组标题（大小、副本数、可释放）、保留项高亮 + 保留原因徽标；文件行包含路径、大小、完整哈希、创建时间（来源）、修改时间、预览、勾选。
   - 需人工组：显示警示，v1 禁止删除、禁止一键全选，需在 DSM 中人工处理。
5. **删除向导**：第一屏“确认删除清单” → 第二屏“最终确认”（永久删除需输入短语）→ 后台任务进度 → 逐文件结果。
6. **字幕页（v0028）**：选择目录 → 枚举视频（递归/非递归、跳过系统与隐藏目录）→ 勾选 → 批量生成或只翻译已有字幕；字幕以 `.srt` 写回视频同目录，批次进度支持暂停/继续/取消/重试；v0038 起任务详情记录各步骤开始/结束/耗时（排队、媒体探测、音频提取+识别、翻译、写盘）及 ASR 分片/翻译批次明细；v0039 起批次进度上方增加「批次总览」，进行中/最近完成的批次组可切换查看与控制。详见 [SUBTITLE_BATCH_CONTRACT.md](SUBTITLE_BATCH_CONTRACT.md)。
7. **任务中心（v0038 调整 / v0039 队列视图）**：扫描历史、字幕任务、删除任务、操作日志，标签页切换；字幕任务按「进行中 / 即将执行（真实 FIFO 队列位置）/ 已暂停 / 最近完成」展示。
8. **操作日志**：审计事件（含稳定 id，可勾选删除或只保留最近 N 天）+ 日志文件列表/下载/删除；删除动作本身记审计。
9. **设置**：默认扫描参数、并发/限速、回收站策略、云端字幕（含目录批量默认选项）与「系统 · 存储与运行诊断」（v0038 从任务中心移入）。

## 5. 后端扫描处理流程

### 5.1 阶段与状态

`queued → running(enumerate → size → sample → hash → finalize) → completed`
可中断为 `paused / stopping / stopped`；异常为 `failed`。同一时间只允许一个扫描任务，避免多任务同时打满 IO。

### 5.2 枚举（enumerate）

1. 校验请求：DSM 会话、管理员、roots/excludes 真实存在且在共享目录内、无系统目录、exclude 属于某个 root。
2. 为每个 root 建立扫描任务；使用 `os.scandir` 迭代（不递归 Python 栈），逐项：
   - 符号链接/快捷方式 → 跳过 `symlink`；
   - 目录名为系统/隐藏目录（`@eaDir`、`#recycle`、`#snapshot`、`@docker`、`@synologydrive`、`.SynoFSFolder`、`.@__thumb` 等）→ 跳过；
   - 命中排除目录/后缀/大小区间/空文件规则 → 跳过并计数；
   - 只接受普通文件；读取 `lstat` 元数据 + statx btime；
   - 用 `(st_dev, st_ino)` 去重硬链接；
   - 超过 `max_files`（默认 500,000）立即停止并报错，避免 NAS 内存/句柄耗尽。
3. 每处理 N 个文件更新进度（节流），检查 pause/stop 事件。

### 5.3 大小分组（size）

`size -> [file]`，只保留 `count >= 2` 的桶。0 字节默认已跳过。

### 5.4 快速指纹（sample）

对每个候选文件读取头/尾采样（`os.pread`，不改变文件偏移，多线程安全），计算 `SHA-256(size || head || tail)` 作为 `quick_hash`；按 `(size, quick_hash)` 分组，只保留 `count >= 2`。

### 5.5 全量哈希（hash）

- 线程池 `concurrency`（1–8，默认 2）；
- 每个文件以 1 MiB 分片顺序读取，`hashlib.sha256/md5` 增量更新；
- **全局令牌桶限速**：`hash_rate_mbps`（0 为不限），令牌跨线程共享，超限时 sleep；暂停/停止时立即中断；
- 统计实际读取字节、哈希字节、耗时、速度；
- 完成后按 `(size, full_hash)` 分组；只有 count>=2 才是重复组。

### 5.6 选择保留项与生成报告（finalize）

- 按 `keep_mode` 计算 keep 文件：全部文件有可信 btime 时按创建时间；任一组员缺少可信 btime 时，整组改用 **mtime_ns** 选择保留项（不混用 btime/mtime，不用 ctime 比较），并标记 `time_fallback=true`；可信 btime 相同仍转人工；pinned 命中优先保留，未命中按 mtime 兜底；
- 计算每组 `wasted_bytes = size * (count - 1)`；
- 生成 `scan_id`、组/文件 ID、摘要统计；写入 `scans/<scan_id>.json`（临时文件 + `os.replace` 原子替换）；
- 写 `logs/scan-<id>.log` 与 `audit.jsonl`；UI 轮询拿到 `completed` 后展示报告；
- 报告只展示，不触发任何删除。

### 5.7 删除计划（delete-plan）

- 用户勾选后前端请求计划；后端重新加载扫描报告，按当前设置/勾选计算候选；
- 对每个候选与保留文件执行 3.4 的完整校验；校验通过的生成 plan 快照，失败的放入 `skipped`；
- 候选总量 ≤512 MB 时 plan 阶段立即做全量哈希复核；超过预算时 plan 阶段只做元数据校验，
  全量哈希推迟到实际删除前逐文件执行——执行阶段始终重新哈希候选与保留文件，安全性不降低；
- plan 保存到 `plans/`（或内存+磁盘），5 分钟 TTL，一次性，绑定会话；
- 前端展示计划摘要与冲突项，进入二次确认。

### 5.8 执行删除（delete）

- 校验 plan、`confirm=DELETE`、永久删除短语；
- 后台线程逐文件执行，每完成一个写审计事件与进度；
- 回收站模式：计算目标路径 → 创建父目录 → 同文件系统 `os.rename`；永久模式：`os.unlink`；
- 失败/跳过不中断整体任务，最终状态 `completed/partial/failed`；
- 逐文件结果写 SQLite `deletion_results`（`log_file` 仅作兼容标识）与 `audit.jsonl`，UI 轮询展示逐文件结果。

## 6. 群晖 API 调用点

代码中统一用中文注释标注 `【群晖 API】` / `【创建时间】` / `【哈希计算】` / `【文件删除】`。

| 能力 | 调用点 | 说明 |
|---|---|---|
| 登录 | `SYNO.API.Auth` `method=login`，本地 5001/5000 | 使用 DSM 账号密码换 sid；2FA 传 `otp_code`；不落盘 |
| 登出 | `SYNO.API.Auth` `method=logout` | 会话过期/用户退出时调用 |
| 共享文件夹枚举 | `SYNO.Core.Share` `method=list` | 管理员 sid 调用；失败回退 `synoshare --enum SHARE` 和文件系统目录扫描 |
| 管理员校验 | `synogroup --member administrators` / `id -Gn` | 仅管理员可使用删除能力 |
| 创建时间 | Linux `statx`（`STATX_BTIME`） | 群晖内核能力，不是 DSM Web API；Btrfs/EXT4 均支持 |
| 文件哈希 | POSIX 分片读取 + `hashlib` | 群晖没有公开的“文件哈希”Web API；本地读取是唯一可靠方式 |
| 删除/回收站 | POSIX `rename` → `<share>/#recycle/...` / `unlink` | 群晖 File Station 的回收站本质也是移动进 `#recycle`；直接操作可精确控制路径与审计。若需接入 File Station 异步删除 API，可在 `deletion.py` 的删除点替换为 `SYNO.FileStation.Delete`，但必须处理异步任务和逐文件结果 |

> 安全说明：本套件以 DSM 系统内部用户 `duplicatecleaner` 运行（非 root），只拥有管理员显式授予的共享文件夹权限。首次使用需在 DSM 控制面板给该用户授权，未授权时不会静默跳过或删除。
> 因此代码层面强制路径白名单、系统目录黑名单、管理员认证、二次确认和删除前重校验。
> 若只需要扫描不需要删除，可在设置中关闭删除操作（或后续版本提供只读模式）。

## 7. 数据与日志

```
/var/packages/DuplicateCleaner/var/
├── config.json                 # 设置（不含密码）
├── scans/
│   └── 20261101-201600-ab12cd.json   # 扫描报告（含组、文件、哈希、时间、保留项）
├── plans/
│   └── plan-....json           # 5 分钟删除计划（一次性）
├── deletions/
│   └── del-....jsonl           # 逐文件删除记录
├── logs/
│   ├── backend.log             # 服务运行日志
│   └── scan-20261101-....log   # 扫描摘要
├── audit.jsonl                 # 登录/扫描/计划/删除审计事件（追加写）
└── tmp/                        # 原子写入临时文件
```

- 报告、计划、日志均使用原子写（`tmp + os.replace`）或追加写，异常断电不产生半个 JSON。
- 卸载默认保留 `var/`（日志持久化）；需手动清理。
- 审计日志包含时间、用户、动作、scan_id、组数、文件路径、结果；不记录密码/sid。

## 8. 性能与限速

- 扫描线程数默认 2，可 1–8；哈希只在候选文件上执行。
- 快速指纹把大文件全量哈希范围大幅缩小（典型照片/视频库中大部分文件在大小或采样阶段已排除）。
- 全局令牌桶限制读取速率（MB/s）；分片 1 MiB；暂停/停止在每个分片检查。
- 进程以 `nice 10` + `ionice class 2 best-effort 7` 启动（若系统有 `ionice`）。
- 单任务扫描；`max_files` 上限；结果分页；删除任务逐文件校验，避免一次性重哈希全部文件。
- UI 轮询 800 ms，服务端返回轻量摘要；组列表分页 100 条。
- 结果分页/搜索/排序走 SQLite 索引列（schema v2+）；schema v5 起 `scan_files` 路径搜索在 SQLite 支持 FTS5 + trigram 时用 `scan_files_fts`（触发器同步），否则自动回退 LIKE 子串。100k 明细基准（`qa/bench_search.py`）：FTS P95 ≈ 80 ms，强制 LIKE P95 ≈ 164 ms。
- 「内容相似/视频」缓存仍落 SQLite；字幕 ASR 分片按任务/分片缓存，支持暂停续跑。

## 9. 兼容性与限制

- 支持 DSM 7.0+；Btrfs、EXT4 存储池；共享文件夹/子目录。
- 需要 Python 3.8+（官方 Python3 套件）。
- 加密共享文件夹在解锁后可扫描；套件不负责自动解锁。
- 不支持：跨共享文件夹的硬链接（Linux 不允许）、符号链接指向的目标、系统目录、快照内部、Docker 内部卷。
- 若文件系统/内核不提供 btime，默认不自动选保留项；这是有意的安全降级，不是 bug。
- 单次扫描文件数默认上限 50 万；超大库建议分目录多次扫描。
- 浏览器需支持 ES2019；推荐 DSM 内置 Chrome/Firefox。

## 10. 与需求的对应关系

| 需求 | 实现 |
|---|---|
| 文件夹多选/排除 | `/api/shares` + `/api/browse` + 文件夹选择器；后端白名单校验 |
| 哈希判重、非文件名 | 大小 → 快速采样 → 全量 SHA-256/MD5；三级漏斗 |
| 大文件分片/限速 | 1 MiB 分片 + 全局令牌桶 + 并发 1–8 + nice/ionice |
| 只保留创建最晚 | `keep_mode=latest`；优先 statx btime；无 btime 时整组按 mtime 兜底并标记时间回退 |
| 分组、保留高亮 | 结果页组卡片；keep 行高亮、保留原因徽标 |
| 路径/大小/哈希/创建/修改 | 文件行完整展示；哈希可复制 |
| 预览/勾选/全选 | 预览 API（Range）；组/全局选择；全选待删除副本 |
| 禁止静默删除 | 扫描完成只出报告；plan + 两次确认 + 服务端重校验 |
| 统计数量/可释放空间 | 摘要卡片；按大小 × 副本数计算 |
| 回收站可选 | 默认移动到 `#recycle/@duplicate-cleaner/...` |
| 操作日志持久化 | `audit.jsonl` + scan log + deletion jsonl；下载接口；v0038 支持按 id 删除 / 只保留最近 N 天（审计与日志文件） |
| DSM 7 / Btrfs / EXT4 | `os_min_ver=7.0`；statx；不依赖文件系统私有 API |
| 避开系统目录 | 共享根白名单 + 目录黑名单 + 不跟随 symlink |
| 跳过规则 | symlink/空文件/隐藏/自定义后缀/大小区间 |
| 停止/暂停/CSV | 任务状态机 + 事件；CSV 带 BOM |
| 保留最早/指定路径 | `keep_mode=earliest/pinned` |
| 大小区间 | `min_size_bytes/max_size_bytes` |
| 忽略大小写后缀 | `ignore_case_ext` |
| 风险提示常驻 | 顶部固定风险条，不可关闭 |


---

## 11. 内容相似度扩展（v1.1）

> 详细接口与算法见 [SIMILARITY_CONTRACT.md](SIMILARITY_CONTRACT.md)。

### 11.1 两种模式

| 模式 | 行为 | 结果 |
|---|---|---|
| `exact`（默认） | 原有三级哈希漏斗，全量哈希一致才算重复 | 全部标记 `match_type=exact`（哈希一致） |
| `similar` | 先执行精准哈希；再对允许类型提取内容特征，按阈值做相似检索 | 精准组 + 相似组，分区展示 |

相似模式中，已进入精准组的文件不重复进入相似组；压缩包/媒体/可执行文件/超大文件只参与精准哈希，
不做相似计算（由类型、大小和硬编码黑名单控制），避免 CPU/IO 过载。

### 11.2 内容特征

- **文本/文档**（txt/md/csv/json/xml/html/log/pdf/docx/odt）：
  规范化文本 → 字符 5-gram → 64 位 MinHash 签名；相似度 = Jaccard 估计。
- **图片**（png/jpg/jpeg/webp）：PNG 用标准库解码后计算 64-bit dHash；
  JPEG/WebP 优先 Pillow（可选安装），无 Pillow 时降级为字节 MinHash 并标记低置信。
- **二进制**（仅高级开关允许）：1 KiB 分片 5-gram MinHash。
- 候选检索：MinHash 16 bands × 4 rows，pHash 分带；只对候选对计算精确分数。
- 分组：安全团（组内任意两份都 ≥ 阈值），避免链式传递把不相似文件并进同组；
  相似组至少 2 个文件，组内按原保留策略（默认最晚创建）选保留项。
- 每个相似文件保存 `content_sha256`，删除前用于验证内容未被修改；相似组不要求候选与保留文件哈希相等。

### 11.3 性能与缓存

- 相似特征并发 1–4；复用全局读取限速；每个文件/分片检查暂停/停止。
- 特征缓存：`<var>/feature_cache.json`，key = `v1|size|mtime_ns|inode|dev|type`，
  原子写、最多 50,000 条；同内容文件在一次扫描内复用特征。
- 缓存只是加速层；删除安全性仍靠删除前身份 + 内容哈希重校验。
- 统计：相似候选数、特征数、缓存命中/未命中、候选对数、按原因跳过数。

### 11.4 人工复核门禁

- 相似组 `default_selected=false`、`manual_review_required=true`；
  前端禁止组级全选、禁止全局一键批量选择相似组。
- 删除计划若包含相似文件，必须显式提供 `file_ids`、`confirm_similar=true` 和
  `reviewed_similar_group_ids`；否则返回 `SIMILAR_REVIEW_REQUIRED`，不生成计划。
- `scope=all_duplicates` 永不包含相似组。
- 相似组需逐组勾选“人工复核”后，用户逐条勾选文件；删除按钮默认置灰。
- 相似组仍执行“创建时间不可靠则整组禁止删除”的原有安全规则。

### 11.5 展示与导出

- 结果页固定分区：**哈希一致（精准重复）** / **内容相似**；摘要分别统计组数、文件数、潜在可释放空间。
- 相似组显示“内容相似 · N%”、相对保留文件的相似度、方法（MinHash/pHash/bytes）、低置信标记。
- 相似区块常驻黄色警示：
  “内容相似度为 AI/特征比对结果，存在误匹配概率，相似组必须人工复核，禁止一键批量删除相似文件。”
- CSV 新增列：`匹配类型`（哈希一致/内容相似）、`相似度分值`（0–100）。

## 12. 目录批量字幕扩展（v0028）

> 详细接口、字段与验收见 [SUBTITLE_BATCH_CONTRACT.md](SUBTITLE_BATCH_CONTRACT.md)。

### 12.1 与查重解耦

- 新增独立「字幕」页：选目录 → 递归枚举视频 → 勾选 → 批量提交，不依赖扫描结果。
- 后端新增 `POST /api/subtitles/enumerate`（递归枚举，跳过系统/隐藏/符号链接，只留视频后缀；不读时长、不调 FFmpeg）与 `POST /api/subtitles/batch`（批次提交，部分无效不整批失败）。
- 批次控制：`GET /api/subtitles/batches/{id}`、`POST /api/subtitles/batches/{id}/{pause|resume|cancel|retry}`；`GET /api/subtitles?batch_id=` 支持过滤。

### 12.2 两种任务来源

| source | 行为 | 是否上传音频 |
|---|---|---|
| `asr`（默认） | FFmpeg 抽音频 → 云端 ASR → 可选 LLM 翻译 | 是 |
| `existing_srt` | 读取视频旁 `<stem>.srt` → 只调 LLM 翻译 | 否（只上传字幕文本） |

### 12.3 落盘策略

- 任务目录默认生成 `original.srt`（可下载）；v0033 起 `.vtt` 仅在设置 `subtitle_write_vtt=true` 时生成。
- `subtitle_output_mode=alongside_video`（默认）时，把 `.srt` 原子复制到视频同目录：`<stem>.srt`、`<stem>.<lang>.srt`、`<stem>.bilingual.<lang>.srt`；**只写 srt，不覆盖已有文件**。
- 目标目录不可写/复制失败：任务仍为 `done`，置 `warning=OUTPUT_NOT_WRITABLE/OUTPUT_WRITE_PARTIAL`，任务目录下载保留。
- 已有字幕按 `subtitle_skip_existing`（默认 true）跳过，不会重复调用 ASR。

### 12.4 容量与恢复

- 默认单 worker 串行执行；v0034 起 `subtitle_concurrency`（1..4，默认 1）可并行执行多个任务，v0037 起保存设置后立即生效（动态增减 worker，无需重启），批次内仍按提交顺序取队。
- 已完成任务保留上限 `subtitle_job_limit`（默认 200）；LRU 清理只删任务目录与 DB 记录，**不删视频旁 srt**。
- SQLite schema v6 新增 `subtitle_batches`；v0034 起服务启动时 `queued` 任务自动重新入队继续（不再全判 `SERVICE_RESTART`），`running/translating` 有分片转 `paused` 可继续（沿用分片缓存）。
- v0034 可见性：组快照带 `active_jobs`/`eta_seconds`/`concurrency`，任务列表支持 `status` 过滤；前端任务明细与任务中心把执行中任务置顶。
- 批次状态由子任务实时聚合，不单独持久化状态机。

### 12.5 补翻译（v0035）

- 历史任务解耦：已完成但 `translate=false` 的任务（或 `translation_partial=true` 的部分译文任务）可派生一条**仅翻译任务**（`source=existing_srt` + `source_job_id`），从源任务目录 `original.srt` 取文本，只调 LLM——不跑 FFmpeg、不重新 ASR、不上传音频，原任务记录不变。
- 源文本解析顺序：源任务目录 `original.srt` → 视频同目录 `<stem>.srt` → 都没有则 `SOURCE_SRT_MISSING`；视频文件已删除也能补翻译（任务目录可下载，写回视频旁按 warning 降级）。
- 接口：`POST /api/subtitles/{job_id}/translate`（单任务，幂等）与 `POST /api/subtitles/batch-groups/{group_id}/translate`（服务端筛选可翻译任务，新建批次组，返回 summary 分类计数）；组快照新增 `translatable={count,partial}` 供按钮计数。
- 重启恢复：翻译任务运行中遇重启转 `paused` 可继续（无 ASR 分片损失）；`queued` 沿用 v0034 自动重新入队。

### 12.6 语言码与译文输出组合（v0036）

- 语言码：后端 `LANGUAGE_RE` 接受任意 2–3 位合法语言码；设置页下拉扩展为常见 19 种，非下拉合法码由前端保留（临时补 option），不再在保存设置时被重置为 `auto`/`zh`。
- 译文输出：`subtitle_translate_outputs`（`both`/`target_only`/`bilingual_only`）在 `_write_outputs`/`_write_alongside` 统一裁剪译文文件；`original.srt` 始终保留（补翻译源文本），已有文件不覆盖、旧文件不删除。

### 12.7 分片音频预取（v0040）

- 单任务原本是「抽第 N 片音频（FFmpeg）→ 上传 → 识别 → 下一片」，FFmpeg 与网络互等；`subtitle_concurrency` 只解决任务间并行，不解决任务内互等。
- `_run_chunked_asr` 现在通过 `_AudioPrefetch` 只预读 **1 片**：预取线程在主线程识别第 N 片时抽第 N+1 片，结果经内存/条件变量交接；`take(index)` 时已就绪则零等待。设置 `subtitle_audio_prefetch`（默认 `true`）关闭时退化为原地串行抽取。
- 边界语义：仍按分片顺序 ASR（时间轴/内容不变）；暂停只在分片边界生效并丢弃预取；取消/任务收尾通过传入 FFmpeg 的 `stop_check` 让在跑抽取尽快退出；抽音频异常保存后在主线程 `take()` 原样抛出（错误码不变）；缓存里已完成的分片不预取；未消费的临时音频在 `close()` 中删除，任务临时目录整体 `rmtree`。
- 观测：`timeline.asr_chunks[]` 的 `extract_sec`/`asr_sec` 记录各自真实时间窗（预取时两者重叠），仍是定位「慢在抽音频还是识别」的依据。

### 12.8 任务视图的进度信息（v0041）

- `SubtitleJob.snapshot()` 新增 `eta_seconds`（仅 `running`/`translating` 有值）：ASR 阶段用已完成分片 `extract_sec + asr_sec` 均值 × 剩余分片数；翻译阶段用已完成批次 `duration_sec` 均值 × 由进度带（70..90）反推的剩余批次数；其它情况返回 `None`。只读内存记录，不做文件 IO，列表轮询无额外开销，也不持久化。
- 前端进度条统一走 `subtitleProgressBar(pct)`：`el()` 只支持 `className/text/attrs/dataset/on`，**不支持 `style`**，此前 `el(..., {style: 'width:..%'})` 被静默丢弃导致任务行/详情进度条恒为空条。
- 分块文案按后端语义（`chunk_index` 为 1-based 已完成片数）直接展示，不再 +1；任务行顺序调整为 `阶段 · 分块 · 步骤消息 · 已耗时 · 预计剩余 · 译文 · 创建`，来源移到行尾弱化。

### 12.9 服务启动与升级（v0042）

- **启动耗时构成**（NAS 实测，7540 个字幕任务 / 4672 排队）：`build_services` 27s，其中逐任务解析 `subtitles/jobs/*.json` 占 21s、DB 读取 0.5s、其余为 `load_subtitle_chunks` 逐任务查询与队列恢复。
- **优化**：`_load_jobs` 以文件名即 job_id（`_job_json`）判定 DB 是否已有该任务，命中则跳过文件读取；仅 DB 缺失时才解析 JSON 并回写 DB（兜底语义不变）。终态任务的 ASR 分片缓存查询也跳过（分片只影响“能否继续执行”）。
- **升级链路**：DSM 7 升级 = `preupgrade` → 停服务 → 换文件 → `postupgrade` → `start-stop-status start`；`start` 返回非 0 即被标记“启动失败”，套件中心要求【修复】。因此 `start` 的健康检查放宽到 90s，失败清理残留后重试一次，进程存活但未通过检查时返回成功（宁可显示运行中，也不判升级失败）；`PYTHONPYCACHEPREFIX` 指向 `<var>/pycache`，`postupgrade` 在 root 下补 `chown`。
- 可观测：`<var>/log/start.log` 记录每次启动尝试、耗时与失败时的 backend.log 尾部。

## 13. 增量扫描 / 保留规则链 / 排除规则（v0043）

> 详细字段、语义与验收见 [INCREMENTAL_SCAN_CONTRACT.md](INCREMENTAL_SCAN_CONTRACT.md)。

### 13.1 增量扫描（跨扫描哈希缓存）

- 需求原文“仅对比上次扫描后新增/修改的文件”**不能照做**：会漏掉“新文件与未变化旧文件重复”、
  “旧文件被修改后与另一旧文件重复”等主要场景。实现改为：枚举仍 `stat` 全部文件，
  **哈希复用**——`(dev, inode)` + `size` + `mtime_ns` 未变、算法/采样配置一致时直接复用
  `quick_hash`/`full_hash`，不读文件内容；第二次扫描 `bytes_hashed` 接近 0。
- 新表 `hash_cache` / `media_probe_cache`（schema v8），只缓存候选文件；上限 + LRU 裁剪；
  `GET /api/cache`、`POST /api/cache/clear` 可查看/清理。
- 结果带增量标记：文件级 `new_since_last_scan` / `changed_since_last_scan` / `hash_cached`，
  组级 `new_files` / `changed_files`；`GET /api/scans/{id}/groups?new_only=1` 只看增量，
  UI 有“只看本次新增/变化”开关与组徽标。
- 安全不变：缓存只影响速度；删除前仍做 size/mtime_ns/inode/dev + 全量哈希重校验。

### 13.2 快速指纹 v2

- `QUICK_SAMPLE_VERSION=2`；大文件（≥256 MiB）在头/尾之外加采样**中间段**，降低“只换中段”漏检。
- 采样版本参与缓存校验：版本升级时旧条目只复用 `full_hash`，`quick_hash` 重算。

### 13.3 保留规则链

- `keep_rules` 为有序规则数组；某条分出唯一胜者即停止，平局继续下一条，仍平局转人工（禁止删除）。
- 支持：创建/修改/访问时间（最早/最晚）、体积、路径优先级、文件名优先级（通配符或 `/正则/`）、
  带内嵌字幕优先、内嵌音轨多者优先；后两者需要 FFmpeg 探测，结果进 `media_probe_cache`。
- `keep_rules` 为空时逐字保持 `keep_mode`/`pinned_path` 的历史语义（含 btime 不可信时 mtime 回退）。
- 注意：精准重复组内文件大小必然相同，`size_largest` 只在相似/视频组有意义；`atime` 在 DSM 默认
  `relatime` 下不可靠，仅作参考。

### 13.4 排除规则与附属文件

- 文件名排除：正则（basename、IGNORECASE、保存时校验）与通配符（≤50 条）。
- `.ignore`：gitignore 常用子集（`#` 注释、`!` 否定、前导 `/` 锚定、结尾 `/` 只匹配目录、
  `*`/`?`，`**` 折叠为 `*`），带 base_dir 作用域；解析失败只告警不中断。
- 影视附属文件：默认只跳过“与同目录视频同源”的字幕/海报/nfo（同名、带语言段、`X-poster.*`、
  常见固定名），**不按后缀无差别跳过**（照片库不受影响）。

## 14. 预览增强与删除白名单（v0044）

> 详细字段与验收见 [PREVIEW_PROTECTION_CONTRACT.md](PREVIEW_PROTECTION_CONTRACT.md)。

### 14.1 预览增强

- 问题：原实现把视频 `fetch → blob` 后播放，等于**先下载整段视频**才能看，大文件无法“片段播放”。
- 现在：先 `Range: bytes=0-0` 探测类型；视频/音频**直接用 URL** 作为 `<video>/<audio>` 源，
  浏览器按 Range 流式加载，可立即拖动与定位；图片/文本/PDF 仍走原来的完整读取。
- 缩略图：FFmpeg 抽 1 帧 → JPEG（默认宽 320），缓存 `<var>/cache/thumbs/<sha1(path|size|mtime|t|w)>.jpg`，
  上限 3000 条 / 300MB，LRU 裁剪；进程内并发 2。`GET /api/preview/thumb`。
- 媒体信息：解析 `ffmpeg -i` 的 Stream 行得到时长/分辨率/编码/音轨/字幕轨（含语言），内存缓存 200 条；
  `GET /api/preview/media`。缩略图时间点清单 `GET /api/preview/thumbs`（只给时间点与 URL）。
- 前端：视频预览弹窗显示媒体信息 + 6 张可点击缩略图（跳到对应时间点）；同组 ≥2 文件时提供
  “并排对比”弹窗（左右两个播放器、同组文件下拉、同步播放开关、对齐时间点）。
- 限制：浏览器不支持 HEVC/MKV/DTS 等编码时无法播放（缩略图仍可见）；不提供在线转码。

### 14.2 删除白名单（保护附属文件）

- 为什么需要：判重只看内容；外挂字幕/海报/nfo 与视频不是同内容，正常不会成组，但**同名附属文件
  之间的重复**（如两份 `movie.srt`）或用户误选会导致删除附属文件；白名单是删除侧的终局防线。
- 规则：目录（绝对路径前缀）/ 后缀（默认字幕+元数据，**不含图片**）/ 文件名正则 /
  视频附属文件（同目录存在同源视频的文件，视频本身不受影响；照片库无视频的图片不受影响）。
- 三层生效：扫描期标记（UI 不可勾选、计入 `stats.protected_files`、不计入可选数量）→
  计划期按当前设置重算（`skipped[].reason=PROTECTED`）→ 执行期再算一次（防止计划生成后改设置/改名）。
- 性能：目录内视频 stem 按目录缓存（一次 `scandir`），一次计划/任务内复用。

## 15. 硬链接合并与套件独立回收站（v0045）

> 详细字段与验收见 [MERGE_TRASH_CONTRACT.md](MERGE_TRASH_CONTRACT.md)。

### 15.1 硬链接合并（`merge.py`）

- 需求原文“硬链接替代删除”做一个**独立操作**（默认关闭、逐组确认），只对精准重复组生效：
  非保留项 `link(master, tmp)` + `rename(tmp, candidate)` 原子替换成指向保留项的硬链接，
  路径保留、内容共享、空间真正释放。
- 计划阶段校验扫描记录仍一致、受保护/保留项跳过、同一文件系统、非已链接；执行阶段再跑一次
  `revalidate(..., hash_check=True)`（含全量哈希），任何不一致跳过。计划 5 分钟过期、一次性、
  `confirm="MERGE"`。
- 合并不可逆的“第二份 inode”无法恢复，但 manifest（`<var>/merges/*.json`）支持
  “恢复为独立副本”：`FICLONE`（Btrfs 秒级复制）优先，回退普通复制并保持 mtime。
- 合并后扫描器按 `(dev,inode)` 去重，重复组会缩小 → 结果页语义与“可释放空间”自然收敛；
  需要立即看到变化时可重新扫描。

### 15.2 套件独立回收站（`trash.py`）

- 删除去向可选：`share`（默认，`<share>/#recycle/@duplicate-cleaner/...`，DSM 回收站可见）/
  `package`（`<share>/#duplicate-cleaner-trash/<时间戳>/<相对路径>`，同卷 rename）。
- 为什么不用 `#recycle` 直放：DSM 回收站 UI 只认自己的目录结构，手工塞入可能不显示、也不便
  “按删除批次整组恢复”。`#` 前缀目录会被扫描器当作系统目录跳过。
- 每个删除任务结束写 manifest `<var>/trash/<job_id>.json`，记录原路径/回收站路径/大小/组/保留项：
  - 整组恢复：原位置已存在则跳过（绝不覆盖），逐条 rename 回原路径；
  - 永久清理：需 `confirm` + “永久删除”短语，只删除 manifest 记录且位于回收站目录下的文件。
- 计划时快照 `recycle_mode`；执行期间不跟随设置变化。

## 16. 通知系统与计划扫描（v0046）

> 详细契约见 [NOTIFICATION_SCHEDULE_CONTRACT.md](NOTIFICATION_SCHEDULE_CONTRACT.md)。

### 16.1 通知（`notifications.py`）

- `NotificationService`：设置驱动（`notify_enabled`/`notify_channels`/`notify_events`），
  发送走后台单线程队列（200 条上限），不阻塞扫描/删除/字幕线程；队列满丢弃并审计。
- 渠道：DSM `synodsmnotify @administrators`（不存在则自动跳过）、Webhook（POST JSON，含
  event/title/message/level/timestamp/version/detail）、Telegram Bot API。
- 触发点：扫描完成/失败（`ScanManager._audit_finish`）、删除任务结束的释放空间/失败阈值
  （`DeletionManager._run_job`）、字幕批次结束/批量失败（`SchedulerService` watcher 轮询
  `batch_group_overview`，按 group_id 去重持久化到 `<var>/notify_state.json`）。
- 安全：token 只从设置读取；日志/审计/响应只记录 event/channel/level/error，Webhook 只留
  scheme+host+path；`public_settings` 对 token 置空并给 `*_set` 标记。

### 16.2 计划扫描（`scheduler.py`）

- 规则：`daily HH:MM` / `weekly mon,wed,fri HH:MM` / `interval MIN`（5..10080），设置保存时校验。
- 线程每 20 秒 tick：按规则 `last_run`（持久化 `<var>/scheduler_state.json`）计算下一次；
  到点后依次检查“是否已有扫描/负载阈值/可用 roots”，任何一项不满足都跳过本次并审计
  （`scheduled_scan_skipped`），不补跑；满足则 `ScanManager.start_scan(..., user="scheduler")`。
- roots 为空时复用最近一条 completed 扫描的 `config.roots`；同一规则两次触发至少间隔 300s。
- `POST /api/scheduler/run-now` 复用同一套检查，便于用户手动验证。

### 16.3 前端

- 设置页「自动化 · 计划扫描与通知」分组：规则/目录/负载阈值 + 渠道/事件/阈值/Webhook/Telegram，
  以及逐渠道测试按钮；状态（下次执行/上次执行/当前负载、渠道可用性）在打开设置与保存后刷新。

## 17. 相似视频识别增强（v0047）

> 详细契约见 [VIDEO_MATCHING_V2_CONTRACT.md](VIDEO_MATCHING_V2_CONTRACT.md)。

- 帧指纹 v2：每帧同时产出“全帧 pHash”和“内容裁剪 pHash”（按行/列亮度均值检测 bbox 裁掉黑边后
  放大回 32×32），`VIDEO_ALGO_VERSION=video-v2` 使旧缓存一次性失效。
- 配对打分增强（`pair_score_enhanced`）：全帧/裁剪/交叉 DTW + ±3 帧时间轴偏移对齐，取最高分；
  可选 ASR 文本签名与音频包络签名加权（合计权重 ≤0.5）。
- 音频签名（可选、默认关）：`ffmpeg -f s16le` 解 180s PCM → 120 桶 RMS 包络 → 零均值互相关；
  失败 open，缓存独立 key，不写日志/结果（只写分数）。
- 置信度分档 `confidence_band`：≥95 `same_source`、≥85 `likely_same`、其余 `possible`；
  组级取最低配对分数（保守），结果页以徽标展示，并显示匹配方法/是否裁剪/音频分。
- 分组算法（安全团：任意两两分数 ≥ 阈值）与删除安全校验不变；`video_low_confidence` 等原字段保留。

## 18. 字幕 ASR 模块优化（v0048）

> 详细契约见 [SUBTITLE_ASR_V2_CONTRACT.md](SUBTITLE_ASR_V2_CONTRACT.md)。

- **失败分类**：`failure_category(code, message)` 映射到 audio_extract/no_speech/timeout/
  unsupported_stream/permission/network/provider/internal/unknown；`_handle_failure` 统一处理。
- **自动重试**：可恢复分类且 `attempt < subtitle_auto_retry_max` 时回 `queued`（保留分片缓存），
  审计 `subtitle_retry_auto`；no_speech 标记 skipped 不重试，避免无效排队。
- **失败清单**：`failures()` / `failures_export_rows()` / `retry_failures()`（支持 force），
  任务中心「失败任务」面板 + CSV 导出。
- **资源管控**：`subtitle_max_load` > 0 时 worker 取任务前检查 loadavg，超标保留队列
  （`load_guard` 状态随 counts/queue_snapshot 返回）。
- **优先级**：任务 `priority`，队列按“优先级降序 + 创建时间”插入；`set_priority(top)`：
  `subtitle_dir_priorities` 在提交时计算目录优先级。
- **ETA**：最近 20 个非跳过任务实际耗时中位数校准；执行中任务按 `elapsed*(100-progress)/progress`
  估算剩余，再叠加排队任务。
- **音轨选择**：任务 `audio_track`/`audio_track_language` → `probe_media` 解析为 `0:a:<n>`，
  抽音频（NAS mp3 / 服务器 remux / 预取）统一带 `-map`；`audio_tracks()` 供 UI 查询。
- **目录监控**（新 `subtitle_watch.py`）：30 秒 tick + 配置间隔扫描监控目录，按
  `(size, mtime_ns)` 去重（`<var>/subtitle_watch_state.json`），每轮最多入队 20 个，
  以 `user="watcher"` 提交批量任务，翻译开关用 `subtitle_translate_enabled`。

## 19. 任务中心 / 权限体检 / 日志增强（v0049）

> 详细契约见 [TASK_CENTER_V2_CONTRACT.md](TASK_CENTER_V2_CONTRACT.md)。

- **任务检索**：`SubtitleService._matches_filters` 支持 q（路径/任务ID/消息）、suffix、directory
  （path_key 前缀）、category（失败分类），与 status/group_id/batch_id 组合；前端在有筛选条件时
  走 `/api/subtitles?limit=200`，否则沿用队列视图。
- **批量操作**：`bulk_action(user, job_ids, action)` 支持 cancel/pause/resume/retry/priority，
  逐条结果、最多 200 个、审计 `subtitle_bulk_action`。
- **权限体检**（新 `permissions.py`）：`PermissionInspector.report()` 收集设置路径 + 最近扫描
  roots + 共享根，检查存在/目录/可读/可写（`os.access`，即套件用户实际权限），汇总扫描日志权限
  关键字与字幕 permission 失败，输出 `ok/warn/error` 与 DSM 操作指引；**不自动提权**。
- **日志增强**：`/api/logs/search` 遍历日志文件（单文件 512KB、最多 500 个）按文件名/内容匹配
  并同时过滤审计；`/api/logs/export` 生成可读文本；前端日志页新增检索与导出。
- 任务行/详情已有错误信息与失败分类（v0041/v0048），本版补检索与批量操作入口。







