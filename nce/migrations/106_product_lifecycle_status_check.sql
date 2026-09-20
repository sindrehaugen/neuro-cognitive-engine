-- 106_product_lifecycle_status_check.sql
-- ============================================================================
-- product_catalog.lifecycle_status becomes a closed, checked vocabulary:
-- 'coming' | 'active' | 'EOL' | 'EOS'.
--
-- Sindre, Q-49: "put it next to eol so that we have both" -- a real column,
-- alongside the existing NCE_PRODUCT_EOL_LIST config-seeded signal, not one
-- replacing the other. Unlike assets.lifecycle_state (in-service asset
-- lifecycle, deliberately unconstrained -- see migration history there), this
-- vocabulary is closed and known, so it gets a real CHECK constraint rather
-- than staying free text.
--
-- The column already exists (schema.sql:1366, migration 031) as
-- TEXT NOT NULL DEFAULT 'active' -- this migration adds the constraint only,
-- no new column. It has been writable via the C12 PRODUCT_SKU_SPEC
-- (nce/vertical_modules/product/resources.py) since Lane E's wave; that
-- generic REST/MCP write path is unchanged by this migration and simply
-- starts being enforced by the database.
--
-- Backfill first, per the standing rule (E's DESIGN_REQUEST precedent):
-- establish the actual data rather than assume "still in dev, no rows to
-- worry about." Normalises every value the estate's own (partially dead)
-- vocabularies already treat as a synonym, rather than silently discarding
-- anything unrecognised:
--   * nce/vertical_modules/product/watchers.py::_scan_catalog_for_eol
--     checks {'eol','eos','end_of_life','end_of_sale','discontinued'}
--     (lower-cased) -- that whole path is unreachable today (it also
--     requires a successor_sku column that was never created), but its
--     vocabulary is still real intent, not noise.
--   * nce/vertical_modules/product/related.py::_EOL_STATUSES checks
--     {'eol','discontinued','obsolete'} (lower-cased).
-- Anything not covered by an explicit synonym falls back to 'active' --
-- the column's own existing default -- rather than guessing a lifecycle
-- state for a value nobody named.
--
-- Every statement here is idempotent, and deliberately so: nce/schema.sql
-- mirrors this same end state and NCEEngine._init_pg_schema re-executes that
-- whole file on every connect(), so a statement that failed on a database
-- already in the target state would take startup down.
--
-- The runner wraps this file in a transaction (see nce/migration_ledger.py);
-- no explicit BEGIN/COMMIT here, matching migrations 058-064.
-- ============================================================================

-- 1. Backfill: normalise known synonyms, default anything else to 'active'.
UPDATE product_catalog
SET    lifecycle_status = CASE LOWER(lifecycle_status)
           WHEN 'coming' THEN 'coming'
           WHEN 'active' THEN 'active'
           WHEN 'eol' THEN 'EOL'
           WHEN 'end_of_life' THEN 'EOL'
           WHEN 'discontinued' THEN 'EOL'
           WHEN 'obsolete' THEN 'EOL'
           WHEN 'eos' THEN 'EOS'
           WHEN 'end_of_sale' THEN 'EOS'
           ELSE 'active'
       END
WHERE  lifecycle_status IS DISTINCT FROM (
           CASE LOWER(lifecycle_status)
               WHEN 'coming' THEN 'coming'
               WHEN 'active' THEN 'active'
               WHEN 'eol' THEN 'EOL'
               WHEN 'end_of_life' THEN 'EOL'
               WHEN 'discontinued' THEN 'EOL'
               WHEN 'obsolete' THEN 'EOL'
               WHEN 'eos' THEN 'EOS'
               WHEN 'end_of_sale' THEN 'EOS'
               ELSE 'active'
           END
       );

-- 2. The CHECK constraint. Drop-then-add is the idempotent idiom: Postgres
--    has no ADD CONSTRAINT IF NOT EXISTS.
ALTER TABLE product_catalog
    DROP CONSTRAINT IF EXISTS product_catalog_lifecycle_status_check;
ALTER TABLE product_catalog
    ADD CONSTRAINT product_catalog_lifecycle_status_check
    CHECK (lifecycle_status IN ('coming', 'active', 'EOL', 'EOS'));
