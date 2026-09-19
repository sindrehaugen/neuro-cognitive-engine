-- 092_pricing_fx_rates.sql
--
-- FX rate feed for the shared pricing service (Geodata/Platform FEED
-- module, Lane F Wave F-15) -- last-known-good persistence for
-- nce/pricing/fx.py's Norges Bank EXR adapter.
--
-- GLOBAL, not tenant-scoped: an EUR/NOK exchange rate describes the world,
-- not a tenant's data -- the same reasoning as geodata_osm_elements
-- (migration 086) and product_catalog (migration 064). No namespace_id
-- column, RLS not applicable, registered in
-- nce.event_log.EXPECTED_GLOBAL_TABLES (same PR).
--
-- One row per currency code, upserted on every successful fetch. This is
-- deliberately NOT a history table: the adapter's own in-memory cache
-- covers "what's the rate right now", and this table exists only so a
-- process restart still has a last-known-good rate to fall back on before
-- the source answers again -- the same job the host's own app_settings
-- key/value row does for fx.py, done here as a dedicated table because
-- this codebase has no generic settings store to reuse.

BEGIN;

CREATE TABLE IF NOT EXISTS pricing_fx_rates (
    currency    TEXT        NOT NULL,
    -- NOK per one unit of `currency` -- Norges Bank's own OBS_VALUE shape.
    rate        NUMERIC     NOT NULL,
    -- The rate's own as-of date from the source (a business day, not a
    -- fetch timestamp) -- Norges Bank only republishes on weekdays.
    rate_date   DATE        NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (currency),
    CONSTRAINT pricing_fx_rates_rate_positive CHECK (rate > 0)
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE pricing_fx_rates FROM nce_app;
        GRANT SELECT, INSERT, UPDATE ON TABLE pricing_fx_rates TO nce_app;
    END IF;
END $$;

COMMIT;
