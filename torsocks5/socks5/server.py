"""SOCKS5 服务端：把客户端请求转发到本地 tor 的 SOCKS5 端口。

特性：
    * 完整实现 RFC 1928（CONNECT / UDP ASSOCIATE）与 RFC 1929（用户名密码认证）
    * IPv4 / IPv6 / 域名三种地址类型；域名**不在本机解析**，直接交给 tor 解析
    * 访问控制（allow_from，支持 IP / CIDR）、连接数上限、空闲超时
    * 双向流量统计、按需打印日志；UDP 走 tor 的 UDP ASSOCIATE
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import threading
import time
from typing import Callable, List, Optional, Tuple

from . import client as socks_client
from .protocol import (
    ATYP_V4,
    AUTH_NONE,
    AUTH_UNACCEPTABLE,
    AUTH_USERNAME,
    CMD_CONNECT,
    CMD_UDP_ASSOCIATE,
    REP_ADDRESS_TYPE_NOT_SUPPORTED,
    REP_COMMAND_NOT_SUPPORTED,
    REP_CONNECTION_REFUSED,
    REP_GENERAL_FAILURE,
    REP_HOST_UNREACHABLE,
    REP_NOT_ALLOWED,
    REP_SUCCESS,
    UDP_HEADER,
    VERSION,
    SocksError,
    build_reply,
    decode_address,
)

BUFFER_SIZE = 65536


class SocksServerError(Exception):
    pass


class AccessList:
    """基于 IP / CIDR 的访问控制。

    规则按书写顺序匹配，**第一条命中的规则生效**（与主流 ACL 语义一致），
    都没命中则默认拒绝。因此例外规则要写在通用规则前面，例如::

        allow_from = ["127.0.0.1", "::1", "10.0.0.5"]        # 只放行本机与 10.0.0.5
        allow_from = ["0.0.0.0/0", "!10.0.0.5"]             # 全放行但排除 10.0.0.5

    ``!`` 前缀表示拒绝。
    """

    def __init__(self, rules: List[str]) -> None:
        self.entries: List[Tuple[ipaddress._BaseNetwork, bool]] = []
        for rule in rules or []:
            text = str(rule).strip()
            if not text:
                continue
            negate = text.startswith("!")
            if negate:
                text = text[1:]
            try:
                network = ipaddress.ip_network(text, strict=False)
            except ValueError:
                continue
            self.entries.append((network, not negate))

    def allows(self, host: str) -> bool:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        for network, permit in self.entries:
            if address.version == network.version and address in network:
                return permit
        return False


class _Reader:
    """带缓冲的读取器。"""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = b""
        self.eof = False

    def read(self, size: int) -> bytes:
        while len(self.buffer) < size and not self.eof:
            try:
                chunk = self.sock.recv(max(size - len(self.buffer), 4096))
            except (socket.timeout, OSError):
                raise
            if not chunk:
                self.eof = True
                break
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data


class SocksConnection:
    """一条客户端连接。"""

    def __init__(self, sock: socket.socket, peer, server: SocksServer) -> None:
        self.sock = sock
        self.peer = peer
        self.server = server
        self.username = ""
        self.bytes_up = 0
        self.bytes_down = 0
        self.started = time.time()

    # ------------------------------------------------------------------ 工具
    def _log(self, message: str) -> None:
        self.server.log(message)

    def _reply(self, code: int, host: str = "0.0.0.0", port: int = 0) -> None:
        try:
            self.sock.sendall(build_reply(code, host, port))
        except OSError:
            pass

    # ------------------------------------------------------------------ 认证
    def negotiate(self) -> Optional[Tuple[str, int, int]]:
        """完成方法协商与认证，返回 ``(host, port, cmd)``。"""
        reader = _Reader(self.sock)
        head = reader.read(2)
        if len(head) < 2 or head[0] != VERSION:
            # 协议都不对，无法给出合法应答，直接断开
            raise SocksError("SOCKS 版本不正确")
        methods = reader.read(head[1])
        need_auth = self.server.username is not None
        if need_auth and AUTH_USERNAME not in methods:
            self.sock.sendall(bytes([VERSION, AUTH_UNACCEPTABLE]))
            raise SocksError("客户端不支持用户名/密码认证")
        if not need_auth and AUTH_NONE not in methods:
            self.sock.sendall(bytes([VERSION, AUTH_UNACCEPTABLE]))
            raise SocksError("客户端不支持免认证方式")
        self.sock.sendall(bytes([VERSION, AUTH_USERNAME if need_auth else AUTH_NONE]))
        if need_auth:
            ver = reader.read(1)
            if len(ver) != 1 or ver[0] != 0x01:
                raise SocksError("RFC 1929 版本不正确")
            ulen = reader.read(1)
            if not ulen:
                raise SocksError("缺少用户名长度")
            username = reader.read(ulen[0]).decode("utf-8", "replace")
            plen = reader.read(1)
            if not plen:
                raise SocksError("缺少密码长度")
            password = reader.read(plen[0]).decode("utf-8", "replace")
            if username != self.server.username or password != (self.server.password or ""):
                self.sock.sendall(bytes([0x01, 0x01]))
                raise SocksError("用户名或密码错误")
            self.sock.sendall(bytes([0x01, 0x00]))
            self.username = username

        request = reader.read(3)  # VER CMD RSV；ATYP 之后的地址由 decode_address 解析
        if len(request) < 3 or request[0] != VERSION:
            self._reply(REP_GENERAL_FAILURE)
            raise SocksError("请求格式错误")
        command = request[1]
        if command not in (CMD_CONNECT, CMD_UDP_ASSOCIATE):
            self._reply(REP_COMMAND_NOT_SUPPORTED)
            raise SocksError("不支持的命令 0x%02x" % command)
        if request[2] != 0x00:
            self._reply(REP_GENERAL_FAILURE)
            raise SocksError("RSV 字段必须为 0")
        try:
            host, port = decode_address(reader)
        except SocksError as exc:
            self._reply(REP_ADDRESS_TYPE_NOT_SUPPORTED)
            raise SocksError(str(exc)) from None
        return host, port, command

    # ------------------------------------------------------------------ 处理
    def handle(self) -> None:
        self.sock.settimeout(self.server.idle_timeout)
        try:
            result = self.negotiate()
        except SocksError as exc:
            self._log("拒绝 %s：%s" % (self._label(), exc))
            return
        if result is None:
            return
        host, port, command = result
        # 访问控制在方法协商之后、真正转发之前检查，这样回复的是合法的
        # SOCKS5 应答码（0x02 not allowed），而不是被客户端误读的方法选择字节。
        if not self._allowed():
            self._reply(REP_NOT_ALLOWED)
            self._log("拒绝来自 %s 的连接（不在 allow_from 列表）" % self._label())
            return
        if command == CMD_CONNECT:
            self._handle_connect(host, port)
        else:
            self._handle_udp(host, port)

    def _allowed(self) -> bool:
        peer_host = self.peer[0] if self.peer else ""
        if peer_host.startswith("::ffff:") and "." in peer_host:
            peer_host = peer_host[7:]  # IPv4 映射地址
        return self.server.access.allows(peer_host)

    def _label(self) -> str:
        return "%s:%s" % (self.peer[0] if self.peer else "?", self.peer[1] if self.peer else "?")

    def _handle_connect(self, host: str, port: int) -> None:
        target = "%s:%d" % (host, port)
        upstream_addr = self.server.upstream
        if self.server.connector is None and upstream_addr is None:
            # 没有 connector 也没有上游：配置错误，明确报出来而不是抛 TypeError
            self._reply(REP_GENERAL_FAILURE)
            self._log("没有可用的出口：既没有 connector 也没有上游 SOCKS5 地址")
            return
        try:
            if self.server.connector is not None:
                # 隧道路由：直接拿到一个 socket 风格对象（真实 socket 或 TunnelSocket）
                upstream = self.server.connector(host, port)
            else:
                assert upstream_addr is not None
                upstream = socks_client.socks5_connect(
                    upstream_addr,
                    host,
                    port,
                    username=self.server.upstream_username,
                    password=self.server.upstream_password,
                    timeout=self.server.connect_timeout,
                )
        except SocksError as exc:
            self._reply(REP_HOST_UNREACHABLE)
            self._log("连接 %s 失败：%s" % (target, exc))
            return
        except OSError as exc:
            # 隧道路由的异常自带 rep_code（如「目标不在白名单」→ REP_NOT_ALLOWED）
            self._reply(int(getattr(exc, "rep_code", REP_CONNECTION_REFUSED)))
            self._log("连接 %s 失败：%s" % (target, exc))
            return
        except Exception as exc:  # noqa: BLE001 - 兜底：不让一个坏 connector 拖垮服务
            self._reply(REP_GENERAL_FAILURE)
            self._log("连接 %s 出现意外错误：%s" % (target, exc))
            return
        self._reply(REP_SUCCESS, "0.0.0.0", 0)
        self._log("CONNECT %s%s" % (target, "（认证：%s）" % self.username if self.username else ""))
        self._pump(upstream)
        self._log(
            "关闭 %s 用时 %.1fs，↑ %s ↓ %s"
            % (target, time.time() - self.started, _fmt(self.bytes_up), _fmt(self.bytes_down))
        )

    def _pump(self, upstream: socket.socket) -> None:
        upstream.settimeout(self.server.idle_timeout)
        self.sock.settimeout(self.server.idle_timeout)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        done = threading.Event()

        def client_to_tor() -> None:
            try:
                while not done.is_set():
                    data = self.sock.recv(BUFFER_SIZE)
                    if not data:
                        break
                    self.bytes_up += len(data)
                    self.server.bytes_up_total += len(data)
                    upstream.sendall(data)
            except OSError:
                pass
            finally:
                done.set()
                try:
                    upstream.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        def tor_to_client() -> None:
            try:
                while not done.is_set():
                    data = upstream.recv(BUFFER_SIZE)
                    if not data:
                        break
                    self.bytes_down += len(data)
                    self.server.bytes_down_total += len(data)
                    self.sock.sendall(data)
            except OSError:
                pass
            finally:
                done.set()
                try:
                    self.sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        threads = [
            threading.Thread(target=client_to_tor, daemon=True),
            threading.Thread(target=tor_to_client, daemon=True),
        ]
        for thread in threads:
            thread.start()
        while not done.is_set() and threads[0].is_alive() and threads[1].is_alive():
            done.wait(0.5)
        for thread in threads:
            thread.join(timeout=2.0)
        try:
            upstream.close()
        except OSError:
            pass

    # ------------------------------------------------------------------ UDP
    def _handle_udp(self, host: str, port: int) -> None:
        if self.server.connector is not None:
            self._reply(REP_COMMAND_NOT_SUPPORTED)
            self._log("UDP ASSOCIATE 不可用：隧道路由只承载 TCP")
            return
        if not self.server.udp_associate:
            self._reply(REP_COMMAND_NOT_SUPPORTED)
            self._log("UDP ASSOCIATE 已在配置中关闭")
            return
        upstream_addr = self.server.upstream
        if upstream_addr is None:
            self._reply(REP_GENERAL_FAILURE)
            self._log("UDP ASSOCIATE 需要上游 SOCKS5 地址，但当前没有配置")
            return
        try:
            control, relay = socks_client.socks5_udp_associate(
                upstream_addr, timeout=self.server.connect_timeout
            )
        except (SocksError, OSError) as exc:
            self._reply(REP_GENERAL_FAILURE)
            self._log("向 tor 申请 UDP ASSOCIATE 失败：%s" % exc)
            return
        if relay[0] in ("0.0.0.0", ""):
            relay = (upstream_addr[0], relay[1])
        self._reply(REP_SUCCESS, relay[0], relay[1])
        self._log("UDP ASSOCIATE -> %s:%d" % relay)
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.settimeout(2.0)
            udp.connect((relay[0], relay[1]))
        except OSError as exc:
            self._log("创建 UDP 中继失败：%s" % exc)
            return
        peers: dict = {}
        lock = threading.Lock()
        stop = threading.Event()

        def from_tor() -> None:
            while not stop.is_set():
                try:
                    data, sender = udp.recvfrom(BUFFER_SIZE)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data.startswith(UDP_HEADER):
                    continue
                try:
                    peer_host, peer_port, payload = _split_udp(data)
                except SocksError:
                    continue
                with lock:
                    key = (peer_host, peer_port)
                    target = peers.get(key)
                    if target is None:
                        try:
                            target = socket.socket(
                                socket.AF_INET6 if ":" in peer_host else socket.AF_INET,
                                socket.SOCK_DGRAM,
                            )
                            target.settimeout(5.0)
                        except OSError:
                            continue
                        peers[key] = target
                try:
                    target.sendto(payload, (peer_host, peer_port))
                except OSError:
                    continue
                self.bytes_down += len(payload)
                self.server.bytes_down_total += len(payload)

        def to_tor() -> None:
            while not stop.is_set():
                try:
                    data, sender = self.sock.recvfrom(BUFFER_SIZE)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data.startswith(UDP_HEADER):
                    continue
                try:
                    peer_host, peer_port, payload = _split_udp(data)
                except SocksError:
                    continue
                header = UDP_HEADER + b"\x01" + _encode_udp_address(peer_host, peer_port)
                try:
                    udp.send(header + payload)
                except OSError:
                    continue
                with lock:
                    key = (peer_host, peer_port)
                    target = peers.get(key)
                    if target is None:
                        family = socket.AF_INET6 if ":" in peer_host else socket.AF_INET
                        try:
                            target = socket.socket(family, socket.SOCK_DGRAM)
                        except OSError:
                            continue
                        target.settimeout(5.0)
                        peers[key] = target
                try:
                    target.sendto(payload, (peer_host, peer_port))
                    self.bytes_up += len(payload)
                    self.server.bytes_up_total += len(payload)
                except OSError:
                    continue

        threads = [
            threading.Thread(target=from_tor, daemon=True),
            threading.Thread(target=to_tor, daemon=True),
        ]
        for thread in threads:
            thread.start()
        while not stop.is_set() and self.sock_recv_alive():
            time.sleep(0.5)
        stop.set()
        for thread in threads:
            thread.join(timeout=1.0)
        for target in list(peers.values()):
            try:
                target.close()
            except OSError:
                pass
        for sock in (udp, control):
            try:
                sock.close()
            except OSError:
                pass
        self._log("关闭 UDP 会话 %s，↑ %s ↓ %s" % (self._label(), _fmt(self.bytes_up), _fmt(self.bytes_down)))

    def sock_recv_alive(self) -> bool:
        """UDP 控制连接是否还连着（靠读事件判断）。"""
        import select

        try:
            readable, _, _ = select.select([self.sock], [], [], 0.5)
            if not readable:
                return True
            data = self.sock.recv(1)
            return bool(data)
        except OSError:
            return False


def _split_udp(data: bytes) -> Tuple[str, int, bytes]:
    if len(data) < 10:
        raise SocksError("UDP 报文过短")
    frag = data[2]
    if frag != 0:
        raise SocksError("暂不支持 UDP 分片")
    atyp = data[3]
    offset = 4
    if atyp == ATYP_V4:
        host = socket.inet_ntoa(data[offset : offset + 4])
        offset += 4
    elif atyp == 0x04:
        host = socket.inet_ntop(socket.AF_INET6, data[offset : offset + 16])
        offset += 16
    elif atyp == 0x03:
        length = data[offset]
        offset += 1
        host = data[offset : offset + length].decode("idna", "replace")
        offset += length
    else:
        raise SocksError("未知地址类型 0x%02x" % atyp)
    port = struct.unpack("!H", data[offset : offset + 2])[0]
    return host, port, data[offset + 2 :]


def _encode_udp_address(host: str, port: int) -> bytes:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raw = host.encode("idna") if any(ord(c) > 127 for c in host) else host.encode("ascii")
        return bytes([0x03, len(raw)]) + raw + struct.pack("!H", port)
    if address.version == 4:
        return bytes([ATYP_V4]) + address.packed + struct.pack("!H", port)
    return bytes([0x04]) + address.packed + struct.pack("!H", port)


def _fmt(size: int) -> str:
    if size < 1024:
        return "%d B" % size
    if size < 1024 * 1024:
        return "%.1f KB" % (size / 1024)
    return "%.1f MB" % (size / 1024 / 1024)


class SocksServer:
    """对外的 SOCKS5 服务。"""

    def __init__(
        self,
        upstream: Optional[Tuple[str, int]] = None,
        host: str = "127.0.0.1",
        port: int = 9051,
        username: Optional[str] = None,
        password: str = "",
        allow_from: Optional[List[str]] = None,
        max_connections: int = 512,
        idle_timeout: float = 300.0,
        connect_timeout: float = 30.0,
        udp_associate: bool = True,
        verbose: bool = False,
        upstream_username: Optional[str] = None,
        upstream_password: str = "",
        on_log: Optional[Callable[[str], None]] = None,
        connector: Optional[Callable[[str, int], socket.socket]] = None,
    ) -> None:
        self.upstream = upstream
        self.connector = connector
        self.host = host
        self.port = port
        self.username = username or None
        self.password = password or ""
        self.access = AccessList(allow_from or ["127.0.0.1", "::1"])
        self.max_connections = max_connections
        self.idle_timeout = idle_timeout
        self.connect_timeout = connect_timeout
        self.udp_associate = udp_associate
        self.verbose = verbose
        self.upstream_username = upstream_username
        self.upstream_password = upstream_password
        self._on_log = on_log
        self._listener: Optional[socket.socket] = None
        self._connections = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self.total_connections = 0
        self.started_at = 0.0
        #: 服务级流量聚合（Web 面板展示用；多线程下允许极小误差）
        self.bytes_up_total = 0
        self.bytes_down_total = 0

    # ------------------------------------------------------------------ 基础设施
    def log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)
        elif self.verbose:
            print("[socks5] %s" % message, flush=True)

    def bind(self) -> Tuple[str, int]:
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            try:
                listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass
        listener.bind((self.host, self.port))
        listener.listen(128)
        listener.settimeout(0.5)
        self._listener = listener
        self.port = listener.getsockname()[1]
        return listener.getsockname()[:2]

    def serve_forever(self) -> None:
        if self._listener is None:
            self.bind()
        assert self._listener is not None
        self.started_at = time.time()
        self._stop.clear()
        while not self._stop.is_set():
            try:
                client, peer = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                if self._connections >= self.max_connections:
                    self.log("连接数已达上限 %d，拒绝 %s" % (self.max_connections, peer[0]))
                    try:
                        client.sendall(build_reply(REP_NOT_ALLOWED))
                        client.close()
                    except OSError:
                        pass
                    continue
                self._connections += 1
                self.total_connections += 1
            connection = SocksConnection(client, peer, self)
            thread = threading.Thread(target=self._run_connection, args=(connection,), daemon=True)
            thread.start()
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)

    def _run_connection(self, connection: SocksConnection) -> None:
        try:
            connection.handle()
        except (OSError, SocksError) as exc:
            self.log("连接 %s 出错：%s" % (connection._label(), exc))
        except Exception as exc:  # noqa: BLE001
            self.log("连接 %s 异常：%s" % (connection._label(), exc))
        finally:
            try:
                connection.sock.close()
            except OSError:
                pass
            with self._lock:
                self._connections -= 1

    def shutdown(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None

    @property
    def address(self) -> Tuple[str, int]:
        return (self.host, self.port)

    def stats(self) -> str:
        return "已服务 %d 条连接，当前 %d 条" % (self.total_connections, self._connections)

    def stats_dict(self) -> dict:
        """结构化统计（Web 面板 / API 用）。"""
        return {
            "listen": "%s:%d" % (self.host, self.port),
            "route_auth": bool(self.username),
            "max_connections": self.max_connections,
            "current_connections": self._connections,
            "total_connections": self.total_connections,
            "bytes_up": self.bytes_up_total,
            "bytes_down": self.bytes_down_total,
            "uptime": round(time.time() - self.started_at, 1) if self.started_at else 0,
        }
