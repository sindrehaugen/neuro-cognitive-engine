> **Status:** shipped · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

# Customer Portal Engine Admin Guide (Module 17)

> **Status:** shipped · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

This guide documents the administrative, architectural, and security boundaries of the **Customer Portal Engine** (`nce/vertical_modules/customer_portal/`) — NCE's only **internet-facing, customer-principal-authenticated** surface. It covers tenant enablement (there is none), the engine's own app shell and why it is not actually running anywhere outside tests, the RLS/tenant-isolation spine and its real reach, the redaction allow-list, the REST routes and auth model, and a live-drift finding this audit re-verified rather than copied from the board. Every claim is grounded in a specific file/line on `main @ b75c873`; where the design spec (`docs/vertical_engines/17-customer-portal-engine.md`) promises more than ships, this guide says so.

---

## 1. Enablement — there is no opt-in gate, and no config at all

> [!WARNING]
> **Zero `NCE_CUSTOMER_PORTAL_*` configuration keys exist.** A repository-wide search of `nce/config.py` for `portal` (case-insensitive) returns nothing. The spec (`17-customer-portal-engine.md:105`) names seven: `NCE_CUSTOMER_PORTAL_ENABLED`, `_PUBLIC_BASE_URL`, `_AUTH_PROVIDER`, `_MAGIC_LINK_TTL_MINUTES`, `_RATE_LIMIT_PER_MIN`, `_DOCUMENT_GRANT_TTL_DAYS`, `_ADVISOR_ENABLED` — **none exist**. There is also no `metadata.customer_portal.enabled` check anywhere in `nce/` (grepped for `customer_portal.enabled` and `metadata["customer_portal"` — zero hits). Every namespace that can reach the 9 MCP tools (`nce/tool_registry.py:1295-1332`, none `admin_only`) or the standalone app's REST routes (§2) can use the engine; there is no per-tenant kill switch in this codebase today. This is the same posture documented for Sales in `docs/engines/sales-admin.md` §1 — an enablement gate that exists in the spec but not in the code.
>
> `customer_portal` is nonetheless a recognised name in the shared engine registry (`VERTICAL_MODULE_NAMES`, `nce/engine_registry.py:31-51`), so `engine.modules["customer_portal"]` resolves for other engines that might look it up — but that registry entry gates *cross-engine lookup*, not *this engine's own* tenant availability.

---

## 2. The "Own App Shell" (Charter Layer 3) — built, tested, never started

`nce/vertical_modules/customer_portal/app.py` implements `build_customer_portal_app(engine=None) -> Starlette` (`app.py:360-387`), matching the spec's Layer-3 requirement for "a dedicated, rate-limited application surface... no internal admin endpoints or internal tool surfaces mounted." `docs/vertical_engines/ENGINE_STATUS.md:28,93` confirms the count this guide re-derives: **12 REST routes**, tallied separately from the 209 routes on the main admin app ("+12 on the Customer Portal's own app shell").

### 2.1 Routes (`app.py:362-375`)
| Method | Path | Handler | Notes |
|---|---|---|---|
| GET | `/health` | `portal_health` | Public, no auth — returns `{"status":"ok","surface":"customer_portal"}` |
| POST | `/api/portal/login` | `portal_login` | See §3 |
| GET | `/api/portal/rooms/overview` | `api_portal_room_overview` | |
| GET | `/api/portal/rooms/{room_id}/tracker` | `api_portal_room_tracker` | |
| GET | `/api/portal/rooms/{room_id}/assets` | `api_portal_asset_register` | |
| GET | `/api/portal/documents` | `api_portal_documents` | |
| GET | `/api/portal/documents/{share_id}` | `api_portal_document` | Only path to `do_get_document` — no MCP tool (user guide §2.1) |
| GET | `/api/portal/sla` | `api_portal_sla_status` | |
| GET | `/api/portal/invoices` | `api_portal_invoices` | |
| POST | `/api/portal/service-requests` | `api_portal_service_requests` | mutation |
| POST | `/api/portal/expansion-interest` | `api_portal_expansion_interest` | mutation |
| POST | `/api/portal/advisor` | `api_portal_advisor` | |

