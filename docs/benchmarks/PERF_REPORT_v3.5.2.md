> **Status:** shipped · **Verified-against:** a40d993 (main) · **Last-audited:** 2026-11-17

# NCE `v3.5.2` — 100k-Row Deep Profiling, Hot-Path Optimization & Sub-50ms LLM-Loop Report

**Repository:** `sindrehaugen/neuro-cognitive-engine` (`v3.5.2`) + `sindrehaugen/nce-vertical-engines` (`v3.5.2`)  
**Dataset (`nce_v352_perf`):** `100,000` `memories` + Mongo `episodes` (`TCDEK\x01` AES-256-GCM envelope-encrypted), `20,000` `memory_salience`, `5,000` `kg_nodes`, `15,000` `kg_edges`, `10,000` signed WORM `event_log` rows (`Tenant-Perf`) + `500` canary rows (`Tenant-Canary`).

---

## 1. Executive Summary & Cross-Layer Remediations (`v3.5.0` -> `v3.5.1` -> `v3.5.2`)

This canonical benchmark report consolidates the Stage 1 (`v3.5.1` 100k-row Core hot-path optimization), Stage 2 (`v3.5.1` 22-engine live Docker burn-in in `nce-vertical-engines`), and Stage 3 (`v3.5.2` `/mcp` gateway & multi-tool LLM reasoning loop tuning) findings and empirical scorecards:

### 1.1 Stage 1 Core Substrate Hot-Path Remediations (`v3.5.0` -> `v3.5.1`)
1. **Smoking Gun #1 — `246.60 ms` Per-Search Argon2id KDF Unwrap Tax (`nce/envelope.py`):**
   - With `NCE_ENVELOPE_ENCRYPTION_ENABLED=true`, `maybe_decrypt_raw_data()` called `unwrap_dek()` -> `decrypt_signing_key()` -> `_argon2id_derive_aes_key(time_cost=3, memory_cost=64 MiB)` (`~50 ms` CPU-bound per hydrated episode) on every read across `semantic_search`, `graph_search`, and `compile_working_memory`.
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
   - Tightened `motor_timeout_kwargs()` defaults to `2000ms`, reduced inline rollback `wait_for` timeout to `0.25s` when `inserted_result is None` (deferring to background `_deferred_mongo_cleanup` + `state='recovery_needed'`), and added `FOR UPDATE SKIP LOCKED` + partial index `idx_saga_log_recovery_needed` to `nce/cron.py`. Paused-Mongo failover dropped from **`10.10 s` -> `2.36 s`** (`< 3.0s`).

### 1.2 Stage 3 Cross-Layer Gateway & LLM-Loop Remediations (`v3.5.1` -> `v3.5.2`)
1. **O(1) Pre-Tokenized `/mcp` Gateway Catalog & Progressive `discover_tools` (`nce/mcp_http_gateway.py`):**
   - Cached the canonical Core + Plugin tool catalog (`_ALL_GATEWAY_TOOLS_CACHE`) and pre-tokenized search index (`_DISCOVER_INDEX_CACHE`) keyed by `(id(_DISCOVERED_PLUGINS), len(TOOL_REGISTRY), len(TOOLS))`.
   - Upgraded `discover_tools_handler()` with BM25/keyword token-overlap scoring across `(tool_name, tool_description, engine_slug, engine_description)`, added `limit` parameter support (`1..100`), stripped redundant `mcp_api_key`/`admin_api_key` properties from compact `inputSchema` payloads (`_compact_input_schema()`), and skipped redundant Redis session writes when matched tools are already expanded.
   - **Impact:** `discover_tools` E2E over `/mcp` dropped from **`35.78 ms` -> `1.345 ms` p50** (`2.465 ms` p50 / `9.322 ms` p95 under 10-agent concurrent burst).

2. **Bounded 30s Verified-Token Cache & 2s Redis Session Sync Coalescing (`nce/mcp_http_gateway.py`):**
   - Added `_VERIFIED_TOKEN_CACHE` (`30.0s` TTL, max 512 entries) with explicit `invalidate_verified_token_cache()` on revocation/rotation, and coalesced Redis `nce:mcp:sessions` re-syncs within a `2.0s` window (`last_redis_sync_mono`) during active multi-tool reasoning turns.

3. **In-Memory Plugin Descriptor & RLS Table Caches (`nce/plugins.py`):**
   - Added `_DESCRIPTOR_CACHE`, `_TENANT_RLS_CACHE`, and `_GLOBAL_TABLES_CACHE` keyed by `(NCE_ENABLED_ENGINES, NCE_ENGINE_TYPE_FILTER, NCE_ENGINE_CATEGORY_FILTER, discovered_plugins)`, invalidated automatically on `load_plugins(force_reload=True)`.

