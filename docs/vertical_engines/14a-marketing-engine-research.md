# 14a — Marketing Engine: AI-Marketing-Systems Research & Idea Backlog

**Status:** research companion to `14-marketing-engine.md` (spec, Tier 4 — builds LAST) · **Date:** 2026-06-17
**Question asked:** *"research modern AI-driven marketing systems to inform the Marketing engine of an AI-native backend suite for a B2B AV-integrator."*
**Method:** cited web investigation across six fronts — AI marketing platforms/agents; generative content + brand voice; the SEO→AEO/GEO shift; B2B "experience-as-marketing" + case-study/testimonial automation; attribution; and the risk/consent surface. Sources at the bottom. Marketing-hype vs verified flagged in §8.

---

## 1. Strategic read — where we play

The whole market is converging on **agentic marketing**: an AI agent takes a brief and *assembles segments, copy, and journeys itself*, then optimizes live. HubSpot Breeze, Salesforce Agentforce, Adobe GenStudio, and Jasper all now ship some version of "describe the campaign, the agent builds it." That convergence is real and worth learning from — **but it points the wrong way for us.**

The incumbents are all **demand-generation engines**: they manufacture outbound *volume* (more emails, more ads, more posts, more personalized landing pages) and bolt brand-voice governance on afterward to keep the volume on-brand. Their input is a marketer's brief; their risk is slop at scale.

**Our wedge is the inverse, and it falls straight out of the `14` thesis ("the customer experience IS the marketing"):**
1. **Marketing is a pure consumer of the cognitive graph, not a content factory.** Our input is not a brief — it is *delivered reality*: a PROJECT that landed well, a Support NPS spike, a won DEAL. We don't generate marketing volume; we *harvest* a small number of true stories the rest of the suite already produced. No competitor has the upstream graph; they buy/scrape third-party data (Clay) or ingest survey responses (UserEvidence). We already own the outcome data as a byproduct of doing the work.
2. **Generative but structurally human-gated.** Every platform here treats the human approval step as a *workflow convenience* ("cut review cycles from 6 to 1" — Jasper). For us it is a **structural invariant**: there is no autonomy ceiling that unlocks auto-publish of customer content (`14` §Sensitivity). The FTC's 2025–2026 enforcement wave (§6) makes that the *correct* posture, not a limitation.
3. **MCP/A2A-native, both directions.** We consume the graph over A2A *and* our published content can itself be AEO-optimized and agent-queryable — the same structural advantage the Product Engine has (`02a` §1). Everyone else is *retrofitting* AI-readability onto a CMS; we can emit structured, schema-rich, citable content from day one.

> **Positioning in one line:** *Not a campaign-generation machine — an evidence-harvesting engine that turns delivered project outcomes into a small number of true, consent-cleared, human-gated, AI-citable stories.*

**Honest framing:** this engine is greenfield, builds LAST, and produces **nothing** until Project(7), Support(10), System Design(6), and Sales(5) are live and producing outcome data. Everything below is a backlog to graduate when that data exists — not a near-term build.

---

## 2. AI marketing platforms / agents — what to borrow from each

