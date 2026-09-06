> **Status:** shipped · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

# Support Engine User Guide

> **Status:** shipped · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

The **Support Engine** (`nce/vertical_modules/support/`, Module 10) is the native customer-service desk of the Neuro-Cognitive Engine. It owns the `TICKET`, `SLA`, `SUPPORT_HEALTH_SCORE`, and `SUPPORT_DIAGNOSIS` graph node types (`nce/config_data/node-ownership.json:60-67`) and provides: a native `ServiceTicket` lifecycle (open → resolve), a deterministic SLA countdown clock, a rolling customer health / churn-risk score, an AI Troubleshooter that recalls past resolutions from the cognitive ledger, keyword-based ticket triage, autonomous-under-ceiling dispatch to Field Tech, and D365 case sync. The module docstring (`nce/vertical_modules/support/__init__.py:6-7`) frames this as unblocking Copper waves B191–B193.

> [!IMPORTANT]
> **Surface summary (verified `b75c873`):**
> - **10 exposed MCP tools**, all registered in `nce/tool_registry.py:1030-1089`: `support_query_ticket`, `support_open_ticket`, `support_sla_clock`, `support_health_score`, `support_troubleshoot`, `support_resolve_ticket`, `support_triage_ticket`, `support_record_touchpoint`, `support_dispatch_work_order`, `support_sync_now`.
> - **11 mounted REST routes** in `nce/admin_app.py:994-1052` → `nce/admin_handlers/support.py`. One of them, `GET /api/support/sync/status`, has **no MCP-tool twin** — it's REST-only.
> - **15 internal `do_*` domain cores** across 9 files. **4 of them have no MCP tool and no REST route** — they are callable only by direct Python import from another engine, and today nothing in the codebase calls them (see §8, "cores with no door").
> - **Known drift, verified in code:** the customer health score's own module docstring claims it reduces over "ticket cadence, recency, SLA breaches, **frustration trend**, and **touchpoint responses**" (`health.py:6-8`), but `do_health_score` (`health.py:219-226`) hardcodes `frustration_score=0.0` and `touchpoint_avg=None` on every call — the only call site for `compute_health_score` in the module. Two of the five weighted health dimensions can **never** move the score away from zero contribution through any code path that exists today. See §3.3.
> - Every namespace must opt in via `metadata.support.enabled = true` before any tool or route works (`_guard.py`) — see the admin guide §1 for details.

---

## 1. Ticket Lifecycle

### 1.1 `support_open_ticket` — open a ticket
Actor; **mutation, admin_only** (`nce/tool_registry.py:1036-1041`, `mcp_handlers.py:122-130`). Opens a native `service_tickets` row and (by default) initializes a running SLA clock in the same transaction (`tickets.py:159-302`).

- **Required:** `namespace_id`, `summary` (non-blank).
- **Optional:** `priority` (`low|medium|high|critical`, default `medium`), `description`, `source` (`nce|d365`, default `nce`), `source_id`, `asset_id`, `room_id`, `customer_id`, `sla_profile` (default `standard`), `change_origin` (default `agent`; allowed set below), `ai_diagnosis` (dict), `create_sla_clock` (bool, default `True`), `id`/`ticket_id` (for deterministic replay).
- **Allowed `change_origin` values** (`tickets.py:32-44`): `sync`, `webhook`, `agent`, `operator`, `consolidation`, `replay`, `proactive_telemetry`, `proactive_health`, `unknown`.
- **Response:** `{"ok": true, "ticket": {...}, "sla_clock": {...} | null}`.

### 1.2 `support_query_ticket` — read one ticket, or list
Watcher; **read-only, cacheable** (`tool_registry.py:1030-1035`). Two modes in one tool (`tickets.py:305-417`):
- Pass `ticket_id` (or `id`) → single ticket + its SLA clock. Raises a `ticket_not_found` refusal (MCP `-32005`) if the ticket doesn't exist in this namespace.
- Omit it → filtered list. Optional filters: `status`, `priority`, `customer_id`, `room_id`, `asset_id`. `limit` (default 50, max 200), `offset` (default 0). Response includes `total` from a window-function count, so pagination doesn't need a second query.

