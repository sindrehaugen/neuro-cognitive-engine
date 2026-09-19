-- 094_outbound_webhooks.sql
--
-- C4 Outbound Webhooks (Wave A-7)
-- Multi-tenant outbound webhook subscriptions table for event notification delivery via transactional outbox.

BEGIN;

CREATE TABLE IF NOT EXISTS outbound_webhooks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    url VARCHAR(2048) NOT NULL,
    secret VARCHAR(512) NOT NULL,
    selectors TEXT[] NOT NULL DEFAULT '{}',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    description VARCHAR(512) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_outbound_webhooks_namespace_active
    ON outbound_webhooks (namespace_id, is_active);

ALTER TABLE outbound_webhooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbound_webhooks FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'outbound_webhooks' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON outbound_webhooks
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE outbound_webhooks FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE outbound_webhooks TO nce_app;
    END IF;
END $$;

COMMIT;
