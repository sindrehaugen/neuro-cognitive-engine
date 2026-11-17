> **Status:** shipped · **Verified-against:** a40d993 (main) · **Last-audited:** 2026-11-17

# NCE `v3.5.2` — Post-Burn-In Telemetry Audit, Cross-Layer Remediation & Sub-50ms LLM-Loop Report

**Repository:** `sindrehaugen/neuro-cognitive-engine` (`v3.5.2`) + `sindrehaugen/nce-vertical-engines` (`v3.5.2`)  
**Dataset (`nce_v352_perf`):** `100,000` `memories` + Mongo `episodes` (`TCDEK\x01` AES-256-GCM envelope-encrypted), `20,000` `memory_salience`, `5,000` `kg_nodes`, `15,000` `kg_edges`, `10,000` signed WORM `event_log` rows (`Tenant-Perf`) + `500` canary rows (`Tenant-Canary`).

---

## 1. Executive Summary & Cross-Layer Remediations (`v3.5.1` -> `v3.5.2`)

Following the Stage 1 (`v3.5.1` 100k-row Core hot-path optimization) and Stage 2 (`v3.5.1` 22-engine live Docker burn-in in `nce-vertical-engines`), Stage 3 (`v3.5.2`) remediated every cross-layer plugin/gateway friction point and profiled realistic multi-tool LLM reasoning loops under concurrent background maintenance:

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
| `semantic_search(limit=5)` | `681.31 ms` (`336.26 ms` warm) | `24.46 ms` | **`35.60 ms`** (incl. HTTP + Auth + JSON-RPC) | **19.1x** |
| `compile_working_memory` | `651.32 ms` | `26.70 ms` | **`38.44 ms`** (incl. HTTP + Auth + JSON-RPC) | **16.9x** |
| `graph_search(max_depth=2)` | `108.73 ms` | `21.00 ms` | **`36.18 ms`** (incl. HTTP + Auth + JSON-RPC) | **3.0x** |
| `discover_tools` (`/mcp` compact) | `35.78 ms` | `35.78 ms` | **`1.35 ms`** | **26.5x** |
| **Full 5-Tool Chained LLM Turn** | `~1,515 ms` | `~148 ms` | **`116.94 ms`** (E2E over `/mcp`) | **13.0x** |

---

## 4. Security, Durability & Resilience Invariants Verified (`v3.5.2`)

| Invariant Check | Measured Result | SLA / Requirement | Status |
| :--- | :--- | :--- | :--- |
| **Cross-Tenant Canary Isolation (`100k` + `500` rows)** | `canary_leak_count = 0` (`20` prose hits, `20` ident hits) | `0` cross-tenant rows leaked | **PASS** |
| **30-Way Concurrent WORM `append_event` + `verify_merkle_chain`** | `elapsed_s = 0.491s`, `valid = true`, `checked_count = 30` | 100% Merkle chain & JCS HMAC integrity | **PASS** |
| **KEK/DEK `shred_memory` Cryptographic Erasure** | `wrapped_dek = NULL`, `dek_key_id = NULL`, `receipt_verified = true` | Unrecoverable ciphertext + signed receipt | **PASS** |
| **Fail-Closed Redis Outage (`/mcp`)** | `elapsed_s = 0.007s` (`< 2.0s`), fail-closed rejection | `< 2.0s` fail-closed | **PASS** |
| **Paused-Mongo Fail-Fast + `_saga_recovery_tick`** | `failfast_s = 2.379s` (`< 3.0s`), `remaining_recovery_needed_sagas = 0` | `< 3.0s` failover, `0` orphaned records | **PASS** |