### 2.2 🔴 This app is never mounted or started anywhere in the running system
A repo-wide search for `build_customer_portal_app` outside `nce/vertical_modules/customer_portal/app.py` finds it **only in test files** (`tests/unit/test_customer_portal_actions.py:25,156`; `test_customer_portal_advisor.py:21,168`; `test_customer_portal_post_handover.py:22,278`; `test_customer_portal_spine.py:151,205,225`; `test_customer_portal_tracker.py:378,380`), all instantiating it directly via Starlette's `TestClient`. There is:
- No reference in `nce/admin_app.py` (confirmed: `grep customer_portal nce/admin_app.py` returns nothing).
- No `Mount(...)` of it in any of the other four files that build a `Starlette(...)` app (`nce/a2a_server.py`, `nce/me_app.py`, `nce/observability.py`, plus `admin_app.py` itself).
- No entry in any `docker-compose*.yml`, `*.sh` launcher, or `pyproject.toml` entry-point referencing `customer_portal`.

**Practical implication:** the security spine (RLS-backed tables, allow-list redaction, rate-limit middleware, the separate app object) is real, buildable, and covered by tests — but as of this snapshot there is no running process that serves `/api/portal/*` to an actual customer. Standing this engine up in production requires someone to wire `build_customer_portal_app(engine)` into an ASGI entry point (its own container, or a `Mount()` on an existing one) — that wiring does not exist yet.

### 2.3 The rate limiter is a stub
`CustomerRateLimitMiddleware` (`app.py:28-37`) is constructed with `max_requests_per_minute: int = 120` and is the sole middleware on the app (`app.py:377-379`), but its `dispatch` method is:
```python
async def dispatch(self, request: Request, call_next: Any) -> Any:
    # Standard rate-limiting inspection hook
    return await call_next(request)
```
It stores `max_requests_per_minute` and never reads it again — **no counter, no Redis call, no 429 response path exists**. The module's own docstring (`app.py:6-9`) claims "Dedicated rate limiting per customer IP / principal"; the code does not implement it. If this app is wired up per §2.2, do not rely on this middleware for abuse protection until it is actually built out — front it with a real rate limiter (reverse proxy, gateway, or a completed implementation here) first.

---

## 3. Customer Principal Authentication — mock, not BankID

`portal_login` (`app.py:45-72`) is the only login path this engine ships. It requires `email` and `token` in the POST body, accepts an optional `auth_provider` (default `"magic_link"`), and — regardless of provider or token value — computes:
```python
customer_scope_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"customer.{email}"))
session_token = f"cp_sess_{uuid.uuid4().hex}"
```
The code comment is explicit about this: *"In production, broker/magic-link token is verified cryptographically. For synthetic/staff authentication, generate a deterministic customer scope."* **No cryptographic verification of `token` happens anywhere in this function** — any string satisfies the `not email or not token` check. This is fine for the tests that exercise it (`test_customer_portal_spine.py:171-182`, `test_customer_portal_actions.py`) but is **not** the BankID/Criipto-broker or verified magic-link flow the spec (`17-customer-portal-engine.md:27,105`) requires before real customer data can flow — see §7's DPIA note.

`do_authenticate`, the `do_*` core the spec names for this responsibility (`17-customer-portal-engine.md:60`), **does not exist** anywhere in `nce/vertical_modules/customer_portal/` — confirmed absent from `__init__.py`'s 10-item `__all__` and from every `.py` file in the directory. `portal_login` is the closest analog, and it is REST-only (no MCP tool, no `do_` naming, no package export).

---

## 4. RLS / Tenant Isolation — a real, tested spine with an unused write/read path

