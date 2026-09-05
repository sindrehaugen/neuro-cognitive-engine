> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 12 — Field Tech Engine  (nce/vertical_modules/field_tech)

**Status:** spec (Tier 3 — Delivery/Operations axis) · **Owner:** NCE core (Sindre)
**Pattern companions:** `docs/VERTICAL_MODULE_PATTERN.md`, `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§2 conventions + AI-roles, §4 graph catalogue, §7 spec format), `docs/vertical_engines/04-vendors-engine.md` (the Partner Access Model this engine enforces on the mobile/WO surface)

## Mission
Be **"the hands"** of the spine — the engine that turns a frozen BOM (install) or a support ticket (service dispatch) into physical work executed by techs in the field, and turns that field work back into structured graph signal. It manages a shared pool of ~7 install + ~5 service technicians plus 5–15 external contractors, dispatching **WORK_ORDERs** to the right person, capturing **checklist-driven quality** (→ ISO9001 verification records), **serial-number scans** (the seed for the Assets register — scan S/N at install → `BOM_LINE -[installed_as]-> ASSET`), **photo documentation**, and **GPS auto-time-tracking**. It is **mobile-first and offline-capable**: the backend is the engine, a field app consumes its REST surface and syncs an offline queue when connectivity returns. The deep-AI angle: **AI dispatch as cognitive recall** — match a tech to a job by skill/certification, location, current load, and *who did similar jobs well* (read from the ledger, not a static roster), and enforce the **Partner Access Model** structurally so an external contractor sees only their own work orders and the relevant BOM lines — never margin, price, or strategy.

## Inspiration & triage
- **the planning sources (module map `04-virksomhets-modulkart.md`):**
  - **Module 07 — Technical / Installation:** «hendene» — Work order, sjekklister, serienummer-scan, timeføring, foto-dokumentasjon, dispatch; mobil-først offline-app; AI-dispatch (skill/lokasjon/last/historikk); GPS-auto-timeføring; S/N-scan as the seed for asset-lifecycle. The ~7 install + ~5 service shared-pool model.
  - **Module 11 — External Techs:** the elastic 5–15 freelancer capacity doing the *same physical job* as internals, under **strictly restricted data access** ("ser egne work orders + relevante BOM-linjer, ALDRI margin/pris/strategi"). The Partner Access Model designed in the field-service skill.
- **Honest code status — greenfield.** the reference implementation has **NO field-service code**: `lib/field-service/` does not exist. Module 07 is "delvis" only in that a read-only Dynamics work-order sync exists and checklists/dispatch were *designed on paper*; the mobile app was never built. Module 11 is "ikke-startet". This engine is a **build**, richly researched but with no pure functions to lift 1:1 (contrast Procurement, which lifts TCO/scoring verbatim). The runnable spec is the checklist/dispatch design notes + the Partner Access Model from the field-service skill.
- **Portal sidecars to lift:** none directly. Contractor master-data + matching come from **Vendors(4)** (`do_match_contractor`, `contractor_profiles`); skills/certs for internals from **HR(13)**.
- **Lysning page(s) served:** the (new) field/dispatch surface + the restricted external-contractor mobile view; install/service status feeds the room-centric customer view.

## Classification
**mobile + push (internal + offline-sync).** No third-party external system of its own and no OAuth — the "client" is the **field app**, which talks to the engine over the REST surface. The essential divergence from the pattern doc is the **offline-sync transport**: the app queues mutations (checklist ticks, S/N scans, photo refs, time entries) locally and replays them; the backend must **reconcile** an out-of-order, possibly-duplicated batch idempotently (every queued op carries a client-generated `op_id` + `work_order_id` + monotonic device clock → dedup on `op_id`, last-writer-wins per field with conflict surfacing). **Push** the other direction (assignment, reschedule, SLA-risk nudge) via the platform push channel. Semantic track: photo captions / completion notes / service findings → `memories` for cognitive recall ("what did we find last time at this room"). Resilience: standard `httpx.AsyncClient` only for outbound push/A2A; the sync endpoint is internal.

## Graph contribution
Node `entity_type` prefixes: `FIELD_TECH_*` for engine-owned nodes; shared spine nodes `WORK_ORDER`, `EMPLOYEE`, plus references to `CONTRACTOR`, `PROJECT`, `TICKET`, `BOM_LINE`, `ASSET`, `FUNCTIONAL_LOCATION`/`ROOM`, `CERT`/`SKILL`.
- **Nodes:** `WORK_ORDER` (the unit of field work — install or service), `FIELD_TECH_CHECKLIST` (a checklist instance bound to a WO; its ticked items are the ISO9001 verification record), `FIELD_TECH_TIME_ENTRY` (a GPS-derived or manual labor span), `FIELD_TECH_SCAN` (an S/N scan event — the asset seed), `FIELD_TECH_PHOTO` (a documentation reference, blob in object store).
- **Edges (the §4 contract, our slice):**
  - `WORK_ORDER -[assigned_to]-> EMPLOYEE` **or** `WORK_ORDER -[assigned_to]-> CONTRACTOR` (the assignment; EMPLOYEE from HR, CONTRACTOR endpoint owned by Vendors).
  - `WORK_ORDER -[for]-> PROJECT` (install, from a frozen BOM) **or** `WORK_ORDER -[for]-> TICKET` (service dispatch, from Support).
  - `WORK_ORDER -[at]-> FUNCTIONAL_LOCATION`/`ROOM` (the physical site — the functional-location principle shared with System Design/Assets).
  - `WORK_ORDER -[installs]-> BOM_LINE` and, on S/N scan, `BOM_LINE -[installed_as]-> ASSET` — **the boundary edge handed to Assets(9)** (Field Tech writes the scan + the seed edge; Assets owns lifecycle thereafter).
  - `EMPLOYEE -[has]-> CERT`/`SKILL` — **read from HR(13)** for dispatch eligibility (mirrors `CONTRACTOR -[has]-> VENDORS_CERT` in Vendors).
- **memories/ledger:** completion notes / service findings / photo captions → `memories` (embedding + `content_fts`) for "similar job" recall. Every dispatch decision + every WO outcome/rating → `v3_cognitive_ledger` — this is the source the dispatch advisor and the Vendors/HR performance reducers read. Tag every derived row with `field_tech_source_id` for hard-retirement on WO delete (D365 retirement pattern).

## Core functions
Pure-ish `do_<action>(engine, params) -> dict`; the dispatch ranking core is **pure** over profiles + ledger, the sync/CRUD paths are thin DB.
- `do_create_work_order(engine, params) -> dict` — `{source: project|ticket, ref_id, location, bom_lines[]?, kind: install|service}` → a WO node + edges (`for`, `at`, `installs`). Actor. The A2A entry Project (install) and Support (service dispatch) call.
- `do_dispatch(engine, params) -> dict` — `{work_order_id}` → ranked candidate techs (internal EMPLOYEE via HR skills + external CONTRACTOR via `vendors_match_contractor`) by **skill/cert × location × current-load × outcome-history**, with plain-language rationale. Pure over config + ledger; Advisor. Weights from `dispatch-weights.json`.
- `do_assign(engine, params) -> dict` — `{work_order_id, assignee_id, assignee_kind}` → writes `assigned_to`, pushes to the assignee's device. Actor (Autonomous under threshold — see AI features). Validates cert/skill eligibility before write (rejects a cert-mismatch assignment).
- `do_complete_checklist(engine, params) -> dict` — `{work_order_id, checklist_id, items[]}` → records ticked items as the ISO9001 verification record; flags missing-required items. Actor.
- `do_scan_serial(engine, params) -> dict` — `{work_order_id, bom_line_id, serial}` → `FIELD_TECH_SCAN` + the `BOM_LINE -[installed_as]-> ASSET` seed edge; hands off to Assets via A2A. Actor.
- `do_log_time(engine, params) -> dict` — `{work_order_id, start, end | gps_track}` → a `FIELD_TECH_TIME_ENTRY`; can derive span from GPS geofence enter/exit (auto-timesheet). Actor (Autonomous from GPS).
- `do_attach_photo(engine, params) -> dict` — `{work_order_id, blob_ref, caption?}` → photo node; caption → `memories`.
- `do_sync(engine, params) -> dict` — **the offline reconcile endpoint.** `{device_id, ops[]}` (each op: `op_id`, `work_order_id`, type, payload, device_clock) → idempotent replay (dedup on `op_id`), per-field last-writer-wins, returns applied/conflicted op ids + server state delta. The hard core of the engine.
- `do_partner_view(engine, params) -> dict` — **delegates to Vendors `do_partner_view`** for the redacted projection, then layers the WO mobile fields (checklist, location, assigned BOM lines) — partner-safe only. The single surface external contractors hit; see Partner Access.
- `do_record_outcome(engine, params) -> dict` — appends WO completion quality/rating to the ledger; the event source Vendors(4) `do_compute_performance` and HR(13) utilization consume.

## Partner Access (enforces the Vendors Partner Access Model on the WO surface)
External contractors hit the field app like internal techs, but see **ONLY their own work orders + the relevant BOM lines** and **NEVER** margin/price/strategy/pipeline. Field Tech is the WO-surface enforcement leg of the three-layer model defined in Vendors(4):
1. **RLS sub-scope.** `work_orders`, `checklists`, `time_entries` carry the assignee's `partner_scope_id`; `FORCE ROW LEVEL SECURITY` + a `partner_isolation_policy USING (namespace_id = get_nce_namespace() AND partner_scope_id = get_nce_partner_scope())` means a contractor's connection physically cannot read WOs that aren't theirs — a bug in app code cannot widen the set.
2. **A2A tool-scoping.** A contractor-facing agent is bound **only** the partner-safe tools — `field_tech_partner_view`, `field_tech_complete_checklist`/`scan_serial`/`log_time`/`attach_photo` scoped to their own WO, and `vendors_partner_view`. The dispatch/assign/cost tools and every `procurement_*`/economy/sales tool are **never registered** into the partner agent's surface.
3. **Field redaction.** `do_partner_view` reuses the Vendors **allow-list** projection (`partner-redaction.json`) — only explicitly partner-safe WO/BOM-line fields serialize; cost/margin/pipeline/strategy fields are dropped before serialization. Allow-list, not deny-list.

## MCP tools
Registered in `nce/tool_registry.py` via `_h(...)` late-binding. AI-role tag per roadmap §2 taxonomy.

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `field_tech_dispatch` | ✔ | ✘ | ✘ | Advisor |
| `field_tech_partner_view` | ✔ | ✘ | ✘ | Advisor (partner-scoped) |
| `field_tech_create_work_order` | ✘ | ✔ | ✔ | Actor |
| `field_tech_assign` | ✘ | ✔ | ✔ | Actor (Autonomous under threshold) |
| `field_tech_complete_checklist` | ✘ | ✘ | ✔ | Actor |
| `field_tech_scan_serial` | ✘ | ✘ | ✔ | Actor |
| `field_tech_log_time` | ✘ | ✘ | ✔ | Actor (Autonomous from GPS) |
| `field_tech_attach_photo` | ✘ | ✘ | ✔ | — (capture) |
| `field_tech_sync` | ✘ | ✘ | ✔ | — (offline reconcile) |
| `field_tech_record_outcome` | ✘ | ✔ | ✔ | — (ledger append) |

> `field_tech_partner_view` + the own-WO capture tools (`complete_checklist`/`scan_serial`/`log_time`/`attach_photo`) are the only tools bound into a contractor-facing agent profile. `dispatch`/`assign`/`create_work_order` are operator/internal — never registered into a partner agent's surface (Partner Access layer 2).

## REST routes
No-model path for the **field app**, the BFF, cron, scripts. Mounted via `build_app(extra_routes=...)`; HMAC/mTLS-authed in `nce/admin_handlers/field_tech.py`:
- `api_field_tech_dispatch` (POST) — ranked candidate techs for a WO (dispatch board).
- `api_field_tech_create_work_order` (POST) — WO from project/ticket.
- `api_field_tech_assign` (POST) — assign + push.
- `api_field_tech_work_order` (GET) — full WO detail for the app (checklist, BOM lines, location).
- `api_field_tech_complete_checklist` / `api_field_tech_scan_serial` / `api_field_tech_log_time` / `api_field_tech_attach_photo` (POST) — the per-capture app calls.
- `api_field_tech_sync` (POST) — **the offline-queue reconcile endpoint** (the app's primary write path).
- `api_field_tech_partner_view` (GET) — **partner-scoped, field-redacted** WO + BOM-line projection (external-contractor app surface; redaction layer 3).

## AI features
- **Watcher:** **SLA/deadline-risk** detection (WO due-date vs current progress/load → alert before breach, shared signal with Support); **missing checklist items** before a WO can close (quality gate → ISO9001); **cert/skill mismatch** for a proposed or standing assignment (block dispatch eligibility, shared cert-expiry signal with Vendors/HR).
- **Advisor:** **dispatch optimization** — best tech for the job (skill/cert × location × load × outcome-history) with rationale; **route** suggestion across a tech's day's WOs.
- **Actor (with confirmation):** `assign`/`dispatch` a WO; **auto-timesheet from GPS** geofence enter/exit (`do_log_time` derives the span, tech confirms).
- **Autonomous (gated by threshold):** **auto-assign trivial jobs** under a value/complexity ceiling (`NCE_FIELD_TECH_AUTONOMY_WO_CEILING`) to the top-ranked available tech without human dispatch — governance gate before write; anything above falls back to Advisor.
- **Cognitive recall:** *"who did a similar job well / what did we find last time at this room"* — answered from `v3_cognitive_ledger` outcome history + `memories` completion notes, not a static roster; an auditor can query *why* a tech was picked.
- **Enrichment triggers (event-scoped, never a background sweep):** dispatch ranking + outcome recompute fire **only** when a WO is created/completed or a reschedule lands. Never bulk-recompute all WOs or all techs.

## A2A flows
- **Serves Install→Asset→Cover:** WO completion + S/N scan → hands the `BOM_LINE -[installed_as]-> ASSET` seed to **Assets(9)** for lifecycle enrich → Support SLA activation. (This engine is the *install* leg of that canonical flow.)
- **Receives install jobs from Project(7):** a frozen-BOM phase-gate emits install WOs via `field_tech_create_work_order`.
- **Receives service dispatch from Support(10):** a ticket needing a site visit emits a service WO.
- **Serves Economy(8):** approved `time_entries` → labor cost into the cascade (the `do_record_outcome`/time stream).
- **Serves HR(13):** capacity/utilization signal from time entries + assignment load.
- **Serves Vendors(4):** **contractor performance feedback** — WO ratings/outcomes via `do_record_outcome` feed `vendors_compute_performance`.
- **Consumes:** `vendors_match_contractor` (external candidates) + HR skills/certs (internal candidates) for dispatch.

## Config keys
`NCE_FIELD_TECH_*` in `nce/config.py`: `NCE_FIELD_TECH_ENABLED`, `NCE_FIELD_TECH_AUTONOMY_WO_CEILING` (auto-assign value/complexity gate), `NCE_FIELD_TECH_SLA_RISK_WARN_HOURS` (Watcher trip lead time), `NCE_FIELD_TECH_GPS_GEOFENCE_METERS` (auto-timesheet radius), `NCE_FIELD_TECH_SYNC_MAX_OPS` (per-batch reconcile cap), `NCE_FIELD_TECH_PHOTO_MAX_BYTES`, `NCE_FIELD_TECH_REQUIRE_CHECKLIST_TO_CLOSE` (quality gate on/off). Namespaces opt in via `metadata.field_tech.enabled = true`. Never a host-specific key (FE-5).
**Config-as-IP JSON (namespace-scoped, the business IP — NOT code):**
- `dispatch-weights.json` — skill/cert × location × load × history weights for `do_dispatch` (each tenant tunes its own dispatch policy).
- `checklist-templates.json` — per-WO-kind checklist definitions (required vs optional items, the ISO9001 verification template).
- Reuses Vendors' `partner-redaction.json` as the partner-safe field allow-list (single source of truth; not duplicated here).

## Tables/migrations
**Graph-first** for the WO/checklist/scan relationships (live as `kg_nodes`/`kg_edges`; dispatch/outcome history in `v3_cognitive_ledger`). Three own tables where a keyed lookup + the restricted-access enforcement + the offline-sync write path beat the graph — all `ENABLE` + **`FORCE ROW LEVEL SECURITY`** with **both** the tenant policy **and** the contractor partner sub-scope (Partner Access layer 1), mirrored into `schema.sql` + a numbered migration:
- `work_orders` (`work_order_id, namespace_id, partner_scope_id, kind, source_kind, source_ref, location_id, assignee_id, assignee_kind, status, due_at, raw jsonb, updated_at`) — `tenant_isolation_policy` + `partner_isolation_policy USING (namespace_id = get_nce_namespace() AND partner_scope_id = get_nce_partner_scope())`.
- `checklists` (`checklist_id, work_order_id, namespace_id, partner_scope_id, template_id, items jsonb, completed_at, raw jsonb`) — the ISO9001 verification record; same dual RLS.
- `time_entries` (`time_entry_id, work_order_id, namespace_id, partner_scope_id, started_at, ended_at, source gps|manual, approved bool, op_id, raw jsonb`) — `op_id` unique for offline-sync idempotency; same dual RLS; `approved` gates the Economy labor-cost handoff.

## Dependencies
- **Upstream:** **HR(13)** for internal-tech skills/certs (dispatch eligibility); **Vendors(4)** for contractor profiles + `match_contractor` + the Partner Access Model it owns; **Project(7)** (frozen BOM → install WOs) and **Support(10)** (tickets → service WOs) as WO sources.
- **Downstream:** **Assets(9)** (S/N scan seed → asset lifecycle), **Economy(8)** (approved time entries → labor cost), **HR(13)** (utilization), **Vendors(4)** (contractor performance feedback).
- **Boundary (do NOT duplicate):** **Vendors owns contractor IDENTITY + the Partner Access Model definition + the allow-list** — Field Tech *enforces* it on the WO surface and *delegates* the redacted projection, never redefines it. **Assets owns asset LIFECYCLE** — Field Tech only writes the install-time S/N seed edge. **HR owns the skills-matrix** — Field Tech reads it for dispatch, does not store certs for employees.

## Review round-2 hardening (2026-06-17 — these govern the build)
Field Tech mostly **confirms** the §9 contracts (Partner Access → §9.6 partner-scope RLS; `WORK_ORDER` ownership → §9.1; `BOM_LINE -[installed_as]-> ASSET` → the §9.1 hand-off; autonomy → §9.5). Its genuinely new content is **engine-internal — the offline-sync core — and it's the riskiest engine-internal core in the suite:**
1. **"Last-writer-wins per field by device clock" is UNSAFE — fix B2.** LWW is fine for a **photo or a free-text note**, but for **safety/quality-critical fields** (a checklist item *attesting a verification*, an S/N scan) a stale offline replay can **silently clobber a correct value** — and a **device wall-clock cannot be trusted to order writes** (skew, tamper). Order by **server-receive sequence or a logical/Lamport clock**, and on verification/attestation fields **surface a conflict for human resolution — never blanket silent LWW**. This is the genuinely hard part; "LWW per field" undersells the danger.
2. **Contract-B idempotency must hold at SYNC-REPLAY, not just creation (roadmap §9.5).** Offline-generated autonomous acts (GPS auto-timesheet, auto-assign under `AUTONOMY_WO_CEILING`) are **queued offline then replayed** — so the idempotency key **and** the autonomy gate must apply **across the sync boundary**, not only at first creation. **`do_sync` is where Contract B meets the offline queue** — replaying a queued auto-assign must not double-act, and must still pass the governance gate.
3. **The app↔engine sync protocol is a VERSIONED external contract, not an endpoint.** The op-envelope + conflict protocol is a client-server contract between a **bespoke mobile app and the engine** — the *one place* NCE's pristine backend meets a **non-NCE client**. Version it and keep a compat discipline (like an API contract), so an old app build and a new engine don't silently corrupt the replay.
4. **Grace-degradation — capture/sync ship standalone; dispatch *ranking* needs HR (Tier 4, last) + Vendors.** `do_dispatch` is "pure over profiles + ledger," but profiles come from **HR skills/certs (Tier 4)** + **Vendors performance**. So **WO-create / capture / offline-sync work day one**; dispatch ranking **degrades to location + availability** until HR/Vendors populate, then sharpens.
5. **This is where the partner-scope RLS primitive is most exercised — confirms §9.6 item 4 is a CORE prerequisite.** A contractor on the mobile app hits partner sub-scope RLS on **all three tables** (`work_orders`/`checklists`/`time_entries`). The `nce.partner_scope_id` primitive must be **built + security-hardened in core before B4** — not a Field-Tech feature.

## Build phases
- **B1 — WO core + graph:** `do_create_work_order` (from project/ticket), `work_orders` table (dual RLS), WO node + `for`/`at`/`installs` edges, `field_tech_source_id` retirement. MCP + REST for create/get. A2A receivers for Project + Support.
- **B2 — Capture surface + offline sync:** `do_complete_checklist` (+ `checklist-templates.json`, ISO9001 record), `do_scan_serial` (asset seed edge + Assets A2A handoff), `do_log_time`, `do_attach_photo`. The **`do_sync` reconcile core** (idempotent replay, `op_id` dedup, **server-sequence/logical-clock ordering — NOT device-clock LWW**; verification/safety fields surface conflicts rather than silent-overwrite; Contract-B idempotency + autonomy gate apply at replay — see hardening #1/#2) — the hard part. `checklists`/`time_entries` tables (dual RLS).
- **B3 — Dispatch + AI:** pure `do_dispatch` (skill/cert × location × load × history, `dispatch-weights.json`) reading HR + `vendors_match_contractor`; `do_assign` with cert-eligibility validation + push. SLA-risk / missing-checklist / cert-mismatch Watchers. Cognitive-recall ("similar job") over ledger + memories.
- **B4 — Partner Access + autonomy:** `do_partner_view` (delegates Vendors redaction + layers WO fields), partner sub-scope RLS verified across all three tables, partner-scoped A2A toolset binding. GPS auto-timesheet (`do_log_time` from geofence). Autonomous auto-assign under `AUTONOMY_WO_CEILING` with governance gate.
- **B5 — Feedback loops:** `do_record_outcome` feeding Vendors performance + HR utilization + Economy approved-labor cost; route optimization across a tech's day; data-driven dispatch-weight calibration closing the ledger loop; field slice into the Morning-brief (#19 aggregate).
