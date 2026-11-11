"""NCE Plugin Spine & Discovery System.

Defines the NCEPlugin protocol and dynamically discovers and registers
external plugins (such as nce-vertical-engines) via Python's entry_points API.
"""

from __future__ import annotations

import importlib.metadata
import logging
import re
from pathlib import Path
from typing import Any, Protocol, TypedDict, runtime_checkable

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "nce.plugins"


class EngineDescriptor(TypedDict, total=False):
    """Typed descriptor contract for an engine/module exported by an NCEPlugin."""

    slug: str
    id: str
    name: str
    plugin_id: str
    description: str
    version: str
    types: list[str]
    categories: list[str]
    installed: bool
    enabled: bool
    enabled_by_env: bool
    tool_count: int
    route_count: int
    requires_schemas: list[str]
    install_extra: str


@runtime_checkable
class NCEPlugin(Protocol):
    """Protocol that all NCE plugins must implement."""

    plugin_id: str

    def register_tools(self, registry: Any) -> None:
        """Register MCP tools into the central TOOL_REGISTRY dictionary."""
        ...

    def register_routes(self, app: Any) -> None:
        """Mount HTTP/REST routes onto FastAPI/Starlette applications."""
        ...

    def register_workers(self, worker_registry: Any) -> None:
        """Register background job handlers and queue definitions."""
        ...

    def register_cognitive_hooks(self, cognitive_engine: Any) -> None:
        """Register cognitive hooks (spreading activation, consolidation, NLI)."""
        ...

    def get_migration_dir(self) -> Path | None:
        """Return the directory containing PostgreSQL migration files for this plugin."""
        ...


_DISCOVERED_PLUGINS: dict[str, NCEPlugin] | None = None


def load_plugins(*, force_reload: bool = False) -> dict[str, NCEPlugin]:
    """Discover and instantiate all plugins registered under 'nce.plugins'."""
    global _DISCOVERED_PLUGINS
    if _DISCOVERED_PLUGINS is not None and not force_reload:
        return _DISCOVERED_PLUGINS

    plugins: dict[str, NCEPlugin] = {}
    try:
        entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:
        log.warning("Failed to query entry points for %s: %s", ENTRY_POINT_GROUP, exc)
        entry_points = ()  # type: ignore[assignment]

    for ep in entry_points:
        try:
            plugin_cls = ep.load()
            plugin_instance = plugin_cls() if callable(plugin_cls) else plugin_cls
            plugin_id = getattr(plugin_instance, "plugin_id", ep.name)
            plugins[plugin_id] = plugin_instance
            log.info("Discovered and loaded NCE plugin: %s (%s)", plugin_id, ep.value)
        except Exception as exc:
            log.exception("Failed to load NCE plugin %s: %s", ep.name, exc)

    _DISCOVERED_PLUGINS = plugins
    return plugins


def register_all_plugin_tools(registry: Any) -> None:
    """Invoke register_tools on all discovered plugins."""
    for plugin_id, plugin in load_plugins().items():
        try:
            plugin.register_tools(registry)
            log.info("Registered tools from plugin: %s", plugin_id)
        except Exception as exc:
            log.exception("Error registering tools from plugin %s: %s", plugin_id, exc)


def register_all_plugin_routes(app: Any) -> None:
    """Invoke register_routes on all discovered plugins."""
    for plugin_id, plugin in load_plugins().items():
        try:
            plugin.register_routes(app)
            log.info("Registered routes from plugin: %s", plugin_id)
        except Exception as exc:
            log.exception("Error registering routes from plugin %s: %s", plugin_id, exc)


def register_all_plugin_workers(worker_registry: Any) -> None:
    """Invoke register_workers on all discovered plugins."""
    for plugin_id, plugin in load_plugins().items():
        try:
            plugin.register_workers(worker_registry)
            log.info("Registered workers from plugin: %s", plugin_id)
        except Exception as exc:
            log.exception("Error registering workers from plugin %s: %s", plugin_id, exc)


def register_all_plugin_cognitive_hooks(cognitive_engine: Any) -> None:
    """Invoke register_cognitive_hooks on all discovered plugins."""
    for plugin_id, plugin in load_plugins().items():
        try:
            plugin.register_cognitive_hooks(cognitive_engine)
            log.info("Registered cognitive hooks from plugin: %s", plugin_id)
        except Exception as exc:
            log.exception("Error registering cognitive hooks from plugin %s: %s", plugin_id, exc)