### 4.1 Migration 073 — three tables, all `FORCE ROW LEVEL SECURITY`
`nce/migrations/073_customer_portal_engine.sql` creates `portal_users`, `portal_document_shares`, `portal_service_requests`, each:
- `namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE` + `customer_scope_id UUID NOT NULL`.
- `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`.
- An `external_isolation_policy` requiring **both** `namespace_id = get_nce_namespace()` **and** `customer_scope_id = get_nce_external_scope()` (migration lines 43-57, 92-106, 140-154 for the three tables respectively).
- `nce_app` grants: `SELECT, INSERT, UPDATE, DELETE` on all three (no WORM restriction on this engine's own tables, unlike Sales' `sales_signed_baselines`).

`get_nce_external_scope()` (`nce/schema.sql:723-740`) returns the nil-UUID sentinel when `nce.external_scope_id` is unset or malformed, guaranteeing zero rows for an unscoped session — the **deny-when-unset** invariant. All three tables are registered in `EXPECTED_TENANT_RLS_TABLES` (`nce/event_log.py:387-389`) and covered by `tests/unit/test_customer_portal_spine.py:28-71`, which asserts both the migration and `schema.sql` define them identically.

### 4.2 🔴 None of the three tables are ever read or written by application code
A repo-wide grep for `portal_users`, `portal_document_shares`, or `portal_service_requests` outside `nce/schema.sql`/`nce/migrations/073_customer_portal_engine.sql`/tests turns up exactly two files: `nce/event_log.py` (the RLS-table registration, §4.1) and `nce/vertical_modules/customer_portal/app.py` (which only defines the route names `api_portal_service_requests`/`api_portal_documents`, never a SQL statement). Concretely:
- `portal_login` never inserts a `portal_users` row — it computes a scope UUID and returns it; no session is persisted.
- `do_raise_service_request` never inserts into `portal_service_requests` — it writes to the in-memory `_IDEMPOTENCY_CACHE` dict (`actions.py:27`) and hands off to Support; the dedicated table sits empty.
- `do_list_documents`/`do_get_document` never query `portal_document_shares` — they redact whatever `documents`/`document` the caller passed in `params` (`documents.py:61,86`).

**Net effect:** the RLS spine is real at the schema level, unit-tested, and structurally correct — but it is not yet wired to any live data path. `scoped_customer_pg_session` (`auth.py:65-85`), the one helper that would actually open a DB connection with `nce.external_scope_id` set, is likewise **never called anywhere** outside its own definition (confirmed by grep; the sole other reference is a comment in `test_customer_portal_spine.py:194` describing a data flow through `rooms.py` that does not exist in `rooms.py`'s current source). Treat the SQL-level RLS as a prepared foundation, not as an already-enforced live control, until a caller actually opens a scoped connection against these tables.

### 4.3 The Python-level IDOR check is what actually runs today
Every `do_*` core instead calls `evaluate_customer_scope_access(cust_scope, target_scope)` (`auth.py:28-62`) — a pure in-Python UUID comparison, no database round-trip. Because every call site in this codebase (all 9 MCP tools, all 12 REST routes) leaves `target_scope_id` unset, it **always defaults to `customer_scope_id` itself** (e.g. `actions.py:33`, `rooms.py:95`) — so the check reduces to "is `customer_scope_id` a valid, non-nil UUID?" It cannot, by itself, detect a genuine cross-customer request, because there is no second, independently-sourced scope to compare against in the current wiring. Real tenant isolation for the six read-projection tools therefore depends entirely on whoever assembles the `rooms`/`assets`/`documents`/`invoices` parameters filtering them to the right customer *before* calling these functions (user guide §5) — this engine's own code does not fetch or filter that data itself (§4.2, and the user guide's architectural note).

