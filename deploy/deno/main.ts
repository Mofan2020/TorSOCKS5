/**
 * TSU/1 隧道中继的 Deno 形态（可部署到 Deno Deploy）。
 *
 * 严格实现 docs/tunnel-protocol.md：一个 WebSocket 二进制消息 = 恰好一个 TSU 帧，
 * opcode / stream id 见协议第 2 节，地址编码复用 SOCKS5 的 ATYP 编号。
 * 出网连接用 Deno.connect，载体用 Deno.serve + Deno.upgradeWebSocket，
 * 全程只依赖 Deno 内置 API，没有任何远程 import（CI 无需联网）。
 *
 * 本文件会被 main_test.ts 直接 import，所以纯逻辑（帧编解码、地址编解码、
 * 白名单匹配、配置解析、TCP 转发循环）全部是无副作用的导出函数，
 * 只有 `import.meta.main` 成立时才真正开始监听。
 */

/** WS 子协议标识，握手时客户端声明、中继必须回显这个值（协议 §1）。 */
export const PROTOCOL = "tsu.v1";

/** 协议名，用于 /healthz 的 `proto` 字段与启动日志。 */
export const PROTO_NAME = "tsu/1";

/** 默认路径。 */
export const DEFAULT_PATH = "/tsu";

// ---------------------------------------------------------------------------
// opcode（协议 §2.1）
// ---------------------------------------------------------------------------

export const OP_OPEN = 0x01;
export const OP_OPEN_OK = 0x02;
export const OP_OPEN_ERR = 0x03;
export const OP_DATA = 0x04;
export const OP_CLOSE = 0x05;
export const OP_RESET = 0x06;
export const OP_PING = 0x07;
export const OP_PONG = 0x08;

/** opcode → 名称，仅用于日志与测试断言。 */
export const OPCODE_NAMES: Record<number, string> = {
  [OP_OPEN]: "OPEN",
  [OP_OPEN_OK]: "OPEN_OK",
  [OP_OPEN_ERR]: "OPEN_ERR",
  [OP_DATA]: "DATA",
  [OP_CLOSE]: "CLOSE",
  [OP_RESET]: "RESET",
  [OP_PING]: "PING",
  [OP_PONG]: "PONG",
};

/** 是否是协议定义过的 opcode；未定义的要按协议用 RESET 回应。 */
export function isKnownOpcode(opcode: number): boolean {
  return OPCODE_NAMES[opcode] !== undefined;
}

// ---------------------------------------------------------------------------
// OPEN_ERR 错误码（协议 §2.3）
// ---------------------------------------------------------------------------

export const ERR_NOT_ALLOWED = 0x01;
export const ERR_CONNECT_FAILED = 0x02;
export const ERR_TOO_MANY_STREAMS = 0x03;
export const ERR_BAD_REQUEST = 0x04;
export const ERR_BLOCKED_TARGET = 0x05;
export const ERR_UNAUTHORIZED = 0x06;

export const ERROR_NAMES: Record<number, string> = {
  [ERR_NOT_ALLOWED]: "NOT_ALLOWED",
  [ERR_CONNECT_FAILED]: "CONNECT_FAILED",
  [ERR_TOO_MANY_STREAMS]: "TOO_MANY_STREAMS",
  [ERR_BAD_REQUEST]: "BAD_REQUEST",
  [ERR_BLOCKED_TARGET]: "BLOCKED_TARGET",
  [ERR_UNAUTHORIZED]: "UNAUTHORIZED",
};

// ---------------------------------------------------------------------------
// 地址编码（协议 §2.2），与 SOCKS5 的 ATYP 编号一致
// ---------------------------------------------------------------------------

export const ATYP_V4 = 0x01;
export const ATYP_DOMAIN = 0x03;
export const ATYP_V6 = 0x04;

/** 帧头长度：1 字节 opcode + 4 字节 stream id。 */
export const FRAME_HEADER_BYTES = 5;

/** 单个 WS 消息的推荐分片大小。 */
export const DATA_CHUNK_BYTES = 32 * 1024;

/** 接收方必须能接受的最大消息大小，超过即 `RESET`。 */
export const MAX_FRAME_BYTES = 1024 * 1024;

/** 帧格式非法时抛出，携带一个 OPEN_ERR 错误码。 */
export class ProtocolError extends Error {
  readonly code: number;

  constructor(message: string, code: number = ERR_BAD_REQUEST) {
    super(message);
    this.name = "ProtocolError";
    this.code = code;
  }
}

/** 一个 TSU 帧。 */
export interface Frame {
  opcode: number;
  streamId: number;
  payload: Uint8Array;
}

/**
 * 编码一个 TSU 帧：`[opcode:1][stream id:4 大端][payload]`。
 */
export function encodeFrame(
  opcode: number,
  streamId: number,
  payload: Uint8Array = new Uint8Array(0),
): Uint8Array {
  const out = new Uint8Array(FRAME_HEADER_BYTES + payload.byteLength);
  out[0] = opcode & 0xff;
  out[1] = (streamId >>> 24) & 0xff;
  out[2] = (streamId >>> 16) & 0xff;
  out[3] = (streamId >>> 8) & 0xff;
  out[4] = streamId & 0xff;
  out.set(payload, FRAME_HEADER_BYTES);
  return out;
}

/**
 * 解码一个 TSU 帧。结构性非法（长度不足 / 超长）时抛 ProtocolError；
 * 未定义的 opcode 不算结构错误，由调用方按协议用 RESET 回应。
 */
export function decodeFrame(data: Uint8Array): Frame {
  if (data.byteLength > MAX_FRAME_BYTES) {
    throw new ProtocolError(`消息超过 ${MAX_FRAME_BYTES} 字节`, ERR_BAD_REQUEST);
  }
  if (data.byteLength < FRAME_HEADER_BYTES) {
    throw new ProtocolError("帧长度不足 5 字节", ERR_BAD_REQUEST);
  }
  const opcode = data[0];
  const streamId = ((data[1] << 24) | (data[2] << 16) | (data[3] << 8) | data[4]) >>> 0;
  return { opcode, streamId, payload: data.subarray(FRAME_HEADER_BYTES) };
}

