# 90 — Competitive Landscape: Jetbuilt · XTEN-AV · D-Tools (and how the NCE engine suite wins)

<!-- BLOCKED ON OQ-2 / OQ-4: RESEARCH COMPANION. Architectural research backlog. Verified-against: 7304330 -->

**Status:** competitive-analysis companion to the engine specs · **Date:** 2026-06-17
**Question asked:** *"deeply analyse the AV-integration software incumbents → ideas for our engines + an honest read on how we beat them and where they're ahead."*
**Method:** cited investigation of the three platforms integrators actually run — Jetbuilt, XTEN-AV, D-Tools — plus AVIXA's published standards. Sources at the bottom.
**Informs:** System Design(06), Sales(05), Project(07), Product(02), Support(10), Procurement(01).

---

## 1. Executive read — the loop nobody closes

The AV-software market is consolidating around one slogan: **"one platform, design → delivery."** All three incumbents are racing left-to-right along the same conveyor — catalog → quote → drawing → proposal → project management. **None of them closes the loop.**

- Service and failure data **never feed back into design.** Even D-Tools — the only vendor shipping a real service suite — keeps service a strictly downstream silo.
- Delivered outcomes **never inform future designs.** What shipped late, what failed in the field, what won the deal — none of it weights the next BOM.
- Their "AI" is **generative-from-templates:** BOM-recommendation and drawing-generation synthesized from product manuals and best-practice corpora. It **produces**; it does not **validate against a device-capability model.**
- Despite "AVIXA-compliant symbol" marketing, **none does real signal-flow validation or AVIXA-rule checking.** A compliant *symbol* is not a validated *system*.

That double absence — the missing cross-domain feedback loop, and the missing capability/validation layer — is our structural opening. It maps directly onto the NCE thesis (`00-ENGINES-ROADMAP.md` §1, §4): the incumbents are 3–14 separate databases stitched by consultancies; we are **one cognitive graph** where a Support failure event and a System Design query land on the *same device node*. The loop they cannot build because of their architecture, we get for free because of ours.

---

## 2. Positioning table — who owns what

| | **D-Tools** (SI desktop + Cloud) | **XTEN-AV** | **Jetbuilt** |
|---|---|---|---|
| **Owns** | Engineering drawings (native Visio + AutoCAD, draw-first → BOM, decades of shapes/blocks); real procurement/inventory; the **only shipping service-management suite** | AI-first single platform design → proposal → PM; largest catalog | Fast, beautiful quoting + client presentation |
| **Catalog** | ~1.9M, manufacturer-approved + dealer-priced | **~1.5M products / 5,200 brands** (breadth leader; freshness complaints) | **3.4M products** with live dealer pricing |
| **AI brand** | Least AI-forward | **XAVIA** — AV's self-styled "first AI agent": NL-orchestrated BOM rec + X-DRAW auto-schematics/rack/cable + auto-proposal | **Jetbot** — Drawings (Nov 2025), Service Desk (manual-aware troubleshooting), Recommend (NL → BOM + labor, 2026, **self-checks vs design rules N times**) |
| **PM** | Mature | X-PRO cloud PM (strongest single-platform design→PM) | Lighter PM; design capability **only since 2026** |
| **Procurement / service** | Strong (real inventory + service) | Weak | Modular (Stock, Service) but lighter |
| **Shape** | Heavy, costly, complex, Windows-rooted | Boldest AI brand + momentum (~30k users claimed) | Modular suite: Funnel (CRM) · Project · Drawings · Jetpay · Portal · Install · Stock · Service · Radar |

> **The tell:** integrators run **multiple tools at once** — Jetbuilt for quoting *plus* XTEN for automation; D-Tools for engineering *plus* a catalog like Portal. None does fast-quote **and** deep-engineering **and** procurement **and** service. Consultancies are paid to stitch them. That multi-tool reality is the empirical proof of the unified-spine gap.

---

## 3. Per-engine ideas + how we beat them

