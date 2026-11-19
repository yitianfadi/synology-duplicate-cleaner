# -*- coding: utf-8 -*-
"""播放网页静态解析（v0052，docs/DOWNLOAD_CONTRACT.md §14）。

只做纯字符串处理：**不执行 JavaScript、不发网络请求、不读写文件**。
输入一段 HTML（或浏览器 `curl` 命令），输出候选媒体地址列表；由
`downloads.DownloadService.parse_target` 负责抓页面、校验 m3u8、暂存登录态。

提取顺序（同一地址只保留一次，先出现的来源优先）：
1. `<video>` / `<audio>` / `<source>` 的 src、data-src 等属性；
2. `<meta property="og:video...">` 等社交卡片与 `<link rel="preload">`；
3. JSON-LD（`application/ld+json`）里的 contentUrl / embedUrl / videoUrl；
4. 脚本/内联配置里的字符串字面量（含 `.m3u8`、`.mp4` 等扩展名），
   并尝试记住上下文键名（file/source/url/src/loadSource…）；
5. 兜底：整页任意引号内的媒体扩展名地址。

相对地址用 `urljoin` 补全；`data:` / `blob:` / `javascript:` / `about:` 丢弃。
"""

from __future__ import absolute_import

import html as html_module
import json
import re
import urllib.parse

# 媒体扩展名（点播容器 + HLS/DASH 清单）
MEDIA_EXTENSIONS = (
    ".m3u8", ".mpd", ".mp4", ".m4v", ".webm", ".mkv", ".mov", ".flv",
    ".f4v", ".ts", ".m4s", ".mp3", ".m4a",
)

# 只在“看起来是媒体地址”时采纳的扩展名（其余扩展名容易误报，见 _looks_media）
_STRICT_EXTENSIONS = (".m3u8", ".mpd", ".mp4", ".m4v", ".webm", ".mkv",
                      ".mov", ".flv", ".f4v")

_ATTR_RE = re.compile(
    r"""(?P<name>[A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*"""
    r"""(?P<value>"[^"]*"|'[^']*'|[^\s"'>]+)""")

_TAG_RE = re.compile(r"<\s*(video|audio|source|link)\b([^>]*)>",
                     re.IGNORECASE)