/** 编码 `OPEN_ERR` 的 payload：`[code:1][utf8 消息]`。 */
export function encodeOpenErr(code: number, message = ""): Uint8Array {
  const text = new TextEncoder().encode(message);
  const out = new Uint8Array(1 + text.byteLength);
  out[0] = code & 0xff;
  out.set(text, 1);
  return out;
}

/** 解析 `OPEN_ERR` 的 payload，返回 `{code, message}`。 */
export function decodeOpenErr(payload: Uint8Array): { code: number; message: string } {
  if (payload.byteLength < 1) {
    throw new ProtocolError("OPEN_ERR 缺少错误码", ERR_BAD_REQUEST);
  }
  return {
    code: payload[0],
    message: new TextDecoder().decode(payload.subarray(1)),
  };
}

/** 规范化主机名：去空白、转小写、去结尾点、去 IPv6 字面量的方括号。 */
export function normalizeHost(host: string): string {
  let h = host.trim().toLowerCase();
  if (h.startsWith("[") && h.endsWith("]")) h = h.slice(1, -1);
  while (h.endsWith(".")) h = h.slice(0, -1);
  return h;
}

/** 解析点分十进制的 IPv4，失败返回 null。 */
export function parseIPv4(host: string): Uint8Array | null {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host.trim());
  if (!m) return null;
  const out = new Uint8Array(4);
  for (let i = 0; i < 4; i++) {
    const v = Number(m[i + 1]);
    if (v > 255) return null;
    out[i] = v;
  }
  return out;
}

/** 解析 IPv6（支持 `::` 压缩与内嵌 IPv4），失败返回 null。 */
export function parseIPv6(host: string): Uint8Array | null {
  let s = host.trim();
  if (s.startsWith("[") && s.endsWith("]")) s = s.slice(1, -1);
  const dot = s.lastIndexOf(".");
  if (dot >= 0) {
    // 内嵌 IPv4（如 ::ffff:127.0.0.1）转成两个 hextet
    const colonBeforeDot = s.lastIndexOf(":", dot);
    if (colonBeforeDot < 0) return null;
    const v4 = parseIPv4(s.slice(colonBeforeDot + 1));
    if (!v4) return null;
    const hi = ((v4[0] << 8) | v4[1]).toString(16);
    const lo = ((v4[2] << 8) | v4[3]).toString(16);
    s = `${s.slice(0, colonBeforeDot + 1)}${hi}:${lo}`;
  }
  const parts = s.split("::");
  if (parts.length > 2) return null;
  const head = parts[0] ? parts[0].split(":") : [];
  const tail = parts.length === 2 && parts[1] ? parts[1].split(":") : [];
  if (parts.length === 1 && head.length !== 8) return null;
  const fill = 8 - head.length - tail.length;
  if (parts.length === 2 && fill < 1) return null;
  const hextets = [...head, ...new Array<string>(Math.max(fill, 0)).fill("0"), ...tail];
  if (hextets.length !== 8) return null;
  const out = new Uint8Array(16);
  for (let i = 0; i < 8; i++) {
    if (!/^[0-9a-f]{1,4}$/.test(hextets[i])) return null;
    const v = parseInt(hextets[i], 16);
    out[i * 2] = (v >> 8) & 0xff;
    out[i * 2 + 1] = v & 0xff;
  }
  return out;
}

/**
 * 编码 `OPEN` 的 payload：`[atyp:1][addr][port:2 大端]`。
 * 域名原样透传给中继解析，不做本地 DNS。
 */
export function encodeAddress(host: string, port: number): Uint8Array {
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new ProtocolError(`端口非法: ${port}`, ERR_BAD_REQUEST);
  }
  const h = normalizeHost(host);
  const v4 = parseIPv4(h);
  if (v4) return concat([new Uint8Array([ATYP_V4]), v4, portBytes(port)]);

  const v6 = parseIPv6(h);
  if (v6) return concat([new Uint8Array([ATYP_V6]), v6, portBytes(port)]);

  const raw = encodeDomain(h);
  if (raw) {
    if (raw.byteLength > 255) throw new ProtocolError("域名过长", ERR_BAD_REQUEST);
    return concat([new Uint8Array([ATYP_DOMAIN, raw.byteLength]), raw, portBytes(port)]);
  }

  throw new ProtocolError(`无法编码的地址: ${host}`, ERR_BAD_REQUEST);
}

/** 解析 `OPEN` 的 payload，返回 `{host, port}`。 */
export function decodeAddress(payload: Uint8Array): { host: string; port: number } {
  if (payload.byteLength < 3) {
    throw new ProtocolError("OPEN payload 过短", ERR_BAD_REQUEST);
  }
  const atyp = payload[0];
  let host: string;
  let offset: number;
  if (atyp === ATYP_V4) {
    if (payload.byteLength < 7) throw new ProtocolError("IPv4 地址不完整", ERR_BAD_REQUEST);
    host = Array.from(payload.subarray(1, 5)).join(".");
    offset = 5;
  } else if (atyp === ATYP_V6) {
    if (payload.byteLength < 19) throw new ProtocolError("IPv6 地址不完整", ERR_BAD_REQUEST);
    host = formatIPv6(payload.subarray(1, 17));
    offset = 17;
  } else if (atyp === ATYP_DOMAIN) {
    const len = payload[1];
    if (len === 0) throw new ProtocolError("域名长度为 0", ERR_BAD_REQUEST);
    if (payload.byteLength < 2 + len + 2) throw new ProtocolError("域名不完整", ERR_BAD_REQUEST);
    host = new TextDecoder("utf-8", { fatal: false }).decode(payload.subarray(2, 2 + len));
    offset = 2 + len;
  } else {
    throw new ProtocolError(`未知地址类型 0x${atyp.toString(16)}`, ERR_BAD_REQUEST);
  }
  if (payload.byteLength < offset + 2) throw new ProtocolError("端口缺失", ERR_BAD_REQUEST);
  const port = (payload[offset] << 8) | payload[offset + 1];
  if (port === 0) throw new ProtocolError("端口为 0", ERR_BAD_REQUEST);
  return { host, port };
}

