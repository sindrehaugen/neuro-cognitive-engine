> **Status:** shipped · **Verified-against:** a40d993 (main) · **Last-audited:** 2026-11-17

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

When `get_migration_dir()` returns a directory containing numbered `.sql` files (e.g., `001_acme_work_orders.sql`), `NCEEngine._apply_plugin_pg_migrations()` executes them in lexical order after Core migrations (`001`–`038`) complete.

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

To keep LLM context windows lean when hundreds of vertical tools are installed:

1. **Initial Compact Exposure:** Sessions connecting to the `/mcp` Streamable HTTP Gateway in `compact` mode initially receive only the **12 Core working-memory tools** (`11` memory/graph/schema tools + `discover_tools`), consuming `~3,100` tokens instead of `~138,000` tokens.
2. **Engine Descriptors (`get_engine_descriptors`):** Plugins expose engine metadata (`slug`, `name`, `description`, `types`, `categories`, `tool_count`) so `discover_tools(query=..., type_filter=..., category_filter=..., limit=...)` ranks matching tools in sub-millisecond time (`O(1)` cached catalog + tokenized keyword/BM25 scoring).
3. **Zero-PII Execution Telemetry:** Wrap or route plugin tool execution through `record_plugin_tool_execution(plugin_id, engine_slug, tool_name, duration_s, error=...)` to emit OpenTelemetry span attributes and invocation counters without logging tenant arguments or PII.
