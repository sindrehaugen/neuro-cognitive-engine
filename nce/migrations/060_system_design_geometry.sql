-- 060_system_design_geometry.sql
-- Canvas geometry + the per-DESIGN optimistic-concurrency token for the
-- System Design engine (Module 6, Wave 14 -- B067e).
--
-- ============================================================================
-- TWO KEY GRAINS IN ONE TABLE -- DELIBERATE, AND THE ONLY EXCEPTION
-- ============================================================================
-- This table holds two kinds of row under one natural key.  It reads as a bug
-- if you do not know that, so:
--
--   1. GEOMETRY ROWS.  node_label is a *node* label -- DEVICE:, PORT:, RACK:,
--      CABLE: or FL:.  These rows carry x/y, rack_position/rack_face,
--      cable_length_m/cable_type and meta.  They NEVER carry version.
--
--   2. THE DESIGN VERSION ROW.  node_label is the *design* label
--      (DESIGN:<DESIGN_ID>) -- exactly one per design per namespace.  It
--      carries version and nothing else; every geometry column is NULL.
--
-- HOW TO TELL THEM APART, in this order:
--   * version IS NOT NULL          -> it is the design version row.
--   * node_label LIKE 'DESIGN:%'   -> it is the design version row.
-- Both hold simultaneously for every row this engine writes; version is the
-- discriminator to reach for in SQL because it is indexed by the primary read
-- and needs no pattern match.  A geometry row is anything with version NULL.
--
-- The two grains share a table rather than splitting because they share the
-- same natural key (namespace_id, node_label), the same tenancy boundary, the
-- same lifecycle (both die with their namespace) and the same writer
-- transaction.  A DESIGN node is not placed on the canvas -- the design IS the
-- canvas -- so no design label ever needs a geometry row, and the UNIQUE below
-- can safely be the only key.  If a later wave ever needs to place a DESIGN
-- node, the two grains must be split, because they would then collide on the
-- unique key.  No CHECK enforces the exclusivity: NCE stores what Copper sends
-- and a structural refusal here would be a contract NCE has not agreed.
--
-- ============================================================================
-- UNITS AND AXES ARE NORMATIVE (Rev 2 section 4)
-- ============================================================================
-- x / y are CANVAS GRID UNITS.  The origin is TOP-LEFT and y increases
-- DOWNWARD (y-down).  NCE stores these numbers and converts nothing;
-- exporters convert.
--
-- Room dimensions do NOT live in x/y.  They live in meta, under the reserved
-- keys copper.room.w / copper.room.d / copper.room.h, and they are in METERS.
--
-- ============================================================================
-- NAMING IS CONTRACTUAL
-- ============================================================================
-- rack_position and rack_face carry the NetBox vocabulary -- "position" and
-- "face" -- which Copper follows as a binding ADR.  Renaming either column
-- breaks Copper.  They are not to be renamed to slot/side/orientation or
-- anything else.
--
-- rack_position is NUMERIC(4,1) because a rack unit is a half-U grid in
-- practice: 0.0, 0.5, 1.0, 1.5 ... up to 999.5.  rack_face is 'front' |
-- 'rear', matching NetBox's face vocabulary exactly.
--
-- 999.5, NOT 999.9: the column can hold 999.9, but 999.9 is not a multiple
-- of 0.5 and the half-U rule below refuses it, so 999.5 is the largest
-- value a caller can actually send. An earlier revision of this comment
-- advertised 999.9 as legal while the code rejected it.
--
-- WHERE THE HALF-U RULE IS ENFORCED: in validate_geometry()
-- (nce/vertical_modules/system_design/geometry.py), at the write boundary,
-- NOT by this column. NUMERIC(4,1) only guarantees ONE DECIMAL PLACE: it
-- accepts 1.27 and silently stores 1.3, which is not a half-U either and
-- which the writer is never told about. Every write that goes through the
-- authoring surfaces is refused with a 422 instead. A direct INSERT (psql,
-- a repair script) still gets the silent round -- stated here rather than
-- left for the next reader to discover.
--
-- ============================================================================
-- RLS
-- ============================================================================
-- ENABLE + FORCE ROW LEVEL SECURITY with tenant_isolation_policy scoped by
-- get_nce_namespace() -- mirrors migration 039
-- (system_design_device_capabilities), this engine's sibling side-table.
--
-- FORCE RLS is not on its own the tenant boundary for this table: the pools
-- that serve requests are owner pools and bypass it.  The boundary is the
-- explicit namespace_id = $n::uuid predicate carried by every query in
-- nce/vertical_modules/system_design/geometry.py.  FORCE RLS is the backstop.
--
-- ============================================================================
-- EVERY CHECK AND UNIQUE IS EXPLICITLY NAMED
-- ============================================================================
-- An anonymous column CHECK is auto-named by PostgreSQL and the auto-name
-- differs between the two install paths (schema.sql on a fresh database vs
-- this migration on an existing one), which is a catalog divergence that has
-- already caused one rejection (Batch 132).  Every constraint below is named.
--
-- Idempotent DDL throughout (IF NOT EXISTS / DO $$ ... $$).
-- MIRROR OF the block at the end of nce/schema.sql -- the DDL statements below
-- are byte-identical to that file's.
-- ============================================================================

