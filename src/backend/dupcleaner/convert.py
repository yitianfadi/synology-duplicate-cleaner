# -*- coding: utf-8 -*-
"""格式转换服务（v0056，docs/CONVERT_CONTRACT.md）。

职责
----
- 预设驱动的视频/音频互转：视频↔视频（换容器/换编码）、视频→音频、
  音频↔音频、以及只换封装（`-c copy`，秒级、无损）；
- 任务队列：并发（默认 1）+ FIFO、进度（`-progress pipe:1`）、取消、重试、
  重启恢复、空闲时段闸门；
- 落盘：先写 `<目标>.dcpart`，校验通过后 `os.replace` 原子改名（同目录，不跨卷）；
- 面向 API：能力探测（ffmpeg 版本/编码器）、提交、目录枚举、列表、队列快照、
  取消/重试/删除/批量。

明确不做
--------
- 音频→视频（凭空生成画面没有意义）、图片/字幕文件转换、字幕烧录（v1）；
- 硬编（v1 只用 CPU；NAS 上 VAAPI 不稳定）、DRM、直播、HDR tonemap
  （检测到 HDR 只告警，不保证色彩正确）；
- 单任务断点续转（FFmpeg 无此能力）：只支持取消/重试，重试从头开始。

安全约定
--------
- 源文件必须在共享文件夹内、必须是普通文件、拒绝符号链接；
- 产物默认不覆盖任何已有文件（rename/skip/overwrite 三策略，默认 rename）；
- 「删除源文件」必须显式 `confirm="DELETE SOURCE"`，且仅在产物校验通过后执行；
- 输出目录必须可写，且剩余空间不低于 `convert_min_free_gb`。
"""

from __future__ import absolute_import

import logging
import os
import re
import shutil
import threading
import time
import uuid

from . import config, hwaccel
from .errors import ApiError, invalid, not_found
from .fsutil import (check_path_safety, extension_of, has_symlink_component,
                     is_dir_nofollow, is_hidden_name, is_regular_file,
                     is_safe_dir_name, is_system_name, is_under, path_key,
                     writable_dir)
from .timeutil import iso_local
from .video_similarity import (DURATION_RE, FFmpegTool, VideoSkip, VideoStopped,
                               VideoTimeout, parse_media_streams)

logger = logging.getLogger("dupcleaner.convert")

# ----------------------------------------------------------------------
# 状态 / 阶段
# ----------------------------------------------------------------------
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED,
            STATUS_CANCELLED)
FINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)

STAGE_LABELS = {
    "queued": u"排队中",
    "probe": u"探测源",
    "convert": u"转换",
    "verify": u"校验",
    "done": u"完成",
}

_NAME_MAX = 120
_BAD_NAME_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")
_PIX_FMT_RE = re.compile(
    r"\b(yuv[a-z0-9_]+|nv12|nv21|p010[a-z]*|p016[a-z]*|rgb[a-z0-9_]*"
    r"|gray[a-z0-9_]*)\b")
_HDR_RE = re.compile(r"smpte2084|arib-std-b67|bt2020", re.IGNORECASE)


# ----------------------------------------------------------------------
# 错误
# ----------------------------------------------------------------------
class ConvertError(Exception):
    """转换过程中的可读错误（code 与 API 错误码之一）。"""

    def __init__(self, code, message, details=None):
        super(ConvertError, self).__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class _JobCancelled(Exception):
    pass


class _JobSkipped(Exception):
    """目标已存在且策略为 skip：直接完成，不消耗 CPU。"""

    def __init__(self, path, size=0):
        super(_JobSkipped, self).__init__(u"已存在: %s" % path)
        self.path = path
        self.size = size


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def make_job_id():
    return "cv-" + uuid.uuid4().hex[:20]


def _iso(epoch):
    if not epoch:
        return None
    return iso_local(float(epoch))


