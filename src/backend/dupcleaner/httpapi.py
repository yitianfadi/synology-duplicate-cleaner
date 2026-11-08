# -*- coding: utf-8 -*-
"""REST API 路由与处理（契约 §3–§8）。"""

from __future__ import absolute_import

import json
import logging
import os
import platform
import re
import time
import urllib.parse
import uuid

from . import config, similarity, timeutil, video_similarity
from .errors import ApiError, forbidden, invalid, not_found, auth_required
from .settings import (CLEAR_SECRET_ALIASES, default_settings,
                       public_settings)
from .subtitles import SubtitleError

logger = logging.getLogger("dupcleaner.api")


class Request(object):
    def __init__(self, method, path, query, headers, body, client_ip=None,
                 scheme="http"):
        self.method = (method or "GET").upper()
        self.path = path or "/"
        self.query = query or {}
        self.headers = headers or {}
        self.body = body or b""
        self.client_ip = client_ip
        self.scheme = scheme

    def query_last(self, name, default=None):
        values = self.query.get(name)
        if not values:
            return default
        return values[-1]

    def cookie(self, name):
        raw = self.headers.get("cookie") or ""
        for part in raw.split(";"):
            if "=" not in part:
                continue
            key, _, value = part.partition("=")
            if key.strip() == name:
                return value.strip()
        return None


class Response(object):
    def __init__(self, status=200, body=b"", headers=None,
                 content_type="application/json; charset=utf-8",
                 file_path=None, file_size=None, allow_range=False,
                 delete_after=False, cookies=None):
        self.status = int(status)
        self.body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.headers = dict(headers or {})
        self.content_type = content_type
        self.file_path = file_path
        self.file_size = file_size
        self.allow_range = allow_range
        self.delete_after = delete_after
        self.cookies = list(cookies or [])


RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range_header(header, size):
    """解析 Range 头，返回 (start, end) / None（忽略）/ 'unsatisfiable'。

    供媒体文件与文本预览共用；只支持单区间 bytes=a-b / bytes=a- / bytes=-n。
    """
    if not header:
        return None
    match = RANGE_RE.match(str(header).strip())
    if match is None:
        return None
    try:
        size = int(size)
    except (TypeError, ValueError):
        return "unsatisfiable"
    if size <= 0:
        return "unsatisfiable"
    start_text, end_text = match.group(1), match.group(2)
    if not start_text and not end_text:
        return None
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix == 0:
                return "unsatisfiable"
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except (TypeError, ValueError):
        return None
    if start >= size or start > end:
        return "unsatisfiable"
    end = min(end, size - 1)
    return (start, end)


