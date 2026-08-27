-- 056_sales_signed_baselines_bigserial.sql
--
-- Converge sales_signed_baselines.id on the declared BIGSERIAL form.
--
-- Both nce/schema.sql and migration 041 have declared `id BIGSERIAL PRIMARY KEY`
-- since the table's first commit (Batch 087, f35c39a). Some databases
-- nonetheless carry `id uuid DEFAULT gen_random_uuid()` -- created outside these
-- declarations, before 041 landed. `CREATE TABLE IF NOT EXISTS` silently no-ops
-- against them, so the two forms have coexisted ever since. That drift is what
-- made 041's unconditional `GRANT ... ON SEQUENCE sales_signed_baselines_id_seq`
-- abort startup: on a uuid-keyed table the sequence does not exist.
--
-- 041 keeps its EXISTS guard on purpose -- migrations run in order, so at 041's
-- turn the table may still be uuid-keyed.
--
-- Safety of the conversion (verified before writing this):
--   * no foreign key anywhere references sales_signed_baselines;
--   * the surrogate id is never persisted -- callers stringify it into an API
--     response (`signed_baseline_id`) and nothing stores it;
--   * the table carries no triggers, so no WORM rule blocks the rewrite;
--   * rows are preserved. Only the surrogate key changes, assigned in
--     signed_at order so the new ids follow the freeze order.
--
-- The business columns -- quote_id, signed_margin_pct, signed_total_nok,
-- signed_at -- are untouched. The natural key (namespace_id, quote_id) is what
-- actually identifies a baseline, and it is unaffected.
--
-- Idempotent: does nothing once id is already bigint.

DO $$
DECLARE
    pk_name TEXT;
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'sales_signed_baselines'
          AND column_name = 'id'
          AND data_type = 'uuid'
    ) THEN
        RETURN;  -- already bigint, or the table does not exist here
    END IF;

    SELECT c.conname INTO pk_name
    FROM pg_constraint c
    WHERE c.conrelid = 'sales_signed_baselines'::regclass
      AND c.contype = 'p';

    IF pk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE sales_signed_baselines DROP CONSTRAINT %I', pk_name);
    END IF;

    ALTER TABLE sales_signed_baselines DROP COLUMN id;

    CREATE SEQUENCE IF NOT EXISTS sales_signed_baselines_id_seq;

    ALTER TABLE sales_signed_baselines
        ADD COLUMN id BIGINT DEFAULT nextval('sales_signed_baselines_id_seq');

    -- Assign in freeze order rather than whatever order the heap returns.
    UPDATE sales_signed_baselines t
       SET id = o.new_id
      FROM (
          SELECT ctid,
                 row_number() OVER (ORDER BY signed_at, quote_id) AS new_id
          FROM sales_signed_baselines
      ) o
     WHERE t.ctid = o.ctid;

    PERFORM setval(
        'sales_signed_baselines_id_seq',
        GREATEST(COALESCE((SELECT max(id) FROM sales_signed_baselines), 0), 1)
    );

    ALTER TABLE sales_signed_baselines ALTER COLUMN id SET NOT NULL;
    ALTER SEQUENCE sales_signed_baselines_id_seq OWNED BY sales_signed_baselines.id;
    ALTER TABLE sales_signed_baselines ADD PRIMARY KEY (id);

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT USAGE, SELECT ON SEQUENCE sales_signed_baselines_id_seq TO nce_app;
    END IF;

    RAISE NOTICE 'sales_signed_baselines.id converted uuid -> bigserial';
END $$;
