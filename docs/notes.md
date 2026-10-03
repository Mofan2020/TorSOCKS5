# 实施笔记（v1.1.0：三种流量路由方式）

本文件记录这次改造的**决策、取舍、没动的部分与原因**。写给下一个要改这份代码的人看。

---

## 这次加了什么

原来的 TorSOCKS5 只有一条出口路径（Tor + meek 网桥）。这次把它抽象成「路由方式」，
并新增两条：

| 路由 | 文件 | 说明 |
| --- | --- | --- |
| `tor-meek` | `torsocks5/routes/tor_meek.py` | 原有能力，原样封装，行为不变 |
| `cf-relay` | `torsocks5/routes/cf_relay.py` | Cloudflare Worker 中转（免费版） |
| `self-relay` | `torsocks5/routes/self_relay.py` | 自建 / 多平台中继 |
| `upstream` | `torsocks5/routes/upstream.py` | `--upstream` 的兼容路径（内部，不出现在文档主表） |

配套：`torsocks5/tunnel/`（TSU/1 隧道协议 + WebSocket + 多路复用客户端 + 中继）、
`torsocks5/split.py`（智能分流）、`torsocks5/hostrules.py`（主机匹配语义）、
`torsocks5/defaults.py`（共享默认值）、`docs/tunnel-protocol.md`（协议唯一真相源）、
`docs/routes.md`（路由对比与排障）。

---

## 关键决策与理由

### 1. Cloudflare 只能用 WebSocket 承载，这是平台逼出来的

免费版 Worker **不支持入站 TCP**（只能 `connect()` 出网），所以隧道载体只能是 WebSocket；
再加上「单请求最多 6 条并发出站连接」和「不能连 CF 自己的 IP 段」，形成了两个直接后果：

* 单链路并发上限 = 6，客户端必须靠**多开链路**扩容（`cf_relay.links`）；
* **任何走 Cloudflare 的站点都无法经由 cf-relay 中转**，会返回 `BLOCKED_TARGET`。
  这不是 bug，README 与 docs/routes.md 都写明了。

### 2. 条款风险用「默认白名单」而不是「拒绝实现」来兜

Cloudflare 自助服务协议 2.2.1(j) 禁止把其服务当 VPN/代理。项目的处理方式是：
默认只放行学习类站点（`torsocks5/defaults.py` 的 `LEARNING_ALLOW_HOSTS`），
默认开启 smart 分流，并在 README 顶部与文档里写明风险由使用者承担。
**没有**做「检测到 cf-relay 就拒绝启动」——使用者该有权知道自己承担什么。

### 3. `defaults.py` 是叶子模块，专门用来断环形导入

`config` / `tunnel.protocol` / `split` 都要用「学习站点白名单 + 允许端口」这两份常量。
若把常量放在 `config.py`，会让 `tunnel` 反向依赖 `config`，而 `config` 又要引用路由……
最后落到一个只依赖标准库的 `defaults.py`，谁都可以 import 它。

### 4. 分流默认开启，且**用户显式规则优先于内置名单**

内置名单只能猜个大概；使用者写 `direct_hosts = ["github.com"]` 时必须能覆盖内置判定。
`SplitRouter.decide()` 的顺序因此是：`direct_hosts` → `proxy_hosts` → 私有地址 → 模式内置名单。

### 5. 中继永远拒绝私有目标

`allow_all` 只关掉「目标白名单」，**不**关掉「私有地址拦截」。否则一台放在公网的中继
会立刻变成打穿内网的跳板。要本机测试私有目标得显式 `--allow-private`（CLI 里 `argparse.SUPPRESS`
隐藏，只在测试与冒烟脚本里用）。

### 6. 半关闭（SHUT_WR）如实传递

HTTP/1.0 这类协议「请求发完就关写方向、但仍等响应」很常见。`TunnelSocket.shutdown(SHUT_WR)`
只发 CLOSE、**不清接收缓冲**（早期版本清了，被 1MB 回环测试抓出来：只收到 557056/1048576 字节），
`SHUT_RD`/`SHUT_RDWR` 才丢弃已收数据。

### 7. 错误码映射到正确的 SOCKS5 回复码

中继回 `NOT_ALLOWED` → 客户端抛带 `rep_code=0x02` 的异常 → `SocksServer` 回
`REP_NOT_ALLOWED`（而不是笼统的 `REP_CONNECTION_REFUSED`）。为此 `SocksServer` 增加了
`connector` 钩子与 `getattr(exc, "rep_code", …)` 的映射，Tor 路径完全不受影响。

### 8. 协议文档是唯一真相源，四端一致

`docs/tunnel-protocol.md` 冻结了线格式，Python 客户端 / Python 中继 / Cloudflare Worker /
Deno 中继四个实现都照它写。CI 里 `scripts/check_docs.py` 会校验「协议里的错误码与帧类型
必须出现在文档中」，防止代码改了文档没跟上。

---

## 没动的与原因

* **`macapp` / DevKit 等上层项目**：不在本仓库里，本仓库只提供库与 CLI。若上层要用隧道，
  接 `SocksServer(connector=…)` 即可，无需改协议。
