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
import secrets
import shutil
import socket
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

from . import __version__, hostrules
from . import bridges as bridges_mod
from . import config as config_mod
from . import log as log_mod
from . import routes as routes_mod
from .socks5.server import SocksServer
from .tor import find as tor_find
from .tor.manager import TorProcess, port_available

PROGRESS_WIDTH = log_mod.PROGRESS_WIDTH


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

    if not port_available(port, listen if ":" not in listen else listen):
        logger.error("端口 %d 已被占用，请用 --port 指定其它端口" % port)
        return 2

    # ---------------------------------------------------------------- 选路由
    route_name = (args.route or str(config.get("proxy.route") or routes_mod.DEFAULT_ROUTE))
    if upstream_override is not None:
        route_name = "upstream"  # 兼容旧行为：--upstream 就是「复用已有 SOCKS5」
    options = routes_mod.RouteOptions(
        bridge_lines=list(args.bridge or []),
        tor_binary=args.tor or "",
        direct=bool(args.no_bridge),
        ready_timeout=args.ready_timeout,
        keep_going=bool(args.keep_going),
        upstream=upstream_override,
        verbose=bool(args.verbose),
        relay_url=args.relay_url or "",
        relay_token=args.relay_token or "",
    )
    try:
        route = routes_mod.create_route(route_name, config, logger, options)
    except routes_mod.RouteError as exc:
        logger.error(str(exc))
        return exc.exit_code

    try:
        route.start()
    except routes_mod.RouteError as exc:
        logger.error(str(exc))
        route.stop()
        return exc.exit_code

    connector = route.connector()
    upstream = route.upstream_socks()
    if connector is None and upstream is None:
        logger.error("路由 %s 既没有提供上游 SOCKS5 也没有提供 connector" % route.name)
        route.stop()
        return 3

    socks_server = SocksServer(
        upstream=upstream,
        connector=connector,
        host=listen,
        port=port,
        username=username,
        password=password,
        allow_from=config.get("proxy.allow_from"),
        max_connections=int(config.get("proxy.max_connections")),
        idle_timeout=float(config.get("proxy.idle_timeout")),
        connect_timeout=float(config.get("proxy.connect_timeout")),
        udp_associate=_bool(config.get("proxy.udp_associate")) and route.supports_udp,
        verbose=_bool(config.get("proxy.verbose")) or args.verbose,
        on_log=logger.info if (args.verbose or _bool(config.get("proxy.verbose"))) else None,
    )
    bound = socks_server.bind()
    threading.Thread(target=socks_server.serve_forever, name="socks5", daemon=True).start()

    show_ready_banner(logger, bound, username, socks_server, route)

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
        route.stop()
    logger.ok("已退出。")
    return 0


def _clean_tor_line(line: str) -> str:
    """（保留的别名）去掉 tor 日志里的时间与级别前缀。"""
    return log_mod.clean_tor_line(line)


def make_progress_printer(logger: log_mod.Logger):
    """（保留的别名）tor 引导进度 → 日志。实现在 ``torsocks5.log``。"""
    return log_mod.progress_printer(logger)


def show_ready_banner(logger: log_mod.Logger, address: Tuple[str, int],
                      username: Optional[str], server: SocksServer, route=None) -> None:
    host, port = address
    display = "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)
    log_mod.banner(logger, "代理已就绪")
    logger.ok("SOCKS5 地址: %s" % display)
    if route is not None:
        logger.info("路由方式: %s（%s）" % (route.name, route.title))
        note = route.target_note()
        if note:
            logger.info(note)
        status = route.status()
        if status:
            logger.info("路由状态: %s" % status)
    if server.connector is not None:
        logger.info("UDP ASSOCIATE: 不支持（隧道路由只承载 TCP）")
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
    logger.info("提醒：meek 每 64KB 就要一次完整 HTTP 往返，速度慢是正常现象，"
                "首次引导可能需要 10~30 分钟。")

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


