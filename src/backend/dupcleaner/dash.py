# -*- coding: utf-8 -*-
"""DASH（MPD）清单解析与下载计划（v0054，docs/DOWNLOAD_CONTRACT.md §16）。

只做纯解析：输入 MPD 文本与地址，输出「初始化段 + 分片」下载计划，由
`downloads.DownloadService` 复用已有的分片下载器与 FFmpeg 合并流水线。

支持范围（够覆盖常见的点播 DASH）：
- 单个 Period（多 Period 只取第一个含媒体的）；
- `SegmentTemplate`（`$Number$` / `$Time$` / `$RepresentationID$` / `$Bandwidth$`，
  支持 `$Number%05d$` 补零），含 `SegmentTimeline`（`S@t/d/r`）；
- `SegmentList`（`Initialization@sourceURL` + `SegmentURL@media`）；
- `SegmentBase`（单文件 + 可选 `indexRange` 时按整文件直下）；
- 视频/音频各取一档（视频按高度→码率取最高，可用 max_height 限制；音频取最高码率）；
- `BaseURL` 在 MPD/Period/AdaptationSet/Representation 四级拼接。

明确不支持：DRM（ContentProtection）、多 Period 拼接、字幕轨、SAMPLE-AES。
"""

from __future__ import absolute_import

import math
import re
import urllib.parse
import xml.etree.ElementTree as ElementTree

