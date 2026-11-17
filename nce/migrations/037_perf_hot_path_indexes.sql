-- 037_perf_hot_path_indexes.sql
--
-- v3.5.0 Hot-path query indexes & redundant event_log index cleanup.
-- Note: Plain CREATE INDEX IF NOT EXISTS (never CONCURRENTLY) inside BEGIN; ... COMMIT;
-- propagates automatically to all existing partitions on partitioned parent tables
-- (memories, kg_nodes, kg_edges, contradictions, event_log).

BEGIN;

-- KG payload_ref, trigram label/subject/object, and composite (namespace_id, entity_type, label) lookups
CREATE INDEX IF NOT EXISTS idx_kg_nodes_ns_payload_ref
    ON kg_nodes (namespace_id, payload_ref) WHERE payload_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kg_edges_ns_payload_ref
    ON kg_edges (namespace_id, payload_ref) WHERE payload_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kg_nodes_label_trgm
    ON kg_nodes USING GIN (label gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_kg_edges_subj_trgm
    ON kg_edges USING GIN (subject_label gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_kg_edges_obj_trgm
    ON kg_edges USING GIN (object_label gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_ns_type_label
    ON kg_nodes (namespace_id, entity_type, label);

-- Active memories derived_from (ATMS cascade) and recent episodic recall
CREATE INDEX IF NOT EXISTS idx_memories_derived_from_gin
    ON memories USING GIN (derived_from jsonb_path_ops) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_memories_ns_agent_recent
    ON memories (namespace_id, agent_id, created_at DESC)
    WHERE valid_to IS NULL AND memory_type = 'episodic';

-- Open contradictions lookup by memory_a_id / memory_b_id
CREATE INDEX IF NOT EXISTS idx_contradictions_ns_mem_a_open
    ON contradictions (namespace_id, memory_a_id) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_contradictions_ns_mem_b_open
    ON contradictions (namespace_id, memory_b_id) WHERE resolved_at IS NULL;

-- Redefine idx_event_log_time_travel to index (namespace_id, memory_id, occurred_at DESC, event_seq DESC)
-- and include 'memory_shredded' events; drop idx_event_log_ns_seq (covered by UNIQUE(namespace_id, event_seq, occurred_at))
DROP INDEX IF EXISTS idx_event_log_ns_seq;
DROP INDEX IF EXISTS idx_event_log_time_travel;
CREATE INDEX IF NOT EXISTS idx_event_log_time_travel
    ON event_log (namespace_id, ((params->>'memory_id')::uuid), occurred_at DESC, event_seq DESC)
    WHERE event_type IN ('store_memory', 'forget_memory', 'memory_shredded');

COMMIT;
