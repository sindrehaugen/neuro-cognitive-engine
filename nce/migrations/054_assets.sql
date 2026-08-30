-- 054_assets.sql
-- ============================================================================
-- Assets engine (Module 9, Wave 2 -- seed-from-bom): the `assets` table
-- backing nce/vertical_modules/assets/seed.py's do_seed_asset_from_bom. One
-- row per originating BOM line -- the RELATIONAL asset register that
-- docs/vertical_engines/09-assets-engine.md's "Tables/migrations" section
-- calls the "fast register ... high-volume room-register reads".
--
-- MIGRATION NUMBER IS PRE-ALLOCATED (054), NOT DERIVED
-- --------------------------------------------------------------------------
-- Do not renumber this file by listing the directory. main holds 050/051/053
-- and 052 is Batch 132's, in flight in an open PR (branch
-- vm-b132-m11-w4-goods-receipt) -- a directory listing would hand the next
-- writer 052 and collide. The next free number after this file is 055.
--
-- THIS WAVE WRITES NO GRAPH -- declared, not silently omitted
-- --------------------------------------------------------------------------
-- Batch 142 was SPLIT. This half is relational only. The ASSET kg_node, the
-- BOM_LINE -[installed_as]-> ASSET / ASSET -[lives_in]-> FUNCTIONAL_LOCATION
-- edges, and ASSET's Contract-A row in nce/config_data/node-ownership.json
-- are Batch 142b's. Same shape as Batch 132's goods_receipt.py/migration 052,
-- whose module docstring likewise declares "This module writes NO kg_nodes
-- and NO kg_edges at all" while GOODS_RECEIPT's ownership row lands in 132b.
--
-- NO FOREIGN KEY ON bom_line_id OR functional_location_id -- also declared
-- --------------------------------------------------------------------------
-- Neither target is a table in this repo. BOM_LINE is a graph node type that
-- NOTHING creates yet (Batch 132a, unbuilt), and FUNCTIONAL_LOCATION is the
-- shared System-Design/NetBox spine node whose intent->as-built lifecycle is
-- an unresolved foundation gap (roadmap §9.1; 09-assets-engine.md "Review
-- round-2 hardening" #1). Both columns are therefore plain TEXT REFERENCES-
-- IN-NAME-ONLY, carrying the originating identifier so Batch 142b can build
-- both edges by reading this row -- exactly the way migration 053 provisioned
-- location_id/qty for Batch 138b. A real FK becomes possible only once those
-- nodes have a relational home; adding one here would fabricate a dependency
-- on a table that does not exist.
--
-- THE IDEMPOTENCY KEY DEVIATES FROM THE ENGINE SPEC -- declared, not silent
-- --------------------------------------------------------------------------
-- docs/vertical_engines/09-assets-engine.md ("Core functions",
-- do_seed_asset_from_bom) specifies "Idempotent on serial." The UNIQUE below
-- is on (namespace_id, bom_line_id) instead. This is a deliberate
-- substitution, named here rather than left to be discovered.
--
-- WHY the spec's key is not usable: serial is nullable BY DESIGN, because a
-- seed made at install handover legitimately precedes the installer's serial
-- scan (see the serial column's comment). A UNIQUE (namespace_id, serial)
-- cannot express this idempotency in either shape available to it -- under
-- Postgres' default NULLS-DISTINCT semantics it is VACUOUS for exactly the
-- pre-scan case (every unscanned seed inserts and never conflicts, so
-- re-seeding one BOM line before the scan double-writes), and under NULLS NOT
-- DISTINCT it over-constrains, collapsing every unscanned asset in a
-- namespace into one row and refusing the second legitimate pre-scan seed.
-- bom_line_id is NOT NULL and present at seed time, so it is the only offered
-- key that actually holds.
--
-- THE CONSEQUENCE, ACCEPTED HERE: the two keys are not equivalent. One
-- physical device re-issued under a second BOM line -- SN-123 seeded against
-- BL-1, the BOM revised and the same unit re-issued as BL-2 -- yields TWO
-- rows, both reported as newly created, and nothing detects it: there is no
-- unique index on (namespace_id, serial) and no code reads by serial. Whether
-- serial uniqueness is wanted at all is a LATER wave's decision. It cannot be
-- a plain UNIQUE while serial is nullable, so a partial index WHERE serial IS
-- NOT NULL would be its shape; this wave neither adds it nor implies it.
--
-- EVERY CHECK AND UNIQUE IS EXPLICITLY NAMED
-- --------------------------------------------------------------------------
-- An anonymous column CHECK is auto-named by PostgreSQL, and the auto-name
-- depends on the order constraints are created in -- so a fresh install (from
-- schema.sql alone) and a migrated install (previous schema.sql + this file)
-- can end up with catalogs that agree on the enforced EXPRESSION but differ
-- in constraint IDENTITY. That precise divergence caused a rejection on
-- Batch 132. Every CHECK and UNIQUE below carries an explicit name, and the
-- DDL statements here are byte-identical to the block mirrored into
-- nce/schema.sql.
--
-- WHY lifecycle_state HAS NO ENUMERATED CHECK -- a decision, not an oversight
-- --------------------------------------------------------------------------
-- The 14-state vocabulary is config-as-IP: it lives in
-- nce/config_data/asset-lifecycle.json, which Batch 141's lifecycle.py reads
-- and which 09-assets-engine.md's "Config keys" section says each tenant
-- tunes. Freezing that list into DDL would make a config change require a
-- migration -- the exact coupling the config-as-IP convention exists to
-- prevent -- and would create a second, competing source of truth for the
-- state set. This column therefore gets a structural non-blank CHECK only;
-- the legal-value set stays in the JSON, and lifecycle.py's advance() stays
-- the only arbiter of legal transitions. (Contrast migration 053's
-- weee_state, whose vocabulary is NOT config-driven and IS enumerated in
-- DDL.)
--
-- Idempotent DDL -- there is no migration ledger in this repo; schema.sql and
-- every migrations/*.sql file re-run on every boot under an advisory lock.
-- ============================================================================

CREATE TABLE IF NOT EXISTS assets (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    -- The originating BOM line, as a caller-supplied identifier. This is the
    -- idempotency handle for do_seed_asset_from_bom (see the UNIQUE below)
    -- and Batch 142b's handle for the BOM_LINE -[installed_as]-> ASSET edge.
    -- NOT an FK: no BOM_LINE table or node exists yet (file header).
    bom_line_id            TEXT        NOT NULL,
    -- Serialised units; a seed made before the installer scans a serial has
    -- none. Nullable on purpose -- an absent serial is an honest "not
    -- captured yet", never an empty string (assets_serial_not_blank).
    serial                 TEXT,
    -- The room the asset lives in. Nullable for the same reason, and NOT an
    -- FK (file header). Batch 142b builds ASSET -[lives_in]->
    -- FUNCTIONAL_LOCATION from this column.
    functional_location_id TEXT,
    -- The 14-state lifecycle position. Written once here, at seed time, from
    -- asset-lifecycle.json's entry state; transitions belong to a later
    -- wave's do_advance_lifecycle, which is why nce_app holds UPDATE below.
    -- No enumerated CHECK -- see the file header.
    lifecycle_state        TEXT        NOT NULL,
    change_origin          TEXT        NOT NULL DEFAULT 'agent',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    -- THE idempotency arbiter: one asset per (namespace, BOM line). Seeding
    -- the same line twice is refused HERE, by the database, not by a Python
    -- "does it exist?" pre-check -- two concurrent identical seeds would both
    -- pass such a pre-check and both insert. Precedent: migration 053's
    -- inventory_rma_ns_ref_uq, migration 052's goods_receipts_idempotency_uq.
    CONSTRAINT assets_ns_bom_line_uq UNIQUE (namespace_id, bom_line_id),
    -- Structural non-blank guards: a whitespace-only identifier is not an
    -- identifier, and must not be able to occupy the idempotency key.
    CONSTRAINT assets_bom_line_id_not_blank
        CHECK (btrim(bom_line_id) <> ''),
    CONSTRAINT assets_lifecycle_state_not_blank
        CHECK (btrim(lifecycle_state) <> ''),
    CONSTRAINT assets_serial_not_blank
        CHECK (serial IS NULL OR btrim(serial) <> ''),
    CONSTRAINT assets_functional_location_id_not_blank
        CHECK (functional_location_id IS NULL OR btrim(functional_location_id) <> ''),
    CONSTRAINT assets_change_origin_check
        CHECK (change_origin IN
            ('sync','webhook','agent','operator','consolidation','replay','unknown'))
);

-- The room-centric register read named in 09-assets-engine.md's REST surface
-- (api_assets_register: "assets by FUNCTIONAL_LOCATION"). The
-- (namespace_id, bom_line_id) read is already served by the unique index
-- behind assets_ns_bom_line_uq. Deliberately NOT indexed here: serial,
-- lifecycle_state -- nothing in this wave or Batch 142b reads by either, and
-- the wave that does owns its own index.
CREATE INDEX IF NOT EXISTS idx_assets_namespace_functional_location
    ON assets (namespace_id, functional_location_id);

ALTER TABLE assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE assets FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON assets;
CREATE POLICY tenant_isolation_policy ON assets
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE assets FROM nce_app;
        -- UPDATE is granted for do_advance_lifecycle, which transitions
        -- lifecycle_state on this same row (09-assets-engine.md, build phase
        -- B1) -- the same forward-provisioning migration 053 did for Batch
        -- 138b. No DELETE: retirement is a lifecycle STATE (RETIRED), never a
        -- deleted row -- an asset register that can forget a device is not a
        -- register. A namespace teardown still removes rows via the
        -- namespace_id FK's ON DELETE CASCADE, which RLS grants do not gate.
        GRANT SELECT, INSERT, UPDATE ON TABLE assets TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE assets IS
'Relational asset register (Module 9, Wave 2 -- seed-from-bom). One row per
originating BOM line: do_seed_asset_from_bom
(nce/vertical_modules/assets/seed.py) is the SOLE writer and is INSERT-only,
creating the row with lifecycle_state taken from
nce/config_data/asset-lifecycle.json''s entry state via Batch 141''s pure
lifecycle module. Idempotency is by DB constraint
(assets_ns_bom_line_uq + INSERT ... ON CONFLICT DO NOTHING), never a
check-then-write. This table is the RELATIONAL half only: the ASSET kg_node
and the BOM_LINE -[installed_as]-> ASSET / ASSET -[lives_in]->
FUNCTIONAL_LOCATION edges are Batch 142b''s, as is ASSET''s row in
node-ownership.json -- no code in this wave writes kg_nodes or kg_edges.
bom_line_id and functional_location_id are identifier columns with NO foreign
key: neither target has a relational home yet (BOM_LINE nodes are Batch
132a, unbuilt; FUNCTIONAL_LOCATION is the unresolved intent->as-built spine
gap, roadmap §9.1). lifecycle_state carries no enumerated CHECK because the
state vocabulary is config-as-IP in asset-lifecycle.json, tuned per tenant.
FORCE RLS isolates per tenant; nce_app is granted SELECT, INSERT, UPDATE
(the UPDATE is for a later wave''s do_advance_lifecycle) but never DELETE --
retirement is the RETIRED lifecycle state, not an erased row.';
