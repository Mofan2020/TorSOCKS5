"""WebSocket 服务端（纯标准库）。

只服务本项目的隧道中继：HTTP/1.1 Upgrade 握手 + 帧收发。客户端帧必须带掩码
（RFC 6455 要求），服务端发出的帧按规范不加掩码。
"""

from __future__ import annotations

import socket
import ssl
import threading
from typing import Callable, Dict, Optional, Tuple

from .protocol import HEALTH_PATH
from .wsframe import (
    OP_BINARY,
    OP_CLOSE,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    FrameParser,
    HandshakeError,
    WebSocketError,
    close_payload,
    encode_frame,
    parse_close_payload,
    websocket_accept,
)

DEFAULT_MAX_MESSAGE = 1024 * 1024
MAX_REQUEST = 64 * 1024

STATUS_TEXT = {
    101: "Switching Protocols",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    426: "Upgrade Required",
    500: "Internal Server Error",
}


class WSRequest:
    """握手请求（只保留需要的字段）。"""

    def __init__(self, method: str, target: str, headers: Dict[str, str], leftover: bytes) -> None:
        self.method = method
        self.target = target
        self.headers = headers
        self.leftover = leftover

    @property
    def path(self) -> str:
        return self.target.split("?", 1)[0]

    @property
    def query(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        if "?" not in self.target:
            return out
        for piece in self.target.split("?", 1)[1].split("&"):
            name, _, value = piece.partition("=")
            if name:
                out[name] = _url_decode(value)
        return out

    def token(self) -> str:
        """从 ``?token=`` 或 ``Authorization: Bearer`` 取令牌。"""
        token = self.query.get("token", "")
        if token:
            return token
        auth = self.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return ""

    def subprotocols(self) -> Tuple[str, ...]:
        raw = self.headers.get("sec-websocket-protocol", "")
        return tuple(piece.strip() for piece in raw.split(",") if piece.strip())


def _url_decode(text: str) -> str:
    from urllib.parse import unquote

    return unquote(text)


def read_http_request(sock: socket.socket, *, timeout: Optional[float] = None) -> WSRequest:
    """读取并解析 HTTP 请求头（只读头部，剩余字节原样带回）。"""
    if timeout is not None:
        sock.settimeout(timeout)
    raw = bytearray()
    while b"\r\n\r\n" not in raw:
        chunk = sock.recv(4096)
        if not chunk:
            raise HandshakeError("连接在握手前关闭")
        raw.extend(chunk)
        if len(raw) > MAX_REQUEST:
            raise HandshakeError("请求头超过 %d 字节" % MAX_REQUEST)
    end = bytes(raw).find(b"\r\n\r\n")
    head = bytes(raw[:end]).decode("latin-1")
    leftover = bytes(raw)[end + 4 :]
    lines = head.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 3:
        raise HandshakeError("请求行非法: %r" % lines[0])
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if not separator:
            continue
        key = name.strip().lower()
        value = value.strip()
        headers[key] = (headers[key] + ", " + value) if key in headers else value
    return WSRequest(parts[0].upper(), parts[1], headers, leftover)


def send_http_response(sock: socket.socket, status: int, headers: Optional[Dict[str, str]] = None,
                       body: bytes = b"") -> None:
    lines = ["HTTP/1.1 %d %s" % (status, STATUS_TEXT.get(status, "Status"))]
    data = dict(headers or {})
    if body:
        data.setdefault("Content-Type", "text/plain; charset=utf-8")
        data["Content-Length"] = str(len(body))
    else:
        data.setdefault("Content-Length", "0")
    data.setdefault("Connection", "close")
    for name, value in data.items():
        lines.append("%s: %s" % (name, value))
    try:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body)
    except OSError:
        pass


