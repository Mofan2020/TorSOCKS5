"""跨平台注册为后台服务：systemd（Linux）、launchd（macOS）、计划任务（Windows）。"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Tuple

from . import config as config_mod

LAUNCH_LABEL = "com.torsocks5.agent"


def _executable_command(port: int) -> Tuple[str, List[str]]:
    """返回用于启动服务的 (可执行文件, 参数列表)。"""
    if getattr(sys, "frozen", False):
        return sys.executable, ["run", "--port", str(port)]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    entry = os.path.join(root, "torsocks5_cli.py")
    if os.path.exists(entry):
        return sys.executable, [entry, "run", "--port", str(port)]
    return sys.executable, ["-m", "torsocks5", "run", "--port", str(port)]


def _env_lines() -> List[str]:
    return [
        "Environment=PYTHONUNBUFFERED=1",
        "Environment=NO_COLOR=1",
    ]


# --------------------------------------------------------------------- systemd
def systemd_unit(port: int) -> str:
    exe, args = _executable_command(port)
    exec_line = " ".join([exe] + args)
    return "\n".join(
        [
            "[Unit]",
            "Description=TorSOCKS5 (SOCKS5 proxy over Tor via meek bridges)",
            "Documentation=https://github.com/Mofan2020/TorSOCKS5",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            "ExecStart=%s" % exec_line,
            "Restart=on-failure",
            "RestartSec=10",
            "WorkingDirectory=%s" % os.path.expanduser("~"),
        ]
        + _env_lines()
        + [
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def install_systemd(args, logger, apply: bool) -> int:
    unit = systemd_unit(args.port or int(config_mod.Config.load().get("proxy.port")))
    path = os.path.expanduser("~/.config/systemd/user/torsocks5.service")
    logger.plain("# 写入 %s" % path)
    logger.plain(unit)
    if not apply:
        logger.info("加上 --apply 才会真正写入。")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(unit)
    logger.ok("已写入 %s" % path)
    logger.info("启用: systemctl --user daemon-reload && systemctl --user enable --now torsocks5")
    logger.info("查看: journalctl --user -u torsocks5 -f")
    return 0


# --------------------------------------------------------------------- launchd
def launchd_plist(port: int) -> str:
    exe, args = _executable_command(port)
    arg_lines = "\n".join("        <string>%s</string>" % item for item in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCH_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe}</string>
{arg_lines}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{os.path.expanduser('~')}/Library/Logs/torsocks5.log</string>
    <key>StandardErrorPath</key>
    <string>{os.path.expanduser('~')}/Library/Logs/torsocks5.err.log</string>
</dict>
</plist>
"""


def install_launchd(args, logger, apply: bool) -> int:
    port = args.port or int(config_mod.Config.load().get("proxy.port"))
    plist = launchd_plist(port)
    path = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LAUNCH_LABEL)
    logger.plain("# 写入 %s" % path)
    logger.plain(plist)
    if not apply:
        logger.info("加上 --apply 才会真正写入。")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(plist)
    logger.ok("已写入 %s" % path)
    logger.info("加载: launchctl load -w %s" % path)
    logger.info("查看日志: tail -f ~/Library/Logs/torsocks5.log")
    return 0


# --------------------------------------------------------------------- Windows
def install_schtasks(args, logger, apply: bool) -> int:
    port = args.port or int(config_mod.Config.load().get("proxy.port"))
    exe, cmd_args = _executable_command(port)
    command = subprocess.list2cmdline([exe] + cmd_args)
    task_command = [
        "schtasks", "/Create", "/F",
        "/SC", "ONLOGON",
        "/TN", "TorSOCKS5",
        "/TR", command,
    ]
    logger.plain("# 执行 %s" % " ".join(task_command))
    if not apply:
        logger.info("加上 --apply 才会真正创建计划任务。")
        logger.info("如需开机即启动且不依赖登录，可把 /SC ONLOGON 换成 /SC ONSTART 并以管理员运行。")
        return 0
    try:
        out = subprocess.run(task_command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("创建计划任务失败: %s" % exc)
        return 1
    if out.returncode != 0:
        logger.error("schtasks 返回 %d: %s" % (out.returncode, out.stderr.strip() or out.stdout.strip()))
        return 1
    logger.ok("已创建计划任务 TorSOCKS5（用户登录时启动）")
    logger.info("立即运行: schtasks /Run /TN TorSOCKS5")
    logger.info("删除: schtasks /Delete /TN TorSOCKS5 /F")
    return 0


def detect_kind() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if os.name == "nt":
        return "schtasks"
    return "systemd"


def install_service(args, logger) -> int:
    kind = args.kind
    if kind == "auto":
        kind = detect_kind()
        logger.info("自动选择: %s" % kind)
    if kind == "print":
        port = args.port or int(config_mod.Config.load().get("proxy.port"))
        logger.plain(systemd_unit(port) if sys.platform != "darwin" else launchd_plist(port))
        return 0
    handler = {
        "systemd": install_systemd,
        "launchd": install_launchd,
        "schtasks": install_schtasks,
    }[kind]
    return handler(args, logger, args.apply)
