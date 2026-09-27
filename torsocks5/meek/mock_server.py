"""一个 Python 实现的 meek 服务端（测试用）。

这是官方 ``meek-server`` 的等价实现（协议行为一致：``X-Session-Id`` 分流、
请求体直接喂给 ORPort、等待 10ms 把回程数据作为 200 响应返回），
用于离线端到端测试与 ``torsocks5 selftest``，不需要 Go 也不需要真实网桥。
"""

from __future__ import annotations

import argparse
import http.server
import socket
import socketserver
import ssl
import threading
import time
from typing import Dict, Optional, Tuple

MAX_PAYLOAD = 0x10000
TURNAROUND_TIMEOUT = 0.010
SESSION_STALENESS = 120.0


class _Sessions:
    def __init__(self, orport: Tuple[str, int]) -> None:
        self.orport = orport
        self.lock = threading.Lock()
        self.map: Dict[str, Tuple[socket.socket, float]] = {}

    def get(self, session_id: str) -> socket.socket:
        with self.lock:
            entry = self.map.get(session_id)
            if entry is None:
                sock = socket.create_connection(self.orport, timeout=5.0)
                sock.settimeout(None)
                entry = (sock, time.time())
                self.map[session_id] = entry
            else:
                entry = (entry[0], time.time())
                self.map[session_id] = entry
            return entry[0]

    def reap(self) -> None:
        now = time.time()
        with self.lock:
            for key in [k for k, v in self.map.items() if now - v[1] > SESSION_STALENESS]:
                try:
                    self.map[key][0].close()
                except OSError:
                    pass
                del self.map[key]


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "meek-server-python/1.0"
    sys_version = ""

    # 屏蔽 BaseHTTPRequestHandler 的 stderr 日志
    def log_message(self, fmt, *args):  # noqa: A003
        if getattr(self.server, "verbose", False):
            print("[meek-server] " + (fmt % args), flush=True)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") not in ("", "/"):
            self.send_error(404)
            return
        body = b"I'm just a happy little web server.\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        session_id = self.headers.get("X-Session-Id", "")
        if len(session_id) < 8:
            self.send_error(400, "Bad request.\n")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "Bad request.\n")
            return
        if length > MAX_PAYLOAD + 1:
            self.send_error(400, "Bad request.\n")
            return
        body = self.rfile.read(length) if length else b""
        if len(body) > MAX_PAYLOAD:
            self.send_error(400, "Bad request.\n")
            return

        sessions: _Sessions = self.server.sessions  # type: ignore[attr-defined]
        try:
            orconn = sessions.get(session_id)
        except OSError as exc:
            self.send_error(500, "cannot reach ORPort: %s\n" % exc)
            return
        try:
            orconn.sendall(body)
        except OSError:
            self.send_error(500, "writing to ORPort failed\n")
            return

        orconn.settimeout(TURNAROUND_TIMEOUT)
        try:
            data = orconn.recv(MAX_PAYLOAD)
        except socket.timeout:
            data = b""
        except OSError:
            self.send_error(500, "reading from ORPort failed\n")
            return
        finally:
            orconn.settimeout(None)

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)


class MeekTestServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, orport, verbose: bool = False, certfile=None, keyfile=None):
        super().__init__(addr, _Handler)
        self.sessions = _Sessions(orport)
        self.verbose = verbose
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
        if certfile and keyfile:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile, keyfile)
            self.socket = context.wrap_socket(self.socket, server_side=True)

    def _reap_loop(self) -> None:
        while True:
            time.sleep(SESSION_STALENESS / 2)
            self.sessions.reap()


def serve(
    host: str = "127.0.0.1",
    port: int = 0,
    orport: Tuple[str, int] = ("127.0.0.1", 1),
    verbose: bool = False,
    certfile: Optional[str] = None,
    keyfile: Optional[str] = None,
) -> MeekTestServer:
    return MeekTestServer((host, port), orport, verbose=verbose, certfile=certfile, keyfile=keyfile)


def main() -> int:
    parser = argparse.ArgumentParser(description="测试用 meek 服务端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--orport", default="127.0.0.1:1", help="模拟网桥的 ORPort")
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    host, _, port = args.orport.rpartition(":")
    server = serve(
        args.host, args.port, (host, int(port)), args.verbose, args.cert, args.key
    )
    print("meek 测试服务端监听 %s:%d -> ORPort %s" % (args.host, server.server_address[1], args.orport))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
