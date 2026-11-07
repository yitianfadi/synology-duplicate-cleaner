# 打包、部署与运维（DSM 7 SPK）

> 套件名：`DuplicateCleaner` · 版本：`1.1.0-0039` · 架构：`noarch`（Python 代码；内置 x86_64/aarch64 FFmpeg 二进制，运行时按 `uname -m` 选择）
> 产物：`dist/DuplicateCleaner-1.1.0-0039.spk` + SHA256 校验文件

## 1. 运行依赖与前置条件

| 依赖 | 要求 | 说明 |
|---|---|---|
| DSM | 7.0-40000 及以上 | `INFO` 的 `os_min_ver`；脚本会再次检查主版本为 7 |
| Python | Python 3.8+ | DSM 7 不预装 Python 3；先在 **套件中心** 安装官方 **Python3**（或兼容的 Python 3.8+ 套件）。`preinst` 会检测并给出中文错误 |
| 权限 | DSM administrators 组 + 共享文件夹 ACL | Web 界面登录需要管理员；后端以系统内部用户 `duplicatecleaner` 运行，需在 DSM 中给该用户授予目标共享文件夹读取/写入权限 |
| 文件系统 | Btrfs / EXT4 | 创建时间依赖 `statx` 的 btime；两类文件系统在 DSM 7 内核下均支持（EXT4 需 inode 256） |
| 浏览器 | 支持 ES2019 | DSM 7 自带 Chrome/Firefox；无 CDN 依赖 |
| 可选 Pillow | 非必需 | 相似模式的 JPEG/WebP 感知哈希优先使用 Pillow；未安装时自动降级为字节特征并标记低置信。PNG 使用标准库解码，无需 Pillow |
| FFmpeg | SPK 内置静态二进制 | `x86_64` / `aarch64` 两套已打包进 `bin/ffmpeg-*`；启动脚本按 `uname -m` 设置 `DUP_FFMPEG_PATH`；无需用户安装 |
| 构建机 | Python 3.8+ | `build/build_spk.py` 跨平台；首次构建先运行 `build/fetch_ffmpeg.py`；生成图标需要 Pillow（可选，仓库已含 PNG） |

> 安装 Python3 的路径不必手动指定：启动脚本会依次探测
> `/usr/local/bin/python3`、`/usr/bin/python3`、`/var/packages/Python3/target/...`、SynoCommunity 的 `python38/python311` 路径。
> 若你的 DSM 上 Python 位于其他路径，可修改 `spk/scripts/preinst` 与 `start-stop-status` 的探测列表后重新打包。

## 2. 仓库目录到 SPK 的映射

```
仓库                                     SPK / 安装后
src/backend/app.py                ─┐
src/backend/dupcleaner/**          ├─ package.tgz/app/**  → /var/packages/DuplicateCleaner/target/app/
src/web/**                        ─┘   package.tgz/web/** → .../target/web/
spk/ui/**                          → package.tgz/ui/**   → .../target/ui/（dsmuidir=ui，桌面入口）
spk/port_conf/DuplicateCleaner.sc  → package.tgz/port_conf/** → .../target/port_conf/（端口注册）
spk/INFO                           → SPK 根 INFO（构建时注入 package.tgz 的 MD5 checksum）
spk/scripts/**                     → SPK 根 scripts/（preinst/postinst/postupgrade/...）
spk/conf/privilege, resource       → SPK 根 conf/
spk/PACKAGE_ICON*.PNG, LICENSE     → SPK 根
```

**重要**：`ui/` 在 `package.tgz` 内，不在外层 SPK 根；`dsmuidir="ui"` 指向 target/ui。
DSM 会把它链接到 `/usr/syno/synoman/webman/3rdparty/DuplicateCleaner/`，桌面图标随之出现。

## 3. 构建

### 3.1 本机（Windows / Linux / macOS）

```bash
cd D:\code\zhouyi\qunhui\synology-duplicate-cleaner
python build/build_spk.py
# 或：python3 build/build_spk.py
```

产物：

```
dist/DuplicateCleaner-1.1.0-0039.spk
dist/DuplicateCleaner-1.1.0-0039.spk.sha256
```

构建脚本做了：