| Platform | What it is | The 1–2 standout AI capabilities worth stealing |
|---|---|---|
| **HubSpot Breeze** | 4 core agents (Content, Social, Customer, Prospecting) over the Smart CRM | **Content Agent writes case studies + blogs in brand voice grounded in CRM data** — exactly our move, but their grounding is CRM fields; ours is the richer delivered-project graph. **Marketplace of narrow agents** (Deal-Loss, Customer-Health, RFP) — the named-single-purpose-agent pattern (mirrors our `do_*` Watcher/Advisor/Actor split). |
| **Salesforce Agentforce Marketing** (Marketing Cloud Next) | Agentic platform: brief → segmentation + emails + journeys, self-optimizing | **Campaign-from-a-brief assembly** and **always-on real-time optimization**. *Steal the assembly idea, reject the autonomy* — their campaigns "assemble and optimize themselves" with no human gate; that is precisely the path we forbid for customer content. |
| **Adobe GenStudio for Performance Marketing** | Enterprise content supply chain: plan→create→manage→activate→measure | **Brand guidelines (fonts, tone, channel rules) uploaded once and enforced across every generation** via fine-tuned + third-party models → our `marketing-brand-voice.json`. **Explicitly third-party-LLM-pluggable** (Firefly, Azure OpenAI, WRITER Palmyra) — validates a bring-your-own-LLM posture. **Content Production Agent**: upload a plan → on-brand assets — the structured-input→drafted-asset shape we already have (graph in, draft out). |
| **Jasper** | "Agentic marketing OS"; Jasper IQ brand-voice layer | **Brand voice + style guide + audience + product knowledge embedded into every output, admin-set once** → our brand-voice config. **LLM-agnostic / bring-your-own-LLM** + role/access/publishing-rights controls. **Drafts flow into existing human review by default** — confirms the human-in-loop gate is industry-standard, not exotic. |
| **Writer (Palmyra)** | Enterprise generative platform, regulated-sector focus | **Governed generation for regulated content** (the "no unsubstantiated claims" discipline) — the closest analog to our banned-claims/required-disclaimer rules. |
| **Clay** | GTM data enrichment + AI outbound (Claymation agent, 75+ data providers, waterfall enrichment) | **Waterfall enrichment + AI research-then-personalize.** Relevant as *contrast*: Clay buys/scrapes external data to fake first-party knowledge of an account; **we already have first-party delivered-project truth** — the thing Clay's whole product is trying to approximate. |
| **Mutiny** | No-code AI B2B website personalization + ABM microsites + LinkedIn ad orchestration | **AI-generated per-account microsites** — the "personalized proof page for this prospect's industry/size" idea. Ties to UserEvidence's finding that buyers trust **proof from similar customers** most (§4). A future Sales/proposal-microsite surface, not core Marketing. |

→ **idea for our Marketing engine:** keep the **named-single-purpose-agent** decomposition (already in `14` as `do_find_case_study_candidates` / `do_draft_case_study` / `do_suggest_content` / `do_audit_seo`, tagged Watcher/Advisor/Actor). Steal Breeze's **CRM-grounded content** and GenStudio's **brand-guidelines-uploaded-once** — both already map onto `marketing-brand-voice.json` + the graph. **Reject** Agentforce's self-assembling/self-optimizing autonomy at the publish boundary: adopt the *assembly* of a draft, never the *firing* of it.

---

## 3. Generative content + brand voice (from structured data)

The mature pattern across GenStudio, Jasper IQ, and Writer is identical and worth copying wholesale:
- **A brand-voice model is config, not a prompt afterthought** — tone rules, style guide, banned claims, required disclaimers, audience profiles are uploaded/admin-set once and *every* generation inherits them. (We already have this as `marketing-brand-voice.json`.)
- **Structured data in → on-brand draft out.** GenStudio's Content Production Agent (plan → assets) and Breeze's Content Agent (CRM → case study) both take structured input, not free prompts. Our input is strictly richer: a graph with design intent, BOM highlights, outcome metrics, room narrative.
- **Human review is the default terminal step**, universally. Jasper sells it as a speed win (6 cycles → 1); we treat it as a non-negotiable gate.
- **Bring-your-own / LLM-agnostic is now table stakes** (GenStudio multi-model, Jasper LLM-agnostic, Writer's own Palmyra). Confirms ADR-style "the model is swappable" is correct, and that cost/margin-sensitive data should stay governed.

→ **idea for our Marketing engine:** our `do_draft_case_study` should follow the **constrained-generation** discipline the Product Engine adopted (`02a` A1): the LLM's job is *"narrate these graph facts on-brand,"* not *"invent a customer success story."* Pull brand-voice exemplars + similar prior approved studies from `memories` (already `14` §B5) so drafts stay on-voice — this is the same cognitive-recall loop, applied to narrative instead of attributes. Add **banned-claims + required-disclaimer enforcement** as a hard check inside the draft path (Writer's regulated-content posture), not just a tone hint.

