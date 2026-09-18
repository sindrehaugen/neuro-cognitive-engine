-- 084_document_register.sql
--
-- C14 Document Register (Wave A-4)
-- Persistent multi-tenant document metadata register, polymorphic entity links, and expiring share tokens.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Documents table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    title VARCHAR(256) NOT NULL,
    document_ref TEXT NOT NULL,
    source_kind VARCHAR(64) NOT NULL DEFAULT 'sharepoint',
    document_kind VARCHAR(64) NOT NULL DEFAULT 'other',
    file_name VARCHAR(256),
    mime_type VARCHAR(128),
    file_size_bytes BIGINT,
    sha256 VARCHAR(64),
    tags TEXT[] NOT NULL DEFAULT '{}',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    archived BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_documents_lookup
    ON documents (namespace_id, archived, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_documents_kind
    ON documents (namespace_id, document_kind, source_kind);

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'documents' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON documents
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Document Links table (about / attachment edges to any entity)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    entity_type VARCHAR(64) NOT NULL,
    entity_id VARCHAR(256) NOT NULL,
    relation VARCHAR(64) NOT NULL DEFAULT 'about',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_document_links UNIQUE (namespace_id, document_id, entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_document_links_entity
    ON document_links (namespace_id, entity_type, entity_id);

CREATE INDEX IF NOT EXISTS idx_document_links_doc
    ON document_links (namespace_id, document_id);

ALTER TABLE document_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_links FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'document_links' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON document_links
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. Document Shares table (expiring share tokens)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_shares (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    share_id VARCHAR(128) NOT NULL,
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    customer_scope_id UUID,
    granted_by VARCHAR(128) NOT NULL DEFAULT 'system',
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_document_shares UNIQUE (share_id, namespace_id)
);

CREATE INDEX IF NOT EXISTS idx_document_shares_lookup
    ON document_shares (namespace_id, share_id);

CREATE INDEX IF NOT EXISTS idx_document_shares_doc
    ON document_shares (namespace_id, document_id);

ALTER TABLE document_shares ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_shares FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'document_shares' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON document_shares
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

COMMIT;
