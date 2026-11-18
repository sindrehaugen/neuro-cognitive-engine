> **Status:** shipped · **Verified-against:** e00118e (main) · **Last-audited:** 2026-09-18

# NCE Documentation Index

Technical specifications, architectural guides, and operational references for NCE v3.5.3.

---

## Getting Started

- [**Quick Start Guide**](quick_start.md): Fastest path from zero to a working MCP server.
- [**Developer Onboarding**](developer_onboarding.md): Local Quad-DB setup, pytest execution, codebase map, and contribution invariants.
- [**Usage Modes**](usage_modes.md): MCP/LLM stdio (JSON-RPC 2.0) vs. Admin REST API — wire-level payload examples for both.

---

## Architecture & System Design

- [**Architecture Specification (Spec v1.0)**](architecture-v1.md): Runtime topology, temporal engine, A2A protocol, cognitive workers, GraphRAG pipeline (§7.1), partitioning tradeoffs, and MCP tool surface.
- [**Database Architecture**](database_architecture.md): Connection pools (asyncpg, Motor, Redis, MinIO), `scoped_pg_session` pattern, Saga cross-DB write path, GraphRAG hydration pipeline, WORM event log design, and module map.
- [**Recursive Indexing Flow**](recursive_indexing_flow.md): Async code-indexing via MCP + RQ workers; how NCE ingests its own codebase or any directory.
- [**Push Architecture**](push_architecture.md): Document Bridge push flow — webhook ingest from providers through the worker and subscription renewal lifecycle.
- [**MCP Tool Cookbook**](mcp_tool_cookbook.md): Per-tool JSON-RPC 2.0 recipes for the core (pre-engine) tool surface with gating-column reference; engine tool families are documented in the per-engine guides below.
- [**Frontend Integration Guide**](frontend_integration_guide.md): FE-1/FE-2 host-integration seams for mounting custom Starlette routes and registering custom MCP tools against a pristine NCE installation.
- [**API Usage Examples**](api_usage_examples.md): End-to-end copy-paste examples for the NCE HTTP REST API.
- [**Citus Multi-Node Descope**](citus_descope.md): Citus distribution descope rationale and single-node PostgreSQL migration strategy.

---

## Configuration & Operations

- [**Configuration Reference**](configuration_reference.md): Authoritative reference for every environment variable, server launch command, and runtime flag.
- [**IT Admin Guide**](it_admin_guide.md): Operational procedures for production deployments.
- [**Airgapped & Edge Deployment**](airgapped_deployment.md): Local inference stack, OpenVINO NPU hardware acceleration, and offline configuration.
- [**VRAM Monitoring**](vram_monitoring.md): Prometheus gauges for CUDA/PyTorch VRAM consumption in the re-embedding worker (Item 49 observability fix).
- [**Observability Guide**](observability.md): Prometheus metrics, OpenTelemetry tracing, health endpoints, and recommended dashboards and alerts.
- [**Performance Tuning**](performance_tuning.md): Operator-tunable performance knobs across every NCE subsystem; sizing guidance and measurement methodology.
- [**Troubleshooting & FAQ**](troubleshooting_faq.md): Common errors and their resolutions.
- [**Backfill Chain Hash Guide**](scripts/BACKFILL_CHAIN_HASH.md): Operational runbook for verifying and backfilling cryptographic Merkle chain hashes across historical event log records.

---

## Security & Multi-Tenancy

- [**Enterprise Security Guide**](enterprise_security.md): mTLS client certificates, JWT/SSO integration, HMAC API authentication, signing key management, RLS enforcement, and production security checklist.
- [**Cryptographic Signing & Integrity**](signing.md): HMAC-SHA256 integrity layer, JCS canonicalization (RFC 8785), and AES-256-GCM key management.
- [**Multi-Tenancy & Resource Quotas**](multi_tenancy.md): Isolation boundaries, Row-Level Security (RLS) enforcement, and the atomic quota engine.
- [**PII Detection & Redaction**](pii.md): Automated PII pipeline (Presidio/Regex), redaction policies, and the reversible pseudonymization vault.
- [**AWS IAM & Worker Isolation**](aws_iam_worker_isolation.md): Fargate worker IAM policy, network boundary definitions, and Phase 2 infrastructure hardening.

---

## Shared-Core Foundation (Cross-Engine Contracts)

The C1–C9 substrate every business engine builds on:

- [**Shared Core Overview**](shared-core/overview.md): C1–C9 map, per-namespace opt-in model, and the two cross-engine contracts.
- [**Autonomy & Governance**](shared-core/autonomy-governance.md): `@governed` confirm-first gates, value/volume ceilings, allowlists, idempotency, kill switch (Contract B).
- [**Entity Resolution**](shared-core/entity-resolution.md): `resolve()` + merge-review queue, survivorship, node-ownership registry.
- [**External-Scope RLS**](shared-core/external-scope-rls.md): External-principal isolation (partner/customer tiers) enforced in the database.
- [**Field Redaction**](shared-core/redaction.md): Allow-list `project(node, surface)` redactor for external surfaces.
- [**Source-Mode & Divergence**](shared-core/source-mode-divergence.md): Per-function `d365 | both | nce` resolver, divergence log, and flip gate.
- [**Pricing, Signing & Grounding**](shared-core/pricing-signing-grounding.md): Deterministic pricing, SignTransport ceremony, and structural grounding guards (C9a/C9b).

---

## Vertical Engine Guides (`nce-vertical-engines`)

