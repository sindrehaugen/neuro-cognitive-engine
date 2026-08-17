-- 023_d365_sync_runs.sql
-- Per-entity audit trail for Dynamics 365 vertical sync runs (run_full_sync).
-- Tenant-scoped (RLS); surfaced by the d365_sync_status MCP tool.
--
-- Durability note: rows are written on the sync connection, so per-entity rows
-- for a run that ultimately rolls back are lost with it (the common success path
-- commits fully). Cross-transaction durable error capture is a follow-up.
-- ============================================================================

CREATE TABLE IF NOT EXISTS d365_sync_runs (
    id            UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    run_id        UUID        NOT NULL,
    entity        TEXT        NOT NULL,
    upserted      INTEGER     NOT NULL DEFAULT 0,
    incremental   BOOLEAN     NOT NULL DEFAULT FALSE,
    status        TEXT        NOT NULL DEFAULT 'ok'
                              CHECK (status IN ('ok', 'error')),
    error         TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- Tenant-scoped recency lookup for the status tool.
CREATE INDEX IF NOT EXISTS idx_d365_sync_runs_namespace_time
    ON d365_sync_runs(namespace_id, started_at DESC);

-- Row-Level Security: each tenant sees only its own sync-run records.
ALTER TABLE d365_sync_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE d365_sync_runs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON d365_sync_runs;
CREATE POLICY tenant_isolation_policy ON d365_sync_runs
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

-- Application role grants (append-only audit: SELECT + INSERT).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE d365_sync_runs FROM nce_app;
        GRANT SELECT, INSERT ON TABLE d365_sync_runs TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE d365_sync_runs IS
'Per-entity audit trail of Dynamics 365 vertical sync runs. Tenant-scoped (RLS).
Surfaced by the d365_sync_status MCP tool.';
