"""NCE Plugin Spine & Discovery System.

Defines the NCEPlugin protocol and dynamically discovers and registers
external plugins (such as nce-vertical-engines) via Python's entry_points API.
"""

from __future__ import annotations

import importlib.metadata
import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "nce.plugins"


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


def _normalize_filter_tokens(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {tok.strip().lower() for tok in raw.split(",") if tok.strip()}


def _normalize_descriptor(raw: dict[str, Any], default_id: str) -> dict[str, Any]:
    types_raw = raw.get("types") or ()
    categories_raw = raw.get("categories") or ()
    types_list = sorted({str(t).strip() for t in types_raw if str(t).strip()})
    categories_list = sorted({str(c).strip() for c in categories_raw if str(c).strip()})
    plugin_id = str(raw.get("id") or raw.get("plugin_id") or default_id)
    return {
        **raw,
        "id": plugin_id,
        "plugin_id": str(raw.get("plugin_id") or default_id),
        "name": str(raw.get("name") or plugin_id),
        "types": types_list,
        "categories": categories_list,
        "installed": bool(raw.get("installed", True)),
        "enabled": bool(raw.get("enabled", True)),
    }


def get_plugin_descriptors(
    *,
    type_filter: str | None = None,
    category_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Collect and optionally filter plugin descriptors by type and/or category facets."""
    wanted_types = _normalize_filter_tokens(type_filter)
    wanted_categories = _normalize_filter_tokens(category_filter)
    descriptors: list[dict[str, Any]] = []

    for plugin_id, plugin in load_plugins().items():
        raw_items: list[dict[str, Any]] = []
        getter = getattr(plugin, "get_plugin_descriptors", None)
        if callable(getter):
            try:
                res = getter()
                if isinstance(res, list):
                    raw_items.extend(d for d in res if isinstance(d, dict))
            except Exception as exc:
                log.exception("Error reading descriptors from plugin %s: %s", plugin_id, exc)
        if not raw_items:
            raw_items.append(
                {
                    "id": plugin_id,
                    "plugin_id": plugin_id,
                    "name": getattr(plugin, "name", plugin_id),
                    "types": list(getattr(plugin, "types", ()) or ()),
                    "categories": list(getattr(plugin, "categories", ()) or ()),
                    "installed": bool(getattr(plugin, "installed", True)),
                    "enabled": bool(getattr(plugin, "enabled", True)),
                }
            )
        for item in raw_items:
            norm = _normalize_descriptor(item, plugin_id)
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
