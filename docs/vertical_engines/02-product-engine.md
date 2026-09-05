> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 02 — Product Engine  (nce/vertical_modules/product)

<!-- BLOCKED ON OQ-2 / OQ-4: SPEC PROPOSAL VOICE. This document is an architectural design specification. At baseline 7304330, Product ships 6 MCP tools and 3 REST routes (see docs/_generated/surface.md). Refer to docs/engines/product-user.md and docs/engines/product-admin.md for shipped reality. Verified-against: 7304330 -->

**Status:** spec · **Owner:** NCE core (Sindre)
**Tier:** 1 (spine) · **Axis:** Revenue/Delivery-bridge · **Upstream of:** Procurement(1), consumed by System Design(6) & Sales(5)
**Pattern refs:** `docs/VERTICAL_MODULE_PATTERN.md` · `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§4 graph, §5 on-demand AI rule, §7 format)

## Mission

The Product Engine turns the organization's product knowledge from a personal asset into a graph asset. It owns the canonical `PRODUCT`/`SKU` nodes that the whole BOM-backbone hangs off, ingests catalog data from **several** external sources (Nettailer/Netset is only one), and answers "what is this product, what does it cost, what does it pair with, and what fails on it" for Sales and System Design. Its deep-AI angle is **strictly on-demand enrichment**: AI never sweeps the catalog — it expands a single product's missing specs only at the moment that product enters a quote or a design. Pricing (DG-margin) is a real engine here, and failure-patterns flow *back* in from Support/Assets so a product's BOM recommendations get smarter every time something breaks in the field.

## Research-informed direction (see `02a-product-engine-pim-research.md`)

A deep analysis of Icecat + the PIM/syndication/classification landscape (Akeneo, Salsify, 1WorldSync/Syndigo, D-Tools, XTEN-AV, ETIM) settled four **load-bearing decisions** now baked into this spec. Full landscape, prioritized idea backlog, and sources live in the companion doc `02a`.

- **Positioning:** *not a bigger AV catalog* (D-Tools ~1.9M / XTEN-AV ~1.5M own bulk, manufacturer-approved data) — an **AI-native, ETIM-coded, MCP-exposed engine that turns any manufacturer PDF into a trusted, confidence-scored, graph-connected product on demand.** AV has **no product-data standard and no manufacturer APIs** (vendors ship PDF spec sheets, not feeds), so **PDF-in → structured-out is the wedge**, and we are **MCP-native where Icecat is retrofitting MCP**.
- **A1 — ETIM-coded schema:** specs are stored as coded **(ETIM class, feature, value, unit)** tuples, not free-form fields; `PRODUCT_CATEGORY` references an ETIM class whose template defines mandatory/searchable features. **GTIN = universal key, brand+MPN = canonical match key**, UNSPSC/GPC/eCl@ss kept as crosswalk codes. Gives multilingual catalog *for free* (critical for NO) + Nordic supplier interop; enrichment becomes *"map this datasheet to the right ETIM class + valid values"*. AV-specific classes (control processors, DSP, AV-over-IP) via a **namespace ETIM extension**. Config: `product-etim-schema.json`.
- **A2 — field-level golden record:** dedup on `(manufacturer, mfr_part_no)`, but resolve conflicts **per field** via survivorship — **source-trust (manufacturer-verified > distributor > AI-derived > scraped) → recency → confidence** tiebreaker — with **field-level provenance** (which source won, why). Config: `product-survivorship.json`.
- **A3 — two-score quality model:** compute **completeness** (required fields per target channel) **and** a **quality/confidence grade** (consistency/units/provenance), roll up a per-manufacturer **data-health** score, and surface the *specific failing criteria* (extends the `needs_review` queue; gates "trusted" status per channel).
- **A4 — Claude confidence ≠ logprobs:** Anthropic models don't expose token logprobs, so `confidence` fields come from **verbalized self-rating + self-consistency (ensemble) + optional judge model**, never logprob reading. Invariant alongside ADR-0017.

Deferred (in `02a` backlog, graduate on trigger): AI-proposed typed **compatibility graph** (B1), **EoX-style + LLM EOL** (B2), **on-demand design BOM gap-audit** (B3, XAVIA-style), delta-index sync (B4), rich-media ladder, personalized catalog.

## Inspiration & triage

**the planning sources (READ):**
- Module 03 *Product & Løsningsdesign* (`handoff/04-virksomhets-modulkart.md`) — product knowledge as org asset, failure-patterns, EOL warning, manufacturer-API enrichment (Cisco/Microsoft/Neat/Poly/Shure/Biamp/Crestron/QSC/Huddly), Icecat/XTEN-AV considered.
- `handoff/02-...§1 Produkt/katalog (Netset)` — 🟢 **netset-matcher** fuzzy/learned BOM→SKU (`the reference implementation:297 findBestNetsetMatch`, learns over time); 🟢 **DG-pricing** (the reference implementation, salgspris = kost / (1 − DG%)) — a real engine the solution-builder bypasses with inline `*0.7` (**re-wire opportunity**).
- `_audit/manifest.json secondary_tier` — confirms netset-matcher + DG-pricing as real, liftable IP; the inline-`*0.7` bypass is called out explicitly.

**Portal sidecar `backend/steps_product/` to lift (the seed):**
- `semantic.py` → embedding via the public `nce.embeddings` contract (same vector space as NCE `semantic_search`) — graduates into `ingestion.py`.
- `related.py` → `extract_model_tokens` + `classify_accessory` + `find_replacements` (on-read accessory/warranty/replacement derivation from catalog text, no relation table) → becomes graph edges + a `do_related_products` core fn.
- `bidprices.py` / `sync.py` / `auto_sync.py` → streaming idempotent CSV ingest, column-report self-tuning, soft-delete of vanished rows → the **Nettailer source adapter**.
- `offer_ai.py` → cost-stripped Anthropic drafting (ADR-0017 invariant: cost/margin never leave to a third-party API) → enrichment-prompt hygiene; **do NOT** lift the per-block text drafting (that belongs to Sales).
- `backend/integrations/nettailer_client.py` → the field-alias map + quote-safe CSV parse; this is the seed that becomes `sources/nettailer.py` (config becomes vertical-owned `NCE_PRODUCT_*`, no longer Portal env).

**Lysning pages served:** `Produktbibliotek.jsx`, `Bibliotek.jsx`, `Komponenter.jsx`, `NettailerExplorer.jsx` — all read through the no-model REST routes below.

## Classification

**Source pattern: pull + on-demand AI enrichment** (not push+semantic — no inbound webhooks; the catalog is polled, enrichment is event-triggered). **Multi-source ingestion:** Nettailer/Netset is **one source adapter among several**. Each adapter (`sources/<name>.py`) pulls its native format and **normalizes to the canonical `PRODUCT` node** — so a Cisco-API product and a Netset-CSV product land as the same node shape, deduped on `(manufacturer, mfr_part_no)`. Auth per adapter: Netset = secret GUID-in-URL (treated as secret, never logged — Portal pattern); manufacturer APIs = OAuth/API-key in `auth.py`, Redis-cached.

## Graph contribution

**Nodes** (`entity_type` prefix `PRODUCT_`):
- `PRODUCT` / `SKU` — canonical product, deduped on `(manufacturer, mfr_part_no)`; carries `confidence`-scored spec fields, `lifecycle_status` (aktiv/utgaatt/EOL/EOS), `product_source_id` per contributing adapter (multi-source provenance + retirement).
- `PRODUCT_CATEGORY` — hoved-/underkategori taxonomy node.

**Edges:**
- `BOM_LINE -[references]-> PRODUCT` (the spine join from Sales/System Design — §4 catalogue).
- `PRODUCT -[accessory_of | warranty_for | mounts]-> PRODUCT` (derived from `related.py` token/classifier logic, `confidence`-scored).
- `PRODUCT -[replaced_by]-> PRODUCT` (EOL successor chain, from `find_replacements`).
- `PRODUCT -[supplied_by]-> VENDOR` (price-source → Vendors(4)/Procurement(1)).
- **Failure-pattern edges back in:** `TICKET/ASSET -[failure_pattern]-> PRODUCT` (written by Support(10)/Assets(9), *read* here) — this closes the "service→product silence" gap the reference implementation flags; the Product Engine surfaces it in BOM advice and feeds Sales upsell.

**What hits memories/ledger:** product **specs/datasheets** (and manufacturer-API descriptions) are embedded into `memories` (embedding + `content_fts`) at enrichment time — so cognitive recall can answer "which similar products had this spec / this failure". Each enrichment writes a `v3_cognitive_ledger` entry recording source, confidence, and trigger — auditable.

## Core functions

Dual-surface: each `do_<action>(engine, params) -> dict` is exposed once as an MCP `handle_*` and once as a no-model REST `api_*`.

- `do_search_products(engine, params)` — hybrid lexical + semantic (Tier-1 lexical floor, Tier-2 `nce.embeddings`; degrades to lexical if embeddings unavailable — `semantic.py` pattern). Read-only.
- `do_get_product(engine, params)` — single product, merged master + live price, accessory/warranty/replacement edges.
- `do_related_products(engine, params)` — accessory/warranty/mount/replacement groups (`related.py` logic over the graph).
- `do_match_bom_line(engine, params)` — **netset-matcher**: free-text BOM line → best catalog `SKU` with a match score; learned (records accepted/rejected matches, recalibrates over time — event-sourced, per the reference implementation's learning pattern). Read-only but writes a learning event on feedback.
- `do_price_product(engine, params)` — **DG-pricing** engine: `sales_price = cost / (1 − DG%)`, DG% from namespace config-as-IP (`product-dg.json`). The one true price computation (replaces the inline `*0.7` bypass). Cost/margin are internal — never returned on customer-facing surfaces (ADR-0017).
- `do_enrich_product(engine, params)` — **the centerpiece on-demand enrichment** (see AI features). Mutating, idempotent, scoped to one `product_id`.
- `do_ingest_source(engine, params)` — run one source adapter's pull (admin/cron only); streaming idempotent upsert + soft-delete of vanished rows.

## MCP tools

`TOOL_REGISTRY` entries (via `_h`), with AI-role tags:

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `product_search` | ✓ | ✗ | ✗ | Advisor |
| `product_get` | ✓ | ✗ | ✗ | Advisor |
| `product_related` | ✓ | ✗ | ✗ | Advisor |
| `product_match_bom_line` | ✗ | ✗ | ✗ | Advisor (learns on feedback) |
| `product_price` | ✓ | ✗ | ✗ | Advisor |
| `product_enrich` | ✗ | ✗ | ✓ | **Actor** (confirmation = the quote/design trigger; low-confidence → human review) |
| `product_ingest_source` | ✗ | ✓ | ✓ | Autonomous (cron-gated, scoped to one adapter) |

## REST routes

No-model paths for the BFF / Lysning pages / cron (admin app, HMAC/mTLS authed):
- `GET  /api/product/search` → `api_product_search` (Produktbibliotek.jsx, NettailerExplorer.jsx)
- `GET  /api/product/{id}` → `api_product_get` (Komponenter.jsx profile)
- `GET  /api/product/{id}/related` → `api_product_related`
- `POST /api/product/match-bom-line` → `api_product_match_bom_line`
- `POST /api/product/price` → `api_product_price`
- `POST /api/product/{id}/enrich` → `api_product_enrich` (also callable directly by Sales/Design BFF, not just via A2A)
- `GET  /api/product/sources` / `POST /api/product/sources/{name}/sync` → adapter status + manual sync (Bibliotek.jsx integrations card; status payload never leaks the secret GUID)
- `GET  /api/product/enrichment/review` → low-confidence enrichment queue (the OCR/confidence-review surface)

## AI features

**THE ON-DEMAND ENRICHMENT RULE (hard directive — the centerpiece):**
AI expands/fills missing product info **only** when a product is (a) added to a **QUOTE** with missing info, or (b) added to a **DESIGN** with missing info. There is **NEVER** a bulk update of all products. Enrichment is **event-triggered and scoped to the single product**. At 552k products / 1.57M prices / ~1000 BIDs, a background sweep is both wasteful and unsafe — scoping to the one product that just entered a workspace is the discipline (and the system-wide rule per roadmap §5).

`do_enrich_product(engine, {product_id, trigger_context})`:
- **Called by** Sales / System Design via A2A (`trigger_context = {kind: "quote"|"design", ref_id, missing_fields}`); also directly via the REST route.
- **Idempotent** — re-running on an already-enriched product is a no-op unless new source data appeared (compares source watermarks); safe to retry.
- **Scoped** — touches exactly `product_id`; never iterates the catalog.
- **Fills only `missing_fields`** by querying the source adapters / manufacturer APIs + embedded datasheet recall, then **writes confidence-scored specs back to the `PRODUCT` node** (each field carries `confidence` 0–1 + `product_source_id`).
- **Flags low-confidence for human review** (`< NCE_PRODUCT_ENRICH_MIN_CONFIDENCE`) — the OCR/confidence-review pattern: written but marked `needs_review`, surfaced in the review queue, never silently trusted.
- Writes a `v3_cognitive_ledger` entry per enrichment (source, confidence, trigger) so "why does this spec say X" is answerable.

**Other AI behaviours:**
- **Watcher — EOL/EOS detection:** a cron Watcher flags products approaching end-of-life/end-of-sale (from source lifecycle fields + manufacturer signals), writes `replaced_by` edges, alerts Sales/Procurement. No writes to product specs — alert only.
- **Advisor — failure-pattern surfacing:** reads the `failure_pattern` edges fed back from Support/Assets and surfaces them in `product_get`/`product_related` so BOM recommendations avoid known-bad products and Sales sees upsell signals.
- **Advisor — BOM→SKU matcher:** learned fuzzy match (event-sourced feedback recalibration).
- **Cognitive recall:** embedded specs/datasheets answer similarity/"what failed like this" queries.

## A2A flows

- **Serves Quote→Design→Procure** (roadmap §5): System Design asks Product for missing specs → `do_enrich_product` fires (trigger=design); Sales asks Product for price/match → `do_price_product` / `do_match_bom_line`; Product is the spec/price authority Procurement's TCO scoring depends on.
- **Initiates** nothing autonomously except the EOL Watcher alert and (on A2A request) enrichment.
- **Consumes** the `failure_pattern` edges that Support(10)/Assets(9)/Field-Tech(12) write back — the only inbound flow.

## Config keys

Namespace `NCE_PRODUCT_*` (never host-specific — FE-5):
- `NCE_PRODUCT_ENABLED` — engine enable flag.
- `NCE_PRODUCT_SOURCES` — comma-list of active adapters (e.g. `nettailer,cisco,microsoft`).
- `NCE_PRODUCT_NETTAILER_PRODUCT_URL` / `_BIDPRICES_URL` / `_SUPPLIERPRICES_URL` — Netset export URLs (secret GUID; never logged, never in API responses — Portal invariant).
- `NCE_PRODUCT_SYNC_INTERVAL_MINUTES` (floor 5), `NCE_PRODUCT_SYNC_BATCH_SIZE` (default 2000 — streaming RAM floor), `NCE_PRODUCT_PAGE_SIZE`.
- `NCE_PRODUCT_SEMANTIC_ENABLED` — opt-in hybrid search (default off until backfill).
- `NCE_PRODUCT_ENRICH_MIN_CONFIDENCE` — below this, enrichment is flagged `needs_review`.
- `NCE_PRODUCT_EOL_WARN_DAYS` — Watcher horizon.
- Manufacturer-API creds per adapter: `NCE_PRODUCT_<MFR>_API_KEY` / `_URL` (Cisco/Microsoft/Neat/Poly/Shure/Biamp/Crestron/QSC/Huddly).

**Config-as-IP (namespace-scoped JSON):** `product-dg.json` (DG% by category/brand — the pricing weights), `product-source-aliases.json` (per-adapter column/field alias maps, self-tuned from the ingest column-report). The *engine* is shared code; the *weights* are per-tenant.

## Tables/migrations

Mostly **graph-only** (`kg_nodes`/`kg_edges`), plus own tables where the graph is the wrong shape (all `ENABLE`+`FORCE ROW LEVEL SECURITY`, `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`, mirrored to `schema.sql` + numbered migration):
- `product_catalog` — the 552k-row catalog staging/master (streaming upsert target; RLS; soft-delete column; `product_source_id` per source for retirement).
- `product_prices` — 1.57M price rows (list/cost/BID), `(mfr_part_no, supplier, bid_id)` PK (BID pattern from `bidprices.py`).
- `product_match_feedback` — event-sourced accepted/rejected BOM→SKU matches (drives matcher learning + audit).
- `product_enrichment_log` — per-enrichment record (product_id, trigger_context, fields, confidence, needs_review) backing the review queue.
- Source ingest state (column-report, last-sync, never the URL) → `app_settings`-style key per adapter.

## Dependencies

- **Upstream engines:** none (Tier-1 root — Product is built *before* Procurement so its specs/pricing exist for scoring).
- **Reads back from:** Support(10) & Assets(9) for `failure_pattern` edges (graceful: edges simply absent until those engines ship).
- **NCE core:** `nce.embeddings` (public contract), `kg_nodes`/`kg_edges`, `memories`, `v3_cognitive_ledger`, `nce.http_resilience`, `scoped_pg_session`.
- **External blockers:** manufacturer API keys/agreements (Cisco/MS/Neat/Poly/Shure/Biamp/Crestron/QSC/Huddly) gate those adapters — absence is an integration hole, not a code gap; Netset adapter ships first (seed exists). Netset *Order* API (outbound) is out of scope here — that's Procurement(1).

## Review round-2 hardening (2026-06-17 — these govern the build)
1. **Identity is the foundational risk — make entity resolution first-class (more important than any AI feature).** Everything (BOM lines, pricing, failure-patterns) hangs off `PRODUCT` identity, and `(manufacturer, mfr_part_no)` both **false-merges** (`Cisco`/`Cisco Systems`/`CISCO`, dashed/undashed MPNs, regional variants) and **false-splits** at 552k scale; GTIN is often absent on AV SKUs. **False merges are silent and permanently poison the graph.** Use the **shared entity-resolution service (roadmap §9.4)**: a **merge-review queue distinct from the enrichment-confidence queue**, a manufacturer-normalization map as config-as-IP, and field-level provenance — *not* a key tuple. Both Product instances (golden-record dedup **and** BOM→SKU matching) use it.
2. **ETIM coding is on-demand, not an ingest gate.** AV ETIM classes don't exist yet (we'd author/maintain an AV-ETIM taxonomy — an ongoing ownership project), and coded `(class,feature,value,unit)` tuples are heavier than free-form. So: **products land with free-form specs; they get ETIM-coded when they enter a quote/design** (same discipline as enrichment). Ingest is **never blocked on a taxonomy we're still authoring**.
3. **Price resolution is an explicit, tested function with a freshness signal.** Which price applies is a **resolution** (`customer BID > supplier list > base`), not a lookup — that ambiguity is exactly where the `*0.7` bug bred. `do_resolve_price(product, customer) -> {cost, source, as_of}` is tested and carries a **staleness signal** (a quote on stale synced cost = wrong margin = the erosion we're fighting). `do_price_product` (DG) consumes the **resolved** cost, and **Sales/System Design call it — never reimplement** (the shared-pricing rule).
4. **Enrichment is fire-and-backfill (async), never synchronous.** When a product enters a workspace with missing fields, `do_enrich_product` **returns what's known instantly + queues enrichment + backfills** (line shows `specs pending`). A salesperson adding a line must **never wait** on an OCR/feed round-trip. (Scoped + idempotent as specified; this fixes the sync/async silence.)
5. **"Any PDF → trusted product" is R&D-grade — gate it like System Design (roadmap §9.3).** PDF→structured is the wedge *and* the least-reliable part (multi-column, image-heavy AV datasheets). Verbalized-confidence + self-consistency + judge (A4) is the right method, but **initial auto-trust is LOW + human-review heavy** until review-queue override rates prove calibration.
6. **"Never bulk" (enrichment) ≠ the search-embedding backfill.** The never-bulk rule governs **AI enrichment**. Hybrid search needs the 552k specs embedded — a **legitimate one-time bulk job** (feeds the halfvec storage pressure migration 019 addressed). These are **different operations**; semantic stays off until that backfill, and "never bulk" does not forbid it.
7. **`steps_product` is a misleadingly-named grab-bag — the leave-behind list is as explicit as the lift list.** **LIFT (Product):** `semantic.py`, `related.py`, `bidprices.py`, `sync.py`/`auto_sync.py`, + `nettailer_client.py`. **LEAVE (not Product):** `offer_ai`/`offer_cert`/`offer_doc`/`offer_followup`/`offer_import`/`oneflow`/`package_import`/`mailer`/`standards` → these are **Sales / Agreements**. Resist absorption-by-proximity. Also: per 02a ("AV vendors ship PDFs, not feeds"), reframe the source-adapter model as **feed adapters** (Netset CSV) **vs document adapters** (manufacturer PDF datasheets) — the 9 "manufacturer-API adapters" are mostly **document-ingestion pipelines, not APIs**; don't imply integrations that don't exist.
8. **Nettailer ingestion: Product OWNS the single ingest (roadmap §9.1)** and exposes supplier-price/BID/orderline **projections** that Procurement consumes — Procurement never re-ingests the 295 MB feed.

## Build phases

RL-batch-sized increments:
1. **Skeleton + Netset adapter** — `mcp_handlers.py` + `sources/nettailer.py` (lift `nettailer_client` alias map + streaming idempotent sync from `steps_product/sync.py`); `product_catalog`/`product_prices` tables + RLS + migration; `product_search`/`product_get` tools + REST. Graph: `PRODUCT` nodes, `BOM_LINE -references-> PRODUCT`.
2. **Pricing + related + match** — `do_price_product` (DG engine, kills `*0.7`); `do_related_products` (lift `related.py` → edges); `do_match_bom_line` (netset-matcher port) + `product_match_feedback` learning loop.
3. **On-demand enrichment** — `do_enrich_product` (idempotent, scoped, confidence-scored, review-flagged) + A2A wiring from Sales/Design + `product_enrichment_log` + review-queue route; semantic ingest of specs/datasheets into `memories`/ledger.
4. **Multi-source + Watchers** — source-adapter pattern generalized; first manufacturer-API adapter behind a key; EOL/EOS Watcher (cron) writing `replaced_by` edges; failure-pattern edge consumption surfaced in Advisor outputs.
5. **Hardening** — tool-count test, ruff/format/mypy/pytest green, BID secret-leak guards, namespace opt-in (`metadata.product.enabled`), config-as-IP JSON externalized.

## Change log
- 2026-06-17 — Initial spec. Tier-1 Product Engine: multi-source pull + the on-demand (quote/design-triggered, never-bulk) enrichment rule as centerpiece; lifts steps_product (semantic/related/bidprices/sync) + nettailer_client + the reference implementation's netset-matcher and DG-pricing; failure-pattern feedback edges close the service→product silence.
- 2026-06-17 — Added "Research-informed direction" from the Icecat/PIM deep-dive (`02a`): ETIM-coded schema (A1), field-level golden record/survivorship (A2), two-score quality model (A3), Claude verbalized-confidence not logprobs (A4); positioned as AI-native ETIM-coded MCP-exposed PDF→structured engine vs the bulk-catalog incumbents. Idea backlog (compatibility graph, EOL, BOM gap-audit, etc.) in `02a`.
