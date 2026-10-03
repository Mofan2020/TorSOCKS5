# 贡献指南

感谢关注。这个项目不大，代码结构清晰，欢迎各种形式的贡献。

## 开发环境

```bash
git clone https://github.com/Mofan2020/TorSOCKS5.git
cd TorSOCKS5
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

ruff check .                              # 静态检查
mypy torsocks5                            # 类型检查
python -m unittest discover -s tests -v   # 全部单元测试（离线）
python scripts/check_docs.py              # 文档 ↔ 代码一致性（CI 会拦）
python scripts/e2e_smoke.py               # CLI 级端到端冒烟：起真中继 + 真代理，经 SOCKS5 取数据
python torsocks5_cli.py selftest          # 离线端到端自检（SOCKS5 + meek + PT 握手）
```

改动中继实现时请参考 TSU/1 协议规范（`docs/tunnel-protocol.md`）并运行各自的测试与跨语言互通。

## 项目约定

* **运行期零第三方依赖**。CI 里有一条检查断言 `project.dependencies == []`，
  新增依赖前请先确认标准库做不到。
* **支持 Python 3.10+**，且要在 Windows / macOS / Linux 上都能跑。
  * 不要用 3.9+ 的内建泛型注解（`list[int]`）除非有 `from __future__ import annotations`
  * 不要依赖 `os.sys`（应 `import sys` 后用 `sys.platform`）
  * 输出中文/符号前确保 `torsocks5.log.force_utf8_output()` 已生效
    （Windows 控制台是本地代码页 cp936，直接打印 `✓`/`✗` 会 `UnicodeEncodeError`）。
    **不要各写一份**：CLI 与 `scripts/*.py` 共用这一个实现。
* **不要在日志里记录用户访问的域名**。当前只记录 `CONNECT host:port` 这一层信息。
* 注释和文档用中文，与现有代码保持一致。
* 文档不是可选项：改了 CLI 选项、配置项、错误码、路由名或测试数量，
  就要同步改 README / `docs/`，`scripts/check_docs.py` 会逐项核对
  （连「某个选项只在 `--help` 里存在」和「写了没链接的孤儿文档」都会拦）。

## 协议相关的改动要格外小心

项目里有**两套线协议**，都要求与既有实现兼容：

### 1. meek 传输（Tor 官方）

meek 传输与 Tor 官方的 [pluggable-transports/meek](https://git.torproject.org/pluggable-transports/meek.git)
**线协议必须兼容**。改动 `torsocks5/meek/channel.py` 或 `socks_server.py` 时：

1. 先读官方 `meek-client.go` 与 `meek-server.go` 确认行为
2. 补一个回归测试（`tests/test_meek.py` 或 `tests/test_http_transport.py`）
3. 如果是修 bug，**测试要先能复现原问题**

已知踩过的坑，都在 `tests/test_http_transport.py` 里有对应的回归用例
（例如 chunked 响应必须读到终止块，否则 keep-alive 连接会永久错位）。

### 2. TSU/1 隧道协议（路由 2 / 3）

`docs/tunnel-protocol.md` 是**唯一真相源**，Python 客户端与 Python 中继（`torsocks5/tunnel/`）按它编解码。
其它平台实现（Cloudflare Worker、Deno Deploy 等）需自行遵循该规范。因此：

1. **先改协议文档**，再改 Python 实现；只改一端的 PR 不会被接受。
2. Python 端要有对应测试（`tests/test_tunnel_*.py`），
   再加一次跨语言互通：`python scripts/interop_relay.py …`（连接自行部署的中继）。
3. 错误码、opcode、上限（64 KiB 消息 / 32 KiB 分片 / 1 MiB 背压）这些数字
   在文档与代码里必须一致——`check_docs.py` 会核对错误码与帧类型。

## 提交前自查

```bash
ruff check . && mypy torsocks5 \
  && python -m unittest discover -s tests \
  && python scripts/check_docs.py && python scripts/check_docs.py --selftest \
  && python scripts/e2e_smoke.py
```

全绿再提 PR。CI 会在 ubuntu / macos / windows × Python 3.8 / 3.12 上跑同一批测试，
外加中继实现的测试与跨语言互通验证。

## 报告问题

请附上：

* 操作系统与 Python 版本
* `torsocks5 doctor` 的完整输出
* `torsocks5 run -v` 的相关日志（**注意先删掉里面的网桥地址再公开**）
* `<数据目录>/tor.log` 里的相关片段

> 网桥行是给你个人用的，**不要贴到公开 issue 里**。

## 安全问题

发现安全问题请发邮件给仓库维护者，不要开公开 issue。
