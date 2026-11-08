# -*- coding: utf-8 -*-
"""删除：一次性计划 + 二次确认 + 逐文件重校验 + 回收站/永久删除。

安全约束（契约 §9）：
- 绝不自动删除：必须先生成计划（POST delete-plan），再 confirm=DELETE；
  永久删除还必须带短语“永久删除”。
- 计划 5 分钟过期、一次性、只对创建它的会话有效。
- 删除前逐个重校验：仍存在、仍是普通文件、仍在允许共享路径内、不是保留项、
  size / mtime_ns / inode / dev 未变化、保留文件仍存在且未变化、
  候选文件与保留文件的全量哈希一致。任何一项失败都进入 skipped，绝不静默删除。
- 回收站目标：<共享文件夹根>/#recycle/@duplicate-cleaner/<YYYYMMDD-HHMMSS>/<相对路径>，
  同名冲突追加 .dup-<n>；跨文件系统禁止移动，按失败报告。
- 删除结果逐条落盘（JSONL），任务进度可查询，审计日志持久化。
"""

from __future__ import absolute_import

import json
import logging
import os
import secrets
import threading
import time
import traceback

from . import config, fsutil, hashing, protection, timeutil, video_similarity
from .errors import ApiError, forbidden, invalid, not_found, path_not_allowed

logger = logging.getLogger("dupcleaner.deletion")

# 删除结果状态
STATUS_RECYCLED = "recycled"
STATUS_DELETED = "deleted"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

# 校验失败原因码（与契约常见错误码对齐）
REASON_NOT_FOUND = "NOT_FOUND"
REASON_FILE_CHANGED = "FILE_CHANGED"
REASON_HASH_MISMATCH = "HASH_MISMATCH"
REASON_PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
REASON_KEEPER_MISSING = "KEEPER_MISSING"
REASON_IS_KEEPER = "IS_KEEPER"
REASON_RECYCLE_UNAVAILABLE = "RECYCLE_UNAVAILABLE"
REASON_CROSS_DEVICE = "CROSS_DEVICE"
REASON_MANUAL_KEEP_REQUIRED = "MANUAL_KEEP_REQUIRED"
# v0044：删除白名单（附属文件/指定目录/后缀保护）
REASON_PROTECTED = "PROTECTED"
REASON_INTERNAL = "INTERNAL_ERROR"


def make_plan_id():
    return "plan-" + secrets.token_hex(8)


def make_job_id():
    return "del-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def _result(ok, code="", message=""):
    return {"ok": bool(ok), "code": code, "message": message}


def _fresh_hash(path, algo, hash_cache):
    identity = fsutil.file_identity(path)
    if identity is None:
        raise IOError(u"文件不存在")
    key = (path, identity["dev"], identity["inode"], identity["size"],
           identity["mtime_ns"], algo)
    if hash_cache is not None and key in hash_cache:
        return hash_cache[key]
    digest, _ = hashing.full_hash_file(path, algo=algo)
    if hash_cache is not None:
        if len(hash_cache) > 512:
            hash_cache.clear()
        hash_cache[key] = digest
    return digest


