> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# NCE Vertical-Module Pattern & Authoring Guide

> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

**Companion:** `docs/FRONTEND_READINESS.md` (vertical modules are first-class in the NCE API)

A **vertical module** (`nce/vertical_modules/<name>/`) adds a domain capability to NCE
and **auto-exposes it through the NCE API** (MCP tools in `TOOL_REGISTRY`, optional admin
routes). NetBox and Dynamics 365 are the two shipped reference implementations on main.
This guide distills the canonical shape so the next verticals (planned/in-flight, not yet
on main) are consistent and fast to build, and so they ride the pristine mirror to every
front-end for free.

## Canonical skeleton

```
nce/vertical_modules/<name>/
  __init__.py        # package marker (may export the public surface)
  client.py          # external-system HTTP client(s)            [required if external]
  auth.py            # OAuth/token lifecycle (Redis-cached)       [if the system needs it]
  sync.py            # deterministic track: entities → kg_nodes/kg_edges   [if syncing]
  ingestion.py       # semantic track: raw data → memories + embeddings    [if ingesting]
  <domain>.py        # domain logic (e.g. circuits, contacts, mtbf, discovery)
  <domain>_bridge.py # cross-system mapping (e.g. d365 ↔ netbox)  [if bridging]
  mcp_handlers.py    # MCP tool entry points                      [REQUIRED]
```

Only `mcp_handlers.py` + a `TOOL_REGISTRY` entry are strictly required to expose a tool.
Everything else is included as the domain needs it. **Essential divergence to decide up
front:** is the source *pull-only* (like NetBox — query handlers, no webhooks/ingestion)
or *push + semantic* (like D365 — `webhooks.py` + `ingestion.py` + `sync.py`)?

## The wiring (how a vertical reaches the NCE API)

### 1. Handler convention (`mcp_handlers.py`)
```python
async def handle_<name>_<action>(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool handler — returns a JSON string."""
    namespace_id = arguments.get("namespace_id", "")
    if not namespace_id:
        return json.dumps({"error": "namespace_id is required"})
    try:
        from nce.config import cfg
        client = <Name>Client(cfg.NCE_<NAME>_URL, cfg.NCE_<NAME>_TOKEN)
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            ...
        return json.dumps(result, default=str)
    except Exception as exc:
        log.exception("handle_<name>_<action> failed namespace=%s", namespace_id)
        return json.dumps({"error": str(exc)})
```

### 2. Register in `nce/tool_registry.py`
```python
from nce.vertical_modules.<name> import mcp_handlers as <name>_mcp_handlers

# inside TOOL_REGISTRY:
"<name>_<action>": ToolSpec(
    _h(<name>_mcp_handlers, "handle_<name>_<action>"),
    cacheable=True,     # read-only → cache
    admin_only=False,   # True if operator-gated
    mutation=False,     # True if it writes state (bumps cache generation)
),
```
Use `_h(module, attr)` late-binding (preserves `mock.patch` in tests). The dispatch layer
reads `spec.admin_only/mutation/cacheable` directly and applies the `nce:tools:disabled`
toggle — so a registered vertical tool is fully gated automatically.

> **NCE-owned vertical ≠ host tool.** A vertical's tools are registered **here, by NCE**,
> and ship inside the pristine mirror. The `register_tool()` hook (NCE-FE-2) is only for
> *host-specific* tools, never for NCE's own verticals. See `FRONTEND_READINESS.md`.

