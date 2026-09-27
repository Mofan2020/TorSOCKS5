#!/usr/bin/env python3
"""meek 传输插件的独立入口（tor 通过 ``ClientTransportPlugin ... exec`` 调用它）。

用法::

    ClientTransportPlugin meek,meek_lite exec /path/to/python /path/to/meek_pt.py

tor 启动本脚本后，会在 stdin/stdout 上进行可插拔传输（PT）协议握手。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from torsocks5.meek.pt import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
