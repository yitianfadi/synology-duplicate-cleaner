# 发布说明 1.1.0-0055（修复设置键回归）

> **状态：已打包（未发布）。** 产物 `dist/DuplicateCleaner-1.1.0-0055.spk`；未推送代码、未发 GitHub Release、未装 NAS。
> 构建命令：`python build/build_spk.py`；静态检查 `python qa/static_checks.py`（含 SPK 结构与源码同步校验）。

- 版本：`1.1.0-0055`
- 日期：2026-11-15
- 基线：`1.1.0-0054`（已打包未发布）
- 重点：修复 v0054 的**设置键白名单漏项**导致「保存设置」整单失败，并把这类漂移做成三层回归。

## 1. 修复

### 1.1 `未知的设置键: download_subscribe_enabled`

- **现象**：在设置页点「保存设置」返回 400 `INVALID_ARGUMENT / 未知的设置键: download_subscribe_enabled`；订阅总开关始终显示为关。
- **原因**：v0054 新增订阅下载时，只在前端 `DEFAULT_SETTINGS`/表单、`download_subs.py` 消费者与文档里加了 `download_subscribe_enabled`，**漏加后端 `DEFAULT_SETTINGS` 与 `_normalize_value` 校验分支**。前端 `readSettingsForm()` 每次保存都会整体提交全部设置键，`validate_patch` 因此整单拒绝。
- **修复**：后端补上默认值（`false`）与布尔校验分支；`GET /api/settings` 现在会回显该键，设置页开关可正常存取。

### 1.2 同类问题排查结果

按「前端提交键 ↔ 后端白名单」「后端读取键 ↔ 默认值」两个方向全量比对，除上述一例外**没有其他漏项**（136 个默认键、139 个前端提交键、3 个 `clear_*` 别名全部对得上）；也没有「后端有默认值但前端没有任何入口」的死设置。

## 2. 防回归

| 层 | 新增 | 说明 |
|---|---|---|
| 静态 | `BE-SETTINGS-KEYS` | 前端 `settings.<key> = ...` 提交的键必须 ⊆ 后端白名单 ∪ `clear_*` 别名 |
| 静态 | `BE-SETTINGS-NORMALIZE` | 每个默认键都必须能在 `_normalize_value` 里找到校验分支 |
| 单元 | `tests/test_v0054_settings_keys.py`（9 例） | 默认值全部可校验、前端键都被后端认识、开关类型严格、持久化与 `public_settings` 回显 |
| e2e | `SET-08/09/10` | 开关可存可回显、非布尔被拒、**GET 到的完整设置可原样回传**（模拟前端整体保存） |

两个静态检查做过反向验证：删掉默认键 → `BE-SETTINGS-KEYS` FAIL；只删校验分支 → `BE-SETTINGS-NORMALIZE` FAIL。

## 3. 兼容性

| 项 | 变化 |
|---|---|
| SQLite schema | v9 不变 |
| 设置 | 新增 `download_subscribe_enabled`（默认 `false`，v0054 已文档化） |
| API | 无新增端点；`/api/settings` 的返回/接受键增加上述一项 |
| 数据迁移 | 从 v0054 升级时若已写入 `settings.json`，该键缺失会按默认 `false` 补齐；无破坏性迁移 |
| 前端 | `?v=` 更新为 `1.1.0-0055` |

## 4. 校验值

出包后填写（`dist/DuplicateCleaner-1.1.0-0055.spk`）：

```
SHA256: 待填
package.tgz MD5: 待填（= spk/INFO checksum）
大小: 待填
```

## 5. QA 结果（源码级）

- 单元测试：757 例通过（5 跳过，含新增 9 例）。
- e2e：230 通过 / 0 失败 / 1 未验证（Windows 无 symlink 权限，与历史一致），较 v0054 增加 `SET-08/09/10`。
- 静态：`BE-SETTINGS-KEYS`/`BE-SETTINGS-NORMALIZE` 新增并通过，其余项与 v0054 基线一致；数字见 [`qa/REPORT.md`](../qa/REPORT.md)。

## 6. 回退

- 回退到 `DuplicateCleaner-1.1.0-0054.spk`：设置文件兼容（多余键会被忽略），下载产物与订阅状态不受影响。
- 若只想绕开本 bug 而不升级：在设置页「下载」分组里保持订阅开关为**关**并避免保存设置；或直接手工 PUT 去掉该键的补丁。
