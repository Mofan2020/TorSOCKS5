/**
 * deploy/deno/main_test.ts —— TSU/1 中继的单元测试与真实端到端测试。
 *
 * 运行：`cd deploy/deno && deno test --allow-net`
 * 端到端部分会真的起一个 TCP echo 服务端 + 真的用 WebSocket 客户端连中继，
 * 走完 OPEN → OPEN_OK → DATA → CLOSE 并逐字节比对。
 */

import {
  ATYP_DOMAIN,
  ATYP_V4,
  ATYP_V6,
  type ByteSource,
  DATA_CHUNK_BYTES,
  decodeAddress,
  decodeFrame,
  decodeOpenErr,
  encodeAddress,
  encodeFrame,
  encodeOpenErr,
  ERR_BAD_REQUEST,
  ERR_BLOCKED_TARGET,
  ERR_NOT_ALLOWED,
  ERR_TOO_MANY_STREAMS,
  extractToken,
  formatIPv6,
  type Frame,
  type FrameSink,
  hostMatches,
  isBlockedTarget,
  isHostAllowed,
  isKnownOpcode,
  isPortAllowed,
  OP_CLOSE,
  OP_DATA,
  OP_OPEN,
  OP_OPEN_ERR,
  OP_OPEN_OK,
  OP_PING,
  OP_PONG,
  OP_RESET,
  parseConfig,
  parseIPv4,
  parseIPv6,
  parsePortList,
  pickSubprotocol,
  PROTOCOL,
  pumpTcpToFrames,
  type RelayConfig,
  serveRelay,
  tokenMatches,
} from "./main.ts";

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

function bytes(...values: number[]): Uint8Array {
  return new Uint8Array(values);
}

