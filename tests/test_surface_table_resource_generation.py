"""Regression guard for a real drift bug found in Lane H janitor pass 6 (K-H3).

``scripts/gen_surface_table.py`` renders ``docs/_generated/surface.md``, the
per-engine table of every MCP tool and REST route. Its ``extract_tools`` and
``extract_routes`` walked only the hand-written registries -- the
``TOOL_REGISTRY`` dict literal in ``nce/tool_registry.py`` and the ``Route(...)``
literals in ``nce/admin_app.py::build_admin_routes()``. Neither AST walk saw
the C12 resource-surface additions: ``TOOL_REGISTRY.update(build_all_resource_
tool_specs())`` and ``*build_all_resource_routes()`` are runtime calls, not
literal dict/list elements, so every registered ``ResourceSpec`` (Inventory,
Notifications, PO_LINE) was invisible to this generator. The ``notifications``
row of the committed doc read ``-`` / ``-`` / ``-`` despite having two real,
routed, tool-advertised resources.

``gen_engine_figures.py`` already carried an equivalent simulation block for
tool *counts* (added after Wave A-1, predating this fix) and was unaffected.
``gen_surface_table.py`` had no such block for tools and none at all for
routes -- this file locks in the fix so the per-engine table cannot regress
back to silently dropping C12 rows.

This generator has no ``--check`` mode (unlike the other three of Lane H's
four generated docs), so there is no CI gate on its output short of this test
suite exercising its extraction functions directly against live registry
state.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

os.environ.setdefault("NCE_MASTER_KEY", "x" * 32)

import pytest

from nce.resource_surface import get_all_resource_specs, load_all_engine_resources

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "gen_surface_table",
    Path(__file__).resolve().parents[1] / "scripts" / "gen_surface_table.py",
)
gen_surface_table = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_surface_table)

_REPO_ROOT = str(Path(__file__).resolve().parents[1])


@pytest.fixture(scope="module")
def registered_specs():
    load_all_engine_resources()
    specs = get_all_resource_specs()
    assert specs, (
        "Positive-control precondition failed: no ResourceSpec is registered. "
        "Every assertion below would be vacuously true against an empty set."
    )
    return specs


@pytest.fixture(scope="module")
def tools():
    gen_surface_table.VERTICAL_ENGINES = gen_surface_table.discover_vertical_engines(
        _REPO_ROOT, "HEAD"
    )
    return gen_surface_table.extract_tools(_REPO_ROOT, "HEAD")


@pytest.fixture(scope="module")
def routes():
    gen_surface_table.VERTICAL_ENGINES = gen_surface_table.discover_vertical_engines(
        _REPO_ROOT, "HEAD"
    )
    return gen_surface_table.extract_routes(_REPO_ROOT, "HEAD")


def test_every_registered_spec_contributes_exactly_four_tools(registered_specs, tools):
    """Each ResourceSpec mounts 4 MCP tools (list/get/upsert/archive) --
    see nce/resource_surface/mcp.py::build_mcp_tool_definitions. The
    generator's simulation must find exactly len(specs) * 4 of them."""
    resource_surface_tools = [
        t for t in tools if t["resolved_module"] == "nce.resource_surface.mcp"
    ]
    assert len(resource_surface_tools) == len(registered_specs) * 4


def test_every_registered_spec_contributes_exactly_thirteen_routes(registered_specs, routes):
    """Each ResourceSpec mounts 13 REST routes -- see
    nce/resource_surface/rest.py::make_resource_routes. The generator's
    simulation must find exactly len(specs) * 13 of them."""
    resource_surface_routes = [
        r for r in routes if r["resolved_mod"] == "nce.resource_surface.rest"
    ]
    assert len(resource_surface_routes) == len(registered_specs) * 13


def test_notifications_row_is_no_longer_silently_empty(tools, routes):
    """The exact regression found in janitor pass 6: notifications has two
    registered ResourceSpecs (notifications, reminders) but the committed
    doc's row read '-' / '-' / '-' before this fix."""
    notif_tools = [t for t in tools if t["engine"] == "notifications"]
    notif_routes = [r for r in routes if r["engine"] == "notifications"]
    assert notif_tools, "notifications must have generated tool rows, not '-'"
    assert notif_routes, "notifications must have generated route rows, not '-'"


def test_derived_tool_total_matches_live_registry(registered_specs, tools):
    """Cross-check against the real object graph, not just this file's own
    simulation: derived total must equal len(TOOL_REGISTRY) at runtime."""
    from nce.tool_registry import TOOL_REGISTRY

    assert len(tools) == len(TOOL_REGISTRY)
