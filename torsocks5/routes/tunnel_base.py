"""路由 2 与路由 3 的公共实现：都走 TSU/1 隧道协议，区别只在中继部署在哪里。

支持单中继（旧格式 url/token）和多中继（新格式 [[nodes]]）。
"""

from __future__ import annotations

import socket
from typing import Any, Dict, List, Optional, Tuple

from .. import config as config_mod
from .. import log as log_mod
from ..split import SplitRouter
from ..tunnel import (
    MultiRelayClient,
    TunnelClient,
    TunnelError,
    TunnelUnavailable,
)
from .base import Connector, Route, RouteError, RouteOptions, make_split_connector


class TunnelRoute(Route):
    """基于隧道协议的路由基类。"""

    #: 配置文件里的段名（``cf_relay`` / ``self_relay``）
    config_section = ""
    #: 单条链路的并发上限默认值（CF 免费版硬限制为 6）
    default_max_streams = 64
    #: 配置缺失时的提示（子类覆盖）
    missing_config_hint = ""

    def __init__(self, config, logger: log_mod.Logger, options: RouteOptions) -> None:
        super().__init__(config, logger, options)
        self.multi_client: Optional[MultiRelayClient] = None
        self.client: Optional[TunnelClient] = None  # 兼容：单中继时指向主客户端
        self.router: Optional[SplitRouter] = None

    # ------------------------------------------------------------ 配置读取
    def section(self, key: str, default=None):
        return self.config.section(self.config_section).get(key, default)

    def _parse_nodes(self) -> List[Dict[str, Any]]:
        """解析中继节点配置，支持新旧两种格式。"""
        section = self.config_section

        # 新格式：[[nodes]] 数组
        nodes_data = self.config.section(section).get("nodes")
        if nodes_data and isinstance(nodes_data, list):
            nodes = []
            for node_cfg in nodes_data:
                if isinstance(node_cfg, dict):
                    node = {
                        "url": str(node_cfg.get("url", "")),
                        "token": str(node_cfg.get("token", "")),
                        "weight": int(node_cfg.get("weight", 1)),
                        "max_streams": int(node_cfg.get("max_streams", self.default_max_streams)),
                        "health_check_interval": float(node_cfg.get("health_check_interval", 30.0)),
                        "bind_hosts": node_cfg.get("bind_hosts", []),
                        "front": str(node_cfg.get("front", "")),
                        "insecure": config_mod.as_bool(node_cfg.get("insecure", False)),
                        "token_in_header": config_mod.as_bool(node_cfg.get("token_in_header", False)),
                    }
                    if node["url"]:
                        nodes.append(node)
            if nodes:
                return nodes

        # 旧格式：单 url/token（兼容）
        url = self.relay_url()
        token = self.relay_token()
        if url:
            return [{
                "url": url,
                "token": token,
                "weight": 1,
                "max_streams": int(self.config.get("%s.max_streams" % section) or self.default_max_streams),
                "health_check_interval": 30.0,
                "bind_hosts": [],
                "front": str(self.config.get("%s.front" % section) or ""),
                "insecure": config_mod.as_bool(self.config.get("%s.insecure" % section)),
                "token_in_header": config_mod.as_bool(self.config.get("%s.token_in_header" % section)),
            }]

        return []

    def relay_url(self) -> str:
        return str(self.options.relay_url or self.config.get("%s.url" % self.config_section) or "")

    def relay_token(self) -> str:
        return str(self.options.relay_token or self.config.get("%s.token" % self.config_section) or "")

    def build_router(self) -> SplitRouter:
        return SplitRouter(
            str(self.config.get("split.mode")),
            route_name=self.name,
            builtin_proxy=config_mod.as_bool(self.config.get("split.builtin_proxy")),
            builtin_direct=config_mod.as_bool(self.config.get("split.builtin_direct")),
            proxy_hosts=self.config.get("split.proxy_hosts"),
            direct_hosts=self.config.get("split.direct_hosts"),
            on_log=self.logger.debug,
        )

    def build_client(self, url: str) -> TunnelClient:
        section = self.config_section
        max_streams = int(self.config.get("%s.max_streams" % section) or self.default_max_streams)
        return TunnelClient(
            url,
            self.relay_token(),
            links=max(1, int(self.config.get("%s.links" % section) or 4)),
            max_streams=max(1, min(max_streams, 1024)),
            open_timeout=float(self.config.get("proxy.connect_timeout")),
            idle_timeout=float(self.config.get("proxy.idle_timeout")),
            keepalive=30.0,
            on_log=self.logger.debug,
            front=str(self.config.get("%s.front" % section) or ""),
            insecure=config_mod.as_bool(self.config.get("%s.insecure" % section)),
            token_in_header=config_mod.as_bool(self.config.get("%s.token_in_header" % section)),
        )

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        nodes = self._parse_nodes()
        if not nodes:
            raise RouteError(self.missing_config_hint or "缺少中继地址", exit_code=2)

        self.router = self.build_router()

        # 负载均衡策略
        lb_strategy = str(self.config.get("%s.lb_strategy" % self.config_section) or "weighted_rr")
        cb_threshold = int(self.config.get("%s.circuit_breaker_threshold" % self.config_section) or 5)
        cb_timeout = float(self.config.get("%s.circuit_breaker_timeout" % self.config_section) or 60.0)

        self.multi_client = MultiRelayClient(
            nodes,
            strategy_name=lb_strategy,
            circuit_breaker_threshold=cb_threshold,
            circuit_breaker_timeout=cb_timeout,
            on_log=self.logger.debug,
        )

        try:
            self.multi_client.start()
        except TunnelUnavailable as exc:
            raise RouteError("无法连接中继：%s" % exc, exit_code=3) from exc
        except TunnelError as exc:
            raise RouteError("隧道初始化失败：%s" % exc, exit_code=3) from exc

        # 兼容：单中继时，client 指向第一个可用的
        if self.multi_client.nodes:
            for node in self.multi_client.nodes:
                if node.client:
                    self.client = node.client
                    break

        self._started = True

    def stop(self) -> None:
        if self.multi_client is not None:
            self.multi_client.stop()
        elif self.client is not None:
            self.client.close()
        self._started = False

    # ------------------------------------------------------------ 连接方式
    def connector(self) -> Optional[Connector]:
        if self.multi_client is None and self.client is None:
            return None
        assert self.router is not None

        if self.multi_client:
            # 多中继：使用自定义 connector
            return self._make_multi_connector()
        else:
            # 单中继：使用原有逻辑
            return make_split_connector(
                self.client,
                self.router,
                self.logger,
                connect_timeout=float(self.config.get("proxy.connect_timeout")),
            )

    def _make_multi_connector(self) -> Connector:
        """创建支持多中继的 connector。"""
        multi = self.multi_client
        if multi is None:
            raise TunnelUnavailable("多中继客户端未初始化")
        connect_timeout = float(self.config.get("proxy.connect_timeout"))

        def connector(host: str, port: int) -> socket.socket:
            # 由负载均衡策略（含分流绑定规则）选择中继
            client = multi.get_client_for_target(host, port)
            if client is None:
                raise TunnelUnavailable("无可用中继节点")
            try:
                sock = client.connect(host, port, timeout=connect_timeout)
            except Exception:
                multi.release_client(client, success=False)
                raise
            # 连接关闭时释放节点并发计数（只释放一次）
            released = False
            orig_close = sock.close

            def _close() -> None:
                nonlocal released
                try:
                    orig_close()
                finally:
                    if not released:
                        released = True
                        multi.release_client(client, success=True)

            sock.close = _close  # type: ignore[method-assign]
            return sock  # type: ignore[return-value]

        return connector

    # ------------------------------------------------------------ 展示
    def stats(self) -> dict:
        data = {}
        if self.multi_client:
            data.update(self.multi_client.stats())
        elif self.client:
            data.update(self.client.stats())
        if self.router is not None:
            data.update({"split": self.router.stats()})
        return data

    def status(self) -> str:
        if self.multi_client:
            stats = self.multi_client.stats()
            node_summary = []
            for n in stats["nodes"]:
                node_summary.append(
                    f"{n['url']}({n['state']}:{n['active_streams']}/{n['max_streams']})"
                )
            text = f"多中继({stats['strategy']}) · 节点: {'; '.join(node_summary)}"
            if self.router and self.router.enabled:
                text += f" · {self.router.describe()}"
            return text

        if self.client is None:
            return "未启动"
        stats = self.client.stats()
        text = ("中继 %s · 链路 %d/%d（活跃 %d）· 并发流 %d/%d · 累计 %d"
                % (stats["url"], stats["links"], stats["links_max"], stats["links_live"],
                   stats["streams"], stats["streams_max"], stats["streams_total"]))
        if self.router is not None and self.router.enabled:
            text += " · %s" % self.router.describe()
        if stats.get("last_error"):
            text += " · 最近错误：%s" % stats["last_error"]
        return text

    def target_note(self) -> str:
        if self.router is None:
            return ""
        if self.router.mode == "off":
            return "目标：中继允许的任意地址（分流关闭）"
        return "目标：%s（私有地址始终直连）" % self.router.describe().replace("分流：", "")


def direct_connect(host: str, port: int, timeout: float = 30.0) -> socket.socket:
    """分流判为直连时的本地连接（域名在本机解析）。"""
    return socket.create_connection((host, port), timeout=timeout)


def relay_host_port(url: str) -> Tuple[str, int]:
    """从 URL 里取出主机与端口，供日志与自检显示。"""
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    return parsed.hostname or "", port
