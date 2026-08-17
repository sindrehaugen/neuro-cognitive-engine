> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 00b — Vertical-Engine Spec Review: Cross-Engine Gaps & Per-Engine Notes

<!-- BLOCKED ON OQ-2 / OQ-4: SPEC REVIEW CONTEXT. This document is a historical architectural review pass (2026-06-17) analyzing prospective engine specs and cross-engine seams. Features, tables, and tool names described herein reflect proposed design contracts. For shipped software reality at baseline 7304330, refer to docs/engines/ and docs/_generated/surface.md. Verified-against: 7304330 -->


**Status:** spec review · **Date:** 2026-06-17 · **Reviewer:** NCE core (Claude/Sindre review pass)
**Scope of this pass:** specs `05-sales`, `06-system-design`, `02-product`, `01-procurement`, `07-project` (all of Tier 1 + the two Revenue↔Delivery-bridge engines). Tier-2+ reviewed separately as the pass continues.
**Companions:** `00-ENGINES-ROADMAP.md`, `VERTICAL_MODULE_PATTERN.md`, the per-engine specs.

---

## Executive summary

The per-engine specs are **individually strong** — clean scope lines, pure-core lifts with tests, the on-demand-enrichment discipline, config-as-IP separation. **The risk is not inside any engine; it is in the seams between them.** Five engines in, the same failure shapes recur, and in **two cases two specs directly contradict each other** about who owns a shared thing.

The single highest-leverage action available is **not** another per-engine refinement — it is to write **two cross-engine contracts into the roadmap before any Tier-1 build batches start**:

1. **Shared-node ownership & lifecycle registry** — one owning engine per spine node, a defined status state-machine, one write-authority rule per transition.
2. **Autonomy governance** — promote Project's size-tier model and add idempotency + kill-switch + allowlist + rate-cap + ledger audit, applied to every Actor/Autonomous tool.

These two pieces de-risk every remaining engine more than any single spec edit.

---

## Cross-engine findings (the headline)
<!-- BLOCKED ON OQ-2 / OQ-4: Cross-engine contracts (Contract 1 node-ownership, Contract 2 autonomy governance) are architectural designs implemented across vertical modules and shared core; refer to docs/shared-core/ for current status. -->

### Concrete contradictions / unowned shared state

| Shared thing | Writers / owners as specced today | Problem | Proposed resolution |
|---|---|---|---|
| `BOM_LINE` | Sales, System Design, Procurement, Warehouse, Project, Assets (**6**) | No owner; `status` races; **Assets' 14-state ASSET lifecycle competes with `BOM_LINE.status` on the install path** | Name one owner + status state-machine; one writer per transition; define the hand-off point to `ASSET` |
| `SIGNED_BASELINE` | **Sales** (`do_freeze_signed_baseline` + `sales_signed_baselines`) **and Project** (`do_convert_signed_quote` + `project_signed_baselines`) both freeze | **Two tables for one immutable object** — opposite of the margin-trinity's "one signed truth" | **Sales owns the freeze at signature**; Project *reads* the frozen baseline, never re-creates it |
| Nettailer/Netset feed | **Product** (`sources/nettailer.py` + `product_prices`) **and Procurement** (`client.py` + `procurement_bid_prices`) both ingest | ~295 MB feed synced twice; two overlapping price tables; two staleness clocks | One ingest of record (Product owns catalog/SKU; Procurement owns supplier-prices/BID/orderlines), or shared core ingest |
| `FUNCTIONAL_LOCATION` | System Design (design *intent*) vs NetBox (*as-built*) | Spec says "pull tree from NetBox," but design happens for rooms NetBox doesn't have yet | Design **authors** tentative locations; NetBox bridge **promotes** them to as-built (promotion edge) |
| `PRODUCT` identity | dedup on `(manufacturer, mfr_part_no)` | False-merge + false-split at 552k scale; GTIN often absent on AV SKUs; silent graph poisoning | First-class entity-resolution subsystem + merge-review queue + field provenance |
| `MARGIN` (the trinity) | Project (`PROJECT -[has]-> MARGIN`, signed+estimated) **and** Economy (`ECONOMY_MARGIN`, actual via cascade) | Two engines touch margin; no owner per dimension | One owner per dimension: **signed** = Sales-frozen, **estimated** = Project, **actual** = Economy cascade |
| `SLA` (driftsavtale) | Agreements (terms) + Assets (per-room coverage) + Economy (MRR) + Support (clock/breach state) | **4-way** co-owned | Per-aspect owners: Agreements = contract terms; Assets = room-coverage link; Economy = MRR; Support = running clock + breach |
| `BOM_LINE.cost` (a field, not the node) | Economy cascade is "the ONLY place cost is updated" | *(this is the GOOD model)* — per-field write-authority done right | Adopt as the worked example for per-field ownership in Contract 1 |
| Autonomous writes (spend / sign / auto-act) | each engine invents its own gate | inconsistent safety; money/legal blast radius | Shared autonomy-governance pattern (below) |

### Contract 1 — Shared-node ownership & lifecycle registry (propose for roadmap §4)
For each spine node type (`BOM_LINE`, `SIGNED_BASELINE`, `QUOTE`, `PRODUCT`, `FUNCTIONAL_LOCATION`, `CUSTOMER`, `VENDOR`): **exactly one owning engine**, a **defined status state-machine** where the node has lifecycle, and a **write-authority rule per transition** (which engine may write which transition). Other engines reference and edge onto the node; they never re-create it or mutate fields they don't own. Project's *"edges onto, never re-creates"* rule is the model — make it the roadmap default. Pairs naturally with a **shared entity-resolution service** (candidate match → confidence → human-review merge → provenance), whose first and heaviest client is Product (it needs it twice: product identity dedup *and* BOM→SKU matching).

