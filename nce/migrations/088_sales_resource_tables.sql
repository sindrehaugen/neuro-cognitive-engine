-- 088_sales_resource_tables.sql
--
-- C12 Sales Resource Surface (Lane B Wave B-1)
-- Relational tables for CUSTOMER, LEAD, DEAL, and QUOTE entities with tenant RLS isolation.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Sales Customers table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales_customers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    org_number TEXT,
    email TEXT,
    phone TEXT,
    billing_address JSONB NOT NULL DEFAULT '{}'::jsonb,
    shipping_address JSONB NOT NULL DEFAULT '{}'::jsonb,
    tier TEXT NOT NULL DEFAULT 'standard',
    status TEXT NOT NULL DEFAULT 'active',
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sales_customers_lookup
    ON sales_customers (namespace_id, is_archived, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sales_customers_org
    ON sales_customers (namespace_id, org_number);

ALTER TABLE sales_customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_customers FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'sales_customers' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON sales_customers
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Sales Leads table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales_leads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    customer_id UUID REFERENCES sales_customers(id) ON DELETE SET NULL,
    contact_name TEXT,
    email TEXT,
    phone TEXT,
    company TEXT,
    source TEXT NOT NULL DEFAULT 'direct',
    status TEXT NOT NULL DEFAULT 'new',
    estimated_value NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sales_leads_lookup
    ON sales_leads (namespace_id, is_archived, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sales_leads_customer
    ON sales_leads (namespace_id, customer_id);

ALTER TABLE sales_leads ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_leads FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'sales_leads' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON sales_leads
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. Sales Deals table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales_deals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    customer_id UUID REFERENCES sales_customers(id) ON DELETE SET NULL,
    lead_id UUID REFERENCES sales_leads(id) ON DELETE SET NULL,
    owner_slug TEXT,
    stage TEXT NOT NULL DEFAULT 'discovery',
    value NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
    currency VARCHAR(3) NOT NULL DEFAULT 'NOK',
    expected_close_date DATE,
    probability INT NOT NULL DEFAULT 50,
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sales_deals_lookup
    ON sales_deals (namespace_id, is_archived, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sales_deals_customer
    ON sales_deals (namespace_id, customer_id);

CREATE INDEX IF NOT EXISTS idx_sales_deals_stage
    ON sales_deals (namespace_id, stage);

ALTER TABLE sales_deals ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_deals FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'sales_deals' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON sales_deals
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4. Sales Quotes table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales_quotes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    quote_number TEXT NOT NULL,
    deal_id UUID REFERENCES sales_deals(id) ON DELETE SET NULL,
    customer_id UUID REFERENCES sales_customers(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    version INT NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'draft',
    total_ex_vat NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
    total_inc_vat NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
    currency VARCHAR(3) NOT NULL DEFAULT 'NOK',
    valid_until DATE,
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sales_quotes_lookup
    ON sales_quotes (namespace_id, is_archived, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sales_quotes_deal
    ON sales_quotes (namespace_id, deal_id);

CREATE INDEX IF NOT EXISTS idx_sales_quotes_customer
    ON sales_quotes (namespace_id, customer_id);

CREATE INDEX IF NOT EXISTS idx_sales_quotes_number
    ON sales_quotes (namespace_id, quote_number);

ALTER TABLE sales_quotes ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_quotes FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'sales_quotes' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON sales_quotes
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

COMMIT;