1. 检查 `src/backend/app.py`、`src/backend/dupcleaner/__init__.py`、`src/web/index.html`、`src/web/app.js`、`spk/INFO` 存在；
2. 生成/复用图标；
3. 复制后端与前端到临时 target，排除 `__pycache__`、tests、pyc；
4. 替换 `spk/ui/index.html` 的 `__DUP_BACKEND_SCHEME__` 占位符（默认 `http`，TLS 时用 `--scheme https`）；
5. 生成 `package.tgz`（gzip，固定 mtime，便于复现）；
6. 计算 package.tgz 的 MD5 写入 `INFO` 的 `checksum`；
7. 生成外层未压缩 tar（`.spk`），并写 SHA256；
8. 自检必须存在的文件。

### 3.2 在 Linux 构建机上用 shell 的等价步骤

如果不使用 Python 构建脚本，可手工执行（仅示意，推荐用脚本）：

```bash
set -e
rm -rf build/stage && mkdir -p build/stage/package build/stage/spk
# 1) 组装 target
mkdir -p build/stage/package/app build/stage/package/web build/stage/package/ui build/stage/package/port_conf
cp -a src/backend/. build/stage/package/app/
rm -rf build/stage/package/app/tests build/stage/package/app/**/__pycache__
cp -a src/web/. build/stage/package/web/
cp -a spk/ui/. build/stage/package/ui/
cp -a spk/port_conf/. build/stage/package/port_conf/
echo "1.1.0-0039" > build/stage/package/VERSION
# 2) package.tgz
tar -czf build/stage/package.tgz -C build/stage/package .
# 3) 外层
cp -a spk/INFO spk/PACKAGE_ICON.PNG spk/PACKAGE_ICON_256.PNG spk/LICENSE build/stage/spk/
cp -a spk/conf spk/scripts build/stage/spk/
cp build/stage/package.tgz build/stage/spk/
MD5=$(md5sum build/stage/package.tgz | cut -d' ' -f1)
sed -i '/^checksum=/d' build/stage/spk/INFO
echo "checksum=\"$MD5\"" >> build/stage/spk/INFO
chmod 755 build/stage/spk/scripts/*
# 4) 未压缩外层 tar
tar -cf dist/DuplicateCleaner-1.1.0-0039.spk -C build/stage/spk .
```

> 注意：手工构建时 `ui/index.html` 里的 `__DUP_BACKEND_SCHEME__` 需要替换为 `http` 或 `https`；`ui` 必须在 package.tgz 里。

### 3.3 构建后自检

```bash
tar -tf dist/DuplicateCleaner-1.1.0-0039.spk
# 应包含 INFO、package.tgz、PACKAGE_ICON.PNG、conf/*、scripts/*
mkdir -p /tmp/dupcheck && tar -xf dist/DuplicateCleaner-1.1.0-0039.spk -C /tmp/dupcheck
tar -tzf /tmp/dupcheck/package.tgz | head -50
grep -E '^(package|version|os_min_ver|dsmuidir|dsmappname|adminport)=' /tmp/dupcheck/INFO
python -m json.tool /tmp/dupcheck/conf/privilege
python -m json.tool /tmp/dupcheck/conf/resource
```

Windows 自带 `tar.exe`（bsdtar）也可执行 `tar -tf`、`tar -xf`。

## 4. 安装到 DSM

### 4.1 上传安装

1. DSM → **套件中心** → **手动安装**（右上角“手动安装”按钮）。
2. 选择 `DuplicateCleaner-1.1.0-0039.spk`。
3. 阅读风险提示/许可证；如 DSM 提示“未知发布者”，确认信任来源后继续。
   - DSM 7.2+ 若禁止未签名第三方套件，需在 **套件中心 → 设置 → 常规 → 信任层级** 调整为允许（或对套件签名，见 §9）。
4. 安装前确认已安装 **Python3**；未安装会被 `preinst` 拒绝并提示。
5. 安装完成后，套件中心显示“重复文件清理与字幕”。
6. **授权共享文件夹（首次必做）**：控制面板 → 共享文件夹 → 编辑 → 权限 → 系统内部用户 → `duplicatecleaner` → 读取（扫描）或读取/写入（删除）。详见 §6。
7. 启动套件，打开方式二选一：
   - DSM 主菜单 → **重复文件清理与字幕**（`ui/config` 桌面入口，跳转到 8787）；
   - 套件中心 → 打开（`adminport=8787`）。

### 4.2 首次访问与登录

