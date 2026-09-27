"""可插拔传输（PT）协议层的测试：用「假 tor」驱动 meek 插件子进程。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.test_meek import _OrPort  # noqa: E402
from torsocks5.meek.mock_server import serve  # noqa: E402


class FakeTor:
    """模拟 tor：启动插件、读 CMETHOD、连 SOCKS5、带上网桥参数。"""

    def __init__(self, extra_args=(), env_extra=None, methods="meek", managed_ver="1"):
        env = dict(os.environ)
        env["TOR_PT_METHODS"] = methods
        env["TOR_PT_MANAGED_TRANSPORT_VER"] = managed_ver
        env["TOR_PT_STATE_LOCATION"] = "/tmp"
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        if env_extra:
            env.update(env_extra)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "torsocks5.meek", "-v", *extra_args],
            cwd=ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.methods = {}
        self.lines = []
        self.stderr_lines = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._handshake()

    def _drain_stderr(self):
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip())

    def _readline(self, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError(
                    "插件提前退出，stderr:\n%s" % "\n".join(self.stderr_lines)
                )
            line = line.rstrip()
            self.lines.append(line)
            return line
        raise AssertionError("等待插件输出超时")

    def _handshake(self):
        while True:
            line = self._readline()
            if line.startswith("CMETHODS DONE"):
                break
            if line.startswith("CMETHOD "):
                _, name, socks, addr = line.split()
                assert socks == "socks5", socks
                self.methods[name] = addr
        self.proc.stdin.write("AUTHENTICATE %s\n" % ("ab" * 16))
        self.proc.stdin.write("PROXY DONE\n")
        self.proc.stdin.flush()

    def connect(self, args: str):
        """用 SOCKS5 + username/password 传参，模拟 tor 连接插件。"""
        host, port = self.methods["meek"].rsplit(":", 1)
        sock = socket.create_connection((host, int(port)), timeout=15)
        sock.sendall(bytes([0x05, 0x02, 0x00, 0x02]))
        reply = sock.recv(2)
        assert reply == bytes([0x05, 0x02]), reply
        user = b"url=https://x.example/;"
        pwd = args.encode()
        sock.sendall(bytes([0x01, len(user)]) + user + bytes([len(pwd)]) + pwd)
        assert sock.recv(2) == bytes([0x01, 0x00])
        sock.sendall(bytes([0x05, 0x01, 0x00, 0x01]) + socket.inet_aton("0.0.2.0") + (3).to_bytes(2, "big"))
        head = sock.recv(10)
        assert head[1] == 0x00, "CONNECT 被拒绝: %r" % head
        return sock

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class PluggableTransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.orport = _OrPort("prefix")
        cls.server = serve("127.0.0.1", 0, cls.orport.addr)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.orport.stop()

    def test_handshake_and_data(self):
        tor = FakeTor()
        try:
            self.assertIn("VERSION 1", tor.lines)
            self.assertIn("CMETHODS DONE", tor.lines)
            self.assertTrue(any(line.startswith("CMETHOD meek socks5 127.0.0.1:") for line in tor.lines))
            url = "http://127.0.0.1:%d/" % self.port
            sock = tor.connect("url=%s" % url)
            sock.settimeout(20)
            sock.sendall(b"ping-through-pt")
            self.assertEqual(sock.recv(4096), b"echo:ping-through-pt")
            sock.close()
        finally:
            tor.close()

    def test_multiple_method_names(self):
        """同时声明 meek / meek_lite / meek_azure，网桥行怎么写都能用。"""
        tor = FakeTor(methods="meek_lite,meek,meek_azure,obfs4")
        try:
            self.assertEqual(set(tor.methods), {"meek", "meek_lite", "meek_azure"})
            self.assertTrue(any(line.startswith("CMETHOD-ERROR obfs4") for line in tor.lines))
            host, port = tor.methods["meek_lite"].rsplit(":", 1)
            self.assertTrue(port.isdigit())
        finally:
            tor.close()

    def test_missing_url_rejected(self):
        tor = FakeTor()
        try:
            host, port = tor.methods["meek"].rsplit(":", 1)
            sock = socket.create_connection((host, int(port)), timeout=15)
            try:
                sock.sendall(bytes([0x05, 0x02, 0x00, 0x02]))
                self.assertEqual(sock.recv(2), bytes([0x05, 0x02]))
                # 只给 front=，没有 url= -> 必须拒绝
                user = b"front=only.example"
                sock.sendall(bytes([0x01, len(user)]) + user + bytes([0x01, 0x00]))
                self.assertEqual(sock.recv(2), bytes([0x01, 0x00]))
                sock.sendall(
                    bytes([0x05, 0x01, 0x00, 0x01])
                    + socket.inet_aton("0.0.2.0")
                    + (3).to_bytes(2, "big")
                )
                head = sock.recv(10)
                self.assertNotEqual(head[1], 0x00, "缺少可用 url 时应拒绝")
            finally:
                sock.close()
        finally:
            tor.close()

    def test_default_url_from_command_line(self):
        tor = FakeTor(extra_args=["--url=http://127.0.0.1:%d/" % self.port])
        try:
            host, port = tor.methods["meek"].rsplit(":", 1)
            sock = socket.create_connection((host, int(port)), timeout=15)
            sock.sendall(bytes([0x05, 0x01, 0x00]))  # 无认证（无参数）
            self.assertEqual(sock.recv(2), bytes([0x05, 0x00]))
            sock.sendall(
                bytes([0x05, 0x01, 0x00, 0x01]) + socket.inet_aton("0.0.2.0") + (3).to_bytes(2, "big")
            )
            self.assertEqual(sock.recv(10)[1], 0x00)
            sock.sendall(b"no-auth-path")
            self.assertEqual(sock.recv(4096), b"echo:no-auth-path")
            sock.close()
        finally:
            tor.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
