# -*- coding: utf-8 -*-
"""扫描引擎：枚举 → 大小分组 → 快速指纹 → 全量哈希 → 分组/保留策略。

安全与性能要点：
- 单进程同一时间只允许一个扫描（SCAN_BUSY）。
- 不跟随符号链接；默认跳过隐藏/系统目录与文件；#recycle/@eaDir 永不扫描。
- 硬链接按 (dev, inode) 去重，避免“删了不释放空间”。
- max_files 上限保护：超过即停止并报错（TOO_MANY_FILES）。
- 支持暂停 / 继续 / 停止；进度、统计实时更新并持久化。
- 全量哈希结论才用于判定重复；快速指纹只用于缩小候选集。
"""

from __future__ import absolute_import

import concurrent.futures
import copy
import csv
import heapq
import os
import secrets
import stat as stat_module
import threading
import time
import traceback

from . import (config, fsutil, hashing, settings as settings_module,
               similarity, timeutil)
from .errors import ApiError, invalid, not_found

STAGES = ("enumerate", "size", "sample", "hash", "similarity", "finalize", "done")
FINISHED_STATUSES = ("stopped", "completed", "failed")
ACTIVE_STATUSES = ("queued", "running", "paused", "stopping")
DEFAULT_LIST_LIMIT = 50


class ScanStopped(Exception):
    pass


class ScanLimitError(Exception):
    def __init__(self, seen, limit):
        super(ScanLimitError, self).__init__(
            u"文件数量 %d 超过上限 %d" % (seen, limit))
        self.seen = seen
        self.limit = limit


class FileRecord(object):
    __slots__ = ("path", "size", "mtime_ns", "inode", "dev", "nlink",
                 "quick_hash", "hash")

    def __init__(self, path, size, mtime_ns, inode, dev, nlink):
        self.path = path
        self.size = int(size)
        self.mtime_ns = int(mtime_ns)
        self.inode = int(inode)
        self.dev = int(dev)
        self.nlink = int(nlink)
        self.quick_hash = None
        self.hash = None


def _empty_skipped():
    return {
        "symlink": 0,
        "hidden": 0,
        "empty": 0,
        "extension": 0,
        "size": 0,
        "hardlink": 0,
        "excluded": 0,
        "permission": 0,
        "other": 0,
    }


def _empty_stats():
    return {
        "files_seen": 0,
        "dirs_scanned": 0,
        "files_scanned": 0,
        "candidate_files": 0,
        "groups": 0,
        "duplicate_groups": 0,
        "duplicate_files": 0,
        "reclaimable_bytes": 0,
        "selectable_duplicate_files": 0,
        "selectable_reclaimable_bytes": 0,
        "bytes_hashed": 0,
        "bytes_per_sec": 0,
        "errors": 0,
        "skipped": _empty_skipped(),
        # 相似度模式统计（docs/SIMILARITY_CONTRACT.md §3.4）
        "exact_groups": 0,
        "exact_files": 0,
        "similar_groups": 0,
        "similar_files": 0,
        "similar_reclaimable_bytes": 0,
        "similarity_candidates": 0,
        "similarity_features": 0,
        "similarity_cache_hits": 0,
        "similarity_cache_misses": 0,
        "similarity_pairs_checked": 0,
        "mtime_fallback_groups": 0,
        "mtime_fallback_files": 0,
        "similarity_skipped": {
            "type": 0, "size": 0, "binary": 0,
            "decode": 0, "no_text": 0, "error": 0,
        },
    }


def make_scan_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def _time_key(file_info):
    if (file_info.get("created_ts") is not None
            and file_info.get("created_source") in ("statx", "birthtime")):
        return (int(file_info["created_ts"]), int(file_info.get("created_ns") or 0))
    return None


def _fallback_key(file_info, latest):
    mtime_ns = int(file_info.get("mtime_ns") or 0)
    if latest:
        return (mtime_ns, file_info.get("path") or "")
    return (-mtime_ns, file_info.get("path") or "")


def _mtime_key(file_info):
    return int(file_info.get("mtime_ns") or 0)


def _choose_by_mtime(files, latest):
    """创建时间不可信时整组改用 mtime_ns 选保留项（绝不用 ctime 比较）。

    返回 (keeper_index, keep_reason, tie_flag, mtime_impossible)。
    """
    if latest:
        keeper = max(range(len(files)),
                     key=lambda i: (_mtime_key(files[i]),
                                    files[i].get("path") or ""))
    else:
        keeper = min(range(len(files)),
                     key=lambda i: (_mtime_key(files[i]),
                                    files[i].get("path") or ""))
    mtimes = set(_mtime_key(file_info) for file_info in files)
    if len(mtimes) == 1:
        return keeper, "mtime_tie", True, False
    reason = "latest_modified_fallback" if latest else "earliest_modified_fallback"
    return keeper, reason, False, False


def choose_keeper(files, options):
    """选择保留文件。

    返回 (keeper_index, keep_reason, needs_manual_keep, creation_suspect,
    time_fallback, keep_time_source)。

    - 组内所有文件都有可信 btime：latest_created / earliest_created；
      可信 btime 相同（tie）仍要求人工确认（原有行为）。
    - 只要有一个文件 btime 不可信：整组改用 mtime_ns（绝不混用 btime/ctime），
      latest_modified_fallback / earliest_modified_fallback / mtime_tie，
      不再需要人工确认。
    - pinned：命中路径优先保留；btime 不可信时仍保留 pinned（无需人工）；
      未命中任何文件时按 mtime 兜底（新的确定性行为，不再 needs_manual）。
    """
    keep_mode = options.get("keep_mode") or "latest"
    pinned = (options.get("pinned_path") or "").strip()
    all_trusted = all(_time_key(file_info) is not None for file_info in files)
    suspect = any(file_info.get("created_source") in ("ctime", "unknown")
                  for file_info in files)

    if pinned:
        matched = []
        for index, file_info in enumerate(files):
            path = file_info.get("path") or ""
            if path == pinned or fsutil.is_under(path, pinned):
                matched.append(index)
        if matched:
            if all_trusted:
                matched_keys = {index: _time_key(files[index])
                                for index in matched}
                if len(matched) == 1:
                    return matched[0], "pinned_path", False, suspect, False, "btime"
                if len(set(matched_keys.values())) == 1:
                    keeper = min(matched, key=lambda i: files[i].get("path") or "")
                    return keeper, "tie", True, suspect, False, "btime"
                keeper = max(matched,
                             key=lambda i: (matched_keys[i],
                                            files[i].get("path") or ""))
                return keeper, "pinned_path", False, suspect, False, "btime"
            # btime 不可信：仍保留 pinned（用 mtime 最新做确定性兜底），无需人工
            keeper = max(matched,
                         key=lambda i: (_mtime_key(files[i]),
                                        files[i].get("path") or ""))
            return keeper, "pinned_path", False, True, True, "mtime"
        # pinned 未命中任何文件：整组按 mtime 兜底（不再 needs_manual）
        keeper, reason, _tie, _impossible = _choose_by_mtime(
            files, latest=(keep_mode != "earliest"))
        return keeper, reason, False, True, True, "mtime"

    if all_trusted:
        keys = [_time_key(file_info) for file_info in files]
        latest = keep_mode != "earliest"
        if latest:
            keeper = max(range(len(files)),
                         key=lambda i: (keys[i], files[i].get("path") or ""))
        else:
            keeper = min(range(len(files)),
                         key=lambda i: (keys[i], files[i].get("path") or ""))
        if len(set(keys)) == 1:
            return keeper, "tie", True, suspect, False, "btime"
        reason = "latest_created" if latest else "earliest_created"
        return keeper, reason, False, suspect, False, "btime"

    # btime 不可信（unknown/ctime/部分缺失）：整组用 mtime，绝不混用
    keeper, reason, _tie, _impossible = _choose_by_mtime(
        files, latest=(keep_mode != "earliest"))
    return keeper, reason, False, True, True, "mtime"


