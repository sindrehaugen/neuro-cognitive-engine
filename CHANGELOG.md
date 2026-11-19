# Changelog

All notable changes to the Neuro-Cognitive Engine (`nce`) project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [3.7.0] - 2026-11-19

### Added
- **Day-2 Control-Plane Schema & Leaf-Partition Autovacuum (`nce/migrations/042_day2_ops_backup_and_governance.sql`, `nce/schema.sql`, `nce/ops/models.py`):**
  - Added 7 global control-plane tables (`runtime_tool_governance`, `backup_leases`, `backup_manifests`, `ops_probe_runs`, `ops_incidents`, `ops_quarantine`, `ops_playbook_executions`) owned by admin/`nce_gc` roles with direct table privileges revoked from `nce_app` and narrow `SECURITY DEFINER` lookup helpers (`nce_get_disabled_runtime_tools`, `nce_is_backup_lease_active`).
  - Applied dynamic leaf-partition autovacuum tuning (`autovacuum_vacuum_scale_factor = 0.02`, `autovacuum_analyze_scale_factor = 0.02`) across partitioned `memory_salience` and `kg_edges` leaf partitions plus `ops_probe_runs`.
- **`T_cut` Catalog-Watermark Backups, CoW Immutability & Ephemeral Drills (`nce/ops/backup.py`, `nce/ops/cli.py`, `nce/storage.py`, `nce/orchestrators/`, `nce/envelope.py`):**
  - Enforced Copy-on-Write (`CoW`) revision immutability on MongoDB `episodes` (`created_at` ascending index + `saga_id`) and versioned SHA-256 MinIO keys (`x-amz-meta-created-at`, `x-amz-meta-saga-id`).
  - Implemented `T_cut` coordinated `PG -> MongoDB -> MinIO` archive capture inside `REPEATABLE READ READ ONLY` snapshot transactions (`NCE_MIGRATION_PG_DSN`), TTL-bounded `<= 30m` `backup_leases`, two-phase `crypto_shred` appending HMAC-signed zero-PII lines to `<NCE_BACKUP_DIR>/shred_tombstones.jsonl` + deferred ciphertext GC (`drain_deferred_shred_gc`), disk headroom preflight (`OpsDiskHeadroomError`), and isolated single-host `nce_drill_*` ephemeral restore drills (`nce-ops backup|drill|restore-drill`, `make backup`, `make restore-drill`).
- **Deterministic Maintenance Caddy, `ops-elm` Sidecar, `@governed` Playbooks & RQ Quarantine (`nce/ops/caddy.py`, `nce/ops/elm.py`, `nce/ops/playbooks.py`, `nce/ops/quarantine.py`, `nce/cron.py`, `start_worker.py`):**
  - Added zero-`docker.sock` 5-minute, nightly, and weekly Maintenance Caddy probes writing `ops_probe_runs` and deduplicated `ops_incidents`, plus bounded Redis salience buffers (`NCE_SALIENCE_BUFFER_MAX_LEN=50000`, `24h` TTL) and `idle_in_transaction_session_timeout = '30s'`.
  - Added optional isolated `ops-elm` Ollama sidecar (`docker compose --profile ops-elm`, `OLLAMA_KEEP_ALIVE=2m`) with secret scrubbing, prompt-injection delimiter fencing, Pydantic `ELMDiagnosticBrief` playbook allowlist validation, and `< 2.0 s` deterministic offline fallback.
  - Added 5 two-step confirm-first `@governed` repair playbooks (`ops_playbook_executions`) and `RecoveringWorker` 2-strike / inflight OOM poison-pill quarantine into `ops_quarantine`.
- **Air-Gapped 5-Tab `/admin` Console, Dual-Auth & Durable Governance (`admin/`, `nce/admin_ops.py`, `nce/tool_governance.py`):**
  - Delivered 5-tab native-DOM `/admin` control plane (`1. Overview`, `2. Backups & Drills`, `3. Ops Caddy & Quarantine`, `4. Tenants & MCP Onboarding`, `5. Plugins, Tools & Connections`) with `0` external CDNs and `0` `innerHTML`, pure-ASGI `AdminDualAuthMiddleware` (`HttpOnly` signed session cookie + `X-NCE-CSRF-Token`, 3-header `HMAC-SHA256` + Redis nonces, strict Entra OIDC Bearer), and PostgreSQL-durable `runtime_tool_governance` surviving Redis `FLUSHALL`.
