> **Status:** shipped · **Verified-against:** a40d993 (main) · **Last-audited:** 2026-11-17

# NCE Core `v3.5.1` — 100k-Row Deep Profiling & Hot-Path Optimization Report

**Repository:** `sindrehaugen/neuro-cognitive-engine` (`v3.5.1`)  
**Dataset (`nce_v351_perf`):** `100,000` `memories` + Mongo `episodes` (`TCDEK\x01` AES-256-GCM envelope-encrypted), `20,000` `memory_salience`, `5,000` `kg_nodes`, `15,000` `kg_edges`, `10,000` signed WORM `event_log` rows (`Tenant-Perf`) + `500` canary rows (`Tenant-Canary`).

---

## 1. Executive Summary & Root-Cause Findings

1. **Smoking Gun #1 — `246.60 ms` Per-Search Argon2id KDF Unwrap Tax (`nce/envelope.py`):**
   - With `NCE_ENVELOPE_ENCRYPTION_ENABLED=true`, `maybe_decrypt_raw_data()` called `unwrap_dek()` -> `decrypt_signing_key()` -> `_argon2id_derive_aes_key(time_cost=3, memory_cost=64 MiB)` (`~50 ms` CPU-bound per hydrated episode!) on every read across `semantic_search`, `graph_search`, and `compile_working_memory`.
   - **Fix:** Added a bounded TTL LRU cache (`_UNWRAPPED_DEK_CACHE`, `maxsize=512, ttl=300s`) keyed by `(master_key_fingerprint_bytes(master_key), sha256(wrapped_dek).digest())` holding mutable `bytearray` buffers that are zeroed in-place on eviction and explicitly purged via `clear_unwrapped_dek_cache()` on `shred_memory`, `forget_memory`, and `crypto_shred_subject`. `wrap_dek()` also pre-warms the cache on write.
   - **Impact:** `t_dek_decrypt` p50 dropped from **`246.60 ms` -> `0.095 ms`** (**2,595x faster**).

2. **Smoking Gun #2 — `COALESCE(m.search_tsv, m.content_fts)` Parallel Seq Scan & `halfvec(768)` Cast (`nce/semantic_search.py`):**
   - `COALESCE(m.search_tsv, m.content_fts) @@ query` prevented PostgreSQL from using `memories_default_search_tsv_idx`, forcing a `Parallel Seq Scan` across all 100k rows (`13.57 ms`, 8,448 buffers). Additionally, `m.embedding <=> $1::vector` on a `halfvec(768)` column forced runtime casts and blocked HNSW index usage on `as_of` queries (`1,231.25 ms` p50).
   - **Fix:** Rewrote the lexical predicate to `m.search_tsv @@ query.q_eng OR m.search_tsv @@ query.q_simple OR (m.search_tsv IS NULL AND ...)` (`Bitmap Index Scan` in `0.61 ms`, 76 buffers — **22.2x faster**) and aligned vector distance casts to `::halfvec(768)` across `nce/semantic_search.py` and `nce/graph_query.py`.

3. **Smoking Gun #3 — `kg_edges` Trigram `OR ILIKE` & `memories` Duplicate `payload_ref` Lookup (`nce/semantic_search.py`, `nce/working_memory.py`, `nce/graph_query.py`, Migration `038`):**
   - Added expression index `idx_kg_edges_combined_trgm ON kg_edges USING gin (((subject_label || ' ' || object_label)) gin_trgm_ops) WHERE payload_ref IS NOT NULL` in migration `038` and rewrote `kg_edges` trigram filters to `((subject_label || ' ' || object_label) ILIKE '%' || $q || '%')` (`Bitmap Index Scan` in `0.21 ms` across 4 hash partitions).
   - Added `idx_memories_ns_payload_ref_active` and constrained `_hydrate_sources` DEK lookup to `AND valid_to IS NULL`.

4. **Smoking Gun #4 — RLS Volatility & Per-Checkout GUC Round-Trips (`nce/migrations/038_v351_hotpath_rls_and_indexes.sql`, `nce/db_utils.py`, `nce/embeddings.py`):**
   - Marked `get_nce_namespace()`, `get_nce_external_scope()`, and `nce_decayed_score()` `STABLE PARALLEL SAFE` and wrapped all tenant RLS policy predicates in `(SELECT get_nce_namespace())` so PostgreSQL evaluates the GUC once per statement as an `InitPlan` (`0.56 ms`).
   - Combined `set_config('nce.namespace_id', ...)` and `set_config('hnsw.iterative_scan', 'relaxed_order', true)` into a single SQL round-trip in `scoped_pg_session`, and added a stateless query-embedding TTL LRU cache (`_embed_query_cache`) in `nce/embeddings.py` (`t_embed_http` p50 `8.12 ms -> 0.014 ms`).

