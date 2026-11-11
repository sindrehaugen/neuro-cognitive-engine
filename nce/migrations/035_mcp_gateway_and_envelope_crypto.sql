-- 035_mcp_gateway_and_envelope_crypto.sql
--
-- v3.3.0 Zero-Trust Streamable HTTP /mcp Gateway, KEK/DEK Envelope Encryption & Namespace Physics
-- 1. mcp_client_tokens: scoped SHA-256 bearer tokens with progressive tool_exposure_mode & allowed_engines
-- 2. encryption_deks: per-tenant & per-subject AES-256-GCM DEK store supporting GDPR crypto-shredding
-- 3. namespaces: embedding dimension/provider/model pinning & is_replay_sandbox fencing flag

BEGIN;

-- 1. Namespace embedding pinning & replay sandbox columns
ALTER TABLE namespaces ADD COLUMN IF NOT EXISTS embedding_provider TEXT;
ALTER TABLE namespaces ADD COLUMN IF NOT EXISTS embedding_model TEXT;
ALTER TABLE namespaces ADD COLUMN IF NOT EXISTS embedding_dim INT;
ALTER TABLE namespaces ADD COLUMN IF NOT EXISTS is_replay_sandbox BOOLEAN NOT NULL DEFAULT false;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_namespaces_embedding_dim_positive'
    ) THEN
        ALTER TABLE namespaces
            ADD CONSTRAINT ck_namespaces_embedding_dim_positive
            CHECK (embedding_dim IS NULL OR (embedding_dim > 0 AND embedding_dim <= 8192));
    END IF;
END $$;

CREATE OR REPLACE FUNCTION enforce_namespace_embedding_pin()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.embedding_dim IS NOT NULL AND NEW.embedding_dim IS NOT NULL AND OLD.embedding_dim <> NEW.embedding_dim THEN
        RAISE EXCEPTION 'Namespace % embedding_dim is pinned to %; cannot mutate to % without re-embedding migration',
            OLD.id, OLD.embedding_dim, NEW.embedding_dim
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.embedding_provider IS NOT NULL AND NEW.embedding_provider IS NOT NULL AND OLD.embedding_provider <> NEW.embedding_provider THEN
        RAISE EXCEPTION 'Namespace % embedding_provider is pinned to %; cannot mutate to %',
            OLD.id, OLD.embedding_provider, NEW.embedding_provider
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.embedding_model IS NOT NULL AND NEW.embedding_model IS NOT NULL AND OLD.embedding_model <> NEW.embedding_model THEN
        RAISE EXCEPTION 'Namespace % embedding_model is pinned to %; cannot mutate to %',
            OLD.id, OLD.embedding_model, NEW.embedding_model
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_namespaces_embedding_pin ON namespaces;
CREATE TRIGGER trg_namespaces_embedding_pin
    BEFORE UPDATE ON namespaces
    FOR EACH ROW
    EXECUTE FUNCTION enforce_namespace_embedding_pin();

-- 2. mcp_client_tokens table
CREATE TABLE IF NOT EXISTS mcp_client_tokens (
    token_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_sha256 TEXT UNIQUE NOT NULL,
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    principal_id TEXT NOT NULL,
    scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
    allowed_engines TEXT[] NOT NULL DEFAULT ARRAY['*']::TEXT[],
    tool_exposure_mode TEXT NOT NULL DEFAULT 'compact'
        CONSTRAINT ck_mcp_client_tokens_exposure_mode CHECK (tool_exposure_mode IN ('compact', 'full')),
    rate_limit_rpm INT NOT NULL DEFAULT 120
        CONSTRAINT ck_mcp_client_tokens_rate_limit CHECK (rate_limit_rpm > 0),
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mcp_client_tokens_namespace_id
    ON mcp_client_tokens (namespace_id);

CREATE INDEX IF NOT EXISTS idx_mcp_client_tokens_sha256
    ON mcp_client_tokens (token_sha256);

ALTER TABLE mcp_client_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE mcp_client_tokens FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'mcp_client_tokens' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON mcp_client_tokens
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE mcp_client_tokens FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE mcp_client_tokens TO nce_app;
    END IF;
END $$;

-- SHA-256 ingress lookup function (resolves token at gateway ingress prior to namespace context binding)
CREATE OR REPLACE FUNCTION lookup_mcp_client_token(p_token_sha256 TEXT)
RETURNS TABLE (
    token_id UUID,
    namespace_id UUID,
    principal_id TEXT,
    scopes JSONB,
    allowed_engines TEXT[],
    tool_exposure_mode TEXT,
    rate_limit_rpm INT,
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    RETURN QUERY
    SELECT
        t.token_id,
        t.namespace_id,
        t.principal_id,
        t.scopes,
        t.allowed_engines,
        t.tool_exposure_mode,
        t.rate_limit_rpm,
        t.expires_at,
        t.revoked_at
    FROM public.mcp_client_tokens t
    WHERE t.token_sha256 = p_token_sha256
      AND t.revoked_at IS NULL
      AND (t.expires_at IS NULL OR t.expires_at > NOW())
    LIMIT 1;
END;
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT EXECUTE ON FUNCTION lookup_mcp_client_token(TEXT) TO nce_app;
    END IF;
END $$;

-- 3. encryption_deks table (KEK/DEK envelope crypto & GDPR crypto-shredding)
CREATE TABLE IF NOT EXISTS encryption_deks (
    dek_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    subject_ref TEXT,
    wrapped_key BYTEA NOT NULL,
    kek_version TEXT NOT NULL,
    shredded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_encryption_deks_namespace_id
    ON encryption_deks (namespace_id);

CREATE INDEX IF NOT EXISTS idx_encryption_deks_namespace_subject
    ON encryption_deks (namespace_id, subject_ref);

ALTER TABLE encryption_deks ENABLE ROW LEVEL SECURITY;
ALTER TABLE encryption_deks FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'encryption_deks' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON encryption_deks
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE encryption_deks FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE encryption_deks TO nce_app;
    END IF;
END $$;

COMMIT;
