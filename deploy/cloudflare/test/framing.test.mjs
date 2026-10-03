/**
 * TSU/1 帧编解码 / 白名单 / 地址解析的单元测试。
 *
 * 全部针对 worker.js 导出的纯函数，直接 import 即可，不依赖 workerd：
 *   node --test test/
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  TsuError,
  checkTarget,
  chunkForFrames,
  classifyConnectError,
  decodeAddress,
  decodeFrame,
  decodeOpenErr,
  encodeAddress,
  encodeFrame,
  encodeOpenErr,
  extractToken,
  formatIPv6,
  isBlockedHost,
  isHostAllowed,
  isKnownOpcode,
  isPortAllowed,
  loadConfig,
  normalizeHost,
  parseAllowHosts,
  parseAllowPorts,
  parseIPv4,
  parseIPv6,
  protocolSpec,
  tokenEquals,
} from "../worker.js";

// worker.js 同时是 Worker 入口，按 workerd 的要求只导出函数/类，
// 常量统一从 protocolSpec() 取（见 worker.js 里的说明）。
const {
  ATYP,
  DATA_CHUNK,
  DEFAULT_ALLOW_PORTS,
  ERR,
  MAX_STREAMS_HARD_LIMIT,
  MAX_WS_RECV,
  OP,
  PROTO,
  SUBPROTOCOL,
  PING_INTERVAL_MS,
  PING_MAX_MISSED,
  BACKPRESSURE_LIMIT,
  CONNECT_TIMEOUT_MS,
  DEFAULT_STREAM_IDLE_TIMEOUT_MS,
  PREOPEN_BUFFER_LIMIT,
} = protocolSpec();

/* ----------------------------- 帧编解码 ----------------------------- */

test("encodeFrame/decodeFrame：opcode + 大端流 id + payload 往返", () => {
  const payload = new TextEncoder().encode("hello");
  const frame = encodeFrame(OP.DATA, 0x01020304, payload);

  assert.equal(frame.byteLength, 5 + payload.byteLength);
  assert.deepEqual([...frame.subarray(0, 5)], [OP.DATA, 0x01, 0x02, 0x03, 0x04]);

  const parsed = decodeFrame(frame);
  assert.equal(parsed.opcode, OP.DATA);
  assert.equal(parsed.streamId, 0x01020304);
  assert.equal(new TextDecoder().decode(parsed.payload), "hello");
});

test("encodeFrame：空 payload 是合法的（CLOSE/RESET/OPEN_OK）", () => {
  const frame = encodeFrame(OP.OPEN_OK, 7);
  assert.equal(frame.byteLength, 5);
  assert.equal(decodeFrame(frame).payload.byteLength, 0);
});

test("decodeFrame：可接受 ArrayBuffer 与 Buffer 视图", () => {
  const frame = encodeFrame(OP.PING, 3, new Uint8Array([1, 2]));
  const fromBuffer = decodeFrame(frame.buffer);
  assert.equal(fromBuffer.streamId, 3);
  assert.deepEqual([...fromBuffer.payload], [1, 2]);
  const fromNodeBuffer = decodeFrame(Buffer.from(frame));
  assert.equal(fromNodeBuffer.opcode, OP.PING);
});

test("decodeFrame：长度不足 5 字节 → BAD_REQUEST", () => {
  for (const bad of [new Uint8Array(0), new Uint8Array(4)]) {
    assert.throws(() => decodeFrame(bad), (err) => err instanceof TsuError && err.code === ERR.BAD_REQUEST);
  }
});

test("decodeFrame：未定义 opcode 抛 BAD_REQUEST 并带出流 id（用于 RESET 该流）", () => {
  for (const opcode of [0x00, 0x09, 0x7f, 0xff]) {
    const frame = new Uint8Array([opcode, 0, 0, 0, 9]);
    assert.equal(isKnownOpcode(opcode), false);
    assert.throws(
      () => decodeFrame(frame),
      (err) => err instanceof TsuError && err.code === ERR.BAD_REQUEST && err.streamId === 9,
    );
  }
  // 表内 opcode 全部可识别
  for (let opcode = OP.OPEN; opcode <= OP.PONG; opcode++) assert.equal(isKnownOpcode(opcode), true);
});

