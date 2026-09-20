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

This generator has no dedicated ``--check`` mode of its own, but
``tests/test_docs_engine_guides_ratchet.py`` (Lane D, DL-Orch D-3, predates
this fix) already re-derives its expected output and diffs it against the
committed ``docs/_generated/surface.md`` -- the actual CI gate on this file.
This test suite exercises the extraction functions directly against live
registry state, including a second real bug found while fixing the first:
the initial version of this fix hardcoded "13 routes per ResourceSpec" from a
single read of ``rest.py`` on a commit that predated Wave A-4, which added 3
more (generic document-attachment) routes to every spec, not just
``documents`` itself -- undercounting was caught only by re-reading the
current route list rather than trusting the earlier count. The route-count
assertions below derive the per-spec count from the real
``make_resource_routes`` function instead of a literal, so a third such drift
cannot happen silently again.
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
    generator's simulation must find exactly that many.

    NOT a uniform len(specs) * 4 since Wave E-19's ``excluded_verbs`` field:
    a spec may omit a verb (e.g. RESOURCE/FUNCTIONAL_LOCATION exclude "list"
    to avoid a hand-written tool-name collision -- see
    ``nce/resource_surface/exemptions.py``), so the expected total is summed
    per spec from each spec's own ``excluded_verbs``, not multiplied
    uniformly. A hardcoded ``* 4`` here would repeat the exact class of drift
    this fix was written to catch, just for a newer field."""
    resource_surface_tools = [
        t for t in tools if t["resolved_module"] == "nce.resource_surface.mcp"
    ]
    expected = sum(4 - len(s.excluded_verbs) for s in registered_specs)
    assert len(resource_surface_tools) == expected


def test_every_registered_spec_contributes_the_real_route_count(registered_specs, routes):
    """Cross-check against the real function, not a hardcoded literal: each
    spec's own route count is read fresh from make_resource_routes itself (16
    as of Wave A-4, was 13 before A-4 added generic document-attachment
    routes to every spec; a spec with excluded_verbs, Wave E-19, has fewer).

    NOT ``len(specs) * routes_per_spec(specs[0])`` -- since Wave E-19, specs
    can genuinely differ in their own route count, so specs[0] is no longer
    representative of every spec. Summed per spec instead. A hardcoded/
    uniform count here would repeat the exact class of drift this fix was
    written to catch, just for a newer field."""
    from nce.resource_surface.rest import make_resource_routes

    expected = sum(len(make_resource_routes(s)) for s in registered_specs)
    resource_surface_routes = [
        r for r in routes if r["resolved_mod"] == "nce.resource_surface.rest"
    ]
    assert len(resource_surface_routes) == expected


def test_derived_route_total_matches_live_registry(registered_specs, routes):
    """Cross-check against the real object graph: derived total must equal
    build_all_resource_routes()'s live length, the same treatment already
    given to the tool count below."""
    from nce.resource_surface import build_all_resource_routes

    resource_surface_routes = [
        r for r in routes if r["resolved_mod"] == "nce.resource_surface.rest"
    ]
    assert len(resource_surface_routes) == len(build_all_resource_routes())


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
