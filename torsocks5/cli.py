"""命令行入口。

    torsocks5 run            启动 tor（经 meek 网桥）+ SOCKS5 代理
    torsocks5 doctor         环境自检
    torsocks5 bridges ...    网桥管理
    torsocks5 config ...     配置管理
    torsocks5 fetch-tor      下载 tor 官方专家包
    torsocks5 selftest       离线自检（SOCKS5 协议 + meek 隧道）
    torsocks5 install-service  生成后台服务配置
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

from . import __version__
from . import bridges as bridges_mod
from . import config as config_mod
from . import log as log_mod
from .socks5.server import SocksServer
from .tor import find as tor_find
from .tor.manager import TorProcess, TorSupervisor, port_available

PROGRESS_WIDTH = 28


def _force_utf8_output() -> None:
    """把标准输出/错误切到 UTF-8。

    Windows 控制台默认使用本地代码页（cp936 / cp1252），直接输出中文与非
    ASCII 符号会抛 UnicodeEncodeError，``--help`` 甚至会因此崩溃。
    必须在构造 argparse 解析器之前调用（argparse 会在 parse_args 时打印帮助）。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):  # 已被重定向到不支持的对象
            pass


_force_utf8_output()


# --------------------------------------------------------------------- 工具
def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _parse_addr(text: str, default_port: int = 0) -> Tuple[str, int]:
    text = (text or "").strip()
    if not text:
        return ("127.0.0.1", default_port)
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = int(rest.lstrip(":") or default_port)
        return host, port
    if ":" in text:
        host, _, raw_port = text.rpartition(":")
        return host, int(raw_port or default_port)
    return text, default_port


def _addr(value) -> Tuple[str, int]:
    """把 ``getsockname()`` 之类的结果规整成 ``(host, port)``。"""
    host, port = value[0], value[1]
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    return str(host), int(port)


def load_bridges(config: config_mod.Config, extra: Optional[List[str]] = None) -> bridges_mod.BridgeStore:
    store = bridges_mod.BridgeStore(config.bridges_path)
    store.load(include_builtin=_bool(config.get("bridges.builtin")))
    for line in extra or []:
        for item in bridges_mod.iter_lines([line]):
            item = item.strip()
            if not item or item.startswith("#"):
                continue
            try:
                store.add(bridges_mod.parse_bridge_line(item))
            except bridges_mod.BridgeError as exc:
                raise SystemExit("网桥行无效: %s -> %s" % (item, exc)) from exc
    return store


