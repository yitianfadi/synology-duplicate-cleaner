# 增量扫描 / 保留规则链 / 排除规则契约（v0043）

> 版本：`1.1.0-0043` · SQLite schema **v8** · 后端 Python 3.8+ 仅标准库
> 关联文档：[ARCHITECTURE.md](ARCHITECTURE.md) §13、[CONTRACT.md](CONTRACT.md) §6/§7/§8、
> [STORAGE_CONTRACT.md](STORAGE_CONTRACT.md) §1、[REQUIREMENTS_V2_ASSESSMENT.md](REQUIREMENTS_V2_ASSESSMENT.md) §2.1–2.3/2.8

## 0. 一句话结论

- **增量扫描 ≠ 跳过未变化的文件不做比对**。枚举仍然会 `stat` 全部文件（否则无法发现新增/删除/重命名，
  也无法发现“新文件与旧文件重复”这种最主要场景）；增量体现在**哈希复用**：`(dev, inode)` +
  `size` + `mtime_ns` 都没变且算法/采样配置一致时，直接复用上次的 `quick_hash`/`full_hash`，
  **不读文件内容**。第二次扫描的 `bytes_hashed` 接近 0。
- 结果里给每个文件/组标出“本次新增 / 内容变化”，结果页可切换“只看本次新增 / 变化”。
- 保留策略从单选升级为**规则链**（体积、路径优先级、文件名优先级、内嵌字幕/音轨、修改/访问时间…）。
- 排除规则支持**文件名正则、通配符、`.ignore`（gitignore 常用子集）**，并把“与视频同名的附属文件”
  （外挂字幕/海报/nfo）默认排除在判重之外。

## 1. 数据模型（schema v8）

### 1.1 `hash_cache`

| 列 | 类型 | 说明 |
|---|---|---|
| dev, inode | INTEGER | 主键；文件系统身份（Windows 的 64 位无符号 inode 按补码存为有符号，读写两侧同一映射） |
| path | TEXT | 最近一次见到的路径（重命名后更新） |
| size, mtime_ns | INTEGER | 内容身份校验；两者都一致才认为“内容未变” |
| quick_hash | TEXT | 快速指纹（v2 采样） |
| full_hash | TEXT | 全量哈希 |
| algo | TEXT | `sha256` / `md5` |
| sample_kb, sample_version | INTEGER | 快速指纹采样配置与算法版本（不一致只重算 quick_hash） |
| first_scan_id, last_scan_id | TEXT | 首次/最近见到该文件的扫描 |
| first_seen_epoch, last_seen_epoch, last_used | REAL | LRU |

- 上限 `HASH_CACHE_MAX_ENTRIES = 300000`，写入后按 `last_used` 裁剪；超限后旧条目被淘汰（只影响下次速度）。
- 只缓存**候选文件**（进入“大小重复组”的文件）；非候选文件不写，避免把照片库里几万个唯一大小文件塞进缓存。

### 1.2 `media_probe_cache`

保留规则 `has_subtitle_stream` / `has_audio_streams` 需要 FFmpeg 探测媒体流；结果按
`(dev, inode)` + `size`/`mtime_ns` 缓存：`payload = {audio_stream_count, subtitle_stream_count,
audio_langs, subtitle_langs, duration}`。上限 `MEDIA_PROBE_CACHE_MAX_ENTRIES = 50000`。
探测只对音视频后缀执行，失败/超时一律计 0，不阻塞分组，也不影响删除安全。

### 1.3 `scan_groups` 新增列

`new_files` / `changed_files`（组内本次新增/内容变化的文件数），用于 `new_only` 筛选与徽标；
历史组回填 0。

## 2. 扫描流程变化

```
enumerate（stat 全部文件；应用排除规则/附属文件过滤）
  → size（大小分组；**对候选文件批量查 hash_cache**，填充 quick_hash/full_hash 与 new/changed/same）
  → sample（已有 quick_hash 直接复用；否则按 v2 三段采样计算）
  → hash（已有 full_hash 直接复用；否则线程池 + 限速全量哈希）
  → **写回 hash_cache**（仅候选文件，UPSERT）
  → similarity / video / finalize（组级 new_files/changed_files）
```

- 统计（`stats`）：`new_files`、`changed_files`、`unchanged_files`、`hash_cache_hits`、
  `hash_cache_misses`、`bytes_hashed_reused`、`new_groups`、`media_probe_hits`、
  `keep_rule_hits`（规则名 → 组数）、`exclude_rule_errors`。
