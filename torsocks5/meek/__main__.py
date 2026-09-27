"""``python -m torsocks5.meek`` 入口：作为 tor 的 meek 传输插件运行。"""

import sys

from .pt import main

if __name__ == "__main__":
    sys.exit(main())
