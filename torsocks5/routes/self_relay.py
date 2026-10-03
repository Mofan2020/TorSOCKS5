"""路由方式 3：自建 / 多平台中继（TSU/1 隧道协议）。

和路由 2 用同一套协议与客户端，区别是**中继由你自己提供**：

* ``torsocks5 relay serve`` —— 本机、局域网主机、自有 VPS 都能跑（纯标准库，零依赖）；
* Deno Deploy / 其它平台 —— 参考 ``docs/tunnel-protocol.md`` 自行实现；
* 任何按 ``docs/tunnel-protocol.md`` 实现的中继。

好处是不依赖 Tor、不借别人的免费额度、也不涉及第三方条款；代价是需要自己有一台
随时在线且网络可达的机器。默认分流模式是 ``all``（除私有地址外全部走隧道）。
"""

from __future__ import annotations

from .base import RouteOptions
from .tunnel_base import TunnelRoute

MISSING_CONFIG_HINT = """路由 self-relay 需要一个中继（三选一）：
  A) 本机 / 局域网主机（最简单）：
       torsocks5 relay serve --port 9052        # 另开一个终端常驻
       # 配置文件里：
       [self_relay]
       url = "ws://127.0.0.1:9052/tsu"
  B) 自有 VPS：把中继跑在 VPS 上（建议加 --token 与反向代理的 TLS），
       url = "wss://你的域名/tsu"，token = "<同一个令牌>"
  C) Deno Deploy / 其它平台：参考 docs/tunnel-protocol.md 自行实现
  也可以临时用命令行：torsocks5 run --route self-relay --relay-url ws://... --relay-token ..."""


class SelfRelayRoute(TunnelRoute):
    name = "self-relay"
    title = "自建 / 多平台中继"
    summary = "中继由你自己提供（本机/VPS/Deno Deploy）：不依赖 Tor，速度接近直连"
    supports_udp = False
    config_section = "self_relay"
    default_max_streams = 64
    missing_config_hint = MISSING_CONFIG_HINT


__all__ = ["SelfRelayRoute", "MISSING_CONFIG_HINT", "RouteOptions"]
