-- 036_procurement_bid_prices.sql
-- Procurement consumer cache for Product's price projections (BID / supplier-price).
-- Fed by Product's projection push (A2A/REST); this module does NOT re-ingest
-- the Nettailer feed (§9.1 — Product owns the single feed ingest + SKU identity).
-- Upserts arrive via upsert_bid_projection(); ON CONFLICT DO UPDATE keeps the
-- cache current.  FORCE RLS isolates per tenant.
--
-- Design decisions:
--   - Consumer cache: sourced from Product's projection, not a primary ingest.
--   - ON CONFLICT DO UPDATE (artnr, leverandor, bid_id, namespace_id): natural key.
--   - valid_to TIMESTAMPTZ: projection TTL forwarded from Product; NULL = open-ended.
--   - raw JSONB: full projection row preserved for auditability.
--   - synced_at TIMESTAMPTZ: wall-clock of last cache write.
--   - FORCE RLS: every row scoped to one tenant (namespace_id).
--   - Idempotent DDL throughout (IF NOT EXISTS / DO $$ … $$).
-- ============================================================================

CREATE TABLE IF NOT EXISTS procurement_bid_prices (
    id           UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    artnr        TEXT        NOT NULL,
    leverandor   TEXT        NOT NULL,
    bid_id       TEXT        NOT NULL,
    prodid       TEXT,
    pris         NUMERIC,
    valid_to     TIMESTAMPTZ,
    raw          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (namespace_id, artnr, leverandor, bid_id)
);

-- Index: per-namespace artnr lookups (primary access pattern for do_resolve_bids).
CREATE INDEX IF NOT EXISTS idx_procurement_bid_prices_namespace_artnr
    ON procurement_bid_prices (namespace_id, artnr);

-- Index: per-namespace leverandor queries.
CREATE INDEX IF NOT EXISTS idx_procurement_bid_prices_namespace_leverandor
    ON procurement_bid_prices (namespace_id, leverandor);

-- Row-Level Security: each tenant sees only its own bid cache rows.
ALTER TABLE procurement_bid_prices ENABLE ROW LEVEL SECURITY;
ALTER TABLE procurement_bid_prices FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON procurement_bid_prices;
CREATE POLICY tenant_isolation_policy ON procurement_bid_prices
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

-- Application role grants: SELECT, INSERT, UPDATE, DELETE for cache upsert and resolution.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE procurement_bid_prices FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE procurement_bid_prices TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE procurement_bid_prices IS
'Procurement consumer cache for Product''s BID/supplier-price projections.
Fed via upsert_bid_projection() from Product''s A2A projection push — not a
primary Nettailer ingest (§9.1: Product owns the single feed).  Natural key is
(namespace_id, artnr, leverandor, bid_id); ON CONFLICT DO UPDATE keeps cache
current.  do_resolve_bids() reads this cache for best-BID-per-artnr resolution.
FORCE RLS isolates per tenant.';
