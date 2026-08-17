> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# NCE API Reference & Surface Specification

This document provides the definitive, synchronized API specification for the Neuro-Cognitive Engine (NCE) platform at baseline commit `7304330`. It reconciles the dual API surface of NCE:

1. **Admin REST HTTP Surface**: 128 mounted routes served by the Starlette application (`nce/admin_app.py::build_admin_routes()`), authenticated via HMAC-SHA256 signature verification and mutual TLS (mTLS).
2. **Model Context Protocol (MCP) Tool Surface**: 112 tools registered in `nce/tool_registry.py::TOOL_REGISTRY` (46 domain/vertical tools + 66 shared/platform tools) backed by declarative `ToolSpec` dispatch metadata.
3. **Stdio Discovery Gap**: An architectural audit of the 41-tool gap between registered MCP tools (112) and stdio-exposed tools (71 in `nce/mcp_stdio_tools.py::TOOLS`), referencing [`FINDINGS_OQ3_tool_surface.md`](https://github.com/sindrehaugen/NCE/blob/main/FINDINGS_OQ3_tool_surface.md).

---

## Table of Contents

- [1. Surface Architecture & Security Model](#1-surface-architecture--security-model)
  - [1.1 Dual API Topologies](#11-dual-api-topologies)
  - [1.2 Authentication & Authorization Protocols](#12-authentication--authorization-protocols)
  - [1.3 MCP Dispatch Loop & ToolSpec Metadata](#13-mcp-dispatch-loop--toolspec-metadata)
- [2. Surface of Truth Matrix](#2-surface-of-truth-matrix)
- [3. MCP Tool Registry Reference (112 Tools)](#3-mcp-tool-registry-reference-112-tools)
  - [3.1 Shared & Platform Core Tools (66 Tools)](#31-shared--platform-core-tools-66-tools)
  - [3.2 Vertical & Domain Engine Tools (46 Tools)](#32-vertical--domain-engine-tools-46-tools)
- [4. Admin REST Routes Reference (128 Endpoints)](#4-admin-rest-routes-reference-128-endpoints)
  - [4.1 Shared Platform & Administration Routes (84 Routes)](#41-shared-platform--administration-routes-84-routes)
  - [4.2 Vertical Domain Engine Routes (44 Routes)](#42-vertical-domain-engine-routes-44-routes)
- [5. Stdio Tool Discovery Gap Analysis (OQ-3)](#5-stdio-tool-discovery-gap-analysis-oq-3)
  - [5.1 Root Cause & Mechanism](#51-root-cause--mechanism)
  - [5.2 Detailed Inventory of the 41 Missing Stdio Tools](#52-detailed-inventory-of-the-41-missing-stdio-tools)
  - [5.3 Operational Workarounds & Remediation Plan](#53-operational-workarounds--remediation-plan)
- [6. Verification & Drift Prevention Gates](#6-verification--drift-prevention-gates)

---

## 1. Surface Architecture & Security Model

### 1.1 Dual API Topologies

NCE exposes capabilities across two primary access interfaces tailored for distinct consumer topologies:

| Surface Interface | Transport & Format | Target Consumers | Authentication & Guards | Single Source of Truth |
|---|---|---|---|---|
| **Admin REST API** | HTTP/1.1 JSON over TCP | Web BFF, Admin Dashboards, Cron Jobs, Automation Scripts | HMAC-SHA256, NonceStore (Redis SETNX), mTLS (`ADMIN_PRINCIPAL_KIND=employee`), Rate Limiter | `nce.admin_app.build_admin_routes()` (128 routes) |
| **MCP Agent Surface** | JSON-RPC 2.0 (stdio / WebSocket / SSE) | Autonomous AI Agents, Claude Desktop, Antigravity Swarms | API Keys, Tenant Namespace RLS, Dispatch Gating (`admin_only`, `mutation`, `cacheable`, `migration`) | `nce.tool_registry.TOOL_REGISTRY` (112 tools) |

### 1.2 Authentication & Authorization Protocols

All endpoints under `/api/` are guarded by `HMACAuthMiddleware` (`nce/auth.py`) and zero-trust transport checks:

1. **HMAC Signature Headers**:
   - `X-NCE-Timestamp`: Unix epoch integer in UTC. Timestamps outside ±90 seconds (`NCE_CLOCK_SKEW_TOLERANCE_S`) are rejected with JSON-RPC code `-32002` (`replay_or_clock_skew`).
   - `X-NCE-Nonce`: Unique UUIDv4 token checked against Redis via `SET nce:nonce:<nonce> 1 NX PX 180000`. Replayed nonces within the TTL window are rejected with `replay_nonce_conflict`.
   - `Authorization`: Header formatted as `HMAC-SHA256 <hex_signature>` computed over `METHOD\nPATH\nTIMESTAMP[\nSHA256_HEX(raw_body)]` with shared secret `NCE_API_KEY`.
2. **Principal Model & RLS Context**:
   - Admin endpoints operate under `ADMIN_PRINCIPAL_KIND = "employee"`. The Postgres session sets `nce.namespace_id` but leaves `nce.external_scope_id` unset, enforcing default-deny on external customer tables while allowing internal service orchestration.
3. **Public Seams**:
   - `/public-api/sales/quotes/{id}` is explicitly unauthenticated for customer portal quote viewing.
   - Health and UI endpoints (`/healthz`, `/`, `/styles.css`) are exempt from HMAC requirements.

### 1.3 MCP Dispatch Loop & ToolSpec Metadata

The MCP dispatch server (`nce/mcp_stdio_dispatch.py`) utilizes declarative metadata encapsulated in `ToolSpec` (`nce/tool_registry.py`):

- `handler`: Late-binding async coroutine resolving at call time (`_h(module, attr)`).
- `admin_only`: When `True`, requires administrative role verification before execution.
- `cacheable`: When `True`, cache-eligible responses are stored in Redis (`TTL = MCP_CACHE_TTL_S`).
- `mutation`: When `True`, automatically increments the global Redis cache-generation counter before invocation.
- `migration`: When `True`, gated by `cfg.NCE_DISABLE_MIGRATION_MCP` during live maintenance.

---

## 2. Surface of Truth Matrix

Reconciled against `docs/_generated/surface.md` and codebase commit `7304330`:

| Engine / Domain | MCP Tools Count | Mounted REST Routes Count | Cores (`do_*`) / Handlers | Status |
|---|---|---|---|---|
| **agreements** | 1 | 5 | Agreements extraction, review, coverage analysis | Shipped |
| **diagnostics** | 5 | 0 | Log bundle ingestion, commit, anomaly detection, device health | Shipped |
| **dynamics365** | 6 | 0 | Case queries, SLA breach tracking, NetBox mapping sync | Shipped |
| **economy** | 3 | 3 | do_compute_bucket_targets, do_compute_dunning, do_compute_recognition_schedule, do_emit_financial_event, do_forecast_cashflow, do_generate_kid, do_match_invoice, do_snapshot_mrr_arr_churn, do_validate_kid | Shipped |
| **inventory** | 0 | 0 | Stock locations, inventory items (M11.W1 tables staged) | In-Progress |
| **netbox** | 1 | 0 | Circuit impact evaluation & topology mapping | Shipped |
| **procurement** | 6 | 8 | do_calculate_tco, do_evaluate_three_way_match, do_rank_suppliers | Shipped |
| **product** | 6 | 3 | Product search, price resolution, BOM line matching, PIM enrichment | Shipped |
| **project** | 4 | 7 | Phase-gate checks, Sales→Project conversion, PL advisor | Shipped |
| **sales** | 2 | 15 | Quote lifecycle, target configuration, signed baseline reads | Shipped |
| **system_design** | 2 | 1 | System design verification, Lucidchart design doc export | Shipped |
| **vendors** | 10 | 2 | Vendor scorecards, tier status, contractor matching, reliability radar | Shipped |
| **shared / core** | 66 | 84 | Tri-Stack memory, GraphRAG, A2A grants, entity resolution, migrations, admin | Shipped |
| **Total Surface** | **112 Tools** | **128 Routes** | **Core Domain Engine Operations** | **Baseline 7304330** |

---

## 3. MCP Tool Registry Reference (112 Tools)

The `TOOL_REGISTRY` contains 112 tools divided into 66 shared/platform core tools and 46 vertical domain engine tools.

### 3.1 Shared & Platform Core Tools (66 Tools)

| Tool Name | Handler Target | Admin Only | Mutation | Cacheable | Migration Gated | In Stdio (`TOOLS`) |
|---|---|---|---|---|---|---|
| `a2a_create_grant` | `a2a_mcp_handlers.handle_a2a_create_grant` | — | ✅ Yes | — | — | ✅ Stdio |
| `a2a_inspect_grant` | `a2a_mcp_handlers.handle_a2a_inspect_grant` | — | — | — | — | ✅ Stdio |
| `a2a_list_grants` | `a2a_mcp_handlers.handle_a2a_list_grants` | — | — | — | — | ✅ Stdio |
| `a2a_query_shared` | `a2a_mcp_handlers.handle_a2a_query_shared` | — | — | — | — | ✅ Stdio |
| `a2a_revoke_grant` | `a2a_mcp_handlers.handle_a2a_revoke_grant` | — | ✅ Yes | — | — | ✅ Stdio |
| `a2a_update_grant_scopes` | `a2a_mcp_handlers.handle_a2a_update_grant_scopes` | — | ✅ Yes | — | — | ✅ Stdio |
| `a2a_verify_grant_status` | `a2a_mcp_handlers.handle_a2a_verify_grant_status` | — | — | — | — | ✅ Stdio |
| `abort_migration` | `migration_mcp_handlers.handle_abort_migration` | — | ✅ Yes | — | ✅ Yes | ✅ Stdio |
| `boost_memory` | `memory_mcp_handlers.handle_boost_memory` | — | ✅ Yes | — | — | ✅ Stdio |
| `bridge_status` | `bridge_mcp_handlers.bridge_status` | — | — | — | — | ✅ Stdio |
| `check_indexing_status` | `code_mcp_handlers.handle_check_indexing_status` | — | — | — | — | ✅ Stdio |
| `commit_migration` | `migration_mcp_handlers.handle_commit_migration` | — | ✅ Yes | — | ✅ Yes | ✅ Stdio |
| `compare_states` | `snapshot_mcp_handlers.handle_compare_states` | — | — | — | — | ✅ Stdio |
| `complete_bridge_auth` | `bridge_mcp_handlers.complete_bridge_auth` | — | ✅ Yes | — | — | ✅ Stdio |
| `connect_bridge` | `bridge_mcp_handlers.connect_bridge` | — | ✅ Yes | — | — | ✅ Stdio |
| `consolidation_status` | `admin_mcp_handlers.handle_consolidation_status` | — | — | — | — | ✅ Stdio |
| `create_snapshot` | `snapshot_mcp_handlers.handle_create_snapshot` | — | ✅ Yes | — | — | ✅ Stdio |
| `delete_snapshot` | `snapshot_mcp_handlers.handle_delete_snapshot` | — | ✅ Yes | — | — | ✅ Stdio |
| `describe_schema` | `catalog_mcp_handlers.handle_describe_schema` | — | — | — | — | ✅ Stdio |
| `detect_causal_cycles` | `replay_mcp_handlers.handle_detect_causal_cycles` | ✅ Yes | — | — | — | ⚠️ Missing (OQ-3) |
| `disconnect_bridge` | `bridge_mcp_handlers.disconnect_bridge` | — | ✅ Yes | — | — | ✅ Stdio |
| `execute_query_template` | `catalog_mcp_handlers.handle_execute_query_template` | — | — | — | — | ✅ Stdio |
| `explain_config_change` | `settings_mcp_handlers.handle_explain_config_change` | ✅ Yes | — | — | — | ✅ Stdio |
| `explain_memory` | `replay_mcp_handlers.handle_explain_memory` | — | — | — | — | ✅ Stdio |
| `explain_past_decision` | `replay_mcp_handlers.handle_explain_past_decision` | ✅ Yes | ✅ Yes | — | — | ✅ Stdio |
| `force_resync_bridge` | `bridge_mcp_handlers.force_resync_bridge` | — | ✅ Yes | — | — | ✅ Stdio |
| `forget_memory` | `memory_mcp_handlers.handle_forget_memory` | — | ✅ Yes | — | — | ✅ Stdio |
| `get_event_provenance` | `replay_mcp_handlers.handle_get_event_provenance` | — | — | — | — | ✅ Stdio |
| `get_health` | `admin_mcp_handlers.handle_get_health` | — | — | — | — | ✅ Stdio |
| `get_recent_context` | `memory_mcp_handlers.handle_get_recent_context` | — | — | — | — | ✅ Stdio |
| `graph_search` | `graph_mcp_handlers.handle_graph_search` | — | — | ✅ Yes | — | ✅ Stdio |
| `import_snapshot` | `snapshot_mcp_handlers.handle_import_snapshot` | — | ✅ Yes | — | — | ✅ Stdio |
| `index_code_file` | `code_mcp_handlers.handle_index_code_file` | — | ✅ Yes | — | — | ✅ Stdio |
| `list_bridges` | `bridge_mcp_handlers.list_bridges` | — | — | — | — | ✅ Stdio |
| `list_contradictions` | `contradiction_mcp_handlers.handle_list_contradictions` | — | — | — | — | ✅ Stdio |
| `list_dlq` | `admin_mcp_handlers.handle_list_dlq` | — | — | — | — | ✅ Stdio |
| `list_snapshots` | `snapshot_mcp_handlers.handle_list_snapshots` | — | — | — | — | ✅ Stdio |
| `manage_namespace` | `admin_mcp_handlers.handle_manage_namespace` | — | ✅ Yes | — | — | ✅ Stdio |
| `manage_quotas` | `admin_mcp_handlers.handle_manage_quotas` | — | ✅ Yes | — | — | ✅ Stdio |
| `merge_queue_confirm` | `entity_resolution_mcp_handlers.handle_merge_queue_confirm` | ✅ Yes | ✅ Yes | — | — | ⚠️ Missing (OQ-3) |
| `merge_queue_list` | `entity_resolution_mcp_handlers.handle_merge_queue_list` | — | — | ✅ Yes | — | ⚠️ Missing (OQ-3) |
| `merge_queue_reject` | `entity_resolution_mcp_handlers.handle_merge_queue_reject` | ✅ Yes | ✅ Yes | — | — | ⚠️ Missing (OQ-3) |
| `migration_status` | `migration_mcp_handlers.handle_migration_status` | — | — | — | ✅ Yes | ✅ Stdio |
| `neuromorphic_search` | `graph_mcp_handlers.handle_neuromorphic_search` | — | — | ✅ Yes | — | ✅ Stdio |
| `pricing_resolve` | `pricing_mcp_handlers.handle_pricing_resolve` | — | — | ✅ Yes | — | ⚠️ Missing (OQ-3) |
| `purge_dlq` | `admin_mcp_handlers.handle_purge_dlq` | — | ✅ Yes | — | — | ✅ Stdio |
| `replay_dlq` | `admin_mcp_handlers.handle_replay_dlq` | — | ✅ Yes | — | — | ✅ Stdio |
| `replay_fork` | `replay_mcp_handlers.handle_replay_fork` | ✅ Yes | — | — | — | ✅ Stdio |
| `replay_observe` | `replay_mcp_handlers.handle_replay_observe` | ✅ Yes | — | — | — | ✅ Stdio |
| `replay_reconstruct` | `replay_mcp_handlers.handle_replay_reconstruct` | ✅ Yes | ✅ Yes | — | — | ✅ Stdio |
| `replay_status` | `replay_mcp_handlers.handle_replay_status` | ✅ Yes | — | — | — | ✅ Stdio |
| `resolve` | `entity_resolution_mcp_handlers.handle_resolve` | — | — | ✅ Yes | — | ⚠️ Missing (OQ-3) |
| `resolve_contradiction` | `contradiction_mcp_handlers.handle_resolve_contradiction` | — | ✅ Yes | — | — | ✅ Stdio |
| `rotate_signing_key` | `admin_mcp_handlers.handle_rotate_signing_key` | — | ✅ Yes | — | — | ✅ Stdio |
| `search_codebase` | `code_mcp_handlers.handle_search_codebase` | — | — | ✅ Yes | — | ✅ Stdio |
| `semantic_search` | `memory_mcp_handlers.handle_semantic_search` | — | — | ✅ Yes | — | ✅ Stdio |
| `shred_memory` | `memory_mcp_handlers.handle_shred_memory` | ✅ Yes | ✅ Yes | — | — | ✅ Stdio |
| `start_migration` | `migration_mcp_handlers.handle_start_migration` | — | ✅ Yes | — | ✅ Yes | ✅ Stdio |
| `store_artifact` | `memory_mcp_handlers.handle_store_artifact` | — | ✅ Yes | — | — | ✅ Stdio |
| `store_media` | `memory_mcp_handlers.handle_store_media` | — | ✅ Yes | — | — | ✅ Stdio |
| `store_memory` | `memory_mcp_handlers.handle_store_memory` | — | ✅ Yes | — | — | ✅ Stdio |
| `suggest_queries` | `catalog_mcp_handlers.handle_suggest_queries` | — | — | — | — | ✅ Stdio |
| `trigger_consolidation` | `admin_mcp_handlers.handle_trigger_consolidation` | — | ✅ Yes | — | — | ✅ Stdio |
| `unredact_memory` | `memory_mcp_handlers.handle_unredact_memory` | ✅ Yes | ✅ Yes | — | — | ✅ Stdio |
| `validate_migration` | `migration_mcp_handlers.handle_validate_migration` | — | — | — | ✅ Yes | ✅ Stdio |
| `verify_memory` | `admin_mcp_handlers.handle_verify_memory` | — | — | — | — | ✅ Stdio |

#### Detailed Functional Grouping of Shared Platform Tools

##### Memory & Persistence (Tri-Stack)
- **`store_memory`** *(Exposed in stdio)*: Persist a memory (conversation turn, document, or summary) to MongoDB, PostgreSQL vector index, and Redis summary cache. Supports contradiction checks.
- **`store_artifact`** *(Exposed in stdio)*: Ingest large binary artifacts (media, PDFs, diagnostic logs) into object storage and associate metadata with the Quad-Stack.
- **`store_media`** *(Exposed in stdio)*: Store media payloads with tenant-isolated S3/MinIO reference pointers.
- **`semantic_search`** *(Exposed in stdio)*: Execute cosine similarity vector search over pgvector memory embeddings.
- **`get_recent_context`** *(Exposed in stdio)*: Retrieve the N most recent episodic memory entries for an agent session.
- **`boost_memory`** *(Exposed in stdio)*: Boost salience weight of a memory for attention ranking in context synthesis.
- **`forget_memory`** *(Exposed in stdio)*: Soft-delete or decay salience to 0.0 for an agent memory.
- **`unredact_memory`** *(Exposed in stdio)*: Administrative decryption and de-pseudonymization of PII-redacted memory entities.
- **`shred_memory`** *(Exposed in stdio)*: Cryptographic hard deletion and zeroization of memory nodes across all storage engines.

##### Code Indexing & Exploration
- **`index_code_file`** *(Exposed in stdio)*: Parse source code AST, chunk, embed, and asynchronously index into pgvector.
- **`check_indexing_status`** *(Exposed in stdio)*: Check the progress and error status of a background codebase indexing job.
- **`search_codebase`** *(Exposed in stdio)*: Execute semantic code search across indexed syntax chunks with file path anchors.

##### Knowledge Graph & GraphRAG
- **`graph_search`** *(Exposed in stdio)*: Execute GraphRAG breadth-first search (BFS) traversal over the knowledge graph (`kg_nodes`, `kg_edges`).
- **`neuromorphic_search`** *(Exposed in stdio)*: Hybrid neuromorphic activation spread over knowledge graph associative clusters.

##### External Document Bridges (OAuth)
- **`connect_bridge`** *(Exposed in stdio)*: Initiate OAuth connection flow for SharePoint, Google Drive, or Dropbox document bridges.
- **`complete_bridge_auth`** *(Exposed in stdio)*: Exchange OAuth authorization code, store refresh token, and activate bridge subscription.
- **`list_bridges`** *(Exposed in stdio)*: List active document bridge subscriptions for the current tenant.
- **`disconnect_bridge`** *(Exposed in stdio)*: Revoke provider subscription and transition bridge state to DISCONNECTED.
- **`force_resync_bridge`** *(Exposed in stdio)*: Reset stored sync cursor and enqueue full re-synchronization of document hierarchy.
- **`bridge_status`** *(Exposed in stdio)*: Return live subscription health, last sync cursor, and OAuth token expiry hint.

##### Contradiction Detection & Resolution
- **`list_contradictions`** *(Exposed in stdio)*: List detected semantic contradictions and knowledge graph conflicts.
- **`resolve_contradiction`** *(Exposed in stdio)*: Resolve a detected contradiction with explicit tenant RLS and audit trail.

##### Embedding Migrations (Zero-Downtime)
- **`start_migration`** *(Exposed in stdio)*: Initiate background re-embedding of memory corpus to a new vector model.
- **`migration_status`** *(Exposed in stdio)*: Poll progress, ETA, and error rates of an ongoing embedding migration.
- **`validate_migration`** *(Exposed in stdio)*: Run cosine quality gate benchmarks against dual-embedded shadow memories.
- **`commit_migration`** *(Exposed in stdio)*: Atomic cutover to new embedding model version; bumps schema epoch.
- **`abort_migration`** *(Exposed in stdio)*: Abort in-flight embedding migration and purge temporary vector tables.

##### Memory Replay & Causal Provenance
- **`replay_observe`** *(Exposed in stdio)*: Stream historical event log records in read-only audit mode.
- **`replay_reconstruct`** *(Exposed in stdio)*: Reconstruct byte-identical state at a historical point in time by replaying event stream.
- **`replay_fork`** *(Exposed in stdio)*: Fork a tenant namespace from historical event log offset into a sandbox namespace.
- **`replay_status`** *(Exposed in stdio)*: Poll execution status and record counter of an active replay task.
- **`get_event_provenance`** *(Exposed in stdio)*: Return complete upstream and downstream causal event graph for a memory entity.
- **`explain_memory`** *(Exposed in stdio)*: Generate causal trace explanation for how a memory was formed.
- **`explain_past_decision`** *(Exposed in stdio)*: Replay decision context and model inputs to explain past agent action.
- **`detect_causal_cycles`** *(⚠️ Undiscovered in stdio - OQ-3)*: Analyze causal graph for circular event dependencies or causal loops.

##### Agent-to-Agent (A2A) Protocols
- **`a2a_create_grant`** *(Exposed in stdio)*: Issue cryptographically signed sharing grant allowing another agent scoped read access.
- **`a2a_revoke_grant`** *(Exposed in stdio)*: Revoke active A2A grant token immediately across cluster.
- **`a2a_list_grants`** *(Exposed in stdio)*: List all active and expired A2A sharing grants owned by namespace.
- **`a2a_query_shared`** *(Exposed in stdio)*: Execute semantic vector search against another agent namespace via valid A2A grant token.
- **`a2a_verify_grant_status`** *(Exposed in stdio)*: Verify validity, remaining TTL, and scope permissions of an A2A token.
- **`a2a_update_grant_scopes`** *(Exposed in stdio)*: Modify resource access scopes on an active grant.
- **`a2a_inspect_grant`** *(Exposed in stdio)*: Inspect grant metadata, issuing authority, and token audit history.

##### Entity Resolution & Pricing (C1/C7 Foundations)
- **`pricing_resolve`** *(⚠️ Undiscovered in stdio - OQ-3)*: Resolve real-time product/part pricing across vendor pricebooks and currency conversions.
- **`resolve`** *(⚠️ Undiscovered in stdio - OQ-3)*: Resolve entity cross-references and fuzzy matches across ERP, CRM, and PIM.
- **`merge_queue_list`** *(⚠️ Undiscovered in stdio - OQ-3)*: List candidate entity merge pairs pending human-in-the-loop review.
- **`merge_queue_confirm`** *(⚠️ Undiscovered in stdio - OQ-3)*: Approve entity merge, triggering graph edge unification and alias recording.
- **`merge_queue_reject`** *(⚠️ Undiscovered in stdio - OQ-3)*: Reject proposed entity merge and mark pair as distinct.

##### Platform Operations & Dead-Letter Queue
- **`manage_namespace`** *(Exposed in stdio)*: Create, list, update metadata, or configure tenant namespace parameters.
- **`verify_memory`** *(Exposed in stdio)*: Verify HMAC signature and causal hash chain integrity of a memory record.
- **`trigger_consolidation`** *(Exposed in stdio)*: Trigger memory consolidation, sleep-phase summarization, and pruning.
- **`consolidation_status`** *(Exposed in stdio)*: Check status and progress of memory consolidation cycle.
- **`manage_quotas`** *(Exposed in stdio)*: Configure or inspect tenant resource quotas (token caps, vector dimensions, rate limits).
- **`rotate_signing_key`** *(Exposed in stdio)*: Generate new cryptographic signing key pair and retire active key with grace period.
- **`get_health`** *(Exposed in stdio)*: Comprehensive system health diagnostic covering DB connections, Redis, and workers.
- **`list_dlq`** *(Exposed in stdio)*: List dead-letter queue entries for failed background operations and event syncs.
- **`replay_dlq`** *(Exposed in stdio)*: Re-enqueue failed DLQ entry for retry processing.
- **`purge_dlq`** *(Exposed in stdio)*: Permanently discard dead-letter queue message entry.

##### Snapshots & Query Catalog
- **`create_snapshot`** *(Exposed in stdio)*: Create point-in-time state snapshot of a namespace memory graph.
- **`list_snapshots`** *(Exposed in stdio)*: List available state snapshots for a namespace.
- **`delete_snapshot`** *(Exposed in stdio)*: Delete stored point-in-time state snapshot.
- **`compare_states`** *(Exposed in stdio)*: Diff memory state graphs between two snapshot timestamps.
- **`import_snapshot`** *(Exposed in stdio)*: Restore or import memory snapshot into a namespace.
- **`suggest_queries`** *(Exposed in stdio)*: Retrieve optimized SQL/vector query templates based on natural language intent.
- **`execute_query_template`** *(Exposed in stdio)*: Execute parameterized query template against tenant store.
- **`describe_schema`** *(Exposed in stdio)*: Describe relational and graph database schemas.

##### Settings & Configuration
- **`explain_config_change`** *(Exposed in stdio)*: Audit and explain history of dynamic configuration overrides and diffs.

### 3.2 Vertical & Domain Engine Tools (46 Tools)

| Engine | Tool Name | Handler Target | Admin Only | Mutation | Cacheable | In Stdio (`TOOLS`) |
|---|---|---|---|---|---|---|
| **agreements** | `agreements_lookup_terms` | `agreements_mcp_handlers.handle_agreements_lookup_terms` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **diagnostics** | `diag_ingest_bundle` | `diag_mcp_handlers.handle_diag_ingest_bundle` | — | ✅ Yes | — | ✅ Stdio |
| **diagnostics** | `diag_commit_bundle` | `diag_mcp_handlers.handle_diag_commit_bundle` | — | ✅ Yes | — | ✅ Stdio |
| **diagnostics** | `diag_digest_status` | `diag_mcp_handlers.handle_diag_digest_status` | — | — | ✅ Yes | ✅ Stdio |
| **diagnostics** | `diag_device_health` | `diag_mcp_handlers.handle_diag_device_health` | — | — | ✅ Yes | ✅ Stdio |
| **diagnostics** | `diag_list_anomalies` | `diag_mcp_handlers.handle_diag_list_anomalies` | — | — | ✅ Yes | ✅ Stdio |
| **dynamics365** | `d365_query_case` | `d365_mcp_handlers.handle_d365_query_case` | — | — | ✅ Yes | ✅ Stdio |
| **dynamics365** | `d365_sync_now` | `d365_mcp_handlers.handle_d365_sync_now` | ✅ Yes | ✅ Yes | — | ✅ Stdio |
| **dynamics365** | `d365_case_stress_report` | `d365_mcp_handlers.handle_d365_case_stress_report` | — | — | ✅ Yes | ✅ Stdio |
| **dynamics365** | `d365_list_sla_breaches` | `d365_mcp_handlers.handle_d365_list_sla_breaches` | ✅ Yes | — | — | ✅ Stdio |
| **dynamics365** | `d365_netbox_mappings` | `d365_mcp_handlers.handle_d365_netbox_mappings` | — | — | ✅ Yes | ✅ Stdio |
| **dynamics365** | `d365_sync_status` | `d365_mcp_handlers.handle_d365_sync_status` | — | — | — | ⚠️ Missing (OQ-3) |
| **economy** | `economy_match_invoice` | `economy_mcp_handlers.handle_economy_match_invoice` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **economy** | `economy_compute_periodisering` | `economy_mcp_handlers.handle_economy_compute_periodisering` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **economy** | `economy_emit_event` | `economy_mcp_handlers.handle_economy_emit_event` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **netbox** | `evaluate_circuit_impact` | `netbox_circuits.handle_evaluate_circuit_impact` | — | — | — | ✅ Stdio |
| **procurement** | `procurement_calculate_tco` | `procurement_mcp_handlers.handle_procurement_calculate_tco` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **procurement** | `procurement_rank_suppliers` | `procurement_mcp_handlers.handle_procurement_rank_suppliers` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **procurement** | `procurement_evaluate_match` | `procurement_mcp_handlers.handle_procurement_evaluate_match` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **procurement** | `procurement_forecast_rebate` | `procurement_mcp_handlers.handle_procurement_forecast_rebate` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **procurement** | `procurement_recommend_move_spend` | `procurement_mcp_handlers.handle_procurement_recommend_move_spend` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **procurement** | `procurement_whatif_spend` | `procurement_mcp_handlers.handle_procurement_whatif_spend` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **product** | `product_search` | `product_mcp_handlers.handle_product_search` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **product** | `product_get` | `product_mcp_handlers.handle_product_get` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **product** | `product_price` | `product_mcp_handlers.handle_product_price` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **product** | `product_related` | `product_mcp_handlers.handle_product_related` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **product** | `product_match_bom_line` | `product_mcp_handlers.handle_product_match_bom_line` | — | — | — | ⚠️ Missing (OQ-3) |
| **product** | `product_enrich` | `product_mcp_handlers.handle_product_enrich` | — | ✅ Yes | — | ⚠️ Missing (OQ-3) |
| **project** | `project_can_enter_phase` | `project_mcp_handlers.handle_project_can_enter_phase` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **project** | `project_convert_signed_quote` | `project_mcp_handlers.handle_project_convert_signed_quote` | ✅ Yes | ✅ Yes | — | ⚠️ Missing (OQ-3) |
| **project** | `project_advance_phase` | `project_mcp_handlers.handle_project_advance_phase` | ✅ Yes | ✅ Yes | — | ⚠️ Missing (OQ-3) |
| **project** | `project_suggest_pl` | `project_mcp_handlers.handle_project_suggest_pl` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **sales** | `sales_ping` | `sales_mcp_handlers.handle_sales_ping` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **sales** | `sales_get_signed_baseline` | `sales_mcp_handlers.handle_sales_get_signed_baseline` | — | — | — | ⚠️ Missing (OQ-3) |
| **system_design** | `system_design_ping` | `system_design_mcp_handlers.handle_system_design_ping` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **system_design** | `system_design_publish_design_docs` | `system_design_mcp_handlers.handle_system_design_publish_design_docs` | — | ✅ Yes | — | ⚠️ Missing (OQ-3) |
| **vendors** | `vendors_get_vendor` | `vendors_mcp_handlers.handle_vendors_get_vendor` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **vendors** | `vendors_compute_scorecard` | `vendors_mcp_handlers.handle_vendors_compute_scorecard` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **vendors** | `vendors_get_tier_status` | `vendors_mcp_handlers.handle_vendors_get_tier_status` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **vendors** | `vendors_detect_reliability_degradation` | `vendors_mcp_handlers.handle_vendors_detect_reliability_degradation` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **vendors** | `vendors_check_tier_at_risk` | `vendors_mcp_handlers.handle_vendors_check_tier_at_risk` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **vendors** | `vendors_match_contractor` | `vendors_mcp_handlers.handle_vendors_match_contractor` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **vendors** | `vendors_compute_performance` | `vendors_mcp_handlers.handle_vendors_compute_performance` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **vendors** | `vendors_recall_similar_jobs` | `vendors_mcp_handlers.handle_vendors_recall_similar_jobs` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **vendors** | `vendors_reliability_radar` | `vendors_mcp_handlers.handle_vendors_reliability_radar` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |
| **vendors** | `vendors_calibrate_weights` | `vendors_mcp_handlers.handle_vendors_calibrate_weights` | — | — | ✅ Yes | ⚠️ Missing (OQ-3) |

#### Detailed Specifications of Vertical Engine Tools

##### Agreements Engine (1 Tool)
- **`agreements_lookup_terms`** *(⚠️ Undiscovered in stdio - OQ-3)*: Lookup structured contract clauses, SLA commitments, warranty terms, and renewal covenants for a customer or vendor agreement.

##### Diagnostic Log Digestion Engine (5 Tools)
- **`diag_ingest_bundle`** *(Exposed in stdio)*: Ingest and parse raw switch/device diagnostic bundles into structured event records.
- **`diag_commit_bundle`** *(Exposed in stdio)*: Commit parsed diagnostic logs into permanent time-series and graph memory.
- **`diag_digest_status`** *(Exposed in stdio)*: Query status and progress of background diagnostic log bundle ingestion.
- **`diag_device_health`** *(Exposed in stdio)*: Compute holistic device health score and anomaly indicators from diagnostic stream.
- **`diag_list_anomalies`** *(Exposed in stdio)*: List detected hardware, protocol (VRRP/STP/LLDP), and interface anomalies.

##### Dynamics 365 / Dataverse Engine (6 Tools)
- **`d365_query_case`** *(Exposed in stdio)*: Query CRM support cases, service tickets, and account history from Dynamics 365 / Dataverse.
- **`d365_sync_now`** *(Exposed in stdio)*: Trigger immediate bidirectional synchronization between Dataverse and NCE memory graph.
- **`d365_case_stress_report`** *(Exposed in stdio)*: Generate customer account stress index based on open cases, severity, and escalations.
- **`d365_list_sla_breaches`** *(Exposed in stdio)*: List active or impending SLA breaches across all open support cases.
- **`d365_netbox_mappings`** *(Exposed in stdio)*: Retrieve mapping table correlating D365 service accounts with NetBox site/rack topologies.
- **`d365_sync_status`** *(⚠️ Undiscovered in stdio - OQ-3)*: Inspect status, error logs, and last-synced timestamp of Dynamics 365 sync worker.

##### Economy Engine (3 Tools)
- **`economy_match_invoice`** *(⚠️ Undiscovered in stdio - OQ-3)*: Perform automated 3-way match between vendor invoice, purchase order, and receipt records.
- **`economy_compute_periodisering`** *(⚠️ Undiscovered in stdio - OQ-3)*: Calculate periodized revenue recognition schedules and deferred expense amortizations.
- **`economy_emit_event`** *(⚠️ Undiscovered in stdio - OQ-3)*: Emit financial event (billing, posting, payment) to immutable ledger and event log.

##### NetBox Network Operations Engine (1 Tool)
- **`evaluate_circuit_impact`** *(Exposed in stdio)*: Evaluate circuit maintenance or outage impact across downstream switches, VLANs, and endpoints.

##### Procurement Engine (6 Tools)
- **`procurement_calculate_tco`** *(⚠️ Undiscovered in stdio - OQ-3)*: Calculate Total Cost of Ownership (TCO) comparing equipment, maintenance, and energy across suppliers.
- **`procurement_rank_suppliers`** *(⚠️ Undiscovered in stdio - OQ-3)*: Rank qualified suppliers using multi-criteria weighted scoring (price, lead time, reliability).
- **`procurement_evaluate_match`** *(⚠️ Undiscovered in stdio - OQ-3)*: Evaluate 3-way invoice-PO-delivery match discrepancy tolerance.
- **`procurement_forecast_rebate`** *(⚠️ Undiscovered in stdio - OQ-3)*: Forecast vendor volume rebates and tier threshold achievement.
- **`procurement_recommend_move_spend`** *(⚠️ Undiscovered in stdio - OQ-3)*: Recommend spend reallocation across vendor agreements to maximize tier discounts.
- **`procurement_whatif_spend`** *(⚠️ Undiscovered in stdio - OQ-3)*: Simulate what-if procurement scenarios under varying volume and currency assumptions.

##### Product PIM Engine (6 Tools)
- **`product_search`** *(⚠️ Undiscovered in stdio - OQ-3)*: Search product catalog by SKU, description, category, or semantic compatibility query.
- **`product_get`** *(⚠️ Undiscovered in stdio - OQ-3)*: Retrieve complete product master record including attributes, dimensions, and datasheets.
- **`product_price`** *(⚠️ Undiscovered in stdio - OQ-3)*: Resolve real-time product price book including customer discount matrices.
- **`product_related`** *(⚠️ Undiscovered in stdio - OQ-3)*: Find compatible accessories, replacement parts, and alternative SKUs.
- **`product_match_bom_line`** *(⚠️ Undiscovered in stdio - OQ-3)*: Match raw BOM line descriptions to canonical product catalog entries.
- **`product_enrich`** *(⚠️ Undiscovered in stdio - OQ-3)*: Trigger on-demand AI PIM attribute enrichment, translation, and specification extraction.

##### Project Engine (4 Tools)
- **`project_can_enter_phase`** *(⚠️ Undiscovered in stdio - OQ-3)*: Evaluate phase-gate transition rules and prerequisite readiness criteria.
- **`project_convert_signed_quote`** *(⚠️ Undiscovered in stdio - OQ-3)*: Convert signed sales quote and baseline BOM into active delivery project.
- **`project_advance_phase`** *(⚠️ Undiscovered in stdio - OQ-3)*: Advance project to subsequent execution phase with stage-gate validation.
- **`project_suggest_pl`** *(⚠️ Undiscovered in stdio - OQ-3)*: Analyze project requirements and suggest optimal Project Lead staffing based on skills and capacity.

##### Sales Engine (2 Tools)
- **`sales_ping`** *(⚠️ Undiscovered in stdio - OQ-3)*: Health and connectivity probe for Sales vertical engine.
- **`sales_get_signed_baseline`** *(⚠️ Undiscovered in stdio - OQ-3)*: Retrieve immutable signed baseline quote snapshot for downstream project provisioning.

##### System Design Engine (2 Tools)
- **`system_design_ping`** *(⚠️ Undiscovered in stdio - OQ-3)*: Health probe for System Design engine.
- **`system_design_publish_design_docs`** *(⚠️ Undiscovered in stdio - OQ-3)*: Publish validated system design drawings and bills of materials to Lucidchart / external repositories.

##### Vendors Engine (10 Tools)
- **`vendors_get_vendor`** *(⚠️ Undiscovered in stdio - OQ-3)*: Retrieve vendor master profile, credentials, and contracted service terms.
- **`vendors_compute_scorecard`** *(⚠️ Undiscovered in stdio - OQ-3)*: Generate aggregate vendor scorecard covering on-time delivery, defect rate, and responsiveness.
- **`vendors_get_tier_status`** *(⚠️ Undiscovered in stdio - OQ-3)*: Check current volume discount tier status and required spend for next tier.
- **`vendors_detect_reliability_degradation`** *(⚠️ Undiscovered in stdio - OQ-3)*: Detect statistically significant degradation in vendor delivery or failure metrics.
- **`vendors_check_tier_at_risk`** *(⚠️ Undiscovered in stdio - OQ-3)*: Identify vendor agreements at risk of missing annual minimum spend thresholds.
- **`vendors_match_contractor`** *(⚠️ Undiscovered in stdio - OQ-3)*: Match external field contractor capabilities to project work orders and regional availability.
- **`vendors_compute_performance`** *(⚠️ Undiscovered in stdio - OQ-3)*: Compute historical performance trends and defect recurrence for a contractor or vendor.
- **`vendors_recall_similar_jobs`** *(⚠️ Undiscovered in stdio - OQ-3)*: Retrieve similar past installation or integration jobs completed by a vendor.
- **`vendors_reliability_radar`** *(⚠️ Undiscovered in stdio - OQ-3)*: Multi-axis reliability visualization comparing vendor performance across operational categories.
- **`vendors_calibrate_weights`** *(⚠️ Undiscovered in stdio - OQ-3)*: Calibrate scorecard evaluation weights against business priorities.

---

## 4. Admin REST Routes Reference (128 Endpoints)

All 128 routes are mounted in `nce/admin_app.py::build_admin_routes()` and served by the Starlette application.

### 4.1 Shared Platform & Administration Routes (84 Routes)

| Method | Path | Handler Endpoint | Subsystem / Function |
|---|---|---|---|
| `GET` | `/` | `h.serve_index` | Admin dashboard web UI root |
| `GET` | `/healthz` | `get_healthz` | Kubernetes / load balancer liveness probe |
| `GET` | `/styles.css` | `h.serve_styles` | Admin dashboard stylesheet asset |
| `GET` | `/api/health` | `h.get_health` | System health summary |
| `GET` | `/api/health/v1` | `h.get_health_v1` | Detailed subsystem health diagnostics |
| `POST` | `/api/gc/trigger` | `h.trigger_gc` | Trigger memory and storage garbage collection |
| `POST` | `/api/search` | `h.api_search` | Execute semantic vector and hybrid search |
| `POST` | `/api/replay/observe` | `h.api_replay_observe` | Stream historical event log records |
| `POST` | `/api/replay/fork` | `h.api_replay_fork` | Fork tenant namespace from event stream |
| `GET` | `/api/replay/status/{run_id}` | `h.api_replay_status` | Check status of replay execution |
| `GET` | `/api/replay/provenance/{memory_id}` | `h.api_event_provenance` | Get causal event provenance graph |
| `POST` | `/api/snapshot/export` | `h.api_snapshot_export` | Export namespace memory snapshot |
| `POST` | `/api/a2a/grants/create` | `h.api_a2a_create_grant` | Issue new A2A sharing grant |
| `POST` | `/api/a2a/grants/{grant_id}/revoke` | `h.api_a2a_revoke_grant` | Revoke A2A sharing grant |
| `GET` | `/api/a2a/grants` | `h.api_a2a_list_grants` | List active A2A sharing grants |
| `GET` | `/api/admin/a2a/grants` | `h.api_admin_a2a_grants` | Admin view of all system A2A grants |
| `GET` | `/api/admin/a2a/grants/summary` | `h.api_admin_a2a_grants_summary` | Summary metrics of A2A grants across tenants |
| `POST` | `/api/admin/a2a/grants/{grant_id}/revoke` | `h.api_admin_a2a_revoke_grant` | Administrative grant revocation |
| `GET` | `/api/admin/events` | `h.api_admin_events` | Query immutable audit event log |
| `GET` | `/api/admin/events/summary` | `h.api_admin_events_summary` | Aggregate event log statistics |
| `GET` | `/api/admin/tools` | `h.api_admin_tools` | List runtime MCP tool enablement status |
| `POST` | `/api/admin/tools/toggle` | `h.api_admin_tools_toggle` | Enable/disable specific MCP tools at runtime |
| `GET` | `/api/admin/quotas` | `h.api_admin_quotas` | Query tenant quota utilization |
| `GET` | `/api/admin/quotas/summary` | `h.api_admin_quotas_summary` | Global resource quota summary |
| `GET` | `/api/admin/settings` | `h.api_admin_settings_list` | List dynamic configuration settings |
| `PATCH` | `/api/admin/settings` | `h.api_admin_settings_patch` | Update dynamic configuration setting |
| `GET` | `/api/admin/settings/effective` | `h.api_admin_settings_effective` | Inspect effective merged runtime config |
| `GET` | `/api/admin/settings/pending` | `h.api_admin_settings_pending` | List pending uncommitted config changes |
| `POST` | `/api/admin/settings/reset` | `h.api_admin_settings_reset` | Reset configuration to environment defaults |
| `POST` | `/api/admin/settings/reload` | `h.api_admin_settings_reload` | Reload configuration from backing store |
| `POST` | `/api/admin/settings/rollback` | `h.api_admin_settings_rollback` | Rollback configuration to previous revision |
| `GET` | `/api/admin/settings/{key}` | `h.api_admin_settings_get` | Get specific configuration setting value |
| `GET` | `/api/admin/signing/status` | `h.api_admin_signing_status` | Inspect key rotation and signature verification status |
| `GET` | `/api/admin/pii-redactions` | `h.api_admin_pii_redactions_list` | Audit PII detection and redaction log |
| `GET` | `/api/admin/security/event-seq-gaps/{namespace_id}` | `h.api_admin_security_event_seq_gaps` | Detect sequence number gaps in tenant event log |
| `POST` | `/api/admin/security/verify-memory-sample` | `h.api_admin_security_verify_memory_sample` | Sample and verify cryptographic memory signatures |
| `POST` | `/api/admin/security/test-rls-isolation` | `h.api_admin_security_test_rls_isolation` | Adversarial multi-tenant RLS boundary test |
| `GET` | `/api/admin/verify-chain/{namespace_id}` | `h.api_admin_verify_chain` | Verify complete cryptographic hash chain of event stream |
| `POST` | `/api/admin/graph/explore` | `h.api_admin_graph_explore` | Explore knowledge graph nodes and relationships |
| `GET` | `/api/admin/graph/provenance/{memory_id}` | `h.api_event_provenance` | Trace graph memory provenance |
| `GET` | `/api/admin/embedding-models` | `h.api_admin_embedding_models` | List configured and available embedding models |
| `POST` | `/api/admin/embedding-migrations/start` | `h.api_admin_embedding_migration_start` | Start background vector model migration |
| `GET` | `/api/admin/embedding-migrations/{migration_id}/status` | `h.api_admin_embedding_migration_status` | Check vector migration progress and health |
| `POST` | `/api/admin/embedding-migrations/{migration_id}/validate` | `h.api_admin_embedding_migration_validate` | Run cosine quality gates on migrated embeddings |
| `POST` | `/api/admin/embedding-migrations/{migration_id}/commit` | `h.api_admin_embedding_migration_commit` | Commit migration cutover to active model |
| `POST` | `/api/admin/embedding-migrations/{migration_id}/abort` | `h.api_admin_embedding_migration_abort` | Abort migration and cleanup temporary vectors |
| `GET` | `/api/admin/schema` | `h.api_admin_schema` | Describe active database and table schemas |
| `GET` | `/api/admin/dlq` | `h.api_admin_dlq_list` | List dead-letter queue messages |
| `POST` | `/api/admin/dlq/{dlq_id}/replay` | `h.api_admin_dlq_replay` | Replay failed DLQ message |
| `POST` | `/api/admin/dlq/{dlq_id}/purge` | `h.api_admin_dlq_purge` | Purge DLQ message |
| `GET` | `/api/admin/db/postgres/status` | `h.api_admin_db_postgres_status` | Inspect PostgreSQL pool and replication status |
| `GET` | `/api/admin/db/mongo/status` | `h.api_admin_db_mongo_status` | Inspect MongoDB cluster and collection status |
| `GET` | `/api/admin/db/redis/status` | `h.api_admin_db_redis_status` | Inspect Redis cache memory and key stats |
| `GET` | `/api/admin/db/minio/status` | `h.api_admin_db_minio_status` | Inspect MinIO/S3 object storage buckets |
| `GET` | `/api/admin/connectors/status` | `h.api_admin_connectors_status` | Check third-party connector health |
| `POST` | `/api/admin/connectors/save` | `h.api_admin_connectors_save` | Save connector configuration |
| `GET` | `/api/admin/datastores/status` | `h.api_admin_datastores_status` | Check overall datastore status |
| `POST` | `/api/admin/datastores/save` | `h.api_admin_datastores_save` | Save datastore configuration |
| `GET` | `/api/admin/namespaces` | `h.api_admin_namespaces_list` | List registered tenant namespaces |
| `GET` | `/api/admin/namespaces/{namespace_id}` | `h.api_admin_namespaces_get` | Get namespace configuration and metadata |
| `POST` | `/api/admin/namespaces/{namespace_id}/metadata` | `h.api_admin_namespaces_update_metadata` | Update namespace metadata (extra='forbid') |
| `POST` | `/api/admin/memory/boost` | `h.api_admin_memory_boost` | Manually adjust memory salience weight |
| `GET` | `/api/admin/salience-map` | `h.api_admin_salience_map` | Inspect global salience distribution |
| `GET` | `/api/admin/llm-payload` | `h.api_admin_llm_payload` | Inspect synthesized prompt payload for debugging |
| `GET` | `/api/admin/fleet-overview` | `h.api_admin_fleet_overview` | Multi-tenant agent fleet monitoring |
| `GET` | `/api/admin/actor-trust` | `h.api_admin_actor_trust` | Actor trust scores and behavioral anomalies |
| `GET` | `/api/admin/approval-queue` | `h.api_admin_approval_queue_list` | List pending human approval governance items |
| `GET` | `/api/admin/approval-queue/{id}` | `h.api_admin_approval_queue_get` | Inspect approval queue item detail |
| `GET` | `/api/admin/contradictions/recent` | `h.api_admin_contradictions_recent` | List recently flagged semantic contradictions |
| `GET` | `/api/admin/namespaces/{namespace_id}/bridges` | `h.api_admin_namespace_bridges` | List bridge subscriptions for namespace |
| `POST` | `/api/admin/bridges/{bridge_id}/renew` | `h.api_admin_bridge_renew` | Renew OAuth token for document bridge |
| `GET` | `/api/admin/d365/config` | `h.api_admin_d365_config` | Get Dynamics 365 connector configuration |
| `GET` | `/api/admin/d365/integrations` | `h.api_admin_d365_integrations` | List active Dataverse table integrations |
| `POST` | `/api/admin/d365/sync` | `h.api_admin_d365_sync_now` | Trigger immediate Dynamics 365 sync cycle |
| `GET` | `/api/admin/d365/sla-breaches` | `h.api_admin_d365_sla_breaches` | List open Dynamics 365 SLA breaches |
| `POST` | `/api/admin/d365/namespace/{ns_id}/d365-enabled` | `h.api_admin_d365_namespace_update` | Enable/disable D365 integration for namespace |
| `GET` | `/api/admin/d365/netbox-mappings` | `h.api_admin_d365_netbox_mappings` | List CRM-to-NetBox topology mappings |
| `POST` | `/api/admin/d365/netbox-mappings/{mapping_id}/confirm` | `h.api_admin_d365_netbox_mapping_confirm` | Confirm proposed NetBox mapping |
| `POST` | `/api/admin/d365/netbox-bridge/sync` | `h.api_admin_d365_netbox_bridge_sync` | Trigger NetBox topology sync |
| `POST` | `/api/admin/entity-resolution/resolve` | `entity_resolution_handlers.api_entity_resolution_resolve` | Resolve entity cross-references |
| `GET` | `/api/admin/entity-resolution/queue` | `entity_resolution_handlers.api_entity_resolution_queue_list` | List candidate entity merge queue |
| `POST` | `/api/admin/entity-resolution/queue/{queue_id}/confirm` | `entity_resolution_handlers.api_entity_resolution_queue_confirm` | Confirm entity merge |
| `POST` | `/api/admin/entity-resolution/queue/{queue_id}/reject` | `entity_resolution_handlers.api_entity_resolution_queue_reject` | Reject entity merge |
| `POST` | `/api/admin/pricing/resolve` | `pricing_handlers.api_pricing_resolve` | Resolve product pricing with discounts |

### 4.2 Vertical Domain Engine Routes (44 Routes)

| Engine | Method | Path | Handler Endpoint | Description |
|---|---|---|---|---|
| **sales** | `GET` | `/public-api/sales/quotes/{id}` | `sales_public_handlers.api_sales_quote_public` | Public quote viewer (unauthenticated customer access) |
| **sales** | `GET` | `/api/admin/sales/source-mode` | `sales_handlers.api_sales_source_mode_get` | Get current sales data-source mode (DB / MCP / Graph) |
| **sales** | `PUT` | `/api/admin/sales/source-mode` | `sales_handlers.api_sales_source_mode_put` | Switch sales data-source mode |
| **sales** | `GET` | `/api/sales/customers` | `sales_handlers.api_admin_sales_customers` | List sales customer accounts with pipeline value |
| **sales** | `GET` | `/api/sales/customers/{id}` | `sales_handlers.api_admin_sales_customer_profile` | Get detailed customer profile, opportunities, and quotes |
| **sales** | `GET` | `/api/sales/overview` | `sales_handlers.api_admin_sales_overview` | Sales executive overview and KPI scorecard |
| **sales** | `GET` | `/api/sales/seller-detail/{user}` | `sales_handlers.api_admin_sales_seller_detail` | Individual seller quota attainment and active deals |
| **sales** | `GET` | `/api/sales/dashboard` | `sales_handlers.api_admin_sales_dashboard` | Aggregated sales operations dashboard |
| **sales** | `GET` | `/api/sales/stats` | `sales_handlers.api_admin_sales_stats` | Pipeline conversion and deal velocity statistics |
| **sales** | `GET` | `/api/sales/manager` | `sales_handlers.api_admin_sales_manager` | Sales manager team roll-up and forecasts |
| **sales** | `GET` | `/api/sales/agreements` | `sales_handlers.api_admin_sales_agreements` | List customer master sales agreements |
| **sales** | `GET` | `/api/sales/agreements/{id}` | `sales_handlers.api_admin_sales_agreement_detail` | Get sales agreement terms, renewals, and SLAs |
| **sales** | `GET` | `/api/sales/quotes/{id}` | `sales_handlers.api_admin_sales_quote_detail` | Get quote detail with line items and signature state |
| **sales** | `GET` | `/api/sales/targets` | `sales_handlers.api_admin_sales_targets_get` | Get annual and quarterly revenue targets |
| **sales** | `PUT` | `/api/sales/targets` | `sales_handlers.api_admin_sales_targets_put` | Update sales revenue targets |
| **agreements** | `GET` | `/api/agreements` | `agreements_handlers.api_agreements_list` | List active customer and vendor agreements |
| **agreements** | `GET` | `/api/agreements/coverage` | `agreements_handlers.api_agreements_coverage` | Analyze contract renewal coverage and 90-day expiry radar |
| **agreements** | `GET` | `/api/agreements/{id}` | `agreements_handlers.api_agreements_detail` | Get agreement full terms, clauses, and SLA thresholds |
| **agreements** | `POST` | `/api/agreements/extract` | `agreements_handlers.api_agreements_extract` | Extract structured clauses from PDF/DOCX agreement files |
| **agreements** | `POST` | `/api/agreements/review` | `agreements_handlers.api_agreements_review` | Submit agreement clause review annotations |
| **economy** | `POST` | `/api/economy/match-invoice` | `economy_handlers.api_economy_match_invoice` | Execute 3-way invoice match against PO and delivery receipt |
| **economy** | `POST` | `/api/economy/periodisering` | `economy_handlers.api_economy_periodisering` | Generate periodized accounting schedule for contracts |
| **economy** | `POST` | `/api/economy/emit-event` | `economy_handlers.api_economy_emit_event` | Post financial ledger event to event log |
| **procurement** | `POST` | `/api/procurement/tco` | `procurement_handlers.api_procurement_calculate_tco` | Calculate Total Cost of Ownership across suppliers |
| **procurement** | `POST` | `/api/procurement/rank` | `procurement_handlers.api_procurement_rank_suppliers` | Rank suppliers by weighted capability and price score |
| **procurement** | `POST` | `/api/procurement/match` | `procurement_handlers.api_procurement_evaluate_match` | Evaluate PO-to-invoice match tolerance |
| **procurement** | `POST` | `/api/procurement/sync` | `procurement_handlers.api_procurement_sync_now` | Trigger procurement ERP sync cycle |
| **procurement** | `GET` | `/api/procurement/sync/status` | `procurement_handlers.api_procurement_sync_status` | Check procurement ERP sync status |
| **procurement** | `POST` | `/api/procurement/frontier/forecast-rebate` | `procurement_handlers.api_procurement_forecast_rebate` | Forecast vendor rebate tier achievements |
| **procurement** | `POST` | `/api/procurement/frontier/recommend-move-spend` | `procurement_handlers.api_procurement_recommend_move_spend` | Optimize supplier spend allocation for tier discounts |
| **procurement** | `POST` | `/api/procurement/frontier/whatif-spend` | `procurement_handlers.api_procurement_whatif_spend` | Simulate procurement what-if scenarios |
| **product** | `GET` | `/api/product/search` | `product_handlers.api_product_search` | Search product catalog with faceted filtering |
| **product** | `GET` | `/api/product/enrichment/review` | `product_handlers.api_product_enrichment_review` | Review pending AI-enriched product attributes |
| **product** | `GET` | `/api/product/{id}` | `product_handlers.api_product_get` | Get product master record and datasheet specs |
| **project** | `POST` | `/api/project/convert-signed-quote` | `project_handlers.api_project_convert_signed_quote` | Convert signed quote into operational project structure |
| **project** | `GET` | `/api/project/{id}/phase` | `project_handlers.api_project_get_phase` | Get current project stage-gate status |
| **project** | `POST` | `/api/project/{id}/phase` | `project_handlers.api_project_advance_phase` | Advance project phase with readiness validation |
| **project** | `GET` | `/api/project/my-day` | `project_handlers.api_admin_project_my_day` | Daily task dashboard for project team members |
| **project** | `GET` | `/api/project/capacity` | `project_handlers.api_admin_project_capacity` | Project engineering resource capacity planning |
| **project** | `GET` | `/api/project/{id}/scope-creep` | `project_handlers.api_admin_project_scope_creep` | Scope creep variance detection vs signed baseline |
| **project** | `GET` | `/api/project/{id}/status-report` | `project_handlers.api_admin_project_status_report` | Generate comprehensive project health status report |
| **system_design** | `POST` | `/api/system-design/publish-design-docs` | `system_design_handlers.api_system_design_publish_design_docs` | Publish system architecture drawings to Lucidchart |
| **vendors** | `GET` | `/api/vendors/scorecard` | `vendors_handlers.api_vendors_scorecard` | Compute vendor scorecard analytics |
| **vendors** | `GET` | `/api/vendors/{id}` | `vendors_handlers.api_vendors_get_vendor` | Get vendor profile, certifications, and contracts |

---

## 5. Stdio Tool Discovery Gap Analysis (OQ-3)

*(Referencing [`FINDINGS_OQ3_tool_surface.md`](https://github.com/sindrehaugen/NCE/blob/main/FINDINGS_OQ3_tool_surface.md))*.

### 5.1 Root Cause & Mechanism

An architectural desynchronization exists between NCE's execution layer and presentation layer:

1. **Backend Execution Layer (`TOOL_REGISTRY` in `nce/tool_registry.py`)**:
   - Registers **112 tools** complete with dispatch coroutine wrappers (`_h()`) and metadata flags (`admin_only`, `mutation`, `cacheable`, `migration`).
   - The dispatch engine (`mcp_stdio_dispatch.py`) routes incoming JSON-RPC calls via `TOOL_REGISTRY.get(tool_name)`.
2. **Presentation / Discovery Layer (`TOOLS` in `nce/mcp_stdio_tools.py`)**:
   - Declares static `mcp.types.Tool` schema definitions (input properties, types, descriptions) for **71 tools** (66 platform/shared + 5 diagnostics).
   - The MCP stdio server method `server.py::list_tools()` returns this static `TOOLS` list during client initialization (`tools/list` handshake).
3. **Operational Consequence**:
   - **41 vertical module tools** are fully functional and dispatchable at runtime if a client knows the exact name and schema, but they are **completely undiscoverable** by generic MCP clients.

### 5.2 Detailed Inventory of the 41 Missing Stdio Tools

| Vertical Engine / Domain | Missing Stdio Tools Count | Specific Tools Undiscovered in `TOOLS` | Gating & Mutation Flags |
|---|---|---|---|
| **Vendors Engine** | **10** | `vendors_get_vendor`, `vendors_compute_scorecard`, `vendors_get_tier_status`, `vendors_detect_reliability_degradation`, `vendors_check_tier_at_risk`, `vendors_match_contractor`, `vendors_compute_performance`, `vendors_recall_similar_jobs`, `vendors_reliability_radar`, `vendors_calibrate_weights` | All `cacheable=True`, read-only |
| **Product PIM Engine** | **6** | `product_search`, `product_get`, `product_price`, `product_related`, `product_match_bom_line`, `product_enrich` | 5 `cacheable=True`, `product_enrich` is `mutation=True` |
| **Procurement Engine** | **6** | `procurement_calculate_tco`, `procurement_rank_suppliers`, `procurement_evaluate_match`, `procurement_forecast_rebate`, `procurement_recommend_move_spend`, `procurement_whatif_spend` | All `cacheable=True`, read-only |
| **Project Engine** | **4** | `project_can_enter_phase`, `project_convert_signed_quote`, `project_advance_phase`, `project_suggest_pl` | `project_can_enter_phase` & `project_suggest_pl` (`cacheable`), `convert_signed_quote` & `advance_phase` (`admin_only, mutation`) |
| **Economy Engine** | **3** | `economy_match_invoice`, `economy_compute_periodisering`, `economy_emit_event` | All `cacheable=True` |
| **Sales Engine** | **2** | `sales_ping`, `sales_get_signed_baseline` | `sales_ping` (`cacheable`), `sales_get_signed_baseline` (read-fresh) |
| **System Design Engine** | **2** | `system_design_ping`, `system_design_publish_design_docs` | `system_design_ping` (`cacheable`), `publish_design_docs` (`mutation`) |
| **Agreements Engine** | **1** | `agreements_lookup_terms` | `cacheable=True` |
| **Dynamics 365 Engine** | **1** | `d365_sync_status` | Read-only status probe |
| **Shared Platform Core** | **6** | `detect_causal_cycles`, `merge_queue_confirm`, `merge_queue_list`, `merge_queue_reject`, `pricing_resolve`, `resolve` | `detect_causal_cycles` (`admin_only`), `merge_queue_*` (`mutation/admin`), `resolve/pricing` (`cacheable`) |
| **Total Missing Tools** | **41** | *See inventory above* | *Full ToolSpec metadata verified in section 3* |

### 5.3 Operational Workarounds & Remediation Plan

1. **Direct Invocation Workaround**:
   - Internal subagents, orchestration tests, and scripts can invoke any of the 41 vertical tools directly by name via `call_tool(name="...", arguments={...})` because the dispatch layer resolves them via `TOOL_REGISTRY`.
2. **Remediation Roadmap**:
   - A planned build step will auto-generate `mcp.types.Tool` schema declarations directly from Pydantic V2 input models declared on vertical handlers, ensuring 100% synchronization between `TOOL_REGISTRY` and `mcp_stdio_tools.py::TOOLS` without manual duplication.

---

## 6. Verification & Drift Prevention Gates

NCE enforces strict automated testing gates in CI to prevent API documentation and implementation drift:

- **API Spec Drift Gate (`tests/test_api_docs_current.py`)**: Runs `python scripts/gen_api_docs.py --check` on every PR to verify that `docs/API.md` perfectly matches live routes and tools.
- **RLS Catalog Gate (`tests/test_rls_catalog.py`)**: Verifies that every database table carrying tenant isolation policies is registered in `EXPECTED_TENANT_RLS_TABLES` (`nce/event_log.py`).
- **Zero-Trust Boot Guard (`nce/mtls.py::assert_server_mtls_or_acknowledged`)**: Refuses application startup in production if mutual TLS is disabled without explicit WORM audit acknowledgement.
- **Hash Chain Integrity (`tests/test_event_hash_chain.py`)**: Cryptographically validates SHA-256 event chaining and sequence monotonically increasing order.