def revalidate(record, keeper_record, share_service, algo="sha256",
               hash_check=True, hash_cache=None):
    """删除前重校验（安全核心），返回 {ok, code, message}。

    record / keeper_record 为扫描时保存的文件快照（含 path/size/mtime_ns/
    inode/dev/hash/group_id 等）。
    """
    path = record.get("path") or ""
    try:
        normalized = fsutil.check_path_safety(path)
    except ApiError as exc:
        return _result(False, REASON_PATH_NOT_ALLOWED, exc.message)
    share = share_service.find_share(normalized)
    if share is None:
        return _result(False, REASON_PATH_NOT_ALLOWED,
                       u"文件不在允许的共享文件夹内: %s" % normalized)
    if fsutil.has_symlink_component(normalized, share["path"]):
        return _result(False, REASON_PATH_NOT_ALLOWED,
                       u"文件路径包含符号链接: %s" % normalized)

    identity = fsutil.file_identity(normalized)
    if identity is None:
        if fsutil.lstat_or_none(normalized) is None:
            return _result(False, REASON_NOT_FOUND, u"文件已不存在: %s" % normalized)
        return _result(False, REASON_PATH_NOT_ALLOWED,
                       u"目标不是普通文件（符号链接/目录/设备等）: %s" % normalized)
    if identity["size"] != int(record.get("size") or 0):
        return _result(False, REASON_FILE_CHANGED, u"文件大小已变化: %s" % normalized)
    if identity["mtime_ns"] != int(record.get("mtime_ns") or 0):
        return _result(False, REASON_FILE_CHANGED, u"文件修改时间已变化: %s" % normalized)
    if identity["inode"] != int(record.get("inode") or 0) or \
            identity["dev"] != int(record.get("dev") or 0):
        return _result(False, REASON_FILE_CHANGED,
                       u"文件 inode/设备已变化（可能被替换）: %s" % normalized)

    # 保留文件必须仍存在且未被修改
    keeper_path = (keeper_record or {}).get("path") or ""
    if not keeper_path:
        return _result(False, REASON_KEEPER_MISSING, u"缺少保留文件信息")
    try:
        keeper_norm = fsutil.check_path_safety(keeper_path)
    except ApiError as exc:
        return _result(False, REASON_KEEPER_MISSING, exc.message)
    if share_service.find_share(keeper_norm) is None:
        return _result(False, REASON_KEEPER_MISSING,
                       u"保留文件不在允许的共享文件夹内: %s" % keeper_norm)
    if fsutil.has_symlink_component(keeper_norm, share_service.find_share(keeper_norm)["path"]):
        return _result(False, REASON_KEEPER_MISSING, u"保留文件路径包含符号链接")
    keeper_identity = fsutil.file_identity(keeper_norm)
    if keeper_identity is None:
        return _result(False, REASON_KEEPER_MISSING,
                       u"保留文件已不存在: %s" % keeper_norm)
    if (keeper_identity["size"] != int((keeper_record or {}).get("size") or 0)
            or keeper_identity["mtime_ns"] != int((keeper_record or {}).get("mtime_ns") or 0)
            or keeper_identity["inode"] != int((keeper_record or {}).get("inode") or 0)
            or keeper_identity["dev"] != int((keeper_record or {}).get("dev") or 0)):
        return _result(False, REASON_FILE_CHANGED,
                       u"保留文件已被修改，取消删除: %s" % keeper_norm)

    match_type = (record.get("match_type")
                  or (keeper_record or {}).get("match_type") or "exact")
    if match_type == "video":
        # 视频相似删除：不要求候选与 keeper 字节相等。
        # 安全增强（v0018）：比较扫描时保存的内容快速哈希（size+head/tail 64KB
        # SHA256），防止“size/mtime/inode 未变但内容被替换”的误删。
        # 历史扫描缺少该哈希时：按契约回退到既有身份校验，但显式标记
        # content_hash_verified=false（计划 warning，不 fail-open 到字节相等假设）。
        content_verified = True
        content_missing = False
        stored_candidate = record.get("content_quick_hash")
        stored_keeper = (keeper_record or {}).get("content_quick_hash")
        try:
            if stored_candidate:
                if (video_similarity.content_quick_hash(normalized)
                        != stored_candidate):
                    return _result(
                        False, REASON_HASH_MISMATCH,
                        u"候选视频内容已变化（快速内容哈希不一致）: %s"
                        % normalized)
            else:
                content_verified = False
                content_missing = True
            if stored_keeper:
                if (video_similarity.content_quick_hash(keeper_norm)
                        != stored_keeper):
                    return _result(
                        False, REASON_HASH_MISMATCH,
                        u"保留视频内容已变化（快速内容哈希不一致）: %s"
                        % keeper_norm)
            else:
                content_verified = False
                content_missing = True
        except (OSError, IOError) as exc:
            return _result(False, REASON_FILE_CHANGED,
                           u"读取视频内容失败: %s" % exc)
        result = _result(True)
        result["content_hash_verified"] = bool(content_verified)
        result["content_hash_missing"] = bool(content_missing)
        return result
    candidate_hash = record.get("hash") or ""
    keeper_hash = (keeper_record or {}).get("hash") or ""
    if match_type == "similar":
        # 相似组：候选与保留文件内容本来就不同，不要求哈希相等；
        # 只要求各自内容未变（下一段 fresh hash 分别对比自己的扫描记录）。
        if not candidate_hash:
            return _result(False, REASON_HASH_MISMATCH,
                           u"缺少相似文件内容哈希，无法校验")
        if not keeper_hash:
            return _result(False, REASON_KEEPER_MISSING,
                           u"缺少保留文件内容哈希，无法校验")
    else:
        if not candidate_hash or candidate_hash != keeper_hash:
            return _result(False, REASON_HASH_MISMATCH,
                           u"候选文件与保留文件记录的哈希不一致")

    if hash_check:
        try:
            # 哈希计算点：删除前重新全量哈希校验候选文件
            fresh_candidate = _fresh_hash(normalized, algo, hash_cache)
            if fresh_candidate != candidate_hash:
                return _result(False, REASON_HASH_MISMATCH,
                               u"候选文件内容已变化（哈希不一致）: %s" % normalized)
            # 哈希计算点：删除前重新全量哈希校验保留文件
            fresh_keeper = _fresh_hash(keeper_norm, algo, hash_cache)
            if fresh_keeper != keeper_hash:
                return _result(False, REASON_HASH_MISMATCH,
                               u"保留文件内容已变化（哈希不一致）: %s" % keeper_norm)
        except (OSError, IOError) as exc:
            return _result(False, REASON_FILE_CHANGED, u"重新哈希失败: %s" % exc)
    return _result(True)


def recycle_destination(share_root, source_path, stamp):
    """回收站目标路径生成（纯函数，便于测试）。"""
    try:
        relative = os.path.relpath(source_path, share_root)
    except ValueError:
        relative = os.path.basename(source_path)
    if relative.startswith(".."):
        relative = os.path.basename(source_path)
    base_dir = os.path.join(share_root, "#recycle", "@duplicate-cleaner", stamp)
    destination = os.path.join(base_dir, relative)
    if not os.path.exists(destination):
        return destination
    index = 1
    while index <= 1000:
        candidate = destination + ".dup-%d" % index
        if not os.path.exists(candidate):
            return candidate
        index += 1
    raise ApiError("INTERNAL_ERROR", u"回收站目标文件名冲突过多: %s" % destination)


def _nearest_existing(path):
    current = path
    while True:
        try:
            st = os.lstat(current)
            return current, st
        except OSError:
            parent = os.path.dirname(current)
            if parent == current or not parent:
                return None, None
            current = parent


class DeleteJobState(object):
    def __init__(self, job_id, scan_id, plan_id, user, recycle, total, bytes_total,
                 plan_path, create_recycle_dir):
        self.job_id = job_id
        self.scan_id = scan_id
        self.plan_id = plan_id
        self.user = user
        self.recycle = bool(recycle)
        self.recycle_mode = "share"
        self.create_recycle_dir = bool(create_recycle_dir)
        self.total = int(total)
        self.bytes_total = int(bytes_total)
        self.done = 0
        self.failed = 0
        self.skipped = 0
        self.bytes_done = 0
        self.status = "running"
        self.started_epoch = time.time()
        self.finished_epoch = None
        self.results = []
        self.results_truncated = False
        self.plan_path = plan_path
        self.lock = threading.RLock()
        self.thread = None
        self.last_persist = 0.0

    def snapshot(self):
        with self.lock:
            progress = (float(self.done) / self.total) if self.total else 1.0
            return {
                "job_id": self.job_id,
                "scan_id": self.scan_id,
                "plan_id": self.plan_id,
                "status": self.status,
                "progress": round(min(1.0, progress), 4),
                "total": self.total,
                "done": self.done,
                "failed": self.failed,
                "skipped": self.skipped,
                "bytes_total": self.bytes_total,
                "bytes_done": self.bytes_done,
                "results": list(self.results),
                "results_truncated": bool(self.results_truncated),
                "started_at": timeutil.iso_local(self.started_epoch),
                "finished_at": timeutil.iso_local(self.finished_epoch),
                "recycle": self.recycle,
                "recycle_mode": self.recycle_mode,
                "log_file": "deletions/%s.jsonl" % self.job_id,
            }

    def to_meta(self):
        data = self.snapshot()
        data.pop("results", None)
        data["started_epoch"] = self.started_epoch
        data["finished_epoch"] = self.finished_epoch
        return data


