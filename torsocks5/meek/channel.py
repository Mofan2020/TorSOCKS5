"""meek 通道：把一条 TCP 流编码成一串 HTTP(S) 请求/响应（域前置 domain fronting）。

协议要点（与 Tor 官方 meek 传输 0.38.x / meek_lite 完全一致）：

* 客户端 → 服务器：``POST <url>``，请求体是原始 Tor 字节（单次最多 65536 字节）。
* 请求头必须包含：

  - ``Content-Type: application/octet-stream``
  - ``X-Session-Id: <会话 id>``（8 字节随机数做 base64、去掉 ``=`` 补位）
  - ``Host: <url 中的真实主机>``（启用 front 时与实际连接的主机不同）

* 服务器 → 客户端：``200 OK``，响应体是回传给 Tor 的原始字节（同样最多 65536）。
* 服务器无法主动推送，因此客户端必须轮询：空闲时 100ms 起、按 1.5 倍退避、最长 5s；
  一旦有数据收发就立刻再发一次。
* front（域前置）：DNS 解析、TCP 连接、TLS SNI 都用 front 域名，而 HTTP 的
  ``Host`` 头仍然是 url 里的真实域名，由 CDN 按 Host 转发到 meek 网桥。

参考实现：https://git.torproject.org/pluggable-transports/meek.git
"""

from __future__ import annotations

import base64
import os
import queue
import socket
import ssl
import threading
import time
from typing import IO, Callable, List, Optional, Tuple, cast
from urllib.parse import urlsplit, urlunsplit

# 与官方实现保持一致的协议常量。
MAX_PAYLOAD = 0x10000  # 单次请求/响应承载的最大字节数
SESSION_ID_BYTES = 8
INIT_POLL_INTERVAL = 0.1
MAX_POLL_INTERVAL = 5.0
POLL_INTERVAL_MULTIPLIER = 1.5
MAX_TRIES = 10
RETRY_DELAY = 30.0
STATS_INTERVAL = 15.0

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class MeekError(Exception):
    """meek 通道相关错误的基类。"""


class MeekConnectError(MeekError):
    """连接/握手阶段失败（尚未发送有效数据，可以安全重试）。"""


class MeekProtocolError(MeekError):
    """协议层或 HTTP 状态异常。"""


def gen_session_id() -> str:
    """生成会话 id：8 字节随机数的 base64（去掉 ``=``）。"""
    raw = os.urandom(SESSION_ID_BYTES)
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def _default_tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # meek 网桥只走 HTTP/1.1；显式声明可避免部分 CDN 的 HTTP/2 行为差异。
    try:
        ctx.set_alpn_protocols(["http/1.1"])
    except NotImplementedError:  # 极少数构建没有 ALPN
        pass
    return ctx


def _open_socket(host: str, port: int, timeout: float) -> socket.socket:
    last: Optional[BaseException] = None
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:  # DNS 失败
        raise MeekConnectError("DNS 解析 %s 失败: %s" % (host, exc)) from exc
    for family, socktype, proto, _canon, addr in infos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(timeout)
            sock.connect(addr)
            return sock
        except OSError as exc:  # 逐个地址尝试
            last = exc
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    raise MeekConnectError("连接 %s:%d 失败: %s" % (host, port, last))


