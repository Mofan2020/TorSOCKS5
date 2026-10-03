"""配置热重载的单元测试：展平、变更检测、重建判定、HTTP API。"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request

from torsocks5 import config as config_mod
from torsocks5.hotreload import HotReloadManager, _flatten, start_reload_api
from torsocks5.routes import RouteOptions


class _StubLogger:
    """极简日志桩：记录消息供断言。"""

    def __init__(self) -> None:
        self.messages = []

    def _rec(self, level: str, msg: str, *args) -> None:
        self.messages.append((level, msg))

    def debug(self, msg, *args): self._rec("debug", msg)
    def info(self, msg, *args): self._rec("info", msg)
    def warn(self, msg, *args): self._rec("warn", msg)
    def error(self, msg, *args): self._rec("error", msg)
    def ok(self, msg, *args): self._rec("ok", msg)

    def find(self, level: str, fragment: str):
        return [m for lv, m in self.messages if lv == level and fragment in m]


def _http(url: str, user: str = "", password: str = "", method: str = "GET"):
    req = urllib.request.Request(url, method=method)
    if user or password:
        token = base64.b64encode(("%s:%s" % (user, password)).encode("utf-8"))
        req.add_header("Authorization", "Basic " + token.decode("ascii"))
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body.decode("utf-8"))
        except ValueError:
            return exc.code, {}


class HotReloadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "config.toml")

    def _write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _manager(self) -> HotReloadManager:
        config = config_mod.Config.load(self.path)
        return HotReloadManager(
            config,
            _StubLogger(),
            None,
            RouteOptions(),
            get_connector=lambda: None,
            set_connector=lambda _c: None,
            get_upstream_socks=lambda: None,
            set_upstream_socks=lambda _u: None,
            get_split_router=lambda: None,
            set_split_router=lambda _r: None,
        )

    # ------------------------------------------------------------ 展平与检测
    def test_flatten_nested(self):
        flat = _flatten({"proxy": {"route": "tor-meek", "port": 9051},
                         "flags": ["a", "b"]})
        self.assertEqual(flat["proxy.route"], "tor-meek")
        self.assertEqual(flat["proxy.port"], 9051)
        self.assertEqual(flat["flags"], ["a", "b"])

    def test_detect_nested_changes(self):
        self._write('[proxy]\nroute = "tor-meek"\nport = 9051\n')
        manager = self._manager()
        old = manager.config.data
        self._write('[proxy]\nroute = "self-relay"\nport = 9051\n')
        new = config_mod.load_toml(self.path)
        changes = manager._detect_changes(old, new)
        keys = [c["key"] for c in changes]
        self.assertIn("proxy.route", keys)
        # 未变的键不应出现
        self.assertNotIn("proxy.port", keys)

    def test_rebuild_needed_for_relay_nodes_and_split(self):
        manager = self._manager()
        self.assertTrue(manager._needs_route_rebuild(
            [{"key": "self_relay.nodes", "old": None, "new": []}]))
        self.assertTrue(manager._needs_route_rebuild(
            [{"key": "split.mode", "old": "auto", "new": "all"}]))
        self.assertTrue(manager._needs_route_rebuild(
            [{"key": "cf_relay.lb_strategy", "old": "weighted_rr",
              "new": "least_conn"}]))
        # 纯运行期参数不需要重建路由
        self.assertFalse(manager._needs_route_rebuild(
            [{"key": "proxy.max_connections", "old": 512, "new": 256}]))

    def test_reload_applies_runtime_changes(self):
        self._write('[proxy]\nmax_connections = 512\n')
        manager = self._manager()
        applied = []
        manager.apply_runtime = lambda changes: applied.extend(changes)
        # 改配置后重载
        self._write('[proxy]\nmax_connections = 256\n')
        result = manager.reload()
        self.assertTrue(result["success"], result.get("error"))
        keys = [c["key"] for c in applied]
        self.assertIn("proxy.max_connections", keys)
        self.assertEqual(manager.get_status()["reload_count"], 1)

    def test_reload_no_changes_is_success(self):
        self._write('[proxy]\nroute = "tor-meek"\n')
        manager = self._manager()
        result = manager.reload(force=False)
        self.assertTrue(result["success"])
        self.assertIn("配置无变化", result["warnings"])

    # ------------------------------------------------------------ HTTP API
    def test_api_requires_auth(self):
        self._write('[proxy]\nusername = "admin"\npassword = "secret"\n')
        manager = self._manager()
        server = start_reload_api(manager, port=0)
        self.assertIsNotNone(server)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        base = "http://127.0.0.1:%d/api/config/reload" % port

        # 未带凭据 → 401
        code, _ = _http(base)
        self.assertEqual(code, 401)
        # 错误凭据 → 401
        code, _ = _http(base, "admin", "wrong")
        self.assertEqual(code, 401)
        # 正确凭据 → 200，返回状态
        code, payload = _http(base, "admin", "secret")
        self.assertEqual(code, 200)
        self.assertIn("reload_count", payload)
        # POST 触发重载
        code, payload = _http(base, "admin", "secret", method="POST")
        self.assertEqual(code, 200)
        self.assertTrue(payload.get("success"), payload)
        # 未知路径 → 404
        code, _ = _http("http://127.0.0.1:%d/other" % port, "admin", "secret")
        self.assertEqual(code, 404)

    def test_api_rejects_when_no_credentials(self):
        self._write('[proxy]\n')  # 未设置用户名密码
        manager = self._manager()
        server = start_reload_api(manager, port=0)
        self.assertIsNotNone(server)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        base = "http://127.0.0.1:%d/api/config/reload" % port
        code, payload = _http(base, "admin", "secret")
        self.assertEqual(code, 403)
        self.assertIn("proxy.username", payload.get("error", ""))

    def test_api_disabled_returns_none(self):
        self._write('[hotreload]\napi_enabled = false\n')
        manager = self._manager()
        self.assertIsNone(start_reload_api(manager, port=0))


if __name__ == "__main__":
    unittest.main()