- **Chaos Verification & Security Fixes:**
  - Replaced `tarfile.extractall()` in `nce/ops/backup.py` with member-by-member validated stream extraction (eliminating Bandit `B202`), added `scripts/verify_day2_chaos_hardening.py` (`make verify-day2`), and published verified `6/6` telemetry in `docs/benchmarks/DAY2_OPS_HARDENING_v3.7.0.md`.

---

## [3.6.0] - 2026-11-19

### Added
- **Core Connector SDK (`nce/connectors/`):**
  - Added vendor-neutral connector contracts (`BaseAppConnector`, `ConnectorDescriptor`, `ConnectorMaturity`, `ConnectorAuthKind`) in `nce/connectors/base.py`, defaulting app connectors to `maturity="beta"` and `vendor_specific=True`.
  - Added `@governed_action`, `governed_action_response`, `assert_not_replay_sandbox`, `ReplaySandboxFenceError`, `record_connector_worm_event`, and `compute_jcs_sha256` in `nce/connectors/governance.py` enforcing replay-sandbox fencing, dry-run/approval previews, C8 redaction, and signed WORM `connector_action` audit events.
  - Added SSRF, XXE/defusedxml, zip-slip/deflate-bomb, and `AllowListRedactor` C8 guards (`nce/connectors/guards.py`), asymmetric `EntraOIDCVerifier` (`nce/connectors/entra.py`), canonical entity/diagram IR envelopes (`nce/connectors/envelopes.py`), and tenant substrate adapters (`nce/connectors/substrate.py`).
- **Encrypted Multi-Tenant Connection Vault (`nce/connectors/vault.py`, `nce/migrations/041_app_connections_vault.sql`):**
  - Added Core migration `041_app_connections_vault.sql` provisioning `app_connections` with `FORCE ROW LEVEL SECURITY` (`(SELECT get_nce_namespace())`) and envelope-encrypted `encrypted_config` (`dek_id`, `kek_version`).
  - Added `TenantConnectionVault` and `AppConnectionMetadata` for tenant-scoped credential upsert, metadata listing without secret leakage, runtime secret decryption, deletion, and connected `app_slug` resolution.
- **Admin Beta UI, Maturity Filtering & Vault API (`nce/plugins.py`, `nce/admin_plugins.py`, `admin/plugins.html`):**
  - Normalized `maturity` (`stable` / `beta` / `experimental`) and `vendor_specific` on engine descriptors, added `?maturity=` comma-separated case-insensitive filtering and `summary.maturity` / `maturity_counts` to `/admin/plugins/api` and `/api/admin/plugins`.
  - Added `/admin/plugins/api/connections` and `/api/admin/plugins/connections` (`GET` / `POST` / `DELETE`) behind existing admin auth, HMAC nonce, and SSRF validation, returning masked metadata only.
  - Updated `admin/plugins.html` with air-gapped inline CSS maturity badges (`BETA` amber/orange, `STABLE` emerald, `EXPERIMENTAL` purple/slate), Stage filter controls, and the App Connections Vault modal with zero external CDN/URLs and zero `innerHTML`.
- **Connected-First `/mcp` Compact Discovery (`nce/mcp_http_gateway.py`):**
  - Updated `discover_tools_handler()` to resolve connected `app_slug`s for `session.namespace_id` via `TenantConnectionVault(pg_pool).get_connected_app_slugs()` (failing open silently when offline/uninitialized), boost connected app tools, and strictly preserve the 12-tool `compact` limit.

