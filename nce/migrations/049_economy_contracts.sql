-- 049_economy_contracts.sql
-- ============================================================================
-- Economy engine (Module 8, Wave 10 -- contracts-renewal): the real
-- economy_contracts table backing recurring-revenue contracts, replacing
-- Wave 9's temporary `namespaces.metadata->'economy'->'recurring_contracts'`
-- shim. nce/vertical_modules/economy/recurring.py's module docstring and
-- nce/cron.py's `_economy_recurring_recognition_tick` were both updated by
-- this wave to read contracts from this table instead (see
-- nce/vertical_modules/economy/contracts.py's `fetch_contracts_for_recognition`).
--
-- Deviations from docs/vertical_engines/08-economy-engine.md's literal column
-- list (`contract_id, mrr, cpi_cap, next_invoice, finago_ref, raw jsonb`),
-- recorded here so a future reader does not "fix" this file back to the docs:
--   * No `mrr` column. `annual_amount` is the single source of truth -- the
--     same field do_recognize_recurring / do_compute_recognition_schedule
--     (Wave 9) already take as an explicit parameter. Storing MRR separately
--     would let the two drift; `mrr = annual_amount / 12`, quantised, is
--     already computed on read by do_snapshot_mrr_arr_churn. One money
--     field, not two.
--   * No `finago_ref` column. `finagoRef = ms:{contractId}:{YYYY-MM}` is
--     PERIOD-specific (Wave 9), not a fixed per-contract value -- a single
--     stored finago_ref here would be misleading. `contract_id` (the
--     natural-key half already used to build every period's finagoRef) is
--     the stable identifier this table owns.
--   * `next_invoice` -> `next_renewal_date` (a DATE, not a period string) --
--     what the 90-day renewal scan (do_scan_renewals) compares against
--     "today".
--
-- Idempotent DDL -- there is no migration ledger in this repo; schema.sql and
-- every migrations/*.sql file re-run on every boot under an advisory lock.
-- ============================================================================

CREATE TABLE IF NOT EXISTS economy_contracts (
    id                 UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id       UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    contract_id        TEXT          NOT NULL,
    status             TEXT          NOT NULL
                                      CHECK (status IN ('active', 'churned')),
    -- NOK, 2-decimal (oere) precision -- mirrors economy_bom_actual_costs.actual_cost
    -- (migration 047) / economy_postings.amount (migration 048). The
    -- contract's annual value; do_recognize_recurring (recurring.py, Wave 9)
    -- ratably recognises 1/12 of this per period.
    annual_amount      NUMERIC(18,2) NOT NULL CHECK (annual_amount > 0),
    -- 'YYYY-MM' -- first recognised month, passed straight through to
    -- do_compute_recognition_schedule (recurring.py). Format validated at the
    -- Python boundary (contracts.py's _parse_period), not here -- mirrors
    -- economy_postings.period_id's own bare-TEXT precedent.
    start_period       TEXT          NOT NULL,
    -- CPI uplift ceiling for this contract's renewal quote, as a fraction
    -- (0.05 = 5%). The CHECK is the wave's "CPI cap is a money ceiling"
    -- requirement enforced STRUCTURALLY: no row -- not even one written by a
    -- future bug or a raw-SQL admin fix -- can ever carry a cap above 5%,
    -- independent of whatever the application layer (do_validate_contract)
    -- separately enforces per proposed uplift.
    cpi_cap            NUMERIC(5,4)  NOT NULL DEFAULT 0.05
                                      CHECK (cpi_cap >= 0 AND cpi_cap <= 0.05),
    -- Date the renewal-engine's 90-day scan (do_scan_renewals) compares
    -- against "today". Required: a contract with no renewal date can never
    -- be meaningfully scanned, so this table refuses to represent that
    -- ambiguous state rather than silently excluding the row from every scan.
    next_renewal_date  DATE          NOT NULL,
    raw                JSONB         NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- Natural key: ONE ROW PER (namespace, contract). Unlike economy_postings /
-- economy_bom_actual_costs (append-only ledger lines, ON CONFLICT DO
-- NOTHING), a contract is a live, mutable record -- status changes to
-- 'churned', a renewal moves next_renewal_date forward, an amendment changes
-- annual_amount -- so this table's sole writer (contracts.py's
-- do_upsert_contract) uses ON CONFLICT DO UPDATE against this key.
ALTER TABLE economy_contracts DROP CONSTRAINT IF EXISTS economy_contracts_natural_key;
ALTER TABLE economy_contracts
    ADD CONSTRAINT economy_contracts_natural_key
    UNIQUE (namespace_id, contract_id);

-- Supports the recognition tick's per-namespace read (WHERE namespace_id=...)
-- and both engines' status filtering (WHERE status = 'active').
CREATE INDEX IF NOT EXISTS idx_economy_contracts_namespace_status
    ON economy_contracts (namespace_id, status);

-- Supports the renewal scan's per-namespace active-contracts read, ordered
-- for the "which renews soonest" query shape.
CREATE INDEX IF NOT EXISTS idx_economy_contracts_renewal_date
    ON economy_contracts (namespace_id, next_renewal_date)
    WHERE status = 'active';

ALTER TABLE economy_contracts ENABLE ROW LEVEL SECURITY;
ALTER TABLE economy_contracts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON economy_contracts;
CREATE POLICY tenant_isolation_policy ON economy_contracts
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE economy_contracts FROM nce_app;
        -- Live mutable record (not a WORM ledger) -- nce_app gets the full
        -- CRUD set, mirroring economy_bom_actual_costs (migration 047), not
        -- economy_postings' append-only grant (migration 048).
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE economy_contracts TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE economy_contracts IS
'Economy-owned recurring-revenue contract store (Module 8, Wave 10). One row
per (namespace, contract) -- a LIVE mutable record (status/next_renewal_date/
annual_amount change over the contract''s life), unlike the append-only
economy_postings / economy_bom_actual_costs ledgers. Natural-keyed
(namespace_id, contract_id); contracts.py''s do_upsert_contract is the SOLE
writer, using ON CONFLICT DO UPDATE. annual_amount is the single source of
truth for the contract''s value (do_recognize_recurring ratably recognises
1/12 of it per period, Wave 9) -- no separate mrr column, to avoid two money
fields drifting apart. cpi_cap is bounded 0-0.05 by CHECK -- a structural
ceiling no row can exceed, backing do_validate_contract''s per-proposal
enforcement. next_renewal_date drives do_scan_renewals''s 90-day scan.
Retires the Wave-9 namespaces.metadata->economy->recurring_contracts shim
(see recurring.py + cron.py). FORCE RLS isolates per tenant (mirrors
economy_postings / economy_bom_actual_costs).';
