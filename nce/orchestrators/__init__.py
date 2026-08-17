"""Domain orchestrators — extracted from NCEEngine per Clean Code (Uncle Bob) SRP split.

Each orchestrator receives shared connection pools (PG, Mongo, Redis) via constructor
injection.  NCEEngine creates them during connect() and delegates method calls
through thin pass-through wrappers for backward compatibility.

Public surface is lazy-loaded to avoid circular imports and heavy startup cost.
Use the REGISTRY dict for dynamic orchestrator discovery.
"""

__all__ = [
    "MemoryOrchestrator",
    "CognitiveOrchestrator",
    "GraphOrchestrator",
    "MigrationOrchestrator",
    "NamespaceOrchestrator",
    "TemporalOrchestrator",
]

# Lazy orchestrator registry — maps canonical name → (module_path, class_name)
_REGISTRY: dict[str, tuple[str, str]] = {
    "memory": (".memory", "MemoryOrchestrator"),
    "cognitive": (".cognitive", "CognitiveOrchestrator"),
    "graph": (".graph", "GraphOrchestrator"),
    "migration": (".migration", "MigrationOrchestrator"),
    "namespace": (".namespace", "NamespaceOrchestrator"),
    "temporal": (".temporal", "TemporalOrchestrator"),
}

# Build a reverse lookup: class_name → (module_path, class_name)
_BY_CLASS: dict[str, tuple[str, str]] = {v[1]: v for v in _REGISTRY.values()}


def __getattr__(name: str) -> type:
    """PEP 562 lazy loader — defers orchestrator imports until first access."""
    if name in _BY_CLASS:
        module_path, class_name = _BY_CLASS[name]
        import importlib

        mod = importlib.import_module(module_path, package=__name__)
        cls = getattr(mod, class_name)
        # Cache on the module so subsequent accesses are O(1)
        globals()[name] = cls
        return cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_orchestrator_class(name: str) -> type:
    """Return an orchestrator class by its canonical registry name.

    Args:
        name: Registry key e.g. ``"memory"``, ``"cognitive"``.

    Returns:
        The orchestrator class (lazy-loaded on first call).

    Raises:
        KeyError: If *name* is not a registered orchestrator.
    """
    if name not in _REGISTRY:
        raise KeyError(f"Unknown orchestrator {name!r}. Available: {sorted(_REGISTRY)}")
    module_path, class_name = _REGISTRY[name]
    return __getattr__(class_name)
