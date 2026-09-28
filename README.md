# TorSOCKS5

**跨平台 SOCKS5 本地代理：出口可选「Tor + meek 网桥」「Cloudflare Worker 中转」「自建隧道」。
meek 传输与隧道协议都是纯 Python 实现（运行期零第三方依赖），Windows / macOS / Linux 皆可。**

```
你的应用 ──SOCKS5──▶ TorSOCKS5 ──┬─ tor-meek   ── meek(HTTPS+CDN 前置) ──▶ Tor 网络 ──▶ 目标
                                  ├─ cf-relay   ── WSS(TSU/1 隧道) ──▶ Cloudflare Worker ──▶ 目标
                                  └─ self-relay ── WSS(TSU/1 隧道) ──▶ 你的中继(本机/VPS/Deno) ──▶ 目标
```

* 客户端只要一个普通的 SOCKS5 代理地址（默认 `socks5h://127.0.0.1:9051`）
* 三种路由方式随环境切换，都带**智能分流**（局域网/国内直连，只把需要加速的站点送出去）
* 支持源码运行、`pip` 安装，以及 CI 打包的单文件可执行程序

---

## ⚠️ 免责声明与使用范围（请先读）

**本项目仅供学习、研究与网络协议实验使用。**

* **作者不对任何使用后果负责。** 因使用本项目造成的任何直接或间接损失、账号封禁、
  服务中断、数据丢失或法律责任，一律由使用者自行承担。
* **违法使用后果自负。** 请自行确认你的用法符合所在地法律法规，以及你所依赖的服务
  （Tor、Cloudflare、Deno、你自己的 VPS 等）的服务条款。
* **建议只用它辅助访问学习类网站**：GitHub、Hugging Face、Docker Hub、PyPI、npm、
  crates.io、arXiv 等开发与学习基础设施。中继因此默认带**学习站点白名单**；
  请不要把它改成对任意目标开放的公共代理，也不要分享给别人。
* 不要用它做侵犯他人权益的事：扫描或攻击第三方、绕过付费墙、抓取隐私数据等。
* `cf-relay` 额外提示：Cloudflare 自助服务协议 **2.2.1(j)** 明确禁止把其服务用作
  VPN/代理，**账号有被限制或停用的风险**，请自行权衡（见 [docs/routes.md](docs/routes.md)）。
* 不要公开分享你申请到的 meek 网桥、Cloudflare Worker 地址或中继令牌——
  滥用导致的封禁会落到**真正需要它们的人**身上。

---

## 三种流量路由方式

| | `tor-meek` | `cf-relay` | `self-relay` |
| --- | --- | --- | --- |
| 出口 | Tor 网络（meek 网桥） | 你部署的 Cloudflare Worker | 你自己的中继（本机 / VPS / Deno Deploy） |
| 实测吞吐 | 约 25 KB/s（协议天花板） | 受免费版限制（见 §路由 2） | **约 798 KB/s，≈直连** |
| 首次可用 | 10~30 分钟 | 部署约 1 分钟 | 约 10 秒 |
| 抗封锁 | 最强（CDN 域前置） | 中（看中继域名是否被封） | 看你的线路与域名 |
| 目标范围 | 任意地址（含 `.onion`） | 仅白名单内的学习类站点 | 可配置 |
| 合规风险 | 低 | **高（CF 条款）** | 低 |

```bash
torsocks5 routes                       # 看三种方式，以及当前环境各自缺什么
torsocks5 run                          # 默认 = 路由 1（tor-meek）
torsocks5 run --route cf-relay         # 路由 2：先部署 deploy/cloudflare/
torsocks5 relay serve                  # 路由 3：终端 1 起中继
torsocks5 run --route self-relay --relay-url ws://127.0.0.1:9052/tsu   # 终端 2 起代理
```

**怎么选：** 审查最严、什么都被封 → 路由 1；没有自己的服务器 → 路由 2；
有台能常驻的机器（旧电脑/路由器/NAS/VPS）→ 路由 3，体验最好。

完整对比、平台限制、实测数据与排障：**[docs/routes.md](docs/routes.md)**；
隧道线格式（Python / Cloudflare Worker / Deno 三端一致的规范）：
**[docs/tunnel-protocol.md](docs/tunnel-protocol.md)**。

---

## 目录

