# -*- coding: utf-8 -*-
"""v0021 P0 后端硬化测试：权限、异常脱敏、诊断、备份/维护、删除历史。"""

from __future__ import absolute_import

import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import unittest

from tests.helpers import Harness, WebServer, write_bytes

from dupcleaner import config, db as db_module
from dupcleaner.httpapi import Api, Request
from dupcleaner.settings import SettingsManager
from dupcleaner.store import Store

SECRET = "SECRET-V21-ASR-KEY-9f3a7c"
FIXED_INTERNAL_MESSAGE = u"服务器内部错误，请稍后重试"


def _posix_only(test):
    return unittest.skipIf(os.name == "nt", "Windows 无法验证 POSIX 权限位")(test)


class V21ApiTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root
        os.makedirs(os.path.join(self.root, "#recycle"), exist_ok=True)
        self.a1 = write_bytes(os.path.join(self.root, "a1.txt"), b"X" * 100)
        self.a2 = write_bytes(os.path.join(self.root, "sub", "a2.txt"), b"X" * 100)
        self.harness.set_all_creation([self.a1, self.a2], base=1000)
        self.web = WebServer(self.harness).start()
        self.client = self.web.client
        self.csrf = None

    def tearDown(self):
        self.web.stop()
        self.harness.cleanup()

    # -- helpers -------------------------------------------------------
    def login(self):
        self.csrf = self.client.login("admin", "devpass")
        return self.csrf

    def json_call(self, method, path, body=None, csrf=True):
        return self.client.json(method, path, body=body,
                                csrf=self.csrf if csrf else None)

    def scan(self):
        if self.csrf is None:
            self.login()
        status, _headers, payload = self.json_call(
            "POST", "/api/scans",
            {"roots": [self.root], "excludes": [],
             "options": {"keep_mode": "latest"}})
        self.assertEqual(status, 202)
        scan_id = payload["scan_id"]
        deadline = time.time() + 40.0
        while time.time() < deadline:
            status, _headers, payload = self.json_call(
                "GET", "/api/scans/%s" % scan_id)
            self.assertEqual(status, 200)
            scan = payload["scan"]
            if scan["status"] in ("completed", "failed", "stopped"):
                self.assertEqual(scan["status"], "completed")
                return scan_id
            time.sleep(0.02)
        raise AssertionError("scan did not finish")

    def run_delete_job(self, scan_id):
        status, _headers, payload = self.json_call(
            "POST", "/api/scans/%s/delete-plan" % scan_id,
            {"scope": "all_duplicates", "recycle": True})
        self.assertEqual(status, 200)
        plan = payload["plan"]
        status, _headers, payload = self.json_call(
            "POST", "/api/scans/%s/delete" % scan_id,
            {"plan_id": plan["plan_id"], "confirm": "DELETE"})
        self.assertEqual(status, 202)
        job_id = payload["job_id"]
        deadline = time.time() + 40.0
        while time.time() < deadline:
            status, _headers, payload = self.json_call(
                "GET", "/api/scans/%s/delete/%s" % (scan_id, job_id))
            self.assertEqual(status, 200)
            job = payload["job"]
            if job["status"] in ("completed", "partial", "failed", "stopped"):
                return job
            time.sleep(0.02)
        raise AssertionError("delete job did not finish")

    @staticmethod
    def _session():
        class _Viewer(object):
            user = "viewer"
            is_admin = False
            csrf_token = "csrf-viewer"
            session_id = "viewer-session"
        return _Viewer()

    # -- 认证 / 授权 ----------------------------------------------------
    def test_admin_endpoints_require_login(self):
        for method, path in (("GET", "/api/diagnostics"),
                             ("GET", "/api/deletions?limit=10"),
                             ("POST", "/api/storage/backup"),
                             ("POST", "/api/storage/maintenance")):
            status, _headers, payload = self.client.json(method, path, {})
            self.assertEqual(status, 401, path)
            self.assertEqual(payload["error"]["code"], "AUTH_REQUIRED", path)

    def test_admin_endpoints_forbidden_for_non_admin(self):
        services = self.harness.services
        auth = services.auth
        original_require = auth.require
        original_verify = auth.verify_csrf
        auth.require = lambda token: self._session()
        auth.verify_csrf = lambda session, token: True
        try:
            api = self.web.api
            cases = (
                ("GET", "/api/diagnostics", b""),
                ("GET", "/api/deletions", b""),
                ("POST", "/api/storage/backup", b"{}"),
                ("POST", "/api/storage/maintenance",
                 json.dumps({"confirm": True}).encode("utf-8")),
            )
            for method, path, body in cases:
                request = Request(method, path, {}, {}, body,
                                  client_ip="127.0.0.1")
                response = api.dispatch(request)
                payload = json.loads(response.body.decode("utf-8"))
                self.assertEqual(response.status, 403, path)
                self.assertEqual(payload["error"]["code"], "FORBIDDEN", path)
                self.assertNotIn(SECRET, response.body.decode("utf-8"))
        finally:
            auth.require = original_require
            auth.verify_csrf = original_verify

    # -- 诊断 -----------------------------------------------------------
    def test_diagnostics_payload_redaction(self):
        self.login()
        status, _headers, payload = self.json_call(
            "PUT", "/api/settings",
            {"settings": {"subtitle_asr_api_key": SECRET}})
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET, json.dumps(payload, ensure_ascii=False))

        status, _headers, raw = self.client.request("GET", "/api/diagnostics")
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET.encode("utf-8"), raw)
        payload = json.loads(raw.decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["app_version"], config.APP_VERSION)
        database = payload["database"]
        for key in ("path", "size", "wal_size", "shm_size", "total_size",
                    "schema_version"):
            self.assertIn(key, database)
        self.assertGreaterEqual(database["schema_version"],
                                db_module.CURRENT_SCHEMA_VERSION)
        self.assertTrue(os.path.isabs(database["path"]))
        self.assertEqual(sorted(payload["row_counts"].keys()),
                         sorted(Store.DIAGNOSTIC_TABLES))
        for table, count in payload["row_counts"].items():
            self.assertIsInstance(count, int, table)
            self.assertGreaterEqual(count, 0)
        self.assertIn("entries", payload["feature_cache"])
        for key in ("path", "available", "entries", "size"):
            self.assertIn(key, payload["video_cache"])
        ffmpeg = payload["ffmpeg"]
        for key in ("available", "path", "version", "hwaccels", "gpu_hint"):
            self.assertIn(key, ffmpeg)
        self.assertIsInstance(ffmpeg["hwaccels"], list)
        asr = payload["asr"]
        for key in ("enabled", "configured", "available", "reason", "weight"):
            self.assertIn(key, asr)
        active = payload["active"]
        for key in ("scans", "paused_scans", "subtitle_jobs"):
            self.assertIsInstance(active[key], int)
        self.assertEqual(payload["keys"],
                         {"subtitle_asr_api_key_set": True,
                          "subtitle_llm_api_key_set": False})
        self.assertFalse(payload["database"]["path"].startswith(SECRET))

    # -- 备份 / 恢复 -----------------------------------------------------
    def test_backup_restore_smoke(self):
        self.login()
        scan_id = self.scan()
        status, _headers, raw = self.client.request(
            "POST", "/api/storage/backup", {}, headers={
                "X-CSRF-Token": self.csrf})
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET.encode("utf-8"), raw)
        payload = json.loads(raw.decode("utf-8"))
        self.assertTrue(payload["ok"])
        backup = payload["backup"]
        for key in ("filename", "path", "size", "schema_version", "created_at"):
            self.assertIn(key, backup)
        self.assertTrue(os.path.isfile(backup["path"]))
        self.assertGreater(backup["size"], 0)
        self.assertGreaterEqual(backup["schema_version"],
                                db_module.CURRENT_SCHEMA_VERSION)
        expected_dir = os.path.join(self.harness.data_dir, "backups")
        self.assertEqual(os.path.dirname(os.path.abspath(backup["path"])),
                         os.path.abspath(expected_dir))
        # 备份文件可独立打开，schema 与主库一致。
        conn = sqlite3.connect(backup["path"])
        try:
            self.assertEqual(
                str(conn.execute("PRAGMA quick_check(1)").fetchone()[0]).lower(),
                "ok")
            version = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            self.assertEqual(int(version[0]),
                             db_module.CURRENT_SCHEMA_VERSION)
            backup_scans = conn.execute(
                "SELECT COUNT(*) FROM scans").fetchone()[0]
        finally:
            conn.close()
        main_scans = self.harness.services.store.db.scalar(
            "SELECT COUNT(*) FROM scans", (), 0)
        self.assertEqual(int(backup_scans), int(main_scans))
        # 恢复冒烟：把备份放回全新数据目录，可读回扫描元数据。
        restore_dir = os.path.join(self.harness.tmpdir, "restore")
        os.makedirs(restore_dir)
        shutil.copyfile(backup["path"],
                        os.path.join(restore_dir, "dupcleaner.sqlite3"))
        restored = Store(restore_dir)
        try:
            meta = restored.load_scan_meta(scan_id)
            self.assertIsInstance(meta, dict)
            self.assertEqual(meta.get("scan_id"), scan_id)
        finally:
            restored.close()

    # -- 维护 -----------------------------------------------------------
    def test_maintenance_requires_confirm_and_reports_sizes(self):
        self.login()
        status, _headers, payload = self.json_call(
            "POST", "/api/storage/maintenance", {})
        self.assertEqual(status, 400)
        self.assertTrue(payload["ok"] is False)
        self.assertIn(payload["error"]["code"],
                      ("INVALID_ARGUMENT", "INVALID_PATH"))
        status, _headers, payload = self.json_call(
            "POST", "/api/storage/maintenance", {"confirm": False})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")

        status, _headers, raw = self.client.request(
            "POST", "/api/storage/maintenance",
            json.dumps({"confirm": True, "vacuum": False}).encode("utf-8"),
            headers={"X-CSRF-Token": self.csrf,
                     "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        payload = json.loads(raw.decode("utf-8"))
        maintenance = payload["maintenance"]
        self.assertEqual(maintenance["checkpoint"], "truncate")
        self.assertTrue(maintenance["optimized"])
        self.assertFalse(maintenance["vacuumed"])
        for key in ("before", "after"):
            for size_key in ("db_size", "wal_size", "shm_size", "total_size"):
                self.assertIsInstance(maintenance[key][size_key], int)
        self.assertGreaterEqual(maintenance["reclaimed_bytes"], 0)
        self.assertNotIn(SECRET.encode("utf-8"), raw)

        status, _headers, payload = self.json_call(
            "POST", "/api/storage/maintenance",
            {"confirm": True, "vacuum": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["maintenance"]["vacuumed"])
        # 维护后数据库仍可用。
        self.assertEqual(
            self.harness.services.store.db.schema_version(),
            db_module.CURRENT_SCHEMA_VERSION)
        status, _headers, payload = self.json_call("GET", "/api/healthz")
        self.assertEqual(status, 200)

    # -- 删除任务历史 ----------------------------------------------------
    def test_deletion_history_sanitized(self):
        self.login()
        self.harness.services.settings.update(
            {"subtitle_asr_api_key": SECRET})
        scan_id = self.scan()
        job = self.run_delete_job(scan_id)
        self.assertEqual(job["status"], "completed")

        status, _headers, raw = self.client.request(
            "GET", "/api/deletions?limit=10")
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET.encode("utf-8"), raw)
        payload = json.loads(raw.decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["limit"], 10)
        self.assertGreaterEqual(payload["total"], 1)
        entry = [item for item in payload["jobs"]
                 if item.get("job_id") == job["job_id"]][0]
        for key in ("job_id", "scan_id", "status", "total", "done", "failed",
                    "skipped", "bytes_total", "bytes_done", "recycle",
                    "started_at"):
            self.assertIn(key, entry)
        self.assertNotIn("results", entry)
        self.assertNotIn("path", entry)
        self.assertEqual(entry["files"], entry["total"])
        self.assertEqual(entry["deleted"], entry["done"])
        self.assertEqual(entry["bytes"], entry["bytes_total"])
        self.assertEqual(entry["scan_id"], scan_id)
        self.assertTrue(entry["recycle"])

        status, _headers, _payload = self.client.request(
            "GET", "/api/deletions?limit=0")
        self.assertEqual(status, 200)

    # -- 全局异常脱敏 ----------------------------------------------------
    def test_global_exception_redacted_with_request_id(self):
        self.login()
        store = self.harness.services.store
        original = store.list_logs

        def _boom(*args, **kwargs):
            raise RuntimeError("internal detail %s" % SECRET)

        store.list_logs = _boom
        try:
            with self.assertLogs("dupcleaner.api", level="ERROR") as captured:
                status, headers, raw = self.client.request("GET", "/api/logs")
        finally:
            store.list_logs = original
        self.assertEqual(status, 500)
        text = raw.decode("utf-8")
        self.assertNotIn(SECRET, text)
        self.assertNotIn("internal detail", text)
        payload = json.loads(text)
        self.assertEqual(payload["error"]["code"], "INTERNAL_ERROR")
        self.assertEqual(payload["error"]["message"], FIXED_INTERNAL_MESSAGE)
        request_id = payload["error"]["details"].get("request_id")
        self.assertTrue(request_id)
        self.assertIn("x-request-id", headers)
        self.assertEqual(headers["x-request-id"][0], request_id)
        # 完整堆栈只写服务器日志（含异常类型），不回传客户端。
        log_text = "\n".join(captured.output)
        self.assertIn("API 处理异常", log_text)
        self.assertIn(request_id, log_text)

    # -- 密钥不落审计/任务 JSON -----------------------------------------
    def test_no_secret_persisted_in_audit_job_or_db_json(self):
        self.login()
        self.harness.services.settings.update(
            {"subtitle_asr_api_key": SECRET,
             "subtitle_llm_api_key": SECRET + "-LLM"})
        scan_id = self.scan()
        job = self.run_delete_job(scan_id)

        # 1) API 回读不出现明文密钥
        status, _headers, raw = self.client.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET.encode("utf-8"), raw)
        status, _headers, raw = self.client.request("GET", "/api/logs")
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET.encode("utf-8"), raw)

        # 2) DB 中所有扫描/分组/任务 JSON 不含密钥
        db = self.harness.services.store.db
        payloads = []
        payloads.extend(row[0] for row in db.query(
            "SELECT meta_json FROM scans"))
        payloads.extend(row[0] for row in db.query(
            "SELECT payload_json FROM scan_groups"))
        # v5：scan_files 不再保存冗余 payload_json；改为检查路径列不含密钥，
        # 并断言该列已从表结构中移除。
        payloads.extend(row[0] for row in db.query(
            "SELECT path FROM scan_files"))
        columns = {row[1] for row in db.query(
            "PRAGMA table_info(scan_files)")}
        self.assertNotIn("payload_json", columns)
        payloads.extend(row[0] for row in db.query(
            "SELECT meta_json FROM deletion_jobs"))
        payloads.extend(row[0] for row in db.query(
            "SELECT payload_json FROM deletion_results"))
        for text in payloads:
            self.assertNotIn(SECRET, text)
        self.assertNotIn(SECRET, json.dumps(
            self.harness.services.store.load_job_meta(job["job_id"]),
            ensure_ascii=False))
        # 3) 审计/计划/日志等文本文件（settings.json 除外，它必须存密钥但 600）
        for directory, _dirs, files in os.walk(self.harness.data_dir):
            for name in files:
                if name == "settings.json" or name.endswith((".sqlite3", ".sqlite3-wal",
                                                               ".sqlite3-shm")):
                    continue
                path = os.path.join(directory, name)
                try:
                    with open(path, "rb") as handle:
                        data = handle.read()
                except OSError:
                    continue
                self.assertNotIn(SECRET.encode("utf-8"), data, path)


