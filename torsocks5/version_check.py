"""版本更新检查：启动时后台查询 GitHub Releases，结果缓存，默认 24 小时一次。

设计约束（对应验收标准「启动 5s 内异步完成，有新版本在日志/Web 面板可见」）：

- **不阻塞**：``start()`` 只拉一个守护线程，网络超时 3 秒，主线程不受影响；
- **失败静默**：中国大陆访问 GitHub 可能失败，错误只写进内存状态
  （``get_status()["error"]``），不打印、不重试，完全不影响正常使用；
- **结果缓存**：写在配置目录 ``version_check.json``（原子写），有效期内
  直接用缓存，不再发网络请求；
- **展示位置**：发现新版本时启动日志打一行提示；Web 面板仪表盘显示横幅
  （``/api/status`` 的 ``update`` 字段）。

环境变量 ``TORSOCKS5_VERSION_CHECK_URL`` 可覆盖接口地址、
``TORSOCKS5_VERSION_CHECK_CACHE`` 可覆盖缓存路径（测试 / 沙箱隔离 / 镜像源），
响应接受 GitHub JSON（``tag_name``）或纯文本版本号。
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, Optional

#: GitHub Releases 最新版本接口
GITHUB_LATEST_URL = "https://api.github.com/repos/Mofan2020/TorSOCKS5/releases/latest"
#: 提示里展示的发布页地址
RELEASES_URL = "https://github.com/Mofan2020/TorSOCKS5/releases/latest"
#: 覆盖接口地址（测试 / 镜像）
ENV_URL = "TORSOCKS5_VERSION_CHECK_URL"
#: 覆盖缓存文件路径（测试 / 沙箱隔离）
ENV_CACHE = "TORSOCKS5_VERSION_CHECK_CACHE"
#: 网络超时（秒）——远小于「5 秒内完成」的验收线
DEFAULT_TIMEOUT = 3.0

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "enabled": True,
    "current": None,
    "latest": None,
    "has_update": False,
    "url": RELEASES_URL,
    "checked_at": None,
    "error": None,
}
_thread: Optional[threading.Thread] = None


# --------------------------------------------------------------------- 版本比较
def parse_version(text: str) -> tuple:
    """把 ``v2.10.0`` / ``2.1.0-beta`` 解析成可比较的整数元组。

    去掉 ``v`` 前缀与预发布后缀，按 ``.`` 切分并取每段开头的数字；
    不足三段补零，保证 ``(2, 1) == (2, 1, 0)``。
    """
    cleaned = (text or "").strip().lstrip("vV")
    cleaned = cleaned.split("-", 1)[0].split("+", 1)[0]
    parts = []
    for piece in cleaned.split("."):
        digits = ""
        for ch in piece:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def has_newer(current: Optional[str], latest: Optional[str]) -> bool:
    """``latest`` 是否比 ``current`` 更新（任一侧为空/不可解析 → 否）。"""
    if not current or not latest:
        return False
    return parse_version(latest) > parse_version(current)


# --------------------------------------------------------------------- 磁盘缓存
def _load_cache(path: str, max_age_hours: float) -> Optional[Dict[str, Any]]:
    """读缓存；过期或损坏返回 None（任何异常都视为无缓存）。"""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        age = time.time() - float(data.get("checked_at") or 0)
        if 0 <= age < max_age_hours * 3600 and data.get("latest"):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def _save_cache(path: str, latest: str, current: Optional[str]) -> None:
    """原子写缓存；失败静默（缓存只是优化，不是必需品）。"""
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"checked_at": time.time(), "latest": latest,
                       "current": current}, handle)
        os.replace(tmp, path)
    except OSError:
        pass


# --------------------------------------------------------------------- 网络请求
def fetch_latest_tag(timeout: float = DEFAULT_TIMEOUT,
                     verify_ssl: bool = False) -> str:
    """请求最新发布标签（GitHub JSON 或纯文本镜像）。

    ``verify_ssl`` 默认 **False（关闭证书校验）**：GitHub 相关请求默认跳过
    SSL 认证——国内经加速器/代理访问 GitHub 时证书异常非常普遍，校验只会
    让功能在最需要它的网络环境里失效。需要严格校验时显式传 True。
    """
    url = os.environ.get(ENV_URL) or GITHUB_LATEST_URL
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "TorSOCKS5-version-check",
    })
    context = None
    if not verify_ssl:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {"tag_name": body.strip()}  # 纯文本镜像
    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        raise ValueError("版本接口响应缺少 tag_name")
    return tag


# --------------------------------------------------------------------- 状态与线程
def get_status() -> Dict[str, Any]:
    """当前检查状态的副本（供 /api/status 等读取）。"""
    with _lock:
        return dict(_state)


def reset() -> None:
    """恢复初始状态（测试用）。"""
    global _thread
    with _lock:
        _state.update(enabled=True, current=None, latest=None, has_update=False,
                      checked_at=None, error=None)
        _thread = None


def wait(timeout: float = 5.0) -> bool:
    """等待后台检查线程结束（测试用）。返回 True 表示已结束。"""
    with _lock:
        thread = _thread
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()


def start(
    current: str,
    *,
    enabled: bool = True,
    cache_path: Optional[str] = None,
    interval_hours: float = 24.0,
    timeout: float = DEFAULT_TIMEOUT,
    verify_ssl: bool = False,
    on_result: Optional[Callable[[Dict[str, Any]], None]] = None,
    fetcher: Optional[Callable[[], str]] = None,
) -> bool:
    """后台启动一次版本检查；幂等（已有线程在跑则直接返回）。

    :returns: 是否真正启动了线程（``enabled=False`` 或已在跑 → False）
    """
    global _thread
    if not enabled:
        with _lock:
            _state["enabled"] = False
        return False
    if cache_path is None:
        # 解析顺序：环境变量（测试/沙箱隔离）→ 用户配置目录
        cache_path = os.environ.get(ENV_CACHE)
    if not cache_path:
        from .config import default_config_dir

        cache_path = os.path.join(default_config_dir(), "version_check.json")
    with _lock:
        if _thread is not None and _thread.is_alive():
            _state["enabled"] = True
            return False
        _state["enabled"] = True
        _state["current"] = current
        thread = threading.Thread(
            target=_run,
            kwargs={
                "current": current,
                "cache_path": cache_path,
                "interval_hours": interval_hours,
                "timeout": timeout,
                "verify_ssl": verify_ssl,
                "on_result": on_result,
                "fetcher": fetcher,
            },
            daemon=True,
            name="torsocks5-version-check",
        )
        _thread = thread
    thread.start()
    return True


def _run(*, current: str, cache_path: str, interval_hours: float,
         timeout: float, verify_ssl: bool,
         on_result: Optional[Callable[[Dict[str, Any]], None]],
         fetcher: Optional[Callable[[], str]]) -> None:
    """线程体：缓存命中用缓存，否则发请求；一切异常收敛为 error 字段。"""
    do_fetch = fetcher or (
        lambda: fetch_latest_tag(timeout=timeout, verify_ssl=verify_ssl)
    )
    try:
        cached = _load_cache(cache_path, interval_hours)
        if cached is not None:
            latest = str(cached["latest"])
            checked_at = cached.get("checked_at")
        else:
            latest = do_fetch()
            checked_at = time.time()
            _save_cache(cache_path, latest, current)
        with _lock:
            _state.update(latest=latest, checked_at=checked_at,
                          has_update=has_newer(current, latest), error=None)
    except Exception as exc:  # noqa: BLE001 - 网络/解析异常类型不统一，失败必须静默
        with _lock:
            _state["error"] = "%s: %s" % (type(exc).__name__, exc)
    if on_result is not None:
        try:
            on_result(get_status())
        except Exception:  # noqa: BLE001 - 回调出错同样不能影响主流程
            pass
