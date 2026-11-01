# -*- coding: utf-8 -*-
"""认证：DSM SYNO.API.Auth 登录 + 管理员校验 + 会话 / CSRF。

群晖 API 调用点（重要）：
- SYNO.API.Auth（version 6/3，method=login/logout）用于校验 DSM 账号密码，
  只通过 127.0.0.1:5001(https) / 127.0.0.1:5000(http) 回环访问本机 DSM。
- SYNO.Core.CurrentUser / SYNO.Core.User 用于判断管理员身份。
- 管理员判定还做本地兜底：synogroup --member administrators、id -Gn。
- 密码只在内存中使用，绝不写磁盘、绝不写日志。
"""

from __future__ import absolute_import

import hmac
import json
import os
import re
import secrets
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .errors import ApiError, auth_failed, auth_required, forbidden, invalid
from .timeutil import iso_local

DSM_AUTH_ERRORS = {
    400: u"账号或密码错误",
    401: u"该账号已被停用",
    402: u"该账号没有权限登录",
    403: u"需要两步验证码（OTP）",
    404: u"两步验证码错误或已过期",
    407: u"账号已被封锁，请稍后重试",
    408: u"密码已过期，请先在 DSM 修改密码",
    409: u"密码必须修改后才能登录",
}

# 登录失败原因（仅用于审计，不含密码）
AUTH_SOURCES = ("local", "dsm")

# 登录失败限流（内存实现，无需第三方依赖）
LOGIN_MAX_ATTEMPTS = 5          # 同一 IP+账号 5 分钟内最多失败 5 次
LOGIN_IP_MAX_ATTEMPTS = 10      # 同一 IP 5 分钟内最多失败 10 次（防止撞库）
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_TRACKED_KEYS = 2048
LOGIN_RATE_LIMIT_MESSAGE = u"登录失败次数过多，请 5 分钟后再试"


class DsmTransportError(Exception):
    pass


class DsmApiError(Exception):
    def __init__(self, code, detail=None):
        super(DsmApiError, self).__init__("dsm api error %s" % code)
        self.code = int(code or 0)
        self.detail = detail or {}


class Session(object):
    __slots__ = ("session_id", "user", "is_admin", "csrf_token", "created_at",
                 "expires_at", "last_seen", "dsm_sid", "client_ip", "source")

    def __init__(self, session_id, user, is_admin, csrf_token, ttl, dsm_sid=None,
                 client_ip=None, source="local"):
        self.session_id = session_id
        self.user = user
        self.is_admin = bool(is_admin)
        self.csrf_token = csrf_token
        self.created_at = time.time()
        self.expires_at = self.created_at + int(ttl)
        self.last_seen = self.created_at
        self.dsm_sid = dsm_sid
        self.client_ip = client_ip
        self.source = source

    def user_dict(self):
        return {"name": self.user, "is_admin": bool(self.is_admin)}

    def expires_iso(self):
        return iso_local(self.expires_at)

    def touch(self):
        self.last_seen = time.time()

    def is_expired(self, now=None):
        return (now or time.time()) >= self.expires_at


# ---------------------------------------------------------------------------
# 本地管理员校验（群晖命令兜底）
# ---------------------------------------------------------------------------

def _run_command(args, timeout=3.0):
    try:
        proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    try:
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
    except Exception:
        return None
    return proc.returncode, stdout, stderr


def local_is_admin(username):
    """本地校验用户是否属于 administrators 组。

    返回 True / False / None（None 表示无法判断，调用方必须按失败处理）。
    群晖 API 调用点补充：synogroup --member administrators、id -Gn。
    """
    if not username:
        return False
    result = _run_command(["synogroup", "--member", "administrators"])
    if result is not None and result[0] == 0:
        text = result[1]
        match = re.search(r"Members:\s*\[([^\]]*)\]", text)
        if match is not None:
            members = [item.strip() for item in match.group(1).split(",") if item.strip()]
            return username in members
        if "administrators" in text:
            # 命令成功但格式不认识：按失败处理，绝不误判为管理员
            return False
    result = _run_command(["id", "-Gn", username])
    if result is not None and result[0] == 0:
        groups = result[1].split()
        return "administrators" in groups
    return None


# ---------------------------------------------------------------------------
# DSM 客户端
# ---------------------------------------------------------------------------

def _default_transport(scheme, port, path, params, timeout):
    query = urllib.parse.urlencode(params)
    url = "%s://127.0.0.1:%d%s?%s" % (scheme, port, path, query)
    context = None
    if scheme == "https":
        # DSM 默认自签名证书；只访问 127.0.0.1，因此放宽证书校验。
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            body = response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            raise DsmTransportError("HTTP %s" % exc.code)
        if not body:
            raise DsmTransportError("HTTP %s" % exc.code)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DsmTransportError(str(exc))
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeError):
        raise DsmTransportError("invalid json response")


