"""
tests/unit/test_system_design_toolcount.py
==========================================
Plain unit test for Module 6 Phase-1a — tool-count regression guard.

Pins the live ``system_design_*`` entries in ``TOOL_REGISTRY`` to the exact
expected set (W1 ping + W11 lucid-export + W13a get-topology + the two W13b
authoring tools + the W13c design-graph validator + the W17 retire tool).  Fails
loudly when:
  - a tool is silently added or dropped,
  - a tool's ``cacheable`` / ``admin_only`` / ``mutation`` flag changes,
  - the ``system_design_*`` tools ADVERTISED over MCP ``tools/list``
    (``nce.mcp_stdio_tools.TOOLS``) drift apart from the ones REGISTERED in
    ``TOOL_REGISTRY``, in EITHER direction.  This module shipped that exact
    divergence: two registered tools were dispatchable but never advertised.

No DB, no Redis, no HTTP — pure import-time assertion.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Expected tool set (W1 ping + W11 lucid-export + W13a get-topology + the two
# W13b authoring tools + the W13c design-graph validator + the W17 retire tool).
# The flow cores (propose/from_quote/sow/enrichment/to_quote/validate) are
# NOT registered as MCP tools — they are invoked through the flow.
# ---------------------------------------------------------------------------

_EXPECTED_TOOLS: dict[str, dict[str, bool]] = {
    "system_design_ping": {
        "cacheable": True,
        "admin_only": False,
        "mutation": False,
    },
    "system_design_publish_design_docs": {
        "cacheable": False,
        "admin_only": False,
        "mutation": True,
    },
    # M6.W13a — read-only topology surface; the first row of Copper's contract
    # table.  These three flags ARE the contract: never adjust them to make a
    # failing test pass.
    "system_design_get_topology": {
        "cacheable": True,
        "admin_only": False,
        "mutation": False,
    },
    # M6.W13b — the authoring surface, rows two and three of Copper's contract
    # table.  ``mutation=True`` is not decoration: it is what makes the dispatch
    # loop bump the MCP cache generation, and that bump is the only thing that
    # keeps the cacheable ``system_design_get_topology`` entry above from
    # serving pre-write data for the full MCP_CACHE_TTL_S.  Flipping it to False
    # to quiet something is a silent stale-read bug.
    "system_design_author_topology": {
        "cacheable": False,
        "admin_only": False,
        "mutation": True,
    },
    "system_design_author_functional_location": {
        "cacheable": False,
        "admin_only": False,
        "mutation": True,
    },
    # M6.W13c — the design-graph validator, row four of Copper's contract table.
    # ``cacheable=False`` next to ``mutation=False`` is neither a contradiction
    # nor a copy-paste slip: it IS a read, but a design under active canvas
    # editing must never be served a stale verdict, and no write sits on this
    # path whose cache-generation bump would refresh one.  Flipping it to True
    # to win a cache hit is a silent stale-verdict bug.
    "system_design_validate_design_graph": {
        "cacheable": False,
        "admin_only": False,
        "mutation": False,
    },
    # M6.W17 — retire planned nodes, and the codebase's FIRST delete path.
    # ``admin_only=True`` is the flag that separates this row from the two
    # authoring rows above, and it is the contract rather than a preference:
    # those add and update, this is the only tool in the module that can take
    # something away. Flipping it to False to let a tenant key call it hands
    # every Copper caller a delete.
    #
    # ``mutation=True`` for the same cache reason as the authoring tools, and
    # more sharply: without the generation bump the cacheable
    # ``system_design_get_topology`` entry above keeps serving a device the
    # caller just removed for the full MCP_CACHE_TTL_S.
    #
    # The NAME is a deliberate mismatch with the behaviour — the default is a
    # soft retire and nothing is removed without ``permanent=true`` — and the
    # name is pinned by Copper's contract, so this row must never be "fixed"
    # by renaming the tool to match what it does.
    "system_design_delete_planned": {
        "cacheable": False,
        "admin_only": True,
        "mutation": True,
    },
    # M6.W26 (Batch 230a) -- the COMMERCIAL half of the design loop. Four cores
    # that had no route and no tool. Flags come from each core's call graph:
    #   from_quote           _upsert_edge + do_author_functional_location +
    #                        emit_graph_write            -> mutation=True
    #   to_quote             _upsert_edge                -> mutation=True
    #   enrich_design_lines  _fire_product_enrichment -> enqueue_product_enrichment
    #                        writes no graph row but QUEUES work -> mutation=True
    #   generate_sow         only _read_* helpers         -> mutation=False
    # cacheable=False on all four for validate_design_graph's stated reason: a
    # design under active canvas editing must not be served a stale answer.
    "system_design_from_quote": {
        "cacheable": False,
        "admin_only": False,
        "mutation": True,
    },
    "system_design_to_quote": {
        "cacheable": False,
        "admin_only": False,
        "mutation": True,
    },
    "system_design_generate_sow": {
        "cacheable": False,
        "admin_only": False,
        "mutation": False,
    },
    "system_design_enrich_design_lines": {
        "cacheable": False,
        "admin_only": False,
        "mutation": True,
    },
    # M6.W27 (Batch 230a2) -- propose-only, so mutation=False. Exposed separately
    # because this core already has two internal callers.
    "system_design_propose_design": {
        "cacheable": False,
        "admin_only": False,
        "mutation": False,
    },
}


def _get_system_design_tools() -> dict[str, object]:
    """Return only the system_design_* entries from the live TOOL_REGISTRY."""
    from nce.tool_registry import TOOL_REGISTRY

    return {k: v for k, v in TOOL_REGISTRY.items() if k.startswith("system_design_")}


# ---------------------------------------------------------------------------
# 1. Exact count
# ---------------------------------------------------------------------------


def test_system_design_tool_count_is_exact() -> None:
    """Exactly the expected system_design_* tools must be registered."""
    live = _get_system_design_tools()
    assert len(live) == len(_EXPECTED_TOOLS), (
        f"Expected {len(_EXPECTED_TOOLS)} system_design_* tools, got {len(live)}.\n"
        f"Live set: {sorted(live)}\n"
        f"Expected: {sorted(_EXPECTED_TOOLS)}"
    )


# ---------------------------------------------------------------------------
# 2. Exact name set
# ---------------------------------------------------------------------------


def test_system_design_tool_names_are_exact() -> None:
    """The system_design_* tool names must match the expected set exactly."""
    live = _get_system_design_tools()
    assert set(live) == set(_EXPECTED_TOOLS), (
        f"Tool name mismatch.\n"
        f"Extra tools:   {sorted(set(live) - set(_EXPECTED_TOOLS))}\n"
        f"Missing tools: {sorted(set(_EXPECTED_TOOLS) - set(live))}"
    )


# ---------------------------------------------------------------------------
# 3. Per-tool flag assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name,expected_flags", list(_EXPECTED_TOOLS.items()))
def test_system_design_tool_flags(tool_name: str, expected_flags: dict[str, bool]) -> None:
    """Each system_design_* tool must carry the exact cacheable/admin_only/mutation flags."""
    from nce.tool_registry import TOOL_REGISTRY

    assert tool_name in TOOL_REGISTRY, (
        f"Tool '{tool_name}' not found in TOOL_REGISTRY.\nLive keys: {sorted(TOOL_REGISTRY)}"
    )
    spec = TOOL_REGISTRY[tool_name]
    for flag, expected_value in expected_flags.items():
        actual_value = getattr(spec, flag)
        assert actual_value == expected_value, (
            f"Tool '{tool_name}': expected {flag}={expected_value!r}, got {actual_value!r}"
        )


# ---------------------------------------------------------------------------
# 4. Advertised-vs-registered parity - asserted in BOTH directions
#
# ``TOOL_REGISTRY`` is what the dispatcher will execute; ``mcp_stdio_tools.TOOLS``
# is what an MCP client is told exists.  A tool present in one and absent from
# the other is a real defect either way round, and the two failures are NOT the
# same bug:
#   registry - advertised  =>  dispatchable but invisible (no client can ever
#                              discover it; this module shipped exactly that,
#                              for ``ping`` and ``publish_design_docs``).
#   advertised - registry  =>  advertised but undispatchable (a client calls it
#                              and gets "unknown tool").
# Asserting only one direction is half a gate, so each direction gets its own
# test and therefore its own named failure.
# ---------------------------------------------------------------------------


def _get_advertised_system_design_tool_names() -> set[str]:
    """Return the ``system_design_*`` names advertised over MCP ``tools/list``."""
    from nce.mcp_stdio_tools import TOOLS

    return {t.name for t in TOOLS if t.name.startswith("system_design_")}


def test_system_design_no_registered_tool_is_unadvertised() -> None:
    """Every registered ``system_design_*`` tool must also be advertised."""
    registered = set(_get_system_design_tools())
    advertised = _get_advertised_system_design_tool_names()
    missing = sorted(registered - advertised)
    assert not missing, (
        "system_design_* tools are in TOOL_REGISTRY but NOT advertised in "
        "nce.mcp_stdio_tools.TOOLS - dispatchable, but no MCP client can "
        f"discover them: {missing} | registered={sorted(registered)} | "
        f"advertised={sorted(advertised)}"
    )


def test_system_design_no_advertised_tool_is_unregistered() -> None:
    """Every advertised ``system_design_*`` tool must also be registered."""
    registered = set(_get_system_design_tools())
    advertised = _get_advertised_system_design_tool_names()
    extra = sorted(advertised - registered)
    assert not extra, (
        "system_design_* tools are advertised in nce.mcp_stdio_tools.TOOLS but "
        "NOT in TOOL_REGISTRY - a client that calls one gets 'unknown tool': "
        f"{extra} | registered={sorted(registered)} | "
        f"advertised={sorted(advertised)}"
    )


def test_system_design_advertised_set_equals_registered_set() -> None:
    """Positive control: with both sides correct the two sets are equal and non-empty.

    Guards the two directional tests above against vacuity.  If the
    ``system_design_`` prefix filter ever selected nothing - a rename, a moved
    module, a TOOLS list that failed to build - both directional tests would
    pass trivially on two empty sets.
    """
    registered = set(_get_system_design_tools())
    advertised = _get_advertised_system_design_tool_names()
    assert advertised, "no system_design_* tools advertised at all - the filter is vacuous"
    assert registered == set(_EXPECTED_TOOLS), (
        f"registry drifted from the expected set: {sorted(registered)}"
    )
    assert advertised == registered, (
        f"advertised-only={sorted(advertised - registered)} | "
        f"registered-only={sorted(registered - advertised)}"
    )