class ScanState(object):
    def __init__(self, scan_id, roots, excludes, options, user):
        self.scan_id = scan_id
        self.roots = list(roots)
        self.excludes = list(excludes)
        self.options = dict(options)
        self.user = user
        self.status = "queued"
        self.stage = "enumerate"
        self.progress = 0.0
        self.message = u"排队中"
        self.stats = _empty_stats()
        self.current_path = ""
        self.created_epoch = time.time()
        self.started_epoch = None
        self.finished_epoch = None
        self.error = None
        self.creation_time_unsafe = False
        self.needs_manual_keep_groups = 0
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.running_event = threading.Event()
        self.running_event.set()
        self.thread = None
        self.last_persist = 0.0
        self.keep_reason = options.get("keep_mode") or "latest"

    def snapshot(self):
        with self.lock:
            started = self.started_epoch
            finished = self.finished_epoch
            if started is None:
                elapsed = 0.0
            else:
                elapsed = (finished or time.time()) - started
            stats = copy.deepcopy(self.stats)
            if elapsed and elapsed > 0:
                stats["bytes_per_sec"] = int(stats.get("bytes_hashed", 0) / elapsed)
            else:
                stats["bytes_per_sec"] = 0
            return {
                "scan_id": self.scan_id,
                "status": self.status,
                "stage": self.stage,
                "progress": round(float(self.progress), 4),
                "message": self.message,
                "started_at": timeutil.iso_local(started),
                "finished_at": timeutil.iso_local(finished),
                "created_at": timeutil.iso_local(self.created_epoch),
                "created_epoch": self.created_epoch,
                "elapsed_sec": round(max(0.0, elapsed), 3),
                "current_path": self.current_path,
                "stats": stats,
                "creation_time_unsafe": bool(self.creation_time_unsafe),
                "needs_manual_keep_groups": int(self.needs_manual_keep_groups),
                "options": copy.deepcopy(self.options),
                "error": copy.deepcopy(self.error),
                "config": {
                    "roots": list(self.roots),
                    "excludes": list(self.excludes),
                    "keep_mode": self.options.get("keep_mode"),
                    "hash_algo": self.options.get("hash_algo"),
                },
                "user": self.user,
            }

    def to_meta(self):
        meta = self.snapshot()
        meta["created_epoch"] = float(self.created_epoch)
        return meta


