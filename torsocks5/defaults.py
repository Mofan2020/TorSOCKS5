"""跨模块共享的默认策略值（叶子模块，不导入任何本项目其它模块）。

放在这里是为了避免环形导入：``config``、``tunnel.protocol``、``split`` 都要用到
这几个默认名单，而 ``torsocks5.tunnel`` 包的 ``__init__`` 会拉起隧道客户端。
"""

from __future__ import annotations

from typing import Tuple

#: 默认放行的目标端口：443/80（HTTP(S)）、22（git over SSH）、9418（git 协议）
DEFAULT_ALLOW_PORTS: Tuple[int, ...] = (443, 80, 22, 9418)

#: 中继默认白名单（学习/开发基础设施）。
#: 路由方式 2（Cloudflare Worker）默认只放行这些目标，避免把免费额度变成通用代理；
#: 也符合项目「建议只用于辅助访问 GitHub / HuggingFace / Docker Hub 等学习站点」的定位。
LEARNING_ALLOW_HOSTS: Tuple[str, ...] = (
    # GitHub 及其静态资源
    "github.com",
    "githubusercontent.com",
    "githubassets.com",
    "github.io",
    "ghcr.io",
    "github.dev",
    # Hugging Face（含国内镜像）
    "huggingface.co",
    "hf.co",
    "hf-mirror.com",
    # 容器镜像
    "docker.io",
    "docker.com",
    "gcr.io",
    "quay.io",
    "k8s.io",
    "registry.k8s.io",
    "maven.org",
    "gradle.org",
    # 包管理器与语言生态
    "pythonhosted.org",
    "pypi.org",
    "npmjs.org",
    "npmjs.com",
    "yarnpkg.com",
    "jsdelivr.net",
    "unpkg.com",
    "crates.io",
    "rust-lang.org",
    "go.dev",
    "golang.org",
    "goproxy.io",
    "nodejs.org",
    "deno.land",
    "deno.com",
    "bun.sh",
    "anaconda.org",
    "anaconda.com",
    "pytorch.org",
    "conda-forge.org",
    # 系统与驱动源
    "ubuntu.com",
    "debian.org",
    "archlinux.org",
    "packages.microsoft.com",
    "nvidia.com",
    "download.nvidia.com",
    # 论文与数据集
    "arxiv.org",
    "openreview.net",
    "paperswithcode.com",
    "kaggle.com",
    "storage.googleapis.com",
    # 其它代码托管
    "gitlab.com",
    "bitbucket.org",
    "sourceforge.net",
    "kernel.org",
    "gnu.org",
    "googlesource.com",
    "chromium.org",
    "android.com",
    "jetbrains.com",
)
