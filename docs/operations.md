# 运维：配置热重载与结构化日志

面向长期运行的部署场景：**改配置不重启进程**、**日志可被程序消费**。

---

## 1. 配置热重载

v2.0 起 `torsocks5 run` 支持两种触发方式（可同时开启）：

| 触发方式 | 配置项 | 说明 |
| --- | --- | --- |
| SIGHUP 信号 | `hotreload.enabled`（默认 `true`） | `kill -HUP <pid>` 立即重载 |
| 文件监听 | `hotreload.watch`（默认 `false`） | 轮询配置文件 mtime，保存约 2 秒后自动重载 |
| HTTP API | `hotreload.api_enabled`（默认 `true`） | 仅监听 `127.0.0.1:9053`，Basic Auth |

```toml
[hotreload]
enabled = true
watch = false
api_enabled = true
# api_host = "127.0.0.1"
# api_port = 9053
# api_path = "/api/config/reload"
```

### 触发示例

```bash
# 1) SIGHUP
kill -HUP $(pgrep -f "torsocks5.*run")

# 2) HTTP API（认证用 [proxy] 的 username/password，未设置凭据时 API 一律拒绝）
# 查看状态
curl -u admin:secret http://127.0.0.1:9053/api/config/reload
# 触发重载
curl -u admin:secret -X POST http://127.0.0.1:9053/api/config/reload
```

`POST` 返回 JSON：

```json
{"success": true, "changes": [{"key": "proxy.max_connections", "old": 512, "new": 256,
  "sensitive": false}], "warnings": [], "error": null}
```

敏感键（`proxy.password`、各 `*.token` 等）在日志与返回里都会脱敏显示。

### 哪些配置能热更

| 变更类型 | 生效方式 | 影响 |
| --- | --- | --- |
| `proxy.max_connections` / `idle_timeout` / `connect_timeout` / `verbose` / 认证 | 立即生效 | 新连接按新值；已有连接不受影响 |
| `split.*`、`*_relay.*`（含 `nodes`、`lb_strategy`）、`proxy.route`、`tor.*`、`meek.*` | **热切换路由** | 旧路由停掉、新路由拉起，**已有连接保持到自然结束** |
| `relay.*`（服务端） | 下次 `relay serve` 启动生效 | 中继是独立进程 |
| `proxy.listen` / `proxy.port` | **需重启** | 改监听地址要重新 bind |

实现见 `torsocks5/hotreload.py`：重载时把新配置展平成点分路径做 diff，
只对变更项动作；路由重建走 `create_route()` + 原子替换 connector。

---

## 2. 结构化日志（JSON Lines）

`[logging].file` 非空时，`run` 与 `relay serve` 的日志会**同时**追加一份到文件，
每行一个 JSON 对象：

```toml
[logging]
format = "json"                  # json | text
level = "info"
file = "~/.torsocks5/run.jsonl"
max_size_mb = 100                # 单文件大小上限
max_files = 10                   # 保留历史文件数
max_age_days = 30                # 按天轮转
compress = true                  # 轮转后 gzip（.jsonl.gz）
redact = ["my_custom_secret"]    # 额外脱敏字段
```

```json
{"ts": "2026-10-03T18:11:29.123", "level": "INFO", "logger": "torsocks5",
 "msg": "中继地址: ws://127.0.0.1:9052/tsu"}
```

- **脱敏**：`token` / `password` / `secret` / `api_key` / `authorization` / `Bearer` /
  带凭据的 URL 等模式自动打码，`redact` 可追加字段名；
- **轮转**：大小与时间双条件，超龄/超数的文件自动清理；
- 控制台输出不受影响（仍是你熟悉的彩色文本）。

实现见 `torsocks5/logging_structured.py`。

---

## 3. 中继访问日志

与上面的进程日志独立：`[relay].access_log` 记录**连接级事件**，
为审计与限流排障设计：

```toml
[relay]
access_log = "~/.torsocks5/relay-access.log"
access_log_format = "json"       # json | text
```

```json
{"ts": "2026-10-03T18:11:29.123", "event": "conn_open", "ip": "203.0.113.7", "token": true}
{"ts": "2026-10-03T18:11:30.456", "event": "conn_reject", "reason": "rate_limit",
 "ip": "203.0.113.7", "retry_after": 0.8}
```

**默认不记录目标域名**（隐私）；确需审计目标时用中继的 `-v` 打到控制台日志。
事件类型：`conn_open` / `conn_close` / `conn_reject` / `stream_open` /
`stream_reject`。

---

## 相关测试

```bash
python -m unittest tests.test_hotreload    # 热重载：diff / 重建判定 / API 认证
python -m unittest tests.test_phase1       # 限流 / 连接限制 / IP 过滤 / 熔断 / 负载均衡
```
