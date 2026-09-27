"""tor 进程管理：生成 torrc、启动/监控/重启、优雅关闭。

生成的 torrc 要点：
    * ``ClientOnly 1`` + 独立数据目录，互不干扰系统里已有的 tor
    * ``UseBridges 1`` + meek 网桥行
    * ``ClientTransportPlugin meek,meek_lite,meek_azure exec <本项目的 meek 插件>``
    * 控制端口用 cookie 认证，随机端口，仅监听 127.0.0.1
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .. import config as config_module
from . import control as control_module
from . import find as find_module

BOOTSTRAP_RE = re.compile(r"Bootstrapped (\d+)% \(([^)]*)\): (.*)")

def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_BOOTSTRAPPING = "bootstrapping"
STATE_READY = "ready"
STATE_FAILED = "failed"


def free_port(host: str = "127.0.0.1") -> int:
    """向系统申请一个空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def port_available(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def space_free(path: str) -> bool:
    return " " not in path and "\t" not in path


class ShimError(RuntimeError):
    pass


class TorProcess:
    """一个 tor 客户端进程。"""

    def __init__(
        self,
        config: config_module.Config,
        bridges: List[str],
        on_log: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[int, str, str], None]] = None,
        on_state: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config = config
        self.bridges = list(bridges)
        self.on_log = on_log
        self.on_progress = on_progress
        self.on_state = on_state
        self.process: Optional[subprocess.Popen] = None
        self.state = STATE_STOPPED
        self.progress = 0
        self.summary = ""
        self.tag = ""
        self.data_dir = os.path.abspath(config.data_dir)
        self.socks_port = int(config.get("tor.socks_port")) or free_port()
        self.control_port = int(config.get("tor.control_port")) or free_port()
        self.cookie_path = os.path.join(self.data_dir, "control_auth_cookie")
        self.torrc_path = os.path.join(self.data_dir, "torrc")
        self.tor_log_path = os.path.join(self.data_dir, "tor.log")
        self.binary = ""
        self.version: Optional[Tuple[int, int, int]] = None
        self._reader: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._state_lock = threading.Lock()
        self._exited = threading.Event()

    # ------------------------------------------------------------------ 路径
    @property
    def socks_address(self) -> Tuple[str, int]:
        return ("127.0.0.1", self.socks_port)

    def _log(self, message: str) -> None:
        if self.on_log is not None:
            self.on_log(message)

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            if self.state != state:
                self.state = state
                if self.on_state is not None:
                    self.on_state(state)

    # ------------------------------------------------------------------ 插件命令
    def raw_plugin_command(self) -> List[str]:
        """本项目 meek 插件的启动命令。"""
        extra = ["-v"] if _bool(self.config.get("meek.verbose")) else []
        if getattr(sys, "frozen", False):
            return [sys.executable, "--transport-plugin"] + extra
        entry = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "meek_pt.py",
        )
        return [sys.executable, entry] + extra

    def plugin_command(self) -> List[str]:
        """返回可直接写进 torrc 的命令（必要时生成无空格的 shim）。

        tor 用空格朴素切分 ``ClientTransportPlugin`` 的参数，不处理引号，
        所以只要路径里有空格就必须借道一个「无空格」的启动脚本。
        """
        command = self.raw_plugin_command()
        if all(space_free(token) for token in command):
            return command
        return self._make_shim(command)

    def _shim_dir(self) -> str:
        configured = self.config.get("tor.pt_shim_dir") or ""
        candidates: List[str] = []
        if configured:
            candidates.append(configured)
        if os.name == "nt":
            system_temp = os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"), "Temp"
            )
            candidates += [system_temp, os.environ.get("TEMP", ""), r"C:\torsocks5-pt"]
        else:
            candidates += [
                os.path.join("/tmp", "torsocks5-pt-%d" % os.getuid()),
                os.environ.get("TMPDIR", ""),
            ]
        for candidate in candidates:
            if not candidate:
                continue
            target = os.path.join(candidate, "torsocks5-pt")
            try:
                os.makedirs(target, exist_ok=True)
            except OSError:
                continue
            if space_free(target):
                return target
        raise ShimError(
            "无法创建不含空格的临时目录来启动 meek 插件（tor 不支持带空格的路径）。\n"
            "请把 TorSOCKS5 移动到不含空格的目录，或在配置里设置 tor.pt_shim_dir。"
        )

    def _make_shim(self, command: List[str]) -> List[str]:
        digest = hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest()[:10]
        directory = self._shim_dir()
        quoted = " ".join('"%s"' % token for token in command)
        if os.name == "nt":
            shim_path = os.path.join(directory, "meek-pt-%s.cmd" % digest)
            content = "@echo off\r\n%s %%*\r\n" % quoted
            launcher = [
                os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"),
                "/c",
                shim_path,
            ]
        else:
            shim_path = os.path.join(directory, "meek-pt-%s.sh" % digest)
            content = "#!/bin/sh\nexec %s \"$@\"\n" % quoted
            launcher = [shim_path]
        try:
            with open(shim_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
            os.chmod(shim_path, 0o755)
        except OSError as exc:
            raise ShimError("写入启动脚本 %s 失败: %s" % (shim_path, exc)) from exc
        self._log("已生成无空格启动脚本: %s" % shim_path)
        return launcher

    # ------------------------------------------------------------------ torrc
    def build_torrc(self) -> str:
        mode = str(self.config.get("tor.meek_mode") or "plugin").lower()
        methods = [m for m in (self.config.get("meek.methods") or []) if m]
        lines: List[str] = [
            "# 由 TorSOCKS5 自动生成，请勿手工编辑（改配置后会自动重写）",
            "DataDirectory %s" % self.data_dir,
            "SocksPort 127.0.0.1:%d" % self.socks_port,
        ]
        isolation = [str(item) for item in (self.config.get("tor.isolation") or []) if item]
        if isolation:
            lines[1] += " " + " ".join(isolation)
        lines += [
            "ControlPort 127.0.0.1:%d" % self.control_port,
            "CookieAuthentication 1",
            "CookieAuthFile %s" % self.cookie_path,
            "ClientOnly 1",
            "SafeLogging 1",
            "RunAsDaemon 0",
            "Log %s stdout" % self.config.get("tor.log_level"),
            "Log %s file %s" % (self.config.get("tor.log_level"), self.tor_log_path),
        ]
        if self.config.get("tor.avoid_disk_writes"):
            lines.append("AvoidDiskWrites 1")
        if not getattr(sys, "frozen", False) and os.name != "nt":
            # 控制器进程消失时自动退出，避免留下孤儿 tor
            lines.append("__OwningControllerProcess %d" % os.getpid())
        if mode == "plugin" and methods:
            command = self.plugin_command()
            lines.append("ClientTransportPlugin %s exec %s" % (",".join(methods), " ".join(command)))
        if self.config.get("tor.direct"):
            lines.append("UseBridges 0")
        else:
            lines.append("UseBridges 1")
            for line in self.bridges:
                lines.append(line)
        for option in self.config.get("tor.extra_options") or []:
            lines.append(str(option))
        return "\n".join(lines) + "\n"

    def write_torrc(self) -> str:
        os.makedirs(self.data_dir, exist_ok=True)
        content = self.build_torrc()
        with open(self.torrc_path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return content

    # ------------------------------------------------------------------ 生命周期
    def resolve_binary(self) -> str:
        binary = find_module.find_tor(str(self.config.get("tor.binary") or ""))
        if not binary:
            raise FileNotFoundError(
                "找不到 tor 可执行文件。\n"
                "  macOS:  brew install tor\n"
                "  Debian/Ubuntu: sudo apt install tor\n"
                "  Windows: 安装 Tor Expert Bundle 或 Tor Browser，"
                "或用 torsocks5 fetch-tor 自动下载。\n"
                "  也可以在配置里写 tor.binary = \"/path/to/tor\""
            )
        self.binary = binary
        self.version = find_module.tor_version(binary)
        return binary

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.resolve_binary()
        if not port_available(self.socks_port):
            raise RuntimeError(
                "tor 的 SOCKS 端口 %d 已被占用，可用 tor.socks_port 指定其它端口" % self.socks_port
            )
        self.write_torrc()
        try:
            os.remove(self.cookie_path)
        except OSError:
            pass
        self._stop_flag.clear()
        self._exited.clear()
        self._set_state(STATE_STARTING)
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            [self.binary, "-f", self.torrc_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._reader = threading.Thread(target=self._read_output, name="tor-log", daemon=True)
        self._reader.start()
        self._set_state(STATE_BOOTSTRAPPING)

    def _read_output(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if self._stop_flag.is_set():
                    break
                text = line.rstrip()
                match = BOOTSTRAP_RE.search(text)
                if match:
                    percent = int(match.group(1))
                    tag = match.group(2)
                    summary = match.group(3)
                    if percent != self.progress or summary != self.summary:
                        self.progress = percent
                        self.tag = tag
                        self.summary = summary
                        if self.on_progress is not None:
                            self.on_progress(percent, tag, summary)
                    if percent >= 100:
                        self._set_state(STATE_READY)
                    continue
                if text and self.on_log is not None:
                    self.on_log(text)
        except (OSError, ValueError):
            pass
        finally:
            self._exited.set()
            if not self._stop_flag.is_set():
                self._set_state(STATE_FAILED)

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def wait_for_ready(self, timeout: float = 180.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.state == STATE_READY:
                return True
            if self.state == STATE_FAILED and not self.running:
                return False
            time.sleep(0.3)
        return self.state == STATE_READY

    def bootstrap_status(self) -> Tuple[int, str]:
        """通过控制端口查询引导进度（比解析日志更可靠）。"""
        if not self.running:
            return (-1, "tor 未运行")
        if not control_module.wait_for_cookie(self.cookie_path, timeout=5.0):
            return (-1, "控制端口 cookie 尚未就绪")
        try:
            with control_module.ControlClient(
                "127.0.0.1", self.control_port, cookie_path=self.cookie_path, timeout=10.0
            ) as client:
                return control_module.bootstrap_percent(client)
        except (control_module.ControlError, OSError) as exc:
            return (-1, str(exc))

    def exit_code(self) -> Optional[int]:
        return self.process.poll() if self.process else None

    def stop(self, timeout: float = 20.0) -> None:
        self._stop_flag.set()
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            # 优先用控制端口优雅关闭
            if control_module.wait_for_cookie(self.cookie_path, timeout=2.0):
                try:
                    with control_module.ControlClient(
                        "127.0.0.1", self.control_port, cookie_path=self.cookie_path, timeout=5.0
                    ) as client:
                        client.signal("HALT")
                except (control_module.ControlError, OSError):
                    pass
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._terminate(process)
        if self._reader is not None:
            self._reader.join(timeout=3.0)
            self._reader = None
        self.process = None
        self._set_state(STATE_STOPPED)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        try:
            if os.name == "nt":
                process.terminate()
            else:
                process.send_signal(signal.SIGTERM)
            process.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError, ProcessLookupError):
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass

    def restart(self) -> None:
        self.stop()
        time.sleep(0.5)
        self.start()

    def summary_text(self) -> str:
        version = ".".join(str(part) for part in self.version) if self.version else "未知"
        return "tor %s (pid=%s, 端口 %d, 状态 %s)" % (
            version,
            self.process.pid if self.process else "-",
            self.socks_port,
            self.state,
        )


class TorSupervisor:
    """带自动重启的 tor 守护。"""

    def __init__(self, factory: Callable[[], TorProcess], restart: bool = True,
                 on_log: Optional[Callable[[str], None]] = None) -> None:
        self.factory = factory
        self.restart = restart
        self.on_log = on_log
        self.tor: Optional[TorProcess] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.restarts = 0

    def start(self) -> TorProcess:
        self.tor = self.factory()
        self.tor.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="tor-supervisor", daemon=True)
        self._thread.start()
        return self.tor

    def _watch(self) -> None:
        while not self._stop.is_set():
            time.sleep(1.0)
            tor = self.tor
            if tor is None or self._stop.is_set():
                return
            if not tor.running:
                if not self.restart:
                    return
                self.restarts += 1
                if self.on_log:
                    self.on_log("tor 意外退出（第 %d 次），正在重启…" % self.restarts)
                try:
                    tor.restart()
                except Exception as exc:  # noqa: BLE001
                    if self.on_log:
                        self.on_log("tor 重启失败: %s" % exc)
                    time.sleep(3.0)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.tor is not None:
            self.tor.stop()
