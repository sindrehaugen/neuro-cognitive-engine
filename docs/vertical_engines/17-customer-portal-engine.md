> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 17 — Customer Portal Engine  (nce/vertical_modules/customer_portal)

**Status:** spec (Tier 4 — external surface; high lock-in) · **Owner:** NCE core (Sindre)
**Pattern companions:** `docs/VERTICAL_MODULE_PATTERN.md`, `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§2 conventions incl. §2.10 build-vs-buy, §4 graph catalogue, §7 spec format, §9.1 FUNCTIONAL_LOCATION / BOM_LINE state-machine / SLA 4-way / ASSET, §9.6 partner-scope RLS primitive, §10 moat), `docs/vertical_engines/05-sales-engine.md` (public-surface hardening), `docs/vertical_engines/04-vendors-engine.md` (allow-list partner redaction — the canonical external-surface model)

## Mission
Be **the customer-facing surface, done properly in NCE** — replacing the existing **poor-quality portal** with a native build (per §2.10: copy-and-improve, don't extend the legacy). It is **planning module 13 — Customer Portal**, and its organising principle is **ROOM-CENTRIC**: the customer sees **rooms, not project numbers** — *"your boardroom is 80% ready"* — via a **Domino's-style tracker** (`Planned → Ordered → Delivered → Installed → Tested → Ready`). The engine is designed to become **MORE valuable after handover** (the lock-in thesis): a room-centric **asset register**, **service-request** intake, **document access** (FDV / as-built), **SLA self-service**, and **expansion / re-buy** surfaces all keep the customer logging in long after delivery. **Hard rule (the reference implementation): external pitch / marketing sites must NEVER link into the portal.** The deep-AI angle is a strictly-scoped, customer-principal-sandboxed Advisor that narrates room status and answers *"when will my room be ready / how do I raise a service request"* — over the customer's own data only.

## What it is architecturally
**A READ-PROJECTION engine over the cognitive graph + a thin set of inbound CUSTOMER ACTIONS that HAND OFF.** It owns almost no operational state. Room-centric views hang off **`FUNCTIONAL_LOCATION`** — the **customer-site `SITE>…>ROOM>POSITION` tree** (§9.1), explicitly **NOT** `STOCK_LOCATION` (internal logistics). The Domino's tracker is the **`BOM_LINE.status` progression** (§9.1 state-machine) and the **`ASSET` lifecycle**, **projected per room** — never re-computed, never re-stored.

**Owns** (sole writer) only three thin nodes:
- `PORTAL_USER` — customer login identity / session (a **customer principal**, not an employee).
- `SERVICE_REQUEST` — customer-raised request **intake**, *before* Support owns the resulting `TICKET`.
- `DOCUMENT_SHARE` — a scoped grant of an FDV / as-built / SoW document to a customer.

**Does NOT own** Project / Asset / Ticket / SLA / Invoice / Design — it **projects** them (read) and **hands off** writes to their owners. There is no portal-side copy of operational truth.

## Inspiration & triage
- **the planning source (module map `04-virksomhets-modulkart.md`):** Module **13 — Customer Portal** — room-centric tracker (*"boardroom 80% ready"*), post-handover asset register + FDV access + service requests + SLA self-service + re-buy; the *"more valuable after handover"* lock-in bet; the *"pitch sites never link into the portal"* hard rule.
- **Portal sidecar to lift:** the **existing poor-quality customer portal** — treated as a **feature catalogue to replace**, not a codebase to extend (§2.10). Its room/tracker UX intent is the spec; its implementation is discarded.
- **Lysning pages served:** the customer-facing shares already noted in Sales (`Motebrief.jsx`, `TilbudKunde.jsx`) are *pre-sale*; this engine owns the *post-sale* customer surface (room tracker, asset register, service-request, document, SLA self-service pages) — a **separate app** from the internal BFF.

## Classification
**pull / read-projection + thin inbound actions** (NOT push+semantic — it produces no feed of its own). External systems: **none of its own**; it reads other engines' graph nodes over A2A/REST. Auth is the defining divergence: customer login is a **customer principal** authenticated by **BankID** (via the §2.10 Criipto/Signicat broker carve-out) or **email magic-link** — *not* the internal HMAC/mTLS employee path. It runs as **its own rate-limited, identity-authed app** (admin/BFF split per PR #241), a **different threat model** from the internal admin app. Any customer-facing AI assistant is treated as a **prompt-injection surface** — heavily sandboxed and customer-principal-scoped.

## Security is the engine (the centerpiece — sharpest external surface in the suite)
The portal's value is the *features*; the **build is the security boundary**. Four enforced layers (defence in depth — any one failing still denies):

1. **Customer-scope RLS (generalise §9.6 partner-scope to an external-PRINCIPAL scope).** The §9.6 partner-scope RLS primitive (`nce.partner_scope_id` GUC + `get_nce_partner_scope()`, **deny-when-unset**, **ANDs with the tenant policy**) is generalised to an **external customer principal scope**: a customer session can read ONLY its own org's sites / rooms / assets / tickets / SLAs / invoices. **Customer Portal is the OTHER heavy client of that core primitive** (alongside Field-Tech contractors) — so the primitive must be **built and security-hardened in NCE core** (not as portal vertical work), and its scope model must cover *external customer* principals, not only internal contractors. Defaults to **DENY** when the customer scope GUC is unset.
2. **Explicit field ALLOWLIST (not denylist).** Every projection passes an **allow-list of customer-safe fields** before serialization (reuse Vendors' canonical `partner-redaction.json` model — `04` §"Partner Access Model" layer 3). **Never leak** margin / cost / our-cost / supplier terms / internal status (*"we're behind schedule"* must never reach the customer — the tracker shows neutral room state, not internal slip). The safe-field set lives in `customer-redaction.json` (config-as-IP, auditable, tenant-tunable).
3. **Its OWN rate-limited, identity-authed app.** A separate small app (admin/BFF split per PR #241 + the Sales public-surface threat model, `05` hardening #4) with its own rate limiting, its own customer-principal session, and **no internal tool surface** — the customer agent profile binds only customer-safe read tools (the Vendors layer-2 pattern).
4. **External-facing AI = prompt-injection threat model.** The Advisor runs under the customer principal's RLS scope, with a **redacted toolset** and input treated as untrusted — it cannot be prompted into reading another customer's data or an internal field, because the RLS scope + tool binding deny it structurally, not by instruction.

**Compliance gate — DPIA / GDPR / personvern-by-design.** The portal handles **customer personal data**, so it is bound by the reference implementation **Blocker #10**, which was **DPIA-blocked to May 2026**. As of today (June 2026) that gate is **likely now clearing** — but it remains a **hard gating compliance dependency**: no production customer data flows until the DPIA signs off. Personvern-by-design: data minimisation in the allow-list, scoped document grants with expiry, customer-principal audit trail.

## Graph contribution
Node `entity_type` prefixes: `PORTAL_*` for engine-owned nodes; **projects** (reads, never writes) the shared spine nodes `FUNCTIONAL_LOCATION`/`SITE`/`ROOM`, `BOM_LINE`, `ASSET`, `TICKET`, `SLA`, `INVOICE`, `PROJECT`, `DESIGN`, `CUSTOMER`.
- **Nodes (owned):** `PORTAL_USER` (customer login identity/session, customer-principal-scoped), `PORTAL_SERVICE_REQUEST` (intake before Support's `TICKET`), `PORTAL_DOCUMENT_SHARE` (scoped FDV/as-built/SoW grant w/ expiry).
- **Edges (the §4 contract, our slice):**
  - `PORTAL_USER -[principal_for]-> CUSTOMER` — binds a login to a customer org (the RLS scope anchor).
  - `PORTAL_SERVICE_REQUEST -[becomes]-> TICKET` — the **hand-off** to Support (Support owns the `TICKET`; the portal owns only the intake).
  - `PORTAL_DOCUMENT_SHARE -[grants]-> DOCUMENT` (scoped, expiring access to an FDV/as-built doc).
  - `EXPANSION_INTEREST -[surfaces_to]-> SALES_LEAD` — re-buy / expansion signal handed to **Sales** (never pushed at the customer).
- **Projected (read-only):** the room tracker reads `BOM_LINE.status` (per §9.1 state-machine) + `ASSET` lifecycle scoped to a `ROOM` (`BOM_LINE -[installed_as]-> ASSET -[lives_in]-> ROOM`); the asset register reads `ASSET -[lives_in]-> ROOM`; SLA self-service reads the **Support-owned clock + Agreements-owned terms** slice of the 4-way `SLA` (§9.1).
- **memories/ledger:** every customer login, document-grant, service-request, and Advisor interaction → `v3_cognitive_ledger` (customer-principal audit trail — GDPR-relevant, who-saw-what). The Advisor uses cognitive recall **only within the customer's own scope**. No customer free-text is embedded into shared `memories` without DPIA-cleared consent.

## Core functions
Pure-ish `do_<action>(engine, params) -> dict`; **every** read resolves the **customer-principal scope** first (deny-when-unset) and passes the **allow-list projection** before returning. Reads dispatch to owner engines over A2A; writes only hand off.
- `do_room_tracker(engine, params) -> dict` — `{site_id|room_id}` → per-room Domino's status (`Planned→Ordered→Delivered→Installed→Tested→Ready`) projected from `BOM_LINE.status` + `ASSET` lifecycle. Returns neutral room state; **never** internal slip/schedule fields. — read-projection.
- `do_room_overview(engine, params) -> dict` — all the customer's rooms with % -ready rollup (*"boardroom 80% ready"*). — read-projection.
- `do_asset_register(engine, params) -> dict` — `{room_id}` → room-centric asset list (model, warranty, SLA coverage) from Assets, allow-list projected. — read-projection.
- `do_list_documents(engine, params)` / `do_get_document(engine, params)` — FDV / as-built / SoW the customer is granted (`PORTAL_DOCUMENT_SHARE`-scoped, expiry-checked). — read-projection.
- `do_sla_status(engine, params) -> dict` — SLA self-service: the customer's SLA terms (Agreements) + running clock / breach state (Support), allow-list projected (no internal cost/MRR). — read-projection.
- `do_list_invoices(engine, params) -> dict` — the customer's own invoices (Economy), allow-list projected. — read-projection.
- `do_raise_service_request(engine, params) -> dict` — create a `PORTAL_SERVICE_REQUEST`; **hands off** to Support `do_open_ticket` (Actor; idempotent — never double-opens). The portal owns the intake, Support owns the resulting `TICKET`.
- `do_register_expansion_interest(engine, params) -> dict` — capture a re-buy/expansion signal; **hands off** to Sales as a lead (Actor). Surfaced to Sales' Advisor — never an outbound push at the customer.
- `do_authenticate(engine, params) -> dict` — establish a customer principal (BankID via broker, or email magic-link); sets the customer-scope GUC for the session. Never an employee path.
- `do_advisor_answer(engine, params) -> dict` — customer-facing assistant (room-status narrative + "when ready / how to raise a request"), **strictly scoped to the customer's own data**, redacted toolset, prompt-injection-sandboxed. — Advisor.

## MCP tools
Registered in `nce/tool_registry.py` via `_h(...)` late-binding. **All customer-facing tools run under the customer-principal scope** and are the only tools bound into a customer agent profile (Vendors layer-2 pattern). AI-role tag per roadmap §2 taxonomy.

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `customer_portal_room_tracker` | ✔ | ✘ | ✘ | Advisor (customer-scoped) |
| `customer_portal_room_overview` | ✔ | ✘ | ✘ | — (read-projection) |
| `customer_portal_asset_register` | ✔ | ✘ | ✘ | — (read-projection) |
| `customer_portal_list_documents` | ✔ | ✘ | ✘ | — (read-projection) |
| `customer_portal_sla_status` | ✔ | ✘ | ✘ | Watcher (customer-scoped) |
| `customer_portal_list_invoices` | ✔ | ✘ | ✘ | — (read-projection) |
| `customer_portal_advisor_answer` | ✘ | ✘ | ✘ | Advisor (sandboxed, customer-scoped) |
| `customer_portal_raise_service_request` | ✘ | ✘ | ✔ | Actor (hand-off → Support) |
| `customer_portal_register_expansion_interest` | ✘ | ✘ | ✔ | Actor (hand-off → Sales) |

> Only these customer-safe tools are bound into a customer agent profile. **No** Sales/Project/Economy/Procurement margin/cost/internal tools are ever registered into the customer surface — there is no MCP path to privileged data regardless of prompt (Partner Access Model layer 2, generalised to external customers).

## REST routes
The customer-facing **separate app** (NOT the internal HMAC/mTLS admin app) — its own rate-limited, **customer-principal-authed** path (PR #241 split). Handlers in `nce/admin_handlers/customer_portal.py`; each resolves the customer scope internally and applies the allow-list projection before serialization.
- `api_customer_portal_login` (POST) — BankID (broker) / email magic-link → customer-principal session.
- `api_customer_portal_room_tracker` / `api_customer_portal_room_overview` (GET) — the Domino's tracker + % -ready rollup.
- `api_customer_portal_asset_register` (GET) — room-centric asset register.
- `api_customer_portal_documents` / `api_customer_portal_document` (GET) — FDV / as-built / SoW (scoped, expiring).
- `api_customer_portal_sla_status` (GET) — SLA self-service.
- `api_customer_portal_invoices` (GET) — the customer's own invoices.
- `api_customer_portal_service_request` (POST) — raise a service request → Support hand-off.
- `api_customer_portal_expansion_interest` (POST) — re-buy/expansion → Sales lead.
- `api_customer_portal_advisor` (POST) — sandboxed customer assistant.

## AI features
- **Advisor (room-status narrative):** turns projected `BOM_LINE.status` / `ASSET` state into plain language — *"your boardroom is 80% ready; the displays are installed, audio is being tested."* A customer-facing assistant answering *"when will my room be ready / how do I raise a service request"* — **scoped to the customer's own data only**, redacted toolset, prompt-injection-sandboxed.
- **Watcher (SLA self-service status):** surfaces the customer's own SLA clock / breach state read-only (no internal escalation detail).
- **Expansion / re-buy:** signals (an asset nearing EOL in a room, a frequently-serviced room) are surfaced to **Sales** (its Advisor) for a human-gated follow-up — **never pushed at the customer** as an upsell. A **churn/health score never surfaces to the customer** (roadmap §9.5).
- **Content discipline like Marketing:** any outbound customer-facing content is **human-gated**; the portal narrates state, it does not autonomously market.
- **Enrichment triggers (event-scoped, never a sweep):** the Advisor runs only on a customer request; tracker/asset projections are read-on-demand. No background generation over customer data.

## A2A flows
- **READS (projection, never writes):** **Project(7)** — room/phase status + `BOM_LINE.status`; **Assets(9)** — room asset register + warranty; **Support(10)** — the customer's own ticket status; **Agreements(3)** — SLA terms for self-service; **Economy(8)** — the customer's own invoices; **System Design(6)** — room design / SoW docs for document access.
- **WRITES only via hand-off:** `PORTAL_SERVICE_REQUEST` → **Support** `do_open_ticket` (idempotent); `EXPANSION_INTEREST` → **Sales** lead. **Never writes operational state directly** — it calls the owner's tool or files a request the owner reconciles.
- **Does not serve the Morning-brief (#19)** directly; its data is already the owner engines' — the portal is a consumer/projector, not a producer.

## Config keys
`NCE_CUSTOMER_PORTAL_*` in `nce/config.py`: `NCE_CUSTOMER_PORTAL_ENABLED`, `NCE_CUSTOMER_PORTAL_PUBLIC_BASE_URL` (the separate app), `NCE_CUSTOMER_PORTAL_AUTH_PROVIDER` (`bankid|magic_link`), `NCE_CUSTOMER_PORTAL_MAGIC_LINK_TTL_MINUTES`, `NCE_CUSTOMER_PORTAL_RATE_LIMIT_PER_MIN`, `NCE_CUSTOMER_PORTAL_DOCUMENT_GRANT_TTL_DAYS`, `NCE_CUSTOMER_PORTAL_ADVISOR_ENABLED`. The BankID broker (Criipto/Signicat) credentials are reused from the shared signing/eID rail, never re-declared (FE-5). Namespaces opt in via `metadata.customer_portal.enabled = true`.
**Config-as-IP JSON (namespace-scoped, the business IP — NOT code):**
- `customer-redaction.json` — the **allow-list** of customer-safe fields for every projection (the safe-field contract; auditable, tenant-tunable — modelled on Vendors' `partner-redaction.json`).
- `room-tracker-stages.json` — the Domino's stage labels + per-stage % -ready weighting + the `BOM_LINE.status`→stage mapping.

## Tables/migrations
**Graph-first** for the three owned nodes (`PORTAL_USER`/`PORTAL_SERVICE_REQUEST`/`PORTAL_DOCUMENT_SHARE` live as `kg_nodes`/`kg_edges`; audit in `v3_cognitive_ledger`). Own tables only where the **customer-principal RLS enforcement** and login session beat the graph — both `ENABLE` + **`FORCE ROW LEVEL SECURITY`**, mirrored into `schema.sql` + a numbered migration:
- `portal_users` (`portal_user_id, namespace_id, customer_scope_id, customer_id, auth_provider, contact jsonb, last_login_at`) — carries `customer_scope_id`; **`customer_isolation_policy USING (namespace_id = get_nce_namespace() AND customer_scope_id = get_nce_partner_scope())`** (the generalised §9.6 primitive, deny-when-unset).
- `portal_document_shares` (`share_id, namespace_id, customer_scope_id, document_ref, granted_by, expires_at, revoked_at`) — scoped, expiring grants; same `customer_isolation_policy`.
- `portal_service_requests` (`request_id, namespace_id, customer_scope_id, room_id, payload jsonb, handed_off_ticket_id, created_at`) — intake before Support's `TICKET`; same `customer_isolation_policy`.
All projection reads of *other* engines' nodes go through their owners (A2A) — the portal stores **no** copy of Project/Asset/Ticket/SLA/Invoice state.

## Dependencies
- **Upstream engines (produce the data the portal projects):** Project(7) (BOM_LINE status / room phase), Assets(9) (room asset register), Support(10) (ticket status + SLA clock), Agreements(3) (SLA terms), Economy(8) (invoices), System Design(6) (room design/SoW docs). Hence **Tier 4** — it needs these producing before its surfaces have content.
- **Shared-core prerequisites (build + harden in core first):** the **§9.6 partner-scope RLS primitive generalised to external customer principals** (security-reviewed; Customer Portal is its second heavy client after Field Tech); the BankID broker rail (§2.10 carve-out); the reactive graph-event mechanism (§9.6) if the tracker is push-updated rather than read-on-demand.
- **External / compliance blocker 🔴:** **DPIA / GDPR (the reference implementation Blocker #10, DPIA-blocked to May 2026 — likely clearing as of June 2026)** — a hard gate before production customer data flows. The customer-principal RLS extension is a **core security-review gate**, not vertical work.
- **Boundary (do NOT duplicate):** the portal **owns** only `PORTAL_USER` / `SERVICE_REQUEST` intake / `DOCUMENT_SHARE`. It **projects** Project/Asset/Ticket/SLA/Invoice/Design and **hands off** writes — it is **not** a system of record for any operational node.

## Honest assessment (these govern the build)
- **The security boundary IS the build — not the features.** The room tracker, asset register, and service-request UX are straightforward projections; the hard, load-bearing, expensive-to-retrofit work is the **external customer-principal RLS scope, the allow-list redaction, the separate rate-limited app, and the sandboxed AI** — that is where engineering and security-review effort goes.
- **DPIA is a gating dependency.** No production customer personal data until the DPIA clears (Blocker #10) — design personvern-by-design from B1.
- **The existing poor portal is replaced, not extended** (§2.10) — its UX intent is the spec, its code is discarded.

## Review round-2 hardening (2026-06-17 — these govern the build)
Most security-forward spec in the suite (security *is* the build). Four sharpenings:
1. **Generalising §9.6 to an external CUSTOMER principal ESCALATES the core primitive's threat model — say so explicitly.** A contractor is a known counterparty *under contract*; a customer-portal login is **internet-facing and potentially adversarial** (credential attacks, **IDOR via the scope GUC**, account enumeration, session fixation, prompt-injection on the assistant). So the §9.6 primitive now has **three principal tiers — employee (namespace) · contractor (partner-scope) · external customer (internet-facing)** — and its security-review **must explicitly cover the adversarial-external threat model**, not just the contractor case. Customer Portal is the **second engine making the primitive load-bearing (after Field Tech) and the more dangerous one** — the generalization *raises* the hardening bar, it doesn't reuse it for free.
2. **The happy-path Domino's tracker is easy; the messy-reality projection is the real design.** "Never show internal slip" is right, but **the status progression itself leaks**: a room frozen at `Ordered` for 3 months signals trouble with no explicit field; a tracker that **regresses** (a `CHANGE_ORDER` removed a line, a return) exposes internal churn. Define what the customer sees under **delay / CO / partial-delivery / regression** — not just `Planned→…→Ready`. The messy cases are where "never leak internal reality" is actually tested (`room-tracker-stages.json` must encode the customer-safe projection of *each* of those, e.g. a CO shows as a neutral "scope updated", not a regression).
3. **The intake→ticket→customer-status loop is a mini two-master — define the status-sync.** `SERVICE_REQUEST -[becomes]-> TICKET` (portal owns intake, Support owns the ticket) is clean, but the customer wants progress: define **what they see between "request raised" and "ticket opened,"** and **how a Support close/merge/reject reflects back** to their service-request view. A small two-master between the portal request and the Support ticket — give it an explicit, customer-safe status-projection (not the raw Support ticket state).
4. **DPIA is BINARY (go/no-go), not "likely."** B1 builds the security spine on **synthetic/staff data**; the **DPIA sign-off is a hard gate before any real customer logs in** — do not let "likely clearing June 2026" soften into an assumption. No production customer personal data flows until DPIA is signed off, full stop.

## Build phases
- **B1 — Customer-principal scope + the separate app shell (the security spine, ahead of features):** generalise the §9.6 partner-scope RLS to an **external customer principal** (with NCE core); `portal_users` + `customer_isolation_policy` (deny-when-unset); the **separate rate-limited, customer-principal-authed app** (PR #241 split) with BankID-broker / magic-link login; the **allow-list redaction** harness (`customer-redaction.json`). Personvern-by-design + DPIA alignment from day one. **No customer data flows until the RLS scope + DPIA gate are green.**
- **B2 — Room-centric read projections:** `do_room_tracker` / `do_room_overview` (Domino's tracker over `BOM_LINE.status` + `ASSET`, `room-tracker-stages.json`); `do_asset_register`; REST + MCP for each, all allow-list projected. Grace-degradation: the tracker works as soon as Project produces `BOM_LINE.status`; the asset register as soon as Assets seeds.
- **B3 — Post-handover surfaces (the lock-in value):** `do_list_documents`/`do_get_document` (`PORTAL_DOCUMENT_SHARE`, scoped + expiring); `do_sla_status` (SLA self-service over the 4-way `SLA` — Agreements terms + Support clock); `do_list_invoices` (Economy). Each surface lights up as its owner engine goes live.
- **B4 — Inbound customer actions (hand-offs):** `do_raise_service_request` → Support `do_open_ticket` (idempotent, Contract-B gated); `do_register_expansion_interest` → Sales lead (human-gated at Sales). Customer-principal audit trail to the ledger.
- **B5 — Sandboxed AI surface:** the customer-facing **Advisor** (room-status narrative + "when ready / how to raise a request", prompt-injection-sandboxed, customer-scoped, redacted toolset); SLA-status **Watcher**; expansion/re-buy signals surfaced to Sales' Advisor (never pushed at the customer; churn/health score never shown).
