> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 05 — Sales Engine  (nce/vertical_modules/sales)

<!-- BLOCKED ON OQ-2 / OQ-4: SPEC PROPOSAL VOICE. This document is an architectural design specification. At baseline 7304330, Sales ships exactly 2 MCP tools (sales_ping, sales_get_signed_baseline) and 15 REST routes (1 public: /public-api/sales/quotes/{id}). 12 prospective MCP tools described below remain unbuilt/unwired. Refer to docs/engines/sales-user.md and docs/engines/sales-admin.md for shipped reality. Verified-against: 7304330 -->


**Status:** spec (Tier 2 — Revenue axis) · **Owner:** NCE core (Sindre)
**Pattern companions:** `docs/VERTICAL_MODULE_PATTERN.md`, `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§4 graph catalogue, §7 spec format, Tier-2 "Sales replaces steps_d365" note), `docs/DATA_SOURCE_MODES.md` (Sales is the headline `d365|both|nce` use case)

## Mission
Own the front of the spine — lead → opportunity → deal → quote → signature → project — as a cognitive capability, and in doing so **become the system of record for the sales read-model that today powers ~12 Lysning pages** (`steps_d365`). The decision (roadmap §8.2): **Sales replaces `steps_d365`**; D365 is demoted to an *optional source adapter* toggled per-function via the admin `d365|both|nce` switch, so NCE can leave D365 with **no data migration at flip** — NCE has retained the source data all along via the D365 watermark/delta incremental-sync pattern. The deep-AI angle: every deal carries cognitive recall ("deals like this that closed / slipped"), the DealRoom personalises itself from won/loss memory, and the **signed-baseline freeze** at signature is the immutable contract-truth that Project margin is later measured against — Sales owns the signed quote, hands the frozen BOM forward, and never lets a cost update rewrite history.

## Inspiration & triage
- **the planning sources (lift / wire):**
  - the reference implementation `convertSignedQuoteToProject` — the **signing→project bridge**; freezes the immutable baseline (`:232-237`). 🟢 **fresh + correct but orphan (0 callers)** — the reference implementation built it, never wired it. We wire it (manifest `signed-quote-to-project`, angerkost 3).
  - the reference implementation `applySignedBaseline` — idempotent signed-baseline application; `marginSignedPct` is **never** overwritten by the cascade (the margin-trinity discipline). Lift the invariant.
  - the reference implementation — DG-pricing (`salgspris = kostpris / (1 − DG%)`); the quote-builder bug is the inline `*0.7` that bypasses it — close that gap when we build quote pricing.
  - Spor A (quote-driven) is the canonical path; Spor B (Dynamics-Won) is the legacy we are retiring (handoff 02 §2). DealRoom / BankID-signing / AI-lead-scoring are listed *not built* in the reference implementation — greenfield for us.
- **Portal sidecar to lift (the sales read-model):** `backend/steps_d365/` — `db.py` (the stored-truth aggregations: company_profile, mine, dashboard, manager, stats, targets), `client.py` (OData/Dataverse pull), `source.py` + `sync.py` + `auto_sync.py` (incremental retention), `classify.py`, `api.py` (the 12-page REST contract). These become the `sales/source_adapters/d365` adapter + the native NCE read-model.
- **Lysning pages served (12 + 2 customer-facing):** `Kunder.jsx`, `KundeDetalj.jsx`, `Oversikt.jsx`, `Salgsoversikt.jsx`, `SelgerDetalj.jsx`, `Avtaler.jsx`, `AvtaleDetalj.jsx`, `TilbudDetalj.jsx`, `SalgsStat`, `SalgsDashboard`, `D365Oversikt.jsx` (+ the cross-cut Oversikt). Customer-facing share surfaces: `Motebrief.jsx` (meeting brief) and `TilbudKunde.jsx` (the customer-facing quote/DealRoom view).
- **Crown-jewel doc:** planning module **01 — Sales** (`handoff/04-virksomhets-modulkart.md`): Lead→quote→DealRoom→BankID→auto-project; "commission must be tied to DB/contribution-margin — it must pay to sell drift (service)".

## Classification
**push + semantic.** External systems: **Dynamics 365 / Dataverse** (OData v4 + OAuth/Azure AD) — but only as an *optional adapter*, not a hard dependency; **Scrive / Criipto** (BankID e-signing, webhook-confirmed). Auth: reuse the `dynamics365` vertical's `DataverseTokenManager` (Redis-cached) for the D365 adapter; HMAC-validated webhooks for signing callbacks. Semantic track: meeting briefs, deal notes, win/loss reasons → `memories` for win/loss recall. **The source-mode resolver picks the path per `(namespace, function)`** — the whole engine is built so `nce` mode stands alone.

## Graph contribution
Node `entity_type` prefixes: `SALES_*`, plus shared spine nodes `CUSTOMER`, `QUOTE`, `BOM_LINE`, `PROJECT`, `SIGNED_BASELINE`.
- **Nodes:** `CUSTOMER` (account), `LEAD`, `OPPORTUNITY`, `DEAL`, `QUOTE`, `SALES_DEALROOM` (live web quote w/ toggle options), `SIGNED_BASELINE` (immutable signed margin/sum), `SALES_SELLER` (commission target).
- **Edges (the §4 contract, our slice):**
  - `CUSTOMER -[has]-> LEAD -[qualifies_into]-> OPPORTUNITY -[becomes]-> DEAL -[priced_as]-> QUOTE`
  - `QUOTE -[contains]-> BOM_LINE -[references]-> PRODUCT` (Sales/System Design → Product)
  - `QUOTE <-[designed_by]-> DESIGN` — **bidirectional** to System Design (06): the quote and the design hang off the same site/room **functional-location** tree, so either order (design→quote or quote→design) is cheap.
  - `QUOTE -[freezes]-> SIGNED_BASELINE`; `SIGNED_BASELINE -[becomes]-> PROJECT` — the **Sales→Project handoff** (Sales owns the signed quote; Project receives the frozen BOM).
  - `DEAL -[owned_by]-> SALES_SELLER` (commission attribution, DB-weighted).
- **memories/ledger:** meeting briefs, deal notes, win/loss reasons → `memories` (embedding + `content_fts`) for "deals like this" recall. Every stage transition + every signature → `v3_cognitive_ledger` (auditable deal history; commission-affecting events are append-only). Tag every derived row with `sales_source_id` for hard-retirement on delete (D365 retirement pattern) — and so a row's *origin adapter* is always known.

## Core functions
<!-- BLOCKED ON OQ-2 / OQ-4: Read-model functions are mounted directly on REST routes for Lysning pages; AI lead scoring and draft quote remain prospective/unwired. -->
Pure-ish `do_<action>(engine, params) -> dict`; every read path goes through the **source-mode resolver** (`d365|both|nce`), never hard-wires D365.
- `do_list_customers(engine, params)` / `do_customer_profile(engine, params)` — Kunder / KundeDetalj. Resolver-dispatched (`source_mode.sales.customers`).
- `do_sales_overview(engine, params)` — Salgsoversikt / Oversikt aggregate (pipeline value by stage).
- `do_seller_detail(engine, params)` — SelgerDetalj: per-seller pipeline + commission-to-date (DB-weighted).
- `do_sales_dashboard(engine, params)` / `do_sales_stats(engine, params)` — SalgsDashboard / SalgsStat (lifts `steps_d365/db.py` dashboard + stats + targets aggregations).
- `do_list_agreements(engine, params)` / `do_agreement_detail(engine, params)` — Avtaler / AvtaleDetalj.
- `do_quote_detail(engine, params)` — TilbudDetalj; the customer-facing projection feeds `TilbudKunde.jsx`.
- `do_open_dealroom(engine, params)` — materialise a live web quote with toggle-able option lines (price recomputes via DG-pricing, not inline `*0.7`).
- `do_request_signature(engine, params)` — Actor: send the DealRoom quote to Scrive/Criipto for BankID signing; returns a signing-session ref. Webhook confirms.
- `do_freeze_signed_baseline(engine, params)` — on signature: snapshot the immutable signed margin/sum + the frozen BOM. Idempotent; the baseline is **never** rewritten (margin-trinity).
- `do_convert_signed_quote_to_project(engine, params)` — wires the reference implementation's orphan `convertSignedQuoteToProject`: hands the frozen BOM to the Project engine (Actor; emits `SIGNED_BASELINE -[becomes]-> PROJECT`).
- `do_score_lead(engine, params)` — Advisor: lead score from cognitive recall (pure over features + ledger).
- `do_draft_quote(engine, params)` — Advisor: AI quote-draft assist (asks System Design for a BOM; never bulk-runs).

## MCP tools
<!-- BLOCKED ON OQ-2 / OQ-4: Historical proposal listed 14 tools. Baseline 7304330 registers exactly 2 MCP tools: sales_ping and sales_get_signed_baseline. The remaining 12 prospective tools are not in TOOL_REGISTRY. -->
Registered in `nce/tool_registry.py` via `_h(...)` late-binding. AI-role tag per roadmap §2 taxonomy.

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `sales_list_customers` | ✔ | ✘ | ✘ | — (read-model) |
| `sales_customer_profile` | ✔ | ✘ | ✘ | — (read-model) |
| `sales_overview` | ✔ | ✘ | ✘ | — (read-model) |
| `sales_dashboard` | ✔ | ✘ | ✘ | — (read-model) |
| `sales_stats` | ✔ | ✘ | ✘ | — (read-model) |
| `sales_seller_detail` | ✔ | ✘ | ✘ | Advisor |
| `sales_quote_detail` | ✔ | ✘ | ✘ | — (read-model) |
| `sales_score_lead` | ✔ | ✘ | ✘ | Advisor |
| `sales_draft_quote` | ✘ | ✘ | ✘ | Advisor |
| `sales_open_dealroom` | ✘ | ✘ | ✔ | Actor |
| `sales_request_signature` | ✘ | ✔ | ✔ | Actor |
| `sales_freeze_signed_baseline` | ✘ | ✔ | ✔ | Actor |
| `sales_convert_signed_quote_to_project` | ✘ | ✔ | ✔ | Actor |
| `sales_sync_now` | ✘ | ✔ | ✔ | — (operator) |

## REST routes
<!-- BLOCKED ON OQ-2 / OQ-4: Mounted REST routes at baseline 7304330 comprise 15 endpoints (14 under /api/sales, /api/admin/sales, plus 1 public quote endpoint at /public-api/sales/quotes/{id}). -->
No-model path for the BFF (the 12 Lysning pages + the 2 customer-facing shares), cron, scripts. Mounted via `build_app(extra_routes=...)`; HMAC/mTLS-authed in `nce/admin_handlers/sales.py`. Each resolves its source mode internally.
- `api_sales_customers` / `api_sales_customer_profile` (GET) — Kunder, KundeDetalj.
- `api_sales_overview` (GET) — Oversikt / Salgsoversikt.
- `api_sales_seller_detail` (GET) — SelgerDetalj.
- `api_sales_dashboard` / `api_sales_stats` (GET) — SalgsDashboard, SalgsStat (+ `api_sales_targets` GET/PUT, lifts `steps_d365` targets).
- `api_sales_agreements` / `api_sales_agreement_detail` (GET) — Avtaler, AvtaleDetalj.
- `api_sales_quote_detail` (GET) — TilbudDetalj.
- `api_sales_quote_public` (GET) — `TilbudKunde.jsx` customer-facing share (token-scoped, no internal margin fields).
- `api_sales_meeting_brief` (GET) — `Motebrief.jsx` (semantic recall over deal notes).
- `api_sales_dealroom` (POST) — open/update a DealRoom (toggle options, recompute price).
- `api_sales_signing_webhook` (POST) — Scrive/Criipto BankID callback → triggers freeze + convert.
- `api_sales_source_mode` (GET/PUT) — the admin `d365|both|nce` per-function control surface.
- `api_sales_sync_status` / `api_sales_sync_now` — D365-adapter feed health + watermark.

## AI features
- **Watcher:** stalled-deal / slip detection (deal past expected close with no movement); expiring-agreement alerts (feeds Avtaler).
- **Advisor:** **lead scoring** (from cognitive recall — "leads like this converted at X%"); **quote-draft assist** (asks System Design for a BOM, fills gaps from won-deal memory); commission what-if (DB-weighted, so it pays to sell drift/service).
- **Actor:** open DealRoom → request BankID signature → on callback **freeze signed baseline** → **convert signed quote to project** (each gated; signature is the trigger, not a sweep).
- **DealRoom personalisation:** the live web quote reorders/recommends toggle options from win/loss memory for similar customers — personalised, not static.
- **Cognitive recall:** **win/loss recall** — "deals like this that closed / slipped" via `memories` + ledger; the meeting brief (`Motebrief.jsx`) is generated from this recall.
- **Enrichment triggers (event-scoped, never a background sweep):** AI runs *only* when a lead arrives (score), a quote is drafted (BOM assist), a DealRoom opens (personalise), or a deal closes/slips (win/loss capture). Never bulk-rescore the pipeline.

## A2A flows
- **Initiates Quote→Design→Procure:** the Sales agent asks **System Design** (06) for a BOM (bidirectional, functional-location-anchored), which asks **Product** for missing specs, which asks **Procurement** for live TCO/availability.
- **Initiates Sales→Project handoff:** on signature, hands the frozen signed-baseline BOM to **Project** (7) via `sales_convert_signed_quote_to_project` — the orphan bridge, now wired.
- **Serves win/loss feedback to Product:** repeated loss reasons tied to a SKU emit a `failure_pattern` signal toward Product (the "silence" the reference implementation flags — upsell/BOM feedback).
- **Feeds Morning-brief (#19 aggregate):** exposes pipeline value, at-risk deals, and won-this-period as the Sales slice of the cross-engine risk/opportunity query ("sales celebrates, operations cries").

## Config keys
`NCE_SALES_*` in `nce/config.py`: `NCE_SALES_ENABLED`, `NCE_SALES_D365_ADAPTER_ENABLED` (the adapter on/off — independent of the engine), `NCE_SALES_SYNC_INTERVAL_MINUTES`, `NCE_SALES_PAGE_SIZE`, `NCE_SALES_SIGNING_PROVIDER` (`scrive|criipto`), `NCE_SALES_SIGNING_WEBHOOK_SECRET`, `NCE_SALES_DEALROOM_PUBLIC_BASE_URL`, `NCE_SALES_SLIP_DAYS` (stalled-deal threshold). D365 OAuth keys are reused from the `dynamics365` vertical, never re-declared (FE-5). Namespaces opt in via `metadata.sales.enabled = true`; source modes live in the `settings` table per `source_mode.sales.<function>`.
**Config-as-IP JSON (namespace-scoped, the business IP — NOT code):**
- `sales-commission.json` — DB/contribution-margin commission tiers (the "make it pay to sell drift" rule — service/MRR lines weighted higher than hardware).
- `sales-pipeline-stages.json` — stage definitions + slip thresholds + lead-scoring feature weights.

## Tables/migrations
**Graph-first** for the deal lifecycle (LEAD/OPPORTUNITY/DEAL/QUOTE/SIGNED_BASELINE live as `kg_nodes`/`kg_edges`; win/loss + commission events in `v3_cognitive_ledger`). Own tables only where the 12-page read-model needs fast keyed aggregation and the source switch needs a retained store:
- `sales_read_model` — the **retained NCE-side store** mirrored from D365 (accounts, opportunities, quotes, agreements, sellers; `entity, entity_id, payload jsonb, source ('d365'|'nce'), synced_at, watermark`). This is the *precondition* for `nce` mode (DATA_SOURCE_MODES §"load-bearing principle"). `ENABLE` + `FORCE ROW LEVEL SECURITY` + `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`.
- `sales_signed_baselines` — immutable signed margin/sum snapshot (append-only; no UPDATE — enforces margin-trinity). FORCE RLS.
- `sales_sync_runs` (audit, mirrors `d365_sync_runs`) — D365-adapter run history + watermark/delta cursor.
Mirror all DDL into `schema.sql` + numbered migration.

## Dependencies
- **Upstream engines:** Product(2) — quote/BOM lines reference PRODUCT specs/pricing; System Design(6) — produces the BOM (bidirectional, functional-location-anchored); both must exist (Tier 1) before Sales can quote against them — hence Sales is Tier 2.
- **Downstream:** Project(7) — receives the frozen signed baseline (Sales owns the signed quote, Project receives the frozen BOM); Economy(8) — DB/contribution-margin feeds commission + revenue-recognition (Sales does not own GL).
- **Replaces:** `steps_d365` read-model (production-critical, ~12 Lysning pages). The cutover is per-function via the admin switch, not big-bang.
- **External blocker 🔴:** Scrive/Criipto BankID production credentials + DPIA for signing (sandbox works without). The D365 adapter is *optional by design* — its eventual retirement is the goal, not a blocker.

## Hardening — the load-bearing machinery (review 2026-06-17)
The vision (read-model-of-record → AI last) is right; the under-weighted parts are the *unglamorous* machinery that decides whether D365 can ever actually be retired. These are **first-class scope**, governed by roadmap §9:

1. **`both` is a two-master problem, not a read dispatcher.** `both` = **NCE-primary + D365 parity check**; **every divergence (D365 edited but not synced; native deal D365 never saw) is logged to a `sales_divergence_log`** (entity, field, nce_value, d365_value, detected_at). This ledger is **built in B1** — it is the only honest basis for ever flipping a function to `nce` ("prove parity" is hand-waving without it).
2. **Write-back is routed, not just reads.** Reps *create/edit* deals/quotes/stages — the source-mode resolver routes **writes** too: **write-through to D365 while a function is `d365`/`both`, native-only once flipped.** Decide the collision-proof identity scheme **in B2 design**: source-prefixed IDs + mapping (extend `sales_source_id`) so a native edit and a D365 record never collide.
3. **DG-pricing is ONE shared service, not a third copy.** Killing the inline `*0.7` must **not** become "reimplement DG-pricing in the quote builder" (a third drifting path). `salgspris = kostpris/(1−DG%)` lives in a **shared pricing module Sales, Product (02 §A1) and Procurement all call** — Sales imports it, never re-derives it.
4. **The customer-facing surface is a security boundary, not a bullet.** `api_sales_quote_public` / `TilbudKunde` / DealRoom are externally exposed + token-scoped — a **different threat model** from the internal HMAC admin app. Build the public projection with an **explicit field allowlist (not a denylist)**, its own **rate-limited token-auth path**, and quite possibly **its own small app** (the admin/BFF split from PR #241). **Margin, cost, commission must never leak.**
5. **Commission must be reproducible, not just append-only.** Invariant: **any payout is re-derivable from `(ledger events + the versioned `sales-commission.json`)`** — so we **version the commission config**, not just store events. "The model decided" won't survive a commission dispute.
6. **The signed-baseline freeze ships early (B2), ahead of signing.** The freeze/immutability mechanism is the data-integrity contract the whole Project-margin story rests on — cheap to build right early, expensive to retrofit. **The Scrive/Criipto integration (the external blocker) can lag; the freeze mechanism cannot wait for it.**

7. **DealRoom stays native + graph-backed; Oneflow is the export/signing target, not the authoring surface (Oneflow research + Portal ADR 0023).** The DealRoom's value is exactly what Oneflow lacks — catalog-driven calc (Nettailer), BID prices + accessories, **live margin/coverage per line** (Oneflow knows only sale price), AI draft/deal-coach, read-time telemetry. So per ADR 0023: keep the DealRoom native, **mirror Oneflow's core data model 1:1** (`product_groups`/`products`, parties, two price columns), and use Oneflow only as the **export + BankID-signing rail** via the shared signing service (§9.6). `do_request_signature` calls that service (`SignTransport=oneflow` for authored quotes); on the `contract:sign` webhook → **freeze the signed baseline** (B2 mechanism). Don't let Oneflow's live contract *become* the DealRoom, and don't pay for CLM you don't use on the quote path.

**Scope discipline (Sales references, never owns):** GL / revenue recognition → **Economy** (Finago system-of-record); BOM authorship → **System Design** (Sales receives a BOM, freezes it, hands it forward); product enrichment → **Product**, on-demand only; e-signing → the **shared signing service** (§9.6), never a private Scrive/Oneflow integration.

## Build phases
<!-- BLOCKED ON OQ-2 / OQ-4: Historical build phases B1-B5. Refer to docs/engines/sales-admin.md for shipped milestone status. -->
- **B1 — Read-model parity + divergence audit (the cutover precondition):** port `steps_d365/db.py` aggregations → native `do_*` read functions + `sales_read_model` table (RLS) + the D365 source adapter (`sales/source_adapters/d365`, reusing `DataverseTokenManager`) + incremental sync/watermark retention. Wire the **source-mode resolver** and `api_sales_source_mode`. **Build `sales_divergence_log` now** (NCE-primary parity check on every `both`-mode read). Serve all 12 Lysning pages in `both`. REST routes for every read.
- **B2 — Native pipeline + write routing + freeze:** upsert LEAD/OPPORTUNITY/DEAL/QUOTE/CUSTOMER nodes + edges; **write routing** (write-through to D365 while `d365`/`both`, native once flipped) + **source-prefixed identity/mapping scheme**; native deal creation/edit. **`sales_signed_baselines` (append-only, margin-trinity) — the freeze mechanism, pulled forward here** (no dependency on signing). Customer-facing `api_sales_quote_public` on its **own allowlisted, rate-limited token path**.
- **B3 — Quote→signature→project:** DealRoom (`do_open_dealroom`, toggle options, **shared DG-pricing service** — no inline `*0.7`, no re-impl); Scrive/Criipto signing + webhook → triggers the (already-built) freeze; **wire the reference implementation's orphan** `convert_signed_quote_to_project`. A2A Sales→Project handoff.
- **B4 — AI surface:** lead scoring + quote-draft assist (Advisor, cognitive recall, **propose-only per §9.3**); win/loss capture + "deals like this" recall (`Motebrief.jsx`); DealRoom personalisation; **reproducible** DB-weighted commission (ledger events + versioned `sales-commission.json`). A2A Quote→Design→Procure + failure-pattern feedback to Product.
- **B5 — Stand-alone flip:** flip each function `both`→`nce` **only when its `sales_divergence_log` is clean over the parity window**; stalled-deal Watcher; feed the Morning-brief (#19) Sales slice. When all functions are `nce` with clean parity logs, the D365 adapter switches off with zero migration.
