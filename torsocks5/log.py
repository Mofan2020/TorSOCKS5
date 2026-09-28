"""跨平台日志：带颜色、级别、时间戳，支持同时写文件。"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Optional, TextIO

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40, "silent": 100}

_COLORS = {
    "debug": "\033[90m",
    "info": "\033[36m",
    "warn": "\033[33m",
    "error": "\033[31m",
    "ok": "\033[32m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def force_utf8_output(line_buffering: bool = False) -> None:
    """把标准输出/错误切到 UTF-8。

    Windows 控制台默认使用本地代码页（cp936 / cp1252），直接输出中文与非
    ASCII 符号（✓ / ✗ 之类）会抛 UnicodeEncodeError，``--help`` 甚至会因此崩溃。

    CLI 必须在构造 argparse 解析器之前调用（argparse 会在 parse_args 时打印帮助）；
    仓库里的脚本（``scripts/*.py``）也共用这个实现，不要各写一份。

    ``line_buffering=True`` 让输出立刻可见——起子进程、看子进程日志的脚本需要它。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if line_buffering:
                reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):  # 已被重定向到不支持的对象
            pass


def supports_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.name == "nt":
        # Windows 10+ 的终端支持 ANSI，但需要主动打开
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            return True
        except Exception:  # noqa: BLE001
            return False
    return hasattr(stream, "isatty") and stream.isatty()


class Logger:
    def __init__(
        self,
        level: str = "info",
        stream: Optional[TextIO] = None,
        log_file: Optional[str] = None,
        color: Optional[bool] = None,
    ) -> None:
        self.level = LEVELS.get(str(level).lower(), 20)
        self.stream = stream or sys.stderr
        self.use_color = supports_color(self.stream) if color is None else color
        self.lock = threading.Lock()
        self.file = None
        if log_file:
            try:
                directory = os.path.dirname(os.path.abspath(log_file))
                os.makedirs(directory, exist_ok=True)
                self.file = open(log_file, "a", encoding="utf-8")
            except OSError as exc:  # pragma: no cover
                print("无法打开日志文件 %s: %s" % (log_file, exc), file=sys.stderr)

    # ------------------------------------------------------------------
    def _emit(self, level_name: str, message: str, color: str = "") -> None:
        if LEVELS.get(level_name, 20) < self.level:
            return
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            if self.use_color and color:
                line = "%s%s%s %s %s\n" % (
                    _COLORS[color], stamp, _COLORS["reset"], level_name.upper().ljust(5), message
                )
            else:
                line = "%s %s %s\n" % (stamp, level_name.upper().ljust(5), message)
            self.stream.write(line)
            self.stream.flush()
            if self.file is not None:
                self.file.write("%s %s %s\n" % (stamp, level_name.upper(), message))
                self.file.flush()

    def debug(self, message: str) -> None:
        self._emit("debug", message, "debug")

    def info(self, message: str) -> None:
        self._emit("info", message, "info")

    def ok(self, message: str) -> None:
        self._emit("info", message, "ok")

    def warn(self, message: str) -> None:
        self._emit("warn", message, "warn")

    def error(self, message: str) -> None:
        self._emit("error", message, "error")

    def plain(self, message: str = "") -> None:
        with self.lock:
            self.stream.write(message + "\n")
            self.stream.flush()
            if self.file is not None:
                self.file.write(message + "\n")
                self.file.flush()

    def close(self) -> None:
        if self.file is not None:
            try:
                self.file.close()
            except OSError:
                pass
            self.file = None


def banner(logger: Logger, text: str) -> None:
    logger.plain("")
    if logger.use_color:
        logger.plain("%s%s%s" % (_COLORS["bold"], text, _COLORS["reset"]))
    else:
        logger.plain(text)
    logger.plain("")


PROGRESS_WIDTH = 28


def clean_tor_line(line: str) -> str:
    """去掉 tor 日志里的时间与级别前缀，只留正文。"""
    parts = line.split("] ", 1)
    return parts[1] if len(parts) == 2 else line


def progress_printer(logger: Logger):
    """把 tor 的引导进度变成日志输出。

    终端里原地刷新一条进度条；重定向到文件时改成一行一条，避免刷出几十万行。
    """
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
        with logger.lock:
            sys.stderr.write("\r  [%s] %3d%% %-22s %s\033[K"
                             % (bar, percent, tag, summary[:40]))
            sys.stderr.flush()

    return printer
