"""TSU/1 中继服务端（纯标准库参考实现）。

用途：路由方式 3（``self-relay``）—— 把中继跑在**你自己的**机器、局域网主机或 VPS 上。
它不做任何内容检查、不记录访问内容，只做 TCP 转发；目标过滤与并发上限按
``docs/tunnel-protocol.md`` 执行。

也可以当作一份可读的参考实现：Cloudflare Worker 与 Deno 版本的逻辑与这里一一对应。
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from ..hostrules import host_matches, is_private_address, split_list
from .protocol import (
    DEFAULT_ALLOW_PORTS,
    DEFAULT_PATH,
    ERR_BAD_REQUEST,
    ERR_BLOCKED_TARGET,
    ERR_CONNECT_FAILED,
    ERR_NOT_ALLOWED,
    ERR_TOO_MANY_STREAMS,
    HEALTH_PATH,
    OP_CLOSE,
    OP_DATA,
    OP_OPEN,
    OP_OPEN_ERR,
    OP_OPEN_OK,
    OP_PING,
    OP_PONG,
    OP_RESET,
    PROTO,
    SUBPROTOCOL,
    ProtocolError,
    UnknownOpcode,
    decode_address,
    decode_frame,
    encode_error,
    encode_frame,
    frame_name,
)
from .ratelimit import (
    AccessLogger,
    ConnectionLimiter,
    IPFilter,
    RateLimiter,
    RelayCircuitBreaker,
    parse_ip_rules,
)
from .wsclient import WebSocketError
from .wsserver import (
    HandshakeError,
    WSConnection,
    accept_handshake,
    read_http_request,
    send_http_response,
    wrap_tls,
)

#: 单条流允许在上层未发送时积压的字节数，超过就暂停读 WS（背压，规范 3.4）
MAX_PENDING_BYTES = 1024 * 1024


class _Stream:
    """中继侧的一条流：一个目标 TCP 连接 + 一个写队列 + 两个线程。"""

    def __init__(self, session: "RelaySession", stream_id: int, sock: socket.socket,
                 host: str, port: int) -> None:
        self.session = session
        self.sid = stream_id
        self.sock = sock
        self.host = host
        self.port = port
        self.label = "%s:%d" % (host, port)
        self.created = time.time()
        self.bytes_up = 0
        self.bytes_down = 0
        self._out: Deque[Optional[bytes]] = deque()
        self._cond = threading.Condition()
        self._pending = 0
        self._closed = False
        self._remote_eof = False     # 收到对端 CLOSE
        self._local_eof = False      # 本地读到 EOF，已回 CLOSE
        self._threads: List[threading.Thread] = []

    # ---------------------------------------------------------- 写队列
    def enqueue(self, data: bytes) -> None:
        """session 循环调用；积压过多时在这里阻塞，从而暂停读 WS。"""
        with self._cond:
            while self._pending > MAX_PENDING_BYTES and not self._closed:
                self._cond.wait(0.5)
            if self._closed:
                return
            self._out.append(data)
            self._pending += len(data)
            self._cond.notify_all()

    def finish(self) -> None:
        """对端发来 CLOSE：把队列里的数据写完，然后半关闭目标连接。"""
        with self._cond:
            self._remote_eof = True
            self._out.append(None)
            self._cond.notify_all()

    def abort(self) -> None:
        with self._cond:
            self._closed = True
            self._out.clear()
            self._pending = 0
            self._cond.notify_all()
        try:
            self.sock.close()
        except OSError:
            pass

    def notify_closed(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    # ---------------------------------------------------------- 线程
    def start(self) -> None:
        for target, name in ((self._writer, "tsu-relay-w"), (self._pump_tcp, "tsu-relay-r")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def join(self, timeout: float = 2.0) -> None:
        for thread in self._threads:
            thread.join(timeout=timeout)

    def _writer(self) -> None:
        while True:
            with self._cond:
                while not self._out and not self._closed:
                    self._cond.wait(0.5)
                if self._closed:
                    return
                chunk = self._out.popleft()
                if chunk is not None:
                    self._pending -= len(chunk)
                self._cond.notify_all()
            if chunk is None:
                try:
                    self.sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                continue
            try:
                self.sock.sendall(chunk)
                self.bytes_up += len(chunk)
            except (OSError, socket.timeout):
                self._fail("写入目标失败")
                return

    def _pump_tcp(self) -> None:
        self.sock.settimeout(self.session.server.idle_timeout)
        while True:
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                self._fail("目标连接空闲超时", reset=True)
                return
            except OSError:
                self._fail("读取目标失败")
                return
            if not data:
                self._local_eof = True
                try:
                    self.session.send(OP_CLOSE, self.sid)
                except WebSocketError:
                    pass
                self._maybe_release()
                return
            self.bytes_down += len(data)
            try:
                for start in range(0, len(data), 32 * 1024):
                    self.session.send(OP_DATA, self.sid, data[start : start + 32 * 1024])
            except WebSocketError:
                self._fail("隧道已断开")
                return

    def _fail(self, reason: str, *, reset: bool = False) -> None:
        if reset:
            try:
                self.session.send(OP_RESET, self.sid)
            except WebSocketError:
                pass
        self.session.drop(self.sid, reason)

    def _maybe_release(self) -> None:
        if self._local_eof and self._remote_eof:
            self.session.drop(self.sid, "双向关闭")

    def mark_remote_eof(self) -> None:
        self._remote_eof = True
        self.finish()

    def stats(self) -> Dict[str, object]:
        return {"target": self.label, "sid": self.sid, "up": self.bytes_up,
                "down": self.bytes_down, "age": round(time.time() - self.created, 1)}


class RelaySession:
    """一条 WS 连接上的会话：分发帧、管理这条连接上的所有流。"""

    def __init__(self, ws: WSConnection, server: "RelayServer") -> None:
        self.ws = ws
        self.server = server
        self.streams: Dict[int, _Stream] = {}
        self.lock = threading.Lock()
        self.closed = False
        self.pings = 0
        self.last_rx = time.time()

    # ---------------------------------------------------------- 发送
    def send(self, opcode: int, stream_id: int, payload: bytes = b"") -> None:
        self.ws.send_binary(encode_frame(opcode, stream_id, payload))

    def send_open_error(self, stream_id: int, code: int, message: str) -> None:
        self.send(OP_OPEN_ERR, stream_id, encode_error(code, message))

    # ---------------------------------------------------------- 主循环
    def run(self) -> None:
        while not self.closed:
            try:
                kind, payload = self.ws.recv(timeout=self.server.keepalive)
            except WebSocketError as exc:
                if "读超时" in str(exc):
                    if time.time() - self.last_rx >= self.server.keepalive:
                        try:
                            self.ws.send_ping(b"")
                        except WebSocketError:
                            break
                        self.pings += 1
                        if self.pings >= 2:
                            self.server.log("客户端 %s 保活无响应，断开" % (self.ws.peer[0],))
                            break
                    continue
                self.server.log("连接 %s 出错：%s" % (self.ws.peer[0], exc))
                break
            self.last_rx = time.time()
            self.pings = 0
            if kind == "binary":
                if not self._dispatch(payload):
                    break
            elif kind == "close":
                break
            # 文本 / ping / pong：忽略（ping 已由 WS 层自动回 pong）
        self.shutdown()

    def _dispatch(self, payload: bytes) -> bool:
        try:
            opcode, stream_id, body = decode_frame(payload)
        except UnknownOpcode as exc:
            # 规范 2.1：未定义 opcode → 用 RESET(该流) 回应；流 id 为 0 时关闭连接
            if exc.stream_id:
                self.send(OP_RESET, exc.stream_id)
                return True
            self.server.log("未知 opcode 0x%02x（流 0），断开连接" % exc.opcode)
            return False
        except ProtocolError as exc:
            self.server.log("帧非法（%s），断开连接" % exc)
            return False
        if opcode == OP_OPEN:
            self._handle_open(stream_id, body)
            return True
        if opcode == OP_DATA:
            stream = self.streams.get(stream_id)
            if stream is not None:
                stream.enqueue(body)
            return True
        if opcode == OP_CLOSE:
            stream = self.streams.get(stream_id)
            if stream is not None:
                stream.mark_remote_eof()
            return True
        if opcode == OP_RESET:
            stream = self.streams.get(stream_id)
            if stream is not None:
                self.drop(stream_id, "客户端重置")
            return True
        if opcode == OP_PING:
            self.send(OP_PONG, stream_id, body[:32])
            return True
        if opcode == OP_PONG:
            return True
        if opcode in (OP_OPEN_OK, OP_OPEN_ERR):
            self.server.log("客户端不应发送 %s，断开连接" % frame_name(opcode))
            return False
        # 其余已定义 opcode 都已在上面的分支里处理完，能走到这里说明是客户端发错了方向
        self.server.log("客户端不应发送 %s（流 %d），断开连接" % (frame_name(opcode), stream_id))
        return False

    # ---------------------------------------------------------- OPEN
    def _handle_open(self, stream_id: int, body: bytes) -> None:
        if stream_id < 1:
            self.send_open_error(stream_id, ERR_BAD_REQUEST, "流 id 非法")
            return
        with self.lock:
            if stream_id in self.streams:
                self.send_open_error(stream_id, ERR_BAD_REQUEST, "流 id 重复")
                return
            if len(self.streams) >= self.server.max_streams:
                self.send_open_error(stream_id, ERR_TOO_MANY_STREAMS,
                                     "本连接并发上限 %d" % self.server.max_streams)
                return
        try:
            host, port = decode_address(body)
        except ProtocolError as exc:
            self.send_open_error(stream_id, ERR_BAD_REQUEST, "地址非法: %s" % exc)
            return
        code = self.server.check_target(host, port)
        if code is not None:
            self.send_open_error(stream_id, code, "%s 不被允许" % host)
            return
        try:
            sock = socket.create_connection((host, port), timeout=self.server.connect_timeout)
        except OSError as exc:
            self.send_open_error(stream_id, ERR_CONNECT_FAILED, str(exc))
            self.server.counters["stream_failed"] += 1
            self.server.circuit_breaker.record_error()
            self.server.access_logger.record({
                "event": "stream_reject", "reason": "connect_failed",
                "sid": stream_id, "target": "%s:%d" % (host, port),
            })
            return
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        stream = _Stream(self, stream_id, sock, host, port)
        with self.lock:
            self.streams[stream_id] = stream
        self.server.counters["streams_total"] += 1
        self.server.counters["streams_active"] += 1
        self.server.circuit_breaker.record_success()
        self.server.access_logger.record({
            "event": "stream_open", "sid": stream_id,
            "target": "%s:%d" % (host, port),
        })
        self.send(OP_OPEN_OK, stream_id)
        stream.start()

    # ---------------------------------------------------------- 收尾
    def drop(self, stream_id: int, reason: str = "") -> None:
        with self.lock:
            stream = self.streams.pop(stream_id, None)
        if stream is None:
            return
        stream.notify_closed()
        try:
            stream.sock.close()
        except OSError:
            pass
        self.server.counters["streams_active"] -= 1
        self.server.counters["bytes_up"] += stream.bytes_up
        self.server.counters["bytes_down"] += stream.bytes_down
        if reason:
            if self.server.log_targets:
                self.server.log("流 %s 关闭：%s（↑%d ↓%d 字节）"
                                % (stream.label, reason, stream.bytes_up, stream.bytes_down))
            else:
                # 规范 3.8：默认只记录流 id 与字节数，不记录目标域名
                self.server.log("流 #%d 关闭：%s（↑%d ↓%d 字节）"
                                % (stream.sid, reason, stream.bytes_up, stream.bytes_down))

    def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        with self.lock:
            ids = list(self.streams)
        for stream_id in ids:
            self.drop(stream_id, "")
        try:
            self.ws.close()
        except OSError:
            pass


class RelayServer:
    """TSU/1 中继服务（可自建）。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9052,
        *,
        token: str = "",
        allow_hosts: Optional[Sequence[str]] = None,
        allow_ports: Optional[Sequence[int]] = None,
        allow_all: bool = False,
        allow_private: bool = False,
        max_streams: int = 64,
        path: str = DEFAULT_PATH,
        idle_timeout: float = 300.0,
        connect_timeout: float = 30.0,
        keepalive: float = 30.0,
        tls_cert: str = "",
        tls_key: str = "",
        on_log=None,
        log_targets: bool = False,
        # 增强：限流
        rate_limit_rps: float = 0.0,
        rate_limit_burst: Optional[float] = None,
        per_client_rps: float = 0.0,
        per_token_rps: float = 0.0,
        # 增强：连接数限制
        max_conns_per_ip: int = 0,
        max_conns_per_token: int = 0,
        max_conns_total: int = 0,
        # 增强：IP 过滤
        ip_whitelist: Optional[Sequence[str]] = None,
        ip_blacklist: Optional[Sequence[str]] = None,
        # 增强：熔断
        circuit_breaker_enabled: bool = False,
        cb_error_threshold: int = 20,
        cb_window_seconds: float = 10.0,
        cb_recovery_seconds: float = 30.0,
        # 增强：访问日志
        access_log: str = "",
        access_log_format: str = "json",
    ) -> None:
        self.host = host
        self.port = int(port)
        self.token = token or ""
        self.allow_hosts = list(allow_hosts or [])
        self.allow_ports = list(allow_ports) if allow_ports else list(DEFAULT_ALLOW_PORTS)
        self.allow_all = bool(allow_all)
        self.allow_private = bool(allow_private)
        self.max_streams = max(1, int(max_streams))
        self.path = path or DEFAULT_PATH
        self.idle_timeout = float(idle_timeout)
        self.connect_timeout = float(connect_timeout)
        self.keepalive = max(5.0, float(keepalive))
        self.tls_cert = tls_cert or ""
        self.tls_key = tls_key or ""
        # 规范 3.8：默认不把目标域名写进日志；只有显式打开才逐条打印
        self.log_targets = bool(log_targets)
        self._on_log = on_log
        self._listener: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        # 增强组件
        self.rate_limiter = RateLimiter(
            global_rps=rate_limit_rps,
            global_burst=rate_limit_burst,
            per_ip_rps=per_client_rps,
            per_token_rps=per_token_rps,
        )
        self.conn_limiter = ConnectionLimiter(
            max_per_ip=max_conns_per_ip,
            max_per_token=max_conns_per_token,
            max_total=max_conns_total,
        )
        wl, bl = parse_ip_rules(list(ip_whitelist or []) + ["!" + str(x) for x in (ip_blacklist or [])])
        self.ip_filter = IPFilter(wl, bl)
        self.circuit_breaker = RelayCircuitBreaker(
            error_threshold=cb_error_threshold if circuit_breaker_enabled else 0,
            window_seconds=cb_window_seconds,
            recovery_seconds=cb_recovery_seconds,
        )
        self.access_logger = AccessLogger(
            path=access_log,
            fmt=access_log_format,
            log_targets=log_targets,
        )
        self.counters: Dict[str, int] = {
            "sessions_total": 0,
            "sessions_active": 0,
            "streams_total": 0,
            "streams_active": 0,
            "stream_failed": 0,
            "bytes_up": 0,
            "bytes_down": 0,
            "rejected_token": 0,
            "rejected_target": 0,
            "rejected_rate_limit": 0,
            "rejected_conn_limit": 0,
            "rejected_ip_filter": 0,
            "rejected_circuit": 0,
        }
        self.started_at = 0.0

    # ---------------------------------------------------------- 基础设施
    def log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    def check_target(self, host: str, port: int) -> Optional[int]:
        """返回 ``None`` 表示放行，否则返回 ``OPEN_ERR`` 错误码。"""
        if not host or not 0 < port <= 0xFFFF:
            return ERR_BAD_REQUEST
        if not self.allow_private and is_private_address(host):
            self.counters["rejected_target"] += 1
            return ERR_BLOCKED_TARGET
        # ``allow_all`` 表示「任意 host:port」，此时端口白名单也一并关闭
        if self.allow_all:
            return None
        if self.allow_ports and port not in self.allow_ports:
            self.counters["rejected_target"] += 1
            return ERR_NOT_ALLOWED
        if host_matches(host, self.allow_hosts):
            return None
        self.counters["rejected_target"] += 1
        return ERR_NOT_ALLOWED

    def describe(self) -> str:
        hosts = "任意" if self.allow_all else ("%d 条规则" % len(self.allow_hosts))
        ports = "任意" if not self.allow_ports else ",".join(str(p) for p in self.allow_ports)
        return ("中继 %s:%d 路径=%s 目标=%s 端口=%s 并发/连接=%d 令牌=%s"
                % (self.host, self.port, self.path, hosts, ports, self.max_streams,
                   "已设置" if self.token else "无"))

    def bind(self) -> Tuple[str, int]:
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(64)
        listener.settimeout(0.5)
        self._listener = listener
        self.port = listener.getsockname()[1]
        return (self.host, self.port)

    def serve_forever(self) -> None:
        if self._listener is None:
            self.bind()
        assert self._listener is not None
        self.started_at = time.time()
        self._stop.clear()
        self.log(self.describe())
        while not self._stop.is_set():
            try:
                client, peer = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._handle_client, args=(client, peer),
                                      name="tsu-relay-session", daemon=True)
            thread.start()
            self._threads.append(thread)
            self._threads = [item for item in self._threads if item.is_alive()]

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        self.access_logger.close()

    # ---------------------------------------------------------- 单连接
    def _handle_client(self, sock: socket.socket, peer) -> None:
        peer_ip = peer[0]
        self.counters["sessions_total"] += 1
        self.counters["sessions_active"] += 1

        # 增强：IP 黑白名单
        if self.ip_filter.enabled and not self.ip_filter.allowed(peer_ip):
            self.counters["rejected_ip_filter"] += 1
            self.log("拒绝 IP %s（不在白名单或命中黑名单）" % peer_ip)
            self.access_logger.record({"event": "conn_reject", "reason": "ip_filter", "ip": peer_ip})
            _safe_close(sock)
            self.counters["sessions_active"] -= 1
            return

        # 增强：熔断打开时拒绝新连接
        if self.circuit_breaker.open_:
            self.counters["rejected_circuit"] += 1
            self.access_logger.record({"event": "conn_reject", "reason": "circuit_open", "ip": peer_ip})
            send_http_response(sock, 503, body="熔断中，请稍后重试\n".encode("utf-8"))
            _safe_close(sock)
            self.counters["sessions_active"] -= 1
            return

        # 增强：令牌解析（连接数限制与限流都按令牌统计）
        token_key = ""
        # 注意：真正的令牌校验仍在 WebSocket 升级时进行；这里提前解析用于统计

        # 增强：并发连接数限制
        if self.conn_limiter.enabled:
            if not self.conn_limiter.acquire(peer_ip, token_key):
                self.counters["rejected_conn_limit"] += 1
                self.access_logger.record({"event": "conn_reject", "reason": "conn_limit", "ip": peer_ip})
                send_http_response(sock, 429, body="连接数超限\n".encode("utf-8"))
                _safe_close(sock)
                self.counters["sessions_active"] -= 1
                return

        def _release_conn_limit() -> None:
            if self.conn_limiter.enabled:
                self.conn_limiter.release(peer_ip, token_key)

        if self.tls_cert and self.tls_key:
            try:
                sock = wrap_tls(sock, self.tls_cert, self.tls_key)
            except (OSError, ssl.SSLError) as exc:
                self.log("TLS 握手失败（%s）：%s" % (peer_ip, exc))
                _safe_close(sock)
                self.counters["sessions_active"] -= 1
                _release_conn_limit()
                return
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            request = read_http_request(sock, timeout=10.0)
        except (HandshakeError, OSError) as exc:
            self.log("握手失败（%s）：%s" % (peer_ip, exc))
            _safe_close(sock)
            self.counters["sessions_active"] -= 1
            _release_conn_limit()
            return
        if request.path == HEALTH_PATH:
            body = json.dumps({
                "ok": True,
                "proto": PROTO,
                "allow_all": self.allow_all,
                "max_streams": self.max_streams,
                "uptime": round(time.time() - self.started_at, 1),
                "rate_limit": self.rate_limiter.enabled,
                "conn_limit": self.conn_limiter.enabled,
                "circuit_open": self.circuit_breaker.open_,
            }).encode("utf-8")
            send_http_response(sock, 200, {"Content-Type": "application/json"}, body)
            _safe_close(sock)
            self.counters["sessions_active"] -= 1
            _release_conn_limit()
            return
        if request.path != self.path:
            send_http_response(sock, 404, body="未知路径\n".encode("utf-8"))
            _safe_close(sock)
            self.counters["sessions_active"] -= 1
            _release_conn_limit()
            return
        if request.method != "GET" or "websocket" not in request.headers.get("upgrade", "").lower():
            send_http_response(sock, 426, body="需要 WebSocket 升级\n".encode("utf-8"))
            _safe_close(sock)
            self.counters["sessions_active"] -= 1
            _release_conn_limit()
            return
        if self.token and request.token() != self.token:
            self.counters["rejected_token"] += 1
            self.log("拒绝令牌错误的连接：%s" % peer_ip)
            send_http_response(sock, 401, body="令牌错误\n".encode("utf-8"))
            _safe_close(sock)
            self.counters["sessions_active"] -= 1
            _release_conn_limit()
            return

        # 增强：限流（在令牌校验之后按真实令牌统计）
        token_key = request.token() if self.token else ""
        if self.rate_limiter.enabled:
            allowed, retry_after = self.rate_limiter.check(peer_ip, token_key)
            if not allowed:
                self.counters["rejected_rate_limit"] += 1
                self.access_logger.record({
                    "event": "conn_reject", "reason": "rate_limit",
                    "ip": peer_ip, "retry_after": round(retry_after, 2),
                })
                send_http_response(sock, 429, {"Retry-After": str(int(retry_after) + 1)},
                                   ("请求过快，请 %.1f 秒后重试\n" % retry_after).encode("utf-8"))
                _safe_close(sock)
                self.counters["sessions_active"] -= 1
                _release_conn_limit()
                return

        try:
            ws = accept_handshake(sock, request, subprotocol=SUBPROTOCOL)
        except (HandshakeError, OSError, WebSocketError) as exc:
            self.log("升级失败：%s" % exc)
            _safe_close(sock)
            self.counters["sessions_active"] -= 1
            _release_conn_limit()
            return

        self.access_logger.record({"event": "conn_open", "ip": peer_ip, "token": bool(token_key)})
        session = RelaySession(ws, self)
        try:
            session.run()
        finally:
            self.counters["sessions_active"] -= 1
            self.access_logger.record({
                "event": "conn_close", "ip": peer_ip,
                "duration": round(time.time() - session.last_rx, 1),
            })
            _release_conn_limit()
            _safe_close(sock)

    # ---------------------------------------------------------- 统计
    def stats(self) -> Dict[str, Any]:
        data: Dict[str, Any] = dict(self.counters)
        data.update({
            "listen": "%s:%d" % (self.host, self.port),
            "uptime": round(time.time() - self.started_at, 1) if self.started_at else 0,
        })
        if self.rate_limiter.enabled:
            data["rate_limit_rejected"] = self.rate_limiter.rejected
        if self.conn_limiter.enabled:
            data["conn_limit"] = self.conn_limiter.stats()
        if self.circuit_breaker.enabled:
            data["circuit_open"] = self.circuit_breaker.open_
            data["circuit_trips"] = self.circuit_breaker.trips
        return data

    def status_line(self) -> str:
        return ("会话 %d（活跃 %d）· 流 %d（活跃 %d）· ↑%s ↓%s"
                % (self.counters["sessions_total"], self.counters["sessions_active"],
                   self.counters["streams_total"], self.counters["streams_active"],
                   _human(self.counters["bytes_up"]), _human(self.counters["bytes_down"])))


def _safe_close(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "%.1f%s" % (value, unit)
        value /= 1024
    return "%.1fGB" % value


def build_allow_hosts(values: Optional[object]) -> List[str]:
    """把配置里的白名单（字符串或列表）规整成列表。"""
    return split_list(values)