### 4.4 A previously-shipped IDOR was found and fixed — the current shape is hardened
`tests/unit/test_customer_portal_spine.py:185-274` documents (and guards against regressing) a real, previously-shipped defect: every REST route originally built its `params` dict as `{"namespace_id": ns, "customer_scope_id": cust_scope, **body}` — client-controlled `**body`/`**query_params` **last**, so a client-supplied `customer_scope_id` in the query string or JSON body silently overwrote the authoritative value read from the `X-Customer-Scope-ID` header, producing a cross-customer IDOR on all routes. The current `app.py` (verified: every one of the 12 handlers, e.g. lines 86-94, 114-121, 283-290) spreads the client-supplied mapping **first** and sets `namespace_id`/`customer_scope_id` **last**, with an explicit comment at every call site (*"Client-supplied values FIRST so the authoritative namespace/scope assigned below cannot be overridden"*). `test_get_route_query_param_cannot_override_header_scope` and `test_post_route_body_cannot_override_header_scope` (`:233-265`) plus a structural AST-shape guard (`:267-`) assert this ordering holds and will catch a regression on any newly added route.

---

## 5. Redaction (Charter Layer 2) — allow-list, fails closed

`project_customer_safe` (`redaction.py:32-72`) loads `customer-redaction.json` once (`@lru_cache`, `redaction.py:22-29`) and, for any `projection_name` not present in the config, **returns `{}`** rather than passing data through — an unrecognised projection fails closed, not open (`redaction.py:54-56`). Seven projections are defined: `room_tracker`, `room_overview`, `asset_register`, `document_share`, `sla_status`, `invoices`, `service_request` — each with an explicit `allowed_fields` list and a documentary `forbidden_fields` list (the latter is redundant with the allow-list by construction, since only `allowed_fields` gates inclusion — `forbidden_fields` is never separately checked at runtime beyond the `key in allowed and key not in forbidden` test on `redaction.py:63`, which is already implied by a disjoint allow-list; it exists for auditability, not as an independent second gate).

---

## 6. 🔴 The `-32603` Error-Surface Finding — RE-VERIFIED, and it is now FIXED

`C:\Claude\ORCH_BOARD.md` (ML-orch section, 2026-09-06) records: *"`resources_resolve_capacity` and `customer_portal_room_overview` surface refusals as `-32603 Internal error`... `ResourcesError` and `PermissionError` match no clause in `mcp_errors.py`'s `mcp_handler`. An IDOR refusal reported as an internal error is invisible in every log."* This is a serious finding if true — a permission refusal indistinguishable from a server crash defeats every monitoring/alerting rule built on error codes.

**This audit re-checked the current code rather than copying the board entry, per the standing rule to validate the premise, not just the instrument.** As of `main @ b75c873`, `nce/mcp_errors.py`'s `mcp_handler` decorator **does** have both clauses:
- `except PermissionError as e:` → `McpError(MCP_INVALID_REQUEST, "Permission denied", ...)` — code **`-32600`**, not `-32603` (`mcp_errors.py:288-294`).
- `except _RESOURCES_ERRORS as e:` → `McpError(MCP_INVALID_PARAMS, "Invalid parameters", ...)` — code **`-32602`**, not `-32603` (`mcp_errors.py:333-338`, with the `ResourcesError` import guard at `mcp_errors.py:59-66`).

`git blame` on both clauses attributes them to commit `16f86b0` ("feat(customer_portal): close loop from service request to support ticket via W-1 engine registry (Wave CP-1)"), which is the tip commit of `main @ b75c873` in this worktree (merged via PR #26, `mlv15b/cp1-customer-portal-ticket`). The diff (`git show 16f86b0 -- nce/mcp_errors.py`) shows both the `PermissionError` clause and the `ResourcesError`/`_RESOURCES_ERRORS` machinery were **added** in that same commit, alongside 24 lines of edits to `nce/mcp_errors.py`.

**Verdict: the finding was true when the board entry was written, and it is fixed as of this worktree's HEAD.** `customer_portal_room_overview`'s `PermissionError` (raised on an IDOR refusal in `do_room_overview` → `do_room_tracker`, `rooms.py:97-99,138-140`) now surfaces as `-32600 Permission denied`, not `-32603 Internal error`. Do not re-open this as a live defect without re-checking `nce/mcp_errors.py` against whatever commit you are auditing — the fix landed in the same wave (CP-1) that this guide's other findings (§4.4's IDOR hardening) also came from, so it is plausible both were addressed together. If you are auditing an older commit or a different worktree, re-run this check before repeating the board's claim.

