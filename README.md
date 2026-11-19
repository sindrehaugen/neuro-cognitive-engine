# NCE (Neuro-Cognitive Engine)

> Multi-tenant context, hybrid retrieval, and governance substrate for Model Context Protocol (MCP) clients and LLM-assisted engineering workflows.

<p>
  <img alt="version" src="https://img.shields.io/badge/version-3.7.0-blue">
  <img alt="stage" src="https://img.shields.io/badge/stage-active%20R%26D-0ea5e9">
  <img alt="python" src="https://img.shields.io/badge/python-3.10%20--%203.12-3776ab">
  <img alt="protocol" src="https://img.shields.io/badge/MCP-JSON--RPC%202.0-6e40c9">
  <img alt="license" src="https://img.shields.io/badge/license-AGPL--3.0-blue">
</p>

## Project Status & Research Scope

NCE v3.7.0 is an **ongoing personal Research & Development (R&D) architecture testbed started in early May 2026**. It was built to design, implement, and stress-test three systems-engineering pillars end to end:

1. **Multi-Database Systems Architecture:** Coordinating relational/vector state (`PostgreSQL 16` + `pgvector`), unstructured document payloads (`MongoDB 7.0`), distributed locks/queues (`Redis 7.4` + `RQ`), and content-addressed blobs (`MinIO`) behind transaction-scoped Saga compensation and point-in-time (`as_of`) temporal queries.
2. **Enterprise Security Engineering:** Database-enforced PostgreSQL Row-Level Security (`FORCE ROW LEVEL SECURITY`) across partitioned tables, per-record `AES-256-GCM` KEK/DEK envelope encryption with cryptographic erasure, and an append-only, hash-chained WORM `event_log` signed via RFC 8785 (JCS) `HMAC-SHA256`.
3. **Grounded Retrieval & Action Governance:** 3-way Reciprocal Rank Fusion (lexical + vector + exact identifier boosting), cycle-safe recursive graph traversal, cross-encoder NLI contradiction warnings, provenance tracking, and `@governed` confirm-first human-in-the-loop approval gates.

> [!IMPORTANT]
> **What NCE Is Not:** NCE is **not** a commercially supported product, a turnkey enterprise platform, or a certified drop-in replacement for statutory ERP, accounting, or safety-critical engineering software. Background LLM summarization and NLI contradiction checks remain probabilistic; NCE reduces ungrounded outputs and operational blast radius by anchoring reads to exact/vector hybrid recall, WORM provenance, and human approval gates rather than claiming deterministic factual perfection.

**Note on Naming:** The project started in early May 2026 as **TriMCP** (a three-store MCP server) and was renamed `neuro-cognitive-engine` as graph traversal, dependency tracking, and scheduled summarization workers were added. Python package names (`neuro-cognitive-engine`, `nce`), SQL identifiers (`salience`, `episodic`, `semantic`, `consolidation_runs`), and file paths (`docs/cognitive_layer.md`, `docs/time_travel.md`) retain their original names for backwards compatibility; throughout this documentation, each legacy identifier is paired directly with its concrete database or retrieval mechanism.

---

## Table of Contents

