-- 093_principal_bindings.sql
--
-- C16 Principal Mapping (Wave A-6)
-- Multi-tenant principal bindings table mapping caller principal_id to employee/customer/contractor identities and roles.

BEGIN;

CREATE TABLE IF NOT EXISTS principal_bindings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    principal_id VARCHAR(255) NOT NULL,
    tier VARCHAR(64) NOT NULL DEFAULT 'employee',
    employee_id VARCHAR(255),
    customer_id VARCHAR(255),
    contractor_id VARCHAR(255),
    roles JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_principal_bindings_namespace_principal UNIQUE (namespace_id, principal_id)
);

CREATE INDEX IF NOT EXISTS idx_principal_bindings_lookup
    ON principal_bindings (namespace_id, principal_id);

CREATE INDEX IF NOT EXISTS idx_principal_bindings_employee
    ON principal_bindings (namespace_id, employee_id) WHERE employee_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_principal_bindings_customer
    ON principal_bindings (namespace_id, customer_id) WHERE customer_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_principal_bindings_contractor
    ON principal_bindings (namespace_id, contractor_id) WHERE contractor_id IS NOT NULL;

ALTER TABLE principal_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE principal_bindings FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'principal_bindings' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON principal_bindings
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE principal_bindings FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE principal_bindings TO nce_app;
    END IF;
END $$;

COMMIT;