"""路由方式抽象。

「路由方式」= 把本地的 SOCKS5 请求交给谁送到目标。三种方式：

============  ==================================================================
``tor-meek``  Tor 网络 + meek 网桥（经 CDN 域前置的 HTTPS 隧道接入 Tor）
``cf-relay``  Cloudflare Worker 中转（走 TSU/1 隧道协议，免费额度）
``self-relay`` 自建 / 多平台（Deno Deploy、自有 VPS、局域网主机）中转，同一套协议
============  ==================================================================

路由只需向 SOCKS5 服务端提供下面两者之一，二者都由现有代码直接使用：

* :meth:`Route.upstream_socks` —— 一个上游 SOCKS5 地址（Tor 就是这种）；
* :meth:`Route.connector` —— 一个 ``(host, port) -> socket 风格对象`` 的函数（隧道是这种）。
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from .. import log as log_mod
from ..split import DIRECT, SplitRouter

Connector = Callable[[str, int], socket.socket]


class RouteError(Exception):
    """路由无法启动（缺少配置、依赖不可用等）。

    ``exit_code`` 是本进程该返回的退出码，让命令行把失败原因映射成稳定的
    退出码（2=用法/配置问题，3=启动失败，4=就绪超时）。
    """

    def __init__(self, message: str, exit_code: int = 3) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class RouteOptions:
    """命令行传进来的覆盖项。"""

    bridge_lines: List[str] = field(default_factory=list)
    tor_binary: str = ""
    direct: bool = False
    ready_timeout: Optional[float] = None
    keep_going: bool = False
    upstream: Optional[Tuple[str, int]] = None
    verbose: bool = False
    #: 覆盖配置文件里的中继地址/令牌（``--relay-url`` / ``--relay-token``）
    relay_url: str = ""
    relay_token: str = ""


class Route:
    """一种流量路由方式。"""

    name = ""
    title = ""
    summary = ""
    #: 是否支持 UDP ASSOCIATE（meek 只承载 TCP，隧道同理）
    supports_udp = False

    def __init__(self, config, logger: log_mod.Logger, options: RouteOptions) -> None:
        self.config = config
        self.logger = logger
        self.options = options
        self._started = False

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        self._started = False

    # ------------------------------------------------------------ 连接方式
    def upstream_socks(self) -> Optional[Tuple[str, int]]:
        """返回上游 SOCKS5 地址；用 ``connector`` 的路由返回 ``None``。"""
        return None

    def connector(self) -> Optional[Connector]:
        """返回 ``(host, port) -> socket`` 直连函数；用上游 SOCKS5 的路由返回 ``None``。"""
        return None

    # ------------------------------------------------------------ 展示
    def status(self) -> str:
        return ""

    def target_note(self) -> str:
        """目标范围说明，启动时打一行（隧道类路由有白名单/分流时要讲清楚）。"""
        return ""

    def stats(self) -> dict:
        return {}


def direct_connect(host: str, port: int, timeout: float = 30.0) -> socket.socket:
    """本地直连（域名在本机解析）。分流里被判为「直连」的目标走这里。"""
    return socket.create_connection((host, port), timeout=timeout)


def make_split_connector(
    client,
    router: SplitRouter,
    logger: log_mod.Logger,
    *,
    connect_timeout: float = 30.0,
) -> Connector:
    """把「隧道客户端 + 分流规则」组合成一个 connector。

    分流判为直连的目标在本地直接连（``socket.create_connection``），
    判为走隧道的目标交给 :class:`~torsocks5.tunnel.client.TunnelClient`。
    """

    def connector(host: str, port: int) -> socket.socket:
        if router.enabled and router.decide(host, port) == DIRECT:
            logger.debug("直连 %s:%d（分流）" % (host, port))
            return direct_connect(host, port, timeout=connect_timeout)
        return client.connect(host, port, timeout=connect_timeout)  # type: ignore[return-value]

    return connector
