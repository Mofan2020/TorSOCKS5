"""TSU/1 隧道协议：帧编解码与目标策略。

线格式的唯一真相源是 ``docs/tunnel-protocol.md``。Python 客户端、Python 中继、
Cloudflare Worker、Deno 中继四个实现必须保持一致；改协议先改那份文档。

帧结构::

     0        1        2        3        4        5 ...
    +--------+--------+--------+--------+--------+-----------------+
    | opcode |         stream id (uint32, big endian)   |   payload   |
    +--------+--------+--------+--------+--------+-----------------+
"""

from __future__ import annotations

import ipaddress
import struct
from typing import Iterator, List, Optional, Sequence, Tuple

from ..defaults import DEFAULT_ALLOW_PORTS, LEARNING_ALLOW_HOSTS
from ..hostrules import host_matches, is_private_address, normalize_host

PROTO = "tsu/1"
SUBPROTOCOL = "tsu.v1"
DEFAULT_PATH = "/tsu"
HEALTH_PATH = "/healthz"

# ------------------------------------------------------------------ opcode
OP_OPEN = 0x01
OP_OPEN_OK = 0x02
OP_OPEN_ERR = 0x03
OP_DATA = 0x04
OP_CLOSE = 0x05
OP_RESET = 0x06
OP_PING = 0x07
OP_PONG = 0x08

OP_NAMES = {
    OP_OPEN: "OPEN",
    OP_OPEN_OK: "OPEN_OK",
    OP_OPEN_ERR: "OPEN_ERR",
    OP_DATA: "DATA",
    OP_CLOSE: "CLOSE",
    OP_RESET: "RESET",
    OP_PING: "PING",
    OP_PONG: "PONG",
}

# ------------------------------------------------------------------ 错误码
ERR_NOT_ALLOWED = 0x01
ERR_CONNECT_FAILED = 0x02
ERR_TOO_MANY_STREAMS = 0x03
ERR_BAD_REQUEST = 0x04
ERR_BLOCKED_TARGET = 0x05
ERR_UNAUTHORIZED = 0x06

ERR_NAMES = {
    ERR_NOT_ALLOWED: "NOT_ALLOWED",
    ERR_CONNECT_FAILED: "CONNECT_FAILED",
    ERR_TOO_MANY_STREAMS: "TOO_MANY_STREAMS",
    ERR_BAD_REQUEST: "BAD_REQUEST",
    ERR_BLOCKED_TARGET: "BLOCKED_TARGET",
    ERR_UNAUTHORIZED: "UNAUTHORIZED",
}

# ------------------------------------------------------------------ 帧尺寸
_HEADER = struct.Struct("!BI")
HEADER_SIZE = _HEADER.size          # 5
STREAM_ID_START = 3                 # 1、2 留给协议探测
SEND_CHUNK = 32 * 1024              # 发送分片大小
MAX_SEND_MESSAGE = 64 * 1024        # 单个 WS 消息上限（规范要求）
MAX_RECV_MESSAGE = 1024 * 1024      # 接收上限，超过视为协议错误
MAX_PING_PAYLOAD = 32

# ``DEFAULT_ALLOW_PORTS`` / ``LEARNING_ALLOW_HOSTS`` 定义在 :mod:`torsocks5.defaults`，
# 这里通过上面的 import 直接复用（避免环形导入），并作为协议默认策略对外可见。


class ProtocolError(Exception):
    """帧格式非法。"""


class UnknownOpcode(ProtocolError):
    """未定义的 opcode。

    规范 2.1：接受方**必须**用 ``RESET``（该流）回应；流 id 为 ``0`` 时直接关闭连接。
    因此这里带上 opcode 与 stream id，让上层能按规范处理，而不是笼统地断开。
    """

    def __init__(self, opcode: int, stream_id: int) -> None:
        super().__init__("未知 opcode 0x%02x（流 %d）" % (opcode, stream_id))
        self.opcode = opcode
        self.stream_id = stream_id


# ------------------------------------------------------------------ 编解码
def encode_frame(op: int, stream_id: int, payload: bytes = b"") -> bytes:
    """把 opcode / stream id / payload 编成一个 WS 消息。"""
    if not 0 <= op <= 0xFF:
        raise ProtocolError("opcode 越界: %r" % (op,))
    if not 0 <= stream_id <= 0xFFFFFFFF:
        raise ProtocolError("stream id 越界: %r" % (stream_id,))
    if len(payload) > MAX_SEND_MESSAGE:
        raise ProtocolError("payload 超过 %d 字节上限" % MAX_SEND_MESSAGE)
    return _HEADER.pack(op, stream_id) + payload


def decode_frame(data: bytes) -> Tuple[int, int, bytes]:
    """解析一个 WS 消息，返回 ``(opcode, stream_id, payload)``。

    未定义的 opcode 抛 :class:`UnknownOpcode`（上层按规范回 ``RESET``）；
    结构性非法抛 :class:`ProtocolError`。
    """
    if len(data) < HEADER_SIZE:
        raise ProtocolError("帧长度不足：%d 字节" % len(data))
    op, stream_id = _HEADER.unpack_from(data, 0)
    if op not in OP_NAMES:
        raise UnknownOpcode(op, stream_id)
    return op, stream_id, data[HEADER_SIZE:]