4. **Coalesced Salience Ring Buffer & `compile_working_memory` Connection Sharing (`nce/semantic_search.py`, `nce/working_memory.py`, `nce/graph_query.py`, `nce/orchestrator.py`):**
   - Added bounded `_SALIENCE_RING_BUFFER` (`maxlen=4096`) in `nce/semantic_search.py` to batch concurrent read-side salience reinforcements per `(pool, namespace_id, agent_id, delta)` into a single `reinforce_batch` transaction.
   - Coalesced the 3 fast SQL branches of `compile_working_memory` (`_fetch_kg_neighborhood`, `_fetch_heuristics`, `_fetch_contradictions`) onto a single `scoped_pg_session` connection in parallel with `_run_search()`, cutting connection pool checkouts per `compile_working_memory` call by 50% (`4 -> 2`).
   - Bounded `_hydrate_sources` DEK lookup in `nce/graph_query.py` (`LIMIT 200`) and enabled `statement_cache_size=256` on `asyncpg.create_pool` in `nce/orchestrator.py`.

---

## 2. Multi-Tool Chained LLM Reasoning Loop Scorecard (`100k` Rows, `/mcp` Streamable HTTP Gateway)

Simulated via `scripts/profile_llm_agent_loop.py` executing 5-tool chained reasoning turns (`discover_tools` -> `compile_working_memory` -> `semantic_search(limit=5)` -> `graph_search` -> `describe_schema`) over `/mcp` (`compact` mode) while `_saga_recovery_tick` and salience queries run concurrently in the background:

| Step / Metric | Stage 3 Target SLA | Warm Chained LLM Loop (`p50`) | Warm Chained LLM Loop (`p95`) | Warm Min | 10-Agent Burst (`p50`) | Status |
| :--- | ---: | ---: | ---: | ---: | ---: | :--- |
| 1. `discover_tools(limit=8)` | `p50 < 8 ms`, `p95 < 15 ms` | **`1.345 ms`** | **`9.322 ms`*** | `1.253 ms` | `2.465 ms` | **PASS** |
| 2. `compile_working_memory` | `p50 < 55 ms`, `p95 < 110 ms` | **`38.443 ms`** | **`41.804 ms`** | `38.014 ms` | `143.940 ms` | **PASS** |
| 3. `semantic_search(limit=5)` | `p50 < 45 ms`, `p95 < 95 ms` | **`35.600 ms`** | **`38.874 ms`** | `33.642 ms` | `141.637 ms` | **PASS** |
| 4. `graph_search(max_depth=2)` | `p50 < 55 ms`, `p95 < 110 ms` | **`36.184 ms`** | **`48.643 ms`** | `31.824 ms` | `188.554 ms` | **PASS** |
| 5. `describe_schema` / domain read | `p50 < 50 ms`, `p95 < 80 ms` | **`4.525 ms`** | **`6.453 ms`** | `4.266 ms` | `34.075 ms` | **PASS** |
| **Full 5-Tool Chained LLM Turn** | **Total `p50 < 200 ms`** | **`116.944 ms`** | **`230.569 ms`** | **`111.795 ms`** | `523.711 ms` | **PASS** |
| **Asyncio Event-Loop Lag** | **`p95 < 10 ms`** | **`0.000 ms`** | **`0.000 ms`** (`mean 0.112 ms`) | `0.000 ms` | `0.000 ms` (`mean 0.139 ms`) | **PASS** |

*\*Note: `discover_tools` p95 across 40 concurrent 10-agent turns is `9.322 ms` (`p99 = 11.515 ms < 15 ms`).*

---

## 3. Evolution Across `v3.5.0` -> `v3.5.1` -> `v3.5.2` (`100k` Rows)

