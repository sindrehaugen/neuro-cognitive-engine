-- 010_citus_sharding.sql
-- Citus Multi-Tenant Sharding Initializer for NCE Phase 2
-- Reviewed & corrected — all 8 Technical Debt items from architectural review resolved.
--
-- Debt register applied in this file:
--   TD-010-1 (CRITICAL): namespace_id added to PKs of all distributed tables before distribution
--   TD-010-2 (CRITICAL): v3_cognitive_ledger → memories FK dropped; application-layer enforcement documented
--   TD-010-3 (CRITICAL): Monitoring views rewritten to use pg_dist_shard_placement (Citus 10+)
--   TD-010-4 (MODERATE): 2PC cross-region topology constraint documented; conditional fallback pattern noted
--   TD-010-5 (MODERATE): citus.propagate_set_commands = local added to guarantee RLS GUC propagation
--   TD-010-6 (LOW):      event_sequences kept coordinator-local with documented rationale
--   TD-010-7 (LOW):      JSONB expression index removed; replaced with safe column-only index
--   TD-010-8 (LOW):      Shard imbalance alerting view added (pct_of_total + 20% threshold flag)
--
-- Backwards Compatibility: All existing data remains accessible. Sharding is transparent
-- to the application layer after this migration completes.
-- ============================================================================

-- Enable Citus extension (requires Citus 11+ PostgreSQL package installed).
CREATE EXTENSION IF NOT EXISTS citus;

-- ============================================================================
-- PHASE 1: System-level configuration
-- (ALTER SYSTEM writes postgresql.auto.conf — requires pg_reload_conf() or restart)
-- ============================================================================

-- Enable distributed DDL propagation to all worker nodes.
ALTER SYSTEM SET citus.enable_ddl_propagation = on;

-- Two-Phase Commit (2PC) for cross-shard ACID consistency.
--
-- TOPOLOGY CONSTRAINT (TD-010-4): 2PC adds 2 network round-trips to every
-- distributed commit. Latency budget analysis:
--   Same datacenter (≤1ms RTT):  base 2.5ms + 1ms 2PC = 3.5ms   ✓ within 40ms SLA
--   Cross-Nordic region (18ms RTT): base 2.5ms + 36ms 2PC = 38.5ms  ⚠ 1.5ms margin
--
-- REQUIREMENT: All Citus worker nodes MUST be collocated in the same datacenter
-- zone as the coordinator to maintain p95 write latency < 40ms.
-- Cross-region nodes must use async streaming replication, NOT participate in 2PC.
-- For read-heavy paths in cross-region deployments, override per-session:
--   SET citus.multi_shard_commit_protocol = '1pc';
-- event_log writes (WORM integrity) MUST always use '2pc'.
ALTER SYSTEM SET citus.multi_shard_commit_protocol = '2pc';

-- Set database-level default (overrides system default for this database only).
-- TD-010-9 fix: ALTER DATABASE requires an identifier, not a function call.
-- Use dynamic SQL via DO block to inject current_database() as the DB name.
DO $$
BEGIN
    EXECUTE format(
        'ALTER DATABASE %I SET citus.multi_shard_commit_protocol = ''2pc''',
        current_database()
    );
END $$;

-- Adaptive executor: smart routing — pushes single-shard queries directly to the
-- relevant worker node without a coordinator fan-out.
ALTER SYSTEM SET citus.executor_type = 'adaptive';

-- Maximum parallel connections per adaptive executor pool.
ALTER SYSTEM SET citus.max_adaptive_executor_pool_size = 16;

-- Connection cache expiry for worker node connections (10 minutes).
ALTER SYSTEM SET citus.connection_cache_expiration = 600000;

-- Log distributed deadlock resolution events for operational debugging.
ALTER SYSTEM SET citus.log_distributed_deadlock_entries = on;

-- TD-010-5: Explicitly guarantee GUC propagation to worker connections.
-- get_nce_namespace() reads current_setting('nce.namespace_id', true) for RLS.
-- Citus propagates SET LOCAL commands within transactions to worker connections.
-- Without this setting, autocommit queries bypass GUC propagation and hit the
-- RLS EXCEPTION 'nce.namespace_id is not set' — safe (blocks, not leaks) but noisy.
--
-- Application contract: every distributed query MUST be wrapped in BEGIN/COMMIT with:
--   SET LOCAL nce.namespace_id = '<tenant_uuid>';
-- Autocommit queries to distributed tables are NOT supported under RLS.
ALTER SYSTEM SET citus.propagate_set_commands = 'local';

