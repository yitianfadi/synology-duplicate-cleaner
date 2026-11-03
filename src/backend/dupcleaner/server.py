# -*- coding: utf-8 -*-
"""HTTP(S) 服务器：REST API + 静态文件（同源，无 CORS）。"""

from __future__ import absolute_import

import logging
import mimetypes
import os
import re
import ssl
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, fsutil, timeutil
from .httpapi import Request, Response, parse_range_header

logger = logging.getLogger("dupcleaner.server")

COMMON_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "SAMEORIGIN",
}

CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self'; "
       "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'")


class DcHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, *args, **kwargs):
        ThreadingHTTPServer.__init__(self, *args, **kwargs)
        self.api = None
        self.web_dir = None
        self.runtime_config = None
        self.services = None


class DcRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DuplicateCleaner/" + config.APP_VERSION
    sys_version = ""

    # 不使用 BaseHTTPRequestHandler 的默认 stderr 日志格式
    def log_message(self, fmt, *args):
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    # ------------------------------------------------------------------
    def _handle(self):
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._send_simple(400, u"请求路径非法")
            return
        path = urllib.parse.unquote(parsed.path or "/")
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        headers = {}
        for key, value in self.headers.items():
            headers[key.lower()] = value
        try:
            body = self._read_body()
        except ValueError as exc:
            self._send_simple(413, str(exc))
            return
        scheme = "https" if isinstance(self.connection, ssl.SSLSocket) else "http"
        request = Request(
            method=self.command,
            path=path,
            query=query,
            headers=headers,
            body=body,
            client_ip=self.client_address[0] if self.client_address else "",
            scheme=scheme,
        )
        if path == "/api" or path.startswith("/api/"):
            response = self.server.api.dispatch(request)
        else:
            response = self._static_response(path)
        self._send_response(response)

    def _read_body(self):
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            return b""
        try:
            length = int(length_header)
        except (TypeError, ValueError):
            raise ValueError(u"Content-Length 非法")
        if length < 0:
            raise ValueError(u"Content-Length 非法")
        if length > config.MAX_BODY_BYTES:
            raise ValueError(u"请求体过大")
        if length == 0:
            return b""
        return self.rfile.read(length)

    # ------------------------------------------------------------------
    def _send_simple(self, status, message):
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in COMMON_SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_response(self, response):
        if response is None:
            response = self._static_response("/")
        if response.file_path:
            self._send_file(response)
            return
        body = response.body or b""
        headers = dict(response.headers)
        headers.setdefault("Content-Type", response.content_type)
        headers["Content-Length"] = str(len(body))
        headers["Cache-Control"] = headers.get("Cache-Control", "no-store")
        for key, value in COMMON_SECURITY_HEADERS.items():
            headers.setdefault(key, value)
        self.send_response(response.status)
        for key, value in headers.items():
            self.send_header(key, value)
        for cookie in response.cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_file(self, response):
        path = response.file_path
        try:
            size = response.file_size
            if size is None:
                size = os.path.getsize(path)
        except OSError:
            self._send_simple(404, u"文件不存在")
            self._cleanup_file(response)
            return
        range_header = self.headers.get("Range")

        start = 0
        end = size - 1
        status = 200
        content_range = None
        if response.allow_range and range_header:
            parsed = parse_range_header(range_header, size)
            if parsed == "unsatisfiable":
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                for key, value in COMMON_SECURITY_HEADERS.items():
                    self.send_header(key, value)
                self.end_headers()
                self._cleanup_file(response)
                return
            if parsed is not None:
                start, end = parsed
                status = 206
                content_range = "bytes %d-%d/%d" % (start, end, size)

        length = max(0, end - start + 1) if size > 0 else 0
        headers = dict(response.headers)
        headers.setdefault("Content-Type", response.content_type)
        headers["Content-Length"] = str(length)
        headers.setdefault("Cache-Control", "no-store")
        headers["Accept-Ranges"] = "bytes"
        if content_range:
            headers["Content-Range"] = content_range
        for key, value in COMMON_SECURITY_HEADERS.items():
            headers.setdefault(key, value)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        for cookie in response.cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command == "HEAD":
            self._cleanup_file(response)
            return
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self._cleanup_file(response)
            try:
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    @staticmethod
    def _cleanup_file(response):
        if response.delete_after and response.file_path:
            try:
                os.unlink(response.file_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    def _static_response(self, path):
        web_dir = self.server.web_dir
        if not web_dir or not os.path.isdir(web_dir):
            return Response(status=404, body=u"静态资源未安装".encode("utf-8"),
                            content_type="text/plain; charset=utf-8")
        decoded = path or "/"
        candidate = os.path.normpath(os.path.join(web_dir, decoded.lstrip("/")))
        if not fsutil.is_under(candidate, web_dir):
            return Response(status=404, body=u"未找到".encode("utf-8"),
                            content_type="text/plain; charset=utf-8")
        if os.path.isdir(candidate):
            candidate = os.path.join(candidate, "index.html")
        index_path = os.path.join(web_dir, "index.html")
        if not os.path.isfile(candidate):
            base = os.path.basename(decoded.rstrip("/")) or ""
            is_spa_route = base and "." not in base
            if is_spa_route and os.path.isfile(index_path):
                candidate = index_path
            else:
                return Response(status=404, body=u"未找到".encode("utf-8"),
                                content_type="text/plain; charset=utf-8")
        content_type = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
                "application/javascript", "application/json",
                "image/svg+xml", "application/xml"):
            if "charset" not in content_type:
                content_type += "; charset=utf-8"
        headers = {}
        if os.path.basename(candidate) == "index.html":
            headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            headers["Content-Security-Policy"] = CSP
        elif os.path.splitext(candidate)[1].lower() in (".js", ".css"):
            # ES modules 拆分后，升级时必须让浏览器重新校验 js/css，避免缓存旧模块。
            headers["Cache-Control"] = "no-cache, must-revalidate"
        else:
            headers["Cache-Control"] = "public, max-age=3600"
        return Response(
            status=200,
            content_type=content_type,
            file_path=candidate,
            file_size=os.path.getsize(candidate),
            allow_range=True,
            headers=headers,
        )


