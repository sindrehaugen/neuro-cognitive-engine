-- 041_app_connections_vault.sql
--
-- v3.6.0 Multi-Tenant App Connector Connection Vault
-- 1. app_connections table holding envelope-encrypted connector configs per tenant & app slug
-- 2. Strict FORCE ROW LEVEL SECURITY bound to (SELECT get_nce_namespace())
-- 3. Runtime role least-privilege grants synchronized with nce_app & nce_gc

BEGIN;

CREATE TABLE IF NOT EXISTS app_connections (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id      UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    app_slug          TEXT NOT NULL,
    connection_name   TEXT NOT NULL DEFAULT 'default',
    auth_kind         TEXT NOT NULL DEFAULT 'api_key'
        CONSTRAINT ck_app_connections_auth_kind
        CHECK (auth_kind IN ('oauth2', 'api_key', 'entra_oidc', 'webhook', 'local_file', 'none')),
    endpoint_url      TEXT,
    status            TEXT NOT NULL DEFAULT 'connected'
        CONSTRAINT ck_app_connections_status
        CHECK (status IN ('connected', 'disconnected', 'error', 'pending')),
    scopes            JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    encrypted_config  JSONB NOT NULL,
    dek_id            UUID,
    kek_version       TEXT,
    last_verified_at  TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_app_connections_ns_slug_name UNIQUE (namespace_id, app_slug, connection_name)
);

CREATE INDEX IF NOT EXISTS idx_app_connections_ns_app
    ON app_connections (namespace_id, app_slug);

CREATE INDEX IF NOT EXISTS idx_app_connections_ns_status
    ON app_connections (namespace_id, status);

ALTER TABLE app_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_connections FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'app_connections' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON app_connections
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = (SELECT get_nce_namespace()))
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = (SELECT get_nce_namespace()));
    END IF;
END $$;

REVOKE ALL ON TABLE app_connections FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE app_connections FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE app_connections TO nce_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_gc') THEN
        REVOKE ALL ON TABLE app_connections FROM nce_gc;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE app_connections TO nce_gc;
    END IF;
    IF to_regprocedure('public.nce_sync_runtime_role_grants()') IS NOT NULL THEN
        PERFORM nce_sync_runtime_role_grants();
    END IF;
END $$;

COMMIT;