### Changed
- **Documentation & Integrity Gates (`pyproject.toml`, `README.md`, `docs/plugin_development.md`, `scripts/check_docs_integrity.py`):**
  - Bumped package version to `3.6.0`, documented the `nce.connectors` SDK and 4-repo ecosystem (`neuro-cognitive-engine`, `nce-app-engines` Beta connectors, `nce-vertical-engines` Stable/Beta split, and private enterprise extensions), and ensured static `https://img.shields.io/badge/` badge URLs pass `scripts/check_docs_integrity.py` offline without live network fetches.

---

## [3.5.4] - 2026-11-18

### Performance & Pure-ASGI Runtime
- **Eliminated HTTP/1.1 Delayed-ACK Floor Across Core (`nce/auth.py`, `nce/jwt_auth.py`, `nce/webhook_receiver/main.py`, `tests/_verified_tier_middleware.py`):**
  - Replaced all `BaseHTTPMiddleware` subclasses (`HMACAuthMiddleware`, `BasicAuthMiddleware`, `AdminHTTPRateLimitMiddleware`, `JWTAuthMiddleware`, `VerifiedTierTestMiddleware`) and FastAPI `@app.middleware("http")` (`webhook_rate_limit_middleware`) with pure-ASGI (`__call__(scope, receive, send)`) implementations preserving `scope["state"]`, fast-probe bypasses (`/healthz`, `/health`, `/readyz`, `/ready`, `/.well-known/agent-card`), bounded-body HTTP `413` enforcement before HMAC hashing, and single-consumption replayable `receive`.
- **Cron Remote-Inference RSS & Idle Healthcheck CPU (`nce/reembedding_worker.py`, `docker-compose.yml`):**
  - Short-circuited `ReembeddingWorker._vram_pressure_gate()` prior to `import torch` when `NCE_COGNITIVE_BASE_URL` is configured, preventing ~400+ MiB `libtorch` RSS growth in `nce-cron`.
  - Updated Compose `mongodb` healthcheck to `mongosh --quiet --norc --eval "quit(db.runCommand({ ping: 1 }).ok ? 0 : 2)"` (`30s` interval, `start_period: 15s`, `start_interval: 2s`) and aligned `admin`, `a2a`, `mcp-gateway`, and `webhook-receiver` readiness intervals to `30s` (`start_period: 10s`, `start_interval: 2s`).
- **Air-Gapped Baked ONNX Cognitive Compose Wiring (`docker-compose.yml`, `nce/config.py`):**
  - Upgraded Compose `cognitive` service to `nce-cognitive:3.5.4` with `NCE_COGNITIVE_BACKEND=onnx`, micro-batching (`NCE_COGNITIVE_BATCH_WINDOW_MS=5`, `NCE_COGNITIVE_MAX_BATCH_SIZE=32`, `NCE_COGNITIVE_NUM_THREADS=4`), pinned `HF_HUB_OFFLINE="1"` / `TRANSFORMERS_OFFLINE="1"`, and `/readyz` engine acceptance for `onnxruntime`, `onnxruntime-int8`, and `sentence-transformers`.

### Security & Verification
- **Cron Seccomp Parity (`docker-compose.yml`):** Enforced `- "seccomp:./deploy/seccomp/worker.json"` on `cron.security_opt`.
- **Fail-Closed Entra Production Gate (`nce/config.py`, `nce/admin_app.py`, `admin_server.py`):** Raised `RuntimeError` during boot validation if `NCE_ENV` is `prod`/`production` and `NCE_ENTRA_TENANT_ID` is non-empty without `NCE_ENTRA_ENFORCE_MODE=strict`.
- **Dynamic Package Version & Event Retention Tests (`nce/__init__.py`, `nce/webhook_receiver/main.py`, `nce/admin_app.py`, `nce/a2a_server.py`, `tests/test_event_retention.py`):** Resolved `nce.__version__` from `pyproject.toml`/`neuro-cognitive-engine` dist metadata instead of hardcoded `"3.5.3"`, and fixed `tests/test_event_retention.py` partition inserts to supply 32-byte `chain_hash` without silent `pytest.skip` suppression.

---

## [3.5.3] - 2026-11-18

