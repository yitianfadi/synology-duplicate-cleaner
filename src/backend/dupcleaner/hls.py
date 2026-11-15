# -*- coding: utf-8 -*-
"""HLS(m3u8) 播放列表解析 / 本地化重写（v0051，docs/DOWNLOAD_CONTRACT.md §4）。

本模块只做纯字符串与 URL 处理：不发网络请求、不读写文件、不依赖 FFmpeg，
因此可以被单测直接驱动。下载器（downloads.py）负责真正拉取分片与合并。

设计要点
--------
1. 只支持**点播**播放列表（有 #EXT-X-ENDLIST 或 #EXT-X-PLAYLIST-TYPE:VOD）；
   直播播放列表一律拒绝（LIVE_UNSUPPORTED），避免把"录制"这种语义混进来。
2. 只支持不加密、AES-128、以及 SAMPLE-AES/DRM 之外的方式；遇到
   SAMPLE-AES / CENC / Widevine 等直接抛 DRM_UNSUPPORTED，不做任何绕过。
3. "本地化"：把分片/init/密钥全部落到本机后，重写成一份只引用本地文件的
   m3u8，再交给 FFmpeg 用 `-allowed_extensions ALL` 解密 + 无损封装。
   这样既不需要在标准库里实现 AES（会慢到不可用），也不改变播放时序。
"""

from __future__ import absolute_import

import math
import re
import urllib.parse

# m3u8 属性列表：[KEY=VALUE,KEY="QUOTED",...]
_ATTR_RE = re.compile(r"([A-Za-z0-9-]+)=(\"[^\"]*\"|[^,]*)")
_RANGE_RE = re.compile(r"^(\d+)(?:@(\d+))?$")

# 允许作为本地分片后缀的容器（其余一律按 .ts 处理）。
_MEDIA_SUFFIXES = (".ts", ".m4s", ".mp4", ".m4v", ".m4a", ".aac", ".cmfv",
                   ".cmfa", ".mp3", ".ac3", ".ec3")
_INIT_SUFFIX_DEFAULT = ".mp4"

# 加密方式分类（METHOD 原文 → 内部归类）
_ENCRYPTION_KINDS = {
    "NONE": "none",
    "AES-128": "aes-128",
    "SAMPLE-AES": "sample-aes",
    "SAMPLE-AES-CTR": "sample-aes",
    "SAMPLE-AES-CENC": "drm",
    "ISO-23001-7": "drm",
}

VARIANT_SORT_KEYS = ("height", "bandwidth")


class HlsError(Exception):
    """播放列表层面的可读错误（code 与 API 错误码一致）。"""

    def __init__(self, code, message):
        super(HlsError, self).__init__(message)
        self.code = code
        self.message = message

    def __repr__(self):
        return "HlsError(%r, %r)" % (self.code, self.message)


def is_playlist(text):
    """内容是否像 HLS 播放列表（允许 UTF-8 BOM 与前置空行）。"""
    if not text:
        return False
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    head = text.lstrip(u"\ufeff\r\n\t ")
    return head.startswith("#EXTM3U")


def parse_attributes(text):
    """解析 `KEY=VALUE,KEY="VALUE"` 形式的属性串。"""
    out = {}
    if not text:
        return out
    for match in _ATTR_RE.finditer(text):
        key = match.group(1).upper()
        value = match.group(2)
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        out[key] = value
    return out


def _split_byte_range(text, previous_end):
    """`1234@567` → (length, offset)；省略 offset 时接上一次结束位置。"""
    match = _RANGE_RE.match((text or "").strip())
    if match is None:
        raise HlsError("PLAYLIST_INVALID", u"分片字节范围非法: %s" % text)
    length = int(match.group(1))
    offset = match.group(2)
    if offset is None:
        offset = previous_end or 0
    else:
        offset = int(offset)
    return (length, offset)


def _duration_of(text):
    """`#EXTINF:9.009,` → 9.009；无法解析时返回 0.0。"""
    try:
        return float(str(text).split(",", 1)[0].strip())
    except (TypeError, ValueError):
        return 0.0


def _suffix_for(uri, default):
    path = urllib.parse.urlsplit(uri).path or ""
    lower = path.lower()
    for suffix in _MEDIA_SUFFIXES:
        if lower.endswith(suffix):
            return suffix
    return default


