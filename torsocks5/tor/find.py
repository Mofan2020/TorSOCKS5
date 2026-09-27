"""跨平台查找 tor 可执行文件。"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from typing import List, Optional

ENV_VARS = ("TORSOCKS5_TOR", "TOR_BINARY")


def _candidates() -> List[str]:
    home = os.path.expanduser("~")
    exe = "tor.exe" if os.name == "nt" else "tor"
    system = platform.system()
    paths: List[str] = []

    if system == "Darwin":
        paths += [
            "/opt/homebrew/bin/tor",
            "/usr/local/bin/tor",
            "/opt/local/bin/tor",
            "/Applications/Tor Browser.app/Contents/MacOS/Tor/tor",
            os.path.join(home, "Applications", "Tor Browser.app", "Contents", "MacOS", "Tor", exe),
            os.path.join("/Applications", "Tor Browser.app", "Contents", "MacOS", "Tor", exe),
        ]
    elif system == "Windows":
        program_files = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local")),
        ]
        for base in program_files:
            paths += [
                os.path.join(base, "Tor Browser", "Browser", "TorBrowser", "Tor", exe),
                os.path.join(base, "Tor", exe),
                os.path.join(base, "tor", exe),
                os.path.join(base, "Tor Browser", "Tor", exe),
            ]
        paths.append(os.path.join(os.getcwd(), exe))
    else:
        paths += [
            "/usr/bin/tor",
            "/usr/local/bin/tor",
            "/bin/tor",
            "/snap/bin/tor",
            os.path.join(home, ".local", "bin", exe),
            "/Applications/Tor Browser.app/Contents/MacOS/Tor/tor",
        ]

    # 应用自带目录（便携版 / PyInstaller 打包后同级目录）
    app_root = _app_root()
    if app_root:
        paths += [
            os.path.join(app_root, exe),
            os.path.join(app_root, "tor", exe),
            os.path.join(app_root, "Tor", exe),
        ]
    return paths


def _app_root() -> str:
    """可执行文件所在目录（源码运行时是项目根目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and (
        os.name == "nt" or os.access(path, os.X_OK)
    )


_VERSION_RE = re.compile(r"Tor version (\d+)\.(\d+)\.(\d+)")


def tor_version(path: str, timeout: float = 15.0) -> Optional[tuple]:
    """返回 ``(major, minor, patch)``；无法获取时返回 ``None``。"""
    try:
        out = subprocess.run(
            [path, "--version"],
            capture_output=True,
            timeout=timeout,
            text=True,
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(out.stdout or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def find_tor(explicit: str = "", require_version: bool = True) -> Optional[str]:
    """按优先级查找 tor：显式路径 -> 环境变量 -> PATH -> 常见安装位置。"""
    tried: List[str] = []
    if explicit:
        tried.append(explicit)
        if _is_executable(explicit):
            return explicit
    for var in ENV_VARS:
        value = os.environ.get(var)
        if value:
            tried.append(value)
            if _is_executable(value):
                return value
    from shutil import which

    for name in ("tor", "tor.exe"):
        found = which(name)
        if found and _is_executable(found):
            tried.append(found)
            if not require_version or tor_version(found):
                return found
    for path in _candidates():
        tried.append(path)
        if _is_executable(path) and (not require_version or tor_version(path)):
            return path
    # 最后再放宽一次版本要求，至少保证路径存在
    for path in tried:
        if _is_executable(path):
            return path
    return None


def describe_sources(explicit: str = "") -> List[str]:
    """给 ``doctor`` 用的候选路径列表。"""
    sources: List[str] = []
    if explicit:
        sources.append("配置: %s" % explicit)
    for var in ENV_VARS:
        if os.environ.get(var):
            sources.append("环境变量 %s=%s" % (var, os.environ[var]))
    from shutil import which

    for name in ("tor", "tor.exe"):
        found = which(name)
        if found:
            sources.append("PATH: %s" % found)
    for path in _candidates():
        if _is_executable(path):
            sources.append("常见位置: %s" % path)
    return sources
