# 视频视觉相似度扩展契约（v1.2，binding）

> 在现有精准/图片文档相似模式上新增 **视频视觉相似**，独立分区。用户已确认：FFmpeg 打进 SPK；视频相似组默认不勾选但支持一键全选批量删除。

## 1. 配置

新增设置（全部持久化、白名单校验）：

| key | 默认 | 约束 |
|---|---|---|
| video_similarity_enabled | false | 总开关；默认关闭 |
| video_extensions | [".mp4",".mkv",".mov",".avi",".flv",".wmv",".webm",".m4v",".ts",".mpg",".mpeg"] | 允许视频相似的后缀 |
| video_sample_frames | 20 | 5..100 |
| video_max_sample_interval_sec | 300 | 60..600 |
| video_similarity_threshold | 90 | 0..100 |
| video_concurrency | 1 | 1..2 |
| video_hwaccel | "auto" | auto/off |
| video_max_file_mb | 8192 | 1..102400 |
| video_timeout_sec | 180 | 30..1800 |
| video_cache | true | 布尔 |

ASR 语音辅助不进入本版本实现（无内置模型/不联网）；文档和 UI 必须明确“未实现、默认关闭”，不得假装可用。

## 2. FFmpeg

- 由 SPK 自带 Linux 静态 FFmpeg（x86_64 + aarch64），启动脚本按 `uname -m` 导出 `DUP_FFMPEG_PATH`。
- 后端优先使用 `DUP_FFMPEG_PATH`；开发模式可回退 PATH 中的 `ffmpeg`；可配置外部路径（企业可换带硬解的 ffmpeg）。
- 不依赖 ffprobe：用 `ffmpeg -i` stderr 的 `Duration:` 解析时长；解析失败则跳过并记录。
- 所有调用必须：`-nostdin -hide_banner -loglevel error`、参数数组、无 shell；单次超时；停止扫描时 kill 子进程。
- 硬件解码：
  - 检测 `ffmpeg -hwaccels`；auto 时尝试 `-hwaccel auto`；
  - 失败自动回退软件解码；
  - 每个视频记录实际 hwaccel（none/vaapi/qsv/nvdec）。
- 后端 `/api/meta` 增加 `video` 能力：enabled, ffmpeg_path, ffmpeg_version, hwaccels, gpu_hint, default settings。

## 3. 动态采样与指纹

- 用时长 D、配置帧数 N（5..100）、最大间隔 I（默认300s）：
  `n_eff = min(100, max(N, ceil(D / I)))`；若 `D / n_eff > I`，记录 warning（超长视频无法完全满足间隔）。
- 采样时间点 `t_i = start + (i + 0.5) * D / n_eff`，i=0..n_eff-1。
- 每个时间点用一次 FFmpeg seek 取 1 帧，输出 **32×32 灰度 rawvideo（1024B）**，不落盘：
  `ffmpeg -nostdin -hide_banner -loglevel error -ss t -i file -frames:v 1 -vf "scale=32:32:force_original_aspect_ratio=decrease,pad=32:32:(ow-iw)/2:(oh-ih)/2:black,format=gray" -f rawvideo -pix_fmt gray -`
- 每帧计算 64-bit **pHash**（DCT 8×8）；保存每帧 hex；视频指纹 = 16进制帧序列（或紧凑编码）。
- 短视频、几十秒片段可以与长度相近的视频匹配；**片段 vs 完整长视频不承诺**（固定 N 均匀采样无法覆盖任意短片段；后续可用多尺度/片段检索，v1 明确说明边界）。
- 容忍：转码、分辨率、黑边、轻微调色、小水印、帧率变化。
- 不识别：剧情语义、剪辑乱序、大量替换画面、换脸/重绘。

## 4. 相似度与分组

- 时长比例预筛：0.7..1.4。
- 候选视频用视频指纹初筛，再做 DTW 序列对齐：
  - 帧相似 = 1 - hamming(pHashA, pHashB)/64；
  - DTW 路径平均帧相似度 avg；
  - 覆盖率 coverage = aligned_pairs / min(nA,nB)；
  - score = round(100 * (0.7*avg + 0.3*coverage))；
  - score >= threshold 才成组；要求 coverage >= 0.6（v1 先按长度相近）。
