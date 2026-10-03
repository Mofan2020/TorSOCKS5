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
- **Web management panel** (`torsocks5/web/`): `torsocks5 run --web` or `[web] enabled = true`. Pages: Dashboard (live connections/traffic/route status), Routes (one-click hot switch + relay node health), Bridges (add/remove), Config (TOML editor: validate → save → hot reload), Logs (SSE live stream with startup history). Auth: Basic Auth credentials in config file; write operations verify `Origin` against CSRF. Stack: stdlib `http.server` + CDN Tailwind/Alpine/htmx — zero build, zero runtime dependencies.
- **Version update check** (`torsocks5/version_check.py`): `run` queries GitHub Releases in a background thread (3s timeout, results cached 24h, never blocks startup). New version → one-line startup log hint + amber banner on the Web dashboard (`/api/status` → `update`). Network failures are completely silent — no log, no retry, no impact on usage (designed for restricted networks). Disable via `[version_check] enabled = false`; `TORSOCKS5_VERSION_CHECK_URL` / `TORSOCKS5_VERSION_CHECK_CACHE` env vars override endpoint and cache path.
- **GitHub requests skip SSL certificate verification by default**: accelerator/proxy setups in mainland China commonly present untrusted certificates, which would break the version check exactly where it matters most. Default is `verify_ssl = false`; set `[version_check] verify_ssl = true` to enforce strict validation.
- **In-panel configuration wizard** (`torsocks5/web/wizard.py`, page `/wizard`): a "配置向导" button in the panel header opens a 5-step guided setup — environment detection (with actionable fix hints) → basic config (surgical TOML edits that preserve comments, validated before atomic write, hot-reloaded) → exit & bridges (one-click on-demand bridge request with email/web fallback guidance) → startup service (generates launchd/systemd/Windows Task Scheduler config with step-by-step install and uninstall instructions) → summary.
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

## [2.0.1] - 2026-10-04

### Added
- **Web panel enabled by default**: `torsocks5 run` now starts the Web UI automatically (127.0.0.1:9054). New users see the dashboard URL and random password in the startup log. Use `--no-web` to disable or set `[web] enabled = false` in config.
- **`--no-web` CLI flag**: Override config to force-disable the panel.
- **Restored `deploy/cloudflare/`**: Cloudflare Worker TSU/1 relay source code back in repo for self-deployment reference (was removed in v2.0.0 BREAKING, now restored as documentation/example).

### Changed
- **meek TLS fingerprint hardening** (`torsocks5/meek/channel.py`): Standard-library fallback now uses Chrome 120 cipher suite order, explicit ALPN http/1.1, TLS 1.2/1.3 only, KTLS where available, compression disabled. Optional `utls-python` integration point reserved for full browser fingerprint (PyPI package not yet available; interface ready).

### Fixed
- Windows CI tests: TOML path escaping for backslash-containing temp paths (`test_web.py`, `test_wizard.py`).
- Release build: aarch64 tor expert bundle now downloads from alpha channel (16.0a12) since stable never publishes aarch64.
- Spec: removed stale `deploy/` reference from `torsocks5.spec` (caused PyInstaller failures).

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