"""配置热重载：SIGHUP 信号 + HTTP API 触发，运行时无需重启更新配置。"""

from __future__ import annotations

import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from . import config as config_mod
from . import log as log_mod
from .routes import Route, RouteOptions, create_route
from .split import SplitRouter


@dataclass
class HotReloadConfig:
    """热重载配置。"""
    enabled: bool = True
    signal_name: str = "SIGHUP"
    api_enabled: bool = True
    api_path: str = "/api/config/reload"
    # 需要认证的配置键（修改这些需要管理员权限）
    sensitive_keys: Set[str] = field(default_factory=lambda: {
        "proxy.username", "proxy.password", "proxy.allow_from",
        "cf_relay.token", "self_relay.token", "relay.token",
    })


def _flatten(data: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """把嵌套配置展平成点分路径（``proxy.route``），列表/标量视为叶子。"""
    out: Dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


class ConfigWatcher:
    """配置文件变更监听（可选，基于轮询）。"""

    def __init__(self, config_path: str, interval: float = 2.0):
        self.config_path = config_path
        self.interval = interval
        self._last_mtime: Optional[float] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable[[], Any]] = None

    def start(self, callback: Callable[[], Any]) -> None:
        if not os.path.exists(self.config_path):
            return
        self._last_mtime = os.path.getmtime(self.config_path)
        self._callback = callback
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="config-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(self.interval)
            if self._stop_event.is_set():
                break
            try:
                mtime = os.path.getmtime(self.config_path)
                if mtime != self._last_mtime:
                    self._last_mtime = mtime
                    if self._callback:
                        self._callback()
            except OSError:
                pass


