"""网桥（bridge）行的解析、校验与存储。

支持的网桥类型：
    * meek / meek_lite / meek_azure —— 本项目内置的 Python 传输
    * obfs4 / snowflake / webtunnel —— 透传给 tor（需要对应的传输插件或内置支持）

典型 meek 网桥行::

    Bridge meek 0.0.2.0:3 url=https://meek.azureedge.net/ front=ajax.aspnetcdn.com
    Bridge meek_lite 192.0.2.20:80 url=https://1314488750.rsc.cdn77.org front=www.phpmyadmin.net utls=HelloRandomizedALPN
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, List, Optional, Tuple

MEEK_TRANSPORTS = ("meek", "meek_lite", "meek_azure")
KNOWN_TRANSPORTS = MEEK_TRANSPORTS + ("obfs4", "snowflake", "webtunnel", "conflux")

_FINGERPRINT_RE = re.compile(r"^[0-9A-Fa-f]{40}$")
_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")

# 公开文档里出现过的 meek 网桥。CDN 前置域名随时可能失效，正式使用请通过
# https://bridges.torproject.org 获取自己的网桥行。
BUILTIN_MEEK_BRIDGES: List[str] = [
    "Bridge meek 0.0.2.0:3 url=https://meek.azureedge.net/ front=ajax.aspnetcdn.com",
]


class BridgeError(Exception):
    """网桥行错误。"""


class BridgeLine:
    """一条网桥行。"""

    def __init__(
        self,
        transport: str,
        address: str,
        fingerprint: str = "",
        args: Optional[Dict[str, str]] = None,
        version: str = "",
        raw: str = "",
    ) -> None:
        self.transport = transport
        self.address = address
        self.fingerprint = fingerprint
        self.args: Dict[str, str] = dict(args or {})
        self.version = version
        self.raw = raw
        self.enabled = True

    # ------------------------------------------------------------------
    @property
    def is_meek(self) -> bool:
        return self.transport in MEEK_TRANSPORTS

    @property
    def key(self) -> str:
        return "%s|%s|%s|%s" % (
            self.transport,
            self.address,
            self.fingerprint.lower(),
            "&".join("%s=%s" % (k, v) for k, v in sorted(self.args.items())),
        )

    def to_torrc(self) -> str:
        parts = ["Bridge", self.transport]
        if self.version:
            parts.append(self.version)
        parts.append(self.address)
        if self.fingerprint:
            parts.append(self.fingerprint)
        for key, value in self.args.items():
            parts.append("%s=%s" % (key, value) if value != "" else key)
        return " ".join(parts)

    def to_dict(self) -> Dict[str, object]:
        data: Dict[str, object] = {
            "transport": self.transport,
            "address": self.address,
        }
        if self.fingerprint:
            data["fingerprint"] = self.fingerprint
        if self.version:
            data["version"] = self.version
        if self.args:
            data["args"] = dict(self.args)
        if not self.enabled:
            data["enabled"] = False
        if self.raw:
            data["raw"] = self.raw
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "BridgeLine":
        args = data.get("args") or {}
        if not isinstance(args, dict):
            raise BridgeError("args 必须是键值表")
        return cls(
            transport=str(data.get("transport", "")),
            address=str(data.get("address", "")),
            fingerprint=str(data.get("fingerprint", "") or ""),
            version=str(data.get("version", "") or ""),
            args={str(k): str(v) for k, v in args.items()},
            raw=str(data.get("raw", "") or ""),
        )

    def __repr__(self) -> str:  # pragma: no cover
        return "<BridgeLine %s>" % self.to_torrc()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, BridgeLine) and self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)


def _split_tokens(text: str) -> List[str]:
    """按空白切分，支持 ``key="含空格的值"``。"""
    tokens: List[str] = []
    current: List[str] = []
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
        elif ch in "\"'":
            quote = ch
        elif ch.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
        i += 1
    if current:
        tokens.append("".join(current))
    return tokens


def validate_bridge(bridge: BridgeLine) -> List[str]:
    """返回问题列表（空列表表示没问题）。"""
    problems: List[str] = []
    if not bridge.address:
        problems.append("缺少网桥地址")
    if bridge.fingerprint and not _FINGERPRINT_RE.match(bridge.fingerprint):
        problems.append("指纹必须是 40 位十六进制")
    if bridge.is_meek:
        url = bridge.args.get("url", "")
        if not url:
            problems.append("meek 网桥必须有 url= 参数")
        elif not url.startswith(("http://", "https://")):
            problems.append("url 必须以 http:// 或 https:// 开头: %s" % url)
    fingerprint_arg = bridge.args.get("fingerprint")
    if fingerprint_arg is not None:
        value = fingerprint_arg.strip()
        if value and not _FINGERPRINT_RE.match(value):
            problems.append("fingerprint= 参数不是 40 位十六进制")
        elif value:
            bridge.fingerprint = value.upper()
    return problems


def parse_bridge_line(line: str) -> BridgeLine:
    """解析一条网桥行，失败时抛出 :class:`BridgeError`。"""
    text = line.strip()
    if not text or text.startswith("#"):
        raise BridgeError("空行或注释")
    text = re.sub(r"^Bridge\s+", "", text, flags=re.IGNORECASE)
    tokens = _split_tokens(text)
    if not tokens:
        raise BridgeError("网桥行内容为空")

    transport = tokens[0].lower()
    if transport not in KNOWN_TRANSPORTS:
        raise BridgeError("未知的网桥传输类型: %s" % tokens[0])
    index = 1

    version = ""
    if index < len(tokens) and _VERSION_RE.match(tokens[index]):
        version = tokens[index]
        index += 1

    if index >= len(tokens):
        raise BridgeError("网桥行缺少地址")
    address = tokens[index]
    if ":" not in address:
        address += ":1"
    index += 1

    fingerprint = ""
    if index < len(tokens) and _FINGERPRINT_RE.match(tokens[index]):
        fingerprint = tokens[index].upper()
        index += 1

    args: Dict[str, str] = {}
    for token in tokens[index:]:
        if "=" in token:
            key, _, value = token.partition("=")
            args[key.strip()] = value.strip()
        else:
            args[token.strip()] = ""

    bridge = BridgeLine(
        transport=transport,
        address=address,
        fingerprint=fingerprint,
        args=args,
        version=version,
        raw=line.strip(),
    )
    problems = validate_bridge(bridge)
    if problems and bridge.is_meek:
        raise BridgeError("；".join(problems))
    if problems:
        raise BridgeError("；".join(problems))
    return bridge


def normalize(raw: str) -> Tuple[str, List[str]]:
    """把任意输入（多行、含说明文字）解析成规范网桥行列表。"""
    bridges: List[BridgeLine] = []
    errors: List[str] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if not text.lower().startswith("bridge "):
            text = "Bridge " + text
        try:
            bridges.append(parse_bridge_line(text))
        except BridgeError as exc:
            errors.append("%s -> %s" % (text[:60], exc))
    return "\n".join(b.to_torrc() for b in bridges), errors


def iter_lines(values) -> List[str]:
    result: List[str] = []
    for value in values or []:
        result.extend(str(value).splitlines())
    return result


# --------------------------------------------------------------------- 存储
class BridgeStore:
    """网桥集合（保存在 TOML 文件里）。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.bridges: List[BridgeLine] = []

    # --------------------------------------------------------------
    def load(self, include_builtin: bool = True) -> "BridgeStore":
        from . import config as config_module

        self.bridges = []
        if os.path.exists(self.path):
            try:
                data = config_module.load_toml(self.path)
            except config_module.ConfigError:
                data = {}
            for item in data.get("bridge") or []:
                if not isinstance(item, dict):
                    continue
                if not item.get("transport") or not item.get("address"):
                    continue
                try:
                    bridge = BridgeLine.from_dict(item)
                except BridgeError:
                    continue
                if item.get("enabled") is False:
                    bridge.enabled = False
                self.bridges.append(bridge)
        if include_builtin and not self.bridges:
            for line in BUILTIN_MEEK_BRIDGES:
                try:
                    self.bridges.append(parse_bridge_line(line))
                except BridgeError:
                    continue
        return self

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        lines = [
            "# TorSOCKS5 网桥配置（自动生成，可手工编辑）",
            "# 从 https://bridges.torproject.org 获取 meek 网桥行后执行：",
            '#   torsocks5 bridges add "Bridge meek 0.0.2.0:3 url=... front=..."',
            "",
        ]
        for bridge in self.bridges:
            lines.append("[[bridge]]")
            lines.append('transport = "%s"' % bridge.transport)
            lines.append('address = "%s"' % bridge.address)
            if bridge.fingerprint:
                lines.append('fingerprint = "%s"' % bridge.fingerprint)
            if bridge.version:
                lines.append('version = "%s"' % bridge.version)
            if not bridge.enabled:
                lines.append("enabled = false")
            if bridge.raw:
                lines.append("raw = %s" % _quote(bridge.raw))
            if bridge.args:
                # 子表必须放在所有标量键之后，否则会被当成 args 的一部分
                lines.append("[bridge.args]")
                for key, value in bridge.args.items():
                    lines.append("%s = %s" % (key, _quote(str(value))))
            lines.append("")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))

    # --------------------------------------------------------------
    def add(self, bridge: BridgeLine) -> bool:
        for existing in self.bridges:
            if existing.key == bridge.key:
                existing.enabled = True
                return False
        self.bridges.append(bridge)
        return True

    def remove(self, needle: str) -> int:
        removed = 0
        keep: List[BridgeLine] = []
        for bridge in self.bridges:
            if needle in bridge.to_torrc() or needle in bridge.key:
                removed += 1
            else:
                keep.append(bridge)
        self.bridges = keep
        return removed

    def clear(self) -> None:
        self.bridges = []

    def active(self) -> List[BridgeLine]:
        return [b for b in self.bridges if b.enabled]

    def torrc_lines(self) -> List[str]:
        return [b.to_torrc() for b in self.active()]


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % escaped


# --------------------------------------------------------------------- 剪贴板
def read_clipboard() -> str:
    """从系统剪贴板读取文本（尽力而为）。"""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                capture_output=True,
                timeout=10,
            )
            if out.returncode == 0:
                return out.stdout.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        return ""
    from shutil import which

    for command in (
        ["pbpaste"],
        ["wl-paste", "--no-newline"],
        ["xclip", "-selection", "clipboard", "-o"],
    ):
        if not which(command[0]):
            continue
        try:
            out = subprocess.run(command, capture_output=True, timeout=10)
            if out.returncode == 0 and out.stdout:
                return out.stdout.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
    return ""
