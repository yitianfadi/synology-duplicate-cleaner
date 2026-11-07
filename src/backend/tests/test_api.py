# -*- coding: utf-8 -*-
import json
import os
import shutil
import sys
import time
import unittest
import urllib.parse

from tests.helpers import Harness, WebServer, write_bytes
from tests.test_similarity import BASE_TEXT, VARIANT_TEXT

from dupcleaner import config

CONTENT_X = b"X" * 100
CONTENT_Y = b"Y" * 250


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root
        os.makedirs(os.path.join(self.root, "#recycle"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "@eaDir"), exist_ok=True)
        a1 = write_bytes(os.path.join(self.root, "a1.txt"), CONTENT_X)
        a2 = write_bytes(os.path.join(self.root, "sub", "a2.txt"), CONTENT_X)
        b1 = write_bytes(os.path.join(self.root, "b", "b1.txt"), CONTENT_Y)
        b2 = write_bytes(os.path.join(self.root, "b", "b2.txt"), CONTENT_Y)
        b3 = write_bytes(os.path.join(self.root, "c", "b3.txt"), CONTENT_Y)
        # 注入严格递增且互不相同的创建时间，避免 Windows 真实 btime 相同
        # 导致 needs_manual_keep，从而让 reclaimable_bytes 期望值抖动（测试专用）。
        self.harness.set_all_creation([a1, a2], base=1000)
        self.harness.set_all_creation([b1, b2, b3], base=2000)
        self.note = os.path.join(self.root, "note.txt")
        write_bytes(self.note, u"hello preview 你好".encode("utf-8"))
        write_bytes(os.path.join(self.root, "pic.png"),
                    b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        write_bytes(os.path.join(self.root, "data.bin"), b"\x00\x01\x02")
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

    def json_call(self, method, path, body=None, csrf=True, headers=None,
                  use_cookie=True):
        return self.client.json(method, path, body=body,
                                csrf=self.csrf if csrf else None,
                                headers=headers, use_cookie=use_cookie)

    def wait_scan(self, scan_id, timeout=40.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, _headers, payload = self.json_call(
                "GET", "/api/scans/%s" % scan_id)
            self.assertEqual(status, 200)
            scan = payload["scan"]
            if scan["status"] in ("completed", "failed", "stopped"):
                return scan
            time.sleep(0.02)
        raise AssertionError("scan did not finish")

    def wait_job(self, scan_id, job_id, timeout=40.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, _headers, payload = self.json_call(
                "GET", "/api/scans/%s/delete/%s" % (scan_id, job_id))
            self.assertEqual(status, 200)
            job = payload["job"]
            if job["status"] in ("completed", "partial", "failed", "stopped"):
                return job
            time.sleep(0.02)
        raise AssertionError("delete job did not finish")

    # -- 认证 -----------------------------------------------------------
    def test_healthz_without_auth(self):
        client = self.client
        status, headers, payload = client.json(
            "GET", "/api/healthz", use_cookie=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "status": "ok",
                                   "version": config.APP_VERSION})

    def test_api_requires_cookie(self):
        status, headers, payload = self.client.json("GET", "/api/meta")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "AUTH_REQUIRED")
        status, headers, payload = self.client.json("GET", "/api/auth/me")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "AUTH_REQUIRED")

    def test_login_me_logout(self):
        status, headers, payload = self.client.json(
            "POST", "/api/auth/login",
            {"username": "admin", "password": "wrong"})
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "AUTH_FAILED")

        status, headers, payload = self.client.json(
            "POST", "/api/auth/login",
            {"username": "admin", "password": "devpass"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["user"], {"name": "admin", "is_admin": True})
        self.assertTrue(payload["csrf_token"])
        self.csrf = payload["csrf_token"]
        self.assertIn("expires_at", payload)
        cookie_headers = headers.get("set-cookie", [])
        cookie = [item for item in cookie_headers
                  if item.startswith("dc_session=")]
        self.assertTrue(cookie)
        cookie_text = cookie[0]
        self.assertIn("HttpOnly", cookie_text)
        self.assertIn("SameSite=Strict", cookie_text)
        self.assertIn("Path=/", cookie_text)
        self.assertIn("Max-Age=28800", cookie_text)
        self.assertNotIn("Secure", cookie_text)

        status, _headers, payload = self.client.json("GET", "/api/auth/me")
        self.assertEqual(status, 200)
        self.assertEqual(payload["user"]["name"], "admin")
        self.assertTrue(payload["csrf_token"])

        status, _headers, payload = self.json_call("POST", "/api/auth/logout")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        status, _headers, payload = self.client.json("GET", "/api/auth/me")
        self.assertEqual(status, 401)

    # -- CSRF / Origin --------------------------------------------------
    def test_csrf_enforced(self):
        self.login()
        status, _headers, payload = self.client.json(
            "POST", "/api/scans", {"roots": [self.root]})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "CSRF_FAILED")
        status, _headers, payload = self.client.json(
            "POST", "/api/scans", {"roots": [self.root]}, csrf="wrong-token")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "CSRF_FAILED")
        status, _headers, payload = self.json_call(
            "POST", "/api/scans", {"roots": [self.root]})
        self.assertEqual(status, 202)
        self.harness.wait_scan(payload["scan_id"], timeout=40)

    def test_origin_check(self):
        self.login()
        status, _headers, payload = self.client.json(
            "GET", "/api/meta", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "FORBIDDEN")
        good_origin = "http://127.0.0.1:%d" % self.web.port
        status, _headers, payload = self.client.json(
            "GET", "/api/meta", headers={"Origin": good_origin})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    # -- 元信息 / 共享 / 浏览 / 设置 -------------------------------------
    def test_meta_shares_browse(self):
        self.login()
        status, _headers, payload = self.json_call("GET", "/api/meta")
        self.assertEqual(status, 200)
        self.assertEqual(payload["app_version"], config.APP_VERSION)
        self.assertEqual(payload["dsm_version"], "dev")
        self.assertTrue(payload["dev_mode"])
        self.assertTrue(payload["creation_time_supported"] is not None)
        self.assertEqual(payload["limits"]["max_files"], 500000)
        self.assertIn("keep_mode", payload["defaults"])
        self.assertIn("statx_supported", payload)
        self.assertIn("platform", payload)
        similarity_caps = payload["similarity"]
        self.assertIn(".txt", similarity_caps["default_extensions"])
        self.assertEqual(similarity_caps["default_threshold"], 85)
        self.assertIn("image_backend", similarity_caps)
        self.assertIn("binary_extensions", similarity_caps)

        status, _headers, payload = self.json_call("GET", "/api/shares")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["shares"]), 1)
        share = payload["shares"][0]
        self.assertEqual(share["path"], os.path.normpath(self.root))
        for key in ("name", "volume", "filesystem", "is_usb",
                    "is_encrypted", "is_writable"):
            self.assertIn(key, share)

        status, _headers, payload = self.json_call("GET", "/api/browse")
        self.assertEqual(status, 200)
        self.assertEqual(payload["path"], "")
        self.assertIsNone(payload["parent"])
        self.assertEqual(payload["entries"], [])
        self.assertEqual(len(payload["shares"]), 1)

        status, _headers, payload = self.json_call(
            "GET", "/api/browse?path=" + urllib.parse.quote(self.root))
        self.assertEqual(status, 200)
        self.assertEqual(payload["breadcrumbs"][-1]["path"], self.root)
        names = {entry["name"] for entry in payload["entries"]}
        self.assertIn("sub", names)
        self.assertIn("@eaDir", names)
        excluded = {entry["name"]: entry for entry in payload["entries"]}
        self.assertTrue(excluded["#recycle"]["excluded"])
        self.assertEqual(excluded["#recycle"]["exclude_reason"], "system")

        status, _headers, payload = self.json_call(
            "GET", "/api/browse?path=%s&include_files=1"
            % urllib.parse.quote(self.root))
        self.assertEqual(status, 200)
        names = {entry["name"] for entry in payload["entries"]}
        self.assertIn("a1.txt", names)
        self.assertIn("data.bin", names)

    def test_settings_get_put(self):
        self.login()
        status, _headers, payload = self.json_call("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(payload["settings"]["keep_mode"], "latest")
        self.assertIn("quick_sample_kb", payload["defaults"])

        status, _headers, payload = self.json_call(
            "PUT", "/api/settings", {"settings": {"keep_mode": "earliest",
                                                  "concurrency": 4}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["settings"]["keep_mode"], "earliest")
        status, _headers, payload = self.json_call("GET", "/api/settings")
        self.assertEqual(payload["settings"]["concurrency"], 4)

        status, _headers, payload = self.json_call(
            "PUT", "/api/settings", {"settings": {"unknown_key": 1}})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")
        self.assertIn("unknown_key",
                      payload["error"]["details"]["unknown_keys"])

        status, _headers, payload = self.json_call(
            "PUT", "/api/settings", {"settings": {"skip_symlinks": False}})
        self.assertEqual(status, 200)
        self.assertTrue(payload["settings"]["skip_symlinks"])

    def test_external_ffmpeg_settings_meta_diagnostics(self):
        """v0021 P1：外部 FFmpeg 路径校验 + meta/diagnostics 能力字段。"""
        self.login()
        status, _headers, payload = self.json_call(
            "PUT", "/api/settings", {"settings": {"ffmpeg_path": "ffmpeg"}})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(payload["error"]["details"].get("key"),
                         "ffmpeg_path")

        fake_ffmpeg = os.path.join(self.harness.tmpdir, "ffmpeg_bin")
        shutil.copyfile(sys.executable, fake_ffmpeg)
        try:
            os.chmod(fake_ffmpeg, 0o755)
        except OSError:
            pass
        status, _headers, payload = self.json_call(
            "PUT", "/api/settings",
            {"settings": {"ffmpeg_path": fake_ffmpeg,
                          "video_hwaccel": "vaapi"}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["settings"]["ffmpeg_path"], fake_ffmpeg)
        self.assertEqual(payload["settings"]["video_hwaccel"], "vaapi")

        status, _headers, payload = self.json_call(
            "PUT", "/api/settings", {"settings": {"video_hwaccel": "cuda"}})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")

        status, _headers, payload = self.json_call("GET", "/api/meta")
        self.assertEqual(status, 200)
        video = payload["video"]
        self.assertEqual(video["hwaccel_mode"], "vaapi")
        self.assertIn("videotoolbox", video["hwaccel_modes"])
        self.assertEqual(video["defaults"]["ffmpeg_path"], "")
        ffmpeg = video["ffmpeg"]
        self.assertEqual(ffmpeg["configured_path"], fake_ffmpeg)
        self.assertEqual(ffmpeg["source"], "settings")
        self.assertEqual(ffmpeg["path"], fake_ffmpeg)
        self.assertIsInstance(ffmpeg["hwaccels"], list)

        status, _headers, payload = self.json_call("GET", "/api/diagnostics")
        self.assertEqual(status, 200)
        diag = payload["ffmpeg"]
        self.assertEqual(diag["configured_path"], fake_ffmpeg)
        self.assertEqual(diag["source"], "settings")
        self.assertIn(diag["hwaccel"],
                      ("none",) + tuple(config.VIDEO_HWACCEL_MODES))
        self.assertEqual(diag["hwaccel_mode"], "vaapi")

    # -- 扫描 → 结果 → CSV → 预览 → 删除 ---------------------------------
    def test_scan_groups_csv_preview_delete_flow(self):
        self.login()
        status, _headers, payload = self.json_call(
            "POST", "/api/scans", {"roots": [self.root], "excludes": [],
                                   "options": {"keep_mode": "latest"}})
        self.assertEqual(status, 202)
        scan_id = payload["scan_id"]
        self.assertTrue(scan_id)
        scan = self.wait_scan(scan_id)
        self.assertEqual(scan["status"], "completed")
        self.assertEqual(scan["stats"]["groups"], 2)
        self.assertEqual(scan["stats"]["duplicate_files"], 5)
        self.assertEqual(scan["stats"]["reclaimable_bytes"], 100 + 250 * 2)
        self.assertEqual(scan["stats"]["selectable_duplicate_files"], 3)
        self.assertEqual(scan["stats"]["selectable_reclaimable_bytes"],
                         100 + 250 * 2)
        self.assertIn("skipped", scan["stats"])
        self.assertIsNone(scan["error"])

        status, _headers, payload = self.json_call(
            "GET", "/api/scans/%s/groups?offset=0&limit=100&sort=wasted_desc"
            % scan_id)
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["limit"], 100)
        self.assertEqual(payload["exact_total"], 2)
        self.assertEqual(payload["similar_total"], 0)
        self.assertEqual(len(payload["groups"]), 2)

        status, _headers, payload = self.json_call(
            "GET", "/api/scans/%s/groups?match_type=exact" % scan_id)
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 2)
        status, _headers, payload = self.json_call(
            "GET", "/api/scans/%s/groups?match_type=similar" % scan_id)
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 0)
        self.assertEqual(payload["similar_total"], 0)

        status, _headers, payload = self.json_call(
            "GET", "/api/scans/%s/groups?offset=0&limit=100&sort=wasted_desc"
            % scan_id)
        self.assertEqual(status, 200)
        group = payload["groups"][0]
        for key in ("group_id", "hash", "algo", "size", "count",
                    "wasted_bytes", "keep_file_id", "keep_reason",
                    "needs_manual_keep", "creation_time_suspect", "files"):
            self.assertIn(key, group)
        file_info = group["files"][0]
        for key in ("file_id", "path", "name", "size", "hash", "quick_hash",
                    "created_at", "created_ts", "created_ns",
                    "created_source", "modified_at", "mtime_ns", "inode",
                    "dev", "is_keep", "default_selected"):
            self.assertIn(key, file_info)
        self.assertIn(file_info["created_source"],
                      ("statx", "birthtime", "ctime", "unknown"))

        status, _headers, payload = self.json_call(
            "GET", "/api/scans/%s/stats" % scan_id)
        self.assertEqual(status, 200)
        self.assertTrue(payload["by_extension"])
        self.assertTrue(payload["top_dirs"])

        status, headers, raw = self.client.request(
            "GET", "/api/scans/%s/csv" % scan_id)
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["content-type"][0])
        self.assertIn("attachment", headers["content-disposition"][0])
        self.assertIn("duplicate-scan-%s.csv" % scan_id,
                      headers["content-disposition"][0])
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(u"组号".encode("utf-8"), raw)
        self.assertIn(u"匹配类型".encode("utf-8"), raw)
        self.assertIn(u"相似度分值".encode("utf-8"), raw)

        status, headers, raw = self.client.request(
            "GET", "/api/preview?path=" + urllib.parse.quote(self.note))
        self.assertEqual(status, 200)
        self.assertIn("text/", headers["content-type"][0])
        self.assertIn(u"你好".encode("utf-8"), raw)
        self.assertEqual(headers["accept-ranges"][0], "bytes")

        # 文本预览必须支持 Range（契约 §8）
        preview_path = "/api/preview?path=" + urllib.parse.quote(self.note)
        status, headers, ranged = self.client.request(
            "GET", preview_path, headers={"Range": "bytes=0-4"})
        self.assertEqual(status, 206)
        self.assertEqual(ranged, raw[:5])
        self.assertEqual(headers["content-range"][0],
                         "bytes 0-4/%d" % len(raw))
        self.assertEqual(headers["accept-ranges"][0], "bytes")
        status, headers, ranged = self.client.request(
            "GET", preview_path, headers={"Range": "bytes=-4"})
        self.assertEqual(status, 206)
        self.assertEqual(ranged, raw[-4:])
        self.assertEqual(headers["content-range"][0],
                         "bytes %d-%d/%d" % (len(raw) - 4, len(raw) - 1, len(raw)))
        status, headers, _body = self.client.request(
            "GET", preview_path, headers={"Range": "bytes=999999-"})
        self.assertEqual(status, 416)
        self.assertEqual(headers["content-range"][0], "bytes */%d" % len(raw))

        status, headers, raw = self.client.request(
            "GET", "/api/preview?path="
            + urllib.parse.quote(os.path.join(self.root, "pic.png")),
            headers={"Range": "bytes=0-7"})
        self.assertEqual(status, 206)
        self.assertEqual(headers["content-range"][0], "bytes 0-7/24")
        self.assertEqual(headers["accept-ranges"][0], "bytes")
        self.assertEqual(raw, b"\x89PNG\r\n\x1a\n")

        status, _headers, payload = self.client.json(
            "GET", "/api/preview?path=" + urllib.parse.quote(
                os.path.join(self.root, "data.bin")))
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "PREVIEW_UNSUPPORTED")

        status, _headers, payload = self.json_call(
            "POST", "/api/scans/%s/delete-plan" % scan_id,
            {"scope": "all_duplicates", "recycle": True})
        self.assertEqual(status, 200)
        plan = payload["plan"]
        self.assertEqual(plan["count"], 3)
        self.assertEqual(plan["bytes"], 100 + 250 * 2)
        self.assertEqual(plan["skipped"], [])
        self.assertTrue(plan["recycle"])
        self.assertIn("plan_id", plan)
        self.assertIn("expires_at", plan)
        self.assertEqual(len(plan["files"]), 3)
        for item in plan["files"]:
            self.assertIn("file_id", item)
            self.assertIn("group_id", item)

        status, _headers, payload = self.json_call(
            "POST", "/api/scans/%s/delete" % scan_id,
            {"plan_id": plan["plan_id"], "confirm": "DELETE"})
        self.assertEqual(status, 202)
        job_id = payload["job_id"]
        self.assertTrue(job_id.startswith("del-"))

        status, _headers, payload = self.json_call(
            "GET", "/api/scans/%s/delete/%s" % (scan_id, job_id))
        self.assertEqual(status, 200)
        job = self.wait_job(scan_id, job_id)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["total"], 3)
        self.assertEqual(job["done"], 3)
        self.assertEqual(job["failed"], 0)
        self.assertEqual(job["progress"], 1.0)
        self.assertIn("log_file", job)
        self.assertTrue(job["log_file"].startswith("deletions/"))
        for result in job["results"]:
            self.assertIn(result["status"], ("recycled", "deleted"))
            self.assertIn("recycle_path", result)

        scan_files = []
        for group in self.harness.groups(scan_id):
            scan_files.extend(group["files"])
        keepers = [item for item in scan_files if item["is_keep"]]
        duplicates = [item for item in scan_files if not item["is_keep"]]
        for item in keepers:
            self.assertTrue(os.path.exists(item["path"]))
        for item in duplicates:
            self.assertFalse(os.path.exists(item["path"]))

        status, _headers, payload = self.json_call("GET", "/api/logs")
        self.assertEqual(status, 200)
        log_names = [item["name"] for item in payload["logs"]]
        self.assertIn("scan-%s.log" % scan_id, log_names)
        self.assertIn("delete-%s.log" % job_id, log_names)
        self.assertTrue(payload["audit"])
        actions = [item["action"] for item in payload["audit"]]
        self.assertIn("scan_start", actions)
        self.assertIn("delete_start", actions)

        status, headers, raw = self.client.request(
            "GET", "/api/logs/download?name=scan-%s.log" % scan_id)
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["content-disposition"][0])

        status, _headers, payload = self.client.json(
            "GET", "/api/logs/download?name="
            + urllib.parse.quote("../audit.jsonl"))
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")

    def test_preview_text_range(self):
        self.login()
        preview_path = "/api/preview?path=" + urllib.parse.quote(self.note)
        status, headers, raw = self.client.request("GET", preview_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["accept-ranges"][0], "bytes")
        total = len(raw)

        status, headers, chunk = self.client.request(
            "GET", preview_path, headers={"Range": "bytes=2-6"})
        self.assertEqual(status, 206)
        self.assertEqual(chunk, raw[2:7])
        self.assertEqual(headers["content-range"][0],
                         "bytes 2-6/%d" % total)
        self.assertEqual(headers["content-length"][0], "5")

        status, headers, chunk = self.client.request(
            "GET", preview_path, headers={"Range": "bytes=0-"})
        self.assertEqual(status, 206)
        self.assertEqual(chunk, raw)
        self.assertEqual(headers["content-range"][0],
                         "bytes 0-%d/%d" % (total - 1, total))

        status, headers, _chunk = self.client.request(
            "GET", preview_path, headers={"Range": "bytes=abc"})
        # 无法解析的 Range 按规范忽略，返回完整内容
        self.assertEqual(status, 200)

        status, headers, _chunk = self.client.request(
            "GET", preview_path, headers={"Range": "bytes=999999-"})
        self.assertEqual(status, 416)
        self.assertEqual(headers["content-range"][0], "bytes */%d" % total)

    def test_similarity_api_flow(self):
        self.login()
        base = BASE_TEXT
        variant = VARIANT_TEXT
        first = write_bytes(os.path.join(self.root, "sim_api_a.txt"),
                            base.encode("utf-8"))
        second = write_bytes(os.path.join(self.root, "sim_api_b.txt"),
                             variant.encode("utf-8"))
        self.harness.set_creation(first, 3000)
        self.harness.set_creation(second, 3100)

        status, _headers, payload = self.json_call(
            "POST", "/api/scans",
            {"roots": [self.root],
             "options": {"scan_mode": "similar",
                         "similarity_threshold": 85}})
        self.assertEqual(status, 202)
        scan_id = payload["scan_id"]
        scan = self.wait_scan(scan_id)
        self.assertEqual(scan["status"], "completed")
        self.assertEqual(scan["options"]["scan_mode"], "similar")
        stats = scan["stats"]
        self.assertEqual(stats["exact_groups"], 2)
        self.assertEqual(stats["similar_groups"], 1)
        self.assertGreater(stats["similar_reclaimable_bytes"], 0)

        status, _headers, payload = self.json_call(
            "GET", "/api/scans/%s/groups?match_type=similar" % scan_id)
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["exact_total"], 2)
        self.assertEqual(payload["similar_total"], 1)
        group = payload["groups"][0]
        self.assertEqual(group["match_type"], "similar")
        self.assertEqual(group["hash"], "")
        self.assertTrue(group["manual_review_required"])
        self.assertFalse(group["low_confidence"])
        self.assertGreaterEqual(group["similarity_score"], 85)
        for file_info in group["files"]:
            self.assertFalse(file_info["default_selected"])
        keeper = [item for item in group["files"] if item["is_keep"]][0]
        non_keeper = [item for item in group["files"] if not item["is_keep"]][0]

        status, headers, raw = self.client.request(
            "GET", "/api/scans/%s/csv" % scan_id)
        self.assertEqual(status, 200)
        self.assertIn(u"匹配类型".encode("utf-8"), raw)
        self.assertIn(u"内容相似".encode("utf-8"), raw)

        status, _headers, payload = self.json_call(
            "GET", "/api/scans/%s/groups?sort=similarity_desc" % scan_id)
        self.assertEqual(status, 200)
        self.assertTrue(payload["groups"])

        # 相似文件未复核 → 400 SIMILAR_REVIEW_REQUIRED
        status, _headers, payload = self.json_call(
            "POST", "/api/scans/%s/delete-plan" % scan_id,
            {"file_ids": [non_keeper["file_id"]], "recycle": True})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "SIMILAR_REVIEW_REQUIRED")

        status, _headers, payload = self.json_call(
            "POST", "/api/scans/%s/delete-plan" % scan_id,
            {"file_ids": [non_keeper["file_id"]], "recycle": True,
             "confirm_similar": True,
             "reviewed_similar_group_ids": [group["group_id"]]})
        self.assertEqual(status, 200)
        plan = payload["plan"]
        self.assertEqual(plan["similar_count"], 1)
        self.assertEqual(plan["exact_count"], 0)
        self.assertTrue(plan["manual_review"])
        self.assertEqual(plan["files"][0]["match_type"], "similar")

        # scope=all_duplicates 只包含精准组
        status, _headers, payload = self.json_call(
            "POST", "/api/scans/%s/delete-plan" % scan_id,
            {"scope": "all_duplicates", "recycle": True})
        self.assertEqual(status, 200)
        self.assertEqual(payload["plan"]["similar_count"], 0)
        self.assertEqual(payload["plan"]["exact_count"], 3)

        # 相似组删除执行（回收站）
        status, _headers, payload = self.json_call(
            "POST", "/api/scans/%s/delete" % scan_id,
            {"plan_id": plan["plan_id"], "confirm": "DELETE"})
        self.assertEqual(status, 202)
        job = self.wait_job(scan_id, payload["job_id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["results"][0]["match_type"], "similar")
        self.assertFalse(os.path.exists(non_keeper["path"]))
        self.assertTrue(os.path.exists(keeper["path"]))

    def test_scan_invalid_root(self):
        self.login()
        status, _headers, payload = self.json_call(
            "POST", "/api/scans", {"roots": ["/etc"]})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "PATH_NOT_ALLOWED")
        status, _headers, payload = self.json_call(
            "POST", "/api/scans", {"roots": []})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")
        status, _headers, payload = self.json_call(
            "POST", "/api/scans", {"roots": [self.root],
                                   "excludes": [self.harness.tmpdir]})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")

    def test_bad_json_body(self):
        self.login()
        status, headers, raw = self.client.request(
            "PUT", "/api/settings", body=b"{bad json",
            headers={"Content-Type": "application/json"}, csrf=self.csrf)
        self.assertEqual(status, 400)
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")

    def test_unknown_api_route(self):
        self.login()
        status, _headers, payload = self.json_call("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")

    # -- 静态资源 -------------------------------------------------------
    def test_static_files_and_spa_fallback(self):
        status, headers, raw = self.client.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("DC-TEST-INDEX", raw.decode("utf-8"))
        self.assertIn("text/html", headers["content-type"][0])
        self.assertIn("no-cache", headers["cache-control"][0])
        self.assertIn("content-security-policy", headers)

        status, headers, raw = self.client.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"__DC_TEST__", raw)

        status, headers, raw = self.client.request("GET", "/some/spa/route")
        self.assertEqual(status, 200)
        self.assertIn("DC-TEST-INDEX", raw.decode("utf-8"))

        status, headers, raw = self.client.request("GET", "/../etc/passwd")
        self.assertEqual(status, 404)


