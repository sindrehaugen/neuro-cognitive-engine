-- 083_notifications_reminders.sql
--
-- C13 Notifications and Reminders (Wave A-3)
-- Persistent multi-tenant user notification inbox, reminder engine, and subscription registry.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Notifications table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL,
    principal_id TEXT NOT NULL,
    title VARCHAR(256) NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    severity VARCHAR(32) NOT NULL DEFAULT 'info',
    category VARCHAR(64) NOT NULL DEFAULT 'general',
    source_selector VARCHAR(128),
    source_id VARCHAR(256),
    idempotency_key VARCHAR(256),
    read_at TIMESTAMPTZ,
    seen_at TIMESTAMPTZ,
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_notifications_lookup
    ON notifications (namespace_id, principal_id, is_archived, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_idempotency
    ON notifications (namespace_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'notifications' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON notifications
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Reminders table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reminders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL,
    principal_id TEXT NOT NULL,
    node_type VARCHAR(64) NOT NULL,
    node_id VARCHAR(256) NOT NULL,
    title VARCHAR(256) NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    remind_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    fired_at TIMESTAMPTZ,
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reminders_lookup
    ON reminders (namespace_id, principal_id, status, remind_at ASC);

CREATE INDEX IF NOT EXISTS idx_reminders_pending_scan
    ON reminders (status, remind_at ASC)
    WHERE status = 'pending' AND is_archived = FALSE;

ALTER TABLE reminders ENABLE ROW LEVEL SECURITY;
ALTER TABLE reminders FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'reminders' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON reminders
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. Notification Subscriptions table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notification_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL,
    principal_id TEXT NOT NULL,
    selector VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_notification_subscriptions UNIQUE (namespace_id, principal_id, selector)
);

CREATE INDEX IF NOT EXISTS idx_notification_subs_lookup
    ON notification_subscriptions (namespace_id, selector);

ALTER TABLE notification_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_subscriptions FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'notification_subscriptions' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON notification_subscriptions
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

COMMIT;
