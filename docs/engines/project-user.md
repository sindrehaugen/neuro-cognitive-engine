> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Project Engine User Guide (Doc 71)

> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

The **Project Engine** (`nce/vertical_modules/project/`) is the workspace where a signed Sales quote becomes a delivered installation. It owns **no BOM of its own** — it edges onto the shared `BOM_LINE` nodes that System Design/Sales already wrote — and enforces three disciplines as code+config rather than free text: a **G0–G6 phase-gate state machine**, the **signed-quote→project bridge** that materializes a project from a Sales-frozen baseline, and **BOM-line-status-driven auto-tasking**. Read-only "My Day" prioritization, team capacity, scope-creep detection, and status-report surfaces are also live and exposed over REST.

---

## 1. Phase-Gate Lifecycle (G0–G6)

The state machine is **pure** — zero DB, zero HTTP — and lives in [`phase_gates.py`](https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/project/phase_gates.py). It loads its rules from the config-as-IP file `nce/config_data/project-gate-criteria.json` on every call (no in-memory caching) rather than hardcoding transitions or criteria in Python.

### 1.1 Valid transitions
The chain is strictly linear — no skips, no branches:

```
G0 → G1 → G2 → G3 → G4 → G5 → G6 (terminal)
```

### 1.2 Gate criteria
Each target gate names the criterion keys that must be present in the caller-supplied `criteria_met` list before the transition is allowed:

| Target gate | Required criteria |
|---|---|
| G0 | *(none — opened automatically at conversion)* |
| G1 | `signed_quote_attached`, `project_manager_assigned` |
| G2 | `signed_baseline_frozen`, `bom_lines_linked`, `kick_off_meeting_held` |
| G3 | `design_approved`, `bom_fully_specified`, `site_access_confirmed` |
| G4 | `frozen_baseline_locked`, `bom_ordered`, `project_lead_assigned` |
| G5 | `all_bom_lines_delivered`, `installation_complete`, `testing_started` |
| G6 | `all_tests_passed`, `customer_sign_off`, `as_built_documented`, `handover_to_support_done` |

> **Note on criteria resolution:** `can_enter_phase` does not itself gather these facts from Sales/Procurement/HR — it only checks whether the criterion key is present in the caller-supplied `criteria_met` list. There is currently no `unknown`/`waived` state for a criterion that references an engine not yet live; a criterion is either present (satisfied) or absent (missing). Auto-resolution of cross-engine criteria is explicitly deferred in both `phase_gates.py` and `advance.py`.

### 1.3 `project_can_enter_phase` (MCP tool)
Pure readiness check — no DB, no side effects.

- **Cacheable:** true · **Admin-only:** false · **Mutation:** false
- **Params:** `namespace_id` (required), `project` (dict with `current_phase` and `criteria_met`), `target_phase` (str)
- **Returns:** `{"ok": bool, "missing_criteria": list[str]}`
  - `ok=True, missing_criteria=[]` — legal transition, all criteria met.
  - `ok=False, missing_criteria=[...]` — legal edge, but named criteria are unmet.
  - `ok=False, missing_criteria=[]` — the transition itself is not a legal edge (e.g. skipping G2→G5); never raises.

```json
// request
{"namespace_id": "...", "project": {"current_phase": "G1", "criteria_met": ["signed_quote_attached"]}, "target_phase": "G2"}

// response
{"ok": false, "missing_criteria": ["signed_baseline_frozen", "bom_lines_linked", "kick_off_meeting_held"]}
```

### 1.4 `project_advance_phase` (MCP tool / Actor)
Advances a real project. Reads the current phase from the graph (`PROJECT -[in_phase]-> GATE` edge), validates via `can_enter_phase`, and — on success — atomically upserts the new `PROJECT_GATE` node, moves the `in_phase` edge, and appends a `project_phase_advanced` row to the WORM `event_log`.

