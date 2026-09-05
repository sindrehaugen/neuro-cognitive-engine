-- 069_marketing_engine.sql
-- ============================================================================
-- Marketing Engine (Module 14, Wave 1 -- marketing-schema):
-- Tables backing nce/vertical_modules/marketing/**:
--   1. case_studies (drafted, approved, and published customer success stories)
--   2. testimonials (quotes with high-NPS capture, structured consent tiers & scopes)
--   3. content_assets (marketing assets, AEO/GEO metadata, JSON-LD schemas, MinIO storage)
--
-- STRICT ROW LEVEL SECURITY + EXPLICIT NAMESPACE PREDICATE ENFORCEMENT
-- --------------------------------------------------------------------------
-- All three tables enable and force RLS for nce_app.
-- As documented in Charter section 5.4, the live environment connects as mcp_user
-- (rolsuper=true, rolbypassrls=true). Therefore, RLS policies are defense-in-depth;
-- EVERY application query must carry explicit WHERE namespace_id = $1 predicates.
-- ============================================================================

CREATE TABLE IF NOT EXISTS case_studies (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    project_id             TEXT        NOT NULL,
    title                  TEXT        NOT NULL,
    body                   TEXT        NOT NULL DEFAULT '',
    status                 TEXT        NOT NULL DEFAULT 'draft',
    anonymized             BOOLEAN     NOT NULL DEFAULT TRUE,
    approver               TEXT,
    approved_at            TIMESTAMPTZ,
    marketing_source_id    TEXT,
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT case_studies_title_not_blank
        CHECK (btrim(title) <> ''),
    CONSTRAINT case_studies_status_check
        CHECK (status IN ('draft', 'in_review', 'approved', 'published', 'retracted'))
);

CREATE INDEX IF NOT EXISTS idx_case_studies_ns_status
    ON case_studies (namespace_id, status);
CREATE INDEX IF NOT EXISTS idx_case_studies_ns_project
    ON case_studies (namespace_id, project_id);
CREATE INDEX IF NOT EXISTS idx_case_studies_source_id
    ON case_studies (marketing_source_id) WHERE marketing_source_id IS NOT NULL;

ALTER TABLE case_studies ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_studies FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON case_studies;
CREATE POLICY tenant_isolation_policy ON case_studies
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE case_studies FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE case_studies TO nce_app;
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS testimonials (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    customer_id            TEXT        NOT NULL,
    project_id             TEXT,
    quote                  TEXT        NOT NULL DEFAULT '',
    status                 TEXT        NOT NULL DEFAULT 'requested',
    consent                BOOLEAN     NOT NULL DEFAULT FALSE,
    consent_tier           TEXT        NOT NULL DEFAULT 'web_retractable',
    consent_scope          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    consent_recorded_at    TIMESTAMPTZ,
    nps_at_capture         NUMERIC(4, 2),
    marketing_source_id    TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT testimonials_status_check
        CHECK (status IN ('requested', 'received', 'approved', 'declined', 'retracted')),
    CONSTRAINT testimonials_consent_tier_check
        CHECK (consent_tier IN ('none', 'web_retractable', 'ai_citable_irrevocable'))
);

CREATE INDEX IF NOT EXISTS idx_testimonials_ns_status
    ON testimonials (namespace_id, status);
CREATE INDEX IF NOT EXISTS idx_testimonials_ns_customer
    ON testimonials (namespace_id, customer_id);
CREATE INDEX IF NOT EXISTS idx_testimonials_source_id
    ON testimonials (marketing_source_id) WHERE marketing_source_id IS NOT NULL;

ALTER TABLE testimonials ENABLE ROW LEVEL SECURITY;
ALTER TABLE testimonials FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON testimonials;
CREATE POLICY tenant_isolation_policy ON testimonials
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE testimonials FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE testimonials TO nce_app;
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS content_assets (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    kind                   TEXT        NOT NULL DEFAULT 'case_study',
    ref_id                 TEXT,
    title                  TEXT        NOT NULL DEFAULT '',
    seo                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    storage_uri            TEXT,
    status                 TEXT        NOT NULL DEFAULT 'draft',
    marketing_source_id    TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT content_assets_kind_check
        CHECK (kind IN ('case_study', 'testimonial', 'blog', 'brand', 'drip')),
    CONSTRAINT content_assets_status_check
        CHECK (status IN ('draft', 'approved', 'published', 'archived'))
);

CREATE INDEX IF NOT EXISTS idx_content_assets_ns_kind
    ON content_assets (namespace_id, kind);
CREATE INDEX IF NOT EXISTS idx_content_assets_ns_status
    ON content_assets (namespace_id, status);
CREATE INDEX IF NOT EXISTS idx_content_assets_source_id
    ON content_assets (marketing_source_id) WHERE marketing_source_id IS NOT NULL;

ALTER TABLE content_assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE content_assets FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON content_assets;
CREATE POLICY tenant_isolation_policy ON content_assets
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE content_assets FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE content_assets TO nce_app;
    END IF;
END $$;
