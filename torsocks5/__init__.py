"""TorSOCKS5 —— 跨平台 SOCKS5 本地代理，支持 Tor+meek / Cloudflare Worker / 自建中继三种路由。

组成：
    * socks5   —— 完整实现的 SOCKS5 服务端（RFC 1928 / RFC 1929）
    * tor      —— tor 进程与控制端口管理
    * meek     —— 纯 Python 实现的 meek 客户端传输（可插拔传输插件）
    * bridges  —— 网桥（bridge）行解析、校验与导入
    * tunnel   —— TSU/1 隧道协议（WebSocket 多路复用）
    * routes   —— 三种流量路由方式
    * split    —— 智能分流
    * web      —— HTTP 管理面板（HTMX + Alpine.js）
"""

__version__ = "2.0.0"
__all__ = ["__version__"]
