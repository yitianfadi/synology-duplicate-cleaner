# -*- coding: utf-8 -*-
"""硬链接合并（v0045）：把重复组的非保留项替换成指向保留项的硬链接。

安全设计（与“删除”同级的不可逆操作，默认关闭）：
- 只处理**精准重复组**（内容哈希一致）；相似/视频组不能合并（内容不同）。
- 必须 `merge_enabled=true`；必须显式选择文件/组；计划 5 分钟过期、一次性；
  执行需 `confirm="MERGE"`。
- 计划阶段校验：扫描记录仍一致（size/mtime_ns/inode/dev）、受保护文件跳过、
  与保留项同一文件系统（跨卷无法硬链接）、已经是硬链接的跳过。
- 执行阶段再跑一次完整重校验（含全量哈希），然后 `link(master, tmp)` +
  `rename(tmp, candidate)` 原子替换；任何失败进入 skipped/failed，不中断整体。
- 每个合并任务写 manifest `<var>/merges/<job_id>.json`，支持“恢复为独立副本”
  （Btrfs FICLONE 秒级复制，非 Btrfs 回退普通复制）。
- 合并后同一 inode 被多个路径共享：**任一路径的内容修改会影响所有路径**；
  恢复为独立副本可撤销这种共享（但不能恢复“原来的第二个 inode”本身）。
"""

from __future__ import absolute_import

import json
import logging
import os
import secrets
import shutil
import threading
import time
import traceback

from . import config, fsutil, hashing, protection, timeutil
from .errors import ApiError, invalid, forbidden, not_found
from .deletion import revalidate

logger = logging.getLogger("dupcleaner.merge")

STATUS_MERGED = "merged"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

REASON_ALREADY_LINKED = "ALREADY_LINKED"
REASON_CROSS_DEVICE = "CROSS_DEVICE"
REASON_NOT_LINKED = "NOT_LINKED"
REASON_MISSING = "MISSING"
REASON_RESTORE_EXISTS = "RESTORE_EXISTS"


def make_merge_job_id():
    return "mrg-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def make_merge_plan_id():
    return "mplan-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


class MergeJobState(object):
    def __init__(self, job_id, scan_id, plan_id, user, total, bytes_total,
                 plan_path):
        self.job_id = job_id
        self.scan_id = scan_id
        self.plan_id = plan_id
        self.user = user
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
        self.entries = []
        self.lock = threading.RLock()
        self.thread = None
        self.last_persist = 0.0

    def snapshot(self, include_results=True):
        with self.lock:
            progress = (float(self.done) / self.total) if self.total else 1.0
            data = {
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
                "started_at": timeutil.iso_local(self.started_epoch),
                "finished_at": timeutil.iso_local(self.finished_epoch),
                "entries": len(self.entries),
            }
            if include_results:
                data["results"] = list(self.results)
                data["results_truncated"] = bool(self.results_truncated)
            return data