# --------------------------------------------------------------------- run
def cmd_run(args: argparse.Namespace, logger: log_mod.Logger) -> int:
    config = config_mod.Config.load(args.config)
    listen = args.listen or config.get("proxy.listen")
    port = int(args.port or config.get("proxy.port"))
    username = config.get("proxy.username") or None
    password = config.get("proxy.password") or ""
    upstream_override = _parse_addr(args.upstream) if args.upstream else None

    store = load_bridges(config, args.bridge)
    use_bridges = not (args.no_bridge or _bool(config.get("tor.direct")))
    bridge_lines = store.torrc_lines() if use_bridges else []
    if use_bridges and not bridge_lines:
        logger.error("没有可用的网桥。请先添加：torsocks5 bridges add \"Bridge meek 0.0.2.0:3 url=... front=...\"")
        logger.error("或从 https://bridges.torproject.org 获取 meek 网桥行。")
        return 2

    if not port_available(port, listen if ":" not in listen else listen):
        logger.error("端口 %d 已被占用，请用 --port 指定其它端口" % port)
        return 2

    socks_server: Optional[SocksServer] = None
    supervisor: Optional[TorSupervisor] = None
    tor: Optional[TorProcess] = None

    if upstream_override is None:
        def factory() -> TorProcess:
            cfg = config_mod.Config(dict(config.data), config.path)
            if args.tor:
                cfg.data.setdefault("tor", {})["binary"] = args.tor
            if args.no_bridge:
                cfg.data.setdefault("tor", {})["direct"] = True
            return TorProcess(
                cfg,
                bridge_lines,
                on_log=lambda line: logger.debug(_clean_tor_line(line)),
                on_progress=make_progress_printer(logger),
                on_state=lambda state: logger.debug("tor 状态: %s" % state),
            )

        tor = factory()
        supervisor = TorSupervisor(factory, restart=_bool(config.get("tor.restart")),
                                   on_log=logger.warn)
        log_mod.banner(logger, "TorSOCKS5 %s —— 正在通过 meek 网桥连接 Tor" % __version__)
        try:
            tor = supervisor.start()
        except Exception as exc:  # noqa: BLE001
            logger.error("启动 tor 失败: %s" % exc)
            return 3
        logger.info("tor: %s" % tor.summary_text())
        logger.info("torrc: %s" % tor.torrc_path)
        if bridge_lines:
            logger.info("网桥: %s" % ("; ".join(bridge_lines[:3]) + ("…" if len(bridge_lines) > 3 else "")))
        timeout = args.ready_timeout if args.ready_timeout is not None else 300
        if timeout > 0:
            logger.info("等待 Tor 引导完成（最多 %d 秒，meek 网桥通常需要 20~90 秒）…" % timeout)
            if not tor.wait_for_ready(timeout):
                percent, summary = tor.bootstrap_status()
                logger.error("Tor 引导未完成（%d%% %s）。" % (percent, summary))
                logger.error("常见原因：网桥失效 / CDN 前置域名被封 / 需要换一条网桥。")
                logger.error("查看详细日志: %s" % tor.tor_log_path)
                if args.keep_going:
                    logger.warn("按 --keep-going 继续以提供代理（多数请求会失败）")
                else:
                    supervisor.stop()
                    return 4
        upstream = tor.socks_address
    else:
        upstream = upstream_override
        logger.info("使用已有的 tor SOCKS5 端口: %s:%d" % upstream)

    socks_server = SocksServer(
        upstream=upstream,
        host=listen,
        port=port,
        username=username,
        password=password,
        allow_from=config.get("proxy.allow_from"),
        max_connections=int(config.get("proxy.max_connections")),
        idle_timeout=float(config.get("proxy.idle_timeout")),
        connect_timeout=float(config.get("proxy.connect_timeout")),
        udp_associate=_bool(config.get("proxy.udp_associate")),
        verbose=_bool(config.get("proxy.verbose")) or args.verbose,
        on_log=logger.info if (args.verbose or _bool(config.get("proxy.verbose"))) else None,
    )
    bound = socks_server.bind()
    threading.Thread(target=socks_server.serve_forever, name="socks5", daemon=True).start()

    show_ready_banner(logger, bound, username, socks_server)

    stop_event = threading.Event()

    def shutdown(_signum=None, _frame=None) -> None:
        stop_event.set()

    import signal as signal_mod

    for sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
        try:
            signal_mod.signal(sig, shutdown)
        except (ValueError, OSError):
            pass
    try:
        while not stop_event.is_set():
            stop_event.wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("正在关闭…")
        socks_server.shutdown()
        if supervisor is not None:
            supervisor.stop()
    logger.ok("已退出。")
    return 0


def _clean_tor_line(line: str) -> str:
    """去掉 tor 日志里的时间与级别前缀。"""
    parts = line.split("] ", 1)
    return parts[1] if len(parts) == 2 else line


def make_progress_printer(logger: log_mod.Logger):
    tty = sys.stderr.isatty()

    def printer(percent: int, tag: str, summary: str) -> None:
        if percent >= 100:
            logger.ok("Tor 引导完成：%s" % (summary or "Done"))
            return
        if not tty:
            logger.info("引导 %d%% %s %s" % (percent, tag, summary))
            return
        filled = int(PROGRESS_WIDTH * percent / 100)
        bar = "#" * filled + "-" * (PROGRESS_WIDTH - filled)
        sys.stderr.write("\r  [%s] %3d%% %-22s %s\033[K" % (bar, percent, tag, summary[:40]))
        sys.stderr.flush()

    return printer


def show_ready_banner(logger: log_mod.Logger, address: Tuple[str, int],
                      username: Optional[str], server: SocksServer) -> None:
    host, port = address
    display = "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)
    log_mod.banner(logger, "代理已就绪")
    logger.ok("SOCKS5 地址: %s" % display)
    if username:
        logger.info("认证: %s / ******" % username)
    logger.info("示例: curl -x socks5h://%s%s https://ifconfig.me" % (
        ("%s@" % username) if username else "", display))
    logger.info("      浏览器: 把代理设为 SOCKS v5 %s %d" % (host, port))
    logger.info("      远程主机解析请用 socks5h://，避免本地 DNS 泄漏")
    logger.info("停止服务: Ctrl+C")