---

## 4. SEO → AEO/GEO (2025–2026) — the citation shift

The ground has moved. The 2025–2026 consensus:
- **The metric is now "are you cited by the answer engine," not "do you rank."** AEO = structuring content so AI platforms (ChatGPT, Perplexity, Copilot, Claude, Google AI Mode/Overviews) *select you as a cited source*; GEO = optimizing for synthesized/blended generative answers.
- **The economics are real and verified-ish:** Ahrefs (Dec 2025, 300k keywords) found **position-1 CTR drops 58% when an AI Overview is present** — classic SEO traffic is being eaten. Counterweight: AI-referred visitors reportedly **convert ~4.4× and spend 68% more time** (vendor data — treat as directional), because they arrive later in the research journey. For a B2B firm with a long, considered buying cycle, that higher-intent traffic is exactly the right shape.
- **Structured content is the mechanism.** The emerging stack: `llms.txt` (step one), **JSON-LD structured fact sheets** (Organization, Service, Review schema), and machine-readable content anchors that reduce LLM hallucination by giving "concrete data anchors." Schema is "what AI reads before it reads your content." MCP itself is named as part of this architecture (~97M monthly SDK downloads cited for 2026).
- **Adoption gap = opportunity:** ~70% of orgs expect AEO to matter, only ~20% have started.

→ **idea for our Marketing engine:** this is our sharpest structural edge and it should be folded into `do_audit_seo` — **rename/reframe it as an AEO/GEO audit, not classic SEO.** Two concrete moves:
1. **Emit our own content as structured, schema-rich, AI-citable artifacts.** A drafted/approved `CASE_STUDY` already *is* structured graph data (challenge → design → BOM → outcome metrics → room). Publishing it with JSON-LD (Service/Review/Organization schema) and stable anchors makes it citation-ready by construction — we are not retrofitting schema onto prose, we are *projecting graph facts into schema*. `content_assets.seo jsonb` already exists for this.
2. **Make our content agent-queryable over MCP.** Because the suite is MCP-native, our approved public content can be exposed as a queryable surface — the same "publish to an AI agent / LLM endpoint as a first-class channel" pattern Salsify pioneered for product data (`02a` C2). This is the AEO endgame: not just *findable* by answer engines, but *directly queryable* by agents.
*Caveat (honest):* AEO measurement is immature and partly vendor hype; "4.4× conversion" is unverified. Treat AEO as **structural readiness we get nearly for free from being graph- + MCP-native**, not as a measurable channel to over-invest in yet.

---

## 5. B2B "experience-as-marketing" + advocacy automation

This is the section that most directly validates the `14` thesis — the market has independently arrived at our exact model.
- **Customer-advocacy software's core loop:** *"trigger the right ask (review, quote, reference, case study) at the moment of proven value, through the right channel,"* surfacing the most satisfied customers **using NPS, product-usage, and engagement signals** to ask the right person at the right time. That is *precisely* `14`'s high-NPS-triggered `do_request_testimonial` and outcome-scored `do_find_case_study_candidates` — independently arrived at by Influitive, Base, Krowdbase et al.
- **Case-study/proof automation is a real category** (UserEvidence, $9M Series A): survey feedback / Gong call transcripts / G2 reviews → verified case studies, testimonials, and stat-proof "in minutes," published **on-brand and segment-tailored** (by company size, industry, geography). 
- **Two findings that directly back our defaults:**
  - **"Proof from similar customers" is the #1 buyer factor (78%)** — argues for *segment-tailored* drafting (industry/size/room-type), which our graph supports natively (we know the SITE/ROOM/vertical).
  - **Blind-but-verified testimonials carry near-equal trust to named ones (60% vs 64%)** — this is a strong external validation of our **anonymize-by-default** posture (`14` `NCE_MARKETING_DEFAULT_ANONYMIZE=true`): we lose almost no persuasive power by masking the customer, and we sidestep the entire consent/disclosure risk surface until consent is explicitly secured.
