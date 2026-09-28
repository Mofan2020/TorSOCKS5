#!/usr/bin/env python3
"""CLI 级端到端冒烟：真的起两个进程，经 SOCKS5 取回真实数据。

单元测试覆盖的是库内部；这个脚本覆盖「命令行入口 → 路由装配 → SOCKS5 服务 → 隧道 → 目标」
整条链路，用 subprocess 起 `torsocks5 relay serve` 与 `torsocks5 run --route self-relay`，
再用真实 SOCKS5 客户端取一段 HTTP 内容比对。三平台（Windows / macOS / Linux）都能跑，
不依赖 curl 或外部网络。

用法::

    python scripts/e2e_smoke.py
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.selftest_helpers import TOKEN, start_http_echo  # noqa: E402
from torsocks5.socks5 import client as socks_client  # noqa: E402

RELAY_TOKEN = "e2e-smoke-token"
CLI = os.path.join(ROOT, "torsocks5_cli.py")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_port(host: str, port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def wait_health(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=2) as resp:
                if b'"ok": true' in resp.read(512):
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return False


def fetch_through_proxy(proxy_port: int, target_port: int, timeout: float = 15.0) -> bytes:
    sock = socks_client.socks5_connect(("127.0.0.1", proxy_port), "127.0.0.1", target_port,
                                       timeout=timeout)
    try:
        sock.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        data = b""
        deadline = time.time() + timeout
        while TOKEN.encode() not in data and time.time() < deadline:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data
    finally:
        sock.close()


def build_config(path: str, proxy_port: int, relay_port: int) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "[proxy]\n"
            'listen = "127.0.0.1"\n'
            "port = %d\n"
            'route = "self-relay"\n'
            "\n[split]\n"
            'mode = "off"\n'
            "\n[self_relay]\n"
            'url = "ws://127.0.0.1:%d/tsu"\n'
            'token = "%s"\n'
            % (proxy_port, relay_port, RELAY_TOKEN)
        )


def force_utf8_output() -> None:
    """把 stdout/stderr 切到 UTF-8。

    Windows 控制台/重定向流默认是 cp936 之类的窄编码，直接打印中文或 ✓/✗ 会
    抛 UnicodeEncodeError（CI 上就是这么红的）。三平台都统一成 UTF-8。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError, OSError):  # pragma: no cover - 极老的解释器
            pass


def main() -> int:
    force_utf8_output()
    parser = argparse.ArgumentParser(description="CLI 级端到端冒烟")
    parser.add_argument("--binary", default="",
                        help="要测试的可执行文件/解释器入口（默认用当前解释器跑 torsocks5_cli.py）；"
                             "打包后可以传 dist/TorSOCKS5 验证冻结产物")
    args = parser.parse_args()
    base_cmd = [args.binary] if args.binary else [sys.executable, CLI]
    label = args.binary or ("%s %s" % (sys.executable, os.path.basename(CLI)))

    steps: list[tuple[str, bool, str]] = []
    processes: list[subprocess.Popen] = []
    workdir = tempfile.mkdtemp(prefix="torsocks5-e2e-")
    config_path = os.path.join(workdir, "config.toml")
    relay_port = free_port()
    proxy_port = free_port()
    build_config(config_path, proxy_port, relay_port)
    echo = start_http_echo()
    echo_port = echo.server_address[1]

    def spawn(args_list: list[str], log_name: str) -> subprocess.Popen:
        log_path = os.path.join(workdir, log_name)
        handle = open(log_path, "wb")
        proc = subprocess.Popen(base_cmd + ["--config", config_path] + args_list,
                                stdout=handle, stderr=subprocess.STDOUT)
        proc._torsocks5_log = log_path  # type: ignore[attr-defined]
        processes.append(proc)
        return proc

    def read_log(proc: subprocess.Popen) -> str:
        path = getattr(proc, "_torsocks5_log", "")
        try:
            with open(path, "rb") as handle:
                return handle.read().decode("utf-8", "replace")
        except OSError:
            return ""

    try:
        # ---------------------------------------------------------- 1. 中继
        spawn(["relay", "serve", "--port", str(relay_port), "--token", RELAY_TOKEN,
               "--allow-all", "--allow-private"], "relay.log")
        steps.append(("中继进程起来并监听端口", wait_port("127.0.0.1", relay_port),
                      "relay serve 未在 30s 内监听"))
        steps.append(("中继 /healthz 返回 ok", wait_health(relay_port), "健康检查未通过"))

        # ---------------------------------------------------------- 2. 代理
        spawn(["run"], "proxy.log")
        steps.append(("代理进程起来并监听端口", wait_port("127.0.0.1", proxy_port),
                      "torsocks5 run 未在 30s 内监听"))

        # ---------------------------------------------------------- 3. 经 SOCKS5 取数据
        try:
            body = fetch_through_proxy(proxy_port, echo_port)
            got = TOKEN.encode() in body and b"200 OK" in body
            detail = "经隧道取回 %d 字节：%r" % (len(body), body[:60])
        except OSError as exc:
            got, detail = False, "SOCKS5 请求失败：%s" % exc
        steps.append(("经 SOCKS5 → 隧道 → 目标 取回正确内容", got, detail))

        # ---------------------------------------------------------- 4. 分流：私有地址直连
        direct_ok = False
        try:
            sock = socks_client.socks5_connect(("127.0.0.1", proxy_port), "127.0.0.1", echo_port,
                                               timeout=10.0)
            sock.close()
            direct_ok = True
        except OSError as exc:
            direct_ok = False
            detail = str(exc)
        steps.append(("第二次连接（复用同一代理）正常", direct_ok,
                      "" if direct_ok else detail))

        # ---------------------------------------------------------- 5. tunnel check 子命令
        check = subprocess.run(
            base_cmd + ["tunnel", "check",
                        "--relay-url", "ws://127.0.0.1:%d/tsu" % relay_port,
                        "--relay-token", RELAY_TOKEN,
                        "--hosts", "127.0.0.1:%d" % echo_port, "--http"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
        out = check.stdout.decode("utf-8", "replace")
        steps.append(("tunnel check 报告目标可用", check.returncode == 0 and "1/1" in out,
                      out.strip().splitlines()[-1] if out.strip() else "无输出"))

        # ---------------------------------------------------------- 6. routes 子命令
        routes = subprocess.run(base_cmd + ["routes"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        out = routes.stdout.decode("utf-8", "replace")
        steps.append(("routes 列出三种路由",
                      all(name in out for name in ("tor-meek", "cf-relay", "self-relay")),
                      "输出里缺少路由名"))
    finally:
        for proc in processes:
            try:
                proc.terminate()
            except OSError:
                pass
        deadline = time.time() + 10
        for proc in processes:
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
        echo.shutdown()

    print("端到端冒烟（CLI 级）")
    print("  被测入口：%s" % label)
    failed = 0
    for label, ok, detail in steps:
        print("  %s %s%s" % ("✓" if ok else "✗", label, "" if ok else " —— " + detail))
        if not ok:
            failed += 1
            for proc in processes:
                log = read_log(proc)
                if log:
                    print("      [%s 末尾日志]\n%s" % (os.path.basename(
                        getattr(proc, "_torsocks5_log", "log")),
                        "\n".join("        " + line for line in log.splitlines()[-12:])))
    print()
    print("共 %d 项，%d 项失败" % (len(steps), failed))
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