### 3. Optional admin routes
Add routes in `nce/admin_app.py` `build_admin_routes()` and handlers in
`nce/admin_handlers/<name>.py` (`async def api_admin_<name>_<action>(request) -> JSONResponse`).
Wire the handler module in `admin_app.py`’s imports. Use this for config/status/sync-now
operator surfaces (D365 has 8 such routes; NetBox has none — it's MCP-tool-only).

### 4. Config keys (`nce/config.py`)
Namespace every key `NCE_<NAME>_*`: at minimum an enable flag, URL, token; plus tunables
(`*_SYNC_INTERVAL_MINUTES`, `*_PAGE_SIZE`, thresholds). Add a `validate_*` check if any key
is production-required. **Never** put a host-specific key here (NCE-FE-5).

### 5. External client
Use `httpx.AsyncClient` with an explicit 30s timeout, route through
`nce.http_resilience.request_with_retry()` (exponential backoff + jitter), inject auth
headers, handle 404 gracefully. Cache tokens in Redis (`auth.py`) when the system uses OAuth.

### 6. Persistence
- Graph: upsert into `kg_nodes`/`kg_edges` with `ON CONFLICT DO UPDATE`, batch via `UNNEST`,
  attach confidence (0–1), entity_type prefix `<NAME>_<Entity>`. RLS by `namespace_id`.
- Semantic: raw → MongoDB collection; indexed rows → `memories` (embedding + content_fts);
  cognitive metadata → `v3_cognitive_ledger`.
- Domain tables: only if needed beyond the graph; **always** `ENABLE`+`FORCE ROW LEVEL
  SECURITY` + a `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`.
  Mirror DDL into `schema.sql` + a numbered migration.

### 7. Tests (`tests/unit/test_<name>_<domain>.py`)
Mock `httpx.AsyncClient`, `asyncpg.Connection` (RLS-scoped), Redis, Motor. Fixtures for
`namespace_id` (uuid). Assert response shape, DB writes, cache behavior. Add a tool-count
assertion update if you added registry entries.

## Dual-surface exposure: MCP tool + REST endpoint (one core function)

A vertical capability should be callable **both** by an AI agent (MCP) **and** by a
plain HTTP client with **no model involved** (the BFF, the frontend via the BFF,
scripts, cron). Implement the logic once and expose it twice:

```python
# core logic — once
async def do_<action>(engine, params: dict) -> dict: ...

# MCP surface (agents) — TOOL_REGISTRY / register_tool() (NCE-FE-2)
async def handle_<name>_<action>(engine, arguments) -> str:
    return json.dumps(await do_<action>(engine, arguments), default=str)

# REST surface (no AI) — mounted via build_app(extra_routes=...) (NCE-FE-1)
async def api_<name>_<action>(request) -> JSONResponse:
    return JSONResponse(await do_<action>(request.app.state.engine, await _params(request)))
```

- **MCP tool** = for LLM agents; the model only *decides* to call it — the call itself
  isn't AI. Registered in `TOOL_REGISTRY` (built-in verticals) with scope/cache/mutation flags.
- **REST route** = for everything that shouldn't need a model: the BFF/frontend, curl,
  scripts. Authed by the admin app's HMAC/mTLS; **no LLM in the path**.
- **Rule of thumb:** every read-only/deterministic capability gets a REST route so it's
  usable without an AI; mutating/agent-facing ones get the MCP tool; most get both.

## Add-a-vertical checklist
- [ ] `nce/vertical_modules/<name>/` with `mcp_handlers.py` (+ client/auth/sync/ingestion as needed)
- [ ] `ToolSpec` entries in `tool_registry.py` (via `_h`), correct `admin_only`/`mutation`/`cacheable`
- [ ] `NCE_<NAME>_*` config keys (+ validation if prod-required)
- [ ] Optional: admin routes + `nce/admin_handlers/<name>.py`
- [ ] Optional: domain tables with FORCE RLS + migration + `schema.sql` mirror
- [ ] Tests under `tests/unit/`; update tool-count test
- [ ] Gates green: ruff, ruff format, mypy `nce/`, pytest

## Divergence reference (so the template captures the essential, not the incidental)
| Aspect | NetBox (pull-only) | D365 (push + semantic) |
|---|---|---|
| Transport | GraphQL + REST | OData v4 + OAuth (Azure AD) |
| `auth.py` | token header | `DataverseTokenManager` (Redis-cached) |
| `sync.py` | — | entities → kg_edges |
| `ingestion.py` | — | notes → memories + empathic tensor + ledger |
| `webhooks.py` | — | HMAC-validated push |
| admin routes | none (MCP-only) | 8 routes |
| own tables | `on_call_routing` | `d365_netbox_mappings`, uses core tables |

## Change log
- 2026-06-20 — Re-verified against 7304330 (main). Corrected status block; marked
  procurement-intelligence, system-design-engine, and project-management verticals as
  planned/in-flight (not on main); confirmed D365 admin-route count (8), NetBox
  MCP-tool-only constraint, `_h` late-binding pattern, and `register_tool()` hook.
- 2026-06-13 — Initial pattern, distilled from the netbox + d365 verticals.
