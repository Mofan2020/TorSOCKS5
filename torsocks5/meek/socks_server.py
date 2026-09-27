"""tor 与 meek 传输之间的一层：SOCKS5 服务端 + 可插拔传输（PT）参数解析。

tor 启动传输插件后，会连接插件公布的 SOCKS5 地址，并按 PT 规范把网桥参数
（``url=``、``front=`` 等）通过 RFC 1929 的 username/password 字段传进来。
本模块负责解析这些参数、建立 meek 通道，然后双向转发字节。
"""

from __future__ import annotations

import socket
import sys
import threading
from typing import Callable, Dict, Optional

from .channel import MeekChannel, MeekError

SOCKS_VERSION = 0x05
AUTH_NONE = 0x00
AUTH_USERNAME = 0x02
AUTH_UNACCEPTABLE = 0xFF
CMD_CONNECT = 0x01
REP_SUCCESS = 0x00
ATYP_V4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_V6 = 0x04

# 网桥行里可能出现的参数别名 -> 内部统一名称。
ARG_ALIASES = {
    "url": "url",
    "front": "front",
    "cdnfronting": "front",       # 老版本 meek 网桥行用 CDNFronting=
    "meekurl": "url",
    "utls": "utls",               # 识别但忽略（Python 无法模拟浏览器 TLS 指纹）
    "fragments": "fragments",
    "ignorepqc": "ignorePQC",
}


def parse_pt_args(raw: str) -> Dict[str, str]:
    """解析 PT 参数串。

    官方 goptlib 用 ``;`` / ``=`` 作为分隔符（支持反斜杠转义），而 tor 的
    网桥行本身用空格分隔。这里两种写法都接受，容错性更好。
    """
    args: Dict[str, str] = {}
    buf: list = []
    for ch in raw:
        if ch in ";\t\r\n":
            token = "".join(buf).strip()
            buf = []
            if token:
                args.update(_split_token(token))
            continue
        if ch == "\\" and buf:
            buf.append(ch)  # 保留转义符，交给 _split_token 处理
            continue
        buf.append(ch)
    token = "".join(buf).strip()
    if token:
        args.update(_split_token(token))

    normalized: Dict[str, str] = {}
    for key, value in args.items():
        canonical = ARG_ALIASES.get(key.lower())
        if canonical and canonical not in normalized:
            normalized[canonical] = value
    return normalized


