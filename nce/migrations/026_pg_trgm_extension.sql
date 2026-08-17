-- ============================================================================
-- Migration 025: Enable pg_trgm Extension
-- ============================================================================
-- Purpose: Enable the PostgreSQL pg_trgm (trigram) extension for fuzzy-string
-- matching and similarity calculations (similarity() function). Required by
-- Module 0.Wave 5 (C1 entity-resolution resolve() function).
--
-- Idempotent: safe to re-run on every boot.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;
