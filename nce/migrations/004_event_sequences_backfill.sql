-- One-time / idempotent backfill: align event_sequences with existing event_log rows.
-- Run after schema.sql has created event_sequences (FIX-068) on clusters that already
-- had events before the counter table existed; otherwise the first append may reuse
-- low event_seq values and hit UNIQUE (namespace_id, event_seq, occurred_at).

BEGIN;

LOCK TABLE public.event_log IN SHARE MODE;
LOCK TABLE public.event_sequences IN EXCLUSIVE MODE;

INSERT INTO public.event_sequences (namespace_id, seq)
SELECT namespace_id, MAX(event_seq)::bigint
FROM   public.event_log
WHERE  event_seq IS NOT NULL
GROUP BY namespace_id
ON CONFLICT (namespace_id) DO UPDATE
SET seq = GREATEST(public.event_sequences.seq, EXCLUDED.seq);

DO $$
DECLARE
    mismatch_count int;
    namespace_count int;
BEGIN
    SELECT COUNT(*)::int INTO namespace_count
    FROM (
        SELECT namespace_id
        FROM public.event_log
        WHERE event_seq IS NOT NULL
        GROUP BY namespace_id
    ) logged;

    SELECT COUNT(*)::int INTO mismatch_count
    FROM (
        SELECT es.namespace_id
        FROM public.event_sequences es
        INNER JOIN (
            SELECT namespace_id, MAX(event_seq)::bigint AS max_seq
            FROM public.event_log
            WHERE event_seq IS NOT NULL
            GROUP BY namespace_id
        ) el USING (namespace_id)
        WHERE es.seq < el.max_seq
    ) mismatches;

    RAISE NOTICE
        'event_sequences backfill (FIX-068): % namespaces in event_log, % counter mismatches after backfill',
        namespace_count,
        mismatch_count;

    IF mismatch_count > 0 THEN
        RAISE EXCEPTION
            'event_sequences backfill verification failed: % namespace(s) have seq < max(event_seq)',
            mismatch_count;
    END IF;
END $$;

COMMIT;