- 浏览器访问 `http://<NAS_IP或主机名>:8787/`。
- 使用 DSM **administrators 组**账号登录；需要 2FA 的账号在“验证码”输入 6 位码。
- 后端通过本机 `https://127.0.0.1:5001`（或 `http://127.0.0.1:5000`）调用 `SYNO.API.Auth` 校验，不保存密码。
- 登录成功后，浏览器只持有 HttpOnly 会话 Cookie 与内存中的 CSRF Token。

> **网络风险**：默认是 HTTP。不要在公网直接映射 8787；远程使用请走 VPN，或为 DSM 配置反向代理 + 有效 TLS 证书。
> 点击 DSM 桌面图标时，由于 DSM 可能是 HTTPS，跳转到 HTTP 的 8787 会打开新窗口（不是 iframe），浏览器允许；但凭据走明文，务必只在可信局域网使用。
> 如需内置 HTTPS：见 §7。

## 5. 运行、日志与数据

### 5.1 服务控制

```bash
# SSH 登录 DSM（需开启 SSH）
sudo /var/packages/DuplicateCleaner/scripts/start-stop-status status
sudo /var/packages/DuplicateCleaner/scripts/start-stop-status start
sudo /var/packages/DuplicateCleaner/scripts/start-stop-status stop

# DSM 7 也支持
sudo synopkg status DuplicateCleaner
sudo synopkg start DuplicateCleaner
sudo synopkg stop DuplicateCleaner
```

> 实际脚本路径由套件框架复制到 `/var/packages/DuplicateCleaner/scripts/`（不同 DSM 版本可能略有差异，以 `synopkg` 为准）。

### 5.2 数据目录

```
/var/packages/DuplicateCleaner/target/      只读程序（app/, web/, ui/, port_conf/）
/var/packages/DuplicateCleaner/var/         数据（duplicatecleaner:duplicatecleaner 700）
  settings.json                             设置（云端密钥 600）
  dupcleaner.sqlite3 (+WAL/SHM)             扫描/分组/明细/断点/删除/subtitle 任务
  plans/*.json                              删除计划（5 分钟）
  logs/backend.log                          服务日志
  logs/scan-*.log                           扫描摘要
  logs/delete-*.log                         删除任务摘要
  （旧 scans/*.json、deletions/*.jsonl 仅作迁移来源，不再新增）
  audit.jsonl                               审计事件（追加）
  backend.pid                               进程号
  tls.env                                   （可选）TLS 配置
```

- 卸载默认**保留** `var/`，满足日志持久化；彻底清理需手动 `rm -rf`。
- 备份/迁移：停止套件后复制 `var/` 即可（不含明文密码）。

### 5.3 端口与防火墙

- 默认 `8787/tcp`，在 `conf/resource` 中注册（`port_conf/DuplicateCleaner.sc`）。
- 若 DSM 防火墙开启：**控制面板 → 安全性 → 防火墙**，允许该套件/端口，仅限内网网段。
- 不要做路由器端口映射到公网。
- 端口冲突：`adminport` 和启动脚本硬编码 8787；修改需同时改 `INFO`、`start-stop-status`、`port_conf/DuplicateCleaner.sc`（或先用 `servicetool --conf-port-conflict-check --tcp 8787` 检查）。

## 6. 权限模型（必读）

本版本**不再以 root 运行**（DSM 7 会拒绝安装 root 第三方套件）。`conf/privilege` 使用：

```json
{
  "defaults": { "run-as": "package" },
  "username": "duplicatecleaner",
  "groupname": "duplicatecleaner"
}
```

安装后套件以系统内部用户 **`duplicatecleaner`** 运行。首次使用前必须授权共享文件夹：

1. **控制面板 → 共享文件夹 → 选择目标共享文件夹 → 编辑 → 权限**。
2. 权限对象下拉选择 **系统内部用户**。
3. 找到 **`duplicatecleaner`**：
   - 只扫描/预览：授予 **读取**；
   - 需要删除/移入回收站：授予 **读取/写入**。
4. 确定。若目标在子目录，父目录需要至少“遍历/列出”权限。

也可在 **File Station → 目标目录 → 属性 → 权限** 中单独授权。权限是 DSM/Windows ACL，
不是简单的 Unix chmod；请优先用 DSM 图形界面设置。

