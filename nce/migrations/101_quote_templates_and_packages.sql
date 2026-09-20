-- 101_quote_templates_and_packages.sql
--
-- C12 QUOTE_TEMPLATE and PACKAGE resource surfaces (Wave B-7, dispatched by
-- ML-orch).
--
-- Premise check before building: grepped for any existing quote-template or
-- package-catalog infrastructure -- zero hits anywhere in nce/. The charter's
-- "Exists" column cites inventory kitting (IN-2, do_reserve_kit /
-- do_release_kit in nce/vertical_modules/inventory/kitting.py), but
-- do_reserve_kit takes an ad-hoc ``items: list[dict]`` from the caller, not a
-- stored catalog entity with its own id -- there is no PACKAGE CRUD/list
-- surface today (NCE_ENGINES_V1_6_OPERATIONS_BACKEND_2026-09-18.md's own gap
-- note: "No package CRUD/list surface"). Both node types are genuinely new,
-- unlike B-15's DEAL_REGISTRATION, which had partial phantom infrastructure.
-- This migration builds the catalog records only; "expansion = reserve_kit"
-- (the charter's own note) describes a FUTURE wiring point, not something
-- this wave builds -- do_reserve_kit already exists and is not touched here.
--
-- Two node types, two owner engines, two tables -- same migration number
-- since ML-orch allocated one number to this whole wave (WORK_QUEUE.md:
-- "100-101 -> F: B-15 DEAL_REGISTRATION, then B-7 QUOTE_TEMPLATE/PACKAGE"),
-- mirroring migration 088's precedent (sales_resource_tables covered four
-- tables in one file).
--
-- Both plain postgres-table resources (not kg_nodes-primary): a quote
-- template or a package definition is a discrete catalog record, not a
-- graph identity needing label-based dedup. Both tenant-scoped (unlike
-- PRODUCT_SKU's global product_catalog): a package or quote-template is a
-- tenant's own commercial content, not a universal shared parts fact.
--
-- RLS pattern follows migration 097/100: ENABLE + FORCE, one policy with
-- both USING and WITH CHECK.

BEGIN;

-- ---------------------------------------------------------------------------
-- sales_quote_templates (QUOTE_TEMPLATE, Sales-owned)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales_quote_templates (
    id             UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id   UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    name           TEXT          NOT NULL,
    description    TEXT,
    -- Reusable starting content for a new quote: an array of line-item
    -- templates, e.g. [{"description": "...", "artnr": "...", "quantity": 1}].
    -- JSONB, not a normalised child table -- a template is a static draft, not
    -- a live quote whose lines need their own identity/status lifecycle.
    template_lines JSONB         NOT NULL DEFAULT '[]'::jsonb,
    is_archived    BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT sales_quote_templates_name_not_blank
        CHECK (btrim(name) <> '')
);

CREATE INDEX IF NOT EXISTS idx_sales_quote_templates_ns_name
    ON sales_quote_templates (namespace_id, name);

ALTER TABLE sales_quote_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_quote_templates FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON sales_quote_templates;
CREATE POLICY tenant_isolation_policy ON sales_quote_templates
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE sales_quote_templates FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE sales_quote_templates TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE sales_quote_templates IS
'Reusable quote starting-point templates (Wave B-7), Sales-owned. template_lines
is JSONB, not a normalised child table -- a template is a static draft, never a
live quote with its own line-item lifecycle. Isolates per tenant via FORCE RLS.';

-- ---------------------------------------------------------------------------
-- product_packages (PACKAGE, Product-owned)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_packages (
    id             UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id   UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    name           TEXT          NOT NULL,
    description    TEXT,
    -- Kit components this package expands to, e.g.
    -- [{"artnr": "...", "quantity": 2}]. JSONB for the same reason as
    -- sales_quote_templates.template_lines: a package definition is a static
    -- catalog record, not a live reservation -- do_reserve_kit (inventory
    -- kitting, IN-2) is the existing function that turns concrete line items
    -- into stock reservations, and is not touched by this migration.
    components     JSONB         NOT NULL DEFAULT '[]'::jsonb,
    is_archived    BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT product_packages_name_not_blank
        CHECK (btrim(name) <> '')
);

CREATE INDEX IF NOT EXISTS idx_product_packages_ns_name
    ON product_packages (namespace_id, name);

ALTER TABLE product_packages ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_packages FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON product_packages;
CREATE POLICY tenant_isolation_policy ON product_packages
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE product_packages FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE product_packages TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE product_packages IS
'Package catalog definitions (Wave B-7), Product-owned. components is JSONB, a
static list of kit components -- expanding a package into a live stock
reservation is do_reserve_kit''s job (inventory kitting, IN-2), unchanged by
this migration. Isolates per tenant via FORCE RLS, unlike the global
product_catalog table: a package is a tenant''s own commercial bundling, not a
universal shared parts fact.';

COMMIT;