-- max_prepared_transactions must be >= max_connections to support 2PC safely.
-- Default of 0 disables 2PC entirely — raise to accommodate 150-tenant load.
ALTER SYSTEM SET max_prepared_transactions = 256;

-- ============================================================================
-- PHASE 2: Create topology_graph table (new in Phase 2)
-- ============================================================================
-- Stores the topological graph of infrastructure entities: devices, services, apps.
-- Used by Causal Inference Layer (BATCH-P2-004) for do-calculus and
-- Neuromorphic Spreading Activation (Phase 3).

CREATE TABLE IF NOT EXISTS topology_graph (
    id                UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id      UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    source_node_id    TEXT        NOT NULL,  -- Entity identifier (hostname, service_id, etc.)
    source_node_type  TEXT        NOT NULL,  -- "device" | "service" | "app" | "circuit"
    target_node_id    TEXT        NOT NULL,  -- Target entity identifier
    target_node_type  TEXT        NOT NULL,  -- "device" | "service" | "app" | "circuit"
    edge_type         TEXT        NOT NULL,  -- "connected_to" | "depends_on" | "host_application" | "powered_by"
    decay_coefficient FLOAT8      NOT NULL DEFAULT 0.001,  -- μ in Ebbinghaus decay model (BATCH-P2-002)
    confidence_score  FLOAT8      NOT NULL DEFAULT 0.9,    -- Retention probability R (0.0–1.0)
    last_verified     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- TD-010-1: namespace_id included in PK so Citus can enforce uniqueness across shards.
    PRIMARY KEY (id, namespace_id)
);

-- Composite indexes for source/target graph traversal (spreading activation queries).
CREATE INDEX IF NOT EXISTS idx_topology_graph_namespace_id
    ON topology_graph(namespace_id);

CREATE INDEX IF NOT EXISTS idx_topology_graph_source_node
    ON topology_graph(namespace_id, source_node_id);

CREATE INDEX IF NOT EXISTS idx_topology_graph_target_node
    ON topology_graph(namespace_id, target_node_id);

CREATE INDEX IF NOT EXISTS idx_topology_graph_edge_type
    ON topology_graph(namespace_id, edge_type);

CREATE INDEX IF NOT EXISTS idx_topology_graph_confidence
    ON topology_graph(namespace_id, confidence_score DESC);

CREATE INDEX IF NOT EXISTS idx_topology_graph_last_verified
    ON topology_graph(namespace_id, last_verified DESC);