test("decodeFrame：超过 1 MiB 的消息视为协议错误", () => {
  assert.equal(MAX_WS_RECV, 1024 * 1024);
  const frame = new Uint8Array(MAX_WS_RECV + 6);
  frame[0] = OP.DATA;
  assert.throws(() => decodeFrame(frame), (err) => err instanceof TsuError && err.code === ERR.BAD_REQUEST);
});

test("流 id 允许 0xFFFFFFFF，不做有符号处理", () => {
  const frame = encodeFrame(OP.DATA, 0xffffffff, new Uint8Array([1]));
  assert.equal(decodeFrame(frame).streamId, 4294967295);
});

test("DATA 分片大小不超过 32 KiB（规范要求的单帧上限）", () => {
  assert.equal(DATA_CHUNK, 32 * 1024);
  assert.ok(DATA_CHUNK <= 64 * 1024);
});

test("chunkForFrames：32 KiB 以内原样一片", () => {
  const small = new Uint8Array(1024).fill(7);
  const [only] = chunkForFrames(small);
  assert.equal(only.byteLength, 1024);
  assert.equal(only, small); // 不拷贝
  assert.deepEqual(chunkForFrames(new Uint8Array(0)).map((c) => c.byteLength), [0]);
});

test("chunkForFrames：超过 32 KiB 拆成多片且顺序/内容不变", () => {
  const size = 100 * 1024;
  const data = new Uint8Array(size);
  for (let i = 0; i < size; i++) data[i] = (i * 7) & 0xff;

  const chunks = chunkForFrames(data);
  assert.deepEqual(chunks.map((c) => c.byteLength), [32 * 1024, 32 * 1024, 32 * 1024, 4 * 1024]);
  assert.equal(chunks.every((c) => c.byteLength <= DATA_CHUNK), true);
  // 拼回来必须与原数据逐字节相同
  const joined = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  assert.deepEqual([...joined], [...data]);
});

test("chunkForFrames：刚好 32 KiB 边界与 64 KiB 边界", () => {
  assert.deepEqual(chunkForFrames(new Uint8Array(DATA_CHUNK)).map((c) => c.byteLength), [DATA_CHUNK]);
  assert.deepEqual(chunkForFrames(new Uint8Array(DATA_CHUNK * 2)).map((c) => c.byteLength), [DATA_CHUNK, DATA_CHUNK]);
  assert.deepEqual(chunkForFrames(new Uint8Array(DATA_CHUNK + 1)).map((c) => c.byteLength), [DATA_CHUNK, 1]);
});

test("chunkForFrames：可自定义分片大小", () => {
  assert.deepEqual(chunkForFrames(new Uint8Array(10), 4).map((c) => c.byteLength), [4, 4, 2]);
});

test("protocolSpec：规范里的硬性数值全部对齐", () => {
  assert.equal(PROTO, "tsu/1");
  assert.equal(SUBPROTOCOL, "tsu.v1");
  assert.equal(MAX_STREAMS_HARD_LIMIT, 6); // CF 免费版单请求出站连接上限
  assert.equal(BACKPRESSURE_LIMIT, 1024 * 1024); // 规范 3.4
  assert.equal(PING_INTERVAL_MS, 30_000); // 规范 3.6
  assert.equal(PING_MAX_MISSED, 2); // 规范 3.6
  assert.equal(DEFAULT_STREAM_IDLE_TIMEOUT_MS, 300_000); // 规范 3.7
  assert.ok(CONNECT_TIMEOUT_MS <= 30_000); // 别比客户端的 OPEN 超时还晚
  assert.ok(PREOPEN_BUFFER_LIMIT <= BACKPRESSURE_LIMIT);
  assert.equal(Object.isFrozen(protocolSpec()), true);
});

/* --------------------------- OPEN_ERR 错误码 --------------------------- */

