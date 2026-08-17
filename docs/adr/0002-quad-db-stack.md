> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# ADR-0002: Quad-DB Stack — asyncpg + Motor + Redis + MinIO

## Status

Shipped

## Context

NCE must store four structurally distinct classes of data with different access patterns:

1. **Relational / transactional** — tenant metadata, memory metadata, graph edges, RLS-enforced records: require ACID transactions, row-level security, and partitioned range queries.
2. **Document / unstructured payloads** — raw episode content, code files, LLM prompt-response pairs: variable-schema JSON blobs; size makes them poor fits for row storage.
3. **Ephemeral / fast** — agent cognitive state, task queues (RQ), A2A skill toggles: sub-millisecond reads; data is transient or cache-warm.
4. **Binary object storage** — LLM payload archives for deterministic replay, snapshot exports: large blobs, streaming reads, content-addressable by hash.

A single database cannot serve all four access patterns efficiently. A pure-Postgres approach would materialise large JSON payloads in rows (bloating TOAST storage and slowing vacuum) and would require an external object store for GB-scale replay exports regardless.

Alternatives considered:
- **Postgres only** — adequate for (1) and possibly (2) with JSONB, but ill-suited for (3) and (4).
- **MongoDB only** — no ACID cross-document transactions required for graph edges; no native RLS.
- **Redis + Postgres (no Mongo)** — possible, but blobs still need an external store.

## Decision

`NCEEngine` (in `nce/orchestrator.py`) owns and initialises four clients, each mapped to its role:

| Client | Library | Role |
|--------|---------|------|
| `pg_pool` | `asyncpg` | Relational: tenanted data, schema enforcement, RLS |
| `mongo_client` | `motor.motor_asyncio.AsyncIOMotorClient` | Document: heavy episode/code payloads |
| `redis_client` | `redis.asyncio` | Ephemeral: agent state, RQ task queues, feature flags |
| `minio_client` | `minio.Minio` | Object: LLM payload archive, snapshot exports |

All four are connected in `NCEEngine.connect()` before any request is served. An optional read-replica pool (`pg_read_pool`) is created from `DB_READ_URL` when it differs from `PG_DSN`.

Distributed writes across PG + Mongo follow the Python Saga pattern: a PG failure triggers Mongo cleanup to prevent orphaned documents.

**Source citations** (verified via `git show main:<path>`):
- `nce/orchestrator.py:1-4` — module docstring: "Tri-Stack … Python Saga Pattern … Redis, Postgres, and MongoDB"
- `nce/orchestrator.py:111-116` — `NCEEngine.__init__`: `self.pg_pool`, `self.mongo_client`, `self.redis_client`, `self.minio_client` properties declared
- `nce/orchestrator.py:129-132` — `AsyncIOMotorClient` initialised in `connect()`
- `nce/orchestrator.py:133-138` — `asyncpg.create_pool` initialised in `connect()`
- `nce/orchestrator.py:139-150` — `redis.asyncio.from_url` initialised in `connect()`
- `nce/orchestrator.py:174` — `Minio(...)` initialised in `connect()`, comment: "New Quad-Stack MinIO property" (`nce/orchestrator.py:116`)
- `health_probe.py:11-12` — `import asyncpg` + `from motor.motor_asyncio import AsyncIOMotorClient` — health probe verifies all four
- `nce/admin_handlers/fleet.py:984` — `api_admin_db_minio_status` — MinIO admin endpoint

## Consequences

### Positive

- Each store is matched to its optimal access pattern; no impedance mismatch between data shape and storage engine.
- MinIO enables GB-scale streaming exports and deterministic replay without loading data into Python heap (server-side cursor + object streaming).
- The Saga pattern prevents cross-store data corruption on partial failures.

### Negative / Trade-offs

- Four operational services must be healthy for the engine to start; any outage degrades the full system.
- Cross-store transactions (PG + Mongo) are eventually-consistent; the Saga compensating path only fires on PG failure, not Mongo failure.
- `health_probe.py` must verify all four backends; partial health states are harder to reason about.
- MinIO is not replicated by default in single-node deployments; replay archive durability depends on the MinIO deployment configuration.
