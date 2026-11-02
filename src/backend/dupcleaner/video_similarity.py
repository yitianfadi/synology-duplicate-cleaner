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

import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time

from . import config

DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
VIDEO_FILTER = ("scale=32:32:force_original_aspect_ratio=decrease,"
                "pad=32:32:(ow-iw)/2:(oh-ih)/2:black,format=gray")

_MASK64 = 0xFFFFFFFFFFFFFFFF
_PROBE_LOCK = threading.RLock()
_PROBE_CACHE = {}
_COS_TABLE = None


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


def build_video_groups(items, threshold):
    """安全团分组：任意两两分数 >= 阈值。items 需含 analysis.frame_hashes。"""
    threshold = max(0, min(100, int(threshold)))
    count = len(items)
    pairs = {}
    for left in range(count):
        for right in range(left + 1, count):
            first = items[left]["analysis"]
            second = items[right]["analysis"]
            duration_a = float(first.get("duration") or 0)
            duration_b = float(second.get("duration") or 0)
            if duration_a <= 0 or duration_b <= 0:
                continue
            ratio = duration_a / duration_b
            if ratio < config.VIDEO_DURATION_RATIO_MIN or ratio > config.VIDEO_DURATION_RATIO_MAX:
                continue
            frames_a = first.get("frame_hashes") or []
            frames_b = second.get("frame_hashes") or []
            if prefilter_similarity(frames_a, frames_b) < 0.25:
                continue
            result = pair_score(frames_a, frames_b, threshold)
            if result is None:
                continue
            pairs[(left, right)] = result[0]
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
        for left in range(len(members)):
            for right in range(left + 1, len(members)):
                key = (members[left], members[right])
                scores[(members[left], members[right])] = pairs.get(key)
        clusters.append({"indices": members, "scores": scores})
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


class FFmpegTool(object):
    def __init__(self, cmd=None, timeout=180.0):
        self._cmd = list(cmd) if cmd else None
        self.timeout = float(timeout)
        self._lock = threading.RLock()
        self._resolved = False
        self._version = None
        self._hwaccels = None

    def resolve(self, runtime_config=None):
        with self._lock:
            if self._cmd:
                return self._cmd
            if self._resolved:
                return self._cmd
            self._resolved = True
            candidates = []
            env_path = os.environ.get("DUP_FFMPEG_PATH")
            if env_path:
                candidates.append(env_path)
            cfg_path = getattr(runtime_config, "ffmpeg_path", None)
            if cfg_path:
                candidates.append(cfg_path)
            which = shutil.which("ffmpeg")
            if which:
                candidates.append(which)
            for candidate in candidates:
                if not candidate:
                    continue
                if not os.path.isfile(candidate):
                    continue
                if not os.access(candidate, os.X_OK):
                    continue
                self._cmd = [candidate]
                return self._cmd
            return None

    @property
    def path(self):
        with self._lock:
            return self._cmd[0] if self._cmd else ""

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
        accels = self.hwaccels()
        if "vaapi" in accels:
            return "vaapi"
        if "qsv" in accels:
            return "qsv"
        if "nvdec" in accels or "cuda" in accels:
            return "nvdec"
        if "d3d11va" in accels or "dxva2" in accels or "videotoolbox" in accels:
            return "auto"
        return "none"

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

    def extract_frame(self, path, timestamp, timeout, hwaccel_label="none",
                      stop_check=None):
        args = ["-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1"]
        if hwaccel_label and hwaccel_label != "none":
            args += ["-hwaccel", "auto"]
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


def probe_ffmpeg(runtime_config=None):
    """探测 ffmpeg 能力（进程内缓存，/api/meta 可用）。"""
    tool = FFmpegTool()
    resolved = tool.resolve(runtime_config)
    path = resolved[0] if resolved else None
    key = path or ""
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(key)
        if cached is not None:
            return dict(cached)
    info = {"available": bool(path), "ffmpeg_path": path or "",
            "ffmpeg_version": "unavailable", "hwaccels": []}
    if path:
        info["ffmpeg_version"] = tool.version()
        info["hwaccels"] = tool.hwaccels()
    with _PROBE_LOCK:
        _PROBE_CACHE[key] = dict(info)
    return dict(info)


def video_capabilities(runtime_config, settings=None):
    settings = settings or {}
    info = probe_ffmpeg(runtime_config)
    accels = info.get("hwaccels") or []
    return {
        "enabled": bool(settings.get("video_similarity_enabled", False)),
        "ffmpeg_path": info.get("ffmpeg_path") or "",
        "ffmpeg_version": info.get("ffmpeg_version") or "unavailable",
        "hwaccels": accels,
        "gpu_hint": bool(accels),
        "defaults": {
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
                 cache=None, timeout_cap=None):
        self.config = runtime_config
        if ffmpeg is None:
            ffmpeg = FFmpegTool(cmd=ffmpeg_cmd)
            ffmpeg.resolve(runtime_config)
        self.ffmpeg = ffmpeg
        self.timeout_cap = timeout_cap
        self.cache = cache
        self.ffmpeg_used = 0
        self._lock = threading.RLock()

    def ensure_cache(self, options):
        if not options.get("video_cache", True):
            return None
        if self.cache is None:
            self.cache = VideoCache(
                os.path.join(self.config.data_dir, config.VIDEO_CACHE_FILENAME))
        return self.cache

    def capabilities(self, settings=None):
        return video_capabilities(self.config, settings)

    def analyze(self, path, options, stop_check=None):
        """分析一个视频，返回 dict（ok/failed + 指纹字段）。"""
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
        hw_label = "none"
        if hwaccel_mode == "auto":
            hw_label = self.ffmpeg.hwaccel_label()
        frames = []
        software_fallback = False
        index = 0
        while index < n_eff:
            if stop_check is not None and stop_check():
                raise VideoStopped()
            timestamp = (index + 0.5) * duration / n_eff
            label = "none" if software_fallback else hw_label
            try:
                raw = self.ffmpeg.extract_frame(path, timestamp, timeout, label,
                                                stop_check)
            except VideoTimeout:
                self._cache_failure(cache, key, "timeout", u"抽帧超时")
                raise VideoSkip("timeout", u"抽帧超时")
            except VideoStopped:
                raise
            except VideoSkip as exc:
                if not software_fallback and hw_label != "none":
                    # 硬件解码失败 → 自动回退软件解码后重试当前帧
                    software_fallback = True
                    hw_label = "none"
                    continue
                self._cache_failure(cache, key, "decode", exc.detail)
                raise VideoSkip("decode", exc.detail)
            try:
                digest = phash_from_gray(raw)
            except ValueError as exc:
                self._cache_failure(cache, key, "decode", str(exc))
                raise VideoSkip("decode", str(exc))
            frames.append(digest)
            index += 1
        hex_frames = ["%016x" % value for value in frames]
        fingerprint = "v1:%d:%s" % (n_eff, "".join(hex_frames))
        result = {
            "duration": float(duration),
            "n_eff": int(n_eff),
            "frame_hashes": frames,
            "frame_hex": hex_frames,
            "fingerprint": fingerprint,
            "hwaccel": hw_label,
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
