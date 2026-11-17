# -*- coding: utf-8 -*-
"""音视频合成服务（v0059，docs/AV_MERGE_CONTRACT.md）。

职责
----
- 把用户选择的**视频片段**按选择顺序拼接成画面、**音频片段**按选择顺序拼接成
  声音，再混流（复用原编码或统一转码）成一个最终音视频文件；
- 只有一个视频 + 一个音频时退化为混流；音频为空时输出无声视频；
- 音频时长以视频为准：短了静音补齐、长了截断（容差见 config）；
- 队列：并发（默认 1）+ FIFO、进度、取消、重试、重启恢复；
- 落盘：先写 `<目标>.dcpart`，校验通过后 `os.replace` 原子改名。

安全约定
--------
- 源文件必须在共享文件夹内、必须是普通文件、拒绝符号链接；
- 产物默认不覆盖已有文件（rename/skip/overwrite，默认 rename）；
- 「合成成功后删除源文件」必须 `confirm="DELETE SOURCE"`，且仅在产物校验通过后执行；
- 输出目录必须可写，剩余空间不低于 `merge_min_free_gb`。
"""

from __future__ import absolute_import

import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid

from . import config
from .convert import parse_probe_text, parse_progress_line
from .errors import ApiError, invalid, not_found
from .fsutil import (check_path_safety, extension_of, has_symlink_component,
                     is_dir_nofollow, is_hidden_name, is_regular_file,
                     is_safe_dir_name, is_system_name, is_under, path_key,
                     writable_dir)
from .timeutil import iso_local
from .video_similarity import (FFmpegTool, VideoSkip, VideoStopped,
                               VideoTimeout)

logger = logging.getLogger("dupcleaner.merge")

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
    "probe": u"探测素材",
    "video": u"拼接画面",
    "audio": u"拼接声音",
    "mux": u"混流输出",
    "verify": u"校验产物",
    "done": u"完成",
}

_NAME_MAX = 120
_BAD_NAME_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s+fps\b", re.IGNORECASE)
_AUDIO_PARAM_RE = re.compile(
    r"Audio:.*?(\d+)\s*Hz,\s*([^,]+),\s*([^,]+)", re.IGNORECASE)


class AVMergeError(ApiError):
    """合成业务错误（v0059）：直接继承 ApiError，HTTP 状态按错误码映射。"""

    def __init__(self, code, message, details=None):
        super(AVMergeError, self).__init__(code, message, details=details)


class _JobCancelled(Exception):
    pass


def make_job_id():
    return "am-" + uuid.uuid4().hex[:12]