- 上述计数只覆盖**候选文件**（有判重意义的文件），不是全盘文件数。
- 关闭 `incremental_scan` 时行为与 v0042 完全一致（每次全量重算）。

### 2.1 正确性与安全边界

- 复用条件：`(dev, inode)` 命中、`size` 与 `mtime_ns` 都相等、`algo` 相同；
  `quick_hash` 还要求 `sample_kb` 与 `sample_version` 相同。
- “内容被原地修改但 `size` 与 `mtime_ns` 都不变”的极端情况无法感知——这与 rsync 等工具同一假设；
  **删除前的实时重校验（size/mtime_ns/inode/dev + 全量哈希）不受影响**，不会因此误删。
- 升级影响：`quick_sample_version` 从 1 变 2 时，旧缓存条目只复用 `full_hash`，`quick_hash` 重算；
  暂停中恢复的旧断点可能带 v1 指纹，最坏结果是候选集被拆开（漏判，不会误删），下次全量扫描自愈。

## 3. 增量标记语义

| 字段 | 含义 |
|---|---|
| `new_since_last_scan` | 该 `(dev, inode)` 首次进入哈希缓存（上次扫描没有这个文件） |
| `changed_since_last_scan` | 缓存命中同 inode，但 `size`/`mtime_ns` 变化（内容可能已改） |
| `hash_cached` / `quick_hash_cached` | 对应哈希来自缓存（本次未读盘/未现算） |
| `first_scan_id` | 首次见到该文件的扫描 ID |

- `GET /api/scans/{id}/groups?new_only=1` 只返回“组内存在新增/变化文件”的组；响应带
  `new_only`、`new_total`。
- 前端：扫描选项与结果页各有一个“只看本次新增 / 变化”入口；组卡片显示 `本次增量 · 新增 N / 变化 M`。

## 4. 保留规则链（`keep_rules`）

- 设置键 `keep_rules`（string[]，默认 `[]` = 沿用 `keep_mode`/`pinned_path` 历史语义）。
- 规则按数组顺序逐条比较候选集：**某条规则分出唯一胜者即停止**；平局则候选集收窄后继续下一条；
  规则耗尽仍平局 → `needs_manual_keep=true`（该组禁止自动删除）。
- 允许规则：

| 规则 | 参数 | 语义 |
|---|---|---|
| created_latest / created_earliest | — | 比较 btime；**任一组员 btime 不可信时整组改用 mtime**（沿用历史安全降级） |
| mtime_latest / mtime_earliest | — | 始终比较 mtime |
| atime_latest / atime_earliest | — | 比较 atime（DSM 默认 relatime，**不可靠，仅参考**） |
| size_largest / size_smallest | — | 比较文件大小；**精准重复组内大小必然相同**，该规则只对相似/视频组有意义 |
| path_priority | 目录/路径前缀 | 命中该前缀（含其子目录）的文件优先保留；多条按顺序 |
| filename_priority | 通配符，或 `/正则/` | 对 basename 命中者优先保留（不区分大小写） |
| has_subtitle_stream | — | 带内嵌字幕轨的优先 |
| has_audio_streams | — | 内嵌音轨数多者优先 |

- 结果字段：`keep_reason`（规则结果，新增 `size_largest`/`path_priority`/`filename_priority`/
  `has_subtitle_stream`/`has_audio_streams`/`latest_modified`…）、`keep_time_source`
  （`btime`/`mtime`/`atime`/`rule`）、`time_fallback`（仅在 created_* 因 btime 不可信而降级时为 true）。
- 校验：规则名必须合法；`path_priority`/`filename_priority` 必须有参数；其它规则不接受参数；
  `/正则/` 必须能编译；单条 ≤300 字符、最多 40 条；非法返回 400 `INVALID_ARGUMENT`。

## 5. 排除规则

### 5.1 文件名（正则 / 通配符）

- `exclude_name_regex`：对 **basename** 做 `re.search(..., IGNORECASE)`；≤50 条、单条 ≤200 字符；
  非法正则 400（设置保存时即校验）。
- `exclude_name_glob`：对 basename 做 `fnmatch`（不区分大小写）；≤50 条。
- 文件与目录都适用；目录命中即整棵跳过；统计进 `skipped.rule`。

### 5.2 `.ignore`（gitignore 常用子集）

- 每个目录若有 `.ignore` 则在进入该目录时读取（≤1000 行）；规则带 base_dir 作用域：
  父目录规则对子目录生效，子目录规则不影响父目录，越界路径不命中。
