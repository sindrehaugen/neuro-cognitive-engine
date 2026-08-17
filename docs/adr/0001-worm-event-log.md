> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# ADR-0001: WORM Immutability for event_log via prevent_mutation Trigger

## Status

Shipped

## Context

`event_log` is the append-only audit spine of NCE. Every memory write, agent action, and system event produces a row there. Because the chain-hash integrity check depends on the immutability of every prior row, any UPDATE or DELETE would silently corrupt the Merkle chain and undermine the tamper-evidence guarantees.

Without an enforcement mechanism, application bugs, ad-hoc DBA queries, or future code paths could mutate rows, breaking chain verification retroactively and silently.

Alternatives considered:
- **Revoke UPDATE/DELETE grants only** — grants can be re-added; does not survive superuser access or future provisioning mistakes.
- **Application-layer prohibition** — fragile; bypassed by any raw SQL connection.
- **Postgres trigger** — enforced at the storage layer for all connections regardless of role, including superusers running explicit `FORCE ROW LEVEL SECURITY` bypass.

## Decision

A PostgreSQL trigger function `prevent_mutation()` is attached to `event_log` (and `event_parents`) as a `BEFORE UPDATE OR DELETE FOR EACH ROW` trigger. The trigger raises an exception unconditionally:

```
RAISE EXCEPTION 'event_log is immutable (WORM). % operation is forbidden.', TG_OP;
```

The trigger is installed idempotently inside a `DO $$ IF NOT EXISTS ... $$` block so schema migrations can re-run safely. The `event_log` table is also `PARTITION BY RANGE (occurred_at)`, so the trigger must be defined on the parent table; PostgreSQL propagates it to all partitions automatically.

`NCEEngine.connect()` calls `_verify_worm_enforcement()` at startup to assert the trigger exists before accepting traffic.

**Source citations** (verified via `git show main:<path>`):
- `nce/schema.sql:821` — `CREATE OR REPLACE FUNCTION prevent_mutation()` — defines the trigger function
- `nce/schema.sql:823` — `RAISE EXCEPTION 'event_log is immutable (WORM). % operation is forbidden.'` — enforcement body
- `nce/schema.sql:851-857` — `CREATE TRIGGER trg_event_log_worm BEFORE UPDATE OR DELETE ON event_log FOR EACH ROW EXECUTE FUNCTION prevent_mutation()` — attachment to event_log
- `nce/schema.sql:1253-1259` — same trigger reused on `event_parents`
- `nce/migrations/022_muscles_schema_contract.sql:65-71` — `event_parents` WORM attachment in migrations layer
- `nce/orchestrator.py:168` — `await self._verify_worm_enforcement()` — startup assertion

## Consequences

### Positive

- Any mutation attempt on `event_log` (UPDATE or DELETE) raises a database exception immediately, regardless of which connection or role issues it.
- Chain-hash verification (`nce/admin_handlers/fleet.py:217`) can trust that no row has been silently mutated between writes.
- Startup verification means a misconfigured database (trigger missing) fails loudly before serving requests.

### Negative / Trade-offs

- Rows with errors cannot be corrected in-place; a compensating event must be appended instead.
- Table partitioning requires the trigger to live on the parent; DDL changes to partitions require re-verifying trigger propagation.
- `_verify_worm_enforcement()` adds one metadata query to every cold-start path.
