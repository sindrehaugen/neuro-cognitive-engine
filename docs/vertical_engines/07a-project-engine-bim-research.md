# 07a — Project/System-Design Engine: BIM Integration Research & Idea Backlog

<!-- BLOCKED ON OQ-2 / OQ-4: RESEARCH COMPANION. Architectural research backlog. Verified-against: 7304330 -->

**Status:** research companion to `07-project-engine.md` + `06-system-design-engine.md` · **Date:** 2026-06-17
**Question asked:** *"how do we integrate with BIM for the Project (and System Design / Assets) engines — without getting locked into Autodesk?"*
**Method:** parallel cited investigations — the openBIM stack (IFC / COBie / IfcOpenShell); the Autodesk APS cloud; Speckle as a graph-native AEC data platform; manufacturer BIM content + the AVIXA AV-parameter standard; and the 2024–26 AI+BIM frontier. Sources at the bottom.

---

## 1. Strategic read — where we play

A graph-native cognitive backend is, almost by accident, **the right shape for BIM**. A building model *is* a graph — a spatial tree (site → building → storey → space) with equipment hung off the nodes and typed relationships between them. Every other AEC tool flattens that graph into files (.rvt, .ifc, .nwd) and pays to reconstruct it on read. We already hold it as nodes and edges. So the integration question is not "how do we learn BIM" but **"how do we speak the open BIM dialects in and out of a graph we already have."**

The discipline that keeps this from becoming an Autodesk dependency:

1. **Standardize internally on OPEN standards — IFC + Speckle + COBie.** These are the things we read, write, validate, and store against. Our `FUNCTIONAL_LOCATION` tree maps 1:1 onto the IFC spatial structure; our nodes/edges map ~1:1 onto Speckle objects; our handover maps onto COBie worksheets. The open stack is the *foundation*.
2. **Make Autodesk and AI pluggable connectors, never the foundation.** APS (Model Derivative, Data Management) is a *read-adapter* behind the same interface as the IFC and Speckle adapters — useful to pull a client's Revit/ACC model without a Revit licence, but swappable and metered. Scan-to-BIM and LLM normalization sit behind the same boundary. If any vendor disappears or triples its price, the open core is untouched.
3. **Position System Design as the AV-native authoring layer that speaks BIM out the back.** Integrators today author in AV-CAD (D-Tools SI, XTEN-AV X-DRAW) and merely *consume* the architect's BIM. AVIXA is pushing AV *into* BIM but the authoring gap is real. We can be the layer that designs AV natively **and** emits valid IFC/Speckle/COBie — the thing D-Tools/XTEN only partially do.

> **Positioning in one line:** *Not another BIM viewer — a graph-native AEC backend whose spatial tree already is the IFC structure, that versions like Speckle, hands over like COBie, and treats Autodesk + AI as connectors it can unplug.*

---

## 2. The landscape (what to borrow from each)

