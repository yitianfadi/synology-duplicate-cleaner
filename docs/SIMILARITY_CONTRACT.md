# 内容相似度匹配扩展契约（v1.1，binding）

> 在原有“哈希精准模式”之上新增**内容相似度模式**。原精准模式的行为、API、安全约束**全部保持不变**。
> 本文件是 backend/frontend 并行实现的接口与行为约定；与 `docs/CONTRACT.md` 冲突时，精确模式以原契约优先，
> 相似模式以本文件为准。

## 1. 产品行为

- 扫描模式由用户选择：
  - `exact`（默认）：只做 SHA256/MD5 全量哈希精准判重，结果全部为 `match_type="exact"`。
  - `similar`：先执行原精准哈希流程；再对允许的类型做内容特征提取与相似检索。
    - 哈希完全一致的组仍归入 **精准组**（哈希一致），不重复出现在相似组。
    - 非字节一致、但相似度达到阈值的组归入 **相似组**（内容相似）。
    - 两者在结果页**分区展示**。
- 相似组保留策略与精准组一致：组内按 `keep_mode`（默认最晚创建）保留一份，其余待处理。
- 相似组**没有默认勾选**；必须人工逐条选择，禁止一键批量删除相似文件。
- 相似组可进入删除计划，但必须满足：
  - 明确提供 `file_ids`（不能通过 `scope=all_duplicates` 自动包含相似组）；
  - 每个涉及的相似组都在 `reviewed_similar_group_ids` 中；
  - 请求带 `confirm_similar=true`。
- 精准组仍支持原有的 `scope=all_duplicates` 和默认勾选行为。

## 2. 设置字段（settings / scan options）

新增键与默认值：

| key | 类型 | 默认 | 约束 |
|---|---|---|---|
| scan_mode | "exact"\|"similar" | "exact" | 扫描模式 |
| similarity_threshold | int | 85 | 0..100，达到阈值才算相似 |
| similarity_extensions | string[] | 见下 | 允许相似计算的扩展名白名单；空数组 = 使用内置默认列表 |
| similarity_exclude_extensions | string[] | [] | 显式关闭的后缀（优先级高于白名单） |
| similarity_concurrency | int | 2 | 1..4 |
| similarity_cache | bool | true | 持久化特征缓存 |
| similarity_max_file_mb | int | 32 | 1..1024；超过则跳过相似计算 |
| similarity_allow_binary | bool | false | 默认跳过二进制/压缩包/媒体；仅高级用户可开 |

默认 `similarity_extensions`：

```text
.txt .md .markdown .csv .json .xml .html .htm .log
.pdf .docx .odt
.png .jpg .jpeg .webp
```

无论白名单如何，以下类别**默认**跳过相似计算（只参与哈希精准匹配），`similarity_allow_binary=true` 时也不改变安全策略：
压缩包、磁盘镜像、可执行文件、音视频大文件。后端维护硬编码 `SIMILARITY_BINARY_EXTENSIONS`
（如 .zip .7z .rar .tar .gz .bz2 .xz .iso .exe .dll .so .dmg .msi .mp4 .mkv .avi .mov .mp3 .flac 等）。
超过 `similarity_max_file_mb` 的文件也跳过相似计算；`similarity_allow_binary` 只对“其他未知二进制”生效，不用于上述类别。

设置校验：未知键返回 `INVALID_ARGUMENT`；扩展名与原有规则一致（以 `.` 开头，长度 2..16）。

## 3. 相似特征算法（后端实现最低要求）

纯 Python 3.8 标准库，不新增第三方运行依赖；可选 Pillow 只用于解 JPEG（缺失时降级，不报错）。

### 3.1 特征类型

| kind | 类型 | 特征 |
|---|---|---|
| text | .txt/.md/.csv/.json/.xml/.html/.log 等文本 | 规范化文本后的字符 5-gram MinHash 签名（默认 64 个 32-bit 值） |
| document | .pdf（文本层）、.docx/.odt（zip XML 文本） | 提取文本后同上 MinHash |
| image | .png/.jpg/.jpeg/.webp | 感知哈希：PNG 用纯 stdlib 解码 + dHash；JPEG 优先 Pillow，无 Pillow 时降级为字节 MinHash 并标记 `low_confidence=true` |
| bytes | 其他（仅 allow_binary） | 字节 1 KiB 分片 5-gram MinHash |

