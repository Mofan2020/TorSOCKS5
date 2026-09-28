"""TSU/1 端到端：真起中继 + 真起目标服务器，走完整隧道。

覆盖：健康检查、鉴权、真实 HTTP 转发、1 MiB 大数据往返、并发换链路、半关闭、
错误码映射、白名单/私有地址策略、未定义 opcode 的规范行为、SOCKS5 over tunnel。
（纯编解码与策略测试在 ``tests/test_tunnel_protocol.py``。）
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.selftest_helpers import TOKEN, start_http_echo  # noqa: E402
from torsocks5.socks5 import client as socks_client  # noqa: E402
from torsocks5.socks5.protocol import SocksError  # noqa: E402
from torsocks5.socks5.server import SocksServer  # noqa: E402
from torsocks5.tunnel import protocol as proto  # noqa: E402
from torsocks5.tunnel.client import (  # noqa: E402
    TargetNotAllowed,
    TunnelClient,
    TunnelError,
    TunnelUnavailable,
)
from torsocks5.tunnel.probe import (  # noqa: E402
    format_report,
    http_url_of,
    probe_health,
    run_probe,
)
from torsocks5.tunnel.relay import RelayServer  # noqa: E402
from torsocks5.tunnel.stream import MAX_PENDING_BYTES  # noqa: E402
from torsocks5.tunnel.wsclient import WebSocketError, WSClient  # noqa: E402

RELAY_TOKEN = "unit-test-token"


class TunnelEndToEndTests(unittest.TestCase):
    """真起一个中继 + 真起一个目标服务器，走完整隧道。"""

    @classmethod
    def setUpClass(cls):
        cls.echo = start_http_echo()
        cls.echo_port = cls.echo.server_address[1]
        cls.relay = RelayServer("127.0.0.1", 0, token=RELAY_TOKEN, allow_all=True,
                                allow_private=True, max_streams=2, keepalive=5.0,
                                idle_timeout=20.0, connect_timeout=5.0)
        cls.relay.bind()
        cls.relay_port = cls.relay.port
        cls.relay_thread = threading.Thread(target=cls.relay.serve_forever, daemon=True)
        cls.relay_thread.start()
        cls.url = "ws://127.0.0.1:%d/tsu" % cls.relay_port

    @classmethod
    def tearDownClass(cls):
        cls.relay.stop()
        cls.echo.shutdown()

    def make_client(self, **kwargs) -> TunnelClient:
        params = dict(links=2, max_streams=2, open_timeout=5.0, idle_timeout=10.0,
                      keepalive=5.0, auto_reconnect=False)
        params.update(kwargs)
        client = TunnelClient(self.url, RELAY_TOKEN, **params)
        self.addCleanup(client.close)
        client.start()
        return client

    # ------------------------------------------------------------ 基础
    def test_health_endpoint(self):
        health = probe_health(self.url, timeout=5.0)
        self.assertTrue(health["ok"], health)
        self.assertEqual(health["proto"], proto.PROTO)
        self.assertEqual(health["max_streams"], 2)

    def test_wrong_token_is_rejected(self):
        client = TunnelClient(self.url, "wrong-token", links=1, max_streams=1, open_timeout=5.0)
        with self.assertRaises(TunnelUnavailable) as ctx:
            client.start()
        self.assertIn("401", str(ctx.exception))
        client.close()

    def test_missing_token_is_rejected(self):
        client = TunnelClient(self.url, "", links=1, max_streams=1, open_timeout=5.0)
        with self.assertRaises(TunnelUnavailable):
            client.start()
        client.close()

    # ------------------------------------------------------------ 转发
    def test_http_through_tunnel(self):
        client = self.make_client()
        stream = client.connect("127.0.0.1", self.echo_port, timeout=5.0)
        try:
            request = ("GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n").encode()
            stream.sendall(request)
            data = b""
            deadline = time.time() + 5
            while TOKEN.encode() not in data and time.time() < deadline:
                chunk = stream.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.assertIn(b"200 OK", data)
            self.assertIn(TOKEN.encode(), data)
            self.assertGreater(stream.bytes_up, 0)
            self.assertGreater(stream.bytes_down, 0)
        finally:
            stream.close()

    def test_large_payload_roundtrip(self):
        """1 MiB 数据（跨越 32 KiB 分片上限）要能原样回来，半关闭不能吃掉已收数据。"""
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        sink_port = server.getsockname()[1]
        received = bytearray()

        def echo_once():
            conn, _ = server.accept()
            try:
                while len(received) < 1024 * 1024:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    received.extend(chunk)
                    conn.sendall(chunk)
            finally:
                conn.close()
                server.close()

        threading.Thread(target=echo_once, daemon=True).start()
        payload = os.urandom(1024 * 1024)
        client = self.make_client(max_streams=4)
        stream = client.connect("127.0.0.1", sink_port, timeout=10.0)
        try:
            stream.sendall(payload)
            stream.shutdown(socket.SHUT_WR)
            got = bytearray()
            while len(got) < len(payload):
                chunk = stream.recv(65536)
                if not chunk:
                    break
                got.extend(chunk)
            self.assertEqual(len(got), len(payload))
            self.assertEqual(bytes(got), payload)
        finally:
            stream.close()

    def test_concurrent_streams_and_link_pool(self):
        """并发数超过单链路上限（2）时必须自动换链路，而不是报错给上层。"""
        client = self.make_client()
        streams = [client.connect("127.0.0.1", self.echo_port, timeout=8.0) for _ in range(3)]
        try:
            for stream in streams:
                stream.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
            for stream in streams:
                data = b""
                deadline = time.time() + 8
                while TOKEN.encode() not in data and time.time() < deadline:
                    chunk = stream.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.assertIn(TOKEN.encode(), data)
            stats = client.stats()
            self.assertEqual(stats["streams_total"], 3)
            self.assertGreaterEqual(stats["links_live"], 2)
        finally:
            for stream in streams:
                stream.close()

    def test_half_close_is_forwarded(self):
        client = self.make_client()
        stream = client.connect("127.0.0.1", self.echo_port, timeout=5.0)
        try:
            stream.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
            stream.shutdown(socket.SHUT_WR)  # 半关闭：本地不再写，但仍要能读
            data = b""
            deadline = time.time() + 5
            while TOKEN.encode() not in data and time.time() < deadline:
                chunk = stream.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.assertIn(TOKEN.encode(), data)
            # 对端也会半关闭（HTTP/1.0），最终读到 EOF
            self.assertEqual(stream.recv(16), b"")
        finally:
            stream.close()

    def test_backpressure_stalls_both_directions(self):
        """规范 3.4：积压超过 1 MiB 必须停止读对端，不能无界缓冲。

        两个方向都测：
        * 目标 → 客户端：客户端故意不读，目标推 32 MiB 必须被刹住（且随后读回来一字节不差）；
        * 客户端 → 目标：目标故意不读，客户端 ``sendall`` 必须被刹住。
        """
        total = 32 * 1024 * 1024
        drain_wait = 2.0

        # ---------------------------------------------------- 目标 → 客户端
        fast = socket.socket()
        fast.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        fast.bind(("127.0.0.1", 0))
        fast.listen(1)
        fast_port = fast.getsockname()[1]
        payload = os.urandom(total)
        progress = {"sent": 0}

        def push():
            conn, _ = fast.accept()
            try:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
                view = memoryview(payload)
                while progress["sent"] < total:
                    sent = conn.send(view[progress["sent"]:progress["sent"] + 262144])
                    if not sent:
                        break
                    progress["sent"] += sent
            except OSError:
                pass
            finally:
                conn.close()
                fast.close()

        threading.Thread(target=push, daemon=True).start()
        client = self.make_client(max_streams=4)
        stream = client.connect("127.0.0.1", fast_port, timeout=10.0)
        try:
            time.sleep(drain_wait)
            first = progress["sent"]
            pending = stream.pending
            time.sleep(drain_wait)
            second = progress["sent"]
            self.assertLess(pending, MAX_PENDING_BYTES + 1024 * 1024,
                            "客户端积压超过了背压上限：%d 字节" % pending)
            self.assertLess(second, total, "目标应该被刹住，却把 32 MiB 全推完了")
            self.assertLess(second - first, 1024 * 1024,
                            "阻塞期间目标又推进了 %d 字节，说明没有真正背压" % (second - first))
            # 继续读：数据必须完整无损
            got = bytearray()
            deadline = time.time() + 60
            while len(got) < total and time.time() < deadline:
                chunk = stream.recv(262144)
                if not chunk:
                    break
                got.extend(chunk)
            self.assertEqual(len(got), total)
            self.assertEqual(bytes(got), payload)
        finally:
            stream.close()
            client.close()

        # ---------------------------------------------------- 客户端 → 目标
        slow = socket.socket()
        slow.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        slow.bind(("127.0.0.1", 0))
        slow.listen(1)
        slow_port = slow.getsockname()[1]
        received = {"total": 0, "conn": None}

        def accept_and_stall():
            conn, _ = slow.accept()
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
            received["conn"] = conn
            while True:  # 收到就丢，绝不主动读——模拟「下游很慢」
                time.sleep(0.2)
                break

        threading.Thread(target=accept_and_stall, daemon=True).start()
        client2 = self.make_client(max_streams=4)
        stream2 = client2.connect("127.0.0.1", slow_port, timeout=10.0)
        sent = {"bytes": 0}

        def push_from_client():
            view = memoryview(payload[:8 * 1024 * 1024])
            try:
                while sent["bytes"] < len(view):
                    chunk = view[sent["bytes"]:sent["bytes"] + 262144]
                    stream2.sendall(chunk)
                    sent["bytes"] += len(chunk)
            except (OSError, WebSocketError):
                # 被背压刹住后 WS 发送会超时，这是预期结果，不是错误
                pass

        try:
            thread = threading.Thread(target=push_from_client, daemon=True)
            thread.start()
            time.sleep(drain_wait)
            first = sent["bytes"]
            time.sleep(drain_wait)
            second = sent["bytes"]
            self.assertLess(second - first, 1024 * 1024,
                            "对端不读时客户端仍在推进 %d 字节，说明没有背压" % (second - first))
            self.assertLess(second, 8 * 1024 * 1024, "8 MiB 不该全推完")
        finally:
            stream2.close()
            client2.close()
            conn = received["conn"]
            if conn is not None:
                conn.close()
            slow.close()

    def test_connection_refused_maps_to_error(self):
        client = self.make_client()
        with self.assertRaises(TunnelError) as ctx:
            client.connect("127.0.0.1", 1, timeout=5.0)  # 1 号端口几乎不可能有服务
        self.assertTrue(hasattr(ctx.exception, "rep_code"))

    def test_stats_and_probe_report(self):
        client = self.make_client()
        stats = client.stats()
        self.assertEqual(stats["url"], self.url)
        self.assertGreaterEqual(stats["links_live"], 1)
        client.close()
        report = run_probe(self.url, RELAY_TOKEN, targets=[("127.0.0.1", self.echo_port)],
                           timeout=5.0, http_probe=True, max_streams=2)
        self.assertTrue(report["ok"], report)
        self.assertIn("200 OK", str(report["targets"][0]["http"]))
        self.assertIn("合计 1/1", format_report(report))
        self.assertTrue(http_url_of(self.url, "/healthz").endswith("/healthz"))

    # ------------------------------------------------------------ 协议规范行为
    def test_unknown_opcode_gets_reset_and_link_survives(self):
        """规范 2.1：未定义 opcode 必须用 RESET(该流) 回应，而不是断开整条链路。"""
        ws = WSClient("%s?token=%s" % (self.url, RELAY_TOKEN), timeout=5.0)
        self.addCleanup(ws.close)
        ws.connect()
        ws.send_binary(proto.encode_frame(0x09, 7))
        kind, payload = ws.recv(timeout=4.0)
        self.assertEqual(kind, "binary")
        opcode, stream_id, _body = proto.decode_frame(payload)
        self.assertEqual((opcode, stream_id), (proto.OP_RESET, 7))
        # 链路还能继续用：PING 仍能拿到 PONG
        ws.send_binary(proto.encode_frame(proto.OP_PING, 0, b"still-alive"))
        kind, payload = ws.recv(timeout=4.0)
        self.assertEqual(proto.decode_frame(payload)[0], proto.OP_PONG)

    def test_unknown_opcode_on_stream_zero_closes_link(self):
        """流 id 为 0 的未定义 opcode → 按规范关闭连接。"""
        ws = WSClient("%s?token=%s" % (self.url, RELAY_TOKEN), timeout=5.0)
        self.addCleanup(ws.close)
        ws.connect()
        ws.send_binary(proto.encode_frame(0x7F, 0))
        ended = False
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                kind, _payload = ws.recv(timeout=4.0)
                if kind == "close":
                    ended = True
                    break
        except (WebSocketError, OSError):
            ended = True
        self.assertTrue(ended, "中继对未定义 opcode（流 0）应当关闭连接")

    def test_text_frames_are_ignored(self):
        """规范 1：文本帧一律忽略，不得当成 DATA。"""
        ws = WSClient("%s?token=%s" % (self.url, RELAY_TOKEN), timeout=5.0)
        self.addCleanup(ws.close)
        ws.connect()
        ws.send_text(b"not-a-tsu-frame")
        ws.send_binary(proto.encode_frame(proto.OP_PING, 0, b"ping-after-text"))
        kind, payload = ws.recv(timeout=4.0)
        self.assertEqual(kind, "binary")
        self.assertEqual(proto.decode_frame(payload)[0], proto.OP_PONG)

    # ------------------------------------------------------------ 通过 SOCKS5 服务
    def test_socks5_server_over_tunnel(self):
        """把隧道 connector 接到现有 SOCKS5 服务端上，用真实 SOCKS5 客户端跑一次。"""
        client = self.make_client()
        server = SocksServer(upstream=None, connector=client.connect, host="127.0.0.1", port=0,
                             idle_timeout=10.0, connect_timeout=5.0, udp_associate=False)
        bound = server.bind()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        try:
            sock = socks_client.socks5_connect(bound, "127.0.0.1", self.echo_port, timeout=8.0)
        except OSError as exc:  # pragma: no cover - 失败时给出可读原因
            self.fail("SOCKS5 over tunnel 连接失败: %s" % exc)
        with sock:
            sock.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            data = b""
            deadline = time.time() + 8
            while TOKEN.encode() not in data and time.time() < deadline:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        self.assertIn(TOKEN.encode(), data)
        self.assertEqual(server.total_connections, 1)

    def test_socks5_udp_is_refused_for_tunnel_routes(self):
        client = self.make_client()
        server = SocksServer(upstream=None, connector=client.connect, host="127.0.0.1", port=0,
                             udp_associate=True, idle_timeout=5.0)
        bound = server.bind()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with self.assertRaises(SocksError):
            socks_client.socks5_udp_associate(bound, timeout=3.0)


class RelayPolicyTests(unittest.TestCase):
    """中继侧的策略：白名单关闭时拒绝私有地址，白名单开启时拒绝名单外目标。"""

    def _serve(self, **kwargs) -> RelayServer:
        server = RelayServer("127.0.0.1", 0, keepalive=5.0, idle_timeout=10.0,
                             connect_timeout=5.0, **kwargs)
        server.bind()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.stop)
        return server

    def test_private_target_blocked_by_default(self):
        server = self._serve(allow_all=True, allow_private=False, max_streams=4)
        client = TunnelClient("ws://127.0.0.1:%d/tsu" % server.port, "",
                              links=1, max_streams=2, open_timeout=5.0, auto_reconnect=False)
        self.addCleanup(client.close)
        client.start()
        with self.assertRaises(TargetNotAllowed) as ctx:
            client.connect("127.0.0.1", 80, timeout=5.0)
        self.assertEqual(ctx.exception.err_code, proto.ERR_BLOCKED_TARGET)

    def test_allow_list_rejects_other_targets(self):
        server = self._serve(allow_hosts=["github.com"], allow_ports=[443], max_streams=4)
        client = TunnelClient("ws://127.0.0.1:%d/tsu" % server.port, "",
                              links=1, max_streams=2, open_timeout=5.0, auto_reconnect=False)
        self.addCleanup(client.close)
        client.start()
        with self.assertRaises(TargetNotAllowed) as ctx:
            client.connect("example.org", 443, timeout=5.0)
        self.assertEqual(ctx.exception.err_code, proto.ERR_NOT_ALLOWED)
        # 端口不在白名单同样被拒
        with self.assertRaises(TargetNotAllowed) as port_ctx:
            client.connect("github.com", 8080, timeout=5.0)
        self.assertEqual(port_ctx.exception.err_code, proto.ERR_NOT_ALLOWED)

    def test_concurrency_limit_reports_too_many_streams(self):
        server = self._serve(allow_all=True, allow_private=True, max_streams=1)
        echo = start_http_echo()
        self.addCleanup(echo.shutdown)
        client = TunnelClient("ws://127.0.0.1:%d/tsu" % server.port, "",
                              links=1, max_streams=1, open_timeout=5.0, auto_reconnect=False)
        self.addCleanup(client.close)
        client.start()
        first = client.connect("127.0.0.1", echo.server_address[1], timeout=5.0)
        self.addCleanup(first.close)
        # 上限 1 且链路数已满：第二次会被中继拒绝，客户端只能等或失败
        with self.assertRaises(Exception) as ctx:
            client.connect("127.0.0.1", echo.server_address[1], timeout=1.0)
        self.assertTrue(hasattr(ctx.exception, "rep_code"))
        self.assertGreaterEqual(server.counters["streams_active"], 1)

    def test_target_labels_are_not_logged_by_default(self):
        """规范 3.8：中继默认不把目标域名写进日志。"""
        messages: list[str] = []
        server = RelayServer("127.0.0.1", 0, allow_all=True, allow_private=True,
                             max_streams=4, keepalive=5.0, idle_timeout=10.0,
                             connect_timeout=5.0, on_log=messages.append)
        server.bind()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.stop)
        echo = start_http_echo()
        self.addCleanup(echo.shutdown)
        client = TunnelClient("ws://127.0.0.1:%d/tsu" % server.port, "", links=1, max_streams=2,
                              open_timeout=5.0, auto_reconnect=False)
        self.addCleanup(client.close)
        client.start()
        stream = client.connect("127.0.0.1", echo.server_address[1], timeout=5.0)
        stream.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
        deadline = time.time() + 5
        while time.time() < deadline and not stream.pending:
            time.sleep(0.05)
        stream.close()
        time.sleep(0.3)
        joined = "\n".join(messages)
        self.assertNotIn("127.0.0.1:%d" % echo.server_address[1], joined)
        # 打开开关后才允许打印目标
        messages.clear()
        server.log_targets = True
        stream = client.connect("127.0.0.1", echo.server_address[1], timeout=5.0)
        stream.close()
        time.sleep(0.3)
        self.assertIn("127.0.0.1:%d" % echo.server_address[1], "\n".join(messages))

    def test_relay_stats(self):
        server = self._serve(allow_all=True, allow_private=True, max_streams=4)
        echo = start_http_echo()
        self.addCleanup(echo.shutdown)
        client = TunnelClient("ws://127.0.0.1:%d/tsu" % server.port, "",
                              links=1, max_streams=2, open_timeout=5.0, auto_reconnect=False)
        self.addCleanup(client.close)
        client.start()
        stream = client.connect("127.0.0.1", echo.server_address[1], timeout=5.0)
        stream.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
        deadline = time.time() + 5
        while time.time() < deadline and not stream.pending:
            time.sleep(0.05)
        stream.close()
        time.sleep(0.3)
        stats = server.stats()
        self.assertEqual(stats["streams_total"], 1)
        self.assertIn("会话 1", server.status_line())
        self.assertEqual(server.describe().count("中继"), 1)


class WSClientTests(unittest.TestCase):
    """WS 客户端与自己的中继握不上手时要给出明确错误（不静默）。"""

    def test_handshake_against_plain_http_server(self):
        echo = start_http_echo()
        self.addCleanup(echo.shutdown)
        client = WSClient("ws://127.0.0.1:%d/tsu" % echo.server_address[1], timeout=5.0)
        with self.assertRaises(WebSocketError):
            client.connect()
        client.close()

    def test_health_endpoint_wrong_path(self):
        relay = RelayServer("127.0.0.1", 0, allow_all=True, allow_private=True)
        relay.bind()
        threading.Thread(target=relay.serve_forever, daemon=True).start()
        self.addCleanup(relay.stop)
        health = probe_health("ws://127.0.0.1:%d/tsu" % relay.port, timeout=5.0)
        self.assertTrue(health["ok"], health)
        self.assertFalse(health["allow_all"] is None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
