-- Migration: Add embedding_aspects companion table for multi-vector / aspect search
CREATE TABLE IF NOT EXISTS embedding_aspects (
    memory_id    UUID NOT NULL,
    aspect       VARCHAR(64) NOT NULL,
    embedding    halfvec(768),
    namespace_id UUID REFERENCES namespaces(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, aspect)
) PARTITION BY HASH (memory_id);

CREATE TABLE IF NOT EXISTS embedding_aspects_0 PARTITION OF embedding_aspects FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE IF NOT EXISTS embedding_aspects_1 PARTITION OF embedding_aspects FOR VALUES WITH (MODULUS 4, REMAINDER 1);
CREATE TABLE IF NOT EXISTS embedding_aspects_2 PARTITION OF embedding_aspects FOR VALUES WITH (MODULUS 4, REMAINDER 2);
CREATE TABLE IF NOT EXISTS embedding_aspects_3 PARTITION OF embedding_aspects FOR VALUES WITH (MODULUS 4, REMAINDER 3);

CREATE INDEX IF NOT EXISTS idx_embedding_aspects_hnsw ON embedding_aspects USING hnsw (embedding halfvec_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_embedding_aspects_namespace_id ON embedding_aspects (namespace_id);

ALTER TABLE embedding_aspects ENABLE ROW LEVEL SECURITY;
ALTER TABLE embedding_aspects FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON embedding_aspects;
CREATE POLICY tenant_isolation_policy ON embedding_aspects
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON embedding_aspects TO nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON embedding_aspects_0 TO nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON embedding_aspects_1 TO nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON embedding_aspects_2 TO nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON embedding_aspects_3 TO nce_app;
    END IF;
END $$;