/** 把 16 字节 IPv6 转成带 `::` 压缩的字符串。 */
export function formatIPv6(bytes: Uint8Array): string {
  const groups: number[] = [];
  for (let i = 0; i < 16; i += 2) groups.push((bytes[i] << 8) | bytes[i + 1]);
  let bestStart = -1;
  let bestLen = 0;
  let curStart = -1;
  let curLen = 0;
  for (let i = 0; i < 8; i++) {
    if (groups[i] === 0) {
      if (curStart < 0) curStart = i;
      curLen++;
      if (curLen > bestLen) {
        bestLen = curLen;
        bestStart = curStart;
      }
    } else {
      curStart = -1;
      curLen = 0;
    }
  }
  if (bestLen < 2) {
    return groups.map((g) => g.toString(16)).join(":");
  }
  const head = groups.slice(0, bestStart).map((g) => g.toString(16)).join(":");
  const tail = groups.slice(bestStart + bestLen).map((g) => g.toString(16)).join(":");
  return `${head}::${tail}`;
}

function portBytes(port: number): Uint8Array {
  return new Uint8Array([(port >> 8) & 0xff, port & 0xff]);
}

function concat(chunks: Uint8Array[]): Uint8Array {
  const total = chunks.reduce((n, c) => n + c.byteLength, 0);
  const out = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.byteLength;
  }
  return out;
}

/** 域名按 ASCII/IDNA 编码；含非 ASCII 时退回 UTF-8（由中继侧解析）。 */
function encodeDomain(host: string): Uint8Array | null {
  if (!host) return null;
  // 含 : [ ] 与空白的一律不是域名（IPv6 字面量已在上游处理）
  if (/[:[\]\s]/.test(host)) return null;
  if (/^[a-z0-9_-]+(\.[a-z0-9_-]+)*$/i.test(host)) return new TextEncoder().encode(host);
  if (!hasNonAscii(host)) return null;
  try {
    return new TextEncoder().encode(new URL(`http://${host}`).hostname);
  } catch {
    return null;
  }
}

