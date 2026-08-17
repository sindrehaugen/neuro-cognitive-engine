-- 030_c5_source_mode_config.sql
-- C5 per-namespace source-mode configuration table.
--
-- Stores, per (namespace, engine, function), the runtime mode (d365|both|nce).
-- The global settings table (PK=key) cannot scope per-namespace, so this is a
-- new own-table with FORCE RLS to isolate per tenant.
-- Idempotent DDL: CREATE TABLE IF NOT EXISTS, idempotent RLS via DO $$.
-- ============================================================================

CREATE TABLE IF NOT EXISTS source_mode_config (
    namespace_id UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    engine       TEXT        NOT NULL,
    function     TEXT        NOT NULL,
    mode         TEXT        NOT NULL CHECK (mode IN ('d365', 'both', 'nce')),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, engine, function)
);

-- Composite index for efficient lookups by namespace and engine.
CREATE INDEX IF NOT EXISTS idx_source_mode_config_namespace_engine
    ON source_mode_config(namespace_id, engine);

-- Row-Level Security: each tenant sees only its own source-mode config rows.
ALTER TABLE source_mode_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_mode_config FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON source_mode_config;
CREATE POLICY tenant_isolation_policy ON source_mode_config
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

-- Application role grants: read and write for configuration management.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE source_mode_config FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE source_mode_config TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE source_mode_config IS
'C5 per-namespace source-mode configuration: per (namespace, engine, function),
the runtime mode (d365|both|nce). FORCE RLS isolates per tenant. No resolver logic
here — that is Wave 27. Global settings (PK=key) cannot scope per-namespace,
so this dedicated table provides tenant-scoped source-mode storage.';
