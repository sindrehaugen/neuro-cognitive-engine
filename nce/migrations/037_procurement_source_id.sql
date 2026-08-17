-- 037_procurement_source_id.sql
-- Add per-vertical procurement_source_id column to kg_nodes and kg_edges.
-- Mirrors the d365_source_id pattern (§2.3 — one source-id column per vertical).
-- Enables hard-retirement of procurement-derived rows when the source record is
-- deleted. Idempotent: IF NOT EXISTS guards on both ALTER TABLE statements.
-- ============================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kg_nodes' AND column_name = 'procurement_source_id'
    ) THEN
        ALTER TABLE kg_nodes ADD COLUMN procurement_source_id TEXT;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kg_edges' AND column_name = 'procurement_source_id'
    ) THEN
        ALTER TABLE kg_edges ADD COLUMN procurement_source_id TEXT;
    END IF;
END $$;

-- Partial indexes for provenance-based retirement queries.
CREATE INDEX IF NOT EXISTS idx_kg_nodes_procurement_source
    ON kg_nodes (namespace_id, procurement_source_id)
    WHERE procurement_source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_kg_edges_procurement_source
    ON kg_edges (namespace_id, procurement_source_id)
    WHERE procurement_source_id IS NOT NULL;
