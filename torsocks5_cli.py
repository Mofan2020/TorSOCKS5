#!/usr/bin/env python3
"""开发/源码安装时的命令行入口（``python torsocks5_cli.py run``）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from torsocks5.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
