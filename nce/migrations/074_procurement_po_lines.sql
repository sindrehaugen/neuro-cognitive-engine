-- 074_procurement_po_lines.sql
-- ============================================================================
-- Procurement Engine (Module 1, Wave PR-2):
-- Table backing PO_LINE entity and status state machine (DRAFT -> ORDERED -> RECEIVED).
-- Guarded by Contract-A ownership registry (Procurement owner) and FORCE RLS.
-- ============================================================================

CREATE TABLE IF NOT EXISTS procurement_po_lines (
    id                UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id      UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    po_number         TEXT        NOT NULL,
    line_ref          TEXT        NOT NULL,
    project_id        TEXT,
    bom_line_label    TEXT,
    artnr             TEXT,
    description       TEXT,
    quantity          NUMERIC(14,4) NOT NULL DEFAULT 1.0,
    unit_price        NUMERIC(18,2),
    line_total        NUMERIC(18,2),
    currency          CHAR(3)     NOT NULL DEFAULT 'NOK',
    status            TEXT        NOT NULL DEFAULT 'DRAFT',
    status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT procurement_po_lines_ns_po_line_unique UNIQUE (namespace_id, po_number, line_ref),
    CONSTRAINT procurement_po_lines_status_check CHECK (status IN ('DRAFT', 'ORDERED', 'RECEIVED', 'CANCELLED'))
);

CREATE INDEX IF NOT EXISTS idx_procurement_po_lines_ns_po ON procurement_po_lines (namespace_id, po_number);
CREATE INDEX IF NOT EXISTS idx_procurement_po_lines_ns_bom ON procurement_po_lines (namespace_id, bom_line_label);
CREATE INDEX IF NOT EXISTS idx_procurement_po_lines_ns_status ON procurement_po_lines (namespace_id, status);

ALTER TABLE procurement_po_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE procurement_po_lines FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON procurement_po_lines;
CREATE POLICY tenant_isolation_policy ON procurement_po_lines
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE procurement_po_lines FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE procurement_po_lines TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE procurement_po_lines IS
'Procurement purchase order lines content store and status state machine (Wave PR-2). Isolates per tenant namespace via FORCE RLS.';
