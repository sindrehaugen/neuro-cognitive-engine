> **Status:** shipped · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

# Customer Portal Engine User Guide (Module 17)

> **Status:** shipped · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

The **Customer Portal Engine** (`nce/vertical_modules/customer_portal/`) is NCE's **external, customer-facing surface** — planning module 13, "Customer Portal," in the source module map. Its organising principle is **room-centric, not project-centric**: a customer sees *"your boardroom is 80% ready,"* never an internal project number, via a Domino's-style tracker (`Planned → Ordered → Delivered → Installed → Tested → Ready`). Per the design intent recorded in `docs/vertical_engines/17-customer-portal-engine.md`, the engine is meant to become **more valuable after handover** — a room-centric asset register, service-request intake, document access (FDV/as-built), SLA self-service, and expansion/re-buy capture all keep the customer returning long after delivery. This guide covers the **9 registered MCP tools**; the companion admin guide covers enablement, the app shell, RLS, and a live drift finding.

> [!IMPORTANT]
> **Surface summary (`main` @ `b75c873`):**
> - **9 exposed MCP tools**, all registered `nce/tool_registry.py:1295-1332`. **None** are `admin_only`; 6 are `cacheable`; 3 are `mutation`.
> - **12 REST routes** mounted on the engine's **own, separate Starlette app** (`nce/vertical_modules/customer_portal/app.py:360-387`) — not the internal admin app. See the admin guide §2 for why this app is never actually started outside tests.
> - **10 internal `do_*` cores** (`nce/vertical_modules/customer_portal/__init__.py:34-45`), one of which (`do_get_document`) has **no MCP tool** — REST-only.
> - **Caller identity:** an authenticated **external customer principal** (`customer_scope_id`), never an internal employee or partner/contractor session. There is no "admin caller" mode for this surface at all — see admin guide §1.
> - 🔴 **Architectural reality check:** the six read-projection cores (`do_room_tracker`, `do_room_overview`, `do_asset_register`, `do_list_documents`, `do_list_invoices`, `do_sla_status`) accept an `engine` argument but **never call it** — verified by grepping `nce/vertical_modules/customer_portal/{rooms,documents,invoices,sla,advisor}.py` for any `engine.` use (zero hits). They do not fetch data from Project/Assets/Support/Agreements/Economy themselves; **the caller supplies the raw room/asset/document/invoice/SLA data as call parameters**, and the function's job is to compute derived display fields and apply the allow-list redaction (§7). Plan your integration accordingly — see §6.

---

## 1. Room-Centric Read Projections

### 1.1 `customer_portal_room_tracker` — cacheable, read-only
Handler: `handle_customer_portal_room_tracker` (`nce/vertical_modules/customer_portal/mcp_handlers.py:42-50`) → `do_room_tracker` (`rooms.py:89-131`).

Projects the Domino's tracker state for a single room: `stage`, `percent_ready`, a customer-safe `status` label, and a plain-language `summary`. The caller supplies `room_id`, `room_name`, `site_id`/`site_name`, and the raw `bom_lines`/`assets` lists (each carrying a `status`/`lifecycle` field); `compute_room_stage_and_progress` (`rooms.py:30-86`) maps each line through `room-tracker-stages.json`'s `bom_status_to_stage`/`asset_lifecycle_to_stage` tables, averages the resulting stage weights, and reports the *lowest* matched stage the average percentage clears. **When to call it:** a customer or a portal front-end asking "how is my boardroom coming along?"

**Messy-reality note:** if the caller also passes `messy_context: {"case": "delay"|"change_order"|"partial_delivery"|"frozen"}`, the function swaps in a customer-safe status/narrative override from `room-tracker-stages.json`'s `messy_reality_rules` (e.g. a change order surfaces as *"Scope updated"*, never as a regression). **This override is caller-driven, not auto-detected** — the engine has no logic of its own that infers "this room has been stuck at Ordered for 3 months"; whoever assembles `messy_context` must decide which case applies.

### 1.2 `customer_portal_room_overview` — cacheable, read-only
Handler: `mcp_handlers.py:53-61` → `do_room_overview` (`rooms.py:134-173`).

Rolls up every room the caller supplies (`rooms: [...]`) into a site-level `overall_percent_ready` by calling `do_room_tracker` once per room and averaging. **When to call it:** the portal's dashboard/landing view — *"3 rooms, 72% ready overall."*

### 1.3 `customer_portal_asset_register` — cacheable, read-only
Handler: `mcp_handlers.py:64-72` → `do_asset_register` (`rooms.py:176-194`).

Projects a room-centric list of installed equipment (`asset_id`, `manufacturer`, `model`, `serial_number`, `warranty_expires_at`, `coverage_tier`) with commercial fields (`purchase_price`, `supplier_name`, `vendor_id`, `margin`) stripped by the `asset_register` allow-list (§7). **When to call it:** the post-handover "what's installed in this room, and is it still under warranty" view — the lock-in surface the spec is built around.

