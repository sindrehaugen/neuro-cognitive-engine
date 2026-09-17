-- 080_worm_truncate_guard.sql — RL-H18
--
-- The WORM guard could not stop a TRUNCATE.
--
-- `trg_event_log_worm` and `trg_event_parents_worm` are declared
-- `BEFORE UPDATE OR DELETE ... FOR EACH ROW`. A row-level trigger is invoked once
-- per affected row, and TRUNCATE does not visit rows — it deallocates the table's
-- storage. So it fires no row trigger, and both append-only, hash-chained tables
-- could be emptied in one statement by anything holding TRUNCATE rights, leaving
-- the guard silent and the audit trail gone.
--
-- This is not theoretical for this deployment: the connecting role (`mcp_user`) is
-- `rolsuper`/`rolbypassrls`, so it holds TRUNCATE on both tables and RLS does not
-- constrain it.
--
-- PostgreSQL supports a statement-level TRUNCATE trigger, which is the only shape
-- that can refuse this. `prevent_mutation()` already raises with `TG_OP` in the
-- message, so it reports "TRUNCATE operation is forbidden" unchanged — no function
-- change is needed.
--
-- Idempotent: guarded on pg_trigger, like the row triggers in schema.sql.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_event_log_worm_truncate'
    ) THEN
        CREATE TRIGGER trg_event_log_worm_truncate
            BEFORE TRUNCATE ON event_log
            FOR EACH STATEMENT EXECUTE FUNCTION prevent_mutation();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_event_parents_worm_truncate'
    ) THEN
        CREATE TRIGGER trg_event_parents_worm_truncate
            BEFORE TRUNCATE ON event_parents
            FOR EACH STATEMENT EXECUTE FUNCTION prevent_mutation();
    END IF;
END $$;
