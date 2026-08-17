> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# chain_hash backfill runbook

One-off migration: `scripts/backfill_chain_hash.py`

## Preconditions

- PostgreSQL role with `TRIGGER` privilege on `event_log` (owner or superuser).
- `NCE_MASTER_KEY` set (same as production signing).
- Maintenance window — no concurrent appends to `event_log` during the WORM-disabled window.
- Backup or snapshot taken before running.

## Run

```bash
PG_DSN="postgresql://..." python scripts/backfill_chain_hash.py
```

Docker Compose:

```bash
docker compose exec admin python scripts/backfill_chain_hash.py
```

## Safety checks (built into script)

1. Verifies `trg_event_log_worm` exists before changes.
2. Disables the WORM trigger only after logging a warning.
3. Recomputes `chain_hash` per namespace in `event_seq` order.
4. Re-enables the trigger and **verifies** it is enabled before exit.
5. On failure, attempts best-effort re-enable; logs `CRITICAL` if re-enable fails.

## Post-run verification

```sql
SELECT COUNT(*) FROM event_log WHERE chain_hash IS NULL;
-- expect 0

SELECT t.tgenabled FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
WHERE c.relname = 'event_log' AND t.tgname = 'trg_event_log_worm';
-- expect 'O' (enabled), not 'D'
```

Run Merkle verification from admin health or `verify_merkle_chain` at startup.

## Rollback

If the script fails with `CRITICAL: Could not re-enable WORM trigger`, manually run:

```sql
ALTER TABLE event_log ENABLE TRIGGER trg_event_log_worm;
```

Then confirm `tgenabled` is not `D` before resuming traffic.
