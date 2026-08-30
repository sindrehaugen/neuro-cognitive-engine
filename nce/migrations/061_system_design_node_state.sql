-- 061_system_design_node_state.sql
-- Per-node LIFECYCLE STATE for the System Design engine (Module 6, Wave 16 --
-- B067g): status, revision and salience for a DEVICE, a RACK or a CABLE.
--
-- ============================================================================
-- A SIBLING TABLE, NOT A COLUMN ON system_design_geometry
-- ============================================================================
-- Signed off by Sindre. Three reasons, recorded here so nobody rediscovers
-- them later as a defect:
--
--   1. system_design_geometry already carries TWO key grains under one natural
--      key (node-geometry rows and the one per-design version row). A third
--      grain in the same table is a smell, not a saving.
--   2. The status vocabulary is PER NODE TYPE, and enforcing that in the
--      database needs node_type ON THE ROW. system_design_geometry does not
--      carry node_type and is not going to start.
--   3. The wave that reads state (W17 / B067h) never reads geometry. Separate
--      tables keep that transaction narrow.
--
-- ACCEPTED COST, STATED UP FRONT: do_get_topology will join TWO side tables
-- instead of one. That is the price of the three reasons above and it was
-- agreed before this table was written.
--
-- ============================================================================
-- A ROW MEANS "SOMEBODY DECLARED SOMETHING ABOUT THIS NODE"
-- ============================================================================
-- Round 2, ratified by Sindre. There are THREE distinguishable states, and
-- W17's retirement guard depends on all three staying distinguishable:
--
--   * NO ROW          -- nothing has ever been declared about this node. Every
--                        node authored before this wave is here, and stays
--                        here until somebody declares something. W17 DENIES.
--   * status IS NULL  -- we hold data for this node (a revision, a salience),
--                        but nobody has declared its lifecycle. W17 DENIES.
--   * status = '...'  -- a lifecycle was declared. W17 decides on the value.
--
-- The writer creates a row ONLY when the node is genuinely new to the
-- authoring call, OR the caller supplied an explicit lifecycle key. A
-- pre-existing node re-authored with no lifecycle keys -- an ordinary canvas
-- save, a geometry-only drag, 67f's "backfill by re-author" data-fix -- keeps
-- having NO row. See nce/vertical_modules/system_design/devices.py.
--
-- status IS NULLABLE AND CARRIES NO COLUMN DEFAULT, DELIBERATELY.
-- Round 1 had NOT NULL DEFAULT 'planned'. That default was a SECOND,
-- INDEPENDENT source of the one dangerous value: any future writer or manual
-- data-fix doing
--     INSERT INTO system_design_node_state (namespace_id, node_label, node_type)
-- would mint a fully retirable row without a single review touching the write
-- path. Removing the default closes that door, and NULL is what makes "the
-- caller sent a revision and nothing else" expressible without inventing a
-- lifecycle declaration nobody made. The composite CHECK below therefore
-- permits status IS NULL.
--
-- THIS MIGRATION WRITES NO ROWS AND MUST NEVER WRITE ANY. It creates the
-- table and stops. A backfill here -- INSERT ... SELECT ... FROM kg_nodes --
-- would hand every legacy as-built node a lifecycle it never had, which is the
-- one-way door this whole wave exists to keep shut. Gated by
-- tests/test_system_design_node_state.py::TestNoBackfill, which RE-APPLIES
-- this file against a namespace holding legacy nodes and asserts the table
-- stays empty.
--
-- ============================================================================
-- THE STATUS CHECK IS COMPOSITE, PER node_type -- A UNION CHECK WOULD BE WRONG
-- ============================================================================
-- The whole reason this constraint exists is that the vocabularies are
-- DISJOINT in meaning. A single
--
--     status IN (<every value from every type>)
--
-- would accept a CABLE whose status is 'inventory' and a DEVICE whose status
-- is 'connected'. Both are nonsense, and both would then be stored, read back
-- and acted on. The CASE below is therefore keyed on node_type, and its ELSE
-- branch is FALSE: an unknown node_type is REFUSED rather than waved through.
--
-- That deny-by-default ELSE is load-bearing in one specific way: PORT nodes.
-- devices.py writes DEVICE, PORT, RACK and CABLE kg_nodes, but NetBox has no
-- lifecycle status for a port (an interface is enabled or not, which is a
-- different fact), so no PORT vocabulary is contractual and none is invented
-- here. The ELSE FALSE means a PORT state row cannot be written at all --
-- structurally, not by convention.
--
-- THE `status IS NULL` ALLOWANCE LIVES INSIDE EACH ARM, NOT IN FRONT OF THE
-- CASE.  Written as `status IS NULL OR CASE ... ELSE FALSE END`, a NULL status
-- short-circuits the whole expression and the ELSE FALSE is never reached --
-- so a PORT row with no status is ACCEPTED and the one node type that must not
-- have lifecycle state acquires a row.  Caught by
-- tests/test_system_design_node_state.py::TestCompositeStatusCheck::
-- test_an_unknown_node_type_is_denied_even_with_a_null_status, which was
-- written for exactly this and found it on the first run.
--
-- The CHECK cannot evaluate to NULL (a CHECK that evaluates to NULL PASSES):
-- node_type is NOT NULL so a CASE arm is always chosen, `status IS NULL` is
-- never NULL, and `TRUE OR <null>` is TRUE while `FALSE OR <bool>` is that
-- bool -- so every arm yields a real boolean.
--
-- THE VOCABULARY IS NetBox's AND IT IS CONTRACTUAL (Copper follows NetBox as a
-- binding ADR):
--   DEVICE -> planned | staged | active | offline | decommissioning |
--             inventory | failed
--   CABLE  -> planned | connected | decommissioning
--   RACK   -> reserved | available | planned | active | deprecated
-- Adding, renaming or removing a value here is a Copper contract change.
--
-- ============================================================================
-- salience MUST BE FINITE AND NON-NEGATIVE -- AND NaN IS NOT WHAT YOU THINK
-- ============================================================================
-- PostgreSQL `numeric` NaN is NOT IEEE NaN. Measured on this server:
--     'NaN'::numeric > 1000000000        -> TRUE
--     'NaN'::numeric = 'NaN'::numeric    -> TRUE
-- So a stored NaN sorts as the LARGEST salience in the tenant. Any W17
-- predicate of the shape "salience below X -> retire" silently excludes it and
-- "salience above X -> act" silently includes it. salience exists FOR W17, so
-- a value that quietly wins every comparison is not a rendering nuisance, it
-- is a decision defect. (json.dumps would also emit a bare NaN, which is
-- invalid RFC 8259 and which JSONResponse refuses outright -- that is the
-- lesser of the two blast radii.)
--
-- NEGATIVE IS ALSO REFUSED, and that is a decision with a precedent rather
-- than a taste: this engine's own salience decay clamps at a floor of zero
-- (`GREATEST(0.0, ...)`, nce/me_app.py), so a negative salience has no meaning
-- anywhere in NCE, and one would sort below every legitimate value and thereby
-- silently satisfy a "below X -> retire" predicate. Refusing now is the
-- REVERSIBLE direction: relaxing a CHECK later is a migration; un-storing
-- values that a relaxed CHECK already let in is not.
--
--     salience >= 0 AND salience < 'Infinity'::numeric
--
-- rejects all four bad shapes in one clause, because of the NaN ordering
-- above: NaN passes `>= 0` and fails `< Infinity`; +Infinity fails
-- `< Infinity`; -Infinity fails `>= 0`; a negative fails `>= 0`.
--
-- ============================================================================
-- revision
-- ============================================================================
-- revision is INERT STORAGE this wave: NCE stores the string and interprets
-- nothing. Sibling-retirement is a Copper-side flow (Rev 2 section 7).
-- It is explicitly NOT the netbox-branching implementation, which is
-- PolyForm-licensed and forbidden: nothing here is read from, ported from or
-- modelled on it.
--
-- ============================================================================
-- AN OBLIGATION THIS TABLE HANDS TO W17 (B067h)
-- ============================================================================
-- NO FOREIGN KEY ties a state row to its kg_nodes row, and none can cheaply:
-- kg_nodes is HASH-partitioned on label and its natural key is
-- (label, namespace_id).
--
-- Consequence W17 must handle: if W17 deletes or retires a node and leaves its
-- state row behind, the row is an ORPHAN keyed by a label that no longer
-- exists. A later re-author of that same label -- the same design_id and the
-- same device_ref produce the same label deterministically -- lands on the
-- orphan via ON CONFLICT DO UPDATE and INHERITS its status. A device retired
-- as 'decommissioning' would come back already carrying that status and would
-- look to this table like a node somebody had declared, which is exactly the
-- distinction the three-state model above exists to protect.
--
-- W17 must therefore delete the state row in the same transaction as the node.
-- Stated here rather than fixed here: W17 is not this wave's.
--
-- ============================================================================
-- RLS
-- ============================================================================
-- ENABLE + FORCE ROW LEVEL SECURITY with tenant_isolation_policy scoped by
-- get_nce_namespace() -- copied from migration 039
-- (system_design_device_capabilities), this engine's original side-table, and
-- matching migration 060 (system_design_geometry).
--
-- FORCE RLS is NOT on its own the tenant boundary for this table: the pools
-- that serve requests are owner pools and BYPASS it. The boundary is the
-- explicit namespace_id = $n::uuid predicate carried by every statement in
-- nce/vertical_modules/system_design/devices.py. FORCE RLS is the backstop.
--
-- ============================================================================
-- EVERY CHECK AND UNIQUE IS EXPLICITLY NAMED
-- ============================================================================
-- An anonymous column CHECK is auto-named by PostgreSQL and the auto-name
-- differs between the two install paths (schema.sql on a fresh database vs
-- this migration on an existing one), which is a catalog divergence that has
-- already caused one rejection (Batch 132). Every constraint below is named.
--
-- Idempotent DDL throughout (IF NOT EXISTS / DO $$ ... $$).
-- MIRROR OF the block at the end of nce/schema.sql -- the DDL statements below
-- are byte-identical to that file's.
-- ============================================================================

