"""WebSocket 客户端（纯标准库）。

只实现本项目需要的那部分 RFC 6455：HTTP/1.1 Upgrade 握手、二进制/文本消息、
掩码、分片拼装、PING/PONG/CLOSE 控制帧。没有 permessage-deflate（不必要，
且压缩会破坏密文长度特征）。
"""

from __future__ import annotations

import socket
import ssl
import threading
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

from .protocol import DEFAULT_PATH, SUBPROTOCOL
from .wsframe import (
    OP_BINARY,
    OP_CLOSE,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    FrameParser,
    HandshakeError,
    WebSocketError,
    build_handshake_request,
    check_handshake,
    close_payload,
    encode_frame,
    new_key,
    parse_http_response,
)

DEFAULT_MAX_MESSAGE = 1024 * 1024


class WebSocketTimeout(WebSocketError):
    """读超时（链路可能还活着，只是暂时没有数据）。"""


class WSClient:
    """一条 WebSocket 连接。

    ``url`` 支持 ``ws://`` / ``wss://``（``http://`` / ``https://`` 会自动转换）。
    可选 ``front``：TLS 的 SNI 用另一个域名（域前置），HTTP ``Host`` 仍用真实中继域名。
    """

    def __init__(
        self,
        url: str,
        *,
        token: str = "",
        token_in_header: bool = False,
        subprotocol: str = SUBPROTOCOL,
        timeout: float = 30.0,
        front: str = "",
        connect_host: str = "",
        insecure: bool = False,
        ca_file: str = "",
        user_agent: str = "TorSOCKS5",
        extra_headers: Optional[Dict[str, str]] = None,
        max_message: int = DEFAULT_MAX_MESSAGE,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        parsed = urlsplit(url)
        scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
        if scheme not in ("ws", "wss"):
            raise WebSocketError("不支持的 URL 协议: %r（只支持 ws/wss）" % parsed.scheme)
        if not parsed.hostname:
            raise WebSocketError("URL 缺少主机名: %r" % url)
        self.scheme = scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if scheme == "wss" else 80)
        query = parsed.query
        if token and "token=" not in query:
            query = (query + "&" if query else "") + "token=" + _quote(token)
        self.path = (parsed.path or DEFAULT_PATH) + (("?" + query) if query else "")
        self.url = urlunsplit((scheme, parsed.netloc, parsed.path or DEFAULT_PATH, query, ""))

        self.token = token
        self.token_in_header = token_in_header
        self.subprotocol = subprotocol
        self.timeout = timeout
        self.front = front
        self.connect_host = connect_host
        self.insecure = insecure
        self.ca_file = ca_file
        self.user_agent = user_agent
        self.extra_headers = dict(extra_headers or {})
        self.on_log = on_log

        self._sock: Optional[socket.socket] = None
        self._parser = FrameParser(max_message=max_message)
        self._send_lock = threading.Lock()
        self._closed = False
        self._close_sent = False
        self.sent_bytes = 0
        self.recv_bytes = 0

    # ------------------------------------------------------------- 连接
    @property
    def is_open(self) -> bool:
        return self._sock is not None and not self._closed

    def _log(self, message: str) -> None:
        if self.on_log is not None:
            self.on_log(message)

    def connect(self) -> None:
        """建立 TCP（+TLS）连接并完成 WS 握手。"""
        if self._sock is not None:
            raise WebSocketError("已经连接过了")
        target = self.connect_host or self.host
        self._sock = socket.create_connection((target, self.port), timeout=self.timeout)
        try:
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        if self.scheme == "wss":
            self._sock = self._wrap_tls(self._sock)
        key = new_key()
        headers: Dict[str, str] = {}
        if self.token and self.token_in_header:
            headers["Authorization"] = "Bearer " + self.token
        headers.update(self.extra_headers)
        request = build_handshake_request(
            self.host if self.port in (80, 443) else "%s:%d" % (self.host, self.port),
            self.path,
            key,
            subprotocol=self.subprotocol,
            headers=headers,
            user_agent=self.user_agent,
        )
        assert self._sock is not None
        self._sock.sendall(request)
        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise HandshakeError("握手中连接被关闭")
            raw.extend(chunk)
            if len(raw) > 64 * 1024:
                raise HandshakeError("响应头过大")
        status, response_headers, consumed = parse_http_response(bytes(raw))
        warning = check_handshake(status, response_headers, key, self.subprotocol)
        if warning:
            self._log(warning)
        self._parser.feed(bytes(raw)[consumed:])
        self._closed = False
        self._close_sent = False
        self._log("已连接 %s" % self.url)

    def _wrap_tls(self, sock: socket.socket) -> socket.socket:
        # 只提供 http/1.1：握手是 HTTP/1.1 的，如果 ALPN 协商成 h2 会导致对端按 h2 解析而失败
        context = ssl.create_default_context(cafile=self.ca_file or None)
        context.set_alpn_protocols(["http/1.1"])
        if self.insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        server_hostname = self.front or self.host
        return context.wrap_socket(sock, server_hostname=server_hostname)

    # ------------------------------------------------------------- 发送
    def send_binary(self, payload: bytes) -> None:
        self._send_frame(OP_BINARY, payload)

    def send_text(self, payload: bytes) -> None:
        self._send_frame(OP_TEXT, payload)

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
        sock = self._sock
        if sock is None or self._closed:
            raise WebSocketError("连接已关闭")
        frame = encode_frame(opcode, payload, mask=True)
        with self._send_lock:
            try:
                sock.sendall(frame)
            except OSError as exc:
                self._closed = True
                raise WebSocketError("发送失败: %s" % exc) from exc
            self.sent_bytes += len(payload)

    # ------------------------------------------------------------- 接收
    def recv(self, timeout: Optional[float] = None) -> Tuple[str, bytes]:
        """读取一条消息，返回 ``(kind, payload)``。

        ``kind`` 取 ``binary`` / ``text`` / ``close`` / ``ping`` / ``pong``。
        PING 会自动回 PONG（仍然把事件返回给调用者，便于统计）；
        读超时抛 ``WebSocketTimeout``（链路未必断开）。
        """
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
            sock = self._sock
            if sock is None:
                raise WebSocketError("连接已关闭")
            if self._closed:
                return "close", b""
            sock.settimeout(self.timeout if timeout is None else timeout)
            try:
                chunk = sock.recv(65536)
            except socket.timeout as exc:
                raise WebSocketTimeout("读超时") from exc
            except OSError as exc:
                self._closed = True
                raise WebSocketError("接收失败: %s" % exc) from exc
            if not chunk:
                self._closed = True
                return "close", b""
            self._parser.feed(chunk)

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self._sock is None:
            return
        if not self._closed:
            self.send_close(code, reason)
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None

    def __enter__(self) -> "WSClient":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _quote(text: str) -> str:
    return quote(text, safe="")
