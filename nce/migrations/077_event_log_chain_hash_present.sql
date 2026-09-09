-- 077_event_log_chain_hash_present.sql
-- ============================================================================
-- Require a Merkle chain hash on every NEW event_log row.
--
-- WHY
-- ---
-- `event_log` declares `signature` and `signature_key_id` NOT NULL but leaves
-- `chain_hash` nullable. So the tamper-evidence surface was half-enforced: a direct
-- INSERT could write a signed event with no chain hash, and the Merkle verifier would
-- then report that namespace corrupt forever.
--
-- That is not hypothetical. On 2026-09-07 namespace
-- wormpinned-teardown-probe-76199e4f98c8 received exactly one such row --
-- `event_seq=1`, `chain_hash IS NULL`, `agent="a"`, written directly rather than through
-- `append_event` -- and by 2026-09-09 the chain verifier had raised a CRITICAL
-- "tampering detected" alert on every tick for two days and appended eight
-- `chain_verification_failed` events into the very chain it had declared broken (fixed
-- separately in #112).
--
-- Measured before writing this migration, on the deployed database:
--   * an INSERT with `chain_hash = NULL` SUCCEEDS today  ("INSERT 0 1")
--   * with this constraint present it is rejected:
--       new row for relation "event_log_2026_09" violates check constraint
--       "event_log_chain_hash_present"
--   * exactly ONE existing row violates it -- hence NOT VALID.
--
-- NOT VALID is deliberate
-- -----------------------
-- The one offending row CANNOT be removed: `event_log` carries a WORM trigger
-- (`trg_event_log_worm`, "event_log is immutable (WORM). DELETE operation is forbidden.")
-- which fires even for a superuser, and `namespaces` cannot be deleted either because
-- `event_log_namespace_id_fkey` still references it. Both were verified, and neither
-- should be bypassed to satisfy a constraint. NOT VALID enforces the rule for every new
-- row while tolerating the historical one, which is the correct trade for an append-only
-- ledger: the constraint protects the future without rewriting the past.
--
-- Do NOT later run `VALIDATE CONSTRAINT` expecting it to pass -- it will fail on that
-- row until the row is legitimately removable, which under WORM it never is.
--
-- WHAT THIS DOES NOT FIX
-- ---------------------
-- `(namespace_id, event_seq)` uniqueness. `event_log` is RANGE partitioned on
-- `occurred_at`, and Postgres requires every unique index on a partitioned table to
-- include the partition key -- so the existing
-- `event_log_namespace_id_event_seq_occurred_at_key` on
-- `(namespace_id, event_seq, occurred_at)` is the strongest constraint the schema permits.
-- `event_seq` can therefore still repeat within a namespace at different `occurred_at`,
-- which is precisely how `event_seq=1` came to exist twice in that namespace. Closing
-- that needs application-level allocation, not DDL: `append_event` already allocates
-- safely via the `event_sequences` upsert, so the real gap is that nothing forces writers
-- through `append_event`. Tracked separately.
-- ============================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM   pg_constraint
        WHERE  conrelid = 'event_log'::regclass
          AND  conname  = 'event_log_chain_hash_present'
    ) THEN
        ALTER TABLE event_log
            ADD CONSTRAINT event_log_chain_hash_present
            CHECK (chain_hash IS NOT NULL) NOT VALID;
    END IF;
END
$$;
