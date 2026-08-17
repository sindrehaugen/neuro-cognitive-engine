-- 018_memories_envelope_dek.sql
-- Part II.4 (Provable Forgetting) — envelope-encryption DEK columns on memories.
-- Adds the wrapped Data Encryption Key (envelope-encrypted under NCE_MASTER_KEY
-- via nce.envelope.wrap_dek) and an opaque DEK identifier.  Destroying
-- wrapped_dek renders the corresponding episodes.raw_data ciphertext
-- permanently undecryptable.  Read-path/raw_data encryption wiring is Batch 46.
-- ============================================================================

ALTER TABLE memories ADD COLUMN IF NOT EXISTS wrapped_dek BYTEA;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS dek_key_id TEXT;

COMMENT ON COLUMN memories.wrapped_dek IS
'AES-256-GCM-wrapped Data Encryption Key (envelope-encrypted under NCE_MASTER_KEY). NULL until the memory payload is encrypted (Batch 46). Zeroing this column crypto-shreds episodes.raw_data.';

COMMENT ON COLUMN memories.dek_key_id IS
'Opaque identifier (no key material) for the wrapped DEK, used in deletion receipts and audit events.';