---

## 2. Post-Handover Surfaces

### 2.1 `customer_portal_list_documents` — cacheable, read-only
Handler: `mcp_handlers.py:75-83` → `do_list_documents` (`documents.py:52-74`).

Lists FDV/as-built/SoW document *shares* the caller supplies (`documents: [...]`), filtering out any share that is revoked (`revoked_at` set) or expired (`expires_at <= now`) via `_is_grant_valid` (`documents.py:21-49`), then redacting through the `document_share` allow-list.

> [!NOTE]
> **`do_get_document` has no MCP tool.** The single-document fetch core (`documents.py:77-94`) is exported from the package (`__init__.py:23`) and reachable over REST (`GET /api/portal/documents/{share_id}`, app.py:187-213), but **no `customer_portal_*` tool wraps it** — `nce/tool_registry.py` registers only `customer_portal_list_documents`. An MCP-only caller cannot fetch a single document by `share_id`; it must fetch the full list and pick the entry it needs.

### 2.2 `customer_portal_sla_status` — cacheable, read-only (Watcher, customer-scoped)
Handler: `mcp_handlers.py:86-94` → `do_sla_status` (`sla.py:20-43`).

Projects the customer's SLA tier, response/resolution targets, and running clock (`current_clock_hours`, `is_breached`), stripping `contract_value`, `mrr`, `penalty_amount`, `internal_cost`. If the caller passes no SLA data at all, the function falls back to hardcoded defaults (`tier_name: "Standard SLA"`, `response_target_hours: 8`, `resolution_target_hours: 24`) rather than erroring — useful for demo/staging calls, but note that an empty response looks identical to a real "Standard SLA, no breach" tenant unless you check for these exact default values.

### 2.3 `customer_portal_list_invoices` — cacheable, read-only
Handler: `mcp_handlers.py:97-105` → `do_list_invoices` (`invoices.py:20-37`).

Lists the caller-supplied invoices, stripping `internal_margin`, `our_cost`, `rebate_percentage` per the `invoices` allow-list. Degrades gracefully to an empty list if the caller passes no `invoices` param.

---

## 3. Sandboxed Advisor

### 3.1 `customer_portal_advisor_answer` — NOT cacheable (Advisor, sandboxed, customer-scoped)
Handler: `mcp_handlers.py:108-116` → `do_advisor_answer` (`advisor.py:35-131`).

> [!NOTE]
> **What kind of "AI" this is.** This Advisor is **not a generative LLM call** — it is deterministic keyword-matched template selection over caller-supplied `room_data`. It never embeds, retrieves, or generates free text; every response is one of five hardcoded string templates chosen by regex/keyword match on the customer's `query`. This matches the broader Portal-advisor pattern noted in `docs/vertical_engines/ENGINE_STATUS.md` ("recall-and-template compositions... the word 'AI' in their specs means retrieval and composition, not generation").

Two regex guards run **before** any templating (`advisor.py:22-32`):
1. `_FORBIDDEN_INTERNAL_PATTERNS` — blocks queries probing for `churn_risk`, `health_score`, `margin`, `profit`, `our_cost`, `internal_notes`, `supplier_terms`, `internal_priority`, `escalation_level`, `p1_critical`.
2. `_INJECTION_PATTERNS` — blocks `system override`, `ignore (all/previous/prior) (rules|instructions)`, `you are in debug`, `switch scope`, `reveal internal`, `print all`.

Either match short-circuits to a fixed refusal template (`advisor.py:52-63`) — the query is echoed back but never processed further. Passing the guards, the function then checks for service-request keywords (`"ticket"`, `"broken"`, `"repair"`, …) → guidance to use `/api/portal/service-requests`; else if `room_data` (a dict) was supplied → a room-status narrative built from `percent_ready`/`stage`/`summary`; else if only `room_id` was supplied → a generic in-progress message; else a default greeting. **When to call it:** any free-text customer question routed through the portal's chat surface.

---

## 4. Inbound Customer Actions (hand-offs)

### 4.1 `customer_portal_raise_service_request` — mutation, not cacheable (Actor → Support hand-off)
Handler: `mcp_handlers.py:119-127` → `do_raise_service_request` (`actions.py:30-110`).

Creates a service-request intake and hands off to the Support engine's `do_open_ticket` via the W-1 engine registry (`engine.modules["support"]`, resolved per-namespace when `for_namespace` is available). Notable behaviour:
- **Contract-B gating defaults to permissive.** `contract_b_covered` and `spend_authorized` both `params.get(..., True)` (`actions.py:40-41`) — the refusal (`PermissionError`, "Contract-B entitlement or spend authorization required") only fires if a caller **explicitly** passes `contract_b_covered=False` **and** `spend_authorized=False`. Omitting both fields never triggers the gate.
- **Idempotency is in-memory only.** `_IDEMPOTENCY_CACHE` (`actions.py:27`) is a process-local `dict` keyed on `(customer_scope_id, request_id)` — it survives repeated calls within one running process but is lost on restart and is not shared across workers. If you rely on idempotent retries across a multi-worker deployment, pass a caller-generated `request_id` and be aware the guarantee is best-effort, not durable.
- **Graceful degradation:** if the Support engine is disabled or missing from the registry, the response still returns with `support_degraded: true` and a `degradation_reason` instead of raising — the customer-visible request is still recorded, but no ticket is created.

