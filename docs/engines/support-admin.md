> **Status:** shipped · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

# Support Engine Admin Guide

> **Status:** shipped · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

This guide documents how to enable, configure, and operate the Support Engine (`nce/vertical_modules/support/`, Module 10): the namespace opt-in guard, the single migration backing its three tables and their RLS posture, the 11 mounted REST routes and their error mapping, cross-engine graph ownership (Contract A), the two real autonomy ceilings, and — matching the honesty standard of `docs/engines/sales-admin.md` — three concrete places where the code does not do what its own docstrings claim.

---

## 1. Enablement & Guard

Unlike Sales (`docs/engines/sales-admin.md` §1, which has **no** enablement gate at all), Support follows the Product/Inventory pattern: a dedicated `_guard.py` (`nce/vertical_modules/support/_guard.py`, 86 lines).

- **Convention:** `require_support_enabled(pool, namespace_id)` reads `namespaces.metadata->'support'->>'enabled'` and raises `SupportDisabledError` unless it is exactly `true` (`_guard.py:32-85`). A malformed `namespace_id` (fails `::uuid` cast) is caught as `asyncpg.exceptions.DataError` and re-raised as the same `SupportDisabledError`, not a raw DB error (`_guard.py:54-75`).
- **Enforced at the boundary only.** Every one of the 10 MCP handlers calls `_check_support_enabled` first (`mcp_handlers.py:80-95`, called at the top of each `handle_support_*`), and every REST handler in `admin_handlers/support.py` calls `require_support_enabled` directly before touching any core. The `do_*` cores themselves never check enablement — by design, so they stay callable from trusted in-process code (e.g. a future Assets(9) telemetry pipeline calling `do_open_proactive_telemetry_ticket`) without an extra opt-in round-trip.
- **One core is the exception:** `do_support_at_risk_aggregate` (`ecosystem.py:249-352`) calls `require_support_enabled` itself, inline (`ecosystem.py:276`), because it is meant to be invoked directly by Module 16's morning-brief aggregator rather than through the MCP/REST boundary — see §7.
- **No `NCE_SUPPORT_ENABLED` master config switch exists.** Enablement is entirely per-namespace metadata; there is no engine-wide kill switch in `nce/config.py` (confirmed by the config key list in §2 — only two `NCE_SUPPORT_*` keys exist and neither is an enable flag).

---

## 2. Configuration Keys (`nce/config.py:1292-1304`)

Exactly two `NCE_SUPPORT_*` keys exist in the codebase — both are autonomy ceilings, not feature flags:

| Config Key | Default | Type | Enforced where |
|---|---|:---:|---|
| `NCE_SUPPORT_AUTONOMY_DISPATCH_CEILING` | `0.0` | float | `dispatch.py:107` — max estimated cost (NOK) for an autonomous Field Tech dispatch. `0.0` means **every** dispatch needs `confirm: true` by default. |
| `NCE_SUPPORT_AUTONOMY_AUTOCLOSE_CONFIDENCE` | `0.95` | float | `tickets.py:452` — min AI confidence for an autonomous ticket close via `support_resolve_ticket(autonomous=true)`. |

**Config-as-IP JSON** (`nce/config_data/`):
- `support-sla-profiles.json` — 4 profiles (`mission_critical`, `standard`, `basic`, `best_effort`) × 4 priorities, each `{first_response_hours, resolution_hours}`. Loaded by `load_sla_profiles()` (`sla.py:58-69`); an identical hardcoded `_FALLBACK_PROFILES` table exists if the file is absent, so the two are behaviorally interchangeable in this snapshot — confirmed byte-for-byte identical against the on-disk file.
- `support-health-weights.json` — `weights` (5 keys summing the docked-points model), `churn_risk_thresholds` (`high_risk: 50.0`, `medium_risk: 75.0`, `low_risk: 100.0` — informational; the actual thresholding is hardcoded in `compute_health_score`, `health.py:117-122`, not read from this block), `default_lookback_days: 90`. Loaded by `load_health_weights()` (`health.py:45-57`), same fallback-parity pattern.

---

## 3. Database: Migration 065 & RLS

One migration, `nce/migrations/065_support_service_tickets.sql` (175 lines), creates all three Support tables. All three `ENABLE`+`FORCE ROW LEVEL SECURITY` with an identical `tenant_isolation_policy` (`namespace_id = get_nce_namespace()`, both `USING` and `WITH CHECK`) and grant `nce_app` full `SELECT, INSERT, UPDATE, DELETE` — as the migration's header notes, this is **defense-in-depth**: the live connection role is `mcp_user` (`rolsuper=true, rolbypassrls=true`), so RLS is inert at runtime and every application query must (and does, per grep of all support `*.py` files) carry an explicit `WHERE namespace_id = $N::uuid` predicate.