### Security
- **PostgreSQL Partition RLS, WORM Parity & Key Confinement (`nce/schema.sql`, Migration `040_partition_rls_and_key_confinement.sql`, `nce/signing.py`, `nce/event_log.py`):**
  - Enabled and forced Row-Level Security (`ENABLE` + `FORCE ROW LEVEL SECURITY` with `(SELECT get_nce_namespace())`) on `kg_node_embeddings` and every tenant hash/range/default child partition (`kg_nodes_0..3`, `kg_edges_0..3`, `kg_node_embeddings_0..3`, `embedding_aspects_0..3`, `memory_embeddings_0..3`, `memory_salience_0..3`, `memories_default`, `contradictions_default`, `pii_redactions_default`, and all `event_log_*` child partitions), and revoked direct `UPDATE, DELETE, TRUNCATE` on `event_log_*` partitions from `nce_app`/`nce_gc`.
  - Hardened `nce_ensure_event_log_monthly_partitions(int)` (`SECURITY DEFINER`, `SET search_path = public, pg_temp`) so newly minted monthly partitions automatically inherit forced RLS and WORM protections.
  - Revoked direct `public.signing_keys` table privileges from `nce_app` in favor of narrow `SECURITY DEFINER` helpers (`nce_get_signing_key_for_unwrap`, `nce_list_signing_keys_metadata`, `nce_list_active_signing_keys`, `nce_resolve_signing_key_status`, `nce_list_signing_key_versions`), and tightened `verify_rls_role_capability()` and `verify_v1_launch.py` to audit partitions and key confinement continuously.
- **Nonce-Bound HMAC-SHA256 Authentication (`nce/auth.py`):**
  - Bound `X-NCE-Nonce` (`^[A-Za-z0-9._:-]{16,128}$`) directly into canonical HMAC strings (`METHOD\nPATH\nTIMESTAMP\nNONCE[\nSHA256(BODY)]`), stored per-principal atomic replay keys (`nce:hmac_nonce:{principal_scope}:{nonce}`) in Redis `SETNX`, and attached `request.state.hmac_auth` across `HMACAuthMiddleware`.
- **Single-Port Edge Proxy, Internal mTLS & Worker Seccomp (`docker-compose.yml`, `Caddyfile`, `deploy/Caddyfile.mtls`, `docker-compose.mtls.yml`, `deploy/seccomp/worker.json`):**
  - Isolated datastores and internal services onto `nce_data` (`internal: true`) with host ports bound solely on `nce-caddy` (`127.0.0.1:8082`/`8443`), added opt-in `docker-compose.debug-ports.yml` and `docker-compose.ci-stub.yml`, added `scripts/generate_internal_mtls.py` + `docker-compose.mtls.yml` for Caddy <-> Admin mTLS (`NCE_ENV=prod`, `--ssl-cert-reqs 2`), updated `X-XSS-Protection "0"` and dynamic vertical `vars` in `Caddyfile`, and enforced a 21-syscall `deploy/seccomp/worker.json` profile (`Seccomp: 2`) on Celery/RQ workers and cron.

### Changed
- **Fast Liveness, Singleflight Readiness & Real CPU Inference (`nce/health_probe.py`, `nce/embeddings.py`, `deploy/cognitive-service/`):**
  - Decoupled `< 3 ms` in-memory `GET /healthz` from `5.0 s` asyncio singleflight `GET /readyz` across Admin, A2A, MCP Gateway, Webhook Receiver, and `health_probe.py --liveness`.
  - Replaced default stub inference with CPU `sentence-transformers/all-mpnet-base-v2` (768-d L2-normalized) + `spaCy` `en_core_web_sm` in `deploy/cognitive-service/` and enforced fail-closed `assert_cognitive_not_stub_in_prod()` in production.
- **Public Brand-Wall & Benchmark Documentation:** Removed residual vertical/downstream references from Core tooling (`Makefile`, `scripts/stage-vertical-wheels.py`, `scripts/check_docs_integrity.py`, `nce/mcp_stdio_tools.py`), regenerated `docs/API.md`, added `scripts/benchmark_live_stack.py`, and published `docs/benchmarks/HARDENING_READINESS_v3.5.3.md`.

