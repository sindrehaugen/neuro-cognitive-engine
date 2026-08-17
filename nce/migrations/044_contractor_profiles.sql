-- 044_contractor_profiles.sql
-- Create tenant-isolated and partner-scoped contractor_profiles table.
-- Keyed on (contractor_id, namespace_id) with partner isolation RLS policy.
-- ============================================================================

CREATE TABLE IF NOT EXISTS contractor_profiles (
    contractor_id      TEXT        NOT NULL,
    namespace_id       UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    partner_scope_id   UUID        NOT NULL,
    profile            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    rates              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    skills             TEXT[]      NOT NULL DEFAULT '{}'::text[],
    availability       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    performance_score  NUMERIC,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (contractor_id, namespace_id)
);

CREATE INDEX IF NOT EXISTS idx_contractor_profiles_namespace ON contractor_profiles (namespace_id);
CREATE INDEX IF NOT EXISTS idx_contractor_profiles_partner_scope ON contractor_profiles (partner_scope_id);

ALTER TABLE contractor_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE contractor_profiles FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS external_isolation_policy ON contractor_profiles;
CREATE POLICY external_isolation_policy ON contractor_profiles
    FOR ALL TO nce_app
    USING (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND partner_scope_id IS NOT NULL
        AND partner_scope_id = get_nce_external_scope()
    )
    WITH CHECK (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND partner_scope_id IS NOT NULL
        AND partner_scope_id = get_nce_external_scope()
    );

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE contractor_profiles FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE contractor_profiles TO nce_app;
    END IF;
END $$;