- 安全团分组：组内任意两份 >= threshold；组至少 2 个视频；不把精确重复组视频纳入视频相似组。
- 视频组保留策略：latest/earliest 按创建时间；无可信创建时间整组按 mtime；与 v1.1 一致。
- 组 schema：
```json
{
 "group_id":"v-000001",
 "match_type":"video",
 "similarity_score":92,
 "video_similarity_score":92,
 "video_fingerprint":"v1:20:...",
 "video_sample_frames":20,
 "video_hwaccel":"none",
 "video_duration":7200,
 "video_low_confidence":false,
 "manual_review_required":false,
 "allow_batch_delete":true,
 "keep_reason":"latest_created",
 "needs_manual_keep":false,
 "time_fallback":false,
 "files":[{"file_id":"vf-000001","match_type":"video","similarity_score":92,
           "video_fingerprint":"...","video_sample_frames":20,
           "video_hwaccel":"none","video_duration":7200,
           "default_selected":false,"is_keep":false}]
}
```
- 视频组 **default_selected=false**；支持组级“全选本组视频副本”和全局“全选视频相似组副本”，批量进入删除计划。仍保留两次确认；keeper 不可选。
- 视频组与图片/文档相似组分开：`match_type` 值 exact / similar / video；groups 接口支持 `match_type=video`，返回 `video_total`。
- 统计：video_groups, video_files, video_candidates, video_cache_hits/misses, video_skipped {type,size,decode,no_duration,timeout,error}, video_ffmpeg_used。

## 5. 缓存

- 缓存路径 `<var>/video_cache.sqlite3`（sqlite3 标准库），单表主键：
  `algo_version|size|mtime_ns|inode|dev|sample_frames|max_interval|threshold`（阈值不影响指纹则可省略）。
- 值：duration, sample_frames, interval, frame_hashes, hwaccel, created_at, source_size。
- 文件 identity 变化失效；最多 20000 条 LRU；损坏/无时长结果缓存失败标记，避免重复解码。
- 命中统计入 stats；缓存只加速，不改变删除前重校验。

## 6. 扫描阶段

- 新增 stage `video`（前端显示“视频视觉分析”）。
- 精准哈希结束后执行；视频后缀且启用时进入视频阶段；视频不进入图片/文档相似阶段。
- 独立并发 `video_concurrency`（默认1），每视频一个 ffmpeg 子进程，`-threads 1`，nice/ionice 由启动脚本；超时 kill；检查 stop。
- 进度 0.97→1.0 临近结束；显示当前视频、已完成/总数、缓存命中、跳过。
- 低配提示：CPU/GPU/ffmpeg 能力检测，UI 提示“视频扫描可能很慢，建议少量或关闭”，不强制关闭。

## 7. 删除与导出

- 视频文件删除前仍校验存在、路径、size/mtime_ns/inode/dev、keeper 存在；视频相似不要求候选与 keeper 字节相等。
- 视频组批量：允许 `scope="video_similar"` 或显式 file_ids；计划拆分 video_count/video_bytes；不得包含 exact/similar 组以外的意外对象。
- 视频批量删除仍需两步确认；永久删除仍输入“永久删除”。
- 新增 Excel 导出 `GET /api/scans/{id}/xlsx`：
  - 标准库 zipfile+OOXML，不引入 openpyxl；
  - 列包含现有 CSV 全部列 + 匹配类型（哈希一致/内容相似/视频视觉相似）+ 视频指纹、采样帧数、硬解、视频时长、相似度；
  - 保留 CSV 兼容。
- UI 提供“导出 Excel”按钮和视频组批量删除入口。

## 8. 测试

- 后端测试使用 fake ffmpeg/可执行脚本模拟帧输出，不依赖实际视频；提供 ffmpeg 真实生成样本的可选集成测试。
- 覆盖：动态采样公式、时长解析、pHash、DTW、阈值、缓存、损坏/无时长/超时、停止、分组保留、批次删除、Excel。
- 原有 147 测试必须继续通过。
