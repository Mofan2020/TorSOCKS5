"""Web 面板 HTTP 服务（纯标准库，零第三方依赖）。

- 页面：``torsocks5/web/html.py``（Tailwind / Alpine / htmx CDN，零构建）
- 认证：Basic Auth，凭据取 ``[web] username/password``；未设置密码时启动随机生成并打印
- 数据：同源 JSON API；实时日志走 SSE（``/api/logs/stream``）
- 安全：默认只监听 127.0.0.1；写操作校验 ``Origin`` 防跨站 CSRF
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .. import bridge_fetch as bridge_fetch_mod
from .. import bridges as bridges_mod
from .. import config as config_mod
from .. import log as log_mod
from .. import routes as routes_mod
from .. import service as service_mod
from .. import version_check as version_check_mod
from . import html as html_mod
from . import wizard as wizard_mod
from .logstream import LogStream


class WebPanel:
    """本机管理面板。

    运行期对象通过可调用注入（热切换后拿到的是最新对象）：
    ``get_socks_server`` / ``get_route`` / ``get_hot_manager``。
    """

    def __init__(
        self,
        config: config_mod.Config,
        logger: log_mod.Logger,
        *,
        get_socks_server: Callable[[], Any],
        get_route: Callable[[], Any],
        get_hot_manager: Callable[[], Any],
        version: str = "",
        force_enable: bool = False,
    ) -> None:
        self.config = config
        self.logger = logger
        self._get_socks_server = get_socks_server
        self._get_route = get_route
        self._get_hot_manager = get_hot_manager
        self._version = version

        self.enabled = force_enable or config_mod.as_bool(config.get("web.enabled"))
        self.host = str(config.get("web.listen") or "127.0.0.1")
        # port = 0 表示由系统分配临时端口（测试用）；未配置时取默认 9054
        port_value = config.get("web.port")
        self.port = 9054 if port_value is None else int(port_value)
        self.username = str(config.get("web.username") or "").strip() or "admin"
        self.password = str(config.get("web.password") or "")
        #: 密码是否为启动时随机生成（用于日志提示）
        self.generated_password = not bool(self.password)
        if self.generated_password:
            self.password = secrets.token_urlsafe(12)

        self.log_stream = LogStream()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        #: 网桥测试后台任务状态（GET /api/bridges 返回）
        self._bridge_test: Dict[str, Any] = {"running": False, "last_result": ""}

    # ------------------------------------------------------------------ 生命周期
    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return "http://%s:%d" % (host, self.port)

    def start(self) -> bool:
        """启动面板；失败只告警、不影响代理主流程。"""
        if not self.enabled:
            return False
        handler = self._make_handler()
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as exc:
            self.logger.warn("Web 面板启动失败（%s:%d）: %s" % (self.host, self.port, exc))
            return False
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="web-panel", daemon=True)
        self._thread.start()
        if self.generated_password:
            self.logger.ok("Web 面板: %s  登录 %s / %s（[web].password 未配置，随机生成）"
                           % (self.url, self.username, self.password))
        else:
            self.logger.ok("Web 面板: %s（登录 %s）" % (self.url, self.username))
        return True

    def stop(self) -> None:
        self._stopping = True
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except OSError:
                pass
            self._httpd = None
        self._thread = None

    # ------------------------------------------------------------------ 认证
    def _auth_ok(self, header: str) -> bool:
        if not header.startswith("Basic "):
            return False
        try:
            import base64
            raw = base64.b64decode(header[6:]).decode("utf-8")
            username, password = raw.split(":", 1)
        except Exception:  # noqa: BLE001 - 格式错一律视为未通过
            return False
        return (hmac.compare_digest(username, self.username)
                and hmac.compare_digest(password, self.password))

    # ------------------------------------------------------------------ 路由分发
    def _dispatch(self, h: Any, method: str, path: str) -> None:
        parsed = urlparse(path)
        route_path = parsed.path
        query = parse_qs(parsed.query)

        # 页面
        if method == "GET" and route_path in html_mod.PAGES:
            h.send_html(html_mod.PAGES[route_path]())
            return

        # API
        key = (method, route_path)
        if key == ("GET", "/api/status"):
            h.send_json(self._api_status())
        elif key == ("GET", "/api/routes"):
            h.send_json(self._api_routes())
        elif key == ("POST", "/api/routes/switch"):
            h.send_json(self._api_switch_route(h))
        elif key == ("GET", "/api/bridges"):
            h.send_json(self._api_bridges_get())
        elif key == ("POST", "/api/bridges"):
            h.send_json(self._api_bridges_post(h))
        elif key == ("GET", "/api/config"):
            h.send_json(self._api_config_get())
        elif key == ("PUT", "/api/config"):
            h.send_json(self._api_config_put(h))
        elif key == ("POST", "/api/reload"):
            h.send_json(self._api_reload())
        elif key == ("GET", "/api/logs"):
            since = int(query.get("since", ["0"])[0] or 0)
            limit = min(1000, int(query.get("limit", ["200"])[0] or 200))
            h.send_json({"entries": self.log_stream.history(limit=limit, since=since)})
        elif key == ("GET", "/api/logs/stream"):
            self._serve_sse(h)
        elif key == ("GET", "/api/wizard/env"):
            h.send_json(self._api_wizard_env())
        elif key == ("POST", "/api/wizard/config"):
            h.send_json(self._api_wizard_config(h))
        elif key == ("POST", "/api/wizard/fetch"):
            h.send_json(self._api_wizard_fetch(h))
        elif key == ("GET", "/api/wizard/service"):
            h.send_json(self._api_wizard_service())
        else:
            h.send_json({"error": "not found"}, code=404)

    # ------------------------------------------------------------------ API 实现
    def _api_status(self) -> Dict[str, Any]:
        socks = self._get_socks_server()
        route = self._get_route()
        hot = self._get_hot_manager()
        proxy = socks.stats_dict() if socks is not None and hasattr(socks, "stats_dict") else {
            "listen": "", "max_connections": 0, "current_connections": 0,
            "total_connections": 0, "bytes_up": 0, "bytes_down": 0, "uptime": 0,
        }
        route_info = {"name": "-", "title": "", "status": "", "stats": {}}
        split_stats: Any = {}
        if route is not None:
            stats = route.stats()
            split_stats = stats.get("split", {})
            route_info = {
                "name": route.name,
                "title": getattr(route, "title", ""),
                "status": route.status(),
                "stats": stats,
            }
        return {
            "version": self._version,
            "now": time.strftime("%H:%M:%S"),
            "proxy": proxy,
            "route": route_info,
            "split": split_stats,
            "hotreload": hot.get_status() if hot is not None else None,
            "update": version_check_mod.get_status(),
            "bridge_count": len(self._load_store().active()),
            "config_path": self.config.path,
        }

    def _api_routes(self) -> Dict[str, Any]:
        route = self._get_route()
        hot = self._get_hot_manager()
        info: Dict[str, Any] = {
            "current": route.name if route is not None else "",
            "title": getattr(route, "title", "") if route is not None else "",
            "status": route.status() if route is not None else "",
            "stats": route.stats() if route is not None else {},
            "available": [list(row) for row in routes_mod.describe_table()],
            "switchable": hot is not None,
            "nodes": [],
        }
        if isinstance(info["stats"], dict):
            info["nodes"] = info["stats"].get("nodes", []) or []
        return info

    def _api_switch_route(self, h: Any) -> Dict[str, Any]:
        blocked = self._check_origin(h)
        if blocked is not None:
            return blocked
        payload = h.read_json()
        name = str((payload or {}).get("route") or "").strip()
        if name not in routes_mod.route_names():
            return {"success": False, "error": "未知路由 %r" % name}
        hot = self._get_hot_manager()
        if hot is None:
            return {"success": False, "error": "热重载未启用（[hotreload].enabled = false），无法在线切换"}
        return hot.switch_route(name)

    def _load_store(self) -> bridges_mod.BridgeStore:
        store = bridges_mod.BridgeStore(self.config.bridges_path)
        store.load(include_builtin=False)
        return store

    def _api_bridges_get(self) -> Dict[str, Any]:
        store = self._load_store()
        return {
            "path": store.path,
            "active": len(store.active()),
            "test": dict(self._bridge_test),
            "bridges": [
                {
                    "transport": b.transport,
                    "address": b.address,
                    "fingerprint": b.fingerprint,
                    "enabled": b.enabled,
                    "line": b.to_torrc(),
                }
                for b in store.bridges
            ],
        }

    def _api_bridges_post(self, h: Any) -> Dict[str, Any]:
        blocked = self._check_origin(h)
        if blocked is not None:
            return blocked
        payload = h.read_json() or {}
        action = str(payload.get("action") or "")
        store = self._load_store()

        if action == "add":
            line = str(payload.get("line") or "").strip()
            if not line:
                return {"success": False, "error": "缺少网桥行"}
            if not line.lower().startswith("bridge "):
                line = "Bridge " + line
            try:
                bridge = bridges_mod.parse_bridge_line(line)
            except bridges_mod.BridgeError as exc:
                return {"success": False, "error": str(exc)}
            if store.add(bridge):
                store.save()
                return {"success": True, "message": "已添加并保存"}
            # 已存在：add() 已把存储里的那条重新启用
            store.save()
            return {"success": True, "message": "已存在，已重新启用"}

        if action == "rm":
            needle = str(payload.get("needle") or "").strip()
            if not needle:
                return {"success": False, "error": "缺少删除条件"}
            removed = store.remove(needle)
            store.save()
            if not removed:
                return {"success": False, "error": "没有匹配的网桥"}
            return {"success": True, "message": "已删除 %d 条" % removed}

        if action == "test":
            if self._bridge_test.get("running"):
                return {"success": False, "error": "已有测试在进行中"}
            lines = store.torrc_lines()
            if not lines:
                return {"success": False, "error": "没有可测试的网桥"}
            timeout = min(600, max(30, int(payload.get("timeout") or 120)))
            worker = threading.Thread(target=self._bridge_test_worker,
                                      args=(lines, timeout),
                                      name="bridge-test", daemon=True)
            self._bridge_test = {"running": True, "last_result": ""}
            worker.start()
            return {"success": True, "message": "测试已在后台开始（最多 %d 秒），进度见日志页" % timeout}

        return {"success": False, "error": "未知动作 %r" % action}

    def _bridge_test_worker(self, lines: List[str], timeout: int) -> None:
        """后台启动 tor 验证网桥；结果写进日志流。"""
        from ..tor.manager import TorProcess

        tor = TorProcess(self.config, lines,
                         on_log=lambda line: self.logger.debug(log_mod.clean_tor_line(line)),
                         on_progress=log_mod.progress_printer(self.logger))
        try:
            self.logger.info("面板触发网桥测试：等待引导（最多 %d 秒）…" % timeout)
            tor.start()
            ok = tor.wait_for_ready(timeout)
            percent, summary = tor.bootstrap_status()
            if ok:
                result = "引导成功：%s" % (summary or "Done")
                self.logger.ok(result)
            else:
                result = "引导失败：%d%% %s" % (percent, summary)
                self.logger.error(result)
        except Exception as exc:  # noqa: BLE001 - 后台线程不能把异常抛出去
            result = "测试失败：%s" % exc
            self.logger.error(result)
        finally:
            try:
                tor.stop()
            except Exception:  # noqa: BLE001
                pass
            self._bridge_test = {"running": False, "last_result": result}

    # ------------------------------------------------------------------ 配置向导
    def _api_wizard_env(self) -> Dict[str, Any]:
        socks = self._get_socks_server()
        listen = ""
        if socks is not None and hasattr(socks, "stats_dict"):
            listen = str(socks.stats_dict().get("listen") or "")
        route = self._get_route()
        return wizard_mod.env_checks(
            self.config,
            route_name=route.name if route is not None else "",
            listen=listen,
            bridge_count=len(self._load_store().active()),
        )

    def _api_wizard_config(self, h: Any) -> Dict[str, Any]:
        blocked = self._check_origin(h)
        if blocked is not None:
            return blocked
        payload = h.read_json() or {}
        updates: Dict[str, Dict[str, Any]] = {}
        for section in ("proxy", "cf_relay", "self_relay"):
            kv = payload.get(section)
            if isinstance(kv, dict):
                clean = {str(k): v for k, v in kv.items()
                         if v is not None and v != ""}
                if clean:
                    updates[section] = clean
        if not updates:
            return {"success": False, "error": "没有要保存的修改"}
        proxy_kv = updates.get("proxy", {})
        if "port" in proxy_kv:
            try:
                port = int(proxy_kv["port"])
            except (TypeError, ValueError):
                return {"success": False, "error": "端口必须是数字"}
            if not 1 <= port <= 65535:
                return {"success": False, "error": "端口必须在 1-65535 之间"}
            proxy_kv["port"] = port
        if "route" in proxy_kv and proxy_kv["route"] not in routes_mod.route_names():
            return {"success": False, "error": "未知路由 %r" % proxy_kv["route"]}
        # 读现有文本（文件不存在则空），就地更新保留注释；校验通过才原子写入
        path = self.config.path
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError:
            text = ""
        except OSError as exc:
            return {"success": False, "error": "读取配置失败: %s" % exc}
        new_text = wizard_mod.apply_updates(text, updates)
        error = wizard_mod.validate_or_error(new_text)
        if error is not None:
            return {"success": False, "error": error}
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(new_text)
            os.replace(tmp, path)
        except OSError as exc:
            return {"success": False, "error": "写入失败: %s" % exc}
        parsed = config_mod.loads(new_text)
        restart_payload = any(k in ("listen", "port") for k in proxy_kv)
        hot = self._get_hot_manager()
        if hot is None:
            self.config.data = parsed
            warnings = ["热重载未启用，重启进程后生效"]
            if restart_payload:
                warnings.append("监听地址/端口变更需要重启生效")
            return {"success": True, "reloaded": False, "changes": [],
                    "restart_required": restart_payload, "warnings": warnings}
        result = hot.reload(force=True)
        if not result.get("success"):
            return {"success": True, "reloaded": False,
                    "restart_required": restart_payload, "changes": [],
                    "error": "文件已保存，但热重载失败: %s" % result.get("error")}
        changes = result.get("changes") or []
        restart_required = any(
            isinstance(change, dict)
            and change.get("key") in ("proxy.listen", "proxy.port")
            for change in changes)
        return {"success": True, "reloaded": True, "changes": changes,
                "warnings": result.get("warnings") or [],
                "restart_required": restart_required}

    def _api_wizard_fetch(self, h: Any) -> Dict[str, Any]:
        blocked = self._check_origin(h)
        if blocked is not None:
            return blocked
        payload = h.read_json() or {}
        transport = str(payload.get("transport") or "meek")
        fetch_error = None
        try:
            lines = bridge_fetch_mod.fetch_bridges_via_https(
                transport=transport, timeout=15.0, logger=self.logger)
        except Exception as exc:  # noqa: BLE001 - 网络异常类型不统一
            lines, fetch_error = [], str(exc)
        if lines:
            valid, errors = bridge_fetch_mod.validate_and_normalize_bridges(lines)
            store = self._load_store()
            added = 0
            for bridge in valid:
                if store.add(bridge):
                    added += 1
            if valid:
                store.save()
            self.logger.ok("向 tor 官网请求到 %d 条网桥（新增 %d 条）"
                           % (len(lines), added))
            return {"success": True, "method": "https", "found": len(lines),
                    "added": added, "count": len(store.active()),
                    "errors": errors}
        _, guidance = bridge_fetch_mod.fetch_bridges_via_email(transport=transport)
        return {"success": False, "method": "https", "found": 0,
                "fallback": "email", "guidance": guidance,
                "manual_url": "https://bridges.torproject.org",
                "error": fetch_error}

    def _api_wizard_service(self) -> Dict[str, Any]:
        try:
            port = int(self.config.get("proxy.port") or 9051)
        except (TypeError, ValueError):
            port = 9051
        plan = service_mod.startup_plan(port, self.config.path)
        plan["success"] = True
        return plan

    def _api_config_get(self) -> Dict[str, Any]:
        path = self.config.path
        try:
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
        except OSError as exc:
            return {"success": False, "error": "读取失败: %s" % exc,
                    "path": path, "content": ""}
        return {"success": True, "path": path, "content": content}

    def _api_config_put(self, h: Any) -> Dict[str, Any]:
        blocked = self._check_origin(h)
        if blocked is not None:
            return blocked
        payload = h.read_json() or {}
        content = payload.get("content")
        if not isinstance(content, str):
            return {"success": False, "error": "缺少 content 字段"}
        # 1) 语法校验（不通过绝不写文件）
        try:
            parsed = config_mod.loads(content)
        except Exception as exc:  # noqa: BLE001 - 解析器异常类型不统一
            return {"success": False, "error": "TOML 校验失败: %s" % exc}
        # 2) 原子写入
        path = self.config.path
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.replace(tmp, path)
        except OSError as exc:
            return {"success": False, "error": "写入失败: %s" % exc}
        # 3) 热重载（没有热重载时至少刷新内存配置）
        hot = self._get_hot_manager()
        reloaded = False
        if hot is not None:
            result = hot.reload(force=True)
            if not result.get("success"):
                return {"success": True, "reloaded": False,
                        "error": "文件已保存，但热重载失败: %s" % result.get("error")}
            reloaded = True
        else:
            self.config.data = parsed
        return {"success": True, "reloaded": reloaded}

    def _api_reload(self) -> Dict[str, Any]:
        hot = self._get_hot_manager()
        if hot is None:
            return {"success": False, "error": "热重载未启用"}
        result = hot.reload()
        # 让面板「触发重载」这个动作本身可见于日志流
        if result.get("success"):
            self.logger.ok("面板触发配置重载：%d 项变更"
                           % len(result.get("changes") or []))
        else:
            self.logger.error("面板触发配置重载失败：%s" % result.get("error"))
        return result

    # ------------------------------------------------------------------ SSE
    def _serve_sse(self, h: Any) -> None:
        try:
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream; charset=utf-8")
            h.send_header("Cache-Control", "no-cache")
            h.send_header("Connection", "keep-alive")
            h.end_headers()
            h.wfile.write(b": connected\n\n")
            h.wfile.flush()
        except OSError:
            return
        sub = self.log_stream.subscribe()
        try:
            while not self._stopping:
                try:
                    entry = sub.get(timeout=15.0)
                    data = json.dumps(entry, ensure_ascii=False)
                    h.wfile.write(("data: %s\n\n" % data).encode("utf-8"))
                except queue.Empty:
                    h.wfile.write(b": heartbeat\n\n")
                h.wfile.flush()
        except OSError:
            pass  # 客户端断开
        finally:
            self.log_stream.unsubscribe(sub)

    # ------------------------------------------------------------------ 安全
    def _check_origin(self, h: Any) -> Optional[Dict[str, Any]]:
        """写操作校验 Origin：浏览器跨站请求直接拒绝（防 CSRF）。"""
        origin = h.headers.get("Origin") or ""
        if not origin:
            referer = h.headers.get("Referer") or ""
            if not referer:
                return None  # curl 等非浏览器客户端
            origin = referer
        try:
            origin_host = urlparse(origin).netloc
        except ValueError:
            origin_host = ""
        request_host = (h.headers.get("Host") or "").strip()
        if origin_host and request_host and \
                hmac.compare_digest(origin_host, request_host):
            return None
        return {"success": False, "error": "跨站请求被拒绝"}

    # ------------------------------------------------------------------ Handler 工厂
    def _make_handler(self):
        panel = self

        class _PanelHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "TorSOCKS5-Web"

            # ---------------------------------------------------------- 工具
            def log_message(self, fmt: str, *args) -> None:
                panel.logger.debug("web: " + fmt % args)

            def _finish_body(self, body: bytes, code: int,
                             content_type: str = "application/json; charset=utf-8") -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def send_json(self, payload: Dict[str, Any], code: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._finish_body(body, code)

            def send_html(self, text: str) -> None:
                self._finish_body(text.encode("utf-8"), 200,
                                  "text/html; charset=utf-8")

            def read_json(self) -> Optional[Dict[str, Any]]:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return None
                if length <= 0 or length > 4 * 1024 * 1024:
                    return None
                try:
                    raw = self.rfile.read(length)
                    data = json.loads(raw.decode("utf-8"))
                except (OSError, ValueError, UnicodeDecodeError):
                    return None
                return data if isinstance(data, dict) else None

            # ---------------------------------------------------------- 认证
            def _authorized(self) -> bool:
                return panel._auth_ok(self.headers.get("Authorization", ""))

            def _require(self) -> bool:
                if self._authorized():
                    return True
                body = json.dumps({"error": "需要认证"}, ensure_ascii=False).encode("utf-8")
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="TorSOCKS5"')
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
                return False

            # ---------------------------------------------------------- 动词
            def _handle(self, method: str) -> None:
                if not self._require():
                    return
                path = self.path or "/"
                try:
                    panel._dispatch(self, method, path)
                except BrokenPipeError:
                    pass
                except Exception as exc:  # noqa: BLE001 - 单个请求出错不拖垮面板
                    panel.logger.debug("web 请求处理异常: %s" % exc)
                    try:
                        self.send_json({"success": False, "error": str(exc)}, code=500)
                    except OSError:
                        pass

            def do_GET(self) -> None:  # noqa: N802 - http.server 约定
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

            def do_PUT(self) -> None:  # noqa: N802
                self._handle("PUT")

            def do_DELETE(self) -> None:  # noqa: N802
                self._handle("DELETE")

        return _PanelHandler


__all__ = ["WebPanel"]
