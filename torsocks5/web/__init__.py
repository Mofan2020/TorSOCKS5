"""Web 管理面板：纯标准库 HTTP 服务 + CDN 前端（零构建、零运行期依赖）。

页面：仪表盘 / 路由 / 网桥 / 配置 / 日志；认证用 Basic Auth（[web] 段配置）。
"""

from __future__ import annotations

from .logstream import LogStream
from .server import WebPanel

__all__ = ["WebPanel", "LogStream"]
