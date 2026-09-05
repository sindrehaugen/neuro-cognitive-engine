# 11a — Warehouse & Inventory Engine: Rackbeat Research & Idea Backlog

<!-- BLOCKED ON OQ-2 / OQ-4: RESEARCH COMPANION. Architectural research backlog. Verified-against: 7304330 -->

**Status:** research companion to `11-inventory-engine.md` · **Date:** 2026-06-17
**Question asked:** *"deeply research Rackbeat (the Nordic cloud WMS) to inform our Inventory engine."*
**Framing (roadmap §2.10 build-vs-buy):** we are **NOT integrating Rackbeat**. This is a **feature catalogue to copy-and-improve natively** — proven WMS mechanics we replicate on our cognitive-graph spine, plus the AV-specific edges Rackbeat doesn't have. We own the stack; we don't rent the SaaS.
**Method:** cited web investigation across Rackbeat's marketing site, helpdesk, and (crucially) its **public Developer Hub / API entity index** — the API surface is the cleanest signal of the right data model, since we're learning the model, not calling it. Sources at the bottom. Marketing-only claims flagged in §5.

---

## 1. Strategic read — where we play

Rackbeat (founded 2017, Danish; **acquired by/integrated into Visma e-conomic**) is a **cloud WMS/inventory layer bolted onto an accounting system** for Nordic SMBs. Its whole reason to exist: e-conomic/Business Central are weak at physical stock, so Rackbeat owns *items, locations, receiving, picking, BOM/production, valuation* and **posts the financial result back to the ledger**. That boundary — **WMS owns physical stock + goods-receipt; accounting owns the GL posting** — is *exactly* our spec's boundary (`11` §Dependencies: Inventory owns physical stock + GR, Economy owns posting). Rackbeat is, in effect, a proof that our chosen seam is the industry-standard one.

**What this means for us — two reads:**

1. **The mechanics are commodity and battle-tested.** Reorder points, FIFO/avg valuation, bin/location levels, cycle counting, partial goods-receipt, pick/pack/ship, serial/lot tracking, BOM assemblies — Rackbeat (and every WMS) implements these the same way because the domain is mature. **We copy these near-1:1.** Re-inventing them is wasted motion; the value is in *having* them, not in novelty.

2. **Rackbeat's intelligence ceiling is our floor.** Its "Smart Reordering" is **min-stock threshold + draft-PO generation** — reactive, history-blind, single-location. It has **no demand forecasting, no pipeline awareness, no van/mobile-stock model, no cross-engine reasoning**. That is precisely our wedge: we forecast from the **actual project/quote/design pipeline via A2A** (not sales history), treat the **van as a first-class stock location** (= VEHICLE shared node), and make every restock decision an auditable cognitive-ledger entry.

> **Positioning in one line:** *Not a WMS bolted onto accounting — an AI-native inventory engine on a cognitive graph: Rackbeat's proven stock mechanics, plus pipeline-driven forecasting, the van-as-shared-node, and serial→Asset seeding that no SMB WMS attempts.*

---

## 2. The landscape (what to borrow)

