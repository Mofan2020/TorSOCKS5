"""路由方式 1：Tor 网络 + meek 网桥。

这是项目的初始形态：本地起一个 tor 客户端，meek 传输由本项目用纯 Python 实现
（不需要 Go、不需要 Tor Browser），通过 CDN 域前置的 HTTPS 隧道连上 meek 网桥，
再把 tor 暴露出来的 SOCKS5 端口转给用户。

代价是慢（meek 每 64KB 一次完整 HTTP 往返），但最抗封锁。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .. import __version__
from .. import bridges as bridges_mod
from .. import config as config_mod
from .. import log as log_mod
from ..tor.manager import TorProcess, TorSupervisor
from .base import Route, RouteError, RouteOptions

PROGRESS_HINTS = (
    "提示：meek 协议每 64KB 需要一次完整 HTTP 往返，**慢是正常现象**。",
    "      首次启动要下载约 7MB 目录信息，可能需要 10~30 分钟；",
    "      进度停在 50%%~99%% 且仍在缓慢增长即属正常。",
    "      想更久可加 --ready-timeout 1800；不想等待用 --ready-timeout 0 --keep-going",
)


class TorMeekRoute(Route):
    name = "tor-meek"
    title = "Tor 网络 + meek 网桥"
    summary = "CDN 域前置的 HTTPS 隧道接入 Tor：最抗封锁、最慢"
    supports_udp = True

    def __init__(self, config, logger: log_mod.Logger, options: RouteOptions) -> None:
        super().__init__(config, logger, options)
        self.supervisor: Optional[TorSupervisor] = None
        self.tor: Optional[TorProcess] = None
        self.bridge_lines: List[str] = []
        self._upstream: Optional[Tuple[str, int]] = None

    # ------------------------------------------------------------ 网桥
    def load_bridge_store(self) -> bridges_mod.BridgeStore:
        """加载网桥（内置 + 配置文件 + 命令行）。"""
        store = bridges_mod.BridgeStore(self.config.bridges_path)
        store.load(include_builtin=config_mod.as_bool(self.config.get("bridges.builtin")))
        for line in self.options.bridge_lines:
            for item in bridges_mod.iter_lines([line]):
                item = item.strip()
                if not item or item.startswith("#"):
                    continue
                try:
                    store.add(bridges_mod.parse_bridge_line(item))
                except bridges_mod.BridgeError as exc:
                    raise RouteError("网桥行无效: %s -> %s" % (item, exc), exit_code=2) from exc
        return store

    @property
    def using_bridges(self) -> bool:
        return not (self.options.direct or config_mod.as_bool(self.config.get("tor.direct")))

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        config = self.config
        logger = self.logger
        store = self.load_bridge_store()
        self.bridge_lines = store.torrc_lines() if self.using_bridges else []
        if self.using_bridges and not self.bridge_lines:
            raise RouteError(
                "没有可用的网桥。请先添加：torsocks5 bridges add \"Bridge meek 0.0.2.0:3 url=... front=...\"\n"
                "或从 https://bridges.torproject.org 获取 meek 网桥行。"
                "（也可以 --route self-relay 换用不依赖 Tor 的中转）",
                exit_code=2,
            )

        def factory() -> TorProcess:
            cfg = config_mod.Config(dict(config.data), config.path)
            if self.options.tor_binary:
                cfg.data.setdefault("tor", {})["binary"] = self.options.tor_binary
            if self.options.direct:
                cfg.data.setdefault("tor", {})["direct"] = True
            return TorProcess(
                cfg,
                self.bridge_lines,
                on_log=lambda line: logger.debug(log_mod.clean_tor_line(line)),
                on_progress=log_mod.progress_printer(logger),
                on_state=lambda state: logger.debug("tor 状态: %s" % state),
            )

        self.supervisor = TorSupervisor(
            factory,
            restart=config_mod.as_bool(config.get("tor.restart")),
            on_log=logger.warn,
        )
        if self.using_bridges:
            log_mod.banner(logger, "TorSOCKS5 %s —— 正在通过 meek 网桥连接 Tor" % __version__)
        else:
            log_mod.banner(logger, "TorSOCKS5 %s —— 直连 Tor（未使用网桥）" % __version__)
        try:
            self.tor = self.supervisor.start()
        except Exception as exc:  # noqa: BLE001
            raise RouteError("启动 tor 失败: %s" % exc, exit_code=3) from exc
        logger.info("tor: %s" % self.tor.summary_text())
        logger.info("torrc: %s" % self.tor.torrc_path)
        if self.bridge_lines:
            preview = "; ".join(self.bridge_lines[:3]) + ("…" if len(self.bridge_lines) > 3 else "")
            logger.info("网桥: %s" % preview)

        timeout = self.options.ready_timeout if self.options.ready_timeout is not None else 300
        if timeout > 0:
            logger.info("等待 Tor 引导完成（最多 %d 秒）…" % timeout)
            for hint in PROGRESS_HINTS:
                logger.info(hint)
            if not self.tor.wait_for_ready(timeout):
                percent, summary = self.tor.bootstrap_status()
                logger.error("Tor 引导未完成（%d%% %s）。" % (percent, summary))
                logger.error("常见原因：网桥失效 / CDN 前置域名被封 / 需要换一条网桥。")
                logger.error("查看详细日志: %s" % self.tor.tor_log_path)
                if self.options.keep_going:
                    logger.warn("按 --keep-going 继续以提供代理（多数请求会失败）")
                else:
                    self.supervisor.stop()
                    raise RouteError("Tor 引导未完成（%d%%）" % percent, exit_code=4)
        self._upstream = self.tor.socks_address
        self._started = True

    def stop(self) -> None:
        if self.supervisor is not None:
            self.supervisor.stop()
        self._started = False

    # ------------------------------------------------------------ 连接方式
    def upstream_socks(self) -> Optional[Tuple[str, int]]:
        return self._upstream

    def status(self) -> str:
        if self.tor is None:
            return "未启动"
        percent, summary = self.tor.bootstrap_status()
        return "tor %s · 引导 %d%% %s" % (self.tor.summary_text(), percent, summary)

    def target_note(self) -> str:
        if self.using_bridges:
            return "目标：Tor 出口可到达的任意地址（含 .onion）；速度受 meek 限制"
        return "目标：Tor 出口可到达的任意地址（未使用网桥，直连 Tor 入口）"