CREATE TABLE IF NOT EXISTS system_design_node_state (
    id              UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id    UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,

    -- Graph key -- matches (label, namespace_id) in kg_nodes, exactly as
    -- system_design_device_capabilities and system_design_geometry do.
    node_label      TEXT        NOT NULL,

    -- The node's entity_type. On the row because the status vocabulary is per
    -- node type and the CHECK below has to see it. DEVICE | RACK | CABLE only:
    -- the CHECK's ELSE FALSE refuses everything else, PORT included.
    node_type       TEXT        NOT NULL,

    -- NetBox lifecycle status. NULLABLE AND WITHOUT A DEFAULT, deliberately --
    -- see the file header. NULL means "we hold data for this node, nobody has
    -- declared its lifecycle", which W17 denies on exactly as it denies on a
    -- missing row.
    status          TEXT,

    -- INERT STORAGE this wave. Free text; NCE interprets nothing.
    revision        TEXT,

    -- Per-node salience. kg_nodes has no salience column; this is it.
    -- Finite and non-negative -- see the file header for why NaN is the
    -- dangerous case and why one clause catches all four bad shapes.
    salience        NUMERIC,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT system_design_node_state_pkey PRIMARY KEY (id),
    CONSTRAINT system_design_node_state_ns_node_uq UNIQUE (namespace_id, node_label),
    CONSTRAINT system_design_node_state_node_label_not_blank
        CHECK (btrim(node_label) <> ''),
    -- COMPOSITE, per node_type. See the header: a union CHECK would let a
    -- CABLE be 'inventory'. ELSE FALSE denies unknown node types by default.
    --
    -- The `status IS NULL` allowance is INSIDE each arm, never in front of the
    -- CASE: in front, a NULL status short-circuits the whole expression and a
    -- PORT row slips past ELSE FALSE. THAT PLACEMENT IS LOAD-BEARING.
    --
    -- The disjunct itself is NOT. `NULL IN ('planned', ...)` evaluates to NULL
    -- and a CHECK that evaluates to NULL PASSES, so a NULL status is accepted
    -- with or without it. The mutation sweep proved that: removing it left the
    -- whole suite green. It is kept as DOCUMENTATION -- it says a NULL status
    -- is permitted on purpose rather than by three-valued accident -- and no
    -- test can gate it, which is recorded rather than papered over. Do NOT
    -- "simplify" it to `status IS NOT NULL AND ...`: that DOES change
    -- behaviour and breaks the revision-only row.
    CONSTRAINT system_design_node_state_status_per_node_type
        CHECK (
            CASE node_type
                WHEN 'DEVICE' THEN status IS NULL OR status IN (
                    'planned', 'staged', 'active', 'offline',
                    'decommissioning', 'inventory', 'failed'
                )
                WHEN 'CABLE' THEN status IS NULL OR status IN (
                    'planned', 'connected', 'decommissioning'
                )
                WHEN 'RACK' THEN status IS NULL OR status IN (
                    'reserved', 'available', 'planned', 'active', 'deprecated'
                )
                ELSE FALSE
            END
        ),
    -- Finite and non-negative. NaN passes >= 0 (numeric NaN sorts above
    -- everything) and is caught by < Infinity; +Infinity is caught by
    -- < Infinity; -Infinity and any negative are caught by >= 0.
    CONSTRAINT system_design_node_state_salience_finite_non_negative
        CHECK (
            salience IS NULL
            OR (salience >= 0 AND salience < 'Infinity'::numeric)
        )
);

