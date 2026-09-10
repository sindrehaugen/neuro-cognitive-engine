-- 078_event_log_chain_hash_validate.sql
-- ============================================================================
-- Promote `event_log_chain_hash_present` from NOT VALID to a validated constraint.
--
-- WHY THIS IS NOW POSSIBLE, AND WAS NOT WHEN 077 WAS WRITTEN
-- ----------------------------------------------------------
-- 077 added the CHECK as NOT VALID because exactly one existing row violated it, and
-- said so plainly: "Do NOT later run `VALIDATE CONSTRAINT` expecting it to pass -- it
-- will fail on that row until the row is legitimately removable, which under WORM it
-- never is."
--
-- That was true and correctly reasoned when written. It is no longer true. On 2026-09-10
-- the offending row was removed with Sindre's explicit authorisation, after the live
-- rebuild found that the row was still breaking Merkle verification on every cron tick.
-- It was test litter: namespace `a148f2aa-956c-42d1-92b4-45af06a09e88`, `event_seq=1`,
-- `chain_hash IS NULL`, `agent_id='a'` -- written straight into `event_log` by a fixture
-- that bypassed `append_event`, so no sequence was allocated and no chain hash computed.
-- Removing it required temporarily disabling `trg_event_log_worm`, which is normally
-- forbidden; it was done in a single transaction that re-armed the trigger before
-- committing, and WORM was then proved live again by confirming a DELETE still raises.
--
-- 🔴 DO NOT EDIT 077 TO CORRECT ITS COMMENT. `applied_migrations` records a content
-- checksum per file (`nce/migration_ledger.py`), so editing an applied migration presents
-- as a changed migration. 077 is idempotent (`IF NOT EXISTS`) so a re-apply would be
-- harmless, but the ledger churn is pointless. This migration supersedes that note; the
-- historical record stays as it was written.
--
-- WHAT VALIDATION BUYS
-- --------------------
-- Until now the rule was enforced for new rows only, and the constraint's presence was
-- documentation of an intent that the database had never actually checked. After this,
-- PostgreSQL has verified every existing row and the constraint is a real invariant the
-- planner may rely on. Measured immediately before writing this: **zero** rows in the
-- estate have a NULL `chain_hash`, and **zero** `(namespace_id, event_seq)` pairs are
-- duplicated.
--
-- WHAT THIS STILL DOES NOT FIX
-- ----------------------------
-- `(namespace_id, event_seq)` uniqueness -- the same limit 077 named. `event_log` is RANGE
-- partitioned on `occurred_at` and PostgreSQL requires every unique index on a partitioned
-- table to include the partition key, so a duplicate `event_seq` at a different
-- `occurred_at` is still insertable at the database level. The estate currently has none,
-- and the code path that produced the one it did have -- a direct `INSERT INTO event_log`
-- bypassing `append_event` -- is now a test failure rather than a review habit, pinned by
-- the chokepoint ratchet in `tests/unit/test_append_event_transaction_ratchet.py` (#128).
-- But that is a code guard, not a database guarantee. Do not read this migration as
-- closing the duplicate-sequence hole.
--
-- OPERATIONAL NOTES
-- -----------------
-- `VALIDATE CONSTRAINT` takes a SHARE UPDATE EXCLUSIVE lock and scans the table. It does
-- NOT block concurrent reads or writes. On this database that is 102 rows.
--
-- Proven on a throwaway restored from a snapshot of the live database, not on the live
-- database itself:
--   * before: `convalidated = false` on the parent and all five partitions
--   * VALIDATE on the PARENT recurses -- after: `convalidated = true` on all six
--   * running it a second time succeeds (idempotent)
--   * an INSERT with `chain_hash = NULL` is still rejected:
--       new row for relation "event_log_2026_09" violates check constraint
--       "event_log_chain_hash_present"
--   * row count unchanged at 102
-- ============================================================================

DO $$
BEGIN
    -- Guarded rather than unconditional: VALIDATE is itself idempotent, but skipping it
    -- when already validated avoids taking the lock and re-scanning on every deploy.
    IF EXISTS (
        SELECT 1
        FROM   pg_constraint
        WHERE  conrelid = 'event_log'::regclass
          AND  conname  = 'event_log_chain_hash_present'
          AND  NOT convalidated
    ) THEN
        ALTER TABLE event_log VALIDATE CONSTRAINT event_log_chain_hash_present;
    END IF;
END
$$;
