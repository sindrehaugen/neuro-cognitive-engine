> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 04 — Vendors & Contractors Engine  (nce/vertical_modules/vendors)

<!-- BLOCKED ON OQ-2 / OQ-4: SPEC PROPOSAL VOICE. This document is an architectural design specification. At baseline 7304330, Vendors ships 10 registered MCP tools and 2 mounted REST routes (/api/vendors/scorecard, /api/vendors/{id}). Partner Access Model and contractor principal scoping are enforced. Refer to docs/engines/vendors-user.md and docs/engines/vendors-admin.md for shipped reality. Verified-against: 7304330 -->


**Status:** spec (Tier 2 — Operations axis) · **Owner:** NCE core (Sindre)
**Pattern companions:** `docs/VERTICAL_MODULE_PATTERN.md`, `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§4 graph catalogue, §7 spec format)

## Mission
Be the **master-data + reliability engine** for the two counterparty classes every operations flow touches: **VENDORs** (distributors/manufacturers we buy from) and **CONTRACTORs** (external techs/freelancers we dispatch). It owns the canonical *identity* of each counterparty and the *reliability/performance* signal layer that hangs off it — supplier scorecards (on-time %, defect/RMA rate, substitution rate, reliability), kickback-tier membership + ytd-progress, and contractor profiles (rates, certs, availability, performance score). The deep-AI angle is twofold: (1) the scorecards are **derived from outcomes other engines emit** (PO 3-way-match results from Procurement, work-order ratings from Field Tech) via the cognitive ledger, so the engine answers *"how did this vendor/contractor perform on similar jobs"* from auditable memory rather than a static field; (2) the **Partner Access Model** — a strict, multi-layer restricted-access design for external contractors that NCE enforces structurally (RLS + A2A tool-scoping + REST field-redaction), not by convention. This is the registry the rest of the operations spine references; it is deliberately thin and authoritative.

## Inspiration & triage
- **the planning sources (module map `04-virksomhets-modulkart.md`):**
  - **Module 05 — Procurement:** supplier **scorecards**, **kickback-tier** management, leverandør-reliabilitet as a TCO input. The *terms* of the kickback agreement are Agreements(3) IP; what lives here is the vendor's *current tier + ytd-progress toward the next tier* as a reliability/relationship attribute that Procurement's step-5 scoring reads.
  - **Module 11 — External Techs:** the elastic-capacity model (5–15 variable freelancers, same physical job as internal techs) and the **strictly restricted data access** ("ser egne work orders + relevante BOM-linjer, ALDRI margin/pris/strategi"). The "Partner Access Model" was designed in the field-service skill; this engine is where it becomes structural.
- **Portal sidecars to lift:** supplier master-data **rows already arriving via the Product/Procurement feeds** (`backend/integrations/nettailer_client.py` supplier/supplierprices rows, `backend/steps_product/bidprices.py` `leverandor` column). Vendors does **not** re-pull these — it **subscribes** to the canonical VENDOR upserts those feeds already produce and enriches them with the reliability layer. Contractor data is entered via the admin surface (no external feed).
- **Lysning page(s) served:** indirectly — supplier scorecards surface on `Bestillinger.jsx` (procurement) sourcing rationale; contractor matching + restricted partner views feed the Field Tech dispatch surface.

## Classification
**pull / master-data (registry).** No heavy external transport of its own — it is the registry other engines reference, so it has **no `client.py`/`webhooks.py`**. Two inbound data paths instead: (a) VENDOR master-data **ingested from the Product/Procurement Nettailer feeds** (those engines own the pull; Vendors enriches the resulting `VENDOR` nodes); (b) **contractor master-data entered via admin REST** (`api_vendors_upsert_contractor`). Scorecard/performance attributes are **derived**, updated from outcome events (Procurement match results, Field Tech work-order ratings) consumed off the cognitive ledger — no external auth, no OAuth. The only resilience surface is internal DB + ledger reads.

## Graph contribution
Node `entity_type` prefixes: `VENDORS_*` for engine-owned reliability nodes; shared spine nodes `VENDOR`, `CONTRACTOR` (new spine node this engine introduces), plus references to `SKU`, `PO`, `WORK_ORDER`, `EMPLOYEE`, `AGREEMENT`.
- **Nodes:** `VENDOR` (canonical supplier — orgnr, contacts; the identity record), `CONTRACTOR` (external tech/freelancer — profile, rates, availability), `VENDORS_SCORECARD` (a derived reliability snapshot for a vendor), `VENDORS_CERT` (a contractor certification w/ expiry), `VENDORS_PERF` (a derived contractor performance snapshot).
- **Edges (the §4 contract, our slice):**
  - `PO -[from]-> VENDOR` — shared with Procurement (Procurement writes the PO; Vendors owns the VENDOR endpoint identity).
  - `VENDOR -[offers]-> SKU` — **shared with Product/Procurement** (same edge type; Vendors maintains the vendor side of the relation, freshness `confidence`).
  - `VENDOR -[in_tier]-> KICKBACK_TIER` (tier membership; the tier *terms* node belongs to Agreements — Vendors holds the membership + `ytd_progress` attribute and a `VENDOR -[under]-> AGREEMENT` reference).
  - `WORK_ORDER -[assigned_to]-> CONTRACTOR` — shared with Field Tech (Field Tech writes the assignment; Vendors owns the CONTRACTOR endpoint).
  - `CONTRACTOR -[has]-> VENDORS_CERT` (with expiry → drives Watcher alerts).
  - `VENDOR -[scored_by]-> VENDORS_SCORECARD`; `CONTRACTOR -[scored_by]-> VENDORS_PERF` — **derived edges**, `confidence` = sample-size sufficiency, recomputed from outcomes.
- **memories/ledger:** every scorecard/performance recompute appends its inputs + result to `v3_cognitive_ledger` (this is where reliability *history* lives — so "how did this vendor perform on similar jobs / why did its on-time score drop" is auditable, not a snapshot). Free-text contractor reviews / vendor incident notes → `memories` (embedding + `content_fts`) for cognitive recall. Tag every derived row with `vendors_source_id` for hard-retirement on counterparty delete (D365 retirement pattern).

## Core functions
<!-- BLOCKED ON OQ-2 / OQ-4: Core calculation reducers and registry functions are wrapped by the 10 registered MCP tools and 2 REST routes. -->
Pure-ish `do_<action>(engine, params) -> dict`; scorecard math is a **pure** reducer over outcome events (0 DB), the registry CRUD is thin DB.
- `do_get_vendor(engine, params) -> dict` — `{vendor_id}` → canonical identity + current scorecard + tier/ytd-progress. The record Procurement reads.
- `do_upsert_vendor(engine, params) -> dict` — reconcile a VENDOR identity (orgnr-keyed); idempotent merge of feed-ingested + admin-entered fields. Actor.
- `do_compute_scorecard(engine, params) -> dict` — `{vendor_id, window}` → `{on_time_pct, defect_rma_rate, substitution_rate, reliability, sample_n}`. **Pure** reducer over PO-match + GR outcome events from the ledger. Weights from `vendor-scorecard-weights.json`.
- `do_get_tier_status(engine, params) -> dict` — `{vendor_id}` → `{current_tier, ytd_volume, next_tier_threshold, ytd_progress, days_left}`. Membership/progress only; tier *terms* fetched by reference from Agreements.
- `do_upsert_contractor(engine, params) -> dict` — admin-entered profile (rates, skills, availability, certs). Actor; writes CONTRACTOR + VENDORS_CERT nodes.
- `do_match_contractor(engine, params) -> dict` — `{job: {skills[], location, window}}` → ranked contractors by **skill × location × current-load × performance-history**. Pure over profile + ledger; feeds Field Tech dispatch. Advisor.
- `do_compute_performance(engine, params) -> dict` — `{contractor_id, window}` → performance score from work-order ratings/outcomes on the ledger. Pure reducer.
- `do_record_outcome(engine, params) -> dict` — appends a vendor/contractor outcome (match result, WO rating) to the ledger; the event source the two reducers consume.
- `do_partner_view(engine, params) -> dict` — **redacted** projection for an external contractor: their own work orders + relevant BOM lines only, with margin/price/strategy/pipeline fields stripped. The single REST/A2A entry external partners hit (see Partner Access Model).

## Partner Access Model (key design)
External contractors must see **ONLY their own work orders + the relevant BOM lines**, and **NEVER** margin, price, strategy, or customer-pipeline data. NCE enforces this at **three independent layers** (defence in depth — any one failing still denies):
1. **Namespace / sub-scope RLS.** Contractor sessions run under a **sub-scope** of the namespace. `contractor_profiles` and the partner-facing views carry a `partner_scope_id`; `FORCE ROW LEVEL SECURITY` + a `partner_isolation_policy USING (namespace_id = get_nce_namespace() AND partner_scope_id = get_nce_partner_scope())` means a contractor's connection physically cannot read rows outside their own assignments — even a bug in app code cannot widen the row set.
2. **A2A tool-scoping.** Contractor-facing agents are issued a **redacted toolset**: only the read-only, partner-safe tools (`vendors_partner_view`, the field-tech WO-update tool) are bound to the partner agent profile. Margin/price/scoring tools (`procurement_*`, economy/sales tools) are **never registered** into a partner agent's tool surface, so there is no MCP path to privileged data regardless of prompt.
3. **Field-level redaction in the REST surface.** `do_partner_view` / `api_vendors_partner_view` apply an **allow-list projection** (not a deny-list): only explicitly partner-safe fields pass; price/cost/margin/pipeline/strategy fields are dropped *before serialization*. The allow-list lives in `partner-redaction.json` (config-as-IP) so the safe-field set is auditable and tenant-tunable.

This is the canonical "Partner Access Model" referenced by Field Tech(12) and the field-service skill — restricted access is **partly enforced here** (the data model + the redacted projection) and partly by Field Tech (the mobile WO surface) and the A2A server (tool binding).

## MCP tools
<!-- BLOCKED ON OQ-2 / OQ-4: Historical proposal listed 6 tools. Baseline 7304330 registers 10 MCP tools: vendors_get_vendor, vendors_compute_scorecard, vendors_get_tier_status, vendors_detect_reliability_degradation, vendors_check_tier_at_risk, vendors_match_contractor, vendors_compute_performance, vendors_recall_similar_jobs, vendors_reliability_radar, vendors_calibrate_weights. -->
Registered in `nce/tool_registry.py` via `_h(...)` late-binding. AI-role tag per roadmap §2 taxonomy.

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `vendors_get_vendor` | ✔ | ✘ | ✘ | Advisor |
| `vendors_compute_scorecard` | ✔ | ✘ | ✘ | Advisor |
| `vendors_get_tier_status` | ✔ | ✘ | ✘ | Watcher |
| `vendors_match_contractor` | ✔ | ✘ | ✘ | Advisor |
| `vendors_compute_performance` | ✔ | ✘ | ✘ | Advisor |
| `vendors_partner_view` | ✔ | ✘ | ✘ | Advisor (partner-scoped) |
| `vendors_upsert_vendor` | ✘ | ✔ | ✔ | Actor |
| `vendors_upsert_contractor` | ✘ | ✔ | ✔ | Actor |
| `vendors_record_outcome` | ✘ | ✔ | ✔ | — (ledger append) |

> `vendors_partner_view` is the **only** tool bound into a contractor-facing agent profile. All others are operator/internal — never registered into a partner agent's surface (Partner Access Model layer 2).

## REST routes
<!-- BLOCKED ON OQ-2 / OQ-4: Mounted REST routes at baseline 7304330 are /api/vendors/scorecard and /api/vendors/{id}. Contractor upsert and partner view routes are not mounted as standalone REST endpoints (contractor partner view is accessed via A2A skill vendors_partner_view). -->
No-model path for the BFF, cron, scripts. Mounted via `build_app(extra_routes=...)`; HMAC/mTLS-authed in `nce/admin_handlers/vendors.py`:
- `api_vendors_get_vendor` (GET) — identity + scorecard + tier (Procurement sourcing rationale).
- `api_vendors_scorecard` (GET) — reliability dashboard for a vendor (or all, paged).
- `api_vendors_tier_status` (GET) — kickback tier + ytd-progress + days-left.
- `api_vendors_upsert_vendor` (POST) — admin master-data merge.
- `api_vendors_upsert_contractor` (POST) — admin contractor profile/cert entry.
- `api_vendors_match_contractor` (POST) — ranked contractor candidates for a job (Field Tech dispatch).
- `api_vendors_partner_view` (GET) — **partner-scoped, field-redacted** WO + BOM-line projection (the external-contractor surface; redaction layer 3).

## AI features
- **Watcher:** reliability **degradation** detection (on-time % / defect-rate trending worse over the window → alert before it bites a project); **expiring contractor certs/insurance** (cert expiry within N days → block dispatch eligibility); kickback-tier **at-risk** ("days-left vs ytd-pace" race, shared signal with Procurement).
- **Advisor:** **best contractor for a job** (skill/location/current-load/history ranking → Field Tech dispatch) with plain-language rationale; **supplier-risk scoring** feeding Procurement's step-5 (reliability as a TCO input, tier-proximity bonus).
- **Cognitive recall:** *"how did this vendor/contractor perform on similar jobs"* — answered from `v3_cognitive_ledger` outcome history + `memories` review text, not a static field; an auditor can query *why* a scorecard moved.
- **Enrichment triggers (event-scoped, never a background sweep):** a scorecard/performance recompute fires **only** when a relevant outcome lands (a PO match result for the vendor, a work-order rating for the contractor) or a cert nears expiry. Never bulk-recompute all counterparties.

## A2A flows
- **Serves Procurement(1):** supplier scorecards + reliability + current kickback-tier/ytd-progress as inputs to the 5-step scoring (step 3 TCO reliability, step 5 tier × kickback-proximity).
- **Serves Field Tech(12):** `vendors_match_contractor` for dispatch + `vendors_partner_view` as the restricted external-contractor surface.
- **Serves Agreements(3):** canonical **counterparty identity** (VENDOR/CONTRACTOR) that signed terms attach to (`VENDOR -[under]-> AGREEMENT`).
- **Serves HR(13):** the contractor↔employee **skills parallel** — same skills/cert vocabulary so Field Tech can rank internal techs (HR) and externals (Vendors) on one axis.
- **Consumes:** Procurement match outcomes and Field Tech work-order ratings (via `do_record_outcome` / ledger) to keep scorecards live.

## Config keys
`NCE_VENDORS_*` in `nce/config.py`: `NCE_VENDORS_ENABLED`, `NCE_VENDORS_SCORECARD_WINDOW_DAYS` (default 365), `NCE_VENDORS_SCORECARD_MIN_SAMPLE` (min N before a score is shown vs "insufficient data"), `NCE_VENDORS_CERT_EXPIRY_WARN_DAYS` (default 30), `NCE_VENDORS_RELIABILITY_DEGRADE_PCT` (Watcher trip threshold), `NCE_VENDORS_RECOMPUTE_AFTER_N` (outcomes before recompute). Namespaces opt in via `metadata.vendors.enabled = true`. Never a host-specific key (FE-5).
**Config-as-IP JSON (namespace-scoped, the business IP — NOT code):**
- `vendor-scorecard-weights.json` — weighting of on-time/defect/substitution/reliability into the composite score; each tenant tunes its own.
- `contractor-match-weights.json` — skill/location/load/history weights for `do_match_contractor`.
- `partner-redaction.json` — the **allow-list** of partner-safe fields for `do_partner_view` (the safe-field contract; auditable, tenant-tunable).

## Tables/migrations
**Graph-first** for identity/reliability (VENDOR/CONTRACTOR/scorecard/cert live as `kg_nodes`/`kg_edges`; history in `v3_cognitive_ledger`). Two own tables where a keyed lookup + the restricted-access enforcement beats the graph — both `ENABLE` + **`FORCE ROW LEVEL SECURITY`** (restricted-access is partly enforced here), mirrored into `schema.sql` + a numbered migration:
- `vendor_scorecards` (`vendor_id, namespace_id, on_time_pct, defect_rma_rate, substitution_rate, reliability, current_tier, ytd_progress, sample_n, raw jsonb, computed_at`) — fast keyed read for Procurement sourcing. `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`.
- `contractor_profiles` (`contractor_id, namespace_id, partner_scope_id, profile jsonb, rates jsonb, skills text[], availability jsonb, performance_score, updated_at`) — the partner-facing master record; carries `partner_scope_id` and a **`partner_isolation_policy USING (namespace_id = get_nce_namespace() AND partner_scope_id = get_nce_partner_scope())`** so contractor sessions are physically scoped to their own rows (Partner Access Model layer 1).

## Dependencies
- **Upstream:** Product(2)/Procurement(1) feeds for VENDOR master-data ingest (Vendors enriches, does not re-pull); Agreements(3) for the kickback-tier *terms* and signed counterparty terms (Vendors holds membership, not terms).
- **Downstream:** Procurement(1) (scorecards/tiers into scoring), Field Tech(12) (contractor matching + restricted access), HR(13) (shared skills/cert vocabulary).
- **Boundary (do NOT duplicate):** **Vendors owns IDENTITY + RELIABILITY/PERFORMANCE.** **Agreements owns the signed TERMS** (kickback agreement, vendor contracts) — Vendors references them, never stores them. **Procurement owns SCORING + the PO** (the 5-step rank, 3-way match) — Vendors *feeds* the reliability/tier inputs but does not score or create POs.

## Review round-2 hardening (2026-06-17 — these govern the build)
> **Positive exemplars (hold these — they're the canonical models for the suite):** this engine's per-node ownership ("registry is the sole creator of `VENDOR`/`CONTRACTOR`; everyone references"), its sample-size-gated scorecards (`min_sample` + confidence), and especially its **allow-list (not deny-list) partner redaction** (`partner-redaction.json`) are the model the other engines copy — Sales's public quote surface should reference this exact allow-list pattern.

1. **Partner Access Model Layer-1 is an NCE-CORE prerequisite, not B3 vertical work (roadmap §9.6).** The `partner_isolation_policy USING (… AND partner_scope_id = get_nce_partner_scope())` relies on primitives NCE doesn't have yet: a new session GUC `nce.partner_scope_id`, a `get_nce_partner_scope()` function, and contractor-session setup in `admin_app`/`a2a_server`. Hard requirements: **defaults to DENY when unset** (never "see all in namespace") and **ANDs with** the tenant policy. This is a **security-reviewed core RLS extension** that must land *before* B3 — and Field Tech(12) depends on it too.
2. **Kickback data originates HERE — the Procurement conflict-of-interest traces back to this engine.** `current_tier`/`ytd_progress` + the "days-left vs ytd-pace" Watcher actively nudge buying to chase the next rebate tier. Tracking tiers is fine; **tier-proximity as a ranking objective needs the governance boundary** (Procurement hardening #1 / `procurement-governance.json`). Tier data is **commercially sensitive** → already stripped from the partner view; it must **also be stripped from any Sales/customer-facing projection** (never leaks to a customer-visible surface).
3. **Scorecards depend on Field Tech (Tier 3) ratings — a sparse score must NOT silently penalize.** No contractor-performance data exists until Field Tech ships, and scorecards feed Procurement ranking + dispatch. The producer flags `insufficient data` (good); the discipline is **at the seam — consumers (Procurement/dispatch) must honour that flag as NEUTRAL, not a low score** — never down-rank a vendor on 2 data points.
4. **"Subscribes to feed-produced upserts" assumes a reactive mechanism NCE doesn't have (roadmap §9.6).** How Vendors learns a new `VENDOR` node appeared ("subscribes") is the unspecified **reactive graph-event** problem (recurs in Project's auto-task, Economy's fires-on-ingest). Resolve via the shared trigger mechanism / polling convention — not a per-engine assumption.

## Build phases
<!-- BLOCKED ON OQ-2 / OQ-4: Historical build phases B1-B5. Refer to docs/engines/vendors-admin.md for shipped milestone status. -->
- **B1 — Vendor registry + scorecard core:** `VENDOR` identity upsert (orgnr-keyed, idempotent merge of feed + admin), `vendor_scorecards` table (RLS), pure `do_compute_scorecard` reducer over ledger outcomes (+ `vendor-scorecard-weights.json`). `vendors_get_vendor`/`compute_scorecard` MCP + REST. `vendors_source_id` retirement.
- **B2 — Tiers + Procurement feed:** `do_get_tier_status` (membership + ytd-progress, Agreements reference), `do_record_outcome` consuming Procurement match results. Wire scorecards/tiers into Procurement's step-5 scoring via A2A. Reliability-degradation + tier-at-risk Watchers.
- **B3 — Contractors + Partner Access Model:** `contractor_profiles` (RLS + `partner_scope_id` partner-isolation policy), `do_upsert_contractor`, `do_partner_view` allow-list redaction (`partner-redaction.json`), partner-scoped A2A toolset binding. Cert nodes + expiry Watcher.
- **B4 — Contractor matching + performance:** `do_match_contractor` (skill/location/load/history, `contractor-match-weights.json`), `do_compute_performance` from work-order ratings. Serve Field Tech dispatch via A2A. Cognitive-recall ("similar jobs") over ledger + memories.
- **B5 — Reliability frontier:** cross-engine reliability radar (supplier-risk + contractor-burnout signals into the Morning-brief #19 aggregate); data-driven scorecard-weight calibration closing the ledger loop.