| Standard / platform | What it is | The idea worth stealing |
|---|---|---|
| **IFC / openBIM** (ISO 16739; IFC4.3 ADD2 current, IFC4.4 minor in progress, IFC5 in dev, more graph/DB-oriented) | The vendor-neutral building data model. Serializations: IFC-SPF (.ifc), ifcXML, **ifcJSON** | The spatial structure **maps directly to our tree**: `IfcSite→IfcBuilding→IfcBuildingStorey→IfcSpace` via `IfcRelAggregates`; equipment attached via `IfcRelContainedInSpatialStructure`; **`IfcGlobalId` ↔ our node-id** as the durable cross-system key. IFC5's DB/graph orientation validates the bet. |
| **IfcOpenShell** (Python, LGPL) | The open read/write engine for IFC | `ifcopenshell.open()`, `model.by_type("IfcSpace")`, `ifcopenshell.util.element.get_psets()`, `ifcopenshell.api` for writes; handles SPF/ifcXML/ifcJSON and IFC2X3/IFC4/IFC4.3 — **one open library does both import and export**, no Revit. |
| **COBie** (as-built/FM handover MVD, an IFC4 subset) | Structured equipment/space/warranty handover; worksheets Facility/Floor/**Space**/Zone/**Type**/**Component**/System/**Spare**/Resource/**Job**/Document/**Attribute**/**Warranty** | The **handover schema** to emit at project close — and >90% of real-world use is the **spreadsheet form**, so ship that first. Maps cleanly onto Assets + Support: ROOM→Space, product→Type, installed instance→Component, warranty/spares/PM→Warranty/Spare/Job. |
| **Speckle ★** | Open-source AEC data platform; moves BIM as **versioned objects, not files** | **Best fit for a graph engine.** Every object is a `Base` (props + nested children); `Collection` gives arbitrary nesting; **detach/reference dedupes shared sub-objects = nodes + edges**; versioning is projects→models→versions (a commit trail). APIs: **GraphQL + specklepy** + many connectors; **Speckle Automate** runs server-side functions per version. Map objects⇄our nodes/edges ~1:1; map **versions→our phase-gate snapshots**. |
| **Autodesk APS** (formerly Forge; proprietary cloud, OAuth2) | The Revit/ACC ecosystem's API surface | **Model Derivative API** extracts object hierarchy + properties **without Revit**; **Data Management API** traverses ACC/BIM360 file trees; Viewer (WebGL), Webhooks, Design Automation (headless Revit); Navisworks = clash detection (heavier). Take the **read-adapter pattern only** — pull a client model, ingest rooms+AV components — behind the open interface. Caveat: metered/paid/proprietary. |
| **Manufacturer BIM content** | AV vendors publish Revit families on BIMobject / ARCAT / BIMsmith | The catalog should be **BIM-ready by construction** so we can emit families/Types, not hand-model them. |
| **AVIXA AV-in-BIM standard** | Official **"AV Device Revit Parameter List"** (.xls, Phase 2.1) + shared parameter file + OpenDefinery collection; recommends Revit category **"Communication Devices"** | **Adopt the AVIXA parameter set as the canonical property schema for equipment/PRODUCT nodes** (model number, device type, clearances, mounting). Concrete, published, and ties straight to Product engine 02a. |
| **AV-CAD incumbents** (D-Tools SI, XTEN-AV X-DRAW) | Where AV is actually designed today | They author AV but only **consume** architect BIM. The open-BIM-out-the-back authoring layer is the gap to fill. |
| **AI + BIM** (2024–26) | Scan-to-BIM, model→schedule, Pset normalization | Scan-to-BIM is the most mature but **semi-automated, not autonomous** (~1 hr/GB). Model→schedule/BOM extraction is a **deterministic query today** (IfcOpenShell `by_type`+Psets / APS Model Derivative / Speckle objects); AI's job is **normalization/classification**, not extraction. |

---

## 3. Idea backlog, prioritized & mapped to our engines

Each idea names the concrete change (which engine doc / node / edge / config it touches) and the engine it serves.

### Tier A — adopt now (architecture-shaping, low regret, open-core)

