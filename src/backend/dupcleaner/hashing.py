# -*- coding: utf-8 -*-
"""哈希计算：快速采样指纹 + 全量分片哈希 + 全局限速。

哈希计算点（重要）：
- 快速指纹：文件大小 + 头部 sample_kb + 尾部 sample_kb；大文件（默认
  >= 256 MiB）额外采样中间段，避免仅在中间不同的文件被误判为同一指纹。
  用于缩小候选集，不能作为“重复”的最终依据。
- 全量哈希：按 HASH_CHUNK_SIZE 分片读取，支持进度回调、取消回调与全局限速，
  最终判定重复必须使用全量哈希。
"""

from __future__ import absolute_import

import hashlib
import os
import struct
import threading
import time

from . import config
from .errors import ApiError

_HASHERS = {
    "sha256": hashlib.sha256,
    "md5": hashlib.md5,
}

# 快速指纹版本：v1 = 大小 + 头 + 尾；v2 = 大文件额外采样中间段。
# 摘要输入以 b"dcq2" 开头，版本变化会让不同版本的指纹自然区分开。
QUICK_SAMPLE_VERSION = 2
# 达到该大小（字节）的文件，快速指纹额外采样中间段。
LARGE_FILE_SAMPLE_THRESHOLD = 256 * 1024 * 1024

_QUICK_MAGIC = b"dcq2"


class HashCancelled(Exception):
    """哈希过程被暂停/停止/失败打断。"""


def hasher_factory(algo):
    factory = _HASHERS.get(algo)
    if factory is None:
        raise ApiError("INVALID_ARGUMENT", u"不支持的哈希算法: %s" % algo)
    return factory


def hash_bytes(data, algo="sha256"):
    hasher = hasher_factory(algo)()
    hasher.update(data)
    return hasher.hexdigest()


def quick_sample_hash(path, algo="sha256", sample_kb=64, size=None,
                      large_threshold=None):
    """快速指纹（哈希计算点）：大小 + 头 + 尾（大文件再加中间段）。

    采样规则（确定性、自描述）：
    - sample = sample_kb * 1024 字节；
    - 头：文件开头 min(sample, size) 字节；
    - 尾：从 max(len(head), size - sample) 起读 sample 字节，与头不重叠；
    - 中间：size >= (large_threshold or LARGE_FILE_SAMPLE_THRESHOLD) 且头/中/尾
      三段放得下时，offset = clamp(size // 2 - sample // 2, len(head),
      size - len(tail) - sample)，读 sample 字节。

    摘要输入：b"dcq2" + struct.pack("<Q", size) + b"h" + head
    + (b"m" + mid) + b"t" + tail。

    size 不为 None 时直接使用，避免调用方重复 os.stat。
    读文件失败按原样抛 OSError / IOError（调用方已有处理）。
    """
    factory = hasher_factory(algo)
    sample = max(0, int(sample_kb)) * 1024
    if size is None:
        size = int(os.stat(path).st_size)
    else:
        size = int(size)
    threshold = large_threshold or LARGE_FILE_SAMPLE_THRESHOLD

    with open(path, "rb") as handle:
        head = handle.read(min(sample, size)) if sample > 0 else b""
        tail = b""
        if size > len(head):
            tail_start = max(len(head), size - sample)
            handle.seek(tail_start)
            tail = handle.read(sample)
        mid = None
        if sample > 0 and size >= int(threshold):
            upper = size - len(tail) - sample
            lower = len(head)
            if upper >= lower:
                offset = size // 2 - sample // 2
                if offset < lower:
                    offset = lower
                elif offset > upper:
                    offset = upper
                handle.seek(offset)
                mid = handle.read(sample)

    hasher = factory()
    hasher.update(_QUICK_MAGIC)
    hasher.update(struct.pack("<Q", size))
    hasher.update(b"h")
    hasher.update(head)
    if mid is not None:
        hasher.update(b"m")
        hasher.update(mid)
    hasher.update(b"t")
    hasher.update(tail)
    return hasher.hexdigest()


def full_hash_file(path, algo="sha256", chunk_size=config.HASH_CHUNK_SIZE,
                   on_progress=None, should_stop=None, limiter=None):
    """全量哈希（哈希计算点），返回 (hexdigest, bytes_read)。"""
    factory = hasher_factory(algo)
    hasher = factory()
    total = 0
    chunk_size = max(1, int(chunk_size))
    with open(path, "rb", buffering=0) as handle:
        while True:
            if should_stop is not None and should_stop():
                raise HashCancelled()
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            if limiter is not None:
                limiter.acquire(len(chunk))
            hasher.update(chunk)
            total += len(chunk)
            if on_progress is not None:
                on_progress(total)
    return hasher.hexdigest(), total


class TokenBucketRateLimiter(object):
    """全局令牌桶限速器（线程安全，支持可注入时钟便于测试）。"""

    def __init__(self, rate_mbps, clock=None, sleep=None):
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._rate_mbps = max(0, int(rate_mbps))
        self._rate = float(self._rate_mbps) * 1024 * 1024
        self._capacity = max(self._rate, 1.0)
        self._tokens = self._capacity
        self._last = self._clock()

    @property
    def rate_mbps(self):
        return self._rate_mbps

    @property
    def rate_bytes_per_sec(self):
        return self._rate

    @property
    def enabled(self):
        return self._rate > 0

    def set_rate(self, rate_mbps):
        with self._lock:
            self._rate_mbps = max(0, int(rate_mbps))
            self._rate = float(self._rate_mbps) * 1024 * 1024
            self._capacity = max(self._rate, 1.0)
            self._tokens = min(self._tokens, self._capacity)
            self._last = self._clock()

    def acquire(self, nbytes):
        """申请 nbytes 字节配额，必要时 sleep；返回累计 sleep 秒数。"""
        if self._rate <= 0 or nbytes <= 0:
            return 0.0
        remaining = float(nbytes)
        slept = 0.0
        while remaining > 0.000001:
            wait = 0.0
            with self._lock:
                now = self._clock()
                elapsed = now - self._last
                if elapsed < 0:
                    elapsed = 0.0
                self._last = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                take = min(remaining, self._tokens)
                self._tokens -= take
                remaining -= take
                if remaining > 0.000001:
                    wait = min(remaining / self._rate, 0.25)
            if remaining > 0.000001:
                self._sleep(wait)
                slept += wait
        return slept


_rate_lock = threading.Lock()
_global_limiter = TokenBucketRateLimiter(0)


def configure_global_rate(rate_mbps):
    """配置全局限速（同一时间只有一个扫描，因此全局限速即可）。"""
    global _global_limiter
    with _rate_lock:
        _global_limiter.set_rate(rate_mbps)
        return _global_limiter


def get_global_limiter():
    with _rate_lock:
        return _global_limiter
