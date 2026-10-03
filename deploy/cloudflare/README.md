# TSU/1 隧道中继 —— Cloudflare Worker 形态

把 `torsocks5` 的 TSU/1 隧道协议（`docs/tunnel-protocol.md`）跑在 Cloudflare Workers 上：
客户端用 WSS 连上来，Worker 用 `cloudflare:sockets` 的 `connect()` 替它出网。

```
你的应用 ──SOCKS5──▶ TorSOCKS5 ──WSS(TSU/1)──▶ 本 Worker ──TCP──▶ 目标
```

单文件、零构建、零 npm 依赖（只用 Worker 内置的 `cloudflare:sockets` 与标准 Web API）。
`test/framing.test.mjs` 用 `node --test` 直接跑，不需要 workerd。

---

## ⚠️ 先读这一段：Cloudflare 条款明确禁止这种用法

> **Cloudflare 自助服务条款 2.2.1(j) 明文禁止使用其服务提供 VPN 或类似的代理服务。**
> 用它做隧道中继属于**违反条款**的行为：账号（连同该账号下的域名、Worker、DNS）可能被
> **直接封禁**，且申诉通常无效。
>
> 本项目里的这个 Worker **仅供个人学习与协议实现研究**：
> * 不要公开分享部署出来的地址，不要做成公开的中转服务，不要给第三方使用；
> * 想稳定跑中继请用 `torsocks5 relay serve`（Python 参考实现）或 Deno Deploy 形态，
>   架在自己的机器/自购 VPS 上，那里没有这条限制；
> * 用不用、封不封号，风险由使用者自负。

另外，即使不谈条款，免费版单请求的出站连接数上限是 6、CPU 时间 10 ms、请求数 10 万/日，
吞吐和并发都远不如自建中继。

---

## 目录

