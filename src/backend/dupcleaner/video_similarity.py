# -*- coding: utf-8 -*-
"""视频视觉相似度：FFmpeg 抽帧 + 纯 Python pHash + DTW + SQLite 指纹缓存。

契约：docs/VIDEO_CONTRACT.md（binding）。
- 不依赖 ffprobe：解析 ffmpeg -i stderr 的 Duration。
- 所有 ffmpeg 调用使用参数数组、无 shell、-nostdin/-hide_banner/-loglevel error、
  -threads 1、单次超时；扫描停止时 kill 子进程。
- 128 帧内动态采样：n_eff = min(100, max(N, ceil(D / I)))，
  t_i = (i + 0.5) * D / n_eff；每帧 32x32 gray rawvideo（1024B）不落盘。
- 64-bit pHash（32x32 DCT 低频 8x8），视频指纹 = 帧 hex 序列。
- 相似度：时长比例预筛 0.7..1.4 → 指纹初筛 → DTW。
  score = round(100 * (0.7*avg + 0.3*coverage))，coverage >= 0.6 且 score >= 阈值。
- 缓存 <var>/video_cache.sqlite3，最多 20000 条 LRU；失败标记避免重复解码。
- 缓存/特征只用于缩小候选与人工决策；删除仍逐文件重校验。
"""

from __future__ import absolute_import

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time

from . import config, hashing, similarity

DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
# v0043：`ffmpeg -i` 的流信息行，例如
#   Stream #0:1(eng): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo, ...
#   Stream #0:2(chi): Subtitle: subrip (srt)
_STREAM_RE = re.compile(
    r"Stream #\d+:(?P<stream_index>\d+)"
    r"(?:\[0x[0-9a-fA-F]+\])?"
    r"(?:\((?P<language>[A-Za-z0-9_\-]+)\))?"
    r": (?P<kind>Audio|Subtitle|Video): (?P<codec>[^,\n]+)"
    r"(?P<rest>[^\n]*)")
_CHANNELS_RE = re.compile(
    r",\s*(mono|stereo|quad|5\.1|6\.1|7\.1|\d+ channels)")
_STREAM_TITLE_RE = re.compile(r"title\s*:\s*([^,\n]+)")
_RESOLUTION_RE = re.compile(r"(\d{2,5})x(\d{2,5})")
VIDEO_FILTER = ("scale=32:32:force_original_aspect_ratio=decrease,"
                "pad=32:32:(ow-iw)/2:(oh-ih)/2:black,format=gray")


def parse_media_streams(text):
    """解析 ffmpeg `-i` 输出里的 Stream 行 → 音轨/字幕轨/视频轨列表。

    纯文本解析（不依赖 ffprobe）；字段：stream_index/codec/language/channels/
    default/title。语言标签可能缺失（很多容器不写），此时只能按序号选择。
    """
    result = {"audio_streams": [], "subtitle_streams": [], "video_streams": []}
    for line in (text or "").splitlines():
        match = _STREAM_RE.search(line)
        if not match:
            continue
        rest = match.group("rest") or ""
        item = {
            "stream_index": int(match.group("stream_index")),
            "codec": (match.group("codec") or "").strip(),
            "language": (match.group("language") or "").strip().lower(),
            "channels": "",
            "default": "(default)" in rest,
            "title": "",
        }
        title = _STREAM_TITLE_RE.search(rest)
        if title:
            item["title"] = title.group(1).strip()
        channels = _CHANNELS_RE.search(rest)
        if channels:
            item["channels"] = channels.group(1).strip()
        kind = match.group("kind").lower()
        if kind == "video":
            resolution = _RESOLUTION_RE.search(rest)
            if resolution:
                item["width"] = int(resolution.group(1))
                item["height"] = int(resolution.group(2))
        if kind == "audio":
            result["audio_streams"].append(item)
        elif kind == "subtitle":
            result["subtitle_streams"].append(item)
        else:
            result["video_streams"].append(item)
    return result

_MASK64 = 0xFFFFFFFFFFFFFFFF
_PROBE_LOCK = threading.RLock()
_PROBE_CACHE = {}
# 用于区分“settings provider 尚未读取过”和“settings 路径就是空字符串”。
_SETTINGS_PATH_UNSET = object()
_COS_TABLE = None

CONTENT_QUICK_HASH_ALGO = config.VIDEO_CONTENT_QUICK_HASH_ALGO
CONTENT_QUICK_HASH_SAMPLE_KB = config.VIDEO_CONTENT_QUICK_HASH_SAMPLE_KB
FRAME_BATCH_SIZE = config.VIDEO_BATCH_FRAMES