CREATE TABLE IF NOT EXISTS system_design_geometry (
    id              UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id    UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,

    -- Grain key.  A NODE label for a geometry row; the DESIGN label for the
    -- one version row.  See the file header.
    node_label      TEXT        NOT NULL,

    -- Canvas placement.  Grid units, origin top-left, y-down (Rev 2 section 4).
    x               NUMERIC,
    y               NUMERIC,

    -- Rack elevation (NetBox vocabulary -- do not rename).
    -- Half-U granularity: 0.0, 0.5, 1.0 ... 999.5 -- enforced by
    -- validate_geometry(), not by this column (see the file header).
    -- 999.9 fits the column but is NOT a legal value: it is not a half-U.
    rack_position   NUMERIC(4,1),
    rack_face       TEXT,

    -- Cable run.  Length in METERS; cable_type is free text (Cat6A, OM4, ...)
    -- and is deliberately not enumerated -- the vocabulary is the installer's.
    cable_length_m  NUMERIC,
    cable_type      TEXT,

    -- Escape hatch.  Room dimensions live here under copper.room.w/d/h in
    -- METERS.  Reserved copper.* keys are stored verbatim and interpreted by
    -- Copper, never by NCE (Rev 2 section 5).
    meta            JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Per-DESIGN optimistic-concurrency token.  NULL on every geometry row;
    -- set only on the design version row.  Monotonic, starts at 0 when the
    -- row is created and is incremented by every authoring write.
    version         BIGINT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT system_design_geometry_pkey PRIMARY KEY (id),
    CONSTRAINT system_design_geometry_ns_node_uq UNIQUE (namespace_id, node_label),
    CONSTRAINT system_design_geometry_node_label_not_blank
        CHECK (btrim(node_label) <> ''),
    CONSTRAINT system_design_geometry_rack_face_check
        CHECK (rack_face IS NULL OR rack_face IN ('front', 'rear')),
    -- Every stored coordinate must be a REAL number.
    --
    -- NOT written as `x = x`. That idiom catches NaN for IEEE floats but is a
    -- NO-OP on NUMERIC: PostgreSQL defines NUMERIC 'NaN' = 'NaN' as TRUE so
    -- that NaN sorts and groups deterministically. Verified on PG 16.14.
    -- The three special values are therefore excluded by name.
    --
    -- validate_geometry() already refuses these at the write boundary; this
    -- is the structural backstop for a writer that does not go through it --
    -- psql, a repair script, a future core. A stored NaN cannot be undone
    -- (there is no delete path) and makes the WHOLE design's topology
    -- response raise for every reader, so it is worth a constraint.
    CONSTRAINT system_design_geometry_numerics_finite
        CHECK (
            (x IS NULL OR x NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))
            AND (y IS NULL OR y NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))
            AND (rack_position IS NULL OR rack_position NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))
            AND (cable_length_m IS NULL OR cable_length_m NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))
        ),
    -- And it must survive the JSON round trip.
    --
    -- NUMERIC stores 10^400 happily; the read path converts back with
    -- float(Decimal), which does NOT raise for an over-large Decimal -- it
    -- returns inf -- and JSONResponse.render (allow_nan=False) then raises on
    -- that. So a merely LARGE finite value poisons a design exactly as NaN
    -- does. The bound is the IEEE double maximum.
    --
    -- It is spelled as the EXACT 309-digit expansion of that double, not as
    -- the familiar 1.7976931348623157e308, and the difference is load-bearing:
    -- the short form is the 17-digit ROUNDED decimal and is strictly SMALLER
    -- than the real maximum. Using it here put every value in the gap into a
    -- state the application accepted and the database refused -- a
    -- CheckViolationError, i.e. a 500, which is the exact defect class this
    -- constraint exists to prevent. Caught by the test below before it shipped.
    --
    -- The application bound is Decimal(sys.float_info.max) -- this same exact
    -- value. Agreement between the two is NOT automatic just because the
    -- numbers match: an earlier revision claimed here that "the two agree on
    -- every input" while the application compared with Python's abs(), which
    -- ROUNDS a Decimal to the context precision (28 significant digits against
    -- this value's 309) and so accepted a ~1.8e280-wide band of values above
    -- the true maximum -- which this constraint then refused as a 500. The
    -- application now compares with Decimal.copy_abs(), which does no
    -- rounding. THAT is what makes the two agree, and it is a property of the
    -- comparison, not of the constant: anything here that reintroduces a
    -- rounding operation on either side reopens the gap.
    --
    -- This is a serialisation limit expressed in the schema, which is a real
    -- trade-off: a future consumer reading NUMERIC natively would not need
    -- it. It is here anyway because EVERY consumer of this table today goes
    -- through do_get_topology's JSON, and a silent 500 for every reader of a
    -- design is worse than a visible ALTER TABLE for the wave that one day
    -- needs bigger numbers.
    CONSTRAINT system_design_geometry_numerics_in_double_range
        CHECK (
            (x IS NULL OR abs(x) <= '179769313486231570814527423731704356798070567525844996598917476803157260780028538760589558632766878171540458953514382464234321326889464182768467546703537516986049910576551282076245490090389328944075868508455133942304583236903222948165808559332123348274797826204144723168738177180919299881250404026184124858368'::numeric)
            AND (y IS NULL OR abs(y) <= '179769313486231570814527423731704356798070567525844996598917476803157260780028538760589558632766878171540458953514382464234321326889464182768467546703537516986049910576551282076245490090389328944075868508455133942304583236903222948165808559332123348274797826204144723168738177180919299881250404026184124858368'::numeric)
            AND (cable_length_m IS NULL OR abs(cable_length_m) <= '179769313486231570814527423731704356798070567525844996598917476803157260780028538760589558632766878171540458953514382464234321326889464182768467546703537516986049910576551282076245490090389328944075868508455133942304583236903222948165808559332123348274797826204144723168738177180919299881250404026184124858368'::numeric)
        ),
    CONSTRAINT system_design_geometry_version_non_negative
        CHECK (version IS NULL OR version >= 0)
);

