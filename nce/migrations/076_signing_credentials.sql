-- 076_signing_credentials.sql
-- ============================================================================
-- Signing Provider Credentials Storage (Wave Q-4):
-- Operator-entered signing credentials for national eID & broker providers
-- (e.g. Criipto, Signicat, Oneflow):
--   - Write-only from admin UI
--   - Encrypted at rest under NCE_MASTER_KEY (AES-256-GCM / PBKDF2/Argon2id)
--   - Per-tenant namespace isolation
--   - Non-secret fingerprint for operator feedback
--
-- STRICT ROW LEVEL SECURITY + EXPLICIT NAMESPACE PREDICATE ENFORCEMENT
-- ============================================================================

CREATE TABLE IF NOT EXISTS signing_credentials (
    id                 UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id       UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    provider           TEXT        NOT NULL,
    client_id          TEXT        NOT NULL,
    encrypted_secret   BYTEA       NOT NULL,
    secret_fingerprint TEXT        NOT NULL,
    metadata           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT uq_signing_credentials_namespace_provider UNIQUE (namespace_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_signing_credentials_ns
    ON signing_credentials (namespace_id, provider);

ALTER TABLE signing_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE signing_credentials FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON signing_credentials;
CREATE POLICY tenant_isolation_policy ON signing_credentials
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE signing_credentials FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE signing_credentials TO nce_app;
    END IF;
END $$;
