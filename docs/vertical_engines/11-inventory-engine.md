> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 11 — Warehouse & Inventory Engine  (nce/vertical_modules/inventory)

<!-- STATUS (Verified-against: 6ae61ee): SHIPPED, and this engine now EXCEEDS the surface this document proposes. Measured on main: 17 do_* cores, 14 MCP tools registered in TOOL_REGISTRY, 14 REST routes in nce/admin_handlers/inventory.py, build phases B1-B5 all implemented. The previous marker here claimed '0 MCP tools and 0 REST routes' against baseline 7304330; that was true then and is false now. Refer to docs/engines/inventory-user.md and inventory-admin.md for the operator view. -->


**Status:** spec (Tier 3 — Operations axis) · **Owner:** NCE core (Sindre)
**Pattern companions:** `docs/VERTICAL_MODULE_PATTERN.md`, `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§2 AI-role, §4 graph catalogue, §7 spec format), `docs/vertical_engines/01-procurement-engine.md` (the Receive→Match→Cascade A2A flow this engine fires)

## Mission
Give NCE the digital inventory nervous system it has never had — today there is **no inventory system at all, "alt i én persons hode"** (planning module 10 Logistics, status *ikke-startet*). That makes this a greenfield, high-value engine: it owns **physical stock**, in real time, across the **main warehouse + N service vans** (each van is a stock location), per SKU per location. The deep-AI angle is twofold. First, the **goods-receipt is the trigger of record**: when goods arrive against a PO, Inventory records the GR and **fires Procurement's `procurement_evaluate_match`** — the canonical Receive→Match→Cascade flow that flows GREEN/YELLOW/RED into the Economy cascade. Second, the engine is **predictive**: the Advisor recommends van restocking from consumption patterns + upcoming Field Tech work-orders, and pre-positions stock from **pipeline demand** (designs/quotes/projects implied by Sales + System Design + Project) — cognitive forecast and cross-engine recall, not a static stock card.

## Research-informed direction (see `11a-inventory-engine-research.md`)
A deep dive on **Rackbeat** (the Nordic cloud WMS) — read as a **feature catalogue to copy-and-improve, NOT integrate** (roadmap §2.10 build-vs-buy; *we are not going to use Rackbeat*). The big strategic confirmation: Rackbeat exists because accounting systems are weak at physical stock, so it **owns stock + goods-receipt and posts the financial result back to the ledger** — *exactly* our boundary (Inventory owns physical stock + GR; **Economy** owns the GL posting). And its intelligence ceiling is our floor: **"Smart Reordering" is min-threshold + draft-PO — no forecasting, no pipeline-awareness, no van model, no cross-engine reasoning.** That gap is our wedge. Positioning: *not a WMS bolted onto accounting — an AI-native inventory engine on the cognitive graph: Rackbeat's proven mechanics + pipeline-driven forecasting + the van-as-shared-node + serial→Asset seeding.* Load-bearing copies (Tier A):
- **A1 — serial number captured at goods-receipt → seeds an `ASSET` (the highest-leverage AV move).** Rackbeat registers a serial *at GR* and carries it to the customer for warranty/recall. We go further: capture serial in `do_record_goods_receipt` `scans[]`, store on `GOODS_RECEIPT -[of]-> SKU`, and **A2A-hand-off the serial to Assets as the seed of an `ASSET`** when stock is consumed/installed on a work-order (the `serienr→asset` spine note). *New cross-engine hand-off — the spec previously only fired Procurement on GR.*
- **A2 — the reservation algebra `available = on_hand − reserved − blocked`** (adopt verbatim). `do_stock_levels` returns `{on_hand, reserved, blocked, available}` (+ a `qty_blocked` quarantine/RMA-hold bucket); **Procurement's "own stock first" read consumes `available`, not on-hand.**
- **A3 — valuation-method config (`fifo | average`).** Per-namespace `NCE_INVENTORY_VALUATION_METHOD`; the valuation number is what Inventory **hands to Economy for posting** (boundary holds: we value, Economy posts). `inventory_transactions` is the FIFO-layer source.
- **A4 — partial goods-receipt semantics vs the `BOM_LINE.status=DELIVERED` write (roadmap §9.1).** A partial GR advances only the received lines; the PO stays open for the remainder; **only fully-received lines flip their `BOM_LINE` to `DELIVERED`** (validated: no `DELIVERED` before `ORDERED`).
- **A5 — append-only `inventory_transactions` ledger + typed adjustment categories** (Rackbeat's movements-vs-adjustments split). Every qty change is an immutable transaction with a typed reason — the ledger discipline applied to stock, so "why is this count what it is" is answerable.
- **A6 — hierarchical `STOCK_LOCATION` (warehouse→zone→bin; a van = flat top-level).** `stock_locations` gains parent/level; pick-routes traverse it.

Tier B (van-as-shared-node, pipeline forecast, project-kitting via phantom-BOM, van/work-order-aware restock, lot/batch+recall Watcher) is **already this spec's stated direction** — `11a` confirms the mechanics and sharpens the `do_*` shapes; Tier C (QA-quarantine, drop-ship-to-site, dead-stock + forecast-shortfall reports) is backlog.

## Inspiration & triage
- **the planning sources:** module **10 Logistics** (`docs/handoff/04-virksomhets-modulkart.md:59`) — 1 logistics-person, 1 warehouse, 6 vans; core objects Varemottak, lager-inventar, van-inventar, retur/RMA, WEEE. Spine note (`:128`): *Procurement (BOM→PO) → Logistics (varemottak→BOM-status) → Technical (install→serienr→asset)* — Inventory sits between Procurement and Field Tech in the delivery chain. Supply Chain (#09, merged into Procurement) contributes the **demand-forecasting-from-pipeline** strategic layer (`:57`).
- **Portal sidecar to lift:** none (greenfield — no existing inventory code). Reuses Procurement's `client.py`/feed only indirectly via SKU master data.
- **Lysning page served:** a new warehouse/van-stock + goods-receipt screen (consumes the no-model REST surface); GR screen wires into Procurement's `Bestillinger.jsx` match view.

## Classification
**internal + IoT.** No external system of record — NCE *is* the inventory system. Inputs are internal events (GR, picks, transfers, returns) and **IoT/scanner style** capture: barcode/QR scans of SKU + serial number at receive/pick time (the same S/N-scan substrate Field Tech #12 uses). No OAuth; scanner clients authenticate to the admin app via HMAC/mTLS like any other no-model caller. Optional later: a handheld-scanner push endpoint. Resilience for any scanner callbacks via `nce.http_resilience.request_with_retry()`.

## Graph contribution
Node `entity_type` prefixes: `INVENTORY_*`, plus shared spine nodes `PO`, `PRODUCT`/`SKU`, `PROJECT`, `WORK_ORDER`.
- **Nodes:** `STOCK_LOCATION` (warehouse | van — an **Inventory-owned node type, NOT a `FUNCTIONAL_LOCATION`**: it is a company-internal *logistics* location, a different ontology from the customer-site tree (see hardening #1); a van is the `STOCK_LOCATION` half of the `VEHICLE`+`STOCK_LOCATION` shared node, keyed to its `VEHICLE`/field-tech), `INVENTORY_ITEM` (a quantity of a SKU at a location), `GOODS_RECEIPT` (a recorded arrival against a PO), `INVENTORY_RMA` (a return/RMA record, carries WEEE-disposal state).
- **Edges (the §4 contract, our slice):**
  - `INVENTORY_ITEM -[at]-> STOCK_LOCATION` (with `confidence` = stock-count freshness)
  - `GOODS_RECEIPT -[against]-> PO` (the boundary edge Procurement consumes for the 3-way match)
  - `GOODS_RECEIPT -[of]-> SKU` (qty received per article; substitution surfaces here)
  - `INVENTORY_ITEM -[reserved_for]-> PROJECT` (pre-positioned / committed stock against pipeline demand)
- **memories/ledger:** no large unstructured-text track (inventory is structured). Consumption events + forecast decisions → `v3_cognitive_ledger` so the restock/forecast Advisor is auditable ("why did it recommend +4 of this SKU to van-3"). Tag every derived row with `inventory_source_id` for hard-retirement on delete (D365 retirement pattern).

## Core functions
<!-- STATUS (Verified-against: 6ae61ee): ALL NINE cores listed below are implemented in nce/vertical_modules/inventory/, plus eight more the spec never named (do_release_stock, do_valuation, do_reconcile_dead_stock, do_restock_from_rma, do_dispose_rma_weee, do_flag_stock_alerts, do_advance_bom_line_to_delivered, do_record_goods_receipt_and_evaluate_match) -- 17 in total. The previous marker claimed none were implemented at baseline 7304330. -->
Pure-ish `do_<action>(engine, params) -> dict`; reads write the graph, forecasts are pure over consumption history + pipeline.
- `do_stock_levels(engine, params) -> dict` — `{sku?, location?}` → live qty per SKU per location (warehouse + each van). The "own stock first" source of truth.
- `do_record_goods_receipt(engine, params) -> dict` — `{po, lines[], location, scans[]}` → creates `GOODS_RECEIPT` + `-against->PO` / `-of->SKU` edges, increments `INVENTORY_ITEM` at the receiving location. **Then fires Procurement** (see A2A). Actor; writes graph.
- `do_record_consumption(engine, params) -> dict` — `{sku, qty, location, work_order?}` → decrements stock when a tech picks/uses stock for a job (Field Tech demand realised).
- `do_transfer_stock(engine, params) -> dict` — `{sku, qty, from_location, to_location}` → warehouse↔van or van↔van move (the physical leg of a restock).
- `do_reserve_stock(engine, params) -> dict` — `{sku, qty, project}` → `INVENTORY_ITEM -[reserved_for]-> PROJECT`; pre-positions against pipeline demand. Actor.
- `do_recommend_restock(engine, params) -> dict` — `{location}` → per-SKU restock recommendation from consumption velocity + upcoming work-orders (Field Tech) + reorder thresholds. Advisor; pure over config + history.
- `do_forecast_demand(engine, params) -> dict` — `{horizon_days}` → future SKU demand implied by the pipeline (open quotes/designs/projects), with confidence; drives pre-positioning. Cognitive forecast.
- `do_record_rma(engine, params) -> dict` — `{sku, serial?, reason, weee?}` → `INVENTORY_RMA` node with returns/WEEE-disposal compliance state.
- `do_create_restock_po(engine, params) -> dict` — orchestrates `recommend_restock` → emits a restock-PO **request to Procurement** (`procurement_generate_po`); does NOT source/order itself. Actor (Autonomous under threshold).

## MCP tools
<!-- STATUS (Verified-against: 6ae61ee): 14 inventory_* tools are registered in TOOL_REGISTRY.
     🔴 THREE cores are DELIBERATELY NOT REGISTERED, and this is a safety boundary, not a gap.
     tests/unit/test_inventory_surface.py asserts the exclusion so the ruling survives a refactor:
       * do_create_restock_po -- does not take the (engine, params) core shape at all. It takes an
         open asyncpg connection, a keyword-only idempotency_key, a confirm flag, and an optional
         redis_client WHOSE ABSENCE TURNS ITS KILL-SWITCH FROM FAIL-CLOSED TO OPEN. No thin adapter
         can supply those without holding a transaction and deriving policy state, which the MCP
         layer must not do. The `inventory_create_restock_po` row in the table below is therefore
         a SPEC PROPOSAL THAT MUST NOT BE IMPLEMENTED as written.
       * do_advance_bom_line_to_delivered -- exposing it would let an admin caller mark a BOM line
         delivered with no goods receipt behind it.
       * do_flag_stock_alerts -- already wired to the cron tick, which holds acquire_cron_lock; a
         manual twin would sweep outside that lock. -->
Registered in `nce/tool_registry.py` via `_h(...)` late-binding. AI-role tag per roadmap §2 taxonomy.

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `inventory_stock_levels` | ✔ | ✘ | ✘ | Watcher |
| `inventory_recommend_restock` | ✔ | ✘ | ✘ | Advisor |
| `inventory_forecast_demand` | ✔ | ✘ | ✘ | Advisor |
| `inventory_record_goods_receipt` | ✘ | ✔ | ✔ | Actor |
| `inventory_record_consumption` | ✘ | ✔ | ✔ | Actor |
| `inventory_transfer_stock` | ✘ | ✔ | ✔ | Actor |
| `inventory_reserve_stock` | ✘ | ✔ | ✔ | Actor |
| `inventory_record_rma` | ✘ | ✔ | ✔ | Actor |
| `inventory_create_restock_po` | ✘ | ✔ | ✔ | Actor (Autonomous under threshold) |

## REST routes
<!-- STATUS (Verified-against: 6ae61ee): 14 api_inventory_* routes are implemented in nce/admin_handlers/inventory.py and mounted in admin_app.py -- every route this document proposes, plus six more. The previous marker claimed zero at baseline 7304330. -->
No-model path for the BFF (warehouse/van-stock + GR screen), scanners, cron. Mounted via `build_app(extra_routes=...)`; HMAC/mTLS-authed in `nce/admin_handlers/inventory.py`:
- `api_inventory_stock_levels` (GET) — live stock per SKU per location (the screen + the "own stock first" read Procurement scoring calls).
- `api_inventory_recommend_restock` (POST) — van restock recommendation.
- `api_inventory_forecast_demand` (POST) — pipeline-driven demand forecast.
- `api_inventory_record_goods_receipt` (POST) — GR capture (scanner screen) → fires Procurement match.
- `api_inventory_record_consumption` (POST) — pick/use against a work-order.
- `api_inventory_transfer_stock` / `api_inventory_reserve_stock` (POST) — stock moves + project reservation.
- `api_inventory_record_rma` (POST) — return/RMA + WEEE state.

## AI features
- **Watcher:** low-stock alerts (below reorder threshold, per location), **dead-stock** detection (no movement in N days — capital sitting idle), **expiring** items (warranty/shelf-bound SKUs), and over-reserved-but-unconsumed stock.
- **Advisor:** **restock recommendation** (consumption velocity × upcoming work-order demand × van capacity); **demand forecast** from pipeline (designs/quotes/projects → future SKU need); **where-to-pull-from** (which location should fulfil a job at lowest transfer cost) with plain-language rationale.
- **Actor:** `create_restock_po` (via Procurement) and `reserve_stock` *with confirmation*.
- **Autonomous (gated):** **auto-restock under threshold** — when a warehouse/van SKU drops below its reorder point and the order value is under `NCE_INVENTORY_AUTONOMY_RESTOCK_CEILING`, auto-emit the restock-PO request to Procurement (value/risk governance gate before write).
- **Cognitive recall:** restock + forecast decisions read from `v3_cognitive_ledger`, so an operator can query *why* a recommendation was made and how consumption velocity shifted.
- **Enrichment triggers (event-scoped, never a background sweep):** the forecast/restock Advisor runs *only* on a GR, a consumption event, a new pipeline node (quote/design/project created), or an explicit operator request — never a periodic full-catalogue recompute.

## A2A flows
- **Initiates Receive→Match→Cascade:** on `do_record_goods_receipt`, fires Procurement's `procurement_evaluate_match` with `{po, goods_receipt, invoice?}`; Procurement returns GREEN/YELLOW/RED and hands the result to **Economy** for the approval cascade + posting. **This is the key cross-engine wiring** — Inventory is the event source; Procurement owns the match algorithm.
- **Serves Field Tech (#12):** answers "what's on van-N right now / is the part for this job in stock" via `inventory_stock_levels`; consumes work-order completion as consumption.
- **Consumes pipeline demand:** reads Sales(#5) / Project(#7) / System Design(#6) nodes (open quotes/designs/projects) to drive `do_forecast_demand` and pre-position stock (`reserve_stock`).
- **Emits restock-PO requests to Procurement(#1):** `do_create_restock_po` → `procurement_generate_po`; Procurement runs its 5-step scoring (whose **step 1 "own stock first" reads this engine's stock levels**) and owns the actual sourcing/order.
- **Feeds Morning-brief (#19 aggregate):** exposes low-stock / dead-stock / forecast-shortfall as the inventory slice of the cross-engine risk/opportunity query.

## Config keys
`NCE_INVENTORY_*` in `nce/config.py`: `NCE_INVENTORY_ENABLED`, `NCE_INVENTORY_REORDER_LOOKBACK_DAYS` (consumption-velocity window), `NCE_INVENTORY_DEAD_STOCK_DAYS`, `NCE_INVENTORY_FORECAST_HORIZON_DAYS`, `NCE_INVENTORY_AUTONOMY_RESTOCK_CEILING` (auto-restock value gate), `NCE_INVENTORY_DEFAULT_VAN_COUNT` (seed N van locations). Namespaces opt in via `metadata.inventory.enabled = true`. Never a host-specific key (FE-5).
**Config-as-IP JSON (namespace-scoped, the business logic — NOT code):**
- `inventory-reorder-points.json` — per-SKU / per-location reorder thresholds + van capacity profiles. Each tenant tunes its own.
- `inventory-forecast-weights.json` — pipeline-stage → demand-probability weights (a draft quote implies less than a signed project) feeding `do_forecast_demand`.

## Tables/migrations
**Graph + keyed tables** (stock needs fast atomic decrement/aggregate that the graph alone serves poorly). All `ENABLE` + `FORCE ROW LEVEL SECURITY` + `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`; mirror DDL into `schema.sql` + a numbered migration:
- `stock_locations` (`id, namespace_id, kind warehouse|van, name, parent_id, level, vehicle_ref, raw jsonb`) — **internal logistics, hierarchical (warehouse→zone→bin)**; a van's `vehicle_ref` is the `VEHICLE`+`STOCK_LOCATION` shared-node link. **No `functional_location_id`** — `STOCK_LOCATION` is not a customer `FUNCTIONAL_LOCATION` (hardening #1).
- `inventory_items` (`id, namespace_id, sku, location_id, qty_on_hand, qty_reserved, qty_blocked, reorder_point, updated_at`) — the hot read/**atomic-decrement** path; `available = qty_on_hand − qty_reserved − qty_blocked`.
- `inventory_transactions` (append-only: `id, namespace_id, item_id, delta, reason_category, ref, at`) — the immutable movement ledger + FIFO-layer source (typed adjustments).
- `goods_receipts` (`id, namespace_id, po_ref, location_id, lines jsonb, scans jsonb, match_result, received_at`).
- `rma` (`id, namespace_id, sku, serial, reason, weee_state, status, created_at`).
**FORCE RLS on all.** **Authority model (hardening #2): the `inventory_items` row is the source of truth; the `INVENTORY_ITEM` graph node is an eventually-consistent *projection*.** Stock-truth reads (Procurement "own stock first", forecast, reservation) hit the **row/consistent view, never the graph mirror**; decrements use `UPDATE … WHERE qty >= n` (row-locked) to prevent overselling.

## Dependencies
- **Upstream engines:** Procurement(#1) — owns PO/sourcing and the `procurement_evaluate_match` this engine fires (must exist for Receive→Match→Cascade); Product(#2) — SKU master data; Sales(#5)/Project(#7)/System Design(#6) — pipeline nodes for demand forecasting; Field Tech(#12) — work-order demand + consumption (can land before Field Tech with manual consumption entry).
- **Downstream boundary — clear ownership:** **Procurement owns PO creation + sourcing + the 3-way-match algorithm; Inventory owns PHYSICAL stock + the goods-receipt event; Economy owns the GL posting.** Inventory writes the `GOODS_RECEIPT -[against]-> PO` edge and *triggers* the match — it does NOT compute the match or post. The "own stock first" rule in Procurement's 5-step scoring **reads this engine's `stock_levels`** rather than Procurement holding its own stock model.
- **External blocker:** none (greenfield, internal). Scanner-hardware push endpoint is optional and can be deferred — manual/BFF GR entry works day one.

## Review round-2 hardening (2026-06-17 — these govern the build)
Inventory mostly **confirms §9** and is the **producer that unblocks two Tier-1 dependencies** (it owns the `GOODS_RECEIPT` = the GR half of Procurement's 3-way match, and the `BOM_LINE.status=DELIVERED` transition Project's auto-tasking keys off). New catches:
1. **`STOCK_LOCATION` is NOT a `FUNCTIONAL_LOCATION` (roadmap §9.1 — corrected).** A van/warehouse is a **company-internal logistics** location; `FUNCTIONAL_LOCATION` is the **customer-site** tree (where assets live, where SLAs apply, System-Design-intent→Assets-as-built). Conflating them pollutes the customer-site node and muddies §9.1 ownership. **`STOCK_LOCATION` is its own Inventory-owned node type — two trees, not one.** (A van links to its `VEHICLE` via the shared-node, not to a customer `FUNCTIONAL_LOCATION`.)
2. **The dual representation (row-truth + graph-mirror) is an internal divergence + atomicity hazard — the engine's hard core (a §9.2 variant inside one engine).** Declare it explicitly: **the `inventory_items` row is authoritative; the `INVENTORY_ITEM` graph node is an eventually-consistent *projection*.** All **stock-truth reads** — Procurement's "own stock first", the forecast, reservation checks — **go to the row / a consistent view, never the possibly-lagging graph mirror.** And concurrent picks of the last unit need **real atomicity**: decrement with `UPDATE … SET qty = qty − n WHERE qty >= n` (row-locked), not a graph upsert — **or you oversell.**
3. **Sequencing inversion: two *Tier-1* engines depend on this *Tier-3* engine for a real signal.** Procurement's 3-way match needs the `GOODS_RECEIPT`; Project's auto-tasking needs `DELIVERED` — **both owned here.** Until Inventory ships, those run on **manual/absent GR** (grace-degradation — stated, not hidden). **Tension to surface:** given two Tier-1 consumers, consider **pulling Inventory earlier** than its Tier-3 slot (at least the locations + GR core, B1–B2).
4. **Compounding cross-engine autonomy → real spend (roadmap §9.5).** `do_create_restock_po` (Autonomous under `RESTOCK_CEILING`) emits a PO request; Procurement's `submit_po` (its own gate) places the order — **two gates in series ending in money out.** Requires an **end-to-end idempotency key propagated across the boundary** (an auto-restock retry must not create a duplicate PO request *or* a duplicate order) and a **single audit trail spanning both decisions.**

## Build phases
<!-- STATUS (Verified-against: 6ae61ee): 🔴 B1-B5 ARE ALL IMPLEMENTED. The previous marker said waves 2-12 (goods receipt, replenishment, demand forecast, RMA/WEEE) were 'planned future work'; every one of them has shipped -- goods_receipt.py, replenishment.py, forecast.py, reservation.py, restock_po.py, rma.py, reconcile.py, watchers.py. AUTONOMY_RESTOCK_CEILING is wired to NCE_PROCUREMENT_AUTONOMY_PO_CEILING in restock_po.py. Do not rebuild these phases. -->
- **B1 — Locations + stock core + RLS:** `stock_locations` / `inventory_items` tables (FORCE RLS) + seed warehouse + N vans; `do_stock_levels`, `do_transfer_stock`, `do_record_consumption`; graph mirror (`STOCK_LOCATION`, `INVENTORY_ITEM -[at]->`). MCP + REST for the reads. This alone replaces "alt i én persons hode".
- **B2 — Goods-receipt + the A2A trigger:** `goods_receipts` table; `do_record_goods_receipt` with scan capture; `GOODS_RECEIPT -[against]->PO / -[of]->SKU` edges; **fire `procurement_evaluate_match`** (Receive→Match→Cascade). This is the cross-engine keystone.
- **B3 — Predictive replenishment:** `do_recommend_restock` (consumption velocity + work-order demand), `inventory-reorder-points.json`, low-stock/dead-stock/expiring Watcher, ledger-backed rationale.
- **B4 — Demand forecast + reservation + restock-PO:** `do_forecast_demand` from pipeline (`inventory-forecast-weights.json`), `do_reserve_stock` (`-reserved_for->PROJECT`), `do_create_restock_po` → Procurement, autonomy gate (`AUTONOMY_RESTOCK_CEILING`).
- **B5 — Returns/RMA + WEEE:** `rma` table + `do_record_rma`, WEEE electronics-disposal compliance state, reverse-logistics into stock (restock on return) + dead-stock reconciliation.
