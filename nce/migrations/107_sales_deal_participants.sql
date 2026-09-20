-- 107_sales_deal_participants.sql
--
-- C12 DEAL_PARTICIPANT resource (charter Wave B-3, sub-resource half, Lane E).
-- Genuinely new node type -- Sindre's Q-5 carve-out extension (new node type
-- allowed with its own migration and stated owner engine).
--
-- Plain relational child table, modeled on agreement_parties (migration 096)
-- rather than procurement_po_lines: sales_deals has a real UUID `id` primary
-- key, so a straightforward FK is the correct shape here (PO_LINE's
-- natural-key-no-FK design exists because PO lines key off po_number/
-- line_ref text identifiers, not because it is the general sub-resource
-- pattern). No kg_nodes involvement -- like DEAL/AGREEMENT/AGREEMENT_PARTY/
-- PO_LINE, this is a table-backed C12 resource; the generic upsert/list
-- handler in nce/resource_surface/{mcp,rest}.py issues plain SQL against
-- sales_deal_participants directly, so no assert_owner guard applies (that
-- Contract-A guard is specific to code paths that write kg_nodes rows).
--
-- B-3's charter also asks for DEAL_TAG, declined as already satisfied: the
-- generic `/api/{entity}/{id}/tags` sub-resource route
-- (nce/resource_surface/rest.py) already serves every registered spec,
-- including DEAL, verified namespace-scoped via v3_cognitive_ledger's forced
-- RLS plus an explicit namespace_id predicate in the replay query. See
-- CHARTER_WAVE_AUDIT.md's B-3 row for the full evidence -- not rebuilt here.

BEGIN;

CREATE TABLE IF NOT EXISTS sales_deal_participants (
    id                  UUID        NOT NULL DEFAULT gen_random_uuid(),
    deal_id             UUID        NOT NULL REFERENCES sales_deals(id) ON DELETE CASCADE,
    namespace_id        UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    participant_name    TEXT        NOT NULL,
    participant_email   TEXT,
    role                TEXT        NOT NULL DEFAULT 'stakeholder',
    is_archived         BOOLEAN     NOT NULL DEFAULT FALSE,
    metadata            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id),
    CONSTRAINT sales_deal_participants_name_not_blank
        CHECK (btrim(participant_name) <> '')
);

CREATE INDEX IF NOT EXISTS idx_sales_deal_participants_lookup
    ON sales_deal_participants (namespace_id, deal_id, is_archived);

CREATE INDEX IF NOT EXISTS idx_sales_deal_participants_email
    ON sales_deal_participants (namespace_id, participant_email);

ALTER TABLE sales_deal_participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_deal_participants FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'sales_deal_participants' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON sales_deal_participants
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE sales_deal_participants FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE sales_deal_participants TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE sales_deal_participants IS
'People/stakeholders participating in a sales deal (Wave B-3), Sales-owned.
Plain relational child table with a real FK to sales_deals(id), mirroring
agreement_parties'' shape. No kg_nodes row -- table-backed C12 resource like
DEAL/AGREEMENT/AGREEMENT_PARTY/PO_LINE. Isolates per tenant via FORCE RLS.';

COMMIT;
