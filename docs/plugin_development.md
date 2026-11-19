> **Status:** shipped · **Verified-against:** a40d993 (main) · **Last-audited:** 2026-11-19

# NCE Plugin Development Guide (`nce.plugins`)

Neuro-Cognitive Engine (`NCE`) provides a zero-trust, entry-point-driven plugin architecture (`nce.plugins`) that allows external vertical packages (such as `nce-vertical-engines`) to register MCP tools, FastAPI routes, PostgreSQL migrations, RLS catalog declarations, and cognitive lifecycle hooks without modifying Core source code.

---

## 1. Entry-Point Registration (`pyproject.toml`)

Plugins register under the `nce.plugins` Python package entry-point group in their `pyproject.toml`:

```toml
[project.entry-points."nce.plugins"]
acme_operations = "acme_operations.plugin:AcmeOperationsPlugin"
```

At startup (or on demand via `nce.plugins.load_plugins()`), NCE discovers all installed entry points in `nce.plugins`, filters them against `NCE_ENABLED_ENGINES`, `NCE_ENGINE_TYPE_FILTER`, and `NCE_ENGINE_CATEGORY_FILTER`, and caches the resulting plugin descriptors in O(1) memory (`_DESCRIPTOR_CACHE`).

---

## 2. Implementing the `NCEPlugin` Protocol & `PluginCapabilities`

Every plugin class implements the `NCEPlugin` structural protocol (`nce/plugins.py`) and declares its capabilities via `PluginCapabilities`:

```python
from pathlib import Path
from typing import Any
from nce.plugins import NCEPlugin, PluginCapabilities


class AcmeOperationsPlugin(NCEPlugin):
    plugin_id: str = "acme_operations"
    name: str = "Acme Operations Engine"
    version: str = "1.0.0"

    def get_capabilities(self) -> PluginCapabilities:
        return PluginCapabilities(
            tenant_rls_tables={
                "acme_work_orders": "namespace_id",
                "acme_dispatch_logs": "namespace_id",
            },
            global_tables=frozenset({"acme_iso_country_codes"}),
            unmanaged_pg_sites=frozenset(),
        )

    def get_migration_dir(self) -> Path | None:
        return Path(__file__).resolve().parent / "migrations"

    def register_tools(self, registry: dict[str, Any]) -> None:
        # Register ToolSpec entries into NCE's central TOOL_REGISTRY
        ...

    def register_routes(self, app: Any) -> None:
        # Mount FastAPI APIRouter instances onto admin_server / gateway
        ...

    def register_cognitive_hooks(self, engine: Any) -> None:
        # Attach background watchers or event hooks to NCEEngine
        ...

    async def on_namespace_created(self, conn: Any, namespace_id: Any) -> None:
        # Idempotently seed per-tenant default configuration rows
        await conn.execute(
            """
            INSERT INTO acme_work_orders_config (namespace_id, default_priority)
            VALUES ($1, 'standard')
            ON CONFLICT (namespace_id) DO NOTHING
            """,
            namespace_id,
        )
```

---

## 3. PostgreSQL Migrations & Namespace-Prefixed Ledger

When `get_migration_dir()` returns a directory containing numbered `.sql` files (e.g., `001_acme_work_orders.sql`), `NCEEngine._apply_plugin_pg_migrations()` executes them in lexical order after Core migrations (`001`–`041`) complete.

- **Collision-Free Ledger Keys:** Core migrations are recorded in `applied_migrations` as bare filenames (`038_v351_hotpath_rls_and_indexes.sql`), whereas plugin migrations are recorded with their plugin scope prefix (`acme_operations:001_acme_work_orders.sql`).
- **Mandatory `FORCE ROW LEVEL SECURITY` & `InitPlan` Pattern:** Every tenant table created by a plugin migration **must** enable and force RLS using the `(SELECT get_nce_namespace())` `InitPlan` pattern so PostgreSQL evaluates the tenant GUC once per query rather than per row:

```sql
CREATE TABLE IF NOT EXISTS acme_work_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    order_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_acme_work_orders_ns_status
    ON acme_work_orders (namespace_id, status, created_at DESC);

ALTER TABLE acme_work_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE acme_work_orders FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS acme_work_orders_tenant_isolation ON acme_work_orders;
CREATE POLICY acme_work_orders_tenant_isolation ON acme_work_orders
    FOR ALL
    USING (namespace_id = (SELECT get_nce_namespace()))
    WITH CHECK (namespace_id = (SELECT get_nce_namespace()));

GRANT SELECT, INSERT, UPDATE, DELETE ON acme_work_orders TO nce_app;
```

---

## 4. Progressive Tool Discovery (`/mcp` Gateway `compact` Mode)

To keep LLM context windows lean when hundreds of vertical tools or app connectors are installed:

1. **Initial Compact Exposure:** Sessions connecting to the `/mcp` Streamable HTTP Gateway in `compact` mode initially receive only the **12 Core working-memory tools** (`11` memory/graph/schema tools + `discover_tools`), consuming `~3,100` tokens instead of `~138,000` tokens.
2. **Engine Descriptors (`get_engine_descriptors`) & Connected-First Ranking:** Plugins expose engine metadata (`slug`, `name`, `description`, `types`, `categories`, `tool_count`, `maturity`, `vendor_specific`) so `discover_tools(query=..., type_filter=..., category_filter=..., limit=...)` ranks matching tools in sub-millisecond time (`O(1)` cached catalog + tokenized keyword/BM25 scoring). When a PostgreSQL pool is reachable, `discover_tools` resolves connected `app_slug` values for `session.namespace_id` via `TenantConnectionVault.get_connected_app_slugs()` and boosts tools belonging to connected apps while strictly enforcing the **12-tool cap** in `compact` mode.
3. **Zero-PII Execution Telemetry:** Wrap or route plugin tool execution through `record_plugin_tool_execution(plugin_id, engine_slug, tool_name, duration_s, error=...)` to emit OpenTelemetry span attributes and invocation counters without logging tenant arguments or PII.

---

## 5. Core Connector SDK (`nce.connectors`) — `v3.6.0`

NCE `v3.6.0` ships a vendor-neutral **Core Connector SDK** under `nce/connectors/` so standalone application connectors (such as `nce-app-engines`) and external integrations can build directly on Core primitives without copying security guards, encryption logic, or governance decorators.

### 5.1 `BaseAppConnector`, `ConnectorDescriptor`, `maturity` & `vendor_specific`

Every application connector subclasses `BaseAppConnector` (`nce/connectors/base.py`) and attaches a frozen `ConnectorDescriptor`:

- **`maturity`** (`Literal["stable", "beta", "experimental"]`): Defaults to `"beta"` on `ConnectorDescriptor` and `BaseAppConnector` (while `nce.plugins._normalize_descriptor()` defaults legacy unspecified descriptors to `"stable"`).
- **`vendor_specific`** (`bool`): Defaults to `True` on `ConnectorDescriptor` and `BaseAppConnector` (`False` by default on legacy vertical descriptors unless specified).
- **`auth_kind`** (`Literal["oauth2", "api_key", "entra_oidc", "webhook", "local_file", "none"]`): Declares how tenant credentials are validated and stored in `TenantConnectionVault`.

```python
from nce.connectors import BaseAppConnector, ConnectorDescriptor


class SampleAppConnector(BaseAppConnector):
    descriptor = ConnectorDescriptor(
        slug="sample_cad_app",
        name="Sample CAD App",
        description="Core-only Beta connector built on nce.connectors.",
        types=("app-connector",),
        categories=("operations-delivery",),
        maturity="beta",
        vendor_specific=True,
        auth_kind="oauth2",
        capabilities=("diagram.read", "diagram.write"),
        tool_count=2,
        route_count=1,
    )
```

`list_plugin_engine_descriptors()` and `/admin/plugins/api` normalize and return `maturity` and `vendor_specific` on every engine descriptor, supporting comma-separated case-insensitive `?maturity=` filtering (e.g. `/admin/plugins/api?maturity=beta`) and reporting `summary.maturity` stage counts (`stable`, `beta`, `experimental`).