---

## [3.5.2] - 2026-11-17

### Performance
- **Post-Burn-In Sub-50ms LLM-Loop Optimization (`nce/mcp_http_gateway.py`, `nce/plugins.py`, `nce/semantic_search.py`, `nce/orchestrator.py`):**
  - Added `O(1)` cached gateway tool catalog (`_ALL_GATEWAY_TOOLS_CACHE`), tokenized keyword/BM25 ranking with `limit` cap in `discover_tools_handler()`, and `_compact_input_schema()` stripping redundant auth properties (`mcp_api_key`, `admin_api_key`) from compact schemas.
  - Added bounded 30-second TTL verified-token cache (`_VERIFIED_TOKEN_CACHE`, `invalidate_verified_token_cache()`) eliminating per-request DB round-trips on active `/mcp` sessions.
  - Added in-memory plugin descriptor & RLS table caches (`_DESCRIPTOR_CACHE`, `_TENANT_RLS_CACHE`, `_GLOBAL_TABLES_CACHE`) in `nce/plugins.py`, invalidated automatically on `load_plugins(force_reload=True)`.
  - Added bounded `_SALIENCE_RING_BUFFER` (`maxlen=4096`) in `nce/semantic_search.py` coalescing concurrent read-side salience reinforcements into single `reinforce_batch` transactions, and enabled `statement_cache_size=256` on `asyncpg.create_pool`.
  - Added `scripts/profile_llm_agent_loop.py` and `docs/benchmarks/PERF_REPORT_v3.5.2.md` certifying 10-agent concurrent 5-tool chained LLM reasoning loops at 100k rows.

### Documentation & Tooling
- **100% Configuration & Documentation Truth Parity (`README.md`, `.env.example`, `docs/configuration_reference.md`, `docs/plugin_development.md`, `scripts/check_docs_integrity.py`):**
  - Documented all 209 `NCE_*` configuration variables across `.env.example` and `docs/configuration_reference.md` (`0` missing keys).
  - Authored `docs/plugin_development.md` covering `NCEPlugin`, `PluginCapabilities`, namespace-prefixed migrations, and progressive `/mcp` `compact` routing.
  - Added `scripts/check_docs_integrity.py` enforcing zero broken links, 100% `NCE_*` config parity, version/tool-count alignment, and strict brand-wall hygiene.

---

## [3.5.1] - 2026-11-17

### Performance
- **100k-Row Hot-Path Latency Optimization (`nce/envelope.py`, `nce/semantic_search.py`, `nce/embeddings.py`, `nce/db_utils.py`, `nce/graph_query.py`, `nce/working_memory.py`):**
  - Added bounded TTL LRU cache (`_UNWRAPPED_DEK_CACHE`, `maxsize=512, ttl=300s`) with in-place `bytearray` zeroing on eviction/clear (`shred_memory`, `forget_memory`, `crypto_shred_subject`), eliminating the `~246 ms` per-call Argon2id KDF unwrap tax (`semantic_search` p50 `336.26 ms -> 24.46 ms`, `as_of` p50 `1231.25 ms -> 25.06 ms`, `compile_working_memory` p50 `651.32 ms -> 26.70 ms`, `graph_search` live p50 `108.73 ms -> 21.00 ms`).
  - Fixed `::vector` -> `::halfvec(768)` cast alignment in `nce/semantic_search.py` and `nce/graph_query.py`, replaced `COALESCE(m.search_tsv, m.content_fts) @@ ...` sequential scan with direct `m.search_tsv @@ ...` `Bitmap Index Scan` (`13.57 ms -> 0.61 ms`), and rewrote `kg_edges` trigram `ILIKE` to use expression index `idx_kg_edges_combined_trgm` (`0.23 ms`).
  - Added bounded TTL LRU query-embedding cache (`_embed_query_cache`) in `nce/embeddings.py` and combined `set_config` round-trips in `scoped_pg_session` (`nce/db_utils.py`).
