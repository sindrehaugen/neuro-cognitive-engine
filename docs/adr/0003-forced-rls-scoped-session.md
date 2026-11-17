> **Status:** audited · **Verified-against:** `628b231` (main) · **Last-audited:** 2026-09-16

# ADR-0003: Forced RLS + scoped_pg_session for Tenant Isolation

## Status

Shipped (DDL & Scoped Sessions) · **Updated in `v3.4`–`v3.5.2`**: All **36 Core forced-RLS tables** (plus **96 vertical-engine forced-RLS tables** and downstream OT/edge plugin tables via `nce.plugins`) enforce `ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY` for the non-bypass `nce_app` runtime role, verified at boot by `verify_rls_catalog_consistency()`. In `v3.5.1` (Migration `038`), `get_nce_namespace()` and `get_nce_external_scope()` were marked `STABLE PARALLEL SAFE` and all RLS policies were rewritten to `(SELECT get_nce_namespace())` so PostgreSQL evaluates the tenant GUC once per statement as an `InitPlan` (`0.56 ms`). *(Historical note from 2026-09-16: prior to `nce_app` role enforcement, local dev deployments connecting as superuser `mcp_user` relied on application-level `WHERE` predicates; see [`docs/vertical_engines/_security/c3-external-scope-adversarial-review.md`](https://github.com/sindrehaugen/nce-vertical-engines/blob/main/docs/vertical_engines/_security/c3-external-scope-adversarial-review.md)).*

## Context

NCE is a multi-tenant system: each tenant (namespace) must be strictly isolated at the data layer. Without enforcement, a bug in any query — missing a `WHERE namespace_id = $1` clause, a JOIN that broadens scope, or a future code path that forgets to filter — would silently expose one tenant's data to another.

Application-layer filtering (checking `namespace_id` in Python before issuing SQL) is fragile. A single omitted WHERE clause bypasses all application logic.

The requirement is a data-layer guarantee that no connection can read or write another tenant's rows without explicit authorisation, even if application code is defective.

## Decision

Two mechanisms work together:

**1. FORCE ROW LEVEL SECURITY on 14 tenant tables (migration 001)**

`nce/migrations/001_enable_rls.sql` enables `ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY` on every tenant table. `FORCE ROW LEVEL SECURITY` causes RLS policies to apply even to the table owner, preventing any application role from bypassing them.

The enforced tables are: `memories`, `kg_nodes`, `kg_edges`, `pii_redactions`, `memory_salience`, `contradictions`, `snapshots`, `event_log`, `resource_quotas`, `consolidation_runs`, `bridge_subscriptions`, `dead_letter_queue`, `embedding_migrations`, `memory_embeddings`.

Each table gets a `tenant_isolation_policy` that checks `namespace_id = get_nce_namespace()`, where `get_nce_namespace()` reads the `nce.namespace_id` session variable and raises if it is unset.

**2. scoped_pg_session context manager (nce/db_utils.py)**

Every tenant data path must use `scoped_pg_session(pool, namespace_id)`. This context manager:
- Acquires a connection from the pool with a bounded timeout (`POOL_ACQUIRE_TIMEOUT = 10.0 s`).
- Opens an explicit transaction.
- Calls `set_config('nce.namespace_id', <uuid>, true)` (`SET LOCAL` — scoped to the transaction).
- Yields the connection; all SQL issued on it is filtered by RLS.
- Clears the namespace setting automatically at transaction end.

A companion `unmanaged_pg_connection` path exists for global/admin operations (schema maintenance, cron scans) but requires the call site to be registered in `UNMANAGED_PG_AUDITED_SITES` — enforced at runtime.

**Source citations** (verified via `git show main:<path>`):
- `nce/migrations/001_enable_rls.sql:61-80` — `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY` loop over 14 tables
- `nce/migrations/001_enable_rls.sql:154-157` — quality gate: raises if RLS or FORCE RLS is missing after migration
- `nce/migrations/001_enable_rls.sql:20-30` — `get_nce_namespace()` function: reads `nce.namespace_id`, raises on NULL/invalid UUID
- `nce/db_utils.py:20` — `POOL_ACQUIRE_TIMEOUT: Final[float] = 10.0`
- `nce/db_utils.py:100-135` — `scoped_pg_session` — full implementation with `SET LOCAL` and transaction wrapping
- `nce/db_utils.py:22-38` — `UNMANAGED_PG_AUDITED_SITES` — allowlist for bypass paths
- `nce/db_utils.py:80-97` — `unmanaged_pg_connection` — runtime enforcement of allowlist
- `nce/orchestrator.py:169` — `await self._verify_rls_enforcement()` — startup assertion

## Consequences

### Positive (Design Intent vs. Deployment Reality)

- *Design Intent:* A missing `WHERE namespace_id` clause in application code was intended to be silently corrected by the RLS policy on role `nce_app`.
- 🔴 *Deployment Reality:* As measured in Wave A-T6 (PR #148), the application connects as `mcp_user` (`rolsuper = true, rolbypassrls = true`) and `scoped_pg_session` never executes `SET ROLE`. Therefore, RLS policies targeting `nce_app` do not evaluate; PostgreSQL does NOT silently correct a missing `WHERE` clause. Application queries must explicitly include `WHERE namespace_id = $1`.
- The startup assertion (`_verify_rls_enforcement`) validates catalog structure (`relrowsecurity` and policy existence), but does not assert that the connecting role is subject to RLS.
- `unmanaged_pg_connection` audit-site registration creates a paper trail for every intentional RLS bypass.

### Negative / Trade-offs

- Every tenant SQL operation must be inside a transaction (required for `SET LOCAL`); this increases lock contention relative to autocommit reads.
- Long-running operations (LLM calls, embedding generation) must not be performed inside the `scoped_pg_session` block — the docstring explicitly warns against this.
- The `get_nce_namespace()` function raises at query time if `nce.namespace_id` is unset, which means a missing context manager produces a database error rather than a Python exception; callers must be instrumented to surface this clearly.
- Adding a new tenant table requires manually adding it to the migration's ARRAY and re-running, then verifying the quality gate.
