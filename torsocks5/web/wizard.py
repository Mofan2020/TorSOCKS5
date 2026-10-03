"""配置向导后端：环境检测、配置文本就地更新（保留注释）。

纯函数实现，便于单测；HTTP 层的编排在 ``server.py``。
"""

from __future__ import annotations

import os
import platform
import re
import sys
from typing import Any, Dict, List, Optional

from .. import config as config_mod
from .. import routes as routes_mod
from ..tor import find as tor_find
from ..tor.manager import port_available

_SECTION_RE = re.compile(r"^\s*\[\[?([^\]]+)\]\]?\s*$")


# --------------------------------------------------------------------- 环境检测
def _check(key: str, label: str, ok: bool, detail: str,
           fix: str = "") -> Dict[str, Any]:
    return {"key": key, "label": label, "ok": bool(ok),
            "detail": detail, "fix": fix if not ok else ""}


def env_checks(config: Any, route_name: str = "", listen: str = "",
               bridge_count: int = 0) -> Dict[str, Any]:
    """收集向导第 1 步所需的全部检测结果与表单预填数据。"""
    checks: List[Dict[str, Any]] = []

    # 1) Python 版本
    ver = sys.version_info
    checks.append(_check(
        "python", "Python 运行环境",
        ver >= (3, 10),
        "%d.%d.%d" % (ver.major, ver.minor, ver.micro),
        "升级到 Python 3.10 及以上（项目最低要求）",
    ))

    # 2) tor 可执行文件
    explicit = str(config.get("tor.binary") or "")
    tor_path = tor_find.find_tor(explicit)
    checks.append(_check(
        "tor", "tor 主程序",
        bool(tor_path),
        tor_path or "未找到 tor（tor-meek 路由需要它）",
        "macOS: brew install tor；Debian/Ubuntu: sudo apt install tor；"
        "或下载后用 run --tor /路径/tor 指定",
    ))

    # 3) 配置文件
    path = getattr(config, "path", "") or ""
    exists = bool(path) and os.path.isfile(path)
    checks.append(_check(
        "config", "配置文件",
        exists,
        path + ("" if exists else "（尚未生成）"),
        "本机暂无配置文件；向导下一步保存后会自动创建",
    ))

    # 4) 监听端口
    configured_port = int(config.get("proxy.port") or 9051)
    listen_host = str(config.get("proxy.listen") or "127.0.0.1")
    if listen and (":" + str(configured_port)) in str(listen):
        port_ok, port_detail = True, "已在监听 %s" % listen
    else:
        port_ok = port_available(configured_port, listen_host)
        port_detail = ("%s:%d 空闲" % (listen_host, configured_port)) if port_ok \
            else ("%s:%d 已被占用" % (listen_host, configured_port))
    checks.append(_check(
        "port", "SOCKS5 监听端口", port_ok, port_detail,
        "换一个端口（上一步），或停止占用该端口的程序",
    ))

    # 5) 当前路由
    current_route = route_name or str(config.get("proxy.route") or "")
    title = summary = ""
    for name, row_title, row_summary in routes_mod.describe_table():
        if name == current_route:
            title, summary = row_title, row_summary
            break
    checks.append(_check(
        "route", "流量路由", bool(current_route),
        "%s —— %s" % (current_route or "未设置", summary or title),
        "在上一步选择一种路由方式",
    ))

    # 6) 出口中继（cf-relay / self-relay 才需要）
    relay_kind = "none"
    relay_url = ""
    token_set = False
    if current_route in ("cf-relay", "self-relay"):
        section = "cf_relay" if current_route == "cf-relay" else "self_relay"
        relay_kind = "cf" if section == "cf_relay" else "self"
        relay_url = str(config.get(section + ".url") or "")
        token_set = bool(str(config.get(section + ".token") or ""))
        checks.append(_check(
            "relay", "中继地址",
            bool(relay_url),
            relay_url or "[%s] url 未配置" % section,
            "在第 3 步填写中继地址：自建中继运行 `torsocks5 relay serve`，"
            "Worker 中转自行部署（见 docs/routes.md）",
        ))
    else:
        checks.append(_check(
            "relay", "中继地址", True, "当前路由不依赖中继（tor-meek 直连 Tor 网桥）", ""))

    # 7) 网桥（仅 tor-meek 需要）
    if current_route == "tor-meek":
        checks.append(_check(
            "bridges", "meek 网桥",
            bridge_count > 0,
            "已启用 %d 条" % bridge_count,
            "在第 3 步一键向 tor 官网请求，或手动添加 Bridge 行",
        ))
    else:
        checks.append(_check(
            "bridges", "meek 网桥", True,
            "当前路由不使用网桥（已保存 %d 条备用）" % bridge_count, ""))

    # 8) 热重载（信息项，永远 ok）
    hot_on = bool(config.get("hotreload.enabled"))
    checks.append(_check(
        "hotreload", "配置热重载", True,
        "已启用（改配置即时生效）" if hot_on else "未启用（保存后需重启进程生效）", ""))

    return {
        "checks": checks,
        "os": {"system": platform.system(), "release": platform.release(),
               "platform": sys.platform},
        "python": "%d.%d.%d" % (ver.major, ver.minor, ver.micro),
        "config_path": path,
        "route_options": [
            {"name": name, "title": row_title, "summary": row_summary}
            for name, row_title, row_summary in routes_mod.describe_table()
        ],
        "form": {
            "listen": listen_host,
            "port": configured_port,
            "route": current_route,
            "username": str(config.get("proxy.username") or ""),
        },
        "relay": {"kind": relay_kind, "url": relay_url, "token_set": token_set},
        "bridge_count": bridge_count,
    }


