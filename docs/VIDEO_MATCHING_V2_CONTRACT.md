# 相似视频识别增强契约（v0047）

> 版本：`1.1.0-0047` · SQLite schema 仍 **v8**（视频指纹缓存为独立 SQLite 文件 `<var>/cache/video.sqlite`）
> 关联：[CONTRACT.md](CONTRACT.md) §6/§7、[ARCHITECTURE.md](ARCHITECTURE.md) §17、
> [REQUIREMENTS_V2_ASSESSMENT.md](REQUIREMENTS_V2_ASSESSMENT.md) §2.4、§3.3

## 0. 一句话

在原有“抽样帧 pHash + DTW”基础上增加三件事，目标是识别**同一源片的不同压制版本**
（不同码率/封装/分辨率/黑边/片头片尾剪切），并给出可解释的**置信度分档**：

1. **边缘裁剪指纹**：自动检测并裁掉四周黑边后再算 pHash，与全帧指纹一起参与比对；
2. **时间轴偏移对齐**：允许 ±3 帧整体位移，覆盖片头/片尾剪切导致的采样错位；
3. **可选音频包络签名**：ffmpeg 解 PCM → 分段 RMS 包络 → 归一化互相关，识别“换配音/无音轨”；
4. **置信度分档**：按组内最低配对分数映射 `same_source` / `likely_same` / `possible`。

## 1. 指纹与比对算法

### 1.1 帧指纹（版本 v2）

- 每帧仍取 32×32 灰度（保留原 ffmpeg 抽帧/批处理/硬解回退路径）。
- 每帧产出两个 64-bit pHash：
  - `frame_hashes`：全帧（缩放 + 黑边 pad，原行为）；
  - `frame_hashes_center`：**内容裁剪指纹**——按行/列亮度均值检测内容 bbox（阈值
    `VIDEO_CONTENT_BLACK_THRESHOLD=18`），裁掉四周黑边后最近邻放大回 32×32 再算 pHash；
    检测不到合理 bbox（纯黑/内容过小）时退回中心 80% 裁剪。
- `fingerprint = "v2:<n_eff>:<full hex...>"`；`frame_hex_center` 单独存放。
- **缓存失效**：`VIDEO_ALGO_VERSION` 从 `video-v1` 升到 `video-v2`，首次扫描会重算视频指纹
  （一次性成本；之后命中 `<var>/cache/video.sqlite`）。

### 1.2 配对打分（增强版）

对每对视频：

1. 时长比例过滤（沿用 `duration_ratio_allowed`：短片 0.5–2.0，长片 0.7–1.4）；
2. 初筛：全帧/裁剪指纹的按位相似度取最大，<0.25 直接跳过；
3. 候选方案：
   - 全帧 DTW；
   - 裁剪帧 DTW；
   - 全帧 × 裁剪帧（交叉）DTW；
   - 全帧/裁剪帧的**时间轴偏移对齐**（±3 帧，重叠 ≥0.8，按 `0.7*avg + 0.3*overlap` 打分）；
4. 取最高分作为视觉分；覆盖不足（<0.6）视为不可比；
5. 可选加权：`final = round(base*(1-w_asr-w_audio) + asr*w_asr + audio*w_audio)`，
   权重合计上限 0.5（超出按比例缩放）；ASR/音频缺失时自动退化为纯视觉。

### 1.3 音频包络签名（可选，默认关闭）

- `video_audio_signature_enabled=true` 时，每视频额外一次
  `ffmpeg -t 180 -vn -ac 1 -ar 8000 -f s16le -`（120 桶 RMS，归一化到 0..255）。
- 签名只比较**音量包络**，不可还原语音内容，不写日志/结果（只写相似度分数）。
- 相似度 = 零均值归一化互相关 × 100；静音/采样不足返回 None（不参与加权）。
- 失败 open：无音轨/解码失败只把 `audio_signature_status` 记为 `failed`，不影响视觉分析与删除安全。
- 缓存：`<var>/cache/video.sqlite` 内单独 key（`audio-v1|size|mtime|inode|dev|buckets`）。