- **Migration `038` (`nce/migrations/038_v351_hotpath_rls_and_indexes.sql`, `nce/schema.sql`):** Marked `get_nce_namespace()`, `get_nce_external_scope()`, and `nce_decayed_score()` `STABLE PARALLEL SAFE`, wrapped RLS policy predicates in `(SELECT get_nce_namespace())` `InitPlan`, and added `idx_kg_edges_combined_trgm`, `idx_memories_ns_payload_ref_active`, `idx_memories_search_tsv_active`, and `idx_saga_log_recovery_needed`.
- **Sub-3s Mongo Fail-Fast & Saga Concurrency (`nce/_client_factory.py`, `nce/orchestrators/memory.py`, `nce/cron.py`):** Tightened Motor default timeouts to `2000ms`, bounded inline rollback wait when `insert_one` times out (`10.1s -> 2.36s` failover to `state='recovery_needed'`), and added `FOR UPDATE SKIP LOCKED` to `_saga_recovery_tick` queries.

### Fixed
- **Schema Drift & Cross-Repo Migration Seam (`scripts/check_schema_drift.py`, `tests/unit/test_v351_hotpath_and_seam.py`):** Reconciled `check_schema_drift.py` table reporting (`33` `schema.sql` tables + `10` migration-created tables = `43` Core SQL tables) and verified disjoint ledger keys between Core `037`/`038` and plugin `nce_verticals:037_bom_line_priced.sql`.

---

## [3.5.0] - 2026-11-17

### Security
- **Hardening Wave (`S1`–`S7`):** Strengthened runtime, transport, and supply-chain security boundaries across ingress surfaces, credential handling, and container/CI posture (including least-privilege `contents: read` workflow permissions, commit-SHA pinned third-party GitHub Actions, `pip-audit` against `requirements.lock`, pinned `en_core_web_sm` `3.8.0` wheel, root-owned `/app` runtime code, `.dockerignore` secret exclusions, and loopback-bound datastore ports in legacy multiuser Compose).

### Changed
- **Resilience & Infrastructure (`R1`–`R2`, `I1`):** Added `stop_grace_period: 60s` for `worker` and `cron` Compose services, pinned `deploy/cognitive-stub/Dockerfile` to `python:3.12-slim-bookworm`, replaced `curl` healthchecks with stdlib `urllib.request` probes in `deploy/multiuser/docker-compose.yml` (now marked deprecated in favour of root `docker-compose.yml`), added Docker image build validation in CI, and wired `tests/test_replay_handlers_integration.py` into the CI integration job.
- **Performance & Schema (`P1`–`P4`, Migration `037`):** Applied v3.5.0 performance optimizations alongside database migration `037`.

---

## [3.4.4] - 2026-11-17

### Changed
- **Sub-2s Redis Fail-Closed Timeouts (`nce/config.py`, `nce/orchestrator.py`, `nce/auth.py`):** Added `REDIS_SOCKET_TIMEOUT_S` and `REDIS_CONNECT_TIMEOUT_S` (default `1.5`s) and wired them into engine Redis pools and `NonceStore`, ensuring paused/unreachable Redis fails closed (`-32005`) in `< 2000 ms`.
- **True Compact MCP Router (`nce/mcp_http_gateway.py`):** Trimmed `tool_exposure_mode="compact"` initial `tools/list` to the 12 core working-memory tools (`discover_tools` + 11 core memory/graph/contradiction tools, `<= 16`), deferring admin/A2A/bridge/replay schemas to `discover_tools(query=...)` while preserving all tools in `full` mode.
- **Admin Token Scopes & Documentation (`admin/plugins.html`, `README.md`):** Added an `admin` scope checkbox to the `/admin/plugins` token-minting modal and updated the `README.md` MinIO storage table to include `PDF/logs/binary`.
- **CI/CD & Dependency Hygiene (`.github/workflows/dep-audit.yml`, `scripts/dep_report.py`, `.github/workflows/refresh-lock.yml`, `.github/dependabot.yml`):** Added empty-secret SMTP guard and real `pip-audit` exit-code propagation in `dep-audit.yml` + `scripts/dep_report.py`, added `refresh-lock.yml` fallback, fixed `dependabot.yml` `docker-compose` ecosystem configuration, and cleaned up secret-scanner test fixtures.
- **Dependency Floor Consolidation (`pyproject.toml`, `requirements.txt`, `requirements.lock`):** Consolidated Dependabot floor bumps (`fastapi>=0.162.5`, `pydantic>=2.14.3`, `python-multipart>=0.0.32`, `RestrictedPython>=8.5`, `rq>=3.0.0`, `pypdf>=7.0.1`, `sentence-transformers>=6.3.0`, `python-docx>=1.2.0`, `tree-sitter-language-pack>=1.23.1`, `vsdx>=0.6.1`) and refreshed `requirements.lock`.