def get_all_plugin_migration_dirs() -> dict[str, Path]:
    """Collect unique non-None migration directories from all discovered plugins."""
    migration_dirs: dict[str, Path] = {}
    seen_resolved: set[Path] = set()
    plugins = load_plugins()
    ordered_ids = sorted(plugins.keys(), key=lambda k: (0 if k == "nce_verticals" else 1, k))
    for plugin_id in ordered_ids:
        plugin = plugins[plugin_id]
        try:
            mdir = plugin.get_migration_dir()
            if mdir is not None and Path(mdir).is_dir():
                resolved = Path(mdir).resolve()
                if resolved not in seen_resolved:
                    seen_resolved.add(resolved)
                    migration_dirs[plugin_id] = Path(mdir)
        except Exception as exc:
            log.exception("Error obtaining migration dir from plugin %s: %s", plugin_id, exc)
    return migration_dirs


_SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_SQL_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_SQL_ENABLE_RLS_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(?:public\.)?([a-zA-Z_][a-zA-Z0-9_]*)\s+"
    r"ENABLE\s+ROW\s+LEVEL\s+SECURITY",
    re.IGNORECASE,
)


def get_all_plugin_tenant_rls_tables() -> dict[str, str]:
    """Collect tenant RLS table -> namespace column declarations from discovered plugins.

    Combines explicit ``plugin.get_tenant_rls_tables()`` mappings (when provided)
    with ``ALTER TABLE ... ENABLE ROW LEVEL SECURITY`` statements declared in
    each plugin's SQL migration directory so external plugins remain decoupled
    from Core's static catalog while still undergoing fail-closed RLS policy checks.
    """
    tables: dict[str, str] = {}
    plugins = load_plugins()
    for plugin_id, plugin in plugins.items():
        getter = getattr(plugin, "get_tenant_rls_tables", None)
        if callable(getter):
            try:
                explicit = getter()
                if isinstance(explicit, dict):
                    for tbl, col in explicit.items():
                        if (
                            isinstance(tbl, str)
                            and isinstance(col, str)
                            and tbl.strip()
                            and col.strip()
                        ):
                            tables[tbl.strip().lower()] = col.strip()
            except Exception as exc:
                log.exception("Error reading tenant RLS tables from plugin %s: %s", plugin_id, exc)

    for plugin_id, mdir in get_all_plugin_migration_dirs().items():
        try:
            for sql_file in sorted(mdir.glob("*.sql")):
                raw_sql = sql_file.read_text(encoding="utf-8")
                cleaned = _SQL_LINE_COMMENT_RE.sub("", _SQL_BLOCK_COMMENT_RE.sub("", raw_sql))
                for match in _SQL_ENABLE_RLS_RE.finditer(cleaned):
                    tables.setdefault(match.group(1).lower(), "namespace_id")
        except Exception as exc:
            log.exception(
                "Error scanning RLS tables in migrations for plugin %s: %s", plugin_id, exc
            )

    return tables


