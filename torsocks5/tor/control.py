"""tor 控制端口（ControlPort）客户端。

用于：优雅关闭 tor、查询 bootstrap 进度、订阅事件、以及 ``doctor`` 自检。
协议细节见 https://spec.torproject.org/control-spec/ —— 控制连接是普通文本行协议：

.. code-block:: text

    → AUTHENTICATE <hex cookie>
    ← 250 OK
    → GETINFO status/bootstrap-phase
    ← 250-status/bootstrap-phase=NOTICE BOOTSTRAP PROGRESS=100 TAG=done SUMMARY="Done"
    ← 250 OK
"""

from __future__ import annotations

import binascii
import os
import socket
import threading
import time
from typing import IO, Callable, Dict, List, Optional, Tuple, cast


class ControlError(Exception):
    """控制端口错误。"""


class ControlClient:
    def __init__(
        self,
        host: str,
        port: int,
        cookie_path: str = "",
        password: str = "",
        timeout: float = 30.0,
    ) -> None:
        self.host = host
        self.port = port
        self.cookie_path = cookie_path
        self.password = password
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._file: Optional[IO[bytes]] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 连接
    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        self._file = cast(IO[bytes], sock.makefile("rwb", buffering=0))
        self.authenticate()

    def authenticate(self) -> None:
        if self.cookie_path:
            with open(self.cookie_path, "rb") as handle:
                cookie = handle.read().strip()
            self._send("AUTHENTICATE %s" % binascii.hexlify(cookie).decode())
        elif self.password:
            escaped = self.password.replace("\\", "\\\\").replace('"', '\\"')
            self._send('AUTHENTICATE "%s"' % escaped)
        else:
            raise ControlError("既没有 cookie 文件也没有控制口令，拒绝无认证连接")
        code, message = self._read_reply()
        if code != 250:
            raise ControlError("认证失败: %s %s" % (code, message))

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> ControlClient:
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------ 协议
    def _send(self, line: str) -> None:
        if self._file is None:
            raise ControlError("控制连接未建立")
        with self._lock:
            self._file.write((line + "\r\n").encode("utf-8"))

    def _readline(self) -> str:
        if self._file is None:
            raise ControlError("控制连接未建立")
        raw = self._file.readline()
        if not raw:
            raise ControlError("控制连接已被 tor 关闭")
        return raw.decode("utf-8", "replace").rstrip("\r\n")

    def _read_reply(self) -> Tuple[int, str]:
        """读取一条应答，返回 ``(状态码, 内容)``（多行以 ``-`` 续行）。"""
        lines: List[str] = []
        while True:
            line = self._readline()
            if len(line) < 4:
                raise ControlError("无法解析控制应答: %r" % line)
            code_text, sep, rest = line[:3], line[3:4], line[4:]
            try:
                code = int(code_text)
            except ValueError as exc:
                raise ControlError("无法解析状态码: %r" % line) from exc
            lines.append(rest)
            if sep == " ":
                return code, "\n".join(lines)

    def command(self, line: str) -> Tuple[int, str]:
        self._send(line)
        return self._read_reply()

    def get_info(self, key: str) -> Dict[str, str]:
        code, body = self.command("GETINFO %s" % key)
        if code != 250:
            raise ControlError("GETINFO %s 失败: %s %s" % (key, code, body))
        out: Dict[str, str] = {}
        for line in body.splitlines():
            if "=" in line:
                name, _, value = line.partition("=")
                out[name.strip()] = value.strip()
        return out

    def get_info_value(self, key: str) -> str:
        return self.get_info(key).get(key, "")

    def set_events(self, events: List[str]) -> None:
        code, body = self.command("SETEVENTS " + " ".join(events))
        if code != 250:
            raise ControlError("SETEVENTS 失败: %s %s" % (code, body))

    def signal(self, name: str) -> None:
        self.command("SIGNAL %s" % name)

    def take_ownership(self) -> None:
        try:
            self.command("TAKEOWNERSHIP")
        except ControlError:
            pass

    def reset_conf(self, options: Dict[str, str]) -> None:
        for key, value in options.items():
            self.command("SETCONF %s=%s" % (key, value))


# --------------------------------------------------------------------- 事件流
class ControlEventWatcher:
    """后台读取事件并回调（用于显示引导进度 / 网关状态）。"""

    def __init__(self, client: ControlClient, callback: Callable[[str, List[str]], None]) -> None:
        self.client = client
        self.callback = callback
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, events: Optional[List[str]] = None) -> None:
        self.client.set_events(events or ["STATUS_CLIENT", "BOOTSTRAP", "CIRC", "NOTICE", "WARN", "ERR"])
        self._thread = threading.Thread(target=self._run, name="tor-events", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                line = self.client._readline()
            except (ControlError, OSError):
                return
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "650":
                if len(parts) > 2 and parts[1] in ("STATUS_CLIENT", "BOOTSTRAP", "CIRC", "NOTICE", "WARN", "ERR"):
                    try:
                        self.callback(parts[1], parts[2:])
                    except Exception:  # noqa: BLE001
                        pass
            elif parts[0].isdigit():
                # 应答行，忽略
                continue

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def bootstrap_percent(client: ControlClient) -> Tuple[int, str]:
    """返回 ``(进度百分比, 阶段说明)``。"""
    try:
        value = client.get_info_value("status/bootstrap-phase")
    except ControlError:
        return -1, ""
    percent = -1
    summary = ""
    for token in value.split():
        if token.startswith("PROGRESS="):
            try:
                percent = int(token.split("=", 1)[1])
            except ValueError:
                pass
        elif token.startswith("SUMMARY="):
            summary = token.split("=", 1)[1].strip('"')
    return percent, summary


def wait_for_cookie(cookie_path: str, timeout: float = 30.0) -> bool:
    """等待 tor 生成控制端口 cookie 文件。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(cookie_path) and os.path.getsize(cookie_path) > 0:
            return True
        time.sleep(0.2)
    return False
