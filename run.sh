#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键启动脚本（macOS / Linux）。

用法：
    ./run.sh                    # 用默认配置
    ./run.sh --port 1080        # 透传参数
    ./run.sh doctor             # 自检
"""

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON="$candidate"
            break
        fi
    done
fi

if [ -z "$PYTHON" ]; then
    echo "错误：找不到 Python 3，请先安装 Python 3.8 或更高版本。" >&2
    exit 1
fi

VERSION_OK=$("$PYTHON" -c 'import sys; print(1 if sys.version_info >= (3, 8) else 0)')
if [ "$VERSION_OK" != "1" ]; then
    echo "错误：Python 版本过低（$("$PYTHON" -V 2>&1)），需要 3.8+。" >&2
    exit 1
fi

if [ $# -eq 0 ]; then
    set -- run
fi

exec "$PYTHON" "$SCRIPT_DIR/torsocks5_cli.py" "$@"
