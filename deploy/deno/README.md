# Deno 形态的 TSU/1 隧道中继

本目录是 TSU/1 隧道协议（见 [`docs/tunnel-protocol.md`](../../docs/tunnel-protocol.md)）的 Deno 实现，
既能在本机用 `deno run` 跑，也能部署到 Deno Deploy，作为路由方式 3（`self-relay`）的中继。

```
你的应用 ──SOCKS5──▶ TorSOCKS5 ──WSS(tsu/1)──▶ 本中继 ──TCP──▶ 目标
```

* `main.ts` —— 中继主体：`Deno.serve` + `Deno.upgradeWebSocket` + `Deno.connect`，纯 Deno 内置 API，**无任何远程 import**。
* `main_test.ts` —— `deno test` 用例：帧/地址编解码、白名单、错误码、可注入 socket 的转发循环，以及真实 TCP echo 端到端。
* `deno.json` —— 只放格式化（行宽 100）、lint 与 `deno task`（`check` / `test` / `serve`），不引入任何依赖。
* 代码内所有纯逻辑（编解码、白名单、配置解析、转发循环）都是导出函数，便于被测试直接调用。

## 1. 本地运行

```sh
cd deploy/deno
deno run --allow-net --allow-env main.ts
# 或指定端口 / 只监听本机
TSU_HOST=127.0.0.1 TSU_PORT=9052 TSU_TOKEN=devtoken TSU_ALLOW_ALL=1 \
  deno run --allow-net --allow-env main.ts
```

自检：

```sh
curl -s http://127.0.0.1:9052/healthz
# {"ok":true,"proto":"tsu/1","allow_all":true,"max_streams":64}
```

跑测试（含真实 TCP echo 端到端）：

```sh
cd deploy/deno
deno check main.ts main_test.ts
deno test --allow-net --allow-env --allow-read
# 等价的 task 写法：deno task check / deno task test
```

## 2. 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TSU_PORT` | `PORT` 或 `9052` | 监听端口；Deno Deploy 上会被平台忽略 |
| `TSU_HOST` | `0.0.0.0` | 监听地址，本地调试建议 `127.0.0.1` |
| `TSU_PATH` | `/tsu` | WebSocket 路径 |
| `TSU_TOKEN` | 空 | 鉴权令牌，支持 `?token=` 与 `Authorization: Bearer`。**部署时必须设置**；为空则关闭鉴权并打印告警 |
| `TSU_ALLOW_ALL` | `0` | `1` = 关闭白名单。**同时**放开私网/回环限制（仅本地调试，启动时告警） |
| `TSU_ALLOW_PRIVATE` | `0` | 单独放开私网/回环目标，不影响白名单 |
| `TSU_ALLOW_HOSTS` | `github.com,githubusercontent.com` | 允许的目标主机后缀，逗号分隔；`github.com` 匹配其子域，`.x.com` 只匹配子域 |
| `TSU_ALLOW_PORTS` | `443,80,22,9418` | 允许的目标端口 |
| `TSU_MAX_STREAMS` | `64` | 单条 WS 连接的并发流上限，超限直接 `TOO_MANY_STREAMS`，不排队 |
| `TSU_CONNECT_TIMEOUT_MS` | `15000` | 出网 TCP 连接超时 |
| `TSU_STREAM_IDLE_MS` | `300000` | 单条流空闲超时，超时 `RESET` |
| `TSU_IDLE_PING_MS` | `30000` | 连接空闲多久发 `PING` |
| `TSU_MAX_MISSED_PONGS` | `2` | 连续多少次 `PING` 无 `PONG` 判定链路失效并断开 |
| `TSU_SWEEP_INTERVAL_MS` | `5000` | 巡检（空闲超时 / 保活）间隔 |

> `TSU_ALLOW_ALL=1` 会把白名单与私网限制一起放开，只适合本地调试。默认（白名单模式）下私网、
> 回环、链路本地目标一律回 `BLOCKED_TARGET`，与协议 §3.2 一致。

## 3. 部署到 Deno Deploy

