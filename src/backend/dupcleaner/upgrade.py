# -*- coding: utf-8 -*-
"""升级 drain 握手（docs/RESUME_CONTRACT.md §4）。

升级前 SPK 的 preupgrade 脚本创建 <var>/upgrade.drain 并最多等待 30 秒；
后端 UpgradeDrainMonitor 轮询该文件，对活动扫描触发 pause_event，等扫描
到安全点写完 checkpoint 后写 <var>/upgrade.done，让 DSM 尽快停止旧服务。

- 没有活动扫描时立即写 done（升级不必等待）。
- 不阻塞删除任务（删除不自动续跑）。
- drain 超时不伪造断点、不写 done：preupgrade 自己超时退出 0，
  DSM 停止服务后未暂停的扫描在下次启动被标记 interrupted。
- 进程正常启动时清除上一次升级残留的 drain/done 文件。
"""

from __future__ import absolute_import

import logging
import os
import threading
import time

logger = logging.getLogger("dupcleaner.upgrade")

DRAIN_FILENAME = "upgrade.drain"
DONE_FILENAME = "upgrade.done"
DEFAULT_TIMEOUT_SEC = 25.0
DEFAULT_POLL_SEC = 0.2


class UpgradeDrainMonitor(object):
    def __init__(self, data_dir, scanner, timeout=DEFAULT_TIMEOUT_SEC,
                 poll_interval=DEFAULT_POLL_SEC):
        self.data_dir = os.path.abspath(data_dir)
        self.scanner = scanner
        try:
            self.timeout = max(0.0, float(timeout))
        except (TypeError, ValueError):
            self.timeout = DEFAULT_TIMEOUT_SEC
        try:
            self.poll_interval = max(0.02, float(poll_interval))
        except (TypeError, ValueError):
            self.poll_interval = DEFAULT_POLL_SEC
        self.drain_path = os.path.join(self.data_dir, DRAIN_FILENAME)
        self.done_path = os.path.join(self.data_dir, DONE_FILENAME)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self.done_written = False
        self.timed_out = False
        self.last_result = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self):
        self.cleanup_stale()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self.done_written = False
            self.timed_out = False
            self.last_result = None
            self._thread = threading.Thread(
                target=self._loop, name="upgrade-drain", daemon=True)
            self._thread.start()

    def stop(self, timeout=5.0):
        self._stop.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.1, float(timeout)))

    def cleanup_stale(self):
        """清除上次升级残留的 drain/done，避免新进程立即再次 drain。"""
        for path in (self.drain_path, self.done_path):
            try:
                os.unlink(path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # drain 处理
    # ------------------------------------------------------------------
    def drain_requested(self):
        return os.path.isfile(self.drain_path)

    def check_once(self):
        """轮询线程/测试同步入口：有 drain 文件则处理一次，返回是否写了 done。"""
        if not self.drain_requested():
            return False
        return self._handle_drain()

    def _loop(self):
        while not self._stop.wait(self.poll_interval):
            if not self.drain_requested():
                continue
            try:
                self._handle_drain()
            except Exception:  # pragma: no cover - 监视线程兜底
                logger.warning(u"升级 drain 处理失败", exc_info=True)

    def _handle_drain(self):
        if not self.drain_requested():
            return False
        paused = False
        try:
            paused = bool(self.scanner.pause_for_upgrade(timeout=self.timeout))
        except Exception:  # pragma: no cover - 防御性
            logger.warning(u"升级 drain 暂停扫描失败", exc_info=True)
        if not paused:
            # 超时：不伪造断点、不写 done；preupgrade 30s 后自行退出 0。
            self._remove_drain()
            self.timed_out = True
            self.last_result = "timeout"
            logger.warning(
                u"升级 drain 超时（%.1fs）：活动扫描未能暂停，交由 DSM 停止服务",
                self.timeout)
            return False
        # 先移除 drain 再写 done：preupgrade 看到 done 时 drain 一定已清理。
        self._remove_drain()
        self._write_done()
        self.last_result = "done"
        return True

    def _write_done(self):
        payload = u"upgrade-drain-paused\nts=%.3f\n" % time.time()
        try:
            with open(self.done_path, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
        except OSError:  # pragma: no cover - 目录不可写时只记录日志
            logger.warning(u"写入 upgrade.done 失败: %s", self.done_path)
            return
        with self._lock:
            self.done_written = True
        logger.info(u"升级 drain：扫描已暂停并写入 upgrade.done")

    def _remove_drain(self):
        try:
            os.unlink(self.drain_path)
        except OSError:
            pass
