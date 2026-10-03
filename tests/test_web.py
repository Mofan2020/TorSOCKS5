"""Web 面板单元测试：认证、页面、JSON API、CSRF 防护、SSE、配置保存。"""

from __future__ import annotations

import base64
import json
import os
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from torsocks5 import config as config_mod
from torsocks5.web import WebPanel


class _StubLogger:
    """极简日志桩：实现 sink 相关接口。"""

    def __init__(self) -> None:
        self.messages = []
        self.sinks = []

    def _rec(self, level: str, msg: str, *args) -> None:
        self.messages.append((level, msg))

    def debug(self, msg, *args): self._rec("debug", msg)
    def info(self, msg, *args): self._rec("info", msg)
    def warn(self, msg, *args): self._rec("warn", msg)
    def error(self, msg, *args): self._rec("error", msg)
    def ok(self, msg, *args): self._rec("ok", msg)

    def add_sink(self, sink): self.sinks.append(sink)
    def remove_sink(self, sink):
        if sink in self.sinks:
            self.sinks.remove(sink)


class _StubSocks:
    def stats_dict(self):
        return {
            "listen": "127.0.0.1:9051", "route_auth": False,
            "max_connections": 512, "current_connections": 2,
            "total_connections": 10, "bytes_up": 1024, "bytes_down": 2048,
            "uptime": 60.0,
        }


class _StubRoute:
    name = "self-relay"
    title = "自建中继"

    def status(self):
        return "已就绪"

    def stats(self):
        return {"split": {"tunnel": 3, "direct": 7},
                "nodes": [{"url": "ws://127.0.0.1:9052/tsu", "state": "healthy",
                           "active_streams": 1, "weight": 3,
                           "success_rate": 1.0, "avg_latency_ms": 5}]}


class _StubHot:
    def __init__(self):
        self.switched = None

    def switch_route(self, name):
        self.switched = name
        return {"success": True, "route": name}

    def reload(self, force=False):
        return {"success": True, "changes": [{"key": "proxy.port", "old": 1, "new": 2}],
                "warnings": [], "error": None}

    def get_status(self):
        return {"enabled": True, "signal": "SIGHUP", "api_enabled": True,
                "reload_count": 0, "last_error": None}


def _http(url, user="", password="", method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=body)
    if user:
        token = base64.b64encode(("%s:%s" % (user, password)).encode("utf-8"))
        req.add_header("Authorization", "Basic " + token.decode("ascii"))
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


class WebPanelTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_path = os.path.join(self._tmp.name, "config.toml")
        self._write_config()
        self.config = config_mod.Config.load(self.config_path)
        self.logger = _StubLogger()
        self.hot = _StubHot()
        self.panel = WebPanel(
            self.config,
            self.logger,  # type: ignore[arg-type]
            get_socks_server=lambda: _StubSocks(),
            get_route=lambda: _StubRoute(),
            get_hot_manager=lambda: self.hot,
            version="test",
        )
        self.assertTrue(self.panel.start())
        self.addCleanup(self.panel.stop)
        self.base = "http://127.0.0.1:%d" % self.panel.port

    def _write_config(self):
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(
                "[proxy]\n"
                'route = "self-relay"\n'
                "port = 9051\n"
                "\n[bridges]\n"
                'file = "%s"\n'
                "\n[web]\n"
                "enabled = true\n"
                "port = 0\n"
                'username = "admin"\n'
                'password = "secret"\n'
                % os.path.join(self._tmp.name, "bridges.toml")
            )

    # ------------------------------------------------------------ 认证
    def test_auth_required_for_pages_and_api(self):
        for path in ("/", "/api/status", "/api/config", "/api/bridges"):
            code, _ = _http(self.base + path)
            self.assertEqual(code, 401, path)
        # 密码错误 → 401
        code, _ = _http(self.base + "/api/status", "admin", "wrong")
        self.assertEqual(code, 401)
        # 正确 → 200
        code, body = _http(self.base + "/api/status", "admin", "secret")
        self.assertEqual(code, 200)
        self.assertIn("version", json.loads(body))

    # ------------------------------------------------------------ 页面
    def test_pages_render(self):
        for path, marker in (("/", "仪表盘"), ("/routes", "切换路由"),
                             ("/bridges", "添加网桥"), ("/config", "保存并热重载"),
                             ("/logs", "EventSource")):
            code, body = _http(self.base + path, "admin", "secret")
            self.assertEqual(code, 200, path)
            self.assertIn("<!DOCTYPE html>", body, path)
            self.assertIn(marker, body, path)
            # CDN 引入（零构建）
            self.assertIn("cdn.tailwindcss.com", body, path)
        # 未知路径 → 404
        code, _ = _http(self.base + "/nope", "admin", "secret")
        self.assertEqual(code, 404)

    # ------------------------------------------------------------ 状态与路由
    def test_status_shape(self):
        code, body = _http(self.base + "/api/status", "admin", "secret")
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(data["proxy"]["current_connections"], 2)
        self.assertEqual(data["route"]["name"], "self-relay")
        self.assertEqual(data["split"]["tunnel"], 3)
        self.assertEqual(data["hotreload"]["signal"], "SIGHUP")

    def test_routes_list_and_switch(self):
        code, body = _http(self.base + "/api/routes", "admin", "secret")
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(data["current"], "self-relay")
        self.assertTrue(data["switchable"])
        names = [row[0] for row in data["available"]]
        self.assertIn("tor-meek", names)
        self.assertEqual(data["nodes"][0]["state"], "healthy")

        payload = json.dumps({"route": "cf-relay"}).encode("utf-8")
        code, body = _http(self.base + "/api/routes/switch", "admin", "secret",
                           method="POST", body=payload,
                           headers={"Content-Type": "application/json"})
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertTrue(data["success"])
        self.assertEqual(self.hot.switched, "cf-relay")

        # 未知路由 → 拒绝
        payload = json.dumps({"route": "nope"}).encode("utf-8")
        code, body = _http(self.base + "/api/routes/switch", "admin", "secret",
                           method="POST", body=payload,
                           headers={"Content-Type": "application/json"})
        self.assertFalse(json.loads(body)["success"])

    # ------------------------------------------------------------ CSRF
    def test_cross_site_post_rejected(self):
        payload = json.dumps({"route": "cf-relay"}).encode("utf-8")
        code, body = _http(self.base + "/api/routes/switch", "admin", "secret",
                           method="POST", body=payload,
                           headers={"Content-Type": "application/json",
                                    "Origin": "http://evil.example"})
        self.assertFalse(json.loads(body)["success"])
        self.assertIn("跨站", json.loads(body)["error"])
        # 同源 Origin 放行
        code, body = _http(self.base + "/api/routes/switch", "admin", "secret",
                           method="POST", body=payload,
                           headers={"Content-Type": "application/json",
                                    "Origin": self.base})
        self.assertTrue(json.loads(body)["success"])

    # ------------------------------------------------------------ 网桥
    def _write_bridges(self):
        path = os.path.join(self._tmp.name, "bridges.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('[[bridge]]\n'
                         'transport = "meek"\n'
                         'address = "192.0.2.1:80"\n'
                         '[bridge.args]\n'
                         'url = "http://192.0.2.1:80"\n'
                         'front = "cdn.example"\n')
        return path

    def test_bridges_add_list_rm(self):
        self._write_bridges()
        code, body = _http(self.base + "/api/bridges", "admin", "secret")
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(data["active"], 1)
        self.assertEqual(data["bridges"][0]["transport"], "meek")

        # 添加
        payload = json.dumps({"action": "add", "line":
                              "meek 0.0.2.0:3 url=http://192.0.2.2:80 front=x.example"}).encode()
        code, body = _http(self.base + "/api/bridges", "admin", "secret",
                           method="POST", body=payload,
                           headers={"Content-Type": "application/json"})
        self.assertTrue(json.loads(body)["success"], body)

        # 删除（按地址子串）
        payload = json.dumps({"action": "rm", "needle": "192.0.2.2"}).encode()
        code, body = _http(self.base + "/api/bridges", "admin", "secret",
                           method="POST", body=payload,
                           headers={"Content-Type": "application/json"})
        self.assertTrue(json.loads(body)["success"], body)
        code, body = _http(self.base + "/api/bridges", "admin", "secret")
        self.assertEqual(json.loads(body)["active"], 1)

    # ------------------------------------------------------------ 配置
    def test_config_get_put_validation(self):
        code, body = _http(self.base + "/api/config", "admin", "secret")
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(data["path"], self.config_path)
        self.assertIn("[proxy]", data["content"])

        # 非法 TOML → 拒绝，且不写文件
        with open(self.config_path, encoding="utf-8") as handle:
            original = handle.read()
        payload = json.dumps({"content": "[proxy\nport = "}).encode()
        code, body = _http(self.base + "/api/config", "admin", "secret",
                           method="PUT", body=payload,
                           headers={"Content-Type": "application/json"})
        data = json.loads(body)
        self.assertFalse(data["success"])
        self.assertIn("TOML", data["error"])
        with open(self.config_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)

        # 合法内容 → 写入并热重载（stub reload 成功）
        payload = json.dumps({"content": original + "\n# edited\n"}).encode()
        code, body = _http(self.base + "/api/config", "admin", "secret",
                           method="PUT", body=payload,
                           headers={"Content-Type": "application/json"})
        data = json.loads(body)
        self.assertTrue(data["success"], data)
        self.assertTrue(data["reloaded"])
        with open(self.config_path, encoding="utf-8") as handle:
            self.assertIn("# edited", handle.read())

        # 触发重载
        code, body = _http(self.base + "/api/reload", "admin", "secret", method="POST")
        self.assertTrue(json.loads(body)["success"])

    # ------------------------------------------------------------ 日志
    def test_logs_history_and_stream(self):
        # 通过 sink 推两条日志
        for sink in self.logger.sinks:
            sink("info", "hello web")
        # 面板自身没挂 sink？直接推到 log_stream
        self.panel.log_stream.publish("warn", "direct line")
        code, body = _http(self.base + "/api/logs?limit=10", "admin", "secret")
        entries = json.loads(body)["entries"]
        msgs = [entry["msg"] for entry in entries]
        self.assertIn("direct line", msgs)

        # SSE：连上后应先收到握手注释，再收到新日志
        sock = socket.create_connection(("127.0.0.1", self.panel.port), timeout=5)
        try:
            request = ("GET /api/logs/stream HTTP/1.1\r\n"
                       "Host: 127.0.0.1:%d\r\n"
                       "Authorization: Basic %s\r\n"
                       "Accept: text/event-stream\r\n\r\n"
                       % (self.panel.port,
                          base64.b64encode(b"admin:secret").decode("ascii")))
            sock.sendall(request.encode("ascii"))
            sock.settimeout(5)
            # 反复推送直到订阅建立（SSE 只转发订阅后的消息），同时读取响应
            received = b""
            deadline = time.time() + 5
            while time.time() < deadline and b"sse-line-1" not in received:
                self.panel.log_stream.publish("info", "sse-line-1")
                sock.settimeout(0.5)
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                received += chunk
            self.assertIn(b"text/event-stream", received)
            self.assertIn(b"sse-line-1", received)
        finally:
            sock.close()

    # ------------------------------------------------------------ 生命周期
    def test_disabled_panel_does_not_start(self):
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write("[web]\nenabled = false\n")
        config = config_mod.Config.load(self.config_path)
        panel = WebPanel(config, self.logger,  # type: ignore[arg-type]
                         get_socks_server=lambda: None,
                         get_route=lambda: None,
                         get_hot_manager=lambda: None)
        self.assertFalse(panel.enabled)
        self.assertFalse(panel.start())


if __name__ == "__main__":
    unittest.main()
