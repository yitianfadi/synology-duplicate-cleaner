# -*- coding: utf-8 -*-
"""下载中心服务（v0051，docs/DOWNLOAD_CONTRACT.md）。

职责
----
- 任务队列：任务级并发（`download_concurrency`）+ FIFO；超出并发排队；
- 直链下载（HTTP Range 续传）与 HLS 下载（分片并发、断点续传、AES-128）；
- 合并：把本地化的播放列表交给 FFmpeg 做无损封装（`-c copy`）成 MP4；
- 落盘：临时目录在输出目录内部，最终产物 `os.replace` 原子改名（不跨卷搬运）；
- 面向 API 的查询/控制：列表、队列、详情、暂停、继续、取消、重试、删除。

明确不做
--------
- DRM（Widevine/FairPlay/PlayReady）与 SAMPLE-AES 解密；
- 直播/滚动播放列表（无 #EXT-X-ENDLIST）；
- 浏览器渲染与 JS 执行（网页解析在 v0052，且只做 HTML/JS 字面量提取）。

安全约定
--------
- 只允许 http/https；拒绝 URL 内嵌账号密码；
- 默认拒绝内网/回环地址（SSRF），需要时用 `download_allow_private` 显式放开；
- 输出目录必须是共享文件夹内的可写目录（`ShareService.share_root`）；
- Cookie/代理等敏感值只存在于内存与 settings（密钥键不进日志/审计）。
"""

from __future__ import absolute_import

import errno
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config, hls
from .errors import ApiError, invalid, not_found, path_not_allowed
from .fsutil import (check_path_safety, is_safe_dir_name, writable_dir)
from .video_similarity import (FFmpegTool, VideoSkip, VideoStopped,
                               VideoTimeout)

logger = logging.getLogger("dupcleaner.downloads")

# 运行状态
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_PAUSED,
            STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)
FINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_PAUSED)

# 阶段（详情页"步骤耗时"直接复用字幕那套渲染）
STAGE_LABELS = {
    "queued": u"排队中",
    "resolve": u"解析地址",
    "download": u"下载",
    "merge": u"合并",
    "verify": u"校验",
    "done": u"完成",
}

_READ_CHUNK = 256 * 1024
_MAX_URL_LEN = 8192
_MAX_PLAYLIST_BYTES = 4 * 1024 * 1024
_MAX_ERROR_CHARS = 400
_NAME_MAX = 120
_BAD_NAME_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")
_QUERY_RE = re.compile(r"\?[^\s'\"]*")


class DownloadError(Exception):
    """下载过程中的可读错误（code 与 API 错误码之一）。"""

    def __init__(self, code, message, details=None):
        super(DownloadError, self).__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class _JobCancelled(Exception):
    pass


class _JobPaused(Exception):
    pass


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def make_job_id():
    return "dl-" + uuid.uuid4().hex[:20]


def make_group_id():
    return "dg-" + uuid.uuid4().hex[:20]


def _iso(epoch):
    if not epoch:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(epoch)))
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_name(text, fallback=u"video", max_len=_NAME_MAX):
    """清理成可用的文件名/目录名（去掉路径分隔符与控制字符）。"""
    raw = (text or u"").strip()
    raw = _BAD_NAME_RE.sub(u"_", raw)
    raw = re.sub(r"\s+", u" ", raw).strip(u" .")
    if not raw:
        raw = fallback
    return raw[:max_len]


def _safe_part(text, fallback=u"other"):
    """子目录模板里的单个片段：不允许多余的分隔符与 ..。"""
    raw = _BAD_NAME_RE.sub(u"_", (text or u"").strip())
    raw = raw.strip(u" .")
    if raw in (u"", u".", u".."):
        return fallback
    return raw[:_NAME_MAX]


def _site_of(url):
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return _safe_part(host, u"other")


def _sanitize_error(text, cookie=""):
    """错误文本去掉查询串与 Cookie，避免把签名/令牌写进任务详情与日志。"""
    out = str(text or u"")
    if cookie:
        out = out.replace(cookie, u"***")
    out = _QUERY_RE.sub(u"?***", out)
    out = out.strip()
    if len(out) > _MAX_ERROR_CHARS:
        out = out[:_MAX_ERROR_CHARS] + u"…"
    return out


def _looks_private(ip_text):
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return True
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def assert_url_allowed(url, allow_private=False):
    """URL 白名单：协议/长度/凭据/内网地址。失败抛 DownloadError。"""
    text = (url or u"").strip()
    if not text:
        raise DownloadError("URL_INVALID", u"下载地址为空")
    if len(text) > _MAX_URL_LEN:
        raise DownloadError("URL_TOO_LONG", u"下载地址过长")
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        raise DownloadError("URL_INVALID", u"下载地址无法解析")
    if parsed.scheme.lower() not in config.DOWNLOAD_SCHEMES:
        raise DownloadError(
            "URL_UNSUPPORTED_SCHEME",
            u"只支持 http/https 地址，收到: %s" % (parsed.scheme or u"空"))
    if not parsed.hostname:
        raise DownloadError("URL_INVALID", u"下载地址缺少主机名")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise DownloadError("URL_INVALID",
                            u"下载地址不能包含账号密码（请用 Cookie 设置）")
    if allow_private:
        return text
    try:
        port = parsed.port
    except ValueError:
        raise DownloadError("URL_INVALID", u"下载地址端口非法")
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, 0,
                                   socket.SOCK_STREAM)
    except socket.gaierror:
        raise DownloadError("URL_INVALID",
                            u"无法解析主机名: %s" % parsed.hostname)
    resolved = [info[4][0] for info in infos if info and info[4]]
    if not resolved:
        raise DownloadError("URL_INVALID",
                            u"无法解析主机名: %s" % parsed.hostname)
    for ip_text in resolved:
        if _looks_private(ip_text):
            raise DownloadError(
                "URL_PRIVATE_ADDRESS",
                u"目标地址属于内网/回环地址（%s）。如确需下载局域网资源，"
                u"请在设置中开启「允许内网地址」" % ip_text)
    return text


def _free_bytes(path):
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return usage.free


def _human_bytes(value):
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return u"0 B"
    units = (u"B", u"KB", u"MB", u"GB", u"TB")
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return u"%d %s" % (int(value), units[index])
    return u"%.1f %s" % (value, units[index])


class _SpeedMeter(object):
    """最近 6 秒的滑动窗口速度（字节/秒）。"""

    def __init__(self, window=6.0):
        self.window = float(window)
        self._lock = threading.Lock()
        self._samples = []
        self._total = 0

    def add(self, nbytes):
        now = time.monotonic()
        with self._lock:
            self._samples.append((now, int(nbytes)))
            self._total += int(nbytes)
            self._trim(now)

    def _trim(self, now):
        limit = now - self.window
        while self._samples and self._samples[0][0] < limit:
            self._samples.pop(0)

    def bps(self):
        now = time.monotonic()
        with self._lock:
            self._trim(now)
            if not self._samples:
                return 0
            span = max(0.5, now - self._samples[0][0])
            total = sum(item[1] for item in self._samples)
        return int(total / span)

    @property
    def total(self):
        with self._lock:
            return self._total


class _RateLimiter(object):
    """简单令牌桶：全局限速按运行中任务均分（bps=0 表示不限）。"""

    def __init__(self, bps=0):
        self._bps = max(0, int(bps or 0))
        self._lock = threading.Lock()
        self._next = time.monotonic()

    def wait(self, nbytes):
        if not self._bps or nbytes <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + float(nbytes) / float(self._bps)
        if delay > 0:
            time.sleep(min(delay, 5.0))


