> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 99 — Shared-Core Foundation (build BEFORE the engines)

<!-- BLOCKED ON OQ-2 / OQ-4: SPEC PROPOSAL VOICE. This document is a foundational architectural design specification. Refer to docs/shared-core/ and docs/vertical_engines/ENGINE_STATUS.md for shipped reality at baseline 7304330. Verified-against: 7304330 -->

**Status:** foundation build plan · **Owner:** NCE core (Sindre) · **Date:** 2026-06-17
**Companion:** `00-ENGINES-ROADMAP.md` §9 (the two contracts + the standing tally), §2.10/§2.11 conventions, §9.3 structural-enforcement.

## 0. Why this exists

The 17-engine review converged on one conclusion: **each engine spec is individually strong; the risk and the reuse both live in the seams.** §9 turned those seams into binding **contracts** and a **standing tally** of shared infrastructure. This doc turns that tally into **buildable NCE-core components, sequenced before the engines that depend on them** — so we never build the same fuzzy-match / autonomy-gate / source-resolver / redactor twice, and so the Tier-1 engines have solid ground to stand on.

**Rule:** a component here is **built once in `nce/` core**, has a stable interface engines *call*, and is **security-/hardening-reviewed on its own** — it is not a vertical feature. Engines reference these; they never reinvent them.

NCE primitives these build on (already exist): `kg_nodes`/`kg_edges`, `memories` (embeddings + `content_fts`), `v3_cognitive_ledger`, `scoped_pg_session` + `get_nce_namespace()` RLS, `nce.http_resilience`, `build_app(extra_routes=…)` (FE-1), `register_tool()` (FE-2), `TOOL_REGISTRY`, the `dynamics365` watermark/delta + `*_source_id` retirement pattern, and the existing `netbox-plugins/netbox_nce` push plugin.

---

## 1. Component catalogue

Nine components. Each: **what · interface · consumers · depends-on · done-when**.

### C1 — Entity-resolution & node-ownership registry  *(Contract A §9.1 + §9.4)*
- **What:** the single match→confidence→**human-review merge queue**→provenance primitive, plus the machine-readable **ownership/lifecycle registry** (who may write each shared node + per-transition writer-of-record).
- **Interface:** `resolve(candidates, keys, normalizers) -> ranked matches + confidence`; a `merge_review` queue (sub-threshold merges held for human confirm — **distinct from enrichment-confidence review**; an identity false-merge silently + permanently poisons the graph); field-level survivorship (source-trust > recency > confidence) with provenance; normalization maps as config-as-IP (e.g. manufacturer-name). The ownership registry is a typed table the write-path consults so a non-owner write is rejected at the core, not per-engine.
- **Consumers:** Product (golden-record dedup **and** BOM→SKU — first/heaviest client), Sales (deal identity), System Design (functional-location intent vs as-built), Procurement (feed identity), Agreements (coverage-matrix supplier reconciliation).
- **Depends on:** graph + ledger (exist).
- **Done-when:** a proposed merge below threshold lands in the queue (never auto-merges); survivorship + provenance auditable in the ledger; a cross-engine write to a non-owned node is refused.

### C2 — Autonomy-governance wrapper  *(Contract B §9.5)*
- **What:** one wrapper every **Actor/Autonomous** tool passes through; **default = human-confirm-only**, autonomy earned per-tool from measured precision.
- **Interface:** decorate a mutating handler with `{value_ceiling, volume_rate_cap, counterparty_allowlist, risk_flags(flagship|first_of_kind|regulated), idempotency_key, kill_switch}`; every act → `v3_cognitive_ledger`. **Idempotency holds at sync-replay** (Field Tech offline queue) and **across engine boundaries** (compounding chains — Inventory auto-restock → Procurement `submit_po`: one end-to-end key + one spanning audit trail).
- **Consumers:** Procurement `submit_po` (sharpest — real money), Sales signing, Product enrich, Project `sync_bom_tasks`/`advance_phase`, Inventory restock, Resources reserve, Field Tech dispatch, Support auto-close/dispatch, Customer-Portal/Marketing customer-facing acts.
- **Depends on:** ledger; ties into `nce:tools:disabled` (kill switch).
- **Done-when:** no world-writing tool can self-trigger without a ceiling + idempotency key + ledger entry; a replayed/duplicated act is a no-op.

### C3 — External-principal RLS primitive  *(§9.6 — security-critical)*
- **What:** row isolation **below** the namespace, across **three principal tiers of escalating threat: employee (namespace) · contractor (partner-scope) · external customer (internet-facing, adversarial).**
- **Interface:** session GUC `nce.external_scope_id` (principal-kind + scope-id) + `get_nce_external_scope()`; `external_isolation_policy USING (namespace_id = get_nce_namespace() AND external_scope_id = get_nce_external_scope())`; **defaults to DENY when unset**, **ANDs with** the tenant policy; principal-session setup in `admin_app`/`a2a_server`/the portal app.
- **Consumers:** Vendors (contractor restricted-access), Field Tech (contractor mobile — most-exercised), **Customer Portal (external customer — the more dangerous, raises the bar)**.
- **Depends on:** existing RLS (`get_nce_namespace`).
- **Done-when:** its own **security-review covers the adversarial-external threat model** (IDOR via the GUC, enumeration, session fixation), deny-when-unset proven, contractor + customer sessions both scoped.

