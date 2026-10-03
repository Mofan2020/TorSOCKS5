"""阶段 1 核心增强的单元测试：限流、连接限制、IP 过滤、熔断、访问日志、负载均衡。"""

from __future__ import annotations

import os
import tempfile
import unittest

from torsocks5.tunnel.balancer import (
    CircuitBreaker,
    LeastConnections,
    RelayNode,
    RelayState,
    SplitRuleBinding,
    WeightedRoundRobin,
    create_strategy,
)
from torsocks5.tunnel.ratelimit import (
    AccessLogger,
    ConnectionLimiter,
    IPFilter,
    RateLimiter,
    RelayCircuitBreaker,
    TokenBucket,
    parse_ip_rules,
)


class TokenBucketTest(unittest.TestCase):
    def test_disabled_when_zero_rate(self):
        bucket = TokenBucket(0)
        self.assertFalse(bucket.enabled)
        self.assertTrue(bucket.try_acquire(1000))

    def test_consume_and_refill(self):
        bucket = TokenBucket(rate=10, burst=10, now=0.0)
        # 初始满桶，容量 10
        for _ in range(10):
            self.assertTrue(bucket.try_acquire(1, now=0.0))
        # 第 11 个失败
        self.assertFalse(bucket.try_acquire(1, now=0.0))
        # 0.1 秒后补 1 个令牌
        self.assertTrue(bucket.try_acquire(1, now=0.1))

    def test_retry_after(self):
        bucket = TokenBucket(rate=2, burst=1, now=0.0)
        self.assertTrue(bucket.try_acquire(1, now=0.0))
        # 桶空，rate=2 → 需要 0.5 秒
        self.assertAlmostEqual(bucket.retry_after(1), 0.5, places=2)


class RateLimiterTest(unittest.TestCase):
    def test_disabled(self):
        limiter = RateLimiter()
        self.assertFalse(limiter.enabled)
        allowed, retry = limiter.check("1.2.3.4")
        self.assertTrue(allowed)
        self.assertEqual(retry, 0.0)

    def test_per_ip_limit(self):
        limiter = RateLimiter(per_ip_rps=1, global_burst=1)
        # burst 默认 = max(1, rate) = 1
        ok1, _ = limiter.check("10.0.0.1")
        ok2, retry = limiter.check("10.0.0.1")
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertGreater(retry, 0)
        # 不同 IP 不受影响
        ok3, _ = limiter.check("10.0.0.2")
        self.assertTrue(ok3)
        self.assertEqual(limiter.rejected, 1)

    def test_per_token_limit(self):
        limiter = RateLimiter(per_token_rps=1)
        ok1, _ = limiter.check("10.0.0.1", "tok-a")
        ok2, _ = limiter.check("10.0.0.1", "tok-a")
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        # 同 IP 不同 token 不受影响
        ok3, _ = limiter.check("10.0.0.1", "tok-b")
        self.assertTrue(ok3)


class ConnectionLimiterTest(unittest.TestCase):
    def test_limits(self):
        limiter = ConnectionLimiter(max_per_ip=2, max_per_token=3, max_total=10)
        self.assertTrue(limiter.enabled)
        self.assertTrue(limiter.acquire("1.1.1.1", "t1"))
        self.assertTrue(limiter.acquire("1.1.1.1", "t1"))
        self.assertFalse(limiter.acquire("1.1.1.1", "t1"))  # IP 上限 2
        self.assertTrue(limiter.acquire("2.2.2.2", "t2"))
        self.assertTrue(limiter.acquire("3.3.3.3", "t2"))
        self.assertTrue(limiter.acquire("4.4.4.4", "t2"))
        self.assertFalse(limiter.acquire("5.5.5.5", "t2"))  # token 上限 3
        self.assertTrue(limiter.acquire("5.5.5.5", "t3"))   # 换 token 可以
        # 释放后可以再拿
        limiter.release("1.1.1.1", "t1")
        self.assertTrue(limiter.acquire("1.1.1.1", "t1"))

    def test_total_limit(self):
        limiter = ConnectionLimiter(max_total=4)
        for i in range(4):
            self.assertTrue(limiter.acquire("10.0.0.%d" % i))
        self.assertFalse(limiter.acquire("10.0.0.99"))  # 总数上限 4
        limiter.release("10.0.0.1")
        self.assertTrue(limiter.acquire("10.0.0.99"))

    def test_no_limits(self):
        limiter = ConnectionLimiter()
        self.assertFalse(limiter.enabled)
        for _ in range(100):
            self.assertTrue(limiter.acquire("1.1.1.1"))

    def test_stats(self):
        limiter = ConnectionLimiter(max_per_ip=10)
        limiter.acquire("1.1.1.1", "tok")
        stats = limiter.stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["tracked_ips"], 1)
        self.assertEqual(stats["tracked_tokens"], 1)


