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
import http.client
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config, dash, hls, pageparse
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


class _JobSkipped(Exception):
    """目标文件已存在：按「下载前去重」直接完成，不消耗流量。"""

    def __init__(self, path, size, expected=None):
        super(_JobSkipped, self).__init__(u"已存在: %s" % path)
        self.path = path
        self.size = size
        self.expected = expected


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


_HHMM_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")


def _parse_hhmm(text):
    """'01:30' → 90（分钟）；非法返回 None。"""
    match = _HHMM_RE.match((text or u"").strip())
    if match is None:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


_SEASON_EPISODE_RE = re.compile(
    r"[Ss](\d{1,2})[ ._-]*[Ee](\d{1,4})")
_SEASON_CN_RE = re.compile(u"第([0-9一二三四五六七八九十百]{1,4})季")
_EPISODE_CN_RE = re.compile(u"第([0-9一二三四五六七八九十百]{1,4})集")
_CN_NUMBERS = {u"一": 1, u"二": 2, u"三": 3, u"四": 4, u"五": 5, u"六": 6,
               u"七": 7, u"八": 8, u"九": 9, u"十": 10}


def _cn_number(text):
    if not text:
        return None
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            return None
    if text in _CN_NUMBERS:
        return _CN_NUMBERS[text]
    return None


def _split_season_episode(title):
    """从标题里猜季/集：S01E02 / 第1季 第2集 → ('S01', 'E02')。"""
    text = title or u""
    season = episode = ""
    match = _SEASON_EPISODE_RE.search(text)
    if match:
        season = u"S%02d" % int(match.group(1))
        episode = u"E%02d" % int(match.group(2))
        return season, episode
    cn_season_match = _SEASON_CN_RE.search(text)
    cn_episode_match = _EPISODE_CN_RE.search(text)
    cn_season = _cn_number(cn_season_match.group(1)) if cn_season_match else None
    cn_episode = (_cn_number(cn_episode_match.group(1))
                  if cn_episode_match else None)
    if cn_season:
        season = u"S%02d" % cn_season
    if cn_episode:
        episode = u"E%02d" % cn_episode
    return season, episode


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


