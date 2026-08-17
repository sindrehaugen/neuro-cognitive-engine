> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 03 — Agreements Engine  (nce/vertical_modules/agreements)

<!-- BLOCKED ON OQ-2 / OQ-4: SPEC PROPOSAL VOICE. This document is an architectural design specification. At baseline 7304330, Agreements ships 1 registered MCP tool (agreements_lookup_terms) and 5 REST routes (see docs/_generated/surface.md). Unwired extraction/compliance cores and automated approval are blocked/deferred. Refer to docs/engines/agreements-user.md and docs/engines/agreements-admin.md for shipped reality. Verified-against: 7304330 -->


**Status:** spec (Tier 2 — Platform axis) · **Owner:** NCE core (Sindre)
**Pattern companions:** `docs/VERTICAL_MODULE_PATTERN.md`, `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§2 AI-role taxonomy, §4 graph catalogue, §7 spec format)

## Mission
Be both the place where agreements are **created** — licenses, customer contracts, vendor/supplier agreements, SLAs — and the **library** where every agreement is kept and its **core commercial terms are synthesised into the cognitive engine**. The deep-AI angle is the headline pattern from Andreas's recon blueprint (handoff 15), lifted near-1:1: a signed document (PDF/docx) is run through **Claude Vision OCR** into structured terms, then **confidence-gated** — high-confidence extractions auto-promote (`auto_green`), low-confidence ones land in a review queue (`needs_review_yellow` / `manual_red`). We never trust OCR of legal text blindly. Once extracted, terms become graph nodes + `memories`, so the engine answers "what are our payment terms with X", "which SLA covers this room", and "which counterparties have GL spend but no agreement on file" from auditable memory rather than a frozen spreadsheet. The engine owns the **agreement library, term extraction, and the coverage/gap matrix**; it consumes live GL spend from Economy for tier reconciliation but does not own the GL reader.

## Inspiration & triage
- **Andreas sources (the core blueprint — lift the pattern, not the contracts):**
  - `docs/handoff/15-leverandoravtale-recon-blueprint.md` — the whole spec: `SupplierAgreement` model, OCR→structured→confidence-review, coverage/gap matrix, kickback-tier reconciliation against **live** Finago GL (replaces the frozen `Leverandøravtaler_oversikt.csv`).
  - `lib/finance/supplier-invoices/ocr.ts` — the **reused** Claude Vision OCR engine (bilag module already has it); extraction is the same OCR→structured→confidence path.
  - `lib/procurement/contract-repository.ts` — frame-discount / volume / payment-term repository shape; `scoring.ts` + `savings-aggregator.ts` for the kickback-tier logic the recon feeds.
  - `lib/integrations/finago-gl.ts` (`getAccountBalances` / `fetchGlLines` with supplier dimension) — the live GL reader. **Owned by Economy(8) in NCE**; Agreements consumes it, never re-implements it.
- **Portal sidecar to lift:** `backend/agreement_sidecar/` — `contracts.py` (`ContractManager`, `NCEContract`, `ContractState` DRAFT→NEGOTIATION→SIGNING→SIGNED→ACTIVE→ARCHIVED), `negotiator.py` (`ContractNegotiator.suggest_revision`/`add_comment` → episodic memories), `compliance.py` (`AIContractGuard.run_compliance_audit` — discount-limit + SLA-clause checks), `signatures.py` (`ContractSigner` — SHA-256 fingerprint + signature event + `verify_memory`). These become `agreements/contracts.py` / `negotiator.py` / `compliance.py` / `signatures.py`, rewired off `MemoryPayload` onto the dual-surface core + graph upserts.
- **Lysning pages served:** `Avtaler.jsx` (the library — one row per agreement, two-layer status: signing Utkast/Ventende/Signert/Forfalt/Avslått · lifecycle Kommende/Aktiv/Avsluttet, four KPI cards), `AvtaleDetalj.jsx` (detail + revision/comment timeline), `NCEAgreement.jsx` (the Salg landing — Avtaler/Pipeline/Signering/Kunder/Rapport tiles), `Dokumenter.jsx` (source-doc surface). All consume the no-model REST surface.

## Classification
**push + semantic (OCR).** The headline transport is **document ingestion**, not an external API poll: a PDF/docx is pushed (upload or SharePoint/blob watcher) → Claude Vision OCR → structured terms → confidence gate. External systems: **SharePoint / blob** (source-doc store — commercially sensitive, see Sensitivity), **Claude Vision** (OCR, via the core cognitive engine — no separate API key here), **BankID / Scrive** (e-signature for created agreements). No OData/GraphQL master poll. The one **live** external read — actual GL spend — is **delegated to Economy(8)** via A2A, not a direct Finago client in this module. Resilience: `httpx.AsyncClient` (30s timeout) via `nce.http_resilience.request_with_retry()` for the SharePoint/blob fetch and the Scrive callback verification.

## Graph contribution
Node `entity_type` prefixes: `AGREEMENT_*`, plus shared spine nodes `AGREEMENT`, `VENDOR`, `CUSTOMER`, `PO`, `FUNCTIONAL_LOCATION`.
- **Nodes:** `AGREEMENT` (the extracted/created contract), `AGREEMENT_TERM` (a single extracted clause/tier with its own confidence), `AGREEMENT_SIGNATURE` (signer + SHA-256 hash + provider). `AGREEMENT` fields (from the blueprint model): `supplierId`/`customerId` (match on orgnr/name), `sourceDocRef` (SharePoint/blob ref — **gitignored, never in repo**), `validFrom`/`validTo`, `kickbackTiers[{threshold,pct}]`, `volumeCommitment`, `paymentTermsDays`, `frameDiscountPct`, `extractionConfidence` (0–100), `reviewStatus` enum (`auto_green`/`needs_review_yellow`/`manual_red`), `lifecycleState` (DRAFT…ARCHIVED).
- **Edges (the §4 contract, our slice):**
  - `VENDOR -[under]-> AGREEMENT` and `CUSTOMER -[under]-> AGREEMENT` (counterparty binding).
  - `AGREEMENT -[covers]-> FUNCTIONAL_LOCATION` (the SLA-per-room edge — answers "which SLA covers this room", joins to Assets/Support).
  - `PO -[from]-> VENDOR -[under]-> AGREEMENT` (closes the §4 procurement chain; Procurement writes `PO -[from]-> VENDOR`, Agreements supplies the `-[under]->` term context).
  - Every derived edge carries `confidence` (0–1, from the 0–100 extraction confidence) and an `agreements_source_id` for hard-retirement on delete.
- **memories/ledger:** full agreement text → `memories` (embedding + `content_fts`) so the engine answers term/SLA questions semantically. Every extraction, revision, compliance audit, and signature → `v3_cognitive_ledger` (this is where the lifecycle event-stream lives — generalising the Portal `agreement_sidecar_system` episodic-memory pattern onto the ledger). Raw OCR JSON → MongoDB before indexing.

## Core functions
<!-- BLOCKED ON OQ-2 / OQ-4: do_extract_agreement, do_coverage_matrix, do_reconcile_kickback, do_create_agreement, do_suggest_revision, do_run_compliance_audit, do_request_signature, do_record_signature, do_review_extraction are internal domain functions; only term lookup is exposed via MCP. -->
Pure-ish `do_<action>(engine, params) -> dict`; extraction and reconciliation cores isolate the AI call from the math.
- `do_extract_agreement(engine, params) -> dict` — `{source_doc_ref}` → run Claude Vision OCR → structured `AGREEMENT` fields + per-field `extractionConfidence` + a `reviewStatus` from the confidence gate. Writes the `AGREEMENT`/`AGREEMENT_TERM` nodes, the source text to `memories`, the event to the ledger. The headline pattern.
- `do_coverage_matrix(engine, params) -> dict` — namespace → per-counterparty matrix: has-agreement? tiers? volume? payment? discount? valid? Cross-joins Economy GL spend to flag **spend WITHOUT agreement** (money leaking), expiring/expired agreements, and low-confidence extractions in the review queue. Pure over graph + the GL slice it asks Economy for.
- `do_reconcile_kickback(engine, params) -> dict` — `{counterparty, period}` → kickback-tier progression against **real GL spend** (asks Economy for `fetchGlLines`, not internal projection), earned-kickback-to-date = f(actual GL spend × active tier), "X kr to next tier", and drift vs any internal projection. Agreement terms = static (extracted); spend = live.
- `do_create_agreement(engine, params) -> dict` — instantiate a DRAFT from template + catalog items (lifts `ContractManager.create_from_template`); Actor — writes graph, no signature yet.
- `do_suggest_revision(engine, params) -> dict` / `do_add_comment(engine, params)` — advance DRAFT→NEGOTIATION, record the exact edit/comment to the ledger (lifts `ContractNegotiator`).
- `do_run_compliance_audit(engine, params) -> dict` — discount-limit + SLA-clause checks (lifts `AIContractGuard`); thresholds from config-as-IP.
- `do_request_signature(engine, params) -> dict` / `do_record_signature(engine, params)` — issue a BankID/Scrive signing request; on callback, SHA-256-fingerprint the content, transition to SIGNED, write `AGREEMENT_SIGNATURE` + `verify_memory` (lifts `ContractSigner`).
- `do_review_extraction(engine, params) -> dict` — human accept/correct a `needs_review_yellow`/`manual_red` extraction; promotes to `auto_green` and re-upserts the corrected terms.

## MCP tools
<!-- BLOCKED ON OQ-2 / OQ-4: Historical proposal listed 10 tools. Baseline 7304330 registers exactly 1 MCP tool: agreements_lookup_terms. The other 9 tools remain unbuilt/unwired in TOOL_REGISTRY. -->
Registered in `nce/tool_registry.py` via `_h(...)` late-binding. AI-role tag per roadmap §2 taxonomy.

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `agreements_coverage_matrix` | ✔ | ✘ | ✘ | Advisor |
| `agreements_reconcile_kickback` | ✔ | ✘ | ✘ | Advisor |
| `agreements_lookup_terms` | ✔ | ✘ | ✘ | Watcher |
| `agreements_run_compliance_audit` | ✔ | ✘ | ✘ | Advisor |
| `agreements_extract` | ✘ | ✔ | ✔ | Actor (Advisor on review-needed) |
| `agreements_create` | ✘ | ✔ | ✔ | Actor |
| `agreements_suggest_revision` | ✘ | ✔ | ✔ | Actor |
| `agreements_request_signature` | ✘ | ✔ | ✔ | Actor |
| `agreements_record_signature` | ✘ | ✔ | ✔ | Actor |
| `agreements_review_extraction` | ✘ | ✔ | ✔ | — (operator) |

## REST routes
<!-- BLOCKED ON OQ-2 / OQ-4: Mounted REST routes at baseline 7304330 are /api/agreements, /api/agreements/coverage, /api/agreements/{id}, /api/agreements/extract, /api/agreements/review. Routes for reconcile, create, request_signature, record_signature are not mounted. -->
No-model path for the BFF (`Avtaler.jsx`, `AvtaleDetalj.jsx`, `Dokumenter.jsx`), cron, scripts. Mounted via `build_app(extra_routes=...)`; HMAC/mTLS-authed in `nce/admin_handlers/agreements.py`:
- `api_agreements_list` (GET) — the library (one row per agreement, two-layer signing×lifecycle status + the four KPI cards Avtaler.jsx renders).
- `api_agreements_detail` (GET) — agreement + extracted terms + revision/comment/signature timeline (AvtaleDetalj).
- `api_agreements_coverage` (GET) — coverage/gap matrix + leakage/expiry/review-queue flags (the dashboard).
- `api_agreements_reconcile` (POST) — kickback reconciliation for a counterparty/period.
- `api_agreements_extract` (POST) — push a source doc to OCR extraction (Dokumenter upload).
- `api_agreements_review` (POST) — review-queue accept/correct.
- `api_agreements_create` / `api_agreements_request_signature` / `api_agreements_record_signature` — create + sign lifecycle (Scrive/BankID callback lands on `record_signature`).

## AI features
- **Watcher:** expiring/expired-agreement alerts (`validTo` race); **GL-spend-without-agreement leakage** detection (live GL × coverage matrix); low-confidence-extraction review-queue alerts.
- **Advisor:** the **AI negotiator** — suggests terms vs benchmark (peer agreements + ledger history: "your payment terms with X are Net 30; comparable vendors run Net 60 — propose extending"); coverage-gap recommendations ("3 counterparties have spend but no agreement on file"); compliance audit with plain-language exceptions (discount > limit, 24/7 vs standard SLA).
- **Actor:** create agreement, suggest revision, request + record signature — all *with confirmation*. Extraction is Actor when confidence is high (auto-writes terms), demoting to Advisor (review queue) when low.
- **Cognitive recall:** term/SLA questions answered from `memories` ("which SLA covers room 4.12", "what's our frame discount with Crestron"); the lifecycle/why-did-a-term-change history read from `v3_cognitive_ledger`.
- **Enrichment triggers (event-scoped, never a background sweep):** OCR/extraction fires only when a doc is uploaded or a SharePoint/blob change is observed; reconciliation fires on demand or on a period close; negotiator runs only when a draft is opened or an audit flags a gap. Never bulk-re-OCR the library.

## A2A flows
- **Serves Procurement(1):** supplies extracted terms (tiers, frame discount, payment days, volume commitment) for supplier scoring — Procurement *consumes* terms, Agreements *owns* them.
- **Initiates reconciliation against Economy(8):** calls Economy's GL-line tool for actual spend per counterparty (kickback progression + leakage), never reading Finago directly.
- **Serves Assets/Support (Install→Asset→Cover):** answers "which SLA covers this functional location" via the `AGREEMENT -[covers]-> FUNCTIONAL_LOCATION` edge when a ticket/asset needs its SLA clock.
- **Feeds Morning-brief (#19 aggregate):** exposes expiring agreements + leakage + earned-vs-expected kickback as the agreements slice of the cross-engine risk/opportunity query.

## Config keys
`NCE_AGREEMENTS_*` in `nce/config.py`: `NCE_AGREEMENTS_ENABLED`, `NCE_AGREEMENTS_SHAREPOINT_URL` / `NCE_AGREEMENTS_BLOB_URL` (source-doc store — **secret, never logged**), `NCE_AGREEMENTS_OCR_AUTOGREEN_THRESHOLD` (default 90 — ≥ → `auto_green`), `NCE_AGREEMENTS_OCR_REVIEW_THRESHOLD` (default 70 — between → `needs_review_yellow`, below → `manual_red`), `NCE_AGREEMENTS_SIGN_PROVIDER` (`bankid`|`scrive`), `NCE_AGREEMENTS_SCRIVE_*` (token/callback secret), `NCE_AGREEMENTS_EXPIRY_WARN_DAYS` (default 60). Namespaces opt in via `metadata.agreements.enabled = true`.
**Config-as-IP JSON (namespace-scoped — NOT code):**
- `agreement-compliance-rules.json` — `max_discount_limit` (default 15%), standard SLA hours, restricted-clause list (the `AIContractGuard` thresholds per tenant).
- `agreement-benchmark.json` — peer payment-term / discount / tier benchmarks the negotiator advises against.

## Tables/migrations
**Graph-first.** `AGREEMENT`/`AGREEMENT_TERM`/`AGREEMENT_SIGNATURE` live as `kg_nodes`/`kg_edges`; lifecycle/extraction/audit/signature events live in `v3_cognitive_ledger`; agreement text in `memories`; raw OCR in MongoDB. Own tables where a keyed lookup or queue beats the graph (each `ENABLE` + `FORCE ROW LEVEL SECURITY` + `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`, mirrored into `schema.sql` + numbered migration):
- `agreement_review_queue` (`agreement_id, source_doc_ref, extraction_confidence, review_status, extracted jsonb, flagged_at, reviewed_by, reviewed_at`) — the low-confidence review surface.
- `agreement_extraction_runs` (audit, mirrors `d365_sync_runs`) — per-doc OCR run history + confidence + outcome.
- **No `sourceDocRef` content table** — only the ref/pointer; the document bytes stay in gitignored SharePoint/blob (see Sensitivity).

## Dependencies
- **Upstream engines:** Vendors(4) — counterparty master-data + the kickback-tier home (Agreements binds terms to a `VENDOR`); Product(2) for catalog items on created agreements.
- **Boundary — Economy(8):** Economy **owns the Finago GL reader** (`finago-gl.ts` → live spend). Agreements **owns the agreement library + term extraction + coverage/gap matrix** and *asks* Economy for actual spend. Do NOT re-implement the GL reader here. Procurement(1) **consumes** terms for scoring but does not store them. This three-way boundary (Agreements = terms & coverage, Economy = GL spend, Procurement = scoring) is the one to keep crisp — all three touch kickback, only Agreements holds the signed terms.
- **External blocker 🔴:** e-signing credentials + the SharePoint/blob watcher are environment-provisioned — `do_request_signature`/`do_record_signature` call the **shared signing service** (roadmap §9.6), `SignTransport` = (`oneflow` | `criipto`/`signicat` | `manual`), so extraction + coverage + reconciliation work fully without them; only live e-signing waits on the credential. **Oneflow is the CLM backend for agreements *we author***: create-from-template → negotiate (live HTML contract) → BankID-sign → archive → lifecycle (`lifecycle_state` renewal/expiry events feed the coverage/renewal timeline natively). **Native OCR stays for *incoming* third-party contracts** — Oneflow can't blind-extract an arbitrary supplier PDF (it maps known data fields), so authored→Oneflow and incoming→OCR are **both needed** (Portal ADR 0023: mirror Oneflow core 1:1, wrap value around it; mirror already exists at `steps_product/oneflow.py`).

## Review round-2 hardening (2026-06-17 — these govern the build)
1. **This is the highest-consequence OCR in the suite — `auto_green` must NOT auto-promote money/legal fields (roadmap §9.3 money/legal rule).** The extracted terms (`kickbackTiers`, `frameDiscountPct`, `paymentTermsDays`, `volumeCommitment`) directly drive kickback reconciliation against **real GL money**, Procurement's ranking, and compliance verdicts — a mis-OCR'd tier threshold = a wrong rebate claim. `auto_green` (≥90) is **self-reported/verbalized confidence, not logprob-calibrated** (Product A4). Binding: the confidence gate sets **review *priority*, never sign-off** — **a human signs off on money/legal fields regardless of score** before they reconcile/rank/post.
2. **Agreements is the ENFORCEMENT POINT for the suite's kickback-ethics risk (turn the problem into a solution).** It is the only kickback-toucher holding the **signed terms** *and* running a compliance audit. Extend `do_run_compliance_audit` (AIContractGuard) into the **governance gate** Procurement's `rebate_override` (Procurement hardening #1) is checked against over A2A: *"is this rebate-chasing within the signed contract and within policy?"* — answerable here because the terms live here. (Roadmap §9.6 enforcement-point note.)
3. **The "spend-without-agreement" coverage matrix is an entity-resolution problem in disguise (roadmap §9.4 consumer).** It cross-joins **Finago GL supplier-dim ↔ agreement `supplierId` (matched on orgnr/name) ↔ Vendors' canonical `VENDOR`**. If those three "who is this supplier" notions don't reconcile, it emits **false leakage** ("no agreement!" when one exists under a slightly different name) or false coverage. Its accuracy depends entirely on the **shared entity-resolution primitive** — use it, don't fuzzy-match locally.
4. **Two signing systems now exist (Sales + Agreements) → use the shared signing service (roadmap §9.6).** Sales has `request_signature` (Scrive/Criipto) for quotes; Agreements has `request_signature`/`record_signature` + SHA-256 fingerprint + `AGREEMENT_SIGNATURE` for contracts — overlapping providers, two webhook handlers, two credential sets, two fingerprint impls. Both call **one shared signing service** (one integration, one verification primitive, one `SignTransport`) — analogous to the shared pricing service.
5. **Clarify `AGREEMENT_SIGNATURE` vs Sales's signed-baseline freeze (one ceremony or two?).** A customer deal can carry a signed quote (**Sales → freezes `SIGNED_BASELINE`**) *and* a customer service/SLA agreement (**Agreements → `SIGNED`**). The spec must state whether a customer agreement **shares the deal's signature event** or is **distinct** — and **which signature freezes the baseline** — to avoid double-signing/ambiguity. **Vendor agreements are cleanly separate; only customer agreements overlap Sales.**

> **Scope exemplar (hold it):** the **four-way kickback boundary** — Agreements owns *terms + coverage*, Economy owns *GL spend* (its reader), Procurement owns *scoring*, Vendors owns *counterparty identity*; all four touch kickback, only Agreements holds the signed terms — is the cleanest boundary articulation in the suite.

## Build phases
<!-- BLOCKED ON OQ-2 / OQ-4: Historical build phases B1-B5. Refer to docs/engines/agreements-admin.md for shipped milestone status. -->
- **B1 — Extraction core + library:** `do_extract_agreement` (Claude Vision OCR → structured + confidence gate), `agreement_review_queue` + `agreement_extraction_runs` tables (RLS), `do_review_extraction`. `AGREEMENT`/`AGREEMENT_TERM` graph upserts + `agreements_source_id` + text→`memories`. `api_agreements_list`/`detail`/`extract`/`review`.
- **B2 — Coverage/gap matrix:** `do_coverage_matrix` cross-joining Economy GL (A2A) — leakage, expiry, review-queue flags. `agreements_lookup_terms` + the coverage REST/dashboard. Watcher alerts (expiry, leakage).
- **B3 — Reconciliation:** `do_reconcile_kickback` against live GL (earned-to-date, X-to-next-tier, drift). Ledger-backed term-change history. Feeds Procurement scoring + Morning-brief.
- **B4 — Create + negotiate + compliance:** `do_create_agreement`, `do_suggest_revision`/`do_add_comment`, `do_run_compliance_audit` (lift the four `agreement_sidecar` modules onto the dual-surface core). AI negotiator (Advisor vs benchmark).
- **B5 — Signing lifecycle:** `do_request_signature`/`do_record_signature` with `SignTransport` adapters (scrive/bankid live, manual stub), SHA-256 fingerprint + `AGREEMENT_SIGNATURE` node + `verify_memory`; the `covers FUNCTIONAL_LOCATION` SLA edge for Assets/Support.