def _normalize_filter_tokens(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {tok.strip().lower() for tok in raw.split(",") if tok.strip()}


def _normalize_descriptor(raw: dict[str, Any], default_id: str) -> dict[str, Any]:
    types_raw = raw.get("types") or ()
    categories_raw = raw.get("categories") or ()
    requires_raw = raw.get("requires_schemas") or ()
    types_list = sorted({str(t).strip() for t in types_raw if str(t).strip()})
    categories_list = sorted({str(c).strip() for c in categories_raw if str(c).strip()})
    requires_list = [str(r).strip() for r in requires_raw if str(r).strip()]
    slug = str(raw.get("slug") or raw.get("id") or raw.get("plugin_id") or default_id)
    plugin_id = str(raw.get("plugin_id") or default_id)
    installed = bool(raw.get("installed", True))
    enabled_by_env = bool(raw.get("enabled_by_env", raw.get("enabled", installed)))
    enabled = bool(raw.get("enabled", enabled_by_env and installed))
    return {
        **raw,
        "slug": slug,
        "id": str(raw.get("id") or slug),
        "plugin_id": plugin_id,
        "name": str(raw.get("name") or slug),
        "description": str(raw.get("description") or ""),
        "version": str(raw.get("version") or "3.0.0"),
        "types": types_list,
        "categories": categories_list,
        "installed": installed,
        "enabled": enabled,
        "enabled_by_env": enabled_by_env,
        "tool_count": int(raw.get("tool_count") or 0),
        "route_count": int(raw.get("route_count") or 0),
        "requires_schemas": requires_list,
        "install_extra": str(raw.get("install_extra") or slug),
    }


def list_plugin_engine_descriptors(
    *,
    type_filter: str | None = None,
    category_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Collect and optionally filter normalized engine descriptors across all plugins."""
    wanted_types = _normalize_filter_tokens(type_filter)
    wanted_categories = _normalize_filter_tokens(category_filter)
    descriptors: list[dict[str, Any]] = []

    plugins_map = load_plugins()
    umbrella_modules: set[str] = set()
    for p in plugins_map.values():
        if callable(
            getattr(p, "get_plugin_descriptors", None) or getattr(p, "get_engine_descriptors", None)
        ):
            umbrella_modules.add(p.__class__.__module__)

    seen_slugs: set[str] = set()
    for plugin_id, plugin in plugins_map.items():
        raw_items: list[dict[str, Any]] = []
        getter = getattr(plugin, "get_plugin_descriptors", None) or getattr(
            plugin, "get_engine_descriptors", None
        )
        if callable(getter):
            try:
                res = getter()
                if isinstance(res, list):
                    raw_items.extend(d for d in res if isinstance(d, dict))
            except Exception as exc:
                log.exception("Error reading descriptors from plugin %s: %s", plugin_id, exc)
        elif plugin.__class__.__module__ not in umbrella_modules:
            raw_items.append(
                {
                    "slug": plugin_id,
                    "id": plugin_id,
                    "plugin_id": plugin_id,
                    "name": getattr(plugin, "name", plugin_id),
                    "description": getattr(plugin, "description", ""),
                    "types": list(getattr(plugin, "types", ()) or ()),
                    "categories": list(getattr(plugin, "categories", ()) or ()),
                    "installed": bool(getattr(plugin, "installed", True)),
                    "enabled": bool(getattr(plugin, "enabled", True)),
                    "enabled_by_env": bool(getattr(plugin, "enabled_by_env", True)),
                    "tool_count": int(getattr(plugin, "tool_count", 0) or 0),
                    "route_count": int(getattr(plugin, "route_count", 0) or 0),
                    "requires_schemas": list(getattr(plugin, "requires_schemas", ()) or ()),
                    "install_extra": str(getattr(plugin, "install_extra", plugin_id)),
                }
            )
        for item in raw_items:
            norm = _normalize_descriptor(item, plugin_id)
            slug_key = norm["slug"]
            if slug_key in seen_slugs:
                continue
            seen_slugs.add(slug_key)
            if wanted_types:
                item_types = {t.lower() for t in norm["types"]}
                if not (item_types & wanted_types):
                    continue
            if wanted_categories:
                item_cats = {c.lower() for c in norm["categories"]}
                if not (item_cats & wanted_categories):
                    continue
            descriptors.append(norm)

    return descriptors


def get_plugin_descriptors(
    *,
    type_filter: str | None = None,
    category_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Collect and optionally filter plugin descriptors by type and/or category facets."""
    return list_plugin_engine_descriptors(
        type_filter=type_filter,
        category_filter=category_filter,
    )


# ---------------------------------------------------------------------------
# Plugin Execution Telemetry & Observability (Zero-PII)
# ---------------------------------------------------------------------------

_PLUGIN_METRICS: dict[str, dict[str, int | float]] = {}


def record_plugin_tool_execution(
    plugin_id: str,
    engine_slug: str,
    tool_name: str,
    duration_s: float,
    *,
    error: bool = False,
) -> None:
    """Record lightweight, zero-PII plugin tool execution metrics.

    Records invocation count, error count, and cumulative duration in
    _PLUGIN_METRICS[f"{plugin_id}:{engine_slug}:{tool_name}"] (strictly
    metadata-only: never logs or stores tool arguments or tenant PII).
    Emits an OpenTelemetry span event or attributes if opentelemetry.trace
    has an active recording span (wrapped in try/except Exception: pass).
    """
    key = f"{plugin_id}:{engine_slug}:{tool_name}"
    metrics = _PLUGIN_METRICS.setdefault(
        key,
        {
            "invocations": 0,
            "errors": 0,
            "total_duration_s": 0.0,
        },
    )
    metrics["invocations"] = int(metrics.get("invocations", 0)) + 1
    if error:
        metrics["errors"] = int(metrics.get("errors", 0)) + 1
    metrics["total_duration_s"] = float(metrics.get("total_duration_s", 0.0)) + float(duration_s)

    try:
        from opentelemetry import trace  # type: ignore[import-not-found]

        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("plugin.id", plugin_id)
            span.set_attribute("plugin.engine_slug", engine_slug)
            span.set_attribute("plugin.tool_name", tool_name)
            span.set_attribute("plugin.duration_s", float(duration_s))
            span.set_attribute("plugin.error", bool(error))
            span.add_event(
                "plugin_tool_execution",
                {
                    "plugin.id": plugin_id,
                    "plugin.engine_slug": engine_slug,
                    "plugin.tool_name": tool_name,
                    "plugin.duration_s": float(duration_s),
                    "plugin.error": bool(error),
                },
            )
    except Exception:
        pass


def get_plugin_metrics_snapshot() -> dict[str, dict[str, int | float]]:
    """Return a shallow copy of _PLUGIN_METRICS for observability / diagnostics inspection."""
    return {k: dict(v) for k, v in _PLUGIN_METRICS.items()}