| Table | Primary Key | Notable constraints |
|---|---|---|
| `service_tickets` | `id` | `status` CHECK ∈ 7 values; `priority` CHECK ∈ 4 values; `source` CHECK ∈ `{nce, d365}`; `summary` non-blank; **`change_origin` CHECK ∈ 7 values — see §4 for the drift this causes** |
| `sla_clocks` | `ticket_id` (FK → `service_tickets.id` `ON DELETE CASCADE`) | `breach_type` CHECK ∈ `{first_response, resolution, both}` or NULL |
| `customer_health` | `(namespace_id, customer_id)` | `score` CHECK `0.00–100.00`; `churn_risk` CHECK ∈ 4 values; `customer_id` non-blank |

Indexes: `(namespace_id, status)`, partial `(namespace_id, room_id)` / `(namespace_id, customer_id)` / `(namespace_id, asset_id)` on `service_tickets`; `(namespace_id, breached)` and `(namespace_id, resolution_due)` on `sla_clocks`; `(namespace_id, churn_risk)` on `customer_health` (`065_support_service_tickets.sql:57-70,109-114,156-157`).

No migration after 065 touches any of these three tables (confirmed: `065` is the only file matching `service_tickets|sla_clocks|customer_health` across `nce/migrations/*.sql`, and the only file containing `ALTER TABLE service_tickets` or `service_tickets_change_origin_check`, out of 74 migrations through `074_procurement_po_lines.sql`).

---

## 4. Drift #1 — the `change_origin` CHECK constraint doesn't match the application's allow-list

This is a real, verifiable schema/application mismatch, not yet triggered because the code path that would hit it is currently unreachable (see §7):

