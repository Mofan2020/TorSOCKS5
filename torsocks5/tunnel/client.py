"""多路复用隧道客户端：连接池 + 流分配 + 断线重连。

一条 WebSocket 连接（下称「链路」）上可以并发跑多条流（TCP 连接）。链路的并发上限由
中继决定：Cloudflare Worker 只能 6 条（平台硬限制），自建中继默认 64 条。客户端因此维护
一个链路池，把新流分配给当前负载最低、还有额度的链路。
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Dict, List, Optional

from ..socks5.protocol import (
    REP_CONNECTION_REFUSED,
    REP_GENERAL_FAILURE,
    REP_HOST_UNREACHABLE,
    REP_NOT_ALLOWED,
)
from .protocol import (
    ERR_BLOCKED_TARGET,
    ERR_CONNECT_FAILED,
    ERR_NOT_ALLOWED,
    ERR_TOO_MANY_STREAMS,
    ERR_UNAUTHORIZED,
    OP_CLOSE,
    OP_DATA,
    OP_OPEN,
    OP_OPEN_ERR,
    OP_OPEN_OK,
    OP_PING,
    OP_PONG,
    OP_RESET,
    STREAM_ID_START,
    ProtocolError,
    UnknownOpcode,
    decode_error,
    decode_frame,
    encode_address,
    encode_frame,
    error_name,
    frame_name,
)
from .stream import MAX_PENDING_BYTES, TunnelSocket
from .wsclient import WebSocketError, WebSocketTimeout, WSClient


class TunnelError(OSError):
    """隧道层错误。``rep_code`` 给出建议回给 SOCKS5 客户端的应答码。"""

    def __init__(self, message: str, *, rep_code: int = REP_GENERAL_FAILURE,
                 err_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.rep_code = rep_code
        self.err_code = err_code


class TargetNotAllowed(TunnelError):
    def __init__(self, message: str, err_code: Optional[int] = None) -> None:
        super().__init__(message, rep_code=REP_NOT_ALLOWED, err_code=err_code)


class TargetUnreachable(TunnelError):
    def __init__(self, message: str, err_code: Optional[int] = None) -> None:
        super().__init__(message, rep_code=REP_HOST_UNREACHABLE, err_code=err_code)


class TunnelUnavailable(TunnelError):
    def __init__(self, message: str, err_code: Optional[int] = None) -> None:
        super().__init__(message, rep_code=REP_CONNECTION_REFUSED, err_code=err_code)


class _TooManyStreams(TunnelError):
    """内部信号：换一条链路重试，不暴露给上层。"""

    def __init__(self, message: str, err_code: int = 0) -> None:
        super().__init__(message, err_code=err_code)


def _map_open_error(code: int, message: str) -> TunnelError:
    detail = "%s（%s）" % (message or error_name(code), error_name(code))
    if code in (ERR_NOT_ALLOWED, ERR_BLOCKED_TARGET):
        return TargetNotAllowed(detail, code)
    if code == ERR_TOO_MANY_STREAMS:
        return _TooManyStreams(detail, code)
    if code == ERR_UNAUTHORIZED:
        return TunnelUnavailable("中继拒绝令牌：" + detail, code)
    if code == ERR_CONNECT_FAILED:
        return TargetUnreachable(detail, code)
    return TunnelError(detail, rep_code=REP_GENERAL_FAILURE, err_code=code)


class Link:
    """一条 WebSocket 链路。"""

    def __init__(self, client: "TunnelClient", index: int) -> None:
        self.client = client
        self.index = index
        self.ws: Optional[WSClient] = None
        self.streams: Dict[int, TunnelSocket] = {}
        self.streams_lock = threading.Lock()
        self._ids = itertools.count(STREAM_ID_START)
        self._drain = threading.Condition()
        self.closed = False
        self.connected = False
        self.pings = 0
        self.last_rx = 0.0
        self.opened_at = 0.0
        self.reconnects = 0
        self.down_reason = ""
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self.ws = self.client.make_ws()
        self.ws.connect()
        self.connected = True
        self.closed = False
        self.last_rx = time.time()
        self.opened_at = self.last_rx
        self.pings = 0
        self._thread = threading.Thread(target=self._reader, name="tsu-link-%d" % self.index,
                                        daemon=True)
        self._thread.start()

    @property
    def is_open(self) -> bool:
        return bool(self.connected and not self.closed and self.ws is not None and self.ws.is_open)

    def capacity(self) -> int:
        if not self.is_open:
            return 0
        with self.streams_lock:
            return max(0, self.client.max_streams - len(self.streams))

    def load(self) -> int:
        with self.streams_lock:
            return len(self.streams)

    def pending_bytes(self) -> int:
        with self.streams_lock:
            return sum(stream.pending for stream in self.streams.values())

    def local_address(self) -> str:
        return self.ws.host if self.ws is not None else "tunnel"

    def notify_drained(self) -> None:
        with self._drain:
            self._drain.notify_all()

    # ------------------------------------------------------------ 发送
    def send_frame(self, opcode: int, stream_id: int, payload: bytes = b"") -> None:
        ws = self.ws
        if ws is None or self.closed:
            raise WebSocketError("链路已关闭")
        ws.send_binary(encode_frame(opcode, stream_id, payload))

    def send_data(self, stream_id: int, payload: bytes) -> None:
        self.send_frame(OP_DATA, stream_id, payload)

    def send_close(self, stream_id: int) -> None:
        try:
            self.send_frame(OP_CLOSE, stream_id)
        except WebSocketError:
            pass

    def send_reset(self, stream_id: int) -> None:
        try:
            self.send_frame(OP_RESET, stream_id)
        except WebSocketError:
            pass

    def release(self, stream_id: int) -> None:
        with self.streams_lock:
            self.streams.pop(stream_id, None)
        self.client.notify_capacity()

    # ------------------------------------------------------------ 开流
    def open_stream(self, host: str, port: int, *, timeout: float) -> TunnelSocket:
        if not self.is_open:
            raise TunnelUnavailable("链路 %d 不可用" % self.index)
        with self.streams_lock:
            if len(self.streams) >= self.client.max_streams:
                raise _TooManyStreams("链路 %d 并发已满" % self.index)
            stream_id = next(self._ids)
            stream = TunnelSocket(self, stream_id, host, port,
                                  idle_timeout=self.client.idle_timeout)
            self.streams[stream_id] = stream
        try:
            self.send_frame(OP_OPEN, stream_id, encode_address(host, port))
        except (WebSocketError, ProtocolError) as exc:
            with self.streams_lock:
                self.streams.pop(stream_id, None)
            raise TunnelUnavailable("发送 OPEN 失败: %s" % exc) from exc
        if not stream.open_event.wait(max(0.5, timeout)):
            self.send_reset(stream_id)
            self.release(stream_id)
            raise TargetUnreachable("等待中继建立连接超时（%.0fs）" % timeout)
        if stream.open_error is not None:
            error = stream.open_error
            self.release(stream_id)
            raise error
        return stream

    # ------------------------------------------------------------ 接收
    def _reader(self) -> None:
        assert self.ws is not None
        while not self.closed:
            try:
                kind, payload = self.ws.recv(timeout=self.client.keepalive)
            except WebSocketTimeout:
                if time.time() - self.last_rx >= self.client.keepalive:
                    try:
                        self.ws.send_ping(b"")
                    except WebSocketError:
                        break
                    self.pings += 1
                    if self.pings >= 2:
                        self.client.log("链路 %d：连续 %d 次保活无响应，判定断开"
                                        % (self.index, self.pings))
                        break
                continue
            except WebSocketError as exc:
                self.down_reason = str(exc)
                break
            self.last_rx = time.time()
            self.pings = 0
            if kind == "binary":
                if not self._dispatch(payload):
                    break
                self._apply_backpressure()
            elif kind == "close":
                self.down_reason = "对端关闭了连接"
                break
            # text / ping / pong：按规范忽略（ping 已由 WS 层自动回 pong）
        self._teardown(self.down_reason or "链路结束")

    def _dispatch(self, payload: bytes) -> bool:
        """处理一个 TSU 帧；返回 False 表示链路需要断开。"""
        try:
            opcode, stream_id, body = decode_frame(payload)
        except UnknownOpcode as exc:
            # 规范 2.1：未定义 opcode → 用 RESET(该流) 回应；流 id 为 0 时断开链路
            if exc.stream_id:
                self.client.log("链路 %d：收到未定义 opcode 0x%02x，回 RESET(流 %d)"
                                % (self.index, exc.opcode, exc.stream_id))
                try:
                    self.send_frame(OP_RESET, exc.stream_id)
                except WebSocketError:
                    return False
                return True
            self.client.log("链路 %d：未定义 opcode 0x%02x（流 0），断开"
                            % (self.index, exc.opcode))
            return False
        except ProtocolError as exc:
            self.client.log("链路 %d：帧非法（%s），断开" % (self.index, exc))
            return False
        if opcode == OP_DATA:
            stream = self._stream(stream_id)
            if stream is not None:
                stream._on_data(body)
            return True
        if opcode == OP_OPEN_OK:
            stream = self._stream(stream_id)
            if stream is not None:
                stream._set_open_result(None)
            return True
        if opcode == OP_OPEN_ERR:
            try:
                code, message = decode_error(body)
            except ProtocolError:
                code, message = 0, "错误的 OPEN_ERR 帧"
            stream = self._stream(stream_id)
            if stream is not None:
                stream._set_open_result(_map_open_error(code, message))
            return True
        if opcode == OP_CLOSE:
            stream = self._stream(stream_id)
            if stream is not None:
                stream._on_remote_close()
            return True
        if opcode == OP_RESET:
            stream = self._stream(stream_id)
            if stream is not None:
                stream._on_reset("对端重置")
            return True
        if opcode == OP_PING:
            try:
                self.send_frame(OP_PONG, stream_id, body)
            except WebSocketError:
                return False
            return True
        if opcode == OP_PONG:
            return True
        if stream_id:
            self.send_reset(stream_id)
            return True
        self.client.log("链路 %d：收到未知 opcode %s（流 0），断开" % (self.index, frame_name(opcode)))
        return False

    def _stream(self, stream_id: int) -> Optional[TunnelSocket]:
        with self.streams_lock:
            return self.streams.get(stream_id)

    def _apply_backpressure(self) -> None:
        """积压过多时暂停读 WS，等上层消费掉一部分再继续（规范 3.4）。"""
        while not self.closed and self.pending_bytes() > MAX_PENDING_BYTES:
            with self._drain:
                self._drain.wait(0.5)

    def _teardown(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.connected = False
        self.down_reason = reason
        if self.ws is not None:
            self.ws.close()
        with self.streams_lock:
            streams = list(self.streams.values())
            self.streams.clear()
        for stream in streams:
            stream._on_reset(reason)
        self.client.on_link_dead(self, reason)

    def close(self, reason: str = "主动关闭") -> None:
        self._teardown(reason)


class TunnelClient:
    """隧道客户端：对上层只暴露 ``connect(host, port) -> TunnelSocket``。"""

    def __init__(
        self,
        url: str,
        token: str = "",
        *,
        links: int = 4,
        max_streams: int = 6,
        open_timeout: float = 30.0,
        idle_timeout: float = 300.0,
        keepalive: float = 30.0,
        auto_reconnect: bool = True,
        on_log=None,
        **ws_kwargs,
    ) -> None:
        self.url = url
        self.token = token
        self.links = max(1, int(links))
        self.max_streams = max(1, int(max_streams))
        self.open_timeout = float(open_timeout)
        self.idle_timeout = float(idle_timeout)
        self.keepalive = max(5.0, float(keepalive))
        self.auto_reconnect = bool(auto_reconnect)
        self._ws_kwargs = ws_kwargs
        self._on_log = on_log

        self._links: List[Link] = []
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._closed = False
        self._maintenance: Optional[threading.Thread] = None
        self._backoff = 1.0

        self.total_streams = 0
        self.total_reconnects = 0
        self.last_error = ""

    # ------------------------------------------------------------ 日志
    def log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    def make_ws(self) -> WSClient:
        kwargs = dict(self._ws_kwargs)
        kwargs.setdefault("timeout", 30.0)
        return WSClient(self.url, token=self.token, on_log=self.log, **kwargs)  # type: ignore[arg-type]

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """建立第一条链路（失败即抛错），并启动维护线程。"""
        link = self._new_link()
        self.log("隧道已就绪：%s（每链路上限 %d 条流，最多 %d 条链路）"
                 % (self.url, self.max_streams, self.links))
        assert link is not None
        self._maintenance = threading.Thread(target=self._maintain, name="tsu-maintain",
                                             daemon=True)
        self._maintenance.start()

    def _new_link(self) -> Link:
        with self._lock:
            if len(self._links) >= self.links:
                raise TunnelUnavailable("链路数已达上限 %d" % self.links)
            index = len(self._links)
        link = Link(self, index)
        try:
            link.start()
        except (WebSocketError, OSError) as exc:
            raise TunnelUnavailable("无法连接中继 %s：%s" % (self.url, exc)) from exc
        with self._lock:
            self._links.append(link)
        self.notify_capacity()
        return link

    def close(self) -> None:
        self._closed = True
        with self._lock:
            links = list(self._links)
            self._links = []
        for link in links:
            link.close("客户端退出")
        with self._cond:
            self._cond.notify_all()

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------ 连接
    def connect(self, host: str, port: int, timeout: Optional[float] = None) -> TunnelSocket:
        """开一条新流到 ``host:port``。错误都是 ``TunnelError``（带建议的 SOCKS 应答码）。"""
        total = float(timeout or self.open_timeout)
        deadline = time.time() + total
        attempts = 0
        last_error: Optional[TunnelError] = None
        while time.time() < deadline:
            link = self._pick()
            if link is None:
                if not self._has_link_room() and not self._wait_capacity(deadline - time.time()):
                    break
                try:
                    self._new_link()
                except TunnelUnavailable as exc:
                    last_error = exc
                    self.last_error = str(exc)
                    time.sleep(min(0.5, max(0.05, deadline - time.time())))
                continue
            try:
                stream = link.open_stream(host, port, timeout=max(0.5, deadline - time.time()))
                self.total_streams += 1
                return stream
            except _TooManyStreams:
                attempts += 1
                if attempts > 3:
                    last_error = TunnelUnavailable("中继并发已满，且换链路重试 3 次仍失败")
                    break
                continue
            except TunnelError:
                raise
            except (WebSocketError, ProtocolError, OSError) as exc:
                last_error = TunnelUnavailable("链路故障：%s" % exc)
                continue
        if last_error is not None:
            self.last_error = str(last_error)
            raise last_error
        raise TunnelUnavailable("等待可用的隧道连接超时（%.0fs）" % total)

    def _has_link_room(self) -> bool:
        with self._lock:
            return len(self._links) < self.links

    def _pick(self) -> Optional[Link]:
        with self._lock:
            candidates = [link for link in self._links if link.capacity() > 0]
        if not candidates:
            return None
        # 负载最低优先；一样低时用最早的链路（更稳定，避免频繁新建）
        return min(candidates, key=lambda link: (link.load(), link.index))

    def _wait_capacity(self, timeout: float) -> bool:
        if timeout <= 0:
            return False
        with self._cond:
            return self._cond.wait(timeout)

    def notify_capacity(self) -> None:
        with self._cond:
            self._cond.notify_all()

    # ------------------------------------------------------------ 维护
    def on_link_dead(self, link: Link, reason: str) -> None:
        with self._lock:
            if link in self._links:
                self._links.remove(link)
        self.log("链路 %d 断开：%s" % (link.index, reason or "未知原因"))
        self.notify_capacity()

    def _maintain(self) -> None:
        while not self._closed:
            time.sleep(1.0)
            if self._closed:
                break
            if not self.auto_reconnect:
                continue
            with self._lock:
                alive = [link for link in self._links if link.is_open]
            if alive:
                self._backoff = 1.0
                continue
            with self._lock:
                if len(self._links) >= self.links:
                    continue
            try:
                link = self._new_link()
                link.reconnects += 1
                self.total_reconnects += 1
                self._backoff = 1.0
            except TunnelUnavailable as exc:
                self.last_error = str(exc)
                self.log("重连中继失败（%.0fs 后重试）：%s" % (self._backoff, exc))
                time.sleep(self._backoff)
                self._backoff = min(30.0, self._backoff * 2)

    # ------------------------------------------------------------ 统计
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            links = list(self._links)
        live = [link for link in links if link.is_open]
        streams = sum(link.load() for link in links)
        capacity = sum(link.capacity() for link in links)
        return {
            "url": self.url,
            "links": len(links),
            "links_live": len(live),
            "links_max": self.links,
            "streams": streams,
            "streams_max": self.max_streams * self.links,
            "capacity_free": capacity,
            "streams_total": self.total_streams,
            "reconnects": self.total_reconnects,
            "last_error": self.last_error,
        }
