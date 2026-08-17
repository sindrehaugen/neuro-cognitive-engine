-- 027_node_ownership_registry.sql
-- Machine-readable Contract-A registry: per shared node type, the sole-writer engine
-- and per-transition writer-of-record. Consulted by the write-path to enforce ownership.
--
-- This table is read by the write-path to validate that only the registered owner_engine
-- may write to a given node type. Tenant-scoped (RLS); idempotent CREATE TABLE IF NOT EXISTS.
-- ============================================================================

CREATE TABLE IF NOT EXISTS node_ownership_registry (
    id                    UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id          UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    node_type             TEXT        NOT NULL,
    transition            TEXT,
    owner_engine          TEXT        NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- Composite index for write-path lookup: (namespace, node_type, transition).
CREATE INDEX IF NOT EXISTS idx_node_ownership_registry_namespace_type_transition
    ON node_ownership_registry(namespace_id, node_type, transition);

-- Row-Level Security: each tenant sees only its own ownership registry rows.
ALTER TABLE node_ownership_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE node_ownership_registry FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON node_ownership_registry;
CREATE POLICY tenant_isolation_policy ON node_ownership_registry
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

-- Application role grants: write-path reads, and operations may insert/update (idempotent upsert).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE node_ownership_registry FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE node_ownership_registry TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE node_ownership_registry IS
'Contract-A registry: per (namespace, node_type, transition), the sole-writer engine.
Consulted by the write-path to enforce single-writer invariant. Tenant-scoped (RLS).';
