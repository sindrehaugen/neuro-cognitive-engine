# NCE — Neuro-Cognitive Engine

> A cognitive memory and reasoning substrate for autonomous agents.
> Persistent, multi-tenant, time-travelling memory across a four-database stack — with a brain on top.

<p>
  <img alt="version" src="https://img.shields.io/badge/version-3.4.0-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.10%20--%203.12-3776ab">
  <img alt="protocol" src="https://img.shields.io/badge/MCP-JSON--RPC%202.0-6e40c9">
  <img alt="license" src="https://img.shields.io/badge/license-AGPL--3.0-blue">
</p>

NCE began life as **TriMCP** — a Model Context Protocol server backed by a tri-database stack. It has since grown into a full **Neuro-Cognitive Engine**: MCP is now just *one* of several front doors onto a system that consolidates memories while agents sleep, models how knowledge decays and is reinforced, maintains logical consistency across competing beliefs, reasons about cause and effect, and federates memory securely between independent agent networks.

The engine is **provider-agnostic** (BYO LLM — local, OpenAI, Anthropic, Gemini, and more), **multi-tenant by construction** (database-enforced Row-Level Security), and **auditable by design** (an append-only, hash-chained event log that makes every state reconstructable and every memory's causal provenance traceable).

---

## Table of Contents

- [Why NCE](#why-nce)
- [The Cognitive Model](#the-cognitive-model)
- [System Architecture](#system-architecture)
- [The Quad-Database Stack](#the-quad-database-stack)
- [Capabilities](#capabilities)
- [Surfaces & Entrypoints](#surfaces--entrypoints)
- [Quickstart](#quickstart)
- [Connecting an MCP Client](#connecting-an-mcp-client)
- [MCP Tool Surface](#mcp-tool-surface)
- [Security Model](#security-model)
- [Official Plugins](#official-plugins)
- [Tech Stack](#tech-stack)
- [Testing & Quality Gates](#testing--quality-gates)
- [Documentation](#documentation)
- [Production Checklist](#production-checklist)

---

## Why NCE

Most "agent memory" is a vector index with a `search()` call bolted on. That works until an agent runs for weeks, serves multiple tenants, accumulates contradictory facts, and someone asks *"what did the agent believe last Tuesday, and why?"*

NCE is built for that second world:

| Concern | Naïve approach | NCE |
| :--- | :--- | :--- |
| **Recall** | Flat vector search | Semantic search **+** GraphRAG traversal **+** spiking spreading activation |
| **Isolation** | App-layer `WHERE tenant = ?` | PostgreSQL **Row-Level Security**, forced on every table |
| **History** | Last-write-wins | Append-only **WORM event log**; `as_of` time-travel on every read |
| **Knowledge quality** | Store everything forever | **Consolidation** (sleep cycle), **salience decay**, **contradiction detection** |
| **Truth** | Trust the latest write | **ATMS** belief revision with justification graphs |
| **Sharing** | Copy data between agents | **A2A** cryptographic, scope-bound, RLS-enforced federation |
| **Auditability** | Logs, maybe | Hash-chained provenance; deterministic **replay & fork** of any namespace |

---

## The Cognitive Model

NCE treats memory the way a mind does — as a lifecycle, not a bucket.

```
   ingest ──▶ EPISODIC ──▶ consolidate ──▶ SEMANTIC ──▶ knowledge graph
   (raw)       memories      (sleep cycle)   abstractions      (entities + relations)
                  │                                                  │
            salience decay                                    spreading activation
          (Ebbinghaus curve)                                 (neuromorphic recall)
                  │                                                  │
              reinforced ◀──────────── retrieval ───────────────────┘
```

- **Episodic → Semantic consolidation.** A background "sleep cycle" runs HDBSCAN density clustering over episodic embeddings, distils each cluster into a *Semantic Abstraction* via an LLM (output strictly validated by Pydantic V2), and upserts the result into both the memory store and the knowledge graph.
- **Salience & forgetting.** Every memory carries a salience score that decays exponentially per the **Ebbinghaus forgetting curve**, `s(t) = s₀·e^(−λΔt)`, and is reinforced on retrieval, `s ← min(1.0, s + δ)`. Important things stay sharp; noise fades.
- **Contradiction detection.** New facts are checked against existing knowledge via semantic match → KG conflict → a **cross-encoder NLI** model (`nli-deberta-v3-small`) → an LLM tiebreaker. Unresolved conflicts are surfaced for the agent to settle.
- **Belief revision (ATMS).** An Assumption-Based Truth Maintenance System tracks `ASSUMPTION` / `PREMISE` / `DERIVED` nodes and their justifications, propagating deprecation through the justification graph (cycle-safe) when an assumption is retracted.
- **Causal reasoning.** A do-calculus causal engine and counterfactual **chrono-branching** let agents ask *"what if"* — overlaying hypothetical mutations on an isolated timeline without ever touching production rows.

See [docs/cognitive_layer.md](docs/cognitive_layer.md).

---

## System Architecture

```mermaid
flowchart TB
  subgraph Clients [Clients]
    IDE["MCP clients · Claude Desktop · Cursor"]
    PEER["Peer agent networks"]
    OPS["Operators / Admins"]
  end

  subgraph Surfaces [Surfaces & Ingress Gateways]
    STDIO["server.py · MCP stdio"]
    GW["mcp_gateway_server.py · Streamable HTTP /mcp"]
    PROXY["nce.mcp_client_proxy · CLI bridge"]
    A2A["nce/a2a_server.py · A2A federation"]
    ADM["admin_server.py · REST + Admin UI"]
    WH["nce/webhook_receiver · document bridges"]
  end

  subgraph Background [Background processing]
    RQ["start_worker.py · RQ worker"]
    CRON["nce.cron · schedulers"]
  end

  subgraph Engine [NCEEngine — orchestration]
    ORCH["Saga write path"]
    COG["Cognitive workers · compile_working_memory"]
    PROC["procedural_heuristics · distillation"]
    TMP["Temporal / replay"]
  end

  subgraph Data [Quad-Database Stack]
    PG[("PostgreSQL + pgvector")]
    MG[("MongoDB")]
    RD[("Redis")]
    S3[("MinIO")]
  end

  IDE --> STDIO
  IDE --> PROXY
  PROXY --> GW
  IDE --> GW
  PEER --> A2A
  OPS --> ADM
  STDIO --> ORCH
  GW --> ORCH
  A2A --> ORCH
  ADM --> ORCH
  WH --> RQ
  RQ --> ORCH
  CRON --> COG
  COG --> PROC
  ORCH --> PG & MG & RD & S3
  COG --> PG & MG
  PROC --> PG
  TMP --> PG & S3
```

Every standard write travels a transaction-scoped **Saga** path with automatic compensating rollbacks, so a partial failure across the four stores never leaves orphaned state. Detailed sequence diagrams live in [docs/architecture-v1.md](docs/architecture-v1.md) and [docs/database_architecture.md](docs/database_architecture.md).

---

## The Quad-Database Stack

Duties are split across four engines so each does only what it is best at:

| Store | Role | Holds |
| :--- | :--- | :--- |
| **PostgreSQL + pgvector** | Relational core & vector index | Semantic embeddings (HNSW), knowledge-graph triplets (`kg_nodes` / `kg_edges`), RLS policies, the WORM `event_log` |
| **MongoDB** | Episodic payload archive | Heavy unstructured content — transcripts, code, document pages — referenced by ObjectID |
| **Redis** | Transient & coordination | TTL context caches, rate limits, distributed locks, single-use HMAC nonces, RQ job queues |
| **MinIO** | Object storage (S3 API) | Binary artifacts (image/audio/video) and the deterministic LLM response cache used by replay |

---

## Capabilities

- **3-way Hybrid RRF & Cognitive Synthesis** — `semantic_search` combining vector embeddings (pgvector HNSW cosine), full-text search (`search_tsv`), and exact keyword/identifier boosting (SKU, orgnr, IP) via Reciprocal Rank Fusion, with calibrated `confidence_score` and `epistemic_warnings`; `compile_working_memory` (tool #71) dynamically synthesizes working memory across episodic, semantic, graph, and procedural tiers.
- **Procedural memory & heuristics** — `procedural_heuristics` distillation extracts operational rules, trigger conditions, and action guidance from governed decisions with trigger-maintained full-text indexing and RLS isolation.
- **Hybrid recall** — `semantic_search` with MongoDB `$in` batch hydration, `graph_search` GraphRAG BFS traversal, and neuromorphic **spiking spreading activation** with LTP/LTD weight adaptation.
- **Time travel** — pass an `as_of` ISO-8601 timestamp to any read and see memory exactly as it stood: `valid_from <= as_of AND (valid_to IS NULL OR valid_to > as_of)`. Applies symmetrically to vector search and graph traversal.
- **Snapshots & state diffing** — name a point in time (`create_snapshot`) and diff two instants with `compare_states`.
- **Replay engine** — `replay_observe` streams the event log read-only; `replay_fork` rebuilds a namespace into an isolated target, either *deterministically* (LLM responses served from the MinIO cache, byte-identical) or *re-executed* (call the LLM fresh for A/B "what-if" divergence); `replay_reconstruct` for exact rebuilds.
- **Code intelligence** — `index_code_file` AST-parses source (Tree-sitter; Python, JS, TS, Go, Rust) into per-symbol chunks; `search_codebase` returns matching functions/classes with line ranges.
- **Document bridges** — OAuth + webhook sync from **SharePoint/OneDrive, Google Drive, and Dropbox**, with subscription renewal, retry, and a dead-letter queue.
- **Rich ingestion** — extractors for PDF, Office (Word/Excel/PowerPoint), email, CAD, diagrams, project files, plaintext, with OCR and LibreOffice fallbacks.
- **Provider-agnostic cognition** — `local-cognitive-model`, `openai`, `azure_openai`, `anthropic`, `google_gemini`, `deepseek`, `moonshot_kimi`, and any `openai_compatible` endpoint.
- **Edge & air-gapped** — local inference stack with optional **OpenVINO NPU** acceleration; see [docs/airgapped_deployment.md](docs/airgapped_deployment.md).
- **Observability** — OpenTelemetry → OTLP/Jaeger tracing and a Prometheus metrics endpoint, on by default.

---

## Surfaces & Entrypoints

NCE is no longer "just an MCP server." It exposes several coordinated surfaces protected by ASGI `BoundedBodyMiddleware` (`NCE_MAX_HTTP_BODY_BYTES`, rejecting oversized payloads with HTTP `413`):

| Entrypoint | Transport | Purpose | Security & Limits |
| :--- | :--- | :--- | :--- |
| `server.py` | MCP stdio (JSON-RPC 2.0) | Direct tool surface for local LLM clients (Claude Desktop, Cursor) | Process-isolated stdio, tenant pinned via `NCE_MCP_NAMESPACE_ID` |
| `mcp_gateway_server.py` | Streamable HTTP `/mcp` (SSE / JSON-RPC 2.0) | Zero-trust remote MCP gateway for IDEs and desktop clients | Scoped bearer tokens, fail-closed Redis governance, `BoundedBodyMiddleware` (HTTP `413`) |
| `nce.mcp_client_proxy` | CLI bridge (stdio → HTTP `/mcp`) | Zero-secret local bridge (`nce-mcp-proxy`) connecting stdio-only MCP clients to HTTP `/mcp` | Zero database secrets on workstations, scoped token authentication |
| `admin_server.py` | HTTP (Starlette REST + Admin UI) | Operations, namespace/quota management, runtime tool toggles | HMAC-SHA256 three-header auth, anti-replay nonces, `BoundedBodyMiddleware` (HTTP `413`) |
| `nce/a2a_server.py` | HTTP (A2A RPC) | Federated, scope-bound memory sharing between agent networks | Ephemeral Ed25519/RS256 JWTs, atomic 1-time grant redemption, `BoundedBodyMiddleware` (HTTP `413`) |
| `nce/webhook_receiver/main.py` | HTTP | Inbound document-bridge change notifications | Provider HMAC verification, SSRF egress validation, `BoundedBodyMiddleware` (HTTP `413`) |
| `start_worker.py` | RQ worker | Async background jobs — code indexing, bridge sync, re-embedding | Queue-isolated execution, automatic retry, dead-letter queue (DLQ) |
| `nce/cron.py` | Scheduler | Sleep consolidation cycles, salience decay pruning, bridge renewal, GC | Distributed Redis locking, non-blocking health probes |

A **Dynamic Tools Console** in the Admin UI can enable/disable individual stdio tools or A2A skills at runtime (persisted to a Redis hash); disabled calls are rejected by the dispatch interceptor. In production, tool and engine governance fails **closed** (`-32005`) if Redis is unreachable to prevent unauthenticated or unauthorized dispatch.

---

## Quickstart

> Prerequisites: **Docker Desktop** and **Python 3.10+**.

### 1. Configure

```bash
cp .env.example .env
```

Generate real secrets in `.env` (never commit it):

- `NCE_MASTER_KEY` — ≥32 random bytes; AES-256-GCM key for PII/credential encryption. `openssl rand -base64 32`
- `NCE_API_KEY` / `NCE_ADMIN_API_KEY` / `NCE_MCP_API_KEY` — long random tokens
- `NCE_MCP_NAMESPACE_ID` — a UUID pinning the stdio connection to one tenant, e.g. `00000000-0000-4000-8000-000000000001`

### 2. Bring up the full stack

```bash
make up          # bootstraps compose secrets, then `docker compose up -d --build`
make status      # container health
```

This launches the Quad-Stack (`nce-postgres`, `nce-mongo`, `nce-redis`, `nce-minio`), the cognitive model, and the application services (`worker`, `cron`, `admin`, `a2a`, `webhook-receiver`) behind Caddy, plus Jaeger.

**Databases only** (when you want to run the app from your host):

```bash
make local-up    # docker-compose.local.yml — just Postgres, Mongo, Redis, MinIO
```

### 3. Run from the host (optional)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows ·  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

python server.py                   # MCP stdio server (listens on stdin for JSON-RPC)
python start_worker.py             # background RQ worker  (separate shell)
python -m nce.cron                 # schedulers           (separate shell)
```

### 4. Verify

```bash
make verify                        # runs verify_v1_launch.py end-to-end
```

---

## Connecting an MCP Client
 
 ### Claude Desktop
 
 Connect directly to NCE's zero-trust Streamable HTTP gateway `/mcp` or use the zero-secret `nce.mcp_client_proxy`. Neither requires database connection strings (`PG_DSN`, `MONGO_URI`) or `NCE_MASTER_KEY` on client workstations.
 
 Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
 `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):
 
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
 
 Mint scoped bearer tokens directly from the `/admin/plugins` console UI or via `POST /admin/plugins/api/mint-token`.
 
 **Cursor** — *Settings → MCP → Add New Tool*, type `command` with `python -m nce.mcp_client_proxy` or direct HTTP URL `https://localhost:8443/mcp`. A ready-to-edit template ships as [`mcp_config.json.example`](mcp_config.json.example).


---

## MCP Tool Surface

NCE provides **71** static core MCP tools dispatched through `nce/mcp_stdio_dispatch.py` and the Streamable HTTP `/mcp` gateway, enforcing auth, quotas, and runtime enable/disable state. Highlights (`[ADMIN]` tools require `admin_api_key`):

| Tool | What it does |
| :--- | :--- |
| `store_memory` | Persist a memory; extract entities; build KG edges (Saga write) |
| `store_artifact` | Ingest media/PDF/logs into MinIO + index metadata |
| `semantic_search` | 3-way Hybrid RRF (pgvector cosine + BM25 keyword + SKU/orgnr/IP boost) with `confidence_score` & `epistemic_warnings`; optional `as_of` |
| `compile_working_memory` | Cognitive synthesis tool (tool #71): assemble episodic, semantic, graph, and procedural memory tiers with epistemic confidence |
| `graph_search` | GraphRAG: anchor by similarity, cycle-safe recursive CTE BFS traversal, return subgraph; optional `as_of` |
| `describe_schema` | List live entity types & edge predicates (avoid hallucinated graph constraints) |
| `suggest_queries` / `execute_query_template` | Discover and run pre-optimised query templates |
| `index_code_file` / `check_indexing_status` / `search_codebase` | Async AST code indexing & semantic code search |
| `boost_memory` / `forget_memory` | Reinforce or zero a memory's salience |
| `list_contradictions` / `resolve_contradiction` | Surface and settle logical conflicts |
| `create_snapshot` / `list_snapshots` / `compare_states` | Point-in-time references & state diffing |
| `replay_observe` / `replay_fork` / `replay_reconstruct` / `replay_status` | Deterministic & forked replay |
| `verify_memory` / `get_event_provenance` | Integrity check & causal-chain trace |
| `a2a_create_grant` · `a2a_query_shared` · `a2a_revoke_grant` · `a2a_list_grants` · … | Cross-agent federated sharing |
| `connect_bridge` / `complete_bridge_auth` / `list_bridges` / `force_resync_bridge` | Document-bridge lifecycle |
| `manage_namespace` · `manage_quotas` · `trigger_consolidation` · `rotate_signing_key` · `get_health` · `list_dlq` | `[ADMIN]` operations |

Migration tools (`start_migration`, `validate_migration`, `commit_migration`, …) are included unless disabled, and vertical-engine tool families (`product_*`, `procurement_*`, `vendors_*`, `sales_*`, `system_design_*`, `project_*`, `d365_*`, `diag_*`, …) register when their engine is enabled. The authoritative registry is [`nce/tool_registry.py`](nce/tool_registry.py), pinned by `_EXPECTED_TOTAL` in [`tests/test_tool_registry.py`](tests/test_tool_registry.py); current per-engine and shared counts are generated in [`docs/_generated/surface.md`](docs/_generated/surface.md) and [`ENGINE_STATUS.md`](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/vertical_engines/ENGINE_STATUS.md).

---

## Security Model

- **Multi-tenant by construction.** Every application checkout passes through `scoped_pg_session`, which sets `SET LOCAL nce.namespace_id = '<tenant-uuid>'`. All 121 relational tables have RLS **enabled and forced**; policies validate via `get_nce_namespace()`. Privileged GC runs under a separate `BYPASSRLS` role, out of band.
- **Encryption at rest & GDPR crypto-shredding.** KEK/DEK AES-256-GCM envelope encryption (`encryption_deks` / `wrapped_dek`) allows cryptographic deletion of forgotten memories by nulling the wrapped DEK. PII, credentials, and sensitive cognitive payloads are encrypted under versioned KEK rings derived from `NCE_MASTER_KEY` — see [docs/pii.md](docs/pii.md).
- **Integrity & WORM serialization.** The `event_log` is append-only (a `prevent_mutation` trigger blocks edits) and hash-chained. Per-namespace WORM serialization via `pg_advisory_xact_lock` (`acquire_event_log_worm_lock`) prevents Merkle-chain races. Entries are HMAC-SHA256 signed over RFC 8785 (JCS) canonical JSON, with rotatable signing keys — see [docs/signing.md](docs/signing.md).
- **AuthN/Z & Anti-Replay.** HMAC-authenticated admin HTTP with mandatory production nonces validated against a Redis distributed store; sliding timestamp drift window; fail-closed production guards (`live_admin_override_enabled()`); JWT (HS256 secret or RS256 public key) for A2A and protected routes; optional **mTLS** behind your edge proxy.
- **Network & Ingress Defense.** ASGI `BoundedBodyMiddleware` (`NCE_MAX_HTTP_BODY_BYTES`, HTTP `413`) enforces hard payload limits across all HTTP entrypoints. SSRF guards block private IP ranges, CGNAT (`100.64.0.0/10`), and IPv4-mapped IPv6 (`::ffff:169.254.169.254`), with strict host allowlisting (`NCE_SSRF_ALLOWLIST_HOSTS`).
- **A2A federation.** Sharing tokens are stored only as SHA-256 hashes mapped to expirations and JSONB scopes; inbound queries are scope-checked then executed bound to the *owner's* RLS namespace.

Full guide: [docs/enterprise_security.md](docs/enterprise_security.md) · [docs/multi_tenancy.md](docs/multi_tenancy.md).

---

## Official Plugins

NCE provides a decoupled, pluggable architecture. Operational business engines, domain-specific tooling, and enterprise ERP/IT integrations are maintained as official extension packages:

- **[NCE Vertical Engines](https://github.com/sindrehaugen/nce-vertical-engines)**: 22 modular domain engines spanning Sales, Procurement, Product, Agreements, Vendors, Inventory, Economy, System Design, Project, Field Tech, HR, Marketing, Business Insights, Resources, Assets, Support, NetBox, Dynamics 365, Odoo, Salesforce, Diagnostics, and Customer Portal.
  - Installable via entry points (`pip install "nce-vertical-engines[all]"` or per-engine/type/category extras).
  - Dynamically mounts MCP tools and REST endpoints via the NCE Plugin interface (`nce.plugins`).
  - Enables full tenant-scoped operational automation on top of the NCE cognitive memory substrate.

For installation, engine documentation, and domain specifications, see the [nce-vertical-engines repository](https://github.com/sindrehaugen/nce-vertical-engines).

---

## Tech Stack

- **Runtime** — Python 3.10–3.12 (runtime 3.12)
- **Protocol** — MCP over JSON-RPC 2.0 (stdio and Streamable HTTP `/mcp` gateway); HTTP for admin/A2A/webhooks
- **Relational + vector** — PostgreSQL 16 with `pgvector` (HNSW iterative scan with `relaxed_order`), `pgcrypto`, and cycle-safe `WITH RECURSIVE` CTE graph queries
- **Serialization & XML** — `orjson` (accelerated JSON & `tools/list` schema caching), `defusedxml` (XML-RPC Billion Laughs / XXE prevention)
- **Episodic store** — MongoDB 7.0 (with `$in` batch hydration)
- **Cache / queues** — Redis 7.4 (+ `rq`)
- **Object storage** — MinIO (S3-compatible)
- **NLP / graph** — spaCy (entities), NetworkX, HDBSCAN, cross-encoder NLI
- **Code parsing** — Tree-sitter
- **Validation** — Pydantic V2
- **Observability** — OpenTelemetry, Prometheus, Jaeger

Transitive dependencies are pinned in [`requirements.lock`](requirements.lock); regenerate with `make lockfile`.

---

## Testing & Quality Gates

```bash
pytest tests/                 # full suite (RLS scoping, Saga rollbacks, temporal reads, tools)
pytest -m integration         # integration tests (require running backing services)

make lint                     # ruff check + ruff format --check (the CI gate)
make typecheck                # mypy (strict)
make fmt                      # apply the formatter
```

---

## Documentation

The [`docs/`](docs/) tree is the source of truth. Start here:

| Area | Document |
| :--- | :--- |
| Get running fast | [quick_start.md](docs/quick_start.md) · [developer_onboarding.md](docs/developer_onboarding.md) |
| How it talks | [usage_modes.md](docs/usage_modes.md) |
| Architecture | [architecture-v1.md](docs/architecture-v1.md) · [database_architecture.md](docs/database_architecture.md) |
| Configuration | [configuration_reference.md](docs/configuration_reference.md) · [it_admin_guide.md](docs/it_admin_guide.md) |
| Security | [enterprise_security.md](docs/enterprise_security.md) · [signing.md](docs/signing.md) · [pii.md](docs/pii.md) |
| Cognition | [cognitive_layer.md](docs/cognitive_layer.md) · [llm_providers.md](docs/llm_providers.md) |
| Time & simulation | [time_travel.md](docs/time_travel.md) · [replay.md](docs/replay.md) · [migrations.md](docs/migrations.md) |
| Integrations | [service_integrations.md](docs/service_integrations.md) · [bridge_setup_guide.md](docs/bridge_setup_guide.md) · [a2a.md](docs/a2a.md) |
| Edge | [airgapped_deployment.md](docs/airgapped_deployment.md) · [vram_monitoring.md](docs/vram_monitoring.md) |
| Plugins & Verticals | [shared-core/overview.md](docs/shared-core/overview.md) · [nce-vertical-engines](https://github.com/sindrehaugen/nce-vertical-engines) |
| Design decisions | [adr/](docs/adr/) |

---

## Production Checklist

- Set `NCE_ENV=production`; supply strong random `NCE_API_KEY`, `NCE_ADMIN_API_KEY`, `NCE_MASTER_KEY` (≥32 bytes).
- `NCE_ADMIN_PASSWORD` must be a `$pbkdf2$…` hash; set `NCE_LOAD_DOTENV=false`.
- Guardrails enforced: `NCE_ADMIN_OVERRIDE` is hard-blocked in `NCE_ENV=production` (raises `RuntimeError` at boot if set).
- Enforce TLS everywhere (`?sslmode=require` for Postgres); prefer `NCE_ADMIN_MTLS_ENABLED=true` behind your edge proxy.
- Leave the `prevent_mutation` trigger on `event_log` in place — never disable WORM.
- Migration MCP tools stay disabled in prod (`NCE_DISABLE_MIGRATION_MCP=true`) outside controlled windows.
- Rotate HMAC keys and JWT certificates on a schedule.

---

<sub>NCE — Neuro-Cognitive Engine · v3.4.0 · © Sindre Løvlie Haugen · AGPL-3.0. Formerly TriMCP.</sub>

