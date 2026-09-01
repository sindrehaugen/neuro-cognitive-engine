"""
tests/unit/test_mcp_tool_surface_ratchet.py
===========================================
The MCP tool surface has two halves that nothing tied together.

``nce/mcp_stdio_main.py``'s ``list_tools()`` returns ``mcp_stdio_tools.TOOLS``
**verbatim** -- that is what an MCP client can *discover*. ``call_tool()``
dispatches through ``tool_registry.TOOL_REGISTRY`` -- that is what a client can
actually *invoke*. Nothing checked that the two agree, so registering a tool
without writing a ``Tool(...)`` definition produced a tool that is callable by
name and invisible to every client that enumerates.

DL raised this as **OQ-3** on 2026-08-17 (112 registered vs 71 advertised) and
flagged it as *"if it is a bug, it outranks the entire documentation effort"*.
It was never answered, and it grew -- because nothing failed when it grew.

Measured 2026-08-31 against the private repo's ``origin/main@7e97efe`` and
re-measured on this repo at ``124b80b`` -- the numbers are IDENTICAL in both.
The honest split is
NOT the raw runtime difference:

    registered in TOOL_REGISTRY                                     135
    have a Tool(...) definition in mcp_stdio_tools.py                 92
      ...of which are behind a config flag (see below)                11
    NO definition anywhere -> undiscoverable under ANY config         43

**Why this file compares against the FILE, not against ``TOOLS``.**
``TOOLS`` is assembled conditionally at import time -- ``NCE_DISABLE_MIGRATION_MCP``,
``NCE_D365_ENABLED``, ``NCE_DIAG_ENABLED`` each splice in more tools. So the
runtime count of "hidden" tools depends on the environment the tests run in: a
first version of this ratchet hard-coded that runtime number and would have
failed the moment anyone enabled D365. The set of tools with a *definition* is
a property of the source, so the gate below is config-independent.

**This file is the gate, not the whole repair.** The repair is authoring a
description and an ``inputSchema`` per tool, which cannot be generated:
``ToolSpec`` carries only ``handler``, ``admin_only``, ``cacheable``,
``mutation`` and ``migration``, and ``mcp_stdio_tools.py`` is the only file in
the tree holding any ``inputSchema``. Auto-advertising 43 permissive schemas
would satisfy a naive gate while making clients *worse* off -- it invites
malformed calls instead of no calls.

Module 11's 14 ``inventory_*`` tools were authored on 2026-08-31 (the first
tranche, taken because that surface had just shipped and the FE is being
pointed at it), which is why the list below is 43 and not 57.

Same shape as ``TENANT_TABLES_WITHOUT_NAMESPACE_FK`` in
``tests/test_namespace_fk_cascade.py``: an allowlist plus a reverse assertion
records debt without blessing it.

Pure unit tests -- no database, no Redis.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from nce import mcp_stdio_tools
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import TOOL_REGISTRY

# ---------------------------------------------------------------------------
# Registered in TOOL_REGISTRY (so callable) with NO Tool(...) definition
# anywhere in mcp_stdio_tools.py, so undiscoverable under every configuration.
# Each needs a description + inputSchema authored by whoever owns the module.
#
# Grouped by module with counts so a diff to this list is legible.
# ---------------------------------------------------------------------------
TOOLS_WITH_NO_DEFINITION: frozenset[str] = frozenset(
    {
        # agreements (1)
        "agreements_lookup_terms",
        # assets (4)
        "assets_advance_lifecycle",
        "assets_get",
        "assets_list",
        "assets_ping",
        # d365 (1) -- its five siblings ARE defined, behind NCE_D365_ENABLED.
        # This one was simply missed, so the d365 block is short by one.
        "d365_sync_status",
        # cognitive / graph one-offs (2)
        "detect_causal_cycles",
        "resolve",
        # economy (3)
        "economy_compute_periodisering",
        "economy_emit_event",
        "economy_match_invoice",
        # merge queue (3)
        "merge_queue_confirm",
        "merge_queue_list",
        "merge_queue_reject",
        # pricing (1)
        "pricing_resolve",
        # procurement (6)
        "procurement_calculate_tco",
        "procurement_evaluate_match",
        "procurement_forecast_rebate",
        "procurement_rank_suppliers",
        "procurement_recommend_move_spend",
        "procurement_whatif_spend",
        # product (6)
        "product_enrich",
        "product_get",
        "product_match_bom_line",
        "product_price",
        "product_related",
        "product_search",
        # project (4)
        "project_advance_phase",
        "project_can_enter_phase",
        "project_convert_signed_quote",
        "project_suggest_pl",
        # sales (2)
        "sales_get_signed_baseline",
        "sales_ping",
        # vendors (10)
        "vendors_calibrate_weights",
        "vendors_check_tier_at_risk",
        "vendors_compute_performance",
        "vendors_compute_scorecard",
        "vendors_detect_reliability_degradation",
        "vendors_get_tier_status",
        "vendors_get_vendor",
        "vendors_match_contractor",
        "vendors_recall_similar_jobs",
        "vendors_reliability_radar",
    }
)

# Tools that DO have a definition but are spliced into TOOLS only when their
# feature flag is on, so whether they are advertised is an operator choice, not
# debt. They are exempt from both directions of the gate below -- asserting
# either way would make this file fail depending on the environment.
#   NCE_D365_ENABLED  -> the d365_* block
#   NCE_DIAG_ENABLED  -> the diag_* block plus evaluate_circuit_impact
# (NCE_DISABLE_MIGRATION_MCP gates _MIGRATION_TOOLS, which ToolSpec also marks
#  with migration=True, so the registry knows about that gate; these two do not
#  have an equivalent ToolSpec flag, which is itself worth a decision.)
TOOLS_DEFINED_BUT_CONFIG_GATED: frozenset[str] = frozenset(
    {
        "d365_case_stress_report",
        "d365_list_sla_breaches",
        "d365_netbox_mappings",
        "d365_query_case",
        "d365_sync_now",
        "diag_commit_bundle",
        "diag_device_health",
        "diag_digest_status",
        "diag_ingest_bundle",
        "diag_list_anomalies",
        "evaluate_circuit_impact",
    }
)


def _defined_in_file() -> set[str]:
    """Every ``Tool(name=...)`` literal in ``mcp_stdio_tools.py``.

    Parsed from source, so it does not depend on which feature flags happen to
    be set in the environment running the tests.
    """
    path = Path(inspect.getsourcefile(mcp_stdio_tools) or "")
    tree = ast.parse(path.read_bytes().decode("utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Tool":
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    names.add(kw.value.value)
    return names


def _advertised() -> set[str]:
    return {t.name for t in TOOLS}


def _registered() -> set[str]:
    return set(TOOL_REGISTRY)


# ---------------------------------------------------------------------------
# The gate -- config-independent
# ---------------------------------------------------------------------------


def test_no_tool_is_registered_without_a_definition_or_an_entry_here() -> None:
    """The assertion that stops the gap growing.

    This is what would have caught OQ-3 drifting from 41 to 68.
    """
    undiscoverable = sorted(_registered() - _defined_in_file() - TOOLS_WITH_NO_DEFINITION)
    assert not undiscoverable, (
        "these tools are in TOOL_REGISTRY (so call_tool can invoke them) but have "
        "no Tool(...) definition in nce/mcp_stdio_tools.py, so list_tools can "
        "never show them, and they are not listed as known debt: "
        + ", ".join(undiscoverable)
        + ". Add a Tool(name=..., description=..., inputSchema=...), or -- if it "
        "is deliberately internal -- add it to TOOLS_WITH_NO_DEFINITION with the "
        "reason."
    )


def test_a_listed_tool_that_now_has_a_definition_is_removed_from_the_list() -> None:
    """The reverse assertion: the list cannot rot into a permanent exemption."""
    repaired = sorted(TOOLS_WITH_NO_DEFINITION & _defined_in_file())
    assert not repaired, (
        "listed as having no definition, but a Tool(...) now exists -- drop from "
        "TOOLS_WITH_NO_DEFINITION: " + ", ".join(repaired)
    )


def test_neither_list_names_a_tool_that_no_longer_exists() -> None:
    """A stale name would quietly excuse a tool that was since renamed away."""
    for label, names in (
        ("TOOLS_WITH_NO_DEFINITION", TOOLS_WITH_NO_DEFINITION),
        ("TOOLS_DEFINED_BUT_CONFIG_GATED", TOOLS_DEFINED_BUT_CONFIG_GATED),
    ):
        ghosts = sorted(names - _registered())
        assert not ghosts, (
            f"{label} names tools absent from TOOL_REGISTRY -- drop them: " + ", ".join(ghosts)
        )


def test_the_two_lists_are_disjoint() -> None:
    """A tool is either undefined or defined-and-gated. Never both."""
    both = sorted(TOOLS_WITH_NO_DEFINITION & TOOLS_DEFINED_BUT_CONFIG_GATED)
    assert not both, "listed in both lists: " + ", ".join(both)


def test_nothing_defined_is_uncallable() -> None:
    """A discoverable tool that ``call_tool`` cannot dispatch.

    A ``Tool`` definition whose name is not in ``TOOL_REGISTRY`` means a client
    can see it, call it, and get ``UnknownToolError`` back.
    """
    phantom = sorted(_defined_in_file() - _registered())
    assert not phantom, (
        "defined in mcp_stdio_tools.py but absent from TOOL_REGISTRY, so calling "
        "them raises UnknownToolError: " + ", ".join(phantom)
    )


def test_everything_advertised_at_runtime_is_registered() -> None:
    """The runtime half of the check, in whatever config this run uses."""
    phantom = sorted(_advertised() - _registered())
    assert not phantom, "advertised but not registered: " + ", ".join(phantom)


def test_config_gated_tools_are_the_only_defined_ones_that_may_be_unadvertised() -> None:
    """In THIS run, a defined-but-unadvertised tool must be a known flag case."""
    unexplained = sorted(_defined_in_file() - _advertised() - TOOLS_DEFINED_BUT_CONFIG_GATED)
    assert not unexplained, (
        "these tools have a Tool(...) definition but are not in TOOLS in this "
        "configuration, and are not listed as flag-gated: "
        + ", ".join(unexplained)
        + ". Either splice them into TOOLS or add them to "
        "TOOLS_DEFINED_BUT_CONFIG_GATED with the flag that gates them."
    )


def test_advertised_names_are_unique() -> None:
    names = [t.name for t in TOOLS]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, "duplicate Tool definitions: " + ", ".join(dupes)


@pytest.mark.parametrize("tool", TOOLS, ids=[t.name for t in TOOLS])
def test_every_advertised_tool_carries_a_usable_contract(tool: object) -> None:
    """An advertised tool with no schema invites malformed calls.

    This is exactly why the 43 are not auto-advertised with a permissive
    schema: that would pass the gate above while harming clients.
    """
    name = getattr(tool, "name", None)
    description = getattr(tool, "description", None)
    schema = getattr(tool, "inputSchema", None)
    assert name, "a Tool with no name"
    assert description and description.strip(), f"{name}: empty description"
    assert isinstance(schema, dict), f"{name}: inputSchema is not a dict"
    assert schema.get("type") == "object", f"{name}: inputSchema.type must be 'object'"
    properties = schema.get("properties")
    assert isinstance(properties, dict) and properties, f"{name}: no properties"
    for required in schema.get("required", []):
        assert required in properties, f"{name}: required '{required}' is not a property"


# ---------------------------------------------------------------------------
# Module 11 -- the tranche authored on 2026-08-31
# ---------------------------------------------------------------------------


def test_every_registered_inventory_tool_is_advertised() -> None:
    """Module 11's MCP half must be discoverable, not merely callable.

    The refusal contract added for D38 (McpError(-32005) + data.reason) is only
    reachable by a client that can find the tool.
    """
    registered = {n for n in _registered() if n.startswith("inventory_")}
    missing = sorted(registered - _advertised())
    assert not missing, "registered inventory tools still hidden: " + ", ".join(missing)
    assert len(registered) == 14, sorted(registered)


def test_inventory_quantities_never_force_a_float() -> None:
    """``qty`` is NUMERIC(18,3); coercing money/stock through float is forbidden.

    The schema must accept a string so an exact decimal survives the wire.
    """
    by_name = {t.name: t for t in TOOLS}
    for name in (
        "inventory_transfer_stock",
        "inventory_record_consumption",
        "inventory_reserve_stock",
        "inventory_release_stock",
        "inventory_record_rma",
    ):
        schema = by_name[name].inputSchema
        qty = schema["properties"]["qty"]["type"]
        assert isinstance(qty, list) and "string" in qty, (name, qty)


def test_the_recorded_gap_matches_what_is_measured() -> None:
    """Pins the numbers this docstring, OQ-3 and any FE write-up quote.

    Config-independent: both sides are derived from the source, not from the
    conditionally-assembled ``TOOLS``. Treat an INCREASE in the first number as
    the gate above having been bypassed.
    """
    assert len(TOOLS_WITH_NO_DEFINITION) == 43
    assert len(_registered()) == 135
    assert len(_defined_in_file()) == 92
    assert len(_registered()) == len(_defined_in_file()) + len(TOOLS_WITH_NO_DEFINITION)
