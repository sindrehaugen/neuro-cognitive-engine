-- 036_hybrid_rrf_procedural_and_worm_lock.sql
--
-- v3.4.0 Security, Scalability, Hybrid RRF + Procedural Intelligence & WORM Serialization
-- 1. Per-namespace WORM advisory lock helper acquire_event_log_worm_lock(UUID) + BEFORE INSERT trigger on event_log
-- 2. Trigger-maintained memories.search_tsv + GIN index (preserving content_fts compatibility) + 'procedural' memory_type
-- 3. topology_graph.valid_from temporal column + idx_topology_graph_temporal
-- 4. procedural_heuristics table with RLS + FORCE RLS bound to get_nce_namespace() + trigger-maintained search_tsv

BEGIN;

-- 1. Per-namespace WORM advisory lock helper + BEFORE INSERT trigger on event_log
CREATE OR REPLACE FUNCTION acquire_event_log_worm_lock(p_namespace_id UUID)
RETURNS void
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
    IF p_namespace_id IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(
            hashtext('nce_worm_event_log'),
            hashtext(p_namespace_id::text)
        );
    END IF;
END;
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT EXECUTE ON FUNCTION acquire_event_log_worm_lock(UUID) TO nce_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_gc') THEN
        GRANT EXECUTE ON FUNCTION acquire_event_log_worm_lock(UUID) TO nce_gc;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION trg_acquire_event_log_worm_lock()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
    IF NEW.namespace_id IS NOT NULL THEN
        PERFORM acquire_event_log_worm_lock(NEW.namespace_id);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_event_log_worm_advisory_lock ON event_log;
CREATE TRIGGER trg_event_log_worm_advisory_lock
    BEFORE INSERT ON event_log
    FOR EACH ROW
    EXECUTE FUNCTION trg_acquire_event_log_worm_lock();

-- 2. Trigger-maintained search_tsv on memories + 'procedural' memory_type
ALTER TABLE memories ADD COLUMN IF NOT EXISTS search_tsv TSVECTOR;

ALTER TABLE memories DROP CONSTRAINT IF EXISTS ck_memories_memory_type;
ALTER TABLE memories ADD CONSTRAINT ck_memories_memory_type
    CHECK (memory_type IN ('episodic', 'consolidated', 'decision', 'code_chunk', 'procedural'));

CREATE OR REPLACE FUNCTION sync_memories_search_tsv()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    meta_tokens TEXT;
BEGIN
    -- Respect GDPR crypto-shredding / provable forgetting when content_fts + wrapped_dek are nulled.
    IF TG_OP = 'UPDATE'
       AND NEW.content_fts IS NULL
       AND NEW.wrapped_dek IS NULL
       AND (OLD.content_fts IS NOT NULL OR OLD.search_tsv IS NOT NULL) THEN
        NEW.search_tsv := NULL;
        RETURN NEW;
    END IF;

    meta_tokens := concat_ws(
        ' ',
        COALESCE(NEW.name, ''),
        COALESCE(NEW.filepath, ''),
        COALESCE(NEW.metadata->>'summary', ''),
        COALESCE(NEW.metadata->>'title', ''),
        COALESCE(NEW.metadata->>'sku', ''),
        COALESCE(NEW.metadata->>'orgnr', ''),
        COALESCE(NEW.metadata->>'ip', ''),
        COALESCE(NEW.metadata->>'ip_address', ''),
        COALESCE(NEW.metadata->>'identifiers', '')
    );

    IF NEW.content_fts IS NULL AND btrim(meta_tokens) = '' THEN
        RETURN NEW;
    END IF;

    NEW.search_tsv := COALESCE(NEW.content_fts, ''::tsvector)
        || to_tsvector('simple', meta_tokens);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sync_memories_search_tsv ON memories;
CREATE TRIGGER trg_sync_memories_search_tsv
    BEFORE INSERT OR UPDATE ON memories
    FOR EACH ROW
    EXECUTE FUNCTION sync_memories_search_tsv();

UPDATE memories
SET search_tsv = content_fts
WHERE search_tsv IS NULL AND content_fts IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memories_search_tsv
    ON memories USING GIN (search_tsv);

-- 3. topology_graph.valid_from temporal column & index fix (latent bug in nce/causal/correlation.py:289)
ALTER TABLE topology_graph ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE topology_graph ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_topology_graph_temporal
    ON topology_graph (namespace_id, valid_from, valid_to);

-- 4. procedural_heuristics table (partition-safe memory_id UUID without FK to partitioned memories)
CREATE TABLE IF NOT EXISTS procedural_heuristics (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id       UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    memory_id          UUID,
    rule_text          TEXT NOT NULL,
    trigger_condition  TEXT NOT NULL DEFAULT '',
    action_guidance    TEXT NOT NULL DEFAULT '',
    source_type        TEXT NOT NULL DEFAULT 'governed_decision',
    source_refs        JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence_weight  FLOAT8 NOT NULL DEFAULT 0.5
        CONSTRAINT ck_procedural_heuristics_confidence CHECK (confidence_weight >= 0.0 AND confidence_weight <= 1.0),
    support_count      INT NOT NULL DEFAULT 1
        CONSTRAINT ck_procedural_heuristics_support CHECK (support_count >= 0),
    search_tsv         TSVECTOR,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to           TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_procedural_heuristics_namespace_id
    ON procedural_heuristics (namespace_id);

CREATE INDEX IF NOT EXISTS idx_procedural_heuristics_memory_id
    ON procedural_heuristics (memory_id);

CREATE INDEX IF NOT EXISTS idx_procedural_heuristics_temporal
    ON procedural_heuristics (namespace_id, valid_from, valid_to);

CREATE INDEX IF NOT EXISTS idx_procedural_heuristics_search_tsv
    ON procedural_heuristics USING GIN (search_tsv);

CREATE OR REPLACE FUNCTION sync_procedural_heuristics_search_tsv()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
    NEW.search_tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.rule_text, '')), 'A')
        || setweight(to_tsvector('english', COALESCE(NEW.trigger_condition, '')), 'B')
        || setweight(to_tsvector('english', COALESCE(NEW.action_guidance, '')), 'B')
        || to_tsvector(
            'simple',
            concat_ws(
                ' ',
                COALESCE(NEW.rule_text, ''),
                COALESCE(NEW.trigger_condition, ''),
                COALESCE(NEW.action_guidance, '')
            )
        );
    IF TG_OP = 'UPDATE' AND NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at THEN
        NEW.updated_at := NOW();
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sync_procedural_heuristics_search_tsv ON procedural_heuristics;
CREATE TRIGGER trg_sync_procedural_heuristics_search_tsv
    BEFORE INSERT OR UPDATE ON procedural_heuristics
    FOR EACH ROW
    EXECUTE FUNCTION sync_procedural_heuristics_search_tsv();

ALTER TABLE procedural_heuristics ENABLE ROW LEVEL SECURITY;
ALTER TABLE procedural_heuristics FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'procedural_heuristics' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON procedural_heuristics
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE procedural_heuristics FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE procedural_heuristics TO nce_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_gc') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE procedural_heuristics TO nce_gc;
    END IF;
END $$;

COMMIT;
