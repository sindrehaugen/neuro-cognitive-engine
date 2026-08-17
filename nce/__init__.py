"""
Neuro-Cognitive Engine (NCE) — public package API.

Exports the stable, versioned surface of the NCE package. All names are
lazy-loaded: importing this module does NOT trigger loading of heavy internal
modules (orchestrator, garbage_collector) until those names are first accessed.

Public API:
  NCEEngine           — primary orchestration engine (use to connect/disconnect)
  MemoryPayload       — typed payload for episodic memory writes
  MediaPayload        — typed payload for artifact/media ingestion
  OrchestratorConfig  — runtime configuration object
  run_gc_loop         — OPERATIONAL: background GC coroutine, intended for use
                        in server entrypoints only (server.py / admin_server.py).
                        Do not call from library code or tests.

The MCP stdio entrypoint (server.py) and admin server (admin_server.py) live
at the repo root and import everything they need from here.
"""

__all__ = [
    "NCEEngine",
    "MemoryPayload",
    "MediaPayload",
    "OrchestratorConfig",
    "run_gc_loop",
]

try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    __version__: str = _pkg_version("nce")
except PackageNotFoundError:
    # Package not installed in editable/development mode or metadata missing.
    # Fallback keeps the attribute available without raising at import time.
    __version__ = "0.0.0+dev"

__author__ = "Sindre Løvlie Haugen"
__license__ = "Proprietary"
__homepage__ = "https://github.com/sindrehaugen/NCE"

import logging as _logging

_log = _logging.getLogger(__name__)

# Lazy import map: public name → (module_path, attr_name)
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "NCEEngine": ("nce.orchestrator", "NCEEngine"),
    "MemoryPayload": ("nce.orchestrator", "MemoryPayload"),
    "MediaPayload": ("nce.orchestrator", "MediaPayload"),
    "OrchestratorConfig": ("nce.config", "OrchestratorConfig"),
    "run_gc_loop": ("nce.garbage_collector", "run_gc_loop"),
}


def __getattr__(name: str):
    """Lazy-load public names to avoid heavy imports at package import time.

    Isolates import errors so that a partially broken environment does not
    make the entire nce package unavailable.
    """
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        try:
            import importlib

            module = importlib.import_module(module_path)
            obj = getattr(module, attr_name)
            globals()[name] = obj
            return obj
        except ImportError as exc:
            _log.warning(
                "nce: deferred import of %r failed: %s — "
                "this name will be unavailable until the dependency is installed.",
                name,
                exc,
            )
            raise AttributeError(f"nce.{name} is unavailable: {exc}") from exc

    # Preserve `from nce import <submodule>` for names not in __all__.
    import importlib

    try:
        module = importlib.import_module(f"nce.{name}")
    except ModuleNotFoundError as exc:
        raise AttributeError(f"module 'nce' has no attribute {name!r}") from exc
    globals()[name] = module
    return module