---

## [3.4.3] - 2026-11-12

### Added
- **Plugin Capabilities & Namespace Lifecycle Hooks (`nce/plugins.py`):** Added `PluginCapabilities` dataclass (`tenant_rls_tables`, `global_tables`), `get_all_plugin_global_tables()`, and `run_plugin_namespace_created_hooks(conn, namespace_id)` so installed plugins register forced-RLS/global tables dynamically and seed plugin-owned per-namespace registries cleanly without hardcoded vertical imports in Pure Core.
- **Deterministic Consolidation Replay Cache (`nce/consolidation.py`):** Added `ConsolidationWorker._emit_consolidation_event()` persisting canonical LLM synthesis payloads to `nce-llm-payloads/{namespace_id}/{event_id}.json` with SHA-256 verification and `llm_response_uri` linkage for deterministic replay reconstruction.
- **Auto-Seeded Initial Signing Key Bootstrap (`nce/orchestrator.py`):** Added advisory-locked `_ensure_initial_signing_key()` startup bootstrap when `signing_keys` is empty, eliminating fresh-database `@require_admin_auth` -> `append_event` bootstrap deadlocks on `rotate_signing_key`.
- **Custom Token Scopes & Optional Crypto-Shredding:** Added optional `scopes` validation to `POST /admin/plugins/api/mint-token`, added `ForgetMemoryRequest.crypto_shred: bool = False` delegating `crypto_shred=True` to `shred_memory()`, expanded `StoreArtifactRequest` (`"image" | "audio" | "video" | "pdf" | "binary" | "log"`) storing objects in `nce-artifacts/{namespace_id}/{artifact_type}/...`, and enabled `NCE_ENVELOPE_ENCRYPTION_ENABLED=true` by default with backward-compatible legacy plaintext reads.

