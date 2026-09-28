"""智能分流：决定一次连接是「直连」还是「走隧道」。

隧道路由（``cf-relay`` / ``self-relay``）默认都开启分流，因为这两种路由对目标有额外成本：

* ``cf-relay`` 走 Cloudflare 免费额度，且中继侧有白名单，把局域网/国内流量也塞进去毫无意义；
* ``self-relay`` 的中继可能在远端，访问局域网设备本来就不可能。

四种模式：

==================  ==========================================================
``off``             全部分流关闭，所有连接都走隧道（私有地址除外）
``smart``           命中「代理名单」才走隧道，其余直连（默认给 ``cf-relay``）
``all``             除私有/局域网地址外全部走隧道（默认给 ``self-relay``）
``auto``            由路由类型决定（见上），默认值
==================  ==========================================================

私有地址（RFC1918 / 回环 / 链路本地 / CGNAT）**永远直连**：它们不可能通过远端中继到达。
"""

from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Sequence

from .hostrules import host_matches, is_private_address, split_list

MODE_AUTO = "auto"
MODE_SMART = "smart"
MODE_ALL = "all"
MODE_OFF = "off"

MODES = (MODE_AUTO, MODE_SMART, MODE_ALL, MODE_OFF)

#: 路由 → auto 模式下的实际行为
AUTO_BY_ROUTE = {
    "tor-meek": MODE_OFF,
    "upstream": MODE_OFF,
    "cf-relay": MODE_SMART,
    "self-relay": MODE_ALL,
}

#: 内置「走隧道」名单：国内访问常年不稳的开发/学习基础设施
BUILTIN_PROXY_SUFFIXES: Sequence[str] = (
    "github.com",
    "githubusercontent.com",
    "githubassets.com",
    "github.io",
    "ghcr.io",
    "huggingface.co",
    "hf.co",
    "hf-mirror.com",
    "docker.io",
    "docker.com",
    "gcr.io",
    "quay.io",
    "k8s.io",
    "registry.k8s.io",
    "pypi.org",
    "pythonhosted.org",
    "npmjs.org",
    "npmjs.com",
    "yarnpkg.com",
    "jsdelivr.net",
    "unpkg.com",
    "nodejs.org",
    "deno.land",
    "deno.com",
    "bun.sh",
    "crates.io",
    "rust-lang.org",
    "go.dev",
    "golang.org",
    "proxy.golang.org",
    "storage.googleapis.com",
    "googleapis.com",
    "googlesource.com",
    "dl.google.com",
    "android.com",
    "chromium.org",
    "jetbrains.com",
    "ubuntu.com",
    "debian.org",
    "archlinux.org",
    "anaconda.org",
    "anaconda.com",
    "pytorch.org",
    "conda-forge.org",
    "mirrors.tuna.tsinghua.edu.cn",
    "modelscope.cn",
    "arxiv.org",
    "openreview.net",
    "paperswithcode.com",
    "kaggle.com",
    "gitlab.com",
    "bitbucket.org",
    "sourceforge.net",
    "kernel.org",
    "gnu.org",
    "openai.com",
    "anthropic.com",
    "npmjs.org",
    "registry.npmmirror.com",
)

#: 内置「直连」名单：国内站点与常见镜像，走隧道只会更慢
BUILTIN_DIRECT_SUFFIXES: Sequence[str] = (
    ".cn",
    "baidu.com",
    "qq.com",
    "tencent.com",
    "weixin.qq.com",
    "taobao.com",
    "tmall.com",
    "alibaba.com",
    "aliyun.com",
    "alicdn.com",
    "aliyuncs.com",
    "jd.com",
    "163.com",
    "126.net",
    "bilibili.com",
    "hdslb.com",
    "zhihu.com",
    "csdn.net",
    "cnblogs.com",
    "juejin.cn",
    "gitee.com",
    "weibo.com",
    "douyin.com",
    "xiaohongshu.com",
    "meituan.com",
    "alipay.com",
    "douban.com",
    "mi.com",
    "huawei.com",
    "deepin.org",
    "tsinghua.edu.cn",
    "ustc.edu.cn",
    "npmmirror.com",
    "miyoushe.com",
    "hoyolab.com",
    "qcloud.com",
    "cnbeta.com",
    "segmentfault.com",
    "oschina.net",
    "chinaunix.net",
    "tonkotsu.cn",
)

