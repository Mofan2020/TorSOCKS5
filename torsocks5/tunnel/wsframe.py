"""RFC 6455 底层：握手报文、帧编解码、掩码。

客户端（``wsclient``）与服务端（``wsserver``）共用这一份实现，也便于单元测试：
帧解析器是**增量式**的，喂多少字节解析多少帧，不需要一次性拿到完整报文。
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
from typing import Dict, List, Optional, Tuple

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

CONTROL_OPS = (OP_CLOSE, OP_PING, OP_PONG)

#: 单个 WS 消息的接收上限，超过即视为协议错误（与隧道规范一致）
DEFAULT_MAX_MESSAGE = 1024 * 1024
MAX_CONTROL_PAYLOAD = 125
MAX_HTTP_HEADER = 64 * 1024


class WebSocketError(Exception):
    """WebSocket 层错误。"""


class HandshakeError(WebSocketError):
    """握手失败（状态码非 101、Accept 不匹配等）。"""


def websocket_accept(key: str) -> str:
    """由 ``Sec-WebSocket-Key`` 计算 ``Sec-WebSocket-Accept``。"""
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def new_key() -> str:
    """生成一个随机的 ``Sec-WebSocket-Key``。"""
    return base64.b64encode(os.urandom(16)).decode("ascii")


def mask_payload(data: bytes, key: bytes) -> bytes:
    """用 4 字节掩码异或数据。

    走 ``int.from_bytes`` 走 C 实现，比逐字节 Python 循环快一个数量级——
    代理路径上每 32KB 就要掩码一次，这个开销不能忽略。
    """
    if not data:
        return b""
    if len(key) != 4:
        raise WebSocketError("掩码必须是 4 字节")
    length = len(data)
    mask = key * (length // 4) + key[: length % 4]
    masked = int.from_bytes(data, "big") ^ int.from_bytes(mask, "big")
    return masked.to_bytes(length, "big")


def encode_frame(opcode: int, payload: bytes = b"", *, fin: bool = True,
                 mask: bool = True) -> bytes:
    """编一个 WS 帧；``mask=True`` 时自动生成掩码键（客户端必须掩码）。"""
    if opcode in CONTROL_OPS:
        if not fin:
            raise WebSocketError("控制帧不能分片")
        if len(payload) > MAX_CONTROL_PAYLOAD:
            raise WebSocketError("控制帧 payload 不能超过 125 字节")
    first = (0x80 if fin else 0x00) | (opcode & 0x0F)
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", first, (0x80 if mask else 0x00) | length)
    elif length <= 0xFFFF:
        header = struct.pack("!BBH", first, (0x80 if mask else 0x00) | 126, length)
    else:
        header = struct.pack("!BBQ", first, (0x80 if mask else 0x00) | 127, length)
    if not mask:
        return header + payload
    key = os.urandom(4)
    return header + key + mask_payload(payload, key)


def build_handshake_request(
    host: str,
    path: str,
    key: str,
    *,
    port: Optional[int] = None,
    subprotocol: str = "",
    headers: Optional[Dict[str, str]] = None,
    user_agent: str = "TorSOCKS5",
) -> bytes:
    """构造握手请求（HTTP/1.1 Upgrade）。"""
    host_header = host if port in (None, 80, 443) else "%s:%d" % (host, port or 0)
    lines: List[str] = [
        "GET %s HTTP/1.1" % path,
        "Host: %s" % host_header,
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: %s" % key,
        "Sec-WebSocket-Version: 13",
        "User-Agent: %s" % user_agent,
    ]
    if subprotocol:
        lines.append("Sec-WebSocket-Protocol: %s" % subprotocol)
    for name, value in (headers or {}).items():
        lines.append("%s: %s" % (name, value))
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


def parse_http_response(data: bytes) -> Tuple[int, Dict[str, str], int]:
    """解析 HTTP 响应头，返回 ``(状态码, 头字段, 已消费字节数)``。

    只解析头部：``data`` 里剩下的字节原样交回给调用者（握手后可能紧跟 WS 帧）。
    """
    end = data.find(b"\r\n\r\n")
    if end < 0:
        if len(data) > MAX_HTTP_HEADER:
            raise HandshakeError("响应头超过 %d 字节仍未结束" % MAX_HTTP_HEADER)
        raise HandshakeError("响应头不完整")
    head = data[:end].decode("latin-1")
    lines = head.split("\r\n")
    status_line = lines[0].split(" ")
    if len(status_line) < 2 or not status_line[1].isdigit():
        raise HandshakeError("状态行非法: %r" % lines[0])
    status = int(status_line[1])
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if not separator:
            continue
        key = name.strip().lower()
        value = value.strip()
        if key in headers:
            headers[key] = headers[key] + ", " + value
        else:
            headers[key] = value
    return status, headers, end + 4


def check_handshake(status: int, headers: Dict[str, str], key: str,
                    subprotocol: str = "", *, strict_subprotocol: bool = False) -> Optional[str]:
    """校验握手响应，返回警告文本（``None`` 表示无警告），失败抛 ``HandshakeError``。"""
    if status != 101:
        raise HandshakeError("中继返回 HTTP %d，预期 101" % status)
    if "websocket" not in headers.get("upgrade", "").lower():
        raise HandshakeError("响应缺少 Upgrade: websocket")
    if "upgrade" not in headers.get("connection", "").lower():
        raise HandshakeError("响应缺少 Connection: Upgrade")
    accept = headers.get("sec-websocket-accept", "")
    if accept != websocket_accept(key):
        raise HandshakeError("Sec-WebSocket-Accept 不匹配（可能不是 WebSocket 端点）")
    echo = headers.get("sec-websocket-protocol", "")
    if subprotocol:
        if echo and echo.strip() != subprotocol:
            raise HandshakeError("中继回显的子协议是 %r，预期 %r" % (echo, subprotocol))
        if not echo:
            if strict_subprotocol:
                raise HandshakeError("中继没有回显子协议 %r" % subprotocol)
            return "中继没有回显子协议 %s（继续，但对方可能不是本项目的实现）" % subprotocol
    return None


class FrameParser:
    """增量帧解析器：``feed()`` 喂字节，``next_message()`` 取完整消息。

    数据帧会自动拼装分片，只返回**完整消息**（``(opcode, payload)``）；
    控制帧（PING/PONG/CLOSE）立即返回，不受分片影响。
    """

    def __init__(self, *, max_message: int = DEFAULT_MAX_MESSAGE,
                 require_mask: bool = False) -> None:
        self._buf = bytearray()
        self.max_message = max_message
        self.require_mask = require_mask
        self._frag_op: Optional[int] = None
        self._frag = bytearray()

    def feed(self, data: bytes) -> None:
        if data:
            self._buf.extend(data)

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def next_message(self) -> Optional[Tuple[int, bytes]]:
        """返回下一个完整消息，数据不足时返回 ``None``。"""
        while True:
            if len(self._buf) < 2:
                return None
            first, second = self._buf[0], self._buf[1]
            fin = bool(first & 0x80)
            if first & 0x70:
                raise WebSocketError("RSV 位非零（未协商扩展）")
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            offset = 2
            if length == 126:
                if len(self._buf) < offset + 2:
                    return None
                length = struct.unpack_from("!H", self._buf, offset)[0]
                offset += 2
            elif length == 127:
                if len(self._buf) < offset + 8:
                    return None
                length = struct.unpack_from("!Q", self._buf, offset)[0]
                offset += 8
            if masked:
                if len(self._buf) < offset + 4:
                    return None
                mask_key = bytes(self._buf[offset : offset + 4])
                offset += 4
            else:
                mask_key = None
            if length > self.max_message:
                raise WebSocketError("WS 消息 %d 字节，超过上限 %d" % (length, self.max_message))
            if len(self._buf) < offset + length:
                return None
            payload = bytes(self._buf[offset : offset + length])
            del self._buf[: offset + length]
            if mask_key is not None:
                payload = mask_payload(payload, mask_key)
            elif self.require_mask:
                raise WebSocketError("客户端帧必须带掩码")

            if opcode in CONTROL_OPS:
                if not fin:
                    raise WebSocketError("控制帧不能分片")
                return opcode, payload
            if opcode == OP_CONTINUATION:
                if self._frag_op is None:
                    raise WebSocketError("收到没有起始帧的续帧")
                self._frag.extend(payload)
                if len(self._frag) > self.max_message:
                    raise WebSocketError("分片消息超过上限 %d" % self.max_message)
                if fin:
                    message = (self._frag_op, bytes(self._frag))
                    self._frag_op = None
                    self._frag = bytearray()
                    return message
                continue
            # TEXT / BINARY
            if self._frag_op is not None:
                raise WebSocketError("上一个分片消息尚未结束")
            if fin:
                return opcode, payload
            self._frag_op = opcode
            self._frag = bytearray(payload)


def close_payload(code: int = 1000, reason: str = "") -> bytes:
    return struct.pack("!H", code) + reason.encode("utf-8", "replace")


def parse_close_payload(payload: bytes) -> Tuple[int, str]:
    if len(payload) < 2:
        return 1005, ""
    return struct.unpack_from("!H", payload, 0)[0], payload[2:].decode("utf-8", "replace")