class _HttpClient(object):
    """每个任务一个 HTTP 客户端：统一 UA/Referer/Cookie/代理/限速/超时。"""

    def __init__(self, timeout=30.0, user_agent="", referer="", cookie="",
                 proxy="", limiter=None, allow_private=False):
        self.timeout = float(timeout or 30.0)
        self.user_agent = user_agent or config.DOWNLOAD_DEFAULT_USER_AGENT
        self.referer = referer or ""
        self.cookie = cookie or ""
        self.proxy = proxy or ""
        self.limiter = limiter
        self.allow_private = bool(allow_private)
        self._opener = self._build_opener()

    def _build_opener(self):
        handlers = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler(
                {"http": self.proxy, "https": self.proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    def _headers(self, byte_range=None):
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if self.referer:
            headers["Referer"] = self.referer
            try:
                origin = urllib.parse.urlsplit(self.referer)
                headers["Origin"] = "%s://%s" % (origin.scheme, origin.netloc)
            except ValueError:
                pass
        if self.cookie:
            headers["Cookie"] = self.cookie
        if byte_range is not None:
            start, end = byte_range
            if end is None:
                headers["Range"] = "bytes=%d-" % int(start)
            else:
                headers["Range"] = "bytes=%d-%d" % (int(start), int(end))
        return headers

    def open(self, url, byte_range=None, timeout=None):
        assert_url_allowed(url, allow_private=self.allow_private)
        request = urllib.request.Request(
            url, headers=self._headers(byte_range), method="GET")
        limit = float(timeout or self.timeout)
        try:
            return self._opener.open(request, timeout=limit)
        except urllib.error.HTTPError as exc:
            try:
                exc.close()
            except Exception:
                pass
            raise DownloadError(
                "HTTP_STATUS", u"服务器返回 HTTP %s" % exc.code,
                {"status": int(exc.code), "url": _sanitize_error(url)})
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise DownloadError(
                "HTTP_STATUS", u"网络请求失败: %s" % reason,
                {"url": _sanitize_error(url)})
        except (socket.timeout, TimeoutError):
            raise DownloadError("HTTP_STATUS", u"请求超时")

    def read_text(self, url, limit=_MAX_PLAYLIST_BYTES):
        response = self.open(url)
        try:
            chunks = []
            total = 0
            while True:
                block = response.read(_READ_CHUNK)
                if not block:
                    break
                chunks.append(block)
                total += len(block)
                if total > limit:
                    raise DownloadError("PLAYLIST_INVALID",
                                        u"播放列表过大（超过 %s）"
                                        % _human_bytes(limit))
                if self.limiter:
                    self.limiter.wait(len(block))
            data = b"".join(chunks)
        finally:
            try:
                response.close()
            except Exception:
                pass
        return data.decode("utf-8", "replace"), response.headers, url


# ----------------------------------------------------------------------
# 任务
# ----------------------------------------------------------------------
class DownloadJob(object):
    def __init__(self, job_id, url, group_id=None, options=None, user="",
                 created_epoch=None):
        self.job_id = job_id
        self.group_id = group_id or None
        self.url = url or ""
        self.user = user or ""
        self.options = dict(options or {})
        self.title = self.options.get("title") or ""
        self.status = STATUS_QUEUED
        self.stage = STATUS_QUEUED
        self.message = u"排队中"
        self.progress = 0
        self.error_code = ""
        self.error_message = ""
        self.output_dir = ""
        self.output_name = ""
        self.output_path = ""
        self.output_size = 0
        self.kind = ""
        self.variant = ""
        self.encryption = ""
        self.segments_total = 0
        self.segments_done = 0
        self.bytes_total = 0
        self.bytes_done = 0
        self.speed_bps = 0
        self.verify = {}
        self.temp_dir = ""
        self.temp_kept = False
        self.attempts = 0
        self.created_epoch = float(created_epoch or time.time())
        self.started_epoch = 0.0
        self.finished_epoch = 0.0
        self.updated_epoch = self.created_epoch
        self.timeline = []
        # 运行期（不持久化）
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self._abort = threading.Event()
        self._limiter = None
        self._speed = _SpeedMeter()
        self._last_flush = 0.0
        self._active_start = 0.0

    # -- 状态 -----------------------------------------------------------
    @property
    def finished(self):
        return self.status in FINAL_STATUSES

    def cancel_requested(self):
        return self._cancel.is_set()

    def pause_requested(self):
        return self._pause.is_set()

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
        speed = self._speed.bps()
        if speed <= 0:
            return None
        if self.bytes_total and self.bytes_done < self.bytes_total:
            return int(max(0, self.bytes_total - self.bytes_done) / speed)
        if self.segments_total and self.segments_done < self.segments_total:
            per = (float(self.bytes_done or 0)
                   / max(1, self.segments_done))
            remaining = (self.segments_total - self.segments_done) * per
            return int(max(0.0, remaining) / speed)
        return None

    # -- 序列化 ---------------------------------------------------------
    def snapshot(self, include_detail=False):
        payload = {
            "job_id": self.job_id,
            "group_id": self.group_id,
            "url": self.url,
            "title": self.title or self.output_name or self.url,
            "kind": self.kind or "auto",
            "status": self.status,
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "message": self.message,
            "progress": int(self.progress or 0),
            "variant": self.variant,
            "encryption": self.encryption,
            "segments_total": self.segments_total,
            "segments_done": self.segments_done,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "bytes_total_text": _human_bytes(self.bytes_total),
            "bytes_done_text": _human_bytes(self.bytes_done),
            "speed_bps": self.speed_bps or self._speed.bps(),
            "speed_text": _human_bytes(self.speed_bps or self._speed.bps())
            + u"/s",
            "eta_seconds": self.eta_seconds(),
            "duration_sec": self.duration_sec(),
            "output_dir": self.output_dir,
            "output_name": self.output_name,
            "output_path": self.output_path,
            "output_size": self.output_size,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "attempts": self.attempts,
            "verify": dict(self.verify or {}),
            "temp_kept": bool(self.temp_kept),
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
            payload["options"] = _public_options(self.options)
        return payload

    def to_record(self):
        return {
            "job_id": self.job_id,
            "group_id": self.group_id,
            "url": self.url,
            "user": self.user,
            "options": self.options,
            "title": self.title,
            "status": self.status,
            "stage": self.stage,
            "message": self.message,
            "progress": self.progress,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "output_dir": self.output_dir,
            "output_name": self.output_name,
            "output_path": self.output_path,
            "output_size": self.output_size,
            "kind": self.kind,
            "variant": self.variant,
            "encryption": self.encryption,
            "segments_total": self.segments_total,
            "segments_done": self.segments_done,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "verify": self.verify,
            "temp_dir": self.temp_dir,
            "temp_kept": self.temp_kept,
            "attempts": self.attempts,
            "created_epoch": self.created_epoch,
            "started_epoch": self.started_epoch,
            "finished_epoch": self.finished_epoch,
            "updated_epoch": time.time(),
            "timeline": self.timeline,
        }

    @classmethod
    def from_record(cls, record):
        job = cls(record.get("job_id") or make_job_id(),
                  record.get("url") or "",
                  group_id=record.get("group_id"),
                  options=record.get("options") or {},
                  user=record.get("user") or "",
                  created_epoch=record.get("created_epoch"))
        for field in ("title", "status", "stage", "message", "progress",
                      "error_code", "error_message", "output_dir",
                      "output_name", "output_path", "output_size", "kind",
                      "variant", "encryption", "segments_total",
                      "segments_done", "bytes_total", "bytes_done",
                      "verify", "temp_dir", "temp_kept", "attempts",
                      "started_epoch", "finished_epoch", "updated_epoch",
                      "timeline"):
            if field in record:
                setattr(job, field, record[field])
        job.progress = int(job.progress or 0)
        job.segments_total = int(job.segments_total or 0)
        job.segments_done = int(job.segments_done or 0)
        job.bytes_total = int(job.bytes_total or 0)
        job.bytes_done = int(job.bytes_done or 0)
        if not isinstance(job.timeline, list):
            job.timeline = []
        if not isinstance(job.verify, dict):
            job.verify = {}
        return job


def _public_options(options):
    """对外暴露的任务选项：Cookie 只给"已设置"标记，绝不回传明文。"""
    out = {}
    for key, value in (options or {}).items():
        if key == "cookie":
            out["cookie_set"] = bool(value)
            continue
        out[key] = value
    return out


# ----------------------------------------------------------------------
# 服务
# ----------------------------------------------------------------------
class DownloadService(object):
    def __init__(self, runtime_config, store, share_service, settings_provider,
                 ffmpeg_tool=None, on_complete=None):
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
        self._on_complete = on_complete
        self._load_jobs()

    # -- 生命周期 -------------------------------------------------------
    def start(self):
        if self._dispatcher is not None:
            return
        self._stop.clear()
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="dl-dispatch", daemon=True)
        self._dispatcher.start()
        logger.info(u"下载中心调度已启动（并发上限 %s）", self._concurrency())

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
            remaining = max(0.1, deadline - time.time())
            thread.join(remaining)
        self._dispatcher = None

    def _load_jobs(self):
        try:
            records = self.store.load_download_jobs(limit=1000)
        except Exception:
            logger.exception(u"下载任务加载失败")
            records = []
        for record in records:
            if record.get("status") == STATUS_RUNNING:
                # 进程重启：运行中任务回到队列（临时目录仍在，可分片续传）。
                record["status"] = STATUS_QUEUED
                record["message"] = u"服务重启，重新排队"
            try:
                job = DownloadJob.from_record(record)
            except Exception:
                logger.exception(u"下载任务记录损坏，已跳过")
                continue
            self._jobs[job.job_id] = job
            if job.status == STATUS_QUEUED:
                self._queue.append(job.job_id)

    def apply_settings(self, settings=None):
        """设置保存后调用：并发上限变化由调度循环自然生效。"""
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
                "download_concurrency") or 0)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            value = config.DOWNLOAD_DEFAULT_CONCURRENCY
        return max(1, min(value, config.DOWNLOAD_CONCURRENCY_MAX))

    # -- 调度 -----------------------------------------------------------
    def _dispatch_loop(self):
        while not self._stop.wait(0.3):
            try:
                self._dispatch_once()
            except Exception:
                logger.exception(u"下载调度循环异常")

    def _dispatch_once(self):
        limit = self._concurrency()
        with self._lock:
            while len(self._running) < limit and self._queue:
                job_id = self._queue.pop(0)
                job = self._jobs.get(job_id)
                if job is None or job.status != STATUS_QUEUED:
                    continue
                job.status = STATUS_RUNNING
                job.started_epoch = job.started_epoch or time.time()
                job.progress = max(int(job.progress or 0), 1)
                thread = threading.Thread(
                    target=self._run_job, args=(job,),
                    name="dl-job-%s" % job_id[-6:], daemon=True)
                self._running[job_id] = thread
                thread.start()

    def _run_job(self, job):
        try:
            self._execute(job)
        except _JobPaused:
            self._finish_status(job, STATUS_PAUSED, u"已暂停（分片边界）")
        except _JobCancelled:
            self._cleanup_temp(job)
            self._finish_status(job, STATUS_CANCELLED, u"已取消")
        except DownloadError as exc:
            self._fail(job, exc.code, exc.message)
        except ApiError as exc:
            self._fail(job, exc.code, exc.message)
        except Exception:
            logger.exception(u"下载任务异常: %s", job.job_id)
            self._fail(job, "INTERNAL_ERROR", u"下载失败（内部错误）")
        finally:
            with self._lock:
                self._running.pop(job.job_id, None)
            self._persist(job, force=True)

    def _execute(self, job):
        options = job.options
        job.attempts = int(job.attempts or 0) + 1
        job._cancel.clear()
        job._abort.clear()
        speed_bps = int(options.get("rate_limit_kbps") or 0) * 1024
        job._limiter = _RateLimiter(speed_bps) if speed_bps > 0 else None

        job.step_start("resolve")
        self._touch(job, force=True)
        output_dir = self._prepare_output_dir(job)
        job.output_dir = output_dir
        client = self._client(job)

        kind, body, playlist, final_url = self._probe_source(job, client)
        job.kind = kind
        job.url = final_url or job.url
        self._check_cancelled(job)

        if kind == "hls":
            job.step_end("resolve")
            self._step_start(job, "download")
            local_list, audio_list = self._materialize_hls(
                job, client, playlist)
            self._step_end(job, "download")
            self._merge(job, local_list, audio_list)
            self._verify(job)
        else:
            job.step_end("resolve")
            self._step_start(job, "download")
            self._download_direct(job, client)
            self._step_end(job, "download")
            self._finalize_direct(job)

        self._complete(job)

    # -- 输出目录 -------------------------------------------------------
    def _prepare_output_dir(self, job):
        options = job.options
        raw = (options.get("output_dir")
               or self._settings_snapshot().get("download_output_dir")
               or u"")
        if not raw:
            raise DownloadError("OUTPUT_DIR_INVALID",
                                u"请先选择下载目录")
        try:
            path = check_path_safety(raw, reject_hidden=False)
        except ApiError as exc:
            raise DownloadError("OUTPUT_DIR_INVALID", exc.message)
        try:
            self._shares.share_root(path)
        except ApiError:
            raise DownloadError(
                "OUTPUT_DIR_INVALID",
                u"下载目录必须位于共享文件夹内: %s" % path)
        try:
            if not os.path.isdir(path):
                os.makedirs(path)
        except OSError as exc:
            raise DownloadError("OUTPUT_DIR_INVALID",
                                u"无法创建下载目录: %s" % exc)
        if not writable_dir(path):
            raise DownloadError(
                "OUTPUT_DIR_INVALID",
                u"下载目录不可写；请给系统内部用户 duplicatecleaner 授予写权限: %s"
                % path)
        target = self._apply_subdir(job, path)
        try:
            if not os.path.isdir(target):
                os.makedirs(target)
        except OSError as exc:
            raise DownloadError("OUTPUT_DIR_INVALID",
                                u"无法创建子目录: %s" % exc)
        minimum = int(options.get("min_free_gb") or 0)
        if minimum > 0:
            free = _free_bytes(target)
            if free is not None and free < minimum * 1024 ** 3:
                raise DownloadError(
                    "INSUFFICIENT_SPACE",
                    u"下载目录剩余空间不足（需要 %d GB，剩余 %s）"
                    % (minimum, _human_bytes(free)))
        return target

    def _apply_subdir(self, job, base):
        options = job.options
        mode = (options.get("subdir_mode") or "none").strip()
        if mode == "none":
            return base
        when = time.localtime(job.created_epoch or time.time())
        tokens = {
            "site": _site_of(job.url),
            "date": time.strftime("%Y-%m-%d", when),
            "yyyy": time.strftime("%Y", when),
            "mm": time.strftime("%m", when),
            "dd": time.strftime("%d", when),
            "hour": time.strftime("%H", when),
            "min": time.strftime("%M", when),
            "title": _safe_name(job.title or u"video", u"video"),
        }
        if mode == "site":
            template = u"{site}"
        elif mode == "date":
            template = u"{date}"
        elif mode == "site_date":
            template = u"{site}/{date}"
        else:
            template = (options.get("subdir_template")
                        or config.DOWNLOAD_SUBDIR_TEMPLATE_DEFAULT)
        try:
            rendered = template.format(**tokens)
        except (KeyError, IndexError, ValueError):
            rendered = config.DOWNLOAD_SUBDIR_TEMPLATE_DEFAULT.format(**tokens)
        parts = []
        for part in rendered.replace("\\", "/").split("/"):
            if part in (u"", u".", u".."):
                continue
            clean = _safe_part(part)
            if clean:
                parts.append(clean)
        if not parts:
            return base
        target = base
        for part in parts:
            target = os.path.join(target, part)
        if not is_safe_dir_name(os.path.basename(target) or u""):
            raise DownloadError("OUTPUT_DIR_INVALID",
                                u"子目录名非法: %s" % target)
        return target

    # -- HTTP 客户端 ----------------------------------------------------
    def _client(self, job):
        options = job.options
        return _HttpClient(
            timeout=float(options.get("timeout")
                          or config.DOWNLOAD_DEFAULT_TIMEOUT_SEC),
            user_agent=options.get("user_agent")
            or config.DOWNLOAD_DEFAULT_USER_AGENT,
            referer=options.get("referer") or u"",
            cookie=options.get("cookie") or u"",
            proxy=options.get("proxy") or u"",
            limiter=job._limiter,
            allow_private=bool(options.get("allow_private")))

    # -- 探测 -----------------------------------------------------------
    def _probe_source(self, job, client):
        """先取 64KB 判断是播放列表还是直链；返回 (kind, body, playlist, url)。"""
        response = client.open(job.url, byte_range=(0, 65535))
        try:
            head = response.read(65536)
            content_type = (response.headers.get("Content-Type") or "").lower()
            disposition = (response.headers.get(
                "Content-Disposition") or u"")
            final_url = response.geturl() or job.url
            total = response.headers.get("Content-Range") or ""
            if not head:
                raise DownloadError("HTTP_STATUS", u"服务器返回空内容")
            if hls.is_playlist(head):
                body = head.decode("utf-8", "replace")
                if len(head) >= 65536:
                    body, _headers, final_url = client.read_text(final_url)
                playlist = self._parse_playlist(job, client, body, final_url)
                return "hls", u"", playlist, final_url
            # 直链：记录体积，之后单独开流下载（可续传）
            job.bytes_total = self._parse_total(total, response.headers)
            job.title = job.title or self._title_from(
                disposition, final_url)
            return "direct", u"", None, final_url
        finally:
            try:
                response.close()
            except Exception:
                pass

    @staticmethod
    def _parse_total(content_range, headers):
        text = content_range or u""
        match = re.search(r"/(\d+)\s*$", text)
        if match:
            return int(match.group(1))
        length = headers.get("Content-Length")
        try:
            return int(length) if length else 0
        except (TypeError, ValueError):
            return 0

    def _title_from(self, disposition, url):
        if disposition:
            match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?',
                              disposition, re.IGNORECASE)
            if match:
                name = urllib.parse.unquote(match.group(1)).strip()
                if name:
                    return _safe_name(os.path.splitext(name)[0])
        path = urllib.parse.urlsplit(url).path or u""
        base = os.path.basename(path)
        if base:
            stem = os.path.splitext(base)[0]
            if stem and not stem.isdigit():
                return _safe_name(stem)
        return u""

    def _parse_playlist(self, job, client, body, url):
        try:
            playlist = hls.parse(body, url)
        except hls.HlsError as exc:
            raise DownloadError(exc.code, exc.message)
        if playlist.get("kind") == "master":
            variant = hls.pick_variant(
                playlist, int(job.options.get("max_height") or 0))
            job.variant = hls.variant_label(variant)
            job.title = job.title or self._title_from(
                u"", variant.get("uri") or url)
            text, _headers, media_url = client.read_text(variant["uri"])
            try:
                playlist = hls.parse(text, media_url)
            except hls.HlsError as exc:
                raise DownloadError(exc.code, exc.message)
            audio = hls.pick_audio_group(playlist, variant)
            playlist["_audio"] = audio
        job.encryption = hls.encryption_label(playlist)
        job.segments_total = hls.segment_count(playlist)
        return playlist

    # -- 解包/下载 ------------------------------------------------------
    def _materialize_hls(self, job, client, playlist):
        """把视频（含可选独立音轨）的播放列表落成本地化 m3u8。"""
        job.temp_dir = os.path.join(
            job.output_dir, config.DOWNLOAD_TEMP_PREFIX + job.job_id)
        try:
            os.makedirs(job.temp_dir)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise DownloadError("OUTPUT_DIR_INVALID",
                                    u"无法创建临时目录: %s" % exc)
        video_list = self._materialize_one(job, client, playlist, u"")
        try:
            expected = float(playlist.get("duration") or 0.0)
        except (TypeError, ValueError):
            expected = 0.0
        if expected > 0:
            job.options["expected_duration"] = expected
        audio = playlist.get("_audio") or None
        audio_list = u""
        if audio and audio.get("uri"):
            text, _headers, media_url = client.read_text(audio["uri"])
            try:
                audio_playlist = hls.parse(text, media_url)
            except hls.HlsError as exc:
                raise DownloadError(exc.code, exc.message)
            if audio_playlist.get("kind") == "media":
                audio_list = self._materialize_one(
                    job, client, audio_playlist, u"audio")
        return video_list, audio_list

    def _materialize_one(self, job, client, playlist, subdir):
        try:
            plan = hls.plan_local_files(playlist)
        except hls.HlsError as exc:
            raise DownloadError(exc.code, exc.message)
        base = job.temp_dir
        if subdir:
            base = os.path.join(job.temp_dir, subdir)
        try:
            if not os.path.isdir(base):
                os.makedirs(base)
        except OSError as exc:
            raise DownloadError("OUTPUT_DIR_INVALID",
                                u"无法创建临时目录: %s" % exc)
        fetches = plan["fetches"]
        segments = [item for item in fetches if item.get("kind") == "segment"]
        with self._lock:
            job.segments_total = max(int(job.segments_total or 0),
                                     int(job.segments_done or 0)
                                     + len(segments))
        self._fetch_all(job, client, fetches, base)
        local_path = os.path.join(base, "local.m3u8")
        text = hls.build_local_playlist(playlist, plan)
        try:
            with open(local_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except OSError as exc:
            raise DownloadError("OUTPUT_DIR_INVALID",
                                u"无法写本地播放列表: %s" % exc)
        return local_path

    def _fetch_all(self, job, client, fetches, base):
        todo = []
        for item in fetches:
            target = os.path.join(base, item["name"])
            if os.path.isfile(target) and os.path.getsize(target) > 0:
                if item.get("kind") == "segment":
                    self._segment_done(job, os.path.getsize(target))
                continue
            todo.append(item)
        if not todo:
            return
        concurrency = self._segment_concurrency(job)
        pool = ThreadPoolExecutor(max_workers=concurrency)
        futures = []
        try:
            futures = [pool.submit(self._fetch_one, job, client, item, base)
                       for item in todo]
            for future in as_completed(futures):
                future.result()
        except Exception:
            job._abort.set()
            for future in futures:
                future.cancel()
            raise
        finally:
            pool.shutdown(wait=True)

    def _segment_concurrency(self, job):
        try:
            value = int(job.options.get("segment_concurrency") or 0)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            value = config.DOWNLOAD_DEFAULT_SEGMENT_CONCURRENCY
        return max(1, min(value, config.DOWNLOAD_SEGMENT_CONCURRENCY_MAX))

    def _fetch_one(self, job, client, item, base):
        self._check_cancelled(job)
        target = os.path.join(base, item["name"])
        retry = int(job.options.get("retry") or 0)
        last_error = None
        for attempt in range(retry + 1):
            self._check_cancelled(job)
            if job._abort.is_set():
                raise DownloadError("SEGMENT_FAILED", u"下载已中止")
            try:
                self._download_to(job, client, item, target)
                if item.get("kind") == "segment":
                    self._segment_done(
                        job, os.path.getsize(target)
                        if os.path.isfile(target) else 0)
                return
            except DownloadError as exc:
                last_error = exc
                status = int((exc.details or {}).get("status") or 0)
                if 400 <= status < 500 and status not in (408, 429):
                    raise  # 4xx（除超时/限流）重试无意义
                if attempt >= retry:
                    raise
                time.sleep(min(config.DOWNLOAD_RETRY_BACKOFF_MAX_SEC,
                               config.DOWNLOAD_RETRY_BACKOFF_SEC
                               * (2 ** attempt)))
        if last_error:
            raise last_error

    def _download_to(self, job, client, item, target):
        byte_range = item.get("byte_range")
        start = 0
        end = None
        if byte_range:
            start, end = int(byte_range[0]), int(byte_range[1]) - 1
        elif os.path.isfile(target) and os.path.getsize(target) > 0:
            start = os.path.getsize(target)
        response = client.open(item["uri"],
                              byte_range=((start, end)
                                          if (byte_range or start)
                                          else None))
        part = target + config.DOWNLOAD_PART_EXT
        mode = "wb"
        expected = end - start + 1 if byte_range else None
        try:
            status = getattr(response, "status", 200)
            if start and status == 200 and not byte_range:
                # 服务器不支持 Range：只能从头重下
                start = 0
                mode = "wb"
            elif start and status != 206 and not byte_range:
                start = 0
            elif byte_range and status == 200:
                raise DownloadError(
                    "SEGMENT_FAILED",
                    u"服务器不支持分片字节范围请求（返回 200）")
            written = start if mode == "ab" else 0
            with open(part, mode) as handle:
                while True:
                    self._check_cancelled(job)
                    if job._abort.is_set():
                        raise _JobCancelled()
                    block = response.read(_READ_CHUNK)
                    if not block:
                        break
                    handle.write(block)
                    written += len(block)
                    self._add_bytes(job, len(block))
                    if job._limiter:
                        job._limiter.wait(len(block))
        finally:
            try:
                response.close()
            except Exception:
                pass
        size = os.path.getsize(part) if os.path.isfile(part) else 0
        if expected is not None and size != expected:
            try:
                os.remove(part)
            except OSError:
                pass
            raise DownloadError(
                "SEGMENT_FAILED",
                u"分片大小不符：期望 %d 字节，实际 %d 字节" % (expected, size))
        if size <= 0:
            raise DownloadError("SEGMENT_FAILED", u"分片下载为空")
        try:
            os.replace(part, target)
        except OSError as exc:
            raise DownloadError("SEGMENT_FAILED", u"写入分片失败: %s" % exc)

    def _download_direct(self, job, client):
        ext = self._extension_for(job)
        final_path = self._final_path(job, ext)
        part = final_path + config.DOWNLOAD_PART_EXT
        start = os.path.getsize(part) if os.path.isfile(part) else 0
        response = client.open(job.url, byte_range=(start, None)
                              if start else None)
        try:
            status = getattr(response, "status", 200)
            if start and status != 206:
                start = 0
            total = self._parse_total(
                response.headers.get("Content-Range"), response.headers)
            if total:
                job.bytes_total = total
            mode = "ab" if start else "wb"
            with open(part, mode) as handle:
                while True:
                    self._check_cancelled(job)
                    block = response.read(_READ_CHUNK)
                    if not block:
                        break
                    handle.write(block)
                    self._add_bytes(job, len(block))
                    if job._limiter:
                        job._limiter.wait(len(block))
        finally:
            try:
                response.close()
            except Exception:
                pass
        size = os.path.getsize(part) if os.path.isfile(part) else 0
        if size < config.DOWNLOAD_MIN_FILE_BYTES:
            raise DownloadError(
                "SEGMENT_FAILED",
                u"下载内容过小（%d 字节），可能不是可下载的视频" % size)
        job.output_name = os.path.basename(final_path)
        job.output_size = size
        job._part_path = part
        job._final_path_value = final_path
        self._touch(job, force=True)

    def _extension_for(self, job):
        path = urllib.parse.urlsplit(job.url).path or u""
        ext = os.path.splitext(path)[1].lower()
        if ext and re.match(r"^\.[a-z0-9]{2,5}$", ext):
            return ext
        return u".bin"

    def _finalize_direct(self, job):
        final_path = getattr(job, "_final_path_value", u"")
        part = getattr(job, "_part_path", u"")
        if not final_path or not part:
            raise DownloadError("INTERNAL_ERROR", u"下载收尾失败")
        try:
            os.replace(part, final_path)
        except OSError as exc:
            raise DownloadError("OUTPUT_DIR_INVALID",
                                u"重命名失败: %s" % exc)
        job.output_path = final_path
        job.progress = 100

    # -- 合并 -----------------------------------------------------------
    def _merge(self, job, video_list, audio_list=""):
        self._step_start(job, "merge")
        tool = self._ffmpeg
        if not tool.available():
            raise DownloadError("FFMPEG_UNAVAILABLE",
                                u"未找到 FFmpeg，无法合并视频")
        final_path = self._final_path(job, config.DOWNLOAD_OUTPUT_EXT)
        part = final_path + config.DOWNLOAD_PART_EXT
        timeout = float(job.options.get("merge_timeout") or 3600)
        attempts = []
        base = ["-nostdin", "-hide_banner", "-loglevel", "error",
                "-allowed_extensions", "ALL", "-i", video_list]
        if audio_list:
            base += ["-i", audio_list]
        map_args = []
        if audio_list:
            map_args = ["-map", "0:v:0", "-map", "1:a:0?"]
        attempts.append(base + map_args + [
            "-c", "copy", "-movflags", "+faststart", "-y", part])
        attempts.append(base + map_args + [
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-y", part])
        last_error = u""
        for index, args in enumerate(attempts):
            try:
                code, _stdout, stderr = tool.run(
                    args, timeout=timeout,
                    stop_check=lambda: job._cancel.is_set())
            except VideoStopped:
                raise _JobCancelled()
            except VideoTimeout:
                raise DownloadError(
                    "MERGE_FAILED",
                    u"合并超时（超过 %d 秒）" % int(timeout))
            except VideoSkip as exc:
                raise DownloadError(
                    "MERGE_FAILED",
                    u"无法执行 FFmpeg：%s" % (exc.detail or exc.reason))
            if code == 0 and os.path.isfile(part) \
                    and os.path.getsize(part) > 0:
                break
            last_error = _sanitize_error(
                (stderr or b"").decode("utf-8", "replace"),
                job.options.get("cookie") or u"")
            logger.warning(u"合并失败（第 %d 次）: %s", index + 1, last_error)
            if job._cancel.is_set():
                raise _JobCancelled()
        else:
            raise DownloadError("MERGE_FAILED",
                                u"合并失败: %s" % (last_error or u"FFmpeg 返回错误"))
        self._check_cancelled(job)
        size = os.path.getsize(part)
        if size <= 0:
            raise DownloadError("MERGE_FAILED", u"合并产物为空")
        job.step_end("merge")
        try:
            os.replace(part, final_path)
        except OSError as exc:
            raise DownloadError("OUTPUT_DIR_INVALID",
                                u"重命名失败: %s" % exc)
        job.output_name = os.path.basename(final_path)
        job.output_path = final_path
        job.output_size = size
        job.progress = max(int(job.progress or 0), 95)

    # -- 校验 -----------------------------------------------------------
    def _verify(self, job):
        self._step_start(job, "verify")
        expected = 0.0
        try:
            expected = float(
                (job.options.get("expected_duration") or 0) or 0)
        except (TypeError, ValueError):
            expected = 0.0
        info = {}
        try:
            tool = self._ffmpeg
            if tool.available():
                info = tool.probe_media(job.output_path, 120) or {}
        except Exception:
            logger.debug(u"下载产物探测失败: %s", job.job_id)
            info = {}
        duration = info.get("duration")
        tolerance = float(job.options.get("verify_tolerance") or 0)
        ok = True
        detail = u""
        if duration and expected and tolerance > 0:
            delta = abs(float(duration) - expected)
            ok = delta <= tolerance
            detail = u"时长 %.1fs（期望 %.1fs，偏差 %.1fs）" % (
                float(duration), expected, delta)
        elif duration:
            detail = u"时长 %.1fs" % float(duration)
        else:
            detail = u"未取得时长（不影响下载完成）"
        job.verify = {
            "duration": duration,
            "expected": expected or None,
            "tolerance": tolerance or None,
            "ok": bool(ok),
            "detail": detail,
        }
        job.step_end("verify", "warning" if not ok else "done", detail)

    # -- 收尾 -----------------------------------------------------------
    def _complete(self, job):
        job.progress = 100
        job.stage = "done"
        if not job.options.get("keep_temp"):
            self._cleanup_temp(job)
        else:
            job.temp_kept = bool(job.temp_dir and os.path.isdir(job.temp_dir))
        self._finish_status(job, STATUS_DONE,
                            u"已完成：%s（%s）" % (
                                job.output_name or u"",
                                _human_bytes(job.output_size)))
        if self._on_complete is not None:
            try:
                self._on_complete(job)
            except Exception:
                logger.exception(u"下载完成回调失败: %s", job.job_id)

    def _finish_status(self, job, status, message):
        now = time.time()
        with self._lock:
            job.status = status
            job.message = message or job.message
            job.progress = 100 if status == STATUS_DONE else int(
                job.progress or 0)
            job.speed_bps = 0
            if status in FINAL_STATUSES:
                job.finished_epoch = job.finished_epoch or now
            job.updated_epoch = now
        if status == STATUS_DONE:
            logger.info(u"下载完成 %s → %s", job.job_id, job.output_path)
        elif status == STATUS_FAILED:
            logger.warning(u"下载失败 %s: %s %s", job.job_id,
                           job.error_code, job.error_message)
        self._persist(job, force=True)

    def _fail(self, job, code, message):
        job.error_code = code or "INTERNAL_ERROR"
        job.error_message = _sanitize_error(
            message, job.options.get("cookie") or u"")
        job.step_end(job.stage if job.stage in STAGE_LABELS else "download",
                     "failed", job.error_message)
        # 失败保留临时目录（便于重试续传），显式标记避免误以为已清理。
        job.temp_kept = bool(job.temp_dir and os.path.isdir(job.temp_dir))
        self._finish_status(
            job, STATUS_FAILED,
            u"失败：%s" % (job.error_message or job.error_code))

    def _cleanup_temp(self, job):
        temp_dir = job.temp_dir or u""
        if not temp_dir or not os.path.isdir(temp_dir):
            return
        base = os.path.basename(temp_dir.rstrip(os.sep))
        if not base.startswith(config.DOWNLOAD_TEMP_PREFIX):
            logger.warning(u"拒绝清理非下载临时目录: %s", temp_dir)
            return
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            logger.exception(u"清理临时目录失败: %s", temp_dir)
        job.temp_kept = False

    def _check_cancelled(self, job):
        if job._cancel.is_set():
            raise _JobCancelled()
        if job.pause_requested():
            raise _JobPaused()

    def _add_bytes(self, job, nbytes):
        with self._lock:
            job.bytes_done = int(job.bytes_done or 0) + int(nbytes)
            job._speed.add(nbytes)
            job.speed_bps = job._speed.bps()
            total = job.bytes_total or 0
            if total > 0:
                ratio = float(job.bytes_done) / float(total)
                job.progress = min(90, int(10 + ratio * 80))
            elif job.segments_total:
                ratio = float(job.segments_done) / float(job.segments_total)
                job.progress = min(90, int(10 + ratio * 80))
        self._touch(job)

    def _segment_done(self, job, size):
        with self._lock:
            job.segments_done = int(job.segments_done or 0) + 1
            if job.segments_total:
                ratio = float(job.segments_done) / float(job.segments_total)
                job.progress = min(90, max(int(job.progress or 0),
                                           int(10 + ratio * 80)))
        self._touch(job)

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
                if now - float(job._last_flush or 0) < \
                        config.DOWNLOAD_PROGRESS_FLUSH_SEC:
                    return
        with self._lock:
            job._last_flush = now
            job.updated_epoch = now
            record = job.to_record()
        try:
            self.store.save_download_job(job.job_id, record)
        except Exception:
            logger.exception(u"下载任务持久化失败: %s", job.job_id)

    def _touch(self, job, force=False):
        self._persist(job, force=force)

    # -- 对外：创建 -----------------------------------------------------
    def create(self, user, payload):
        settings = self._settings_snapshot()
        if not settings.get("download_enabled"):
            raise ApiError("DOWNLOAD_DISABLED",
                           u"下载功能未启用，请先在设置中开启")
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        urls = payload.get("urls")
        if urls is None:
            single = payload.get("url")
            urls = [single] if single else []
        if not isinstance(urls, list):
            raise invalid(u"urls 必须是字符串数组")
        cleaned = []
        for item in urls:
            text = (item or u"").strip() if isinstance(item, str) else u""
            if text and text not in cleaned:
                cleaned.append(text)
        if not cleaned:
            raise invalid(u"请至少提供一个下载地址")
        if len(cleaned) > config.DOWNLOAD_BATCH_MAX_ITEMS:
            raise invalid(u"单次最多提交 %d 个地址"
                          % config.DOWNLOAD_BATCH_MAX_ITEMS)
        for url in cleaned:
            try:
                assert_url_allowed(
                    url, bool(settings.get("download_allow_private")))
            except DownloadError as exc:
                # 提交期校验失败要变成 4xx API 错误（运行期才走任务失败）。
                raise ApiError(exc.code, exc.message, details=exc.details)
        options = self._options_for(settings, payload.get("options") or {})
        with self._lock:
            active = [job for job in self._jobs.values()
                      if job.status in ACTIVE_STATUSES]
            limit = int(settings.get("download_job_limit")
                        or config.DOWNLOAD_JOB_LIMIT_DEFAULT)
            if len(active) + len(cleaned) > limit:
                raise ApiError(
                    "JOB_LIMIT_REACHED",
                    u"活动下载任务已达上限（%d），请等待完成或清理后再提交"
                    % limit)
            group = self._create_group_locked(
                payload.get("group_title") or u"",
                options.get("output_dir") or u"",
                cleaned)
            created = []
            for url in cleaned:
                job = DownloadJob(make_job_id(), url, group_id=group["group_id"],
                                  options=dict(options), user=user)
                job.title = job.options.get("title") or u""
                job.message = u"排队中"
                self._jobs[job.job_id] = job
                self._queue.append(job.job_id)
                group["job_ids"].append(job.job_id)
                created.append(job)
            self._save_group_locked(group)
            self._prune_locked(int(settings.get("download_job_limit")
                                   or config.DOWNLOAD_JOB_LIMIT_DEFAULT))
        for job in created:
            self._persist(job, force=True)
        return {
            "ok": True,
            "group": self._group_view(group),
            "jobs": [job.snapshot() for job in created],
        }

    def _options_for(self, settings, overrides):
        if not isinstance(overrides, dict):
            overrides = {}

        def pick(name, default=None):
            if name in overrides and overrides[name] not in (None, u""):
                return overrides[name]
            return default

        try:
            segment_concurrency = int(pick(
                "segment_concurrency",
                settings.get("download_segment_concurrency")))
        except (TypeError, ValueError):
            segment_concurrency = config.DOWNLOAD_DEFAULT_SEGMENT_CONCURRENCY
        try:
            retry = int(pick("retry", settings.get("download_retry")))
        except (TypeError, ValueError):
            retry = config.DOWNLOAD_DEFAULT_RETRY
        try:
            timeout = int(pick("timeout_sec",
                               settings.get("download_timeout_sec")))
        except (TypeError, ValueError):
            timeout = config.DOWNLOAD_DEFAULT_TIMEOUT_SEC
        try:
            rate = int(pick("rate_limit_kbps",
                            settings.get("download_rate_limit_kbps")))
        except (TypeError, ValueError):
            rate = 0
        try:
            max_height = int(pick("max_height",
                                  settings.get("download_max_height")))
        except (TypeError, ValueError):
            max_height = 0
        try:
            min_free = int(pick("min_free_gb",
                                settings.get("download_min_free_gb")))
        except (TypeError, ValueError):
            min_free = config.DOWNLOAD_MIN_FREE_GB_DEFAULT
        try:
            tolerance = int(pick(
                "verify_tolerance_sec",
                settings.get("download_verify_tolerance_sec")))
        except (TypeError, ValueError):
            tolerance = config.DOWNLOAD_VERIFY_TOLERANCE_DEFAULT

        output_dir = pick("output_dir",
                          settings.get("download_output_dir") or u"")
        if output_dir:
            output_dir = str(output_dir).strip()
        subdir_mode = pick("subdir_mode",
                           settings.get("download_subdir_mode") or u"none")
        if subdir_mode not in config.DOWNLOAD_SUBDIR_MODES:
            subdir_mode = config.DOWNLOAD_SUBDIR_DEFAULT
        return {
            "title": _safe_name(pick("title", u""), u"") if pick("title", u"")
            else u"",
            "output_dir": output_dir,
            "subdir_mode": subdir_mode,
            "subdir_template": pick("subdir_template",
                                    settings.get(
                                        "download_subdir_template") or u""),
            "segment_concurrency": max(
                1, min(segment_concurrency,
                       config.DOWNLOAD_SEGMENT_CONCURRENCY_MAX)),
            "retry": max(0, min(retry, config.DOWNLOAD_RETRY_MAX)),
            "timeout": max(config.DOWNLOAD_TIMEOUT_MIN_SEC,
                           min(timeout, config.DOWNLOAD_TIMEOUT_MAX_SEC)),
            "rate_limit_kbps": max(0, rate),
            "max_height": max(0, max_height),
            "min_free_gb": max(0, min_free),
            "verify_tolerance": max(0, tolerance),
            "user_agent": pick("user_agent",
                               settings.get("download_user_agent") or u""),
            "referer": pick("referer", settings.get("download_referer") or u""),
            "cookie": pick("cookie", settings.get("download_cookie") or u""),
            "proxy": pick("proxy", settings.get("download_proxy") or u""),
            "allow_private": bool(settings.get("download_allow_private")),
            "overwrite": bool(pick("overwrite", False)),
            "keep_temp": bool(pick("keep_temp",
                                   settings.get("download_keep_temp"))),
            "auto_scan": bool(settings.get("download_auto_scan")),
            "auto_subtitle": bool(settings.get("download_auto_subtitle")),
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

    def list_page(self, status=None, group_id=None, offset=0, limit=50,
                  q=None):
        wanted = None
        if status:
            items = [part.strip() for part in str(status).split(",")
                     if part.strip()]
            wanted = [item for item in items if item in STATUSES] or None
        needle = (q or u"").strip().lower()
        with self._lock:
            jobs = list(self._jobs.values())
        rows = []
        for job in jobs:
            if group_id and job.group_id != group_id:
                continue
            if wanted and job.status not in wanted:
                continue
            if needle:
                haystack = u" ".join([job.title or u"", job.url or u"",
                                      job.output_name or u""]).lower()
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
        paused = [job for job in jobs if job.status == STATUS_PAUSED]
        position = {}
        ordered = []
        queued = [job for job in jobs if job.status == STATUS_QUEUED]
        for index, job_id in enumerate(queued_ids):
            position[job_id] = index + 1
        for job in sorted(queued, key=lambda item: item.created_epoch):
            item = job.snapshot()
            item["queue_position"] = position.get(job.job_id)
            ordered.append(item)
        queued.sort(key=lambda item: item.created_epoch)
        recent = [job for job in jobs if job.status in FINAL_STATUSES]
        recent.sort(key=lambda item: item.finished_epoch or 0, reverse=True)
        return {
            "ok": True,
            "active": [job.snapshot() for job in active[:active_limit]],
            "queued": ordered[:queued_limit],
            "paused": [job.snapshot() for job in paused[:queued_limit]],
            "recent": [job.snapshot() for job in recent[:recent_limit]],
            "counts": self.counts(),
            "concurrency": self._concurrency(),
        }

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise not_found(u"下载任务不存在: %s" % job_id)
        return job

    def get_view(self, job_id):
        return self.get(job_id).snapshot(include_detail=True)

    def file_path(self, job_id):
        job = self.get(job_id)
        if job.status != STATUS_DONE:
            raise ApiError("DOWNLOAD_NOT_DONE", u"任务尚未完成")
        path = job.output_path or u""
        if not path or not os.path.isfile(path):
            raise not_found(u"下载文件不存在或已被移动")
        return job, path

    # -- 对外：控制 -----------------------------------------------------
    def pause(self, job_id):
        job = self.get(job_id)
        with self._lock:
            if job.status == STATUS_QUEUED:
                if job.job_id in self._queue:
                    self._queue.remove(job.job_id)
                job.status = STATUS_PAUSED
                job.message = u"已暂停"
            elif job.status == STATUS_RUNNING:
                job._pause.set()
                job.message = u"暂停请求已提交（分片边界生效）"
            else:
                raise ApiError("DOWNLOAD_BUSY", u"当前状态不可暂停")
        self._persist(job, force=True)
        return job.snapshot()

    def resume(self, job_id):
        job = self.get(job_id)
        with self._lock:
            if job.status != STATUS_PAUSED:
                raise ApiError("DOWNLOAD_BUSY", u"只有已暂停的任务可以继续")
            job._pause.clear()
            job.status = STATUS_QUEUED
            job.message = u"重新排队"
            if job.job_id not in self._queue:
                self._queue.append(job.job_id)
        self._persist(job, force=True)
        return job.snapshot()

    def cancel(self, job_id):
        job = self.get(job_id)
        with self._lock:
            if job.status in FINAL_STATUSES:
                raise ApiError("DOWNLOAD_BUSY", u"任务已结束")
            if job.status == STATUS_QUEUED:
                if job.job_id in self._queue:
                    self._queue.remove(job.job_id)
                job.status = STATUS_CANCELLED
                job.message = u"已取消"
                job.finished_epoch = time.time()
                job._cancel.set()
                self._cleanup_temp(job)
            else:
                job._cancel.set()
                job._pause.clear()
                job.message = u"取消请求已提交"
        self._persist(job, force=True)
        return job.snapshot()

    def retry(self, job_id):
        job = self.get(job_id)
        with self._lock:
            if job.status not in FINAL_STATUSES:
                raise ApiError("DOWNLOAD_BUSY", u"任务尚未结束")
            job.status = STATUS_QUEUED
            job.stage = STATUS_QUEUED
            job.message = u"重新排队"
            job.error_code = u""
            job.error_message = u""
            job.progress = 0
            job.finished_epoch = 0.0
            job._cancel.clear()
            job._pause.clear()
            job._abort.clear()
            if job.job_id not in self._queue:
                self._queue.append(job.job_id)
        self._persist(job, force=True)
        return job.snapshot()

    def delete(self, job_id, delete_files=False):
        job = self.get(job_id)
        with self._lock:
            if job.status not in FINAL_STATUSES:
                raise ApiError("DOWNLOAD_BUSY", u"任务尚未结束，不能删除")
            self._jobs.pop(job_id, None)
            if job_id in self._queue:
                self._queue.remove(job_id)
        removed_file = False
        if delete_files and job.output_path and os.path.isfile(job.output_path):
            try:
                base = check_path_safety(job.output_path, reject_hidden=False)
                os.remove(base)
                removed_file = True
            except (ApiError, OSError):
                removed_file = False
        self._cleanup_temp(job)
        try:
            self.store.delete_download_job(job_id)
        except Exception:
            logger.exception(u"删除下载任务失败: %s", job_id)
        group = None
        if job.group_id:
            group = self._group_locked(job.group_id, prune=(job_id,))
        return {"ok": True, "deleted": job_id,
                "deleted_file": removed_file, "group": group}

    # -- 批量组 ---------------------------------------------------------
    def _create_group_locked(self, title, output_dir, urls):
        now = time.time()
        group = {
            "group_id": make_group_id(),
            "title": _safe_name(title, u"") or time.strftime(
                u"%Y-%m-%d %H:%M 下载", time.localtime(now)),
            "output_dir": output_dir,
            "url_count": len(urls),
            "job_ids": [],
            "created_epoch": now,
            "updated_epoch": now,
        }
        return group

    def _save_group_locked(self, group):
        group["updated_epoch"] = time.time()
        try:
            self.store.save_download_group(group["group_id"], group)
        except Exception:
            logger.exception(u"下载组保存失败: %s", group.get("group_id"))

    def _group_locked(self, group_id, prune=None):
        record = None
        try:
            record = self.store.load_download_group(group_id)
        except Exception:
            record = None
        if record is None:
            return None
        job_ids = list(record.get("job_ids") or [])
        if prune:
            job_ids = [item for item in job_ids if item != prune[0]]
            record["job_ids"] = job_ids
            self._save_group_locked(record)
        return record

    def get_group(self, group_id):
        record = self._group_locked(group_id)
        if record is None:
            raise not_found(u"下载组不存在: %s" % group_id)
        return self._group_view(record)

    def list_groups(self, limit=50):
        try:
            records = self.store.load_download_groups(limit=limit)
        except Exception:
            records = []
        return {"ok": True,
                "groups": [self._group_view(record) for record in records]}

    def _group_view(self, record):
        job_ids = list(record.get("job_ids") or [])
        with self._lock:
            jobs = [self._jobs.get(job_id) for job_id in job_ids]
        jobs = [job for job in jobs if job is not None]
        counts = {status: 0 for status in STATUSES}
        for job in jobs:
            counts[job.status] = counts.get(job.status, 0) + 1
        total = len(jobs) or int(record.get("url_count") or 0)
        finished = sum(counts.get(status, 0) for status in FINAL_STATUSES)
        view = dict(record)
        view.update({
            "ok": True,
            "jobs": [job.snapshot() for job in jobs],
            "counts": counts,
            "total": total,
            "finished": finished,
            "progress": int(round(100.0 * finished / total)) if total else 0,
            "is_done": bool(total) and finished >= total,
        })
        return view

    def group_control(self, group_id, action):
        record = self._group_locked(group_id)
        if record is None:
            raise not_found(u"下载组不存在: %s" % group_id)
        results = []
        for job_id in list(record.get("job_ids") or []):
            try:
                if action == "pause":
                    results.append(self.pause(job_id))
                elif action == "resume":
                    results.append(self.resume(job_id))
                elif action == "cancel":
                    results.append(self.cancel(job_id))
                elif action == "retry":
                    results.append(self.retry(job_id))
            except ApiError:
                continue
        return {"ok": True, "action": action,
                "group": self.get_group(group_id), "jobs": results}

    # -- 最终路径 -------------------------------------------------------
    def _final_path(self, job, ext):
        directory = job.output_dir or u""
        if not directory:
            raise DownloadError("OUTPUT_DIR_INVALID", u"下载目录未确定")
        stem = job.title or self._title_from(u"", job.url) or \
            u"download-" + time.strftime("%Y%m%d-%H%M%S",
                                         time.localtime(job.created_epoch))
        stem = _safe_name(stem)
        if not stem.lower().endswith(ext):
            name = stem + ext
        else:
            name = stem
        candidate = os.path.join(directory, name)
        if job.options.get("overwrite"):
            return candidate
        if not os.path.exists(candidate):
            return candidate
        base, suffix = os.path.splitext(name)
        for index in range(2, 200):
            candidate = os.path.join(directory, u"%s (%d)%s" % (
                base, index, suffix))
            if not os.path.exists(candidate):
                return candidate
        raise DownloadError("OUTPUT_EXISTS",
                            u"同名文件过多，无法确定输出文件名")

    # -- 清理 -----------------------------------------------------------
    def _prune_locked(self, limit):
        if limit <= 0:
            return
        finished = [job for job in self._jobs.values()
                    if job.status in FINAL_STATUSES]
        if len(finished) <= limit:
            return
        finished.sort(key=lambda item: item.finished_epoch or item.created_epoch)
        for job in finished[:len(finished) - limit]:
            self._jobs.pop(job.job_id, None)
            try:
                self.store.delete_download_job(job.job_id)
            except Exception:
                logger.exception(u"清理下载任务失败: %s", job.job_id)
            if job.temp_kept:
                self._cleanup_temp(job)
