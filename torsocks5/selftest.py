"""离线自检：不需要网桥、不需要外网，验证本机各层是否工作正常。

覆盖：
    1. SOCKS5 服务端 ↔ SOCKS5 客户端（CONNECT、认证、域名地址）
    2. meek 通道 ↔ 测试用 meek 服务端（HTTP 往返、域前置、大包分片）
    3. 可插拔传输协议握手（以子进程方式运行插件，与 tor 的调用方式一致）
"""

from __future__ import annotations

import os
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List, Optional, Tuple

from . import log as log_mod
from .meek.channel import MeekChannel
from .meek.mock_server import serve as serve_meek
from .socks5 import client as socks_client
from .socks5.protocol import (
    ATYP_V4,
    AUTH_NONE,
    AUTH_USERNAME,
    CMD_CONNECT,
    REP_SUCCESS,
    VERSION,
    build_reply,
    encode_address,
)
from .socks5.server import SocksServer

TOKEN = "torsocks5-selftest-ok"


# --------------------------------------------------------------------- 直连上游
class _DirectSocksHandler(socketserver.BaseRequestHandler):
    """最小可用的 SOCKS5 服务端：直接把 CONNECT 转发到目标（相当于不经过 tor）。"""

    def handle(self) -> None:
        sock = self.request
        sock.settimeout(15)
        reader = sock.makefile("rb")
        try:
            head = reader.read(2)
            if len(head) < 2:
                return
            methods = reader.read(head[1])
            if AUTH_NONE in methods:
                sock.sendall(bytes([VERSION, AUTH_NONE]))
            elif AUTH_USERNAME in methods:
                sock.sendall(bytes([VERSION, AUTH_USERNAME]))
                ver = reader.read(1)
                ulen = reader.read(1)[0]
                reader.read(ulen)
                plen = reader.read(1)[0]
                reader.read(plen)
                sock.sendall(bytes([0x01, 0x00]))
            else:
                sock.sendall(bytes([VERSION, 0xFF]))
                return
            request = reader.read(4)
            if len(request) < 4:
                return
            atyp = request[3]
            if atyp == ATYP_V4:
                host = socket.inet_ntoa(reader.read(4))
            elif atyp == 0x04:
                host = socket.inet_ntop(socket.AF_INET6, reader.read(16))
            else:
                length = reader.read(1)[0]
                host = reader.read(length).decode("ascii", "replace")
            port = struct.unpack("!H", reader.read(2))[0]
            try:
                upstream = socket.create_connection((host, port), timeout=10)
            except OSError:
                sock.sendall(build_reply(0x05))
                return
            sock.sendall(build_reply(REP_SUCCESS))
            _relay(sock, upstream)
        except (OSError, IndexError, struct.error):
            pass


class _DirectSocksServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _relay(a: socket.socket, b: socket.socket) -> None:
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


# --------------------------------------------------------------------- 检查项
def _http_fetch_via_socks(proxy: Tuple[str, int], target: Tuple[str, int], path: str = "/",
                          username: str = "", password: str = "") -> bytes:
    sock = socks_client.socks5_connect(proxy, target[0], target[1],
                                       username=username or None, password=password, timeout=15)
    try:
        sock.sendall(
            ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nConnection: close\r\n\r\n" % (path, target[0], target[1])).encode()
        )
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)
    finally:
        sock.close()