All 22 official vertical engines ship in [`sindrehaugen/nce-vertical-engines`](https://github.com/sindrehaugen/nce-vertical-engines) (`v3.5.2`) and register dynamically via `nce.plugins` (`nce.verticals` entry points):

- **Product** — [User Guide](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/engines/product-user.md) · [Admin Guide](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/engines/product-admin.md)
- **Procurement** — [User Guide](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/engines/procurement-user.md) · [Admin Guide](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/engines/procurement-admin.md)
- **Agreements** — [User Guide](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/engines/agreements-user.md) · [Admin Guide](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/engines/agreements-admin.md)
- **Vendors & Contractors** — [User Guide](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/engines/vendors-user.md) · [Admin Guide](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/engines/vendors-admin.md)
- **Sales** — [User Guide](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/engines/sales-user.md) · [Admin Guide](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/engines/sales-admin.md)
- **System Design** — [User Guide](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/engines/system-design-user.md) · [Admin Guide](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/engines/system-design-admin.md)
- **Project** — [User Guide](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/engines/project-user.md) · [Admin Guide](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/engines/project-admin.md)
- **Economy, Assets, Support, Inventory, Field Tech, HR, Marketing, Resources, Business Insights, Customer Portal, Odoo, Salesforce, Diagnostics** — [Complete 22-Engine Documentation Hub](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/README.md) · [Vertical Engine Specifications](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/vertical_engines/00-ENGINES-ROADMAP.md)
- **NetBox** — [Integration & Cognitive Extensions](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/netbox_and_cognitive_extensions.md): topology activation, asset discovery, operator stress tracking, active-learning queue, and Phase-3 cognitive specs.
- **Dynamics 365** — [Integration Reference](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/d365_integration_reference.md): D365/Dataverse admin REST routes and MCP tool surface.

---

## Integrations & Bridges

- [**Service Integrations**](service_integrations.md): SharePoint/OneDrive, Google Drive, and Dropbox webhook flows, subscription lifecycle, retry logic, and dead-letter queue behavior.
- [**Bridge Setup Guide**](bridge_setup_guide.md): OAuth registration and webhook endpoint configuration for all three providers.
- [**Agent-to-Agent (A2A) Protocol**](a2a.md): Secure cross-agent memory sharing via cryptographic handshakes and scoped tokens.

---

## Cognitive Layer

- [**Cognitive Features (Consolidation & Salience)**](cognitive_layer.md): HDBSCAN-based memory "sleep cycle", Ebbinghaus forgetting curve modeling, and contradiction detection.
- [**LLM Providers & Structured Output**](llm_providers.md): Provider-agnostic engine and mandatory Pydantic V2 schema validation for all cognitive tasks.

---

## Data Engineering & Simulation

- [**Memory Time Travel**](time_travel.md): Temporal state reconstruction using the WORM event log and `as_of` querying.
- [**Memory Replay Engine**](replay.md): Observational and forked replay modes for simulation and "What-If" analysis with alternate causal provenance.
- [**System Migrations & Re-embedding**](migrations.md): "Shadow Column" re-embedding strategies, neighbor overlap quality gates, and schema evolution.

---

## Reference, Benchmarks & Supplementary

- [**100k-Row Performance & LLM-Loop Benchmark Report (`v3.5.2`)**](benchmarks/PERF_REPORT_v3.5.2.md): Empirical latency scorecards, sub-stage breakdowns, `EXPLAIN (ANALYZE, BUFFERS)` plans, and 5-tool chained LLM reasoning loop telemetry.
- [**Plugin Architecture & Authoring Guide**](plugin_development.md): `NCEPlugin` protocol, `register_tools`, `register_routes`, `register_rls_tables`, and entry-point packaging.
- [**Plugin Overview (Non-Technical)**](plugin-overview-nontechnical.md): Plain-language overview of NCE plugins for non-technical colleagues.
- [**Vertical Module Pattern**](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/vertical_engines/VERTICAL_MODULE_PATTERN.md): Authoring guide for NCE vertical modules.
- [**API Reference (Curated)**](api_reference.md): Curated HTTP endpoint and MCP tool overview.
- [**API Reference (Auto-Generated)**](API.md): Auto-generated route and tool surface (regenerate via `scripts/gen_api_docs.py`).
- [**Detector Limits & AST Ratchet**](detector_limits.md): Swallowed-DB-write AST scanner scope, 1-level indirection rules, and shrink-only ratchet contract.
- [**Data-Source Modes**](DATA_SOURCE_MODES.md): Per-function `d365 | both | nce` switch — living architecture spec.
- [**Frontend Readiness**](FRONTEND_READINESS.md): Living spec for making NCE fully front-end-ready; NCE-FE-1..6 build list.
- [**Architecture Decision Records**](adr/README.md): Ratified ADRs 0001–0008 (WORM log, Quad-DB, forced RLS, signing v2/v5, shadow re-embedding, env-only master key, snapshot/replay, append-only ticket action log).
- **Auto-Generated Telemetry & Surface Ledgers (`docs/_generated/`)**:
  - [**Canonical Surface Inventory**](_generated/surface.md) · [**Engine Figures**](_generated/engine_figures.md) · [**Golden Thread Seams**](_generated/golden_thread_seams.md) · [**Host Parity Matrix**](_generated/host_parity.md) · [**C12 Resource Surface**](_generated/resource_surface.md) · [**Waves Landed Ledger**](_generated/waves_landed.md)
- **Docsify Site Navigation Chrome**: [`_sidebar.md`](_sidebar.md) · [`_navbar.md`](_navbar.md) · [`_404.md`](_404.md)
- **Repository Governance & Changelogs**: [`../README.md`](../README.md) · [`../CHANGELOG.md`](../CHANGELOG.md) · [`../CONTRIBUTING.md`](../CONTRIBUTING.md) · [`../SECURITY.md`](../SECURITY.md)

