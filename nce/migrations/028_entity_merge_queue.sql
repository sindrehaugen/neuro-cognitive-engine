-- 028_entity_merge_queue.sql
-- Entity merge queue: stores sub-threshold proposed merges awaiting human confirmation.
-- Prevents silent graph corruption from false identity merges; distinct from enrichment confidence.
--
-- Idempotent CREATE TABLE IF NOT EXISTS. FORCE RLS to ensure cross-tenant isolation.
-- ============================================================================

CREATE TABLE IF NOT EXISTS entity_merge_queue (
    id                    UUID            NOT NULL DEFAULT gen_random_uuid(),
    namespace_id          UUID            NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    node_type             TEXT            NOT NULL,
    candidate_payload     JSONB           NOT NULL,
    target_node_id        UUID,
    score                 DOUBLE PRECISION NOT NULL,
    status                TEXT            NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'rejected')),
    created_at            TIMESTAMPTZ     NOT NULL DEFAULT now(),
    decided_by            TEXT,
    decided_at            TIMESTAMPTZ,
    PRIMARY KEY (id)
);

-- Index for efficient lookup by namespace and status.
CREATE INDEX IF NOT EXISTS idx_entity_merge_queue_namespace_status
    ON entity_merge_queue(namespace_id, status);

-- Index for pagination/timeline queries.
CREATE INDEX IF NOT EXISTS idx_entity_merge_queue_created_at
    ON entity_merge_queue(namespace_id, created_at DESC);

-- Row-Level Security: each tenant sees only its own merge queue rows.
ALTER TABLE entity_merge_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_merge_queue FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON entity_merge_queue;
CREATE POLICY tenant_isolation_policy ON entity_merge_queue
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

-- Application role grants: read queue, and operations may insert/update/delete.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE entity_merge_queue FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE entity_merge_queue TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE entity_merge_queue IS
'Sub-threshold proposed entity merges awaiting human confirmation.
Distinct from enrichment-confidence review; prevents silent graph poisoning from false identity merges.
Tenant-scoped (RLS). Status: pending | confirmed | rejected.';