def _human_bytes(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.0
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return u"%d %s" % (value, units[index])
    return u"%.1f %s" % (value, units[index])


def _one_line(text, limit=400):
    text = re.sub(r"\s+", " ", u"%s" % (text or u"")).strip()
    return text[:limit]


def _clean_subdir(text):
    text = u"%s" % (text or u"")
    text = text.strip().strip("/")
    if not text or text in (".", "..") or "/" in text or "\\" in text:
        return config.AVMERGE_SUBDIR_DEFAULT
    return text


def _concat_escape(path):
    return path.replace("'", "'\\''")


def build_concat_list(paths):
    """生成 concat demuxer 清单文本（路径单引号转义）。"""
    lines = []
    for path in paths:
        lines.append(u"file '%s'" % _concat_escape(path))
    return u"\n".join(lines) + u"\n"


def probe_media(text):
    """解析 `ffmpeg -i` stderr：复用 convert 的解析并补 fps/音频参数。"""
    info = parse_probe_text(text)
    fps = None
    for line in (text or u"").splitlines():
        if "Video:" in line:
            match = _FPS_RE.search(line)
            if match:
                try:
                    fps = float(match.group(1))
                except (TypeError, ValueError):
                    fps = None
                break
    sample_rate = 0
    channels = u""
    for line in (text or u"").splitlines():
        if "Audio:" in line:
            match = _AUDIO_PARAM_RE.search(line)
            if match:
                try:
                    sample_rate = int(match.group(1))
                except (TypeError, ValueError):
                    sample_rate = 0
                channels = (match.group(2) or u"").strip()
                break
    info["fps"] = fps
    info["sample_rate"] = sample_rate
    info["channels"] = channels
    return info


def video_copy_compatible(infos, container):
    """同编码/同分辨率/同像素格式/同帧率才允许 `-c copy` 拼视频。"""
    if not infos:
        return False, u"没有视频片段"
    first = infos[0]
    for item in infos[1:]:
        if (item.get("video_codec") or u"") != (first.get("video_codec") or u""):
            return False, u"视频编码不一致"
        if (int(item.get("width") or 0), int(item.get("height") or 0)) != \
                (int(first.get("width") or 0), int(first.get("height") or 0)):
            return False, u"分辨率不一致"
        if (item.get("pix_fmt") or u"") != (first.get("pix_fmt") or u""):
            return False, u"像素格式不一致"
        if item.get("fps") and first.get("fps") \
                and abs(float(item["fps"]) - float(first["fps"])) > 0.01:
            return False, u"帧率不一致"
    codec = (first.get("video_codec") or u"").lower()
    if container == "mp4" and codec not in config.AVMERGE_MP4_VIDEO_CODECS:
        return False, u"该视频编码不适合 MP4 容器（%s）" % (codec or u"未知")
    return True, u""


def audio_copy_compatible(infos, container):
    """音频拼接要求同编码/同采样率/同声道布局。"""
    if not infos:
        return True, u""
    first = infos[0]
    for item in infos[1:]:
        if (item.get("audio_codec") or u"") != (first.get("audio_codec") or u""):
            return False, u"音频编码不一致"
        if int(item.get("sample_rate") or 0) != int(first.get("sample_rate") or 0):
            return False, u"音频采样率不一致"
        if (item.get("channels") or u"") != (first.get("channels") or u""):
            return False, u"音频声道不一致"
    codec = (first.get("audio_codec") or u"").lower()
    if container == "mp4" and codec not in config.AVMERGE_MP4_AUDIO_CODECS:
        return False, u"该音频编码不适合 MP4 容器（%s）" % (codec or u"未知")
    return True, u""


def choose_strategy(mode, video_infos, audio_infos, container):
    """返回 (strategy, reason)；copy 模式不兼容时抛 AVMERGE_INCOMPATIBLE。"""
    ok_video, reason_video = video_copy_compatible(video_infos, container)
    ok_audio, reason_audio = audio_copy_compatible(audio_infos, container)
    if mode == "encode":
        return "encode", u"按要求统一转码"
    if ok_video and ok_audio:
        return "copy", u"编码一致，直接拼接（秒级）"
    reason = reason_video or reason_audio or u"素材参数不一致"
    if mode == "copy":
        raise AVMergeError("MERGE_INCOMPATIBLE", u"无法直接拼接：%s" % reason)
    return "encode", u"%s，将统一转码" % reason


def _video_filter_graph(count, width, height, fps):
    parts = []
    for index in range(count):
        parts.append(
            u"[%d:v:0]scale=%d:%d:force_original_aspect_ratio=decrease,"
            u"pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=%s[v%d]"
            % (index, width, height, width, height, fps, index))
    labels = u"".join(u"[v%d]" % i for i in range(count))
    parts.append(u"%sconcat=n=%d:v=1:a=0[vout]" % (labels, count))
    return u";".join(parts)


def _audio_filter_graph(count):
    parts = []
    for index in range(count):
        parts.append(
            u"[%d:a:0]aresample=48000,"
            u"aformat=sample_fmts=fltp:channel_layouts=stereo[a%d]"
            % (index, index))
    labels = u"".join(u"[a%d]" % i for i in range(count))
    parts.append(u"%sconcat=n=%d:v=0:a=1[aout]" % (labels, count))
    return u";".join(parts)


def target_size(video_infos, max_resolution=0):
    first = video_infos[0] if video_infos else {}
    width = int(first.get("width") or 0)
    height = int(first.get("height") or 0)
    if width <= 0 or height <= 0:
        width, height = 1920, 1080
    width -= width % 2
    height -= height % 2
    cap = int(max_resolution or 0)
    if cap > 0 and height > cap:
        ratio = float(cap) / float(height)
        width = int(width * ratio)
        height = cap
        width -= width % 2
    return max(2, width), max(2, height)


def target_fps(video_infos):
    for item in video_infos:
        fps = item.get("fps")
        if fps and 1 <= float(fps) <= 120:
            return round(float(fps), 3)
    return 30


def _quality_crf(quality):
    return int(config.AVMERGE_CRF.get(quality or config.AVMERGE_QUALITY_DEFAULT,
                                    config.AVMERGE_CRF["standard"]))


def _audio_kbps(quality):
    return int(config.AVMERGE_AUDIO_KBPS.get(
        quality or config.AVMERGE_QUALITY_DEFAULT,
        config.AVMERGE_AUDIO_KBPS["standard"]))


class AVMergeJob(object):
    def __init__(self, job_id, video_paths, audio_paths, options=None,
                 user=u"", status=STATUS_QUEUED, created_epoch=None):
        self.job_id = job_id
        self.video_paths = list(video_paths or [])
        self.audio_paths = list(audio_paths or [])
        self.options = dict(options or {})
        self.user = user or u""
        self.status = status
        self.stage = STATUS_QUEUED
        self.progress = 0.0
        self.message = u""
        self.strategy = u""
        self.output_path = u""
        self.output_name = u""
        self.output_size = 0
        self.duration = 0.0
        self.video_duration = 0.0
        self.audio_duration = 0.0
        self.returncode = None
        self.error_code = u""
        self.error_message = u""
        self.verify = {}
        self.steps = []
        self.created_epoch = float(created_epoch or time.time())
        self.started_epoch = None
        self.finished_epoch = None
        self.updated_epoch = self.created_epoch
        self._cancel = threading.Event()

    @property
    def finished(self):
        return self.status in FINAL_STATUSES

    def cancel_requested(self):
        return self._cancel.is_set()

    def step(self, key, status="running", detail=None):
        entry = {"key": key, "label": STAGE_LABELS.get(key, key),
                 "status": status, "at": time.time()}
        if detail:
            entry["detail"] = _one_line(detail, 200)
        self.steps.append(entry)
        self.steps = self.steps[-30:]

    def snapshot(self, include_detail=False):
        data = {
            "job_id": self.job_id,
            "status": self.status,
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "progress": round(max(0.0, min(100.0, self.progress)), 1),
            "message": self.message,
            "strategy": self.strategy,
            "video_paths": list(self.video_paths),
            "audio_paths": list(self.audio_paths),
            "video_count": len(self.video_paths),
            "audio_count": len(self.audio_paths),
            "output_path": self.output_path,
            "output_name": self.output_name,
            "output_size": self.output_size,
            "duration_sec": round(self.duration, 2),
            "video_duration_sec": round(self.video_duration, 2),
            "audio_duration_sec": round(self.audio_duration, 2),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "verify": dict(self.verify),
            "created_at": iso_local(self.created_epoch),
            "started_at": iso_local(self.started_epoch),
            "finished_at": iso_local(self.finished_epoch),
            "created_epoch": self.created_epoch,
            "options": dict(self.options),
        }
        if include_detail:
            data["steps"] = list(self.steps)
        return data

    def to_record(self):
        return {
            "job_id": self.job_id,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "message": self.message,
            "strategy": self.strategy,
            "video_paths": list(self.video_paths),
            "audio_paths": list(self.audio_paths),
            "options": dict(self.options),
            "user": self.user,
            "output_path": self.output_path,
            "output_name": self.output_name,
            "output_size": self.output_size,
            "duration": self.duration,
            "video_duration": self.video_duration,
            "audio_duration": self.audio_duration,
            "returncode": self.returncode,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "verify": dict(self.verify),
            "steps": list(self.steps),
            "created_epoch": self.created_epoch,
            "started_epoch": self.started_epoch,
            "finished_epoch": self.finished_epoch,
            "updated_epoch": self.updated_epoch,
        }

    @classmethod
    def from_record(cls, record):
        job = cls(record.get("job_id") or make_job_id(),
                  record.get("video_paths") or [],
                  record.get("audio_paths") or [],
                  record.get("options") or {},
                  record.get("user") or u"",
                  record.get("status") or STATUS_QUEUED,
                  record.get("created_epoch"))
        for key in ("stage", "progress", "message", "strategy", "output_path",
                    "output_name", "output_size", "duration",
                    "video_duration", "audio_duration", "returncode",
                    "error_code", "error_message", "started_epoch",
                    "finished_epoch", "updated_epoch"):
            setattr(job, key, record.get(key, getattr(job, key)))
        job.verify = dict(record.get("verify") or {})
        job.steps = list(record.get("steps") or [])
        return job


class AVMergeService(object):
    def __init__(self, runtime_config, store, share_service, settings_provider,
                 ffmpeg_tool=None):
        self.config = runtime_config
        self.store = store
        self._shares = share_service
        self._settings = settings_provider
        self._lock = threading.RLock()
        self._jobs = {}
        self._queue = []
        self._running = {}
        self._stop = threading.Event()
        self._dispatcher = None
        self._ffmpeg = ffmpeg_tool or FFmpegTool(
            runtime_config=runtime_config,
            settings_provider=settings_provider)
        self._load_jobs()

    # -- 生命周期 -------------------------------------------------------
    def start(self):
        if self._dispatcher is not None:
            return
        self._stop.clear()
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="am-dispatch", daemon=True)
        self._dispatcher.start()
        logger.info(u"音视频合成调度已启动（并发上限 %s）", self._concurrency())

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
            records = self.store.load_avmerge_jobs(limit=1000)
        except Exception:
            logger.exception(u"合成任务加载失败")
            records = []
        for record in records:
            if record.get("status") == STATUS_RUNNING:
                record["status"] = STATUS_QUEUED
                record["stage"] = STATUS_QUEUED
                record["message"] = u"服务重启，重新排队"
                record["progress"] = 0
            try:
                job = AVMergeJob.from_record(record)
            except Exception:
                logger.exception(u"合成任务记录损坏，已跳过")
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
                "avmerge_concurrency") or 0)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            value = config.AVMERGE_CONCURRENCY_DEFAULT
        return max(1, min(value, config.AVMERGE_CONCURRENCY_MAX))

    # -- 调度 -----------------------------------------------------------
    def _dispatch_loop(self):
        while not self._stop.wait(0.3):
            try:
                self._dispatch_once()
            except Exception:
                logger.exception(u"合成调度循环异常")

    def _dispatch_once(self):
        limit = self._concurrency()
        with self._lock:
            while len(self._running) < limit and self._queue:
                job_id = self._queue.pop(0)
                job = self._jobs.get(job_id)
                if job is None or job.status != STATUS_QUEUED:
                    continue
                job.status = STATUS_RUNNING
                job.stage = "probe"
                job.started_epoch = time.time()
                job.updated_epoch = job.started_epoch
                thread = threading.Thread(
                    target=self._run_job, args=(job,),
                    name="am-%s" % job.job_id, daemon=True)
                self._running[job.job_id] = thread
                thread.start()
            self._persist_dirty_locked()

    def _run_job(self, job):
        try:
            self._execute(job)
            if job.cancel_requested():
                self._finish(job, STATUS_CANCELLED, u"已取消")
            else:
                self._finish(job, STATUS_DONE, u"合成完成")
        except _JobCancelled:
            self._cleanup_part(job)
            self._finish(job, STATUS_CANCELLED, u"已取消")
        except AVMergeError as exc:
            self._cleanup_part(job)
            job.error_code = exc.code
            job.error_message = exc.message
            self._finish(job, STATUS_FAILED, exc.message)
        except Exception as exc:
            logger.exception(u"合成任务异常: %s", job.job_id)
            self._cleanup_part(job)
            job.error_code = "MERGE_FAILED"
            job.error_message = _one_line(u"%s" % exc)
            self._finish(job, STATUS_FAILED, u"合成失败：%s" % _one_line(exc))
        finally:
            with self._lock:
                self._running.pop(job.job_id, None)
            self._persist(job, force=True)
            self._prune_locked()

    def _execute(self, job):
        self._check_cancelled(job)
        self._step(job, "probe")
        video_infos = []
        for path in job.video_paths:
            info = self._probe(job, path)
            if not info.get("has_video"):
                raise AVMergeError("SOURCE_INVALID",
                                 u"视频片段没有视频轨：%s"
                                 % os.path.basename(path))
            video_infos.append(info)
        audio_infos = []
        for path in job.audio_paths:
            info = self._probe(job, path)
            if not info.get("has_audio"):
                raise AVMergeError("SOURCE_INVALID",
                                 u"音频片段没有音频轨：%s"
                                 % os.path.basename(path))
            audio_infos.append(info)
        job.video_duration = sum(float(i.get("duration") or 0)
                                 for i in video_infos)
        job.audio_duration = sum(float(i.get("duration") or 0)
                                 for i in audio_infos)
        container = (job.options.get("container")
                     or self._settings_snapshot().get("avmerge_container")
                     or config.AVMERGE_CONTAINER_DEFAULT)
        mode = (job.options.get("mode")
                or self._settings_snapshot().get("avmerge_mode")
                or config.AVMERGE_MODE_DEFAULT)
        strategy, reason = choose_strategy(mode, video_infos, audio_infos,
                                           container)
        job.strategy = strategy
        self._step(job, "probe", "done", reason)
        self._ensure_free_space(job)
        output_dir = self._resolve_output_dir(job, video_infos)
        target, overwrite = self._resolve_target(job, output_dir,
                                                 video_infos, container)
        part = target + u".dcpart"
        job._part_path = part
        job.output_name = os.path.basename(target)
        workdir = tempfile.mkdtemp(prefix="dcavmerge-")
        try:
            video_part = os.path.join(workdir, "video.mkv")
            audio_part = os.path.join(workdir, "audio.m4a")
            self._build_video(job, video_infos, workdir, video_part,
                              strategy, container)
            video_info = self._probe(job, video_part)
            job.duration = float(video_info.get("duration") or 0) \
                or job.video_duration
            has_audio = bool(job.audio_paths)
            if has_audio:
                self._build_audio(job, audio_infos, workdir, audio_part,
                                  strategy, job.duration)
            self._mux(job, video_part, audio_part if has_audio else u"",
                      part, job.duration, container)
            self._verify(job, part, job.duration, has_audio)
            if job.cancel_requested():
                raise _JobCancelled()
            if overwrite and os.path.exists(target):
                os.replace(part, target)
            else:
                os.replace(part, target)
            job.output_path = target
            job.output_size = os.path.getsize(target)
            job.stage = "done"
            job.progress = 100.0
            self._step(job, "done", "done")
            if job.options.get("delete_source") \
                    and job.options.get("delete_source_confirmed"):
                self._delete_sources(job)
        finally:
            self._cleanup_part(job)
            shutil.rmtree(workdir, ignore_errors=True)

    # -- 各阶段 ---------------------------------------------------------
    def _build_video(self, job, infos, workdir, output, strategy, container):
        self._check_cancelled(job)
        job.stage = "video"
        self._step(job, "video")
        self._set_progress(job, 0)
        if strategy == "copy":
            list_path = os.path.join(workdir, "video.txt")
            self._write_text(list_path,
                             build_concat_list(job.video_paths))
            args = ["-nostdin", "-hide_banner", "-loglevel", "error",
                    "-progress", "pipe:1", "-nostats",
                    "-f", "concat", "-safe", "0", "-i", list_path,
                    "-map", "0:v:0", "-an", "-c", "copy",
                    "-f", "matroska", output]
            timeout = self._timeout(job.video_duration, False)
        else:
            width, height = target_size(
                infos, self._settings_snapshot().get("avmerge_max_resolution")
                or config.AVMERGE_MAX_RESOLUTION_DEFAULT)
            fps = target_fps(infos)
            args = ["-nostdin", "-hide_banner", "-loglevel", "error"]
            for path in job.video_paths:
                args.extend(["-i", path])
            args.extend([
                "-filter_complex",
                _video_filter_graph(len(job.video_paths), width, height, fps),
                "-map", "[vout]", "-an",
                "-c:v", config.AVMERGE_DEFAULT_VIDEO_ENCODER,
                "-preset", "veryfast", "-crf",
                str(_quality_crf(self._settings_snapshot().get("avmerge_quality"))),
                "-pix_fmt", "yuv420p",
                "-progress", "pipe:1", "-nostats",
                "-f", "matroska", output])
            timeout = self._timeout(job.video_duration, True)
        code = self._run_ffmpeg(job, args, timeout, job.video_duration,
                                weight=60.0)
        if code != 0:
            raise AVMergeError("MERGE_FAILED", u"拼接画面失败（FFmpeg 返回 %s）"
                             % code)
        self._step(job, "video", "done")

    def _build_audio(self, job, infos, workdir, output, strategy, duration):
        self._check_cancelled(job)
        job.stage = "audio"
        self._step(job, "audio")
        settings = self._settings_snapshot()
        container = (job.options.get("container")
                     or settings.get("avmerge_container")
                     or config.AVMERGE_CONTAINER_DEFAULT)
        ok, _reason = audio_copy_compatible(infos, container)
        close = abs(job.audio_duration - duration) <= \
            float(config.AVMERGE_VERIFY_TOLERANCE_SEC)
        first_codec = (infos[0].get("audio_codec") or u"") if infos else u""
        copy_mode = (strategy == "copy" and ok and close and infos
                     and first_codec == "aac")
        if copy_mode:
            list_path = os.path.join(workdir, "audio.txt")
            self._write_text(list_path, build_concat_list(job.audio_paths))
            args = ["-nostdin", "-hide_banner", "-loglevel", "error",
                    "-progress", "pipe:1", "-nostats",
                    "-f", "concat", "-safe", "0", "-i", list_path,
                    "-map", "0:a:0", "-c", "copy", "-f", "ipod", output]
            timeout = self._timeout(job.audio_duration, False)
        else:
            args = ["-nostdin", "-hide_banner", "-loglevel", "error"]
            for path in job.audio_paths:
                args.extend(["-i", path])
            args.extend([
                "-filter_complex",
                _audio_filter_graph(len(job.audio_paths)) +
                u";[aout]apad[aout2]",
                "-map", "[aout2]",
                "-t", u"%.3f" % max(0.1, duration),
                "-c:a", "aac", "-b:a", "%dk" % _audio_kbps(
                    settings.get("avmerge_quality")),
                "-progress", "pipe:1", "-nostats",
                "-f", "ipod", output])
            timeout = self._timeout(duration, True)
        code = self._run_ffmpeg(job, args, timeout, duration, weight=30.0)
        if code != 0:
            raise AVMergeError("MERGE_FAILED", u"拼接声音失败（FFmpeg 返回 %s）"
                             % code)
        self._step(job, "audio", "done")

    def _mux(self, job, video_part, audio_part, output, duration, container):
        self._check_cancelled(job)
        job.stage = "mux"
        self._step(job, "mux")
        muxer = "matroska" if container == "mkv" else "mp4"
        args = ["-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", video_part]
        if audio_part:
            args.extend(["-i", audio_part, "-map", "0:v:0",
                         "-map", "1:a:0", "-c", "copy"])
        else:
            args.extend(["-map", "0:v:0", "-c", "copy"])
        args.extend(["-t", u"%.3f" % max(0.1, duration),
                     "-progress", "pipe:1", "-nostats",
                     "-f", muxer, output])
        code = self._run_ffmpeg(job, args, self._timeout(duration, False),
                                duration, weight=10.0)
        if code != 0:
            raise AVMergeError("MERGE_FAILED", u"混流失败（FFmpeg 返回 %s）" % code)
        self._step(job, "mux", "done")

    def _verify(self, job, part, expected_duration, expect_audio):
        job.stage = "verify"
        self._step(job, "verify")
        if not os.path.isfile(part):
            raise AVMergeError("MERGE_FAILED", u"合成产物不存在")
        size = int(os.path.getsize(part))
        if size < config.AVMERGE_MIN_OUTPUT_BYTES:
            raise AVMergeError("MERGE_FAILED", u"合成产物为空或过小")
        info = self._probe(job, part)
        if not info.get("has_video"):
            raise AVMergeError("MERGE_FAILED", u"产物没有视频轨")
        if expect_audio and not info.get("has_audio"):
            raise AVMergeError("MERGE_FAILED", u"产物没有音频轨")
        actual = float(info.get("duration") or 0)
        tolerance = float(config.AVMERGE_VERIFY_TOLERANCE_SEC)
        duration_ok = True
        if expected_duration > 0 and actual > 0 \
                and abs(actual - expected_duration) > tolerance:
            duration_ok = False
            logger.warning(u"合成产物时长与预期不一致（%.1fs vs %.1fs）: %s",
                           actual, expected_duration, job.job_id)
        job.verify = {
            "size": size,
            "duration": actual,
            "expected_duration": expected_duration,
            "duration_ok": duration_ok,
            "video_codec": info.get("video_codec") or u"",
            "audio_codec": info.get("audio_codec") or u"",
            "has_video": bool(info.get("has_video")),
            "has_audio": bool(info.get("has_audio")),
        }
        job.duration = actual or expected_duration
        self._step(job, "verify", "done")

    # -- FFmpeg 执行 -----------------------------------------------------
    def _option_setting(self, key, default):
        return self._settings_snapshot().get(key) or default

    def _timeout(self, duration, transcode):
        factor = (config.AVMERGE_TIMEOUT_FACTOR_TRANSCODE if transcode
                  else config.AVMERGE_TIMEOUT_FACTOR_COPY)
        return max(float(config.AVMERGE_TIMEOUT_MIN_SEC),
                   float(duration or 0) * factor)

    def _run_ffmpeg(self, job, args, timeout, duration, weight):
        base = float(job.progress)
        span = weight

        def on_line(line):
            fields = parse_progress_line(line)
            if not fields:
                return
            out_time = fields.get("out_time")
            if out_time is not None and duration > 0:
                pct = min(100.0, float(out_time) / duration * 100.0)
                job.progress = min(99.0, base + span * pct / 100.0)
                self._touch(job)
            speed = fields.get("speed")
            if speed is not None and fields.get("progress") != "end":
                job.message = u"处理中 · %sx" % speed

        try:
            code, stderr = self._ffmpeg.run_stream(
                args, on_line, timeout=timeout,
                stop_check=lambda: job._cancel.is_set())
        except VideoStopped:
            raise _JobCancelled()
        except VideoTimeout:
            raise AVMergeError("MERGE_TIMEOUT", u"合成超时，已停止")
        except (VideoSkip, OSError) as exc:
            raise AVMergeError("FFMPEG_UNAVAILABLE",
                             u"无法执行 FFmpeg：%s" % (getattr(exc, "detail", "")
                                                      or exc))
        job.returncode = code
        if code != 0:
            logger.warning(u"FFmpeg 返回 %s：%s", code,
                           (stderr or b"")[-600:].decode("utf-8", "replace"))
        return code

    def _probe(self, job, path):
        if not self._ffmpeg.available():
            raise AVMergeError("FFMPEG_UNAVAILABLE", u"未找到 FFmpeg，无法合成")
        check = (lambda: job._cancel.is_set())
        try:
            _code, _stdout, stderr = self._ffmpeg.run(
                ["-nostdin", "-hide_banner", "-loglevel", "info",
                 "-i", path],
                timeout=config.AVMERGE_PROBE_TIMEOUT_SEC, stop_check=check)
        except VideoStopped:
            raise _JobCancelled()
        except VideoTimeout:
            raise AVMergeError("SOURCE_INVALID", u"探测素材超时")
        except VideoSkip as exc:
            raise AVMergeError("FFMPEG_UNAVAILABLE",
                             u"无法执行 FFmpeg：%s"
                             % (exc.detail or exc.reason))
        info = probe_media((stderr or b"").decode("utf-8", "replace"))
        if not info["has_video"] and not info["has_audio"]:
            raise AVMergeError("SOURCE_INVALID",
                             u"无法识别素材的音视频轨：%s" % os.path.basename(path))
        return info

    # -- 输出路径 --------------------------------------------------------
    def _resolve_output_dir(self, job, video_infos):
        settings = self._settings_snapshot()
        options = job.options
        mode = (options.get("output_mode")
                or settings.get("avmerge_output_mode")
                or config.AVMERGE_OUTPUT_MODE_DEFAULT)
        if mode not in config.AVMERGE_OUTPUT_MODES:
            mode = config.AVMERGE_OUTPUT_MODE_DEFAULT
        base = str(options.get("output_dir")
                   or settings.get("avmerge_output_dir") or u"").strip()
        if mode == "alongside":
            directory = os.path.dirname(job.video_paths[0])
        else:
            if not base:
                raise AVMergeError(
                    "OUTPUT_DIR_INVALID",
                    u"未设置输出目录：请选择目录，或改用「源文件同目录」")
            directory = base
            if mode == "subdir":
                sub = _clean_subdir(options.get("output_subdir")
                                    if options.get("output_subdir")
                                    is not None
                                    else settings.get("avmerge_output_subdir"))
                directory = os.path.join(base, sub)
        normalized = check_path_safety(directory)
        self._shares.validate_root(normalized)
        if not os.path.isdir(normalized):
            try:
                os.makedirs(normalized)
            except OSError as exc:
                raise AVMergeError("OUTPUT_DIR_INVALID",
                                 u"无法创建输出目录：%s" % exc)
        if not writable_dir(normalized):
            raise AVMergeError(
                "OUTPUT_DIR_INVALID",
                u"输出目录不可写：%s（请给 duplicatecleaner 写权限）"
                % normalized)
        return normalized

    def _resolve_target(self, job, output_dir, video_infos, container):
        settings = self._settings_snapshot()
        options = job.options
        template = str(options.get("name_template")
                       or settings.get("avmerge_name_template")
                       or config.AVMERGE_NAME_TEMPLATE_DEFAULT).strip()
        if not template:
            template = config.AVMERGE_NAME_TEMPLATE_DEFAULT
        stem = os.path.splitext(os.path.basename(job.video_paths[0]))[0]
        text = template.replace("{name}", stem).replace(
            "{date}", time.strftime("%Y%m%d"))
        text = _BAD_NAME_RE.sub("_", text).strip(" .") or (stem or u"merged")
        text = text[:_NAME_MAX]
        ext = ".mkv" if container == "mkv" else ".mp4"
        target = os.path.join(output_dir, text + ext)
        overwrite = (options.get("overwrite")
                     or settings.get("avmerge_overwrite")
                     or config.AVMERGE_OVERWRITE_DEFAULT)
        if overwrite not in config.AVMERGE_OVERWRITE_MODES:
            overwrite = config.AVMERGE_OVERWRITE_DEFAULT
        source_keys = set(path_key(p) for p in
                          job.video_paths + job.audio_paths)
        if path_key(target) in source_keys:
            return self._unique_path(output_dir, text, ext, source_keys), False
        if not os.path.exists(target):
            return target, False
        if overwrite == "overwrite":
            return target, True
        if overwrite == "skip":
            raise AVMergeError("OUTPUT_EXISTS",
                             u"同名文件已存在（跳过）：%s" % os.path.basename(target))
        return self._unique_path(output_dir, text, ext, source_keys), False

    @staticmethod
    def _unique_path(directory, stem, ext, source_keys):
        for index in range(2, 1000):
            candidate = os.path.join(directory,
                                     u"%s (%d)%s" % (stem, index, ext))
            if not os.path.exists(candidate) \
                    and path_key(candidate) not in source_keys:
                return candidate
        raise AVMergeError("OUTPUT_EXISTS", u"同名文件过多，无法自动命名")

    def _ensure_free_space(self, job):
        settings = self._settings_snapshot()
        try:
            need_gb = int(settings.get("avmerge_min_free_gb")
                          or config.AVMERGE_MIN_FREE_GB_DEFAULT)
        except (TypeError, ValueError):
            need_gb = config.AVMERGE_MIN_FREE_GB_DEFAULT
        if need_gb <= 0:
            return
        directory = self._resolve_output_dir(job, [])
        try:
            free = shutil.disk_usage(directory).free
        except OSError:
            return
        if free < need_gb * 1024 ** 3:
            raise AVMergeError(
                "INSUFFICIENT_SPACE",
                u"输出目录剩余空间不足：需要 %s GB，剩余 %.1f GB"
                % (need_gb, free / float(1024 ** 3)))

    def _delete_sources(self, job):
        deleted = 0
        for path in job.video_paths + job.audio_paths:
            try:
                if os.path.isfile(path) and not os.path.islink(path):
                    os.unlink(path)
                    deleted += 1
            except OSError:
                logger.warning(u"删除源文件失败: %s", path)
        job.message = u"合成完成，已删除 %d 个源文件" % deleted

    # -- 状态 / 持久化 ---------------------------------------------------
    def _step(self, job, key, status="running", detail=None):
        job.step(key, status, detail)
        job.message = u"%s…" % STAGE_LABELS.get(key, key) if status == "running" \
            else job.message
        self._touch(job, force=True)

    def _set_progress(self, job, value):
        job.progress = max(0.0, min(99.0, float(value)))
        self._touch(job)

    def _touch(self, job, force=False):
        job.updated_epoch = time.time()
        self._persist(job, force=force)

    def _persist(self, job, force=False):
        now = time.time()
        if not force and now - getattr(job, "_last_persist", 0) < \
                float(config.AVMERGE_PROGRESS_FLUSH_SEC):
            return
        job._last_persist = now
        try:
            self.store.save_avmerge_job(job.job_id, job.to_record())
        except Exception:
            logger.exception(u"合成任务持久化失败: %s", job.job_id)

    def _persist_dirty_locked(self):
        for job in list(self._jobs.values()):
            self._persist(job)

    def _finish(self, job, status, message):
        job.status = status
        job.message = message
        if status == STATUS_DONE:
            job.progress = 100.0
        job.finished_epoch = time.time()
        job.updated_epoch = job.finished_epoch
        try:
            self.store.save_avmerge_job(job.job_id, job.to_record())
        except Exception:
            logger.exception(u"合成任务持久化失败: %s", job.job_id)

    def _cleanup_part(self, job):
        path = getattr(job, "_part_path", u"")
        if path and os.path.isfile(path):
            try:
                os.unlink(path)
            except OSError:
                pass
        job._part_path = u""

    def _check_cancelled(self, job):
        if job.cancel_requested():
            raise _JobCancelled()

    def _write_text(self, path, text):
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def _prune_locked(self):
        limit = config.AVMERGE_JOB_LIMIT_DEFAULT
        try:
            limit = int(self._settings_snapshot().get("avmerge_job_limit")
                        or limit)
        except (TypeError, ValueError):
            pass
        with self._lock:
            finals = [job for job in self._jobs.values() if job.finished]
            finals.sort(key=lambda item: item.created_epoch or 0)
            while len(finals) > max(10, limit):
                job = finals.pop(0)
                self._jobs.pop(job.job_id, None)
                try:
                    self.store.delete_avmerge_job(job.job_id)
                except Exception:
                    logger.exception(u"合成任务清理失败: %s", job.job_id)
            try:
                count = self.store.count_avmerge_jobs(
                    statuses=FINAL_STATUSES)
            except Exception:
                count = 0
            if count > max(10, limit) * 2:
                for record in self.store.load_avmerge_jobs(limit=1000)[:]:
                    if record.get("status") in FINAL_STATUSES:
                        try:
                            self.store.delete_avmerge_job(record["job_id"])
                        except Exception:
                            pass

    # -- 对外：能力 / 提交 -------------------------------------------------
    def capabilities(self):
        ffmpeg = {"available": self._ffmpeg.available()}
        try:
            ffmpeg["version"] = self._ffmpeg.version() or u""
        except Exception:
            ffmpeg["version"] = u""
        settings = self._settings_snapshot()
        return {
            "ffmpeg": ffmpeg,
            "containers": list(config.AVMERGE_CONTAINERS),
            "modes": list(config.AVMERGE_MODES),
            "qualities": list(config.AVMERGE_QUALITIES),
            "max_resolutions": list(config.AVMERGE_MAX_RESOLUTIONS),
            "concurrency": self._concurrency(),
            "defaults": {
                "container": settings.get("avmerge_container")
                or config.AVMERGE_CONTAINER_DEFAULT,
                "mode": settings.get("avmerge_mode")
                or config.AVMERGE_MODE_DEFAULT,
                "quality": settings.get("avmerge_quality")
                or config.AVMERGE_QUALITY_DEFAULT,
                "output_mode": settings.get("avmerge_output_mode")
                or config.AVMERGE_OUTPUT_MODE_DEFAULT,
                "output_dir": settings.get("avmerge_output_dir") or u"",
                "name_template": settings.get("avmerge_name_template")
                or config.AVMERGE_NAME_TEMPLATE_DEFAULT,
            },
        }

    def create(self, user, payload):
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        video_paths = payload.get("video_paths")
        audio_paths = payload.get("audio_paths") or []
        if not isinstance(video_paths, list) or not video_paths:
            raise invalid(u"至少选择一个视频片段")
        if not isinstance(audio_paths, list):
            raise invalid(u"audio_paths 必须是数组")
        if len(video_paths) > config.AVMERGE_ENUMERATE_MAX_ITEMS:
            raise invalid(u"视频片段过多")
        video_clean = [self._validate_source(path) for path in video_paths]
        audio_clean = [self._validate_source(path) for path in audio_paths]
        options = payload.get("options") or {}
        if not isinstance(options, dict):
            raise invalid(u"options 必须是 JSON 对象")
        settings = self._settings_snapshot()
        container = options.get("container") or settings.get(
            "avmerge_container") or config.AVMERGE_CONTAINER_DEFAULT
        if container not in config.AVMERGE_CONTAINERS:
            raise invalid(u"未知容器格式")
        mode = options.get("mode") or settings.get(
            "avmerge_mode") or config.AVMERGE_MODE_DEFAULT
        if mode not in config.AVMERGE_MODES:
            raise invalid(u"未知合成模式")
        delete_source = bool(options.get("delete_source"))
        confirm = payload.get("confirm") or options.get("confirm")
        if delete_source and confirm != "DELETE SOURCE":
            raise invalid(u"删除源文件需要在 confirm 里传入 DELETE SOURCE")
        job_options = {
            "container": container,
            "mode": mode,
            "quality": options.get("quality") or settings.get(
                "avmerge_quality") or config.AVMERGE_QUALITY_DEFAULT,
            "overwrite": options.get("overwrite") or settings.get(
                "avmerge_overwrite") or config.AVMERGE_OVERWRITE_DEFAULT,
            "output_mode": options.get("output_mode") or settings.get(
                "avmerge_output_mode") or config.AVMERGE_OUTPUT_MODE_DEFAULT,
            "output_dir": options.get("output_dir") or settings.get(
                "avmerge_output_dir") or u"",
            "output_subdir": options.get("output_subdir"),
            "name_template": options.get("name_template"),
            "delete_source": delete_source,
            "delete_source_confirmed": delete_source and confirm == "DELETE SOURCE",
        }
        with self._lock:
            active = [job for job in self._jobs.values()
                      if job.status in ACTIVE_STATUSES]
            if len(active) >= max(10, int(settings.get("avmerge_job_limit")
                                          or config.AVMERGE_JOB_LIMIT_DEFAULT)):
                raise AVMergeError("JOB_LIMIT_REACHED",
                                 u"排队任务过多，请先等待或清理任务")
            job = AVMergeJob(make_job_id(), video_clean, audio_clean,
                           job_options, user)
            self._jobs[job.job_id] = job
            self._queue.append(job.job_id)
            self.store.save_avmerge_job(job.job_id, job.to_record())
        logger.info(u"合成任务已创建: %s（视频 %d / 音频 %d）", job.job_id,
                    len(video_clean), len(audio_clean))
        return job.snapshot(include_detail=True)

    def _validate_source(self, path):
        if not isinstance(path, str) or not path.strip():
            raise invalid(u"素材路径不能为空")
        normalized = check_path_safety(path.strip())
        if not is_regular_file(normalized):
            raise AVMergeError("SOURCE_INVALID",
                             u"素材不存在或不是普通文件：%s" % normalized)
        parent = os.path.dirname(normalized)
        share = self._shares.validate_root(parent)
        root = (share or {}).get("path") or u""
        if root and not is_under(normalized, root):
            raise AVMergeError("SOURCE_INVALID",
                               u"素材不在共享文件夹内：%s" % normalized)
        if root and has_symlink_component(normalized, root):
            raise AVMergeError("SOURCE_INVALID",
                               u"不允许合成符号链接路径：%s" % normalized)
        return normalized

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
        allowed = list(config.AVMERGE_VIDEO_EXTENSIONS) + \
            list(config.AVMERGE_AUDIO_EXTENSIONS)
        allowed_set = frozenset(allowed)
        limit = int(config.AVMERGE_ENUMERATE_MAX_ITEMS)
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
                    if recursive and is_safe_dir_name(name):
                        stack.append(entry.path)
                    continue
                ext = extension_of(name)
                if ext not in allowed_set:
                    skipped["extension"] += 1
                    continue
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    skipped["other"] += 1
                    continue
                kind = "video" if ext in config.AVMERGE_VIDEO_EXTENSIONS \
                    else "audio"
                items.append({"path": entry.path, "name": name,
                              "size": int(size), "kind": kind,
                              "size_label": _human_bytes(size)})
        return {"path": normalized, "items": items, "count": len(items),
                "limit": limit, "skipped": skipped}

    # -- 查询 / 控制 ------------------------------------------------------
    def counts(self):
        result = {status: 0 for status in STATUSES}
        with self._lock:
            for job in self._jobs.values():
                result[job.status] = result.get(job.status, 0) + 1
        return result

    def list_page(self, status=None, offset=0, limit=50, q=None):
        query = (q or u"").strip().lower()
        with self._lock:
            jobs = list(self._jobs.values())
        if status:
            wanted = set(part.strip() for part in str(status).split(",")
                         if part.strip())
            jobs = [job for job in jobs if job.status in wanted]
        if query:
            jobs = [job for job in jobs
                    if query in job.job_id.lower()
                    or any(query in path.lower()
                           for path in job.video_paths + job.audio_paths)]
        jobs.sort(key=lambda item: item.created_epoch or 0, reverse=True)
        total = len(jobs)
        offset = max(0, int(offset or 0))
        limit = max(1, int(limit or 50))
        return {"total": total, "items": [
            job.snapshot() for job in jobs[offset:offset + limit]]}

    def queue_snapshot(self, active_limit=20, queued_limit=50,
                       recent_limit=20):
        with self._lock:
            running = [job for job in self._jobs.values()
                       if job.status == STATUS_RUNNING]
            queued = [job for job in self._jobs.values()
                      if job.status == STATUS_QUEUED]
            finished = [job for job in self._jobs.values() if job.finished]
        running.sort(key=lambda item: item.started_epoch or 0)
        queued.sort(key=lambda item: item.created_epoch or 0)
        finished.sort(key=lambda item: item.finished_epoch or 0, reverse=True)
        return {
            "running": [job.snapshot() for job in running[:active_limit]],
            "queued": [job.snapshot() for job in queued[:queued_limit]],
            "recent": [job.snapshot() for job in finished[:recent_limit]],
            "counts": self.counts(),
            "concurrency": self._concurrency(),
        }

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise not_found(u"合成任务不存在")
        return job

    def get_view(self, job_id):
        return self.get(job_id).snapshot(include_detail=True)

    def export_rows(self, status=None):
        with self._lock:
            jobs = list(self._jobs.values())
        if status:
            jobs = [job for job in jobs if job.status == status]
        jobs.sort(key=lambda item: item.created_epoch or 0, reverse=True)
        rows = []
        for job in jobs:
            rows.append([
                job.job_id, iso_local(job.created_epoch), job.status,
                job.strategy, len(job.video_paths), len(job.audio_paths),
                u"%d" % int(job.duration or 0),
                job.output_name, job.error_code, job.error_message,
            ])
        return rows

    def cancel(self, job_id):
        job = self.get(job_id)
        if job.finished:
            raise AVMergeError("MERGE_BUSY", u"任务已结束，无法取消")
        job._cancel.set()
        if job.status == STATUS_QUEUED:
            with self._lock:
                if job_id in self._queue:
                    self._queue.remove(job_id)
            self._finish(job, STATUS_CANCELLED, u"已取消")
        else:
            job.message = u"正在取消…"
        return job.snapshot()

    def retry(self, job_id):
        job = self.get(job_id)
        if not job.finished:
            raise AVMergeError("MERGE_BUSY", u"任务尚未结束，无法重试")
        with self._lock:
            job.status = STATUS_QUEUED
            job.stage = STATUS_QUEUED
            job.progress = 0
            job.message = u"重新排队"
            job.error_code = u""
            job.error_message = u""
            job.steps = []
            job.verify = {}
            job._cancel.clear()
            job.created_epoch = time.time()
            job.started_epoch = None
            job.finished_epoch = None
            self._queue.append(job.job_id)
            self.store.save_avmerge_job(job.job_id, job.to_record())
        return job.snapshot()

    def delete(self, job_id, delete_output=False):
        job = self.get(job_id)
        if not job.finished:
            raise AVMergeError("MERGE_BUSY", u"任务尚未结束，无法删除")
        if delete_output and job.output_path:
            try:
                if os.path.isfile(job.output_path):
                    os.unlink(job.output_path)
            except OSError as exc:
                raise AVMergeError("MERGE_FAILED", u"删除产物失败：%s" % exc)
        with self._lock:
            self._jobs.pop(job_id, None)
            try:
                self.store.delete_avmerge_job(job_id)
            except Exception:
                logger.exception(u"合成任务删除失败: %s", job_id)
        return {"ok": True, "job_id": job_id}

    def bulk_action(self, user, action, scope="all"):
        with self._lock:
            jobs = list(self._jobs.values())
        targets = []
        for job in jobs:
            if scope == "finished" or scope == "all":
                if job.finished:
                    targets.append(job)
            elif scope == "queued" and job.status == STATUS_QUEUED:
                targets.append(job)
            elif scope == "failed" and job.status == STATUS_FAILED:
                targets.append(job)
        done = 0
        for job in targets:
            try:
                if action == "cancel_all" and not job.finished:
                    self.cancel(job.job_id)
                    done += 1
                elif action == "retry_failed" and job.status == STATUS_FAILED:
                    self.retry(job.job_id)
                    done += 1
                elif action == "clear_finished" and job.finished:
                    self.delete(job.job_id)
                    done += 1
            except AVMergeError:
                continue
        return {"ok": True, "action": action, "affected": done}
