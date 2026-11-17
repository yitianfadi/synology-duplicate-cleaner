# -*- coding: utf-8 -*-
"""服务容器：把各模块组装成可注入的依赖集合（供 app / 测试复用）。"""

from __future__ import absolute_import

import logging
import os
import time

from . import config
from .auth import AuthManager
from .browse import BrowseService
from .convert import ConvertService
from .deletion import DeletionManager
from .downloads import DownloadService
from .download_subs import DownloadSubscriptionService
from .merge import MergeManager
from .notifications import NotificationService
from .permissions import PermissionInspector
from .preview import PreviewService
from .scanner import ScanManager
from .scheduler import SchedulerService
from .settings import SettingsManager
from .shares import ShareService
from .store import Store
from .subtitles import SubtitleService
from .subtitle_watch import SubtitleWatchService
from .thumbnails import ThumbnailService
from .trash import TrashManager
from .upgrade import UpgradeDrainMonitor

logger = logging.getLogger("dupcleaner.services")

# v0051：自动扫描去抖（同一目录 10 分钟内不重复触发）
AUTO_SCAN_DEBOUNCE_SEC = 600
DOWNLOAD_USER = "downloader"


class Services(object):
    def __init__(self, runtime_config, store, settings_manager, auth_manager,
                 share_service, browse_service, preview_service, scan_manager,
                 deletion_manager, subtitle_service=None,
                 upgrade_monitor=None, thumbnail_service=None,
                 merge_manager=None, trash_manager=None,
                 notification_service=None, scheduler_service=None,
                 subtitle_watch_service=None, permission_inspector=None,
                 download_service=None, download_subs_service=None,
                 convert_service=None):
        self.config = runtime_config
        self.store = store
        self.settings = settings_manager
        self.auth = auth_manager
        self.shares = share_service
        self.browse = browse_service
        self.preview = preview_service
        self.scanner = scan_manager
        self.deletion = deletion_manager
        self.subtitles = subtitle_service
        self.upgrade_monitor = upgrade_monitor
        self.thumbnails = thumbnail_service
        self.merges = merge_manager
        self.trash = trash_manager
        self.notify = notification_service
        self.scheduler = scheduler_service
        self.subtitle_watch = subtitle_watch_service
        self.permissions = permission_inspector
        self.downloads = download_service
        self.download_subs = download_subs_service
        self.convert = convert_service

    def shutdown(self, timeout=5.0):
        # 先停升级 drain 监视，避免正常退出时又把扫描暂停。
        try:
            if self.upgrade_monitor is not None:
                self.upgrade_monitor.stop(timeout=timeout)
        except Exception:
            pass
        try:
            self.scanner.stop_all(timeout=timeout)
        except Exception:
            pass
        try:
            self.deletion.stop_all(timeout=timeout)
        except Exception:
            pass
        try:
            if self.merges is not None:
                self.merges.stop_all(timeout=timeout)
        except Exception:
            pass
        try:
            if self.subtitles is not None:
                self.subtitles.shutdown(timeout=timeout)
        except Exception:
            pass
        try:
            if self.scheduler is not None:
                self.scheduler.stop(timeout=timeout)
        except Exception:
            pass
        try:
            if self.subtitle_watch is not None:
                self.subtitle_watch.stop(timeout=timeout)
        except Exception:
            pass
        try:
            if self.download_subs is not None:
                self.download_subs.stop(timeout=timeout)
        except Exception:
            pass
        try:
            if self.downloads is not None:
                self.downloads.shutdown(timeout=timeout)
        except Exception:
            pass
        try:
            if self.convert is not None:
                self.convert.shutdown(timeout=timeout)
        except Exception:
            pass
        try:
            if self.notify is not None:
                self.notify.stop(timeout=timeout)
        except Exception:
            pass
        # 退出前 WAL checkpoint(TRUNCATE) 并关闭 SQLite 元数据库。
        try:
            self.store.close()
        except Exception:
            pass


def _make_download_complete(scan_manager, subtitle_service, settings_manager):
    """下载完成后的可选联动（v0051）：自动入队扫描 / 自动提交字幕。"""
    last_scan = {}

    def _settings():
        try:
            return settings_manager.get() or {}
        except Exception:
            return {}

    def on_complete(job):
        settings = _settings()
        path = getattr(job, "output_path", "") or ""
        if not path or not os.path.isfile(path):
            return
        directory = os.path.dirname(path)
        if settings.get("download_auto_scan") and scan_manager is not None:
            now = time.time()
            last = float(last_scan.get(directory) or 0)
            if now - last >= AUTO_SCAN_DEBOUNCE_SEC:
                last_scan[directory] = now
                try:
                    scan_manager.start_scan([directory], [], {},
                                            DOWNLOAD_USER)
                    logger.info(u"下载完成，已入队扫描目录: %s", directory)
                except Exception as exc:
                    logger.warning(u"下载后自动扫描失败: %s", exc)
        if settings.get("download_auto_subtitle") \
                and subtitle_service is not None:
            if not settings.get("subtitle_enabled"):
                logger.info(u"下载后自动字幕跳过：云端字幕未启用")
                return
            payload = {
                "paths": [path],
                "confirm_cloud": True,
                "translate": bool(
                    settings.get("subtitle_translate_enabled")),
                "output_mode": settings.get("subtitle_output_mode"),
                "audio_track": settings.get("subtitle_audio_track") or None,
                "audio_track_language":
                    settings.get("subtitle_audio_track_language") or None,
            }
            try:
                subtitle_service.create_batch(DOWNLOAD_USER, payload)
                logger.info(u"下载完成，已入队字幕: %s", path)
            except Exception as exc:
                logger.warning(u"下载后自动字幕失败: %s", exc)

    return on_complete