5. **Smoking Gun #5 — `10.1s` Paused-Mongo Failover Stall (`nce/_client_factory.py`, `nce/orchestrators/memory.py`, `nce/cron.py`):**
   - When `nce-mongo-local` was paused, `insert_one` waited `5.0s` (`socketTimeoutMS=5000`) and `_apply_rollback_on_failure` immediately waited another `5.0s` calling `delete_one` on the same paused container (`10.1s` total).
   - **Fix:** Tightened `motor_timeout_kwargs()` defaults to `2000ms`, reduced inline rollback `wait_for` timeout to `0.25s` when `inserted_result is None` (deferring to background `_deferred_mongo_cleanup` + `state='recovery_needed'`), and added `FOR UPDATE SKIP LOCKED` + partial index `idx_saga_log_recovery_needed` to `nce/cron.py`. Paused-Mongo failover dropped from **`10.10 s` -> `2.36 s`** (`< 3.0s`).

---

## 2. Before vs. After Latency Comparison (100k Rows, 30 Warm Calls)

| Operation / Sub-Stage | `v3.5.0` Baseline p50 | `v3.5.0` Baseline p95 | `v3.5.1` Optimized p50 | `v3.5.1` Optimized p95 | Speedup (p50) |
| :--- | ---: | ---: | ---: | ---: | ---: |
| `semantic_search` (prose) | `336.26 ms` | `404.19 ms` | **`24.46 ms`** | **`26.46 ms`** | **13.7x** |
| `semantic_search` (identifier) | `593.27 ms` | `682.29 ms` | **`44.62 ms`** | **`47.92 ms`** | **13.3x** |
| `semantic_search` (`as_of`) | `1,231.25 ms` | `1,358.01 ms` | **`25.06 ms`** | **`29.13 ms`** | **49.1x** |
| `graph_search` (live, `max_depth=2`) | `108.73 ms` | `133.85 ms` | **`21.00 ms`** | **`27.86 ms`** | **5.2x** |
| `graph_search` (`as_of`, `max_depth=2`) | `186.66 ms` | `224.36 ms` | **`75.66 ms`** | **`97.00 ms`** | **2.5x** |
| `compile_working_memory` | `651.32 ms` | `716.85 ms` | **`26.70 ms`** | **`43.08 ms`** | **24.4x** |
| `store_memory` (full Quad-Stack Saga) | `142.40 ms` | `151.77 ms` | **`114.33 ms`** | **`130.17 ms`** | **1.25x** |
| **Mongo-Pause Saga Failover** | `10.10 s` | `10.10 s` | **`2.36 s`** | **`2.36 s`** | **4.3x** |
| **Cross-Tenant Canary Leak Count** | `0` | `0` | **`0`** | **`0`** | **0 leaks** |

### `semantic_search` Sub-Stage Breakdown (p50 / p95 ms)

| Sub-Stage | `v3.5.0` Baseline (p50 / p95) | `v3.5.1` Optimized (p50 / p95) | Notes |
| :--- | ---: | ---: | :--- |
| `t_redis_gov` | `0.006 / 2.454 ms` | `0.003 / 0.006 ms` | Fail-closed governance check |
| `t_embed_http` | `8.124 / 26.169 ms` | **`0.014 / 0.016 ms`** | Bounded TTL LRU query-embedding cache |
| `t_pg_checkout_guc` | `2.391 / 3.535 ms` | **`1.351 / 2.040 ms`** | Single-round-trip `set_config` in `scoped_pg_session` |
| `t_sql_exec` | `1.846 / 3.073 ms` | **`1.607 / 2.185 ms`** | HNSW `halfvec(768)` + `search_tsv` GIN + `kg_edges` trgm GIN |
| `t_mongo_hydrate` | `1.832 / 2.971 ms` | **`1.215 / 1.706 ms`** | Batch `$in` projection `{"raw_data": 1}` |
| `t_dek_decrypt` | `246.596 / 315.375 ms` | **`0.095 / 0.113 ms`** | Zeroing `bytearray` TTL LRU unwrapped-DEK cache |
| `t_postprocess_serialize` | `0.042 / 0.050 ms` | **`0.023 / 0.025 ms`** | Epistemic confidence synthesis |