class HotReloadManager:
    """热重载管理器：协调信号、API、配置应用。"""

    def __init__(
        self,
        config: config_mod.Config,
        logger: log_mod.Logger,
        current_route: Optional[Route],
        route_options: RouteOptions,
        get_connector: Callable[[], Any],
        set_connector: Callable[[Any], None],
        get_upstream_socks: Callable[[], Any],
        set_upstream_socks: Callable[[Any], None],
        get_split_router: Callable[[], Optional[SplitRouter]],
        set_split_router: Callable[[Optional[SplitRouter]], None],
        set_route: Optional[Callable[[Any], None]] = None,
        apply_runtime: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    ):
        self.config = config
        self.logger = logger
        self.current_route = current_route
        self.route_options = route_options
        self.get_connector = get_connector
        self.set_connector = set_connector
        self.get_upstream_socks = get_upstream_socks
        self.set_upstream_socks = set_upstream_socks
        self.get_split_router = get_split_router
        self.set_split_router = set_split_router
        #: 路由重建后同步外部持有的路由引用（cmd_run 关停时要用最新的）
        self.set_route = set_route
        #: 可热更新的运行期参数（如 SOCKS5 服务器的超时/连接数）由调用方应用
        self.apply_runtime = apply_runtime

        self.hotreload_config = HotReloadConfig(
            enabled=config_mod.as_bool(config.get("hotreload.enabled")),
            signal_name=config.get("hotreload.signal") or "SIGHUP",
            api_enabled=config_mod.as_bool(config.get("hotreload.api_enabled")),
            api_path=config.get("hotreload.api_path") or "/api/config/reload",
        )

        self._signal_handler_installed = False
        self._watcher = ConfigWatcher(config.path or config_mod.default_config_path())
        self._lock = threading.RLock()
        self._reload_count = 0
        self._last_reload_time: Optional[float] = None
        self._last_error: Optional[str] = None

    def install_signal_handler(self) -> None:
        """安装信号处理器。"""
        if self._signal_handler_installed:
            return
        sig_name = self.hotreload_config.signal_name
        try:
            sig_num = getattr(signal, sig_name)
            signal.signal(sig_num, self._on_signal)
            self._signal_handler_installed = True
            self.logger.info(f"热重载信号处理器已安装: {sig_name}")
        except (AttributeError, ValueError, OSError) as exc:
            self.logger.warn(f"无法安装信号 {sig_name}: {exc}")

    def _on_signal(self, signum, frame) -> None:
        self.logger.info(f"收到信号 {signum}，触发配置热重载")
        self.reload()

    def start_watcher(self) -> None:
        """启动文件监听（可选）。"""
        if self.hotreload_config.enabled:
            self._watcher.start(self.reload)

    def stop_watcher(self) -> None:
        self._watcher.stop()

    def reload(self, force: bool = False) -> Dict[str, Any]:
        """执行热重载，返回结果信息。"""
        with self._lock:
            self._reload_count += 1
            start_time = time.time()
            result: Dict[str, Any] = {
                "success": False,
                "reloaded_at": start_time,
                "changes": [],
                "warnings": [],
                "error": None,
            }

            try:
                # 1. 重新加载配置文件
                old_data = self.config.data
                new_data = config_mod.load_toml(self.config.path) if os.path.exists(self.config.path) else {}
                self.config.data = new_data

                # 2. 检测变更
                changes = self._detect_changes(old_data, new_data)
                result["changes"] = changes

                if not changes and not force:
                    result["success"] = True
                    result["warnings"].append("配置无变化")
                    self._last_reload_time = start_time
                    self._last_error = None
                    return result

                # 3. 应用可热更新的配置
                self._apply_hot_changes(changes)

                # 4. 如果路由相关配置变了，重建路由（保持现有连接不断）
                if self._needs_route_rebuild(changes):
                    self._rebuild_route()

                result["success"] = True
                self._last_reload_time = start_time
                self._last_error = None
                self.logger.ok(f"配置热重载成功 ({len(changes)} 项变更)")

            except Exception as exc:
                self._last_error = str(exc)
                result["success"] = False
                result["error"] = str(exc)
                self.logger.error(f"配置热重载失败: {exc}")

            return result

    def _detect_changes(self, old: Dict[str, Any], new: Dict[str, Any]) -> List[Dict[str, Any]]:
        """对比新旧配置（展平成点分路径），返回变更列表。"""
        old_flat = _flatten(old)
        new_flat = _flatten(new)
        changes = []
        all_keys = set(old_flat.keys()) | set(new_flat.keys())
        for key in sorted(all_keys):
            old_val = old_flat.get(key)
            new_val = new_flat.get(key)
            if old_val != new_val:
                changes.append({
                    "key": key,
                    "old": old_val,
                    "new": new_val,
                    "sensitive": key in self.hotreload_config.sensitive_keys,
                })
        return changes

    def _apply_hot_changes(self, changes: List[Dict[str, Any]]) -> None:
        """应用可热更新的配置项。"""
        # 这些配置修改后立即生效，无需重建路由
        hot_keys = {
            "proxy.max_connections",
            "proxy.idle_timeout",
            "proxy.connect_timeout",
            "proxy.verbose",
            "split.mode",
            "split.builtin_proxy",
            "split.builtin_direct",
            "split.proxy_hosts",
            "split.direct_hosts",
            "relay.max_streams",
            "relay.allow_hosts",
            "relay.allow_ports",
            "relay.allow_all",
            "meek.verbose",
            "tor.log_level",
        }

        for change in changes:
            key = change["key"]
            if key in hot_keys:
                # 敏感值不落日志
                shown = "******" if change.get("sensitive") else change["new"]
                self.logger.info(f"热更新配置: {key} = {shown}")
                # 分流路由器会在下次决策时自动读取新配置
                # SOCKS5 服务器的限制会在新连接时生效

        # 交给调用方应用能直接热更的运行期参数（如 SOCKS5 服务器超时/连接数）
        if self.apply_runtime is not None and changes:
            try:
                self.apply_runtime(changes)
            except Exception as exc:  # noqa: BLE001
                self.logger.warn(f"应用运行期热更新失败: {exc}")

    def _needs_route_rebuild(self, changes: List[Dict[str, Any]]) -> bool:
        """判断是否需要重建路由。"""
        route_keys = {
            "proxy.route",
            "cf_relay.url",
            "cf_relay.token",
            "cf_relay.links",
            "cf_relay.max_streams",
            "cf_relay.front",
            "cf_relay.insecure",
            "cf_relay.token_in_header",
            "cf_relay.nodes",            # 多中继节点列表
            "cf_relay.lb_strategy",
            "cf_relay.circuit_breaker_threshold",
            "cf_relay.circuit_breaker_timeout",
            "self_relay.url",
            "self_relay.token",
            "self_relay.links",
            "self_relay.max_streams",
            "self_relay.front",
            "self_relay.insecure",
            "self_relay.token_in_header",
            "self_relay.nodes",
            "self_relay.lb_strategy",
            "self_relay.circuit_breaker_threshold",
            "self_relay.circuit_breaker_timeout",
            "tor.binary",
            "tor.meek_mode",
            "tor.direct",
            "tor.restart",
            "meek.methods",
            "meek.connect_timeout",
            "meek.read_timeout",
        }
        for change in changes:
            key = change["key"]
            # 分流规则烘焙在 connector 闭包里，split.* 任何变化都必须重建路由
            if key in route_keys or key.startswith("split."):
                return True
        return False

    def _rebuild_route(self) -> None:
        """重建路由（保持现有连接，新连接走新配置）。"""
        self.logger.info("路由配置变更，重建路由...")

        # 停止旧路由
        if self.current_route:
            try:
                self.current_route.stop()
            except Exception as exc:
                self.logger.warn(f"停止旧路由时出错: {exc}")

        # 创建新路由
        route_name = str(self.config.get("proxy.route") or "tor-meek")
        try:
            new_route = create_route(route_name, self.config, self.logger, self.route_options)
            new_route.start()

            # 原子切换
            self.set_connector(new_route.connector())
            self.set_upstream_socks(new_route.upstream_socks())
            self.current_route = new_route
            if self.set_route is not None:
                self.set_route(new_route)

            # 重建分流路由器
            new_router = self._build_split_router()
            self.set_split_router(new_router)

            self.logger.ok("路由已热切换")
        except Exception as exc:
            self.logger.error(f"重建路由失败: {exc}")
            raise

    def _build_split_router(self) -> SplitRouter:
        return SplitRouter(
            str(self.config.get("split.mode")),
            route_name=self.current_route.name if self.current_route else "unknown",
            builtin_proxy=config_mod.as_bool(self.config.get("split.builtin_proxy")),
            builtin_direct=config_mod.as_bool(self.config.get("split.builtin_direct")),
            proxy_hosts=self.config.get("split.proxy_hosts"),
            direct_hosts=self.config.get("split.direct_hosts"),
            on_log=self.logger.debug,
        )

    def switch_route(self, route_name: str) -> Dict[str, Any]:
        """运行期切换路由（仅改内存配置并热切换，不写回配置文件）。

        面板「切换路由」按钮与 ``POST /api/routes/switch`` 走这里。
        失败时回滚并尝试切回旧路由。
        """
        with self._lock:
            section = self.config.data.get("proxy")
            if not isinstance(section, dict):
                section = {}
                self.config.data["proxy"] = section
            old_route = section.get("route")
            section["route"] = route_name
            try:
                self._rebuild_route()
            except Exception as exc:  # noqa: BLE001 - 构造路由的异常类型不统一
                section["route"] = old_route
                try:
                    self._rebuild_route()
                except Exception:  # noqa: BLE001 - 回滚也失败就只能等重启
                    self.logger.error("路由切换失败且回滚失败: %s" % exc)
                return {"success": False, "error": str(exc)}
            self._reload_count += 1
            self.logger.ok("路由已切换: %s -> %s（运行期，不写入配置文件）"
                           % (old_route, route_name))
            return {"success": True, "route": route_name, "old_route": old_route}

    def get_status(self) -> Dict[str, Any]:
        return {
            "enabled": self.hotreload_config.enabled,
            "signal": self.hotreload_config.signal_name,
            "api_enabled": self.hotreload_config.api_enabled,
            "api_path": self.hotreload_config.api_path,
            "reload_count": self._reload_count,
            "last_reload_time": self._last_reload_time,
            "last_error": self._last_error,
        }