### 1.3 `support_resolve_ticket` — close a ticket
Actor; **mutation, admin_only** (`mcp_handlers.py:181-227`). Requires `namespace_id`, `ticket_id`, `resolution_text` (non-blank). Optional: `was_fix` (bool, default `True`), `resolution_category`, `fixed_asset_id`, `fixed_product_id`, `resolved_by`.

Refuses (via structured `McpError` data) when: the ticket doesn't exist, is already `resolved`, or is `closed`/`cancelled` (`InvalidTicketStatusError`). On success it: flips status to `resolved`, appends a `ticket_resolved` event, touches the SLA clock's `updated_at`, and **appends the resolution as a fact to `v3_cognitive_ledger`** (`tickets.py:534-571`) — this is what feeds the AI Troubleshooter in §4. Response: `{"ok": true, "ticket": {...}, "ledger_id": "...", "status": "resolved"}`.

**Autonomous close guard** (Charter §6): pass `autonomous: true` to attempt an unattended close. The code compares `confidence` (your supplied value) against a threshold that is `max(config_default, caller_supplied)` — i.e. **a caller can only tighten the bar, never lower it below `NCE_SUPPORT_AUTONOMY_AUTOCLOSE_CONFIDENCE` (default `0.95`, `nce/config.py:1302-1304`)**. Below threshold, the call raises `autoclose_confidence_refusal` rather than closing (`tickets.py:448-464`).

---

## 2. SLA Clock — `support_sla_clock`

Watcher; **read-only, cacheable** (`tool_registry.py:1042-1047`). Requires `namespace_id`, `ticket_id`. Implemented in `sla.py`.

- **Lazy seeding:** if no `sla_clocks` row exists yet for the ticket, one is created on first read from `calculate_sla_targets(sla_profile, priority, created_at)` (`sla.py:240-265`) — you don't have to call `support_open_ticket` with `create_sla_clock: true` to get a clock later.
- **Targets come from `nce/config_data/support-sla-profiles.json`**, four profiles (`mission_critical`, `standard`, `basic`, `best_effort`) × four priorities, each with `first_response_hours` / `resolution_hours`. If the file is missing, an identical hardcoded fallback table is used (`sla.py:30-55`) — so behavior is the same either way in this snapshot.
- **Countdown math** (`evaluate_sla_status`, `sla.py:93-193`): deducts any paused time from `paused_intervals` before comparing to `now`; computes `breach_type` (`first_response`, `resolution`, or `both`); and flags `breach_risk = true` when a clock is not yet breached but has ≤30 min left on first response or ≤1 hour left on resolution.
- Every read that changes the breach flag also **writes it back** (`sla.py:280-298`) — reads are not side-effect-free once a clock crosses a threshold.
- **Response** includes `sla_profile`, `first_response_due`, `resolution_due`, `first_response_at`, `resolved_at`, plus every key from `evaluate_sla_status` (`breached`, `breach_type`, `breach_risk`, `is_paused`, `paused_seconds`, `remaining_first_response_seconds`, `remaining_resolution_seconds`, `effective_first_response_due`, `effective_resolution_due`).

---

## 3. Customer Health & Touchpoints

