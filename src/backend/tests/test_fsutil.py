# -*- coding: utf-8 -*-
import os
import unittest

from tests.helpers import Harness, write_bytes, write_text

from dupcleaner import fsutil
from dupcleaner.errors import ApiError


class PathSafetyTest(unittest.TestCase):
    def test_system_and_hidden_names(self):
        for name in ("@eaDir", "@tmp", "@docker", "#recycle", "#snapshot",
                     ".SynoFSFolder", "Thumbs.db", "desktop.ini"):
            self.assertTrue(fsutil.is_system_name(name), name)
        self.assertFalse(fsutil.is_system_name("photo"))
        self.assertFalse(fsutil.is_system_name("a.jpg"))
        self.assertTrue(fsutil.is_hidden_name(".hidden"))
        self.assertFalse(fsutil.is_hidden_name("visible"))

    def test_blocked_absolute_paths(self):
        blocked = [
            "/etc", "/etc/passwd", "/usr/bin", "/var/log", "/root/.ssh",
            "/volume1", "/volume1/@eaDir", "/volume1/photo/#recycle",
            "/volume1/photo/#snapshot/x", "/volume1/photo/@docker",
            "/volume1/photo/.SynoFSFolder", "relative/path", "",
            "/volume1/photo/.hidden/x", "/volume1/photo/@eaDir/x",
        ]
        for path in blocked:
            with self.assertRaises(ApiError, msg=path) as ctx:
                fsutil.check_path_safety(path)
            self.assertIn(ctx.exception.code, ("INVALID_PATH", "PATH_NOT_ALLOWED"))
        self.assertEqual(fsutil.check_path_safety("/volume1/photo/2024"),
                         os.path.normpath("/volume1/photo/2024"))

    def test_allow_components_for_recycle(self):
        normalized = fsutil.check_path_safety(
            "/volume1/photo/#recycle/@duplicate-cleaner/20260101/a.jpg",
            allow_components={"#recycle", "@duplicate-cleaner"})
        self.assertTrue(normalized.endswith("a.jpg"))

    def test_windows_drive_root_blocked(self):
        if os.name != "nt":
            self.skipTest("only on windows")
        with self.assertRaises(ApiError):
            fsutil.check_path_safety("C:\\")
        with self.assertRaises(ApiError):
            fsutil.check_path_safety("C:\\Windows\\System32")

    def test_is_under(self):
        self.assertTrue(fsutil.is_under("/volume1/photo/a", "/volume1/photo"))
        self.assertTrue(fsutil.is_under("/volume1/photo", "/volume1/photo"))
        self.assertFalse(fsutil.is_under("/volume1/photo2", "/volume1/photo"))
        self.assertFalse(fsutil.is_under("/volume2/photo", "/volume1/photo"))

    def test_symlink_component(self):
        harness = Harness()
        try:
            target = os.path.join(harness.dev_root, "target")
            os.makedirs(target)
            link = os.path.join(harness.dev_root, "link")
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError, AttributeError):
                self.skipTest("symlink not supported")
            self.assertTrue(fsutil.has_symlink_component(link, harness.dev_root))
            self.assertFalse(fsutil.has_symlink_component(target, harness.dev_root))
        finally:
            harness.cleanup()


class BrowsePreviewTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        root = self.harness.dev_root
        write_bytes(os.path.join(root, "a.txt"), b"hello duplicate cleaner")
        write_bytes(os.path.join(root, "pic.png"),
                    b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        write_bytes(os.path.join(root, "data.bin"), b"\x00\x01\x02binary")
        os.makedirs(os.path.join(root, "sub"))
        write_text(os.path.join(root, "sub", "b.txt"), u"内容")
        os.makedirs(os.path.join(root, "@eaDir"))
        write_text(os.path.join(root, "@eaDir", "thumb.txt"), u"x")
        os.makedirs(os.path.join(root, ".hidden"))
        self.root = root

    def tearDown(self):
        self.harness.cleanup()

    def test_empty_browse_lists_shares(self):
        result = self.harness.services.browse.browse("")
        self.assertEqual(result["path"], "")
        self.assertIsNone(result["parent"])
        self.assertEqual(result["breadcrumbs"], [])
        self.assertEqual(len(result["shares"]), 1)
        self.assertEqual(result["entries"], [])

    def test_browse_dirs_only_and_excluded_markers(self):
        result = self.harness.services.browse.browse(self.root)
        names = {entry["name"]: entry for entry in result["entries"]}
        self.assertIn("sub", names)
        self.assertTrue(names["@eaDir"]["excluded"])
        self.assertEqual(names["@eaDir"]["exclude_reason"], "system")
        self.assertTrue(names[".hidden"]["excluded"])
        self.assertEqual(names[".hidden"]["exclude_reason"], "hidden")
        self.assertNotIn("a.txt", names)

    def test_browse_include_files(self):
        result = self.harness.services.browse.browse(self.root, include_files=True)
        names = {entry["name"]: entry for entry in result["entries"]}
        self.assertIn("a.txt", names)
        self.assertEqual(names["a.txt"]["size"], 23)
        self.assertEqual(names["a.txt"]["type"], "file")
        self.assertIsNotNone(names["a.txt"]["modified_at"])
        # 目录在前
        self.assertEqual(result["entries"][0]["type"], "dir")

    def test_browse_breadcrumbs(self):
        result = self.harness.services.browse.browse(
            os.path.join(self.root, "sub"))
        crumb_names = [item["name"] for item in result["breadcrumbs"]]
        self.assertEqual(crumb_names[-1], "sub")
        self.assertEqual(len(crumb_names), 2)
        self.assertEqual(result["parent"], self.root)

    def test_browse_outside_share_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            self.harness.services.browse.browse(self.harness.tmpdir)
        self.assertEqual(ctx.exception.code, "PATH_NOT_ALLOWED")

    def test_preview_text(self):
        info = self.harness.services.preview.resolve(
            os.path.join(self.root, "a.txt"))
        self.assertEqual(info["kind"], "text")
        data, truncated = self.harness.services.preview.read_text(info)
        self.assertEqual(data, b"hello duplicate cleaner")
        self.assertFalse(truncated)

    def test_preview_image_and_unsupported(self):
        info = self.harness.services.preview.resolve(
            os.path.join(self.root, "pic.png"))
        self.assertEqual(info["kind"], "image")
        self.assertEqual(info["content_type"], "image/png")
        with self.assertRaises(ApiError) as ctx:
            self.harness.services.preview.resolve(
                os.path.join(self.root, "data.bin"))
        self.assertEqual(ctx.exception.code, "PREVIEW_UNSUPPORTED")

    def test_preview_directory_and_outside(self):
        with self.assertRaises(ApiError) as ctx:
            self.harness.services.preview.resolve(os.path.join(self.root, "sub"))
        self.assertEqual(ctx.exception.code, "NOT_FOUND")
        outside = os.path.join(self.harness.tmpdir, "outside.txt")
        write_text(outside, u"x")
        with self.assertRaises(ApiError) as ctx:
            self.harness.services.preview.resolve(outside)
        self.assertEqual(ctx.exception.code, "PATH_NOT_ALLOWED")


class ShareServiceTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.root = self.harness.dev_root
        write_text(os.path.join(self.root, "a.txt"), u"a")
        os.makedirs(os.path.join(self.root, "sub"))
        os.makedirs(os.path.join(self.root, "@eaDir"))
        self.shares = self.harness.services.shares

    def tearDown(self):
        self.harness.cleanup()

    def test_dev_shares(self):
        shares = self.shares.list_shares()
        self.assertEqual(len(shares), 1)
        share = shares[0]
        self.assertEqual(share["path"], os.path.normpath(self.root))
        self.assertEqual(share["name"], os.path.basename(os.path.normpath(self.root)) or share["name"])
        self.assertTrue(share["is_writable"])
        self.assertEqual(share["filesystem"], "dev")
        self.assertIsNotNone(self.shares.find_share(os.path.join(self.root, "a.txt")))

    def test_validate_root(self):
        share = self.shares.validate_root(self.root)
        self.assertEqual(share["path"], os.path.normpath(self.root))
        self.shares.validate_root(os.path.join(self.root, "sub"))
        for bad in (os.path.join(self.root, "@eaDir"),
                    os.path.join(self.root, "#recycle"),
                    "/etc", self.harness.tmpdir):
            with self.assertRaises(ApiError, msg=bad) as ctx:
                self.shares.validate_root(bad)
            self.assertIn(ctx.exception.code, ("PATH_NOT_ALLOWED", "INVALID_ARGUMENT"))

    def test_validate_file(self):
        self.shares.validate_file(os.path.join(self.root, "a.txt"))
        with self.assertRaises(ApiError):
            self.shares.validate_file(os.path.join(self.root, "sub"))
        with self.assertRaises(ApiError):
            self.shares.validate_file(os.path.join(self.root, "missing.txt"))

    def test_recycle_dir_detection(self):
        share = self.shares.list_shares()[0]
        self.assertFalse(self.shares.has_recycle_dir(share))
        os.makedirs(os.path.join(self.root, "#recycle"))
        self.assertTrue(self.shares.has_recycle_dir(share))
        self.assertEqual(self.shares.recycle_dir(share),
                         os.path.join(self.root, "#recycle"))