function utf8(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

function assertEqualsBytes(actual: Uint8Array, expected: Uint8Array, message: string): void {
  const a = Array.from(actual);
  const e = Array.from(expected);
  if (a.length !== e.length || a.some((v, i) => v !== e[i])) {
    throw new Error(`${message}: 实际 ${a.length} 字节 / 期望 ${e.length} 字节`);
  }
}

function assert(cond: boolean, message: string): void {
  if (!cond) throw new Error(message);
}

function delayMs(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** 生成可复现的伪随机数据（不用真随机，方便失败时复现）。 */
function pseudoRandom(n: number, seed = 1): Uint8Array {
  const out = new Uint8Array(n);
  let x = seed >>> 0;
  for (let i = 0; i < n; i++) {
    x = (x * 1103515245 + 12345) & 0x7fffffff;
    out[i] = x & 0xff;
  }
  return out;
}

const decoder = new TextDecoder();

// ---------------------------------------------------------------------------
// 1. 帧编解码往返
// ---------------------------------------------------------------------------

Deno.test("帧编解码：opcode / 大端 stream id / payload 往返一致", () => {
  const payload = utf8("hello-tsu");
  const raw = encodeFrame(OP_DATA, 0x01020304, payload);
  assert(raw.byteLength === 5 + payload.byteLength, "帧长度应为 5 + payload");
  assert(
    raw[1] === 0x01 && raw[2] === 0x02 && raw[3] === 0x03 && raw[4] === 0x04,
    "stream id 必须大端",
  );

  const frame = decodeFrame(raw);
  assert(frame.opcode === OP_DATA, "opcode 应为 DATA");
  assert(
    frame.streamId === 0x01020304,
    `stream id 应为 0x01020304，实际 0x${frame.streamId.toString(16)}`,
  );
  assertEqualsBytes(frame.payload, payload, "payload 往返不一致");

  // 空 payload 的控制帧
  const ctrl = decodeFrame(encodeFrame(OP_CLOSE, 3));
  assert(
    ctrl.opcode === OP_CLOSE && ctrl.streamId === 3 && ctrl.payload.byteLength === 0,
    "CLOSE 帧应无 payload",
  );

  // 高位 stream id 不能被当成负数
  assert(
    decodeFrame(encodeFrame(OP_DATA, 0xffffffff)).streamId === 0xffffffff,
    "stream id 应按无符号解释",
  );
});

Deno.test("帧编解码：结构性非法报 BAD_REQUEST，未定义 opcode 留给 RESET 处理", () => {
  for (const raw of [bytes(), bytes(0x04, 0x00, 0x00, 0x00)]) {
    let code = -1;
    try {
      decodeFrame(raw);
    } catch (err) {
      code = (err as { code?: number }).code ?? -1;
    }
    assert(code === ERR_BAD_REQUEST, "长度非法的帧必须报 0x04 BAD_REQUEST");
  }
  // 未定义 opcode 不是结构错误：能解出 header，由 handler 按协议回 RESET
  const unknown = decodeFrame(bytes(0x7f, 0x00, 0x00, 0x00, 0x03));
  assert(
    !isKnownOpcode(unknown.opcode) && unknown.streamId === 3,
    "未定义 opcode 应解出 header 并标记为未知",
  );
  assert(isKnownOpcode(OP_OPEN) && isKnownOpcode(OP_PONG), "已定义 opcode 必须被识别");
});

// ---------------------------------------------------------------------------
// 2. 地址编解码往返（IPv4 / 域名 / IPv6）
// ---------------------------------------------------------------------------

Deno.test("地址编解码：IPv4 往返", () => {
  const raw = encodeAddress("1.2.3.4", 443);
  assertEqualsBytes(raw, bytes(ATYP_V4, 1, 2, 3, 4, 0x01, 0xbb), "IPv4 编码不符");
  const addr = decodeAddress(raw);
  assert(addr.host === "1.2.3.4" && addr.port === 443, "IPv4 解码不符");
});

Deno.test("地址编解码：域名往返（含 255 字节上限与空域名拒绝）", () => {
  const raw = encodeAddress("API.GitHub.com.", 9418);
  assertEqualsBytes(
    raw,
    new Uint8Array([ATYP_DOMAIN, 14, ...utf8("api.github.com"), 0x24, 0xca]),
    "域名编码不符（应转小写、去结尾点）",
  );
  const addr = decodeAddress(raw);
  assert(addr.host === "api.github.com" && addr.port === 9418, "域名解码不符");

  const long = encodeAddress(
    `${"a".repeat(63)}.${"b".repeat(63)}.${"c".repeat(63)}.${"d".repeat(60)}`,
    80,
  );
  assert(long[1] === 252, "长域名长度字节应为 252");

  let rejected = 0;
  try {
    decodeAddress(bytes(ATYP_DOMAIN, 0, 0x00, 0x50));
  } catch {
    rejected++;
  }
  try {
    decodeAddress(bytes(ATYP_DOMAIN, 5, ...utf8("abc"), 0x00, 0x50)); // 长度与内容不符
  } catch {
    rejected++;
  }
  assert(rejected === 2, "非法域名必须被拒绝");
});

Deno.test("地址编解码：IPv6 往返（含压缩与内嵌 IPv4）", () => {
  const raw = encodeAddress("2001:db8::1", 443);
  assertEqualsBytes(
    raw,
    new Uint8Array([
      ATYP_V6,
      0x20,
      0x01,
      0x0d,
      0xb8,
      0,
      0,
      0,
      0,
      0,
      0,
      0,
      0,
      0,
      0,
      0,
      1,
      0x01,
      0xbb,
    ]),
    "IPv6 编码不符",
  );
  const addr = decodeAddress(raw);
  assert(addr.host === "2001:db8::1" && addr.port === 443, `IPv6 解码不符: ${addr.host}`);

  // 字面量带方括号、内嵌 IPv4
  const mapped = decodeAddress(encodeAddress("[::ffff:192.0.2.128]", 80));
  assert(mapped.host === "::ffff:c000:280", `IPv4 映射地址解码不符: ${mapped.host}`);
  assert(parseIPv4("::ffff:127.0.0.1") === null, "parseIPv4 不应接受 IPv6");
  assertEqualsBytes(
    parseIPv6("::1")!,
    bytes(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1),
    "::1 解析不符",
  );
  assert(formatIPv6(parseIPv6("fe80::abcd:1")!) === "fe80::abcd:1", "IPv6 格式化不符");

  for (const bad of ["1:2:3:4:5:6:7", "1:2:3:4:5:6:7:8:9", "::1::2", "gggg::1", "300.1.1.1", ""]) {
    assert(parseIPv6(bad) === null, `应拒绝非法 IPv6: ${bad}`);
  }
});

Deno.test("地址编解码：非法端口与未知 atyp 报 BAD_REQUEST", () => {
  const codes: number[] = [];
  try {
    encodeAddress("example.com", 0);
  } catch (err) {
    codes.push((err as { code?: number }).code ?? -1);
  }
  try {
    decodeAddress(bytes(0x09, 1, 2, 3, 4, 0x00, 0x50));
  } catch (err) {
    codes.push((err as { code?: number }).code ?? -1);
  }
  assert(codes.every((c) => c === ERR_BAD_REQUEST), "非法地址必须报 0x04 BAD_REQUEST");
});

// ---------------------------------------------------------------------------
// 3. 错误码编码
// ---------------------------------------------------------------------------

Deno.test("错误码：OPEN_ERR payload 为 [code:1][utf8 消息]", () => {
  for (
    const [code, name] of [
      [ERR_NOT_ALLOWED, "host not allowed"],
      [ERR_BLOCKED_TARGET, "blocked target"],
      [ERR_TOO_MANY_STREAMS, "too many streams"],
    ] as const
  ) {
    const payload = encodeOpenErr(code, name);
    assert(payload[0] === code, "首字节必须是错误码");
    assertEqualsBytes(payload.subarray(1), utf8(name), "错误消息必须是 utf8");

    const parsed = decodeOpenErr(payload);
    assert(parsed.code === code && parsed.message === name, "错误码往返不一致");

    // 嵌在完整帧里也要能取出来
    const frame = decodeFrame(encodeFrame(OP_OPEN_ERR, 7, payload));
    const inner = decodeOpenErr(frame.payload);
    assert(inner.code === code && inner.message === name, "帧内 OPEN_ERR 解析不一致");
  }
  assert(decodeOpenErr(encodeOpenErr(ERR_TOO_MANY_STREAMS)).message === "", "消息可为空");
});

// ---------------------------------------------------------------------------
// 4. 白名单与端口匹配
// ---------------------------------------------------------------------------

Deno.test("白名单：后缀匹配、. 前缀只匹配子域、大小写与结尾点归一", () => {
  assert(hostMatches("github.com", "github.com"), "同域应匹配");
  assert(hostMatches("github.com", "api.github.com"), "子域应匹配");
  assert(hostMatches("GitHub.com.", "API.GITHUB.COM"), "大小写与结尾点应归一");
  assert(!hostMatches("github.com", "evilgithub.com"), "后缀必须按标签边界匹配");
  assert(!hostMatches("github.com", "github.com.evil.net"), "不得匹配拼接域名");
  assert(!hostMatches(".githubusercontent.com", "githubusercontent.com"), ". 前缀只匹配子域");
  assert(hostMatches(".githubusercontent.com", "raw.githubusercontent.com"), ". 前缀应匹配子域");

  const list = ["github.com", ".githubusercontent.com"];
  assert(isHostAllowed("api.github.com", list), "列表命中");
  assert(!isHostAllowed("example.com", list), "列表未命中");
});

Deno.test("白名单：端口列表解析与匹配", () => {
  const ports = parsePortList("443,80,22,9418");
  assert(
    ports.size === 4 && isPortAllowed(443, ports) && isPortAllowed(9418, ports),
    "默认端口解析有误",
  );
  assert(!isPortAllowed(25, ports), "25 端口不应放行");
  assert(parsePortList("443, , 70000, abc").size === 1, "非法端口项应被忽略");
  assert(parseConfig({}).ports.size === 4, "默认端口白名单应为 443,80,22,9418");
});

Deno.test("私网 / 回环 / 链路本地目标判定", () => {
  const blocked = [
    "0.0.0.0",
    "10.1.2.3",
    "127.0.0.1",
    "169.254.1.1",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.1.1",
    "::1",
    "::",
    "fc00::1",
    "fd12:3456::1",
    "fe80::1",
    "::ffff:127.0.0.1",
    "localhost",
    "foo.localhost",
  ];
  const allowed = [
    "1.1.1.1",
    "8.8.8.8",
    "172.32.0.1",
    "192.169.1.1",
    "2606:4700::1111",
    "example.com",
  ];
  for (const host of blocked) assert(isBlockedTarget(host), `${host} 应被判为禁用目标`);
  for (const host of allowed) assert(!isBlockedTarget(host), `${host} 不应被判为禁用目标`);
});

Deno.test("配置：白名单默认开启，allow_all 才放开", () => {
  const base = parseConfig({ TSU_TOKEN: "s3cret" });
  assert(!base.allowAll && !base.allowPrivate, "默认必须是白名单 + 禁止私网");
  assert(base.token === "s3cret" && base.maxStreams === 64, "默认 max_streams 应为 64");
  const open = parseConfig({ TSU_ALLOW_ALL: "1" });
  assert(open.allowAll && open.allowPrivate, "TSU_ALLOW_ALL=1 应放开白名单与私网限制");
  const custom = parseConfig({ TSU_MAX_STREAMS: "6", TSU_ALLOW_HOSTS: "a.com, .b.com" });
  assert(custom.maxStreams === 6, "TSU_MAX_STREAMS 应生效");
  assert(
    custom.hosts.length === 2 && isHostAllowed("x.b.com", custom.hosts),
    "TSU_ALLOW_HOSTS 应生效",
  );
});

Deno.test("鉴权：令牌提取与常量时间比较", () => {
  assert(tokenMatches("abc", "abc"), "相同令牌应通过");
  assert(!tokenMatches("abd", "abc"), "不同令牌应拒绝");
  assert(!tokenMatches("abcd", "abc"), "长度不同应拒绝");
  assert(!tokenMatches(null, "abc"), "缺令牌应拒绝");
  assert(tokenMatches(null, ""), "未配置令牌时鉴权关闭");

  const req = new Request("https://relay.example/tsu?token=abc", {
    headers: { upgrade: "websocket" },
  });
  assert(extractToken(req) === "abc", "应支持 ?token=");
  const bearer = new Request("https://relay.example/tsu", {
    headers: { upgrade: "websocket", authorization: "Bearer xyz" },
  });
  assert(extractToken(bearer) === "xyz", "应支持 Authorization: Bearer");
  assert(extractToken(new Request("https://relay.example/tsu")) === null, "无令牌应返回 null");

  assert(pickSubprotocol("tsu.v1") === PROTOCOL, "应回显 tsu.v1");
  assert(pickSubprotocol("foo, tsu.v1") === PROTOCOL, "多值里含 tsu.v1 应回显");
  assert(pickSubprotocol(null) === null, "客户端未声明子协议时容忍");
  assert(pickSubprotocol("foo") === undefined, "客户端声明了别的子协议应拒绝");
});

// ---------------------------------------------------------------------------
// 5. 可注入 socket 的 TCP 转发循环
// ---------------------------------------------------------------------------

class FakeSource implements ByteSource {
  private readonly chunks: Array<Uint8Array | null>;
  closeWriteCalls = 0;
  closed = false;

  constructor(chunks: Array<Uint8Array | null>) {
    this.chunks = chunks;
  }

  read(p: Uint8Array): Promise<number | null> {
    const chunk = this.chunks.shift();
    if (chunk === undefined) throw new Error("FakeSource 已耗尽");
    if (chunk === null) return Promise.resolve(null);
    // 按调用方给的缓冲区大小切分，多余的留在队列头部（模拟 TCP 分包）
    const take = Math.min(chunk.byteLength, p.byteLength);
    p.set(chunk.subarray(0, take));
    if (take < chunk.byteLength) this.chunks.unshift(chunk.subarray(take));
    return Promise.resolve(take);
  }

  closeWrite(): void {
    this.closeWriteCalls++;
  }

  close(): void {
    this.closed = true;
  }
}

class FakeSink implements FrameSink {
  readonly sent: Frame[] = [];
  drainWaits = 0;

  send(opcode: number, streamId: number, payload: Uint8Array = new Uint8Array(0)): void {
    this.sent.push({ opcode, streamId, payload: payload.slice() });
  }

  waitDrain(): Promise<void> {
    this.drainWaits++;
    return Promise.resolve();
  }
}

Deno.test("转发循环：大块数据按 ≤32KiB 分片，EOF 触发 onEof", async () => {
  const source = new FakeSource([pseudoRandom(80 * 1024, 7), null]);
  const sink = new FakeSink();
  let eof = false;
  let error: unknown = null;
  let received = 0;

  await pumpTcpToFrames(source, sink, 3, {
    chunkBytes: DATA_CHUNK_BYTES,
    onEof: () => {
      eof = true;
    },
    onError: (err) => {
      error = err;
    },
    onData: (n) => {
      received += n;
    },
  });

  assert(error === null, "不应报错");
  assert(eof, "应触发 onEof");
  assert(received === 80 * 1024, "onData 应统计原始字节");
  assert(sink.sent.length === 3, `80 KiB 应拆成 3 个 DATA 帧，实际 ${sink.sent.length}`);
  for (const frame of sink.sent) {
    assert(frame.opcode === OP_DATA && frame.streamId === 3, "必须是该流的 DATA 帧");
    assert(frame.payload.byteLength <= DATA_CHUNK_BYTES, "单帧不得超过 32 KiB");
  }
  const total = sink.sent.reduce((n, f) => n + f.payload.byteLength, 0);
  assert(total === 80 * 1024, "分片后总字节必须不变");
  assertEqualsBytes(
    sink.sent[1].payload,
    pseudoRandom(80 * 1024, 7).subarray(32 * 1024, 64 * 1024),
    "第二帧内容不符（必须是原始流的第 2 段）",
  );
  assert(sink.drainWaits >= 3, "每次读 TCP 前都应检查背压水位");
});

Deno.test("转发循环：读错误走 onError，取消后静默停止", async () => {
  const boom: ByteSource = {
    read: () => Promise.reject(new Error("tcp broken")),
    close: () => {},
  };
  const sink = new FakeSink();
  let error: unknown = null;
  await pumpTcpToFrames(boom, sink, 5, {
    chunkBytes: DATA_CHUNK_BYTES,
    onEof: () => {
      throw new Error("不该 EOF");
    },
    onError: (err) => {
      error = err;
    },
  });
  assert(error instanceof Error, "读错误应交给 onError");

  const cancelSource = new FakeSource([pseudoRandom(1024, 9)]);
  let onErrorCalled = false;
  await pumpTcpToFrames(cancelSource, new FakeSink(), 6, {
    chunkBytes: DATA_CHUNK_BYTES,
    onEof: () => {},
    onError: () => {
      onErrorCalled = true;
    },
    isCancelled: () => true,
  });
  assert(!onErrorCalled, "已取消时不应上报错误");
});

// ---------------------------------------------------------------------------
// 6. 真实端到端：临时 TCP echo 服务端 + 真实 WebSocket 客户端
// ---------------------------------------------------------------------------

interface EchoServer {
  port: number;
  eofSeen: boolean;
  received: Uint8Array;
  stop(): void;
}

/** 起一个真实的 TCP echo 服务端；读到 EOF 后关闭连接（用于验证半关闭）。 */
function startEchoServer(): EchoServer {
  const listener = Deno.listen({ hostname: "127.0.0.1", port: 0 });
  const state: EchoServer = {
    port: (listener.addr as Deno.NetAddr).port,
    eofSeen: false,
    received: new Uint8Array(0),
    stop: () => {
      try {
        listener.close();
      } catch {
        /* 已关闭 */
      }
    },
  };

  void (async () => {
    for await (const conn of listener) {
      void (async () => {
        const buf = new Uint8Array(16 * 1024);
        const parts: Uint8Array[] = [];
        try {
          while (true) {
            const n = await conn.read(buf);
            if (n === null) {
              state.eofSeen = true; // 对端 CLOSE 的 closeWrite 只有真的下发才会到这里
              break;
            }
            const chunk = buf.slice(0, n);
            parts.push(chunk);
            let off = 0;
            while (off < chunk.byteLength) off += await conn.write(chunk.subarray(off));
          }
        } catch {
          /* 连接被重置 */
        }
        const total = parts.reduce((n, p) => n + p.byteLength, 0);
        const all = new Uint8Array(total);
        let off = 0;
        for (const p of parts) {
          all.set(p, off);
          off += p.byteLength;
        }
        state.received = all;
        try {
          conn.close();
        } catch {
          /* 已关闭 */
        }
      })();
    }
  })().catch(() => {});

  return state;
}

interface RelayHandle {
  port: number;
  server: Deno.HttpServer;
  config: RelayConfig;
  stop(): Promise<void>;
}

/** 在 127.0.0.1 的随机端口上起一个真实中继。 */
async function startRelay(
  overrides: Partial<RelayConfig> = {},
  env: Record<string, string> = {},
): Promise<RelayHandle> {
  const config: RelayConfig = {
    ...parseConfig({ TSU_TOKEN: "test-token", ...env }),
    hostname: "127.0.0.1",
    port: 0,
    ...overrides,
  };
  const { server, listening } = serveRelay(config);
  const addr = await listening;
  return {
    port: addr.port,
    server,
    config,
    stop: async () => {
      await server.shutdown();
    },
  };
}

/** 测试用 WS 客户端：收集帧并支持按条件等待。 */
class TestClient {
  readonly socket: WebSocket;
  readonly frames: Frame[] = [];
  pings = 0;
  private readonly waiters: Array<{
    pred: (f: Frame) => boolean;
    resolve: (f: Frame) => void;
    reject: (e: unknown) => void;
  }> = [];
  private failure: unknown = null;
  private autoPong = false;

  private constructor(socket: WebSocket) {
    this.socket = socket;
    socket.binaryType = "arraybuffer";
    socket.onmessage = (ev: MessageEvent) => {
      const data = ev.data;
      let raw: Uint8Array;
      if (typeof data === "string") return;
      if (data instanceof ArrayBuffer) raw = new Uint8Array(data);
      else raw = new Uint8Array(data as ArrayBuffer);
      const frame = decodeFrame(raw);
      if (frame.opcode === OP_PING) {
        this.pings++;
        if (this.autoPong) this.socket.send(encodeFrame(OP_PONG, frame.streamId, frame.payload));
      }
      this.frames.push(frame);
      for (let i = this.waiters.length - 1; i >= 0; i--) {
        if (this.waiters[i].pred(frame)) {
          const w = this.waiters.splice(i, 1)[0];
          w.resolve(frame);
        }
      }
    };
    socket.onerror = () => {
      this.failure = new Error("WebSocket 出错");
      for (const w of this.waiters.splice(0)) w.reject(this.failure);
    };
  }

  static async connect(
    relay: RelayHandle,
    token = "test-token",
    protocols?: string[],
  ): Promise<TestClient> {
    const url = `ws://127.0.0.1:${relay.port}${relay.config.path}?token=${
      encodeURIComponent(token)
    }`;
    const socket = protocols ? new WebSocket(url, protocols) : new WebSocket(url);
    const client = new TestClient(socket);
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("WebSocket 握手超时")), 5000);
      socket.onopen = () => {
        clearTimeout(timer);
        resolve();
      };
      socket.onerror = () => {
        clearTimeout(timer);
        reject(new Error("WebSocket 握手失败"));
      };
    });
    return client;
  }

  /** 打开自动回 PONG，用于验证保活逻辑下连接能长期存活。 */
  enableAutoPong(): void {
    this.autoPong = true;
  }

  waitFor(pred: (f: Frame) => boolean, timeoutMs = 5000): Promise<Frame> {
    const existing = this.frames.find(pred);
    if (existing) return Promise.resolve(existing);
    if (this.failure) return Promise.reject(this.failure);
    return new Promise<Frame>((resolve, reject) => {
      const waiter = { pred, resolve, reject };
      this.waiters.push(waiter);
      setTimeout(() => {
        const idx = this.waiters.indexOf(waiter);
        if (idx >= 0) this.waiters.splice(idx, 1);
        reject(new Error(`等待帧超时（已收到 ${this.frames.length} 帧）`));
      }, timeoutMs);
    });
  }

  waitForOpcode(opcode: number, streamId?: number, timeoutMs = 5000): Promise<Frame> {
    return this.waitFor(
      (f) => f.opcode === opcode && (streamId === undefined || f.streamId === streamId),
      timeoutMs,
    );
  }

  /** 按下标顺序取第 index 个帧（轮询等待到达），用于严格按序比对回程数据。 */
  async frameAt(index: number, timeoutMs = 5000): Promise<Frame> {
    const deadline = Date.now() + timeoutMs;
    while (this.frames.length <= index) {
      if (this.failure) throw this.failure;
      if (Date.now() > deadline) {
        throw new Error(`等待第 ${index} 帧超时（已收到 ${this.frames.length} 帧）`);
      }
      await new Promise((r) => setTimeout(r, 5));
    }
    return this.frames[index];
  }

  /** 从 from 下标开始，按顺序收集 dataBytes 字节的 DATA 帧。 */
  async collectData(
    streamId: number,
    from: number,
    dataBytes: number,
    timeoutMs = 10000,
  ): Promise<Uint8Array> {
    const out = new Uint8Array(dataBytes);
    let written = 0;
    let index = from;
    while (written < dataBytes) {
      const frame = await this.frameAt(index++, timeoutMs);
      assert(
        frame.opcode === OP_DATA && frame.streamId === streamId,
        `第 ${index - 1} 帧应为流 ${streamId} 的 DATA，实际 opcode=0x${
          frame.opcode.toString(16)
        } stream=${frame.streamId} len=${frame.payload.byteLength}`,
      );
      assert(frame.payload.byteLength <= DATA_CHUNK_BYTES, "回程单帧不得超过 32 KiB");
      out.set(frame.payload, written);
      written += frame.payload.byteLength;
    }
    assert(written === dataBytes, `回程字节数应为 ${dataBytes}，实际 ${written}`);
    return out;
  }

  open(streamId: number, host: string, port: number): void {
    this.socket.send(encodeFrame(OP_OPEN, streamId, encodeAddress(host, port)));
  }

  data(streamId: number, payload: Uint8Array): void {
    for (let off = 0; off < payload.byteLength; off += DATA_CHUNK_BYTES) {
      this.socket.send(
        encodeFrame(
          OP_DATA,
          streamId,
          payload.subarray(off, Math.min(off + DATA_CHUNK_BYTES, payload.byteLength)),
        ),
      );
    }
  }

  frame(opcode: number, streamId = 0, payload: Uint8Array = new Uint8Array(0)): void {
    this.socket.send(encodeFrame(opcode, streamId, payload));
  }

  close(): void {
    try {
      this.socket.close();
    } catch {
      /* 已关闭 */
    }
  }
}

