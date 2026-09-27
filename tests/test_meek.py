"""meek 传输的离线端到端测试。

链路：MeekChannel → HTTP → mock meek-server → 假 ORPort
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from torsocks5.meek.channel import MeekChannel, gen_session_id  # noqa: E402
from torsocks5.meek.mock_server import serve  # noqa: E402
from torsocks5.meek.socks_server import parse_pt_args  # noqa: E402


class _OrPort(threading.Thread):
    """模拟网桥的 ORPort：把收到的字节按 ``mode`` 处理后返回。"""

    def __init__(self, mode: str = "prefix") -> None:
        super().__init__(daemon=True)
        self.mode = mode
        self.received = bytearray()
        self.lock = threading.Lock()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self._stop = threading.Event()
        self.start()

    @property
    def addr(self):
        return self.listener.getsockname()[:2]

    def stop(self):
        self._stop.set()
        try:
            self.listener.close()
        except OSError:
            pass

    def run(self):
        while not self._stop.is_set():
            try:
                self.listener.settimeout(0.5)
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(20)
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                with self.lock:
                    self.received += data
                conn.sendall((b"echo:" + data) if self.mode == "prefix" else data)
        except OSError:
            pass
        finally:
            conn.close()


def _self_signed_cert(directory: str):
    cert = os.path.join(directory, "cert.pem")
    key = os.path.join(directory, "key.pem")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", key, "-out", cert, "-days", "2", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,DNS:front.example,IP:127.0.0.1",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cert, key


class MeekChannelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.orport = _OrPort("prefix")
        cls.server = serve("127.0.0.1", 0, cls.orport.addr)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base_url = "http://127.0.0.1:%d/" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.orport.stop()

    def test_round_trip_plain(self):
        channel = MeekChannel(self.base_url, user_agent="")
        try:
            payload = b"hello meek " + os.urandom(8).hex().encode()
            reply = channel.round_trip_blocking(payload, timeout=15)
            self.assertEqual(reply, b"echo:" + payload)
            self.assertGreaterEqual(channel.polls, 1)
        finally:
            channel.close()

    def test_domain_fronting(self):
        """带 front= 时，连接目标与 Host 头必须不同。"""
        port = self.server.server_address[1]
        channel = MeekChannel(
            "http://meek-bridge.example:%d/path" % port,
            front="127.0.0.1",
            user_agent="",
        )
        try:
            reply = channel.round_trip_blocking(b"fronted", timeout=15)
            self.assertEqual(reply, b"echo:fronted")
            self.assertEqual(channel._http.host_header, "meek-bridge.example:%d" % port)
            self.assertEqual(channel._http.dial_host, "127.0.0.1")
            self.assertEqual(channel._http.path, "/path")
        finally:
            channel.close()

    def test_streaming_both_directions(self):
        """连续多次请求复用同一条连接（keep-alive）。"""
        channel = MeekChannel(self.base_url, user_agent="")
        try:
            channel.start()
            for i in range(5):
                channel.send(b"ping-%d" % i)
            got = b""
            deadline = time.time() + 20
            while got.count(b"echo:") < 5 and time.time() < deadline:
                chunk = channel.receive(timeout=5)
                self.assertTrue(chunk, "通道意外结束")
                got += chunk
            for i in range(5):
                self.assertIn(b"echo:ping-%d" % i, got)
        finally:
            channel.close()


class MeekLargePayloadTest(unittest.TestCase):
    """超过单次 65536 字节上限的数据必须被正确分片传输。"""

    def setUp(self):
        self.orport = _OrPort("raw")
        self.server = serve("127.0.0.1", 0, self.orport.addr)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.orport.stop()

    def test_large_payload(self):
        channel = MeekChannel(self.url, user_agent="")
        try:
            payload = os.urandom(200 * 1024)
            channel.start()
            channel.send(payload)
            got = bytearray()
            deadline = time.time() + 60
            while len(got) < len(payload) and time.time() < deadline:
                chunk = channel.receive(timeout=10)
                self.assertTrue(chunk, "通道意外结束")
                got += chunk
            self.assertEqual(bytes(got), payload)
            self.assertGreater(channel.polls, 1, "大数据应被拆成多次请求")
        finally:
            channel.close()


class MeekTlsTest(unittest.TestCase):
    """https + front：SNI 与证书校验都基于 front 域名。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="torsocks5-tls-")
        self.cert, self.key = _self_signed_cert(self.tmpdir)
        self.orport = _OrPort("prefix")
        self.server = serve(
            "127.0.0.1", 0, self.orport.addr, certfile=self.cert, keyfile=self.key
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.orport.stop()

    def test_tls_fronted(self):
        import ssl

        channel = MeekChannel(
            "https://front.example:%d/" % self.port, front="localhost", user_agent=""
        )
        ctx = ssl.create_default_context(cafile=self.cert)
        original = channel._http._ensure_socket

        def ensure():
            sock = channel._http._sock
            if sock is not None:
                return sock
            raw = socket.create_connection((channel._http.dial_host, channel._http.port), timeout=10)
            wrapped = ctx.wrap_socket(raw, server_hostname=channel._http.dial_host)
            wrapped.settimeout(10)
            channel._http._sock = wrapped
            channel._http._rfile = wrapped.makefile("rb", buffering=64 * 1024)
            return wrapped

        channel._http._ensure_socket = ensure
        self.assertTrue(callable(original))
        try:
            reply = channel.round_trip_blocking(b"tls-ok", timeout=20)
            self.assertEqual(reply, b"echo:tls-ok")
        finally:
            channel.close()


class SessionIdTest(unittest.TestCase):
    def test_session_id_shape(self):
        sid = gen_session_id()
        self.assertNotIn("=", sid)
        self.assertGreaterEqual(len(sid), 8)
        self.assertLessEqual(len(sid), 11)

    def test_session_ids_unique(self):
        ids = {gen_session_id() for _ in range(500)}
        self.assertEqual(len(ids), 500)


class ParseArgsTest(unittest.TestCase):
    def test_semicolon_form(self):
        args = parse_pt_args("url=https://x.example/;front=a.example")
        self.assertEqual(args["url"], "https://x.example/")
        self.assertEqual(args["front"], "a.example")

    def test_space_form(self):
        args = parse_pt_args("url=https://x.example/ front=a.example utls=HelloFirefox_105")
        self.assertEqual(args["url"], "https://x.example/")
        self.assertEqual(args["front"], "a.example")
        self.assertEqual(args["utls"], "HelloFirefox_105")

    def test_legacy_cdnfronting(self):
        args = parse_pt_args("url=https://ajax.aspnetcdn.com/;CDNFronting=ajax.aspnetcdn.com")
        self.assertEqual(args["front"], "ajax.aspnetcdn.com")

    def test_concatenated_username_password(self):
        # tor 可能把参数拆到 username / password 两段，goptlib 直接拼接后解析
        args = parse_pt_args("url=https://x.example/;front=a.example;utls=HelloChrome_102")
        self.assertEqual(args["url"], "https://x.example/")
        self.assertEqual(args["front"], "a.example")
        self.assertEqual(args["utls"], "HelloChrome_102")

    def test_empty(self):
        self.assertEqual(parse_pt_args(""), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
