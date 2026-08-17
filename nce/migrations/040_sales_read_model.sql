-- 040_sales_read_model.sql
-- Native tenant-isolated sales read-model (replaces steps_d365.records).
-- Scoped to namespace_id with GUC RLS policy.
-- ============================================================================

CREATE TABLE IF NOT EXISTS sales_read_model (
    id             BIGSERIAL   PRIMARY KEY,
    namespace_id   UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    entity         TEXT        NOT NULL,                    -- 'accounts'|'opportunities'|'incidents'|'agreements'|'systemusers'|'appointments'
    source_id      TEXT        NOT NULL,                    -- Dataverse GUID
    name           TEXT,                                    -- Visningsnavn
    modifiedon     TIMESTAMPTZ,                             -- Kildens modified-ts
    source_json    JSONB       NOT NULL DEFAULT '{}'::jsonb, -- Rå D365-post
    manual         JSONB       NOT NULL DEFAULT '{}'::jsonb, -- Manuell berikelse
    source         TEXT        NOT NULL DEFAULT 'direct',   -- Kilde
    is_deleted     BOOLEAN     NOT NULL DEFAULT false,      -- Soft-delete
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sales_read_model_natural_key UNIQUE (namespace_id, entity, source_id)
);

CREATE INDEX IF NOT EXISTS idx_sales_read_model_entity ON sales_read_model (namespace_id, entity);
CREATE INDEX IF NOT EXISTS idx_sales_read_model_modified ON sales_read_model (namespace_id, entity, modifiedon DESC);
CREATE INDEX IF NOT EXISTS idx_sales_read_model_name ON sales_read_model (namespace_id, entity, lower(name));
CREATE INDEX IF NOT EXISTS idx_sales_read_model_deleted ON sales_read_model (namespace_id, entity, is_deleted);
CREATE INDEX IF NOT EXISTS idx_sales_read_model_opp_customer ON sales_read_model (namespace_id, (source_json->>'_customerid_value')) WHERE entity='opportunities';
CREATE INDEX IF NOT EXISTS idx_sales_read_model_contact_parent ON sales_read_model (namespace_id, (source_json->>'_parentcustomerid_value')) WHERE entity='contacts';
CREATE INDEX IF NOT EXISTS idx_sales_read_model_asset_account ON sales_read_model (namespace_id, (source_json->>'_msdyn_account_value')) WHERE entity='customerassets';
CREATE INDEX IF NOT EXISTS idx_sales_read_model_owner ON sales_read_model (namespace_id, entity, (source_json->>'_ownerid_value')) WHERE is_deleted=false;

ALTER TABLE sales_read_model ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_read_model FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON sales_read_model;
CREATE POLICY tenant_isolation_policy ON sales_read_model
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE sales_read_model FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE sales_read_model TO nce_app;
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS sales_targets (
    namespace_id UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    owner_slug   TEXT        NOT NULL,
    metric       TEXT        NOT NULL,
    value        NUMERIC,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, owner_slug, metric)
);

ALTER TABLE sales_targets ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_targets FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON sales_targets;
CREATE POLICY tenant_isolation_policy ON sales_targets
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE sales_targets FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE sales_targets TO nce_app;
    END IF;
END $$;