def accept_handshake(sock: socket.socket, request: WSRequest, *,
                     subprotocol: str = "", max_message: int = DEFAULT_MAX_MESSAGE,
                     on_log: Optional[Callable[[str], None]] = None) -> "WSConnection":
    """回应 101 并返回 WS 连接对象。"""
    key = request.headers.get("sec-websocket-key", "")
    if not key:
        raise HandshakeError("缺少 Sec-WebSocket-Key")
    headers = {
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Accept": websocket_accept(key),
    }
    if subprotocol:
        offered = request.subprotocols()
        if subprotocol in offered:
            headers["Sec-WebSocket-Protocol"] = subprotocol
    send_http_response(sock, 101, headers)
    return WSConnection(sock, max_message=max_message, on_log=on_log, leftover=request.leftover)


class WSConnection:
    """服务端的一条 WebSocket 连接。"""

    def __init__(self, sock: socket.socket, *, max_message: int = DEFAULT_MAX_MESSAGE,
                 on_log: Optional[Callable[[str], None]] = None, leftover: bytes = b"") -> None:
        self.sock = sock
        self._parser = FrameParser(max_message=max_message, require_mask=True)
        self._send_lock = threading.Lock()
        self._closed = False
        self._close_sent = False
        self._on_log = on_log
        self.peer = _peer_of(sock)
        self.sent_bytes = 0
        self.recv_bytes = 0
        if leftover:
            self._parser.feed(leftover)

    def _log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    @property
    def is_open(self) -> bool:
        return not self._closed

    def send_binary(self, payload: bytes) -> None:
        self._send_frame(OP_BINARY, payload)

    def send_ping(self, payload: bytes = b"") -> None:
        self._send_frame(OP_PING, payload)

    def send_pong(self, payload: bytes = b"") -> None:
        self._send_frame(OP_PONG, payload)

    def send_close(self, code: int = 1000, reason: str = "") -> None:
        try:
            self._send_frame(OP_CLOSE, close_payload(code, reason))
        except (WebSocketError, OSError):
            pass
        self._close_sent = True

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise WebSocketError("连接已关闭")
        frame = encode_frame(opcode, payload, mask=False)
        with self._send_lock:
            try:
                self.sock.sendall(frame)
            except OSError as exc:
                self._closed = True
                raise WebSocketError("发送失败: %s" % exc) from exc
            self.sent_bytes += len(payload)

    def recv(self, timeout: Optional[float] = None) -> Tuple[str, bytes]:
        """读一条消息，语义见 ``wsclient.WSClient.recv``。"""
        while True:
            message = self._parser.next_message()
            if message is not None:
                opcode, payload = message
                self.recv_bytes += len(payload)
                if opcode == OP_PING:
                    try:
                        self.send_pong(payload)
                    except WebSocketError:
                        pass
                    return "ping", payload
                if opcode == OP_PONG:
                    return "pong", payload
                if opcode == OP_CLOSE:
                    if not self._close_sent:
                        self.send_close()
                    self._closed = True
                    return "close", payload
                if opcode == OP_TEXT:
                    return "text", payload
                return "binary", payload
            if self._closed:
                return "close", b""
            self.sock.settimeout(timeout)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout as exc:
                raise WebSocketError("读超时") from exc
            except OSError as exc:
                self._closed = True
                raise WebSocketError("接收失败: %s" % exc) from exc
            if not chunk:
                self._closed = True
                return "close", b""
            self._parser.feed(chunk)

    def close(self, code: int = 1000, reason: str = "") -> None:
        if not self._closed:
            self.send_close(code, reason)
        self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass


def _peer_of(sock: socket.socket) -> Tuple[str, int]:
    try:
        peer = sock.getpeername()
        return (str(peer[0]), int(peer[1]))
    except OSError:
        return ("?", 0)


def parse_close(payload: bytes) -> Tuple[int, str]:
    return parse_close_payload(payload)


def wrap_tls(sock: socket.socket, cert_file: str, key_file: str) -> socket.socket:
    """给监听 socket 套上 TLS（自建中继可选）。"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context.wrap_socket(sock, server_side=True)


__all__ = [
    "HEALTH_PATH",
    "HandshakeError",
    "WSConnection",
    "WSRequest",
    "accept_handshake",
    "read_http_request",
    "send_http_response",
    "wrap_tls",
]
