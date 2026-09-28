"""路由 2 与路由 3 的公共实现：都走 TSU/1 隧道协议，区别只在中继部署在哪里。"""

from __future__ import annotations

import socket
from typing import Optional, Tuple

from .. import config as config_mod
from .. import log as log_mod
from ..split import SplitRouter
from ..tunnel import TunnelClient, TunnelError, TunnelUnavailable
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
        self.client: Optional[TunnelClient] = None
        self.router: Optional[SplitRouter] = None

    # ------------------------------------------------------------ 配置读取
    def section(self, key: str, default=None):
        return self.config.section(self.config_section).get(key, default)

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
        url = self.relay_url()
        if not url:
            raise RouteError(self.missing_config_hint or "缺少中继地址", exit_code=2)
        self.router = self.build_router()
        # 分流开启时私有地址一定直连，隧道里的并发限制就不会被局域网流量占满
        self.client = self.build_client(url)
        try:
            self.client.start()
        except TunnelUnavailable as exc:
            raise RouteError("无法连接中继：%s" % exc, exit_code=3) from exc
        except TunnelError as exc:  # pragma: no cover - 兜底
            raise RouteError("隧道初始化失败：%s" % exc, exit_code=3) from exc
        self._started = True

    def stop(self) -> None:
        if self.client is not None:
            self.client.close()
        self._started = False

    # ------------------------------------------------------------ 连接方式
    def connector(self) -> Optional[Connector]:
        if self.client is None:
            return None
        assert self.router is not None
        return make_split_connector(
            self.client,
            self.router,
            self.logger,
            connect_timeout=float(self.config.get("proxy.connect_timeout")),
        )

    # ------------------------------------------------------------ 展示
    def stats(self) -> dict:
        data = {}
        if self.client is not None:
            data.update(self.client.stats())
        if self.router is not None:
            data.update({"split": self.router.stats()})
        return data

    def status(self) -> str:
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
