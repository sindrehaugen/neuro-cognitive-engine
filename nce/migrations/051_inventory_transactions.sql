-- 051_inventory_transactions.sql
-- ============================================================================
-- Inventory engine (Module 11, Wave 11 -- transactions-valuation): the
-- append-only inventory_transactions ledger + typed reason categories that
-- back nce/vertical_modules/inventory/transactions.py's do_valuation
-- (FIFO/average, config nce/config_data/inventory-valuation.json).
--
-- Re-sequenced ahead of its numeric slot -- runs right after Batch 130,
-- before Batch 131 -- per this wave's own orchestrator amendment: Batch 134
-- needs a ledger-backed consumption rationale and inventory_items itself has
-- no history; Batch 131 exposes do_transfer_stock/do_record_consumption as
-- MCP tools and must not do so before every movement is recorded.
--
-- Honest scope limit: inventory_items (migration 050) has NO cost column, so
-- unit_cost rides on THIS ledger row and only becomes real at inbound.
-- Costed goods-receipt (Batch 132) has not landed -- do_transfer_stock /
-- do_record_consumption (nce/vertical_modules/inventory/stock.py) append
-- rows with unit_cost = NULL, because no cost source exists yet. This
-- migration does not invent one; do_valuation is proven against SEEDED
-- ledger rows only (see tests/test_inventory_transactions.py).
--
-- Idempotent DDL -- there is no migration ledger in this repo; schema.sql and
-- every migrations/*.sql file re-run on every boot under an advisory lock.
-- ============================================================================

CREATE TABLE IF NOT EXISTS inventory_transactions (
    id               UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id     UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    sku              TEXT          NOT NULL,
    location_id      UUID          NOT NULL,
    -- Same NUMERIC(18,3) scale as inventory_items.qty_on_hand (migration
    -- 050) -- signed: positive = stock entering this location, negative =
    -- stock leaving it. Never zero -- a zero-quantity "movement" recorded
    -- nothing and must not be appended.
    delta            NUMERIC(18,3) NOT NULL CHECK (delta <> 0),
    -- Typed, not free text (Rackbeat's movements-vs-adjustments split;
    -- docs/vertical_engines/11-inventory-engine.md A5). Extend this list via
    -- an idempotent ALTER ... DROP/ADD CONSTRAINT in the migration that lands
    -- the next writer (e.g. Batch 132 goods-receipt) -- never widen it by
    -- dropping the CHECK outright.
    reason_category  TEXT          NOT NULL
                                    CHECK (reason_category IN
                                        ('transfer_in', 'transfer_out', 'consumption', 'adjustment')),
    -- Money, NOT quantity -- mirrors economy_postings.amount / economy_bom_
    -- actual_costs.actual_cost's NUMERIC(18,2) precision (migrations 047/048).
    -- NULL is the honest default for the transfer/consumption rows this wave's
    -- writers append (see header) -- only a caller that actually knows a cost
    -- (a future goods-receipt writer, or a seeded test row simulating one)
    -- supplies one.
    unit_cost        NUMERIC(18,2),
    -- Free-form caller reference (e.g. do_record_consumption's work_order, or
    -- a transfer's counterpart location id) -- optional, never interpreted by
    -- this table.
    ref              TEXT,
    change_origin    TEXT          NOT NULL DEFAULT 'agent'
                                    CHECK (change_origin IN
                                        ('sync','webhook','agent','operator','consolidation','replay','unknown')),
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    -- Composite FK target is stock_locations_id_ns_uq (migration 050) -- a
    -- ledger row can never reference another tenant's location. No ON DELETE
    -- CASCADE: unlike inventory_items (a live mutable balance), this is a WORM
    -- audit trail -- a location with recorded history must not be able to
    -- take its ledger down with it.
    CONSTRAINT inventory_transactions_location_fk
        FOREIGN KEY (location_id, namespace_id)
        REFERENCES stock_locations (id, namespace_id),
    -- Sign must agree with the category (storage-level backstop, mirrors
    -- economy_postings' non-empty-account CHECK reasoning -- migration 048):
    -- a 'transfer_out' row with a positive delta would be silently wrong and
    -- an application-level bug must not be able to write it. 'adjustment' is
    -- deliberately unconstrained in sign -- a manual correction can go
    -- either way.
    CONSTRAINT inventory_transactions_sign_matches_category CHECK (
        (reason_category = 'transfer_in' AND delta > 0)
        OR (reason_category IN ('transfer_out', 'consumption') AND delta < 0)
        OR (reason_category = 'adjustment')
    )
);

-- FIFO/average valuation's read pattern: every row for one
-- (namespace, sku, location), oldest first.
CREATE INDEX IF NOT EXISTS idx_inventory_transactions_namespace_sku_location
    ON inventory_transactions (namespace_id, sku, location_id, created_at);

ALTER TABLE inventory_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_transactions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON inventory_transactions;
CREATE POLICY tenant_isolation_policy ON inventory_transactions
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE inventory_transactions FROM nce_app;
        -- Append-only ledger (WORM), mirrors event_log / economy_postings /
        -- audit_log's precedent: withhold UPDATE/DELETE from nce_app at the
        -- grant level so no application code path -- buggy or future -- can
        -- rewrite history. A correction is a NEW row, never an edit.
        GRANT SELECT, INSERT ON TABLE inventory_transactions TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE inventory_transactions IS
'Append-only movement ledger (Module 11, Wave 11 -- transactions-valuation).
One row per qty change at one (sku, location): do_transfer_stock
(nce/vertical_modules/inventory/stock.py) writes a transfer_out row at
from_location and a transfer_in row at to_location in the SAME transaction as
the inventory_items row write it reflects; do_record_consumption writes one
consumption row. unit_cost rides on this row (inventory_items itself has no
cost column) and enters at inbound -- NULL until a real cost source exists
(Batch 132 goods-receipt); do_valuation
(nce/vertical_modules/inventory/transactions.py) computes FIFO/average value
from these rows per nce/config_data/inventory-valuation.json, and is the
number Inventory hands to Economy to post -- this table and its reader never
post to the GL themselves. FORCE RLS isolates per tenant; nce_app is granted
only SELECT, INSERT -- corrections are new rows, never an UPDATE/DELETE.';