class IPFilterTest(unittest.TestCase):
    def test_empty_allows_all(self):
        f = IPFilter()
        self.assertFalse(f.enabled)
        self.assertTrue(f.allowed("8.8.8.8"))

    def test_whitelist(self):
        f = IPFilter(whitelist=["192.168.0.0/16", "10.0.0.1"])
        self.assertTrue(f.enabled)
        self.assertTrue(f.allowed("192.168.1.5"))
        self.assertTrue(f.allowed("10.0.0.1"))
        self.assertFalse(f.allowed("8.8.8.8"))

    def test_blacklist(self):
        f = IPFilter(blacklist=["10.0.0.0/8"])
        self.assertFalse(f.allowed("10.1.2.3"))
        self.assertTrue(f.allowed("8.8.8.8"))

    def test_parse_ip_rules(self):
        wl, bl = parse_ip_rules(["10.0.0.0/8", "!10.1.2.3", "bad-rule", "192.168.1.1"])
        self.assertEqual(wl, ["10.0.0.0/8", "192.168.1.1"])
        self.assertEqual(bl, ["10.1.2.3"])


class CircuitBreakerTest(unittest.TestCase):
    def test_client_circuit_breaker(self):
        cb = CircuitBreaker(failure_threshold=3, success_threshold=2, timeout=0.05)
        self.assertTrue(cb.can_execute())
        cb.record_failure()
        cb.record_failure()
        self.assertNotEqual(cb.state, RelayState.CIRCUIT_OPEN)
        cb.record_failure()
        self.assertEqual(cb.state, RelayState.CIRCUIT_OPEN)
        self.assertFalse(cb.can_execute())
        # 超时后进入半开
        import time
        time.sleep(0.06)
        self.assertEqual(cb.state, RelayState.CIRCUIT_HALF_OPEN)
        self.assertTrue(cb.can_execute())
        # 半开中失败 → 重新熔断
        cb.record_failure()
        self.assertEqual(cb.state, RelayState.CIRCUIT_OPEN)
        # 半开成功 → 恢复
        time.sleep(0.06)
        cb.record_success()
        cb.record_success()
        self.assertEqual(cb.state, RelayState.HEALTHY)

    def test_relay_circuit_breaker(self):
        cb = RelayCircuitBreaker(error_threshold=3, window_seconds=10, recovery_seconds=0.05)
        self.assertTrue(cb.enabled)
        self.assertFalse(cb.open_)
        cb.record_error()
        cb.record_error()
        self.assertFalse(cb.open_)
        cb.record_error()
        self.assertTrue(cb.open_)
        self.assertEqual(cb.trips, 1)
        import time
        time.sleep(0.06)
        self.assertFalse(cb.open_)  # 半开恢复

    def test_relay_circuit_disabled(self):
        cb = RelayCircuitBreaker(error_threshold=0)
        self.assertFalse(cb.enabled)
        for _ in range(100):
            cb.record_error()
        self.assertFalse(cb.open_)


