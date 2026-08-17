> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 14 — Marketing Engine  (nce/vertical_modules/marketing)

**Status:** spec (Tier 4 — Revenue axis, builds LAST) · **Owner:** NCE core (Sindre)
**Pattern companions:** `docs/VERTICAL_MODULE_PATTERN.md`, `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§4 graph catalogue, §7 spec format)

## Mission
Turn the work every other engine already did into outbound marketing — with **zero net-new operational data**. The thesis (Andreas module 02): *"Bravo gjør ikke marketing — Bravo ER marketing"* — the customer experience itself is the marketing, so the job is not to *invent* a brand voice but to *harvest* one from delivered reality. This is the one engine classified **primarily generative**: it is a pure **consumer** of the graph that **produces content artifacts** for human review, exactly like the #19 Executive aggregate is a pure consumer that produces a briefing. The deep-AI angle: when a PROJECT lands well, the engine drafts a case study from the graph (design intent, BOM highlights, outcome metrics) without anyone retyping it; when Support flags a happy customer, the engine times a testimonial request to that moment. Greenfield — Bravo measures **zero** marketing KPIs today and the website is static/outdated, so there is no legacy to migrate, only signal to mine. The hard discipline that defines this engine: **it drafts, a human publishes** — customer-facing content is never auto-published.

## Research-informed direction (see `14a-marketing-engine-research.md`)
A scan of AI-marketing systems (HubSpot Breeze, Salesforce Agentforce, Adobe GenStudio, Jasper, Clay, Mutiny, advocacy tools UserEvidence/Influitive) mostly **confirmed** this spec — the market independently arrived at our model (advocacy tools trigger the proof-ask at the moment of proven value via NPS). Positioning: *not a campaign-generation machine — an **evidence-harvesting engine** that turns delivered project outcomes into a small number of true, consent-cleared, human-gated, AI-citable stories.* Incumbents are demand-gen volume machines that bolt brand-voice governance on afterward; we are the inverse, and we own the upstream graph they try to approximate by buying/scraping (Clay) or surveying (UserEvidence). Additions to fold:
- **A — reframe `do_audit_seo` → AEO/GEO** (the sharpest near-free edge). The metric is now *"are you cited by the answer engine,"* not *"do you rank"* (Ahrefs: position-1 CTR −58% under an AI Overview). A drafted/approved `CASE_STUDY` already **is** structured graph data → publish it as **JSON-LD/schema-rich, AI-citable** artifacts (project `content_assets.seo`), and **expose approved content as an MCP-queryable channel** (we're MCP-native; the same "publish to an AI agent" pattern as Product 02a C2). We *project graph facts into schema*, not retrofit schema onto prose.
- **B — segment-tailored, anonymize-by-default drafting.** The graph knows room-type/vertical/size, so `do_draft_case_study` frames the study for *similar prospects* (**"proof from similar customers" = the #1 buyer factor, 78%**). Lean into **anonymize-by-default as a feature**: blind-but-verified testimonials carry near-equal trust (60% vs 64% named) while sidestepping the consent/disclosure risk surface.
- **C — provenance link + no-hallucinated-claims publish gate.** Every factual claim in a draft **must resolve to a graph node, or approval blocks** — the marketing analog of Product's confidence gate (02a A3): *if it isn't in the graph, it can't be in the case study.* Store the evidence link on the `CASE_STUDY`/`TESTIMONIAL` node ("verified" is the load-bearing word).
- **D — consent as structured scope/duration/revocation, not a boolean** (FTC 2025–2026: first actions against AI-generated testimonials; undisclosed AI content = material misrepresentation). Extend `testimonials.consent` to capture scope/duration/revocation; `do_publish_content` hard-refuses without it; right-to-retract = hard retire via `marketing_source_id`.
- **E — drop any multi-touch attribution ambition.** We miss GA4's data-driven-attribution volume floor (≥600 conv / ≥15k interactions/mo) by an order of magnitude; measure **directional throughput of harvested stories** (candidates / pending / published) into the #19 brief — not last-touch revenue credit.
- **Validated as-is:** the Watcher/Advisor/Actor split, brand-voice config, recall loop, and especially the **absence of an Autonomous tier** — FTC enforcement makes the structural human gate the *correct* posture, not a limitation. Keep it non-tunable.

## Inspiration & triage
- **Andreas source (concept, not code):** module map `04-virksomhets-modulkart.md` §02 Marketing — case-study-utkast, testimonials, brand-assets, thought-leadership-drypp, SEO. Listed as **ikke-startet (greenfield)** and flagged as one of the largest *nytenknings-områder* for the peer. There is **no Portal sidecar to lift** and **no Lysning page** today (#14 is "new" in roadmap §3) — this engine is built from the graph, not ported.
- **Adjacent signal sources (consumed, not lifted):** Project(7) delivered-project + outcome data, Support(10) customer-health/NPS, System Design(6) notable designs, Sales(5) won deals. The testimonial-timing idea overlaps Andreas module 15 (Customer Satisfaction) — Marketing *reads* the health score, it does not own it.
- **Lysning page served:** none yet — a future "Marketing / Content" admin surface consumes the no-model REST routes (review queue, asset library). Out of scope for the first build beyond the REST contract.

## Classification
**generative.** No external system to sync from and no inbound transport — the input *is* the NCE cognitive graph, the output is content artifacts staged for human approval. There is therefore **no `client.py` / `auth.py` / `webhooks.py`**; the engine is a `sync.py`-free, ingestion-light reader plus an LLM-drafting layer. Auth model: internal only (graph reads are namespace-scoped via RLS). Optional outbound publishing connectors (website CMS, SEO platform) are explicitly **deferred** and, when built, sit behind a `PublishTransport` adapter that **always requires a recorded human sign-off** before it fires — never an autonomous path.

## Graph contribution
Node `entity_type` prefixes: `MARKETING_*`, plus it *reads* shared spine nodes `PROJECT`, `CUSTOMER`, `PRODUCT`/`SKU`, `QUOTE`, `SITE`/`ROOM`.
- **Nodes:** `CASE_STUDY` (a drafted/approved study), `TESTIMONIAL` (a captured customer quote + consent state), `CONTENT_ASSET` (thought-leadership/blog/brand asset + SEO metadata).
- **Edges (the §4 contract, our slice):**
  - `PROJECT -[generates]-> CASE_STUDY` (the canonical Project→Marketing edge from §4)
  - `CUSTOMER -[gave]-> TESTIMONIAL` (with `confidence` = NPS strength at capture time)
  - `CASE_STUDY -[features]-> PRODUCT`/`DESIGN` (and `-[features]-> ROOM` for the room-centric narrative)
  - `TESTIMONIAL -[supports]-> CASE_STUDY` (a study can embed an approved quote)
- **memories/ledger:** approved case studies + brand voice exemplars → `memories` (embedding + `content_fts`) so future drafts recall *"how did we tell a similar room-system story"* — this is the brand-voice learning loop. Every draft/approve/publish decision → `v3_cognitive_ledger` (auditable consent + sign-off trail). Tag every derived row with `marketing_source_id` so a retracted/anonymised study hard-retires cleanly (D365 retirement pattern). **Marketing writes only `MARKETING_*` nodes** — it never mutates spine nodes it reads.

## Core functions
Generative `do_<action>(engine, params) -> dict`. The drafting cores read the graph and call the cognitive engine; **no function in this module publishes externally without a prior recorded approval**.
- `do_find_case_study_candidates(engine, params) -> dict` — namespace/period → delivered PROJECTs scoring well on outcome metrics (clean handover, high health, notable design); ranked, with the graph evidence each would draw on. Pure-ish read (Watcher surface).
- `do_draft_case_study(engine, params) -> dict` — `{project_id, anonymize?}` → a structured case-study draft assembled from the graph (challenge → design → BOM highlights → outcome metrics → room narrative), staged as a `CASE_STUDY` node in `status=draft`. Anonymises customer/site per the `anonymize` flag and the namespace consent policy. Actor (writes a draft node; never publishes).
- `do_request_testimonial(engine, params) -> dict` — `{customer_id, project_id}` → creates a `TESTIMONIAL` node in `status=requested` and emits a request task; gated on health/NPS threshold + a consent precondition. Actor.
- `do_capture_testimonial(engine, params) -> dict` — `{testimonial_id, quote, consent}` → records the returned quote **with explicit consent state**; cannot move to `approved` without consent=true.
- `do_suggest_content(engine, params) -> dict` — `{theme?|product?}` → thought-leadership / drip ideas grounded in real delivered work + failure-pattern learnings; Advisor.
- `do_audit_seo(engine, params) -> dict` — `{asset_id|url}` → SEO recommendations (metadata, keyword coverage, internal-link gaps) for a content asset. Advisor; read-only.
- `do_approve_content(engine, params) -> dict` — `{artifact_id, approver, decision}` → the **human-sign-off gate**: flips a draft to `approved`, writing the approver + consent confirmation to the ledger. Required before any publish path exists.
- `do_publish_content(engine, params) -> dict` — `{artifact_id}` → **transport-abstracted** publish via `PublishTransport` (`cms` | `manual`); **refuses** unless the artifact is `approved` *and* (for customer content) consent is recorded. The live CMS adapter is deferred 🔴; `manual` (export for a human to post) ships first.

## MCP tools
Registered in `nce/tool_registry.py` via `_h(...)` late-binding. AI-role tag per roadmap §2 taxonomy.

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `marketing_find_case_study_candidates` | ✔ | ✘ | ✘ | Watcher |
| `marketing_draft_case_study` | ✘ | ✔ | ✔ | Actor |
| `marketing_request_testimonial` | ✘ | ✔ | ✔ | Actor (Watcher-triggered) |
| `marketing_capture_testimonial` | ✘ | ✔ | ✔ | Actor |
| `marketing_suggest_content` | ✔ | ✘ | ✘ | Advisor |
| `marketing_audit_seo` | ✔ | ✘ | ✘ | Advisor |
| `marketing_approve_content` | ✘ | ✔ | ✔ | Actor (human gate) |
| `marketing_publish_content` | ✘ | ✔ | ✔ | Actor (sign-off-gated; never Autonomous) |

**No tool in this engine is Autonomous.** Publishing customer-facing content is always human-gated — there is no value/risk threshold that unlocks auto-publish, by design.

## REST routes
No-model path for the future Marketing/Content admin surface, the BFF, and scripts. Mounted via `build_app(extra_routes=...)`; HMAC/mTLS-authed in `nce/admin_handlers/marketing.py`:
- `api_marketing_candidates` (GET) — case-study-worthy projects (review queue).
- `api_marketing_draft_case_study` (POST) — generate a draft from a project.
- `api_marketing_testimonials` (GET) / `api_marketing_capture_testimonial` (POST) — testimonial pipeline + consent capture.
- `api_marketing_suggest_content` (POST) — content/drip ideas.
- `api_marketing_audit_seo` (POST) — SEO audit for an asset/URL.
- `api_marketing_approve_content` (POST) — the sign-off action (records approver + consent).
- `api_marketing_assets` (GET) — the brand-asset / content library listing.

## AI features
- **Watcher:** high-NPS testimonial trigger (when Support(10) health/NPS crosses the threshold for a customer → flag a testimonial moment); case-study-worthy project-completion detection (delivered + good outcome). Both observe + alert; they propose, they do not act unprompted.
- **Advisor:** thought-leadership/drip content suggestions grounded in real delivered work; SEO recommendations; "which delivered projects would make the strongest studies this quarter".
- **Actor (with human approval):** draft a case study; issue a testimonial request; **never auto-publish customer content without sign-off.** Every Actor write lands in a review/approval queue.
- **Autonomous:** **none.** Stated explicitly — publishing is always human-gated; there is no autonomy ceiling here.
- **Cognitive recall:** drafts pull brand-voice exemplars + similar past studies from `memories`, so the engine answers *"how have we narrated a comparable room/system before"* and stays on-voice.
- **Enrichment triggers (event-scoped, never a background sweep):** a draft is generated **only** when (a) a project is detected as case-study-worthy or a human requests one, and (b) a testimonial request fires **only** on a high-NPS event. No bulk content generation, no "draft a study for every project" sweep — the system-wide on-demand rule (roadmap §5).

## A2A flows
- **Consumes (leaf, inbound only):** queries Project(7) for delivered projects + outcomes, Support(10) for NPS/health, System Design(6) for notable designs, Sales(5) for won deals — composing them into a draft. These are read calls into other engines' tools.
- **Serves Morning-brief (#19 aggregate) lightly:** can expose "N case-study candidates / pending testimonials" as a small marketing slice of the executive query.
- **Feeds nothing downstream operationally.** Marketing is a graph **leaf**: its outputs are content artifacts for humans, not inputs other engines act on. (The one back-pressure it could create — "this delivered story shows an upsell angle" — belongs to Sales/Product's failure-pattern loop, not here.)

## Config keys
`NCE_MARKETING_*` in `nce/config.py`: `NCE_MARKETING_ENABLED`, `NCE_MARKETING_NPS_TESTIMONIAL_THRESHOLD` (the high-NPS trigger point), `NCE_MARKETING_CASE_STUDY_MIN_OUTCOME_SCORE` (worthiness gate), `NCE_MARKETING_REQUIRE_CONSENT` (default **true** — disabling is a deliberate, audited act), `NCE_MARKETING_DEFAULT_ANONYMIZE` (default **true**), `NCE_MARKETING_PUBLISH_TRANSPORT` (`manual` default; `cms` 🔴 deferred), `NCE_MARKETING_CANDIDATE_LOOKBACK_DAYS`. Namespaces opt in via `metadata.marketing.enabled = true`.
**Config-as-IP JSON (namespace-scoped, the business IP — NOT code):**
- `marketing-brand-voice.json` — tone/voice rules, banned claims, required disclaimers, anonymisation rules (what counts as PII to mask). Each tenant tunes its own brand.
- `marketing-content-templates.json` — case-study / testimonial / drip structures the drafter fills from the graph.

## Tables/migrations
**Graph-first** (CASE_STUDY/TESTIMONIAL/CONTENT_ASSET live as `kg_nodes`/`kg_edges`; consent + sign-off trail lives in `v3_cognitive_ledger`). Three own tables where status-state, consent, and binary assets need first-class storage beyond the graph — **all `ENABLE` + `FORCE ROW LEVEL SECURITY` + `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`**, mirrored into `schema.sql` + a numbered migration:
- `case_studies` — `id, project_id, title, body, status (draft|in_review|approved|published|retracted), anonymized bool, approver, approved_at, marketing_source_id, raw jsonb, created_at`.
- `testimonials` — `id, customer_id, project_id, quote, status (requested|received|approved|declined), consent bool, consent_recorded_at, nps_at_capture, marketing_source_id, created_at`.
- `content_assets` — `id, kind (case_study|testimonial|blog|brand|drip), ref_id, seo jsonb, storage_uri, status, marketing_source_id, created_at`.

## Sensitivity & consent (call-out)
Customer-facing content is **the** sensitivity boundary of this engine:
- **Explicit consent before publish.** A `TESTIMONIAL` or customer-named `CASE_STUDY` cannot reach `published` without `consent=true` recorded in the table **and** an `approve_content` sign-off in the ledger. `do_publish_content` hard-refuses otherwise.
- **Anonymisation by default.** `NCE_MARKETING_DEFAULT_ANONYMIZE=true` — drafts mask customer/site/PII per `marketing-brand-voice.json` unless a human explicitly de-anonymises *and* consent covers naming.
- **No auto-publish, ever.** There is no autonomy path that writes to the outside world; the human gate is structural, not a tunable threshold.
- **Right to retract.** Retracting consent flips status to `retracted` and hard-retires derived rows via `marketing_source_id`.

## Dependencies
- **Upstream engines (data producers, all must be live first):** Project(7) delivered projects + outcomes; Support(10) NPS/health for the testimonial trigger; System Design(6) notable designs; Sales(5) won deals. This is why Marketing is **Tier 4 / builds LAST** (roadmap §6) — it is a pure consumer and has nothing to draft until the others produce data, exactly like the #19 Executive aggregate.
- **Downstream:** none — it is a graph leaf; nothing consumes its output operationally.
- **External (deferred 🔴):** website CMS / SEO platform publish connectors — abstracted behind `PublishTransport` so the engine ships fully usable (draft / approve / `manual` export all work) without them.

## Review round-2 hardening (2026-06-17 — these govern the build)
This is the most-governed-by-design spec in the suite and the review **confirms** the posture (no-Autonomous-by-design + FTC-aligned consent are correct, not limitations). Four sharpenings:
1. **The no-hallucinated-claims gate must SHAPE generation, not post-hoc verify.** "Every claim resolves to a graph node or approval blocks" is right in principle, but *mechanically checking* that an LLM's fluent prose contains only graph-backed claims is itself a fallible AI task (you can't regex it). So `do_draft_case_study` is **retrieval-grounded assembly** — *pull fact → cite its graph node → template into prose* — the draft is **constructed from cited graph facts**, not freely generated then claim-checked. The gate **constrains generation**; otherwise "no hallucinated claims" stays aspirational. **(Generalises — roadmap §9.3 — to every generative output: Economy close-narrative, Project status-report, Support troubleshooter, Business-Insights board pack.)**
2. **Marketing is the widest data-EGRESS surface — redact at DRAFT-ASSEMBLY time, not publish.** A case study is assembled from real customer/site/margin data *before* anonymisation; if that draft is stored (`case_studies.body`) or shown in the review queue, un-consented PII/margin is **already exposed internally**. So: **anonymise at draft-assembly** (the draft never contains un-consented PII) and **margin/cost/internal fields NEVER enter a marketing draft at all** — via the same **allow-list** the Sales public-quote, Vendors partner-view, and Customer-Portal surfaces use. This makes allow-list field-redaction a **4-consumer pattern** (cross-ref §9.6 — RLS scopes rows, the allow-list redacts fields).
3. **Governance tension to reconcile: AI-citable publishing (AEO/GEO) vs right-to-retract.** Publishing case studies as JSON-LD + an MCP-queryable channel feeds *answer engines* — but content an LLM/crawler has ingested is **effectively irrevocable** (`marketing_source_id` hard-retire un-publishes our CMS copy, **not** what an AI already trained on). Therefore **consent for AI-citable content is a HIGHER, DURABLE bar** than consent for a retractable web page. Model **two consent tiers**: `web_retractable` vs `ai_citable_irrevocable` — the latter requires explicit, durable, can't-be-walked-back consent.
4. **Testimonial-timing reads Support NPS/health — the customer-data red line extends here.** Trigger **only** on the *high*-NPS positive signal; **never act on low health** (no "we noticed you're unhappy" outreach — that belongs to Support's care workflow, not Marketing). Marketing reads the positive trigger and nothing else.

> **Scope exemplar (hold it):** a true graph **leaf** — reads other engines, writes only `MARKETING_*`, never mutates the spine, no autonomy, feeds nothing downstream. Its **Tier-4-last** placement is genuinely justified (a pure consumer like #19), not a sequencing accident.

## Build phases
- **B1 — Read + draft cores:** `do_find_case_study_candidates`, `do_draft_case_study` (graph → structured draft, anonymise-by-default), `case_studies` table (RLS) + `CASE_STUDY` graph upserts (`marketing_source_id`). MCP tools + REST routes for the two. Wire `marketing-brand-voice.json` / `marketing-content-templates.json`.
- **B2 — Testimonials + consent:** `do_request_testimonial` (high-NPS Watcher trigger off Support), `do_capture_testimonial`, `testimonials` table (RLS, consent columns), `CUSTOMER -[gave]-> TESTIMONIAL` edges. Consent + sign-off ledger trail.
- **B3 — Advisor surfaces:** `do_suggest_content`, `do_audit_seo`, `content_assets` table + brand-asset library REST; `memories` brand-voice recall loop.
- **B4 — Approval + publish gate:** `do_approve_content` (human sign-off), `do_publish_content` with `PublishTransport` (`manual` live, `cms` 🔴 stub raising `NotImplementedError` with a clear message). Enforce consent/anonymisation refusals at the publish boundary.
- **B5 — Brand-voice learning:** approved studies feed `memories`; drafts recalled-and-styled from prior approved work; "case-study candidate of the quarter" + testimonial-timing tuned from the ledger. Marketing slice of the #19 morning brief.