test("OPEN_ERR：六个错误码逐一编码为 [code][utf8 消息]", () => {
  const cases = [
    [ERR.NOT_ALLOWED, "not allowed"],
    [ERR.CONNECT_FAILED, "connect failed"],
    [ERR.TOO_MANY_STREAMS, "too many streams"],
    [ERR.BAD_REQUEST, "bad request"],
    [ERR.BLOCKED_TARGET, "blocked target"],
    [ERR.UNAUTHORIZED, "unauthorized"],
  ];
  for (const [code, message] of cases) {
    const payload = encodeOpenErr(code, message);
    assert.equal(payload[0], code);
    assert.equal(new TextDecoder().decode(payload.subarray(1)), message);

    const decoded = decodeOpenErr(payload);
    assert.equal(decoded.code, code);
    assert.equal(decoded.message, message);
  }
  // 码值必须与规范表格一致
  assert.deepEqual(ERR, {
    NOT_ALLOWED: 0x01,
    CONNECT_FAILED: 0x02,
    TOO_MANY_STREAMS: 0x03,
    BAD_REQUEST: 0x04,
    BLOCKED_TARGET: 0x05,
    UNAUTHORIZED: 0x06,
  });
});

test("OPEN_ERR：UTF-8 中文消息往返不乱码", () => {
  const payload = encodeOpenErr(ERR.TOO_MANY_STREAMS, "并发流已满，请换一条连接");
  const decoded = decodeOpenErr(payload);
  assert.equal(decoded.code, ERR.TOO_MANY_STREAMS);
  assert.equal(decoded.message, "并发流已满，请换一条连接");
});

test("OPEN_ERR：未知错误码也能编码（向前兼容）", () => {
  const payload = encodeOpenErr(0x42, "future");
  assert.equal(payload[0], 0x42);
  assert.equal(decodeOpenErr(payload).code, 0x42);
});

/* ---------------------------- 地址编解码 ---------------------------- */

test("IPv4 地址编码往返", () => {
  const encoded = encodeAddress("1.2.3.4", 443);
  assert.deepEqual([...encoded], [ATYP.V4, 1, 2, 3, 4, 0x01, 0xbb]);
  const decoded = decodeAddress(encoded);
  assert.equal(decoded.atyp, ATYP.V4);
  assert.equal(decoded.host, "1.2.3.4");
  assert.equal(decoded.port, 443);
  // 逐字节二次编码一致
  assert.deepEqual([...encodeAddress(decoded.host, decoded.port)], [...encoded]);
});

test("IPv4：最大端口 65535 与最小端口 1", () => {
  assert.equal(decodeAddress(encodeAddress("8.8.8.8", 65535)).port, 65535);
  assert.equal(decodeAddress(encodeAddress("8.8.8.8", 1)).port, 1);
});

test("域名地址编码往返（长度字节 + ASCII 原文）", () => {
  const encoded = encodeAddress("api.github.com", 9418);
  assert.equal(encoded[0], ATYP.DOMAIN);
  assert.equal(encoded[1], "api.github.com".length);
  assert.equal(new TextDecoder().decode(encoded.subarray(2, 2 + encoded[1])), "api.github.com");
  const decoded = decodeAddress(encoded);
  assert.equal(decoded.atyp, ATYP.DOMAIN);
  assert.equal(decoded.host, "api.github.com");
  assert.equal(decoded.port, 9418);
});

test("域名：255 字节上限，超长抛 BAD_REQUEST", () => {
  const ok = "a".repeat(63) + "." + "b".repeat(63) + "." + "c".repeat(63) + "." + "d".repeat(63);
  assert.equal(ok.length, 255);
  assert.equal(decodeAddress(encodeAddress(ok, 443)).host, ok);
  assert.throws(
    () => encodeAddress("e".repeat(256), 443),
    (err) => err.code === ERR.BAD_REQUEST,
  );
});

test("域名：拒绝非 ASCII（不做本地 IDNA 转换，避免 DNS 泄漏）", () => {
  assert.throws(() => encodeAddress("例え.jp", 443), (err) => err.code === ERR.BAD_REQUEST);
});

test("域名：解码时去掉结尾点，其余原样保留大小写", () => {
  assert.equal(decodeAddress(encodeAddress("example.com", 443)).host, "example.com");
  const withDot = new Uint8Array([ATYP.DOMAIN, 12, ...new TextEncoder().encode("Example.COM."), 0x01, 0xbb]);
  assert.equal(decodeAddress(withDot).host, "Example.COM");
});