class DeletionManager(object):
    def __init__(self, runtime_config, store, share_service, scan_manager,
                 settings_manager, hash_verifier=None, trash_manager=None,
                 notifier=None):
        self.config = runtime_config
        self.store = store
        self.shares = share_service
        self.scanner = scan_manager
        self.settings = settings_manager
        self.trash = trash_manager
        self.notifier = notifier
        self._hash_verifier = hash_verifier or _fresh_hash
        self._lock = threading.RLock()
        self._plans = {}
        self._jobs = {}
        self._jobs_by_scan = {}

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------
    def _protector(self):
        """按当前设置构造保护判定器（删除侧终局防线）。"""
        try:
            settings = self.settings.get() or {}
        except Exception:  # pragma: no cover - 设置读取失败时按默认保护
            settings = {}
        return protection.Protector(settings)

    def create_plan(self, session, scan_id, payload):
        meta = self.scanner.require_completed(scan_id)
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        recycle = payload.get("recycle", True)
        if not isinstance(recycle, bool):
            raise invalid(u"recycle 必须是布尔值")
        # v0045：删除去向（共享文件夹 #recycle / 套件独立回收站）按当前设置快照
        recycle_mode = "share"
        if recycle:
            try:
                recycle_mode = str(
                    (self.settings.get() or {}).get("delete_recycle_mode")
                    or config.RECYCLE_MODE_DEFAULT)
            except Exception:  # pragma: no cover - 设置读取失败按默认
                recycle_mode = config.RECYCLE_MODE_DEFAULT
            if recycle_mode not in config.RECYCLE_MODES:
                recycle_mode = config.RECYCLE_MODE_DEFAULT
            if recycle_mode == "package" and self.trash is None:
                recycle_mode = "share"
        options = meta.get("options") or {}
        algo = options.get("hash_algo") or "sha256"
        create_recycle_dir = bool(options.get("create_recycle_dir", False))
        share_list = self.shares.list_shares()

        scope = payload.get("scope")
        file_ids = payload.get("file_ids")
        exclude_ids = payload.get("exclude_file_ids") or []
        # v1 明确禁止自动删除 needs_manual_keep 组：用户真实保留项可能不是
        # 服务端兜底保留项，自动删除有误删风险。UI 也不会提交该开关。
        if payload.get("include_manual_groups"):
            raise invalid(u"当前版本不允许删除需要人工确认保留项的组")
        if not isinstance(exclude_ids, list) or any(
                not isinstance(item, str) for item in exclude_ids):
            raise invalid(u"exclude_file_ids 必须是文件 ID 数组")
        exclude_set = set(exclude_ids)
        reviewed_ids = payload.get("reviewed_similar_group_ids") or []
        if not isinstance(reviewed_ids, list) or any(
                not isinstance(item, str) for item in reviewed_ids):
            raise invalid(u"reviewed_similar_group_ids 必须是字符串数组")
        reviewed_set = set(reviewed_ids)
        confirm_similar = payload.get("confirm_similar") is True

        candidates = []  # (group, file, keeper)
        skipped = []
        if file_ids is not None:
            if not isinstance(file_ids, list) or not file_ids:
                raise invalid(u"file_ids 必须是非空数组")
            if any(not isinstance(item, str) for item in file_ids):
                raise invalid(u"file_ids 必须是字符串数组")
            wanted = set(file_ids)
            found = {}
            for group in self.store.iter_scan_groups(scan_id):
                keeper = self._keeper_of(group)
                for file_info in group.get("files") or []:
                    file_id = file_info.get("file_id")
                    if file_id in wanted:
                        found[file_id] = (group, file_info, keeper)
            for file_id in file_ids:
                if file_id not in found:
                    skipped.append({"path": "", "file_id": file_id,
                                    "reason": REASON_NOT_FOUND,
                                    "message": u"文件 ID 不在扫描结果中: %s" % file_id})
                else:
                    group, file_info, keeper = found[file_id]
                    if file_info.get("is_keep"):
                        skipped.append({"path": file_info.get("path") or "",
                                        "file_id": file_id,
                                        "reason": REASON_IS_KEEPER,
                                        "message": u"不能删除保留文件"})
                        continue
                    if group.get("needs_manual_keep"):
                        skipped.append({
                            "path": file_info.get("path") or "",
                            "file_id": file_id,
                            "reason": REASON_MANUAL_KEEP_REQUIRED,
                            "message": u"该组创建时间无法安全区分，需要人工确认保留项，v1 不自动删除",
                        })
                        continue
                    candidates.append((group, file_info, keeper))
            if not candidates and not skipped:
                raise invalid(u"没有可删除的文件")
        elif scope == "all_duplicates":
            for group in self.store.iter_scan_groups(scan_id):
                self._purge_expired_plans()
                if (group.get("match_type") or "exact") in ("similar", "video"):
                    # scope=all_duplicates 永不包含相似/视频组：
                    # 相似文件必须复核后显式选择；视频只能走 video_similar/file_ids
                    continue
                if group.get("needs_manual_keep"):
                    # 一律跳过：宁可少删，也不能误删用户真实想保留的文件
                    for file_info in group.get("files") or []:
                        if file_info.get("is_keep"):
                            continue
                        skipped.append({
                            "path": file_info.get("path") or "",
                            "file_id": file_info.get("file_id"),
                            "reason": REASON_MANUAL_KEEP_REQUIRED,
                            "message": u"该组创建时间无法安全区分，需要人工确认保留项，v1 不自动删除",
                        })
                    continue
                keeper = self._keeper_of(group)
                for file_info in group.get("files") or []:
                    if file_info.get("is_keep"):
                        continue
                    if file_info.get("file_id") in exclude_set:
                        continue
                    candidates.append((group, file_info, keeper))
        elif scope == "video_similar":
            # 视频相似批量：所有视频组的非保留项；不含 exact/similar 组
            for group in self.store.iter_scan_groups(scan_id):
                self._purge_expired_plans()
                if (group.get("match_type") or "exact") != "video":
                    continue
                if group.get("needs_manual_keep"):
                    for file_info in group.get("files") or []:
                        if file_info.get("is_keep"):
                            continue
                        skipped.append({
                            "path": file_info.get("path") or "",
                            "file_id": file_info.get("file_id"),
                            "reason": REASON_MANUAL_KEEP_REQUIRED,
                            "message": u"该视频组创建时间无法安全区分，需要人工确认保留项",
                        })
                    continue
                keeper = self._keeper_of(group)
                for file_info in group.get("files") or []:
                    if file_info.get("is_keep"):
                        continue
                    if file_info.get("file_id") in exclude_set:
                        continue
                    candidates.append((group, file_info, keeper))
        else:
            raise invalid(u"必须提供 file_ids 或 scope=all_duplicates/video_similar")

        # v0044：删除白名单（保护附属文件/指定目录/后缀）。
        # 用**当前设置**重新计算（不信任扫描期标记，防止扫描后改设置/改文件名）。
        protector = self._protector()
        protected_candidates = []
        for group, file_info, keeper in candidates:
            state = protector.check(file_info.get("path") or "")
            if state is not None:
                skipped.append({
                    "path": file_info.get("path") or "",
                    "file_id": file_info.get("file_id"),
                    "reason": REASON_PROTECTED,
                    "protect_source": state.get("source") or "",
                    "message": protector.describe(state),
                })
                continue
            protected_candidates.append((group, file_info, keeper))
        protected_skipped = len(candidates) - len(protected_candidates)
        candidates = protected_candidates

        # 相似组删除门槛：confirm_similar + 每个涉及组都在 reviewed 列表内
        needed_similar_groups = set()
        for group, _file_info, _keeper in candidates:
            if (group.get("match_type") or "exact") == "similar":
                needed_similar_groups.add(group.get("group_id"))
        if needed_similar_groups:
            if not confirm_similar:
                raise ApiError(
                    "SIMILAR_REVIEW_REQUIRED",
                    u"相似文件必须人工复核后才能删除（需要 confirm_similar=true）",
                    details={"groups": sorted(needed_similar_groups)})
            missing_groups = needed_similar_groups - reviewed_set
            if missing_groups:
                raise ApiError(
                    "SIMILAR_REVIEW_REQUIRED",
                    u"存在未人工复核的相似组，禁止删除",
                    details={"groups": sorted(missing_groups)})

        plan_files = []
        groups_affected = set()
        recycle_unavailable = []
        recycle_seen = set()
        hash_cache = {}
        content_hash_verified = True
        content_hash_missing_videos = 0
        candidate_bytes = sum(int(file_info.get("size") or 0)
                              for _, file_info, _keeper in candidates)
        hash_verify_budget = config.PLAN_HASH_VERIFY_BUDGET_BYTES
        hash_check = candidate_bytes <= hash_verify_budget

        for group, file_info, keeper in candidates:
            group_id = group.get("group_id")
            match_type = group.get("match_type") or "exact"
            # 相似/视频文件内容哈希固定为 SHA256；精准组沿用配置的哈希算法
            entry_algo = "sha256" if match_type in ("similar", "video") else algo
            path = file_info.get("path") or ""
            result = revalidate(file_info, keeper, self.shares,
                                algo=entry_algo,
                                hash_check=hash_check, hash_cache=hash_cache)
            if not result["ok"]:
                skipped.append({"path": path, "file_id": file_info.get("file_id"),
                                "reason": result["code"],
                                "message": result["message"]})
                continue
            if (match_type == "video"
                    and not result.get("content_hash_verified", True)):
                # 历史扫描缺少视频内容快速哈希：允许按身份校验删除，但计划显式告警。
                content_hash_verified = False
                content_hash_missing_videos += 1
            # 回收站可用性（v0045：套件独立回收站只要共享根可写即可，无需 #recycle）
            if recycle:
                try:
                    share = self.shares.share_root(path)
                except ApiError as exc:
                    skipped.append({"path": path, "file_id": file_info.get("file_id"),
                                    "reason": REASON_PATH_NOT_ALLOWED,
                                    "message": exc.message})
                    continue
                if recycle_mode == "share":
                    share_key = fsutil.path_key(share["path"])
                    if share_key not in recycle_seen:
                        recycle_seen.add(share_key)
                        available = self.shares.has_recycle_dir(share)
                        if not available and not create_recycle_dir:
                            recycle_unavailable.append({
                                "share": share["name"],
                                "path": share["path"],
                            })
                    if (not self.shares.has_recycle_dir(share)
                            and not create_recycle_dir):
                        skipped.append({
                            "path": path,
                            "file_id": file_info.get("file_id"),
                            "reason": REASON_RECYCLE_UNAVAILABLE,
                            "message": u"共享文件夹没有 #recycle 回收站目录，已跳过",
                        })
                        continue
                    source_identity = fsutil.file_identity(path)
                    share_identity = fsutil.stat_or_none(share["path"])
                    if (source_identity is not None and share_identity is not None
                            and source_identity["dev"]
                            != int(share_identity.st_dev)):
                        skipped.append({
                            "path": path,
                            "file_id": file_info.get("file_id"),
                            "reason": REASON_CROSS_DEVICE,
                            "message": u"文件与共享文件夹不在同一文件系统，无法移动到回收站",
                        })
                        continue
            plan_files.append({
                "file": file_info,
                "keeper": keeper,
                "group_hash": group.get("hash"),
                "group_id": group_id,
                "match_type": match_type,
                "similarity_score": file_info.get("similarity_score"),
                "algo": entry_algo,
            })
            groups_affected.add(group_id)

        if not plan_files:
            if recycle and recycle_unavailable:
                raise ApiError(
                    "RECYCLE_UNAVAILABLE",
                    u"共享文件夹没有 #recycle 回收站目录，无法生成删除计划；"
                    u"请开启“自动创建回收站目录”或改用永久删除",
                    details={"recycle_unavailable": recycle_unavailable,
                             "skipped": skipped[:100]})
            raise ApiError("PLAN_INVALID", u"计划中没有任何可安全删除的文件",
                           details={"skipped": skipped[:100],
                                    "recycle_unavailable": recycle_unavailable})

        # 计划中的字节数只统计真正通过校验的文件（UI 用它显示可释放空间）
        total_bytes = sum(int(entry["file"].get("size") or 0)
                          for entry in plan_files)
        similar_entries = [entry for entry in plan_files
                           if entry.get("match_type") == "similar"]
        video_entries = [entry for entry in plan_files
                         if entry.get("match_type") == "video"]
        exact_entries = [entry for entry in plan_files
                         if entry.get("match_type") not in ("similar", "video")]
        similar_bytes = sum(int(entry["file"].get("size") or 0)
                            for entry in similar_entries)
        video_bytes = sum(int(entry["file"].get("size") or 0)
                          for entry in video_entries)
        exact_bytes = sum(int(entry["file"].get("size") or 0)
                          for entry in exact_entries)
        plan_id = make_plan_id()
        now = time.time()
        internal_path = self._write_plan_files(plan_id, plan_files)
        public_files = [
            {
                "file_id": entry["file"].get("file_id"),
                "path": entry["file"].get("path"),
                "size": entry["file"].get("size"),
                "group_id": entry["group_id"],
                "match_type": entry.get("match_type") or "exact",
                "similarity_score": entry.get("similarity_score"),
            }
            for entry in plan_files
        ]
        plan = {
            "plan_id": plan_id,
            "scan_id": scan_id,
            "session_id": session.session_id,
            "user": session.user,
            "recycle": recycle,
            "recycle_mode": recycle_mode,
            "create_recycle_dir": create_recycle_dir,
            "algo": algo,
            "created_epoch": now,
            "expires_epoch": now + config.PLAN_TTL_SECONDS,
            "expires_at": timeutil.iso_local(now + config.PLAN_TTL_SECONDS),
            "count": len(plan_files),
            "bytes": total_bytes,
            "similar_count": len(similar_entries),
            "similar_bytes": similar_bytes,
            "exact_count": len(exact_entries),
            "exact_bytes": exact_bytes,
            "video_count": len(video_entries),
            "video_bytes": video_bytes,
            "manual_review": bool(similar_entries),
            "reviewed_similar_group_ids": sorted(reviewed_set),
            "groups_affected": len(groups_affected),
            "files": public_files,
            "skipped": skipped,
            "recycle_unavailable": recycle_unavailable,
            "protected_skipped": protected_skipped,
            "hash_verified": bool(hash_check),
            "hash_verification_deferred": not hash_check,
            "content_hash_verified": bool(content_hash_verified),
            "content_hash_missing_videos": int(content_hash_missing_videos),
            "internal_path": internal_path,
            "consumed": False,
        }
        with self._lock:
            self._purge_expired_plans_locked()
            self._plans[plan_id] = plan
        self._write_plan_meta(plan)
        self.store.audit("delete_plan", session.user, {
            "scan_id": scan_id,
            "plan_id": plan_id,
            "recycle": recycle,
            "count": plan["count"],
            "bytes": plan["bytes"],
            "recycle_mode": plan.get("recycle_mode") or "share",
            "exact_count": plan["exact_count"],
            "similar_count": plan["similar_count"],
            "video_count": plan.get("video_count", 0),
            "manual_review": plan["manual_review"],
            "reviewed_similar_group_ids": plan["reviewed_similar_group_ids"],
            "skipped": len(skipped),
            "protected_skipped": protected_skipped,
            "hash_verified": plan["hash_verified"],
            "content_hash_verified": plan.get("content_hash_verified", True),
            "content_hash_missing_videos": plan.get(
                "content_hash_missing_videos", 0),
        })
        return self._public_plan(plan)

    def _public_plan(self, plan):
        files = plan["files"]
        truncated = len(files) > config.PLAN_FILES_RESPONSE_LIMIT
        skipped = plan["skipped"]
        skipped_truncated = len(skipped) > 2000
        return {
            "plan_id": plan["plan_id"],
            "expires_at": plan["expires_at"],
            "recycle": plan["recycle"],
            "recycle_mode": plan.get("recycle_mode") or "share",
            "count": plan["count"],
            "bytes": plan["bytes"],
            "similar_count": plan.get("similar_count", 0),
            "similar_bytes": plan.get("similar_bytes", 0),
            "exact_count": plan.get("exact_count", 0),
            "exact_bytes": plan.get("exact_bytes", 0),
            "video_count": plan.get("video_count", 0),
            "video_bytes": plan.get("video_bytes", 0),
            "manual_review": plan.get("manual_review", False),
            "reviewed_similar_group_ids": plan.get("reviewed_similar_group_ids") or [],
            "files": files[:config.PLAN_FILES_RESPONSE_LIMIT],
            "files_total": len(files),
            "files_truncated": truncated,
            "skipped": skipped[:2000],
            "skipped_total": len(skipped),
            "skipped_truncated": skipped_truncated,
            "groups_affected": plan["groups_affected"],
            "recycle_unavailable": plan["recycle_unavailable"],
            "protected_skipped": int(plan.get("protected_skipped") or 0),
            "hash_verified": plan["hash_verified"],
            "hash_verification_deferred": plan["hash_verification_deferred"],
            "content_hash_verified": plan.get("content_hash_verified", True),
            "content_hash_missing_videos": plan.get(
                "content_hash_missing_videos", 0),
        }

    @staticmethod
    def _keeper_of(group):
        keep_file_id = group.get("keep_file_id")
        for file_info in group.get("files") or []:
            if file_info.get("is_keep") or file_info.get("file_id") == keep_file_id:
                return file_info
        return None

    def _write_plan_files(self, plan_id, plan_files):
        directory = self.store.plans_dir
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, plan_id + ".files.jsonl")
        tmp_path = path + ".tmp"
        with open(tmp_path, "wb") as handle:
            for entry in plan_files:
                handle.write((json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
        return path

    def _write_plan_meta(self, plan):
        meta = dict(plan)
        meta.pop("files", None)
        meta.pop("skipped", None)
        meta["files_total"] = plan.get("count")
        self.store.write_json(
            os.path.join(self.store.plans_dir, plan["plan_id"] + ".json"), meta)

    def _purge_expired_plans(self):
        with self._lock:
            self._purge_expired_plans_locked()

    def _purge_expired_plans_locked(self):
        now = time.time()
        expired = [plan_id for plan_id, plan in self._plans.items()
                   if now > plan["expires_epoch"] or plan.get("consumed")]
        for plan_id in expired:
            self._remove_plan_files(plan_id)
            self._plans.pop(plan_id, None)

    def _remove_plan_files(self, plan_id):
        for suffix in (".json", ".files.jsonl"):
            try:
                os.unlink(os.path.join(self.store.plans_dir, plan_id + suffix))
            except OSError:
                pass

    def get_plan_summary(self, plan_id):
        with self._lock:
            plan = self._plans.get(plan_id)
        return plan

    # ------------------------------------------------------------------
    # 执行删除
    # ------------------------------------------------------------------
    def start_delete(self, session, scan_id, payload):
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        plan_id = payload.get("plan_id")
        if not isinstance(plan_id, str) or not config.PLAN_ID_RE.match(plan_id):
            raise ApiError("PLAN_INVALID", u"计划 ID 非法或已过期")
        confirm = payload.get("confirm")
        if confirm != "DELETE":
            raise invalid(u"必须提供 confirm=DELETE 二次确认")
        permanent_phrase = payload.get("permanent_phrase")
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                raise ApiError("PLAN_INVALID", u"计划不存在或已使用")
            if plan["scan_id"] != scan_id:
                raise ApiError("PLAN_INVALID", u"计划不属于该扫描任务")
            if plan["session_id"] != session.session_id:
                raise forbidden(u"该计划只对创建它的会话有效")
            now = time.time()
            if now > plan["expires_epoch"]:
                self._remove_plan_files(plan_id)
                self._plans.pop(plan_id, None)
                raise ApiError("PLAN_EXPIRED", u"删除计划已过期，请重新生成")
            if not plan["recycle"]:
                if permanent_phrase != config.REQUIRED_PERMANENT_PHRASE:
                    raise invalid(u"永久删除必须输入确认短语“%s”" % config.REQUIRED_PERMANENT_PHRASE)
            plan["consumed"] = True
            self._plans.pop(plan_id, None)

        job_id = make_job_id()
        job = DeleteJobState(
            job_id=job_id,
            scan_id=scan_id,
            plan_id=plan_id,
            user=session.user,
            recycle=plan["recycle"],
            total=plan["count"],
            bytes_total=plan["bytes"],
            plan_path=plan["internal_path"],
            create_recycle_dir=plan.get("create_recycle_dir", False),
        )
        # v0044：本任务的保护判定器（目录缓存跨文件复用）；执行前再按当前设置复查。
        job.protector = self._protector()
        # v0045：删除去向（计划时快照，执行期间不跟随设置变化）
        job.recycle_mode = plan.get("recycle_mode") or "share"
        with self._lock:
            self._jobs[job_id] = job
            self._jobs_by_scan.setdefault(scan_id, []).append(job_id)
        try:
            self.store.save_job_meta(job_id, job.to_meta())
        except Exception:
            pass
        self.store.audit("delete_start", session.user, {
            "scan_id": scan_id,
            "plan_id": plan_id,
            "job_id": job_id,
            "recycle": plan["recycle"],
            "recycle_mode": job.recycle_mode,
            "count": plan["count"],
            "bytes": plan["bytes"],
            "exact_count": plan.get("exact_count", 0),
            "similar_count": plan.get("similar_count", 0),
            "video_count": plan.get("video_count", 0),
            "manual_review": plan.get("manual_review", False),
            "reviewed_similar_group_ids": plan.get(
                "reviewed_similar_group_ids") or [],
        })
        job.thread = threading.Thread(target=self._run_job, args=(job,),
                                      name="delete-" + job_id, daemon=True)
        job.thread.start()
        return job_id

    def _run_job(self, job):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        hash_cache = {}
        # v0045：套件回收站条目（任务结束后写 manifest，供整组恢复/清理）
        trash_entries = []
        # v0032：删除成功的文件写入 scan_file_deletions，结果页/统计/CSV 读取时剔除；
        # 每 200 条刷一次，避免任务中断时丢失全部标记。
        deleted_entries = []

        def flush_deleted():
            if not deleted_entries:
                return
            chunk = deleted_entries[:]
            del deleted_entries[:]
            try:
                self.store.mark_scan_files_deleted(job.scan_id, chunk)
            except Exception:
                pass

        try:
            try:
                handle = open(job.plan_path, "rb")
            except OSError:
                handle = None
            if handle is None:
                raise ApiError("PLAN_INVALID", u"计划文件丢失，无法执行删除")
            with handle:
                for raw in handle:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeError):
                        job.skipped += 1
                        continue
                    result = self._delete_one(job, entry, stamp, hash_cache)
                    status = result.get("status")
                    if status in (STATUS_RECYCLED, STATUS_DELETED):
                        deleted_entries.append({
                            "path": result.get("path"),
                            "job_id": job.job_id,
                            "mode": status,
                        })
                    elif (status == STATUS_SKIPPED
                          and result.get("reason") == REASON_NOT_FOUND):
                        # 文件已经不存在：同样从结果集中剔除，避免继续显示
                        deleted_entries.append({
                            "path": result.get("path"),
                            "job_id": job.job_id,
                            "mode": "missing",
                        })
                    if len(deleted_entries) >= 200:
                        flush_deleted()
                    if (status == STATUS_RECYCLED
                            and result.get("trash_mode") == "package"):
                        trash_entries.append({
                            "original": result.get("path"),
                            "trash_path": result.get("recycle_path"),
                            "size": result.get("size"),
                            "group_id": (entry.get("group_id")
                                         or (entry.get("file") or {}).get("group_id")),
                            "keeper_path": (entry.get("keeper") or {}).get("path"),
                            "file_id": result.get("file_id"),
                            "match_type": result.get("match_type"),
                        })
                    self._record_result(job, result)
                    self._persist_job(job)
            flush_deleted()
            with job.lock:
                if job.failed == 0 and job.skipped == 0:
                    final_status = "completed"
                elif job.failed == 0:
                    final_status = "partial"
                elif job.done > 0 and job.failed < job.total:
                    final_status = "partial"
                else:
                    final_status = "failed"
                job.finished_epoch = time.time()
            # 先把日志与审计落盘，再对外暴露最终状态：
            # 避免轮询方看到 completed 时 delete_end/日志还没写入。
            self._write_job_log(job, final_status)
            self.store.audit("delete_end", job.user, {
                "scan_id": job.scan_id,
                "job_id": job.job_id,
                "status": final_status,
                "total": job.total,
                "done": job.done,
                "failed": job.failed,
                "skipped": job.skipped,
                "recycle_mode": getattr(job, "recycle_mode", "share"),
            })
            self._record_trash(job, trash_entries)
            with job.lock:
                job.status = final_status
            self._persist_job(job, force=True)
            # v0046：释放空间/大量失败通知（异步入队，失败不影响任务收尾）
            try:
                if self.notifier is not None:
                    self.notifier.deletion_finished(job.snapshot())
            except Exception:
                logger.warning(u"发送删除通知失败", exc_info=True)
        except Exception as exc:  # pragma: no cover
            flush_deleted()
            self._record_trash(job, trash_entries)
            with job.lock:
                job.status = "failed"
                job.finished_epoch = time.time()
            try:
                self.store.append_job_result(job.job_id, {
                    "path": "",
                    "status": STATUS_FAILED,
                    "reason": REASON_INTERNAL,
                    "message": u"删除任务异常: %s" % exc,
                    "recycle_path": None,
                })
            except Exception:
                pass
            self._persist_job(job, force=True)
            try:
                self.store.write_log("delete-%s.log" % job.job_id,
                                     u"删除任务异常:\n" + traceback.format_exc() + "\n")
            except Exception:
                pass
            self.store.audit("delete_end", job.user, {
                "scan_id": job.scan_id,
                "job_id": job.job_id,
                "status": "failed",
                "error": str(exc),
            })

    def _record_trash(self, job, trash_entries):
        """v0045：把套件回收站的移动记录写成 manifest（供整组恢复/清理）。"""
        if self.trash is None or not trash_entries:
            return
        try:
            self.trash.record_job(job.job_id, job.scan_id, job.user,
                                  trash_entries,
                                  recycle_mode=getattr(job, "recycle_mode",
                                                       "package"))
        except Exception:  # pragma: no cover - 清单失败不影响已完成的移动
            logger.warning(u"写入套件回收站清单失败：%s", exc_info=True)

    def _delete_one(self, job, entry, stamp, hash_cache):
        file_info = entry.get("file") or {}
        keeper = entry.get("keeper") or {}
        path = file_info.get("path") or ""
        algo = entry.get("algo") or "sha256"
        base = {
            "path": path,
            "file_id": file_info.get("file_id"),
            "size": int(file_info.get("size") or 0),
            "match_type": (file_info.get("match_type")
                           or entry.get("match_type") or "exact"),
            "similarity_score": file_info.get("similarity_score"),
            "recycle_path": None,
        }
        result = revalidate(file_info, keeper, self.shares, algo=algo,
                            hash_check=True, hash_cache=hash_cache)
        if result.get("content_hash_verified") is not None:
            base["content_hash_verified"] = bool(
                result.get("content_hash_verified"))
        if not result["ok"]:
            base.update({"status": STATUS_SKIPPED, "reason": result["code"],
                         "message": result["message"]})
            return base
        # v0044：删除白名单终局检查（计划生成后设置/文件名可能变化）
        protector = getattr(job, "protector", None)
        if protector is not None:
            protect_state = protector.check(path)
            if protect_state is not None:
                base.update({"status": STATUS_SKIPPED,
                             "reason": REASON_PROTECTED,
                             "protect_source": protect_state.get("source") or "",
                             "message": protector.describe(protect_state)})
                return base
        try:
            share = self.shares.share_root(path)
        except ApiError as exc:
            base.update({"status": STATUS_SKIPPED,
                         "reason": REASON_PATH_NOT_ALLOWED,
                         "message": exc.message})
            return base

        if job.recycle:
            # v0045：套件独立回收站（同卷 rename，manifest 支持整组恢复）
            if (getattr(job, "recycle_mode", "share") == "package"
                    and self.trash is not None):
                try:
                    destination = self.trash.destination_for(
                        share["path"], path, stamp)
                    parent = os.path.dirname(destination)
                    os.makedirs(parent, exist_ok=True)
                    source_identity = fsutil.file_identity(path)
                    if source_identity is not None:
                        parent_stat = fsutil.stat_or_none(parent)
                        if (parent_stat is not None
                                and int(parent_stat.st_dev) != source_identity["dev"]):
                            base.update({
                                "status": STATUS_FAILED,
                                "reason": REASON_CROSS_DEVICE,
                                "message": u"套件回收站与文件不在同一文件系统，禁止跨设备移动"})
                            return base
                    # 文件删除点（套件回收站）：移动到 <共享文件夹根>/#duplicate-cleaner-trash/<时间戳>/<相对路径>
                    os.rename(path, destination)
                except ApiError as exc:
                    base.update({"status": STATUS_FAILED,
                                 "reason": REASON_INTERNAL,
                                 "message": exc.message})
                    return base
                except OSError as exc:
                    base.update({
                        "status": STATUS_FAILED,
                        "reason": REASON_CROSS_DEVICE
                        if getattr(exc, "errno", None) == 18 else REASON_INTERNAL,
                        "message": u"移动到套件回收站失败: %s" % exc})
                    return base
                result_dict = dict(base)
                result_dict.update({
                    "status": STATUS_RECYCLED, "reason": "",
                    "message": u"已移动到套件回收站（可在任务中心整组恢复）",
                    "recycle_path": destination,
                    "trash_mode": "package"})
                self._audit_file_result(job, result_dict)
                return result_dict
            recycle_dir = self.shares.recycle_dir(share)
            if not self.shares.has_recycle_dir(share):
                if not job.create_recycle_dir:
                    base.update({"status": STATUS_SKIPPED,
                                 "reason": REASON_RECYCLE_UNAVAILABLE,
                                 "message": u"共享文件夹没有 #recycle 回收站目录"})
                    return base
                try:
                    os.makedirs(recycle_dir, exist_ok=True)
                except OSError as exc:
                    base.update({"status": STATUS_FAILED,
                                 "reason": REASON_RECYCLE_UNAVAILABLE,
                                 "message": u"创建回收站目录失败: %s" % exc})
                    return base
            try:
                destination = recycle_destination(share["path"], path, stamp)
                parent = os.path.dirname(destination)
                ancestor, ancestor_stat = _nearest_existing(parent)
                source_identity = fsutil.file_identity(path)
                if (ancestor_stat is not None and source_identity is not None
                        and int(ancestor_stat.st_dev) != source_identity["dev"]):
                    base.update({"status": STATUS_FAILED,
                                 "reason": REASON_CROSS_DEVICE,
                                 "message": u"回收站与文件不在同一文件系统，禁止跨设备移动"})
                    return base
                os.makedirs(parent, exist_ok=True)
                # 文件删除点：移动到 <共享文件夹根>/#recycle/@duplicate-cleaner/<时间戳>/<相对路径>
                os.rename(path, destination)
            except OSError as exc:
                base.update({"status": STATUS_FAILED,
                             "reason": REASON_CROSS_DEVICE
                             if getattr(exc, "errno", None) == 18 else REASON_INTERNAL,
                             "message": u"移动到回收站失败: %s" % exc})
                return base
            result_dict = dict(base)
            result_dict.update({"status": STATUS_RECYCLED, "reason": "",
                                "message": u"已移动到回收站",
                                "recycle_path": destination})
            self._audit_file_result(job, result_dict)
            return result_dict

        try:
            # 文件删除点：永久删除（os.unlink）
            os.unlink(path)
        except OSError as exc:
            base.update({"status": STATUS_FAILED, "reason": REASON_INTERNAL,
                         "message": u"删除失败: %s" % exc})
            return base
        result_dict = dict(base)
        result_dict.update({"status": STATUS_DELETED, "reason": "",
                            "message": u"已永久删除"})
        self._audit_file_result(job, result_dict)
        return result_dict

    def _audit_file_result(self, job, result):
        detail = {
            "job_id": job.job_id,
            "scan_id": job.scan_id,
            "path": result.get("path"),
            "status": result.get("status"),
            "match_type": result.get("match_type") or "exact",
            "similarity_score": result.get("similarity_score"),
            "recycle_path": result.get("recycle_path"),
            "size": result.get("size"),
        }
        self.store.audit("delete_file", job.user, detail)

    def _record_result(self, job, result):
        with job.lock:
            job.done += 1
            job.bytes_done += int(result.get("size") or 0)
            if result.get("status") in (STATUS_FAILED,):
                job.failed += 1
            elif result.get("status") == STATUS_SKIPPED:
                job.skipped += 1
            if len(job.results) < config.JOB_RESULTS_MEMORY_LIMIT:
                job.results.append(result)
            else:
                job.results_truncated = True
        try:
            self.store.append_job_result(job.job_id, result)
        except OSError:
            pass

    def _persist_job(self, job, force=False):
        now = time.time()
        if not force and now - job.last_persist < 1.0:
            return
        job.last_persist = now
        try:
            self.store.save_job_meta(job.job_id, job.to_meta())
        except Exception:
            pass

    def _write_job_log(self, job, final_status=None):
        try:
            snapshot = job.snapshot()
            status = final_status or snapshot["status"]
            lines = [
                u"重复文件清理与字幕 删除任务日志",
                u"job_id: %s" % job.job_id,
                u"scan_id: %s" % job.scan_id,
                u"plan_id: %s" % job.plan_id,
                u"user: %s" % job.user,
                u"recycle: %s" % job.recycle,
                u"started_at: %s" % snapshot["started_at"],
                u"finished_at: %s" % snapshot["finished_at"],
                u"status: %s" % status,
                u"total: %s done: %s failed: %s skipped: %s" % (
                    job.total, job.done, job.failed, job.skipped),
                u"bytes_total: %s bytes_done: %s" % (job.bytes_total, job.bytes_done),
                u"results: SQLite deletion_results（逐文件结果已入库；"
                u"旧 log_file 字段仅作兼容标识）",
                u"",
            ]
            self.store.write_log("delete-%s.log" % job.job_id, "\n".join(lines) + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_job(self, scan_id, job_id):
        if not isinstance(job_id, str) or not config.JOB_ID_RE.match(job_id):
            raise not_found(u"删除任务不存在")
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None:
            if job.scan_id != scan_id:
                raise not_found(u"删除任务不属于该扫描")
            return job.snapshot()
        meta = self.store.load_job_meta(job_id)
        if not isinstance(meta, dict):
            raise not_found(u"删除任务不存在: %s" % job_id)
        if meta.get("scan_id") != scan_id:
            raise not_found(u"删除任务不属于该扫描")
        results = []
        truncated = False
        for result in self.store.iter_job_results(job_id,
                                                  limit=config.JOB_RESULTS_MEMORY_LIMIT + 1):
            if len(results) >= config.JOB_RESULTS_MEMORY_LIMIT:
                truncated = True
                break
            results.append(result)
        meta["results"] = results
        meta["results_truncated"] = truncated
        return meta

    def list_jobs(self, scan_id=None, limit=50):
        result = []
        with self._lock:
            jobs = list(self._jobs.values())
        seen = set()
        for job in jobs:
            if scan_id is not None and job.scan_id != scan_id:
                continue
            seen.add(job.job_id)
            result.append(job.to_meta())
        for meta in self.store.list_job_metas(scan_id=scan_id, limit=limit):
            if meta.get("job_id") in seen:
                continue
            result.append(meta)
        result.sort(key=lambda item: item.get("started_epoch") or 0, reverse=True)
        return result[:max(1, int(limit or 50))]

    def stop_all(self, timeout=5.0):
        deadline = time.time() + timeout
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            thread = job.thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=max(0.1, deadline - time.time()))
