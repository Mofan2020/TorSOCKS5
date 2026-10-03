/**
 * TorSOCKS5 隧道协议 TSU/1 —— Cloudflare Worker 形态的中继。
 *
 * 规范（唯一真相源）：docs/tunnel-protocol.md
 *   * 载体：WebSocket 二进制消息，一条 WS 消息 = 恰好一个 TSU 帧
 *   * 出网：cloudflare:sockets 的 connect()（平台不允许入站 TCP CONNECT，只能用 WS 承载）
 *   * 并发：单请求出站连接上限 6（免费版硬限制）→ max_streams 固定不得超过 6
 *
 * 单文件、零构建、零第三方依赖；纯逻辑（编解码/白名单/地址解析）全部导出，
 * 便于 test/framing.test.mjs 直接用 node --test 覆盖，无需 workerd。
 */

const PROTO = "tsu/1";
const SUBPROTOCOL = "tsu.v1";
const DEFAULT_PATH = "/tsu";

/** 帧 opcode 表（docs/tunnel-protocol.md 2.1）。 */
const OP = {
  OPEN: 0x01,
  OPEN_OK: 0x02,
  OPEN_ERR: 0x03,
  DATA: 0x04,
  CLOSE: 0x05,
  RESET: 0x06,
  PING: 0x07,
  PONG: 0x08,
};

/** 地址类型编号（与 SOCKS5 ATYP 一致）。 */
const ATYP = { V4: 0x01, DOMAIN: 0x03, V6: 0x04 };

/** OPEN_ERR 错误码（docs/tunnel-protocol.md 2.3）。 */
const ERR = {
  NOT_ALLOWED: 0x01,
  CONNECT_FAILED: 0x02,
  TOO_MANY_STREAMS: 0x03,
  BAD_REQUEST: 0x04,
  BLOCKED_TARGET: 0x05,
  UNAUTHORIZED: 0x06,
};

const ERR_TEXT = {
  [ERR.NOT_ALLOWED]: "not allowed",
  [ERR.CONNECT_FAILED]: "connect failed",
  [ERR.TOO_MANY_STREAMS]: "too many streams",
  [ERR.BAD_REQUEST]: "bad request",
  [ERR.BLOCKED_TARGET]: "blocked target",
  [ERR.UNAUTHORIZED]: "unauthorized",
};

/** 默认端口白名单。 */
const DEFAULT_ALLOW_PORTS = [443, 80, 22, 9418];

/**
 * 默认域名白名单：白名单模式下必须显式配置，这里只给一份最小示例，
 * 部署时按需在 wrangler.toml 的 [vars] 里替换。
 */
const DEFAULT_ALLOW_HOSTS = ["torproject.org", "github.com", ".githubusercontent.com"];

/** 单条 WS 连接上的隧道数量上限（CF 免费版单请求出站连接硬上限）。 */
const MAX_STREAMS_HARD_LIMIT = 6;
/** 分片大小：单个 DATA 帧的 payload 上限。 */
const DATA_CHUNK = 32 * 1024;
/** 发送方单个 WS 消息上限。 */
const MAX_WS_SEND = 64 * 1024;
/** 接收方必须能接受的最大消息；超过视为协议错误。 */
const MAX_WS_RECV = 1024 * 1024;
/** 待发送队列超过该值时暂停从 TCP 读取（规范 3.4：禁止无界缓冲）。 */
const BACKPRESSURE_LIMIT = 1024 * 1024;
/** 空闲多久后发 PING。 */
const PING_INTERVAL_MS = 30_000;
/** 连续多少次 PING 未收到 PONG 判定链路失效。 */
const PING_MAX_MISSED = 2;
/** 单条流空闲超时（规范 3.7）。 */
const DEFAULT_STREAM_IDLE_TIMEOUT_MS = 300_000;
/** TCP 建连超时（客户端侧的 OPEN 超时是 30 秒，中继略早收手）。 */
const CONNECT_TIMEOUT_MS = 25_000;
/** 流在 OPEN_OK 之前收到的 DATA 最多暂存多少字节。 */
const PREOPEN_BUFFER_LIMIT = 64 * 1024;

const EMPTY = new Uint8Array(0);

/**
 * 规范常量表（opcode、错误码、地址类型、各类上限）。
 *
 * 为什么用函数而不是 `export const`：本文件同时是 Worker 的入口模块，workerd 要求
 * 入口模块的每一个导出都是函数或类，直接导出普通值会让运行时启动失败
 * （`Incorrect type for map entry: ... is not of type 'function or ExportedHandler'`）。
 * 测试从这里取常量，保证测的就是运行时真正用的那份表。
 */
