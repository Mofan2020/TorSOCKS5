# TorSOCKS5 隧道协议 TSU/1（TorSOCKS5 Tunnel Protocol）

本文件是**唯一真相源**。Python 客户端、Python 中继、Cloudflare Worker、Deno 中继
四个实现都必须严格按这份规范编解码，任何一端改动都要先改这里。

```
你的应用 ──SOCKS5──▶ TorSOCKS5 ──WSS(本协议)──▶ 中继(CF Worker / Deno / 自建) ──TCP──▶ 目标
```

* 路由方式 2（`cf-relay`）：中继 = Cloudflare Worker
* 路由方式 3（`self-relay`）：中继 = `torsocks5 relay serve`（Python 参考实现）、Deno Deploy、或任何实现了本协议的服务

---

## 1. 载体

| 项 | 约定 |
| --- | --- |
| 传输层 | WebSocket（RFC 6455），二进制帧；生产环境必须 `wss://` |
| 握手 | `GET <path>?token=<token>` + `Upgrade: websocket` + `Sec-WebSocket-Protocol: tsu.v1` |
| 子协议 | 客户端发起时**必须**带 `Sec-WebSocket-Protocol: tsu.v1`；中继接受时**必须**回显同一个值 |
| 默认路径 | `/tsu` |
| 令牌 | 优先用查询参数 `?token=`；也**必须**接受 `Authorization: Bearer <token>`。令牌不匹配 → 握手阶段返回 `401`，不做 Upgrade |
| 备选路径 | `GET /healthz` 返回 `200` + JSON `{"ok":true,"proto":"tsu/1","allow_all":bool,"max_streams":int}`，用于连通性探测（不需要令牌） |

WS 消息边界即帧边界：**一个 WS 二进制消息 = 恰好一个 TSU 帧**，没有长度前缀。

* 发送方**必须**把单个 WS 消息控制在 **64 KiB 以内**（推荐 32 KiB 分片）。
* 接收方**必须**能接受最大 **1 MiB** 的消息，超过则视为协议错误并 `RESET` 该流。
* 文本帧一律忽略（不得当成 `DATA`）。

## 2. 帧格式

```
 0        1        2        3        4        5 ...
+--------+--------+--------+--------+--------+-----------------+
| opcode |         stream id (uint32, big endian)   |   payload   |
+--------+--------+--------+--------+--------+-----------------+
  1 byte                4 bytes                        N bytes
```

* `opcode`：1 字节，见下表。
* `stream id`：4 字节大端无符号整数。**客户端分配**，从 `3` 开始递增（`1`、`2` 留给协议保留/探测），
  单个连接内不得重复使用尚未关闭的 id。`0` 为控制帧保留（当前未使用）。
* `payload`：其余全部字节，可为空。

### 2.1 opcode 表

| 值 | 名称 | 方向 | payload | 含义 |
| --- | --- | --- | --- | --- |
| `0x01` | `OPEN` | C→S | 目标地址（见 2.2） | 请求建立到目标的 TCP 连接 |
| `0x02` | `OPEN_OK` | S→C | 空 | 连接建立成功，可以发 `DATA` |
| `0x03` | `OPEN_ERR` | S→C | `[code:1][utf8 消息]` | 连接失败，见 2.3 |
| `0x04` | `DATA` | 双向 | 原始字节 | 数据 |
| `0x05` | `CLOSE` | 双向 | 空 | 本方向不再发 `DATA`（半关闭）；接收方对 TCP 做 `shutdown(SHUT_WR)` |
| `0x06` | `RESET` | 双向 | 空 | 立即中止：关闭 TCP、释放 stream id，不再发任何该流帧 |
| `0x07` | `PING` | 双向 | ≤ 32 字节随机数据 | 保活探测 |
| `0x08` | `PONG` | 双向 | 原样回显 `PING` 的 payload | 保活应答 |

未定义的 opcode → 接受方**必须**用 `RESET`(该流) 或直接关闭连接（流 id 为 0 时）回应。

### 2.2 地址编码（`OPEN` 的 payload）

```
[atyp:1][addr][port:2 大端]
  atyp = 0x01  IPv4      addr = 4 字节
  atyp = 0x03  域名      addr = 1 字节长度 + ASCII 域名（不得含结尾点；≤ 255 字节）
  atyp = 0x04  IPv6      addr = 16 字节
```

与 SOCKS5 的 `ATYP` 编号保持一致，方便复用已有解析代码。
域名**必须**原样透传给中继解析（不在本地解析），避免 DNS 泄漏。

### 2.3 `OPEN_ERR` 错误码

| 码 | 名称 | 含义 | 客户端建议动作 |
| --- | --- | --- | --- |
| `0x01` | `NOT_ALLOWED` | 目标不在中继白名单 / 端口不允许 | 不再重试，直接向 SOCKS5 客户端回 `REP_NOT_ALLOWED` |
| `0x02` | `CONNECT_FAILED` | TCP 连接失败（DNS 失败、拒绝、超时） | 回 `REP_HOST_UNREACHABLE` |
| `0x03` | `TOO_MANY_STREAMS` | 本连接并发流已满（CF 免费版单请求上限 6） | **换一条 WS 连接重试**，不要把错误抛给上层 |
| `0x04` | `BAD_REQUEST` | 帧格式/地址编码非法 | 回 `REP_GENERAL_FAILURE` |
| `0x05` | `BLOCKED_TARGET` | 目标是私有网段 / 回环 / 中继自身 IP（平台禁止） | 回 `REP_NOT_ALLOWED` |
| `0x06` | `UNAUTHORIZED` | 令牌错误（个别平台只能在升级后校验） | 终止并提示检查 token |

## 3. 中继行为要求

