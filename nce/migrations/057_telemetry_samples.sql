-- 057_telemetry_samples.sql
-- ============================================================================
-- Assets engine (Module 9, Wave 5 -- telemetry-adapter): the `telemetry_samples`
-- table backing nce/vertical_modules/assets/telemetry.py's do_pull_telemetry.
-- One row per (asset, metric, sample instant) -- the high-write reading stream
-- that docs/vertical_engines/09-assets-engine.md's "Tables/migrations" section
-- specifies as `telemetry_samples (asset_id, namespace_id, metric, value,
-- sampled_at, raw jsonb)`.
--
-- MIGRATION NUMBER IS PRE-ALLOCATED (057), NOT DERIVED
-- --------------------------------------------------------------------------
-- Do not renumber this file by listing the directory. main holds up to 054;
-- 055 is Batch 132a's and 056 is Batch 144's, both in flight in other
-- worktrees and neither on main -- a directory listing would hand this file
-- 055 and collide with a number someone else already owns. Gaps are harmless:
-- there is no migration version ledger, schema.sql and every migrations/*.sql
-- re-run on every boot under an advisory lock, and all DDL here is
-- IF NOT EXISTS / DO $$ ... $$.
--
-- THIS WAVE WRITES NO GRAPH -- declared, not silently omitted
-- --------------------------------------------------------------------------
-- 09-assets-engine.md's do_pull_telemetry spec says the pull "writes TELEMETRY
-- nodes + monitored_by edges". THIS WAVE DOES NOT, by orchestrator decision:
-- a telemetry sample is a ROW IN THIS TABLE, not a kg_node and not a kg_edge.
-- No code in this wave writes kg_nodes or kg_edges, calls assert_owner, or
-- adds a row to nce/config_data/node-ownership.json -- so TELEMETRY is NOT a
-- registered node type and the deny-by-default ownership guard would
-- (correctly) refuse such a write today. The TELEMETRY node and the
-- ASSET -[monitored_by]-> TELEMETRY edge are a separate projection wave's.
-- Same shape as migration 054's own declaration for the ASSET node.
--
-- asset_id CARRIES A SINGLE-COLUMN FK, NOT A COMPOSITE ONE -- also declared
-- --------------------------------------------------------------------------
-- telemetry_samples_asset_fk is FOREIGN KEY (asset_id) REFERENCES assets (id).
-- It guarantees the asset EXISTS; it does NOT and cannot guarantee the asset
-- belongs to THIS row's namespace_id. The composite form used by migration
-- 052 (goods_receipts.location_id -> stock_locations) needs a
-- UNIQUE (id, namespace_id) on the referenced table. Migration 054's `assets`
-- has PRIMARY KEY (id) and UNIQUE (namespace_id, bom_line_id) -- no such key
-- -- and this wave does not add a constraint to another wave's table.
--
-- WHAT ACTUALLY BINDS A SAMPLE TO ITS TENANT, then, is two other things and
-- they are named here so nobody reads the FK as more than it is:
--   1. FORCE RLS below. A tenant can neither read a foreign namespace's rows
--      nor INSERT a row carrying a foreign namespace_id (the WITH CHECK).
--   2. do_pull_telemetry's own namespace-scoped existence pre-check, which
--      refuses an asset_id that is not visible in the caller's namespace.
-- The residual, accepted here: a caller that BYPASSES do_pull_telemetry can
-- still write a row in ITS OWN namespace pointing at another tenant's asset
-- row. That is a mislabelled reference inside one tenant, not a cross-tenant
-- read -- RLS still stops the leak. Closing it is a UNIQUE (id, namespace_id)
-- on `assets` plus a composite FK, which belongs to whichever wave owns that
-- table next. This wave neither adds it nor implies it is there.
--
-- IMMUTABLE OBSERVATIONS: INSERT-only, no UPDATE and no DELETE grant
-- --------------------------------------------------------------------------
-- A telemetry sample is a reading that was taken. Nothing in the application
-- may revise it (an UPDATE would silently rewrite the healthScore inputs) and
-- nothing here may erase it. Retention/downsampling on this "high-write
-- stream" is a real future need and will need its own DELETE grant; that is a
-- later wave's decision and is NOT provisioned forward here.
--
-- EVERY CHECK AND UNIQUE IS EXPLICITLY NAMED
-- --------------------------------------------------------------------------
-- An anonymous column CHECK is auto-named by PostgreSQL and the auto-name
-- depends on creation order, so a fresh install (schema.sql alone) and a
-- migrated install (previous schema.sql + this file) can agree on the
-- enforced EXPRESSION while differing in constraint IDENTITY. That precise
-- divergence caused a rejection on Batch 132. Every CHECK, UNIQUE and FK
-- below carries an explicit name, and the DDL here is byte-identical to the
-- block mirrored into nce/schema.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS telemetry_samples (
    id            UUID             NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID             NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    -- The asset this reading was taken from. Single-column FK -- see the file
    -- header for exactly what that does and does not guarantee.
    asset_id      UUID             NOT NULL,
    -- Vendor metric name, verbatim from the adapter (e.g. 'uptime_seconds').
    -- No enumerated CHECK: the metric vocabulary is whatever the manufacturer
    -- platform emits and differs per vendor, so freezing a list in DDL would
    -- make onboarding a new platform require a migration.
    metric        TEXT             NOT NULL,
    -- The reading. DOUBLE PRECISION because vendor telemetry is float-valued;
    -- the finite CHECK below is what keeps a NaN/Infinity out of the
    -- healthScore inputs a later wave will average over.
    value         DOUBLE PRECISION NOT NULL,
    -- The instant the VENDOR sampled it, not the instant we pulled it. This
    -- is a component of the idempotency key precisely because re-pulling an
    -- overlapping window must re-deliver the same instants (created_at is the
    -- pull time and is deliberately NOT in that key).
    sampled_at    TIMESTAMPTZ      NOT NULL,
    -- The adapter's untouched payload for this sample, so a later health
    -- writer can recover vendor fields this table does not model.
    raw           JSONB            NOT NULL DEFAULT '{}'::jsonb,
    change_origin TEXT             NOT NULL DEFAULT 'agent',
    created_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT telemetry_samples_asset_fk
        FOREIGN KEY (asset_id) REFERENCES assets (id) ON DELETE CASCADE,
    -- THE idempotency arbiter. A telemetry pull is a cron that re-reads
    -- overlapping windows, so the SAME reading arrives repeatedly by design;
    -- one (namespace, asset, metric, instant) is one row. Refused HERE, by
    -- the database, not by a Python "have I seen this?" pre-check -- two
    -- concurrent pulls would both pass such a pre-check and both insert.
    -- Precedent: migration 054's assets_ns_bom_line_uq.
    CONSTRAINT telemetry_samples_idempotency_uq
        UNIQUE (namespace_id, asset_id, metric, sampled_at),
    -- A blank metric name is not a metric, and must not be able to occupy the
    -- idempotency key.
    CONSTRAINT telemetry_samples_metric_not_blank
        CHECK (btrim(metric) <> ''),
    -- NaN and +/-Infinity are storable in DOUBLE PRECISION and would poison
    -- any average taken over this column. Note NaN is NOT caught by
    -- `value = value` in PostgreSQL -- unlike IEEE-754, PostgreSQL defines
    -- NaN = NaN as TRUE so its btree ordering is total -- so the comparison
    -- against 'NaN'::float8 below is the form that actually rejects it.
    CONSTRAINT telemetry_samples_value_finite
        CHECK (value <> 'NaN'::float8
               AND value <> 'Infinity'::float8
               AND value <> '-Infinity'::float8),
    CONSTRAINT telemetry_samples_change_origin_check
        CHECK (change_origin IN
            ('sync','webhook','agent','operator','consolidation','replay','unknown'))
);

-- No further index in this wave, deliberately. The one read this wave
-- performs -- "samples for this asset in this namespace" -- is already served
-- by the leading columns of the unique index behind
-- telemetry_samples_idempotency_uq. The "latest reading per metric" scan that
-- 09-assets-engine.md's do_compute_health will want is a
-- (namespace_id, asset_id, sampled_at DESC) index, and the wave that performs
-- that read owns it -- the same rule migration 054 applied to serial /
-- lifecycle_state.

ALTER TABLE telemetry_samples ENABLE ROW LEVEL SECURITY;
ALTER TABLE telemetry_samples FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON telemetry_samples;
CREATE POLICY tenant_isolation_policy ON telemetry_samples
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE telemetry_samples FROM nce_app;
        -- SELECT + INSERT only. No UPDATE: a reading that was taken is not
        -- revisable. No DELETE: nothing in the application may erase an
        -- observation, and retention is a later wave's explicit decision (see
        -- the file header). A namespace teardown still removes rows via the
        -- namespace_id FK's ON DELETE CASCADE, which RLS grants do not gate.
        GRANT SELECT, INSERT ON TABLE telemetry_samples TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE telemetry_samples IS
'Manufacturer-telemetry reading stream (Module 9, Wave 5 -- telemetry-adapter).
One row per (namespace, asset, metric, vendor sample instant):
do_pull_telemetry (nce/vertical_modules/assets/telemetry.py) is the SOLE
writer and is INSERT-only. Samples reach it through the TelemetryAdapter
interface; `mock` is the only adapter with real behaviour in this wave and the
five vendor platforms (crestron/qsys/neat/huddly/poly) are env-swap stubs
selected by NCE_ASSETS_TELEMETRY_<PLATFORM>_REAL that raise NotImplementedError
-- no vendor HTTP client, credential or dependency exists yet. Idempotency is
by DB constraint (telemetry_samples_idempotency_uq + INSERT ... ON CONFLICT DO
NOTHING), never a check-then-write, because a telemetry cron re-reads
overlapping windows by design; sampled_at is the VENDOR instant and created_at
the pull instant, and only the former is in the key. NO graph is written from
this table or its writer: the TELEMETRY kg_node and the
ASSET -[monitored_by]-> TELEMETRY edge are a later projection wave''s, and
TELEMETRY has no row in node-ownership.json. asset_id has a SINGLE-column FK
to assets(id) -- it proves the asset exists, NOT that it belongs to this
row''s namespace; that binding comes from FORCE RLS plus do_pull_telemetry''s
namespace-scoped pre-check, and closing the residual needs a
UNIQUE (id, namespace_id) on `assets` that migration 054 does not provide.
FORCE RLS isolates per tenant; nce_app is granted SELECT and INSERT only --
never UPDATE (a reading is not revisable) and never DELETE (retention is a
later wave''s decision).';
