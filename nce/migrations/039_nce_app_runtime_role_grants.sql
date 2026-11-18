-- 039_nce_app_runtime_role_grants.sql
--
-- Three-role runtime PostgreSQL privilege separation:
--   1. Enable LOGIN on nce_gc (NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS).
--   2. Mark nce_ensure_event_log_monthly_partitions(int) SECURITY DEFINER.
--   3. Provide narrow SECURITY DEFINER lookup helpers for webhook-receiver under nce_app.
--   4. Grant missing global table / sequence rights to nce_app and least-privilege
--      mirror rights to nce_gc while revoking UPDATE/DELETE/TRUNCATE on WORM tables.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_gc') THEN
        CREATE ROLE nce_gc WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS PASSWORD 'nce_gc_secret';
    ELSE
        ALTER ROLE nce_gc WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
    END IF;
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
        END IF;
    END LOOP;
END;
$fn$;

CREATE OR REPLACE FUNCTION nce_lookup_active_bridge_subscription(
    p_provider text,
    p_subscription_id text
)
RETURNS SETOF public.bridge_subscriptions
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
BEGIN
    IF p_subscription_id IS NULL OR btrim(p_subscription_id) = '' THEN
        RETURN;
    END IF;
    RETURN QUERY
    SELECT *
    FROM public.bridge_subscriptions
    WHERE provider = p_provider
      AND status = 'ACTIVE'
      AND subscription_id = p_subscription_id
    LIMIT 1;
END;
$fn$;

CREATE OR REPLACE FUNCTION nce_resolve_signing_session_namespace(p_session_id text)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
    ns_id uuid;
BEGIN
    IF p_session_id IS NULL OR btrim(p_session_id) = '' THEN
        RETURN NULL;
    END IF;
    IF to_regclass('public.sales_read_model') IS NULL THEN
        RETURN NULL;
    END IF;
    EXECUTE $q$
        SELECT namespace_id
        FROM public.sales_read_model
        WHERE entity = 'quotes'
          AND (manual->>'signing_session_id' = $1 OR source_json->>'signing_session_id' = $1)
        LIMIT 1
    $q$
    INTO ns_id
    USING p_session_id;
    RETURN ns_id;
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
        IF to_regclass('public.signing_keys') IS NOT NULL THEN
            GRANT SELECT, INSERT, UPDATE ON TABLE public.signing_keys TO nce_app;
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

    REVOKE ALL ON FUNCTION nce_ensure_event_log_monthly_partitions(int) FROM PUBLIC;
    REVOKE ALL ON FUNCTION nce_lookup_active_bridge_subscription(text, text) FROM PUBLIC;
    REVOKE ALL ON FUNCTION nce_resolve_signing_session_namespace(text) FROM PUBLIC;
    IF app_exists THEN
        GRANT EXECUTE ON FUNCTION nce_ensure_event_log_monthly_partitions(int) TO nce_app;
        GRANT EXECUTE ON FUNCTION nce_lookup_active_bridge_subscription(text, text) TO nce_app;
        GRANT EXECUTE ON FUNCTION nce_resolve_signing_session_namespace(text) TO nce_app;
    END IF;
    IF gc_exists THEN
        GRANT EXECUTE ON FUNCTION nce_ensure_event_log_monthly_partitions(int) TO nce_gc;
        GRANT EXECUTE ON FUNCTION nce_lookup_active_bridge_subscription(text, text) TO nce_gc;
        GRANT EXECUTE ON FUNCTION nce_resolve_signing_session_namespace(text) TO nce_gc;
    END IF;
END;
$fn$;

SELECT nce_sync_runtime_role_grants();