**A1. IFC adapter via IfcOpenShell (the open floor).**
An import/export adapter built on IfcOpenShell. **Import:** walk `IfcRelAggregates` to build `IfcSite→IfcBuilding→IfcBuildingStorey→IfcSpace` and map onto our `FUNCTIONAL_LOCATION` tree (SITE>BUILDING>FLOOR>ROOM); contained elements (`IfcRelContainedInSpatialStructure`) become equipment nodes at the **POSITION** layer (the element's local placement); AV/MEP gear arrives as `IfcDistributionElement` / `IfcAudioVisualAppliance` / `IfcCommunicationsAppliance` / `IfcBuildingElementProxy` with properties in Psets. **Export:** emit valid `.ifc` / ifcJSON from our designs. **Key:** persist `IfcGlobalId` ↔ our node-id as the cross-system identity. *(IFC4.3 today; structure is stable into IFC5.)*
- *Touches:* new `ifc-adapter` (IfcOpenShell); `FUNCTIONAL_LOCATION` import/export mapping; node gains `ifc_global_id`; new `ifc-spatial-map.json` config (IfcSpace→ROOM rules).
- *→ idea for **System Design (06)** + **Project (07)**:* import the architect's shell to seed the location tree; export the AV design as IFC for the BIM coordinator.

**A2. Speckle adapter — the highest-priority integration (best graph fit).**
A specklepy/GraphQL adapter mapping **Speckle objects⇄our nodes/edges ~1:1** (`Base`→node, nested children/`Collection`→edges, detach/reference→shared-node dedup). Map **Speckle versions→our Project phase-gate snapshots** so a commit trail on the live model lines up with our gate baselines. Add a **Speckle Automate function** that validates our BOM against the live model on each commit and flags **data-clashes** (e.g. "room has a display node but no power/network element").
- *Touches:* new `speckle-adapter` (specklepy + GraphQL); version↔gate snapshot mapping in Project; an Automate validator fn; `speckle_object_id` on nodes.
- *→ idea for **Project (07)**:* versions↔phase-gates is the natural home; the as-built/live-model state lives here. *→ idea for **System Design (06)**:* the per-commit BOM-vs-model data-clash check.

**A3. AVIXA parameter set as the canonical equipment-node schema.**
Adopt the AVIXA **AV Device Revit Parameter List (Phase 2.1)** + shared parameter file + OpenDefinery collection as the property schema for equipment/PRODUCT nodes (model number, device type, clearances, mounting), and align on the recommended **"Communication Devices"** Revit category. The catalog becomes BIM-ready by construction — IFC/Speckle export and manufacturer-family matching all fall out of one shared schema.
- *Touches:* equipment/`PRODUCT` node property model; new `avixa-param-schema.json`; the System Design Phase-2 device-capability model adopts these params.
- *→ idea for **Product (02 / 02a)**:* equipment schema = AVIXA params (folds straight into the ETIM-coded schema — AVIXA params become the AV-specific feature set). *→ idea for **System Design (06)**.*

**A4. COBie exporter at handover (→ Assets / Support).**
At project close, emit COBie from the graph: ROOM→Space, product→Type, installed instance→Component, warranty/spares/PM→Warranty/Spare/Job, plus Floor/Zone/System/Attribute/Document. **Spreadsheet form first** (>90% of real use). This is the bridge that feeds the **Assets** and **Support** engines a clean as-built record. The same mapping run in reverse **imports** COBie so we can ingest buildings we didn't design.
- *Touches:* new `cobie-exporter` (graph→worksheets) + `cobie-importer`; consumes Assets node model + Support warranty/SLA/PM data.
- *→ idea for **Assets (09)**:* COBie handover is the canonical Assets ingest at go-live. *→ idea for **Support**:* Warranty/Spare/Job worksheets seed SLA + PM.

### Tier B — design for (build when the trigger arrives)

**B1. APS read-adapter (optional connector, never the floor).**
When a client lives in Revit/ACC and won't export IFC, pull the model via **Data Management** (traverse the ACC/BIM360 tree) + **Model Derivative** (object tree + properties, **no Revit licence**) and ingest rooms + AV components as graph nodes. Kept strictly **behind the same interface as the IFC/Speckle adapters** so we're never APS-locked.
- *Why:* meets clients where they are without adopting Autodesk as the foundation; metered/paid, so it's a convenience tier, not the spine.
- *Touches:* new `aps-adapter` implementing the same import contract as `ifc-adapter`; OAuth2 + webhook config; degrades cleanly to "ask for an IFC export."

**B2. Deterministic model→schedule / BOM service (ship-now-confidence).**
A service that turns any ingested model into a room schedule + equipment schedule + BOM via **structured query** — IfcOpenShell `by_type`+Psets, APS Model Derivative, or Speckle objects, whichever adapter sourced it. No AI in the extraction path → high confidence, auditable.
- *Touches:* new core fn over the adapter layer; feeds Product (BOM lines) + System Design (room schedule); A2A-served.

**B3. LLM-assist for Pset normalization + data-clash flagging.**
Real-world Psets are messy and inconsistent across authors. Use an LLM to **normalize incoming Pset data onto the AVIXA schema** (A3) and to **flag data-clashes** the deterministic check (B2/A2) surfaces — classification/normalization only, **never** the extraction itself, and confidence-scored.
- *Touches:* normalization step inside the import adapters; reuses the Product engine's verbalized-confidence discipline (02a A4 — not logprobs).

**B4. Scan-to-BIM / digital-twin as an as-built *source* (partner, don't build).**
Treat point-cloud→classified-BIM pipelines as an **ingestion source** for as-built/renovation projects, not something we build. It's the most mature AI-BIM area but still semi-automated (~1 hr/GB). Pull its IFC output through A1; let it feed the as-built state alongside the live Speckle model.
- *Touches:* documented as an external source feeding `ifc-adapter`; no new core build.

### Tier C — later / opportunistic

- **C1. Speckle/APS Viewer embed** for proposal microsites and field reference (WebGL) — front-end work on the adapter surface; Sales/Field Tech facing.
- **C2. Clash detection** beyond data-clashes — geometric clash (Navisworks-style) is heavy via APS; defer unless a project demands it, and prefer Speckle/IFC-side checks first.
- **C3. Emit manufacturer-grade Revit families** from AVIXA-schema'd catalog nodes, so we publish content the way BIMobject/ARCAT do rather than only consuming it.
- **C4. IFC5 migration watch** — as IFC5 lands its DB/graph orientation, revisit the IFC adapter to map our graph more natively (likely *less* impedance, not more).

---

## 4. Net changes to fold into the engine docs

- **Project (07):** add a **BIM-integration section** — Speckle versions↔phase-gate snapshots (A2), the as-built/live-model state, and COBie handover as the close-out artifact (A4).
- **System Design (06):** the **Phase-2 device-capability model adopts the AVIXA parameter set** (A3); document **IfcSpace↔FUNCTIONAL_LOCATION** mapping (A1) and the per-commit BOM-vs-model data-clash check (A2).
- **Assets (09):** add **COBie handover** as the canonical ingest at go-live (A4).
- **Product (02 / 02a):** **equipment schema = AVIXA params** (A3), layered into the ETIM-coded schema as the AV-specific feature set.

A short "Research-informed direction" pointer is added to `07` and `06` referencing this doc.

**Recommended build order:** 1) IFC adapter (IfcOpenShell, open) → 2) Speckle adapter (specklepy/GraphQL, best fit; versions→phase-gates) → 3) COBie exporter (→Assets/Support) → 4) AVIXA param schema as the equipment-node model → 5) APS read-adapter (optional connector) → 6) deterministic model→schedule, then LLM normalization on top. **Lock-in posture:** open core (IFC/Speckle/COBie); Autodesk + AI + scan = pluggable connectors.

