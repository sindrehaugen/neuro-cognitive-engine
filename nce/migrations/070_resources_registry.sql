-- 070_resources_registry.sql
-- ============================================================================
-- Staff & Resources Engine (Module 15, Phase 1 -- resources-registry):
-- Tables backing nce/vertical_modules/resources/**:
--   1. resources (registry: id, kind IN ('employee', 'contractor', 'vehicle', 'tool'),
--                ref_id, display_name, attrs jsonb)
--
-- STRICT ROW LEVEL SECURITY + EXPLICIT NAMESPACE PREDICATE ENFORCEMENT
-- --------------------------------------------------------------------------
-- Enables and forces RLS for nce_app.
-- As documented in Charter section 6.4, the live environment connects as mcp_user
-- (rolsuper=true, rolbypassrls=true). Therefore, RLS policies are defense-in-depth;
-- EVERY application query must carry explicit WHERE namespace_id = $1 predicates.
-- ============================================================================

CREATE TABLE IF NOT EXISTS resources (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    kind                   TEXT        NOT NULL,
    ref_id                 TEXT,
    display_name           TEXT        NOT NULL,
    attrs                  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT resources_kind_check
        CHECK (kind IN ('employee', 'contractor', 'vehicle', 'tool')),
    CONSTRAINT resources_display_name_not_blank
        CHECK (btrim(display_name) <> '')
);

CREATE INDEX IF NOT EXISTS idx_resources_ns_kind
    ON resources (namespace_id, kind);
CREATE INDEX IF NOT EXISTS idx_resources_ns_ref
    ON resources (namespace_id, ref_id) WHERE ref_id IS NOT NULL;

ALTER TABLE resources ENABLE ROW LEVEL SECURITY;
ALTER TABLE resources FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON resources;
CREATE POLICY tenant_isolation_policy ON resources
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE resources FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE resources TO nce_app;
    END IF;
END $$;