-- Index: the primary read path -- geometry for a batch of node labels within
-- one namespace, and the single-row version lookup, are the same shape.
-- (namespace_id, node_label) is already unique-indexed by the UNIQUE above;
-- no second index is added for it.

-- Row-Level Security.
ALTER TABLE system_design_geometry ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_design_geometry FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON system_design_geometry;
CREATE POLICY tenant_isolation_policy ON system_design_geometry
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

-- Application role grants.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE system_design_geometry FROM nce_app;
        -- UPDATE is required: geometry is re-authored in place on every canvas
        -- save, and the version row is incremented rather than appended.
        -- DELETE is granted for parity with the sibling capability table
        -- (migration 039); no code in this wave issues one.
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE system_design_geometry TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE system_design_geometry IS
'Canvas geometry AND the per-DESIGN optimistic-concurrency token for the System
Design engine (Module 6, Wave 14).

TWO KEY GRAINS IN ONE TABLE -- deliberate, and the only exception in this
engine. Most rows are GEOMETRY rows keyed by a NODE label (DEVICE:/PORT:/RACK:/
CABLE:/FL:) carrying x/y, rack_position/rack_face, cable_length_m/cable_type
and meta, with version NULL. Exactly one row per design is the DESIGN VERSION
row, keyed by the DESIGN label (DESIGN:<ID>), carrying version and no geometry.
Distinguish them by version IS NOT NULL (equivalently node_label LIKE
''DESIGN:%''); a geometry row is any row with version NULL. They share a table
because they share the natural key, the tenancy boundary, the lifecycle and the
writer transaction, and because a DESIGN node is never placed on the canvas so
the two grains cannot collide on (namespace_id, node_label).

