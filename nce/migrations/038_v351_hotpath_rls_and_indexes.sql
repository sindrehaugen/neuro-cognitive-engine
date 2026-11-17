-- 038_v351_hotpath_rls_and_indexes.sql
--
-- v3.5.1 Hot-path RLS InitPlan optimization, trigram & active-row indexes, and Saga recovery partial index:
-- 1. Mark get_nce_namespace(), get_nce_external_scope(), and nce_decayed_score() STABLE PARALLEL SAFE.
-- 2. Rewrite RLS policies across Core tenant tables to wrap get_nce_namespace() in a scalar
--    subquery (SELECT get_nce_namespace()) so PostgreSQL evaluates the GUC once per query via InitPlan.
-- 3. Add combined trigram GIN index idx_kg_edges_combined_trgm on ((subject_label || ' ' || object_label))
--    to eliminate sequential scans on OR-ed ILIKE edge filters.
-- 4. Add partial active-row indexes idx_memories_ns_payload_ref_active, idx_memories_search_tsv_active,
--    and idx_saga_log_recovery_needed for sub-millisecond hot-path and saga recovery sweeps.

BEGIN;

-- 1. Mark RLS and decay functions STABLE PARALLEL SAFE
CREATE OR REPLACE FUNCTION get_nce_namespace() RETURNS uuid AS $$
DECLARE
    val text;
BEGIN
    val := nullif(trim(current_setting('nce.namespace_id', true)), '');
    IF val IS NULL THEN
        RAISE EXCEPTION 'nce.namespace_id is not set for this transaction';
    END IF;
    BEGIN
        RETURN val::uuid;
    EXCEPTION
        WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'nce.namespace_id is not a valid UUID: %', val;
    END;
END;
$$ LANGUAGE plpgsql STABLE PARALLEL SAFE;

CREATE OR REPLACE FUNCTION get_nce_external_scope() RETURNS uuid AS $$
DECLARE
    val text;
BEGIN
    val := nullif(trim(current_setting('nce.external_scope_id', true)), '');
    IF val IS NULL THEN
        RETURN '00000000-0000-0000-0000-000000000000'::uuid;
    END IF;
    BEGIN
        RETURN val::uuid;
    EXCEPTION
        WHEN invalid_text_representation THEN
            RETURN '00000000-0000-0000-0000-000000000000'::uuid;
    END;
END;
$$ LANGUAGE plpgsql STABLE PARALLEL SAFE;

CREATE OR REPLACE FUNCTION nce_decayed_score(
    s_last FLOAT,
    updated_at TIMESTAMPTZ,
    half_life_days FLOAT
) RETURNS FLOAT AS $$
DECLARE
    delta_t FLOAT;
    decay_constant FLOAT;
    exponent FLOAT;
    MAX_EXP CONSTANT FLOAT := 20.0;
BEGIN
    IF half_life_days <= 0 THEN
        RETURN s_last;
    END IF;
    delta_t := GREATEST(0.0, EXTRACT(EPOCH FROM (NOW() - updated_at)) / 86400.0);
    decay_constant := LN(2) / half_life_days;
    exponent := LEAST(decay_constant * delta_t, MAX_EXP);
    RETURN s_last * EXP(-exponent);
END;
$$ LANGUAGE plpgsql STABLE PARALLEL SAFE;

-- 2. Update RLS policies to evaluate (SELECT get_nce_namespace()) via InitPlan
DO $$
DECLARE
    t text;
    tenant_tables text[] := ARRAY[
        'memories',
        'kg_nodes',
        'kg_edges',
        'pii_redactions',
        'memory_salience',
        'contradictions',
        'snapshots',
        'event_log',
        'resource_quotas',
        'consolidation_runs',
        'bridge_subscriptions',
        'dead_letter_queue',
        'embedding_migrations',
        'memory_embeddings',
        'embedding_aspects',
        'active_learning_queue',
        'processed_outbox_events',
        'actor_trust',
        'event_parents',
        'action_approval_queue',
        'action_idempotency',
        'outbox_events',
        'saga_execution_log',
        'procedural_heuristics',
        'graph_schema_registry',
        'signing_credentials',
        'principal_bindings',
        'outbound_webhooks',
        'mcp_client_tokens',
        'encryption_deks'
    ];
