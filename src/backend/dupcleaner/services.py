# -*- coding: utf-8 -*-
"""服务容器：把各模块组装成可注入的依赖集合（供 app / 测试复用）。"""

from __future__ import absolute_import

from . import config
from .auth import AuthManager
from .browse import BrowseService
from .deletion import DeletionManager
from .preview import PreviewService
from .scanner import ScanManager
from .settings import SettingsManager
from .shares import ShareService
from .store import Store


class Services(object):
    def __init__(self, runtime_config, store, settings_manager, auth_manager,
                 share_service, browse_service, preview_service, scan_manager,
                 deletion_manager):
        self.config = runtime_config
        self.store = store
        self.settings = settings_manager
        self.auth = auth_manager
        self.shares = share_service
        self.browse = browse_service
        self.preview = preview_service
        self.scanner = scan_manager
        self.deletion = deletion_manager

    def shutdown(self, timeout=5.0):
        try:
            self.scanner.stop_all(timeout=timeout)
        except Exception:
            pass
        try:
            self.deletion.stop_all(timeout=timeout)
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
    scan_manager = ScanManager(runtime_config, store, share_service,
                               settings_manager)
    deletion_manager = DeletionManager(runtime_config, store, share_service,
                                       scan_manager, settings_manager)
    return Services(runtime_config, store, settings_manager, auth_manager,
                    share_service, browse_service, preview_service,
                    scan_manager, deletion_manager)
