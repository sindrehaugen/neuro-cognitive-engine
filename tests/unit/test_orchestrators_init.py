"""Smoke tests for ``nce.orchestrators`` PEP 562 lazy loading and registry API."""

from __future__ import annotations

import importlib
import inspect
import sys

import pytest

_EXPECTED_ALL = [
    "MemoryOrchestrator",
    "CognitiveOrchestrator",
    "GraphOrchestrator",
    "MigrationOrchestrator",
    "NamespaceOrchestrator",
    "TemporalOrchestrator",
]

_ORCHESTRATOR_SUBMODULES = (
    "nce.orchestrators.memory",
    "nce.orchestrators.cognitive",
    "nce.orchestrators.graph",
    "nce.orchestrators.migration",
    "nce.orchestrators.namespace",
    "nce.orchestrators.temporal",
)

_REGISTRY_KEYS = (
    ("memory", "MemoryOrchestrator"),
    ("cognitive", "CognitiveOrchestrator"),
    ("graph", "GraphOrchestrator"),
    ("migration", "MigrationOrchestrator"),
    ("namespace", "NamespaceOrchestrator"),
    ("temporal", "TemporalOrchestrator"),
)


def _purge_orchestrators_modules() -> None:
    for key in list(sys.modules):
        if key == "nce.orchestrators" or key.startswith("nce.orchestrators."):
            del sys.modules[key]


def _fresh_orchestrators():
    _purge_orchestrators_modules()
    return importlib.import_module("nce.orchestrators")


def test_bare_import_does_not_eagerly_load_orchestrator_modules() -> None:
    _purge_orchestrators_modules()

    import nce.orchestrators  # noqa: F401

    for module_name in _ORCHESTRATOR_SUBMODULES:
        assert module_name not in sys.modules


def test_lazy_load_memory_orchestrator() -> None:
    pkg = _fresh_orchestrators()

    cls_first = pkg.MemoryOrchestrator

    assert inspect.isclass(cls_first)
    assert "nce.orchestrators.memory" in sys.modules
    assert pkg.MemoryOrchestrator is cls_first


def test_all_contains_expected_exports() -> None:
    pkg = _fresh_orchestrators()

    assert pkg.__all__ == _EXPECTED_ALL
    assert len(pkg.__all__) == 6


@pytest.mark.parametrize("registry_key,class_name", _REGISTRY_KEYS)
def test_get_orchestrator_class_returns_lazy_export(
    registry_key: str,
    class_name: str,
) -> None:
    pkg = _fresh_orchestrators()

    cls_via_registry = pkg.get_orchestrator_class(registry_key)
    cls_via_attr = getattr(pkg, class_name)

    assert cls_via_registry is cls_via_attr
    assert inspect.isclass(cls_via_registry)


def test_get_orchestrator_class_unknown_key_raises_key_error() -> None:
    pkg = _fresh_orchestrators()

    with pytest.raises(KeyError, match="Unknown orchestrator"):
        pkg.get_orchestrator_class("bogus")


def test_unknown_attribute_raises_attribute_error() -> None:
    pkg = _fresh_orchestrators()

    with pytest.raises(AttributeError):
        _ = pkg.BogusOrchestrator
