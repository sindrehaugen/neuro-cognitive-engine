-- 053_inventory_rma.sql
-- ============================================================================
-- Inventory engine (Module 11, Wave 10 -- rma-table): the inventory_rma table
-- backing nce/vertical_modules/inventory/rma.py's do_record_rma. Records a
-- customer return with its WEEE (Waste Electrical and Electronic Equipment)
-- electronics-disposal compliance STATE.
--
-- This migration records; it does not move. It writes no
-- inventory_transactions row itself and this table's own stock_movement_state
-- column is written as 'pending' by do_record_rma and NEVER anything else in
-- this wave -- Batch 138b owns both stock-leg transitions (restock-on-return
-- and permanent WEEE disposal) and needs no DDL of its own because
-- location_id / qty / stock_movement_state are provisioned here for it.
-- Batch 138c (dead-stock-reconcile) is the reconciliation routine that reads
-- this table's settled rows against the ledger.
--
-- INVENTORY_RMA + a first-class WEEE state is a genuine addition over
-- off-the-shelf WMS -- research doc finding 130: Rackbeat has no dedicated RMA
-- object at all.
--
-- Idempotent DDL -- there is no migration ledger in this repo; schema.sql and
-- every migrations/*.sql file re-run on every boot under an advisory lock.
-- ============================================================================

CREATE TABLE IF NOT EXISTS inventory_rma (
    id                    UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id          UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    -- Caller-supplied natural key -- the idempotency handle for do_record_rma
    -- (re-recording the same rma_ref returns the existing row, unchanged) and
    -- Batch 138b's stable handle for the two stock legs it performs later.
    rma_ref               TEXT          NOT NULL,
    sku                   TEXT          NOT NULL,
    -- Serialised units; a non-serialised return has none.
    serial                TEXT,
    -- Where the returned stock physically is. Batch 138b restocks TO this
    -- location on the restock leg and disposes FROM it on the WEEE-disposal
    -- leg. Provisioned here so Batch 138b needs no DDL of its own.
    location_id           UUID          NOT NULL,
    -- Same NUMERIC(18,3) scale as inventory_items.qty_on_hand /
    -- inventory_transactions.delta (migrations 050/051). Provisioned here for
    -- the same reason as location_id above.
    qty                   NUMERIC(18,3) NOT NULL CHECK (qty > 0),
    -- Free-form return reason; never interpreted by this table.
    reason                TEXT          NOT NULL,
    -- The WEEE compliance lifecycle. do_record_rma only ever writes
    -- 'not_applicable' or a caller-supplied value from this set at INSERT
    -- time -- there is no UPDATE path in this module (see rma.py's module
    -- docstring); a future wave owns the state's own transitions.
    weee_state            TEXT          NOT NULL DEFAULT 'not_applicable'
                                         CHECK (weee_state IN
                                             ('not_applicable', 'pending', 'awaiting_collection', 'disposed')),
    -- The approved take-back scheme's documentation reference. Required the
    -- moment weee_state = 'disposed' -- see the CHECK constraint below.
    disposal_ref          TEXT,
    -- The stock-leg lifecycle, independent of weee_state. This wave writes
    -- ONLY 'pending' here -- Batch 138b performs both transitions
    -- (restocked / disposed).
    stock_movement_state  TEXT          NOT NULL DEFAULT 'pending'
                                         CHECK (stock_movement_state IN ('pending', 'restocked', 'disposed')),
    change_origin         TEXT          NOT NULL DEFAULT 'agent'
                                         CHECK (change_origin IN
                                             ('sync','webhook','agent','operator','consolidation','replay','unknown')),
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    -- The natural key -- precedent: economy_contracts' (ns, contract_id),
    -- migration 049.
    CONSTRAINT inventory_rma_ns_ref_uq UNIQUE (namespace_id, rma_ref),
    -- Composite, so an RMA can never point at another tenant's location.
    -- Target index is stock_locations_id_ns_uq (migration 050). No
    -- ON DELETE CASCADE: an RMA is compliance evidence and must not be taken
    -- down by a location deletion (same reasoning as
    -- inventory_transactions_location_fk).
    CONSTRAINT inventory_rma_location_fk
        FOREIGN KEY (location_id, namespace_id)
        REFERENCES stock_locations (id, namespace_id),
    -- The compliance claim of this wave: a WEEE item cannot be recorded as
    -- disposed without the take-back scheme's documentation reference.
    -- Storage-level, so no application bug -- present or future -- can record
    -- an undocumented disposal.
    CONSTRAINT inventory_rma_disposed_requires_ref
        CHECK (weee_state <> 'disposed' OR disposal_ref IS NOT NULL)
);

-- Read pattern: every RMA row for one (namespace, sku), newest first.
CREATE INDEX IF NOT EXISTS idx_inventory_rma_namespace_sku
    ON inventory_rma (namespace_id, sku, created_at);

-- Batch 138b's worklist read: every RMA row still awaiting its stock leg.
CREATE INDEX IF NOT EXISTS idx_inventory_rma_pending
    ON inventory_rma (namespace_id)
    WHERE stock_movement_state = 'pending';

ALTER TABLE inventory_rma ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_rma FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON inventory_rma;
CREATE POLICY tenant_isolation_policy ON inventory_rma
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE inventory_rma FROM nce_app;
        -- UPDATE is granted because Batch 138b transitions the two state
        -- columns (weee_state, stock_movement_state) on this same row. No
        -- DELETE: an RMA row is compliance evidence and nothing in the
        -- application may erase a WEEE disposal record -- a correction is a
        -- new row, never an erased one.
        GRANT SELECT, INSERT, UPDATE ON TABLE inventory_rma TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE inventory_rma IS
'Returns/RMA + WEEE disposal state (Module 11, Wave 10 -- rma-table).
One row per return: do_record_rma (nce/vertical_modules/inventory/rma.py)
INSERT-only creates this row with stock_movement_state = ''pending'' and
records the WEEE compliance lifecycle for the returned item. This table
records; it does not move stock -- no inventory_transactions row is written
on this path and inventory_items is left untouched. Batch 138b performs both
stock legs (restock-on-return and permanent WEEE disposal), transitioning
weee_state / stock_movement_state on this same row via the UPDATE grant
below; Batch 138c (dead-stock-reconcile) reads this table''s settled rows
against the ledger. FORCE RLS isolates per tenant; nce_app is granted
SELECT, INSERT, UPDATE but never DELETE -- an RMA is compliance evidence and
nothing in the application may erase a WEEE disposal record.';