| Operation | `v3.5.0` Baseline (`p50`) | `v3.5.1` Direct Engine (`p50`) | `v3.5.2` Full `/mcp` HTTP Gateway (`p50`) | Cumulative Speedup vs `v3.5.0` |
| :--- | ---: | ---: | ---: | ---: |
| `semantic_search(limit=5)` (prose) | `681.31 ms` (`336.26 ms` warm) | `24.46 ms` | **`35.60 ms`** (incl. HTTP + Auth + JSON-RPC) | **19.1x** |
| `semantic_search` (identifier) | `593.27 ms` | `44.62 ms` | **`44.62 ms`** (Direct) | **13.3x** |
| `semantic_search` (`as_of`) | `1,231.25 ms` | `25.06 ms` | **`25.06 ms`** (Direct) | **49.1x** |
| `compile_working_memory` | `651.32 ms` | `26.70 ms` | **`38.44 ms`** (incl. HTTP + Auth + JSON-RPC) | **16.9x** |
| `graph_search(max_depth=2)` | `108.73 ms` | `21.00 ms` | **`36.18 ms`** (incl. HTTP + Auth + JSON-RPC) | **3.0x** |
| `discover_tools` (`/mcp` compact) | `35.78 ms` | `35.78 ms` | **`1.35 ms`** | **26.5x** |
| `store_memory` (full Quad-Stack Saga) | `142.40 ms` | `114.33 ms` | **`114.33 ms`** | **1.25x** |
| **Full 5-Tool Chained LLM Turn** | `~1,515 ms` | `~148 ms` | **`116.94 ms`** (E2E over `/mcp`) | **13.0x** |

### `semantic_search` Sub-Stage Breakdown (p50 / p95 ms)

| Sub-Stage | `v3.5.0` Baseline (p50 / p95) | `v3.5.1` / `v3.5.2` Optimized (p50 / p95) | Notes |
| :--- | ---: | ---: | :--- |
| `t_redis_gov` | `0.006 / 2.454 ms` | `0.003 / 0.006 ms` | Fail-closed governance check |
| `t_embed_http` | `8.124 / 26.169 ms` | **`0.014 / 0.016 ms`** | Bounded TTL LRU query-embedding cache |
| `t_pg_checkout_guc` | `2.391 / 3.535 ms` | **`1.351 / 2.040 ms`** | Single-round-trip `set_config` in `scoped_pg_session` |
| `t_sql_exec` | `1.846 / 3.073 ms` | **`1.607 / 2.185 ms`** | HNSW `halfvec(768)` + `search_tsv` GIN + `kg_edges` trgm GIN |
| `t_mongo_hydrate` | `1.832 / 2.971 ms` | **`1.215 / 1.706 ms`** | Batch `$in` projection `{"raw_data": 1}` |
| `t_dek_decrypt` | `246.596 / 315.375 ms` | **`0.095 / 0.113 ms`** | Zeroing `bytearray` TTL LRU unwrapped-DEK cache |
| `t_postprocess_serialize` | `0.042 / 0.050 ms` | **`0.023 / 0.025 ms`** | Epistemic confidence synthesis |

---

## 4. Authentic `EXPLAIN (ANALYZE, BUFFERS)` Plans (`100k` Rows)

### 4.1 RLS `InitPlan` Proof under Non-Bypass `nce_app` Role (`0.562 ms`)
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

### 4.2 Lexical FTS Leg: `COALESCE` Seq Scan (`14.83 ms`) vs. Direct `search_tsv @@` Bitmap Index Scan (`0.61 ms`)
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

### 4.3 `kg_edges` Combined Trigram GIN Index (`idx_kg_edges_combined_trgm`, `0.21 ms` across 4 Partitions)
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

## 5. Security, Durability & Schema Drift Invariants Verified (`v3.5.2`)

| Invariant Check | Measured Result | SLA / Requirement | Status |
| :--- | :--- | :--- | :--- |
| **Cross-Tenant Canary Isolation (`100k` + `500` rows)** | `canary_leak_count = 0` (`20` prose hits, `20` ident hits) | `0` cross-tenant rows leaked | **PASS** |
| **30-Way Concurrent WORM `append_event` + `verify_merkle_chain`** | `elapsed_s = 0.491s`, `valid = true`, `checked_count = 30` | 100% Merkle chain & JCS HMAC integrity | **PASS** |
| **KEK/DEK `shred_memory` Cryptographic Erasure** | `wrapped_dek = NULL`, `dek_key_id = NULL`, `receipt_verified = true` | Unrecoverable ciphertext + signed receipt | **PASS** |
| **Fail-Closed Redis Outage (`/mcp`)** | `elapsed_s = 0.007s` (`< 2.0s`), fail-closed rejection | `< 2.0s` fail-closed | **PASS** |
| **Paused-Mongo Fail-Fast + `_saga_recovery_tick`** | `failfast_s = 2.379s` (`< 3.0s`), `remaining_recovery_needed_sagas = 0` | `< 3.0s` failover, `0` orphaned records | **PASS** |
| **Schema Drift & Migration Disjointness (`001`–`038`)** | `43/43` Core SQL tables (`36` forced-RLS, `7` global), `{plugin_scope}:` prefix | `0` missing columns/policies/indexes/triggers | **PASS** |
