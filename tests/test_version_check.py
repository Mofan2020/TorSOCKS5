"""版本更新检查单元测试：版本比较、缓存策略、失败静默、状态输出。

全部离线：网络请求用注入的 fetcher 或本机临时 HTTP 服务器替代。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from torsocks5 import version_check as vc


class VersionCompareTest(unittest.TestCase):
    def test_parse_v_prefix_and_padding(self):
        self.assertEqual(vc.parse_version("v2.1.0"), (2, 1, 0))
        self.assertEqual(vc.parse_version("2.1"), (2, 1, 0))
        self.assertEqual(vc.parse_version("  V1.2.3 "), (1, 2, 3))

    def test_parse_multi_digit_segments(self):
        self.assertGreater(vc.parse_version("2.10.0"), vc.parse_version("2.9.9"))

    def test_parse_prerelease_suffix(self):
        self.assertEqual(vc.parse_version("2.1.0-beta.1"), (2, 1, 0))

    def test_parse_garbage_is_zero(self):
        self.assertEqual(vc.parse_version("not-a-version"), (0, 0, 0))
        self.assertEqual(vc.parse_version(""), (0, 0, 0))

    def test_has_newer_matrix(self):
        self.assertTrue(vc.has_newer("2.0.0", "v2.1.0"))
        self.assertFalse(vc.has_newer("2.1.0", "2.1.0"))
        self.assertFalse(vc.has_newer("2.2.0", "2.1.0"))
        self.assertFalse(vc.has_newer(None, "2.1.0"))
        self.assertFalse(vc.has_newer("2.0.0", None))
        self.assertFalse(vc.has_newer("", ""))


class VersionCheckTest(unittest.TestCase):
    def setUp(self):
        vc.reset()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(vc.reset)
        self.cache = os.path.join(self._tmp.name, "version_check.json")

    def _start(self, fetcher, **kwargs):
        """启动检查并登记清理（先等线程结束再重置状态）。"""
        results = []
        started = vc.start(
            "2.0.0",
            cache_path=self.cache,
            on_result=results.append,
            fetcher=fetcher,
            **kwargs,
        )
        self.addCleanup(vc.wait, 5.0)
        return started, results

    # ------------------------------------------------------------ 生命周期
    def test_disabled_does_not_start(self):
        started, results = self._start(lambda: "v9.9.9", enabled=False)
        self.assertFalse(started)
        self.assertTrue(vc.wait(1.0))
        self.assertFalse(vc.get_status()["enabled"])
        self.assertEqual(results, [])

    def test_start_is_idempotent_while_running(self):
        gate = threading.Event()

        def fetcher():
            gate.wait(5)
            return "v9.9.9"

        first, _ = self._start(fetcher)
        self.assertTrue(first)
        second = vc.start("2.0.0", cache_path=self.cache,
                          fetcher=lambda: "v0.0.1")
        self.assertFalse(second)  # 已有线程在跑，不重复启动
        gate.set()
        self.assertTrue(vc.wait(5.0))
        self.assertEqual(vc.get_status()["latest"], "v9.9.9")

    # ------------------------------------------------------------ 正常路径
    def test_fetch_success_updates_state_and_cache(self):
        calls = []

        def fetcher():
            calls.append(1)
            return "v9.9.9"

        started, results = self._start(fetcher)
        self.assertTrue(started)
        self.assertTrue(vc.wait(5.0))

        status = vc.get_status()
        self.assertEqual(status["latest"], "v9.9.9")
        self.assertTrue(status["has_update"])
        self.assertEqual(status["current"], "2.0.0")
        self.assertIsNotNone(status["checked_at"])
        self.assertIsNone(status["error"])
        self.assertEqual(len(calls), 1)
        self.assertTrue(results and results[0]["has_update"])
        with open(self.cache, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["latest"], "v9.9.9")

    def test_current_newer_than_latest_no_update(self):
        self._start(lambda: "v1.0.0")
        self.assertTrue(vc.wait(5.0))
        self.assertFalse(vc.get_status()["has_update"])

    # ------------------------------------------------------------ 缓存
    def test_fresh_cache_skips_network(self):
        with open(self.cache, "w", encoding="utf-8") as handle:
            json.dump({"checked_at": time.time(), "latest": "v9.9.9",
                       "current": "2.0.0"}, handle)

        def fetcher():
            raise AssertionError("缓存命中时不应发请求")

        self._start(fetcher)
        self.assertTrue(vc.wait(5.0))
        status = vc.get_status()
        self.assertEqual(status["latest"], "v9.9.9")
        self.assertTrue(status["has_update"])
        self.assertIsNone(status["error"])

    def test_expired_cache_refetches(self):
        with open(self.cache, "w", encoding="utf-8") as handle:
            json.dump({"checked_at": time.time() - 25 * 3600,
                       "latest": "v9.0.0", "current": "2.0.0"}, handle)
        self._start(lambda: "v9.9.9")
        self.assertTrue(vc.wait(5.0))
        self.assertEqual(vc.get_status()["latest"], "v9.9.9")

    def test_corrupt_cache_treated_as_miss(self):
        with open(self.cache, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self._start(lambda: "v9.9.9")
        self.assertTrue(vc.wait(5.0))
        status = vc.get_status()
        self.assertEqual(status["latest"], "v9.9.9")
        self.assertIsNone(status["error"])

    # ------------------------------------------------------------ 失败静默
    def test_network_failure_is_silent(self):
        def fetcher():
            raise OSError("network unreachable")

        started, results = self._start(fetcher)  # 绝不能抛异常
        self.assertTrue(vc.wait(5.0))
        status = vc.get_status()
        self.assertIsNotNone(status["error"])
        self.assertIn("network unreachable", status["error"])
        self.assertFalse(status["has_update"])
        self.assertIsNone(status["latest"])
        self.assertEqual(len(results), 1)  # 回调照常返回（带 error）
        self.assertIsNotNone(results[0]["error"])

    def test_on_result_callback_exception_is_swallowed(self):
        def boom(_status):
            raise RuntimeError("callback bug")

        vc.start("2.0.0", cache_path=self.cache, fetcher=lambda: "v9.9.9",
                 on_result=boom)
        self.addCleanup(vc.wait, 5.0)
        self.assertTrue(vc.wait(5.0))  # 回调抛错不能杀死线程/影响状态
        self.assertEqual(vc.get_status()["latest"], "v9.9.9")

    # ------------------------------------------------------------ 真实 HTTP
    def test_fetch_latest_tag_via_env_url(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"tag_name": "v8.8.8"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        os.environ[vc.ENV_URL] = ("http://127.0.0.1:%d/latest"
                                  % server.server_address[1])
        self.addCleanup(os.environ.pop, vc.ENV_URL, None)
        self.assertEqual(vc.fetch_latest_tag(timeout=5), "v8.8.8")


if __name__ == "__main__":
    unittest.main()
