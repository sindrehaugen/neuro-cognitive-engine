"""Surface-parity ratchet — core business logic reachability from external surfaces.

Every ``do_*`` core under ``nce/vertical_modules/**`` must either be reachable from a
production surface (MCP tools, HTTP routes, scheduled cron jobs, background watchers,
event subscribers, database/domain triggers, webhooks, or A2A skills) or be catalogued
in ``nce/config_data/internal-cores.json`` with a reviewer-verifiable owner and reason.

The allowlist is shrink-only: when a future wave wires an unreached core to a surface,
the test fails until the allowlist entry is removed.

Copying the design of ``tests/test_producer_coverage.py``, this census operates via
pure AST inspection (covering both ``FunctionDef`` and ``AsyncFunctionDef``) to prevent
silent omissions from grep-based or sync-only scanning.

**Unobserved surfaces (audited per Phase 4 Wave T-5 / Charter §13 Question 3):**
1. *Dynamic dispatch*: Core invocations performed via ``getattr(module, 'do_' + name)`` or
   reflection without an explicit AST call node.
2. *Non-do_* helpers*: Internal helper functions that do not follow the ``do_*`` or
   ``async def do_*`` naming convention are outside this census.
3. *Dead branches in reached cores*: Function-level AST reachability proves entry points
   can reach a core, but does not assert line-level execution of inner branches.
"""

from __future__ import annotations

import ast
import json
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_NCE_DIR = _REPO_ROOT / "nce"
_VERT_DIR = _NCE_DIR / "vertical_modules"
_ALLOWLIST_PATH = _NCE_DIR / "config_data" / "internal-cores.json"

_SURFACE_ROOT_PATTERNS = frozenset(
    {
        "tool_registry.py",
        "mcp_stdio_tools.py",
        "mcp_handlers.py",
        "admin_app.py",
        "admin_handlers/",
        "cron.py",
        "webhook_receiver/",
        "a2a.py",
        "a2a_server.py",
        "watchers.py",
        "triggers.py",
        "subscribers.py",
    }
)