/** 读原始 HTTP 响应首行，用于验证握手阶段的 401/400/426。 */
async function rawHttpGet(
  port: number,
  path: string,
  headers: Record<string, string> = {},
): Promise<string> {
  const conn = await Deno.connect({ hostname: "127.0.0.1", port });
  try {
    const lines = [`GET ${path} HTTP/1.1`, "Host: 127.0.0.1", "Connection: close"];
    for (const [k, v] of Object.entries(headers)) lines.push(`${k}: ${v}`);
    await conn.write(new TextEncoder().encode(`${lines.join("\r\n")}\r\n\r\n`));
    // 一直读到响应头结束（101 的响应可能被拆成多个 TCP 段）
    const buf = new Uint8Array(4096);
    let text = "";
    const deadline = Date.now() + 5000;
    while (!text.includes("\r\n\r\n")) {
      if (Date.now() > deadline) break;
      let n: number | null;
      try {
        n = await conn.read(buf);
      } catch {
        break;
      }
      if (n === null) break;
      text += decoder.decode(buf.subarray(0, n));
    }
    return text;
  } finally {
    try {
      conn.close();
    } catch {
      /* 已关闭 */
    }
  }
}

Deno.test("端到端：真实 TCP echo 往返 + 半关闭 CLOSE", async () => {
  const echo = startEchoServer();
  const relay = await startRelay({ allowAll: true, allowPrivate: true });
  const client = await TestClient.connect(relay, "test-token", [PROTOCOL]);
  try {
    assert(
      client.socket.protocol === PROTOCOL,
      `子协议应回显 tsu.v1，实际 ${client.socket.protocol}`,
    );

    // OPEN → OPEN_OK
    client.open(3, "127.0.0.1", echo.port);
    const ok = await client.waitForOpcode(OP_OPEN_OK, 3);
    assert(ok.payload.byteLength === 0, "OPEN_OK 必须无 payload");

    // 小数据往返
    const first = utf8("hello tsu/1");
    let cursor = client.frames.length;
    client.data(3, first);
    assertEqualsBytes(
      await client.collectData(3, cursor, first.byteLength),
      first,
      "第一次回显不一致",
    );

    // 大数据往返（80 KiB → 验证分片与聚合）
    const big = pseudoRandom(80 * 1024, 42);
    cursor = client.frames.length;
    client.data(3, big);
    const merged = await client.collectData(3, cursor, big.byteLength);
    assertEqualsBytes(merged, big, "80 KiB 回程字节必须逐字节一致");

    // 半关闭：客户端 CLOSE → 中继对 TCP 做 closeWrite → echo 端看到 EOF → 中继回来 CLOSE
    client.frame(OP_CLOSE, 3);
    const close = await client.waitForOpcode(OP_CLOSE, 3, 8000);
    assert(close.payload.byteLength === 0, "CLOSE 必须无 payload");
    assert(echo.eofSeen, "echo 端必须真实收到 EOF（半关闭生效）");
    const expectedAtEcho = new Uint8Array(first.byteLength + big.byteLength);
    expectedAtEcho.set(first, 0);
    expectedAtEcho.set(big, first.byteLength);
    assertEqualsBytes(echo.received, expectedAtEcho, "echo 端收到的字节应与客户端发出的完全一致");

    // 流已释放：再用同一个 id 可以重新 OPEN
    const reuseCursor = client.frames.length;
    client.open(3, "127.0.0.1", echo.port);
    const reopened = await client.frameAt(reuseCursor, 8000);
    assert(
      reopened.opcode === OP_OPEN_OK && reopened.streamId === 3,
      `stream id 应被释放后重用，实际 opcode=0x${reopened.opcode.toString(16)}`,
    );
  } finally {
    client.close();
    await relay.stop();
    echo.stop();
  }
});

