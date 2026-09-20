-- 104_customer_invoices.sql
--
-- C12 CUSTOMER_INVOICE resource (B-13, Lane G, dispatched by ML-orch --
-- owner: economy). Follows directly on B-12 (migrations 102/103,
-- BILLING_RUN/BILLING_CANDIDATE): one CUSTOMER_INVOICE per
-- BILLING_CANDIDATE, carrying the charter's own lifecycle (F13,
-- host_parity.md): proposal -> approved -> exported -> paid.
--
-- CUSTOMER_INVOICE is deliberately a NEW node type, not a reuse of the
-- existing bare INVOICE node type (node-ownership.json, owner: economy,
-- transition: null). INVOICE is already live for a different, real thing:
-- economy/graph.py's upsert_invoice_from_procurement writes it for the
-- supplier-side PO -[posted_to]-> INVOICE boundary edge (Procurement's own
-- aspect). Giving that same node type a second, customer-facing satellite
-- table would conflate two distinct real-world documents (a supplier bill
-- NCE receives vs. a customer invoice NCE issues) under one identity --
-- the same kind of collision Q-46/Q-36 already steer away from elsewhere.
-- resource_surface/exemptions.py's INVOICE entry ("no economy_invoices
-- table exists... Scheduled for Wave B-13 CUSTOMER_INVOICE once that table
-- is built") reads as pointing at this wave by name; it is answered by
-- CUSTOMER_INVOICE existing as its own resource, not by repurposing
-- INVOICE. INVOICE's own exemption is untouched by this migration -- it
-- remains a kg_nodes-only stub for its own, separate reason.
--
-- Kg_nodes-primary, same FK pattern as migration 097/102/103:
-- FOREIGN KEY (node_label, namespace_id) REFERENCES kg_nodes (label,
-- namespace_id) ON DELETE CASCADE.
--
-- UNIQUE (namespace_id, billing_candidate_id): one invoice per candidate,
-- enforced at the DB level as defense-in-depth beyond the @governed
-- idempotency key (which only catches an exact-same-call replay, not a
-- second, differently-keyed proposal attempt against the same candidate).
--
-- invoice_number and kid are nullable and populated only at export time,
-- not at proposal time -- per the charter's own boundary ("accounting
-- system stays the legal system of record"), NCE does not mint a legally
-- sequential invoice number itself; a proposal is a priced draft awaiting
-- that external assignment. Wave B-13 (this migration + its governed core,
-- do_propose_customer_invoice) ships the 'proposal' transition only.
-- approved/exported/paid are real CHECK-constrained states with no writer
-- yet -- same aspirational-enum-now, real-transition-later shape B-12's
-- own economy_billing_candidates.status already has (verified: nothing
-- anywhere transitions that column either), stated here rather than
-- silently repeated.
--
-- subtotal_amount is read from economy_billing_candidates.total_amount
-- unchanged (agreements/price_rules.py's rates_per_month figures carry no
-- VAT of any kind -- confirmed, no vat/mva reference anywhere in
-- price-rules.json or price_rules.py), i.e. treated as ex-VAT, matching
-- sales_quotes' own total_ex_vat/total_inc_vat split. vat_rate_pct defaults
-- to the existing finago-account-mapping.json mva_codes["3"] rate (25%,
-- "Utgaende MVA, hoy sats" -- outbound revenue-side VAT), reusing the
-- estate's one existing VAT source of truth rather than inventing a new
-- constant.
--
-- vat_rate_assumed: TRUE whenever the 25% default was used because nothing
-- in the data determined a different rate -- checked directly, not assumed
-- safe: sales_customers has no country/export/VAT-exemption field of any
-- kind (billing_address is free-form JSONB with no guaranteed schema), so
-- a customer legitimately due 0% (export) or another rate cannot currently
-- be distinguished from a standard-rate one. Per Sindre's own refuse-and-
-- name discipline applied to money documents rather than just room
-- categories: a defaulted rate on an invoice must be VISIBLE to whoever
-- reviews the proposal, not silently indistinguishable from a determined
-- one -- this column is that visibility, not a guess dressed up as a fact.

BEGIN;

CREATE TABLE IF NOT EXISTS economy_customer_invoices (
    id                    UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id          UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    node_label            TEXT          NOT NULL,
    billing_candidate_id  UUID          NOT NULL REFERENCES economy_billing_candidates(id) ON DELETE CASCADE,
    customer_id           UUID          REFERENCES sales_customers(id) ON DELETE SET NULL,
    issue_date            DATE          NOT NULL,
    due_date              DATE          NOT NULL,
    currency              TEXT          NOT NULL DEFAULT 'NOK',
    subtotal_amount       NUMERIC(18,2) NOT NULL CHECK (subtotal_amount >= 0),
    vat_rate_pct          NUMERIC(5,2)  NOT NULL DEFAULT 25.00 CHECK (vat_rate_pct >= 0),
    vat_rate_assumed      BOOLEAN       NOT NULL DEFAULT TRUE,
    vat_amount            NUMERIC(18,2) NOT NULL CHECK (vat_amount >= 0),
    total_amount          NUMERIC(18,2) NOT NULL CHECK (total_amount >= 0),
    status                TEXT          NOT NULL DEFAULT 'proposal'
                          CHECK (status IN ('proposal', 'approved', 'exported', 'paid')),
    -- Assigned by the external accounting system at export time -- NULL
    -- for every 'proposal' row by design (see header).
    invoice_number        TEXT,
    kid                   TEXT,
    exported_at           TIMESTAMPTZ,
    paid_at               TIMESTAMPTZ,
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (namespace_id, node_label),
    UNIQUE (namespace_id, billing_candidate_id),
    CONSTRAINT fk_economy_customer_invoices_kg_nodes
        FOREIGN KEY (node_label, namespace_id)
        REFERENCES kg_nodes (label, namespace_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_economy_customer_invoices_namespace_node_label
    ON economy_customer_invoices (namespace_id, node_label);

CREATE INDEX IF NOT EXISTS idx_economy_customer_invoices_candidate
    ON economy_customer_invoices (billing_candidate_id);

CREATE INDEX IF NOT EXISTS idx_economy_customer_invoices_customer
    ON economy_customer_invoices (namespace_id, customer_id);

ALTER TABLE economy_customer_invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE economy_customer_invoices FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'economy_customer_invoices' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON economy_customer_invoices
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

COMMIT;
