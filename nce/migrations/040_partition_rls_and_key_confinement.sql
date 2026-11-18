-- 040_partition_rls_and_key_confinement.sql
--
-- Hardening Step 2:
--   1. Scope kg_node_embeddings by namespace_id (backfill from kg_nodes, NOT NULL, FK, index)
--      and enforce FORCED RLS with get_nce_namespace() isolation policy.
--   2. Enforce FORCED RLS on event_sequences (least-privilege SELECT, INSERT, UPDATE).
--   3. Enforce ENABLE + FORCE RLS and tenant_isolation_policy on every child partition
--      of every tenant RLS parent table, plus WORM UPDATE/DELETE/TRUNCATE revocations
--      across all event_log_* partitions and future monthly partitions.
--   4. Confine public.signing_keys behind narrow SECURITY DEFINER helpers and revoke
--      raw table access from PUBLIC, nce_app, and nce_gc.

ALTER TABLE public.kg_node_embeddings
    ADD COLUMN IF NOT EXISTS namespace_id UUID;

UPDATE public.kg_node_embeddings kne
SET namespace_id = kn.namespace_id
FROM public.kg_nodes kn
WHERE kn.id = kne.node_id
  AND kne.namespace_id IS NULL;

DELETE FROM public.kg_node_embeddings
WHERE namespace_id IS NULL;

ALTER TABLE public.kg_node_embeddings
    ALTER COLUMN namespace_id SET DEFAULT get_nce_namespace(),
    ALTER COLUMN namespace_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.kg_node_embeddings'::regclass
          AND confrelid = 'public.namespaces'::regclass
    ) THEN
        ALTER TABLE public.kg_node_embeddings
            ADD CONSTRAINT kg_node_embeddings_namespace_id_fkey
            FOREIGN KEY (namespace_id) REFERENCES public.namespaces(id) ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_kg_node_embeddings_namespace_id
    ON public.kg_node_embeddings (namespace_id);

DO $$
DECLARE
    t text;
    app_exists boolean := EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app');
BEGIN
    FOREACH t IN ARRAY ARRAY['kg_node_embeddings', 'event_sequences']
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS namespace_isolation_policy ON public.%I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_policy ON public.%I', t);
        IF app_exists THEN
            EXECUTE format(
                'CREATE POLICY tenant_isolation_policy ON public.%I '
                'FOR ALL TO nce_app '
                'USING (namespace_id IS NOT NULL AND namespace_id = (SELECT get_nce_namespace())) '
                'WITH CHECK (namespace_id IS NOT NULL AND namespace_id = (SELECT get_nce_namespace()))',
                t
            );
            EXECUTE format('REVOKE ALL ON TABLE public.%I FROM nce_app', t);
            IF t = 'event_sequences' THEN
                EXECUTE format('GRANT SELECT, INSERT, UPDATE ON TABLE public.%I TO nce_app', t);
            ELSE
                EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO nce_app', t);
            END IF;
        END IF;
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION nce_ensure_event_log_monthly_partitions(p_months_ahead int DEFAULT 3)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
    m int;
    p_start timestamptz;
    p_end timestamptz;
    p_name text;
    violating_count int;
BEGIN
    IF p_months_ahead < 0 THEN
        RAISE EXCEPTION 'p_months_ahead must be >= 0';
    END IF;
    FOR m IN 0..p_months_ahead LOOP
        p_start := date_trunc('month', now() + make_interval(months => m));
        p_end := p_start + interval '1 month';
        p_name := 'event_log_' || to_char(p_start, 'YYYY_MM');
        IF to_regclass(format('public.%I', p_name)) IS NULL THEN
            EXECUTE format(
                'SELECT count(*)::int FROM event_log_default WHERE occurred_at >= %L AND occurred_at < %L',
                p_start,
                p_end
            ) INTO violating_count;

            IF violating_count > 0 THEN
                CREATE TEMP TABLE temp_event_log_migrate ON COMMIT DROP AS
                    SELECT * FROM event_log_default
                    WHERE occurred_at >= p_start AND occurred_at < p_end;

                PERFORM set_config('nce.partition_maintenance', 'on', true);
                DELETE FROM event_log_default
                WHERE occurred_at >= p_start AND occurred_at < p_end;
                PERFORM set_config('nce.partition_maintenance', '', true);

                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF event_log FOR VALUES FROM (%L) TO (%L)',
                    p_name,
                    p_start,
                    p_end
                );

                INSERT INTO event_log SELECT * FROM temp_event_log_migrate;

                DROP TABLE temp_event_log_migrate;
            ELSE
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF event_log FOR VALUES FROM (%L) TO (%L)',
                    p_name,
                    p_start,
                    p_end
                );
            END IF;

            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', p_name);
            EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', p_name);
            EXECUTE format('DROP POLICY IF EXISTS namespace_isolation_policy ON public.%I', p_name);
            EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_policy ON public.%I', p_name);
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
                EXECUTE format(
                    'CREATE POLICY tenant_isolation_policy ON public.%I '
                    'FOR ALL TO nce_app '
                    'USING (namespace_id IS NOT NULL AND namespace_id = (SELECT get_nce_namespace())) '
                    'WITH CHECK (namespace_id IS NOT NULL AND namespace_id = (SELECT get_nce_namespace()))',
                    p_name
                );
                EXECUTE format('REVOKE ALL ON TABLE public.%I FROM nce_app', p_name);
                EXECUTE format('GRANT SELECT, INSERT ON TABLE public.%I TO nce_app', p_name);
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_gc') THEN
                EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.%I FROM nce_gc', p_name);
            END IF;
            EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.%I FROM PUBLIC', p_name);
        END IF;
    END LOOP;