Deno.test("端到端：PING → PONG 原样回显", async () => {
  const relay = await startRelay({ allowAll: true, allowPrivate: true });
  const client = await TestClient.connect(relay);
  try {
    const payload = pseudoRandom(16, 3);
    client.frame(OP_PING, 0, payload);
    const pong = await client.waitForOpcode(OP_PONG);
    assertEqualsBytes(pong.payload, payload, "PONG 必须原样回显 PING 的 payload");
  } finally {
    client.close();
    await relay.stop();
  }
});

Deno.test("端到端：白名单拒绝 → NOT_ALLOWED（主机与端口各一例）", async () => {
  const echo = startEchoServer();
  const relay = await startRelay({ allowAll: false, allowPrivate: true, hosts: ["example.com"] });
  const client = await TestClient.connect(relay);
  try {
    client.open(3, "127.0.0.1", echo.port);
    const err = await client.waitForOpcode(OP_OPEN_ERR, 3);
    assert(decodeOpenErr(err.payload).code === ERR_NOT_ALLOWED, "主机不在白名单应回 0x01");
  } finally {
    client.close();
    await relay.stop();
    echo.stop();
  }

  // 主机放行但端口不在白名单
  const relay2 = await startRelay({
    allowAll: false,
    allowPrivate: true,
    hosts: ["127.0.0.1"],
    ports: new Set([443]),
  });
  const client2 = await TestClient.connect(relay2);
  try {
    client2.open(3, "127.0.0.1", echo.port);
    const err = await client2.waitForOpcode(OP_OPEN_ERR, 3);
    assert(decodeOpenErr(err.payload).code === ERR_NOT_ALLOWED, "端口不在白名单应回 0x01");
  } finally {
    client2.close();
    await relay2.stop();
  }
});

