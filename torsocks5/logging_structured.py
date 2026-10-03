"""结构化日志 + 轮转 + 敏感信息脱敏。"""

from __future__ import annotations

import gzip
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Union

from . import config as config_mod

# 敏感字段模式（正则）
SENSITIVE_PATTERNS: List["re.Pattern[str]"] = [
    re.compile(r'''(token["']?\s*[:=]\s*["']?)([^"'\s,}]+)''', re.IGNORECASE),
    re.compile(r'''(password["']?\s*[:=]\s*["']?)([^"'\s,}]+)''', re.IGNORECASE),
    re.compile(r'''(secret["']?\s*[:=]\s*["']?)([^"'\s,}]+)''', re.IGNORECASE),
    re.compile(r'''(api[_-]?key["']?\s*[:=]\s*["']?)([^"'\s,}]+)''', re.IGNORECASE),
    re.compile(r'''(authorization["']?\s*[:=]\s*["']?)([^"'\s,}]+)''', re.IGNORECASE),
    re.compile(r'''(Bearer\s+)([^"'\s,}]+)''', re.IGNORECASE),
    re.compile(r'(url\s*=\s*)([^,\s]+://[^@\s]+@[^,\s]+)', re.IGNORECASE),  # URL with credentials
]

# 额外需要脱敏的键名
SENSITIVE_KEYS: Set[str] = {
    "token", "password", "secret", "api_key", "apikey",
    "authorization", "auth", "credential", "private_key",
    "bridge_url", "relay_token", "relay_url",
}


class RedactingFormatter(logging.Formatter):
    """带脱敏的格式化器，支持 JSON Lines 和文本格式。"""

    def __init__(
        self,
        fmt: str = "json",
        redact_keys: Optional[Set[str]] = None,
        redact_patterns: Optional[List[re.Pattern]] = None,
        include_extra: bool = True,
    ):
        super().__init__()
        self.fmt = fmt
        self.redact_keys = redact_keys or SENSITIVE_KEYS
        self.redact_patterns = redact_patterns or SENSITIVE_PATTERNS
        self.include_extra = include_extra

    def format(self, record: logging.LogRecord) -> str:
        # 基础字段
        log_data = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # 添加额外字段
        if self.include_extra:
            for key, value in record.__dict__.items():
                if key not in {
                    "name", "msg", "args", "created", "filename", "funcName",
                    "levelname", "levelno", "lineno", "module", "msecs",
                    "message", "pathname", "process",
                    "processName", "relativeCreated", "thread", "threadName",
                    "exc_info", "exc_text", "stack_info", "asctime",
                }:
                    log_data[key] = value

        # 脱敏处理
        log_data = self._redact(log_data)

        if self.fmt == "json":
            return json.dumps(log_data, ensure_ascii=False, separators=(",", ":"))
        else:
            # 文本格式：ts level logger msg [key=value ...]
            parts = [
                log_data["ts"],
                log_data["level"],
                log_data["logger"],
                log_data["msg"],
            ]
            extra_parts = []
            for k, v in log_data.items():
                if k not in {"ts", "level", "logger", "msg"}:
                    extra_parts.append(f"{k}={v}")
            if extra_parts:
                parts.append("[" + " ".join(extra_parts) + "]")
            return " ".join(parts)

    def _redact(self, obj: Any) -> Any:
        """递归脱敏。"""
        if isinstance(obj, dict):
            return {k: self._redact(v) if k.lower() not in self.redact_keys else "***REDACTED***"
                    for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._redact(item) for item in obj]
        elif isinstance(obj, str):
            return self._redact_string(obj)
        else:
            return obj

    def _redact_string(self, text: str) -> str:
        """字符串脱敏：URL 中的凭证、token=xxx 等。"""
        result = text
        for pattern in self.redact_patterns:
            result = pattern.sub(r'\1***REDACTED***', result)
        return result


class SensitiveFilter(logging.Filter):
    """日志过滤器：在记录层面就脱敏 message 和 args。"""

    def __init__(self, redact_keys: Optional[Set[str]] = None):
        super().__init__()
        self.redact_keys = redact_keys or SENSITIVE_KEYS
        self.patterns = SENSITIVE_PATTERNS

    def filter(self, record: logging.LogRecord) -> bool:
        # 脱敏 message
        if isinstance(record.msg, str):
            record.msg = self._redact_string(record.msg)
        # 脱敏 args
        if record.args:
            record.args = tuple(self._redact_arg(arg) for arg in record.args)
        return True

    def _redact_arg(self, arg: Any) -> Any:
        if isinstance(arg, str):
            return self._redact_string(arg)
        elif isinstance(arg, dict):
            return {k: "***REDACTED***" if k.lower() in self.redact_keys else self._redact_arg(v)
                    for k, v in arg.items()}
        elif isinstance(arg, (list, tuple)):
            return type(arg)(self._redact_arg(item) for item in arg)
        return arg

    def _redact_string(self, text: str) -> str:
        result = text
        for pattern in self.patterns:
            result = pattern.sub(r'\1***REDACTED***', result)
        return result


class TimedCompressedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """带压缩的按时间轮转处理器。"""

    def __init__(
        self,
        filename: str,
        when: str = "midnight",
        interval: int = 1,
        backup_count: int = 30,
        encoding: str = "utf-8",
        delay: bool = False,
        utc: bool = False,
        compress: bool = True,
        max_size_mb: Optional[int] = None,  # 如果设置，同时按大小轮转
    ):
        super().__init__(filename, when, interval, backup_count, encoding, delay, utc)
        self.compress = compress
        self.max_size_bytes = max_size_mb * 1024 * 1024 if max_size_mb else None
        self._size_handler: Optional[logging.handlers.RotatingFileHandler] = None

        if self.max_size_bytes:
            # 同时创建基于大小的处理器
            self._size_handler = logging.handlers.RotatingFileHandler(
                filename,
                maxBytes=self.max_size_bytes,
                backupCount=backup_count,
                encoding=encoding,
                delay=delay,
            )
            self._size_handler.setFormatter(self.formatter)
            self._size_handler.addFilter(SensitiveFilter())

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        if self._size_handler:
            self._size_handler.emit(record)

    def rotation_filename(self, default_name: str) -> str:
        """轮转文件名：添加时间戳。"""
        base = super().rotation_filename(default_name)
        if self.compress and not base.endswith(".gz"):
            return base + ".gz"
        return base

    def rotate(self, source: str, dest: str) -> None:
        """轮转时压缩旧文件。"""
        if self.compress:
            # 先压缩源文件
            with open(source, "rb") as f_in:
                with gzip.open(dest, "wb") as f_out:
                    f_out.writelines(f_in)
            os.remove(source)
        else:
            super().rotate(source, dest)

    def close(self) -> None:
        super().close()
        if self._size_handler:
            self._size_handler.close()