def _load_allowlist() -> dict[str, dict[str, Any]]:
    """Load the shrink-only internal-cores allowlist."""
    assert _ALLOWLIST_PATH.exists(), f"Missing allowlist at {_ALLOWLIST_PATH}"
    with open(_ALLOWLIST_PATH, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def _collect_vertical_cores() -> dict[str, tuple[str, str]]:
    """Map ``f"{rel_path}::{func_name}" -> (rel_path, func_name)`` for all vertical cores."""
    cores: dict[str, tuple[str, str]] = {}
    for p in sorted(_VERT_DIR.glob("**/*.py")):
        rel = p.relative_to(_REPO_ROOT).as_posix()
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("do_"):
                key = f"{rel}::{n.name}"
                cores[key] = (rel, n.name)
    return cores


@lru_cache(maxsize=1)
def _build_reachability_graph() -> tuple[set[str], set[str], dict[str, set[str]]]:
    """Perform AST reachability walk starting from all surface roots under ``nce/``.

    Returns:
        (surface_roots, reachable_do_funcs, call_graph)
    """
    call_graph: dict[str, set[str]] = defaultdict(set)
    file_level_calls: dict[str, set[str]] = defaultdict(set)

    for p in sorted(_NCE_DIR.glob("**/*.py")):
        rel = p.relative_to(_REPO_ROOT).as_posix()
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.scope = "<module>"

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                old = self.scope
                self.scope = node.name
                self.generic_visit(node)
                self.scope = old

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                old = self.scope
                self.scope = node.name
                self.generic_visit(node)
                self.scope = old

            def visit_Call(self, node: ast.Call) -> None:
                target = None
                if isinstance(node.func, ast.Name):
                    target = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    target = node.func.attr
                if target:
                    call_graph[self.scope].add(target)
                    file_level_calls[rel].add(target)
                self.generic_visit(node)

        Visitor().visit(tree)

    surface_roots = {
        rel for rel in file_level_calls if any(pat in rel for pat in _SURFACE_ROOT_PATTERNS)
    }

    reachable_do_funcs: set[str] = set()
    frontier: deque[str] = deque()

    for s_rel in surface_roots:
        for called in file_level_calls[s_rel]:
            if called.startswith("do_"):
                reachable_do_funcs.add(called)
                frontier.append(called)

    visited = set(reachable_do_funcs)
    while frontier:
        curr = frontier.popleft()
        for called in call_graph.get(curr, set()):
            if called.startswith("do_") and called not in visited:
                visited.add(called)
                reachable_do_funcs.add(called)
                frontier.append(called)

    return surface_roots, reachable_do_funcs, call_graph


def _unreached_cores() -> set[str]:
    """Return keys of vertical cores not reached by any surface."""
    all_cores = _collect_vertical_cores()
    _, reachable_funcs, _ = _build_reachability_graph()
    return {key for key, (_, name) in all_cores.items() if name not in reachable_funcs}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_surface_reachability_discovery_floor() -> None:
    """Guard the guard: ensure AST analysis discovers surfaces and cores.

    A broken walk that declaration-fails would vacuously declare 0 or 100% reachability.
    Floors ensure the scanner discovers expected magnitudes across the estate.
    """
    cores = _collect_vertical_cores()
    assert len(cores) >= 200, (
        f"Discovery collapse: found only {len(cores)} vertical cores under nce/vertical_modules/"
    )

    surface_roots, reachable, _ = _build_reachability_graph()
    assert len(surface_roots) >= 20, (
        f"Discovery collapse: found only {len(surface_roots)} surface root files"
    )
    reachable_defs = [k for k, (_, name) in cores.items() if name in reachable]
    assert len(reachable_defs) >= 150, (
        f"Discovery collapse: reachability walk resolved only {len(reachable_defs)} reachable "
        f"core definitions (floor is 150, found {len(reachable_defs)})"
    )
    assert len(reachable) >= 140, (
        f"Discovery collapse: reachability walk resolved only {len(reachable)} distinct "
        f"reachable core names (floor is 140, found {len(reachable)})"
    )

    # Positive control: Known reached cores must be in reachable set
    assert "do_match_invoice" in reachable, (
        "Positive control failed: economy's 'do_match_invoice' is reached via mcp_handlers "
        "and admin_handlers, but AST walk did not find it."
    )
    assert "do_list_customers" in reachable, (
        "Positive control failed: sales' 'do_list_customers' is reached via admin_handlers, "
        "but AST walk did not find it."
    )


def test_every_do_core_is_reachable_or_allowlisted() -> None:
    """Every vertical core must be reachable from a surface or explicitly allowlisted."""
    unreached = _unreached_cores()
    allowlist = _load_allowlist()

    unexpected = unreached - set(allowlist.keys())
    assert not unexpected, (
        f"Found {len(unexpected)} vertical do_* cores with NO surface and NO allowlist entry:\n"
        + "\n".join(f"  - {k}" for k in sorted(unexpected))
        + "\nEither wire a surface (tool/route/cron/subscriber/watcher/trigger/webhook/a2a) "
        "or add to nce/config_data/internal-cores.json with an owner and a reasoned justification."
    )


def test_internal_cores_allowlist_is_shrink_only_and_reasoned() -> None:
    """Every entry in internal-cores.json must exist in code, be unreached, and have valid reasons."""
    all_cores = _collect_vertical_cores()
    unreached = _unreached_cores()
    allowlist = _load_allowlist()

    # 1. No stale entries that no longer exist in code
    stale = set(allowlist.keys()) - set(all_cores.keys())
    assert not stale, (
        "Allowlist references deleted or renamed cores that no longer exist:\n"
        + "\n".join(f"  - {k}" for k in sorted(stale))
        + "\nRemove these stale entries from nce/config_data/internal-cores.json."
    )

    # 2. Shrink-only: any core that gained a surface must be removed from allowlist
    now_reached = set(allowlist.keys()) - unreached
    assert not now_reached, (
        "Shrink-only violation! Cores gained a surface and must be removed from internal-cores.json:\n"
        + "\n".join(f"  - {k}" for k in sorted(now_reached))
    )

    # 3. Every entry must be reasoned and have an owner
    for key, entry in allowlist.items():
        assert isinstance(entry, dict), f"{key}: entry must be a dictionary"
        assert "owner" in entry and entry["owner"].strip(), f"{key}: missing or empty 'owner'"
        assert "engine" in entry and entry["engine"].strip(), f"{key}: missing or empty 'engine'"
        assert "reason" in entry, f"{key}: missing 'reason'"

        reason = " ".join(entry["reason"].split())
        assert len(reason) >= 60, (
            f"{key}: reason too short for review ({len(reason)} chars < 60 min): '{reason}'"
        )
        # Check that reason isn't just repeating the function name
        func_name = key.split("::")[-1]
        assert reason.replace(func_name, "").strip(), (
            f"{key}: reason merely repeats the function name"
        )


def test_positive_control_fails_on_unregistered_core() -> None:
    """Standing positive control (U18): prove the ratchet fails when an offender is present."""
    unreached = set(_unreached_cores())
    allowlist = _load_allowlist()

    # Synthetic unreached core not in allowlist
    synthetic_unreached = unreached | {"nce/vertical_modules/fake/core.py::do_synthetic_fake"}
    unexpected = synthetic_unreached - set(allowlist.keys())
    assert unexpected == {"nce/vertical_modules/fake/core.py::do_synthetic_fake"}

    # Synthetic premature removal from allowlist
    if unreached:
        sample_key = next(iter(unreached))
        shrunk_allowlist = {k: v for k, v in allowlist.items() if k != sample_key}
        unexpected_sample = unreached - set(shrunk_allowlist.keys())
        assert sample_key in unexpected_sample
