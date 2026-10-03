#!/usr/bin/env python3
"""跨语言互通验证：**Python 客户端** ↔ 真实运行的 JS/TS 中继（Cloudflare Worker 或 Deno）。

四端都按 `docs/tunnel-protocol.md` 编解码，但「各写各的测试」不等于「能互相说话」。
这个脚本用项目自己的 Python 客户端去连别人实现的的中继，并**真的转发一次数据**：

1. 起中继（`--start` 给命令），等它的 `/healthz` 就绪；
2. 用 `TunnelClient` 连上去，向一个公开目标（默认 `github.com:80`）发一条 HTTP/1.0 请求，
   断言拿回以 `HTTP/` 开头的状态行——证明帧编解码、多路复用、数据转发三件事都真的对上了；
3. 再连一个私有地址，断言被中继按规范拒绝（策略一致性）；
4. 关掉中继，打印结果。

用法::

    # 自行实现的中继（参考 docs/tunnel-protocol.md）
    python scripts/interop_relay.py \
        --start "your-relay-command --port 8791 --token devtoken" \
        --port 8791 --token devtoken --front-url "ws://127.0.0.1:8791/tsu"
"""

from __future__ import annotations

import argparse
import os
import shlex  # noqa: F401 - 保留给未来的非 shell 启动方式
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from torsocks5.log import force_utf8_output  # noqa: E402
from torsocks5.tunnel.client import (  # noqa: E402
    TargetNotAllowed,
    TunnelClient,
    TunnelError,
    TunnelUnavailable,
)


def wait_health(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=3) as resp:
                if b'"ok"' in resp.read(1024):
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def wait_port(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def http_via_relay(client: TunnelClient, host: str, port: int, timeout: float = 20.0) -> str:
    stream = client.connect(host, port, timeout=timeout)
    try:
        stream.settimeout(timeout)
        stream.sendall(("GET / HTTP/1.0\r\nHost: %s\r\nUser-Agent: TorSOCKS5-interop\r\n\r\n"
                        % host).encode())
        data = b""
        deadline = time.time() + timeout
        while b"\r\n" not in data and time.time() < deadline and len(data) < 8192:
            chunk = stream.recv(1024)
            if not chunk:
                break
            data += chunk
        return data.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
    finally:
        stream.close()


def terminate_tree(proc: subprocess.Popen, grace: float = 8.0) -> str:
    """结束整个进程组（wrangler/deno 会派生子进程，只 kill 父进程会留下孤儿）。"""
    import signal as signal_mod

    try:
        os.killpg(os.getpgid(proc.pid), signal_mod.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    text = ""
    deadline = time.time() + grace
    try:
        text = proc.communicate(timeout=grace)[0].decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal_mod.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        try:
            text = proc.communicate(timeout=5)[0].decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            text = ""
    _ = deadline
    return text


def main() -> int:
    force_utf8_output(line_buffering=True)  # Windows 编码 + 子进程日志实时可见
    parser = argparse.ArgumentParser(description="Python 客户端 ↔ JS/TS 中继 互通验证")
    parser.add_argument("--start", required=True, help="启动中继的 shell 命令")
    parser.add_argument("--url", default="", help="客户端连接地址（默认 ws://127.0.0.1:<port>/tsu）")
    parser.add_argument("--port", type=int, default=8790, help="中继 HTTP/WS 端口（用于健康检查）")
    parser.add_argument("--token", default="", help="中继令牌")
    parser.add_argument("--host", default="github.com", help="互通测试目标")
    parser.add_argument("--target-port", type=int, default=80)
    parser.add_argument("--env", action="append", default=[], help="额外环境变量 KEY=VALUE（可重复）")
    parser.add_argument("--timeout", type=float, default=90.0, help="等待中继就绪的秒数")
    args = parser.parse_args()

    url = args.url or "ws://127.0.0.1:%d/tsu" % args.port
    env = dict(os.environ)
    for item in args.env:
        key, _, value = item.partition("=")
        env[key] = value

    print("跨语言互通验证：Python 客户端 → %s" % url)
    print("  启动命令：%s" % args.start)
    proc = subprocess.Popen(args.start, shell=True, cwd=ROOT, env=env,  # noqa: S602 - 开发脚本
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)
    results: list[tuple[str, bool, str]] = []
    try:
        port_ok = wait_port(args.port, timeout=args.timeout)
        health_ok = port_ok and wait_health(args.port, timeout=15.0)
        results.append(("中继起来并 /healthz 正常", health_ok,
                        "端口未就绪" if not port_ok else "健康检查失败"))
        if not health_ok:
            raise RuntimeError("中继没在 %d 端口监听——若中继用的是别的端口，"
                               "请检查 --start 里有没有把端口/令牌传给中继"
                               "（Deno 与 Worker 都认 TSU_PORT / TSU_TOKEN）" % args.port)

        client = TunnelClient(url, args.token, links=1, max_streams=6, open_timeout=20.0,
                              idle_timeout=30.0, auto_reconnect=False)
        try:
            client.start()
        except TunnelUnavailable as exc:
            results.append(("Python 客户端完成 WS 握手 + 鉴权", False, str(exc)))
            raise RuntimeError("握手失败") from exc
        results.append(("Python 客户端完成 WS 握手 + 鉴权", True, ""))
        try:
            status = http_via_relay(client, args.host, args.target_port)
            ok = status.startswith("HTTP/")
            results.append(("经中继真实转发数据（%s:%d）" % (args.host, args.target_port), ok,
                            status or "没有收到任何响应"))
        except (TunnelError, OSError) as exc:
            results.append(("经中继真实转发数据（%s:%d）" % (args.host, args.target_port),
                            False, str(exc)))

        try:
            stream = client.connect("127.0.0.1", 80, timeout=10.0)
            stream.close()
            results.append(("私有地址策略一致（应被拒绝）", False, "竟然放行了私有地址！"))
        except TargetNotAllowed as exc:
            results.append(("私有地址策略一致（应被拒绝）", True, str(exc)))
        except (TunnelError, OSError) as exc:
            results.append(("私有地址策略一致（应被拒绝）", False,
                            "被拒但不是策略错误，而是：%s" % exc))
        finally:
            client.close()
    except RuntimeError as exc:
        print("  ! %s" % exc)
    finally:
        out = terminate_tree(proc)
        if out:
            tail = out.splitlines()[-15:]
            print("  中继日志尾部：")
            for line in tail:
                print("    " + line)

    failed = sum(1 for _label, ok, _detail in results if not ok)
    print()
    for label, ok, detail in results:
        print("  %s %s%s" % ("✓" if ok else "✗", label, "" if ok else " —— " + detail))
    print()
    print("共 %d 项，%d 项失败" % (len(results), failed))
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