class SizeAndTimeRotatingHandler(logging.Handler):
    """同时按大小和时间轮转的组合处理器（更可靠的实现）。"""

    def __init__(
        self,
        filename: str,
        max_size_mb: int = 100,
        max_files: int = 10,
        max_age_days: int = 30,
        compress: bool = True,
        encoding: str = "utf-8",
    ):
        super().__init__()
        self.filename = os.path.abspath(filename)
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.max_files = max_files
        self.max_age_days = max_age_days
        self.compress = compress
        self.encoding = encoding

        os.makedirs(os.path.dirname(self.filename), exist_ok=True)

        self._file: Optional[Any] = None
        self._current_size = 0
        self._lock = threading.Lock()
        self._open_file()

        # 启动清理线程
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def _open_file(self) -> None:
        if self._file:
            self._file.close()
        self._file = open(self.filename, "a", encoding=self.encoding)
        self._current_size = os.path.getsize(self.filename)

    def _rotate(self) -> None:
        """执行轮转。"""
        if self._file:
            self._file.close()

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base, ext = os.path.splitext(self.filename)
        rotated = f"{base}.{timestamp}{ext}"
        if self.compress:
            rotated += ".gz"

        # 压缩并移动当前文件
        if self.compress:
            with open(self.filename, "rb") as f_in:
                with gzip.open(rotated, "wb") as f_out:
                    f_out.writelines(f_in)
            os.remove(self.filename)
        else:
            os.rename(self.filename, rotated)

        self._open_file()
        self._cleanup_old_files()

    def _cleanup_old_files(self) -> None:
        """清理过期/超数的轮转文件。"""
        base_dir = os.path.dirname(self.filename)
        base_name = os.path.basename(self.filename)
        prefix = base_name + "."

        files = []
        for fname in os.listdir(base_dir):
            if fname.startswith(prefix):
                fpath = os.path.join(base_dir, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                    files.append((mtime, fpath))
                except OSError:
                    pass

        # 按时间排序，保留最新的 max_files 个
        files.sort(reverse=True)
        cutoff_time = time.time() - self.max_age_days * 86400

        for i, (mtime, fpath) in enumerate(files):
            if i >= self.max_files or mtime < cutoff_time:
                try:
                    os.remove(fpath)
                except OSError:
                    pass

    def _cleanup_loop(self) -> None:
        while True:
            time.sleep(3600)  # 每小时检查一次
            self._cleanup_old_files()

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            try:
                if self._file is None:
                    return
                msg = self.format(record) + "\n"
                msg_bytes = msg.encode(self.encoding)
                self._file.write(msg)
                self._file.flush()
                self._current_size += len(msg_bytes)

                if self._current_size >= self.max_size_bytes:
                    self._rotate()
            except Exception:
                self.handleError(record)

    def close(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None
        super().close()


class StructuredLogger:
    """统一的结构化日志入口。"""

    def __init__(
        self,
        name: str = "torsocks5",
        level: Union[str, int] = "INFO",
        log_file: Optional[str] = None,
        fmt: str = "json",  # json | text
        max_size_mb: int = 100,
        max_files: int = 10,
        max_age_days: int = 30,
        compress: bool = True,
        redact_keys: Optional[Set[str]] = None,
        console_output: bool = True,
    ):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(self._parse_level(level))
        self.logger.propagate = False

        # 清除已有处理器
        for h in self.logger.handlers[:]:
            self.logger.removeHandler(h)

        # 格式化器
        formatter = RedactingFormatter(
            fmt=fmt,
            redact_keys=redact_keys,
            include_extra=True,
        )

        # 控制台处理器
        if console_output:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(formatter)
            console_handler.addFilter(SensitiveFilter(redact_keys))
            self.logger.addHandler(console_handler)

        # 文件处理器
        if log_file:
            file_handler = SizeAndTimeRotatingHandler(
                log_file,
                max_size_mb=max_size_mb,
                max_files=max_files,
                max_age_days=max_age_days,
                compress=compress,
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(SensitiveFilter(redact_keys))
            self.logger.addHandler(file_handler)

    @staticmethod
    def _parse_level(level: Union[str, int]) -> int:
        if isinstance(level, int):
            return level
        return getattr(logging, level.upper(), logging.INFO)

    def debug(self, msg: str, *args, **kwargs) -> None:
        self.logger.debug(msg, *args, **kwargs)

    def info(self, msg: str, *args, **kwargs) -> None:
        self.logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs) -> None:
        self.logger.warning(msg, *args, **kwargs)

    warn = warning

    def error(self, msg: str, *args, **kwargs) -> None:
        self.logger.error(msg, *args, **kwargs)

    def critical(self, msg: str, *args, **kwargs) -> None:
        self.logger.critical(msg, *args, **kwargs)

    def exception(self, msg: str, *args, **kwargs) -> None:
        self.logger.exception(msg, *args, **kwargs)

    def log(self, level: int, msg: str, *args, **kwargs) -> None:
        self.logger.log(level, msg, *args, **kwargs)

    def with_fields(self, **fields) -> "BoundLogger":
        """创建带固定字段的绑定日志器。"""
        return BoundLogger(self.logger, fields)

    def close(self) -> None:
        for h in self.logger.handlers:
            h.close()
        self.logger.handlers.clear()


class BoundLogger:
    """带上下文字段的日志器。"""

    def __init__(self, logger: logging.Logger, fields: Dict[str, Any]):
        self.logger = logger
        self.fields = fields

    def _log(self, level: int, msg: str, *args, **kwargs) -> None:
        extra = {**self.fields, **kwargs.pop("extra", {})}
        self.logger.log(level, msg, *args, extra=extra, **kwargs)

    def debug(self, msg: str, *args, **kwargs) -> None:
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: str, *args, **kwargs) -> None:
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs) -> None:
        self._log(logging.WARNING, msg, *args, **kwargs)

    warn = warning

    def error(self, msg: str, *args, **kwargs) -> None:
        self._log(logging.ERROR, msg, *args, **kwargs)

    def exception(self, msg: str, *args, **kwargs) -> None:
        self._log(logging.ERROR, msg, *args, exc_info=True, **kwargs)


# 全局默认日志器（兼容现有 torsocks5.log.Logger 接口）
_default_logger: Optional[StructuredLogger] = None


def get_default_logger() -> StructuredLogger:
    global _default_logger
    if _default_logger is None:
        _default_logger = StructuredLogger()
    return _default_logger


def configure_logging(config: config_mod.Config) -> StructuredLogger:
    """从配置创建结构化日志器。"""
    global _default_logger

    log_config = config.section("logging")
    if not log_config:
        log_config = {}

    log_file = log_config.get("file")
    if log_file:
        log_file = os.path.expanduser(log_file)

    logger = StructuredLogger(
        name="torsocks5",
        level=log_config.get("level", "info"),
        log_file=log_file,
        fmt=log_config.get("format", "json"),
        max_size_mb=int(log_config.get("max_size_mb", 100)),
        max_files=int(log_config.get("max_files", 10)),
        max_age_days=int(log_config.get("max_age_days", 30)),
        compress=config_mod.as_bool(log_config.get("compress", True)),
        redact_keys=set(log_config.get("redact", list(SENSITIVE_KEYS))),
        console_output=True,
    )
    _default_logger = logger
    return logger


import json  # noqa: E402 - 放在最后避免循环导入

__all__ = [
    "StructuredLogger",
    "BoundLogger",
    "RedactingFormatter",
    "SensitiveFilter",
    "SizeAndTimeRotatingHandler",
    "configure_logging",
    "get_default_logger",
    "SENSITIVE_KEYS",
    "SENSITIVE_PATTERNS",
]
