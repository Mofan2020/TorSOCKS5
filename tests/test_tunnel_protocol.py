"""TSU/1 协议层：帧编解码、地址编码、目标策略、主机匹配、智能分流、RFC 6455 帧层。

（真起中继做端到端转发的测试在 ``tests/test_tunnel_e2e.py``。）
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from torsocks5.hostrules import (  # noqa: E402
    host_matches,
    is_private_address,
    normalize_host,
    split_list,
)
from torsocks5.split import DIRECT, TUNNEL, SplitRouter  # noqa: E402
from torsocks5.tunnel import protocol as proto  # noqa: E402
from torsocks5.tunnel.wsclient import WebSocketError  # noqa: E402
from torsocks5.tunnel.wsframe import (  # noqa: E402
    OP_BINARY,
    OP_CONTINUATION,
    OP_PING,
    OP_TEXT,
    FrameParser,
    check_handshake,
    encode_frame,
    mask_payload,
    new_key,
    parse_close_payload,
    parse_http_response,
    websocket_accept,
)

PUBLIC_HOST = "github.com"


# --------------------------------------------------------------------- 协议
class ProtocolTests(unittest.TestCase):
    def test_frame_roundtrip(self):
        raw = proto.encode_frame(proto.OP_DATA, 42, b"hello")
        opcode, stream_id, payload = proto.decode_frame(raw)
        self.assertEqual((opcode, stream_id, payload), (proto.OP_DATA, 42, b"hello"))
        self.assertEqual(proto.HEADER_SIZE, 5)  # 规范：1 字节 opcode + 4 字节 stream id

    def test_frame_rejects_oversized_payload(self):
        with self.assertRaises(proto.ProtocolError):
            proto.encode_frame(proto.OP_DATA, 1, b"x" * (proto.MAX_SEND_MESSAGE + 1))

    def test_frame_allows_full_stream_id_range(self):
        raw = proto.encode_frame(proto.OP_DATA, 0xFFFFFFFF, b"")
        self.assertEqual(proto.decode_frame(raw)[1], 0xFFFFFFFF)
        with self.assertRaises(proto.ProtocolError):
            proto.encode_frame(proto.OP_DATA, 0x1_0000_0000)

    def test_decode_short_and_unknown_opcode(self):
        with self.assertRaises(proto.ProtocolError):
            proto.decode_frame(b"\x01\x00")
        # 未定义 opcode 必须是 UnknownOpcode（带上流 id，供上层按规范回 RESET）
        with self.assertRaises(proto.UnknownOpcode) as ctx:
            proto.decode_frame(bytes([0x77, 0, 0, 0, 9]))
        self.assertEqual(ctx.exception.opcode, 0x77)
        self.assertEqual(ctx.exception.stream_id, 9)
        self.assertIsInstance(ctx.exception, proto.ProtocolError)
        with self.assertRaises(proto.UnknownOpcode) as zero:
            proto.decode_frame(bytes([0x77, 0, 0, 0, 0]))
        self.assertEqual(zero.exception.stream_id, 0)

    def test_address_roundtrip_v4_domain_v6(self):
        for host, port in (("127.0.0.1", 443), ("example.com", 80), ("::1", 9418)):
            payload = proto.encode_address(host, port)
            self.assertEqual(proto.decode_address(payload), (host, port))

    def test_address_domain_case_is_normalised(self):
        payload = proto.encode_address("GitHub.COM.", 443)
        self.assertEqual(proto.decode_address(payload), ("github.com", 443))
        self.assertEqual(payload[0], 0x03)
        self.assertEqual(payload[1], len("github.com"))

    def test_address_rejects_bad_input(self):
        with self.assertRaises(proto.ProtocolError):
            proto.encode_address("", 443)
        with self.assertRaises(proto.ProtocolError):
            proto.encode_address("example.com", 0)
        with self.assertRaises(proto.ProtocolError):
            proto.decode_address(b"")
        with self.assertRaises(proto.ProtocolError):
            proto.decode_address(b"\x09\x00\x00")
        with self.assertRaises(proto.ProtocolError):
            proto.decode_address(proto.encode_address("example.com", 443) + b"junk")

    def test_error_payload(self):
        payload = proto.encode_error(proto.ERR_NOT_ALLOWED, "不在白名单")
        code, message = proto.decode_error(payload)
        self.assertEqual(code, proto.ERR_NOT_ALLOWED)
        self.assertEqual(message, "不在白名单")
        self.assertEqual(proto.error_name(code), "NOT_ALLOWED")

    def test_chunk_payload_split(self):
        pieces = list(proto.chunk_payload(b"a" * 100, 30))
        self.assertEqual([len(item) for item in pieces], [30, 30, 30, 10])
        self.assertEqual(b"".join(pieces), b"a" * 100)

    def test_target_policy(self):
        cases = [
            (PUBLIC_HOST, 443, None),
            ("api.github.com", 443, None),
            ("evil.example.com", 443, proto.ERR_NOT_ALLOWED),
            (PUBLIC_HOST, 3306, proto.ERR_NOT_ALLOWED),
            ("127.0.0.1", 443, proto.ERR_BLOCKED_TARGET),
            ("192.168.1.10", 443, proto.ERR_BLOCKED_TARGET),
            ("10.0.0.1", 80, proto.ERR_BLOCKED_TARGET),
            ("169.254.1.1", 80, proto.ERR_BLOCKED_TARGET),
            ("::1", 443, proto.ERR_BLOCKED_TARGET),
            ("fd00::1", 443, proto.ERR_BLOCKED_TARGET),
            ("", 443, proto.ERR_BAD_REQUEST),
            (PUBLIC_HOST, 0, proto.ERR_BAD_REQUEST),
        ]
        for host, port, expected in cases:
            with self.subTest(host=host, port=port):
                self.assertEqual(proto.target_policy(host, port), expected)
        # 关闭白名单后任意目标都放行（私有地址仍然拦）
        self.assertIsNone(proto.target_policy("anything.example", 443, allow_all=True))
        self.assertIsNone(proto.target_policy("anything.example", 12345, allow_all=True))
        self.assertEqual(proto.target_policy("10.1.2.3", 443, allow_all=True),
                         proto.ERR_BLOCKED_TARGET)


class HostRuleTests(unittest.TestCase):
    def test_suffix_matching(self):
        self.assertTrue(host_matches("github.com", ["github.com"]))
        self.assertTrue(host_matches("api.github.com", ["github.com"]))
        self.assertTrue(host_matches("GitHub.COM.", ["github.com"]))
        self.assertTrue(host_matches("a.b.com", [".b.com"]))
        self.assertFalse(host_matches("b.com", [".b.com"]))
        self.assertFalse(host_matches("notgithub.com", ["github.com"]))
        self.assertFalse(host_matches("github.com.evil.net", ["github.com"]))
        self.assertTrue(host_matches("anything.net", ["*"]))

    def test_private_and_public(self):
        for host in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.5.5",
                     "169.254.1.1", "100.64.0.1", "::1", "fe80::1", "fd00::1", "0.0.0.0"):
            with self.subTest(host=host):
                self.assertTrue(is_private_address(host))
        for host in ("8.8.8.8", "1.1.1.1", "2606:4700::1111", "github.com"):
            with self.subTest(host=host):
                self.assertFalse(is_private_address(host))

    def test_helpers(self):
        self.assertEqual(normalize_host("  [::1]  "), "::1")
        self.assertEqual(split_list("a.com, b.com;c.com\nd.com"), ["a.com", "b.com", "c.com", "d.com"])
        self.assertEqual(split_list(None), [])
        self.assertEqual(split_list(["a.com", "b.com,c.com"]), ["a.com", "b.com", "c.com"])


# --------------------------------------------------------------------- WS 层
class WsFrameTests(unittest.TestCase):
    def test_mask_roundtrip(self):
        key = b"\x01\x02\x03\x04"
        data = os.urandom(1000)
        self.assertEqual(mask_payload(mask_payload(data, key), key), data)
        self.assertEqual(mask_payload(b"", key), b"")

    def test_client_frame_and_parser_roundtrip(self):
        parser = FrameParser(require_mask=True)
        parser.feed(encode_frame(OP_BINARY, b"payload", mask=True))
        self.assertEqual(parser.next_message(), (OP_BINARY, b"payload"))
        self.assertIsNone(parser.next_message())

    def test_parser_reassembles_fragments(self):
        parser = FrameParser()
        parser.feed(encode_frame(OP_TEXT, b"he", mask=False, fin=False))
        parser.feed(encode_frame(OP_CONTINUATION, b"llo", mask=False, fin=True))
        self.assertEqual(parser.next_message(), (OP_TEXT, b"hello"))

    def test_parser_handles_control_frames_and_sizes(self):
        parser = FrameParser()
        parser.feed(encode_frame(OP_PING, b"abc", mask=False))
        self.assertEqual(parser.next_message(), (OP_PING, b"abc"))
        big = os.urandom(70000)  # 超过 16 位长度上限，走 64 位分支
        parser.feed(encode_frame(OP_BINARY, big, mask=True))
        opcode, payload = parser.next_message()
        self.assertEqual(opcode, OP_BINARY)
        self.assertEqual(payload, big)

    def test_parser_rejects_oversized_and_bad_frames(self):
        parser = FrameParser(max_message=8)
        parser.feed(encode_frame(OP_BINARY, b"x" * 9, mask=False))
        with self.assertRaises(WebSocketError):
            parser.next_message()
        rsv = FrameParser()
        rsv.feed(bytes([0x41, 0x00]))  # RSV1 置位
        with self.assertRaises(WebSocketError):
            rsv.next_message()
        unmasked = FrameParser(require_mask=True)
        unmasked.feed(encode_frame(OP_BINARY, b"x", mask=False))
        with self.assertRaises(WebSocketError):
            unmasked.next_message()

    def test_accept_key_known_vector(self):
        # RFC 6455 里的示例
        self.assertEqual(websocket_accept("dGhlIHNhbXBsZSBub25jZQ=="),
                         "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_http_response_and_handshake_check(self):
        key = new_key()
        raw = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n"
               "Sec-WebSocket-Protocol: tsu.v1\r\n\r\nEXTRA" % websocket_accept(key)).encode()
        status, headers, consumed = parse_http_response(raw)
        self.assertEqual(status, 101)
        self.assertEqual(raw[consumed:], b"EXTRA")
        self.assertIsNone(check_handshake(status, headers, key, "tsu.v1"))
        with self.assertRaises(WebSocketError):
            check_handshake(404, headers, key, "tsu.v1")
        with self.assertRaises(WebSocketError):
            check_handshake(101, headers, "wrong-key", "tsu.v1")

    def test_close_payload(self):
        self.assertEqual(parse_close_payload(b"\x03\xe8bye"), (1000, "bye"))
        self.assertEqual(parse_close_payload(b""), (1005, ""))


# --------------------------------------------------------------------- 分流
class SplitRouterTests(unittest.TestCase):
    def test_auto_mode_by_route(self):
        self.assertEqual(SplitRouter("auto", route_name="cf-relay").mode, "smart")
        self.assertEqual(SplitRouter("auto", route_name="self-relay").mode, "all")
        self.assertEqual(SplitRouter("auto", route_name="tor-meek").mode, "off")

    def test_smart_mode_decisions(self):
        router = SplitRouter("smart", route_name="cf-relay")
        self.assertEqual(router.decide("github.com", 443), TUNNEL)
        self.assertEqual(router.decide("objects.githubusercontent.com", 443), TUNNEL)
        self.assertEqual(router.decide("huggingface.co", 443), TUNNEL)
        self.assertEqual(router.decide("baidu.com", 443), DIRECT)
        self.assertEqual(router.decide("some-random-site.example", 443), DIRECT)
        # 私有地址永远直连，即便命中了代理名单
        self.assertEqual(router.decide("192.168.1.1", 80), DIRECT)

    def test_user_rules_override(self):
        router = SplitRouter("smart", route_name="cf-relay",
                             proxy_hosts=["my-internal.example"],
                             direct_hosts=["github.com"])
        self.assertEqual(router.decide("github.com", 443), DIRECT)
        self.assertEqual(router.decide("my-internal.example", 443), TUNNEL)

    def test_all_and_off_modes(self):
        self.assertEqual(SplitRouter("all", route_name="self-relay").decide("anything.example", 443),
                         TUNNEL)
        self.assertEqual(SplitRouter("off", route_name="self-relay").decide("anything.example", 443),
                         TUNNEL)
        self.assertEqual(SplitRouter("all", route_name="self-relay").decide("10.0.0.5", 22), DIRECT)
        self.assertFalse(SplitRouter("off", route_name="self-relay").enabled)
        self.assertTrue(SplitRouter("all", route_name="self-relay").enabled)

    def test_learning_hosts_are_the_proxy_list(self):
        """内置「需要辅助访问」名单必须真的覆盖任务书里点名的站点。"""
        router = SplitRouter("smart", route_name="cf-relay")
        for host in ("github.com", "raw.githubusercontent.com", "huggingface.co",
                     "registry-1.docker.io", "pypi.org", "cdn.jsdelivr.net", "arxiv.org"):
            with self.subTest(host=host):
                self.assertEqual(router.decide(host, 443), TUNNEL)

    def test_stats_and_description(self):
        router = SplitRouter("smart", route_name="cf-relay")
        router.decide("github.com", 443)
        router.decide("baidu.com", 443)
        stats = router.stats()
        self.assertEqual(stats["mode"], "smart")
        self.assertGreaterEqual(stats["hits_tunnel"], 1)
        self.assertIn("smart", router.describe())
        self.assertTrue(SplitRouter("off", route_name="tor-meek").describe())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