* **meek 传输实现**：一行没改。路由 1 只是把它包进 `routes/tor_meek.py`，参数、行为、
  日志格式都保持原样，`--upstream` / `--no-bridge` / `--bridge` 语义不变。
* **UDP ASSOCIATE 在隧道路由下直接拒绝**：TSU/1 只承载 TCP。没有做「UDP over WS」——
  那需要另一套协议与 MTU 处理，收益（DNS over UDP）不值得这次的复杂度。
  配置里 `udp_associate = true` 在隧道路由下会被自动关掉，启动横幅明确写「不支持」。
* **没有实现 Live2D / 数据库 / WebUI**：与「本地 SOCKS5 代理」无关。
* **没有默认启用任何网桥、Worker 地址或中继令牌**：这类东西一旦写进仓库就会被滥用。
* **没有做 UDP/QUIC 转发、没有做 TCP over CF 的 `connect()` 直连方案**：CF 免费版不支持入站 TCP，
  这条路走不通，不是「以后再说」。
* **`torsocks5 doctor` 未检查隧道路由**：隧道侧有自己的 `tunnel probe/check`，两者的检查项
  几乎不重叠（doctor 关心 tor/网桥/PT 插件路径）。为避免 doctor 变成「什么都查一点」的筐，
  保持分工：doctor 管路由 1，tunnel 管路由 2/3。

---

## 已知限制 / 后补项

1. **cf-relay 的实测数据取决于你的 Worker 与网络**。仓库里记录的是本地 `workerd` 验证
   与平台硬限制；真实的免费额度表现（CPU 10ms 掐连接）需要在真实 Worker 上跑
   `torsocks5 tunnel check --route cf-relay` 才能得到确切数字。
   （已实测的部分：Worker 在本地 workerd 上能对真实公网目标完成 OPEN→DATA→完整性校验，
   已由 `scripts/interop_relay.py` 用 Python 客户端复核——`connect()` 出网是通的。）
2. **中继没有限速与配额**。放在公网的中继若不设令牌，任何人都能用；项目只做警告不阻止。
   若将来要跑公开中继，需要加连接数配额与限速（当前 `--max-streams` 只限制单链路并发）。
3. **链路重连是「整体重连」**：一条 WS 链路断了，它上面正在跑的流会失败，客户端会重建链路，
   但**不会**自动重放那些流。对 HTTP 这类短连接影响很小，对长连接（SSH、长轮询）会断一次。
4. **没有粘性路由/负载统计面板**：链路按当前流数分配，没有做延迟感知。
5. **Windows 上的 `relay serve` 没做「服务化」集成**：`install-service` 目前只服务代理，
   要常驻中继得自己写计划任务（`install-service schtasks` 生成的是代理的命令行）。
6. **未在真实 Cloudflare 上部署验证**：Worker 形态的验证是在本地 `workerd`（`wrangler dev`）
   上做的——协议行为、鉴权、策略、PING/PONG 超时都实测过，但**免费额度的真实表现
   （CPU 10 ms 掐连接、每日 10 万请求）需要你自己部署到真实 Worker 上才知道**。
   本项目没有也不应该拿真实账号去压测（条款风险），这一点请读者知悉。
7. **保活与空闲超时的真实秒级常数未实等**：规范里的 30 秒 PING、300 秒流空闲、1 MiB 背压水位
   在自动化测试里是用压缩后的时间尺度验证的（同一套计时逻辑）。
   其中**背压的 1 MiB 水位有跨进程实测**（`test_backpressure_stalls_both_directions`：
   目标推 32 MiB、客户端故意不读，2 秒观察窗内推进 < 1 MiB，且随后读回来一字节不差）；
   30 秒 / 300 秒这类常数没有真等那么久。

---

## 怎么验证这次改动

```bash
# 1. 单元测试（190 个，全部离线、不需要网络与 tor）
python -m unittest discover -s tests -v

# 2. CLI 级端到端冒烟（起两个真进程，经 SOCKS5 取数据）
python scripts/e2e_smoke.py

# 3. 文档 ↔ 代码一致性（含负向测试，证明校验器真的会拦）
python scripts/check_docs.py && python scripts/check_docs.py --selftest

# 4. 跨语言互通测试（连接自行部署的 TSU/1 兼容中继）
# python scripts/interop_relay.py --start "your-relay-command" --port 8790 --token devtoken

# 6. 手动实测一条真实链路（需要网络）
python torsocks5_cli.py relay serve --allow-all &
python torsocks5_cli.py run --route self-relay --relay-url ws://127.0.0.1:9052/tsu
curl -x socks5h://127.0.0.1:9051 https://example.com/
```

CI 里的「尽力而为」项：`Python 客户端 ↔ 本地 workerd 的 Worker` 互通步骤需要 `npx` 临时
下载 wrangler，受网络影响大，因此带 `continue-on-error`（有网络时会真跑；本地已实测通过）。
Python ↔ Deno 中继的互通是必过项。
