-- =============================================================================
-- 025_diagnostics.sql
-- Diagnostic Log Digestion Engine — Phase 1: schema foundation
-- Batch 67 — diag-schema
--
-- NOTE: The prompt cites migration number 019, but 019 is already taken by
-- 019_halfvec_embeddings.sql.  Current max on main branch is 024
-- (024_d365_change_tracking.sql); this file is therefore numbered 025.
--
-- What this migration does
-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Creates three diagnostics tables:
--      • diag_ingestions      — per-upload/API/ticketing ingestion record
--      • diag_anomalies       — anomalies extracted from an ingestion
--      • device_health_rollup — per-device latest health aggregate
-- 2. Adds a unique index on topology_graph(namespace_id, source_node_id,
--    target_node_id, edge_type) to close the duplicate-edge gap.
-- 3. Applies RLS (ENABLE + FORCE + tenant_isolation_policy) to all three
--    diag tables via the standard tenant_tables loop pattern.
-- 4. Security fix — Domain 5 / D1 (HIGH): topology_graph has had
--    ENABLE ROW LEVEL SECURITY since migration 010, but was never FORCEd,
--    meaning the table owner / nce_gc role (BYPASSRLS-adjacent) could read
--    every tenant's network topology with nce.namespace_id unset.
--    This migration adds:
--        ALTER TABLE topology_graph FORCE ROW LEVEL SECURITY;
--    idempotent; kept as a standalone ALTER (not folded into the loop)
--    because topology_graph carries a differently-named policy
--    (topology_graph_tenant_isolation) created in migration 010.
--
-- All DDL is idempotent (IF NOT EXISTS / DO $$ guards).
-- Never edits migration 010.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1.  diag_ingestions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS diag_ingestions (
    id                 UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id       UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    ingest_id          TEXT        NOT NULL,
    source             TEXT        CHECK (source IN ('upload', 'api', 'ticketing')),
    vendor_profile     TEXT,
    device_slug        TEXT,
    landing_uri        TEXT,
    status             TEXT        NOT NULL DEFAULT 'PENDING'
                                   CHECK (status IN ('PENDING', 'PROCESSING', 'DIGESTED', 'FAILED')),
    bytes              BIGINT,
    processed_lines    BIGINT,
    anomaly_count      INT,
    digest_payload_ref TEXT,
    created_at         TIMESTAMPTZ DEFAULT now(),
    updated_at         TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (namespace_id, ingest_id)
);

CREATE INDEX IF NOT EXISTS idx_diag_ingestions_namespace_id
    ON diag_ingestions (namespace_id);

COMMENT ON TABLE diag_ingestions IS
'Diagnostic Log Digestion Engine — Phase 1.
Per-ingestion record (upload / API push / ticketing pull).
RLS: tenant_isolation_policy via get_nce_namespace() GUC.
status lifecycle: PENDING → PROCESSING → DIGESTED | FAILED.';

-- ---------------------------------------------------------------------------
-- 2.  diag_anomalies
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS diag_anomalies (
    id             UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id   UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    ingestion_id   UUID        NOT NULL REFERENCES diag_ingestions(id) ON DELETE CASCADE,
    device_slug    TEXT,
    anomaly_type   TEXT,
    severity       INT,
    first_line     BIGINT,
    occurrences    INT,
    -- sample is truncated to ≤200 chars by the writer before INSERT
    sample         TEXT,
    window_start   TIMESTAMPTZ,
    window_end     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_diag_anomalies_namespace_id
    ON diag_anomalies (namespace_id);

CREATE INDEX IF NOT EXISTS idx_diag_anomalies_ingestion_id
    ON diag_anomalies (ingestion_id);

COMMENT ON TABLE diag_anomalies IS
'Diagnostic Log Digestion Engine — Phase 1.
Anomalies extracted from a single diag_ingestions row.
sample column MUST be truncated to ≤200 chars by the writer (no PII allowed).
RLS: tenant_isolation_policy via get_nce_namespace() GUC.';

-- ---------------------------------------------------------------------------
-- 3.  device_health_rollup
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS device_health_rollup (
    namespace_id      UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    device_slug       TEXT        NOT NULL,
    health_state      TEXT        CHECK (health_state IN ('HEALTHY', 'DEGRADED', 'CRITICAL')),
    top_anomaly_type  TEXT,
    anomaly_score     FLOAT8,
    last_ingestion_id UUID,
    last_seen_at      TIMESTAMPTZ,
    PRIMARY KEY (namespace_id, device_slug)
);

-- namespace_id is part of the PK so the PK index already covers it, but an
-- explicit single-column index helps the RLS qual push-down on full scans.
CREATE INDEX IF NOT EXISTS idx_device_health_rollup_namespace_id
    ON device_health_rollup (namespace_id);

COMMENT ON TABLE device_health_rollup IS
'Diagnostic Log Digestion Engine — Phase 1.
Latest health aggregate per (namespace_id, device_slug).
Upserted by the digestion worker after each completed ingestion.
RLS: tenant_isolation_policy via get_nce_namespace() GUC.';

-- ---------------------------------------------------------------------------
-- 4.  topology_graph: close duplicate-edge gap
-- ---------------------------------------------------------------------------
-- Guard note: if duplicate rows already exist this index will fail with
-- "could not create unique index".  In that case run a dedup query first:
--
--   DELETE FROM topology_graph t1
--   USING topology_graph t2
--   WHERE t1.ctid > t2.ctid
--     AND t1.namespace_id    = t2.namespace_id
--     AND t1.source_node_id  = t2.source_node_id
--     AND t1.target_node_id  = t2.target_node_id
--     AND t1.edge_type       = t2.edge_type;
--
-- This migration does NOT issue the destructive DELETE automatically; the DBA
-- should verify absence of duplicates on production data before applying.
-- The integration DB is fresh-initialized and has no rows, so this is safe.

CREATE UNIQUE INDEX IF NOT EXISTS uq_topology_edge
    ON topology_graph (namespace_id, source_node_id, target_node_id, edge_type);

-- ---------------------------------------------------------------------------
-- 5.  Apply RLS to the three new diag tables
--     (mirrors the tenant_tables loop pattern in schema.sql)
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    t text;
    diag_tables text[] := ARRAY[
        'diag_ingestions',
        'diag_anomalies',
        'device_health_rollup'
    ];
BEGIN
    FOREACH t IN ARRAY diag_tables
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
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO nce_app',
            t
        );
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 6.  Security fix — Domain 5 / D1 HIGH: FORCE RLS on topology_graph
--
--     topology_graph was created in 010_citus_sharding.sql with
--     ENABLE ROW LEVEL SECURITY but NEVER FORCE'd.  The table-owner and
--     nce_gc role (which holds BYPASSRLS-equivalent privileges) could bypass
--     the topology_graph_tenant_isolation policy when nce.namespace_id is not
--     set in their session — exposing every tenant's infrastructure topology.
--
--     ALTER TABLE … FORCE ROW LEVEL SECURITY is idempotent (safe to re-run).
--     We keep this as a standalone statement (not folded into the loop above)
--     because topology_graph carries policy name topology_graph_tenant_isolation
--     (created in migration 010), whereas the loop creates tenant_isolation_policy.
--     Folding would drop the existing, correctly-named policy and recreate it
--     under the new name — a behaviour change outside this batch's scope.
-- ---------------------------------------------------------------------------

ALTER TABLE topology_graph FORCE ROW LEVEL SECURITY;
