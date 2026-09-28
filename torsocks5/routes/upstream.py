"""``--upstream`` 用到的路由：不启动 tor，直接复用你已有的 SOCKS5 端口。

保留这个行为是为了兼容：有人已经跑着 tor（或别的代理），只想让本项目提供
带认证、带 ACL、带统计的本地 SOCKS5 入口。
"""

from __future__ import annotations

from typing import Optional, Tuple

from .. import log as log_mod
from .base import Route, RouteError, RouteOptions


class UpstreamRoute(Route):
    name = "upstream"
    title = "复用已有 SOCKS5 上游"
    summary = "不启动 tor，直接把请求转给你指定的 SOCKS5（例如本机 tor 的 9050）"
    supports_udp = True

    def __init__(self, config, logger: log_mod.Logger, options: RouteOptions) -> None:
        super().__init__(config, logger, options)
        self._upstream: Optional[Tuple[str, int]] = None

    def start(self) -> None:
        if not self.options.upstream:
            raise RouteError("路由 upstream 需要 --upstream host:port", exit_code=2)
        self._upstream = self.options.upstream
        self.logger.info("使用已有的 SOCKS5 上游: %s:%d" % self._upstream)
        self._started = True

    def upstream_socks(self) -> Optional[Tuple[str, int]]:
        return self._upstream

    def status(self) -> str:
        if self._upstream is None:
            return "未启动"
        return "上游 %s:%d" % self._upstream
