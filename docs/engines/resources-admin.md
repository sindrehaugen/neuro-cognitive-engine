> **Status:** shipped · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

# Resources Engine Admin Guide

> **Status:** shipped · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

This guide documents the administrative, operational, and security surface of the **Resources Engine** (`nce/vertical_modules/resources/`, internally Module 15): tenant enablement, the `allocations` migration and its database-enforced no-double-booking guarantee, the 10 mounted REST routes and their auth model, the 9 MCP tools' cache/mutation/admin flags, a JSON-RPC error-mapping finding that was live in this codebase and is now resolved, the AI-planner weights and Norwegian travel/per-diem config, and the RS-2 through RS-5 hardening rules enforced in code.

---

## 1. Engine Enablement & Configuration

### 1.1 Two-layer enablement, one of them caller-supplied
`require_resources_enabled()` (`nce/vertical_modules/resources/_guard.py:37-45`) is called at the top of every `do_*` core in this module. It checks two things:
1. **Global toggle:** `cfg.NCE_RESOURCES_ENABLED` (default `true`, `nce/config.py:1334`). If false, every call raises `ResourcesDisabledError` regardless of tenant.
2. **Per-tenant opt-out:** `namespace_metadata.resources.enabled == False`. This check only runs `if namespace_metadata:` (`_guard.py:43-45`) — and `namespace_metadata` is a plain key inside the `params` dict passed by the *caller*.

> [!WARNING]
> **The per-tenant disable is only as strong as whoever calls the tool.** There is no dispatch-layer code (MCP dispatch, `mcp_handlers.py`, or REST, `admin_handlers/resources.py`) that fetches the tenant's `namespaces.metadata` row from the database and injects it as `namespace_metadata` before invoking a `do_*` core — grep confirms no such site exists outside this module. Both `_check_resources_enabled()` in `mcp_handlers.py:46-49` and `_check_enabled()` in `admin_handlers/resources.py:62-63` simply forward whatever `namespace_metadata` key the caller happened to include in its own request body/arguments. A caller that omits `namespace_metadata` from its request silently bypasses the per-tenant disable — the global toggle (`NCE_RESOURCES_ENABLED`) is the only enforcement that is unconditional. If per-tenant disable needs to be load-bearing, the fetch-and-inject step has to be added at the dispatch layer, not assumed to already exist.

Namespaces are documented to opt in via `metadata.resources.enabled = true` (per the design spec, `docs/vertical_engines/15-staff-resources-engine.md` "Config keys"), consistent with the disable check above, but nothing in the shipped code enforces a default-*off* posture the way `NCE_RESOURCES_ENABLED=false` would — the default is effectively "enabled everywhere unless a caller actively tells the guard otherwise."

### 1.2 Configuration keys (`nce/config.py:1334-1345`)

| Config key | Default | Type | Used at |
|---|---|:---:|---|
| `NCE_RESOURCES_ENABLED` | `true` | bool | `_guard.py:39` — global kill switch |
| `NCE_RESOURCES_AUTONOMY_ALLOCATION_CEILING` | `50000.0` (NOK) | float | `planner.py:247` — tiered-autonomy auto-reserve threshold |
| `NCE_RESOURCES_CALENDAR_SYNC_ENABLED` | `false` | bool | `field_schedule.py:383` — gates the M365/Outlook calendar-sync block in `resources_field_schedule`; off by default, falls back to `"provider": "internal"` |
| `NCE_RESOURCES_TRAVEL_PROVIDER` | `"internal_plan"` | str | Declared in config; not yet branched on inside `travel.py` — travel booking is internally-planned only in this snapshot (no external booking-provider adapter is wired) |
| `NCE_RESOURCES_TRAVEL_MAX_AUTO_SPEND` | `10000.0` (NOK) | float | `travel.py:242` — Contract-B spend-gate ceiling for `resources_plan_travel` book actions |

### 1.3 Config-as-IP JSON files
Both live beside the code, not in `nce/config_data/` like most other engines:
- `nce/vertical_modules/resources/resources-allocation-weights.json` — the 5 planner weights (§5).
- `nce/vertical_modules/resources/resources-travel-policy.json` — Norwegian statutory diett rates, meal-deduction percentages, lodging/flight cost caps, and the spend-gate defaults (§6). Both fall back to hardcoded defaults in Python (`planner.py:48-54`, `travel.py:43-60`) if the file is missing or fails to parse — a load failure is logged but never fatal.

