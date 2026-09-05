# 02a — Product Engine: PIM / Icecat Research & Idea Backlog

<!-- BLOCKED ON OQ-2 / OQ-4: RESEARCH COMPANION. Architectural research backlog. Verified-against: 7304330 -->

**Status:** research companion to `02-product-engine.md` · **Date:** 2026-06-17
**Question asked:** *"deeply analyse solutions like Icecat and get ideas for what we can do here."*
**Method:** three parallel cited web investigations — Icecat deep-dive; the broader PIM/syndication/classification-standards landscape; AV-specific tooling + AI-enrichment techniques. Sources at the bottom.

---

## 1. Strategic read — where we play

The incumbents split into two camps, and **neither owns our wedge**:

- **Catalog owners** (D-Tools ~1.9M products, manufacturer-approved + dealer-priced, *not* scraped; XTEN-AV ~1.5M, AVIXA-aligned BOM). Their moat is *trusted bulk data* built over decades of supplier relationships. **We will not out-bulk them.**
- **Content syndicators / PIMs** (Icecat ~25M datasheets, brand-sponsored; Akeneo/Salsify/inriver; 1WorldSync+Syndigo merged into the dominant GDSN network). Their model is *manufacturers publish → retailers consume*, with GenAI **bolted onto** a human-curated catalog.

**The gap we exploit (all three investigations converged on this):**
1. **AV has no product-data standard and no manufacturer APIs.** Crestron/Biamp/QSC/Shure/Neat/Poly/Cisco publish **PDF spec sheets + control drivers**, not JSON/PIM feeds. AVIXA standardizes *process* (ANSI/AVIXA D401.01 documentation, rack design), **not product data**. So **PDF-in → structured-out is the whole opportunity.**
2. **We are MCP-native.** Icecat is *retrofitting* an MCP service so AI agents can reach its catalog; our Product Engine is an MCP tool from day one (roadmap §5). That is a structural advantage, not a feature to add.
3. **On-demand, single-SKU, confidence-scored enrichment is our discipline** (the hard rule in `02` §AI features). Where a PIM runs bulk enrichment pipelines, we enrich exactly the one product entering a quote/design — which is *cheaper, safer, and more trustable*, and is precisely what the catalog owners' bulk feeds can't do for the long tail.

> **Positioning in one line:** *Not a bigger AV catalog — an AI-native, ETIM-coded, MCP-exposed enrichment engine that turns any manufacturer PDF into a trusted, confidence-scored, graph-connected product on demand.*

---

## 2. The landscape (what to borrow from each)

| Solution | What it is | The idea worth stealing |
|---|---|---|
| **Icecat** | Brand-sponsored product-content syndication; ~25M datasheets, 70+ langs; free Open tier + paid Full; now "AI platform" + MCP | **Per-category attribute templates**; **Product Completeness Score (0–100) + Brand Data Health roll-up**; **provenance flag** (`SUPPLIER` vs editor-verified `ICECAT`); **daily delta index with explicit REMOVED flags**; three delivery surfaces (delta feed / token JSON API / drop-in JS) |
| **Akeneo** | Reference PIM | **Two-axis quality**: *completeness* (required fields per channel) **and** an **A–E quality grade** (enrichment + consistency); **AI extraction from PDF/images constrained to valid option-list values** (not free text); central AI-prompt config; bring-your-own-LLM governance |
| **Salsify / inriver** | PXM + syndication | **Per-channel requirement profiles** validated before publish; **"publish to an AI agent / LLM endpoint" as a first-class channel**; named single-purpose agents (Extract/Translate/Describe) with human-in-loop |
| **1WorldSync + Syndigo / GS1 GDSN** | The dominant manufacturer→retailer data network | **"Publish once, sync everywhere"**: downstream channels *subscribe* to a product and receive deltas, keyed on stable identity (GTIN+GLN, GPC Brick) |
| **ETIM** | Electro-technical classification standard, dominant in EU/Nordics, **integrated into GS1 GDSN (May 2024)** | **The schema to adopt.** Coded (class, feature, value, unit) tuples; ~6,000 classes; values from a shared 7,500-value list (free-text prohibited → comparable across brands); **multilingual for free** (coded values, language-independent IDs); ETIM-MC carries 3D/BIM geometry |
| **D-Tools** | AV/security integrator catalog + design | **Manufacturer-approved, dealer-priced data is the trust moat**; products used in a quote auto-save to the dealer's own catalog (personalized catalog) |
| **XTEN-AV (XAVIA)** | AI-native AV design + BOM | **"Audit an existing BOM for gaps"** + **AI accessory/cable suggestion** into the BOM — the single-shot move to mirror |
| **Cisco EoX API** | Manufacturer EOL feed | The **per-SKU EOL pattern**: query by PID → EndOfSale/EndOfSupport dates + **migration/replacement product** |

