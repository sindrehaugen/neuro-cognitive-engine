-- 068_hr_engine.sql
-- ============================================================================
-- HR Engine (Module 13, Wave 1 -- hr-schema):
-- Tables backing nce/vertical_modules/hr/**:
--   1. employees (native employee profile card & identity)
--   2. skills (employee-skill relations & assessment levels)
--   3. certifications (cert lifecycle, authority & expiry tracking for Watcher)
--   4. absences (sensitive leave/sick records & Norwegian compliance state)
--
-- STRICT ROW LEVEL SECURITY + EXPLICIT NAMESPACE PREDICATE ENFORCEMENT
-- --------------------------------------------------------------------------
-- All four tables enable and force RLS for nce_app.
-- As documented in Charter section 5.4, the live environment connects as mcp_user
-- (rolsuper=true, rolbypassrls=true). Therefore, RLS policies are defense-in-depth;
-- EVERY application query must carry explicit WHERE namespace_id = $1 predicates.
-- Derived rows carry hr_source_id for GDPR erasure (RL-3).
-- ============================================================================

-- ============================================================================
-- 1. employees
-- ============================================================================

CREATE TABLE IF NOT EXISTS employees (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    employee_id            TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    name                   TEXT        NOT NULL,
    email                  TEXT,
    role                   TEXT        NOT NULL DEFAULT 'technician',
    department             TEXT        NOT NULL DEFAULT 'operations',
    location_id            TEXT,
    leave_balance          NUMERIC     NOT NULL DEFAULT 25.0,
    active                 BOOLEAN     NOT NULL DEFAULT true,
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    hr_source_id           TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT employees_id_ns_unique UNIQUE (employee_id, namespace_id)
);

CREATE INDEX IF NOT EXISTS idx_employees_ns_active ON employees (namespace_id, active);
CREATE INDEX IF NOT EXISTS idx_employees_ns_dept ON employees (namespace_id, department);
CREATE INDEX IF NOT EXISTS idx_employees_ns_role ON employees (namespace_id, role);
CREATE INDEX IF NOT EXISTS idx_employees_ns_location ON employees (namespace_id, location_id) WHERE location_id IS NOT NULL;

ALTER TABLE employees ENABLE ROW LEVEL SECURITY;
ALTER TABLE employees FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON employees;
CREATE POLICY tenant_isolation_policy ON employees
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE employees FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE employees TO nce_app;
    END IF;
END $$;


-- ============================================================================
-- 2. skills
-- ============================================================================

CREATE TABLE IF NOT EXISTS skills (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    skill_id               TEXT        NOT NULL,
    employee_id            TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    name                   TEXT        NOT NULL,
    category               TEXT        NOT NULL DEFAULT 'general',
    level                  TEXT        NOT NULL DEFAULT 'intermediate',
    assessed_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    hr_source_id           TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT skills_emp_skill_ns_unique UNIQUE (employee_id, skill_id, namespace_id),
    CONSTRAINT fk_skills_employees FOREIGN KEY (employee_id, namespace_id)
        REFERENCES employees (employee_id, namespace_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_skills_ns_emp ON skills (namespace_id, employee_id);
CREATE INDEX IF NOT EXISTS idx_skills_ns_name ON skills (namespace_id, name);

ALTER TABLE skills ENABLE ROW LEVEL SECURITY;
ALTER TABLE skills FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON skills;
CREATE POLICY tenant_isolation_policy ON skills
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE skills FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE skills TO nce_app;
    END IF;
END $$;


-- ============================================================================
-- 3. certifications
-- ============================================================================

CREATE TABLE IF NOT EXISTS certifications (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    cert_id                TEXT        NOT NULL,
    employee_id            TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    authority              TEXT        NOT NULL,
    name                   TEXT        NOT NULL,
    issued                 TIMESTAMPTZ NOT NULL,
    valid_to               TIMESTAMPTZ,
    status                 TEXT        NOT NULL DEFAULT 'active',
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    hr_source_id           TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT certs_id_ns_unique UNIQUE (cert_id, namespace_id),
    CONSTRAINT fk_certs_employees FOREIGN KEY (employee_id, namespace_id)
        REFERENCES employees (employee_id, namespace_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_certs_ns_emp ON certifications (namespace_id, employee_id);
CREATE INDEX IF NOT EXISTS idx_certs_ns_valid_to ON certifications (namespace_id, valid_to);
CREATE INDEX IF NOT EXISTS idx_certs_ns_status ON certifications (namespace_id, status);

ALTER TABLE certifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE certifications FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON certifications;
CREATE POLICY tenant_isolation_policy ON certifications
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE certifications FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE certifications TO nce_app;
    END IF;
END $$;


-- ============================================================================
-- 4. absences
-- ============================================================================

CREATE TABLE IF NOT EXISTS absences (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    absence_id             TEXT        NOT NULL,
    employee_id            TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    type                   TEXT        NOT NULL DEFAULT 'sick',
    start_date             TIMESTAMPTZ NOT NULL,
    end_date               TIMESTAMPTZ,
    days                   NUMERIC     NOT NULL DEFAULT 1.0,
    reason                 TEXT,
    status                 TEXT        NOT NULL DEFAULT 'pending',
    compliance_state       TEXT        NOT NULL DEFAULT 'normal',
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    hr_source_id           TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT absences_id_ns_unique UNIQUE (absence_id, namespace_id),
    CONSTRAINT fk_absences_employees FOREIGN KEY (employee_id, namespace_id)
        REFERENCES employees (employee_id, namespace_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_absences_ns_emp ON absences (namespace_id, employee_id);
CREATE INDEX IF NOT EXISTS idx_absences_ns_start ON absences (namespace_id, start_date);
CREATE INDEX IF NOT EXISTS idx_absences_ns_type ON absences (namespace_id, type);

ALTER TABLE absences ENABLE ROW LEVEL SECURITY;
ALTER TABLE absences FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON absences;
CREATE POLICY tenant_isolation_policy ON absences
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE absences FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE absences TO nce_app;
    END IF;
END $$;
