# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-10-03

### ⚠️ BREAKING CHANGES

- **Removed Docker support**: Deleted `Dockerfile`, `docker-entrypoint.sh`, `.dockerignore`, and the entire `deploy/` directory (Cloudflare Worker and Deno Deploy deployment files). Users who relied on Docker images must now use the standalone executables from Releases or run from source.
- **Minimum Python version raised to 3.10**: Dropped support for Python 3.8 and 3.9. This enables use of modern standard library features (e.g., `tomllib`, `zoneinfo`, improved `asyncio`).
- **Configuration format for multiple relays**: The `[self_relay]` and `[cf_relay]` sections now support a new `[[nodes]]` array format for multiple relay configuration. The old single `url`/`token` format is still supported for backward compatibility but will be deprecated.
- **Removed `deploy/` directory from release artifacts**: Release packages no longer include Cloudflare Worker or Deno Deploy deployment files. Users must maintain their own deployment configurations.

### Added

- **Structured logging with rotation** (`torsocks5/logging_structured.py`): JSON Lines format, configurable rotation by size/time, sensitive field redaction (tokens, passwords, bridge URLs, client IPs).
- **Configuration hot reload** (`torsocks5/hotreload.py`): Trigger via `SIGHUP` signal or HTTP API (`POST /api/config/reload` with Basic Auth). Supports live updates to proxy port, routing, split rules, authentication, and relay settings without restart.
- **Multi-relay load balancing with circuit breaker** (`torsocks5/tunnel/balancer.py`): Configure multiple relay nodes with weights, health checks, and automatic failover. Strategies: weighted round-robin, least connections + latency-aware (EWMA), and split-rule binding (domain/port → specific relay).
- **Relay server enhancements** (`torsocks5/tunnel/relay.py`): Token bucket rate limiting (global/per-client/per-token), connection limits per IP/token, IP allowlist/blocklist (CIDR), structured access logs, circuit breaker on upstream errors.
- **Bridge request from torproject.org** (`torsocks5/bridge_fetch.py`): New CLI command `torsocks5 bridges fetch` to request meek bridges via email/HTTPS (on-demand, failure doesn't affect operation).

### Changed

- **PyInstaller spec updated**: Release builds now target Python 3.10+.
- **CI matrix updated**: Tests run on Python 3.10 and 3.12 (dropped 3.8).
- **Release workflow updated**: Removed deploy directory copying from release artifacts.
- **Documentation updated**: Removed Docker and deploy references from README and docs.

### Fixed

- Various type hints improvements for mypy 3.10+.
- Ruff target version updated to py310.

---

## [1.1.0] - 2026-09-28

### Added
- Three routing modes: `tor-meek`, `cf-relay`, `self-relay`
- Pure Python meek transport (pluggable transport compatible)
- TSU/1 tunnel protocol (WebSocket multiplexing)
- Smart split routing (China direct, learning sites via proxy)
- Bridge management (add/import/test/normalize)
- Self-test suite (offline SOCKS5 + meek + PT handshake)
- Service installation (systemd, launchd, schtasks)
- Tor Expert Bundle downloader (`fetch-tor`)
- Cross-language interop tests (Python client ↔ Deno/Worker relay)

### Changed
- Zero runtime dependencies (stdlib only)
- Configuration via TOML with fallback parser for Python < 3.11

---

## [1.0.0] - 2026-09-20

### Added
- Initial release: SOCKS5 proxy over Tor with meek bridges
- Basic CLI: run, doctor, bridges, config, selftest