"""中继服务端增强：令牌桶限流、连接数限制、IP 黑白名单、访问日志。"""

from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple


# --------------------------------------------------------------------- 限流
class TokenBucket:
    """令牌桶：平滑限流，允许受控的突发。"""

    def __init__(self, rate: float, burst: Optional[float] = None, now: Optional[float] = None):
        """``rate`` 为每秒令牌数（<=0 表示不限流）；``burst`` 为桶容量，默认等于 rate。"""
        self.rate = float(rate)
        self.capacity = float(burst) if burst is not None else max(1.0, float(rate))
        self._tokens = self.capacity
        self._updated = now if now is not None else time.monotonic()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.rate > 0

    def try_acquire(self, n: float = 1.0, now: Optional[float] = None) -> bool:
        if self.rate <= 0:
            return True
        current = now if now is not None else time.monotonic()
        with self._lock:
            elapsed = max(0.0, current - self._updated)
            self._updated = current
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def retry_after(self, n: float = 1.0) -> float:
        """还需要等多少秒才能拿到令牌。"""
        if self.rate <= 0:
            return 0.0
        with self._lock:
            missing = n - self._tokens
            if missing <= 0:
                return 0.0
            return missing / self.rate


class RateLimiter:
    """三层限流：全局 / 单客户端 IP / 单令牌。"""

    def __init__(
        self,
        global_rps: float = 0.0,
        global_burst: Optional[float] = None,
        per_ip_rps: float = 0.0,
        per_token_rps: float = 0.0,
        max_tracked: int = 4096,
    ) -> None:
        self.global_bucket = TokenBucket(global_rps, global_burst)
        self.per_ip_rps = float(per_ip_rps)
        self.per_token_rps = float(per_token_rps)
        self.max_tracked = max_tracked
        self._ip_buckets: Dict[str, TokenBucket] = {}
        self._token_buckets: Dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self.rejected = 0

    @property
    def enabled(self) -> bool:
        return self.global_bucket.enabled or self.per_ip_rps > 0 or self.per_token_rps > 0

    def check(self, ip: str, token_key: str = "") -> Tuple[bool, float]:
        """请求一次额度。返回 ``(allowed, retry_after_seconds)``。"""
        if self.global_bucket.enabled and not self.global_bucket.try_acquire():
            self.rejected += 1
            return False, self.global_bucket.retry_after()

        if self.per_ip_rps > 0:
            bucket = self._get_bucket(self._ip_buckets, ip, self.per_ip_rps)
            if bucket is not None and not bucket.try_acquire():
                self.rejected += 1
                return False, bucket.retry_after()

        if self.per_token_rps > 0 and token_key:
            bucket = self._get_bucket(self._token_buckets, token_key, self.per_token_rps)
            if bucket is not None and not bucket.try_acquire():
                self.rejected += 1
                return False, bucket.retry_after()

        return True, 0.0

    def _get_bucket(self, store: Dict[str, TokenBucket], key: str, rate: float) -> Optional[TokenBucket]:
        bucket = store.get(key)
        if bucket is not None:
            return bucket
        with self._lock:
            if key not in store:
                if len(store) >= self.max_tracked:
                    # 简单淘汰：清掉一半最旧的（按插入序）
                    for stale in list(store)[: self.max_tracked // 2]:
                        store.pop(stale, None)
                store[key] = TokenBucket(rate)
            return store.get(key)


# --------------------------------------------------------------------- 连接数限制
class ConnectionLimiter:
    """单 IP / 单令牌 的并发连接数限制。"""

    def __init__(self, max_per_ip: int = 0, max_per_token: int = 0, max_total: int = 0) -> None:
        self.max_per_ip = int(max_per_ip)
        self.max_per_token = int(max_per_token)
        self.max_total = int(max_total)
        self._per_ip: Dict[str, int] = {}
        self._per_token: Dict[str, int] = {}
        self._total = 0
        self._lock = threading.Lock()
        self.rejected = 0

    @property
    def enabled(self) -> bool:
        return self.max_per_ip > 0 or self.max_per_token > 0 or self.max_total > 0

    def acquire(self, ip: str, token_key: str = "") -> bool:
        with self._lock:
            if self.max_total > 0 and self._total >= self.max_total:
                self.rejected += 1
                return False
            if self.max_per_ip > 0 and self._per_ip.get(ip, 0) >= self.max_per_ip:
                self.rejected += 1
                return False
            if self.max_per_token > 0 and token_key and self._per_token.get(token_key, 0) >= self.max_per_token:
                self.rejected += 1
                return False
            self._total += 1
            self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
            if token_key:
                self._per_token[token_key] = self._per_token.get(token_key, 0) + 1
            return True

    def release(self, ip: str, token_key: str = "") -> None:
        with self._lock:
            self._total = max(0, self._total - 1)
            if ip in self._per_ip:
                self._per_ip[ip] -= 1
                if self._per_ip[ip] <= 0:
                    del self._per_ip[ip]
            if token_key and token_key in self._per_token:
                self._per_token[token_key] -= 1
                if self._per_token[token_key] <= 0:
                    del self._per_token[token_key]

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "total": self._total,
                "tracked_ips": len(self._per_ip),
                "tracked_tokens": len(self._per_token),
                "rejected": self.rejected,
            }


