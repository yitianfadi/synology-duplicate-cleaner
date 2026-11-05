# -*- coding: utf-8 -*-
"""统一 API 错误类型与错误码。"""

from __future__ import absolute_import

# 契约 §3 列出的错误码（另外补充少量内部错误码，见各模块注释）
ERROR_STATUS = {
    "AUTH_REQUIRED": 401,
    "AUTH_FAILED": 401,
    "FORBIDDEN": 403,
    "CSRF_FAILED": 403,
    "NOT_FOUND": 404,
    "INVALID_ARGUMENT": 400,
    "INVALID_PATH": 400,
    "PATH_NOT_ALLOWED": 403,
    "SCAN_BUSY": 409,
    "SCAN_NOT_DONE": 409,
    "PLAN_EXPIRED": 410,
    "PLAN_INVALID": 400,
    "HASH_MISMATCH": 409,
    "FILE_CHANGED": 409,
    "RECYCLE_UNAVAILABLE": 409,
    "PREVIEW_UNSUPPORTED": 415,
    "TOO_MANY_FILES": 400,
    "SIMILAR_REVIEW_REQUIRED": 400,
    "INSUFFICIENT_SPACE": 409,
    # v0026 云端字幕探测（不要用 401 以免前端误判登录失效）
    "PROVIDER_AUTH_FAILED": 400,
    "PROVIDER_REJECTED": 400,
    "MODELS_UNSUPPORTED": 400,
    "PROVIDER_HTTP_ERROR": 502,
    "PROVIDER_NETWORK_ERROR": 502,
    "PROVIDER_TIMEOUT": 504,
    "AUDIO_FAILED": 400,
    "FFMPEG_UNAVAILABLE": 400,
    "PROBE_BUSY": 409,
    # v0028 目录批量字幕（docs/SUBTITLE_BATCH_CONTRACT.md §10）
    "INVALID_DIRECTORY": 400,
    "ENUMERATE_LIMIT": 400,
    "BATCH_EMPTY": 400,
    "BATCH_TOO_LARGE": 400,
    "SOURCE_SRT_MISSING": 400,
    "OUTPUT_NOT_WRITABLE": 400,
    "INTERNAL_ERROR": 500,
}


class ApiError(Exception):
    """业务错误：带 HTTP 状态码、错误码、中文可读信息与可选 details。"""

    def __init__(self, code, message, status=None, details=None):
        super(ApiError, self).__init__(message)
        self.code = code or "INTERNAL_ERROR"
        if status is None:
            status = ERROR_STATUS.get(self.code, 400)
        self.status = int(status)
        self.message = message or self.code
        self.details = details or {}

    def to_payload(self):
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }

    def __repr__(self):
        return "ApiError(%r, %r, status=%r)" % (self.code, self.message, self.status)


def invalid(msg, code="INVALID_ARGUMENT", details=None):
    return ApiError(code, msg, details=details)


def not_found(msg=u"对象不存在"):
    return ApiError("NOT_FOUND", msg)


def auth_required(msg=u"请先登录"):
    return ApiError("AUTH_REQUIRED", msg)


def auth_failed(msg=u"账号或密码错误"):
    return ApiError("AUTH_FAILED", msg)


def forbidden(msg=u"没有权限执行该操作"):
    return ApiError("FORBIDDEN", msg)


def csrf_failed(msg=u"CSRF 校验失败，请刷新页面后重试"):
    return ApiError("CSRF_FAILED", msg)


def path_not_allowed(msg=u"该路径不允许访问", details=None):
    return ApiError("PATH_NOT_ALLOWED", msg, details=details)


def internal(msg=u"服务器内部错误"):
    return ApiError("INTERNAL_ERROR", msg)
