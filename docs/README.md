> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# NCE Documentation Index

Technical specifications, architectural guides, and operational references for NCE v3.0.0.

---

## Getting Started

- [**Quick Start Guide**](quick_start.md): Fastest path from zero to a working MCP server.
- [**Developer Onboarding**](developer_onboarding.md): Local Quad-DB setup, pytest execution, codebase map, and contribution invariants.
- [**Usage Modes**](usage_modes.md): MCP/LLM stdio (JSON-RPC 2.0) vs. Admin REST API — wire-level payload examples for both.

---

## Architecture & System Design

- [**Architecture v1.0 Specification**](architecture-v1.md): Runtime topology, temporal engine, A2A protocol, cognitive workers, GraphRAG pipeline (§7.1), partitioning tradeoffs, and MCP tool surface.
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

## Vertical Engine Guides

Per-engine user + admin runbooks (engines are per-namespace opt-in):

- **Product** — [User Guide](engines/product-user.md) · [Admin Guide](engines/product-admin.md)
- **Procurement** — [User Guide](engines/procurement-user.md) · [Admin Guide](engines/procurement-admin.md)
- **Agreements** — [User Guide](engines/agreements-user.md) · [Admin Guide](engines/agreements-admin.md)
- **Vendors & Contractors** — [User Guide](engines/vendors-user.md) · [Admin Guide](engines/vendors-admin.md)
- **Sales** — [User Guide](engines/sales-user.md) · [Admin Guide](engines/sales-admin.md)
- **System Design** — [User Guide](engines/system-design-user.md) · [Admin Guide](engines/system-design-admin.md)
- **Project** — [User Guide](engines/project-user.md) · [Admin Guide](engines/project-admin.md)
- **NetBox** — [Integration & Cognitive Extensions](netbox_and_cognitive_extensions.md): topology activation, asset discovery, operator stress tracking, active-learning queue, and the Phase-3 cognitive specs (ATMS, chrono-branching, spiking activation).
- **Dynamics 365** — [Integration Reference](d365_integration_reference.md): D365/Dataverse admin REST routes and MCP tool surface.

Design specs for all engines (including not-yet-shipped ones) live under [**Vertical Engine Specifications**](vertical_engines/00-ENGINES-ROADMAP.md).

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

## Reference & Supplementary

- [**API Reference**](API.md): Auto-generated route and tool surface (regenerate via `scripts/gen_api_docs.py`).
- [**Architecture Decision Records**](adr/README.md): Ratified ADRs 0001–0007 (WORM log, Quad-DB, forced RLS, signing v2, shadow re-embedding, env-only master key, snapshot/replay).
- [**Data-Source Modes**](DATA_SOURCE_MODES.md): Per-function `d365 | both | nce` switch — living architecture spec.
- [**Frontend Readiness**](FRONTEND_READINESS.md): Living spec for making NCE fully front-end-ready; NCE-FE-1..6 build list.
- [**Vertical Module Pattern**](vertical_engines/VERTICAL_MODULE_PATTERN.md): Authoring guide for NCE vertical modules.
- [**Plugin Overview (Non-Technical)**](plugin-overview-nontechnical.md): Plain-language overview of NCE plugins for non-technical colleagues.

---

## Planned / Forthcoming

Guides are added as each implementation ships to `main`:

- **Remaining engine guides** — Economy, Assets, Support, Inventory, Field Tech, HR, Marketing, Staff Resources, Business Insights, Customer Portal, Network Ops, Remote Access & RMM. Design specs already live under [**Vertical Engine Specifications**](vertical_engines/00-ENGINES-ROADMAP.md).
- **Cognitive-muscles / diagnostics / innovation docs** — governance-rails, diagnostic log digestion, pipeline governor, self-healing actuation, transport hub (tracking the refactor pipeline as it merges).
- **Day-2 operator runbooks & upgrade guide** — backup/restore, DR, key rotation, version-to-version upgrades.