def encode_address(host: str, port: int) -> bytes:
    """编码 ``atyp + addr + port``，编号与 SOCKS5 对齐。"""
    if not 0 < port <= 0xFFFF:
        raise ProtocolError("端口非法: %r" % (port,))
    text = normalize_host(host)
    if not text:
        raise ProtocolError("目标主机为空")
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        raw = text.encode("idna") if any(ord(char) > 127 for char in text) else text.encode("ascii", "strict")
        if not raw or len(raw) > 255:
            raise ProtocolError("域名长度非法: %r" % (host,)) from None
        return bytes([0x03, len(raw)]) + raw + struct.pack("!H", port)
    if addr.version == 4:
        return bytes([0x01]) + addr.packed + struct.pack("!H", port)
    return bytes([0x04]) + addr.packed + struct.pack("!H", port)


def decode_address(payload: bytes) -> Tuple[str, int]:
    """解析 ``OPEN`` 的 payload，返回 ``(host, port)``；多余字节视为非法。"""
    if not payload:
        raise ProtocolError("地址缺失")
    atyp = payload[0]
    rest = payload[1:]
    if atyp == 0x01:
        if len(rest) != 6:
            raise ProtocolError("IPv4 地址长度非法")
        host = str(ipaddress.ip_address(rest[:4]))
        tail = rest[4:]
    elif atyp == 0x04:
        if len(rest) != 18:
            raise ProtocolError("IPv6 地址长度非法")
        host = str(ipaddress.ip_address(rest[:16]))
        tail = rest[16:]
    elif atyp == 0x03:
        if not rest:
            raise ProtocolError("域名长度缺失")
        length = rest[0]
        raw = rest[1 : 1 + length]
        if len(raw) != length:
            raise ProtocolError("域名不完整")
        host = raw.decode("idna") if any(byte > 127 for byte in raw) else raw.decode("ascii")
        tail = rest[1 + length :]
    else:
        raise ProtocolError("未知地址类型 0x%02x" % atyp)
    if len(tail) != 2:
        raise ProtocolError("端口缺失或存在多余字节")
    return host, struct.unpack("!H", tail)[0]


def encode_error(code: int, message: str = "") -> bytes:
    """``OPEN_ERR`` 的 payload。"""
    return bytes([code & 0xFF]) + (message or "").encode("utf-8", "replace")


def decode_error(payload: bytes) -> Tuple[int, str]:
    if not payload:
        raise ProtocolError("OPEN_ERR 缺少错误码")
    return payload[0], payload[1:].decode("utf-8", "replace")


def frame_name(op: int) -> str:
    return OP_NAMES.get(op, "0x%02x" % op)


def error_name(code: int) -> str:
    return ERR_NAMES.get(code, "0x%02x" % code)


# ------------------------------------------------------------------ 目标策略
def is_blocked_target(host: str) -> bool:
    """目标是私有 / 回环 / 链路本地 / 保留地址时返回 True。"""
    return is_private_address(host)


def target_policy(
    host: str,
    port: int,
    allow_hosts: Optional[Sequence[str]] = None,
    allow_ports: Optional[Sequence[int]] = None,
    allow_all: bool = False,
) -> Optional[int]:
    """判定目标是否放行，返回 ``None`` 表示放行，否则返回 ``OPEN_ERR`` 错误码。

    中继侧和客户端预检共用这一份策略，避免出现「客户端以为能过、中继却拒绝」。
    顺序：地址合法性 → 私有地址（任何情况下都拦）→ ``allow_all`` → 端口 → 白名单。
    """
    if not host or not 0 < port <= 0xFFFF:
        return ERR_BAD_REQUEST
    if is_blocked_target(host):
        return ERR_BLOCKED_TARGET
    if allow_all:
        return None
    ports: Sequence[int] = DEFAULT_ALLOW_PORTS if allow_ports is None else allow_ports
    if ports and port not in ports:
        return ERR_NOT_ALLOWED
    hosts: Sequence[str] = LEARNING_ALLOW_HOSTS if allow_hosts is None else allow_hosts
    if host_matches(host, hosts):
        return None
    return ERR_NOT_ALLOWED


def chunk_payload(data: bytes, limit: int = SEND_CHUNK) -> Iterator[bytes]:
    """把写入拆成不超过 ``limit`` 的分片。"""
    if limit <= 0:
        raise ValueError("limit 必须为正数")
    for start in range(0, len(data), limit):
        yield data[start : start + limit]


def describe_targets(hosts: Sequence[str], limit: int = 8) -> str:
    """给日志用的目标摘要（只显示数量与前几项，避免刷屏）。"""
    items: List[str] = list(hosts)
    head = ", ".join(items[:limit])
    return head + (" 等 %d 项" % len(items) if len(items) > limit else "")
