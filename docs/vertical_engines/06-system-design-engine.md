> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 06 — System Design Engine  (nce/vertical_modules/system_design)

<!-- BLOCKED ON OQ-2 / OQ-4: SPEC PROPOSAL VOICE. This document is an architectural design specification. At baseline 7304330, System Design ships 2 registered MCP tools (system_design_ping, system_design_publish_design_docs) and 1 REST route (/api/system-design/publish-design-docs). Interactive CAD/topology/BOM generation functions remain unbuilt/unwired on main. Refer to docs/engines/system-design-user.md and docs/engines/system-design-admin.md for shipped reality. Verified-against: 7304330 -->


> **Status: DISCUSSION doc** — Sindre flagged this one "lets discuss here". This lays out what it is, the scope options, a recommendation, and the open questions. The other engines have settled shapes; this one needs a decision before it's specced like the rest.

## Why this engine is special

In the AV-integrator value chain it sits exactly on the **Revenue↔Delivery bridge**: a customer wants AV in some rooms → *someone designs the system* (which products, how they connect, against which standards) → that design becomes the BOM that Sales quotes and Delivery builds. the reference implementation calls this the single biggest bottleneck: *"Product/Design-avhengighet på komplekse tilbud"* — complex quotes stall waiting for a human designer. His answer is the **AI Solution Agent** that learns from 450+ historical projects and auto-generates ~80% of a standard BOM, with Product validating the remaining 20%.

It is also where NCE's cognitive layer pays off hardest: *"design this room like the 12 similar rooms we've delivered"* is an embedding-recall query, not a rules engine. This is the most **AI-native** of the 14 engines.

## What the Portal/the reference implementation already gesture at

