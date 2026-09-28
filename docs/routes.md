# 三种流量路由方式

TorSOCKS5 是一个**本地 SOCKS5 代理**，它本身不产生网络出口；出口由「路由方式」决定。
用 `torsocks5 routes` 可以随时查看三种方式与当前环境的就绪情况，用
`torsocks5 run --route <名字>` 或配置里的 `[proxy] route` 切换。

```
你的应用 ──SOCKS5──▶ TorSOCKS5 ──┬─ tor-meek   ── meek(HTTPS+CDN 前置) ──▶ Tor 网络 ──▶ 目标
                                  ├─ cf-relay   ── WSS(TSU/1 隧道) ──▶ Cloudflare Worker ──▶ 目标
                                  └─ self-relay ── WSS(TSU/1 隧道) ──▶ 你自己的中继 ──▶ 目标
```

---

## 1. 一眼对比

| | `tor-meek` | `cf-relay` | `self-relay` |
| --- | --- | --- | --- |
| 出口 | Tor 网络 | 你部署的 Cloudflare Worker | 你自己的中继（本机/VPS/Deno Deploy） |
| 需要的资源 | 本机 tor + 一条 meek 网桥 | 一个 Cloudflare 账号（免费） | 一台能常驻的机器或免费的 Deno Deploy |
| 实测吞吐 | **约 25 KB/s**（协议天花板） | 受免费版 CPU/并发限制（见 §3） | **约 798 KB/s**（本机中继，≈直连） |
| 首次可用时间 | 10~30 分钟（要下 7MB 目录信息） | 部署 Worker 约 1 分钟 | 起中继 + 客户端，约 10 秒 |
| 抗封锁 | 最强（CDN 域前置） | 中（取决于中继域名是否被封） | 取决于你自己的域名/线路 |
| 目标范围 | 任意地址（含 .onion） | **仅白名单内的站点** | 白名单可关，默认只放部分端口 |
| UDP | 只到 tor 这一层 | 不支持 | 不支持 |
| 合规风险 | 低（Tor 官方协议与网桥） | **高**：CF 条款 2.2.1(j) 禁止代理用途 | 低（用的是你自己的机器） |

**怎么选：**

1. 网络审查最严、什么都被封 → `tor-meek`（慢是代价）。
2. 没有自己的服务器，只想偶尔拉一下 GitHub/PyPI/模型仓库 → `cf-relay`。
3. 有台机器（哪怕是家里的旧电脑/路由器/NAS）→ `self-relay`，体验最好。

---

## 2. `tor-meek`：Tor 网络 + meek 网桥（默认）

本项目最初的形态。meek 传输是**纯 Python 实现**的，不需要 Go、不需要编译、不需要 Tor Browser。

* 原理：把 Tor 流量伪装成对 CDN 的普通 HTTPS 请求，meek 网桥的地址由 CDN 前置域名承载。
* 代价：**每传 64KB 必须完成一次 HTTP 往返**，吞吐上限 = 64KB ÷ RTT。
* 详细说明、引导进度含义、排障都在 [README](../README.md) 里（这原本是项目的主体）。

```bash
torsocks5 bridges add "Bridge meek 0.0.2.0:3 url=https://... front=..."
torsocks5 run --route tor-meek
```

靶点：只要有一条可用网桥就能用；`.onion` 只有这条路能访问。

---

## 3. `cf-relay`：Cloudflare Worker 中转

中继跑在 Cloudflare 免费 Worker 上，用本项目的 **TSU/1 隧道协议**（[规范](tunnel-protocol.md)）
把 SOCKS5 的 TCP 流转成 WebSocket 上的多路复用流。

### 平台限制（都是硬限制，不是实现偷懒）

| 限制 | 值 | 影响 |
| --- | --- | --- |
| 每请求 CPU 时间 | 免费版 10 ms | 高吞吐长时间传输容易被掐 |
| 每日请求数 | 10 万 | 单链路可长连，但连接多了会累计 |
| 单请求并发出站连接 | **6** | 一条 WS 链路只能跑 6 条流；本项目靠多开链路扩容（`links`） |
| 禁止连接的目标 | Cloudflare 自己的 IP 段、私网、localhost、25 端口 | **任何走 Cloudflare 的站点都无法中转**（表现为中继回 `BLOCKED_TARGET`） |
| 入站 TCP | 不支持 | 所以只能以 WebSocket 为载具 |

