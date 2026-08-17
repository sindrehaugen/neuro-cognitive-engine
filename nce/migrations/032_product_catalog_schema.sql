-- 032_product_catalog_schema.sql
-- Product catalog schema: ETIM-coded product data + pricing (multi-source ingest target).
-- Streaming-upsert master for 552k products + 1.57M price rows; FORCE RLS isolation per tenant.
--
-- Design decisions:
--   - ETIM-native: specs stored as coded (etim_class, feature, value, unit) tuples in a JSONB column
--     with per-field provenance/confidence, not bare columns.
--   - GTIN is the universal key (nullable — often absent on AV SKUs).
--   - (manufacturer, mfr_part_no) is the canonical match key for dedup.
--   - product_source_id TEXT tracks per-source provenance/retirement (multi-source ingestion).
--   - Soft-delete column for idempotent, audit-safe removal.
--   - product_prices: (mfr_part_no, supplier, bid_id) natural key; list/cost/BID columns.
--   - FORCE RLS: every row scoped to one tenant (namespace_id).
--   - Idempotent DDL throughout (IF NOT EXISTS / DO $$ … $$).
-- ============================================================================

CREATE TABLE IF NOT EXISTS product_catalog (
    id                UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id      UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    gtin              TEXT,
    manufacturer      TEXT        NOT NULL,
    mfr_part_no       TEXT        NOT NULL,
    product_source_id TEXT        NOT NULL,
    lifecycle_status  TEXT        NOT NULL DEFAULT 'active',
    is_deleted        BOOLEAN     NOT NULL DEFAULT false,
    etim_specs        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (namespace_id, manufacturer, mfr_part_no)
);

CREATE TABLE IF NOT EXISTS product_prices (
    id            UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    mfr_part_no   TEXT        NOT NULL,
    supplier      TEXT        NOT NULL,
    bid_id        TEXT        NOT NULL,
    list_price    NUMERIC,
    cost_price    NUMERIC,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (namespace_id, mfr_part_no, supplier, bid_id)
);

-- Indexes for product_catalog: namespace+match-key, namespace+gtin for lookups.
CREATE INDEX IF NOT EXISTS idx_product_catalog_namespace_mfr_mfr_part_no
    ON product_catalog (namespace_id, manufacturer, mfr_part_no);

CREATE INDEX IF NOT EXISTS idx_product_catalog_namespace_gtin
    ON product_catalog (namespace_id, gtin)
    WHERE gtin IS NOT NULL;

-- Index for soft-delete: speed up queries that filter on is_deleted.
CREATE INDEX IF NOT EXISTS idx_product_catalog_namespace_is_deleted
    ON product_catalog (namespace_id, is_deleted);

-- Indexes for product_prices: namespace+supplier for price queries.
CREATE INDEX IF NOT EXISTS idx_product_prices_namespace_mfr_part_no
    ON product_prices (namespace_id, mfr_part_no);

CREATE INDEX IF NOT EXISTS idx_product_prices_namespace_supplier
    ON product_prices (namespace_id, supplier);

-- Row-Level Security: each tenant sees only its own catalog and price rows.
ALTER TABLE product_catalog ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_catalog FORCE ROW LEVEL SECURITY;

ALTER TABLE product_prices ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_prices FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON product_catalog;
CREATE POLICY tenant_isolation_policy ON product_catalog
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DROP POLICY IF EXISTS tenant_isolation_policy ON product_prices;
CREATE POLICY tenant_isolation_policy ON product_prices
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

-- Application role grants: INSERT, SELECT, UPDATE, DELETE for catalog ingestion/enrichment.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE product_catalog FROM nce_app;
        REVOKE ALL ON TABLE product_prices FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE product_catalog TO nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE product_prices TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE product_catalog IS
'ETIM-coded product catalog: 552k-row streaming-upsert master for multi-source ingestion.
Deduped on (namespace_id, manufacturer, mfr_part_no); GTIN is the universal key (nullable).
etim_specs JSONB holds coded (etim_class, feature, value, unit) tuples with per-field
provenance and confidence inside the JSONB. product_source_id tracks per-source provenance
for multi-source dedup. is_deleted enables soft-delete. FORCE RLS isolates per tenant.';

COMMENT ON TABLE product_prices IS
'Product pricing: 1.57M cost/list/BID rows (mfr_part_no, supplier, bid_id) natural key.
Streaming-upsert target for price syncs. FORCE RLS isolates per tenant.';
