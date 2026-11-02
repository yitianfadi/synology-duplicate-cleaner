# -*- coding: utf-8 -*-
"""REST API 路由与处理（契约 §3–§8）。"""

from __future__ import absolute_import

import json
import logging
import os
import platform
import re
import urllib.parse

from . import config, similarity, timeutil, video_similarity
from .errors import ApiError, forbidden, invalid, not_found, auth_required
from .settings import default_settings

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
            ("GET", re.compile(r"^/api/preview$"), self._preview, False),
            ("GET", re.compile(r"^/api/logs$"), self._logs, False),
            ("GET", re.compile(r"^/api/logs/download$"), self._logs_download, False),
        ]

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------
    def dispatch(self, request):
        try:
            return self._dispatch(request)
        except ApiError as exc:
            return error_response(exc)
        except Exception as exc:  # pragma: no cover - 防御性兜底
            logger.exception("API 处理异常: %s %s", request.method, request.path)
            return error_response(ApiError("INTERNAL_ERROR",
                                           u"服务器内部错误: %s" % exc,
                                           details={}))

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
            "defaults": default_settings(),
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
        return json_response({
            "ok": True,
            "settings": self.services.settings.get(),
            "defaults": default_settings(),
        })

    def _put_settings(self, request, session, params):
        body = _json_body(request)
        patch = body.get("settings")
        if not isinstance(patch, dict):
            raise invalid(u"settings 必须是 JSON 对象")
        updated = self.services.settings.update(patch)
        self.services.store.audit("settings_update", session.user,
                                  {"keys": sorted(patch.keys())})
        return json_response({"ok": True, "settings": updated})

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
            match_type=request.query_last("match_type", None) or None)
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


def _is_loopback(host):
    host = (host or "").strip().lower()
    return host in ("127.0.0.1", "localhost", "::1")