# --------------------------------------------------------------------- doctor
def cmd_doctor(args: argparse.Namespace, logger: log_mod.Logger) -> int:
    config = config_mod.Config.load(args.config)
    problems = 0
    log_mod.banner(logger, "TorSOCKS5 %s 环境自检" % __version__)

    logger.info("Python: %s (%s)" % (platform.python_version(), sys.executable))
    if sys.version_info < (3, 8):  # noqa: UP036 - 运行时兜底检查
        logger.error("需要 Python 3.8 或更高版本")
        problems += 1
    logger.ok("操作系统: %s %s (%s)" % (platform.system(), platform.release(), platform.machine()))

    logger.plain("\n[1/6] 查找 tor")
    binary = tor_find.find_tor(str(config.get("tor.binary") or ""))
    if binary:
        version = tor_find.tor_version(binary)
        logger.ok("找到 tor: %s (%s)" % (binary, ".".join(str(v) for v in version) if version else "版本未知"))
        if version and version < (0, 4, 0):
            logger.warn("tor 版本偏旧（%s），建议 0.4.8 以上" % ".".join(str(v) for v in version))
        data = open(binary, "rb").read(4 * 1024 * 1024)
        if b"meek" in data:
            logger.ok("该 tor 内置了 meek 传输（可用 tor.meek_mode = \"builtin\"）")
        else:
            logger.info("该 tor 没有内置 meek，将使用本项目的 Python 传输插件（正常现象）")
    else:
        logger.error("找不到 tor 可执行文件")
        for source in tor_find.describe_sources(str(config.get("tor.binary") or "")):
            logger.info("  候选: %s" % source)
        logger.info("  安装: macOS `brew install tor` / Debian `sudo apt install tor` /"
                    " Windows 安装 Tor Expert Bundle，或 torsocks5 fetch-tor")
        problems += 1

    logger.plain("\n[2/6] 插件启动路径（tor 不支持带空格的路径）")
    tor = TorProcess(config, [])
    try:
        command = tor.plugin_command()
        logger.ok("插件命令: %s" % " ".join(command))
        for token in command:
            if " " in token:
                logger.error("命令中仍有空格: %s" % token)
                problems += 1
    except Exception as exc:  # noqa: BLE001
        logger.error("生成插件命令失败: %s" % exc)
        problems += 1

    logger.plain("\n[3/6] 网桥配置 (%s)" % config.bridges_path)
    try:
        store = load_bridges(config)
    except SystemExit as exc:
        logger.error(str(exc))
        problems += 1
        store = bridges_mod.BridgeStore(config.bridges_path)
        store.load()
    if store.active():
        for bridge in store.active():
            logger.ok("%s" % bridge.to_torrc())
    else:
        logger.error("没有启用任何网桥")
        problems += 1
    meek_bridges = [b for b in store.active() if b.is_meek]
    if meek_bridges and not store.active():
        logger.warn("有 meek 网桥但未启用")

    logger.plain("\n[4/6] 代理端口")
    port = int(args.port or config.get("proxy.port"))
    if port_available(port, "127.0.0.1"):
        logger.ok("端口 %d 可用" % port)
    else:
        logger.error("端口 %d 已被占用" % port)
        problems += 1

    logger.plain("\n[5/6] 到 CDN 前置域名的连通性（可选）")
    target = "ajax.aspnetcdn.com"
    if meek_bridges:
        front = meek_bridges[0].args.get("front")
        if front:
            target = front
    reachable, detail = _probe_tcp(target, 443)
    if reachable:
        logger.ok("能连接 %s:443（%s）" % (target, detail))
    else:
        logger.warn("无法连接 %s:443（%s）" % (target, detail))
        logger.info("如果你在受审查网络里，这可能正是需要 meek 网桥的原因；"
                    "若已能连通则说明当前网络并不封锁 Tor。")

    logger.plain("\n[6/6] 本地回环自检")
    ok, detail = _local_selfcheck()
    if ok:
        logger.ok(detail)
    else:
        logger.error(detail)
        problems += 1

    logger.plain("")
    if problems:
        logger.error("发现 %d 个问题，请按上面的提示处理。" % problems)
        return 1
    logger.ok("全部检查通过，可以运行: torsocks5 run")
    return 0


