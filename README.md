# TorSOCKS5

**通过 meek 网桥连接 Tor 网络的 SOCKS5 代理 —— meek 传输用纯 Python 实现，跨平台、零第三方依赖。**

```
你的应用 ──SOCKS5──▶ TorSOCKS5 ──▶ tor 客户端 ──meek(HTTPS+CDN 域前置)──▶ meek 网桥 ──▶ Tor 网络
```

* 客户端只需要一个普通的 SOCKS5 代理地址（默认 `socks5h://127.0.0.1:9051`）
* 中间的 meek 传输**不需要 Go、不需要编译、不需要 Tor Browser**，本仓库用 Python 标准库实现
* Windows / macOS / Linux 全平台，支持源码运行、pip 安装、以及 CI 打包的单文件可执行程序

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [快速开始](#快速开始)
- [获取 meek 网桥](#获取-meek-网桥)
- [命令行详解](#命令行详解)
- [配置说明](#配置说明)
- [在各种应用里使用](#在各种应用里使用)
- [开机自启 / 服务化](#开机自启--服务化)
- [工作原理](#工作原理)
- [实测数据与限制](#实测数据与限制)
- [故障排查](#故障排查)
- [开发与测试](#开发与测试)
- [安全与合规](#安全与合规)
- [许可证](#许可证)

---

## 它解决什么问题

在受审查的网络里，Tor 的直连中继地址和端口很容易被识别并封锁，于是需要**网桥（bridge）**。
其中 **meek** 类网桥把 Tor 流量伪装成访问某个大型 CDN（微软、Cloudflare、jsDelivr…）的普通 HTTPS 请求：

```
你 ──TLS(SNI: cdn.jsdelivr.net)──▶ CDN ──Host: meek.example──▶ meek 网桥 ──▶ Tor 网络
    审查者看到：你在访问一个 CDN 的静态资源
```

普通用户的困难在于：官方只提供**预编译的 Tor Browser**，或者需要 Go 工具链自己编译
`meek-client` 插件。TorSOCKS5 把这件事变成「装个 Python 包，跑一条命令」。

## 快速开始

### 环境要求

| 项目 | 要求 |
| --- | --- |
| Python | 3.8 及以上（Windows / macOS / Linux） |
| tor | 需要一个可执行文件（多数系统可直接安装，见下） |
| 网络 | 能连通某个 CDN 前置域名 |

**安装 tor**

```bash
# macOS
brew install tor

# Debian / Ubuntu
sudo apt update && sudo apt install tor

# Windows：用官方 Tor Expert Bundle（或 Tor Browser 自带的那份）
# 安装后 torsocks5 会自动在常见位置找到它；
# 也可以 torsocks5 fetch-tor 自动下载，或在配置里写 tor.binary = "C:\\path\\to\\tor.exe"
```

### 三步跑起来

```bash
# 1. 获取（或安装）本项目
git clone https://github.com/Mofan2020/TorSOCKS5.git
cd TorSOCKS5
pip install -e .            # 可选，直接 python torsocks5_cli.py 也能跑

# 2. 自检：检查 Python / tor / 网桥 / 端口 / meek 隧道是否正常
torsocks5 doctor

# 3. 启动
torsocks5 run
```

启动成功后会看到：

```
代理已就绪

SOCKS5 地址: 127.0.0.1:9051
示例: curl -x socks5h://127.0.0.1:9051 https://ifconfig.me
```

**立刻验证**（另开一个终端）：

```bash
# 出口 IP 应该和你的真实 IP 不同
curl -x socks5h://127.0.0.1:9051 https://api.ipify.org; echo

# DNS 是否也走代理（socks5h:// 表示由代理解析域名）
curl -x socks5h://127.0.0.1:9051 https://www.dnsleaktest.com
```

> 首次启动需要引导（bootstrap）。**meek 很慢**：要下载约 7 MB 的目录信息，
> 在高延迟链路上可能需要 **10 分钟以上**。期间程序会实时显示进度：
> `[#########-----------------]  45% requesting_descriptors  Asking for relay descriptors`
>
> 默认等待 300 秒，想更久就显式指定：
> ```bash
> torsocks5 run --ready-timeout 1800
> ```
> 第二次启动会复用缓存，通常快得多。

### 不想装包？直接跑

```bash
python3 torsocks5_cli.py run
python3 torsocks5_cli.py doctor
python3 torsocks5_cli.py selftest
```

### 用打包好的可执行程序（Windows / macOS / Linux）

从 [Releases](https://github.com/Mofan2020/TorSOCKS5/releases) 下载对应平台的压缩包，解压后：

```bat
:: Windows
TorSOCKS5.exe doctor
TorSOCKS5.exe run
```

```bash
# macOS / Linux
chmod +x TorSOCKS5-linux-x86_64   # 或 TorSOCKS5-macos-arm64
./TorSOCKS5-linux-x86_64 run
```

## 获取 meek 网桥

meek 网桥**不会**自动出现在公共列表里（那正是它抗封锁的意义），需要你主动获取。
本项目**不内置任何可用的网桥地址**，请按下面任一方式获取自己的。

### 方式 A：Tor 官方桥分发（推荐）

1. 打开 <https://bridges.torproject.org/>
2. 「Get Bridges」→ 选择 **Meek** → 填邮箱 / 解决验证码
3. 复制得到的那一行，形如：

   ```
   Bridge meek 0.0.2.0:3 url=https://xxxx.rsc.cdn77.org front=www.example.com utls=HelloRandomizedALPN
   ```

4. 导入本项目：

   ```bash
   torsocks5 bridges add "Bridge meek 0.0.2.0:3 url=https://xxxx.rsc.cdn77.org front=www.example.com"
   ```

   或者直接从剪贴板/文件导入：

   ```bash
   torsocks5 bridges import --clipboard      # macOS 用 pbpaste，Linux 用 xclip/wl-paste
   torsocks5 bridges import --file bridges.txt
   torsocks5 bridges import --url https://example.com/my-bridges.txt
   ```

> 想通过邮件获取：发邮件到 `bridges@torproject.org`，主题留空，正文写 `get transport meek`。

### 方式 B：复用已有的 tor 配置

如果你机器上已经能用的 tor 配置里有 meek 网桥，直接抄过来：

```bash
grep '^Bridge meek' ~/.tor/torrc
torsocks5 bridges import --file <(grep '^Bridge meek' ~/.tor/torrc)
```

### 验证网桥是否可用

```bash
# 只测这一条网桥能不能引导成功（会真的启动 tor）
torsocks5 bridges test "Bridge meek 0.0.2.0:3 url=... front=..."
```

网桥管理的完整命令：

```bash
torsocks5 bridges list                 # 列出全部
torsocks5 bridges add "Bridge meek ..."  # 新增（可重复传多条）
torsocks5 bridges rm cdn77.org         # 按子串删除
torsocks5 bridges clipboard            # 打印剪贴板内容
torsocks5 bridges normalize "..."      # 校验并规范化一行网桥配置
```

网桥保存在 `bridges.toml`（与配置文件同目录），格式是普通 TOML，可以手工编辑。

## 命令行详解

```
torsocks5 [全局参数] <子命令> [子命令参数]

全局参数
  --config PATH     指定配置文件（默认见下）
  --log-file PATH   同时把日志写入文件
  -v, --verbose     详细日志（含 tor 与 meek 内部事件）
  -V, --version     版本号
```

### `run` — 启动代理

```bash
torsocks5 run                          # 用配置文件的默认值
torsocks5 run --port 1080              # 换端口
torsocks5 run --listen 0.0.0.0         # 监听所有网卡（务必同时配置 allow_from 与认证！）
torsocks5 run --bridge "Bridge meek ..."  # 临时追加网桥，不写进配置文件
torsocks5 run --no-bridge              # 不用网桥，直连 Tor（网络没被墙时更快）
torsocks5 run --upstream 127.0.0.1:9050  # 复用已有的 tor，只做 SOCKS5 转发
torsocks5 run --tor /opt/homebrew/bin/tor  # 指定 tor 路径
torsocks5 run --ready-timeout 600      # 引导超时（秒），默认 300
torsocks5 run --keep-going             # 引导失败也继续监听
```

### `doctor` — 环境自检

依次检查：Python 版本、tor 可执行文件与是否内置 meek、插件启动路径是否含空格、
网桥配置是否合法、代理端口是否被占用、CDN 前置域名能否连通、本地 SOCKS5 回环自检。

```bash
torsocks5 doctor
torsocks5 doctor --port 1080
```

### `selftest` — 完全离线的自检

不联网、不需要 tor，验证本机各层逻辑（用于排除安装问题）：

```
[1/5] SOCKS5 服务端（免认证 / 用户名密码 / 错误密码拒绝）
[2/5] UDP ASSOCIATE
[3/5] meek 隧道（HTTP 往返 / 域前置 / 大包分片）
[4/5] 可插拔传输协议握手（模拟 tor 启动插件）
[5/5] tor 可执行文件
```

### `fetch-tor` — 下载 tor 官方专家包

```bash
torsocks5 fetch-tor                              # 自动挑选平台对应的包
torsocks5 fetch-tor --version 0.4.8.12
torsocks5 fetch-tor --file ~/Downloads/tor-expert-bundle-....tar.gz
torsocks5 fetch-tor --mirror https://mirror.example.org
```

下载完成后会提示把路径写进配置：

```toml
[tor]
binary = "/path/to/tor"
```

### `config` / `install-service`

```bash
torsocks5 config init        # 生成带注释的配置模板
torsocks5 config show        # 打印当前生效配置
torsocks5 config show --format json
torsocks5 config path

torsocks5 install-service              # 按平台自动选择，只打印不落盘
torsocks5 install-service systemd      # 生成 systemd user unit
torsocks5 install-service launchd      # 生成 launchd plist
torsocks5 install-service schtasks     # 生成 Windows 计划任务
torsocks5 install-service systemd --apply   # 真正写入
```

## 配置说明

配置文件位置（`torsocks5 config path` 可查）：

| 平台 | 默认路径 |
| --- | --- |
| macOS | `~/Library/Application Support/torsocks5/config.toml` |
| Linux | `~/.config/torsocks5/config.toml` |
| Windows | `%APPDATA%\torsocks5\config.toml` |

完整示例见 [`torsocks5/config.example.toml`](torsocks5/config.example.toml)。常用项：

```toml
[proxy]
listen = "127.0.0.1"        # 监听地址；对外暴露务必改 0.0.0.0 并加认证
port = 9051
username = ""               # 非空则启用 RFC 1929 认证
password = ""
allow_from = ["127.0.0.1", "::1"]   # 访问控制，按顺序第一条命中生效
udp_associate = true
max_connections = 512
idle_timeout = 300          # 空闲超时（秒）

[tor]
# binary = ""               # 留空自动探测
# direct = false            # true = 不用网桥直连 Tor
meek_mode = "plugin"        # plugin = 本项目的 Python 传输（默认）
log_level = "notice"        # err/warn/notice/info/debug
restart = true              # tor 意外退出自动重启
avoid_disk_writes = false   # true = 不落盘（更干净，但每次都要重下 geoip）
isolation = ["IsolateSOCKSAuth", "IsolateClientProtocol"]  # 流量隔离
# extra_options = ["TorCircuitBuildTimeout 120"]

[meek]
methods = ["meek", "meek_lite", "meek_azure"]  # 兼容不同网桥行的写法
connect_timeout = 20.0
read_timeout = 30.0
verbose = false             # true = 打印通道吞吐统计，排障很有用

[bridges]
builtin = false             # 无网桥时是否回落到内置的公开网桥（默认不回落）
```

> **关于 `meek_mode`**
> * `plugin`（默认）：用本项目内置的 Python meek 传输，任何 tor 版本都能用。
> * `builtin`：用 tor 自己编译进去的 meek（需要你的 tor 带 meek 支持），此时不启动 Python 插件。
> * `off`：完全不注册传输插件，配合 `direct = true` 使用。

## 在各种应用里使用

```bash
# curl（socks5h = 域名也交给代理解析，避免本地 DNS 泄漏）
curl -x socks5h://127.0.0.1:9051 https://ifconfig.me

# 开启了认证时
curl -x socks5h://user:pass@127.0.0.1:9051 https://ifconfig.me

# git
git config --global http.proxy socks5h://127.0.0.1:9051
git clone https://github.com/Mofan2020/TorSOCKS5.git

# pip
pip config set global.proxy socks5h://127.0.0.1:9051
# 或临时：PIP_PROXY=socks5h://127.0.0.1:9051 pip install xxx

# SSH（通过 SOCKS5 代理，注意用 nc 或 connect）
ssh -o ProxyCommand='nc -X 5 -x 127.0.0.1:9051 %h %p' user@host

# Node.js
npm config set proxy socks5h://127.0.0.1:9051

# 浏览器：设置 → 网络 → 代理 → SOCKS v5，填 127.0.0.1 9051
# 「通过 SOCKS 解析主机名」要勾上，否则 DNS 仍走本地
```

系统级（macOS / Linux）：

```bash
# macOS：在「网络 → 高级 → 代理」里勾选 SOCKS 代理，填 127.0.0.1 9051
# Linux：设置 GNOME/ KDE 的网络代理，或 export all_proxy=socks5h://127.0.0.1:9051
```

## 开机自启 / 服务化

```bash
# macOS（launchd，用户级）
torsocks5 install-service launchd --apply
launchctl load -w ~/Library/LaunchAgents/com.torsocks5.agent.plist
tail -f ~/Library/Logs/torsocks5.log

# Linux（systemd，用户级，无需 root）
torsocks5 install-service systemd --apply
systemctl --user daemon-reload
systemctl --user enable --now torsocks5
journalctl --user -u torsocks5 -f

# Windows（计划任务，登录时启动；用管理员运行可改成开机启动）
torsocks5 install-service schtasks --apply
schtasks /Run /TN TorSOCKS5
```

也可以直接用 Docker：

```bash
docker build -t torsocks5 .
docker run -d --name torsocks5 -p 9051:9051 -v torsocks5-data:/data torsocks5
docker exec torsocks5 torsocks5 doctor
```

## 工作原理

### 分层结构

```
torsocks5/
├── cli.py                 命令行入口（run / doctor / bridges / config / fetch-tor / selftest）
├── config.py              TOML 配置（3.11+ 用 tomllib，低版本用内置精简解析器）
├── bridges.py             网桥行解析、校验、去重、存储
├── log.py                 跨平台彩色日志
├── selftest.py            离线自检
├── service.py             systemd / launchd / schtasks 配置生成
├── socks5/
│   ├── protocol.py        RFC 1928 / 1929 编解码
│   ├── client.py          SOCKS5 客户端（连上游 tor 用）
│   └── server.py          SOCKS5 服务端（对外提供，支持 ACL / UDP / 认证）
├── tor/
│   ├── find.py            跨平台查找 tor 可执行文件
│   ├── control.py         控制端口客户端（查进度、优雅关闭）
│   └── manager.py         torrc 生成、进程管理、自动重启、无空格路径 shim
└── meek/
    ├── channel.py         meek 隧道核心：HTTP 轮询 + 域前置
    ├── socks_server.py    传输插件侧的 SOCKS5 服务端（接收 tor 的 CONNECT 与网桥参数）
    ├── pt.py              可插拔传输（PT）协议前端
    └── mock_server.py     测试用 meek 服务端（与官方 meek-server 协议等价）
```

### meek 隧道协议

与 Tor 官方 `meek`（[pluggable-transports/meek](https://git.torproject.org/pluggable-transports/meek.git)）
**协议兼容**：

| 方向 | 内容 |
| --- | --- |
| 请求 | `POST <url>`，`Content-Type: application/octet-stream`，`X-Session-Id: <会话 id>`，请求体是原始 Tor 字节（单次 ≤ 65536） |
| 响应 | `200 OK`，`Content-Type: application/octet-stream`，响应体是回传的 Tor 字节 |
| 会话 | 8 字节随机数 base64（去 `=`）作为 `X-Session-Id`，把一条 TCP 流拆成多个 HTTP 请求 |
| 轮询 | 空闲时 100ms 起、×1.5 退避、最长 5s；一旦有数据收发立刻再发 |
| 域前置 | DNS/TCP/TLS SNI 用 `front=`，HTTP `Host` 头用 `url=` 的主机，由 CDN 转发到网桥 |

网桥行里常见的参数都被支持：`url=`（必需）、`front=`（含旧写法 `CDNFronting=`）、
`utls=`（识别但忽略，见下）、`fragments=`、`ignorePQC`。

> **关于 `utls=`**
> 官方客户端用 Go 的 uTLS 库模拟浏览器 TLS 指纹。Python 标准库的 `ssl` 模块做不到这件事，
> 本项目**不模拟浏览器指纹**，而是使用 OpenSSL 的真实指纹。
> 在只做「TLS 被动识别」的网络里这通常无影响；若你的审查者做主动指纹识别，
> 请优先使用 Tor Browser（它自带官方 uTLS 客户端），或改用 snowflake 等传输。

### 与 tor 的集成方式

本项目以**可插拔传输插件（ClientTransportPlugin）**的形式接入 tor：

```
ClientTransportPlugin meek,meek_lite,meek_azure exec /usr/bin/python3 /path/to/meek_pt.py
```

tor 启动 `meek_pt.py` 后走 PT 行协议握手（`VERSION` / `CMETHOD` / `AUTHENTICATE` / `PROXY`），
然后连接插件公布的 SOCKS5 地址，用 RFC 1929 的 username/password 字段把网桥参数传进来。
这些参数由 `parse_pt_args()` 解析，兼容 `;` 与空格两种分隔写法。

**已知实现细节**：tor 解析 `ClientTransportPlugin` 时按空格朴素切分参数，不处理引号或转义。
因此当安装路径含空格时，`tor.plugin_command()` 会自动生成一个**无空格的启动脚本**
（POSIX 下是 `#!/bin/sh` 包装脚本，Windows 下是 `cmd.exe /c x.cmd`），
放在 `/tmp/torsocks5-pt-$UID/`（或 `%WINDIR%\Temp\torsocks5-pt`）再交给 tor。
`doctor` 会检查这一点。

### torrc 要点

生成的 torrc 位于 `<数据目录>/torrc`：

```
DataDirectory <独立目录>          # 不干扰系统里已有的 tor
SocksPort 127.0.0.1:<随机端口> IsolateSOCKSAuth IsolateClientProtocol
ControlPort 127.0.0.1:<随机端口>  # Cookie 认证
ClientOnly 1 / SafeLogging 1
UseBridges 1
Bridge meek 0.0.2.0:3 url=... front=...
__OwningControllerProcess <pid>     # 控制器退出时自动关闭 tor（POSIX）
```

## 实测数据与限制

在一条受审查网络（客户端在中国大陆）上，用真实 meek 网桥
（`url=https://*.rsc.cdn77.org` `front=www.example.com`）实测：

| 指标 | 观测值 |
| --- | --- |
| PT 握手 | tor 加载本项目插件后 1 秒内到达 `2% conn_done_pt` |
| 与网桥完成 Tor 握手 | 约 5 秒（`15% handshake_done`） |
| 隧道吞吐 | 单连接约 **25～50 KB/s**（64KB/次往返，取决于 CDN 延迟） |
| 连接稳定性 | 长跑 20+ 分钟、传输 7 MB+ 数据，**0 次通道错误** |
| CDN 错误率 | 约 15～20% 的请求被 CDN 注入 `HTTP 570`（短退避重试即可） |
| 微描述符下载 | 约 2.4 MB/12 分钟（9458 个中的 2378 个） |

**必须知道的限制**：

1. **meek 很慢，这是协议本身的特性，不是本项目的实现问题。**
   每 64KB 数据都要一个完整 HTTP 往返，吞吐上限就是 `64KB / RTT`。
   在高延迟链路上实测约 25 KB/s。浏览网页尚可，**大文件下载会非常慢**。
   **需要速度请用 obfs4 / snowflake / webtunnel**（本项目对这类网桥行是透传支持，
   在配置里加上对应的传输插件即可）。
2. **首次引导可能需要 10 分钟以上。** meek 首次要下载权威共识（~1MB）
   与中继微描述符（9458 个，~6MB），实测 12 分钟只下了 25%。
   之后重启会复用缓存，快得多。默认 `--ready-timeout` 为 300 秒，
   meek 环境下建议显式调大：

   ```bash
   torsocks5 run --ready-timeout 1800
   ```

   想跳过等待、直接开始监听（请求会暂时失败）：

   ```bash
   torsocks5 run --ready-timeout 0 --keep-going
   ```

3. **网桥会失效。** 前置 CDN 可能被封或下线。表现是 4xx（快速失败并提示换桥）
   或长时间停在某个进度。
4. **UDP 只到 tor 这一层。** 代理的 UDP ASSOCIATE 会转发给 tor，
   但 meek 隧道本身只承载 TCP，UDP 流量无法穿透 meek。
5. **不是浏览器级指纹伪装。** 见上文 `utls=` 说明。

## 故障排查

### `doctor` 报错对照

| 现象 | 原因与处理 |
| --- | --- |
| 找不到 tor | 装 tor，或在配置写 `tor.binary`，或 `torsocks5 fetch-tor` |
| 插件命令中仍有空格 | 移动到无空格目录，或设置 `tor.pt_shim_dir` |
| 没有启用任何网桥 | `torsocks5 bridges add "..."` 导入自己的网桥行 |
| 端口已被占用 | `torsocks5 run --port 1080` |
| 连不上 CDN 前置域名 | 这正是需要 meek 的原因；若你本就不受限制，可用 `--no-bridge` 直连 |

### 引导卡住

```bash
# 打开详细日志，能看到每个引导阶段与插件内部事件
torsocks5 run -v
# 打开通道吞吐统计
#   在配置里设 [meek] verbose = true

# 查看 tor 自己的日志
cat "$(torsocks5 config path | xargs dirname)/../Caches/torsocks5/tor.log"
# 或直接看本次运行的 torrc 同目录下的 tor.log
```

常见卡点：

| 卡在 | 含义 | 处理 |
| --- | --- | --- |
| `0%` 一直不动 | 插件没起来，或网桥连不上 | 看 `-v` 日志里有没有 `CMETHOD`；换网桥 |
| `2% conn_done_pt` | 已连上传输插件，在等网桥响应 | 网桥后端可能已下线，换桥 |
| `30% loading_status` | 正在下权威共识（约 1MB） | 耐心等，或提高 `--ready-timeout` |
| `50% ~ 99% loading_descriptors` | 正在下 9458 个中继微描述符（约 6MB，**最慢**） | 这是正常现象；meek 上可能要 10～30 分钟 |

> 引导期间 tor 日志里刷屏的 `Delaying directory fetches (no running bridges known)`
> 也是正常现象：meek 链路太慢，tor 会短暂认为桥连接不健康而暂缓拉取，
> 随后自动继续。判断是否真的卡住，看插件的吞吐统计（配置 `[meek] verbose = true`）：
> 只要「接收」字节数还在涨，就说明隧道是活的。

### 4xx / 5xx 状态码含义

程序对 HTTP 状态做了分级处理，日志里会直接说明：

* **403 / 404**：请求被 CDN 或网桥拒绝 —— **这条网桥不可用**，程序会立刻失败并提示换桥
  （官方客户端在这里会傻等 30 秒 × 10 次，本项目做了优化）
* **500 / 502 / 570**：CDN 或网桥的临时错误 —— 短退避（0.5→1→2→4→5 秒）后重试
* **连接中断 / 状态行异常**：CDN 掐断了 keep-alive 连接 —— 自动重连重试（最多 20 次）

### 排障命令速查

```bash
torsocks5 selftest                       # 离线自检，排除本地安装问题
torsocks5 doctor                         # 环境体检
torsocks5 bridges test "Bridge meek ..."  # 单独验证某条网桥
torsocks5 run --upstream 127.0.0.1:9050   # 你的 tor 已经能用了？只借它的 SOCKS 端口
python3 torsocks5_cli.py -v run          # 源码直接跑 + 详细日志
```

## 开发与测试

```bash
git clone https://github.com/Mofan2020/TorSOCKS5.git
cd TorSOCKS5
pip install -e ".[dev]"

# 单元测试（63 个用例，全部离线）
python -m unittest discover -s tests -v

# 离线端到端自检
python torsocks5_cli.py selftest

# 静态检查
ruff check .
mypy torsocks5

# 本地打包
pip install pyinstaller && pyinstaller torsocks5.spec
```

测试分层：

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/test_meek.py` | meek 通道：HTTP 往返、域前置、keep-alive、大包分片、TLS、参数解析 |
| `tests/test_http_transport.py` | HTTP 层健壮性：**响应体错位回归**、chunked、截断、超长响应 |
| `tests/test_pt.py` | PT 协议：握手、多传输名、参数传递、缺失 url 的处理 |
| `tests/test_core.py` | SOCKS5 服务端/客户端、ACL、认证、网桥解析、配置与 TOML 解析器 |

CI（GitHub Actions）覆盖三平台 × 多 Python 版本、静态检查、
以及一个**集成测试**：用 mock meek 网桥 + 真实 tor 验证「tor 能通过纯 Python 插件完成 PT 握手」。

## 安全与合规

* 本项目只做**流量转发**，不记录访问内容、不做中间人、不注入任何内容。
* 默认只监听 `127.0.0.1` 且默认免认证。**若要监听 `0.0.0.0`，请务必设置
  `username`/`password` 和 `allow_from`**，否则你的代理会对整个网络开放，成为严重的安全问题。
* 日志里不会记录你访问的域名（只有 `CONNECT host:port` 的连接级信息），
  且已给 tor 开了 `SafeLogging`。
* 请遵守所在地法律法规与 Tor 官方使用条款。Meek 网桥由 Tor 志愿者提供，
  滥用会导致网桥被封禁，从而影响真正需要它的人。
* 官方提供的网桥行是**给你自己用的**，不要公开分享。

## 许可证

[MIT](LICENSE) © 2026 TorSOCKS5 contributors

协议实现参考了 Tor 官方项目
[pluggable-transports/meek](https://git.torproject.org/pluggable-transports/meek.git)（BSD 3-Clause），
本仓库为独立实现，遵循同样的线协议以保证互操作。
