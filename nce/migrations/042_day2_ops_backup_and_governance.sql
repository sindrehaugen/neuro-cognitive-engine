-- 042_day2_ops_backup_and_governance.sql
--
-- v3.7.0 Day-2 Operations Control Plane, Backup Leases/Manifests & High-Churn Vacuum Tuning:
--   1. Control-plane tables (Admin & nce_gc roles only; tenant /mcp role nce_app has no direct table access):
--        - runtime_tool_governance
--        - backup_leases
--        - backup_manifests
--        - ops_probe_runs (+ (probe_name, created_at DESC) index)
--        - ops_incidents
--        - ops_quarantine
--        - ops_playbook_executions
--   2. High-churn autovacuum tuning on memory_salience, kg_edges, and ops_probe_runs.
--   3. Narrow SECURITY DEFINER helpers (nce_get_disabled_runtime_tools, nce_is_backup_lease_active)
--      and updated nce_sync_runtime_role_grants() maintaining strict nce_app confinement.

BEGIN;

CREATE TABLE IF NOT EXISTS public.runtime_tool_governance (
    scope_key   TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    reason      TEXT,
    updated_by  TEXT NOT NULL DEFAULT 'system',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_key, tool_name)
);

CREATE TABLE IF NOT EXISTS public.backup_leases (
    lease_name   TEXT PRIMARY KEY,
    holder_id    TEXT NOT NULL,
    acquired_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS public.backup_manifests (
    backup_id           UUID PRIMARY KEY,
    t_cut               TIMESTAMPTZ NOT NULL,
    t_blob_completed    TIMESTAMPTZ,
    archive_path        TEXT NOT NULL,
    archive_sha256      TEXT NOT NULL,
    size_bytes          BIGINT NOT NULL,
    master_key_hmac_fp  TEXT NOT NULL,
    schema_heads        JSONB NOT NULL,
    worm_heads          JSONB NOT NULL,
    tombstone_count     INT NOT NULL DEFAULT 0,
    status              TEXT NOT NULL,
    last_drill_at       TIMESTAMPTZ,
    last_drill_status   TEXT,
    last_drill_report   JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.ops_probe_runs (
    run_id       UUID PRIMARY KEY,
    probe_name   TEXT NOT NULL,
    status       TEXT NOT NULL,
    metrics_json JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ops_probe_runs_probe_created
    ON public.ops_probe_runs (probe_name, created_at DESC);

CREATE TABLE IF NOT EXISTS public.ops_incidents (
    incident_id          UUID PRIMARY KEY,
    fingerprint          TEXT NOT NULL UNIQUE,
    severity             TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'open',
    title                TEXT NOT NULL,
    deterministic_facts  JSONB NOT NULL,
    elm_brief_json       JSONB,
    elm_mode_used        TEXT NOT NULL DEFAULT 'none',
    recommended_playbook TEXT,
    occurrence_count     INT NOT NULL DEFAULT 1,
    first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at          TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS public.ops_quarantine (
    quarantine_id       UUID PRIMARY KEY,
    job_fingerprint     TEXT NOT NULL UNIQUE,
    queue_name          TEXT NOT NULL,
    func_name           TEXT NOT NULL,
    namespace_id        UUID,
    failure_count       INT NOT NULL,
    exit_reason         TEXT NOT NULL,
    sanitized_traceback TEXT NOT NULL,
    job_args_preview    JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              TEXT NOT NULL DEFAULT 'quarantined',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.ops_playbook_executions (
    execution_id  UUID PRIMARY KEY,
    incident_id   UUID,
    playbook_id   TEXT NOT NULL,
    dry_run       BOOLEAN NOT NULL,
    approved_by   TEXT NOT NULL,
    result_json   JSONB NOT NULL,
    executed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- High-churn autovacuum tuning to prevent dead-tuple and HNSW index bloat.
-- Partitioned parent tables disallow reloptions, so partitioned targets tune leaf partitions.
DO $$
DECLARE
    target_tbl text;
    tbl_kind "char";
    leaf record;
BEGIN
    FOREACH target_tbl IN ARRAY ARRAY['memory_salience', 'kg_edges', 'ops_probe_runs']
    LOOP
        SELECT c.relkind INTO tbl_kind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = target_tbl;

        IF tbl_kind = 'r' THEN
            EXECUTE format(
                'ALTER TABLE public.%I SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02)',
                target_tbl
            );
        ELSIF tbl_kind = 'p' THEN
            FOR leaf IN
                WITH RECURSIVE part_tree AS (
                    SELECT c.oid, c.relkind, c.relname
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = target_tbl
                    UNION ALL
                    SELECT ch.oid, ch.relkind, ch.relname
                    FROM part_tree pt
                    JOIN pg_inherits i ON i.inhparent = pt.oid
                    JOIN pg_class ch ON ch.oid = i.inhrelid
                )
                SELECT pt.relname
                FROM part_tree pt
                WHERE pt.relkind = 'r'
            LOOP
                EXECUTE format(
                    'ALTER TABLE public.%I SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02)',
                    leaf.relname
                );
            END LOOP;
        END IF;
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION nce_get_disabled_runtime_tools(p_scope_key text DEFAULT NULL)
RETURNS TABLE(scope_key text, tool_name text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
BEGIN
    RETURN QUERY
    SELECT rtg.scope_key, rtg.tool_name
    FROM public.runtime_tool_governance rtg
    WHERE rtg.enabled = false
      AND (p_scope_key IS NULL OR rtg.scope_key = p_scope_key);
END;
$fn$;

CREATE OR REPLACE FUNCTION nce_is_backup_lease_active(p_lease_name text DEFAULT 't_cut_backup')
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
BEGIN
    RETURN EXISTS (
        SELECT 1
        FROM public.backup_leases bl
        WHERE bl.lease_name = COALESCE(NULLIF(btrim(p_lease_name), ''), 't_cut_backup')
          AND bl.expires_at > now()
    );
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
    ctl text;
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
    day2_ops_tables text[] := ARRAY[
        'runtime_tool_governance',
        'backup_leases',
        'backup_manifests',
        'ops_probe_runs',
        'ops_incidents',
        'ops_quarantine',
        'ops_playbook_executions'
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

    -- Confinement for Day-2 Ops control-plane tables: admin & nce_gc only, never nce_app.
    FOREACH ctl IN ARRAY day2_ops_tables
    LOOP
        IF to_regclass(format('public.%I', ctl)) IS NOT NULL THEN
            EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC', ctl);
            IF app_exists THEN
                EXECUTE format('REVOKE ALL ON TABLE public.%I FROM nce_app', ctl);
            END IF;
            IF gc_exists THEN
                EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO nce_gc', ctl);
            END IF;
        END IF;
    END LOOP;

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
    REVOKE ALL ON FUNCTION nce_get_disabled_runtime_tools(text) FROM PUBLIC;
    REVOKE ALL ON FUNCTION nce_is_backup_lease_active(text) FROM PUBLIC;
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
        GRANT EXECUTE ON FUNCTION nce_get_disabled_runtime_tools(text) TO nce_app;
        GRANT EXECUTE ON FUNCTION nce_is_backup_lease_active(text) TO nce_app;
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
        GRANT EXECUTE ON FUNCTION nce_get_disabled_runtime_tools(text) TO nce_gc;
        GRANT EXECUTE ON FUNCTION nce_is_backup_lease_active(text) TO nce_gc;
        REVOKE ALL ON FUNCTION nce_enforce_partition_rls_and_worm() FROM nce_gc;
        REVOKE ALL ON FUNCTION nce_sync_runtime_role_grants() FROM nce_gc;
    END IF;
END;
$fn$;

SELECT nce_sync_runtime_role_grants();

COMMIT;
