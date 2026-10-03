"""从 torproject.org 请求 meek 网桥（按需、失败不影响使用）。"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

from . import log as log_mod
from .bridges import BridgeError, BridgeLine, parse_bridge_line

BRIDGES_EMAIL = "bridges@torproject.org"
BRIDGES_URL = "https://bridges.torproject.org/bridges"
BRIDGES_API_URL = "https://bridges.torproject.org/api/v1/bridges"


class BridgeFetchError(Exception):
    """获取网桥失败。"""
    pass


def _extract_meek_bridges_from_html(html: str) -> List[str]:
    """从 HTML 页面提取 meek 网桥行。"""
    # bridges.torproject.org 返回的页面中，meek 网桥通常在 <pre> 或特定格式中
    # 尝试多种模式
    bridges = []

    # 模式 1: <pre> 标签内的 Bridge 行
    pre_pattern = re.compile(r'<pre[^>]*>(.*?)</pre>', re.DOTALL | re.IGNORECASE)
    for match in pre_pattern.finditer(html):
        content = match.group(1)
        for line in content.splitlines():
            line = line.strip()
            if line.lower().startswith("bridge meek"):
                bridges.append(line)

    # 模式 2: 直接在文本中搜索 Bridge meek 行
    if not bridges:
        bridge_pattern = re.compile(r'(Bridge\s+meek(?:_lite|_azure)?\s+\S.*)', re.IGNORECASE)
        for match in bridge_pattern.finditer(html):
            bridges.append(match.group(1).strip())

    # 去重
    seen = set()
    unique = []
    for b in bridges:
        if b not in seen:
            seen.add(b)
            unique.append(b)

    return unique


def _extract_meek_bridges_from_json(data: dict) -> List[str]:
    """从 JSON API 响应提取 meek 网桥行。"""
    bridges = []
    # API 可能返回不同结构，尝试兼容
    if isinstance(data, dict):
        # 可能的结构: {"bridges": ["Bridge meek ...", ...]}
        if "bridges" in data and isinstance(data["bridges"], list):
            for item in data["bridges"]:
                if isinstance(item, str) and item.lower().startswith("bridge meek"):
                    bridges.append(item)
        # 或者直接是列表
        elif "data" in data and isinstance(data["data"], list):
            for item in data["data"]:
                if isinstance(item, str) and item.lower().startswith("bridge meek"):
                    bridges.append(item)
    return bridges


def fetch_bridges_via_email(email: str = "", transport: str = "meek") -> Tuple[List[str], str]:
    """
    通过邮件方式获取网桥（模拟发送邮件到 bridges@torproject.org）。
    实际上这里打印邮件内容让用户自己发送，因为我们无法直接发送邮件。
    """
    subject = ""
    body = f"get transport {transport}"
    if email:
        body += f"\nemail {email}"

    message = f"""要通过邮件获取 {transport} 网桥，请发送以下邮件：

收件人: {BRIDGES_EMAIL}
主题: {subject}
正文:
{body}

收到回复后，将网桥行复制并用以下命令导入：
  torsocks5 bridges import --clipboard
  # 或
  torsocks5 bridges import --file bridges.txt
"""
    return [], message


def fetch_bridges_via_https(
    transport: str = "meek",
    timeout: float = 30.0,
    logger: Optional[log_mod.Logger] = None
) -> List[str]:
    """
    通过 HTTPS API 获取 meek 网桥。
    注意：bridges.torproject.org 可能需要验证码或有速率限制。
    失败不抛出异常，返回空列表并记录警告。
    """
    if logger is None:
        logger = log_mod.Logger(level="info")

    bridges = []

    # 尝试 API 端点
    try:
        params = urllib.parse.urlencode({"transport": transport})
        url = f"{BRIDGES_API_URL}?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "TorSOCKS5/2.0.0",
                "Accept": "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8", "replace"))
                bridges = _extract_meek_bridges_from_json(data)
                if bridges:
                    logger.info(f"从 API 获取到 {len(bridges)} 条 meek 网桥")
                    return bridges
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
        logger.debug(f"API 获取失败: {exc}")

    # 尝试网页端点
    try:
        params = urllib.parse.urlencode({"transport": transport})
        url = f"{BRIDGES_URL}?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; TorSOCKS5/2.0.0)",
                "Accept": "text/html",
            }
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                html = resp.read().decode("utf-8", "replace")
                bridges = _extract_meek_bridges_from_html(html)
                if bridges:
                    logger.info(f"从网页获取到 {len(bridges)} 条 meek 网桥")
                    return bridges
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        logger.debug(f"网页获取失败: {exc}")

    logger.warn("无法从 torproject.org 自动获取网桥（可能需要验证码、速率限制或网络不通）")
    logger.info("请手动访问 https://bridges.torproject.org 选择 Meek 类型获取网桥")
    return []


def validate_and_normalize_bridges(lines: List[str]) -> Tuple[List[BridgeLine], List[str]]:
    """验证并规范化网桥行，返回 (成功列表, 错误信息列表)。"""
    valid = []
    errors = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            bridge = parse_bridge_line(line)
            if bridge.is_meek:
                valid.append(bridge)
            else:
                errors.append(f"非 meek 网桥已跳过: {line[:60]}...")
        except BridgeError as exc:
            errors.append(f"{line[:60]}... -> {exc}")
    return valid, errors


def cmd_bridges_fetch(args, logger: log_mod.Logger) -> int:
    """CLI 命令：获取 meek 网桥。"""
    transport = getattr(args, "transport", "meek")
    method = getattr(args, "method", "auto")  # auto | https | email
    timeout = float(getattr(args, "timeout", 30))

    logger.plain(f"尝试获取 {transport} 网桥...")

    bridges = []
    if method in ("auto", "https"):
        bridges = fetch_bridges_via_https(transport, timeout, logger)

    if not bridges and method in ("auto", "email"):
        _, msg = fetch_bridges_via_email(getattr(args, "email", ""), transport)
        logger.plain(msg)
        return 0

    if not bridges:
        logger.error("未获取到任何 meek 网桥")
        logger.info("建议：手动访问 https://bridges.torproject.org 选择 Meek 类型获取")
        return 1

    # 验证并规范化
    valid, errors = validate_and_normalize_bridges(bridges)
    for err in errors:
        logger.warn(err)

    if not valid:
        logger.error("获取到的网桥行均无效")
        return 1

    # 显示获取到的网桥
    logger.ok(f"成功获取 {len(valid)} 条有效 meek 网桥：")
    for bridge in valid:
        logger.plain(f"  {bridge.to_torrc()}")

    # 如果指定了 --add，直接添加到配置
    if getattr(args, "add", False):
        from . import config as config_mod
        from .bridges import BridgeStore
        config = config_mod.Config.load(getattr(args, "config", ""))
        store = BridgeStore(config.bridges_path)
        store.load(include_builtin=False)
        added = 0
        for bridge in valid:
            if store.add(bridge):
                added += 1
        store.save()
        logger.ok(f"已添加 {added} 条新网桥到 {store.path}")

    return 0


__all__ = [
    "fetch_bridges_via_https",
    "fetch_bridges_via_email",
    "validate_and_normalize_bridges",
    "cmd_bridges_fetch",
    "BridgeFetchError",
]
