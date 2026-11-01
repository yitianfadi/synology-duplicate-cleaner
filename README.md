# 重复文件清理 · DuplicateCleaner（群晖 DSM 7 SPK）

一款为 Synology DSM 7.x 编写的重复文件清理套件：按**文件内容哈希**识别重复文件，默认只保留**创建时间最晚**的一份；先出报告，人工核对、两次确认后才删除，默认移入群晖回收站，并持久化操作日志。

> ## ⚠️ 风险提示
> **删除文件存在数据丢失风险，重要数据建议先备份；本工具仅基于哈希判定重复，无法保证业务文件适用性，请核对扫描结果再操作。**
>
> 本工具以 root 权限运行以读取共享文件夹并写入 `#recycle`。请只在可信局域网使用，不要将 8787 端口暴露到公网。删除前请务必确认保留项与副本清单。

## 功能

- 📁 文件夹选择器：共享文件夹/子目录多选，排除指定目录
- 🔍 三级哈希判重：大小 → 头尾采样指纹 → 全量 SHA-256/MD5（文件名不参与）
- 🕒 保留策略：最晚创建（默认）/ 最早创建 / 指定路径；读取 `statx` btime，拒绝用 `st_ctime` 冒充
- 📊 分组报告：路径、大小、完整哈希、创建时间、修改时间；保留项高亮；可预览、勾选、全选、导出 CSV
- 🛡️ 强安全删除：报告 → 手动确认 → 二次弹窗 → 服务端逐文件重校验 → 默认进回收站 → 审计日志
- ⏯️ 扫描可暂停/继续/停止；并发 1–8、哈希限速、低优先级（nice/ionice）
- 🚫 跳过符号链接、空文件、隐藏/系统文件、自定义后缀；避开系统目录
- 📜 扫描历史与操作日志持久化，可下载
- ⚙️ 可选：大小区间、大小写后缀、保留指定路径、回收站/永久删除

## 快速开始

### 1. 构建 SPK（开发机）

```bash
python build/build_spk.py
# 生成 dist/DuplicateCleaner-1.0.0-0001.spk
```

构建机需要 Python 3.8+；重新生成图标需要 Pillow（仓库已带 PNG，可 `--no-icons`）。

### 2. 安装到 DSM

1. DSM 套件中心先安装 **Python3**（3.8+）。
2. 套件中心 → 手动安装 → 选择 `dist/DuplicateCleaner-1.0.0-0001.spk`。
3. 启动套件；从 DSM 主菜单打开“重复文件清理”，或访问 `http://<NAS>:8787/`。
4. 使用 administrators 组账号登录。

详细步骤、权限、TLS、排错见 [打包与部署](docs/SPK_PACKAGING.md)。

### 3. 本地开发联调（Windows/Linux/macOS）

不需要 DSM 也可以跑通完整流程（开发模式，仅用于本机测试）：

```bash
# PowerShell
$env:DUP_CLEANER_DEV="1"
$env:DUP_DEV_ROOTS="D:\tmp\dup-test;D:\tmp\dup-test2"
$env:DUP_DEV_PASSWORD="admin"
python src/backend/app.py --host 127.0.0.1 --port 8787 --data-dir ./data --web-dir ./src/web
```

浏览器打开 `http://127.0.0.1:8787/`，账号 `admin` / 密码 `admin`（或 `DUP_DEV_PASSWORD`）。
开发模式只在环境变量显式开启时生效，SPK 启动脚本会主动 `unset` 相关变量。

### 4. 测试

```bash
cd src/backend
python -m unittest discover -s tests -v
node --check ../web/app.js
```

验收脚本与报告见 `qa/`（集成测试、需求逐项对照、SPK 结构检查）。

## 文档

- [整体方案架构](docs/ARCHITECTURE.md)：组件、扫描/哈希/时间/删除设计、群晖 API 调用点、性能与限制
- [前端页面原型说明](docs/UI_PROTOTYPE.md)：页面线框、文件夹选择器、结果页、两次确认流程、状态与验收
- [打包、部署与运维](docs/SPK_PACKAGING.md)：依赖、构建、安装、权限、TLS、日志、升级/卸载、排错
- [前后端实现契约](docs/CONTRACT.md)：接口、字段、错误码、安全规则（开发/联调用）
- [QA 验收报告](qa/REPORT.md)：需求逐项对照与测试证据

## 仓库结构

```
src/backend/        Python 3.8+ 标准库后端（HTTP API、扫描、哈希、statx、删除、审计）
src/web/            纯静态 SPA（无构建步骤、无 CDN）
spk/                DSM 套件外壳（INFO、scripts、conf、ui、port_conf、图标）
build/              SPK 构建脚本与图标生成
docs/               方案、原型、打包部署、接口契约
qa/                 集成验收脚本与报告
dist/               构建产物（.spk 与 SHA256）
```

## 安全设计摘要

1. **绝不自动删除**：扫描完成只生成报告；删除需要用户选文件 → 生成计划 → 第一次清单确认 → 第二次最终确认。
2. **服务端二次校验**：删除前重新检查文件存在、大小/mtime/inode/dev 未变、保留文件仍在、候选与保留文件全量哈希一致。
3. **路径白名单/黑名单**：只允许共享文件夹子树；禁止系统目录、`@*`、`#recycle`、`#snapshot`、`@eaDir` 等。
4. **不跟随符号链接**；硬链接按 inode 去重，避免“删了不释放空间”。
5. **默认回收站**：移动到 `<共享文件夹>/#recycle/@duplicate-cleaner/<时间>/<相对路径>`；永久删除需额外输入“永久删除”。
6. **认证与审计**：DSM 管理员登录（`SYNO.API.Auth`），HttpOnly Cookie + CSRF；审计日志持久化。

## 已知限制

- DSM 7 不预装 Python 3，必须安装 Python3 套件。
- 创建时间依赖 `statx` btime；不支持的文件系统上默认不自动选保留项，需人工指定。
- 单次扫描默认最多 500,000 个文件；超大库建议分目录扫描。
- 默认 HTTP 仅适合可信局域网；公网/远程请使用 VPN 或 DSM 反向代理 + TLS。
- 加密共享文件夹需先在 DSM 解锁；套件不处理加密解锁。
- 本工具不判断业务语义，只判断内容哈希；请自行确认副本可删。

## License

MIT（见 `spk/LICENSE`）；数据安全声明同样包含在 LICENSE 中。
