#!/usr/bin/env python3
"""生成「本包未包含 tor 运行时」的说明文件。

单独做成脚本而不是在 workflow 里用 heredoc，是为了避开 YAML 块标量
剥离缩进后 heredoc 终止符不匹配的问题。

用法：python scripts/write_tor_readme.py <输出路径> [平台标签]
"""

import os
import sys

TEMPLATE = """# 本包未包含 tor 运行时

构建时未能从 Tor 官方站点下载 tor（通常是临时网络问题）。
TorSOCKS5 本身不需要编译，但**运行时需要系统里有 tor 可执行文件**。

## 方式 1：让程序自己下载（推荐）

```bash
{TOR} fetch-tor
```

下载完成后按提示把路径写进配置文件：

```toml
[tor]
binary = "<提示中的路径>"
```

## 方式 2：手动安装 tor

| 平台 | 命令 |
| --- | --- |
| macOS | `brew install tor` |
| Debian / Ubuntu | `sudo apt install tor` |
| Fedora / RHEL | `sudo dnf install tor` |
| Windows | 安装 [Tor Expert Bundle](https://www.torproject.org/download/tor-browser/)，或直接用 Tor Browser 自带的 `Tor/tor.exe` |
| 其他 | 从 <https://dist.torproject.org/> 下载对应平台的包 |

装好后验证：

```bash
{TOR} doctor
```

看到「找到 tor: ...」就说明可以正常使用了。

## 说明

本平台：{label}

Meek 传输是纯 Python 实现、随包提供；tor 是 Tor 官方发布的独立程序，
我们只负责在构建时下载并随包分发，不修改其内容。
"""


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: write_tor_readme.py <输出路径> [平台标签]", file=sys.stderr)
        return 2
    path = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else "未知平台"
    exe = "TorSOCKS5.exe" if os.name == "nt" else "./TorSOCKS5"
    content = TEMPLATE.replace("{TOR}", exe).replace("{label}", label)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    print("已写入 %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
