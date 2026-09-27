"""配置加载：优先用标准库 ``tomllib``（Python 3.11+），否则用内置的精简 TOML 解析器。

精简解析器只覆盖本项目配置文件用到的语法子集，作用是**零第三方依赖**：
没有 tomllib 的 Python 3.8~3.10 也能直接跑。
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Tuple

try:  # Python 3.11+
    import tomllib as _tomllib
except ImportError:  # pragma: no cover - 老版本 Python
    _tomllib = None

APP_NAME = "torsocks5"


def default_config_dir() -> str:
    """跨平台的用户配置目录。"""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME)
    if os.sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_NAME)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_NAME)


def default_data_dir() -> str:
    """tor 数据目录（缓存、geoip、控制端口 cookie）。"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME)
    if os.sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Caches", APP_NAME)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, APP_NAME)


def default_config_path() -> str:
    return os.path.join(default_config_dir(), "config.toml")


def default_bridges_path() -> str:
    return os.path.join(default_config_dir(), "bridges.toml")


class ConfigError(Exception):
    """配置文件错误。"""


# --------------------------------------------------------------------- 精简 TOML
_KEY_RE = re.compile(r'^\s*((?:"[^"]*"|\'[^\']*\'|[A-Za-z0-9_.\-]+)\s*)=\s*(.*)$')
_TABLE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _strip_comment(line: str) -> str:
    out = []
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(line):
                out.append(line[i : i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
            out.append(ch)
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).strip()


def _parse_value(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        raise ConfigError("缺少值")
    if raw[0] == '"' and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1].encode("utf-8").decode("unicode_escape")
    if raw[0] == "'" and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1]
    if raw[0] == "[":
        return _parse_array(raw)
    if raw[0] == "{":
        return _parse_inline_table(raw)
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(raw, 0) if raw.lower().startswith(("0x", "0o", "0b")) else int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _split_top_level(body: str) -> List[str]:
    items: List[str] = []
    depth = 0
    quote = None
    current: List[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if quote:
            current.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(body):
                current.append(body[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            current.append(ch)
        elif ch in "[{":
            depth += 1
            current.append(ch)
        elif ch in "]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    if "".join(current).strip():
        items.append("".join(current))
    return [item.strip() for item in items if item.strip()]


def _parse_array(raw: str) -> List[Any]:
    if not raw.rstrip().endswith("]"):
        raise ConfigError("数组没有闭合: %r" % raw)
    return [_parse_value(item) for item in _split_top_level(raw.strip()[1:-1])]


def _parse_inline_table(raw: str) -> Dict[str, Any]:
    if not raw.rstrip().endswith("}"):
        raise ConfigError("内联表没有闭合: %r" % raw)
    out: Dict[str, Any] = {}
    for item in _split_top_level(raw.strip()[1:-1]):
        key, _, value = item.partition("=")
        out[key.strip().strip("\"'")] = _parse_value(value)
    return out


def loads(text: str) -> Dict[str, Any]:
    """解析 TOML 文本（精简实现）。"""
    root: Dict[str, Any] = {}
    table: Dict[str, Any] = root
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        index += 1
        line = _strip_comment(raw_line)
        if not line:
            continue
        table_match = _TABLE_RE.match(line)
        if table_match:
            table = root
            for part in table_match.group(1).split("."):
                part = part.strip().strip("\"'")
                table = table.setdefault(part, {})
                if not isinstance(table, dict):
                    raise ConfigError("表名冲突: %s" % part)
            continue
        # 多行数组
        if line.count("[") > line.count("]") and "=" in line:
            while index < len(lines) and line.count("[") > line.count("]"):
                line += " " + _strip_comment(lines[index])
                index += 1
        key_match = _KEY_RE.match(line)
        if not key_match:
            raise ConfigError("无法解析的行: %r" % raw_line)
        key = key_match.group(1).strip().strip("\"'")
        value_raw = key_match.group(2)
        if value_raw.strip().startswith("[") and value_raw.strip().count("[") > value_raw.strip().count("]"):
            while index < len(lines) and value_raw.count("[") > value_raw.count("]"):
                value_raw += " " + _strip_comment(lines[index])
                index += 1
        table[key] = _parse_value(value_raw)
    return root


def load_toml(path: str) -> Dict[str, Any]:
    """从文件读取 TOML。"""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        raise ConfigError("配置文件不存在: %s" % path) from None
    except OSError as exc:
        raise ConfigError("无法读取配置文件 %s: %s" % (path, exc)) from None
    if _tomllib is not None:
        try:
            return _tomllib.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ConfigError("配置文件 %s 语法错误: %s" % (path, exc)) from None
    return loads(raw.decode("utf-8"))


# --------------------------------------------------------------------- 配置对象
def _dig(data: Dict[str, Any], path: str, default: Any) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


DEFAULTS: List[Tuple[str, Any]] = [
    ("proxy.listen", "127.0.0.1"),
    ("proxy.port", 9051),
    ("proxy.username", ""),
    ("proxy.password", ""),
    ("proxy.allow_from", ["127.0.0.1", "::1"]),
    ("proxy.max_connections", 512),
    ("proxy.idle_timeout", 300),
    ("proxy.connect_timeout", 30),
    ("proxy.udp_associate", True),
    ("proxy.verbose", False),
    ("tor.binary", ""),
    ("tor.socks_port", 0),
    ("tor.control_port", 0),
    ("tor.data_dir", ""),
    ("tor.log_level", "notice"),
    ("tor.extra_options", []),
    ("tor.meek_mode", "plugin"),
    ("tor.direct", False),
    ("tor.restart", True),
    ("tor.isolation", ["IsolateSOCKSAuth", "IsolateClientProtocol"]),
    ("meek.methods", ["meek", "meek_lite", "meek_azure"]),
    ("meek.connect_timeout", 20.0),
    ("meek.read_timeout", 30.0),
    ("meek.verbose", False),
    ("bridges.file", ""),
    ("bridges.builtin", True),
]


class Config:
    """点号路径访问的配置对象。"""

    def __init__(self, data: Dict[str, Any], path: str = "") -> None:
        self.data = data
        self.path = path

    @classmethod
    def load(cls, path: str = "") -> "Config":
        path = path or os.environ.get("TORSOCKS5_CONFIG") or default_config_path()
        if not os.path.exists(path):
            return cls({}, path)
        return cls(load_toml(path), path)

    def get(self, key: str, default: Any = None) -> Any:
        for name, value in DEFAULTS:
            if name == key:
                default = value
                break
        value = _dig(self.data, key, None)
        return default if value is None else value

    def section(self, name: str) -> Dict[str, Any]:
        value = _dig(self.data, name, {})
        return value if isinstance(value, dict) else {}

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for name, value in DEFAULTS:
            node = out
            parts = name.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = self.get(name)
        # 保留用户额外写的键
        for key, value in self.data.items():
            if isinstance(value, dict):
                merged = out.setdefault(key, {})
                if isinstance(merged, dict):
                    for sub_key, sub_value in value.items():
                        merged.setdefault(sub_key, sub_value)
            else:
                out.setdefault(key, value)
        return out

    # 常用派生路径 -------------------------------------------------
    @property
    def data_dir(self) -> str:
        return self.get("tor.data_dir") or default_data_dir()

    @property
    def bridges_path(self) -> str:
        return self.get("bridges.file") or default_bridges_path()
