"""主机名 / 地址匹配规则。

隧道中继的白名单、客户端智能分流、断流判断共用这里的语义，避免四个实现各写一套：

* **后缀匹配**：``github.com`` 同时命中 ``github.com`` 与 ``api.github.com``；
* **以点开头**：``.githubusercontent.com`` 只命中子域，不命中 ``githubusercontent.com`` 本身；
* 比较前统一转小写并去掉结尾的点（``GitHub.COM.`` == ``github.com``）；
* ``*`` 表示全部放行。

私网判断统一用 ``ipaddress`` 的 ``is_global``，这样 CGNAT（100.64/10）、保留段、
链路本地、IPv6 唯一本地地址都会被正确拦下，不需要手写一堆网段。
"""

from __future__ import annotations

import ipaddress
from typing import Iterable, List, Optional, Sequence

WILDCARD = "*"


def normalize_host(host: str) -> str:
    """规整主机名：去空白、转小写、去掉结尾的点、去掉 IPv6 的方括号。"""
    text = (host or "").strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    while text.endswith("."):
        text = text[:-1]
    return text


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(normalize_host(host))
    except ValueError:
        return False
    return True


def is_private_address(host: str) -> bool:
    """目标是不是「中继不该连」的地址（私有 / 回环 / 链路本地 / 保留段）。

    域名返回 ``False``（域名是否指向内网由中继侧的平台解析策略决定）。
    """
    text = normalize_host(host)
    if "%" in text:  # 带 scope id 的 IPv6，如 fe80::1%eth0
        text = text.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    return not addr.is_global


def host_matches(host: str, patterns: Iterable[str]) -> bool:
    """``host`` 是否命中 ``patterns`` 中的任意一条（见模块 docstring 的匹配规则）。"""
    target = normalize_host(host)
    if not target:
        return False
    for raw in patterns or ():
        pattern = normalize_host(str(raw))
        if not pattern:
            continue
        if pattern == WILDCARD:
            return True
        if pattern.startswith("."):
            if len(target) > len(pattern) and target.endswith(pattern):
                return True
        elif target == pattern or target.endswith("." + pattern):
            return True
    return False


def split_list(text: Optional[object]) -> List[str]:
    """把 ``"a.com, b.com"`` / 列表 / ``None`` 统一成字符串列表。"""
    if text is None:
        return []
    items: List[str] = []
    if isinstance(text, (list, tuple, set)):
        for item in text:
            items.extend(split_list(item))
        return items
    for piece in str(text).replace(";", ",").replace("\n", ",").split(","):
        piece = piece.strip()
        if piece:
            items.append(piece)
    return items


def filter_targets(hosts: Sequence[str], patterns: Iterable[str]) -> List[str]:
    """从 ``hosts`` 里筛出命中 ``patterns`` 的项（用于自检命令）。"""
    return [host for host in hosts if host_matches(host, patterns)]