END;
$fn$;

CREATE OR REPLACE FUNCTION nce_count_signing_keys()
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
    cnt bigint;
BEGIN
    SELECT count(*)::bigint INTO cnt FROM public.signing_keys;
    RETURN COALESCE(cnt, 0);
END;
$fn$;

CREATE OR REPLACE FUNCTION nce_get_active_signing_key()
RETURNS TABLE(key_id text, encrypted_key bytea)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
BEGIN
    RETURN QUERY
    SELECT sk.key_id, sk.encrypted_key
    FROM public.signing_keys sk
    WHERE sk.status = 'active'
    ORDER BY sk.created_at DESC
    LIMIT 1;
END;
$fn$;

CREATE OR REPLACE FUNCTION nce_get_signing_key_by_id(p_key_id text)
RETURNS TABLE(key_id text, encrypted_key bytea)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
BEGIN
    IF p_key_id IS NULL OR btrim(p_key_id) = '' THEN
        RETURN;
    END IF;
    RETURN QUERY
    SELECT sk.key_id, sk.encrypted_key
    FROM public.signing_keys sk
    WHERE sk.key_id = p_key_id
    LIMIT 1;
END;
$fn$;

CREATE OR REPLACE FUNCTION nce_get_signing_keys_status()
RETURNS TABLE(
    active_key_id text,
    active_created_at timestamptz,
    status_counts jsonb,
    latest_retired_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
    v_active_id text;
    v_active_created timestamptz;
    v_counts jsonb;
    v_latest_retired timestamptz;
BEGIN
    SELECT sk.key_id, sk.created_at
    INTO v_active_id, v_active_created
    FROM public.signing_keys sk
    WHERE sk.status = 'active'
    ORDER BY sk.created_at DESC
    LIMIT 1;

    SELECT COALESCE(jsonb_object_agg(s.status, s.cnt), '{}'::jsonb)
    INTO v_counts
    FROM (
        SELECT status, count(*)::bigint AS cnt
        FROM public.signing_keys
        GROUP BY status
    ) s;

    SELECT max(retired_at)
    INTO v_latest_retired
    FROM public.signing_keys
    WHERE status = 'retired';

    RETURN QUERY SELECT v_active_id, v_active_created, v_counts, v_latest_retired;
END;
$fn$;

CREATE OR REPLACE FUNCTION nce_rotate_signing_key(p_key_id text, p_encrypted_key bytea)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
BEGIN
    IF p_key_id IS NULL OR btrim(p_key_id) = '' THEN
        RAISE EXCEPTION 'p_key_id must not be empty';
    END IF;
    IF p_encrypted_key IS NULL OR octet_length(p_encrypted_key) = 0 THEN
        RAISE EXCEPTION 'p_encrypted_key must not be empty';
    END IF;

    UPDATE public.signing_keys
    SET status = 'retired',
        retired_at = now()
    WHERE status = 'active';

    INSERT INTO public.signing_keys (key_id, encrypted_key, status)
    VALUES (p_key_id, p_encrypted_key, 'active');
END;
$fn$;

CREATE OR REPLACE FUNCTION nce_enforce_partition_rls_and_worm()
RETURNS void
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $fn$
DECLARE
    app_exists boolean := EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app');
    gc_exists boolean := EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_gc');
    part record;
    ns_col text;
BEGIN
    FOR part IN
        SELECT
            c.relname AS part_name,
            p.relname AS parent_name,
            EXISTS (
                SELECT 1 FROM pg_attribute a
                WHERE a.attrelid = c.oid AND a.attname = 'namespace_id' AND NOT a.attisdropped
            ) AS has_ns_id,
            EXISTS (
                SELECT 1 FROM pg_attribute a
                WHERE a.attrelid = c.oid AND a.attname = 'source_namespace_id' AND NOT a.attisdropped
            ) AS has_src_ns_id
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p')
          AND c.relispartition
          AND p.relrowsecurity
        ORDER BY c.relname
    LOOP
        IF part.has_ns_id THEN
            ns_col := 'namespace_id';
        ELSIF part.has_src_ns_id THEN
            ns_col := 'source_namespace_id';
        ELSE
            CONTINUE;
        END IF;

        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', part.part_name);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', part.part_name);
        EXECUTE format('DROP POLICY IF EXISTS namespace_isolation_policy ON public.%I', part.part_name);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_policy ON public.%I', part.part_name);

        IF app_exists THEN
            EXECUTE format(
                'CREATE POLICY tenant_isolation_policy ON public.%I '
                'FOR ALL TO nce_app '
                'USING (%I IS NOT NULL AND %I = (SELECT get_nce_namespace())) '
                'WITH CHECK (%I IS NOT NULL AND %I = (SELECT get_nce_namespace()))',
                part.part_name,
                ns_col,
                ns_col,
                ns_col,
                ns_col
            );
        END IF;

        IF part.parent_name = 'event_log' THEN
            EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.%I FROM PUBLIC', part.part_name);
            IF app_exists THEN
                EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.%I FROM nce_app', part.part_name);
            END IF;
            IF gc_exists THEN
                EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.%I FROM nce_gc', part.part_name);
            END IF;
        END IF;
    END LOOP;
