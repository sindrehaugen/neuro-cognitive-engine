# 19a — GoTo Resolve / LogMeIn Resolve API reference (as investigated)

**Status:** research → adapter map · **Owner:** NCE core (Sindre) · **Companion of:** `19-remote-access-rmm-engine.md`
**Confidence:** MIXED — read the §Confidence & what-to-confirm block before implementing. The GoTo developer portal (`developer.goto.com/LogMeInResolve`) is a JS-rendered SPA that serves its OpenAPI spec + Postman collection dynamically, so exact endpoint paths, request/response schemas, OAuth scope strings, and Notification-Channel event names **could not be scraped statically** and must be confirmed against the live spec. What is stated here is grounded in: GoTo's published OAuth model, the LogMeIn Resolve product capability set, and GoTo's cross-product API conventions (GoTo Connect / Webinar / the Notification Channel API all share the same auth + webhook shape).

## 1. Product → API mapping (what the engine wraps)

GoTo Resolve / LogMeIn Resolve (rebranded from GoTo Resolve in 2025; itself the successor to GoToAssist) is a unified IT-management platform. The capabilities the engine consumes:

| Product capability | What it gives NCE | Engine function |
|---|---|---|
| **Remote support sessions** (attended / ad-hoc) | Live remote-control session lifecycle + join link | `do_launch_session(mode=attended)` |
| **Unattended access** | Connect to an endpoint with no user present; agent presence | `do_launch_session(mode=unattended)`, `do_deploy_agent` |
| **Device / endpoint inventory (RMM)** | Managed-device list, hardware/software inventory, online/last-seen | `do_list_endpoints` |
| **Alerts / alert policies** | Proactive CPU / memory / disk / offline / inventory / patch / AV alerts | `do_list_alerts`, alert→ticket path |
| **Remote execution / automation** | Run scripts, background terminal, background file manager | `do_run_remediation` |
| **Patch management** | Patch status per endpoint | endpoint posture (`rmm_endpoints.patch_status`) |
| **Antivirus** | AV enabled/state per endpoint | endpoint posture (`rmm_endpoints.av_status`) |
| **Users / technicians** | Technician identity for `performed_by` | `RMM_SESSION -[performed_by]-> EMPLOYEE` |
| **Notification Channel (webhooks)** | Real-time session + alert events | `api_rmm_webhook`, `webhooks.py` |

## 2. Authentication (OAuth 2.0 — high confidence)

GoTo uses **OAuth 2.0** across all products via a single authorization server. Confirmed model:
1. Create an **OAuth client** in the GoTo developer console → get `client_id` + `client_secret`. The client is bound to the GoTo products/scopes you select.
2. Obtain an **access token**, used as `Authorization: Bearer {access_token}` on every API call.
3. Two grant paths, both applicable here:
   - **Authorization Code** (user context) — for operator-initiated actions that must act *as a technician* (launching a session, running a remediation). Yields a refresh token.
   - **Client Credentials / service account** (backend context) — for unattended backend sync (inventory + alert + session-history pull). Preferred for `do_sync_now`.
4. Token refresh via the standard OAuth refresh-token flow; cache tokens in **Redis** exactly like `DataverseTokenManager` (the D365 vertical) — reuse that class shape.

**NCE mapping:** `auth.py:RmmTokenManager` (Redis-cached, dual-grant); secrets via the `*_FILE` seam (`NCE_RMM_OAUTH_CLIENT_SECRET_FILE`), never plaintext (per the NCE signing runbook).

