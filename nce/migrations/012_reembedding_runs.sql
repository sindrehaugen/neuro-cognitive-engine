-- 012_reembedding_runs.sql
-- Supports Task GG / TD-G5-004: Clean up runaway runtime DDL modifications
--
-- Creates the reembedding_runs table for tracking worker status and key cursor offsets.
-- ============================================================================

CREATE TABLE IF NOT EXISTS reembedding_runs (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    model_version     UUID        NOT NULL,
    model_name        TEXT        NOT NULL,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ,
    status            TEXT        NOT NULL DEFAULT 'running'
                                  CHECK (status IN ('running','completed','failed')),
    memories_done     BIGINT      NOT NULL DEFAULT 0,
    kg_nodes_done     BIGINT      NOT NULL DEFAULT 0,
    error_message     TEXT,
    -- Keyset cursor checkpointed after each batch for resumability.
    cursor_created_at TIMESTAMPTZ,
    cursor_id         UUID
);

-- Application role grants.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE ON reembedding_runs TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE reembedding_runs IS
'Global table for tracking re-embedding worker runs, checkpoints, and model transitions.';
