> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# NCE-FE — Front-End Readiness & Extension Seams
> **Status:** FE-1/FE-2/FE-6 SHIPPED · FE-3/FE-4/FE-5 PLANNED  
> **Target:** Host application / consuming front-end integration  
> **Baseline:** `main` @ `9415eb0` · Verified-against: `9415eb0`

---

## Executive Summary

NCE provides a clean extension interface allowing any external host application, BFF (Backend for Frontend), or web UI to consume NCE services, mount custom routes, register domain-specific tools, and query cognitive state **without modifying NCE core source code**.

> [!WARNING]
> **Critical Auth Boundary (BFF Requirement)**
> The NCE `/api/*` surface expects HMAC three-header (+ optional mTLS) **machine** identity. Tenancy comes *solely* from `namespace_id` in the argument bag. **NCE cannot distinguish two users inside one namespace.** Therefore, a BFF (Backend for Frontend) or host application is necessarily the *only* user-auth and permission boundary. Allowing an authenticated end-user direct network access to NCE means that user can read or write any namespace the host's machine identity can access.

When a host vendors or consumes NCE as a backend engine, NCE is treated as an immutable core. All host-specific extensions are attached via explicit extension hooks rather than in-tree edits.

```
┌────────────────────────────────────────────────────────┐
│ Host Application (e.g. Web Portal / BFF)               │
│                                                        │
│  ┌──────────────────────┐    ┌──────────────────────┐  │
│  │ Host Routes          │    │ Host Tools           │  │
│  │ (extra_routes)       │    │ (register_tool)      │  │
│  └──────────┬───────────┘    └──────────┬───────────┘  │
│             │                           │              │
│    (NCE-FE-1 Hook)             (NCE-FE-2 Hook)         │
│             ▼                           ▼              │
│  ┌──────────────────────────────────────────────────┐  │
│  │ NCE Admin App & Registry                         │  │
│  │ (Starlette / FastAPI / MCP Dispatch)             │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────┘
```

---

## Extension Seam Registry

### NCE-FE-1 — App composition hook · **SHIPPED**