为什么需要授权：套件后端要读取文件内容计算哈希/特征，并在删除时把文件移动到
`<共享文件夹>/#recycle/@duplicate-cleaner/...`。没有授权时目录选择器/扫描会提示权限不足，
不会静默跳过或删除。若回收站提示不可用，请确认 `duplicatecleaner` 对 `#recycle` 也有写入权限（授权时应用到子文件夹/文件），
或先改用永久删除（需二次确认）。

其他安全收敛：

- 只暴露经过 DSM 管理员认证的 Web 会话；
- 路径白名单（共享文件夹根）+ 系统目录黑名单；
- 扫描/删除均不跟随 symlink；
- 删除前完整重校验 + 两次确认 + 服务端 `confirm=DELETE`；
- 默认回收站而非永久删除；
- 审计日志。

如只需扫描、不允许删除：只授予“读取”即可（删除计划会因写入失败被跳过）。

> 从早期 root 版本升级时，旧数据目录可能仍属 root。若启动日志提示“数据目录不可写”，
> 以管理员 SSH 执行：
> `sudo chown -R duplicatecleaner:duplicatecleaner /var/packages/DuplicateCleaner/var`
> 然后重启套件。

## 7. 可选：启用内置 HTTPS

1. 准备证书（PEM）：`fullchain.pem` + `privkey.pem`。由于后端以 `duplicatecleaner` 用户运行，
   不能直接读取 DSM 默认证书私钥；请把证书复制到套件数据目录并只给套件用户读取：

```bash
sudo mkdir -p /var/packages/DuplicateCleaner/var/tls
sudo cp /usr/syno/etc/certificate/system/default/fullchain.pem /var/packages/DuplicateCleaner/var/tls/
sudo cp /usr/syno/etc/certificate/system/default/privkey.pem /var/packages/DuplicateCleaner/var/tls/
sudo chown -R duplicatecleaner:duplicatecleaner /var/packages/DuplicateCleaner/var/tls
sudo chmod 700 /var/packages/DuplicateCleaner/var/tls
sudo chmod 600 /var/packages/DuplicateCleaner/var/tls/*
```
2. 创建 `/var/packages/DuplicateCleaner/var/tls.env`（duplicatecleaner 可读）：

```bash
sudo tee /var/packages/DuplicateCleaner/var/tls.env >/dev/null <<'EOF'
DUP_TLS_CERT=/var/packages/DuplicateCleaner/var/tls/fullchain.pem
DUP_TLS_KEY=/var/packages/DuplicateCleaner/var/tls/privkey.pem
EOF
sudo chown duplicatecleaner:duplicatecleaner /var/packages/DuplicateCleaner/var/tls.env
sudo chmod 600 /var/packages/DuplicateCleaner/var/tls.env
```

3. 用 `--scheme https` 重新打包（让 DSM 桌面入口跳转 HTTPS）：

```bash
python build/build_spk.py --scheme https
```

4. 重新安装/升级套件并启动；访问 `https://<NAS>:8787/`。
5. 若证书主机名不匹配，浏览器会告警；推荐使用覆盖 NAS 主机名的有效证书。`adminport` 仍为 http，套件中心的“打开”可能不适用，请用桌面图标或手动输入 https。

> 更推荐的做法：DSM **控制面板 → 登录门户 → 高级 → 反向代理**，将
> `https://<自定义域名>/duplicate/` 反代到 `http://127.0.0.1:8787/`，并配置有效证书；
> 此时建议把后端的 host 改为 `127.0.0.1`，只允许本机反代访问（需要改启动脚本）。

## 8. 故障排查

| 现象 | 排查 |
|---|---|
| 套件安装失败：未找到 Python 3 | 套件中心安装官方 Python3；SSH 执行 `/usr/local/bin/python3 --version` |
| 启动失败，套件中心显示 stopped | `tail -n 100 /var/packages/DuplicateCleaner/var/log/backend.log`；检查 8787 占用、Python 路径、权限 |
| 升级后页面仍加载旧 JS | 本版本页面资源已带版本查询串；若仍出现，强制刷新/清缓存，或确认安装的 build number 已递增 |
| 8787 能开但登录失败 | 管理员账号？2FA？DSM 5001/5000 是否可本机访问？`curl -k https://127.0.0.1:5001/webapi/query.cgi?api=SYNO.API.Info` |
| 登录提示非管理员 | 将账号加入 administrators 组；或检查 `synogroup --member administrators` |
| 扫描结果为空 | 检查大小区间、后缀过滤、是否跳过空文件、目录权限；确认不是只选了空目录 |
| 创建时间显示“未知”，无法自动保留 | 文件系统/内核不支持 btime，或文件在非 Btrfs/EXT4 挂载；手动选择保留项；不要开启 ctime 降级 |
| 删除时提示回收站不可用 | 共享文件夹未启用回收站；在 DSM 共享文件夹设置中启用，或选择永久删除（二次确认） |
| 删除全部跳过 FILE_CHANGED | 扫描后文件被修改/替换；重新扫描后再删 |
| 页面空白/跳转失败 | 确认后端启动、8787 可达、桌面入口由 `--scheme` 构建；HTTPS DSM 跳 HTTP 需允许新窗口 |
| 升级后代码未生效 | 停止/启动套件；构建脚本会排除 `__pycache__`；确认安装的是新版本 build number |

