"""SOCKS5 服务端 / 客户端 / 网桥 / 配置的单元测试。"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from torsocks5 import bridges as bridges_mod  # noqa: E402
from torsocks5 import config as config_mod  # noqa: E402
from torsocks5.socks5 import client as socks_client  # noqa: E402
from torsocks5.socks5.protocol import SocksError, decode_address, encode_address  # noqa: E402
from torsocks5.socks5.server import AccessList, SocksServer  # noqa: E402
from tests.selftest_helpers import DirectSocksHandler, DirectSocksServer, start_http_echo  # noqa: E402


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read(self, size: int) -> bytes:
        chunk = self.data[self.pos : self.pos + size]
        self.pos += len(chunk)
        return chunk


class AddressCodecTest(unittest.TestCase):
    def test_ipv4_roundtrip(self):
        raw = encode_address("1.2.3.4", 8080)
        self.assertEqual(raw[0], 0x01)
        host, port = decode_address(_Reader(raw))
        self.assertEqual((host, port), ("1.2.3.4", 8080))

    def test_ipv6_roundtrip(self):
        raw = encode_address("2001:db8::1", 443)
        self.assertEqual(raw[0], 0x04)
        host, port = decode_address(_Reader(raw))
        self.assertEqual(host, "2001:db8::1")
        self.assertEqual(port, 443)

    def test_domain_roundtrip(self):
        raw = encode_address("example.com", 80)
        self.assertEqual(raw[0], 0x03)
        host, port = decode_address(_Reader(raw))
        self.assertEqual((host, port), ("example.com", 80))

    def test_idna_domain(self):
        raw = encode_address("例え.jp", 80)
        host, _ = decode_address(_Reader(raw))
        self.assertTrue(host)

    def test_unknown_atype(self):
        with self.assertRaises(SocksError):
            decode_address(_Reader(bytes([0x09, 1, 2, 3, 4, 0, 80])))


class AccessListTest(unittest.TestCase):
    def test_localhost_allowed(self):
        rules = AccessList(["127.0.0.1", "::1"])
        self.assertTrue(rules.allows("127.0.0.1"))
        self.assertTrue(rules.allows("::1"))
        self.assertFalse(rules.allows("8.8.8.8"))

    def test_cidr(self):
        rules = AccessList(["10.0.0.0/8"])
        self.assertTrue(rules.allows("10.1.2.3"))
        self.assertFalse(rules.allows("11.1.2.3"))

    def test_deny_after_allow_is_ignored(self):
        """第一条命中的规则生效：allow 写在前面时，后面的 deny 不起作用。"""
        rules = AccessList(["0.0.0.0/0", "!10.0.0.5"])
        self.assertTrue(rules.allows("8.8.8.8"))
        self.assertTrue(rules.allows("10.0.0.5"))

    def test_deny_first_wins(self):
        """例外写在前面：deny 优先。"""
        rules = AccessList(["!10.0.0.5", "0.0.0.0/0"])
        self.assertFalse(rules.allows("10.0.0.5"))
        self.assertTrue(rules.allows("8.8.8.8"))

    def test_default_deny(self):
        self.assertFalse(AccessList(["10.0.0.0/8"]).allows("8.8.8.8"))


class SocksServerTest(unittest.TestCase):
    def setUp(self):
        self.upstream = DirectSocksServer(("127.0.0.1", 0), DirectSocksHandler)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.httpd = start_http_echo()
        self.server = SocksServer(upstream=self.upstream.server_address, host="127.0.0.1", port=0)
        self.server.bind()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.upstream.shutdown()
        self.httpd.shutdown()

    def test_connect_no_auth(self):
        sock = socks_client.socks5_connect(
            self.server.address, "127.0.0.1", self.httpd.server_address[1], timeout=10
        )
        try:
            sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.assertIn(b"200 OK", data)
        finally:
            sock.close()

    def test_connect_domain_name(self):
        """域名地址应原样交给上游，不在本机解析。"""
        sock = socks_client.socks5_connect(
            self.server.address, "localhost", self.httpd.server_address[1], timeout=10
        )
        sock.close()

    def test_ipv6_target(self):
        """IPv6 字面量地址应被正确解析并转发。"""
        echo6 = start_http_echo(host="::1")
        try:
            sock = socks_client.socks5_connect(
                self.server.address, "::1", echo6.server_address[1], timeout=10
            )
            try:
                sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                data = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.assertIn(b"200 OK", data)
            finally:
                sock.close()
        finally:
            echo6.shutdown()

    def test_bind_command_rejected(self):
        sock = socket.create_connection(self.server.address, timeout=10)
        try:
            sock.sendall(bytes([0x05, 0x01, 0x00]))
            sock.recv(2)
            sock.sendall(bytes([0x05, 0x02, 0x00, 0x01]) + socket.inet_aton("1.2.3.4") + (80).to_bytes(2, "big"))
            reply = sock.recv(10)
            self.assertEqual(reply[1], 0x07)  # command not supported
        finally:
            sock.close()

    def test_wrong_version_rejected(self):
        """版本不是 5 时无法给出合法应答，正确行为是直接断开。"""
        sock = socket.create_connection(self.server.address, timeout=10)
        try:
            sock.sendall(bytes([0x04, 0x01, 0x00]))
            self.assertEqual(sock.recv(10), b"")
        finally:
            sock.close()

    def test_unknown_auth_method_rejected(self):
        sock = socket.create_connection(self.server.address, timeout=10)
        try:
            sock.sendall(bytes([0x05, 0x01, 0x09]))  # 0x09 = GSSAPI，双方都不支持
            self.assertEqual(sock.recv(2), bytes([0x05, 0xFF]))
        finally:
            sock.close()


class SocksAuthTest(unittest.TestCase):
    def setUp(self):
        self.upstream = DirectSocksServer(("127.0.0.1", 0), DirectSocksHandler)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.httpd = start_http_echo()
        self.server = SocksServer(
            upstream=self.upstream.server_address, host="127.0.0.1", port=0,
            username="bob", password="hunter2",
        )
        self.server.bind()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.upstream.shutdown()
        self.httpd.shutdown()

    def test_correct_credentials(self):
        sock = socks_client.socks5_connect(
            self.server.address, "127.0.0.1", self.httpd.server_address[1],
            username="bob", password="hunter2", timeout=10,
        )
        sock.close()

    def test_wrong_password(self):
        with self.assertRaises(SocksError):
            socks_client.socks5_connect(
                self.server.address, "127.0.0.1", 80,
                username="bob", password="nope", timeout=10,
            )

    def test_client_without_auth_support_rejected(self):
        sock = socket.create_connection(self.server.address, timeout=10)
        try:
            sock.sendall(bytes([0x05, 0x01, 0x00]))  # 只声明免认证
            reply = sock.recv(2)
            self.assertEqual(reply, bytes([0x05, 0xFF]))
        finally:
            sock.close()


class SocksAclTest(unittest.TestCase):
    def setUp(self):
        self.upstream = DirectSocksServer(("127.0.0.1", 0), DirectSocksHandler)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.httpd = start_http_echo()
        self.server = SocksServer(
            upstream=self.upstream.server_address, host="127.0.0.1", port=0,
            allow_from=["10.99.99.0/24"],
        )
        self.server.bind()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.upstream.shutdown()
        self.httpd.shutdown()

    def test_denied_client(self):
        with self.assertRaises(SocksError) as ctx:
            socks_client.socks5_connect(
                self.server.address, "127.0.0.1", 80, timeout=10
            )
        self.assertIn("0x02", str(ctx.exception))


class BridgeParseTest(unittest.TestCase):
    def test_minimal_meek(self):
        bridge = bridges_mod.parse_bridge_line(
            "Bridge meek 0.0.2.0:3 url=https://meek.example/ front=ajax.example"
        )
        self.assertEqual(bridge.transport, "meek")
        self.assertTrue(bridge.is_meek)
        self.assertEqual(bridge.args["url"], "https://meek.example/")
        self.assertEqual(bridge.args["front"], "ajax.example")
        self.assertIn("url=https://meek.example/", bridge.to_torrc())

    def test_meek_lite_with_utls(self):
        bridge = bridges_mod.parse_bridge_line(
            "Bridge meek_lite 192.0.2.20:80 url=https://cdn.example "
            "front=www.example utls=HelloRandomizedALPN"
        )
        self.assertEqual(bridge.transport, "meek_lite")
        self.assertEqual(bridge.args["utls"], "HelloRandomizedALPN")

    def test_with_fingerprint_and_version(self):
        bridge = bridges_mod.parse_bridge_line(
            "Bridge meek 1.0.0 0.0.2.0:3 9772D04C153434D4A6E01D4B821614B2B1F91611 "
            "url=https://x.example/ front=y.example"
        )
        self.assertEqual(bridge.version, "1.0.0")
        self.assertEqual(bridge.fingerprint, "9772D04C153434D4A6E01D4B821614B2B1F91611")

    def test_obfs4_passthrough(self):
        bridge = bridges_mod.parse_bridge_line(
            "Bridge obfs4 1.2.3.4:443 ABCDEF0123456789ABCDEF0123456789ABCD cert=abc iat-mode=0"
        )
        self.assertEqual(bridge.transport, "obfs4")
        self.assertEqual(bridge.args["iat-mode"], "0")

    def test_fingerprint_kwarg(self):
        bridge = bridges_mod.parse_bridge_line(
            "Bridge meek 0.0.2.0:3 fingerprint=9772D04C153434D4A6E01D4B821614B2B1F91611 "
            "url=https://x.example/"
        )
        self.assertEqual(bridge.fingerprint, "9772D04C153434D4A6E01D4B821614B2B1F91611")

    def test_missing_url_rejected(self):
        with self.assertRaises(bridges_mod.BridgeError):
            bridges_mod.parse_bridge_line("Bridge meek 0.0.2.0:3 front=x.example")

    def test_bad_url_scheme(self):
        with self.assertRaises(bridges_mod.BridgeError):
            bridges_mod.parse_bridge_line("Bridge meek 0.0.2.0:3 url=ftp://x/")

    def test_unknown_transport(self):
        with self.assertRaises(bridges_mod.BridgeError):
            bridges_mod.parse_bridge_line("Bridge fancy 1.2.3.4:80")

    def test_quoted_value(self):
        bridge = bridges_mod.parse_bridge_line(
            'Bridge meek 0.0.2.0:3 url=https://x.example/ front="a b.example"'
        )
        self.assertEqual(bridge.args["front"], "a b.example")

    def test_normalize_mixed_input(self):
        text = """
