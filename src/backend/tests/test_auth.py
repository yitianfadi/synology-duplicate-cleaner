# -*- coding: utf-8 -*-
import os
import shutil
import tempfile
import unittest

from tests.helpers import BACKEND_DIR  # noqa: F401

from dupcleaner import config
from dupcleaner.auth import (AuthManager, DsmClient, _extract_groups,
                             _extract_is_admin, build_cookie, clear_cookie)
from dupcleaner.errors import ApiError
from dupcleaner.store import Store


class FakeTransport(object):
    """模拟 DSM WebAPI 回环响应。"""

    def __init__(self, is_admin=True, login_error=None, current_user=True):
        self.is_admin = is_admin
        self.login_error = login_error
        self.current_user_enabled = current_user
        self.calls = []

    def __call__(self, scheme, port, path, params, timeout):
        self.calls.append(dict(params))
        api = params.get("api")
        method = params.get("method")
        if api == "SYNO.API.Auth" and method == "login":
            if self.login_error is not None:
                return {"success": False, "error": {"code": self.login_error}}
            return {"success": True, "data": {"sid": "SID-1"}}
        if api == "SYNO.API.Auth" and method == "logout":
            return {"success": True, "data": {}}
        if api == "SYNO.Core.CurrentUser":
            if not self.current_user_enabled:
                return {"success": False, "error": {"code": 103}}
            return {"success": True, "data": {"is_admin": self.is_admin}}
        return {"success": False, "error": {"code": 103}}

    def logout_calls(self):
        return [call for call in self.calls
                if call.get("api") == "SYNO.API.Auth"
                and call.get("method") == "logout"]


class AuthTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-auth-")
        self.store = Store(os.path.join(self.tmpdir, "data"))
        self.clock = {"now": 1700000000.0}

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _dev_config(self):
        return config.RuntimeConfig(
            data_dir=os.path.join(self.tmpdir, "data"),
            dev_mode=True, dev_roots=[], dev_password="pw", environ={})

    def _dsm_config(self):
        return config.RuntimeConfig(
            data_dir=os.path.join(self.tmpdir, "data"),
            dev_mode=False, dev_roots=[], environ={})


class DevAuthTest(AuthTestBase):
    def test_dev_login_success(self):
        manager = AuthManager(self._dev_config(), self.store)
        session = manager.login("admin", "pw", client_ip="127.0.0.1")
        self.assertEqual(session.user_dict(), {"name": "admin", "is_admin": True})
        self.assertTrue(session.csrf_token)
        self.assertIs(manager.get(session.session_id), session)
        self.assertIsNone(manager.get("nope"))
        cookie = build_cookie(session)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Max-Age=28800", cookie)
        self.assertNotIn("Secure", cookie)
        self.assertIn("Max-Age=0", clear_cookie())

    def test_dev_login_wrong_password(self):
        manager = AuthManager(self._dev_config(), self.store)
        with self.assertRaises(ApiError) as ctx:
            manager.login("admin", "bad", client_ip="1.2.3.4")
        self.assertEqual(ctx.exception.code, "AUTH_FAILED")
        with self.assertRaises(ApiError):
            manager.login("other", "pw", client_ip="1.2.3.4")

    def test_session_expiry(self):
        manager = AuthManager(self._dev_config(), self.store,
                              clock=lambda: self.clock["now"])
        session = manager.login("admin", "pw")
        self.assertIsNotNone(manager.get(session.session_id))
        self.clock["now"] += config.SESSION_TTL_SECONDS + 1
        self.assertIsNone(manager.get(session.session_id))

    def test_csrf_verification(self):
        manager = AuthManager(self._dev_config(), self.store)
        session = manager.login("admin", "pw")
        self.assertTrue(manager.verify_csrf(session, session.csrf_token))
        self.assertFalse(manager.verify_csrf(session, "wrong"))
        self.assertFalse(manager.verify_csrf(session, None))
        self.assertFalse(manager.verify_csrf(None, "x"))

    def test_login_rate_limit(self):
        manager = AuthManager(self._dev_config(), self.store,
                              clock=lambda: self.clock["now"])
        for _ in range(5):
            with self.assertRaises(ApiError):
                manager.login("admin", "bad", client_ip="9.9.9.9")
        with self.assertRaises(ApiError) as ctx:
            manager.login("admin", "bad", client_ip="9.9.9.9")
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn(u"登录失败次数过多", ctx.exception.message)
        # 被限流后即使密码正确也拒绝
        with self.assertRaises(ApiError):
            manager.login("admin", "pw", client_ip="9.9.9.9")
        # 其他 IP 不受影响
        self.assertIsNotNone(manager.login("admin", "pw", client_ip="8.8.8.8"))
        # 窗口过后自动恢复
        self.clock["now"] += 301
        manager2 = AuthManager(self._dev_config(), self.store,
                               clock=lambda: self.clock["now"])
        self.assertIsNotNone(manager2.login("admin", "pw", client_ip="9.9.9.9"))

    def test_success_resets_failures(self):
        manager = AuthManager(self._dev_config(), self.store)
        for _ in range(4):
            with self.assertRaises(ApiError):
                manager.login("admin", "bad", client_ip="7.7.7.7")
        manager.login("admin", "pw", client_ip="7.7.7.7")
        for _ in range(4):
            with self.assertRaises(ApiError):
                manager.login("admin", "bad", client_ip="7.7.7.7")
        self.assertIsNotNone(manager.login("admin", "pw", client_ip="7.7.7.7"))