test("IPv6 地址编码往返（含压缩写法与全零段）", () => {
  const cases = [
    "2606:4700:4700::1111",
    "2001:db8::1",
    "::1",
    "::",
    "2001:db8:0:0:1:0:0:1",
    "fe80::1",
    "2001:0db8:85a3:0000:0000:8a2e:0370:7334",
  ];
  for (const host of cases) {
    const encoded = encodeAddress(host, 443);
    assert.equal(encoded[0], ATYP.V6, `${host} 应编码为 IPv6`);
    assert.equal(encoded.byteLength, 1 + 16 + 2, `${host} 长度应为 19`);
    const decoded = decodeAddress(encoded);
    assert.equal(decoded.atyp, ATYP.V6);
    assert.equal(decoded.port, 443);
    // 解码出来的字符串再编码必须字节相同（规范压缩形式）
    assert.deepEqual([...encodeAddress(decoded.host, 443)], [...encoded], `${host} → ${decoded.host}`);
  }
});

test("IPv6：格式化为 RFC 5952 压缩形式", () => {
  assert.equal(formatIPv6(parseIPv6("::1")), "::1");
  assert.equal(formatIPv6(parseIPv6("::")), "::");
  assert.equal(formatIPv6(parseIPv6("2001:0db8:0000:0000:0000:0000:0000:0001")), "2001:db8::1");
  assert.equal(formatIPv6(parseIPv6("2001:db8:0:0:1:0:0:1")), "2001:db8::1:0:0:1");
  assert.equal(formatIPv6(parseIPv6("fe80::1")), "fe80::1");
});

test("IPv6：内嵌 IPv4 写法（::ffff:1.2.3.4）", () => {
  const parsed = parseIPv6("::ffff:1.2.3.4");
  assert.deepEqual([...parsed.subarray(0, 10)], new Array(10).fill(0));
  assert.deepEqual([...parsed.subarray(10, 12)], [0xff, 0xff]);
  assert.deepEqual([...parsed.subarray(12)], [1, 2, 3, 4]);
});

test("IPv6：非法写法返回 null", () => {
  for (const bad of ["1:2:3:4:5:6:7:8:9", "2001:db8:::1", "gggg::1", "2001:db8::1::2", "12345::1"]) {
    assert.equal(parseIPv6(bad), null, bad);
  }
});

test("地址解码：长度与声明不符 / 未知 atyp / 端口 0 → BAD_REQUEST", () => {
  const cases = [
    new Uint8Array([ATYP.V4, 1, 2, 3, 4, 0x01]), // 缺端口
    new Uint8Array([ATYP.V4, 1, 2, 3, 4, 0x01, 0xbb, 0xff]), // 多余字节
    new Uint8Array([ATYP.DOMAIN, 0, 0x01, 0xbb]), // 域名长度为 0
    new Uint8Array([ATYP.DOMAIN, 9, 0x61, 0x01, 0xbb]), // 声明长度超出实际
    new Uint8Array([ATYP.DOMAIN, 2, 0xc3, 0xa9, 0x01, 0xbb]), // 非 ASCII 字节
    new Uint8Array([ATYP.V4, 1, 2, 3, 4, 0x00, 0x00]), // 端口 0
    new Uint8Array([0x05, 1, 2, 3, 4, 0x01, 0xbb]), // 未知 atyp
    new Uint8Array([]),
  ];
  for (const payload of cases) {
    assert.throws(() => decodeAddress(payload), (err) => err instanceof TsuError && err.code === ERR.BAD_REQUEST, [
      ...payload,
    ].join(","));
  }
});

test("地址解码：IPv6 长度不足 → BAD_REQUEST", () => {
  assert.throws(() => decodeAddress(new Uint8Array([ATYP.V6, 1, 2, 3])), (err) => err.code === ERR.BAD_REQUEST);
});

/* ------------------------------ 白名单 ------------------------------ */

test("normalizeHost：去空白、转小写、去结尾点", () => {
  assert.equal(normalizeHost(" API.GitHub.COM. "), "api.github.com");
  assert.equal(normalizeHost("[::1]"), "::1");
});