_IFRAME_RE = re.compile(r"<\s*(?:iframe|frame)\b([^>]*)>", re.IGNORECASE)
_META_TAG_RE = re.compile(r"<\s*meta\b([^>]*)>", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_JSONLD_RE = re.compile(
    r"""<script[^>]+type\s*=\s*["']application/ld\+json["'][^>]*>"""
    r"""(.*?)</script>""", re.IGNORECASE | re.DOTALL)

# v0064：标题清洗。站点标题常见形态：
#   「正在播放_<片名>- <站名>_<站名长尾>」，要还原成 <片名> 当文件名。
_TITLE_META_KEYS = ("og:title", "twitter:title", "title")
_TITLE_SPLIT_RE = re.compile(u"[_|｜丨]+|\\s*[-–—]\\s+|\\s+[-–—]\\s*")
_TITLE_PREFIX_RE = re.compile(
    u"^(?:正在播放|正在直播|在线播放|在线观看|立即播放|视频播放|播放中|播放)"
    u"\\s*[:：_\\-–—|｜·]*\\s*")
_TITLE_SUFFIX_RE = re.compile(
    u"(?:\\s*[-–—_|｜]\\s*)?(?:免费)?(?:高清|超清|蓝光)?"
    u"(?:在线观看|在线播放|手机观看|完整版|全集|迅雷下载|下载|观看)$")
_TITLE_EDGE_CHARS = u" -_|｜丨·:：,，.。;；、"
_TITLE_NOISE_SEGMENTS = (u"正在播放", u"在线播放", u"正在直播", u"播放中",
                         u"在线观看", u"视频", u"首页", u"立即播放",
                         u"视频播放", u"播放")
_TITLE_SITE_WORDS = (u"视频", u"影视", u"影院", u"电影网", u"网站", u"网",
                     u"站", u"在线", u"直播", u"下载", u"APP", u"app",
                     u"官网", u"首页", u"免费", u"高清")

# 引号内的媒体地址（允许查询串；不跨引号/尖括号）
_QUOTED_MEDIA_RE = re.compile(
    r"""["']([^"'\s<>]{4,4096}?(?:%s)(?:\?[^"'\s<>]*)?)["']"""
    % "|".join(re.escape(ext) for ext in MEDIA_EXTENSIONS),
    re.IGNORECASE)

# 播放器配置键（用于给候选加“来源键名”提示，不参与匹配地址本身）
_CONTEXT_KEY_RE = re.compile(
    r"""(?P<key>file|source|src|url|videoUrl|video_url|playUrl|play_url|
         loadSource|attachSource|sources)\s*[:=(]\s*["']""",
    re.IGNORECASE | re.VERBOSE)

_HTML_ATTR_KEYS = (
    "src", "data-src", "data-video", "data-url", "data-file", "data-stream",
    "data-source", "data-mp4", "data-hls", "data-live", "data-playurl",
)

_META_VIDEO_KEYS = (
    "og:video", "og:video:url", "og:video:secure_url",
    "twitter:player:stream", "twitter:player",
)

_JSONLD_KEYS = ("contentUrl", "contentURL", "embedUrl", "embedURL",
                "videoUrl", "videoURL")

_IGNORED_SCHEMES = ("data:", "blob:", "javascript:", "about:", "mailto:")

_CURL_URL_RE = re.compile(
    r"""curl(?:\.exe)?[\s\S]{0,2000}?["'](https?://[^"'\s]+)["']""",
    re.IGNORECASE)
_CURL_HANDLE_RE = re.compile(
    r"""(?:-H|--header)\s+(?P<q>["'])(?P<value>[\s\S]*?)(?P=q)""",
    re.IGNORECASE)
_CURL_UA_RE = re.compile(
    r"""(?:-A|--user-agent)\s+(?P<q>["'])(?P<value>[\s\S]*?)(?P=q)""",
    re.IGNORECASE)
_CURL_LINE_CONT = re.compile(r"\\\r?\n[ \t]*")
_CURL_PS_CONT = re.compile(r"`\r?\n[ \t]*")


class PageParseError(Exception):
    """解析层面的可读错误。"""

    def __init__(self, code, message):
        super(PageParseError, self).__init__(message)
        self.code = code
        self.message = message


# ----------------------------------------------------------------------
# 基础清洗
# ----------------------------------------------------------------------
def normalize_text(text):
    """解码 `\\u002F` / `\\/` 等 JS 转义，并还原 HTML 实体。"""
    raw = text or u""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    # \u002F、\u003D 之类的 JS 字符串转义
    raw = re.sub(r"\\u([0-9a-fA-F]{4})",
                 lambda m: _chr(m.group(1)), raw)
    # 转义的斜杠（JSON 内常见）
    raw = raw.replace(u"\\/", u"/")
    # HTML 实体（&amp; &quot; &#39;）
    try:
        raw = html_module.unescape(raw)
    except Exception:  # pragma: no cover - 兜底
        pass
    return raw


def _chr(hex_text):
    try:
        return chr(int(hex_text, 16))
    except (ValueError, OverflowError):
        return u""


def _is_site_segment(text):
    """判断一段标题切片是不是「站名 / 导航词」，判真则从标题里丢掉。"""
    seg = (text or u"").strip()
    if not seg:
        return True
    if seg in _TITLE_NOISE_SEGMENTS:
        return True
    if len(seg) <= 40 and any(word in seg for word in _TITLE_SITE_WORDS):
        return True
    return False


def clean_title(text):
    """把站点标题清洗成可当文件名的片名（v0064）。

    规则：去标签/实体 → 按 `_ | ｜ -` 等切成片段 → 丢掉「正在播放」「视频网站」
    这类站名片段 → 取剩下最长的一段 → 去掉首部「正在播放」与尾部「在线观看」
    之类后缀。清洗后为空则返回空串（调用方回退到 URL 名）。
    """
    raw = normalize_text(text or u"")
    raw = re.sub(r"<[^>]+>", u" ", raw)
    raw = re.sub(r"\s+", u" ", raw).strip()
    if not raw:
        return u""
    segments = [item.strip(_TITLE_EDGE_CHARS)
                for item in _TITLE_SPLIT_RE.split(raw)]
    segments = [item for item in segments if item]
    if not segments:
        return u""
    kept = [item for item in segments if not _is_site_segment(item)]
    chosen = max(kept or segments, key=len)
    chosen = _TITLE_PREFIX_RE.sub(u"", chosen).strip(_TITLE_EDGE_CHARS)
    for _ in range(3):
        trimmed = _TITLE_SUFFIX_RE.sub(u"", chosen)
        trimmed = trimmed.strip(_TITLE_EDGE_CHARS).strip()
        if trimmed == chosen:
            break
        chosen = trimmed
    chosen = re.sub(r"\s+", u" ", chosen).strip(_TITLE_EDGE_CHARS).strip()
    return chosen[:200]


def extract_title(page_html):
    """页面标题（v0064：优先 og:title / twitter:title，再 `<title>` / `<h1>`）。

    返回值已经过 `clean_title` 清洗，可直接当文件名用；找不到时返回空串。
    """
    text = normalize_text(page_html)
    if not text:
        return u""
    for match in _META_TAG_RE.finditer(text):
        attrs = _parse_attrs(match.group(1))
        key = (attrs.get("property") or attrs.get("name") or u"").lower()
        if key not in _TITLE_META_KEYS:
            continue
        title = clean_title(attrs.get("content") or u"")
        if title:
            return title
    for pattern in (_TITLE_RE, _H1_RE):
        match = pattern.search(text)
        if not match:
            continue
        title = clean_title(match.group(1))
        if title:
            return title
    return u""


def decode_body(data, content_type=None):
    """按 Content-Type / meta charset 解码页面，失败回退 utf-8 → gb18030。"""
    if isinstance(data, str):
        return data
    blob = bytes(data or b"")
    candidates = []
    if content_type:
        match = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)",
                          content_type, re.IGNORECASE)
        if match:
            candidates.append(match.group(1))
    head = blob[:4096].decode("ascii", "ignore")
    match = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)", head,
                      re.IGNORECASE)
    if match:
        candidates.append(match.group(1))
    candidates.extend(["utf-8", "gb18030"])
    seen = set()
    for charset in candidates:
        key = charset.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            return blob.decode(charset, "strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return blob.decode("utf-8", "replace")


# ----------------------------------------------------------------------
# URL 归一
# ----------------------------------------------------------------------
def _absolutize(candidate, page_url):
    text = (candidate or u"").strip()
    if not text:
        return u""
    lower = text.lower()
    for prefix in _IGNORED_SCHEMES:
        if lower.startswith(prefix):
            return u""
    if lower.startswith("//"):
        scheme = u"https:"
        if page_url and page_url.lower().startswith("http://"):
            scheme = u"http:"
        return scheme + text
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", text):
        # 已有绝对 scheme（http/https/ftp…），只接受 http(s)
        if not lower.startswith(("http://", "https://")):
            return u""
        return text
    if not page_url:
        return text
    try:
        return urllib.parse.urljoin(page_url, text)
    except ValueError:
        return u""


def _path_of(url):
    try:
        return urllib.parse.urlsplit(url).path or u""
    except ValueError:
        return u""


def _kind_of(url):
    path = _path_of(url).lower()
    if path.endswith(".m3u8"):
        return u"hls"
    if path.endswith(".mpd"):
        return u"dash"
    if path.endswith(_STRICT_EXTENSIONS):
        return u"file"
    return u"file"


def _looks_media(url):
    path = _path_of(url).lower()
    if any(path.endswith(ext) for ext in MEDIA_EXTENSIONS):
        return True
    # 没有扩展名但路径像播放地址（/hls/xxx、/live/xxx、playlist）
    return bool(re.search(r"/(hls|live|vod|playlist|stream|media)/", path))


def _score(url, kind, source):
    score = 0
    if kind == u"hls":
        score += 100
    elif kind == u"dash":
        score += 80
    elif kind == u"file":
        score += 60
    if source in (u"video-tag", u"meta", u"json-ld"):
        score += 10
    if re.search(r"(master|index|playlist|prog_index)", _path_of(url),
                 re.IGNORECASE):
        score += 5
    return score


# ----------------------------------------------------------------------
# 提取
# ----------------------------------------------------------------------
def _parse_attrs(text):
    out = {}
    if not text:
        return out
    for match in _ATTR_RE.finditer(text):
        name = match.group("name").lower()
        value = match.group("value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in (u"\"", u"'"):
            value = value[1:-1]
        out.setdefault(name, value)
    return out


def _add(result, seen, url, page_url, source, kind=None, label=None):
    absolute = _absolutize(url, page_url)
    if not absolute:
        return
    # 嵌入播放器页不是媒体文件，但保留给用户点开；其余必须像媒体地址。
    if kind != "embed" and not _looks_media(absolute):
        return
    if absolute in seen:
        return
    seen.add(absolute)
    resolved_kind = kind or _kind_of(absolute)
    result.append({
        "url": absolute,
        "kind": resolved_kind,
        "ext": _suffix(absolute),
        "source": source,
        "label": label or u"",
        "score": _score(absolute, resolved_kind, source),
        "order": len(result),
    })


def _suffix(url):
    path = _path_of(url).lower()
    for ext in MEDIA_EXTENSIONS:
        if path.endswith(ext):
            return ext
    return u""


def _iter_jsonld_urls(node, depth=0):
    """递归遍历 JSON-LD，产出 (key, url)。"""
    if depth > 8 or node is None:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _JSONLD_KEYS and isinstance(value, str):
                yield key, value
            elif isinstance(value, (dict, list)):
                for item in _iter_jsonld_urls(value, depth + 1):
                    yield item
    elif isinstance(node, list):
        for item in node[:200]:
            for found in _iter_jsonld_urls(item, depth + 1):
                yield found


def extract_candidates(page_html, page_url=u""):
    """从一段 HTML 提取候选媒体地址（去重、按可信度排序）。"""
    text = normalize_text(page_html)
    if not text:
        return []
    result = []
    seen = set()

    # 1) media/source/link 标签属性
    for match in _TAG_RE.finditer(text):
        attrs = _parse_attrs(match.group(2))
        for name in _HTML_ATTR_KEYS:
            value = attrs.get(name)
            if value:
                _add(result, seen, value, page_url, u"video-tag", label=name)
        if (attrs.get("rel") or u"").lower() == "preload":
            as_value = (attrs.get("as") or u"").lower()
            href = attrs.get("href") or attrs.get("src")
            if href and as_value in (u"video", u"audio", u"fetch", u""):
                _add(result, seen, href, page_url, u"link-preload")

    # 2) 社交卡片 meta
    for match in _META_TAG_RE.finditer(text):
        attrs = _parse_attrs(match.group(1))
        key = (attrs.get("property") or attrs.get("name") or u"").lower()
        content = attrs.get("content") or u""
        if key in _META_VIDEO_KEYS and content:
            embed = content.lower().startswith(("http://", "https://")) \
                and not _looks_media(content)
            _add(result, seen, content, page_url, u"meta",
                 kind=u"embed" if embed else None, label=key)

    # 3) JSON-LD
    for block in _JSONLD_RE.findall(text):
        payload = _loads_json(block)
        for key, url in _iter_jsonld_urls(payload):
            embed = key.lower().startswith("embed")
            _add(result, seen, url, page_url, u"json-ld",
                 kind=u"embed" if embed else None, label=key)

    # 4) 引号内媒体地址（脚本/内联配置），带上下文键名
    for match in _QUOTED_MEDIA_RE.finditer(text):
        url = match.group(1)
        context = _context_before(text, match.start())
        _add(result, seen, url, page_url, u"script", label=context)

    result.sort(key=lambda item: (-item["score"], item.get("order", 0)))
    return result


def _context_before(text, index):
    window = text[max(0, index - 120):index]
    found = None
    for match in _CONTEXT_KEY_RE.finditer(window):
        found = match.group("key")
    return (found or u"").lower()


def _loads_json(block):
    raw = (block or u"").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        # 常见脏数据：结尾多余逗号
        cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
        try:
            return json.loads(cleaned)
        except ValueError:
            return None


# ----------------------------------------------------------------------
# curl 命令
# ----------------------------------------------------------------------
def parse_curl(text):
    """从浏览器「Copy as cURL」里取出 URL 与常用请求头。"""
    raw = text or u""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    raw = _CURL_LINE_CONT.sub(u" ", raw)
    raw = _CURL_PS_CONT.sub(u" ", raw)
    match = _CURL_URL_RE.search(raw)
    if not match:
        fallback = re.search(r"https?://[^\s'\"<>]+", raw)
        if not fallback:
            raise PageParseError("URL_INVALID",
                                 u"没能从 curl 命令里找到 http(s) 地址")
        url = fallback.group(0)
    else:
        url = match.group(1)
    headers = {}
    for header in _CURL_HANDLE_RE.finditer(raw):
        value = header.group("value") or u""
        if u":" not in value:
            continue
        name, _, header_value = value.partition(u":")
        name = name.strip().lower()
        header_value = header_value.strip()
        if name in ("referer", "referrer"):
            headers["referer"] = header_value
        elif name == "cookie":
            headers["cookie"] = header_value
        elif name == "user-agent":
            headers["user_agent"] = header_value
        elif name == "origin":
            headers["origin"] = header_value
    ua = _CURL_UA_RE.search(raw)
    if ua and "user_agent" not in headers:
        headers["user_agent"] = ua.group("value").strip()
    return {"url": url, "headers": headers}


def extract_iframes(page_html, page_url=u"", limit=8):
    """提取页面里的 iframe/frame 地址（v0053：解析时最多跟一层）。"""
    text = normalize_text(page_html)
    result = []
    seen = set()
    for match in _IFRAME_RE.finditer(text):
        attrs = _parse_attrs(match.group(1))
        value = attrs.get("src") or attrs.get("data-src") or u""
        absolute = _absolutize(value, page_url)
        if not absolute or absolute in seen:
            continue
        if not absolute.lower().startswith(("http://", "https://")):
            continue
        seen.add(absolute)
        result.append(absolute)
        if len(result) >= limit:
            break
    return result


def summarize(candidates):
    """给 API/UI 的一行摘要：hls 3 · file 2。"""
    counts = {}
    for item in candidates or []:
        counts[item.get("kind") or "file"] = counts.get(
            item.get("kind") or "file", 0) + 1
    parts = []
    for kind, label in (("hls", u"HLS"), ("dash", u"DASH"), ("file", u"直链"),
                        ("embed", u"嵌入页")):
        if counts.get(kind):
            parts.append(u"%s %d" % (label, counts[kind]))
    return u" · ".join(parts)