BEGIN
    FOREACH t IN ARRAY tenant_tables
    LOOP
        IF to_regclass(format('public.%I', t)) IS NOT NULL THEN
            EXECUTE format('DROP POLICY IF EXISTS namespace_isolation_policy ON public.%I', t);
            EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_policy ON public.%I', t);
            EXECUTE format(
                'CREATE POLICY tenant_isolation_policy ON public.%I '
                'FOR ALL TO nce_app '
                'USING (namespace_id IS NOT NULL AND namespace_id = (SELECT get_nce_namespace())) '
                'WITH CHECK (namespace_id IS NOT NULL AND namespace_id = (SELECT get_nce_namespace()))',
                t
            );
        END IF;
    END LOOP;
END $$;

DROP POLICY IF EXISTS namespace_isolation_policy ON replay_runs;
DROP POLICY IF EXISTS tenant_isolation_policy ON replay_runs;
CREATE POLICY tenant_isolation_policy ON replay_runs
    FOR ALL TO nce_app
    USING (
        (source_namespace_id IS NOT NULL AND source_namespace_id = (SELECT get_nce_namespace()))
        OR (target_namespace_id IS NOT NULL AND target_namespace_id = (SELECT get_nce_namespace()))
    )
    WITH CHECK (
        source_namespace_id IS NOT NULL
        AND source_namespace_id = (SELECT get_nce_namespace())
    );

DROP POLICY IF EXISTS namespace_isolation_policy ON a2a_grants;
DROP POLICY IF EXISTS tenant_isolation_policy ON a2a_grants;
CREATE POLICY tenant_isolation_policy ON a2a_grants
    FOR ALL TO nce_app
    USING (
        owner_namespace_id = (SELECT get_nce_namespace())
        OR target_namespace_id = (SELECT get_nce_namespace())
    )
    WITH CHECK (owner_namespace_id = (SELECT get_nce_namespace()));

DROP POLICY IF EXISTS topology_graph_tenant_isolation ON topology_graph;
CREATE POLICY topology_graph_tenant_isolation ON topology_graph
    FOR ALL
    USING (namespace_id = (SELECT get_nce_namespace()));

DO $$
BEGIN
    IF to_regclass('public.query_templates') IS NOT NULL THEN
        DROP POLICY IF EXISTS tenant_isolation_policy ON query_templates;
        CREATE POLICY tenant_isolation_policy ON query_templates
            FOR ALL TO nce_app
            USING (namespace_id IS NULL OR namespace_id = (SELECT get_nce_namespace()))
            WITH CHECK (namespace_id IS NOT NULL AND namespace_id = (SELECT get_nce_namespace()));
    END IF;
    IF to_regclass('public.v3_cognitive_ledger') IS NOT NULL THEN
        DROP POLICY IF EXISTS tenant_isolation ON v3_cognitive_ledger;
        CREATE POLICY tenant_isolation ON v3_cognitive_ledger
            FOR ALL
            USING (namespace_id = (SELECT get_nce_namespace()));
    END IF;
    IF to_regclass('public.audit_log') IS NOT NULL THEN
        DROP POLICY IF EXISTS audit_log_tenant_isolation ON audit_log;
        CREATE POLICY audit_log_tenant_isolation ON audit_log
            FOR ALL
            USING (namespace_id = (SELECT get_nce_namespace()));
    END IF;
END $$;

-- 3. Combined trigram GIN index on kg_edges and active-row partial indexes
CREATE INDEX IF NOT EXISTS idx_kg_edges_combined_trgm
    ON kg_edges USING GIN (((subject_label || ' ' || object_label)) gin_trgm_ops)
    WHERE payload_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memories_ns_payload_ref_active
    ON memories (namespace_id, payload_ref)
    WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS idx_memories_search_tsv_active
    ON memories USING GIN (search_tsv)
    WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS idx_saga_log_recovery_needed
    ON saga_execution_log (created_at)
    WHERE state = 'recovery_needed';

COMMIT;