-- Index: the primary read path -- state for a batch of node labels within one
-- namespace. (namespace_id, node_label) is already unique-indexed by the
-- UNIQUE above; no second index is added for it, and none is added for a
-- status filter: that filter (B067g2) narrows an already-narrow label set.

-- Row-Level Security.
ALTER TABLE system_design_node_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_design_node_state FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON system_design_node_state;
CREATE POLICY tenant_isolation_policy ON system_design_node_state
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

-- Application role grants.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE system_design_node_state FROM nce_app;
        -- UPDATE is required: a node's status is re-authored in place.
        -- DELETE is granted because W17 MUST delete a node's state row in the
        -- same transaction as the node itself -- see the orphan obligation in
        -- the file header. No code in THIS wave issues one.
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE system_design_node_state TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE system_design_node_state IS
'Per-node lifecycle state for the System Design engine (Module 6, Wave 16):
status, revision and salience for a DEVICE, a RACK or a CABLE.

A SIBLING of system_design_geometry, not a column on it: geometry already
carries two key grains, it does not carry node_type (which the per-type status
CHECK needs on the row), and the wave that reads state never reads geometry.
The accepted cost is that do_get_topology joins two side tables instead of one.

THREE DISTINGUISHABLE STATES, and W17''s retirement guard needs all three:
NO ROW = nothing was ever declared about this node (every pre-W16 node, and it
stays that way until somebody declares something); status IS NULL = we hold
data for this node but nobody declared a lifecycle; status = a value = a
lifecycle was declared. W17 denies on the first two. The writer creates a row
only when the node is genuinely NEW to the authoring call or the caller
supplied an explicit lifecycle key, so an ordinary re-author, a geometry-only
canvas save and a re-author-shaped data-fix all leave a pre-existing node with
no row. NOTHING backfills this table.