### 条款风险（请认真读）

Cloudflare 自助服务协议 **2.2.1(j)** 明确写着：不得「use the Services to provide a virtual
private network or other similar proxy services」。也就是说，把 Worker 当代理中继**在条款上是
被禁止的**，账号可能被限制或停用。本项目因此：

* 默认**只放行学习/开发类站点**（`TSU_ALLOW_ALL` 默认关闭，白名单见 `torsocks5/defaults.py`）；
* 默认开启**智能分流**，只把名单内的站点送去中转，其余直连；
* README 顶部有完整的免责声明。

**不要**把它改成对任意目标开放的公共代理，也不要把你的 Worker 地址分享给别人。

### 部署与使用

```bash
# 1) 部署（零依赖、无需构建）
cd deploy/cloudflare
npx wrangler deploy
npx wrangler secret put TSU_TOKEN      # 自己定一个随机字符串

# 2) 写进配置
# [cf_relay]
# url = "wss://<你的-worker>.workers.dev/tsu"
# token = "<上面的 TSU_TOKEN>"

# 3) 先探测，再启动
torsocks5 tunnel probe --route cf-relay
torsocks5 run --route cf-relay
```

`*.workers.dev` 在部分网络里不可达（DNS 污染/SNI 阻断），此时需要给 Worker 绑一个**自己的域名**
（Cloudflare 面板 → Workers → 你的 Worker → Settings → Domains & Routes）。绑自有域名后把
`url` 改成 `wss://你的域名/tsu` 即可。

---

## 4. `self-relay`：自建 / 多平台中继（本仓库推荐的快路）

同一套 TSU/1 协议，但中继由你自己提供。三种常见形态：

### A. 本机 / 局域网主机（最简单）

```bash
# 终端 1：起中继（默认监听 127.0.0.1:9052）
torsocks5 relay serve                       # 只给本机用，不需要令牌
torsocks5 relay serve --token <随机串>       # 需要给局域网其它机器用时，务必带令牌

# 终端 2：代理走这个中继
torsocks5 run --route self-relay --relay-url ws://127.0.0.1:9052/tsu
```

配置写法（`~/.config/torsocks5/config.toml` 或 macOS 的
`~/Library/Application Support/torsocks5/config.toml`）：

```toml
[proxy]
route = "self-relay"

[self_relay]
url = "ws://127.0.0.1:9052/tsu"
token = ""
```

这种形态的实际收益：把代理逻辑（认证、ACL、连接数限制、日志、分流）与出口解耦。
如果中继跑在**另一台网络更好的机器**上（比如有 IPv6 的家宽、云主机），出口就跟着变好。

### B. 自有 VPS（最稳）

```bash
# VPS 上
torsocks5 relay serve --listen 0.0.0.0 --port 9052 --token <随机串>
# 建议前面套一层 Caddy/Nginx 做 TLS（ws:// → wss://），或用自带的 TLS：
torsocks5 relay serve --tls-cert /path/fullchain.pem --tls-key /path/privkey.pem
```

客户端 `url = "wss://你的域名/tsu"`。

### C. Deno Deploy（免费、无需服务器）

见 [`deploy/deno/README.md`](../deploy/deno/README.md)：`Deno.serve` + `Deno.connect`
原生支持 WebSocket 与出站 TCP，免费额度足够个人使用；可以绑自己的域名。

### 中继的目标策略

中继默认**只放行学习类站点白名单**（与 `cf-relay` 同一份），端口默认 `443,80,22,9418`：

```bash
torsocks5 relay serve --allow-all                    # 关闭白名单：任意 host:port（私有地址仍被拦）
torsocks5 relay serve --allow-host "example.com"     # 追加白名单
torsocks5 relay serve --allow-port 8080              # 追加端口
```

无论怎么配，**私有地址/回环/链路本地/CGNAT 一律拒绝**（`BLOCKED_TARGET`）——否则中继会变成
打穿内网的跳板。

---

## 5. 智能分流（`[split]`）

隧道路由（`cf-relay` / `self-relay`）默认开启分流：**没必要走隧道的东西就别走**。