class AccessLoggerTest(unittest.TestCase):
    def test_json_logging_and_target_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "access.log")
            logger = AccessLogger(path=path, fmt="json", log_targets=False)
            logger.record({"event": "stream_open", "sid": 1, "target": "github.com:443"})
            logger.record({"event": "conn_open", "ip": "1.2.3.4"})
            logger.close()
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            self.assertEqual(len(lines), 2)
            import json
            first = json.loads(lines[0])
            self.assertEqual(first["event"], "stream_open")
            self.assertNotIn("target", first)  # 未记录目标域名
            second = json.loads(lines[1])
            self.assertEqual(second["ip"], "1.2.3.4")
            self.assertIn("ts", second)

    def test_disabled_when_no_path(self):
        logger = AccessLogger(path="")
        self.assertFalse(logger.enabled)
        logger.record({"event": "x"})  # 不应报错

    def test_text_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "access.log")
            logger = AccessLogger(path=path, fmt="text")
            logger.record({"event": "conn_open", "ip": "1.2.3.4"})
            logger.close()
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("event=conn_open", content)
            self.assertIn("ip=1.2.3.4", content)


def _node(url: str, weight: int = 1, active: int = 0, latency: float = 0.0,
          bind=None, available: bool = True) -> RelayNode:
    node = RelayNode(url=url, weight=weight, bind_hosts=bind or [],
                     active_streams=active, avg_latency_ms=latency)
    node.client = object() if available else None  # type: ignore[assignment]
    return node


class LoadBalancerTest(unittest.TestCase):
    def test_weighted_round_robin_distribution(self):
        balancer = WeightedRoundRobin()
        a = _node("ws://a", weight=3)
        b = _node("ws://b", weight=1)
        counts = {"a": 0, "b": 0}
        for _ in range(400):
            node = balancer.select([a, b])
            counts["a" if node is a else "b"] += 1
        # 3:1 权重 → 大约 300:100（平滑加权轮询精确）
        self.assertAlmostEqual(counts["a"], 300, delta=5)
        self.assertAlmostEqual(counts["b"], 100, delta=5)

    def test_skips_unavailable(self):
        balancer = WeightedRoundRobin()
        down = _node("ws://down", available=False)
        up = _node("ws://up")
        for _ in range(10):
            self.assertIs(balancer.select([down, up]), up)
        self.assertIsNone(balancer.select([down]))

    def test_least_connections_prefers_idle(self):
        balancer = LeastConnections()
        busy = _node("ws://busy", active=10)
        idle = _node("ws://idle", active=0)
        for _ in range(5):
            self.assertIs(balancer.select([busy, idle]), idle)

    def test_least_connections_latency_factor(self):
        # 连接数相同时，延迟低的优先
        balancer = LeastConnections(latency_weight=0.5)
        slow = _node("ws://slow", active=2, latency=100)
        fast = _node("ws://fast", active=2, latency=10)
        self.assertIs(balancer.select([slow, fast]), fast)

    def test_split_binding_host_match(self):
        fallback = WeightedRoundRobin()
        balancer = SplitRuleBinding(fallback)
        github = _node("ws://github-relay", bind=["github.com"])
        general = _node("ws://general-relay")
        # 绑定域名命中
        self.assertIs(balancer.select([github, general], host="api.github.com"), github)
        self.assertIs(balancer.select([github, general], host="github.com"), github)
        # 未命中 → 回退
        node = balancer.select([github, general], host="pypi.org")
        self.assertIn(node, (github, general))

    def test_create_strategy_unknown(self):
        with self.assertRaises(ValueError):
            create_strategy("no-such-strategy")

    def test_create_strategy_names(self):
        self.assertEqual(create_strategy("weighted_rr").name(), "weighted_round_robin")
        self.assertEqual(create_strategy("least_conn").name(), "least_connections")
        self.assertEqual(create_strategy("split_binding").name(), "split_binding")


class MultiRelayClientStatsTest(unittest.TestCase):
    def test_stats_without_start(self):
        from torsocks5.tunnel.balancer import MultiRelayClient
        client = MultiRelayClient(
            [{"url": "ws://127.0.0.1:1/tsu", "token": "t", "weight": 2}],
            strategy_name="weighted_rr",
        )
        stats = client.stats()
        self.assertEqual(stats["total_nodes"], 1)
        self.assertEqual(stats["strategy"], "weighted_round_robin")
        self.assertEqual(stats["nodes"][0]["weight"], 2)
        self.assertEqual(stats["nodes"][0]["state"], "healthy")
        self.assertIsNone(client.get_client_for_target("example.com", 443))


if __name__ == "__main__":
    unittest.main()
