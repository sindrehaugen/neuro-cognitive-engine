-- 008_v3_cognitive_ledger.sql
-- Creates the v3_cognitive_ledger table for Empathic Tensor storage.
-- Empathic Tensor vector(6) encodes:
--   [0] valence        — VAD positive/negative sentiment (VADER)
--   [1] arousal        — VAD calm/excited state
--   [2] dominance      — VAD in-control/helpless dimension
--   [3] temporal_demand  — NASA-TLX time pressure
--   [4] mental_demand    — NASA-TLX cognitive complexity
--   [5] frustration      — NASA-TLX stress/irritation
--
-- RLS policy isolates rows by namespace via get_nce_namespace().

CREATE TABLE IF NOT EXISTS v3_cognitive_ledger (
    id               UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    namespace_id     UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    memory_id        UUID,
    empathic_tensor  vector(6)   NOT NULL,
    tlx_scores       JSONB,
    vad_scores       JSONB,
    model_version    TEXT        NOT NULL,
    created_at       TIMESTAMPTZ DEFAULT now() NOT NULL
);

-- HNSW index for nearest-neighbour empathic tensor queries (cosine similarity).
CREATE INDEX IF NOT EXISTS v3_cognitive_ledger_empathic_tensor_hnsw
    ON v3_cognitive_ledger USING hnsw (empathic_tensor vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Namespace lookup index for efficient RLS scans and tenant queries.
CREATE INDEX IF NOT EXISTS v3_cognitive_ledger_namespace_id
    ON v3_cognitive_ledger (namespace_id);

-- Memory lookup index for JOIN-heavy recall paths.
CREATE INDEX IF NOT EXISTS v3_cognitive_ledger_memory_id
    ON v3_cognitive_ledger (memory_id)
    WHERE memory_id IS NOT NULL;

-- Chronological scan index for time-range analytics.
CREATE INDEX IF NOT EXISTS v3_cognitive_ledger_created_at
    ON v3_cognitive_ledger (created_at DESC);

-- Row-Level Security: each tenant sees only its own empathic vectors.
ALTER TABLE v3_cognitive_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE v3_cognitive_ledger FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON v3_cognitive_ledger;
CREATE POLICY tenant_isolation ON v3_cognitive_ledger
    FOR ALL
    USING (namespace_id = get_nce_namespace());

-- Application role grants.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON v3_cognitive_ledger TO nce_app;
    END IF;
END $$;

-- GC role bypasses RLS for maintenance sweeps.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_gc') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON v3_cognitive_ledger TO nce_gc;
    END IF;
END $$;