---

## 5. Honest flags (don't over-trust the marketing)

- **Directional / vendor-sourced:** Speckle adoption counts, AI efficiency percentages, scan-to-BIM throughput (~1 hr/GB), and XTEN-AV's BIM depth are vendor/marketing claims — treat as directional, not measured.
- **Verified from primary/standards sources:** the IFC spatial structure + relationships, IfcOpenShell's capabilities, the COBie worksheet structure, the APS API surface, and the AVIXA parameter resources are confirmed from standards/primary docs.
- **Our inference, not an observed product:** the Speckle-version↔phase-gate mapping and the BOM-vs-model "data-clash" Automate check are our design proposals, not features anyone ships today.
- **AV-in-BIM is modest but rising** — most integrators still author in AV-CAD and consume architect BIM; AVIXA is pushing the other direction. The native-authoring-with-open-BIM-out wedge is real but the market is early.

---

## Sources

**openBIM / IFC:** [buildingSMART IFC](https://www.buildingsmart.org/standards/bsi-standards/industry-foundation-classes/) · [IFC4.3 IfcSpatialStructureElement](https://ifc43-docs.standards.buildingsmart.org/IFC/RELEASE/IFC4x3/HTML/lexical/IfcSpatialStructureElement.htm) · [IfcOpenShell](https://ifcopenshell.org/) · [IfcOpenShell docs](https://docs.ifcopenshell.org/introduction.html) · [COBie](https://en.wikipedia.org/wiki/COBie)

**Autodesk APS:** [Autodesk Platform Services overview](https://www.autodesk.com/products/autodesk-platform-services/overview) · [Model Derivative API](https://aps.autodesk.com/apis-and-services/model-derivative-api) · [Data Management API](https://forge.autodesk.com/developer/overview/data-management-api)

**Speckle:** [Speckle docs](https://speckle.guide/) · [decomposition / Base](https://speckle.guide/dev/decomposition.html) · [Speckle GitHub](https://github.com/specklesystems) · [AEC Magazine on Speckle](https://aecmag.com/features/speckle-the-open-source-cloud-data-platform/)

**Manufacturer BIM content & AVIXA standard:** [BIMobject AV (Revit)](https://www.bimobject.com/en-us/categories/electronics/aves?software=revit) · [ARCAT AV BIM](https://www.arcat.com/content-type/bim/equipment-11/audio-visual-equipment-115200) · [AVIXA AV Device Revit Parameter List (.xls)](https://www.avixa.org/docs/default-source/xls/avixaavdevicerevitparameterphase21.xls) · [AVIXA Revit Parameter User Guide (PDF)](https://www.avixa.org/docs/default-source/default-document-library/broch_bim_revit_parameter.pdf) · [AVIXA OpenDefinery collection](http://app.opendefinery.com/collection/28730)

**AI + BIM:** [Scan-to-BIM deep-learning 2025 (ScienceDirect)](https://www.sciencedirect.com/science/article/pii/S2352710225008332)
