# 13a — HR Engine: Huma / Nordic-HR Research & Idea Backlog

<!-- BLOCKED ON OQ-2 / OQ-4: RESEARCH COMPANION. Architectural research backlog. Verified-against: 7304330 -->

**Status:** research companion to `13-hr-engine.md` · **Date:** 2026-06-17
**Question asked:** *"Research Huma (huma.no) and the modern / Nordic HR-system landscape to inform the HR engine of an AI-native backend suite for a Norwegian AV-integrator."*
**Method:** parallel cited web investigations — Huma deep-dive (product, AI, API, scale); Norwegian HR-compliance reality (verneombud / AMU / HMS / sykefravær / varsling); skills/competence management vendors; AI-in-HR 2025-2026 and the ethical/legal line; build-vs-integrate. Sources at the bottom.

---

> **⚠️ SUPERSEDED in part (user directive 2026-06-17):** we are **moving off external HR SaaS** (Huma/Simployer/Hailey) to avoid the spend — **build native, copy-and-improve**. So this doc is a **feature catalogue to replicate**, not an integration plan. Specifically: **A2's "Huma as HRIS-of-record adapter" is dropped** — we own employee master data natively and copy Huma's UX; **A3 becomes "encode the law ourselves"** (the statute is public) rather than integrate Simployer, accepting that we maintain the encoded rules. Everything else (proficiency/multi-rater, sykefravær state-machine, EU-AI-Act Article-5 re-scope, no-ranking) stands. See roadmap §2.10 (build-vs-buy default) and the updated `13` Research-informed section.

## 1. Strategic read — where we play

The Nordic SMB HR market is **owned, mature, and explicitly compliance-first** — and that is *good news* for us, because none of it is what our engine is for. Two camps:

- **Daily-experience HRIS for Nordic SMBs** (Huma, Hailey HR, Sympa). Their moat is *frictionless admin + Nordic legal templates*: handbook, digital contracts + e-sign, absence with **automatic NAV sick-leave import**, onboarding, 1:1s, pulse surveys, whistleblowing. Huma is the local leader (Oslo, 1,000+ customers, ISO 27001, "top-10 fastest-growing tech in Norway"). They are *not* assignment engines — skills/competence is a self-service "map your skills" module, not infrastructure that *dispatches work*.
- **Compliance / HR-knowledge specialists** (Simployer, CatalystOne). Simployer's product is *the legal content itself* — HMS-håndbok, verneombud/AMU training, varsling guidance, an AI chatbot over Norwegian employment law. CatalystOne is the enterprise competence/CV/learning HRIS.

