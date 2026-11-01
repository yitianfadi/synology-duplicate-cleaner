# DuplicateCleaner 需求追溯清单（QA）

来源：用户原始需求 + docs/CONTRACT.md（binding）+ docs/ARCHITECTURE.md + docs/SPK_PACKAGING.md + README.md。

状态说明：`PASS` 有可复现证据；`FAIL` 有具体复现；`UNVERIFIED` 未验证/环境不支持；`N/A` 不适用。
最终逐项结论与证据写入 `qa/REPORT.md`。

| ID | 用户显式需求 | 验收口径（可测断言） | 预期实现位置 | 验证手段 | 状态 |
|---|---|---|---|---|---|
| REQ-01 | 仅按文件内容哈希判重，不按文件名/大小/时间 | 内容相同→同组；大小相同内容不同→不同组；改名不影响 | backend scanner/hashing | e2e: 同内容不同名 → 1 组；同大小不同内容 → 0 组 |  |
| REQ-02 | 默认只保留创建时间最晚的一份 | safe 组 `keep_reason=latest_created`，keep 的 created_ts 最大 | scanner finalize + settings keep_mode | e2e: 3 副本按创建顺序，keep=最后创建 |  |
| REQ-03 | 创建时间不能用 mtime 冒充 | 反转 mtime 后 keep 仍按 btime；created_source ∈ statx/birthtime | timeutil + scanner | e2e: 设置相反 mtime；检查 created_source/keep；静态 grep 无 `st_ctime` 静默使用 |  |
| REQ-04 | 大文件分片读取 + 限速（可配置并发） | 1MiB 分片、token bucket、rate 生效、concurrency 1..8 | hashing.py / scanner.py | 静态审阅 + e2e 限速扫描耗时/设置校验 |  |
| REQ-05 | 文件夹多选 + 排除目录 | /api/shares + /api/browse；roots 多选、excludes 生效 | browse.py / scanner | e2e: 2 个 root 同时扫描；exclude 目录内文件不出现 |  |
| REQ-06 | 结果分组展示/保留高亮/预览/全选/CSV | groups 分页、is_keep、default_selected；/api/preview Range；CSV BOM+列 | scanner/store/preview/api + app.js | e2e API + node 静态检查 app.js DOM 行为 |  |
| REQ-07 | 绝不静默/自动删除；扫描只出报告 | 扫描后文件零变化；删除必须 plan+confirm=DELETE | scanner/deletion/api | e2e: 扫描完成后对比文件快照；静态 grep 无自动 delete |  |
| REQ-08 | 报告统计（组数/副本数/可释放空间/跳过计数） | stats 字段齐全且数值自洽，wasted=size*(count-1) | scanner | e2e 断言 stats |  |
| REQ-09 | 删除必须两次确认 | 前端第一屏清单 + 第二屏最终确认；永久删除输入“永久删除” | app.js | 静态审查 + DOM/字符串证据 |  |
| REQ-10 | 默认移入回收站，可选永久删除 | recycle=true → #recycle/@duplicate-cleaner/...；recycle=false 需短语 | deletion.py | e2e: 两种删除都验证 + 目标路径 |  |
| REQ-11 | 操作日志持久化可追溯 | audit.jsonl + scans/*.json + deletions/*.jsonl + logs；重启后可见 | store/deletion | e2e: 检查文件与 /api/logs、重启后列表 |  |
| REQ-12 | DSM 7 / Btrfs / EXT4 兼容 | INFO os_min_ver=7.0；statx x86_64=332/aarch64=291；Python 3.8 语法 | spk/INFO, timeutil, 全部 py | 静态检查 + py_compile + SPK 结构 |  |
| REQ-13 | 跳过系统目录/符号链接/空文件/隐藏文件/后缀过滤 | 分别有 skipped 计数；系统目录（@eaDir/#recycle 等）永不进入 | scanner/fsutil | e2e: 构造各类 fixture；断言 groups/skipped |  |
| REQ-14 | 扫描可暂停/继续/停止 | 状态机 paused/stopped 合法；非法转换报错 | scanner/api | e2e: 限速大文件扫描中 pause/resume/stop |  |
| REQ-15 | 可保留最早 / 指定路径（pinned） | keep_mode=earliest/pinned 生效；pinned 未命中→人工 | scanner | e2e: earliest 与 pinned 两种扫描 |  |
| REQ-16 | 大小区间过滤 | min/max_size_bytes 生效 | scanner | e2e: 边界大小 fixture |  |
| REQ-17 | 后缀比较可忽略大小写 | ignore_case_ext=true 时 .JPG 命中 .jpg 过滤 | scanner | e2e: 大小写后缀过滤 |  |
| REQ-18 | 顶部常驻风险提示（精确文案） | 文案逐字包含契约 §10 文本且不可关闭 | src/web/index.html/app.js | grep 精确文本 + 静态审查 |  |
| REQ-19 | 仅管理员 DSM 登录；不保存密码 | SYNO.API.Auth 5001/5000、管理员组校验；dev 模式仅显式开启 | auth.py | e2e dev 登录/错误密码；静态审阅；SPK 脚本 unset dev 变量 |  |
| REQ-20 | 删除前服务端逐文件重校验（存在/未变/非保留/哈希一致/路径安全） | 校验失败进 skipped，绝不静默删除；plan 5 分钟一次性绑定会话 | deletion.py | e2e: 删除保留项被拒；改文件后 plan 跳过；plan 重放失败 |  |
| REQ-21 | 路径白名单/黑名单（防遍历、防系统路径） | /etc /usr /var /root /volumeX 根、@*/#recycle 等拒绝 | fsutil/shares/browse/preview | e2e: 遍历/系统路径/preview 越权请求 |  |
| REQ-22 | 硬链接按 (dev,ino) 去重 | 同 inode 多路径只算一份，避免虚假可释放空间 | scanner | e2e: os.link fixture（若平台支持） |  |
| REQ-23 | SPK 打包正确（INFO/checksum/scripts/conf/ui/图标；无 dev/测试/pycache） | tar 结构、MD5 checksum、脚本可执行、无 DUP_CLEANER_DEV、无 tests/pycache | spk/** + build/** + dist/** | SPK 解包审查 |  |
| REQ-24 | 扫描/删除低影响（nice/ionice、单任务、max_files、分页） | 单扫描 busy；start-stop-status nice/ionice；分页参数 | SCANNER/api/spk 脚本 | 静态 + e2e SCAN_BUSY |  |
| REQ-25 | 设置持久化与白名单校验 | GET/PUT /api/settings；未知键 INVALID_ARGUMENT；范围校验；重启保留 | settings/store/api | e2e: 保存→重启→读取；非法值 400 |  |
| REQ-26 | 前端无 CDN/无构建、ES2019、全中文、安全 DOM | node --check；无 http(s):// 外链资源；textContent；路由/端点覆盖 | src/web/** | node --check + grep 审查 |  |
| REQ-27 | 预览仅共享内普通文件，支持 Range，文本截断 | 206 Range；不支持类型 415；越权拒绝 | preview.py | e2e: 图片/文本/Range/文本截断/目录/越权 |  |
| REQ-28 | 日志下载白名单，禁路径穿越 | name 正则；../ 拒绝 | api.py | e2e: 下载正常/穿越拒绝 |  |
| REQ-29 | 删除任务后台执行、逐文件结果、终态 partial/failed | 202 + job 轮询；results 状态；重启后历史可见 | deletion/store | e2e: 轮询 job；检查 JSONL/audit |  |
| REQ-30 | 卸载保留 var 日志；升级保留数据 | postinst/preuninst/postuninst/postupgrade 不删 var | spk/scripts | 静态审查 |  |
| REQ-31 | 回收站不可用时默认不降级为永久删除 | RECYCLE_UNAVAILABLE / recycle_unavailable 字段 | deletion | e2e: 无 #recycle 时 plan 标记不可用 |  |
| REQ-32 | 扫描完成只展示、不触发删除 | scanner 无删除调用 | scanner.py | 静态 grep + e2e 快照 |  |

## 环境限制与降级
- Windows 主机无 bash：SPK shell 脚本做静态审查（人工 + 可选 Git Bash/WSL），无法真机安装 DSM。
- 符号链接创建需权限：若 `os.symlink` 失败，该项标记 UNVERIFIED 并说明。
- 5 分钟 plan 过期：不等待 5 分钟；用一次性重放/错误 plan 验证，过期逻辑仅静态审阅。