def content_quick_hash(path):
    """视频内容快速校验哈希：size + 头/中/尾各 64KB，SHA256（删除前重校验用）。

    该哈希不是视觉指纹；只用于确认“候选/保留视频内容未变化”。
    中间采样用于发现仅替换中段的伪原创/篡改；仍不是全量校验。
    不依赖 ffmpeg，读取成本约 192KB/文件。
    """
    sample = max(1, int(CONTENT_QUICK_HASH_SAMPLE_KB)) * 1024
    stat_result = os.stat(path)
    size = int(stat_result.st_size)
    positions = [0]
    if size > sample * 2:
        positions.append(max(sample, (size // 2) - (sample // 2)))
    positions.append(max(0, size - sample))
    digest = hashing.hasher_factory(CONTENT_QUICK_HASH_ALGO)()
    digest.update(b"VCQ2" + int(size).to_bytes(8, "big", signed=False))
    with open(path, "rb") as handle:
        for offset in positions:
            handle.seek(offset)
            data = handle.read(sample)
            digest.update(int(offset).to_bytes(8, "big", signed=False))
            digest.update(len(data).to_bytes(8, "big", signed=False))
            digest.update(data)
    return digest.hexdigest()


class VideoSkip(Exception):
    """该视频不参与相似计算：type/size/decode/no_duration/timeout/error。"""

    def __init__(self, reason, detail=""):
        super(VideoSkip, self).__init__(reason + (": " + detail if detail else ""))
        self.reason = reason
        self.detail = detail


class VideoTimeout(Exception):
    pass


class VideoStopped(Exception):
    pass


# ---------------------------------------------------------------------------
# pHash（32x32 灰度 → DCT 低频 8x8 → 64-bit）
# ---------------------------------------------------------------------------

def _cos_table():
    global _COS_TABLE
    if _COS_TABLE is None:
        size = config.VIDEO_FRAME_SIZE
        _COS_TABLE = tuple(
            tuple(math.cos((2.0 * x + 1.0) * u * math.pi / (2.0 * size))
                  for x in range(size))
            for u in range(8))
    return _COS_TABLE


def phash_from_gray(gray):
    """纯 Python DCT pHash；gray 为 1024 字节/整数序列。"""
    size = config.VIDEO_FRAME_SIZE
    if len(gray) != size * size:
        raise ValueError("gray frame must be %d bytes" % (size * size))
    cos = _cos_table()
    row_dct = [[0.0] * 8 for _ in range(size)]
    for y in range(size):
        row = gray[y * size:(y + 1) * size]
        target = row_dct[y]
        for u in range(8):
            cu = cos[u]
            total = 0.0
            for x in range(size):
                total += row[x] * cu[x]
            target[u] = total
    coeffs = [0.0] * 64
    for v in range(8):
        cv = cos[v]
        for u in range(8):
            total = 0.0
            for y in range(size):
                total += row_dct[y][u] * cv[y]
            coeffs[v * 8 + u] = total
    coeffs[0] = 0.0  # 忽略 DC 分量（亮度）
    ordered = sorted(coeffs)
    median = ordered[len(ordered) // 2]
    bits = 0
    for value in coeffs:
        bits = (bits << 1) | (1 if value > median else 0)
    return bits & _MASK64


def frame_similarity(hash_a, hash_b):
    hamming = bin((int(hash_a) ^ int(hash_b)) & _MASK64).count("1")
    return 1.0 - hamming / 64.0


def gray_center_crop(gray, ratio=None):
    """从 32×32 灰度帧取中心裁剪区域，再最近邻放大回 32×32（v0047）。

    用于抵消上下黑边/左右黑边（不同压制版本的 padding 不同）导致的指纹差异；
    纯整数运算，不引入额外 ffmpeg 调用。
    """
    size = config.VIDEO_FRAME_SIZE
    if len(gray) != size * size:
        raise ValueError("gray frame must be %d bytes" % (size * size))
    try:
        ratio = float(ratio if ratio is not None
                      else config.VIDEO_CENTER_CROP_RATIO)
    except (TypeError, ValueError):
        ratio = config.VIDEO_CENTER_CROP_RATIO
    ratio = max(0.5, min(1.0, ratio))
    side = max(8, int(round(size * ratio)))
    if side >= size:
        return bytes(gray)
    offset = (size - side) // 2
    result = bytearray(size * size)
    for y in range(size):
        source_y = offset + min(side - 1, int(y * side / size))
        row_start = source_y * size + offset
        row = gray[row_start:row_start + side]
        for x in range(size):
            result[y * size + x] = row[min(side - 1, int(x * side / size))]
    return bytes(result)


def gray_content_crop(gray, black_threshold=None, min_ratio=None):
    """检测并裁掉四周黑边后放大回 32×32（v0047“边缘裁剪”指纹）。

    真实压制版本常带不同宽高比黑边/上下黑边，ffmpeg pad 后指纹差异大；
    这里按行/列亮度均值检测内容 bbox（>= 阈值视为有内容），裁掉黑边后
    最近邻放大回 32×32。检测不到合理 bbox（纯黑/内容过小）时退回中心裁剪。
    """
    size = config.VIDEO_FRAME_SIZE
    if len(gray) != size * size:
        raise ValueError("gray frame must be %d bytes" % (size * size))
    try:
        threshold = int(black_threshold if black_threshold is not None
                        else config.VIDEO_CONTENT_BLACK_THRESHOLD)
    except (TypeError, ValueError):
        threshold = config.VIDEO_CONTENT_BLACK_THRESHOLD
    try:
        min_ratio = float(min_ratio if min_ratio is not None
                          else config.VIDEO_CONTENT_MIN_RATIO)
    except (TypeError, ValueError):
        min_ratio = config.VIDEO_CONTENT_MIN_RATIO
    rows = []
    for y in range(size):
        row = gray[y * size:(y + 1) * size]
        rows.append(sum(row) / float(size))
    columns = []
    for x in range(size):
        total = 0
        for y in range(size):
            total += gray[y * size + x]
        columns.append(total / float(size))
    top = 0
    while top < size and rows[top] <= threshold:
        top += 1
    bottom = size - 1
    while bottom > top and rows[bottom] <= threshold:
        bottom -= 1
    left = 0
    while left < size and columns[left] <= threshold:
        left += 1
    right = size - 1
    while right > left and columns[right] <= threshold:
        right -= 1
    height = bottom - top + 1
    width = right - left + 1
    if (top >= size or bottom < top or left >= size or right < left
            or height < size * min_ratio or width < size * min_ratio):
        return gray_center_crop(gray)
    if (top, bottom, left, right) == (0, size - 1, 0, size - 1):
        return bytes(gray)
    result = bytearray(size * size)
    for y in range(size):
        source_y = top + min(height - 1, int(y * height / size))
        row_start = source_y * size + left
        for x in range(size):
            source_x = left + min(width - 1, int(x * width / size))
            result[y * size + x] = gray[row_start + (source_x - left)]
    return bytes(result)


def phash_from_gray_pair(gray):
    """返回 (full_hash, content_hash)；content 用于黑边/裁剪鲁棒比对（v0047）。"""
    return phash_from_gray(gray), phash_from_gray(gray_content_crop(gray))


def _filtered_frames(frames):
    try:
        return [int(value) for value in (frames or [])]
    except (TypeError, ValueError):  # pragma: no cover - 缓存脏数据兜底
        return []


def offset_aligned_stats(frames_a, frames_b, max_shift=None,
                         min_overlap=None):
    """时间轴整体偏移对齐（v0047）：尝试 -N..N 帧位移，返回 (avg, overlap, shift)。

    适合“片头/片尾被剪切、采样网格整体错位”的同源视频；overlap 低于阈值
    （默认 0.8）的位移不参与比较。
    """
    frames_a = _filtered_frames(frames_a)
    frames_b = _filtered_frames(frames_b)
    n, m = len(frames_a), len(frames_b)
    if not n or not m:
        return 0.0, 0.0, 0
    try:
        max_shift = int(max_shift if max_shift is not None
                        else config.VIDEO_OFFSET_MAX_SHIFT)
    except (TypeError, ValueError):
        max_shift = config.VIDEO_OFFSET_MAX_SHIFT
    try:
        min_overlap = float(min_overlap if min_overlap is not None
                            else config.VIDEO_OFFSET_MIN_OVERLAP)
    except (TypeError, ValueError):
        min_overlap = config.VIDEO_OFFSET_MIN_OVERLAP
    best = (0.0, 0.0, 0)
    for shift in range(-max_shift, max_shift + 1):
        start_a = max(0, shift)
        start_b = max(0, -shift)
        count = min(n - start_a, m - start_b)
        if count <= 0:
            continue
        overlap = float(count) / float(min(n, m))
        if overlap < min_overlap:
            continue
        total = 0.0
        for index in range(count):
            total += frame_similarity(frames_a[start_a + index],
                                      frames_b[start_b + index])
        avg = total / count
        score = 0.7 * avg + 0.3 * overlap
        if score > 0.7 * best[0] + 0.3 * best[1]:
            best = (avg, overlap, shift)
    return best


def pair_score_enhanced(frames_a_full, frames_a_center, frames_b_full,
                        frames_b_center, threshold=0, use_center=True,
                        use_offset=True):
    """v0047 增强配对：全帧/中心裁剪多组合 DTW + 时间轴偏移对齐，取最优。

    返回 (score, avg, coverage, method, center_used) 或 None。method 为
    "dtw" / "offset"；覆盖不足（<VIDEO_MIN_COVERAGE）返回 None。
    """
    variants = [(frames_a_full, frames_b_full)]
    if use_center:
        variants.extend([
            (frames_a_center, frames_b_center),
            (frames_a_full, frames_b_center),
            (frames_a_center, frames_b_full),
        ])
    best = None  # (score01, avg, coverage, method, center_used)
    for index, (first, second) in enumerate(variants):
        first = _filtered_frames(first)
        second = _filtered_frames(second)
        if not first or not second:
            continue
        avg, coverage = dtw_stats(first, second)
        if coverage < config.VIDEO_MIN_COVERAGE:
            continue
        score = 0.7 * avg + 0.3 * coverage
        center_used = index in (1, 2, 3)
        if best is None or score > best[0]:
            best = (score, avg, coverage, "dtw", center_used)
    if use_offset:
        offset_variants = [(_filtered_frames(frames_a_full),
                            _filtered_frames(frames_b_full), False)]
        if use_center:
            offset_variants.append((_filtered_frames(frames_a_center),
                                    _filtered_frames(frames_b_center), True))
        for first, second, center_used in offset_variants:
            if not first or not second:
                continue
            avg, overlap, _shift = offset_aligned_stats(first, second)
            if overlap < config.VIDEO_MIN_COVERAGE:
                continue
            score = 0.7 * avg + 0.3 * overlap
            if best is None or score > best[0]:
                best = (score, avg, overlap, "offset", center_used)
    if best is None:
        return None
    score = int(round(100.0 * best[0]))
    if score < int(threshold or 0):
        return None
    return (score, best[1], best[2], best[3], bool(best[4]))


def confidence_band(score, bands=None):
    """按分数映射置信度档位（v0047）；返回 (key, label)。"""
    try:
        score = int(score or 0)
    except (TypeError, ValueError):
        score = 0
    bands = bands or config.VIDEO_CONFIDENCE_BANDS
    for key, minimum in bands:
        if score >= int(minimum):
            return key, config.VIDEO_CONFIDENCE_LABELS.get(key, key)
    return "possible", config.VIDEO_CONFIDENCE_LABELS.get("possible", u"可能相似")


def audio_signature_from_pcm(pcm, buckets=None, rate=None):
    """从 s16le 单声道 PCM 计算 RMS 包络签名（v0047，替代真实音频指纹）。

    返回 0..255 的整数数组；签名只用于相似度比较，不可还原音频内容。
    """
    try:
        buckets = int(buckets or config.VIDEO_AUDIO_BUCKETS)
    except (TypeError, ValueError):
        buckets = config.VIDEO_AUDIO_BUCKETS
    buckets = max(8, min(600, buckets))
    data = bytes(pcm or b"")
    sample_count = len(data) // 2
    if sample_count < buckets:
        return []
    try:
        import array
        samples = array.array("h")
        samples.frombytes(data[:sample_count * 2])
    except (ValueError, ImportError):  # pragma: no cover
        return []
    chunk = max(1, sample_count // buckets)
    values = []
    for index in range(buckets):
        start = index * chunk
        end = min(sample_count, start + chunk)
        if end <= start:
            values.append(0)
            continue
        total = 0
        for position in range(start, end):
            value = samples[position]
            total += value * value
        values.append(int(math.sqrt(total / float(end - start))))
    peak = max(values) or 1
    return [int(round(value * 255.0 / peak)) for value in values]


def audio_signature_similarity(signature_a, signature_b):
    """音频包络签名相似度（0..100）：零均值归一化互相关。

    长度不同时按较短长度截断（采样秒数/桶数一致时无影响）。
    """
    try:
        first = [float(value) for value in (signature_a or [])]
        second = [float(value) for value in (signature_b or [])]
    except (TypeError, ValueError):
        return None
    count = min(len(first), len(second))
    if count < 8:
        return None
    first = first[:count]
    second = second[:count]
    mean_a = sum(first) / count
    mean_b = sum(second) / count
    numerator = 0.0
    denom_a = 0.0
    denom_b = 0.0
    for index in range(count):
        value_a = first[index] - mean_a
        value_b = second[index] - mean_b
        numerator += value_a * value_b
        denom_a += value_a * value_a
        denom_b += value_b * value_b
    if denom_a <= 0 or denom_b <= 0:
        # 静音签名（无语音/无人声）无法比较 → 不给分
        return None
    correlation = numerator / math.sqrt(denom_a * denom_b)
    return int(round(max(0.0, min(1.0, correlation)) * 100.0))


def prefilter_similarity(frames_a, frames_b):
    """视频指纹初筛：按相对位置对齐的帧平均相似度。"""
    if not frames_a or not frames_b:
        return 0.0
    count = min(len(frames_a), len(frames_b))
    total = 0.0
    for index in range(count):
        total += frame_similarity(frames_a[index], frames_b[index])
    return total / count


def dtw_stats(frames_a, frames_b):
    """DTW 序列对齐（最小化帧差异代价），返回 (avg_similarity, coverage)。

    用代价 1-frame_similarity 做 DP，避免“路径越长累计相似度越大”的偏差；
    avg = 1 - total_cost / path_length。
    """
    n, m = len(frames_a), len(frames_b)
    if not n or not m:
        return 0.0, 0.0
    previous = [0.0] * m
    previous_len = [0] * m
    for i in range(n):
        current = [0.0] * m
        current_len = [0] * m
        for j in range(m):
            cost = 1.0 - frame_similarity(frames_a[i], frames_b[j])
            if i == 0 and j == 0:
                best = 0.0
                best_len = 0
            else:
                candidates = []
                if i > 0:
                    candidates.append((previous[j], previous_len[j]))
                if j > 0:
                    candidates.append((current[j - 1], current_len[j - 1]))
                if i > 0 and j > 0:
                    candidates.append((previous[j - 1], previous_len[j - 1]))
                best, best_len = min(candidates, key=lambda item: item[0])
            current[j] = best + cost
            current_len[j] = best_len + 1
        previous = current
        previous_len = current_len
    total_cost = previous[m - 1]
    length = previous_len[m - 1]
    if length <= 0:
        return 0.0, 0.0
    avg = 1.0 - total_cost / length
    coverage = float(length) / min(n, m)
    return max(0.0, avg), min(1.0, coverage)


def pair_score(frames_a, frames_b, threshold=0):
    """返回 (score, avg, coverage)；coverage<0.6 返回 None。"""
    avg, coverage = dtw_stats(frames_a, frames_b)
    if coverage < config.VIDEO_MIN_COVERAGE:
        return None
    score = int(round(100.0 * (0.7 * avg + 0.3 * coverage)))
    if score < threshold:
        return None
    return (score, avg, coverage)


def combine_scores(base_score, text_score, weight):
    """final = round(base*(1-w) + text*w)；w 限制在 0..0.5。"""
    weight = max(0.0, min(0.5, float(weight or 0.0)))
    return int(round(float(base_score) * (1.0 - weight)
                     + float(text_score) * weight))


def duration_ratio_allowed(duration_a, duration_b):
    """短视频（双方 <=180s）放宽到 0.5..2.0，否则 0.7..1.4。"""
    duration_a = float(duration_a or 0)
    duration_b = float(duration_b or 0)
    if duration_a <= 0 or duration_b <= 0:
        return False
    ratio = duration_a / duration_b
    if (duration_a <= config.SHORT_VIDEO_MAX_SEC
            and duration_b <= config.SHORT_VIDEO_MAX_SEC):
        return (config.SHORT_VIDEO_RATIO_MIN <= ratio
                <= config.SHORT_VIDEO_RATIO_MAX)
    return (config.VIDEO_DURATION_RATIO_MIN <= ratio
            <= config.VIDEO_DURATION_RATIO_MAX)


def build_video_groups(items, threshold, asr_weight=0.0, audio_weight=0.0,
                       use_center=True, use_offset=True):
    """安全团分组（v0047 增强）：任意两两分数 >= 阈值。

    - 视觉：全帧/中心裁剪多组合 DTW + 时间轴偏移对齐，取最优（抵消黑边/片头剪切）；
    - 可选 ASR 文本签名与音频包络签名加权（两者权重之和上限 0.5）；
    - 每个簇附置信度档位（same_source / likely_same / possible）与分数统计。
    items 需含 analysis.frame_hashes；可选 frame_hashes_center / asr_signature /
    audio_signature。
    """
    threshold = max(0, min(100, int(threshold)))
    asr_weight = max(0.0, min(0.5, float(asr_weight or 0.0)))
    audio_weight = max(0.0, min(0.5, float(audio_weight or 0.0)))
    if asr_weight + audio_weight > 0.5:
        scale = 0.5 / (asr_weight + audio_weight)
        asr_weight *= scale
        audio_weight *= scale
    count = len(items)
    pairs = {}
    pair_details = {}
    for left in range(count):
        for right in range(left + 1, count):
            first = items[left]["analysis"]
            second = items[right]["analysis"]
            duration_a = float(first.get("duration") or 0)
            duration_b = float(second.get("duration") or 0)
            if not duration_ratio_allowed(duration_a, duration_b):
                continue
            frames_a = first.get("frame_hashes") or []
            frames_b = second.get("frame_hashes") or []
            center_a = first.get("frame_hashes_center") or []
            center_b = second.get("frame_hashes_center") or []
            prefilter = prefilter_similarity(frames_a, frames_b)
            if center_a and center_b:
                prefilter = max(prefilter,
                                prefilter_similarity(center_a, center_b))
            if prefilter < 0.25:
                continue
            visual = pair_score_enhanced(
                frames_a, center_a, frames_b, center_b, 0,
                use_center=bool(use_center and center_a and center_b),
                use_offset=bool(use_offset))
            if visual is None:
                continue
            base_score, _avg, _coverage, method, center_used = visual
            base_score = int(base_score)
            text_score = None
            asr_used = False
            audio_score = None
            audio_used = False
            weighted = []
            signature_a = first.get("asr_signature")
            signature_b = second.get("asr_signature")
            if asr_weight > 0 and signature_a and signature_b:
                text_score = similarity.minhash_similarity_percent(
                    signature_a, signature_b)
                if text_score is not None:
                    weighted.append((float(text_score), asr_weight))
                    asr_used = True
            if audio_weight > 0:
                audio_score = audio_signature_similarity(
                    first.get("audio_signature"),
                    second.get("audio_signature"))
                if audio_score is not None:
                    weighted.append((float(audio_score), audio_weight))
                    audio_used = True
            final_score = base_score
            if weighted:
                weight_total = sum(item[1] for item in weighted)
                weighted_mean = (sum(item[0] * item[1] for item in weighted)
                                 / weight_total)
                final_score = int(round(
                    base_score * (1.0 - weight_total)
                    + weighted_mean * weight_total))
            if final_score < threshold:
                continue
            pairs[(left, right)] = final_score
            pair_details[(left, right)] = {
                "base": base_score,
                "text_score": text_score,
                "asr_used": asr_used,
                "audio_score": audio_score,
                "audio_used": audio_used,
                "method": method,
                "center_used": bool(center_used),
            }
    groups = []
    group_of = {}
    for (first, second), score in sorted(pairs.items(),
                                         key=lambda item: (-item[1], item[0])):
        group_first = group_of.get(first)
        group_second = group_of.get(second)
        if group_first is None and group_second is None:
            groups.append({"members": [first, second]})
            group_of[first] = group_of[second] = len(groups) - 1
            continue
        if group_first is not None and group_second is None:
            group = groups[group_first]
            if all(pairs.get((min(m, second), max(m, second)), 0) >= threshold
                   for m in group["members"]):
                group["members"].append(second)
                group_of[second] = group_first
            continue
        if group_second is not None and group_first is None:
            group = groups[group_second]
            if all(pairs.get((min(m, first), max(m, first)), 0) >= threshold
                   for m in group["members"]):
                group["members"].append(first)
                group_of[first] = group_second
            continue
        if group_first == group_second:
            continue
        left_group = groups[group_first]
        right_group = groups[group_second]
        mergeable = True
        for member in right_group["members"]:
            for existing in left_group["members"]:
                key = (min(existing, member), max(existing, member))
                if pairs.get(key, 0) < threshold:
                    mergeable = False
                    break
            if not mergeable:
                break
        if mergeable:
            for member in right_group["members"]:
                left_group["members"].append(member)
                group_of[member] = group_first
            groups[group_second] = {"members": []}
    clusters = []
    for group in groups:
        members = sorted(group.get("members") or [])
        if len(members) < 2:
            continue
        scores = {}
        details = {}
        for left in range(len(members)):
            for right in range(left + 1, len(members)):
                key = (members[left], members[right])
                scores[key] = pairs.get(key)
                details[key] = pair_details.get(key) or {
                    "base": pairs.get(key), "text_score": None,
                    "asr_used": False, "audio_score": None,
                    "audio_used": False, "method": "dtw",
                    "center_used": False,
                }
        member_scores = [value for value in scores.values()
                         if value is not None]
        band_key, band_label = confidence_band(min(member_scores)
                                               if member_scores else 0)
        clusters.append({
            "indices": members,
            "scores": scores,
            "details": details,
            "confidence": band_key,
            "confidence_label": band_label,
            "max_score": max(member_scores) if member_scores else 0,
            "min_score": min(member_scores) if member_scores else 0,
            "avg_score": int(round(sum(member_scores)
                                   / max(1, len(member_scores)))),
            "center_used": any(bool(item.get("center_used"))
                               for item in details.values()),
            "method": next((item.get("method") for item in details.values()
                            if item.get("method")), "dtw"),
            "audio_used": any(bool(item.get("audio_used"))
                              for item in details.values()),
        })
    clusters.sort(key=lambda item: (-int(item.get("min_score") or 0),
                                    item["indices"]))
    return clusters

def compute_n_eff(duration, sample_frames, max_interval):
    duration = max(0.0, float(duration))
    sample_frames = int(sample_frames)
    max_interval = max(1, int(max_interval))
    if duration <= 0:
        return max(1, sample_frames)
    needed = int(math.ceil(duration / float(max_interval)))
    return max(1, min(config.VIDEO_MAX_SAMPLE_FRAMES,
                      max(sample_frames, needed)))


# ---------------------------------------------------------------------------
# FFmpeg
# ---------------------------------------------------------------------------

def _kill_process(process):
    try:
        if process.poll() is None:
            process.kill()
    except OSError:
        pass
    try:
        process.communicate(timeout=5)
    except Exception:
        try:
            process.wait(timeout=5)
        except Exception:
            pass


def _normalize_hwaccels(values):
    result = []
    for item in values or []:
        text = str(item).strip().lower()
        if text:
            result.append(text)
    return result


def hwaccel_label_from(hwaccels):
    """按 ffmpeg -hwaccels 能力推导 auto 模式的实际硬解标签（旧逻辑保持不变）。"""
    accels = _normalize_hwaccels(hwaccels)
    if "vaapi" in accels:
        return "vaapi"
    if "qsv" in accels:
        return "qsv"
    if "nvdec" in accels or "cuda" in accels:
        return "nvdec"
    if "d3d11va" in accels or "dxva2" in accels or "videotoolbox" in accels:
        return "auto"
    return "none"


# 显式硬解模式 -> ffmpeg -hwaccels 实际名称（同义名）。
# 用户/UI 使用 "nvdec" 语义，但 ffmpeg 的 -hwaccels 在 NVIDIA 上通常列出
# "cuda"（旧版本为 "cuvid"），传给 -hwaccel 的必须是能力列表里的名字。
_HWACCEL_MODE_ALIASES = {
    "nvdec": ("nvdec", "cuda", "cuvid"),
}


def hwaccel_supported(hwaccels):
    """当前 ffmpeg 实际支持的显式硬解模式（供 UI 启用/禁用下拉项）。"""
    accels = _normalize_hwaccels(hwaccels)
    return [mode for mode in config.VIDEO_HWACCEL_EXPLICIT
            if any(alias in accels
                   for alias in _HWACCEL_MODE_ALIASES.get(mode, (mode,)))]


def hwaccel_for_mode(mode, hwaccels):
    """解析硬解模式，返回 (上报标签, 传给 ffmpeg 的 -hwaccel 值, 是否降级)。

    - off/空：不做硬解，返回 ("none", "none", False)；
    - auto：沿用旧 hwaccel_label() 语义上报实际能力，ffmpeg 仍用 -hwaccel auto；
    - 显式 vaapi/qsv/nvdec/videotoolbox：仅在 ffmpeg -hwaccels 支持（含同义名，
      如 nvdec -> cuda/cuvid）时使用，否则回退 none 并标记 fallback
      （能力接口会暴露该标记）。
    """
    mode = str(mode or "auto").strip().lower()
    accels = _normalize_hwaccels(hwaccels)
    if mode in ("", "auto"):
        report = hwaccel_label_from(accels)
        flag = "auto" if report != "none" else "none"
        return (report, flag, False)
    if mode == "off":
        return ("none", "none", False)
    for alias in _HWACCEL_MODE_ALIASES.get(mode, (mode,)):
        if alias in accels:
            return (mode, alias, False)
    return ("none", "none", True)


def _hwaccel_flag_args(label):
    """把硬解标签转成 ffmpeg 参数；none/off/空表示纯软件解码。"""
    text = str(label or "").strip().lower()
    if not text or text in ("none", "off"):
        return []
    return ["-hwaccel", text]


class FFmpegTool(object):
    def __init__(self, cmd=None, timeout=180.0, settings_provider=None,
                 runtime_config=None):
        self._explicit_cmd = list(cmd) if cmd else None
        self._cmd = list(cmd) if cmd else None
        self.timeout = float(timeout)
        self._settings_provider = settings_provider
        self._runtime_config = runtime_config
        self._source = "explicit" if cmd else None
        self._lock = threading.RLock()
        self._resolved = bool(cmd)
        self._version = None
        self._hwaccels = None
        self._settings_path_seen = _SETTINGS_PATH_UNSET

    # -- settings 路径变化检测 -----------------------------------------
    def _settings_path(self):
        if self._settings_provider is None:
            return ""
        try:
            settings = self._settings_provider() or {}
        except Exception:
            return ""
        if not isinstance(settings, dict):
            return ""
        value = settings.get("ffmpeg_path") or ""
        return str(value).strip()

    def _refresh_settings_path(self):
        """settings.ffmpeg_path 变化时重新解析，并清空 version/hwaccels 缓存。"""
        current = self._settings_path()
        if self._settings_path_seen is _SETTINGS_PATH_UNSET:
            self._settings_path_seen = current
        elif current != self._settings_path_seen:
            self._settings_path_seen = current
            if not self._explicit_cmd:
                self._cmd = None
                self._resolved = False
                self._source = None
                self._version = None
                self._hwaccels = None
        return current

    def resolve(self, runtime_config=None):
        """解析 ffmpeg 路径。优先级（v0021 P1）：

        显式 cmd > settings.ffmpeg_path > DUP_FFMPEG_PATH /
        runtime_config.ffmpeg_path > shutil.which('ffmpeg')。
        """
        with self._lock:
            if runtime_config is not None:
                self._runtime_config = runtime_config
            if self._explicit_cmd:
                self._source = "explicit"
                self._cmd = list(self._explicit_cmd)
                return self._cmd
            settings_path = self._refresh_settings_path()
            if self._resolved:
                return self._cmd
            self._resolved = True
            candidates = []
            if settings_path:
                candidates.append((settings_path, "settings"))
            env_path = os.environ.get("DUP_FFMPEG_PATH")
            if env_path:
                candidates.append((env_path, "env"))
            cfg_path = getattr(self._runtime_config, "ffmpeg_path", None)
            if cfg_path:
                candidates.append((cfg_path, "env"))
            which = shutil.which("ffmpeg")
            if which:
                candidates.append((which, "which"))
            for candidate, source in candidates:
                if not candidate:
                    continue
                if not os.path.isfile(candidate):
                    continue
                if not os.access(candidate, os.X_OK):
                    continue
                self._cmd = [candidate]
                self._source = source
                return self._cmd
            self._cmd = None
            self._source = "unavailable"
            return None

    @property
    def path(self):
        with self._lock:
            return self._cmd[0] if self._cmd else ""

    @property
    def source(self):
        """解析来源：explicit / settings / env / which / unavailable。"""
        with self._lock:
            return self._source or "unavailable"

    @property
    def configured_path(self):
        """用户在 settings 中显式配置的路径（空=自动探测）。"""
        with self._lock:
            if self._explicit_cmd:
                return ""
            return self._settings_path()

    def available(self):
        return bool(self.resolve())

    def _run(self, args, timeout, stop_check=None):
        command = list(self.resolve() or []) + list(args)
        if not command:
            raise VideoSkip("error", u"未找到 ffmpeg")
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
        except OSError as exc:
            raise VideoSkip("error", u"无法启动 ffmpeg: %s" % exc)
        deadline = time.monotonic() + max(1.0, float(timeout))
        try:
            while process.poll() is None:
                if stop_check is not None and stop_check():
                    _kill_process(process)
                    raise VideoStopped()
                if time.monotonic() >= deadline:
                    _kill_process(process)
                    raise VideoTimeout()
                time.sleep(0.05)
            stdout, stderr = process.communicate()
        except BaseException:
            _kill_process(process)
            raise
        return process.returncode, stdout or b"", stderr or b""

    def version(self):
        # 先 resolve：settings 路径变化时会清空缓存并重新探测。
        self.resolve()
        with self._lock:
            if self._version is not None:
                return self._version
        if not self.available():
            return "unavailable"
        try:
            _code, stdout, stderr = self._run(["-version"], timeout=10.0)
        except (VideoSkip, VideoTimeout, VideoStopped):
            return "unavailable"
        text = (stdout or stderr).decode("utf-8", "replace")
        first_line = text.splitlines()[0] if text else ""
        match = re.search(r"ffmpeg version\s+(\S+)", first_line)
        version = match.group(1) if match else (first_line.strip() or "unknown")
        with self._lock:
            self._version = version
        return version

    def hwaccels(self):
        # 先 resolve：settings 路径变化时会清空缓存并重新探测。
        self.resolve()
        with self._lock:
            if self._hwaccels is not None:
                return list(self._hwaccels)
        if not self.available():
            return []
        try:
            _code, stdout, _stderr = self._run(["-hwaccels"], timeout=10.0)
        except (VideoSkip, VideoTimeout, VideoStopped):
            return []
        text = stdout.decode("utf-8", "replace")
        accels = []
        for line in text.splitlines():
            line = line.strip().lower()
            if not line or line.startswith("hardware acceleration"):
                continue
            accels.append(line)
        with self._lock:
            self._hwaccels = list(accels)
        return list(accels)

    def hwaccel_label(self):
        return hwaccel_label_from(self.hwaccels())

    def probe_duration(self, path, timeout, stop_check=None):
        """解析 ffmpeg -i stderr 的 Duration（不依赖 ffprobe）。

        注意：Duration 是 info 级别日志，探测必须用 -loglevel info（或省略），
        抽帧仍使用 -loglevel error 以保持输出干净。
        """
        args = ["-nostdin", "-hide_banner", "-loglevel", "info", "-i", path]
        try:
            _code, _stdout, stderr = self._run(args, timeout, stop_check)
        except VideoSkip:
            raise
        match = DURATION_RE.search(stderr.decode("utf-8", "replace"))
        if not match:
            return None
        hours, minutes, seconds = match.groups()
        try:
            return int(hours) * 3600.0 + int(minutes) * 60.0 + float(seconds)
        except (TypeError, ValueError):
            return None

    def probe_media(self, path, timeout, stop_check=None):
        """探测时长、音轨/字幕轨/视频轨列表（-loglevel info；不记录隐私内容）。

        v0043：额外解析每路流的序号/语言/编码/声道，供“优先保留带内嵌字幕或
        多音轨版本”的保留规则与后续音轨选择使用。
        """
        args = ["-nostdin", "-hide_banner", "-loglevel", "info", "-i", path]
        try:
            _code, _stdout, stderr = self._run(args, timeout, stop_check)
        except VideoSkip:
            raise
        text = stderr.decode("utf-8", "replace")
        match = DURATION_RE.search(text)
        duration = None
        if match:
            hours, minutes, seconds = match.groups()
            try:
                duration = (int(hours) * 3600.0 + int(minutes) * 60.0
                            + float(seconds))
            except (TypeError, ValueError):
                duration = None
        streams = parse_media_streams(text)
        has_audio = (bool(streams["audio_streams"])
                     or re.search(r"Stream #\d+:\d+.*Audio:", text) is not None)
        return {
            "duration": duration,
            "has_audio": has_audio,
            "has_subtitle": bool(streams["subtitle_streams"]),
            "audio_streams": streams["audio_streams"],
            "subtitle_streams": streams["subtitle_streams"],
            "video_streams": streams["video_streams"],
        }

    def extract_audio_mp3(self, path, out_path, start, duration, timeout,
                          stop_check=None):
        """按时间范围抽取 16kHz 单声道 32kbps MP3（临时文件）。"""
        args = ["-nostdin", "-hide_banner", "-loglevel", "error",
                "-ss", "%.3f" % float(start), "-i", path,
                "-t", "%.3f" % float(duration),
                "-vn", "-ac", "1", "-ar", str(config.ASR_AUDIO_RATE),
                "-b:a", "32k", "-f", "mp3", "-y", out_path]
        code, _stdout, stderr = self._run(args, timeout, stop_check)
        if (code != 0 or not os.path.isfile(out_path)
                or os.path.getsize(out_path) <= 0):
            detail = stderr.decode("utf-8", "replace").strip()[-300:]
            raise VideoSkip("decode", detail or "audio chunk extract failed")

    def extract_audio_remux(self, path, out_path, start, duration, timeout,
                            stop_check=None):
        """只解封装、不解码：流复制抽取音轨（v0032 “服务器解码”模式）。

        NAS 侧只做 demux + copy，CPU 占用远低于解码转码；输出的 mka 交给
        ASR 服务器（自带 ffmpeg）解码。音轨缺失/容器不支持时抛 VideoSkip。
        """
        args = ["-nostdin", "-hide_banner", "-loglevel", "error",
                "-ss", "%.3f" % float(start), "-i", path,
                "-t", "%.3f" % float(duration),
                "-map", "0:a:0?", "-vn", "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                "-f", "matroska", "-y", out_path]
        code, _stdout, stderr = self._run(args, timeout, stop_check)
        if (code != 0 or not os.path.isfile(out_path)
                or os.path.getsize(out_path) <= 0):
            detail = stderr.decode("utf-8", "replace").strip()[-300:]
            raise VideoSkip("decode", detail or "audio remux failed")

    def extract_audio_pcm(self, path, seconds, rate, timeout, stop_check=None):
        """抽取 s16le 单声道 PCM（v0047 音频包络签名用）。

        注意：**不能直接把 PCM 写到 stdout**——`_run` 采用“轮询后 communicate”的模式，
        Windows 匿名管道缓冲区较小，ffmpeg 写满就会被阻塞导致超时。这里先写临时文件
        再读入内存（180s@8kHz 约 2.8MB），读完即删。
        音频缺失/解码失败抛 VideoSkip('audio', ...)，由调用方降级为“无音频签名”。
        """
        try:
            seconds = max(1.0, float(seconds))
            rate = max(8000, min(48000, int(rate)))
        except (TypeError, ValueError):
            seconds, rate = float(config.VIDEO_AUDIO_SECONDS), config.VIDEO_AUDIO_RATE
        tempdir = tempfile.mkdtemp(prefix="dc-audio-pcm-")
        output = os.path.join(tempdir, "audio.s16le")
        try:
            args = ["-nostdin", "-hide_banner", "-loglevel", "error",
                    "-threads", "1", "-i", path,
                    "-t", "%.1f" % seconds, "-vn", "-ac", "1",
                    "-ar", str(rate), "-f", "s16le", "-y", output]
            code, _stdout, stderr = self._run(args, timeout, stop_check)
            if code != 0 or not os.path.isfile(output):
                detail = stderr.decode("utf-8", "replace").strip()[-300:]
                raise VideoSkip("audio", detail or ("audio pcm exit=%s" % code))
            with open(output, "rb") as handle:
                data = handle.read()
            if not data:
                raise VideoSkip("audio", u"音频采样为空")
            return data
        except OSError as exc:
            raise VideoSkip("audio", u"读取音频采样失败: %s" % exc)
        finally:
            shutil.rmtree(tempdir, ignore_errors=True)

    def extract_silence_mp3(self, out_path, seconds=1.0, timeout=15.0):
        """用 lavfi anullsrc 生成指定秒数的 16kHz 单声道静音 MP3。

        v0026 云端字幕“测试连接”专用：不上传任何真实用户音频。
        失败抛 VideoSkip('audio', ...)，由字幕服务转成 AUDIO_FAILED。
        """
        try:
            duration = max(0.1, float(seconds))
        except (TypeError, ValueError):
            duration = 1.0
        args = ["-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                "-t", "%.3f" % duration,
                "-vn", "-ac", "1", "-ar", str(config.ASR_AUDIO_RATE),
                "-b:a", "32k", "-f", "mp3", "-y", out_path]
        code, _stdout, stderr = self._run(args, timeout)
        if (code != 0 or not os.path.isfile(out_path)
                or os.path.getsize(out_path) <= 0):
            detail = stderr.decode("utf-8", "replace").strip()[-300:]
            raise VideoSkip("audio",
                            detail or "silence audio generation failed")

    def extract_frame(self, path, timestamp, timeout, hwaccel_label="none",
                      stop_check=None):
        args = ["-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1"]
        args += _hwaccel_flag_args(hwaccel_label)
        args += ["-ss", "%.6f" % float(timestamp), "-i", path,
                 "-frames:v", "1", "-vf", VIDEO_FILTER,
                 "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        code, stdout, stderr = self._run(args, timeout, stop_check)
        if code != 0 or not stdout:
            detail = stderr.decode("utf-8", "replace").strip()[-300:]
            raise VideoSkip("decode", detail or ("ffmpeg exit=%s" % code))
        if len(stdout) < config.VIDEO_FRAME_BYTES:
            raise VideoSkip("decode", u"帧数据不足 %d 字节" % len(stdout))
        return stdout[:config.VIDEO_FRAME_BYTES]

    def extract_thumbnail(self, path, out_path, timestamp, width,
                          timeout=60.0, hwaccel_label="none", stop_check=None):
        """按时间点抽 1 帧缩放为 JPEG（v0044 预览缩略图）。

        与 `extract_frame`（32×32 灰度指纹）不同：这里输出可直接给浏览器显示的
        JPEG 文件，宽度上限由调用方控制，保持等比。
        """
        width = max(64, int(width or 320))
        args = ["-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1"]
        args += _hwaccel_flag_args(hwaccel_label)
        args += ["-ss", "%.3f" % max(0.0, float(timestamp)), "-i", path,
                 "-frames:v", "1", "-an", "-sn",
                 "-vf", "scale=w='min(%d,iw)':h=-2" % width,
                 "-q:v", str(config.THUMB_JPEG_QUALITY),
                 "-f", "image2", "-y", out_path]
        code, _stdout, stderr = self._run(args, timeout, stop_check)
        if code != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
            detail = stderr.decode("utf-8", "replace").strip()[-300:]
            raise VideoSkip("decode", detail or ("ffmpeg thumbnail exit=%s" % code))
        return out_path

    def extract_frames_batch(self, path, timestamps, timeout,
                             hwaccel_label="none", stop_check=None):
        """单进程顺序抽取 2-4 帧（每帧一个 rawvideo 输出文件）。

        ffmpeg 只打开/输入 seek 一次（-ss 定位到首帧），其余帧用 output -ss
        相对偏移，避免每个时间戳单独起进程；返回按输入顺序的帧字节列表。
        任一输出缺失/不足一帧则抛 VideoSkip，由调用方回退逐帧抽取。
        """
        timestamps = [float(item) for item in timestamps]
        if not timestamps:
            return []
        tempdir = tempfile.mkdtemp(prefix="dc-batch-frames-")
        try:
            outputs = [os.path.join(tempdir, "frame-%03d.raw" % index)
                       for index in range(len(timestamps))]
            base_ts = timestamps[0]
            args = ["-nostdin", "-hide_banner", "-loglevel", "error",
                    "-threads", "1"]
            args += _hwaccel_flag_args(hwaccel_label)
            args += ["-ss", "%.6f" % base_ts, "-i", path]
            for index, (timestamp, out_path) in enumerate(
                    zip(timestamps, outputs)):
                if index > 0:
                    args += ["-ss", "%.6f" % max(0.0, timestamp - base_ts)]
                args += ["-frames:v", "1", "-vf", VIDEO_FILTER,
                         "-f", "rawvideo", "-pix_fmt", "gray", "-y", out_path]
            code, stdout, stderr = self._run(args, timeout, stop_check)
            frames = self._read_batch_outputs(outputs, code, stdout)
            if frames is not None:
                return frames
            detail = stderr.decode("utf-8", "replace").strip()[-300:]
            raise VideoSkip("decode", detail or ("batch ffmpeg exit=%s" % code))
        finally:
            shutil.rmtree(tempdir, ignore_errors=True)

    @staticmethod
    def _read_batch_outputs(outputs, code, stdout):
        """读取批量输出；兼容把 N 帧写 stdout 的测试 ffmpeg。失败返回 None。"""
        frame_bytes = config.VIDEO_FRAME_BYTES
        frames = []
        for out_path in outputs:
            try:
                with open(out_path, "rb") as handle:
                    data = handle.read(frame_bytes + 1)
            except OSError:
                data = b""
            if len(data) < frame_bytes:
                frames = None
                break
            frames.append(data[:frame_bytes])
        if frames is not None:
            return frames
        if code == 0 and stdout and len(stdout) >= frame_bytes * len(outputs):
            return [stdout[index * frame_bytes:(index + 1) * frame_bytes]
                    for index in range(len(outputs))]
        return None

    def extract_audio(self, path, audio_path, max_sec, timeout,
                      stop_check=None):
        """抽取 16k mono WAV（临时文件，-t 限制最大秒数），供外部 ASR 使用。"""
        args = ["-nostdin", "-hide_banner", "-loglevel", "error", "-i", path,
                "-vn", "-ac", "1", "-ar", str(config.ASR_AUDIO_RATE),
                "-t", "%d" % int(max_sec), "-f", "wav", "-y", audio_path]
        code, _stdout, stderr = self._run(args, timeout, stop_check)
        if (code != 0 or not os.path.isfile(audio_path)
                or os.path.getsize(audio_path) <= 0):
            detail = stderr.decode("utf-8", "replace").strip()[-300:]
            raise VideoSkip("decode", detail or "audio extract failed")


def split_asr_command(template):
    """把命令模板拆成参数数组（无 shell；Windows 保留反斜杠路径）。"""
    if not template:
        return []
    posix = os.name != "nt"
    try:
        tokens = shlex.split(template, posix=posix)
    except ValueError:
        return []
    if not posix:
        cleaned = []
        for token in tokens:
            if (len(token) >= 2 and token[0] == token[-1]
                    and token[0] in ("'", '"')):
                token = token[1:-1]
            cleaned.append(token)
        tokens = cleaned
    return tokens


def asr_availability(options, ffmpeg_available=None):
    """返回 ASR 可用性（不启动外部命令，只做配置/可执行性判断）。"""
    options = options or {}
    enabled = bool(options.get("video_asr_enabled", False))
    template = (options.get("video_asr_command") or "").strip()
    weight = float(options.get("video_asr_weight")
                   if options.get("video_asr_weight") is not None
                   else config.ASR_DEFAULT_WEIGHT)
    result = {"enabled": enabled, "configured": bool(template),
              "available": False, "reason": "disabled", "weight": weight}
    if not enabled:
        return result
    if not template:
        result["reason"] = "no_command"
        return result
    tokens = split_asr_command(template)
    if not tokens:
        result["reason"] = "invalid_command"
        return result
    executable = tokens[0]
    found = (os.path.isfile(executable)
             and os.access(executable, os.X_OK)) or (
        shutil.which(executable) is not None)
    if not found:
        result["reason"] = "command_not_found"
        return result
    if ffmpeg_available is False:
        result["reason"] = "no_ffmpeg"
        return result
    result["available"] = True
    result["reason"] = "ok"
    return result


def run_external_command(command, cwd, timeout, stop_check=None):
    """执行外部命令（参数数组、无 shell），超时/停止时 kill。"""
    try:
        process = subprocess.Popen(command, cwd=cwd,
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
    except OSError as exc:
        raise VideoSkip("error", u"无法启动 ASR 命令: %s" % exc)
    deadline = time.monotonic() + max(1.0, float(timeout))
    try:
        while process.poll() is None:
            if stop_check is not None and stop_check():
                _kill_process(process)
                raise VideoStopped()
            if time.monotonic() >= deadline:
                _kill_process(process)
                raise VideoTimeout()
            time.sleep(0.05)
        stdout, stderr = process.communicate()
    except BaseException:
        _kill_process(process)
        raise
    return process.returncode, stdout or b"", stderr or b""


def collect_asr_text(stdout, outdir):
    """优先 stdout；否则取 outdir 下第一个 .txt（只供内部签名，绝不外泄）。"""
    text = (stdout or b"").decode("utf-8", "replace").strip()
    if text:
        return text
    try:
        names = sorted(os.listdir(outdir))
    except OSError:
        return ""
    for name in names:
        if not name.lower().endswith(".txt"):
            continue
        path = os.path.join(outdir, name)
        try:
            if os.path.getsize(path) > 4 * 1024 * 1024:
                continue
            with open(path, "rb") as handle:
                return handle.read().decode("utf-8", "replace").strip()
        except OSError:
            continue
    return ""


def probe_ffmpeg(runtime_config=None, settings=None):
    """探测 ffmpeg 能力（进程内缓存，/api/meta 与 /api/diagnostics 共用）。

    解析优先级见 FFmpegTool.resolve；缓存键包含解析出的路径/来源/
    settings 配置路径，settings.ffmpeg_path 改变会得到新的探测结果。
    返回字段兼容旧键（ffmpeg_path/ffmpeg_version），并新增
    path/version/source/configured_path。
    """
    provider = (lambda: settings) if isinstance(settings, dict) else None
    tool = FFmpegTool(settings_provider=provider,
                      runtime_config=runtime_config)
    resolved = tool.resolve(runtime_config)
    path = (resolved[0] if resolved else "") or ""
    source = tool.source
    configured = tool.configured_path
    key = "%s|%s|%s" % (path, source, configured)
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(key)
        if cached is not None:
            return dict(cached)
    info = {
        "available": bool(path),
        "path": path,
        "ffmpeg_path": path,
        "version": "unavailable",
        "ffmpeg_version": "unavailable",
        "hwaccels": [],
        "source": source,
        "configured_path": configured,
    }
    if path:
        info["version"] = tool.version()
        info["ffmpeg_version"] = info["version"]
        info["hwaccels"] = tool.hwaccels()
    with _PROBE_LOCK:
        _PROBE_CACHE[key] = dict(info)
    return dict(info)


def video_cache_stats(runtime_config):
    """视频指纹缓存统计（诊断用；只读计数，不创建/迁移缓存文件）。"""
    path = os.path.join(runtime_config.data_dir, config.VIDEO_CACHE_FILENAME)
    result = {"path": path, "available": False, "entries": 0, "size": 0}
    try:
        result["size"] = int(os.path.getsize(path))
    except OSError:
        return result
    result["available"] = True
    try:
        connection = sqlite3.connect(path, timeout=2.0)
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM video_cache").fetchone()
            result["entries"] = int(row[0]) if row is not None else 0
        except sqlite3.Error:
            result["entries"] = 0
        finally:
            connection.close()
    except sqlite3.Error:
        pass
    return result


def video_capabilities(runtime_config, settings=None):
    """视频能力（/api/meta）：保留旧键，新增外部 FFmpeg 与硬解模式信息。"""
    settings = settings or {}
    info = probe_ffmpeg(runtime_config, settings)
    accels = info.get("hwaccels") or []
    mode = str(settings.get("video_hwaccel") or "auto").strip().lower()
    if mode not in config.VIDEO_HWACCEL_MODES:
        mode = "auto"
    report_label, _flag, fallback = hwaccel_for_mode(mode, accels)
    asr = asr_availability(settings, info.get("available"))
    return {
        "enabled": bool(settings.get("video_similarity_enabled", False)),
        "ffmpeg_path": info.get("path") or "",
        "ffmpeg_version": info.get("version") or "unavailable",
        "hwaccels": accels,
        "gpu_hint": bool(accels),
        # 外部 FFmpeg 路径与来源（settings/env/which/unavailable）
        "ffmpeg": {
            "available": bool(info.get("available")),
            "path": info.get("path") or "",
            "version": info.get("version") or "unavailable",
            "source": info.get("source") or "unavailable",
            "configured_path": info.get("configured_path") or "",
            "hwaccels": list(accels),
        },
        # 硬解模式：配置值、实际生效标签、显式模式不支持时的降级标记
        "hwaccel_modes": list(config.VIDEO_HWACCEL_MODES),
        "hwaccel_mode": mode,
        "hwaccel_effective": report_label,
        "hwaccel_fallback": bool(fallback),
        # 当前 ffmpeg 实际支持的显式模式（UI 据此启用下拉项）：
        # nvdec 在 NVIDIA 上通常以 cuda/cuvid 出现在 -hwaccels。
        "hwaccel_supported": hwaccel_supported(accels),
        "asr": {
            "enabled": asr["enabled"],
            "configured": asr["configured"],
            "available": asr["available"],
            "reason": asr["reason"],
            "weight": asr["weight"],
        },
        "defaults": {
            "ffmpeg_path": "",
            "video_similarity_enabled": False,
            "video_extensions": list(config.DEFAULT_VIDEO_EXTENSIONS),
            "video_sample_frames": config.VIDEO_DEFAULT_SAMPLE_FRAMES,
            "video_max_sample_interval_sec": config.VIDEO_DEFAULT_MAX_INTERVAL,
            "video_similarity_threshold": config.VIDEO_DEFAULT_THRESHOLD,
            "video_concurrency": 1,
            "video_hwaccel": "auto",
            "video_max_file_mb": config.VIDEO_DEFAULT_MAX_FILE_MB,
            "video_timeout_sec": config.VIDEO_DEFAULT_TIMEOUT_SEC,
            "video_cache": True,
            "video_asr_enabled": False,
            "video_asr_command": "",
            "video_asr_weight": config.ASR_DEFAULT_WEIGHT,
            "video_asr_timeout_sec": config.ASR_DEFAULT_TIMEOUT_SEC,
            "video_asr_max_sec": config.ASR_DEFAULT_MAX_SEC,
        },
    }


# ---------------------------------------------------------------------------
# SQLite 指纹缓存
# ---------------------------------------------------------------------------

class VideoCache(object):
    """<var>/video_cache.sqlite3（最多 20000 条 LRU，失败标记避免重复解码）。"""

    def __init__(self, path, max_entries=None, enabled=True):
        self.path = path
        self.max_entries = int(max_entries or config.VIDEO_CACHE_MAX_ENTRIES)
        self.enabled = bool(enabled)
        self._lock = threading.RLock()
        self._conn = None
        if self.enabled:
            self._connect()

    def _connect(self):
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=10.0,
                                         check_same_thread=False)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS video_cache ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                "created_at REAL NOT NULL, last_used REAL NOT NULL)")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_video_cache_last_used "
                "ON video_cache(last_used)")
            connection.commit()
            self._conn = connection
        except sqlite3.Error:
            self._conn = None

    @property
    def available(self):
        return self._conn is not None

    def get(self, key):
        if not self.enabled or self._conn is None:
            return None
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT value FROM video_cache WHERE key=?", (key,)).fetchone()
                if row is None:
                    return None
                self._conn.execute(
                    "UPDATE video_cache SET last_used=? WHERE key=?",
                    (time.time(), key))
                self._conn.commit()
            except sqlite3.Error:
                return None
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return None

    def put(self, key, value):
        if not self.enabled or self._conn is None:
            return False
        payload = json.dumps(value, ensure_ascii=False)
        now = time.time()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO video_cache"
                    "(key, value, created_at, last_used) VALUES(?,?,?,?)",
                    (key, payload, now, now))
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM video_cache").fetchone()[0]
                if count > self.max_entries:
                    self._conn.execute(
                        "DELETE FROM video_cache WHERE key IN ("
                        "SELECT key FROM video_cache ORDER BY last_used ASC "
                        "LIMIT ?)", (count - self.max_entries,))
                self._conn.commit()
                return True
            except sqlite3.Error:
                return False

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None


# ---------------------------------------------------------------------------
# 分析引擎
# ---------------------------------------------------------------------------

def cache_key(identity, sample_frames, max_interval):
    return "%s|%d|%d|%d|%d|%d|%d" % (
        config.VIDEO_ALGO_VERSION, int(identity["size"]),
        int(identity["mtime_ns"]), int(identity["inode"]),
        int(identity["dev"]), int(sample_frames), int(max_interval))


class VideoEngine(object):
    def __init__(self, runtime_config, ffmpeg=None, ffmpeg_cmd=None,
                 cache=None, timeout_cap=None, asr_timeout_cap=None,
                 settings_provider=None):
        self.config = runtime_config
        # settings provider 用于解析外部 ffmpeg 路径（settings.ffmpeg_path），
        # 也供 capabilities() 返回当前配置；显式 ffmpeg/cmd 时优先级最高。
        self.settings_provider = settings_provider
        if ffmpeg is None:
            ffmpeg = FFmpegTool(cmd=ffmpeg_cmd,
                                settings_provider=settings_provider)
            ffmpeg.resolve(runtime_config)
        self.ffmpeg = ffmpeg
        self.timeout_cap = timeout_cap
        self.asr_timeout_cap = asr_timeout_cap
        self.cache = cache
        self.ffmpeg_used = 0
        self.asr_used = 0
        self.audio_used = 0
        self._lock = threading.RLock()

    def ensure_cache(self, options):
        if not options.get("video_cache", True):
            return None
        if self.cache is None:
            self.cache = VideoCache(
                os.path.join(self.config.data_dir, config.VIDEO_CACHE_FILENAME))
        return self.cache

    def capabilities(self, settings=None):
        if settings is None and self.settings_provider is not None:
            try:
                settings = self.settings_provider()
            except Exception:
                settings = None
        return video_capabilities(self.config, settings)

    @staticmethod
    def _frame_batch_size(options):
        try:
            value = options.get("video_batch_frames")
            if value is None:
                value = config.VIDEO_BATCH_FRAMES
            return max(1, min(4, int(value)))
        except (TypeError, ValueError):
            return max(1, min(4, int(config.VIDEO_BATCH_FRAMES)))

    def _analyze_visual(self, path, options, stop_check=None):
        """视觉分析（批处理抽帧 + 帧指纹 + 缓存），返回 dict。"""
        identity = fsutil_identity(path)
        if identity is None:
            raise VideoSkip("error", u"文件不存在")
        max_mb = int(options.get("video_max_file_mb") or config.VIDEO_DEFAULT_MAX_FILE_MB)
        if identity["size"] > max_mb * 1024 * 1024:
            raise VideoSkip("size", u"超过视频相似分析大小上限")
        if not self.ffmpeg.available():
            raise VideoSkip("error", u"未找到 ffmpeg")
        sample_frames = int(options.get("video_sample_frames")
                            or config.VIDEO_DEFAULT_SAMPLE_FRAMES)
        max_interval = int(options.get("video_max_sample_interval_sec")
                           or config.VIDEO_DEFAULT_MAX_INTERVAL)
        hwaccel_mode = options.get("video_hwaccel") or "auto"
        timeout = float(options.get("video_timeout_sec")
                        or config.VIDEO_DEFAULT_TIMEOUT_SEC)
        if self.timeout_cap:
            timeout = min(timeout, float(self.timeout_cap))
        cache = self.ensure_cache(options)
        key = cache_key(identity, sample_frames, max_interval)
        if cache is not None:
            cached = cache.get(key)
            if isinstance(cached, dict):
                if cached.get("failed"):
                    raise VideoSkip(cached.get("reason") or "error",
                                    cached.get("detail") or "")
                result = dict(cached)
                result["cache_hit"] = True
                return result

        try:
            duration = self.ffmpeg.probe_duration(path, timeout, stop_check)
        except VideoTimeout:
            self._cache_failure(cache, key, "timeout", u"探测时长超时")
            raise VideoSkip("timeout", u"探测时长超时")
        except VideoStopped:
            raise
        except VideoSkip as exc:
            self._cache_failure(cache, key, exc.reason, exc.detail)
            raise
        if duration is None or duration <= 0:
            self._cache_failure(cache, key, "no_duration", u"无法解析视频时长")
            raise VideoSkip("no_duration", u"无法解析视频时长")

        n_eff = compute_n_eff(duration, sample_frames, max_interval)
        warning = (duration / n_eff) > max_interval
        # 硬解解析：auto=沿用 hwaccel_label()（ffmpeg 仍用 -hwaccel auto）；
        # 显式模式=直接 -hwaccel <name>；不支持则回退 none 并标记 fallback。
        hwaccel_mode = str(hwaccel_mode or "auto").strip().lower()
        if hwaccel_mode not in config.VIDEO_HWACCEL_MODES:
            hwaccel_mode = "auto"
        hw_label, hw_flag, hw_fallback = hwaccel_for_mode(
            hwaccel_mode, self.ffmpeg.hwaccels())
        frames = []
        center_frames = []
        software_fallback = False
        batch_size = self._frame_batch_size(options)
        timestamps = [(index + 0.5) * duration / n_eff
                      for index in range(n_eff)]
        index = 0
        while index < n_eff:
            if stop_check is not None and stop_check():
                raise VideoStopped()
            batch_ts = timestamps[index:index + batch_size]
            raws = None
            if len(batch_ts) > 1:
                while True:
                    label = "none" if software_fallback else hw_flag
                    try:
                        raws = self.ffmpeg.extract_frames_batch(
                            path, batch_ts, timeout, label, stop_check)
                        break
                    except VideoTimeout:
                        self._cache_failure(cache, key, "timeout", u"抽帧超时")
                        raise VideoSkip("timeout", u"抽帧超时")
                    except VideoStopped:
                        raise
                    except VideoSkip as exc:
                        if not software_fallback and hw_flag != "none":
                            # 硬件解码失败 → 软件解码重试同一批
                            software_fallback = True
                            hw_label = "none"
                            hw_flag = "none"
                            continue
                        raws = None
                        break
            if raws is None:
                # 批处理失败/单帧：回退逐帧抽取（保持原顺序）
                raws = []
                for timestamp in batch_ts:
                    if stop_check is not None and stop_check():
                        raise VideoStopped()
                    while True:
                        label = "none" if software_fallback else hw_flag
                        try:
                            raw = self.ffmpeg.extract_frame(
                                path, timestamp, timeout, label, stop_check)
                            break
                        except VideoTimeout:
                            self._cache_failure(cache, key, "timeout",
                                                u"抽帧超时")
                            raise VideoSkip("timeout", u"抽帧超时")
                        except VideoStopped:
                            raise
                        except VideoSkip as exc:
                            if not software_fallback and hw_flag != "none":
                                software_fallback = True
                                hw_label = "none"
                                hw_flag = "none"
                                continue
                            self._cache_failure(cache, key, "decode",
                                                exc.detail)
                            raise VideoSkip("decode", exc.detail)
                    raws.append(raw)
            for raw in raws:
                try:
                    full_digest, center_digest = phash_from_gray_pair(raw)
                except ValueError as exc:
                    self._cache_failure(cache, key, "decode", str(exc))
                    raise VideoSkip("decode", str(exc))
                frames.append(full_digest)
                center_frames.append(center_digest)
            index += len(batch_ts)
        hex_frames = ["%016x" % value for value in frames]
        hex_center = ["%016x" % value for value in center_frames]
        fingerprint = "v2:%d:%s" % (n_eff, "".join(hex_frames))
        result = {
            "duration": float(duration),
            "n_eff": int(n_eff),
            "frame_hashes": frames,
            "frame_hashes_center": center_frames,
            "frame_hex": hex_frames,
            "frame_hex_center": hex_center,
            "fingerprint": fingerprint,
            "fingerprint_version": int(config.VIDEO_FINGERPRINT_VERSION),
            "center_crop_ratio": float(config.VIDEO_CENTER_CROP_RATIO),
            "hwaccel": hw_label,
            "hwaccel_mode": hwaccel_mode,
            "hwaccel_fallback": bool(hw_fallback or software_fallback),
            "low_confidence": False,
            "interval": duration / n_eff,
            "interval_warning": bool(warning),
            "source_size": identity["size"],
            "created_at": time.time(),
        }
        if cache is not None:
            cache.put(key, result)
        with self._lock:
            self.ffmpeg_used += 1
        result["cache_hit"] = False
        return result

    # ------------------------------------------------------------------
    # 可选 ASR 辅助（失败 open：绝不因 ASR 失败而让视频分析失败）
    # ------------------------------------------------------------------
    def analyze(self, path, options, stop_check=None):
        """视觉分析 + 可选音频包络签名 + 可选 ASR 文本签名（均不返回文本）。"""
        visual = self._analyze_visual(path, options, stop_check)
        identity = fsutil_identity(path)
        # v0047：可选音频包络签名（失败 open：不影响视觉结果）
        audio_info = {"audio_signature_status": "disabled",
                      "audio_signature": None, "audio_cache_hit": False}
        if identity is not None and options.get("video_audio_signature_enabled"):
            try:
                audio_info = self._analyze_audio_signature(
                    path, identity, options, self.ensure_cache(options),
                    stop_check)
            except VideoStopped:
                raise
            except Exception:  # pragma: no cover - 防御性兜底
                audio_info = {"audio_signature_status": "failed",
                              "audio_signature": None,
                              "audio_cache_hit": False}
        visual.update(audio_info)
        if audio_info.get("audio_signature_status") == "ok":
            with self._lock:
                self.audio_used += 1
        asr_info = {"asr_status": "disabled", "asr_signature": None,
                    "asr_cache_hit": False}
        if identity is not None and options.get("video_asr_enabled"):
            try:
                asr_info = self._analyze_asr(path, identity, options,
                                             self.ensure_cache(options),
                                             stop_check)
            except VideoStopped:
                raise
            except Exception:
                asr_info = {"asr_status": "failed", "asr_signature": None,
                            "asr_cache_hit": False}
        visual.update(asr_info)
        # 删除安全：内容快速校验哈希（size+head/tail 64KB SHA256）。
        # 每次 analyze 都按当前文件重新计算（缓存命中也会补算，兼容旧缓存）。
        try:
            visual["content_quick_hash"] = content_quick_hash(path)
        except (OSError, IOError):
            visual["content_quick_hash"] = None
        if visual.get("asr_signature"):
            with self._lock:
                self.asr_used += 1
        return visual

    def _audio_signature_key(self, identity):
        return "audio-v%d|%d|%d|%d|%d|%d" % (
            config.VIDEO_AUDIO_SIGNATURE_VERSION,
            int(identity["size"]), int(identity["mtime_ns"]),
            int(identity["inode"]), int(identity["dev"]),
            int(config.VIDEO_AUDIO_BUCKETS))

    def _analyze_audio_signature(self, path, identity, options, cache,
                                 stop_check=None):
        """可选音频包络签名（v0047）：ffmpeg 解 PCM → 分桶 RMS → 0..255 签名。

        结果缓存（<var>/cache/video.sqlite）；任何失败都降级为 status=failed，
        绝不影响视觉分析或删除安全。
        """
        result = {"audio_signature_status": "disabled",
                  "audio_signature": None, "audio_cache_hit": False}
        if not options.get("video_audio_signature_enabled"):
            return result
        if not self.ffmpeg.available():
            result["audio_signature_status"] = "unavailable"
            return result
        key = self._audio_signature_key(identity)
        if cache is not None:
            cached = cache.get(key)
            if isinstance(cached, dict):
                if cached.get("failed"):
                    result["audio_signature_status"] = "failed"
                    result["audio_signature_detail"] = cached.get("detail") or ""
                    return result
                signature = cached.get("signature")
                if isinstance(signature, list) and len(signature) >= 8:
                    result["audio_signature_status"] = "ok"
                    result["audio_signature"] = signature
                    result["audio_cache_hit"] = True
                    return result
        timeout = float(options.get("video_timeout_sec")
                        or config.VIDEO_DEFAULT_TIMEOUT_SEC)
        if self.timeout_cap:
            timeout = min(timeout, float(self.timeout_cap))
        timeout = min(timeout, float(config.VIDEO_AUDIO_TIMEOUT_SEC))
        try:
            pcm = self.ffmpeg.extract_audio_pcm(
                path, config.VIDEO_AUDIO_SECONDS, config.VIDEO_AUDIO_RATE,
                timeout, stop_check)
        except VideoStopped:
            raise
        except VideoSkip as exc:
            if cache is not None:
                cache.put(key, {"failed": True, "reason": "audio",
                                "detail": exc.detail or ""})
            result["audio_signature_status"] = "failed"
            result["audio_signature_detail"] = exc.detail or ""
            return result
        signature = audio_signature_from_pcm(
            pcm, config.VIDEO_AUDIO_BUCKETS, config.VIDEO_AUDIO_RATE)
        if len(signature) < 8:
            if cache is not None:
                cache.put(key, {"failed": True, "reason": "empty",
                                "detail": u"音频采样不足"})
            result["audio_signature_status"] = "failed"
            result["audio_signature_detail"] = u"音频采样不足"
            return result
        if cache is not None:
            cache.put(key, {"signature": signature})
        with self._lock:
            self.ffmpeg_used += 1
        result["audio_signature_status"] = "ok"
        result["audio_signature"] = signature
        return result

    def _asr_temp_root(self):
        directory = os.path.join(self.config.data_dir, "tmp")
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            directory = None
        return directory

    def _asr_cache_key(self, identity, options):
        template = (options.get("video_asr_command") or "").strip()
        fingerprint = hashlib.sha256(template.encode("utf-8")).hexdigest()[:16]
        max_sec = int(options.get("video_asr_max_sec")
                      or config.ASR_DEFAULT_MAX_SEC)
        return "%s|%s|%d|%d|%d|%d|%d" % (
            config.ASR_ALGO_VERSION, fingerprint,
            int(identity["size"]), int(identity["mtime_ns"]),
            int(identity["inode"]), int(identity["dev"]), max_sec)

    def _asr_fail(self, cache, key, status):
        if cache is not None:
            cache.put(key, {"asr_status": status, "signature": None,
                            "created_at": time.time()})
        return {"asr_status": status, "asr_signature": None,
                "asr_cache_hit": False}

    def _analyze_asr(self, path, identity, options, cache, stop_check=None):
        availability = asr_availability(options, self.ffmpeg.available())
        if not availability["available"]:
            return {"asr_status": availability["reason"],
                    "asr_signature": None, "asr_cache_hit": False}
        key = self._asr_cache_key(identity, options)
        if cache is not None:
            cached = cache.get(key)
            if isinstance(cached, dict):
                status = cached.get("asr_status") or "failed"
                if status == "ok" and cached.get("signature"):
                    return {"asr_status": "ok",
                            "asr_signature": cached.get("signature"),
                            "asr_cache_hit": True}
                if status != "ok":
                    return {"asr_status": status, "asr_signature": None,
                            "asr_cache_hit": True}
        timeout = float(options.get("video_asr_timeout_sec")
                        or config.ASR_DEFAULT_TIMEOUT_SEC)
        if self.asr_timeout_cap:
            timeout = min(timeout, float(self.asr_timeout_cap))
        max_sec = int(options.get("video_asr_max_sec")
                      or config.ASR_DEFAULT_MAX_SEC)
        tempdir = tempfile.mkdtemp(prefix="dc-asr-", dir=self._asr_temp_root())
        try:
            audio_path = os.path.join(tempdir, "audio.wav")
            outdir = os.path.join(tempdir, "out")
            os.makedirs(outdir, exist_ok=True)
            try:
                self.ffmpeg.extract_audio(path, audio_path, max_sec,
                                          timeout, stop_check)
            except VideoTimeout:
                return self._asr_fail(cache, key, "timeout")
            except VideoStopped:
                raise
            except VideoSkip:
                return self._asr_fail(cache, key, "extract_failed")
            tokens = split_asr_command(options.get("video_asr_command") or "")
            if not tokens:
                return self._asr_fail(cache, key, "unavailable")
            stem = os.path.splitext(os.path.basename(path))[0]
            command = []
            for token in tokens:
                command.append(token.replace("{audio}", audio_path)
                               .replace("{outdir}", outdir)
                               .replace("{stem}", stem))
            try:
                code, stdout, _stderr = run_external_command(
                    command, outdir, timeout, stop_check)
            except VideoTimeout:
                return self._asr_fail(cache, key, "timeout")
            except VideoStopped:
                raise
            except VideoSkip:
                return self._asr_fail(cache, key, "failed")
            if code != 0:
                return self._asr_fail(cache, key, "failed")
            text = collect_asr_text(stdout, outdir)
            if not text:
                return self._asr_fail(cache, key, "no_speech")
            normalized = similarity.normalize_text(text)
            if len(normalized) < config.ASR_MIN_TEXT_CHARS:
                return self._asr_fail(cache, key, "no_speech")
            signature = similarity.minhash_signature(
                similarity.text_shingles(normalized))
            if not signature:
                return self._asr_fail(cache, key, "no_speech")
            if cache is not None:
                cache.put(key, {"asr_status": "ok",
                                "signature": signature,
                                "created_at": time.time()})
            # 隐私：只返回签名，绝不返回转写文本
            return {"asr_status": "ok", "asr_signature": signature,
                    "asr_cache_hit": False}
        finally:
            shutil.rmtree(tempdir, ignore_errors=True)

    @staticmethod
    def _cache_failure(cache, key, reason, detail):
        if cache is None:
            return
        cache.put(key, {"failed": True, "reason": reason,
                        "detail": detail or "",
                        "created_at": time.time()})


def fsutil_identity(path):
    from . import fsutil
    return fsutil.file_identity(path)


def video_extension_set(options):
    extensions = options.get("video_extensions") or list(config.DEFAULT_VIDEO_EXTENSIONS)
    return frozenset(str(item).lower() for item in extensions)


def is_video_path(path, options):
    extension = os.path.splitext(path)[1].lower()
    return extension in video_extension_set(options)
