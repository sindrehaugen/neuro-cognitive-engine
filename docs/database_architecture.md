> **Status:** shipped · **Verified-against:** becc2ff (main) · **Last-audited:** 2026-09-18

# NCE Database Architecture

Deep-dive into the Neuro Cognitive Engine (NCE) data persistence layer: the Quad-Database stack, connection pool sizing, transaction boundaries, Row-Level Security (RLS) context initialization, the Saga pattern implementation, Saga crash-recovery logging, the GraphRAG hydration pipeline, partition strategies, and the PostgreSQL schema lifecycle.

---

## 1. Quad-Database Role Assignment

To meet enterprise requirements for performance, scalability, and strict temporal isolation, NCE distributes its data across four distinct databases, each matched to the specific storage model for which it is optimized:

| Storage Layer | Database | Access Library | Primary Role | Data Lifecycle & Retention |
| :--- | :--- | :--- | :--- | :--- |
| **Semantic Index** | PostgreSQL (with `pgvector` & `pgcrypto`) | `asyncpg` (async pool) | Enforces the relational schema, vector embeddings (768-dim, stored as `halfvec`), Knowledge Graph (KG) triplets, Row-Level Security (RLS), and the append-only (WORM) event log. | Long-term persistent storage; partitioned monthly or via hash. |
| **Episodic Archive** | MongoDB | `motor` (async driver) | Stores heavy unstructured raw payloads (full conversation transcripts, raw document pages, bulk code file contents, and media metadata). | Persistent archive; referenced via hex-encoded 24-character ObjectIDs. |
| **Working Memory & Queues** | Redis | `redis.asyncio` (async) & `redis` (sync for RQ) | Handles short-term context cache, distributed locks, rate-limiting, HMAC nonces, active token checks, and background worker queues (RQ). | Transient; TTL-evicted (default 3600s) or job-completed pruned. |
| **Object Store** | MinIO (S3 compatible) | `minio` (thread-pooled via `asyncio.to_thread`) | Archival storage of large media objects (audio recordings, images, video segments) and LLM response caches for deterministic replay. | Persistent bucket storage with path indexing. |

Global database connections are initialized and managed by the `NCEEngine` class within `nce/orchestrator.py` during application boot.

---

## 2. Connection Pools & Resource Control

### 2a. PostgreSQL Connection Pooling
NCE utilizes a high-performance, non-blocking connection pool via `asyncpg` configured in `nce/orchestrator.py`:

```python
self.pg_pool = await asyncpg.create_pool(
    cfg.PG_DSN,
    min_size=cfg.PG_MIN_POOL,    # Default: 1
    max_size=cfg.PG_MAX_POOL,    # Default: 10
    command_timeout=30,           # Hard statement timeout in seconds
)
```

* **Read Replicas**: If `DB_READ_URL` is set and differs from `PG_DSN` (`cfg.DB_READ_URL and cfg.DB_READ_URL != cfg.PG_DSN`), NCE instantiates an independent `pg_read_pool`. The pool is passed to `MemoryOrchestrator`, which routes read-only operations (such as `verify_memory`) through it via its `_db_pool(read_only=True)` helper. `graph_search` traversals use the primary `pg_pool` directly.
* **Checkout Timeouts**: Connections acquired from the pool are strictly bound by a checkout timeout constant `POOL_ACQUIRE_TIMEOUT = 10.0` seconds defined in `nce/db_utils.py`. This ensures that pool exhaustion raises a catchable timeout error rather than stalling the ASGI event loop:
  ```python
  async with pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
      # Perform database operations
  ```

### 2b. MongoDB Connection Pooling
MongoDB access is coordinated by the `AsyncIOMotorClient` pool:

```python
self.mongo_client = AsyncIOMotorClient(
    cfg.MONGO_URI,
    serverSelectionTimeoutMS=5_000,
)
```

Motor's default internal pool sizing applies; no explicit `maxPoolSize` override is set. The `serverSelectionTimeoutMS=5000` governs how long the driver will wait to find a suitable server before raising a timeout.

* **Indexes**: At boot time, `NCEEngine._init_mongo_indexes()` ensures indexes exist on `user_id` (episodes), `filepath` (code_files), and `user_id` (code_files) to guarantee lookup performance of raw documents.

