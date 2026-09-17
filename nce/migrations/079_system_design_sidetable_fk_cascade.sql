-- 079_system_design_sidetable_fk_cascade.sql
-- ============================================================================
-- System Design: side-table -> kg_nodes FK and cascade (Wave SD-1 / D12)
--
-- Closes technical debt D12 across the three System Design side tables:
--   1. system_design_device_capabilities (migration 039)
--   2. system_design_geometry (migration 060)
--   3. system_design_node_state (migration 061)
--
-- WHY THIS CONSTRAINT WAS DEFERRED UNTIL NOW
-- -----------------------------------------
-- kg_nodes is HASH-partitioned on `label` with a composite unique key
-- `UNIQUE (label, namespace_id)`.  In early revisions, composite foreign keys
-- referencing partitioned tables were avoided.  Until W17, no delete path
-- existed, so orphan rows could not be created through normal operations.
--
-- THE D12 HAZARD (THE RESURRECTION)
-- ---------------------------------
-- Node labels are deterministic: the same design_id and device_ref produce the
-- same label forever.  Without a foreign key, deleting a node from kg_nodes
-- leaves its capability, geometry, and lifecycle rows behind as orphans.  A
-- subsequent re-author of that same label lands on the orphan through
-- ON CONFLICT DO UPDATE and silently inherits its status, capabilities, and
-- placement.  An auditor reproduced this on 67g.
--
-- retire.py mitigated this at the application level by executing manual DELETEs
-- across all three side tables within the same transaction as the node delete.
-- This migration provides the structural database-level guarantee: every side-
-- table row is bound by a foreign key to kg_nodes with ON DELETE CASCADE.
-- When a node is deleted from kg_nodes, all of its side-table rows cascade
-- automatically.
--
-- IDEMPOTENCY & CLEANUP
-- ---------------------
-- Before adding the foreign key constraints, any pre-existing orphan rows in
-- the three side tables that have no corresponding row in kg_nodes are purged.
-- Each constraint is created with IF NOT EXISTS inside a DO block.
-- ============================================================================

-- 1. Purge pre-existing orphan rows that lack a parent in kg_nodes.
DELETE FROM system_design_device_capabilities c
 WHERE NOT EXISTS (
     SELECT 1 FROM kg_nodes n
      WHERE n.label = c.node_label AND n.namespace_id = c.namespace_id
 );

DELETE FROM system_design_geometry g
 WHERE NOT EXISTS (
     SELECT 1 FROM kg_nodes n
      WHERE n.label = g.node_label AND n.namespace_id = g.namespace_id
 );

DELETE FROM system_design_node_state s
 WHERE NOT EXISTS (
     SELECT 1 FROM kg_nodes n
      WHERE n.label = s.node_label AND n.namespace_id = s.namespace_id
 );

-- 2. Add foreign key constraints with ON DELETE CASCADE.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_sddc_kg_nodes'
    ) THEN
        ALTER TABLE system_design_device_capabilities
            ADD CONSTRAINT fk_sddc_kg_nodes
            FOREIGN KEY (node_label, namespace_id)
            REFERENCES kg_nodes (label, namespace_id)
            ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_sdg_kg_nodes'
    ) THEN
        ALTER TABLE system_design_geometry
            ADD CONSTRAINT fk_sdg_kg_nodes
            FOREIGN KEY (node_label, namespace_id)
            REFERENCES kg_nodes (label, namespace_id)
            ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_sdns_kg_nodes'
    ) THEN
        ALTER TABLE system_design_node_state
            ADD CONSTRAINT fk_sdns_kg_nodes
            FOREIGN KEY (node_label, namespace_id)
            REFERENCES kg_nodes (label, namespace_id)
            ON DELETE CASCADE;
    END IF;
END $$;
