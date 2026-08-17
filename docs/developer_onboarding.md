> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# NCE Developer Onboarding Guide

Welcome to the Neuro-Cognitive Engine (NCE) development team. This document covers local environment setup (Quad-DB Compose stack, schema, `.env`, tests) and the git/branch/commit/PR conventions used on this project.

---

## 1. Prerequisites

Ensure your host machine has the following tools installed:

| Tool | Minimum Version | Required For |
| :--- | :--- | :--- |
| **Python** | 3.10+ | Core engine runtime (`pyproject.toml` → `requires-python = ">=3.10"`) |
| **Docker + Compose** | Docker 24+ / Compose v2 | Local Quad-Database stack deployment |
| **Git** | Recent version | Version control and source management |

---

## 2. Step-by-Step Local Workspace Setup

Follow these steps sequentially to configure NCE on your local environment.

### Step 2a: Clone the Repository

```bash
git clone git@github.com:sindrehaugen/NCE.git
cd NCE
```

### Step 2b: Boot the Backing Services (Docker Compose)

**Option A — databases only** (recommended for host-run development):

```bash
make local-up
```

This starts `docker-compose.local.yml` which launches five containers:

| Container | Image | Host port |
| :--- | :--- | :--- |
| `nce-redis-local` | `redis:7.4-alpine` | `127.0.0.1:6379` |
| `nce-postgres-local` | `pgvector/pgvector:pg16` | `127.0.0.1:5432` |
| `nce-mongo-local` | `mongo:7.0` | `127.0.0.1:27017` |
| `nce-minio-local` | `minio/minio:RELEASE.2024-11-07T00-52-20Z` | `127.0.0.1:9002` (S3 API) |
| `nce-cognitive-local` | `ghcr.io/sindrehaugen/nce-cognitive:v1` | `127.0.0.1:11435` |

**Option B — full application stack** (includes workers, admin, A2A, webhooks, Jaeger, Caddy):

```bash
make up
```

This runs `python scripts/bootstrap-compose-secrets.py` then `docker compose up -d --build`. Full service list:

| Container | Role | Host port |
| :--- | :--- | :--- |
| `nce-postgres` | PostgreSQL 16 + pgvector | `127.0.0.1:5432` |
| `nce-mongo` | MongoDB 7.0 | `127.0.0.1:27017` |
| `nce-redis` | Redis 7.4 | `127.0.0.1:6379` |
| `nce-minio` | MinIO S3 API / console | `127.0.0.1:9002` / `127.0.0.1:9003` |
| `nce-cognitive` | Embeddings sidecar | `11435` |
| `worker` (no fixed name) | RQ background worker (profile: gpu) | — |
| `nce-cron` | APScheduler (singleton) | — |
| `nce-admin` | Starlette admin UI + REST | `8003` |
| `nce-a2a` | A2A federation endpoint | `8004` |
| `nce-webhook-receiver` | Bridge webhook receiver | `8080` |
| `nce-jaeger` | Distributed tracing UI | `16686` |
| `nce-caddy` | Edge proxy (UI + webhooks) | `8082` |

> **Worker note:** the `worker` service is gated by Compose profile `gpu` and is **not** started by `make up` (`docker compose up -d --build`) alone. To include it, run:
> ```bash
> docker compose --profile gpu up -d --build
> ```
> If you do not have a GPU or do not need the worker containerised, run `python start_worker.py` on the host instead (see §5).

Verify health after start:

```bash
make status
# docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Health}}\t{{.Ports}}"
```

### Step 2c: Schema Initialization

`nce/schema.sql` is mounted into the Postgres container as `/docker-entrypoint-initdb.d/10-schema.sql` and is applied automatically on first boot — no manual `psql` step is needed. The schema is idempotent (`IF NOT EXISTS` throughout) so restarting an existing volume is safe.

To verify that extensions and tables were created:

```bash
# Full stack:
docker exec nce-postgres psql -U mcp_user memory_meta -c "\dx"

# Local-only stack:
docker exec nce-postgres-local psql -U mcp_user memory_meta -c "\dx"
```

Expect `pgvector` and `pgcrypto` in the extension list.

**Key schema objects** (abbreviated; full DDL in `nce/schema.sql`):