class _Http:
    """极简 HTTP/1.1 客户端：只实现 meek 用到的 POST + 读响应体。

    之所以不用 ``http.client``，是因为需要精确控制 ``Host`` 头（域前置时与
    连接目标不同），并且要保证请求体以 ``Content-Length`` 一次性发出。
    """

    def __init__(
        self,
        url: str,
        front: Optional[str] = None,
        connect_timeout: float = 20.0,
        read_timeout: float = 30.0,
        busy_read_timeout: Optional[float] = None,
        user_agent: Optional[str] = DEFAULT_USER_AGENT,
    ) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise MeekConnectError("url 协议不受支持: %r（只支持 http/https）" % parts.scheme)
        if not parts.hostname:
            raise MeekConnectError("url 缺少主机名: %r" % url)
        self.scheme = parts.scheme
        self.host_header = parts.netloc
        self.path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
        default_port = 443 if self.scheme == "https" else 80
        self.port = parts.port or default_port
        self.dial_host = front or parts.hostname
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._busy_read_timeout = (
            busy_read_timeout if busy_read_timeout is not None else max(read_timeout, 120.0)
        )
        self.user_agent = user_agent
        self._sock: Optional[socket.socket] = None
        self._rfile: Optional[IO[bytes]] = None
        self._version = "HTTP/1.1"
        self._closed = False

    # ------------------------------------------------------------------ 底层
    def _ensure_socket(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        sock = _open_socket(self.dial_host, self.port, self.connect_timeout)
        if self.scheme == "https":
            ctx = _default_tls_context()
            # SNI 使用 front 域名（域前置的核心），证书也按 front 域名校验。
            try:
                sock = ctx.wrap_socket(sock, server_hostname=self.dial_host)
            except (ssl.SSLError, OSError) as exc:
                try:
                    sock.close()
                except OSError:
                    pass
                raise MeekConnectError(
                    "TLS 握手失败 (%s): %s" % (self.dial_host, exc)
                ) from exc
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        sock.settimeout(self.read_timeout)
        self._sock = sock
        self._rfile = cast(IO[bytes], sock.makefile("rb", buffering=64 * 1024))
        return sock

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._rfile is not None:
            try:
                self._rfile.close()
            except OSError:
                pass
            self._rfile = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ------------------------------------------------------------------ 请求
    def _read_line(self) -> bytes:
        """读一整行。

        真实链路上（尤其是 CDN 在中间重置连接时）偶尔会收到残缺的行，
        这里统一按「连接已损坏」处理，交由上层重连。
        """
        assert self._rfile is not None
        line = self._rfile.readline(8192)
        if not line:
            raise MeekProtocolError("服务器提前关闭了连接")
        if not line.endswith(b"\n"):
            # 没有换行结尾：可能是连接被中途截断
            raise MeekProtocolError("响应行不完整: %r" % line[:40])
        return line.rstrip(b"\r\n")

    def _read_headers(self) -> List[Tuple[str, str]]:
        headers: List[Tuple[str, str]] = []
        while True:
            line = self._read_line()
            if not line:
                break
            if b":" not in line:
                continue
            name, _, value = line.partition(b":")
            headers.append((name.decode("latin-1").strip(), value.decode("latin-1").strip()))
        return headers

    def post(self, body: bytes, session_id: str) -> Tuple[int, bytes]:
        """发送一次 POST，返回 ``(状态码, 响应体)``。"""
        sock = self._ensure_socket()
        # 有数据要发时说明通道是「忙」的，给更长的时间等响应
        try:
            sock.settimeout(self._busy_read_timeout if body else self.read_timeout)
        except OSError:
            pass
        req = [
            "POST %s HTTP/1.1" % self.path,
            "Host: %s" % self.host_header,
            "Content-Type: application/octet-stream",
            "X-Session-Id: %s" % session_id,
            "Content-Length: %d" % len(body),
            "Accept-Encoding: identity",
        ]
        if self.user_agent:
            req.append("User-Agent: %s" % self.user_agent)
        req.append("Connection: keep-alive")
        head = ("\r\n".join(req) + "\r\n\r\n").encode("latin-1")
        try:
            sock.sendall(head + body)
        except socket.timeout as exc:
            self.close()
            raise MeekConnectError("发送请求超时: %s" % exc) from exc
        except OSError as exc:
            self.close()
            raise MeekConnectError("发送请求失败: %s" % exc) from exc

        status_line = self._read_line()
        parts = status_line.split(None, 2)
        if len(parts) < 2 or not parts[0].upper().startswith(b"HTTP/"):
            self.close()
            # 出现残缺响应时按「连接损坏」处理，重连即可恢复
            raise MeekConnectError("无法解析状态行: %r" % status_line[:40])
        self._version = parts[0].upper().decode("latin-1")
        try:
            status = int(parts[1])
        except ValueError as exc:
            self.close()
            raise MeekProtocolError("无法解析状态码: %r" % status_line) from exc
        headers = self._read_headers()

        lowered = {name.lower(): value for name, value in headers}
        encoding = lowered.get("transfer-encoding", "").lower()
        if "chunked" in encoding:
            data = self._read_chunked()
        else:
            length = lowered.get("content-length")
            if length is not None:
                try:
                    want = int(length)
                except ValueError:
                    self.close()
                    raise MeekConnectError("非法的 Content-Length: %r" % length) from None
                data = self._read_exactly(min(want, MAX_PAYLOAD))
                if want > MAX_PAYLOAD:
                    # 超额数据必须丢弃，否则连接会错位
                    self._discard(want - MAX_PAYLOAD)
            elif lowered.get("connection", "").lower() == "close" or (
                self._version == "HTTP/1.0"
            ):
                # 无长度且即将关闭：读到 EOF 即可
                data = self._read_to_eof()
            else:
                # 无长度但保持连接：无法确定边界，按「连接不可复用」处理
                self.close()
                raise MeekProtocolError("响应既无 Content-Length 也非 chunked")
        if lowered.get("connection", "").lower() == "close":
            self.close()
        return status, data

    def _discard(self, count: int) -> None:
        assert self._rfile is not None
        remaining = count
        while remaining > 0:
            chunk = self._rfile.read(min(remaining, 65536))
            if not chunk:
                self.close()
                raise MeekConnectError("丢弃超额数据时连接被关闭")
            remaining -= len(chunk)

    def _read_to_eof(self) -> bytes:
        assert self._rfile is not None
        out = bytearray()
        while len(out) <= MAX_PAYLOAD:
            chunk = self._rfile.read(65536)
            if not chunk:
                break
            out += chunk
        self.close()
        return bytes(out[:MAX_PAYLOAD])

    def _read_exactly(self, want: int) -> bytes:
        assert self._rfile is not None
        chunks: List[bytes] = []
        got = 0
        while got < want:
            try:
                chunk = self._rfile.read(want - got)
            except socket.timeout as exc:
                # 超时属于「连接层面的问题」，交给上层重连，而不是直接终止通道
                self.close()
                raise MeekConnectError("读取响应超时: %s" % exc) from exc
            except OSError as exc:
                self.close()
                raise MeekConnectError("读取响应失败: %s" % exc) from exc
            if not chunk:
                self.close()
                raise MeekConnectError("响应体不完整（收到 %d/%d 字节）" % (got, want))
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)

    def _read_chunked(self) -> bytes:
        """读取 chunked 响应体。

        必须**完整消费**到终止块（含 ``0\\r\\n`` 与 trailer），否则连接上的残留
        字节会被下一个请求当成状态行，导致 keep-alive 连接永久错位。
        这里允许读超额数据（丢弃），但一定要把流读到结尾。
        """
        assert self._rfile is not None
        out = bytearray()
        while True:
            line = self._read_line()
            size_text = line.split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError as exc:
                self.close()
                raise MeekConnectError("非法的分块长度: %r" % size_text) from exc
            if size == 0:
                # 终止块：继续读完 trailer（可能为空）直到空行
                while True:
                    trailer = self._read_line()
                    if not trailer:
                        break
                return bytes(out[:MAX_PAYLOAD])
            out += self._read_exactly(size)
            self._read_line()  # 块数据后的 CRLF