- **Referral/advocacy ROI** (vendor stats, directional): referred customers ~16% higher LTV, ~18% more loyalty; ~84% of B2B buyers weight peer recommendations.

→ **idea for our Marketing engine:** we have the **structural advantage these tools simulate.** UserEvidence has to *survey* customers and *mine* Gong transcripts to reconstruct proof; we read it straight from PROJECT outcomes, Support NPS, and won DEALs — **zero net-new operational data** (the `14` mission). Concrete adds:
- **Segment-tailored drafting** inside `do_draft_case_study`: because we know room type / vertical / system class from the graph, generate the study *framed for similar prospects* (the 78% finding) — and let Sales/Mutiny-style microsites pull the right proof per account later.
- **Lean into anonymize-by-default as a feature, not a fallback** (the 60%-vs-64% finding): the default path produces a fully usable, low-risk, blind-but-verified study; naming is the deliberate, consent-gated exception.
- **Treat "verified" as the load-bearing word.** Our edge over survey-based proof is that every claim is **traceable to graph evidence** (a real delivered project, a real recorded NPS) — store that provenance link on the `CASE_STUDY`/`TESTIMONIAL` node so any published claim is auditable back to source (mirrors `02a` field-level provenance).

---

## 6. Attribution + measurement — what's realistic

Sobering and useful for scoping:
- **Even sophisticated multi-touch attribution captures only ~30–40% of the real B2B buyer journey.** Models *distribute credit*, they don't *explain impact*, and they treat touches as equal regardless of timing/intent/stage.
- **Data-volume floors are brutal for a firm our size:** GA4 data-driven attribution needs **≥600 conversions and ≥15,000 ad interactions/month** or it silently falls back to last-click. A B2B AV-integrator will never hit those volumes.
- **2025 best practice has retreated to "directional":** directional attribution + AI correlation + market intelligence for ~80–90% *directional* accuracy, explicitly abandoning the dream of precise per-touch credit.

→ **idea for our Marketing engine:** **do not build a multi-touch attribution model — we lack the volume by an order of magnitude, and the spec rightly says Marketing feeds nothing downstream operationally.** What *is* realistic and on-brand: a **directional, evidence-linked measure** — tie an approved `CASE_STUDY`/`TESTIMONIAL` back to the PROJECT/DEAL it came from and surface simple counts ("N candidates, M pending testimonials, K published this quarter") into the #19 morning-brief slice (already `14` §A2A). Resist any vendor pitch for "marketing ROI attribution"; for us the honest measure is *throughput of true stories harvested*, not last-touch revenue credit.

---

## 7. The risks — why human-gated publishing is non-negotiable

The 2025–2026 regulatory and reputational picture makes the `14` sensitivity rules look prescient, not paranoid:
- **FTC enforcement is now live on exactly our content type.** First actions against AI-generated *testimonials* landed late 2024; updated Endorsement Guides covering synthetic media/AI personas by mid-2025; warning letters for fake/AI consumer reviews in Dec 2025; "AI Transparency in Advertising" rules finalizing early 2026. Undisclosed AI-generated content is treated as **material misrepresentation** when consumers would expect real people/real experiences.
- **Consent must be explicit, written, and scoped.** For a real customer's likeness/words: written permission covering **AI generation, scope, duration, territory, and revocation.** "Real customers" claims require retained evidence (records, verification, consent forms).
- **A disclosure cannot rescue an unsubstantiated claim** — it only clarifies source. So hallucinated capability/outcome claims in a case study are a hard failure regardless of any "AI-assisted" label.
- **AI slop = brand damage.** Treating synthetic praise as "creative copy" invites both enforcement and reputational hit.

