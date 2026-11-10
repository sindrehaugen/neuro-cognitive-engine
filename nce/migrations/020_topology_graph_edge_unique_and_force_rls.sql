-- 020_topology_graph_edge_unique_and_force_rls.sql
-- Core topology_graph constraints and RLS hardening.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 4.  topology_graph: close duplicate-edge gap
-- ---------------------------------------------------------------------------
-- Guard note: if duplicate rows already exist this index will fail with
-- "could not create unique index".  In that case run a dedup query first:
--
--   DELETE FROM topology_graph t1
--   USING topology_graph t2
--   WHERE t1.ctid > t2.ctid
--     AND t1.namespace_id    = t2.namespace_id
--     AND t1.source_node_id  = t2.source_node_id
--     AND t1.target_node_id  = t2.target_node_id
--     AND t1.edge_type       = t2.edge_type;
--
-- This migration does NOT issue the destructive DELETE automatically; the DBA
-- should verify absence of duplicates on production data before applying.
-- The integration DB is fresh-initialized and has no rows, so this is safe.

CREATE UNIQUE INDEX IF NOT EXISTS uq_topology_edge
    ON topology_graph (namespace_id, source_node_id, target_node_id, edge_type);

-- ---------------------------------------------------------------------------
-- 6.  Security fix — Domain 5 / D1 HIGH: FORCE RLS on topology_graph
--
--     topology_graph was created in 010_citus_sharding.sql with
--     ENABLE ROW LEVEL SECURITY but NEVER FORCE'd.  The table-owner and
--     nce_gc role (which holds BYPASSRLS-equivalent privileges) could bypass
--     the topology_graph_tenant_isolation policy when nce.namespace_id is not
--     set in their session — exposing every tenant's infrastructure topology.
--
--     ALTER TABLE … FORCE ROW LEVEL SECURITY is idempotent (safe to re-run).
--     We keep this as a standalone statement (not folded into the loop above)
--     because topology_graph carries policy name topology_graph_tenant_isolation
--     (created in migration 010), whereas the loop creates tenant_isolation_policy.
--     Folding would drop the existing, correctly-named policy and recreate it
--     under the new name — a behaviour change outside this batch's scope.
-- ---------------------------------------------------------------------------

ALTER TABLE topology_graph FORCE ROW LEVEL SECURITY;