# --------------------------------------------------------------------- routes
def cmd_routes(args: argparse.Namespace, logger: log_mod.Logger) -> int:
    """列出三种流量路由方式，以及当前环境里各自缺什么。"""
    config = config_mod.Config.load(args.config)
    log_mod.banner(logger, "TorSOCKS5 %s —— 可选的流量路由方式" % __version__)
    current = str(config.get("proxy.route") or routes_mod.DEFAULT_ROUTE)
    for name, title, summary in routes_mod.describe_table():
        mark = "*" if name == current else " "
        logger.plain("%s %-11s %s" % (mark, name, title))
        logger.plain("  %s %s" % (" " * 11, summary))
    logger.plain("")
    logger.info("当前配置：route = %s（命令行可用 --route 覆盖）" % current)

    logger.plain("")
    logger.plain("就绪检查：")
    binary = tor_find.find_tor(str(config.get("tor.binary") or ""))
    if binary:
        store = bridges_mod.BridgeStore(config.bridges_path)
        try:
            store.load(include_builtin=True)
            bridge_count = len(store.active())
        except (bridges_mod.BridgeError, OSError):
            bridge_count = 0
        logger.plain("  tor-meek   : tor 已就绪（%s），启用中的网桥 %d 条" % (binary, bridge_count))
    else:
        logger.plain("  tor-meek   : 未找到 tor —— torsocks5 fetch-tor 或系统安装 tor")
    cf_url = str(config.get("cf_relay.url") or "")
    logger.plain("  cf-relay   : %s" % (("已配置 %s" % cf_url) if cf_url
                                       else "未配置 —— 先部署 deploy/cloudflare/，再把地址写进 [cf_relay]"))
    relay_url = str(config.get("self_relay.url") or "")
    logger.plain("  self-relay : %s" % (("已配置 %s" % relay_url) if relay_url
                                        else "未配置 —— 本机跑 torsocks5 relay serve 即可，见 docs/routes.md"))
    logger.plain("")
    logger.info("细节、实测速度与限制: docs/routes.md")
    return 0


# --------------------------------------------------------------------- relay
def cmd_relay(args: argparse.Namespace, logger: log_mod.Logger) -> int:
    """自建中继（路由方式 3 的服务端）。"""
    from .tunnel import RelayServer

    action = getattr(args, "action", "serve") or "serve"
    if action == "token":
        token = secrets.token_urlsafe(24)
        logger.plain(token)
        return 0
    if action != "serve":
        logger.error("未知的子命令: %s（可选 serve / token）" % action)
        return 2

    config = config_mod.Config.load(args.config)
    listen = args.listen or str(config.get("relay.listen"))
    port = int(args.port or config.get("relay.port"))
    token = args.token or str(config.get("relay.token") or "")
    allow_all = bool(args.allow_all) or config_mod.as_bool(config.get("relay.allow_all"))
    allow_hosts = list(config.get("relay.allow_hosts") or [])
    for extra in args.allow_host or []:
        allow_hosts.extend(hostrules.split_list(extra))
    allow_ports = [int(item) for item in (args.allow_port or config.get("relay.allow_ports") or [])]
    if args.max_streams:
        max_streams = int(args.max_streams)
    else:
        max_streams = int(config.get("relay.max_streams"))
    server = RelayServer(
        listen,
        port,
        token=token,
        allow_hosts=allow_hosts,
        allow_ports=allow_ports,
        allow_all=allow_all,
        allow_private=bool(args.allow_private),
        max_streams=max_streams,
        path=str(config.get("relay.path") or "/tsu"),
        idle_timeout=float(config.get("proxy.idle_timeout")),
        connect_timeout=float(config.get("proxy.connect_timeout")),
        tls_cert=args.tls_cert or "",
        tls_key=args.tls_key or "",
        on_log=logger.info,
        log_targets=bool(args.verbose),
    )
    bound = server.bind()
    host, real_port = bound[0], server.port
    display = "[%s]:%d" % (host, real_port) if ":" in str(host) else "%s:%d" % (host, real_port)
    log_mod.banner(logger, "TorSOCKS5 %s —— 自建中继已启动" % __version__)
    logger.ok("中继地址: ws://%s%s" % (display, server.path))
    logger.info("目标范围: %s" % ("任意地址（白名单已关闭）" if allow_all
                                  else "%d 条规则命中才放行" % len(allow_hosts)))
    logger.info("端口范围: %s" % (",".join(str(item) for item in allow_ports) if allow_ports else "任意"))
    logger.info("单连接并发上限: %d 条流" % max_streams)
    if args.allow_private:
        logger.warn("已开启 --allow-private：中继将允许连接私有/回环地址（仅供本机测试）")
    if not token:
        loopback = str(host).startswith("127.") or host in ("::1", "localhost")
        if loopback:
            logger.info("未设置令牌：只监听回环地址，本机自用没问题")
        else:
            logger.warn("⚠️ 监听 %s 且未设置令牌：任何能连到这个端口的人都能拿它当代理！" % display)
            logger.warn("   请加上 --token，或把 relay.token 写进配置。")
    logger.plain("")
    logger.plain("客户端配置：")
    logger.plain("  [self_relay]")
    logger.plain('  url = "ws://%s%s"' % (display, server.path))
    if token:
        logger.plain('  token = "%s"' % token)
    logger.plain("")
    logger.info("健康检查: curl http://%s/healthz" % display)
    logger.info("停止服务: Ctrl+C")

    stop_event = threading.Event()

    def shutdown(_signum=None, _frame=None) -> None:
        stop_event.set()

    import signal as signal_mod

    for sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
        try:
            signal_mod.signal(sig, shutdown)
        except (ValueError, OSError):
            pass
    thread = threading.Thread(target=server.serve_forever, name="tsu-relay", daemon=True)
    thread.start()
    try:
        while not stop_event.is_set() and thread.is_alive():
            stop_event.wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("正在关闭…")
        server.stop()
    logger.ok("中继已停止。累计：%s" % server.status_line())
    return 0


