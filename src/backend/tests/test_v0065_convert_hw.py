# -*- coding: utf-8 -*-
"""v0065 硬件编解码：VAAPI 预设参数、探测、环境变量与 CPU 回退。

NAS 上（DS920+）实测参考值（720p/1080p 合成源）：
- iHD HEVC 硬解 + H.264 硬编（全 GPU）：9.07×
- iHD H.264 硬编 1080p：3.49×
- i965 H.264 硬编 1080p：2.02×
- CPU libx264 medium 1080p：约 1.0×
"""

from __future__ import absolute_import

import os
import shutil
import tempfile
import unittest

from tests.test_v0056_convert import (_FakeRuntime, _FakeShares, _FakeTool,
                                      _probe_info)
from tests.helpers import Harness  # noqa: F401  （保持与其他转换测试一致的导入）

from dupcleaner import config, convert, hwaccel, settings as settings_module
from dupcleaner.errors import ApiError
from dupcleaner.store import Store
from dupcleaner.trash import TrashManager

DEVICE = "/dev/dri/renderD128"


class BuildArgsHwTest(unittest.TestCase):
    def setUp(self):
        self.info = _probe_info()
        self.options = {"quality": "standard", "speed": "veryfast",
                        "max_resolution": 0, "channels": ""}
        self.hw = {"device": DEVICE, "decode": False}

    def _args(self, preset_id, hw=None, options=None):
        return convert.build_args(
            convert.PRESET_MAP[preset_id], self.info,
            options or self.options, "/src/a.mkv", "/out/a.mp4",
            hw=self.hw if hw is None else hw)

    def test_h264_hw_sw_decode(self):
        args = self._args("mp4_h264_hw")
        self.assertIn("-vaapi_device", args)
        self.assertEqual(args[args.index("-vaapi_device") + 1], DEVICE)
        self.assertLess(args.index("-vaapi_device"), args.index("-i"))
        self.assertNotIn("-hwaccel", args)
        self.assertEqual(args[args.index("-c:v") + 1], "h264_vaapi")
        self.assertEqual(args[args.index("-rc_mode") + 1], "CQP")
        self.assertEqual(args[args.index("-qp") + 1], "24")
        self.assertEqual(args[args.index("-vf") + 1], "format=nv12,hwupload")
        self.assertNotIn("-pix_fmt", args)
        self.assertNotIn("-preset", args)
        self.assertEqual(args[args.index("-f") + 1], "mp4")

    def test_h264_hw_full_gpu(self):
        args = self._args("mp4_h264_hw",
                          hw={"device": DEVICE, "decode": True})
        self.assertIn("-hwaccel", args)
        self.assertEqual(args[args.index("-hwaccel") + 1], "vaapi")
        self.assertEqual(args[args.index("-hwaccel_device") + 1], DEVICE)
        self.assertEqual(
            args[args.index("-hwaccel_output_format") + 1], "vaapi")
        self.assertEqual(args[args.index("-vf") + 1], "scale_vaapi=format=nv12")

    def test_hw_scale_with_max_resolution(self):
        options = dict(self.options, max_resolution=720)
        sw = self._args("mp4_h264_hw", hw=self.hw, options=options)
        self.assertEqual(sw[sw.index("-vf") + 1],
                         "scale=-2:min(720\\,ih),format=nv12,hwupload")
        full = self._args("mp4_h264_hw",
                          hw={"device": DEVICE, "decode": True},
                          options=options)
        self.assertEqual(full[full.index("-vf") + 1],
                         "scale_vaapi=w=-2:h=720:format=nv12")

    def test_hevc_hw_preset(self):
        args = self._args("mp4_h265_hw", hw={"device": DEVICE, "decode": True})
        self.assertEqual(args[args.index("-c:v") + 1], "hevc_vaapi")
        self.assertEqual(args[args.index("-qp") + 1], "26")
        self.assertIn("hvc1", args)

    def test_copy_subtitles_for_mkv_hw(self):
        args = self._args("mkv_h264_hw")
        self.assertIn("-c:s", args)
        self.assertEqual(args[args.index("-f") + 1], "matroska")

    def test_missing_device_raises(self):
        with self.assertRaises(convert.ConvertError) as ctx:
            self._args("mp4_h264_hw", hw={})
        self.assertEqual(ctx.exception.code, "HWACCEL_UNAVAILABLE")


class PresetViewHwTest(unittest.TestCase):
    def test_unavailable_hardware_marks_preset(self):
        view = convert.preset_view(convert.PRESET_MAP["mp4_h264_hw"],
                                   {"h264_vaapi", "aac"},
                                   {"available": False, "reason": u"没有核显"})
        self.assertFalse(view["available"])
        self.assertTrue(view["hw"])
        self.assertEqual(view["reason"], u"没有核显")

    def test_available_hardware_keeps_preset(self):
        view = convert.preset_view(convert.PRESET_MAP["mp4_h264_hw"],
                                   {"h264_vaapi", "aac"},
                                   {"available": True})
        self.assertTrue(view["available"])
        self.assertEqual(view["reason"], u"")

    def test_cpu_preset_ignores_hardware(self):
        view = convert.preset_view(convert.PRESET_MAP["mp4_h264"],
                                   {"libx264", "aac"},
                                   {"available": False})
        self.assertTrue(view["available"])
        self.assertFalse(view["hw"])

    def test_missing_encoder_marks_preset(self):
        view = convert.preset_view(convert.PRESET_MAP["mp4_h264_hw"],
                                   {"aac"}, {"available": True})
        self.assertFalse(view["available"])
        self.assertIn("h264_vaapi", view["reason"])


class HwaccelDetectTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-hw-")
        self.bundle = os.path.join(self.tmpdir, "vaapi")
        os.makedirs(os.path.join(self.bundle, "dri"))
        self.device = os.path.join(self.tmpdir, "renderD128")
        with open(self.device, "wb") as handle:
            handle.write(b"x")
        self._old_candidates = config.VAAPI_DEVICE_CANDIDATES
        config.VAAPI_DEVICE_CANDIDATES = (self.device,)
        hwaccel.reset_cache()

    def tearDown(self):
        config.VAAPI_DEVICE_CANDIDATES = self._old_candidates
        hwaccel.reset_cache()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _touch(self, *names):
        for name in names:
            path = os.path.join(self.bundle, *name.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b"x")

    def test_ihd_bundle(self):
        self._touch("libva.so.2", "libva-drm.so.2", "libigdgmm.so.12",
                    "dri/iHD_drv_video.so")
        os.environ["DUP_VAAPI_DIR"] = self.bundle
        try:
            info = hwaccel.detect(force=True)
            self.assertTrue(info["available"])
            self.assertEqual(info["driver"], "iHD")
            environ = hwaccel.env()
            self.assertEqual(environ["LIBVA_DRIVER_NAME"], "iHD")
            self.assertEqual(environ["LIBVA_DRIVERS_PATH"],
                             os.path.join(self.bundle, "dri"))
            self.assertIn(self.bundle, environ["LD_LIBRARY_PATH"])
            self.assertTrue(hwaccel.codec_supported("hevc"))
            self.assertTrue(hwaccel.encoder_supported("hevc_vaapi"))
        finally:
            os.environ.pop("DUP_VAAPI_DIR", None)

    def test_ihd_without_gmm_falls_back_to_i965(self):
        self._touch("libva.so.2", "dri/iHD_drv_video.so",
                    "dri/i965_drv_video.so")
        os.environ["DUP_VAAPI_DIR"] = self.bundle
        try:
            info = hwaccel.detect(force=True)
            self.assertEqual(info["driver"], "i965")
            self.assertTrue(info["available"])
            self.assertFalse(hwaccel.codec_supported("hevc"))
            self.assertTrue(hwaccel.codec_supported("h264"))
        finally:
            os.environ.pop("DUP_VAAPI_DIR", None)

    def test_no_driver(self):
        os.environ["DUP_VAAPI_DIR"] = self.bundle
        try:
            info = hwaccel.detect(force=True)
            self.assertFalse(info["available"])
            self.assertIn(u"VAAPI 驱动", info["reason"])
            self.assertEqual(hwaccel.env(), {})
        finally:
            os.environ.pop("DUP_VAAPI_DIR", None)


class HwFallbackTest(unittest.TestCase):
    """硬件预设执行失败 → 自动改用 CPU 预设重跑一次。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dc-cvhw-")
        self.share = os.path.join(self.tmpdir, "share")
        os.makedirs(self.share)
        self.store = Store(os.path.join(self.tmpdir, "data"))
        self.settings = settings_module.default_settings()
        self.settings.update({
            "convert_enabled": True,
            "convert_min_free_gb": 0,
            "convert_output_mode": "alongside",
            "convert_concurrency": 1,
            "convert_hwaccel": "auto",
        })
        self.shares = _FakeShares(self.share)
        self.tool = _FakeTool()
        self.tool.encoders = lambda: ["libx264", "libx265", "aac",
                                      "h264_vaapi", "hevc_vaapi"]
        self.trash = TrashManager(
            _FakeRuntime(os.path.join(self.tmpdir, "data")), self.store,
            self.shares)
        self.service = convert.ConvertService(
            None, self.store, self.shares,
            lambda: dict(self.settings), ffmpeg_tool=self.tool,
            trash_manager=self.trash)
        self._old_detect = hwaccel.detect
        hwaccel.detect = lambda force=False: {
            "available": True, "device": DEVICE, "driver": "iHD",
            "drivers_path": "/x/dri", "libs_path": "/x", "reason": u"",
        }
        self._old_env = hwaccel.env
        hwaccel.env = lambda info=None: {"LIBVA_DRIVER_NAME": "iHD"}
        self.tool.probe_vaapi = lambda: {"ok": True, "message": u"ok"}

    def tearDown(self):
        hwaccel.detect = self._old_detect
        hwaccel.env = self._old_env
        self.service.shutdown(timeout=0.2)
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, preset_id, exit_codes):
        source = os.path.join(self.share, "movie.mkv")
        with open(source, "wb") as handle:
            handle.write(b"v" * 2048)
        codes = list(exit_codes)

        def run_stream(args, on_line, timeout, stop_check=None, **kwargs):
            code = codes.pop(0) if codes else 0
            target = args[-1]
            with open(target, "wb") as handle:
                handle.write(b"x" * 4096)
            return code, b"vaapi error" if code else b""

        self.tool.run_stream = run_stream
        created = self.service.create(
            "admin", {"paths": [source], "preset": preset_id})
        job = self.service._jobs[created["jobs"][0]["job_id"]]
        self.service._execute(job)
        return job

    def test_falls_back_to_cpu_preset(self):
        job = self._run("mp4_h264_hw", [1])
        self.assertEqual(job.status, "done", job.error_message)
        self.assertTrue(job.hw_fallback)
        self.assertEqual(job.preset, "mp4_h264_hw")

    def test_success_without_fallback(self):
        job = self._run("mp4_h264_hw", [0])
        self.assertEqual(job.status, "done", job.error_message)
        self.assertFalse(job.hw_fallback)


if __name__ == "__main__":
    unittest.main()