class V21PermissionTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-v21-perm-")
        self.data_dir = os.path.join(self.tmpdir, "data")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _store(self):
        return Store(self.data_dir)

    @_posix_only
    def test_settings_json_is_600_on_save_and_load(self):
        store = self._store()
        try:
            manager = SettingsManager(store)
            manager.update({"subtitle_asr_api_key": SECRET})
            path = os.path.join(self.data_dir, "settings.json")
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            # 模拟升级/外部修复把权限放宽，加载时必须重新收紧。
            os.chmod(path, 0o644)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
            SettingsManager(store)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        finally:
            store.close()

    def test_settings_save_calls_chmod_600(self):
        # Windows 无法验证权限位，但必须验证代码确实请求了 0o600。
        from unittest import mock
        store = self._store()
        real_chmod = os.chmod
        seen = []

        def _record(path, mode):
            seen.append((os.path.abspath(path), int(mode)))
            try:
                return real_chmod(path, mode)
            except OSError:
                return None

        try:
            manager = SettingsManager(store)
            with mock.patch("os.chmod", side_effect=_record):
                manager.update({"subtitle_asr_api_key": SECRET})
            path = os.path.abspath(os.path.join(self.data_dir,
                                                "settings.json"))
            self.assertIn((path, 0o600), seen)
        finally:
            store.close()

    def test_tls_load_calls_chmod_600(self):
        from unittest import mock
        cert = write_bytes(os.path.join(self.tmpdir, "server.crt"), b"cert")
        key = write_bytes(os.path.join(self.tmpdir, "server.key"), b"key")
        real_chmod = os.chmod
        seen = []

        def _record(path, mode):
            seen.append((os.path.abspath(path), int(mode)))
            try:
                return real_chmod(path, mode)
            except OSError:
                return None

        with mock.patch("os.chmod", side_effect=_record):
            config.RuntimeConfig(data_dir=self.data_dir, tls_cert=cert,
                                 tls_key=key, environ={})
        self.assertIn((os.path.abspath(cert), 0o600), seen)
        self.assertIn((os.path.abspath(key), 0o600), seen)

    @_posix_only
    def test_tls_files_are_600_on_load(self):
        cert = write_bytes(os.path.join(self.tmpdir, "server.crt"), b"cert")
        key = write_bytes(os.path.join(self.tmpdir, "server.key"), b"key")
        os.chmod(cert, 0o644)
        os.chmod(key, 0o644)
        config.RuntimeConfig(data_dir=self.data_dir, tls_cert=cert,
                             tls_key=key, environ={})
        self.assertEqual(stat.S_IMODE(os.stat(cert).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(key).st_mode), 0o600)

    def test_secure_permissions_handles_missing_files(self):
        self.assertFalse(config.secure_permissions(""))
        self.assertFalse(config.secure_permissions(
            os.path.join(self.tmpdir, "missing.key")))
        self.assertTrue(config.ensure_private_dir(
            os.path.join(self.tmpdir, "backups")))


if __name__ == "__main__":
    unittest.main()