class DsmClient(object):
    """极简 DSM WebAPI 客户端（仅回环地址，供认证与共享文件夹枚举使用）。"""

    def __init__(self, transport=None, timeout=5.0,
                 endpoints=None, ports=None):
        self._transport = transport or _default_transport
        self.timeout = timeout
        if endpoints is not None:
            self.endpoints = list(endpoints)
        elif ports is not None:
            self.endpoints = []
            for port in ports:
                self.endpoints.append((5001, "https"))
        else:
            self.endpoints = [(5001, "https"), (5001, "http"),
                              (5000, "https"), (5000, "http")]

    def call(self, api, version, method, params=None, path="/webapi/entry.cgi",
             sid=None):
        payload = {"api": api, "version": version, "method": method}
        if sid:
            payload["_sid"] = sid
        if params:
            payload.update(params)
        last_error = None
        for port, scheme in self.endpoints:
            try:
                data = self._transport(scheme, port, path, payload, self.timeout)
            except DsmTransportError as exc:
                last_error = exc
                continue
            if not isinstance(data, dict):
                last_error = DsmTransportError("bad response")
                continue
            if data.get("success"):
                result = data.get("data")
                return result if result is not None else {}
            error = data.get("error") or {}
            code = error.get("code")
            raise DsmApiError(int(code or 0), error.get("errors") or {})
        raise DsmTransportError(str(last_error or "DSM WebAPI 不可达"))

    def login(self, username, password, otp=None):
        """群晖 API 调用点：SYNO.API.Auth 登录，返回 sid。"""
        params = {
            "account": username,
            "passwd": password,
            "session": "Core",
            "format": "sid",
        }
        if otp:
            params["otp_code"] = otp
        candidates = [
            ("/webapi/auth.cgi", 6),
            ("/webapi/entry.cgi", 6),
            ("/webapi/auth.cgi", 3),
        ]
        last_api_error = None
        for path, version in candidates:
            try:
                data = self.call("SYNO.API.Auth", version, "login", params, path=path)
            except DsmApiError as exc:
                # 103/104/105 是版本/接口不匹配，换下一种组合；其余为真实认证错误。
                if exc.code in (103, 104, 105):
                    last_api_error = exc
                    continue
                message = DSM_AUTH_ERRORS.get(exc.code, u"DSM 认证失败")
                raise ApiError("AUTH_FAILED", message,
                               details={"dsm_error_code": exc.code})
            sid = data.get("sid") if isinstance(data, dict) else None
            if sid:
                return sid
            last_api_error = DsmApiError(0)
        raise ApiError("AUTH_FAILED", u"DSM 认证失败，请检查账号或服务状态",
                       details={"dsm_error": str(last_api_error)})

    def logout(self, sid):
        """群晖 API 调用点：SYNO.API.Auth 注销（尽力而为，不影响主流程）。"""
        if not sid:
            return
        for path, version in (("/webapi/auth.cgi", 6), ("/webapi/auth.cgi", 3),
                              ("/webapi/entry.cgi", 6)):
            try:
                self.call("SYNO.API.Auth", version, "logout",
                          {"session": "Core"}, path=path, sid=sid)
                return
            except (DsmApiError, DsmTransportError):
                continue

    def current_user(self, sid):
        """群晖 API 调用点：SYNO.Core.CurrentUser / SYNO.Core.User。"""
        attempts = [
            ("SYNO.Core.CurrentUser", 1, "get", {}, "/webapi/entry.cgi"),
        ]
        for api, version, method, params, path in attempts:
            try:
                data = self.call(api, version, method, params, path=path, sid=sid)
            except (DsmApiError, DsmTransportError):
                continue
            if isinstance(data, dict):
                return data
        try:
            data = self.call("SYNO.Core.User", 1, "get",
                             {"name": "", "additional": json.dumps(
                                 ["is_admin", "groups", "email"])},
                             path="/webapi/entry.cgi", sid=sid)
            if isinstance(data, dict):
                return data
        except (DsmApiError, DsmTransportError):
            pass
        return None

    def authenticate(self, username, password, otp=None, local_admin_checker=None,
                     admin_users=None):
        """登录并判断管理员身份；返回 dict（dsm_sid / is_admin / groups）。"""
        sid = self.login(username, password, otp=otp)
        is_admin = None
        groups = []
        info = self.current_user(sid)
        if isinstance(info, dict):
            is_admin = _extract_is_admin(info)
            groups = _extract_groups(info)
        if is_admin is None:
            checker = local_admin_checker or local_is_admin
            try:
                local_result = checker(username)
            except Exception:
                local_result = None
            if local_result is not None:
                is_admin = bool(local_result)
        if is_admin is not True and admin_users and username in admin_users:
            is_admin = True
        return {"dsm_sid": sid, "is_admin": is_admin, "groups": groups}