- **Cacheable:** false · **Admin-only:** true · **Mutation:** true
- **Params:** `namespace_id`, `project_id` (e.g. `"PROJECT:Q123"`), `target_phase`, `actor` (all required); `criteria_met` (list[str], optional, default `[]`)
- **Returns** one of:
  - `{"ok": true, "phase": "G2"}` — transition succeeded.
  - `{"ok": true, "phase": "G1", "noop": true}` — already in the target phase; no writes, no gate check performed.
  - `{"ok": false, "missing_criteria": [...], "current_phase": "G1"}` — gate refused.
  - `{"ok": false, "error": "..."}` — bad params, or the project has no `in_phase` edge yet (i.e. it hasn't been converted).

The old `PROJECT_GATE` node is retained in the graph as history; only the `in_phase` pointer moves.

### 1.5 REST equivalents
`GET /api/project/{id}/phase` and `POST /api/project/{id}/phase` mirror the read/advance operations for a non-LLM frontend (Lysning's `ModulDetalj.jsx`). A gate-refused advance returns **HTTP 409** with `{"missing_criteria": [...], "current_phase": ...}`. Missing/invalid params (path `id`, `namespace_id`, malformed JSON body) return **HTTP 422** on both routes. An absent/unconverted project returns **HTTP 400** only on the `POST` advance route (surfaced from the domain-core `{"ok": false, "error": ...}` result); the `GET` phase route has no error path for that case — it returns **200** with `"phase": null`.

---

## 2. Sales → Project Bridge (`convert_signed_quote`)

Implemented in [`convert.py`](https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/project/convert.py). This is the cross-engine handoff (**Contract A**): Sales freezes a `sales_signed_baselines` row **once**, at signature time; Project only ever **reads** it — it never creates a `SIGNED_BASELINE` node or a `project_signed_baselines` table.

### 2.1 What it does
1. Reads the Sales-frozen baseline via the seam `_read_signed_baseline(engine, namespace_id, quote_id)`, which resolves at runtime to the A2A tool `sales_get_signed_baseline`.
2. **Discovers existing `BOM_LINE` nodes for the quote** (`_fetch_bom_line_labels`, `:313-346`): queries `kg_nodes` where `entity_type = 'BOM_LINE'` and `namespace_id = $1::uuid` using literal prefix matching `starts_with(label, $2)` with parameter `f"BOM_LINE:{quote_id.upper()}:"`. **Postgres `starts_with()` is used instead of SQL `LIKE`** (PR #67). Because `quote_id` is caller-supplied, SQL `LIKE` wildcard metacharacters (`_` matching any single character, `%` matching any sequence) would otherwise silently match and attach BOM lines belonging to a different quote within the tenant (e.g. `'BOM_LINE:QA1:AMP01' LIKE 'BOM_LINE:Q_1:%'` evaluates to true). `starts_with()` enforces strict literal-prefix matching with zero wildcard semantics.
3. In one transaction: upserts `PROJECT_PROJECT` (label `PROJECT:{QUOTE_ID}`), a `PROJECT_GATE` node at `G0`, one seed `PROJECT_TASK` node (`TASK:{QUOTE_ID}:INIT:000`), the `PROJECT -[in_phase]-> GATE@G0` edge, and one `PROJECT -[contains]-> BOM_LINE` edge per discovered BOM line.

### 2.2 `project_convert_signed_quote` (MCP tool / Actor)
- **Cacheable:** false · **Admin-only:** true · **Mutation:** true
- **Params:** `namespace_id`, `quote_id` (both required); `signed_by`, `signature_ref` (read but currently not persisted onto the `PROJECT_PROJECT` node — see §5 Known code notes)
- **Returns:**

*Current runtime response on main (where no code path creates `BOM_LINE` nodes yet):*
```json
{
  "project_id": "PROJECT:Q123",
  "gate": "G0",
  "bom_lines_linked": 0,
  "degraded": true,
  "degraded_reasons": [
    "no_bom_lines_in_graph"
  ],
  "degraded_detail": "No BOM_LINE nodes exist in NCE for this quote, so the project was created with an empty bill of materials. NCE has no path that creates BOM_LINE nodes today, so a zero count does NOT confirm the quote itself had no lines — treat it as missing line data, not as an empty quote.",
  "baseline": {
    "signed_baseline_id": "b1e2...",
    "sales_available": true
  }
}
```

*Ideal/future non-degraded response (when `BOM_LINE` nodes are present in the graph):*
```json
{
  "project_id": "PROJECT:Q123",
  "gate": "G0",
  "bom_lines_linked": 4,
  "degraded": false,
  "degraded_reasons": [],
  "degraded_detail": null,
  "baseline": {
    "signed_baseline_id": "b1e2...",
    "sales_available": true
  }
}
```

> **Read `degraded`, not the HTTP status.** A conversion can succeed
> structurally and still be incomplete. `degraded: true` means the project WAS
> created but part of the data it should reference is missing:
>
> | Reason code | Meaning |
> |---|---|
> | `no_bom_lines_in_graph` | No `BOM_LINE` nodes were found, so the project has an **empty bill of materials**. Nothing in NCE creates `BOM_LINE` nodes today, so this means the line data is absent from NCE — it does **not** mean the quote had no lines. |
> | `sales_baseline_unavailable` | The Sales-frozen baseline could not be read (mirrors `baseline.sales_available == false`). |
>
> The response has no `ok` key, and the REST route still returns 200 for a
> degraded conversion because the project really was created. Clients and UI
> must branch on `degraded` rather than infer full success from the status code.

### 2.3 Idempotency
Idempotent on `quote_id`: the `PROJECT_PROJECT`/`PROJECT_GATE`/`PROJECT_TASK` labels are deterministically derived from `quote_id`, and every INSERT uses `ON CONFLICT ... DO UPDATE`. Re-running the same conversion updates the same rows in place — no duplicates.

### 2.4 Graceful degradation
If the Sales baseline is unavailable (Sales engine not deployed for the namespace, or no row for that `quote_id`), the function does **not** block or fabricate data — it proceeds with the graph writes and returns `"sales_available": false, "signed_baseline_id": null`, plus `degraded: true` with the `sales_baseline_unavailable` reason code.

The same applies to the bill of materials: if no `BOM_LINE` nodes exist for the quote, the project is created with zero `contains` edges and the result carries `degraded: true` / `no_bom_lines_in_graph`. **This is currently the case for every conversion** — no code path in NCE creates `BOM_LINE` nodes yet, so `bom_lines_linked` is always `0` in practice. Degrading loudly here is deliberate: a silent `0` would be indistinguishable from a quote that genuinely had no lines.

### 2.5 REST equivalent
`POST /api/project/convert-signed-quote` — same request/response shape as the MCP tool.

---

## 3. Auto-Tasking from BOM-Line Status (`sync_bom_tasks`)

Implemented in [`tasks.py`](https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/project/tasks.py) + [`automation.py`](https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/project/automation.py). **This is event-driven, not an MCP tool** — there is no `project_sync_bom_tasks` tool and no REST route. It fires when a `BOM_LINE.status_changed` (or Procurement/Warehouse) graph event arrives on the C4 reactive bus.

### 3.1 Status → task mapping
Each BOM-line status advance opens the corresponding task kind and closes tasks for earlier statuses:

| BOM_LINE status | Task kind opened |
|---|---|
| `PLANNED` | `PROCUREMENT` |
| `ORDERED` | `DELIVERY` |
| `DELIVERED` | `INSTALLATION` |
| `INSTALLED` | `TESTING` |
| `TESTED` | `HANDOVER` |

`do_sync_bom_tasks(engine, params)` writes one `PROJECT_TASK` node + one `BOM_LINE -[generates]-> TASK` edge (confidence `0.9`) for the current status, then removes the `generates` edges for every earlier-status task ("closing" a task = deleting its edge; there is no status column on `kg_nodes`). Per **Contract A**, this module reads `BOM_LINE` status but never writes to a `BOM_LINE` node — Procurement/Warehouse/Field Tech own those transitions.

Params: `namespace_id`, `project_id`, `bom_line_label`, `status` (all required). Returns `{"ok": true, "tasks_created": [...], "tasks_closed": [...]}`, or a soft `{"ok": true, "tasks_created": [], "tasks_closed": [], "skipped_reason": "..."}` for an unrecognized status or a BOM line not linked to the project.

### 3.2 Tier-gated autonomy
Because auto-tasking is a mutating act, `automation.py` gates it by the project's contract value against `nce/config_data/automation-tiers.json` before calling `do_sync_bom_tasks` through the `@governed` decorator:

| Tier | Value band | Autonomy |
|---|---|---|
| 1 | < 50,000 | Autonomous — self-executes, no confirmation |
| 2 | 50,000 – 499,999.99 | Actor — single human confirmation required |
| 3 | 500,000 – 2,999,999.99 | Advisor + mandatory PL review |
| 4 | ≥ 3,000,000 | Advisor only — no autonomous or single-confirm action |

If a publisher omits `project_value` entirely, the resolver **fails closed to Tier 4** (confirm required) rather than defaulting to `0.0`/Tier 1 — a deliberate fix for a prior bug. A negative value is rejected outright. The idempotency key is `bom_sync:{namespace_id}:{bom_line_label}:{status}`.

This whole path (subscribing to Procurement `PO_LINE.status_changed` and Warehouse `GOODS_RECEIPT.created`) is fully implemented and unit-tested, and **as of M0.W20d the subscribers and both engine registries ARE called** at startup in both relay-running processes (`nce/mcp_stdio_main.py` and `nce/cron.py`). The earlier note here — that `register_automation_subscribers()` was not observably called — is no longer true.

> [!NOTE]
> **The automation is nevertheless DORMANT BY DECISION (2026-09-01), and that is not a defect.** Nothing in the repository emits either selector: `status_changed` is emitted nowhere, Procurement's node type is `PO` rather than `PO_LINE` and it emits `upserted`, and Warehouse emits `GOODS_RECEIPT.upserted` — a different selector with a different payload contract. So these handlers are registered and never invoked. Waking the path up requires Procurement to grow a `PO_LINE` node with a status model, which is a feature rather than wiring. Registration is still correct and deliberate: an unregistered `event_type` fast-fails to the dead-letter queue, so being registered is what stops the first real producer manufacturing DLQ rows.

---

## 4. My Day, Capacity, Scope Creep & Status Report
These are read-only Advisor/Watcher surfaces, exposed only over REST admin routes (no MCP tool, no LLM in the path).

### 4.1 My Day (`do_my_day` / `pl.py`)
`GET /api/project/my-day?namespace_id=...&employee_id=...&reference_date=...`

Ranks all `PROJECT_TASK` nodes reached by a `generates` edge by priority:

$$\text{priority} = \text{gate\_blocking\_factor} \times \text{deadline\_factor} \times \text{value}$$

- `gate_blocking_factor` = `1.5` if a `is_gate_blocking`/`gate_blocking` edge is true, else `1.0`.
- `deadline_factor` = `10.0` if the deadline is today or past; otherwise `1.0 / (days_until_deadline + 1)`.
- `value` comes from a `value`/`has_value` edge (default `1.0`).

Returns `{"ok": true, "tasks": [{"task_label", "priority", "gate_blocking", "deadline", "value"}, ...]}`, sorted by priority descending (ties broken alphabetically by task label for determinism).

### 4.2 Capacity (`do_capacity` / `pl.py`)
`GET /api/project/capacity?namespace_id=...&start_date=...&end_date=...`

Aggregates open-task load per team. Employees are resolved to teams via `member_of`/`belongs_to`/`reports_to`/`team`/`pl` edges; tasks with no resolvable assignee land under `"Unassigned"`. Returns `{"ok": true, "teams": {"<team>": {"total_load": float, "tasks": [...]}}}`. If `start_date`/`end_date` are supplied, only tasks with a deadline inside that window are counted.

### 4.3 Scope creep (`do_detect_scope_creep` / `insights.py`)
`GET /api/project/{id}/scope-creep?namespace_id=...`

Sums the value of every `PROJECT_CHANGE_ORDER` node that `amends` a `BOM_LINE` the project `contains`, and compares the total against the Sales-frozen `signed_total_nok` (read via the same `_read_signed_baseline` seam as §2). Returns:
```json
{
  "ok": true,
  "change_orders": [{"label": "...", "node_id": "...", "value": 12000.0, "amended_bom_line": "BOM_LINE:Q123:AMP01"}],
  "delta_signed_vs_current": 12000.0,
  "signed_total_nok": 480000.0,
  "current_total_nok": 492000.0,
  "sales_available": true
}
```
When the Sales baseline is unavailable, `signed_total_nok` is `null` and `current_total_nok` falls back to the raw creep total.

### 4.4 Status report (`do_status_report` / `insights.py`)
`GET /api/project/{id}/status-report?namespace_id=...&estimated_cost_nok=...&estimated_revenue_nok=...`

Builds the margin-trinity snapshot (§5), reads the current gate + dwell time (days since the `PROJECT_GATE` node was created), calls `do_detect_scope_creep` internally, then produces a grounded, citation-backed narrative via the shared C9a `ground()` helper. Returns `{"ok": true, "narrative": str, "margin_trinity": {...}, "citations": [...]}`.

---

## 5. Margin Trinity & the Signed Baseline

[`baseline.py`](https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/project/baseline.py) computes the three-way margin snapshot without ever writing to the database:

- **`signed`** — read verbatim from Sales's `sales_signed_baselines` row; Project never overwrites it.
- **`estimated`** — computed here from caller-supplied `estimated_cost_nok`/`estimated_revenue_nok` as `(revenue - cost) / revenue`; this is the only dimension Project "writes" (in-memory only — no persistence).
- **`actual`** — always `None` here; owned by the Economy engine's cost cascade, out of scope for Project.

If the Sales baseline is unavailable, `build_margin_trinity` returns `{"signed": "unknown", "estimated": None, "actual": None, "signed_baseline_id": None, "sales_available": False}` rather than fabricating a number.

---

## 6. PL Suggestion & Cognitive Recall (Advisor)

### 6.1 `project_suggest_pl` (MCP tool)
- **Cacheable:** true · **Admin-only:** false · **Mutation:** false
- **Params:** `namespace_id`, `project_id` (required)
- Calls HR's `hr.suggest_pl` tool via an injected A2A client. Degrades gracefully to `{"ok": false, "error": "HR unavailable"}` when no client is supplied or the call fails — this is expected today, since HR (Module 13) is not yet built.

### 6.2 "Projects like this that slipped" — implemented, not yet exposed
`do_recall_similar_projects` (`recall.py`) is a fully working pgvector similarity search over `memories`/`v3_cognitive_ledger` (`node_type='PROJECT'`), returning `{"project", "slip_reason", "similarity"}` tuples ranked by cosine similarity, optionally filtered by a keyword query. `do_record_project_outcome` idempotently writes the outcome memory + ledger row that this search reads. **Neither function is registered as an MCP tool or wired to any REST route or event trigger today** — they exist and are integration-tested, but nothing in production code calls them yet. Treat this feature as *implemented-but-dormant* rather than live.

### 6.3 Case study generation — implemented, not yet triggered
`do_generate_case_study_edge` (`case_study.py`) creates a `PROJECT_CASE_STUDY` node and a `PROJECT -[generates]-> CASE_STUDY` edge, but **only when the project's current phase is `G6`** (returns `{"ok": false, "reason": "in_flight", "current_phase": ...}` otherwise). Like §6.2, this function has no production caller — it is not invoked automatically when `do_advance_phase` reaches G6, and there is no MCP tool or REST route for it. *(planned — not yet wired)*.

---

## 7. Worked Example: Quote to G2

```
1. project_convert_signed_quote
   {"namespace_id": "...", "quote_id": "Q100", "signed_by": "alice", "signature_ref": "SIG-001"}
   → {"project_id": "PROJECT:Q100", "gate": "G0", "bom_lines_linked": 0,
      "degraded": true, "degraded_reasons": ["no_bom_lines_in_graph"],
      "degraded_detail": "No BOM_LINE nodes exist in NCE for this quote, so the project was created with an empty bill of materials. NCE has no path that creates BOM_LINE nodes today, so a zero count does NOT confirm the quote itself had no lines — treat it as missing line data, not as an empty quote.",
      "baseline": {"signed_baseline_id": "...", "sales_available": true}}

2. project_advance_phase (G0 → G1)
   {"namespace_id": "...", "project_id": "PROJECT:Q100", "target_phase": "G1",
    "actor": "alice", "criteria_met": ["signed_quote_attached", "project_manager_assigned"]}
   → {"ok": true, "phase": "G1"}

3. project_advance_phase (G1 → G2), missing a criterion
   {"namespace_id": "...", "project_id": "PROJECT:Q100", "target_phase": "G2",
    "actor": "alice", "criteria_met": ["signed_baseline_frozen", "bom_lines_linked"]}
   → {"ok": false, "missing_criteria": ["kick_off_meeting_held"], "current_phase": "G1"}
```

*(Note: In step 3, `bom_lines_linked` is supplied in `criteria_met` by the caller if manually verified, enabling the gate transition once all required criteria are satisfied).*

---

## 8. Reference: Tool Summary

| Tool | Cacheable | Admin-only | Mutation | Surface |
|---|---|---|---|---|
| `project_can_enter_phase` | true | false | false | MCP |
| `project_convert_signed_quote` | false | true | true | MCP + REST |
| `project_advance_phase` | false | true | true | MCP + REST |
| `project_suggest_pl` | true | false | false | MCP |
| `do_sync_bom_tasks` (no tool name) | — | — | — | C4 event-driven only, tier-gated |
| `do_my_day` / `do_capacity` / `do_detect_scope_creep` / `do_status_report` | — | — | — | REST only (no MCP tool) |
| `do_recall_similar_projects` / `do_record_project_outcome` / `do_generate_case_study_edge` | — | — | — | *(planned — implemented, unwired)* |

Every project entry point is namespace-scoped: writes go through `assert_owner` for `PROJECT_PROJECT`/`PROJECT_GATE`/`PROJECT_TASK`, and all queries carry an explicit `namespace_id` filter rather than relying on RLS alone.
