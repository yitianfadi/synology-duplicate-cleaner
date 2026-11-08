# -*- coding: utf-8 -*-
"""服务容器：把各模块组装成可注入的依赖集合（供 app / 测试复用）。"""

from __future__ import absolute_import

from . import config
from .auth import AuthManager
from .browse import BrowseService
from .deletion import DeletionManager
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


class Services(object):
    def __init__(self, runtime_config, store, settings_manager, auth_manager,
                 share_service, browse_service, preview_service, scan_manager,
                 deletion_manager, subtitle_service=None,
                 upgrade_monitor=None, thumbnail_service=None,
                 merge_manager=None, trash_manager=None,
                 notification_service=None, scheduler_service=None,
                 subtitle_watch_service=None, permission_inspector=None):
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
            if self.notify is not None:
                self.notify.stop(timeout=timeout)
        except Exception:
            pass
        # 退出前 WAL checkpoint(TRUNCATE) 并关闭 SQLite 元数据库。
        try:
            self.store.close()
        except Exception:
            pass


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
    # 升级 drain 握手：preupgrade 写 <var>/upgrade.drain，后端暂停扫描后写 done。
    upgrade_monitor = UpgradeDrainMonitor(runtime_config.data_dir, scan_manager)
    upgrade_monitor.start()
    return Services(runtime_config, store, settings_manager, auth_manager,
                    share_service, browse_service, preview_service,
                    scan_manager, deletion_manager, subtitle_service,
                    upgrade_monitor, thumbnail_service, merge_manager,
                    trash_manager, notification_service, scheduler_service,
                    subtitle_watch_service, permission_inspector)
