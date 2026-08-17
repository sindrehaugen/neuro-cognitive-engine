> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 16 — Business Insights Engine  (nce/vertical_modules/business_insights)

**Status:** spec (Tier 4 — Platform/Executive surface, builds LAST) · **Owner:** NCE core (Sindre)
**Pattern companions:** `docs/VERTICAL_MODULE_PATTERN.md`, `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§4 graph catalogue, §5 A2A, §7 spec format, §9 contracts, §10 moat)

## Mission
Analytics + decision support for **management and the board** — the promotion of the **"#19 Morning-brief aggregate"** that every other engine's spec says it "feeds" into a first-class engine. This **is** Andreas module **19 Management/Executive**: the *"12-minutters morgen"* — ONE intelligent surface that opens to an AI **morning brief** (1 risk + 1 opportunity + financial pulse + capacity), a cross-engine **RISK RADAR** that catches collisions no single engine can see (*"sales celebrates, operations cries"* — pipeline up while delivery capacity is already redlined), **scenario/what-if** modelling, an **auto board-pack**, and KPI dashboards. The deep-AI angle and **the entire architectural point: this is a PURE CONSUMER / A2A-composition engine that "leverages heavily on MCP."** It owns almost no operational nodes; it **composes every other engine's read/MCP tools via A2A** and synthesises across them. Two MCP angles: **(a)** internally it fans out across all engines' MCP tools and reasons over the one cognitive graph; **(b)** it **exposes a read-only, role-scoped MCP surface so the board/management can "ask their business" through their own AI** — the internal analog of Marketing's AEO/MCP-queryable content and Product's MCP-native edge. The **cross-domain reasoning IS the moat** (§10): only possible because every engine writes to ONE cognitive graph — no incumbent running separate apps/DBs per domain can correlate sales pipeline against delivery capacity against cashflow in one query.

## Inspiration & triage
- **Andreas source (concept, not code):** module map `04-virksomhets-modulkart.md` §19 Management/Executive — *12-minutters morgen*, AI morning brief, cross-module risk radar, scenario/what-if, auto board-pack, KPI cockpit. Listed as the **aggregate consumer** of every other module; it is the surface the peer's "feeds the #19 Morning-brief" lines all point at. There is **no Portal sidecar to lift** and **no Lysning page** today.
- **Adjacent signal sources (consumed via A2A, never lifted/owned):** Economy(8) financial pulse/margin/cashflow, Project(7) capacity/slip/portfolio, Support(10) churn/SLA/health, Sales(5) pipeline/win-loss, Procurement(1) savings/leakage, Inventory(11) dead-stock/shortfall, Resources(15) capacity, Agreements(3) expiry, Assets(9) health. It **reads** these; it owns none of them.
- **Lysning page served:** none yet — a future "Executive / Cockpit" admin surface (the morning-brief dashboard + board-pack export) consumes the no-model REST routes. Out of scope for the first build beyond the REST contract.
- **Crown-jewel framing:** this is the engine whose value is the §10 moat made tangible — the cross-domain loop incumbents can't run.

## Classification
**pure consumer / A2A-composition + generative synthesis.** No external system to sync from and no inbound transport — its input *is* the live cognitive graph plus every other engine's MCP/read tools fanned out over A2A; its output is briefings, risk findings, scenarios, board-packs, and cached KPI snapshots. There is therefore **no `client.py` / `auth.py` / `webhooks.py` / `ingestion.py`**; the module is a fan-out **A2A client** + a cognitive-synthesis layer + a thin **read-only MCP server surface**. Auth model: **internal only, and the most sensitive internal surface in the suite** — exec/board-scoped (see Security). Build-vs-buy (§2.10): this **replaces PowerBI/Tableau/board-reporting tools** — copy-and-improve, but natively **graph- + MCP-native**, which warehouse-backed BI tools structurally cannot be (they query a stale warehouse; we reason over the live cognitive graph and compose live engine tools at query time).

## Graph contribution
Node `entity_type` prefix: `BUSINESS_INSIGHTS_*`. It **reads** essentially every spine node (`PROJECT`, `QUOTE`, `INVOICE`, `MARGIN`, `TICKET`, `RESOURCE`, `ALLOCATION`, `AGREEMENT`, `ASSET`, `PO`, …) and **writes only its own artifact nodes** — minimal, cached aggregates + history for audit/trend.
- **Nodes (owns, minimal):** `BUSINESS_INSIGHT` (a surfaced cross-engine finding — e.g. a risk-radar collision), `BRIEFING` (a generated morning-brief, timestamped), `SCENARIO` (a what-if model + its assumptions + result), `KPI_SNAPSHOT` (a cached point-in-time KPI roll-up for trend).
- **Edges (the §4 contract, our slice — all advisory/read, never operational):**
  - `BUSINESS_INSIGHT -[derived_from]-> {PROJECT|INVOICE|TICKET|QUOTE|…}` (provenance: every finding resolves to the source nodes it correlated)
  - `BRIEFING -[surfaces]-> BUSINESS_INSIGHT` (a brief bundles the day's risk + opportunity findings)
  - `SCENARIO -[projects]-> {PIPELINE|CAPACITY|CASHFLOW}` slices (the composed inputs it modelled)
  - `KPI_SNAPSHOT -[rolls_up]-> {ENGINE}` (trend lineage)
- **memories/ledger:** generated briefings/scenarios/board-packs → `memories` (embedding + `content_fts`) so the engine answers *"have we seen quarters like this"* and narrates trends. Every generation + every surfaced risk/opportunity + every board-pack export → `v3_cognitive_ledger` (auditable: who saw what, when, derived from which nodes). Tag every derived row with `business_insights_source_id` for hard-retirement (D365 retirement pattern). **It writes ONLY `BUSINESS_INSIGHTS_*` nodes — it never mutates a single spine node it reads** (read/advisory only, per §9.1 write-authority rule).

## Core functions
Synthesis `do_<action>(engine, params) -> dict`. Every core **fans out over A2A** to the owning engines' read tools, then reasons over the composed result + the graph. **No function writes operational state in any other engine** — outputs are findings, briefings, scenarios, snapshots staged for human decision.
- `do_morning_brief(engine, params) -> dict` — namespace/date → the *12-minutters* brief: **1 top risk + 1 top opportunity + financial pulse + capacity headline**, each with a one-line rationale and provenance links. Composes Economy + Project + Support + Sales + Resources read tools (the §5 "Morning brief" flow, now owned here). Writes a `BRIEFING` node. Advisor.
- `do_risk_radar(engine, params) -> dict` — namespace → **cross-engine collision detection**: correlates slices no single engine sees (pipeline-up × capacity-redlined; margin-erosion × dead-stock; SLA-breach-trend × renewal-due) and emits ranked `BUSINESS_INSIGHT` findings. **This is the moat function.** Watcher.
- `do_run_scenario(engine, params) -> dict` — `{assumptions}` → what-if model composing **Sales pipeline × Resources capacity × Economy cashflow** (e.g. "win these 3 deals → can we staff them, what does cashflow do"); supports a **Monte-Carlo** mode over Economy's cashflow distribution. Writes a `SCENARIO` node. Advisor; read-only over live tools.
- `do_generate_board_pack(engine, params) -> dict` — namespace/period → a structured board narrative (KPI trends + the quarter's risks/opportunities + scenario summaries), assembled from the graph and prior snapshots; staged as a draft for human review. Advisor (drafts; a human presents).
- `do_kpi_dashboard(engine, params) -> dict` — namespace → the live KPI roll-up (revenue/margin/MRR/ARR, pipeline, utilisation, churn, savings, on-time delivery), each KPI a composition of the owning engine's read tool; snapshots to `KPI_SNAPSHOT` for trend. Advisor.
- `do_ask_business(engine, params) -> dict` — `{question, principal}` → **NL "ask your business"**: routes a natural-language question over the whole graph + the relevant engines' read tools and answers with cited provenance. The synthesis core behind the role-scoped MCP surface. Advisor.

## MCP tools
Registered in `nce/tool_registry.py` via `_h(...)` late-binding. AI-role tag per roadmap §2 taxonomy. **All are `admin_only` (exec/board-scoped) and `mutation=False`** — this engine only writes its own cached artifact/history nodes; it never mutates spine state.

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `business_insights_morning_brief` | ✔ | ✔ | ✘ | Advisor |
| `business_insights_risk_radar` | ✔ | ✔ | ✘ | Watcher |
| `business_insights_run_scenario` | ✘ | ✔ | ✘ | Advisor |
| `business_insights_generate_board_pack` | ✘ | ✔ | ✘ | Advisor |
| `business_insights_kpi_dashboard` | ✔ | ✔ | ✘ | Advisor |
| `business_insights_ask_business` | ✘ | ✔ | ✘ | Advisor |

**No tool in this engine is Actor or Autonomous.** It informs decisions; **humans act in the owning engines** (Sales adjusts a quote, Project re-plans, Procurement orders). Like Marketing, there is no autonomy tier — but for a *different* reason: not consent/FTC, but that this engine **owns no operational state to write**, by design. The "ask your business" tool is the read-only role-scoped MCP surface board/management point their own AI at.

## REST routes
No-model path for the future Executive/Cockpit admin surface, the BFF, and scheduled board-pack export. Mounted via `build_app(extra_routes=...)`; HMAC/mTLS-authed **and exec/board-role-gated** in `nce/admin_handlers/business_insights.py`:
- `api_business_insights_morning_brief` (GET) — the daily 12-minute surface.
- `api_business_insights_risk_radar` (GET) — the cross-engine collision feed.
- `api_business_insights_run_scenario` (POST) — what-if / Monte-Carlo model.
- `api_business_insights_board_pack` (GET/POST) — generate + fetch the board narrative.
- `api_business_insights_kpi_dashboard` (GET) — live KPI cockpit + trend.
- `api_business_insights_ask` (POST) — NL "ask your business" (role-scoped).

## AI features
- **Watcher:** the **cross-engine RISK RADAR** — threshold breaches and, crucially, **collisions across engines** (the *"sales celebrates, operations cries"* class) that no owning engine can see alone, because each only holds its slice; this engine holds the join. Emits ranked `BUSINESS_INSIGHT` findings with provenance.
- **Advisor:** the morning brief (1 risk + 1 opportunity + financial pulse + capacity); **scenario what-if** composing Sales pipeline × Resources capacity × Economy cashflow (+ Monte-Carlo); **board-pack narrative**; the NL "ask your business" answer surface over the whole graph.
- **Actor / Autonomous:** **none.** Stated explicitly — it never writes operational state. Decisions are taken by humans in the owning engines; this engine only surfaces and explains.
- **Cognitive recall:** *"quarters like this"* — prior briefings/scenarios from `memories` to contextualise today's findings; trend narratives ("margin has compressed three quarters running, driven by…") read from `KPI_SNAPSHOT` history + the ledger, so a board member can ask *why* a number moved and get an auditable answer.
- **Enrichment triggers (event-scoped, never a background sweep):** the morning brief and KPI snapshots run on a **scheduled cadence** (daily/period cron) and **on demand**; risk-radar re-evaluates when an upstream engine signals a material change (reactive graph-event, §9.6) — not a continuous all-graph re-scan. Scenarios + ask-business are purely on-demand.

## A2A flows
- **Consumes (the aggregate consumer — inbound composition only):** fans out to **Economy(8)** (financial pulse/margin/cashflow/Monte-Carlo inputs), **Project(7)** (capacity/slip/portfolio), **Support(10)** (churn/SLA/health), **Sales(5)** (pipeline/win-loss), **Procurement(1)** (savings/leakage), **Inventory(11)** (dead-stock/shortfall), **Resources(15)** (capacity), **Agreements(3)** (expiry), **Assets(9)** (health) — composing each engine's existing read tools into one synthesised view. **This engine IS the consumer the other specs' "feeds the #19 Morning-brief" lines point to** — those slices are consolidated here, not re-implemented.
- **Exposes (the second MCP angle):** a **read-only, role-scoped MCP surface** so board/management query the business through their own AI ("ask your business") — internal analog of Marketing's AEO/MCP-queryable channel and Product's MCP-native edge.
- **Feeds nothing downstream operationally.** It is a graph **leaf** (like Marketing): its outputs are findings/briefings/scenarios for humans, never inputs other engines act on. Any action lives in the owning engine.

## Config keys
`NCE_BUSINESS_INSIGHTS_*` in `nce/config.py`: `NCE_BUSINESS_INSIGHTS_ENABLED`, `NCE_BUSINESS_INSIGHTS_BRIEF_CRON` (morning-brief cadence), `NCE_BUSINESS_INSIGHTS_KPI_SNAPSHOT_INTERVAL_MINUTES`, `NCE_BUSINESS_INSIGHTS_SCENARIO_MONTE_CARLO_ITERATIONS` (default), `NCE_BUSINESS_INSIGHTS_EXEC_ROLE` / `NCE_BUSINESS_INSIGHTS_BOARD_ROLE` (the principal roles permitted), `NCE_BUSINESS_INSIGHTS_BOARD_SCOPE_READONLY` (default **true** — board surface is aggregate-only, never drill-to-individual). Namespaces opt in via `metadata.business_insights.enabled = true`.
**Config-as-IP JSON (namespace-scoped, the business IP — NOT code):**
- `business-insights-kpi-definitions.json` — which KPIs roll up, their formulas (which owning-engine tool feeds each), targets/thresholds. Each tenant defines its own cockpit.
- `business-insights-risk-rules.json` — the cross-engine collision rules (the "sales-up × capacity-redlined" correlations + materiality thresholds) the risk radar evaluates. The moat, as tunable config.
- `business-insights-board-pack-template.json` — board-narrative structure the generator fills from the graph.

## Tables/migrations
**Graph-first** (`BUSINESS_INSIGHT`/`BRIEFING`/`SCENARIO`/`KPI_SNAPSHOT` live as `kg_nodes`/`kg_edges`; the generation + access audit trail lives in `v3_cognitive_ledger`). One own table where time-series KPI history needs first-class, fast keyed/range storage beyond the graph — `ENABLE` + `FORCE ROW LEVEL SECURITY` + `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`, mirrored into `schema.sql` + a numbered migration:
- `business_insights_kpi_snapshots` — `id, namespace_id, kpi_key, value numeric, period, captured_at, source_engine, business_insights_source_id, raw jsonb` — the trend store behind `do_kpi_dashboard` and the "why did this move" recall. (Briefings/scenarios stay graph + ledger.)

## Security & sensitivity (call-out — the most sensitive INTERNAL surface in the suite)
This engine consolidates **financials, margins, pipeline, and people data** into one place — so its access model is the sharpest internal boundary:
- **Exec/board-scoped only.** Tools are `admin_only` and additionally **role-gated** (`NCE_BUSINESS_INSIGHTS_EXEC_ROLE` / `_BOARD_ROLE`) via the HMAC admin app; the **board may get a separate read-only scoped surface** (`BOARD_SCOPE_READONLY=true`). **NOT customer-facing**, ever.
- **Respect HR's no-ranking / EU-AI-Act line (§roadmap, `13a`).** The board sees **AGGREGATES, never individual person-rankings** — capacity/utilisation roll-ups by team/role, never a "rank these employees" view. Person-level data is summarised before it reaches this surface; the engine refuses to surface individual rankings even if the underlying graph holds the data.
- **Read/advisory only.** It writes nothing to other engines' nodes (§9.1) and takes no autonomous action — so its blast radius is *disclosure*, not mutation. The defence is therefore principally **access scoping + the ledger access-audit**, not an autonomy gate.
- **Provenance on every claim.** Every brief/risk/KPI line resolves to the graph nodes it was derived from (`derived_from` edges + ledger), so a board figure is always traceable to source — no black-box number.

## Dependencies
- **Upstream engines (data producers — quality depends on ALL of them):** Economy(8), Project(7), Support(10), Sales(5), Procurement(1), Inventory(11), Resources(15), Agreements(3), Assets(9). This is why Business Insights is **Tier 4 / builds LAST** (roadmap §6) — it is a pure consumer and has nothing to synthesise until the others produce structured data. Its quality is **gated on upstream data quality**: specifically Project/Support **structured-outcome-attribution** (§9.3 — a risk-radar finding is only as good as the attribution behind it) and Economy **divergence-clean books** (§9.2 — financial pulse off un-reconciled books is misleading).
- **Grace-degradation (mandatory):** the engine **shows only the slices whose engines are live** — a missing engine collapses its KPI/risk slice with an explicit "not available yet" rather than a wrong/empty number. The morning brief degrades gracefully from full cross-engine to whatever subset exists. (Same posture as Field Tech/Inventory grace-degradation in §3.)
- **Shared-core prereqs (§9.6):** the **reactive graph-event mechanism** (so risk-radar re-evaluates on material upstream change rather than polling everything) and the **role-scope gating** in the admin app. No partner-scope RLS (this surface is internal-exec only, never partner-facing).
- **Downstream:** none — graph leaf; nothing consumes its output operationally.
- **External:** none (no `client.py`). It replaces external BI tools (§2.10) rather than integrating one.

## Honest assessment
- **Pure consumer, builds last.** It produces no net-new operational data; it is the synthesis layer over everything else. Until the upstream engines exist and produce structured data, it has little to say — hence Tier 4.
- **Only as good as the upstream structured data.** Garbage attribution in Project/Support or un-reconciled Economy books → misleading briefs. The engine surfaces provenance precisely so a degraded input is visible, not hidden.
- **The value is the cross-engine collision-detection no single engine sees** (the risk radar) and the live-graph + MCP-native posture that warehouse-backed BI tools structurally can't match — that, not yet-another-dashboard, is why this engine exists (§10 moat).

## Review round-2 hardening (2026-06-17 — these govern the build)
1. **The no-ranking red line meets an OPEN NL surface — enforce it STRUCTURALLY, not by LLM instruction.** `do_ask_business` lets the board point their own AI at the graph in natural language — and an open NL surface is trivially circumvented by phrasing (*"average resolution time by technician"*, *"which team has the most tickets"* = de-facto people-ranking). Telling the LLM to refuse is as weak as Marketing's free-generation claim-gate (§9.3). **Enforce at the data-access layer: the query path physically cannot return person-grain rows for comparison/ranking — period — regardless of how the question is phrased** (HR's EU-AI-Act/no-ranking red line, enforced where it's hardest). The board gets aggregates by team/period/engine; person-grain comparison is not a returnable shape.
2. **Risk-radar findings carry a confidence/coverage indicator — don't present a confident collision built on half-stale slices.** As the capstone, the radar is *only as good as upstream attribution* (it admits this). A wrong *"operations is redlined"* shown to the board — built on garbage attribution or un-reconciled books — erodes trust fast. Extend the **expose-input-coverage** discipline (Vendors/Assets/Support) to board findings: each carries **"based on N engines, M fully reconciled / K with structured attribution."** A finding leaning on a stale or attribution-poor slice is flagged, not asserted.
3. **"Board's own AI" is a third-party-AI EGRESS boundary — give it customer-portal-grade rigor.** The read-only MCP surface (b) means an **external LLM client** (ChatGPT / Claude desktop / whatever the board uses) connects to NCE — so the org's **consolidated financials flow to whatever AI vendor the board's tool uses**, and the surface is effectively a **client-controlled-agent API**. It needs: authenticated + rate-limited + **fully audited (who-asked-what, every query to the ledger)**, role-scoped to the board principal, and an **explicit, signed-off acceptance that financials leave NCE's control to the board's AI vendor** (a data-egress decision, not a default-on feature). Same egress-boundary rigor the Customer Portal applies to customers.

## Build phases
- **B1 — KPI cockpit + snapshots:** `do_kpi_dashboard` composing the live engines that exist, `business_insights_kpi_snapshots` table (RLS) + `KPI_SNAPSHOT` graph upserts (`business_insights_source_id`). Wire `business-insights-kpi-definitions.json`. MCP tool + REST route. Grace-degrade for absent engines from day one.
- **B2 — Morning brief + provenance:** `do_morning_brief` (Economy + Project + Support + Sales + Resources A2A composition → 1 risk + 1 opportunity + pulse + capacity), `BRIEFING`/`BUSINESS_INSIGHT` nodes with `derived_from` provenance edges, scheduled cron cadence. Exec-role gating + ledger access-audit.
- **B3 — Risk radar (the moat):** `do_risk_radar` cross-engine collision detection over `business-insights-risk-rules.json`; reactive re-evaluation on material upstream change (§9.6). Ranked findings + provenance.
- **B4 — Scenario / what-if:** `do_run_scenario` composing Sales × Resources × Economy (+ Monte-Carlo over cashflow), `SCENARIO` nodes. Board-pack generator `do_generate_board_pack` + `business-insights-board-pack-template.json` (drafts; human presents).
- **B5 — Ask-your-business + recall:** `do_ask_business` over the role-scoped read-only MCP surface; `memories` recall ("quarters like this") + trend narratives from snapshot history; tune risk-rule materiality from the ledger. The promoted, consolidated #19 Executive surface, complete.