Deno.test("端到端：私网目标 → BLOCKED_TARGET", async () => {
  const echo = startEchoServer();
  // allow_all 打开但私网限制保留（生产默认姿势）
  const relay = await startRelay({ allowAll: true, allowPrivate: false });
  const client = await TestClient.connect(relay);
  try {
    client.open(3, "127.0.0.1", echo.port);
    const err = await client.waitForOpcode(OP_OPEN_ERR, 3);
    assert(decodeOpenErr(err.payload).code === ERR_BLOCKED_TARGET, "私网目标必须回 0x05");
  } finally {
    client.close();
    await relay.stop();
    echo.stop();
  }
});

Deno.test("端到端：并发上限 → TOO_MANY_STREAMS，且不排队", async () => {
  const echo = startEchoServer();
  const relay = await startRelay({ allowAll: true, allowPrivate: true, maxStreams: 1 });
  const client = await TestClient.connect(relay);
  try {
    client.open(3, "127.0.0.1", echo.port);
    await client.waitForOpcode(OP_OPEN_OK, 3);
    client.open(4, "127.0.0.1", echo.port);
    const err = await client.waitForOpcode(OP_OPEN_ERR, 4);
    assert(decodeOpenErr(err.payload).code === ERR_TOO_MANY_STREAMS, "超限应回 0x03");
    // 第一个流不受影响
    client.data(3, utf8("still-alive"));
    const echoed = await client.waitFor((f) => f.opcode === OP_DATA && f.streamId === 3);
    assertEqualsBytes(echoed.payload, utf8("still-alive"), "既有流应继续工作");
  } finally {
    client.close();
    await relay.stop();
    echo.stop();
  }
});

