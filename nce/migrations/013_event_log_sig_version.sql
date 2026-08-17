-- 013_event_log_sig_version.sql
-- Back-compat hinge for binding prev_chain_hash into the signature.
-- ============================================================================

ALTER TABLE event_log ADD COLUMN IF NOT EXISTS signature_version SMALLINT NOT NULL DEFAULT 1;

COMMENT ON COLUMN event_log.signature_version IS
'The version of the event log entry signature format. Defaults to 1 for backward compatibility (chain_hash not signed).';
