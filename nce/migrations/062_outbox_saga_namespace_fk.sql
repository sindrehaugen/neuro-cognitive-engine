-- 062_outbox_saga_namespace_fk.sql
--
-- Give outbox_events and saga_execution_log the foreign key to namespaces they
-- never had. Debt item D1, extended by TD-1's new catalog ratchet.
--
-- Migration 055 made every tenant-scoped FK to namespaces ON DELETE CASCADE so
-- a tenant could actually be deleted. It could not fix these two, because they
-- have no FK to namespaces AT ALL -- a table with no constraint never enters
-- the catalog query 055 and its ratchet operate on, so both were invisible to
-- it. TD-1 closed that blind spot by checking the required half
-- (EXPECTED_TENANT_RLS_TABLES) rather than only the allowlist half, and these
-- two tables are what it found: 2 of 64 tenant-scoped RLS tables, each with a
-- NOT NULL namespace_id that references nothing.
--
-- Why it matters beyond tidiness (D1's recorded downstream effect): an orphaned
-- outbox row survives its tenant's deletion, and the next relay pass that tries
-- to dead-letter it raises ForeignKeyViolationError -- because
-- dead_letter_queue.namespace_id IS ON DELETE CASCADE, so the namespace it
-- needs is already gone -- which ABORTS THE WHOLE PASS. One deleted tenant can
-- therefore stall event delivery for every other tenant.
--
-- ON DELETE semantics -- decided, not defaulted:
--
--   outbox_events      -- CASCADE. Undelivered events belong to a tenant that
--       no longer exists; relaying them onward after deletion is useless at
--       best and a data-protection problem at worst. RESTRICT would make
--       tenant deletion fail whenever any unpublished row exists, which is
--       precisely the pre-055 defect ("nothing could delete a tenant") being
--       reintroduced on a new table. The sibling table dead_letter_queue is
--       already ON DELETE CASCADE, so CASCADE is also the consistent choice.
--
--   saga_execution_log -- CASCADE. This is operational recovery state, not an
--       audit trail: payload is documented as "enough to re-drive rollback",
--       the row carries a mutable state machine (started / pg_committed /
--       completed / rolled_back / recovery_needed) and an updated_at, and the
--       partial index targets the in-flight states. So audit_log's retention
--       argument for staying NO ACTION does not transfer. A recovery_needed
--       saga whose namespace is gone can never complete; leaving it orphaned
--       means the recovery sweeper repeatedly picks up work that cannot
--       succeed -- the same defect shape as the outbox relay above.
--
-- Both tables therefore stay OUT of EXPECTED_NON_CASCADE, and their entries in
-- TENANT_TABLES_WITHOUT_NAMESPACE_FK are removed in this same commit. That is
-- not optional: TD-1's ratchet asserts that a table listed as having no FK
-- which now HAS one must be dropped from the allowlist, so leaving the entries
-- would fail the gate. A ratchet that passes because you widened the schema is
-- the same defect in a new coat.
--
-- PRE-EXISTING ORPHANS -- this migration DELETES DATA, deliberately.
--
-- ADD CONSTRAINT validates existing rows, so it fails outright while orphans
-- exist. On the shared dev database on 2026-08-31 this migration purged:
--
--     outbox_events        21 orphaned row(s)
--     saga_execution_log   22 orphaned row(s)
--
-- Do NOT read those as production estimates. An earlier count on the same
-- database read 342 orphaned outbox rows out of 2852 total, but it was taken
-- while the test suite was running: most of that was transient test data, and
-- outbox_events settled at 1 row once the run finished. The shared dev
-- database is not a population sample -- re-count on the target before
-- applying this anywhere that matters.
--
-- Those rows are purged below. An orphan is unreachable and unprocessable by
-- construction: FORCE ROW LEVEL SECURITY plus a tenant policy means no tenant
-- session can ever select it, and per D1 the relay's attempt to dead-letter it
-- is what aborts delivery for everyone. Keeping them has no upside and one
-- large downside. The alternative -- ADD CONSTRAINT ... NOT VALID -- would
-- leave the D1 defect live indefinitely behind a constraint that looks present,
-- which is worse than either fixing or not fixing it.
--
-- The purge emits its row counts as NOTICEs. Read them: on production they are
-- the record of what this migration destroyed, and they should be reviewed
-- before this is applied there.
--
-- NEW LOCK CONTENTION, and it is the point rather than a side effect. This
-- migration puts outbox_events into the namespace-DELETE cascade set for the
-- FIRST time. In PostgreSQL an FK insert takes FOR KEY SHARE on the parent
-- namespaces row, and DELETE FROM namespaces takes FOR UPDATE on it -- those
-- two conflict. So from here on:
--
--   * deleting a tenant WAITS for in-flight transactions that are inserting
--     outbox rows for that tenant, and
--   * those writers wait for a tenant deletion already in progress.
--
-- That is correct -- it is exactly what makes tenant deletion consistent, and
-- what closes D1 -- but it is a real change in lock dynamics that shows up
-- during tenant deprovisioning, not in a unit test. The namespace_id index
-- added above reduces the cascade SCAN cost; it does not reduce this
-- contention. Deprovision a busy tenant when its writers are quiet, and do
-- not hold a namespaces DELETE open across other work.
--
-- Idempotent: the purge is a no-op once no orphans remain, and each constraint
-- and index is created only when absent.

DO $$
DECLARE
    purged_outbox BIGINT;
    purged_saga   BIGINT;
BEGIN
    ----------------------------------------------------------------------
    -- 1. Purge pre-existing orphans (see header -- this deletes data).
    ----------------------------------------------------------------------
    DELETE FROM outbox_events x
     WHERE NOT EXISTS (SELECT 1 FROM namespaces n WHERE n.id = x.namespace_id);
    GET DIAGNOSTICS purged_outbox = ROW_COUNT;

    DELETE FROM saga_execution_log x
     WHERE NOT EXISTS (SELECT 1 FROM namespaces n WHERE n.id = x.namespace_id);
    GET DIAGNOSTICS purged_saga = ROW_COUNT;

    RAISE NOTICE '062: purged % orphaned outbox_events row(s) and % orphaned saga_execution_log row(s) before adding the FKs',
                 purged_outbox, purged_saga;

    ----------------------------------------------------------------------
    -- 2. Supporting indexes. 59 of the 62 cascading tables already carry a
    --    leading namespace_id index; without one, every parent DELETE
    --    sequentially scans the child to find its cascade targets.
    ----------------------------------------------------------------------
    CREATE INDEX IF NOT EXISTS idx_outbox_events_namespace_id
        ON outbox_events (namespace_id);
    CREATE INDEX IF NOT EXISTS idx_saga_execution_log_namespace_id
        ON saga_execution_log (namespace_id);

    ----------------------------------------------------------------------
    -- 3. The foreign keys.
    ----------------------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'outbox_events_namespace_id_fkey'
           AND conrelid = 'outbox_events'::regclass
    ) THEN
        ALTER TABLE outbox_events
            ADD CONSTRAINT outbox_events_namespace_id_fkey
            FOREIGN KEY (namespace_id) REFERENCES namespaces(id) ON DELETE CASCADE;
        RAISE NOTICE '062: added outbox_events_namespace_id_fkey (ON DELETE CASCADE)';
    ELSE
        RAISE NOTICE '062: outbox_events_namespace_id_fkey already present';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'saga_execution_log_namespace_id_fkey'
           AND conrelid = 'saga_execution_log'::regclass
    ) THEN
        ALTER TABLE saga_execution_log
            ADD CONSTRAINT saga_execution_log_namespace_id_fkey
            FOREIGN KEY (namespace_id) REFERENCES namespaces(id) ON DELETE CASCADE;
        RAISE NOTICE '062: added saga_execution_log_namespace_id_fkey (ON DELETE CASCADE)';
    ELSE
        RAISE NOTICE '062: saga_execution_log_namespace_id_fkey already present';
    END IF;
END
$$;