| Object | Type | Notes |
| :--- | :--- | :--- |
| `namespaces` | Table | Tenant registry; hierarchical via `parent_id` |
| `signing_keys` | Table | Encrypted HMAC-SHA256 signing keys (rotatable) |
| `memories` | Partitioned table | RANGE on `created_at`; `halfvec(768)` HNSW index |
| `kg_nodes` / `kg_edges` | Partitioned tables | HASH × 4 partitions; knowledge-graph triplets |
| `event_log` | Partitioned table | WORM append-only; `prevent_mutation` trigger |
| `saga_execution_log` | Table | Cross-DB Saga state machine |
| `a2a_grants` | Table | Federated sharing tokens (SHA-256 hashed) |
| `resource_quotas` | Table | Per-namespace atomic quota engine |

All user-facing tables have `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY`. Application code must always use `scoped_pg_session` (see §7).

### Step 2d: Configure the Local `.env` File

Copy the provided environment template:

```bash
cp .env.example .env
```

Fill in the required keys. Never commit your local `.env` to version control. Critical development keys:

```ini
# --- Backing service DSNs (host → Compose-published ports) ---
MONGO_URI=mongodb://127.0.0.1:27017
PG_DSN=postgresql://mcp_user:mcp_password@127.0.0.1:5432/memory_meta
REDIS_URL=redis://127.0.0.1:6379/0
MINIO_ENDPOINT=127.0.0.1:9002
MINIO_ACCESS_KEY=mcp_admin
MINIO_SECRET_KEY=super_secure_minio_password
MINIO_SECURE=false

# --- Security (required at process start) ---
# Minimum 32 UTF-8 bytes; generate with: openssl rand -base64 32
NCE_MASTER_KEY=replace-with-32-plus-random-chars-minimum
NCE_API_KEY=replace-with-long-random-api-key
NCE_ADMIN_API_KEY=replace-with-long-random-admin-mcp-key

# --- MCP stdio client ---
NCE_MCP_API_KEY=replace-with-long-random-mcp-tenant-key
# Pin the stdio connection to one tenant namespace
NCE_MCP_NAMESPACE_ID=00000000-0000-4000-8000-000000000001
```

> `MINIO_ENDPOINT` must be `127.0.0.1:9002` when connecting from the host. Both `docker-compose.yml` and `docker-compose.local.yml` map host port **9002** → container port 9000 (the S3 API). The console is on **9003**. The `configuration_reference.md` default of `localhost:9000` applies only when MinIO runs directly on the host without Docker port remapping.

---

## 3. Python Virtual Environment Setup

Create an isolated virtual environment and install the pinned dependencies:

```bash
python -m venv .venv

# Activate — Windows (Command Prompt):
.venv\Scripts\activate
# Activate — Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Activate — macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### Optional: spaCy NLP model

Required only if working on the NLP/entity-extraction pipeline:

```bash
python -m spacy download en_core_web_sm
```

---

## 4. IDE Configuration (Cursor / Claude Desktop)

To allow an LLM client to invoke NCE tools via MCP over stdio, configure it using `mcp_config.json.example` as a template.

### Cursor

1. **Settings → MCP → + Add New MCP Server**
2. Name: `nce-memory`, Type: `command`, Command: `python`, Args: `["/absolute/path/to/NCE/server.py"]`
3. Add environment variables (same keys as your `.env`):

```text
MONGO_URI          mongodb://127.0.0.1:27017
PG_DSN             postgresql://mcp_user:mcp_password@127.0.0.1:5432/memory_meta
REDIS_URL          redis://127.0.0.1:6379/0
MINIO_ENDPOINT     127.0.0.1:9002
MINIO_ACCESS_KEY   mcp_admin
MINIO_SECRET_KEY   super_secure_minio_password
NCE_MASTER_KEY     <your-key>
NCE_MCP_API_KEY    <your-key>
NCE_MCP_NAMESPACE_ID  00000000-0000-4000-8000-000000000001
```

### Claude Desktop

Edit:
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS/Linux**: `~/Library/Application Support/Claude/claude_desktop_config.json`

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

Restart Claude Desktop to load the server.

---

## 5. Launching Services Locally (host-run mode)

When using `make local-up` (databases only), start the application processes on your host in separate shells:

| Process | Command | Purpose |
| :--- | :--- | :--- |
| MCP stdio server | `python server.py` | JSON-RPC 2.0 tool surface for IDE clients |
| RQ worker | `python start_worker.py` | Background jobs — code indexing, bridge sync, re-embedding |
| Cron scheduler | `python -m nce.cron` | Consolidation, bridge renewal, GC, outbox relay |
| Admin REST server | `python admin_server.py` | Starlette admin UI + REST on port `8003` |

Health check:

```bash
curl http://localhost:8003/api/health
```

---

## 6. Running Tests

NCE uses pytest. The `pytest.ini` in the repository root configures `asyncio_mode = strict`, a 60-second per-test timeout, and three custom markers.

### Unit tests (no live services required)

```bash
pytest
```

### Integration tests (require running Compose stack)

```bash
pytest -m integration
```

Integration tests exercise live PostgreSQL, MongoDB, Redis, and MinIO — RLS scoping, Saga rollbacks, signing-key roundtrips, and temporal reads.

### Selective runs

```bash
pytest -m "not heavy"           # skip tests that load ML models (SentenceTransformer, spaCy, CrossEncoder, OpenVINO)
pytest -m signing_isolation     # signing-key cache isolation tests only
pytest -x                       # stop on first failure
pytest -k "time_travel"         # keyword filter
pytest tests/test_signing_cache.py -v   # single file, verbose
```

### Lint and type checks

```bash
make lint        # ruff check . && ruff format --check .  (the CI gate)
make fmt         # apply ruff formatter
make typecheck   # mypy nce/
```

---

## 7. Core Contribution Standards

**Type safety.** All files under `nce/` must pass `mypy nce/` (run via `make typecheck`). New code must not introduce `Any` escapes or missing annotations.

**Linting.** `ruff check .` and `ruff format --check .` are the CI gate (`make lint`). Run `make fmt` to auto-format before committing. Line length is 100 (`pyproject.toml`).

**RLS / `scoped_pg_session`.** Every query against user data must execute inside a `scoped_pg_session(pool, namespace_id)` context. This sets `SET LOCAL nce.namespace_id = '<uuid>'` so Postgres RLS policies activate. Never pass a raw `asyncpg` connection that has not been scoped. Violating this causes test failures.

**WORM `event_log`.** Never issue `UPDATE` or `DELETE` against `event_log`. The table is protected by a `prevent_mutation` trigger and is hash-chained for auditability. This applies to migrations as well — backfill via insert-only paths.

---

## 8. Git, Branch, Commit, and PR Conventions

### Branch naming

| Work type | Pattern | Example |
| :--- | :--- | :--- |
| Feature / engine batch | `<scope>/<short-slug>` | `nce/netbox-stress-tracker` |
| Batch-based ML/module work | `ml/<descriptor>` | `ml/foundation` |
| Bug fix | `fix/<slug>` | `fix/saga-mongo-rollback` |
| Documentation | `docs/<slug>` | `docs/onboarding-update` |
| Hotfix to main | `hotfix/<slug>` | `hotfix/rls-bypass-guard` |

**One batch, one branch, one commit.** Automated batch runs (RL sequences, module ledger batches) each get their own branch and produce a single squashed commit before merging. This keeps `main` linear and makes `git bisect` fast.

### Commit message format

```
<type>(<scope>): <imperative summary under 72 chars>

<Optional body — what changed and why, not how.>

<Optional: Closes #123 / Refs #456>
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`. Scope is the subsystem (`nce`, `admin`, `a2a`, `schema`, `worker`, `ci`).

Examples:

```
feat(nce): add spreading-activation recall path with LTP/LTD weights

fix(schema): guard kg_nodes hash-partition migration against duplicate constraint

docs(onboarding): add git/branch conventions and MINIO_ENDPOINT note
```

### Pull request rules

1. **Target `main`** for all work; never push directly to `main`.
2. **All gates must be green** before requesting review: `make lint`, `make typecheck`, `pytest` (unit), `pytest -m integration` if touching DB logic.
3. **One logical change per PR.** If a branch accumulated exploratory commits, squash or fixup before opening the PR.
4. **Description.** Include: what changed, why, how to test manually, and any migration steps.
5. **Migrations.** If `nce/schema.sql` is modified, also add a numbered migration file under `nce/migrations/` and update the schema version note in `docs/database_architecture.md`.
6. **Secrets.** Never commit `.env`, `deploy/compose.stack.env`, or `deploy/compose.stack.env.generated`. If a secret was accidentally committed, rotate it immediately — rewriting history is not sufficient.

### Local quality check before pushing

```bash
make lint && make typecheck && pytest
```

Run `pytest -m integration` as well if you touched database code, Saga paths, or RLS policies.