# --------------------------------------------------------------------- tunnel
def cmd_tunnel(args: argparse.Namespace, logger: log_mod.Logger) -> int:
    """隧道连通性检查：真的连上中继、真的转发一次数据。"""
    from .tunnel.probe import DEFAULT_PROBE_TARGETS, format_report, run_probe

    config = config_mod.Config.load(args.config)
    action = args.action
    url = args.relay_url or ""
    token = args.relay_token or ""
    if not url:
        name = args.route or str(config.get("proxy.route") or "")
        section = {"cf-relay": "cf_relay", "self-relay": "self_relay"}.get(name, "")
        if section:
            url = str(config.get("%s.url" % section) or "")
            token = token or str(config.get("%s.token" % section) or "")
        else:
            for candidate in ("cf_relay", "self_relay"):
                if config.get("%s.url" % candidate):
                    url = str(config.get("%s.url" % candidate))
                    token = token or str(config.get("%s.token" % candidate) or "")
                    break
    if not url:
        logger.error("没有可测的中继地址。用 --relay-url wss://... 指定，"
                     "或先配置 [cf_relay] url / [self_relay] url")
        return 2

    quick = [("github.com", 443), ("pypi.org", 443), ("huggingface.co", 443),
             ("registry-1.docker.io", 443)]
    targets = quick if action == "probe" else list(DEFAULT_PROBE_TARGETS)
    if args.hosts:
        targets = []
        for piece in hostrules.split_list(args.hosts):
            host, _, raw_port = piece.rpartition(":")
            if host and raw_port.isdigit():
                targets.append((host, int(raw_port)))
            else:
                targets.append((piece, 443))
    log_mod.banner(logger, "TorSOCKS5 %s —— 中继探测：%s" % (__version__, url))

    def progress(item) -> None:
        if item.get("ok"):
            logger.plain("  ✓ %s:%s %sms" % (item["host"], item["port"], item.get("connect_ms")))
        else:
            logger.plain("  ✗ %s:%s %s：%s" % (item["host"], item["port"],
                                             item.get("stage", "?"), item.get("error")))

    report = run_probe(
        url,
        token,
        targets=targets,
        timeout=float(args.timeout),
        http_probe=bool(args.http),
        front=args.front or "",
        insecure=bool(args.insecure),
        on_log=logger.debug,
        on_progress=progress,
    )
    logger.plain("")
    for line in format_report(report).splitlines():
        if line.startswith("  "):
            continue  # 逐条已经打过了
        logger.plain(line)
    if report.get("ok"):
        logger.ok("中继可用，可以 torsocks5 run --route %s" % (
            "cf-relay" if "workers.dev" in url else "self-relay"))
        return 0
    logger.error("中继不可用或目标不可达，见上面的逐条结果")
    return 1


