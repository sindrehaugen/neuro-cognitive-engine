> **Status:** shipped · **Verified-against:** `1e402e1` (public `main`) · **Last-audited:** 2026-09-06

# NCE Vertical Engines — Build & Production Status

> **For frontend planning.** **All 17 of 17 numbered engines are merged and running on public
> `main`** (+3 supporting integrations, +2 Operations-axis extensions not yet scheduled). Every
> engine with a registered tool now has a `docs/engines/<engine>-user.md` guide — the last five
> (Support, Customer Portal, Resources, Assets, Marketing) were written 2026-09-06.
> **Repository of record since 2026-09-05: `sindrehaugen/neuro-cognitive-engine` (public, AGPL-3.0).**
> Source of truth for every number on this page: `nce/tool_registry.py`, `nce/admin_app.py`, and
> `nce/vertical_modules/`, read by `scripts/gen_surface_table.py` into
> [`docs/_generated/surface.md`](../_generated/surface.md).

> [!IMPORTANT]
> **This file was five days stale until 2026-09-06.** It read "135 MCP tools" and marked Support,
> Field Tech, HR, Marketing, Resources, Business Insights and Customer Portal ⬜ Planned while six
> of the seven were merged and running. A deep review of the estate inherited that error and
> reported seven engines as unbuilt. If a number here disagrees with `docs/_generated/surface.md`,
> **the generated file is right and this one is stale** — regenerate with:
> ```
> python scripts/gen_surface_table.py --repo . --baseline HEAD --out docs/_generated/surface.md
> ```

## Measured inventory at `1e402e1`

