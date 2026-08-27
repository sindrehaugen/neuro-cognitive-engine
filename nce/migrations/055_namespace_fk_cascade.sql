-- 055_namespace_fk_cascade.sql
--
-- Make every tenant-scoped child FK to namespaces ON DELETE CASCADE, so a
-- namespace can actually be deleted.
--
-- Before this migration, 54 of the 94 FKs referencing namespaces were NO
-- ACTION, so `DELETE FROM namespaces` failed on the first child row. That is
-- why the pytest fixtures leaked: they had no teardown they could have written.
-- It is also why real tenant deprovisioning does not work.
--
-- Deliberately NOT converted:
--
--   event_log, event_parents  -- WORM. `prevent_mutation()` is a plain plpgsql
--       trigger that RAISEs on any DELETE, and triggers fire regardless of
--       role, so a CASCADE here would turn an FK violation into a trigger
--       exception rather than a working delete. Namespaces holding event rows
--       remain undeletable by design.
--
--   audit_log  -- cascading destroys a tenant's audit trail on deletion. That
--       is a retention/compliance decision, not a mechanical one. Left NO
--       ACTION until it is made explicitly.
--
--   namespaces_parent_id_fkey  -- self-reference. Whether deleting a parent
--       tenant should delete its children is a product decision.
--
-- Idempotent: each constraint is only rebuilt when it is not already CASCADE.
-- ON DELETE cannot be changed with ALTER CONSTRAINT, so the constraint is
-- dropped and re-added. For partitioned tables (event_log aside, that is
-- kg_nodes, kg_edges, memory_embeddings, memory_salience, embedding_aspects,
-- contradictions, memories, pii_redactions) acting on the partitioned parent
-- propagates to every partition.

DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('a2a_grants', 'a2a_grants_owner_namespace_id_fkey', 'owner_namespace_id'),
            ('action_approval_queue', 'action_approval_queue_namespace_id_fkey', 'namespace_id'),
            ('action_idempotency', 'action_idempotency_namespace_id_fkey', 'namespace_id'),
            ('actor_trust', 'actor_trust_namespace_id_fkey', 'namespace_id'),
            ('bridge_subscriptions', 'bridge_subscriptions_namespace_id_fkey', 'namespace_id'),
            ('consolidation_runs', 'consolidation_runs_namespace_id_fkey', 'namespace_id'),
            ('contradictions', 'contradictions_namespace_id_fkey', 'namespace_id'),
            ('dead_letter_queue', 'dead_letter_queue_namespace_id_fkey', 'namespace_id'),
            ('embedding_aspects', 'embedding_aspects_namespace_id_fkey', 'namespace_id'),
            ('embedding_migrations', 'embedding_migrations_namespace_id_fkey', 'namespace_id'),
            ('event_sequences', 'event_sequences_namespace_id_fkey', 'namespace_id'),
            ('kg_edges', 'kg_edges_namespace_id_fkey', 'namespace_id'),
            ('kg_nodes', 'kg_nodes_namespace_id_fkey', 'namespace_id'),
            ('memories', 'memories_namespace_id_fkey', 'namespace_id'),
            ('memory_embeddings', 'memory_embeddings_namespace_id_fkey', 'namespace_id'),
            ('memory_salience', 'memory_salience_namespace_id_fkey', 'namespace_id'),
            ('pii_redactions', 'pii_redactions_namespace_id_fkey', 'namespace_id'),
            ('processed_outbox_events', 'processed_outbox_events_namespace_id_fkey', 'namespace_id'),
            ('replay_runs', 'replay_runs_source_namespace_id_fkey', 'source_namespace_id'),
            ('replay_runs', 'replay_runs_target_namespace_id_fkey', 'target_namespace_id')
        ) AS v(tbl, con, col)
    LOOP
        -- Skip when the table does not exist in this database at all.
        IF NOT EXISTS (
            SELECT 1 FROM pg_class WHERE relname = r.tbl AND relkind IN ('r', 'p')
        ) THEN
            CONTINUE;
        END IF;

        -- Skip when the FK is already ON DELETE CASCADE.
        IF EXISTS (
            SELECT 1
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE c.conname = r.con
              AND t.relname = r.tbl
              AND c.contype = 'f'
              AND c.confdeltype = 'c'
        ) THEN
            CONTINUE;
        END IF;

        IF EXISTS (
            SELECT 1
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE c.conname = r.con AND t.relname = r.tbl AND c.contype = 'f'
        ) THEN
            EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', r.tbl, r.con);
        END IF;

        EXECUTE format(
            'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (%I) '
            'REFERENCES namespaces(id) ON DELETE CASCADE',
            r.tbl, r.con, r.col
        );
    END LOOP;
END $$;
