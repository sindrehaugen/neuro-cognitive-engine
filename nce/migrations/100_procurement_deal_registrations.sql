-- 100_procurement_deal_registrations.sql
--
-- C12 DEAL_REGISTRATION resource surface (Wave B-15, dispatched by ML-orch).
--
-- Premise check before building (2026-09-20): the charter's EXISTS column for
-- this wave cites "Procurement rebate model" -- but procurement_kickback_tiers,
-- the table nce/vertical_modules/procurement/frontier.py's rebate forecasting
-- reads from, does not exist anywhere in schema.sql or migrations. That code
-- already checks to_regclass('public.procurement_kickback_tiers') and degrades
-- to empty tiers if absent -- infrastructure the charter assumed, that the code
-- was already written defensively around never existing. Separately, the
-- charter's consumer column ("Agreements compliance audit reads it") is also
-- aspirational: do_run_compliance_audit only reads agreement_review_queue
-- today. Neither blocks this wave -- DEAL_REGISTRATION is a self-contained,
-- buildable record -- but this migration deliberately builds ONLY the C12
-- resource surface, not a consumer nothing calls for yet.
--
-- Plain postgres-table resource (storage_kind="postgres", not kg_nodes-primary):
-- a registered deal is a discrete business record, not a graph identity needing
-- label-based dedup the way CONTACT (email/phone) or DEVICE (physical asset)
-- are. Modeled directly on procurement_po_lines / procurement_bid_prices
-- (migration-free legacy tables, same shape): `supplier` is plain TEXT, not an
-- FK to a suppliers table -- there is no such table anywhere in this schema;
-- every existing procurement table (bid_prices.leverandor, po_lines has no
-- supplier column at all) already references suppliers by free text.
--
-- RLS pattern follows migration 097 (sales_contacts): ENABLE + FORCE, one
-- policy with both USING and WITH CHECK.

BEGIN;

CREATE TABLE IF NOT EXISTS procurement_deal_registrations (
    id                UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id      UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    supplier          TEXT          NOT NULL,
    deal_name         TEXT          NOT NULL,
    status            TEXT          NOT NULL DEFAULT 'submitted',
    estimated_value   NUMERIC(18,2),
    registered_by     TEXT,
    valid_from        TIMESTAMPTZ,
    valid_to          TIMESTAMPTZ,
    notes             TEXT,
    is_archived       BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT procurement_deal_registrations_status_check
        CHECK (status IN ('submitted', 'approved', 'rejected', 'expired')),
    CONSTRAINT procurement_deal_registrations_supplier_not_blank
        CHECK (btrim(supplier) <> ''),
    CONSTRAINT procurement_deal_registrations_deal_name_not_blank
        CHECK (btrim(deal_name) <> '')
);

CREATE INDEX IF NOT EXISTS idx_procurement_deal_registrations_ns_supplier
    ON procurement_deal_registrations (namespace_id, supplier);

CREATE INDEX IF NOT EXISTS idx_procurement_deal_registrations_ns_status
    ON procurement_deal_registrations (namespace_id, status);

ALTER TABLE procurement_deal_registrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE procurement_deal_registrations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON procurement_deal_registrations;
CREATE POLICY tenant_isolation_policy ON procurement_deal_registrations
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE procurement_deal_registrations FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE procurement_deal_registrations TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE procurement_deal_registrations IS
'Supplier deal registrations (Wave B-15), Procurement-owned. A registered deal
protects pricing/terms for a specific opportunity with a named supplier.
`supplier` is plain TEXT (no suppliers table exists anywhere in this schema --
every other procurement table references suppliers the same way). Isolates
per tenant namespace via FORCE RLS, mirrors migration 097''s policy shape.';

COMMIT;