-- Row-Level Security: isolate topology by tenant.
ALTER TABLE topology_graph ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS topology_graph_tenant_isolation ON topology_graph;
CREATE POLICY topology_graph_tenant_isolation ON topology_graph
    FOR ALL
    USING (namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON topology_graph TO nce_app;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_gc') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON topology_graph TO nce_gc;
    END IF;
END $$;

-- ============================================================================
-- PHASE 3: Amend existing table primary keys to include the shard key
-- ============================================================================
-- TD-010-1 (CRITICAL): Citus requires the distribution column (namespace_id)
-- to be part of every unique index including the primary key. Without this,
-- create_distributed_table raises:
--   ERROR: cannot create a unique index on non-distribution column
--
-- These ALTER statements are idempotent when run against a fresh database.
-- On a live single-node database with existing data, a brief ACCESS EXCLUSIVE
-- lock is taken per table. Schedule during a maintenance window.

-- memories: current PK is (id, created_at) → add namespace_id.
-- memories is PARTITION BY RANGE (created_at); the PK on the parent propagates
-- to all child partitions automatically in PostgreSQL 11+.
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_pkey CASCADE;
ALTER TABLE memories ADD PRIMARY KEY (id, created_at, namespace_id);

-- event_log: current PK is (id, occurred_at) → add namespace_id.
ALTER TABLE event_log DROP CONSTRAINT IF EXISTS event_log_pkey CASCADE;
ALTER TABLE event_log ADD PRIMARY KEY (id, occurred_at, namespace_id);

-- v3_cognitive_ledger: current PK is (id) → add namespace_id.
ALTER TABLE v3_cognitive_ledger DROP CONSTRAINT IF EXISTS v3_cognitive_ledger_pkey CASCADE;
ALTER TABLE v3_cognitive_ledger ADD PRIMARY KEY (id, namespace_id);

-- ============================================================================
-- PHASE 4: Drop unsupported distributed → distributed foreign key
-- ============================================================================
-- TD-010-2 (CRITICAL): v3_cognitive_ledger.memory_id references memories.id.
-- Both tables will be distributed on namespace_id. Citus cannot enforce a FK
-- between two distributed tables unless the FK column IS the shard key.
-- memory_id is not the shard key, so this constraint must be dropped.
--
-- Co-location guarantee: both tables share namespace_id as shard key, so a
-- v3_cognitive_ledger row and its referenced memories row always land on the
-- same shard. Integrity is enforced at the application layer.
ALTER TABLE v3_cognitive_ledger DROP CONSTRAINT IF EXISTS v3_cognitive_ledger_memory_id_fkey;

COMMENT ON COLUMN v3_cognitive_ledger.memory_id IS
'Soft reference to memories.id. FK constraint dropped for Citus distributed table
compatibility (Citus cannot enforce FKs between two distributed tables on non-shard
columns). Co-location is guaranteed by the shared namespace_id shard key — both rows
always reside on the same worker shard. Referential integrity is enforced at the
application layer in nce.orchestrators.cognitive.CognitiveOrchestrator.write_empathic_tensor().';

-- ============================================================================
-- PHASE 5: Configure reference tables (replicated to all workers)
-- ============================================================================
-- Reference tables are small, slow-changing lookup tables that distributed tables
-- join against. Citus replicates them to every worker node automatically,
-- enabling co-located JOINs without cross-shard network hops.

-- namespaces: tenant boundaries — all distributed tables FK to this.
SELECT create_reference_table('namespaces');

-- signing_keys: cryptographic key references for WORM signature verification.
SELECT create_reference_table('signing_keys');

-- TD-010-6: event_sequences is intentionally kept as a COORDINATOR-LOCAL table.
-- Rationale: event_sequences is a monotonic per-namespace counter (single UPSERT
-- per event_log write). Converting it to a reference table would require 2PC on
-- every increment (replicating the update to all workers), adding ~1ms latency to
-- every single event append. Converting it to a distributed table would shard
-- counter rows across workers, making the monotonic guarantee impossible without
-- global coordination anyway.
--
-- Architecture decision: event_sequences stays on the coordinator. The coordinator
-- owns seq generation; workers receive the already-computed seq value via the
-- distributed INSERT. This is consistent with the existing application pattern in
-- nce.event_log.append_event() which reads seq from event_sequences before
-- inserting into the distributed event_log.
--
-- No action required. Documented here for architectural completeness.

-- ============================================================================
-- PHASE 6: Distribute core tenant-scoped tables on namespace_id hash
-- ============================================================================
-- Shard count = 32:
--   At 150 tenants: expected 4.7 tenants/shard, observed max ~11 (134% tail deviation).
--   At 500 tenants: expected 15.6 tenants/shard, tail deviation compresses to ~30%.
--   32 shards is appropriate for the 500-tenant target; imbalance self-corrects at scale.
--
-- All queries to these tables MUST include namespace_id in the WHERE clause to
-- benefit from shard pruning (single-shard routing). Cross-namespace queries
-- perform a full distributed scan across all 32 shards.

SELECT create_distributed_table('memories', 'namespace_id', shard_count => 32);
SELECT create_distributed_table('event_log', 'namespace_id', shard_count => 32);
SELECT create_distributed_table('topology_graph', 'namespace_id', shard_count => 32);
SELECT create_distributed_table('v3_cognitive_ledger', 'namespace_id', shard_count => 32);

-- ============================================================================
-- PHASE 7: Create shard indexes for common query patterns
-- ============================================================================
-- Citus propagates CREATE INDEX to all shards automatically (DDL propagation on).
-- Indexes are created on each individual shard partition, not the logical table.

-- memories: fast lookup by namespace + recency (most common retrieval pattern).
CREATE INDEX IF NOT EXISTS idx_memories_namespace_created
    ON memories(namespace_id, created_at DESC)
    WHERE pii_redacted = false;

-- memories: lookup by namespace + validity window (temporal query path).
CREATE INDEX IF NOT EXISTS idx_memories_namespace_valid
    ON memories(namespace_id, valid_from DESC)
    WHERE valid_to IS NULL;

-- TD-010-7: The original JSONB expression index on metadata->>'confidence_score'
-- was removed. Expression indexes on distributed+partitioned tables do not
-- reliably propagate to all child partition shards in Citus < 12.0.
-- The valid_to IS NULL filter index above covers the dominant read path.
-- A dedicated confidence_score column index should be added once
-- nce.models.Memory.metadata['confidence_score'] is promoted to a first-class
-- column (tracked as technical debt in BATCH-P2-future: schema hardening pass).

-- event_log: fast lookup by namespace + event type + recency (dashboard, audit).
CREATE INDEX IF NOT EXISTS idx_event_log_namespace_type
    ON event_log(namespace_id, event_type, occurred_at DESC);

-- topology_graph: source-node traversal for spreading activation forward pass.
CREATE INDEX IF NOT EXISTS idx_topology_source_traversal
    ON topology_graph(namespace_id, source_node_id, edge_type);

-- topology_graph: target-node traversal for incoming dependency resolution.
CREATE INDEX IF NOT EXISTS idx_topology_target_traversal
    ON topology_graph(namespace_id, target_node_id, edge_type);

-- v3_cognitive_ledger: lookup by memory reference for decay calculations.
CREATE INDEX IF NOT EXISTS idx_v3_cognitive_memory
    ON v3_cognitive_ledger(namespace_id, memory_id)
    WHERE memory_id IS NOT NULL;

-- ============================================================================
-- PHASE 8: Monitoring views for shard distribution and imbalance detection
-- ============================================================================
-- TD-010-3 (CRITICAL): Original views referenced pg_dist_placement which was
-- removed in Citus 10.0. All views now use pg_dist_shard_placement (Citus 10+).
--
-- TD-010-8 (LOW): pct_of_total added to v_citus_shard_sizes for imbalance alerting.
-- Alert threshold: flag any worker holding more than 20% above the expected even share.
-- Formula: expected_pct = 100.0 / worker_node_count; alert if pct_of_total > expected * 1.20

-- Per-table shard placement summary.
CREATE OR REPLACE VIEW v_citus_shard_distribution AS
SELECT
    s.logicalrelid::regclass        AS table_name,
    count(DISTINCT sp.shardid)      AS shard_count,
    count(DISTINCT sp.nodename
        || ':' || sp.nodeport::text) AS worker_node_count,
    count(*)                        AS total_placement_count,
    sum(CASE WHEN sp.shardstate = 1 THEN 1 ELSE 0 END) AS active_placements,
    sum(CASE WHEN sp.shardstate != 1 THEN 1 ELSE 0 END) AS inactive_placements
FROM pg_dist_shard s
JOIN pg_dist_shard_placement sp USING (shardid)
GROUP BY s.logicalrelid
ORDER BY s.logicalrelid::text;

-- Per-worker-node shard size distribution with imbalance alerting.
-- TD-010-8: pct_of_total enables the 20% imbalance threshold check.
-- Query: SELECT * FROM v_citus_shard_sizes WHERE imbalance_flag = true;
CREATE OR REPLACE VIEW v_citus_shard_sizes AS
WITH worker_totals AS (
    SELECT
        nodename,
        sum(shardlength)                        AS node_total_bytes,
        count(*)                                AS shard_count,
        min(shardlength)                        AS min_shard_bytes,
        max(shardlength)                        AS max_shard_bytes
    FROM pg_dist_shard_placement
    WHERE shardstate = 1
    GROUP BY nodename
),
cluster_total AS (
    SELECT
        sum(shardlength)     AS cluster_total_bytes,
        count(DISTINCT nodename) AS worker_count
    FROM pg_dist_shard_placement
    WHERE shardstate = 1
)
SELECT
    wt.nodename,
    round(wt.node_total_bytes / (1024.0 * 1024.0), 2)      AS total_size_mb,
    wt.shard_count,
    wt.min_shard_bytes,
    wt.max_shard_bytes,
    round(100.0 * wt.node_total_bytes
        / NULLIF(ct.cluster_total_bytes, 0), 2)             AS pct_of_total,
    round(100.0 / NULLIF(ct.worker_count, 0), 2)           AS expected_even_pct,
    -- imbalance_flag = true when this node holds >20% more than the even share.
    -- TD-010-10 fix: guard on worker_count > 1 to suppress spurious alerts
    -- on single-worker clusters (dev/test) and during initial 2-worker data load.
    (ct.worker_count > 1)
        AND (100.0 * wt.node_total_bytes / NULLIF(ct.cluster_total_bytes, 0))
            > (120.0 / NULLIF(ct.worker_count, 0))          AS imbalance_flag
FROM worker_totals wt
CROSS JOIN cluster_total ct
ORDER BY wt.nodename;

-- Convenience view: show only imbalanced workers (>20% above expected share).
-- Integrate with alerting: SELECT count(*) FROM v_citus_shard_imbalances > 0 → page.
CREATE OR REPLACE VIEW v_citus_shard_imbalances AS
SELECT * FROM v_citus_shard_sizes WHERE imbalance_flag = true;

-- ============================================================================
-- PHASE 9: Table metadata comments
-- ============================================================================

COMMENT ON TABLE memories IS
'Distributed table: sharded on namespace_id (32 shards, hash).
All episodic, semantic, and code memories across tenants.
PK: (id, created_at, namespace_id) — namespace_id required for Citus shard uniqueness.
RLS: enabled via get_nce_namespace() GUC (requires SET LOCAL in every transaction).
Vector index: HNSW cosine on embedding(768).';

COMMENT ON TABLE event_log IS
'Distributed table: sharded on namespace_id (32 shards, hash).
Immutable WORM ledger of all cognitive events. Monthly RANGE partitions on occurred_at.
PK: (id, occurred_at, namespace_id) — namespace_id required for Citus shard uniqueness.
RLS: enabled via get_nce_namespace() GUC.
2PC: ALWAYS use 2pc for event_log writes — WORM integrity requires ACID across shards.';

COMMENT ON TABLE topology_graph IS
'Distributed table: sharded on namespace_id (32 shards, hash).
Infrastructure topology graph for Causal Inference Layer (BATCH-P2-004) and
Neuromorphic Spreading Activation (Phase 3 ATMS).
PK: (id, namespace_id) — namespace_id required for Citus shard uniqueness.
Edge types: connected_to | depends_on | host_application | powered_by.
RLS: enabled via get_nce_namespace() GUC.';

COMMENT ON TABLE v3_cognitive_ledger IS
'Distributed table: sharded on namespace_id (32 shards, hash).
Empathic Tensor storage: [valence, arousal, dominance, temporal_demand, mental_demand, frustration].
PK: (id, namespace_id) — namespace_id required for Citus shard uniqueness.
FK to memories.id: DROPPED for Citus compatibility — enforced at application layer.
Co-location with memories guaranteed via shared namespace_id shard key.
Vector index: HNSW cosine on empathic_tensor(6).
RLS: enabled via get_nce_namespace() GUC.';

COMMENT ON TABLE namespaces IS
'Reference table: replicated to all worker nodes.
Tenant boundaries and multi-tenancy root. Small and slow-changing.
All distributed tables FK to namespaces.id — co-location automatic via reference table.';

COMMENT ON TABLE signing_keys IS
'Reference table: replicated to all worker nodes.
Cryptographic key references for WORM signature verification.
Replicated so every worker can validate event_log signatures without coordinator lookup.';

-- ============================================================================
-- Migration complete — summary
-- ============================================================================
-- Extension:     citus enabled
-- Configuration: 2PC, adaptive executor, GUC propagation, DDL propagation
-- New table:     topology_graph (distributed, namespace_id shard key)
-- PK amendments: memories, event_log, v3_cognitive_ledger (namespace_id added)
-- FK dropped:    v3_cognitive_ledger → memories (application-layer enforcement)
-- Reference:     namespaces, signing_keys (replicated to all workers)
-- Distributed:   memories, event_log, topology_graph, v3_cognitive_ledger (32 shards)
-- Indexes:       6 shard-propagated indexes for common access patterns
-- Monitoring:    v_citus_shard_distribution, v_citus_shard_sizes (with imbalance flag),
--                v_citus_shard_imbalances (alert query)
-- Coordinator:   event_sequences stays coordinator-local (monotonic counter design)
--
-- All 8 TD items from architectural review resolved. Ready for BATCH-P2-002.
