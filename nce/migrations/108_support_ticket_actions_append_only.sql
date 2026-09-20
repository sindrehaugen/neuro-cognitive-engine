-- 108_support_ticket_actions_append_only.sql
-- ============================================================================
-- Support Engine: enforce the append-only invariant support_ticket_actions
-- was always documented to have (migration 091's header, this table's own
-- ResourceSpec description, and four other sites -- all citing a nonexistent
-- "ADR 0042"). See docs/adr/0008-append-only-ticket-action-log.md for the
-- real decision record this migration implements.
--
-- 091 granted this table SELECT, INSERT, UPDATE, DELETE -- full CRUD, no
-- enforcement of the append-only claim at all. do_log_ticket_action
-- (support/tickets.py) is the only writer and only ever INSERTs; nothing in
-- this codebase issues UPDATE or DELETE against this table (verified by
-- direct search before writing this migration).
--
-- Grant-level only, matching event_log's own shape (schema.sql:838,
-- GRANT INSERT, SELECT ON event_log TO nce_app -- no UPDATE, no DELETE),
-- not event_log's trigger-based enforcement (ADR 0001) -- ticket actions
-- are not part of the cryptographic chain-hash integrity system a trigger
-- exists to protect there. A grant revocation is proportionate here: it
-- closes the one live gap (the generic C12 upsert route, excluded in the
-- accompanying ResourceSpec change) without adding trigger machinery this
-- table's own risk profile doesn't call for.
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE UPDATE, DELETE ON TABLE support_ticket_actions FROM nce_app;
    END IF;
END $$;