### Sales(05) / DealRoom
- **Borrow:** sign-and-pay in one step (Jetpay — a proven conversion lever) + a persistent client Portal.
- **Beat:** their proposal is a rigid, near-static signed PDF ("limited layout control" is a verified Jetbuilt complaint). A true **DealRoom** is live, interactive options/packages with **real-time re-pricing backed by the graph** — a client toggle instantly updates BOM/labor/PO downstream, because they all reference the same nodes (the signed-baseline freeze in `00` §4 makes the "live until signed, frozen after" boundary clean).

### Product(01/02)
- **Borrow:** the **3-layer catalog** (global master / company-overlay / custom) so global updates never overwrite negotiated pricing — Jetbuilt's pattern, already mirrored in `02a` (Tier C C3 personalized catalog).
- **Beat:** **stale pricing is Jetbuilt's #1 verified complaint** ("automatic price changes weren't as automatic" → forced upward revisions + awkward client conversations). Our answer is **on-demand, scoped enrichment at quote time** with multi-source verification (`02a` §1, §3 A2), holding **competing distributor prices as graph edges** so Procurement optimizes at PO time. **Outcome-weighted recall** surfaces products/vendors that *won deals and shipped on time* — a signal no catalog has.

### System Design(06)
- **Borrow:** Jetbot Recommend's "self-check vs design rules N times" reliability loop.
- **Beat:** build the thing all three conspicuously **lack** — a **"validate / audit this design" capability.** Signal-flow continuity; port/format compatibility (HDMI 2.1 vs 2.0, Dante channel counts, PoE budget, power/heat in rack); SPOF/redundancy; **AVIXA checkpoint conformance** — all as **graph queries with explanations.** Their AI **produces**; ours produces **and proves.** Encode AVIXA design checkpoints as **graph validators over a device-capability model** (this is the A-then-B signal-flow/AVIXA layer already committed in `00` §8 decision 1).

### Project(07)
- **Reality:** D-Tools and XTEN PM exist, but they are **conveyor belts ending at install.**
- **Beat:** make **as-built a first-class state transition** — `designed → quoted → delivered → as-built → serviced` — with Project **emitting the as-built diff back onto the design nodes** (NetBox is in the design loop from the start, `00` §8). The state machine is what turns a one-way pipeline into a loop.

### Support(10)
- **Reality:** Jetbot Service Desk reads owner's manuals — a **static snapshot.**
- **Beat:** **on-demand scoped enrichment** pulling live manual/firmware/known-issue **per case** (`02a` B2 lifecycle pattern), plus the **failure → design feedback edge** (`00` §4: `TICKET/ASSET -[failure_pattern]-> PRODUCT`). The case doesn't just resolve — it *teaches the graph*.

### Procurement(01)
- Where they pick **one vendor tier**, we hold **competing prices as edges** and optimize at PO time (A2A with Product — the Quote→Design→Procure flow in `00` §5).

### Pricing model (commercial moat)
- Jetbuilt's **à-la-carte per-seat modules** are its biggest commercial friction (verified: "prohibitive to roll out to all field techs"). Customers **ration seats** → the field layer stays underused → **that starves the data.**
- A **unified-spine price** (per-company / per-active-project, with cheap-or-free field access) undercuts them **and feeds the graph more data** — the data advantage compounds precisely where their pricing model bleeds.

