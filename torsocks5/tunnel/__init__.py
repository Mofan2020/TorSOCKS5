"""TSU/1 隧道：客户端（多路复用）、中继服务端、协议编解码、负载均衡。

* :class:`TunnelClient` —— 单中继客户端
* :class:`MultiRelayClient` —— 多中继负载均衡客户端
* :class:`RelayServer` —— 自建中继服务端
* :mod:`torsocks5.tunnel.protocol` —— 线格式，唯一真相源见 ``docs/tunnel-protocol.md``
* :mod:`torsocks5.tunnel.balancer` —— 负载均衡策略与熔断器
"""

from __future__ import annotations

from .balancer import (
    CircuitBreaker,
    HealthProber,
    LeastConnections,
    LoadBalancerStrategy,
    MultiRelayClient,
    RelayNode,
    RelayState,
    SplitRuleBinding,
    WeightedRoundRobin,
    create_strategy,
)
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
from .ratelimit import (
    AccessLogger,
    ConnectionLimiter,
    IPFilter,
    RateLimiter,
    RelayCircuitBreaker,
    TokenBucket,
)
from .relay import RelayServer
from .stream import TunnelSocket
from .wsclient import WebSocketError, WebSocketTimeout, WSClient
from .wsframe import HandshakeError

__all__ = [
    "AccessLogger",
    "CircuitBreaker",
    "ConnectionLimiter",
    "DEFAULT_ALLOW_PORTS",
    "DEFAULT_PATH",
    "ERR_NAMES",
    "HealthProber",
    "IPFilter",
    "LEARNING_ALLOW_HOSTS",
    "LeastConnections",
    "Link",
    "LoadBalancerStrategy",
    "MultiRelayClient",
    "PROTO",
    "RateLimiter",
    "RelayCircuitBreaker",
    "RelayNode",
    "RelayServer",
    "RelayState",
    "SUBPROTOCOL",
    "SplitRuleBinding",
    "TargetNotAllowed",
    "TargetUnreachable",
    "TokenBucket",
    "TunnelClient",
    "TunnelError",
    "TunnelSocket",
    "TunnelUnavailable",
    "WeightedRoundRobin",
    "WSClient",
    "WebSocketError",
    "WebSocketTimeout",
    "HandshakeError",
    "create_strategy",
]