test("parseAllowHosts：逗号分隔 + 归一化，丢弃空项", () => {
  assert.deepEqual(parseAllowHosts(" GitHub.com , .githubusercontent.com ,, "), [
    "github.com",
    ".githubusercontent.com",
  ]);
  assert.deepEqual(parseAllowHosts(""), []);
  assert.deepEqual(parseAllowHosts(undefined), []);
});

test("白名单后缀匹配：条目命中裸域与其任意层级子域", () => {
  const entries = parseAllowHosts("github.com");
  assert.equal(isHostAllowed("github.com", entries), true);
  assert.equal(isHostAllowed("api.github.com", entries), true);
  assert.equal(isHostAllowed("a.b.github.com", entries), true);
  assert.equal(isHostAllowed("notgithub.com", entries), false);
  assert.equal(isHostAllowed("github.com.evil.net", entries), false);
  assert.equal(isHostAllowed("evilgithub.com", entries), false);
});

test("白名单：. 开头的条目只匹配子域，不匹配裸域本身", () => {
  const entries = parseAllowHosts(".githubusercontent.com");
  assert.equal(isHostAllowed("raw.githubusercontent.com", entries), true);
  assert.equal(isHostAllowed("a.b.githubusercontent.com", entries), true);
  assert.equal(isHostAllowed("githubusercontent.com", entries), false);
});

test("白名单：大小写与结尾点不影响匹配", () => {
  assert.equal(isHostAllowed("API.GitHub.COM", parseAllowHosts("github.com")), true);
  assert.equal(isHostAllowed("api.github.com.", parseAllowHosts("GitHub.COM")), true);
  assert.equal(isHostAllowed("API.GithubUserContent.com", parseAllowHosts(".GitHubUserContent.COM.")), true);
});

test("白名单：IP 字面量按完整字符串匹配（不做网段）", () => {
  const entries = parseAllowHosts("1.1.1.1");
  assert.equal(isHostAllowed("1.1.1.1", entries), true);
  assert.equal(isHostAllowed("1.1.1.2", entries), false);
});

test("白名单：空列表一律拒绝，IPv6 也能匹配", () => {
  assert.equal(isHostAllowed("example.com", []), false);
  assert.equal(isHostAllowed("", parseAllowHosts("*")), false);
  assert.equal(isHostAllowed("2606:4700::1111", parseAllowHosts("2606:4700::1111")), true);
});

/* ----------------------------- 端口白名单 ----------------------------- */

test("默认端口白名单是 443,80,22,9418", () => {
  const ports = parseAllowPorts(DEFAULT_ALLOW_PORTS.join(","));
  assert.deepEqual([...ports].sort((a, b) => a - b), [22, 80, 443, 9418]);
  assert.equal(isPortAllowed(443, ports), true);
  assert.equal(isPortAllowed(9418, ports), true);
  assert.equal(isPortAllowed(8080, ports), false);
  assert.equal(isPortAllowed(25, ports), false);
});

test("端口白名单：非法项丢弃，0/越界不生效", () => {
  const ports = parseAllowPorts("443, 0, 65536, -1, abc, 80");
  assert.deepEqual([...ports].sort((a, b) => a - b), [80, 443]);
  assert.equal(isPortAllowed(0, ports), false);
});

/* ---------------------------- 目标过滤 ---------------------------- */

test("私网/回环/链路本地地址一律 BLOCKED_TARGET", () => {
  for (const host of [
    "0.0.0.0",
    "0.1.2.3",
    "10.0.0.1",
    "127.0.0.1",
    "127.1.2.3",
    "169.254.169.254",
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
    "ff02::1",
  ]) {
    assert.equal(isBlockedHost(host), true, host);
  }
});

test("公网地址不拦截", () => {
  for (const host of [
    "1.1.1.1",
    "8.8.8.8",
    "172.32.0.1",
    "192.169.1.1",
    "2606:4700:4700::1111",
    "2001:db8::1",
    "github.com",
    "notlocalhost.com",
  ]) {
    assert.equal(isBlockedHost(host), false, host);
  }
});

