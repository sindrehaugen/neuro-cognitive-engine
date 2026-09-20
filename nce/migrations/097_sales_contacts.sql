-- 097_sales_contacts.sql
--
-- C12 CONTACT resource surface (Lane H Wave B-2, dispatched by ML-orch).
--
-- CONTACT is kg_nodes-primary (identity: label, entity_type, change_origin,
-- timestamps -- kg_nodes has no attribute column of its own, same finding as
-- every prior graph-primary wave). Real fields (name, email, phone) live on
-- this satellite table, joined to kg_nodes by label, following the exact
-- FK pattern migration 079 established for system_design's satellite tables
-- (fk_sddc_kg_nodes / fk_sdns_kg_nodes): FOREIGN KEY (node_label,
-- namespace_id) REFERENCES kg_nodes (label, namespace_id) ON DELETE CASCADE.
--
-- Deliberately NO customer_id / sales_customers FK (Q-46: nothing anywhere
-- writes sales_customers from a resolved-identity path yet -- adding a FK
-- here would invent a relationship no caller has established).

BEGIN;

CREATE TABLE IF NOT EXISTS sales_contacts (
    id            UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    node_label    TEXT        NOT NULL,
    name          TEXT,
    email         TEXT,
    phone         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (namespace_id, node_label),
    CONSTRAINT fk_sales_contacts_kg_nodes
        FOREIGN KEY (node_label, namespace_id)
        REFERENCES kg_nodes (label, namespace_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sales_contacts_namespace_node_label
    ON sales_contacts (namespace_id, node_label);

CREATE INDEX IF NOT EXISTS idx_sales_contacts_namespace_email
    ON sales_contacts (namespace_id, email);

CREATE INDEX IF NOT EXISTS idx_sales_contacts_namespace_phone
    ON sales_contacts (namespace_id, phone);

ALTER TABLE sales_contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_contacts FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'sales_contacts' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON sales_contacts
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

COMMIT;
