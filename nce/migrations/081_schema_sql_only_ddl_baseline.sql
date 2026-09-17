-- 081_schema_sql_only_ddl_baseline.sql
--
-- Q-4 step 1: give the ledger a path to the DDL that only ``schema.sql`` provides.
--
-- WHY THIS EXISTS
-- ---------------
-- ``NCEEngine.connect()`` runs two schema writers (``orchestrator.py:205-206``): the whole
-- of ``nce/schema.sql``, then the numbered migration chain with its ``applied_migrations``
-- ledger.
--
-- It is easy to assume ``schema.sql`` is a dead second writer whose edits never reach a
-- live database. That is false. Its unguarded ``ADD COLUMN`` statements sit inside
-- ``DO $$`` blocks that probe ``information_schema`` first, so it detects missing columns
-- and adds them -- on every connect. ``schema.sql`` is a migration runner. It is an
-- *unledgered* one, and that is the actual defect: nothing can state what schema version a
-- live database is at, because one of the two writers keeps no record.
--
-- Measured on the live database 2026-09-17:
--     SELECT count(*), max(filename) FROM applied_migrations;  ->  74 | 078_...
-- while all ten columns below were present. The ledger had never heard of them.
--
-- WHAT THIS MIGRATION DOES
-- ------------------------
-- It makes those ten columns reachable *through the ledger*. Today every one of them is a
-- no-op on any database that has connected recently, because ``schema.sql`` already applied
-- them. That is the point: it is a baseline, not a change.
--
-- It matters for Q-4 step 2. Once ``_init_pg_schema`` becomes bootstrap-only -- running only
-- when ``applied_migrations`` is empty -- an *existing* database stops receiving anything
-- that lives solely in ``schema.sql``. Without this migration, a database missing one of
-- these columns would have no path to it and would fail at query time instead of at
-- migration time. Step 2 must not land before this has.
--
-- SCOPE, STATED HONESTLY
-- ----------------------
-- Columns only. The core tables (``memories``, ``kg_nodes``, ``kg_edges``, ``event_log``,
-- ``namespaces``) are created in ``schema.sql`` and in ZERO migrations, and this migration
-- deliberately does not try to reproduce 127 CREATE TABLE statements. ``schema.sql`` remains
-- the bootstrap for an empty database; that is the intended model and step 2 does not change
-- it. What step 2 changes is that it stops being a silent patcher of populated ones.
--
-- Verify with:  python scripts/check_schema_drift.py --dsn "$DSN"

BEGIN;

-- --------------------------------------------------------------------------
-- kg_nodes / kg_edges: tenant scoping and embedding provenance
--
-- The namespace_id columns are the tenant-isolation keys. In schema.sql they arrive with a
-- backfill into a '_global_legacy' namespace; that backfill is reproduced below so a
-- database reaching this migration ends in the same state, not a NULL-bearing one.
-- --------------------------------------------------------------------------
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS namespace_id UUID;
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS embedding_model_id UUID;
ALTER TABLE kg_edges ADD COLUMN IF NOT EXISTS namespace_id UUID;

INSERT INTO namespaces (slug, metadata)
VALUES ('_global_legacy', '{"description":"Fallback namespace for pre-RLS KG data"}'::jsonb)
ON CONFLICT (slug) DO NOTHING;

UPDATE kg_nodes
   SET namespace_id = (SELECT id FROM namespaces WHERE slug = '_global_legacy')
 WHERE namespace_id IS NULL;

UPDATE kg_edges
   SET namespace_id = (SELECT id FROM namespaces WHERE slug = '_global_legacy')
 WHERE namespace_id IS NULL;

-- --------------------------------------------------------------------------
-- memories: free-form metadata
-- --------------------------------------------------------------------------
ALTER TABLE memories
  ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

-- --------------------------------------------------------------------------
-- consolidation_runs: run outcome columns
-- --------------------------------------------------------------------------
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS completed_at          TIMESTAMPTZ;
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS events_processed      INTEGER;
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS clusters_formed       INTEGER;
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS abstractions_created  INTEGER;
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS error_message         TEXT;

-- --------------------------------------------------------------------------
-- bridge_subscriptions: encrypted OAuth access token
--
-- BYTEA because the value is envelope-encrypted before it is stored; it is never a string
-- at rest. Named here so a reader does not "helpfully" widen it to TEXT later.
-- --------------------------------------------------------------------------
ALTER TABLE bridge_subscriptions
  ADD COLUMN IF NOT EXISTS oauth_access_token_enc BYTEA;

COMMIT;
