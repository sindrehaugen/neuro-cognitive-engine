-- 067_field_tech_work_orders.sql
-- ============================================================================
-- Field Tech Engine (Module 12, Wave 1 -- field-tech-schema):
-- Tables backing nce/vertical_modules/field_tech/** and unblocking Copper waves B196-B197:
--   1. work_orders (the physical unit of field work -- install or service)
--   2. checklists (checklist instances bound to a WO; ISO9001 verification records)
--   3. time_entries (GPS-derived or manual labor spans with offline-sync op_id dedup)
--
-- MIGRATION NUMBER IS ALLOCATED (067)
-- --------------------------------------------------------------------------
-- main runs through 064. PR #209 reserves 066 (066_audit_signing_key_rotation.sql).
-- 067 is the allocated number for Module 12 Field Tech Engine.
--
-- STRICT ROW LEVEL SECURITY + EXPLICIT NAMESPACE PREDICATE ENFORCEMENT
-- --------------------------------------------------------------------------
-- All three tables enable and force RLS for nce_app.
-- As documented in Charter section 4.4, the live environment connects as mcp_user
-- (rolsuper=true, rolbypassrls=true). Therefore, RLS policies are defense-in-depth;
-- EVERY application query must carry explicit WHERE namespace_id =  predicates.
-- Partner views additionally carry partner_scope_id =  predicates.
-- ============================================================================

CREATE TABLE IF NOT EXISTS work_orders (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    work_order_id          TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    partner_scope_id       UUID,
    kind                   TEXT        NOT NULL DEFAULT 'install',
    source_kind            TEXT        NOT NULL DEFAULT 'project',
    source_ref             TEXT        NOT NULL,
    location_id            TEXT,
    assignee_id            TEXT,
    assignee_kind          TEXT,
    status                 TEXT        NOT NULL DEFAULT 'draft',
    priority               TEXT        NOT NULL DEFAULT 'medium',
    summary                TEXT        NOT NULL DEFAULT '',
    due_at                 TIMESTAMPTZ,
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    field_tech_source_id   TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT work_orders_id_ns_unique UNIQUE (work_order_id, namespace_id),
    CONSTRAINT work_orders_kind_check CHECK (kind IN ('install', 'service')),
    CONSTRAINT work_orders_source_kind_check CHECK (source_kind IN ('project', 'ticket', 'manual')),
    CONSTRAINT work_orders_status_check CHECK (status IN ('draft', 'scheduled', 'dispatched', 'in_progress', 'completed', 'cancelled')),
    CONSTRAINT work_orders_priority_check CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    CONSTRAINT work_orders_assignee_kind_check CHECK (assignee_kind IS NULL OR assignee_kind IN ('employee', 'contractor'))
);

CREATE INDEX IF NOT EXISTS idx_work_orders_ns_status ON work_orders (namespace_id, status);
CREATE INDEX IF NOT EXISTS idx_work_orders_ns_assignee ON work_orders (namespace_id, assignee_id) WHERE assignee_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_work_orders_ns_partner ON work_orders (namespace_id, partner_scope_id) WHERE partner_scope_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_work_orders_ns_location ON work_orders (namespace_id, location_id) WHERE location_id IS NOT NULL;

ALTER TABLE work_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE work_orders FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON work_orders;
CREATE POLICY tenant_isolation_policy ON work_orders
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE work_orders FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE work_orders TO nce_app;
    END IF;
END $$;


-- ============================================================================
-- 2. checklists
-- ============================================================================

CREATE TABLE IF NOT EXISTS checklists (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    checklist_id           TEXT        NOT NULL,
    work_order_id          TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    partner_scope_id       UUID,
    template_id            TEXT        NOT NULL,
    items                  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    completed_at           TIMESTAMPTZ,
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT checklists_id_ns_unique UNIQUE (checklist_id, namespace_id),
    CONSTRAINT fk_checklists_work_orders FOREIGN KEY (work_order_id, namespace_id)
        REFERENCES work_orders (work_order_id, namespace_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_checklists_ns_wo ON checklists (namespace_id, work_order_id);
CREATE INDEX IF NOT EXISTS idx_checklists_ns_partner ON checklists (namespace_id, partner_scope_id) WHERE partner_scope_id IS NOT NULL;

ALTER TABLE checklists ENABLE ROW LEVEL SECURITY;
ALTER TABLE checklists FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON checklists;
CREATE POLICY tenant_isolation_policy ON checklists
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE checklists FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE checklists TO nce_app;
    END IF;
END $$;


-- ============================================================================
-- 3. time_entries
-- ============================================================================

CREATE TABLE IF NOT EXISTS time_entries (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    time_entry_id          TEXT        NOT NULL,
    work_order_id          TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    partner_scope_id       UUID,
    started_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at               TIMESTAMPTZ,
    source                 TEXT        NOT NULL DEFAULT 'manual',
    approved               BOOLEAN     NOT NULL DEFAULT FALSE,
    op_id                  TEXT,
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT time_entries_id_ns_unique UNIQUE (time_entry_id, namespace_id),
    CONSTRAINT time_entries_op_id_unique UNIQUE (op_id, namespace_id),
    CONSTRAINT time_entries_source_check CHECK (source IN ('gps', 'manual')),
    CONSTRAINT fk_time_entries_work_orders FOREIGN KEY (work_order_id, namespace_id)
        REFERENCES work_orders (work_order_id, namespace_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_time_entries_ns_wo ON time_entries (namespace_id, work_order_id);
CREATE INDEX IF NOT EXISTS idx_time_entries_ns_approved ON time_entries (namespace_id, approved);
CREATE INDEX IF NOT EXISTS idx_time_entries_ns_partner ON time_entries (namespace_id, partner_scope_id) WHERE partner_scope_id IS NOT NULL;

ALTER TABLE time_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE time_entries FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON time_entries;
CREATE POLICY tenant_isolation_policy ON time_entries
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE time_entries FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE time_entries TO nce_app;
    END IF;
END $$;