test("checkTarget：白名单模式下的四类结果", () => {
  const cfg = loadConfig({
    TSU_ALLOW_ALL: "0",
    TSU_ALLOW_HOSTS: "github.com",
    TSU_ALLOW_PORTS: "443",
  });
  assert.equal(checkTarget("api.github.com", 443, cfg), null);
  assert.equal(checkTarget("example.com", 443, cfg), ERR.NOT_ALLOWED);
  assert.equal(checkTarget("api.github.com", 80, cfg), ERR.NOT_ALLOWED);
  assert.equal(checkTarget("127.0.0.1", 443, cfg), ERR.BLOCKED_TARGET);
  assert.equal(checkTarget("10.0.0.1", 80, cfg), ERR.BLOCKED_TARGET);
});

test("checkTarget：allow_all 关掉白名单后私网仍被拒", () => {
  const cfg = loadConfig({ TSU_ALLOW_ALL: "1" });
  assert.equal(cfg.allowAll, true);
  assert.equal(checkTarget("example.com", 443, cfg), null);
  assert.equal(checkTarget("example.com", 8080, cfg), ERR.NOT_ALLOWED);
  assert.equal(checkTarget("192.168.1.1", 443, cfg), ERR.BLOCKED_TARGET);
});

/* ------------------------------ 配置 ------------------------------ */

test("loadConfig：默认值符合规范（白名单开、max_streams=6）", () => {
  const cfg = loadConfig({});
  assert.equal(cfg.allowAll, false);
  assert.equal(cfg.maxStreams, MAX_STREAMS_HARD_LIMIT);
  assert.equal(cfg.maxStreams, 6);
  assert.equal(cfg.path, "/tsu");
  assert.equal(cfg.streamIdleTimeoutMs, 300_000);
  assert.equal(isPortAllowed(9418, cfg.allowPorts), true);
});

test("loadConfig：max_streams 夹到 [1,6]，任何配置都不能超过 CF 免费版上限", () => {
  assert.equal(loadConfig({ TSU_MAX_STREAMS: "64" }).maxStreams, 6);
  assert.equal(loadConfig({ TSU_MAX_STREAMS: "999999" }).maxStreams, 6);
  assert.equal(loadConfig({ TSU_MAX_STREAMS: "3" }).maxStreams, 3);
  assert.equal(loadConfig({ TSU_MAX_STREAMS: "0" }).maxStreams, 1);
  assert.equal(loadConfig({ TSU_MAX_STREAMS: "-5" }).maxStreams, 1);
  assert.equal(loadConfig({ TSU_MAX_STREAMS: "abc" }).maxStreams, 6);
});

test("loadConfig：TSU_ALLOW_ALL 只有 \"1\" 才算开启", () => {
  assert.equal(loadConfig({ TSU_ALLOW_ALL: "1" }).allowAll, true);
  assert.equal(loadConfig({ TSU_ALLOW_ALL: "0" }).allowAll, false);
  assert.equal(loadConfig({ TSU_ALLOW_ALL: "true" }).allowAll, false);
  assert.equal(loadConfig({}).allowAll, false);
});

test("loadConfig：PROTO 与路径前缀", () => {
  assert.equal(PROTO, "tsu/1");
  assert.equal(loadConfig({ TSU_PATH: "/x" }).path, "/x");
});

/* ------------------------------ 鉴权 ------------------------------ */

test("extractToken：优先 ?token=，其次 Authorization: Bearer", () => {
  const mk = (headersMap) => ({ headers: { get: (k) => headersMap[k.toLowerCase()] ?? null } });
  const url = new URL("https://relay.example/tsu?token=abc");
  assert.equal(extractToken(mk({ authorization: "Bearer def" }), url), "abc");
  assert.equal(extractToken(mk({ authorization: "Bearer def" }), new URL("https://relay.example/tsu")), "def");
  assert.equal(extractToken(mk({ authorization: "bearer def" }), new URL("https://relay.example/tsu")), "def");
  assert.equal(extractToken(mk({ authorization: "Basic def" }), new URL("https://relay.example/tsu")), "");
  assert.equal(extractToken(mk({}), new URL("https://relay.example/tsu")), "");
});

