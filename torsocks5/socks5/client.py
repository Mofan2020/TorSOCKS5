"""SOCKS5 客户端：用于连接上游 tor 的 SOCKS5 端口，或给 meek 隧道套一层上游代理。"""

from __future__ import annotations

import socket
import struct
from typing import Optional, Tuple

from .protocol import (
    ATYP_V4,
    AUTH_NONE,
    AUTH_USERNAME,
    CMD_CONNECT,
    CMD_UDP_ASSOCIATE,
    REP_SUCCESS,
    VERSION,
    SocksError,
    encode_address,
)


def _recv_exactly(sock: socket.socket, want: int) -> bytes:
    chunks = []
    got = 0
    while got < want:
        chunk = sock.recv(want - got)
        if not chunk:
            raise SocksError("SOCKS 服务器提前关闭了连接")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def socks5_connect(
    proxy: Tuple[str, int],
    host: str,
    port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout: float = 30.0,
    sock: Optional[socket.socket] = None,
) -> socket.socket:
    """通过 ``proxy`` 建立一条到 ``host:port`` 的 SOCKS5 隧道。"""
    close_after = False
    if sock is None:
        sock = socket.create_connection(proxy, timeout=timeout)
        close_after = True
    try:
        if username:
            # 声明 2 个认证方法：用户名/密码 + 免认证
            sock.sendall(bytes([VERSION, 2, AUTH_USERNAME, AUTH_NONE]))
        else:
            sock.sendall(bytes([VERSION, 1, AUTH_NONE]))
        reply = _recv_exactly(sock, 2)
        if reply[0] != VERSION:
            raise SocksError("SOCKS 版本不正确: %d" % reply[0])
        if reply[1] == AUTH_USERNAME:
            user = (username or "").encode("utf-8")
            secret = (password or "").encode("utf-8")
            if len(user) > 255 or len(secret) > 255:
                raise SocksError("用户名或密码过长")
            sock.sendall(bytes([0x01, len(user)]) + user + bytes([len(secret)]) + secret)
            auth = _recv_exactly(sock, 2)
            if auth[1] != 0x00:
                raise SocksError("SOCKS5 用户名/密码认证失败")
        elif reply[1] != AUTH_NONE:
            raise SocksError("SOCKS5 认证方式被拒绝: 0x%02x" % reply[1])

        addr = encode_address(host, port)
        sock.sendall(bytes([VERSION, CMD_CONNECT, 0x00]) + addr)
        head = _recv_exactly(sock, 4)
        if head[1] != REP_SUCCESS:
            raise SocksError("SOCKS5 CONNECT 被拒绝，错误码 0x%02x" % head[1])
        atyp = head[3]
        if atyp == ATYP_V4:
            _recv_exactly(sock, 4)
        elif atyp == 0x04:
            _recv_exactly(sock, 16)
        elif atyp == 0x03:
            length = _recv_exactly(sock, 1)[0]
            _recv_exactly(sock, length)
        else:
            raise SocksError("未知地址类型 0x%02x" % atyp)
        _recv_exactly(sock, 2)
    except Exception:
        if close_after:
            try:
                sock.close()
            except OSError:
                pass
        raise
    sock.settimeout(None)
    return sock


def socks5_udp_associate(
    proxy: Tuple[str, int],
    timeout: float = 30.0,
) -> Tuple[socket.socket, Tuple[str, int]]:
    """向 SOCKS5 服务器请求 UDP 代理，返回 ``(TCP 控制连接, UDP 中继地址)``。"""
    control = socket.create_connection(proxy, timeout=timeout)
    try:
        control.sendall(bytes([VERSION, 1, AUTH_NONE]))
        if _recv_exactly(control, 2)[1] != AUTH_NONE:
            raise SocksError("上游 SOCKS5 服务器要求认证")
        control.sendall(
            bytes([VERSION, CMD_UDP_ASSOCIATE, 0x00, ATYP_V4]) + b"\x00" * 6
        )
        head = _recv_exactly(control, 4)
        if head[1] != REP_SUCCESS:
            raise SocksError("UDP ASSOCIATE 被拒绝，错误码 0x%02x" % head[1])
        atyp = head[3]
        if atyp == ATYP_V4:
            host = socket.inet_ntoa(_recv_exactly(control, 4))
        elif atyp == 0x04:
            host = socket.inet_ntop(socket.AF_INET6, _recv_exactly(control, 16))
        elif atyp == 0x03:
            length = _recv_exactly(control, 1)[0]
            host = _recv_exactly(control, length).decode("ascii", "replace")
        else:
            raise SocksError("未知地址类型 0x%02x" % atyp)
        port = struct.unpack("!H", _recv_exactly(control, 2))[0]
    except Exception:
        control.close()
        raise
    return control, (host, port)