→ **idea for our Marketing engine:** the spec's posture is correct and should be *hardened, not softened*. Specifically:
- **Consent state is a first-class, structured field with a recorded timestamp + scope** (already `testimonials.consent` / `consent_recorded_at`) — extend it to capture *scope/duration/revocation* per the FTC requirement, not just a boolean. `do_publish_content` hard-refusing without it is the right structural guarantee.
- **No-hallucinated-claims is a publish-gate check, not a tone preference.** Every factual claim in a draft must resolve to graph evidence (the §5 provenance link); a claim with no backing node blocks approval. This is the marketing analog of the Product Engine's confidence gate (`02a` A3) — *if it isn't in the graph, it can't be in the case study.*
- **Disclosure where AI-generated content is material** — bake a configurable disclosure rule into `marketing-brand-voice.json`.
- **Right-to-retract stays a hard retire** via `marketing_source_id` (already `14`) — now also a *compliance* feature (revocation terms), not just hygiene.
- **The absence of an Autonomous tier is the single most defensible design choice in this engine.** Agentforce's self-publishing campaigns are a liability for *customer* content; our structural human gate is exactly what the FTC posture rewards. Keep it non-tunable.

---

## 8. Honest flags (hype vs verified)
- **"Agentic, self-assembling, self-optimizing campaigns"** (Agentforce, GenStudio Content Production Agent) are **real products** but heavily **marketing-framed**; the autonomy is for *demand-gen volume*, and is the wrong model for customer-facing proof content. Verified that they exist; unverified that the autonomy is safe for our use.
- **AEO conversion stats** ("4.4× conversion, +68% time on site") are **vendor data, directional at best.** The Ahrefs 58%-CTR-drop figure is the most credible AEO datapoint.
- **Advocacy/referral ROI numbers** (16% LTV, 71% higher referral conversion, 84% peer-trust) are **vendor-sourced** — directionally consistent across multiple vendors but not independent research.
- **UserEvidence's 60%-vs-64% blind-vs-named trust** and **78% "similar customers"** are from their own 2025 research — self-interested but specific and plausible; treat as the strongest external support for anonymize-by-default and segment-tailoring.
- **MCP "97M monthly SDK downloads / OpenAI+Google+Microsoft adoption"** is consistent with the broader MCP momentum we already bet on (`02a`); credible.
- **Our AEO/agent-queryable edge is an inference**, not an observed competitor product — no incumbent yet exposes approved marketing content as an MCP-queryable, schema-projected surface. That's the open lane.

---

## 9. Net guidance for `14-marketing-engine.md`
The spec is already well-aligned with the market — most findings *confirm* it. The few additions worth folding in:
- **A. Reframe `do_audit_seo` → AEO/GEO** (§4): emit case studies as JSON-LD/schema-rich, AI-citable artifacts; expose approved content as an MCP-queryable channel. Highest-leverage, near-free given our architecture.
- **B. Segment-tailored, anonymize-by-default drafting** (§5): use graph-known room/vertical/size to frame studies for "similar customers"; lean on blind-but-verified as the safe default.
- **C. Provenance link on every claim + no-hallucinated-claims publish gate** (§5, §7): every factual statement resolves to a graph node, or approval blocks — the marketing analog of the Product Engine confidence gate.
- **D. Consent as structured scope/duration/revocation**, not a boolean (§7), to match FTC 2025–2026 requirements.
- **E. Drop any multi-touch attribution ambition** (§6); measure directional throughput of harvested stories only.
Everything else (Watcher/Advisor/Actor split, brand-voice config, human gate, memories recall loop, no-Autonomous tier) is validated as-is. This stays a backlog until Tier 1–3 engines produce the data.

---

## Sources

