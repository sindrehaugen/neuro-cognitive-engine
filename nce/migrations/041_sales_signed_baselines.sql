-- 041_sales_signed_baselines.sql
-- Immutable append-only signed-baseline freeze table.
-- ============================================================================

CREATE TABLE IF NOT EXISTS sales_signed_baselines (
    id                 BIGSERIAL   PRIMARY KEY,
    namespace_id       UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    quote_id           TEXT        NOT NULL,
    signed_margin_pct  NUMERIC     NOT NULL, -- signed gross-margin percentage (0–1)
    signed_total_nok   NUMERIC     NOT NULL, -- total signed value in NOK
    signed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sales_signed_baselines_natural_key UNIQUE (namespace_id, quote_id)
);

CREATE INDEX IF NOT EXISTS idx_sales_signed_baselines_quote ON sales_signed_baselines (namespace_id, quote_id);

ALTER TABLE sales_signed_baselines ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_signed_baselines FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON sales_signed_baselines;
CREATE POLICY tenant_isolation_policy ON sales_signed_baselines
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE sales_signed_baselines FROM nce_app;
        GRANT SELECT, INSERT ON TABLE sales_signed_baselines TO nce_app;
        -- Pre-BIGSERIAL databases created this table with a UUID key, so the
        -- sequence may not exist; grant only when it does.
        IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'sales_signed_baselines_id_seq') THEN
            GRANT USAGE, SELECT ON SEQUENCE sales_signed_baselines_id_seq TO nce_app;
        END IF;
    END IF;
END $$;