def _extract_is_admin(data):
    if not isinstance(data, dict):
        return None
    for key in ("is_admin", "isAdmin", "admin"):
        value = data.get(key)
        if isinstance(value, bool):
            return value
    user = data.get("user")
    if isinstance(user, dict):
        for key in ("is_admin", "isAdmin", "admin"):
            value = user.get(key)
            if isinstance(value, bool):
                return value
        groups = user.get("groups")
        if isinstance(groups, list):
            return "administrators" in groups
    groups = data.get("groups")
    if isinstance(groups, list):
        return "administrators" in groups
    return None


def _extract_groups(data):
    if not isinstance(data, dict):
        return []
    user = data.get("user") if isinstance(data.get("user"), dict) else data
    groups = user.get("groups") if isinstance(user, dict) else None
    if isinstance(groups, list):
        return [str(item) for item in groups]
    return []


# ---------------------------------------------------------------------------
# 会话管理
# ---------------------------------------------------------------------------

class AuthManager(object):
    def __init__(self, runtime_config, store, dsm_client=None, admin_users=None,
                 local_admin_checker=None, clock=None):
        self.config = runtime_config
        self.store = store
        self._dsm = dsm_client or DsmClient()
        self.admin_users = set(admin_users or config_admin_users(runtime_config))
        self._local_admin_checker = local_admin_checker or local_is_admin
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._sessions = {}
        # 登录失败限流状态：{(client_ip, username): [ts...]} 与 {client_ip: [ts...]}
        self._login_failures = {}
        self._ip_failures = {}

    # -- 登录限流 -------------------------------------------------------
    def _prune_login_failures(self, now):
        cutoff = now - LOGIN_WINDOW_SECONDS
        for store in (self._login_failures, self._ip_failures):
            expired_keys = []
            for key, stamps in store.items():
                fresh = [stamp for stamp in stamps if stamp > cutoff]
                if fresh:
                    store[key] = fresh
                else:
                    expired_keys.append(key)
            for key in expired_keys:
                store.pop(key, None)
        if len(self._login_failures) > LOGIN_MAX_TRACKED_KEYS:
            # 有界：删除最旧的一半，避免内存无限增长
            ordered = sorted(self._login_failures.items(),
                             key=lambda item: max(item[1] or [0]))
            for key, _stamps in ordered[:len(ordered) // 2]:
                self._login_failures.pop(key, None)

    def _check_login_rate_limit(self, client_ip, username):
        now = self._clock()
        with self._lock:
            self._prune_login_failures(now)
            ip_key = client_ip or "unknown"
            attempts = self._login_failures.get((ip_key, username)) or []
            ip_attempts = self._ip_failures.get(ip_key) or []
            if (len(attempts) >= LOGIN_MAX_ATTEMPTS
                    or len(ip_attempts) >= LOGIN_IP_MAX_ATTEMPTS):
                raise ApiError("AUTH_FAILED", LOGIN_RATE_LIMIT_MESSAGE, status=429)

    def _record_login_failure(self, client_ip, username):
        now = self._clock()
        with self._lock:
            self._prune_login_failures(now)
            ip_key = client_ip or "unknown"
            self._login_failures.setdefault((ip_key, username), []).append(now)
            self._ip_failures.setdefault(ip_key, []).append(now)

    def _reset_login_failures(self, client_ip, username):
        with self._lock:
            ip_key = client_ip or "unknown"
            self._login_failures.pop((ip_key, username), None)
            self._ip_failures.pop(ip_key, None)

    # -- 登录 -----------------------------------------------------------
    def login(self, username, password, otp=None, client_ip=None):
        if not isinstance(username, str) or not username.strip():
            raise invalid(u"请输入账号")
        username = username.strip()
        if not isinstance(password, str) or not password:
            raise invalid(u"请输入密码")
        if otp is not None and not isinstance(otp, str):
            raise invalid(u"OTP 参数非法")

        # 先检查限流：账号不存在/密码错误/非管理员一律计入，不泄露账号是否存在
        self._check_login_rate_limit(client_ip, username)

        if self.config.dev_mode:
            if username == "admin" and password == self.config.dev_password:
                self._reset_login_failures(client_ip, username)
                session = self._new_session(username, True, dsm_sid=None,
                                            client_ip=client_ip, source="dev")
                self.store.audit("login_success", username,
                                 {"dev_mode": True, "client_ip": client_ip})
                return session
            self._record_login_failure(client_ip, username)
            self.store.audit("login_failed", username,
                             {"dev_mode": True, "reason": "invalid_credentials"})
            raise auth_failed(u"开发模式下请使用 admin / 配置的密码登录")

        try:
            result = self._dsm.authenticate(
                username, password, otp=otp,
                admin_users=self.admin_users,
                local_admin_checker=self._local_admin_checker)
        except ApiError as exc:
            self._record_login_failure(client_ip, username)
            self.store.audit("login_failed", username,
                             {"reason": exc.code, "client_ip": client_ip})
            raise
        except Exception as exc:
            self._record_login_failure(client_ip, username)
            self.store.audit("login_failed", username,
                             {"reason": "dsm_unreachable", "error": str(exc),
                              "client_ip": client_ip})
            raise ApiError("AUTH_FAILED", u"无法连接 DSM 认证服务，请稍后重试",
                           details={"error": str(exc)})

        dsm_sid = result.get("dsm_sid")
        is_admin = bool(result.get("is_admin") is True)
        if not is_admin:
            try:
                self._dsm.logout(dsm_sid)
            except Exception:
                pass
            self._record_login_failure(client_ip, username)
            self.store.audit("login_failed", username,
                             {"reason": "not_admin", "client_ip": client_ip})
            raise forbidden(u"仅 DSM administrators 组成员可以使用本套件")

        self._reset_login_failures(client_ip, username)
        session = self._new_session(username, True, dsm_sid=dsm_sid,
                                    client_ip=client_ip, source="dsm")
        self.store.audit("login_success", username,
                         {"dev_mode": False, "client_ip": client_ip})
        return session

    def _new_session(self, username, is_admin, dsm_sid=None, client_ip=None,
                     source="local"):
        session = Session(
            session_id=secrets.token_urlsafe(32),
            user=username,
            is_admin=is_admin,
            csrf_token=secrets.token_urlsafe(32),
            ttl=config.SESSION_TTL_SECONDS,
            dsm_sid=dsm_sid,
            client_ip=client_ip,
            source=source,
        )
        now = self._clock()
        session.created_at = now
        session.last_seen = now
        session.expires_at = now + int(config.SESSION_TTL_SECONDS)
        with self._lock:
            self._purge_locked()
            self._sessions[session.session_id] = session
        return session

    def _purge_locked(self):
        now = self._clock()
        expired = [sid for sid, session in self._sessions.items()
                   if session.is_expired(now)]
        for sid in expired:
            session = self._sessions.pop(sid, None)
            self._logout_dsm(session)

    def _logout_dsm(self, session):
        if session is None or not session.dsm_sid:
            return
        try:
            self._dsm.logout(session.dsm_sid)
        except Exception:
            pass

    # -- 会话查询 -------------------------------------------------------
    def get(self, session_id):
        if not session_id or not isinstance(session_id, str):
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.is_expired(self._clock()):
                self._sessions.pop(session_id, None)
                self._logout_dsm(session)
                return None
            session.touch()
            return session

    def require(self, session_id):
        session = self.get(session_id)
        if session is None:
            raise auth_required()
        return session

    def logout(self, session_id):
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            self._logout_dsm(session)
            self.store.audit("logout", session.user, {"client_ip": session.client_ip})
        return True

    def verify_csrf(self, session, token):
        if session is None:
            return False
        if not isinstance(token, str) or not token:
            return False
        return hmac.compare_digest(session.csrf_token, token)

    def purge_expired(self):
        with self._lock:
            self._purge_locked()

    def clear(self):
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            self._logout_dsm(session)


def config_admin_users(runtime_config):
    """显式配置的管理员兜底名单（生产可设 DUP_ADMIN_USERS=a,b）。"""
    admin_users = getattr(runtime_config, "admin_users", None)
    if admin_users is None:
        raw = os.environ.get("DUP_ADMIN_USERS", "")
        admin_users = [item.strip() for item in raw.split(",") if item.strip()]
    return [str(item) for item in admin_users]


def build_cookie(session, secure=False):
    parts = [
        "%s=%s" % (config.SESSION_COOKIE, session.session_id),
        "HttpOnly",
        "SameSite=Strict",
        "Path=/",
        "Max-Age=%d" % config.SESSION_TTL_SECONDS,
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie(secure=False):
    parts = [
        "%s=" % config.SESSION_COOKIE,
        "HttpOnly",
        "SameSite=Strict",
        "Path=/",
        "Max-Age=0",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)
