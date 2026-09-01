> **Status:** shipped · **Verified-against:** 7e97efe (main) · **Last-audited:** 2026-09-01

# NCE Vertical Engines — Build & Production Status

> **For frontend planning.** 18 core modules (+2 Operations-axis extensions, unscheduled) · 229 build waves · Verified-against: `7e97efe`.
> Source of truth: `nce/tool_registry.py` (135 MCP tools registered), `nce/admin_app.py` (REST routes), and the Module Ledger (`vertical_modules/dev/prompts/ML.md`).

> [!NOTE]
> **Production & Main Status:** Vertical engines M0–M8 (Shared Core, Product, Procurement, System Design, Project, Sales, Vendors, Agreements, Economy) along with Dynamics 365, Diagnostics, and NetBox integrations are merged to `main` at commit `7e97efe`. Total MCP tools in registry: **135** (and 134 REST routes).

**Status legend**

| Badge | Meaning |
|---|---|
| ✅ **Complete** | Built, test-gated, and merged to `main` (`7304330`). |
| 🟡 **In progress** | Partially built and merged to `main` (active wave development). |
| ⬜ **Planned** | Fully specced (see `docs/vertical_engines/`), not started. |

---

## Foundation — Core Cognitive Spine

| Engine | Module | Waves (Batches) | Build | Status | What it provides |
|---|---|---|---|---|---|
| Shared Core | M0 | 30 · B1–B30 | ✅ Complete | Main (`7304330`) | The cognitive-graph spine all engines stand on: entity resolution, shared pricing, autonomy governance, event bus, retrieval-grounded generation, external-principal RLS, field redaction, source-mode resolver, signing, no-person-grain guard (C1–C9). |

## Tier 1 — Core Operating Spine
*Build order: Product → Procurement → System Design → Project*

| Engine | Module | Waves (Batches) | Build | Status | What it owns / does |
|---|---|---|---|---|---|
| Product | M2 | 13 · B31–B43 | ✅ Complete | Main (`7304330`) | Owns `PRODUCT`/`SKU`. Nettailer catalog ingest, search, pricing, on-demand enrichment. Exposes 6 MCP tools and 3 REST routes. |
| Procurement | M1 | 12 · B44–B55 | ✅ Complete | Main (`7304330`) | Owns `PO`. TCO, supplier ranking, 3-way match, human-confirmed PO submission, rebate forecasting. Exposes 6 MCP tools and 8 REST routes (3 domain cores unwired). |
| System Design | M6 | 12 · B56–B67 | ✅ Complete | Main (`7304330`) | Owns `DESIGN`/`FUNCTIONAL_LOCATION`. Design↔quote, SoW, NetBox/SharePoint/Lucid export, and device-capability validation. Exposes 7 MCP tools and 5 REST routes. |
| Project | M7 | 13 of 13 · B68–B79d | ✅ Complete | Main (`7304330`) | Owns `PROJECT`/`GATE`/`TASK`. Phase-gate state machine (G0–G5), signed-quote→project conversion, phase advance, my-day, capacity calendar, scope creep detection, and status reports. Exposes 4 MCP tools and 7 REST routes. |

## Tier 2 — Commercial & Financial Spine
*Build order: Sales → Vendors → Agreements → Economy*

| Engine | Module | Waves (Batches) | Build | Status | What it owns / does |
|---|---|---|---|---|---|
| Sales | M5 | 14 · B80–B93 | ✅ Complete | Main (`7304330`) | Owns `CUSTOMER`/`LEAD`/`QUOTE`/`SIGNED_BASELINE`. Replaces `steps_d365`; freezes the signed baseline Project reads. Exposes 2 MCP tools and 15 REST routes (including public quote endpoint). |
| Vendors & Contractors | M4 | 11 · B94–B104 | ✅ Complete | Main (`7304330`) | Owns `VENDOR`/`CONTRACTOR`. Scorecards, tier status, reliability degradation detection, contractor matching, performance calculation. Exposes 10 MCP tools and 2 REST routes. |
| Agreements | M3 | 11 · B105–B115 | ✅ Complete | Main (`7304330`) | Owns `AGREEMENT`. OCR extraction, coverage matrix, kickback governance, and review workflows. Exposes 1 MCP tool and 5 REST routes. |
| Economy | M8 | 13 · B116–B128 | ✅ Complete | Main (`7304330`) | Owns `INVOICE`/`POSTING`/`MARGIN`. Approval cascade, invoice matching, periodisering, financial event dispatch, Finago GL reconciliation, recurring revenue, KID validation, dunning. Exposes 3 MCP tools and 3 REST routes (9 domain cores unwired). |