class ScanManager(object):
    def __init__(self, runtime_config, store, share_service, settings_manager,
                 time_reader=None, executor_factory=None):
        self.config = runtime_config
        self.store = store
        self.shares = share_service
        self.settings = settings_manager
        self._time_reader = time_reader or timeutil.read_creation_time
        self._executor_factory = executor_factory or (
            lambda workers: concurrent.futures.ThreadPoolExecutor(max_workers=workers))
        self._lock = threading.RLock()
        self._active_id = None
        self._states = {}

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------
    def start_scan(self, roots, excludes, options_override, user):
        if not isinstance(roots, list) or not roots:
            raise invalid(u"roots 必须是非空数组")
        if len(roots) > config.MAX_ROOTS:
            raise invalid(u"扫描根目录最多 %d 个" % config.MAX_ROOTS)
        if excludes is None:
            excludes = []
        if not isinstance(excludes, list):
            raise invalid(u"excludes 必须是数组")
        if len(excludes) > config.MAX_EXCLUDES:
            raise invalid(u"排除目录最多 %d 个" % config.MAX_EXCLUDES)
        if options_override is None:
            options_override = {}
        current = self.settings.get()
        merged = settings_module.merge_settings(current, options_override)

        share_list = self.shares.list_shares()
        normalized_roots = []
        seen_roots = set()
        for raw in roots:
            share = self.shares.validate_root(raw, shares=share_list)
            root = os.path.normpath(os.path.abspath(raw))
            key = fsutil.path_key(root)
            if key in seen_roots:
                continue
            # 根目录互相嵌套时只保留上层，避免重复枚举
            nested = False
            for existing in normalized_roots:
                if fsutil.is_under(root, existing):
                    nested = True
                    break
            if nested:
                continue
            normalized_roots = [item for item in normalized_roots
                                if not fsutil.is_under(item, root)]
            seen_roots.add(key)
            normalized_roots.append(root)
        if not normalized_roots:
            raise invalid(u"没有有效的扫描根目录")

        normalized_excludes = []
        for raw in excludes:
            if not isinstance(raw, str) or not raw.strip():
                raise invalid(u"排除目录必须是路径字符串")
            normalized = fsutil.check_path_safety(raw)
            if not any(fsutil.is_under(normalized, root, allow_equal=False)
                       for root in normalized_roots):
                raise ApiError("INVALID_ARGUMENT",
                               u"排除目录必须在某个扫描根目录内: %s" % normalized)
            normalized_excludes.append(normalized)

        pinned = (merged.get("pinned_path") or "").strip()
        if merged.get("keep_mode") == "pinned" and pinned:
            pinned_norm = fsutil.check_path_safety(pinned, reject_hidden=False)
            if self.shares.find_share(pinned_norm, shares=share_list) is None:
                raise ApiError("PATH_NOT_ALLOWED",
                               u"指定保留路径必须在共享文件夹内: %s" % pinned_norm)
            merged["pinned_path"] = pinned_norm

        with self._lock:
            active = self._states.get(self._active_id) if self._active_id else None
            if active is not None and active.status in ACTIVE_STATUSES:
                raise ApiError("SCAN_BUSY", u"已有扫描正在进行，请等待完成或停止")
            scan_id = make_scan_id()
            state = ScanState(scan_id, normalized_roots, normalized_excludes,
                              merged, user)
            state.thread = threading.Thread(target=self._run, args=(state,),
                                            name="scan-" + scan_id, daemon=True)
            self._states[scan_id] = state
            self._active_id = scan_id

        self.store.audit("scan_start", user, {
            "scan_id": scan_id,
            "roots": normalized_roots,
            "excludes": normalized_excludes,
            "keep_mode": merged.get("keep_mode"),
            "hash_algo": merged.get("hash_algo"),
        })
        try:
            self.store.write_log("scan-%s.log" % scan_id, self._log_header(state))
        except Exception:
            pass
        state.thread.start()
        return scan_id

    def _log_header(self, state):
        lines = [
            u"DuplicateCleaner 扫描日志",
            u"scan_id: %s" % state.scan_id,
            u"started_at: %s" % timeutil.iso_local(state.created_epoch),
            u"user: %s" % state.user,
            u"roots: %s" % "; ".join(state.roots),
            u"excludes: %s" % ("; ".join(state.excludes) or "(none)"),
            u"keep_mode: %s" % state.options.get("keep_mode"),
            u"hash_algo: %s" % state.options.get("hash_algo"),
            u"concurrency: %s" % state.options.get("concurrency"),
            u"hash_rate_mbps: %s" % state.options.get("hash_rate_mbps"),
            u"scan_mode: %s" % (state.options.get("scan_mode") or "exact"),
            u"similarity_threshold: %s" % state.options.get("similarity_threshold"),
            u"",
        ]
        return "\n".join(lines) + "\n"

    def _active_state(self):
        with self._lock:
            active_id = self._active_id
        if not active_id:
            return None
        with self._lock:
            state = self._states.get(active_id)
        if state is not None and state.status in ACTIVE_STATUSES:
            return state
        return None

    def pause(self, scan_id):
        state = self._require_state(scan_id)
        with state.lock:
            if state.status != "running":
                raise invalid(u"当前状态不允许暂停: %s" % state.status)
            state.status = "paused"
            state.running_event.clear()
            state.message = u"扫描已暂停"
        self._persist(state, force=True)
        return state.snapshot()

    def resume(self, scan_id):
        state = self._require_state(scan_id)
        with state.lock:
            if state.status != "paused":
                raise invalid(u"当前状态不允许继续: %s" % state.status)
            state.status = "running"
            state.running_event.set()
            state.message = u"扫描继续"
        self._persist(state, force=True)
        return state.snapshot()

    def stop(self, scan_id):
        state = self._require_state(scan_id)
        with state.lock:
            if state.status not in ("running", "paused", "queued", "stopping"):
                raise invalid(u"当前状态不允许停止: %s" % state.status)
            state.stop_event.set()
            state.running_event.set()
            if state.status != "stopping":
                state.status = "stopping"
                state.message = u"正在停止扫描"
        self._persist(state, force=True)
        return state.snapshot()

    def stop_all(self, timeout=10.0):
        with self._lock:
            states = list(self._states.values())
        for state in states:
            if state.status in ACTIVE_STATUSES:
                try:
                    self.stop(state.scan_id)
                except ApiError:
                    pass
        deadline = time.time() + timeout
        for state in states:
            thread = state.thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=max(0.1, deadline - time.time()))

    def _require_state(self, scan_id):
        if not isinstance(scan_id, str) or not config.SCAN_ID_RE.match(scan_id):
            raise not_found(u"扫描不存在")
        with self._lock:
            state = self._states.get(scan_id)
        if state is None:
            raise not_found(u"扫描不存在: %s" % scan_id)
        return state

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_scan(self, scan_id):
        meta = self._load_meta(scan_id)
        if meta is None:
            raise not_found(u"扫描不存在: %s" % scan_id)
        return meta

    def _load_meta(self, scan_id):
        if not isinstance(scan_id, str) or not config.SCAN_ID_RE.match(scan_id):
            raise not_found(u"扫描不存在")
        with self._lock:
            state = self._states.get(scan_id)
        if state is not None:
            return state.snapshot()
        return self.store.load_scan_meta(scan_id)

    def list_scans(self, limit=DEFAULT_LIST_LIMIT):
        metas = {}
        for meta in self.store.list_scan_metas(limit=200):
            scan_id = meta.get("scan_id")
            if scan_id:
                metas[scan_id] = meta
        with self._lock:
            states = list(self._states.values())
        for state in states:
            metas[state.scan_id] = state.to_meta()
        ordered = sorted(metas.values(),
                         key=lambda item: item.get("created_epoch") or 0,
                         reverse=True)
        items = []
        for meta in ordered[:max(1, int(limit or DEFAULT_LIST_LIMIT))]:
            items.append({
                "scan_id": meta.get("scan_id"),
                "status": meta.get("status"),
                "stage": meta.get("stage"),
                "progress": meta.get("progress"),
                "started_at": meta.get("started_at"),
                "finished_at": meta.get("finished_at"),
                "stats": meta.get("stats"),
                "options": meta.get("options"),
                "config": meta.get("config") or {
                    "roots": (meta.get("config") or {}).get("roots", []),
                    "keep_mode": (meta.get("options") or {}).get("keep_mode"),
                },
                "error": meta.get("error"),
                "needs_manual_keep_groups": meta.get("needs_manual_keep_groups", 0),
                "creation_time_unsafe": meta.get("creation_time_unsafe", False),
            })
        return items

    def require_completed(self, scan_id):
        meta = self.get_scan(scan_id)
        if (meta or {}).get("status") != "completed":
            status = (meta or {}).get("status") or "unknown"
            raise ApiError("SCAN_NOT_DONE", u"扫描尚未完成（当前状态：%s）" % status)
        return meta

    def groups_page(self, scan_id, offset=0, limit=100, q=None,
                    sort="wasted_desc", match_type=None):
        self.require_completed(scan_id)
        try:
            offset = max(0, int(offset or 0))
            limit = int(limit if limit is not None else 100)
        except (TypeError, ValueError):
            raise invalid(u"分页参数非法")
        limit = max(1, min(limit, 500))
        needle = (q or "").strip().lower()
        sort = sort or "wasted_desc"
        if sort not in ("wasted_desc", "size_desc", "created_desc",
                        "similarity_desc"):
            raise invalid(u"不支持的排序方式: %s" % sort)
        if match_type in ("", "all"):
            match_type = None
        if match_type is not None and match_type not in ("exact", "similar"):
            raise invalid(u"match_type 只能是 exact 或 similar")

        def group_match_type(group):
            return group.get("match_type") or "exact"

        def matches(group):
            if not needle:
                return True
            if needle in (group.get("hash") or "").lower():
                return True
            for file_info in group.get("files") or []:
                if needle in (file_info.get("path") or "").lower():
                    return True
                if needle in (file_info.get("name") or "").lower():
                    return True
            return False

        def sort_key(group):
            wasted = int(group.get("wasted_bytes") or 0)
            size = int(group.get("size") or 0)
            if sort == "size_desc":
                return (size, wasted, group.get("group_id") or "")
            if sort == "created_desc":
                created = 0
                for file_info in group.get("files") or []:
                    created = max(created, int(file_info.get("created_ts") or 0))
                return (created, wasted, group.get("group_id") or "")
            if sort == "similarity_desc":
                similarity = 100
                if group_match_type(group) == "similar":
                    similarity = int(group.get("similarity_score") or 0)
                return (similarity, wasted, size, group.get("group_id") or "")
            return (wasted, size, group.get("group_id") or "")

        exact_total = 0
        similar_total = 0
        total = 0
        wanted = offset + limit
        top = []
        for group in self.store.iter_scan_groups(scan_id):
            if not isinstance(group, dict):
                continue
            if group_match_type(group) == "similar":
                similar_total += 1
            else:
                exact_total += 1
            if match_type is not None and group_match_type(group) != match_type:
                continue
            if not matches(group):
                continue
            total += 1
            item = (sort_key(group), group)
            if len(top) < wanted:
                heapq.heappush(top, (item[0], total, group))
            else:
                if item[0] > top[0][0]:
                    heapq.heapreplace(top, (item[0], total, group))
        ordered = [entry[2] for entry in sorted(top, key=lambda x: x[0], reverse=True)]
        page = ordered[offset:offset + limit]
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "match_type": match_type,
            "exact_total": exact_total,
            "similar_total": similar_total,
            "groups": page,
        }

    def stats_full(self, scan_id):
        meta = self.get_scan(scan_id)
        extension_counts = {}
        extension_bytes = {}
        dir_counts = {}
        for group in self.store.iter_scan_groups(scan_id):
            size = int(group.get("size") or 0)
            for file_info in group.get("files") or []:
                name = file_info.get("name") or ""
                extension = fsutil.extension_of(name).lower() or u"(无后缀)"
                extension_counts[extension] = extension_counts.get(extension, 0) + 1
                extension_bytes[extension] = extension_bytes.get(extension, 0) + size
                directory = os.path.dirname(file_info.get("path") or "")
                dir_counts[directory] = dir_counts.get(directory, 0) + 1
        by_extension = sorted(
            [{"extension": key, "count": value,
              "bytes": extension_bytes.get(key, 0)}
             for key, value in extension_counts.items()],
            key=lambda item: (-item["count"], item["extension"]))[:20]
        top_dirs = sorted(
            [{"path": key, "count": value} for key, value in dir_counts.items()],
            key=lambda item: (-item["count"], item["path"]))[:20]
        return {
            "scan_id": scan_id,
            "status": meta.get("status"),
            "stats": meta.get("stats") or {},
            "creation_time_unsafe": bool(meta.get("creation_time_unsafe")),
            "needs_manual_keep_groups": int(meta.get("needs_manual_keep_groups") or 0),
            "by_extension": by_extension,
            "top_dirs": top_dirs,
        }

    def write_csv(self, scan_id, dest_path=None):
        self.require_completed(scan_id)
        path = dest_path or self.store.temp_path(prefix="csv-", suffix=".csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([u"组号", u"匹配类型", u"状态", u"文件路径", u"大小(字节)",
                             u"SHA256/MD5", u"创建时间", u"修改时间", u"保留",
                             u"默认勾选", u"时间来源", u"相似度分值",
                             u"时间回退", u"保留时间来源"])
            for group in self.store.iter_scan_groups(scan_id):
                match_type = group.get("match_type") or "exact"
                match_label = u"内容相似" if match_type == "similar" else u"哈希一致"
                for file_info in group.get("files") or []:
                    if file_info.get("is_keep"):
                        status = u"保留"
                    elif file_info.get("default_selected"):
                        status = u"待删除"
                    else:
                        status = u"需人工确认"
                    writer.writerow([
                        group.get("group_id") or "",
                        match_label,
                        status,
                        file_info.get("path") or "",
                        file_info.get("size") or 0,
                        file_info.get("hash") or "",
                        file_info.get("created_at") or "",
                        file_info.get("modified_at") or "",
                        u"是" if file_info.get("is_keep") else u"否",
                        u"是" if file_info.get("default_selected") else u"否",
                        file_info.get("created_source") or "unknown",
                        int(file_info.get("similarity_score")
                            if file_info.get("similarity_score") is not None
                            else 100),
                        u"是" if group.get("time_fallback") else u"否",
                        group.get("keep_time_source") or "btime",
                    ])
        return path

    def filename_for_csv(self, scan_id):
        return "duplicate-scan-%s.csv" % scan_id

    # ------------------------------------------------------------------
    # 扫描执行
    # ------------------------------------------------------------------
    def _run(self, state):
        started = time.time()
        state.started_epoch = started
        try:
            with state.lock:
                state.status = "running"
                state.stage = "enumerate"
                state.message = u"正在枚举文件"
                state.progress = 0.01
            self._persist(state, force=True)

            records = self._enumerate(state)
            self._check_control(state)

            buckets = self._size_stage(state, records)
            self._check_control(state)

            candidate_buckets = self._sample_stage(state, buckets)
            del buckets
            self._check_control(state)

            hashed_buckets = self._hash_stage(state, candidate_buckets)
            del candidate_buckets
            self._check_control(state)

            # 精准组内文件不参与相似计算；相似阶段在精准哈希之后执行
            exact_paths = set()
            for bucket in hashed_buckets:
                for record in bucket:
                    exact_paths.add(record.path)
            similar_clusters = []
            if (state.options.get("scan_mode") or "exact") == "similar":
                similar_clusters = self._similarity_stage(state, records,
                                                          exact_paths)
            del records
            self._check_control(state)

            self._finalize_stage(state, hashed_buckets, similar_clusters)
            del hashed_buckets
            del similar_clusters

            with state.lock:
                state.status = "completed"
                state.stage = "done"
                state.progress = 1.0
                state.message = u"扫描完成"
                state.finished_epoch = time.time()
                state.current_path = ""
            self._persist(state, force=True)
            self._audit_finish(state, "scan_end")
            self._write_log_finish(state)
        except ScanStopped:
            with state.lock:
                state.status = "stopped"
                state.message = u"扫描已停止"
                state.finished_epoch = time.time()
                state.current_path = ""
            self._persist(state, force=True)
            self._audit_finish(state, "scan_stopped")
            self._write_log_finish(state)
        except ScanLimitError as exc:
            with state.lock:
                state.status = "failed"
                state.error = {
                    "code": "TOO_MANY_FILES",
                    "message": u"文件数量超过上限 %d，已停止扫描" % exc.limit,
                    "details": {"limit": exc.limit, "seen": exc.seen},
                }
                state.message = state.error["message"]
                state.finished_epoch = time.time()
                state.current_path = ""
            self._persist(state, force=True)
            self._audit_finish(state, "scan_failed")
            self._write_log_finish(state)
        except Exception as exc:  # pragma: no cover - 防御性兜底
            with state.lock:
                state.status = "failed"
                state.error = {
                    "code": "INTERNAL_ERROR",
                    "message": u"扫描失败：%s" % exc,
                    "details": {},
                }
                state.message = state.error["message"]
                state.finished_epoch = time.time()
                state.current_path = ""
            self._persist(state, force=True)
            try:
                self.store.write_log("scan-%s.log" % state.scan_id,
                                     u"扫描异常:\n" + traceback.format_exc() + "\n",
                                     append=True)
            except Exception:
                pass
            self._audit_finish(state, "scan_failed")
            self._write_log_finish(state)
        finally:
            with state.lock:
                state.running_event.set()
                if state.finished_epoch is None:
                    state.finished_epoch = time.time()
                self._active_id = None
            self._persist(state, force=True)

    def _audit_finish(self, state, action):
        try:
            snapshot = state.snapshot()
            self.store.audit(action, state.user, {
                "scan_id": state.scan_id,
                "status": snapshot["status"],
                "elapsed_sec": snapshot["elapsed_sec"],
                "scan_mode": state.options.get("scan_mode") or "exact",
                "similarity_threshold": state.options.get("similarity_threshold"),
                "stats": {
                    "files_seen": snapshot["stats"]["files_seen"],
                    "groups": snapshot["stats"]["groups"],
                    "exact_groups": snapshot["stats"].get("exact_groups", 0),
                    "similar_groups": snapshot["stats"].get("similar_groups", 0),
                    "duplicate_files": snapshot["stats"]["duplicate_files"],
                    "reclaimable_bytes": snapshot["stats"]["reclaimable_bytes"],
                    "similar_reclaimable_bytes": snapshot["stats"].get(
                        "similar_reclaimable_bytes", 0),
                    "errors": snapshot["stats"]["errors"],
                },
            })
        except Exception:
            pass

    def _write_log_finish(self, state):
        try:
            snapshot = state.snapshot()
            lines = [
                u"finished_at: %s" % timeutil.iso_local(snapshot["finished_at"]),
                u"status: %s" % snapshot["status"],
                u"elapsed_sec: %s" % snapshot["elapsed_sec"],
                u"files_seen: %s" % snapshot["stats"]["files_seen"],
                u"files_scanned: %s" % snapshot["stats"]["files_scanned"],
                u"candidate_files: %s" % snapshot["stats"]["candidate_files"],
                u"scan_mode: %s" % (snapshot["options"].get("scan_mode") or "exact"),
                u"similarity_threshold: %s" % snapshot["options"].get(
                    "similarity_threshold"),
                u"groups: %s" % snapshot["stats"]["groups"],
                u"exact_groups: %s" % snapshot["stats"].get("exact_groups", 0),
                u"similar_groups: %s" % snapshot["stats"].get("similar_groups", 0),
                u"exact_files: %s" % snapshot["stats"].get("exact_files", 0),
                u"similar_files: %s" % snapshot["stats"].get("similar_files", 0),
                u"similar_reclaimable_bytes: %s" % snapshot["stats"].get(
                    "similar_reclaimable_bytes", 0),
                u"similarity_candidates: %s" % snapshot["stats"].get(
                    "similarity_candidates", 0),
                u"similarity_features: %s" % snapshot["stats"].get(
                    "similarity_features", 0),
                u"similarity_cache_hits: %s" % snapshot["stats"].get(
                    "similarity_cache_hits", 0),
                u"similarity_cache_misses: %s" % snapshot["stats"].get(
                    "similarity_cache_misses", 0),
                u"similarity_pairs_checked: %s" % snapshot["stats"].get(
                    "similarity_pairs_checked", 0),
                u"similarity_skipped: %s" % snapshot["stats"].get(
                    "similarity_skipped", {}),
                u"duplicate_files: %s" % snapshot["stats"]["duplicate_files"],
                u"reclaimable_bytes: %s" % snapshot["stats"]["reclaimable_bytes"],
                u"errors: %s" % snapshot["stats"]["errors"],
                u"skipped: %s" % snapshot["stats"]["skipped"],
                u"creation_time_unsafe: %s" % snapshot["creation_time_unsafe"],
                u"needs_manual_keep_groups: %s" % snapshot["needs_manual_keep_groups"],
                u"mtime_fallback_groups: %s" % snapshot["stats"].get(
                    "mtime_fallback_groups", 0),
                u"mtime_fallback_files: %s" % snapshot["stats"].get(
                    "mtime_fallback_files", 0),
                u"",
            ]
            if snapshot.get("error"):
                lines.insert(0, u"error: %s" % snapshot["error"])
            self.store.write_log("scan-%s.log" % state.scan_id,
                                 "\n".join(lines) + "\n", append=True)
        except Exception:
            pass

    def _persist(self, state, force=False):
        now = time.time()
        if not force and now - state.last_persist < 1.0:
            return
        state.last_persist = now
        try:
            self.store.save_scan_meta(state.scan_id, state.to_meta())
        except Exception:
            pass

    def _check_control(self, state):
        if state.stop_event.is_set():
            raise ScanStopped()
        while not state.running_event.wait(0.2):
            if state.stop_event.is_set():
                raise ScanStopped()

    def _should_stop(self, state):
        while not state.running_event.wait(0.1):
            if state.stop_event.is_set():
                return True
        return state.stop_event.is_set()

    def _set_progress(self, state, stage, progress, message=None):
        with state.lock:
            state.stage = stage
            state.progress = max(0.0, min(1.0, float(progress)))
            if message is not None:
                state.message = message
        self._persist(state)

    def _inc(self, state, key, amount=1):
        with state.lock:
            state.stats[key] = state.stats.get(key, 0) + amount

    def _inc_skip(self, state, key, amount=1):
        with state.lock:
            state.stats["skipped"][key] = state.stats["skipped"].get(key, 0) + amount

    def _inc_error(self, state):
        with state.lock:
            state.stats["errors"] = state.stats.get("errors", 0) + 1

    # ------------------------------------------------------------------
    # 阶段一：枚举
    # ------------------------------------------------------------------
    def _enumerate(self, state):
        records = []
        seen_inodes = {}
        max_files = int(state.options.get("max_files") or config.MAX_FILES_DEFAULT)
        self._set_progress(state, "enumerate", 0.02, u"正在枚举文件")
        for root in state.roots:
            if self._is_excluded(state, root):
                continue
            self._walk(state, root, records, seen_inodes, max_files)
        records.sort(key=lambda record: record.path)
        with state.lock:
            state.stats["files_scanned"] = len(records)
        self._set_progress(state, "size", 0.18,
                           u"枚举完成：%d 个文件" % len(records))
        return records

    def _is_excluded(self, state, path):
        for exclude in state.excludes:
            if fsutil.is_under(path, exclude):
                return True
        return False

    def _walk(self, state, root, records, seen_inodes, max_files):
        skip_hidden = bool(state.options.get("skip_hidden", True))
        skip_empty = bool(state.options.get("skip_empty", True))
        include_ext = state.options.get("include_extensions") or []
        exclude_ext = state.options.get("exclude_extensions") or []
        ignore_case = bool(state.options.get("ignore_case_ext", True))
        if ignore_case:
            include_ext = [item.lower() for item in include_ext]
            exclude_ext = [item.lower() for item in exclude_ext]
        min_size = int(state.options.get("min_size_bytes") or 0)
        max_size = int(state.options.get("max_size_bytes") or 0)

        stack = [root]
        processed = 0
        while stack:
            current = stack.pop()
            self._check_control(state)
            with state.lock:
                state.current_path = current
                state.stats["dirs_scanned"] += 1
            try:
                with os.scandir(current) as iterator:
                    entries = list(iterator)
            except PermissionError:
                self._inc_skip(state, "permission")
                self._inc_error(state)
                continue
            except OSError:
                self._inc_skip(state, "other")
                self._inc_error(state)
                continue
            entries.sort(key=lambda item: item.name.lower())

            for entry in entries:
                self._check_control(state)
                processed += 1
                if processed % 64 == 0:
                    self._persist(state)
                name = entry.name
                try:
                    if entry.is_symlink():
                        self._inc_skip(state, "symlink")
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    self._inc_skip(state, "permission")
                    self._inc_error(state)
                    continue

                if is_dir:
                    # 系统目录（@eaDir/#recycle/#snapshot/@docker 等）永远跳过，
                    # 与 skip_hidden 无关；隐藏目录才受 skip_hidden 控制。
                    if fsutil.is_system_name(name):
                        self._inc_skip(state, "excluded")
                        continue
                    if skip_hidden and fsutil.is_hidden_name(name):
                        self._inc_skip(state, "hidden")
                        continue
                    if self._is_excluded(state, entry.path):
                        self._inc_skip(state, "excluded")
                        continue
                    stack.append(entry.path)
                    continue

                with state.lock:
                    state.stats["files_seen"] += 1
                    seen = state.stats["files_seen"]
                if seen > max_files:
                    raise ScanLimitError(seen, max_files)

                # 系统文件（@eaDir 内文件、Thumbs.db、desktop.ini 等）永远跳过；
                # 隐藏文件只受 skip_hidden 控制。
                if fsutil.is_system_name(name):
                    self._inc_skip(state, "excluded")
                    continue
                if skip_hidden and fsutil.is_hidden_name(name):
                    self._inc_skip(state, "hidden")
                    continue

                extension = fsutil.extension_of(name)
                compare_ext = extension.lower() if ignore_case else extension
                if include_ext:
                    if compare_ext not in include_ext:
                        self._inc_skip(state, "extension")
                        continue
                if exclude_ext and compare_ext in exclude_ext:
                    self._inc_skip(state, "extension")
                    continue

                # 注意：Windows 上 DirEntry.stat() 的 st_ino/st_nlink 不可靠，
                # 删除前重校验依赖 inode/dev/nlink，因此这里强制用 os.lstat。
                st = fsutil.lstat_or_none(entry.path)
                if st is None:
                    self._inc_skip(state, "permission")
                    self._inc_error(state)
                    continue
                if stat_module.S_ISLNK(st.st_mode):
                    self._inc_skip(state, "symlink")
                    continue
                if not stat_module.S_ISREG(st.st_mode):
                    self._inc_skip(state, "other")
                    continue
                size = int(st.st_size)
                if skip_empty and size == 0:
                    self._inc_skip(state, "empty")
                    continue
                if min_size and size < min_size:
                    self._inc_skip(state, "size")
                    continue
                if max_size and size > max_size:
                    self._inc_skip(state, "size")
                    continue

                inode = int(getattr(st, "st_ino", 0))
                dev = int(getattr(st, "st_dev", 0))
                nlink = int(getattr(st, "st_nlink", 1))
                if nlink > 1:
                    inode_key = (dev, inode)
                    if inode_key in seen_inodes:
                        self._inc_skip(state, "hardlink")
                        continue
                    seen_inodes[inode_key] = entry.path

                mtime_ns = int(getattr(st, "st_mtime_ns",
                                       int(st.st_mtime * 1000000000)))
                records.append(FileRecord(entry.path, size, mtime_ns, inode,
                                          dev, nlink))
            if processed % 512 == 0:
                self._set_progress(state, "enumerate", 0.05,
                                   u"正在枚举文件…")

    # ------------------------------------------------------------------
    # 阶段二：大小分组
    # ------------------------------------------------------------------
    def _size_stage(self, state, records):
        by_size = {}
        for record in records:
            by_size.setdefault(record.size, []).append(record)
        buckets = [items for items in by_size.values() if len(items) >= 2]
        candidate_files = sum(len(items) for items in buckets)
        with state.lock:
            state.stats["candidate_files"] = candidate_files
        self._set_progress(state, "size", 0.2,
                           u"按大小分组：%d 个候选文件" % candidate_files)
        del by_size
        return buckets

    # ------------------------------------------------------------------
    # 阶段三：快速指纹（哈希计算点：头尾采样）
    # ------------------------------------------------------------------
    def _sample_stage(self, state, buckets):
        algo = state.options.get("hash_algo") or "sha256"
        sample_kb = int(state.options.get("quick_sample_kb") or 64)
        total = sum(len(items) for items in buckets)
        done = 0
        result = []
        for bucket in buckets:
            self._check_control(state)
            groups = {}
            for record in bucket:
                self._check_control(state)
                try:
                    # 哈希计算点：快速采样指纹（非最终判定）
                    record.quick_hash = hashing.quick_sample_hash(
                        record.path, algo=algo, sample_kb=sample_kb)
                except (OSError, IOError):
                    self._inc_skip(state, "permission")
                    self._inc_error(state)
                    done += 1
                    continue
                groups.setdefault(record.quick_hash, []).append(record)
                done += 1
                if done % 64 == 0:
                    with state.lock:
                        state.current_path = record.path
                    fraction = 0.2 + 0.25 * (float(done) / max(1, total))
                    self._set_progress(state, "sample", fraction,
                                       u"正在计算快速指纹 %d/%d" % (done, total))
            for items in groups.values():
                if len(items) >= 2:
                    result.append(items)
        self._set_progress(state, "sample", 0.45,
                           u"快速指纹完成：%d 组候选" % len(result))
        return result

    # ------------------------------------------------------------------
    # 阶段四：全量哈希（哈希计算点）
    # ------------------------------------------------------------------
    def _hash_stage(self, state, buckets):
        algo = state.options.get("hash_algo") or "sha256"
        concurrency = max(1, min(8, int(state.options.get("concurrency") or 1)))
        rate_mbps = int(state.options.get("hash_rate_mbps") or 0)
        limiter = hashing.configure_global_rate(rate_mbps)
        total = sum(len(items) for items in buckets)
        if total == 0:
            self._set_progress(state, "hash", 0.85, u"没有需要全量哈希的文件")
            return []
        self._set_progress(state, "hash", 0.46,
                           u"正在计算哈希 0/%d" % total)
        report_every = max(1, min(64, total // 50))
        done = 0
        results = {}
        executor = self._executor_factory(concurrency)
        try:
            flat = []
            for bucket_index, bucket in enumerate(buckets):
                for record in bucket:
                    flat.append((bucket_index, record))
            batch_size = max(64, concurrency * 32)
            for start in range(0, len(flat), batch_size):
                self._check_control(state)
                batch = flat[start:start + batch_size]
                futures = []
                for bucket_index, record in batch:
                    futures.append((bucket_index, record,
                                    executor.submit(self._hash_one, state, record, limiter)))
                for bucket_index, record, future in futures:
                    try:
                        digest = future.result()
                    except hashing.HashCancelled:
                        if state.stop_event.is_set():
                            raise ScanStopped()
                        raise ScanStopped()
                    except (OSError, IOError):
                        self._inc_skip(state, "permission")
                        self._inc_error(state)
                    except Exception:
                        self._inc_skip(state, "other")
                        self._inc_error(state)
                    else:
                        record.hash = digest
                        results.setdefault(bucket_index, []).append(record)
                    done += 1
                    if done % report_every == 0 or done == total:
                        fraction = 0.45 + 0.40 * (float(done) / max(1, total))
                        self._set_progress(
                            state, "hash", fraction,
                            u"正在计算哈希 %d/%d" % (done, total))
                        with state.lock:
                            state.current_path = record.path
                del futures
            del flat
        finally:
            executor.shutdown(wait=False)

        hashed_buckets = []
        for bucket_index in sorted(results.keys()):
            groups = {}
            for record in results[bucket_index]:
                groups.setdefault(record.hash, []).append(record)
            for items in groups.values():
                if len(items) >= 2:
                    hashed_buckets.append(items)
        self._set_progress(state, "hash", 0.85,
                           u"全量哈希完成：%d 组重复" % len(hashed_buckets))
        return hashed_buckets

    def _hash_one(self, state, record, limiter):
        # 哈希计算点：全量分片哈希 + 全局限速 + 暂停/停止检查
        before = fsutil.file_identity(record.path)
        if before is None or before["size"] != record.size:
            raise IOError(u"文件在扫描过程中发生变化: %s" % record.path)
        digest, bytes_read = hashing.full_hash_file(
            record.path,
            algo=state.options.get("hash_algo") or "sha256",
            chunk_size=config.HASH_CHUNK_SIZE,
            should_stop=lambda: self._should_stop(state),
            limiter=limiter,
        )
        after = fsutil.file_identity(record.path)
        if (after is None or after["size"] != record.size
                or after["mtime_ns"] != before["mtime_ns"]):
            raise IOError(u"文件在哈希过程中发生变化: %s" % record.path)
        with state.lock:
            state.stats["bytes_hashed"] = state.stats.get("bytes_hashed", 0) + bytes_read
        return digest

    # ------------------------------------------------------------------
    # 阶段五：分组 + 保留策略
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 阶段五：相似度特征提取 + LSH 分组（仅 scan_mode=similar）
    # ------------------------------------------------------------------
    def _inc_similarity_skip(self, state, reason):
        if reason not in ("type", "size", "binary", "decode", "no_text", "error"):
            reason = "error"
        with state.lock:
            counters = state.stats["similarity_skipped"]
            counters[reason] = counters.get(reason, 0) + 1

    def _wait_worker(self, state):
        """worker 内检查暂停/停止：暂停时阻塞，停止时抛 ScanStopped。"""
        while not state.running_event.wait(0.1):
            if state.stop_event.is_set():
                raise ScanStopped()
        if state.stop_event.is_set():
            raise ScanStopped()

    def _extract_feature(self, state, record, kind, extension, options,
                         cache, limiter):
        self._wait_worker(state)
        key = similarity.cache_key(record.size, record.mtime_ns,
                                   record.inode, record.dev, kind)
        cached = cache.get(key) if cache is not None else None
        if cached and isinstance(cached.get("feature"), dict):
            return {"feature": dict(cached["feature"]), "cache_hit": True,
                    "degraded": cached.get("degraded")}
        feature = similarity.compute_feature(record.path, kind, extension,
                                             options, limiter)
        degraded = feature.get("degraded")
        if cache is not None:
            cache.put(key, {
                "feature": feature,
                "content_sha256": feature.get("content_sha256"),
                "kind": feature.get("kind"),
                "low_confidence": bool(feature.get("low_confidence")),
                "degraded": degraded,
            })
        return {"feature": feature, "cache_hit": False, "degraded": degraded}

    def _similarity_stage(self, state, records, exact_paths):
        options = state.options
        threshold = int(options.get("similarity_threshold") or 85)
        concurrency = max(1, min(4, int(options.get("similarity_concurrency") or 2)))
        max_mb = int(options.get("similarity_max_file_mb") or 32)
        cache = similarity.FeatureCache(
            os.path.join(self.store.data_dir, config.SIMILARITY_CACHE_FILENAME),
            max_entries=config.SIMILARITY_CACHE_MAX_ENTRIES,
            enabled=bool(options.get("similarity_cache", True)))
        limiter = hashing.get_global_limiter()

        candidates = []
        for record in records:
            if record.path in exact_paths:
                # 精准组内文件不参与相似组，避免重复
                continue
            extension = fsutil.extension_of(record.path)
            kind, reason, _detail = similarity.classify_extension(extension, options)
            if reason:
                self._inc_similarity_skip(state, reason)
                continue
            if record.size > max_mb * 1024 * 1024:
                self._inc_similarity_skip(state, "size")
                continue
            candidates.append((record, kind, extension))
        with state.lock:
            state.stats["similarity_candidates"] = len(candidates)
        if not candidates:
            cache.save()
            self._set_progress(state, "similarity", 0.97,
                               u"没有需要相似计算的文件")
            return []

        self._set_progress(state, "similarity", 0.85,
                           u"正在分析内容相似度 0/%d" % len(candidates))
        total = len(candidates)
        done = 0
        feature_entries = []
        features_by_sha = {}
        cache_hits = 0
        cache_misses = 0
        report_every = max(1, min(16, total // 30 or 1))
        executor = self._executor_factory(concurrency)
        try:
            batch_size = max(8, concurrency * 8)
            for start in range(0, total, batch_size):
                self._check_control(state)
                batch = candidates[start:start + batch_size]
                futures = []
                for record, kind, extension in batch:
                    futures.append((record, executor.submit(
                        self._extract_feature, state, record, kind, extension,
                        options, cache, limiter)))
                for record, future in futures:
                    try:
                        result = future.result()
                    except similarity.SimilaritySkip as exc:
                        self._inc_similarity_skip(state, exc.reason)
                    except ScanStopped:
                        raise
                    except hashing.HashCancelled:
                        raise ScanStopped()
                    except (OSError, IOError):
                        self._inc_similarity_skip(state, "error")
                        self._inc_error(state)
                    except Exception:
                        self._inc_similarity_skip(state, "error")
                        self._inc_error(state)
                    else:
                        feature = result["feature"]
                        if result.get("cache_hit"):
                            cache_hits += 1
                        else:
                            cache_misses += 1
                        if result.get("degraded"):
                            self._inc_similarity_skip(state, "decode")
                        content_sha = feature.get("content_sha256")
                        if content_sha and content_sha in features_by_sha:
                            # 同一扫描内相同内容复用特征
                            feature = features_by_sha[content_sha]
                        elif content_sha:
                            features_by_sha[content_sha] = feature
                        feature_entries.append({"record": record,
                                                "feature": feature})
                    done += 1
                    if done % report_every == 0 or done == total:
                        fraction = 0.85 + 0.12 * (float(done) / max(1, total))
                        self._set_progress(
                            state, "similarity", fraction,
                            u"正在分析内容相似度 %d/%d" % (done, total))
                        with state.lock:
                            state.current_path = record.path
                del futures
        finally:
            executor.shutdown(wait=False)
            cache.save()

        features = [entry["feature"] for entry in feature_entries]
        clusters, group_stats = similarity.build_groups(features, threshold)
        cluster_entries = []
        for cluster in clusters:
            indices = list(cluster["indices"])
            position_of = {index: position for position, index in enumerate(indices)}
            local_scores = {}
            for (first, second), score in cluster.get("scores", {}).items():
                if first in position_of and second in position_of:
                    local_scores[(position_of[first],
                                  position_of[second])] = score
            cluster_entries.append({
                "entries": [feature_entries[index] for index in indices],
                "scores": local_scores,
                "method": cluster.get("method") or "minhash",
                "similarity_score": int(cluster.get("similarity_score") or 0),
            })
        with state.lock:
            stats = state.stats
            stats["similarity_features"] = len(features)
            stats["similarity_cache_hits"] = stats.get("similarity_cache_hits", 0) + cache_hits
            stats["similarity_cache_misses"] = stats.get("similarity_cache_misses", 0) + cache_misses
            stats["similarity_pairs_checked"] = (
                stats.get("similarity_pairs_checked", 0)
                + int(group_stats.get("pairs_checked") or 0))
        self._set_progress(state, "similarity", 0.97,
                           u"相似度分析完成：%d 组" % len(cluster_entries))
        return cluster_entries

    # ------------------------------------------------------------------
    # 阶段六：分组 + 保留策略（精准组 + 相似组）
    # ------------------------------------------------------------------
    def _file_info(self, state, record, match_type, content_sha256=None):
        # 创建时间读取点：statx/btime 优先，ctime 仅显式允许时降级
        allow_ctime = bool(state.options.get("allow_ctime_fallback", False))
        created = self._time_reader(record.path, allow_ctime)
        modified_sec = record.mtime_ns // 1000000000
        file_info = {
            "path": record.path,
            "name": os.path.basename(record.path),
            "size": record.size,
            "hash": content_sha256 if match_type == "similar" else record.hash,
            "quick_hash": record.quick_hash,
            "created_at": timeutil.iso_local(created.get("created_ts")),
            "created_ts": created.get("created_ts"),
            "created_ns": int(created.get("created_ns") or 0),
            "created_source": created.get("created_source") or "unknown",
            "created_unsafe": bool(created.get("created_unsafe", True)),
            "modified_at": timeutil.iso_local(modified_sec),
            "mtime_ns": record.mtime_ns,
            "inode": record.inode,
            "dev": record.dev,
            "match_type": match_type,
            "similarity_score": 100 if match_type == "exact" else 0,
            "is_keep": False,
            "default_selected": False,
        }
        return file_info

    def _build_exact_groups(self, state, hashed_buckets):
        options = state.options
        algo = options.get("hash_algo") or "sha256"
        groups = []
        total = max(1, len(hashed_buckets))
        for index, bucket in enumerate(hashed_buckets):
            self._check_control(state)
            files = [self._file_info(state, record, "exact")
                     for record in bucket]
            (keeper, reason, needs_manual, suspect, time_fallback,
             keep_time_source) = choose_keeper(files, options)
            group_suspect = (suspect or time_fallback
                             or any(file_info["created_unsafe"]
                                    for file_info in files))
            for file_index, file_info in enumerate(files):
                file_info["is_keep"] = file_index == keeper
                file_info["default_selected"] = (
                    not file_info["is_keep"] and not needs_manual)
            files.sort(key=lambda item: (
                0 if item["is_keep"] else 1,
                -(item["created_ts"] or 0),
                item["path"],
            ))
            groups.append({
                "hash": bucket[0].hash,
                "algo": algo,
                "match_type": "exact",
                "similarity_score": 100,
                "size": bucket[0].size,
                "count": len(files),
                "wasted_bytes": bucket[0].size * (len(files) - 1),
                "keep_reason": reason,
                "needs_manual_keep": bool(needs_manual),
                "creation_time_suspect": bool(group_suspect),
                "time_fallback": bool(time_fallback),
                "keep_time_source": keep_time_source,
                "files": files,
            })
            if index % 50 == 0:
                self._set_progress(
                    state, "finalize", 0.87 + 0.04 * (float(index) / total),
                    u"正在整理结果 %d/%d" % (index, len(hashed_buckets)))
        groups.sort(key=lambda group: (-group["wasted_bytes"], -group["size"],
                                       group["hash"]))
        return groups

    def _build_similar_groups(self, state, similar_clusters):
        options = state.options
        groups = []
        for cluster in similar_clusters:
            self._check_control(state)
            members = cluster.get("entries") or []
            scores = cluster.get("scores") or {}
            files = []
            for entry in members:
                record = entry["record"]
                feature = entry["feature"]
                file_info = self._file_info(state, record, "similar",
                                            content_sha256=feature.get("content_sha256"))
                file_info["similarity_method"] = feature.get("method")
                file_info["low_confidence"] = bool(feature.get("low_confidence"))
                files.append(file_info)
            (keeper, reason, needs_manual, suspect, time_fallback,
             keep_time_source) = choose_keeper(files, options)
            group_suspect = (suspect or time_fallback
                             or any(file_info["created_unsafe"]
                                    for file_info in files))
            for position, file_info in enumerate(files):
                if position == keeper:
                    file_info["similarity_score"] = 100
                else:
                    score = scores.get((min(keeper, position),
                                        max(keeper, position)))
                    file_info["similarity_score"] = int(score or 0)
                file_info["is_keep"] = position == keeper
                # 相似组绝不默认勾选：必须人工逐条选择
                file_info["default_selected"] = False
            non_keeper_scores = [file_info["similarity_score"]
                                 for position, file_info in enumerate(files)
                                 if position != keeper]
            group_score = int(round(
                float(sum(non_keeper_scores)) / max(1, len(non_keeper_scores))))
            files.sort(key=lambda item: (
                0 if item["is_keep"] else 1,
                -int(item["similarity_score"] or 0),
                -(item["created_ts"] or 0),
                item["path"],
            ))
            low_confidence = any(bool(file_info["low_confidence"])
                                 for file_info in files)
            groups.append({
                "hash": "",
                "algo": "",
                "match_type": "similar",
                "similarity_score": group_score,
                "similarity_method": cluster.get("method") or "minhash",
                "low_confidence": low_confidence,
                "manual_review_required": True,
                "size": max(file_info["size"] for file_info in files),
                "count": len(files),
                "wasted_bytes": sum(file_info["size"] for file_info in files
                                    if not file_info["is_keep"]),
                "keep_reason": reason,
                "needs_manual_keep": bool(needs_manual),
                "creation_time_suspect": bool(group_suspect),
                "time_fallback": bool(time_fallback),
                "keep_time_source": keep_time_source,
                "files": files,
            })
        groups.sort(key=lambda group: (-int(group["similarity_score"] or 0),
                                       -group["wasted_bytes"],
                                       -group["count"],
                                       group["files"][0]["path"]))
        return groups

    def _finalize_stage(self, state, hashed_buckets, similar_clusters=None):
        similar_clusters = similar_clusters or []
        exact_groups = self._build_exact_groups(state, hashed_buckets)
        similar_groups = self._build_similar_groups(state, similar_clusters)

        file_counter = 0
        for group_index, group in enumerate(exact_groups):
            group["group_id"] = "g-%06d" % (group_index + 1)
            for file_info in group["files"]:
                file_counter += 1
                file_info["file_id"] = "f-%06d" % file_counter
                file_info["group_id"] = group["group_id"]
            group["keep_file_id"] = group["files"][0]["file_id"]

        similar_file_counter = 0
        for group_index, group in enumerate(similar_groups):
            group["group_id"] = "s-%06d" % (group_index + 1)
            for file_info in group["files"]:
                similar_file_counter += 1
                file_info["file_id"] = "sf-%06d" % similar_file_counter
                file_info["group_id"] = group["group_id"]
            group["keep_file_id"] = group["files"][0]["file_id"]

        groups = exact_groups + similar_groups
        exact_files = sum(group["count"] for group in exact_groups)
        similar_files = sum(group["count"] for group in similar_groups)
        exact_safe = [group for group in exact_groups
                      if not group["needs_manual_keep"]]
        reclaimable = sum(group["wasted_bytes"] for group in exact_safe)
        selectable_files = sum(group["count"] - 1 for group in exact_safe)
        similar_reclaimable = sum(group["wasted_bytes"]
                                  for group in similar_groups)
        needs_manual_groups = sum(1 for group in groups
                                  if group["needs_manual_keep"])
        fallback_groups = [group for group in groups
                           if group.get("time_fallback")]
        mtime_fallback_groups = len(fallback_groups)
        mtime_fallback_files = sum(group["count"] for group in fallback_groups)
        duplicate_files = exact_files + similar_files
        creation_unsafe = any(group["creation_time_suspect"] for group in groups)
        with state.lock:
            stats = state.stats
            stats["exact_groups"] = len(exact_groups)
            stats["exact_files"] = exact_files
            stats["similar_groups"] = len(similar_groups)
            stats["similar_files"] = similar_files
            stats["similar_reclaimable_bytes"] = similar_reclaimable
            stats["groups"] = len(groups)
            stats["duplicate_groups"] = len(groups)
            stats["duplicate_files"] = duplicate_files
            stats["reclaimable_bytes"] = reclaimable
            stats["selectable_duplicate_files"] = selectable_files
            stats["selectable_reclaimable_bytes"] = reclaimable
            stats["mtime_fallback_groups"] = mtime_fallback_groups
            stats["mtime_fallback_files"] = mtime_fallback_files
            state.needs_manual_keep_groups = needs_manual_groups
            state.creation_time_unsafe = creation_unsafe
        self._set_progress(state, "finalize", 0.995, u"正在保存扫描结果")

        try:
            self.store.write_scan_groups(state.scan_id, groups)
        except Exception as exc:
            raise ApiError("INTERNAL_ERROR", u"保存扫描结果失败: %s" % exc)
        self._set_progress(state, "finalize", 1.0, u"扫描结果已保存")