* **Delivered in:** `nce/admin_app.py` (`build_app`).
* **Mechanism:**
  ```python
  def build_app(
      extra_routes: Sequence[Route] = (),
      extra_middleware: Sequence[Middleware] = (),
  ) -> Starlette:
  ```
  A host passes its own Starlette routes and middleware without editing NCE modules:
  * `extra_routes` are placed **before** NCE's own routes so a host may add or override route matching.
  * `extra_middleware` is placed **outermost** (before NCE's auth, rate-limit, and tracing stack) so cross-cutting host middleware (such as CORS) can handle preflight requests before NCE authentication runs.
  * Calling `build_app()` with default arguments produces the standard standalone NCE application.
* **Acceptance Criteria (Met):**
  - [x] Host mounts additional routes purely from its own setup code.
  - [x] `admin_app.py` contains zero host-specific imports.
  - [x] Standalone default behaviour (no extras) is completely unchanged.

---

### NCE-FE-2 — Tool / admin-handler registration hook · **SHIPPED**

* **Delivered in:** `nce/tool_registry.py` (`register_tool`) and `_refresh_derived_sets`.
* **Mechanism:**
  ```python
  def register_tool(name: str, spec: ToolSpec, *, replace: bool = False) -> None:
  ```
  A host registers custom MCP tools at startup using the public `ToolSpec` dataclass without editing `tool_registry.py` or the dispatch loop:
  * Registered tools are subject to the identical dispatch-time gating as built-in tools (`nce:tools:disabled` Redis toggle, `spec.admin_only` scope check, `spec.mutation` cache-generation bump, `spec.cacheable` caching, and `NCE_DISABLE_MIGRATION_MCP` guard).
  * Derived frozensets (`MUTATION_TOOLS`, `CACHEABLE_TOOLS`, `ADMIN_ONLY_TOOLS`, `MIGRATION_TOOLS`) are automatically refreshed via `_refresh_derived_sets()`.
* **Acceptance Criteria (Met):**
  - [x] Host tools appear in the tool registry via the public API only.
  - [x] `tool_registry.py` and admin handlers carry zero host imports.
  - [x] Scope enforcement, caching, and disabled toggles apply uniformly to host-registered tools.

---

### NCE-FE-3 — Configurable static front-end serving · **PLANNED**

* **Current Reality:** `nce/admin_handlers/health.py` serves a hardcoded index path (`index_path = os.path.join(os.path.dirname(__file__), "admin", "index.html")`) and stylesheet (`styles_path = os.path.join(os.path.dirname(__file__), "admin", "styles.css")`).
* **Status:** `NCE_FRONTEND_DIR` / `NCE_FRONTEND_INDEX` config keys do not exist in `nce/config.py` on `main`. Hardcoded fallback paths remain.
* **Target Architecture:** Add configurable static-asset root settings (`NCE_FRONTEND_DIR` / `NCE_FRONTEND_INDEX`) in `nce/config.py` so NCE can optionally serve a host-provided SPA/asset bundle from an external directory.
* **Acceptance Criteria:**
  - [ ] A host can serve its own front-end bundle by configuration alone.
  - [ ] No host HTML/CSS files are required inside the `nce/` package directory.
  - [ ] Standalone default admin UI serves when unconfigured.

---

### NCE-FE-4 — Sanctioned browser auth + CORS surface · **PLANNED**

* **Current Reality:** `nce/jwt_auth.py` and `nce/a2a_server.py` log warnings and reject general browser client tokens when authenticating as agent principals:
  * `jwt_auth.py`: `logger.warning("Rejecting token with aud=%s; tokens issued for other services (web frontend, admin UI, etc.) must not authenticate as agent", aud)`
  * `a2a_server.py`: validates tokens with `expected_audience=cfg.NCE_A2A_JWT_AUDIENCE`.
* **Status:** The audience verification infrastructure is already configurable on the A2A side (`NCE_A2A_JWT_AUDIENCE`). What is missing is a dedicated front-end JWT audience setting (`NCE_FRONTEND_JWT_AUDIENCE`) and native CORS configuration (`NCE_ADMIN_CORS`, `NCE_CORS_ORIGINS`) in the admin server.
* **Target Architecture:** Document and provide a native, configurable front-end auth surface — formalizing CORS allow-origins settings and defining a structured JWT audience/issuer policy for browser-issued session tokens without relaxing agent/MCP authentication.
* **Acceptance Criteria:**
  - [ ] A browser front end can authenticate against a configured audience with native CORS support.
  - [ ] Default configuration preserves strict agent/mTLS/HMAC isolation.

> [!WARNING]
> **Interim CORS Requirement:** Until NCE-FE-4 is delivered, CORS **must** be mounted by the host through the FE-1 `build_app(extra_middleware=…)` hook. The configuration keys `NCE_FRONTEND_JWT_AUDIENCE`, `NCE_ADMIN_CORS`, and `NCE_CORS_ORIGINS` **do not exist in `nce/config.py`** (verified, not inferred).

---

### NCE-FE-5 — Host/extension configuration namespace · **PLANNED**

* **Current Reality:** `nce/config.py` (`_Config`) on `main` contains only NCE-generic parameters. Host-specific keys are cleanly excluded.
* **Target Architecture:** Formalize the documented extension-config pattern so host modules manage their own environment settings independently without modifying `nce/config.py`.
* **Acceptance Criteria:**
  - [x] Zero host-specific configuration keys exist in `nce/config.py`.
  - [ ] Formal helper patterns documented for host extensions reading isolated config.

---

### NCE-FE-6 — Stable API / data contract · **SHIPPED**

* **Delivered by:** Migration `022_muscles_schema_contract` (`nce/migrations/022_muscles_schema_contract.sql`).
* **What Shipped:**
  * **Schema Additions (migration 022):** Provenance columns (`change_origin`, `origin_event_id`) on `memories`, `kg_nodes`, `kg_edges`; `derivation_depth` on `memories`; DLQ triage columns; `processed_outbox_events` table; `actor_trust` table; `event_parents` WORM table; `action_approval_queue` table.
  * **Enums (`nce/models.py`):** `ChangeOrigin(StrEnum)` — `sync`, `webhook`, `agent`, `operator`, `consolidation`, `replay`, `unknown`; plus `ApprovalStatus` and `ActorKind`.
  * **Read-only Output DTOs (`nce/models.py`):** `ActorTrustOut`, `ApprovalQueueItemOut`, `ActionIdempotencyOut`, `EventParentOut` — all `extra="ignore"`.
  * **GET Endpoints (`nce/admin_app.py`):**
    * `GET /api/admin/actor-trust`
    * `GET /api/admin/approval-queue`
    * `GET /api/admin/approval-queue/{id}`
* **Acceptance Criteria (Met):**
  - [x] Versioned front-end API contract established with additive-only schema evolution.
  - [x] Core governance and approval queues queryable over REST.

---

## Extension Pattern Summary

| Seam | Purpose | Status | Implementation Site |
|---|---|---|---|
| **NCE-FE-1** | Mount custom Starlette routes & middleware | **SHIPPED** | `nce/admin_app.py` (`build_app`) |
| **NCE-FE-2** | Register custom MCP tools & handlers | **SHIPPED** | `nce/tool_registry.py` (`register_tool`) |
| **NCE-FE-3** | Configurable static asset / SPA serving | **PLANNED** | `nce/admin_handlers/health.py` (`admin_index`) |
| **NCE-FE-4** | Browser JWT audience & CORS configuration | **PLANNED** | `nce/jwt_auth.py`, `nce/a2a_server.py` |
| **NCE-FE-5** | Host extension configuration isolation | **PLANNED** | `nce/config.py` (`_Config`) |
| **NCE-FE-6** | Stable muscles & governance data contract | **SHIPPED** | Migration `022`, `models.py`, `admin_app.py` |

---

## Vertical Modules as First-Class NCE Capabilities

NCE ships **12 vertical engines** under `nce/vertical_modules/*` that expose capabilities through the unified NCE API surface (135 MCP tools in `TOOL_REGISTRY` + 134 admin routes):
* **NetBox & Dynamics 365** (core network & CRM integration)
* **Sales & Agreements** (deal rooms, contract OCR, signed-baseline freeze)
* **Procurement & Product** (BOM line matching, ETIM lookup, supplier ranking)
* **Economy & Inventory** (balanced postings, 130-pt match, warehouse stock)
* **Vendors, Diagnostics, System Design, HR, Field Tech**

**Two Extension Paths:**
1. **NCE-Owned Vertical Engines:** Built directly into `nce/vertical_modules/` and registered in `TOOL_REGISTRY`. Automatically available to all consuming front ends.
2. **Host-Specific Tools:** Implemented in host application code and registered at startup via **NCE-FE-2** `register_tool()`.
