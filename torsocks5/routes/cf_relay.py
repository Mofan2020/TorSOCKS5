"""路由方式 2：Cloudflare Worker 中转。

把中继放在 Cloudflare 的免费 Worker 上：不需要自己的服务器，但代价很实在：

* 免费版每个请求 **CPU 10ms**、每天 10 万次请求；
* 单个请求最多 **6 条并发出站连接**（所以每链路只能在 6 条流，靠多开链路扩容）；
* 平台禁止 Worker 连回 Cloudflare 自己的 IP 段、私网与 25 端口
  —— 也就是**任何走 Cloudflare 的站点都无法通过它中转**（实测见 README）；
* Cloudflare 服务条款 2.2.1(j) 明文禁止「用其服务提供 VPN 或类似代理服务」，
  账号有被封风险。

所以这个方式默认只放行学习类站点（中继侧白名单），并且默认开启智能分流：
只把名单里的站点送去中转，其余直连。
"""

from __future__ import annotations

from .tunnel_base import TunnelRoute

MISSING_CONFIG_HINT = """路由 cf-relay 需要一个已部署的 Cloudflare Worker：
  1) 部署本仓库自带的 Worker（零依赖、无需构建）：
       cd deploy/cloudflare && npx wrangler deploy
       npx wrangler secret put TSU_TOKEN     # 自己定一个随机字符串
  2) 把地址与令牌写进配置：
       [cf_relay]
       url = "wss://<你的-worker>.workers.dev/tsu"
       token = "<上面设置的 TSU_TOKEN>"
  3) 或者临时用命令行：torsocks5 run --route cf-relay --relay-url wss://... --relay-token ...
  完整步骤与限制见 deploy/cloudflare/README.md"""


class CfRelayRoute(TunnelRoute):
    name = "cf-relay"
    title = "Cloudflare Worker 中转"
    summary = "免费 Worker 做中转（TSU/1 隧道）：快、但受免费额度与条款限制"
    supports_udp = False
    config_section = "cf_relay"
    default_max_streams = 6  # 平台硬限制：单请求最多 6 条并发出站连接
    missing_config_hint = MISSING_CONFIG_HINT

    def target_note(self) -> str:
        note = super().target_note()
        return (note + "；中继侧只放行学习类站点，名单在 Worker 的 TSU_ALLOW_HOSTS 里")