UNITS AND AXES ARE NORMATIVE (Rev 2 section 4): x/y are CANVAS GRID UNITS with
the origin TOP-LEFT and y increasing DOWNWARD. NCE converts nothing; exporters
convert. Room dimensions are NOT x/y -- they live in meta under
copper.room.w / copper.room.d / copper.room.h, in METERS.

NAMING IS CONTRACTUAL: rack_position and rack_face carry the NetBox vocabulary
(position/face) that Copper follows as a binding ADR. Renaming either breaks
Copper.

version is monotonic per design, starts at 0 and is incremented inside the
authoring write''s own transaction. A caller that supplies expected_version
gets a compare-and-swap; a caller that omits it gets last-writer-wins and the
increment still happens.

SCOPE OF THAT PROMISE: it covers writes made through the two authoring
adapters in nce/vertical_modules/system_design/mcp_handlers.py -- the
system_design_author_topology and system_design_author_functional_location
tools and their REST twins -- and ONLY those. Three other modules under
system_design/ write kg_nodes/kg_edges for a design without passing through
them and never move the token: from_quote.py, to_quote.py and
netbox_bridge.py. All three are unwired today (no non-test callers), so this
is latent rather than exploitable -- but it is not hypothetical, because
read.py''s edge projection filters on subject_label only, so to_quote.py''s
DESIGN -[becomes]-> QUOTE edge would appear in a topology read while version
stood still. Do not read this token as covering every change to a design.

FORCE RLS isolates per tenant, but the pools that serve requests are owner
pools and bypass it -- the real boundary is the explicit namespace_id predicate
in nce/vertical_modules/system_design/geometry.py.';

COMMENT ON COLUMN system_design_geometry.node_label IS
'GRAIN KEY. A node label (DEVICE:/PORT:/RACK:/CABLE:/FL:) on a geometry row;
the DESIGN label on the one version row. Matches kg_nodes.label for geometry
rows.';

COMMENT ON COLUMN system_design_geometry.x IS
'Canvas X in GRID UNITS. Origin TOP-LEFT (Rev 2 section 4). Not meters.';

COMMENT ON COLUMN system_design_geometry.y IS
'Canvas Y in GRID UNITS, increasing DOWNWARD (y-down), origin TOP-LEFT
(Rev 2 section 4). Not meters.';

COMMENT ON COLUMN system_design_geometry.rack_position IS
'NetBox "position": the lowest rack unit this device occupies. NUMERIC(4,1)
carries one decimal place; the HALF-U STEP (a multiple of 0.5) is enforced by
validate_geometry() at the write boundary, NOT by this column -- a direct
INSERT of 1.27 is still rounded to 1.3 silently. Legal range is 0.0 to 999.5
in 0.5 steps; 999.9 fits the column but is not a half-U and is refused.
CONTRACTUAL NAME -- renaming breaks Copper.';

COMMENT ON COLUMN system_design_geometry.rack_face IS
'NetBox "face": ''front'' or ''rear''. CONTRACTUAL NAME AND VOCABULARY --
renaming or extending breaks Copper.';

COMMENT ON COLUMN system_design_geometry.cable_length_m IS
'Cable run length in METERS.';

COMMENT ON COLUMN system_design_geometry.meta IS
'Verbatim passthrough store. Room dimensions live here under copper.room.w /
copper.room.d / copper.room.h, in METERS. NCE stores, Copper interprets
(Rev 2 section 5).';

COMMENT ON COLUMN system_design_geometry.version IS
'Per-DESIGN optimistic-concurrency token. NULL on every geometry row; set only
on the design version row, where it starts at 0 and is incremented by every
authoring write inside that write''s own transaction. Its presence is the
discriminator between the two key grains.';
