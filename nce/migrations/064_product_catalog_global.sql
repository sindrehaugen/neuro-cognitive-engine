-- 064_product_catalog_global.sql
-- ============================================================================
-- product_catalog becomes a GLOBAL shared parts library.
--
-- Sindre ruled 2026-09-04: "products catalogue should be global." A
-- manufacturer part number names the same physical part for every tenant, so
-- the catalogue is reference data, not tenant data. namespace_id on this table
-- was already vestigial: the only production writer (enrich.py) updates by
-- primary key, no query filtered or joined on it, and no foreign key pointed
-- at the table.
--
-- product_prices is NOT touched: supplier-bid pricing is per-tenant commercial
-- confidential and stays tenant-scoped with its RLS policy intact.
--
-- Every statement here is idempotent, and deliberately so: nce/schema.sql
-- mirrors this same end state and NCEEngine._init_pg_schema re-executes that
-- whole file on every connect(), so a statement that failed on a database
-- already in the target state would take startup down.
--
-- The runner wraps this file in a transaction (see nce/migration_ledger.py);
-- no explicit BEGIN/COMMIT here, matching migrations 058-063.
-- ============================================================================

-- 1. The tenant policy reads namespace_id, so it must go before the column.
DROP POLICY IF EXISTS tenant_isolation_policy ON product_catalog;
DROP POLICY IF EXISTS namespace_isolation_policy ON product_catalog;

-- 2. RLS off. Both statements are no-ops when already off.
ALTER TABLE product_catalog NO FORCE ROW LEVEL SECURITY;
ALTER TABLE product_catalog DISABLE ROW LEVEL SECURITY;

-- 3. Drop the tenant-scoped unique identity.
ALTER TABLE product_catalog
    DROP CONSTRAINT IF EXISTS product_catalog_namespace_id_manufacturer_mfr_part_no_key;

-- 4. Drop the three namespace_id-leading indexes.
DROP INDEX IF EXISTS idx_product_catalog_namespace_mfr_mfr_part_no;
DROP INDEX IF EXISTS idx_product_catalog_namespace_gtin;
DROP INDEX IF EXISTS idx_product_catalog_namespace_is_deleted;

-- 5. Drop the column. This drops the namespaces(id) ON DELETE CASCADE FK with it.
ALTER TABLE product_catalog DROP COLUMN IF EXISTS namespace_id;

-- 6. The global identity: one row per real part.
CREATE UNIQUE INDEX IF NOT EXISTS product_catalog_manufacturer_mfr_part_no_key
    ON product_catalog (manufacturer, mfr_part_no);

-- 7. Recreate the surviving lookups without namespace_id.
CREATE INDEX IF NOT EXISTS idx_product_catalog_gtin
    ON product_catalog (gtin)
    WHERE gtin IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_product_catalog_is_deleted
    ON product_catalog (is_deleted);

-- 8. Leaving the schema.sql tenant_tables loop also left its only GRANT site,
--    so the application role's privileges are granted explicitly here.
DO $BODY$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE product_catalog TO nce_app;
    END IF;
END $BODY$;
