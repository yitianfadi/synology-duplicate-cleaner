# -*- coding: utf-8 -*-
"""套件独立回收站（v0045）：删除文件进 `<share>/#duplicate-cleaner-trash/...`，
按删除任务记录 manifest，支持整组恢复与清理。

为什么不用共享文件夹 `#recycle`：DSM 回收站 UI 只认它自己的目录结构，手工塞进去
的文件在 File Station 里可能不显示，也无法“按删除批次整组恢复”。独立目录放在共享
文件夹根（同卷 rename，秒级、不复制数据），`#` 前缀会被扫描器当作系统目录跳过。
"""

from __future__ import absolute_import

import json
import os
import shutil
import threading
import time

from . import config, fsutil, timeutil
from .errors import ApiError, invalid, not_found, path_not_allowed

MANIFEST_VERSION = 1


class TrashManager(object):
    def __init__(self, runtime_config, store, share_service):
        self.config = runtime_config
        self.store = store
        self.shares = share_service
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 路径 / 校验
    # ------------------------------------------------------------------
    def manifest_dir(self):
        return os.path.join(self.config.data_dir,
                            config.TRASH_MANIFEST_SUBDIR)

    def _manifest_path(self, trash_id):
        self._check_id(trash_id)
        return os.path.join(self.manifest_dir(), trash_id + ".json")

    @staticmethod
    def _check_id(trash_id):
        if not isinstance(trash_id, str) or not config.JOB_ID_RE.match(trash_id):
            raise not_found(u"回收站记录不存在")

    def trash_root(self, share_root):
        return os.path.join(share_root, config.TRASH_DIR_NAME)

    def destination_for(self, share_root, source_path, stamp):
        """`<share>/#duplicate-cleaner-trash/<stamp>/<相对路径>`（同名冲突加后缀）。"""
        relative = os.path.relpath(source_path, share_root)
        target = os.path.join(self.trash_root(share_root), stamp, relative)
        if not os.path.exists(target):
            return target
        base, extension = os.path.splitext(target)
        for index in range(1, 1000):
            candidate = "%s.trash-%d%s" % (base, index, extension)
            if not os.path.exists(candidate):
                return candidate
        raise ApiError("INTERNAL_ERROR", u"无法生成不冲突的回收站路径")

    # ------------------------------------------------------------------
    # manifest 读写
    # ------------------------------------------------------------------
    def _write_manifest(self, manifest):
        directory = self.manifest_dir()
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise ApiError("INTERNAL_ERROR", u"创建回收站清单目录失败: %s" % exc)
        path = self._manifest_path(manifest["trash_id"])
        tmp = path + ".tmp"
        data = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        return path

    def load(self, trash_id):
        path = self._manifest_path(trash_id)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError:
            return None
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            return None
        if not isinstance(manifest, dict):
            return None
        if not isinstance(manifest.get("entries"), list):
            manifest["entries"] = []
        return manifest

    def record_job(self, job_id, scan_id, user, entries, recycle_mode="package"):
        """删除任务结束后登记 manifest（entries 为已成功移动的文件）。"""
        entries = [item for item in (entries or []) if isinstance(item, dict)]
        if not entries:
            return None
        now = time.time()
        manifest = {
            "version": MANIFEST_VERSION,
            "trash_id": job_id,
            "scan_id": scan_id,
            "user": user,
            "recycle_mode": recycle_mode,
            "created_epoch": now,
            "created_at": timeutil.iso_local(now),
            "purged": False,
            "entries": [],
        }
        for item in entries:
            original = item.get("original") or item.get("path") or ""
            trash_path = item.get("trash_path") or item.get("recycle_path") or ""
            if not original or not trash_path:
                continue
            manifest["entries"].append({
                "original": original,
                "trash_path": trash_path,
                "size": int(item.get("size") or 0),
                "group_id": item.get("group_id") or "",
                "keeper_path": item.get("keeper_path") or "",
                "file_id": item.get("file_id") or "",
                "match_type": item.get("match_type") or "exact",
                "restored_at": None,
                "purged_at": None,
            })
        if not manifest["entries"]:
            return None
        with self._lock:
            self._write_manifest(manifest)
        self.store.audit("trash_record", user or "system", {
            "trash_id": job_id,
            "scan_id": scan_id,
            "entries": len(manifest["entries"]),
            "bytes": sum(item["size"] for item in manifest["entries"]),
        })
        return self._summary(manifest)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def _summary(self, manifest, check=False):
        entries = manifest.get("entries") or []
        restored = sum(1 for item in entries if item.get("restored_at"))
        purged = sum(1 for item in entries if item.get("purged_at"))
        remaining = len(entries) - restored - purged
        bytes_total = sum(int(item.get("size") or 0) for item in entries)
        bytes_remaining = sum(
            int(item.get("size") or 0) for item in entries
            if not item.get("restored_at") and not item.get("purged_at"))
        summary = {
            "trash_id": manifest.get("trash_id"),
            "scan_id": manifest.get("scan_id"),
            "created_at": manifest.get("created_at"),
            "created_epoch": manifest.get("created_epoch"),
            "entries_total": len(entries),
            "entries_restored": restored,
            "entries_remaining": remaining,
            "bytes_total": bytes_total,
            "bytes_remaining": bytes_remaining,
            "purged": bool(manifest.get("purged")),
        }
        if check:
            exists = 0
            missing = 0
            conflicts = 0
            for item in entries:
                if item.get("restored_at") or item.get("purged_at"):
                    continue
                if os.path.isfile(item.get("trash_path") or ""):
                    exists += 1
                else:
                    missing += 1
                if os.path.exists(item.get("original") or ""):
                    conflicts += 1
            summary.update({"available": exists, "missing": missing,
                            "conflicts": conflicts})
        return summary

    def list_jobs(self, limit=100):
        directory = self.manifest_dir()
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return []
        manifests = []
        for name in names:
            if not name.endswith(".json"):
                continue
            trash_id = name[:-5]
            try:
                manifest = self.load(trash_id)
            except (ApiError, OSError):
                continue
            if manifest is None:
                continue
            manifests.append(manifest)
        manifests.sort(key=lambda item: -(item.get("created_epoch") or 0))
        limit = max(1, min(int(limit or 100), 500))
        return [self._summary(item) for item in manifests[:limit]]

    def detail(self, trash_id):
        manifest = self.load(trash_id)
        if manifest is None:
            raise not_found(u"回收站记录不存在")
        summary = self._summary(manifest, check=True)
        entries = []
        for index, item in enumerate(manifest.get("entries") or []):
            trash_path = item.get("trash_path") or ""
            original = item.get("original") or ""
            entries.append({
                "index": index,
                "original": original,
                "trash_path": trash_path,
                "size": int(item.get("size") or 0),
                "group_id": item.get("group_id") or "",
                "keeper_path": item.get("keeper_path") or "",
                "restored_at": item.get("restored_at"),
                "purged_at": item.get("purged_at"),
                "trash_exists": os.path.isfile(trash_path),
                "original_exists": os.path.exists(original),
            })
        return {"summary": summary, "entries": entries,
                "recycle_mode": manifest.get("recycle_mode") or "package"}

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------
    def _validate_original(self, path):
        try:
            normalized = fsutil.check_path_safety(path)
        except ApiError as exc:
            return None, exc
        share = self.shares.find_share(normalized)
        if share is None:
            return None, path_not_allowed(u"恢复目标不在共享文件夹内")
        if fsutil.has_symlink_component(normalized, share["path"]):
            return None, path_not_allowed(u"恢复目标路径包含符号链接")
        return share, None

    def restore(self, session, trash_id, payload):
        """整组恢复（默认全部未恢复项；可传 paths 只恢复部分）。"""
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        if payload.get("confirm") is not True:
            raise invalid(u"恢复需要 confirm=true 确认")
        with self._lock:
            manifest = self.load(trash_id)
            if manifest is None:
                raise not_found(u"回收站记录不存在")
            if manifest.get("purged"):
                raise invalid(u"该回收站记录已清理，无法恢复")
            wanted = payload.get("paths")
            wanted_set = None
            if isinstance(wanted, list) and wanted:
                wanted_set = set(str(item) for item in wanted)
            results = []
            restored = 0
            for item in manifest.get("entries") or []:
                original = item.get("original") or ""
                if item.get("restored_at") or item.get("purged_at"):
                    continue
                if wanted_set is not None and original not in wanted_set:
                    continue
                entry_result = {"original": original, "status": "",
                                "reason": "", "message": ""}
                trash_path = item.get("trash_path") or ""
                if not os.path.isfile(trash_path):
                    entry_result.update({
                        "status": "skipped", "reason": "TRASH_MISSING",
                        "message": u"回收站内文件已不存在"})
                    results.append(entry_result)
                    continue
                if os.path.exists(original):
                    entry_result.update({
                        "status": "skipped", "reason": "ORIGINAL_EXISTS",
                        "message": u"原位置已存在同名文件，未覆盖"})
                    results.append(entry_result)
                    continue
                share, error = self._validate_original(original)
                if error is not None:
                    entry_result.update({
                        "status": "skipped",
                        "reason": getattr(error, "code", "PATH_NOT_ALLOWED"),
                        "message": error.message})
                    results.append(entry_result)
                    continue
                try:
                    parent = os.path.dirname(original)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    os.rename(trash_path, original)
                except OSError as exc:
                    if getattr(exc, "errno", None) == 18:
                        try:
                            shutil.copy2(trash_path, original + ".restore.tmp")
                            os.replace(original + ".restore.tmp", original)
                            os.remove(trash_path)
                        except OSError as copy_exc:
                            entry_result.update({
                                "status": "failed", "reason": "CROSS_DEVICE",
                                "message": u"恢复失败: %s" % copy_exc})
                            results.append(entry_result)
                            continue
                    else:
                        entry_result.update({
                            "status": "failed", "reason": "INTERNAL_ERROR",
                            "message": u"恢复失败: %s" % exc})
                        results.append(entry_result)
                        continue
                item["restored_at"] = time.time()
                restored += 1
                entry_result.update({"status": "restored"})
                results.append(entry_result)
            if restored:
                for item in manifest.get("entries") or []:
                    if item.get("restored_at"):
                        pass
                self._write_manifest(manifest)
                self._prune_empty_dirs(manifest)
            self.store.audit("trash_restore", session.user, {
                "trash_id": trash_id,
                "restored": restored,
                "skipped": sum(1 for item in results
                               if item.get("status") == "skipped"),
                "failed": sum(1 for item in results
                              if item.get("status") == "failed"),
            })
        return {
            "trash_id": trash_id,
            "restored": restored,
            "results": results,
            "summary": self._summary(manifest, check=True),
        }

    def purge(self, session, trash_id, payload):
        """永久清理回收站内容（需要 confirm + “永久删除”短语）。"""
        if not isinstance(payload, dict):
            raise invalid(u"请求体必须是 JSON 对象")
        if payload.get("confirm") is not True:
            raise invalid(u"清理回收站需要 confirm=true 确认")
        phrase = payload.get("permanent_phrase")
        if phrase != config.REQUIRED_PERMANENT_PHRASE:
            raise invalid(u"清理回收站必须输入确认短语“%s”"
                          % config.REQUIRED_PERMANENT_PHRASE)
        with self._lock:
            manifest = self.load(trash_id)
            if manifest is None:
                raise not_found(u"回收站记录不存在")
            removed = 0
            failed = 0
            bytes_freed = 0
            for item in manifest.get("entries") or []:
                if item.get("restored_at") or item.get("purged_at"):
                    continue
                trash_path = item.get("trash_path") or ""
                if not trash_path or not fsutil.is_under(
                        trash_path, self._trash_root_of(manifest)):
                    failed += 1
                    continue
                size = int(item.get("size") or 0)
                if os.path.isfile(trash_path):
                    try:
                        os.unlink(trash_path)
                        removed += 1
                        bytes_freed += size
                    except OSError:
                        failed += 1
                        continue
                item["purged_at"] = time.time()
            manifest["purged"] = True
            self._write_manifest(manifest)
            self._prune_empty_dirs(manifest)
            self.store.audit("trash_purge", session.user, {
                "trash_id": trash_id,
                "removed": removed,
                "failed": failed,
                "bytes_freed": bytes_freed,
            })
        return {"trash_id": trash_id, "removed": removed, "failed": failed,
                "bytes_freed": bytes_freed,
                "summary": self._summary(manifest)}

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _trash_root_of(self, manifest):
        """从 manifest 的条目反推回收站根（用于清理时的路径白名单校验）。"""
        for item in manifest.get("entries") or []:
            trash_path = item.get("trash_path") or ""
            share = self.shares.find_share(trash_path)
            if share is not None:
                return self.trash_root(share["path"])
        return ""

    def _prune_empty_dirs(self, manifest):
        """删除回收站内已空的目录（只清 <share>/#duplicate-cleaner-trash 内）。"""
        directories = set()
        for item in manifest.get("entries") or []:
            trash_path = item.get("trash_path") or ""
            share = self.shares.find_share(trash_path)
            if share is None:
                continue
            directory = os.path.dirname(trash_path)
            while directory and fsutil.is_under(
                    directory, self.trash_root(share["path"])):
                directories.add(directory)
                parent = os.path.dirname(directory)
                if parent == directory:
                    break
                directory = parent
        for directory in sorted(directories, key=len, reverse=True):
            try:
                os.rmdir(directory)
            except OSError:
                continue
        for item in manifest.get("entries") or []:
            share = self.shares.find_share(item.get("trash_path") or "")
            if share is None:
                continue
            try:
                os.rmdir(self.trash_root(share["path"]))
            except OSError:
                continue