/** 是否含非 ASCII 字符（等价于 Python 侧的 `any(ord(c) > 127)`）。 */
function hasNonAscii(text: string): boolean {
  for (let i = 0; i < text.length; i++) {
    if (text.charCodeAt(i) > 0x7f) return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// 目标过滤（协议 §3.2）
// ---------------------------------------------------------------------------

function isPrivateV4(b: Uint8Array): boolean {
  if (b[0] === 0) return true; // 0.0.0.0/8
  if (b[0] === 10) return true; // 10/8
  if (b[0] === 127) return true; // 127/8
  if (b[0] === 169 && b[1] === 254) return true; // 169.254/16
  if (b[0] === 172 && b[1] >= 16 && b[1] <= 31) return true; // 172.16/12
  if (b[0] === 192 && b[1] === 168) return true; // 192.168/16
  return false;
}

/**
 * 目标是否属于私有 / 回环 / 链路本地地址（协议 §3.2 的禁用列表）。
 * 域名不在本地解析，交给平台自身策略；`localhost` 这类回环域名直接判禁。
 */
export function isBlockedTarget(host: string): boolean {
  const h = normalizeHost(host);
  if (!h) return true;
  if (h === "localhost" || h.endsWith(".localhost")) return true;

  const v4 = parseIPv4(h);
  if (v4) return isPrivateV4(v4);

  const v6 = parseIPv6(h);
  if (v6) {
    let mapped = true;
    for (let i = 0; i < 10; i++) if (v6[i] !== 0) mapped = false;
    if (mapped && v6[10] === 0xff && v6[11] === 0xff) return isPrivateV4(v6.subarray(12));
    const allZero = v6.every((b) => b === 0);
    if (allZero) return true; // ::
    if (v6.subarray(0, 15).every((b) => b === 0) && v6[15] === 1) return true; // ::1
    if ((v6[0] & 0xfe) === 0xfc) return true; // fc00::/7
    if (v6[0] === 0xfe && (v6[1] & 0xc0) === 0x80) return true; // fe80::/10
    return false;
  }
  return false;
}

/** 单条白名单条目与主机名的后缀匹配（协议 §3.2）。 */
export function hostMatches(pattern: string, host: string): boolean {
  const p = normalizeHost(pattern);
  const h = normalizeHost(host);
  if (!p || !h) return false;
  if (p.startsWith(".")) {
    // 以 . 开头的条目只匹配子域
    return h.length > p.length && h.endsWith(p);
  }
  return h === p || h.endsWith(`.${p}`);
}

/** 主机名是否命中白名单列表。 */
export function isHostAllowed(host: string, hosts: readonly string[]): boolean {
  return hosts.some((p) => hostMatches(p, host));
}

/** 解析 `TSU_ALLOW_PORTS` 形式的端口列表。 */
export function parsePortList(spec: string): Set<number> {
  const out = new Set<number>();
  for (const item of spec.split(",")) {
    const trimmed = item.trim();
    if (!trimmed) continue;
    const port = Number(trimmed);
    if (Number.isInteger(port) && port > 0 && port <= 65535) out.add(port);
  }
  return out;
}

/** 端口是否允许。 */
export function isPortAllowed(port: number, ports: ReadonlySet<number>): boolean {
  return ports.has(port);
}

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------

export interface RelayConfig {
  /** 监听地址与端口；Deno Deploy 会忽略这两个值，本地跑时用 TSU_HOST/TSU_PORT。 */
  hostname: string;
  port: number;
  /** WS 路径，默认 /tsu。 */
  path: string;
  /** 鉴权令牌；空串表示未配置（仅本地调试，启动时会告警）。 */
  token: string;
  /** 白名单开关：true = TSU_ALLOW_ALL=1，放行任意目标。 */
  allowAll: boolean;
  /** 是否允许私网 / 回环目标；默认与规范一致为 false。 */
  allowPrivate: boolean;
  /** 允许的目标主机后缀列表（TSU_ALLOW_HOSTS）。 */
  hosts: string[];
  /** 允许的目标端口集合（TSU_ALLOW_PORTS）。 */
  ports: Set<number>;
  /** 单条 WS 连接的并发流上限（TSU_MAX_STREAMS）。 */
  maxStreams: number;
  /** 单个 WS 消息的推荐分片大小。 */
  dataChunkBytes: number;
  /** 允许接收的最大消息大小。 */
  maxFrameBytes: number;
  /** 待发送队列高水位，超过则暂停读 TCP。 */
  drainHighWater: number;
  /** 待发送队列低水位，回落到该值以下才恢复读 TCP。 */
  drainLowWater: number;
  /** 出网 TCP 连接超时。 */
  connectTimeoutMs: number;
  /** 单条流空闲超时。 */
  streamIdleTimeoutMs: number;
  /** 空闲多久发 PING。 */
  idlePingMs: number;
  /** 连续多少次 PING 无 PONG 判定链路失效。 */
  maxMissedPongs: number;
  /** 巡检 / 保活的定时器间隔。 */
  sweepIntervalMs: number;
}

/** 读取环境变量里的数字，非法或缺省时用默认值。 */
function envInt(env: Record<string, string | undefined>, key: string, fallback: number): number {
  const raw = env[key];
  if (raw === undefined || raw.trim() === "") return fallback;
  const value = Number(raw);
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

/** 由环境变量算出中继配置；纯函数，便于测试直接喂一个假 env。 */
export function parseConfig(env: Record<string, string | undefined> = {}): RelayConfig {
  const hostsRaw = env.TSU_ALLOW_HOSTS ?? "github.com,githubusercontent.com";
  const hosts = hostsRaw
    .split(",")
    .map((h) => normalizeHost(h))
    .filter((h) => h.length > 0);
  const allowAll = env.TSU_ALLOW_ALL === "1";
  // 私网目标默认拒绝；仅当显式开启 TSU_ALLOW_ALL/TSU_ALLOW_PRIVATE 时才放开（本地调试用）。
  const allowPrivate = allowAll || env.TSU_ALLOW_PRIVATE === "1";

  return {
    hostname: env.TSU_HOST ?? "0.0.0.0",
    port: envInt(env, "TSU_PORT", envInt(env, "PORT", 9052)),
    path: env.TSU_PATH ?? DEFAULT_PATH,
    token: env.TSU_TOKEN ?? "",
    allowAll,
    allowPrivate,
    hosts,
    ports: parsePortList(env.TSU_ALLOW_PORTS ?? "443,80,22,9418"),
    maxStreams: envInt(env, "TSU_MAX_STREAMS", 64),
    dataChunkBytes: DATA_CHUNK_BYTES,
    maxFrameBytes: MAX_FRAME_BYTES,
    drainHighWater: 1024 * 1024,
    drainLowWater: 512 * 1024,
    connectTimeoutMs: envInt(env, "TSU_CONNECT_TIMEOUT_MS", 15000),
    streamIdleTimeoutMs: envInt(env, "TSU_STREAM_IDLE_MS", 300_000),
    idlePingMs: envInt(env, "TSU_IDLE_PING_MS", 30_000),
    maxMissedPongs: envInt(env, "TSU_MAX_MISSED_PONGS", 2),
    sweepIntervalMs: envInt(env, "TSU_SWEEP_INTERVAL_MS", 5000),
  };
}

/** 令牌比较：长度不等即失败，但都逐字节跑完，避免明显的时序差异。 */
export function tokenMatches(provided: string | null, expected: string): boolean {
  if (expected === "") return true; // 未配置令牌 → 鉴权关闭（仅本地调试）
  if (provided === null) return false;
  let diff = provided.length ^ expected.length;
  const n = Math.max(provided.length, expected.length);
  for (let i = 0; i < n; i++) {
    diff |= (provided.charCodeAt(i) || 0) ^ (expected.charCodeAt(i) || 0);
  }
  return diff === 0;
}

/** 从请求里取令牌：优先 `?token=`，其次 `Authorization: Bearer`。 */
export function extractToken(req: Request): string | null {
  const url = new URL(req.url);
  const query = url.searchParams.get("token");
  if (query !== null && query !== "") return query;
  const auth = req.headers.get("authorization");
  if (auth) {
    const m = /^Bearer\s+(.+)$/i.exec(auth.trim());
    if (m) return m[1].trim();
  }
  return null;
}

// ---------------------------------------------------------------------------
// 统计（只记计数与错误类型，绝不记录目标域名或内容）
// ---------------------------------------------------------------------------

export interface RelayStats {
  connections: number;
  connectionsClosed: number;
  streamsOpened: number;
  streamsRejected: number;
  bytesToTarget: number;
  bytesFromTarget: number;
  errors: Record<string, number>;
}

export function createStats(): RelayStats {
  return {
    connections: 0,
    connectionsClosed: 0,
    streamsOpened: 0,
    streamsRejected: 0,
    bytesToTarget: 0,
    bytesFromTarget: 0,
    errors: {},
  };
}

function countError(stats: RelayStats, name: string): void {
  stats.errors[name] = (stats.errors[name] ?? 0) + 1;
}

// ---------------------------------------------------------------------------
// 可注入的 TCP 转发循环
// ---------------------------------------------------------------------------

/** 字节来源（真实实现是 Deno.TcpConn，测试里可以是假 socket）。 */
export interface ByteSource {
  read(p: Uint8Array): Promise<number | null>;
  closeWrite?(): void | Promise<void>;
  close(): void;
}

/** 帧接收端（真实实现是 WebSocket，测试里可以是假 sink）。 */
export interface FrameSink {
  send(opcode: number, streamId: number, payload?: Uint8Array): void;
  /** 等待待发送队列回落到低水位以下。 */
  waitDrain(): Promise<void>;
}

export interface PumpOptions {
  /** 单个 DATA 帧的最大字节数。 */
  chunkBytes: number;
  /** 对端已 EOF 时回调（用于发 CLOSE）。 */
  onEof: () => void;
  /** 读取出错时回调（用于 RESET）。 */
  onError: (err: unknown) => void;
  /** 每个 DATA 帧的回调，便于统计。 */
  onData?: (bytes: number) => void;
  /** 每次循环前的取消检查；返回 true 表示停止泵送。 */
  isCancelled?: () => boolean;
}

/**
 * TCP → TSU 的转发循环：读 TCP，切成 ≤chunkBytes 的 DATA 帧发出去。
 * 发之前先 `waitDrain()`，实现「待发送队列超过 1 MiB 就暂停读 TCP」的背压要求。
 */
export async function pumpTcpToFrames(
  src: ByteSource,
  sink: FrameSink,
  streamId: number,
  opts: PumpOptions,
): Promise<void> {
  const buf = new Uint8Array(opts.chunkBytes);
  try {
    while (true) {
      if (opts.isCancelled?.()) return;
      await sink.waitDrain(); // 背压：队列高水位时在这里等
      if (opts.isCancelled?.()) return;
      const n = await src.read(buf);
      if (n === null) {
        opts.onEof();
        return;
      }
      if (n <= 0) continue;
      for (let off = 0; off < n; off += opts.chunkBytes) {
        const end = Math.min(off + opts.chunkBytes, n);
        sink.send(OP_DATA, streamId, buf.slice(off, end));
      }
      opts.onData?.(n);
    }
  } catch (err) {
    if (!opts.isCancelled?.()) opts.onError(err);
  }
}

// ---------------------------------------------------------------------------
// WebSocket 发送端（带背压水位）
// ---------------------------------------------------------------------------

class WsSink implements FrameSink {
  private closed = false;

  constructor(
    private readonly socket: WebSocket,
    private readonly cfg: RelayConfig,
  ) {}

  send(opcode: number, streamId: number, payload: Uint8Array = new Uint8Array(0)): void {
    if (this.closed || this.socket.readyState !== WebSocket.OPEN) return;
    try {
      this.socket.send(encodeFrame(opcode, streamId, payload));
    } catch {
      this.closed = true;
    }
  }

  /** Deno 的 WS 无法暂停读，只能靠 bufferedAmount 判断待发送队列；TCP 侧据此暂停。 */
  async waitDrain(): Promise<void> {
    while (
      !this.closed &&
      this.socket.readyState === WebSocket.OPEN &&
      this.socket.bufferedAmount > this.cfg.drainHighWater
    ) {
      await delay(5);
    }
  }

  markClosed(): void {
    this.closed = true;
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// 单条 WS 连接上的流状态
// ---------------------------------------------------------------------------

interface StreamState {
  id: number;
  conn: Deno.TcpConn;
  /** WS 侧已发 CLOSE（不再收该流 DATA），对应「对 TCP 做 shutdown(SHUT_WR)」。 */
  clientClosed: boolean;
  /** TCP 侧已 EOF，已向 WS 发过 CLOSE。 */
  serverClosed: boolean;
  /** 已对 TCP 执行过 closeWrite。 */
  writeShut: boolean;
  /** WS → TCP 的待写队列。 */
  pending: Uint8Array[];
  pendingBytes: number;
  writing: boolean;
  /** 最近一次成功收发数据的时刻，用于空闲超时。 */
  lastActive: number;
  dead: boolean;
}

/**
 * 一条 WS 连接：解析 TSU 帧、管理流、维护保活与空闲超时。
 */
export class RelayConnection {
  private readonly sink: WsSink;
  private readonly streams = new Map<number, StreamState>();
  /** 正在 Deno.connect 中、已占用并发配额的流。 */
  private pendingOpens = 0;
  private sweepTimer: ReturnType<typeof setInterval> | null = null;
  private lastInbound = Date.now();
  private pingOutstanding = false;
  private pingSentAt = 0;
  private missedPongs = 0;
  private closed = false;

  constructor(
    private readonly socket: WebSocket,
    private readonly cfg: RelayConfig,
    private readonly stats: RelayStats,
  ) {
    this.sink = new WsSink(socket, cfg);
  }

  /** 绑定 WebSocket 事件。 */
  attach(): void {
    try {
      // Deno 的 binaryType 默认是 blob，协议要求二进制帧，这里固定为 arraybuffer
      this.socket.binaryType = "arraybuffer";
    } catch {
      /* 个别运行时只读，忽略 */
    }
    this.stats.connections++;
    this.lastInbound = Date.now();

    this.socket.onmessage = (ev: MessageEvent) => {
      void this.onMessage(ev);
    };
    this.socket.onclose = () => this.destroy("socket_closed");
    this.socket.onerror = () => {
      countError(this.stats, "socket_error");
      this.destroy("socket_error");
    };

    this.sweepTimer = setInterval(() => this.sweep(), this.cfg.sweepIntervalMs);
    // 定时器不该拖住进程退出（Deno Deploy 无此 API 时忽略）
    try {
      const unref = (Deno as unknown as {
        unrefTimer?: (id: ReturnType<typeof setInterval>) => void;
      }).unrefTimer;
      if (unref) unref(this.sweepTimer);
    } catch {
      /* 忽略 */
    }
  }

  /** 收 WS 消息：一个消息 = 一个帧；文本帧一律忽略。 */
  private async onMessage(ev: MessageEvent): Promise<void> {
    if (this.closed) return;
    this.lastInbound = Date.now();
    const data = ev.data;
    let bytes: Uint8Array;
    if (typeof data === "string") return; // 文本帧不是 TSU 帧
    if (data instanceof Uint8Array) bytes = data;
    else if (data instanceof ArrayBuffer) bytes = new Uint8Array(data);
    else if (typeof Blob !== "undefined" && data instanceof Blob) {
      bytes = new Uint8Array(await data.arrayBuffer());
    } else {
      countError(this.stats, "bad_message_type");
      return;
    }
    if (bytes.byteLength > this.cfg.maxFrameBytes) {
      // 超过接收上限：无法信任 opcode，按协议对该流 RESET（流 id 不可知时关闭连接）
      countError(this.stats, "oversized_message");
      this.closeSocket(1009, "message too big");
      return;
    }
    let frame: Frame;
    try {
      frame = decodeFrame(bytes);
    } catch (err) {
      countError(this.stats, "bad_frame");
      const code = err instanceof ProtocolError ? err.code : ERR_BAD_REQUEST;
      this.sendOpenErr(0, code, "bad frame");
      return;
    }
    this.handle(frame);
  }

  private handle(frame: Frame): void {
    if (!isKnownOpcode(frame.opcode)) {
      // 未定义的 opcode：流 id 为 0 时直接关闭连接，否则 RESET 该流
      countError(this.stats, "unknown_opcode");
      if (frame.streamId === 0) this.closeSocket(1002, "unknown opcode");
      else this.abortStream(frame.streamId, true);
      return;
    }
    switch (frame.opcode) {
      case OP_OPEN:
        void this.openStream(frame);
        return;
      case OP_DATA:
        this.onData(frame);
        return;
      case OP_CLOSE:
        this.onClose(frame.streamId);
        return;
      case OP_RESET:
        this.abortStream(frame.streamId, false);
        return;
      case OP_PING:
        // 收到 PING 必须原样回显 payload
        this.sink.send(OP_PONG, frame.streamId, frame.payload);
        return;
      case OP_PONG:
        this.missedPongs = 0;
        this.pingOutstanding = false;
        return;
      default:
        // OPEN_OK / OPEN_ERR 由客户端发来时属于协议误用
        if (frame.streamId === 0) this.closeSocket(1002, "unexpected control opcode");
        else this.abortStream(frame.streamId, true);
    }
  }

  /** 处理 OPEN：过滤 → 连接 → OPEN_OK / OPEN_ERR。 */
  private async openStream(frame: Frame): Promise<void> {
    const sid = frame.streamId;
    if (sid === 0) {
      countError(this.stats, "open_stream_id_zero");
      this.sendOpenErr(0, ERR_BAD_REQUEST, "stream id 0 reserved");
      return;
    }
    if (this.streams.has(sid)) {
      countError(this.stats, "duplicate_stream_id");
      this.sendOpenErr(sid, ERR_BAD_REQUEST, "duplicate stream id");
      return;
    }
    if (this.streams.size + this.pendingOpens >= this.cfg.maxStreams) {
      // 超限直接拒绝，不排队（协议 §3.3）
      this.stats.streamsRejected++;
      countError(this.stats, "too_many_streams");
      this.sendOpenErr(sid, ERR_TOO_MANY_STREAMS, "too many streams");
      return;
    }

    let target: { host: string; port: number };
    try {
      target = decodeAddress(frame.payload);
    } catch (err) {
      this.stats.streamsRejected++;
      countError(this.stats, "bad_open");
      this.sendOpenErr(sid, err instanceof ProtocolError ? err.code : ERR_BAD_REQUEST, "bad open");
      return;
    }

    // 先判私网/回环/链路本地，再判白名单
    if (!this.cfg.allowPrivate && isBlockedTarget(target.host)) {
      this.stats.streamsRejected++;
      countError(this.stats, "blocked_target");
      this.sendOpenErr(sid, ERR_BLOCKED_TARGET, "blocked target");
      return;
    }
    if (!this.cfg.allowAll) {
      if (!isHostAllowed(target.host, this.cfg.hosts)) {
        this.stats.streamsRejected++;
        countError(this.stats, "not_allowed");
        this.sendOpenErr(sid, ERR_NOT_ALLOWED, "host not allowed");
        return;
      }
      if (!isPortAllowed(target.port, this.cfg.ports)) {
        this.stats.streamsRejected++;
        countError(this.stats, "not_allowed");
        this.sendOpenErr(sid, ERR_NOT_ALLOWED, "port not allowed");
        return;
      }
    }

    this.pendingOpens++;
    let conn: Deno.TcpConn;
    try {
      conn = await connectTarget(target.host, target.port, this.cfg.connectTimeoutMs);
    } catch (err) {
      this.pendingOpens--;
      this.stats.streamsRejected++;
      countError(this.stats, "connect_failed");
      this.sendOpenErr(sid, ERR_CONNECT_FAILED, connectErrorText(err));
      return;
    }
    this.pendingOpens--;

    if (this.closed || this.socket.readyState !== WebSocket.OPEN) {
      try {
        conn.close();
      } catch {
        /* 忽略 */
      }
      return;
    }

    const state: StreamState = {
      id: sid,
      conn,
      clientClosed: false,
      serverClosed: false,
      writeShut: false,
      pending: [],
      pendingBytes: 0,
      writing: false,
      lastActive: Date.now(),
      dead: false,
    };
    this.streams.set(sid, state);
    this.stats.streamsOpened++;
    this.sink.send(OP_OPEN_OK, sid);

    void pumpTcpToFrames(conn, this.sink, sid, {
      chunkBytes: this.cfg.dataChunkBytes,
      isCancelled: () => state.dead || this.closed,
      onEof: () => this.onTcpEof(state),
      onError: () => {
        countError(this.stats, "tcp_read_error");
        this.abortStream(sid, true);
      },
      onData: (n) => {
        state.lastActive = Date.now();
        this.stats.bytesFromTarget += n;
      },
    });
  }

  /** WS → TCP 的 DATA：入队后异步写出，队列过大时按背压 RESET 该流。 */
  private onData(frame: Frame): void {
    const st = this.streams.get(frame.streamId);
    if (!st || st.dead) {
      // 未知流上的 DATA 按协议中止该流
      this.abortStream(frame.streamId, true);
      return;
    }
    if (frame.payload.byteLength === 0) return;
    if (st.clientClosed) return; // 已半关闭后不再接受数据
    st.pending.push(frame.payload.slice());
    st.pendingBytes += frame.payload.byteLength;
    st.lastActive = Date.now();
    if (st.pendingBytes > this.cfg.drainHighWater) {
      // WS 侧无法暂停读取，只能用 RESET 兜住无界缓冲（见 README「与规范的偏差」）
      countError(this.stats, "ws_backpressure_reset");
      this.abortStream(st.id, true);
      return;
    }
    this.flushWrites(st);
  }

  private flushWrites(st: StreamState): void {
    if (st.writing || st.dead) return;
    st.writing = true;
    void (async () => {
      try {
        while (st.pending.length > 0 && !st.dead) {
          const chunk = st.pending.shift() as Uint8Array;
          st.pendingBytes -= chunk.byteLength;
          let off = 0;
          while (off < chunk.byteLength) {
            off += await st.conn.write(chunk.subarray(off));
          }
          this.stats.bytesToTarget += chunk.byteLength;
          st.lastActive = Date.now();
        }
      } catch {
        countError(this.stats, "tcp_write_error");
        this.abortStream(st.id, true);
      } finally {
        st.writing = false;
        // 只有客户端已发过 CLOSE 时才真正 shutdown(SHUT_WR)，否则会误关正常连接
        if (!st.dead && st.pending.length === 0 && st.clientClosed) {
          await this.shutdownWrite(st);
          this.maybeFinalize(st);
        }
      }
    })();
  }

  /** 收到 CLOSE：半关闭，只 shutdown(SHUT_WR)，不关闭整条流。 */
  private onClose(sid: number): void {
    const st = this.streams.get(sid);
    if (!st || st.dead) return;
    st.clientClosed = true;
    st.lastActive = Date.now();
    if (!st.writing && st.pending.length === 0) {
      void this.shutdownWrite(st).then(() => this.maybeFinalize(st));
    }
  }

  private async shutdownWrite(st: StreamState): Promise<void> {
    if (st.writeShut || st.dead) return;
    st.writeShut = true;
    try {
      const fn = st.conn as unknown as { closeWrite?: () => void | Promise<void> };
      if (typeof fn.closeWrite === "function") await fn.closeWrite();
      else st.conn.close();
    } catch {
      /* 对端已关闭，忽略 */
    }
  }

  /** TCP 侧 EOF：向客户端发 CLOSE（本方向不再发 DATA），流本身保留到双向结束。 */
  private onTcpEof(st: StreamState): void {
    if (st.dead || st.serverClosed) return;
    st.serverClosed = true;
    this.sink.send(OP_CLOSE, st.id);
    this.maybeFinalize(st);
  }

  /** 双向都结束才回收流与 stream id。 */
  private maybeFinalize(st: StreamState): void {
    if (st.dead) return;
    if (st.clientClosed && st.serverClosed && st.pending.length === 0) {
      this.finalize(st);
    }
  }

  /** RESET：立即关闭 TCP 并释放 stream id，不再发该流任何帧。 */
  private abortStream(sid: number, notify: boolean): void {
    const st = this.streams.get(sid);
    if (!st) {
      if (notify && sid !== 0) this.sink.send(OP_RESET, sid);
      return;
    }
    if (notify) this.sink.send(OP_RESET, sid);
    this.finalize(st);
  }

  private finalize(st: StreamState): void {
    if (st.dead) return;
    st.dead = true;
    this.streams.delete(st.id);
    st.pending.length = 0;
    st.pendingBytes = 0;
    try {
      st.conn.close();
    } catch {
      /* 已关闭 */
    }
  }

  /** 巡检：流空闲超时 → RESET；连接空闲 → PING；连续无 PONG → 关闭。 */
  private sweep(): void {
    if (this.closed) return;
    const now = Date.now();
    for (const st of Array.from(this.streams.values())) {
      if (now - st.lastActive > this.cfg.streamIdleTimeoutMs) {
        countError(this.stats, "stream_idle_timeout");
        this.sink.send(OP_RESET, st.id);
        this.finalize(st);
      }
    }
    if (now - this.lastInbound < this.cfg.idlePingMs) return;
    if (!this.pingOutstanding) {
      this.pingOutstanding = true;
      this.pingSentAt = now;
      this.sink.send(OP_PING, 0, randomBytes(8));
      return;
    }
    if (now - this.pingSentAt < this.cfg.idlePingMs) return;
    this.missedPongs++;
    if (this.missedPongs >= this.cfg.maxMissedPongs) {
      countError(this.stats, "keepalive_lost");
      this.closeSocket(1011, "keepalive lost");
      return;
    }
    this.pingSentAt = now;
    this.sink.send(OP_PING, 0, randomBytes(8));
  }

  private sendOpenErr(sid: number, code: number, message: string): void {
    this.sink.send(OP_OPEN_ERR, sid, encodeOpenErr(code, message));
  }

  private closeSocket(code: number, reason: string): void {
    try {
      this.socket.close(code, reason);
    } catch {
      /* 已关闭 */
    }
    this.destroy(reason);
  }

  /** 连接级清理：所有流按 RESET 语义回收。 */
  destroy(reason: string): void {
    if (this.closed) return;
    this.closed = true;
    if (this.sweepTimer !== null) {
      clearInterval(this.sweepTimer);
      this.sweepTimer = null;
    }
    for (const st of Array.from(this.streams.values())) this.finalize(st);
    this.streams.clear();
    this.sink.markClosed();
    this.stats.connectionsClosed++;
    // 只记计数与原因，不记目标域名或内容
    console.log(
      `[tsu] 连接结束 reason=${reason} opened=${this.stats.streamsOpened} closed=${this.stats.connectionsClosed}`,
    );
  }
}

/** 带超时的出网连接；超时后若连接仍成功建立会立即关闭，避免泄漏。 */
export async function connectTarget(
  host: string,
  port: number,
  timeoutMs: number,
): Promise<Deno.TcpConn> {
  const pending = Deno.connect({ hostname: host, port });
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error("connect timeout")), timeoutMs);
  });
  try {
    const conn = await Promise.race([pending, timeout]);
    return conn as Deno.TcpConn;
  } catch (err) {
    pending.then((c) => {
      try {
        c.close();
      } catch {
        /* 忽略 */
      }
    }).catch(() => {});
    throw err;
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

/** 连接失败原因转成给客户端的一句话（不含敏感信息）。 */
function connectErrorText(err: unknown): string {
  const text = err instanceof Error ? err.message : String(err);
  if (/timeout/i.test(text)) return "connect timeout";
  return "connect failed";
}

function randomBytes(n: number): Uint8Array {
  const out = new Uint8Array(n);
  crypto.getRandomValues(out);
  return out;
}

// ---------------------------------------------------------------------------
// HTTP 处理
// ---------------------------------------------------------------------------

/**
 * 协商子协议：客户端带了 `Sec-WebSocket-Protocol` 就必须含 `tsu.v1`（协议 §1）。
 * 返回 "tsu.v1" 表示需要回显，null 表示客户端没带（容忍），undefined 表示拒绝。
 */
export function pickSubprotocol(header: string | null): string | null | undefined {
  if (header === null || header.trim() === "") return null;
  const wanted = header.split(",").map((p) => p.trim()).filter((p) => p.length > 0);
  return wanted.includes(PROTOCOL) ? PROTOCOL : undefined;
}

/** 生成 HTTP 处理器；测试里可以直接拿它去 Deno.serve。 */
export function createHandler(
  cfg: RelayConfig,
  stats: RelayStats = createStats(),
): (req: Request) => Response {
  return (req: Request): Response => {
    const url = new URL(req.url);

    // 连通性探测不需要令牌
    if (url.pathname === "/healthz") {
      return jsonResponse({
        ok: true,
        proto: PROTO_NAME,
        allow_all: cfg.allowAll,
        max_streams: cfg.maxStreams,
      });
    }

    if (url.pathname !== cfg.path) {
      return new Response("not found", { status: 404 });
    }

    const upgrade = (req.headers.get("upgrade") ?? "").toLowerCase();
    if (upgrade !== "websocket") {
      return new Response("upgrade required", { status: 426 });
    }

    // 鉴权必须在升级之前完成，失败返回 401（不做 Upgrade）
    if (!tokenMatches(extractToken(req), cfg.token)) {
      countError(stats, "unauthorized");
      return new Response(
        JSON.stringify({ ok: false, error: "unauthorized", code: ERR_UNAUTHORIZED }),
        {
          status: 401,
          headers: { "content-type": "application/json; charset=utf-8" },
        },
      );
    }

    const protocol = pickSubprotocol(req.headers.get("sec-websocket-protocol"));
    if (protocol === undefined) {
      return new Response("unsupported websocket subprotocol", { status: 400 });
    }

    const { socket, response } = Deno.upgradeWebSocket(
      req,
      protocol ? { protocol } : {},
    );
    const conn = new RelayConnection(socket, cfg, stats);
    conn.attach();
    return response;
  };
}

/** 启动监听；返回 server 与一个在真正 listen 后 resolve 的地址 promise。 */
export function serveRelay(
  cfg: RelayConfig,
  stats: RelayStats = createStats(),
): { server: Deno.HttpServer; listening: Promise<{ hostname: string; port: number }> } {
  let resolveAddr: (addr: { hostname: string; port: number }) => void = () => {};
  const listening = new Promise<{ hostname: string; port: number }>((resolve) => {
    resolveAddr = resolve;
  });
  const server = Deno.serve(
    {
      hostname: cfg.hostname,
      port: cfg.port,
      onListen: (addr) => resolveAddr({ hostname: addr.hostname, port: addr.port }),
    },
    createHandler(cfg, stats),
  );
  return { server, listening };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

// ---------------------------------------------------------------------------
// 入口
// ---------------------------------------------------------------------------

if (import.meta.main) {
  const cfg = parseConfig(Deno.env.toObject());
  const { listening } = serveRelay(cfg);
  const addr = await listening;
  console.log(
    `[tsu] 中继已启动 ${addr.hostname}:${addr.port}${cfg.path} proto=${PROTO_NAME} ` +
      `allow_all=${cfg.allowAll} allow_private=${cfg.allowPrivate} max_streams=${cfg.maxStreams}`,
  );
  if (cfg.token === "") {
    console.warn("[tsu] 未设置 TSU_TOKEN，鉴权已关闭；仅建议本地调试使用");
  }
  if (cfg.allowPrivate) {
    console.warn("[tsu] 已放开私网/回环目标限制，仅建议本地调试使用");
  }
}
