-- Migration 006: Add correlation_id to event_log for cross-request / cross-agent tracing.
--
-- correlation_id is nullable — rows written before this migration (and any event_log
-- append that runs outside a request context, e.g. background cron jobs) store NULL.
-- The conditional index on non-NULL values keeps the index compact.
--
-- The event_log table is range-partitioned by occurred_at.  ALTER TABLE on the parent
-- automatically propagates the column to all existing and future partitions (PG 10+).

ALTER TABLE event_log
    ADD COLUMN IF NOT EXISTS correlation_id UUID;

CREATE INDEX IF NOT EXISTS event_log_correlation_id_idx
    ON event_log(correlation_id)
    WHERE correlation_id IS NOT NULL;
