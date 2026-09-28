"""隧道流：把 TSU 的一条流包装成「看起来像 socket」的对象。

现有的 SOCKS5 服务端（``torsocks5.socks5.server``）在转发时对上游 socket 只用到
``settimeout`` / ``setsockopt`` / ``sendall`` / ``recv`` / ``shutdown`` / ``close``，
所以这里实现这几个方法就够，不需要真的占一个 fd。这样隧道数据可以**直接**进入
现有的双向泵，不用再套一层本地 SOCKS5（少一次回环往返）。
"""

from __future__ import annotations

import io
import socket
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Optional

from .protocol import SEND_CHUNK, chunk_payload

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from .client import Link

#: 单条流允许在上层未读取时积压的字节数，超过就让链路暂停读 WS（背压）
MAX_PENDING_BYTES = 1024 * 1024


class TunnelSocket:
    """一条隧道流，接口兼容 ``socket.socket`` 的常用子集。"""

    def __init__(self, link: "Link", stream_id: int, host: str, port: int,
                 *, idle_timeout: float = 300.0) -> None:
        self.link = link
        self.sid = stream_id
        self.host = host
        self.port = port
        self.label = "%s:%d" % (host, port)
        self.created = time.time()
        self.timeout: Optional[float] = idle_timeout
        self.bytes_up = 0
        self.bytes_down = 0

        self._cond = threading.Condition()
        self._chunks: deque = deque()
        self._pending = 0
        self._eof = False
        self._closed = False
        self._local_close_sent = False
        self._discard = False
        self._error: Optional[BaseException] = None

        #: OPEN 结果：成功时 ``open_error is None``，失败时放入 TunnelError
        self.open_event = threading.Event()
        self.open_error: Optional[BaseException] = None

    # ------------------------------------------------------ 链路读取线程调用
    def _set_open_result(self, error: Optional[BaseException]) -> None:
        """中继回了 OPEN_OK / OPEN_ERR。"""
        self.open_error = error
        self.open_event.set()

    def _on_data(self, data: bytes) -> None:
        if not data:
            return
        with self._cond:
            if self._discard or self._closed:
                return
            self.bytes_down += len(data)
            self._chunks.append(data)
            self._pending += len(data)
            self._cond.notify_all()

    def _on_remote_close(self) -> None:
        """对端发来 CLOSE：半关闭，读侧见到 EOF。"""
        with self._cond:
            self._eof = True
            self._cond.notify_all()
            finished = self._local_close_sent or self._closed
        if finished:
            self.link.release(self.sid)

    def _on_reset(self, reason: str = "") -> None:
        """对端发来 RESET 或链路断开：直接结束。"""
        with self._cond:
            self._eof = True
            if reason and self._error is None:
                self._error = OSError("隧道流被重置: %s" % reason)
            self._cond.notify_all()
        # 还没等到 OPEN 结果的流，也要让等待方立刻醒来
        if not self.open_event.is_set():
            from .client import TargetUnreachable

            self._set_open_result(TargetUnreachable("隧道链路在建立连接时断开：%s" % reason))

    # ------------------------------------------------------ 上层调用
    @property
    def pending(self) -> int:
        with self._cond:
            return self._pending

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    def pending_bytes(self) -> int:
        return self.pending

    def settimeout(self, value: Optional[float]) -> None:
        self.timeout = value

    def gettimeout(self) -> Optional[float]:
        return self.timeout

    def setsockopt(self, *_args: Any, **_kwargs: Any) -> None:
        """隧道没有内核 socket 可调，静默忽略（保持 socket 兼容）。"""

    def getsockname(self) -> tuple:
        return (self.link.local_address(), self.sid)

    def getpeername(self) -> tuple:
        return (self.host, self.port)

    def fileno(self) -> int:
        raise io.UnsupportedOperation("隧道流没有文件描述符")

    def sendall(self, data: bytes, flags: int = 0) -> None:
        if self._closed:
            raise OSError("隧道流已关闭")
        with self._cond:
            if self._local_close_sent:
                raise OSError("隧道流已半关闭（本地已发送 CLOSE）")
        if not data:
            return
        self.bytes_up += len(data)
        for piece in chunk_payload(bytes(data), SEND_CHUNK):
            self.link.send_data(self.sid, piece)

    def send(self, data: bytes, flags: int = 0) -> int:
        self.sendall(data, flags)
        return len(data)

    def recv(self, bufsize: int = 65536, flags: int = 0) -> bytes:
        deadline = None if not self.timeout else time.time() + float(self.timeout)
        with self._cond:
            while True:
                if self._chunks:
                    chunk = self._chunks.popleft()
                    self._pending -= len(chunk)
                    self.link.notify_drained()
                    if len(chunk) > bufsize:
                        # 多出来的塞回去，下一次接着读
                        self._chunks.appendleft(chunk[bufsize:])
                        self._pending += len(chunk) - bufsize
                        return chunk[:bufsize]
                    return chunk
                if self._eof or self._closed:
                    if self._error is not None:
                        error = self._error
                        self._error = None
                        raise error if isinstance(error, OSError) else OSError(str(error))
                    return b""
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise socket.timeout("隧道流读取超时")
                    self._cond.wait(remaining)
                else:
                    self._cond.wait(1.0)

    def recv_into(self, buffer, nbytes: int = 0) -> int:
        data = self.recv(nbytes or len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def shutdown(self, how: int = socket.SHUT_WR) -> None:
        # SHUT_WR 只表示「我不再写了」，**不能**清掉已经收到的数据——
        # 对端可能早就回了一批（HTTP 就是边收边回），清掉就是丢包。
        if how in (socket.SHUT_WR, socket.SHUT_RDWR):
            with self._cond:
                if self._closed or self._local_close_sent:
                    return
                self._local_close_sent = True
            self.link.send_close(self.sid)
        if how in (socket.SHUT_RD, socket.SHUT_RDWR):
            with self._cond:
                self._discard = True
                self._chunks.clear()
                self._pending = 0
                self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._chunks.clear()
            self._pending = 0
            self._eof = True
            graceful = self._local_close_sent
            self._cond.notify_all()
        if graceful:
            self.link.send_close(self.sid)
        else:
            self.link.send_reset(self.sid)
        self.link.release(self.sid)

    def __enter__(self) -> "TunnelSocket":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return "<TunnelSocket %s sid=%d up=%d down=%d>" % (
            self.label, self.sid, self.bytes_up, self.bytes_down)


def new_stream_id(counter) -> int:
    """从 ``counter`` 取一个新的流 id（调用方保证线程安全）。"""
    return next(counter)