export function protocolSpec() {
  return Object.freeze({
    PROTO,
    SUBPROTOCOL,
    DEFAULT_PATH,
    OP: Object.freeze({ ...OP }),
    ATYP: Object.freeze({ ...ATYP }),
    ERR: Object.freeze({ ...ERR }),
    DEFAULT_ALLOW_PORTS: Object.freeze([...DEFAULT_ALLOW_PORTS]),
    DEFAULT_ALLOW_HOSTS: Object.freeze([...DEFAULT_ALLOW_HOSTS]),
    MAX_STREAMS_HARD_LIMIT,
    DATA_CHUNK,
    MAX_WS_SEND,
    MAX_WS_RECV,
    BACKPRESSURE_LIMIT,
    PING_INTERVAL_MS,
    PING_MAX_MISSED,
    DEFAULT_STREAM_IDLE_TIMEOUT_MS,
    CONNECT_TIMEOUT_MS,
    PREOPEN_BUFFER_LIMIT,
  });
}

/** 协议错误：code 为 ERR.* 之一，用于回 OPEN_ERR / 决定是否 RESET。 */
export class TsuError extends Error {
  constructor(code, message, streamId = 0) {
    super(message);
    this.name = "TsuError";
    this.code = code;
    this.streamId = streamId;
  }
}

/* ------------------------------------------------------------------ *
 * 帧编解码
 * ------------------------------------------------------------------ */

/** 判断 opcode 是否在规范定义的表内。 */
export function isKnownOpcode(opcode) {
  return Number.isInteger(opcode) && opcode >= OP.OPEN && opcode <= OP.PONG;
}

function toBytes(data) {
  if (data instanceof Uint8Array) return data;
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  if (ArrayBuffer.isView(data)) {
    return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  }
  throw new TsuError(ERR.BAD_REQUEST, "不支持的帧类型");
}

/** 组装一个 TSU 帧：opcode(1) + stream id(4, 大端) + payload。 */
export function encodeFrame(opcode, streamId, payload = EMPTY) {
  const body = payload == null ? EMPTY : toBytes(payload);
  const out = new Uint8Array(5 + body.byteLength);
  out[0] = opcode;
  new DataView(out.buffer).setUint32(1, streamId >>> 0, false);
  out.set(body, 5);
  return out;
}

/** 解析一个 TSU 帧；长度不足、opcode 未定义时抛 TsuError(BAD_REQUEST)。 */
export function decodeFrame(data) {
  const bytes = toBytes(data);
  if (bytes.byteLength < 5) {
    throw new TsuError(ERR.BAD_REQUEST, "帧长度不足 5 字节");
  }
  if (bytes.byteLength > MAX_WS_RECV) {
    throw new TsuError(ERR.BAD_REQUEST, "帧超过 1 MiB 上限");
  }
  const opcode = bytes[0];
  const streamId = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(1, false);
  if (!isKnownOpcode(opcode)) {
    throw new TsuError(ERR.BAD_REQUEST, `未定义的 opcode 0x${opcode.toString(16)}`, streamId);
  }
  return { opcode, streamId, payload: bytes.subarray(5) };
}

/** OPEN_ERR 的 payload：[code:1][utf8 消息]。 */
export function encodeOpenErr(code, message) {
  const text = new TextEncoder().encode(message ?? ERR_TEXT[code] ?? "error");
  const out = new Uint8Array(1 + text.byteLength);
  out[0] = code & 0xff;
  out.set(text, 1);
  return out;
}

/** 解析 OPEN_ERR 的 payload，测试与客户端复用。 */
export function decodeOpenErr(payload) {
  const bytes = toBytes(payload);
  if (bytes.byteLength < 1) throw new TsuError(ERR.BAD_REQUEST, "OPEN_ERR 缺少错误码");
  return { code: bytes[0], message: new TextDecoder().decode(bytes.subarray(1)) };
}

/**
 * 把 TCP 读到的数据切成 ≤ 32 KiB 的多个 payload（规范 4.5：超过 32 KiB 必须分片）。
 * 分片后每个切片都是原数据的视图，不做拷贝。
 */
