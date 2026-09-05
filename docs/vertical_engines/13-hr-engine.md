> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 13 — HR Engine  (nce/vertical_modules/hr)

**Status:** spec (Tier 4 — Platform axis) · **Owner:** NCE core (Sindre)
**Pattern companions:** `docs/VERTICAL_MODULE_PATTERN.md`, `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§2 AI-role taxonomy, §4 graph catalogue, §7 spec format)

## Mission
Make the workforce a **first-class, queryable layer of the cognitive graph** so the other engines can assign work intelligently. The headline is not HR admin — it is that **the Academy skills-matrix + certification register is OPERATIONAL INFRASTRUCTURE**: it directly drives PL-assignment (Project), tech-dispatch (Field Tech), and capacity planning. HR data is *consumed by other engines*; the daily HR experience is the by-product, not the point. The deep-AI angle is a **"WHOOP for work" private AI coach** — a per-employee advisor that surfaces skill-gaps, burnout signals, and training paths to *that employee and their manager only*. **Hard constraint, stated once and enforced everywhere: NEVER ranking, NEVER leaderboards, NEVER comparative scoring of people.** This is a Nordic-culture / privacy red line, not a feature toggle — the coach is individual and private; its data is never aggregated into a who-is-best list. Norwegian-law compliance (Verneombud, AMU, HMS, sykefravær, varsling) is built **natively** — **we copy-and-improve Huma/Simployer/Hailey's best ideas rather than pay for them** (user directive 2026-06-17: move off external HR SaaS, own it in-house). The *law itself is public*, so we encode its rules + deadlines natively and cite the statute; the honest cost we accept is **keeping the encoded rules current ourselves** (the work Simployer otherwise sells).

## Research-informed direction (see `13a-hr-engine-research.md`)
A deep dive on **Huma (huma.no)** + the Nordic-HR landscape (Simployer, Hailey, Sympa, CatalystOne) — read now through the **user directive (2026-06-17): move OFF external HR SaaS, copy-and-improve them natively, don't spend the subscription money.** So the research is used as a **feature catalogue to replicate**, *not* an integration list. Positioning: *not a better Nordic HRIS bolted onto Huma — a native AI-native HR engine that **replaces** Huma/Simployer for our own org, with skills-as-assignment-infrastructure + a private coach as the differentiator.* Incumbents treat skills as a manager-facing **dashboard**; we make the skills-matrix **queryable by Project/Field-Tech/Resources via A2A** — same word, different artifact. Load-bearing:
- **A1 — proficiency + multi-rater on the skill edge.** `EMPLOYEE -[has]-> HR_SKILL` carries a named **proficiency level** (1–4) **+ rater origin** (self/manager/HR/cert-implied) feeding confidence. Multi-rater is the no-ranking-safe way to raise trust — it rates the *assertion*, never the person. `skills` gains `level` + `rater_role`; `do_match_skills` weights by level. **Build** (this is the assignment core).
- **A2 — BUILD native employee master data (copy Huma's UX, don't integrate it).** Per the directive, Huma is **not** an HRIS-of-record we sync to — we **own** `employees`/`absences`/handbook natively and **replicate Huma's best ideas**: frictionless admin, **digital handbook as a first-class object**, the **NAV sick-leave-import pattern**, whistleblowing/HMS as modeled cases, equipment register. (The earlier "Huma adapter" framing in `13a` A2 is **superseded** by this directive — keep the *feature ideas*, drop the *integration*.) The one external we still touch is **NAV** (statutory reporting), not a paid HR vendor.
- **A3 — encode the law natively (copy Simployer's content, don't pay for it).** Simployer's product is curated statutory content — but **the law is public**, so we **encode the rules + deadlines ourselves** (sykefravær 4w/7w/26w, verneombud/AMU duties, varsling handling) and cite the statute. **Honest trade-off we accept (per the directive):** *we* own keeping the encoded rules current — the maintenance Simployer otherwise sells is now in-house. `hr_source_id` provenance points to the *statute*, not a vendor.
- **A4 — sykefravær follow-up as a first-class Watcher state-machine** (verified vs Arbeidstilsynet/NAV/NHO Arbinn): **oppfølgingsplan ≤4 weeks → dialogmøte 1 ≤7 weeks → dialogmøte 2 ≤26 weeks**, with **verneombud** as a participant role and NAV reporting as an outbound event. Highest-value verifiable NO-compliance feature; manager/HR/verneombud-scoped + extra-redacted.
- **⚖️ LEGAL RED LINE (EU AI Act Article 5, in force 2 Feb 2025) — re-scope the burnout Watcher.** The Act **prohibits AI that infers emotions in the workplace** (fines to €35M/7%); guidance explicitly does **not** exempt stress/burnout/depression monitoring. So the spec's "burnout signal (load × hours × **low coaching-sentiment**)" is non-compliant as worded. **Drop the sentiment term**; rebuild it as a **"sustained-overload flag" from objective signals only** (assigned load, scheduled hours, absence), surfaced privately as a workload/capacity flag — never an emotional state. Document the Article-5 boundary in `config.py` next to `NCE_HR_RANKING_DISABLED`. The NEVER-ranking line is now a **legal floor, not a preference** — a moat (incumbents bolting on "engagement scoring" walk into Article-5 exposure).

## Inspiration & triage
- **the planning source:** `04-virksomhets-modulkart.md` module **16 — HR & Performance (Academy)** — Platform axis; core objects: profilkort, skills-matrise, sertifiserings-lifecycle, kapasitet, Academy, onboarding-quest, leave. Module map §note: "Platform-modulene tjener alle andre … HR/Academy mater assignment"; "Delt ressurs-pool — 07 og 12 deler teknikere; 11 (eksterne) utvider med restricted access; 16 driver hvem som dispatches/assignes."
- **Portal sidecar to lift (`backend/portal_hr/`):** triage each into the clean vertical —
  - `employees.py:EmployeeRegistry` (in-memory dict + `semantic_skills_search`) → `hr/employees.py` + `employees` table; the naïve keyword search becomes a real `memories`/graph skills-match.
  - `absence.py:AbsenceManager` (incl. the Norwegian NLP **Smart Leave Assistant** `parse_natural_language`) → `hr/absence.py` + `absences` table; keep the NLP parser as a pure helper.
  - `checklists.py:SmartChecklistBuilder` (role/department-driven onboarding + offboarding) → `hr/onboarding.py` (the 90-day quest).
  - `performance.py:PerformanceManager.log_one_on_one` (already writes `MemoryPayload` episodic → NCE) → `hr/ingestion.py`; this is the seed of push+semantic, but **PII-hardened** (see Classification).
  - `compliance.py:GDPRComplianceAuditor.scan_text` (fødselsnummer/bank/phone regex) → `hr/redaction.py`, promoted from a passive auditor to an **enforced redaction gate** on every memory write.
- **Lysning pages served:** the HR dashboard page, `Brukere.jsx` (employee list), `BrukerDetalj.jsx` (profile card / skills / certs / absence), `Onboarding.jsx` (the quest) — all consume the no-model REST surface.

## Classification
**push + semantic — with the strictest PII posture of any engine.** **No external HR SaaS (build-vs-buy directive, roadmap §2.10):** the source of truth is internal (employee/skill/cert/absence rows) and **compliance is encoded natively** (we copy Simployer's content discipline, we don't pay for it — the law is public). The **only** external touch is **statutory reporting to NAV** (sykefravær follow-up), not a paid HR vendor. Semantic track: 1-on-1 notes, reviews, coaching observations → `memories`. **Auth/privacy model:** every semantic write passes the `redaction.py` gate (fødselsnummer/bank/phone stripped before embedding); memories are written agent-scoped (`hr_private_coach`) and **never** surfaced to ranking/aggregate queries. Resilience for the NAV reporting adapter: `httpx.AsyncClient` (30s) via `nce.http_resilience.request_with_retry()`, token Redis-cached in `auth.py`.

## Graph contribution
Node `entity_type` prefixes: `HR_*`, plus shared spine node `EMPLOYEE` (and the cross-engine `WORK_ORDER`/`PROJECT` endpoints it links to).
- **Nodes:** `EMPLOYEE` (profile card), `HR_SKILL`, `HR_CERT` (CTS/Crestron/QSC/Biamp/Cisco — with `valid_to`/expiry), `HR_ABSENCE`, `HR_ONBOARDING_QUEST`. **`CONTRACTOR`** is the external parallel to `EMPLOYEE` — owned by Vendors(4) but it **shares the exact same `HR_SKILL`/`HR_CERT` model**, so the assignment engines can score employees and contractors identically.
- **Edges (the §4 contract, our slice):**
  - `EMPLOYEE -[has]-> HR_SKILL` (confidence = recency/assessment strength)
  - `EMPLOYEE -[has]-> HR_CERT` (confidence decays toward expiry; drives Watcher)
  - `EMPLOYEE -[absent]-> HR_ABSENCE` (capacity input)
  - `WORK_ORDER -[assigned_to]-> EMPLOYEE` — **shared edge written by Field Tech**, read here for load/capacity.
  - `PROJECT -[led_by]-> EMPLOYEE` — PL assignment, written when Project picks a lead by skill/capacity.
  - `CONTRACTOR -[has]-> HR_SKILL/HR_CERT` — the parallel external-resource edges (Vendors writes the node, shares this model).
- **memories/ledger:** 1-on-1 notes, reviews, private-coach observations → `memories` (embedding + `content_fts`), **redacted, agent-scoped, RLS-locked**. Capacity/utilization recalcs and assignment recommendations → `v3_cognitive_ledger` (for cognitive recall: "who handled similar work"). Tag every derived row with `hr_source_id` for hard-retirement (D365 retirement pattern) — critical for GDPR right-to-erasure.

## Core functions
Pure-ish `do_<action>(engine, params) -> dict`; the skill-match / capacity / cert-expiry cores are pure (0 DB) and overlay namespace config.
- `do_get_employee(engine, params) -> dict` — profile card: identity + skills-matrix + cert lifecycle + capacity + active quest. Access-scoped (self/manager/HR-admin only).
- `do_match_skills(engine, params) -> dict` — `{required_skills[], required_certs[], when}` → ranked-by-FIT candidate set (employees **and** contractors), each with skill coverage, valid certs, current load. **Returns fit-to-requirement, NOT a person leaderboard** — the score is per-(person × requirement), never a standing rank.
- `do_capacity(engine, params) -> dict` — `{employee_id|team, period}` → utilization from assigned `WORK_ORDER`/`PROJECT` edges minus `HR_ABSENCE`. Pure over graph.
- `do_cert_status(engine, params) -> dict` — cert lifecycle per employee: valid / expiring-within-N / expired (feeds Watcher).
- `do_register_absence(engine, params) -> dict` — wraps the lifted **Smart Leave Assistant** NLP parser (Norwegian free-text → structured leave); writes `HR_ABSENCE`; pushes statutory sykefravær to the Simployer backend.
- `do_build_onboarding_quest(engine, params) -> dict` — role/department → 90-day quest (lifts `SmartChecklistBuilder`); returns staged tasks + progress.
- `do_log_one_on_one(engine, params) -> dict` — review/coaching note → **redaction gate** → private agent-scoped memory (lifts `performance.py`, PII-hardened).
- `do_coach(engine, params) -> dict` — the private AI coach: skill-gap → training recommendation for **one** employee. Strictly self/manager-scoped; explicitly refuses any cross-person comparison request.

## MCP tools
Registered in `nce/tool_registry.py` via `_h(...)` late-binding. AI-role tag per roadmap §2 taxonomy.

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `hr_get_employee` | ✔ | ✘ | ✘ | Watcher (access-scoped) |
| `hr_match_skills` | ✔ | ✘ | ✘ | Advisor |
| `hr_capacity` | ✔ | ✘ | ✘ | Advisor |
| `hr_cert_status` | ✔ | ✘ | ✘ | Watcher |
| `hr_register_absence` | ✘ | ✘ | ✔ | Actor (with confirmation) |
| `hr_build_onboarding_quest` | ✘ | ✔ | ✔ | Actor |
| `hr_log_one_on_one` | ✘ | ✔ | ✔ | Actor (PII-redacted, private) |
| `hr_coach` | ✔ | ✘ | ✘ | Advisor (private, NEVER ranking) |
| `hr_sync_now` | ✘ | ✔ | ✔ | — (operator) |

## REST routes
No-model path for the BFF (the HR dashboard, `Brukere.jsx`, `BrukerDetalj.jsx`, `Onboarding.jsx`), cron, scripts. Mounted via `build_app(extra_routes=...)`; HMAC/mTLS-authed in `nce/admin_handlers/hr.py`. **Every route enforces the access-scope check before returning PII.**
- `api_hr_employees` (GET) — list (`Brukere.jsx`), field-scoped by caller role.
- `api_hr_employee` (GET) — profile card (`BrukerDetalj.jsx`).
- `api_hr_match_skills` (POST) — assignment fit for Project/Field Tech screens.
- `api_hr_capacity` (GET) — utilization dashboard.
- `api_hr_cert_status` (GET) — cert-expiry board (Watcher surface).
- `api_hr_register_absence` (POST) — Smart Leave Assistant intake.
- `api_hr_onboarding` (GET/POST) — quest state + task completion (`Onboarding.jsx`).
- `api_hr_sync_status` / `api_hr_sync_now` — Simployer/Hailey backend health.

## AI features
- **Watcher:** cert-expiry alerts (CTS/Crestron/QSC/Biamp/Cisco approaching `valid_to`); **sustained-overload flag** — **objective signals ONLY** (assigned load × scheduled hours × absence; **NO sentiment / emotion / well-being inference** — prohibited by **EU AI Act Article 5**, in force 2 Feb 2025, fines to €35M/7%; see `13a` §A4) — surfaced **privately** to the individual + their manager as a **workload/capacity** flag, never an emotional state, never broadcast; **sick-leave pattern** detection (statutory thresholds → HMS/Verneombud follow-up, handled with extra confidentiality); compliance-deadline tracking (AMU cadence, varsling case SLAs).
- **Advisor:** `match_skills` assignment fit with plain-language rationale ("Maria fits — holds valid HDMI cert, 60% loaded next week, has fiber-splicing skill") and `coach` skill-gap → training recommendation. **who-to-assign** output feeds Project/Field Tech.
- **Actor:** register absence, build onboarding quest, log 1-on-1 — all *with confirmation*.
- **Explicitly NOT built — NEVER ranking:** no leaderboard, no peer comparison, no standing employee score, no "top performer" surface. The coach is private and individual; any tool/prompt asking for a cross-person ranking is refused by design. (Nordic culture + privacy red line.)
- **Cognitive recall:** "who handled similar work" via `memories` + ledger (e.g. find the tech whose past work-orders most resemble this room/system) — recall of *experience*, not a rating of *people*.
- **Enrichment triggers (event-scoped, never a background sweep):** AI enriches a profile *only* when that employee is considered for an assignment, completes a quest step, has a cert nearing expiry, or has a 1-on-1 logged. Never bulk-recompute all employees.

## A2A flows
- **Serves Project(7):** PL-assignment — Project asks `hr_match_skills`/`hr_capacity` for a lead by skill + availability; HR writes `PROJECT -[led_by]-> EMPLOYEE`.
- **Serves Field Tech(12):** tech-dispatch — Field Tech asks for candidates by required **cert** + skill + current load; consumes `WORK_ORDER -[assigned_to]-> EMPLOYEE` back as capacity input.
- **Serves Vendors(4):** exposes the shared `HR_SKILL`/`HR_CERT` model so `CONTRACTOR` resources score on the same axis as employees (restricted-access external pool).
- **Consumes** work-order/project outcomes for capacity/utilization (the load side of the assignment loop).
- **Feeds Morning-brief (#19 aggregate):** the HR slice = expiring certs + at-capacity teams + open compliance deadlines — **aggregate operational risk only, never per-person performance.**

## Config keys
`NCE_HR_*` in `nce/config.py`: `NCE_HR_ENABLED`, `NCE_HR_SIMPLOYER_URL` / `NCE_HR_SIMPLOYER_TOKEN` (compliance backend; secret), `NCE_HR_CERT_EXPIRY_WARN_DAYS` (default 90), `NCE_HR_CAPACITY_HORIZON_DAYS`, `NCE_HR_SICK_LEAVE_PATTERN_THRESHOLD`, `NCE_HR_COACH_ENABLED` (private coach toggle), `NCE_HR_RANKING_DISABLED` (**hard-pinned `true`, not operator-clearable** — defence-in-depth against the red line), `NCE_HR_SYNC_INTERVAL_MINUTES`. Namespaces opt in via `metadata.hr.enabled = true`.
**Config-as-IP JSON (namespace-scoped, the business IP — NOT code):**
- `hr-skills-taxonomy.json` — the skills-matrix vocabulary + cert→skill implications (CTS/Crestron/QSC/Biamp/Cisco families).
- `hr-onboarding-quests.json` — role/department → 90-day quest templates (lifts `SmartChecklistBuilder`'s role logic out of code).

## Tables/migrations
**Sensitive PII — FORCE RLS on every table, plus the redaction gate on semantic writes.** Beyond the graph, keyed tables are warranted because HR rows are queried by id/role constantly and must be hard-erasable for GDPR:
- `employees` — profile card (identity, role, department, leave_balance, active). `ENABLE` + `FORCE ROW LEVEL SECURITY` + `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`.
- `skills` — employee↔skill with assessment level/recency (mirrors a `kg_edge` for fast matrix queries).
- `certifications` — cert lifecycle (`employee_id, authority, name, issued, valid_to, raw jsonb, hr_source_id`) — the expiry-tracking spine for the Watcher.
- `absences` — leave/sick records (`employee_id, type, start, end, reason, status`); the most sensitive table — **extra-redacted, manager/HR-scoped reads only**.
All four `ENABLE`+`FORCE ROW LEVEL SECURITY` with the tenant policy; mirror DDL into `schema.sql` + numbered migration. `hr_sync_runs` (audit, mirrors `d365_sync_runs`) for Simployer backend run history.

## Dependencies
- **Downstream consumers (the whole point):** Project(7) and Field Tech(12) consume `hr_match_skills`/`hr_capacity`/`hr_cert_status` for assignment/dispatch — HR is assignment infrastructure for both.
- **Upstream / parallel:** Vendors(4) owns the `CONTRACTOR` node but **shares HR's skill/cert model** — coordinate the taxonomy so employees and contractors are interchangeable in match queries.
- **External (optional, deferred):** Simployer / Hailey HR as the statutory compliance backend (sykefravær, Verneombud, AMU, varsling) — integration, not rebuild; HR ships fully usable internally before this lands.
- **Privacy/governance gate (blocking):** the redaction gate (`redaction.py`) + access-scope enforcement + the pinned `NCE_HR_RANKING_DISABLED` must be in place **before** any semantic write or coach feature is enabled. This is the engine's hard precondition, not a later hardening pass.

## Build phases
- **B1 — Profile + skills graph (RLS):** `employees`/`skills`/`certifications` tables (FORCE RLS), `EMPLOYEE`/`HR_SKILL`/`HR_CERT` nodes + edges, `do_get_employee` + `do_match_skills` (pure), access-scope check. MCP tools + REST for the read surface (`Brukere.jsx`/`BrukerDetalj.jsx`). Wire `hr-skills-taxonomy.json`.
- **B2 — Capacity + certs Watcher:** `absences` table, `do_capacity` (consume `WORK_ORDER`/`PROJECT` edges), `do_cert_status` + cert-expiry Watcher alerts, lift the Smart Leave Assistant NLP into `do_register_absence`.
- **B3 — Onboarding quest:** lift `SmartChecklistBuilder` → `do_build_onboarding_quest` + `hr-onboarding-quests.json`, quest progress (`Onboarding.jsx`).
- **B4 — Private semantic + coach (gated on the privacy precondition):** `redaction.py` enforced gate, `do_log_one_on_one` (PII-hardened, agent-scoped memories), `do_coach` (private skill-gap → training), burnout/sick-leave-pattern Watchers — all individual, NEVER ranking. Cognitive recall ("who handled similar work").
- **B5 — Compliance backend + A2A:** Simployer/Hailey adapter (`auth.py` + `client.py`, statutory sykefravær/Verneombud/AMU/varsling), `sync_now`/`sync_status`; finalize A2A flows serving Project/Field Tech/Vendors and the Morning-brief HR slice (aggregate-only).
