"""Public package API contract for ``nce.__init__`` lazy exports."""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest

_EXPECTED_ALL = frozenset(
    {
        "NCEEngine",
        "MemoryPayload",
        "MediaPayload",
        "OrchestratorConfig",
        "run_gc_loop",
    }
)


def _purge_nce_modules() -> None:
    for key in list(sys.modules):
        if key == "nce" or key.startswith("nce."):
            del sys.modules[key]


def _fresh_nce():
    _purge_nce_modules()
    return importlib.import_module("nce")


def test_bare_import_does_not_load_orchestrator(monkeypatch) -> None:
    _purge_nce_modules()

    import nce  # noqa: F401

    assert "nce.orchestrator" not in sys.modules
    assert "nce.garbage_collector" not in sys.modules


def test_all_public_names_accessible() -> None:
    nce = _fresh_nce()
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for name in nce.__all__:
            obj = getattr(nce, name)
            assert obj is not None


def test_all_contains_expected_exports() -> None:
    nce = _fresh_nce()

    assert set(nce.__all__) == set(_EXPECTED_ALL)
    assert len(nce.__all__) == 5


def test_unknown_attribute_raises() -> None:
    nce = _fresh_nce()

    with pytest.raises(AttributeError):
        _ = nce.this_does_not_exist


def test_version_is_non_empty_string() -> None:
    nce = _fresh_nce()

    assert isinstance(nce.__version__, str)
    assert nce.__version__.strip() != ""


def test_lazy_cache_avoids_repeated_getattr() -> None:
    nce = _fresh_nce()

    assert "NCEEngine" not in vars(nce)

    _ = nce.NCEEngine

    assert "NCEEngine" in vars(nce)


def test_broken_import_yields_attribute_error(monkeypatch) -> None:
    """Broken lazy imports surface as AttributeError with 'unavailable'."""

    real_import = builtins.__import__

    def blocking_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "nce.orchestrator":
            raise ImportError("simulated orchestrator import failure")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocking_import)

    real_import_module = importlib.import_module

    def blocking_import_module(name: str, package: str | None = None):
        # Lazy exports use importlib.import_module, which bypasses builtins.__import__.
        if name == "nce.orchestrator":
            blocking_import("nce.orchestrator")
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", blocking_import_module)

    nce = _fresh_nce()

    with pytest.raises(AttributeError, match="unavailable"):
        _ = nce.NCEEngine