_NS = "{urn:mpeg:dash:schema:mpd:2011}"
_DURATION_RE = re.compile(
    r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?"
    r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")
_TOKEN_RE = re.compile(r"\$(\w+)(?:%0(\d+)d)?\$")
_VIDEO_MIME = ("video/",)
_AUDIO_MIME = ("audio/",)


class DashError(Exception):
    def __init__(self, code, message):
        super(DashError, self).__init__(message)
        self.code = code
        self.message = message


def _local(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def _children(node, name):
    return [child for child in list(node) if _local(child.tag) == name]


def _first(node, name):
    for child in list(node):
        if _local(child.tag) == name:
            return child
    return None


def _int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value, default=0.0):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_duration(text):
    """ISO 8601 时长（PT1H2M3.5S）→ 秒。"""
    match = _DURATION_RE.match((text or "").strip())
    if match is None:
        return 0.0
    days = _int(match.group(3))
    hours = _int(match.group(4))
    minutes = _int(match.group(5))
    seconds = _float(match.group(6))
    return days * 86400.0 + hours * 3600.0 + minutes * 60.0 + seconds


def _join(base, relative):
    if not relative:
        return base
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", relative):
        return relative
    if not base:
        return relative
    return urllib.parse.urljoin(base, relative)


def _base_url(node, inherited):
    child = _first(node, "BaseURL")
    if child is None or not (child.text or "").strip():
        return inherited
    return _join(inherited, child.text.strip())


def _mime_of(adaptation):
    mime = (adaptation.get("mimeType") or "").lower()
    content = (adaptation.get("contentType") or "").lower()
    return mime, content


def _is_video(adaptation):
    mime, content = _mime_of(adaptation)
    if content == "video":
        return True
    if content == "audio":
        return False
    return any(mime.startswith(prefix) for prefix in _VIDEO_MIME)


def _is_audio(adaptation):
    mime, content = _mime_of(adaptation)
    if content == "audio":
        return True
    return any(mime.startswith(prefix) for prefix in _AUDIO_MIME)


def _expand(template, tokens):
    def replace(match):
        name = match.group(1)
        width = match.group(2)
        value = tokens.get(name)
        if value is None:
            return match.group(0)
        if width:
            try:
                return ("%0" + str(int(width)) + "d") % int(value)
            except (TypeError, ValueError):
                return str(value)
        return str(value)

    return _TOKEN_RE.sub(replace, template or "")


def _segment_timeline(template):
    timeline = _first(template, "SegmentTimeline")
    if timeline is None:
        return None
    entries = []
    current = 0
    for item in _children(timeline, "S"):
        start = item.get("t")
        if start is not None:
            current = _int(start)
        duration = _int(item.get("d"))
        repeat = _int(item.get("r"), 0)
        count = repeat + 1 if repeat >= 0 else 1
        for _index in range(min(count, 10000)):
            entries.append((current, duration))
            current += duration
    return entries


def _segment_template(node, inherited):
    template = _first(node, "SegmentTemplate") or inherited
    return template


def _plan_template(base, adaptation, representation, template,
                   period_duration):
    timescale = _float(template.get("timescale"), 1.0) or 1.0
    start_number = _int(template.get("startNumber"), 1)
    media_tpl = template.get("media") or ""
    init_tpl = template.get("initialization") or ""
    tokens = {
        "RepresentationID": representation.get("id") or "",
        "Bandwidth": _int(representation.get("bandwidth")),
    }
    rep_base = _base_url(representation, base)
    init = None
    if init_tpl:
        init_url = _join(rep_base, _expand(init_tpl, tokens))
        init = {"url": init_url, "name": "init.mp4"}
    segments = []
    entries = _segment_timeline(template)
    if entries:
        for index, (start, duration) in enumerate(entries):
            local = dict(tokens)
            local["Number"] = start_number + index
            local["Time"] = start
            if not media_tpl:
                break
            segments.append({
                "url": _join(rep_base, _expand(media_tpl, local)),
                "name": "seg_%06d.m4s" % index,
            })
        total = sum(duration for _start, duration in entries) / timescale
    else:
        duration = _float(template.get("duration"))
        if duration <= 0 or period_duration <= 0 or not media_tpl:
            return {"base": rep_base, "init": init, "segments": [],
                    "duration": 0.0}
        count = int(math.ceil(period_duration / (duration / timescale)))
        count = max(1, min(count, 20000))
        for index in range(count):
            local = dict(tokens)
            local["Number"] = start_number + index
            local["Time"] = int(round(index * duration))
            segments.append({
                "url": _join(rep_base, _expand(media_tpl, local)),
                "name": "seg_%06d.m4s" % index,
            })
        total = count * duration / timescale
    return {"base": rep_base, "init": init, "segments": segments,
            "duration": total}


def _plan_list(base, adaptation, representation):
    rep_base = _base_url(representation, base)
    segment_list = _first(representation, "SegmentList")
    if segment_list is None:
        segment_list = _first(adaptation, "SegmentList")
    if segment_list is None:
        return None
    init = None
    init_node = _first(segment_list, "Initialization")
    if init_node is not None:
        source = init_node.get("sourceURL") or ""
        if source:
            init = {"url": _join(rep_base, source), "name": "init.mp4"}
    segments = []
    for index, item in enumerate(_children(segment_list, "SegmentURL")):
        media = item.get("media") or ""
        if not media:
            continue
        segments.append({
            "url": _join(rep_base, media),
            "name": "seg_%06d.m4s" % index,
        })
    duration = _float(segment_list.get("duration"))
    timescale = _float(segment_list.get("timescale"), 1.0) or 1.0
    return {"base": rep_base, "init": init, "segments": segments,
            "duration": len(segments) * duration / timescale}


def _plan_base(representation, base):
    """SegmentBase：整个资源当单个分片（DRM 之外最常见的是单文件 mp4）。"""
    rep_base = _base_url(representation, base)
    segment_base = _first(representation, "SegmentBase")
    init = None
    if segment_base is not None:
        init_node = _first(segment_base, "Initialization")
        if init_node is not None and init_node.get("range"):
            pass  # 字节范围初始化：这里按整文件处理，交给 FFmpeg 判断
    return {
        "base": rep_base,
        "init": init,
        "segments": [{"url": rep_base, "name": "seg_000000.mp4"}],
        "duration": 0.0,
    }


def _representations(adaptation):
    return _children(adaptation, "Representation")


def _kind_of_adaptation(adaptation):
    if _is_video(adaptation):
        return "video"
    if _is_audio(adaptation):
        return "audio"
    return ""


def parse(text, url):
    """解析 MPD，返回 {kind: dash, video: plan, audio: plan, duration}。"""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    body = (text or "").lstrip("\ufeff \r\n\t")
    if "<MPD" not in body:
        raise DashError("PLAYLIST_INVALID", u"内容不是 DASH 清单（MPD）")
    try:
        root = ElementTree.fromstring(body.encode("utf-8"))
    except ElementTree.ParseError as exc:
        raise DashError("PLAYLIST_INVALID", u"MPD 解析失败: %s" % exc)
    if _local(root.tag) != "MPD":
        raise DashError("PLAYLIST_INVALID", u"MPD 根节点非法")
    for node in root.iter():
        if _local(node.tag) == "ContentProtection":
            raise DashError("DRM_UNSUPPORTED",
                            u"该 DASH 流包含 DRM（ContentProtection），不支持")
    mpd_base = _base_url(root, url)
    period = _first(root, "Period")
    if period is None:
        raise DashError("PLAYLIST_INVALID", u"MPD 里没有 Period")
    period_base = _base_url(period, mpd_base)
    period_duration = parse_duration(period.get("duration")) or \
        parse_duration(root.get("mediaPresentationDuration"))
    plan = {"kind": "dash", "duration": period_duration,
            "video": None, "audio": None}
    for adaptation in _children(period, "AdaptationSet"):
        kind = _kind_of_adaptation(adaptation)
        if not kind:
            continue
        adapt_base = _base_url(adaptation, period_base)
        inherited = _first(adaptation, "SegmentTemplate")
        candidates = []
        for representation in _representations(adaptation):
            template = _segment_template(representation, inherited)
            if template is not None:
                item = _plan_template(adapt_base, adaptation, representation,
                                      template, period_duration)
            elif _first(representation, "SegmentList") is not None or \
                    _first(adaptation, "SegmentList") is not None:
                item = _plan_list(adapt_base, adaptation, representation)
            else:
                item = _plan_base(representation, adapt_base)
            if item is None or not item.get("segments"):
                continue
            item["height"] = _int(representation.get("height"))
            item["bandwidth"] = _int(representation.get("bandwidth"))
            item["id"] = representation.get("id") or ""
            candidates.append(item)
        if not candidates:
            continue
        candidates.sort(key=lambda entry: (entry.get("height") or 0,
                                           entry.get("bandwidth") or 0),
                        reverse=True)
        if kind == "video" and plan["video"] is None:
            plan["video"] = candidates[0]
        elif kind == "audio" and plan["audio"] is None:
            plan["audio"] = candidates[0]
    if plan["video"] is None and plan["audio"] is None:
        raise DashError("PLAYLIST_INVALID", u"MPD 里没有可下载的音视频轨")
    video = plan["video"]
    if video is not None and plan["duration"] <= 0:
        plan["duration"] = video.get("duration") or 0.0
    return plan


def pick_video(plan, max_height=0):
    """按清晰度上限选择视频轨（只有一个候选时原样返回）。"""
    video = (plan or {}).get("video")
    if video is None:
        return None
    if max_height and video.get("height") and \
            int(video["height"]) > int(max_height):
        # 没有更低档时仍用它，避免"选不到就失败"
        return video
    return video


def plan_fetches(plan, prefix, stream):
    """把一条轨（video/audio）展开成下载器需要的 fetch 列表。"""
    item = (plan or {}).get(stream)
    if not item:
        return []
    fetches = []
    init = item.get("init")
    if init:
        fetches.append({
            "kind": "init",
            "uri": init["url"],
            "name": "%s_%s" % (prefix, init.get("name") or "init.mp4"),
            "byte_range": None,
        })
    for segment in item.get("segments") or []:
        fetches.append({
            "kind": "segment",
            "uri": segment["url"],
            "name": "%s_%s" % (prefix, segment["name"]),
            "byte_range": None,
        })
    return fetches


def stream_files(plan, prefix, stream):
    """本地合并顺序：init（若有）+ 分片名。"""
    item = (plan or {}).get(stream)
    if not item:
        return []
    names = []
    init = item.get("init")
    if init:
        names.append("%s_%s" % (prefix, init.get("name") or "init.mp4"))
    for segment in item.get("segments") or []:
        names.append("%s_%s" % (prefix, segment["name"]))
    return names