> 现状（2026-09 查证）：旧的 **Deploy Classic（dash.deno.com）已于 2026-07-20 关停**，
> `deployctl` 正在退场；新平台入口是 <https://console.deno.com/>，命令行用 `deno deploy`
> 子命令（`deno 2.9+` 自带）。下面的步骤按新平台写。

### 3.1 用 CLI 部署（推荐，本仓库是单仓库多目录）

```sh
# 1) 登录（首次会弹浏览器授权，token 存在系统 keyring 里）
cd deploy/deno
deno deploy create \
  --org <你的组织> --app tsu-relay \
  --source local \
  --runtime-mode dynamic --entrypoint main.ts \
  --app-directory deploy/deno \
  --region global

# 2) 设置环境变量（令牌用 --secret，只写不读）
deno deploy env add TSU_TOKEN "<一串随机令牌>" --org <你的组织> --app tsu-relay --secret
deno deploy env add TSU_ALLOW_HOSTS "github.com,githubusercontent.com" --org <你的组织> --app tsu-relay
deno deploy env list --org <你的组织> --app tsu-relay

# 3) 部署到生产（不带 --prod 会走非生产 timeline）
deno deploy --org <你的组织> --app tsu-relay --prod

# 4) 看日志（只会有连接计数与错误类型，不会记录目标域名）
deno deploy logs --org <你的组织> --app tsu-relay
```

部署后先探活：

```sh
curl -s https://<app>.<org>.deno.net/healthz
# {"ok":true,"proto":"tsu/1","allow_all":false,"max_streams":64}
```

### 3.2 用 Dashboard 部署

1. 打开 <https://console.deno.com/>，创建 organization（`create org` 之后才能建 app）。
2. `+ New App` → 选 GitHub 仓库，或选 local 上传；`App Config` 里把 **entrypoint 设为 `main.ts`**、
   运行模式选 dynamic（服务端）。注意：**GitHub 集成暂不支持单仓库子目录**，本仓库请用 CLI 的
   `--app-directory deploy/deno`，或把 app directory 指向该子目录。
3. 在 app 的 `Environment Variables` 里把第 2 节的变量逐个加上，`TSU_TOKEN` 勾选 **secret**，
   上下文选 Production。
4. 点击部署，等 build 完成后访问 `/healthz` 验证。

> Deno Deploy 上 `TSU_PORT` / `TSU_HOST` 不生效——平台自己决定监听地址，
> 入口只要调用 `Deno.serve` 即可（本实现默认就是）。

### 3.3 绑定自有域名

1. 在 app 的 `Custom Domains` 里添加域名（例如 `relay.example.com`）。
2. 按提示先加 `_acme-challenge` 的 CNAME 记录，用于平台签发 TLS 证书。
3. 再加业务记录：把域名 CNAME/ANAME 指向 Deploy 给的默认域名。
4. DNS 生效最多等 48 小时再移除其他解析；证书就绪后用 `wss://relay.example.com/tsu?token=...` 连接。
5. 免费版最多 5 个自定义域名。

### 3.4 免费额度与限制（Free 计划，以官方定价页为准）

* 请求数 **1M/月**，出网流量 **20 GiB/月**（入站请求数据不计）。
* **Active CPU 10 小时/月**、内存计费 **150 GiB-hr/月**（默认 768 MB，空闲自动缩容）。
* 自定义域名 **5 个**、org 内 app **10 个**。
* 运行时是标准 Deno（当前平台为 Deno 2.5.0，`--allow-all`），`Deno.connect` / WebSocket 都能用；
  不能传自定义 flags（含 `--unstable-*`）。
* **Serverless 生命周期**：没有流量时实例会被停掉（5 秒 ~ 10 分钟不等）；**有数据来往的 WebSocket
  （含 ping/pong）会保活实例**，这也正是协议要求 30 秒 PING 的原因。平台也可能在热更新/缩容时
  驱逐实例（先发 `SIGINT`，5 秒后 `SIGKILL`），所以客户端必须能按指数退避重连（协议 §4.3）。
* 单条 WS 连接的并发流按本实现默认 **64**；若上游平台另有出站并发限制，会表现为 `CONNECT_FAILED`，
  需要把 `TSU_MAX_STREAMS` 调小。

## 4. 客户端怎么连