Deno.test("端到端：非法 OPEN 与未知流上的 DATA 都被拒绝", async () => {
  const relay = await startRelay({ allowAll: true, allowPrivate: true });
  const client = await TestClient.connect(relay);
  try {
    // atyp 非法
    client.frame(OP_OPEN, 3, bytes(0x09, 1, 2, 3, 4, 0x00, 0x50));
    const err = await client.waitForOpcode(OP_OPEN_ERR, 3);
    assert(decodeOpenErr(err.payload).code === ERR_BAD_REQUEST, "非法 OPEN 应回 0x04");

    // stream id 0 上的 OPEN
    client.frame(OP_OPEN, 0, encodeAddress("example.com", 443));
    const err0 = await client.waitForOpcode(OP_OPEN_ERR, 0);
    assert(decodeOpenErr(err0.payload).code === ERR_BAD_REQUEST, "stream id 0 上的 OPEN 应回 0x04");

    // 未知流的 DATA → RESET
    client.frame(OP_DATA, 9, utf8("nope"));
    const reset = await client.waitForOpcode(OP_RESET, 9);
    assert(reset.payload.byteLength === 0, "RESET 必须无 payload");

    // 未定义 opcode → RESET 该流
    client.frame(0x7f, 11);
    await client.waitForOpcode(OP_RESET, 11);

    // 文本帧必须被忽略（不能当成 DATA）
    client.socket.send("this is not a TSU frame");
    client.frame(OP_PING, 0, utf8("ping"));
    const pong = await client.waitForOpcode(OP_PONG);
    assertEqualsBytes(pong.payload, utf8("ping"), "文本帧被忽略后连接仍须正常");
  } finally {
    client.close();
    await relay.stop();
  }
});

