# NCE — Neuro-Cognitive Engine

> A cognitive memory and reasoning substrate for autonomous agents.
> Persistent, multi-tenant, time-travelling memory across a four-database stack — with a brain on top.

<p>
  <img alt="version" src="https://img.shields.io/badge/version-3.0.0-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-3776ab">
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
- [Vertical Modules](#vertical-modules)
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

See [docs/cognitive_layer.md](docs/cognitive_layer.md) and [docs/netbox_and_cognitive_extensions.md](docs/netbox_and_cognitive_extensions.md).

---

## System Architecture

```mermaid
flowchart TB
  subgraph Clients [Clients]
    IDE[MCP clients · Claude Desktop · Cursor]
    PEER[Peer agent networks]
    OPS[Operators / Admins]
  end

  subgraph Surfaces [Surfaces]
    STDIO[server.py · MCP stdio]
    A2A[a2a_server.py · A2A federation]
    ADM[admin_server.py · REST + Admin UI]
    WH[webhook_receiver · document bridges]
  end

  subgraph Background [Background processing]
    RQ[start_worker.py · RQ worker]
    CRON[nce.cron · schedulers]
  end

  subgraph Engine [NCEEngine — orchestration]
    ORCH[Saga write path]
    COG[Cognitive workers]
    TMP[Temporal / replay]
  end

  subgraph Data [Quad-Database Stack]
    PG[(PostgreSQL + pgvector)]
    MG[(MongoDB)]
    RD[(Redis)]
    S3[(MinIO)]
  end

  IDE --> STDIO
  PEER --> A2A
  OPS --> ADM
  STDIO --> ORCH
  A2A --> ORCH
  ADM --> ORCH
  WH --> RQ
  RQ --> ORCH
  CRON --> COG
  ORCH --> PG & MG & RD & S3
  COG --> PG & MG
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

- **Hybrid recall** — `semantic_search` (pgvector cosine) with MongoDB hydration, `graph_search` GraphRAG BFS traversal, and neuromorphic **spiking spreading activation** with LTP/LTD weight adaptation.
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

NCE is no longer "just an MCP server." It exposes several coordinated surfaces:

| Entrypoint | Transport | Purpose |
| :--- | :--- | :--- |
| `server.py` | MCP stdio (JSON-RPC 2.0) | Tool surface for LLM clients (Claude Desktop, Cursor, …) |
| `admin_server.py` | HTTP (Starlette REST + Admin UI) | Operations, namespace/quota management, runtime tool toggles |
| `nce/a2a_server.py` | HTTP (A2A RPC) | Federated, scope-bound memory sharing between agent networks |
| `nce/webhook_receiver` | HTTP | Inbound document-bridge change notifications |
| `start_worker.py` | RQ worker | Async jobs — code indexing, bridge sync, re-embedding |
| `nce/cron.py` | Scheduler | Consolidation cycles, bridge renewal, GC |

A **Dynamic Tools Console** in the Admin UI can enable/disable individual stdio tools or A2A skills at runtime (persisted to a Redis hash); disabled calls are rejected by the dispatch interceptor. If Redis is unreachable the interceptor fails *open* to avoid cascading outages.

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

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "nce-memory": {
      "command": "python",
      "args": ["/absolute/path/to/NCE/server.py"],
      "env": {
        "MONGO_URI": "mongodb://127.0.0.1:27017",
        "PG_DSN": "postgresql://mcp_user:mcp_password@127.0.0.1:5432/memory_meta",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "MINIO_ENDPOINT": "127.0.0.1:9002",
        "MINIO_ACCESS_KEY": "mcp_admin",
        "MINIO_SECRET_KEY": "super_secure_minio_password",
        "NCE_MASTER_KEY": "your-32-byte-master-key",
        "NCE_MCP_API_KEY": "your-client-api-key",
        "NCE_MCP_NAMESPACE_ID": "00000000-0000-4000-8000-000000000001"
      }
    }
  }
}
```

**Cursor** — *Settings → MCP → Add New Tool*, command `python`, args `["/absolute/path/to/NCE/server.py"]`, same `env` block. A ready-to-edit template ships as [`mcp_config.json.example`](mcp_config.json.example).

---

## MCP Tool Surface

Tools are dispatched through `nce/mcp_stdio_dispatch.py`, which enforces auth, quotas, and runtime enable/disable state. Highlights (`[ADMIN]` tools require `admin_api_key`):

| Tool | What it does |
| :--- | :--- |
| `store_memory` | Persist a memory; extract entities; build KG edges (Saga write) |
| `store_artifact` | Ingest media/PDF/logs into MinIO + index metadata |
| `semantic_search` | Vector cosine search + Mongo hydration; optional `as_of` |
| `graph_search` | GraphRAG: anchor by similarity, BFS the KG, return a subgraph; optional `as_of` |
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

Migration tools (`start_migration`, `validate_migration`, `commit_migration`, …) are included unless disabled, and vertical-engine tool families (`product_*`, `procurement_*`, `vendors_*`, `sales_*`, `system_design_*`, `project_*`, `d365_*`, `diag_*`, …) register when their engine is enabled. The authoritative registry is [`nce/tool_registry.py`](nce/tool_registry.py) — **109 tools** as of the current `main`.

---

## Security Model

- **Multi-tenant by construction.** Every application checkout passes through `scoped_pg_session`, which sets `SET LOCAL nce.namespace_id = '<tenant-uuid>'`. All relational tables have RLS **enabled and forced**; policies validate via `get_nce_namespace()`. Privileged GC runs under a separate `BYPASSRLS` role, out of band.
- **Encryption at rest.** PII, credentials, and biometric tensors are AES-256-GCM encrypted under `NCE_MASTER_KEY`. An automated PII pipeline (Presidio/regex) supports redaction and reversible pseudonymisation — see [docs/pii.md](docs/pii.md).
- **Integrity & non-repudiation.** The `event_log` is append-only (a `prevent_mutation` trigger blocks edits) and hash-chained; entries are HMAC-SHA256 signed over RFC 8785 (JCS) canonical JSON, with rotatable signing keys. See [docs/signing.md](docs/signing.md).
- **AuthN/Z.** HMAC-authenticated admin HTTP with optional Redis-backed replay protection; JWT (HS256 secret or RS256 public key) for A2A and protected routes; optional **mTLS** behind your edge proxy.
- **A2A federation.** Sharing tokens are stored only as SHA-256 hashes mapped to expirations and JSONB scopes; inbound queries are scope-checked then executed bound to the *owner's* RLS namespace.

Full guide: [docs/enterprise_security.md](docs/enterprise_security.md) · [docs/multi_tenancy.md](docs/multi_tenancy.md).

---

## Vertical Modules

NCE ships domain verticals that turn the cognitive core into an operational tool.

### Shared-Core Foundation

All business engines sit on a common cross-engine foundation (C1–C9): **entity resolution** with a merge-review queue and survivorship rules, **autonomy governance** (`@governed` confirm-first gates, value/volume ceilings, allowlists, kill switch), **external-principal RLS** scoping for partner/customer access, **allow-list field redaction**, a per-function **`d365 | both | nce` source-mode** switch with divergence logging, deterministic **pricing resolution**, a cryptographic **signing ceremony** for documents that leave the system, and **structural grounding guards** (citation-required answers, no-ranked-people rule). Guide: [docs/shared-core/overview.md](docs/shared-core/overview.md).

### Engines

Per-namespace opt-in; documented under [docs/engines/](docs/engines/) (see the [docs index](docs/README.md#vertical-engine-guides)):

- **Product** — catalog search & on-demand enrichment, related-product/BOM matching, golden-record survivorship, EOL watchers.
- **Procurement** — TCO calculation, supplier ranking, three-way match, and PO generation/submission behind confirm-first autonomy ceilings (real money never moves without a human).
- **Agreements** — OCR → structured agreement extraction with a human review queue (money/legal terms never auto-promote), kickback reconciliation, coverage analysis.
- **Vendors & Contractors** — vendor registry with scorecards (sparse data scores neutral, not bad), contractor matching & dispatch, partner-scoped views.
- **Sales** — D365-mirrored lead → opportunity → quote read model, dealroom pricing via the shared pricing resolver, a **single immutable signed-baseline freeze per quote** (append-only at the database grant level), public customer quote links (HMAC-tokenised, redacted), and source-mode divergence tracking.
- **System Design** — propose-only design generation with a human validation gate, device/topology capability checks, SoW generation with freeze-on-issue *(design ↔ quote round-trip planned)*.
- **Project** — G0–G6 phase gates with config-as-IP criteria, signed-quote conversion (reads the Sales baseline), event-driven BOM→task sync, My-Day/capacity/scope-creep insights *(partial — some surfaces REST-only or not yet wired)*.
- **NetBox** — GraphQL topology activation (sites/racks/devices/cables → adjacency graph), unregistered-asset discovery against live telemetry, a do-calculus circuit-provider escalator, longitudinal **operator stress tracking** with on-call weight redistribution, and an **active-learning queue** (low-confidence memories quarantined for gamified operator review). There is also a NetBox **Cognitive Dashboard** Django plugin under `src/nce-netbox-plugin/`.
- **Dynamics 365** — case enrichment with graph context, entity sync to `kg_edges`, empathic-tensor frustration/burnout reports, SLA-breach records from the WORM log, and a D365 ↔ NetBox cross-reference mapper.
- **Diagnostics** — log-bundle digestion pipeline (streaming ingest, digest writer, enrichment, source profiles) surfaced through `diag_*` tools.

Details: [docs/netbox_and_cognitive_extensions.md](docs/netbox_and_cognitive_extensions.md) · [docs/d365_integration_reference.md](docs/d365_integration_reference.md).

---

## Tech Stack

- **Runtime** — Python 3.10+
- **Protocol** — MCP over JSON-RPC 2.0 (stdio); HTTP for admin/A2A/webhooks
- **Relational + vector** — PostgreSQL 16 with `pgvector` and `pgcrypto`
- **Episodic store** — MongoDB 7.0
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
| Business engines | [shared-core/overview.md](docs/shared-core/overview.md) · [engines/](docs/engines/) |
| Design decisions | [adr/](docs/adr/) |

---

## Production Checklist

- Set `NCE_ENV=production`; supply strong random `NCE_API_KEY`, `NCE_ADMIN_API_KEY`, `NCE_MASTER_KEY` (≥32 bytes).
- `NCE_ADMIN_PASSWORD` must be a `$pbkdf2$…` hash; set `NCE_LOAD_DOTENV=false`, `NCE_ALLOW_ADMIN_DOTENV_PERSIST=false`.
- Keep guardrails on: `NCE_ADMIN_OVERRIDE=false`, `NCE_BYPASS_WORM=false`, `NCE_BYPASS_RLS=false`.
- Enforce TLS everywhere (`?sslmode=require` for Postgres); prefer `NCE_ADMIN_MTLS_ENABLED=true` behind your edge proxy.
- Leave the `prevent_mutation` trigger on `event_log` in place — never disable WORM.
- Migration MCP tools stay disabled in prod (`NCE_DISABLE_MIGRATION_MCP=true`) outside controlled windows.
- Rotate HMAC keys and JWT certificates on a schedule.

---

<sub>NCE — Neuro-Cognitive Engine · v3.0.0 · © Sindre Løvlie Haugen · AGPL-3.0. Formerly TriMCP.</sub>