class DsmAuthTest(AuthTestBase):
    def _manager(self, transport, admin_users=None):
        client = DsmClient(transport=transport)
        return AuthManager(self._dsm_config(), self.store, dsm_client=client,
                           admin_users=admin_users or [],
                           local_admin_checker=lambda username: None,
                           clock=lambda: self.clock["now"])

    def test_admin_login(self):
        transport = FakeTransport(is_admin=True)
        manager = self._manager(transport)
        session = manager.login("admin", "pw", client_ip="10.0.0.1")
        self.assertEqual(session.user_dict(), {"name": "admin", "is_admin": True})
        self.assertEqual(session.source, "dsm")
        self.assertEqual(session.dsm_sid, "SID-1")
        manager.logout(session.session_id)
        self.assertTrue(transport.logout_calls())

    def test_non_admin_denied(self):
        transport = FakeTransport(is_admin=False)
        manager = self._manager(transport)
        with self.assertRaises(ApiError) as ctx:
            manager.login("guest", "pw", client_ip="10.0.0.2")
        self.assertEqual(ctx.exception.code, "FORBIDDEN")
        self.assertIn(u"administrators", ctx.exception.message)
        self.assertTrue(transport.logout_calls())

    def test_bad_password_maps_to_chinese_error(self):
        transport = FakeTransport(login_error=400)
        manager = self._manager(transport)
        with self.assertRaises(ApiError) as ctx:
            manager.login("admin", "bad", client_ip="10.0.0.3")
        self.assertEqual(ctx.exception.code, "AUTH_FAILED")
        self.assertIn(u"账号或密码错误", ctx.exception.message)

    def test_otp_required_message(self):
        transport = FakeTransport(login_error=403)
        manager = self._manager(transport)
        with self.assertRaises(ApiError) as ctx:
            manager.login("admin", "pw", client_ip="10.0.0.4")
        self.assertIn(u"两步验证", ctx.exception.message)

    def test_admin_users_config_fallback(self):
        transport = FakeTransport(is_admin=None, current_user=False)
        manager = self._manager(transport, admin_users=["alice"])
        session = manager.login("alice", "pw", client_ip="10.0.0.5")
        self.assertTrue(session.is_admin)
        with self.assertRaises(ApiError):
            manager.login("bob", "pw", client_ip="10.0.0.6")

    def test_local_admin_checker_used(self):
        transport = FakeTransport(is_admin=None, current_user=False)
        client = DsmClient(transport=transport)
        manager = AuthManager(self._dsm_config(), self.store, dsm_client=client,
                              local_admin_checker=lambda username: username == "root")
        self.assertTrue(manager.login("root", "pw", client_ip="10.0.0.7").is_admin)
        with self.assertRaises(ApiError):
            manager.login("nobody", "pw", client_ip="10.0.0.8")

    def test_extract_helpers(self):
        self.assertTrue(_extract_is_admin({"is_admin": True}))
        self.assertTrue(_extract_is_admin({"user": {"is_admin": True}}))
        self.assertTrue(_extract_is_admin({"groups": ["administrators"]}))
        self.assertFalse(_extract_is_admin({"user": {"groups": ["users"]}}))
        self.assertIsNone(_extract_is_admin({"foo": 1}))
        self.assertEqual(_extract_groups({"user": {"groups": ["a"]}}), ["a"])
