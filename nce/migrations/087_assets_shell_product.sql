-- 087_assets_shell_product.sql
-- ============================================================================
-- Assets Engine (Module 9, Wave D-1):
-- Extends the assets table with:
--   1. is_shell (BOOLEAN NOT NULL DEFAULT FALSE) for ADR 0037 placeholder coverage exclusion.
--   2. product_id (UUID REFERENCES product_catalog(id) ON DELETE SET NULL) for C1 catalog links.
--   3. product_sku (TEXT) for SKU denormalisation / reference.
-- ============================================================================

ALTER TABLE assets
    ADD COLUMN IF NOT EXISTS is_shell BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE assets
    ADD COLUMN IF NOT EXISTS product_id UUID REFERENCES product_catalog(id) ON DELETE SET NULL;

ALTER TABLE assets
    ADD COLUMN IF NOT EXISTS product_sku TEXT;

CREATE INDEX IF NOT EXISTS idx_assets_namespace_is_shell
    ON assets (namespace_id, is_shell);

CREATE INDEX IF NOT EXISTS idx_assets_namespace_product_id
    ON assets (namespace_id, product_id);