- **Migration 065's CHECK** (`065_support_service_tickets.sql:53-54`): `change_origin IN ('sync','webhook','agent','operator','consolidation','replay','unknown')` — **7 values**.
- **Application's allow-list** (`tickets.py:32-44`, `_ALLOWED_CHANGE_ORIGINS`): the same 7, **plus `proactive_telemetry` and `proactive_health`** — **9 values**.
- **The gap is exercised in shipped code:** `proactive.py:128` hardcodes `"change_origin": "proactive_telemetry"` when building the params for `do_open_ticket` inside `do_open_proactive_telemetry_ticket`. If that function is ever called against this schema, the `INSERT INTO service_tickets` in `do_open_ticket` (`tickets.py:240-269`) will raise a Postgres `CHECK constraint "service_tickets_change_origin_check" violation` — the Python-level validation in `do_open_ticket` will happily accept `change_origin="proactive_telemetry"` (it's in `_ALLOWED_CHANGE_ORIGINS`), and the failure only surfaces at the database.
- **Why this hasn't bitten anyone yet:** `do_open_proactive_telemetry_ticket` has zero callers anywhere in `nce/` outside its own module (§7) — it is a core with no door, so nothing exercises this path today. The moment Assets(9) or any other engine wires a call to it, this migration needs a companion `ALTER TABLE service_tickets DROP CONSTRAINT ... ADD CONSTRAINT ...` adding `proactive_telemetry` and `proactive_health` to the CHECK — it does not exist yet.

---

## 5. REST Routes (`nce/admin_handlers/support.py`, mounted `nce/admin_app.py:994-1052`)

11 routes, all requiring `namespace_id` (query param on GET, JSON body on POST) validated by `_require_namespace_id` before the opt-in guard runs:

| Method | Path | Handler | Notes |
|---|---|---|---|
| GET | `/api/support/tickets` | `api_support_tickets_list` | filters: `status`, `priority`, `customer_id`, `room_id`, `asset_id`, `limit`, `offset` |
| POST | `/api/support/tickets` | `api_support_tickets_open` | 201 on success |
| GET | `/api/support/tickets/{id}` | `api_support_tickets_get` | 404 `not_found` if absent |
| GET | `/api/support/tickets/{id}/sla-clock` | `api_support_ticket_sla_clock` | lazy-seeds the clock, same as the MCP tool |
| GET | `/api/support/customers/{id}/health` | `api_support_customer_health` | `lookback_days` query param |
| POST | `/api/support/troubleshoot` | `api_support_troubleshoot` | body carries `symptom_text`/`ticket_id` |
| POST | `/api/support/tickets/{id}/resolve` | `api_support_tickets_resolve` | 409 on already-resolved/invalid-status, 422 on autoclose confidence refusal |
| POST | `/api/support/tickets/{id}/triage` | `api_support_tickets_triage` | `id` may come from path or body |
| POST | `/api/support/touchpoints` | `api_support_touchpoints_record` | requires `customer_id`, `answer` in body |
| POST | `/api/support/tickets/{id}/dispatch` | `api_support_tickets_dispatch` | 409 on `dispatch_ceiling_exceeded` / invalid status |
| POST | `/api/support/sync/now` | `api_support_sync_now` | see §7 for what the "sync" actually does |
| GET | `/api/support/sync/status` | `api_support_sync_status` | **no MCP-tool equivalent; see §7's second drift item** |

**Error mapping** (`admin_handlers/support.py:30-37`, consistent across all 11 handlers): missing/invalid `namespace_id` or path `id` → 422; `ValueError` from a core → 422; support vertical not enabled → 409; ticket absent → 404; ticket already resolved / invalid status transition / dispatch ceiling exceeded → 409; anything else → 500 via `admin_error_response`.

**Mutating routes bump the MCP response cache generation** (`bump_mcp_cache_generation`, called from `tickets_open`, `tickets_resolve`, `touchpoints_record`, `tickets_dispatch`, `sync_now` — but **not** `tickets_triage`, which is read-only despite being a `POST`).

---

## 6. Cross-Engine Graph Ownership (Contract A)

`nce/config_data/node-ownership.json:60-67` assigns `owner_engine: "support"` to four node types: `TICKET`, `SLA`, `SUPPORT_HEALTH_SCORE`, `SUPPORT_DIAGNOSIS`. Every boundary-edge write in this module calls `assert_owner(conn, namespace_id, "TICKET", "support")` before writing (`dispatch.py:192`, `ecosystem.py:96,201`, `proactive.py:120`) — confirming Support owns the `TICKET` side of the edge without asserting ownership over the object side:

| Edge | Written by | Object side owned by |
|---|---|---|
| `TICKET -[dispatched_as]-> WORK_ORDER` | `do_dispatch_work_order` (`dispatch.py`) | `field_tech` (`node-ownership.json:68-69`) |
| `TICKET -[failure_pattern]-> PRODUCT_SKU` | `do_record_failure_pattern` (`ecosystem.py`) | Product(2) |
| `TICKET -[upsell_opportunity]-> QUOTE\|OPPORTUNITY` | `do_record_upsell_signal` (`ecosystem.py`) | Sales(5) |
| `TICKET -[about]-> ASSET` | `do_open_proactive_telemetry_ticket` (`proactive.py`) | Assets(9) |

Support never asserts ownership over `WORK_ORDER`, `PRODUCT_SKU`, `QUOTE`/`OPPORTUNITY`, or `ASSET` — it only ever writes the edge, consistent with the module's own header comments (`ecosystem.py:10-12`, `dispatch.py:8-9`, `proactive.py:8`).

---

## 7. Operational Concerns — SLA mechanics, and two more drift items

### 7.1 SLA clock mechanics
`do_sla_clock` (`sla.py:196-315`) is read-and-maybe-write: a read that crosses a breach threshold writes the new `breached`/`breach_type` back to `sla_clocks` in the same call (`sla.py:280-298`). There is no background job that ticks these clocks — a clock's breach state is only ever recomputed the next time something calls `support_sla_clock` (via MCP, REST, or the internal query path in `do_query_ticket`/`do_support_at_risk_aggregate`). A ticket that nobody queries can sit breached-but-unflagged indefinitely. Pause/resume of a clock (`paused_intervals`) is modeled in `evaluate_sla_status` but **no `do_*` function in this module ever writes to `paused_intervals`** — grep confirms it is only ever read, never appended to, anywhere in `nce/vertical_modules/support/`. Pausing an SLA clock is schema-supported but has no code path that does it today.

### 7.2 Drift #2 — `support_sync_now`'s "proactive sweep" is a ticket count, not a sweep
`sync.py`'s module docstring says the function "conducts a proactive telemetry sweep" (`sync.py:6`); `do_sync_now`'s own docstring repeats "runs a proactive operational sweep across active tickets and asset health" (`sync.py:32-34`). The actual code (`sync.py:87-102`) runs one query — `SELECT count(*) FROM service_tickets WHERE namespace_id = $1 AND status = 'open'` — and returns the count as `active_tickets_checked`. It reads no asset/telemetry table, and it never calls `do_open_proactive_telemetry_ticket` or any other action. The D365 half of the same function (`mode=d365|both`, delegating to `handle_d365_sync_now`) is real and does perform an actual incremental Dataverse sync. Only the "proactive" half is a stub dressed as a sweep.

### 7.3 Drift #3 — `GET /api/support/sync/status` is fully synthetic
`do_sync_status` (`sync.py:114-131`) has a REST route (`api_support_sync_status`) but **no MCP tool** — it is one of the few support surfaces reachable only via REST. Read the implementation and it never queries a table: it unconditionally returns `status: "healthy"`, `last_sync: <the current call time>` (not any recorded prior sync timestamp), `source_mode` echoing back whatever the caller passed (or `"both"`), and `adapter: "dynamics365"` — all hardcoded or trivially derived from the request, none of it backed by state. A namespace whose D365 credentials are completely broken and has never synced successfully will report the identical `"healthy"` response as one syncing cleanly every minute. Do not wire an alert on this endpoint without first fixing it to read real sync history — there is currently nowhere in the schema this function could even read that history from (no sync-run table exists in migration 065 or anywhere else in `nce/migrations/`).

### 7.4 Autonomy / governance summary
| Operation | Ceiling | Fail-closed behavior |
|---|---|---|
| `support_dispatch_work_order` | `NCE_SUPPORT_AUTONOMY_DISPATCH_CEILING` (default `0.0`) | Missing `estimated_cost` **or** cost above ceiling → `dispatch_ceiling_exceeded` unless `confirm: true` (`dispatch.py:103-131`) |
| `support_resolve_ticket(autonomous=true)` | `NCE_SUPPORT_AUTONOMY_AUTOCLOSE_CONFIDENCE` (default `0.95`) | `confidence` below the threshold → `autoclose_confidence_refusal`; caller-supplied threshold can only raise the bar, never lower it (`tickets.py:448-464`) |

Both ceilings are enforced entirely in application code (`dispatch.py`, `tickets.py`) — there is no database-level backstop equivalent to Economy's `trg_economy_postings_assert_balanced` trigger for either guard.

---

## 8. Operational Checklist

1. **Enable the tenant:** set `metadata.support.enabled = true` on the `namespaces` row for the target namespace — every MCP tool and REST route refuses with a 409 / `-32005` until this is set.
2. **Confirm SLA profiles cover your rooms:** verify `support-sla-profiles.json` has entries for every `sla_profile` string your intake process assigns; an unknown profile silently falls back to `"standard"` (`sla.py:80-81`).
3. **Set the dispatch ceiling deliberately:** the default (`0.0`) means every single dispatch requires a human `confirm: true`. Raise `NCE_SUPPORT_AUTONOMY_DISPATCH_CEILING` only once you've reviewed the cost-estimation inputs feeding `estimated_cost`.
4. **Don't trust `GET /api/support/sync/status` as a monitoring signal** until §7.3's drift is fixed — poll `support_sync_now`'s own response (`d365_sync.status`) for real adapter health instead.
5. **Before wiring `do_open_proactive_telemetry_ticket` to anything** (Assets telemetry, a cron job, etc.), land the migration fix from §4 first — the CHECK constraint will otherwise reject every proactive ticket it tries to open.

---

## Appendix: Drift & Gaps Flagged During This Audit (not fixed)

1. **`change_origin` CHECK constraint (migration 065) is missing 2 of the 9 values the application layer accepts** — `proactive_telemetry` and `proactive_health` — and `proactive.py` already hardcodes one of them into a real insert path. Currently latent because that path has no caller (§4).
2. **`do_health_score` never varies `frustration_score` or `touchpoint_avg`** from their zero-contribution defaults (`health.py:223-224`) — 30% of the health score's documented weight is structurally inert. See user guide §3.3.
3. **`do_record_touchpoint` does not update `last_touchpoint_at`** despite its own docstring's claim — the upsert in `do_health_score` reads back the prior value unchanged (`health.py:216,243`).
4. **`support_sync_now`'s "proactive sweep" is a `COUNT(*)`, not telemetry-driven action** (§7.2) — the D365 half is real, the proactive half is not.
5. **`GET /api/support/sync/status` reads no state** — always reports `"healthy"` regardless of actual sync history, and no sync-run table exists yet to back a real implementation (§7.3).
6. **4 domain cores have no MCP tool and no REST route** — `do_record_failure_pattern`, `do_record_upsell_signal`, `do_support_at_risk_aggregate`, `do_open_proactive_telemetry_ticket` (user guide §8). Confirmed zero callers anywhere in `nce/` outside their own module.
7. **`nce/vertical_modules/support/__init__.py`'s `__all__` only lists 6 of the 10 `handle_support_*` functions** in `mcp_handlers.py` (missing `handle_support_triage_ticket`, `handle_support_record_touchpoint`, `handle_support_dispatch_work_order`, `handle_support_sync_now`) — harmless today because `tool_registry.py:62` imports the whole `mcp_handlers` module directly rather than through the package `__all__`, but a package-surface inconsistency worth closing.
8. **No pause/resume code path exists for `sla_clocks.paused_intervals`** — the field is read by `evaluate_sla_status` but nothing in the module ever writes to it (§7.1).
