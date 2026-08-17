> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Project Engine Admin Guide (Doc 72)

> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

The **Project Engine** (`nce/vertical_modules/project/`) turns a signed Sales quote into a delivered-installation workspace via a G0–G6 phase-gate state machine, BOM-line-status auto-tasking, and read-only PL/capacity/status surfaces. It is **graph-only** — no dedicated `project_*` SQL table exists; every `PROJECT_*` node/edge lives in the shared `kg_nodes`/`kg_edges` tables and inherits their namespace-scoped FORCE RLS. This guide covers enablement, automation-tier configuration, event wiring, RLS/ownership, REST routes, and known operational gaps.

---

## 1. Engine Enablement

> [!WARNING]
> Unlike the Product and Agreements engines (`require_product_enabled` / `require_agreements_enabled` in their respective `_guard.py` files), **the Project engine has no enablement guard today**. There is no `nce/vertical_modules/project/_guard.py`, no `NCE_PROJECT_ENABLED` flag in `nce/config.py`, and neither `nce/vertical_modules/project/mcp_handlers.py` nor `nce/admin_handlers/project.py` checks any opt-in flag before executing. The design doc (`docs/vertical_engines/07-project-engine.md`) describes `NCE_PROJECT_ENABLED` (namespace opt-in via `metadata.project.enabled`) as intended behavior, but it is **not implemented** — all four MCP tools and all seven mounted REST routes are live for every namespace, unconditionally, on any deployment where the code is imported.

### 1.1 Config keys — spec vs. code
The design doc's `NCE_PROJECT_*` config surface does **not exist** in `nce/config.py`. A grep for `NCE_PROJECT` across the config module returns zero hits (only two unrelated comments about System Design's own recall config).

| Spec-claimed key | Present in `nce/config.py`? |
|---|---|
| `NCE_PROJECT_ENABLED` | Not found |
| `NCE_PROJECT_AUTO_TASK` | Not found |
| `NCE_PROJECT_AUTO_PHASE_MAX_TIER` | Not found |
| `NCE_PROJECT_SCOPE_CREEP_THRESHOLD_PCT` | Not found |
| `NCE_PROJECT_RECALL_TOP_K` | Not found — `recall.py:64` reads it defensively via `getattr(cfg, "NCE_PROJECT_RECALL_TOP_K", 5)`, which silently falls back to `5` precisely because the attribute is absent from `cfg` |

Operators should treat every one of these as *planned, not configurable* until they appear in `nce/config.py`. The recall top-k is effectively hardcoded to `5` in practice.

### 1.2 What actually gates behavior today
The only real gating mechanism in the Project engine is the **automation-tiers value axis** (§3) applied to the one mutating background flow (`do_sync_bom_tasks` via `automation.py`) — everything else (`convert_signed_quote`, `advance_phase`, the read-only REST surfaces) runs unconditionally for any namespace whose connection reaches the handler.

---

## 2. Phase-Gate Configuration (Config-as-IP)

`phase_gates.py` hardcodes nothing — it loads `nce/config_data/project-gate-criteria.json` fresh on every call. This is a single global file, not a per-namespace override despite the file's own `_comment` describing it as "namespace-scoped" — `load_gate_config()` resolves one fixed repo path with no namespace parameter, so operators changing this file affect every tenant.

```json
{
  "VALID_PHASE_TRANSITIONS": {
    "G0": ["G1"], "G1": ["G2"], "G2": ["G3"],
    "G3": ["G4"], "G4": ["G5"], "G5": ["G6"], "G6": []
  },
  "GATE_CRITERIA": {
    "G0": [],
    "G1": ["signed_quote_attached", "project_manager_assigned"],
    "G2": ["signed_baseline_frozen", "bom_lines_linked", "kick_off_meeting_held"],
    "G3": ["design_approved", "bom_fully_specified", "site_access_confirmed"],
    "G4": ["frozen_baseline_locked", "bom_ordered", "project_lead_assigned"],
    "G5": ["all_bom_lines_delivered", "installation_complete", "testing_started"],
    "G6": ["all_tests_passed", "customer_sign_off", "as_built_documented", "handover_to_support_done"]
  }
}
```