随便一行说明
Bridge meek 0.0.2.0:3 url=https://a.example/ front=b.example
meek 0.0.2.0:3 url=https://c.example/
"""
        normalized, errors = bridges_mod.normalize(text)
        self.assertEqual(len(normalized.splitlines()), 2)
        self.assertEqual(len(errors), 1)


class BridgeStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="torsocks5-test-")
        self.path = os.path.join(self.tmp, "bridges.toml")

    def test_save_and_load_roundtrip(self):
        store = bridges_mod.BridgeStore(self.path)
        store.add(bridges_mod.parse_bridge_line(
            "Bridge meek 0.0.2.0:3 url=https://a.example/ front=b.example utls=HelloChrome_102"
        ))
        store.add(bridges_mod.parse_bridge_line(
            "Bridge obfs4 1.2.3.4:443 ABCDEF0123456789ABCDEF0123456789ABCD cert=zz"
        ))
        store.save()

        reloaded = bridges_mod.BridgeStore(self.path).load(include_builtin=False)
        self.assertEqual(len(reloaded.bridges), 2)
        meek = [b for b in reloaded.bridges if b.is_meek][0]
        self.assertEqual(meek.args["url"], "https://a.example/")
        self.assertEqual(meek.args["front"], "b.example")
        self.assertEqual(meek.args["utls"], "HelloChrome_102")
        self.assertEqual(meek.to_torrc(), store.bridges[0].to_torrc())

    def test_dedup(self):
        store = bridges_mod.BridgeStore(self.path)
        line = "Bridge meek 0.0.2.0:3 url=https://a.example/ front=b.example"
        self.assertTrue(store.add(bridges_mod.parse_bridge_line(line)))
        self.assertFalse(store.add(bridges_mod.parse_bridge_line(line)))
        self.assertEqual(len(store.bridges), 1)

    def test_disabled_bridge(self):
        store = bridges_mod.BridgeStore(self.path)
        bridge = bridges_mod.parse_bridge_line("Bridge meek 0.0.2.0:3 url=https://a.example/")
        store.add(bridge)
        bridge.enabled = False
        store.save()
        reloaded = bridges_mod.BridgeStore(self.path).load(include_builtin=False)
        self.assertEqual(len(reloaded.active()), 0)
        self.assertEqual(len(reloaded.bridges), 1)

    def test_remove(self):
        store = bridges_mod.BridgeStore(self.path)
        store.add(bridges_mod.parse_bridge_line("Bridge meek 0.0.2.0:3 url=https://a.example/"))
        self.assertEqual(store.remove("a.example"), 1)
        self.assertEqual(len(store.bridges), 0)


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        config = config_mod.Config({})
        self.assertEqual(config.get("proxy.port"), 9051)
        self.assertEqual(config.get("proxy.listen"), "127.0.0.1")
        self.assertEqual(config.get("tor.meek_mode"), "plugin")
        self.assertEqual(config.get("tor.restart"), True)

    def test_user_override(self):
        config = config_mod.Config({"proxy": {"port": 1080}, "tor": {"direct": True}})
        self.assertEqual(config.get("proxy.port"), 1080)
        self.assertEqual(config.get("tor.direct"), True)
        self.assertEqual(config.get("proxy.listen"), "127.0.0.1")

    def test_builtin_toml_parser(self):
        text = """