def start_reload_api(manager: HotReloadManager, host: str = "",
                     port: Optional[int] = None):
    """用标准库 ``http.server`` 启动热重载 API（零第三方依赖）。

    - ``GET  <api_path>``  返回热重载状态（Basic Auth）
    - ``POST <api_path>``  触发一次配置重载（Basic Auth）

    认证凭据取 ``proxy.username`` / ``proxy.password``；未设置凭据时一律拒绝。
    启动失败（端口占用等）只告警、不打断主服务。返回 HTTP server 或 None。
    """
    import base64
    import json as json_mod
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    cfg = manager.hotreload_config
    if not cfg.api_enabled:
        return None

    username = str(manager.config.get("proxy.username") or "")
    password = str(manager.config.get("proxy.password") or "")
    bind_host = host or str(manager.config.get("hotreload.api_host") or "127.0.0.1")
    # port 显式传 0 表示由系统分配临时端口（测试用）；None 才取配置
    bind_port = port if port is not None else int(manager.config.get("hotreload.api_port") or 9053)
    api_path = cfg.api_path

    class _Handler(BaseHTTPRequestHandler):
        server_version = "TorSOCKS5-HotReload"

        def log_message(self, fmt: str, *args) -> None:
            manager.logger.debug("hotreload-api: " + fmt % args)

        def _reply(self, code: int, payload: Dict[str, Any]) -> None:
            body = json_mod.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def _deny(self, code: int, message: str) -> None:
            if code == 401:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="TorSOCKS5"')
                body = json_mod.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
            else:
                self._reply(code, {"error": message})

        def _authed(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                user, pw = decoded.split(":", 1)
            except Exception:  # noqa: BLE001
                return False
            return user == username and pw == password

        def _check(self) -> bool:
            if not username or not password:
                self._deny(403, "请先在配置中设置 proxy.username / proxy.password")
                return False
            if not self._authed():
                self._deny(401, "需要认证")
                return False
            if self.path != api_path:
                self._reply(404, {"error": "未知路径"})
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802 - http.server 约定
            if self._check():
                self._reply(200, manager.get_status())

        def do_POST(self) -> None:  # noqa: N802 - http.server 约定
            if not self._check():
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > 0:
                try:
                    self.rfile.read(length)
                except OSError:
                    pass
            result = manager.reload()
            self._reply(200 if result.get("success") else 500, result)

    try:
        server = ThreadingHTTPServer((bind_host, bind_port), _Handler)
    except OSError as exc:
        manager.logger.warn(f"热重载 API 启动失败（{bind_host}:{bind_port}）: {exc}")
        return None
    threading.Thread(target=server.serve_forever, name="hotreload-api",
                     daemon=True).start()
    manager.logger.info(
        f"热重载 API 已启动: http://{bind_host}:{server.server_address[1]}{api_path}")
    return server


__all__ = [
    "HotReloadConfig",
    "ConfigWatcher",
    "HotReloadManager",
    "start_reload_api",
]