- **Frontend surfaces** (Lysning): `Motebrief.jsx` (meeting/requirements brief), `Moterom.jsx` (meeting room), `Romtegning.jsx` (room drawing), `Skjermnettverk.jsx` (screen network), `Komponenter.jsx`, `TilbudDetalj.jsx` (the quote it feeds).
- **the reference implementation IP**: the reference implementation (the **SoW generator** — *"den reneste handoff-perlen"*, 0-DB pure function, per-room versioned deliverables) · the room-centric BOM model · the AI Solution Agent concept (module #03 Product/Design, learns 450+ projects, AVIXA standards, failure-patterns) · phase-gate *inputs* (the design must satisfy G-criteria).
- **Honest reality check** (from `02-kode-virkelighet`): the SoW generator is real and liftable; the "AI analyzes the offer before handover" is a 🟡 facade pointing at the wrong input (Dynamics handover metadata, not the quote-derived BOM). The AI Solution Agent itself is 🔵 — *structured thinking, not built*. So we are building the engine, not lifting it.

## The scope question (three options)

### Option A — **Design-to-BOM + SoW** (lean, ~Tier-1 sized)
The engine takes a structured **requirements brief** (rooms + use-cases + constraints) and produces a **versioned BOM** + **SoW document** per room. The AI Solution Agent recalls similar past projects from `memories`/graph, proposes line items by pulling specs/pricing from the **Product engine**, and flags the ~20% needing human validation. Signal-flow is *implicit* (the BOM lists devices, not how they connect).
- **Graph:** `DESIGN`, `ROOM`, `DESIGN_LINE` → edges to `PRODUCT`; `DESIGN -[becomes]-> QUOTE`.
- **Pros:** fastest path to the real bottleneck (auto-BOM), reuses Product + SoW directly, pure-function core. **Cons:** doesn't catch design *errors* (resolution/impedance/port-count mismatches) — humans still own correctness.

### Option B — **Room & signal-flow graph** (deep, CAD-adjacent)
Everything in A, **plus** model the design as a real graph: `DEVICE` nodes with typed `PORT`s, `SIGNAL_CHAIN` edges (source → matrix/switch → display, audio DSP chains), rack layouts, cable schedules. Validate against **AVIXA standards** and device port/format specs → the engine *catches* design errors (a 4K source into a 1080p switch, too few inputs, missing DSP). Bridges to **NetBox** (`netbox` vertical already exists) for rack/cabling/IP and to **Assets** for the as-built twin.
- **Graph:** A + `DEVICE`, `PORT`, `SIGNAL_CHAIN`, `RACK`, `CABLE`; `DESIGN -[validated_against]-> STANDARD`.
- **Pros:** this is genuinely differentiating — AI that *designs correctly*, not just *picks parts*; feeds NetBox/Assets a real topology. **Cons:** much bigger; needs a device-capability model (port types, formats) that has to be sourced/maintained; closer to CAD than to a quote tool.

### Option C — **AI Solution Agent only** (thinnest)
Just the recommender: requirements + history → proposed product list + rationale. Humans draw and structure everything else. Effectively a smart tool on top of the Product engine.
- **Pros:** trivial. **Cons:** doesn't own the design artifact, doesn't produce the SoW/BOM, leaves the bottleneck only half-solved.

## Recommendation

**Build Option A now as the Tier-1 engine, architected so Option B is an additive layer — not a rewrite.** Concretely:

1. **Phase 1 (Tier-1):** `system_design` vertical = requirements-brief intake → AI Solution Agent (recall similar `DESIGN`s from the graph, propose `DESIGN_LINE`s via Product) → versioned BOM → SoW generator (lift the reference implementation's pure function near-1:1). Dual-surface: `do_propose_design`, `do_generate_sow`, `do_design_to_quote`. **This kills the stated bottleneck.**
2. **Phase 2 (later tier):** add the `DEVICE`/`PORT`/`SIGNAL_CHAIN` layer + AVIXA validation as new node/edge types on the *same* `DESIGN` node — so a design gains a verified topology without changing the Phase-1 contract. This is where the NetBox bridge and the as-built Asset twin connect.

Rationale: the room-centric design artifact is the same in both; Option B only *enriches* it. Starting at A gets the auto-BOM value into the pipeline fast (the reference implementation's biggest pain), and the cognitive-recall core (A) is the hard/valuable part regardless. The device-capability model that B needs is its own sourcing project (manufacturer specs — overlaps with Product engine's manufacturer-API ambition) and shouldn't block the BOM automation.

## Deep-AI core (the reason it's NCE-native)

- **Recall, not rules:** "propose a design for a 12-person Teams room" → embedding query over past `DESIGN`/`PROJECT` nodes + their *outcomes* (did it get change-orders? service tickets? margin erosion?). The cognitive ledger lets the agent prefer designs that *delivered well*, not just designs that were *quoted*. This closes the reference implementation's "service→design silence" gap structurally.
- **On-demand, scoped enrichment** (the user's global rule): when a `DESIGN_LINE` references a product with missing specs, the engine asks **Product** to enrich *that product only* — never a bulk sweep.
- **A2A:** System Design is the hub of the **Quote→Design→Procure** flow — Sales asks it for a design, it asks Product for specs and Procurement for live TCO/availability before finalizing the BOM.

## Open questions for Sindre

1. **A, B, or A-then-B?** (recommendation: A-then-B.)
2. **Requirements intake** — is `Motebrief.jsx` the structured input (meeting brief → design), or do we define a new requirements schema?
3. **Device-capability model** — if/when Option B: source port/format specs from the Product engine's manufacturer APIs (Cisco/Neat/Poly/Shure/Biamp/Crestron/QSC) or a third party (Icecat/XTEN-AV were considered)?
4. **AVIXA standards** — encode as config-as-IP validation rules (like `procurement-weights.json`), or out of scope for now?
5. **Relationship to Project** — does the design own the room→phase-gate criteria, or just hand the frozen BOM to Project?

---
# Phase-1 Spec (Option A — Design→BOM + SoW)

> **Decision: A-then-B, bidirectional (settled 2026-06-17).** Phase 1 ships the lean **Design↔BOM + SoW** engine — **flexible in both directions** (design-first *or* quote-first) on the **functional-location principle** — architected so the deep signal-flow/AVIXA layer (Phase 2 = Option B) **enriches the same `DESIGN` node** later, never a rewrite. This section specs Phase 1 in the canonical per-engine format (roadmap §7). Engine #6, Tier 1.

## Corrections & hardening (review 2026-06-17 — these override the original draft below)
The bidirectional-via-functional-location design is right and the outcome-weighted recall is the real insight, but the original draft was too optimistic on integrations and AI trust. Binding corrections:

1. **The `netbox` vertical does NOT provide the functional-location tree.** Verified: it's circuits/contacts/discovery/mtbf/graphql_activation — **no sites/locations/racks sync**. So "the bridge is thin / just sync from NetBox" is **false** — **functional-location sync must be built**, and it re-sizes build-phase step 2.
2. **Location direction is inverted.** NetBox is an **as-built** source — it's populated *after* install; design happens for rooms that **don't exist yet**. So the engine **authors tentative `FUNCTIONAL_LOCATION` nodes (design intent)**, and the NetBox/Assets bridge **later reconciles/promotes them to as-built** via a `promoted_to_asbuilt` edge. Default flow = **design authors intent → NetBox confirms reality**, *not* "pull the tree from NetBox at design time."
3. **Bidirectional = the same two-master problem as Sales, worse.** `DESIGN ⇄ QUOTE` keyed by functional location needs an **explicit ownership/lock model per line** (roadmap §9.1): **System Design owns a `BOM_LINE` until the Sales signed-baseline freeze; after freeze it loses write authority** (design proposals are suggestions until a quote pulls them; once a quote line is frozen toward signature, design cannot rewrite it). The freeze is the hand-off point.
4. **Outcome-weighted recall has an upstream dependency + a cold-start.** Project(7)/Support(10) may be empty at launch, and the reference implementation's 450 historical projects live in his Prisma DB, not our ledger. So: **degrade gracefully — recall by *design similarity* first (works day one); outcome-weighting switches on as Project/Support backfill the ledger.** Treat the historical-outcome backfill as a real ingestion project, and list **Project/Support as upstream dependencies of recall *quality*** (not just downstream consumers).
5. **No baked-in "80% auto / 20% validate"** — that's the reference implementation's marketing number, and auto-accepting BOM lines early *causes* the change-orders/margin-erosion we're avoiding. **Ship propose-only, human confirms 100%** (roadmap §9.3); the auto-accept confidence threshold rises **only** after `do_validate_design` accept/override data measures the agent's precision.
6. **Integrations are NOT on the critical path.** The value core (recall → propose BOM → SoW → design↔quote) needs **zero external systems** — just the graph + Product via A2A. **The core ships and delivers value integration-free.** NetBox/SharePoint/Lucid are **independently-sequenced adapters, not gates.** **Cut Lucid *import*** from early scope (shapes→`DESIGN_LINE` is fuzzy diagram parsing) — **export only**.
7. **`generateSoW` lift: the transform is free, the input adapter is the work.** The 0-DB pure function is trivial to lift; assembling `SoWInput` from our graph (design lines + functional locations) is the real effort. And a SoW is a **versioned customer/legal deliverable** — like the signed baseline, **freeze it on issue and tie its version to the design version.**

## Mission
Turn a structured **requirements brief** (rooms + use-cases + constraints) into a **versioned, room-centric BOM** and a **per-room Statement of Work** — and do it **in either order**: design-first (brief → design → quote) *or* quote-first (a quote drafted against rooms → design fills in the system). This kills the reference implementation's #1 bottleneck — *"Product/Design-avhengighet på komplekse tilbud"* (complex quotes stall waiting for a human designer) — without forcing sales to wait for design or vice-versa. The deep-AI angle is **cognitive recall, not rules**: the **AI Solution Agent** proposes `DESIGN_LINE`s by embedding-recalling similar past `DESIGN`/`PROJECT` nodes weighted by *delivered outcome*, pulls specs/pricing from the **Product** engine on demand, and flags the ~20% that need human validation. Signal-flow is implicit in Phase 1 (the BOM lists devices, not how they connect); the verified topology is Phase 2.

## Bidirectional flow & the functional-location principle
**The shared anchor that makes either direction cheap is the functional-location tree.** Every quote line *and* every design line is assigned to a **functional location** — a hierarchical node `SITE > BUILDING > FLOOR > ROOM > POSITION`. **System Design *authors* these nodes as design intent** (the rooms don't exist yet at design time — see Correction #2); NetBox/Assets later promote them to as-built. Because both artifacts hang off the *same* `FUNCTIONAL_LOCATION` nodes, the two directions are just two entry points into one graph:

- **Design-first:** `Motebrief.jsx` brief → `do_propose_design` populates `DESIGN_LINE`s per functional location → `do_design_to_quote` freezes them into a `QUOTE`.
- **Quote-first:** a sales quote is drafted with line items already tagged to functional locations → `do_design_from_quote` lifts those lines into a `DESIGN` (one `DESIGN_LINE` per quote line, same functional location) and the AI Solution Agent **fills the gaps** (missing accessories, infrastructure, labor) by recall — never starting from scratch.

Neither direction is privileged; the engine reconciles to one `DESIGN` ⇄ `QUOTE` pair keyed by functional location, **under the ownership/lock rule of Correction #3** (design owns the line until the signed-baseline freeze). Room-centricity (the reference implementation's SLA-per-room differentiator) falls out for free because the design-intent `FUNCTIONAL_LOCATION` nodes are the *same* nodes Assets/NetBox later promote to as-built.

## External integrations — independently sequenced, NOT on the critical path
**The value core ships integration-free** (Correction #6): recall → propose BOM → SoW → design↔quote needs only the graph + Product via A2A. These three adapters are **sequenced independently after the core delivers value** — they are *not* Phase-1 gates:
- **NetBox/Assets bridge (as-built reconciliation, not a design-time source):** the engine **authors** design-intent `FUNCTIONAL_LOCATION` nodes; the bridge (`system_design/netbox_bridge.py`, modelled on `dynamics365/netbox_bridge.py`) later **promotes** them to as-built and reconciles divergence (`promoted_to_asbuilt` edge). Note **functional-location sync is a build, not a "thin bridge"** — the `netbox` vertical has no sites/locations/racks today (Correction #1).
- **SharePoint (document store):** the issued `SoWDoc` + drawings persist to SharePoint (`DESIGN -[documented_in]-> sharepoint_ref`), not the repo. Reuses the organization's existing integration.
- **Lucid (diagramming) — EXPORT ONLY:** export/sync a generated room/signal-flow diagram to Lucid for `Romtegning.jsx`/`Skjermnettverk.jsx`. **Import (shapes→`DESIGN_LINE`) is cut from scope** (fuzzy diagram parsing, low ROI). In Phase 2 the exported Lucid diagram and the validated `SIGNAL_CHAIN` graph become two views of one model.

## Inspiration & triage
- **`sow-generator`** (the reference implementation → `generateSoW(SoWInput,{versionNumber}) -> SoWDoc`, verdict 🟢) — *"den reneste handoff-perlen"*: **0-DB pure function, versioning built-in. Lift near-1:1** into `sow.py` as a pure transform; no config-as-IP needed (pure transform, no weights to swap).
- **AI Solution Agent** (modulkart 03 Product & Solution Design — *learns 450+ projects, auto-generates 80% of standard BOM, Product validates 20%*) — verdict 🔵 **build-not-lift**: it is *structured thinking, not built code*. The value is **cognitive recall over the graph + ledger**, which NCE provides natively (`memories` + `v3_cognitive_ledger`); we build the recall loop, we do not port a Next.js module.
- **Lysning surfaces served:** `Motebrief.jsx` (requirements brief = the structured intake), `Moterom.jsx` (room), `Romtegning.jsx` (room drawing), `Komponenter.jsx` (the BOM line view) — all feeding `TilbudDetalj.jsx` (the quote the design becomes).

## Classification
**pull + heavy AI + bridges.** External systems in the loop: **NetBox** (functional-location tree + as-built topology, via the existing `netbox` vertical / a `netbox_bridge.py`), **SharePoint** (document store, existing NCE integration), **Lucid** (diagram import/export, REST API + token in `auth.py`). The engine also consumes the **cognitive graph** (recall) and the **Product** engine (specs/pricing via A2A). The heavy lift remains the recall + proposal loop; the bridges are thin (`netbox_bridge.py`, `sharepoint.py`, `lucid.py`).

## Graph contribution
- **Nodes** (entity_type prefix `SYSTEM_DESIGN_*`): `DESIGN`, `DESIGN_LINE`, plus the shared spine `FUNCTIONAL_LOCATION` (`SITE`/`BUILDING`/`FLOOR`/`ROOM`/`POSITION`) — co-owned with NetBox/Assets, the room-centric anchor.
- **Edges:** `DESIGN -[contains]-> FUNCTIONAL_LOCATION`, `FUNCTIONAL_LOCATION -[needs]-> DESIGN_LINE`, `DESIGN_LINE -[references]-> PRODUCT` (the spine `BOM_LINE -[references]-> PRODUCT` edge, roadmap §4), `DESIGN -[becomes]-> QUOTE` **and** `QUOTE -[realized_as]-> DESIGN` (the two bidirectional entry points), `FUNCTIONAL_LOCATION -[maps_to]-> netbox_site/location/rack` (bridge), `DESIGN -[documented_in]-> sharepoint_ref`, `DESIGN -[diagrammed_in]-> lucid_ref`.
- **Recall, not new writes:** design **outcomes** are *read back* from `PROJECT`/`TICKET` nodes via the `v3_cognitive_ledger` (change-orders, service tickets, margin erosion) to weight proposals — Phase 1 writes no outcome nodes of its own, it consumes the ones Project/Support already produce.
- Every derived edge carries `confidence` (0–1) and a `system_design_source_id` for retirement (roadmap §2.3).

## Core functions
<!-- BLOCKED ON OQ-2 / OQ-4: do_propose_design, do_generate_sow, do_design_from_quote, do_design_to_quote, do_validate_design, do_sync_functional_locations remain prospective Phase-1/Phase-2 design cores not exposed on main. -->
```python
async def do_propose_design(engine, params) -> dict
#   params: {namespace_id, brief:{rooms:[{name,use_case,constraints}], ...}, top_k?}
#   → recalls similar DESIGNs (outcome-weighted), proposes DESIGN_LINEs per ROOM via Product,
#     returns {design_id, rooms:[{room, lines:[{product_ref, qty, confidence, validated:false}]}], recall_evidence}

async def do_generate_sow(engine, params) -> dict
#   params: {namespace_id, design_id, version_number?}
#   → pure transform (lifted generateSoW): per-room versioned SoW doc; 0 DB reads of its own

async def do_design_from_quote(engine, params) -> dict
#   params: {namespace_id, quote_id}
#   → QUOTE-FIRST entry: lifts quote lines (already tagged to functional locations) into a DESIGN,
#     AI Solution Agent fills gaps (accessories/infra/labor) by recall; writes QUOTE -[realized_as]-> DESIGN

async def do_design_to_quote(engine, params) -> dict
#   params: {namespace_id, design_id}
#   → DESIGN-FIRST exit: freezes the BOM, writes DESIGN -[becomes]-> QUOTE, hands frozen lines to Sales

async def do_validate_design(engine, params) -> dict
#   params: {namespace_id, design_id, decisions:[{line_id, accept|override, ...}]}
#   → records the human 20% validation; bumps DESIGN version; logs decision to ledger

async def do_sync_functional_locations(engine, params) -> dict
#   params: {namespace_id, site_ref?}
#   → NetBox bridge: pull/sync the SITE>…>POSITION tree into FUNCTIONAL_LOCATION nodes

async def do_publish_design_docs(engine, params) -> dict
#   params: {namespace_id, design_id, targets:["sharepoint","lucid"]}
#   → persists SoW/drawings to SharePoint + syncs the room/signal-flow diagram to Lucid; links refs to DESIGN
```

## MCP tools
<!-- BLOCKED ON OQ-2 / OQ-4: Historical proposal listed 7 tools. Baseline 7304330 registers 2 MCP tools: system_design_ping and system_design_publish_design_docs. -->
| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `system_design_propose_design` | False | False | True | **Advisor** (proposes, human owns) |
| `system_design_design_from_quote` | False | False | True | **Advisor** (quote-first gap-fill) |
| `system_design_generate_sow` | True | False | False | **Advisor** (pure doc gen) |
| `system_design_design_to_quote` | False | True | True | **Actor** (freezes BOM → quote, with confirmation) |
| `system_design_validate_design` | False | False | True | **Actor** (records human validation) |
| `system_design_sync_functional_locations` | False | True | True | — (operator; NetBox bridge) |
| `system_design_publish_docs` | False | True | True | **Actor** (SharePoint + Lucid push) |

## REST routes
<!-- BLOCKED ON OQ-2 / OQ-4: Mounted REST routes at baseline 7304330 comprise exactly 1 endpoint: POST /api/system-design/publish-design-docs. -->
No-model path for the BFF/Lysning (admin app, HMAC/mTLS): `api_system_design_propose_design`, `api_system_design_design_from_quote`, `api_system_design_generate_sow` (read-only deterministic → REST per §2.2), `api_system_design_design_to_quote`, `api_system_design_validate_design`, `api_system_design_sync_functional_locations`, `api_system_design_publish_docs`.

## AI features
- **AI Solution Agent (Advisor) = cognitive RECALL, not a rules engine.** Embedding query over past `DESIGN`/`PROJECT` nodes, **weighted by OUTCOME** from the ledger — prefer designs that *DELIVERED well* (low change-orders, few tickets, held margin), not merely designs that were *quoted*. This structurally closes the reference implementation's "service→design silence" gap — **the closed loop none of the incumbents have** (see `90-competitive-landscape`). **Graceful degradation (Correction #4):** recall by *design similarity* works day one; **outcome-weighting switches on as Project/Support backfill the ledger** (and as the 450-project historical backfill lands). Recall *quality* depends on Project(7)/Support(10) — they are upstream of quality, not just downstream consumers.
- **On-demand, scoped enrichment (system-wide rule, §5):** when a proposed `DESIGN_LINE` references a `PRODUCT` with missing specs/pricing, the engine asks **Product** to enrich **that one product** via A2A — **never a bulk sweep**. Enrichment trigger = *line added to a design*, scoped to the referenced product only.
- **Propose-only until calibrated (Correction #5 / roadmap §9.3):** the agent proposes the full BOM but **human confirms 100%** at launch — **no baked-in "80/20"**. `do_validate_design` records accept/override to the ledger; the auto-accept confidence threshold rises **only** from measured precision. Auto-accepting BOM lines early *causes* the change-orders/margin-erosion we're trying to avoid.

## A2A flows
System Design is the **hub of the Quote→Design→Procure** flow (roadmap §5), and it works **both directions**:
- **Serves (design-first):** Sales agent → `system_design_propose_design` (Sales asks Design for a BOM).
- **Serves (quote-first):** Sales agent → `system_design_design_from_quote` (Sales has a quote, asks Design to realize + gap-fill it).
- **Initiates:** Design → **Product** (`product_*` specs/pricing, scoped) for missing line data; Design → **Procurement** (`procurement_*` live TCO/availability) before finalizing the BOM; Design ↔ **NetBox** vertical for the functional-location tree + as-built topology.
- **Emits:** `DESIGN -[becomes]-> QUOTE` back to Sales; frozen BOM handed to **Project**; docs/diagrams pushed to **SharePoint**/**Lucid**.

## Config keys
`NCE_SYSTEM_DESIGN_ENABLED` (per-namespace opt-in), `NCE_SYSTEM_DESIGN_RECALL_TOP_K` (similar-design fan-out), `NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTS` (config-as-IP JSON — how change-orders/tickets/margin discount a past design's recall score, per §2.9), `NCE_SYSTEM_DESIGN_AUTO_CONFIDENCE_THRESHOLD` (above → auto-accept line; below → flag for the human 20%). Integration keys: `NCE_SYSTEM_DESIGN_NETBOX_*` (reuse the `netbox` vertical's connection), `NCE_SYSTEM_DESIGN_SHAREPOINT_*` (site/drive/folder for design docs), `NCE_SYSTEM_DESIGN_LUCID_*` (API token, folder). Never host-specific keys in `nce/config.py` (FE-5).

## Tables/migrations
**Graph-first in Phase 1.** The engine writes `kg_nodes`/`kg_edges` (RLS by `namespace_id`); the SoW doc is a pure return value persisted to SharePoint (not the DB). One small own table if the bridge needs it: `system_design_doc_refs` (`design_id, kind, sharepoint_ref|lucid_ref, version, synced_at`) with `FORCE ROW LEVEL SECURITY` — mirrors the D365 `*_refs`/`*_sync_runs` pattern. Otherwise graph-only.

## Dependencies
- **Upstream:** **Product** (specs/pricing for lines), **Procurement** (live TCO/availability for finalize), **Sales** (initiates either direction; receives/supplies the `QUOTE`), **NetBox** vertical (functional-location tree + topology).
- **Downstream:** **Project** consumes the **frozen BOM** as its workspace; **Assets** consumes the as-built topology after install.
- **External integrations:** NetBox (in-loop), SharePoint (docs), Lucid (diagrams). **Blockers:** Lucid/SharePoint API credentials (config, not code); Phase 2's device-capability model is a separate sourcing project, deliberately not a Phase-1 blocker.

## Build phases
<!-- BLOCKED ON OQ-2 / OQ-4: Historical build phases B1-B5. Refer to docs/engines/system-design-admin.md for shipped milestone status. -->
- **Phase 1a — the value core (integration-free, Correction #6):** (1) `system_design` package + `mcp_handlers.py` + `TOOL_REGISTRY`; (2) **author** `FUNCTIONAL_LOCATION` design-intent nodes (no NetBox needed); (3) `do_propose_design` recall loop — **similarity-first**, outcome-weighting wired but dormant until the ledger backfills (Correction #4); (4) `do_design_from_quote` (quote-first gap-fill) — the bidirectional pair, under the §9 ownership/lock rule; (5) lift `generateSoW` → `sow.py` — **the work is the `SoWInput` adapter** from the graph, not the transform; **freeze the SoW on issue**, version tied to design version (Correction #7); (6) scoped Product/Procurement A2A enrichment; (7) `do_design_to_quote` freeze + `do_validate_design` (**propose-only**) + ledger feedback; (8) tests + tool-count. **This ships and delivers value with zero external systems.**
- **Phase 1b — adapters (independently sequenced, not gates):** `system_design/netbox_bridge.py` = **build functional-location sync + `promoted_to_asbuilt` reconciliation** (design-intent → as-built; *not* a thin bridge — Correction #1/#2); `sharepoint.py` (SoW/doc store); `lucid.py` **export only** (`do_publish_design_docs`). Each lands when its value justifies it.
- **Phase 2 (additive, deferred) — generate *and prove* (the moat, see `90-competitive-landscape`):** layer `DEVICE`/`PORT`/`SIGNAL_CHAIN`/`RACK`/`CABLE` nodes + a **device-capability model** (adopting the **AVIXA Revit parameter schema** — see `07a-bim-research`) onto the **same `DESIGN` node**, and add **design-validation graph queries** none of the incumbents have: signal-flow continuity, port/format compatibility (HDMI 2.1 vs 2.0, Dante channel counts), PoE/power/heat budget, SPOF/redundancy, AVIXA-checkpoint conformance — each returning pass/fail + reasons. Deepen the NetBox bridge to the as-built twin; on BIM, `IfcSpace ↔ FUNCTIONAL_LOCATION` and COBie handoff to Assets (`07a`). **Enriches, does not rewrite** — the Phase-1 contract (`DESIGN`/`FUNCTIONAL_LOCATION`/`DESIGN_LINE`, the `do_*` functions) is unchanged.