---

## 3. Authentic `EXPLAIN (ANALYZE, BUFFERS)` Plans (`100k` Rows)

### 3.1 RLS `InitPlan` Proof under Non-Bypass `nce_app` Role (`0.562 ms`)
```text
Limit  (cost=833.40..189948.26 rows=10 width=49) (actual time=0.322..0.354 rows=10 loops=1)
  Buffers: shared hit=301
  InitPlan 1 (returns $0)
    ->  Result  (cost=0.00..0.01 rows=1 width=32) (actual time=0.001..0.001 rows=1 loops=1)
  ->  Index Scan using memories_default_embedding_idx on memories_default memories
        Order By: (embedding <=> $1::halfvec(768))
        Filter: ((valid_to IS NULL) AND (namespace_id = $0))
Execution Time: 0.562 ms
```

### 3.2 Lexical FTS Leg: `COALESCE` Seq Scan (`14.83 ms`) vs. Direct `search_tsv @@` Bitmap Index Scan (`0.61 ms`)
```text
-- BEFORE (COALESCE(m.search_tsv, m.content_fts) @@ query):
Parallel Seq Scan on memories_default m  (cost=0.00..17419.04 rows=1 width=20) (actual time=6.918..11.264 rows=17 loops=3)
  Buffers: shared hit=5016 read=3432
Execution Time: 14.830 ms

-- AFTER (m.search_tsv @@ (query.q_eng || query.q_simple)):
Bitmap Heap Scan on memories_default m  (cost=133.75..137.77 rows=1 width=20) (actual time=0.168..0.588 rows=50 loops=1)
  Buffers: shared hit=75 read=1
  ->  Bitmap Index Scan on memories_default_search_tsv_idx1  (cost=0.00..133.75 rows=1 width=0) (actual time=0.110..0.110 rows=550 loops=1)
        Buffers: shared hit=31
Execution Time: 0.610 ms
```

### 3.3 `kg_edges` Combined Trigram GIN Index (`idx_kg_edges_combined_trgm`, `0.21 ms` across 4 Partitions)
```text
Append  (cost=73.59..310.45 rows=4 width=33) (actual time=0.040..0.210 rows=76 loops=1)
  Buffers: shared hit=122
  ->  Bitmap Heap Scan on kg_edges_0  (actual time=0.068..0.109 rows=26 loops=1)
        ->  Bitmap Index Scan on kg_edges_0_expr_idx  (actual time=0.062..0.063 rows=26 loops=1)
  ->  Bitmap Heap Scan on kg_edges_1  (actual time=0.041..0.055 rows=18 loops=1)
        ->  Bitmap Index Scan on kg_edges_1_expr_idx  (actual time=0.038..0.038 rows=18 loops=1)
  ->  Bitmap Heap Scan on kg_edges_2  (actual time=0.042..0.055 rows=15 loops=1)
        ->  Bitmap Index Scan on kg_edges_2_expr_idx  (actual time=0.039..0.039 rows=15 loops=1)
  ->  Bitmap Heap Scan on kg_edges_3  (actual time=0.040..0.051 rows=17 loops=1)
        ->  Bitmap Index Scan on kg_edges_3_expr_idx  (actual time=0.038..0.038 rows=17 loops=1)
```

---

## 4. Cross-Repo Seam & Schema Drift Audit

- **Migration `037` / `038` Ledger Disjointness:** Core `_apply_pg_migrations()` records bare filenames (`037_perf_hot_path_indexes.sql`, `038_v351_hotpath_rls_and_indexes.sql`) in `applied_migrations`, whereas `_apply_plugin_pg_migrations()` prefixes plugin migrations with `{plugin_scope}:` (`nce_verticals:037_bom_line_priced.sql`). `_verify_schema_version()` filters `if ':' not in k`, guaranteeing zero cross-repo collision when `nce-vertical-engines` is mounted.
- **Schema Drift Coverage (`scripts/check_schema_drift.py`):** Reconciled `expected_columns()` (`33` base tables in `nce/schema.sql`) with `expected_migration_columns()` (`10` additional tables created in Core migrations `004`–`035`), reporting all **43 Core SQL tables** (`0` missing tables, columns, policies, indexes, triggers, or functions).