| Instrument | Value |
|---|---|
| Module packages under `nce/vertical_modules/` | **20** (17 engines + `diagnostics`, `dynamics365`, `netbox`) |
| `TOOL_REGISTRY` entries | **222** MCP tools (66 shared + 156 engine) |
| REST routes on the admin app | measured per-engine in [`docs/_generated/surface.md`](../_generated/surface.md) (+12 on the Customer Portal's own app shell) — regenerate for a total, do not hand-add the column |
| `do_*` domain cores | **228** distinct names across the 17 engines |
| SQL migrations | 70 files (+1 optional), `001` → `074` — gaps at `002`, `009`, `059` (never allocated); `010` exists only under `nce/migrations/optional/`. **Corrected 2026-09-06: this row previously said "67 files, 059/066/072 unused" — 066 and 072 are real, in-use migrations (`system_namespace`, `business_insights_engine`); only 002/009/059 were ever actually skipped.** |
| `EXPECTED_TENANT_RLS_TABLES` | **87** |
| Tests | **6,423** `def test_` across 528 files |
| Golden Thread seam burndown | **8 of 28** lifecycle steps still broken (5 distinct seams — `break-degradations` closed by Wave I-5, 2026-09-06) — generated, cannot go stale: [`docs/_generated/golden_thread_seams.md`](../_generated/golden_thread_seams.md) |

**Status legend**

| Badge | Meaning |
|---|---|
| ✅ **Live** | Merged to public `main`, CI-gated, tools and routes registered. |
| 🟡 **In review** | Complete on a branch with an open PR. |
| ⬜ **Planned** | Specced, not built. |

> [!WARNING]
> **✅ Live means "merged and CI-gated", not "exercised against a database."** As of 2026-09-06 the
> six engines that landed on 2026-09-05 (Support, Field Tech, HR, Marketing, Resources, Customer
> Portal) had **never run against a live Postgres** — the deployed containers were still built from
> a 2026-09-04 image carrying 142 tools. Treat the ✅ column as a statement about the tree.

---

## Foundation — Core Cognitive Spine

| Engine | Module | Build | Tools | Routes | Cores | What it provides |
|---|---|---|---|---|---|---|
| Shared Core | M0 | ✅ Live | 66 | 84 | — | The cognitive-graph spine every engine stands on: entity resolution (C1), autonomy governance (C2), external-principal RLS (C3), event bus (C4), source-mode resolver (C5), shared pricing (C6), signing (C7), field redaction (C8), retrieval-grounded generation and the no-person-grain guard (C9a/C9b). |

## Tier 1 — Core Operating Spine

| Engine | Module | Build | Tools | Routes | Cores | What it owns / does |
|---|---|---|---|---|---|---|
| Product | M2 | ✅ Live | 6 | 3 | 10 | Owns `PRODUCT`/`SKU`. Nettailer ingest (real HTTP), ETIM catalog, C1 heavy client with merge queue, `product_match_bom_line`, EOL watcher on cron, governed enrichment. |
| Procurement | M1 | ✅ Live | 6 | 8 | 12 | Owns `PO`. TCO, 5-step supplier ranking, 3-way match, rebate forecasting, frontier advisors. |
| System Design | M6 | ✅ Live | 12 | 10 | 13 | Owns `DESIGN`/`FUNCTIONAL_LOCATION`. Topology authoring with `expected_version`, geometry, validation, SoW, Lucid/SharePoint export (real HTTP), NetBox bridge, design↔quote through the shared BOM store. |
| Project | M7 | ✅ Live | 4 | 7 | 11 | Owns `PROJECT`/`GATE`/`TASK`. G0–G5 phase gates, signed-quote→project conversion, my-day, capacity, scope-creep detection, status reports, governed BOM sync. |

## Tier 2 — Commercial & Financial Spine

| Engine | Module | Build | Tools | Routes | Cores | What it owns / does |
|---|---|---|---|---|---|---|
| Sales | M5 | ✅ Live | 5 | 15 | 31 | Owns `CUSTOMER`/`LEAD`/`QUOTE`/`SIGNED_BASELINE`. Full read model, `d365`/`both`/`nce` source-mode flip with a parity window, public quote endpoint (HMAC token), quote-line tools landing BOM_LINEs, C7 signature request (`sales_request_signature`, Wave S-2a). |
| Vendors & Contractors | M4 | ✅ Live | 10 | 2 | 17 | Owns `VENDOR`/`CONTRACTOR`. Scorecards, tier status, reliability degradation, contractor matching, partner view over A2A with a pinned allowlist. |
| Agreements | M3 | ✅ Live | 1 | 5 | 13 | Owns `AGREEMENT`. **The only real LLM extraction in the suite** (per-field confidence; money and legal always route to review), review queue, coverage matrix, kickback reconciliation, compliance audit, coverage watcher on cron. |
| Economy | M8 | ✅ Live | 9 | 9 | 22 | Owns `INVOICE`/`POSTING`/`MARGIN`. 130-point matching, NGAAP periodisation, WORM balance trigger, 7-effect cascade, MRR/ARR/churn, dunning, Monte-Carlo cashflow, KID, EHF, GL sync status, close-narrative generation, Finago reader (real HTTP). |

## Tier 3 — Delivery & Field Ops

| Engine | Module | Build | Tools | Routes | Cores | What it owns / does |
|---|---|---|---|---|---|---|
| Warehouse & Inventory | M11 | ✅ Live | 14 | 14 | 17 | Owns `STOCK_LOCATION`/`GOODS_RECEIPT`. Append-only transactions, reservation algebra, row-locked decrements, partial-GR semantics, RMA+WEEE, dead stock, forecast/restock advisor, stock watcher on cron. **Every core is surfaced** — the template for the rest. |
| Assets | M9 | ✅ Live | 8 | 8 | 7 | Owns `ASSET`/`TELEMETRY`. 14-state lifecycle, seeding from BOM line, SLA attachment, health scoring — **telemetry is simulated** (`MockTelemetryAdapter`; no real vendor HTTP client is wired for Crestron/Q-SYS/Neat/Huddly/Poly, see `docs/engines/assets-admin.md`). |
| Support | M10 | ✅ Live (PR #2, #11, #13) | 10 | 12 | 15 | Owns `TICKET` + SLA clock. Ticket query/open/resolve/triage, SLA clocks, customer-health scoring with touchpoints, recall-grounded troubleshooter, dispatch, D365 case sync. |
| Staff & Resources | M15 | ✅ Live (PR #12) | 9 | 10 | 14 | Owns `RESOURCE`/`ALLOCATION`. Double-booking made impossible at the database (`EXCLUDE USING gist`), AI allocation planner, material flow, Norwegian *diett* travel rules, redacted field schedule, demand forecast. |
| Field Tech | M12 | ✅ Live (PR #4) | 10 | 12 | 12 | Owns `WORK_ORDER`. Work orders under dual RLS (namespace + partner scope), ISO9001 checklists, time entries, serial scan, photo capture, offline sync, partner view. |
| Network Ops (Edge) | M18 | ⬜ Planned | — | — | — | **Blocked: its primary spec does not exist.** The README promises `18-network-ops-edge-engine.md`; only `EDGE_MCP_WORKER.md` is real. |
| Remote Access & RMM | M19 | ⬜ Planned | — | — | — | GoTo Resolve / LogMeIn adapter. Chartered; read/monitor ships standalone, write actions gated on C2. |

## Tier 4 — Intelligence & External

| Engine | Module | Build | Tools | Routes | Cores | What it owns / does |
|---|---|---|---|---|---|---|
| HR | M13 | ✅ Live (PR #7) | 8 | 12 | 16 | Owns `EMPLOYEE`/`SKILL`/`CERTIFICATION`. Profiles, skills match, capacity, cert status, absences, onboarding quests, redacted 1:1 log, and a **native sykefravær compliance state machine**. EU-AI-Act constrained: no individual ranking. |
| Marketing | M14 | ✅ Live (PR #9) | 8 | 9 | 10 | Case-study candidates and grounded drafting, anonymise-by-default, testimonials with consent tiers, AEO/GEO audit, human-gated publishing (`PublishTransport.MANUAL`; no Autonomous tier exists). |
| Business Insights | M16 | ✅ Live (PR #14 merged) | 6 | 6 | 6 | KPI cockpit, executive morning brief with a provenance graph, **cross-engine risk radar**, Monte-Carlo scenarios, board pack, and "ask your business" behind a person barrier and an egress boundary. |
| Customer Portal | M17 | ✅ Live (PR #15, #16) | 9 | 12 (own app) | 10 | External customer surface — owns `PORTAL_USER`/`SERVICE_REQUEST`. Four-layer security spine on the existing C3 external-scope primitive, allow-list redaction, a rate-limited app shell, delivery tracker, expiring document shares, sandboxed advisor. |

## Supporting integrations

| Module | Build | Tools | Role |
|---|---|---|---|
| dynamics365 | ✅ Live | 6 | Sales read-model and Support case source; 3 cron jobs; webhooks. |
| diagnostics | ✅ Live | 5 | Log digestion → `device_health_rollup`. |
| netbox | ✅ Live | 1 (+ `netbox_nce` push plugin) | As-built topology source. |

---

## Surface of Truth — tools, routes and cores per engine

The per-engine table lives in **[`docs/_generated/surface.md`](../_generated/surface.md)**, generated
by AST from `nce/tool_registry.py`, `nce/admin_app.py` and `nce/vertical_modules/`. Regenerate it
rather than hand-editing it. It is the file to trust when this page and the code disagree.

---

## Critical frontend planning notes

> [!WARNING]
> ### Eight of nine end-to-end flows stop at a seam
> Every engine exists; the joints between some of them do not. A frontend built against a single
> engine's surface is safe. A frontend that assumes a **chain** of engines is not, until the v1.5
> programme closes these. Measured at `dc751f9`:
>
> | Seam | What actually happens today |
> |---|---|
> | Signed quote → frozen baseline → project | The signing orchestrator has no tool, route or webhook, so `project_convert_signed_quote` degrades to `sales_baseline_unavailable` on every real call. |
> | BOM_LINE status ladder | Six stages are projected (Planned → Ordered → Delivered → Installed → Tested → Ready); only `DELIVERED` has a writer. No PO is ever generated, and Field Tech never writes `INSTALLED`/`TESTED`. |
> | Kickback governance | `agreements/coverage.py` raises `NotImplementedError` for the Economy GL read; no `economy_get_gl_records` tool exists. Coverage and kickback reconciliation degrade on every call, including the nightly watcher. |
> | Project outcome → design recall / case study | The outcome, recall and case-study-edge cores have zero callers, so design proposals stay similarity-only and `marketing_find_case_study_candidates` is correct and permanently empty. |
> | Certification expiry → allocation invalidation | Resources subscribes to `CERTIFICATION.{CREATED,UPDATED,EXPIRED}`; HR emits no event and Vendors emits node type `CERT`. |
> | Support dispatch → work order | Support writes a `dispatched_as` edge to a derived work-order id that nothing creates. |
> | Portal request → Support ticket | The hand-off probes `hasattr(engine, "support")`, an attribute nothing sets in production. |
>
> All of these pass their unit tests, because each is tested from one side.

> [!IMPORTANT]
> ### `product_enrich` returns synthetic values
> `nce/vertical_modules/product/enrich.py` builds strings of the form
> `f"{field}_enriched_for_{mfr_part_no}"` with a hard-coded confidence. It is a working pipeline
> with a placeholder generator, not enrichment. Do not build a UI that presents its output as fact.
>
> **Across all 19 module packages, exactly one file calls the LLM provider**:
> `agreements/extract.py`. Support's troubleshooter, Marketing's drafting, HR's coach and the
> Portal advisor are recall-and-template compositions — deliberate and defensible, but the word
> "AI" in their specs means retrieval and composition, not generation.

> [!NOTE]
> ### Cores without a surface
> **83 of 241 `do_*` cores are reachable from no tool, route, subscriber, cron job, webhook or A2A
> skill.** The largest pockets: Economy (17 of 22, including the approval cascade), Sales (19 of
> 43, including every native write path), Agreements (12 of 13). If a capability is described in
> an engine spec but absent from `docs/_generated/surface.md`, it exists as a core and has no door.

> [!NOTE]
> ### Integration protocols
> - **Authentication:** all `/api/*` administrative endpoints require the HMAC three-header
>   protocol (`X-NCE-Timestamp`, `Authorization: HMAC-SHA256`, `X-NCE-Nonce`), and mTLS in production.
> - **Public endpoints:** `GET /public-api/sales/quotes/{id}` is authenticated by a bearer token
>   signature `HMAC-SHA256(NCE_MASTER_KEY, quote_id)`.
> - **Customer Portal** runs its own rate-limited app shell on `/api/portal/*`, separate from the
>   admin app, under the C3 external-principal scope.
> - **Opt-in:** every vertical engine is behind a per-namespace opt-in guard and refuses until enabled.
