-- 045_agreement_review.sql
-- Create tenant-isolated agreement_review_queue and agreement_extraction_runs tables.
-- ============================================================================

CREATE TABLE IF NOT EXISTS agreement_review_queue (
    agreement_id           UUID        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    source_doc_ref         TEXT        NOT NULL,
    extraction_confidence  NUMERIC     NOT NULL,
    review_status          TEXT        NOT NULL DEFAULT 'needs_review_yellow'
                                       CHECK (review_status IN ('auto_green', 'needs_review_yellow', 'manual_red')),
    extracted              JSONB       NOT NULL,
    flagged_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_by            TEXT,
    reviewed_at            TIMESTAMPTZ,
    PRIMARY KEY (agreement_id, namespace_id)
);

CREATE INDEX IF NOT EXISTS idx_agreement_review_queue_namespace ON agreement_review_queue (namespace_id);

CREATE TABLE IF NOT EXISTS agreement_extraction_runs (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    run_id                 UUID        NOT NULL,
    source_doc_ref         TEXT        NOT NULL,
    extraction_confidence  NUMERIC,
    status                 TEXT        NOT NULL DEFAULT 'ok'
                                       CHECK (status IN ('ok', 'error')),
    error                  TEXT,
    started_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_agreement_extraction_runs_namespace_time
    ON agreement_extraction_runs (namespace_id, started_at DESC);

ALTER TABLE agreement_review_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE agreement_review_queue FORCE ROW LEVEL SECURITY;

ALTER TABLE agreement_extraction_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agreement_extraction_runs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON agreement_review_queue;
CREATE POLICY tenant_isolation_policy ON agreement_review_queue
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DROP POLICY IF EXISTS tenant_isolation_policy ON agreement_extraction_runs;
CREATE POLICY tenant_isolation_policy ON agreement_extraction_runs
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE agreement_review_queue FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE agreement_review_queue TO nce_app;

        REVOKE ALL ON TABLE agreement_extraction_runs FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE agreement_extraction_runs TO nce_app;
    END IF;
END $$;
