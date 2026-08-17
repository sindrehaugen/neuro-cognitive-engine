-- 034_product_match_feedback.sql
-- Append-only learning table for BOM-line match decisions (accept / override).
-- Records which SKU the user accepted or overrode so the C1 resolver can be
-- recalibrated over time.  FORCE RLS + tenant_isolation_policy.
--
-- Design decisions:
--   - APPEND-ONLY: never UPDATE or DELETE rows — event-sourced learning loop.
--   - FORCE RLS: every row is scoped to one tenant (namespace_id).
--   - decision CHECK constraint: only 'accept' or 'override' are valid.
--   - matched_score is the pg_trgm similarity returned by C1 resolve() at
--     match time; stored as numeric for exact round-trip.
--   - chosen_sku / rejected_sku are free-text SKU identifiers (the caller may
--     pass mfr_part_no, a node_id, or any stable SKU reference).
--   - Idempotent DDL throughout (IF NOT EXISTS / DO $$ … $$).
-- ============================================================================

CREATE TABLE IF NOT EXISTS product_match_feedback (
    id            UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    bom_line      TEXT        NOT NULL,
    chosen_sku    TEXT,
    rejected_sku  TEXT,
    decision      TEXT        NOT NULL CHECK (decision IN ('accept', 'override')),
    matched_score NUMERIC,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- Index for learning-loop queries: all decisions per namespace ordered by time.
CREATE INDEX IF NOT EXISTS idx_product_match_feedback_namespace_created
    ON product_match_feedback (namespace_id, created_at DESC);

-- Row-Level Security: each tenant sees only its own feedback rows.
ALTER TABLE product_match_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_match_feedback FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON product_match_feedback;
CREATE POLICY tenant_isolation_policy ON product_match_feedback
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

-- Application role grants: INSERT + SELECT only (append-only — no UPDATE/DELETE).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE product_match_feedback FROM nce_app;
        GRANT SELECT, INSERT ON TABLE product_match_feedback TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE product_match_feedback IS
'Append-only learning table for BOM-line match decisions.
Records accept/override decisions from the product_match_bom_line tool so
the C1 resolve() primitive can be recalibrated over time.  Never UPDATE or
DELETE rows — event-sourced learning loop.  FORCE RLS isolates per tenant.';
