> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 10 — Support Engine  (nce/vertical_modules/support)

**Status:** spec (Tier 3 — Operations axis) · **Owner:** NCE core (Sindre)
**Pattern companions:** `docs/VERTICAL_MODULE_PATTERN.md`, `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§4 graph catalogue, §7 spec format), `docs/DATA_SOURCE_MODES.md` (the `d365|both|nce` per-function switch)

## Mission
Make the tenant's promise real: **"Kunden skal glemme at leverandøren eksisterer — fordi alt bare virker."** Support is not a complaint queue; it is the engine that keeps assets healthy, catches dissatisfaction *before* churn, and dispatches a fix before the customer notices. NCE has **already shipped D365 case ingestion** — `d365_query_case`, `d365_case_stress_report`, `d365_list_sla_breaches` exist and write case notes + the Empathic Tensor frustration trend into `memories`/`v3_cognitive_ledger`. The Support engine **generalises that proven slice** into a first-class, source-agnostic capability: a native `ServiceTicket` with D365 as **one** adapter behind the same `d365|both|nce` switch Sales uses (eventually native, no migration at flip). The deep-AI angle is the **AI Troubleshooter**: given a ticket about an ASSET, recall similar past tickets and their *resolutions* from the cognitive ledger and propose a diagnosis + fix from auditable memory — deep cognitive recall is the headline. Folds in Andreas's module 15 (Customer Satisfaction): a passive-signal **customer health score** that detects churn risk before churn, "omsorg ikke overvåking", ÉT-spørsmål touchpoints.

## Inspiration & triage
- **Andreas sources:**
  - Module **12 Operations/Drift & Service** (`handoff/04-virksomhets-modulkart.md`) — the recurring-revenue thesis: asset-register seeded from BOM at handover, proactive monitoring via manufacturer APIs, **driftsavtaler follow ROM, not customer** (SLA per room).
  - Module **15 Customer Satisfaction** — ÉT-spørsmål touchpoints, passive-signal detection, customer health score, recovery-workflow (Nordic, not survey-bombing). Folds in here, not a separate engine.
  - `handoff/02 §9 ServiceTicket/SLA` — `app/api/service-tickets/route.ts` is the 🟢 real backend (SLA-clock, AI-diagnosis fields, events); the `/service/tickets` page is a 🟡 34-line stub; `healthScore` is a 🟡 passive field with no write engine (we build the engine). `service-review` analyzer (`lib/pitch/service-review`) is the deterministic GL-backed health read to generalise.
- **Already-shipped NCE seed (the real foundation):** the `dynamics365` vertical's case track — `ingestion.py:DataverseIngestionWorker.ingest_case_note / ingest_activity / ingest_sla_breach` (case text → embeddings + memories + Empathic Tensor + WORM event_log), and `mcp_handlers.py:handle_d365_query_case / handle_d365_case_stress_report / handle_d365_list_sla_breaches`. **Support wraps these as the `d365` source adapter** rather than rebuilding ingestion.
- **Lysning page served:** the service/tickets + customer-health surfaces (today the 34-line stub) — consume the no-model REST surface.

## Classification
**push + semantic.** External systems: **Dynamics 365 / Dataverse** (incidents/annotations/activities) as the *first* source adapter — reuses the existing OData v4 + OAuth (`DataverseTokenManager`, Redis-cached) path; **Assets(9) telemetry** as a second, internal source (a health drop opens a proactive ticket). The native source (eventually system-of-record) writes the same `ServiceTicket` shape with no external dependency. Source is chosen **per-function** via the `d365|both|nce` switch (`DATA_SOURCE_MODES.md`): `d365` = read live from Dataverse, `both` = NCE + D365 reconciled, `nce` = native only. Incremental from D365 via the watermark/delta pattern (`d365_sync_runs`); NCE **retains** the ingested case data so the flip to `nce` needs no migration. Semantic track: ticket text → `memories` (embedding + `content_fts`) + Empathic Tensor → `v3_cognitive_ledger` (reuses the shipped `DataverseIngestionWorker`).

## Graph contribution
Node `entity_type` prefixes: `SUPPORT_*`, plus shared spine nodes `TICKET`, `SLA`, `ASSET`, `WORK_ORDER`, `PRODUCT`, `CUSTOMER`, `FUNCTIONAL_LOCATION`/`ROOM`.
- **Nodes:** `TICKET` (native ServiceTicket; status, SLA clock, AI-diagnosis fields, event timeline), `SLA` (a driftsavtale SLA profile — first-response + resolution targets), `SUPPORT_HEALTH_SCORE` (per-customer rolling score node), `SUPPORT_DIAGNOSIS` (a Troubleshooter proposal).
- **Edges (the §4 contract, our slice):**
  - `TICKET -[about]-> ASSET -[lives_in]-> ROOM -[covered_by]-> SLA` (the room-centric chain — SLA hangs off FUNCTIONAL_LOCATION, **shared with Assets(9) + Agreements(3)**, never off CUSTOMER).
  - `TICKET -[covered_by]-> SLA -[on]-> FUNCTIONAL_LOCATION` (which clock/targets apply, resolved via the room not the customer).
  - `TICKET -[failure_pattern]-> PRODUCT` — **the "silence" Andreas flags closed**: repeated failures on a SKU flow *back* to Product(2) for better BOMs and to Sales(5) as upsell signal.
  - `TICKET -[dispatched_as]-> WORK_ORDER` (boundary edge written by Support, consumed by Field Tech(12)).
  - `ASSET -[monitored_by]-> TELEMETRY` (read from Assets(9); a degradation crossing threshold authors a proactive `TICKET -[about]-> ASSET`).
  - `CUSTOMER -[has_health]-> SUPPORT_HEALTH_SCORE` (passive-signal + touchpoint derived).
- **memories/ledger:** ticket/note/activity text → `memories` (embedding + `content_fts`) for Troubleshooter recall; every resolution + every diagnosis decision + the Empathic Tensor frustration reading → `v3_cognitive_ledger` (this is where "what similar ticket did we resolve, and how" lives — generalises the shipped D365 case-note tensor track). Tag every derived row with `support_source_id` for hard-retirement on delete (D365 retirement pattern).

## Core functions
Pure-ish `do_<action>(engine, params) -> dict`; the SLA-clock and health-score cores are deterministic (config-driven), the Troubleshooter is recall-backed.
- `do_query_ticket(engine, params) -> dict` — `{ticket_id, source?}` → native ServiceTicket + graph context. When `source=d365|both` delegates to the shipped `handle_d365_query_case` and normalises into the ServiceTicket shape; `nce` reads the native row.
- `do_open_ticket(engine, params) -> dict` — `{asset_id|room_id, summary, origin}` → create TICKET node + start SLA clock (resolved via room → SLA). `origin ∈ {customer, proactive_telemetry, proactive_health}`. Actor.
- `do_sla_clock(engine, params) -> dict` — `{ticket_id}` → `{first_response_due, resolution_due, elapsed, breach_risk}`; computes remaining time against the room's SLA profile. Pure over config + ticket events.
- `do_troubleshoot(engine, params) -> dict` — **the headline.** `{ticket_id|{asset_id, symptom_text}}` → recall top-N similar past tickets (embedding over `memories`, filtered to same PRODUCT/ASSET family) **with their recorded resolutions from `v3_cognitive_ledger`**, then propose `{diagnosis, proposed_fix, confidence, cited_ticket_ids}`. Advisor. Writes a `SUPPORT_DIAGNOSIS` node citing its sources (auditable).
- `do_triage_ticket(engine, params) -> dict` — `{ticket_id}` → priority + suggested route/owner (skill + room + history). Advisor.
- `do_health_score(engine, params) -> dict` — `{customer_id, lookback_days}` → rolling score from passive signals (ticket frequency/recency, SLA-breach history, Empathic Tensor frustration trend — reuses the `case_stress_report` burnout signal) + touchpoint responses. Returns `{score, trend, churn_risk, drivers[]}`.
- `do_record_touchpoint(engine, params) -> dict` — `{customer_id, question_id, answer}` → store an ÉT-spørsmål response, fold into health. ("omsorg ikke overvåking" — one question at a natural touchpoint.)
- `do_dispatch_work_order(engine, params) -> dict` — `{ticket_id, ...}` → write the `TICKET -[dispatched_as]-> WORK_ORDER` boundary edge for Field Tech(12). Actor (Autonomous under threshold).
- `do_resolve_ticket(engine, params) -> dict` — `{ticket_id, resolution_text, was_fix}` → close ticket, **append the resolution to `v3_cognitive_ledger`** so it feeds future Troubleshooter recall. Actor.
- `do_sync_now(engine, params) -> dict` — incremental D365 case pull (delegates to the shipped `handle_d365_sync_now` for the `d365`/`both` modes) + telemetry-derived proactive sweep.

## MCP tools
Registered in `nce/tool_registry.py` via `_h(...)` late-binding. AI-role tag per roadmap §2 taxonomy.

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `support_query_ticket` | ✔ | ✘ | ✘ | Advisor |
| `support_sla_clock` | ✔ | ✘ | ✘ | Watcher |
| `support_troubleshoot` | ✔ | ✘ | ✘ | Advisor |
| `support_triage_ticket` | ✔ | ✘ | ✘ | Advisor |
| `support_health_score` | ✔ | ✘ | ✘ | Watcher |
| `support_open_ticket` | ✘ | ✔ | ✔ | Actor (Autonomous: proactive open) |
| `support_record_touchpoint` | ✘ | ✘ | ✔ | Actor |
| `support_dispatch_work_order` | ✘ | ✔ | ✔ | Actor (Autonomous under threshold) |
| `support_resolve_ticket` | ✘ | ✔ | ✔ | Actor (Autonomous: auto-close trivial) |
| `support_sync_now` | ✘ | ✔ | ✔ | — (operator) |

## REST routes
No-model path for the BFF (service/tickets + customer-health pages), cron, scripts. Mounted via `build_app(extra_routes=...)`; HMAC/mTLS-authed in `nce/admin_handlers/support.py`:
- `api_support_query_ticket` (GET) — ticket + graph context for the detail view.
- `api_support_sla_clock` (GET) — live SLA countdown for the queue board.
- `api_support_troubleshoot` (POST) — AI Troubleshooter recall + diagnosis for the agent UI.
- `api_support_health_score` (GET) — customer-health dashboard (score + trend + churn-risk drivers).
- `api_support_open_ticket` (POST) — manual/proactive ticket creation (still gated).
- `api_support_record_touchpoint` (POST) — ÉT-spørsmål capture from a portal touchpoint.
- `api_support_dispatch_work_order` (POST) — hand a ticket to Field Tech.
- `api_support_sync_status` / `api_support_sync_now` — D365 feed health + delta-run history (mirrors `d365_sync_status`).

## AI features
- **Watcher:** SLA-breach-risk detection (clock approaching `resolution_due` → alert before breach, not after); **device-health degradation** (telemetry from Assets(9) crosses threshold → flag); **churn-risk** from the health-score watcher (frustration trend + ticket cadence + SLA history) — catch dissatisfaction *before* churn.
- **Advisor:** the **AI Troubleshooter** — for a ticket about an ASSET, surface similar past tickets + their resolutions with plain-language diagnosis and a cited fix ("3 prior Crestron NVX dropouts on this firmware resolved by rolling back to 2.x — confidence 0.82"); ticket triage/routing by skill + room + history.
- **Actor:** open a **proactive ticket** on a health drop *with confirmation*; dispatch a work order to Field Tech *with confirmation*; resolve/close with the resolution captured to the ledger.
- **Autonomous (gated):** auto-close trivial tickets (sub-threshold confidence + known-pattern match); auto-dispatch a work order under a value/risk ceiling; auto-open a proactive ticket when a telemetry degradation is unambiguous — all governed by `NCE_SUPPORT_AUTONOMY_*` gates.
- **Cognitive recall:** the Troubleshooter reads resolutions from `v3_cognitive_ledger`, so an agent can query *why* a fix was proposed and which past tickets back it. The frustration/burnout signal is the shipped `case_stress_report` Empathic Tensor trend, generalised per customer.
- **Enrichment triggers (event-scoped, never a background sweep):** AI runs the Troubleshooter *only* when a ticket is opened or a diagnosis is requested; recomputes health *only* on a new ticket, SLA event, or touchpoint response. Never bulk-rescore all customers/tickets.

## A2A flows
- **Consumes Assets(9):** reads `ASSET -[monitored_by]-> TELEMETRY`; a health drop triggers `support_open_ticket` (proactive). Serves the **Install→Asset→Cover** flow's tail — Field Tech work-order completion → Assets lifecycle enrich → **Support SLA activation** (the room's SLA clock starts when the asset goes live).
- **Dispatches to Field Tech(12):** emits `TICKET -[dispatched_as]-> WORK_ORDER` so a Field Tech agent picks it up (`WORK_ORDER -[assigned_to]-> EMPLOYEE` is theirs).
- **Initiates failure-pattern feedback (closes "the silence"):** repeated SKU failures emit `failure_pattern` toward **Product(2)** (better BOMs) and an **upsell signal to Sales(5)**.
- **Feeds Morning-brief (#19 aggregate):** exposes the "drift gråter" signal — at-breach-risk SLAs + churn-risk customers + open proactive tickets — as the operations slice of the cross-engine "1 risk + 1 opportunity" query (the cross-module collision Andreas's module 19 wants to surface).

## Config keys
`NCE_SUPPORT_*` in `nce/config.py`: `NCE_SUPPORT_ENABLED`, `NCE_SUPPORT_SOURCE_MODE` (`d365|both|nce`, per-function-overridable), `NCE_SUPPORT_SYNC_INTERVAL_MINUTES`, `NCE_SUPPORT_TROUBLESHOOT_RECALL_N` (similar-ticket recall depth, default 5), `NCE_SUPPORT_TROUBLESHOOT_MIN_CONFIDENCE` (Advisor surfacing floor), `NCE_SUPPORT_HEALTH_LOOKBACK_DAYS` (default 90), `NCE_SUPPORT_CHURN_RISK_THRESHOLD`, `NCE_SUPPORT_AUTONOMY_AUTOCLOSE_CONFIDENCE` (auto-close gate), `NCE_SUPPORT_AUTONOMY_DISPATCH_CEILING` (auto-dispatch value/risk gate). Reuses the shipped `NCE_D365_*` (org URL, OAuth) for the D365 adapter and `NCE_NETBOX_*`/Assets telemetry config for the proactive sweep. Namespaces opt in via `metadata.support.enabled = true`.
**Config-as-IP JSON (namespace-scoped, the business IP — NOT code):**
- `support-sla-profiles.json` — per-FUNCTIONAL_LOCATION SLA targets (first-response + resolution by priority tier); the driftsavtale terms, tuned per tenant. SLA follows the room, never the customer.
- `support-health-weights.json` — passive-signal weights (ticket cadence, recency, SLA-breach history, frustration-trend, touchpoint response) and churn-risk thresholds.

## Tables/migrations
**Graph-first** for TICKET/SLA/HEALTH/DIAGNOSIS nodes + edges; resolutions/diagnoses/frustration in `v3_cognitive_ledger`; case text in `memories` (reusing the shipped D365 ingestion). Own tables only where a keyed, high-write-rate lookup beats the graph — all `ENABLE` + `FORCE ROW LEVEL SECURITY` + `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`, mirrored into `schema.sql` + a numbered migration:
- `service_tickets` (native ServiceTicket: `id, source, source_id, asset_id, room_id, customer_id, status, priority, summary, sla_profile, first_response_at, resolved_at, ai_diagnosis jsonb, events jsonb, support_source_id, synced_at`) — the source-of-record row for the `nce` mode + the normalised mirror for `d365|both`.
- `sla_clocks` (per-ticket clock state: `ticket_id, sla_profile, first_response_due, resolution_due, breached bool, breach_type, paused_intervals jsonb`) — fast countdown reads for the queue board.
- `customer_health` (rolling score: `customer_id, score, trend jsonb, churn_risk, drivers jsonb, last_touchpoint_at, computed_at`) — fast dashboard reads; recomputed event-scoped.

## Dependencies
- **Upstream engines:** Assets(9) — telemetry + `ASSET -[lives_in]-> ROOM` for health monitoring and the room-centric SLA resolution (Support consumes, does not own assets); Agreements(3) — the driftsavtale terms that populate SLA profiles; Field Tech(12) — receives dispatched work orders (boundary: Support writes the `dispatched_as` edge, Field Tech owns the work-order lifecycle and `assigned_to`).
- **Downstream consumers:** Product(2) + Sales(5) consume the `failure_pattern`/upsell edges; #19 Morning-brief consumes the at-risk aggregate.
- **Already-shipped seed (no rebuild):** the `dynamics365` vertical's case ingestion + the three D365 case tools — Support wraps them as the `d365` adapter rather than reimplementing OData/OAuth/empathic-tensor ingestion.
- **External blocker 🔴:** real manufacturer device-telemetry adapters (Cisco xAPI, QSC Reflect, Neat Pulse, Huddly, Poly Lens) live in **Assets(9)** and are mock-with-swap today — proactive device-health ticketing is fully usable on the mock and flips to live with the env swap; no Support-side blocker.

## Review round-2 hardening (2026-06-17 — these govern the build)
1. **Support is the SECOND engine on `d365|both|nce` → that resolver is SHARED CORE INFRA, not per-engine (roadmap §9.6 item 6).** Support normalizes D365 cases → native `ServiceTicket`, retains for no-migration flip, reconciles in `both` — **structurally identical to Sales's source-mode resolver**, with the same two-master questions (D365-edited case vs NCE-edited ticket — who wins? does an NCE `do_resolve_ticket` write back to D365?). The resolver + divergence audit + write-routing is **one core service both Sales and Support call** (every future D365-sourced engine inherits it).
2. **The Troubleshooter recalls cases but may not recall FIXES — and the fix is the value (structured-resolution attribution).** D365 ingestion brings notes + frustration tensor, but a *resolution* is usually buried in free text / a close-code, not a structured fact. So at launch `do_troubleshoot` surfaces **similar tickets without reliable fix attribution.** Prioritize the **resolution-capture discipline** early (`do_resolve_ticket` writes *which asset/product/firmware fixed it* as structured ledger facts). Honest ramp: **similarity-recall day one, fix-recall as structured resolutions accumulate** (same as Project's structured outcomes).
3. **Auto-close is the sharpest conservative-posture instance — anti-mission if wrong (roadmap §9.5).** A wrongly auto-closed "trivial known-pattern" ticket **suppresses the exact dissatisfaction signal the engine exists to catch = a silent churn driver.** Auto-dispatch sends a real tech to a customer site (cost + customer-facing). Both carry the Contract-B gate (idempotency + kill-switch); **auto-close stays human-confirmed the LONGEST of any tool in the suite.**
4. **The customer-health/churn score is sparse-at-launch AND customer-sensitive.** Fuses ticket cadence + SLA history + frustration tensor + touchpoints — sparse early (expose coverage, don't fake confidence). Critically it is a **churn-prediction score: it must NEVER surface to the customer**, and the recovery workflow must honor *"omsorg, ikke overvåking"* as a **hard guardrail**. Automated outreach off a churn score is an **autonomous customer-facing action → Contract B** (§9.5).
5. **`SLA` confirmed 4-way (roadmap §9.1):** Agreements = terms, Assets = per-ROOM coverage, Economy = MRR, **Support = the running clock + breach state.** The clock is legitimately Support's operational runtime — "confirm the decomposition," not a conflict.
6. **Grace-degradation — reactive core ships standalone; proactive needs Assets (same tier).** `TICKET -[about]-> ASSET` + proactive telemetry tickets depend on Assets being live. **Support's reactive core ships on the shipped D365 seed alone;** proactive/asset-health features layer on when Assets exists (B-phases already sequence this — making the line explicit).

## Build phases
- **B1 — Native ServiceTicket + D365 adapter:** `service_tickets`/`sla_clocks` tables (RLS), `do_query_ticket`/`do_open_ticket`/`do_sla_clock`, the `d365|both|nce` source switch wrapping the shipped `handle_d365_query_case`/`handle_d365_sync_now`. MCP tools + REST routes for the three. Graph upserts (TICKET/SLA edges, `support_source_id`).
- **B2 — AI Troubleshooter (the headline):** `do_troubleshoot` — embedding recall over `memories` + resolution recall from `v3_cognitive_ledger`, `SUPPORT_DIAGNOSIS` node with cited sources; `do_resolve_ticket` writes resolutions back to the ledger to close the recall loop. `do_triage_ticket`. `support-sla-profiles.json` wired.
- **B3 — Customer health + churn watcher:** `customer_health` table, `do_health_score`/`do_record_touchpoint`, passive-signal + ÉT-spørsmål touchpoints, churn-risk watcher (generalises the `case_stress_report` frustration trend). `support-health-weights.json` wired.
- **B4 — Proactive + dispatch:** Assets(9) telemetry consumption → proactive `do_open_ticket` on health drop; `do_dispatch_work_order` boundary edge to Field Tech(12). Autonomy gates (`AUTOCLOSE_CONFIDENCE`, `DISPATCH_CEILING`).
- **B5 — Close the silence + brief:** `failure_pattern` edges → Product(2) + upsell → Sales(5); expose the "drift gråter" at-risk aggregate to the #19 Morning-brief A2A query.
