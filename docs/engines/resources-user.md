> **Status:** shipped · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

# Resources Engine User Guide

> **Status:** shipped · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

The **Resources Engine** (`nce/vertical_modules/resources/`, internally Module 15) is the capacity + scheduling brain of the Neuro-Cognitive Engine. It answers one question for every other engine: *who or what can do this job, and when are they free?* "Resource" here is a single schedulable abstraction over four physical kinds (`nce/vertical_modules/resources/registry.py:34`, `VALID_RESOURCE_KINDS`):

- **`employee`** — internal staff (technicians, PLs).
- **`contractor`** — external labor, sub-scoped so it never sees margin/cost data (§4).
- **`vehicle`** — company vans and cars. A van is *also* an Inventory `STOCK_LOCATION`, but never a customer functional location — this rule (RS-2) is enforced in three separate places in the code (registry, field schedule, material flow) and is covered in §5.
- **`tool`** — equipment/tool-kit assets.

Every one of these is one row in the `resources` table (`kind`, `ref_id`, `display_name`, `attrs`), and every booking of one of them to a job is one row in `allocations` (`resource_id`, `demand_kind`, `demand_id`, `starts_at`, `ends_at`, `status`). This guide covers the 9 registered MCP tools this engine exposes; the admin guide (`docs/engines/resources-admin.md`) covers enablement, the database guarantee behind "no double-booking," REST routes, and a drift finding worth knowing about if you build against the raw JSON-RPC error codes.

> [!IMPORTANT]
> **Surface summary:** **9 MCP tools** registered in `nce/tool_registry.py:1253-1293` (and re-declared for the stdio JSON-RPC schema catalog in `nce/mcp_stdio_tools.py:4905-5082`) and **10 mounted REST routes** in `nce/admin_handlers/resources.py`. One REST route — `GET /api/resources/pulse` — has **no** corresponding MCP tool (§7). Four internal `do_*` cores in `registry.py` (create/get/list/update a resource) and one in `planner.py` (`do_record_allocation_outcome`) have **neither** an MCP tool **nor** a REST route (§8) — master-data CRUD for resources and the cognitive-ledger feedback write are Python-only today.

---

## 1. `resources_resolve_capacity` — capacity calendar

Registered `nce/tool_registry.py:1253-1256` (`cacheable: true`, mutation: false). Handler: `handle_resources_resolve_capacity` (`mcp_handlers.py:52-61`) → core `do_resolve_capacity` (`capacity.py:23-91`).

**What it does today:** given a `namespace_id` and a `window` (`{"starts_at", "ends_at"}`, required), optionally filtered by `kind`/`resource_type` or `skill`/`location`, it lists every resource of that kind in the tenant and returns them under `available_resources`. **Read this literally** — `capacity.py:73-91` marks every returned resource `"status": "available"` unconditionally, and `utilized_resources` is always `[]` with `utilization_pct: 0.0`. The function does not yet cross-reference the `allocations` table to compute real occupancy for this call (that logic lives instead in `do_forecast_demand`, §5, and `do_plan_allocation`, §2, which both *do* query allocations). Use this tool for the resource roster, not yet for a true per-slot free/busy calendar.

**Arguments:** `namespace_id` (str, UUID, required), `window` (dict, required), `kind`/`resource_type` (str, optional — must be one of the four kinds above or the call raises `ResourceValidationError`), `skill` / `location` (optional, accepted but not yet applied as filters in `capacity.py`).

---

## 2. `resources_plan_allocation` — the AI allocation advisor

Registered `nce/tool_registry.py:1257-1260` (not cacheable, not a mutation *unless* it auto-reserves — see below). Handler: `handle_resources_plan_allocation` (`mcp_handlers.py:64-73`) → core `do_plan_allocation` (`planner.py:57-299`).

This is the planner: given a demand window and required skills/kinds, it scores every *conflict-free* candidate resource on five weighted components and returns the winner per required kind.

### 2.1 Scoring table
Weights load from `resources-allocation-weights.json` (`planner.py:29`, per-tenant config-as-IP, defaults at `planner.py:48-54`):

| Component | Default weight | Scoring rule (`planner.py:147-221`) |
|---|:---:|---|
| **Skill match** | `0.35` | Fraction of `required_skills` present in the candidate's `attrs.skills`; `1.0` if no skills required. |
| **Travel distance** | `0.20` | `1.0` if candidate's `base_location` matches/contains the demand `location`; `0.8` if either side is unset; `0.5` on an explicit location mismatch. |
| **Load balance** | `0.15` | `max(0.1, 1.0 - open_alloc_count * 0.1)` — counts the candidate's *own* allocations within ±7 days of the demand window; heavier near-term load lowers the score, floored at `0.1`. |
| **Internal preference** | `0.15` | `1.0` for `employee`/`vehicle`/`tool` kinds, `0.6` for `contractor` — a built-in internal-first bias. |
| **Outcome history** | `0.15` | Reads `v3_cognitive_ledger` for past `resource_allocation_outcome` events keyed by the candidate's id; averages `rating`/5 and `quality_score`, or `0.8` neutral if the candidate has no history. |