def json_response(payload, status=200, headers=None, cookies=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return Response(status=status, body=body, headers=headers or {},
                    content_type="application/json; charset=utf-8",
                    cookies=cookies)


def error_response(exc):
    return json_response(exc.to_payload(), status=exc.status)


def _json_body(request, required=True):
    raw = request.body or b""
    if not raw:
        if required:
            raise invalid(u"请求体不能为空")
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise invalid(u"请求体必须是合法 JSON")
    if not isinstance(data, dict):
        raise invalid(u"请求体必须是 JSON 对象")
    return data


def _query_int(request, name, default):
    raw = request.query_last(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise invalid(u"参数 %s 必须是整数" % name)


def _query_bool(request, name, default=False):
    raw = request.query_last(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _query_float(request, name, default):
    raw = request.query_last(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise invalid(u"参数 %s 必须是数字" % name)


class Api(object):
    def __init__(self, services):
        self.services = services
        self.config = services.config
        self._routes = [
            ("GET", re.compile(r"^/api/healthz$"), self._healthz, True),
            ("POST", re.compile(r"^/api/auth/login$"), self._login, True),
            ("GET", re.compile(r"^/api/auth/me$"), self._me, False),
            ("POST", re.compile(r"^/api/auth/logout$"), self._logout, False),
            ("GET", re.compile(r"^/api/meta$"), self._meta, False),
            ("GET", re.compile(r"^/api/shares$"), self._shares, False),
            ("GET", re.compile(r"^/api/browse$"), self._browse, False),
            ("GET", re.compile(r"^/api/settings$"), self._get_settings, False),
            ("PUT", re.compile(r"^/api/settings$"), self._put_settings, False),
            ("POST", re.compile(r"^/api/scans$"), self._create_scan, False),
            ("GET", re.compile(r"^/api/scans$"), self._list_scans, False),
            ("GET", re.compile(r"^/api/scans/(?P<scan_id>[^/]+)$"),
             self._get_scan, False),
            ("POST", re.compile(
                r"^/api/scans/(?P<scan_id>[^/]+)/(?P<action>pause|resume|stop)$"),
             self._scan_control, False),
            ("GET", re.compile(r"^/api/scans/(?P<scan_id>[^/]+)/groups$"),
             self._scan_groups, False),
            ("GET", re.compile(r"^/api/scans/(?P<scan_id>[^/]+)/stats$"),
             self._scan_stats, False),
            ("GET", re.compile(r"^/api/scans/(?P<scan_id>[^/]+)/csv$"),
             self._scan_csv, False),
            ("GET", re.compile(r"^/api/scans/(?P<scan_id>[^/]+)/xlsx$"),
             self._scan_xlsx, False),
            ("POST", re.compile(r"^/api/scans/(?P<scan_id>[^/]+)/delete-plan$"),
             self._delete_plan, False),
            ("POST", re.compile(r"^/api/scans/(?P<scan_id>[^/]+)/delete$"),
             self._delete, False),
            ("GET", re.compile(r"^/api/scans/(?P<scan_id>[^/]+)/delete/(?P<job_id>[^/]+)$"),
             self._delete_job, False),
            # v0045：硬链接合并（计划/执行/查询/恢复）
            ("POST", re.compile(r"^/api/scans/(?P<scan_id>[^/]+)/merge-plan$"),
             self._merge_plan, False),
            ("POST", re.compile(r"^/api/scans/(?P<scan_id>[^/]+)/merge$"),
             self._merge_start, False),
            ("GET", re.compile(
                r"^/api/scans/(?P<scan_id>[^/]+)/merge/(?P<job_id>[^/]+)$"),
             self._merge_job, False),
            ("GET", re.compile(r"^/api/merges$"), self._merges, False),
            ("GET", re.compile(r"^/api/merges/(?P<job_id>[^/]+)$"),
             self._merge_detail, False),
            ("POST", re.compile(r"^/api/merges/(?P<job_id>[^/]+)/restore$"),
             self._merge_restore, False),
            # v0045：套件独立回收站（列表/详情/整组恢复/清理）
            ("GET", re.compile(r"^/api/trash$"), self._trash_list, False),
            ("GET", re.compile(r"^/api/trash/(?P<trash_id>[^/]+)$"),
             self._trash_detail, False),
            ("POST", re.compile(r"^/api/trash/(?P<trash_id>[^/]+)/restore$"),
             self._trash_restore, False),
            ("POST", re.compile(r"^/api/trash/(?P<trash_id>[^/]+)/purge$"),
             self._trash_purge, False),
            # v0046：通知系统 / 计划扫描
            ("GET", re.compile(r"^/api/notifications$"),
             self._notifications, False),
            ("POST", re.compile(r"^/api/notifications/test$"),
             self._notification_test, False),
            ("GET", re.compile(r"^/api/scheduler$"), self._scheduler, False),
            ("POST", re.compile(r"^/api/scheduler/run-now$"),
             self._scheduler_run_now, False),
            ("GET", re.compile(r"^/api/preview$"), self._preview, False),
            # v0044：预览增强（缩略图 / 媒体信息 / 缩略图时间点清单）
            ("GET", re.compile(r"^/api/preview/thumb$"), self._preview_thumb, False),
            ("GET", re.compile(r"^/api/preview/media$"), self._preview_media, False),
            ("GET", re.compile(r"^/api/preview/thumbs$"), self._preview_thumbs, False),
            ("GET", re.compile(r"^/api/logs$"), self._logs, False),
            ("GET", re.compile(r"^/api/logs/download$"), self._logs_download, False),
            # v0038：操作日志删除（管理员 + CSRF；支持选中删除 / 只保留最近 N 天）
            ("POST", re.compile(r"^/api/logs/audit/delete$"),
             self._delete_audit, False),
            ("POST", re.compile(r"^/api/logs/files/delete$"),
             self._delete_log_files, False),
            # v0021 P0：诊断 / 存储维护 / 删除任务历史（仅管理员）
            ("GET", re.compile(r"^/api/diagnostics$"), self._diagnostics, False),
            # v0043：增量扫描缓存管理（哈希缓存/媒体探测缓存）
            ("GET", re.compile(r"^/api/cache$"), self._cache_stats, False),
            ("POST", re.compile(r"^/api/cache/clear$"), self._cache_clear, False),
            ("POST", re.compile(r"^/api/storage/backup$"), self._storage_backup, False),
            ("POST", re.compile(r"^/api/storage/maintenance$"),
             self._storage_maintenance, False),
            ("GET", re.compile(r"^/api/deletions$"), self._deletions, False),
            # 云端字幕（管理员；POST /api/subtitles 等）
            ("POST", re.compile(r"^/api/subtitles$"),
             self._create_subtitle, False),
            ("GET", re.compile(r"^/api/subtitles$"),
             self._list_subtitles, False),
            # v0039：任务中心「即将执行」队列视图 / 批次组总览
            # （必须放在 /api/subtitles/{job_id} 通用路由之前）
            ("GET", re.compile(r"^/api/subtitles/queue$"),
             self._subtitle_queue, False),
            ("GET", re.compile(r"^/api/subtitles/batch-groups$"),
             self._subtitle_group_overview, False),
            ("GET", re.compile(r"^/api/subtitles/(?P<job_id>[^/]+)$"),
             self._get_subtitle, False),
            ("POST", re.compile(
                r"^/api/subtitles/(?P<job_id>[^/]+)/(?P<action>pause|resume)$"),
             self._subtitle_control, False),
            ("POST", re.compile(r"^/api/subtitles/(?P<job_id>[^/]+)/cancel$"),
             self._cancel_subtitle, False),
            # v0035：补翻译（已完成任务派生仅翻译任务；同源 active 幂等）
            ("POST", re.compile(
                r"^/api/subtitles/(?P<job_id>[^/]+)/translate$"),
             self._subtitle_translate, False),
            ("GET", re.compile(
                r"^/api/subtitles/(?P<job_id>[^/]+)/file/(?P<name>[^/]+)$"),
             self._subtitle_file, False),
            # v0026：模型列表 / 连接测试（管理员+CSRF；密钥不回传）
            ("POST", re.compile(r"^/api/subtitles/models$"),
             self._subtitle_models, False),
            ("POST", re.compile(r"^/api/subtitles/test$"),
             self._subtitle_test, False),
            # v0028：目录枚举 / 批量字幕（管理员+CSRF，docs/SUBTITLE_BATCH_CONTRACT.md）
            ("POST", re.compile(r"^/api/subtitles/enumerate$"),
             self._subtitle_enumerate, False),
            ("POST", re.compile(r"^/api/subtitles/batch$"),
             self._subtitle_batch_create, False),
            # v0032：批次组（一次提交自动拆成多批）
            ("POST", re.compile(r"^/api/subtitles/batch-groups$"),
             self._subtitle_group_create, False),
            ("GET", re.compile(
                r"^/api/subtitles/batch-groups/(?P<group_id>[^/]+)$"),
             self._subtitle_group_get, False),
            # v0035：整组补翻译（与上面各组控制动作路由不冲突）
            ("POST", re.compile(
                r"^/api/subtitles/batch-groups/(?P<group_id>[^/]+)/translate$"),
             self._subtitle_group_translate, False),
            ("POST", re.compile(
                r"^/api/subtitles/batch-groups/(?P<group_id>[^/]+)/"
                r"(?P<action>pause|resume|cancel|retry)$"),
             self._subtitle_group_control, False),
            ("GET", re.compile(
                r"^/api/subtitles/batches/(?P<batch_id>[^/]+)$"),
             self._subtitle_batch_get, False),
            ("POST", re.compile(
                r"^/api/subtitles/batches/(?P<batch_id>[^/]+)/"
                r"(?P<action>pause|resume|cancel|retry)$"),
             self._subtitle_batch_control, False),
        ]

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------
    def dispatch(self, request):
        try:
            return self._dispatch(request)
        except ApiError as exc:
            return error_response(exc)
        except Exception:  # pragma: no cover - 防御性兜底
            # v0021：全局异常绝不把异常文本/堆栈返回给客户端（可能含密钥）。
            # 完整堆栈只写服务器日志；客户端只拿到固定中文提示 + request_id。
            request_id = uuid.uuid4().hex[:16]
            logger.exception("API 处理异常 [%s]: %s %s",
                             request_id, request.method, request.path)
            response = error_response(ApiError(
                "INTERNAL_ERROR", u"服务器内部错误，请稍后重试",
                details={"request_id": request_id}))
            response.headers["X-Request-Id"] = request_id
            return response

    def _dispatch(self, request):
        self._verify_origin(request)
        matched = None
        for method, pattern, handler, public in self._routes:
            match = pattern.match(request.path)
            if match is None:
                continue
            if method != request.method:
                matched = matched or (method, handler, match, public)
                continue
            matched = (method, handler, match, public)
            break
        if matched is None:
            raise not_found(u"接口不存在: %s %s" % (request.method, request.path))
        method, handler, match, public = matched
        if method != request.method:
            raise ApiError("INVALID_ARGUMENT",
                           u"请求方法不支持，应为 %s" % method, status=405)

        session = None
        if not public:
            session = self.services.auth.require(
                request.cookie(config.SESSION_COOKIE))
        if request.method not in ("GET", "HEAD") and not public:
            token = request.headers.get("x-csrf-token")
            if not self.services.auth.verify_csrf(session, token):
                from .errors import csrf_failed
                raise csrf_failed()

        result = handler(request, session, match.groupdict())
        if isinstance(result, Response):
            return result
        return json_response(result)

    def _verify_origin(self, request):
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urllib.parse.urlsplit(origin)
            except ValueError:
                raise forbidden(u"Origin 非法")
            host_header = request.headers.get("host") or ""
            if not host_header:
                raise forbidden(u"缺少 Host 头")
            origin_netloc = parsed.netloc or ""
            if origin_netloc.lower() != host_header.lower():
                if not (self.config.dev_mode
                        and _is_loopback(parsed.hostname or "")
                        and _is_loopback(host_header.split(":")[0])):
                    raise forbidden(u"Origin 与 Host 不同源，已拒绝请求")
            return
        if self.config.dev_mode:
            return
        if request.method in ("GET", "HEAD"):
            # <img>/<video>/<a download> 等浏览器资源请求不带 Origin
            return
        raise forbidden(u"缺少 Origin 头，拒绝跨站请求")

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def _healthz(self, request, session, params):
        return json_response({
            "ok": True,
            "status": "ok",
            "version": config.APP_VERSION,
        })

    def _meta(self, request, session, params):
        dsm_version = "dev" if self.config.dev_mode else (
            timeutil.read_dsm_version() or "unknown")
        try:
            statx = timeutil.statx_supported()
        except Exception:
            statx = False
        return json_response({
            "ok": True,
            "app_version": config.APP_VERSION,
            "dsm_version": dsm_version,
            "python_version": platform.python_version(),
            "creation_time_supported": timeutil.creation_time_supported(),
            "statx_supported": bool(statx),
            "platform": platform.machine() or platform.system(),
            "dev_mode": bool(self.config.dev_mode),
            "defaults": public_settings(default_settings()),
            "limits": {
                "max_files": config.MAX_FILES_DEFAULT,
                "max_roots": config.MAX_ROOTS,
                "max_excludes": config.MAX_EXCLUDES,
            },
            # 相似度模式能力（docs/SIMILARITY_CONTRACT.md §3）
            "similarity": similarity.capabilities(),
            # 视频视觉相似能力（docs/VIDEO_CONTRACT.md §2）
            "video": video_similarity.video_capabilities(
                self.config, self.services.settings.get()),
            # v0043 增量扫描 / 保留规则链 / 排除规则能力
            # （docs/INCREMENTAL_SCAN_CONTRACT.md）
            "scan": {
                "keep_rules": [
                    {"name": name,
                     "label": config.KEEP_RULE_LABELS.get(name, name),
                     "needs_arg": name in ("path_priority",
                                           "filename_priority")}
                    for name in config.KEEP_RULE_NAMES
                ],
                "keep_rules_max": config.KEEP_RULES_MAX,
                "sidecar_modes": list(config.SIDECAR_MODES),
                "exclude_pattern_max_count": config.EXCLUDE_PATTERN_MAX_COUNT,
                "exclude_pattern_max_len": config.EXCLUDE_PATTERN_MAX_LEN,
                "quick_sample_version": config.QUICK_SAMPLE_VERSION,
                "large_file_sample_threshold_bytes":
                    config.LARGE_FILE_SAMPLE_THRESHOLD_BYTES,
                "hash_cache_max_entries": config.HASH_CACHE_MAX_ENTRIES,
                "ignore_file_name": config.IGNORE_FILE_NAME,
                # v0044：删除白名单（保护）原因中文标签
                "protect_reasons": dict(config.PROTECT_REASON_LABELS),
            },
            # v0044：预览增强能力
            "preview": {
                "thumb_width_default": config.THUMB_DEFAULT_WIDTH,
                "thumb_width_max": config.THUMB_MAX_WIDTH,
                "thumb_count_default": config.THUMB_COUNT_DEFAULT,
                "thumb_count_max": config.THUMB_COUNT_MAX,
                "thumb_cache_entries": config.THUMB_MAX_ENTRIES,
                "sync_tolerance_sec": config.PREVIEW_SYNC_TOLERANCE_SEC,
            },
            # v0045：硬链接合并 / 回收站模式
            "merge": {
                "enabled": bool(self.services.settings.get().get("merge_enabled")),
                "enabled_default": config.MERGE_ENABLED_DEFAULT,
                "plan_ttl_seconds": config.MERGE_PLAN_TTL_SECONDS,
            },
            "recycle": {
                "mode": (self.services.settings.get().get("delete_recycle_mode")
                         or config.RECYCLE_MODE_DEFAULT),
                "modes": list(config.RECYCLE_MODES),
                "labels": dict(config.RECYCLE_MODE_LABELS),
                "trash_dir_name": config.TRASH_DIR_NAME,
            },
            # v0046：通知 / 计划扫描能力
            "notify": {
                "enabled": bool(self.services.settings.get().get("notify_enabled")),
                "channels": list(config.NOTIFY_CHANNELS),
                "events": list(config.NOTIFY_EVENTS),
                "event_labels": dict(config.NOTIFY_EVENT_LABELS),
            },
            "scheduler": {
                "tick_sec": config.SCHEDULE_TICK_SEC,
                "min_interval_sec": config.SCHEDULE_MIN_INTERVAL_SEC,
                "rules_max": config.SCHEDULE_RULES_MAX,
            },
            # v0028 目录批量字幕能力（docs/SUBTITLE_BATCH_CONTRACT.md）
            "subtitle": {
                "supported": self.services.subtitles is not None,
                "batch_max_items": config.SUBTITLE_BATCH_MAX_ITEMS,
                "batch_group_max_items": config.SUBTITLE_BATCH_GROUP_MAX_ITEMS,
                "enumerate_max_items": config.SUBTITLE_ENUMERATE_MAX_ITEMS,
                "output_modes": list(config.SUBTITLE_OUTPUT_MODES),
                "audio_sources": list(config.SUBTITLE_AUDIO_SOURCES),
                # v0034：字幕并发上限/缺省值（设置 subtitle_concurrency）
                "concurrency_max": config.SUBTITLE_CONCURRENCY_MAX,
                "default_concurrency": config.SUBTITLE_DEFAULT_CONCURRENCY,
            },
        })

    def _shares(self, request, session, params):
        force = _query_bool(request, "refresh", False)
        return json_response({
            "ok": True,
            "shares": self.services.browse.shares_payload(
                dsm_sid=getattr(session, "dsm_sid", None), force=force),
        })

    def _browse(self, request, session, params):
        path = request.query_last("path", "") or ""
        include_files = _query_bool(request, "include_files", False)
        result = self.services.browse.browse(
            path, include_files=include_files,
            dsm_sid=getattr(session, "dsm_sid", None))
        payload = {"ok": True}
        payload.update(result)
        return json_response(payload)

    # ------------------------------------------------------------------
    # 认证
    # ------------------------------------------------------------------
    def _login(self, request, session, params):
        body = _json_body(request)
        username = body.get("username")
        password = body.get("password")
        otp = body.get("otp")
        session = self.services.auth.login(
            username, password, otp=otp, client_ip=request.client_ip)
        from .auth import build_cookie
        cookie = build_cookie(session, secure=self.config.tls_enabled)
        return json_response({
            "ok": True,
            "user": session.user_dict(),
            "csrf_token": session.csrf_token,
            "expires_at": session.expires_iso(),
        }, cookies=[cookie])

    def _me(self, request, session, params):
        if session is None:
            raise auth_required()
        return json_response({
            "ok": True,
            "user": session.user_dict(),
            "csrf_token": session.csrf_token,
        })

    def _logout(self, request, session, params):
        from .auth import clear_cookie
        self.services.auth.logout(session.session_id)
        cookie = clear_cookie(secure=self.config.tls_enabled)
        return json_response({"ok": True}, cookies=[cookie])

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------
    def _get_settings(self, request, session, params):
        # 密钥永不回传明文，只给 has_*_key 标记
        current = public_settings(self.services.settings.get())
        return json_response({
            "ok": True,
            "settings": current,
            "defaults": public_settings(default_settings()),
        })

    def _put_settings(self, request, session, params):
        body = _json_body(request)
        patch = body.get("settings")
        if not isinstance(patch, dict):
            raise invalid(u"settings 必须是 JSON 对象")
        patch = dict(patch)
        # 允许清空密钥的标记放在顶层或 settings 内
        for alias in CLEAR_SECRET_ALIASES:
            if body.get(alias) is True:
                patch[alias] = True
        updated = self.services.settings.update(patch)
        # v0037：字幕并发数保存后立即生效（动态增减 worker，无需重启套件）。
        if "subtitle_concurrency" in patch:
            subtitles = getattr(self.services, "subtitles", None)
            if subtitles is not None:
                try:
                    subtitles.apply_settings(updated)
                except Exception:  # pragma: no cover - 保存成功优先
                    logger.exception("apply subtitle_concurrency failed")
        # 只记录键名，绝不记录任何值（含密钥）
        self.services.store.audit("settings_update", session.user,
                                  {"keys": sorted(str(key) for key in patch)})
        return json_response({"ok": True,
                              "settings": public_settings(updated)})

    # ------------------------------------------------------------------
    # 诊断 / 存储维护 / 删除任务历史（v0021 P0；仅管理员）
    # ------------------------------------------------------------------
    def _require_admin(self, session, message=u"仅管理员可以执行该操作"):
        if session is None or not getattr(session, "is_admin", False):
            raise forbidden(message)
        return session

    def _diagnostics(self, request, session, params):
        """只读运行诊断：DB/WAL、schema、行数、缓存、FFmpeg/ASR、活动任务。

        密钥只返回是否已设置（布尔），绝不返回任何密钥值。
        """
        self._require_admin(session, u"仅管理员可以查看系统诊断")
        store = self.services.store
        settings = self.services.settings.get()
        public = public_settings(settings)
        database = store.database_info()
        row_counts = store.table_row_counts()
        feature_entries = store.feature_cache_len()
        video_cache = video_similarity.video_cache_stats(self.config)
        # settings.ffmpeg_path 参与解析；设置路径变化会得到新的探测结果。
        ffmpeg_info = video_similarity.probe_ffmpeg(self.config, settings)
        hwaccel_mode = str(settings.get("video_hwaccel") or "auto").strip().lower()
        hwaccel_effective, _hwaccel_flag, hwaccel_fallback = (
            video_similarity.hwaccel_for_mode(
                hwaccel_mode, ffmpeg_info.get("hwaccels") or []))
        asr = video_similarity.asr_availability(
            settings, ffmpeg_info.get("available"))
        scan_counts = self.services.scanner.active_stats()
        subtitle_counts = {"active": 0, "total": 0}
        if self.services.subtitles is not None:
            try:
                subtitle_counts = self.services.subtitles.counts()
            except Exception:
                subtitle_counts = {"active": 0, "total": 0}
        return json_response({
            "ok": True,
            "app_version": config.APP_VERSION,
            "data_dir": self.config.data_dir,
            "database": database,
            "row_counts": row_counts,
            "feature_cache": {"entries": int(feature_entries)},
            # v0043：增量扫描缓存（哈希缓存/媒体探测缓存）
            "hash_cache": store.hash_cache_stats(),
            "media_probe_cache": {
                "entries": store.media_probe_cache_count(),
                "max_entries": config.MEDIA_PROBE_CACHE_MAX_ENTRIES,
            },
            # v0044：预览缩略图缓存
            "thumbnail_cache": (
                self.services.thumbnails.stats()
                if self.services.thumbnails is not None else None),
            "video_cache": video_cache,
            "ffmpeg": {
                "available": bool(ffmpeg_info.get("available")),
                "path": ffmpeg_info.get("path") or "",
                "version": ffmpeg_info.get("version") or "unavailable",
                "hwaccels": list(ffmpeg_info.get("hwaccels") or []),
                "gpu_hint": bool(ffmpeg_info.get("hwaccels")),
                # v0021 P1：外部 FFmpeg 配置路径/解析来源/实际硬解标签
                "configured_path": ffmpeg_info.get("configured_path") or "",
                "source": ffmpeg_info.get("source") or "unavailable",
                "hwaccel": hwaccel_effective,
                "hwaccel_mode": hwaccel_mode,
                "hwaccel_fallback": bool(hwaccel_fallback),
            },
            "asr": {
                "enabled": bool(asr.get("enabled")),
                "configured": bool(asr.get("configured")),
                "available": bool(asr.get("available")),
                "reason": asr.get("reason") or "",
                "weight": asr.get("weight"),
            },
            "active": {
                "scans": int(scan_counts.get("scans", 0) or 0),
                "paused_scans": int(scan_counts.get("paused_scans", 0) or 0),
                "subtitle_jobs": int(subtitle_counts.get("active", 0) or 0),
            },
            "keys": {
                "subtitle_asr_api_key_set": bool(
                    public.get("subtitle_asr_api_key_set")),
                "subtitle_llm_api_key_set": bool(
                    public.get("subtitle_llm_api_key_set")),
            },
        })

    def _storage_backup(self, request, session, params):
        """用 sqlite3 在线备份 API 生成 <var>/backups/ 下的独立备份（600）。"""
        self._require_admin(session, u"仅管理员可以备份数据库")
        try:
            backup = self.services.store.backup_database()
        except ApiError:
            raise
        except Exception:
            request_id = uuid.uuid4().hex[:16]
            logger.exception("数据库备份失败 [%s]", request_id)
            raise ApiError("INTERNAL_ERROR", u"数据库备份失败，请稍后重试",
                           details={"request_id": request_id})
        self.services.store.audit("storage_backup", session.user, {
            "filename": backup.get("filename"),
            "size": backup.get("size"),
        })
        return json_response({"ok": True, "backup": backup})

    def _storage_maintenance(self, request, session, params):
        """WAL checkpoint(TRUNCATE) + PRAGMA optimize，可选 VACUUM（需确认）。"""
        self._require_admin(session, u"仅管理员可以执行存储维护")
        body = _json_body(request)
        if body.get("confirm") is not True:
            raise invalid(u"存储维护需要 confirm=true 确认")
        vacuum = body.get("vacuum") is True
        try:
            result = self.services.store.maintenance(vacuum=vacuum)
        except ApiError:
            raise
        except Exception:
            request_id = uuid.uuid4().hex[:16]
            logger.exception("存储维护失败 [%s]", request_id)
            raise ApiError("INTERNAL_ERROR", u"存储维护失败，请稍后重试",
                           details={"request_id": request_id})
        self.services.store.audit("storage_maintenance", session.user, {
            "vacuum": vacuum,
            "reclaimed_bytes": result.get("reclaimed_bytes"),
        })
        return json_response({"ok": True, "maintenance": result})

    def _cache_stats(self, request, session, params):
        """增量扫描缓存状态（哈希缓存 + 媒体探测缓存；v0043 仅管理员）。"""
        self._require_admin(session, u"仅管理员可以查看缓存状态")
        store = self.services.store
        settings = self.services.settings.get()
        return json_response({
            "ok": True,
            "hash_cache": store.hash_cache_stats(),
            "hash_cache_max_entries": config.HASH_CACHE_MAX_ENTRIES,
            "media_probe_cache": {
                "entries": store.media_probe_cache_count(),
                "max_entries": config.MEDIA_PROBE_CACHE_MAX_ENTRIES,
            },
            "incremental_scan": bool(settings.get("incremental_scan", True)),
        })

    def _cache_clear(self, request, session, params):
        """清理增量扫描缓存（需 confirm=true；只删缓存，不动扫描结果）。"""
        self._require_admin(session, u"仅管理员可以清理缓存")
        body = _json_body(request)
        if body.get("confirm") is not True:
            raise invalid(u"清理缓存需要 confirm=true 确认")
        targets = body.get("targets")
        if not isinstance(targets, list) or not targets:
            targets = ["hash", "media"]
        allowed = {"hash", "media"}
        wanted = [item for item in targets if item in allowed]
        if not wanted:
            raise invalid(u"targets 只能是 hash / media")
        store = self.services.store
        cleared = {}
        if "hash" in wanted:
            store.hash_cache_clear()
            cleared["hash"] = True
        if "media" in wanted:
            store.media_probe_cache_clear()
            cleared["media"] = True
        store.audit("cache_clear", session.user, {"targets": wanted})
        return json_response({"ok": True, "cleared": cleared})

    _DELETION_JOB_FIELDS = (
        "job_id", "scan_id", "plan_id", "status", "total", "done", "failed",
        "skipped", "bytes_total", "bytes_done", "recycle", "started_at",
        "finished_at", "started_epoch", "finished_epoch",
    )

    @classmethod
    def _public_deletion_job(cls, job):
        """删除任务历史的脱敏视图：不含 results、文件路径或密钥。"""
        if not isinstance(job, dict):
            return {}
        data = {}
        for key in cls._DELETION_JOB_FIELDS:
            if key in job:
                data[key] = job.get(key)
        for key in ("total", "done", "failed", "skipped", "bytes_total",
                    "bytes_done"):
            try:
                data[key] = int(data.get(key) or 0)
            except (TypeError, ValueError):
                data[key] = 0
        data["files"] = data.get("total", 0)
        data["deleted"] = data.get("done", 0)
        data["bytes"] = data.get("bytes_total", 0)
        data["recycle"] = bool(data.get("recycle"))
        return data

    def _deletions(self, request, session, params):
        self._require_admin(session, u"仅管理员可以查看删除任务历史")
        limit = _query_int(request, "limit", 50)
        limit = max(1, min(int(limit), 500))
        jobs = self.services.deletion.list_jobs(limit=limit)
        public_jobs = [self._public_deletion_job(job) for job in jobs
                       if isinstance(job, dict)]
        return json_response({
            "ok": True,
            "limit": limit,
            "total": len(public_jobs),
            "jobs": public_jobs,
        })

    # ------------------------------------------------------------------
    # 云端字幕（仅管理员）
    # ------------------------------------------------------------------
    def _require_subtitle_admin(self, session):
        if session is None or not getattr(session, "is_admin", False):
            raise forbidden(u"仅管理员可以生成云端字幕")
        service = getattr(self.services, "subtitles", None)
        if service is None:
            raise ApiError("INTERNAL_ERROR", u"字幕服务不可用")
        return service

    def _create_subtitle(self, request, session, params):
        service = self._require_subtitle_admin(session)
        body = _json_body(request)
        try:
            job = service.submit(session.user, body)
        except SubtitleError as exc:
            raise ApiError(exc.code, exc.message, status=400)
        return json_response({"ok": True, "job": job}, status=202)

    def _get_subtitle(self, request, session, params):
        service = self._require_subtitle_admin(session)
        return json_response({"ok": True,
                              "job": service.get(params["job_id"])})

    def _list_subtitles(self, request, session, params):
        service = self._require_subtitle_admin(session)
        limit = _query_int(request, "limit", 50)
        offset = _query_int(request, "offset", 0)
        batch_id = request.query_last("batch_id") or None
        group_id = request.query_last("group_id") or None
        # v0034：状态过滤（all/active/queued/running/...；非法值由 service 抛 400）
        status = request.query_last("status") or None
        if group_id is not None or offset:
            page = service.list_page(limit=limit, batch_id=batch_id,
                                     group_id=group_id, offset=offset,
                                     status=status)
            payload = {"ok": True}
            payload.update(page)
            return json_response(payload)
        # 旧分支保持 {ok, jobs} 形状（offset=0 且无 group_id 时前端依赖）
        return json_response({"ok": True,
                              "jobs": service.list(limit=limit,
                                                   batch_id=batch_id,
                                                   status=status)})

    # ------------------------------------------------------------------
    # v0039：任务中心队列视图 / 批次组总览（只读；管理员）
    # ------------------------------------------------------------------
    def _subtitle_queue(self, request, session, params):
        service = self._require_subtitle_admin(session)
        snapshot = service.queue_snapshot(
            active_limit=_query_int(request, "active_limit", 10),
            queued_limit=_query_int(request, "queued_limit", 20),
            paused_limit=_query_int(request, "paused_limit", 10),
            recent_limit=_query_int(request, "recent_limit", 10))
        payload = {"ok": True}
        payload.update(snapshot)
        return json_response(payload)

    def _subtitle_group_overview(self, request, session, params):
        service = self._require_subtitle_admin(session)
        snapshot = service.batch_group_overview(
            active_limit=_query_int(request, "active_limit", 10),
            completed_limit=_query_int(request, "completed_limit", 3))
        payload = {"ok": True}
        payload.update(snapshot)
        return json_response(payload)

    def _subtitle_control(self, request, session, params):
        service = self._require_subtitle_admin(session)
        action = params["action"]
        if action == "pause":
            job = service.pause(params["job_id"])
        else:
            job = service.resume(params["job_id"])
        return json_response({"ok": True, "job": job})

    def _cancel_subtitle(self, request, session, params):
        service = self._require_subtitle_admin(session)
        return json_response({"ok": True,
                              "job": service.cancel(params["job_id"])})

    # ------------------------------------------------------------------
    # v0035 补翻译（仅管理员；docs/SUBTITLE_BATCH_CONTRACT.md §15）
    # ------------------------------------------------------------------
    def _subtitle_translate(self, request, session, params):
        service = self._require_subtitle_admin(session)
        body = _json_body(request)
        try:
            result = service.translate_job(session.user, params["job_id"], body)
        except SubtitleError as exc:
            raise ApiError(exc.code, exc.message, status=400)
        payload = {"ok": True}
        payload.update(result)
        # 新建 202；同源已有排队/执行中的翻译任务 → 200 幂等
        return json_response(payload,
                             status=202 if result.get("created") else 200)

    def _subtitle_file(self, request, session, params):
        service = self._require_subtitle_admin(session)
        path = service.get_file(params["job_id"], params["name"])
        return Response(
            status=200,
            content_type="text/plain; charset=utf-8",
            file_path=path,
            file_size=os.path.getsize(path),
            headers={
                "Content-Disposition": 'attachment; filename="%s"' % params["name"],
                "Cache-Control": "no-store",
            },
        )

    # ------------------------------------------------------------------
    # v0026 云端字幕探测（仅管理员；错误码映射见 errors.ERROR_STATUS）
    # ------------------------------------------------------------------
    def _subtitle_models(self, request, session, params):
        service = self._require_subtitle_admin(session)
        body = _json_body(request, required=False)
        try:
            result = service.list_models(body.get("kind"), body)
        except SubtitleError as exc:
            # 默认 ApiError 按 ERROR_STATUS 映射（PROVIDER_AUTH_FAILED=400 等，
            # 不使用 401，避免前端误判会话失效）。
            raise ApiError(exc.code, exc.message)
        payload = {"ok": True}
        payload.update(result)
        return json_response(payload)

    def _subtitle_test(self, request, session, params):
        service = self._require_subtitle_admin(session)
        body = _json_body(request, required=False)
        try:
            result = service.test_connection(body.get("kind"), body)
        except SubtitleError as exc:
            raise ApiError(exc.code, exc.message)
        payload = {"ok": True}
        payload.update(result)
        return json_response(payload)

    # ------------------------------------------------------------------
    # v0028 目录批量字幕（仅管理员；docs/SUBTITLE_BATCH_CONTRACT.md）
    # ------------------------------------------------------------------
    def _subtitle_enumerate(self, request, session, params):
        service = self._require_subtitle_admin(session)
        body = _json_body(request)
        recursive = body.get("recursive", True)
        return json_response(service.enumerate_videos(
            body.get("path"), recursive=recursive,
            max_items=body.get("max_items")))

    def _subtitle_batch_create(self, request, session, params):
        service = self._require_subtitle_admin(session)
        body = _json_body(request)
        try:
            batch = service.create_batch(session.user, body)
        except SubtitleError as exc:
            raise ApiError(exc.code, exc.message, status=400)
        return json_response({"ok": True, "batch": batch}, status=202)

    def _subtitle_batch_get(self, request, session, params):
        service = self._require_subtitle_admin(session)
        return json_response({"ok": True,
                              "batch": service.get_batch(params["batch_id"])})

    def _subtitle_batch_control(self, request, session, params):
        service = self._require_subtitle_admin(session)
        action = params["action"]
        handlers = {
            "pause": service.pause_batch,
            "resume": service.resume_batch,
            "cancel": service.cancel_batch,
            "retry": service.retry_batch,
        }
        handler = handlers.get(action)
        if handler is None:
            raise invalid(u"未知的批次操作")
        return json_response({"ok": True,
                              "batch": handler(params["batch_id"])})

    # ------------------------------------------------------------------
    # v0032：字幕批次组（一次提交自动拆批；docs/SUBTITLE_BATCH_CONTRACT.md §13）
    # ------------------------------------------------------------------
    def _subtitle_group_create(self, request, session, params):
        service = self._require_subtitle_admin(session)
        body = _json_body(request)
        try:
            group = service.create_batch_group(session.user, body)
        except SubtitleError as exc:
            raise ApiError(exc.code, exc.message, status=400)
        return json_response({"ok": True, "group": group}, status=202)

    def _subtitle_group_get(self, request, session, params):
        service = self._require_subtitle_admin(session)
        return json_response({"ok": True,
                              "group": service.get_batch_group(
                                  params["group_id"])})

    def _subtitle_group_translate(self, request, session, params):
        service = self._require_subtitle_admin(session)
        body = _json_body(request)
        try:
            result = service.translate_batch_group(
                session.user, params["group_id"], body)
        except SubtitleError as exc:
            raise ApiError(exc.code, exc.message, status=400)
        payload = {"ok": True}
        payload.update(result)
        # 无可翻译任务时仍返回 202（created=0，前端据 summary 提示）
        return json_response(payload, status=202)

    def _subtitle_group_control(self, request, session, params):
        service = self._require_subtitle_admin(session)
        action = params["action"]
        handlers = {
            "pause": service.pause_batch_group,
            "resume": service.resume_batch_group,
            "cancel": service.cancel_batch_group,
            "retry": service.retry_batch_group,
        }
        handler = handlers.get(action)
        if handler is None:
            raise invalid(u"未知的批次组操作")
        return json_response({"ok": True,
                              "group": handler(params["group_id"])})

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------
    def _create_scan(self, request, session, params):
        body = _json_body(request)
        roots = body.get("roots")
        excludes = body.get("excludes") or []
        options = body.get("options") or {}
        if not isinstance(options, dict):
            raise invalid(u"options 必须是 JSON 对象")
        scan_id = self.services.scanner.start_scan(
            roots, excludes, options, session.user)
        return json_response({"ok": True, "scan_id": scan_id}, status=202)

    def _list_scans(self, request, session, params):
        limit = _query_int(request, "limit", 50)
        return json_response({
            "ok": True,
            "scans": self.services.scanner.list_scans(limit=limit),
        })

    def _get_scan(self, request, session, params):
        return json_response({
            "ok": True,
            "scan": self.services.scanner.get_scan(params["scan_id"]),
        })

    def _scan_control(self, request, session, params):
        scan_id = params["scan_id"]
        action = params["action"]
        method = getattr(self.services.scanner, action)
        return json_response({"ok": True, "scan": method(scan_id)})

    def _scan_groups(self, request, session, params):
        page = self.services.scanner.groups_page(
            params["scan_id"],
            offset=_query_int(request, "offset", 0),
            limit=_query_int(request, "limit", 100),
            q=request.query_last("q", "") or "",
            sort=request.query_last("sort", "wasted_desc") or "wasted_desc",
            match_type=request.query_last("match_type", None) or None,
            new_only=_query_bool(request, "new_only", False))
        payload = {"ok": True}
        payload.update(page)
        return json_response(payload)

    def _scan_stats(self, request, session, params):
        result = self.services.scanner.stats_full(params["scan_id"])
        payload = {"ok": True}
        payload.update(result)
        return json_response(payload)

    def _scan_csv(self, request, session, params):
        scan_id = params["scan_id"]
        path = self.services.scanner.write_csv(scan_id)
        filename = self.services.scanner.filename_for_csv(scan_id)
        return Response(
            status=200,
            content_type="text/csv; charset=utf-8",
            file_path=path,
            file_size=os.path.getsize(path),
            headers={
                "Content-Disposition": 'attachment; filename="%s"' % filename,
                "Cache-Control": "no-store",
            },
            delete_after=True,
        )

    def _scan_xlsx(self, request, session, params):
        scan_id = params["scan_id"]
        path = self.services.scanner.write_xlsx(scan_id)
        filename = self.services.scanner.filename_for_xlsx(scan_id)
        return Response(
            status=200,
            content_type=("application/vnd.openxmlformats-officedocument"
                          ".spreadsheetml.sheet"),
            file_path=path,
            file_size=os.path.getsize(path),
            headers={
                "Content-Disposition": 'attachment; filename="%s"' % filename,
                "Cache-Control": "no-store",
            },
            delete_after=True,
        )

    def _delete_plan(self, request, session, params):
        body = _json_body(request)
        plan = self.services.deletion.create_plan(session, params["scan_id"], body)
        return json_response({"ok": True, "plan": plan})

    def _delete(self, request, session, params):
        body = _json_body(request)
        job_id = self.services.deletion.start_delete(
            session, params["scan_id"], body)
        return json_response({"ok": True, "job_id": job_id}, status=202)

    def _delete_job(self, request, session, params):
        job = self.services.deletion.get_job(params["scan_id"], params["job_id"])
        return json_response({"ok": True, "job": job})

    # ------------------------------------------------------------------
    # v0045：硬链接合并 / 套件独立回收站
    # ------------------------------------------------------------------
    def _merge_plan(self, request, session, params):
        body = _json_body(request)
        plan = self.services.merges.create_plan(
            session, params["scan_id"], body)
        return json_response({"ok": True, "plan": plan})

    def _merge_start(self, request, session, params):
        body = _json_body(request)
        job_id = self.services.merges.start_merge(
            session, params["scan_id"], body)
        return json_response({"ok": True, "job_id": job_id}, status=202)

    def _merge_job(self, request, session, params):
        job = self.services.merges.get_job(params["scan_id"], params["job_id"])
        return json_response({"ok": True, "job": job})

    def _merges(self, request, session, params):
        scan_id = request.query_last("scan_id", None) or None
        limit = _query_int(request, "limit", 50)
        return json_response({"ok": True,
                              "merges": self.services.merges.list_jobs(
                                  scan_id=scan_id, limit=limit)})

    def _merge_restore(self, request, session, params):
        body = _json_body(request)
        result = self.services.merges.restore(
            session, params["job_id"], body)
        payload = {"ok": True}
        payload.update(result)
        return json_response(payload)

    def _merge_detail(self, request, session, params):
        detail = self.services.merges.detail(params["job_id"])
        payload = {"ok": True}
        payload.update(detail)
        return json_response(payload)

    def _trash_list(self, request, session, params):
        limit = _query_int(request, "limit", 100)
        settings = self.services.settings.get() or {}
        mode = settings.get("delete_recycle_mode") or config.RECYCLE_MODE_DEFAULT
        return json_response({
            "ok": True,
            "trash": self.services.trash.list_jobs(limit=limit),
            "recycle_mode": mode,
            "recycle_modes": list(config.RECYCLE_MODES),
            "recycle_mode_labels": dict(config.RECYCLE_MODE_LABELS),
            "trash_dir_name": config.TRASH_DIR_NAME,
        })

    def _trash_detail(self, request, session, params):
        detail = self.services.trash.detail(params["trash_id"])
        payload = {"ok": True}
        payload.update(detail)
        return json_response(payload)

    def _trash_restore(self, request, session, params):
        body = _json_body(request)
        result = self.services.trash.restore(
            session, params["trash_id"], body)
        payload = {"ok": True}
        payload.update(result)
        return json_response(payload)

    def _trash_purge(self, request, session, params):
        body = _json_body(request)
        result = self.services.trash.purge(
            session, params["trash_id"], body)
        payload = {"ok": True}
        payload.update(result)
        return json_response(payload)

    # ------------------------------------------------------------------
    # v0046：通知系统 / 计划扫描
    # ------------------------------------------------------------------
    def _notifications(self, request, session, params):
        return json_response({"ok": True,
                              "notify": self.services.notify.status()})

    def _notification_test(self, request, session, params):
        body = _json_body(request)
        channel = body.get("channel") if isinstance(body, dict) else None
        if not isinstance(channel, str) or not channel:
            raise invalid(u"必须提供 channel（dsm/webhook/telegram）")
        if channel not in config.NOTIFY_CHANNELS:
            raise invalid(u"未知渠道: %s" % channel)
        return json_response({"ok": True,
                              "result": self.services.notify.test_channel(channel)})

    def _scheduler(self, request, session, params):
        return json_response({"ok": True,
                              "scheduler": self.services.scheduler.status()})

    def _scheduler_run_now(self, request, session, params):
        result = self.services.scheduler.run_now(user=session.user)
        status = 202 if result.get("ok") else 200
        return json_response({"ok": bool(result.get("ok")), "result": result},
                             status=status)

    # ------------------------------------------------------------------
    # 预览 / 日志
    # ------------------------------------------------------------------
    def _preview(self, request, session, params):
        path = request.query_last("path", "")
        info = self.services.preview.resolve(path)
        if info["kind"] == "text":
            data, truncated = self.services.preview.read_text(info)
            headers = {"Cache-Control": "no-store", "Accept-Ranges": "bytes"}
            if truncated:
                headers["X-Preview-Truncated"] = "1"
            size = len(data)
            range_header = request.headers.get("range")
            parsed = parse_range_header(range_header, size) if range_header else None
            if parsed == "unsatisfiable":
                headers["Content-Range"] = "bytes */%d" % size
                return Response(status=416, body=b"",
                                content_type=info["content_type"],
                                headers=headers)
            if parsed is not None:
                start, end = parsed
                headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
                return Response(status=206, body=data[start:end + 1],
                                content_type=info["content_type"],
                                headers=headers)
            return Response(status=200, body=data,
                            content_type=info["content_type"], headers=headers)
        return Response(
            status=200,
            content_type=info["content_type"],
            file_path=info["path"],
            file_size=info["size"],
            allow_range=True,
            headers={"Cache-Control": "no-store"},
        )

    # ------------------------------------------------------------------
    # v0044：预览增强（缩略图 / 媒体信息）
    # ------------------------------------------------------------------
    def _preview_thumb(self, request, session, params):
        path = request.query_last("path", "")
        if self.services.thumbnails is None:
            raise ApiError("PREVIEW_UNSUPPORTED", u"当前后端未提供缩略图服务")
        info = self.services.preview.resolve(path)
        if info["kind"] != "video":
            raise ApiError("PREVIEW_UNSUPPORTED", u"缩略图仅支持视频文件")
        timestamp = _query_float(request, "t", 0.0)
        width = _query_int(request, "w", config.THUMB_DEFAULT_WIDTH)
        duration = None
        try:
            media = self.services.thumbnails.media_info(info["path"])
            duration = media.get("duration")
        except ApiError:
            duration = None
        thumbnail = self.services.thumbnails.thumbnail(
            info["path"], timestamp=timestamp, width=width, duration=duration)
        try:
            size = os.path.getsize(thumbnail["path"])
        except OSError:
            raise ApiError("PICTURE_FAILED", u"缩略图文件不可读")
        return Response(
            status=200,
            content_type="image/jpeg",
            file_path=thumbnail["path"],
            file_size=size,
            headers={
                "Cache-Control": "private, max-age=3600",
                "X-Thumb-Cache": "hit" if thumbnail.get("cached") else "miss",
                "X-Thumb-Timestamp": "%.3f" % float(thumbnail.get("timestamp") or 0),
            },
        )

    def _preview_media(self, request, session, params):
        path = request.query_last("path", "")
        if self.services.thumbnails is None:
            raise ApiError("PREVIEW_UNSUPPORTED", u"当前后端未提供媒体信息服务")
        info = self.services.preview.resolve(path)
        if info["kind"] not in ("video", "audio"):
            raise ApiError("PREVIEW_UNSUPPORTED", u"该文件类型不支持媒体信息")
        media = self.services.thumbnails.media_info(info["path"])
        return json_response({
            "ok": True,
            "path": info["path"],
            "name": info["name"],
            "size": info["size"],
            "media": media,
        })

    def _preview_thumbs(self, request, session, params):
        path = request.query_last("path", "")
        if self.services.thumbnails is None:
            raise ApiError("PREVIEW_UNSUPPORTED", u"当前后端未提供缩略图服务")
        info = self.services.preview.resolve(path)
        if info["kind"] not in ("video", "audio"):
            raise ApiError("PREVIEW_UNSUPPORTED", u"该文件类型不支持缩略图清单")
        count = _query_int(request, "count", config.THUMB_COUNT_DEFAULT)
        width = _query_int(request, "w", config.THUMB_DEFAULT_WIDTH)
        plan = self.services.thumbnails.preview_plan(
            info["path"], count=count, width=width)
        payload = {"ok": True, "path": info["path"], "name": info["name"],
                   "size": info["size"]}
        payload.update(plan)
        return json_response(payload)

    def _logs(self, request, session, params):
        limit = _query_int(request, "limit", 50)
        limit = max(1, min(int(limit), 500))
        logs = self.services.store.list_logs(limit=limit)
        audit = self.services.store.read_audit(limit=limit)
        return json_response({"ok": True, "logs": logs, "audit": audit})

    def _logs_download(self, request, session, params):
        name = request.query_last("name", "")
        if not name:
            raise invalid(u"缺少日志文件名")
        path = self.services.store.resolve_log(name)
        return Response(
            status=200,
            content_type="application/octet-stream",
            file_path=path,
            file_size=os.path.getsize(path),
            headers={
                "Content-Disposition": 'attachment; filename="%s"' % name,
                "Cache-Control": "no-store",
            },
        )

    # ------------------------------------------------------------------
    # v0038：操作日志删除（管理员 + CSRF）
    # ------------------------------------------------------------------
    def _delete_audit(self, request, session, params):
        self._require_admin(session)
        body = _json_body(request)
        ids = body.get("ids")
        keep_days = body.get("keep_days")
        if isinstance(ids, list) and ids:
            deleted = self.services.store.delete_audit_entries(
                session.user, ids=ids)
        elif keep_days is not None:
            days = _keep_days_param(keep_days)
            deleted = self.services.store.delete_audit_entries(
                session.user,
                before_epoch=time.time() - days * 86400,
                keep_days=days)
        else:
            raise invalid(u"需要 ids 或 keep_days 参数")
        return json_response({"ok": True, "deleted": int(deleted)})

    def _delete_log_files(self, request, session, params):
        self._require_admin(session)
        body = _json_body(request)
        names = body.get("names")
        keep_days = body.get("keep_days")
        if isinstance(names, list) and names:
            deleted = self.services.store.delete_log_files(
                session.user, names=names)
        elif keep_days is not None:
            days = _keep_days_param(keep_days)
            deleted = self.services.store.delete_log_files(
                session.user,
                before_epoch=time.time() - days * 86400,
                keep_days=days)
        else:
            raise invalid(u"需要 names 或 keep_days 参数")
        return json_response({"ok": True, "deleted": int(deleted)})


def _keep_days_param(value):
    """校验“保留最近 N 天”参数：整数 1..3650。"""
    if isinstance(value, bool):
        raise invalid(u"keep_days 必须是整数", details={"key": "keep_days"})
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise invalid(u"keep_days 必须是整数", details={"key": "keep_days"})
    if days < 1 or days > 3650:
        raise invalid(u"keep_days 超出范围 1..3650",
                      details={"key": "keep_days"})
    return days


def _is_loopback(host):
    host = (host or "").strip().lower()
    return host in ("127.0.0.1", "localhost", "::1")
