-- 046_agreements_source_id.sql
-- Add per-vertical agreements_source_id column to kg_nodes and kg_edges.
-- ============================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kg_nodes' AND column_name = 'agreements_source_id'
    ) THEN
        ALTER TABLE kg_nodes ADD COLUMN agreements_source_id TEXT;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kg_edges' AND column_name = 'agreements_source_id'
    ) THEN
        ALTER TABLE kg_edges ADD COLUMN agreements_source_id TEXT;
    END IF;
END $$;

-- Partial indexes for provenance-based retirement queries.
CREATE INDEX IF NOT EXISTS idx_kg_nodes_agreements_source
    ON kg_nodes (namespace_id, agreements_source_id)
    WHERE agreements_source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_kg_edges_agreements_source
    ON kg_edges (namespace_id, agreements_source_id)
    WHERE agreements_source_id IS NOT NULL;
