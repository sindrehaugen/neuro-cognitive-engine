-- Migration: Create settings table for DB-backed runtime settings (V.1a)
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       JSONB,
    secret_enc  BYTEA,
    is_secret   BOOLEAN NOT NULL DEFAULT false,
    section     TEXT,
    updated_by  TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    REVOKE ALL ON settings FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON settings TO nce_app;
    ELSE
        RAISE NOTICE 'nce_app role not found — settings GRANTs skipped';
    END IF;
END $$;
