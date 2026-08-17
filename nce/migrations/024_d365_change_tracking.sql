-- 024_d365_change_tracking.sql
-- Dynamics 365 change-tracking delta sync: delete detection + edge/node retirement.
--
--   1. d365_delta_tokens — per (namespace, entity) Dataverse @odata.deltaLink store
--      so the change-tracking pass fetches only what changed since last run.
--   2. kg_edges / kg_nodes ADD d365_source_id — the Dataverse record GUID that
--      produced the row, so a deleted record's derived rows can be retired exactly.
--
-- Retirement is a HARD delete keyed by d365_source_id (the source record is gone,
-- so its derived graph rows go with it) — no soft-delete column / reader changes.
-- Retirement only runs when NCE_D365_CHANGE_TRACKING_ENABLED (opt-in, default off).
-- ============================================================================

-- 1. Delta-token store (tenant-scoped, RLS).
CREATE TABLE IF NOT EXISTS d365_delta_tokens (
    namespace_id  UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    entity        TEXT        NOT NULL,
    delta_link    TEXT        NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, entity)
);

ALTER TABLE d365_delta_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE d365_delta_tokens FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON d365_delta_tokens;
CREATE POLICY tenant_isolation_policy ON d365_delta_tokens
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE d365_delta_tokens FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE d365_delta_tokens TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE d365_delta_tokens IS
'Per (namespace, entity) Dataverse change-tracking deltaLink for the D365 vertical.';

-- 2. Provenance: tag D365-derived graph rows with their source Dataverse GUID.
--    Propagates to the HASH partitions automatically.
ALTER TABLE kg_edges ADD COLUMN IF NOT EXISTS d365_source_id TEXT;
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS d365_source_id TEXT;

-- Retirement-lookup indexes (only the D365-tagged subset).
CREATE INDEX IF NOT EXISTS idx_kg_edges_d365_source
    ON kg_edges (namespace_id, d365_source_id)
    WHERE d365_source_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kg_nodes_d365_source
    ON kg_nodes (namespace_id, d365_source_id)
    WHERE d365_source_id IS NOT NULL;
