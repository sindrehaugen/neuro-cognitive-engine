-- 091_support_ticket_actions.sql
-- ============================================================================
-- Support Engine (Module 10, Wave D-5 -- TICKET_ACTION log):
-- Table backing append-only ticket action log tracking interventions (tiltak)
-- and outcomes (utfall) per ticket per ADR 0042.
--
-- STRICT ROW LEVEL SECURITY + EXPLICIT NAMESPACE PREDICATE ENFORCEMENT
-- --------------------------------------------------------------------------
-- Enables and forces RLS for nce_app.
-- As documented in Charter §5.5, RLS policies are defense-in-depth;
-- EVERY application query must carry explicit WHERE namespace_id = $1 predicates.
-- ============================================================================

CREATE TABLE IF NOT EXISTS support_ticket_actions (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    ticket_id              UUID        NOT NULL REFERENCES service_tickets(id) ON DELETE CASCADE,
    action_type            TEXT        NOT NULL,
    action_summary         TEXT        NOT NULL,
    action_details         TEXT,
    outcome                TEXT        NOT NULL,
    outcome_notes          TEXT,
    performed_by           TEXT        NOT NULL,
    performed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    change_origin          TEXT        NOT NULL DEFAULT 'agent',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT support_ticket_actions_action_summary_not_blank
        CHECK (btrim(action_summary) <> ''),
    CONSTRAINT support_ticket_actions_performed_by_not_blank
        CHECK (btrim(performed_by) <> ''),
    CONSTRAINT support_ticket_actions_action_type_check
        CHECK (action_type IN (
            'diagnostic',
            'configuration',
            'restart',
            'firmware_update',
            'hardware_replacement',
            'cable_check',
            'vendor_escalation',
            'work_order',
            'user_instruction',
            'other'
        )),
    CONSTRAINT support_ticket_actions_outcome_check
        CHECK (outcome IN (
            'resolved',
            'improved',
            'no_change',
            'worsened',
            'inconclusive',
            'failed',
            'pending_verification'
        )),
    CONSTRAINT support_ticket_actions_change_origin_check
        CHECK (change_origin IN ('sync','webhook','agent','operator','consolidation','replay','unknown'))
);

CREATE INDEX IF NOT EXISTS idx_support_ticket_actions_ns_ticket
    ON support_ticket_actions (namespace_id, ticket_id, performed_at DESC);

CREATE INDEX IF NOT EXISTS idx_support_ticket_actions_ns_type
    ON support_ticket_actions (namespace_id, action_type);

CREATE INDEX IF NOT EXISTS idx_support_ticket_actions_ns_outcome
    ON support_ticket_actions (namespace_id, outcome);

ALTER TABLE support_ticket_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_ticket_actions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON support_ticket_actions;
CREATE POLICY tenant_isolation_policy ON support_ticket_actions
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE support_ticket_actions FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE support_ticket_actions TO nce_app;
    END IF;
END $$;