export function chunkForFrames(data, size = DATA_CHUNK) {
  const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
  if (bytes.byteLength <= size) return [bytes];
  const out = [];
  for (let offset = 0; offset < bytes.byteLength; offset += size) {
    out.push(bytes.subarray(offset, Math.min(offset + size, bytes.byteLength)));
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * 地址编解码
 * ------------------------------------------------------------------ */

export function parseIPv4(host) {
  const parts = String(host).split(".");
  if (parts.length !== 4) return null;
  const out = new Uint8Array(4);
  for (let i = 0; i < 4; i++) {
    if (!/^\d{1,3}$/.test(parts[i])) return null;
    const n = Number(parts[i]);
    if (n > 255) return null;
    out[i] = n;
  }
  return out;
}

export function parseIPv6(host) {
  let text = String(host).trim().replace(/^\[|\]$/g, "");
  if (!text.includes(":")) return null;
  // 去掉 zone id（fe80::1%eth0），平台不支持也不该出现。
  const pct = text.indexOf("%");
  if (pct >= 0) text = text.slice(0, pct);
  if (text.includes(".")) {
    const lastColon = text.lastIndexOf(":");
    const v4 = parseIPv4(text.slice(lastColon + 1));
    if (!v4) return null;
    const hex = [...v4].map((b) => b.toString(16).padStart(2, "0"));
    text = `${text.slice(0, lastColon)}:${hex[0]}${hex[1]}:${hex[2]}${hex[3]}`;
  }
  const halves = text.split("::");
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(":") : [];
  const tail = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  for (const group of [...head, ...tail]) {
    if (!/^[0-9a-fA-F]{1,4}$/.test(group)) return null;
  }
  const headWords = head.map((group) => Number.parseInt(group, 16));
  const tailWords = tail.map((group) => Number.parseInt(group, 16));
  const words = [];
  if (halves.length === 2) {
    // 压缩写法：中间至少要省略一组（规范外的 "1:2:3:4:5:6:7::8" 之类直接拒绝）。
    if (headWords.length + tailWords.length > 7) return null;
    words.push(...headWords);
    while (words.length < 8 - tailWords.length) words.push(0);
    words.push(...tailWords);
  } else {
    if (headWords.length !== 8) return null;
    words.push(...headWords);
  }
  if (words.length !== 8) return null;
  const out = new Uint8Array(16);
  words.forEach((w, i) => {
    out[i * 2] = (w >> 8) & 0xff;
    out[i * 2 + 1] = w & 0xff;
  });
  return out;
}

/** 把 16 字节 IPv6 按 RFC 5952 压缩成字符串（用于 decode 后的人类可读形式）。 */
export function formatIPv6(bytes) {
  const words = [];
  for (let i = 0; i < 8; i++) words.push((bytes[i * 2] << 8) | bytes[i * 2 + 1]);
  let bestStart = -1;
  let bestLen = 0;
  for (let i = 0; i < 8; i++) {
    if (words[i] !== 0) continue;
    let j = i;
    while (j < 8 && words[j] === 0) j++;
    if (j - i > bestLen) {
      bestStart = i;
      bestLen = j - i;
    }
    i = j;
  }
  if (bestLen < 2) bestStart = -1;
  const parts = [];
  for (let i = 0; i < 8; i++) {
    if (bestStart >= 0 && i >= bestStart && i < bestStart + bestLen) {
      if (i === bestStart) parts.push("");
      continue;
    }
    parts.push(words[i].toString(16));
  }
  let text = parts.join(":");
  if (bestStart === 0) text = `:${text}`;
  if (bestStart >= 0 && bestStart + bestLen === 8) text = `${text}:`;
  return text;
}

/** 把 host/port 编码为 OPEN 的 payload（atyp + addr + port）。 */
export function encodeAddress(host, port) {
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new TsuError(ERR.BAD_REQUEST, `端口非法: ${port}`);
  }
  const v4 = parseIPv4(host);
  if (v4) return concat([new Uint8Array([ATYP.V4]), v4, portBytes(port)]);
  const v6 = parseIPv6(host);
  if (v6) return concat([new Uint8Array([ATYP.V6]), v6, portBytes(port)]);
  const name = String(host);
  if (!/^[\x21-\x7e]+$/.test(name)) {
    throw new TsuError(ERR.BAD_REQUEST, "域名必须是可见 ASCII（不得做本地 IDNA 转换）");
  }
  const raw = new TextEncoder().encode(name);
  if (raw.byteLength === 0 || raw.byteLength > 255) {
    throw new TsuError(ERR.BAD_REQUEST, "域名长度非法");
  }
  return concat([new Uint8Array([ATYP.DOMAIN, raw.byteLength]), raw, portBytes(port)]);
}

/** 解析 OPEN 的 payload，返回 { atyp, host, port }。 */
export function decodeAddress(payload) {
  const bytes = toBytes(payload);
  if (bytes.byteLength < 1) throw new TsuError(ERR.BAD_REQUEST, "OPEN 缺少地址");
  const atyp = bytes[0];
  let offset = 1;
  let host;
  if (atyp === ATYP.V4) {
    if (bytes.byteLength < offset + 4 + 2) throw new TsuError(ERR.BAD_REQUEST, "IPv4 地址长度不足");
    host = [...bytes.subarray(offset, offset + 4)].join(".");
    offset += 4;
  } else if (atyp === ATYP.V6) {
    if (bytes.byteLength < offset + 16 + 2) throw new TsuError(ERR.BAD_REQUEST, "IPv6 地址长度不足");
    host = formatIPv6(bytes.subarray(offset, offset + 16));
    offset += 16;
  } else if (atyp === ATYP.DOMAIN) {
    if (bytes.byteLength < offset + 1) throw new TsuError(ERR.BAD_REQUEST, "域名长度字节缺失");
    const len = bytes[offset];
    offset += 1;
    if (len === 0 || bytes.byteLength < offset + len + 2) {
      throw new TsuError(ERR.BAD_REQUEST, "域名长度非法");
    }
    const raw = bytes.subarray(offset, offset + len);
    for (const b of raw) {
      if (b < 0x21 || b > 0x7e) throw new TsuError(ERR.BAD_REQUEST, "域名含非 ASCII 字节");
    }
    host = new TextDecoder().decode(raw);
    offset += len;
  } else {
    throw new TsuError(ERR.BAD_REQUEST, `未知的地址类型 0x${atyp.toString(16)}`);
  }
  if (bytes.byteLength !== offset + 2) throw new TsuError(ERR.BAD_REQUEST, "地址长度与声明不符");
  const port = (bytes[offset] << 8) | bytes[offset + 1];
  if (port === 0) throw new TsuError(ERR.BAD_REQUEST, "端口 0 非法");
  // 域名大小写与结尾点由发送方负责；这里只按规范去掉结尾点后原样交给平台解析
  // （不在本地做 DNS，避免 DNS 泄漏）。
  return { atyp, host: atyp === ATYP.DOMAIN ? host.replace(/\.+$/, "") : host, port };
}

function portBytes(port) {
  return new Uint8Array([(port >> 8) & 0xff, port & 0xff]);
}

function concat(parts) {
  let total = 0;
  for (const p of parts) total += p.byteLength;
  const out = new Uint8Array(total);
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.byteLength;
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * 白名单与目标过滤
 * ------------------------------------------------------------------ */

/** 归一化：去空白、转小写、去掉结尾点与 IPv6 方括号。 */
export function normalizeHost(host) {
  return String(host ?? "")
    .trim()
    .replace(/^\[|\]$/g, "")
    .replace(/\.+$/, "")
    .toLowerCase();
}

/** 解析 TSU_ALLOW_HOSTS："a.com,.b.com" → ["a.com", ".b.com"]。 */
export function parseAllowHosts(value) {
  return String(value ?? "")
    .split(",")
    .map((item) => normalizeHost(item))
    .filter((item) => item.length > 0);
}

/**
 * 后缀匹配：
 *   * "github.com" 命中 github.com 与 api.github.com
 *   * ".githubusercontent.com" 只命中子域，不命中裸域本身
 */
export function isHostAllowed(host, entries) {
  const target = normalizeHost(host);
  if (!target) return false;
  for (const entry of entries ?? []) {
    const rule = normalizeHost(entry);
    if (!rule) continue;
    if (rule.startsWith(".")) {
      if (target.length > rule.length && target.endsWith(rule)) return true;
    } else if (target === rule || target.endsWith(`.${rule}`)) {
      return true;
    }
  }
  return false;
}

/** 解析 TSU_ALLOW_PORTS："443,80" → Set{443,80}；非法项直接丢弃。 */
export function parseAllowPorts(value) {
  const ports = new Set();
  for (const item of String(value ?? "").split(",")) {
    const n = Number.parseInt(item.trim(), 10);
    if (Number.isInteger(n) && n > 0 && n <= 65535) ports.add(n);
  }
  return ports;
}

export function isPortAllowed(port, ports) {
  return ports instanceof Set ? ports.has(port) : false;
}

function v4Blocked(bytes) {
  const [a, b] = bytes;
  if (a === 0 || a === 10 || a === 127) return true;
  if (a === 169 && b === 254) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  return false;
}

function v6Blocked(bytes) {
  // ::1 / :: == 未指定地址
  let allZero = true;
  for (let i = 0; i < 15; i++) if (bytes[i] !== 0) allZero = false;
  if (allZero && (bytes[15] === 0 || bytes[15] === 1)) return true;
  // IPv4-mapped / IPv4-compatible: ::ffff:a.b.c.d 与 ::a.b.c.d
  let headZero = true;
  for (let i = 0; i < 10; i++) if (bytes[i] !== 0) headZero = false;
  const mapped = headZero && bytes[10] === 0xff && bytes[11] === 0xff;
  const compatible = headZero && bytes[10] === 0 && bytes[11] === 0 && (bytes[12] || bytes[13] || bytes[14] || bytes[15]);
  if (mapped || compatible) return v4Blocked(bytes.subarray(12));
  if ((bytes[0] & 0xfe) === 0xfc) return true; // fc00::/7 唯一本地地址
  if (bytes[0] === 0xfe && (bytes[1] & 0xc0) === 0x80) return true; // fe80::/10 链路本地
  if (bytes[0] === 0xff) return true; // ff00::/8 组播：平台同样拒绝，直接归一为 BLOCKED_TARGET
  return false;
}

/**
 * 私网 / 回环 / 链路本地 / 本机名判断（规范 3.2）。
 * 命中返回 true → 回 BLOCKED_TARGET。
 */
export function isBlockedHost(host) {
  const target = normalizeHost(host);
  if (!target) return true;
  if (target === "localhost" || target.endsWith(".localhost")) return true;
  const v4 = parseIPv4(target);
  if (v4) return v4Blocked(v4);
  const v6 = parseIPv6(target);
  if (v6) return v6Blocked(v6);
  return false;
}

/* ------------------------------------------------------------------ *
 * 鉴权与配置
 * ------------------------------------------------------------------ */

/** 令牌来源：优先 ?token=，其次 Authorization: Bearer。 */
export function extractToken(request, url) {
  const fromQuery = url?.searchParams?.get("token");
  if (fromQuery) return fromQuery;
  const header = request?.headers?.get("Authorization") ?? "";
  const matched = /^Bearer\s+(.+)$/i.exec(header.trim());
  return matched ? matched[1].trim() : "";
}

/**
 * 定长比较：长度不等直接失败，但绝不提前返回，避免用时序差异探测令牌。
 * 空令牌一律视为不匹配（未配置 TSU_TOKEN 时 fail-closed）。
 */
export function tokenEquals(provided, expected) {
  const a = new TextEncoder().encode(String(provided ?? ""));
  const b = new TextEncoder().encode(String(expected ?? ""));
  if (a.byteLength === 0 || b.byteLength === 0) return false;
  let diff = a.byteLength ^ b.byteLength;
  const n = Math.max(a.byteLength, b.byteLength);
  for (let i = 0; i < n; i++) {
    diff |= a[i % a.byteLength] ^ b[i % b.byteLength];
  }
  return diff === 0;
}

/** 从 Worker 环境变量装载配置；max_streams 一律夹到 [1, 6]。 */
export function loadConfig(env = {}) {
  const allowAll = String(env.TSU_ALLOW_ALL ?? "0") === "1";
  const requested = Number.parseInt(env.TSU_MAX_STREAMS ?? "", 10);
  const maxStreams = Number.isInteger(requested)
    ? Math.max(1, Math.min(MAX_STREAMS_HARD_LIMIT, requested))
    : MAX_STREAMS_HARD_LIMIT;
  const idle = Number.parseInt(env.TSU_STREAM_IDLE_TIMEOUT ?? "", 10);
  return {
    token: String(env.TSU_TOKEN ?? ""),
    allowAll,
    allowHosts: parseAllowHosts(env.TSU_ALLOW_HOSTS ?? DEFAULT_ALLOW_HOSTS.join(",")),
    allowPorts: parseAllowPorts(env.TSU_ALLOW_PORTS ?? DEFAULT_ALLOW_PORTS.join(",")),
    maxStreams,
    path: String(env.TSU_PATH ?? DEFAULT_PATH),
    streamIdleTimeoutMs: Number.isInteger(idle) && idle > 0 ? idle : DEFAULT_STREAM_IDLE_TIMEOUT_MS,
  };
}

/** 白名单/端口双层检查，返回 null 表示放行，否则返回应回的 OPEN_ERR 码。 */
export function checkTarget(host, port, cfg) {
  if (isBlockedHost(host)) return ERR.BLOCKED_TARGET;
  if (!cfg.allowAll && !isHostAllowed(host, cfg.allowHosts)) return ERR.NOT_ALLOWED;
  if (!isPortAllowed(port, cfg.allowPorts)) return ERR.NOT_ALLOWED;
  return null;
}

/**
 * 把平台的建连报错映射成协议错误码。
 * CF 对私网/回环/自有 IP/受限端口会直接拒绝，报错文本形如
 * "cannot connect to the specified address"、"not allowed" 等 → BLOCKED_TARGET。
 */
export function classifyConnectError(error) {
  const message = String(error?.message ?? error ?? "").toLowerCase();
  if (!message) return ERR.CONNECT_FAILED;
  if (/too many|concurrent|simultaneous/.test(message)) return ERR.TOO_MANY_STREAMS;
  if (
    /cannot connect to the specified address|cloudflare|not allowed|disallow|private|loopback|localhost|reserved|\bblocked\b|\bdenied\b|banned/.test(
      message,
    )
  ) {
    return ERR.BLOCKED_TARGET;
  }
  return ERR.CONNECT_FAILED;
}

/* ------------------------------------------------------------------ *
 * Worker 运行时
 * ------------------------------------------------------------------ */

// cloudflare:sockets 只有 workerd 能解析，用动态 import 让 node --test 也能直接
// 复用本文件的纯函数（静态 import 会让 node 在链接阶段就报 ERR_UNSUPPORTED_ESM_URL_SCHEME）。
let connectImpl = null;
async function loadConnect() {
  if (!connectImpl) {
    const mod = await import("cloudflare:sockets");
    connectImpl = mod.connect;
  }
  return connectImpl;
}

const enc = new TextEncoder();
const dec = new TextDecoder();

function randomPayload(size) {
  const out = new Uint8Array(size);
  crypto.getRandomValues(out);
  return out;
}

/** 单条 WS 连接上的会话：管理全部隧道流。 */
class TsuSession {
  constructor(webSocket, cfg) {
    this.ws = webSocket;
    this.cfg = cfg;
    this.streams = new Map();
    this.alive = true;
    this.lastInboundAt = Date.now();
    this.missedPings = 0;
    this.pendingPing = null;
    this.keepaliveTimer = null;
    webSocket.addEventListener("message", (event) => this.onMessage(event));
    webSocket.addEventListener("close", () => this.shutdown());
    webSocket.addEventListener("error", () => this.shutdown());
    this.armKeepalive();
  }

  /* --------------------------- WS 收发 --------------------------- */

  send(opcode, streamId, payload) {
    if (!this.alive || this.ws.readyState !== 1) return false;
    try {
      this.ws.send(encodeFrame(opcode, streamId, payload));
      return true;
    } catch {
      this.shutdown();
      return false;
    }
  }

  sendOpenErr(streamId, code, message) {
    this.send(OP.OPEN_ERR, streamId, encodeOpenErr(code, message ?? ERR_TEXT[code]));
  }

  /** 空闲计时：只在有对端数据流过时才发 PING，避免空连接也刷流量。 */
  armKeepalive() {
    this.keepaliveTimer = setTimeout(() => {
      if (!this.alive) return;
      const idle = Date.now() - this.lastInboundAt;
      if (idle >= PING_INTERVAL_MS) {
        if (this.pendingPing && this.missedPings >= PING_MAX_MISSED) {
          this.shutdown();
          return;
        }
        this.pendingPing = randomPayload(8);
        this.missedPings += 1;
        this.send(OP.PING, 0, this.pendingPing);
      }
      this.armKeepalive();
    }, PING_INTERVAL_MS);
  }

  /* --------------------------- 帧分发 --------------------------- */

  onMessage(event) {
    const data = event.data;
    // 文本帧一律忽略（规范 1：不得当成 DATA）。
    if (typeof data === "string") return;
    this.lastInboundAt = Date.now();
    let frame;
    try {
      frame = decodeFrame(data);
    } catch (error) {
      const streamId = error?.streamId ?? 0;
      if (streamId > 0) this.resetStream(streamId);
      else this.shutdown();
      return;
    }
    const { opcode, streamId, payload } = frame;
    switch (opcode) {
      case OP.OPEN:
        this.handleOpen(streamId, payload);
        break;
      case OP.DATA:
        this.handleData(streamId, payload);
        break;
      case OP.CLOSE:
        this.handleClose(streamId);
        break;
      case OP.RESET:
        // 收到 RESET 立即中止：关 TCP、释放 id，且不再回任何该流的帧（不回 RESET 应答）。
        this.resetStream(streamId, false);
        break;
      case OP.PING:
        this.send(OP.PONG, 0, payload.subarray(0, 32));
        break;
      case OP.PONG:
        this.missedPings = 0;
        this.pendingPing = null;
        break;
      default:
        // 服务端专属帧（OPEN_OK / OPEN_ERR）从客户端发来属于方向错误。
        this.resetStream(streamId);
        break;
    }
  }

  /* --------------------------- OPEN --------------------------- */

  handleOpen(streamId, payload) {
    if (streamId < 3) {
      this.sendOpenErr(streamId, ERR.BAD_REQUEST, "流 id 必须 >= 3");
      return;
    }
    if (this.streams.has(streamId)) {
      this.sendOpenErr(streamId, ERR.BAD_REQUEST, "流 id 已被占用");
      return;
    }
    let address;
    try {
      address = decodeAddress(payload);
    } catch (error) {
      this.sendOpenErr(streamId, ERR.BAD_REQUEST, error?.message ?? "地址非法");
      return;
    }
    // 并发上限：超出立即回 TOO_MANY_STREAMS，不排队（规范 3.3）。
    if (this.streams.size >= this.cfg.maxStreams) {
      this.sendOpenErr(streamId, ERR.TOO_MANY_STREAMS, "本连接并发流已满，请换一条 WS 连接重试");
      return;
    }
    const rejected = checkTarget(address.host, address.port, this.cfg);
    if (rejected !== null) {
      this.sendOpenErr(streamId, rejected, rejected === ERR.BLOCKED_TARGET ? "目标地址被平台禁止" : "目标不在白名单");
      return;
    }
    const stream = {
      id: streamId,
      host: address.host,
      port: address.port,
      state: "opening",
      socket: null,
      reader: null,
      writer: null,
      writeChain: Promise.resolve(),
      preopen: [],
      preopenBytes: 0,
      pendingBytes: 0,
      localClosed: false,
      remoteClosed: false,
      idleTimer: null,
      destroyed: false,
    };
    this.streams.set(streamId, stream);
    this.touchStream(stream);
    void this.openStream(stream);
  }

  async openStream(stream) {
    let socket;
    try {
      const connect = await loadConnect();
      socket = connect({ hostname: stream.host, port: stream.port });
      stream.socket = socket;
      await this.withTimeout(socket.opened, CONNECT_TIMEOUT_MS);
    } catch (error) {
      if (stream.destroyed) return;
      const code = classifyConnectError(error);
      this.sendOpenErr(stream.id, code, `连接目标失败: ${code === ERR.BLOCKED_TARGET ? "blocked" : "unreachable"}`);
      this.disposeStream(stream);
      return;
    }
    if (stream.destroyed) {
      try {
        await socket.close();
      } catch {
        /* 忽略关闭失败 */
      }
      return;
    }
    stream.reader = socket.readable.getReader();
    stream.writer = socket.writable.getWriter();
    stream.state = "open";
    if (!this.send(OP.OPEN_OK, stream.id)) {
      this.disposeStream(stream);
      return;
    }
    // OPEN_OK 之前到达的 DATA 现在按序补发。
    const buffered = stream.preopen;
    stream.preopen = [];
    stream.preopenBytes = 0;
    for (const chunk of buffered) this.writeToSocket(stream, chunk);
    void this.readFromSocket(stream);
    // 目标侧异常关闭时收敛该流。
    socket.closed
      .then(() => {
        if (!stream.destroyed && stream.state === "open") this.resetStream(stream.id, false);
      })
      .catch(() => {
        if (!stream.destroyed) this.resetStream(stream.id, false);
      });
  }

  withTimeout(promise, ms) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new TsuError(ERR.CONNECT_FAILED, "连接超时")), ms);
      promise.then(
        (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        (error) => {
          clearTimeout(timer);
          reject(error);
        },
      );
    });
  }

  /* --------------------------- DATA --------------------------- */

  handleData(streamId, payload) {
    const stream = this.streams.get(streamId);
    if (!stream || stream.destroyed) {
      this.send(OP.RESET, streamId);
      return;
    }
    this.touchStream(stream);
    const chunk = payload;
    if (stream.state !== "open" || !stream.writer) {
      // 客户端抢跑：OPEN_OK 之前先暂存，超过上限直接 RESET，避免无界缓冲。
      if (stream.preopenBytes + chunk.byteLength > PREOPEN_BUFFER_LIMIT) {
        this.send(OP.RESET, streamId);
        this.disposeStream(stream);
        return;
      }
      stream.preopen.push(new Uint8Array(chunk));
      stream.preopenBytes += chunk.byteLength;
      return;
    }
    this.writeToSocket(stream, new Uint8Array(chunk));
  }

  /** 写入 TCP；用每流串行的写链保证顺序，写链本身提供真实背压。 */
  writeToSocket(stream, chunk) {
    stream.writeChain = stream.writeChain
      .then(async () => {
        if (stream.destroyed || !stream.writer) return;
        await stream.writer.write(chunk);
      })
      .catch(() => {
        if (!stream.destroyed) this.resetStream(stream.id, false);
      });
  }

  /* --------------------------- 半关闭 --------------------------- */

  handleClose(streamId) {
    const stream = this.streams.get(streamId);
    if (!stream || stream.destroyed) return;
    // 对 TCP 做 shutdown(SHUT_WR)：关掉写半边，保留读半边（规范 3.5）。
    stream.localClosed = true;
    const finish = async () => {
      try {
        if (stream.writer) await stream.writer.close();
      } catch {
        /* 对端已关闭写方向时忽略 */
      }
      if (stream.remoteClosed) this.disposeStream(stream);
    };
    void finish();
  }

  resetStream(streamId, notify = true) {
    const stream = this.streams.get(streamId);
    if (!stream) return;
    if (notify) this.send(OP.RESET, streamId);
    this.disposeStream(stream);
  }

  disposeStream(stream) {
    if (stream.destroyed) return;
    stream.destroyed = true;
    if (stream.idleTimer) clearTimeout(stream.idleTimer);
    this.streams.delete(stream.id);
    const socket = stream.socket;
    if (socket) {
      try {
        void socket.close();
      } catch {
        /* 已经关掉了 */
      }
    }
    try {
      stream.reader?.cancel();
    } catch {
      /* 忽略 */
    }
  }

  /* --------------------------- TCP→WS 读循环 --------------------------- */

  async readFromSocket(stream) {
    try {
      while (!stream.destroyed && this.alive) {
        await this.waitForSendRoom(stream);
        if (stream.destroyed) return;
        const { value, done } = await stream.reader.read();
        if (done) {
          // 目标侧 EOF：向对端发 CLOSE 表示本方向不再有 DATA（半关闭）。
          stream.remoteClosed = true;
          this.send(OP.CLOSE, stream.id);
          if (stream.localClosed) this.disposeStream(stream);
          return;
        }
        if (!value?.byteLength) continue;
        this.touchStream(stream);
        this.sendData(stream, value);
      }
    } catch {
      if (!stream.destroyed) this.resetStream(stream.id, false);
    }
  }

  /**
   * 背压：待发送队列超过 1 MiB 就暂停调用 reader.read()（规范 3.4）。
   * 说明：workerd 的 WebSocket.send() 是同步 fire-and-forget，没有 await 语义，
   * 因此这里统计「已交给运行时、但尚未走完一轮事件循环确认刷出」的字节数，
   * 再加上运行时自报的待发字节（若该版本暴露 bufferedAmount）；一旦超过阈值
   * 就不再从 TCP 读，保证本实现自身的缓冲有界。
   */
  async waitForSendRoom(stream) {
    while (!stream.destroyed && this.pendingBytes(stream) > BACKPRESSURE_LIMIT) {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  }

  pendingBytes(stream) {
    let buffered = 0;
    try {
      const value = this.ws.bufferedAmount;
      if (typeof value === "number" && Number.isFinite(value)) buffered = value;
    } catch {
      /* 该运行时未实现 bufferedAmount */
    }
    return stream.pendingBytes + buffered;
  }

  /** 超过 32 KiB 的写入必须拆成多个 DATA 帧（规范 4.5/2.1）。 */
  sendData(stream, chunk) {
    const chunks = chunkForFrames(chunk);
    for (const slice of chunks) {
      if (!this.send(OP.DATA, stream.id, slice)) return;
    }
    stream.pendingBytes += chunk.byteLength;
    // 让出一轮事件循环后即认为运行时已经接管这批数据。
    setTimeout(() => {
      stream.pendingBytes = Math.max(0, stream.pendingBytes - chunk.byteLength);
    }, 0);
  }

  /* --------------------------- 空闲超时 / 收尾 --------------------------- */

  touchStream(stream) {
    if (stream.idleTimer) clearTimeout(stream.idleTimer);
    stream.idleTimer = setTimeout(() => {
      if (stream.destroyed) return;
      this.send(OP.RESET, stream.id);
      this.disposeStream(stream);
    }, this.cfg.streamIdleTimeoutMs);
  }

  shutdown() {
    if (!this.alive) return;
    this.alive = false;
    if (this.keepaliveTimer) clearTimeout(this.keepaliveTimer);
    for (const stream of [...this.streams.values()]) this.disposeStream(stream);
    try {
      this.ws.close();
    } catch {
      /* 已经关掉了 */
    }
  }
}

