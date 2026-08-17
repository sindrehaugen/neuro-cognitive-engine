> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 07 — Project Engine  (nce/vertical_modules/project)

<!-- BLOCKED ON OQ-2 / OQ-4: SPEC PROPOSAL VOICE. This document is an architectural design specification. At baseline 7304330, Project ships 4 MCP tools and 7 REST routes (see docs/_generated/surface.md). Refer to docs/engines/project-user.md and docs/engines/project-admin.md for shipped reality. Verified-against: 7304330 -->

**Status:** spec · **Owner:** NCE core (Sindre) · **Tier 1 · Delivery axis · engine #7**
**Companions:** `VERTICAL_MODULE_PATTERN.md` (skeleton), `00-ENGINES-ROADMAP.md` (§4 graph, §5 deep-AI, §6 sequencing, §7 format)

## Mission
Project is the **KERNE module everything orbits** (Andreas module 04): the workspace where a signed quote becomes a delivered installation. It owns *no BOM of its own* — it adds `PROJECT`/phase/task/change-order structure **onto the shared `BOM_LINE` nodes** already in the graph. Its job is three disciplines, all enforced as code+config, not free text: (1) a **phase-gate state-machine G0–G6** that refuses invalid transitions, (2) the **signed-quote→project bridge** that freezes an immutable contract baseline actuals are measured against, and (3) **auto-tasking from BOM-line status** so procurement/logistics movement becomes project work without manual entry. The deep-AI angle: because every other engine writes to the same graph, Project gets capacity, scope-creep and "projects like this that slipped" **for free** via cognitive recall — and exposes a "My Day" prioritization surface and tiered automation keyed to project *size*.

## Inspiration & triage   (Andreas sources · no Portal sidecar — new build · Lysning pages: MinManed.jsx / "My Day", ModulDetalj.jsx, Hendelser.jsx)
- `lib/project/phase-gates.ts:174` → `canEnterPhase` (`manifest.json#phase-gates`, angerkost 3, test `tests/project/phase-gates.test.ts`): pure G0–G6 state-machine — **lift near 1:1**, port `VALID_PHASE_TRANSITIONS` + `GATE_CRITERIA` to config-as-IP.
- `lib/quote/service.ts:133` → `convertSignedQuoteToProject` (`manifest.json#signed-quote-to-project`, angerkost 3): freezes immutable signed baseline at `:232-237`. **FRESH + CORRECT but ORPHAN — 0 callers** (handoff §2). We are the call-site. **Write the test when we wire it** (Andreas never did).
- `lib/finance/projections/signed-baseline.ts:33` `applySignedBaseline` (idempotent) — the freeze primitive Economy also reads; `marginSignedPct` **never overwritten by cascade** (handoff §7, margin-trinity).
- Andreas 04: `BomTab.tsx:481`/`:1251` per-room cost + roomGroups (real); CO/SUB/deviation tagging; scope-creep detection; tiered automation Tier 1 <50K → Tier 4 3M+; "My Day"; capacity engine. PL-assignment is **🔵 not built** (PL derived from D365 owner; no skill/capacity matching) — we build it.
- **No Portal FE-dev sidecar exists** (roadmap §3 seed = "—, new"). Greenfield NCE vertical.
- **Lysning pages served** (today empty-state, awaiting backend): `MinManed.jsx` ← "My Day"/portfolio pulse; `ModulDetalj.jsx` ← project/module detail; `Hendelser.jsx` ← phase-transition + CO events. These render from this engine's REST routes, no model in the path.

## Classification         (internal + AI; no external system — consumes the graph; pull-only style transport)
- **internal + AI**, NOT push+semantic and NOT external. There is no third-party system to client/auth against — Project **consumes the shared cognitive graph** (`kg_nodes`/`kg_edges` written by Sales, System Design, Procurement, Economy) and writes back its own typed nodes/edges.
- **Transport = pull-only style** (NetBox class, per pattern-doc divergence table): query handlers over Postgres + RQ task triggers fired by *graph events from other engines* (BOM-line status change → auto-task), **no `client.py`/`auth.py`/`webhooks.py`/`ingestion.py`**.
- Files: `mcp_handlers.py` (REQUIRED) + `phase_gates.py` (pure G0–G6) + `baseline.py` (freeze/margin-trinity) + `tasks.py` (BOM→task) + `capacity.py` + `scope.py` (CO/scope-creep). No semantic ingestion track (Project holds structured state, not unstructured text — status-report *text* is generated on demand, not ingested).

