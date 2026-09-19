-- 096_agreement_parties_and_resources.sql
--
-- C12 Agreements Resource Surface (Lane B Wave B-9)
-- Relational tables for AGREEMENT, AGREEMENT_PARTY (AGREEMENT_SIGNATURE), and AGREEMENT_TEMPLATE
-- with tenant RLS isolation and foreign keys cascading to namespaces(id).

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Agreements table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agreements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    agreement_number TEXT,
    title TEXT NOT NULL,
    customer_id UUID REFERENCES sales_customers(id) ON DELETE SET NULL,
    deal_id UUID REFERENCES sales_deals(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    agreement_type TEXT NOT NULL DEFAULT 'service',
    start_date DATE,
    end_date DATE,
    auto_renewal BOOLEAN NOT NULL DEFAULT FALSE,
    notice_period_days INTEGER DEFAULT 30,
    annual_value NUMERIC(14,2) DEFAULT 0.00,
    monthly_value NUMERIC(14,2) DEFAULT 0.00,
    currency TEXT NOT NULL DEFAULT 'NOK',
    billing_frequency TEXT NOT NULL DEFAULT 'monthly',
    payment_terms_days INTEGER DEFAULT 30,
    oneflow_contract_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agreements_lookup
    ON agreements (namespace_id, is_archived, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agreements_customer
    ON agreements (namespace_id, customer_id);

CREATE INDEX IF NOT EXISTS idx_agreements_oneflow
    ON agreements (namespace_id, oneflow_contract_id);

ALTER TABLE agreements ENABLE ROW LEVEL SECURITY;
ALTER TABLE agreements FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'agreements' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON agreements
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Agreement Parties table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agreement_parties (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agreement_id UUID NOT NULL REFERENCES agreements(id) ON DELETE CASCADE,
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    party_type TEXT NOT NULL DEFAULT 'customer',
    party_name TEXT NOT NULL,
    org_number TEXT,
    signatory_name TEXT,
    signatory_email TEXT,
    signed_at TIMESTAMPTZ,
    signature_status TEXT NOT NULL DEFAULT 'pending',
    signature_source TEXT DEFAULT 'oneflow',
    role TEXT NOT NULL DEFAULT 'signatory',
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agreement_parties_lookup
    ON agreement_parties (namespace_id, agreement_id, is_archived);

CREATE INDEX IF NOT EXISTS idx_agreement_parties_email
    ON agreement_parties (namespace_id, signatory_email);

ALTER TABLE agreement_parties ENABLE ROW LEVEL SECURITY;
ALTER TABLE agreement_parties FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'agreement_parties' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON agreement_parties
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. Agreement Templates table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agreement_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    code TEXT,
    category TEXT NOT NULL DEFAULT 'service',
    description TEXT,
    default_terms JSONB NOT NULL DEFAULT '{}'::jsonb,
    sla_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
    body_template TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agreement_templates_lookup
    ON agreement_templates (namespace_id, category, is_archived);

ALTER TABLE agreement_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE agreement_templates FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'agreement_templates' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON agreement_templates
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

COMMIT;