- [Project Status & Research Scope](#project-status--research-scope)
- [Architectural Goals](#architectural-goals)
- [Retrieval, Consistency & Governance](#retrieval-consistency--governance)
- [Multi-Store Architecture](#multi-store-architecture)
- [Surfaces & Progressive MCP Routing](#surfaces--progressive-mcp-routing)
- [Security Architecture](#security-architecture)
- [Known Limitations & R&D Trade-Offs](#known-limitations--rd-trade-offs)
- [Quickstart](#quickstart)
- [Connecting an MCP Client](#connecting-an-mcp-client)
- [Experimental Extension Packages](#experimental-extension-packages)
- [Tech Stack](#tech-stack)
- [Testing & Quality Gates](#testing--quality-gates)
- [Documentation Index](#documentation-index)
- [Pre-Deployment & Staging Verification Checklist](#pre-deployment--staging-verification-checklist)

---

## Architectural Goals

Flat vector wrappers work well for single-user prototypes, but exhibit predictable failure modes in shared engineering sandboxes: application-layer `WHERE` filters risk cross-tenant leaks when a query omits a predicate, pure cosine similarity misses exact part numbers/SKUs/standards clauses, `UPDATE` statements overwrite prior design rationale, and unguarded MCP tools allow silent state mutations.

| Engineering Concern | Flat Vector Wrapper Failure Mode | NCE Substrate Mechanism |
| :--- | :--- | :--- |
| **Tenant Isolation** | Application-layer `WHERE tenant_id = ?` omitted in one query | PostgreSQL `FORCE ROW LEVEL SECURITY` bound via `SET LOCAL nce.namespace_id` on every checkout |
| **Technical Recall** | Pure cosine search misses exact SKUs, org numbers, IPs, or standards codes | 3-way Hybrid RRF (`pgvector` HNSW + `tsvector` BM25/GIN + exact identifier boost) + `WITH RECURSIVE` graph traversal |
| **Audit & History** | Last-write-wins overwrites prior engineering rationale | Append-only WORM `event_log` with `as_of` point-in-time temporal queries and named snapshots |
| **Conflicting Inputs** | Stale and newer records coexist silently in top-$k$ | Candidate similarity filter + `kg_edges` conflict check + `nli-deberta-v3-small` cross-encoder warnings + ATMS dependency links |
| **Context Lifecycle** | Unbounded raw logs degrade retrieval precision over time | Scheduled HDBSCAN background clustering & Pydantic-validated summarization (`nce.cron`) + configurable time-decay scoring |
| **Cross-Team Federation** | Copying raw rows across workspaces | Scoped, SHA-256 hash-grant A2A JSON-RPC queries evaluated inside the owner's RLS session |
| **Tool Mutation Safety** | LLM client executes high-impact writes directly | C2 Action Governance (`@governed` confirm-first human approval queue, value ceilings, idempotency keys, kill-switch) |

---

## Retrieval, Consistency & Governance

```
  ingest ──▶ raw interaction & document records (`episodic`)
                    │
                    ├──▶ scheduled HDBSCAN clustering + LLM summarization (`nce.cron`)
                    │            │
                    │            ▼
                    │    distilled reference summaries (`semantic`) + `procedural_heuristics`
                    │            │
                    ▼            ▼
            3-Way Hybrid RRF + `WITH RECURSIVE` Knowledge Graph (`kg_nodes` / `kg_edges`)
                    │
                    └──▶ async ring-buffered access-frequency & time-decay scoring (`memory_salience`)
```

- **3-Way Hybrid RRF & Context Compilation:** `semantic_search` fuses dense vector similarity (`pgvector` `halfvec(768)` HNSW with `relaxed_order`), PostgreSQL full-text search (`search_tsv` GIN), and exact token/identifier matching (SKUs, organization numbers, IP addresses) via Reciprocal Rank Fusion, returning calibrated `confidence_score` and `epistemic_warnings` metadata. `compile_working_memory` (tool `#71`) assembles raw records (`episodic`), distilled summaries (`semantic`), graph neighborhoods, and `procedural_heuristics` in a shared `scoped_pg_session`.
- **Cycle-Safe Graph Traversal:** `graph_search` and `neuromorphic_search` execute weighted, cycle-safe multi-hop `WITH RECURSIVE` CTE traversals over `kg_nodes` and `kg_edges` using trigram GIN indexes (`idx_kg_edges_combined_trgm`) and configurable edge-weight adaptation based on co-retrieval frequency.
- **Configurable Time-Decay & Access-Frequency Scoring (`salience`):** Each record tracks a relevance score $s(t) = s_0 \cdot e^{-\lambda \Delta t}$ ($\lambda = \ln(2)/\text{half\_life\_days}$, deterministic $\pm 5\%$ per-UUID jitter) stored across 4 hash partitions of `memory_salience`. Read-path reinforcements (`s <- min(1.0, s + delta)`) are coalesced asynchronously via an in-memory ring buffer so read latency stays off the write lock path.
- **Contradiction Flagging & Dependency Tracking (ATMS):** Candidate writes are compared against top cosine neighbors (`>= 0.85`), checked for conflicting `kg_edges` objects, scored with the local cross-encoder `cross-encoder/nli-deberta-v3-small`, and optionally tie-broken by a schema-validated LLM auditor. Confirmed conflicts are recorded in `contradictions`. An Assumption-Based Dependency Tracker (`ATMS`) links `ASSUMPTION`, `PREMISE`, and `DERIVED` records so retracting an upstream assumption marks downstream derived records superseded (`valid_to = now()`).
- **Scheduled Background Summarization (`nce/cron.py` & RQ):** Background workers cluster dense embeddings with HDBSCAN (`min_cluster_size=2`), generate Pydantic V2-validated summaries (`ConsolidatedAbstraction`), persist raw text to MongoDB, and log provenance edges to `event_log`.

See [docs/cognitive_layer.md](docs/cognitive_layer.md) for mathematical formulas, SQL schemas, and failure-mode mitigations.

---

## Multi-Store Architecture

```mermaid
flowchart TB
  subgraph Clients ["Clients & Operators"]
    IDE["MCP Clients (Claude Desktop, Cursor)"]
    PEER["Federated Namespace Peers"]
    OPS["Operators / Staging Admins"]
  end

  subgraph Surfaces ["Ingress Gateways (Pure-ASGI + HTTP 413 Guards)"]
    STDIO["server.py (MCP stdio)"]
    GW["mcp_gateway_server.py (Streamable HTTP /mcp)"]
    PROXY["nce.mcp_client_proxy (Zero-Secret CLI Bridge)"]
    A2A["nce/a2a_server.py (Scoped JSON-RPC Federation)"]
    ADM["admin_server.py (REST + Admin Console)"]
    WH["nce/webhook_receiver (Document Webhook Ingest)"]
  end

  subgraph Background ["Background Workers & Schedulers"]
    RQ["start_worker.py (RQ Job Worker)"]
    CRON["nce.cron (Summarization, Decay, Renewals, GC)"]
  end

  subgraph Engine ["NCEEngine Core Substrate"]
    ORCH["Transaction-Scoped Write Saga"]
    RET["3-Way RRF & Recursive Graph Retrieval"]
    GOV["C2 @governed Gates & ATMS Dependency Tracker"]
    TMP["as_of Temporal Queries & Deterministic Replay"]
  end

  subgraph Data ["Four-Store Persistence Layer"]
    PG[("PostgreSQL 16 + pgvector")]
    MG[("MongoDB 7.0")]
    RD[("Redis 7.4 + RQ")]
    S3[("MinIO S3 Object Store")]
  end

  IDE --> STDIO
  IDE --> PROXY
  PROXY --> GW
  IDE --> GW
  PEER --> A2A
  OPS --> ADM
  STDIO --> ORCH & RET
  GW --> ORCH & RET
  A2A --> RET
  ADM --> ORCH & GOV
  WH --> RQ
  RQ --> ORCH
  CRON --> ORCH
  ORCH --> PG & MG & RD & S3
  RET --> PG & MG & RD
  GOV --> PG & RD
  TMP --> PG & MG & S3
```

| Backing Store | Primary Engineering Responsibility | Stored Artifacts |
| :--- | :--- | :--- |
| **PostgreSQL 16 + `pgvector`** | Relational integrity, forced RLS, vector/FTS/graph indexing, WORM audit ledger | `halfvec(768)` HNSW embeddings, `search_tsv` GIN indexes, `kg_nodes` / `kg_edges`, RLS policies, hash-chained `event_log` |
| **MongoDB 7.0** | Document payload storage with `$in` batch hydration | Large unstructured transcripts, extracted document pages, AST chunks referenced by `payload_ref` |
| **Redis 7.4** | Low-latency coordination, caching, and background queues | Session/token caches, rate limits, distributed cron locks, single-use HMAC anti-replay nonces, `rq` job queues |
| **MinIO (S3 API)** | Content-addressed binary & replay cache storage | Binary files (PDF, CAD, images, logs) and SHA-256-verified LLM response payloads for deterministic replay |

Standard multi-store writes execute through a transaction-scoped **Saga orchestrator** (`nce/saga.py`) with bounded timeouts (`2000ms` Motor failover) and compensating rollbacks; residual crash orphans are swept asynchronously (`SKIP LOCKED`). Logical revisions follow Copy-on-Write (`CoW`) semantics across MongoDB `episodes` (`created_at` ascending index + `saga_id`) and versioned SHA-256 MinIO keys so `as_of` reads and **`T_cut` catalog-watermark backups** (`042_day2_ops_backup_and_governance.sql`) observe immutable pre-cut blobs, while two-phase `crypto_shred` persists HMAC-signed zero-PII `shred_tombstones.jsonl` records before deferring physical ciphertext GC under active `backup_leases`. See [docs/architecture-v1.md](docs/architecture-v1.md), [docs/database_architecture.md](docs/database_architecture.md), and [docs/backup_and_recovery.md](docs/backup_and_recovery.md).

---

## Surfaces & Progressive MCP Routing

Every HTTP surface runs pure-ASGI streaming middleware (avoiding Starlette `BaseHTTPMiddleware` buffering overhead) fronted by `BoundedBodyMiddleware` (`NCE_MAX_HTTP_BODY_BYTES`, rejecting oversized requests with HTTP `413`):

| Entrypoint | Transport | Role | Security & Isolation Controls |
| :--- | :--- | :--- | :--- |
| `server.py` | MCP stdio (JSON-RPC 2.0) | Local developer MCP server | Process-isolated stdio; namespace pinned via `NCE_MCP_NAMESPACE_ID` |
| `mcp_gateway_server.py` | Streamable HTTP `/mcp` (SSE / JSON-RPC 2.0) | Remote multi-tenant MCP gateway (`:8005` direct, `:8443/mcp` TLS via Caddy) | Scoped bearer tokens (`tools:read`, `tools:call`, `admin`), fail-closed Redis governance, HTTP `413` body cap |
| `nce.mcp_client_proxy` | CLI bridge (`stdio` → HTTPS `/mcp`) | Workstation proxy (`nce-mcp-proxy`) for stdio-only MCP clients | Zero database credentials or `NCE_MASTER_KEY` on client machines |
| `go/cmd/nce-launch` | Native Go CLI shim | Mode-aware launcher for desktop/server setups | OS keyring + MSAL/JWT auth, bounded `<5s` socket probes |
| `admin_server.py` | HTTP REST + 5-Tab `/admin` Console (`:8003`) | Overview telemetry, `T_cut` backups/drills, Ops Caddy & RQ quarantine, tenant/MCP onboarding, plugins & durable tool toggles | Pure-ASGI dual-auth (`HttpOnly` cookie session + `X-NCE-CSRF-Token`, 3-header `HMAC-SHA256` with Redis nonces, strict Entra OIDC Bearer), PG-backed `runtime_tool_governance` surviving `FLUSHALL`, `0` external CDN links |
| `nce/a2a_server.py` | HTTP JSON-RPC (`:8004`) | Cross-namespace federated query endpoint | Pinned JWT (`RS256`/`ES256`/`PS256` or `HS256`), SHA-256 hashed grant scopes, owner-RLS execution |
| `nce/webhook_receiver/main.py` | HTTP | Inbound SharePoint/OneDrive, Google Drive, and Dropbox webhooks | Provider HMAC verification, SSRF egress guards, HTTP `413` body cap |
| `start_worker.py` | RQ worker (`RecoveringWorker`) | Async code AST indexing, document bridge sync, shadow-column re-embedding, poison-pill quarantine | Queue isolation, 2-strike / inflight failure quarantine into `ops_quarantine`, DLQ, seccomp profile (`Seccomp: 2`) |
| `nce/cron.py` | Scheduler (`nce-cron`) + Maintenance Caddy | Summarization, time-decay pruning, bridge renewal, `5m`/`nightly`/`weekly` probes & `T_cut` backup drills | Distributed Redis locks, zero `docker.sock`, optional isolated `ops-elm` (`--profile ops-elm`) with secret scrubbing & deterministic fallback, `Seccomp: 2`, `< 180 MiB` RSS |

### Progressive MCP Tool Routing (`compact` vs. `full`)

NCE registers **71** static Core MCP tools in [`nce/tool_registry.py`](nce/tool_registry.py) (`34` mutation, `13` admin-only, `9` cacheable, `5` migration), plus optional plugin tool families loaded via `nce.plugins`.

- **`compact` mode (default on `/mcp`):** `tools/list` exposes `12` startup tools (`<= 16` budget: `11` Core retrieval/graph/schema tools + `discover_tools`) with internal auth fields stripped from `inputSchema` (`~3,100` initial prompt tokens). Clients dynamically search and activate additional Core or plugin tools in `< 2 ms` via `discover_tools(query=..., limit=...)` (capped at `12` matches per call in `compact` mode, prioritizing connected `app_slug` entries from `TenantConnectionVault`).
- **`full` mode:** Exposes all `71` static Core tools (plus enabled plugin tools) up front for batch test harnesses or high-context clients.

| Tool Family | Representative MCP Tools | Mechanical Function |
| :--- | :--- | :--- |
| **Write & Ingest** | `store_memory`, `store_artifact`, `index_code_file`, `check_indexing_status` | Saga-coordinated record persistence, MinIO artifact storage, Tree-sitter AST symbol chunking |
| **Hybrid & Graph Read** | `semantic_search`, `compile_working_memory`, `graph_search`, `search_codebase`, `describe_schema` | 3-way RRF search, multi-tier context assembly, recursive CTE graph traversal, live schema inspection |
| **Relevance & Conflict** | `boost_memory`, `forget_memory`, `list_contradictions`, `resolve_contradiction` | Adjust `memory_salience`, soft-expire rows (`valid_to`), inspect/resolve NLI & KG conflicts with ATMS propagation |
| **Temporal & Replay** | `create_snapshot`, `list_snapshots`, `compare_states`, `replay_observe`, `replay_fork`, `replay_reconstruct` | `as_of` snapshot markers, point-in-time state diffs, deterministic or re-executed namespace replay |
| **Provenance & Sharing** | `verify_memory`, `get_event_provenance`, `a2a_create_grant`, `a2a_query_shared`, `a2a_revoke_grant` | JCS `HMAC-SHA256` verification, `parent_event_id` chain walk, scoped cross-namespace federation |
| **Bridges & Admin** | `connect_bridge`, `list_bridges`, `manage_namespace`, `manage_quotas`, `trigger_consolidation`, `rotate_signing_key`, `get_health`, `list_dlq` | OAuth/webhook document sync and `[ADMIN]` tenant, quota, key-rotation, summarization, and DLQ operations |

See [`docs/_generated/surface.md`](docs/_generated/surface.md) and [docs/mcp_tool_cookbook.md](docs/mcp_tool_cookbook.md) for complete schemas.

### Controlled Container Benchmark Summary

Measured on a single-host Docker Compose environment against a synthetic **100,000-row** `AES-256-GCM` envelope-encrypted (`TCDEK\x01`) tenant dataset (`100k` `memories` + MongoDB `episodes`, `20k` `memory_salience`, `5k` `kg_nodes`, `15k` `kg_edges`, `10k` signed WORM `event_log` rows; baked ONNX sidecar `NCE_COGNITIVE_BACKEND=onnx` with `HF_HUB_OFFLINE=1`):

| Operation / Chained Workflow Step | `v3.5.0` Baseline (`p50`) | `v3.5.1` Direct Engine (`p50`) | `v3.5.2+` `/mcp` HTTP Gateway (`p50 / p95`) | Primary Engineering Optimization |
| :--- | ---: | ---: | ---: | :--- |
| `discover_tools(limit=8)` (`compact` mode) | `35.78 ms` | `35.78 ms` | **`1.35 ms / 9.32 ms`** | Pre-tokenized catalog cache + compact schema stripping |
| `semantic_search(limit=5)` (`100k` rows) | `681.31 ms` | `24.46 ms` | **`35.60 ms / 38.87 ms`** | Fused CTE, `halfvec(768)` HNSW, direct `search_tsv` GIN, zeroing DEK LRU |
| `compile_working_memory` (`100k` rows) | `651.32 ms` | `26.70 ms` | **`38.44 ms / 41.80 ms`** | Shared `scoped_pg_session` SQL coalescing + query-embedding LRU |
| `graph_search(max_depth=2)` (`15k` edges) | `108.73 ms` | `21.00 ms` | **`36.18 ms / 48.64 ms`** | Cycle-safe `WITH RECURSIVE` CTE + `idx_kg_edges_combined_trgm` GIN |
| **5-Tool Chained MCP Turn** | `~1,515 ms` | `~148 ms` | **`116.94 ms` (`p50`)** | 30s verified-token cache + 2s Redis session sync + async ring-buffered `salience` |
| **Mongo-Pause Saga Rollback (`store_memory`)** | `10.10 s` | `2.36 s` | **`2.38 s` (`< 3.0s`)** | `2000ms` Motor socket timeouts + bounded inline compensation + `SKIP LOCKED` sweeper |

> [!NOTE]
> These figures reflect controlled synthetic single-node container benchmarks, not multi-region commercial SLA guarantees. Full methodology, hardware specs, and `EXPLAIN (ANALYZE, BUFFERS)` plans are documented in [PERF_REPORT_v3.5.2.md](docs/benchmarks/PERF_REPORT_v3.5.2.md), [HARDENING_READINESS_v3.5.3.md](docs/benchmarks/HARDENING_READINESS_v3.5.3.md), [HARDENING_READINESS_v3.5.4.md](docs/benchmarks/HARDENING_READINESS_v3.5.4.md), [HARDENING_READINESS_v3.6.0.md](docs/benchmarks/HARDENING_READINESS_v3.6.0.md), and the live 6-scenario `v3.7.0` chaos hardening report [DAY2_OPS_HARDENING_v3.7.0.md](docs/benchmarks/DAY2_OPS_HARDENING_v3.7.0.md).

---

## Security Architecture

- **Forced PostgreSQL Row-Level Security:** Every application query checks out a connection via `scoped_pg_session`, executing `SET LOCAL nce.namespace_id = '<tenant-uuid>'` inside the transaction. All **39** tenant-scoped Core tables (**38** single-namespace tables in `EXPECTED_TENANT_RLS_TABLES` plus `a2a_grants` in `EXPECTED_SPECIAL_RLS_TABLES`, out of **51** Core SQL tables—including **10** global system/control-plane tables and **2** migration-created internal tables—across **43** SQL migration files `001`–`042` + `optional/010_citus_sharding.sql`) enforce `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY` evaluated via `(SELECT get_nce_namespace())` `InitPlan` predicates. Control-plane tables from `042_day2_ops_backup_and_governance.sql` restrict direct access to `nce_gc`/admin roles and revoke direct table grants from `nce_app`. Installing optional extension packages dynamically registers their additional forced-RLS tables (`110` in `nce-vertical-engines`, `58` in `nce-app-engines`) into the same startup drift verifier.
- **Envelope Encryption & Cryptographic Erasure:** Per-record `AES-256-GCM` Data Encryption Keys (`TCDEK\x01` in `encryption_deks`) wrapped by versioned Key Encryption Keys derived from `NCE_MASTER_KEY` (`>= 32` random bytes, enforced fail-closed at import/boot) enable two-phase cryptographic erasure: nulling `wrapped_dek` inside PostgreSQL while atomically appending HMAC-signed zero-PII records to `shred_tombstones.jsonl` so subsequent restores never resurrect erased keys. Tenant connector secrets live in `app_connections` (`041_app_connections_vault.sql`) via `TenantConnectionVault`. See [docs/pii.md](docs/pii.md) and [docs/backup_and_recovery.md](docs/backup_and_recovery.md).
- **Append-Only WORM `event_log` & Canonical Signing:** `prevent_mutation` triggers block `UPDATE`/`DELETE` on `event_log` and `event_parents`. Per-namespace `pg_advisory_xact_lock` (`acquire_event_log_worm_lock`) serializes hash-chain appends, and every row is signed with rotatable `HMAC-SHA256` keys over RFC 8785 JSON Canonicalization Scheme (JCS). See [docs/signing.md](docs/signing.md).
- **Zero-Trust Auth, Replay Fencing & SSRF Defense:** Admin HTTP requires 3-header `HMAC-SHA256` signatures with Redis single-use nonces and bounded clock-skew windows (`NCE_ADMIN_OVERRIDE` raises `RuntimeError` if set when `NCE_ENV=production`). JWT verification pins explicit algorithms (`RS256`/`ES256`/`PS256` or `HS256`). Replay sandboxes (`is_replay_sandbox`) fence outbound webhooks and hardware/ERP actuation. SSRF validators reject RFC 1918, loopback, link-local, CGNAT (`100.64.0.0/10`), and IPv4-mapped IPv6 addresses unless explicitly allowlisted via `NCE_SSRF_ALLOWLIST_HOSTS`.

See [docs/enterprise_security.md](docs/enterprise_security.md) and [docs/multi_tenancy.md](docs/multi_tenancy.md).

---

## Known Limitations & R&D Trade-Offs

1. **Crash-Consistent Single-Host Backups vs. Distributed Atomic Snapshots:** `T_cut` watermark backups (`042`) coordinate `REPEATABLE READ` PostgreSQL snapshots, Copy-on-Write MongoDB/MinIO stores, TTL-bounded `backup_leases`, post-restore tombstone replay, and orphan blob sweeps without halting writers, trading off synchronous cross-store 2PC pauses. Single-host archives on `NCE_BACKUP_DIR` still share a physical failure domain with the host disk until replicated off-host by the operator.
2. **Probabilistic NLP & Summarization Boundaries:** Cross-encoder NLI (`nli-deberta-v3-small`), LLM-driven HDBSCAN cluster summarization, and optional `ops-elm` incident briefs are statistical/diagnostic aids, not formal theorem provers or unsupervised auto-remediation; `@governed` repair playbooks always require human preview confirmation tokens before mutating state.
3. **Solo R&D Velocity vs. Production Soak Time:** Started in early May 2026 and developed rapidly with AI-assisted coding and automated CI/container test suites, this codebase has **not** undergone multi-year, multi-organization production soak testing or third-party penetration certification.
4. **Copyleft Licensing (`AGPL-3.0-only`):** Networked deployment of modified versions triggers AGPL-3.0 source-disclosure obligations; evaluate license compatibility before embedding in proprietary SaaS offerings.

---

## Quickstart

> Prerequisites: **Docker Desktop** and **Python 3.10+**.

### 1. Configure Environment Secrets

```bash
cp .env.example .env
```

Populate required secrets in `.env` (never commit `.env`):

- `NCE_MASTER_KEY` — `>= 32` random UTF-8 bytes used for KEK/DEK envelope and vault encryption (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).
- `NCE_ENVELOPE_ENCRYPTION_ENABLED=true` — enables per-record `AES-256-GCM` (`TCDEK\x01`) envelope encryption.
- `NCE_API_KEY` / `NCE_ADMIN_API_KEY` / `NCE_MCP_API_KEY` — high-entropy random tokens.
- `NCE_MCP_NAMESPACE_ID` — UUID pinning local `server.py` stdio sessions to a single tenant namespace (remote `/mcp` sessions bind `namespace_id` directly from their scoped bearer token).

### 2. Launch the Container Stack

```bash
make up          # generates compose secrets and starts Caddy, services, ONNX sidecar, and 4 stores
make status      # inspect container healthchecks
```

To start **only the four backing stores** for host-side Python development:

```bash
make local-up    # runs docker-compose.local.yml (Postgres, Mongo, Redis, MinIO)
```

### 3. Run Entrypoints from Host (Optional)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows (use `source .venv/bin/activate` on Linux/macOS)
pip install -r requirements.txt

python mcp_gateway_server.py       # Streamable HTTP /mcp gateway (:8005 host, :8443/mcp via Caddy)
python server.py                   # Local MCP stdio server (stdin/stdout JSON-RPC 2.0)
python start_worker.py             # RQ background worker (separate shell)
python -m nce.cron                 # Background scheduler (separate shell)
```

### 4. Run End-to-End Smoke Verification

```bash
make verify                        # runs verify_v1_launch.py against the running stack
```

---

## Connecting an MCP Client

Connect MCP clients either directly to the Streamable HTTP `/mcp` gateway or through the zero-secret `nce.mcp_client_proxy` bridge (`nce-mcp-proxy`). Neither option places database credentials or `NCE_MASTER_KEY` on client workstations.

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "nce-http": {
      "url": "https://localhost:8443/mcp",
      "headers": {
        "Authorization": "Bearer nce_mcp_REPLACE_WITH_SCOPED_TOKEN"
      }
    },
    "nce-proxy": {
      "command": "python",
      "args": [
        "-m",
        "nce.mcp_client_proxy"
      ],
      "env": {
        "NCE_MCP_HTTP_URL": "https://localhost:8443/mcp",
        "NCE_MCP_CLIENT_TOKEN": "nce_mcp_REPLACE_WITH_SCOPED_TOKEN"
      }
    }
  }
}
```

Scoped tokens can be minted in the `/admin/plugins` console or via `POST /admin/plugins/api/mint-token`. For **Cursor**, add an MCP server pointing to `https://localhost:8443/mcp` or `python -m nce.mcp_client_proxy` (see [`mcp_config.json.example`](mcp_config.json.example)).

---

## Experimental Extension Packages

NCE v3.7.0 extends the vendor-neutral **Core Connector SDK (`nce.connectors`)** (`BaseAppConnector`, `ConnectorDescriptor`, `@governed_action`, `TenantConnectionVault`, SSRF/ZIP/XML/C8 redaction guards, and Entra OIDC helpers) with durable PostgreSQL-backed `runtime_tool_governance` and a 5-tab `/admin` control plane, allowing optional R&D extension repositories to mount schemas, routes, and MCP tools via `nce.plugins` entry points:

1. **`neuro-cognitive-engine` (Core Substrate + Connector SDK)** — ![Stage: Core](https://img.shields.io/badge/stage-core%20R%26D-0ea5e9)
   - Four-store retrieval/security substrate (`71` static Core MCP tools, `39` forced-RLS Core tables), `TenantConnectionVault` (`041_app_connections_vault.sql`), `/admin/plugins` maturity filtering (`?maturity=stable,beta,experimental`), and connected-first `/mcp` `discover_tools` ranking.
2. **[`nce-app-engines`](https://github.com/sindrehaugen/nce-app-engines) (`[Experimental / Beta]` — 20 Core-Only Application Connectors)** — ![Stage: Beta](https://img.shields.io/badge/stage-beta-orange)
   - Standalone R&D connector package (`maturity="beta"`, `vendor_specific=True`, `58` forced-RLS tables, `140` MCP tools) covering CAD/BIM/ECAD, diagramming, analytics, and workspace file/API formats (`visio`, `autocad`, `revit`, `kicad`, `wscad`, `sketchup`, `bim_vision`, `dialux`, `solidworks`, `vectorworks`, `blender`, `pcon_planner`, `drawio`, `figma`, `adobe_suite`, `power_bi`, `nodered`, `webiq`, `vscode`, `m365_workspace`) with zero dependency on business-domain tables.
3. **[`nce-vertical-engines`](https://github.com/sindrehaugen/nce-vertical-engines) (`[Experimental / R&D Prototypes]` — 29 Business & Governance Engines)** — ![Stage: Beta](https://img.shields.io/badge/stage-beta-orange)
   - Experimental reference schemas and `@governed` workflows across 29 business, ERP/CRM, IT/OT, compliance, and system-design modules (`110` forced-RLS tables, Shared-Core C1–C9 contracts, Dual-Axis `type-*` / `cat-*` packaging). Calculations touching engineering, electrical, accounting, or regulatory frameworks (e.g., NEK 400 / NEK 700, NGAAP, ESG, QHSE) are R&D aids for qualified human review, not certified compliance sign-off.
4. **Private / Organization-Specific Extensions**
   - Internal R&D or OT/BMS prototypes mount out-of-band via the same `nce.plugins` protocol without adding proprietary code or identifiers to public repositories.

See [docs/plugin_development.md](docs/plugin_development.md) for the `nce.connectors` and `NCEPlugin` authoring contract.

---

## Tech Stack

- **Runtime:** Python 3.10–3.12 (container runtime 3.12)
- **Transports:** MCP over JSON-RPC 2.0 (`stdio` and Streamable HTTP `/mcp`); pure-ASGI Starlette/FastAPI HTTP services
- **Relational & Vector Store:** PostgreSQL 16 + `pgvector` (`halfvec` HNSW iterative `relaxed_order` scan), `pgcrypto`, `WITH RECURSIVE` graph CTEs
- **Document Store:** MongoDB 7.0 (`motor` async driver, `$in` batch hydration)
- **Coordination & Queues:** Redis 7.4 + `rq`
- **Object Storage:** MinIO (S3-compatible API)
- **NLP, Clustering & Verification:** ONNX Runtime / OpenVINO, `sentence-transformers`, `cross-encoder/nli-deberta-v3-small`, `scikit-learn` HDBSCAN, `spaCy`, `NetworkX`
- **Parsing & Serialization:** `Tree-sitter` (AST chunking), `orjson`, `defusedxml` (XXE / entity-expansion protection), `Pydantic V2`
- **Observability:** OpenTelemetry (OTLP / Jaeger), Prometheus metrics

Locked transitive dependencies live in [`requirements.lock`](requirements.lock) (`make lockfile`).

---

## Testing & Quality Gates

```bash
pytest tests/                           # unit, security, schema-drift, and doc-parity suites
pytest -m integration                   # live multi-store integration suites (requires running containers)
make verify-day2                        # live 6-scenario Day-2 chaos, backup, caddy & dual-auth suite

python scripts/check_docs_integrity.py  # link/anchor, orphan, env-parity, anti-metaphor & brand-wall gate
make lint                               # ruff check + ruff format --check + check_docs_integrity.py
make typecheck                          # mypy static type check on nce/
```

---

## Documentation Index

All technical guides live under [`docs/`](docs/) (indexed in [docs/README.md](docs/README.md)):

| Topic | Guides |
| :--- | :--- |
| **Getting Started** | [quick_start.md](docs/quick_start.md) · [developer_onboarding.md](docs/developer_onboarding.md) · [usage_modes.md](docs/usage_modes.md) |
| **Architecture & Storage** | [architecture-v1.md](docs/architecture-v1.md) · [database_architecture.md](docs/database_architecture.md) · [migrations.md](docs/migrations.md) · [citus_descope.md](docs/citus_descope.md) |
| **Retrieval, Graph & NLP** | [cognitive_layer.md](docs/cognitive_layer.md) · [llm_providers.md](docs/llm_providers.md) · [recursive_indexing_flow.md](docs/recursive_indexing_flow.md) · [mcp_tool_cookbook.md](docs/mcp_tool_cookbook.md) |
| **Temporal & Replay** | [time_travel.md](docs/time_travel.md) · [replay.md](docs/replay.md) |
| **Security & Isolation** | [enterprise_security.md](docs/enterprise_security.md) · [multi_tenancy.md](docs/multi_tenancy.md) · [signing.md](docs/signing.md) · [pii.md](docs/pii.md) · [aws_iam_worker_isolation.md](docs/aws_iam_worker_isolation.md) |
| **Configuration & Ops** | [configuration_reference.md](docs/configuration_reference.md) · [it_admin_guide.md](docs/it_admin_guide.md) · [backup_and_recovery.md](docs/backup_and_recovery.md) · [day2_operations_caddy.md](docs/day2_operations_caddy.md) · [observability.md](docs/observability.md) · [performance_tuning.md](docs/performance_tuning.md) · [troubleshooting_faq.md](docs/troubleshooting_faq.md) · [deploy/README.md](deploy/README.md) |
| **Edge & Hardware** | [airgapped_deployment.md](docs/airgapped_deployment.md) · [vram_monitoring.md](docs/vram_monitoring.md) |
| **Plugins, Bridges & Federation** | [plugin_development.md](docs/plugin_development.md) · [plugin-overview-nontechnical.md](docs/plugin-overview-nontechnical.md) · [shared-core/overview.md](docs/shared-core/overview.md) · [service_integrations.md](docs/service_integrations.md) · [bridge_setup_guide.md](docs/bridge_setup_guide.md) · [push_architecture.md](docs/push_architecture.md) · [a2a.md](docs/a2a.md) |
| **APIs & Frontend Seams** | [api_reference.md](docs/api_reference.md) · [api_usage_examples.md](docs/api_usage_examples.md) · [API.md](docs/API.md) · [frontend_integration_guide.md](docs/frontend_integration_guide.md) · [FRONTEND_READINESS.md](docs/FRONTEND_READINESS.md) |
| **Benchmarks & ADRs** | [PERF_REPORT_v3.5.2.md](docs/benchmarks/PERF_REPORT_v3.5.2.md) · [HARDENING_READINESS_v3.5.3.md](docs/benchmarks/HARDENING_READINESS_v3.5.3.md) · [HARDENING_READINESS_v3.5.4.md](docs/benchmarks/HARDENING_READINESS_v3.5.4.md) · [HARDENING_READINESS_v3.6.0.md](docs/benchmarks/HARDENING_READINESS_v3.6.0.md) · [DAY2_OPS_HARDENING_v3.7.0.md](docs/benchmarks/DAY2_OPS_HARDENING_v3.7.0.md) · [adr/README.md](docs/adr/README.md) |

---

## Pre-Deployment & Staging Verification Checklist

Before exposing a self-hosted pilot or staging instance beyond `localhost`:

- Set `NCE_ENV=production` and supply high-entropy random values for `NCE_MASTER_KEY` (`>= 32` bytes), `NCE_API_KEY`, `NCE_ADMIN_API_KEY`, and `NCE_MCP_API_KEY`.
- Store `NCE_ADMIN_PASSWORD` as a `$pbkdf2$...` digest and set `NCE_LOAD_DOTENV=false` when injecting secrets via Docker/orchestrator secret mounts.
- Verify `NCE_ADMIN_OVERRIDE` is unset (`NCE_ENV=production` aborts startup with `RuntimeError` if `NCE_ADMIN_OVERRIDE=true`).
- Require TLS across external ingress and Postgres connections (`?sslmode=require`); enable internal Caddy-to-Admin mutual TLS (`make up-mtls` / `NCE_ADMIN_MTLS_ENABLED=true`) where supported.
- Keep the `prevent_mutation` WORM trigger active on `event_log` and `event_parents`, and keep `NCE_DISABLE_MIGRATION_MCP=true` outside planned re-embedding maintenance windows.
- Test coordinated backup and restore procedures across PostgreSQL, MongoDB, and MinIO before storing non-synthetic team data.

---

NCE — Neuro-Cognitive Engine · v3.7.0 · © Sindre Løvlie Haugen · AGPL-3.0. Formerly TriMCP.
