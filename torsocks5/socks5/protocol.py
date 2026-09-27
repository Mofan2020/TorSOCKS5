"""SOCKS5 协议常量与报文编解码（RFC 1928 / RFC 1929）。"""

from __future__ import annotations

import ipaddress
import struct
from typing import Tuple

VERSION = 0x05

AUTH_NONE = 0x00
AUTH_USERNAME = 0x02
AUTH_UNACCEPTABLE = 0xFF

CMD_CONNECT = 0x01
CMD_BIND = 0x02
CMD_UDP_ASSOCIATE = 0x03

ATYP_V4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_V6 = 0x04

REP_SUCCESS = 0x00
REP_GENERAL_FAILURE = 0x01
REP_NOT_ALLOWED = 0x02
REP_NETWORK_UNREACHABLE = 0x03
REP_HOST_UNREACHABLE = 0x04
REP_CONNECTION_REFUSED = 0x05
REP_TTL_EXPIRED = 0x06
REP_COMMAND_NOT_SUPPORTED = 0x07
REP_ADDRESS_TYPE_NOT_SUPPORTED = 0x08

UDP_HEADER = b"\x00\x00\x00"


class SocksError(Exception):
    """SOCKS 协议错误。"""


def encode_address(host: str, port: int) -> Tuple[bytes, bytes]:
    """把 ``host``/``port`` 编码成 SOCKS5 的 ATYP+ADDR+PORT 字段。"""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raw = host.encode("idna") if any(ord(c) > 127 for c in host) else host.encode("ascii")
        if len(raw) > 255:
            raise SocksError("域名过长: %r" % host)
        return bytes([ATYP_DOMAIN, len(raw)]) + raw + struct.pack("!H", port)
    if addr.version == 4:
        return bytes([ATYP_V4]) + addr.packed + struct.pack("!H", port)
    return bytes([ATYP_V6]) + addr.packed + struct.pack("!H", port)


def decode_address(reader) -> Tuple[str, int]:
    """从可读流中解析 ATYP+ADDR+PORT，返回 ``(host, port)``。"""
    atyp = reader.read(1)
    if not atyp:
        raise SocksError("读取地址类型失败")
    atyp = atyp[0]
    if atyp == ATYP_V4:
        raw = reader.read(4)
        if len(raw) != 4:
            raise SocksError("IPv4 地址不完整")
        host = socket_inet_ntoa(raw)
    elif atyp == ATYP_V6:
        raw = reader.read(16)
        if len(raw) != 16:
            raise SocksError("IPv6 地址不完整")
        host = socket_inet_ntop(raw)
    elif atyp == ATYP_DOMAIN:
        length = reader.read(1)
        if not length:
            raise SocksError("域名长度缺失")
        raw = reader.read(length[0])
        if len(raw) != length[0]:
            raise SocksError("域名不完整")
        host = raw.decode("idna", "replace") if any(b > 127 for b in raw) else raw.decode("ascii")
    else:
        raise SocksError("未知地址类型 0x%02x" % atyp)
    port_raw = reader.read(2)
    if len(port_raw) != 2:
        raise SocksError("端口缺失")
    return host, struct.unpack("!H", port_raw)[0]


def socket_inet_ntoa(raw: bytes) -> str:
    return str(ipaddress.ip_address(raw))


def socket_inet_ntop(raw: bytes) -> str:
    return str(ipaddress.ip_address(raw))


def build_reply(code: int, host: str = "0.0.0.0", port: int = 0) -> bytes:
    """构造 SOCKS5 应答报文。"""
    try:
        addr = ipaddress.ip_address(host)
        atyp = ATYP_V4 if addr.version == 4 else ATYP_V6
        return bytes([VERSION, code, 0x00, atyp]) + addr.packed + struct.pack("!H", port)
    except ValueError:
        raw = host.encode("ascii", "ignore")
        return bytes([VERSION, code, 0x00, ATYP_DOMAIN, len(raw)]) + raw + struct.pack("!H", port)