def _split_token(token: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in token.split(" "):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        out[key.strip()] = value.strip()
    return out


class TransportSocksServer:
    """监听 127.0.0.1，供 tor 连接，并为每条连接建立一条 meek 隧道。"""

    def __init__(
        self,
        default_url: Optional[str] = None,
        default_front: Optional[str] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        connect_timeout: float = 20.0,
        read_timeout: float = 30.0,
    ) -> None:
        self.default_url = default_url
        self.default_front = default_front
        self._on_log = on_log
        self._on_error = on_error
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._listener: Optional[socket.socket] = None
        self._closed = threading.Event()
        self._threads: list = []

    # ------------------------------------------------------------------ 日志
    def _log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    def _error(self, message: str) -> None:
        text = "错误: " + message
        if self._on_error is not None:
            self._on_error(text)
        else:
            print("[meek] %s" % text, file=sys.stderr, flush=True)

    # ------------------------------------------------------------------ 监听
    def listen(self, host: str = "127.0.0.1", port: int = 0) -> str:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(64)
        self._listener = listener
        return "%s:%d" % listener.getsockname()[:2]

    @property
    def address(self) -> str:
        if self._listener is None:
            raise RuntimeError("尚未监听")
        return "%s:%d" % self._listener.getsockname()[:2]

    def serve_forever(self) -> None:
        if self._listener is None:
            self.listen()
        assert self._listener is not None
        self._listener.settimeout(0.5)
        while not self._closed.is_set():
            try:
                conn, addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(
                target=self._handle, args=(conn, addr), name="meek-conn", daemon=True
            )
            thread.start()
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)

    def close(self) -> None:
        self._closed.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None

    # ------------------------------------------------------------------ 握手
    @staticmethod
    def _recv_exactly(conn: socket.socket, want: int) -> bytes:
        chunks = []
        got = 0
        while got < want:
            chunk = conn.recv(want - got)
            if not chunk:
                raise ConnectionError("连接在握手过程中被关闭")
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)

    def _negotiate(self, conn: socket.socket) -> str:
        """完成 SOCKS5 认证协商，返回拼接后的 PT 参数串。"""
        head = self._recv_exactly(conn, 2)
        if head[0] != SOCKS_VERSION:
            raise ValueError("SOCKS 版本不是 5: %d" % head[0])
        methods = self._recv_exactly(conn, head[1])
        if AUTH_USERNAME in methods:
            conn.sendall(bytes([SOCKS_VERSION, AUTH_USERNAME]))
            ver = self._recv_exactly(conn, 1)[0]
            if ver != 0x01:
                raise ValueError("RFC 1929 版本不是 1: %d" % ver)
            ulen = self._recv_exactly(conn, 1)[0]
            if ulen < 1:
                raise ValueError("RFC 1929 用户名长度为 0")
            username = self._recv_exactly(conn, ulen)
            plen = self._recv_exactly(conn, 1)[0]
            if plen < 1:
                raise ValueError("RFC 1929 密码长度为 0")
            password = self._recv_exactly(conn, plen)
            # tor 在没有参数时会发一个 NUL 字节作为密码。
            if not (plen == 1 and password == b"\x00"):
                conn.sendall(bytes([0x01, 0x00]))
                return (username + password).decode("utf-8", "replace")
            conn.sendall(bytes([0x01, 0x00]))
            return username.decode("utf-8", "replace")
        if AUTH_NONE in methods:
            conn.sendall(bytes([SOCKS_VERSION, AUTH_NONE]))
            return ""
        conn.sendall(bytes([SOCKS_VERSION, AUTH_UNACCEPTABLE]))
        raise ValueError("没有可用的 SOCKS5 认证方式")

    @staticmethod
    def _read_connect_request(conn: socket.socket) -> str:
        head = TransportSocksServer._recv_exactly(conn, 4)
        if head[0] != SOCKS_VERSION:
            raise ValueError("请求版本不是 5")
        if head[1] != CMD_CONNECT:
            raise ValueError("只支持 CONNECT（收到 0x%02x）" % head[1])
        atyp = head[3]
        if atyp == ATYP_V4:
            host = socket.inet_ntoa(TransportSocksServer._recv_exactly(conn, 4))
        elif atyp == ATYP_V6:
            host = socket.inet_ntop(
                socket.AF_INET6, TransportSocksServer._recv_exactly(conn, 16)
            )
        elif atyp == ATYP_DOMAIN:
            length = TransportSocksServer._recv_exactly(conn, 1)[0]
            host = TransportSocksServer._recv_exactly(conn, length).decode("idna", "replace")
        else:
            raise ValueError("未知地址类型 0x%02x" % atyp)
        port = int.from_bytes(TransportSocksServer._recv_exactly(conn, 2), "big")
        return "%s:%d" % (host, port)

    @staticmethod
    def _reply(conn: socket.socket, code: int = REP_SUCCESS) -> None:
        conn.sendall(bytes([SOCKS_VERSION, code, 0x00, ATYP_V4, 0, 0, 0, 0, 0, 0]))

    # ------------------------------------------------------------------ 连接处理
    def _handle(self, conn: socket.socket, addr) -> None:
        channel: Optional[MeekChannel] = None
        try:
            conn.settimeout(30.0)
            raw_args = self._negotiate(conn)
            args = parse_pt_args(raw_args)
            target = self._read_connect_request(conn)
            url = args.get("url") or self.default_url
            front = args.get("front") or self.default_front
            if not url:
                self._error("SOCKS 请求里没有 url= 参数（网桥行缺少 url=）")
                self._reply(conn, 0x01)
                return
            self._log(
                "新建 meek 通道：目标=%s url=%s front=%s%s"
                % (target, url, front or "(同 url)", " [utls 被忽略]" if args.get("utls") else "")
            )
            self._reply(conn)
            conn.settimeout(None)
            channel = MeekChannel(
                url,
                front=front,
                connect_timeout=self._connect_timeout,
                read_timeout=self._read_timeout,
                on_status=self._log,
                on_error=self._error,
            )
            channel.start()
            self._pump(conn, channel)
        except (OSError, ValueError, MeekError) as exc:
            self._error("meek 连接处理失败（来自 %s）: %s" % (addr[0] if addr else "?", exc))
        finally:
            if channel is not None:
                channel.close()
            try:
                conn.close()
            except OSError:
                pass
            self._log("meek 通道已关闭")

    @staticmethod
    def _pump(sock: socket.socket, channel: MeekChannel) -> None:
        done = threading.Event()

        def sock_to_meek() -> None:
            try:
                while not done.is_set():
                    data = sock.recv(65536)
                    if not data:
                        break
                    channel.send(data)
            except OSError:
                pass
            finally:
                done.set()

        def meek_to_sock() -> None:
            try:
                while not done.is_set():
                    data = channel.receive()
                    if not data:
                        break
                    sock.sendall(data)
            except (OSError, MeekError):
                pass
            finally:
                done.set()

        t1 = threading.Thread(target=sock_to_meek, name="meek-up", daemon=True)
        t2 = threading.Thread(target=meek_to_sock, name="meek-down", daemon=True)
        t1.start()
        t2.start()
        while not done.is_set():
            done.wait(0.5)
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)
