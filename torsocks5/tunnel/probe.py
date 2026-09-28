"""中继探测：真的连上线、真的转发一次数据，而不是只看端口通不通。

``torsocks5 tunnel probe`` / ``torsocks5 tunnel check`` 用它。输出是一份可读报告，
README 里的实测数据也是从这里来的。
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .. import __version__
from .client import (
    TargetNotAllowed,
    TargetUnreachable,
    TunnelClient,
    TunnelError,
    TunnelUnavailable,
)
from .protocol import DEFAULT_PATH, HEALTH_PATH
from .relay import _human

#: 默认探测目标：常见的「需要辅助访问」的学习类站点
DEFAULT_PROBE_TARGETS: Sequence[Tuple[str, int]] = (
    ("github.com", 443),
    ("codeload.github.com", 443),
    ("raw.githubusercontent.com", 443),
    ("pypi.org", 443),
    ("files.pythonhosted.org", 443),
    ("cdn.jsdelivr.net", 443),
    ("huggingface.co", 443),
    ("cdn-lfs.huggingface.co", 443),
    ("registry-1.docker.io", 443),
    ("ghcr.io", 443),
    ("storage.googleapis.com", 443),
)


def http_url_of(ws_url: str, path: str) -> str:
    """把 ``wss://host/tsu`` 变成 ``https://host/healthz``。"""
    if ws_url.startswith("wss://"):
        base = "https://" + ws_url[len("wss://") :]
    elif ws_url.startswith("ws://"):
        base = "http://" + ws_url[len("ws://") :]
    else:
        base = ws_url
    host = base.split("//", 1)[1].split("/", 1)[0]
    return "%s://%s%s" % (base.split(":", 1)[0], host, path)


def probe_health(url: str, *, timeout: float = 10.0, insecure: bool = False,
                 path: str = HEALTH_PATH) -> Dict[str, Any]:
    """请求中继的 ``/healthz``（不需要令牌），确认它在线且是本协议实现。"""
    target = http_url_of(url, path)
    context = None
    if target.startswith("https://"):
        context = ssl.create_default_context()
        if insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(target, headers={"User-Agent": "TorSOCKS5/%s" % __version__})
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            body = response.read(4096).decode("utf-8", "replace")
            elapsed = (time.time() - started) * 1000
        try:
            data = json.loads(body)
        except ValueError:
            return {"ok": False, "url": target, "error": "返回不是 JSON: %s" % body[:120],
                    "ms": round(elapsed, 1)}
        data.update({"ok": bool(data.get("ok")), "url": target, "ms": round(elapsed, 1)})
        return data
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "url": target, "error": str(exc)}


def probe_targets(
    client: TunnelClient,
    targets: Sequence[Tuple[str, int]],
    *,
    timeout: float = 15.0,
    http_probe: bool = False,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> List[Dict[str, Any]]:
    """逐个目标做一次真实转发：``OPEN`` → 可选发一段 HTTP 请求 → 读回响应。"""
    results: List[Dict[str, Any]] = []
    for host, port in targets:
        started = time.time()
        item: Dict[str, Any] = {"host": host, "port": port}
        try:
            stream = client.connect(host, port, timeout=timeout)
        except TargetNotAllowed as exc:
            item.update({"ok": False, "stage": "policy", "error": "中继不允许：%s" % exc})
        except (TargetUnreachable, TunnelUnavailable, TunnelError, OSError) as exc:
            item.update({"ok": False, "stage": "connect", "error": str(exc)})
        else:
            item["connect_ms"] = round((time.time() - started) * 1000, 1)
            try:
                if http_probe:
                    banner = _http_roundtrip(stream, host, timeout=timeout)
                    item["http"] = banner
                item["ok"] = True
            except (OSError, socket.timeout) as exc:
                item.update({"ok": False, "stage": "data", "error": str(exc)})
            finally:
                try:
                    stream.close()
                except OSError:
                    pass
            item["total_ms"] = round((time.time() - started) * 1000, 1)
        results.append(item)
        if on_progress is not None:
            on_progress(item)
    return results


def _http_roundtrip(stream, host: str, *, timeout: float) -> str:
    """通过隧道发一个 HTTP/1.0 请求，返回状态行（证明数据真的双向通了）。"""
    stream.settimeout(timeout)
    request = ("GET / HTTP/1.0\r\nHost: %s\r\nUser-Agent: TorSOCKS5-probe\r\n\r\n" % host)
    stream.sendall(request.encode("ascii"))
    data = b""
    deadline = time.time() + timeout
    while b"\r\n" not in data and time.time() < deadline and len(data) < 4096:
        chunk = stream.recv(512)
        if not chunk:
            break
        data += chunk
    return data.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()


def run_probe(
    url: str,
    token: str = "",
    *,
    targets: Optional[Sequence[Tuple[str, int]]] = None,
    timeout: float = 15.0,
    http_probe: bool = False,
    front: str = "",
    insecure: bool = False,
    token_in_header: bool = False,
    links: int = 2,
    max_streams: int = 6,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """完整探测：健康检查 + 逐个目标真实转发。"""
    report: Dict[str, Any] = {"url": url}
    report["health"] = probe_health(url, timeout=timeout, insecure=insecure)
    client = TunnelClient(
        url,
        token,
        links=links,
        max_streams=max_streams,
        open_timeout=timeout,
        keepalive=30.0,
        auto_reconnect=False,
        on_log=on_log,
        front=front,
        insecure=insecure,
        token_in_header=token_in_header,
        timeout=timeout,
    )
    try:
        client.start()
    except TunnelUnavailable as exc:
        report["ok"] = False
        report["error"] = "无法连接中继：%s" % exc
        report["targets"] = []
        return report
    try:
        report["targets"] = probe_targets(
            client,
            list(targets or DEFAULT_PROBE_TARGETS),
            timeout=timeout,
            http_probe=http_probe,
            on_progress=on_progress,
        )
    finally:
        client.close()
    items = report["targets"]
    report["ok"] = all(item.get("ok") for item in items) if items else False
    report["ok_count"] = sum(1 for item in items if item.get("ok"))
    report["total"] = len(items)
    return report


def format_report(report: Dict[str, Any]) -> str:
    """把探测结果排成给人看的表格。"""
    lines: List[str] = []
    health = report.get("health") or {}
    if health.get("ok"):
        lines.append("中继在线：%s（协议 %s，并发上限 %s，往返 %sms）"
                     % (health.get("url"), health.get("proto"), health.get("max_streams"),
                        health.get("ms")))
    else:
        lines.append("中继健康检查失败：%s" % (health.get("error") or health))
    if report.get("error"):
        lines.append(str(report["error"]))
    items = report.get("targets") or []
    for item in items:
        if item.get("ok"):
            detail = "%s:%s 通（%sms%s）" % (item["host"], item["port"], item.get("connect_ms"),
                                            ("，" + str(item["http"])) if item.get("http") else "")
        else:
            detail = "%s:%s 失败（%s）：%s" % (item["host"], item["port"],
                                            item.get("stage", "?"), item.get("error"))
        lines.append("  " + detail)
    if items:
        lines.append("合计 %d/%d 个目标可用" % (report.get("ok_count", 0), report.get("total", 0)))
    return "\n".join(lines)


__all__ = [
    "DEFAULT_PROBE_TARGETS",
    "DEFAULT_PATH",
    "format_report",
    "http_url_of",
    "probe_health",
    "probe_targets",
    "run_probe",
    "_human",
]
