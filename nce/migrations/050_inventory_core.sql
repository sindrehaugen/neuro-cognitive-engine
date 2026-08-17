-- 050_inventory_core.sql
-- ============================================================================
-- Inventory engine (Module 11, Wave 1 -- locations-stock-tables): the
-- `stock_locations` (hierarchical warehouse/van tree) and `inventory_items`
-- (per-SKU-per-location stock row) tables backing
-- nce/vertical_modules/inventory/schema_seed.py's warehouse+van seed.
--
-- STOCK_LOCATION is NOT a FUNCTIONAL_LOCATION (docs/vertical_engines/
-- 11-inventory-engine.md, "Review round-2 hardening" #1): a warehouse/van is
-- a company-internal LOGISTICS location -- a completely different ontology
-- from the customer-site FUNCTIONAL_LOCATION tree System Design/D365 own.
-- This table intentionally carries NO functional_location_id column and no
-- FK toward any customer-site table -- two trees, not one. Pinned by
-- tests/test_inventory_tables.py's
-- test_stock_locations_table_has_no_functional_location_column.
--
-- Idempotent DDL -- there is no migration ledger in this repo; schema.sql and
-- every migrations/*.sql file re-run on every boot under an advisory lock.
-- ============================================================================

CREATE TABLE IF NOT EXISTS stock_locations (
    id            UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    -- warehouse/van are flat top-level (parent_id IS NULL, level = 0); zone/bin
    -- are the hierarchical children a warehouse can have. Enforced structurally
    -- below (stock_locations_hierarchy_shape), not just by convention -- a van
    -- can never gain a parent, and a zone/bin can never float parentless.
    kind          TEXT        NOT NULL
                              CHECK (kind IN ('warehouse', 'van', 'zone', 'bin')),
    name          TEXT        NOT NULL,
    parent_id     UUID,
    level         INT         NOT NULL DEFAULT 0 CHECK (level >= 0),
    -- Only meaningful for kind='van' -- the VEHICLE+STOCK_LOCATION shared-node
    -- link (roadmap §4 graph contract). NULL for warehouse/zone/bin; nothing
    -- enforces that narrower rule here since the link is populated by a later
    -- wave's field-tech wiring, not this one.
    vehicle_ref   TEXT,
    raw           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    -- Composite-unique target so the self-referencing parent_id FK below can
    -- also pin namespace_id -- a hierarchy edge can never cross a tenant
    -- boundary (same-namespace-hierarchy invariant, structurally enforced).
    CONSTRAINT stock_locations_id_ns_uq UNIQUE (id, namespace_id),
    CONSTRAINT stock_locations_parent_fk
        FOREIGN KEY (parent_id, namespace_id)
        REFERENCES stock_locations (id, namespace_id)
        ON DELETE CASCADE,
    CONSTRAINT stock_locations_hierarchy_shape CHECK (
        (kind IN ('warehouse', 'van') AND parent_id IS NULL AND level = 0)
        OR
        (kind IN ('zone', 'bin') AND parent_id IS NOT NULL AND level > 0)
    )
);

-- Idempotent-seed arbiter: one row per (namespace, kind, name) among the
-- parentless (top-level) rows -- schema_seed.py's ON CONFLICT target for the
-- warehouse + van seed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_locations_top_level_name
    ON stock_locations (namespace_id, kind, name)
    WHERE parent_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_stock_locations_namespace_kind
    ON stock_locations (namespace_id, kind);

CREATE INDEX IF NOT EXISTS idx_stock_locations_parent
    ON stock_locations (namespace_id, parent_id);

ALTER TABLE stock_locations ENABLE ROW LEVEL SECURITY;
ALTER TABLE stock_locations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON stock_locations;
CREATE POLICY tenant_isolation_policy ON stock_locations
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE stock_locations FROM nce_app;
        -- Live mutable record (not a WORM ledger) -- nce_app gets the full
        -- CRUD set, mirroring economy_contracts (migration 049).
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE stock_locations TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE stock_locations IS
'Inventory-owned internal LOGISTICS location tree (Module 11, Wave 1). NOT a
customer FUNCTIONAL_LOCATION (system_design/D365''s customer-site tree) --
two trees, not one; see the file header. Hierarchical: warehouse -> zone ->
bin (parent_id chains, level increments); a van is a flat top-level location
(parent_id IS NULL, level = 0), just like a warehouse -- both shapes are
enforced by stock_locations_hierarchy_shape, not left to caller discipline.
schema_seed.py''s seed_warehouse_and_vans is the reference writer: one
warehouse + N vans per namespace, idempotent via the partial unique index
uq_stock_locations_top_level_name. FORCE RLS isolates per tenant; the
self-referencing parent_id FK is composite on (parent_id, namespace_id) so a
hierarchy edge can never cross a tenant boundary.';


CREATE TABLE IF NOT EXISTS inventory_items (
    id            UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    sku           TEXT          NOT NULL,
    location_id   UUID          NOT NULL,
    -- NUMERIC (not INT/FLOAT) -- some SKUs are sold/stocked by fractional unit
    -- (e.g. cable by the metre); Decimal-safe end-to-end, same discipline as
    -- economy_contracts.annual_amount's money precision.
    qty_on_hand   NUMERIC(18,3) NOT NULL DEFAULT 0 CHECK (qty_on_hand >= 0),
    qty_reserved  NUMERIC(18,3) NOT NULL DEFAULT 0 CHECK (qty_reserved >= 0),
    qty_blocked   NUMERIC(18,3) NOT NULL DEFAULT 0 CHECK (qty_blocked >= 0),
    reorder_point NUMERIC(18,3) NOT NULL DEFAULT 0 CHECK (reorder_point >= 0),
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    -- Composite FK on (location_id, namespace_id) -- mirrors
    -- stock_locations_parent_fk's reasoning: an inventory_items row can never
    -- point at a stock_locations row in a different tenant's namespace.
    CONSTRAINT inventory_items_location_fk
        FOREIGN KEY (location_id, namespace_id)
        REFERENCES stock_locations (id, namespace_id)
        ON DELETE CASCADE
);

-- One row per (namespace, sku, location) -- the hot read/atomic-decrement
-- path (docs' "Authority model": this row is authoritative; a future graph
-- INVENTORY_ITEM node is only an eventually-consistent projection of it).
ALTER TABLE inventory_items DROP CONSTRAINT IF EXISTS inventory_items_natural_key;
ALTER TABLE inventory_items
    ADD CONSTRAINT inventory_items_natural_key
    UNIQUE (namespace_id, sku, location_id);

CREATE INDEX IF NOT EXISTS idx_inventory_items_namespace_sku
    ON inventory_items (namespace_id, sku);

ALTER TABLE inventory_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_items FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON inventory_items;
CREATE POLICY tenant_isolation_policy ON inventory_items
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE inventory_items FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE inventory_items TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE inventory_items IS
'Per-SKU-per-location stock row (Module 11, Wave 1) -- the hot
read/atomic-decrement path. One row per (namespace, sku, location)
(inventory_items_natural_key). available = qty_on_hand - qty_reserved -
qty_blocked is computed at read time by a later wave''s do_stock_levels, not
stored here. This row is the SOURCE OF TRUTH for stock-truth reads
(Procurement''s "own stock first", forecast, reservation) -- a future
INVENTORY_ITEM graph node is only an eventually-consistent projection of it,
never the other way around. FORCE RLS isolates per tenant; location_id is a
composite FK on (location_id, namespace_id) into stock_locations so a row can
never reference another tenant''s location.';