# --------------------------------------------------------------------- IP 过滤
def parse_ip_rules(values: Optional[List[str]]) -> Tuple[List[str], List[str]]:
    """解析 CIDR/IP 规则列表。返回 ``(whitelist, blacklist)``，非法项跳过。"""
    whitelist: List[str] = []
    blacklist: List[str] = []
    for raw in values or []:
        text = str(raw).strip()
        if not text:
            continue
        deny = text.startswith("!")
        if deny:
            text = text[1:].strip()
        try:
            ipaddress.ip_network(text, strict=False)
        except ValueError:
            continue
        (blacklist if deny else whitelist).append(text)
    return whitelist, blacklist


class IPFilter:
    """IP 黑白名单（支持 CIDR）。白名单非空时只放行白名单；黑名单永远拒绝。"""

    def __init__(self, whitelist: Optional[List[str]] = None, blacklist: Optional[List[str]] = None) -> None:
        self.whitelist = [self._net(w) for w in (whitelist or [])]
        self.blacklist = [self._net(b) for b in (blacklist or [])]

    @staticmethod
    def _net(cidr: str):
        try:
            return ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            return None

    @property
    def enabled(self) -> bool:
        return bool(self.whitelist) or bool(self.blacklist)

    def allowed(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return not self.whitelist  # 解析不了：白名单开启时拒绝
        for net in self.blacklist:
            if net is not None and addr in net:
                return False
        if self.whitelist:
            return any(net is not None and addr in net for net in self.whitelist)
        return True


# --------------------------------------------------------------------- 熔断
class RelayCircuitBreaker:
    """中继侧熔断：连续错误过多时短暂停止出站，避免拖垮上游与自身线程。"""

    def __init__(self, error_threshold: int = 20, window_seconds: float = 10.0,
                 recovery_seconds: float = 30.0) -> None:
        self.error_threshold = int(error_threshold)
        self.window_seconds = float(window_seconds)
        self.recovery_seconds = float(recovery_seconds)
        self._errors: Deque[float] = deque()
        self._opened_at: float = 0.0
        self._lock = threading.Lock()
        self.trips = 0

    @property
    def enabled(self) -> bool:
        return self.error_threshold > 0

    @property
    def open_(self) -> bool:
        with self._lock:
            if self._opened_at <= 0:
                return False
            if time.monotonic() - self._opened_at >= self.recovery_seconds:
                # 半开：清空错误窗口重新观察
                self._opened_at = 0.0
                self._errors.clear()
                return False
            return True

    def record_error(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            now = time.monotonic()
            self._errors.append(now)
            while self._errors and now - self._errors[0] > self.window_seconds:
                self._errors.popleft()
            if self._opened_at <= 0 and len(self._errors) >= self.error_threshold:
                self._opened_at = now
                self.trips += 1
                self._errors.clear()

    def record_success(self) -> None:
        pass  # 窗口自然过期


# --------------------------------------------------------------------- 访问日志
class AccessLogger:
    """结构化访问日志（JSON Lines），默认不记录目标域名。"""

    def __init__(
        self,
        path: str = "",
        fmt: str = "json",
        log_targets: bool = False,
        max_size_mb: int = 100,
        max_files: int = 5,
    ) -> None:
        self.path = os.path.expanduser(path) if path else ""
        self.fmt = fmt
        self.log_targets = bool(log_targets)
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.max_files = max_files
        self._file: Optional[Any] = None
        self._size = 0
        self._lock = threading.Lock()
        if self.path:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def record(self, event: Dict[str, object]) -> None:
        """记录一条访问事件。``event`` 里不应包含未脱敏的敏感字段。"""
        if not self.enabled:
            return
        event = dict(event)
        event.setdefault("ts", datetime.now().isoformat(timespec="milliseconds"))
        if not self.log_targets:
            event.pop("target", None)
        if self.fmt == "json":
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        else:
            line = " ".join("%s=%s" % (k, v) for k, v in event.items())
        with self._lock:
            try:
                if self._file is None:
                    self._file = open(self.path, "a", encoding="utf-8")
                    self._size = os.path.getsize(self.path)
                self._file.write(line + "\n")
                self._file.flush()
                self._size += len(line) + 1
                if self._size >= self.max_size_bytes:
                    self._rotate()
            except OSError:
                pass

    def _rotate(self) -> None:
        if self._file:
            self._file.close()
            self._file = None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base, ext = os.path.splitext(self.path)
        os.rename(self.path, "%s.%s%s" % (base, stamp, ext or ".log"))
        self._size = 0
        # 清理超数的历史文件
        base_dir = os.path.dirname(self.path) or "."
        try:
            entries = [
                (os.path.getmtime(os.path.join(base_dir, f)), os.path.join(base_dir, f))
                for f in os.listdir(base_dir)
                if f.startswith(os.path.basename(self.path) + ".")
            ]
        except OSError:
            return
        entries.sort(reverse=True)
        for _, fpath in entries[self.max_files:]:
            try:
                os.remove(fpath)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None


__all__ = [
    "TokenBucket",
    "RateLimiter",
    "ConnectionLimiter",
    "IPFilter",
    "RelayCircuitBreaker",
    "AccessLogger",
    "parse_ip_rules",
]