def _probe_tcp(host: str, port: int, timeout: float = 5.0) -> Tuple[bool, str]:
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return False, "DNS 解析失败: %s" % exc
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "TCP 握手成功"
    except OSError as exc:
        return False, str(exc)


def _local_selfcheck() -> Tuple[bool, str]:
    """起一个「直连」上游 SOCKS5 + 我们的代理，跑一次真实请求。"""
    from .selftest import run_selfcheck

    return run_selfcheck()


# --------------------------------------------------------------------- bridges
def cmd_bridges(args: argparse.Namespace, logger: log_mod.Logger) -> int:
    config = config_mod.Config.load(args.config)
    store = bridges_mod.BridgeStore(config.bridges_path)
    store.load(include_builtin=False)
    action = args.action

    if action == "list":
        if not store.bridges:
            logger.info("还没有配置任何网桥。")
            logger.info("从 https://bridges.torproject.org 选择 Meek，复制网桥行后执行：")
            logger.info('  torsocks5 bridges add "Bridge meek 0.0.2.0:3 url=... front=..."')
            return 0
        for index, bridge in enumerate(store.bridges, 1):
            flag = "" if bridge.enabled else "（已停用）"
            logger.plain("%2d. %s %s" % (index, bridge.to_torrc(), flag))
        logger.plain("")
        logger.info("共 %d 条，其中启用 %d 条；文件: %s" % (
            len(store.bridges), len(store.active()), store.path))
        return 0

    if action == "add":
        if not args.line:
            logger.error("请提供网桥行，例如：torsocks5 bridges add \"Bridge meek ...\"")
            return 2
        added = 0
        errors: List[str] = []
        for line in bridges_mod.iter_lines(args.line):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not line.lower().startswith("bridge "):
                line = "Bridge " + line
            try:
                bridge = bridges_mod.parse_bridge_line(line)
            except bridges_mod.BridgeError as exc:
                errors.append("%s -> %s" % (line, exc))
                continue
            if store.add(bridge):
                added += 1
            else:
                logger.info("已存在（重新启用）: %s" % bridge.to_torrc())
        store.save()
        for message in errors:
            logger.error("跳过: %s" % message)
        logger.ok("新增 %d 条网桥 -> %s" % (added, store.path))
        return 0 if added or not errors else 1

    if action in ("rm", "remove"):
        removed = store.remove(args.needle)
        store.save()
        logger.ok("已删除 %d 条网桥" % removed if removed else "没有匹配的网桥")
        return 0

    if action == "import":
        text = ""
        if args.file:
            with open(args.file, encoding="utf-8") as handle:
                text = handle.read()
        elif args.url:
            try:
                with urllib.request.urlopen(args.url, timeout=20) as response:
                    text = response.read().decode("utf-8", "replace")
            except (urllib.error.URLError, OSError) as exc:
                logger.error("下载失败: %s" % exc)
                return 1
        else:
            text = bridges_mod.read_clipboard()
            if not text:
                logger.error("剪贴板为空，也没有指定 --file/--url")
                return 2
        normalized, errors = bridges_mod.normalize(text)
        for message in errors:
            logger.warn("跳过: %s" % message)
        count = 0
        for line in normalized.splitlines():
            try:
                if store.add(bridges_mod.parse_bridge_line(line)):
                    count += 1
            except bridges_mod.BridgeError as exc:
                logger.warn("跳过 %s: %s" % (line, exc))
        store.save()
        logger.ok("导入 %d 条网桥 -> %s" % (count, store.path))
        return 0

    if action == "clipboard":
        text = bridges_mod.read_clipboard()
        if not text:
            logger.error("读取剪贴板失败（需要 pbpaste / wl-paste / xclip）")
            return 1
        sys.stdout.write(text)
        return 0

    if action == "normalize":
        text = "\n".join(args.line or [])
        normalized, errors = bridges_mod.normalize(text)
        for message in errors:
            logger.error("跳过: %s" % message)
        sys.stdout.write(normalized + "\n")
        return 0 if normalized else 1

    if action == "test":
        return _bridges_test(args, logger, config)

    logger.error("未知的子命令: %s" % action)
    return 2


