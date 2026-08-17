-- 047_economy_bom_actual_cost.sql
-- ============================================================================
-- Economy engine (Module 8, Wave 5 — cascade): BOM_LINE.actual_cost.
--
-- Orchestrator ruling (Batch 120): the wave text says "no migration" because
-- the cascade "uses the existing graph + W3" -- but actual_cost has no home
-- in the graph. BOM_LINE nodes are kg_nodes rows (label-addressed, e.g.
-- "BOM_LINE:{QUOTE}:{REF}") and kg_nodes carries no JSONB/numeric payload
-- column other than the edge-only `confidence` -- every existing reference
-- to a BOM_LINE in this codebase (sales/dealroom.py, project/convert.py,
-- project/tasks.py) is a READ, never a write of a cost figure. So this table
-- is the field's home, exactly as PRODUCT_SKU attributes live in
-- product_catalog and device capabilities live in
-- system_design_device_capabilities (migration 039) rather than on kg_nodes
-- itself.
--
-- Field ownership (roadmap §9.1 "5-writer BOM_LINE" worked example):
-- do_cascade_on_approval (nce/vertical_modules/economy/cascade.py) is the
-- SOLE writer of actual_cost. Content is Sales-frozen; status transitions
-- belong to Procurement/Warehouse/Field Tech (never this table); actual_cost
-- belongs to the Economy cascade alone. No kg_nodes/kg_edges rows are written
-- by the cascade at all.
--
-- ROUND 2 (money-semantics auditor REJECT, CRITICAL finding): round 1's
-- natural key was (namespace_id, bom_line_label) with `ON CONFLICT ... DO
-- UPDATE SET actual_cost = EXCLUDED.actual_cost` -- a plain replace. A
-- SECOND approval against the same BOM line silently overwrote the first
-- instead of adding to it (reproduced live: approval A 60 000,00 then
-- approval B 40 000,00 left the row at 40 000,00, not 100 000,00) --
-- understating incurred cost and inflating reported margin. Partial
-- delivery and split invoicing against one BOM line are ordinary in this
-- domain (see the roadmap's own Inventory language, "partial-GR vs
-- BOM_LINE.DELIVERED"), so losing the earlier invoice is a real bug, not an
-- edge case.
--
-- Fix: the natural key becomes (namespace_id, bom_line_label,
-- source_approval_id) -- ONE ROW PER (line, approval) -- and the write
-- becomes `ON CONFLICT ... DO NOTHING`, so a replay of the SAME approval is
-- a no-op BY CONSTRUCTION (the constraint refuses the duplicate row), not by
-- a guard that has to stay correct across interleaved replays. A DIFFERENT
-- approval against the same line is now a NEW row rather than an overwrite.
-- The line's actual cost is therefore the SUM(actual_cost) of its rows,
-- computed by the cascade (nce/vertical_modules/economy/cascade.py) --
-- never stored as a single scalar here. A credit note is a legitimate
-- negative actual_cost row under this scheme -- see cascade.py's
-- `_as_actual_cost` sign-convention note.
--
-- This migration has NOT merged (Batch 120 is still in round-2 review), so
-- it is amended in place rather than superseded by an 048 -- there is no
-- production data to preserve, only the constraint/column shape needs to
-- converge for any environment that already applied the round-1 version of
-- this file.
--
-- Idempotent DDL -- there is no migration ledger in this repo; schema.sql and
-- every migrations/*.sql file re-run on every boot under an advisory lock.
-- ============================================================================

CREATE TABLE IF NOT EXISTS economy_bom_actual_costs (
    id                 UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id       UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    bom_line_label     TEXT        NOT NULL,
    -- NOK, 2-decimal (øre) precision. No money column elsewhere in this
    -- schema declares an explicit scale (sales_signed_baselines.signed_total_nok
    -- and procurement_bid_prices.pris are both bare NUMERIC) -- there is no
    -- existing explicit-scale convention to copy for money, only the general
    -- practice this repo already follows of pinning scale where it matters
    -- (confidence NUMERIC(5,4), migration 035). NUMERIC(18,2) sets that
    -- precedent for money: 2 decimals for øre, 18 total digits leaves
    -- headroom far beyond any realistic BOM line cost while still refusing
    -- a stray fractional-øre amount instead of silently truncating it.
    actual_cost        NUMERIC(18,2) NOT NULL,
    -- Idempotency provenance AND (round 2) part of the natural key: the
    -- approval_id of the cascade run that wrote THIS row. One row per
    -- (line, approval) -- see the natural-key constraint below. NOT NULL:
    -- every row is written by exactly one cascade run and the constraint's
    -- own conflict target depends on this column being present.
    source_approval_id TEXT        NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- Converge an already-applied round-1 table (2-column natural key, nullable
-- source_approval_id, unscaled actual_cost) onto the round-2 shape. All
-- three statements are no-ops against a freshly-created round-2 table (the
-- CREATE TABLE above already has the final shape) and against a table
-- already converged by a prior run of this same file -- idempotent either
-- way. No data-preserving backfill is needed: this table holds no
-- production data (the migration has not merged).
ALTER TABLE economy_bom_actual_costs ALTER COLUMN source_approval_id SET NOT NULL;
ALTER TABLE economy_bom_actual_costs ALTER COLUMN actual_cost TYPE NUMERIC(18,2);

ALTER TABLE economy_bom_actual_costs DROP CONSTRAINT IF EXISTS economy_bom_actual_costs_natural_key;
ALTER TABLE economy_bom_actual_costs
    ADD CONSTRAINT economy_bom_actual_costs_natural_key
    UNIQUE (namespace_id, bom_line_label, source_approval_id);

-- Non-unique: supports both the per-line lookup (WHERE namespace_id=... AND
-- bom_line_label=...) and the per-quote SUM(actual_cost) grouped read the
-- margin-trinity snapshot uses (WHERE bom_line_label LIKE
-- 'BOM_LINE:{QUOTE}:%') -- both filter on this column pair regardless of how
-- many approval rows a line has accumulated.
CREATE INDEX IF NOT EXISTS idx_economy_bom_actual_costs_namespace_label
    ON economy_bom_actual_costs (namespace_id, bom_line_label);

ALTER TABLE economy_bom_actual_costs ENABLE ROW LEVEL SECURITY;
ALTER TABLE economy_bom_actual_costs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON economy_bom_actual_costs;
CREATE POLICY tenant_isolation_policy ON economy_bom_actual_costs
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE economy_bom_actual_costs FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE economy_bom_actual_costs TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE economy_bom_actual_costs IS
'Economy-owned actual-cost-per-BOM-line table (Module 8, Wave 5, round 2).
do_cascade_on_approval (nce/vertical_modules/economy/cascade.py) is the SOLE
writer -- the clean decomposition of the BOM_LINE "5-writer race" (roadmap
§9.1): content is Sales-frozen, status transitions belong to
Procurement/Warehouse/Field Tech, and actual_cost belongs to the Economy
cascade alone. Natural-keyed (namespace_id, bom_line_label,
source_approval_id) -- ONE ROW PER (line, approval); INSERT ... ON CONFLICT
DO NOTHING, so a replay of the same approval is a no-op by construction and a
DIFFERENT approval against the same line is a new row rather than an
overwrite (round 1''s `DO UPDATE SET actual_cost = EXCLUDED.actual_cost`
silently lost an earlier approval''s cost -- see this file''s header
comment). The line''s actual cost is SUM(actual_cost) grouped by
(namespace_id, bom_line_label), computed by the cascade -- never stored as a
single scalar. A negative actual_cost row is a legitimate credit note.
FORCE RLS isolates per tenant (mirrors procurement_bid_prices pattern).';