- [快速开始](#快速开始)
- [环境变量](#环境变量)
- [绑自有域名](#绑自有域名)
- [连通性自检](#连通性自检)
- [免费版限制](#免费版限制)
- [本地开发与测试](#本地开发与测试)
- [实现要点](#实现要点)

---

## 快速开始

前置：Node.js ≥ 20，`npm i -g wrangler`（或直接用 `npx wrangler`）。
wrangler ≥ 4 的 `dev` 已经默认跑本地 workerd，不需要登录。

```bash
cd deploy/cloudflare

# 1. 登录（只有部署才需要，本地 dev 不需要）
wrangler login

# 2. 设置访问令牌（**必须**，不设就一律 401，fail-closed）
wrangler secret put TSU_TOKEN
#    回车后粘贴一个足够长的随机串，例如 `openssl rand -hex 32` 的输出

# 3. 按需改 wrangler.toml 里的 [vars]（白名单域名、端口、max_streams…）

# 4. 部署
wrangler deploy

# 5. 探活
curl -s https://torsocks5-tsu-relay.<你的子域>.workers.dev/healthz
# {"ok":true,"proto":"tsu/1","allow_all":false,"max_streams":6}
```

客户端侧把中继指向：

```
wss://torsocks5-tsu-relay.<你的子域>.workers.dev/tsu?token=<TSU_TOKEN>
```

> 令牌走查询参数或 `Authorization: Bearer <token>` 都可以；生产环境必须 `wss://`（明文 `ws://`
> 会把令牌和数据一起暴露）。

## 环境变量

在 `wrangler.toml` 的 `[vars]` 里改（密钥类只能用 `wrangler secret put`）。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TSU_TOKEN` | 无（空则全部 401） | 访问令牌。**必须**用 `wrangler secret put TSU_TOKEN` 设置，不要写进配置文件 |
| `TSU_ALLOW_ALL` | `"0"` | `"1"` 关闭域名白名单。私网/回环地址**始终**被拒（`BLOCKED_TARGET`） |
| `TSU_ALLOW_HOSTS` | `torproject.org,github.com,.githubusercontent.com` | 逗号分隔，**后缀匹配**：`github.com` 命中 `github.com` 与 `api.github.com`；以 `.` 开头（`.githubusercontent.com`）只命中子域 |
| `TSU_ALLOW_PORTS` | `443,80,22,9418` | 逗号分隔端口白名单，不受 `TSU_ALLOW_ALL` 影响 |
| `TSU_MAX_STREAMS` | `6` | 单条 WS 连接上的并发隧道数，代码里夹到 `[1, 6]`（CF 免费版硬限制，配再大也没用） |
| `TSU_PATH` | `/tsu` | WebSocket 升级路径 |
| `TSU_STREAM_IDLE_TIMEOUT` | `300` | 单条流空闲多少秒后 `RESET` |

改完 `[vars]` 要重新 `wrangler deploy` 才生效。

## 绑自有域名

Worker 默认落在 `*.workers.dev`。想用自己的域名：

1. 在 Cloudflare 面板把域名（或其子域，如 `relay.example.com`）接入本账号、处于 Active 状态；
2. 打开 Worker 的 **Settings → Domains & Routes → Add → Custom Domain**，填 `relay.example.com`；
   也可以在 `wrangler.toml` 里声明（`wrangler deploy` 会自动创建 DNS 记录）：

   ```toml
   routes = [
     { pattern = "relay.example.com", custom_domain = true }
   ]
   ```

3. 客户端用 `wss://relay.example.com/tsu?token=...`。

注意：**别用 `workers.dev` 域名公开分享**（见开头的条款警告）；自有域名被投诉时，
封的是这个账号下的域名解析，影响范围更大。

## 连通性自检

```bash
curl -s https://<中继域名>/healthz
# {"ok":true,"proto":"tsu/1","allow_all":false,"max_streams":6}
```

`/healthz` 不需要令牌，只暴露协议版本、是否关闭白名单、并发上限，不暴露任何目标信息。
令牌错误一律在握手阶段回 `401`，不会升级成 WebSocket。

## 免费版限制

| 限制 | 数值 | 本实现的对策 |
| --- | --- | --- |
| 单请求并发出站连接 | **6** | `max_streams` 强制 ≤ 6，超出立即回 `TOO_MANY_STREAMS`（客户端应换一条 WS 连接重试） |
| CPU 时间 | 10 ms / 请求 | 全程 `Uint8Array` 切片转发，不做逐字节处理 |
| 请求数 | 10 万 / 日 | 一条 WS 连接承载多条流，别给每条 TCP 开一条 WS |
| 出网目标 | 不能连 CF 自有 IP、私网、`localhost`、25 端口 | 本地先拦私网/回环（`BLOCKED_TARGET`），平台报错也映射成 `BLOCKED_TARGET` |
| 单条 WS 消息 | 发送 ≤ 64 KiB（推荐 32 KiB），接收 ≤ 1 MiB | 发送按 32 KiB 分片，接收超过 1 MiB 视为协议错误 |
| 入站 TCP `CONNECT` | 不支持 | 只能以 WebSocket 为唯一载体 |

## 本地开发与测试

```bash
cd deploy/cloudflare

# 单元测试：帧编解码 / 白名单 / 地址解析 / 错误码（不需要 workerd、不需要登录）
node --test test/

# 本地跑起来（workerd 本地模式），令牌和变量可以直接从命令行给
wrangler dev --var TSU_TOKEN:devtoken --var TSU_ALLOW_HOSTS:example.com,ftp.gnu.org
curl -s http://127.0.0.1:8787/healthz
```

本地模式下 workerd 的 `connect()` 会真的去连目标，所以「WS 握手 → OPEN → DATA 往返」
可以在本机完整验证（见文件末尾的验证记录）。**私有/回环地址仍然被本实现拒绝**，
所以别拿 `127.0.0.1` 当测试目标，用一个公网目标（例如 `example.com:80`）。

## 实现要点

* **帧格式**：`opcode(1) + stream id(4, 大端) + payload`，一个 WS 二进制消息 = 一个 TSU 帧；
  文本帧一律忽略。
* **opcode**：`OPEN/OPEN_OK/OPEN_ERR/DATA/CLOSE/RESET/PING/PONG`；
  未定义的 opcode 对**该流**回 `RESET`，流 id 为 0 时直接关连接。
* **地址编码**：`atyp` 与 SOCKS5 一致（`0x01` IPv4 / `0x03` 域名 / `0x04` IPv6），
  域名原样透传给平台解析，不做本地 DNS。
* **错误码**：`NOT_ALLOWED / CONNECT_FAILED / TOO_MANY_STREAMS / BAD_REQUEST /
  BLOCKED_TARGET / UNAUTHORIZED`，平台报错统一映射（私网、CF 自有 IP、
  `cannot connect to the specified address` → `BLOCKED_TARGET`；超并发 → `TOO_MANY_STREAMS`）。
* **半关闭**：收到 `CLOSE` 只对 TCP 做 `shutdown(SHUT_WR)`（`writer.close()`），把读半边留着；
  目标侧 EOF 时反向发 `CLOSE`，双方都关完才释放流；收到 `RESET` 才立刻拆掉。
* **背压**：写 TCP 用 `writer.write()` 的 Promise 做真实背压（每流一条串行写链，保证顺序）；
  TCP→WS 方向由「读前先检查待发送量」控制，超过 1 MiB 就暂停 `reader.read()`。
* **保活**：空闲 ≥ 30 秒发 `PING`，连续 2 次没有 `PONG` 就关连接；收到 `PING` 原样回 `PONG`。
* **日志**：只输出连接计数与错误类型，**不记录目标域名与访问内容**。

---

## 本地验证记录（2026-09-28，macOS + wrangler 4.37.1 + node v26.8.1）

```bash
$ node --test test/
ℹ tests 54
ℹ pass 54
ℹ fail 0

$ wrangler dev --port 8787 --var TSU_TOKEN:devtoken123 \
    --var TSU_ALLOW_PORTS:80,443 --var TSU_ALLOW_HOSTS:example.com,ftp.gnu.org
[wrangler:info] Ready on http://localhost:8787

$ curl -s http://127.0.0.1:8787/healthz
{"ok":true,"proto":"tsu/1","allow_all":false,"max_streams":6}

# 握手 + 子协议回显 + PING/PONG + OPEN + DATA + CLOSE（目标 example.com:80）
握手完成：subprotocol="tsu.v1"
收到 PONG payload=[1,2,3,4]（原样回显 一致）
收到 OPEN_OK stream=3
收到 DATA stream=3 869 字节，首行："HTTP/1.1 200 OK"
收到 CLOSE stream=3（目标侧 EOF，半关闭）

# 256 KiB Range 下载，响应体 sha256 与直连 curl 完全一致
收到 CLOSE：状态行="HTTP/1.1 206" 总收 262868 字节（响应体 262144 字节）
DATA 帧数=108 单帧最大=4096 字节（≤32 KiB 断言：true）
响应体 sha256=6c2b74f2…5da7   （curl 直连同 sha256）

# 白名单 / 私网 / 端口 / 并发
收到 OPEN_ERR stream=3 code=0x1 NOT_ALLOWED msg=目标不在白名单          （iana.org:80）
收到 OPEN_ERR stream=3 code=0x5 BLOCKED_TARGET msg=目标地址被平台禁止   （127.0.0.1:443 / localhost）
[concurrency] 汇总：OPEN_OK=6 OPEN_ERR=1 明细=9:TOO_MANY_STREAMS        （同时开 7 条流）
```