def _bridges_test(args: argparse.Namespace, logger: log_mod.Logger,
                  config: config_mod.Config) -> int:
    """真实测试：启动 tor 并通过指定网桥引导。"""
    store = load_bridges(config, args.line or [])
    if not store.active():
        logger.error("没有可测试的网桥")
        return 2
    lines = store.torrc_lines()
    tor = TorProcess(config, lines, on_log=lambda line: logger.debug(_clean_tor_line(line)),
                     on_progress=make_progress_printer(logger))
    try:
        tor.start()
    except Exception as exc:  # noqa: BLE001
        logger.error("启动 tor 失败: %s" % exc)
        return 3
    logger.info("测试 %d 条网桥，等待引导（最多 %d 秒）…" % (len(lines), args.timeout))
    ok = tor.wait_for_ready(args.timeout)
    percent, summary = tor.bootstrap_status()
    if ok:
        logger.ok("引导成功：%s" % (summary or "Done"))
        tor.stop()
        return 0
    logger.error("引导失败：%d%% %s" % (percent, summary))
    logger.error("tor 日志: %s" % tor.tor_log_path)
    tor.stop()
    return 1


# --------------------------------------------------------------------- config
def cmd_config(args: argparse.Namespace, logger: log_mod.Logger) -> int:
    config = config_mod.Config.load(args.config)
    if args.action == "path":
        sys.stdout.write(config_mod.default_config_path() + "\n")
        return 0
    if args.action == "show":
        data = config.as_dict()
        if args.format == "json":
            sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        else:
            for section, values in data.items():
                if isinstance(values, dict):
                    logger.plain("[%s]" % section)
                    for key, value in values.items():
                        logger.plain("  %s = %s" % (key, _fmt_value(value)))
        return 0
    if args.action == "init":
        path = config.path or config_mod.default_config_path()
        if os.path.exists(path) and not args.force:
            logger.error("配置文件已存在: %s（用 --force 覆盖）" % path)
            return 1
        os.makedirs(os.path.dirname(path), exist_ok=True)
        template = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.toml")
        if os.path.exists(template):
            shutil.copyfile(template, path)
        else:  # pragma: no cover
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(CONFIG_TEMPLATE)
        logger.ok("已生成配置: %s" % path)
        return 0
    logger.error("未知的子命令: %s" % args.action)
    return 2


def _fmt_value(value) -> str:
    if isinstance(value, (list, tuple)):
        return "[%s]" % ", ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --------------------------------------------------------------------- fetch-tor
TOR_MIRRORS = [
    "https://dist.torproject.org",
    "https://ftp.torproject.org",
]


def _bundle_names(version: str) -> List[str]:
    system = platform.system()
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if system == "Windows":
        return ["tor-expert-bundle-windows-x86_64-%s.tar.gz" % version]
    if system == "Darwin":
        arch = "aarch64" if arm else "x86_64"
        return [
            "tor-expert-bundle-macos-%s-%s.tar.gz" % (arch, version),
            "tor-expert-bundle-macos-x86_64-%s.tar.gz" % version,
        ]
    arch = "aarch64" if arm else "x86_64"
    return [
        "tor-expert-bundle-linux-%s-%s.tar.gz" % (arch, version),
        "tor-expert-bundle-linux-x86_64-%s.tar.gz" % version,
    ]