test("tokenEquals：长度/内容不同即失败，空令牌永不通过", () => {
  assert.equal(tokenEquals("s3cret", "s3cret"), true);
  assert.equal(tokenEquals("s3cret", "s3cres"), false);
  assert.equal(tokenEquals("s3cre", "s3cret"), false);
  assert.equal(tokenEquals("s3cret2", "s3cret"), false);
  assert.equal(tokenEquals("", ""), false);
  assert.equal(tokenEquals("x", ""), false);
  assert.equal(tokenEquals("", "x"), false);
  assert.equal(tokenEquals(undefined, "x"), false);
});

/* --------------------------- 错误映射 --------------------------- */

test("classifyConnectError：平台拒绝类报错 → BLOCKED_TARGET", () => {
  for (const message of [
    "cannot connect to the specified address",
    "cannot connect to the specified address: Network connection failed",
    "Cannot connect to the specified address. Cloudflare does not allow connecting to this address.",
    "Connecting to a private address is not allowed",
    "connection to loopback address blocked",
    "port 25 is not allowed",
  ]) {
    assert.equal(classifyConnectError(new Error(message)), ERR.BLOCKED_TARGET, message);
  }
});

test("classifyConnectError：超并发 → TOO_MANY_STREAMS，其余 → CONNECT_FAILED", () => {
  assert.equal(classifyConnectError(new Error("Too many simultaneous connections")), ERR.TOO_MANY_STREAMS);
  assert.equal(classifyConnectError(new Error("concurrent connect limit exceeded")), ERR.TOO_MANY_STREAMS);
  assert.equal(classifyConnectError(new Error("getaddrinfo ENOTFOUND nope.invalid")), ERR.CONNECT_FAILED);
  assert.equal(classifyConnectError(new Error("connection reset by peer")), ERR.CONNECT_FAILED);
  assert.equal(classifyConnectError(undefined), ERR.CONNECT_FAILED);
  assert.equal(classifyConnectError(""), ERR.CONNECT_FAILED);
});

test("分类顺序：既含 too many 又含 blocked 字样时优先 TOO_MANY_STREAMS", () => {
  assert.equal(classifyConnectError(new Error("too many connections, not allowed")), ERR.TOO_MANY_STREAMS);
});

/* --------------------------- opcode 常量 --------------------------- */

test("opcode 数值与规范表格完全一致", () => {
  assert.deepEqual(OP, {
    OPEN: 0x01,
    OPEN_OK: 0x02,
    OPEN_ERR: 0x03,
    DATA: 0x04,
    CLOSE: 0x05,
    RESET: 0x06,
    PING: 0x07,
    PONG: 0x08,
  });
  assert.deepEqual(ATYP, { V4: 0x01, DOMAIN: 0x03, V6: 0x04 });
});

test("opcode 方向：服务端专属帧由客户端发来时按未知帧处理（resetStream）", () => {
  // worker.js 内部对 OPEN_OK/OPEN_ERR 的处理分支依赖这两个常量，这里守住定义。
  const frame = encodeFrame(OP.OPEN_ERR, 5, encodeOpenErr(ERR.CONNECT_FAILED, "x"));
  assert.equal(decodeFrame(frame).streamId, 5);
  assert.equal(decodeFrame(frame).opcode, OP.OPEN_ERR);
});

test("PING/PONG payload ≤ 32 字节且原样回显", () => {
  const ping = new Uint8Array(32).fill(0xab);
  const frame = decodeFrame(encodeFrame(OP.PING, 0, ping));
  assert.deepEqual([...frame.payload], [...ping]);
  const pong = decodeFrame(encodeFrame(OP.PONG, 0, frame.payload.subarray(0, 32)));
  assert.deepEqual([...pong.payload], [...ping]);
});

test("parseIPv4：拒绝越界与前导零之外的非法写法", () => {
  assert.equal(parseIPv4("256.1.1.1"), null);
  assert.equal(parseIPv4("1.1.1"), null);
  assert.equal(parseIPv4("1.1.1.1.1"), null);
  assert.equal(parseIPv4("1.1.1.a"), null);
  assert.deepEqual([...parseIPv4("255.255.255.255")], [255, 255, 255, 255]);
});
