-- ============================================================================
-- TriMCP Migration: Row-Level Security hardening (idempotent)
-- Complements nce/schema.sql — safe to re-run on existing databases.
-- ============================================================================

-- Application role (login enabled — granted to runtime DB user)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        CREATE ROLE nce_app WITH LOGIN PASSWORD 'nce_app_secret';
    ELSE
        ALTER ROLE nce_app WITH LOGIN PASSWORD 'nce_app_secret';
    END IF;
END $$;

-- Fail-fast namespace resolver for RLS policies
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
$$ LANGUAGE plpgsql STABLE;

ALTER TABLE bridge_subscriptions ADD COLUMN IF NOT EXISTS namespace_id UUID REFERENCES namespaces(id);
ALTER TABLE dead_letter_queue ADD COLUMN IF NOT EXISTS namespace_id UUID REFERENCES namespaces(id);
ALTER TABLE embedding_migrations ADD COLUMN IF NOT EXISTS namespace_id UUID REFERENCES namespaces(id);

DO $$
DECLARE
    legacy_ns UUID;
BEGIN
    SELECT id INTO legacy_ns FROM namespaces WHERE slug = '_global_legacy' LIMIT 1;
    IF legacy_ns IS NOT NULL THEN
        UPDATE bridge_subscriptions SET namespace_id = legacy_ns WHERE namespace_id IS NULL;
        UPDATE dead_letter_queue SET namespace_id = legacy_ns WHERE namespace_id IS NULL;
        UPDATE embedding_migrations SET namespace_id = legacy_ns WHERE namespace_id IS NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_dead_letter_queue_namespace_id
    ON dead_letter_queue (namespace_id);
CREATE INDEX IF NOT EXISTS idx_embedding_migrations_namespace_id
    ON embedding_migrations (namespace_id);

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
        'memory_embeddings'
    ];
BEGIN
    FOREACH t IN ARRAY tenant_tables
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS namespace_isolation_policy ON public.%I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_policy ON public.%I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation_policy ON public.%I '
            'FOR ALL TO nce_app '
            'USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace()) '
            'WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())',
            t
        );
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM nce_app', t);
        IF t IN ('event_log', 'pii_redactions') THEN
            EXECUTE format(
                'GRANT SELECT, INSERT ON TABLE public.%I TO nce_app',
                t
            );
        ELSE
            EXECUTE format(
                'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO nce_app',
                t
            );
        END IF;
    END LOOP;
END $$;

ALTER TABLE a2a_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE a2a_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS namespace_isolation_policy ON a2a_grants;
DROP POLICY IF EXISTS tenant_isolation_policy ON a2a_grants;
CREATE POLICY tenant_isolation_policy ON a2a_grants
    FOR ALL TO nce_app
    USING (
        owner_namespace_id = get_nce_namespace()
        OR target_namespace_id = get_nce_namespace()
    )
    WITH CHECK (owner_namespace_id = get_nce_namespace());
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE a2a_grants TO nce_app;

-- Quality gate: catalog + row counts (RLS bypass only inside this transaction).
-- WARNING: Do not copy SET LOCAL row_security = off outside a guarded migration block.
BEGIN;
SET LOCAL row_security = off;
DO $$
DECLARE
    t text;
    tables_to_check text[] := ARRAY[
        'memories',
        'kg_nodes',
        'kg_edges',
        'pii_redactions',
        'memory_salience',
        'contradictions',
        'snapshots',
        'event_log',
        'a2a_grants',
        'resource_quotas',
        'consolidation_runs',
        'bridge_subscriptions',
        'dead_letter_queue',
        'embedding_migrations',
        'memory_embeddings'
    ];
    row_count bigint;
    rls_on boolean;
    force_on boolean;
    pol_count int;
BEGIN
    FOREACH t IN ARRAY tables_to_check
    LOOP
        SELECT c.relrowsecurity, c.relforcerowsecurity
        INTO rls_on, force_on
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = t;

        IF NOT COALESCE(rls_on, false) THEN
            RAISE EXCEPTION '001_enable_rls: RLS not enabled on %', t;
        END IF;
        IF NOT COALESCE(force_on, false) THEN
            RAISE EXCEPTION '001_enable_rls: FORCE RLS not enabled on %', t;
        END IF;

        SELECT count(*)::int INTO pol_count
        FROM pg_policies
        WHERE schemaname = 'public' AND tablename = t;

        IF pol_count < 1 THEN
            RAISE EXCEPTION '001_enable_rls: no policies on %', t;
        END IF;

        EXECUTE format('SELECT count(*) FROM public.%I', t) INTO row_count;
        RAISE NOTICE '001_enable_rls quality gate: table % row_count=%', t, row_count;
    END LOOP;
END $$;
COMMIT;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
        ALTER ROLE postgres RESET row_security;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_gc') THEN
        CREATE ROLE nce_gc BYPASSRLS NOLOGIN;
    ELSE
        ALTER ROLE nce_gc BYPASSRLS NOLOGIN;
    END IF;
END $$;