class MeekChannel:
    """一条通过 meek 隧道传输的字节流（对上层来说就是一个 ``net.Conn``）。"""

    def __init__(
        self,
        url: str,
        front: Optional[str] = None,
        *,
        connect_timeout: float = 20.0,
        read_timeout: float = 30.0,
        busy_read_timeout: Optional[float] = None,
        user_agent: Optional[str] = DEFAULT_USER_AGENT,
        max_tries: int = MAX_TRIES,
        max_reconnects: int = 20,
        retry_delay: float = RETRY_DELAY,
        on_status: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.url = url
        self.front = front
        self.session_id = gen_session_id()
        self._http = _Http(
            url,
            front=front,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            busy_read_timeout=busy_read_timeout,
            user_agent=user_agent,
        )
        self._max_tries = max_tries
        self._max_retries = max(1, min(max_tries, 6))
        self._max_reconnects = max_reconnects
        self._retry_delay = retry_delay
        # meek 链路的 RTT 很高（每次往返都可能好几秒）。空闲轮询用较短的超时
        # 便于及时发现断连；有数据在传时则放宽，避免大响应被误判为超时。
        self._busy_read_timeout = (
            busy_read_timeout if busy_read_timeout is not None else max(read_timeout, 120.0)
        )
        self._on_status = on_status
        self._on_error = on_error
        self._wr: queue.Queue[Optional[bytes]] = queue.Queue()
        self._rd: queue.Queue[object] = queue.Queue()
        self._leftover = b""
        self._closed = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self.bytes_sent = 0
        self.bytes_received = 0
        self.polls = 0

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._io_worker, name="meek-io", daemon=True
        )
        self._worker.start()

    def _status(self, message: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(message)
            except Exception:  # 日志回调不应影响传输
                pass

    def _round_trip(self, payload: bytes) -> bytes:
        """一次 HTTP 往返，带重试与重连。

        重试策略（参考官方 meek-client 并针对 CDN 行为做了优化）：

        * 连接层错误（keep-alive 被 CDN 掐断等）——换新连接重试，这类错误
          发生时请求通常还没被服务端处理，重试是安全的；
        * 4xx（403/404…）——请求被 CDN 或网桥拒绝，重试没有意义，立刻失败，
          让 tor 尽快换下一条网桥；
        * 5xx（含部分 CDN 映射出来的 570）——短退避重试（官方实现固定等 30 秒，
          在这种链路上慢到无法接受）。
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                status, data = self._http.post(payload, self.session_id)
            except MeekConnectError as exc:
                self._http.close()
                if attempt > self._max_reconnects:
                    raise
                self._status(
                    "连接中断（%s），%.1fs 后重连重试（%d/%d）"
                    % (exc, min(0.3 * attempt, 2.0), attempt, self._max_reconnects)
                )
                if self._closed.is_set():
                    raise
                time.sleep(min(0.3 * attempt, 2.0))
                continue
            except MeekProtocolError:
                self._http.close()
                raise
            if status == 200:
                return data
            if 400 <= status < 500 and status not in (408, 429):
                self._http.close()
                raise MeekProtocolError(
                    "HTTP %d：请求被拒绝，通常说明这条网桥或它的 CDN 前置已失效" % status
                )
            if attempt > self._max_retries:
                self._http.close()
                raise MeekProtocolError("服务端持续返回 HTTP %d" % status)
            delay = min(0.5 * (2 ** (attempt - 1)), 5.0)
            self._status(
                "HTTP %d，%.1fs 后重试（%d 次剩余）"
                % (status, delay, self._max_retries - attempt + 1)
            )
            self._http.close()
            if self._closed.is_set():
                raise MeekError("通道已关闭")
            time.sleep(delay)

    def _io_worker(self) -> None:
        interval = INIT_POLL_INTERVAL
        pending = b""
        last_stats = time.time()
        try:
            while not self._closed.is_set():
                try:
                    chunk = self._wr.get(timeout=interval)
                except queue.Empty:
                    chunk = None
                if chunk is None and self._closed.is_set():
                    break
                payload = pending + (chunk or b"")
                if len(payload) > MAX_PAYLOAD:
                    send, pending = payload[:MAX_PAYLOAD], payload[MAX_PAYLOAD:]
                else:
                    send, pending = payload, b""
                try:
                    data = self._round_trip(send)
                except MeekError as exc:
                    self._fail(exc)
                    return
                self.polls += 1
                self.bytes_sent += len(send)
                self.bytes_received += len(data)
                if data:
                    self._rd.put(data)
                if data or send:
                    interval = 0.0
                elif interval == 0.0:
                    interval = INIT_POLL_INTERVAL
                else:
                    interval = min(interval * POLL_INTERVAL_MULTIPLIER, MAX_POLL_INTERVAL)
                now = time.time()
                if self._on_status is not None and now - last_stats >= STATS_INTERVAL:
                    last_stats = now
                    self._status(
                        "统计: 轮询 %d 次，发送 %d 字节，接收 %d 字节，轮询间隔 %.1fs"
                        % (self.polls, self.bytes_sent, self.bytes_received, interval)
                    )
        except Exception as exc:  # 兜底，避免工作线程静默死亡
            self._fail(exc)
        finally:
            self._rd.put(None)
            self._http.close()

    def _fail(self, exc: BaseException) -> None:
        message = "meek 通道错误: %s" % exc
        if self._on_error is not None:
            try:
                self._on_error(message)
            except Exception:  # noqa: BLE001
                pass
        else:
            self._status(message)
        self._rd.put(exc)

    # ------------------------------------------------------------------ 流接口
    def send(self, data: bytes) -> int:
        """把数据交给 meek 隧道（立即返回，实际发送在后台完成）。"""
        if self._closed.is_set():
            raise MeekError("通道已关闭")
        if not data:
            return 0
        self._wr.put(bytes(data))
        return len(data)

    def receive(self, timeout: Optional[float] = None) -> bytes:
        """读取一段来自 meek 隧道的数据；隧道结束返回 ``b""``。"""
        if self._leftover:
            data, self._leftover = self._leftover, b""
            return data
        while True:
            try:
                item = self._rd.get(timeout=timeout)
            except queue.Empty:
                raise MeekError("读取 meek 通道超时") from None
            if item is None:
                return b""
            if isinstance(item, BaseException):
                raise item
            data = item if isinstance(item, bytes) else bytes(item)  # type: ignore[call-overload]
            if not data:
                continue
            return data

    def close(self) -> None:
        self._closed.set()
        self._wr.put(None)
        self._http.close()

    # ------------------------------------------------------------------ 便捷方法
    def round_trip_blocking(self, payload: bytes, timeout: float = 60.0) -> bytes:
        """同步地做一次请求/响应（仅用于连通性自检）。"""
        self.start()
        self._wr.put(payload)
        return self.receive(timeout=timeout)
