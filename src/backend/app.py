# -*- coding: utf-8 -*-
"""DuplicateCleaner 启动入口。

用法（契约 §2 + Lead CLI 约定）：
  python app.py --host 0.0.0.0 --port 8787 \
      --data-dir /var/packages/DuplicateCleaner/var \
      --web-dir /var/packages/DuplicateCleaner/target/web \
      [--tls-cert PATH --tls-key PATH] [--dev]

默认：host 127.0.0.1、port 8787、data-dir ./data、web-dir ../web（相对本文件）。
生产 SPK 启动脚本不得传 --dev、不得设置 DUP_CLEANER_DEV。
"""

from __future__ import absolute_import

import argparse
import logging
import os
import signal
import ssl
import sys

from dupcleaner import config
from dupcleaner.httpapi import Api
from dupcleaner.server import DcHTTPServer, DcRequestHandler
from dupcleaner.services import build_services

logger = logging.getLogger("dupcleaner")


def build_config(args, environ=None):
    host = args.host if args.host is not None else None
    port = args.port if args.port is not None else None
    runtime_config = config.RuntimeConfig(
        data_dir=args.data_dir,
        web_dir=args.web_dir,
        dev_mode=True if args.dev else None,
        tls_cert=args.tls_cert,
        tls_key=args.tls_key,
        host=host,
        port=port,
        environ=environ,
    )
    return runtime_config


def create_server(runtime_config=None, services=None):
    """构建 HTTP(S) 服务器（测试可直接调用并 shutdown）。"""
    cfg = runtime_config or config.RuntimeConfig()
    services = services or build_services(cfg)
    api = Api(services)
    httpd = DcHTTPServer((cfg.host, cfg.port), DcRequestHandler)
    httpd.api = api
    httpd.web_dir = cfg.web_dir
    httpd.runtime_config = cfg
    httpd.services = services
    if cfg.tls_enabled:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cfg.tls_cert, cfg.tls_key)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    return httpd


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="app.py", description=u"Duplicate Cleaner & Subtitles 后端")
    parser.add_argument("--host", default=None,
                        help=u"监听地址（默认 127.0.0.1；SPK 传 0.0.0.0）")
    parser.add_argument("--port", type=int, default=None,
                        help=u"监听端口（默认 8787）")
    parser.add_argument("--data-dir", default=None,
                        help=u"数据目录（默认 ./data，SPK 传 var 目录）")
    parser.add_argument("--web-dir", default=None,
                        help=u"静态资源目录（默认 ../web）")
    parser.add_argument("--tls-cert", default=None, help=u"TLS 证书路径")
    parser.add_argument("--tls-key", default=None, help=u"TLS 私钥路径")
    parser.add_argument("--dev", action="store_true",
                        help=u"开发模式（仅本机测试，禁止生产使用）")
    parser.add_argument("--log-level", default="INFO",
                        help=u"日志级别（DEBUG/INFO/WARNING/ERROR）")
    parser.add_argument("--version", action="store_true", help=u"打印版本并退出")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.version:
        print(config.APP_VERSION)
        return 0
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    runtime_config = build_config(args)
    if runtime_config.dev_mode and not runtime_config.dev_roots:
        logger.warning(u"开发模式已开启，但 DUP_DEV_ROOTS 为空，将没有可用的虚拟共享文件夹")
    try:
        httpd = create_server(runtime_config)
    except OSError as exc:
        logger.error(u"启动失败（端口被占用或权限不足）: %s", exc)
        return 1
    except Exception as exc:
        logger.exception(u"启动失败: %s", exc)
        return 1

    bound_port = httpd.server_address[1]
    display_host = runtime_config.host
    if display_host in ("0.0.0.0", "::", ""):
        display_host = "127.0.0.1"
    url = "%s://%s:%d/" % (runtime_config.scheme, display_host, bound_port)
    logger.info(u"Duplicate Cleaner & Subtitles %s 已启动: %s（数据目录 %s，静态目录 %s）",
                config.APP_VERSION, url, runtime_config.data_dir,
                runtime_config.web_dir)
    if runtime_config.dev_mode:
        logger.warning(u"当前处于开发模式（DUP_CLEANER_DEV），禁止用于生产环境")

    def _terminate(signum, frame):
        logger.info(u"收到信号 %s，正在退出", signum)
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, AttributeError):
        pass
    try:
        httpd.serve_forever(poll_interval=0.25)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        try:
            httpd.services.shutdown()
        finally:
            httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
