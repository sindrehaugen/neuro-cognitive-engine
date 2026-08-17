> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Performance Tuning Guide

This guide consolidates every operator-tunable performance knob in NCE, explains the reasoning behind the defaults, and gives concrete sizing advice for each subsystem. Always measure before changing: the [Observability Guide](observability.md) documents the Prometheus metrics referenced throughout.

Cross-links:
- [Database Architecture](database_architecture.md) — partition DDL, pgvector indexing, read-replica topology
- [Configuration Reference](configuration_reference.md) — full env-var listing with types and validation rules
- [Observability Guide](observability.md) — metrics, OTel tracing, Prometheus scrape setup
- [VRAM Monitoring](vram_monitoring.md) — embedding GPU memory pressure and VRAM alerts

---

## Contents

1. [Measure before you tune](#1-measure-before-you-tune)
2. [PostgreSQL connection-pool sizing](#2-postgresql-connection-pool-sizing)
3. [event_log partition strategy and temporal-lookback limits](#3-event_log-partition-strategy-and-temporal-lookback-limits)
4. [Embedding throughput and batching](#4-embedding-throughput-and-batching)
5. [Redis and RQ worker concurrency](#5-redis-and-rq-worker-concurrency)
6. [MCP tool cache behaviour](#6-mcp-tool-cache-behaviour)
7. [Latency budgets](#7-latency-budgets)
8. [Cron thundering-herd mitigation](#8-cron-thundering-herd-mitigation)
9. [Quick-reference: all tunable knobs](#9-quick-reference-all-tunable-knobs)

---

## 1. Measure before you tune

Every subsystem below exposes Prometheus metrics. Collect baselines before touching defaults.

| Metric | Source | What it tells you |
|---|---|---|
| `nce_tool_latency_seconds{tool_name}` | `nce/observability.py:TOOL_LATENCY` | End-to-end MCP tool call latency; buckets at 10 ms … 60 s |
| `nce_saga_duration_seconds{operation,result}` | `nce/observability.py:SAGA_DURATION` | Distributed saga duration; buckets at 100 ms … 20 s |
| `nce_scoped_session_latency_seconds` | `nce/observability.py:SCOPED_SESSION_LATENCY` | Time to acquire a pool connection and run SET LOCAL RLS; buckets at 0.1 ms … 50 ms |
| `nce_embedding_count{model_id}` | `nce/observability.py:EMBEDDING_COUNT` | Embedded chunks per model — rate tells you embedding load |
| `nce_reembedder_vram_allocated_bytes{worker_id}` | `nce/observability.py:REEMBEDDER_VRAM_ALLOCATED` | VRAM in use by the re-embedder process |
| `nce_event_log_partition_months_ahead` | `nce/observability.py:EVENT_LOG_PARTITION_MONTHS_AHEAD` | Partition runway — alert when < 2 |
| `nce_quota_consumed_total` / `nce_quota_remaining` | `nce/observability.py:QUOTA_CONSUMED/QUOTA_REMAINING` | Per-namespace quota pressure |

Enable observability with `NCE_OBSERVABILITY_ENABLED=true` (default). The Prometheus exporter starts on `NCE_PROMETHEUS_PORT` (default `8000`). See [Observability Guide](observability.md) for scrape configuration.

---

## 2. PostgreSQL connection-pool sizing

NCE uses **asyncpg** connection pools for both the primary write path and the optional read-replica path. The pool is created in `nce/orchestrator.py`:

```python
self.pg_pool = await asyncpg.create_pool(
    cfg.PG_DSN,
    min_size=cfg.PG_MIN_POOL,   # default 1
    max_size=cfg.PG_MAX_POOL,   # default 10
    command_timeout=30,
)
```

An identical pool is opened against `DB_READ_URL` when read-replica splitting is configured (only when `DB_READ_URL != PG_DSN`).

### Defaults

| Variable | Default | Hard minimum |
|---|---|---|
| `PG_MIN_POOL` | `1` | 1 |
| `PG_MAX_POOL` | `10` | 1 |

### Sizing guidance

**Start-up pool size (`PG_MIN_POOL`):** Keep at `1` unless you pay for idle connections. The pool grows on demand.

**Peak concurrency (`PG_MAX_POOL`):** The formula is:

```
PG_MAX_POOL = (concurrent_tool_calls × avg_DB_time_fraction) + headroom
```

A practical starting point for a single NCE process:
- Developer / low-load: `PG_MAX_POOL=5`
- Standard production: `PG_MAX_POOL=20`
- High-traffic (many concurrent agents): `PG_MAX_POOL=50`

PostgreSQL itself imposes a per-database connection ceiling (`max_connections`). Leave room for PgBouncer (`PG_BOUNCER_URL`), the garbage-collector worker (which opens its own pool with `min_size=1, max_size=2`), and the re-embedding worker (same). Rule of thumb: `PG_MAX_POOL × num_nce_replicas ≤ max_connections × 0.8`.

**PgBouncer:** When `PG_BOUNCER_URL` is set, point `PG_DSN` at PgBouncer and set `PG_MAX_POOL` to match the PgBouncer pool size. asyncpg's statement-level cache interacts poorly with PgBouncer transaction mode; use session mode or `prepared_statement_cache_size=0`.

**Read-replica split:** Set `DB_READ_URL` to route semantic search and temporal queries to a replica. `DB_WRITE_URL` controls the write primary. Both pools share the same `PG_MIN_POOL` / `PG_MAX_POOL` sizing variables, so size generously when splitting.

**Monitoring:** `nce_scoped_session_latency_seconds` histogram indicates pool contention. If p99 exceeds 5 ms consistently, increase `PG_MAX_POOL` or add a replica.

---

## 3. event_log partition strategy and temporal-lookback limits

### Partition maintenance

`event_log` is range-partitioned by month. The cron job `event_log_partition_maintenance` runs the PostgreSQL function `nce_ensure_event_log_monthly_partitions(N)` on the first of every month at `00:00 UTC`, creating the next `N` monthly partitions ahead of time.

```
NCE_PARTITION_LOOKAHEAD_MONTHS  default=3  minimum=1
```

The cron lock prevents concurrent runs across replicas. After each run, `nce_event_log_partition_months_ahead` is updated; a reading of `< 2` triggers a warning log. Alert on this gauge.

**Sizing recommendation:** Keep `NCE_PARTITION_LOOKAHEAD_MONTHS=3` (default). Increase to `6` only if you perform infrequent deployments (e.g. quarterly maintenance windows) where the cron job might not fire. Decreasing below `2` risks missing partitions if a deployment or cron failure spans a month boundary.

### Temporal-lookback limits

`as_of` queries reconstruct state from the event log. Unbounded lookbacks cause full-table scans on large partitions.

```
NCE_MAX_TEMPORAL_LOOKBACK_DAYS  default=90  (0 = no boundary, not recommended)
```

This is enforced in `nce/temporal.py:parse_as_of()`. An absolute hard ceiling of 3650 days (10 years) applies regardless of this setting.

| Value | Use case |
|---|---|
| `90` (default) | Standard production — covers most audit and debugging needs |
| `365` | Compliance-heavy deployments needing full-year lookback |
| `0` | Admin maintenance tasks only — disables the boundary entirely |
| `30` | Cost-sensitive / high-volume deployments where event_log is large |

Setting `NCE_MAX_TEMPORAL_LOOKBACK_DAYS=0` disables the guard and may cause slow queries on `event_log` tables with hundreds of millions of rows. If you operate with `0`, ensure partial indexes on `(namespace_id, created_at)` exist. See [Database Architecture](database_architecture.md) for index details.

---

## 4. Embedding throughput and batching

NCE supports multiple hardware backends (CPU, CUDA, ROCm, XPU, OpenVINO NPU, MPS via `NCE_BACKEND`). The public `embed_batch()` function in `nce/embeddings.py` handles chunking, per-text truncation, and event-loop yielding.

### Batch API limits (input guards)

These limits are enforced at the API boundary — batches exceeding them raise `ValueError` before any model call.

| Variable | Default | Purpose |
|---|---|---|
| `NCE_EMBED_MAX_BATCH_TEXTS` | `512` | Maximum number of texts per `embed_batch()` call |
| `NCE_EMBED_MAX_TEXT_CHARS` | `32000` | Per-text character truncation before embedding |

### Internal chunking

`EMBED_BATCH_CHUNK` controls how many texts are sent to the model at once within a single `embed_batch()` call. Between chunks the async event loop is yielded via `asyncio.sleep(0)` so other coroutines can run.

| Variable | Default | Effect |
|---|---|---|
| `EMBED_BATCH_CHUNK` | `64` | Texts per model call within a batch; lower = more responsive event loop |
| `EMBEDDING_MAX_WORKERS` | `1` | Thread-pool workers for the blocking model call |

**Tuning `EMBED_BATCH_CHUNK`:** Larger values improve throughput by reducing Python overhead per inference call. Smaller values keep the event loop responsive for concurrent tool calls. For dedicated embedding workers with no async concurrency, `128`–`256` is reasonable. For shared NCE processes, keep at `64`.

**Tuning `EMBEDDING_MAX_WORKERS`:** Each worker is a thread-pool executor thread. With a single GPU, `1` is correct (the GPU serializes). On CPU-only deployments with many cores, `2`–`4` can improve throughput at the cost of higher context-switch overhead.

### Re-embedding worker rate limiting

The background re-embedding worker (`nce/reembedding_worker.py`) migrates rows to a new model version in bounded, rate-limited batches.

| Variable | Default | Notes |
|---|---|---|
| `REEMBED_BATCH_SIZE` | `32` | Rows per embed batch |
| `REEMBED_BATCHES_PER_MINUTE` | `20` | Rate cap — inter-batch sleep = `60 / batches_per_minute` = 3 s |
| `REEMBED_MAX_ROWS_PER_RUN` | `0` | Maximum rows per cron invocation; `0` = unlimited |
| `REEMBED_MAX_TEXT_CHARS` | `4096` | Per-text truncation for re-embedding (separate from live path) |
| `REEMBED_CRON_INTERVAL_MINUTES` | `60` | How often the cron fires a re-embedding sweep |
| `REEMBED_INCLUDE_KG_NODES` | `false` | Also re-embed `kg_nodes` (adds significant extra work) |

The inter-batch sleep enforces the rate cap: `_sleep = 60.0 / batches_per_minute`. At the default of `20`, one batch runs every 3 s; a 32-row batch at 3 s = ~640 rows/minute. Increase `REEMBED_BATCHES_PER_MINUTE` only after confirming the embedding model and database can sustain the throughput.

The re-embedder uses a Redis distributed lock (`nce:reembed:embed_lock`) to prevent concurrent embedding across multiple worker replicas. If Redis is unavailable, the worker proceeds without the lock (same as single-replica behaviour).

See [VRAM Monitoring](vram_monitoring.md) for GPU memory pressure metrics during re-embedding runs.

---

## 5. Redis and RQ worker concurrency

### Redis connection pool

Both the async Redis client (used by the MCP dispatch loop) and the sync Redis client (used by RQ) are initialized in `nce/orchestrator.py`:

```python
redis.from_url(
    cfg.REDIS_URL,
    socket_connect_timeout=5,
    socket_timeout=5,
    max_connections=cfg.REDIS_MAX_CONNECTIONS,
    health_check_interval=30,
)
```

| Variable | Default | Notes |
|---|---|---|
| `REDIS_MAX_CONNECTIONS` | `20` | Hard cap on pooled connections per client instance |
| `REDIS_TTL` | `3600` | General-purpose Redis TTL for caching primitives (seconds) |

**Sizing `REDIS_MAX_CONNECTIONS`:** Each NCE process opens two Redis clients (async + sync). With `REDIS_MAX_CONNECTIONS=20`, a process can hold up to 40 connections total. For a single-node Redis deployment with one NCE replica, the default is fine. Scale linearly with the number of NCE replicas: `REDIS_MAX_CONNECTIONS = max(20, ceil(max_concurrent_sagas / replicas))`.

### RQ priority lanes

The RQ worker in `start_worker.py` dequeues from three lanes in priority order:

```
high_priority  →  batch_processing  →  default
```

- `high_priority` (`HIGH_PRIORITY_QUEUE`): user-facing / real-time API extractions
- `batch_processing` (`BATCH_QUEUE`): webhooks, bridge re-syncs, bulk `index_all.py` runs
- `default`: legacy backward-compatibility lane

All job classes are idempotent. The `RecoveringWorker` subclass runs a `StartedJobRegistry` sweep on every maintenance tick to requeue jobs abandoned by crashed workers — no manual intervention is needed after a worker OOM.

### Worker concurrency

RQ worker concurrency is controlled at the process level (number of worker processes), not by an env var. Run multiple `python start_worker.py` processes to scale out. Each process handles one job at a time. For burst capacity, scale horizontally: run 3–5 workers for medium deployments, 8–16 for high-throughput document indexing pipelines.

The dead-letter queue (`TASK_MAX_RETRIES=5` by default) ensures failing jobs do not consume worker capacity indefinitely. Monitor `nce_task_dlq_total` and `nce_task_dlq_backlog` to detect poison-pill patterns.

**Job result retention:**
- Successful jobs: `RESULT_TTL = 86400 s` (24 h, hardcoded in `start_worker.py`)
- Failed jobs: `FAILURE_TTL = 604800 s` (7 d)

---

## 6. MCP tool cache behaviour

Cacheable tool responses are stored in Redis using `SETEX` with `MCP_CACHE_TTL_S = 300 s` (5 minutes). This value is a module constant in `nce/constants.py` — it is not currently overridable via env var.

### Which tools are cached

The following tools have `cacheable=True` in `nce/tool_registry.py`:

| Tool | Domain |
|---|---|
| `semantic_search` | Memory |
| `search_codebase` | Code |
| `graph_search` | Knowledge graph |
| `neuromorphic_search` | Knowledge graph |
| `d365_query_case` | Dynamics 365 |
| `d365_case_stress_report` | Dynamics 365 |
| `d365_netbox_mappings` | Dynamics 365 / NetBox |

### Cache key structure

```
mcp_cache:v{generation}:{namespace_id}:{tool_name}:{sha256(args)}
```

Auth and transport keys (`admin_api_key`, `mcp_api_key`, etc.) are stripped before hashing so API key rotation does not produce cache misses.

### Cache invalidation

Two invalidation mechanisms exist:

**Generation-based (coarse):** Mutation tools (`mutation=True`) call `bump_cache_generation()` after a successful write. This increments the global `mcp_cache_generation` counter in Redis. All cache entries with a previous generation value become unreachable on the next lookup — effectively a full-cache flush. The old keys expire naturally at their 300 s TTL.

**Document-scoped (fine-grained):** `forget_memory` and `delete_snapshot` call `purge_document_cache()` to SCAN and delete cache keys matching the deleted document's `memory_id`. This avoids serving stale results for a specific document after deletion.

**Quota interaction:** Cache hits are served before the quota check runs. Quota is consumed only on cache misses. This is intentional: read-heavy namespaces should not burn quota on repeated identical queries.

### Tuning cache behaviour

- **Increase hit rate:** The 300 s TTL is appropriate for most read patterns. If your workload has very frequent identical queries (e.g. dashboards polling every 30 s), the cache will serve them efficiently.
- **Disable caching for debugging:** There is no global cache-disable switch. Set `NCE_QUOTA_REDIS_COUNTERS=false` to investigate quota vs cache interactions, but do not disable Redis.
- **Namespace-scoped purge:** Call `purge_namespace_cache(redis_client, namespace_id)` programmatically (or the corresponding admin endpoint) to evict all cache entries for a tenant after a large data import.

### Concurrent tool limit

The dispatch loop wraps tool execution in an asyncio semaphore:

```
NCE_MAX_CONCURRENT_TOOLS  default=16  minimum=1
```

This limits the number of tool calls running simultaneously within one NCE process. If `nce_tool_latency_seconds` shows queueing-style latency spikes (p50 low, p99 high), raise this value or scale out replicas.

---

## 7. Latency budgets

The Prometheus histograms define implicit latency budgets through their bucket boundaries. The table below translates those buckets into operational targets.

### MCP tool call latency (`nce_tool_latency_seconds`)

Buckets: `0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, +Inf`

| Percentile | Target | Notes |
|---|---|---|
| p50 | < 100 ms | Simple reads, cache hits |
| p95 | < 1 s | Semantic search with embedding |
| p99 | < 5 s | Complex graph traversals, large-context snapshots |
| Max acceptable | 30 s | Timeout cliff — clients typically cut off at 30 s |

Cache hits (`semantic_search`, `graph_search`, etc.) should land in the `< 50 ms` bucket. If they do not, check Redis latency and pool contention.

### Saga duration (`nce_saga_duration_seconds`)

Buckets: `0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, +Inf`

| Percentile | Target | Notes |
|---|---|---|
| p50 | < 500 ms | Normal `store_memory` saga: PG + Mongo + Redis |
| p95 | < 2 s | Acceptable under load |
| p99 | < 10 s | High — investigate Mongo write concern or PG index pressure |

`SAGA_DURATION` is labelled `{operation, result}`. Compare `result="success"` vs `result="compensated"` to detect which operation type is slow.

### Scoped-session acquisition (`nce_scoped_session_latency_seconds`)

Buckets: `0.1 ms, 0.5 ms, 1 ms, 2 ms, 5 ms, 10 ms, 20 ms, 50 ms, +Inf`

| Percentile | Target | Action if exceeded |
|---|---|---|
| p99 | < 2 ms | This measures connection pool checkout + `SET LOCAL` RLS overhead |
| > 10 ms | Warning | Increase `PG_MAX_POOL`; check Postgres server CPU |
| > 20 ms | Critical | Pool is exhausted — scale `PG_MAX_POOL` or add a read replica |

---

## 8. Cron thundering-herd mitigation

When multiple NCE replicas boot simultaneously (rolling deployment, `docker compose --scale`), all cron schedulers fire at the same time. This causes bursty database CPU spikes.

```
CRON_STARTUP_JITTER_MAX_SECONDS  default=60.0
```

On startup, each NCE instance sleeps a random duration uniformly drawn from `[0, CRON_STARTUP_JITTER_MAX_SECONDS]` before the first cron cycle fires. Subsequent cron ticks inherit the offset evenly, so the jitter is one-time only.

Set to `0` to disable (useful in single-replica dev deployments). For deployments of 5+ replicas, increase to `120` s to spread load further.

Cron jobs also use an advisory lock (`CronLock`) keyed to the job name, so even if two instances fire at the same time, only one will actually execute the cron body.

---

## 9. Quick-reference: all tunable knobs

| Variable | Default | Subsystem |
|---|---|---|
| `PG_MIN_POOL` | `1` | PostgreSQL pool |
| `PG_MAX_POOL` | `10` | PostgreSQL pool |
| `DB_READ_URL` | (falls back to `PG_DSN`) | Read-replica split |
| `DB_WRITE_URL` | (falls back to `PG_DSN`) | Write primary |
| `PG_BOUNCER_URL` | `""` | PgBouncer passthrough |
| `REDIS_MAX_CONNECTIONS` | `20` | Redis pool |
| `REDIS_TTL` | `3600` | Redis general TTL (s) |
| `NCE_MAX_TEMPORAL_LOOKBACK_DAYS` | `90` | Temporal query guard |
| `NCE_PARTITION_LOOKAHEAD_MONTHS` | `3` | event_log partition runway |
| `NCE_EMBED_MAX_BATCH_TEXTS` | `512` | Embedding API input guard |
| `NCE_EMBED_MAX_TEXT_CHARS` | `32000` | Per-text truncation |
| `EMBED_BATCH_CHUNK` | `64` | Embedding internal chunk size |
| `EMBEDDING_MAX_WORKERS` | `1` | Embedding thread-pool size |
| `REEMBED_BATCH_SIZE` | `32` | Re-embedding rows per batch |
| `REEMBED_BATCHES_PER_MINUTE` | `20` | Re-embedding rate cap |
| `REEMBED_MAX_ROWS_PER_RUN` | `0` | Re-embedding run cap (0 = unlimited) |
| `REEMBED_MAX_TEXT_CHARS` | `4096` | Re-embedding per-text truncation |
| `REEMBED_CRON_INTERVAL_MINUTES` | `60` | Re-embedding cron frequency |
| `REEMBED_INCLUDE_KG_NODES` | `false` | Extend re-embedding to kg_nodes |
| `NCE_MAX_CONCURRENT_TOOLS` | `16` | Async tool semaphore per process |
| `TASK_MAX_RETRIES` | `5` | RQ job DLQ retry threshold |
| `CRON_STARTUP_JITTER_MAX_SECONDS` | `60.0` | Anti-thundering-herd startup delay |
| `NCE_QUOTA_REDIS_COUNTERS` | `true` | Atomic Redis quota increments |
| `NCE_QUOTA_REDIS_FLUSH_INTERVAL_S` | `60` | Quota flush to PostgreSQL interval |
| `GC_INTERVAL_SECONDS` | `3600` | Garbage-collector run interval |
| `GC_PAGE_SIZE` | `500` | GC batch page size |
| `GC_ORPHAN_AGE_SECONDS` | `86400` | Age before orphaned rows are reaped |
| `CONSOLIDATION_CRON_INTERVAL_MINUTES` | `360` | Memory consolidation frequency |
| `OUTBOX_RELAY_INTERVAL_SECONDS` | `5` | Transactional outbox relay cadence |

For validation rules, type constraints, and production-mandatory vs optional flags, see the full [Configuration Reference](configuration_reference.md).