- 文本不足（如扫描版 PDF 无文本层）→ 跳过，计入 `similarity_skipped.no_text`；不能误当空文本与所有文本相似。
- 图片解码失败 → 跳过或降级 bytes；计入 `similarity_skipped.decode`。
- MinHash 使用确定性哈希（不得用 Python 内置 hash 的随机盐），同内容在不同进程/扫描结果一致。
- 相似度：
  - MinHash → Jaccard 估计 `equal_positions / signature_size`。
  - image → `1 - hamming(pHash, pHash) / 64`。
- 阈值比较用整数百分比：`score = round(similarity * 100)`，`score >= threshold` 才进入相似候选。
- 必须计算并保存每个相似文件的 `content_sha256`，用于删除前内容未变校验。

### 3.2 候选与分组

- 先完成精准哈希分组；精准组内所有文件**不参与**相似组，避免重复。
- 对剩余允许类型文件提取特征。
- LSH 候选生成：
  - MinHash：16 bands × 4 rows（64 签名）。
  - 图像 pHash：按 8 位分带，至少 4 带；候选后计算 Hamming。
  - 候选对再精确计算相似度；低于阈值丢弃。
- 分组使用“安全团”策略：组内任意两份文件相似度都必须 ≥ 阈值；贪心按相似度降序合并，若新文件与组内任一成员低于阈值则不加入。这样保留项与每个副本都满足阈值。
- 每组至少 2 个文件。
- 相似组不能用精准组的哈希作为 group.hash；`hash=""`、`algo=""`。

### 3.3 缓存与并发

- 特征缓存路径：`<data>/feature_cache.json`（原子写，最大 50,000 条，超出按 LRU/最旧淘汰）。
- 缓存 key：`v1|size|mtime_ns|inode|dev|feature_type`；值包含 feature、content_sha256、kind、low_confidence。
- 同一扫描中，内容 SHA256 相同的文件复用特征（避免重复计算）；跨扫描按缓存 key 命中。
- 文件被修改后 key 变化即失效；缓存内容只用于加速，不参与安全判定（删除仍重校验）。
- 特征提取并发 `similarity_concurrency`（1..4），复用现有全局限速器（`hash_rate_mbps`）；每处理一个文件检查 pause/stop，支持中断。
- 超过 `similarity_max_file_mb`、二进制/压缩包/媒体、类型不在白名单、无文本、解码失败都计入统计，不报整体失败。

### 3.4 进度与阶段

- 新增阶段 `similarity`（前端映射为“内容相似度分析”）。
- 进度：精准哈希结束约 85%，相似特征提取/检索 85%→97%，最终整理 97%→100%。
- 统计新增：
```json
{
 "exact_groups": 0, "exact_files": 0,
 "similar_groups": 0, "similar_files": 0,
 "similar_reclaimable_bytes": 0,
 "similarity_candidates": 0, "similarity_features": 0,
 "similarity_cache_hits": 0, "similarity_cache_misses": 0,
 "similarity_pairs_checked": 0,
 "similarity_skipped": {"type":0,"size":0,"binary":0,"decode":0,"no_text":0,"error":0}
}
```
- 原有 `groups/duplicate_groups/duplicate_files` 语义变为“精准组+相似组”的总数；另给分类字段。
  原有 `reclaimable_bytes` 仍只统计精准安全组；`selectable_*` 仍只统计精准组。
- 扫描完成后写 `scan-<id>.log` 时包含模式、阈值、相似组数、缓存命中。

## 4. 结果组 schema 扩展

精准组新增字段：
```json
{"match_type":"exact","similarity_score":100}
```

