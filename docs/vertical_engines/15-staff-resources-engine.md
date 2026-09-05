> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 15 — Staff & Resources Engine  (nce/vertical_modules/resources)

**Status:** spec (Tier 3 — Operations/Delivery axis) · **Owner:** NCE core (Sindre)
**Pattern companions:** `docs/VERTICAL_MODULE_PATTERN.md`, `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§2 conventions, §4 graph catalogue, §7 spec format)

## Mission
The **capacity + scheduling brain**. It plans and allocates every *schedulable* resource — **people** (from HR), **contractors** (from Vendors), **vehicles/cars**, and **tools/equipment** — against the **Project** install pipeline and the **Support** service pipeline, over time. It also coordinates **warehouse→project material flow** (kit a project's BOM, reserve stock, schedule a van + driver to site) and plans the **travel & hospitality** that a crew needs to reach and stay at a job. It is the layer *between demand* (Project/Support want a crew at a functional location in a window) *and execution* (Field Tech runs the work order). The deep-AI angle: optimal allocation is **cognitive recall** — *"which crew/vehicle/tool-set delivered jobs like this well"* weighted by outcome (margin held, few tickets, on-time) — not a static rota. Together with **Field Tech** it forms the unified backend for a future **field webapp** (one surface: my schedule · my travel · my lodging · my work orders · my van stock).

## Inspiration & triage
- **the planning sources (concept, build-not-lift — these are 🔵/🟡 in his repo):**
  - modulkart **04 Project** — the *kapasitetsmotor* (capacity engine, listed built) + tiered automation (Tier 1 <50K autonomous → Tier 4 senior-PL).
  - modulkart **07 Technical/Installation** — AI dispatch by *skill/location/load/history*, GPS time-tracking, the shared install+service tech pool.
  - modulkart **10 Logistics** — van-restocking + materials flow to site (the warehouse→project bridge).
  - modulkart **11 External Techs** — elastic contractor capacity with restricted access.
- **No single Portal sidecar** — greenfield like Field Tech (the tenant has no resource-planning system today; allocation lives in PLs' heads). This is a build, not a lift.
- **Lysning surfaces served:** `Kalender.jsx` (the planning calendar/board), `MinManed.jsx` ("My Day"/"My Month" — a tech's personal schedule), `Mobil.jsx` (the mobile/field stub). These + Field Tech's work-order screens become the field webapp.

## Classification
**internal + AI (scheduling/optimisation).** No external system of its own; it composes over the cognitive graph and the other engines via A2A. Two **optional** outbound integrations, both config-gated and abstracted behind thin adapters (so the engine ships fully usable without them):
- **Calendar sync** (Outlook/M365 — the tenant already integrates M365) for two-way push of allocations to staff calendars.
- **Travel & hospitality booking** (`travel.py` adapter): start with *internally-planned* travel/lodging records; a later adapter can call a booking provider (flights/hotels). The data model is provider-agnostic.

## Graph contribution
Node `entity_type` prefixes: `RESOURCE_*`, plus shared spine nodes `EMPLOYEE`, `CONTRACTOR`, `FUNCTIONAL_LOCATION`, `WORK_ORDER`, `PROJECT`, `TICKET`.
- **Nodes:** `RESOURCE` (the schedulable abstraction), `VEHICLE`, `TOOL` (internal company fleet/equipment master data — see boundary), `ALLOCATION`/`RESERVATION` (a resource booked to a demand in a time window), `CAPACITY_WINDOW` (availability/unavailability), `TRAVEL_LEG` (transport to/from site), `STAY` (accommodation/hospitality), `PER_DIEM` (subsistence allowance).
- **Edges (roadmap §4 contract, our slice):**
  - `EMPLOYEE -[is_a]-> RESOURCE`, `CONTRACTOR -[is_a]-> RESOURCE`, `VEHICLE -[is_a]-> RESOURCE`, `TOOL -[is_a]-> RESOURCE` (one schedulable abstraction over four physical classes).
  - `RESOURCE -[allocated_to]-> WORK_ORDER | PROJECT | TICKET` (carries the time window + `confidence`).
  - `RESOURCE -[has]-> SKILL | CERT` (read-through from HR/Vendors — not re-owned).
  - `VEHICLE -[also_is]-> STOCK_LOCATION` (a van is one node, schedulable here *and* a stock location in Inventory).
  - `ALLOCATION -[needs]-> TRAVEL_LEG`, `ALLOCATION -[needs]-> STAY`, `ALLOCATION -[accrues]-> PER_DIEM` (travel & hospitality hang off the allocation).
  - `PROJECT -[requires]-> RESOURCE_DEMAND` (the demand the planner solves); `INVENTORY_ITEM -[kitted_for]-> PROJECT -[staged_on]-> VEHICLE` (material flow).
- **memories/ledger:** every allocation outcome (on-time? reworked? margin impact?) → `v3_cognitive_ledger`, so the planner learns which crews/kits deliver. Tag derived rows with `resources_source_id` for retirement.

## Core functions
Dual-surface `do_<action>(engine, params) -> dict`.
- `do_resolve_capacity(engine, params) -> dict` — `{window, skill?/resource_type?, location?}` → available resources + current load/utilisation. Pure-ish read (Watcher/Advisor).
- `do_plan_allocation(engine, params) -> dict` — `{demand:{skills, qty, window, functional_location, project_id|ticket_id}}` → proposed optimal allocation across people/contractors/vehicles/tools, with rationale + recall evidence. **Internal-first vs contractor** weighting from config-as-IP.
- `do_reserve(engine, params) -> dict` — book a resource to a demand window (Actor; **conflict-checked** — rejects double-booking / cert mismatch / vehicle-tool clash).
- `do_release(engine, params) -> dict` — free a reservation.
- `do_detect_conflicts(engine, params) -> dict` — scan for double-booking, over-allocation, expiring cert vs assignment window, vehicle/tool clashes (Watcher).
- `do_plan_material_flow(engine, params) -> dict` — `{project_id}` → kit the project's BOM from **Inventory**, reserve stock, schedule a `VEHICLE` + driver run to the `FUNCTIONAL_LOCATION` aligned to the install window. The warehouse→project bridge.
- `do_plan_travel(engine, params) -> dict` — `{allocation_id}` → for a crew allocated to a distant site, plan `TRAVEL_LEG`s (drive via assigned vehicle, or flight/train), `STAY` (lodging near site), and accrue `PER_DIEM` per policy. Returns the itinerary + estimated cost (→ Economy).
- `do_forecast_demand(engine, params) -> dict` — from the **Sales/System Design/Project** pipeline, forecast resource demand → capacity gaps → hire/contractor recommendation (Advisor).
- `do_field_schedule(engine, params) -> dict` — `{resource_id|employee_id, window}` → the unified per-person field view (allocations + travel + stays + linked work orders + van stock) — the **field-webapp** read model, composed with Field Tech.

## MCP tools
Registered in `nce/tool_registry.py` via `_h(...)` late-binding. AI-role tag per roadmap §2.

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `resources_resolve_capacity` | ✔ | ✘ | ✘ | Watcher/Advisor |
| `resources_plan_allocation` | ✘ | ✘ | ✘ | Advisor |
| `resources_detect_conflicts` | ✔ | ✘ | ✘ | Watcher |
| `resources_forecast_demand` | ✔ | ✘ | ✘ | Advisor |
| `resources_field_schedule` | ✔ | ✘ | ✘ | — (read model) |
| `resources_reserve` | ✘ | ✘ | ✔ | Actor (Autonomous under threshold) |
| `resources_release` | ✘ | ✘ | ✔ | Actor |
| `resources_plan_material_flow` | ✘ | ✔ | ✔ | Actor |
| `resources_plan_travel` | ✘ | ✘ | ✔ | Actor (Advisor for the plan, Actor on book) |

## REST routes
No-model path for the BFF/Lysning planning board + the field webapp (admin app, HMAC/mTLS; handlers in `nce/admin_handlers/resources.py`):
- `api_resources_resolve_capacity` (GET) — calendar/Gantt availability.
- `api_resources_plan_allocation` (POST) — allocation suggestion for a demand.
- `api_resources_reserve` / `api_resources_release` (POST) — book/free from the planning board.
- `api_resources_detect_conflicts` (GET) — conflict overlay.
- `api_resources_plan_material_flow` (POST) — staging/van-run plan.
- `api_resources_plan_travel` (POST) — travel + hospitality itinerary.
- `api_resources_field_schedule` (GET) — **the field-webapp per-tech read model** (combined with Field Tech's work-order data).

## AI features
- **Watcher:** double-booking / over-allocation, expiring-cert-vs-assignment, vehicle/tool clashes, capacity-gap (demand > supply) alerts, late-material-flow risk vs install window.
- **Advisor:** optimal allocation (skill × location/travel-distance × load × **internal-vs-contractor cost** × historical outcome), demand forecast → hire/contractor signal, material-staging plan, cheapest-viable travel/lodging.
- **Actor:** reserve/release, dispatch material run, book travel/hospitality — *with confirmation*.
- **Autonomous (gated):** auto-allocate standard/sub-threshold jobs (mirrors Project's tiered automation: small jobs auto-crewed, large jobs Advisor-only for a senior PL). Governed by `AUTONOMY_ALLOCATION_CEILING`.
- **Cognitive recall:** the planner recalls *which crew + kit + vehicle delivered similar jobs well* (outcome-weighted from the ledger) — so allocation improves over time, not a static rota. Closes the loop with Vendors contractor-performance and HR skills.
- **Enrichment triggers (event-scoped, never a sweep):** plan travel/material-flow *only when* an allocation to a remote site is created; forecast demand on pipeline change. Never recompute the whole schedule speculatively.

## A2A flows
- **Serves Project(7):** capacity for PL-assignment + crew planning against phase/task windows; consumes `RESOURCE_DEMAND` from project tasks.
- **Serves Support(10):** resource availability for service dispatch (a ticket → a crew window).
- **Serves Field Tech(12) — the tight integration:** `do_plan_allocation`/`do_reserve` produce the assignment that **becomes a Field Tech `WORK_ORDER`**; `do_field_schedule` composes Resources (schedule/travel/lodging/van stock) + Field Tech (work orders/checklists/scan/GPS) into the **single field-webapp backend**. Resources = *plan*; Field Tech = *do*.
- **Consumes:** HR(13) (people skills/capacity), Vendors(4) (contractor availability/rates/performance + restricted-access), Inventory(11) (stock + van-as-stock-location for material flow).
- **Feeds Economy(8):** allocation labor cost + travel/hospitality/per-diem cost → job true-cost & margin; feeds the **Morning-brief (#19)** capacity pulse ("over-allocated next week / 2 jobs need a contractor").

## Config keys
`NCE_RESOURCES_*` in `nce/config.py`: `NCE_RESOURCES_ENABLED`, `NCE_RESOURCES_AUTONOMY_ALLOCATION_CEILING` (auto-crew value/size gate), `NCE_RESOURCES_CALENDAR_*` (optional M365/Outlook sync), `NCE_RESOURCES_TRAVEL_PROVIDER` + `NCE_RESOURCES_TRAVEL_*` (optional booking adapter; default = internal-plan-only). Namespaces opt in via `metadata.resources.enabled = true`.
**Config-as-IP JSON (namespace-scoped business rules — not code):**
- `resources-allocation-weights.json` — the planner weights: skill-match, travel-distance, load-balancing, **internal-vs-contractor cost preference**, outcome-history weight. Each tenant tunes its own.
- `resources-travel-policy.json` — per-diem rates, lodging caps, when travel/stay is required (distance/overnight thresholds), Norwegian subsistence (diett) rules.

## Tables/migrations
**Graph + own tables** (scheduling needs fast windowed queries the graph alone serves poorly):
- `resources` (registry: `id, kind ∈ {employee,contractor,vehicle,tool}, ref_id (→ EMPLOYEE/CONTRACTOR/own), display_name, attrs jsonb`).
- `allocations` (`resource_id, demand_kind, demand_id, functional_location_id, starts_at, ends_at, status, confidence`) — indexed on `(resource_id, starts_at, ends_at)` for conflict detection.
- `travel_legs`, `stays`, `per_diems` (linked to `allocation_id`).
- All `ENABLE` + `FORCE ROW LEVEL SECURITY` + `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`; **contractor sub-scope** redaction so external resources never see margin/price (cross-ref Vendors(4) restricted-access model). Mirror DDL into `schema.sql` + numbered migration.

## Dependencies
- **Upstream / master data:** **HR(13)** (people, skills, certs, capacity), **Vendors(4)** (contractors + restricted access), **Inventory(11)** (stock + van-as-stock-location).
- **Demand:** **Project(7)** (install pipeline + tasks), **Support(10)** (service dispatch), and the **Sales/System Design** pipeline (for demand forecast).
- **Execution partner:** **Field Tech(12)** — shared field-webapp backend; allocations become work orders.
- **Cost sink:** **Economy(8)** (labor + travel/hospitality cost → margin).
- **No external blockers** (greenfield). Optional: M365 calendar + a travel-booking provider (both config-gated, abstracted).

## Boundary (avoid duplication — the "thin engine" discipline)
| Concern | Owned by | Resources(15) role |
|---|---|---|
| Who a person *is* (skills/certs/employment/coaching) | **HR(13)** | schedules their *availability/allocation* |
| Contractor identity/rates/performance/restricted access | **Vendors(4)** | schedules them as elastic capacity |
| *Executing* the job (checklists, S/N scan, GPS, photos) | **Field Tech(12)** | *plans* the allocation that becomes the work order |
| Physical stock + goods-receipt + van-as-stock-location | **Inventory(11)** | schedules the van + coordinates kit/stage timing |
| Project phases/tasks/baseline + capacity *need* | **Project(7)** | owns cross-project capacity *supply* + allocation |
| Customer-installed AV equipment (lifecycle/telemetry/SLA) | **Assets(9)** | **internal** vehicles/tools live *here*, not in Assets |

## Review round-2 hardening (2026-06-17 — these govern the build)
> **The boundary table above is the Contract-A exemplar** (roadmap §9.1): per-concern ownership with Resources' *narrower* role stated explicitly ("HR owns who-they-are; Resources schedules availability"). It's the template other specs copy.

1. **Resources is the CAPSTONE consumer — the most grace-degradation-exposed engine, and arguably mis-tiered.** It reads **five** engines + produces Field-Tech work orders: HR (skills/capacity), Vendors (contractors), Inventory (stock/van), Project + Support (demand). So its **AI planner (B3) can't function until HR exists — but HR is Tier 4 and Resources is Tier 3** (the Inventory Tier-inversion, but *more acute* — 5 inputs). State it starkly: **B1 (registry + capacity) works on manual data; the planner (B3) needs HR + Vendors + the ledger; material flow (B4) needs Inventory.** Sequence the planner *after* its hard inputs, or it ships inert.
2. **The van is a TRIPLE-ROLE node — name the owner (roadmap §9.1).** A van is `VEHICLE` (**Resources** owns it + schedules it) **+** `STOCK_LOCATION` (**Inventory** references + stocks it) — and it is **NEVER a customer `FUNCTIONAL_LOCATION`** (the Inventory-spec conflation, now fixed). Resources owns the `VEHICLE`/`RESOURCE` identity; Inventory references it as a stock-location; neither is a customer-site node.
3. **Allocation conflict-detection is a concurrency/atomicity problem — solve it in the DB, not the app (roadmap §2.11).** `do_reserve` "rejects double-booking" is an app-level check that **loses the race** when two planners (or planner + auto-allocate) reserve the same resource-window concurrently. Use a **Postgres exclusion constraint** (`EXCLUDE USING gist` on a `tstzrange` per `resource_id`) so double-booking is **impossible at the DB**. This pairs with Inventory's `UPDATE…WHERE qty>=n` into one discipline: **stock and schedule conflicts live in DB constraints, never app code.**
4. **Resources needs the §9.6 reactive-event mechanism more than any engine.** A **cert expiring in HR** *after* Resources reserved a future window **silently invalidates** the allocation; the "expiring-cert-vs-assignment" Watcher must **react to HR's cert change**, not poll. As the heaviest consumer of other engines' state changes, Resources is the strongest argument for building the reactive graph-event bus (§9.6 item 5).
5. **Travel-booking is autonomous SPEND (→ Contract B); per-diem (diett) is NO-jurisdiction tax compliance (→ config, like NGAAP).** `do_plan_travel` books lodging/flights = **real money out** → route through the Contract-B gate (§9.5: idempotency + ceiling + confirm). And **diett is jurisdiction-specific *tax* logic** (taxable vs non-taxable thresholds, overnight rules), **not just rates** — same trap as "swap the chart-of-accounts": scope it explicitly as **Norwegian-diett rules in `resources-travel-policy.json`**, multi-jurisdiction as future work.

## Build phases
- **B1 — Registry + capacity:** `resources` package + `RESOURCE` abstraction over employee/contractor/vehicle/tool; `resources` table; `do_resolve_capacity` + capacity calendar. MCP tools + REST for the calendar.
- **B2 — Allocation + conflicts:** `allocations` table (RLS + contractor sub-scope); `do_reserve`/`do_release`/`do_detect_conflicts`. Planning-board REST surface.
- **B3 — AI planner:** `do_plan_allocation` (cognitive recall + `resources-allocation-weights.json`); tiered autonomy (`AUTONOMY_ALLOCATION_CEILING`); ledger outcome feedback.
- **B4 — Material flow:** `do_plan_material_flow` (Inventory kit/reserve + van + Field Tech run); van-as-stock-location wiring.
- **B5 — Travel & hospitality:** `TRAVEL_LEG`/`STAY`/`PER_DIEM` + `do_plan_travel` + `resources-travel-policy.json`; Economy cost feed.
- **B6 — Field-webapp backend:** `do_field_schedule` composing Resources + Field Tech into one per-tech read model; optional M365 calendar sync. (The field webapp itself is a later front-end build on this backend.)
- **B7 — Demand forecast:** `do_forecast_demand` from the pipeline → capacity-gap/hire signal; Morning-brief capacity pulse.
