# -*- coding: utf-8 -*-
"""内容相似度：特征提取、LSH 候选、安全团分组、持久化特征缓存。

算法（docs/SIMILARITY_CONTRACT.md §3，binding）：
- text/document：文本层提取 → 规范化 → 字符 5-gram MinHash（64 × 32-bit，确定性）。
- image：PNG 纯标准库解码 + dHash；JPEG/WebP 优先 Pillow，无 Pillow 时降级为
  字节 MinHash 并标记 low_confidence=true（不新增运行依赖）。
- bytes：仅 similarity_allow_binary=true 时启用，1KiB 分片 5-gram MinHash。
- 相似度：MinHash → equal_positions/64；pHash → 1 - hamming/64；整数百分比比较。
- LSH：MinHash 16 bands × 4 rows；pHash 8×8bit 分带。
- 分组：安全团（组内任意两两相似度 >= 阈值），贪心按分数降序合并。

安全：特征只用于缩小候选与人工复核，绝不作为删除依据；删除前仍逐文件重校验。
"""

from __future__ import absolute_import

import hashlib
import html
import io
import json
import logging
import os
import re
import struct
import threading
import zipfile
import zlib
from collections import OrderedDict

from . import config

logger = logging.getLogger("dupcleaner.similarity")

# 可选 Pillow：只用于 JPEG/WebP 解码；缺失时自动降级，不新增运行依赖。
try:  # pragma: no cover - 依运行环境
    from PIL import Image  # type: ignore
    PILLOW_AVAILABLE = True
except Exception:  # pragma: no cover
    Image = None
    PILLOW_AVAILABLE = False

_MASK32 = 0xFFFFFFFF
_SIGNATURE_SIZE = config.SIMILARITY_SIGNATURE_SIZE
_BANDS = config.SIMILARITY_MINHASH_BANDS
_ROWS = config.SIMILARITY_MINHASH_ROWS
_MAX_SHINGLES = config.SIMILARITY_MAX_SHINGLES
_MIN_TEXT_CHARS = 16
_MIN_BYTES_FEATURE = 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# 确定性置换常数：不同进程/机器结果一致（不能用 Python 内置 hash 的随机盐）
def _build_permutations():
    mult = []
    add = []
    for index in range(_SIGNATURE_SIZE):
        digest = hashlib.sha256(("dc-sim-perm-%d" % index).encode("ascii")).digest()
        mult.append((int.from_bytes(digest[0:4], "little") | 1) & _MASK32)
        add.append(int.from_bytes(digest[4:8], "little") & _MASK32)
    return tuple(mult), tuple(add)


_MULT, _ADD = _build_permutations()


class SimilaritySkip(Exception):
    """该文件不参与相似计算。reason: type/size/binary/decode/no_text/error"""

    def __init__(self, reason, detail=""):
        super(SimilaritySkip, self).__init__(reason + (": " + detail if detail else ""))
        self.reason = reason
        self.detail = detail


def image_backend():
    return "pillow" if PILLOW_AVAILABLE else "stdlib-png"


def capabilities():
    return {
        "default_extensions": list(config.DEFAULT_SIMILARITY_EXTENSIONS),
        "binary_extensions": sorted(config.SIMILARITY_BINARY_EXTENSIONS),
        "image_backend": image_backend(),
        "pillow_available": bool(PILLOW_AVAILABLE),
        "default_threshold": 85,
        "signature_size": _SIGNATURE_SIZE,
        "cache": config.SIMILARITY_CACHE_FILENAME,
    }


# ---------------------------------------------------------------------------
# 类型判定
# ---------------------------------------------------------------------------