# --------------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="torsocks5",
        description="跨平台 SOCKS5 本地代理：可选 Tor+meek 网桥 / Cloudflare Worker 中转 / 自建隧道",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
        "  torsocks5 run --route tor-meek --port 9051\n"
        "  torsocks5 routes                          # 看三种路由方式与就绪情况\n"
        "  torsocks5 relay serve --port 9052         # 自建中继（路由 3 的服务端）\n"
        "  torsocks5 run --route self-relay --relay-url ws://127.0.0.1:9052/tsu\n"
        "  torsocks5 tunnel check --route self-relay # 真连一次，看哪些站点可用\n"
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
    run.add_argument("--route", default="", choices=list(routes_mod.route_names()),
                     help="流量路由方式，默认 tor-meek（也可写进配置 [proxy] route）")
    run.add_argument("--relay-url", default="", help="覆盖中继地址（cf-relay / self-relay）")
    run.add_argument("--relay-token", default="", help="覆盖中继令牌")
    run.add_argument("--tor", default="", help="指定 tor 可执行文件")
    run.add_argument("--upstream", default="", help="复用已有的 SOCKS5 端口，如 127.0.0.1:9050")
    run.add_argument("--no-bridge", action="store_true", help="不使用网桥，直接连接 Tor")
    run.add_argument("--bridge", action="append", default=[], help="临时追加一条网桥行（可重复）")
    run.add_argument("--ready-timeout", type=int, default=300,
                        help="等待 Tor 引导完成的秒数（meek 首次引导可能需要几分钟），0 表示不等待")
    run.add_argument("--keep-going", action="store_true", help="引导失败也继续提供服务")
    run.set_defaults(func=cmd_run)

    routes = sub.add_parser("routes", help="列出三种流量路由方式与就绪情况")
    routes.set_defaults(func=cmd_routes)

    doctor = sub.add_parser("doctor", help="环境自检")
    doctor.add_argument("--port", type=int, default=0, help="要检查的代理端口")
    doctor.set_defaults(func=cmd_doctor)

    relay = sub.add_parser("relay", help="自建中继（路由方式 3 的服务端）")
    relay.add_argument("action", nargs="?", default="serve", choices=["serve", "token"],
                       help="serve=启动中继，token=生成一个随机令牌")
    relay.add_argument("--listen", default="", help="监听地址，默认 127.0.0.1")
    relay.add_argument("--port", type=int, default=0, help="监听端口，默认 9052")
    relay.add_argument("--token", default="", help="访问令牌（不设则任何人可连，慎用）")
    relay.add_argument("--allow-all", action="store_true", help="关闭目标白名单（任意 host:port）")
    relay.add_argument("--allow-host", action="append", default=[], help="追加白名单（逗号分隔，可重复）")
    relay.add_argument("--allow-port", action="append", default=[], help="追加允许端口（可重复）")
    relay.add_argument("--max-streams", type=int, default=0, help="单连接并发流上限，默认 64")
    relay.add_argument("--tls-cert", default="", help="TLS 证书（给中继套上 wss://）")
    relay.add_argument("--tls-key", default="", help="TLS 私钥")
    relay.add_argument("--allow-private", action="store_true",
                       help=argparse.SUPPRESS)  # 仅供本机测试：允许连接私有地址
    relay.set_defaults(func=cmd_relay)

    tunnel = sub.add_parser("tunnel", help="探测中继是否真的可用")
    tunnel.add_argument("action", nargs="?", default="probe", choices=["probe", "check"],
                        help="probe=快速探测，check=完整目标清单")
    tunnel.add_argument("--relay-url", default="", help="中继地址，默认取配置")
    tunnel.add_argument("--relay-token", default="", help="中继令牌")
    tunnel.add_argument("--route", default="", help="按某个路由的配置取中继地址")
    tunnel.add_argument("--hosts", default="", help="自定义目标，形如 a.com:443,b.com:443")
    tunnel.add_argument("--http", action="store_true", help="额外发一个 HTTP 请求验证双向数据")
    tunnel.add_argument("--front", default="", help="域前置：TLS SNI 用这个域名")
    tunnel.add_argument("--insecure", action="store_true", help="不校验证书（自签证书时用）")
    tunnel.add_argument("--timeout", type=float, default=15.0, help="单个目标的超时秒数")
    tunnel.set_defaults(func=cmd_tunnel)

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
# 流量路由方式: tor-meek | cf-relay | self-relay
route = "tor-meek"
# username 非空则启用 RFC 1929 认证
username = ""
password = ""
allow_from = ["127.0.0.1", "::1"]
udp_associate = true

# 智能分流（只对 cf-relay / self-relay 生效）
# mode: auto | smart | all | off
#   auto  = 按路由自动选（cf-relay → smart；self-relay → all）
#   smart = 命中内置「需要辅助访问」名单才走隧道，其余直连
#   all   = 除私有地址外全部走隧道
#   off   = 不分流，全部走隧道
[split]
mode = "auto"
builtin_proxy = true
builtin_direct = true
# proxy_hosts = ["example.com"]
# direct_hosts = ["intranet.example"]

# 路由 2：Cloudflare Worker 中转（部署步骤见 deploy/cloudflare/README.md）
[cf_relay]
url = ""
token = ""
# links = 4          # 并发 WS 链路数（每条链路最多 6 条流）
# max_streams = 6    # 平台硬限制，不要调大

# 路由 3：自建 / 多平台中继（torsocks5 relay serve，或 deploy/deno/）
[self_relay]
url = ""
token = ""

# 自建中继服务端（torsocks5 relay serve 读取这里）
[relay]
listen = "127.0.0.1"
port = 9052
token = ""
allow_all = false
max_streams = 64
# allow_hosts = ["github.com", "pypi.org"]
# allow_ports = [443, 80, 22, 9418]

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
