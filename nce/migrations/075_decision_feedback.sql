-- 075_decision_feedback.sql
-- ============================================================================
-- C10 Decision-Feedback Service:
-- Stores the ground-truth human decision signal across vertical engines:
--   - product: BOM_LINE -> PRODUCT matches (accept / override)
--   - procurement: supplier ranking matches (accept / override)
--   - economy: invoice matches (accept / override)
--   - resources: allocation outcomes (held / deviated)
--   - field_tech: work order execution outcomes (succeeded / rework)
--
-- Rows carry the proposal, the human's decision, the delta, the actor,
-- the engine, and the tenant namespace.
--
-- STRICT ROW LEVEL SECURITY + EXPLICIT NAMESPACE PREDICATE ENFORCEMENT
-- ============================================================================

CREATE TABLE IF NOT EXISTS decision_feedback (
    id            UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    engine        TEXT        NOT NULL,
    context_id    TEXT,
    proposal      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    decision      TEXT        NOT NULL,
    delta         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    actor         TEXT        NOT NULL DEFAULT 'human',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_decision_feedback_ns_engine
    ON decision_feedback (namespace_id, engine, created_at DESC);

ALTER TABLE decision_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE decision_feedback FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON decision_feedback;
CREATE POLICY tenant_isolation_policy ON decision_feedback
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE decision_feedback FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE decision_feedback TO nce_app;
    END IF;
END $$;