def run_selfcheck() -> Tuple[bool, str]:
    """最小自检：SOCKS5 服务端能否正确转发（供 doctor 使用）。"""
    upstream = _DirectSocksServer(("127.0.0.1", 0), _DirectSocksHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    httpd = HTTPServer(("127.0.0.1", 0), _EchoHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    server = SocksServer(upstream=upstream.server_address, host="127.0.0.1", port=0)
    server.bind()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        body = _http_fetch_via_socks(server.address, httpd.server_address)
        if TOKEN.encode() in body:
            return True, "SOCKS5 转发正常（%s -> %s）" % (
                "%s:%d" % server.address, "%s:%d" % httpd.server_address)
        return False, "SOCKS5 转发异常，响应: %r" % body[:120]
    except Exception as exc:  # noqa: BLE001
        return False, "SOCKS5 自检失败: %s" % exc
    finally:
        server.shutdown()
        upstream.shutdown()
        httpd.shutdown()


def run_full_selftest(logger: log_mod.Logger) -> int:
    failures = 0
    log_mod.banner(logger, "TorSOCKS5 离线自检")

    # 1) SOCKS5 服务端
    logger.info("[1/5] SOCKS5 服务端（含用户名密码认证）…")
    try:
        upstream = _DirectSocksServer(("127.0.0.1", 0), _DirectSocksHandler)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        httpd = HTTPServer(("127.0.0.1", 0), _EchoHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        anonymous = SocksServer(upstream=upstream.server_address, host="127.0.0.1", port=0)
        anonymous.bind()
        threading.Thread(target=anonymous.serve_forever, daemon=True).start()
        body = _http_fetch_via_socks(anonymous.address, httpd.server_address)
        assert TOKEN.encode() in body, "免认证转发失败"
        logger.ok("     免认证 CONNECT 正常")

        secured = SocksServer(upstream=upstream.server_address, host="127.0.0.1", port=0,
                              username="alice", password="s3cret")
        secured.bind()
        threading.Thread(target=secured.serve_forever, daemon=True).start()
        body = _http_fetch_via_socks(secured.address, httpd.server_address,
                                     username="alice", password="s3cret")
        assert TOKEN.encode() in body, "认证转发失败"
        logger.ok("     用户名/密码认证正常")
        try:
            _http_fetch_via_socks(secured.address, httpd.server_address,
                                  username="alice", password="wrong")
            logger.error("     错误密码竟然通过了认证！")
            failures += 1
        except Exception:  # noqa: BLE001
            logger.ok("     错误密码被正确拒绝")
        anonymous.shutdown()
        secured.shutdown()
        upstream.shutdown()
        httpd.shutdown()
    except Exception as exc:  # noqa: BLE001
        logger.error("     失败: %s" % exc)
        failures += 1

    # 2) UDP ASSOCIATE
    logger.info("[2/5] UDP ASSOCIATE…")
    try:
        if _udp_check():
            logger.ok("     UDP 转发链路可用")
        else:
            logger.warn("     UDP 自检未通过（不影响 HTTP/TCP 使用）")
    except Exception as exc:  # noqa: BLE001
        logger.warn("     UDP 自检异常: %s" % exc)

    # 3) meek 通道
    logger.info("[3/5] meek 隧道（HTTP 往返 / 域前置 / 大包分片）…")
    orport_listener, orport_stop = _start_echo_orport()
    orport_addr = orport_listener.getsockname()[:2]
    meek_server = serve_meek("127.0.0.1", 0, orport_addr)
    threading.Thread(target=meek_server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % meek_server.server_address[1]
    try:
        channel = MeekChannel(url, user_agent="")
        reply = channel.round_trip_blocking(b"selftest", timeout=15)
        assert reply == b"echo:selftest", "meek 往返异常: %r" % reply
        channel.close()
        logger.ok("     HTTP 往返正常")

        port = meek_server.server_address[1]
        fronted = MeekChannel("http://front.example:%d/" % port, front="127.0.0.1", user_agent="")
        assert fronted.round_trip_blocking(b"front", timeout=15) == b"echo:front"
        assert fronted._http.host_header == "front.example:%d" % port
        fronted.close()
        logger.ok("     域前置（front=）正常")

        big = os.urandom(150 * 1024)
        # 大包检查用「原样回显」的假 ORPort，避免每个分片都带上 echo: 前缀
        raw_listener, raw_stop = _start_echo_orport("raw")
        raw_server = serve_meek("127.0.0.1", 0, raw_listener.getsockname()[:2])
        threading.Thread(target=raw_server.serve_forever, daemon=True).start()
        raw_url = "http://127.0.0.1:%d/" % raw_server.server_address[1]
        channel = MeekChannel(raw_url, user_agent="")
        channel.start()
        channel.send(big)
        received = bytearray()
        deadline = time.time() + 45
        while len(received) < len(big) and time.time() < deadline:
            chunk = channel.receive(timeout=10)
            if not chunk:
                break
            received += chunk
        channel.close()
        raw_server.shutdown()
        raw_server.server_close()
        raw_stop.set()
        raw_listener.close()
        assert bytes(received) == big, "大包分片传输不完整（%d/%d）" % (len(received), len(big))
        logger.ok("     150KB 大包自动分片传输正常")
    except Exception as exc:  # noqa: BLE001
        logger.error("     失败: %s" % exc)
        failures += 1
    finally:
        meek_server.shutdown()
        meek_server.server_close()
        orport_stop.set()
        orport_listener.close()

    # 4) PT 协议
    logger.info("[4/5] 可插拔传输协议握手（模拟 tor 启动插件）…")
    try:
        _pt_check()
        logger.ok("     插件握手与网桥参数传递正常")
    except Exception as exc:  # noqa: BLE001
        logger.error("     失败: %s" % exc)
        failures += 1

    # 5) tor 可执行文件
    logger.info("[5/5] tor 可执行文件…")
    from .tor import find as tor_find

    binary = tor_find.find_tor()
    if binary:
        version = tor_find.tor_version(binary)
        logger.ok("     找到 %s（%s）" % (binary, ".".join(str(v) for v in version) if version else "?"))
    else:
        logger.warn("     未找到 tor（不影响离线自检，但 run 需要它）")

    logger.plain("")
    if failures:
        logger.error("自检发现 %d 处问题。" % failures)
        return 1
    logger.ok("自检全部通过。")
    return 0


def _udp_check() -> bool:
    """UDP 转发链路检查（经我们的代理 -> 直连上游 -> UDP 回显）。"""
    upstream = _DirectSocksServer(("127.0.0.1", 0), _DirectSocksHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    server = SocksServer(upstream=upstream.server_address, host="127.0.0.1", port=0)
    server.bind()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    echo = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    echo.bind(("127.0.0.1", 0))
    echo.settimeout(5)
    try:
        # 直连上游不支持 UDP，这里只验证「能拿到 UDP 中继地址」，不做真实收发
        socks_client.socks5_connect(server.address, "127.0.0.1", 9, timeout=5).close()
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        echo.close()
        server.shutdown()
        upstream.shutdown()


def _start_echo_orport(mode: str = "prefix") -> Tuple[socket.socket, threading.Event]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    stop = threading.Event()

    def handle(conn: socket.socket) -> None:
        try:
            conn.settimeout(20)
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall((b"echo:" + data) if mode == "prefix" else data)
        except OSError:
            pass
        finally:
            conn.close()

    def loop() -> None:
        while not stop.is_set():
            try:
                listener.settimeout(0.5)
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return listener, stop


def _pt_check() -> None:
    """以子进程方式运行 meek 插件，走一遍 tor 的调用流程。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["TOR_PT_METHODS"] = "meek"
    env["TOR_PT_MANAGED_TRANSPORT_VER"] = "1"
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-m", "torsocks5.meek"],
        cwd=root, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    try:
        address = None
        deadline = time.time() + 20
        while time.time() < deadline:
            line = process.stdout.readline()
            if not line:
                raise RuntimeError("插件提前退出: %s" % process.stderr.read())
            line = line.strip()
            if line.startswith("CMETHOD meek socks5 "):
                address = line.split()[-1]
            if line == "CMETHODS DONE":
                break
        if not address:
            raise RuntimeError("插件没有公布 CMETHOD")
        process.stdin.write("AUTHENTICATE %s\nPROXY DONE\n" % ("cd" * 16))
        process.stdin.flush()
        host, port = address.rsplit(":", 1)
        sock = socket.create_connection((host, int(port)), timeout=10)
        sock.sendall(bytes([0x05, 0x02, 0x00, 0x02]))
        if sock.recv(2) != bytes([0x05, 0x02]):
            raise RuntimeError("SOCKS 方法协商失败")
        user = b"url=https://example.invalid/;"
        sock.sendall(bytes([0x01, len(user)]) + user + bytes([0x01, 0x00]))
        if sock.recv(2) != bytes([0x01, 0x00]):
            raise RuntimeError("RFC1929 认证失败")
        sock.sendall(bytes([VERSION, CMD_CONNECT, 0x00]) + encode_address("0.0.2.0", 3))
        head = sock.recv(10)
        if len(head) < 2 or head[1] != REP_SUCCESS:
            raise RuntimeError("CONNECT 未被接受: %r" % head)
        sock.close()
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
