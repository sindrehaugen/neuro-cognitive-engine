> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 08 — Economy Engine  (nce/vertical_modules/economy)

<!-- BLOCKED ON OQ-2 / OQ-4: SPEC PROPOSAL VOICE. This document is an architectural design specification. At baseline 7304330, Economy ships 3 read-only advisor MCP tools and 3 REST routes (see docs/_generated/surface.md). 9 pure domain calculation cores exist in nce/vertical_modules/economy/ but are not wired to autonomous mutation dispatchers; direct Finago GL posting is locked by CFO policy. Refer to docs/engines/economy-user.md and docs/engines/economy-admin.md for shipped reality. Verified-against: 7304330 -->


**Status:** spec (Tier 2 — Platform axis) · **Owner:** NCE core (Sindre)
**Pattern companions:** `docs/VERTICAL_MODULE_PATTERN.md`, `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§2 AI-role, §4 graph catalogue, §7 spec format), `docs/vertical_engines/01-procurement-engine.md` (the shared-IP boundary)

> **DECISION (roadmap §8.3 — state it up front): NCE Economy mirrors + periodises internally; Finago stays the GL / legal system-of-record.** Economy is **NOT** a GL replacement. It periodises **BEFORE** GL-commit because Finago cannot emit pre-booking vouchers (confirmed by Finago support — manifest `ngaap-periodisering`). Economy computes the correct internal numbers (match, cascade, accruals, projections, balanced postings) and *mirrors* the legal book; the real hovedbok-posting stays in Finago, behind a deliberately locked door (see Dependencies).

## Mission
Turn "did we get the invoice right, what does it do to the project, and when do we recognise it" into a cognitive capability. Economy lifts Andreas's **deepest** module — PFT (~17.3k LOC, 80+ test files, 6/6 phases code-complete): the **130-pt contextual invoice match**, the **7-effect approval cascade** (the single BOM-cost write-path), **NGAAP periodisering**, the **balance-guarantee**, the **margin-trinity**, and the **recurring-revenue** stack — onto the NCE graph spine. The deep-AI angle: every financial event is a balanced, hashed ledger entry, so the engine answers "why did this invoice triage YELLOW", "why did margin erode on this project", and "what does close look like" from auditable memory — and the cross-engine "sales celebrates, operations cries" radar emerges for free because the cascade writes the same graph Sales and Project read.

## Inspiration & triage
- **Andreas sources (lift near-1:1 / as a pattern):**
  - `lib/finance/matching/score.ts:computeMatchScore` — **130-pt** contextual match (manifest `bilag-match-130pt`, angerkost 5). Pure function. Lift 1:1.
  - `lib/finance/cascade/supplier-invoice-approved.ts:cascadeOnApproval` — **7-effect** idempotent cascade (manifest `supplier-invoice-cascade`, 546 LOC). Lift as a **pattern** (centralise your writes the same way).
  - `lib/finance/periodisering/cost-engine.ts:computeBucketTargets` — **NGAAP** 7-bucket accrual (manifest `ngaap-periodisering`). Pure; re-map accounts.
  - `lib/finance/events/emit.ts:UnbalancedPostingsError` — **balance-guarantee** (manifest `balanse-garanti`). Lift 1:1.
  - `lib/finance/matching/learning.ts:recalibrateThresholds` — per-supplier threshold learning (manifest `per-supplier-learning`, N=100, auditor-queryable).
  - `lib/finance/managed-services/engine.ts` — MRR/ARR/churn + ratable recognition + recurring cron; `contract-validator.ts` (CPI cap 5%), `lib/quote/renewal.ts:85` (90d scan). (handoff §8.)
  - `app/lab/.../_shared-enhancements.tsx:DunningTimeline` — Norwegian dunning/credit policy (manifest `lab-revenue-recognition-dunning`, showcase-gold). **Do not delete as "lab-pynt" — the production Wizard imports it LIVE.**
  - `lib/finance/kid/generator.ts` (KID mod-10/11 — **mod-10 shipped Batch 128; mod-11 still pending a bank-arrangement decision, see Build phases B5**), `lib/finance/forecasting/monte-carlo.ts` (cashflow sim), `ehf-generator.ts` (outbound EHF).
  - **Tests are the runnable spec — lift verbatim:** `tests/finance/matching-score.test.ts`, `supplier-invoice-cascade.test.ts`, `cost-engine.test.ts`, `events.test.ts`, `matching-learning.test.ts`.
- **Portal sidecars to lift:** finance flows + the Finago client surface (the GL *reader* used for reconciliation).
- **Lysning page served:** financial pulse / close screens; feeds the **Morning-brief (#19)** financial slice.
- **Crown-jewel context:** handoff §6 (bilag-matching), §7 (Finans/PFT), §8 (drift/recurring).

## Classification
**push + semantic.** External systems: **Finago** (GL/legal system-of-record — Economy **reads** for reconciliation, periodises **before** any commit; Normal-mode GL-commit is locked, see blockers) and **PEPPOL** (EHF in/out; provider pending — Tickstar/Pagero). Invoice ingest is **push + semantic**: EHF/PDF/manual/email/finago → extract via **EHF parser** OR **Claude Vision OCR** for PDFs → semantic text → `memories`. No OAuth on the GL reader path beyond Finago's own key. Resilience: `httpx.AsyncClient` (30s) via `nce.http_resilience.request_with_retry()`. **Incremental** (watermark/delta) so internal numbers stay current with no re-ingest.

## Graph contribution
Node `entity_type` prefixes: `ECONOMY_*`, plus shared spine nodes `INVOICE`, `PERIOD`, `CONTRACT`.
- **Nodes:** `INVOICE` (supplier/outbound), `POSTING` (a balanced ledger entry, sum=0), `PERIOD` (an accounting period / close), `CONTRACT` (recurring-revenue / MRR), `ECONOMY_MATCH` (130-pt result), `MARGIN` (the code registers bare `MARGIN`, not `ECONOMY_MARGIN` — a **per-dimension** node per §9.1's margin-trinity worked example: `signed`=Sales, `estimated`=Project, `actual`=Economy; this module registers and upserts only the `actual` dimension).
- **Edges (the §4 contract, our slice):**
  - `PO -[posted_to]-> INVOICE` — **boundary edge written by Procurement, CONSUMED here** (Receive→Match→Cascade).
  - `INVOICE -[recognized_in]-> PERIOD` (the periodisering output — accrued/deferred/WIP allocation).
  - `PROJECT -[has]-> MARGIN` (the margin-trinity snapshot the cascade updates — except `marginSignedPct`).
  - `INVOICE -[matched_by]-> ECONOMY_MATCH`; `CONTRACT -[recognized_in]-> PERIOD` (ratable 1/12).
- **memories/ledger:** invoice OCR/EHF text → `memories` (embedding + `content_fts`) for "find similar invoices / disputes" recall. **Learning lives on the ledger:** every match decision, every cascade run, every balanced posting → `v3_cognitive_ledger` (this is where per-supplier recalibration and close-narrative recall live — event-sourced, auditor-queryable, replacing Andreas's bespoke learning table). Tag every derived row with `economy_source_id` for hard-retirement.

## Core functions
<!-- BLOCKED ON OQ-2 / OQ-4: 9 domain cores exist in nce/vertical_modules/economy/ (do_compute_bucket_targets, do_compute_dunning, do_compute_recognition_schedule, do_emit_financial_event, do_forecast_cashflow, do_generate_kid, do_match_invoice, do_snapshot_mrr_arr_churn, do_validate_kid). Only 3 are exposed via MCP/REST as read-only advisor tools. Mutation cascades and direct GL posting remain unwired by policy. -->
Pure-ish `do_<action>(engine, params) -> dict`; the match/periodise/balance cores are **pure** (0 DB) and lift near-1:1.
- `do_match_invoice(engine, params) -> dict` — `{invoice, candidates[]}` → `{score 0..130, tier GREEN|YELLOW|RED, breakdown[]}`. **Pure.** 130-pt contextual: **PO-nr 50 / supplier 40 / price 30 / article 20 / project 10** + BOM-tieback / PO-expected-window / supplier-pattern (+15/+10/+5). Triage **≥115 GREEN / 70–114 YELLOW / <70 RED**, per-supplier adjustable thresholds overlaid from the ledger. **Invoice-tier = worst line-tier (conservative).**
- `do_compute_bucket_targets(engine, params) -> dict` — NGAAP accrued/deferred/WIP over a period boundary (regnskapsloven §4-1; 7 buckets; Norwegian accounts 1531/2901/1771/4300…). **Pure.** Accounts come from config-as-IP, not code.
- `do_cascade_on_approval(engine, params) -> dict` — **the ONLY place BOM-line cost is updated.** Idempotent; **one transaction** updates BOM cost, projections, margin, cashflow (the 7 effects). **GREEN = auto-ELIGIBLE, not auto-POSTED** — still requires Stage-2 approval. Lift as a pattern.
- `do_emit_financial_event(engine, params) -> dict` — writes balanced postings or raises `UnbalancedPostingsError` (sum=0 ±0.01) **at WRITE time**. The core discipline all money code routes through.
- `do_recognize_recurring(engine, params) -> dict` — ratable 1/12-per-month recognition; idempotent recurring-invoicing cron, `finagoRef = ms:{contractId}:{YYYY-MM}`; MRR/ARR/churn snapshot.
- `do_validate_contract(engine, params) -> dict` — CPI cap 5%, downgrade ≥30d (Zod-equivalent); `do_scan_renewals` (90d scan + CPI quote, idempotent).
- `do_compute_dunning(engine, params) -> dict` — Bisnode risk-score → dunning aggression (days **-3/+3/+10/+21**), Lindorff handoff; **risk>60 → require 100% HW-signing**. Pure over config-as-IP (credit bureau swappable).
- `do_forecast_cashflow(engine, params) -> dict` — Monte Carlo cashflow simulation (handoff: the real probabilistic sim, not a Claude stream).
- `do_generate_kid(engine, params)` / `do_generate_ehf(engine, params)` — KID mod-10/11 (`variant` param, default `"MOD10"`; **mod-10 ships today, mod-11 raises `NotImplementedError` pending the bank-arrangement decision on which variant a creditor requires — see `peppol.py`**); outbound EHF (EHF mandatory B2B Jan 2027).
- `do_reconcile_gl(engine, params) -> dict` — **Finago GL reader** (internal vs legal book; **Economy OWNS this reader** — also consumed by Agreements(3) for spend reconciliation).

## MCP tools
<!-- BLOCKED ON OQ-2 / OQ-4: Historical proposal listed 10 tools. Baseline 7304330 registers exactly 3 MCP tools: economy_match_invoice, economy_compute_periodisering, economy_emit_event. -->
Registered in `nce/tool_registry.py` via `_h(...)` late-binding. AI-role tag per roadmap §2.

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `economy_match_invoice` | ✔ | ✘ | ✘ | Advisor |
| `economy_compute_periodisering` | ✔ | ✘ | ✘ | Advisor |
| `economy_forecast_cashflow` | ✔ | ✘ | ✘ | Watcher |
| `economy_compute_dunning` | ✔ | ✘ | ✘ | Watcher |
| `economy_mrr_snapshot` | ✔ | ✘ | ✘ | Advisor |
| `economy_reconcile_gl` | ✔ | ✔ | ✘ | Advisor |
| `economy_cascade_on_approval` | ✘ | ✔ | ✔ | Actor |
| `economy_recognize_recurring` | ✘ | ✔ | ✔ | Autonomous (cron) |
| `economy_generate_ehf` | ✘ | ✔ | ✔ | Actor |
| `economy_sync_now` | ✘ | ✔ | ✔ | — (operator) |

## REST routes
<!-- BLOCKED ON OQ-2 / OQ-4: Mounted REST routes at baseline 7304330 are /api/economy/match-invoice, /api/economy/periodisering, /api/economy/emit-event. Routes for dunning, cashflow forecast, GL reconciliation, and EHF generation are not mounted. -->
No-model path for the BFF, cron, scripts. Mounted via `build_app(extra_routes=...)`; HMAC/mTLS-authed in `nce/admin_handlers/economy.py`:
- `api_economy_match_invoice` (POST) — 130-pt triage for an invoice + candidates.
- `api_economy_periodisering` (POST) — NGAAP bucket targets for a period.
- `api_economy_cascade` (POST) — Stage-2 approval → cascade (gated; the only BOM-cost write).
- `api_economy_mrr` (GET) — MRR/ARR/churn pulse.
- `api_economy_cashflow` (GET) — Monte Carlo forecast.
- `api_economy_reconcile` (GET) — internal-vs-Finago diff (also the Agreements spend feed).
- `api_economy_dunning` (POST) — risk-driven dunning plan for a customer.
- `api_economy_sync_status` / `api_economy_sync_now` — ingest health + EHF/OCR coverage report.

## AI features
- **Watcher:** cashflow-risk alerts (Monte Carlo tail), **margin-erosion** detection (actual drifting below signed baseline), **dunning triggers** (Bisnode risk crossing -3/+3/+10/+21 windows; risk>60 → escalate to 100% HW-signing + named approval).
- **Advisor:** **close-narrative** (period-close summary from the ledger — deterministic template today, cognitive-recall enriched), **match triage** rationale ("YELLOW: PO-nr matched 50 but price off 6% → 15/30, supplier fuzzy 20/40").
- **Actor:** `cascade_on_approval` and `generate_ehf` **with confirmation**.
- **Autonomous (gated):** **recurring invoicing** (idempotent cron, `finagoRef`-keyed); **auto-eligible GREEN matches** under a value/risk threshold (still Stage-2 for posting).
- **Cognitive recall:** per-supplier match-threshold recalibration is **read from `v3_cognitive_ledger`** — an auditor can query *why* a threshold moved; "what similar invoices / disputes" via `memories`.
- **Enrichment triggers (event-scoped, never a background sweep):** OCR/EHF enrichment fires **only** on invoice ingest; the cascade fires **only** on Stage-2 approval; recalibration fires **only** after N decisions. Never bulk-reprocess the ledger.

## A2A flows
- **Serves Receive→Match→Cascade:** receives the GREEN/YELLOW/RED 3-way result + `PO -[posted_to]-> INVOICE` edge **from Procurement(1)**, runs `economy_match_invoice` (130-pt) → Stage-2 → cascade. (Procurement owns the PO×GR×invoice match; Economy owns the invoice match + cascade + posting.)
- **Consumes signed baseline from Project(7):** `marginSignedPct` is the contract truth the cascade measures actuals against and **never overwrites**.
- **Feeds Agreements(3) reconciliation:** exposes GL spend via the owned `reconcile_gl` reader for vendor-agreement recon.
- **Feeds Morning-brief (#19):** the **financial pulse** slice — cashflow risk + margin + MRR + at-risk invoices — as the Economy contribution to the cross-engine risk/opportunity query.

## Config keys
`NCE_ECONOMY_*` in `nce/config.py`: `NCE_ECONOMY_ENABLED`, `NCE_ECONOMY_FINAGO_URL`/`_TOKEN` (GL reader — secret), `NCE_ECONOMY_PEPPOL_*` (provider, sandbox/prod flag), `NCE_ECONOMY_MATCH_RECALIBRATE_AFTER_N` (default 100), `NCE_ECONOMY_BALANCE_EPSILON` (default 0.01), `NCE_ECONOMY_AUTO_ELIGIBLE_CEILING` (GREEN auto-eligible value gate), `NCE_ECONOMY_SYNC_INTERVAL_MINUTES`. Namespaces opt in via `metadata.economy.enabled = true`.
**Config-as-IP JSON (namespace-scoped — the business IP, NOT code):**
- `finago-chart-of-accounts.json` — the Norwegian chart of accounts (1531/2901/1771/4300…). Each tenant re-maps to its own country's plan.
- `finago-account-mapping.json` — account/MVA resolver (the periodisering targets). Swap config, keep logic.
- `economy-match-thresholds.json` — GREEN/YELLOW defaults (115/70) + per-supplier overrides.
- `economy-dunning-policy.json` — risk thresholds, dunning days (-3/+3/+10/+21), credit bureau (Bisnode → tenant's bureau), risk>60 HW-signing rule.

## Tables/migrations
**Graph-first.** INVOICE/POSTING/PERIOD/CONTRACT/MATCH/MARGIN live as `kg_nodes`/`kg_edges`; learning + close-narratives live in `v3_cognitive_ledger`. Own tables only where balance-integrity and fast period queries beat the graph:
- `economy_postings` (`id, namespace_id, event_id, event_type, line_no, account, amount, period_id, economy_source_id, change_origin, created_at`) — the balanced double-entry ledger. `amount` is a **single signed column, never a debit/credit pair** — a leg's debit/credit direction follows the SIGN of its amount (`ngaap.py`'s `do_compute_bucket_targets` convention; migration 048). The `UnbalancedPostingsError` guard enforces sum=0 at insert (backed by a storage-level trigger). `ENABLE` + `FORCE ROW LEVEL SECURITY` + `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`.
- `economy_contracts` (recurring-revenue master record, migration 049: `id, namespace_id, contract_id, status, annual_amount, start_period, cpi_cap, next_renewal_date, raw jsonb, created_at, updated_at`) — natural-keyed on `(namespace_id, contract_id)`; `do_upsert_contract` (`contracts.py`) is the SOLE writer, via `ON CONFLICT DO UPDATE` — a LIVE mutable record (status/annual_amount/next_renewal_date change over the contract's life), unlike `economy_postings`' append-only ledger. No separate `mrr` column (`annual_amount` is the single source of truth; MRR is computed on read) and no `finago_ref` column (`finagoRef = ms:{contractId}:{YYYY-MM}` is period-specific, not a fixed per-contract value — see migration 049's header comment for the full rationale). `ENABLE` + `FORCE ROW LEVEL SECURITY` + `tenant_isolation_policy`.
- `economy_sync_runs` (audit, mirrors `d365_sync_runs`) — ingest run history + EHF/OCR coverage report.
Mirror all DDL into `schema.sql` + numbered migrations.

## Dependencies
- **Upstream — Procurement(1):** the **boundary is the key contract.** Procurement owns the **PO×GR×invoice 3-way match + supplier scoring**; Economy owns the **130-pt INVOICE match + the 7-effect approval cascade + posting**. They are **distinct matchers — do NOT duplicate.** Economy consumes Procurement's `PO -[posted_to]-> INVOICE` edge + 3-way result.
- **Upstream — Project(7):** consumes the frozen `marginSignedPct` signed baseline (never overwritten).
- **Downstream — Agreements(3):** consumes Economy's GL reader for spend reconciliation (Economy **owns** the reader).
- **External blockers 🔴 (integration/policy, not technical):**
  - **Finago Normal-mode GL-commit is deliberately locked (CFO policy)** — Economy computes the correct internal numbers and mirrors the legal book, but does **not** post to the real GL yet. Draft/Validation paths run; the door opens by policy, not code.
  - **PEPPOL prod in/out pending provider** (Tickstar/Pagero) — sandbox-only today; EHF parser is a ~95% regex stub. EHF B2B becomes mandatory Jan 2027 — outbound `do_generate_ehf` ships behind the provider flag.

## Review round-2 hardening (2026-06-17 — these govern the build)
1. **"Mirror Finago, don't post" is PERMANENT divergence, not transitional (roadmap §9.2).** NCE runs two books — its computed/periodised internal numbers (drive cascade/margin/dunning) and Finago's legal GL — and they *will* diverge (a manual Finago journal NCE never saw; periodisation booked differently than predicted). Elevate `do_reconcile_gl` to **continuous reconciliation**: `economy_divergence_log` + **materiality thresholds + drift alerts** (built B1). **Truth-rule:** *Finago = legal system-of-record; NCE = authoritative for operational decisions.* This is the structural cost of the mirror decision.
2. **Economy owns `BOM_LINE.actual_cost` — name it (roadmap §9.1 worked example).** "The cascade is the ONLY place BOM-line cost is updated" *is* per-field write-authority: the cascade is the **sole writer of `actual_cost`** and writes nothing else on the line. This is the clean decomposition of the "5-writer `BOM_LINE`".
3. **130-pt vs Procurement's 3-way — say which is authoritative.** Distinct: **Procurement's 3-way = receiving/substitution** (goods vs order); **Economy's 130-pt = financial/posting** (invoice vs commitment) and is **authoritative for the cascade**. Economy **consumes** Procurement's 3-way verdict as an input — it does **not** recompute it; one invoice never carries two conflicting triage verdicts.
4. **OCR'd financial figures get the heaviest review gate of any AI in the suite.** A mis-OCR'd amount/PO-nr/account posts wrong cost. The EHF parser (structured, safe) is a **~95% regex stub**, so today the reliable path is incomplete and the *fallback* (Claude Vision OCR) is the unreliable one. **OCR-extracted figures are confidence-flagged + human-verified before they can drive a cascade — never auto-eligible from an OCR'd source** (§9.3; `GREEN`-auto-eligible applies only to structured/EHF-sourced matches).
5. **NGAAP is a Norwegian regnskapsloven §4-1 accrual ENGINE, not just account numbers.** "Swap the chart-of-accounts JSON per country" understates it — IFRS/US-GAAP accrual rules differ, so multi-jurisdiction is a real **engine-extension, not a config swap**. **Scope explicitly Norwegian-GAAP now; flag multi-jurisdiction as future work.**
6. **EHF mandatory Jan 2027 = hard regulatory clock (dated milestone), not "behind a flag."** ~18 months out; PEPPOL provider unselected; parser is a stub. Unlike the Netset Order API (nice-to-have), this is **legally mandatory by a fixed date** — track as a dated milestone with the **PEPPOL-provider decision as the gating dependency.**
7. **Margin-trinity = one owner per dimension (roadmap §9.1 `MARGIN`).** `signed` = Sales-frozen (immutable), `estimated` = Project, `actual` = Economy cascade. "Never overwrite `marginSignedPct`" is the immutability half of that ownership rule.

## Build phases
<!-- BLOCKED ON OQ-2 / OQ-4: Historical build phases B1-B5. Refer to docs/engines/economy-admin.md for shipped milestone status. -->
- **B1 — Pure cores + tests:** `do_match_invoice` (130-pt), `do_compute_bucket_targets` (NGAAP), `do_emit_financial_event` (balance-guarantee). Lift Andreas tests verbatim. Wire `economy-match-thresholds.json` + `finago-chart-of-accounts.json`/`finago-account-mapping.json`. MCP tools + REST routes for the three.
- **B2 — Cascade + graph:** `do_cascade_on_approval` (idempotent, single BOM-write-path, 7 effects, one transaction) + margin-trinity snapshot (`marginSignedPct` immutable). Graph upserts (INVOICE/POSTING/PERIOD/MARGIN, consume the Procurement boundary edge, `economy_source_id`). `economy_postings` table (RLS, sum=0 guard).
- **B3 — Ingest + reconciliation:** invoice ingest (EHF parser OR Claude Vision OCR) → `memories`; incremental watermark; Finago **GL reader** + `do_reconcile_gl` + sync status/coverage report.
- **B4 — Recurring revenue:** MRR/ARR/churn snapshot, ratable 1/12 recognition, idempotent recurring cron (`finagoRef`), contract-validator (CPI cap 5%), renewal-engine (90d scan). `economy_contracts` table.
- **B5 — Learning + forecast + dunning:** ledger-backed per-supplier recalibration (N=100, auditor-queryable); Monte Carlo cashflow; Norwegian dunning/credit policy (Bisnode → dunning aggression → HW-signing); KID (mod-10 shipped; mod-11 pending the bank-arrangement decision on which variant a creditor requires — `variant` param on `do_generate_kid`/`do_validate_kid` raises `NotImplementedError` for mod-11 until then) + outbound EHF behind the PEPPOL provider flag; close-narrative (cognitive-recall).
