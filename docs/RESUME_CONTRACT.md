# 扫描暂停/继续与升级 drain 契约（v1.5）

目标：升级前暂停扫描、持久化断点；升级后手动“继续扫描”。不自动续跑；删除任务不续；字幕任务本版仍按 SERVICE_RESTART（后续再做 chunk 级续跑）。

## 1. 状态

- paused：可继续（有 checkpoint snapshot）。
- interrupted：无可用 checkpoint，不能继续，只能重新扫描。
- 现有 pause/resume API 保持；新增磁盘恢复。

## 2. Checkpoint 快照

scan_checkpoints payload 增加：
{
  "stage": "size|sample|hash|similarity|video|finalize",
  "progress": 0.2,
  "records": [{"path":..., "size":..., "mtime_ns":..., "inode":..., "dev":..., "nlink":..., "quick_hash":..., "hash":...}],
  "stats": {...}
}

- 仅在暂停时写入 records 快照；普通阶段 checkpoint 仍可轻量。
- 暂停到阶段边界或 hash 循环的安全点；不要求 mid-file 精确续传。
- 升级 drain 超时则标 interrupted，不强行生成假断点。

## 3. 暂停/继续

- 新异常 ScanPaused；_check_control 在 pause_event/drain 文件时抛出。
- _run 捕获后：
  - status=paused；
  - message=“扫描已暂停，升级后可继续”；
  - 写 checkpoint（含 records）；
  - 保留 hashed/feature/video 缓存。
- resume(scan_id)：
  - 同进程 paused：恢复原 thread（现有）；
  - 进程重启后：从 Store 加载 scan meta + checkpoint，重建 ScanState/FileRecord，按 stage 继续。
- 恢复语义：
  - size：从 records 开始 size→sample→hash...
  - sample：从 records/quick_hash 开始 sample→...
  - hash：跳过已有 quick_hash/hash 的记录，继续 hash；精确组由 records hash 重建。
  - similarity/video/finalize：records + hash 重建；相似/视频缓存可复用；必要时重跑该阶段。
- 文件变化：恢复前重新 stat；若 path/size/mtime/inode 变化，丢弃该记录或标记重扫；不得沿用旧 hash。

## 4. 升级 drain

- preupgrade：
  1. 创建 <var>/upgrade.drain；
  2. 等待 <var>/upgrade.done，最长 30 秒；
  3. 超时退出 0，让 DSM 停止服务（未暂停的任务会变 interrupted）。
- 后端监控 drain 文件：
  - 对活动扫描设置 pause_event；
  - 等扫描到安全点写 checkpoint；
  - 没有活动任务时写 upgrade.done；
  - 不阻止删除任务（删除不应自动续）。
- preupgrade 不修改 package ID/数据目录/ACL。

## 5. UI

- paused：显示“已暂停（升级前/用户暂停）”，显示“继续扫描”按钮。
- interrupted：显示“已中断，需重新扫描”，无继续按钮。
- 继续前提示文件可能变化；调用现有 /api/scans/{id}/resume。
- 不自动继续。

## 6. 测试

- 暂停时 checkpoint 含 records。
- 新 ScanManager 从 DB 恢复 paused 扫描，跳过已完成 hash，继续到 completed。
- 阶段恢复（size/sample/hash）。
- 文件变化使记录失效。
- drain 文件触发暂停并写 upgrade.done；无任务立即 done；超时 interrupted。
- 现有 236 测试绿。
