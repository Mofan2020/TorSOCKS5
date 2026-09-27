#!/bin/sh
# Docker 容器入口：首次启动时生成默认配置，然后转发参数给 CLI。
set -e

CONFIG_DIR="${TORSOCKS5_CONFIG_DIR:-/config}"
CONFIG_FILE="$CONFIG_DIR/config.toml"

mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "[entrypoint] 生成默认配置: $CONFIG_FILE"
    cat > "$CONFIG_FILE" <<'EOF'
[proxy]
listen = "0.0.0.0"
port = 9051
username = ""
password = ""
allow_from = ["0.0.0.0/0", "::/0"]
udp_associate = true

[tor]
# 容器里没有 tor，请挂载一个进来并写明路径，例如：
#   binary = "/usr/local/bin/tor"
meek_mode = "plugin"
log_level = "notice"
# 容器内无法使用 __OwningControllerProcess 之外的宿主功能，保持默认
direct = true

[meek]
verbose = false

[bridges]
builtin = false
EOF
    echo "[entrypoint] 提示：容器内默认直连 Tor（direct = true）。"
    echo "[entrypoint]      若要走 meek 网桥，请挂载 tor 并执行："
    echo "[entrypoint]        docker exec <容器> torsocks5 bridges add \"Bridge meek ...\""
fi

exec python /app/torsocks5_cli.py --config "$CONFIG_FILE" "$@"