**AI marketing platforms / agents:** [HubSpot Breeze Spring-2025 four agents](https://www.hubspot.com/company-news/spring-2025-spotlight-breeze-agents) · [Breeze AI Agents product page](https://www.hubspot.com/products/artificial-intelligence/breeze-ai-agents) · [Breeze 2026 SMB guide](https://www.onthefuze.com/hubspot-insights-blog/hubspot-breeze-ai-agents-2026) · [Salesforce Agentic Marketing / Marketing Cloud Next](https://www.salesforce.com/marketing/agentic-marketing/) · [Salesforce AI agents for marketing](https://www.salesforce.com/ap/marketing/ai/ai-agents-for-marketing/) · [Marketing Cloud Next vs alternatives (Bloomreach)](https://www.bloomreach.com/en/blog/salesforce-marketing-cloud-next) · [Adobe GenStudio content supply chain expansion (Mar 2025)](https://news.adobe.com/news/2025/03/adobe-expands-genstudio-content-supply-chain) · [GenStudio for Performance Marketing — creation](https://business.adobe.com/products/genstudio/performance-marketing/creation.html) · [Jasper brand voice](https://www.jasper.ai/brand-voice) · [Jasper enterprise / responsible AI](https://www.jasper.ai/enterprise) · [Clay — enrich with AI](https://www.clay.com/blog/enrich-with-ai) · [Best AI platforms for GTM (Demandbase)](https://www.demandbase.com/blog/ai-platform-for-gtm-teams/)

**SEO → AEO/GEO:** [Jasper — GEO vs AEO vs SEO 2026](https://www.jasper.ai/blog/geo-aeo) · [HubSpot — AEO trends 2026](https://blog.hubspot.com/marketing/answer-engine-optimization-trends) · [Frase — complete AEO guide / getting cited by AI](https://www.frase.io/blog/what-is-answer-engine-optimization-the-complete-guide-to-getting-cited-by-ai) · [Profound — AEO marketer playbook 2025](https://www.tryprofound.com/resources/articles/answer-engine-optimization-aeo-guide-for-marketers-2025) · [llms.txt was step one — next architecture (Forrester)](https://duaneforresterdecodes.substack.com/p/llmstxt-was-step-one-heres-the-architecture) · [Schema App — structured data not tokenization](https://www.schemaapp.com/schema-markup/why-structured-data-not-tokenization-is-the-future-of-llms/) · [Bracket — structured data in 2026](https://bracketmedia.com/blog/structured-data-in-2026-what-ai-is-reading-before-it-reads-your-content)

**B2B advocacy / case-study & testimonial automation:** [UserEvidence — best B2B case-study software 2026](https://userevidence.com/blog/b2b-case-study-software-tools/) · [UserEvidence — case studies → customer evidence 2026](https://userevidence.com/blog/case-studies-had-their-run-in-2026-its-going-to-be-all-about-customer-evidence/) · [UserEvidence + G2 integration](https://userevidence.com/blog/userevidence-and-g2-launch-integration-to-turn-reviews-into-actionable-customer-evidence/) · [Influitive — customer advocacy software](https://influitive.com/blog/customer-advocacy-software/) · [HubSpot — best customer advocacy platforms 2026](https://blog.hubspot.com/service/customer-advocacy-platforms) · [Mutiny (via Demandbase GTM roundup)](https://www.demandbase.com/blog/ai-platform-for-gtm-teams/)

**Attribution:** [RevSure — beyond last-click MTA in B2B](https://www.revsure.ai/blog/beyond-last-click-multi-touch-revenue-attribution-in-b2b) · [HockeyStack — B2B MTA models/challenges](https://www.hockeystack.com/blog-posts/b2b-multi-touch-attribution) · [DOJO AI — why MTA models fail](https://www.dojoai.com/blog/multi-touch-attribution-explained-the-complete-guide-for-b2b-marketing-and-why-they-fail) · [Factors — MTA pros and cons](https://www.factors.ai/blog/multi-touch-attribution-pros-and-cons)

**Risk / consent / disclosure:** [DLA Piper — FTC warning letters, AI consumer reviews (Dec 2025)](https://www.dlapiper.com/en-us/insights/publications/2025/12/ftc-warning-letters-ai-consumer-reviews) · [Holland & Knight — FTC deceptive AI claims](https://www.hklaw.com/en/insights/publications/2025/06/ftc-evaluating-deceptive-artificial-intelligence-claims) · [FTC AI content disclosure rules 2026 (ppl.studio)](https://ppl.studio/blog/ai-generated-content-disclosure-ftc-guidelines) · [FTC AI testimonial disclosure 2025](https://www.influencers-time.com/ftc-guidelines-for-disclosing-ai-generated-testimonials-in-2025/)