### Contract 2 — Autonomy governance (propose for roadmap §2 / §5)
Every engine has an Actor/Autonomous tier that writes to the world: Procurement **submits POs (spends money)**, Sales **requests BankID signatures**, Product **auto-writes specs**, Project **auto-creates/closes tasks & auto-advances phases**. Each currently invents its own gate. Promote a single shared pattern:
- **Value/volume bands** (Project's `automation-tiers.json` size-tier model is the best starting point — but size is *one* risk axis; add strategic-customer / first-of-kind / deadline flags).
- **Idempotency** (non-negotiable for external writes — no double-order/double-sign on retry).
- **Allowlist + rate/volume cap + kill-switch.**
- **Ledger audit** of every autonomous act (already the house style — make it mandatory for this tier).
Highest blast radius: Procurement auto-submit PO (real money) — ship **human-confirm-only first**, earn autonomy later behind the full gate.

### Recurring theme A — Grace-degradation (engines depend on not-yet-built engines)
A Tier-1 "done" engine often can't run on real data because an input-producing engine isn't live:
- **System Design** outcome-weighted recall needs **Project + Support** to have written outcomes (and the 450 historical projects live in Andreas's Prisma DB, not NCE's ledger → cold start).
- **Procurement** 3-way match needs **goods-receipt (Warehouse)** + **invoice (Economy/Finago)**.
- **Project** gate criteria ("BOM ordered", "PL assigned") need **Procurement** + **HR (Tier 4)**; auto-tasking needs status from Procurement/Warehouse/Field-Tech.
**Rule:** pure functions ship day one (they're pure over inputs); the *wired* feature degrades gracefully (criterion = "unknown/waived" + flag; recall by similarity-only until outcomes exist) so nothing deadlocks. State each engine's "works standalone vs needs-X-live" line explicitly.

### Recurring theme B — Don't bake in auto-trust percentages
"~80% auto / 20% human-validate" (System Design) and confidence-threshold auto-accept (Product enrichment, PDF→structured) are **marketing numbers from Andreas's concept**, applied to high-stakes outputs (BOM correctness, product specs). Ship **propose-only / human-confirms** first; let auto-accept thresholds *rise* only after the validation feedback loop (which every spec already captures) **measures** real override rates. The mechanism is right; the default posture must be conservative.

### Recurring theme C — One shared pricing service
DG-pricing (`salgspris = kostpris / (1 − DG%)`) and the inline-`*0.7` bug recur in Sales, System Design, and Product. There must be **one** pricing computation (`product_price` / `do_price_product`), consuming **resolved** cost (BID > supplier list > base, with a staleness signal). Sales/Design **call** it, never reimplement. A third copy = the bug again.

### Recurring theme D — Divergence audit as shared infrastructure
Three engines maintain a **mirror of an external source-of-truth and must reconcile**: Sales `both`-mode (NCE vs D365), Economy (internal books vs Finago GL), and the entity-resolution cases (NCE node vs source record). All need the *same* primitive — a **divergence/parity ledger with materiality thresholds + drift alerts**, and an explicit answer to *which side is truth when they disagree* (often: legal/source-of-record for compliance, NCE for operational decisions). Build it once in core; each engine configures thresholds. This is the operational complement to Contract 1 (ownership) — ownership says who *writes*; divergence-audit catches when the mirror *drifts*.

---

## Per-engine notes
<!-- BLOCKED ON OQ-2 / OQ-4: Per-engine notes reflect historical review findings against early drafts. Shipped vertical engine implementations differ in tool and route exposure per Surface of Truth. -->

### 05 — Sales (Tier 2, Revenue)
- **Core truth:** it's a *read-model-of-record* for the 12 `steps_d365` Lysning pages first, an AI engine last. B1 (read-model parity + source-mode resolver) is the whole game.
- `both` mode is a **two-master problem** — the resolver is specced as a read dispatcher but the hard part is reconciliation. Make `both` = NCE-primary with a **D365 parity check that logs divergence**; that audit log is the only honest basis for flipping a function to `nce`. Build it in B1.
- **Write-back is the deferred elephant.** Reps create/edit deals; the resolver must route **writes** too. Position: write-through to D365 while a function is `d365`/`both`, native-only once flipped — needs a collision-proof identity scheme (source-prefixed IDs + mapping).
- **Public surface = security boundary** (`api_sales_quote_public`/DealRoom): explicit field **allowlist** (not denylist), own rate-limited token path, possibly its own app (like the admin/BFF split). Margin/cost/commission must never leak.
- **Commission must be re-derivable** from `(append-only ledger + versioned sales-commission.json)` — *version* the config, not just store it.
- **Sequencing:** pull the signed-baseline **freeze/immutability mechanism** earlier than B3; signing integration (Scrive/Criipto) can lag, the freeze can't.

### 06 — System Design (Tier 1, Revenue↔Delivery bridge)
- **Codebase catch:** the existing `netbox` vertical is `circuits/contacts/discovery/mtbf/graphql_activation` — **no site/location/rack sync visible.** "Just bridge NetBox's tree" is likely false; functional-location sync may need building. Verify before sizing build-phase 2.
- **Location direction is backwards:** NetBox is *as-built* (populated after install); design happens for rooms that don't exist yet. **Design authors tentative `FUNCTIONAL_LOCATION` nodes; the bridge promotes them to as-built** — not "pull from NetBox."
- **Bidirectional = two-master again:** `DESIGN ⇄ QUOTE` keyed by functional location needs an explicit **ownership/lock per location-line** (design proposes until the quote pulls; once a quote line freezes toward signature, design loses write authority — the signed-baseline freeze is that hand-off point).
- **Outcome-weighted recall has a hard upstream dependency** on Project/Support (which may not exist) + cold start. Degrade: similarity-recall day one, outcome-weighting switches on as the ledger fills.
- **Don't bake in 80/20** — propose-only first, measure via `do_validate_design`.
- **Integrations off the critical path:** the recall→BOM→SoW core needs zero external systems; NetBox/SharePoint/Lucid are independently-sequenced adapters. Cut **Lucid import** (fuzzy diagram parsing) from early scope; keep export.
- **SoW lift:** the transform is free; the **input adapter** (graph → Andreas's `SoWInput` schema) is the work. SoW is a versioned legal deliverable → freeze on issue.

### 02 — Product (Tier 1, spine root)
- **The dedup key is the foundational risk** (see contradiction table) — entity resolution must be a measured, reviewable subsystem, not a key tuple. More important than any AI feature.
- **ETIM timing is the load-bearing ambiguity:** AV ETIM classes don't exist (you'd *build/maintain* an AV extension), and coded tuples are heavy. **Make ETIM coding on-demand** (code at quote/design entry) so the 552k ingest isn't blocked on a taxonomy you're still authoring.
- **Pricing under-models BID resolution + staleness** — resolution (BID>list>base) is where the `*0.7` bug breeds; a quote on stale cost = wrong margin. Explicit resolution fn + freshness signal; this *is* the shared pricing service.
- **On-demand enrichment must be fire-and-backfill, not synchronous** — a rep can't wait 30s for OCR/API. Return known data instantly, queue enrichment, backfill ("specs pending").
- **PDF→structured is R&D-grade**, not a feature — gate behind heavy human review until override rates prove calibration.
- **Distinguish "never bulk" (enrichment) from the search backfill (embedding 552k specs)** — different operations; the rule mustn't forbid the backfill.
- **`steps_product` is a grab-bag** (offer_*/oneflow/mailer/standards = Sales/Agreements). Make the *leave-behind* list as explicit as the lift list. The 9 "manufacturer-API adapters" are mostly **document-ingestion pipelines** (AV ships PDFs, not feeds) — reframe adapter model as *feed* vs *document* adapters.

### 01 — Procurement (Tier 1, quality-bar root)
- **⚠️ Biggest single risk in the suite, specced as a feature bullet:** step-5 ranking by **kickback-proximity** + the B5 **year-end rebate reallocation** optimize purchasing to maximize *the organization's supplier rebates*. "Crestron over the 4%-cheaper option to lock our +3% kickback" must survive an **audit and a customer reading it**; in cost-plus/public-sector/regulated deals this can be conflict-of-interest or fraud. Elevate to a **governed, compliance-reviewed decision** with a disclosure rule + a flag when rebate-proximity overrides best-TCO.
- **3-way match can't run on real data at launch** — needs goods-receipt (Warehouse) + invoice (Economy/Finago). Pure fn day one; wired match later (grace-degradation).
- **"Tests lifted verbatim" quietly forks the config-as-IP** — they assert *the organization's* weights/tolerances as code behavior; a tenant tuning weights breaks the suite. Split: algorithm tests (fixture weights) vs the organization's weight *values* (config, validated separately).
- **`warrantyCost=0` / `priceScore=3` are load-bearing holes, not cleanup** — closing them *changes rankings*; treat as a correctness milestone with before/after validation. Don't enshrine the placeholder by lifting tests verbatim.
- **Auto-submit PO spends real money** — value ceiling is necessary, not sufficient (idempotency + volume cap + allowlist + kill-switch). Ship human-confirm-only first (Netset Order API is a stub anyway).

### 07 — Project (Tier 1, delivery / loop-closer)
- **It's the data source for two other engines' recall** — outcome-writing must be **structured & attributable** (which line/product/design-decision caused the CO / margin erosion), not fuzzy narrative. First-class build phase.
- **`BOM_LINE` 5-writer race** (see contradiction table) — sharpest instance of the ownership gap.
- **Signed-baseline frozen in two places** (Sales + Project) — resolve to Sales-owns-the-freeze (see table).
- **Gate criteria leak cross-engine state** (`baseline`/`ordered`/`PL`) — pure check is fine, but `do_advance_phase` must gather cross-engine facts, with a degraded mode so gates referencing not-yet-built engines don't deadlock.
- **Size-tier autonomy is the best model in the suite** — but size is one axis, and it still lacks idempotency/kill-switch/audit. Promote it into Contract 2 as the value-band component.
- **Exemplar scope discipline:** "owns no BOM, edges onto shared nodes, never re-creates" — make it the roadmap default.

---

### 08 — Economy (Tier 2, Platform / deepest spec)
- **Exemplar boundary:** *periodise before GL-commit, mirror Finago, never replace the GL* (commit locked by CFO policy) — the best "ship-useful-without-the-blocker" framing in the suite; Procurement's PO-stub and Sales's signing-sandbox should copy it.
- **`reconcile_gl` is under-weighted to a feature.** "Mirror, don't post" means **two sets of books** that *will* diverge — and it's permanent, not transitional. Elevate to **first-class continuous reconciliation with a divergence ledger + materiality thresholds + drift alerts** (theme D), with an explicit truth-rule (Finago = legal; NCE = operational).
- **Economy owns `BOM_LINE.cost`** (cascade = the only cost write-path) — the worked example for Contract 1's per-field ownership. *Positive — make it explicit.*
- **130-pt vs Procurement 3-way seam is thin** — one invoice could get two triage verdicts. Define Procurement = receiving/substitution match; Economy = financial/posting match that **consumes** the 3-way result for the cascade, not recomputes it.
- **Claude Vision OCR on invoices = Product's PDF risk + money.** A mis-OCR'd figure can post wrong cost. OCR'd financial figures must be **confidence-flagged + human-verified before driving a cascade**, never auto-eligible. (EHF parser is the safe path but is a "~95% regex stub" today — the reliable path is the incomplete one.)
- **NGAAP is Norwegian-GAAP, not "swap the account JSON."** Accrual *logic* (regnskapsloven §4-1) is jurisdiction-specific IP; scope as NO-GAAP now, flag multi-jurisdiction as a real extension.
- **EHF mandatory Jan 2027 is a hard regulatory clock** (not a soft flag) — PEPPOL provider (Tickstar/Pagero) unselected, parser a stub. Track as a dated milestone.
- **Margin-trinity split across Project + Economy with no per-dimension owner** — resolve (signed=Sales-frozen / estimated=Project / actual=Economy). Contract-1 instance.
- **Scope discipline = the clearest of all eight specs:** not a GL replacement, distinct from Procurement's match, owns the GL reader (Agreements consumes it).

### 04 — Vendors & Contractors (Tier 2, Operations / thinnest + most-secure spec)
- **Cleanest counterparty boundary in the suite:** identity + reliability (Vendors) vs signed terms (Agreements) vs scoring + PO (Procurement) — three engines, one `VENDOR`, no overlap.
- **Proactively declares per-node ownership** (owns the `VENDOR`/`CONTRACTOR` *endpoint identity*; Field Tech writes assignment, Procurement writes PO) — voluntary Contract 1. Make explicit: the registry is the **sole creator** of `VENDOR`/`CONTRACTOR`; everyone else references.
- **Subscribes to feed-produced `VENDOR` upserts, does not re-pull** — practices the feed-ownership discipline (though the upstream Product-vs-Procurement Nettailer-owner question is still unresolved).
- **Partner Access Model = best security design in the suite:** 3 independent layers (sub-scope RLS, A2A tool-scoping, field-level **allow-list** redaction). The allow-list (not deny-list) is the **canonical model for every external surface**, including Sales's public quote — cross-reference them.
- **Push — Layer-1 is an NCE-*core* change, not vertical work:** needs a new `nce.partner_scope_id` session GUC + `get_nce_partner_scope()` + contractor-session setup, **defaulting to deny when unset** and ANDed with the tenant RLS policy. Core prerequisite with its own security-hardening pass.
- **Push — the kickback data originates here** (`in_tier`/`ytd_progress` + the "days-left vs ytd-pace" Watcher) — traces directly to the Procurement ethics risk; tier-proximity as a *ranking objective* needs the governance boundary, and tier data must be stripped from partner **and** customer-facing surfaces.
- **Push — third outcome-consumer** (contractor scores need Field Tech ratings, Tier 3 → no data until it ships): a sparse `sample_n` scorecard must be honored as **neutral by the consumer (Procurement)**, never as a low score. "Confidence flag respected downstream."
- **Push — "subscribes to upserts" assumes a reactive mechanism NCE lacks** (recurs in Project auto-task + Economy invoice-ingest) → shared-core item 5 below.
- **Scope discipline exemplary** — refuses to store the kickback *terms* (references Agreements).

### 03 — Agreements (Tier 2, Platform / closes the counterparty triangle)
- **Best multi-engine boundary in the suite (four-way kickback split):** Agreements owns signed **terms** + coverage, Economy owns **GL spend** (its reader), Procurement owns **scoring**, Vendors owns **counterparty identity**. All touch kickback; only Agreements holds the terms.
- **Defers the GL reader to Economy via A2A** (never re-implements) — same ownership discipline Vendors showed. Lifts a **real Portal sidecar** (`backend/agreement_sidecar/`), so lower-risk than greenfield.
- **`sourceDocRef` only — bytes stay in gitignored SharePoint/blob**; correct sensitivity handling.
- **Push — highest-consequence OCR in the suite; `auto_green` auto-promoting money fields is the risk.** Extracted `kickbackTiers`/`frameDiscountPct`/`paymentTermsDays`/`volumeCommitment` drive kickback reconciliation against **real GL**, Procurement scoring, and compliance verdicts — yet `auto_green` (≥90) auto-promotes with no human review, on *self-reported* (verbalized, uncalibrated) confidence. **Never auto-promote money-affecting legal fields regardless of confidence** — the gate sets review *priority*, not whether a human signs off. Sharpest instance of theme B.
- **Push (positive) — Agreements should OWN the kickback-ethics enforcement.** It's the only kickback-toucher holding the signed terms + running a compliance audit (`AIContractGuard`). Extend that audit into the **governance gate** for Procurement's rebate-maximization behavior — "is this within contract + policy?" This engine can *resolve* the suite's biggest ethics risk rather than add to it.
- **Push — the "spend WITHOUT agreement" coverage matrix is an entity-resolution problem.** It cross-joins Finago GL supplier-dimension ↔ agreement `supplierId` (matched on orgnr/name) ↔ Vendors `VENDOR`; if those don't reconcile it emits false leakage / false coverage. Another consumer of shared-infra item 1.
- **Push — new consolidation finding: shared signing service.** Sales and Agreements both integrate Scrive/BankID (two webhook handlers, two credential sets, two fingerprint impls). Candidate shared core signing service (one integration, one verification primitive, one `SignTransport`) — analogous to the shared pricing service (theme C). See shared-infra item 6.
- **Push — clarify `AGREEMENT_SIGNATURE` vs Sales's signed-baseline freeze.** A deal can have a signed quote (Sales → freezes `SIGNED_BASELINE`) and a customer SLA agreement (Agreements → SIGNED). State whether these share one signing ceremony or are distinct, to avoid double-signing / ambiguity over which signature freezes the baseline.
- **Scope discipline:** owns terms+coverage+extraction; defers GL reader (Economy) + counterparty identity (Vendors); Procurement consumes terms.

### 09 — Assets (Tier 3, Operations / extends the `netbox` vertical)
- **Reuses NetBox (incl. the existing `mtbf.py`) rather than duplicating DCIM;** mock-now/swap-ready telemetry adapters ship usable before any vendor key; **builds the healthScore *writer* Andreas left as a passive field** (the real value gap). SLA-per-ROOM = the recurring-revenue differentiator; `failure_pattern` edge → Product closes the service→product silence.
- **Push — same `FUNCTIONAL_LOCATION` foundation gap as System Design, confirmed in code** (the `netbox` vertical has no site/location tree — only circuits/contacts/discovery/mtbf/graphql_activation). Assets is the node's **3rd toucher** and exactly where the **design-intent→as-built promotion** model lands (a Field-Tech install promotes the intent location). Resolve the FL lifecycle as shared infra before either engine works.
- **Push — codebase catch:** an existing `backend/netbox-plugins/netbox_nce/` plugin (`signals.py`/`mcp_bridge.py`) likely already **pushes** NetBox changes into NCE. The spec models a *pull* bridge (copy of D365's). Investigate the plugin first — a signal push is the better reactive sync and partially answers the FL-sync gap + shared-infra item 5 (reactive mechanism).
- **Push — two competing state machines:** the 14-state ASSET lifecycle overlaps `BOM_LINE.status` (Project's auto-tasking) on the install path — define the hand-off (asset lifecycle begins where BOM-line delivery ends) or get two "installed" truths. See the updated `BOM_LINE` table row.
- **Push — fused healthScore has the sparse-input trap:** telemetry (mock until vendor keys), MTBF, Support tickets (Tier 3, absent), age — at launch it's effectively age-only but presented as a fused score. **Expose input coverage** ("80 — age-only, no telemetry"); predictive-failure must not fire on mock telemetry. Grace-degradation.
- **Push — `SLA` is a new 3-way co-owned node** (Agreements terms / Assets per-room coverage / Economy MRR) — see the new table row.
- **Scope discipline:** NetBox owns DCIM (rack/IP/cabling); Assets owns lifecycle/health/warranty/SLA-coverage/DPP.

### 10 — Support (Tier 3, Operations / least-greenfield — wraps the shipped D365 case track)
- **Builds on already-shipped NCE functionality** (`d365_query_case`/`case_stress_report`/`list_sla_breaches` + `ingest_case_note` + the Empathic Tensor) — generalizes proven code, doesn't rebuild ingestion. **AI Troubleshooter** (recall past tickets + their *resolutions*, cited) is the best cognitive-engine use in the suite; `do_resolve_ticket` closes the recall loop. Folds Customer Satisfaction in (consolidation, not a 15th engine).
- **Biggest finding — 2nd engine on the `d365|both|nce` switch ⇒ that resolver is shared infra** (see item 7). Same two-master / divergence / write-back questions as Sales; build it once in core (resolver + divergence audit + write-routing), not per engine.
- **Push — Troubleshooter recalls *cases* but maybe not *fixes*.** D365 case notes are unstructured; the value is **structured fix attribution** (which asset/product/firmware resolved it) — same as Project's outcomes. Prioritize resolution-capture early; quality ramps as the ledger fills (similarity day one, fix-recall later).
- **Push — auto-close is anti-mission if wrong** (it suppresses the dissatisfaction signal the engine exists to catch); auto-dispatch sends a real technician to a customer site. Most conservative default posture in the suite for auto-close; both through Contract 2.
- **Push — the customer-health/churn score** is sparse-at-launch (expose input coverage) AND customer-sensitive: never surface the raw churn score to the customer; the "recovery workflow" must honor "omsorg ikke overvåking" (hard guardrail); automated outreach = an autonomous customer-facing action → Contract 2.
- **Push — grace-degradation:** the reactive core ships on the shipped D365 seed alone; proactive / asset-health ticketing needs Assets (same tier) live.
- **Scope discipline:** consumes Assets, gets SLA terms from Agreements, dispatches to Field Tech (owns only the `dispatched_as` edge), wraps shipped D365 ingestion (no rebuild).

### 12 — Field Tech (Tier 3, Delivery / mostly confirms §9; offline-sync is the novel risk)
- **Mostly validates the contracts** — Partner Access Model (delegates to Vendors, §9.6), `WORK_ORDER` ownership (§9.1), `BOM_LINE -[installed_as]-> ASSET` hand-off (§9.1), autonomy (§9.5). Good convergence sign. Honest greenfield (no Andreas code to lift).
- **Push (engine-internal) — `do_sync` "last-writer-wins per field by device clock" is unsafe.** LWW is fine for a photo/note, but a stale offline replay can silently clobber a **safety/quality-critical field** (checklist verification, S/N scan); device clocks can't be trusted (skew/tamper). Order by **server-receive sequence or a logical/Lamport clock**, and **surface conflicts** on verification fields rather than silent LWW. The genuinely hard part; the spec undersells it.
- **Push — Contract-B idempotency must hold at *sync-replay* time**, not just creation: offline-generated autonomous acts (GPS auto-timesheet, auto-assign) are queued then replayed — the §9.5 idempotency key + autonomy gate apply across the sync boundary. `do_sync` is where Contract B meets the offline queue.
- **Push — the app↔engine sync protocol is a versioned external client-server contract** (op envelope + conflict protocol), not just a server endpoint — the one place NCE's pristine backend meets a bespoke client; version it like an API.
- **Push — grace-degradation:** WO create/capture/sync ship standalone; **dispatch ranking needs HR (Tier 4, last)** skills + Vendors performance, so it degrades to location+availability until they populate.
- **Confirms §9.6 item 4 load-bearing:** partner-scope RLS on all three tables (`work_orders`/`checklists`/`time_entries`) is most exercised here (a contractor on the mobile app) — build + harden the primitive in core before FT B4.
- **Note:** the offline-sync conflict-resolution risk is engine-internal → belongs in the `12` spec, with one cross-ref that Contract-B idempotency applies at replay.

### 11 — Warehouse & Inventory (Tier 3, Operations / greenfield; fills two Tier-1 dependencies)
- **The GR/`DELIVERED` producer that unblocks Procurement's 3-way match + Project's auto-tasking** (both flagged data-dependent earlier). `GOODS_RECEIPT` fires `procurement_evaluate_match` (Receive→Match→Cascade); "own stock first" reads `stock_levels`. Clean §9-aligned wiring + scope; keyed tables for atomic qty (right call).
- **Push (§9.1 catch) — `STOCK_LOCATION` should NOT be a `FUNCTIONAL_LOCATION`.** The spec says a van/warehouse *is* one, but FL is the **customer-site** tree (where assets live / SLAs apply); a van is a **company-internal logistics** location — a different ontology. Don't overload the customer-site node — make `STOCK_LOCATION` its own Inventory-owned type.
- **Push — dual representation (row-truth + graph-mirror) = internal divergence + atomicity hazard.** Declare the `inventory_items` row authoritative and the graph node an eventually-consistent projection; force stock-reads (Procurement "own stock first", forecast) to the row, not the possibly-stale node; lock the decrement (`UPDATE … WHERE qty >= n`) to prevent overselling. §9.2 divergence applied *inside* one engine.
- **Push — Tier-1-depends-on-Tier-3 inversion:** Procurement match + Project `DELIVERED`-tasking need this Tier-3 engine for a real signal — until it ships they run on manual/absent data. Flag grace-degradation, or consider pulling Inventory earlier (two Tier-1 consumers).
- **Push — compounding cross-engine autonomy:** Inventory auto-restock → Procurement auto-order → real spend (two §9.5 gates in series). Needs **end-to-end idempotency** (no duplicate PO requests on retry) + a **single audit trail** spanning both decisions. Contract B must cover compounding autonomy across boundaries.
- **Scope discipline:** Procurement owns PO/sourcing/match-algorithm; Inventory owns physical stock + GR event; Economy owns posting.

### 13 — HR (Tier 4, Platform / most-governed; assignment-infrastructure for Project + Field Tech)
- **NEVER-ranking as a legal floor** (EU AI Act Art-5 + Nordic red line), defense-in-depth (`NCE_HR_RANKING_DISABLED` hard-pinned) — a moat (incumbents' engagement-scoring = Art-5 exposure). Skills-matrix = operational infrastructure (A2A-queryable by Project/Field-Tech/Resources); multi-rater rates the *assertion*, not the person. Strictest PII posture (redaction gate as a blocking precondition). Real `hr_sidecar/` sidecar to lift.
- **🚨 Critical (legal) — spec body contradicted its own Art-5 fix.** AI-features line 80 still had the burnout signal as `load × consecutive-hours × low coaching-sentiment`, but `13a` §A4 already ruled emotion/sentiment inference non-compliant (EU AI Act Art-5, €35M/7%) and re-scoped it to a **"sustained-overload flag" from objective signals only**. The correction lived in the research section and was never propagated to the body. **Fixed in the `13` spec this session** (sentiment dropped). Lesson: propagate research-section legal corrections into the spec body.
- **New §9 candidate — people-data red line as a CROSS-ENGINE contract.** "Never aggregate person-data into comparative ranking / never infer emotion" must hold in **every** engine that reads HR (the #19 Morning-brief aggregate, Resources scheduling) — not just inside HR. Belongs in §9 alongside the money/legal hard rule + kickback-enforcement.
- **Push — sharpest sequencing inversion in the suite:** assignment-infrastructure (skills-matrix B1) is Tier-4, but Project PL-assignment (Tier 1) + Field-Tech dispatch (Tier 3) need it. **Decouple the skills-matrix slice and build it early**; leave coach/compliance/onboarding at Tier 4.
- **Push — `HR_SKILL`/`HR_CERT` is a shared vocabulary** (HR + Vendors-contractor): **HR owns the taxonomy; Vendors references it** (Contract-1 instance — shared-vocabulary ownership).
- **Push — PII redaction gate is regex-based → leaky for special-category (health) data.** Free-text 1-on-1 + sick-leave notes carry GDPR special-category data regex misses. Add an NER/classification pass, or **keep health/sick-leave notes out of the embedding store entirely** (access-scoped row only).
- **Scope discipline:** HR owns EMPLOYEE/skill/cert/absence; Vendors owns CONTRACTOR (shares the model); never ranks.

### 15 — Staff & Resources (Tier 3, scheduling capstone; best boundary discipline in the suite)
- **The boundary table (lines 108-115) is the best Contract-A artifact in any spec** — explicit owner-per-concern + Resources' narrower role ("HR owns who-they-*are*; Resources schedules *availability*"; "internal vehicles/tools live *here*, not Assets"; "Field Tech *executes*, Resources *plans*"). Make it the **template** other specs copy. Clean `RESOURCE` abstraction over employee/contractor/vehicle/tool; reads skills/certs through HR/Vendors (not re-owned); outcome-weighted allocation recall.
- **Push — capstone consumer (5 engines + Field Tech) → most grace-degradation-exposed, arguably mis-tiered.** Its AI planner (B3) needs HR skills/capacity — but HR is **Tier 4** and Resources is **Tier 3**, so the planner lands *after* its hard input. B1 registry/capacity works on manual data; planner needs HR+Vendors+ledger; material flow needs Inventory. State the capstone nature.
- **Push — new Contract-A node: the van is triple-role.** `VEHICLE -[also_is]-> STOCK_LOCATION` (Resources schedules, Inventory stocks), and Inventory wrongly called `STOCK_LOCATION` a `FUNCTIONAL_LOCATION`. Resolve: **Resources owns `VEHICLE`; Inventory references it as a stock-location; neither is a customer `FUNCTIONAL_LOCATION`.** Coordinates with the Inventory STOCK_LOCATION catch.
- **Push — allocation conflict = DB-layer atomicity** (like Inventory's stock decrement). App-level "conflict-checked" loses a concurrent-reservation race; use a **Postgres exclusion constraint** (`EXCLUDE USING gist` on a `tstzrange` per resource) so double-booking is impossible at the DB. Recurring discipline: **stock + schedule conflicts → DB constraints, not app-checks.**
- **Push — reinforces §9.6 reactive-event most of all** (a cert expiring in HR after a reservation silently invalidates the allocation; Resources is the heaviest consumer of *other engines' state changes*).
- **Push — travel-booking = autonomous spend → Contract B; per-diem (diett) = NO-jurisdiction compliance** (like NGAAP — taxable/non-taxable thresholds, not just rates).
- **Scope discipline:** the boundary table is the exemplar Contract A should cite.

### 14 — Marketing (Tier 4, Revenue / generative leaf; most-governed-by-design)
- **"Brand as Marketing" — harvest brand voice from delivered reality, zero net-new data, pure graph leaf** (reads all, writes only `MARKETING_*`, feeds nothing downstream). Correctly Tier-4-last. **NO Autonomous tier by design** (FTC-driven, non-tunable) — clearest human-gate posture. No-hallucinated-claims publish gate (every claim → a graph node); consent as structured scope/duration/revocation; anonymize-by-default.
- **Push — the no-hallucinated-claims gate must *shape generation*, not post-hoc verify.** Verifying LLM prose contains only graph-backed claims is itself a fallible AI task. Generate as claims-with-citations (retrieval-grounded assembly) so the draft is *constructed* from graph facts, not generated-then-checked. Generalizes to all generative output (close narratives, status reports).
- **Push — widest data-egress surface → enforce allow-list redaction at *draft* time, not publish.** A draft assembled from the graph holds real customer/site/margin data before anonymization; if stored or shown in review, it's already exposed internally. Anonymize at draft-assembly; margin/cost never enter a draft (allow-list, like Sales public-quote / Vendors partner-view). Makes external-surface allow-list a ≥3-consumer pattern.
- **Push — AI-citable publishing (AEO/GEO) vs right-to-retract is a genuine tension.** Content ingested by answer engines is effectively irrevocable; `marketing_source_id` retire un-publishes your CMS copy, not an LLM that trained on it. Consent for AI-citable content must be a higher, durable bar than for a retractable web page.
- **Push — testimonial-timing reads Support NPS/health** (sensitive): trigger only on high-NPS; never act on low health, never expose churn.
- **Scope discipline:** leaf, read-only from others, writes only `MARKETING_*`, no autonomy — exemplary; Tier-4-last genuinely correct.

### 16 — Business Insights (Tier 4, Executive surface / the promoted #19 aggregate; sharpest INTERNAL boundary)
- **The #19 Morning-brief promoted to a first-class pure-consumer / A2A-composition engine** — owns no operational state, all tools `admin_only`+`mutation=False`, no Actor/Autonomy by design, provenance on every claim, grace-degrades, respects HR no-ranking (aggregates only). The cross-engine RISK RADAR (collisions no single engine sees) is the §10 moat function.
- **Push — the no-ranking red line meets an *open NL query surface* (`ask_business`) — the hardest enforcement point.** An open NL surface is trivially phrased around an instruction-level rule ("avg resolution time *by technician*" = people-ranking). **Enforce structurally at the data-access layer** (person-grain comparison rows structurally unreturnable), not by LLM refusal.
- **Push — risk-radar findings shown to the *board* need a confidence/coverage indicator.** Ultimate capstone (depends on every engine's structured data); a wrong "ops is redlined" to the board erodes trust fast. Each finding: "based on N engines, M reconciled." Extends "expose input coverage" to the exec surface.
- **Push — "board's own AI" is a third-party-AI egress boundary** — an external LLM client connecting to NCE's MCP surface; financials flow to the board's AI vendor. Needs auth + rate-limit + full audit + explicit acceptance of the egress.
- **Scope discipline:** graph leaf, reads all / writes only `BUSINESS_INSIGHTS_*`, no autonomy (owns no state). Symmetric to Marketing as an internal read-leaf.

### 17 — Customer Portal (Tier 4, external surface / sharpest EXTERNAL boundary; security IS the build)
- **Read-projection + thin hand-offs; security is the build, features the easy part.** Owns only `PORTAL_USER`/`SERVICE_REQUEST`/`DOCUMENT_SHARE`; customer-principal RLS (generalizes §9.6), allow-list redaction (`customer-redaction.json`), separate rate-limited app (PR #241 split), prompt-injection-sandboxed AI, DPIA gate. Room-centric Domino's tracker over `BOM_LINE.status` + `ASSET`.
- **Push — generalizing §9.6 partner-scope to an *external customer* principal *escalates* the core primitive's security bar.** Three principal tiers now: employee (namespace) / contractor (partner-scope) / **internet-facing external customer** (adversarial — credential attacks, IDOR via the scope GUC, enumeration, session fixation). The §9.6 security-review must cover the adversarial-external threat model, not just contractors — and this is its second heavy client (after Field Tech), the more dangerous one.
- **Push — the messy-reality projection is the real design.** Status progression itself leaks (a room frozen at "Ordered" signals trouble; a *regressing* tracker exposes a CO/return). Define what the customer sees under delay/CO/partial-delivery/regression — not just the happy `Planned→…→Ready` path.
- **Push — the intake→ticket→customer-visible-status loop is a mini two-master:** define what the customer sees between "request raised" and "ticket opened," and how a Support close/merge/reject reflects back to their service-request view.
- **Push — DPIA is binary, not "likely":** build the security spine on synthetic/staff data in B1; the DPIA sign-off is a hard go/no-go before any real customer logs in.
- **Scope discipline:** owns 3 thin nodes; projects Project/Asset/Ticket/SLA/Invoice/Design; hands off writes — not a system of record for any operational node.

## Shared-core infrastructure surfaced (running tally)
The review keeps surfacing pieces that belong in NCE core, not in any one vertical:
1. **Node-ownership + entity-resolution registry** (Contract 1) — one owner per spine node + match/merge/provenance.
2. **Autonomy governance** (Contract 2) — value/volume bands + idempotency + kill-switch + allowlist + audit.
3. **Divergence-audit with materiality thresholds** (theme D) — for every NCE-mirror-of-external-SoT.
4. **Partner-scope RLS primitive** (security-critical; from Vendors' Partner Access Model) — `nce.partner_scope_id` GUC, default-deny, ANDed with tenant RLS.
5. **Reactive graph-event / trigger mechanism** — engines react to nodes other engines wrote (Project auto-task, Economy invoice-ingest, Vendors subscribe); assumed everywhere, specced nowhere.
6. **Shared signing service** (consolidation) — Sales + Agreements both integrate Scrive/BankID; one core integration + verification primitive + `SignTransport`, like the shared pricing service.
7. **`d365|both|nce` source-mode resolver** — Sales + Support both implement it (every future D365-sourced engine will too); a core service = resolver + divergence audit (theme D) + write-routing, not per-engine.
8. **People-data red line as a binding cross-engine rule** — never aggregate person-data into ranking / never infer emotion, in *any* engine reading HR (#19 Business Insights, Resources); **enforce at the data-access layer, not LLM-refusal** (the BI `ask_business` NL surface is the hardest case). [HR 13, BI 16]
9. **External-customer principal scope** — the §9.6 partner-scope primitive generalised to a 3rd, **internet-facing adversarial** tier (Customer Portal); the core security-review must cover it (IDOR/enumeration/session-fixation), not just contractors. [Portal 17]
10. **`VEHICLE`/van node ownership** (Contract-1 row) — Resources owns it; Inventory references it as a stock-location; it is **not** a customer `FUNCTIONAL_LOCATION`. [Resources 15, Inventory 11]
11. **DB-layer concurrency discipline** — stock decrements + schedule conflicts enforced by Postgres constraints (`UPDATE … WHERE qty>=n`; `EXCLUDE USING gist`), not app-level checks. [Inventory 11, Resources 15]
12. **Generation-grounding + retract/AI-citable governance** — provenance gates must *shape generation* (retrieval-grounded), not post-hoc verify; AI-citable content is effectively irrevocable → higher consent bar; external status-projection leaks via progression/regression (messy-reality design). [Marketing 14, Portal 17]
13. **Third-party-AI egress boundary** — board's-own-AI + customer AI connect *client-controlled* agents to NCE's MCP surface; auth + rate-limit + full audit + explicit acceptance that data leaves to the client's AI vendor. [BI 16, Portal 17]

## Recommended actions (priority order)
1. **Write Contracts 1 & 2 into `00-ENGINES-ROADMAP.md`** before any Tier-1 build batch. (Highest leverage; resolves both contradictions + the 5-writer race + unifies autonomy.)
2. **Resolve the two concrete contradictions** explicitly in the affected specs (signed-baseline owner; Nettailer feed owner).
3. **Spec a shared entity-resolution service** in NCE core (Product is first client).
4. **Spec a shared pricing/cost-resolution service** (Product owns; Sales/Design call).
5. Fold per-engine notes above into each spec's body (or link this doc from each).
6. Add a **"works standalone vs needs-X-live"** line to every spec (grace-degradation).

## Change log
- 2026-06-17 — Initial review pass over specs 01/02/05/06/07; cross-engine contradictions + two proposed contracts; per-engine notes.
- 2026-06-17 — Added 08-Economy: margin-trinity + GL-divergence rows to the table, recurring theme D (divergence audit as shared infra), Economy per-engine notes.
- 2026-06-17 — Added 04-Vendors per-engine notes + the "Shared-core infrastructure surfaced" running tally (5 items).
- 2026-06-17 — Added 03-Agreements per-engine notes; shared-infra item 6 (shared signing service). Tier 1+2 review complete (8 engines).
- 2026-06-17 — Added 09-Assets per-engine notes; SLA table row + BOM_LINE 6th-writer/competing-state-machine update.
- 2026-06-17 — Added 10-Support per-engine notes; shared-infra item 7 (d365|both|nce resolver); SLA row → 4-way.
- 2026-06-17 — Added 12-Field-Tech per-engine notes (mostly confirms §9; offline-sync conflict-resolution is the novel engine-internal risk).
- 2026-06-17 — Added 11-Inventory per-engine notes (greenfield GR/DELIVERED producer; STOCK_LOCATION≠FUNCTIONAL_LOCATION catch; row-vs-graph atomicity; Tier-1↔Tier-3 inversion; compounding autonomy).
- 2026-06-17 — Added 13-HR per-engine notes; flagged + FIXED the Art-5 spec-body bug (burnout→objective sustained-overload flag in the 13 spec); new §9 candidate = people-data red line as a cross-engine contract.
- 2026-06-17 — Added 15-Staff&Resources per-engine notes (capstone consumer; new Contract-A VEHICLE/van node; DB-layer concurrency discipline; reinforces reactive-event; travel-spend→Contract B). 14 of 15 reviewed.
- 2026-06-17 — Added 14-Marketing per-engine notes (generative leaf; no-hallucinated-claims gate must shape generation; widest data-egress → allow-list at draft-time; AEO-vs-retract tension; no Autonomy by design).
- 2026-06-17 — Added 16-Business-Insights per-engine notes (promoted #19 aggregate; pure-consumer leaf; no-ranking at the data-access layer not LLM-refusal; risk-radar confidence/coverage; board's-own-AI egress).
- 2026-06-17 — Added 17-Customer-Portal per-engine notes (external read-projection; §9.6 scope primitive now 3-tier incl. internet-facing customer; messy-reality projection; intake↔ticket two-master; DPIA binary gate). Extended shared-core tally to items 8-13. **All 17 engines now have per-engine notes.**