### Integrations
- Theirs are **brittle point-to-point syncs** (Jetbuilt's QuickBooks Online integration is "lackluster," breaks on change-order merges, much of it outsourced to MindCloud).
- Ours is **MCP/A2A-native — a single sync surface with the graph as system-of-record** (`00` §2). We have nothing to "integrate between," because there is one substrate.

---

## 4. The whitespace gaps — the moat

None of the three solves these. The first is the moat; the rest are the reasons it stays defensible.

1. **The cross-domain feedback loop is absent.** Service/failure → design **does not exist anywhere.** Even D-Tools keeps service a downstream silo. For us, a Support failure event is an **edge on the same device node System Design queries**, and outcome-weighted recall **down-weights designs that underperformed in delivery.** This is **architecturally impossible for them** (separate databases) and **free for us** (one graph). **THIS IS THE MOAT.**
2. **No true signal-flow / AVIXA validation.** "Compliant symbols" ≠ validated systems. The validate-this-design capability has no incumbent equivalent.
3. **No real device-capability model.** They have product **catalogs** (SKU / price / symbol), not **capability/constraint models** (port types, protocols, channel counts, power draw, firmware/interop). Theirs is **wide and flat**; ours is **deep and reasoned** via on-demand enrichment (`02a` §1).
4. **No unified data across sales → design → delivery → service.** Proven by the multi-tool reality and the consultancy-stitching market. **We have nothing to integrate:** one substrate, MCP/A2A-native.

---

## 5. Where they are genuinely ahead — be honest

We start **behind** on the things they have spent years building, and pretending otherwise would mislead System Design and Product sequencing.

- **D-Tools:** mature, trade-trusted **engineering drawings** (Visio/AutoCAD native, decades of shapes/blocks), **real procurement/inventory**, and the **only shipping service suite.** We will not out-draw D-Tools on day one.
- **Jetbuilt:** the **fastest, prettiest quoting** and a slick natural-language "Modify" drawing UX.
- **XTEN-AV:** the **largest catalog**, the **strongest single-platform design→PM**, and the **boldest AI brand with real momentum.**
- **All three:** years of real drawings, real catalogs, real customers.

**Honest conclusion:** we begin behind on **catalog breadth** and **drawing maturity.** Our edge is **not** out-drawing or out-cataloging anyone in year one — it is the **graph, the validation, and the closed loop.** Concede breadth and polish short-term; win the seam.

---

## 6. The one-line moat

> Their AI automates **drawing and quoting**; none reasons about **device capability**, validates **signal flow**, or learns from **delivered/serviced outcomes.** Our one-graph, MCP/A2A-native architecture — with **outcome-weighted recall** + **on-demand enrichment** — targets the seam all three leave open: **the closed loop from service/delivery back into design.** Win there first; concede catalog breadth and drawing polish short-term.

---

## 7. Sources

**Jetbuilt:** [Homepage](https://jetbuilt.com/) · [Pricing](https://jetbuilt.com/pricing/) · [Funnel (CRM)](https://jetbuilt.com/funnel/) · [Service](https://jetbuilt.com/service/) · [Item database (help)](http://help.jetbuilt.com/en/articles/3009819-your-item-database) · [Jetbot Recommend press](https://jetbuilt.com/press/jetbuilt-launches-jetbot-recommend-to-accelerate-av-system-design-workflows/) · [Jetbot Drawings @ ISE 2026 (AVNation)](https://www.avnation.tv/2026/01/16/jetbuilt-to-debut-jetbot-drawings-suite-at-ise-2026/) · [Capterra reviews](https://www.capterra.com/p/149371/Jetbuilt/)

**XTEN-AV:** [Homepage](https://xtenav.com/) · [XAVIA](https://xtenav.com/xavia/) · [X-DRAW](https://xtenav.com/x-draw/) · [X-PRO](https://xtenav.com/x-pro/) · [Signal-flow diagram software](https://xtenav.com/signal-flow-diagram-software/) · [Capterra reviews](https://www.capterra.com/p/10008832/XTEN-AV/reviews/)

**D-Tools:** [System Integrator](https://www.d-tools.com/system-integrator) · [Visio / AutoCAD integration](https://www.d-tools.com/system-integrator-visio_autocad-integration) · [Cloud Service Management (Essential Install)](https://essentialinstall.com/news/d-tools-cloud-service-management/)

**Landscape & standards:** [AVSI proposal-software landscape (D-Tools · XTEN · Portal.io · Jetbuilt)](https://officehubtech.com/blogs/avsi-proposal-software-landscape-d-tools-xten-portal-io-jetbuilt/) · [AVIXA published standards](https://www.avixa.org/resources/standards/published-standards) · [AVIXA performance verification](https://www.avixa.org/standards/audiovisual-systems-performance-verification)
