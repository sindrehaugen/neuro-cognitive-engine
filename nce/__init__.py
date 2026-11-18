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

from __future__ import annotations

import logging as _logging
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

import tomllib

__all__ = [
    "NCEEngine",
    "MemoryPayload",
    "MediaPayload",
    "ArtifactPayload",
    "StoreArtifactRequest",
    "OrchestratorConfig",
    "run_gc_loop",
]


def _resolve_package_version() -> str:
    """Resolve NCE version from source pyproject.toml or installed package metadata."""
    pyproject_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyproject.toml"
    )
    if os.path.isfile(pyproject_path):
        try:
            with open(pyproject_path, "rb") as fh:
                data = tomllib.load(fh)
            ver = data.get("project", {}).get("version")
            if isinstance(ver, str) and ver.strip():
                return ver.strip()
        except Exception:
            pass

    for dist_name in ("neuro-cognitive-engine", "nce"):
        try:
            return _pkg_version(dist_name)
        except PackageNotFoundError:
            continue
    return "0.0.0+dev"


__version__: str = _resolve_package_version()

__author__ = "Sindre Løvlie Haugen"
__license__ = "Proprietary"
__homepage__ = "https://github.com/sindrehaugen/NCE"

_log = _logging.getLogger(__name__)

# Lazy import map: public name → (module_path, attr_name)
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "NCEEngine": ("nce.orchestrator", "NCEEngine"),
    "MemoryPayload": ("nce.orchestrator", "MemoryPayload"),
    "MediaPayload": ("nce.orchestrator", "MediaPayload"),
    "ArtifactPayload": ("nce.models", "ArtifactPayload"),
    "StoreArtifactRequest": ("nce.models", "StoreArtifactRequest"),
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
