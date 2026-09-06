"""
tests/unit/test_engine_registry.py
==================================
Unit and acceptance tests for Wave I-0 (W-1: Engine Registry).

Gates per MLv1.5-A Charter §6:
1. Asks registry for an engine disabled for a namespace -> asserts documented refusal (EngineDisabledError).
2. Asks registry for an engine that does not exist -> asserts documented refusal (EngineNotFoundError).
3. Both relay processes (mcp_stdio_main.py and cron.py) populate engine.modules.
4. engine.modules is non-empty at boot in an integration test.
5. Standing positive-control test proving that disabling an engine makes it go RED and re-enabling makes it GREEN.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

from nce.engine_registry import (
    VERTICAL_MODULE_NAMES,
    EngineDisabledError,
    EngineNotFoundError,
    EngineRegistry,
    EngineUnavailableError,
    populate_engine_modules,
)
from nce.orchestrator import NCEEngine

_STDIO = pathlib.Path("nce/mcp_stdio_main.py")
_CRON = pathlib.Path("nce/cron.py")


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_bytes().decode("utf-8"))


def _call_lines(scope: ast.AST, name: str) -> list[int]:
    """Line numbers of real *call expressions* to *name*."""
    return [
        n.lineno
        for n in ast.walk(scope)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)
    ]


# ---------------------------------------------------------------------------
# Gate 1: Disabled engine refusal
# ---------------------------------------------------------------------------


def test_engine_disabled_for_namespace_raises_refusal() -> None:
    """An engine disabled for a namespace must not appear in membership checks

    and must raise the documented refusal (EngineDisabledError / EngineUnavailableError).
    """
    engine = SimpleNamespace()
    registry = populate_engine_modules(engine)
    ns = "00000000-0000-4000-8000-000000000001"

    # Initially enabled
    scoped = registry.for_namespace(ns)
    assert "support" in scoped
    assert callable(scoped["support"].do_open_ticket)

    # Disable support for namespace
    registry.disable_for_namespace(ns, "support")
    scoped_disabled = registry.for_namespace(ns)

    # Must NOT appear in mapping
    assert "support" not in scoped_disabled
    assert "support" not in [name for name in scoped_disabled]

    # Subscript access raises documented refusal
    with pytest.raises(EngineDisabledError) as exc_info:
        _ = scoped_disabled["support"]
    assert "disabled for namespace" in str(exc_info.value)
    assert issubclass(EngineDisabledError, EngineUnavailableError)
    assert issubclass(EngineDisabledError, KeyError)

    # get() accessor also raises documented refusal
    with pytest.raises(EngineDisabledError):
        scoped_disabled.get("support")

    # get() with default returns default
    assert scoped_disabled.get("support", default=None) is None

    # Global registry with namespace_id parameter also enforces refusal
    with pytest.raises(EngineDisabledError):
        registry.get("support", namespace_id=ns)

    with pytest.raises(EngineDisabledError):
        _ = registry["support", ns]


# ---------------------------------------------------------------------------
# Gate 2: Non-existent engine refusal
# ---------------------------------------------------------------------------


def test_engine_nonexistent_raises_refusal() -> None:
    """An engine that does not exist raises EngineNotFoundError (EngineUnavailableError)."""
    engine = SimpleNamespace()
    registry = populate_engine_modules(engine)

    assert "nonexistent_vertical" not in registry

    with pytest.raises(EngineNotFoundError) as exc_info:
        _ = registry["nonexistent_vertical"]
    assert "is not registered" in str(exc_info.value)
    assert issubclass(EngineNotFoundError, EngineUnavailableError)
    assert issubclass(EngineNotFoundError, KeyError)

    # In scoped registry
    scoped = registry.for_namespace("00000000-0000-4000-8000-000000000001")
    assert "nonexistent_vertical" not in scoped

    with pytest.raises(EngineNotFoundError):
        _ = scoped["nonexistent_vertical"]

    with pytest.raises(EngineNotFoundError):
        scoped.get("nonexistent_vertical")


# ---------------------------------------------------------------------------
# Gate 3: Both relay processes populate engine.modules
# ---------------------------------------------------------------------------


def test_both_relay_processes_populate_engine_modules() -> None:
    """Both relay-running processes (mcp_stdio_main.py and cron.py) must call

    populate_engine_modules to prevent cross-process drift (M0.W20d lesson).
    """
    stdio_tree = _parse(_STDIO)
    stdio_hits = _call_lines(stdio_tree, "populate_engine_modules")
    assert stdio_hits, "nce/mcp_stdio_main.py never calls populate_engine_modules()"

    cron_tree = _parse(_CRON)
    cron_hits = _call_lines(cron_tree, "populate_engine_modules")
    assert cron_hits, "nce/cron.py never calls populate_engine_modules()"


def test_populate_engine_modules_attaches_to_relay_duck() -> None:
    """populate_engine_modules attaches an EngineRegistry to SimpleNamespace duck."""
    duck = SimpleNamespace(pg_pool=None)
    reg = populate_engine_modules(duck)
    assert hasattr(duck, "modules")
    assert duck.modules is reg
    assert len(duck.modules) >= len(VERTICAL_MODULE_NAMES)


# ---------------------------------------------------------------------------
# Gate 4: engine.modules is non-empty at boot in NCEEngine
# ---------------------------------------------------------------------------


def test_engine_modules_non_empty_at_boot() -> None:
    """NCEEngine initializes engine.modules at boot with all vertical modules."""
    engine = NCEEngine()
    assert hasattr(engine, "modules")
    assert isinstance(engine.modules, EngineRegistry)
    assert len(engine.modules) >= 20

    for name in VERTICAL_MODULE_NAMES:
        assert name in engine.modules, f"Missing registered vertical module: {name}"
        mod = engine.modules[name]
        assert mod is not None

    # Verify cross-engine access works directly on known cores
    assert hasattr(engine.modules["sales"], "do_freeze_baseline")
    assert hasattr(engine.modules["support"], "do_open_ticket")
    assert hasattr(engine.modules["inventory"], "do_record_goods_receipt")
    assert hasattr(engine.modules["economy"], "do_cascade_on_approval")


# ---------------------------------------------------------------------------
# Standing Positive Control: Guard the Guard (U18)
# ---------------------------------------------------------------------------


def test_positive_control_toggle_moves_gate() -> None:
    """Standing positive-control test (U18):

    Prove the opt-in guard can fail and pass under controlled manipulation.
    Toggling an engine from enabled -> disabled -> enabled must flip access
    from success -> EngineDisabledError -> success.
    """
    engine = SimpleNamespace()
    registry = populate_engine_modules(engine)
    ns = "00000000-0000-4000-8000-000000000042"

    # Step 1: Default state is enabled
    scoped1 = registry.for_namespace(ns)
    assert "economy" in scoped1
    assert callable(scoped1["economy"].do_cascade_on_approval)

    # Step 2: Disable economy -> must raise EngineDisabledError
    registry.disable_for_namespace(ns, "economy")
    scoped2 = registry.for_namespace(ns)
    assert "economy" not in scoped2
    with pytest.raises(EngineDisabledError):
        _ = scoped2["economy"]

    # Step 3: Re-enable economy -> must succeed again
    registry.enable_for_namespace(ns, "economy")
    scoped3 = registry.for_namespace(ns)
    assert "economy" in scoped3
    assert callable(scoped3["economy"].do_cascade_on_approval)


def test_scoped_registry_length_and_iteration() -> None:
    """len(scoped) and list(scoped) accurately reflect only enabled modules."""
    engine = SimpleNamespace()
    registry = populate_engine_modules(engine)
    ns = "00000000-0000-4000-8000-000000000099"

    total = len(registry)
    registry.disable_for_namespace(ns, "support", "sales")
    scoped = registry.for_namespace(ns)

    assert len(scoped) == total - 2
    active_set = set(scoped)
    assert "support" not in active_set
    assert "sales" not in active_set
    assert "inventory" in active_set