**The gap we exploit (all investigations converged):**
1. **Everyone treats skills as an HR feature; we treat it as the operating system for assignment.** Huma/Sympa "skills & competence" produces a *gap-analysis dashboard for a manager to read*. Our skills-matrix is **read by other engines via A2A** to pick a PL, dispatch a field tech, and schedule a resource. That is a different artifact with the same name. **We will not out-handbook Huma — we out-*assign* them.**
2. **We are MCP/A2A-native and graph-backed.** The skills-matrix + cert register live as graph nodes/edges that Project(7), Field Tech(12), and Resources query directly. Huma exposes an open REST API + webhooks (good — that's our integration seam) but is a system-of-record, not a reasoning layer.
3. **The Nordic no-ranking culture is a legal floor, not just taste** — and the EU AI Act now *prohibits* workplace emotion/stress inference outright (see §4, F4). The spec's red line is now **the only compliant design**, which is a moat: incumbents bolting on "engagement scoring" are walking into Article 5 exposure; we are clean by construction.

> **Positioning in one line:** *Not a better Nordic HRIS — an AI-native skills-as-assignment-infrastructure layer + private coach that sits on top of (or beside) Huma/Simployer, turning the workforce into a queryable layer of the cognitive graph while the incumbents own statutory compliance.*

---

## 2. The landscape (what to borrow from each)

| Solution | What it is | The idea worth stealing |
|---|---|---|
| **Huma** (humahr.com) | Nordic SMB HRIS; Oslo, 1,000+ customers, ISO 27001, NPS 60. Modules: People/master-data, digital **håndbok**, absence, onboarding/offboarding, **1:1 appraisals**, **pulse surveys (AI-assisted)**, documents + **digital contracts/e-sign**, **skills & competence (gap analysis)**, equipment, **whistleblowing**, **deviation/HSE (avvik/HMS)** | **Open REST API + webhooks** as the integration seam; **automatic NAV sick-leave import**; **PAXml** export to payroll; the "60% of HR time is admin → kill the admin" framing; **digital handbook as a first-class object**; whistleblowing + HMS already modeled as modules |
| **Simployer** | Norwegian HR-*knowledge* + compliance: HMS-håndbok, verneombud/AMU training, varsling guidance, **AI chatbot over employment-law content** | **The legal content is the product** — don't rebuild Norwegian law, *integrate the authority*. The AI-chatbot-over-statutory-content pattern is exactly our "compliance backend, native experience" split |
| **Hailey HR** | Swedish/Nordic modern HRIS; skills defined as named abilities + **proficiency levels** | **Explicit proficiency-level model** (not just has/has-not) — proficiency is what makes a fit score meaningful |
| **Sympa** | Pan-Nordic HRIS; structured **competence assessment** (HR + manager + self) rolled into a strengths/gaps overview | **Tri-perspective assessment** (self + manager + HR) as the *confidence input* to a skill edge — multi-rater raises trust without ranking people |
| **CatalystOne** | Enterprise competence/learning/CV HRIS | **Always-current employee CV** + competence→learning-path linkage; certs and skills tied to development goals (feeds our coach, not a leaderboard) |
| **Humaans** (global, not Nordic) | AI HRIS with "Agentic AI Workforce" + AI Companion | **AI Companion = Q&A over your own people-data + doc drafting** — the assistant pattern, but note it's *admin-facing*; our coach is *individual-facing* and private |

---

## 3. Idea backlog, prioritized & mapped to our engine

Each idea names the concrete change to `13-hr-engine.md` (which `do_*` / node / table / config it touches).

### Tier A — adopt now (architecture-shaping, low regret)

**A1. Proficiency-level + multi-rater confidence on the skill edge** *(Hailey + Sympa)*
The spec's `EMPLOYEE -[has]-> HR_SKILL` already carries "confidence = recency/assessment strength." Make that explicit: a **named proficiency level** (e.g. 1–4 / novice→expert) *plus* a **multi-rater origin** (self / manager / HR / cert-implied) that feeds the confidence. Hailey models proficiency; Sympa rolls up self+manager+HR. A fit score is only trustable if "has skill" means "at what level, asserted by whom."
- *Why:* `do_match_skills` returns *fit*, and fit needs proficiency granularity, not a boolean. Multi-rater is the no-ranking-safe way to raise confidence — it rates the *assertion*, never the person.
- *Touches:* `skills` table gains `level` + `rater_role`; `do_match_skills` weights coverage by level; `hr-skills-taxonomy.json` defines the level scale per skill family.
- → **integrate vs build:** **build** (this is the assignment core — our differentiator).

**A2. Huma as optional system-of-record via open API + webhooks** *(Huma integration surface)*
Huma exposes an **open REST API ("fetch and send data exactly the way you want") + webhooks**. This is the cleanest seam to *not own employee master data* where a namespace already runs Huma: sync `employees`/`absences` inbound, subscribe to webhooks for change events. The spec currently lists Simployer/Hailey as the compliance backend; **add Huma as an HRIS-of-record adapter option** in the same `client.py`/`auth.py` resilience pattern.
- *Why:* a Norwegian AV-integrator SMB is *exactly* Huma's customer. If they already have Huma, we should mirror its master data and layer skills-as-assignment on top, not re-key employees.
- *Touches:* generalize `hr_sync_now`/`hr_sync_status` + `hr_source_id` to support a `huma` source mode alongside `simployer`; webhook receiver route; `NCE_HR_HUMA_URL`/`_TOKEN` config.
- → **integrate vs build:** **integrate** master data + absence; **build** the skills graph + coach + capacity on top. (See §5 — this is the core build-vs-integrate answer.)

**A3. Don't rebuild Norwegian law — integrate the authority** *(Simployer)*
Simployer's *product is the statutory content* (HMS-håndbok, verneombud/AMU duties, varsling handling, the sykefravær timeline). The spec already says "native experience, Simployer/Hailey as compliance backend." Sharpen it: the **legal *rules and deadlines* are reference data we pull/cite**, not logic we author. Our Watcher tracks the **deadlines**; the *correctness of the rule* is Simployer's (or the råd we cite).
- *Touches:* `do_cert_status`/compliance-deadline Watcher cite a source-of-rule; `hr_source_id` provenance extends to compliance rules; no new law-authoring code.
- → **integrate vs build:** **integrate** the legal content/authority; **build** only the deadline-tracking + native intake UX.

**A4. The sykefravær follow-up timeline as a first-class Watcher state-machine** *(Arbeidstilsynet / NAV / NHO Arbinn — verified statutory)*
This is the single most concrete compliance asset and it's *exactly* the spec's Watcher territory. Verified Norwegian timeline: **oppfølgingsplan within 4 weeks** (shared with the sick-leave issuer); **dialogmøte 1 within 7 weeks** (employer's responsibility); **dialogmøte 2 within 26 weeks** (NAV convenes). Model absence → a staged follow-up quest with these legal deadlines, with **verneombud** as a participant role and **NAV reporting** as an outbound event.
- *Why:* this is the highest-value, fully-verifiable compliance feature for a Norwegian employer that global tools miss — and Huma already does the *NAV import* half, leaving the *follow-up orchestration* open.
- *Touches:* `do_register_absence` spawns an `HR_ABSENCE` follow-up sub-state (4w/7w/26w deadlines); Watcher fires on each; **manager/HR/verneombud-scoped only** (most sensitive, extra-redacted per the spec's `absences` rule).
- → **integrate vs build:** **build** the orchestration; **integrate** the statutory reporting to NAV (or via Huma/Simployer) and the rule-of-law authority (A3).

### Tier B — design for (build when the trigger arrives)

**B1. Cert→skill implication graph with decay** *(spec already has the seed; CatalystOne CV/competence)*
The spec's `hr-skills-taxonomy.json` already says "cert→skill implications." Make it a typed graph move: a valid **HR_CERT implies one or more HR_SKILL edges at a baseline proficiency**, with confidence that **decays toward `valid_to`** (the spec already decays cert confidence). So when a Crestron/QSC/Biamp/CTS cert expires, the *implied skills* soften automatically and `do_match_skills` reflects it without manual re-rating.
- *Touches:* `do_cert_status` → emits/refreshes implied `HR_SKILL` edges; decay shared with the cert Watcher.
- → **build** (cert lifecycle is AV-specific and assignment-load-bearing).

**B2. Tri-perspective assessment intake** *(Sympa)*
Let a skill level be asserted by self + manager + (optionally) HR, stored as separate rated origins reconciled into one confidence — never surfaced as a comparison *between* people, only as the strength of one person's skill claim.
- *Touches:* extends A1's `rater_role`; a lightweight intake on the profile card (`BrukerDetalj.jsx`), self/manager-scoped.
- → **build.**

**B3. Onboarding assistant for the 90-day quest** *(Humaans AI Companion, scoped down)*
Humaans' Companion answers employee questions from people-data + drafts docs. Mirror only the *private, individual* slice: a quest-aware assistant that answers "what's next in my onboarding," surfaces the relevant **håndbok** section, and nudges the next quest step — strictly self-scoped, no aggregation.
- *Touches:* `do_build_onboarding_quest` + a read-only Q&A over the namespace handbook/quest; `Onboarding.jsx`.
- → **build the experience**, **integrate the handbook content** (Huma håndbok or namespace docs).

**B4. Whistleblowing (varsling) + deviation (avvik/HMS) as modeled cases** *(Huma + Simployer both have these)*
Both incumbents model varsling and avvik/HMS as case objects with SLAs. The spec mentions varsling SLAs under the Watcher — formalize them as case states with the **arbeidsmiljøloven** handling duties, but given extreme sensitivity, **strongly prefer integrating** Simployer/Huma's varsling module over building our own intake (a mishandled whistleblowing channel is a legal liability, not a feature win).
- *Touches:* Watcher tracks SLAs only; case substance lives in the compliance backend.
- → **integrate** (strong default — do not own the varsling channel in v1).

### Tier C — later / opportunistic

- **C1. Digital handbook as a queryable object** (Huma): expose the namespace håndbok as graph-linked content the coach/onboarding assistant cites — not authored by us, mirrored from Huma/Simployer.
- **C2. PAXml / payroll bridge** (Huma): if we ever own absence master data, emit PAXml so existing payroll (Visma/Tripletex/PowerOffice) consumes it — the Nordic interop lingua franca.
- **C3. Equipment/asset assignment** (Huma has an equipment module): AV techs carry expensive kit; an `EMPLOYEE -[holds]-> ASSET` edge could feed Field Tech — but this likely belongs to Logistics/Assets, not HR. Flag, don't claim.

---

## 4. Findings to fold + honest flags

### Net changes to fold into `13-hr-engine.md`

Load-bearing (Tier A) — should update the spec now: **A1 proficiency + multi-rater skill edge**, **A2 Huma as an HRIS-of-record adapter option** (generalize the sync/source-mode beyond Simployer), **A3 integrate-the-authority framing** (we track deadlines, not author law), **A4 the verified sykefravær 4w/7w/26w follow-up state-machine** with verneombud role. The rest stay here as backlog and graduate on trigger. A "Research-informed direction" pointer is added to `13` referencing this doc.

### F4 — the EU AI Act red line *(critical, verified — affects the burnout Watcher)*
The EU AI Act **prohibits AI systems that infer emotions of a person in the workplace** (Article 5, in force **2 February 2025**); fines up to €35M / 7% of turnover, and because it touches biometric/special-category data it triggers parallel GDPR. **The Commission's guidance explicitly excludes "medical reasons" from covering general monitoring of stress, burnout, or depression.** This directly constrains the spec's **"burnout signal (load × consecutive-hours × low coaching-sentiment)"** Watcher: it is defensible **only** if built from *operational/objective signals* (assigned load, scheduled hours, absence) and **never from emotion/sentiment inference**. The "low coaching-sentiment" input is the dangerous term — recommend reframing the burnout Watcher to **objective-load-only**, surfaced privately as a *workload/capacity flag*, not an emotional state.
- → **idea for our HR engine:** rename and re-scope the burnout Watcher to a **"sustained-overload flag"** (objective hours/load/absence only), drop sentiment from it, and document the Article-5 boundary in `config.py` next to `NCE_HR_RANKING_DISABLED`. This makes the spec's privacy posture *legally* exact, not just culturally.

### Honest flags (don't over-trust the marketing)
- Huma's **open API is described as "coming soon"** on the integrations page — the read/write employee+absence sync (A2) is **roadmap-dependent, not verified live**; webhooks and PAXml export *are* documented. Treat A2 as "design for, validate the API surface before committing."
- Huma's **"AI" is documented only for survey-building** ("let AI help you"); there is **no verified standalone "Huma AI assistant"** with the breadth of Humaans' Companion. The richer AI-companion pattern is **Humaans (global), not Huma** — don't conflate them.
- Huma **funding/valuation is behind PitchBook paywalls**; "40 employees / top-10 fastest-growing / 1,000+ customers / NPS 60 / ISO 27001" are **vendor- or aggregator-stated**, not independently audited.
- Simployer/Hailey/Sympa/CatalystOne competence claims are **marketing-page level**; the *depth* of their proficiency/multi-rater models (A1/B2) is inferred from feature pages, not hands-on.
- The sykefravær 4w/7w/26w timeline **is verified** against Arbeidstilsynet / NAV / NHO Arbinn — treat A4 as solid.

### Reinforced invariants (unchanged, restated)
**Skills-matrix is assignment infrastructure** — it feeds Project PL-assignment + Field Tech dispatch + Resources scheduling via A2A, and that consumption is the *point*, not the HR dashboard. **Strict PII** — redaction gate + FORCE RLS + agent-scoping stay blocking preconditions. **NEVER ranking** — no leaderboard, no peer comparison, no standing score; now reinforced by EU-AI-Act Article 5 (F4), making the red line a legal floor, not a preference. Multi-rater (A1/B2) rates *assertions about a skill*, never people against each other.

---

## Sources

**Huma:** [Product overview](https://humahr.com/productoverview) · [Integrations (API / webhooks / PAXml)](https://humahr.com/integrations) · [Behind Huma (scale, ISO 27001, NPS)](https://humahr.com/behind-huma) · [Huma EN home](https://humahr.com/en/) · [PowerOffice customer case](https://humahr.com/customercases/poweroffice) · [Huma on Aider (Nordic HR/ERP)](https://aider.no/en/technology/erp/hr-salary/huma) · [GetApp profile](https://www.getapp.com/hr-employee-management-software/a/huma/) · [PitchBook profile (paywalled)](https://pitchbook.com/profiles/company/593997-22)

**Norwegian compliance (verified statutory):** [Arbeidstilsynet — oppfølging av sykmeldte](https://www.arbeidstilsynet.no/arbeidstid-og-organisering/tilrettelegging/oppfolging-av-sykmeldte/) · [NAV — oppfølgingsplan](https://www.nav.no/oppfolgingsplan) · [NAV — slik følger du opp sykmeldte](https://www.nav.no/arbeidsgiver/oppfolging-sykmeldte) · [NHO Arbinn — sykefraværsoppfølging skritt-for-skritt](https://arbinn.nho.no/arbeidsrett/sykefravar_og_permisjoner/sykefravarsoppfolging/sykefravarsoppfolging-skritt-for-skritt/) · [Arbeidstilsynet — roller i HMS-arbeidet (verneombud/AMU/BHT)](https://www.arbeidstilsynet.no/hms/roller-i-hms-arbeidet/)

**Compliance / competence vendors:** [Simployer HMS](https://www.simployer.com/no/produkter/hrm/classic/hms) · [Simployer HMS-håndbok](https://www.simployer.com/no/produkter/handboker/hms-handbok) · [Simployer HMS for verneombud og AMU](https://www.simployer.no/produkter/learn/kurs/e-laering/hms-for-verneombud-og-amu-kontor-butikk) · [Hailey HR features](https://haileyhr.com/features/) · [Sympa — skills & competence management](https://www.sympa.com/product/skills-and-competence-management/) · [CatalystOne — competence & learning](https://www.catalystone.com/en/solutions/competence-and-learning) · [CatalystOne — CV](https://www.catalystone.com/en/solutions/competence-and-learning/cv) · [Humaans (AI HRIS / Companion)](https://humaans.io/)

**AI-in-HR & the legal line:** [FPF — EU AI Act emotion-recognition prohibition in the workplace](https://fpf.org/blog/red-lines-under-eu-ai-act-unpacking-the-prohibition-of-emotion-recognition-in-the-workplace-and-education-institutions/) · [Wolters Kluwer — prohibition of AI emotion recognition at work](https://legalblogs.wolterskluwer.com/global-workplace-law-and-policy/the-prohibition-of-ai-emotion-recognition-technologies-in-the-workplace-under-the-ai-act/) · [EU AI Act employee-monitoring guide](https://www.employee-monitoring.net/compliance/eu-ai-act-employee-monitoring) · [SHRM — State of AI in HR 2026](https://www.shrm.org/topics-tools/research/state-of-ai-hr-2026) · [HRD — AI in HR 2026 predictions](https://www.hrdconnect.com/2025/12/09/ai-predictions-in-hr-2026/) · [Phenom — 2026 onboarding trends (AI + skills)](https://www.phenom.com/blog/onboarding-trends-ai-skills)