def _make_convert_complete(scan_manager, subtitle_service, settings_manager):
    """转换完成后的可选联动（v0056）：自动入队扫描 / 自动提交字幕。"""
    last_scan = {}

    def _settings():
        try:
            return settings_manager.get() or {}
        except Exception:
            return {}

    def on_complete(job):
        settings = _settings()
        path = getattr(job, "output_path", "") or ""
        if not path or not os.path.isfile(path):
            return
        directory = os.path.dirname(path)
        if settings.get("convert_auto_scan") and scan_manager is not None:
            now = time.time()
            last = float(last_scan.get(directory) or 0)
            if now - last >= AUTO_SCAN_DEBOUNCE_SEC:
                last_scan[directory] = now
                try:
                    scan_manager.start_scan([directory], [], {},
                                            DOWNLOAD_USER)
                    logger.info(u"转换完成，已入队扫描目录: %s", directory)
                except Exception as exc:
                    logger.warning(u"转换后自动扫描失败: %s", exc)
        if settings.get("convert_auto_subtitle") \
                and subtitle_service is not None:
            if not settings.get("subtitle_enabled"):
                logger.info(u"转换后自动字幕跳过：云端字幕未启用")
                return
            payload = {
                "paths": [path],
                "confirm_cloud": True,
                "translate": bool(
                    settings.get("subtitle_translate_enabled")),
                "output_mode": settings.get("subtitle_output_mode"),
                "audio_track": settings.get("subtitle_audio_track") or None,
                "audio_track_language":
                    settings.get("subtitle_audio_track_language") or None,
            }
            try:
                subtitle_service.create_batch(DOWNLOAD_USER, payload)
                logger.info(u"转换完成，已入队字幕: %s", path)
            except Exception as exc:
                logger.warning(u"转换后自动字幕失败: %s", exc)

    return on_complete


def build_services(runtime_config):
    store = Store(runtime_config.data_dir)
    settings_manager = SettingsManager(store)
    auth_manager = AuthManager(runtime_config, store,
                               admin_users=runtime_config.admin_users)
    share_service = ShareService(runtime_config, dsm_client=auth_manager._dsm)
    browse_service = BrowseService(runtime_config, share_service,
                                   settings_provider=settings_manager.get)
    preview_service = PreviewService(share_service)
    thumbnail_service = ThumbnailService(
        runtime_config, settings_provider=settings_manager.get)
    # v0046：通知服务（扫描/删除/字幕批次事件）+ 计划扫描调度
    notification_service = NotificationService(
        runtime_config, store, settings_manager.get)
    scan_manager = ScanManager(runtime_config, store, share_service,
                               settings_manager, notifier=notification_service)
    trash_manager = TrashManager(runtime_config, store, share_service)
    deletion_manager = DeletionManager(runtime_config, store, share_service,
                                       scan_manager, settings_manager,
                                       trash_manager=trash_manager,
                                       notifier=notification_service)
    merge_manager = MergeManager(runtime_config, store, share_service,
                                 scan_manager, settings_manager)
    subtitle_service = SubtitleService(runtime_config, store, share_service,
                                       settings_manager.get)
    # v0048：字幕目录监控（新增视频自动入队）
    subtitle_watch_service = SubtitleWatchService(
        runtime_config, store, settings_manager.get,
        subtitle_service=subtitle_service)
    subtitle_watch_service.start()
    scheduler_service = SchedulerService(
        runtime_config, store, settings_manager.get, scan_manager,
        notifier=notification_service,
        subtitles_provider=lambda: subtitle_service)
    scheduler_service.start()
    # v0049：权限与路径体检（只读检测 + 指引，不提权）
    permission_inspector = PermissionInspector(
        runtime_config, store, share_service, settings_manager.get,
        subtitle_service=subtitle_service)
    # v0051：下载中心（直链 / HLS；完成后可选自动入队扫描或字幕）
    download_service = DownloadService(
        runtime_config, store, share_service, settings_manager.get,
        on_complete=_make_download_complete(
            scan_manager, subtitle_service, settings_manager),
        notifier=notification_service)
    download_service.start()
    # v0054：下载订阅（RSS/网页定时检查 → 自动入队）
    download_subs_service = DownloadSubscriptionService(
        runtime_config, store, settings_manager.get, download_service)
    download_subs_service.start()
    # v0056：格式转换（视频/音频互转 + 仅换封装；完成后可选自动扫描/字幕）
    convert_service = ConvertService(
        runtime_config, store, share_service, settings_manager.get,
        on_complete=_make_convert_complete(
            scan_manager, subtitle_service, settings_manager),
        notifier=notification_service)
    convert_service.start()
    # 升级 drain 握手：preupgrade 写 <var>/upgrade.drain，后端暂停扫描后写 done。
    upgrade_monitor = UpgradeDrainMonitor(runtime_config.data_dir, scan_manager)
    upgrade_monitor.start()
    return Services(runtime_config, store, settings_manager, auth_manager,
                    share_service, browse_service, preview_service,
                    scan_manager, deletion_manager, subtitle_service,
                    upgrade_monitor, thumbnail_service, merge_manager,
                    trash_manager, notification_service, scheduler_service,
                    subtitle_watch_service, permission_inspector,
                    download_service, download_subs_service,
                    convert_service)
