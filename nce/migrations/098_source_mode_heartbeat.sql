-- 098_source_mode_heartbeat.sql
--
-- C5 flip-gate staleness heartbeat (follow-on filed in nce/source_mode/flip.py's
-- own "STATED LIMITATION" docstring, Wave D-9): flip_blocked()/flip_status()
-- answer "zero divergence rows in the window" -- which reads identically
-- whether parity held OR nothing ever compared anything. This table makes
-- "did a comparison actually run" observable, independent of whether it
-- found a divergence (record_divergence() only ever fires on a real
-- mismatch -- a clean comparison writes nothing there today).
--
-- One row per (namespace_id, engine), upserted on every "both"-mode
-- resolve() call (nce/source_mode/resolver.py) -- not append-only, since
-- this is a liveness signal, not an audit trail (divergence_log already
-- is the audit trail).

BEGIN;

CREATE TABLE IF NOT EXISTS source_mode_heartbeat (
    namespace_id     UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    engine           TEXT        NOT NULL,
    last_checked_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    check_count      BIGINT      NOT NULL DEFAULT 1,
    PRIMARY KEY (namespace_id, engine)
);

CREATE INDEX IF NOT EXISTS idx_source_mode_heartbeat_last_checked
    ON source_mode_heartbeat (namespace_id, engine, last_checked_at);

ALTER TABLE source_mode_heartbeat ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_mode_heartbeat FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'source_mode_heartbeat' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON source_mode_heartbeat
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

COMMIT;
