-- ============================================================================
-- TriMCP Migration: Quota Lower-Bound Safety — DB-Level CHECK Constraint
-- Target: Defense in depth — prevent manual operator errors from setting
--         negative ``used_amount`` values on ``resource_quotas``.
--
-- The application layer already uses ``GREATEST(0, used - delta)`` in
-- ``QuotaReservation.rollback()``, but relying purely on application logic
-- leaves the database vulnerable to ad-hoc SQL or operator mistakes.
-- This CHECK constraint provides a database-level guarantee.
--
-- Legacy deployments may lack the named constraint even when schema.sql
-- already defines it for fresh installs. Upper bound ``used_amount <= limit_amount``
-- is enforced separately via ``chk_quota`` in schema.sql (not added here).
-- ============================================================================

BEGIN;

DO $$
DECLARE
    negative_count bigint;
    repaired_count bigint;
    null_count bigint;
    constraint_exists boolean;
    used_amount_nullable boolean;
BEGIN
    -- 1. Repair invalid rows before enforcement (operator / ad-hoc SQL mistakes).
    SELECT count(*) INTO negative_count
    FROM resource_quotas
    WHERE used_amount < 0;

    IF negative_count > 0 THEN
        UPDATE resource_quotas
        SET used_amount = 0
        WHERE used_amount < 0;

        GET DIAGNOSTICS repaired_count = ROW_COUNT;
        RAISE NOTICE '003_quota_check: Repaired % row(s) with negative used_amount',
            repaired_count;
    END IF;

    -- 2. Legacy nullable column: backfill then enforce NOT NULL.
    SELECT (is_nullable = 'YES') INTO used_amount_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'resource_quotas'
      AND column_name = 'used_amount';

    IF used_amount_nullable THEN
        SELECT count(*) INTO null_count
        FROM resource_quotas
        WHERE used_amount IS NULL;

        IF null_count > 0 THEN
            UPDATE resource_quotas
            SET used_amount = 0
            WHERE used_amount IS NULL;

            RAISE NOTICE '003_quota_check: Backfilled % NULL used_amount row(s) to 0',
                null_count;
        END IF;

        ALTER TABLE resource_quotas
        ALTER COLUMN used_amount SET NOT NULL;

        RAISE NOTICE '003_quota_check: Enforced NOT NULL on used_amount';
    END IF;

    -- 3. Add named CHECK idempotently (NOT VALID → VALIDATE for safe rollout).
    SELECT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'resource_quotas'::regclass
          AND conname = 'chk_resource_quotas_used_amount_nonnegative'
    ) INTO constraint_exists;

    IF NOT constraint_exists THEN
        ALTER TABLE resource_quotas
        ADD CONSTRAINT chk_resource_quotas_used_amount_nonnegative
        CHECK (used_amount >= 0) NOT VALID;

        ALTER TABLE resource_quotas
        VALIDATE CONSTRAINT chk_resource_quotas_used_amount_nonnegative;

        RAISE NOTICE '003_quota_check: Added and validated CHECK (used_amount >= 0)';
    ELSE
        ALTER TABLE resource_quotas
        VALIDATE CONSTRAINT chk_resource_quotas_used_amount_nonnegative;

        RAISE NOTICE '003_quota_check: CHECK constraint already present — validated';
    END IF;

    -- 4. Fail closed if violations remain.
    SELECT count(*) INTO negative_count
    FROM resource_quotas
    WHERE used_amount < 0;

    IF negative_count > 0 THEN
        RAISE EXCEPTION
            '003_quota_check: % row(s) still have negative used_amount after repair',
            negative_count;
    END IF;

    RAISE NOTICE '003_quota_check quality gate: 0 rows with negative used_amount — OK';
END $$;

COMMIT;
