# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：生成跨平台的单文件可执行程序。

用法（在项目根目录执行）：

    pip install pyinstaller
    pyinstaller torsocks5.spec

产物位于 ``dist/TorSOCKS5-<平台>``，可用 ``--transport-plugin`` 参数作为
tor 的 meek 传输插件运行（见 torsocks5/tor/manager.py）。
"""

import os
import sys

block_cipher = None

ROOT = os.path.abspath(os.getcwd())

# 必须一起打包的数据文件
datas = [
    (os.path.join(ROOT, "torsocks5", "config.example.toml"), "torsocks5"),
    (os.path.join(ROOT, "LICENSE"), "."),
    (os.path.join(ROOT, "README.md"), "."),
    # 随包附带协议规范与三种路由的说明，方便离线查阅
    (os.path.join(ROOT, "docs", "tunnel-protocol.md"), "docs"),
    (os.path.join(ROOT, "docs", "routes.md"), "docs"),
    # Cloudflare Worker 与 Deno 中继的部署物（部署时直接用得上）
    (os.path.join(ROOT, "deploy"), "deploy"),
]

hiddenimports = [
    "torsocks5.meek.socks_server",
    "torsocks5.meek.channel",
    "torsocks5.meek.pt",
    "torsocks5.socks5.client",
    "torsocks5.socks5.server",
    "torsocks5.tor.manager",
    "torsocks5.tor.control",
    "torsocks5.tor.find",
    "torsocks5.service",
    "torsocks5.selftest",
    # 隧道路由（路由 2 / 路由 3）与自建中继
    "torsocks5.defaults",
    "torsocks5.hostrules",
    "torsocks5.split",
    "torsocks5.routes",
    "torsocks5.routes.base",
    "torsocks5.routes.tor_meek",
    "torsocks5.routes.tunnel_base",
    "torsocks5.routes.cf_relay",
    "torsocks5.routes.self_relay",
    "torsocks5.routes.upstream",
    "torsocks5.tunnel",
    "torsocks5.tunnel.protocol",
    "torsocks5.tunnel.wsframe",
    "torsocks5.tunnel.wsclient",
    "torsocks5.tunnel.wsserver",
    "torsocks5.tunnel.stream",
    "torsocks5.tunnel.client",
    "torsocks5.tunnel.relay",
    "torsocks5.tunnel.probe",
]

a = Analysis(
    ["torsocks5_cli.py"],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除与本项目无关的重型模块，显著减小体积
        "tkinter",
        "unittest",
        "pydoc_data",
        "test",
        "lib2to3",
        "distutils",
        "setuptools",
        "pip",
        "numpy",
        "PIL",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="TorSOCKS5",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,  # macOS 构建时用 ARCHS 环境变量指定 arm64/x86_64
    codesign_identity=None,
    entitlements_file=None,
)
