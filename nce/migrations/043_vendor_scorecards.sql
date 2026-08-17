-- 043_vendor_scorecards.sql
-- Create tenant-isolated vendor_scorecards table.
-- Keyed on (vendor_id, namespace_id) with RLS policy.
-- ============================================================================

CREATE TABLE IF NOT EXISTS vendor_scorecards (
    vendor_id         TEXT        NOT NULL,
    namespace_id      UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    on_time_pct       NUMERIC,
    defect_rma_rate   NUMERIC,
    substitution_rate NUMERIC,
    reliability       NUMERIC,
    current_tier      TEXT,
    ytd_progress      NUMERIC,
    sample_n          INTEGER     NOT NULL DEFAULT 0,
    raw               JSONB       NOT NULL DEFAULT '{}'::jsonb,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (vendor_id, namespace_id)
);

CREATE INDEX IF NOT EXISTS idx_vendor_scorecards_namespace ON vendor_scorecards (namespace_id);

ALTER TABLE vendor_scorecards ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendor_scorecards FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON vendor_scorecards;
CREATE POLICY tenant_isolation_policy ON vendor_scorecards
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE vendor_scorecards FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE vendor_scorecards TO nce_app;
    END IF;
END $$;