Deno.test("端到端：连接失败（目标拒绝）→ CONNECT_FAILED", async () => {
  // 取一个刚刚关闭的端口，连接必然失败
  const tmp = Deno.listen({ hostname: "127.0.0.1", port: 0 });
  const deadPort = (tmp.addr as Deno.NetAddr).port;
  tmp.close();

  const relay = await startRelay({ allowAll: true, allowPrivate: true });
  const client = await TestClient.connect(relay);
  try {
    client.open(3, "127.0.0.1", deadPort);
    const err = await client.waitForOpcode(OP_OPEN_ERR, 3, 8000);
    assert(decodeOpenErr(err.payload).code === 0x02, "连接失败应回 0x02 CONNECT_FAILED");
  } finally {
    client.close();
    await relay.stop();
  }
});

Deno.test("端到端：流空闲超时 → RESET；连接空闲 → PING 保活，连续无 PONG 则断开", async () => {
  const echo = startEchoServer();
  // 把巡检间隔/空闲超时/保活周期压到毫秒级，验证计时逻辑本身
  const relay = await startRelay({
    allowAll: true,
    allowPrivate: true,
    sweepIntervalMs: 20,
    streamIdleTimeoutMs: 250,
    idlePingMs: 80,
    maxMissedPongs: 2,
  });
  try {
    // 1) 开一条流后什么都不做 → 空闲超时应 RESET 该流
    const idle = await TestClient.connect(relay);
    idle.enableAutoPong(); // 只测流空闲，不让连接被保活判定断开
    await idle.open(3, "127.0.0.1", echo.port);
    const reset = await idle.waitForOpcode(OP_RESET, 3, 4000);
    assert(reset.payload.byteLength === 0, "空闲超时的 RESET 应无 payload");
    assert(idle.socket.readyState === WebSocket.OPEN, "空闲超时只该 RESET 流，不该断开连接");
    idle.close();

    // 2) 连接空闲 → 中继发 PING（payload ≤ 32 字节）；客户端回 PONG 后连接保持
    const keepalive = await TestClient.connect(relay);
    keepalive.enableAutoPong();
    await keepalive.waitForOpcode(OP_PING, undefined, 4000);
    const pingsBefore = keepalive.pings;
    assert(
      keepalive.frames.find((f) => f.opcode === OP_PING)!.payload.byteLength <= 32,
      "PING payload 不得超过 32 字节",
    );
    await delayMs(400);
    assert(keepalive.pings > pingsBefore, "空闲期间中继应持续发 PING");
    assert(keepalive.socket.readyState === WebSocket.OPEN, "回了 PONG 的连接必须保持");
    keepalive.close();

    // 3) 完全不回 PONG → 连续 2 次 PING 后中继关闭连接
    const quiet = await TestClient.connect(relay);
    const closed = await new Promise<boolean>((resolve) => {
      const timer = setTimeout(() => resolve(false), 5000);
      quiet.socket.addEventListener("close", () => {
        clearTimeout(timer);
        resolve(true);
      });
    });
    assert(closed, "连续 2 次 PING 无 PONG 后中继必须断开连接");
    assert(quiet.pings >= 2, `断开前应至少发过 2 次 PING，实际 ${quiet.pings}`);
  } finally {
    await relay.stop();
    echo.stop();
  }
});