### 5.2 `@governed_action` & Replay Sandbox Fencing

Use `@governed_action` (`nce/connectors/governance.py`) on connector tools and handlers to enforce four Core invariants automatically:

1. **Replay Sandbox Fence (`assert_not_replay_sandbox`):** Blocks outbound external mutations with `ReplaySandboxFenceError` when `NCE_REPLAY_SANDBOX=true`, `X-NCE-Replay-Sandbox: true`, `is_replay_sandbox=True`, or the tenant namespace has `is_replay_sandbox = TRUE`.
2. **Dry-Run & Approval Gate (`require_approval=True` / `dry_run=True`):** Returns a `pending_approval` preview via `governed_action_response()` with C8 field redaction and an RFC 8785 JCS SHA-256 digest before executing side effects.
3. **C8 Allow-List Redaction (`AllowListRedactor` / `redact_c8_allowlist`):** Strips sensitive credentials (`api_key`, `access_token`, `password`, `encrypted_config`, etc.) and restricted commercial cost fields (`unit_cost`, `margin`, `discount_floor`, etc.) from previews, results, and WORM records.
4. **WORM Event Signing (`record_connector_worm_event`):** When invoked inside an active transaction (`conn.is_in_transaction()`), appends a C8-redacted, JCS-hashed `"connector_action"` entry to `event_log`.

```python
from typing import Any
from nce.connectors import governed_action


@governed_action("sample_cad_app", "sync_project", mutation=True, require_approval=True)
async def sync_project(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"status": "synced", "project_id": arguments["project_id"]}
```

### 5.3 `TenantConnectionVault` & Migration `041_app_connections_vault.sql`

Core Migration `041_app_connections_vault.sql` (`nce/migrations/041_app_connections_vault.sql`) creates the tenant-isolated `app_connections` table:

- **Forced RLS Isolation:** `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY` using `namespace_id = (SELECT get_nce_namespace())`, unique constraint `(namespace_id, app_slug, connection_name)`, and `REVOKE ALL ... FROM PUBLIC`.
- **Envelope Encryption:** `TenantConnectionVault` (`nce/connectors/vault.py`) canonicalizes credentials with RFC 8785 JCS and encrypts `secret_config` with a tenant DEK via `EnvelopeCryptoManager` bound to AAD `app_connections:{namespace_id}:{app_slug}:{connection_name}`.
- **Zero-Secret Metadata API:** `list_connections()` and `get_connection_metadata()` return `AppConnectionMetadata` and **never** select or return `encrypted_config` or plaintext secrets; only `get_decrypted_secret()` unwraps credentials for authorized runtime execution.
- **Admin Vault Endpoints:** `/admin/plugins/api/connections` and `/api/admin/plugins/connections` (`GET`, `POST`, `DELETE`) expose tenant connection management behind standard admin authentication (`X-Admin-Api-Key` / `HMAC-SHA256` + nonce replay protection / `MTLSAuthMiddleware`) and SSRF outbound URL validation (`validate_outbound_url`).

### 5.4 Guards, Entra OIDC & Canonical Envelopes

- **SSRF & Archive/XML Safety (`nce/connectors/guards.py`):** `validate_outbound_url`, `validate_outbound_url_async`, `safe_parse_xml_bytes` (`defusedxml`), `safe_extract_zip_bytes` (zip-slip, symlink, executable suffix, and ratio limits), `safe_inflate_raw_deflate`, and `sanitize_html_label`.
- **Entra ID OIDC Verification (`nce/connectors/entra.py`):** `EntraOIDCVerifier` enforces asymmetric JWT signatures (`RS256`–`PS512`, rejecting `HS*`/`none`) and required role/scope checks.
- **Canonical Envelopes & Substrate (`nce/connectors/envelopes.py`, `nce/connectors/substrate.py`):** `CanonicalEntityEnvelope`, `CanonicalDiagramIR`, `diff_diagram_ir`, `ArtifactStoreAdapter`, `BackgroundJobRunner`, `TenantSecretBox`, and `emit_kg_and_worm_link`.