/* ------------------------------------------------------------------ *
 * 入口
 * ------------------------------------------------------------------ */

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

export default {
  async fetch(request, env) {
    const cfg = loadConfig(env);
    const url = new URL(request.url);

    if (url.pathname === "/healthz") {
      return jsonResponse({
        ok: true,
        proto: PROTO,
        allow_all: cfg.allowAll,
        max_streams: cfg.maxStreams,
      });
    }
    if (url.pathname !== cfg.path) {
      return new Response("not found", { status: 404 });
    }
    const upgrade = (request.headers.get("Upgrade") ?? "").toLowerCase();
    if (upgrade !== "websocket") {
      return new Response("upgrade required", { status: 426, headers: { Upgrade: "websocket" } });
    }
    // 令牌错误一律在握手阶段拒绝，不做 Upgrade。
    if (!tokenEquals(extractToken(request, url), cfg.token)) {
      return new Response("unauthorized", {
        status: 401,
        headers: { "www-authenticate": 'Bearer realm="tsu/1"', "cache-control": "no-store" },
      });
    }

    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];
    server.accept();
    new TsuSession(server, cfg);

    const headers = { "cache-control": "no-store" };
    // 客户端要求子协议时必须回显同一个值（规范 1）。
    const offered = (request.headers.get("Sec-WebSocket-Protocol") ?? "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    if (offered.length > 0) headers["Sec-WebSocket-Protocol"] = SUBPROTOCOL;
    return new Response(null, { status: 101, webSocket: client, headers });
  },
};