### 4.2 `customer_portal_register_expansion_interest` — mutation, not cacheable (Actor → Sales hand-off)
Handler: `mcp_handlers.py:130-138` → `do_register_expansion_interest` (`actions.py:113-169`).

Records a re-buy/expansion signal (e.g. *"I'd like to add a projector to Room 2"*) and confirms the Sales engine is reachable via the registry (`scoped_registry["sales"]`) — it does **not** create a Sales lead object itself; presence-confirmation only (`sales_lead_routed: true` on success). Degrades the same way as §4.1 when Sales is disabled/missing. Per the spec's intent, this is a **human-gated** signal to Sales' own Advisor — the customer is told *"a member of our sales and advisory team will review and contact you,"* never shown a churn/health score or upsell push.

---

## 5. IDOR / Scope Enforcement Every Caller Must Understand

Every one of the 9 tools begins by reading `customer_scope_id` (and an optional `target_scope_id`, defaulting to `customer_scope_id` itself) and calling `evaluate_customer_scope_access` (`auth.py:28-62`):
- **Deny-when-unset:** a missing, empty, or nil-UUID (`00000000-0000-0000-0000-000000000000`) `customer_scope_id` always returns `False` → the tool raises `PermissionError`.
- The MCP `inputSchema` for every tool marks `customer_scope_id` as **optional** (`nce/mcp_stdio_tools.py:5113-5270`) — but omitting it is not "unscoped read," it is a **guaranteed refusal**. Always pass it.
- Because `target_scope_id` is never set independently by any current REST route or MCP call site, the check in practice only verifies "`customer_scope_id` is a valid, non-nil UUID," not "this caller may see this specific room/document/invoice." Genuine cross-customer isolation depends on **whoever assembles the `rooms`/`assets`/`documents`/`invoices` parameters** filtering to the right customer before calling these tools — see the architectural note at the top of this guide and the admin guide §4 for the full implication.

---

## 6. Worked Example — Room Tracker via MCP

```python
params = {
    "namespace_id": "00000000-0000-4000-8000-000000000001",
    "customer_scope_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "room_id": "room-boardroom-2",
    "room_name": "Boardroom 2",
    "site_id": "site-oslo-hq",
    "bom_lines": [
        {"status": "installed"},
        {"status": "commissioning"},
    ],
    "assets": [
        {"lifecycle": "installed"},
    ],
}
# via MCP:
result = await handle_customer_portal_room_tracker(engine, params)
# result == '{"room_id": "room-boardroom-2", "room_name": "Boardroom 2",
#             "stage": "installed", "percent_ready": 75,
#             "status": "Installed", "summary": "Equipment mounted, cabled, and powered.", ...}'
```

Note that `bom_lines`/`assets` are supplied by the caller, not fetched by the tool (§0 architectural note) — a real deployment must assemble this data from Project's `BOM_LINE.status` and Assets' lifecycle state before calling.

---

## Appendix: Spec vs. Shipped (delta from `docs/vertical_engines/17-customer-portal-engine.md`)

| Feature / Capability | Spec Proposal | Shipped State (`main` @ `b75c873`) | Notes |
|---|---|---|---|
| `do_room_tracker` / `_overview` / `_asset_register` | Advisor / read-projection over live A2A reads (Project, Assets) | **Shipped as tools**, but data is caller-supplied, not fetched via A2A | See architectural note above |
| `do_get_document` | Read-projection, MCP + REST | **REST only** — no MCP tool registered | `mcp_handlers.py` has no `handle_customer_portal_get_document` |
| `do_authenticate` | Core: establishes customer principal via BankID/magic-link | **Not built as a `do_*` core.** `portal_login` (`app.py:45-72`) is REST-only, not exported from `__init__.py`, has no MCP tool, and issues a **deterministic `uuid5(NAMESPACE_DNS, "customer." + email)`** scope with no cryptographic verification of the token — a mock/staff auth stand-in, not a BankID/Criipto broker integration | See admin guide §3 |
| `customer_portal_sla_status` | Watcher (self-service SLA clock) | **Shipped**, but degrades to hardcoded defaults with no upstream SLA data | §2.2 |
| Expansion interest → Sales lead | Creates a lead object in Sales | **Confirms Sales engine reachability only** — no lead object created by this engine | §4.2 |
| A2A reads from Project/Assets/Support/Agreements/Economy | 6 owner engines feed the 6 read-projection tools | **No A2A client code exists in this module** — `engine` param unused in all 6 read cores | §0 |
