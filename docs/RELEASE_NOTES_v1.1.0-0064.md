# 发布说明 1.1.0-0064（网页解析压缩响应修复 + 按标题命名）

- 版本：`1.1.0-0064`
- 日期：2026-11-19
- 定位：**下载模块的解析可靠性补丁**——修一个“明明页面里有 m3u8，却提示解析不到地址”的真实故障；顺带让下载产物按页面标题命名，不再一律叫 `index.mp4`。
- 升级：覆盖安装即可（无数据库改动，schema 仍为 v11，无设置迁移）。
- 兼容：仅后端与前端改动，`/api/downloads`、`/api/downloads/parse` 的既有请求/返回字段保持兼容，只新增可选字段。

## 1. 问题复现与根因

用户给了一个播放页（`…/vodplay/184731-1-1.html`），页面里其实已有播放器配置：

```html
<script>var player_aaaa={"flag":"play","encrypt":0, … ,
  "url":"https:\/\/thm3u8.co\/20260312\/MmtoHMqf\/index.m3u8"}</script>
```

但「解析播放网页」返回 0 个候选，提示“没有从页面里找到媒体地址”。

排查结论（本机用 curl 复验）：

| 客户端请求头 | 站点返回 |
|---|---|
| `Accept-Encoding: gzip` | `1f 8b…`（gzip），且带 `Content-Encoding: gzip` ✅ |
| `Accept-Encoding: identity` | 压缩正文，**却不带 `Content-Encoding`** ❌ |
| 不发送 `Accept-Encoding` | 同上 ❌ |
| `Accept-Encoding: br` | 同上（brotli 正文，同样无头） ❌ |

后端此前对文本类请求一律发 `Accept-Encoding: identity`，于是拿到一堆压缩字节、按 UTF‑8 解码成乱码 → 正则找不到任何媒体地址。**不是站点结构变了，而是响应编码协商的坑。**

## 2. 修复内容

### 2.1 下载 · 文本请求主动协商 gzip 并解压

- 新增 `config.DOWNLOAD_TEXT_ACCEPT_ENCODING = "gzip, deflate"`：网页、播放列表、RSS 这类**文本类**请求改为主动声明 gzip（与浏览器一致）。
- `_HttpClient.read_raw()`：读取后按 `Content-Encoding` 解压（gzip / x-gzip / deflate，deflate 兼容裸流）；**没有头但正文以 gzip 魔数开头时也按 gzip 解**（覆盖上面那种坏 CDN）。
- 解压有体积上限（`DOWNLOAD_PAGE_MAX_BYTES`），防止解压炸弹；媒体分片下载仍走 `identity` + 连接复用，不受影响。
- `read_raw()` 现在返回**跟随重定向后的最终地址**，页面里的相对地址按真实落地地址补全（此前用重定向前的地址）。
- 页面正文改为按 `Content-Type` / `meta charset` 解码（此前先按 UTF‑8 解码成字符串，GBK 页面会花屏）。

### 2.2 下载 · 按页面标题命名

- 解析结果里的 `title` 现在经过清洗（`pageparse.clean_title`）：
  - 优先 `og:title` / `twitter:title`，其次 `<title>`，最后 `<h1>`；
  - 按 `_ | ｜ -` 切片，丢掉「正在播放」「视频网站」「首页」这类站名片段，取剩下的最长一段；
  - 去首部「正在播放 / 在线观看」与尾部「在线观看 / 完整版 / 全集 / 下载」等后缀。
  - 实测：`正在播放_【东北往事】…国产合集- 一本道视频_不用下载就能看的…视频网站` → `【东北往事】…国产合集`。
- `POST /api/downloads` 新增可选字段 `titles`：`{地址: 标题}`。后端只认本次提交里的地址，标题再走一遍清洗 + 文件名非法字符过滤，然后写入任务 `title`；`{title}` 模板、`title` 子目录模板、文件名都用它。
  - 未传 `titles`、标题为空或地址对不上时，完全保持原行为（回退 URL 名）。
  - 文件名模板仍是 `{title}`（设置项 `download_name_template`），需要时可用 `{title}-{site}` 之类组合。
- 前端：解析完成后把标题记在 `titleMap[url]`，勾选「加入下载列表」时显示“文件名将使用标题「…」”，提交时只把本批地址对应的标题带上；提交成功即清空。

## 3. 验证

- 单元测试：新增 `tests/test_v0064_download_titles.py`（14 例）——标题清洗/优先级、gzip（带头与无头）/deflate/无压缩、压缩页面走完整解析链路、`titles` 命名与模板 token、非法/未知标题回退。
- e2e：新增 `DL-19`（gzip 带头/无头页面解析出地址并清洗标题）、`DL-20`（`titles` → 任务名与产物名 `QA 标题命名.mp4`）。
- 真实站点复验（本机）：`read_raw` 拿到 gzip 71,438 B → 标题 `【东北往事】…国产合集` → 候选 `hls …/index.m3u8`；该 m3u8 首行 `#EXTM3U` 且发现 720p 主列表，链路可用。
- 全量门禁：unittest / static / e2e / 前端模块图 / 设置键审计结果见 `qa/REPORT.md`。

## 4. 仍有的限制

- 仍然**不执行 JavaScript**：播放地址由 JS 动态请求（没有写进 HTML/脚本字面量）的站点，还是要用「粘贴网页源码 / Copy as cURL」。
- brotli 不在 Python 标准库内：若某站点**只**回 brotli（不理 gzip 协商），正文仍无法解压；此时可用 cURL 方式解析（把浏览器请求头一并带进去）。
- 标题清洗是启发式：极端标题（片名与站名用同一分隔符、片名本身带“在线观看”字样）可能被截短，可在「文件名模板」里用 `{site}`/`{date}` 组合，或下载后改名。