# 注释
[proxy]
port = 9052
allow_from = [
  "127.0.0.1",   # 允许本机
  "::1",
]
udp_associate = false

[tor]
extra_options = ["A 1", "B 2"]

[meek]
methods = ["meek", "meek_lite"]
connect_timeout = 12.5
"""
        data = config_mod.loads(text)
        self.assertEqual(data["proxy"]["port"], 9052)
        self.assertEqual(data["proxy"]["allow_from"], ["127.0.0.1", "::1"])
        self.assertEqual(data["proxy"]["udp_associate"], False)
        self.assertEqual(data["tor"]["extra_options"], ["A 1", "B 2"])
        self.assertEqual(data["meek"]["connect_timeout"], 12.5)

    def test_builtin_parser_matches_reference(self):
        text = """
[proxy]
port = 9052
allow_from = ["127.0.0.1", "::1"]
udp_associate = false
verbose = true
[tor]
extra_options = ["A 1", "B 2"]
name = "x"
ratio = 0.25
[meek]
methods = ["meek", "meek_lite"]
connect_timeout = 12.5
"""
        mine = config_mod.loads(text)
        try:
            import tomllib

            theirs = tomllib.loads(text)
        except ImportError:
            self.skipTest("no tomllib")
        self.assertEqual(mine, theirs)

    def test_shipped_example_config_parses(self):
        example = os.path.join(ROOT, "torsocks5", "config.example.toml")
        if not os.path.exists(example):
            self.skipTest("example config missing")
        data = config_mod.load_toml(example)
        config = config_mod.Config(data)
        self.assertEqual(config.get("proxy.port"), 9051)
        self.assertIn("meek", config.get("meek.methods"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