def _key_of(attrs, base_url):
    method = (attrs.get("METHOD") or "").upper()
    kind = _ENCRYPTION_KINDS.get(method, "other")
    if kind == "none":
        return None
    if kind in ("sample-aes", "drm"):
        raise HlsError(
            "DRM_UNSUPPORTED",
            u"该视频使用了 DRM 或 SAMPLE-AES 加密（%s），本功能不提供解密" % (
                method or "UNKNOWN"))
    if kind != "aes-128":
        raise HlsError("PLAYLIST_INVALID",
                       u"不支持的加密方式: %s" % (method or "UNKNOWN"))
    uri = attrs.get("URI") or ""
    if not uri:
        raise HlsError("PLAYLIST_INVALID", u"AES-128 播放列表缺少密钥 URI")
    return {
        "method": "AES-128",
        "uri": urllib.parse.urljoin(base_url, uri),
        "iv": attrs.get("IV") or "",
        "keyformat": attrs.get("KEYFORMAT") or "identity",
    }


def parse(text, base_url):
    """解析播放列表文本，返回可 JSON 化的字典。

    kind=master 时返回 variants；kind=media 时返回 segments。
    任何不合规/不支持的情况都抛 HlsError（code 可直接作为 API 错误码）。
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    body = text or ""
    if not is_playlist(body):
        raise HlsError("PLAYLIST_INVALID", u"内容不是 m3u8 播放列表")

    lines = [line.strip() for line in body.replace(u"\r\n", u"\n").split(u"\n")]
    variants = []
    media = []
    segments = []
    has_alt_audio = False
    vod = False
    target_duration = 0
    media_sequence = 0
    current_key = None
    current_map = None
    pending_duration = None
    pending_range = None
    pending_discontinuity = False
    pending_stream_inf = None
    last_end = {}
    is_master = False
    encryption_seen = "none"

    for raw in lines:
        if not raw or raw.startswith(u"#EXTM3U"):
            continue
        if raw.startswith(u"#"):
            if raw.startswith(u"#EXT-X-STREAM-INF:"):
                is_master = True
                pending_stream_inf = parse_attributes(raw.split(u":", 1)[1])
            elif raw.startswith(u"#EXT-X-MEDIA:"):
                attrs = parse_attributes(raw.split(u":", 1)[1])
                if (attrs.get("TYPE") or "").upper() == "AUDIO":
                    has_alt_audio = True
                    media.append({
                        "type": "AUDIO",
                        "group_id": attrs.get("GROUP-ID") or "",
                        "uri": urllib.parse.urljoin(
                            base_url, attrs.get("URI") or ""),
                        "name": attrs.get("NAME") or "",
                        "language": attrs.get("LANGUAGE") or "",
                        "default": (attrs.get("DEFAULT") or "").upper() == "YES",
                        "autoselect":
                            (attrs.get("AUTOSELECT") or "").upper() == "YES",
                    })
            elif raw.startswith(u"#EXT-X-TARGETDURATION:"):
                try:
                    target_duration = int(float(raw.split(u":", 1)[1]))
                except (TypeError, ValueError):
                    target_duration = 0
            elif raw.startswith(u"#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    media_sequence = int(raw.split(u":", 1)[1])
                except (TypeError, ValueError):
                    media_sequence = 0
            elif raw.startswith(u"#EXT-X-ENDLIST"):
                vod = True
            elif raw.startswith(u"#EXT-X-PLAYLIST-TYPE:"):
                if "VOD" in raw.upper():
                    vod = True
            elif raw.startswith(u"#EXTINF:"):
                pending_duration = _duration_of(raw.split(u":", 1)[1])
            elif raw.startswith(u"#EXT-X-BYTERANGE:"):
                pending_range = raw.split(u":", 1)[1]
            elif raw.startswith(u"#EXT-X-DISCONTINUITY"):
                pending_discontinuity = True
            elif raw.startswith(u"#EXT-X-KEY:"):
                current_key = _key_of(parse_attributes(raw.split(u":", 1)[1]),
                                      base_url)
                if current_key:
                    encryption_seen = "aes-128"
            elif raw.startswith(u"#EXT-X-MAP:"):
                attrs = parse_attributes(raw.split(u":", 1)[1])
                uri = attrs.get("URI") or ""
                if not uri:
                    raise HlsError("PLAYLIST_INVALID",
                                   u"#EXT-X-MAP 缺少 URI")
                byte_range = None
                if attrs.get("BYTERANGE"):
                    byte_range = _split_byte_range(attrs["BYTERANGE"], 0)
                current_map = {
                    "uri": urllib.parse.urljoin(base_url, uri),
                    "byte_range": byte_range,
                    "key": current_key,
                }
            elif raw.startswith(u"#EXT-X-SESSION-KEY:"):
                attrs = parse_attributes(raw.split(u":", 1)[1])
                _key_of(attrs, base_url)  # 仅做 DRM 校验
            continue

        # 非 # 开头 = URI 行
        if pending_stream_inf is not None:
            attrs = pending_stream_inf
            pending_stream_inf = None
            width = height = 0
            resolution = attrs.get("RESOLUTION") or ""
            if "x" in resolution.lower():
                try:
                    width, height = [int(part) for part in
                                     resolution.lower().split("x", 1)]
                except (TypeError, ValueError):
                    width = height = 0
            try:
                bandwidth = int(attrs.get("BANDWIDTH") or 0)
            except (TypeError, ValueError):
                bandwidth = 0
            variants.append({
                "uri": urllib.parse.urljoin(base_url, raw),
                "bandwidth": bandwidth,
                "width": width,
                "height": height,
                "codecs": attrs.get("CODECS") or "",
                "frame_rate": attrs.get("FRAME-RATE") or "",
                "audio": attrs.get("AUDIO") or "",
                "name": attrs.get("NAME") or "",
            })
            continue

        uri = urllib.parse.urljoin(base_url, raw)
        byte_range = None
        if pending_range is not None:
            byte_range = _split_byte_range(pending_range,
                                           last_end.get(uri))
            last_end[uri] = byte_range[0] + byte_range[1]
            pending_range = None
        segments.append({
            "uri": uri,
            "duration": pending_duration or 0.0,
            "sequence": media_sequence + len(segments),
            "discontinuity": bool(pending_discontinuity),
            "byte_range": byte_range,
            "key": current_key,
            "map": current_map,
        })
        pending_duration = None
        pending_discontinuity = False

    if is_master:
        if not variants:
            raise HlsError("PLAYLIST_INVALID", u"主播放列表里没有可用清晰度")
        return {
            "kind": "master",
            "url": base_url,
            "vod": True,
            "variants": variants,
            "media": media,
            "segments": [],
            "duration": 0.0,
            "encryption": encryption_seen,
            "has_alt_audio": has_alt_audio,
            "target_duration": target_duration,
        }

    if not segments:
        raise HlsError("PLAYLIST_INVALID", u"播放列表里没有分片")
    if not vod:
        # 没有 ENDLIST 也没有 VOD 类型：直播/滚动列表，明确拒绝。
        raise HlsError("LIVE_UNSUPPORTED",
                       u"该播放列表是直播或滚动列表（缺少 #EXT-X-ENDLIST），"
                       u"仅支持点播视频")
    duration = 0.0
    for segment in segments:
        duration += segment["duration"]
    if target_duration <= 0:
        longest = max([segment["duration"] for segment in segments] or [0.0])
        target_duration = int(math.ceil(longest)) or 1
    return {
        "kind": "media",
        "url": base_url,
        "vod": True,
        "variants": [],
        "segments": segments,
        "duration": duration,
        "encryption": encryption_seen,
        "has_alt_audio": has_alt_audio,
        "target_duration": target_duration,
    }


def sorted_variants(playlist):
    """按清晰度（高度）→ 码率排序，最好在前。"""
    def key(variant):
        return (int(variant.get("height") or 0),
                int(variant.get("bandwidth") or 0))
    return sorted(list(playlist.get("variants") or []), key=key, reverse=True)


def pick_variant(playlist, max_height=0):
    """从主播放列表挑一个清晰度：默认最高；max_height>0 时不超过它。"""
    variants = sorted_variants(playlist)
    if not variants:
        raise HlsError("PLAYLIST_INVALID", u"主播放列表里没有可用清晰度")
    if max_height and int(max_height) > 0:
        allowed = [item for item in variants
                   if int(item.get("height") or 0) <= int(max_height)]
        # 全部超限时退到最低的一档，避免"选不到就失败"。
        variants = allowed or [variants[-1]]
    return variants[0]


def pick_audio_group(playlist, variant):
    """挑出与所选清晰度配套的独立音轨（EXT-X-MEDIA TYPE=AUDIO）。

    没有独立音轨时返回 None。多个候选取 default=YES 优先，其次第一个。
    """
    items = [item for item in (playlist.get("media") or [])
             if (item.get("type") or "").upper() == "AUDIO"
             and item.get("uri")]
    if not items:
        return None
    group = (variant or {}).get("audio") or ""
    if group:
        items = [item for item in items
                 if (item.get("group_id") or "") == group] or items
    preferred = [item for item in items if item.get("default")]
    return (preferred or items)[0]


def variant_label(variant):
    """用于 UI/日志的清晰度标签：1080p / 720p / 未知。"""
    if not isinstance(variant, dict):
        return u"未知"
    height = int(variant.get("height") or 0)
    if height > 0:
        return u"%dp" % height
    bandwidth = int(variant.get("bandwidth") or 0)
    if bandwidth > 0:
        return u"%dkbps" % max(1, bandwidth // 1000)
    return u"未知"


def plan_local_files(playlist):
    """给分片 / init / 密钥分配本地文件名（纯计算，不落盘）。

    返回 dict：
      names     {"segments": [name...]}
      fetches   待下载条目 [{kind, uri, name, byte_range, key, iv, map}]
      maps      URI → 本地文件名
      keys      URI → {"name", "iv"}（同一 URI 只下一次）
    """
    segments = list(playlist.get("segments") or [])
    if not segments:
        raise HlsError("PLAYLIST_INVALID", u"没有可下载的分片")
    names = []
    fetches = []
    maps = {}
    keys = {}
    for index, segment in enumerate(segments):
        uri = segment.get("uri") or ""
        suffix = _suffix_for(uri, ".ts")
        name = "seg_%06d%s" % (index, suffix)
        names.append(name)

        key = segment.get("key")
        key_name = None
        if key:
            key_uri = key.get("uri") or ""
            entry = keys.get(key_uri)
            if entry is None:
                entry = {
                    "name": "key_%04d.bin" % len(keys),
                    "iv": key.get("iv") or "",
                }
                keys[key_uri] = entry
                fetches.append({
                    "kind": "key",
                    "uri": key_uri,
                    "name": entry["name"],
                    "byte_range": None,
                    "key": None,
                    "map": None,
                })
            key_name = entry["name"]

        map_entry = segment.get("map")
        map_name = None
        if map_entry:
            map_uri = map_entry.get("uri") or ""
            map_name = maps.get(map_uri)
            if map_name is None:
                map_name = "init_%04d%s" % (
                    len(maps), _suffix_for(map_uri, _INIT_SUFFIX_DEFAULT))
                maps[map_uri] = map_name
                fetches.append({
                    "kind": "map",
                    "uri": map_uri,
                    "name": map_name,
                    "byte_range": map_entry.get("byte_range"),
                    "key": None,
                    "map": None,
                })
        fetches.append({
            "kind": "segment",
            "uri": uri,
            "name": name,
            "byte_range": segment.get("byte_range"),
            "key": ({"uri": key.get("uri"), "name": key_name,
                     "iv": key.get("iv") or ""} if key else None),
            "map": map_name,
            "discontinuity": bool(segment.get("discontinuity")),
            "duration": segment.get("duration") or 0.0,
        })
    return {
        "names": {"segments": names},
        "fetches": fetches,
        "maps": maps,
        "keys": keys,
    }


def build_local_playlist(playlist, plan):
    """把播放列表重写成只引用本地文件（供 ffmpeg -allowed_extensions ALL）。"""
    if not isinstance(plan, dict):
        raise HlsError("PLAYLIST_INVALID", u"本地文件计划缺失")
    segments = list(playlist.get("segments") or [])
    fetches = list(plan.get("fetches") or [])
    seg_fetches = [item for item in fetches if item.get("kind") == "segment"]
    if len(seg_fetches) != len(segments):
        raise HlsError("PLAYLIST_INVALID", u"分片与本地文件数量不一致")

    target = int(playlist.get("target_duration") or 0)
    if target <= 0:
        longest = max([float(item.get("duration") or 0.0)
                       for item in seg_fetches] or [0.0])
        target = int(math.ceil(longest)) or 1

    lines = [u"#EXTM3U", u"#EXT-X-VERSION:3",
             u"#EXT-X-TARGETDURATION:%d" % target,
             u"#EXT-X-MEDIA-SEQUENCE:0",
             u"#EXT-X-PLAYLIST-TYPE:VOD"]

    last_key = None
    last_map = None
    for index, fetch in enumerate(seg_fetches):
        key = fetch.get("key")
        key_token = (key or {}).get("name")
        if key_token != last_key:
            if key:
                attrs = [u'METHOD=AES-128', u'URI="%s"' % key.get("name")]
                if key.get("iv"):
                    attrs.append(u"IV=%s" % key.get("iv"))
                lines.append(u"#EXT-X-KEY:" + u",".join(attrs))
            else:
                lines.append(u"#EXT-X-KEY:METHOD=NONE")
            last_key = key_token
        map_name = fetch.get("map")
        if map_name and map_name != last_map:
            lines.append(u'#EXT-X-MAP:URI="%s"' % map_name)
            last_map = map_name
        if fetch.get("discontinuity"):
            lines.append(u"#EXT-X-DISCONTINUITY")
        duration = float(fetch.get("duration") or 0.0)
        lines.append(u"#EXTINF:%.3f," % duration)
        lines.append(str(fetch.get("name") or segments[index].get("uri")))
    lines.append(u"#EXT-X-ENDLIST")
    return u"\n".join(lines) + u"\n"


def segment_count(playlist):
    return len(list(playlist.get("segments") or []))


def encryption_label(playlist):
    kind = (playlist or {}).get("encryption") or "none"
    return {
        "none": u"未加密",
        "aes-128": u"AES-128",
        "sample-aes": u"SAMPLE-AES（不支持）",
        "drm": u"DRM（不支持）",
    }.get(kind, u"未知")
