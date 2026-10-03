"""配置向导测试：环境检测、配置文本就地更新、自启方案、面板向导 API。"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from unittest import mock

from torsocks5 import config as config_mod
from torsocks5 import service as service_mod
from torsocks5.web import WebPanel
from torsocks5.web import wizard as wizard_mod


class _StubLogger:
    def __init__(self):
        self.messages = []
        self.sinks = []

    def _rec(self, level, msg, *args):
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


class _StubRoute:
    name = "tor-meek"

    def status(self):
        return "已就绪"


class _StubHot:
    def __init__(self):
        self.reload_calls = 0

    def reload(self, force=False):
        self.reload_calls += 1
        return {"success": True,
                "changes": [{"key": "proxy.route", "old": "a", "new": "b"}],
                "warnings": [], "error": None}

    def get_status(self):
        return {"enabled": True}


def _http(url, user="", password="", method="GET", body=None, headers=None):
    import urllib.error
    import urllib.request
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


# ====================================================================== 环境检测
class EnvChecksTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = os.path.join(self._tmp.name, "config.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "[proxy]\nroute = \"self-relay\"\nport = 9051\n"
                "\n[self_relay]\nurl = \"ws://127.0.0.1:9052/tsu\"\n")
        self.config = config_mod.Config.load(path)
        patcher = mock.patch(
            "torsocks5.web.wizard.tor_find.find_tor",
            return_value="/opt/homebrew/bin/tor")
        self._tor = patcher.start()
        self.addCleanup(patcher.stop)

    def test_checks_shape_and_keys(self):
        result = wizard_mod.env_checks(self.config, route_name="self-relay",
                                       listen="", bridge_count=0)
        keys = [c["key"] for c in result["checks"]]
        for expected in ("python", "tor", "config", "port", "route",
                         "relay", "bridges", "hotreload"):
            self.assertIn(expected, keys)
        for check in result["checks"]:
            self.assertEqual(
                set(check), {"key", "label", "ok", "detail", "fix"})
            self.assertIsInstance(check["ok"], bool)
            if check["ok"]:
                self.assertEqual(check["fix"], "")
        self.assertTrue(result["checks"][0]["ok"])  # python >= 3.10
        self.assertEqual(result["python"].count("."), 2)
        self.assertTrue(result["config_path"].endswith("config.toml"))

    def test_route_options_and_form_prefill(self):
        result = wizard_mod.env_checks(self.config, route_name="self-relay")
        names = [row["name"] for row in result["route_options"]]
        self.assertIn("tor-meek", names)
        self.assertIn("cf-relay", names)
        self.assertEqual(result["form"]["route"], "self-relay")
        self.assertEqual(result["form"]["port"], 9051)
        self.assertEqual(result["relay"]["kind"], "self")
        self.assertEqual(result["relay"]["url"], "ws://127.0.0.1:9052/tsu")

    def test_relay_missing_url_fails_with_fix(self):
        self.config.data["self_relay"] = {}
        result = wizard_mod.env_checks(self.config, route_name="self-relay")
        relay = next(c for c in result["checks"] if c["key"] == "relay")
        self.assertFalse(relay["ok"])
        self.assertIn("第 3 步", relay["fix"])
        self.assertEqual(result["relay"]["kind"], "self")

    def test_tor_meek_requires_bridges(self):
        path = os.path.join(self._tmp.name, "meek.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("[proxy]\nroute = \"tor-meek\"\n")
        config = config_mod.Config.load(path)
        # 0 条网桥 → 不通过
        result = wizard_mod.env_checks(config, route_name="tor-meek",
                                       bridge_count=0)
        bridge = next(c for c in result["checks"] if c["key"] == "bridges")
        self.assertFalse(bridge["ok"])
        self.assertIn("第 3 步", bridge["fix"])
        # 有网桥 → 通过
        result = wizard_mod.env_checks(config, route_name="tor-meek",
                                       bridge_count=2)
        bridge = next(c for c in result["checks"] if c["key"] == "bridges")
        self.assertTrue(bridge["ok"])
        self.assertEqual(result["relay"]["kind"], "none")


# ============================================================== 配置文本更新
class ApplyUpdatesTest(unittest.TestCase):
    def test_replace_existing_key_preserves_comments(self):
        text = (
            "# 全局注释\n"
            "[proxy]\n"
            "# 路由说明注释\n"
            'route = "tor-meek"\n'
            "port = 9051\n"
            'listen = "127.0.0.1"\n'
            "\n[web]\nenabled = false\n")
        new_text = wizard_mod.apply_updates(
            text, {"proxy": {"port": 1080, "route": "self-relay"}})
        self.assertIn("# 全局注释", new_text)
        self.assertIn("# 路由说明注释", new_text)
        self.assertIn("port = 1080", new_text)
        self.assertIn('route = "self-relay"', new_text)
        self.assertIn('listen = "127.0.0.1"', new_text)  # 未涉及的键不动
        self.assertIn("enabled = false", new_text)
        parsed = config_mod.loads(new_text)
        self.assertEqual(parsed["proxy"]["port"], 1080)
        self.assertEqual(parsed["proxy"]["route"], "self-relay")

    def test_insert_missing_key_into_section(self):
        text = "[proxy]\nroute = \"tor-meek\"\n\n[web]\nenabled = false\n"
        new_text = wizard_mod.apply_updates(text, {"proxy": {"port": 9051}})
        # 插在节尾（web 节之前），且紧跟节内最后一行内容
        self.assertIn('route = "tor-meek"\nport = 9051\n', new_text)
        self.assertIn("[web]", new_text)
        self.assertEqual(config_mod.loads(new_text)["proxy"]["port"], 9051)

    def test_append_missing_section(self):
        text = "# 注释开头\n[proxy]\nport = 9051\n"
        new_text = wizard_mod.apply_updates(
            text, {"cf_relay": {"url": "https://r.example.com"}})
        self.assertIn("# 注释开头", new_text)
        self.assertIn("[cf_relay]", new_text)
        self.assertIn('url = "https://r.example.com"', new_text)
        parsed = config_mod.loads(new_text)
        self.assertEqual(parsed["cf_relay"]["url"], "https://r.example.com")

    def test_empty_text_creates_sections(self):
        new_text = wizard_mod.apply_updates("", {"proxy": {"port": 9051}})
        self.assertEqual(config_mod.loads(new_text)["proxy"]["port"], 9051)
        self.assertTrue(new_text.endswith("\n"))

    def test_toml_value_escaping(self):
        self.assertEqual(wizard_mod.toml_value(True), "true")
        self.assertEqual(wizard_mod.toml_value(False), "false")
        self.assertEqual(wizard_mod.toml_value(1080), "1080")
        self.assertEqual(wizard_mod.toml_value('带"引号"'),
                         '"带\\"引号\\""')
        self.assertEqual(wizard_mod.toml_value("a\\b"), '"a\\\\b"')
        self.assertEqual(wizard_mod.toml_value("中文"), '"中文"')

    def test_validate_or_error(self):
        self.assertIsNone(wizard_mod.validate_or_error("[proxy]\nport = 1\n"))
        error = wizard_mod.validate_or_error("[proxy\nport = ")
        self.assertIsNotNone(error)
        self.assertIn("解析失败", error)


# ================================================================ 自启方案
class StartupPlanTest(unittest.TestCase):
    def test_systemd_unit_with_config_path(self):
        unit = service_mod.systemd_unit(9051, "/home/u/my.toml")
        self.assertIn("ExecStart=", unit)
        self.assertIn("--config /home/u/my.toml", unit)
        self.assertIn("--port 9051", unit)

    def test_systemd_unit_default_config_omits_flag(self):
        unit = service_mod.systemd_unit(9051, "")
        self.assertNotIn("--config", unit)

    def test_launchd_plist_with_config_path(self):
        plist = service_mod.launchd_plist(9051, "/Users/u/my.toml")
        self.assertIn("<string>--config</string>", plist)
        self.assertIn("<string>/Users/u/my.toml</string>", plist)
        self.assertIn("<string>run</string>", plist)

    def test_startup_plan_shape(self):
        plan = service_mod.startup_plan(9051, "")
        for field in ("kind", "title", "filename", "content",
                      "steps", "uninstall"):
            self.assertIn(field, plan)
        self.assertIsInstance(plan["steps"], list)
        self.assertGreaterEqual(len(plan["steps"]), 2)
        self.assertTrue(plan["content"].strip())
        # 平台对应内容
        kind = service_mod.detect_kind()
        self.assertEqual(plan["kind"], kind)
        if kind == "launchd":
            self.assertIn("ProgramArguments", plan["content"])
        elif kind == "systemd":
            self.assertIn("ExecStart=", plan["content"])
        else:
            self.assertIn("schtasks", plan["content"])


# ============================================================ 面板向导 API
class WizardApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_path = os.path.join(self._tmp.name, "config.toml")
        self.bridges_path = os.path.join(self._tmp.name, "bridges.toml")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(
                "# 用户的重要注释\n"
                "[proxy]\n"
                'route = "tor-meek"\n'
                "port = 9051\n"
                'listen = "127.0.0.1"\n'
                "\n[bridges]\n"
                'file = "%s"\n'
                "\n[web]\nenabled = true\n"
                "port = 0\n"
                'username = "admin"\n'
                'password = "secret"\n' % self.bridges_path)
        self.config = config_mod.Config.load(self.config_path)
        self.logger = _StubLogger()
        self.hot = _StubHot()
        self.panel = WebPanel(
            self.config,
            self.logger,  # type: ignore[arg-type]
            get_socks_server=lambda: None,
            get_route=lambda: _StubRoute(),
            get_hot_manager=lambda: self.hot,
            version="test",
        )
        self.assertTrue(self.panel.start())
        self.addCleanup(self.panel.stop)
        self.base = "http://127.0.0.1:%d" % self.panel.port
        patcher = mock.patch(
            "torsocks5.web.wizard.tor_find.find_tor",
            return_value="/opt/homebrew/bin/tor")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, path, payload, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        return _http(self.base + path, "admin", "secret", method="POST",
                     body=json.dumps(payload).encode("utf-8"),
                     headers=headers)

    # ------------------------------------------------------------ 认证与页面
    def test_auth_required_and_page_renders(self):
        for path in ("/wizard", "/api/wizard/env", "/api/wizard/service"):
            code, _ = _http(self.base + path)
            self.assertEqual(code, 401, path)
        code, body = _http(self.base + "/wizard", "admin", "secret")
        self.assertEqual(code, 200)
        self.assertIn("环境检测", body)
        self.assertIn("开机自启", body)
        self.assertIn("wizard()", body)
        # 仪表盘/其他页面右上角均有向导入口
        code, dash = _http(self.base + "/", "admin", "secret")
        self.assertIn("配置向导", dash)

    # ------------------------------------------------------------ env
    def test_env_endpoint_shape(self):
        code, body = _http(self.base + "/api/wizard/env", "admin", "secret")
        data = json.loads(body)
        self.assertEqual(code, 200)
        keys = [c["key"] for c in data["checks"]]
        self.assertIn("python", keys)
        self.assertIn("tor", keys)
        self.assertEqual(data["config_path"], self.config_path)
        self.assertEqual(data["form"]["route"], "tor-meek")
        self.assertEqual(data["bridge_count"], 0)
        self.assertEqual(data["relay"]["kind"], "none")

    # ------------------------------------------------------------ 保存配置
    def test_config_apply_persists_and_reloads(self):
        payload = {"proxy": {"listen": "127.0.0.1", "port": 1080,
                             "route": "cf-relay"},
                   "cf_relay": {"url": "https://relay.example.com"}}
        code, body = self._post("/api/wizard/config", payload,
                                origin=self.base)
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertTrue(data["success"], data)
        self.assertTrue(data["reloaded"])
        # 注释与未涉及的键都还在
        with open(self.config_path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("# 用户的重要注释", text)
        self.assertIn("port = 1080", text)
        self.assertIn('route = "cf-relay"', text)
        self.assertIn("[cf_relay]", text)
        # 端口变更 → 标记需重启（stub 返回的 changes 里没有 listen/port）
        self.assertIn("restart_required", data)
        self.assertEqual(self.hot.reload_calls, 1)

    def test_config_apply_rejects_invalid_port_and_route(self):
        with open(self.config_path, encoding="utf-8") as handle:
            original = handle.read()
        for payload in ({"proxy": {"port": 99999}},
                        {"proxy": {"port": "abc"}},
                        {"proxy": {"route": "no-such-route"}},
                        {}):
            code, body = self._post("/api/wizard/config", payload,
                                    origin=self.base)
            data = json.loads(body)
            self.assertFalse(data["success"], payload)
            self.assertIn("error", data)
        with open(self.config_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)

    def test_config_apply_cross_site_rejected(self):
        payload = {"proxy": {"port": 1080}}
        code, body = self._post("/api/wizard/config", payload,
                                origin="http://evil.example")
        data = json.loads(body)
        self.assertFalse(data["success"])
        self.assertIn("跨站", data["error"])

    # ------------------------------------------------------------ 网桥获取
    def test_fetch_bridges_success(self):
        lines = ["meek 0.0.2.0:3 url=http://192.0.2.1:80 front=cdn.example"]
        with mock.patch(
                "torsocks5.web.server.bridge_fetch_mod.fetch_bridges_via_https",
                return_value=lines):
            code, body = self._post("/api/wizard/fetch", {"transport": "meek"},
                                    origin=self.base)
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertTrue(data["success"], data)
        self.assertEqual(data["found"], 1)
        self.assertEqual(data["added"], 1)
        self.assertEqual(data["count"], 1)
        # 已持久化到网桥文件
        with open(self.bridges_path, encoding="utf-8") as handle:
            self.assertIn("192.0.2.1", handle.read())

    def test_fetch_bridges_failure_falls_back_to_email_guidance(self):
        with mock.patch(
                "torsocks5.web.server.bridge_fetch_mod.fetch_bridges_via_https",
                return_value=[]):
            code, body = self._post("/api/wizard/fetch", {"transport": "meek"},
                                    origin=self.base)
        data = json.loads(body)
        self.assertFalse(data["success"])
        self.assertEqual(data["fallback"], "email")
        self.assertIn("bridges@torproject.org", data["guidance"])
        self.assertTrue(data["manual_url"].startswith("https://"))

    # ------------------------------------------------------------ 自启方案
    def test_service_endpoint(self):
        code, body = _http(self.base + "/api/wizard/service",
                           "admin", "secret")
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertTrue(data["success"])
        self.assertIn("content", data)
        self.assertIsInstance(data["steps"], list)
        self.assertTrue(data["uninstall"])


if __name__ == "__main__":
    unittest.main()
