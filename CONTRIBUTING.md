# 贡献指南

感谢关注。这个项目不大，代码结构清晰，欢迎各种形式的贡献。

## 开发环境

```bash
git clone https://github.com/Mofan2020/TorSOCKS5.git
cd TorSOCKS5
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

ruff check .          # 静态检查
mypy torsocks5        # 类型检查
python -m unittest discover -s tests -v   # 测试
python torsocks5_cli.py selftest          # 离线端到端自检
```

## 项目约定

* **运行期零第三方依赖**。CI 里有一条检查断言 `project.dependencies == []`，
  新增依赖前请先确认标准库做不到。
* **支持 Python 3.8+**，且要在 Windows / macOS / Linux 上都能跑。
  * 不要用 3.9+ 的内建泛型注解（`list[int]`）除非有 `from __future__ import annotations`
  * 不要依赖 `os.sys`（应 `import sys` 后用 `sys.platform`）
  * 输出中文/符号前确保 `_force_utf8_output()` 生效（Windows 控制台是本地代码页）
* **不要在日志里记录用户访问的域名**。当前只记录 `CONNECT host:port` 这一层信息。
* 注释和文档用中文，与现有代码保持一致。

## 协议相关的改动要格外小心

meek 传输与 Tor 官方的 [pluggable-transports/meek](https://git.torproject.org/pluggable-transports/meek.git)
**线协议必须兼容**。改动 `torsocks5/meek/channel.py` 或 `socks_server.py` 时：

1. 先读官方 `meek-client.go` 与 `meek-server.go` 确认行为
2. 补一个回归测试（`tests/test_meek.py` 或 `tests/test_http_transport.py`）
3. 如果是修 bug，**测试要先能复现原问题**

已知踩过的坑，都在 `tests/test_http_transport.py` 里有对应的回归用例
（例如 chunked 响应必须读到终止块，否则 keep-alive 连接会永久错位）。

## 提交前自查

```bash
ruff check . && mypy torsocks5 && python -m unittest discover -s tests
```

三项都通过再提 PR。CI 会在 ubuntu / macos / windows × Python 3.8 / 3.12 上跑同一批测试。

## 报告问题

请附上：

* 操作系统与 Python 版本
* `torsocks5 doctor` 的完整输出
* `torsocks5 run -v` 的相关日志（**注意先删掉里面的网桥地址再公开**）
* `<数据目录>/tor.log` 里的相关片段

> 网桥行是给你个人用的，**不要贴到公开 issue 里**。

## 安全问题

发现安全问题请发邮件给仓库维护者，不要开公开 issue。