```text
ws(s)://<host>/tsu?token=<TSU_TOKEN>          # 或 Authorization: Bearer <TSU_TOKEN>
Sec-WebSocket-Protocol: tsu.v1                # 必须带，中继会回显同一个值
```

最小示例（Node 22+/Deno 均有全局 WebSocket，帧格式见协议第 2 节）：

```js
const ws = new WebSocket("wss://relay.example.com/tsu?token=...", ["tsu.v1"]);
ws.binaryType = "arraybuffer";
ws.onopen = () => {
  // OPEN：atyp=0x03 域名 + 端口
  const host = new TextEncoder().encode("example.com");
  const open = new Uint8Array([1, 0, 0, 0, 3, 3, host.length, ...host, 0x01, 0xbb]);
  ws.send(open);
};
```

Python 侧用仓库自带的 TSU 客户端（`torsocks5 tunnel ...`）时，把中继地址填成
`wss://relay.example.com/tsu?token=...` 即可；协议一致性以 `docs/tunnel-protocol.md` 为准。

## 5. 安全与合规提醒（务必阅读）

* **用别人的免费平台做中转，很可能违反该平台的服务条款**（Deno Deploy 的 ToS 也不欢迎把它当通用
  代理/隧道出口）。账号被封、流量被限、账单风险都由你自己承担。
* 本实现**仅供个人学习与自用**：不要公开分享中继地址与令牌，不要拿它做对外服务，不要用它规避
  所在地法律。要长期稳定使用，请自备 VPS 跑 `torsocks5 relay serve`（Python 参考实现）。
* 一定要设置 `TSU_TOKEN`（长随机串）并勾选 secret；令牌就是你这台中继的唯一门禁。
* 保持白名单模式（`TSU_ALLOW_ALL=0`），只放通你真正需要的域名与端口；私网/回环目标默认拒绝，
  不要为了图方便在生产上打开 `TSU_ALLOW_ALL`。
* 日志遵循项目「不记录访问内容」的承诺：只输出连接计数与错误类型，不打印目标域名与数据。

## 6. 与协议规范的偏差（有意为之）

| 偏差 | 原因 |
| --- | --- |
| `TSU_ALLOW_ALL=1` 会同时放开私网/回环限制（另有 `TSU_ALLOW_PRIVATE` 单独控制） | 规范 §3.2 要求默认拒绝私网，本实现保留该默认；但本地端到端验证（连 127.0.0.1 的 echo 服务）必须能放开，所以把它挂在显式的调试开关上，启动时打印告警 |
| 额外把 `localhost` / `*.localhost` / `::ffff:127.0.0.1` 这类回环等价形式判为 `BLOCKED_TARGET` | 规范只列了网段，这里按「回环不放行」的意图补齐等价写法 |
| WS → TCP 方向队列超过 1 MiB 时对该流 `RESET` | Deno 的 WebSocket API 无法暂停读取，无法像 TCP → WS 方向那样真正暂停；用有界队列 + `RESET` 兜住，避免无界缓冲（规范 §3.4 禁止无界缓冲） |
| 客户端未带 `Sec-WebSocket-Protocol` 时容忍（带了但值不含 `tsu.v1` 则 400） | 规范要求客户端必须带；中继侧对宽松客户端保持兼容，同时不允许回显一个客户端没申请过的子协议（否则浏览器/标准客户端会拒绝） |
| `OPEN` 用了已在用的 stream id → `OPEN_ERR BAD_REQUEST`；未定义 opcode / 未知流上的 `DATA` → `RESET` 该流 | 规范 §2.1 只规定「用 RESET 或直接关闭连接」，这里选择对已建立的流做精确 RESET，不牵连其他流 |
| 非升级请求返回 `426`，路径不匹配返回 `404` | 规范只定义了 401，这两个 HTTP 状态码属于补齐 |

## 7. 目录内其它命令备忘

```sh
deno check main.ts            # 类型检查（无远程依赖，CI 不需要联网）
deno test --allow-net         # 单元 + 端到端测试
deno run --allow-net --allow-env main.ts   # 本地起中继
deno task check/test/serve    # 上面三条的 task 别名（见 deno.json）
```
