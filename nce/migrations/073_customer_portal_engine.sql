-- 073_customer_portal_engine.sql
-- ============================================================================
-- Customer Portal Engine (Module 17, Phase 1 -- security spine):
-- Tables backing nce/vertical_modules/customer_portal/**
--   1. portal_users (customer login identities / sessions under customer principal)
--   2. portal_document_shares (scoped, expiring grants to FDV/as-built documents)
--   3. portal_service_requests (customer service intake before Support owns the ticket)
--
-- STRICT ROW LEVEL SECURITY + C3 EXTERNAL PRINCIPAL SCOPE (get_nce_external_scope)
-- --------------------------------------------------------------------------
-- All three tables enable and force RLS for nce_app.
-- The external_isolation_policy enforces:
--   namespace_id = get_nce_namespace() AND customer_scope_id = get_nce_external_scope()
-- When nce.external_scope_id is unset or empty, get_nce_external_scope() returns
-- the nil-UUID sentinel ('00000000-0000-0000-0000-000000000000'), guaranteeing
-- zero rows are exposed (DENY-WHEN-UNSET invariant).
-- ============================================================================

-- 1. portal_users
CREATE TABLE IF NOT EXISTS portal_users (
    id                 UUID        NOT NULL DEFAULT gen_random_uuid(),
    portal_user_id     TEXT        NOT NULL,
    namespace_id       UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    customer_scope_id  UUID        NOT NULL,
    customer_id        TEXT        NOT NULL,
    email              TEXT,
    auth_provider      TEXT        NOT NULL DEFAULT 'magic_link',
    contact            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    last_login_at      TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT portal_users_id_ns_unique UNIQUE (portal_user_id, namespace_id),
    CONSTRAINT portal_users_auth_provider_check CHECK (auth_provider IN ('magic_link', 'bankid', 'mock'))
);

CREATE INDEX IF NOT EXISTS idx_portal_users_ns_scope ON portal_users (namespace_id, customer_scope_id);
CREATE INDEX IF NOT EXISTS idx_portal_users_ns_customer ON portal_users (namespace_id, customer_id);

ALTER TABLE portal_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE portal_users FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS external_isolation_policy ON portal_users;
CREATE POLICY external_isolation_policy ON portal_users
    FOR ALL TO nce_app
    USING (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND customer_scope_id IS NOT NULL
        AND customer_scope_id = get_nce_external_scope()
    )
    WITH CHECK (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND customer_scope_id IS NOT NULL
        AND customer_scope_id = get_nce_external_scope()
    );

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE portal_users FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE portal_users TO nce_app;
    END IF;
END $$;


-- 2. portal_document_shares
CREATE TABLE IF NOT EXISTS portal_document_shares (
    id                 UUID        NOT NULL DEFAULT gen_random_uuid(),
    share_id           TEXT        NOT NULL,
    namespace_id       UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    customer_scope_id  UUID        NOT NULL,
    document_ref       TEXT        NOT NULL,
    document_kind      TEXT        NOT NULL DEFAULT 'fdv',
    title              TEXT        NOT NULL DEFAULT '',
    granted_by         TEXT        NOT NULL DEFAULT 'system',
    expires_at         TIMESTAMPTZ,
    revoked_at         TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT portal_document_shares_id_ns_unique UNIQUE (share_id, namespace_id),
    CONSTRAINT portal_document_shares_kind_check CHECK (document_kind IN ('fdv', 'as_built', 'sow', 'manual', 'drawing'))
);

CREATE INDEX IF NOT EXISTS idx_portal_document_shares_ns_scope ON portal_document_shares (namespace_id, customer_scope_id);

ALTER TABLE portal_document_shares ENABLE ROW LEVEL SECURITY;
ALTER TABLE portal_document_shares FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS external_isolation_policy ON portal_document_shares;
CREATE POLICY external_isolation_policy ON portal_document_shares
    FOR ALL TO nce_app
    USING (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND customer_scope_id IS NOT NULL
        AND customer_scope_id = get_nce_external_scope()
    )
    WITH CHECK (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND customer_scope_id IS NOT NULL
        AND customer_scope_id = get_nce_external_scope()
    );

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE portal_document_shares FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE portal_document_shares TO nce_app;
    END IF;
END $$;


-- 3. portal_service_requests
CREATE TABLE IF NOT EXISTS portal_service_requests (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    request_id             TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    customer_scope_id      UUID        NOT NULL,
    room_id                TEXT        NOT NULL,
    payload                JSONB       NOT NULL DEFAULT '{}'::jsonb,
    status                 TEXT        NOT NULL DEFAULT 'received',
    handed_off_ticket_id   TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT portal_service_requests_id_ns_unique UNIQUE (request_id, namespace_id),
    CONSTRAINT portal_service_requests_status_check CHECK (status IN ('received', 'under_review', 'scheduled', 'resolved', 'closed'))
);

CREATE INDEX IF NOT EXISTS idx_portal_service_requests_ns_scope ON portal_service_requests (namespace_id, customer_scope_id);
CREATE INDEX IF NOT EXISTS idx_portal_service_requests_ns_room ON portal_service_requests (namespace_id, room_id);

ALTER TABLE portal_service_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE portal_service_requests FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS external_isolation_policy ON portal_service_requests;
CREATE POLICY external_isolation_policy ON portal_service_requests
    FOR ALL TO nce_app
    USING (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND customer_scope_id IS NOT NULL
        AND customer_scope_id = get_nce_external_scope()
    )
    WITH CHECK (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND customer_scope_id IS NOT NULL
        AND customer_scope_id = get_nce_external_scope()
    );

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE portal_service_requests FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE portal_service_requests TO nce_app;
    END IF;
END $$;