### 2c. Redis Connection Pooling
Redis uses a dual-client model to accommodate asynchronous web request routing and synchronous worker queue orchestration:

```python
# Async client for cache, rate-limits, and session management
self.redis_client = redis.from_url(
    cfg.REDIS_URL,
    socket_connect_timeout=5,
    socket_timeout=5,
    max_connections=cfg.REDIS_MAX_CONNECTIONS,  # Default: 20
    health_check_interval=30,
)

# Synchronous client for the RQ background worker queue thread pool
self.redis_sync_client = redis_sync.from_url(
    cfg.REDIS_URL,
    socket_connect_timeout=5,
    socket_timeout=5,
    max_connections=cfg.REDIS_MAX_CONNECTIONS,  # Default: 20
    health_check_interval=30,
)
```

---

## 3. Transaction Boundaries & Row-Level Security (RLS)

All tenant-specific PostgreSQL operations must execute inside a transaction-scoped RLS context using the `scoped_pg_session` context manager.

### 3a. The scoped_pg_session Pattern
The `scoped_pg_session` manager guarantees that every checkout enforces the active namespace ID.

```python
# nce/db_utils.py
from contextlib import asynccontextmanager

@asynccontextmanager
async def scoped_pg_session(pool: asyncpg.Pool, namespace_id: str | UUID):
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    async with pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
        from nce.auth import set_namespace_context
        async with conn.transaction():
            await set_namespace_context(conn, ns_uuid)
            yield conn
            # SET LOCAL is automatically cleared at transaction end.
            # No explicit reset: a reset inside the finally block can
            # mask the original SQL error when the transaction is
            # already in an aborted state.
```

### 3b. Why RLS Context Requires SET LOCAL
Using `SET LOCAL` (or its equivalent `SELECT set_config('nce.namespace_id', $1, true)`) scopes the configuration setting to the immediate transaction block. When the transaction commits or aborts, PostgreSQL automatically clears the session setting, preventing cross-tenant leakage across pooled connections.

* **RLS Surface & Catalog Verification**: The authoritative tenant isolation surface encompasses 118 tables defined in `EXPECTED_TENANT_RLS_TABLES` (`nce/event_log.py`). At server startup, `verify_rls_catalog_consistency()` asserts that every tenant table has active RLS (`relrowsecurity = true`) and carries the required `tenant_isolation_policy` policy. Shared reference tables without RLS (`product_catalog`, plus 5 platform tables) are defined in `EXPECTED_GLOBAL_TABLES`. Confidential commercial pricing (`product_prices`) remains tenant-isolated.
* **Admin Bypass (`unmanaged_pg_connection`)**: A narrow set of global background maintenance operations check out connections via `unmanaged_pg_connection(pool, *, site=...)` (`nce/db_utils.py:145-160`), which skips `SET LOCAL nce.namespace_id`. Every call site must provide a registered string validated against the 22 audited sites in `UNMANAGED_PG_AUDITED_SITES` (`nce/db_utils.py:24-60`). Unregistered sites raise `ValueError` at runtime.
* **Worker principal segregation (`nce_gc`)**: The `nce_gc` role exists in `schema.sql` with the `BYPASSRLS` attribute for least-privilege worker isolation. Background maintenance **workers** (the garbage collector and the re-embedding worker) resolve their connection DSN via `db_utils.resolve_worker_dsn()`, which returns `NCE_GC_DSN` when set (so the worker connects as `nce_gc` with its own credentials) and otherwise falls back to `PG_DSN` (the app role) for backward compatibility. Note that the GC itself runs RLS-scoped per namespace (calling `set_namespace_context`), so segregation functions primarily as a credential-isolation boundary.

---

## 4. The Saga Pattern — Distributed Multi-Database Write Path

Because writing memory requires updating MongoDB (episodic archive), PostgreSQL (semantic index and graph relations), and Redis (active cache), NCE implements a Saga Pattern orchestrator to ensure eventual consistency. 

### 4a. Write Path Sequence Diagram

