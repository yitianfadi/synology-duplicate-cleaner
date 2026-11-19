# 发布说明 · v1.1.0-0061

> 主题：转换功能三处默认行为调整（源后缀多选 / 同名覆盖进回收站 / 默认删源 + 失败清理）
> 升级方式：套件中心手动安装覆盖升级；数据目录、包 ID 与 SQLite schema（v11）不变。

## 1. 源文件后缀可多选（只扫描勾选的后缀）

- 转换页「源文件后缀」支持**同时勾选多个不同后缀**，视频与音频分组显示，带「全选 / 清空 / 仅视频 / 仅音频」。
- 「列出文件」只枚举勾选的后缀；未勾选任何后缀时按钮会提示，不会退回全量扫描。
- 勾选结果自动保存到设置键 `convert_source_extensions`（空数组 = 全部支持后缀），下次打开页面沿用。
- 后端 `POST /api/convert/enumerate` 的 `extensions` 现在严格校验：只能是登记过的音视频后缀，
  传空数组或不支持的后缀返回 400（避免用它扫任意文件）。
- 手动粘贴路径不受后缀筛选限制（用户显式指定，仍按原安全校验执行）。

## 2. 目标同名时覆盖，旧产物移入套件回收站

- 新增同名策略 `trash` 并设为**默认**：目标文件已存在时直接覆盖，**不再生成 `名称 (2).mp4`**。
- 旧的同名产物在写新文件之前被移入 `<共享文件夹>/#duplicate-cleaner-trash/<时间戳>/…`，
  并登记回收站清单，可在「扫描 → 删除与回收站」里恢复或清理。
- 顺序保证不丢数据：FFmpeg 先写 `.dcpart` → 产物校验通过 → 移走旧文件 → 原子改名；
  若最终改名失败，旧文件会被还原回原位。
- 移入回收站失败（权限/跨卷等）时自动降级为加序号 `(2)`，任务继续，不报错。
- **源文件与产物同路径（同目录同格式，如 mp4 → mp4 仅换封装）仍然强制加序号**，绝不原地覆盖源文件。
- 下拉里仍保留 `rename`（加序号）/ `skip`（跳过）/ `overwrite`（直接覆盖，旧的不可恢复）供手动选择。
- 升级迁移：磁盘上仍是旧默认值 `convert_overwrite: rename` 时，首次启动一次性改为 `trash` 并写审计；
  迁移后再手动选回 `rename` 不会被改写。

## 3. 默认转换成功后删除源文件，失败清理临时文件

- 新增设置键 `convert_delete_source`，默认 `true`；转换页「转换成功后删除源文件」默认勾选。
- **不再弹二次确认**，提交即按页面勾选执行；取消勾选则保留源文件。
- 删除时机不变：产物校验通过、原子改名完成之后才删除；`skip` 掉的任务不删源；
  转换失败不会删除源文件。
- 失败 / 取消 / 超时一律删除 `.dcpart`；`convert_keep_temp` 降级为兼容键，不再生效。
- `convert_keep_source` 同样降级为兼容键（接受提交、不再生效）；旧客户端只传 `keep_source=false`
  仍会按「删除源文件」处理。
- 接口安全保留：请求里**显式**要求 `delete_source: true` 时仍必须带 `confirm: "DELETE SOURCE"`
  （前端会自动带上，对用户无感）。

## 4. 兼容性

| 项 | 变化 |
|---|---|
| SQLite schema | **v11 不变**，无迁移 |
| 设置 | 新增 `convert_delete_source`、`convert_source_extensions`；`convert_keep_source` / `convert_keep_temp` 降级为兼容键；`convert_overwrite` 默认值改为 `trash`（旧值 `rename` 一次性迁移） |
| API | `/api/convert/capabilities` 新增 `source_extensions`（video/audio/all/selected）与 `defaults.delete_source`；`/api/convert/enumerate` 的 `extensions` 严格校验；任务快照新增 `overwritten_path` / `trashed_count` |
| 前端 | 转换页新增后缀多选；移除「保留源文件」复选框；删源默认勾选；「同名文件」下拉新增「覆盖旧产物（移入回收站）」 |
| 数据安全 | 「覆盖 + 删源」组合不可逆，但被覆盖的旧产物在套件回收站可恢复；回收站不会自动清理，需手动清理 |

## 5. 明确不做

- 不做转换任务的断点续转（FFmpeg 无此能力）。
- 不自动清理回收站（保留手动清理，避免误删可恢复数据）。
- 不改变 `convert_overwrite` 的三种旧策略语义。

## 6. 安装

1. 下载附件 `DuplicateCleaner-1.1.0-0061.spk`；
2. DSM → 套件中心 → 手动安装 → 覆盖升级（数据保留）；
3. 打开「转换」页确认：后缀多选区已展开、同名文件默认「覆盖旧产物（移入回收站）」、
   「转换成功后删除源文件」默认勾选。

## 7. 校验

- 文件大小 / SHA256 / package.tgz MD5：见 Release 附件与 `dist/*.sha256`。

## 8. 测试

- 后端单测：新增 `tests/test_v0061_convert_defaults.py`（迁移 + 后缀校验 9 例），
  `tests/test_v0056_convert.py` 新增覆盖进回收站 / 默认删源 / 失败清理 / 多后缀枚举等用例；
- 静态：`BE-CONVERT-TRASH-OVERWRITE`、`BE-CONVERT-DELETE-DEFAULT`、`BE-CONVERT-EXT-FILTER`、
  `FE-CONVERT-EXTS`、`FE-CONVERT-DELETE-DEFAULT`；
- 端到端：`CV-15`（多后缀枚举）、`CV-16/17`（非法/空后缀拒绝）、`CV-18`（默认值元数据）、
  `CV-19`（真实转码验证「覆盖进回收站」）。

## 9. 文档

- 契约：[`docs/CONVERT_CONTRACT.md`](CONVERT_CONTRACT.md)（§2 同名策略、§3 API、§6 设置键、§8 落盘与清理）
- QA 报告：[`qa/REPORT.md`](../qa/REPORT.md)