1. **鉴权**：令牌不匹配必须拒绝。比较时长度不等即失败（不要提前返回造成时序泄漏；逐字节比较即可）。
2. **目标过滤**：
   * 必须拒绝对私网/回环/链路本地地址的连接请求（`0.0.0.0/8`、`10/8`、`127/8`、`169.254/16`、
     `172.16/12`、`192.168/16`、`::1`、`fc00::/7`、`fe80::/10`）→ `BLOCKED_TARGET`。
   * 白名单模式（`TSU_ALLOW_ALL != "1"`）下，域名/IP 必须命中允许列表才放行 → 否则 `NOT_ALLOWED`。
     域名匹配规则：**后缀匹配**，`github.com` 同时匹配 `github.com` 与 `api.github.com`；
     以 `.` 开头的条目（如 `.githubusercontent.com`）只匹配子域。比较前统一转小写并去掉结尾点。
   * 端口白名单默认 `443,80,22,9418`，可配置。
3. **并发上限**：单条 WS 连接上并发的 TCP 数量上限 `max_streams`：
   * Cloudflare Worker：**必须 ≤ 6**（平台硬限制，见 `docs/routes.md`）。
   * Python / Deno 中继：默认 64，可配置。
   * 超限时对超出部分回 `TOO_MANY_STREAMS`，**不得**排队等待。
4. **背压**：当待发送队列超过 1 MiB 时必须暂停从 TCP 读取（或暂停 WS 读取），
   排空后恢复。禁止无界缓冲。
5. **半关闭**：收到 `CLOSE` 时对 TCP 做 `shutdown(SHUT_WR)`；**不要**立刻关闭整条连接——
   对端可能还有数据要回来。收到 `RESET` 才立即关闭。
6. **保活**：空闲 30 秒以上应发 `PING`；连续 2 次 `PING` 无 `PONG` 可判定链路失效并关闭。
   收到 `PING` **必须**回 `PONG`（payload 原样回显）。WS 层的 ping/pong 控制帧可选，不作为协议依赖。
7. **空闲超时**：单条流空闲超过 `stream_idle_timeout`（默认 300 秒）→ `RESET`。
8. **日志**：**不得**记录请求内容或目标域名的全量列表；只允许输出连接计数与错误类型
   （与项目「不记录访问内容」的承诺一致）。

## 4. 客户端行为要求

1. **连接池**：维护 `links` 条 WS 连接（默认：`cf-relay` 4 条、`self-relay` 4 条）。
   每条连接最多承载 `max_streams` 条流（默认：`cf-relay` 6、`self-relay` 64）。
   新流分配给**当前负载最低且有额度**的连接；全部满载时才新建连接（不超过 `links` 上限）。
2. **收到 `TOO_MANY_STREAMS`**：必须换一条连接重试（最多 3 次），对上层不可见。
3. **断链**：该连接上的所有流一律 `RESET` 语义关闭（向 SOCKS5 客户端直接断开），
   并按指数退避（1→2→4→8→16→30 秒，上限 30 秒）重连。
4. **`OPEN` 超时**：默认 30 秒未收到 `OPEN_OK`/`OPEN_ERR` → 判定该流失败（`CONNECT_FAILED` 语义）。
   `TOO_MANY_STREAMS` 不在此列（应立即返回）。
5. **分片**：超过 32 KiB 的写入必须拆成多个 `DATA` 帧。
6. **半关闭**：本地读端 EOF → 发 `CLOSE`；收到 `CLOSE` 且本地已发过 `CLOSE` → 关闭流并释放 id。
7. **域前置（可选）**：允许配置 `front`：TLS 的 SNI 用未封锁的域名、HTTP `Host` 用中继域名。
   Cloudflare 目前对此行为会拦截（SNI ≠ Host），**必须在文档中说明这是「看运气」的选项**，
   不作为默认。

## 5. 各实现的额外约束

### Cloudflare Worker（路由 2）
* `connect()` 来自 `cloudflare:sockets`；无法连接 CF 自有 IP、私网、`localhost`、25 端口 → 映射为 `BLOCKED_TARGET`。
* 单请求并发出站连接上限 6 → `max_streams ≤ 6`。
* 免费版 CPU 时间 10 ms/请求、请求数 10 万/日 → 必须避免逐字节处理，直接用 `Uint8Array` 切片转发。
* 不允许入站 TCP 的 `CONNECT`，因此**只能**以 WebSocket 作为载体。
* 部署时必须设置 `TSU_TOKEN`；`TSU_ALLOW_ALL` 默认 `"0"`（白名单模式）。

### Deno 中继（路由 3 的免费平台形态）
* `Deno.serve` + `Deno.upgradeWebSocket` + `Deno.connect`。
* 默认 `max_streams = 64`。

### Python 中继（`torsocks5 relay serve`）
* 纯标准库实现（含 WebSocket 服务端），可在任意机器/自有 VPS 上跑。
* 支持 `--allow-all` 关闭白名单。

## 6. 一致性测试要求

每个实现都要有测试证明「帧编解码 + 端到端转发」真实可用，而不是只测辅助函数：

* Python：`tests/test_tunnel_protocol.py`（编解码）、`tests/test_tunnel_e2e.py`（真实 TCP echo 往返）。
* Worker：`deploy/cloudflare/test/*.test.mjs`（`node --test`），至少覆盖编解码、白名单、错误码。
* Deno：`deploy/deno/main_test.ts`（`deno test`），至少覆盖编解码 + 真实 `Deno.connect` 转发。
* 跨语言互操作：CI 里用 Python 客户端 `torsocks5 tunnel probe` 连**真实运行的** Deno 中继，
  以及用 `wrangler dev` 起来的 Worker（本地 workerd）。