```mermaid
sequenceDiagram
    autonumber
    participant App as Client Application
    participant Orch as MemoryOrchestrator
    participant Redis as Redis Cache
    participant Mongo as MongoDB (Raw Payload)
    participant PG as PostgreSQL (Semantic & KG)
    participant SLog as PostgreSQL (saga_execution_log)

    App->>Orch: store_memory(payload, namespace_id)
    Orch->>SLog: Log Saga BEGIN (status: PENDING)
    
    rect rgb(240, 248, 255)
        Note over Orch,Mongo: Step 1: Ingest Episodic Payload
        Orch->>Mongo: insert_one(raw_payload)
        Mongo-->>Orch: episode_id (ObjectID)
        Orch->>SLog: Update Step 1 COMPLETED
    end

    rect rgb(245, 255, 245)
        Note over Orch,PG: Step 2: Relational & Vector Persistence
        Orch->>PG: scoped_pg_session(namespace_id)
        Orch->>PG: INSERT INTO memories (id, namespace_id, ...)
        Orch->>PG: INSERT INTO memory_embeddings (vector, ...)
        PG-->>Orch: Commit Transaction
        Orch->>SLog: Update Step 2 COMPLETED
    end

    rect rgb(255, 250, 245)
        Note over Orch,Redis: Step 3: Cache Invalidation & Working Memory
        Orch->>Redis: setex(cache_key, 3600, payload)
        Redis-->>Orch: OK
        Orch->>SLog: Update Step 3 COMPLETED
    end

    Orch->>SLog: Mark Saga COMPLETED
    Orch-->>App: Memory Object (id, namespace_id, ...)
```

### 4b. Compensating Transactions & Rollback Details
If any step in the write pipeline fails, the orchestrator triggers compensating backward transactions:
1. **PostgreSQL Failure**: If vector insertion or relational writes fail, the MongoDB episode is deleted via `AsyncIOMotorClient.delete_one({"_id": episode_id})`.
2. **Redis Failure**: Redis failures do not trigger PostgreSQL or MongoDB rollback; instead, a cache tombstone is written with a minimal TTL (5 seconds) to force cache re-hydration on subsequent reads.
3. **Audit Log Failure**: If the `saga_execution_log` entry itself cannot be created or updated, the transaction halts immediately and returns an unrecoverable database error.

### 4c. Crash-Recovery & The Garbage Collection Backstop
To handle hard process crashes midway through a multi-database write, NCE runs a background GC process (`SagaRecoveryWorker`) scheduled via `cron.py` (`cron.saga_recovery.*`):
* The worker queries `saga_execution_log` for sagas stuck in `PENDING` status older than `SAGA_TIMEOUT_SECONDS` (default: 60s).
* Uncommitted steps are rolled back in reverse order (Redis key eviction -> PostgreSQL deletion -> MongoDB payload purge).
* The saga state is updated to `ROLLED_BACK`.

---

## 5. GraphRAG Hydration Pipeline

NCE’s Knowledge Graph persistence model balances high-speed retrieval of semantic concepts with deterministic graph traversal:

```
┌────────────────────────────────────────────────────────┐
│               memories (PostgreSQL / RLS)              │
│       namespace_id = get_nce_namespace()               │
└───────────┬────────────────────────────────┬───────────┘
            │ 1:N                            │ 1:N
            ▼                                ▼
┌────────────────────────┐      ┌────────────────────────┐
│  kg_nodes (PostgreSQL) │      │  kg_edges (PostgreSQL) │
├────────────────────────┤      ├────────────────────────┤
│ - id (UUID)            │      │ - id (UUID)            │
│ - namespace_id (UUID)  │      │ - namespace_id (UUID)  │
│ - label (TEXT)         │◄────►│ - subject_label (TEXT) │
│ - entity_type (TEXT)   │      │ - predicate (TEXT)     │
│ - properties (JSONB)   │      │ - object_label (TEXT)  │
└────────────────────────┘      └────────────────────────┘
```

### 5a. Performance & Safety Guards
* **Traversal Depth Limits**: Recursive GraphRAG queries (`graph_query.py`) are hard-capped at a maximum traversal depth (default: 3 hops) to prevent cyclic dependency stalls.
* **Maximum Node Fan-Out**: Graph neighbor expansion uses `LIMIT` clauses on edge traversals (default: 50 edges per node) to guard against high-degree hub nodes exhausting memory.