## Graph contribution     (PROJECT, GATE, SIGNED_BASELINE, TASK, CHANGE_ORDER nodes; edges PROJECT-in_phase->GATE, PROJECT-freezes->SIGNED_BASELINE, BOM_LINE-generates->TASK; margin-trinity)
**Node entity_types** (prefix `PROJECT_`): `PROJECT_PROJECT`, `PROJECT_GATE` (one per G0–G6 reached), `PROJECT_SIGNED_BASELINE` (immutable), `PROJECT_TASK`, `PROJECT_CHANGE_ORDER`. Reuses spine `CUSTOMER`, `SITE`/`ROOM`, `QUOTE`, **`BOM_LINE`** (never re-created — Project edges onto existing lines), `EMPLOYEE`.
**Edge types** (canonical, roadmap §4):
- `PROJECT -[in_phase]-> GATE` (current phase pointer; history retained)
- `PROJECT -[freezes]-> SIGNED_BASELINE` (set once at conversion, immutable)
- `PROJECT -[contains]-> BOM_LINE` (workspace membership; from the frozen quote)
- `BOM_LINE -[generates]-> TASK` (auto-tasking; `confidence` = rule strength)
- `TASK -[assigned_to]-> EMPLOYEE` (links to HR; populated by PL-assignment)
- `CHANGE_ORDER -[amends]-> BOM_LINE` (scope-creep / deviation trail)
- `PROJECT -[generates]-> CASE_STUDY` (downstream feed to Marketing #14)
**Margin-trinity** lives on `PROJECT_SIGNED_BASELINE` (signed) vs `PROJECT_PROJECT` props (estimated) vs Economy's cascade output (actual): `signed` is write-once and **never overwritten by cost cascades**; estimated/actual move. Every derived edge carries `confidence` (0–1) + `project_source_id` for hard-retire (roadmap §2.3). Cognitive recall writes go to `memories`/`v3_cognitive_ledger` only for slipped-project narratives (see AI features).

## Core functions
Each is a pure-ish `do_<action>(engine, params) -> dict` (dual-surface core; the pure G0–G6/baseline math is import-only, 0 DB):
- `do_convert_signed_quote(engine, {quote_id, signed_by, signature_ref})` → `{project_id, baseline}` — the Sales→Project bridge; **reads the Sales-frozen `SIGNED_BASELINE` (does NOT create one — roadmap §9.1)**, materializes `PROJECT` + `contains` edges onto the quote's `BOM_LINE`s, opens at G0. **Idempotent** on quote_id.
- `can_enter_phase(project, target_phase)` → `{ok, missing_criteria[]}` — **pure**, wraps `VALID_PHASE_TRANSITIONS` + `GATE_CRITERIA`; phase is NOT a free string.
- `do_advance_phase(engine, {project_id, target_phase, actor})` → calls `can_enter_phase`; on `ok` writes new `GATE` + `in_phase`, emits a `Hendelser` event; on fail returns `missing_criteria`.
- `do_sync_bom_tasks(engine, {project_id})` → `{tasks_created[], tasks_closed[]}` — reconciles `BOM_LINE -[generates]-> TASK` from current line statuses (PLANNED→ORDERED→DELIVERED→INSTALLED→TESTED).
- `do_my_day(engine, {employee_id|project_id, date})` → ranked task list (priority = gate-blocking × deadline × value).
- `do_detect_scope_creep(engine, {project_id})` → `{change_orders[], delta_signed_vs_current}` — diffs current BOM against frozen baseline.
- `do_capacity(engine, {window})` → load per PL/team from open `TASK` + `assigned_to`.
- `do_status_report(engine, {project_id})` → generated narrative + margin-trinity snapshot.

## MCP tools          (TOOL_REGISTRY names + flags + AI-role)
| tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `project_can_enter_phase` | true | false | false | Watcher (pure check) |
| `project_advance_phase` | false | true | true | Actor (gated by gate criteria) |
| `project_convert_signed_quote` | false | true | true | Actor / Autonomous-by-tier |
| `project_sync_bom_tasks` | false | false | true | Autonomous (≤Tier 1) / Actor |
| `project_my_day` | true | false | false | Advisor |
| `project_detect_scope_creep` | true | false | false | Watcher |
| `project_capacity` | true | false | false | Advisor |
| `project_status_report` | true | false | false | Advisor |
| `project_suggest_pl` | true | false | false | Advisor (needs HR via A2A) |
Register via `_h(project_mcp_handlers, "handle_project_<action>")` in `nce/tool_registry.py`; update the tool-count test.

## REST routes          (admin api_* routes — no-model path for the BFF / Lysning)
`build_admin_routes()` + `nce/admin_handlers/project.py`, HMAC/mTLS authed, no LLM:
- `GET  /api/project/{id}` → detail (→ `ModulDetalj.jsx`)
- `GET  /api/project/{id}/phase` · `POST /api/project/{id}/phase` (advance; 409 + `missing_criteria` on gate fail)
- `POST /api/project/convert-signed-quote`
- `POST /api/project/{id}/sync-tasks`
- `GET  /api/project/my-day?employee_id=` (→ `MinManed.jsx`)
- `GET  /api/project/{id}/scope-creep` · `GET /api/project/{id}/status-report`
- `GET  /api/project/capacity?window=` · `GET /api/project/{id}/events` (→ `Hendelser.jsx`)
Every read-only/deterministic capability gets a REST route (pattern-doc rule of thumb).

## AI features
- **Watcher** — scope-creep/change-order detection (current BOM vs frozen baseline); gate-blocked-too-long alerts; capacity-overload alerts.
- **Advisor** — **PL-assignment suggestion** (ranks PLs by skill-fit + load; **depends on HR skills-matrix via A2A** — see Dependencies, blocker until HR #13 ships); status-report generation; "My Day" prioritization.
- **Actor** — `advance_phase` and `convert_signed_quote` execute *with confirmation*.
- **Autonomous (tier-gated by project SIZE)** — the AI-role taxonomy applied to value, encoded in `automation-tiers.json`: **Tier 1 <50K = Autonomous** (auto-tasking, auto phase-advance when gate criteria met) · Tier 2 50K–500K = Actor (confirm) · Tier 3 500K–3M = Advisor + mandatory PL review · **Tier 4 3M+ = senior-PL only (Advisor-only, no auto-acts)**. Tool flags don't change; the *tier* gates whether a mutating tool may self-trigger.
- **Cognitive recall** — "**projects like this that slipped**": on conversion/phase-entry, embed the project's BOM/room shape into `memories` and query for similar past projects + their actual-vs-signed margin drift and gate dwell-time (`v3_cognitive_ledger`); surfaces risk before G3→G4.
- **Enrichment trigger discipline** (roadmap §5) — AI is **event-triggered and scoped**, never a background sweep: recall runs on convert + each gate attempt; status-report on request; scope-creep on BOM-line change. No bulk re-analysis of all projects.

## A2A flows
- **Serves** the **Quote→Design→Procure** chain's terminal handoff: receives the frozen BOM from Sales (signed quote in) and is the workspace Procurement/Logistics report status back into.
- **Serves** the #19 **Morning brief** aggregate: exposes `project_capacity` + `project_status_report` + `project_detect_scope_creep` so one Executive agent composes the cross-engine "1 risk + 1 opportunity" with no new engine.
- **Initiates** PL-assignment: `project_suggest_pl` calls **HR**'s skills-matrix tool over the A2A bus.
- **Initiates** task triggers: consumes Procurement **PO-status** / Warehouse **goods-receipt** graph events → `do_sync_bom_tasks` (Receive→Match flow's project-side echo).
- **Produces** `PROJECT -[generates]-> CASE_STUDY` consumed by Marketing #14.

## BIM integration (see `07a-project-engine-bim-research.md`)
A graph-native backend is unusually well-fit for BIM. Standardize on **open standards** (IFC/Speckle/COBie); keep Autodesk/AI as pluggable connectors, never the foundation. Deferred to a later phase (after the pure cores + auto-tasking), but designed-for now:
- **Speckle (best fit) ★** — `specklepy`/GraphQL adapter mapping Speckle objects ⇄ our nodes/edges ~1:1, with **Speckle *versions* → Project phase-gate snapshots**, and a **Speckle Automate** function that validates the BOM against the live model on each commit (data-clash: "room has a display node but no power/network element").
- **IFC (IfcOpenShell)** — `IfcSpace ↔ FUNCTIONAL_LOCATION` (System Design *authors* intent; Project consumes the as-built once promoted). Open import/export floor.
- **COBie exporter at handover** — ROOM→Space, product→Type, installed instance→Component, warranty/spares/PM→Warranty/Spare/Job — feeds **Assets(9)/Support(10)**; spreadsheet form first.
- **Autodesk APS read-adapter** (optional, proprietary connector) — pull a client Revit/ACC model via Model Derivative → ingest rooms+components, no Revit license.
- **As-built as a first-class state transition (the closed loop):** `designed → quoted → delivered → as-built → serviced`. Project **emits the as-built diff back onto the design nodes**, so the next design starts from reality — the loop the incumbents structurally lack (`90-competitive-landscape`).

## Config keys            (NCE_PROJECT_* + config-as-IP: GATE_CRITERIA, automation-tiers.json)
`nce/config.py`, namespaced `NCE_PROJECT_*`:
- `NCE_PROJECT_ENABLED` (bool; namespace opts in via `metadata.project.enabled = true`)
- `NCE_PROJECT_AUTO_TASK` (bool — master switch for BOM→task autonomy)
- `NCE_PROJECT_AUTO_PHASE_MAX_TIER` (int, default 1 — highest tier allowed to self-advance)
- `NCE_PROJECT_SCOPE_CREEP_THRESHOLD_PCT` (margin drift that raises a CO alert)
- `NCE_PROJECT_RECALL_TOP_K` (similar-projects pulled for slipped-project recall)
**Config-as-IP** (namespace-scoped JSON, business rules out of code — roadmap §2.9): `GATE_CRITERIA` (what must be true per gate, e.g. G3→G4 requires frozen baseline + BOM ordered + PL assigned), `VALID_PHASE_TRANSITIONS` (the legal G-edges), `automation-tiers.json` (value bands → autonomy level). Each tenant tunes its own; the *engine* is shared.

## Tables/migrations
**Graph-only by default** — `PROJECT_*` nodes/edges live in `kg_nodes`/`kg_edges` (RLS by `namespace_id`, already FORCE-RLS). **No `project_signed_baselines` table** — the signed baseline is owned + frozen by **Sales** (`sales_signed_baselines`, roadmap §9.1); Project **reads** it via the Sales engine and references it by id. Project's only candidate own table is the **structured-outcome attribution** store (hardening #1) if the graph proves the wrong shape for it; otherwise outcomes are typed edges/nodes. Any own table gets `ENABLE` + `FORCE ROW LEVEL SECURITY` + `tenant_isolation_policy USING (namespace_id = get_nce_namespace())` + `schema.sql` mirror + migration.
Phase transitions emit to the existing WORM `event_log` (`event_type='project_phase_advanced'`/`'project_change_order'`) for `Hendelser.jsx`.

## Dependencies
- **Sales (#5)** — *signed quote in*: provides `QUOTE` + signed event that triggers `convert_signed_quote`. Sales **owns** the signed quote; Project receives the frozen BOM. (Hard upstream.)
- **System Design (#6)** — *frozen BOM*: the `BOM_LINE` nodes Project edges onto and tasks from. (Hard upstream; Tier-1 sibling, shares BOM-as-workspace.)
- **HR (#13)** — *capacity/skills for PL-assignment*: `project_suggest_pl` needs the skills-matrix via A2A. **Blocker** for the Advisor PL feature until HR ships (Tier 4); capacity from open TASKs works without it.
- **Economy (#8)** — *margin/actuals*: consumes the frozen signed baseline; feeds actual margin back via cascade (never overwrites signed). (Downstream consumer + actuals source.)
- **Procurement (#1) / Warehouse (#11)** — *PO/GR status → task triggers*: graph events drive `sync_bom_tasks`.
- **External blockers:** none (no third-party API — internal engine).

## Review round-2 hardening (2026-06-17 — these govern the build)
1. **Project's outcome-writing is load-bearing for the whole suite — make it STRUCTURED, not fuzzy narrative (first-class build phase).** System Design's outcome-weighted recall and Product's `failure_pattern` edges depend on Project emitting delivered-outcome signals. "Slipped-project narrative → `memories`" (embeddings) is too fuzzy to make future designs/products smarter. Required: **structured, attributable outcomes** — not "this project slipped" but "**this `BOM_LINE` / this `PRODUCT` / this design pattern** caused the change-order / the margin erosion / the ticket." That attribution **is the value the other two engines extract**, so it gets its own named build phase, not a footnote under recall.
2. **`BOM_LINE` has 5 writers — Project does NOT own its status (roadmap §9.1).** Auto-tasking *reads* the `PLANNED→ORDERED→DELIVERED→INSTALLED→TESTED` state-machine but **Project writes none of those transitions** (Procurement→ORDERED, Warehouse→DELIVERED, Field Tech→INSTALLED/TESTED). Project edges `TASK`s on and reads status. The per-transition writer-of-record + the change-order-locks-status rule live in the registry; Project relies on it, doesn't re-implement it.
3. **`SIGNED_BASELINE` is frozen ONCE, by Sales — Project READS it (contradiction resolved, roadmap §9.1).** **Remove `do_*` baseline *creation* and the `project_signed_baselines` table.** Sales owns `do_freeze_signed_baseline` + `sales_signed_baselines` (the baseline exists the instant of signing, before any project does). `do_convert_signed_quote` **reads the already-frozen baseline**; it never creates a second one. (See the Tables note below.)
4. **`GATE_CRITERIA` leak cross-engine state — add a degraded mode.** "G3→G4 requires frozen baseline + BOM ordered + PL assigned" references Sales, **Procurement** (ORDERED), **HR** (PL, Tier 4). `can_enter_phase` stays pure, but `do_advance_phase` gathers cross-engine facts — and if a producing engine isn't live, the project would **deadlock**. Each criterion supports `unknown`/`waived` with an **explicit flag** so gates referencing not-yet-built engines don't block delivery (grace-degradation, same shape as System Design recall).
5. **Size-tier autonomy is the best in the suite — but route it through roadmap §9.5.** Value bands are the *value axis* only; add the **risk flags** (flagship/first-of-kind) and the **safety machinery** (idempotency, kill-switch, rate-cap, ledger audit). `do_convert_signed_quote` is idempotent (good); **autonomous `do_sync_bom_tasks` closing real tasks needs the same guards** — a runaway sync is destructive.

> **Scope exemplar (make it the roadmap default):** Project's "**owns no BOM — edges onto shared `BOM_LINE` nodes, never re-creates**" is the cleanest boundary in all the specs; it is the model §9.1 generalises for every engine.

## Build phases
- **P1 — Lift the pure cores.** Port `phase_gates.py` (G0–G6, `VALID_PHASE_TRANSITIONS` + `GATE_CRITERIA` → config) + `baseline.py` (freeze + margin-trinity). `mcp_handlers.py` for `can_enter_phase` only. Tests for the pure functions (the spec Andreas never wrote).
- **P2 — Wire the orphan bridge.** `do_convert_signed_quote` + `project_convert_signed_quote` tool + `project_signed_baselines` table/migration. **Write the conversion test** as we wire it (the 0-caller fix). REST `convert-signed-quote` + phase routes.
- **P3 — Auto-tasking.** `tasks.py` + `do_sync_bom_tasks`, BOM-line-status→TASK rules, RQ trigger on Procurement/Warehouse graph events; `automation-tiers.json` + tier-gated autonomy.
- **P4 — PL surfaces.** `do_my_day`, `do_capacity`, `do_detect_scope_creep`, `do_status_report`; REST routes feeding `MinManed.jsx`/`ModulDetalj.jsx`/`Hendelser.jsx`.
- **P5 — Deep AI.** Cognitive recall ("projects like this that slipped") into `memories`/ledger; `project_suggest_pl` via HR A2A (gated on HR availability); `CASE_STUDY` generate-edge for Marketing.
- **Gates green each phase:** ruff, ruff format, mypy `nce/`, pytest (+ tool-count assertion).
