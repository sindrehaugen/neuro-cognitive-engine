-- 031_ledger_valid_to_and_worm_partition_maintenance.sql
-- Wave R-13 Core components:
-- 1. v3_cognitive_ledger.valid_to (GDPR Article 17 cascade_delete_tenant and pruning)
-- 2. Scoped event_log_default partition-maintenance bypass in prevent_mutation() +
--    nce_ensure_event_log_monthly_partitions() (F-05)
-- ============================================================================

-- 1. Temporal soft-delete marker on v3_cognitive_ledger for GDPR erasure & pruning
ALTER TABLE v3_cognitive_ledger ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;

-- 3. WORM immutability trigger with scoped event_log_default partition-maintenance bypass (F-05)
CREATE OR REPLACE FUNCTION prevent_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND TG_TABLE_NAME = 'event_log_default'
       AND current_setting('nce.partition_maintenance', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'event_log is immutable (WORM). % operation is forbidden.', TG_OP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION nce_ensure_event_log_monthly_partitions(p_months_ahead int DEFAULT 3)
RETURNS void
LANGUAGE plpgsql
AS $fn$
DECLARE
    m int;
    p_start timestamptz;
    p_end timestamptz;
    p_name text;
    violating_count int;
BEGIN
    IF p_months_ahead < 0 THEN
        RAISE EXCEPTION 'p_months_ahead must be >= 0';
    END IF;
    FOR m IN 0..p_months_ahead LOOP
        p_start := date_trunc('month', now() + make_interval(months => m));
        p_end := p_start + interval '1 month';
        p_name := 'event_log_' || to_char(p_start, 'YYYY_MM');
        IF to_regclass(format('public.%I', p_name)) IS NULL THEN
            -- Check if there are violating rows in event_log_default
            EXECUTE format(
                'SELECT count(*)::int FROM event_log_default WHERE occurred_at >= %L AND occurred_at < %L',
                p_start,
                p_end
            ) INTO violating_count;
            
            IF violating_count > 0 THEN
                -- Move violating rows from event_log_default to a temp table
                CREATE TEMP TABLE temp_event_log_migrate ON COMMIT DROP AS 
                    SELECT * FROM event_log_default 
                    WHERE occurred_at >= p_start AND occurred_at < p_end;
                    
                PERFORM set_config('nce.partition_maintenance', 'on', true);
                DELETE FROM event_log_default 
                WHERE occurred_at >= p_start AND occurred_at < p_end;
                PERFORM set_config('nce.partition_maintenance', '', true);
                
                -- Create partition
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF event_log FOR VALUES FROM (%L) TO (%L)',
                    p_name,
                    p_start,
                    p_end
                );
                
                -- Insert them back into event_log so they route to the new partition
                INSERT INTO event_log SELECT * FROM temp_event_log_migrate;
                
                -- Drop the temp table
                DROP TABLE temp_event_log_migrate;
            ELSE
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF event_log FOR VALUES FROM (%L) TO (%L)',
                    p_name,
                    p_start,
                    p_end
                );
            END IF;
        END IF;
    END LOOP;
END;
$fn$;