class LogsDeleteApiTest(ApiTest):
    """v0038：操作日志删除（选中 / 保留最近 N 天）与日志文件删除。"""

    def _audit(self, limit=80):
        status, _headers, payload = self.json_call(
            "GET", "/api/logs?limit=%d" % limit)
        self.assertEqual(status, 200)
        return payload.get("audit") or []

    def test_delete_selected_audit_entry(self):
        self.login()
        self.json_call("PUT", "/api/settings",
                       {"settings": {"keep_mode": "latest"}})
        audit = self._audit()
        self.assertTrue(audit)
        target = audit[-1]
        self.assertTrue(target.get("id"))
        status, _headers, payload = self.json_call(
            "POST", "/api/logs/audit/delete", {"ids": [target["id"]]})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["deleted"], 1)
        after = self._audit()
        self.assertNotIn(target["id"], [item.get("id") for item in after])
        # 删除动作本身写入审计
        self.assertTrue(any(item.get("action") == "audit_delete"
                            for item in after))

    def test_keep_recent_days_audit(self):
        self.login()
        status, _headers, payload = self.json_call(
            "POST", "/api/logs/audit/delete", {"keep_days": 3650})
        self.assertEqual(status, 200, payload)
        self.assertGreaterEqual(payload["deleted"], 0)
        for bad in (0, 3651, True, "x"):
            status, _headers, _payload = self.json_call(
                "POST", "/api/logs/audit/delete", {"keep_days": bad})
            self.assertEqual(status, 400, bad)
        # 1 天保留：测试中刚写入的事件都保留
        status, _headers, payload = self.json_call(
            "POST", "/api/logs/audit/delete", {"keep_days": 1})
        self.assertEqual(status, 200, payload)

    def test_delete_log_files(self):
        self.login()
        store = self.harness.services.store
        name = "scan-20260101-000000-abcdef.log"
        store.write_log(name, "hello")
        status, _headers, payload = self.json_call(
            "POST", "/api/logs/files/delete", {"names": [name]})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["deleted"], 1)
        self.assertFalse(os.path.exists(os.path.join(store.logs_dir, name)))
        self.assertTrue(any(item.get("action") == "log_delete"
                            for item in self._audit()))
        # 路径穿越 / 非法文件名被拒绝
        for bad in ("../config.json", "..\\config.json", "a/b.log", ""):
            status, _headers, _payload = self.json_call(
                "POST", "/api/logs/files/delete", {"names": [bad]})
            self.assertEqual(status, 400, bad)

    def test_delete_requires_csrf(self):
        self.login()
        status, _headers, payload = self.json_call(
            "POST", "/api/logs/audit/delete",
            {"keep_days": 30}, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "CSRF_FAILED")