DIRECT = "direct"
TUNNEL = "tunnel"


def resolve_mode(mode: str, route_name: str) -> str:
    """把 ``auto`` 解析成具体模式；未知取值回落到 ``auto`` 的结果。"""
    value = (mode or MODE_AUTO).strip().lower()
    if value not in MODES:
        value = MODE_AUTO
    if value == MODE_AUTO:
        return AUTO_BY_ROUTE.get(route_name, MODE_ALL)
    return value


class SplitRouter:
    """按配置把目标分成「直连」与「走隧道」。"""

    def __init__(
        self,
        mode: str = MODE_AUTO,
        *,
        route_name: str = "",
        builtin_proxy: bool = True,
        builtin_direct: bool = True,
        proxy_hosts: Optional[object] = None,
        direct_hosts: Optional[object] = None,
        is_private: Callable[[str], bool] = is_private_address,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.requested_mode = (mode or MODE_AUTO).strip().lower()
        self.mode = resolve_mode(mode, route_name)
        self.is_private = is_private
        self._on_log = on_log
        self.proxy_hosts: List[str] = split_list(proxy_hosts)
        self.direct_hosts: List[str] = split_list(direct_hosts)
        self.builtin_proxy = list(BUILTIN_PROXY_SUFFIXES) if builtin_proxy else []
        self.builtin_direct = list(BUILTIN_DIRECT_SUFFIXES) if builtin_direct else []
        self._proxy_patterns: List[str] = list(self.builtin_proxy) + list(self.proxy_hosts)
        self._direct_patterns: List[str] = list(self.builtin_direct) + list(self.direct_hosts)
        self.hits_proxy = 0
        self.hits_direct = 0
        self.hits_private = 0

    # ------------------------------------------------------------------ 查询
    def _log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    @property
    def enabled(self) -> bool:
        """是否需要逐连接判断（全部走隧道 = 不需要分流逻辑）。"""
        return self.mode != MODE_OFF or bool(self.direct_hosts)

    def decide(self, host: str, port: int) -> str:
        """返回 ``"direct"``（本地直连）或 ``"tunnel"``（走隧道）。"""
        # 用户显式写的规则优先，便于覆盖内置名单
        if host_matches(host, self.direct_hosts):
            self.hits_direct += 1
            return DIRECT
        if host_matches(host, self.proxy_hosts):
            self.hits_proxy += 1
            return TUNNEL
        # 私有地址只能直连：远端中继根本到不了
        if self.is_private(host):
            self.hits_private += 1
            return DIRECT
        if self.mode in (MODE_OFF, MODE_ALL):
            return TUNNEL
        if host_matches(host, self._proxy_patterns):
            self.hits_proxy += 1
            return TUNNEL
        self.hits_direct += 1
        return DIRECT

    def describe(self) -> str:
        if self.mode == MODE_OFF:
            return "分流：关闭（全部走隧道）"
        if self.mode == MODE_ALL:
            return "分流：all（除私有地址外全部走隧道）"
        names = ("内置 %d 条" % len(self.builtin_proxy)) if self.builtin_proxy else "内置关闭"
        extra = ("+ 自定义 %d 条" % len(self.proxy_hosts)) if self.proxy_hosts else ""
        return "分流：smart（名单命中走隧道：%s%s）" % (names, extra)

    def stats(self) -> dict:
        return {
            "mode": self.mode,
            "requested": self.requested_mode,
            "proxy_hosts": len(self.proxy_hosts) + len(self.builtin_proxy),
            "direct_hosts": len(self.direct_hosts) + len(self.builtin_direct),
            "hits_tunnel": self.hits_proxy,
            "hits_direct": self.hits_direct,
            "hits_private": self.hits_private,
        }

    def match_proxy(self, hosts: Iterable[str]) -> List[str]:
        """筛出会被判定为「走隧道」的目标（供自检/文档核对用）。"""
        return [host for host in hosts if host_matches(host, self._proxy_patterns)]
