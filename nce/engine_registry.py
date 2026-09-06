"""
nce/engine_registry.py
======================
W-1: The Engine Registry — unified shared-core accessor for NCE vertical modules.

Eliminates fragile `hasattr(engine, "<name>")` probes and string-named tool calls
across vertical engines by providing a single, opt-in-aware mapping on `engine.modules`.

Convention:
    Callers access vertical modules via:
        engine.modules["support"].do_open_ticket(...)
    or with explicit namespace scoping:
        engine.modules.for_namespace(namespace_id)["support"].do_open_ticket(...)

Documented Refusals:
    - EngineDisabledError: raised when an engine is disabled for a namespace.
    - EngineNotFoundError: raised when an engine name is not registered.
    Both inherit from EngineUnavailableError (which subclasses KeyError).
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Iterator, Mapping
from typing import Any

log = logging.getLogger("nce.engine_registry")

_SENTINEL = object()

VERTICAL_MODULE_NAMES: tuple[str, ...] = (
    "agreements",
    "assets",
    "business_insights",
    "customer_portal",
    "diagnostics",
    "dynamics365",
    "economy",
    "field_tech",
    "hr",
    "inventory",
    "marketing",
    "netbox",
    "procurement",
    "product",
    "project",
    "resources",
    "sales",
    "support",
    "system_design",
    "vendors",
)


class EngineRegistryError(Exception):
    """Base exception for all engine registry errors."""


class EngineUnavailableError(EngineRegistryError, KeyError):
    """Documented refusal raised when an engine cannot be provided.

    Inherits from KeyError so standard mapping protocols work as expected,
    and from EngineRegistryError for domain-specific exception handling.
    """


class EngineDisabledError(EngineUnavailableError):
    """Raised when an engine is disabled for a given tenant namespace."""


class EngineNotFoundError(EngineUnavailableError):
    """Raised when an engine name is not registered in the engine registry."""


class NamespaceScopedRegistry(Mapping[str, Any]):
    """An opt-in-aware view of the engine registry scoped to a tenant namespace.

    Engines disabled for this namespace do not appear in membership checks (`in`),
    and subscript access raises `EngineDisabledError` (an `EngineUnavailableError`).
    """

    def __init__(
        self,
        registry: EngineRegistry,
        namespace_id: str,
        disabled: set[str] | None = None,
        enabled: set[str] | None = None,
    ) -> None:
        self._registry = registry
        self._namespace_id = str(namespace_id)
        self._local_disabled = set(disabled) if disabled is not None else None
        self._local_enabled = set(enabled) if enabled is not None else None

    @property
    def namespace_id(self) -> str:
        return self._namespace_id

    def is_enabled(self, name: str) -> bool:
        """Check if *name* is enabled for this namespace."""
        if self._local_disabled is not None and name in self._local_disabled:
            return False
        if self._local_enabled is not None:
            return name in self._local_enabled
        return self._registry.is_enabled(name, self._namespace_id)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        return (name in self._registry._modules) and self.is_enabled(name)

    def __getitem__(self, name: str) -> Any:
        if name not in self._registry._modules:
            raise EngineNotFoundError(f"Engine '{name}' is not registered")
        if not self.is_enabled(name):
            raise EngineDisabledError(
                f"Engine '{name}' is disabled for namespace '{self._namespace_id}'"
            )
        return self._registry._modules[name]

    def get(self, name: str, default: Any = _SENTINEL) -> Any:
        if name not in self._registry._modules:
            if default is not _SENTINEL:
                return default
            raise EngineNotFoundError(f"Engine '{name}' is not registered")
        if not self.is_enabled(name):
            if default is not _SENTINEL:
                return default
            raise EngineDisabledError(
                f"Engine '{name}' is disabled for namespace '{self._namespace_id}'"
            )
        return self._registry._modules[name]

    def __iter__(self) -> Iterator[str]:
        for name in self._registry._modules:
            if self.is_enabled(name):
                yield name

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __repr__(self) -> str:
        active = sorted(list(self))
        return f"<NamespaceScopedRegistry namespace={self._namespace_id!r} active={active}>"


class EngineRegistry(Mapping[str, Any]):
    """Mapping of registered vertical modules with namespace opt-in awareness."""

    def __init__(self, engine: Any = None) -> None:
        self._engine = engine
        self._modules: dict[str, Any] = {}
        self._disabled_by_namespace: dict[str, set[str]] = {}
        self._enabled_by_namespace: dict[str, set[str]] = {}
        self._opt_in_checker: Callable[[str, str], bool] | None = None

    @property
    def engine(self) -> Any:
        return self._engine

    def register(self, name: str, module: Any) -> None:
        """Register a vertical module under *name*."""
        self._modules[name] = module

    def set_opt_in_checker(self, checker: Callable[[str, str], bool] | None) -> None:
        """Set a dynamic opt-in callback: checker(engine_name, namespace_id) -> bool."""
        self._opt_in_checker = checker

    def disable_for_namespace(self, namespace_id: str, *module_names: str) -> None:
        """Explicitly mark one or more modules as disabled for a namespace."""
        ns_str = str(namespace_id)
        current = self._disabled_by_namespace.setdefault(ns_str, set())
        current.update(module_names)
        if ns_str in self._enabled_by_namespace:
            self._enabled_by_namespace[ns_str].difference_update(module_names)

    def enable_for_namespace(self, namespace_id: str, *module_names: str) -> None:
        """Explicitly mark one or more modules as enabled for a namespace."""
        ns_str = str(namespace_id)
        current = self._enabled_by_namespace.setdefault(ns_str, set())
        current.update(module_names)
        if ns_str in self._disabled_by_namespace:
            self._disabled_by_namespace[ns_str].difference_update(module_names)

    def is_enabled(self, name: str, namespace_id: str | None = None) -> bool:
        """Check if *name* is registered and enabled for *namespace_id*."""
        if name not in self._modules:
            return False
        if namespace_id is None:
            return True
        ns_str = str(namespace_id)
        disabled_set = self._disabled_by_namespace.get(ns_str)
        if disabled_set is not None and name in disabled_set:
            return False
        enabled_set = self._enabled_by_namespace.get(ns_str)
        if enabled_set is not None and name in enabled_set:
            return True
        if self._opt_in_checker is not None:
            try:
                return bool(self._opt_in_checker(name, ns_str))
            except Exception as exc:
                log.warning("Opt-in checker error for %s in %s: %s", name, ns_str, exc)
                return False
        return True

    def for_namespace(
        self,
        namespace_id: str,
        disabled: set[str] | None = None,
        enabled: set[str] | None = None,
    ) -> NamespaceScopedRegistry:
        """Return an opt-in-aware mapping view scoped to *namespace_id*."""
        return NamespaceScopedRegistry(
            self,
            namespace_id=str(namespace_id),
            disabled=disabled,
            enabled=enabled,
        )

    def __contains__(self, key: object) -> bool:
        return key in self._modules

    def __getitem__(self, key: str | tuple[str, str]) -> Any:
        if isinstance(key, tuple):
            name, namespace_id = key
            return self.for_namespace(namespace_id)[name]
        if key not in self._modules:
            raise EngineNotFoundError(f"Engine '{key}' is not registered")
        return self._modules[key]

    def get(
        self,
        key: str,
        default: Any = _SENTINEL,
        namespace_id: str | None = None,
    ) -> Any:
        """Get an engine module, optionally scoped to a tenant namespace."""
        if namespace_id is not None:
            return self.for_namespace(namespace_id).get(key, default=default)
        if key in self._modules:
            return self._modules[key]
        if default is not _SENTINEL:
            return default
        raise EngineNotFoundError(f"Engine '{key}' is not registered")

    def __iter__(self) -> Iterator[str]:
        return iter(self._modules)

    def __len__(self) -> int:
        return len(self._modules)

    def __repr__(self) -> str:
        return f"<EngineRegistry modules={sorted(list(self._modules.keys()))}>"


def populate_engine_modules(
    engine: Any,
    module_names: tuple[str, ...] = VERTICAL_MODULE_NAMES,
) -> EngineRegistry:
    """Populate and attach ``engine.modules`` from ``nce.vertical_modules.*``.

    Works for both ``NCEEngine`` instances and relay ducks (such as
    ``SimpleNamespace(pg_pool=pool)`` in ``nce/cron.py``).

    Parameters
    ----------
    engine:
        The orchestration engine or relay namespace.
    module_names:
        The tuple of vertical module package names to register.

    Returns
    -------
    EngineRegistry:
        The populated registry attached to ``engine.modules``.
    """
    registry = EngineRegistry(engine=engine)
    for name in module_names:
        try:
            module = importlib.import_module(f"nce.vertical_modules.{name}")
            registry.register(name, module)
        except Exception as exc:
            log.warning("Could not register vertical module %s: %s", name, exc)

    try:
        setattr(engine, "modules", registry)
    except Exception as exc:
        log.warning("Could not set 'modules' on engine: %s", exc)

    return registry
