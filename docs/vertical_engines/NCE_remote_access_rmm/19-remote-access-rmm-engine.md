> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 19 — Remote Access & RMM Engine  (nce/vertical_modules/remote_access)

**Status:** spec (Tier 3 — Operations axis) · **Owner:** NCE core (Sindre)
**Pattern companions:** `docs/VERTICAL_MODULE_PATTERN.md`, `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§2 AI-role taxonomy, §4 graph catalogue, §7 spec format, §9 contracts), `docs/DATA_SOURCE_MODES.md` (`gotoresolve|both|nce` per-function switch), `10-support-engine.md` (downstream), `09-assets-engine.md` (reconciliation target), `12-field-tech-engine.md` (deflection target), `NCE_remote_access_rmm/19a-gotoresolve-api-reference.md` (the vendor API map), `NCE_remote_access_rmm/19c-security-legal-ops.md` (the remote-action threat model).

## Mission
Turn every managed endpoint into something the platform can **see, monitor, and fix without a truck-roll** — and make every remote fix a fact the business remembers. Example installs AV endpoints (room controllers, signage players, codecs, NUCs, servers) that live in rooms and fail like any computer. GoTo Resolve / LogMeIn Resolve is the **remote-hands data plane**: unattended access, live remote control, RMM device monitoring, proactive alerts (CPU/mem/disk/offline/patch/AV), and remote script execution. This engine wraps that as a first-class NCE capability so an AI can (a) **watch** the RMM alert stream and open a proactive Support ticket *before the customer notices*, (b) **advise** — recall which remote session/script fixed this exact symptom on this device family before, and (c) **act** — launch a session, run an allowlisted remediation, or, when remote fails, hand a fully-contexted work order to Field Tech. It directly closes **Support(10)'s Troubleshooter fix-attribution gap**: a GoTo remediation ("rolled NVX firmware 3.1→2.4 on ASSET-x, alert cleared") is exactly the *structured resolution* the ledger was missing. The deep-AI angle is **remote-first deflection**: resolve remotely, dispatch only when you must — the single clearest ROI line in the whole engine set.

## Inspiration & triage
- **Example drift thesis (Andreas module 12 Operations/Drift):** proactive monitoring via manufacturer/endpoint APIs, driftsavtaler that follow the ROOM not the customer. RMM is the *generic* endpoint-telemetry source that complements Assets(9)'s per-manufacturer adapters (Cisco xAPI, QSC Reflect, Neat Pulse, …): GoTo Resolve monitors the *compute* (the player/controller/NUC), the manufacturer APIs monitor the *AV device*.
- **Already-shipped NCE seed to reuse (no rebuild):** the `dynamics365` vertical's **OAuth token manager (`DataverseTokenManager`, Redis-cached), resilient HTTP client, and delta-watermark sync (`d365_sync_runs`)** — GoTo Resolve is the same shape (OAuth 2.0 Bearer + REST + incremental pull + webhook push). And the **Support(10) proactive-ticket path** (`ASSET -[monitored_by]-> TELEMETRY` crossing threshold → `do_open_ticket(origin=proactive_telemetry)`): an RMM alert is a second producer on that identical path.
- **Lysning page served:** an "Endpoints / Remote Ops" surface — managed-device health board, alert queue, one-click *launch remote session*, remote-deflection scorecard — consuming the no-model REST surface.
- **Vendor:** GoTo Resolve / LogMeIn Resolve (rebranded 2025). API at `developer.goto.com` (OAuth 2.0, OpenAPI + Postman). See `19a` for the concrete surface + confidence caveats.

## Classification
**push + semantic, with a vendor-bound data plane.** External system: **GoTo Resolve API** (OAuth 2.0 authorization-code for operator-context actions; client-credentials / service account for backend sync — Bearer token in `Authorization`). Incremental pull of device inventory + alerts + session history via the delta-watermark pattern (`rmm_sync_runs`, mirroring `d365_sync_runs`); real-time via GoTo's **Notification Channel** webhooks (session-started/ended, alert-raised/cleared) — HMAC-validated in `webhooks.py` exactly like D365. Semantic track: session notes + chat transcript + script stdout/stderr → `memories` (embedding + `content_fts`) + resolution facts → `v3_cognitive_ledger` (reuses the ingestion worker shape).

**The honest asymmetry vs Sales/Support's source switch.** The `gotoresolve|both|nce` switch (`DATA_SOURCE_MODES.md`) governs the **record & recall layer** — NCE retains ingested sessions/alerts/scripts, so recall + reporting flip to `nce` with no migration. But the **live data plane is inherently vendor-bound**: you cannot open a remote session or execute a script on an endpoint without the vendor's agent. So `nce` mode means *"read our retained history + graph natively"*, never *"remote-control without GoTo"*. Live Actor tools always route to the active vendor adapter; only the read/recall/report functions are truly source-switchable. This is stated up front so no one specs a fictional native remote-control path.

## Graph contribution
Node `entity_type` prefixes: `RMM_*`, plus shared spine nodes `ASSET`, `ROOM`/`FUNCTIONAL_LOCATION`, `TICKET`, `WORK_ORDER`, `EMPLOYEE`, `CUSTOMER`. **A GoTo-managed endpoint is an `ASSET`** (owned by Assets(9)) — this engine references it and contributes observability + a management marker; it **never** creates a competing device node (the netops(18)/Contract-A rule).
- **Nodes:**
  - `RMM_SESSION` — a remote-control / unattended session: technician, endpoint, start/end, attended|unattended, notes ref, recording ref, outcome.
  - `RMM_ALERT` — an RMM alert instance: policy, metric (cpu/mem/disk/offline/patch/av), severity, raised/cleared timestamps, source device.
  - `RMM_SCRIPT_RUN` — a remote execution / automation run: script id, target endpoint, exit code, stdout/stderr ref, initiated_by, autonomy tier used.
  - `RMM_AGENT` — the unattended-access agent's presence + posture on an endpoint (version, last-seen, patch/AV status). Coverage of `RMM_AGENT` over `ASSET` is the "monitoring blind-spot" signal.
- **Edges (our §4 slice):**
  - `RMM_SESSION -[on]-> ASSET -[lives_in]-> ROOM` — session targeted an endpoint in a room (room-centric, shared with Assets(9)/Support(10)).
  - `RMM_SESSION -[performed_by]-> EMPLOYEE` — technician (boundary edge to HR(13); Field Tech(12) also reads it).
  - `RMM_SESSION -[resolves]-> TICKET` — the loop-closer: a session tied to a Support ticket; its outcome becomes Troubleshooter recall.
  - `ASSET -[managed_by]-> RMM_AGENT` — endpoint has the unattended agent (absence over an `ASSET` = coverage gap).
  - `RMM_ALERT -[on]-> ASSET` and `RMM_ALERT -[opened]-> TICKET` — alert crossing policy authors a proactive `TICKET` (the Support proactive path; the netops fault→ticket precedent).
  - `RMM_SCRIPT_RUN -[on]-> ASSET` and `RMM_SCRIPT_RUN -[remediates]-> RMM_ALERT` — the execution and what it cleared (the structured fix fact).
- **memories/ledger:** session notes + chat + script output → `memories` (embedding + `content_fts`) for recall; **every remediation** (which script/firmware/action cleared which alert on which asset family) → `v3_cognitive_ledger` as a **structured resolution fact** — this is the data Support(10)'s Troubleshooter needs and today lacks. Tag every derived row with `rmm_source_id` (vendor session/alert id) for hard-retirement on delete (the D365 retirement pattern).

## Core functions
Pure-ish `do_<action>(engine, params) -> dict`; reads are deterministic over vendor+graph; live actions route to the active vendor adapter and pass through the C2 autonomy wrapper.
- `do_list_endpoints(engine, params) -> dict` — `{filter?}` → GoTo device inventory **reconciled to `ASSET`** (C1 entity resolution: hostname/serial/MAC → ASSET), with `RMM_AGENT` posture (version, last-seen, patch/AV/online). Advisor/Watcher. Surfaces the **coverage gap** (ASSETs with no agent).
- `do_query_session(engine, params) -> dict` — `{session_id}` → `RMM_SESSION` + graph context (asset, room, linked ticket, scripts run).
- `do_list_alerts(engine, params) -> dict` — `{status?, severity?}` → active `RMM_ALERT`s normalized (policy → severity → asset → room), each with proactive-ticket eligibility from `rmm-alert-policy-map.json`. Watcher.
- `do_session_recall(engine, params) -> dict` — **the headline.** `{asset_id | {device_family, symptom_text}}` → recall top-N prior `RMM_SESSION`/`RMM_SCRIPT_RUN` on the same asset/device family **with their recorded outcomes from `v3_cognitive_ledger`**, returning `{prior_fixes[], recommended_action, confidence, cited_session_ids}`. Advisor. This is the function Support(10)'s `do_troubleshoot` calls to get **fix attribution**, not just similar-ticket text.
- `do_launch_session(engine, params) -> dict` — `{asset_id, mode: attended|unattended, reason, ticket_id?}` → create a GoTo session (join link for attended; initiate for unattended), write `RMM_SESSION -[on]-> ASSET` (+ `-[resolves]-> TICKET`). **Actor (human-confirmed).**
- `do_run_remediation(engine, params) -> dict` — `{asset_id, script_id, alert_id?}` → execute an **allowlisted** remote script/automation, write `RMM_SCRIPT_RUN` (+ `-[remediates]-> RMM_ALERT`), capture stdout/stderr → ledger, then **verify** (see below). **Actor → Autonomous only under the strict Contract-B gate; see `19c` and the smart-features `19b`.** *The sharpest action in the platform.* Two smart-feature behaviours are **built into this function, not bolted on:**
  - **Graph-computed blast-radius gate (19b F1):** before executing, walk `ASSET → PORT/SIGNAL_CHAIN → SPOF` (System Design(6)) + the `SLA` clock to compute how many downstream endpoints / live signal paths the action jeopardizes. `NCE_RMM_AUTONOMY_BLAST_RADIUS_MAX` is a **ceiling on top of** the computed radius, not the whole gate. (v1 = topology + SLA, graph-native; "room in use *right now*" is a v2 enrichment via an Outlook room-mailbox adapter — there is no native booking source.)
  - **Post-remediation verification (19b F12):** after the run, `compare_states` the asset's graph state pre/post + confirm the target `RMM_ALERT` cleared (webhook) and **no new alert regressed**; return `verified: bool`. Only a `verified` run becomes a trustworthy fix-fact for the self-writing playbook. Exit-code 0 alone is **not** "fixed".
- `do_explain_remediation(engine, params) -> dict` — `{script_run_id}` → the cited "why" chain (prior sessions, policy, allowlist entry + its provenance) via `explain_past_decision`/`get_event_provenance` (19b F13). Advisor.
- `do_propose_playbook_entry(engine, params) -> dict` — deterministic aggregation over `v3_cognitive_ledger` `(alert-pattern → script → cleared+verified)` fix-facts; where success-rate over N crosses a threshold, **propose** an `rmm-remediation-scripts.json` entry with `provenance`, `ratified_by=null`, `autonomy_eligible=false` (19b F3). **Never** self-ratifies. Advisor.
- `do_deploy_agent(engine, params) -> dict` — `{asset_id}` → ensure/deploy the unattended-access agent on an endpoint, write/refresh `ASSET -[managed_by]-> RMM_AGENT`. Actor (human-confirmed) — closes coverage gaps.
- `do_dispatch_to_field(engine, params) -> dict` — `{session_id|ticket_id, reason}` → when remote fails, emit `TICKET -[dispatched_as]-> WORK_ORDER` **with the session/script context attached** so Field Tech(12) arrives pre-briefed. Actor.
- `do_record_session_outcome(engine, params) -> dict` — `{session_id, outcome, root_cause, fix_applied}` → append the **structured resolution** to `v3_cognitive_ledger` (the resolution-capture discipline). Actor.
- `do_sync_now(engine, params) -> dict` — incremental device/alert/session pull (delta watermark) + webhook backfill; runs the alert→proactive-ticket sweep. Operator.

## MCP tools
Registered in `nce/tool_registry.py` via `_h(...)` late-binding. AI-role tag per roadmap §2 taxonomy.

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `rmm_list_endpoints` | ✔ | ✘ | ✘ | Watcher |
| `rmm_query_session` | ✔ | ✘ | ✘ | Advisor |
| `rmm_list_alerts` | ✔ | ✘ | ✘ | Watcher |
| `rmm_session_recall` | ✔ | ✘ | ✘ | Advisor |
| `rmm_explain_remediation` | ✔ | ✘ | ✘ | Advisor |
| `rmm_propose_playbook` | ✘ | ✔ | ✔ | Advisor (proposes only — never ratifies) |
| `rmm_launch_session` | ✘ | ✔ | ✔ | Actor (human-confirmed) |
| `rmm_run_remediation` | ✘ | ✔ | ✔ | Actor (Autonomous: allowlist + ceiling only) |
| `rmm_deploy_agent` | ✘ | ✔ | ✔ | Actor (human-confirmed) |
| `rmm_dispatch_to_field` | ✘ | ✔ | ✔ | Actor |
| `rmm_record_session_outcome` | ✘ | ✘ | ✔ | Actor |
| `rmm_sync_now` | ✘ | ✔ | ✔ | — (operator) |

## REST routes
No-model path for the BFF (Endpoints/Remote-Ops surface), cron, scripts. Mounted via `build_app(extra_routes=...)`; HMAC/mTLS-authed in `nce/admin_handlers/remote_access.py`:
- `api_rmm_list_endpoints` (GET) — managed-device health board + coverage gaps.
- `api_rmm_list_alerts` (GET) — live RMM alert queue for the board.
- `api_rmm_query_session` (GET) — session detail + graph context.
- `api_rmm_session_recall` (POST) — prior-fix recall for the agent UI (also called internally by Support Troubleshooter).
- `api_rmm_launch_session` (POST) — one-click remote session (gated).
- `api_rmm_run_remediation` (POST) — execute allowlisted remediation (gated, hardest path).
- `api_rmm_deploy_agent` (POST) — close a coverage gap (gated).
- `api_rmm_dispatch_to_field` (POST) — remote→truck-roll handoff with context.
- `api_rmm_sync_status` / `api_rmm_sync_now` — feed health + delta-run history (mirrors `d365_sync_status`).
- `api_rmm_webhook` (POST) — GoTo Notification Channel receiver (HMAC-validated), fans session/alert events into the graph + proactive-ticket path.

## AI features
- **Watcher:** RMM alert crossing policy → **proactive Support ticket** (before the customer notices); endpoint offline / missing-patch / AV-disabled detection; **coverage-gap watcher** — an `ASSET` under a driftsavtale with no `RMM_AGENT` is a monitoring blind spot to flag.
- **Advisor:** **remote-fix recall** — for an asset/symptom, surface prior sessions + the scripts/actions that cleared the same alert on the same device family, cited ("2 prior 'disk-full' alerts on this signage-player image cleared by `clear-cache-v3.ps1` — confidence 0.86"); recommend a remediation script for an alert pattern; triage which alerts are auto-remediable vs need a human.
- **Actor:** launch a remote session, deploy an agent, dispatch to Field Tech with context, record the structured outcome — all human-confirmed.
- **Autonomous (gated — the strictest in the platform):** auto-run a remediation **only** if the script is on the allowlist, marked idempotent-and-reversible, the alert pattern is unambiguous, and the target is within the blast-radius ceiling (`NCE_RMM_AUTONOMY_*` + `rmm-remediation-scripts.json`); auto-open the proactive ticket. **Remote code execution on a customer endpoint is anti-mission if wrong** → per-script allowlist, kill-switch, WORM audit, and the longest human-confirmation tenure of any tool (see `19c`).
- **Cognitive recall:** outcomes live in `v3_cognitive_ledger`, so an agent can ask *why* a remediation was proposed and which prior sessions back it — and this is the fix-attribution stream Support(10)'s Troubleshooter consumes.
- **Enrichment triggers (event-scoped, never a background sweep):** recall runs *only* on a session launch or a Support Troubleshooter request; alert→ticket fires *only* on a webhook/delta alert event. Never bulk-rescore all endpoints.

## A2A flows
- **Consumes Assets(9):** `ASSET` identity for reconciliation (GoTo device ↔ ASSET via C1) and `ASSET -[lives_in]-> ROOM` for room-centric alerting. Contributes observability back (`managed_by`, alert/session/script edges); never owns the asset.
- **Feeds Support(10):** (1) `RMM_ALERT -[opened]-> TICKET` on the proactive path Support already built; (2) **structured resolution facts** from `RMM_SESSION`/`RMM_SCRIPT_RUN` → Support's Troubleshooter, directly resolving its hardening-#2 fix-attribution gap. Support's `do_troubleshoot` calls `rmm_session_recall`.
- **Deflects Field Tech(12):** remote-first — a session/remediation resolves before a truck rolls; when remote fails, `do_dispatch_to_field` emits `TICKET -[dispatched_as]-> WORK_ORDER` with session context attached (Field Tech owns the WO lifecycle + `assigned_to`).
- **Boundary to HR(13):** `RMM_SESSION -[performed_by]-> EMPLOYEE` (technician master data is HR's).
- **Feeds Business Insights(16) / #19 Morning-brief:** endpoints-at-risk, unmanaged-endpoint coverage gap, and the **remote-deflection rate** (truck-rolls avoided) — the operations ROI slice of the cross-engine "1 risk + 1 opportunity" query.

## Config keys
`NCE_RMM_*` in `nce/config.py` (secrets via the `*_FILE` seam — see the NCE signing runbook, never plaintext in git): `NCE_RMM_ENABLED`, `NCE_RMM_VENDOR` (`gotoresolve`, extensible), `NCE_RMM_SOURCE_MODE` (`gotoresolve|both|nce`, per-function-overridable — governs the record/recall layer only), `NCE_RMM_OAUTH_CLIENT_ID`, `NCE_RMM_OAUTH_CLIENT_SECRET_FILE`, `NCE_RMM_BASE_URL`, `NCE_RMM_ACCOUNT_KEY`, `NCE_RMM_WEBHOOK_SECRET_FILE`, `NCE_RMM_SYNC_INTERVAL_MINUTES`, `NCE_RMM_PAGE_SIZE`, `NCE_RMM_RECALL_N` (default 5), `NCE_RMM_RECALL_MIN_CONFIDENCE`, `NCE_RMM_AUTONOMY_REMEDIATE_ENABLED` (default false), `NCE_RMM_AUTONOMY_REMEDIATE_CONFIDENCE`, `NCE_RMM_AUTONOMY_BLAST_RADIUS_MAX` (hard ceiling **on top of** the graph-computed blast radius — F1, never the whole gate), `NCE_RMM_PLAYBOOK_MIN_RUNS` + `NCE_RMM_PLAYBOOK_MIN_SUCCESS_RATE` (F3 proposal thresholds over verified fix-facts), `NCE_RMM_SESSION_RECORDING_RETENTION_DAYS`. Add a `validate_rmm_config` for the prod-required OAuth/webhook keys. **Never** put a host-specific key here (NCE-FE-5). Namespaces opt in via `metadata.remote_access.enabled = true`.
**Config-as-IP JSON (namespace-scoped — the business IP, NOT code):**
- `rmm-remediation-scripts.json` — the **allowlist**: per-alert-pattern the approved script id, its idempotent/reversible flags, blast-radius ceiling, and autonomy eligibility. Entries carry `provenance`, `ratified_by`, `autonomy_eligible` — **machine-proposed entries (19b F3) are `autonomy_eligible=false` until a human ratifies them** (the dangerous-composition rule: never let a self-written entry auto-apply, least of all fleet-wide). The single most security-sensitive config in the platform; changes are WORM-audited.
- `rmm-alert-policy-map.json` — GoTo alert policy → Support priority/SLA tier + proactive-ticket + auto-remediate eligibility.

## Tables/migrations
**Graph-first** for SESSION/ALERT/SCRIPT_RUN/AGENT nodes + edges; resolutions/outcomes in `v3_cognitive_ledger`; session text in `memories`. Own tables only where a keyed, high-write-rate lookup beats the graph — all `ENABLE` + `FORCE ROW LEVEL SECURITY` + `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`, mirrored into `schema.sql` + a numbered migration (next-free per `migrations.md` at build time):
- `rmm_endpoints` (`id, vendor, vendor_device_id, asset_id, hostname, os, agent_version, last_seen_at, online, patch_status, av_status, rmm_source_id, synced_at`) — the GoTo device ↔ `ASSET` mapping + posture; fast health-board + coverage-gap reads.
- `rmm_alerts` (`id, vendor_alert_id, endpoint_id, asset_id, policy, metric, severity, raised_at, cleared_at, ticket_id, rmm_source_id`) — fast alert-queue reads + proactive-ticket dedup.
- `rmm_sessions` (`id, vendor_session_id, endpoint_id, asset_id, technician_employee_id, mode, ticket_id, started_at, ended_at, outcome, notes_ref, recording_ref, rmm_source_id`) — normalized session record for the `nce` recall mode.
- `rmm_script_runs` (`id, vendor_run_id, endpoint_id, asset_id, script_id, alert_id, initiated_by, autonomy_tier, exit_code, output_ref, ran_at, rmm_source_id`) — the WORM-adjacent execution audit (the sharpest-action ledger).
- `rmm_sync_runs` (delta watermark; mirrors `d365_sync_runs`).

## Dependencies
- **Upstream engines:** Assets(9) — `ASSET`/`ROOM` identity for reconciliation and room-centric alerting (this engine consumes, never owns assets); Agreements(3) — driftsavtale terms tell the coverage-gap watcher *which* assets are supposed to be managed; HR(13) — technician `EMPLOYEE` master data.
- **Downstream consumers:** Support(10) — proactive tickets + Troubleshooter fix facts (the primary consumer); Field Tech(12) — dispatched work orders with session context; Business Insights(16)/#19 — the at-risk + deflection aggregate.
- **Already-shipped seed (no rebuild):** the `dynamics365` vertical's OAuth token manager + resilient HTTP client + delta-watermark sync + HMAC webhook validator (GoTo Resolve is the same OAuth-2.0-Bearer + REST + webhook shape); the Support(10) proactive-ticket path.
- **External blocker 🔴 / confidence:** the exact GoTo Resolve endpoint paths, request/response shapes, OAuth scopes, and Notification-Channel event names must be confirmed against the live OpenAPI + Postman collection at `developer.goto.com/LogMeInResolve` before B1 — the developer portal is JS-rendered and could not be scraped statically. `19a` documents what is known + conventions + every item to confirm. The client is built mock-with-swap (like Assets' manufacturer adapters) so read/recall features are usable on a fixture and flip to live on credential + endpoint confirmation.

## Review-round hardening (these govern the build)
0. **Dangerous composition — the governing rule (19b).** The self-writing playbook (`do_propose_playbook_entry`) and fleet fan-out are individually valuable and jointly catastrophic: a machine-proposed allowlist entry auto-applied fleet-wide is *pushing unratified code to the whole estate from one inferred pattern*. Hard rules: (a) machine-proposed entries are **never** `autonomy_eligible` until a human ratifies; (b) fleet remediation is **always** human-confirmed and hard-capped by `BLAST_RADIUS_MAX` regardless of confidence; (c) the **graph-computed** blast-radius gate runs on every remediation, single or fleet, and each fleet target is checked individually. This supersedes every convenience.
1. **Remote script execution is the sharpest action in the platform — it runs code on a customer machine.** Sharper than Procurement PO-submit or Support auto-close. It gets the strictest Contract-B: per-script allowlist (`rmm-remediation-scripts.json`), idempotent-and-reversible requirement, **graph-computed** blast-radius gate (not a static integer alone), kill-switch, post-run `compare_states` verification, and full WORM audit — and stays human-confirmed the **longest** of any tool. Autonomy is **off by default** (`NCE_RMM_AUTONOMY_REMEDIATE_ENABLED=false`). See `19c`.
2. **Session recall may recall SESSIONS but not FIXES at launch — and the fix is the value.** GoTo notes are free text; a *structured* "what fixed it" only exists once `do_record_session_outcome` discipline is in place. Honest ramp: **similarity/session recall day one, structured fix-recall as outcomes accumulate** — identical to Support's own resolution-capture ramp, which is exactly why this engine's structured outcomes are the thing that finally makes Support's Troubleshooter work.
3. **`nce` source mode is record-only, not a native remote-control path.** The data plane is vendor-bound; do not spec or promise remote control without an agent. The switch flips recall/reporting, not the live actions.
4. **Reconcile, never duplicate, the device.** A GoTo-managed endpoint is an `ASSET` (Assets(9)/netbox own it). C1 entity resolution matches on hostname/serial/MAC; on ambiguity, log a §9.2 divergence, don't fork a node. (The netops(18) discipline verbatim.)
5. **Remote access is consent- and legally-sensitive.** Attended vs unattended has different consent posture; sessions can be recorded. C2 (autonomy), C3 (external-principal RLS — the technician/vendor is a scoped principal), C8 (config) plus a consent/legal gate and recording-retention policy (`19c`). Mirror GoTo's zero-trust signature-key model for command execution — every session and every execution is WORM-logged.
6. **Grace-degradation — read/monitor core ships standalone; live actions layer on C2.** B1–B3 (inventory + alerts + recall + proactive tickets) ship on the read path with no autonomy surface. B4+ (launch/remediate/deploy) require the C2 autonomy foundation + `19c` gate to be live. Making the line explicit so the read value ships early and the sharp actions wait for their guardrails.

## Build phases
Smart features (`19b-smart-features.md`) are woven in per phase; those blocked on an unbuilt engine ship their standalone slice now and enrich later.
- **B1 — Adapter + inventory + reconciliation:** OAuth token manager (reuse D365 shape, `*_FILE` secrets) + resilient client + `rmm_endpoints`/`rmm_sync_runs` tables (RLS); `do_list_endpoints` with C1 `ASSET` reconciliation + `RMM_AGENT` posture; `do_sync_now` delta pull. MCP + REST for the read surface. Mock-with-swap client. **Smart:** F8 coverage-gap blind-spot detection.
- **B2 — Alerts + proactive tickets:** `rmm_alerts` table; Notification Channel `webhooks.py` (HMAC) + delta alert pull; `do_list_alerts`; `rmm-alert-policy-map.json` wired; `RMM_ALERT -[opened]-> TICKET` on the Support(10) proactive path. **Smart:** F7 drift detection (vs System Design/config intent), F9 RMM-only trend prediction, F6 empathic-priority queue *if Support(10) health is live* (else deferred).
- **B3 — Session ingestion + recall (the headline):** `rmm_sessions`/`rmm_script_runs` tables; ingest session notes/transcript → `memories` + `v3_cognitive_ledger`; `do_query_session`, `do_session_recall`, `do_record_session_outcome`; wire `rmm_session_recall` into Support(10)'s Troubleshooter. Structured-outcome discipline established. **Smart:** F2 session-briefing card (graceful-degrade), F3 self-writing playbook *proposals* (never ratified), F5 replay root-cause (RMM + live-engine events; Field-Tech-WO correlation deferred until Field Tech(12) emits events), F16 anomaly-shaped recall.
- **B4 — Live actions (behind C2 + 19c gate):** `do_launch_session`, `do_deploy_agent`; C2 autonomy wrapper + human-confirmation; WORM audit; consent/legal + recording-retention gate. **Smart:** F1 graph-computed blast-radius gate, F10 signed-WORM audit record (portal exposure deferred to Portal(17)), F11 scoped A2A contractor grant, F15 signed per-session authorization token.
- **B5 — Remediation + autonomy (the sharpest, most-gated):** `do_run_remediation` with `rmm-remediation-scripts.json` allowlist, idempotent/reversible enforcement, **graph-computed** blast-radius gate + `BLAST_RADIUS_MAX` ceiling, kill-switch; autonomy off by default; full `19c` threat model applied. **Smart:** F12 `compare_states` post-remediation verification, F13 explain-this-remediation, F4 fleet fan-out (human-confirmed + hard-capped, ratified-only entries), F14 drift auto-reconcile.
- **B6 — Deflection loop + brief:** `do_dispatch_to_field` with session context → Field Tech(12); expose endpoints-at-risk + coverage-gap + remote-deflection-rate aggregate to Business Insights(16)/#19 Morning-brief. **Smart (blocked-on, land when the engine exists):** F8 billing reconciliation (Agreements(3)), F9 telemetry fusion (Assets(9) adapters), F10 portal transparency (Portal(17)), F1 "room-live-now" (Outlook room-mailbox adapter).
