-- Migration: Add state digest columns to replay_runs for replay state verification
ALTER TABLE replay_runs ADD COLUMN IF NOT EXISTS source_state_digest TEXT;
ALTER TABLE replay_runs ADD COLUMN IF NOT EXISTS target_state_digest TEXT;
ALTER TABLE replay_runs ADD COLUMN IF NOT EXISTS digest_match BOOLEAN;
