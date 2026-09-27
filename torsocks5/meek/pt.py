"""可插拔传输（Pluggable Transport, PT）客户端协议前端。

tor 通过 ``ClientTransportPlugin`` 启动本模块后，会在标准输入/输出上
用行协议（PT 规范 1.x/2.x）交互：

.. code-block:: text

    ← TOR_PT_METHODS=meek  TOR_PT_MANAGED_TRANSPORT_VER=1
    → VERSION 1
    → CMETHOD meek socks5 127.0.0.1:41234
    → CMETHODS DONE
    ← AUTHENTICATE <hex>
    ← PROXY DONE

之后 tor 会连接我们公布的 SOCKS5 地址发起 CONNECT，并带上网桥参数。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from typing import List, Optional

from .socks_server import TransportSocksServer

# 我们能提供的传输名。meek_lite / meek_azure 是 lyrebird 里的同族名字，
# 方便用户直接使用各类网桥行。
SUPPORTED_METHODS = ("meek", "meek_lite", "meek_azure")


def _read_methods() -> List[str]:
    raw = os.environ.get("TOR_PT_METHODS") or os.environ.get("TOR_PT_CLIENT_TRANSPORTS") or ""
    return [item.strip() for item in raw.replace(" ", ",").split(",") if item.strip()]


def _managed_version() -> str:
    raw = os.environ.get("TOR_PT_MANAGED_TRANSPORT_VER", "")
    for offered in raw.split(","):
        if offered.strip() == "1":
            return "1"
    return "1"


class TransportPlugin:
    def __init__(
        self,
        url: Optional[str] = None,
        front: Optional[str] = None,
        upstream_socks: Optional[str] = None,
        verbose: bool = False,
    ) -> None:
        self.url = url
        self.front = front
        self.upstream_socks = upstream_socks
        self.verbose = verbose
        self.server = TransportSocksServer(
            default_url=url,
            default_front=front,
            on_log=self._log if verbose else None,
            on_error=self._log,
        )
        # 错误始终可见（tor 会把插件的 stderr 记进自己的日志）
        self._done = threading.Event()

    def _log(self, message: str) -> None:
        # 插件的标准输出是 PT 协议通道，普通日志必须走 stderr。
        print("[meek] %s" % message, file=sys.stderr, flush=True)

    def _emit(self, line: str) -> None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def negotiate(self) -> List[str]:
        """完成 PT 握手，返回本次可用的传输名列表。"""
        methods = _read_methods()
        if not methods:
            self._emit("ENV-ERROR no TOR_PT_METHODS environment variable")
            return []
        self._emit("VERSION %s" % _managed_version())

        proxy = os.environ.get("TOR_PT_PROXY", "").strip()
        if proxy and not self.upstream_socks:
            # 我们不使用 tor 管理的上游代理（meek 隧道必须直连 CDN），
            # 按规范回报 VERSION 0 表示「不使用该代理」。
            self._log("检测到上游代理 %s，但未配置上游 SOCKS5，声明不使用" % proxy)
            self._emit("VERSION 0")

        address = self.server.listen()
        active: List[str] = []
        for name in methods:
            if name in SUPPORTED_METHODS:
                self._emit("CMETHOD %s socks5 %s" % (name, address))
                active.append(name)
            else:
                self._emit("CMETHOD-ERROR %s no such method" % name)
        self._emit("CMETHODS DONE")
        return active

    def run(self) -> int:
        active = self.negotiate()
        if not active:
            return 1
        thread = threading.Thread(target=self.server.serve_forever, name="pt-socks", daemon=True)
        thread.start()

        exit_on_stdin_close = os.environ.get("TOR_PT_EXIT_ON_STDIN_CLOSE") == "1"
        if exit_on_stdin_close:
            threading.Thread(target=self._watch_stdin, daemon=True).start()

        def handle_signal(signum, _frame):
            self._log("收到信号 %d，退出" % signum)
            self._done.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handle_signal)
            except (ValueError, OSError):
                pass

        while not self._done.is_set():
            self._done.wait(0.5)
        self.server.close()
        return 0

    def _watch_stdin(self) -> None:
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except Exception:
            pass
        self._log("stdin 关闭，退出")
        self._done.set()

    def handle_control(self) -> None:
        """处理 tor 发来的控制行（``AUTHENTICATE`` / ``PROXY`` / ``QUIT``）。"""
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            parts = line.split()
            keyword = parts[0].upper()
            if keyword == "AUTHENTICATE":
                self._log("已通过 AUTHENTICATE 认证")
            elif keyword == "PROXY":
                arg = parts[1].upper() if len(parts) > 1 else ""
                if arg == "DONE":
                    self._emit("PROXY DONE")
                elif arg == "CONNECT":
                    self._emit("PROXY DONE")
                else:
                    self._emit("PROXY-ERROR unsupported managed proxy")
            elif keyword == "QUIT":
                self._done.set()
                return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="torsocks5-meek",
        description="meek 客户端传输插件（通常由 tor 自动启动，一般无需手动运行）",
    )
    parser.add_argument("--url", default=os.environ.get("TORSOCKS5_MEEK_URL"),
                        help="网桥行里没有 url= 时使用的默认 URL")
    parser.add_argument("--front", default=os.environ.get("TORSOCKS5_MEEK_FRONT"),
                        help="默认的前置（front）域名")
    parser.add_argument("--upstream-socks", default=os.environ.get("TORSOCKS5_MEEK_UPSTREAM"),
                        help="上游 SOCKS5 代理（host:port），一般不需要")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志到 stderr")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    # 允许直接以 `meek_pt.py --transport-plugin ...` 的形式启动
    args_list = [item for item in args_list if item != "--transport-plugin"]
    args = build_parser().parse_args(args_list)
    plugin = TransportPlugin(
        url=args.url,
        front=args.front,
        upstream_socks=args.upstream_socks,
        verbose=args.verbose,
    )
    control = threading.Thread(target=plugin.handle_control, name="pt-control", daemon=True)
    control.start()
    try:
        return plugin.run()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print("[meek] 致命错误: %s" % exc, file=sys.stderr, flush=True)
        time.sleep(0.1)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