### C4 — Reactive graph-event / trigger bus  *(§9.6)*
- **What:** the "engine B reacts to a node engine A wrote" mechanism every spec assumes and none defines. Decide **once**: an in-process event/trigger bus **or** a documented polling/watermark convention.
- **Interface:** publish on graph-write (`node_type, op, id, namespace`) → subscribers (`on(event) -> handler`); at-least-once + idempotent handlers (pairs with C2). **⚠️ Corrected target (audit `98`): the real substrate already exists IN CORE — a transactional outbox `nce/outbox_relay.py` + `outbox_events` + DLQ + an unused `processed_outbox_events` dedup table + 2 live producers (cron-driven, post-commit). C4 is ~60% built (re-baseline effort down to M-minus).** Generalise from the **outbox** (add subscribe/publish + **emit on graph-write** — kg upserts emit nothing today — + wire the dedup table). The `netbox_nce` Django push plugin is the **WRONG** target (hardcoded namespace, `asyncio.run`-in-signal = the parallel bridge to avoid).
- **Consumers (≥6, Resources most acutely):** Project (BOM-status→auto-task), Economy (invoice-ingest), Vendors (feed upserts), Assets (install→promote location; telemetry→ticket), Support (health-drop→ticket), Resources (HR cert-expiry silently invalidates a reserved future allocation).
- **Depends on:** graph write-path.
- **Done-when:** a write in A reliably + idempotently fires B's handler; the cert-expiry→allocation-invalidation case works without polling.

### C5 — `d365|both|nce` source-mode resolver + divergence audit  *(§9.2 + §9.6, subsumes theme D)*
- **What:** the read **and write** dispatcher + incremental retention + continuous reconciliation, as **one service** (not per-engine). `both` = NCE-primary + parity check; **every divergence → `<engine>_divergence_log`** (materiality + drift alerts); flip `→nce` only when the log is clean over a window; writes route write-through-while-`d365`/`both`, native-once-flipped; source-prefixed identity.
- **Interface:** `resolve(engine, function, namespace) -> mode`; `read_through(...)`, `write_route(...)`, `record_divergence(...)`; per-pairing **truth-rule** (D365: NCE-primary; Finago: legal SoR / NCE-operational).
- **Consumers:** Sales (the headline, B1), Support (cases), Economy↔Finago (the reconciliation half), every future D365-sourced engine.
- **Depends on:** the watermark/delta + `*_source_id` pattern (exists in `dynamics365`).
- **Done-when:** a function serves in all three modes; the divergence log is the gate for flipping; a write in `both` reaches D365 and NCE without collision.

### C6 — Shared pricing service  *(consolidation §9.6)*
- **What:** DG-pricing `salgspris = kostpris/(1 − DG%)` lives **once**; kills the inline `*0.7` and any 2nd/3rd copy. Includes **price resolution** (`customer BID > supplier list > base`) + a **freshness signal**.
- **Interface:** `resolve_price(product, customer) -> {cost, source, as_of}`; `dg_price(cost, dg_pct) -> sales_price`; DG% from namespace config-as-IP (`product-dg.json`). Cost/margin never cross to a customer-facing surface (ADR-0017).
- **Consumers:** Product (the engine), Sales (DealRoom), Procurement (scoring step reads resolved cost) — all **call**, never re-implement.
- **Depends on:** Product catalog/price rows (Product B1).
- **Done-when:** all three engines price through one module; a stale cost is flagged, not silently used.

### C7 — Shared signing service  *(consolidation §9.6)*
- **What:** one e-sign integration + one verification primitive + one `SignTransport` + one fingerprint impl — **two implementations behind one interface.**
- **Interface:** `SignTransport ∈ {oneflow, criipto|signicat, manual}`; `request_signature(doc, signer, method) -> session`; webhook `on_signed/on_declined` (fire-and-pull → re-GET); audit trail. **Oneflow** = CLM-backend + BankID(AdES) for contracts *we author*; **Criipto/Signicat-direct** = pure BankID/QES rail. (Governed by Portal **ADR 0023** — flag the build-vs-buy revisit per §2.10.)
- **Consumers:** Sales (quote→`SIGNED_BASELINE` freeze on `on_signed`), Agreements (authored-contract lifecycle). **Both depend on the interface, not the vendor.**
- **Depends on:** credentials (external blocker — `manual` ships without).
- **Done-when:** both engines sign through one service; the quote's signature is the single event that freezes the baseline; one-vs-two-ceremony is a per-deal flag.