---

## 2. Database Architecture: the No-Double-Booking Guarantee

Migration `nce/migrations/071_allocations_btree_gist.sql` creates 4 tables (`allocations`, `travel_legs`, `stays`, `per_diems`), all `ENABLE + FORCE ROW LEVEL SECURITY` with an identical `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`, and all granting `nce_app` full `SELECT, INSERT, UPDATE, DELETE`.

### 2.1 The exclusion constraint
The concurrency guarantee described in the spec's hardening notes ("solve conflict-detection in the DB, not the app") is this constraint (migration line 4, extension; lines 22-25, constraint):

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;

CONSTRAINT exclude_resource_double_booking EXCLUDE USING gist (
    resource_id WITH =,
    tstzrange(starts_at, ends_at) WITH &&
) WHERE (status <> 'released')
```

This is a `gist`-backed `EXCLUDE` constraint: PostgreSQL rejects, at commit time, any row whose `(resource_id, tstzrange(starts_at, ends_at))` overlaps an existing non-`released` row for the same resource — **impossible to violate from any client**, no matter how many app-layer callers race each other, because the check runs inside the database's own index structure rather than in a `SELECT`-then-`INSERT` app-level check. `do_reserve` (`allocations.py:135-171`) catches the resulting Postgres error by `sqlstate == "23P01"` (or the constraint name / `asyncpg.exceptions.ExclusionViolationError` as fallbacks) and re-raises it as a domain `ResourceConcurrencyError` — the caller never sees a raw database error.

The constraint is `WHERE (status <> 'released')`, so releasing an allocation (`do_release`, `allocations.py:205-253`, sets `status = 'released'`) immediately frees that window for a new booking without needing to delete the row — audit history is preserved.

### 2.2 The positive-control test
`tests/test_resources_concurrency_race.py` proves the guarantee two ways:
1. **`test_concurrent_overlapping_reservations_race`** (line 30) — fires two overlapping `do_reserve` calls concurrently via `asyncio.gather` across separate pool connections and asserts *exactly* one succeeds and the other raises `ResourceConcurrencyError` — proving the constraint resolves real concurrent races, not just sequential app-level checks (which would be vacuous under true concurrency).
2. **`test_positive_control_dropping_constraint_allows_race`** (line 148) — the RED-proving control. It explicitly runs `ALTER TABLE allocations DROP CONSTRAINT IF EXISTS exclude_resource_double_booking`, fires the same overlapping pair, and asserts **both succeed** (double-booking occurs) — proving the test would fail loudly if the constraint were ever accidentally removed from a migration, rather than passing vacuously. It restores the constraint in a `finally` block after cleaning up the resulting overlapping rows (lines 201-217).

A third test, `test_consecutive_non_overlapping_reservations_succeed` (line 101), confirms the boundary case: two allocations touching exactly at `12:00:00` (`[09:00,12:00)` then `[12:00,15:00)`) both succeed, because Postgres `tstzrange` half-open ranges do not consider a shared boundary point an overlap.

Unit-level coverage of the same schema exists in `tests/unit/test_resources_allocations.py` and `tests/unit/test_resources_schema.py` (not read in full for this audit; the integration race test above is the one that actually exercises the constraint under concurrency).

### 2.3 Schema notes
- `check_allocation_dates CHECK (ends_at > starts_at)` — a structural backstop behind the Python-level `_parse_datetime`/ordering checks in `allocations.py:90-91`.
- Two indexes: `(namespace_id, resource_id, starts_at, ends_at)` and `(namespace_id, demand_kind, demand_id)` — the first backs both the exclusion constraint's own lookups and `do_detect_conflicts`'s self-join; the second backs demand-side lookups.
- `travel_legs`/`stays`/`per_diems` each FK to `allocations(id) ON DELETE CASCADE` and carry their own tenant RLS policy, independently of the parent allocation's.

---

## 3. REST Administration (`nce/admin_handlers/resources.py`)

10 routes mounted in `nce/admin_app.py:1227-1276`, all under `/api/resources/*`:

| Route | Method | Handler | Mutating? | Notes |
|---|---|---|:---:|---|
| `/api/resources/capacity` | GET | `api_resources_resolve_capacity` (`:71`) | | |
| `/api/resources/plan-allocation` | POST | `api_resources_plan_allocation` (`:105`) | | can implicitly reserve — see §5.2 |
| `/api/resources/reserve` | POST | `api_resources_reserve` (`:139`) | ✔ | bumps MCP cache generation (`:159`) |
| `/api/resources/release` | POST | `api_resources_release` (`:178`) | ✔ | bumps MCP cache generation (`:198`) |
| `/api/resources/conflicts` | GET | `api_resources_detect_conflicts` (`:215`) | | |
| `/api/resources/material-flow` | POST | `api_resources_plan_material_flow` (`:249`) | ✔ | bumps MCP cache generation (`:269`) |
| `/api/resources/travel` | POST | `api_resources_plan_travel` (`:288`) | ✔ | bumps cache generation **only** when `action == "book"` (`:308-309`) — a `plan` call is non-mutating |
| `/api/resources/field-schedule` | GET | `api_resources_field_schedule` (`:326`) | | requires `resource_id` query param |
| `/api/resources/forecast` | GET | `api_resources_forecast_demand` (`:369`) | | |
| `/api/resources/pulse` | GET | `api_resources_capacity_pulse` (`:408`) | | **no MCP tool** — see user guide §7 |

### 3.1 Error-to-status mapping
Every handler follows the same pattern (e.g. `:88-97`): `ResourcesDisabledError` → `409`; `ResourceConcurrencyError` → `409` (reserve only); `ResourceNotFoundError` → `404`; `(ResourceValidationError, ValueError)` → `422`; anything else → `admin_error_response(...)` (generic 500-class). Engine-not-connected is checked first on every route and returns `503`.

### 3.2 Auth
`admin_app.py` mounts this router under the shared admin middleware stack (`build_admin_middleware()`, `admin_app.py:134-140+`): `MTLSAuthMiddleware` protecting the `/api/` prefix plus an HMAC nonce layer and rate limiting. Per the module-level contract comment at the top of `admin_app.py` (lines 51-66), **admin_app is reachable only by internal `employee`/agent principals** (`ADMIN_PRINCIPAL_KIND = "employee"`, line 67) — external/contractor/customer principals are never authenticated on this surface. There is no Resources-specific auth beyond this shared admin gate; the contractor data redaction described in the user guide §4/§8 is a data-shaping concern, not an authentication boundary — it applies even to authenticated internal callers who happen to be looking at a contractor's own record.

---

## 4. MCP Tool Registry Flags

All 9 tools registered `nce/tool_registry.py:1253-1292`:

| Tool | Line | `cacheable` | `admin_only` | `mutation` |
|---|---|:---:|:---:|:---:|
| `resources_resolve_capacity` | 1253 | ✔ | | |
| `resources_plan_allocation` | 1257 | | | |
| `resources_detect_conflicts` | 1261 | ✔ | | |
| `resources_forecast_demand` | 1265 | ✔ | | |
| `resources_field_schedule` | 1269 | ✔ | | |
| `resources_reserve` | 1273 | | | ✔ |
| `resources_release` | 1278 | | | ✔ |
| `resources_plan_material_flow` | 1283 | | **✔** | ✔ |
| `resources_plan_travel` | 1289 | | | ✔ |

`resources_plan_material_flow` is the **only** `admin_only` tool of the 9 — a caller without admin scope cannot dispatch it via MCP even though its underlying REST equivalent (`/api/resources/material-flow`) carries no additional employee-vs-admin distinction beyond the shared admin-surface gate in §3.2. `resources_plan_allocation` is not itself flagged `mutation: true` despite being able to call `do_reserve` internally when `auto_reserve=true` and the job is sub-ceiling (`planner.py:250-282`) — the dispatch layer's cache-generation bump (tied to the `mutation` flag) will not fire on an auto-reserving plan call the way it does for an explicit `resources_reserve`. The same 9 tools are declared a second time, independently, as JSON-RPC tool schemas for the stdio transport in `nce/mcp_stdio_tools.py:4905-5082` — both registrations list the identical 9 names; there is no drift between the two surfaces as of this audit.

---

## 5. The AI Planner Weights (`resources-allocation-weights.json`)

Loaded by `load_allocation_weights()` (`planner.py:32-54`). Per-tenant, hot-swappable without a code change:

```json
{
  "version": "1.0",
  "skill_match_weight": 0.35,
  "travel_distance_weight": 0.20,
  "load_balance_weight": 0.15,
  "internal_preference_weight": 0.15,
  "outcome_history_weight": 0.15
}
```

The five weights are consumed as-is inside `do_plan_allocation` (user guide §2.1 documents the per-component scoring rules). A caller can also override individual weights per-call via `tenant_weights` in the request body/arguments — those are merged over the file defaults (`planner.py:87-88`) and are **not** persisted back to the JSON file; a one-off override does not change future calls. There is no validation that the five weights sum to `1.0` — a badly-tuned override (e.g. all weights set to `2.0`) will not error, it will simply produce composite scores outside the conventional `[0,1]` range, which still sort correctly relative to each other but will look unusual if surfaced numerically to an operator.

---

## 6. Travel Policy & the Contract-B Spend Gate (`resources-travel-policy.json`)

Loaded by `load_travel_policy()` (`travel.py:34-60`). Full structure on disk:

```json
{
  "version": "1.0",
  "jurisdiction": "NO",
  "currency": "NOK",
  "rules": {
    "statutory_rates_2026": {"day_trip_short_6_to_12h": 360.0, "day_trip_long_over_12h": 680.0, "overnight_hotel": 940.0, "overnight_unspecified": 450.0},
    "meal_deductions_pct": {"breakfast": 0.20, "lunch": 0.30, "dinner": 0.50},
    "lodging_standard_cap_nok": 2200.0,
    "flight_economy_standard_cap_nok": 4500.0,
    "distance_threshold_km_for_travel": 75.0,
    "overnight_distance_threshold_km": 150.0
  },
  "spend_gate": {"default_max_autonomous_spend_nok": 10000.0, "requires_idempotency_key": true, "multi_jurisdiction_supported": false}
}
```

Two things worth an operator's attention:
1. **`lodging_standard_cap_nok`/`flight_economy_standard_cap_nok`/the two distance thresholds are present in the policy file but not read anywhere in `travel.py`.** Only `statutory_rates_2026` and `meal_deductions_pct` are consumed by `calculate_norwegian_diett()` (`travel.py:76-86`); the cost caps and distance thresholds are declared config with no enforcing code path in this module as of this audit. They read as forward-looking placeholders (perhaps intended to gate whether travel/overnight is even necessary, or to cap booked costs) rather than active policy.
2. **`multi_jurisdiction_supported: false` is accurate and load-bearing** — `calculate_norwegian_diett()` hardcodes `"jurisdiction": "NO"` in its return value regardless of what the policy file says, and there is no per-country dispatch anywhere in `travel.py`. A tenant operating outside Norway gets Norwegian tax-deduction math applied to their per-diems today; multi-jurisdiction is explicitly out of scope, matching the spec's own note that this is future work.

The idempotency-key + ceiling + explicit-confirm gate on `action="book"` is described from the caller's side in the user guide §6.2; from an operator's side, the practical implication is the same as any Contract-B (autonomous-spend) surface elsewhere in NCE: **treat `resources_plan_travel` with `action="book"` as immediately-effective spend once it clears the ceiling/confirm check** — there is no separate human-approval step beyond that single ceiling gate.

---

## 7. The `-32603` Finding — Verified and RESOLVED at This Commit

The finding tracked on the ORCH_BOARD (searched for `resources_resolve_capacity`) describes `resources_resolve_capacity` surfacing domain refusals as JSON-RPC `-32603 Internal error` instead of a proper client-actionable error code, because a `ResourcesError` (the base class for every domain exception in this module — `ResourceValidationError`, `ResourceNotFoundError`, `ResourceConcurrencyError`, `ResourcesDisabledError`) matched no explicit clause in `mcp_errors.py`'s `mcp_handler` decorator and fell through to the generic `except Exception` catch-all (`mcp_errors.py:351-362`), which always returns `MCP_INTERNAL_ERROR = -32603` (`mcp_errors.py:77`).

**This was true.** `git diff d4c2366 16f86b0 -- nce/mcp_errors.py` in this worktree shows the fix landing as part of commit `16f86b0` ("feat(customer_portal): close loop from service request to support ticket via W-1 engine registry (Wave CP-1)") — a bundled, unrelated-titled commit. Before that commit, `mcp_errors.py` had no import of `ResourcesError` and no dedicated except-clause for it; any `ResourceValidationError` raised by `do_resolve_capacity` (e.g. an invalid `resource_type` filter, `capacity.py:44-46`) would indeed have fallen through to `-32603`.

**As of `b75c873` (this worktree's HEAD, which contains `16f86b0`), it is fixed:**
- `mcp_errors.py:59-66` imports `ResourcesError` from `nce.vertical_modules.resources._guard` and builds `_RESOURCES_ERRORS: tuple[type[BaseException], ...] = (ResourcesError,)`.
- `mcp_errors.py:333-338` adds `except _RESOURCES_ERRORS as e: raise McpError(MCP_INVALID_PARAMS, "Invalid parameters", data=invalid_arguments_data(e))` — positioned after `(ValueError, TypeError)` and before `A2AAuthorizationError`, which is safe because `ResourcesError` derives only from `Exception` (verified: `_guard.py:17-18`, `class ResourcesError(Exception)`) and is not itself a `ValueError`/`TypeError` subclass, so clause ordering does not shadow it.
- Net effect: **every** `ResourcesError` subclass raised inside an `@mcp_handler`-decorated Resources handler — including `resources_resolve_capacity`'s `ResourceValidationError` — now maps to **`-32602 Invalid parameters`**, not `-32603`. This is documented in the module's own error-mapping table (`mcp_errors.py:32`).

**Operational implication for anyone still relying on the old behavior:** a monitoring rule, log-alert, or client retry policy written against "`resources_resolve_capacity` refusals show up as `-32603`" is now stale — refusals surface as `-32602` with `data.reason == "invalid_arguments"` and (in dev only, `client_visible_detail()`) the original message. If your alerting still treats `-32603` from Resources as the refusal signal, it will silently stop firing on ordinary validation refusals after this fix; re-point any such rule at `-32602` and `data.reason`.

**What is unaffected by this fix:** a genuinely unexpected failure inside a Resources handler — a database connection drop, an unhandled programming error, anything that is *not* a `ResourcesError` subclass — still correctly falls through to `-32603 Internal error` via the final `except Exception` clause. The fix narrows the internal-error bucket to true internal errors; it does not (and should not) make `-32603` disappear from this module entirely.

---

## 8. RS-2 through RS-5: the hardening rules enforced in code

The spec's "review round-2 hardening" section numbers five findings; RS-1 does not appear anywhere in the shipped code (`grep -rn "RS-1" nce/vertical_modules/resources/` returns nothing) — only RS-2 through RS-5 are referenced in comments, docstrings, and error messages:

| Rule | Enforced where | What it prevents |
|---|---|---|
| **RS-2** — a van is `VEHICLE` (+ Inventory `STOCK_LOCATION`), never a customer `FUNCTIONAL_LOCATION` | `registry.py:70-73` (rejects creating a resource with kind `functional_location`/`site`/`room`/`rack`); `material_flow.py:50-54,105-108` (rejects a destination equal to the van, and requires `kind="vehicle"`); `field_schedule.py:280-326` (annotates van-stock info with an explicit RS-2 note) | A van being silently treated as a customer site, which would double-count it in both Resources' schedule and a customer's site inventory |
| **RS-3** — double-booking is a DB-constraint problem, not an app-level check | `nce/migrations/071_allocations_btree_gist.sql:22-25` (§2) | Two concurrent reservations both succeeding for an overlapping window on the same resource |
| **RS-4** — reactive cert-expiry watcher, not polling | `watcher.py:29-215` — `handle_hr_cert_change` subscribes to HR `CERTIFICATION` `UPDATED`/`EXPIRED`/`CREATED` events on `nce.events.bus` (`watcher.py:207-212`) and flags any future allocation whose window outlives the cert as `status="tentative"` with a `cert_conflict` note in `attrs` | A technician staying scheduled for a job requiring a certification that has since expired or been revoked, with nothing checking until someone happens to look |
| **RS-5** — travel booking is Contract-B autonomous spend | `travel.py:121-249` (§6) | Real money leaving the building on a single unconfirmed call, and duplicate bookings from retried requests |

### 8.1 RS-4 in detail: what the watcher actually does and doesn't do
`register_resources_event_subscribers()` (`watcher.py:202-215`) subscribes `on_hr_cert_event` to three event shapes on the shared bus. When fired, `handle_hr_cert_change` (`watcher.py:29-184`) resolves the technician's `resources` row (matched by resource id, `metadata->>'employee_id'`, or email — `watcher.py:69-82`), determines whether the cert change makes the tech invalid (`status` in `{expired, revoked, suspended, inactive}`, or `valid_to` already in the past), and for every future/overlapping non-released allocation, writes a `cert_conflict` block into `attrs` and demotes `status` to `tentative` (never all the way to `released` — a human still has to actually reassign or cancel the job). If the subscribe call itself fails (e.g. the event bus module can't be imported), the failure is caught and logged as a warning (`watcher.py:214-215`), not raised — a broken RS-4 wiring at boot degrades silently rather than blocking startup, so a "watcher registered" log line should not be assumed present without checking for it.

---

## 9. Cores with no admin surface at all

Repeating the user-guide §9 finding from the admin angle: `do_create_resource`, `do_get_resource`, `do_list_resources`, `do_update_resource` (all in `registry.py`) and `do_record_allocation_outcome` (`planner.py:302-353`) have no REST route in `admin_handlers/resources.py` and no MCP tool in `tool_registry.py`. Practically, this means **there is no admin UI or API path today to onboard a new employee/contractor/vehicle/tool as a schedulable resource, list the existing roster, or edit one** — it must be done via direct Python call, a one-off script, or (for employees/contractors) whatever seeding exists in HR/Vendors that happens to also populate `resources` as a side effect. Confirm any such side-effect seeding exists before assuming the roster is reachable at all in a given deployment.

---

## Appendix: Drift & Known Gaps

1. **Per-tenant `resources.enabled=false` disable is caller-opt-in, not dispatch-enforced** (§1.1) — no code path fetches `namespaces.metadata` and injects `namespace_metadata` before calling a Resources `do_*`; only the global `NCE_RESOURCES_ENABLED` toggle is unconditionally enforced.
2. **`resources_resolve_capacity`'s `-32603` misclassification was real, and is fixed as of this worktree's HEAD** (§7) — flagged here because the fix landed inside an unrelated-titled commit (`16f86b0`, a Customer Portal feature) and is easy to miss; any monitoring built against the old `-32603` signal needs to move to `-32602`.
3. **`do_resolve_capacity` does not yet compute real utilization** — every resource returned is unconditionally `"status": "available"`; `utilized_resources` is always empty (user guide §1). True occupancy-aware capacity resolution only exists inside `do_forecast_demand` and `do_plan_allocation`'s own conflict pre-filter, not in the capacity tool itself.
4. **`resources-travel-policy.json` declares cost caps and distance thresholds that no code path reads** (§6) — `lodging_standard_cap_nok`, `flight_economy_standard_cap_nok`, `distance_threshold_km_for_travel`, `overnight_distance_threshold_km` are dead configuration as of this audit.
5. **`resources_plan_allocation` is not flagged `mutation: true`** even though it can call `do_reserve` internally under tiered autonomy (§4) — an auto-reserving plan call will not trigger the same cache-generation bump an explicit `resources_reserve` call does.
6. **No master-data CRUD surface for resources themselves** (§9) — `do_create_resource`/`do_get_resource`/`do_list_resources`/`do_update_resource` and the outcome-feedback writer `do_record_allocation_outcome` are Python-only; there is no REST or MCP path to onboard, list, or update the resource roster, nor to feed a job-outcome rating back into the planner's history score.
7. **RS-1 is referenced nowhere in the shipped code** — only RS-2 through RS-5 appear in comments/docstrings/error messages; whatever RS-1 named in the original numbering was not carried into implementation artifacts.