---

## 6. Partitioning Strategies

NCE uses partitioned tables for high-throughput tables to maintain query performance and predictable storage index sizing over time.

### 6a. Range Partitioning (Monthly)
Tables partitioned by time range (`RANGE`) route writes to monthly partitions:
* **Partitioned Tables**:
  * `memories` (partitioned on `created_at`)
  * `event_log` (partitioned on `occurred_at`)
  * `contradictions` (partitioned on `detected_at`)
  * `pii_redactions` (partitioned on `created_at`)
* **Partition Maintenance**: At boot time and on periodic cron ticks (`cron.partition_maintenance`), `nce_ensure_event_log_monthly_partitions(p_months_ahead)` executes a PL/pgSQL function to ensure partitions exist for the current month and up to 3 months in advance.
* **Foreign Key Constraints Constraint**: PostgreSQL does not allow referencing tables partitioned by range unless the foreign key constraint includes the partition key columns. Therefore, tables like `pii_redactions` or `memory_salience` maintain **application-layer integrity** (enforced via Saga orchestrators and the Garbage Collector) rather than database-level FK constraints.

### 6b. Hash Partitioning (Scalability)
Tables partitioned by hash (`HASH`) distribute tenant data uniformly across a static modulus of partitions (default: 4):
* **Partitioned Tables**:
  * `kg_nodes` (partitioned by hash on `label`)
  * `kg_edges` (partitioned by hash on `subject_label`, `predicate`, `object_label`)
  * `memory_salience` (partitioned by hash on `memory_id`, `agent_id`)
  * `memory_embeddings` (partitioned by hash on `memory_id`)
  * `embedding_aspects` (partitioned by hash on `memory_id`, `aspect`)

---

## 7. Event Log (WORM) Architecture

The event log table (`event_log`) is configured as a Write-Once, Read-Many (WORM) store:
* **Immutability Enforcement**: An execution trigger `prevent_mutation` is attached to `event_log` and `event_parents` to block all `UPDATE` and `DELETE` queries:
  ```sql
  CREATE OR REPLACE FUNCTION prevent_mutation() RETURNS TRIGGER AS $$
  BEGIN
      RAISE EXCEPTION '% is immutable (WORM). % operation is forbidden.', TG_TABLE_NAME, TG_OP;
      RETURN NULL;
  END;
  $$ LANGUAGE plpgsql;
  ```
* **Role-Level Revocation & Deployment Reality**: In DDL, PostgreSQL roles restrict append-only security:
  ```sql
  REVOKE UPDATE, DELETE ON TABLE public.event_log FROM nce_app;
  REVOKE UPDATE, DELETE ON TABLE public.event_parents FROM nce_app;
  ```
  *Deployment and CI reality:* Runtime connections execute as superuser `mcp_user` (`rolsuper = true`), which bypasses PostgreSQL role-level table grants; the execution trigger `prevent_mutation()` above provides the active database-level barrier against mutation. Furthermore, the integration test verifying role-level rejection (`tests/test_worm_db_enforcement.py`) is parked in `KNOWN_UNWIRED` (ML-CI1) and does not currently run in CI.
* **Merkle Chain Integrity**: Each event log entry includes a `chain_hash` byte array representing the SHA-256 digest of the current record data concatenated with the previous record's `chain_hash`. A periodic cron job (`_chain_verification_tick`, default interval 120 min via `NCE_CHAIN_VERIFY_INTERVAL_MINUTES`) validates the cryptographic chain across all namespaces to ensure no logs have been modified at the database layer; verification depth per namespace per run is controlled by `NCE_CHAIN_VERIFY_STARTUP_DEPTH` (default 500 events). *(Note: while active at runtime in `nce/cron.py`, its integration test suite `tests/test_cron_chain_verify.py` is parked in `KNOWN_UNWIRED` (ML-CI1) and does not run in CI.)*

---

## 8. Dynamics 365 Integration Schema

To support tenant-scoped integrations with Microsoft Dynamics 365 (Dataverse), NCE uses the `d365_integrations` table in PostgreSQL. This table is fully protected by Row-Level Security (RLS) to enforce tenant isolation.