status is NULLABLE AND HAS NO COLUMN DEFAULT on purpose: a DEFAULT ''planned''
would be a second, independent source of the one dangerous value, mintable by
any future writer or manual data-fix that never touches the write path.

THE STATUS CHECK IS COMPOSITE, PER node_type. A union CHECK would accept a
CABLE whose status is ''inventory''. The vocabulary is NetBox''s and Copper
follows it as a binding ADR:
  DEVICE -> planned | staged | active | offline | decommissioning | inventory |
            failed
  CABLE  -> planned | connected | decommissioning
  RACK   -> reserved | available | planned | active | deprecated
The CASE''s ELSE branch is FALSE, so an unknown node_type is refused. PORT is
deliberately among the refused: NetBox has no lifecycle status for a port and
none is invented here.

salience is FINITE and NON-NEGATIVE. PostgreSQL numeric NaN is not IEEE NaN --
it compares GREATER than every finite value and equal to itself -- so a stored
NaN would sort as the largest salience in the tenant and silently flip any W17
threshold predicate. Negative is refused because this engine''s own salience
decay clamps at a floor of zero, so a negative has no meaning in NCE.

W17 OBLIGATION: no FK ties a state row to its node (kg_nodes is HASH-partitioned
on label). W17 must delete a node''s state row in the same transaction as the
node, or a later re-author of the same deterministic label inherits the
orphan''s status through ON CONFLICT DO UPDATE.

revision is INERT STORAGE this wave (Copper-side sibling-retirement flow, Rev 2
section 7); it is explicitly NOT the PolyForm-licensed netbox-branching design.

FORCE RLS isolates per tenant, but the pools that serve requests are owner
pools and bypass it -- the real boundary is the explicit namespace_id predicate
in nce/vertical_modules/system_design/devices.py.';

COMMENT ON COLUMN system_design_node_state.node_label IS
'Graph key. Matches kg_nodes.label. One row per node, at most. No FK -- see the
W17 orphan obligation on the table comment.';

COMMENT ON COLUMN system_design_node_state.node_type IS
'The node''s entity_type, on the row because the status vocabulary is per node
type. DEVICE | RACK | CABLE -- the composite CHECK refuses anything else,
including PORT.';

COMMENT ON COLUMN system_design_node_state.status IS
'NetBox lifecycle status, validated per node_type by the composite CHECK.
CONTRACTUAL VOCABULARY -- adding or renaming a value is a Copper contract
change. NULLABLE AND WITHOUT A DEFAULT: NULL means "we hold data for this node,
nobody has declared its lifecycle", which W17 denies on exactly as it denies on
a missing row. A column DEFAULT would be a second source of ''planned'' that no
review of the write path could catch.';

COMMENT ON COLUMN system_design_node_state.revision IS
'Inert storage. Free text, stored verbatim, interpreted by Copper (Rev 2
section 7). NOT the PolyForm-licensed netbox-branching model.';

COMMENT ON COLUMN system_design_node_state.salience IS
'Per-node salience. Stored here because kg_nodes has no salience column.
FINITE and NON-NEGATIVE: PostgreSQL numeric NaN sorts ABOVE every finite value,
so a stored NaN would silently win every W17 threshold comparison.';
