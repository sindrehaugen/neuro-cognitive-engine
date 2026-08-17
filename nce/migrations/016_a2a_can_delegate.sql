-- Migration: Add can_delegate to a2a_grants table (Batch 41)
ALTER TABLE a2a_grants ADD COLUMN IF NOT EXISTS can_delegate BOOLEAN NOT NULL DEFAULT FALSE;
