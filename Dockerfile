# TorSOCKS5 —— 镜像（零第三方依赖，运行时只需要标准库）
#
#   docker build -t torsocks5 .
#   docker run -d --name torsocks5 -p 9051:9051 -v torsocks5-data:/data torsocks5
#
# 容器内不含 tor 运行时二进制（各平台差异大），默认使用「直连 Tor」模式。
# 若需要经 meek 网桥，请把 tor 与网桥行挂载进来：
#   docker run -d -p 9051:9051 \
#     -v torsocks5-data:/data \
#     -v /opt/homebrew/bin/tor:/usr/local/bin/tor:ro \
#     torsocks5 bridges add "Bridge meek 0.0.2.0:3 url=... front=..."
#   docker start torsocks5

FROM python:3.12-slim

LABEL org.opencontainers.image.title="TorSOCKS5" \
      org.opencontainers.image.description="SOCKS5 proxy over Tor via meek bridges" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TORSOCKS5_CONFIG=/config/config.toml

WORKDIR /app

COPY torsocks5/ ./torsocks5/
COPY meek_pt.py torsocks5_cli.py README.md LICENSE ./

RUN python -m compileall -q torsocks5 && \
    useradd --create-home --shell /bin/bash --uid 10001 socks && \
    mkdir -p /config /data && chown -R socks:socks /config /data /app

COPY --chmod=0644 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

USER socks
VOLUME ["/config", "/data"]
EXPOSE 9051/tcp

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python /app/torsocks5_cli.py --config /config/config.toml doctor --port 9051 || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["run"]