> Auth references: [How to create an OAuth client](https://developer.goto.com/guides/Get%20Started/02_HOW_createClient/) · [How to obtain an OAuth access token](https://developer.goto.com/guides/Authentication/03_HOW_accessToken/) · [Authentication API](https://developer.goto.com/Authentication)

## 3. API surface areas (paths INFERRED — confirm against OpenAPI)

The following resource groups are expected from the capability set and GoTo's REST conventions (versioned base path, JSON, cursor/offset pagination, `Bearer` auth). **Treat every path as a placeholder to confirm against the live OpenAPI/Postman.**

- **Sessions** — `POST` create session (`{deviceId|hostId, type: attended|unattended}`) → returns session id + join/host URL; `GET` session status/detail; `GET` list session history (for ingestion). Maps to `RMM_SESSION`.
- **Devices / endpoints** — `GET` list managed devices (paginated) with hardware/software inventory, online state, last-seen, agent version; `GET` device detail. Maps to `rmm_endpoints` + `RMM_AGENT`, reconciled to `ASSET`.
- **Alerts** — `GET` list alerts (filter by status/severity/device); `GET`/`PUT` alert policies. Maps to `RMM_ALERT`.
- **Remote execution / automation** — `POST` run script/automation on a device (`{deviceId, scriptId|command}`) → run id; `GET` run result (exit code, output). Maps to `RMM_SCRIPT_RUN`. **This is the endpoint behind the sharpest action — treat with the `19c` gate.**
- **Patch / antivirus** — `GET` patch status, `GET` AV status per device. Maps to endpoint posture columns.
- **Users** — `GET` list users/technicians. Maps to `EMPLOYEE` reconciliation for `performed_by`.

## 4. Webhooks — GoTo Notification Channel (medium confidence)

GoTo exposes a **Notification Channel API** (shared across products) for real-time event push rather than per-product webhook config. Expected pattern: register a channel/subscription for the resource types you care about (session lifecycle, alert raised/cleared), GoTo POSTs events to your callback, validated with a shared secret (HMAC-style).

**NCE mapping:** `webhooks.py:validate_and_dispatch` (reuse the D365 HMAC validator), `api_rmm_webhook` receiver → fan session events into `RMM_SESSION` upserts and alert events into the **proactive-ticket path** (`RMM_ALERT -[opened]-> TICKET`). Confirm the exact channel-registration flow + event-type names + signature scheme against the Notification Channel docs before B2.

## 5. Zero-trust / signature keys (product-level, mirror in `19c`)

GoTo Resolve's remote-execution model uses a **zero-trust signature-key** step: executing commands/scripts requires a signature the vendor cannot forge, so even a compromised GoTo account can't run code without the key. The engine **mirrors this discipline**: `do_run_remediation` is allowlist-bound and WORM-audited on our side regardless of the vendor's own gate — defense in depth. Covered in `19c-security-legal-ops.md`.

## 6. GoTo object → engine function → graph (the adapter contract)

| GoTo object | Engine function | Graph write | memories / ledger |
|---|---|---|---|
| Managed device | `do_list_endpoints` | `rmm_endpoints`; `ASSET -[managed_by]-> RMM_AGENT` (C1 reconcile) | — |
| Alert | `do_list_alerts` / webhook | `RMM_ALERT -[on]-> ASSET`; `-[opened]-> TICKET` | — |
| Session (history) | `do_query_session` / sync | `RMM_SESSION -[on]-> ASSET`, `-[performed_by]-> EMPLOYEE`, `-[resolves]-> TICKET` | notes/transcript → `memories` |
| Session (create) | `do_launch_session` | `RMM_SESSION` (new) | — |
| Script/automation run | `do_run_remediation` | `RMM_SCRIPT_RUN -[on]-> ASSET`, `-[remediates]-> RMM_ALERT` | stdout/stderr → `memories`; outcome → `v3_cognitive_ledger` |
| Session outcome (manual) | `do_record_session_outcome` | updates `RMM_SESSION.outcome` | **structured fix fact** → `v3_cognitive_ledger` |
| User / technician | `do_list_endpoints` side-load | `EMPLOYEE` reconcile (HR(13) boundary) | — |

## 7. Confidence & what to confirm (do this before B1)

**High confidence:** OAuth 2.0 model (client creation, Bearer tokens, both grant types, refresh) — reuse `DataverseTokenManager` shape. The product capability set (sessions/unattended/RMM/alerts/remote-exec/patch/AV). The value mapping into NCE's graph + Support/Field-Tech loops.

**To confirm against the live OpenAPI + Postman at `developer.goto.com/LogMeInResolve` (all of §3–§4):**
1. Exact base URL + API version + resource paths for sessions, devices, alerts, remote-execution.
2. Exact **OAuth scope strings** to request when creating the client (least-privilege: separate read scopes for sync from execute scopes for remediation).
3. Pagination style (cursor vs offset) + rate limits (for `NCE_RMM_PAGE_SIZE` + backoff tuning via `http_resilience`).
4. **Notification Channel** registration flow, event-type names, and signature/validation scheme (drives `webhooks.py` + `NCE_RMM_WEBHOOK_SECRET_FILE`).
5. Remote-execution request contract + the **zero-trust signature-key** requirement (does the API require a pre-registered script, a signed payload, or both? — this shapes `rmm-remediation-scripts.json` and `19c`).
6. Whether device inventory exposes stable identifiers (serial / MAC / hostname) sufficient for C1 `ASSET` reconciliation, or whether a manual mapping seed is needed at first.

**Build-time posture:** implement `client.py` **mock-with-swap** (fixtures modeling §6 objects) so B1–B3 read/recall features are testable and demoable now, and flip to live on credential + endpoint confirmation — the same posture Assets(9) uses for its manufacturer adapters.

## Sources
- [LogMeIn Resolve — GoTo Developer Center](https://developer.goto.com/LogMeInResolve)
- [How to create an OAuth client](https://developer.goto.com/guides/Get%20Started/02_HOW_createClient/)
- [How to obtain an OAuth access token](https://developer.goto.com/guides/Authentication/03_HOW_accessToken/)
- [Authentication API](https://developer.goto.com/Authentication)
- [LogMeIn Resolve API — support portal](https://support.logmein.com/resolve/help/goto-resolve-api)
- [RMM Solutions for IT Professionals | LogMeIn Resolve](https://www.goto.com/it-management/solutions/rmm)
- [Unified Endpoint Management (UEM) | LogMeIn Resolve](https://www.logmein.com/products/resolve)
- [Working with unattended support sessions](https://support.logmein.com/resolve/help/working-with-unattended-support-sessions)