## 9. 签名与分发（可选）

- DSM 7 默认允许手动安装未签名第三方套件，但会提示风险；企业环境可能要求签名。
- 使用 Synology 的套件签名工具/开发者证书对 SPK 签名，或通过内部 Package Center 分发。
- 无论是否签名，安装前都应校验 `dist/*.spk.sha256`：

```powershell
Get-FileHash dist\DuplicateCleaner-1.1.0-0039.spk -Algorithm SHA256
Get-Content dist\DuplicateCleaner-1.1.0-0039.spk.sha256
```

## 10. 升级与卸载

### 升级

1. 构建新版本（递增 `INFO` 的 build number，如 `1.0.0-0002`）。
2. 套件中心手动安装同 package 的新 SPK；DSM 调用 `preupgrade/postupgrade`。
3. `postupgrade` 保留 `var/` 中的报告、日志、审计与设置；清理旧 `__pycache__`。
4. 升级后重新启动服务。

### 卸载

1. 套件中心卸载；DSM 先停止服务，再执行 `preuninst/postuninst`。
2. 默认**保留** `/var/packages/DuplicateCleaner/var`（审计日志持久化）。
3. 确认不再需要日志后手动删除：

```bash
sudo rm -rf /var/packages/DuplicateCleaner/var
```

## 11. 代码中的群晖 API / 系统调用点

| 调用点 | 文件（预期） | 说明 |
|---|---|---|
| `SYNO.API.Auth` 登录/登出 | `src/backend/dupcleaner/auth.py` / `syno_api.py` | 本机 5001/5000；2FA `otp_code`；失败映射中文 |
| `SYNO.Core.Share` 共享枚举 | `src/backend/dupcleaner/shares.py` | 管理员 sid；回退 `synoshare --enum SHARE` |
| `synogroup --member administrators` | `src/backend/dupcleaner/auth.py` | 管理员组校验 |
| `statx(STATX_BTIME)` | `src/backend/dupcleaner/timeutil.py` | x86_64=332 / aarch64=291；`AT_SYMLINK_NOFOLLOW` |
| 分片哈希 | `src/backend/dupcleaner/hashing.py` | 1 MiB 块；`hashlib`；全局限速 |
| 文件删除/回收站 | `src/backend/dupcleaner/deletion.py` | `os.rename` → `#recycle/@duplicate-cleaner/...`；`os.unlink` 永久删除 |
| 文件预览 | `src/backend/dupcleaner/api.py` | HTTP Range；路径白名单 |

> 群晖没有公开的“计算文件哈希”Web API，哈希必须本地读取；创建时间使用内核 `statx` 而不是 `st_ctime`；
> 删除的群晖语义是移动到共享文件夹的 `#recycle`，代码直接实现该语义以便精确控制与审计。

## 12. 交付检查清单

- [ ] `dist/*.spk` 可由 `tar -tf` 打开，包含 INFO/package.tgz/scripts/conf/ui 图标
- [ ] `INFO` 的 `checksum` 与 package.tgz 的 MD5 一致
- [ ] `preinst` 在无 Python 时失败并给出中文提示
- [ ] 启动后 `/api/healthz` 返回 ok
- [ ] 默认扫描不进入系统目录/符号链接
- [ ] 保留项为创建时间最晚，且创建时间来源显示正确
- [ ] 删除必须两次确认；默认进回收站；永久删除需短语
- [ ] 审计日志包含扫描、计划、逐文件删除
- [ ] 卸载默认保留 var 日志
