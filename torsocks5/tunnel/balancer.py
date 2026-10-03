"""多中继负载均衡 + 熔断器。"""

from __future__ import annotations

import random
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from .client import TunnelClient


class RelayState(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CIRCUIT_OPEN = "circuit_open"
    CIRCUIT_HALF_OPEN = "circuit_half_open"


@dataclass
class RelayNode:
    """中继节点配置。"""
    url: str
    token: str = ""
    weight: int = 1
    max_streams: int = 64
    health_check_interval: float = 30.0
    # 分流绑定：这些域名强制走此中继
    bind_hosts: List[str] = field(default_factory=list)
    # 连接参数
    front: str = ""
    insecure: bool = False
    token_in_header: bool = False
    open_timeout: float = 30.0
    idle_timeout: float = 300.0
    keepalive: float = 30.0

    # 运行时状态（不持久化）
    client: Optional[TunnelClient] = None
    state: RelayState = RelayState.HEALTHY
    consecutive_failures: int = 0
    total_requests: int = 0
    successful_requests: int = 0
    last_health_check: float = 0.0
    avg_latency_ms: float = 0.0
    active_streams: int = 0

    def is_available(self) -> bool:
        return self.state in (RelayState.HEALTHY, RelayState.DEGRADED) and self.client is not None

    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 1.0
        return self.successful_requests / self.total_requests


class LoadBalancerStrategy(ABC):
    """负载均衡策略基类。"""

    @abstractmethod
    def select(self, nodes: List[RelayNode], host: str = "", port: int = 0) -> Optional[RelayNode]:
        """选择一个节点。返回 None 表示无可用节点。"""
        pass

    @abstractmethod
    def name(self) -> str:
        pass


class WeightedRoundRobin(LoadBalancerStrategy):
    """加权轮询。"""

    def __init__(self):
        self._current_weights: Dict[str, int] = {}
        self._lock = threading.Lock()

    def name(self) -> str:
        return "weighted_round_robin"

    def select(self, nodes: List[RelayNode], host: str = "", port: int = 0) -> Optional[RelayNode]:
        available = [n for n in nodes if n.is_available()]
        if not available:
            return None

        # 先尝试按 bind_hosts 匹配
        for node in available:
            if self._match_host(node.bind_hosts, host):
                return node

        # 加权轮询（平滑加权轮询算法）
        with self._lock:
            total_weight = sum(n.weight for n in available)
            if total_weight == 0:
                return random.choice(available)

            best = None
            best_score = float("-inf")

            for node in available:
                key = node.url
                cw = self._current_weights.get(key, 0)
                cw += node.weight
                self._current_weights[key] = cw

                score = cw - total_weight
                if score > best_score:
                    best_score = score
                    best = node

            if best:
                self._current_weights[best.url] -= total_weight
            return best

    def _match_host(self, bind_hosts: List[str], host: str) -> bool:
        if not bind_hosts or not host:
            return False
        host_lower = host.lower()
        for pattern in bind_hosts:
            pattern = pattern.lower()
            if host_lower == pattern or host_lower.endswith("." + pattern):
                return True
        return False


class LeastConnections(LoadBalancerStrategy):
    """最少连接 + 延迟感知（EWMA 平滑）。"""

    def __init__(self, latency_weight: float = 0.3):
        self.latency_weight = latency_weight
        self._lock = threading.Lock()

    def name(self) -> str:
        return "least_connections"

    def select(self, nodes: List[RelayNode], host: str = "", port: int = 0) -> Optional[RelayNode]:
        available = [n for n in nodes if n.is_available()]
        if not available:
            return None

        # 先尝试按 bind_hosts 匹配
        for node in available:
            if self._match_host(node.bind_hosts, host):
                return node

        # 计算综合分数：连接数 + 延迟权重
        # score = active_streams * (1 + latency_weight * normalized_latency)
        best = None
        best_score = float('inf')

        max_latency = max(n.avg_latency_ms for n in available) or 1.0

        for node in available:
            latency_factor = 1.0
            if node.avg_latency_ms > 0:
                latency_factor = 1.0 + self.latency_weight * (node.avg_latency_ms / max_latency)

            score = node.active_streams * latency_factor
            if score < best_score:
                best_score = score
                best = node

        return best

    def _match_host(self, bind_hosts: List[str], host: str) -> bool:
        if not bind_hosts or not host:
            return False
        host_lower = host.lower()
        for pattern in bind_hosts:
            pattern = pattern.lower()
            if host_lower == pattern or host_lower.endswith("." + pattern):
                return True
        return False


class SplitRuleBinding(LoadBalancerStrategy):
    """按分流规则绑定中继：特定域名/端口走指定中继。"""

    def __init__(self, fallback: LoadBalancerStrategy):
        self.fallback = fallback

    def name(self) -> str:
        return "split_binding"

    def select(self, nodes: List[RelayNode], host: str = "", port: int = 0) -> Optional[RelayNode]:
        available = [n for n in nodes if n.is_available()]
        if not available:
            return None

        # 精确匹配 bind_hosts
        for node in available:
            if self._match_host(node.bind_hosts, host):
                return node

        # 回退到备选策略
        return self.fallback.select(nodes, host, port)

    def _match_host(self, bind_hosts: List[str], host: str) -> bool:
        if not bind_hosts or not host:
            return False
        host_lower = host.lower()
        for pattern in bind_hosts:
            pattern = pattern.lower()
            if host_lower == pattern or host_lower.endswith("." + pattern):
                return True
        return False


STRATEGIES: Dict[str, Callable[[], LoadBalancerStrategy]] = {
    "weighted_rr": WeightedRoundRobin,
    "least_conn": LeastConnections,
    "split_binding": lambda: SplitRuleBinding(WeightedRoundRobin()),
}


def create_strategy(name: str) -> LoadBalancerStrategy:
    factory = STRATEGIES.get(name.lower())
    if not factory:
        raise ValueError(f"未知的负载均衡策略: {name}，可选: {list(STRATEGIES.keys())}")
    return factory()


class CircuitBreaker:
    """熔断器：失败阈值触发熔断，半开状态探测恢复。"""

    def __init__(
        self,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        timeout: float = 60.0,
        half_open_max_calls: int = 3,
    ):
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.timeout = timeout
        self.half_open_max_calls = half_open_max_calls

        self._state = RelayState.HEALTHY
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0
        self._half_open_calls = 0
        self._lock = threading.RLock()  # state 属性与方法互访，需可重入

    @property
    def state(self) -> RelayState:
        with self._lock:
            if self._state == RelayState.CIRCUIT_OPEN:
                # 检查是否超时，进入半开状态
                if time.time() - self._last_failure_time >= self.timeout:
                    self._state = RelayState.CIRCUIT_HALF_OPEN
                    self._half_open_calls = 0
                    self._success_count = 0
            return self._state

    def record_success(self) -> None:
        with self._lock:
            # 先让 state 完成 OPEN → HALF_OPEN 的超时转换（RLock 可重入）
            _ = self.state
            if self._state == RelayState.CIRCUIT_HALF_OPEN:
                self._success_count += 1
                self._half_open_calls += 1
                if self._success_count >= self.success_threshold:
                    self._state = RelayState.HEALTHY
                    self._failure_count = 0
                    self._success_count = 0
            elif self._state == RelayState.HEALTHY:
                self._failure_count = 0
            elif self._state == RelayState.DEGRADED:
                # 降级状态下成功也可能恢复
                self._failure_count = max(0, self._failure_count - 1)
                if self._failure_count == 0:
                    self._state = RelayState.HEALTHY

    def record_failure(self) -> None:
        with self._lock:
            # 先让 state 完成 OPEN → HALF_OPEN 的超时转换（RLock 可重入）
            _ = self.state
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == RelayState.CIRCUIT_HALF_OPEN:
                # 半开状态下任何失败直接重新熔断
                self._state = RelayState.CIRCUIT_OPEN
                self._half_open_calls = 0
                self._success_count = 0
            elif self._state == RelayState.HEALTHY:
                if self._failure_count >= self.failure_threshold:
                    self._state = RelayState.CIRCUIT_OPEN
            elif self._state == RelayState.DEGRADED:
                if self._failure_count >= self.failure_threshold * 2:
                    self._state = RelayState.CIRCUIT_OPEN

    def can_execute(self) -> bool:
        with self._lock:
            s = self.state
            if s == RelayState.CIRCUIT_OPEN:
                return False
            if s == RelayState.CIRCUIT_HALF_OPEN:
                return self._half_open_calls < self.half_open_max_calls
            return True

    def force_open(self) -> None:
        with self._lock:
            self._state = RelayState.CIRCUIT_OPEN
            self._last_failure_time = time.time()

    def force_close(self) -> None:
        with self._lock:
            self._state = RelayState.HEALTHY
            self._failure_count = 0
            self._success_count = 0


class HealthProber:
    """健康探测器：定期检查中继可用性。"""

    def __init__(
        self,
        nodes: List[RelayNode],
        check_callback: Callable[[RelayNode], bool],
        interval: float = 30.0,
        timeout: float = 10.0,
    ):
        self.nodes = nodes
        self.check_callback = check_callback
        self.interval = interval
        self.timeout = timeout
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="health-prober", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            for node in self.nodes:
                if self._stop_event.is_set():
                    break
                if node.client:
                    try:
                        healthy = self.check_callback(node)
                        node.last_health_check = time.time()
                        if healthy:
                            node.consecutive_failures = 0
                            if node.state == RelayState.DEGRADED:
                                node.state = RelayState.HEALTHY
                        else:
                            node.consecutive_failures += 1
                            if node.consecutive_failures >= 3:
                                node.state = RelayState.DEGRADED
                    except Exception:
                        node.consecutive_failures += 1
            self._stop_event.wait(self.interval)


class MultiRelayClient:
    """多中继客户端：管理多个 TunnelClient，提供统一接口。"""

    def __init__(
        self,
        nodes_config: List[Dict[str, Any]],
        strategy_name: str = "weighted_rr",
        circuit_breaker_threshold: int = 5,
        circuit_breaker_timeout: float = 60.0,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        self.nodes: List[RelayNode] = []
        self.strategy = create_strategy(strategy_name)
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=circuit_breaker_threshold,
            timeout=circuit_breaker_timeout,
        )
        self.on_log = on_log or (lambda msg: None)
        self._lock = threading.RLock()
        self._started = False
        self._prober: Optional[HealthProber] = None

        # 初始化节点
        for cfg in nodes_config:
            node = RelayNode(
                url=cfg.get("url", ""),
                token=cfg.get("token", ""),
                weight=cfg.get("weight", 1),
                max_streams=cfg.get("max_streams", 64),
                health_check_interval=cfg.get("health_check_interval", 30.0),
                bind_hosts=cfg.get("bind_hosts", []),
                front=cfg.get("front", ""),
                insecure=cfg.get("insecure", False),
                token_in_header=cfg.get("token_in_header", False),
            )
            self.nodes.append(node)

    def start(self) -> None:
        """启动所有中继连接。"""
        with self._lock:
            if self._started:
                return

            for node in self.nodes:
                if not node.url:
                    continue
                try:
                    client = TunnelClient(
                        node.url,
                        node.token,
                        links=max(1, 4),  # 默认每节点 4 条链路
                        max_streams=node.max_streams,
                        open_timeout=node.open_timeout,
                        idle_timeout=node.idle_timeout,
                        keepalive=node.keepalive,
                        on_log=self.on_log,
                        front=node.front,
                        insecure=node.insecure,
                        token_in_header=node.token_in_header,
                    )
                    client.start()
                    node.client = client
                    self.on_log(f"中继已连接: {node.url}")
                except Exception as exc:
                    self.on_log(f"中继连接失败 {node.url}: {exc}")
                    node.state = RelayState.DEGRADED

            # 启动健康探测
            self._prober = HealthProber(
                self.nodes,
                check_callback=self._check_node_health,
                interval=30.0,
            )
            self._prober.start()
            self._started = True

    def _check_node_health(self, node: RelayNode) -> bool:
        """检查单个节点健康度。"""
        if not node.client:
            return False
        try:
            stats = node.client.stats()
            # 简单检查：有活跃链路且无最近错误
            return stats.get("links_live", 0) > 0 and not stats.get("last_error")
        except Exception:
            return False

    def stop(self) -> None:
        with self._lock:
            if self._prober:
                self._prober.stop()
            for node in self.nodes:
                if node.client:
                    try:
                        node.client.close()
                    except Exception:
                        pass
                    node.client = None
            self._started = False

    def get_client_for_target(self, host: str = "", port: int = 0) -> Optional[TunnelClient]:
        """根据目标选择中继客户端。"""
        with self._lock:
            if not self._started:
                return None

            node = self.strategy.select(self.nodes, host, port)
            if node and node.client:
                node.active_streams += 1
                return node.client
            return None

    def release_client(self, client: TunnelClient, success: bool = True) -> None:
        """释放客户端（记录统计）。"""
        with self._lock:
            for node in self.nodes:
                if node.client is client:
                    node.active_streams = max(0, node.active_streams - 1)
                    node.total_requests += 1
                    if success:
                        node.successful_requests += 1
                        self.circuit_breaker.record_success()
                    else:
                        self.circuit_breaker.record_failure()
                        node.consecutive_failures += 1
                        if node.consecutive_failures >= 3:
                            node.state = RelayState.DEGRADED
                    break

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            node_stats = []
            for node in self.nodes:
                ns = {
                    "url": node.url,
                    "weight": node.weight,
                    "state": node.state.value,
                    "max_streams": node.max_streams,
                    "active_streams": node.active_streams,
                    "bind_hosts": node.bind_hosts,
                    "consecutive_failures": node.consecutive_failures,
                    "total_requests": node.total_requests,
                    "success_rate": round(node.success_rate(), 3),
                    "avg_latency_ms": round(node.avg_latency_ms, 1),
                    "last_health_check": node.last_health_check,
                }
                if node.client:
                    ns.update(node.client.stats())
                node_stats.append(ns)

            return {
                "strategy": self.strategy.name(),
                "circuit_breaker": self.circuit_breaker.state.value,
                "nodes": node_stats,
                "total_nodes": len(self.nodes),
                "healthy_nodes": sum(1 for n in self.nodes if n.is_available()),
            }

    def get_node(self, url: str) -> Optional[RelayNode]:
        for node in self.nodes:
            if node.url == url:
                return node
        return None


__all__ = [
    "RelayNode",
    "RelayState",
    "LoadBalancerStrategy",
    "WeightedRoundRobin",
    "LeastConnections",
    "SplitRuleBinding",
    "CircuitBreaker",
    "HealthProber",
    "MultiRelayClient",
    "create_strategy",
    "STRATEGIES",
]
