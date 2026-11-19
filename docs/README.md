> **Status:** shipped · **Verified-against:** 236e4dc (main) · **Last-audited:** 2026-11-19

# NCE Documentation Index

Technical specifications, systems-architecture guides, security runbooks, and API references for **NCE v3.6.0** (an ongoing personal R&D project started in early May 2026).

---

## Getting Started

- [**Quick Start Guide**](quick_start.md): Fastest path from a fresh clone to a local four-store sandbox and MCP endpoint.
- [**Developer Onboarding**](developer_onboarding.md): Local four-store setup, `pytest` suites, codebase module map, and contribution guardrails.
- [**Usage Modes**](usage_modes.md): Streamable HTTP `/mcp`, MCP `stdio` (JSON-RPC 2.0), and Admin REST API wire-level payload examples.

---

## Architecture & System Design

- [**Architecture Specification (Spec v1.0)**](architecture-v1.md): Runtime process topology, point-in-time temporal queries, A2A federation, background summarization workers, GraphRAG pipeline, partitioning trade-offs, and MCP surface.
- [**Database Architecture**](database_architecture.md): Connection pools (`asyncpg`, `Motor`, `Redis`, `MinIO`), `scoped_pg_session` forced RLS, transaction-scoped Saga write path, GraphRAG hydration, WORM `event_log` design, and cross-store backup skew considerations.
- [**Recursive Indexing Flow**](recursive_indexing_flow.md): Asynchronous Tree-sitter AST code indexing via MCP and RQ workers.
- [**Push Architecture**](push_architecture.md): Inbound document-bridge webhook ingestion, queue dispatch, and subscription renewal lifecycle.
- [**MCP Tool Cookbook**](mcp_tool_cookbook.md): Per-tool JSON-RPC 2.0 request/response recipes for the 71 Core MCP tools and progressive `discover_tools` routing.
- [**Frontend Integration Guide**](frontend_integration_guide.md): Host-integration seams for mounting custom Starlette routes and registering MCP tools.
- [**API Usage Examples**](api_usage_examples.md): Copy-paste HTTP REST and JSON-RPC examples for common operator and client workflows.
- [**Citus Multi-Node Descope**](citus_descope.md): Engineering rationale for descoping Citus sharding in favor of single-node PostgreSQL partitioning.

---

## Configuration & Operations

- [**Configuration Reference**](configuration_reference.md): Complete reference for all `NCE_*` environment variables, defaults, and launch commands.
- [**IT Admin Guide**](it_admin_guide.md): Day-two operational procedures for self-hosted sandboxes and engineering pilots.
- [**Airgapped & Edge Deployment**](airgapped_deployment.md): Offline baked ONNX embedding container, OpenVINO CPU/NPU execution, and air-gapped network setup.
- [**VRAM Monitoring**](vram_monitoring.md): Prometheus gauges and alert thresholds for GPU/VRAM usage in the re-embedding worker.
- [**Observability Guide**](observability.md): Prometheus metrics, OpenTelemetry tracing, health endpoints, and alerting rules.
- [**Performance Tuning**](performance_tuning.md): Operator-tunable pool sizes, HNSW scan knobs, caching parameters, and profiling methodology.
- [**Troubleshooting & FAQ**](troubleshooting_faq.md): Common setup, authentication, and container-networking diagnostics.
- [**Backfill Chain Hash Guide**](scripts/BACKFILL_CHAIN_HASH.md): Runbook for verifying and backfilling Merkle chain hashes across historical `event_log` rows.
- [**Deployment Topologies (`deploy/`)**](../deploy/README.md): Compose overlays, internal mTLS setup, and reverse-proxy hardening.

---

## Security & Multi-Tenancy

- [**Enterprise Security Guide**](enterprise_security.md): Internal mTLS, JWT/OIDC verification, nonce-bound HMAC authentication, SSRF egress guards, forced RLS, and operator custody responsibilities.
- [**Cryptographic Signing & Integrity**](signing.md): `HMAC-SHA256` event signing over RFC 8785 (JCS) canonical JSON and key-rotation mechanics.
- [**Multi-Tenancy & Resource Quotas**](multi_tenancy.md): `scoped_pg_session`, forced RLS across all 38 standard tenant tables + `a2a_grants`, and atomic namespace quotas.
- [**PII Detection & Redaction**](pii.md): Regex/NLP PII detection, redaction policies, and `AES-256-GCM` reversible pseudonymization vault.
- [**AWS IAM & Worker Isolation**](aws_iam_worker_isolation.md): Least-privilege container/Fargate worker IAM and network boundary definitions.

---

## Retrieval, Graph Traversal & Background Summarization

- [**Hybrid Retrieval, Graph Traversal, Contradiction Detection & Background Summarization**](cognitive_layer.md): 3-way RRF ranking, `WITH RECURSIVE` graph traversal, configurable time-decay relevance scoring (`salience`), cross-encoder NLI conflict checks, ATMS dependency retraction, and HDBSCAN/RQ summarization jobs.
- [**LLM Providers & Structured Output**](llm_providers.md): Provider-agnostic model adapters and Pydantic V2 output validation contracts.

---

## Temporal Queries, Replay & Schema Evolution

