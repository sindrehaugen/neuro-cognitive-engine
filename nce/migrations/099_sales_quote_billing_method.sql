-- 099_sales_quote_billing_method.sql
-- ============================================================================
-- Charter §13 Wave B-5: `billing_method ∈ {percent, hours_amount}` attribute
-- on QUOTE.
--
-- Nullable, no default: unlike migration 063's `priced` (where every
-- pre-existing row genuinely HAD a real price and a default of TRUE stated
-- that truth), no caller anywhere computes or reads a billing method for a
-- quote today (confirmed: zero references to either value in
-- nce/vertical_modules/sales/commission.py, the one module that would
-- plausibly consume it). Defaulting to either 'percent' or 'hours_amount'
-- would assert a fact about every historical quote that nothing recorded --
-- NULL correctly states "not yet classified" for all of them, matching the
-- attribute's own status as newly declared, not newly derived.
--
-- Idempotent DDL (nce/migration_ledger.py ledgers by filename+checksum;
-- IF NOT EXISTS covers pre-ledger databases and the shared dev DB, same
-- reasoning as migration 081's header).
-- ============================================================================

ALTER TABLE sales_quotes ADD COLUMN IF NOT EXISTS billing_method TEXT;

ALTER TABLE sales_quotes DROP CONSTRAINT IF EXISTS sales_quotes_billing_method_check;
ALTER TABLE sales_quotes ADD CONSTRAINT sales_quotes_billing_method_check
    CHECK (billing_method IS NULL OR billing_method IN ('percent', 'hours_amount'));