---

## 3. Idea backlog, prioritized & mapped to our engine

Each idea names the concrete change to `02-product-engine.md` (which `do_*` / node / config / table it touches).

### Tier A — adopt now (architecture-shaping, low regret)

**A1. ETIM-native attribute schema** *(the single highest-leverage idea — all three investigations)*
Store every spec as a coded **(ETIM class, feature, value, unit)** tuple instead of free-form fields. Upgrade the `PRODUCT_CATEGORY` node into an ETIM-class reference; each class defines its mandatory/searchable feature template. Keep **GTIN as the universal key, brand+MPN as canonical match key** (Icecat: 100% have MPN, ~70% GTIN), and store **UNSPSC / GPC / eCl@ss as optional crosswalk codes** for procurement (Procurement/ERP) and retail channels.
- *Why:* multilingual catalog **for free** (huge for a Norwegian company — coded values render in any language), instant interoperability with Nordic electro suppliers/wholesalers already shipping ETIM data, and it turns the LLM's job from *"invent attributes"* into *"map this datasheet onto the right ETIM class + valid values"* — far more reliable.
- *Touches:* `PRODUCT_CATEGORY` → ETIM class; spec fields → coded tuples; new `product-etim-schema.json` config; `do_enrich_product` emits only valid ETIM values (Akeneo's constrained-extraction pattern).
- *Caveat (flagged by research):* AV-manufacturer ETIM adoption is low/uneven, and AV has classes ETIM doesn't cover well — so allow a **namespace ETIM extension** for AV-specific classes (control processors, DSP, AV-over-IP) layered on the standard.

**A2. Field-level golden record with survivorship** *(extends our multi-source dedup)*
We already dedup on `(manufacturer, mfr_part_no)`. Make conflict resolution **per-field** with an explicit policy: **source-trust ranking** (manufacturer-verified > distributor feed > AI-derived > scraped) → **recency** → **confidence** as tiebreaker. Keep **field-level provenance** (which source won, and why) so the record is auditable.
- *Touches:* `PRODUCT` node gains per-field `{value, source, confidence, provenance_tier, won_at}`; new `product-survivorship.json` config; `do_ingest_source` + `do_enrich_product` apply survivorship; the `product_source_id` we already track becomes the provenance key.

**A3. Two-score quality model + confidence gate** *(Icecat completeness + Akeneo A–E)*
Compute **(a) completeness** per target channel (quote vs customer datasheet vs System-Design needs) and **(b) a quality/confidence grade** (consistency, units valid, provenance). Roll up a **per-manufacturer "data health" score**. Make enrichment **show the specific failing criteria**, not just a number, and gate "trusted" status on a per-channel threshold. This is a direct extension of our `needs_review` + review queue.
- *Touches:* `do_enrich_product` returns completeness + grade; `product_enrichment_log` stores both; new `GET /api/product/{id}/quality` + a manufacturer data-health rollup on `GET /api/product/sources`.

**A4. Claude confidence method — NOT logprobs** *(critical implementation correction)*
Anthropic models **do not expose token logprobs**. Our `confidence` fields therefore must come from **verbalized/self-rated confidence + self-consistency (ensemble voting) + an optional separate judge model** — never logprob reading. Bake this into the enrichment contract so we don't design around a signal Claude doesn't emit.
- *Touches:* `do_enrich_product` confidence computation; documented as an invariant alongside ADR-0017.

### Tier B — design for (build when the trigger arrives)

**B1. Compatibility / accessory knowledge graph with typed, AI-proposed edges** *(turns single-SKU enrichment into BOM intelligence)*
Expand our current `accessory_of | warranty_for | mounts | replaced_by` edges into a richer **typed compatibility graph**: `requires(license|power)`, `compatible_mount(VESA 400×400)`, `needs_cable(type)`, `controlled_by`, `replaces`/EOL-successor, and the **substitute vs complement** split. The LLM **proposes** edges from spec text ("supports VESA 400×400", "RS-232 control", "PoE+"), each **confidence-scored and human-confirmable**.
- *Why:* this is what XTEN-AV (accessory suggestion) and D-Tools **lack as an open, reasoned graph** — and it's what makes one-SKU enrichment compound into BOM/accessory intelligence for System Design + Procurement.
- *Touches:* `do_related_products` (now graph-backed + AI-proposed), new edge types, confidence per edge; feeds System Design's "audit this design for missing accessories/cables".

**B2. EOL/lifecycle as an unserved AV niche** *(Cisco EoX pattern + LLM-extract the rest)*
Per-SKU EOL lookup: hit EoX-style APIs where they exist (Cisco), and everywhere else **LLM-extract EOL/EOS dates + replacement SKU from the vendor's discontinuation PDF on demand**, confidence-scored with a source link. AV EOL today is manufacturer email blasts + PDFs — no aggregator covers pro-AV.
- *Touches:* sharpens the existing EOL/EOS **Watcher** + `replaced_by` edges; per-adapter `eox`-style hook; `do_enrich_product` can fill `lifecycle_status` + successor on demand.

**B3. BOM gap-audit (XAVIA's killer move), as on-demand + scoped**
A `do_audit_design` capability: given a design's product set, validate completeness and **propose missing complements** (mount, cable, license, PSU) — *on-demand, scoped to that design*, never a bulk sweep. Built on B1's compatibility graph.
- *Touches:* new core fn bridging Product ↔ System Design; A2A-served to System Design(6).

**B4. Delta-index sync with explicit removal flags** *(Icecat daily.index pattern)*
Formalize each source adapter's pull as **full snapshot + daily delta with explicit `REMOVED` flags** (we already soft-delete vanished rows; this makes it a first-class, auditable delta contract). Downstream consumers (and the `d365|both|nce`-style source modes) get clean deltas.
- *Touches:* `do_ingest_source` delta contract; source ingest state.

### Tier C — later / opportunistic

- **C1. Rich media as first-class** (Icecat asset ladder + 360°/3D/video; AV sells on it): auto-derive a resolution ladder on ingest, store assets as structured objects (type/dims/size/expiry) not loose URLs; prioritize rack/connector diagrams + 3D for AV.
- **C2. Per-channel requirement profiles + "publish to an AI agent" channel** (Salsify): validate content against the target channel's requirements before it ships; treat an LLM/agent endpoint as a syndication channel (we get this largely free via MCP).
- **C3. Personalized catalog** (D-Tools): any product used in a quote/design auto-promotes into the namespace's curated catalog with enriched, confirmed data — so repeat use compounds.
- **C4. Drop-in web component** (Icecat Live): a datasheet widget for customer-facing proposal microsites (Sales/TilbudKunde) — later, front-end work on our REST surface.
- **C5. Marketing copy generation** from specs — but **respect ADR-0017** (cost/margin never leave to a third-party API) and the `02` boundary that customer-facing sales copy belongs to **Sales**, not Product. Product generates neutral spec-derived descriptions only.

---

## 4. Net changes to fold into `02-product-engine.md`

Load-bearing (Tier A) — should update the spec now: **A1 ETIM-coded schema**, **A2 field-level golden record/survivorship**, **A3 two-score quality + gate**, **A4 Claude verbalized-confidence (not logprobs)**. The rest stay here as the idea backlog and graduate when their trigger arrives. A short "Research-informed direction" pointer is added to `02` referencing this doc.

---

## 5. Honest flags (don't over-trust the marketing)
- XTEN-AV's "live manufacturer sync / real-time pricing / EOL alerts" are **marketing-only, unverified**.
- Icecat ETIM-native classification is **unconfirmed** (it's UNSPSC-centric); its AI image-recognition / PDF spec-extraction and AI quality-scoring are **not clearly documented** (completeness scoring appears rule-based).
- Compatibility-graph evidence is from general e-commerce/electronics; the **pro-AV application is our inference**, not an observed product.
- AV-manufacturer ETIM adoption specifically is **likely low** — hence the namespace-extension caveat in A1.

---

## Sources

**Icecat:** [OCI XML repositories](https://iceclog.com/open-catalog-interface-oci-open-icecat-xml-and-full-icecat-xml-repositories/) · [index batch processing](https://iceclog.com/manual-icecat-product-xmls-batch-processing/) · [JSON API](https://iceclog.com/manual-for-icecat-json-product-requests/) · [Icecat Live JS](https://iceclog.com/icecat-live-real-time-product-data-in-your-app/) · [completeness/data-health score](https://iceclog.com/completeness-score/) · [taxonomy](https://icecat.com/product-taxonomy/) · [UNSPSC multi-version](https://iceclog.com/new-multi-version-support-of-unspsc-categorization-in-icecat/) · [Generative/Agentic AI](https://icecat.com/generative-ai/) · [matching tips](https://iceclog.com/8-tips-for-successful-integration-of-icecat-data/) · [subscription plans](https://iceclog.com/icecat-subscription-plans/)

**PIM landscape & standards:** [Akeneo data quality](https://help.akeneo.com/serenity-take-the-power-over-your-products/serenity-improve-data-quality) · [Akeneo May 2025 (constrained AI extraction)](https://help.akeneo.com/2025/may-2025-serenity-updates) · [Akeneo quality grade bands](https://webkul.com/blog/akeneo-product-quality-score/) · [Salsify agentic PXM](https://www.globenewswire.com/news-release/2025/10/15/3167047/0/en/Salsify-AI-Transforms-PXM-Ops-for-Digital-Shelf-Agentic-Commerce.html) · [inriver AI + syndication](https://www.inriver.com/2025/10/inriver-raises-the-bar-for-product-data-control/) · [Pimcore ETIM/ECLASS](https://pimcore.com/en/resources/blog/etim-eclass-and-co.-what-you-should-know-about-classification-standards) · [Syndigo acquires 1WorldSync](https://syndigo.com/news/syndigo-acquires-1worldsync/) · [GDSN data pool](https://1worldsync.com/platform/syndication/gdsn/) · [ETIM model](https://www.etim-international.com/classification/model-information/) · [ETIM classes/features (WisePIM)](https://wisepim.com/guides/product-taxonomy/etim) · [Profisee golden record + survivorship](https://profisee.com/solutions/initiatives/matching-and-survivorship/)

**AV-specific & AI enrichment:** [XTEN-AV XAVIA](https://xtenav.com/xavia/) · [D-Tools Integrated Product Library](https://www.d-tools.com/integrated-product-library) · [D-Tools + ADI real-time pricing](https://www.d-tools.com/resource-center/news-events/d-tools-collaborates-with-adi-to-offer-fully-integrated-real-time-product-pricing-information-to-streamline-proposal-generation-for-the-av-industry) · [Crestron Spec Sheet Collection](https://www.crestron.com/Support/Tools/Applications/Spec-Sheet-Collection) · [AVIXA standards](https://www.avixa.org/standards) · [ETIM integrated into GS1 GDSN](https://www.etim-international.com/etim-classification-is-now-integrated-into-gdsn-gs1-global-data-synchronisation-network/) · [Cisco EoX API](https://developer.cisco.com/docs/support-apis/eox/) · [LLM schemas for extraction (Willison)](https://simonwillison.net/2025/Feb/28/llm-schemas/) · [Cleanlab TLM structured-output confidence](https://cleanlab.ai/blog/tlm-structured-outputs-benchmark/) · [Walmart Retail Graph](https://medium.com/walmartglobaltech/retail-graph-walmarts-product-knowledge-graph-6ef7357963bc) · [Consumer-electronics product KG (MDPI)](https://www.mdpi.com/2071-1050/13/4/1722)