def cmd_fetch_tor(args: argparse.Namespace, logger: log_mod.Logger) -> int:
    version = args.version
    target_dir = args.dest or os.path.join(config_mod.default_data_dir(), "tor")
    os.makedirs(target_dir, exist_ok=True)
    mirrors = [args.mirror] if args.mirror else []
    mirrors += [m for m in TOR_MIRRORS if m not in mirrors]

    if args.file:
        return _extract_tor_bundle(args.file, target_dir, logger)

    errors: List[str] = []
    for mirror in mirrors:
        for name in _bundle_names(version):
            url = "%s/tor-%s/%s" % (mirror.rstrip("/"), version, name)
            logger.info("尝试下载 %s" % url)
            try:
                path = _download(url, logger)
            except Exception as exc:  # noqa: BLE001
                errors.append("%s -> %s" % (url, exc))
                continue
            return _extract_tor_bundle(path, target_dir, logger)
    logger.error("全部下载地址都失败了：")
    for message in errors:
        logger.error("  %s" % message)
    logger.info("可以手动下载 Tor Expert Bundle 并用 --file 指定：")
    logger.info("  torsocks5 fetch-tor --file ~/Downloads/tor-expert-bundle-....tar.gz")
    logger.info("或者直接在配置里指定已有的 tor：tor.binary = \"/path/to/tor\"")
    return 1