## Tier 3 — Delivery & Field Ops
*Inventory → Assets → Support → Resources → Field Tech*

| Engine | Module | Waves (Batches) | Build | Status | What it owns / does |
|---|---|---|---|---|---|
| Warehouse & Inventory | M11 | 12 · B129–B140a | ✅ Complete | Main (`7e97efe`) | Owns `STOCK_LOCATION`/`GOODS_RECEIPT`. Migrations 050–053, 3 tools, and 3 routes shipped. Includes 11 entry points and opt-in gate. |
| Assets | M9 | 12 · B141–B152 | ✅ Complete | Main (`7e97efe`) | Owns `ASSET`/`TELEMETRY`. 14-state lifecycle, health scoring, warranty/EOL. `054_assets.sql` + 4 tools + 3 routes (`/api/assets`, `/api/assets/{id}`, `/api/assets/{id}/lifecycle`) shipped. |
| Support | M10 | 11 · B153–B163 | ⬜ Planned | Not started | Owns `TICKET` + SLA clock. Grounded troubleshooter, customer-health scoring. |
| Staff & Resources | M15 | 12 · B164–B175 | ⬜ Planned | Not started | Owns `RESOURCE`/`ALLOCATION`. Capacity calendar, conflict-free scheduling, travel/per-diem. |
| Field Tech | M12 | 12 · B176–B187 | ⬜ Planned | Not started | Owns `WORK_ORDER`. Offline-sync mobile capture, dispatch, partner access (dual RLS). |
| Network Ops (Edge) | M18 (TBD) | unscheduled | ⬜ Planned | Not started | Engine 18 extension. Edge collector (SNMP/syslog/traps/Zeek/capture) reconciled vs intent → fault → Support ticket → Field Tech WO. |
| Remote Access & RMM | M19 (TBD) | unscheduled | ⬜ Planned | Not started | Engine 19 extension. GoTo Resolve/LogMeIn adapter — endpoint monitoring, proactive ticketing, remote-fix deflection. |

## Tier 4 — Intelligence & External
*HR → Marketing → Business Insights → Customer Portal*

| Engine | Module | Waves (Batches) | Build | Status | What it owns / does |
|---|---|---|---|---|---|
| HR | M13 | 11 · B188–B198 | ⬜ Planned | Not started | Owns `EMPLOYEE`/`SKILL`/`CERT`. Master data; EU-AI-Act-constrained (no individual ranking). |
| Marketing | M14 | 10 · B199–B208 | ⬜ Planned | Not started | Case-study generation, testimonials, AEO/GEO content. Grounded + redacted; human-gated publishing. |
| Business Insights | M16 | 10 · B209–B218 | ⬜ Planned | Not started | KPI cockpit, morning brief, cross-engine risk radar, "ask your business". Pure consumer — writes no other engine's data. |
| Customer Portal | M17 | 11 · B219–B229 | ⬜ Planned | Not started | External customer surface — owns `PORTAL_USER`/`SERVICE_REQUEST`. DPIA-gated, hardened security boundary. |

---

## Surface of Truth — Tools & Routes by Engine

The following table provides the ground truth of all exposed MCP tools and mounted REST routes per engine at `7304330`:

| Engine | Tools (Flags) | Routes | Cores (`do_*` unwired) | Frontend Build Guidance |
|---|---|---|---|---|
| **agreements** (M3) | **1 tool:**<br>`agreements_lookup_terms` *(cacheable)* | **5 routes:**<br>`POST /api/agreements`<br>`POST /api/agreements/coverage`<br>`GET /api/agreements/{id}`<br>`POST /api/agreements/extract`<br>`POST /api/agreements/review` | — | Build FE against the 5 REST routes for agreement lifecycle, extraction, coverage analysis, and review. |
| **assets** (M9) | **4 tools** | **3 routes:**<br>`/api/assets`<br>`/api/assets/{id}`<br>`/api/assets/{id}/lifecycle` | — | Live. 14-state lifecycle and health scoring available. |
| **diagnostics** | **5 tools:**<br>`diag_ingest_bundle` *(mutation)*<br>`diag_commit_bundle` *(mutation)*<br>`diag_digest_status` *(cacheable)*<br>`diag_device_health` *(cacheable)*<br>`diag_list_anomalies` *(cacheable)* | — | — | Diagnostic bundle ingestion and anomaly inspection available via MCP tools. |
| **dynamics365** | **6 tools:**<br>`d365_query_case` *(cacheable)*<br>`d365_sync_now` *(admin_only, mutation)*<br>`d365_case_stress_report` *(cacheable)*<br>`d365_list_sla_breaches` *(admin_only)*<br>`d365_netbox_mappings` *(cacheable)*<br>`d365_sync_status` | Admin routes under `/api/admin/d365/*` | — | D365 case queries and SLA monitoring available via MCP and admin REST API. |
| **economy** (M8) | **3 tools:**<br>`economy_match_invoice` *(cacheable)*<br>`economy_compute_periodisering` *(cacheable)*<br>`economy_emit_event` *(cacheable)* | **3 routes:**<br>`POST /api/economy/match-invoice`<br>`POST /api/economy/periodisering`<br>`POST /api/economy/emit-event` | `do_compute_bucket_targets`<br>`do_compute_dunning`<br>`do_compute_recognition_schedule`<br>`do_emit_financial_event`<br>`do_forecast_cashflow`<br>`do_generate_kid`<br>`do_match_invoice`<br>`do_snapshot_mrr_arr_churn`<br>`do_validate_kid` | Build FE against the 3 exposed REST routes. Note that 9 internal `do_*` domain cores are not yet wired to HTTP/MCP surfaces. |
| **inventory** (M11) | **3 tools**<br>(Plus 11 entry points from PR #152) | **3 routes** | — | Fully stable. Stock tables and stock-level operations live with opt-in gate. |
| **netbox** | **1 tool:**<br>`evaluate_circuit_impact` | Admin routes under `/api/admin/d365/netbox-*` | — | NetBox circuit impact assessment tool available. |
| **procurement** (M1) | **6 tools:**<br>`procurement_calculate_tco` *(cacheable)*<br>`procurement_rank_suppliers` *(cacheable)*<br>`procurement_evaluate_match` *(cacheable)*<br>`procurement_forecast_rebate` *(cacheable)*<br>`procurement_recommend_move_spend` *(cacheable)*<br>`procurement_whatif_spend` *(cacheable)* | **8 routes:**<br>`POST /api/procurement/tco`<br>`POST /api/procurement/rank`<br>`POST /api/procurement/match`<br>`POST /api/procurement/sync`<br>`GET /api/procurement/sync/status`<br>`POST /api/procurement/frontier/forecast-rebate`<br>`POST /api/procurement/frontier/recommend-move-spend`<br>`POST /api/procurement/frontier/whatif-spend` | `do_calculate_tco`<br>`do_evaluate_three_way_match`<br>`do_rank_suppliers` | Stable for FE integration across all 8 REST routes and 6 MCP calculation tools. |
| **product** (M2) | **6 tools:**<br>`product_search` *(cacheable)*<br>`product_get` *(cacheable)*<br>`product_price` *(cacheable)*<br>`product_related` *(cacheable)*<br>`product_match_bom_line`<br>`product_enrich` *(mutation)* | **3 routes:**<br>`GET /api/product/search`<br>`POST /api/product/enrichment/review`<br>`GET /api/product/{id}` | — | Fully stable. Catalog search, detail, and enrichment review routes are live. |
| **project** (M7) | **4 tools:**<br>`project_can_enter_phase` *(cacheable)*<br>`project_convert_signed_quote` *(admin_only, mutation)*<br>`project_advance_phase` *(admin_only, mutation)*<br>`project_suggest_pl` *(cacheable)* | **7 routes:**<br>`POST /api/project/convert-signed-quote`<br>`GET /api/project/{id}/phase`<br>`POST /api/project/{id}/phase`<br>`GET /api/project/my-day`<br>`GET /api/project/capacity`<br>`GET /api/project/{id}/scope-creep`<br>`GET /api/project/{id}/status-report` | — | Fully stable across 13 waves. Build project management UI against the 7 REST routes (phase gates, conversion, my-day, capacity, scope creep, status report). |
| **sales** (M5) | **2 tools:**<br>`sales_ping` *(cacheable)*<br>`sales_get_signed_baseline` | **15 routes:**<br>`GET /public-api/sales/quotes/{id}`<br>`GET /api/admin/sales/source-mode`<br>`PUT /api/admin/sales/source-mode`<br>`GET /api/sales/customers`<br>`GET /api/sales/customers/{id}`<br>`GET /api/sales/overview`<br>`GET /api/sales/seller-detail/{user}`<br>`GET /api/sales/dashboard`<br>`GET /api/sales/stats`<br>`GET /api/sales/manager`<br>`GET /api/sales/agreements`<br>`GET /api/sales/agreements/{id}`<br>`GET /api/sales/quotes/{id}`<br>`GET /api/sales/targets`<br>`PUT /api/sales/targets` | — | Primary integration path is REST (15 endpoints for customer profiles, dashboards, quotes, targets, agreements). MCP tool `sales_get_signed_baseline` serves A2A contract consumption. |
| **system_design** (M6) | **2 tools:**<br>`system_design_ping` *(cacheable)*<br>`system_design_publish_design_docs` *(mutation)* | **1 route:**<br>`POST /api/system-design/publish-design-docs` | — | **Limited surface.** Only document publishing is currently exposed. *(See critical warning below)* |
| **vendors** (M4) | **10 tools:**<br>`vendors_get_vendor` *(cacheable)*<br>`vendors_compute_scorecard` *(cacheable)*<br>`vendors_get_tier_status` *(cacheable)*<br>`vendors_detect_reliability_degradation` *(cacheable)*<br>`vendors_check_tier_at_risk` *(cacheable)*<br>`vendors_match_contractor` *(cacheable)*<br>`vendors_compute_performance` *(cacheable)*<br>`vendors_recall_similar_jobs` *(cacheable)*<br>`vendors_reliability_radar` *(cacheable)*<br>`vendors_calibrate_weights` *(cacheable)* | **2 routes:**<br>`GET /api/vendors/scorecard`<br>`GET /api/vendors/{id}` | — | Rich tool surface (10 MCP tools) for contractor matching, scorecards, and tier risk. REST endpoints for vendor detail and scorecard queries. |
| **shared** | **66 tools** (memory, graph, search, A2A, migrations, admin, snapshots, entity resolution, pricing, DLQ, quotas, signing) | **67 routes** (health, search, replay, snapshots, A2A grants, admin settings, security, datastores, namespaces, entity resolution) | — | Platform and administrative capabilities. |

---

## Critical Frontend Planning Notes & Warnings

> [!WARNING]
> ### System Design (M6) Surface Constraint
> System Design is marked ✅ Complete in terms of wave milestones (B56–B67), but its nine internal modules (`from_quote`, `to_quote`, `validate`, `devices`, `propose`, `lucid`, `netbox_bridge`, `sharepoint`, `sow`) are currently **unmounted**. The exposed HTTP/MCP surface is strictly limited to 2 MCP tools and 1 REST route:
> - MCP: `system_design_ping` and `system_design_publish_design_docs`.
> - REST: `POST /api/system-design/publish-design-docs`.
>
> Frontend developers **must not** assume the existence of interactive design-canvas endpoints, BOM-sync routes, or NetBox/Lucid direct REST APIs. *Note: ML wave 230a will mount these capabilities to the network.*

> [!IMPORTANT]
> ### Economy (M8) Built vs. Exposed Cores
> Economy has completed all 13 build waves (B116–B128). However, only 3 endpoints and 3 tools are exposed today (`economy_match_invoice`, `economy_compute_periodisering`, `economy_emit_event`). 9 domain cores (including cashflow forecasting, dunning computation, KID generation/validation, MRR/ARR snapshots, and recognition schedules) reside in `nce/vertical_modules/economy/` as internal modules and are not yet mounted to HTTP routes.

> [!NOTE]
> ### Integration Protocols
> - **Authentication:** All `/api/*` administrative endpoints require the HMAC three-header protocol (`X-NCE-Timestamp`, `Authorization: HMAC-SHA256`, `X-NCE-Nonce`) and mTLS in production.
> - **Public Endpoints:** `GET /public-api/sales/quotes/{id}` is a public quote viewing endpoint authenticated via bearer token signature `HMAC-SHA256(NCE_MASTER_KEY, quote_id)`.
