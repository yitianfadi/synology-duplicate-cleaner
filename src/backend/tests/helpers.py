# -*- coding: utf-8 -*-
"""测试辅助：临时目录、dev 模式服务、注入式创建时间、HTTP 客户端。"""

from __future__ import absolute_import

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from dupcleaner import config, timeutil  # noqa: E402
from dupcleaner.services import build_services  # noqa: E402


def write_bytes(path, data):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def write_text(path, text):
    return write_bytes(path, text.encode("utf-8"))


def make_web_dir(parent):
    web_dir = os.path.join(parent, "web")
    os.makedirs(web_dir, exist_ok=True)
    write_text(os.path.join(web_dir, "index.html"),
               u"<!DOCTYPE html><html><body>DC-TEST-INDEX</body></html>")
    write_text(os.path.join(web_dir, "app.js"), u"window.__DC_TEST__ = true;\n")
    return web_dir


class Harness(object):
    """一个隔离的后端实例（dev 模式，临时目录）。"""

    def __init__(self, dev_roots=None):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-test-")
        self.dev_root = os.path.join(self.tmpdir, "share")
        os.makedirs(self.dev_root, exist_ok=True)
        roots = list(dev_roots) if dev_roots else [self.dev_root]
        self.web_dir = make_web_dir(self.tmpdir)
        self.data_dir = os.path.join(self.tmpdir, "data")
        self.config = config.RuntimeConfig(
            data_dir=self.data_dir,
            web_dir=self.web_dir,
            dev_mode=True,
            dev_roots=roots,
            dev_password="devpass",
            environ={},
        )
        self.services = build_services(self.config)
        self.creation_times = {}
        self.services.scanner._time_reader = self._time_reader

    # -- 创建时间注入 ---------------------------------------------------
    def set_creation(self, path, ts, ns=0, source="statx"):
        key = os.path.normcase(os.path.abspath(path))
        self.creation_times[key] = {
            "created_ts": None if ts is None else int(ts),
            "created_ns": int(ns),
            "created_source": source,
            "created_unsafe": source in ("ctime", "unknown") or ts is None,
        }

    def set_all_creation(self, paths, base=1000, step=100, source="statx"):
        for index, path in enumerate(paths):
            self.set_creation(path, base + index * step, source=source)

    def _time_reader(self, path, allow_ctime_fallback=False):
        key = os.path.normcase(os.path.abspath(path))
        mapped = self.creation_times.get(key)
        if mapped is not None:
            return dict(mapped)
        real = timeutil.read_creation_time(path, allow_ctime_fallback=False)
        if real.get("created_ts") is None and allow_ctime_fallback:
            real = timeutil.read_creation_time(path, allow_ctime_fallback=True)
        return real

    # -- 会话 / 扫描 ----------------------------------------------------
    def login(self):
        return self.services.auth.login("admin", "devpass", client_ip="127.0.0.1")

    def run_scan(self, roots=None, options=None, excludes=None, timeout=30.0):
        roots = roots or [self.dev_root]
        scan_id = self.services.scanner.start_scan(
            roots, excludes or [], options or {}, "admin")
        meta = self.wait_scan(scan_id, timeout=timeout)
        return scan_id, meta

    def wait_scan(self, scan_id, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            meta = self.services.scanner.get_scan(scan_id)
            if meta.get("status") in ("completed", "failed", "stopped"):
                return meta
            time.sleep(0.02)
        raise AssertionError("scan %s did not finish in %.1fs" % (scan_id, timeout))

    def groups(self, scan_id):
        return list(self.services.store.iter_scan_groups(scan_id))

    def group_by_paths(self, scan_id, name):
        for group in self.groups(scan_id):
            for file_info in group["files"]:
                if file_info["name"] == name:
                    return group
        raise AssertionError("group containing %s not found" % name)

    def cleanup(self):
        try:
            self.services.shutdown()
        finally:
            shutil.rmtree(self.tmpdir, ignore_errors=True)


class HttpClient(object):
    def __init__(self, host, port, timeout=30.0):
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self.cookies = {}
        self.conn = None

    def _connection(self):
        if self.conn is None:
            self.conn = http.client.HTTPConnection(self.host, self.port,
                                                   timeout=self.timeout)
        return self.conn

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def request(self, method, path, body=None, headers=None, csrf=None,
                use_cookie=True):
        request_headers = {"Accept": "application/json"}
        if body is not None and not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if use_cookie and self.cookies.get("dc_session"):
            request_headers["Cookie"] = "dc_session=" + self.cookies["dc_session"]
        if csrf:
            request_headers["X-CSRF-Token"] = csrf
        if headers:
            request_headers.update(headers)
        connection = self._connection()
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        response_headers = {}
        for key, value in response.getheaders():
            response_headers.setdefault(key.lower(), []).append(value)
        for cookie in response_headers.get("set-cookie", []):
            if cookie.startswith("dc_session="):
                value = cookie.split(";", 1)[0].split("=", 1)[1]
                if value:
                    self.cookies["dc_session"] = value
                else:
                    self.cookies.pop("dc_session", None)
        return response.status, response_headers, data

    def json(self, method, path, body=None, headers=None, csrf=None,
             use_cookie=True):
        status, response_headers, data = self.request(
            method, path, body=body, headers=headers, csrf=csrf,
            use_cookie=use_cookie)
        try:
            payload = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeError):
            payload = None
        return status, response_headers, payload

    def login(self, username="admin", password="devpass", otp=None):
        payload = {"username": username, "password": password}
        if otp:
            payload["otp"] = otp
        status, headers, data = self.json("POST", "/api/auth/login", payload)
        if status != 200:
            raise AssertionError("login failed: %s %s" % (status, data))
        return data["csrf_token"]


class WebServer(object):
    """在后台线程启动真实 HTTP 服务，测试结束 shutdown。"""

    def __init__(self, harness):
        from dupcleaner.httpapi import Api
        from dupcleaner.server import DcHTTPServer, DcRequestHandler
        self.harness = harness
        self.api = Api(harness.services)
        self.httpd = DcHTTPServer(("127.0.0.1", 0), DcRequestHandler)
        self.httpd.api = self.api
        self.httpd.web_dir = harness.config.web_dir
        self.httpd.runtime_config = harness.config
        self.httpd.services = harness.services
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.client = HttpClient("127.0.0.1", self.port)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.client.close()
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
            self.thread.join(timeout=5)


def find_free_path(root, name):
    return os.path.join(root, name)
