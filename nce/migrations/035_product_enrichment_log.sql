-- 035_product_enrichment_log.sql
-- Review-queue backing store for on-demand product enrichment proposals (M2.W7).
-- Each row is a single field proposal: product_id + field_name + field_value +
-- confidence score + needs_review flag.  Money/legal fields and sub-threshold
-- proposals are always written here first (needs_review=True) before any
-- auto-merge to product_catalog.etim_specs is considered.
--
-- Design decisions:
--   - APPEND-ONLY: never UPDATE or DELETE rows — WORM review log.
--   - FORCE RLS: every row is scoped to one tenant (namespace_id).
--   - confidence NUMERIC(5,4): 0.0000..1.0000, four-decimal precision.
--   - needs_review BOOLEAN: True = must be human-confirmed before catalog merge.
--   - trigger_context JSONB: kind (quote|design), ref_id, missing_fields.
--   - product_source_id TEXT: provenance tag from the source record.
--   - Idempotent DDL throughout (IF NOT EXISTS / DO $$ … $$).
-- ============================================================================

CREATE TABLE IF NOT EXISTS product_enrichment_log (
    id                UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id      UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    product_id        UUID        NOT NULL,
    trigger_context   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    field_name        TEXT        NOT NULL,
    field_value       TEXT,
    confidence        NUMERIC(5,4) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    needs_review      BOOLEAN     NOT NULL DEFAULT true,
    product_source_id TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- Index: per-namespace + product lookups for the review queue.
CREATE INDEX IF NOT EXISTS idx_product_enrichment_log_namespace_product
    ON product_enrichment_log (namespace_id, product_id, created_at DESC);

-- Index: surface all rows that need review per namespace.
CREATE INDEX IF NOT EXISTS idx_product_enrichment_log_needs_review
    ON product_enrichment_log (namespace_id, needs_review, created_at DESC)
    WHERE needs_review = true;

-- Row-Level Security: each tenant sees only its own enrichment log rows.
ALTER TABLE product_enrichment_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_enrichment_log FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON product_enrichment_log;
CREATE POLICY tenant_isolation_policy ON product_enrichment_log
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

-- Application role grants: SELECT + INSERT only (append-only — no UPDATE/DELETE).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE product_enrichment_log FROM nce_app;
        GRANT SELECT, INSERT ON TABLE product_enrichment_log TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE product_enrichment_log IS
'Append-only review-queue backing store for on-demand product enrichment proposals.
Each row is one field proposal: field_name + field_value + verbalized confidence (A4) +
needs_review flag.  Money/legal fields (§9.3) and sub-threshold proposals are always
written with needs_review=True.  High-confidence non-money/legal fields may additionally
be merged into product_catalog.etim_specs (the JSONB designed for per-field provenance).
Never UPDATE or DELETE rows — WORM review log.  FORCE RLS isolates per tenant.';