def classify_extension(extension, options):
    """返回 (kind, skip_reason, detail)。kind: text/document/image/bytes。"""
    ext = (extension or "").lower()
    if not ext:
        return None, "type", u"无扩展名"
    if ext in config.SIMILARITY_BINARY_EXTENSIONS:
        # 压缩包/镜像/可执行/媒体：无论 allow_binary 都只做精准哈希
        return None, "binary", u"二进制/压缩包/媒体类别不参与相似计算"
    allowed = [item.lower() for item in (options.get("similarity_extensions") or [])]
    excluded = [item.lower() for item in
                (options.get("similarity_exclude_extensions") or [])]
    if not allowed:
        allowed = [item.lower() for item in config.DEFAULT_SIMILARITY_EXTENSIONS]
    if ext in excluded:
        return None, "type", u"该后缀被显式排除"
    if ext not in allowed:
        return None, "type", u"该后缀不在相似计算白名单内"
    if ext in config.SIMILARITY_TEXT_EXTENSIONS:
        return "text", None, ""
    if ext in config.SIMILARITY_DOCUMENT_EXTENSIONS:
        return "document", None, ""
    if ext in config.SIMILARITY_IMAGE_EXTENSIONS:
        return "image", None, ""
    if options.get("similarity_allow_binary"):
        return "bytes", None, ""
    return None, "type", u"未知类型默认不参与相似计算"


# ---------------------------------------------------------------------------
# 文本 / 文档提取
# ---------------------------------------------------------------------------

def _decode_text(data):
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)


def normalize_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RE.sub(" ", text).strip().lower()
    return text


def _pdf_unescape(raw):
    out = bytearray()
    index = 0
    while index < len(raw):
        byte = raw[index]
        if byte == 0x5C and index + 1 < len(raw):
            nxt = raw[index + 1]
            mapping = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8,
                       ord("f"): 12, ord("("): 40, ord(")"): 41, ord("\\"): 92}
            if nxt in mapping:
                out.append(mapping[nxt])
                index += 2
                continue
            if 48 <= nxt <= 55:
                digits = bytes([nxt])
                index += 2
                for _ in range(2):
                    if index < len(raw) and 48 <= raw[index] <= 55:
                        digits += bytes([raw[index]])
                        index += 1
                out.append(int(digits, 8) & 0xFF)
                continue
            index += 2
            continue
        out.append(byte)
        index += 1
    return bytes(out)


_PDF_STRING_RE = re.compile(rb"\((?:\\.|[^\\()])*\)", re.DOTALL)


def _pdf_stream_text(stream):
    texts = []
    for match in _PDF_STRING_RE.finditer(stream):
        literal = match.group(0)[1:-1]
        if not literal:
            continue
        texts.append(_pdf_unescape(literal).decode("latin-1", "replace"))
    return " ".join(texts)


def extract_pdf_text(data):
    """PDF 文本层提取（stdlib）：解 FlateDecode 流并抓取字符串字面量。"""
    if not data.startswith(b"%PDF"):
        raise SimilaritySkip("decode", u"不是 PDF")
    chunks = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.DOTALL):
        raw = match.group(1)
        decoded = None
        try:
            decoded = zlib.decompress(raw)
        except zlib.error:
            try:
                decoded = zlib.decompressobj().decompress(raw)
            except zlib.error:
                decoded = None
        if decoded is None:
            continue
        chunks.append(_pdf_stream_text(decoded))
    text = " ".join(chunks).strip()
    return text


