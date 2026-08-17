> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 01 — Procurement Engine  (nce/vertical_modules/procurement)

<!-- BLOCKED ON OQ-2 / OQ-4: SPEC PROPOSAL VOICE. This document is an architectural design specification. At baseline 7304330, Procurement ships 6 MCP tools and 8 REST routes (see docs/_generated/surface.md). do_submit_po and NetsetPoTransport remain unwired stubs under Contract B C2 autonomy ceiling. Refer to docs/engines/procurement-user.md and docs/engines/procurement-admin.md for shipped reality. Verified-against: 7304330 -->


**Status:** spec (Tier 1 — Operations axis) · **Owner:** NCE core (Sindre)
**Pattern companions:** `docs/VERTICAL_MODULE_PATTERN.md`, `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§4 graph catalogue, §7 spec format)

## Mission
Turn "which distributor do we buy this BOM line from, and did we get what we paid for" into a cognitive capability. The engine lifts Andreas's most portable IP — the **5-step distributor policy**, the **pure-function TCO engine**, the **3-way match + substitution detection**, **per-supplier adaptive learning**, and **savings aggregation** — onto the NCE graph spine. The deep-AI angle: the weights are not code, they are namespace-scoped config-as-IP, and the per-supplier thresholds *recalibrate from the cognitive ledger* — so the engine answers "why did we rank Crestron over the 4%-cheaper option" and "why did this supplier's match threshold move" from auditable memory, not a black box. The frontier opening (handoff 16): BOM-pipeline-derived, cross-supplier, ROI-quantified year-end rebate reallocation that no specialist tool commercialises.

## Inspiration & triage
- **Andreas sources (lift near-1:1, pure functions):**
  - `lib/procurement/engine.ts:calculateTCO` — TCO breakdown (price+freight+warranty+stock+delivery-risk). `warrantyCost` hardcoded 0 = known gap to close.
  - `lib/procurement/scoring.ts:scoreSupplier` / `rankSuppliers` — 5-step weighted scoring; `priceScore=3` placeholder.
  - `lib/procurement/three-way-match.ts:evaluateThreeWayMatch` + `detectSubstitution:395` — 3-zone tolerance, confidence 0–100, 4-level substitution.
  - `lib/finance/matching/learning.ts:recalibrateThresholds` — event-sourced per-supplier recalibration (N=100).
  - `lib/procurement/savings-aggregator.ts` — "money on the table"; `lib/procurement/contract-repository.ts`, `bid-matcher.ts`, `generate-po.ts`.
  - Tests are the runnable spec — lift `tests/procurement-engine.test.ts`, `tests/finance/procurement-three-way-match.test.ts`, `procurement-scoring.test.ts`, `matching-learning.test.ts` alongside the engine.
- **Portal sidecars to lift:** `backend/integrations/nettailer_client.py` (Netset Nettailer CSV export client — products/orderlines/supplierprices; streaming index for the ~295 MB feed) and `backend/steps_product/bidprices.py` (BID-price ingest: alias-map + honest column-report). These become `procurement/client.py` + the BID/supplier-price feed into `sync.py`.
- **Lysning page served:** `Bestillinger.jsx` (the procurement/ordering page) — consumes the no-model REST surface.
- **Crown-jewel doc:** `docs/specs/encore/encore-procurement-brief.md` (1376 lines, 10 closed business decisions) — the algorithm + scaling strategy.

## Classification
**push + semantic.** External systems: **Nettailer/Netset** (product catalog, supplier prices, BID prices, orderlines) via CSV object-export over HTTPS — *the export GUID in the URL **is** the auth token* (treat the whole URL as a secret; never log or echo it). No OAuth, no API key. The **Netset Order API** (outbound ordering) is the unbuilt 🔴 blocker — see `do_submit_po` transport abstraction. Semantic track: supplier contracts/agreement terms (OCR'd PDFs) → `memories` for compliance recall. Resilience: `httpx.AsyncClient` (streaming, generous read timeout for large feeds) via `nce.http_resilience.request_with_retry()`.

## Graph contribution
Node `entity_type` prefixes: `PROCUREMENT_*`, plus shared spine nodes `PO`, `VENDOR`, `PRODUCT`/`SKU`, `BOM_LINE`.
- **Nodes:** `VENDOR` (distributor), `PO`, `PROCUREMENT_QUOTE_LINE` (a scored sourcing option), `PROCUREMENT_MATCH` (a 3-way match result), `PROCUREMENT_BID` (BID-price agreement), `PRODUCT`/`SKU`.
- **Edges (the §4 contract, our slice):**
  - `BOM_LINE -[procured_via]-> PO -[from]-> VENDOR -[under]-> AGREEMENT`
  - `VENDOR -[offers]-> SKU` (with `confidence` = price/availability freshness)
  - `PO -[matched_by]-> PROCUREMENT_MATCH` (confidence 0–1 from the 0–100 score)
  - `PO -[posted_to]-> INVOICE` — **boundary edge written by Procurement, consumed by Economy** (see Dependencies).
- **memories/ledger:** supplier-contract text → `memories` (embedding + `content_fts`) for compliance recall. Every match decision + every scoring decision → `v3_cognitive_ledger` (this is where per-supplier learning lives in NCE — the event-sourced recalibration generalises onto the ledger instead of a bespoke table). Tag every derived row with `procurement_source_id` for hard-retirement on delete (D365 retirement pattern).

## Core functions
<!-- BLOCKED ON OQ-2 / OQ-4: do_calculate_tco, do_rank_suppliers, and do_evaluate_three_way_match are pure domain calculation cores wired to MCP/REST; do_generate_po, do_submit_po, do_aggregate_savings, and do_record_match_decision remain internal/unwired or background workflows. -->
Pure-ish `do_<action>(engine, params) -> dict`; the TCO/scoring/match cores are **pure** (0 DB) and lift near-1:1 from Andreas.
- `do_calculate_tco(engine, params) -> dict` — `{supplier, bom_line}` → TCO breakdown (price+freight+warranty+stock+delivery_risk). Pure. Weights from `procurement-weights.json`.
- `do_rank_suppliers(engine, params) -> dict` — `{bom_line, candidates[]}` → ranked list w/ score breakdown, applying the **5-step DELIBERATE order**: (1) own stock → (2) delivery-deadline filter → (3) true TCO → (4) BID price → (5) tier × kickback-proximity × bundling. Pure over config.
- `do_evaluate_three_way_match(engine, params) -> dict` — `{po, goods_receipt, invoice}` → `{confidence 0..100, tier GREEN|YELLOW|RED, tolerance_zone, substitution?}`. Pure; 4-level substitution detection (a substituted item can be a VALID replacement, not an error). Thresholds from `procurement-tolerances.json` overlaid with per-supplier ledger calibration.
- `do_resolve_bids(engine, params) -> dict` — `{artnrs[]}` → best BID price per article (lifts `bidprices.resolve`).
- `do_aggregate_savings(engine, params) -> dict` — namespace/period → "money on the table" (realised + lost savings, leakage candidates).
- `do_generate_po(engine, params) -> dict` — orchestrates rank → BID → contract → draft PO node (Actor; writes graph, no external order).
- `do_submit_po(engine, params) -> dict` — **transport-abstracted** ordering. Picks a `PoTransport` adapter (`nettailer` | `netset` | `manual`); the `netset` adapter is the 🔴 stub raising `NotImplementedError` with a clear message. Isolates the unbuilt blocker behind a stable contract.
- `do_record_match_decision(engine, params) -> dict` — appends a match outcome to the ledger; feeds recalibration after N=100.

## MCP tools
<!-- BLOCKED ON OQ-2 / OQ-4: Historical proposal listed 8 tools. Baseline 7304330 registers 6 tools (procurement_calculate_tco, procurement_rank_suppliers, procurement_evaluate_match, procurement_forecast_rebate, procurement_recommend_move_spend, procurement_whatif_spend). Tools procurement_resolve_bids, procurement_aggregate_savings, procurement_generate_po, procurement_submit_po, procurement_sync_now are not registered in TOOL_REGISTRY. -->
Registered in `nce/tool_registry.py` via `_h(...)` late-binding. AI-role tag per roadmap §2 taxonomy.

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `procurement_calculate_tco` | ✔ | ✘ | ✘ | Advisor |
| `procurement_rank_suppliers` | ✔ | ✘ | ✘ | Advisor |
| `procurement_evaluate_match` | ✔ | ✘ | ✘ | Advisor |
| `procurement_resolve_bids` | ✔ | ✘ | ✘ | Watcher |
| `procurement_aggregate_savings` | ✔ | ✘ | ✘ | Advisor |
| `procurement_generate_po` | ✘ | ✔ | ✔ | Actor |
| `procurement_submit_po` | ✘ | ✔ | ✔ | Actor (Autonomous under threshold) |
| `procurement_sync_now` | ✘ | ✔ | ✔ | — (operator) |

## REST routes
<!-- BLOCKED ON OQ-2 / OQ-4: Mounted REST routes at baseline 7304330 are /api/procurement/tco, /api/procurement/rank, /api/procurement/match, /api/procurement/sync, /api/procurement/sync/status, and 3 frontier routes (/api/procurement/frontier/*). Routes for savings, bid resolution, and draft PO creation are not mounted. -->
No-model path for the BFF (`Bestillinger.jsx`), cron, scripts. Mounted via `build_app(extra_routes=...)`; HMAC/mTLS-authed in `nce/admin_handlers/procurement.py`:
- `api_procurement_rank_suppliers` (POST) — sourcing decision for a BOM line.
- `api_procurement_calculate_tco` (POST) — TCO breakdown.
- `api_procurement_evaluate_match` (POST) — 3-way match (Bestillinger goods-receipt screen).
- `api_procurement_resolve_bids` (POST) — BID prices for article list (mirrors the Portal `resolve` contract; cap 500).
- `api_procurement_savings` (GET) — savings/leakage dashboard.
- `api_procurement_sync_status` / `api_procurement_sync_now` — feed health + column-report (the honest "unknown columns" report from `bidprices.py`).
- `api_procurement_generate_po` (POST) — draft-PO creation (still gated; submit is separate).

## AI features
- **Watcher:** avtaleløs-spend / price-leakage detection (live spend × contract-diff), expiring-BID alerts, year-end kickback-proximity "days-left-vs-pace" race.
- **Advisor:** supplier ranking with plain-language rationale ("Crestron over B which is 4% cheaper — because the order crosses the tier and locks +3% on 1.045M EUR = net better"); TCO breakdown; match triage.
- **Actor:** `generate_po` then `submit_po` *with confirmation*.
- **Autonomous (gated):** auto-route a sub-threshold order to a near-tier supplier — value/risk governance gate before write.
- **Cognitive recall:** per-supplier match-threshold recalibration is **read from `v3_cognitive_ledger`**, so an auditor can query *why* a threshold moved; "what similar lines did we source where" via `memories`.
- **Enrichment triggers (event-scoped, never a background sweep):** AI enriches a supplier/contract *only* when a BOM line is sourced, a goods-receipt arrives, or a contract PDF is ingested. Never bulk-recompute all suppliers.

## A2A flows
- **Serves Quote→Design→Procure:** answers System Design/Product with live TCO + availability for candidate SKUs.
- **Serves Receive→Match→Cascade:** Warehouse goods-receipt → `procurement_evaluate_match` → hands the GREEN/YELLOW/RED result to **Economy** for the approval cascade + posting.
- **Initiates failure-pattern feedback:** a substitution or repeated mismatch on a SKU emits the `failure_pattern` edge toward Product.
- **Feeds Morning-brief (#19 aggregate):** exposes savings + leakage + at-risk-tier as the procurement slice of the cross-engine risk/opportunity query.

## Config keys
`NCE_PROCUREMENT_*` in `nce/config.py`: `NCE_PROCUREMENT_ENABLED`, `NCE_PROCUREMENT_NETTAILER_*_URL` (products/orderlines/supplierprices/bidprices — GUID-bearing, secret), `NCE_PROCUREMENT_FEED_CACHE_TTL_SECONDS`, `NCE_PROCUREMENT_MAX_FEED_BYTES`, `NCE_PROCUREMENT_SYNC_INTERVAL_MINUTES`, `NCE_PROCUREMENT_RECALIBRATE_AFTER_N` (default 100), `NCE_PROCUREMENT_AUTONOMY_PO_CEILING` (auto-submit value gate). Namespaces opt in via `metadata.procurement.enabled = true`.
**Config-as-IP JSON (namespace-scoped, the business IP — NOT code):**
- `procurement-weights.json` — `SCORING_WEIGHTS` (the 5-step weights, kickback-proximity bonus, bundling). Each tenant tunes its own.
- `procurement-tolerances.json` — `MATCH_TOLERANCE` 3-zone profiles + GREEN/YELLOW default thresholds (115/70), per-supplier overrides.

## Tables/migrations
**Graph-first.** PO/VENDOR/match/BID live as `kg_nodes`/`kg_edges`; learning lives in `v3_cognitive_ledger`. One own table where a fast keyed lookup beats the graph:
- `procurement_bid_prices` (lifts the Portal `bid_prices` shape: `artnr, leverandor, bid_id, prodid, pris, valid_to, raw jsonb, synced_at`) — for the high-volume BID resolve path. `ENABLE` + `FORCE ROW LEVEL SECURITY` + `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`. Mirror DDL into `schema.sql` + numbered migration.
- `procurement_sync_runs` (audit, mirrors `d365_sync_runs`) — feed run history + column-report.

## Dependencies
- **Upstream engines:** Product(2) — scoring needs product specs/pricing (build Product first per §6); Vendors(4) for supplier master-data + kickback tiers; Agreements(3) for contract terms (compliance recall).
- **Downstream boundary — Economy(8):** the **130-pt invoice match is shared IP**. **Procurement owns PO creation, the 3-way match, and scoring; Economy owns the 7-effect approval cascade + GL posting.** Do NOT duplicate the cascade here — Procurement writes the `PO -[posted_to]-> INVOICE` edge and the match result; Economy consumes it. (Note: the 130-pt `score.ts` and the procurement 3-way-match are distinct — keep the invoice-scoring detail in Economy, the PO×GR×invoice match here.)
- **External blocker 🔴:** Netset Order API (outbound ordering) is unbuilt — abstracted behind `do_submit_po`/`PoTransport` so the engine ships fully usable without it (rank/TCO/match/savings all work; only auto-submit waits on the API key).

## Review round-2 hardening (2026-06-17 — these govern the build)
1. **Kickback-proximity is a GOVERNED decision, not a scoring factor (sharpest risk in the suite).** Step-5 `tier × kickback-proximity × bundling` and the B5 "move-spend to cross a tier" frontier optimise purchasing to maximise **the organization's** supplier rebates — which, depending on contract type (cost-plus, public-sector, regulated), can be a **conflict of interest or procurement fraud**. Binding: when step-5 changes the winner **vs best-TCO**, `do_rank_suppliers` emits an explicit **`rebate_override` flag + rationale**; a `procurement-governance.json` (config-as-IP) sets, **per contract type**, whether rebate-steering is allowed, whether the customer sees true cost, and the disclosure rule. Rebate-overrides are **compliance-reviewed + ledger-audited**, never silent. (The "Crestron over the 4%-cheaper option to lock +3% kickback" rationale must survive both an audit and a customer reading it.)
2. **The 3-way match has hard upstream feeds it doesn't own — phase it.** `do_evaluate_three_way_match` is **pure over its inputs (ships day one)**, but the *wired* match needs **goods-receipt from Warehouse (11, Tier 3)** and **invoice from Economy/Finago** — neither exists at launch. B1 ships the pure function + tests; the wired match lands when GR + invoice feeds exist. State this so B1 isn't "done" with nothing real to match.
3. **Don't lift Andreas's tests verbatim — it forks the config-as-IP.** His tests assert against *his* weights/tolerances (115/70, etc.). Lifting them verbatim **enshrines the organization's tenant config as code behaviour**, so the "weights are per-tenant config, not code" differentiator becomes fiction. Split: **algorithm tests parameterised by fixture weights** (shared code) vs **the organization's actual weight values validated as config data**.
4. **`warrantyCost=0` and `priceScore=3` are correctness milestones, not footnotes.** `warrantyCost=0` systematically under-weights warranty in TCO; `priceScore=3` placeholder means scoring may not respond to price at all. Closing them **changes rankings** → require **before/after ranking validation**. Trap: if you lift tests verbatim and they encode `priceScore=3`, you lock the bug in as expected behaviour (see #3).
5. **Nettailer feed: Procurement does NOT re-ingest it (roadmap §9.1).** **Product (2) owns the single ingest + SKU identity**; Procurement **consumes Product's supplier-price/BID/orderline projections** over A2A/REST. `procurement_bid_prices` is a **consumer cache/view of Product's projection, not a second 295 MB parse** with its own staleness clock.
6. **`submit_po` is governed by roadmap §9.5 (sharpest blast radius — spends real money).** Ship **human-confirm-only first**; `AUTONOMY_PO_CEILING` is necessary but nowhere near sufficient — it also needs **idempotency** (no double-order on retry), **volume/rate cap**, **supplier allowlist**, **kill switch**, all **ledger-audited**. Earn autonomy later; the Netset Order API is a stub anyway, so there's no reason to auto-submit at launch.

## Build phases
<!-- BLOCKED ON OQ-2 / OQ-4: Build phases B1-B5 represent historical sequencing. See docs/engines/procurement-admin.md for shipped phase status. -->
- **B1 — Pure cores + tests:** `do_calculate_tco`, `do_rank_suppliers` (5-step), `do_evaluate_three_way_match` (+substitution). Lift Andreas tests verbatim. Wire `procurement-weights.json` / `procurement-tolerances.json`. MCP tools + REST routes for the three. Close the `warrantyCost=0` gap.
- **B2 — Feeds + graph:** port `client.py` (Nettailer streaming) + `sync.py` + `procurement_bid_prices` table (RLS) + `do_resolve_bids`. Graph upserts (VENDOR/PO/SKU/match edges, `procurement_source_id`). `sync_now`/`sync_status` + column-report.
- **B3 — Learning + savings:** ledger-backed per-supplier recalibration (`do_record_match_decision`, N=100, auditor-queryable); `do_aggregate_savings` + leakage detection.
- **B4 — PO lifecycle:** `do_generate_po` (Actor) + `do_submit_po` with `PoTransport` adapters (nettailer live, netset 🔴 stub). Autonomy gate (`AUTONOMY_PO_CEILING`).
- **B5 — Frontier AI (handoff 16):** BOM-pipeline → year-end rebate forecast, cross-supplier "move-spend" ROI recommendation + what-if simulator + governance gate. Data-driven weight calibration closing the ledger loop.
