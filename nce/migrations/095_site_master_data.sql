-- 095_site_master_data.sql
--
-- C17 Site Master Data (Wave A-9)
-- Multi-tenant site and building master data register (cadastre_id, address, coordinates, footprint geometry, height, telemetry).

BEGIN;

CREATE TABLE IF NOT EXISTS sites (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    name VARCHAR(256) NOT NULL,
    cadastre_id VARCHAR(64),
    site_type VARCHAR(64) NOT NULL DEFAULT 'building',
    address JSONB NOT NULL DEFAULT '{}'::jsonb,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    altitude DOUBLE PRECISION,
    height DOUBLE PRECISION,
    footprint_geometry JSONB NOT NULL DEFAULT '{}'::jsonb,
    telemetry_stream JSONB,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    archived BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_sites_namespace_cadastre_id UNIQUE (namespace_id, cadastre_id)
);

CREATE INDEX IF NOT EXISTS idx_sites_lookup
    ON sites (namespace_id, archived, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sites_cadastre_id
    ON sites (namespace_id, cadastre_id);

CREATE INDEX IF NOT EXISTS idx_sites_name
    ON sites (namespace_id, name);

CREATE INDEX IF NOT EXISTS idx_sites_type
    ON sites (namespace_id, site_type);

ALTER TABLE sites ENABLE ROW LEVEL SECURITY;
ALTER TABLE sites FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'sites' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON sites
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

COMMIT;