---

## 7. Compliance Gate — DPIA (not this engine's code to enforce)

The spec (`17-customer-portal-engine.md:37,120,125,133`) states production customer personal data may not flow until a DPIA (Data Protection Impact Assessment) signs off, and calls this binary — go/no-go, not "likely clearing." **Nothing in the code enforces this gate** — there is no `NCE_CUSTOMER_PORTAL_DPIA_CLEARED` flag or equivalent check anywhere in `nce/`. Given §2.2 (the app is not mounted anywhere) and §3 (auth is a mock), this is currently moot in practice — there is no running surface for real customer data to flow through yet — but if §2.2 is ever remedied, this gate needs to be built, not assumed.

---

## 8. Autonomy / Governance

| Operation | Autonomy Tier | Governance & Confirmation Gate |
|---|:---:|---|
| 6 read-projection tools (§1 user guide) | Advisor / Watcher (read-only) | None needed — no write occurs |
| `customer_portal_advisor_answer` | Advisor (sandboxed) | Regex-based forbidden-keyword and injection-pattern guards (`advisor.py:22-32`) run before any templated response; no LLM call, no propose/confirm step because nothing is generated freely |
| `customer_portal_raise_service_request` | Actor (mutation) | Contract-B gate defaults **permissive** unless both flags are explicitly `False` (user guide §4.1); in-memory idempotency only (§4.2); no human-in-the-loop confirm step before the Support hand-off fires |
| `customer_portal_register_expansion_interest` | Actor (mutation) | No confirm gate; hands off to Sales as a human-gated **lead surfaced to Sales' own Advisor**, never an autonomous customer-facing upsell |

There is no C2 `@governed` decorator anywhere in `nce/vertical_modules/customer_portal/` (grepped for `governed`/`@governed` — zero hits), the same posture documented for Sales (`sales-admin.md` §8). Both mutation tools are immediately effective on call.

---

## Appendix: Drift, Blockers & Known Gaps (this audit)

1. **No tenant enablement gate or config keys** — zero `NCE_CUSTOMER_PORTAL_*` vars exist; every namespace has the surface available (§1).
2. **The dedicated app shell (`build_customer_portal_app`) is never mounted or started** outside `TestClient` in unit tests — 12 routes exist in code with no live ASGI entry point serving them (§2.2).
3. **The rate-limit middleware is a documented no-op** — accepts a `max_requests_per_minute` parameter it never reads (§2.3).
4. **Customer authentication is a deterministic mock** (`uuid5` over the email, no token verification) — not the BankID/Criipto-broker flow the spec requires (§3).
5. **`do_authenticate` was specced but never built** as a `do_*` core (§3).
6. **The three owned RLS tables (`portal_users`, `portal_document_shares`, `portal_service_requests`) are schema-complete and unit-tested, but never read or written by application code** — the entire data path is caller-supplied params, not a persisted store (§4.2). `scoped_customer_pg_session` (the helper that would open a DB connection under the customer scope) is dead code.
7. **The in-Python IDOR check reduces to "is `customer_scope_id` non-nil,"** not "may this customer see this specific record" — real isolation depends on upstream callers filtering data before it reaches this engine (§4.3).
8. **`-32603` misclassification of `PermissionError`/`ResourcesError` — CONFIRMED FIXED**, not a live defect, as of commit `16f86b0` / `main @ b75c873` (§6). Re-verify on any other commit before repeating the board's claim.
9. **A previously-shipped cross-customer IDOR (client-param-spread-last) is fixed and regression-tested** (§4.4) — recorded here as resolved history, not a current gap.
10. **No DPIA enforcement in code** — moot while §2's app is unmounted, but a gap to close before wiring this engine live (§7).
