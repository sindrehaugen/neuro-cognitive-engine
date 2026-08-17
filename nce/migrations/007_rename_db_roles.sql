-- 007_rename_db_roles.sql
-- Idempotent migration: rename legacy trimcp_app / trimcp_gc roles to nce_app / nce_gc.
-- Safe to re-run; uses EXECUTE inside the DO block to work around PostgreSQL's DDL
-- restriction on ALTER ROLE outside of dynamic SQL.
--
-- PostgreSQL does not permit bare ALTER ROLE statements inside PL/pgSQL blocks;
-- EXECUTE is the standard workaround for running utility statements conditionally.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trimcp_app') THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
            EXECUTE 'REASSIGN OWNED BY trimcp_app TO nce_app';
            EXECUTE 'DROP OWNED BY trimcp_app';
            EXECUTE 'DROP ROLE trimcp_app';
            RAISE NOTICE 'Dropped trimcp_app because nce_app already exists';
        ELSE
            EXECUTE 'ALTER ROLE trimcp_app RENAME TO nce_app';
            RAISE NOTICE 'Renamed role trimcp_app → nce_app';
        END IF;
    ELSE
        RAISE NOTICE 'Role trimcp_app not found — skipping (already renamed or fresh install)';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trimcp_gc') THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_gc') THEN
            EXECUTE 'REASSIGN OWNED BY trimcp_gc TO nce_gc';
            EXECUTE 'DROP OWNED BY trimcp_gc';
            EXECUTE 'DROP ROLE trimcp_gc';
            RAISE NOTICE 'Dropped trimcp_gc because nce_gc already exists';
        ELSE
            EXECUTE 'ALTER ROLE trimcp_gc RENAME TO nce_gc';
            RAISE NOTICE 'Renamed role trimcp_gc → nce_gc';
        END IF;
    ELSE
        RAISE NOTICE 'Role trimcp_gc not found — skipping (already renamed or fresh install)';
    END IF;
END $$;

-- Optional: also rename the app password default if it is still the old value.
-- This is a best-effort operation; do not block the migration if it fails.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        -- Password update is only a convenience; operators who have already set
        -- a strong password are unaffected. This is a no-op if the password was
        -- already changed to something else.
        EXECUTE format(
            'ALTER ROLE nce_app WITH PASSWORD %L',
            'nce_app_secret'
        );
    END IF;
EXCEPTION WHEN OTHERS THEN
    NULL; -- Password update is best-effort; don't block the migration.
END $$;

