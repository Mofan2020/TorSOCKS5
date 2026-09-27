"""TorSOCKS5 —— 通过 Meek 网桥接入 Tor 网络的 SOCKS5 代理。

组成：
    * socks5   —— 完整实现的 SOCKS5 服务端（RFC 1928 / RFC 1929）
    * tor      —— tor 进程与控制端口管理
    * meek     —— 纯 Python 实现的 meek 客户端传输（可插拔传输插件）
    * bridges  —— 网桥（bridge）行解析、校验与导入
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
