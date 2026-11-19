# 发布说明 · v1.1.0-0062

> 主题：转换批量提交不再被 200 上限拦下（前端自动分批 + 活动任务上限 2000）
> 升级方式：套件中心手动安装覆盖升级；数据目录、包 ID 与 SQLite schema（v11）不变。
> 说明：v0061（源后缀多选 / 同名覆盖进回收站 / 默认删源）与 v0062 尚未单独发版，本说明同时覆盖两者增量。

## 1. 单次提交超 200 个文件时自动分批（v0062）

- 后端单次提交上限仍是 **200 个文件**（`CONVERT_BATCH_MAX_ITEMS`，防止单请求过大），
  但转换页现在会**按 200 个一批自动连续提交**，整目录（目录枚举上限 2000）可一次提完。
- 「开始转换」时按钮下方显示进度：`正在分批提交…（3/5 批，已提交 600/1000 个）`。
- 选中的文件数超过一批时，提交提示条会显示 `已选 850 个文件 … · 自动分 5 批提交`。
- 每批成功后只把**真正入队的文件**从「已选文件」里移除；个别文件被拒（不存在/非普通文件等）
  会保留在列表里，并在提交区列出前几个被拒原因，可修正后再提交。
- 某一批失败时：已成功的批次保留，提示「已提交 N 个，之后失败：原因」，剩余文件仍在列表里。
- 批次大小来自 `GET /api/convert/capabilities` 的 `limits.batch_max_items`，前端不写死。
- 活动任务上限 `convert_job_limit` 默认由 **500 提到 2000**（可配 10–5000，与枚举上限对齐），
  否则整目录提交会在中途撞上「活动转换任务已达上限」。
- 旧配置里仍是默认值 `500` 的，升级后首次启动一次性迁到 `2000`（写审计 `settings_migrate`）；
  用户自己设过的其它值不动。迁移标记存在 DB `schema_meta`，不会重复执行。

## 2. v0061 增量（同版本一并发布）

- **源文件后缀可多选**：视频/音频分组 + 全选/清空/仅视频/仅音频；只枚举勾选的后缀，
  勾选结果存 `convert_source_extensions`（空 = 全部）；`extensions` 严格校验，未知后缀/空数组返回 400。
- **同名目标覆盖进回收站**：默认策略 `trash`，不再生成 `名称 (2).mp4`；旧产物在产物校验通过后、
  原子改名之前移入 `#duplicate-cleaner-trash` 并登记回收站清单，可在「扫描 → 删除与回收站」恢复；
  改名失败会还原，移入失败降级为加序号；源=目标（同目录同格式）仍强制加序号。
- **默认删源 + 失败清理**：`convert_delete_source` 默认 `true`、页面默认勾选、不再二次确认；
  失败/取消/超时一律清 `.dcpart`；`convert_keep_source` / `convert_keep_temp` 降级为兼容键。

## 3. 兼容性

| 项 | 变化 |
|---|---|
| SQLite schema | **v11 不变**，无迁移 |
| 设置 | `convert_job_limit` 默认 500 → 2000（旧默认值一次性迁移）；v0061 的 `convert_delete_source`（true）、`convert_source_extensions`（[]）；`convert_keep_source` / `convert_keep_temp` 为兼容键；`convert_overwrite` 默认 `trash` |
| API | `GET /api/convert/capabilities` 新增 `limits`（`batch_max_items` / `enumerate_max_items` / `job_limit` / `job_limit_max`）与 v0061 的 `source_extensions`；`POST /api/convert` 仍限制单请求 200 个文件；返回的 `rejected` 列表可用于判断哪些文件未入队 |
| 前端 | 转换页自动分批提交 + 分批进度；批次大小取自 capabilities |
| 数据安全 | 「覆盖 + 删源」默认组合不可逆，被覆盖的旧产物在套件回收站可恢复；回收站不会自动清理 |

## 4. 安装

1. 下载附件 `DuplicateCleaner-1.1.0-0062.spk`；
2. DSM → 套件中心 → 手动安装 → 覆盖升级（数据保留）；
3. 打开「转换」页确认：后缀多选区已展开、同名文件默认「覆盖旧产物（移入回收站）」、
   「转换成功后删除源文件」默认勾选；选 300 个文件点「开始转换」应显示分 2 批提交。

## 5. 校验

- 文件大小 / SHA256 / package.tgz MD5：见 Release 附件与 `dist/*.sha256`。

## 6. 测试

- 后端单测：`tests/test_v0061_convert_defaults.py` 增加 `convert_job_limit` 迁移用例，
  `tests/test_v0056_convert.py` 断言 `capabilities.limits`；
- 静态：新增 `FE-CONVERT-BATCH`、`BE-CONVERT-LIMITS`；
- 端到端：新增 `CV-20`（分批上限 200 / 活动任务上限 2000 / 枚举上限 2000）。

## 7. 文档

- 契约：[`docs/CONVERT_CONTRACT.md`](CONVERT_CONTRACT.md)（§3 API、§4 队列、§6 设置键、§9 前端）
- QA 报告：[`qa/REPORT.md`](../qa/REPORT.md)
