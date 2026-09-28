"""TSU/1 隧道：客户端（多路复用）、中继服务端、协议编解码。

* :class:`TunnelClient` —— 客户端，把 SOCKS5 请求送进隧道
* :class:`RelayServer` —— 自建中继（路由方式 3 的服务端）
* :mod:`torsocks5.tunnel.protocol` —— 线格式，唯一真相源见 ``docs/tunnel-protocol.md``
"""

from __future__ import annotations

from .client import (
    Link,
    TargetNotAllowed,
    TargetUnreachable,
    TunnelClient,
    TunnelError,
    TunnelUnavailable,
)
from .protocol import (
    DEFAULT_ALLOW_PORTS,
    DEFAULT_PATH,
    ERR_NAMES,
    LEARNING_ALLOW_HOSTS,
    PROTO,
    SUBPROTOCOL,
)
from .relay import RelayServer
from .stream import TunnelSocket
from .wsclient import WebSocketError, WebSocketTimeout, WSClient
from .wsframe import HandshakeError

__all__ = [
    "DEFAULT_ALLOW_PORTS",
    "DEFAULT_PATH",
    "ERR_NAMES",
    "LEARNING_ALLOW_HOSTS",
    "Link",
    "PROTO",
    "RelayServer",
    "SUBPROTOCOL",
    "TargetNotAllowed",
    "TargetUnreachable",
    "TunnelClient",
    "TunnelError",
    "TunnelSocket",
    "TunnelUnavailable",
    "WSClient",
    "WebSocketError",
    "WebSocketTimeout",
    "HandshakeError",
]
