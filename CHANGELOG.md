# Changelog

All notable changes to the Neuro-Cognitive Engine (`nce`) project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [3.4.1] - 2026-11-11

### Fixed
- **Streamable HTTP `/mcp` Gateway Lifespan & Multi-Worker Sessions:** Wired ASGI lifespan connection management (`engine.connect()` / `disconnect()`) in `mcp_gateway_server.py`, added Redis-backed multi-worker session synchronization and `DELETE /mcp` cross-worker session eviction in `nce/mcp_http_gateway.py`, and broadened progressive `nce_discover_tools` discovery across third-party plugin tools.
- **Admin Plugins REST Auth Routing:** Excluded `/admin/plugins/api` from browser-only `BasicAuthMiddleware` interception in `nce/admin_app.py` and read request bodies prior to HMAC verification in `nce/admin_plugins.py` so `GET /admin/plugins/api` and `POST /admin/plugins/api/mint-token` authenticate cleanly via `X-Admin-Api-Key` or HMAC-SHA256 while remaining default-deny.
- **Dynamic Plugin Tenant RLS Catalog Verification:** Added vendor-neutral `get_all_plugin_tenant_rls_tables()` discovery in `nce/plugins.py` and wired runtime preflight checks (`nce/event_log.py`, `nce/orchestrator.py`, `nce/resource_surface/spec.py`) so installed plugin RLS tables pass strict catalog consistency verification without mutating Core's static `EXPECTED_TENANT_RLS_TABLES`.
- **Docker Compose TLS & Image Hardening:** Switched Compose MinIO services to `alpine/minio:RELEASE.2025-10-15T17-29-55Z` with explicit privilege drop (`minio:minio`), added explicit `localhost` / `127.0.0.1` SANs with `auto_https disable_redirects` in `Caddyfile`, included `mcp_gateway_server.py` and optional offline plugin wheelhouse installation in `deploy/multiuser/Dockerfile`, and aligned `verify_v1_launch.py` with non-blocking health degradation semantics.

---

## [3.4.0] - 2026-11-11

### Added
- **Cognitive Synthesis & Working Memory Assembly (`compile_working_memory`):** Added tool #71 to the core MCP surface, synthesizing episodic memories, semantic abstractions, knowledge graph relations, and procedural heuristics into cohesive working memory context with epistemic confidence scores and warning tags.
- **3-Way Hybrid Reciprocal Rank Fusion (RRF):** Upgraded `semantic_search` to combine dense vector similarity (`pgvector` cosine), full-text BM25 ranking (`search_tsv`), and exact identifier boosting (SKU, `orgnr`, IP addresses) via Reciprocal Rank Fusion with calibrated `confidence_score` and `epistemic_warnings`.
- **Procedural Memory Distillation (`procedural_heuristics`):** Added automated extraction and distillation of operational rules, trigger conditions, and action guidance from governed decisions, with trigger-maintained GIN `search_tsv` indexing and Row-Level Security isolation.
- **Database Migration `036_hybrid_rrf_procedural_and_worm_lock.sql`:** Provisioned `procedural_heuristics` table (RLS table #121), `memories.search_tsv`, `topology_graph.valid_from`/`valid_to` temporal columns, and PL/pgSQL helpers `acquire_event_log_worm_lock()`, `sync_memories_search_tsv()`, and `sync_procedural_heuristics_search_tsv()`.
- **Per-Namespace WORM Advisory Lock Serialization:** Added `acquire_event_log_worm_lock(p_namespace_id UUID)` and `trg_event_log_worm_advisory_lock` BEFORE INSERT trigger, serializing Merkle hash chaining strictly per namespace with zero cross-tenant contention.
- **ASGI Ingress Cap (`BoundedBodyMiddleware`):** Added strict request body limiting (`NCE_MAX_HTTP_BODY_BYTES`, default 10 MiB) across Admin REST, Streamable HTTP `/mcp` Gateway, A2A, and Webhook Receiver, terminating oversized requests with HTTP `413 Payload Too Large`.
- **SSRF Egress Hardening & Blindspot Closures:** Hardened `validate_outbound_url` against Carrier-Grade NAT (`100.64.0.0/10`) and IPv4-mapped IPv6 (`::ffff:169.254.169.254`) cloud metadata traversal, enforcing `NCE_SSRF_ALLOWLIST_HOSTS` on outbound webhook registrations and deliveries.

### Changed
- **`pgvector` HNSW Relaxed-Order Iterative Scan:** Enabled `SET LOCAL hnsw.iterative_scan = 'relaxed_order'` on tenant-filtered vector searches, avoiding premature index cutoff on highly selective queries.
- **Cycle-Safe Recursive CTE GraphRAG:** Converted knowledge-graph multi-hop traversals into PostgreSQL `WITH RECURSIVE` CTE queries honouring temporal `valid_from`/`valid_to` bounds and cycle guards.
- **MongoDB Batch Hydration:** Optimized episodic document hydration using single `$in` queries across ObjectID batches instead of sequential single-document roundtrips.
- **`orjson` Fast Serialization & Schema Caching:** Replaced standard JSON encoders with `orjson` across HTTP and stdio layers, introducing TTL-bounded caching for dynamic `tools/list` discovery.
- **Lightweight PEP 508 Base Packaging:** Streamlined `pyproject.toml` dependencies and entry points for clean wheel distribution.

### Fixed
- **Temporal Graph Attribute Alignment:** Fixed latent reference bug in `nce/causal/correlation.py` by adding `valid_from` and `valid_to` columns and index `idx_topology_graph_temporal` to `topology_graph`.
- **Procedural Heuristics Tenant Isolation:** Ensured `procedural_heuristics` enforces strict `tenant_isolation_policy` and `FORCE ROW LEVEL SECURITY`.

### Security
- **Automated CI Security Quality Gates:** Enforced Ruff `S` (flake8-bandit), `ASYNC` (asyncio hazard detection), and `DTZ` (timezone-naive datetime rejection). Integrated blocking high-severity Bandit scans in CI.

### Removed
- **Legacy TriMCP Artifacts:** Safely pruned obsolete files including `trimcp-infra/`, `package.json`, `scripts/render-env.sh`, `build/windows/TriMCP-*`, and `trace_migrations.py`.

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