def _human_bytes(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "0 B"
    units = (u"B", u"KB", u"MB", u"GB", u"TB")
    index = 0
    while number >= 1024 and index < len(units) - 1:
        number /= 1024.0
        index += 1
    if index == 0:
        return u"%d %s" % (int(number), units[index])
    return u"%.1f %s" % (number, units[index])


def _one_line(text, limit=400):
    """把 FFmpeg stderr 压成一行（只取最后一条有意义的错误）。"""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return u""
    # 跳过典型横幅，优先取最后一条
    text = lines[-1]
    if len(text) > limit:
        text = text[:limit] + u"…"
    return text


def _parse_hhmm(text):
    match = re.match(r"^(\d{1,2}):(\d{2})$", str(text or "").strip())
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


def _in_window(start_text, end_text, now):
    start = _parse_hhmm(start_text)
    end = _parse_hhmm(end_text)
    if start is None or end is None or start == end:
        return False
    minutes = int(now.tm_hour) * 60 + int(now.tm_min)
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def _clean_subdir(text):
    """子目录名：只允许单层，去掉分隔符/非法字符，拒绝 .. 与空值。"""
    raw = str(text or "").strip()
    if not raw:
        return config.CONVERT_SUBDIR_DEFAULT
    cleaned = _BAD_NAME_RE.sub("_", raw).strip(" .")
    if not cleaned or cleaned in (".", ".."):
        raise invalid(u"输出子目录名非法")
    if len(cleaned) > config.CONVERT_SUBDIR_MAX:
        cleaned = cleaned[:config.CONVERT_SUBDIR_MAX]
    return cleaned


# ----------------------------------------------------------------------
# 预设表（容器 + 编码 + 参数）
# ----------------------------------------------------------------------
PRESETS = (
    # -------- 视频 --------
    {"id": "mp4_h264", "group": "video", "label": u"MP4 · H.264 + AAC",
     "ext": ".mp4", "v_encoder": "libx264", "a_encoder": "aac",
     "copy": False, "copy_subs": False, "faststart": True,
     "hint": u"通用/手机/网页，兼容性最好"},
    {"id": "mp4_h265", "group": "video", "label": u"MP4 · H.265 + AAC",
     "ext": ".mp4", "v_encoder": "libx265", "a_encoder": "aac",
     "copy": False, "copy_subs": False, "faststart": True,
     "extra_args": ["-tag:v", "hvc1"],
     "hint": u"同画质省 30–50% 空间；老设备可能不支持"},
    {"id": "mkv_h264", "group": "video", "label": u"MKV · H.264 + AAC",
     "ext": ".mkv", "v_encoder": "libx264", "a_encoder": "aac",
     "copy": False, "copy_subs": True,
     "hint": u"复制字幕轨；多音轨只取第一条"},
    {"id": "mkv_copy", "group": "video",
     "label": u"MKV · 仅换封装（不重编码）", "ext": ".mkv",
     "v_encoder": None, "a_encoder": None,
     "copy": True, "copy_subs": True,
     "hint": u"秒级完成、画质不变；任意编码都能封装"},
    {"id": "mp4_copy", "group": "video",
     "label": u"MP4 · 仅换封装（不重编码）", "ext": ".mp4",
     "v_encoder": None, "a_encoder": None,
     "copy": True, "copy_subs": False, "faststart": True,
     "hint": u"秒级完成；源需为 H.264/HEVC/AV1 + AAC/MP3/AC3"},
    {"id": "webm_vp9", "group": "video", "label": u"WEBM · VP9 + Opus",
     "ext": ".webm", "v_encoder": "libvpx-vp9", "a_encoder": "libopus",
     "copy": False, "copy_subs": False,
     "hint": u"浏览器/网页嵌入；CPU 转码很慢"},
    {"id": "mov_h264", "group": "video", "label": u"MOV · H.264 + AAC",
     "ext": ".mov", "v_encoder": "libx264", "a_encoder": "aac",
     "copy": False, "copy_subs": False,
     "hint": u"剪辑软件（Premiere / Final Cut）"},
    {"id": "ts_h264", "group": "video", "label": u"TS · H.264 + AAC",
     "ext": ".ts", "v_encoder": "libx264", "a_encoder": "aac",
     "copy": False, "copy_subs": False,
     "hint": u"老电视/机顶盒"},
    {"id": "avi_mpeg4", "group": "video", "label": u"AVI · MPEG-4 + MP3",
     "ext": ".avi", "v_encoder": "mpeg4", "a_encoder": "libmp3lame",
     "copy": False, "copy_subs": False,
     "hint": u"极老设备（体积大、画质一般）"},
    # -------- v0065：硬件编码（Intel 核显 VAAPI） --------
    {"id": "mp4_h264_hw", "group": "video",
     "label": u"MP4 · H.264 + AAC（硬件，快）",
     "ext": ".mp4", "v_encoder": "h264_vaapi", "a_encoder": "aac",
     "copy": False, "copy_subs": False, "faststart": True,
     "hw": True, "hw_codec": "h264",
     "hint": u"核显硬编：比 CPU 快约 2–4 倍，画质略逊 x264"},
    {"id": "mp4_h265_hw", "group": "video",
     "label": u"MP4 · H.265 + AAC（硬件，快）",
     "ext": ".mp4", "v_encoder": "hevc_vaapi", "a_encoder": "aac",
     "copy": False, "copy_subs": False, "faststart": True,
     "hw": True, "hw_codec": "hevc", "extra_args": ["-tag:v", "hvc1"],
     "hint": u"核显硬编 HEVC：远快于 libx265，同画质省空间"},
    {"id": "mkv_h264_hw", "group": "video",
     "label": u"MKV · H.264 + AAC（硬件，快）",
     "ext": ".mkv", "v_encoder": "h264_vaapi", "a_encoder": "aac",
     "copy": False, "copy_subs": True,
     "hw": True, "hw_codec": "h264",
     "hint": u"核显硬编 + 复制字幕轨；多音轨只取第一条"},
    # -------- 音频（视频也可提取） --------
    {"id": "mp3", "group": "audio", "label": u"MP3",
     "ext": ".mp3", "v_encoder": None, "a_encoder": "libmp3lame",
     "copy": False, "copy_subs": False, "hint": u"最通用"},
    {"id": "m4a", "group": "audio", "label": u"M4A · AAC",
     "ext": ".m4a", "v_encoder": None, "a_encoder": "aac",
     "copy": False, "copy_subs": False, "hint": u"Apple/手机"},
    {"id": "flac", "group": "audio", "label": u"FLAC",
     "ext": ".flac", "v_encoder": None, "a_encoder": "flac",
     "copy": False, "copy_subs": False,
     "hint": u"无损；从有损源转只会变大"},
    {"id": "wav", "group": "audio", "label": u"WAV · PCM 16bit",
     "ext": ".wav", "v_encoder": None, "a_encoder": "pcm_s16le",
     "copy": False, "copy_subs": False, "hint": u"剪辑/ASR 输入，体积很大"},
    {"id": "ogg", "group": "audio", "label": u"OGG · Vorbis",
     "ext": ".ogg", "v_encoder": None, "a_encoder": "libvorbis",
     "copy": False, "copy_subs": False, "hint": u"开源生态"},
    {"id": "opus", "group": "audio", "label": u"OPUS",
     "ext": ".opus", "v_encoder": None, "a_encoder": "libopus",
     "copy": False, "copy_subs": False, "hint": u"语音/网页"},
    {"id": "audio_copy", "group": "audio",
     "label": u"原音轨提取（不重编码）", "ext": ".mka",
     "v_encoder": None, "a_encoder": None,
     "copy": True, "copy_subs": False,
     "hint": u"只取第一条音轨，原样封装（秒级）"},
)

PRESET_MAP = {preset["id"]: preset for preset in PRESETS}
# v0065：硬件预设失败时回退到同容器/同编码的 CPU 预设（输出扩展名不变）。
HW_FALLBACK = {
    "mp4_h264_hw": "mp4_h264",
    "mp4_h265_hw": "mp4_h265",
    "mkv_h264_hw": "mkv_h264",
}
PRESET_IDS = tuple(preset["id"] for preset in PRESETS)
VIDEO_PRESETS = tuple(p for p in PRESETS if p["group"] == "video")
AUDIO_PRESETS = tuple(p for p in PRESETS if p["group"] == "audio")

# 质量三档 → 编码器参数（视频）
_VIDEO_QUALITY_ARGS = {
    "libx264": {"high": ["-crf", "18"], "standard": ["-crf", "23"],
                "small": ["-crf", "28"]},
    "libx265": {"high": ["-crf", "20"], "standard": ["-crf", "25"],
                "small": ["-crf", "30"]},
    "libvpx-vp9": {"high": ["-crf", "28"], "standard": ["-crf", "33"],
                   "small": ["-crf", "38"]},
    "mpeg4": {"high": ["-q:v", "3"], "standard": ["-q:v", "5"],
              "small": ["-q:v", "8"]},
}

# 质量三档 → 编码器参数（音频）
_AUDIO_QUALITY_ARGS = {
    "aac": {"high": ["-b:a", "256k"], "standard": ["-b:a", "192k"],
            "small": ["-b:a", "128k"]},
    "libmp3lame": {"high": ["-q:a", "0"], "standard": ["-q:a", "2"],
                   "small": ["-q:a", "5"]},
    "libvorbis": {"high": ["-q:a", "6"], "standard": ["-q:a", "5"],
                  "small": ["-q:a", "3"]},
    "libopus": {"high": ["-b:a", "192k"], "standard": ["-b:a", "128k"],
                "small": ["-b:a", "96k"]},
    "flac": {"high": ["-compression_level", "8"],
             "standard": ["-compression_level", "5"],
             "small": ["-compression_level", "2"]},
    "pcm_s16le": {},
}

# 只换封装时的容器兼容性（MP4 最挑；MKV 几乎都能装）
_MP4_COPY_VIDEO = ("h264", "hevc", "av1", "mpeg4", "mpeg2video")
_MP4_COPY_AUDIO = ("aac", "mp3", "ac3", "eac3", "alac")

# 输出封装器：产物先写 `<目标>.dcpart`，扩展名无法让 FFmpeg 推断格式，
# 必须显式 `-f <muxer>`（下载中心的合并也有同样问题，v0056 一并修掉）。
_MUXERS = {
    "mp4_h264": "mp4", "mp4_h265": "mp4", "mp4_copy": "mp4",
    "mp4_h264_hw": "mp4", "mp4_h265_hw": "mp4",
    "mkv_h264": "matroska", "mkv_copy": "matroska", "audio_copy": "matroska",
    "mkv_h264_hw": "matroska",
    "webm_vp9": "webm", "mov_h264": "mov", "ts_h264": "mpegts",
    "avi_mpeg4": "avi", "mp3": "mp3", "m4a": "ipod", "flac": "flac",
    "wav": "wav", "ogg": "ogg", "opus": "opus",
}

# 体积粗估（仅提示，不作为闸门）
_VIDEO_ESTIMATE_FACTOR = {"high": 1.3, "standard": 0.9, "small": 0.6}
_AUDIO_ESTIMATE_KBPS = {"mp3": 190, "m4a": 192, "flac": 900, "wav": 1411,
                        "ogg": 160, "opus": 128}


# ----------------------------------------------------------------------
# 探测 / 进度 / 兼容性 / 参数（纯函数，便于单测）
# ----------------------------------------------------------------------
def parse_probe_text(text):
    """解析 `ffmpeg -i` 的 stderr：时长、音视频轨、分辨率、像素格式、HDR。"""
    text = text or u""
    duration = None
    match = DURATION_RE.search(text)
    if match:
        hours, minutes, seconds = match.groups()
        try:
            duration = (int(hours) * 3600.0 + int(minutes) * 60.0
                        + float(seconds))
        except (TypeError, ValueError):
            duration = None
    streams = parse_media_streams(text)
    video = streams["video_streams"]
    audio = streams["audio_streams"]
    pix_fmt = ""
    hdr = False
    for line in text.splitlines():
        if "Video:" not in line:
            continue
        if not pix_fmt:
            fmt = _PIX_FMT_RE.search(line)
            if fmt:
                pix_fmt = fmt.group(1)
        if _HDR_RE.search(line):
            hdr = True
    video_codec = ""
    if video:
        video_codec = (video[0].get("codec") or u"").split(" ")[0].lower()
    audio_codec = ""
    if audio:
        audio_codec = (audio[0].get("codec") or u"").split(" ")[0].lower()
    return {
        "duration": duration,
        "video_streams": video,
        "audio_streams": audio,
        "subtitle_streams": streams["subtitle_streams"],
        "has_video": bool(video),
        "has_audio": bool(audio),
        "has_subtitle": bool(streams["subtitle_streams"]),
        "width": int(video[0].get("width") or 0) if video else 0,
        "height": int(video[0].get("height") or 0) if video else 0,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "pix_fmt": pix_fmt,
        "hdr": hdr,
    }


def parse_progress_line(line):
    """解析 `-progress pipe:1` 的一行；返回 {字段: 值} 或 None。

    FFmpeg 的 `out_time_ms` 历史上其实是微秒，与 `out_time_us` 相同，
    这里两者都按微秒处理（`out_time` 是 HH:MM:SS.ffffff，作为兜底）。
    """
    text = (line or u"").strip()
    if not text or "=" not in text:
        return None
    key, value = text.split("=", 1)
    key = key.strip()
    value = value.strip()
    if key == "progress":
        return {"done": value == "end"}
    if key in ("out_time_us", "out_time_ms"):
        try:
            return {"out_time_sec": float(value) / 1000000.0}
        except (TypeError, ValueError):
            return None
    if key == "out_time":
        match = re.match(r"^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$", value)
        if not match:
            return None
        hours, minutes, seconds = match.groups()
        try:
            total = (int(hours) * 3600.0 + int(minutes) * 60.0
                     + float(seconds))
        except (TypeError, ValueError):
            return None
        return {"out_time_sec": total}
    if key == "speed":
        match = re.match(r"^([0-9.]+)x$", value)
        if not match:
            return None
        try:
            return {"speed": float(match.group(1))}
        except (TypeError, ValueError):
            return None
    if key == "fps":
        try:
            return {"fps": float(value)}
        except (TypeError, ValueError):
            return None
    if key == "total_size":
        try:
            return {"total_size": int(value)}
        except (TypeError, ValueError):
            return None
    if key == "bitrate":
        return {"bitrate": value}
    return None


def check_compat(preset, info, encoders=None):
    """源文件/编码器是否满足该预设；返回 (code, message) 或 None。"""
    if preset["group"] == "video":
        if not info.get("has_video"):
            return ("SOURCE_INVALID", u"源文件没有视频轨，不能转为视频格式")
        if preset["copy"] and preset["id"] == "mp4_copy":
            if info.get("video_codec") not in _MP4_COPY_VIDEO:
                return ("PRESET_UNSUPPORTED",
                        u"MP4 封装只支持 H.264/HEVC/AV1 视频；"
                        u"请改用「MKV 仅换封装」")
            if info.get("has_audio") \
                    and info.get("audio_codec") not in _MP4_COPY_AUDIO:
                return ("PRESET_UNSUPPORTED",
                        u"MP4 封装不支持 %s 音频；请改用「MKV 仅换封装」"
                        % (info.get("audio_codec") or u"该格式"))
    else:
        if not info.get("has_audio"):
            return ("SOURCE_INVALID", u"源文件没有音频轨，无法提取音频")
    if encoders:
        missing = []
        for encoder in (preset.get("v_encoder"), preset.get("a_encoder")):
            if encoder and encoder not in encoders:
                missing.append(encoder)
        if missing:
            return ("PRESET_UNSUPPORTED",
                    u"当前 FFmpeg 不支持编码器 %s" % ", ".join(missing))
    return None


def build_args(preset, info, options, source_path, output_path, hw=None):
    """构造一次转换的 FFmpeg 参数数组（无 shell，顺序固定，便于金标测试）。

    `hw`（v0065）只在硬件预设时传入：`{"device": "/dev/dri/renderD128",
    "decode": True}`；`decode` 表示源编码能否硬解（否则软解 + hwupload）。
    """
    quality = (options.get("quality")
               or config.CONVERT_QUALITY_DEFAULT)
    speed = options.get("speed") or config.CONVERT_SPEED_DEFAULT
    max_resolution = int(options.get("max_resolution") or 0)
    channels = options.get("channels") or ""
    hardware = bool(preset.get("hw"))
    device = (hw or {}).get("device") or ""
    hw_decode = bool(hardware and (hw or {}).get("decode"))
    if hardware and not device:
        raise ConvertError("HWACCEL_UNAVAILABLE",
                           u"硬件编码需要 VAAPI 设备，但当前未探测到")
    prefix = ["-nostdin", "-hide_banner", "-loglevel", "error",
              "-progress", "pipe:1", "-nostats"]
    if hardware:
        prefix += ["-vaapi_device", device]
        if hw_decode:
            prefix += ["-hwaccel", "vaapi", "-hwaccel_device", device,
                       "-hwaccel_output_format", "vaapi"]
    args = prefix + ["-i", source_path]
    args += ["-map_metadata", "0", "-map_chapters", "0"]
    if preset["group"] == "audio":
        args += ["-map", "0:a:0", "-vn", "-sn", "-dn"]
        if preset["copy"]:
            args += ["-c:a", "copy"]
        else:
            args += ["-c:a", preset["a_encoder"]]
            args += list(_AUDIO_QUALITY_ARGS.get(
                preset["a_encoder"], {}).get(quality) or [])
    else:
        args += ["-map", "0:v:0", "-map", "0:a:0?"]
        if preset["copy_subs"]:
            args += ["-map", "0:s?"]
        if preset["copy"]:
            args += ["-c:v", "copy", "-c:a", "copy"]
            if preset["copy_subs"]:
                args += ["-c:s", "copy"]
        else:
            if hardware:
                # VAAPI 走 CQP（-qp），不是 CRF；数值越小画质越好
                qp = config.VAAPI_QP.get(
                    preset.get("hw_codec") or "h264", {}).get(quality, 24)
                args += ["-c:v", preset["v_encoder"], "-rc_mode", "CQP",
                         "-qp", str(qp), "-b:v", "0"]
                if hw_decode:
                    scale = ("scale_vaapi=w=-2:h=%d:format=nv12"
                             % max_resolution) if max_resolution \
                        else "scale_vaapi=format=nv12"
                else:
                    scale = ("scale=-2:min(%d\\,ih),format=nv12,hwupload"
                             % max_resolution) if max_resolution \
                        else "format=nv12,hwupload"
                args += ["-vf", scale]
            else:
                args += ["-c:v", preset["v_encoder"]]
                args += list(_VIDEO_QUALITY_ARGS.get(
                    preset["v_encoder"], {}).get(quality) or [])
                if preset["v_encoder"] in ("libx264", "libx265"):
                    args += ["-preset", speed]
                if preset["v_encoder"] == "libvpx-vp9":
                    args += ["-b:v", "0", "-row-mt", "1", "-cpu-used", "2"]
                args += ["-pix_fmt", "yuv420p"]
                if max_resolution:
                    # 只缩小不放大：min(H, ih)；过滤表达式里的逗号需要转义
                    args += ["-vf", "scale=-2:min(%d\\,ih)" % max_resolution]
            if info.get("has_audio"):
                args += ["-c:a", preset["a_encoder"]]
                args += list(_AUDIO_QUALITY_ARGS.get(
                    preset["a_encoder"], {}).get(quality) or [])
            else:
                args += ["-an"]
            if preset["copy_subs"]:
                args += ["-c:s", "copy"]
            args += list(preset.get("extra_args") or [])
    if channels == "stereo":
        args += ["-ac", "2"]
    if preset.get("faststart"):
        args += ["-movflags", "+faststart"]
    args += ["-f", _MUXERS.get(preset["id"], "matroska")]
    args += ["-y", output_path]
    return args


def estimate_bytes(preset, info, options, source_bytes):
    """产物体积粗估（仅用于 UI 提示）。"""
    source_bytes = int(source_bytes or 0)
    if preset["group"] == "audio":
        if preset["copy"]:
            return source_bytes
        duration = float(info.get("duration") or 0.0)
        kbps = _AUDIO_ESTIMATE_KBPS.get(preset["id"])
        if duration > 0 and kbps:
            return int(kbps * 1000.0 / 8.0 * duration)
        return source_bytes
    if preset["copy"]:
        return source_bytes
    quality = options.get("quality") or config.CONVERT_QUALITY_DEFAULT
    factor = _VIDEO_ESTIMATE_FACTOR.get(quality, 0.9)
    return int(source_bytes * factor)


def preset_view(preset, encoders=None, hardware=None):
    """预设对外视图（含可用性）。

    `hardware`（v0065）：`hwaccel.detect()` 的结果；硬件预设没有可用 VAAPI 时
    标记不可用并给出原因（前端直接置灰）。
    """
    reason = ""
    if encoders is not None and encoders:
        for encoder in (preset.get("v_encoder"), preset.get("a_encoder")):
            if encoder and encoder not in encoders:
                reason = u"当前 FFmpeg 不支持 %s" % encoder
                break
    if not reason and preset.get("hw"):
        info = hardware or {}
        if not info.get("available"):
            reason = info.get("reason") or u"没有可用的硬件编码器（VAAPI）"
    return {
        "id": preset["id"],
        "group": preset["group"],
        "label": preset["label"],
        "ext": preset["ext"],
        "hint": preset.get("hint") or u"",
        "copy": bool(preset.get("copy")),
        "copy_subtitles": bool(preset.get("copy_subs")),
        "hw": bool(preset.get("hw")),
        "available": not reason,
        "reason": reason,
    }


# ----------------------------------------------------------------------
# 任务
# ----------------------------------------------------------------------
class ConvertJob(object):
    def __init__(self, job_id, source_path, preset_id, options=None, user="",
                 created_epoch=None):
        self.job_id = job_id
        self.source_path = source_path or ""
        self.source_name = os.path.basename(self.source_path)
        self.preset = preset_id or config.CONVERT_PRESET_DEFAULT
        self.user = user or ""
        self.options = dict(options or {})
        self.title = self.source_name
        self.status = STATUS_QUEUED
        self.stage = STATUS_QUEUED
        self.message = u"排队中"
        self.progress = 0
        self.error_code = ""
        self.error_message = ""
        self.source_bytes = 0
        self.source_duration = 0.0
        self.source_info = {}
        self.output_dir = ""
        self.output_name = ""
        self.output_path = ""
        self.output_size = 0
        self.out_time = 0.0
        self.speed = 0.0
        self.fps = 0.0
        self.part_path = ""
        self.part_kept = False
        self.attempts = 0
        self.deleted_source = False
        # v0061：overwrite=trash 时被覆盖的旧产物（原路径）；trashed_paths 为
        # [{"original", "trash_path", "size"}]，成功后登记回收站 manifest 用。
        self.overwritten_path = ""
        self.trashed_paths = []
        # v0065：硬件编码失败后自动回退 CPU（UI 上标注，便于排障）
        self.hw_fallback = False
        self.verify = {}
        self.created_epoch = float(created_epoch or time.time())
        self.started_epoch = 0.0
        self.finished_epoch = 0.0
        self.updated_epoch = self.created_epoch
        self.timeline = []
        # 运行期（不持久化）
        self._cancel = threading.Event()
        self._last_flush = 0.0
        self._active_start = 0.0

    @property
    def preset_obj(self):
        return PRESET_MAP.get(self.preset) or PRESET_MAP[
            config.CONVERT_PRESET_DEFAULT]

    @property
    def finished(self):
        return self.status in FINAL_STATUSES

    def cancel_requested(self):
        return self._cancel.is_set()

    # -- 时间线 ---------------------------------------------------------
    def _find_step(self, key):
        for step in self.timeline:
            if step.get("key") == key:
                return step
        return None

    def step_start(self, key, detail=None):
        now = time.time()
        step = self._find_step(key)
        if step is None:
            step = {"key": key, "status": "running", "attempts": 0,
                    "start_epoch": now, "end_epoch": None,
                    "active_sec": 0.0, "detail": detail or ""}
            self.timeline.append(step)
        else:
            step["status"] = "running"
            step["end_epoch"] = None
            step["attempts"] = int(step.get("attempts") or 0) + 1
            if detail:
                step["detail"] = detail
        self._active_start = now
        self.stage = key
        return step

    def step_end(self, key, status="done", detail=None):
        now = time.time()
        step = self._find_step(key)
        if step is None:
            return
        if self._active_start:
            step["active_sec"] = float(step.get("active_sec") or 0.0) + max(
                0.0, now - self._active_start)
        self._active_start = 0.0
        step["status"] = status
        step["end_epoch"] = now
        if detail:
            step["detail"] = detail

    def step_view(self):
        out = []
        current = time.time()
        for step in self.timeline:
            active = float(step.get("active_sec") or 0.0)
            if step.get("status") == "running" and self._active_start:
                active += max(0.0, current - self._active_start)
            out.append({
                "key": step.get("key"),
                "label": STAGE_LABELS.get(step.get("key"), step.get("key")),
                "status": step.get("status"),
                "attempts": step.get("attempts"),
                "started_at": _iso(step.get("start_epoch")),
                "finished_at": _iso(step.get("end_epoch")),
                "duration_sec": round(active, 2) if step.get("start_epoch")
                else None,
                "detail": step.get("detail") or "",
            })
        return out

    def duration_sec(self):
        if self.status == STATUS_RUNNING and self.started_epoch:
            return round(time.time() - self.started_epoch, 1)
        if self.started_epoch and self.finished_epoch:
            return round(self.finished_epoch - self.started_epoch, 1)
        return None

    def eta_seconds(self):
        if self.status != STATUS_RUNNING:
            return None
        total = float(self.source_duration or 0.0)
        done = float(self.out_time or 0.0)
        speed = float(self.speed or 0.0)
        if total <= 0 or done <= 0 or speed <= 0:
            return None
        return int(max(0.0, total - done) / speed)

    # -- 序列化 ---------------------------------------------------------
    def snapshot(self, include_detail=False):
        preset = self.preset_obj
        payload = {
            "job_id": self.job_id,
            "source_path": self.source_path,
            "source_name": self.source_name,
            "title": self.title or self.source_name,
            "preset": self.preset,
            "preset_label": preset["label"],
            "preset_group": preset["group"],
            "target_ext": preset["ext"],
            "status": self.status,
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "message": self.message,
            "progress": int(self.progress or 0),
            "source_bytes": int(self.source_bytes or 0),
            "source_bytes_text": _human_bytes(self.source_bytes),
            "source_duration": round(float(self.source_duration or 0.0), 2),
            "output_dir": self.output_dir,
            "output_name": self.output_name,
            "output_path": self.output_path,
            "output_size": int(self.output_size or 0),
            "output_size_text": _human_bytes(self.output_size),
            "out_time": round(float(self.out_time or 0.0), 2),
            "speed": round(float(self.speed or 0.0), 2),
            "fps": round(float(self.fps or 0.0), 1),
            "eta_seconds": self.eta_seconds(),
            "duration_sec": self.duration_sec(),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "attempts": int(self.attempts or 0),
            "verify": dict(self.verify or {}),
            "part_kept": bool(self.part_kept),
            "deleted_source": bool(self.deleted_source),
            "hw_fallback": bool(self.hw_fallback),
            "overwritten_path": self.overwritten_path,
            "trashed_count": len(self.trashed_paths or []),
            "estimate_bytes": estimate_bytes(
                preset, self.source_info or {}, self.options,
                self.source_bytes),
            "created_at": _iso(self.created_epoch),
            "started_at": _iso(self.started_epoch),
            "finished_at": _iso(self.finished_epoch),
            "created_epoch": self.created_epoch,
            "finished_epoch": self.finished_epoch,
        }
        if include_detail:
            payload["timeline"] = {
                "created_at": _iso(self.created_epoch),
                "started_at": _iso(self.started_epoch),
                "finished_at": _iso(self.finished_epoch),
                "steps": self.step_view(),
            }
            payload["options"] = dict(self.options)
            payload["source_info"] = {
                "duration": self.source_info.get("duration"),
                "width": self.source_info.get("width"),
                "height": self.source_info.get("height"),
                "video_codec": self.source_info.get("video_codec"),
                "audio_codec": self.source_info.get("audio_codec"),
                "has_audio": self.source_info.get("has_audio"),
                "has_subtitle": self.source_info.get("has_subtitle"),
                "pix_fmt": self.source_info.get("pix_fmt"),
                "hdr": self.source_info.get("hdr"),
                "audio_streams": len(self.source_info.get("audio_streams") or []),
                "subtitle_streams": len(
                    self.source_info.get("subtitle_streams") or []),
            }
        return payload

    def to_record(self):
        return {
            "job_id": self.job_id,
            "source_path": self.source_path,
            "source_name": self.source_name,
            "preset": self.preset,
            "user": self.user,
            "options": self.options,
            "title": self.title,
            "status": self.status,
            "stage": self.stage,
            "message": self.message,
            "progress": self.progress,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "source_bytes": self.source_bytes,
            "source_duration": self.source_duration,
            "source_info": self.source_info,
            "output_dir": self.output_dir,
            "output_name": self.output_name,
            "output_path": self.output_path,
            "output_size": self.output_size,
            "out_time": self.out_time,
            "speed": self.speed,
            "fps": self.fps,
            "part_path": self.part_path,
            "part_kept": bool(self.part_kept),
            "attempts": self.attempts,
            "deleted_source": bool(self.deleted_source),
            "hw_fallback": bool(self.hw_fallback),
            "overwritten_path": self.overwritten_path,
            "trashed_paths": self.trashed_paths,
            "verify": self.verify,
            "created_epoch": self.created_epoch,
            "started_epoch": self.started_epoch,
            "finished_epoch": self.finished_epoch,
            "updated_epoch": time.time(),
            "timeline": self.timeline,
        }

    @classmethod
    def from_record(cls, record):
        job = cls(record.get("job_id") or make_job_id(),
                  record.get("source_path") or "",
                  record.get("preset") or config.CONVERT_PRESET_DEFAULT,
                  options=record.get("options") or {},
                  user=record.get("user") or "",
                  created_epoch=record.get("created_epoch"))
        for field in ("source_name", "title", "status", "stage", "message",
                      "progress", "error_code", "error_message",
                      "source_bytes", "source_duration", "source_info",
                      "output_dir", "output_name", "output_path",
                      "output_size", "out_time", "speed", "fps",
                      "part_path", "part_kept", "attempts",
                      "deleted_source", "hw_fallback", "overwritten_path",
                      "trashed_paths",
                      "verify", "started_epoch",
                      "finished_epoch", "updated_epoch", "timeline"):
            if field in record:
                setattr(job, field, record[field])
        job.progress = int(job.progress or 0)
        if not isinstance(job.trashed_paths, list):
            job.trashed_paths = []
        job.source_bytes = int(job.source_bytes or 0)
        job.output_size = int(job.output_size or 0)
        job.attempts = int(job.attempts or 0)
        if not isinstance(job.timeline, list):
            job.timeline = []
        if not isinstance(job.verify, dict):
            job.verify = {}
        if not isinstance(job.source_info, dict):
            job.source_info = {}
        return job


# ----------------------------------------------------------------------
# 服务
# ----------------------------------------------------------------------
class ConvertService(object):
    def __init__(self, runtime_config, store, share_service, settings_provider,
                 ffmpeg_tool=None, on_complete=None, notifier=None,
                 trash_manager=None):
        self.config = runtime_config
        self.store = store
        self._shares = share_service
        self._settings = settings_provider
        # v0061：overwrite=trash 时把旧产物移入套件回收站（manifest 可在回收站页恢复）。
        self._trash = trash_manager
        self._lock = threading.RLock()
        self._jobs = {}
        self._queue = []
        self._running = {}
        self._stop = threading.Event()
        self._dispatcher = None
        self._ffmpeg = ffmpeg_tool or FFmpegTool(
            runtime_config=runtime_config,
            settings_provider=settings_provider)
        self._on_complete = on_complete
        self._notifier = notifier
        self._load_jobs()

    # -- 生命周期 -------------------------------------------------------
    def start(self):
        if self._dispatcher is not None:
            return
        self._stop.clear()
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="cv-dispatch", daemon=True)
        self._dispatcher.start()
        logger.info(u"格式转换调度已启动（并发上限 %s）", self._concurrency())

    def shutdown(self, timeout=5.0):
        self._stop.set()
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.status == STATUS_RUNNING:
                job._cancel.set()
        deadline = time.time() + max(0.5, float(timeout))
        with self._lock:
            threads = list(self._running.values())
        for thread in threads:
            thread.join(max(0.1, deadline - time.time()))
        self._dispatcher = None

    def _load_jobs(self):
        try:
            records = self.store.load_convert_jobs(limit=1000)
        except Exception:
            logger.exception(u"转换任务加载失败")
            records = []
        for record in records:
            if record.get("status") == STATUS_RUNNING:
                # 进程重启：FFmpeg 无法续转，回到队列从头再来。
                record["status"] = STATUS_QUEUED
                record["stage"] = STATUS_QUEUED
                record["message"] = u"服务重启，重新排队"
                record["progress"] = 0
                record["out_time"] = 0.0
            try:
                job = ConvertJob.from_record(record)
            except Exception:
                logger.exception(u"转换任务记录损坏，已跳过")
                continue
            self._jobs[job.job_id] = job
            if job.status == STATUS_QUEUED:
                self._queue.append(job.job_id)

    def apply_settings(self, settings=None):
        return self._concurrency()

    def _settings_snapshot(self):
        try:
            settings = self._settings() or {}
        except Exception:
            settings = {}
        return settings if isinstance(settings, dict) else {}

    def _concurrency(self):
        try:
            value = int(self._settings_snapshot().get(
                "convert_concurrency") or 0)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            value = config.CONVERT_DEFAULT_CONCURRENCY
        return max(1, min(value, config.CONVERT_CONCURRENCY_MAX))

    # -- 空闲时段 -------------------------------------------------------
    def _offpeak_state(self, settings=None):
        settings = settings if settings is not None else self._settings_snapshot()
        enabled = bool(settings.get("convert_offpeak_enabled"))
        start = str(settings.get("convert_offpeak_start")
                    or config.CONVERT_OFFPEAK_START_DEFAULT)
        end = str(settings.get("convert_offpeak_end")
                  or config.CONVERT_OFFPEAK_END_DEFAULT)
        open_now = True
        if enabled:
            open_now = _in_window(start, end, time.localtime())
        return {"enabled": enabled, "start": start, "end": end,
                "open": open_now}

    # -- 调度 -----------------------------------------------------------
    def _dispatch_loop(self):
        while not self._stop.wait(0.3):
            try:
                self._dispatch_once()
            except Exception:
                logger.exception(u"转换调度循环异常")

    def _dispatch_once(self):
        if not self._offpeak_state()["open"]:
            return
        limit = self._concurrency()
        with self._lock:
            while len(self._running) < limit and self._queue:
                index = self._next_queue_index_locked()
                if index is None:
                    break
                job_id = self._queue.pop(index)
                job = self._jobs.get(job_id)
                if job is None or job.status != STATUS_QUEUED:
                    continue
                job.status = STATUS_RUNNING
                job.started_epoch = job.started_epoch or time.time()
                job.progress = max(int(job.progress or 0), 1)
                thread = threading.Thread(
                    target=self._run_job, args=(job,),
                    name="cv-job-%s" % job_id[-6:], daemon=True)
                self._running[job_id] = thread
                thread.start()

    def _next_queue_index_locked(self):
        best_index = None
        best_key = None
        for index, job_id in enumerate(self._queue):
            job = self._jobs.get(job_id)
            if job is None or job.status != STATUS_QUEUED:
                continue
            key = job.created_epoch
            if best_key is None or key < best_key:
                best_key = key
                best_index = index
        return best_index

    def _run_job(self, job):
        try:
            self._execute(job)
        except _JobCancelled:
            self._cleanup_part(job)
            self._finish_status(job, STATUS_CANCELLED, u"已取消")
        except _JobSkipped as exc:
            job.output_path = exc.path
            job.output_name = os.path.basename(exc.path)
            job.output_size = int(exc.size or 0)
            job.progress = 100
            job.stage = "done"
            self._finish_status(
                job, STATUS_DONE,
                u"目标文件已存在，跳过：%s" % job.output_name)
        except (ConvertError, ApiError) as exc:
            self._fail(job, getattr(exc, "code", "CONVERT_FAILED"),
                       getattr(exc, "message", u"转换失败"))
        except Exception:
            logger.exception(u"转换任务异常: %s", job.job_id)
            self._fail(job, "INTERNAL_ERROR", u"转换失败（内部错误）")
        finally:
            with self._lock:
                self._running.pop(job.job_id, None)
            self._persist(job, force=True)

    # -- 执行 -----------------------------------------------------------
    def _job_hardware(self, preset, info):
        """v0065：给本次任务算 VAAPI 参数；非硬件预设返回 None。"""
        if not preset.get("hw"):
            return None
        state = self._hardware_state(
            self._ffmpeg, self._settings_snapshot(),
            frozenset(self._ffmpeg.encoders() or ()))
        if not state.get("available"):
            raise ConvertError(
                "HWACCEL_UNAVAILABLE",
                state.get("reason") or u"硬件编码不可用")
        return {
            "device": state["device"],
            "driver": state["driver"],
            "decode": hwaccel.codec_supported(info.get("video_codec"), state),
        }

    def _run_convert(self, job, args, on_line, timeout, hw):
        """执行一次 FFmpeg 转换，返回 (code, stderr)；硬件任务带 VAAPI 环境。"""
        kwargs = {"timeout": timeout,
                  "stop_check": lambda: job._cancel.is_set()}
        if hw:
            kwargs["env_extra"] = hwaccel.env()
        try:
            return self._ffmpeg.run_stream(args, on_line, **kwargs)
        except VideoStopped:
            raise _JobCancelled()
        except VideoTimeout:
            raise ConvertError(
                "CONVERT_TIMEOUT",
                u"转换超时（超过 %d 秒，已终止）" % int(timeout))
        except VideoSkip as exc:
            raise ConvertError("FFMPEG_UNAVAILABLE",
                               u"无法执行 FFmpeg：%s"
                               % (exc.detail or exc.reason))

    def _execute(self, job):
        job.attempts = int(job.attempts or 0) + 1
        job._cancel.clear()
        job.error_code = u""
        job.error_message = u""
        job.progress = 1
        job.speed = 0.0
        job.fps = 0.0
        job.out_time = 0.0
        preset = job.preset_obj

        self._step_start(job, "probe")
        info = self._probe(job.source_path, job)
        job.source_info = info
        job.source_duration = float(info.get("duration") or 0.0)
        job.source_bytes = int(os.path.getsize(job.source_path))
        problem = check_compat(preset, info,
                               self._ffmpeg.encoders() or None)
        if problem:
            raise ConvertError(problem[0], problem[1])
        self._step_end(job, "probe")

        output_dir = self._resolve_output_dir(job)
        job.output_dir = output_dir
        self._ensure_free_space(job, output_dir)
        target, action = self._resolve_target(job, output_dir)
        job.output_dir = os.path.dirname(target)
        job.output_path = target
        job.output_name = os.path.basename(target)
        if action == "skip" and os.path.exists(target):
            raise _JobSkipped(target, os.path.getsize(target))
        if info.get("hdr") and preset["group"] == "video" and not preset["copy"]:
            logger.warning(u"源为 HDR，转 H.264 后颜色可能偏灰: %s",
                           job.source_path)

        part = target + config.CONVERT_PART_EXT
        job.part_path = part
        self._check_cancelled(job)
        self._step_start(job, "convert")
        hw = self._job_hardware(preset, info)
        args = build_args(preset, info, job.options, job.source_path, part,
                          hw=hw)
        timeout = self._timeout_for(job, info)

        def on_line(line):
            self._on_progress(job, line)

        code, stderr = self._run_convert(job, args, on_line, timeout, hw)
        if code != 0 and hw and preset["id"] in HW_FALLBACK:
            # v0065：硬件编码失败（驱动/格式不支持等）→ 自动回退同容器 CPU 预设
            first_line = _one_line((stderr or b"").decode("utf-8", "replace"))
            logger.warning(u"硬件编码失败，回退 CPU: %s (%s)",
                           job.job_id, first_line)
            cpu_preset = PRESET_MAP.get(HW_FALLBACK[preset["id"]])
            if cpu_preset is not None:
                job.hw_fallback = True
                job.message = u"硬件编码失败，已回退 CPU 编码"
                try:
                    self.store.audit(
                        "convert_hw_fallback", job.user,
                        {"job_id": job.job_id, "preset": preset["id"],
                         "error": first_line})
                except Exception:
                    logger.debug(u"审计硬件回退失败: %s", job.job_id)
                args = build_args(cpu_preset, info, job.options,
                                  job.source_path, part)
                code, stderr = self._run_convert(job, args, on_line, timeout,
                                                 None)
        if code != 0:
            message = _one_line((stderr or b"").decode("utf-8", "replace"))
            raise ConvertError("CONVERT_FAILED",
                               u"FFmpeg 返回 %s：%s" % (code, message
                                                       or u"未知错误"))
        self._check_cancelled(job)
        self._step_end(job, "convert")

        self._step_start(job, "verify")
        size = self._verify(job, part)
        self._step_end(job, "verify")
        if action == "trash" and os.path.exists(target):
            # v0061：覆盖前把旧产物移入套件回收站（失败则降级为加序号）
            if not self._trash_existing_target(job, target):
                directory = os.path.dirname(target)
                stem, ext = os.path.splitext(os.path.basename(target))
                target = self._unique_path(directory, stem, ext,
                                           job.source_path)
                new_part = target + config.CONVERT_PART_EXT
                try:
                    os.replace(part, new_part)
                except OSError as exc:
                    raise ConvertError("OUTPUT_DIR_INVALID",
                                       u"重命名产物失败：%s" % exc)
                part = new_part
                job.part_path = part
                job.output_dir = directory
                job.output_path = target
                job.output_name = os.path.basename(target)
        try:
            os.replace(part, target)
        except OSError as exc:
            if job.trashed_paths:
                self._restore_trashed(job)
            raise ConvertError("OUTPUT_DIR_INVALID", u"重命名产物失败：%s" % exc)
        job.output_path = target
        job.output_name = os.path.basename(target)
        job.output_size = size
        job.progress = 100
        job.stage = "done"
        job.message = u"转换完成：%s → %s" % (
            _human_bytes(job.source_bytes), _human_bytes(size))
        if job.trashed_paths:
            job.message += u"（旧产物已移入套件回收站）"
        self._record_trash(job)
        self._delete_source(job)
        self._finish_status(job, STATUS_DONE, job.message)

    def _probe(self, path, job=None):
        tool = self._ffmpeg
        if not tool.available():
            raise ConvertError("FFMPEG_UNAVAILABLE",
                               u"未找到 FFmpeg，无法转换")
        check = (lambda: job._cancel.is_set()) if job is not None else None
        try:
            _code, _stdout, stderr = tool.run(
                ["-nostdin", "-hide_banner", "-loglevel", "info",
                 "-i", path],
                timeout=config.CONVERT_PROBE_TIMEOUT_SEC, stop_check=check)
        except VideoStopped:
            raise _JobCancelled()
        except VideoTimeout:
            raise ConvertError("SOURCE_INVALID", u"探测源文件超时")
        except VideoSkip as exc:
            raise ConvertError("FFMPEG_UNAVAILABLE",
                               u"无法执行 FFmpeg：%s"
                               % (exc.detail or exc.reason))
        info = parse_probe_text((stderr or b"").decode("utf-8", "replace"))
        if not info["has_video"] and not info["has_audio"]:
            raise ConvertError(
                "SOURCE_INVALID",
                u"无法识别源文件的音视频轨（文件可能损坏或格式不支持）")
        return info

    def _timeout_for(self, job, info):
        duration = float(info.get("duration") or 0.0)
        factor = (config.CONVERT_TIMEOUT_FACTOR_COPY
                  if job.preset_obj["copy"]
                  else config.CONVERT_TIMEOUT_FACTOR_TRANSCODE)
        return max(float(config.CONVERT_TIMEOUT_MIN_SEC),
                   duration * factor)

    def _verify(self, job, part):
        if not os.path.isfile(part):
            raise ConvertError("CONVERT_FAILED", u"转换产物不存在")
        size = int(os.path.getsize(part))
        if size < config.CONVERT_MIN_OUTPUT_BYTES:
            raise ConvertError("CONVERT_FAILED", u"转换产物为空或过小")
        info = self._probe(part)
        preset = job.preset_obj
        if preset["group"] == "video" and not info.get("has_video"):
            raise ConvertError("CONVERT_FAILED", u"产物没有视频轨")
        if preset["group"] == "audio" and not info.get("has_audio"):
            raise ConvertError("CONVERT_FAILED", u"产物没有音频轨")
        expected = float(job.source_duration or 0.0)
        actual = float(info.get("duration") or 0.0)
        tolerance = float(config.CONVERT_VERIFY_TOLERANCE_SEC)
        duration_ok = True
        if expected > 0 and actual > 0 and abs(actual - expected) > tolerance:
            duration_ok = False
            logger.warning(u"转换产物时长与源不一致（%.1fs vs %.1fs）: %s",
                           actual, expected, job.job_id)
        job.verify = {
            "size": size,
            "duration": actual,
            "expected_duration": expected,
            "duration_ok": duration_ok,
            "video_codec": info.get("video_codec") or u"",
            "audio_codec": info.get("audio_codec") or u"",
            "has_video": bool(info.get("has_video")),
            "has_audio": bool(info.get("has_audio")),
        }
        return size

    def _on_progress(self, job, line):
        event = parse_progress_line(line)
        if not event:
            return
        with self._lock:
            if "out_time_sec" in event:
                job.out_time = max(float(job.out_time or 0.0),
                                   float(event["out_time_sec"]))
                duration = float(job.source_duration or 0.0)
                if duration > 0:
                    ratio = min(1.0, job.out_time / duration)
                    job.progress = min(
                        95, max(int(job.progress or 0),
                                int(5 + ratio * 90)))
            if "speed" in event:
                job.speed = float(event["speed"])
            if "fps" in event:
                job.fps = float(event["fps"])
            if event.get("done"):
                job.progress = max(int(job.progress or 0), 95)
        self._touch(job)

    # -- 输出路径 -------------------------------------------------------
    def _resolve_output_dir(self, job):
        settings = self._settings_snapshot()
        options = job.options
        mode = (options.get("output_mode")
                or settings.get("convert_output_mode")
                or config.CONVERT_OUTPUT_MODE_DEFAULT)
        if mode not in config.CONVERT_OUTPUT_MODES:
            mode = config.CONVERT_OUTPUT_MODE_DEFAULT
        base = str(options.get("output_dir")
                   or settings.get("convert_output_dir") or u"").strip()
        if mode == "alongside":
            directory = os.path.dirname(job.source_path)
        else:
            if not base:
                raise ConvertError(
                    "OUTPUT_DIR_INVALID",
                    u"未设置输出目录：请选择目录，或改用「源文件同目录」")
            directory = base
            if mode == "subdir":
                sub = _clean_subdir(options.get("output_subdir")
                                    if options.get("output_subdir") is not None
                                    else settings.get("convert_output_subdir"))
                directory = os.path.join(base, sub)
        try:
            normalized = check_path_safety(directory)
            self._shares.validate_root(normalized)
        except ApiError:
            raise
        if not os.path.isdir(normalized):
            try:
                os.makedirs(normalized)
            except OSError as exc:
                raise ConvertError("OUTPUT_DIR_INVALID",
                                   u"无法创建输出目录：%s" % exc)
        if not writable_dir(normalized):
            raise ConvertError(
                "OUTPUT_DIR_INVALID",
                u"输出目录不可写：%s（请给 duplicatecleaner 写权限）"
                % normalized)
        return normalized

    def _resolve_target(self, job, output_dir):
        """返回 (目标路径, 动作)。

        动作：new=目标不存在，直接写；skip=跳过；overwrite=直接覆盖；
        trash=覆盖前把旧产物移入套件回收站（v0061 默认）。
        源文件与产物同路径（同目录同格式）时一律加序号，任何策略都不覆盖源文件。
        """
        settings = self._settings_snapshot()
        options = job.options
        preset = job.preset_obj
        template = str(options.get("name_template")
                       or settings.get("convert_name_template")
                       or config.CONVERT_NAME_TEMPLATE_DEFAULT).strip()
        if not template:
            template = config.CONVERT_NAME_TEMPLATE_DEFAULT
        stem = os.path.splitext(os.path.basename(job.source_path))[0]
        values = {"name": stem, "preset": preset["id"],
                  "date": time.strftime("%Y%m%d")}
        text = template
        for token, value in values.items():
            text = text.replace("{%s}" % token, value)
        text = _BAD_NAME_RE.sub("_", text).strip(" .")
        if not text:
            text = stem or u"output"
        if len(text) > _NAME_MAX:
            text = text[:_NAME_MAX]
        target = os.path.join(output_dir, text + preset["ext"])
        overwrite = (options.get("overwrite")
                     or settings.get("convert_overwrite")
                     or config.CONVERT_OVERWRITE_DEFAULT)
        if overwrite not in config.CONVERT_OVERWRITE_MODES:
            overwrite = config.CONVERT_OVERWRITE_DEFAULT
        if path_key(target) == path_key(job.source_path):
            # 同目录同名同格式：绝不允许原地覆盖源文件（任何策略都不例外）
            target = self._unique_path(output_dir, text, preset["ext"],
                                       job.source_path)
            return target, "new"
        if not os.path.exists(target):
            return target, "new"
        if overwrite == "overwrite":
            return target, "overwrite"
        if overwrite == "skip":
            return target, "skip"
        if overwrite == "trash":
            return target, "trash"
        return self._unique_path(output_dir, text, preset["ext"],
                                 job.source_path), "new"

    def _trash_existing_target(self, job, target):
        """把已存在的旧产物移入套件回收站；失败时返回 False（调用方降级加序号）。

        返回 True 表示旧文件已移走（job.trashed_paths 里记了条目）。
        """
        if self._trash is None:
            return False
        try:
            share = self._shares.validate_root(os.path.dirname(target))
            root = (share or {}).get("path") or u""
            if not root or not is_under(target, root):
                return False
            stamp = time.strftime("%Y%m%d-%H%M%S")
            destination = self._trash.destination_for(root, target, stamp)
            parent = os.path.dirname(destination)
            if parent:
                os.makedirs(parent, exist_ok=True)
            size = int(os.path.getsize(target))
            os.replace(target, destination)
        except (OSError, ApiError):
            logger.exception(u"旧产物移入回收站失败，降级为加序号: %s", target)
            return False
        job.trashed_paths.append({"original": target,
                                  "trash_path": destination,
                                  "size": size})
        job.overwritten_path = target
        logger.info(u"覆盖前把旧产物移入回收站: %s → %s", target, destination)
        return True

    def _record_trash(self, job):
        """转换成功后登记回收站 manifest（供「删除与回收站」整组恢复/清理）。"""
        if self._trash is None or not job.trashed_paths:
            return
        try:
            self._trash.record_job(job.job_id, "", job.user,
                                   list(job.trashed_paths))
        except Exception:
            logger.exception(u"登记转换回收站清单失败: %s", job.job_id)

    @staticmethod
    def _restore_trashed(job):
        """产物最终改名失败时，把刚移进回收站的旧文件还原回原位。"""
        for entry in list(job.trashed_paths):
            trash_path = entry.get("trash_path") or u""
            original = entry.get("original") or u""
            if not trash_path or not original or not os.path.isfile(trash_path):
                continue
            try:
                os.replace(trash_path, original)
                logger.warning(u"产物落地失败，已还原被覆盖的旧文件: %s", original)
            except OSError:
                logger.exception(u"还原旧文件失败（仍在回收站）: %s", trash_path)
        job.trashed_paths = []

    @staticmethod
    def _unique_path(directory, stem, ext, source_path):
        for index in range(2, 1000):
            candidate = os.path.join(directory,
                                     u"%s (%d)%s" % (stem, index, ext))
            if not os.path.exists(candidate) \
                    and path_key(candidate) != path_key(source_path):
                return candidate
        raise ConvertError("OUTPUT_EXISTS", u"同名文件过多，无法自动命名")

    def _ensure_free_space(self, job, directory):
        settings = self._settings_snapshot()
        try:
            need_gb = int(settings.get("convert_min_free_gb")
                          or config.CONVERT_MIN_FREE_GB_DEFAULT)
        except (TypeError, ValueError):
            need_gb = config.CONVERT_MIN_FREE_GB_DEFAULT
        if need_gb <= 0:
            return
        try:
            free = int(shutil.disk_usage(directory).free)
        except OSError:
            return
        if free < need_gb * 1024 * 1024 * 1024:
            raise ConvertError(
                "INSUFFICIENT_SPACE",
                u"输出目录剩余空间不足（%s < %d GB）"
                % (_human_bytes(free), need_gb))

    def _delete_source(self, job):
        if not job.options.get("delete_source"):
            return
        path = job.source_path
        if not is_regular_file(path):
            return
        try:
            os.remove(path)
            job.deleted_source = True
            logger.info(u"转换完成并删除源文件: %s", path)
            try:
                self.store.audit("convert_delete_source", job.user,
                                 {"job_id": job.job_id, "path": path})
            except Exception:
                logger.debug(u"审计删除源文件失败: %s", job.job_id)
        except OSError as exc:
            logger.warning(u"删除源文件失败（不影响转换结果）: %s", exc)

    def _cleanup_part(self, job):
        part = job.part_path or u""
        if not part or not os.path.isfile(part):
            return
        if not part.endswith(config.CONVERT_PART_EXT):
            logger.warning(u"拒绝清理非转换临时文件: %s", part)
            return
        try:
            os.remove(part)
        except OSError:
            logger.exception(u"清理转换临时文件失败: %s", part)
        job.part_kept = False

    def _check_cancelled(self, job):
        if job._cancel.is_set():
            raise _JobCancelled()

    # -- 持久化 ---------------------------------------------------------
    def _step_start(self, job, key, detail=None):
        with self._lock:
            job.step_start(key, detail)
        self._touch(job, force=True)

    def _step_end(self, job, key, status="done", detail=None):
        with self._lock:
            job.step_end(key, status, detail)
        self._touch(job, force=True)

    def _persist(self, job, force=False):
        now = time.time()
        if not force:
            with self._lock:
                if now - float(job._last_flush or 0.0) < \
                        config.CONVERT_PROGRESS_FLUSH_SEC:
                    return
        with self._lock:
            job._last_flush = now
            job.updated_epoch = now
            record = job.to_record()
        try:
            self.store.save_convert_job(job.job_id, record)
        except Exception:
            logger.exception(u"保存转换任务失败: %s", job.job_id)

    def _touch(self, job, force=False):
        self._persist(job, force=force)

    def _finish_status(self, job, status, message):
        now = time.time()
        with self._lock:
            job.status = status
            job.message = message or job.message
            job.progress = 100 if status == STATUS_DONE else int(
                job.progress or 0)
            job.speed = 0.0
            job.fps = 0.0
            if status in FINAL_STATUSES:
                job.finished_epoch = job.finished_epoch or now
            job.updated_epoch = now
        if status == STATUS_DONE:
            logger.info(u"转换完成 %s → %s", job.job_id, job.output_path)
        elif status == STATUS_FAILED:
            logger.warning(u"转换失败 %s: %s %s", job.job_id,
                           job.error_code, job.error_message)
        self._persist(job, force=True)
        if status == STATUS_DONE:
            self._notify(job, "convert_done", u"转换完成", job.message)
            self._run_complete_hook(job)
        elif status == STATUS_FAILED:
            self._notify(job, "convert_failed", u"转换失败", job.message,
                         level="error")

    def _notify(self, job, event, title, message, level="info"):
        if self._notifier is None:
            return
        try:
            self._notifier.notify(
                event, title,
                u"%s：%s" % (title, message or job.job_id),
                level=level,
                detail={"job_id": job.job_id,
                        "name": job.output_name or job.source_name,
                        "size": int(job.output_size or 0),
                        "preset": job.preset})
        except Exception:
            logger.debug(u"转换通知发送失败: %s", job.job_id)

    def _run_complete_hook(self, job):
        if self._on_complete is None:
            return
        try:
            self._on_complete(job)
        except Exception:
            logger.exception(u"转换完成回调失败: %s", job.job_id)

    def _fail(self, job, code, message):
        job.error_code = code or "CONVERT_FAILED"
        job.error_message = _one_line(message) or u"转换失败"
        stage = job.stage if job.stage in STAGE_LABELS else "convert"
        job.step_end(stage, "failed", job.error_message)
        # v0061：失败一律清理 .dcpart（FFmpeg 无法续转，留着只占空间）；
        # convert_keep_temp 降级为兼容键，不再生效。
        self._cleanup_part(job)
        self._finish_status(
            job, STATUS_FAILED,
            u"失败：%s" % (job.error_message or job.error_code))

    # -- 对外：能力 / 提交 ----------------------------------------------
    @staticmethod
    def _source_extensions(settings, all_exts):
        """转换页后缀筛选：未设置/为空 = 全部；只保留仍受支持的后缀。"""
        raw = settings.get("convert_source_extensions") or []
        if not isinstance(raw, list) or not raw:
            return list(all_exts)
        allowed = set(all_exts)
        result = []
        for item in raw:
            ext = str(item or u"").strip().lower()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = "." + ext
            if ext in allowed and ext not in result:
                result.append(ext)
        return result or list(all_exts)

    def _hardware_state(self, tool, settings, enc_set):
        """v0065：硬件编码可用性（设备 + 驱动 + 设置开关 + FFmpeg 编码器）。

        返回的 dict 直接进 `capabilities()["hardware"]`；硬件预设在
        `preset_view()` 里据此置灰。
        """
        mode = (settings.get("convert_hwaccel")
                or config.CONVERT_HWACCEL_DEFAULT)
        info = hwaccel.detect()
        if mode == "off":
            info = dict(info)
            info["available"] = False
            info["reason"] = u"已在设置里关闭硬件加速（convert_hwaccel=off）"
        elif info.get("available") and tool is not None:
            probe = tool.probe_vaapi()
            if not probe.get("ok"):
                info = dict(info)
                info["available"] = False
                info["reason"] = probe.get("message") or u"VAAPI 初始化失败"
        encoders = []
        if info.get("available"):
            encoders = [name for name in sorted(
                hwaccel.DRIVER_ENCODERS.get(info["driver"], ()))
                if not enc_set or name in enc_set]
            if not encoders:
                info = dict(info)
                info["available"] = False
                info["reason"] = u"内置 FFmpeg 缺少 VAAPI 编码器"
        state = dict(info)
        state["mode"] = mode
        state["encoders"] = encoders
        state["codecs"] = sorted(
            hwaccel.DRIVER_CODECS.get(info.get("driver") or "", ()))
        return state

    def capabilities(self):
        tool = self._ffmpeg
        available = bool(tool.available())
        encoders = tool.encoders() if available else []
        enc_set = frozenset(encoders)
        settings = self._settings_snapshot()
        hardware = self._hardware_state(tool, settings, enc_set)
        presets = []
        for preset in PRESETS:
            item = preset_view(preset, enc_set or None, hardware)
            if not available:
                item["available"] = False
                item["reason"] = u"未找到 FFmpeg（可在设置里指定路径）"
            presets.append(item)
        video_exts = [str(x).lower() for x in
                      (settings.get("video_extensions")
                       or config.DEFAULT_VIDEO_EXTENSIONS)]
        audio_exts = [str(x).lower() for x in config.CONVERT_AUDIO_EXTENSIONS]
        all_exts = sorted(set(video_exts) | set(audio_exts))
        selected = self._source_extensions(settings, all_exts)
        return {
            "ok": True,
            "ffmpeg": {
                "available": available,
                "version": tool.version() if available else u"unavailable",
                "path": tool.path if available else u"",
            },
            "presets": presets,
            "groups": {"video": [p["id"] for p in VIDEO_PRESETS],
                       "audio": [p["id"] for p in AUDIO_PRESETS]},
            "defaults": {
                "preset": settings.get("convert_default_preset")
                or config.CONVERT_PRESET_DEFAULT,
                "quality": settings.get("convert_quality")
                or config.CONVERT_QUALITY_DEFAULT,
                "speed": settings.get("convert_speed")
                or config.CONVERT_SPEED_DEFAULT,
                "max_resolution": settings.get("convert_max_resolution")
                if settings.get("convert_max_resolution") is not None else 0,
                "output_mode": settings.get("convert_output_mode")
                or config.CONVERT_OUTPUT_MODE_DEFAULT,
                "output_dir": settings.get("convert_output_dir") or u"",
                "output_subdir": settings.get("convert_output_subdir")
                or config.CONVERT_SUBDIR_DEFAULT,
                "overwrite": settings.get("convert_overwrite")
                or config.CONVERT_OVERWRITE_DEFAULT,
                "keep_source": settings.get("convert_delete_source",
                                            config.CONVERT_DELETE_SOURCE_DEFAULT) is False,
                "delete_source": settings.get("convert_delete_source",
                                              config.CONVERT_DELETE_SOURCE_DEFAULT),
                "copy_subtitles": settings.get("convert_copy_subtitles", True),
                "name_template": settings.get("convert_name_template")
                or config.CONVERT_NAME_TEMPLATE_DEFAULT,
                "source_extensions": selected,
            },
            "qualities": list(config.CONVERT_QUALITIES),
            "speeds": list(config.CONVERT_SPEEDS),
            "max_resolutions": list(config.CONVERT_MAX_RESOLUTIONS),
            "output_modes": list(config.CONVERT_OUTPUT_MODES),
            "overwrite_modes": list(config.CONVERT_OVERWRITE_MODES),
            "video_extensions": list(settings.get("video_extensions")
                                     or config.DEFAULT_VIDEO_EXTENSIONS),
            "audio_extensions": list(config.CONVERT_AUDIO_EXTENSIONS),
            # v0061：转换页「源文件后缀」多选（分组 + 全部 + 当前选中）
            "source_extensions": {
                "video": video_exts,
                "audio": audio_exts,
                "all": all_exts,
                "selected": selected,
                "max": config.CONVERT_SOURCE_EXTENSIONS_MAX,
            },
            # v0062：前端按 batch_max_items 自动分批提交（后端单次上限不变）
            "hardware": hardware,
            "limits": {
                "batch_max_items": config.CONVERT_BATCH_MAX_ITEMS,
                "enumerate_max_items": config.CONVERT_ENUMERATE_MAX_ITEMS,
                "job_limit": settings.get("convert_job_limit")
                or config.CONVERT_JOB_LIMIT_DEFAULT,
                "job_limit_max": config.CONVERT_JOB_LIMIT_MAX,
            },
            "concurrency": self._concurrency(),
            "counts": self.counts(),
            "offpeak": self._offpeak_state(settings),
            # v0058：转换是独立功能，开关键保留仅为兼容旧配置，始终可用。
            "enabled": True,
        }

    def create(self, user, payload):
        settings = self._settings_snapshot()
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        raw_paths = payload.get("paths")
        if raw_paths is None:
            single = payload.get("path")
            raw_paths = [single] if single else []
        if not isinstance(raw_paths, list):
            raise invalid(u"paths 必须是字符串数组")
        cleaned = []
        seen = set()
        for item in raw_paths:
            text = (item or u"").strip() if isinstance(item, str) else u""
            if not text:
                continue
            key = path_key(text)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text)
        if not cleaned:
            raise invalid(u"请至少选择一个文件")
        if len(cleaned) > config.CONVERT_BATCH_MAX_ITEMS:
            raise invalid(u"单次最多提交 %d 个文件"
                          % config.CONVERT_BATCH_MAX_ITEMS)
        preset_id = payload.get("preset") or settings.get(
            "convert_default_preset") or config.CONVERT_PRESET_DEFAULT
        if preset_id not in PRESET_MAP:
            raise ApiError("PRESET_UNKNOWN",
                           u"未知的输出预设: %s" % preset_id,
                           details={"presets": list(PRESET_IDS)})
        raw_options = payload.get("options") or {}
        options = self._options_for(settings, raw_options, preset_id)
        # v0061：删除源文件已是默认行为，所以 confirm 只对「本次显式要求删除」生效；
        # 沿用默认值时不再强制 token（旧客户端显式传 keep_source=false 同样要确认）。
        explicit_delete = isinstance(raw_options, dict) and (
            raw_options.get("delete_source") is True
            or (raw_options.get("delete_source") is None
                and raw_options.get("keep_source") is False))
        if explicit_delete \
                and str(payload.get("confirm") or u"").strip() != "DELETE SOURCE":
            raise invalid(
                u"删除源文件需要确认：请求体 confirm 必须是 \"DELETE SOURCE\"",
                code="INVALID_ARGUMENT")
        conflicts = []
        validated = []
        for path in cleaned:
            try:
                validated.append(self._validate_source(path))
            except ApiError as exc:
                conflicts.append({"path": path, "message": exc.message})
        if not validated:
            raise ApiError(
                "SOURCE_INVALID",
                u"选中的文件都不可用：%s" % (conflicts[0]["message"]
                                            if conflicts else u"未知原因"),
                details={"rejected": conflicts[:20]})
        with self._lock:
            active = sum(1 for job in self._jobs.values()
                         if job.status in ACTIVE_STATUSES)
            try:
                limit = int(settings.get("convert_job_limit")
                            or config.CONVERT_JOB_LIMIT_DEFAULT)
            except (TypeError, ValueError):
                limit = config.CONVERT_JOB_LIMIT_DEFAULT
            if active + len(validated) > limit:
                raise ApiError(
                    "JOB_LIMIT_REACHED",
                    u"活动转换任务已达上限（%d），请等待完成或清理后再提交"
                    % limit)
            created = []
            for path in validated:
                job = ConvertJob(make_job_id(), path, preset_id,
                                 options=dict(options), user=user)
                self._jobs[job.job_id] = job
                self._queue.append(job.job_id)
                created.append(job)
            self._prune_locked(limit)
        for job in created:
            self._persist(job, force=True)
        return {
            "ok": True,
            "count": len(created),
            "jobs": [job.snapshot() for job in created],
            "rejected": conflicts[:20],
            "counts": self.counts(),
        }

    def _validate_source(self, path):
        normalized = check_path_safety(path)
        if not is_regular_file(normalized):
            raise invalid(u"文件不存在或不是普通文件: %s" % normalized)
        # validate_root 校验的是"目录"；源文件要拿所在目录去校验，
        # 再确认文件本身确实在这个共享根之下（防符号链接跳出）。
        parent = os.path.dirname(normalized)
        share = self._shares.validate_root(parent)
        root = (share or {}).get("path") or u""
        if root and not is_under(normalized, root):
            raise invalid(u"文件不在共享文件夹内: %s" % normalized)
        if root and has_symlink_component(normalized, root):
            raise invalid(u"不允许转换符号链接路径: %s" % normalized)
        return normalized

    def _options_for(self, settings, overrides, preset_id):
        if not isinstance(overrides, dict):
            overrides = {}

        def pick(name, default=None):
            if name in overrides and overrides[name] not in (None, u""):
                return overrides[name]
            return default

        quality = pick("quality", settings.get("convert_quality")
                       or config.CONVERT_QUALITY_DEFAULT)
        if quality not in config.CONVERT_QUALITIES:
            raise invalid(u"quality 只能是 %s"
                          % "/".join(config.CONVERT_QUALITIES))
        speed = pick("speed", settings.get("convert_speed")
                     or config.CONVERT_SPEED_DEFAULT)
        if speed not in config.CONVERT_SPEEDS:
            raise invalid(u"speed 只能是 %s" % "/".join(config.CONVERT_SPEEDS))
        try:
            max_resolution = int(pick(
                "max_resolution", settings.get("convert_max_resolution") or 0))
        except (TypeError, ValueError):
            max_resolution = 0
        if max_resolution not in config.CONVERT_MAX_RESOLUTIONS:
            raise invalid(u"max_resolution 只能是 %s"
                          % "/".join(str(v) for v in
                                     config.CONVERT_MAX_RESOLUTIONS))
        output_mode = pick("output_mode", settings.get("convert_output_mode")
                           or config.CONVERT_OUTPUT_MODE_DEFAULT)
        if output_mode not in config.CONVERT_OUTPUT_MODES:
            raise invalid(u"output_mode 只能是 %s"
                          % "/".join(config.CONVERT_OUTPUT_MODES))
        overwrite = pick("overwrite", settings.get("convert_overwrite")
                         or config.CONVERT_OVERWRITE_DEFAULT)
        if overwrite not in config.CONVERT_OVERWRITE_MODES:
            raise invalid(u"overwrite 只能是 %s"
                          % "/".join(config.CONVERT_OVERWRITE_MODES))
        channels = str(pick("channels", u"") or u"").strip().lower()
        if channels not in (u"", u"keep", u"stereo"):
            raise invalid(u"channels 只能是 keep/stereo")
        output_dir = str(pick("output_dir", u"") or u"").strip()
        if output_dir and not os.path.isabs(output_dir):
            raise invalid(u"output_dir 必须是绝对路径")
        output_subdir = str(pick("output_subdir", u"") or u"").strip()
        name_template = str(pick("name_template",
                                 settings.get("convert_name_template")
                                 or config.CONVERT_NAME_TEMPLATE_DEFAULT)
                            or u"").strip()
        if len(name_template) > config.CONVERT_NAME_TEMPLATE_MAX:
            raise invalid(u"name_template 过长")
        # v0061：默认「转换成功后删除源文件」，默认值来自 convert_delete_source。
        # 兼容旧客户端：显式给了 keep_source 而没给 delete_source 时，尊重 keep_source。
        delete_default = bool(settings.get(
            "convert_delete_source", config.CONVERT_DELETE_SOURCE_DEFAULT))
        if "delete_source" in overrides:
            delete_source = bool(overrides.get("delete_source"))
        elif "keep_source" in overrides:
            delete_source = not bool(overrides.get("keep_source"))
        else:
            delete_source = delete_default
        keep_source = not delete_source
        copy_subtitles = bool(pick(
            "copy_subtitles", settings.get("convert_copy_subtitles", True)))
        return {
            "quality": quality,
            "speed": speed,
            "max_resolution": max_resolution,
            "channels": "" if channels == "keep" else channels,
            "output_mode": output_mode,
            "output_dir": output_dir,
            "output_subdir": output_subdir,
            "overwrite": overwrite,
            "name_template": name_template,
            "keep_source": keep_source,
            "delete_source": delete_source,
            "copy_subtitles": copy_subtitles,
        }

    # -- 对外：枚举 -----------------------------------------------------
    def enumerate_files(self, payload):
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        path = payload.get("path") or u""
        if not isinstance(path, str) or not path.strip():
            raise invalid(u"path 不能为空")
        recursive = payload.get("recursive", True)
        if not isinstance(recursive, bool):
            raise invalid(u"recursive 必须是布尔值")
        normalized = check_path_safety(path)
        self._shares.validate_root(normalized)
        if not is_dir_nofollow(normalized):
            raise ApiError("INVALID_DIRECTORY",
                           u"目录不存在或不是目录: %s" % normalized)
        settings = self._settings_snapshot()
        supported = [str(x).lower() for x in
                     (settings.get("video_extensions")
                      or config.DEFAULT_VIDEO_EXTENSIONS)]
        supported += [str(x).lower() for x in config.CONVERT_AUDIO_EXTENSIONS]
        supported_set = frozenset(item if item.startswith(".") else "." + item
                                  for item in supported)
        raw_ext = payload.get("extensions")
        if raw_ext is None:
            allowed = self._source_extensions(settings, sorted(supported_set))
        elif isinstance(raw_ext, list):
            allowed = [str(x).lower() for x in raw_ext if str(x).strip()]
            if not allowed:
                raise invalid(u"extensions 不能为空数组（不选就传 null 用默认值）")
            unknown = [item for item in allowed
                       if (item if item.startswith(".") else "." + item)
                       not in supported_set]
            if unknown:
                raise invalid(u"不支持的后缀：%s"
                              % u", ".join(sorted(unknown)[:10]))
        else:
            raise invalid(u"extensions 必须是字符串数组")
        allowed_set = frozenset(item if item.startswith(".") else "." + item
                                for item in allowed)
        limit = int(config.CONVERT_ENUMERATE_MAX_ITEMS)
        skipped = {"system": 0, "hidden": 0, "symlink": 0,
                   "permission": 0, "other": 0, "extension": 0}
        items = []
        stack = [normalized]
        while stack and len(items) < limit:
            current = stack.pop()
            try:
                with os.scandir(current) as iterator:
                    entries = list(iterator)
            except PermissionError:
                skipped["permission"] += 1
                continue
            except OSError:
                skipped["other"] += 1
                continue
            entries.sort(key=lambda item: item.name.lower())
            for entry in entries:
                if len(items) >= limit:
                    break
                name = entry.name
                try:
                    is_symlink = entry.is_symlink()
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    skipped["other"] += 1
                    continue
                if is_symlink:
                    skipped["symlink"] += 1
                    continue
                if is_system_name(name):
                    skipped["system"] += 1
                    continue
                if is_hidden_name(name):
                    skipped["hidden"] += 1
                    continue
                if is_dir:
                    if recursive:
                        stack.append(entry.path)
                    continue
                if not is_safe_dir_name(name):
                    skipped["system"] += 1
                    continue
                ext = extension_of(name).lower()
                if ext not in allowed_set:
                    skipped["extension"] += 1
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                    size = int(st.st_size)
                    mtime = float(st.st_mtime)
                except OSError:
                    skipped["permission"] += 1
                    continue
                items.append({
                    "path": entry.path,
                    "name": name,
                    "size": size,
                    "modified_epoch": mtime,
                    "extension": ext,
                })
        items.sort(key=lambda item: item["path"].lower())
        return {
            "ok": True,
            "path": normalized,
            "items": items,
            "count": len(items),
            "total_bytes": sum(item["size"] for item in items),
            "skipped": skipped,
            "truncated": bool(stack) or len(items) >= limit,
            "limit": limit,
            "extensions": sorted(allowed_set),
        }

    # -- 对外：查询 -----------------------------------------------------
    def counts(self):
        with self._lock:
            jobs = list(self._jobs.values())
        out = {status: 0 for status in STATUSES}
        for job in jobs:
            out[job.status] = out.get(job.status, 0) + 1
        out["total"] = len(jobs)
        out["active"] = sum(out.get(s, 0) for s in ACTIVE_STATUSES)
        return out

    def list_page(self, status=None, offset=0, limit=50, q=None):
        wanted = None
        if status:
            parts = [part.strip() for part in str(status).split(",")
                     if part.strip()]
            wanted = [item for item in parts if item in STATUSES] or None
        needle = (q or u"").strip().lower()
        with self._lock:
            jobs = list(self._jobs.values())
        rows = []
        for job in jobs:
            if wanted and job.status not in wanted:
                continue
            if needle:
                haystack = u" ".join([job.title or u"",
                                      job.source_path or u"",
                                      job.output_name or u"",
                                      job.preset or u""]).lower()
                if needle not in haystack:
                    continue
            rows.append(job)
        rows.sort(key=lambda item: item.created_epoch, reverse=True)
        try:
            offset = max(0, int(offset or 0))
            limit = max(1, min(int(limit or 50), 500))
        except (TypeError, ValueError):
            offset, limit = 0, 50
        page = rows[offset:offset + limit]
        return {
            "ok": True,
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "jobs": [job.snapshot() for job in page],
            "counts": self.counts(),
        }

    def queue_snapshot(self, active_limit=20, queued_limit=50,
                       recent_limit=20):
        with self._lock:
            jobs = list(self._jobs.values())
            queued_ids = list(self._queue)
        active = [job for job in jobs if job.status == STATUS_RUNNING]
        active.sort(key=lambda item: item.started_epoch or 0)
        position = {job_id: index + 1 for index, job_id in enumerate(queued_ids)}
        queued = [job for job in jobs if job.status == STATUS_QUEUED]
        queued.sort(key=lambda item: item.created_epoch)
        ordered = []
        for job in queued:
            item = job.snapshot()
            item["queue_position"] = position.get(job.job_id)
            ordered.append(item)
        recent = [job for job in jobs if job.status in FINAL_STATUSES]
        recent.sort(key=lambda item: item.finished_epoch or 0, reverse=True)
        return {
            "ok": True,
            "active": [job.snapshot() for job in active[:active_limit]],
            "queued": ordered[:queued_limit],
            "recent": [job.snapshot() for job in recent[:recent_limit]],
            "counts": self.counts(),
            "concurrency": self._concurrency(),
            "offpeak": self._offpeak_state(),
            "storage": self.storage_stats(),
        }

    def storage_stats(self, directory=None):
        settings = self._settings_snapshot()
        raw = (directory or settings.get("convert_output_dir") or u"").strip()
        if not raw:
            return None
        try:
            usage = shutil.disk_usage(raw)
        except OSError:
            return None
        total = float(usage.total or 0)
        return {
            "path": raw,
            "free_bytes": int(usage.free),
            "used_bytes": int(usage.used),
            "total_bytes": int(total),
            "used_pct": round(100.0 * float(usage.used) / total, 1)
            if total else 0.0,
            "min_free_gb": int(settings.get("convert_min_free_gb")
                               or config.CONVERT_MIN_FREE_GB_DEFAULT),
        }

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise not_found(u"转换任务不存在: %s" % job_id)
        return job

    def get_view(self, job_id):
        return self.get(job_id).snapshot(include_detail=True)

    def export_rows(self, status=None):
        listing = self.list_page(status=status, offset=0, limit=500)
        rows = []
        for item in listing["jobs"]:
            rows.append({
                "job_id": item["job_id"],
                "source_path": item["source_path"],
                "preset": item["preset"],
                "status": item["status"],
                "progress": item["progress"],
                "output_path": item["output_path"],
                "source_bytes": item["source_bytes"],
                "output_size": item["output_size"],
                "created_at": item["created_at"] or u"",
                "finished_at": item["finished_at"] or u"",
                "error": item["error_message"] or u"",
            })
        return rows

    # -- 对外：控制 -----------------------------------------------------
    def cancel(self, job_id):
        job = self.get(job_id)
        with self._lock:
            if job.status in FINAL_STATUSES:
                raise ApiError("CONVERT_BUSY", u"任务已结束")
            if job.status == STATUS_QUEUED:
                if job.job_id in self._queue:
                    self._queue.remove(job.job_id)
                job.status = STATUS_CANCELLED
                job.message = u"已取消"
                job.finished_epoch = time.time()
                job._cancel.set()
                self._cleanup_part(job)
            else:
                job._cancel.set()
                job.message = u"取消请求已提交"
        self._persist(job, force=True)
        return job.snapshot()

    def retry(self, job_id):
        job = self.get(job_id)
        with self._lock:
            if job.status not in FINAL_STATUSES:
                raise ApiError("CONVERT_BUSY", u"任务尚未结束")
            job.status = STATUS_QUEUED
            job.stage = STATUS_QUEUED
            job.message = u"重新排队"
            job.error_code = u""
            job.error_message = u""
            job.progress = 0
            job.out_time = 0.0
            job.finished_epoch = 0.0
            job._cancel.clear()
            if job.job_id not in self._queue:
                self._queue.append(job.job_id)
        self._persist(job, force=True)
        return job.snapshot()

    def delete(self, job_id, delete_output=False):
        job = self.get(job_id)
        with self._lock:
            if job.status not in FINAL_STATUSES:
                raise ApiError("CONVERT_BUSY", u"任务尚未结束，不能删除")
            self._jobs.pop(job_id, None)
            if job_id in self._queue:
                self._queue.remove(job_id)
        removed = False
        if delete_output and job.output_path and os.path.isfile(job.output_path):
            try:
                base = check_path_safety(job.output_path)
                # 只删除本任务产物：路径必须与记录一致且是普通文件
                if is_regular_file(base):
                    os.remove(base)
                    removed = True
            except (ApiError, OSError):
                removed = False
        self._cleanup_part(job)
        try:
            self.store.delete_convert_job(job_id)
        except Exception:
            logger.exception(u"删除转换任务失败: %s", job_id)
        return {"ok": True, "deleted": job_id, "deleted_output": removed}

    def bulk_action(self, user, action, scope="all"):
        action = str(action or "").strip().lower()
        if action not in ("cancel_all", "retry_failed", "clear_finished"):
            raise invalid(u"action 只能是 cancel_all/retry_failed/clear_finished")
        affected = 0
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if action == "cancel_all":
                if job.status in FINAL_STATUSES:
                    continue
                if job.status == STATUS_QUEUED:
                    if job.job_id in self._queue:
                        self._queue.remove(job.job_id)
                    job.status = STATUS_CANCELLED
                    job.message = u"已取消（批量）"
                    job.finished_epoch = time.time()
                    job._cancel.set()
                    self._cleanup_part(job)
                else:
                    job._cancel.set()
                    job.message = u"取消请求已提交"
                affected += 1
                self._persist(job, force=True)
            elif action == "retry_failed":
                if job.status != STATUS_FAILED:
                    continue
                with self._lock:
                    job.status = STATUS_QUEUED
                    job.stage = STATUS_QUEUED
                    job.message = u"重新排队"
                    job.error_code = u""
                    job.error_message = u""
                    job.progress = 0
                    job.out_time = 0.0
                    job.finished_epoch = 0.0
                    job._cancel.clear()
                    if job.job_id not in self._queue:
                        self._queue.append(job.job_id)
                affected += 1
                self._persist(job, force=True)
            elif action == "clear_finished":
                if job.status not in FINAL_STATUSES:
                    continue
                with self._lock:
                    self._jobs.pop(job.job_id, None)
                try:
                    self.store.delete_convert_job(job.job_id)
                except Exception:
                    pass
                affected += 1
        try:
            self.store.audit("convert_bulk", user,
                             {"action": action, "scope": scope,
                              "affected": affected})
        except Exception:
            pass
        return {"ok": True, "action": action, "affected": affected,
                "counts": self.counts()}

    # -- 清理 -----------------------------------------------------------
    def _prune_locked(self, limit):
        if limit <= 0:
            return
        finished = [job for job in self._jobs.values()
                    if job.status in FINAL_STATUSES]
        if len(finished) <= limit:
            return
        finished.sort(key=lambda item: item.finished_epoch
                      or item.created_epoch)
        for job in finished[:len(finished) - limit]:
            self._jobs.pop(job.job_id, None)
            try:
                self.store.delete_convert_job(job.job_id)
            except Exception:
                logger.exception(u"清理转换任务失败: %s", job.job_id)