### 3.1 `support_health_score` — compute/refresh a customer's health
Watcher; **read-only, cacheable** (`tool_registry.py:1048-1053`), though it does upsert `customer_health` as a side effect (`health.py:145-257`). Requires `namespace_id`, `customer_id`. Optional `lookback_days` (default 90, from `support-health-weights.json`'s `default_lookback_days`).

Score starts at 100.0 and is docked by weighted signals (`compute_health_score`, `health.py:60-142`):

| Signal | Weight (config) | What actually drives it today |
|---|---|---|
| Ticket cadence | 0.25 | Real — count of tickets in the lookback window |
| Recency | 0.20 | Real — tickets opened in the last 7 days |
| SLA breaches | 0.25 | Real — count of breached `sla_clocks` rows |
| Frustration trend | 0.20 | **Always 0.0** — never contributes (§3.3) |
| Touchpoint feedback | 0.10 | **Always inert** (`touchpoint_avg=None`) — never contributes (§3.3) |

Response is the upserted `customer_health` row: `customer_id`, `score`, `trend` (`{"direction": "improving"|"stable"|"degrading", "delta": float}`), `churn_risk` (`low` <50 dock ⇒ actually: `score < 50` → `high`, `< 75` → `medium`, else `low`), `drivers` (list of human-readable strings), `last_touchpoint_at`, `computed_at`.

### 3.2 `support_record_touchpoint` — record an ÉT-spørsmål response
Actor; **mutation** (not `admin_only` — the only mutating support tool without that flag, `tool_registry.py:1072-1077`). Requires `namespace_id`, `customer_id`, `answer`. Optional `question_id` (default `et_sporsmal_v1`), `score`.

What it actually does (`health.py:260-334`): appends a `touchpoint_response` fact to `v3_cognitive_ledger`, then calls `do_health_score` again to refresh the customer's rolling score. **What it does not do:** feed your `answer`/`score` into that recomputation — see §3.3 for why the response's `score` field ends up unused by the health engine, and why `last_touchpoint_at` on the customer record doesn't move either.

### 3.3 Drift you should know about before trusting the health score
Two things the code demonstrably does not do, verified at `health.py:219-226`:
1. **`frustration_score` is hardcoded to `0.0`** in the one place `compute_health_score` is called from `do_health_score`. The "Elevated emotional frustration detected" driver string (`health.py:99-100`) can never appear.
2. **`touchpoint_avg` is hardcoded to `None`** in that same call. The touchpoint weight in `support-health-weights.json` (10%) is real config that is silently never applied — a customer who gives five glowing touchpoint answers in a row sees **no change** in health score from that fact.
3. **`do_record_touchpoint` doesn't advance `last_touchpoint_at`.** `do_health_score`'s upsert reads the *existing* `last_touchpoint_at` from the DB and writes it straight back (`COALESCE(EXCLUDED.last_touchpoint_at, customer_health.last_touchpoint_at)` over an unchanged value, `health.py:216,243`) — nothing in this module ever sets it to "now." A customer's `last_touchpoint_at` stays at whatever it was on first health-score computation (typically `NULL`), regardless of how many touchpoints you record.

None of this raises an error — the tools return `{"ok": true, ...}` normally. If you're using health score or churn risk to drive an escalation decision, know that today it is effectively a 3-signal score (cadence, recency, SLA breaches), not the 5-signal one its own docstring describes.

---

## 4. AI Troubleshooter — `support_troubleshoot`

Watcher; **read-only, cacheable** (`tool_registry.py:1054-1059`). Requires `namespace_id`, and either `symptom_text` or a `ticket_id` (whose `summary`/`description` is used as the symptom text if you don't supply your own). Optional `asset_id` (for a match bonus), `limit` (default 5), `min_confidence` (default 0.5).

This is **cognitive recall, not a knowledge base** — it only ever surfaces past tickets that were resolved through `support_resolve_ticket` with `was_fix: true` (`troubleshoot.py:128-151`), by token-overlap similarity against your symptom text plus an asset-match bonus:
- Asset match + token similarity > 0.1 → confidence up to 0.98.
- Token similarity ≥ 0.20 (no asset match) → confidence up to 0.92.
- Token similarity > 0.05 → confidence up to 0.85.
- Asset match alone, no meaningful text overlap → flat 0.65.

**Honest zero-history fallback** (Charter §2.1): if nothing clears `min_confidence`, the tool returns `diagnosis: "No matching past resolution patterns found..."`, `confidence: 0.0`, `cited_ticket_ids: []` — it does not fabricate a plausible-sounding fix. Every proposed fix cites the historical `ticket_id`(s) it came from, so results are auditable back to a real prior resolution.

---

## 5. Triage — `support_triage_ticket`

Advisor; **read-only, cacheable** (its own docstring in `mcp_handlers.py:230-234` calls it "Advisor" — this is the only support tool that labels itself that way, distinct from "Watcher"). Requires `namespace_id`, `ticket_id`.

Pure keyword matching over the ticket's `summary`/`description`/`room_id` (`triage.py`), no ML: room/text keyword sets decide `recommended_priority` and `urgency` (e.g. an "executive"/"boardroom" room combined with an "offline"/"down" keyword ⇒ `critical`); another keyword set (`mic`, `dante`, `nvx`, `vlan`, etc.) picks a `suggested_skill` (`audio_specialist`, `video_specialist`, `systems_engineer`, or the `field_technician` default); a physical-fault keyword set (`cable`, `loose`, `mount`, ...) sets `suggested_route: tier_2_field_dispatch` and flags `auto_dispatch_candidate: true` when combined with `high`/`critical` priority. This is advisory only — triage never writes anything back to the ticket; it just returns a recommendation for you (or an autonomous caller) to act on, e.g. by calling `support_dispatch_work_order`.

---

## 6. Dispatch to Field Tech — `support_dispatch_work_order`

Actor (**Autonomous under threshold**); **mutation, admin_only** (`mcp_handlers.py:259-295`, `tool_registry.py:1078-1083`). Requires `namespace_id`, `ticket_id`. Optional `estimated_cost`/`cost`, `dispatch_ceiling` (can only tighten, never raise, the configured ceiling), `confirm` (bool), `notes`.

This writes a **boundary edge only** — `TICKET:{id} -[dispatched_as]-> WORK_ORDER:{derived_id}` in `kg_edges` (`dispatch.py:196-212`). Support asserts ownership of `TICKET` before writing (Contract A) but never creates or touches a `WORK_ORDER` node itself; Field Tech (Module 12) owns that node type (`node-ownership.json:68-69`).

**Cost-ceiling guard** (`dispatch.py:103-131`): an **absent** `estimated_cost` fails closed — you must also pass `confirm: true`, or the call raises `dispatch_ceiling_exceeded` with `estimated_cost: Infinity`. An explicit `0.0` is treated as genuinely zero-cost and proceeds without confirmation. Above the ceiling (`NCE_SUPPORT_AUTONOMY_DISPATCH_CEILING`, default `0.0` — i.e. every dispatch needs human confirm unless the operator raises the ceiling), the same refusal fires unless `confirm: true`.

**Idempotent by design:** the work-order ID is derived deterministically as `uuid5(namespace_id + ":" + ticket_id + ":work_order")` (`dispatch.py:65-68`), and a repeat call for an already-dispatched ticket returns the same `work_order_id` with `idempotent_replay: true` rather than creating a duplicate edge.

Ticket must be `open`/`in_progress`/etc. — dispatching a `resolved`/`closed`/`cancelled` ticket raises `invalid_ticket_status`.

---

## 7. D365 Sync — `support_sync_now`

Actor / operator; **mutation, admin_only** (`mcp_handlers.py:298-306`, `tool_registry.py:1084-1089`). Requires `namespace_id`. Optional `mode` (`d365|both|nce`, default `both`), `entity_types` (default `["incidents", "incidentresolution", "annotations"]`), `run_proactive_sweep` (bool, default `True`).

- For `mode=d365`/`both`, it delegates the actual case sync to the shared Dynamics 365 vertical's `handle_d365_sync_now` (`sync.py:56-79`) — this part is real. If the D365 adapter isn't reachable/configured, the call doesn't fail; it returns `d365_sync: {"status": "unavailable", ...}` inside an otherwise-`ok: true` envelope.
- **The "proactive sweep" is a count, not a sweep.** Despite the module docstring's language ("conducts a proactive telemetry sweep"), the code only runs `SELECT count(*) FROM service_tickets WHERE status = 'open'` and returns that as `active_tickets_checked` (`sync.py:87-102`). It does not read any asset telemetry, and it does not open, escalate, or touch any ticket. If you're relying on `support_sync_now` to actually open new proactive tickets from degraded assets, it doesn't — see §8's `do_open_proactive_telemetry_ticket`, which is the function that would do that, and which nothing currently calls.

There is also a REST-only `GET /api/support/sync/status` with no MCP-tool equivalent — see the admin guide §5 for why you shouldn't trust its output as a real health check.

---

## 8. Cross-engine cores with no door (yet)

Four `do_*` functions in this module are fully implemented, exported from the package (`nce/vertical_modules/support/__init__.py:53-84` lists all four in `__all__`), and have **zero MCP tool and zero REST route** — confirmed by grepping the whole `nce/` tree for callers outside `nce/vertical_modules/support/` itself. You cannot reach them today as an MCP client or REST caller; they exist only to be called by other engines' Python code, and none currently do:

| Core | File | Intended purpose |
|---|---|---|
| `do_record_failure_pattern` | `ecosystem.py:42-141` | Writes `TICKET -[failure_pattern]-> PRODUCT_SKU` for Product(2)'s BOM-optimization feedback loop |
| `do_record_upsell_signal` | `ecosystem.py:144-246` | Writes `TICKET -[upsell_opportunity]-> QUOTE|OPPORTUNITY` for Sales(5) |
| `do_support_at_risk_aggregate` (alias `get_support_morning_brief_slice`) | `ecosystem.py:249-352` | At-risk SLA clocks + churn-risk customers + open proactive tickets, meant to feed Module 16's Executive Morning Brief |
| `do_open_proactive_telemetry_ticket` | `proactive.py:35-165` | Opens a ticket automatically from Assets(9) telemetry degradation signals, with `TICKET -[about]-> ASSET` |

If you need one of these today, you must call it directly from Python against the shared pool/engine — there is no tool name or HTTP path to hit. See the admin guide §6 for a schema-level issue that would immediately bite `do_open_proactive_telemetry_ticket` the moment something does wire it up.

---

## 9. Worked Example — open, triage, and resolve a ticket

```python
from nce.vertical_modules.support.tickets import do_open_ticket, do_resolve_ticket
from nce.vertical_modules.support.triage import do_triage_ticket
from nce.vertical_modules.support.sla import do_sla_clock

ns = "11111111-1111-1111-1111-111111111111"

opened = await do_open_ticket(pool, {
    "namespace_id": ns,
    "summary": "Boardroom DSP offline, no audio in room",
    "priority": "high",
    "room_id": "boardroom-3",
    "customer_id": "cust-042",
    "sla_profile": "mission_critical",
})
ticket_id = opened["ticket"]["id"]

triage = await do_triage_ticket(pool, {"namespace_id": ns, "ticket_id": ticket_id})
# triage["recommended_priority"] == "critical"  (boardroom + "offline" keyword match)
# triage["suggested_skill"] == "audio_specialist"  ("dsp" keyword match)

clock = await do_sla_clock(pool, {"namespace_id": ns, "ticket_id": ticket_id})
# clock["resolution_due"] reflects the mission_critical/high profile: 4.0 hours out

resolved = await do_resolve_ticket(pool, {
    "namespace_id": ns,
    "ticket_id": ticket_id,
    "resolution_text": "Re-seated DSP network cable; confirmed Dante clock re-sync.",
    "resolution_category": "hardware",
    "was_fix": True,
})
# resolved["status"] == "resolved"; a resolution fact is now in v3_cognitive_ledger
# for support_troubleshoot to recall on a future "DSP offline" symptom.
```

---

## Appendix: Tool Reference (`nce/tool_registry.py:1030-1089`)

| Tool | cacheable | admin_only | mutation | Role (per `mcp_handlers.py` docstrings) |
|---|:---:|:---:|:---:|---|
| `support_query_ticket` | Y | N | N | Watcher |
| `support_open_ticket` | N | Y | Y | Actor |
| `support_sla_clock` | Y | N | N | Watcher |
| `support_health_score` | Y | N | N | Watcher |
| `support_troubleshoot` | Y | N | N | Watcher |
| `support_resolve_ticket` | N | Y | Y | Actor |
| `support_triage_ticket` | Y | N | N | Advisor |
| `support_record_touchpoint` | N | N | Y | Actor |
| `support_dispatch_work_order` | N | Y | Y | Actor (Autonomous under threshold) |
| `support_sync_now` | N | Y | Y | Actor / operator |
