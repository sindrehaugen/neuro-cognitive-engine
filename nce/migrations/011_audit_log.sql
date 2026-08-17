-- 011_audit_log.sql
-- Supports BATCH-P2-003: Cascade Pruning Vector Zero-Fills
--
-- Changes:
--   1. CREATE audit_log — permanent, signed record of GDPR Article 17 deletion events.
--      Distinct from event_log (WORM append-only; cascade pruning engine needs to INSERT,
--      and event_log's WORM trigger forbids UPDATE/DELETE which would block pruning retries).
--   2. ALTER topology_graph ADD COLUMN valid_to — enables soft-deletion by the pruning engine.
--      topology_graph.valid_to = now() signals that the edge is logically deleted but
--      preserved for WORM audit trail purposes.
-- ============================================================================

-- ============================================================================
-- 1. audit_log: permanent deletion audit trail
-- ============================================================================

CREATE TABLE IF NOT EXISTS audit_log (
    id           UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id UUID        NOT NULL REFERENCES namespaces(id),
    event_type   TEXT        NOT NULL,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    signature    TEXT        NOT NULL,
    PRIMARY KEY  (id)
);

-- Tenant-scoped lookup for audit trail queries
CREATE INDEX IF NOT EXISTS idx_audit_log_namespace_time
    ON audit_log(namespace_id, occurred_at DESC);

-- Event type filter for operational dashboards
CREATE INDEX IF NOT EXISTS idx_audit_log_event_type
    ON audit_log(event_type, occurred_at DESC);

-- Row-Level Security: each tenant sees only its own audit records.
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS audit_log_tenant_isolation ON audit_log;
CREATE POLICY audit_log_tenant_isolation ON audit_log
    FOR ALL
    USING (namespace_id = get_nce_namespace());

-- Application role grants.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT ON audit_log TO nce_app;
    END IF;
END $$;

-- GC role (cascade pruning cron job) needs INSERT for deletion records.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_gc') THEN
        GRANT SELECT, INSERT ON audit_log TO nce_gc;
    END IF;
END $$;

COMMENT ON TABLE audit_log IS
'Permanent, signed audit trail for GDPR Article 17 (Right to Erasure) cascade deletions.
Distinct from event_log: event_log is the WORM cognitive event bus (no UPDATE/DELETE);
audit_log records administrative data deletion operations and is INSERT-only in practice.
Written by nce.database.pruning.cascade_delete_tenant() during tenant purge.';

-- ============================================================================
-- 2. topology_graph: add valid_to for soft-deletion support
-- ============================================================================

ALTER TABLE topology_graph ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;

-- Partial index for active (non-deleted) topology edges — the dominant read path.
CREATE INDEX IF NOT EXISTS idx_topology_graph_active
    ON topology_graph(namespace_id, edge_type)
    WHERE valid_to IS NULL;

COMMENT ON COLUMN topology_graph.valid_to IS
'Soft-deletion timestamp. NULL = edge is active. SET BY cascade_delete_tenant()
(nce.database.pruning) on GDPR Article 17 deletion. Preserved for WORM audit trail.
Hard-deletion handled by BATCH-P2-003 cascade pruning engine (future: BATCH-P2-archive).';
