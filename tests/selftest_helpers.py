"""测试与自检共用的辅助代码（直连 SOCKS5 上游、HTTP 回显服务器）。"""

from __future__ import annotations

import socket
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = "torsocks5-selftest-ok"


class DirectSocksHandler(socketserver.BaseRequestHandler):
    """最小 SOCKS5 服务端：把 CONNECT 直接转发到目标（相当于不经过 tor）。"""

    def handle(self) -> None:
        sock = self.request
        sock.settimeout(15)
        reader = sock.makefile("rb")
        try:
            head = reader.read(2)
            if len(head) < 2:
                return
            methods = reader.read(head[1])
            if 0x00 in methods:
                sock.sendall(bytes([0x05, 0x00]))
            elif 0x02 in methods:
                sock.sendall(bytes([0x05, 0x02]))
                reader.read(1)
                ulen = reader.read(1)[0]
                reader.read(ulen)
                plen = reader.read(1)[0]
                reader.read(plen)
                sock.sendall(bytes([0x01, 0x00]))
            else:
                sock.sendall(bytes([0x05, 0xFF]))
                return
            request = reader.read(4)
            if len(request) < 4:
                return
            atyp = request[3]
            if atyp == 0x01:
                host = socket.inet_ntoa(reader.read(4))
            elif atyp == 0x04:
                host = socket.inet_ntop(socket.AF_INET6, reader.read(16))
            else:
                length = reader.read(1)[0]
                host = reader.read(length).decode("ascii", "replace")
            port = struct.unpack("!H", reader.read(2))[0]
            if request[1] != 0x01:  # 只支持 CONNECT
                sock.sendall(bytes([0x05, 0x07, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
                return
            try:
                upstream = socket.create_connection((host, port), timeout=10)
            except OSError:
                sock.sendall(bytes([0x05, 0x05, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
                return
            sock.sendall(bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
            relay(sock, upstream)
        except (OSError, IndexError, struct.error):
            pass


class DirectSocksServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def relay(a: socket.socket, b: socket.socket) -> None:
    done = threading.Event()

    def pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while not done.is_set():
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            done.set()
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threads = [
        threading.Thread(target=pump, args=(a, b), daemon=True),
        threading.Thread(target=pump, args=(b, a), daemon=True),
    ]
    for thread in threads:
        thread.start()
    while not done.is_set():
        done.wait(0.2)
    for thread in threads:
        thread.join(timeout=1.0)
    for sock in (a, b):
        try:
            sock.close()
        except OSError:
            pass


class _EchoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        body = TOKEN.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def start_http_echo(host: str = "127.0.0.1") -> HTTPServer:
    import socket as _socket

    if ":" in host:
        class _V6Server(HTTPServer):
            address_family = _socket.AF_INET6

        server = _V6Server((host, 0), _EchoHandler)
    else:
        server = HTTPServer((host, 0), _EchoHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