相似组：
```json
{
 "group_id":"s-000001",
 "match_type":"similar",
 "similarity_score":91,
 "similarity_method":"minhash|phash|bytes",
 "low_confidence":false,
 "hash":"", "algo":"",
 "size":12345,
 "count":3,
 "wasted_bytes":24690,
 "keep_reason":"latest_created",
 "needs_manual_keep":false,
 "creation_time_suspect":false,
 "manual_review_required":true,
 "files":[
   {
    "file_id":"sf-000001",
    "match_type":"similar",
    "similarity_score":91,
    "similarity_method":"minhash",
    "low_confidence":false,
    "path":"...","name":"...","size":12345,
    "hash":"<content sha256>",
    "created_at":"...","created_ts":...,"created_ns":...,
    "created_source":"statx","created_unsafe":false,
    "modified_at":"...","mtime_ns":...,"inode":...,"dev":...,
    "is_keep":true,"default_selected":false
   }
 ]
}
```

- 相似组 `similarity_score` = 非保留文件相对保留文件的平均分（四舍五入）。
- 每个非保留文件的 `similarity_score` 是“该文件 vs 保留文件”的分数；保留文件为 100。
- 相似组所有文件 `default_selected=false`；`manual_review_required=true`。
- 相似组若 `needs_manual_keep=true`，仍整组禁止删除（原有规则）。
- 精准组文件新增 `match_type="exact"`、`similarity_score=100`，其余字段不变。

### 4.1 groups 接口

`GET /api/scans/{id}/groups?offset=0&limit=100&q=&sort=wasted_desc&match_type=exact|similar`

- `match_type` 可选；缺省返回全部组（兼容旧前端/旧测试）。
- 响应新增：
```json
{"ok":true,"total":12,"offset":0,"limit":100,
 "match_type":"similar","exact_total":7,"similar_total":5,
 "groups":[...]}
```
- 新增排序：`similarity_desc`（仅相似组有意义；精准组按 100 处理）。
- 搜索仍匹配路径/文件名/哈希；相似组兼容 hash 空字符串。

### 4.2 CSV

CSV 列在原有基础上增加 **匹配类型** 与 **相似度分值**：

```text
组号,匹配类型,状态,文件路径,大小(字节),SHA256/MD5,创建时间,修改时间,保留,默认勾选,时间来源,相似度分值
```

- 匹配类型：`哈希一致` / `内容相似`。
- 相似度分值：整数 0..100；精准组为 100。
- 其余列顺序不变。

## 5. 删除计划/执行扩展

### 5.1 delete-plan 请求

精准组：保持原行为。

相似组：
```json
{
 "file_ids":["sf-000002","sf-000003"],
 "recycle":true,
 "confirm_similar":true,
 "reviewed_similar_group_ids":["s-000001"]
}
```

- `scope=all_duplicates` **永不包含相似组**（直接跳过，不报错；相似组必须显式选择）。
- 任何相似文件候选缺少 `confirm_similar=true` 或所属组不在 reviewed 列表 → 整个请求返回 400 `SIMILAR_REVIEW_REQUIRED`，不生成计划。
- `needs_manual_keep=true` 的相似组仍按原规则跳过。
- 精准文件 ID 与相似文件 ID 可同时提交，但两类分别适用上述规则。

### 5.2 删除计划响应新增

```json
{
 "similar_count":2, "similar_bytes":24690,
 "exact_count":0, "exact_bytes":0,
 "manual_review":true,
 "reviewed_similar_group_ids":["s-000001"],
 "files":[
   {"file_id":"sf-000002","path":"...","size":12345,"group_id":"s-000001",
    "match_type":"similar","similarity_score":91}
 ]
}
```

### 5.3 删除前重校验

- 所有文件仍校验存在、普通文件、路径安全、非保留、size/mtime_ns/inode/dev 未变化。
- **精准组**：候选 fresh SHA256 必须等于扫描记录 hash，且保留文件 fresh hash 等于候选 hash（原规则）。
- **相似组**：候选与保留文件的 fresh SHA256 分别等于各自扫描记录 hash（证明内容未变）；
  **不要求二者哈希相等**；相似关系由扫描结果与人工复核保证。
- 删除执行仍为回收站 `rename` 或永久 `unlink`；逐文件记录。
- 删除计划审计新增 `match_type` 统计与相似组 review 信息。

## 6. 前端要求

### 6.1 扫描模式与选项

- 扫描页顶部增加模式选择（分段按钮/单选）：
  - `精准哈希（默认）`
  - `内容相似（哈希 + 相似）`