- [三种流量路由方式](#三种流量路由方式)
- [为什么会有这个项目](#为什么会有这个项目)
- [快速开始](#快速开始)
- [路由 1：Tor + meek 网桥](#路由-1tor--meek-网桥)
- [路由 2：Cloudflare Worker 中转](#路由-2cloudflare-worker-中转)
- [路由 3：自建 / 多平台中继](#路由-3自建--多平台中继)
- [命令行详解](#命令行详解)
- [配置说明](#配置说明)
- [在各种应用里使用](#在各种应用里使用)
- [开机自启 / 服务化](#开机自启--服务化)
- [工作原理](#工作原理)
- [实测数据与限制](#实测数据与限制)
- [故障排查](#故障排查)
- [开发与测试](#开发与测试)
- [安全与合规](#安全与合规)
- [许可证](#许可证)

---

## 为什么会有这个项目

在受审查的网络里，Tor 的直连中继地址和端口很容易被识别并封锁，于是需要**网桥（bridge）**。
其中 **meek** 类网桥把 Tor 流量伪装成访问某个大型 CDN（微软、Cloudflare、jsDelivr…）的普通 HTTPS 请求：

```
你 ──TLS(SNI: cdn.jsdelivr.net)──▶ CDN ──Host: meek.example──▶ meek 网桥 ──▶ Tor 网络
    审查者看到：你在访问一个 CDN 的静态资源
```

普通用户的困难在于：官方只提供**预编译的 Tor Browser**，或者需要 Go 工具链自己编译
`meek-client` 插件。TorSOCKS5 把这件事变成「装个 Python 包，跑一条命令」。

但 meek 的速度有硬上限，所以本项目后来又加了两种**不经过 Tor**的路由方式：
把中继放在 Cloudflare 免费 Worker 上（`cf-relay`），或放在你自己的机器上（`self-relay`），
用自研的 TSU/1 隧道协议传流量——快得多，代价是绕过审查的能力不如 meek。

## 快速开始

### 环境要求

| 项目 | 要求 |
| --- | --- |
| Python | 3.8 及以上（Windows / macOS / Linux） |
| tor | **只有路由 1（tor-meek）需要**；路由 2 / 3 完全不需要 tor |
| 网络 | 路由 1 需要能连通某个 CDN 前置域名 |

**安装 tor**（仅路由 1 需要）

```bash
# macOS
brew install tor

# Debian / Ubuntu
sudo apt update && sudo apt install tor

# Windows：用官方 Tor Expert Bundle（或 Tor Browser 自带的那份）
# 安装后 torsocks5 会自动在常见位置找到它；
# 也可以 torsocks5 fetch-tor 自动下载，或在配置里写 tor.binary = "C:\\path\\to\\tor.exe"
```

### 第一步：安装并自检

```bash
# 1. 获取（或安装）本项目
git clone https://github.com/Mofan2020/TorSOCKS5.git
cd TorSOCKS5
pip install -e .            # 可选，直接 python torsocks5_cli.py 也能跑

# 2. 看三种路由方式，以及当前环境各自缺什么
torsocks5 routes

# 3. 环境体检（tor / 网桥 / 端口 / 插件路径）
torsocks5 doctor
```

### 第二步：选一种路由方式启动

**A. Tor + meek 网桥（默认，最抗封锁、最慢）**

```bash
torsocks5 bridges add "Bridge meek 0.0.2.0:3 url=https://... front=..."
torsocks5 run                       # 等价于 --route tor-meek
```

> **慢是正常现象，这不是 bug。** meek 每传 64KB 就要一次完整 HTTP 往返，
> 实测上限约 25 KB/s；首次引导要下载约 7MB 目录信息，可能花 10~30 分钟，
> 进度会长时间停在 `50%~99% loading_descriptors`。
> 「什么算正常 / 什么算坏了」的完整对照表见[路由 1 小节](#路由-1tor--meek-网桥)。

**B. Cloudflare Worker 中转（没有服务器时的免费选项）**

```bash
cd deploy/cloudflare
npx wrangler deploy                      # 零依赖、无需构建
npx wrangler secret put TSU_TOKEN        # 自己定一个随机字符串

# 把地址写进配置： [cf_relay] url = "wss://<你的-worker>.workers.dev/tsu"
torsocks5 tunnel probe --route cf-relay  # 先探测：真连一次，看哪些站点可用
torsocks5 run --route cf-relay
```

**C. 自建中继（最快，推荐）**

```bash
# 终端 1：起中继（默认监听 127.0.0.1:9052，只给本机用，不需要令牌）
torsocks5 relay serve

# 终端 2：代理走这个中继
torsocks5 run --route self-relay --relay-url ws://127.0.0.1:9052/tsu
```

也可以把中继部署到免费的 Deno Deploy（见 [deploy/deno/](deploy/deno/)）或自有 VPS。

### 第三步：验证

```bash
# 出口 IP 应该和你的真实 IP 不同
curl -x socks5h://127.0.0.1:9051 https://api.ipify.org; echo

# DNS 是否也走代理（socks5h:// 表示由代理解析域名）
curl -x socks5h://127.0.0.1:9051 https://www.dnsleaktest.com
```

启动成功的输出：

```
代理已就绪

SOCKS5 地址: 127.0.0.1:9051
路由方式: self-relay（自建 / 多平台中继）
路由状态: 中继 ws://127.0.0.1:9052/tsu · 链路 1/4（活跃 1）· 并发流 0/256 · 累计 0
UDP ASSOCIATE: 不支持（隧道路由只承载 TCP）
```

### 不想装包？直接跑

```bash
python3 torsocks5_cli.py run
python3 torsocks5_cli.py doctor
python3 torsocks5_cli.py selftest
```

### 用打包好的可执行程序（Windows / macOS / Linux）

从 [Releases](https://github.com/Mofan2020/TorSOCKS5/releases) 下载对应平台的压缩包，解压后：

```bat
:: Windows
TorSOCKS5.exe doctor
TorSOCKS5.exe run --route cf-relay
```

```bash
# macOS / Linux
chmod +x TorSOCKS5-linux-x86_64   # 或 TorSOCKS5-macos-arm64
./TorSOCKS5-linux-x86_64 run --route self-relay
```

## 路由 1：Tor + meek 网桥

```
你 ──▶ TorSOCKS5 ──▶ tor 客户端 ──meek(HTTPS+CDN 域前置)──▶ meek 网桥 ──▶ Tor 网络 ──▶ 目标
```

* 本项目的初始形态，也是唯一能访问 `.onion` 的路由；
* meek 传输是**纯 Python 实现**的：不需要 Go、不需要编译、不需要 Tor Browser；
* 需要一条自己的 meek 网桥（见下文「获取 meek 网桥」）。

### 什么算正常，什么算坏了

| 现象 | 判断 |
| --- | --- |
| 进度停在 50%–99%，几十分钟后仍在缓慢推进 | ✅ 正常，就是慢 |
| 引导期间 tor 日志刷屏 `no running bridges known` | ✅ 正常，是 tor 的背压机制 |
| 进度**完全不动**，且 `[meek] verbose` 统计里「接收字节」也不再增长 | ⚠️ 可能真断了，用 `torsocks5 bridges test` 换网桥 |
| 立刻返回 `403` / `404` | ❌ 这条网桥已失效，换一条 |
| 卡在 `2% conn_done_pt` | ⚠️ 已连上插件、在等网桥响应 → 网桥后端可能下线 |

可用的调整：

```bash
torsocks5 run --ready-timeout 1800            # 引导等待放宽到 30 分钟
torsocks5 run --ready-timeout 0 --keep-going  # 不等引导完成，先开始监听
torsocks5 run --no-bridge                     # 网络没被封锁时直连 Tor，快得多
```

**想更快就换路由**：obfs4 / snowflake / webtunnel 网桥都比 meek 快，本项目对这些网桥行
是透传支持的（在配置里加上对应传输插件即可）；或者直接用
[路由 2](#路由-2cloudflare-worker-中转) / [路由 3](#路由-3自建--多平台中继)。

### 限制

* **UDP 只到 tor 这一层。** 代理的 UDP ASSOCIATE 会转发给 tor，但 meek 隧道只承载 TCP。
* **不是浏览器级指纹伪装。** 见 `utls=` 参数说明与「实测数据与限制」。

### 获取 meek 网桥

meek 网桥**不会**自动出现在公共列表里（那正是它抗封锁的意义），需要你主动获取。
本项目**不内置任何可用的网桥地址**，请按下面任一方式获取自己的。

#### 方式 A：Tor 官方桥分发（推荐）

1. 打开 <https://bridges.torproject.org/>
2. 「Get Bridges」→ 选择 **Meek** → 填邮箱 / 解决验证码
3. 复制得到的那一行，形如：

   ```
   Bridge meek 0.0.2.0:3 url=https://xxxx.rsc.cdn77.org front=www.example.com utls=HelloRandomizedALPN
   ```

4. 导入本项目：

   ```bash
   torsocks5 bridges add "Bridge meek 0.0.2.0:3 url=https://xxxx.rsc.cdn77.org front=www.example.com"
   ```

   或者直接从剪贴板/文件导入：

   ```bash
   torsocks5 bridges import --clipboard      # macOS 用 pbpaste，Linux 用 xclip/wl-paste
   torsocks5 bridges import --file bridges.txt
   torsocks5 bridges import --url https://example.com/my-bridges.txt
   ```

> 想通过邮件获取：发邮件到 `bridges@torproject.org`，主题留空，正文写 `get transport meek`。

#### 方式 B：复用已有的 tor 配置

如果你机器上已经能用的 tor 配置里有 meek 网桥，直接抄过来：

```bash
grep '^Bridge meek' ~/.tor/torrc
torsocks5 bridges import --file <(grep '^Bridge meek' ~/.tor/torrc)
```

#### 验证网桥是否可用

```bash
# 只测这一条网桥能不能引导成功（会真的启动 tor）
torsocks5 bridges test "Bridge meek 0.0.2.0:3 url=... front=..."
```

网桥管理的完整命令：

```bash
torsocks5 bridges list                 # 列出全部
torsocks5 bridges add "Bridge meek ..."  # 新增（可重复传多条）
torsocks5 bridges rm cdn77.org         # 按子串删除
torsocks5 bridges clipboard            # 打印剪贴板内容
torsocks5 bridges normalize "..."      # 校验并规范化一行网桥配置
```

网桥保存在 `bridges.toml`（与配置文件同目录），格式是普通 TOML，可以手工编辑。

## 路由 2：Cloudflare Worker 中转

```
你 ──▶ TorSOCKS5 ──WSS(TSU/1 隧道)──▶ Cloudflare Worker ──TCP──▶ 目标（白名单内）
```

不需要服务器，用 Cloudflare 免费 Worker 当出口。代价是平台限制与条款风险都很实在。

### 部署（约 1 分钟）

```bash
cd deploy/cloudflare
npx wrangler deploy                          # 仓库里的 Worker 零依赖、无构建步骤
npx wrangler secret put TSU_TOKEN            # 定一个随机串（客户端要用同一个）
```

配置：

```toml
[cf_relay]
url = "wss://<你的-worker>.workers.dev/tsu"
token = "<上面的 TSU_TOKEN>"
```

### 先探测再启动

```bash
torsocks5 tunnel probe --route cf-relay     # 健康检查 + 真连 4 个学习站点
torsocks5 tunnel check --route cf-relay     # 更长的目标清单
torsocks5 run --route cf-relay
```

### 平台硬限制（决定了它「降低稳定性与兼容性」的定位）

| 限制 | 值 / 影响 |
| --- | --- |
| 每请求 CPU | 免费版 **10 ms** —— 高速长时间传输容易被掐 |
| 每日请求 | 10 万 |
| 单请求并发出站连接 | **6** —— 所以每链路只能跑 6 条流，本项目靠多开链路扩容（`links`） |
| 禁止连接的目标 | Cloudflare 自己的 IP 段、私网、localhost、25 端口 —— **任何走 Cloudflare 的站点都无法中转** |
| 入站 TCP | 不支持 —— 所以只能用 WebSocket 当载具 |

### 合规风险

Cloudflare 自助服务协议 2.2.1(j) 明确禁止「用其服务提供 VPN 或类似代理服务」。
所以本项目在这一路由下**默认只放行学习类站点**（`TSU_ALLOW_ALL` 默认关闭），
并默认开启智能分流。**不要**改成对任意目标开放，也不要分享你的 Worker 地址。

## 路由 3：自建 / 多平台中继

```
你 ──▶ TorSOCKS5 ──WSS(TSU/1 隧道)──▶ 你的中继(本机/VPS/Deno Deploy) ──TCP──▶ 目标
```

同一套 TSU/1 协议，中继换成你自己的机器：不依赖 Tor、不借别人的免费额度、
不涉及第三方条款，速度接近直连（实测 798 KB/s vs 直连 742 KB/s）。

### 本机 / 局域网（最简单）

```bash
# 终端 1
torsocks5 relay serve                       # 默认 127.0.0.1:9052
torsocks5 relay serve --token <随机串>       # 要给局域网其它机器用时必须带令牌
torsocks5 relay token                       # 帮你生成一个随机令牌

# 终端 2
torsocks5 run --route self-relay --relay-url ws://127.0.0.1:9052/tsu
```

### 自有 VPS（最稳）

```bash
torsocks5 relay serve --listen 0.0.0.0 --port 9052 --token <随机串> \
    --tls-cert /path/fullchain.pem --tls-key /path/privkey.pem
# 也可以放在 Caddy/Nginx 后面做 TLS 终结（把 ws:// 变 wss://）
```

### Deno Deploy（免费）

见 [deploy/deno/](deploy/deno/)：`Deno.serve` + `Deno.connect` 原生支持 WebSocket 与出站 TCP。

### 中继的目标策略

```bash
torsocks5 relay serve --allow-all                  # 关闭白名单：任意 host:port
torsocks5 relay serve --allow-host "example.com"    # 追加白名单
torsocks5 relay serve --allow-port 8080             # 追加端口（默认 443,80,22,9418）
torsocks5 relay serve --allow-private               # ⚠️ 放开私有地址限制，**仅供本机联调**
```

默认情况下**私有地址 / 回环 / 链路本地 / CGNAT 一律拒绝**，否则中继就是打穿内网的跳板。
唯一的例外是 `--allow-private`：它存在只是为了本机调试（单元测试与冒烟测试要连
`127.0.0.1` 上的假目标），启动时会打印警告。**对外提供服务的中继不要开这个开关。**

### 智能分流

隧道路由默认按规则分流：**局域网与国内流量直连，只把需要辅助访问的站点送进隧道**。

```toml
[split]
mode = "auto"        # auto | smart | all | off
# proxy_hosts = ["example.com"]     # 强制走隧道
# direct_hosts = ["intranet.example"]  # 强制直连（可覆盖内置名单）
```

`smart` 模式的内置名单（59 条）覆盖 GitHub、Hugging Face、Docker Hub、PyPI、npm、
crates.io、Ubuntu/Debian 源、arXiv 等；`self-relay` 的 auto 是 `all`（除私有地址全走隧道）。
匹配是后缀匹配，写 `github.com` 即覆盖 `api.github.com`。

## 命令行详解

```
torsocks5 [全局参数] <子命令> [子命令参数]

全局参数
  --config PATH     指定配置文件（默认见下）
  --log-file PATH   同时把日志写入文件
  -v, --verbose     详细日志（含 tor 与 meek 内部事件）
  -V, --version     版本号
  --transport-plugin  把自己当作可插拔传输插件启动（tor 会这么调用，普通用户不用管）
```

### `run` — 启动代理

```bash
torsocks5 run                          # 用配置文件的默认值（默认路由 tor-meek）
torsocks5 run --route self-relay       # 换出口：tor-meek | cf-relay | self-relay
torsocks5 run --relay-url ws://127.0.0.1:9052/tsu   # 临时指定中继地址
torsocks5 run --relay-token <令牌>      # 临时指定中继令牌
torsocks5 run --port 1080              # 换端口
torsocks5 run --listen 0.0.0.0         # 监听所有网卡（务必同时配置 allow_from 与认证！）
torsocks5 run --bridge "Bridge meek ..."  # 临时追加网桥，不写进配置文件
torsocks5 run --no-bridge              # 不用网桥，直连 Tor（网络没被墙时更快）
torsocks5 run --upstream 127.0.0.1:9050  # 复用已有的 SOCKS5 端口，只做转发
torsocks5 run --tor /opt/homebrew/bin/tor  # 指定 tor 路径
torsocks5 run --ready-timeout 600      # 引导超时（秒），默认 300
torsocks5 run --keep-going             # 引导失败也继续监听
```

退出码：`0` 正常退出 · `2` 参数/配置错误 · `3` 路由启动失败 · `4` 就绪超时。

### `routes` — 三种路由方式与就绪情况

```bash
torsocks5 routes
```

会列出三种路由、标记当前配置用的是哪个，并检查各自缺什么
（tor 是否存在、有无网桥、`[cf_relay] url` / `[self_relay] url` 是否配好）。

### `relay` — 自建中继（路由 3 的服务端）

```bash
torsocks5 relay serve                       # 起中继，默认 127.0.0.1:9052
torsocks5 relay serve --token <令牌>         # 设访问令牌（对外监听时必须设）
torsocks5 relay token                       # 生成一个随机令牌
torsocks5 relay serve --listen 0.0.0.0 --port 9052 --token <令牌>
torsocks5 relay serve --allow-all           # 关闭目标白名单（任意 host:port）
torsocks5 relay serve --allow-host "example.com"   # 追加白名单
torsocks5 relay serve --allow-port 8080     # 追加允许端口
torsocks5 relay serve --max-streams 128     # 单连接并发流上限（默认 64）
torsocks5 relay serve --tls-cert cert.pem --tls-key key.pem   # 直接跑 wss://
```

中继自带 `GET /healthz`（返回协议版本、并发上限等，无需令牌）和
`GET /`（人肉可读的自检页）。**私有地址默认一律拒绝**，即使开了 `--allow-all`；
只有本机联调才用的 `--allow-private` 能放开它。

### `tunnel` — 探测中继是否真的可用

```bash
torsocks5 tunnel probe --route cf-relay          # 健康检查 + 真连 4 个学习站点
torsocks5 tunnel check --route self-relay        # 更长的目标清单（11 个）
torsocks5 tunnel check --relay-url wss://... --relay-token xxx
torsocks5 tunnel check --hosts "github.com:443,pypi.org:443" --http
torsocks5 tunnel check --front cdn.example.com   # 域前置：TLS SNI 用这个域名
torsocks5 tunnel check --insecure                # 不校验证书（中继用自签证书时）
torsocks5 tunnel check --timeout 30              # 单个目标的超时秒数，默认 15
```

它是**真的建流、真的转发**（`--http` 还会发一个 HTTP 请求验证双向数据），
不是只 ping 端口。报告里会给出每个目标的连接耗时或失败原因（`NOT_ALLOWED` /
`BLOCKED_TARGET` / `TOO_MANY_STREAMS` 等）。

### `doctor` — 环境自检

依次检查：Python 版本、tor 可执行文件与是否内置 meek、插件启动路径是否含空格、
网桥配置是否合法、代理端口是否被占用、CDN 前置域名能否连通、本地 SOCKS5 回环自检。

```bash
torsocks5 doctor
torsocks5 doctor --port 1080
```

### `selftest` — 完全离线的自检

不联网、不需要 tor，验证本机各层逻辑（用于排除安装问题）：

```
[1/5] SOCKS5 服务端（免认证 / 用户名密码 / 错误密码拒绝）
[2/5] UDP ASSOCIATE
[3/5] meek 隧道（HTTP 往返 / 域前置 / 大包分片）
[4/5] 可插拔传输协议握手（模拟 tor 启动插件）
[5/5] tor 可执行文件
```

### `bridges` — 网桥管理

```bash
torsocks5 bridges list                       # 列出全部网桥
torsocks5 bridges add "Bridge meek 0.0.2.0:3 url=... front=..."
torsocks5 bridges rm cdn77.org               # 按子串删除
torsocks5 bridges import --clipboard         # 或 --file / --url
torsocks5 bridges test "Bridge meek ..."     # 真的启动 tor 验证这条网桥
torsocks5 bridges test "Bridge meek ..." --timeout 300   # 等更久，默认 120 秒
torsocks5 bridges normalize "..."            # 校验并规范化一行网桥配置
torsocks5 bridges clipboard                  # 打印剪贴板内容
```

网桥保存在 `bridges.toml`；获取方式见[路由 1 · 获取 meek 网桥](#路由-1tor--meek-网桥)。

### `config` — 配置管理

```bash
torsocks5 config init        # 生成带注释的配置模板
torsocks5 config init --force   # 已存在时覆盖
torsocks5 config show        # 打印当前生效配置
torsocks5 config show --format json
torsocks5 config path
```

### `install-service` — 生成/安装后台服务

```bash
torsocks5 install-service              # 按平台自动选择，只打印不落盘
torsocks5 install-service systemd      # 生成 systemd user unit
torsocks5 install-service launchd      # 生成 launchd plist
torsocks5 install-service schtasks     # 生成 Windows 计划任务
torsocks5 install-service systemd --apply   # 真正写入
```

### `fetch-tor` — 下载 tor 官方专家包

```bash
torsocks5 fetch-tor                              # 自动挑选平台对应的包
torsocks5 fetch-tor --version 0.4.8.12
torsocks5 fetch-tor --file ~/Downloads/tor-expert-bundle-....tar.gz
torsocks5 fetch-tor --mirror https://mirror.example.org
torsocks5 fetch-tor --dest /opt/tor-bundle       # 指定解压目录
```

下载完成后会提示把路径写进配置：

```toml
[tor]
binary = "/path/to/tor"
```

## 配置说明

配置文件位置（`torsocks5 config path` 可查）：

| 平台 | 默认路径 |
| --- | --- |
| macOS | `~/Library/Application Support/torsocks5/config.toml` |
| Linux | `~/.config/torsocks5/config.toml` |
| Windows | `%APPDATA%\torsocks5\config.toml` |

完整示例见 [`torsocks5/config.example.toml`](torsocks5/config.example.toml)。常用项：

```toml
[proxy]
listen = "127.0.0.1"        # 监听地址；对外暴露务必改 0.0.0.0 并加认证
port = 9051
route = "tor-meek"          # 出口：tor-meek | cf-relay | self-relay
username = ""               # 非空则启用 RFC 1929 认证
password = ""
allow_from = ["127.0.0.1", "::1"]   # 访问控制，按顺序第一条命中生效
udp_associate = true        # 隧道路由只承载 TCP，开了也无效
max_connections = 512
idle_timeout = 300          # 空闲超时（秒）

[split]                     # 智能分流，只对 cf-relay / self-relay 生效
mode = "auto"               # auto | smart | all | off
builtin_proxy = true        # 用内置的「需要辅助访问」名单（59 条）
builtin_direct = true       # 用内置的国内/局域网直连名单
# proxy_hosts = ["example.com"]        # 强制走隧道
# direct_hosts = ["intranet.example"]  # 强制直连（优先于内置名单）

[cf_relay]                  # 路由 2：Cloudflare Worker 中转
url = ""                    # wss://<你的-worker>.workers.dev/tsu
token = ""
# links = 4                 # 并发 WS 链路数（每条链路最多 6 条流）
# max_streams = 6           # 平台硬限制，别调大

[self_relay]                # 路由 3：自建 / 多平台中继
url = ""                    # ws://127.0.0.1:9052/tsu 或 wss://你的域名/tsu
token = ""
# links = 4
# max_streams = 64

[relay]                     # 自建中继服务端（torsocks5 relay serve 读取）
listen = "127.0.0.1"
port = 9052
token = ""                  # 空 + 非回环监听 = 开放代理，会被警告
allow_all = false           # false 时只放行 allow_hosts 命中的目标
max_streams = 64

[tor]
# binary = ""               # 留空自动探测
# direct = false            # true = 不用网桥直连 Tor
meek_mode = "plugin"        # plugin = 本项目的 Python 传输（默认）
log_level = "notice"        # err/warn/notice/info/debug
restart = true              # tor 意外退出自动重启
avoid_disk_writes = false   # true = 不落盘（更干净，但每次都要重下 geoip）
isolation = ["IsolateSOCKSAuth", "IsolateClientProtocol"]  # 流量隔离
# extra_options = ["TorCircuitBuildTimeout 120"]

[meek]
methods = ["meek", "meek_lite", "meek_azure"]  # 兼容不同网桥行的写法
connect_timeout = 20.0
read_timeout = 30.0
verbose = false             # true = 打印通道吞吐统计，排障很有用

[bridges]
builtin = false             # 无网桥时是否回落到内置的公开网桥（默认不回落）
```

> **关于 `meek_mode`**
> * `plugin`（默认）：用本项目内置的 Python meek 传输，任何 tor 版本都能用。
> * `builtin`：用 tor 自己编译进去的 meek（需要你的 tor 带 meek 支持），此时不启动 Python 插件。
> * `off`：完全不注册传输插件，配合 `direct = true` 使用。

## 在各种应用里使用

```bash
# curl（socks5h = 域名也交给代理解析，避免本地 DNS 泄漏）
curl -x socks5h://127.0.0.1:9051 https://ifconfig.me

# 开启了认证时
curl -x socks5h://user:pass@127.0.0.1:9051 https://ifconfig.me

# git
git config --global http.proxy socks5h://127.0.0.1:9051
git clone https://github.com/Mofan2020/TorSOCKS5.git

# pip
pip config set global.proxy socks5h://127.0.0.1:9051
# 或临时：PIP_PROXY=socks5h://127.0.0.1:9051 pip install xxx

# SSH（通过 SOCKS5 代理，注意用 nc 或 connect）
ssh -o ProxyCommand='nc -X 5 -x 127.0.0.1:9051 %h %p' user@host

# Node.js
npm config set proxy socks5h://127.0.0.1:9051

# 浏览器：设置 → 网络 → 代理 → SOCKS v5，填 127.0.0.1 9051
# 「通过 SOCKS 解析主机名」要勾上，否则 DNS 仍走本地
```

系统级（macOS / Linux）：

```bash
# macOS：在「网络 → 高级 → 代理」里勾选 SOCKS 代理，填 127.0.0.1 9051
# Linux：设置 GNOME/ KDE 的网络代理，或 export all_proxy=socks5h://127.0.0.1:9051
```

## 开机自启 / 服务化

```bash
# macOS（launchd，用户级）
torsocks5 install-service launchd --apply
launchctl load -w ~/Library/LaunchAgents/com.torsocks5.agent.plist
tail -f ~/Library/Logs/torsocks5.log

# Linux（systemd，用户级，无需 root）
torsocks5 install-service systemd --apply
systemctl --user daemon-reload
systemctl --user enable --now torsocks5
journalctl --user -u torsocks5 -f

# Windows（计划任务，登录时启动；用管理员运行可改成开机启动）
torsocks5 install-service schtasks --apply
schtasks /Run /TN TorSOCKS5
```

也可以直接用 Docker：

```bash
docker build -t torsocks5 .
docker run -d --name torsocks5 -p 9051:9051 -v torsocks5-data:/data torsocks5
docker exec torsocks5 torsocks5 doctor
```

## 工作原理

### 分层结构

```
torsocks5/
├── cli.py                 命令行入口（run / routes / relay / tunnel / doctor / bridges / ...）
├── config.py              TOML 配置（3.11+ 用 tomllib，低版本用内置精简解析器）
├── defaults.py            共享默认值：学习站点白名单、允许端口
├── hostrules.py           主机名/地址匹配语义（后缀匹配、私有地址判定）
├── split.py               智能分流：决定「直连」还是「走隧道」
├── bridges.py             网桥行解析、校验、去重、存储
├── log.py                 跨平台彩色日志
├── selftest.py            离线自检
├── service.py             systemd / launchd / schtasks 配置生成
├── routes/                三种流量路由方式
│   ├── base.py            路由抽象、RouteOptions、RouteError（含退出码语义）
│   ├── tor_meek.py        路由 1：Tor + meek 网桥
│   ├── tunnel_base.py     路由 2/3 的公共部分（隧道客户端 + 分流）
│   ├── cf_relay.py        路由 2：Cloudflare Worker 中转
│   ├── self_relay.py      路由 3：自建 / 多平台中继
│   └── upstream.py        --upstream 用：复用已有 SOCKS5 端口
├── tunnel/                TSU/1 隧道协议实现（路由 2/3 共用）
│   ├── protocol.py        帧编解码、地址编解码、目标策略
│   ├── wsframe.py         RFC 6455 帧与握手（零依赖）
│   ├── wsclient.py        WebSocket 客户端（掩码、分片、PING/PONG）
│   ├── wsserver.py        WebSocket 服务端
│   ├── stream.py          把一条隧道流包装成 socket 风格对象（可直接接进 SOCKS5 泵）
│   ├── client.py          多路复用客户端：链路池、断线重连、按负载分流
│   ├── relay.py           中继服务端参考实现（relay serve 用的就是它）
│   └── probe.py           中继探测（tunnel probe/check）
├── socks5/
│   ├── protocol.py        RFC 1928 / 1929 编解码
│   ├── client.py          SOCKS5 客户端（连上游用）
│   └── server.py          SOCKS5 服务端（对外提供，支持 ACL / UDP / 认证 / connector 钩子）
├── tor/
│   ├── find.py            跨平台查找 tor 可执行文件
│   ├── control.py         控制端口客户端（查进度、优雅关闭）
│   └── manager.py         torrc 生成、进程管理、自动重启、无空格路径 shim
└── meek/
    ├── channel.py         meek 隧道核心：HTTP 轮询 + 域前置
    ├── socks_server.py    传输插件侧的 SOCKS5 服务端（接收 tor 的 CONNECT 与网桥参数）
    ├── pt.py              可插拔传输（PT）协议前端
    └── mock_server.py     测试用 meek 服务端（与官方 meek-server 协议等价）

deploy/
├── cloudflare/            路由 2：Cloudflare Worker（零依赖，wrangler deploy）
└── deno/                  路由 3：Deno Deploy 中继（Deno.serve + Deno.connect）

docs/
├── tunnel-protocol.md     TSU/1 协议规范（Python / Worker / Deno 三端唯一真相源）
├── routes.md              三种路由的对比、限制、实测与排障
└── notes.md               实施笔记：本版加了什么、取舍、没动的与原因
```

同一个仓库里的其他文档：`deploy/cloudflare/README.md`（Worker 部署）、
`deploy/deno/README.md`（Deno Deploy 部署）。要改这个项目，先读
[CONTRIBUTING.md](CONTRIBUTING.md)（开发环境、项目约定、协议改动的注意事项）。

### meek 隧道协议

与 Tor 官方 `meek`（[pluggable-transports/meek](https://git.torproject.org/pluggable-transports/meek.git)）
**协议兼容**：

| 方向 | 内容 |
| --- | --- |
| 请求 | `POST <url>`，`Content-Type: application/octet-stream`，`X-Session-Id: <会话 id>`，请求体是原始 Tor 字节（单次 ≤ 65536） |
| 响应 | `200 OK`，`Content-Type: application/octet-stream`，响应体是回传的 Tor 字节 |
| 会话 | 8 字节随机数 base64（去 `=`）作为 `X-Session-Id`，把一条 TCP 流拆成多个 HTTP 请求 |
| 轮询 | 空闲时 100ms 起、×1.5 退避、最长 5s；一旦有数据收发立刻再发 |
| 域前置 | DNS/TCP/TLS SNI 用 `front=`，HTTP `Host` 头用 `url=` 的主机，由 CDN 转发到网桥 |

网桥行里常见的参数都被支持：`url=`（必需）、`front=`（含旧写法 `CDNFronting=`）、
`utls=`（识别但忽略，见下）、`fragments=`、`ignorePQC`。

> **关于 `utls=`**
> 官方客户端用 Go 的 uTLS 库模拟浏览器 TLS 指纹。Python 标准库的 `ssl` 模块做不到这件事，
> 本项目**不模拟浏览器指纹**，而是使用 OpenSSL 的真实指纹。
> 在只做「TLS 被动识别」的网络里这通常无影响；若你的审查者做主动指纹识别，
> 请优先使用 Tor Browser（它自带官方 uTLS 客户端），或改用 snowflake 等传输。

### TSU/1 隧道协议（路由 2 / 3）

路由 2 与路由 3 用同一套自研协议：把 SOCKS5 的 TCP 流转成 **WebSocket 上的多路复用流**。
之所以用 WebSocket，是因为 Cloudflare 免费版**不支持入站 TCP**，WebSocket 是把流量送进去的
唯一可用载具；而它同时也是最不容易被中间设备误伤的形态。

```
WebSocket 握手：  Sec-WebSocket-Protocol: tsu.v1
二进制帧（一个 WS 消息 = 恰好一个 TSU 帧）：
 0        1        2        3        4        5 ...
+--------+--------+--------+--------+--------+-----------------+
| opcode |         stream id (uint32, big endian)   |   payload   |
+--------+--------+--------+--------+--------+-----------------+
 1 byte                4 bytes                        N bytes

opcode: 0x01 OPEN  0x02 OPEN_OK  0x03 OPEN_ERR  0x04 DATA
        0x05 CLOSE 0x06 RESET    0x07 PING      0x08 PONG
```

* 单条 WebSocket 连接（「链路」）承载多条流；流 id 由客户端分配（从 3 起递增），响应走同一个 id；
* 目标地址用「地址类型 + 长度 + 内容」编码，支持 IPv4 / 域名 / IPv6，
  域名**原样透传给中继解析**（不在本地做 DNS，避免泄漏）；
* 单条消息载荷上限 64 KiB，超过 32 KiB 的写入在协议层分片，配合 1 MiB 背压上限避免内存膨胀；
* 未定义的 opcode 按规范用 `RESET` 回应（流 id 为 0 时才断开连接），不会因为一个坏帧掐掉整条链路；
* `OPEN_ERR` 带错误码：`BAD_REQUEST` / `NOT_ALLOWED` / `HOST_UNREACHABLE` /
  `CONNECTION_REFUSED` / `BLOCKED_TARGET` / `TOO_MANY_STREAMS` / `INTERNAL_ERROR`；
  错误码会映射成正确的 SOCKS5 回复码（例如白名单拒绝 → `0x02` 不允许），
  不是笼统的「连接失败」；
* 半关闭（`SHUT_WR`）会被如实传递成对端 TCP 的 `shutdown(WR)`，HTTP/1.0 这类
  「请求完就关写方向、但仍等响应」的场景能正常工作；
* 单链路并发上限：Cloudflare 下是 **6**（平台硬限制），自建中继默认 **64**；
  满了客户端自动去开新链路，而不是把错误抛给上层。

完整线格式（含字节级布局、错误码表、中继策略要求）见
[docs/tunnel-protocol.md](docs/tunnel-protocol.md)；Python 客户端、Python 中继、
Cloudflare Worker、Deno 中继四个实现都以那份文档为准。

### 与 tor 的集成方式

本项目以**可插拔传输插件（ClientTransportPlugin）**的形式接入 tor：

```
ClientTransportPlugin meek,meek_lite,meek_azure exec /usr/bin/python3 /path/to/meek_pt.py
# 用打包好的可执行程序时，程序自己就是插件入口（多一个 --transport-plugin 参数）
ClientTransportPlugin meek,meek_lite,meek_azure exec /path/to/TorSOCKS5 --transport-plugin
```

tor 启动 `meek_pt.py` 后走 PT 行协议握手（`VERSION` / `CMETHOD` / `AUTHENTICATE` / `PROXY`），
然后连接插件公布的 SOCKS5 地址，用 RFC 1929 的 username/password 字段把网桥参数传进来。
这些参数由 `parse_pt_args()` 解析，兼容 `;` 与空格两种分隔写法。

**已知实现细节**：tor 解析 `ClientTransportPlugin` 时按空格朴素切分参数，不处理引号或转义。
因此当安装路径含空格时，`tor.plugin_command()` 会自动生成一个**无空格的启动脚本**
（POSIX 下是 `#!/bin/sh` 包装脚本，Windows 下是 `cmd.exe /c x.cmd`），
放在 `/tmp/torsocks5-pt-$UID/`（或 `%WINDIR%\Temp\torsocks5-pt`）再交给 tor。
`doctor` 会检查这一点。

### torrc 要点

生成的 torrc 位于 `<数据目录>/torrc`：

```
DataDirectory <独立目录>          # 不干扰系统里已有的 tor
SocksPort 127.0.0.1:<随机端口> IsolateSOCKSAuth IsolateClientProtocol
ControlPort 127.0.0.1:<随机端口>  # Cookie 认证
ClientOnly 1 / SafeLogging 1
UseBridges 1
Bridge meek 0.0.2.0:3 url=... front=...
__OwningControllerProcess <pid>     # 控制器退出时自动关闭 tor（POSIX）
```

## 实测数据与限制

### 隧道路由（路由 2 / 3）实测

测试环境：macOS（Apple M2），中继跑在同一台机器的 `127.0.0.1:9052`，
客户端 `split.mode = "off"`（强制全部走隧道），测于 2026-09-28：

| 指标 | 观测值 |
| --- | --- |
| 中继 `/healthz` | `{"ok": true, "proto": "tsu/1", "allow_all": true, "max_streams": 64}` |
| HTTPS 全链路（example.com，经隧道） | HTTP 200，1.93 s，拿到 `<title>Example Domain</title>` |
| 5 MB 下载（经隧道，含 TLS） | **798 KB/s**（6.27 s） |
| 5 MB 下载（不走代理，对照） | 742 KB/s（6.73 s） |
| 并发 20 路（经隧道访问同一站点） | **20 成功 / 0 失败** |
| 建流耗时（中继→目标 TCP） | github.com:443 约 1~2 ms；example.com:443 约 1.2 s（取决于对端） |
| 单元测试 | 130 个用例全绿（其中隧道相关 49 个，全部离线） |

结论：**本机中继的隧道开销可以忽略**（798 KB/s vs 742 KB/s，在同一测量的正常波动内），
瓶颈在中继机器的出口带宽，而不是协议本身。相比 meek 的约 25 KB/s 快了约 30 倍。

### 路由 1（meek）实测

在一条受审查网络（客户端在中国大陆）上，用真实 meek 网桥
（`url=https://*.rsc.cdn77.org` `front=www.example.com`）实测：

| 指标 | 观测值 |
| --- | --- |
| PT 握手 | tor 加载本项目插件后 1 秒内到达 `2% conn_done_pt` |
| 与网桥完成 Tor 握手 | 约 5 秒（`15% handshake_done`） |
| 隧道吞吐 | 单连接约 **25～50 KB/s**（64KB/次往返，取决于 CDN 延迟） |
| 连接稳定性 | 长跑 20+ 分钟、传输 7 MB+ 数据，**0 次通道错误** |
| CDN 错误率 | 约 15～20% 的请求被 CDN 注入 `HTTP 570`（短退避重试即可） |
| 微描述符下载 | 约 2.4 MB/12 分钟（9458 个中的 2378 个） |

**必须知道的限制**：

1. **meek 很慢，这是协议本身的特性，不是本项目的实现问题。**
   每 64KB 数据都要一个完整 HTTP 往返，吞吐上限就是 `64KB / RTT`。
   在高延迟链路上实测约 25 KB/s。浏览网页尚可，**大文件下载会非常慢**。
   **需要速度请用 obfs4 / snowflake / webtunnel**（本项目对这类网桥行是透传支持，
   在配置里加上对应的传输插件即可）。
2. **首次引导可能需要 10 分钟以上。** meek 首次要下载权威共识（~1MB）
   与中继微描述符（9458 个，~6MB），实测 12 分钟只下了 25%。
   之后重启会复用缓存，快得多。默认 `--ready-timeout` 为 300 秒，
   meek 环境下建议显式调大：

   ```bash
   torsocks5 run --ready-timeout 1800
   ```

   想跳过等待、直接开始监听（请求会暂时失败）：

   ```bash
   torsocks5 run --ready-timeout 0 --keep-going
   ```

3. **网桥会失效。** 前置 CDN 可能被封或下线。表现是 4xx（快速失败并提示换桥）
   或长时间停在某个进度。
4. **UDP 只到 tor 这一层。** 代理的 UDP ASSOCIATE 会转发给 tor，
   但 meek 隧道本身只承载 TCP，UDP 流量无法穿透 meek。
5. **不是浏览器级指纹伪装。** 见上文 `utls=` 说明。
6. **隧道路由只承载 TCP。** `cf-relay` / `self-relay` 下 UDP ASSOCIATE 会被直接拒绝
   （回复 SOCKS5 命令不支持），这是协议设计而非缺陷。
7. **`cf-relay` 受平台硬限制。** 免费版每请求 10 ms CPU、每天 10 万请求、
   单请求最多 6 条并发出站连接，且**无法中转任何走 Cloudflare 的站点**；
   另有条款风险（见下）。它是「没有服务器时能用」的方案，不是「稳定生产」的方案。
8. **中继白名单是默认关闭的门。** `self-relay` 默认只放行学习类站点与
   `443/80/22/9418` 端口；想放开就明确 `--allow-all`，别把开放中继暴露到公网。

## 故障排查

### `doctor` 报错对照

| 现象 | 原因与处理 |
| --- | --- |
| 找不到 tor | 装 tor，或在配置写 `tor.binary`，或 `torsocks5 fetch-tor` |
| 插件命令中仍有空格 | 移动到无空格目录，或设置 `tor.pt_shim_dir` |
| 没有启用任何网桥 | `torsocks5 bridges add "..."` 导入自己的网桥行 |
| 端口已被占用 | `torsocks5 run --port 1080` |
| 连不上 CDN 前置域名 | 这正是需要 meek 的原因；若你本就不受限制，可用 `--no-bridge` 直连 |

### 引导卡住

```bash
# 打开详细日志，能看到每个引导阶段与插件内部事件
torsocks5 run -v
# 打开通道吞吐统计
#   在配置里设 [meek] verbose = true

# 查看 tor 自己的日志
cat "$(torsocks5 config path | xargs dirname)/../Caches/torsocks5/tor.log"
# 或直接看本次运行的 torrc 同目录下的 tor.log
```

常见卡点：

| 卡在 | 含义 | 处理 |
| --- | --- | --- |
| `0%` 一直不动 | 插件没起来，或网桥连不上 | 看 `-v` 日志里有没有 `CMETHOD`；换网桥 |
| `2% conn_done_pt` | 已连上传输插件，在等网桥响应 | 网桥后端可能已下线，换桥 |
| `30% loading_status` | 正在下权威共识（约 1MB） | 耐心等，或提高 `--ready-timeout` |
| `50% ~ 99% loading_descriptors` | 正在下 9458 个中继微描述符（约 6MB，**最慢**） | 这是正常现象；meek 上可能要 10～30 分钟 |

> 引导期间 tor 日志里刷屏的 `Delaying directory fetches (no running bridges known)`
> 也是正常现象：meek 链路太慢，tor 会短暂认为桥连接不健康而暂缓拉取，
> 随后自动继续。判断是否真的卡住，看插件的吞吐统计（配置 `[meek] verbose = true`）：
> 只要「接收」字节数还在涨，就说明隧道是活的。

### 隧道路由（路由 2 / 3）排查

先用 `tunnel` 命令定位是「中继不通」还是「目标不通」：

```bash
torsocks5 tunnel probe --route cf-relay             # 快速：健康检查 + 4 个站点
torsocks5 tunnel check --route self-relay           # 完整：11 个站点
torsocks5 tunnel check --relay-url wss://... --relay-token xxx
```

| 现象 | 原因与处理 |
| --- | --- |
| `无法连接中继`（退出码 3） | url 写错、域名不可达，或 token 不对（`HTTP 401`） |
| 报错码 `NOT_ALLOWED` | 目标不在中继白名单：自建中继用 `--allow-host`；`cf-relay` 需要改 Worker 变量 |
| 报错码 `BLOCKED_TARGET` | 目标是私网/回环地址；或 `cf-relay` 下目标本身走 Cloudflare（平台禁止 Worker 连 CF 的 IP） |
| 报错码 `TOO_MANY_STREAMS` | 单链路并发满了：调大 `links`（CF 每条链路最多 6 条流） |
| 网页能开但很慢 | `cf-relay` 撞上免费版 CPU 限制 → 换 `self-relay` |
| UDP 应用不通 | 隧道路由只承载 TCP，属预期 |
| 想确认分流是否生效 | 用 `torsocks5 -v relay serve`（加 `-v` 才会逐条打印目标）；直连的连接不会出现在中继日志里 |

### 4xx / 5xx 状态码含义

程序对 HTTP 状态做了分级处理，日志里会直接说明：

* **403 / 404**：请求被 CDN 或网桥拒绝 —— **这条网桥不可用**，程序会立刻失败并提示换桥
  （官方客户端在这里会傻等 30 秒 × 10 次，本项目做了优化）
* **500 / 502 / 570**：CDN 或网桥的临时错误 —— 短退避（0.5→1→2→4→5 秒）后重试
* **连接中断 / 状态行异常**：CDN 掐断了 keep-alive 连接 —— 自动重连重试（最多 20 次）

### 排障命令速查

```bash
torsocks5 selftest                       # 离线自检，排除本地安装问题
torsocks5 doctor                         # 环境体检
torsocks5 routes                         # 三种路由方式与就绪情况
torsocks5 bridges test "Bridge meek ..."  # 单独验证某条网桥
torsocks5 tunnel check --route cf-relay   # 单独验证中继（真连真转发）
torsocks5 relay serve                     # 本机起中继，绕开外部依赖做对照实验
torsocks5 run --upstream 127.0.0.1:9050   # 你的 tor 已经能用了？只借它的 SOCKS 端口
python3 torsocks5_cli.py -v run          # 源码直接跑 + 详细日志
```

## 开发与测试

```bash
git clone https://github.com/Mofan2020/TorSOCKS5.git
cd TorSOCKS5
pip install -e ".[dev]"

# 单元测试（130 个用例，全部离线、不需要网络与 tor）
python -m unittest discover -s tests -v

# 离线端到端自检
python torsocks5_cli.py selftest

# 静态检查
ruff check .
mypy torsocks5

# 中继实现的测试（各自的目录里有说明）
cd deploy/cloudflare && node --test                  # Cloudflare Worker（54 个用例）
cd deploy/deno && deno test --allow-net --allow-env --allow-read   # Deno 中继（23 个用例）

# 跨语言互通：用本项目的 Python 客户端连真实运行的 JS/TS 中继，并真的转发一次数据
python scripts/interop_relay.py \
    --start "cd deploy/deno && deno run --allow-net --allow-env main.ts" \
    --port 8791 --token devtoken
python scripts/interop_relay.py \
    --start "cd deploy/cloudflare && npx wrangler dev --port 8790 --var TSU_TOKEN:devtoken" \
    --port 8790 --token devtoken

# 本地打包
pip install pyinstaller && pyinstaller torsocks5.spec
```

测试分层：

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/test_meek.py` | meek 通道：HTTP 往返、域前置、keep-alive、大包分片、TLS、参数解析 |
| `tests/test_http_transport.py` | HTTP 层健壮性：**响应体错位回归**、chunked、截断、超长响应 |
| `tests/test_pt.py` | PT 协议：握手、多传输名、参数传递、缺失 url 的处理 |
| `tests/test_core.py` | SOCKS5 服务端/客户端、ACL、认证、网桥解析、配置与 TOML 解析器 |
| `tests/test_tunnel_protocol.py` | TSU/1 帧编解码、地址编码、错误码、目标策略、主机匹配、智能分流、RFC 6455 帧层 |
| `tests/test_tunnel_e2e.py` | **真起中继做端到端转发**：HTTP 往返、1 MiB 大数据、并发换链路、半关闭、双向背压（1 MiB 上限真刹得住且不丢数据）、未定义 opcode 回 RESET、白名单/私有地址拒绝、SOCKS5 over tunnel |
| `deploy/cloudflare/test/*.test.mjs` | Worker 形态的中继：编解码、opcode 表、目标策略、错误码分类、PING/PONG（`node --test`，不需要 workerd），共 54 个用例 |
| `deploy/deno/main_test.ts` | Deno 形态的中继：同一批断言 + 真实 `Deno.connect` 转发、空闲超时、保活失效（23 个用例） |
| `scripts/interop_relay.py` | **跨语言互通**：Python 客户端 ↔ 真实运行的 Worker / Deno 中继，握手 + 鉴权 + 真转发一次 HTTP 请求 + 策略一致性 |

CI（GitHub Actions）覆盖三平台 × 多 Python 版本、静态检查、全部单元测试、
CLI 级端到端冒烟、文档一致性校验，以及三类集成测试：

1. 用 mock meek 网桥 + 真实 tor 验证「tor 能通过纯 Python 插件完成 PT 握手」；
2. 跑 Worker 与 Deno 中继的测试；
3. 用 Python 客户端做**跨语言互通**验证（Python ↔ Deno 中继为必过项；
   Python ↔ 本地 workerd 的 Worker 因为要临时下载 wrangler，标记为尽力而为）。

## 安全与合规

* 本项目只做**流量转发**，不记录访问内容、不做中间人、不注入任何内容。
* 默认只监听 `127.0.0.1` 且默认免认证。**若要监听 `0.0.0.0`，请务必设置
  `username`/`password` 和 `allow_from`**，否则你的代理会对整个网络开放，成为严重的安全问题。
* **中继（`relay serve`）对外监听时必须设令牌。** 未设令牌且监听非回环地址时程序会明确警告，
  但不会阻止你——那等于给任何人一个开放代理。另外中继**默认一律拒绝连接私有地址**，
  避免被打穿成内网跳板（只有本机联调用的 `--allow-private` 能放开）。
* 日志里不会记录你访问的域名（只有 `CONNECT host:port` 的连接级信息），
  且已给 tor 开了 `SafeLogging`。中继侧默认同样只记录「流 id + 字节数」，
  **不记录目标域名**；只有显式 `-v` 运行时才逐条打印目标，便于排障。
* **Cloudflare 条款**：自助服务协议 2.2.1(j) 禁止把其服务当作 VPN/代理使用。
  `cf-relay` 因此默认只放行学习类站点白名单，属于「把风险控制到最低」而非「获得许可」；
  账号风险由使用者自负。详见 [docs/routes.md](docs/routes.md)。
* 请遵守所在地法律法规与 Tor 官方使用条款。Meek 网桥由 Tor 志愿者提供，
  滥用会导致网桥被封禁，从而影响真正需要它的人。
* 官方提供的网桥行、你部署的 Worker 地址、中继令牌都是**给你自己用的**，不要公开分享。
* 重申一遍开头的免责声明：**本项目仅供学习使用；作者不对任何使用后果负责；
  违法使用后果自负。**

## 许可证

[MIT](LICENSE) © 2026 TorSOCKS5 contributors

协议实现参考了 Tor 官方项目
[pluggable-transports/meek](https://git.torproject.org/pluggable-transports/meek.git)（BSD 3-Clause），
本仓库为独立实现，遵循同样的线协议以保证互操作。
