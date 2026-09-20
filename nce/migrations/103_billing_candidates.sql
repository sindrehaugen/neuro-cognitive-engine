-- 103_billing_candidates.sql
--
-- C12 BILLING_CANDIDATE resource (B-12, Lane G, dispatched by ML-orch under
-- Sindre's extended Q-5 carve-out -- owner: economy). Companion to migration
-- 102 (BILLING_RUN).
--
-- BILLING_CANDIDATE is kg_nodes-primary, same pattern as migration 102 and
-- 097 (sales_contacts): FOREIGN KEY (node_label, namespace_id) REFERENCES
-- kg_nodes (label, namespace_id) ON DELETE CASCADE.
--
-- customer_id IS a real FK here (unlike sales_contacts' deliberate omission
-- under Q-46) -- this is not a new identity resolution, it is copying
-- forward the agreements.customer_id FK that already exists on the
-- agreement each candidate is generated from (agreements/sla.py's
-- get_sla_coverage reads the agreement; the agreement row already carries
-- customer_id). No new relationship is invented.
--
-- economy_billing_candidate_lines is a plain child table, NOT kg_nodes-
-- primary and NOT its own node type: one line per priced room-category
-- tier per candidate (nce.vertical_modules.agreements.room_category_pricing
-- .resolve_price_tier's output, aggregated the same way price_rules.py's
-- sla_room_pricing rule already aggregates -- see billing_runs.py for why
-- this is per-tier, not per-room). covered_fl_labels on each line names
-- every room that tier's amount was computed from, for traceability without
-- a fourth table.

BEGIN;

CREATE TABLE IF NOT EXISTS economy_billing_candidates (
    id                UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id      UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    node_label        TEXT        NOT NULL,
    billing_run_id    UUID        NOT NULL REFERENCES economy_billing_runs(id) ON DELETE CASCADE,
    customer_id       UUID        REFERENCES sales_customers(id) ON DELETE SET NULL,
    agreement_id      UUID        REFERENCES agreements(id) ON DELETE SET NULL,
    period_start      DATE        NOT NULL,
    period_end        DATE        NOT NULL,
    currency          TEXT        NOT NULL DEFAULT 'NOK',
    total_amount      NUMERIC(18,2) NOT NULL DEFAULT 0.00 CHECK (total_amount >= 0),
    status            TEXT        NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'approved', 'exported')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (namespace_id, node_label),
    CONSTRAINT fk_economy_billing_candidates_kg_nodes
        FOREIGN KEY (node_label, namespace_id)
        REFERENCES kg_nodes (label, namespace_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_economy_billing_candidates_namespace_node_label
    ON economy_billing_candidates (namespace_id, node_label);

CREATE INDEX IF NOT EXISTS idx_economy_billing_candidates_run
    ON economy_billing_candidates (billing_run_id);

CREATE INDEX IF NOT EXISTS idx_economy_billing_candidates_customer
    ON economy_billing_candidates (namespace_id, customer_id);

ALTER TABLE economy_billing_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE economy_billing_candidates FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'economy_billing_candidates' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON economy_billing_candidates
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

-- Plain child table -- not kg_nodes-primary, no node-ownership.json row
-- (same reasoning DESIGN_LINE / AGREEMENT_PARTY-style sub-objects use
-- elsewhere: a line is not independently owned or queryable outside its
-- parent candidate).
CREATE TABLE IF NOT EXISTS economy_billing_candidate_lines (
    id                   UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id         UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    billing_candidate_id UUID          NOT NULL REFERENCES economy_billing_candidates(id) ON DELETE CASCADE,
    price_tier           TEXT          NOT NULL,
    room_count           INTEGER       NOT NULL CHECK (room_count > 0),
    unit_monthly_rate    NUMERIC(18,2) NOT NULL CHECK (unit_monthly_rate >= 0),
    line_amount          NUMERIC(18,2) NOT NULL CHECK (line_amount >= 0),
    covered_fl_labels    JSONB         NOT NULL DEFAULT '[]'::jsonb,
    created_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_economy_billing_candidate_lines_candidate
    ON economy_billing_candidate_lines (billing_candidate_id);

ALTER TABLE economy_billing_candidate_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE economy_billing_candidate_lines FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'economy_billing_candidate_lines' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON economy_billing_candidate_lines
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

COMMIT;