def _copy_as_independent(source, destination):
    """把 source 复制成独立副本写到 destination（FICLONE 优先，回退普通复制）。"""
    source_stat = os.stat(source)
    temp = destination + ".dc-restore-%s" % secrets.token_hex(4)
    try:
        cloned = False
        try:
            import fcntl  # 仅 POSIX；Windows 无该模块
        except ImportError:
            fcntl = None
        if fcntl is not None:
            try:
                with open(source, "rb") as src, open(temp, "wb") as dst:
                    fcntl.ioctl(dst.fileno(), config.FICLONE_IOCTL,
                                src.fileno())
                cloned = True
            except (OSError, IOError):
                cloned = False
                try:
                    if os.path.exists(temp):
                        os.remove(temp)
                except OSError:
                    pass
        if not cloned:
            shutil.copy2(source, temp)
        try:
            os.utime(temp, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        except OSError:
            pass
        os.replace(temp, destination)
        return cloned
    finally:
        try:
            if os.path.exists(temp):
                os.remove(temp)
        except OSError:
            pass


class MergeManager(object):
    def __init__(self, runtime_config, store, share_service, scan_manager,
                 settings_manager, protector_factory=None):
        self.config = runtime_config
        self.store = store
        self.shares = share_service
        self.scanner = scan_manager
        self.settings = settings_manager
        self._protector_factory = protector_factory or protection.Protector
        self._lock = threading.RLock()
        self._plans = {}
        self._jobs = {}
        self._jobs_by_scan = {}

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def manifest_dir(self):
        return os.path.join(self.config.data_dir,
                            config.MERGE_MANIFEST_SUBDIR)

    def _manifest_path(self, job_id):
        if not isinstance(job_id, str) or not job_id.startswith("mrg-"):
            raise not_found(u"合并任务不存在")
        return os.path.join(self.manifest_dir(), job_id + ".json")

    def _settings(self):
        try:
            return self.settings.get() or {}
        except Exception:  # pragma: no cover
            return {}

    @staticmethod
    def _keeper_of(group):
        keep_file_id = group.get("keep_file_id")
        for file_info in group.get("files") or []:
            if file_info.get("is_keep") or file_info.get("file_id") == keep_file_id:
                return file_info
        return None

    def _write_manifest(self, manifest):
        directory = self.manifest_dir()
        os.makedirs(directory, exist_ok=True)
        path = self._manifest_path(manifest["job_id"])
        tmp = path + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(tmp, path)

    def load_manifest(self, job_id):
        path = self._manifest_path(job_id)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError:
            return None
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            return None
        return manifest if isinstance(manifest, dict) else None

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------
    def create_plan(self, session, scan_id, payload):
        settings = self._settings()
        if not settings.get("merge_enabled"):
            raise ApiError("INVALID_ARGUMENT",
                           u"硬链接合并未启用：请先在设置页开启（默认关闭，属不可逆操作）")
        meta = self.scanner.require_completed(scan_id)
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        options = meta.get("options") or {}
        file_ids = payload.get("file_ids")
        scope = payload.get("scope")
        if file_ids is not None:
            if not isinstance(file_ids, list) or not file_ids:
                raise invalid(u"file_ids 必须是非空数组")
            wanted = set(str(item) for item in file_ids)
        elif scope in ("all_exact", "group"):
            wanted = None
        else:
            raise invalid(u"必须提供 file_ids 或 scope=all_exact")
        group_ids = payload.get("group_ids")
        group_set = None
        if isinstance(group_ids, list) and group_ids:
            group_set = set(str(item) for item in group_ids)
        if wanted is None and scope == "group" and group_set is None:
            raise invalid(u"scope=group 需要 group_ids")

        protector = self._protector_factory(settings)
        hash_cache = {}
        candidates = []
        skipped = []
        seen_ids = set()
        for group in self.store.iter_scan_groups(scan_id):
            match_type = group.get("match_type") or "exact"
            if match_type != "exact":
                continue
            group_id = group.get("group_id")
            if group_set is not None and group_id not in group_set:
                continue
            keeper = self._keeper_of(group)
            if keeper is None:
                continue
            keeper_path = keeper.get("path") or ""
            for file_info in group.get("files") or []:
                file_id = file_info.get("file_id")
                path = file_info.get("path") or ""
                if wanted is not None and file_id not in wanted:
                    continue
                seen_ids.add(file_id)
                base_skip = {"path": path, "file_id": file_id}
                if file_info.get("is_keep") or file_id == group.get("keep_file_id"):
                    skipped.append(dict(base_skip, reason="IS_KEEPER",
                                        message=u"保留项不能合并（它本身是链接目标）"))
                    continue
                if group.get("needs_manual_keep"):
                    skipped.append(dict(base_skip, reason="MANUAL_KEEP_REQUIRED",
                                        message=u"该组需人工确认保留项，不能合并"))
                    continue
                protect_state = protector.check(path)
                if protect_state is not None:
                    skipped.append(dict(base_skip, reason="PROTECTED",
                                        message=protector.describe(protect_state)))
                    continue
                # 先看“现在是不是已经共享 inode”（并发/重复提交的兜底），
                # 再做扫描记录一致性校验，避免报 FILE_CHANGED 掩盖真实原因。
                identity = fsutil.file_identity(path)
                keeper_identity = fsutil.file_identity(keeper_path)
                if identity is None or keeper_identity is None:
                    skipped.append(dict(base_skip, reason="NOT_FOUND",
                                        message=u"文件已不存在"))
                    continue
                if identity["dev"] != keeper_identity["dev"]:
                    skipped.append(dict(
                        base_skip, reason=REASON_CROSS_DEVICE,
                        message=u"候选与保留项不在同一文件系统，无法硬链接"))
                    continue
                if identity["inode"] == keeper_identity["inode"]:
                    skipped.append(dict(
                        base_skip, reason=REASON_ALREADY_LINKED,
                        message=u"已经是同一 inode 的硬链接，无需合并"))
                    continue
                result = revalidate(file_info, keeper, self.shares,
                                    algo=(options.get("hash_algo") or "sha256"),
                                    hash_check=False, hash_cache=hash_cache)
                if not result["ok"]:
                    skipped.append(dict(base_skip, reason=result["code"],
                                        message=result["message"]))
                    continue
                candidates.append({
                    "file": file_info,
                    "keeper": keeper,
                    "group_id": group_id,
                    "match_type": "exact",
                    "algo": options.get("hash_algo") or "sha256",
                })
        if wanted is not None:
            for file_id in sorted(wanted - seen_ids):
                skipped.append({"path": "", "file_id": file_id,
                                "reason": "NOT_FOUND",
                                "message": u"文件不在可合并范围内: %s" % file_id})
        if not candidates:
            details = {"skipped": skipped[:200]}
            raise ApiError("PLAN_INVALID", u"没有可合并的文件（需精准重复组的非保留项）",
                           details=details)
        plan_id = make_merge_plan_id()
        now = time.time()
        directory = self.store.plans_dir
        os.makedirs(directory, exist_ok=True)
        internal_path = os.path.join(directory, plan_id + ".files.jsonl")
        tmp_path = internal_path + ".tmp"
        with open(tmp_path, "wb") as handle:
            for entry in candidates:
                handle.write((json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
        os.replace(tmp_path, internal_path)
        total_bytes = sum(int((entry["file"] or {}).get("size") or 0)
                          for entry in candidates)
        plan = {
            "plan_id": plan_id,
            "scan_id": scan_id,
            "session_id": session.session_id,
            "user": session.user,
            "created_epoch": now,
            "expires_epoch": now + config.MERGE_PLAN_TTL_SECONDS,
            "expires_at": timeutil.iso_local(now + config.MERGE_PLAN_TTL_SECONDS),
            "count": len(candidates),
            "bytes": total_bytes,
            "groups_affected": len(set(str(entry["group_id"]) for entry in candidates)),
            "skipped": skipped,
            "internal_path": internal_path,
            "consumed": False,
        }
        with self._lock:
            self._purge_expired_plans_locked()
            self._plans[plan_id] = plan
        self.store.audit("merge_plan", session.user, {
            "scan_id": scan_id,
            "plan_id": plan_id,
            "count": plan["count"],
            "bytes": plan["bytes"],
            "skipped": len(skipped),
        })
        return {
            "plan_id": plan_id,
            "expires_at": plan["expires_at"],
            "count": plan["count"],
            "bytes": plan["bytes"],
            "groups_affected": plan["groups_affected"],
            "skipped": skipped[:500],
            "skipped_total": len(skipped),
            "files": [{"file_id": (entry["file"] or {}).get("file_id"),
                       "path": (entry["file"] or {}).get("path"),
                       "size": (entry["file"] or {}).get("size"),
                       "group_id": entry["group_id"],
                       "keeper_path": (entry["keeper"] or {}).get("path")}
                      for entry in candidates[:config.PLAN_FILES_RESPONSE_LIMIT]],
            "files_total": len(candidates),
            "warning": u"硬链接合并后所有路径共享同一份内容，任一修改会影响其它路径；"
                       u"可用“恢复为独立副本”撤销共享（不恢复原 inode）。",
        }

    def _purge_expired_plans_locked(self):
        now = time.time()
        for plan_id in list(self._plans.keys()):
            plan = self._plans.get(plan_id)
            if plan is None or plan.get("consumed"):
                self._plans.pop(plan_id, None)
                continue
            if now > plan.get("expires_epoch", 0):
                self._plans.pop(plan_id, None)
                try:
                    os.remove(plan.get("internal_path") or "")
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def start_merge(self, session, scan_id, payload):
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        plan_id = payload.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id.startswith("mplan-"):
            raise ApiError("PLAN_INVALID", u"合并计划 ID 非法或已过期")
        confirm = payload.get("confirm")
        if confirm != "MERGE":
            raise invalid(u'必须提供 confirm="MERGE" 二次确认')
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                raise ApiError("PLAN_INVALID", u"合并计划不存在或已使用")
            if plan["scan_id"] != scan_id:
                raise ApiError("PLAN_INVALID", u"合并计划不属于该扫描任务")
            if plan["session_id"] != session.session_id:
                raise forbidden(u"该计划只对创建它的会话有效")
            if time.time() > plan["expires_epoch"]:
                self._plans.pop(plan_id, None)
                raise ApiError("PLAN_EXPIRED", u"合并计划已过期，请重新生成")
            plan["consumed"] = True
            self._plans.pop(plan_id, None)
        job = MergeJobState(
            job_id=make_merge_job_id(),
            scan_id=scan_id,
            plan_id=plan_id,
            user=session.user,
            total=plan["count"],
            bytes_total=plan["bytes"],
            plan_path=plan["internal_path"],
        )
        job.entries = []
        with self._lock:
            self._jobs[job.job_id] = job
            self._jobs_by_scan.setdefault(scan_id, []).append(job.job_id)
        self.store.audit("merge_start", session.user, {
            "scan_id": scan_id,
            "plan_id": plan_id,
            "job_id": job.job_id,
            "count": plan["count"],
            "bytes": plan["bytes"],
        })
        job.thread = threading.Thread(target=self._run_job, args=(job,),
                                      name="merge-" + job.job_id, daemon=True)
        job.thread.start()
        return job.job_id

    def _run_job(self, job):
        hash_cache = {}
        try:
            try:
                handle = open(job.plan_path, "rb")
            except OSError:
                raise ApiError("PLAN_INVALID", u"合并计划文件丢失")
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
                    result = self._merge_one(job, entry, hash_cache)
                    self._record_result(job, result)
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
            # 先写清单/审计再对外暴露终态：避免轮询方看到 completed 时清单还没落盘。
            self._write_job_manifest(job, final_status)
            self.store.audit("merge_end", job.user, {
                "scan_id": job.scan_id,
                "job_id": job.job_id,
                "status": final_status,
                "merged": job.done,
                "failed": job.failed,
                "skipped": job.skipped,
                "bytes": job.bytes_done,
            })
            with job.lock:
                job.status = final_status
        except Exception as exc:  # pragma: no cover
            self._write_job_manifest(job, "failed")
            with job.lock:
                job.status = "failed"
                job.finished_epoch = time.time()
            self.store.audit("merge_end", job.user, {
                "scan_id": job.scan_id,
                "job_id": job.job_id,
                "status": "failed",
                "error": str(exc),
            })
            try:
                self.store.write_log(
                    "merge-%s.log" % job.job_id,
                    u"合并任务异常:\n" + traceback.format_exc() + "\n")
            except Exception:
                pass

    def _record_result(self, job, result):
        with job.lock:
            if len(job.results) < config.JOB_RESULTS_MEMORY_LIMIT:
                job.results.append(result)
            else:
                job.results_truncated = True
            status = result.get("status")
            if status == STATUS_MERGED:
                job.done += 1
                job.bytes_done += int(result.get("size") or 0)
            elif status == STATUS_FAILED:
                job.failed += 1
            else:
                job.skipped += 1

    def _merge_one(self, job, entry, hash_cache):
        file_info = entry.get("file") or {}
        keeper = entry.get("keeper") or {}
        path = file_info.get("path") or ""
        keeper_path = keeper.get("path") or ""
        algo = entry.get("algo") or "sha256"
        base = {
            "path": path,
            "master": keeper_path,
            "file_id": file_info.get("file_id"),
            "group_id": entry.get("group_id"),
            "size": int(file_info.get("size") or 0),
        }
        result = revalidate(file_info, keeper, self.shares, algo=algo,
                            hash_check=True, hash_cache=hash_cache)
        if not result["ok"]:
            base.update({"status": STATUS_SKIPPED, "reason": result["code"],
                         "message": result["message"]})
            return base
        try:
            candidate_identity = fsutil.file_identity(path)
            master_identity = fsutil.file_identity(keeper_path)
        except OSError as exc:
            base.update({"status": STATUS_FAILED, "reason": "INTERNAL_ERROR",
                         "message": u"读取文件身份失败: %s" % exc})
            return base
        if candidate_identity is None or master_identity is None:
            base.update({"status": STATUS_SKIPPED, "reason": "NOT_FOUND",
                         "message": u"文件已不存在"})
            return base
        if candidate_identity["dev"] != master_identity["dev"]:
            base.update({"status": STATUS_SKIPPED,
                         "reason": REASON_CROSS_DEVICE,
                         "message": u"候选与保留项不在同一文件系统"})
            return base
        if candidate_identity["inode"] == master_identity["inode"]:
            base.update({"status": STATUS_SKIPPED,
                         "reason": REASON_ALREADY_LINKED,
                         "message": u"已经是硬链接，无需合并"})
            return base
        temp = path + ".dc-merge-%s" % secrets.token_hex(4)
        try:
            os.link(keeper_path, temp)
            os.replace(temp, path)
        except OSError as exc:
            try:
                if os.path.exists(temp):
                    os.remove(temp)
            except OSError:
                pass
            base.update({"status": STATUS_FAILED, "reason": "INTERNAL_ERROR",
                         "message": u"创建硬链接失败: %s" % exc})
            return base
        entry["merged_at"] = time.time()
        entry["inode_before"] = int(candidate_identity["inode"])
        with job.lock:
            job.entries.append({
                "path": path,
                "master": keeper_path,
                "size": base["size"],
                "group_id": entry.get("group_id"),
                "merged_at": entry["merged_at"],
                "restored_at": None,
            })
        base.update({"status": STATUS_MERGED, "reason": "",
                     "message": u"已合并为硬链接",
                     "freed_bytes": base["size"]})
        return base

    def _write_job_manifest(self, job, status=None):
        with job.lock:
            entries = list(job.entries)
            if status is None:
                status = job.status
        if not entries:
            return
        manifest = {
            "version": 1,
            "job_id": job.job_id,
            "scan_id": job.scan_id,
            "user": job.user,
            "created_epoch": job.started_epoch,
            "created_at": timeutil.iso_local(job.started_epoch),
            "status": status,
            "entries": entries,
        }
        try:
            self._write_manifest(manifest)
        except Exception:  # pragma: no cover
            logger.warning(u"写入合并清单失败：%s", exc_info=True)

    # ------------------------------------------------------------------
    # 查询 / 恢复
    # ------------------------------------------------------------------
    def get_job(self, scan_id, job_id):
        job = self._jobs.get(job_id)
        if job is not None:
            snapshot = job.snapshot()
            snapshot["manifest"] = self.load_manifest(job_id) is not None
            return snapshot
        manifest = self.load_manifest(job_id)
        if manifest is None or manifest.get("scan_id") != scan_id:
            raise not_found(u"合并任务不存在")
        return {
            "job_id": manifest.get("job_id"),
            "scan_id": manifest.get("scan_id"),
            "status": manifest.get("status") or "completed",
            "total": len(manifest.get("entries") or []),
            "done": len(manifest.get("entries") or []),
            "failed": 0,
            "skipped": 0,
            "bytes_total": sum(int(item.get("size") or 0)
                               for item in manifest.get("entries") or []),
            "bytes_done": sum(int(item.get("size") or 0)
                              for item in manifest.get("entries") or []),
            "started_at": manifest.get("created_at"),
            "finished_at": manifest.get("created_at"),
            "results": [],
            "entries": len(manifest.get("entries") or []),
            "manifest": True,
        }

    def detail(self, job_id):
        """合并记录明细（manifest + 每条路径当前状态）。"""
        manifest = self.load_manifest(job_id)
        if manifest is None:
            raise not_found(u"合并记录不存在")
        entries = []
        for index, item in enumerate(manifest.get("entries") or []):
            path = item.get("path") or ""
            master = item.get("master") or ""
            path_state = "missing"
            if os.path.exists(path):
                path_state = "exists"
                try:
                    path_identity = fsutil.file_identity(path)
                    master_identity = fsutil.file_identity(master)
                except OSError:
                    path_identity = master_identity = None
                if path_identity is not None and master_identity is not None:
                    if (path_identity["dev"] == master_identity["dev"]
                            and path_identity["inode"] == master_identity["inode"]):
                        path_state = "linked"
                    else:
                        path_state = "independent"
            entries.append({
                "index": index,
                "path": path,
                "master": master,
                "size": int(item.get("size") or 0),
                "group_id": item.get("group_id"),
                "merged_at": item.get("merged_at"),
                "restored_at": item.get("restored_at"),
                "state": path_state,
            })
        return {
            "job_id": manifest.get("job_id"),
            "scan_id": manifest.get("scan_id"),
            "status": manifest.get("status") or "completed",
            "created_at": manifest.get("created_at"),
            "entries": entries,
            "entries_total": len(entries),
            "restored": sum(1 for item in entries if item.get("restored_at")),
        }

    def list_jobs(self, scan_id=None, limit=50):
        items = []
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if scan_id is not None and job.scan_id != scan_id:
                continue
            items.append(job.snapshot(include_results=False))
        directory = self.manifest_dir()
        seen = set()
        try:
            names = sorted(os.listdir(directory), reverse=True)
        except OSError:
            names = []
        for name in names:
            if not name.endswith(".json") or len(items) >= limit:
                continue
            job_id = name[:-5]
            if any(item.get("job_id") == job_id for item in items):
                continue
            manifest = self.load_manifest(job_id)
            if manifest is None:
                continue
            if scan_id is not None and manifest.get("scan_id") != scan_id:
                continue
            entries = manifest.get("entries") or []
            items.append({
                "job_id": manifest.get("job_id"),
                "scan_id": manifest.get("scan_id"),
                "status": manifest.get("status") or "completed",
                "total": len(entries),
                "done": len(entries),
                "failed": 0,
                "skipped": 0,
                "bytes_total": sum(int(item.get("size") or 0)
                                   for item in entries),
                "bytes_done": sum(int(item.get("size") or 0)
                                  for item in entries),
                "started_at": manifest.get("created_at"),
                "finished_at": manifest.get("created_at"),
                "entries": len(entries),
                "manifest": True,
            })
        items.sort(key=lambda item: str(item.get("started_at") or ""),
                   reverse=True)
        return items[:max(1, min(int(limit or 50), 200))]

    def restore(self, session, job_id, payload):
        """把合并过的路径恢复成独立副本（FICLONE 优先，回退普通复制）。"""
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        if payload.get("confirm") is not True:
            raise invalid(u"恢复为独立副本需要 confirm=true 确认")
        manifest = self.load_manifest(job_id)
        if manifest is None:
            raise not_found(u"合并记录不存在")
        wanted = payload.get("paths")
        wanted_set = None
        if isinstance(wanted, list) and wanted:
            wanted_set = set(str(item) for item in wanted)
        results = []
        restored = 0
        reflinked = 0
        with self._lock:
            for item in manifest.get("entries") or []:
                path = item.get("path") or ""
                if item.get("restored_at"):
                    continue
                if wanted_set is not None and path not in wanted_set:
                    continue
                entry_result = {"path": path, "status": "", "reason": "",
                                "message": ""}
                if not os.path.exists(path):
                    entry_result.update({"status": "skipped",
                                         "reason": REASON_MISSING,
                                         "message": u"文件已不存在"})
                    results.append(entry_result)
                    continue
                source = item.get("master") or ""
                if not os.path.isfile(source):
                    # 保留项不在时，用同 inode 的其它路径作为复制源
                    source = ""
                    for other in manifest.get("entries") or []:
                        candidate = other.get("path") or ""
                        if candidate and os.path.isfile(candidate):
                            source = candidate
                            break
                    if not source:
                        source = path
                try:
                    path_identity = fsutil.file_identity(path)
                    source_identity = fsutil.file_identity(source)
                except OSError as exc:
                    entry_result.update({"status": "failed",
                                         "reason": "INTERNAL_ERROR",
                                         "message": u"读取文件失败: %s" % exc})
                    results.append(entry_result)
                    continue
                if (path_identity is None or source_identity is None):
                    entry_result.update({"status": "skipped",
                                         "reason": REASON_MISSING,
                                         "message": u"文件已不存在"})
                    results.append(entry_result)
                    continue
                if (path_identity["dev"] != source_identity["dev"]
                        or path_identity["inode"] != source_identity["inode"]):
                    entry_result.update({
                        "status": "skipped", "reason": REASON_NOT_LINKED,
                        "message": u"该路径已不是硬链接（可能已被替换），跳过"})
                    results.append(entry_result)
                    continue
                try:
                    cloned = _copy_as_independent(source, path)
                except OSError as exc:
                    entry_result.update({"status": "failed",
                                         "reason": "INTERNAL_ERROR",
                                         "message": u"复制失败: %s" % exc})
                    results.append(entry_result)
                    continue
                item["restored_at"] = time.time()
                item["restore_mode"] = "reflink" if cloned else "copy"
                restored += 1
                if cloned:
                    reflinked += 1
                entry_result.update({"status": "restored",
                                     "mode": "reflink" if cloned else "copy"})
                results.append(entry_result)
            if restored:
                self._write_manifest(manifest)
            self.store.audit("merge_restore", session.user, {
                "job_id": job_id,
                "restored": restored,
                "reflink": reflinked,
                "skipped": sum(1 for item in results
                               if item.get("status") == "skipped"),
                "failed": sum(1 for item in results
                              if item.get("status") == "failed"),
            })
        return {"job_id": job_id, "restored": restored,
                "reflink": reflinked, "results": results}

    def stop_all(self, timeout=5.0):
        deadline = time.time() + timeout
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.thread is not None:
                job.thread.join(timeout=max(0.1, deadline - time.time()))
