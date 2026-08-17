> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Citus Sharding — Formal Descope (Batch 122)

**Status:** DESCOPED — migration 010 moved to `nce/migrations/optional/`

**Date:** 2026-06-21

---

## Decision

NCE's declared Citus multi-tenant sharding strategy (migration `010_citus_sharding.sql`)
has been formally descoped from the active migration sequence.
The stack runs on stock `pgvector/pgvector:pg16` (PostgreSQL 16 + pgvector) without Citus.

---

## Why

Batch 122 attempted to resolve the "untested-claim limbo" by either deploying and testing
Citus, or formally descoping it.  The deploy-and-test path was blocked by the following
evidence gathered during the batch:

| # | Finding | Evidence |
|---|---------|---------|
| 1 | Image tag `citusdata/citus:12-pg16` does not exist | `docker manifest inspect citusdata/citus:12-pg16` → `no such manifest` |
| 2 | Nearest tag `citusdata/citus:12.1.1` (PG 16.1) lacks pgvector | `SELECT name FROM pg_available_extensions WHERE name IN ('citus','vector')` → only `citus`; `vector` absent |
| 3 | `pgvector/pgvector:pg16` (live integration stack image) has no Citus | Same query → zero rows |
| 4 | `nce/schema.sql` requires `CREATE EXTENSION IF NOT EXISTS vector` | Both extensions must coexist in the same PostgreSQL instance |
| 5 | Building a custom combined image is outside batch scope | Batch rule 3: "No new modules/deps in `nce/` runtime code" |

Running `010_citus_sharding.sql` requires both `CREATE EXTENSION citus` (line 20) and
`pgvector` for the HNSW embedding indexes (`embedding::vector`).  No pre-built image
satisfying both constraints was available.

---

## What Changed

| File | Change |
|------|--------|
| `nce/migrations/010_citus_sharding.sql` | Moved to `nce/migrations/optional/` |
| `docker-compose.yml` | Added `citus` profile service (disabled; ports 5440) |
| `.github/workflows/citus-matrix.yml` | New CI job (skips cleanly without Citus image) |
| `tests/integration/test_citus_rls.py` | Matrix test file (skipped via `CITUS_TEST_DSN` guard) |
| `docs/citus_descope.md` | This document |

---

## What Is NOT Affected

- The active migration sequence (`001` – `009`, `011` – `025`) is unchanged.
- The `topology_graph` table defined in `010` was already landed on `main` via
  a separate hotfix commit (`0c024be`) and is available in the stock schema.
- RLS, WORM event log, and all other security invariants continue to run on
  `pgvector/pgvector:pg16` as before.

---

## Resolution Path

To reactivate Citus sharding in a future batch:

1. **Image**: Build or source a combined image with Citus ≥ 12 + pgvector on PG 16.
   Example Dockerfile base:
   ```dockerfile
   FROM citusdata/citus:12.1.1
   RUN apt-get update && apt-get install -y postgresql-16-pgvector
   ```

2. **Compose profile**: Update `docker-compose.yml` `citus-coordinator` service to use
   the combined image.

3. **CI job**: Set `CITUS_AVAILABLE=true` in `.github/workflows/citus-matrix.yml` and
   change the `postgres` service image to the combined image.

4. **Migration**: Move `nce/migrations/optional/010_citus_sharding.sql` back to
   `nce/migrations/010_citus_sharding.sql` and re-run the matrix.

5. **Required GUC**: Every distributed query must run inside a `BEGIN/COMMIT` block with
   `SET LOCAL nce.namespace_id = '<tenant_uuid>'`.
   The migration sets `citus.propagate_set_commands = 'local'` to guarantee GUC
   propagation to worker connections.

---

## RLS Invariant (unchanged)

Whether on stock Postgres or Citus, the RLS invariant holds:

> A tenant query without `nce.namespace_id` set raises an error (fail-closed).
> It never returns another tenant's rows (no silent cross-tenant leak).

On Citus, this is enforced by `citus.propagate_set_commands = 'local'` (TD-010-5
in the migration) so GUC propagation reaches worker shard connections.
The test in `tests/integration/test_citus_rls.py::test_rls_holds_on_distributed_table_with_guc_propagation`
asserts this explicitly and will be activated when the image dependency is resolved.
