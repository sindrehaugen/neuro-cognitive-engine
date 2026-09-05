-- 065_support_service_tickets.sql
-- ============================================================================
-- Support Engine (Module 10, Wave 1 -- support-schema):
-- Tables backing nce/vertical_modules/support/** and unblocking Copper waves B191-B193:
--   1. `service_tickets` (native ServiceTicket store)
--   2. `sla_clocks` (fast queue-board countdown reads)
--   3. `customer_health` (rolling customer health & churn-risk score)
--
-- MIGRATION NUMBER IS ALLOCATED (065)
-- --------------------------------------------------------------------------
-- main runs through 063. PR #205 reserves 064 (064_product_catalog_global.sql).
-- 065 is the allocated number for Module 10 Support Engine.
--
-- STRICT ROW LEVEL SECURITY + EXPLICIT NAMESPACE PREDICATE ENFORCEMENT
-- --------------------------------------------------------------------------
-- All three tables enable and force RLS for nce_app.
-- As documented in Charter §5.5, the live environment connects as mcp_user
-- (rolsuper=true, rolbypassrls=true). Therefore, RLS policies are defense-in-depth;
-- EVERY application query must carry explicit WHERE namespace_id = $1 predicates.
-- ============================================================================

CREATE TABLE IF NOT EXISTS service_tickets (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    source                 TEXT        NOT NULL DEFAULT 'nce',
    source_id              TEXT,
    asset_id               UUID        REFERENCES assets(id) ON DELETE SET NULL,
    room_id                TEXT,
    customer_id            TEXT,
    status                 TEXT        NOT NULL DEFAULT 'open',
    priority               TEXT        NOT NULL DEFAULT 'medium',
    summary                TEXT        NOT NULL,
    description            TEXT,
    sla_profile            TEXT        NOT NULL DEFAULT 'standard',
    first_response_at      TIMESTAMPTZ,
    resolved_at            TIMESTAMPTZ,
    ai_diagnosis           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    events                 JSONB       NOT NULL DEFAULT '[]'::jsonb,
    support_source_id      TEXT,
    change_origin          TEXT        NOT NULL DEFAULT 'agent',
    synced_at              TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT service_tickets_summary_not_blank
        CHECK (btrim(summary) <> ''),
    CONSTRAINT service_tickets_source_check
        CHECK (source IN ('nce', 'd365')),
    CONSTRAINT service_tickets_status_check
        CHECK (status IN ('open', 'in_progress', 'waiting_customer', 'waiting_parts', 'resolved', 'closed', 'cancelled')),
    CONSTRAINT service_tickets_priority_check
        CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    CONSTRAINT service_tickets_change_origin_check
        CHECK (change_origin IN ('sync','webhook','agent','operator','consolidation','replay','unknown'))
);

CREATE INDEX IF NOT EXISTS idx_service_tickets_ns_status
    ON service_tickets (namespace_id, status);

CREATE INDEX IF NOT EXISTS idx_service_tickets_ns_room
    ON service_tickets (namespace_id, room_id)
    WHERE room_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_service_tickets_ns_customer
    ON service_tickets (namespace_id, customer_id)
    WHERE customer_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_service_tickets_ns_asset
    ON service_tickets (namespace_id, asset_id)
    WHERE asset_id IS NOT NULL;

ALTER TABLE service_tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_tickets FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON service_tickets;
CREATE POLICY tenant_isolation_policy ON service_tickets
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE service_tickets FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE service_tickets TO nce_app;
    END IF;
END $$;


-- ============================================================================
-- 2. sla_clocks
-- ============================================================================

CREATE TABLE IF NOT EXISTS sla_clocks (
    ticket_id              UUID        NOT NULL REFERENCES service_tickets(id) ON DELETE CASCADE,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    sla_profile            TEXT        NOT NULL,
    first_response_due     TIMESTAMPTZ,
    resolution_due         TIMESTAMPTZ,
    breached               BOOLEAN     NOT NULL DEFAULT FALSE,
    breach_type            TEXT,
    paused_intervals       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticket_id),
    CONSTRAINT sla_clocks_breach_type_check
        CHECK (breach_type IS NULL OR breach_type IN ('first_response', 'resolution', 'both'))
);

CREATE INDEX IF NOT EXISTS idx_sla_clocks_ns_breached
    ON sla_clocks (namespace_id, breached);

CREATE INDEX IF NOT EXISTS idx_sla_clocks_ns_resolution_due
    ON sla_clocks (namespace_id, resolution_due)
    WHERE resolution_due IS NOT NULL;

ALTER TABLE sla_clocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE sla_clocks FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON sla_clocks;
CREATE POLICY tenant_isolation_policy ON sla_clocks
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE sla_clocks FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE sla_clocks TO nce_app;
    END IF;
END $$;


-- ============================================================================
-- 3. customer_health
-- ============================================================================

CREATE TABLE IF NOT EXISTS customer_health (
    customer_id            TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    score                  NUMERIC(5,2) NOT NULL,
    trend                  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    churn_risk             TEXT        NOT NULL,
    drivers                JSONB       NOT NULL DEFAULT '[]'::jsonb,
    last_touchpoint_at     TIMESTAMPTZ,
    computed_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, customer_id),
    CONSTRAINT customer_health_customer_id_not_blank
        CHECK (btrim(customer_id) <> ''),
    CONSTRAINT customer_health_score_range
        CHECK (score >= 0.00 AND score <= 100.00),
    CONSTRAINT customer_health_churn_risk_check
        CHECK (churn_risk IN ('low', 'medium', 'high', 'critical'))
);

CREATE INDEX IF NOT EXISTS idx_customer_health_ns_churn_risk
    ON customer_health (namespace_id, churn_risk);

ALTER TABLE customer_health ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_health FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON customer_health;
CREATE POLICY tenant_isolation_policy ON customer_health
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE customer_health FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE customer_health TO nce_app;
    END IF;
END $$;