### C8 — Allow-list field-redactor  *(the 4-consumer external-surface component)*
- **What:** the **field-level half** of external-surface security (C3 RLS scopes rows; this scopes fields). **Allow-list, never deny-list.** Built once.
- **Interface:** `project(node, surface) -> redacted view` driven by `<surface>-redaction.json` (the allow-list of safe fields); **margin/cost/internal-status never on the list.**
- **Consumers (4):** Vendors partner-view (canonical), Sales public-quote, Customer Portal, **Marketing drafts (redact at draft-assembly time, not publish — un-consented PII/margin never enters a stored draft).**
- **Depends on:** none (pure projection).
- **Done-when:** a single redactor serves all four; adding a field to an external surface is a config edit, not code; no internal field can leak by omission (allow-list, so new fields are hidden by default).

### C9 — Structural-enforcement helpers  *(§9.3)*
- **What:** two reusable guards that enforce red lines **structurally, not by LLM instruction.**
  - **C9a — retrieval-grounded-generation helper:** *pull fact → cite its graph node → template into prose*; every claim carries a source-node link. Used by Marketing case studies, Economy close-narrative, Project status report, Support troubleshooter, BI board pack.
  - **C9b — no-person-grain-comparison query guard:** the data-access layer **cannot return person-grain rows for comparison/ranking** regardless of phrasing (HR EU-AI-Act/no-ranking line). Hardest at BI's open-NL `do_ask_business`.
- **Depends on:** graph + ledger.
- **Done-when:** a generated artifact's claims are each node-linked; an NL "rank technicians" query returns aggregates only, by construction.

> **Per-engine, NOT foundation (scope guard):** the DB-concurrency *constraints* themselves (Inventory `UPDATE…WHERE qty>=n`; Resources `EXCLUDE USING gist` — §2.11) are written per-engine but follow the documented pattern; domain logic; config-as-IP weights; each engine's own tables.

---

## 2. Build sequencing

Foundation splits into waves by **which tier first needs each**. Build a wave before the engine wave it gates.

**F0 — on the Tier-1 critical path (build FIRST):**
- **C1 entity-resolution registry** — Product (Tier-1 root) is its heaviest client; identity is the foundational risk.
- **C6 shared pricing service** — Product + Procurement (Tier-1) price through it.
- **C2 autonomy wrapper (minimal: confirm-only + ledger + idempotency key)** — any Tier-1 Actor tool (Project `convert_signed_quote`, Product enrich) goes through it from day one; richer ceilings/allowlists grow later.
- **C9a retrieval-grounded helper** — cheap, and Project status-report / Support troubleshooter want it early.

**F1 — cross-engine plumbing (build alongside late-Tier-1 / Tier-2):**
- **C4 reactive event bus** — Project auto-tasking + Assets/Support/Resources reactive flows; generalise the `netbox_nce` push exemplar.
- **C3 external-principal RLS primitive** — needs its own security-review pass; gates Vendors/Field-Tech/Customer-Portal (Tier 2+). Start the review early (long pole).
- **C8 allow-list redactor** — first external surface (Sales public-quote or Vendors partner-view).

**F2 — source & signing (build with Sales/Agreements, Tier-2):**
- **C5 source-mode resolver + divergence audit** — Sales B1 is the cutover precondition; Support + Economy↔Finago reuse it.
- **C7 shared signing service** — Sales + Agreements.

**F3 — guards for the consumer leaves (Tier 3-4):**
- **C9b no-person-grain query guard** — before BI's `do_ask_business` and any HR aggregate surface.

**Dependency notes:** C2 and C4 must interlock (idempotent handlers); C3 must be security-reviewed before *any* external surface ships; C5 contains C-style divergence audit (don't build divergence separately); C8 pairs with C3 (rows + fields) on every external surface.

---

## 3. Proposed RL batches (foundation, before the Tier-1 engine batches)

- **FB-1:** C1 registry + merge-review queue (+ ownership table the write-path consults).
- **FB-2:** C6 pricing service (+ price resolution + freshness) and C2 autonomy wrapper (confirm-only core).
- **FB-3:** C4 event bus (generalise `netbox_nce`) + C9a retrieval-grounded helper.
- **FB-4:** C3 external-principal RLS primitive (**security-review gated**) + C8 redactor.
- **FB-5:** C5 source-mode resolver + divergence audit; C7 signing service.
- **FB-6:** C9b query guard.
- **Then** the Tier-1 engine batches (Product → Procurement → System Design 1a → Project) build on FB-1..FB-3, which already exist.

Each FB closes with gates green (ruff, ruff format, mypy `nce/`, pytest + tool-count) and a component-level test that proves the "done-when".

## Change log
- 2026-06-17 — Initial foundation plan. Turns the §9 contracts + standing tally (2 contracts · 6 infra items · 2 consolidation services · the allow-list redactor · the §9.3 structural-enforcement helpers) into 9 buildable core components (C1–C9), sequenced in waves F0–F3 / batches FB-1..FB-6 ahead of the Tier-1 engine batches.
