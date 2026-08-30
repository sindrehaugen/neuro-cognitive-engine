-- 052_goods_receipts.sql
-- ============================================================================
-- Inventory engine (Module 11, Wave 4 -- goods-receipt, Batch 132): the
-- `goods_receipts` table + the idempotent, ledgered, authoritative inbound
-- stock increment backing `nce/vertical_modules/inventory/goods_receipt.py`'s
-- `do_record_goods_receipt`.
--
-- RE-SCOPED 2026-08-17: this migration used to also cover a graph projection
-- and a C4 publish. Both were split out (B132b / B132c). This migration
-- writes ONE new table and widens ONE existing CHECK -- no kg_nodes, no
-- kg_edges, no event-bus schema.
--
-- A goods receipt is where unit cost first enters the system: inventory_items
-- (migration 050) has no cost column, so unit_cost rides on the
-- inventory_transactions ledger row (migration 051) this table's writer
-- appends -- never on this table itself. This table is the RECORD of the
-- delivery (what arrived, when, against which PO, with which scans);
-- inventory_items remains the authoritative stock row and
-- inventory_transactions the movement ledger. match_result is reserved (NULL
-- until Batch 133's Receive->Match->Cascade verdict populates it) so the
-- column shape does not have to change again.
--
-- Idempotency is BY CONSTRUCTION: goods_receipts_idempotency_uq on
-- (namespace_id, receipt_hash) refuses a duplicate INSERT at the DB level --
-- never a check-then-write a caller has to remember to write correctly.
-- receipt_hash covers (po_ref, delivery_note_ref, location_id, lines, scans);
-- delivery_note_ref exists so that two GENUINE partial deliveries against one
-- PO line do not collide into one receipt (see its COMMENT ON COLUMN).
--
-- Idempotent DDL -- there is no migration ledger in this repo; schema.sql and
-- every migrations/*.sql file re-run on every boot under an advisory lock.
-- ============================================================================

CREATE TABLE IF NOT EXISTS goods_receipts (
    id             UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id   UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    -- Stored in the SAME normal form the receipt hash uses: stripped and
    -- UPPER-CASED by goods_receipt.py's _as_po_ref, once, at the boundary.
    -- See the COMMENT ON COLUMN below -- Batch 133's matcher queries this
    -- column and must not have to guess the case.
    po_ref         TEXT          NOT NULL,
    -- OPTIONAL delivery-note / packing-slip number, same normal form as
    -- po_ref (stripped + upper-cased; blank collapses to NULL). PARTICIPATES
    -- IN receipt_hash, which is the whole point: two genuine PARTIAL
    -- deliveries against one PO -- same location, byte-identical line set,
    -- no scans -- would otherwise hash identically and the second would be
    -- swallowed as a replay, silently losing stock. NULL is legal and means
    -- "no note supplied": hashing then behaves exactly as it did before this
    -- column existed, collision included.
    delivery_note_ref TEXT,
    location_id    UUID          NOT NULL,
    -- Aggregated, sku-sorted line list -- see goods_receipt.py's
    -- _compute_receipt_hash for the exact canonical shape this is hashed
    -- from. Never mutated after insert (WORM-adjacent: a correction is a NEW
    -- receipt, never an edit of this row's lines).
    lines          JSONB         NOT NULL,
    -- Per-unit barcode/serial capture, optional. A SECOND way to distinguish
    -- two deliveries with an identical line set (delivery_note_ref above is
    -- the primary one): real serials differ between real deliveries even
    -- when the aggregate line set does not.
    scans          JSONB         NOT NULL DEFAULT '[]'::jsonb,
    -- Reserved for Batch 133's Receive->Match->Cascade verdict -- NULL until
    -- that wave lands. This wave creates the column and writes nothing to
    -- it; do not populate it here.
    match_result   JSONB,
    -- sha256 hex over the canonically normalised (po_ref, delivery_note_ref,
    -- location_id, lines, scans) payload -- see goods_receipt.py's
    -- _compute_receipt_hash. Deliberately excludes received_at/created_at/id:
    -- including a timestamp would make every retry a new receipt and defeat
    -- idempotency entirely.
    receipt_hash   TEXT          NOT NULL,
    received_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    -- Composite FK target is stock_locations_id_ns_uq (migration 050) -- a
    -- receipt can never point at another tenant's location, refused by
    -- Postgres, not by careful code.
    CONSTRAINT goods_receipts_location_fk
        FOREIGN KEY (location_id, namespace_id)
        REFERENCES stock_locations (id, namespace_id)
        ON DELETE CASCADE,
    -- THE idempotency arbiter. A replay's INSERT ... ON CONFLICT (namespace_id,
    -- receipt_hash) DO NOTHING returns no row, and every subsequent effect in
    -- do_record_goods_receipt is gated on that row having been returned --
    -- that gating IS the idempotency, refused by Postgres at the DB level,
    -- never a Python-side check-then-write.
    CONSTRAINT goods_receipts_idempotency_uq UNIQUE (namespace_id, receipt_hash)
);

-- Idempotent re-run safety for a database that already received an EARLIER
-- revision of this migration (the table exists, so CREATE TABLE IF NOT EXISTS
-- above is a no-op and would leave the column missing). On both audited
-- install paths -- schema.sql alone, and origin/main's schema.sql + this file
-- -- the column already came from the CREATE TABLE above and this statement
-- does nothing, so the two catalogs stay identical. Same statement, verbatim,
-- in nce/schema.sql.
ALTER TABLE goods_receipts ADD COLUMN IF NOT EXISTS delivery_note_ref TEXT;

COMMENT ON COLUMN goods_receipts.po_ref IS
'Purchase-order reference, stored in ONE normal form: stripped and
UPPER-CASED (goods_receipt.py''s _as_po_ref), the same value hashed into
receipt_hash. Normalising for the hash but storing verbatim -- an earlier
revision of this wave -- made idempotency case-insensitive while this column
and idx_goods_receipts_namespace_po stayed case-sensitive, so a replay was
correctly detected yet a lookup by the canonical case found nothing. Batch
133''s matcher queries this column: match against the upper-cased form.';

COMMENT ON COLUMN goods_receipts.delivery_note_ref IS
'OPTIONAL delivery-note / packing-slip number from the paperwork that arrived
with the goods; stripped and UPPER-CASED like po_ref, blank collapsed to
NULL. PARTICIPATES IN receipt_hash so two genuine PARTIAL deliveries against
the same PO line -- identical location, identical aggregated lines, no scans
-- are two receipts instead of one swallowed replay, while a true retry of
the SAME note remains idempotent. NULL (no note supplied) reproduces the
pre-existing behaviour exactly, collision included.';

COMMENT ON COLUMN goods_receipts.match_result IS
'Reserved for Batch 133''s Receive->Match->Cascade verdict. NULL until that
wave lands; this wave (Batch 132) creates the column and never writes to it.';

CREATE INDEX IF NOT EXISTS idx_goods_receipts_namespace_po
    ON goods_receipts (namespace_id, po_ref);

CREATE INDEX IF NOT EXISTS idx_goods_receipts_namespace_location
    ON goods_receipts (namespace_id, location_id);

ALTER TABLE goods_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE goods_receipts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON goods_receipts;
CREATE POLICY tenant_isolation_policy ON goods_receipts
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE goods_receipts FROM nce_app;
        -- Live mutable-shape record (not a WORM ledger like inventory_transactions)
        -- -- nce_app gets the full CRUD set, mirroring stock_locations/inventory_items
        -- (migration 050). match_result is the one column a LATER wave (Batch 133)
        -- will UPDATE; this wave never does.
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE goods_receipts TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE goods_receipts IS
'Record of one inbound delivery (Module 11, Wave 4 -- goods-receipt, Batch
132). Idempotent on (namespace_id, receipt_hash) -- goods_receipts_idempotency_uq
refuses a duplicate INSERT at the DB level, so a replay (including two
concurrent identical submissions) increments stock exactly once.
receipt_hash covers po_ref, delivery_note_ref, location_id, lines and scans;
supplying delivery_note_ref is how two GENUINE partial deliveries against one
PO line stay two receipts. This row is
the RECORD of the delivery; inventory_items (migration 050) remains the
AUTHORITATIVE stock row and inventory_transactions (migration 051) the
movement ledger -- do_record_goods_receipt increments the former and appends
one goods_receipt-category row per line to the latter, inside the SAME
transaction as this row''s own INSERT. match_result is reserved for Batch
133''s Receive->Match->Cascade verdict and is NULL until then. The graph
projection (GOODS_RECEIPT kg_node, -[against]->PO / -[of]->SKU edges) is
Batch 132b''s -- no kg_node or kg_edge is written from this table or its
writer. FORCE RLS isolates per tenant; location_id is a composite FK on
(location_id, namespace_id) into stock_locations so a receipt can never
reference another tenant''s location.';

-- ============================================================================
-- Widen inventory_transactions' typed reason_category vocabulary to admit
-- 'goods_receipt' -- migration 051's own header asked this wave, by name, to
-- do exactly this ("Extend this list via an idempotent ALTER ... DROP/ADD
-- CONSTRAINT in the migration that lands the next writer (e.g. Batch 132
-- goods-receipt) -- never widen it by dropping the CHECK outright.").
--
-- reason_category was an INLINE (unnamed) column CHECK in migration 051, so
-- Postgres auto-named it '{table}_{column}_check' ->
-- inventory_transactions_reason_category_check. Drop that auto-generated
-- name, drop our own new explicit name (idempotent re-run safety), then
-- re-add under the explicit name with the widened list.
-- ============================================================================

ALTER TABLE inventory_transactions
    DROP CONSTRAINT IF EXISTS inventory_transactions_reason_category_check;
ALTER TABLE inventory_transactions
    DROP CONSTRAINT IF EXISTS inventory_transactions_reason_category;
ALTER TABLE inventory_transactions
    ADD CONSTRAINT inventory_transactions_reason_category
    CHECK (reason_category IN
        ('transfer_in', 'transfer_out', 'consumption', 'adjustment', 'goods_receipt'));

ALTER TABLE inventory_transactions
    DROP CONSTRAINT IF EXISTS inventory_transactions_sign_matches_category;
ALTER TABLE inventory_transactions
    ADD CONSTRAINT inventory_transactions_sign_matches_category CHECK (
        (reason_category = 'transfer_in' AND delta > 0)
        OR (reason_category IN ('transfer_out', 'consumption') AND delta < 0)
        OR (reason_category = 'adjustment')
        OR (reason_category = 'goods_receipt' AND delta > 0)
    );