Editing this file changes gate behavior immediately (no cache to bust, no restart required — it's read from disk per call). There is intentionally no degraded/`unknown`/`waived` state for a criterion referencing an engine that isn't deployed yet (e.g. `project_lead_assigned` before HR ships): the caller must either supply the criterion in `criteria_met` or the gate blocks. Resolving cross-engine facts automatically is explicitly deferred in both `phase_gates.py` and `advance.py` docstrings — it is not a bug to be fixed by this doc, but operators should know `do_advance_phase` will never call out to Sales/Procurement/HR on your behalf.

---

## 3. Automation Tiers (Contract B / C2)

The only mutating flow with real tier-gated autonomy today is BOM→task auto-sync, implemented in `automation.py` and configured by `nce/config_data/automation-tiers.json`:

```json
{
  "tiers": [
    {"tier": 1, "label": "Autonomous",          "min_value": 0,       "max_value": 49999.99,    "autonomy_level": "autonomous",       "confirm_required": false},
    {"tier": 2, "label": "Actor/Confirm",        "min_value": 50000,   "max_value": 499999.99,   "autonomy_level": "actor_confirm",    "confirm_required": true},
    {"tier": 3, "label": "Advisor + PL Review",  "min_value": 500000,  "max_value": 2999999.99,  "autonomy_level": "advisor_pl_review","confirm_required": true},
    {"tier": 4, "label": "Advisor Only",         "min_value": 3000000, "max_value": null,        "autonomy_level": "advisor_only",     "confirm_required": true}
  ]
}
```

The file states its own boundary explicitly: it is "the value axis ONLY" — ceiling/idempotency/kill-switch/audit machinery lives exclusively in `@governed` (`nce/autonomy/governor.py`); `automation.py` never re-implements those invariants.

### 3.1 Fail-closed value resolution
`_run_governed_sync` in `automation.py`:
- If the triggering event payload omits `project_value` → resolves to **Tier 4** (confirm required), not Tier 1. This is a deliberate fix for a prior bug where a missing key defaulted to `0.0` and granted autonomous execution.
- A publisher supplying `0.0` explicitly is treated as a legitimate Tier-1 zero-value project.
- A negative `project_value` is rejected outright (`{"ok": false, "error": "invalid_project_value"}`).
- Idempotency key: `bom_sync:{namespace_id}:{bom_line_label}:{status}` — deterministic and replay-safe.
- `confirm` is derived **only** from the resolved tier, never from the event payload, so a caller cannot self-escalate to autonomous execution.

### 3.2 Event triggers
`automation.py` subscribes two C4 outbox handlers:
- `PO_LINE.status_changed` (Procurement) → `rq_sync_bom_on_po`
- `GOODS_RECEIPT.created` (Warehouse) → `rq_sync_bom_on_goods_receipt` (defaults BOM_LINE status to `DELIVERED`)

Both enqueue an RQ task (rather than executing inline) so the C4 relay's transaction commits first; if RQ/Redis is unavailable the handler logs and returns, relying on the outbox's at-least-once redelivery.

### 3.3 Operational wiring checklist
Three registration calls must happen once at worker startup, in this order, or the automation path is inert:
1. `nce.vertical_modules.project.tasks.register_engine(engine)` and `automation.register_engine(engine)` — both maintain separate internal registries; both must be called.
2. `nce.vertical_modules.project.automation.register_redis_client(redis_client)` — without this, the kill-switch gate (`nce:tools:disabled`) is **skipped** with a warning log rather than enforced. Production workers must call this.
3. `nce.vertical_modules.project.automation.register_automation_subscribers()` (and `nce.vertical_modules.project.tasks.register_bom_task_subscriber()`) — subscribes the C4 bus handlers.

> [!IMPORTANT]
> None of these three calls were found wired into any startup/bootstrap module in this snapshot — only test files call them. Confirm your deployment's worker entrypoint actually calls all three before relying on BOM→task automation in production; otherwise Procurement/Warehouse events will be silently ignored (or, if RQ enqueue itself fails, logged and dropped pending outbox redelivery).

---

## 4. Event Wiring & Audit Stream

### 4.1 `project_phase_advanced`
Declared as a member of the flat `EventType` literal union in `nce/event_types.py` (under the `# PROJECT_EVENTS` comment header) — not a separately named Python constant. Appended to the WORM `event_log` by `advance.py`'s `_append_phase_transition_event` on every successful (non-noop) phase transition, with `agent_id="project-advance-phase"` and `params={"project_id", "from_phase", "to_phase", "actor"}`. This feeds the Lysning `Hendelser.jsx` events feed.

### 4.2 Replay handling
`nce/replay.py` registers `project_phase_advanced` in its `_additional_fork_provenance_types` tuple, mapping it to `_handle_fork_provenance_only` in the replay handler registry. In a forked/reconstructive replay this event is treated as **provenance-only** — it doesn't drive full state-mutation replay logic, consistent with its role as an audit-trail event rather than a primary domain event.

### 4.3 Task creation is not a WORM event
`tasks.py` deliberately does **not** call `append_event` when it creates/closes `PROJECT_TASK` nodes — the module docstring states this is intentional: task creation is not a named WORM event type, and audit is left to the C4 outbox row already written by the Procurement/Warehouse publisher that triggered the sync.

### 4.4 Event Feed & Frontend Consumption
Contrary to the design doc, there is **no dedicated `/api/project/{id}/events` REST route** mounted in `admin_app.py`. Event feed consumers (such as Lysning's `Hendelser.jsx`) query the immutable WORM `event_log` table directly under RLS tenant isolation, filtering for `event_type = 'project_phase_advanced'` (and relevant C4 outbox event rows) correlated by project label/ID.

---

## 5. Database Schema & RLS

There is **no dedicated Project SQL table** — a grep across `nce/schema.sql` and `nce/migrations/*.sql` for project-specific tables returns no hits (the only "project" substring matches are unrelated pricing "projections" in the Product/Procurement bid-price tables). This matches the design intent: Project is **graph-only by default**.

### 5.1 Node types and ownership
`nce/config_data/node-ownership.json` registers:

| `node_type` | `owner_engine` |
|---|---|
| `PROJECT_PROJECT` | `project` |
| `PROJECT_GATE` | `project` |
| `PROJECT_TASK` | `project` |
| `PROJECT_CASE_STUDY` | `project` |
| `SIGNED_BASELINE` | `sales` *(Project reads this — never owns or writes it)* |

> [!WARNING]
> **`PROJECT_CHANGE_ORDER` is used as a live `entity_type` filter in `insights.py` (`do_detect_scope_creep`'s query) but has no corresponding entry in `node-ownership.json`.** The ownership registry is incomplete relative to actual code usage — `assert_owner` is never called for change-order nodes, so nothing currently prevents a different engine from writing a `PROJECT_CHANGE_ORDER` node under the Project label space. Flagged for a follow-up ownership-registry fix; not something this doc can resolve.

`PROJECT_SIGNED_BASELINE` does not exist anywhere in code — correctly so, per Contract A (§9.1): the signed baseline is frozen once by Sales in `sales_signed_baselines`, and Project never creates a competing table or node for it.

### 5.2 RLS inheritance
Since Project writes only to `kg_nodes`/`kg_edges`, it inherits their tenant isolation rather than defining its own policy. `nce/schema.sql` iterates a `tenant_tables` array that includes both `kg_nodes` and `kg_edges` and, for each, applies (illustrative, abbreviated — the real policy in `schema.sql` additionally scopes `FOR ALL TO nce_app` and carries a matching `WITH CHECK` clause; do not copy-paste this snippet as DDL):
```sql
ALTER TABLE kg_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE kg_nodes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_policy ON kg_nodes
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
```
(identically for `kg_edges`). Every Project write additionally passes an explicit `namespace_id` parameter rather than relying on RLS alone — called out as a design invariant in `tasks.py` and `automation.py` docstrings. By contrast, `sales_signed_baselines` (Sales' table, which Project reads) is a genuine dedicated table in the same `tenant_tables` RLS array.

### 5.3 Ownership guard on writes
Every `PROJECT_PROJECT` / `PROJECT_GATE` / `PROJECT_TASK` node write in `convert.py`, `advance.py`, `tasks.py`, and `insights.py` (status-report fact nodes) calls `assert_owner(conn, namespace_id, node_type, "project")` before the `INSERT ... ON CONFLICT`, guarding against a different engine's code accidentally claiming a Project-owned label.

---

## 6. REST Routes (`nce/admin_handlers/project.py` & `nce/admin_app.py`)

All routes are thin wrappers — no business logic, no LLM in the path — registered in `nce/admin_app.py` via `build_admin_routes()`. HMAC/mTLS auth applies at the admin-app boundary same as every other admin route family.

| Method | Path | Handler | Delegates to |
|---|---|---|---|
| POST | `/api/project/convert-signed-quote` | `api_project_convert_signed_quote` | `convert.do_convert_signed_quote` |
| GET | `/api/project/{id}/phase` | `api_project_get_phase` | `advance.read_current_phase` |
| POST | `/api/project/{id}/phase` | `api_project_advance_phase` | `advance.do_advance_phase` |
| GET | `/api/project/my-day` | `api_admin_project_my_day` | `pl.do_my_day` |
| GET | `/api/project/capacity` | `api_admin_project_capacity` | `pl.do_capacity` |
| GET | `/api/project/{id}/scope-creep` | `api_admin_project_scope_creep` | `insights.do_detect_scope_creep` |
| GET | `/api/project/{id}/status-report` | `api_admin_project_status_report` | `insights.do_status_report` |

### 6.1 `convert-signed-quote` Degraded Signals (PR #76) & Literal Prefix Matching (PR #67)
The response from `/api/project/convert-signed-quote` returns:
```json
{
  "project_id": "PROJECT:Q123",
  "gate": "G0",
  "bom_lines_linked": 0,
  "degraded": true,
  "degraded_reasons": ["no_bom_lines_in_graph"],
  "degraded_detail": "No BOM_LINE nodes exist in NCE for this quote, so the project was created with an empty bill of materials. NCE has no path that creates BOM_LINE nodes today, so a zero count does NOT confirm the quote itself had no lines — treat it as missing line data, not as an empty quote.",
  "baseline": {
    "signed_baseline_id": "...",
    "sales_available": true
  }
}
```
- **HTTP 200 does NOT imply complete population:** The endpoint returns HTTP 200 because the `PROJECT_PROJECT`, `PROJECT_GATE`, and `PROJECT_TASK` nodes were created. However, callers must inspect `degraded: true` and `degraded_reasons` (`no_bom_lines_in_graph`, `sales_baseline_unavailable`) rather than assuming full data population.
- **BOM line discovery uses literal prefix matching:** `convert.py` queries `kg_nodes` with `starts_with(label, 'BOM_LINE:{QUOTE}:')` rather than SQL `LIKE` with wildcards (PR #67), preventing cross-quote line matching when quote IDs contain `_` or `%`.

**Not present**, contrary to the original design doc:
- `GET /api/project/{id}` (single composite project detail endpoint). Operators building a PL frontend against this engine compose project state from the distinct endpoints: `/phase` (current gate), `/scope-creep` (baseline diff), and `/status-report` (margin/narrative snapshot).
- `GET /api/project/{id}/events` (Hendelser feed). Project events are not exposed via a dedicated REST route; event feeds read directly from the WORM `event_log` table filtered by `event_type = 'project_phase_advanced'`.

Error-handling convention across all routes: missing `admin_state.engine` → 503; missing/invalid required params → 422; a gate-refused phase advance → 409 with `missing_criteria`; a domain-core `{"ok": false, "error": ...}` result → 400; unhandled exceptions → 500 via the shared `admin_error_response` helper.

---

## 7. Operational Notes & Troubleshooting

- **"Project not converted yet" on advance-phase.** `do_advance_phase` returns `{"ok": false, "error": "... has no 'in_phase' edge ... it may not have been converted yet"}` when there's no `PROJECT -[in_phase]-> GATE` edge. Run `project_convert_signed_quote` first — a project only gets its G0 gate at conversion time.
- **Advance-phase always fails with an empty `missing_criteria` list.** This means the transition itself is illegal (e.g. `G1 → G3`, skipping G2), not that criteria are unmet — check `VALID_PHASE_TRANSITIONS` in `project-gate-criteria.json`; the chain is strictly linear (no skips).
- **`degraded: true` / `no_bom_lines_in_graph` on conversion.** This is currently the expected outcome for every conversion on main, because no subsystem writes `BOM_LINE` nodes yet. The project is created with `bom_lines_linked: 0`. To advance past gate G2, the caller must manually supply `bom_lines_linked` in `criteria_met`.
- **`sales_available: false` / `sales_baseline_unavailable` on convert/scope-creep/status-report.** Either the Sales engine's `sales_get_signed_baseline` A2A tool isn't reachable for this namespace, or there's no `sales_signed_baselines` row for the given `quote_id` yet. This is graceful degradation by design, not an error — but downstream numbers (`signed_total_nok`, margin trinity `signed`) will read `null`/`"unknown"` until Sales freezes the baseline.
- **BOM→task auto-sync appears to do nothing.** Confirm the three registration calls in §3.3 were made at worker startup — the C4 subscribers are defined but not observably wired into any bootstrap path in this codebase snapshot. Also confirm RQ/Redis connectivity; a failed enqueue is logged and silently dropped (pending outbox redelivery), not surfaced as an error to the triggering event's publisher.
- **`project_suggest_pl` always returns `"HR unavailable"`.** Expected until HR (Module 13) ships and an `a2a_client` is passed in `params`. This is not a bug.
- **No enablement toggle works.** As noted in §1, `NCE_PROJECT_ENABLED` doesn't exist — there is no way to disable the Project engine's tools/routes per namespace today short of not calling them.
- **PL/Capacity return `"Unassigned"` for every task.** Team resolution in `do_capacity` depends on `member_of`/`belongs_to`/`reports_to`/`team`/`pl` edges from employee nodes — if HR/org-chart data hasn't been written to the graph for this namespace, every task lands under `"Unassigned"`. This is not a Project engine defect.

---

## 8. Known Code Notes (flagged, not fixed by this doc)

- `convert.py`'s `_upsert_project_node` accepts `signed_by`/`signature_ref`/`signed_baseline_id` as parameters but the `INSERT INTO kg_nodes` only writes `label`, `entity_type`, `namespace_id` — these three values are silently dropped rather than persisted onto the `PROJECT_PROJECT` node.
- `insights.py`'s `do_status_report` inserts its three status-fact nodes (margin/gate/creep) with `entity_type = 'PROJECT_TASK'` rather than a dedicated fact/status node type — they are queryable but co-mingled with real auto-tasking `PROJECT_TASK` rows in `kg_nodes`.
- `tasks.py`'s `_read_bom_line_status` helper is defined but never called from `do_sync_bom_tasks`'s main path (status comes directly from the event payload/params) — dead code kept for completeness/testability per its own docstring.
- `do_recall_similar_projects`, `do_record_project_outcome` (`recall.py`), and `do_generate_case_study_edge` (`case_study.py`) are fully implemented and covered by integration tests, but have zero production call sites (no MCP tool, no REST route, no event subscriber) — see the User Guide §6 for detail.