# ------------------------------------------------------------------ 配置文本更新
def toml_value(value: Any) -> str:
    """把 Python 值序列化成 TOML 字面量。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')


def _find_section(lines: List[str], section: str) -> Optional[List[int]]:
    """返回 ``[section]`` 的 [起始行, 结束行) 区间（结束=下一个节头）。"""
    start = None
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if not match or match.group(0).lstrip().startswith("[["):
            continue
        # 精确的普通表头 [section]（[[array]] 不算）
        if line.strip() != "[%s]" % section:
            continue
        if start is None:
            start = index
            continue
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _SECTION_RE.match(lines[index]):
            end = index
            break
    return [start, end]


def apply_updates(text: str, updates: Dict[str, Dict[str, Any]]) -> str:
    """把「节 → 键值」就地写进配置文本。

    - 已存在的键：替换该键所在行；
    - 节内缺失的键：插入到节尾（紧跟最后一行内容）；
    - 节不存在：在文件末尾追加新节。
    其余内容（注释、空行、未涉及的键）原样保留。
    """
    lines = text.splitlines() if text else []
    for section, kv in updates.items():
        for key, value in kv.items():
            new_line = "%s = %s" % (key, toml_value(value))
            span = _find_section(lines, section)
            if span is None:
                # 追加新节（文件末尾）
                if lines and lines[-1].strip():
                    lines.append("")
                lines.append("[%s]" % section)
                lines.append(new_line)
                continue
            start, end = span
            key_re = re.compile(r"^\s*%s\s*=" % re.escape(key))
            replaced = False
            for index in range(start + 1, end):
                if key_re.match(lines[index]):
                    lines[index] = new_line
                    replaced = True
                    break
            if not replaced:
                insert_at = end
                while insert_at > start + 1 and not lines[insert_at - 1].strip():
                    insert_at -= 1
                lines.insert(insert_at, new_line)
    result = "\n".join(lines)
    if text.endswith("\n") or not text:
        result += "\n"
    return result


def validate_or_error(new_text: str) -> Optional[str]:
    """语法校验：通过返回 None，失败返回错误信息（不落盘）。"""
    try:
        config_mod.loads(new_text)
    except Exception as exc:  # noqa: BLE001 - 解析器异常类型不统一
        return "生成的配置解析失败: %s" % exc
    return None
