"""三种流量路由方式。

===========================  ================================================================
``tor-meek``                 Tor 网络 + meek 网桥（默认）：最抗封锁、最慢
``cf-relay``                 Cloudflare Worker 中转：免费额度、受平台限制与条款约束
``self-relay``               自建 / 多平台（本机、VPS、Deno Deploy）中转：不依赖 Tor、快
``upstream``                 ``--upstream`` 专用：复用已有的 SOCKS5 端口
===========================  ================================================================

新增路由方式时：在这里注册，并在 ``docs/routes.md`` 与 README 里补上说明。
"""

from __future__ import annotations

from typing import Dict, List, Tuple, Type

from .base import Connector, Route, RouteError, RouteOptions
from .cf_relay import CfRelayRoute
from .self_relay import SelfRelayRoute
from .tor_meek import TorMeekRoute
from .tunnel_base import TunnelRoute
from .upstream import UpstreamRoute

DEFAULT_ROUTE = "tor-meek"

ROUTES: Dict[str, Type[Route]] = {
    TorMeekRoute.name: TorMeekRoute,
    CfRelayRoute.name: CfRelayRoute,
    SelfRelayRoute.name: SelfRelayRoute,
    UpstreamRoute.name: UpstreamRoute,
}

#: 面向用户的三种路由（``upstream`` 是内部用法，不出现在引导与文档主表里）
USER_ROUTES: Tuple[str, ...] = ("tor-meek", "cf-relay", "self-relay")


def route_names(include_internal: bool = False) -> List[str]:
    if include_internal:
        return list(ROUTES)
    return [name for name in ROUTES if name in USER_ROUTES]


def create_route(name: str, config, logger, options: RouteOptions) -> Route:
    """按名字创建路由实例。"""
    key = (name or DEFAULT_ROUTE).strip().lower()
    if key not in ROUTES:
        raise RouteError("未知的路由方式 %r（可选：%s）"
                         % (name, ", ".join(route_names())), exit_code=2)
    return ROUTES[key](config, logger, options)


def describe_table() -> List[Tuple[str, str, str]]:
    """``(名称, 标题, 说明)`` 列表，给 ``torsocks5 routes`` 和文档用。"""
    rows = []
    for name in route_names():
        cls = ROUTES[name]
        rows.append((name, cls.title, cls.summary))
    return rows


__all__ = [
    "Connector",
    "DEFAULT_ROUTE",
    "ROUTES",
    "Route",
    "RouteError",
    "RouteOptions",
    "TunnelRoute",
    "USER_ROUTES",
    "create_route",
    "describe_table",
    "route_names",
]
