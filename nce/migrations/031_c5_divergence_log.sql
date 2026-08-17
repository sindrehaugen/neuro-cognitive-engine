-- 031_c5_divergence_log.sql
-- C5 divergence audit log: append-only per-engine divergence records with
-- materiality classification, tenant RLS isolation, and the flip-gate window
-- query interface.
--
-- Design decisions:
--   - One shared ``divergence_log`` table (not per-engine); engine is a column.
--   - Append-only (no UPDATE/DELETE) — divergence history must be WORM-like.
--   - FORCE RLS: every row is scoped to one tenant (namespace_id).
--   - No FK to source_mode_config: divergence rows survive config row deletion.
--   - ``materiality NUMERIC NOT NULL`` — caller supplies; storage is agnostic.
--   - Idempotent DDL throughout (IF NOT EXISTS / DO $$ … $$).
-- ============================================================================

CREATE TABLE IF NOT EXISTS divergence_log (
    id           UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    engine       TEXT        NOT NULL,
    entity       TEXT        NOT NULL,
    field        TEXT        NOT NULL,
    nce_value    TEXT,
    ext_value    TEXT,
    materiality  NUMERIC     NOT NULL,
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- Fast flip-gate window queries: namespace + engine + detected_at range.
CREATE INDEX IF NOT EXISTS idx_divergence_log_namespace_engine_detected
    ON divergence_log (namespace_id, engine, detected_at DESC);

-- Row-Level Security: each tenant sees only its own divergence rows.
ALTER TABLE divergence_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE divergence_log FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON divergence_log;
CREATE POLICY tenant_isolation_policy ON divergence_log
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

-- Application role grants: INSERT + SELECT (append-only; no UPDATE/DELETE).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE divergence_log FROM nce_app;
        GRANT SELECT, INSERT ON TABLE divergence_log TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE divergence_log IS
'C5 append-only divergence audit log. One row per detected divergence between NCE
and an external system for a given (engine, entity, field). materiality is caller-
supplied. FORCE RLS isolates per tenant. Underpins the flip-gate: a both→nce flip
is blocked while any unresolved divergence rows exist within the parity window.';
