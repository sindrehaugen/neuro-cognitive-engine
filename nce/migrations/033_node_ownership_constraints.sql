-- 033_node_ownership_constraints.sql
-- Adds a partial unique index and a non-empty CHECK constraint to
-- node_ownership_registry to enforce Contract-A (§9.1) at the database level.
-- Idempotent: both statements are guarded with IF NOT EXISTS / pg_constraint checks.
-- ============================================================================

-- Unique index: at most one ownership row per (namespace, node_type, transition),
-- where NULL transition is treated as a distinct sentinel via COALESCE.
CREATE UNIQUE INDEX IF NOT EXISTS uq_node_ownership_registry_ns_type_transition
    ON node_ownership_registry (namespace_id, node_type, COALESCE(transition, ''));

-- Non-empty CHECK constraint on owner_engine: an empty string is never a valid
-- engine identifier and would silently bypass the deny-by-default guard.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE  conname = 'ck_node_ownership_owner_engine_nonempty'
          AND  conrelid = 'node_ownership_registry'::regclass
    ) THEN
        ALTER TABLE node_ownership_registry
            ADD CONSTRAINT ck_node_ownership_owner_engine_nonempty
            CHECK (owner_engine <> '');
    END IF;
END $$;