END;
$fn$;

CREATE OR REPLACE FUNCTION nce_sync_runtime_role_grants()
RETURNS void
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $fn$
DECLARE
    app_exists boolean := EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app');
    gc_exists boolean := EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_gc');
    r record;
    w text;
    worm_tables text[] := ARRAY[
        'event_log',
        'event_parents',
        'audit_log',
        'economy_postings',
        'entra_id_audit_log',
        'sales_signed_baselines',
        'support_ticket_actions',
        'telemetry_samples',
        'divergence_log',
        'd365_sync_runs',
        'inventory_transactions',
        'product_enrichment_log',
        'product_match_feedback'
    ];
BEGIN
    IF app_exists THEN
        GRANT USAGE ON SCHEMA public TO nce_app;
        IF to_regclass('public.namespaces') IS NOT NULL THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.namespaces TO nce_app;
        END IF;
        IF to_regclass('public.embedding_models') IS NOT NULL THEN
            GRANT SELECT, INSERT, UPDATE ON TABLE public.embedding_models TO nce_app;
        END IF;
        IF to_regclass('public.applied_migrations') IS NOT NULL THEN
            GRANT SELECT ON TABLE public.applied_migrations TO nce_app;
        END IF;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO nce_app;
    END IF;

    IF gc_exists THEN
        GRANT USAGE ON SCHEMA public TO nce_gc;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO nce_gc;
        GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO nce_gc;

        FOR r IN
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind IN ('r', 'p')
              AND NOT c.relispartition
              AND c.relname <> 'signing_keys'
            ORDER BY c.relname
        LOOP
            EXECUTE format('GRANT SELECT ON TABLE public.%I TO nce_gc', r.relname);
            IF app_exists THEN
                IF has_table_privilege('nce_app', format('public.%I', r.relname), 'INSERT') THEN
                    EXECUTE format('GRANT INSERT ON TABLE public.%I TO nce_gc', r.relname);
                ELSE
                    EXECUTE format('REVOKE INSERT ON TABLE public.%I FROM nce_gc', r.relname);
                END IF;
                IF has_table_privilege('nce_app', format('public.%I', r.relname), 'UPDATE') THEN
                    EXECUTE format('GRANT UPDATE ON TABLE public.%I TO nce_gc', r.relname);
                ELSE
                    EXECUTE format('REVOKE UPDATE ON TABLE public.%I FROM nce_gc', r.relname);
                END IF;
                IF has_table_privilege('nce_app', format('public.%I', r.relname), 'DELETE') THEN
                    EXECUTE format('GRANT DELETE ON TABLE public.%I TO nce_gc', r.relname);
                ELSE
                    EXECUTE format('REVOKE DELETE ON TABLE public.%I FROM nce_gc', r.relname);
                END IF;
            END IF;
        END LOOP;

        FOREACH w IN ARRAY worm_tables
        LOOP
            IF to_regclass(format('public.%I', w)) IS NOT NULL THEN
                EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.%I FROM nce_gc', w);
            END IF;
        END LOOP;
    END IF;

    IF to_regclass('public.signing_keys') IS NOT NULL THEN
        REVOKE ALL ON TABLE public.signing_keys FROM PUBLIC;
        IF app_exists THEN
            REVOKE ALL ON TABLE public.signing_keys FROM nce_app;
        END IF;
        IF gc_exists THEN
            REVOKE ALL ON TABLE public.signing_keys FROM nce_gc;
        END IF;
    END IF;

    PERFORM nce_enforce_partition_rls_and_worm();

    REVOKE ALL ON FUNCTION nce_ensure_event_log_monthly_partitions(int) FROM PUBLIC;
    REVOKE ALL ON FUNCTION nce_lookup_active_bridge_subscription(text, text) FROM PUBLIC;
    REVOKE ALL ON FUNCTION nce_resolve_signing_session_namespace(text) FROM PUBLIC;
    REVOKE ALL ON FUNCTION nce_count_signing_keys() FROM PUBLIC;
    REVOKE ALL ON FUNCTION nce_get_active_signing_key() FROM PUBLIC;
    REVOKE ALL ON FUNCTION nce_get_signing_key_by_id(text) FROM PUBLIC;
    REVOKE ALL ON FUNCTION nce_get_signing_keys_status() FROM PUBLIC;
    REVOKE ALL ON FUNCTION nce_rotate_signing_key(text, bytea) FROM PUBLIC;
    REVOKE ALL ON FUNCTION nce_enforce_partition_rls_and_worm() FROM PUBLIC;
    REVOKE ALL ON FUNCTION nce_sync_runtime_role_grants() FROM PUBLIC;
    IF app_exists THEN
        GRANT EXECUTE ON FUNCTION nce_ensure_event_log_monthly_partitions(int) TO nce_app;
        GRANT EXECUTE ON FUNCTION nce_lookup_active_bridge_subscription(text, text) TO nce_app;
        GRANT EXECUTE ON FUNCTION nce_resolve_signing_session_namespace(text) TO nce_app;
        GRANT EXECUTE ON FUNCTION nce_count_signing_keys() TO nce_app;
        GRANT EXECUTE ON FUNCTION nce_get_active_signing_key() TO nce_app;
        GRANT EXECUTE ON FUNCTION nce_get_signing_key_by_id(text) TO nce_app;
        GRANT EXECUTE ON FUNCTION nce_get_signing_keys_status() TO nce_app;
        GRANT EXECUTE ON FUNCTION nce_rotate_signing_key(text, bytea) TO nce_app;
        REVOKE ALL ON FUNCTION nce_enforce_partition_rls_and_worm() FROM nce_app;
        REVOKE ALL ON FUNCTION nce_sync_runtime_role_grants() FROM nce_app;
    END IF;
    IF gc_exists THEN
        GRANT EXECUTE ON FUNCTION nce_ensure_event_log_monthly_partitions(int) TO nce_gc;
        GRANT EXECUTE ON FUNCTION nce_lookup_active_bridge_subscription(text, text) TO nce_gc;
        GRANT EXECUTE ON FUNCTION nce_resolve_signing_session_namespace(text) TO nce_gc;
        GRANT EXECUTE ON FUNCTION nce_count_signing_keys() TO nce_gc;
        GRANT EXECUTE ON FUNCTION nce_get_active_signing_key() TO nce_gc;
        GRANT EXECUTE ON FUNCTION nce_get_signing_key_by_id(text) TO nce_gc;
        GRANT EXECUTE ON FUNCTION nce_get_signing_keys_status() TO nce_gc;
        GRANT EXECUTE ON FUNCTION nce_rotate_signing_key(text, bytea) TO nce_gc;
        REVOKE ALL ON FUNCTION nce_enforce_partition_rls_and_worm() FROM nce_gc;
        REVOKE ALL ON FUNCTION nce_sync_runtime_role_grants() FROM nce_gc;
    END IF;
END;
$fn$;

SELECT nce_sync_runtime_role_grants();
