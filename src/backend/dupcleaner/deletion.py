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
import os
import secrets
import threading
import time
import traceback

from . import config, fsutil, hashing, timeutil
from .errors import ApiError, forbidden, invalid, not_found, path_not_allowed

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

    candidate_hash = record.get("hash") or ""
    keeper_hash = (keeper_record or {}).get("hash") or ""
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
                 settings_manager, hash_verifier=None):
        self.config = runtime_config
        self.store = store
        self.shares = share_service
        self.scanner = scan_manager
        self.settings = settings_manager
        self._hash_verifier = hash_verifier or _fresh_hash
        self._lock = threading.RLock()
        self._plans = {}
        self._jobs = {}
        self._jobs_by_scan = {}

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------
    def create_plan(self, session, scan_id, payload):
        meta = self.scanner.require_completed(scan_id)
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        recycle = payload.get("recycle", True)
        if not isinstance(recycle, bool):
            raise invalid(u"recycle 必须是布尔值")
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
        else:
            raise invalid(u"必须提供 file_ids 或 scope=all_duplicates")

        plan_files = []
        groups_affected = set()
        recycle_unavailable = []
        recycle_seen = set()
        hash_cache = {}
        candidate_bytes = sum(int(file_info.get("size") or 0)
                              for _, file_info, _keeper in candidates)
        hash_verify_budget = config.PLAN_HASH_VERIFY_BUDGET_BYTES
        hash_check = candidate_bytes <= hash_verify_budget

        for group, file_info, keeper in candidates:
            group_id = group.get("group_id")
            path = file_info.get("path") or ""
            result = revalidate(file_info, keeper, self.shares, algo=algo,
                                hash_check=hash_check, hash_cache=hash_cache)
            if not result["ok"]:
                skipped.append({"path": path, "file_id": file_info.get("file_id"),
                                "reason": result["code"],
                                "message": result["message"]})
                continue
            snapshot = result.get("identity")
            # 回收站可用性
            if recycle:
                try:
                    share = self.shares.share_root(path)
                except ApiError as exc:
                    skipped.append({"path": path, "file_id": file_info.get("file_id"),
                                    "reason": REASON_PATH_NOT_ALLOWED,
                                    "message": exc.message})
                    continue
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
                        and source_identity["dev"] != int(share_identity.st_dev)):
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
                "algo": algo,
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
        plan_id = make_plan_id()
        now = time.time()
        internal_path = self._write_plan_files(plan_id, plan_files)
        public_files = [
            {
                "file_id": entry["file"].get("file_id"),
                "path": entry["file"].get("path"),
                "size": entry["file"].get("size"),
                "group_id": entry["group_id"],
            }
            for entry in plan_files
        ]
        plan = {
            "plan_id": plan_id,
            "scan_id": scan_id,
            "session_id": session.session_id,
            "user": session.user,
            "recycle": recycle,
            "create_recycle_dir": create_recycle_dir,
            "algo": algo,
            "created_epoch": now,
            "expires_epoch": now + config.PLAN_TTL_SECONDS,
            "expires_at": timeutil.iso_local(now + config.PLAN_TTL_SECONDS),
            "count": len(plan_files),
            "bytes": total_bytes,
            "groups_affected": len(groups_affected),
            "files": public_files,
            "skipped": skipped,
            "recycle_unavailable": recycle_unavailable,
            "hash_verified": bool(hash_check),
            "hash_verification_deferred": not hash_check,
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
            "skipped": len(skipped),
            "hash_verified": plan["hash_verified"],
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
            "count": plan["count"],
            "bytes": plan["bytes"],
            "files": files[:config.PLAN_FILES_RESPONSE_LIMIT],
            "files_total": len(files),
            "files_truncated": truncated,
            "skipped": skipped[:2000],
            "skipped_total": len(skipped),
            "skipped_truncated": skipped_truncated,
            "groups_affected": plan["groups_affected"],
            "recycle_unavailable": plan["recycle_unavailable"],
            "hash_verified": plan["hash_verified"],
            "hash_verification_deferred": plan["hash_verification_deferred"],
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
            "count": plan["count"],
            "bytes": plan["bytes"],
        })
        job.thread = threading.Thread(target=self._run_job, args=(job,),
                                      name="delete-" + job_id, daemon=True)
        job.thread.start()
        return job_id

    def _run_job(self, job):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        hash_cache = {}
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
                    self._record_result(job, result)
                    self._persist_job(job)
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
            })
            with job.lock:
                job.status = final_status
            self._persist_job(job, force=True)
        except Exception as exc:  # pragma: no cover
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

    def _delete_one(self, job, entry, stamp, hash_cache):
        file_info = entry.get("file") or {}
        keeper = entry.get("keeper") or {}
        path = file_info.get("path") or ""
        algo = entry.get("algo") or "sha256"
        base = {
            "path": path,
            "file_id": file_info.get("file_id"),
            "size": int(file_info.get("size") or 0),
            "recycle_path": None,
        }
        result = revalidate(file_info, keeper, self.shares, algo=algo,
                            hash_check=True, hash_cache=hash_cache)
        if not result["ok"]:
            base.update({"status": STATUS_SKIPPED, "reason": result["code"],
                         "message": result["message"]})
            return base
        try:
            share = self.shares.share_root(path)
        except ApiError as exc:
            base.update({"status": STATUS_SKIPPED,
                         "reason": REASON_PATH_NOT_ALLOWED,
                         "message": exc.message})
            return base

        if job.recycle:
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
                u"DuplicateCleaner 删除任务日志",
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
                u"results_log: deletions/%s.jsonl" % job.job_id,
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
