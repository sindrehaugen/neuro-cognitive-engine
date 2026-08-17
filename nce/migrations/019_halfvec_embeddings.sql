-- 019_halfvec_embeddings.sql
-- NCE_MASTER_PLAN VI.5c D2 (Disk I/O) — migrate fixed-dimension pgvector
-- embedding columns from vector(768) (fp32, ~3 KB/row + a large, write-
-- amplifying HNSW index) to halfvec(768) (fp16). This halves on-disk vector
-- storage, HNSW index size, and read I/O with negligible recall loss; the
-- existing fp32 values cast to fp16 in place (USING embedding::halfvec(768)).
-- The existing re-embedding machinery carries recall going forward — no
-- coordinated re-embedding is required, so this is a pure in-place column-type
-- migration + HNSW index rebuild. Reconciled with Batch 18 (vector compliance /
-- cryptographic erasure), which is DONE+PASSED TAG, so the storage-format change
-- does not conflict with the erasure work.
--
-- Mirrors nce/schema.sql (memories.embedding, kg_nodes.embedding and their HNSW
-- indexes idx_memories_embedding_hnsw / idx_kg_nodes_embedding_hnsw).
--
-- NOTE: The dynamic-dimension embedding stores (memory_embeddings.embedding,
-- kg_node_embeddings.embedding) are unconstrained `vector` (any model dim) and
-- are intentionally NOT touched here.
--
-- Idempotent: re-running is a no-op. ALTER ... TYPE is skipped when the column
-- is already halfvec; the HNSW index drop/recreate is conditioned on the column
-- type so a second run does not rebuild an already-halfvec index.
-- ============================================================================

DO $$
BEGIN
    -- memories.embedding -----------------------------------------------------
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'memories'
          AND column_name = 'embedding' AND udt_name = 'vector'
    ) THEN
        DROP INDEX IF EXISTS idx_memories_embedding_hnsw;
        ALTER TABLE memories
            ALTER COLUMN embedding TYPE halfvec(768)
            USING embedding::halfvec(768);
        CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw
            ON memories USING hnsw (embedding halfvec_cosine_ops);
    END IF;

    -- kg_nodes.embedding -----------------------------------------------------
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'kg_nodes'
          AND column_name = 'embedding' AND udt_name = 'vector'
    ) THEN
        DROP INDEX IF EXISTS idx_kg_nodes_embedding_hnsw;
        ALTER TABLE kg_nodes
            ALTER COLUMN embedding TYPE halfvec(768)
            USING embedding::halfvec(768);
        CREATE INDEX IF NOT EXISTS idx_kg_nodes_embedding_hnsw
            ON kg_nodes USING hnsw (embedding halfvec_cosine_ops);
    END IF;
END $$;
