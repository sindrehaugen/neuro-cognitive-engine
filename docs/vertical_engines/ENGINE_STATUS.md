> **Status:** shipped · **Verified-against:** `ca0d22a` (public `main`) · **Last-audited:** 2026-09-21

# NCE Vertical Engines — Build & Production Status

> **For frontend planning.** **All 17 of 17 numbered engines are merged and running on public
> `main`** (+8 supporting integrations and shared-core resource modules, +2 Operations-axis
> extensions not yet scheduled). Every engine with a registered tool has a
> `docs/engines/<engine>-user.md` guide (enforced by `tests/test_docs_engine_guides_ratchet.py`).
> **Repository of record since 2026-09-05: `sindrehaugen/neuro-cognitive-engine` (public, AGPL-3.0).**
> Source of truth for every number on this page: `nce/tool_registry.py`, `nce/admin_app.py`, and
> `nce/vertical_modules/`, read by `scripts/gen_surface_table.py` into
> [`docs/_generated/surface.md`](../_generated/surface.md) and by `scripts/gen_engine_figures.py` into
> [`docs/_generated/engine_figures.md`](../_generated/engine_figures.md).

> [!IMPORTANT]
> **Always trust the generated inventories over prose tables.** If any per-engine count below
> disagrees with [`docs/_generated/surface.md`](../_generated/surface.md) or
> [`docs/_generated/engine_figures.md`](../_generated/engine_figures.md), **the generated files are
> authoritative** — regenerate with:
> ```
> python scripts/gen_surface_table.py --repo . --baseline HEAD --out docs/_generated/surface.md
> python scripts/gen_engine_figures.py --repo . --baseline HEAD --out docs/_generated/engine_figures.md --update-status docs/vertical_engines/ENGINE_STATUS.md
> ```

## Measured inventory at Wave A-5