### Fixed
- **Pure-Core Forced-RLS Catalog Alignment (`nce/event_log.py`):** Scoped static `EXPECTED_TENANT_RLS_TABLES` (`35`) + `EXPECTED_SPECIAL_RLS_TABLES` (`1`, `a2a_grants`) strictly to the **36** Pure-Core forced-RLS tables (`001`–`036`) and `EXPECTED_GLOBAL_TABLES` to the **5** Pure-Core global tables, decoupled `NamespaceOrchestrator._create_namespace()` from `node_ownership_registry`, removed the vertical DDL shim from `scripts/apply_integration_schema.py`, and migrated 18 orphaned vertical integration test modules to `nce-vertical-engines`.
- **Event Replay Engine Overhaul (`nce/replay.py`):** Included `namespace_id`, `signature_version`, and `chain_hash` in `_build_event_query()`, normalized string/dict JSONB parsing, initialized `is_raw_uuid` safely prior to `try:` blocks, injected `source_namespace_id` before handler dispatch, wrapped all observational/forked/reconstructive/provenance/status queries in tenant-scoped RLS transactions, reconciled `is_replay_sandbox` across `namespaces` schema + `metadata` JSONB, and serialized pgvector bindings safely in `_handle_consolidation_run()`.
- **Saga Timeout Bounds & Pre-Generated Mongo `ObjectId` Compensation (`nce/orchestrator.py`, `nce/orchestrators/memory.py`):** Added explicit `socketTimeoutMS=5000` on Motor and `urllib3.Timeout(connect=2.0, read=5.0)` + bounded retries on MinIO, and pre-generated `ObjectId()` prior to `insert_one(doc)` so saga rollback reliably deletes delayed in-flight MongoDB documents after network/container stalls.
- **Forced-RLS `verify_memory()`, Bi-Temporal `as_of` Queries & KG Lineage (`nce/orchestrators/memory.py`, `nce/graph_query.py`, `nce/semantic_search.py`):** Acquired tenant-scoped RLS connections and `ObjectId(payload_ref)` + `origin_event_id` signature verification in `verify_memory()`, qualified `event_log.id AS event_id` across all 4 time-travel CTEs in `nce/graph_query.py`, bypassed current mutable `salience_score > 0.0` filtering while enforcing `[created_at, valid_to)` bounds when `as_of` is supplied in `nce/semantic_search.py`, and populated `origin_event_id` on `kg_nodes` and `kg_edges`.
- **A2A Transactions, `one_time` Grants & Schema-Aware `/mcp` Tenant Binding (`nce/a2a_mcp_handlers.py`, `nce/mcp_http_gateway.py`):** Wrapped A2A event appends and token verification in RLS transactions, plumbed `one_time` in `_build_grant_request()`, and added schema-aware `bind_session_tenant_arguments()` + `mcp_http_session_var` on `/mcp` so `ReplayForkRequest`, `A2AQuerySharedRequest`, `ReplayStatusRequest`, and admin-scoped tools execute cleanly under `extra="forbid"`.
- **Envelope DEK Decryption, Scoped Code Search, Cognitive Stub & Runtime Deps (`nce/contradictions.py`, `nce/consolidation.py`, `nce/orchestrators/graph.py`, `deploy/cognitive-stub/stub_server.py`):** Decrypted `TCDEK\x01` episode envelopes in contradiction detection and consolidation, scoped `GraphOrchestrator.search_codebase()` via `_get_scoped_mongo(req.namespace_id)`, upgraded `stub_server.py` with deterministic unit-norm embeddings and prompt-aware NLI/synthesis JSON, added `X-NCE-Nonce` to `verify_v1_launch.py`, and added `Pillow`, `pytesseract`, `python-multipart`, `scikit-learn`, and `RestrictedPython` to `pyproject.toml` + `requirements.txt` / `requirements.lock`.

---

## [3.4.2] - 2026-11-12

### Changed
- **Dependency Floors & Python 3.12 Lockfile Sync:** Incorporated Dependabot floor upgrades (`ffc8e59`, `de9b7f0`, `243af33`, `afa6bb1`) across `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, and `requirements.lock` (`fastapi>=0.162.2`, `striprtf>=0.0.35`, `sse-starlette>=3.5.1`, `StrEnum>=0.5.0`, `setuptools>=84.2.1`, `ruff==0.17.4`, `pytest>=9.2.0`, `pytest-asyncio>=1.4.1.post1`, ML stack, and OpenTelemetry SDKs) alongside GitHub Actions bumps (`actions/setup-python@v7` in `78e06c8` and `softprops/action-gh-release@v3` in `ac01960`).
- **Developer Config & Pre-Commit Parity:** Updated `.pre-commit-config.yaml` to run current `pyproject.toml` Ruff lint (`ASYNC`, `DTZ`, `S`, `W605`), `ruff format --check`, `mypy nce/`, and `check_empty_with.py` (including `mcp_gateway_server.py`); refreshed `docker-compose.override.example.yml` for `mcp-gateway`, `admin`, `a2a`, `webhook-receiver`, single-replica CPU `worker`, and `cron`.
- **Documentation & Entrypoint Alignment:** Updated `README.md` Quickstart (`mcp-gateway` Compose service and `python mcp_gateway_server.py` host run), synchronized A2A JWT algorithm documentation (`RS256`/`ES256`/`PS256` public key or `HS256` secret), clarified Core forced-RLS table counts (`36` Core tenant tables vs. `121` full `EXPECTED_TENANT_RLS_TABLES` catalog), documented the native `go/cmd/nce-launch` CLI shim in `docs/usage_modes.md`, fixed footer spacing, and pruned superseded remote branches.

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