Composite score = weighted sum, ties broken by insertion order. Candidates with any conflicting allocation in the requested window (checked per-candidate via a live `tstzrange` overlap query, `planner.py:114-131`) are excluded before scoring — a resource with a clash never wins, but this is a pre-filter, not itself the concurrency guarantee (that's the DB constraint, admin guide §2).

### 2.2 Tiered autonomy
`planner.py:246-299`. The gate is `cfg.NCE_RESOURCES_AUTONOMY_ALLOCATION_CEILING` (default `50000.0` NOK, `nce/config.py:1335-1337`):
- If `estimated_value_nok <= ceiling` **and** the caller passes `auto_reserve=true` **and** every required kind found a candidate: the plan is committed immediately — the engine calls `do_reserve` (§3) for every winner and returns `"status": "reserved", "autonomous": true`.
- Otherwise (over-ceiling, or `auto_reserve` not set, or a required kind has zero eligible candidates): returns `"status": "suggested", "requires_approval": true` with the same scored plan — a human must call `resources_reserve` explicitly.

**Arguments:** `namespace_id`, `demand_kind` (default `"project"`), `demand_id` (optional UUID), `starts_at`/`ends_at` (required, `ends_at` must be after `starts_at`), `required_skills` (list, optional), `required_kinds` (list, default `["employee"]`), `location` (optional), `estimated_value_nok` (float, default `0.0`), `auto_reserve` (bool, default `false`), `tenant_weights` (dict, optional — overrides individual weight keys per call).

---

## 3. `resources_reserve` / `resources_release` — book and free a window

Both are mutations (`tool_registry.py:1273-1282`), not cacheable. Handlers: `handle_resources_reserve`/`handle_resources_release` (`mcp_handlers.py:112-133`) → cores `do_reserve`/`do_release` (`allocations.py:79-202`, `205-253`).

`resources_reserve` inserts a row into `allocations` (`namespace_id, resource_id, demand_kind, demand_id, functional_location_id, starts_at, ends_at, status, confidence, attrs`) after checking the resource exists in the tenant's scope. **The no-double-booking guarantee is not application logic** — it is a PostgreSQL `EXCLUDE USING gist` constraint at the database layer; see the admin guide §2 for the full mechanism and its positive-control test. From this tool's point of view, all you need to know is: if your window collides with an existing non-`released` allocation on the same resource, the insert is rejected and `do_reserve` translates the Postgres `23P01` exclusion violation into a domain `ResourceConcurrencyError` (`allocations.py:159-171`), which the MCP layer reports back distinctly from a plain validation error (see admin guide §6 for exactly which JSON-RPC code that becomes).

`resources_release` sets `status = 'released'` on an existing allocation by id, which also *lifts* the exclusion (the constraint is scoped `WHERE (status <> 'released')`, migration 071) — a released window becomes bookable again immediately.

**`resources_reserve` arguments:** `namespace_id`, `resource_id`, `demand_kind` (required, free text — e.g. `project`, `work_order`, `service`), `starts_at`/`ends_at` (required), `demand_id` / `functional_location_id` (optional UUIDs), `status` (one of `tentative|reserved|confirmed|released`, default `reserved`), `confidence` (0.0–1.0, default `1.0`), `attrs` (dict, optional), `contractor_view` (bool — forces the contractor redaction described in §4 even if the resource itself isn't a contractor).

**`resources_release` arguments:** `namespace_id`, `allocation_id`.

---

## 4. `resources_detect_conflicts` — overlap scan

Registered `nce/tool_registry.py:1261-1264` (`cacheable: true`). Handler `handle_resources_detect_conflicts` → core `do_detect_conflicts` (`allocations.py:256-327`). Self-joins `allocations` on matching `resource_id` and overlapping `tstzrange(starts_at, ends_at)`, excluding released rows, optionally scoped to one `resource_id` or one time window. Returns a `conflicts` list of paired allocation summaries plus `total_conflicts`. Because the DB constraint (§3 / admin §2) already prevents new double-bookings from being *written*, in normal operation this tool finds zero results for anything created after migration 071 landed — its practical use is auditing rows that predate the constraint, or diagnosing during the admin positive-control test when the constraint is deliberately dropped.

**Arguments:** `namespace_id` (required), `resource_id` (optional), `starts_at`/`ends_at` (optional pair — both required together to scope by window).

### Contractor allow-list redaction
Every allocation record returned by `resources_reserve`, `resources_release`, and embedded in `resources_field_schedule` is passed through `redact_contractor_view()` (`allocations.py:72-76`) whenever the underlying resource's `kind == "contractor"`. The redaction is an **allow-list**, not a deny-list — `CONTRACTOR_ALLOWED_ALLOCATION_FIELDS` (`allocations.py:37-52`) names exactly 12 fields (`id`, `namespace_id`, `resource_id`, `demand_kind`, `demand_id`, `functional_location_id`, `starts_at`, `ends_at`, `status`, `confidence`, `created_at`, `updated_at`) and everything else — cost, rate, pricing, internal notes — is dropped, with a `"redaction": "contractor_allow_list_enforced"` marker added so a caller can tell a redacted record from a genuinely-empty one.

---

## 5. `resources_plan_material_flow` — warehouse → van → site

Registered `nce/tool_registry.py:1283-1288` — the **only** admin-only tool of the 9 (`admin_only: true`, plus `mutation: true`). Handler `handle_resources_plan_material_flow` → core `do_plan_material_flow` (`material_flow.py:28-191`).

Plans the physical path materials take from warehouse to job site through a van: `pick_and_kit` (24h before install) → `van_loading` (2h before) → `transit_and_delivery` (1h before, arriving at `install_time`). It looks up a matching Inventory `stock_locations` row for the van (by `vehicle_ref` or name) to attach a `van_stock_location_id`, and if `auto_reserve_van=true` it calls `do_reserve` internally to book the vehicle for the transit window (departure through arrival + 4h buffer).

**RS-2 hardening, enforced here explicitly:** the van resource must have `kind == "vehicle"` (`material_flow.py:105-108`, raises `ResourceValidationError` otherwise), and the destination cannot itself be the van (`material_flow.py:50-54`) — a van is *never* a customer functional location, only a vehicle plus (in Inventory) a stock location.

**Arguments:** `namespace_id`, `van_resource_id` (required, must be `kind=vehicle`), `target_date`/`starts_at` (required — the install time), `demand_kind` (default `"project"`), `demand_id`/`project_id`/`work_order_id` (optional), `destination_location_id`/`functional_location_id` (optional), `items` (list of `{sku|item_id, quantity, description}`), `auto_reserve_van` (bool, default `false`).

---

## 6. `resources_plan_travel` — travel, lodging, and Norwegian per-diem (diett)

Registered `nce/tool_registry.py:1289-1292` (mutation, not cacheable). Handler `handle_resources_plan_travel` → core `do_plan_travel` (`travel.py:118-368`).

Takes an existing `allocation_id` and an `itinerary` (`travel_legs`, `stays`, `per_diems`) and either **plans** (estimates cost, no persistence, `action="plan"`) or **books** (`action="book"`, persists to `travel_legs`/`stays`/`per_diems` and returns an `economy_feed` block for the Economy engine).

### 6.1 Norwegian statutory per-diem (diett)
`calculate_norwegian_diett()` (`travel.py:63-115`) implements Norwegian subsistence tax rules (Statens satser / Skatteetaten) from `resources-travel-policy.json` (`travel.py:31`, fallback rates baked in at `travel.py:43-60` if the file is missing):

| `diet_type` | Base rate (2026, NOK) |
|---|---:|
| `day_trip_short_6_to_12h` | 360.00 |
| `day_trip_long_over_12h` | 680.00 |
| `overnight_hotel` | 940.00 |
| `overnight_unspecified` | 450.00 |

Meal deductions are applied on top when the employer already provided a meal: breakfast −20%, lunch −30%, dinner −50% of the base rate, capped so total deduction never exceeds 100%. This is jurisdiction-specific tax logic, not just a rate table — the policy file is explicitly Norway-only (`policy["jurisdiction"] == "NO"`); a different country's rules would need its own policy file and its own calculation function, not a config swap.

### 6.2 The Contract-B spend gate (booking real money)
Booking travel is treated as autonomous spend, not a plain write. `do_plan_travel` on `action="book"` (`travel.py:234-249`) requires:
1. **`idempotency_key`** — mandatory; its absence raises `ResourceValidationError` with the message *"Contract-B Spend Gate Refusal: idempotency_key is required when booking travel (RS-5)."* A duplicate call with the same key returns the existing booking unchanged (`travel.py:250-273`) rather than double-booking travel.
2. **Ceiling check** — if the itinerary's total cost exceeds `spend_ceiling_nok` (falls back to `cfg.NCE_RESOURCES_TRAVEL_MAX_AUTO_SPEND`, default `10000.0` NOK) and the caller did not pass `confirm=true`, the call is refused outright with a "Contract-B Spend Gate Refusal" `ResourceValidationError` — no partial booking occurs.

**Arguments (both actions):** `namespace_id`, `allocation_id` (required, must already exist), `action` (`plan`|`book`, default `plan`), `itinerary` (`{travel_legs: [...], stays: [...], per_diems: [...]}`). **Book-only:** `idempotency_key` (required), `spend_ceiling_nok` (optional override), `confirm` (bool, required if over ceiling).

---

## 7. `resources_forecast_demand` and the Morning Brief pulse

`resources_forecast_demand` (`tool_registry.py:1265-1268`, cacheable) → `do_forecast_demand` (`forecast.py:27-196`) projects available capacity hours (active resources × `capacity_pct` × working days in the horizon) against committed allocation hours plus pipeline demand, and returns a `status` of `deficit`/`surplus`/`balanced` with a hire/contractor-surge or pull-forward recommendation. Horizon defaults to 30 days (max 365).

A second function in the same module, `get_morning_brief_capacity_pulse` (`forecast.py:199-255`), computes a same-day utilization snapshot (`saturated` > 95%, `underutilized` < 40%, else `optimal`) for the cross-engine Morning Brief digest. **This function has no MCP tool** — it is reachable only via the REST route `GET /api/resources/pulse` (`admin_handlers/resources.py:408-427`, `admin_app.py:1273-1276`). If you need the daily capacity health pulse from an MCP client, there is currently no tool for it; use the REST endpoint or call the Python function directly.

---

## 8. `resources_field_schedule` — the field-webapp read model

`tool_registry.py:1269-1272` (cacheable) → `do_field_schedule` (`field_schedule.py:34-443`). Composes, for one technician (`resource_id`) over an optional window, a single mobile-friendly payload: the technician's own profile, their allocations, every linked `travel_legs`/`stays`/`per_diems` row, matched Field Tech (Module 12) work orders and checklists, their assigned vehicle and its RS-2 van-stock-location, and M365 calendar-sync status (off by default, `cfg.NCE_RESOURCES_CALENDAR_SYNC_ENABLED`). Contractor resources get the same allow-list redaction as §4 applied to every schedule item and to cost fields on travel legs/stays/per-diems (`field_schedule.py:145-146, 175-176, 203-204, 360-362`).

---

## 9. Cores with no MCP tool and no REST route

`registry.py` also defines full CRUD for the resource master record itself, and `planner.py` defines the outcome-feedback writer — none of these five functions are wired to *any* external surface (MCP or REST) as of this audit:

| Function | File:line | Purpose |
|---|---|---|
| `do_create_resource` | `registry.py:54-118` | Register a new schedulable resource (kind/display_name/ref_id/attrs). Rejects `functional_location`/`site`/`room`/`rack` kinds outright (RS-2). |
| `do_get_resource` | `registry.py:121-159` | Fetch one resource by id, tenant-scoped. |
| `do_list_resources` | `registry.py:162-242` | Paginated listing, optional kind filter. |
| `do_update_resource` | `registry.py:245-294` | Merge-update `display_name`/`attrs`. |
| `do_record_allocation_outcome` | `planner.py:302-353` | Appends a `resource_allocation_outcome` row to `v3_cognitive_ledger` — the feedback the planner's outcome-history score (§2.1) reads back. |

If your integration needs to register a new technician/van/tool, or feed back a job-outcome rating, you must call these Python functions directly today — there is no HTTP or MCP path.

---

## Worked example: plan and reserve a crew, then book travel

```python
from nce.vertical_modules.resources.planner import do_plan_allocation
from nce.vertical_modules.resources.travel import do_plan_travel

# 1. Ask the planner for a technician for a 4-hour install window.
plan = await do_plan_allocation(engine, {
    "namespace_id": ns_id,
    "demand_kind": "project",
    "demand_id": project_id,
    "starts_at": "2026-09-10T09:00:00Z",
    "ends_at": "2026-09-10T13:00:00Z",
    "required_skills": ["av_install"],
    "required_kinds": ["employee"],
    "location": "bergen",
    "estimated_value_nok": 12000.0,
    "auto_reserve": True,   # sub-threshold (< 50,000 NOK ceiling) -> auto-books
})
allocation_id = plan["allocations"][0]["id"]

# 2. Book travel behind the Contract-B spend gate.
booking = await do_plan_travel(engine, {
    "namespace_id": ns_id,
    "allocation_id": allocation_id,
    "action": "book",
    "idempotency_key": "trip-2026-09-10-bergen-001",
    "itinerary": {
        "travel_legs": [{"origin": "HQ", "destination": "Bergen", "departure_at": "2026-09-10T06:00:00Z", "cost_nok": 1200.0}],
        "per_diems": [{"date": "2026-09-10", "diet_type": "day_trip_long_over_12h", "meals_provided": {"lunch": True}}],
    },
})
# booking["total_cost_nok"] < 10,000 NOK default ceiling -> books without confirm=True
```