def _download(url: str, logger: log_mod.Logger) -> str:
    import ssl

    context = ssl.create_default_context()
    request = urllib.request.Request(url, headers={"User-Agent": "TorSOCKS5/%s" % __version__})
    with urllib.request.urlopen(request, timeout=60, context=context) as response:
        total = int(response.headers.get("Content-Length") or 0)
        name = os.path.basename(url)
        path = os.path.join(config_mod.default_data_dir(), name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        done = 0
        last_report = 0.0
        with open(path, "wb") as handle:
            while True:
                chunk = response.read(262144)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last_report > 1.0:
                    last_report = now
                    if total:
                        logger.info("  已下载 %.1f%% (%.1f/%.1f MB)" % (
                            100.0 * done / total, done / 1e6, total / 1e6))
                    else:
                        logger.info("  已下载 %.1f MB" % (done / 1e6))
    return path


def _extract_tor_bundle(archive: str, target_dir: str, logger: log_mod.Logger) -> int:
    logger.info("解压 %s -> %s" % (archive, target_dir))
    try:
        with tarfile.open(archive, "r:*") as tar:
            members = [m for m in tar.getmembers() if not m.name.startswith(("/", ".."))]
            for member in members:
                member.uid = member.gid = 0
                if member.name.endswith("/tor") or member.name.lower().endswith("/tor.exe"):
                    member.mode = 0o755
            tar.extractall(target_dir, members=members)
    except (tarfile.TarError, OSError) as exc:
        logger.error("解压失败: %s" % exc)
        return 1
    found = None
    for root, _dirs, files in os.walk(target_dir):
        for name in files:
            if name == "tor" or name.lower() == "tor.exe":
                found = os.path.join(root, name)
    if not found:
        logger.error("压缩包里没有找到 tor 可执行文件")
        return 1
    logger.ok("tor 已就绪: %s" % found)
    logger.info("把它写进配置：")
    logger.info('  [tor]')
    logger.info('  binary = "%s"' % found.replace("\\", "\\\\"))
    return 0


# --------------------------------------------------------------------- selftest
def cmd_selftest(args: argparse.Namespace, logger: log_mod.Logger) -> int:
    from .selftest import run_full_selftest

    return run_full_selftest(logger)


# --------------------------------------------------------------------- service
def cmd_install_service(args: argparse.Namespace, logger: log_mod.Logger) -> int:
    from .service import install_service

    return install_service(args, logger)


# --------------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="torsocks5",
        description="通过 meek 网桥连接 Tor 网络的 SOCKS5 代理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
        "  torsocks5 run --port 9051\n"
        "  torsocks5 bridges add \"Bridge meek 0.0.2.0:3 url=https://... front=...\"\n"
        "  torsocks5 doctor\n",
    )
    parser.add_argument("--config", default="", help="配置文件路径")
    parser.add_argument("--log-file", default="", help="把日志同时写入文件")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出详细日志")
    parser.add_argument("-V", "--version", action="version", version="TorSOCKS5 %s" % __version__)
    parser.add_argument("--transport-plugin", action="store_true",
                        help=argparse.SUPPRESS)  # 内部使用：作为 meek 传输插件运行
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="启动代理服务")
    run.add_argument("--listen", default="", help="监听地址，默认 127.0.0.1")
    run.add_argument("--port", type=int, default=0, help="监听端口，默认 9051")
    run.add_argument("--tor", default="", help="指定 tor 可执行文件")
    run.add_argument("--upstream", default="", help="复用已有的 tor SOCKS5 端口，如 127.0.0.1:9050")
    run.add_argument("--no-bridge", action="store_true", help="不使用网桥，直接连接 Tor")
    run.add_argument("--bridge", action="append", default=[], help="临时追加一条网桥行（可重复）")
    run.add_argument("--ready-timeout", type=int, default=300,
                        help="等待 Tor 引导完成的秒数（meek 首次引导可能需要几分钟），0 表示不等待")
    run.add_argument("--keep-going", action="store_true", help="引导失败也继续提供服务")
    run.set_defaults(func=cmd_run)

    doctor = sub.add_parser("doctor", help="环境自检")
    doctor.add_argument("--port", type=int, default=0, help="要检查的代理端口")
    doctor.set_defaults(func=cmd_doctor)

    bridges = sub.add_parser("bridges", help="网桥管理")
    bridges.add_argument("action", choices=["list", "add", "rm", "import", "clipboard", "normalize", "test"])
    bridges.add_argument("line", nargs="*", help="网桥行（add/normalize/test 可用）")
    bridges.add_argument("--file", default="", help="从文件导入")
    bridges.add_argument("--url", default="", help="从 URL 导入")
    bridges.add_argument("--timeout", type=int, default=120, help="test 的等待秒数")
    bridges.set_defaults(func=cmd_bridges)

    config_parser = sub.add_parser("config", help="配置管理")
    config_parser.add_argument("action", choices=["show", "init", "path"])
    config_parser.add_argument("--force", action="store_true", help="覆盖已存在的配置")
    config_parser.add_argument("--format", choices=["text", "json"], default="text")
    config_parser.set_defaults(func=cmd_config)

    fetch = sub.add_parser("fetch-tor", help="下载并解压 tor 官方专家包")
    fetch.add_argument("--version", default="0.4.8.12", help="tor 版本")
    fetch.add_argument("--dest", default="", help="解压目录")
    fetch.add_argument("--mirror", default="", help="镜像站地址")
    fetch.add_argument("--file", default="", help="使用本地已下载的压缩包")
    fetch.set_defaults(func=cmd_fetch_tor)

    selftest = sub.add_parser("selftest", help="离线自检")
    selftest.set_defaults(func=cmd_selftest)

    service = sub.add_parser("install-service", help="生成/安装后台服务")
    service.add_argument("kind", nargs="?", default="auto",
                         choices=["auto", "systemd", "launchd", "schtasks", "print"])
    service.add_argument("--apply", action="store_true", help="真正写入系统（默认只打印）")
    service.add_argument("--port", type=int, default=0)
    service.set_defaults(func=cmd_install_service)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    # 作为 tor 的 meek 传输插件被启动时，走 PT 协议而不是命令行界面。
    # 冻结（PyInstaller）与源码运行都要支持，因为 torrc 里用的命令形式不同。
    if "--transport-plugin" in (sys.argv[1:] if argv is None else argv):
        from .meek.pt import main as pt_main

        args_list = list(sys.argv[1:] if argv is None else argv)
        return pt_main([item for item in args_list if item != "--transport-plugin"])

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args((argv or []) + ["run"])
    logger = log_mod.Logger(
        level="debug" if args.verbose else "info",
        log_file=args.log_file or None,
    )
    try:
        return int(args.func(args, logger) or 0)
    except KeyboardInterrupt:
        logger.plain("")
        return 130
    except SystemExit as exc:
        if isinstance(exc.code, str):
            logger.error(exc.code)
            return 2
        return int(exc.code or 0)
    finally:
        logger.close()


CONFIG_TEMPLATE = """# TorSOCKS5 配置示例
[proxy]
listen = "127.0.0.1"
port = 9051
# username 非空则启用 RFC 1929 认证
username = ""
password = ""
allow_from = ["127.0.0.1", "::1"]
udp_associate = true

[tor]
# binary = ""            # 留空自动探测
# direct = false         # true = 不用网桥直连 Tor
# log_level = "notice"
# extra_options = ["TorCircuitBuildTimeout 60"]

[meek]
# 传输插件行为，一般不用改
methods = ["meek", "meek_lite", "meek_azure"]
"""


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
