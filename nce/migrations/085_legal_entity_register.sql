-- 085_legal_entity_register.sql
--
-- C15 Legal-Entity Register (Wave A-5)
-- Multi-tenant legal entities register (org.nr, name, group parent, roles).

BEGIN;

CREATE TABLE IF NOT EXISTS legal_entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    org_nr VARCHAR(32) NOT NULL,
    name VARCHAR(256) NOT NULL,
    group_parent_org_nr VARCHAR(32),
    roles JSONB NOT NULL DEFAULT '[]'::jsonb,
    country VARCHAR(8) NOT NULL DEFAULT 'NO',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    archived BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_legal_entities_namespace_org_nr UNIQUE (namespace_id, org_nr)
);

CREATE INDEX IF NOT EXISTS idx_legal_entities_lookup
    ON legal_entities (namespace_id, archived, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_legal_entities_org_nr
    ON legal_entities (namespace_id, org_nr);

CREATE INDEX IF NOT EXISTS idx_legal_entities_name
    ON legal_entities (namespace_id, name);

CREATE INDEX IF NOT EXISTS idx_legal_entities_group_parent
    ON legal_entities (namespace_id, group_parent_org_nr);

ALTER TABLE legal_entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE legal_entities FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'legal_entities' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON legal_entities
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

COMMIT;
