# Changelog

All notable changes to the Neuro-Cognitive Engine (`nce`) project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [3.3.0] - 2026-11-10

### Added
- **Streamable HTTP `/mcp` Gateway & Zero-Secret Client Proxy:** Introduced high-throughput SSE / JSON-RPC 2.0 streaming HTTP `/mcp` gateway and companion `nce.mcp_client_proxy` (`nce-mcp-proxy`), eliminating raw database credentials (`PG_DSN`, `MONGO_URI`) and `NCE_MASTER_KEY` from client workstation configurations.
- **Progressive Tool Discovery & Token Scoping:** Added progressive schema exposure (`compact` single meta-tool `nce_discover_tools` vs `full`) and fine-grained dual-axis token restrictions (`type:*`, `cat:*`, explicit engine slugs).
- **`/admin/plugins` Scoped Token Minter:** Added administrative endpoint (`POST /admin/plugins/api/mint-token`) and interactive UI modal for minting scoped bearer tokens and ready-to-copy desktop client configurations.
- **KEK/DEK Envelope Encryption & Cryptographic Shredding (`nce/crypto/envelope.py`):** Added hierarchical key management with versioned KEK rings, KMS integration hooks, and per-subject AES-256-GCM DEK shredding with RFC 8785 canonical receipts.
- **Database Migration `035_mcp_gateway_and_envelope_crypto.sql`:** Created `mcp_client_tokens` and `encryption_deks` with Row-Level Security, along with namespace embedding dimension/provider pinning and replay sandbox flags.
- **Contributor License Agreement (`CLA.md`):** Formally codified project contributor intellectual property, copyright, and patent licensing terms.

### Changed
- **Fail-Closed Governance Semantics:** Standardized tool and engine governance across Redis dispatches to fail closed (`-32005`) in production when Redis is unreachable.
- **Hard-Blocked Admin Override in Production:** Hardened `NCE_ADMIN_OVERRIDE` to raise `RuntimeError` at boot if set in production environments.
- **Repo Hygiene & Dead Code Removal:** Safely pruned obsolete `nce-launch/` duplicate Go module directory.

---

## [3.1.0] - 2026-11-10

### Added
- **Domain-Agnostic Plugin Retention Hooks (`nce/cron.py`):** Added scheduled background discovery and invocation of plugin retention hooks (`run_plugin_retention_hooks`) without hardcoded domain couplings.
- **Zero-PII Plugin Execution Metrics (`nce/plugins.py`):** Implemented anonymous execution latency and invocation telemetry for plugins stripped of tenant PII or payload secrets.

### Changed
- **Saga Rollback RLS Context & Outbox Cancellation (`nce/orchestrators/memory.py`):** Enhanced Saga compensation handlers to enforce `SET LOCAL app.current_namespace` during rollback and cancel queued transactional outbox events on compensation failures.
- **Fail-Closed Administrative Override (`nce/config.py`):** Hardened `live_admin_override_enabled()` to unconditionally return `False` when `IS_PROD` or `NCE_ENV=production`, preventing runtime bypass of security constraints in production.
- **Atomic One-Time A2A Token Consumption (`nce/a2a.py`):** Switched A2A grant token verification to atomic consumption (`GETDEL` / single-statement transition), eliminating TOCTOU race windows.
- **SQL Identifier Validation on Resource Surface (`nce/resource_surface/mcp.py`):** Enforced regex-based identifier validation on dynamic table and column parameters to prevent SQL injection in generic MCP resource endpoints.
- **Non-Blocking MinIO Health Probes (`nce/cron.py`):** Converted MinIO bucket and connectivity checks to asynchronous non-blocking probes with configurable timeouts.
- **Graceful Process Shutdown Across Daemons:** Integrated unified `SIGTERM` and `SIGINT` signal handlers with clean drain lifecycles across `server.py`, `admin_server.py`, `start_worker.py`, and `nce/cron.py`.
- **Cognitive-Stub Container Hardening:** Dropped root privileges in `cognitive-stub` container definitions to run as an unprivileged service user.

---

## [3.0.1] - 2026-11-10

### Added
- **Air-Gapped, XSS-Safe `/admin/plugins` Dual-Axis Console:** Integrated administrative console for inspecting registered vertical engines across Type and Business Category facets without external CDN dependencies.
- **Cross-Faceted Catalog API (`/admin/plugins/api`):** Exposed structured introspection endpoints for real-time plugin inspection and dependency resolution.
- **Fail-Closed Runtime Toggle Endpoint (`/admin/plugins/api/toggle`):** Added Redis-backed runtime plugin enabling/disabling with fail-closed safety semantics.

---

## [3.0.0] - 2026-11-10

### Added
- **Domain-Agnostic Core Extraction:** Decoupled all vertical business domains, ERP integrations, and operational modules into the standalone `nce-vertical-engines` repository.
- **`nce.plugins` Entry-Point Architecture:** Standardized plugin extension contract enabling dynamic registration of MCP tools, REST endpoints, and event listeners via Python packaging entry points.
- **Quad-Database Enterprise Substrate:** Formally stabilized the 4-tier storage foundation across PostgreSQL 16 (relational + pgvector), MongoDB 7.0 (episodic memory), Redis 7.4 (cache/queues), and MinIO (WORM objects).