- 精准模式隐藏相似选项；相似模式展示：
  - 相似度阈值（0–100，默认 85，滑块 + 数字输入）；
  - 允许计算的文件类型：多选 chips/checkbox（来自后端默认列表；可关闭如 pdf/txt/jpg）；
  - 相似并发（1–4，默认 2）；
  - 特征缓存开关（默认开）；
  - 单文件大小上限（MB，默认 32）；
  - 高级：允许未知二进制（默认关，警示 CPU）；
  - 说明：zip/7z/媒体/可执行文件/超限文件只做精准哈希；相似计算可暂停/停止。
- 扫描进度增加“内容相似度分析”阶段；展示相似特征/缓存命中/跳过计数。
- 模式发送到 `POST /api/scans` 的 `options.scan_mode` 和新增字段。

### 6.2 结果页分区

- 结果页在模式 B 下显示两个独立区块：
  1. **哈希一致（精准重复）**：沿用原有 UI、默认勾选、全选副本、删除。
  2. **内容相似**：
     - 顶部固定黄色/红色警示：“内容相似度为 AI/特征比对结果，存在误匹配概率；相似组必须人工复核，禁止一键批量删除相似文件。”
     - 组卡片标记“内容相似 · 91%”，不显示哈希值（或显示“—/字节哈希”）。
     - 组内文件显示相对保留文件的相似度分值、时间来源；保留项高亮。
     - **不允许组级“全选副本”**；全选待删除副本（全局）只作用于精准组。
     - 每组提供“人工复核”checkbox；未勾选前组内文件 checkbox 禁用；勾选后仅允许用户逐条勾选（不提供全选）。
     - 相似组删除按钮默认置灰；只有至少一条被手动勾选且该组已人工复核才启用；全局删除按钮也需满足所有已选相似组均已复核。
     - 精准模式不显示相似区块（或显示空态）。
- 摘要卡片增加：精准组数/相似组数、精准可释放/相似潜在可释放；避免把两者混为一个数。
- 历史记录显示扫描模式；查看旧扫描时按结果中的 match_type 分区。
- CSV 链接直接下载新列。

### 6.3 删除两步确认

- 相似组与精准组可混合，但计划摘要必须分别显示：
  - 精准文件数/字节；
  - 相似文件数/字节；
  - “相似组已人工复核”的组数。
- 最终确认仍需 `confirm:"DELETE"`；永久删除仍输入“永久删除”。
- 提交 delete-plan 时，若包含相似文件，必须带 `confirm_similar:true` 和 `reviewed_similar_group_ids`（只包含实际选中且已复核的相似组）。
- 相似组禁止通过“全选待删除副本”进入计划。

## 7. 向后兼容与测试

- 不传 `scan_mode` 的旧请求 = `exact`，行为与 v1 完全一致；旧测试必须继续通过。
- 旧结果 JSON 无 `match_type` 时，读取层默认视为 exact（用于历史扫描/升级）。
- 精准组的 `hash`、CSV 原列、删除计划原字段不变；新增字段只增不改。
- 必须有测试：
  * 文本相似（内容改写但 5-gram 相似度 ≥ 阈值）成组，低于阈值不成组；
  * 精准组不进入相似组；相似组默认不勾选、manual_review_required；
  * 相似组 keep latest、default_selected=false；
  * scope=all_duplicates 跳过相似组；无 review 的相似 file_ids → 400 SIMILAR_REVIEW_REQUIRED；
  * 带 review 的相似删除计划可生成并通过删除；keeper 删除仍被拒；
  * CSV 新列与匹配类型；
  * 中断相似阶段可停止；缓存命中计数增加；
  * 二进制/压缩包/媒体不进相似组，但精准哈希仍可匹配；
  * groups 接口 match_type 过滤与 exact_total/similar_total。

## 8. 非目标

- 不做云端 AI API、不上传文件、不依赖外部模型服务。
- 不保证语义等价；相似度是内容特征近似，必须人工复核。
- 不自动删除相似组，不提供“一键删除全部相似文件”。
- 不用相似结果替代精准哈希结论；精准组仍是最高置信度。