- [**Point-in-Time Temporal Queries (`as_of`)**](time_travel.md): Historical state filtering over append-only `event_log` records, named snapshots, and `compare_states`.
- [**Deterministic Event Replay & Sandbox Forking**](replay.md): Read-only streaming, MinIO-cached deterministic reconstruction, `re-execute` sandbox forking, and outbound actuation fencing.
- [**System Migrations & Shadow-Column Re-embedding**](migrations.md): Isolated plugin migration ledgers, dual-column zero-downtime re-embedding, and recall overlap quality gates.

---

## Shared-Core Foundation (Cross-Plugin Contracts)

Cross-module governance and isolation primitives used by optional R&D domain plugins:

- [**Shared Core Overview**](shared-core/overview.md): C1–C9 capability map, per-namespace opt-in flags, and cross-module contracts.
- [**C2 Action Governance (`@governed`)**](shared-core/autonomy-governance.md): Confirm-first human approval gates, value/volume ceilings, allowlists, idempotency keys, and emergency kill-switches.
- [**Entity Resolution (C1)**](shared-core/entity-resolution.md): Deterministic/fuzzy `resolve()`, human merge-review queue, survivorship rules, and node-ownership registry.
- [**External-Scope RLS (C3)**](shared-core/external-scope-rls.md): Database-enforced partner and customer principal row scoping.
- [**Field Redaction (C8)**](shared-core/redaction.md): Default-deny allowlist `project(node, surface)` field projection for external surfaces.
- [**Source-Mode & Divergence (C5)**](shared-core/source-mode-divergence.md): Per-domain `d365 | both | nce` routing and shadow-read divergence logging.
- [**Pricing, Signing & Grounding (C6/C7/C9)**](shared-core/pricing-signing-grounding.md): Deterministic integer-minor-unit pricing, signature ceremony transport, and structural reference checks.

---

## Integrations, Federation & Extension Packages

- [**Plugin Development & Connector SDK (`nce.connectors`)**](plugin_development.md): `BaseAppConnector`, `ConnectorDescriptor`, `@governed_action`, `TenantConnectionVault`, `NCEPlugin`, and entry-point packaging.
- [**Plugin Overview (Non-Technical)**](plugin-overview-nontechnical.md): Plain-language summary of what optional NCE plugins do and their human-review boundaries.
- [**Service Integrations**](service_integrations.md): SharePoint/OneDrive, Google Drive, and Dropbox webhook ingestion, retries, and dead-letter queues.
- [**Bridge Setup Guide**](bridge_setup_guide.md): OAuth app registration and webhook endpoint setup for cloud document providers.
- [**Cross-Namespace Query Federation (A2A)**](a2a.md): Scoped, SHA-256 hash-grant and JWT-authenticated JSON-RPC query federation bound to owner RLS.
- **Experimental Sibling R&D Repositories:**
  - [`nce-app-engines`](https://github.com/sindrehaugen/nce-app-engines) (`[Experimental / Beta]`): 20 standalone Core-only application & CAD/BIM connectors.
  - [`nce-vertical-engines`](https://github.com/sindrehaugen/nce-vertical-engines) (`[Experimental / Beta]`): 29 modular business, ERP/CRM, IT/OT, and engineering prototype schemas.

---

## Benchmarks, Reference & Historical ADRs

- **Controlled Container Benchmark & Hardening Reports:**
  - [**100k-Row Performance & Chained MCP-Turn Report (`v3.5.2`)**](benchmarks/PERF_REPORT_v3.5.2.md)
  - [**Security Hardening & Partition RLS Report (`v3.5.3`)**](benchmarks/HARDENING_READINESS_v3.5.3.md)
  - [**Pure-ASGI, Baked ONNX & Cron Isolation Report (`v3.5.4`)**](benchmarks/HARDENING_READINESS_v3.5.4.md)
  - [**Connector SDK & Maturity Contract Report (`v3.6.0`)**](benchmarks/HARDENING_READINESS_v3.6.0.md)
- [**API Reference (Curated)**](api_reference.md) · [**API Surface (Auto-Generated)**](API.md)
- [**Detector Limits & AST Ratchet**](detector_limits.md) · [**Data-Source Modes**](DATA_SOURCE_MODES.md) · [**Frontend Readiness**](FRONTEND_READINESS.md)
- [**Architecture Decision Records**](adr/README.md): Historical ADRs `0001`–`0008` preserved for design lineage.
- **Auto-Generated Telemetry & Surface Ledgers (`docs/_generated/`):**
  - [**Canonical Surface Inventory**](_generated/surface.md) · [**Engine Figures**](_generated/engine_figures.md) · [**Golden Thread Seams**](_generated/golden_thread_seams.md) · [**Host Parity Matrix**](_generated/host_parity.md) · [**C12 Resource Surface**](_generated/resource_surface.md) · [**Waves Landed Ledger**](_generated/waves_landed.md)
- **Docsify Navigation Chrome:** [`_sidebar.md`](_sidebar.md) · [`_navbar.md`](_navbar.md) · [`_404.md`](_404.md)
- **Repository Governance:** [`../README.md`](../README.md) · [`../CHANGELOG.md`](../CHANGELOG.md) · [`../CONTRIBUTING.md`](../CONTRIBUTING.md) · [`../SECURITY.md`](../SECURITY.md) · [`../CLA.md`](../CLA.md)