| Area | What Rackbeat does (verified from API/site) | The idea worth stealing |
|---|---|---|
| **Item master + variants** | `Products` + `Variations`, `Units`, `Item Groups`, `Collections`, custom `Fields` | Variant axis on SKU; custom-fields escape hatch maps to our PRODUCT/SKU node |
| **Serial numbers** | First-class `Serial Numbers` resource; registered **at goods-receipt**, follows item to **customer/invoice**; warranty + recall + where-located lookup | **The single most important AV mechanic** — serial captured at GR → seeds an `ASSET` |
| **Lot/batch** | `Lots` (per product) + `Batches` (inventory); FIFO/expiry auto-suggests oldest batch first | Batch model for consumables/cable; expiry → Watcher |
| **Locations / bins** | Multi-**level** locations (1 level Basic, 3 levels Premium); `Locations` + `Location Settings` per product | Multi-level = warehouse→zone→bin; our STOCK_LOCATION is the top level |
| **Multi-warehouse** | Multiple stock locations; `Transport Movements` (was Internal Movements) between them | Maps directly to warehouse↔van / van↔van `do_transfer_stock` |
| **Valuation** | `Cost Price Principle: FIFO` **and** `Average Cost` (chooseable); `Valuation` report | Copy both as a per-namespace config switch; valuation feeds Economy |
| **Reorder** | `Smart Reordering`: min-stock threshold → highlights below-min → **draft PO w/ primary supplier auto-selected**; `Reordering` report | Copy the mechanic; **replace the trigger** (history→pipeline) |
| **Stocktaking** | `Stock Count Drafts`, mobile + scanner count, `Stock Counting` report | Cycle-count workflow per location; van counts |
| **Adjustments** | `Inventory Adjustments` + `Adjustment Categories`; `Inventory Movements` / `Inventory Transactions` ledger | Typed, categorised adjustments = our audit trail / ledger rows |
| **Reservations** | `available = physical − reserved − blocked/quarantine`; reserve against orders/production | **The reservation algebra to adopt verbatim**; our `reserved_for->PROJECT` |
| **Goods receipt** | `Purchase Receipts` (+ lines); full/**partial** receipt → PO marked `delivered` / `partially received` | This *is* our GR→PO event + the BOM_LINE `DELIVERED` write |
| **Pick/pack/ship** | `Shipments` (+ lines), Pick & Pack, **Pick Routes**, full/partial shipment | Pick routes = van-loading / kit-staging order |
| **BOM / production** | `Bills of Material` (+ Lines/Prices/Units), `Item Assemblies`, `Production Orders` + `Registrations`, **Phantom BOM** | **Kitting a project's BOM** = assemble components into a staged kit |
| **Barcode / mobile** | Mobile app + scanner: receive, pick, count, move, serial/batch scan | The scanner substrate our `11` §Classification already assumes |
| **Returns** | Reverse of shipment/receipt; restock on return (no dedicated RMA object found) | We go **further**: dedicated `INVENTORY_RMA` + WEEE state |
| **Reporting** | `Valuation`, `Reordering`, `Stock Counting`, `Batch`, `Sales`, `Ledger` reports | KPI set: stock value, turnover; **we add dead-stock + forecast-shortfall** |
| **API / webhooks** | Full REST + **Webhooks** (event push) on all plans; OpenAPI + `llms.txt` index | The **entity list itself is the data-model spec** — see §3 mapping |
| **AI** | None verified. "Smart" = threshold rules, not ML/forecasting | The entire forecasting/cognitive layer is greenfield = our moat |

---

## 3. Idea backlog, prioritized & mapped to our engine

Each idea names the concrete change to `11-inventory-engine.md` (which `do_*` / node / config / table it touches).

### Tier A — copy near-1:1 (proven WMS mechanics, low regret)

**A1. Serial number as a first-class resource, captured at goods-receipt → asset-seed** *(the highest-leverage AV idea)*
Rackbeat registers a serial **the moment goods are received**, carries it to the customer/invoice, and uses it for warranty + recall + where-located lookup. For an AV integrator this is the **birth event of an installed asset**. Capture serial in `do_record_goods_receipt` `scans[]`, store on the `GOODS_RECEIPT -[of]-> SKU` edge, and **emit the serial→Assets engine as the seed of an `ASSET` node** when stock is consumed/installed on a work-order.
- *Touches:* `do_record_goods_receipt` (serial capture), new `serials` concept on `inventory_items`/`goods_receipts`; A2A hand-off Inventory→Assets at consumption (the `serienr→asset` spine note, `11` Andreas `:128`); `INVENTORY_ITEM` carries serial for serialized SKUs.
- *Why it's our edge:* Rackbeat stops at warranty/recall; **we turn the serial into the asset graph** that Support/Field Tech reason over. This is the AV-specific compounding move.

**A2. The reservation algebra: `available = physical − reserved − blocked`** *(adopt verbatim)*
Rackbeat's clean separation of physical vs reserved vs available/quarantine is the correct stock model. Adopt it exactly: `inventory_items` already has `qty_on_hand` + `qty_reserved`; add a derived `available` and an optional `qty_blocked` (quarantine/RMA-hold). `do_reserve_stock` writes `reserved_for->PROJECT`; the "own stock first" read Procurement calls must report **available**, not on-hand.
- *Touches:* `do_stock_levels` returns `{on_hand, reserved, blocked, available}`; `do_reserve_stock`; the Procurement 5-step "own stock first" contract reads `available`.

**A3. FIFO + weighted-average valuation as a config switch** *(copy both)*
Rackbeat lets the tenant pick FIFO or Average Cost; both drive the `Valuation` report. Copy this exactly — per-namespace valuation method in config; valuation is the number Inventory hands to **Economy** for posting (the boundary holds: we value, Economy posts).
- *Touches:* new config key `NCE_INVENTORY_VALUATION_METHOD` (`fifo|average`); a valuation read consumed by Economy; `inventory_transactions` ledger is the FIFO layer source.

**A4. Partial goods-receipt → PO status + BOM_LINE `DELIVERED`** *(the keystone, already in spec — Rackbeat confirms the mechanic)*
Rackbeat: a receipt marks the PO `delivered`, or `partially received` if qty < ordered. This is **exactly** our Receive→Match→Cascade and the roadmap §9.1 rule that **Warehouse writes `BOM_LINE.status = DELIVERED`** at goods-receipt. Confirm partial-receipt handling in our model: a partial GR advances only the received lines; PO stays open for the remainder; only fully-received lines flip their `BOM_LINE` to `DELIVERED`.
- *Touches:* `do_record_goods_receipt` (per-line partial qty), `GOODS_RECEIPT -[against]-> PO`; fires `procurement_evaluate_match`; writes `BOM_LINE.status=DELIVERED` per fully-received line (validated: no `DELIVERED` before `ORDERED`).

**A5. Typed inventory transaction ledger + categorised adjustments** *(audit substrate)*
Rackbeat separates `Inventory Movements` / `Inventory Transactions` (the immutable event log) from `Inventory Adjustments` + `Adjustment Categories` (typed corrections). Adopt this split: every qty change is an append-only transaction with a typed reason. This *is* our `v3_cognitive_ledger` discipline applied to stock, and makes "why is this count what it is" answerable.
- *Touches:* `inventory_transactions` table (append-only), adjustment categories config; feeds the ledger-backed restock rationale.

**A6. Multi-level locations (warehouse→zone→bin)** *(copy the hierarchy)*
Rackbeat's 3-level locations are how a warehouse is actually navigated. Make `STOCK_LOCATION` hierarchical: a van is a top-level location with no sub-bins; the main warehouse has zone/bin children. Pick Routes (below) traverse this.
- *Touches:* `stock_locations` gains parent/level; `STOCK_LOCATION` (a FUNCTIONAL_LOCATION) nests.

### Tier B — copy-and-improve (where our graph/pipeline beats Rackbeat)

**B1. Van as a first-class stock location = the VEHICLE+STOCK_LOCATION shared node** *(our structural edge)*
Rackbeat has multi-location + inter-location `Transport Movements` but **no concept of a mobile/vehicle stock location**. Our roadmap §9.1 makes **van = one shared node (VEHICLE owned by Resources + STOCK_LOCATION owned by Inventory)**. So a van restock is literally a `Transport Movement` from warehouse→van, and "what's on van-3" is a stock-levels read on that shared node. This is mechanics Rackbeat has, pointed at a node type it lacks.
- *Touches:* `do_transfer_stock` (warehouse↔van), `STOCK_LOCATION.kind=van` keyed to `vehicle_ref`; the §9.1 shared-node lifecycle (Resources writes VEHICLE identity, Inventory writes its stock).

**B2. Pipeline-driven demand forecast** *(the wedge — Rackbeat reorders from history; we forecast from the pipeline)*
Rackbeat's "Smart Reordering" is min-threshold only. We keep that as the floor and add `do_forecast_demand`: future SKU demand implied by **open quotes/designs/signed projects** read via A2A from Sales/System Design/Project, weighted by pipeline stage (`inventory-forecast-weights.json` — a draft quote implies less than a signed project). This pre-positions stock **before** the threshold is hit, which a history-only system structurally cannot do.
- *Touches:* `do_forecast_demand`, `inventory-forecast-weights.json`, A2A reads of pipeline nodes; drives `do_reserve_stock` pre-positioning.

**B3. Kit a project's BOM (Item Assemblies / Production Orders, repurposed)** *(AV project-kitting)*
Rackbeat assembles components into a finished/kitted item via `Item Assemblies` + `Production Orders` + Phantom BOM. We repurpose this for **project kitting**: given a project's frozen BOM, reserve + stage the components as a kit on a van (`INVENTORY_ITEM -[kitted_for]-> PROJECT -[staged_on]-> VEHICLE`, roadmap §9.1). A "kit" is a phantom BOM — components stay tracked individually, but reserved and picked as one unit, in pick-route order.
- *Touches:* a `do_kit_project` capability (assemble = reserve+stage a project BOM), `kitted_for`/`staged_on` edges, Pick Routes ordering; consumes the Sales-frozen BOM content.

**B4. Smart Reordering, upgraded to van-aware + work-order-aware restock** *(copy mechanic, richer inputs)*
Keep Rackbeat's "below-min → draft PO, primary supplier auto-selected" exactly — but `do_recommend_restock` blends **consumption velocity × upcoming Field-Tech work-orders × van capacity**, not just a static minimum. Output remains a draft restock request, but it routes through Procurement (`do_create_restock_po` → `procurement_generate_po`), preserving our boundary (Inventory recommends; Procurement sources).
- *Touches:* `do_recommend_restock`, `do_create_restock_po`, `inventory-reorder-points.json` (per-SKU/per-location + van capacity), autonomy gate `NCE_INVENTORY_AUTONOMY_RESTOCK_CEILING`.

**B5. Lot/batch + expiry + serial recall → Watcher events** *(copy, wire to cognition)*
Rackbeat auto-suggests oldest batch (FIFO/expiry) and traces a serial's whole series for recall. Adopt batch-FIFO suggestion in picking; turn expiry and recall into **Watcher** alerts (expiring warranty-bound SKUs; "this serial's series is recalled — here are the affected installs/customers" via the asset graph).
- *Touches:* batch model on `inventory_items`; Watcher expiry/recall alerts; recall lookup traverses serial→ASSET→Support.

### Tier C — later / opportunistic

- **C1. Quality-assurance / quarantine hold** — Rackbeat's `blocked/quarantine` bucket: a QA-hold state on inbound stock before it's available (damaged-on-receipt, awaiting inspection). Maps to `qty_blocked`; ties to RMA reverse-logistics.
- **C2. Drop-ship / multi-channel rules** — Rackbeat lets you define backorder-accept + drop-ship rules per channel. For us: a "ship direct from supplier to site" flag on a PO line that skips warehouse stock (no GR into inventory, but still advances BOM_LINE). Relevant for large AV gear delivered straight to a project site.
- **C3. Webhook-style internal event contract** — Rackbeat's webhook catalogue (order created, stock changed, receipt made…) is a good template for **our A2A event taxonomy** — what inventory events other engines subscribe to. We don't expose webhooks externally, but the *event list* is a design checklist.
- **C4. Supplier-product linkage on items** — Rackbeat's per-product `Suppliers` + `Supplier Products` (primary supplier, supplier SKU). We don't own sourcing (Procurement does), but the **primary-supplier hint on a SKU** is what makes restock-PO auto-fill work — confirm Product/Procurement carries it.
- **C5. Reporting parity + our two extras** — match Rackbeat's Valuation/Reordering/Stock-Counting reports, then add **dead-stock** (no movement in N days) and **forecast-shortfall** as the inventory slice of the Morning-brief.

---

## 4. Net changes to fold into `11-inventory-engine.md`

Load-bearing (Tier A) — should update the spec now:
- **A1** serial captured at GR → **A2A seed to Assets** (new cross-engine hand-off; currently the spec only fires Procurement on GR).
- **A2** the **`available = on_hand − reserved − blocked`** algebra made explicit in `do_stock_levels` (add `qty_blocked`); "own stock first" reads `available`.
- **A3** **valuation-method config** (`fifo|average`) + a valuation read consumed by Economy — currently unstated in `11`.
- **A4** **partial-receipt semantics** spelled out against the BOM_LINE `DELIVERED` transition (per-line, only fully-received lines flip).
- **A5** **`inventory_transactions` append-only ledger + typed adjustment categories** added to Tables/migrations.
- **A6** **hierarchical STOCK_LOCATION** (warehouse→zone→bin; van = flat top-level).

The Tier B items (van-shared-node, pipeline forecast, project-kitting, upgraded restock, recall-Watcher) are **already the spec's stated direction** — this research confirms the mechanics and sharpens the edges/`do_*` shapes. Tier C stays as backlog. A short "Research-informed direction" pointer is added to `11` referencing this doc.

---

## 5. Honest flags (don't over-trust the marketing)
- **No verified AI in Rackbeat.** "Smart Reordering" is **threshold rules + draft-PO generation**, not ML/forecasting. The demand-forecasting/dead-stock search returned **generic market articles, not Rackbeat features** — treat Rackbeat as having *no* predictive layer. (Good for us: our forecasting is genuinely differentiated, not table-stakes.)
- **No dedicated RMA object found** in the API entity index — returns appear to be handled as reverse shipments/receipts + restock. Our dedicated `INVENTORY_RMA` + WEEE state is a **genuine addition**, not a copy.
- **The detailed entity/field list comes from the API `llms.txt` index + reference**, which is reliable for *names of resources* but I did **not** verify every field of every entity (would require deep per-endpoint fetches). The §2/§3 entity names are trustworthy; specific field claims beyond what's quoted are inference.
- **Asset linkage is our inference.** Rackbeat's serial tracking explicitly stops at warranty/recall/where-located — it does **not** model a fixed-asset graph. A1's "serial→ASSET seed" is *our* extension of a mechanic Rackbeat has, not a Rackbeat feature.
- **Plan-gating:** serial-number management, 3-level locations, and batch are **premium/add-on** in Rackbeat — irrelevant to us (we build all tiers natively) but noted so we don't mistake tier-locked features for absent ones.
- **Multi-level "locations" ≠ multi-warehouse for everyone.** Basic = 1 level; the bin/zone hierarchy is a Premium concept. We treat hierarchy as core.

---

## Sources

**Rackbeat — product & mechanics:** [Features (full tier matrix)](https://rackbeat.com/en/features/) · [Homepage / what it is](https://rackbeat.com/en/) · [Serial-number management](https://rackbeat.com/en/serial-number-management/) · [Serial-number traceability (receipt→customer→recall)](https://rackbeat.com/en/inventory-management-with-serial-numbers-gain-full-control-of-traceability/) · [Trace purchases & sales with serial numbers](https://rackbeat.com/en/product_features/trace-your-purchases-and-sales-with-serial-numbers/) · [Batch tracking for wholesalers](https://rackbeat.com/en/batch-tracking-for-products-how-to-maintain-full-control-as-a-wholesaler/) · [Batch number glossary](https://rackbeat.com/en/glossaries/batch-number/) · [FIFO valuation](https://rackbeat.com/en/product_features/inventory-valuation-method-fifo/) · [Smart Reordering](https://rackbeat.com/en/product_features/reorder-reminders/) · [Reorder point (ROP) glossary](https://rackbeat.com/en/glossaries/reorder-point-rop/) · [Registration of goods (receipt)](https://rackbeat.com/en/product_features/the-registration-of-goods/) · [Bills of Materials](https://rackbeat.com/en/product_features/bills-of-materials-bom/) · [BOM glossary](https://rackbeat.com/en/glossaries/bom-bill-of-materials/) · [Phantom BOM](https://rackbeat.com/en/glossaries/phantom-bom/) · [What Rackbeat does for manufacturing](https://rackbeat.com/en/what-can-rackbeat-do-for-my-manufacturing/) · [Reserved inventory in production](https://rackbeat.com/en/reserved-inventory-in-production/) · [Parallel sales/inventory channels (backorder/drop-ship)](https://rackbeat.com/en/how-to-manage-parallel-sales-and-inventory-channels/) · [E-commerce](https://rackbeat.com/en/ecommerce/) · [Add-ons](https://rackbeat.com/en/add-ons/)

**Rackbeat — developer / data model:** [Developer Hub](https://developer.rackbeat.com/) · [API entity/endpoint index (llms.txt)](https://developer.rackbeat.com/llms.txt) · [Intro to Webhooks](https://developer.rackbeat.com/reference/intro-to-webhooks) · [REST APIs vs Webhooks](https://developer.rackbeat.com/reference/rest-apis-vs-webhooks) · [API reference (Stoplight)](https://rackbeat.stoplight.io/docs/api/ap3odmy211lg8-rackbeat)

**Rackbeat — helpdesk / flows:** [How to create a receipt (partial receipt)](https://helpdesk.rackbeat.com/knowledge/ht-00011) · [FAQ](https://helpdesk.rackbeat.com/knowledge/ht-00103) · [Setup A–Z](https://helpdesk.rackbeat.com/knowledge/ht-00004) · [Visma e-conomic integration setup](https://helpdesk.rackbeat.com/knowledge/ht-01002)

**Company / market:** [Rackbeat acquired by Visma (Nordic9)](https://nordic9.com/news/rackbeat-was-acquired-by-visma/) · [GetApp profile](https://www.getapp.com/operations-management-software/a/rackbeat/) · [Software Advice profile](https://www.softwareadvice.com/scm/rackbeat-profile/) · [Trustpilot reviews](https://www.trustpilot.com/review/rackbeat.com)