class _PooledResponse(object):
    """v0054：keep-alive 连接上的响应（读尽后把连接放回池里复用）。"""

    def __init__(self, client, key, conn, response, url):
        self._client = client
        self._key = key
        self._conn = conn
        self._response = response
        self._url = url
        self._reusable = False
        self._closed = False

    @property
    def status(self):
        return getattr(self._response, "status", 200)

    @property
    def headers(self):
        return self._response.headers

    def geturl(self):
        return self._url

    def read(self, size=-1):
        data = self._response.read(size)
        if not data:
            self._reusable = True
        return data

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._reusable and getattr(self._response, "isclosed", None) \
                and self._response.isclosed():
            self._client._release(self._key, self._conn)
        else:
            self._client._discard(self._key, self._conn)


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
        # v0054：keep-alive 连接池（仅无代理的 http/https 分片下载使用）
        self._pool = {}
        self._pool_lock = threading.Lock()

    def close(self):
        with self._pool_lock:
            connections = list(self._pool.values())
            self._pool.clear()
        for conn in connections:
            try:
                conn.close()
            except Exception:
                pass

    # -- keep-alive -----------------------------------------------------
    @staticmethod
    def _pool_key(parsed):
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme.lower() == "https" else 80
        return (parsed.scheme.lower(), (parsed.hostname or "").lower(), port)

    def _new_connection(self, parsed, key, timeout):
        host, port = key[1], key[2]
        if key[0] == "https":
            context = ssl.create_default_context()
            return http.client.HTTPSConnection(
                host, port, timeout=timeout, context=context)
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def _release(self, key, conn):
        if conn is None or getattr(conn, "sock", None) is None:
            return
        with self._pool_lock:
            existing = self._pool.get(key)
            if existing is None:
                self._pool[key] = conn
                return
        try:
            conn.close()
        except Exception:
            pass

    def _discard(self, key, conn):
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        with self._pool_lock:
            if self._pool.get(key) is conn:
                self._pool.pop(key, None)

    def _open_pooled(self, url, headers, limit):
        """返回 _PooledResponse；无法复用（重定向等）时返回 None 交给 urllib。"""
        if self.proxy or not url.lower().startswith(("http://", "https://")):
            return None
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            return None
        key = self._pool_key(parsed)
        with self._pool_lock:
            conn = self._pool.pop(key, None)
        fresh = conn is None
        if fresh:
            try:
                conn = self._new_connection(parsed, key, limit)
            except (OSError, http.client.HTTPException):
                return None
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        send_headers = dict(headers)
        send_headers["Connection"] = "keep-alive"
        try:
            conn.request("GET", path, headers=send_headers)
            response = conn.getresponse()
        except (http.client.HTTPException, OSError):
            self._discard(key, conn)
            return None
        status = getattr(response, "status", 200)
        if 300 <= status < 400:
            try:
                response.read()
            except Exception:
                pass
            self._discard(key, conn)
            return None
        if status >= 400:
            self._discard(key, conn)
            raise DownloadError(
                "HTTP_STATUS", u"服务器返回 HTTP %s" % status,
                {"status": int(status), "url": _sanitize_error(url)})
        return _PooledResponse(self, key, conn, response, url)

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

    def open(self, url, byte_range=None, timeout=None, reuse=False):
        assert_url_allowed(url, allow_private=self.allow_private)
        limit = float(timeout or self.timeout)
        if reuse:
            pooled = self._open_pooled(url, self._headers(byte_range), limit)
            if pooled is not None:
                return pooled
        request = urllib.request.Request(
            url, headers=self._headers(byte_range), method="GET")
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
        # v0053：去重跳过 / 优先级 / 自动重试排期 / 产物高度（命名模板用）
        self.skipped = False
        self.priority = 0
        self.retry_at = 0.0
        self.height = 0
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
            # v0053：去重跳过 / 优先级 / 自动重试排期
            "skipped": bool(self.skipped),
            "priority": int(self.priority or 0),
            "retry_at": _iso(self.retry_at) if self.retry_at else None,
            "height": int(self.height or 0),
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
            "skipped": bool(self.skipped),
            "priority": int(self.priority or 0),
            "retry_at": float(self.retry_at or 0.0),
            "height": int(self.height or 0),
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
                      "skipped", "priority", "retry_at", "height",
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
                 ffmpeg_tool=None, on_complete=None, notifier=None):
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
        # v0053：下载完成/失败通知（NotificationService.notify）
        self._notifier = notifier
        # v0053：页面解析短时缓存（同一 URL 10 分钟内不重复抓取）
        self._parse_cache = {}
        # v0052：网页解析得到的 Cookie/Referer 临时暂存（不进设置、不落盘）
        self._parse_sessions = {}
        self._parse_lock = threading.Lock()
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
        now = time.time()
        with self._lock:
            while len(self._running) < limit and self._queue:
                index = self._next_queue_index_locked(now)
                if index is None:
                    break
                job_id = self._queue.pop(index)
                job = self._jobs.get(job_id)
                if job is None or job.status != STATUS_QUEUED:
                    continue
                job.status = STATUS_RUNNING
                job.retry_at = 0.0
                job.started_epoch = job.started_epoch or time.time()
                job.progress = max(int(job.progress or 0), 1)
                thread = threading.Thread(
                    target=self._run_job, args=(job,),
                    name="dl-job-%s" % job_id[-6:], daemon=True)
                self._running[job_id] = thread
                thread.start()

    def _next_queue_index_locked(self, now):
        """按 (优先级高, 创建早) 取下一个可运行任务；自动重试未到期的先跳过。"""
        best_index = None
        best_key = None
        for index, job_id in enumerate(self._queue):
            job = self._jobs.get(job_id)
            if job is None or job.status != STATUS_QUEUED:
                continue
            if job.retry_at and job.retry_at > now:
                continue
            key = (-int(job.priority or 0), job.created_epoch)
            if best_key is None or key < best_key:
                best_key = key
                best_index = index
        return best_index

    def _run_job(self, job):
        try:
            self._execute(job)
        except _JobPaused:
            self._finish_status(job, STATUS_PAUSED, u"已暂停（分片边界）")
        except _JobCancelled:
            self._cleanup_temp(job)
            self._finish_status(job, STATUS_CANCELLED, u"已取消")
        except _JobSkipped as exc:
            job.skipped = True
            job.output_path = exc.path
            job.output_size = exc.size
            job.output_name = os.path.basename(exc.path)
            job.progress = 100
            job.stage = "done"
            self._finish_status(
                job, STATUS_DONE,
                u"已存在同名文件，跳过下载：%s（%s）"
                % (job.output_name, _human_bytes(exc.size)))
        except DownloadError as exc:
            if not self._schedule_retry(job, exc):
                self._fail(job, exc.code, exc.message)
        except ApiError as exc:
            if not self._schedule_retry(job, exc):
                self._fail(job, exc.code, exc.message)
        except Exception:
            logger.exception(u"下载任务异常: %s", job.job_id)
            self._fail(job, "INTERNAL_ERROR", u"下载失败（内部错误）")
        finally:
            with self._lock:
                self._running.pop(job.job_id, None)
            client = getattr(job, "_client", None)
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
                job._client = None
            self._persist(job, force=True)

    # v0053：网络类错误可自动重试（指数退避重排），确定性错误不重试。
    RETRYABLE_CODES = frozenset(["HTTP_STATUS", "SEGMENT_FAILED",
                                 "INTERNAL_ERROR"])

    def _schedule_retry(self, job, exc):
        options = job.options
        if not options.get("auto_retry"):
            return False
        code = getattr(exc, "code", "") or "INTERNAL_ERROR"
        if code not in self.RETRYABLE_CODES:
            return False
        try:
            limit = int(options.get("auto_retry_max") or 0)
        except (TypeError, ValueError):
            limit = 0
        attempts = int(job.attempts or 0)
        if attempts > limit:
            return False
        delay = config.DOWNLOAD_AUTO_RETRY_BACKOFF_SEC * max(1, attempts)
        message = getattr(exc, "message", u"") or u""
        with self._lock:
            job.status = STATUS_QUEUED
            job.stage = STATUS_QUEUED
            job.retry_at = time.time() + delay
            job.message = u"自动重试 %d/%d（%d 秒后）" % (
                attempts, limit, int(delay))
            job.error_code = code
            job.error_message = _sanitize_error(
                message, job.options.get("cookie") or u"")
            # v0054：重试说明网络/服务端不稳，降低分片并发再试
            job._slow_hits = int(getattr(job, "_slow_hits", 0)) + 2
            if job.job_id not in self._queue:
                self._queue.append(job.job_id)
        self._persist(job, force=True)
        logger.info(u"下载任务自动重试 %s（%s/%s）", job.job_id,
                    attempts, limit)
        return True

    def _effective_rate_limit(self, job):
        """v0053：分时限速——低谷窗口用 offpeak 档，其余用常规档。"""
        options = job.options
        try:
            base = int(options.get("rate_limit_kbps") or 0)
        except (TypeError, ValueError):
            base = 0
        if not options.get("offpeak_enabled"):
            return max(0, base) * 1024
        if self._in_offpeak(options.get("offpeak_start") or "",
                            options.get("offpeak_end") or "",
                            time.localtime()):
            try:
                offpeak = int(options.get("offpeak_rate_limit_kbps") or 0)
            except (TypeError, ValueError):
                offpeak = 0
            return max(0, offpeak) * 1024
        return max(0, base) * 1024

    @staticmethod
    def _in_offpeak(start_text, end_text, now):
        start = _parse_hhmm(start_text)
        end = _parse_hhmm(end_text)
        if start is None or end is None or start == end:
            return False
        minutes = int(now.tm_hour) * 60 + int(now.tm_min)
        if start < end:
            return start <= minutes < end
        # 跨零点：（如 23:00–07:00）
        return minutes >= start or minutes < end

    def _execute(self, job):
        options = job.options
        job.attempts = int(job.attempts or 0) + 1
        job._cancel.clear()
        job._abort.clear()
        speed_bps = self._effective_rate_limit(job)
        job._limiter = _RateLimiter(speed_bps) if speed_bps > 0 else None

        job.step_start("resolve")
        self._touch(job, force=True)
        output_dir = self._prepare_output_dir(job)
        job.output_dir = output_dir
        client = self._client(job)
        job._client = client

        kind, body, playlist, final_url = self._probe_source(job, client)
        job.kind = kind
        job.url = final_url or job.url
        self._check_cancelled(job)

        if kind == "hls":
            job.step_end("resolve")
            self._maybe_skip(job, self._base_path(
                job, config.DOWNLOAD_OUTPUT_EXT))
            self._step_start(job, "download")
            local_list, audio_list = self._materialize_hls(
                job, client, playlist)
            self._step_end(job, "download")
            self._merge(job, local_list, audio_list)
            self._verify(job)
        elif kind == "dash":
            job.step_end("resolve")
            self._maybe_skip(job, self._base_path(
                job, config.DOWNLOAD_OUTPUT_EXT))
            self._step_start(job, "download")
            video_path, audio_path = self._materialize_dash(
                job, client, playlist)
            self._step_end(job, "download")
            self._remux(job, video_path, audio_path)
            self._verify(job)
        else:
            job.step_end("resolve")
            self._maybe_skip(job, self._base_path(
                job, self._extension_for(job)), job.bytes_total or None)
            self._step_start(job, "download")
            self._download_direct(job, client)
            self._step_end(job, "download")
            self._finalize_direct(job)

        self._complete(job)

    def _maybe_skip(self, job, final_path, expected_bytes=None):
        """v0053：下载前去重——目标文件已存在且体积吻合时直接完成。"""
        if not job.options.get("skip_existing"):
            return
        if job.options.get("overwrite") or not final_path:
            return
        if not os.path.isfile(final_path):
            return
        try:
            size = os.path.getsize(final_path)
        except OSError:
            return
        if expected_bytes and int(expected_bytes) != int(size):
            return
        raise _JobSkipped(final_path, size, expected_bytes)

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
            # v0054：DASH（.mpd）
            if self._looks_like_mpd(head, final_url, content_type):
                body = head.decode("utf-8", "replace")
                if len(head) >= 65536:
                    body, _headers, final_url = client.read_text(
                        final_url, limit=config.DOWNLOAD_PAGE_MAX_BYTES)
                playlist = self._parse_dash(job, body, final_url)
                return "dash", u"", playlist, final_url
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
    def _looks_like_mpd(head, url, content_type):
        if "dash+xml" in (content_type or ""):
            return True
        path = (urllib.parse.urlsplit(url or "").path or "").lower()
        if path.endswith(".mpd"):
            return True
        sample = (head or b"")[:8192].lstrip()
        return sample.startswith(b"<?xml") and b"<MPD" in (head or b"")[:8192]

    def _parse_dash(self, job, body, url):
        try:
            plan = dash.parse(body, url)
        except dash.DashError as exc:
            raise DownloadError(exc.code, exc.message)
        video = dash.pick_video(plan, int(job.options.get("max_height") or 0))
        if video is not None:
            job.height = int(video.get("height") or 0)
            try:
                job.variant = u"%dp" % job.height if job.height else u""
            except (TypeError, ValueError):
                job.variant = u""
        job.encryption = u"未加密"
        job.title = job.title or self._title_from(u"", url)
        fetches = (dash.plan_fetches(plan, "v", "video")
                   + dash.plan_fetches(plan, "a", "audio"))
        job.segments_total = len([item for item in fetches
                                  if item.get("kind") == "segment"])
        plan["_fetches"] = fetches
        return plan

    def _materialize_dash(self, job, client, plan):
        """v0054：下载 DASH 音视频轨 → 逐轨拼成 mp4（init + 分片）。"""
        job.temp_dir = os.path.join(
            job.output_dir, config.DOWNLOAD_TEMP_PREFIX + job.job_id)
        try:
            os.makedirs(job.temp_dir)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise DownloadError("OUTPUT_DIR_INVALID",
                                    u"无法创建临时目录: %s" % exc)
        fetches = plan.get("_fetches") or []
        if not fetches:
            raise DownloadError("PLAYLIST_INVALID", u"DASH 清单没有可下载分片")
        self._fetch_all(job, client, fetches, job.temp_dir)
        outputs = []
        for prefix, stream in (("v", "video"), ("a", "audio")):
            names = dash.stream_files(plan, prefix, stream)
            if not names:
                continue
            out_path = os.path.join(job.temp_dir, "%s_track.mp4" % stream)
            self._concat_files(job, job.temp_dir, names, out_path)
            outputs.append(out_path)
        if not outputs:
            raise DownloadError("PLAYLIST_INVALID", u"DASH 清单没有可合并的轨")
        video_path = outputs[0]
        audio_path = outputs[1] if len(outputs) > 1 else u""
        return video_path, audio_path

    def _concat_files(self, job, base_dir, names, out_path):
        part = out_path + config.DOWNLOAD_PART_EXT
        try:
            with open(part, "wb") as target:
                for name in names:
                    source_path = os.path.join(base_dir, name)
                    if not os.path.isfile(source_path):
                        raise DownloadError(
                            "SEGMENT_FAILED", u"缺少分片文件: %s" % name)
                    with open(source_path, "rb") as source:
                        while True:
                            self._check_cancelled(job)
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            target.write(block)
            os.replace(part, out_path)
        except OSError as exc:
            raise DownloadError("MERGE_FAILED", u"拼接 DASH 分片失败: %s" % exc)
        return out_path

    def _remux(self, job, video_path, audio_path=u""):
        """v0054：把 DASH 轨封装成最终 MP4（只做 copy，失败回退音频 AAC）。"""
        self._step_start(job, "merge")
        tool = self._ffmpeg
        if not tool.available():
            raise DownloadError("FFMPEG_UNAVAILABLE",
                                u"未找到 FFmpeg，无法合并视频")
        final_path = self._final_path(job, config.DOWNLOAD_OUTPUT_EXT)
        part = final_path + config.DOWNLOAD_PART_EXT
        timeout = float(job.options.get("merge_timeout") or 3600)
        base = ["-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", video_path]
        map_args = []
        if audio_path:
            base += ["-i", audio_path]
            map_args = ["-map", "0:v:0", "-map", "1:a:0?"]
        attempts = [
            base + map_args + ["-c", "copy", "-movflags", "+faststart",
                               "-f", "mp4", "-y", part],
            base + map_args + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                               "-movflags", "+faststart", "-f", "mp4",
                               "-y", part],
        ]
        last_error = u""
        for index, args in enumerate(attempts):
            try:
                code, _stdout, stderr = tool.run(
                    args, timeout=timeout,
                    stop_check=lambda: job._cancel.is_set())
            except VideoStopped:
                raise _JobCancelled()
            except VideoTimeout:
                raise DownloadError("MERGE_FAILED",
                                    u"封装超时（超过 %d 秒）" % int(timeout))
            except VideoSkip as exc:
                raise DownloadError("MERGE_FAILED",
                                    u"无法执行 FFmpeg：%s"
                                    % (exc.detail or exc.reason))
            if code == 0 and os.path.isfile(part) \
                    and os.path.getsize(part) > 0:
                break
            last_error = _sanitize_error(
                (stderr or b"").decode("utf-8", "replace"),
                job.options.get("cookie") or u"")
            logger.warning(u"DASH 封装失败（第 %d 次）: %s", index + 1,
                           last_error)
            if job._cancel.is_set():
                raise _JobCancelled()
        else:
            raise DownloadError("MERGE_FAILED",
                                u"封装失败: %s" % (last_error or u"FFmpeg 错误"))
        self._check_cancelled(job)
        size = os.path.getsize(part)
        if size <= 0:
            raise DownloadError("MERGE_FAILED", u"封装产物为空")
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
            # v0053：记录清晰度高度，供文件名模板 {height} 使用
            try:
                job.height = int(variant.get("height") or 0)
            except (TypeError, ValueError):
                job.height = 0
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
        concurrency = self._adaptive_concurrency(
            job, self._segment_concurrency(job))
        if concurrency < self._segment_concurrency(job):
            logger.info(u"下载 %s 自适应降低分片并发 → %d",
                        job.job_id, concurrency)
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
        started_at = time.monotonic()
        response = client.open(item["uri"],
                              byte_range=((start, end)
                                          if (byte_range or start)
                                          else None),
                              reuse=True)
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
        # v0054：记录分片耗时，供自适应并发使用
        self._note_segment_duration(job, time.monotonic() - started_at, size)

    # -- v0054：自适应分片并发 ------------------------------------------
    SLOW_SEGMENT_SEC = 8.0
    FAST_SEGMENT_SEC = 2.0

    def _note_segment_duration(self, job, elapsed, size):
        with self._lock:
            if elapsed >= self.SLOW_SEGMENT_SEC and size < 16 * 1024 * 1024:
                job._slow_hits = int(getattr(job, "_slow_hits", 0)) + 1
            elif elapsed <= self.FAST_SEGMENT_SEC:
                job._fast_hits = int(getattr(job, "_fast_hits", 0)) + 1
                if job._fast_hits >= 5 and getattr(job, "_slow_hits", 0) > 0:
                    job._slow_hits -= 1
                    job._fast_hits = 0

    def _adaptive_concurrency(self, job, base):
        """近期持续出现慢分片时把并发减半（最少 1）；恢复后自动回到基准。"""
        slow = int(getattr(job, "_slow_hits", 0))
        if slow >= 2:
            return max(1, int(base) // 2)
        return max(1, int(base))

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
            "-c", "copy", "-movflags", "+faststart",
            "-f", "mp4", "-y", part])
        attempts.append(base + map_args + [
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-f", "mp4", "-y", part])
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
        if status == STATUS_DONE:
            self._notify(job, "download_done", u"下载完成", job.message,
                         level="info")
        elif status == STATUS_FAILED:
            self._notify(job, "download_failed", u"下载失败", job.message,
                         level="error")

    def _notify(self, job, event, title, message, level="info"):
        """v0053：复用通知系统（DSM/Webhook/Telegram），失败只记日志。"""
        if self._notifier is None:
            return
        try:
            self._notifier.notify(
                event, title,
                u"%s：%s" % (title, message or job.job_id),
                level=level,
                detail={"job_id": job.job_id,
                        "name": job.output_name or job.title,
                        "size": int(job.output_size or 0),
                        "url": _sanitize_error(job.url)})
        except Exception:
            logger.debug(u"下载通知发送失败: %s", job.job_id)

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

    # -- 对外：网页解析（v0052） ----------------------------------------
    def parse_target(self, user, payload):
        """解析一个播放网页 / 粘贴的源码 / curl 命令，返回候选媒体地址。"""
        settings = self._settings_snapshot()
        if not settings.get("download_enabled"):
            raise ApiError("DOWNLOAD_DISABLED",
                           u"下载功能未启用，请先在设置中开启")
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        allow_private = bool(settings.get("download_allow_private"))
        page_url = str(payload.get("url") or u"").strip()
        raw_html = payload.get("html")
        raw_curl = payload.get("curl")
        headers = {}
        if raw_curl:
            try:
                parsed = pageparse.parse_curl(str(raw_curl))
            except pageparse.PageParseError as exc:
                raise ApiError(exc.code, exc.message)
            page_url = page_url or parsed.get("url") or u""
            headers.update(parsed.get("headers") or {})
        if not page_url and not raw_html:
            raise invalid(u"请提供网页地址、网页源码或 curl 命令之一")

        if raw_html:
            body = str(raw_html)
            final_url = page_url
            client = None
        else:
            try:
                assert_url_allowed(page_url, allow_private)
            except DownloadError as exc:
                raise ApiError(exc.code, exc.message, details=exc.details)
            # v0053：同一 URL 短时缓存，避免重复抓取（curl/源码不缓存）
            if not raw_curl:
                cached = self._parse_cache_get(page_url)
                if cached is not None:
                    result = dict(cached)
                    result["cached"] = True
                    result["parse_session"] = self._remember_parse_session(
                        {}, result.get("page_url") or page_url, settings)
                    return result
            client = _HttpClient(
                timeout=float(settings.get("download_timeout_sec")
                              or config.DOWNLOAD_DEFAULT_TIMEOUT_SEC),
                user_agent=headers.get("user_agent")
                or settings.get("download_user_agent")
                or config.DOWNLOAD_DEFAULT_USER_AGENT,
                referer=headers.get("referer")
                or settings.get("download_referer") or u"",
                cookie=headers.get("cookie")
                or settings.get("download_cookie") or u"",
                proxy=settings.get("download_proxy") or u"",
                allow_private=allow_private)
            try:
                raw_body, resp_headers, final_url = client.read_text(
                    page_url, limit=config.DOWNLOAD_PAGE_MAX_BYTES)
            except DownloadError as exc:
                raise ApiError(exc.code, exc.message, details=exc.details)
            body = pageparse.decode_body(
                raw_body, resp_headers.get("Content-Type") if resp_headers
                else u"")

        playlist_summary = None
        if hls.is_playlist(body):
            # 直接把 m3u8 地址当网页解析：返回单个候选 + 清晰度/时长摘要。
            playlist_summary = self._playlist_summary(final_url, body)
            candidates = [{
                "url": final_url,
                "kind": "hls",
                "ext": ".m3u8",
                "source": "direct",
                "label": u"播放列表",
                "score": 200,
                "valid": True,
            }]
        else:
            # v0054：直接把 .mpd 地址填进解析框
            if self._looks_like_mpd(body[:8192].encode("utf-8", "replace"),
                                    final_url, u""):
                playlist_summary = self._dash_summary(final_url, body)
                candidates = [{
                    "url": final_url,
                    "kind": "dash",
                    "ext": ".mpd",
                    "source": "direct",
                    "label": u"DASH 清单",
                    "score": 190,
                    "valid": True,
                }]
            else:
                candidates = pageparse.extract_candidates(body, final_url)
                # v0053：跟随一层 iframe（最多 N 个）再提取
                candidates = self._merge_iframe_candidates(
                    candidates, body, final_url, client, settings)
                candidates = candidates[:config.DOWNLOAD_PARSE_CANDIDATE_LIMIT]
                self._validate_candidates(candidates, client, settings)

        token = self._remember_parse_session(
            headers, final_url, settings)
        hint = u""
        if not candidates:
            hint = (u"没有从页面里找到媒体地址。该页可能需要登录或由 JS 动态加载，"
                    u"请用「粘贴网页源码」或浏览器 DevTools 的「Copy as cURL」再试。")
        result = {
            "ok": True,
            "page_url": final_url,
            "title": pageparse.extract_title(body),
            "summary": pageparse.summarize(candidates),
            "candidates": candidates,
            "playlist": playlist_summary,
            "parse_session": token,
            "hint": hint,
        }
        # v0053：只有纯 URL 请求才缓存（curl/源码带不同的登录态）
        if not raw_html and not raw_curl:
            self._parse_cache_put(page_url, result)
        return result

    def _parse_cache_get(self, key):
        if not key:
            return None
        now = time.time()
        with self._parse_lock:
            entry = self._parse_cache.get(key)
            if entry is None:
                return None
            if now - float(entry.get("at") or 0) > \
                    config.DOWNLOAD_PARSE_CACHE_TTL_SEC:
                self._parse_cache.pop(key, None)
                return None
            return dict(entry.get("result") or {})

    def _parse_cache_put(self, key, result):
        if not key:
            return
        now = time.time()
        with self._parse_lock:
            expired = [item for item, value in self._parse_cache.items()
                       if now - float((value or {}).get("at") or 0)
                       > config.DOWNLOAD_PARSE_CACHE_TTL_SEC]
            for item in expired:
                self._parse_cache.pop(item, None)
            while len(self._parse_cache) >= config.DOWNLOAD_PARSE_CACHE_MAX:
                oldest = min(self._parse_cache.items(),
                             key=lambda pair: (pair[1] or {}).get("at") or 0)
                self._parse_cache.pop(oldest[0], None)
            self._parse_cache[key] = {"at": now, "result": dict(result)}

    def _merge_iframe_candidates(self, candidates, body, page_url, client,
                                 settings):
        """v0053：跟随一层 iframe，把里面的候选并进来（失败忽略）。"""
        if client is None:
            return candidates
        frames = pageparse.extract_iframes(
            body, page_url, limit=config.DOWNLOAD_PARSE_IFRAME_MAX)
        if not frames:
            return candidates
        seen = {item.get("url") for item in candidates}
        merged = list(candidates)
        for frame in frames:
            try:
                frame_body, _headers, frame_url = client.read_text(
                    frame, limit=config.DOWNLOAD_PAGE_MAX_BYTES)
            except DownloadError:
                continue
            if hls.is_playlist(frame_body):
                # iframe 里直接是播放列表：作为候选保留
                if frame_url not in seen:
                    seen.add(frame_url)
                    merged.append({
                        "url": frame_url, "kind": "hls", "ext": ".m3u8",
                        "source": "iframe", "label": u"iframe 播放列表",
                        "score": 95, "valid": True,
                    })
                continue
            for item in pageparse.extract_candidates(frame_body, frame_url):
                if item.get("url") in seen:
                    continue
                seen.add(item.get("url"))
                item["source"] = "iframe"
                merged.append(item)
        merged.sort(key=lambda item: (-int(item.get("score") or 0),
                                      int(item.get("order") or 0)))
        return merged

    def _dash_summary(self, url, body):
        try:
            plan = dash.parse(body, url)
        except dash.DashError as exc:
            raise ApiError(exc.code, exc.message)
        video = plan.get("video") or {}
        audio = plan.get("audio") or {}
        return {
            "kind": "dash",
            "height": int(video.get("height") or 0),
            "duration": round(float(plan.get("duration") or 0.0), 1),
            "segments": len(video.get("segments") or []) +
                        len((video.get("init") and [1] or [])) +
                        len(audio.get("segments") or []),
            "tracks": [name for name, item in (("video", video),
                                               ("audio", audio)) if item],
            "encryption": u"未加密",
        }

    def _playlist_summary(self, url, body):
        try:
            playlist = hls.parse(body, url)
        except hls.HlsError as exc:
            raise ApiError(exc.code, exc.message)
        if playlist.get("kind") == "master":
            variants = []
            for item in hls.sorted_variants(playlist):
                variants.append({
                    "label": hls.variant_label(item),
                    "bandwidth": item.get("bandwidth") or 0,
                    "height": item.get("height") or 0,
                    "url": item.get("uri") or u"",
                })
            return {"kind": "master", "variants": variants[:20],
                    "encryption": hls.encryption_label(playlist)}
        return {
            "kind": "media",
            "segments": hls.segment_count(playlist),
            "duration": round(float(playlist.get("duration") or 0.0), 1),
            "encryption": hls.encryption_label(playlist),
        }

    def _validate_candidates(self, candidates, client, settings):
        """对前 N 个 m3u8/DASH 候选做一次范围请求，标记是否真的是播放列表。"""
        if client is None:
            return
        budget = int(config.DOWNLOAD_PARSE_VALIDATE_MAX)
        for item in candidates:
            if budget <= 0:
                break
            if item.get("kind") not in ("hls", "dash"):
                continue
            budget -= 1
            try:
                response = client.open(item["url"], byte_range=(0, 65535))
                try:
                    head = response.read(65536)
                finally:
                    response.close()
            except DownloadError as exc:
                item["valid"] = False
                item["note"] = _sanitize_error(exc.message)
                continue
            if item.get("kind") == "hls" and hls.is_playlist(head):
                item["valid"] = True
                item["note"] = u"播放列表"
            elif item.get("kind") == "dash":
                item["valid"] = head[:256].lstrip().startswith(b"<?xml") \
                    or b"<MPD" in head[:4096]
                item["note"] = u"MPD" if item.get("valid") else u"不是 MPD"
            else:
                item["valid"] = False
                item["note"] = u"不是有效的 m3u8"

    def _remember_parse_session(self, headers, page_url, settings):
        """暂存 curl/页面得到的登录态；返回一次性引用 token。"""
        cookie = (headers or {}).get("cookie") or ""
        referer = (headers or {}).get("referer") or page_url or ""
        ua = (headers or {}).get("user_agent") or ""
        if not cookie and not ua and not referer:
            return u""
        token = "ps-" + uuid.uuid4().hex[:20]
        now = time.time()
        with self._parse_lock:
            if len(self._parse_sessions) >= config.DOWNLOAD_PARSE_SESSION_MAX:
                self._prune_parse_sessions_locked(now)
            self._parse_sessions[token] = {
                "referer": referer, "cookie": cookie, "user_agent": ua,
                "created": now,
            }
        return token

    def _peek_parse_session(self, token):
        if not token or not isinstance(token, str):
            return None
        now = time.time()
        with self._parse_lock:
            entry = self._parse_sessions.get(token)
            if entry is None:
                return None
            ttl = config.DOWNLOAD_PARSE_SESSION_TTL_SEC
            if now - float(entry.get("created") or 0) > ttl:
                self._parse_sessions.pop(token, None)
                return None
            return dict(entry)

    def _prune_parse_sessions_locked(self, now):
        ttl = config.DOWNLOAD_PARSE_SESSION_TTL_SEC
        expired = [key for key, value in self._parse_sessions.items()
                   if now - float((value or {}).get("created") or 0) > ttl]
        for key in expired:
            self._parse_sessions.pop(key, None)
        if len(self._parse_sessions) < config.DOWNLOAD_PARSE_SESSION_MAX:
            return
        ordered = sorted(self._parse_sessions.items(),
                         key=lambda pair: (pair[1] or {}).get("created") or 0)
        for key, _value in ordered[:max(1, len(ordered) // 2)]:
            self._parse_sessions.pop(key, None)

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
        raw_options = payload.get("options") or {}
        session = None
        if isinstance(raw_options, dict):
            session = self._peek_parse_session(
                raw_options.get("parse_session"))
        options = self._options_for(settings, raw_options)
        if session:
            options["referer"] = (options.get("referer")
                                  or session.get("referer") or u"")
            options["cookie"] = (options.get("cookie")
                                 or session.get("cookie") or u"")
            options["user_agent"] = (options.get("user_agent")
                                     or session.get("user_agent") or u"")
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
        try:
            auto_retry_max = int(pick(
                "auto_retry_max",
                settings.get("download_auto_retry_max")) or 0)
        except (TypeError, ValueError):
            auto_retry_max = config.DOWNLOAD_AUTO_RETRY_MAX_DEFAULT
        auto_retry_max = max(0, min(auto_retry_max,
                                    config.DOWNLOAD_AUTO_RETRY_MAX_LIMIT))
        try:
            offpeak_rate = int(pick(
                "offpeak_rate_limit_kbps",
                settings.get("download_offpeak_rate_limit_kbps")) or 0)
        except (TypeError, ValueError):
            offpeak_rate = 0
        offpeak_rate = max(0, offpeak_rate)
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
            # --- v0053 下载增强 ---
            "skip_existing": bool(pick(
                "skip_existing", settings.get("download_skip_existing"))),
            "name_template": (pick(
                "name_template",
                settings.get("download_name_template")
                or config.DOWNLOAD_NAME_TEMPLATE_DEFAULT) or u""),
            "auto_retry": bool(pick(
                "auto_retry", settings.get("download_auto_retry"))),
            "auto_retry_max": auto_retry_max,
            "offpeak_enabled": bool(pick(
                "offpeak_enabled", settings.get("download_offpeak_enabled"))),
            "offpeak_start": pick(
                "offpeak_start", settings.get("download_offpeak_start")
                or config.DOWNLOAD_OFFPEAK_START_DEFAULT),
            "offpeak_end": pick(
                "offpeak_end", settings.get("download_offpeak_end")
                or config.DOWNLOAD_OFFPEAK_END_DEFAULT),
            "offpeak_rate_limit_kbps": offpeak_rate,
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
        # v0053：排队顺序 = 优先级高 → 创建早（与调度器的取队规则一致）
        queued.sort(key=lambda item: (-int(item.priority or 0),
                                      item.created_epoch))
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
            "paused": [job.snapshot() for job in paused[:queued_limit]],
            "recent": [job.snapshot() for job in recent[:recent_limit]],
            "counts": self.counts(),
            "concurrency": self._concurrency(),
            # v0053：下载目录空间看板
            "storage": self.storage_stats(),
        }

    def storage_stats(self, directory=None):
        """v0053：输出目录所在卷的剩余空间（用于看板与告警）。"""
        raw = directory or self._settings_snapshot().get(
            "download_output_dir") or u""
        if not raw:
            return None
        try:
            usage = shutil.disk_usage(raw)
        except OSError:
            return None
        total = float(usage.total or 0)
        used_pct = round(100.0 * float(usage.used) / total, 1) if total else 0.0
        return {
            "path": raw,
            "free_bytes": int(usage.free),
            "used_bytes": int(usage.used),
            "total_bytes": int(total),
            "used_pct": used_pct,
            "min_free_gb": int(self._settings_snapshot().get(
                "download_min_free_gb") or 0),
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

    # -- v0053：优先级 / 批量操作 / 导出 -------------------------------
    def set_priority(self, job_id, priority=None, top=False):
        job = self.get(job_id)
        with self._lock:
            if job.status in FINAL_STATUSES:
                raise ApiError("DOWNLOAD_BUSY", u"任务已结束，不能调整优先级")
            if top:
                values = [int(item.priority or 0)
                          for item in self._jobs.values()
                          if item.status in ACTIVE_STATUSES]
                job.priority = (max(values) if values else 0) + 1
            elif priority is not None:
                try:
                    value = int(priority)
                except (TypeError, ValueError):
                    raise invalid(u"priority 必须是整数")
                job.priority = max(0, min(value,
                                          config.DOWNLOAD_PRIORITY_MAX))
        self._persist(job, force=True)
        return job.snapshot()

    def bulk_action(self, user, action, scope="all"):
        """批量暂停/继续/取消/重试（v0053）。

        scope: all（活动任务）/ queued / running / paused / failed（失败+取消）。
        """
        if action not in ("pause", "resume", "cancel", "retry"):
            raise invalid(u"不支持的操作: %s" % action)
        mapping = {
            "all": ACTIVE_STATUSES,
            "queued": (STATUS_QUEUED,),
            "running": (STATUS_RUNNING,),
            "paused": (STATUS_PAUSED,),
            "failed": (STATUS_FAILED, STATUS_CANCELLED),
        }
        statuses = mapping.get(scope or "all")
        if statuses is None:
            raise invalid(u"不支持的批量范围: %s" % scope)
        with self._lock:
            targets = [job.job_id for job in self._jobs.values()
                       if job.status in statuses]
        targets = targets[:config.DOWNLOAD_BULK_MAX]
        results = []
        for job_id in targets:
            try:
                if action == "pause":
                    results.append(self.pause(job_id))
                elif action == "resume":
                    results.append(self.resume(job_id))
                elif action == "cancel":
                    results.append(self.cancel(job_id))
                else:
                    results.append(self.retry(job_id))
            except ApiError:
                continue
        return {"ok": True, "action": action, "scope": scope,
                "matched": len(targets), "affected": len(results),
                "counts": self.counts()}

    EXPORT_HEADERS = (
        ("job_id", u"任务 ID"), ("title", u"名称"), ("status", u"状态"),
        ("stage_label", u"阶段"), ("kind", u"类型"), ("variant", u"清晰度"),
        ("segments_total", u"分片总数"), ("segments_done", u"已完成分片"),
        ("bytes_total", u"总字节"), ("bytes_done", u"已下载字节"),
        ("output_name", u"文件名"), ("output_size", u"产物字节"),
        ("error_code", u"错误码"), ("error_message", u"错误信息"),
        ("attempts", u"尝试次数"), ("skipped", u"已跳过"),
        ("priority", u"优先级"), ("created_at", u"创建时间"),
        ("finished_at", u"完成时间"), ("output_path", u"产物路径"),
        ("url", u"地址"),
    )

    def export_rows(self, status=None, group_id=None):
        """v0053：导出下载任务（表头 + 行），供 CSV 下载。"""
        data = self.list_page(status=status, group_id=group_id,
                              offset=0, limit=5000)
        rows = [[label for _key, label in self.EXPORT_HEADERS]]
        for job in data.get("jobs") or []:
            rows.append([job.get(key, u"") for key, _label in
                         self.EXPORT_HEADERS])
        return rows

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
    def _render_name(self, job, ext):
        """v0053：按 download_name_template 渲染文件名（不含目录）。"""
        options = job.options
        when = time.localtime(job.created_epoch or time.time())
        fallback_title = (job.title or self._title_from(u"", job.url)
                          or u"download-" + time.strftime("%Y%m%d-%H%M%S", when))
        season, episode = _split_season_episode(fallback_title)
        tokens = {
            "title": _safe_name(fallback_title, u"video"),
            "site": _site_of(job.url),
            "date": time.strftime("%Y-%m-%d", when),
            "yyyy": time.strftime("%Y", when),
            "mm": time.strftime("%m", when),
            "dd": time.strftime("%d", when),
            "height": (u"%dp" % int(job.height)) if job.height else u"",
            "season": season,
            "episode": episode,
            "ext": (ext or u"").lstrip(u"."),
        }
        template = (options.get("name_template")
                    or config.DOWNLOAD_NAME_TEMPLATE_DEFAULT)
        try:
            rendered = template.format(**tokens)
        except (KeyError, IndexError, ValueError):
            rendered = u"{title}"
            rendered = rendered.format(**tokens)
        rendered = re.sub(r"\s+", u" ", rendered or u"").strip()
        stem = _safe_name(rendered, u"video")
        if not stem.lower().endswith(ext):
            stem += ext
        if len(stem) > _NAME_MAX + len(ext):
            stem = stem[:len(stem) - len(ext)][:_NAME_MAX] + ext
        return stem

    def _final_path(self, job, ext):
        directory = job.output_dir or u""
        if not directory:
            raise DownloadError("OUTPUT_DIR_INVALID", u"下载目录未确定")
        name = self._render_name(job, ext)
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

    def _base_path(self, job, ext):
        """v0053：不含「重名加序号」的首选路径（去重判断用）。"""
        directory = job.output_dir or u""
        if not directory:
            raise DownloadError("OUTPUT_DIR_INVALID", u"下载目录未确定")
        return os.path.join(directory, self._render_name(job, ext))

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