### 8a. Table Schema & Column Specifications
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | `UUID` | `PRIMARY KEY`, `DEFAULT gen_random_uuid()` | Unique identifier for the integration configuration. |
| `namespace_id` | `UUID` | `NOT NULL`, `REFERENCES namespaces(id) ON DELETE CASCADE` | The tenant namespace isolation boundary. |
| `org_url` | `TEXT` | `NOT NULL` | The target Dynamics 365 / Dataverse organization URL. |
| `status` | `TEXT` | `NOT NULL DEFAULT 'ACTIVE'`, `CHECK (status IN ('ACTIVE', 'DEGRADED', 'DISABLED'))` | Operational state of the integration channel. |
| `token_enc` | `BYTEA` | | AES-256-GCM encrypted JSON representation of the Access Token and Refresh Token details. |
| `token_expires_at` | `TIMESTAMPTZ` | | Expiration timestamp of the active access token. |
| `webhook_secret_enc`| `BYTEA` | | AES-256-GCM encrypted webhook validation secret. |
| `last_sync_at` | `TIMESTAMPTZ` | | Timestamp of the last execution of the synchronization worker. |
| `last_sync_stats` | `JSONB` | | Statistics and execution metrics from the last sync (e.g. accounts/contacts/opportunities count). |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Record creation timestamp. |
| `updated_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Record modification timestamp. |

* **Unique Constraints**: A composite unique constraint `UNIQUE (namespace_id, org_url)` guarantees that a tenant namespace can configure at most one integration profile per target Dataverse organization.

### 8b. AES-256-GCM Envelope Encryption
Sensitive credentials (`token_enc`, `webhook_secret_enc`) are encrypted before serialization to PostgreSQL using the NCE cryptographic signing infrastructure defined in [`nce/signing.py`](https://github.com/sindrehaugen/NCE/blob/main/nce/signing.py):
* **KDF & Master Key**: The AES wrapping key is derived from the master secret (`NCE_MASTER_KEY`) via **Argon2id** (with a salt size of 16 bytes, time cost = 3, memory cost = 64 MiB, parallelism = 4) or falls back to **PBKDF2-HMAC-SHA256** (600,000 iterations) if `argon2-cffi` is unavailable.
* **Cipher Mode**: **AES-256-GCM** (authenticated envelope encryption) wraps the plaintext using a cryptographically random 12-byte nonce generated for each encryption operation.
* **Wire / Storage Format**:
  * For Argon2id: `b'TC3\x01' || salt (16 bytes) || nonce (12 bytes) || ciphertext + tag`
  * For PBKDF2: `b'TC4\x01' || salt (16 bytes) || nonce (12 bytes) || ciphertext + tag`
* **Zeroing Buffers**: Decrypted credentials reside in process memory exclusively within `SecureKeyBuffer` context blocks to ensure that heap buffers are zeroed immediately upon block exit.

### 8c. Indexing Strategy
To ensure query performance for high-frequency runtime operations, the following indexes are defined:
* **Namespace Scan**:
  ```sql
  CREATE INDEX IF NOT EXISTS idx_d365_integrations_namespace ON d365_integrations (namespace_id);
  ```
  Improves lookup performance when loading configuration profiles within a tenant's transaction-scoped RLS context.
* **Active Status Filtering**:
  ```sql
  CREATE INDEX IF NOT EXISTS idx_d365_integrations_status ON d365_integrations (status) WHERE status = 'ACTIVE';
  ```
  A partial index that optimizes background synchronization tasks querying for active integration channels across all tenants.

### 8d. Row-Level Security (RLS) Policy
The table participates in the global tenant database boundary:
* **RLS Policies**:
  ```sql
  CREATE POLICY tenant_isolation_policy ON public.d365_integrations
      FOR ALL TO nce_app
      USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
      WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
  ```
  Any connection checking out from the connection pool under the `nce_app` role must set the active `nce.namespace_id` in its transaction context, preventing cross-tenant reads or writes.
* **Grants**:
  ```sql
  REVOKE ALL ON TABLE public.d365_integrations FROM nce_app;
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.d365_integrations TO nce_app;
  ```

---

## 9. PostgreSQL Schema Source & Lifecycle Management

> **Authoritative DDL Source:** The canonical DDL is maintained in [`nce/schema.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/schema.sql) and the sequential migration chain in [`nce/migrations/`](https://github.com/sindrehaugen/NCE/tree/main/nce/migrations/) (`001_enable_rls.sql` through `095_site_master_data.sql`).
>
> ⚠ **Important Provisioning Notice:** Do not copy or execute static embedded SQL snippets from documentation to provision database tables. Doing so bypasses the migration lifecycle and risks provisioning tenant tables without mandatory Row-Level Security (RLS) policies and security triggers. Always allow the engine to initialize its schema automatically via `NCEEngine._init_pg_schema()` or execute the versioned migrations in order.

### 9a. Schema Boot Initialization & Split Execution
At engine startup, `NCEEngine.connect()` ([`nce/orchestrator.py`](https://github.com/sindrehaugen/NCE/blob/main/nce/orchestrator.py)) invokes `_init_pg_schema()`. To prevent unledgered structural mutations and eliminate repeated backfill side effects on established databases (Q-4 step 2), schema execution is partitioned into two deterministic halves generated from canonical [`nce/schema.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/schema.sql) via `scripts/split_schema.py`:
* **`nce/schema_bootstrap.sql` (Bootstrap Execution):** Executes only when the migration ledger (`applied_migrations`) is uninitialized. Provisions baseline tables, columns, indexes, constraints, and baseline backfills on virgin databases.
* **`nce/schema_runtime.sql` (Runtime Execution):** Executes on every connection. Applies idempotent functions, triggers, and Row-Level Security policies (including the tenant isolation loop) ensuring active security invariants remain self-healing across boots.

> [!WARNING]
> **Test Environment Setup (`schema.sql` is not enough)**
> `nce/schema.sql` alone is not a usable database. A schema-only initialization will yield a false green on test runs by skipping hundreds of tests (e.g. missing `public.event_log.chain_hash`, missing `v3_cognitive_ledger`). All files in `nce/migrations/*.sql` **must be applied on top**, in filename order, to reach a full green test run.

### 9b. Complete Versioned Migrations Catalog (001 through 083)

Schema evolution is governed by chronological migration scripts located in [`nce/migrations/`](https://github.com/sindrehaugen/NCE/tree/main/nce/migrations/):

| Migration File | Primary Focus / Description | Tables Created | Tables Altered / Hardened | Triggers / Functions / Policies |
| :--- | :--- | :--- | :--- | :--- |
| [`001_enable_rls.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/001_enable_rls.sql) | Core schema migration `001_enable_rls.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`002_quota_check.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/002_quota_check.sql) | Core schema migration `002_quota_check.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`003_event_sequences_backfill.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/003_event_sequences_backfill.sql) | Core schema migration `003_event_sequences_backfill.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`004_query_catalog_and_schema_registry.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/004_query_catalog_and_schema_registry.sql) | Core schema migration `004_query_catalog_and_schema_registry.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`005_event_log_correlation.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/005_event_log_correlation.sql) | Core schema migration `005_event_log_correlation.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`006_rename_db_roles.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/006_rename_db_roles.sql) | Core schema migration `006_rename_db_roles.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`007_v3_cognitive_ledger.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/007_v3_cognitive_ledger.sql) | Core schema migration `007_v3_cognitive_ledger.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`008_audit_log.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/008_audit_log.sql) | Core schema migration `008_audit_log.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`009_reembedding_runs.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/009_reembedding_runs.sql) | Core schema migration `009_reembedding_runs.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`010_event_log_sig_version.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/010_event_log_sig_version.sql) | Core schema migration `010_event_log_sig_version.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`011_replay_runs_digest.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/011_replay_runs_digest.sql) | Core schema migration `011_replay_runs_digest.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`012_settings_table.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/012_settings_table.sql) | Core schema migration `012_settings_table.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`013_a2a_can_delegate.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/013_a2a_can_delegate.sql) | Core schema migration `013_a2a_can_delegate.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`014_a2a_one_time.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/014_a2a_one_time.sql) | Core schema migration `014_a2a_one_time.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`015_memories_envelope_dek.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/015_memories_envelope_dek.sql) | Core schema migration `015_memories_envelope_dek.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`016_halfvec_embeddings.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/016_halfvec_embeddings.sql) | Core schema migration `016_halfvec_embeddings.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`017_kg_node_embeddings_grants.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/017_kg_node_embeddings_grants.sql) | Core schema migration `017_kg_node_embeddings_grants.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`018_embedding_aspects.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/018_embedding_aspects.sql) | Core schema migration `018_embedding_aspects.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`019_muscles_schema_contract.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/019_muscles_schema_contract.sql) | Core schema migration `019_muscles_schema_contract.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`020_topology_graph_edge_unique_and_force_rls.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/020_topology_graph_edge_unique_and_force_rls.sql) | Core schema migration `020_topology_graph_edge_unique_and_force_rls.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`021_pg_trgm_extension.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/021_pg_trgm_extension.sql) | Core schema migration `021_pg_trgm_extension.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`022_c3_external_scope_rls.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/022_c3_external_scope_rls.sql) | Core schema migration `022_c3_external_scope_rls.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`023_namespace_fk_cascade.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/023_namespace_fk_cascade.sql) | Core schema migration `023_namespace_fk_cascade.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`024_outbox_saga_namespace_fk.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/024_outbox_saga_namespace_fk.sql) | Core schema migration `024_outbox_saga_namespace_fk.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`025_system_namespace.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/025_system_namespace.sql) | Core schema migration `025_system_namespace.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`026_event_log_chain_hash_present.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/026_event_log_chain_hash_present.sql) | Core schema migration `026_event_log_chain_hash_present.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`027_event_log_chain_hash_validate.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/027_event_log_chain_hash_validate.sql) | Core schema migration `027_event_log_chain_hash_validate.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`028_worm_truncate_guard.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/028_worm_truncate_guard.sql) | Core schema migration `028_worm_truncate_guard.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`029_schema_sql_only_ddl_baseline.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/029_schema_sql_only_ddl_baseline.sql) | Core schema migration `029_schema_sql_only_ddl_baseline.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`030_schema_sql_only_rls_baseline.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/030_schema_sql_only_rls_baseline.sql) | Core schema migration `030_schema_sql_only_rls_baseline.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`031_ledger_valid_to_and_worm_partition_maintenance.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/031_ledger_valid_to_and_worm_partition_maintenance.sql) | Core schema migration `031_ledger_valid_to_and_worm_partition_maintenance.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`032_signing_credentials.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/032_signing_credentials.sql) | Core schema migration `032_signing_credentials.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`033_principal_bindings.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/033_principal_bindings.sql) | Core schema migration `033_principal_bindings.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`034_outbound_webhooks.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/034_outbound_webhooks.sql) | Core schema migration `034_outbound_webhooks.sql`. | Core schema | Core tables | Core RLS / triggers |
| [`010_citus_sharding.sql`](https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/optional/010_citus_sharding.sql) *(optional)* | Core schema migration `010_citus_sharding.sql`. | Core schema | Core tables | Core RLS / triggers |

### 9c. Automated RLS Catalog Consistency Gate
To guarantee that newly added tables cannot be deployed without tenant isolation, NCE executes `verify_rls_catalog_consistency()` ([`nce/event_log.py`](https://github.com/sindrehaugen/NCE/blob/main/nce/event_log.py)) during startup. The validator compares live PostgreSQL catalog metadata (`pg_tables`, `pg_class`, `pg_policies`) against the 118 tables in `EXPECTED_TENANT_RLS_TABLES`:
* Asserts that `relrowsecurity` is enabled on all 118 tenant tables.
* Asserts that `tenant_isolation_policy` (or `a2a_grants` dual-ownership policy) is attached.
* Raises `RuntimeError` and halts server startup if any unisolated tenant table is detected.

> [!NOTE]
> **Deployment Reality vs. DDL Invariants:** While `verify_rls_catalog_consistency()` asserts that RLS policies are enabled and attached in PostgreSQL catalog metadata, runtime application connections connect as `mcp_user` (`rolsuper = true`, `rolbypassrls = true`). Zero policies target `mcp_user`, rendering RLS inert during live execution; active tenant isolation is enforced by application `WHERE`-clause predicates (see [`docs/vertical_engines/_security/enforcement-census.md`](vertical_engines/_security/enforcement-census.md)).