| Instrument | Value |
|---|---|
| Module packages under `nce/vertical_modules/` | **25** (17 numbered engines + `diagnostics`, `dynamics365`, `netbox`, `legal_entities`, `geodata`, `notifications`, `documents`, `sites`) |
| `TOOL_REGISTRY` entries | **526** MCP tools (70 shared + 456 engine) — see [`docs/_generated/engine_figures.md`](../_generated/engine_figures.md) |
| REST routes on the admin app | **1,168** across `shared` and vertical modules in [`docs/_generated/surface.md`](../_generated/surface.md) (+12 on the Customer Portal's own app shell) — see [`docs/_generated/surface.md`](../_generated/surface.md) as authoritative |
| `do_*` domain cores | **293** distinct names across the 25 packages (302 per-module entries in [`docs/_generated/surface.md`](../_generated/surface.md); 316 `file::do_*` definitions) |
| SQL migrations | 104 files (+1 optional), `001` → `108` — gaps at `002`, `009`, `059` (never allocated); `010` exists only under `nce/migrations/optional/`. — see [`docs/_generated/engine_figures.md`](../_generated/engine_figures.md) |
| `EXPECTED_TENANT_RLS_TABLES` | **118** |
| Tests | **6,430** `def test_` across 529 files |
| Golden Thread seam burndown | **4 of 37** lifecycle steps broken (4 distinct seams) — mirrored from [`docs/_generated/golden_thread_seams.md`](../_generated/golden_thread_seams.md); see [`docs/_generated/engine_figures.md`](../_generated/engine_figures.md) |
| v1.6 C12 resource-surface registrations | **19 of 25** engines have `resources.py` — see [`docs/_generated/engine_figures.md`](../_generated/engine_figures.md) |

**Status legend**

| Badge | Meaning |
|---|---|
| ✅ **Live** | Merged to public `main`, CI-gated, tools and routes registered. |
| 🟡 **In review** | Complete on a branch with an open PR. |
| ⬜ **Planned** | Specced, not built. |

> [!NOTE]
> **Live-database verification:** In addition to per-module unit tests, **33 of 37** cross-engine
> lifecycle steps in [`tests/integration/test_golden_thread.py`](../../tests/integration/test_golden_thread.py)
> run end-to-end against live Postgres in CI — see
> [`docs/_generated/golden_thread_seams.md`](../_generated/golden_thread_seams.md) for the exact step-by-step status.

---

## Foundation — Core Cognitive Spine

> *Per-engine Tool, Route, and Core counts in the tables below reflect [`docs/_generated/surface.md`](../_generated/surface.md) (including C12 resource-surface auto-mounted tools and routes). [`docs/_generated/surface.md`](../_generated/surface.md) is authoritative.*

| Engine | Module | Build | Tools | Routes | Cores | What it provides |
|---|---|---|---|---|---|---|
| Shared Core | M0 | ✅ Live | 70 | 99 | — | The cognitive-graph spine every engine stands on: entity resolution (C1), autonomy governance (C2), external-principal RLS (C3), event bus (C4), source-mode resolver (C5), shared pricing (C6), signing (C7), field redaction (C8), retrieval-grounded generation and the no-person-grain guard (C9a/C9b), decision feedback (C10), and the generic resource surface (C12). |

## Tier 1 — Core Operating Spine

| Engine | Module | Build | Tools | Routes | Cores | What it owns / does |
|---|---|---|---|---|---|---|
| Product | M2 | ✅ Live | 15 | 34 | 11 | Owns `PRODUCT`/`SKU`/`PACKAGE`. Nettailer ingest (real HTTP), ETIM catalog, C1 heavy client with merge queue, `product_match_bom_line`, EOL watcher on cron, structured LLM enrichment (`product/enrich.py` via `provider.complete`, Wave P-1), C12 `product-skus` and `packages` surfaces. |
| Procurement | M1 | ✅ Live | 16 | 37 | 12 | Owns `PO`/`DEAL_REGISTRATION`. TCO, 5-step supplier ranking, 3-way match, PO generation & submission, rebate forecasting, frontier advisors, C12 `po-lines` and `deal-registrations` surfaces. |
| System Design | M6 | ✅ Live | 73 | 157 | 26 | Owns `DESIGN`/`FUNCTIONAL_LOCATION`. Topology authoring with `expected_version`, FL hierarchy tree & room categories, design requests, geometry, validation, SoW, Lucid/SharePoint export (real HTTP), NetBox bridge, design↔quote through the shared BOM store, 7 C12 resource surfaces. |
| Project | M7 | ✅ Live | 15 | 27 | 11 | Owns `PROJECT`/`GATE`/`TASK`. G0–G5 phase gates, signed-quote→project conversion, my-day, capacity, scope-creep detection, status reports, outcome recording & similar-project recall, governed BOM sync, C12 `projects` surface. |

## Tier 2 — Commercial & Financial Spine

| Engine | Module | Build | Tools | Routes | Cores | What it owns / does |
|---|---|---|---|---|---|---|
| Sales | M5 | ✅ Live | 47 | 148 | 36 | Owns `CUSTOMER`/`LEAD`/`DEAL`/`QUOTE`/`SIGNED_BASELINE`. Full read/write model, `d365`/`both`/`nce` source-mode flip with parity window, public quote endpoint (HMAC token), quote-line tools landing BOM_LINEs, C7 signature request (`sales_request_signature`), 8 C12 resource surfaces. |
| Vendors & Contractors | M4 | ✅ Live | 14 | 7 | 18 | Owns `VENDOR`/`CONTRACTOR`. Scorecards, tier status, reliability degradation, contractor matching, certification tracking, partner view over A2A with a pinned allowlist. |
| Agreements | M3 | ✅ Live | 26 | 69 | 17 | Owns `AGREEMENT`. Structured LLM extraction (`agreements/extract.py` via `provider.complete`; per-field confidence, money and legal always route to review), review queue, coverage matrix, kickback reconciliation, compliance audit, index series & price rules, coverage watcher on cron, 3 C12 resource surfaces. |
| Economy | M8 | ✅ Live | 26 | 68 | 26 | Owns `INVOICE`/`POSTING`/`MARGIN`/`BILLING_RUN`. 130-point matching, NGAAP periodisation, WORM balance trigger, 7-effect approval cascade, MRR/ARR/churn, dunning, Monte-Carlo cashflow, KID, EHF, GL records & sync status, close-narrative generation, Finago reader (real HTTP), 5 C12 resource surfaces. |

## Tier 3 — Delivery & Field Ops

| Engine | Module | Build | Tools | Routes | Cores | What it owns / does |
|---|---|---|---|---|---|---|
| Warehouse & Inventory | M11 | ✅ Live | 29 | 73 | 19 | Owns `STOCK_LOCATION`/`GOODS_RECEIPT`. Append-only transactions, reservation algebra, row-locked decrements, partial-GR semantics, RMA+WEEE, dead stock, forecast/restock advisor, stock watcher on cron, 4 C12 resource surfaces. |
| Assets | M9 | ✅ Live | 22 | 40 | 23 | Owns `ASSET`/`TELEMETRY`. 14-state lifecycle, seeding from BOM line, SLA attachment, person assignment, sub-components, QR generation, NetBox sync, health scoring, and real vendor HTTP telemetry adapters for Neat Pulse, Q-SYS Reflect, Disruptive, Ochno, and AIS (`UnimplementedVendorAdapter` for unconfigured vendors), plus C12 `assets` surface. |
| Support | M10 | ✅ Live (PR #2, #11, #13) | 30 | 74 | 22 | Owns `TICKET` + SLA clock. Ticket query/open/resolve/triage, append-only ticket actions & timeline, SLA clocks, customer-health scoring with touchpoints, recall-grounded troubleshooter, work-order dispatch, D365 case sync, 4 C12 resource surfaces. |
| Staff & Resources | M15 | ✅ Live (PR #12) | 22 | 55 | 15 | Owns `RESOURCE`/`ALLOCATION`. Double-booking made impossible at the database (`EXCLUDE USING gist`), allocation planner, material flow, Norwegian *diett* travel rules, redacted field schedule, demand forecast, 3 C12 resource surfaces. |
| Field Tech | M12 | ✅ Live (PR #4) | 19 | 55 | 13 | Owns `WORK_ORDER`. Work orders under dual RLS (namespace + partner scope), ISO9001 checklists, time entries, serial scan (advancing BOM_LINE to `INSTALLED`), checklist completion (advancing BOM_LINE to `TESTED`), photo capture, offline sync, partner view, 3 C12 resource surfaces. |
| Network Ops (Edge) | M18 | ⬜ Planned | — | — | — | **Blocked: its primary spec does not exist.** The README promises `18-network-ops-edge-engine.md`; only `EDGE_MCP_WORKER.md` is real. |
| Remote Access & RMM | M19 | ⬜ Planned | — | — | — | GoTo Resolve / LogMeIn adapter. Chartered; read/monitor ships standalone, write actions gated on C2. |

## Tier 4 — Intelligence & External

| Engine | Module | Build | Tools | Routes | Cores | What it owns / does |
|---|---|---|---|---|---|---|
| HR | M13 | ✅ Live (PR #7) | 25 | 73 | 18 | Owns `EMPLOYEE`/`SKILL`/`CERTIFICATION`. Profiles, skills match, capacity, cert status & expiry emission, absences, onboarding quests, redacted 1:1 log, and a **native sykefravær compliance state machine**. EU-AI-Act constrained: no individual ranking. 4 C12 resource surfaces. |
| Marketing | M14 | ✅ Live (PR #9) | 18 | 52 | 10 | Case-study candidates and grounded drafting, anonymise-by-default, testimonials with consent tiers, AEO/GEO audit, human-gated publishing (`PublishTransport.MANUAL`; no Autonomous tier exists), 3 C12 resource surfaces. |
| Business Insights | M16 | ✅ Live (PR #14 merged) | 9 | 20 | 6 | KPI cockpit, executive morning brief with a provenance graph, **cross-engine risk radar**, Monte-Carlo scenarios, board pack, "ask your business" behind a person barrier and an egress boundary, and C12 `kpi-snapshots` surface. |
| Customer Portal | M17 | ✅ Live (PR #15, #16) | 9 | 12 (own app) | 10 | External customer surface — owns `PORTAL_USER`/`SERVICE_REQUEST`. Four-layer security spine on the existing C3 external-scope primitive, allow-list redaction, a rate-limited app shell (`/api/portal/*`), delivery tracker, expiring document shares, sandboxed advisor. |

## Supporting integrations & shared-core modules

| Module | Build | Tools | Routes | Cores | Role |
|---|---|---|---|---|---|
| `dynamics365` | ✅ Live | 6 | — | — | Sales read-model and Support case source; 3 cron jobs; webhooks. |
| `diagnostics` | ✅ Live | 5 | — | — | Log digestion → `device_health_rollup`. |
| `netbox` | ✅ Live | 1 (+ `netbox_nce` push plugin) | — | — | As-built topology source and circuit-impact evaluation. |
| `geodata` | ✅ Live | 7 | — | 7 | OSM elements, Kartverket N50 land cover & SSR place-name imports/queries, and MET Norway Locationforecast weather lookup. |
| `legal_entities` | ✅ Live | 5 | 16 | 1 | C16 Brønnøysundregistrene (`data.brreg.no`) enrichment and C12 `legal-entities` resource surface. |
| `notifications` | ✅ Live | 8 | 32 | — | C13 user notifications and reminders (`notifications`, `reminders` C12 resource surfaces). |
| `documents` | ✅ Live | 4 | 16 | — | C14 cross-engine document metadata and attachment registry (`documents` C12 resource surface). |
| `sites` | ✅ Live | 5 | 16 | 1 | C17 Kartverket/Geonorge address & coordinate enrichment and C12 `sites` resource surface. |

---

## Surface of Truth — tools, routes and cores per engine

The per-engine table lives in **[`docs/_generated/surface.md`](../_generated/surface.md)**, generated
by AST from `nce/tool_registry.py`, `nce/admin_app.py` and `nce/vertical_modules/`. Regenerate it
rather than hand-editing it. It is the file to trust when this page and the code disagree.

---

## Critical frontend planning notes

> [!WARNING]
> ### Golden Thread lifecycle: 33 of 37 steps pass against live Postgres; 4 v1.6 seams remain open
> All 7 original v1.5 cross-engine seams (Steps 1–28) and 5 v1.6 extension steps (Steps 29–32, 37)
> are **closed and verified end-to-end against live Postgres** in
> [`tests/integration/test_golden_thread.py`](../../tests/integration/test_golden_thread.py).
> Only **4** v1.6 extension seams (`break-h9b`, `break-h9c`, `break-h9d`, `break-h9e` in Steps 33–36)
> remain open as strict `xfail`s — mirrored automatically in
> [`docs/_generated/golden_thread_seams.md`](../_generated/golden_thread_seams.md):
>
> | Open Seam | Step | What happens today |
> |---|---|---|
> | `break-h9b` | Step 33 (`test_step_33_deal_participants`) | The C12 `sales_deal_participants` surface is mounted, but `do_create_deal` accepts no inline participants and `sales_deal_participants.deal_id` FKs to relational `sales_deals(id)` whereas `do_create_deal` writes a `kg_nodes` `DEAL` node. |
> | `break-h9c` | Step 34 (`test_step_34_agreement_parties`) | Relational `agreements.customer_id` carries a hard FK to `sales_customers(id)`, whereas Step 1's `do_create_deal` writes a `kg_nodes` `CUSTOMER` node without bridging to `sales_customers`. |
> | `break-h9d` | Step 35 (`test_step_35_billing_run`) | `do_generate_billing_run` (`nce/vertical_modules/economy/billing_runs.py`) is implemented and read-only C12 surfaces (`billing-runs`, `billing-candidates`) exist, but no writer tool or route invokes `do_generate_billing_run`. |
> | `break-h9e` | Step 36 (`test_step_36_customer_invoice`) | `do_propose_customer_invoice` (`nce/vertical_modules/economy/customer_invoices.py`) is implemented and read-only C12 surface (`customer-invoices`) exists, but no writer tool or route invokes `do_propose_customer_invoice`. |

> [!IMPORTANT]
> ### LLM extraction & enrichment vs. deterministic retrieval-grounded advisors
> **Two vertical modules invoke the structured LLM provider (`provider.complete`)** with confidence
> gating and human-review routing:
> 1. `nce/vertical_modules/agreements/extract.py` — structured contract/SLA extraction with per-field confidence (money and legal fields always route to the review queue).
> 2. `nce/vertical_modules/product/enrich.py` — structured product catalog enrichment (`provider.complete(messages, ProductEnrichmentResult)`, wired in Wave P-1).
>
> By contrast, Support's troubleshooter, Marketing's case-study drafting, HR's coach, and the
> Customer Portal advisor deliberately use **deterministic retrieval-grounded composition** (C9a/C9b)
> rather than free-form LLM generation.

> [!NOTE]
> ### Asset telemetry vendor adapters (`nce/vertical_modules/assets/telemetry.py`)
> Real vendor HTTP telemetry adapters are wired for **Neat Pulse** (`neat_pulse`), **Q-SYS Reflect**
> (`qsys_reflect`), **Disruptive Technologies** (`disruptive`), **Ochno** (`ochno`), and **AIS**
> (`ais`) (Waves F-1..F-5). The 5 unconfigured vendor platforms (`crestron_xio`, `poly_lens`,
> `logitech_sync`, `cisco_control_hub`, `huddly`) route to `UnimplementedVendorAdapter` (Q-38),
> which fails closed rather than returning synthetic `MockTelemetryAdapter` data.

> [!NOTE]
> ### Internal cores without a public surface
> Unreached `do_*` cores across `nce/vertical_modules/` have been reduced from 83 to **19**
> explicitly reasoned entries in [`nce/config_data/internal-cores.json`](../../nce/config_data/internal-cores.json),
> guarded by the shrink-only AST reachability ratchet in
> [`tests/test_surface_parity.py`](../../tests/test_surface_parity.py). Every entry records an
> `owner` and a reviewer-verifiable `reason` explaining why the core is not wired to a tool, route,
> cron job, webhook, subscriber, or A2A skill.

> [!NOTE]
> ### Integration protocols
> - **Authentication:** all `/api/*` administrative endpoints require the HMAC three-header
>   protocol (`X-NCE-Timestamp`, `Authorization: HMAC-SHA256`, `X-NCE-Nonce`), and mTLS in production.
> - **Public endpoints:** `GET /public-api/sales/quotes/{id}` is authenticated by a bearer token
>   signature `HMAC-SHA256(NCE_MASTER_KEY, quote_id)`.
> - **Customer Portal** runs its own rate-limited app shell on `/api/portal/*`, separate from the
>   admin app, under the C3 external-principal scope.
> - **Opt-in:** every vertical engine is behind a per-namespace opt-in guard and refuses until enabled.