def _zip_document_text(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise SimilaritySkip("decode", u"docx/odt 解析失败: %s" % exc)
    try:
        names = archive.namelist()
        target = None
        for candidate in ("word/document.xml", "content.xml", "word/document2.xml"):
            if candidate in names:
                target = candidate
                break
        if target is None:
            raise SimilaritySkip("no_text", u"文档内没有文本层")
        raw = archive.read(target)
    finally:
        try:
            archive.close()
        except Exception:
            pass
    try:
        xml_text = raw.decode("utf-8", "replace")
    except Exception:
        xml_text = raw.decode("latin-1", "replace")
    xml_text = re.sub(r"<[^>]+>", " ", xml_text)
    xml_text = html.unescape(xml_text)
    return xml_text


# ---------------------------------------------------------------------------
# 图片：PNG 纯标准库解码 + dHash；Pillow 可选
# ---------------------------------------------------------------------------

def decode_png_gray(data):
    """解码非隔行 PNG 为灰度列表（0..255）。失败抛 SimilaritySkip(decode)。"""
    if not data.startswith(_PNG_SIGNATURE):
        raise SimilaritySkip("decode", u"不是 PNG")
    position = 8
    width = height = bit_depth = color_type = None
    interlace = 0
    idat = bytearray()
    palette = None
    while position + 8 <= len(data):
        length = struct.unpack(">I", data[position:position + 4])[0]
        chunk_type = data[position + 4:position + 8]
        chunk_data = data[position + 8:position + 8 + length]
        position += 12 + length
        if chunk_type == b"IHDR":
            if len(chunk_data) < 13:
                raise SimilaritySkip("decode", u"IHDR 损坏")
            (width, height, bit_depth, color_type, compression,
             filter_method, interlace) = struct.unpack(">IIBBBBB", chunk_data[:13])
            if compression != 0 or filter_method != 0:
                raise SimilaritySkip("decode", u"不支持的 PNG 压缩/过滤方式")
        elif chunk_type == b"PLTE":
            palette = chunk_data
        elif chunk_type == b"IDAT":
            idat += chunk_data
        elif chunk_type == b"IEND":
            break
    if width is None or height is None or width <= 0 or height <= 0:
        raise SimilaritySkip("decode", u"PNG 缺少 IHDR")
    if interlace != 0:
        raise SimilaritySkip("decode", u"不支持隔行 PNG（降级字节特征）")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise SimilaritySkip("decode", u"不支持的 PNG 颜色类型")
    if bit_depth not in (8, 16):
        raise SimilaritySkip("decode", u"不支持的 PNG 位深")
    if not idat:
        raise SimilaritySkip("decode", u"PNG 缺少图像数据")
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise SimilaritySkip("decode", u"PNG 解压失败: %s" % exc)
    bytes_per_sample = bit_depth // 8
    bytes_per_pixel = channels * bytes_per_sample
    stride = width * bytes_per_pixel
    expected = height * (stride + 1)
    if len(raw) < expected:
        raise SimilaritySkip("decode", u"PNG 数据不完整")
    gray = []
    previous = bytearray(stride)
    offset = 0
    for _row in range(height):
        filter_type = raw[offset]
        offset += 1
        line = bytearray(raw[offset:offset + stride])
        offset += stride
        if filter_type == 1:
            for index in range(bytes_per_pixel, stride):
                line[index] = (line[index] + line[index - bytes_per_pixel]) & 0xFF
        elif filter_type == 2:
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 0xFF
        elif filter_type == 3:
            for index in range(stride):
                left = line[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif filter_type == 4:
            for index in range(stride):
                left = line[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                up = previous[index]
                up_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                p = left + up - up_left
                pa = abs(p - left)
                pb = abs(p - up)
                pc = abs(p - up_left)
                if pa <= pb and pa <= pc:
                    predictor = left
                elif pb <= pc:
                    predictor = up
                else:
                    predictor = up_left
                line[index] = (line[index] + predictor) & 0xFF
        elif filter_type != 0:
            raise SimilaritySkip("decode", u"PNG 过滤类型非法")
        previous = line

        def sample(channel_index):
            base = channel_index * bytes_per_sample
            if bytes_per_sample == 2:
                return line[base]
            return line[base]

        if color_type == 3:
            for x in range(width):
                palette_index = line[x]
                if palette is None or palette_index * 3 + 2 >= len(palette):
                    raise SimilaritySkip("decode", u"PNG 调色板损坏")
                r = palette[palette_index * 3]
                g = palette[palette_index * 3 + 1]
                b = palette[palette_index * 3 + 2]
                gray.append((r * 299 + g * 587 + b * 114) // 1000)
        elif color_type in (0, 4):
            for x in range(width):
                gray.append(sample(x * channels))
        else:
            for x in range(width):
                base = x * channels
                r = line[base]
                g = line[base + bytes_per_sample]
                b = line[base + 2 * bytes_per_sample]
                gray.append((r * 299 + g * 587 + b * 114) // 1000)
    return width, height, gray


def dhash_from_gray(width, height, gray):
    if width < 2 or height < 1:
        raise SimilaritySkip("decode", u"图片过小")
    bits = 0
    for row in range(8):
        y = min(height - 1, int((row + 0.5) * height / 8.0))
        values = []
        for column in range(9):
            x = min(width - 1, int((column + 0.5) * width / 9.0))
            values.append(gray[y * width + x])
        for column in range(8):
            bits = (bits << 1) | (1 if values[column] > values[column + 1] else 0)
    return bits & 0xFFFFFFFFFFFFFFFF


def _dhash_from_pillow(data):
    image = Image.open(io.BytesIO(data))  # type: ignore
    image = image.convert("L").resize((9, 8))
    pixels = list(image.getdata())
    gray = [int(value) for value in pixels]
    return dhash_from_gray(9, 8, gray)


# ---------------------------------------------------------------------------
# MinHash
# ---------------------------------------------------------------------------

def _sample_step(total, max_items):
    if total <= max_items or max_items <= 0:
        return 1
    return (total + max_items - 1) // max_items


def text_shingles(normalized_text):
    data = normalized_text.encode("utf-8", "replace")
    if len(data) < 5:
        return []
    count = len(data) - 4
    step = _sample_step(count, _MAX_SHINGLES)
    return [zlib.crc32(data[index:index + 5]) & _MASK32
            for index in range(0, count, step)]


def bytes_shingles(data):
    """1KiB 分片 → 连续 5 个分片组合的确定性 32-bit 值。"""
    if len(data) < _MIN_BYTES_FEATURE:
        return []
    chunk_size = config.SIMILARITY_CHUNK_BYTES
    chunks = [zlib.crc32(data[index:index + chunk_size]) & _MASK32
              for index in range(0, len(data), chunk_size)]
    if len(chunks) < 5:
        return []
    count = len(chunks) - 4
    step = _sample_step(count, _MAX_SHINGLES)
    out = []
    for index in range(0, count, step):
        value = (chunks[index] * 0x9E3779B1) & _MASK32
        value = (value ^ (chunks[index + 1] * 0x85EBCA77)) & _MASK32
        value = (value ^ (chunks[index + 2] * 0xC2B2AE3D)) & _MASK32
        value = (value ^ (chunks[index + 3] * 0x27D4EB2F)) & _MASK32
        value = (value ^ (chunks[index + 4] * 0x165667B1)) & _MASK32
        out.append(value & _MASK32)
    return out


def minhash_signature(shingles):
    if not shingles:
        return []
    signature = [_MASK32] * _SIGNATURE_SIZE
    mult = _MULT
    add = _ADD
    size = _SIGNATURE_SIZE
    for shingle in shingles:
        for index in range(size):
            value = (shingle * mult[index] + add[index]) & _MASK32
            if value < signature[index]:
                signature[index] = value
    return signature


def minhash_similarity_percent(signature_a, signature_b):
    if not signature_a or not signature_b:
        return 0
    if len(signature_a) != len(signature_b):
        return 0
    equal = 0
    for left, right in zip(signature_a, signature_b):
        if left == right:
            equal += 1
    return int(round(equal * 100.0 / len(signature_a)))


def phash_similarity_percent(hash_a, hash_b):
    hamming = bin((hash_a ^ hash_b) & 0xFFFFFFFFFFFFFFFF).count("1")
    return int(round((64 - hamming) * 100.0 / 64.0))


def comparison_family(feature):
    if feature.get("method") == "phash":
        return "phash"
    if feature.get("kind") == "image":
        # 图片降级字节特征只与同类图片字节特征比较
        return "image-bytes"
    return "minhash"


def pair_similarity_percent(feature_a, feature_b):
    family = comparison_family(feature_a)
    if family != comparison_family(feature_b):
        return 0
    if family == "phash":
        return phash_similarity_percent(feature_a["phash"], feature_b["phash"])
    return minhash_similarity_percent(feature_a.get("signature"),
                                      feature_b.get("signature"))


# ---------------------------------------------------------------------------
# 特征提取入口
# ---------------------------------------------------------------------------

def _feature_from_kind(kind, data, extension):
    with_text = None
    low_confidence = False
    if kind == "text":
        with_text = _decode_text(data)
    elif kind == "document":
        ext = extension.lower()
        if ext == ".pdf":
            with_text = extract_pdf_text(data)
        else:
            with_text = _zip_document_text(data)
    elif kind == "image":
        ext = extension.lower()
        if ext == ".png":
            try:
                width, height, gray = decode_png_gray(data)
                return {
                    "kind": "image",
                    "method": "phash",
                    "phash": dhash_from_gray(width, height, gray),
                    "low_confidence": False,
                }
            except SimilaritySkip as exc:
                logger.debug("PNG 解码失败，降级字节特征: %s", exc)
                low_confidence = True
                degraded = True
                with_text = None
                kind_effective = "bytes"
                feature = _bytes_feature(data)
                feature["low_confidence"] = True
                feature["degraded"] = "decode"
                return feature
        elif PILLOW_AVAILABLE:
            try:
                return {
                    "kind": "image",
                    "method": "phash",
                    "phash": _dhash_from_pillow(data),
                    "low_confidence": False,
                }
            except Exception:
                low_confidence = True
                feature = _bytes_feature(data)
                feature["low_confidence"] = True
                feature["degraded"] = "decode"
                return feature
        else:
            feature = _bytes_feature(data)
            feature["low_confidence"] = True
            feature["degraded"] = "decode"
            return feature
    elif kind == "bytes":
        return _bytes_feature(data)

    normalized = normalize_text(with_text or "")
    if len(normalized) < _MIN_TEXT_CHARS:
        raise SimilaritySkip("no_text", u"没有可用文本层")
    signature = minhash_signature(text_shingles(normalized))
    if not signature:
        raise SimilaritySkip("no_text", u"文本过短，无法生成特征")
    return {
        "kind": kind,
        "method": "minhash",
        "signature": signature,
        "low_confidence": False,
    }


def _bytes_feature(data):
    signature = minhash_signature(bytes_shingles(data))
    if not signature:
        raise SimilaritySkip("size", u"文件过小，无法生成字节特征")
    return {
        "kind": "bytes",
        "method": "bytes",
        "signature": signature,
        "low_confidence": True,
    }


def read_file_bytes(path, size_limit, limiter=None, chunk_size=1024 * 1024):
    """读取文件（受 size_limit 约束），同时计算 content_sha256。"""
    hasher = hashlib.sha256()
    chunks = []
    total = 0
    with open(path, "rb", buffering=0) as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if total > size_limit:
                raise SimilaritySkip("size", u"文件超过相似计算大小上限")
            if limiter is not None:
                limiter.acquire(len(chunk))
            hasher.update(chunk)
            chunks.append(chunk)
    return b"".join(chunks), hasher.hexdigest(), total


def compute_feature(path, kind, extension, options, limiter=None):
    """读取文件并提取特征，返回 feature dict（含 content_sha256）。"""
    max_mb = int(options.get("similarity_max_file_mb") or 32)
    size_limit = max_mb * 1024 * 1024
    data, content_sha256, total = read_file_bytes(path, size_limit, limiter)
    feature = _feature_from_kind(kind, data, extension)
    feature = dict(feature)
    feature["content_sha256"] = content_sha256
    feature["size"] = total
    return feature


# ---------------------------------------------------------------------------
# LSH 候选 + 安全团分组
# ---------------------------------------------------------------------------

_MAX_BUCKET_ITEMS = 200


def build_groups(features, threshold):
    """对特征列表做 LSH 候选 + 安全团分组。

    返回 (clusters, stats)：
      clusters: [{"indices": [int...], "scores": {(i,j): percent}, "method": str}]
    """
    threshold = max(0, min(100, int(threshold)))
    buckets = {}
    for index, feature in enumerate(features):
        family = comparison_family(feature)
        if family == "phash":
            value = int(feature.get("phash") or 0)
            for band in range(8):
                key = ("p", band, (value >> (band * 8)) & 0xFF)
                buckets.setdefault(key, []).append(index)
        else:
            signature = feature.get("signature") or []
            if len(signature) < _BANDS * _ROWS:
                continue
            for band in range(_BANDS):
                start = band * _ROWS
                key = (family, band, tuple(signature[start:start + _ROWS]))
                buckets.setdefault(key, []).append(index)

    candidate_pairs = set()
    for key, items in buckets.items():
        if len(items) < 2:
            continue
        if len(items) > _MAX_BUCKET_ITEMS:
            items = items[:_MAX_BUCKET_ITEMS]
        for left in range(len(items)):
            for right in range(left + 1, len(items)):
                first, second = items[left], items[right]
                if first == second:
                    continue
                if first > second:
                    first, second = second, first
                candidate_pairs.add((first, second))

    scores = {}
    for first, second in candidate_pairs:
        score = pair_similarity_percent(features[first], features[second])
        if score >= threshold:
            scores[(first, second)] = score

    groups = []
    group_of = {}

    def score_of(first, second):
        if first > second:
            first, second = second, first
        return scores.get((first, second))

    def can_join(group, member):
        for existing in group["members"]:
            score = score_of(existing, member)
            if score is None or score < threshold:
                return False
        return True

    def group_scores(members):
        result = {}
        for left in range(len(members)):
            for right in range(left + 1, len(members)):
                first, second = members[left], members[right]
                result[(first, second)] = score_of(first, second)
        return result

    edges = sorted(scores.items(),
                   key=lambda item: (-item[1], item[0][0], item[0][1]))
    for (first, second), score in edges:
        group_first = group_of.get(first)
        group_second = group_of.get(second)
        if group_first is None and group_second is None:
            group = {"members": [first, second]}
            index = len(groups)
            groups.append(group)
            group_of[first] = index
            group_of[second] = index
            continue
        if group_first is not None and group_second is None:
            group = groups[group_first]
            if can_join(group, second):
                group["members"].append(second)
                group_of[second] = group_first
            continue
        if group_second is not None and group_first is None:
            group = groups[group_second]
            if can_join(group, first):
                group["members"].append(first)
                group_of[first] = group_second
            continue
        if group_first == group_second:
            continue
        left_group = groups[group_first]
        right_group = groups[group_second]
        mergeable = True
        for member in right_group["members"]:
            if not can_join(left_group, member):
                mergeable = False
                break
        if mergeable:
            offset = len(left_group["members"])
            base = group_first
            for member in right_group["members"]:
                left_group["members"].append(member)
                group_of[member] = base
            groups[group_second] = {"members": [], "merged": True}

    clusters = []
    for group in groups:
        members = sorted(group.get("members") or [])
        if len(members) < 2:
            continue
        methods = set()
        for member in members:
            methods.add(features[member].get("method"))
        if "phash" in methods:
            method = "phash"
        elif methods == set(["bytes"]):
            method = "bytes"
        else:
            method = "minhash"
        clusters.append({
            "indices": members,
            "scores": group_scores(members),
            "method": method,
            "similarity_score": int(round(
                sum(group_scores(members).get((members[left], members[right]), 0)
                    for left in range(len(members))
                    for right in range(left + 1, len(members)))
                / max(1, len(members) * (len(members) - 1) / 2.0))),
        })
    clusters.sort(key=lambda item: (-item["similarity_score"],
                                    -len(item["indices"]),
                                    item["indices"][0]))
    return clusters, {"pairs_checked": len(candidate_pairs),
                      "groups": len(clusters)}


# ---------------------------------------------------------------------------
# 持久化特征缓存
# ---------------------------------------------------------------------------

def cache_key(size, mtime_ns, inode, dev, feature_type):
    return "v1|%d|%d|%d|%d|%s" % (int(size), int(mtime_ns), int(inode), int(dev),
                                   feature_type)


class FeatureCache(object):
    """<data>/feature_cache.json（原子写，最大 50k 条，LRU 淘汰）。"""

    def __init__(self, path, max_entries=None, enabled=True):
        self.path = path
        self.max_entries = int(max_entries or config.SIMILARITY_CACHE_MAX_ENTRIES)
        self.enabled = bool(enabled)
        self._lock = threading.RLock()
        self._entries = OrderedDict()
        self._dirty = False
        if self.enabled:
            self._load()

    def _load(self):
        try:
            with open(self.path, "rb") as handle:
                payload = json.loads(handle.read().decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            return
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            return
        with self._lock:
            for key, value in entries.items():
                if isinstance(value, dict):
                    self._entries[str(key)] = value
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def get(self, key):
        if not self.enabled:
            return None
        with self._lock:
            value = self._entries.pop(key, None)
            if value is None:
                return None
            self._entries[key] = value
            return value

    def put(self, key, value):
        if not self.enabled:
            return
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            self._dirty = True

    def save(self):
        if not self.enabled:
            return False
        with self._lock:
            if not self._dirty:
                return False
            payload = {"version": 1, "entries": dict(self._entries)}
            self._dirty = False
        try:
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            temporary = self.path + ".tmp"
            with open(temporary, "wb") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            os.replace(temporary, self.path)
            return True
        except OSError:
            return False

    def __len__(self):
        with self._lock:
            return len(self._entries)
