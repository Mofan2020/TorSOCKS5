"""HTTP 传输层的健壮性回归测试。

重点覆盖 keep-alive 连接上的**响应体错位（desync）**问题：
一旦某个响应没被完整消费，残留字节就会被下一个请求当成状态行解析，
导致连接永久损坏。这类问题在真实 CDN（如 CDN77）上表现为
「无法解析状态行: b'0'」，每十几秒断一次。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from torsocks5.meek.channel import (  # noqa: E402
    MeekConnectError,
    MeekProtocolError,
    _Http,
    gen_session_id,
)


class _RawServer:
    """可控的 HTTP 响应服务器，用来精确复现各类响应形态。"""

    def __init__(self, responder):
        self.responder = responder
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.stop_flag = threading.Event()
        self.requests = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/" % self.listener.getsockname()[1]

    def _serve(self):
        self.listener.settimeout(0.5)
        while not self.stop_flag.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        f = conn.makefile("rb")
        try:
            while True:
                head = b""
                while b"\r\n\r\n" not in head:
                    part = f.read(1)
                    if not part:
                        return
                    head += part
                body_len = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        body_len = int(line.split(b":", 1)[1])
                f.read(body_len) if body_len else None
                with self.lock:
                    self.requests += 1
                    index = self.requests
                payload = self.responder(index)
                if payload is None:
                    conn.close()
                    return
                conn.sendall(payload)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self.stop_flag.set()
        try:
            self.listener.close()
        except OSError:
            pass


class KeepAliveDesyncTest(unittest.TestCase):
    """核心回归：连续请求不得因响应体错位而崩。"""

    def _run(self, responder, count=40):
        server = _RawServer(responder)
        try:
            http = _Http(server.url, read_timeout=10, user_agent="")
            session = gen_session_id()
            received = []
            for i in range(count):
                status, data = http.post(b"x" * 32, session)
                self.assertEqual(status, 200, "第 %d 次请求状态码异常" % (i + 1))
                received.append(data)
            http.close()
            return received
        finally:
            server.stop()

    def test_content_length_normal(self):
        got = self._run(lambda i: (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
            b"Content-Length: 3\r\n\r\nabc"
        ))
        self.assertEqual(len(got), 40)
        self.assertTrue(all(item == b"abc" for item in got))

    def test_chunked_responses(self):
        """chunked 是最危险的形态：必须读到终止块，否则连接错位。"""
        def responder(index):
            return (
                b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
                b"3\r\nabc\r\n"
                b"2\r\nde\r\n"
                b"0\r\n\r\n"
            )
        got = self._run(responder)
        self.assertTrue(all(item == b"abcde" for item in got))

    def test_chunked_with_trailers(self):
        def responder(index):
            return (
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"2\r\nhi\r\n"
                b"0\r\nX-Checksum: 1\r\n\r\n"
            )
        got = self._run(responder)
        self.assertTrue(all(item == b"hi" for item in got))

    def test_oversized_content_length_is_drained(self):
        """超出 65536 的响应体必须丢弃剩余部分，而不是留在流里污染下一个请求。"""
        big = b"Z" * (65536 + 1000)
        def responder(index):
            return (
                b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(big)
            ) + big
        got = self._run(responder, count=6)
        self.assertTrue(all(len(item) == 65536 for item in got))

    def test_empty_body_repeatedly(self):
        """空响应（纯轮询）是 meek 的常态，必须稳定。"""
        got = self._run(lambda i: (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
            b"Content-Length: 0\r\n\r\n"
        ), count=60)
        self.assertTrue(all(item == b"" for item in got))

    def test_truncated_response_recovers(self):
        """服务器中途断开时应抛连接错误（可重连），而不是静默错位。"""
        def responder(index):
            if index % 3 == 0:
                return b"HTTP/1.1 200 OK\r\nContent-Len"  # 截断
            return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        server = _RawServer(responder)
        try:
            http = _Http(server.url, read_timeout=5, user_agent="")
            session = gen_session_id()
            errors = 0
            oks = 0
            for _ in range(20):
                try:
                    status, data = http.post(b"y" * 16, session)
                    self.assertEqual(status, 200)
                    self.assertEqual(data, b"ok")
                    oks += 1
                except MeekConnectError:
                    errors += 1
                except OSError:
                    errors += 1
            http.close()
            self.assertGreater(oks, 0)
            self.assertGreater(errors, 0, "截断响应应当被识别为连接错误")
        finally:
            server.stop()

    def test_status_codes_do_not_desync(self):
        """非 200 响应（403 等）后连接仍应可用。"""
        def responder(index):
            if index % 4 == 0:
                return (
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 9\r\n\r\nForbidden"
                )
            return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        server = _RawServer(responder)
        try:
            http = _Http(server.url, read_timeout=10, user_agent="")
            session = gen_session_id()
            statuses = []
            for _ in range(24):
                status, _data = http.post(b"z" * 8, session)
                statuses.append(status)
            http.close()
            self.assertIn(403, statuses)
            self.assertIn(200, statuses)
        finally:
            server.stop()

    def test_no_length_keepalive_is_rejected_cleanly(self):
        """既无 Content-Length 也非 chunked 的响应必须报错而不是猜边界。"""
        server = _RawServer(lambda i: b"HTTP/1.1 200 OK\r\n\r\nbody-without-length")
        try:
            http = _Http(server.url, read_timeout=5, user_agent="")
            with self.assertRaises((MeekProtocolError, MeekConnectError)):
                http.post(b"a" * 4, gen_session_id())
            http.close()
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
