-- 071_business_insights_engine.sql
-- ============================================================================
-- Business Insights Engine (Module 16, Wave 1 -- bi-schema-cockpit):
-- Table backing nce/vertical_modules/business_insights/**:
--   1. business_insights_kpi_snapshots (cached point-in-time roll-ups and trend history)
--
-- STRICT ROW LEVEL SECURITY + EXPLICIT NAMESPACE PREDICATE ENFORCEMENT
-- --------------------------------------------------------------------------
-- Enables and forces RLS for nce_app.
-- All queries must carry explicit WHERE namespace_id = $1 predicates.
-- ============================================================================

CREATE TABLE IF NOT EXISTS business_insights_kpi_snapshots (
    id                          UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id                UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    kpi_key                     TEXT        NOT NULL,
    value                       NUMERIC,
    period                      TEXT        NOT NULL DEFAULT 'live',
    captured_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_engine               TEXT        NOT NULL,
    business_insights_source_id  TEXT,
    raw                         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT business_insights_kpi_key_not_blank
        CHECK (btrim(kpi_key) <> '')
);

CREATE INDEX IF NOT EXISTS idx_bi_kpi_snapshots_ns_key_period
    ON business_insights_kpi_snapshots (namespace_id, kpi_key, period);
CREATE INDEX IF NOT EXISTS idx_bi_kpi_snapshots_ns_captured
    ON business_insights_kpi_snapshots (namespace_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_bi_kpi_snapshots_source_id
    ON business_insights_kpi_snapshots (business_insights_source_id) WHERE business_insights_source_id IS NOT NULL;

ALTER TABLE business_insights_kpi_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_insights_kpi_snapshots FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON business_insights_kpi_snapshots;
CREATE POLICY tenant_isolation_policy ON business_insights_kpi_snapshots
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE business_insights_kpi_snapshots FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE business_insights_kpi_snapshots TO nce_app;
    END IF;
END $$;