- 支持：`#` 注释、空行、行尾空格、`!` 否定（按加载顺序最后命中者胜）、前导 `/` 锚定、
  结尾 `/` 只匹配目录、含 `/` 的相对路径匹配、不含 `/` 的任意层级 basename、`*`/`?`；
  `**` 折叠为 `*`（**不做完整 gitignore 语义**）。
- 解析失败/读不到只记录告警（写扫描日志），不中断扫描；`ignore_file_enabled=false` 时完全不读。

### 5.3 影视附属文件（`media_sidecar_mode`）

- `skip`（默认）：若某目录存在视频文件 `X.<视频后缀>`，则**只跳过**同目录下与该视频同源的附属文件：
  - 同名不同后缀：`X.srt`、`X.ass`、`X.nfo`、`X.bif`…；带语言/版本段：`X.zh.srt`、`X.en.ass`；
  - 前缀同源：`X-poster.jpg`、`X-poster.png`、`X-fanart.jpg`；
  - 常见固定名：`poster.*`、`folder.*`、`fanart.*`、`backdrop.*`、`banner.*`、`logo.png`、
    `clearlogo.png`、`landscape.*`、`thumb.*`、`movie.nfo`、`tvshow.nfo`、`season.nfo`。
- **不按后缀无差别跳过**：照片库里没有同名视频的 `jpg/png` 仍然正常参与判重。
- 统计进 `skipped.sidecar` 与 `stats.sidecar_files`；`include` 时不过滤。

## 6. 快速指纹 v2（`QUICK_SAMPLE_VERSION = 2`）

- 小文件：仍为 `size + 头 N KB + 尾 N KB` 的 SHA-256（N = `quick_sample_kb`）。
- 大文件（`size >= LARGE_FILE_SAMPLE_THRESHOLD_BYTES = 256 MiB`）：额外读**中间段** N KB，
  降低“只替换中段”的漏检概率。
- 摘要输入自描述：`b"dcq2" + struct.pack("<Q", size) + b"h"+head + [b"m"+mid] + b"t"+tail`。
- 仍只用于剪枝；最终判重必须全量哈希（不变）。

## 7. 设置默认值

| key | 默认 | 说明 |
|---|---|---|
| incremental_scan | true | 跨扫描哈希缓存 |
| keep_rules | [] | 保留规则链（空=历史语义） |
| exclude_name_regex | [] | 文件名正则排除 |
| exclude_name_glob | [] | 文件名通配符排除 |
| ignore_file_enabled | true | 读取 `.ignore` |
| media_sidecar_mode | "skip" | 影视附属文件不参与判重 |

## 8. 接口

- `GET /api/meta` → 新增 `scan` 段：`keep_rules`（名称/中文标签/是否需要参数）、`keep_rules_max`、
  `sidecar_modes`、`exclude_pattern_max_count/len`、`quick_sample_version`、
  `large_file_sample_threshold_bytes`、`hash_cache_max_entries`、`ignore_file_name`。
- `GET /api/scans/{id}/groups?new_only=1`、`/stats`：见 [CONTRACT.md](CONTRACT.md) §7。
- `GET /api/cache`（管理员）、`POST /api/cache/clear`（管理员+CSRF+`confirm=true`）：清理缓存。
- 扫描任务的 `options`/`config` 快照包含上述设置键，便于历史回看。

## 9. 兼容性

- SQLite v7 → v8：只新建 `hash_cache`、`media_probe_cache` 两张表 + `scan_groups` 加两列，**不重写历史数据**；
  升级后第一次扫描全量重算（缓存为空），之后逐步收益。
- 旧扫描结果可正常查看；`new_files`/`changed_files` 为 0、`new_only=1` 返回空（属预期）。
- 关闭 `incremental_scan` 或清空缓存后，行为与 v0042 等价。
- 保留规则链为空时，`keep_mode`/`pinned_path` 的所有历史语义（含 btime 不可信时的 mtime 回退、
  pinned 未命中回退、平局转人工）逐字保持不变。

## 10. 验收（unittest + 真机）

- `tests/test_v0043_incremental.py`（25 例）：缓存读写/大 inode/裁剪/清空、第二次扫描 `bytes_hashed=0`、
  新增与变化标记、`incremental_scan=false`、`new_only` 过滤、规则链各规则与平局、
  正则/通配符/`.ignore`/附属文件过滤、设置校验、`/api/cache` 接口。
- 真机（NAS 真实媒体库，见 `qa/REPORT.md` §25）：第二次扫描耗时与 `bytes_hashed` 对比；
  `.ignore`/附属文件是否让结果更干净；规则链是否按预期选保留项。