Deno.test("HTTP 层：/healthz、鉴权 401、缺 Upgrade 426、子协议 400", async () => {
  const relay = await startRelay({ maxStreams: 6, allowAll: false });
  try {
    // /healthz 不需要令牌
    const health = await fetch(`http://127.0.0.1:${relay.port}/healthz`);
    assert(health.status === 200, "healthz 应返回 200");
    const body = await health.json();
    assert(body.ok === true && body.proto === "tsu/1", "healthz 字段不符");
    assert(body.allow_all === false && body.max_streams === 6, "healthz 应反映配置");
    assert(
      health.headers.get("content-type")?.includes("application/json") === true,
      "healthz 应为 JSON",
    );

    // 未知路径 404
    assert((await fetch(`http://127.0.0.1:${relay.port}/nope`)).status === 404, "未知路径应 404");

    // 令牌错误 / 缺失 → 401，且不做 Upgrade
    const noToken = await rawHttpGet(relay.port, "/tsu", {
      Upgrade: "websocket",
      "Sec-WebSocket-Protocol": PROTOCOL,
    });
    assert(noToken.startsWith("HTTP/1.1 401"), `缺令牌应 401，实际: ${noToken.split("\r\n")[0]}`);
    const badToken = await rawHttpGet(relay.port, "/tsu?token=wrong", { Upgrade: "websocket" });
    assert(
      badToken.startsWith("HTTP/1.1 401"),
      `错误令牌应 401，实际: ${badToken.split("\r\n")[0]}`,
    );
    assert(!/101/i.test(badToken.split("\r\n")[0]), "401 不得返回 101 Switching Protocols");

    // 缺 Upgrade → 426
    const plain = await rawHttpGet(relay.port, "/tsu?token=test-token");
    assert(plain.startsWith("HTTP/1.1 426"), `非升级请求应 426，实际: ${plain.split("\r\n")[0]}`);

    // 声明了别的子协议 → 400
    const wrongProto = await rawHttpGet(relay.port, "/tsu?token=test-token", {
      Upgrade: "websocket",
      "Sec-WebSocket-Protocol": "foo",
    });
    assert(
      wrongProto.startsWith("HTTP/1.1 400"),
      `未知子协议应 400，实际: ${wrongProto.split("\r\n")[0]}`,
    );

    // 正确令牌 + 正确子协议 → 101，并回显 tsu.v1
    const good = await rawHttpGet(relay.port, "/tsu?token=test-token", {
      Upgrade: "websocket",
      Connection: "Upgrade",
      "Sec-WebSocket-Version": "13",
      "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
      "Sec-WebSocket-Protocol": PROTOCOL,
    });
    assert(good.startsWith("HTTP/1.1 101"), `合法握手应 101，实际: ${good.split("\r\n")[0]}`);
    assert(
      /sec-websocket-protocol:\s*tsu\.v1/i.test(good),
      "必须回显 Sec-WebSocket-Protocol: tsu.v1",
    );

    // Authorization: Bearer 同样可用（raw 探测同样拿到 101）
    const bearer = await rawHttpGet(relay.port, "/tsu", {
      Upgrade: "websocket",
      Connection: "Upgrade",
      "Sec-WebSocket-Version": "13",
      "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
      Authorization: "Bearer test-token",
    });
    assert(
      bearer.startsWith("HTTP/1.1 101"),
      `Bearer 令牌应通过，实际: ${bearer.split("\r\n")[0]}`,
    );
  } finally {
    await relay.stop();
  }
});
