"""实时日志流：环形缓冲 + 多订阅者广播（Web 面板 SSE 用）。"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Deque, Dict, List

#: 每个订阅者的队列容量；满了丢最旧的（日志宁可丢也不应拖垮面板）
_QUEUE_SIZE = 500


class LogStream:
    """日志环形缓冲，支持多个订阅者实时消费。

    作为 sink 挂到 :class:`torsocks5.log.Logger` 上（``add_sink``），
    面板的 SSE 端点从订阅队列里取，历史接口从环形缓冲里取。
    """

    def __init__(self, capacity: int = 1000) -> None:
        self._buffer: Deque[Dict] = deque(maxlen=capacity)
        self._subscribers: List[queue.Queue] = []
        self._lock = threading.Lock()
        self._seq = 0

    # ------------------------------------------------------------ 生产
    def publish(self, level_name: str, message: str) -> None:
        """记录一条日志（作为 Logger 的 sink 调用）。"""
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "ts": time.strftime("%H:%M:%S"),
                "level": level_name,
                "msg": message,
            }
            self._buffer.append(entry)
            subscribers = list(self._subscribers)
        for sub in subscribers:
            try:
                sub.put_nowait(entry)
            except queue.Full:
                # 队列满：丢弃最旧的一条再试一次，保持实时性
                try:
                    sub.get_nowait()
                    sub.put_nowait(entry)
                except (queue.Empty, queue.Full):
                    pass

    # ------------------------------------------------------------ 消费
    def history(self, limit: int = 200, since: int = 0) -> List[Dict]:
        """返回 ``seq > since`` 的最近 ``limit`` 条。"""
        with self._lock:
            items = [entry for entry in self._buffer if entry["seq"] > since]
        return items[-limit:]

    def subscribe(self) -> queue.Queue:
        """订阅实时日志；用完必须 :meth:`unsubscribe`。"""
        sub: queue.Queue = queue.Queue(maxsize=_QUEUE_SIZE)
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(sub)
            except ValueError:
                pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


__all__ = ["LogStream"]