| 模式 | 行为 | 谁在用 |
| --- | --- | --- |
| `auto`（默认） | 按路由自动决定 | 全部 |
| `smart` | 命中「需要辅助访问」名单 → 隧道；其余直连 | `cf-relay` 的 auto 结果 |
| `all` | 除私有地址外全部走隧道 | `self-relay` 的 auto 结果 |
| `off` | 不分流，全部走隧道 | 想完全当代理用时 |

判定顺序（先命中先算）：

1. `split.direct_hosts` 里明确写了的 → **直连**（可用来覆盖内置名单）
2. `split.proxy_hosts` 里明确写了的 → **隧道**
3. 私有地址（`10/8`、`172.16/12`、`192.168/16`、`127/8`、`169.254/16`、`100.64/10`、
   `::1`、`fc00::/7`、`fe80::/10`）→ **直连**（远端中继根本到不了）
4. 按模式：`smart` 时内置 59 条代理名单命中 → 隧道，否则直连

匹配规则是**后缀匹配**：写 `github.com` 就能覆盖 `api.github.com`、`raw.githubusercontent.com`
一类的子域；写 `.example.com` 则只匹配子域。规则以内置名单为准的话，可以直接
`python -c "from torsocks5.split import BUILTIN_PROXY_SUFFIXES as b; print(len(b), b)"` 查看。

分流只在**新建连接时**判定一次；已经在跑的连接不会因为规则改动而切换。

---

## 6. 实测数据

测试环境：macOS 15（Apple M2，本机网络），中继跑在同一台机器的 `127.0.0.1:9052`，
客户端 `split.mode = "off"`（强制全部走隧道）。测于 2026-09-28。

| 项目 | 结果 |
| --- | --- |
| 中继 `/healthz` | `{"ok": true, "proto": "tsu/1", "allow_all": true, "max_streams": 64}` |
| HTTPS 全链路（example.com，经隧道） | HTTP 200，1.93s，页面标题 `<title>Example Domain</title>` |
| 5 MB 下载（经隧道，含 TLS） | **798 KB/s**（6.27s） |
| 5 MB 下载（不走代理） | 742 KB/s（6.73s） |
| 并发 20 路（经隧道访问同一站点） | **20 成功 / 0 失败** |
| 隧道建立连接（中继→目标 TCP） | github.com:443 约 1~2 ms，example.com:443 约 1.2 s（取决于对端） |
| `torsocks5 tunnel check` 目标清单 | 3/3 可建立连接 |

结论：本机中继的隧道开销可以忽略（798 KB/s vs 742 KB/s，落在同一次测量的正常波动内）。
瓶颈不在协议，而在中继机器的出口带宽。对比 meek 的约 25 KB/s，快了约 30 倍。

> `cf-relay` 的实测数据受平台限制影响很大，且 `*.workers.dev` 在部分网络不可达。
> 本地 workerd（`wrangler dev`）验证的结果记录在
> [`deploy/cloudflare/README.md`](../deploy/cloudflare/README.md)。

---

## 7. 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| `路由 xxx 需要...` 后退出（退出码 2） | 配置缺失，按提示先部署中继/填 url |
| `无法连接中继`（退出码 3） | url 或 token 不对；`torsocks5 tunnel probe --relay-url ...` 单独验证 |
| 启动就报 `中继返回 HTTP 401` | 令牌不匹配，检查 `token` 与 Worker/中继上设置的是否一致 |
| 部分站点失败，错误码 `NOT_ALLOWED` | 该目标不在中继白名单：`cf-relay` 请改 Worker 变量；自建中继用 `--allow-host` |
| 错误码 `BLOCKED_TARGET` | 目标是私网地址，或（`cf-relay` 下）目标走 Cloudflare——平台不允许 Worker 连 CF 自己的 IP |
| 一律 `TOO_MANY_STREAMS`、越来越慢 | 单链路并发满了：调大 `links`（CF 版每条链路最多 6 条流） |
| 网页能开但很慢、大量超时 | `cf-relay` 撞上免费版 CPU 限制；换 `self-relay` |
| UDP 应用（DoUDP、游戏）不通 | 隧道路由只承载 TCP，属预期行为 |
