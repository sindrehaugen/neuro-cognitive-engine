-- Migration 065: reserved system namespace for GLOBAL (non-tenant) audit events.
--
-- WHY
-- ---
-- `signing_key_rotated` was a declared event type in nce/event_types.py with a
-- live replay handler and no producer: handle_rotate_signing_key() logged at
-- WARNING and returned, because `event_log.namespace_id` is NOT NULL and
-- FK-references `namespaces`, while signing keys are process-global and belong
-- to no tenant. A master/signing key rotation therefore left NO immutable
-- audit record. `audit_log` was rejected as the destination: it has zero
-- triggers, so its rows are freely deletable.
--
-- WHAT
-- ----
-- Seeds ONE reserved, non-tenant namespace row, following the pattern the
-- pre-RLS `_global_legacy` row already established in nce/schema.sql.
--
-- This is DATA SEEDING, not DDL -- no table, column, constraint, index, or
-- policy is touched, which is exactly why nce/schema.sql needs no edit here.
-- scripts/apply_integration_schema.py applies schema.sql and then every
-- migration in order, so this row reaches a fresh database and an existing one
-- by the same path. (schema.sql alone would not: it is mounted at
-- /docker-entrypoint-initdb.d/ and Postgres runs it only on an empty data dir.)
--
-- The slug MUST stay byte-identical to nce.system_namespace.SYSTEM_NAMESPACE_SLUG.
-- Re-runnable: ON CONFLICT DO NOTHING on the existing unique `slug`.

INSERT INTO namespaces (slug, metadata)
VALUES (
    '_system',
    '{"description":"Reserved system namespace: global, non-tenant security and audit events (e.g. signing_key_rotated). Not a tenant.","reserved":true,"system":true}'::jsonb
)
ON CONFLICT (slug) DO NOTHING;