### 1.4 置信度分档

按“非保留项与保留项”的最终分数（组内取**最低**配对分数，保守）：

| 档位 | 分数 | 含义 |
|---|---|---|
| same_source | ≥95 | 极可能同源（仅编码/封装不同） |
| likely_same | ≥85 | 很可能同源 |
| possible | <85 | 可能相似（建议人工复核） |

## 2. 新增设置

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| video_center_crop_enabled | bool | true | 启用边缘裁剪指纹（关=只用全帧） |
| video_offset_align_enabled | bool | true | 启用时间轴偏移对齐（关=只用 DTW） |
| video_audio_signature_enabled | bool | false | 启用音频包络签名（额外一次 ffmpeg 解码） |
| video_audio_weight | float | 0.2 | 音频签名权重（0–0.5；仅启用时生效；与 ASR 权重合计 ≤0.5） |

## 3. 结果字段（`GET /api/scans/{id}/groups`，match_type=video）

组级新增：
- `video_confidence`：`same_source` / `likely_same` / `possible`；
- `video_confidence_label`：中文说明；
- `video_max_score` / `video_min_score` / `video_avg_score`：组内配对分数统计；
- `video_match_method`：`dtw` / `offset`（取组内代表性方法）；
- `video_center_used`：是否用到边缘裁剪指纹；
- `video_audio_used` / `video_audio_score`：是否有音频签名参与及平均音频分；
- `video_duration_delta_sec`：组内时长差（秒）。

文件级新增：
- `video_match_method`（`dtw`/`offset`/`keeper`）；
- `video_center_used`（该文件与保留项的配对是否用到裁剪指纹）；
- `video_audio_status`（`disabled`/`unavailable`/`failed`/`ok`）；
- `video_audio_used` / `video_audio_score`（只存分数，不存签名）。

## 4. 前端

- 设置页「视频视觉相似」分组新增：边缘裁剪开关、时间轴对齐开关、音频签名开关、音频权重
  （未启用音频签名时置灰；权重非法时字段级报错）。
- 结果页视频组徽标新增：置信度（蓝/绿/黄）、`边缘裁剪`、`时间轴对齐`、`音频辅助 · N%`；
  分组副标题在组内时长差 >3 秒时显示“组内时长差”。
- 设置摘要显示“边缘裁剪 / 时间轴对齐 / 音频辅助 N%”。

## 5. 限制（与评估结论一致）

- **不承诺识别“不同剪辑版本”**：同源不同剪辑（增删片段/重剪）仍可能被判为相似或漏判；
  置信度分档与人工复核是必需的。
- 极端情况仍可能误判：大段黑屏/片头字幕卡、画面相同但内容不同的“同模板”视频；
  音频签名对纯音乐/环境音区分度有限（只比包络）。
- 覆盖率、长视频采样间隔等沿用原限制（`video_max_sample_interval_sec` 越大越省时间、越易漏检）。
- 首扫重算所有视频指纹（算法版本升级）会带来一次性 CPU 开销；可用「立即分析」/低峰时段执行。

## 6. 验收

- unittest：`tests/test_v0047_video.py`（内容裁剪去黑边、偏移对齐、增强配对、置信度分档、
  音频签名生成/相似度/静音、build_video_groups 音频加权、引擎级签名缓存与失败 open、
  设置校验、扫描结果字段）。
- e2e：`VIDEO-01`（真实 ffmpeg 生成“同源重编码 + 带黑边”样例，验证组字段与命中）、
  `VIDEO-02`（音频权重越界 400）。
- 静态：`BE-VIDEO-V2`、`FE-VIDEO-V2`。
- 真机：不同压制版本（H.264/H.265、不同码率、上下黑边、片头片尾差几秒）的命中率与耗时；
  音频签名对“国配/原声”版本的区分；置信度分档与人工复核体验。
