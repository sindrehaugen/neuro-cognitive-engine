"""
tests/unit/test_business_insights_composed_slices_ratchet.py
============================================================
Composition Ratchet Test Suite for Module 16 (Business Insights Engine).

Wave BI-2 (Charter Section 13 & Spec Section 3.4 / 6):
Every slice the morning brief, the board pack, and the risk radar compose
resolves to a registered read -- a cacheable tool or a catalogued event --
never a per-engine query negotiated at call time.

Ratchet Contract:
1. Producer Registry: Every slice in COMPOSED_SLICES MUST exist in TOOL_REGISTRY
   with mutation=False (read-only) and a callable handler.
2. Positive Control (U18): Proves that a composed slice with no producer fails the ratchet.
3. Absent and Labelled: An unavailable or unlanded slice must be degraded=True,
   status='not available yet', display_value='not available yet', value=None --
   NEVER defaulted to 0, 0.0, or blank.
4. Observable Degradation: Non-fatal degradation is recorded in get_degradation_register().
5. AG-5 Re-derivation: Documents and ratifies that AG-5 was folded into Wave AG-2 (PR #38)
   under agreements_coverage_matrix.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from nce.degradation import get_degradation_register
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.business_insights.board_pack import do_generate_board_pack
from nce.vertical_modules.business_insights.brief import do_morning_brief
from nce.vertical_modules.business_insights.kpi import STATUS_NOT_AVAILABLE_YET
from nce.vertical_modules.business_insights.radar import do_risk_radar
from nce.vertical_modules.business_insights.slices import (
    COMPOSED_SLICES,
    ComposedSliceSpec,
    get_composed_slice_spec,
    resolve_slice,
)


class MockEngine:
    """Mock engine with no active pool for unit degradation tests."""

    def __init__(self) -> None:
        self.pg_pool = None
        self.pool = None


# ---------------------------------------------------------------------------
# 1. Producer Registry Ratchet: Every slice maps to a registered read tool
# ---------------------------------------------------------------------------


def _assert_composed_slices_have_registered_producers(
    slices: dict[str, ComposedSliceSpec],
) -> None:
    """Shared enforcement mechanism for the Producer Registry Ratchet.

    Both the real-registry ratchet test and its positive control call this
    exact function. That is deliberate: the positive control below mutates
    the real, live COMPOSED_SLICES dict (a copy of it plus one phantom entry)
    and passes it through this same validator, proving the *mechanism* itself
    fires against production data -- not merely that Python's `assert x in
    dict` raises on a string nobody registered, which would be true
    regardless of whether this validator, or anything wired to it, actually
    runs anywhere.
    """
    for slice_id, spec in sorted(slices.items()):
        assert isinstance(spec, ComposedSliceSpec)
        assert spec.slice_id == slice_id
        assert spec.tool_name, f"Slice {slice_id} has empty tool_name"
        assert spec.engine, f"Slice {slice_id} has empty engine"

        # Producer MUST exist in live TOOL_REGISTRY
        assert spec.tool_name in TOOL_REGISTRY, (
            f"Composed slice {slice_id!r} producer tool {spec.tool_name!r} is missing from TOOL_REGISTRY"
        )

        tool_spec = TOOL_REGISTRY[spec.tool_name]

        # Producer MUST be a read, NEVER a state mutation
        assert tool_spec.mutation is False, (
            f"Composed slice {slice_id!r} producer {spec.tool_name!r} has mutation=True; slices must be reads"
        )

        # Producer MUST have a callable handler coroutine
        assert callable(tool_spec.handler), (
            f"Composed slice {slice_id!r} producer {spec.tool_name!r} handler is not callable"
        )


def test_every_composed_slice_resolves_to_registered_read_tool() -> None:
    """Ratchet: Every composed slice in COMPOSED_SLICES must exist in TOOL_REGISTRY as a read."""
    assert len(COMPOSED_SLICES) >= 6, (
        "Expected at least 6 canonical composed slices in COMPOSED_SLICES"
    )
    _assert_composed_slices_have_registered_producers(COMPOSED_SLICES)


def test_composed_slices_cover_all_canonical_charter_producers() -> None:
    """Ratchet: Ensure all charter-specified producers (S-4, SU-3, RS-4, E-1, PR-3, AG-2/AG-5) are mapped."""
    mapped_tools = {spec.tool_name for spec in COMPOSED_SLICES.values()}

    expected_producer_tools = {
        "sales_morning_brief_slice",  # S-4 (#158)
        "support_at_risk_aggregate",  # SU-3 (#169)
        "resources_forecast_demand",  # RS-4 (839c3ff)
        "economy_snapshot_mrr_arr_churn",  # E-1 (#28)
        "procurement_aggregate_savings",  # PR-3 (#52)
        "agreements_coverage_matrix",  # AG-2 folding AG-5 (#38)
    }

    missing = expected_producer_tools - mapped_tools
    assert not missing, f"Missing charter-mandated producer tools in COMPOSED_SLICES: {missing}"


# ---------------------------------------------------------------------------
# 2. Positive Control (U18): A composed slice with no producer fails the ratchet
# ---------------------------------------------------------------------------


def test_ratchet_fails_when_composed_slice_has_no_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive Control: Proves the real ratchet mechanism fails on real production data.

    Fixed 2026-09-20 (inert-instrument audit): the previous version built a
    throwaway local dict and re-ran a hand-copied `assert x in TOOL_REGISTRY`
    inline, on a slice_id/tool_name it invented itself. That is trivially true
    by construction -- no string called "unregistered_phantom_hardware_probe"
    was ever going to be in TOOL_REGISTRY, so the test could not have told us
    anything about whether the real ratchet, or anything wired to it, would
    actually catch this. It never touched the real COMPOSED_SLICES module
    object or the shared validation logic.

    This version monkeypatches the real, live
    ``nce.vertical_modules.business_insights.slices.COMPOSED_SLICES`` module
    attribute -- a copy of the actual production registry plus one phantom
    entry -- and feeds it through ``_assert_composed_slices_have_registered_producers``,
    the exact same function the real ratchet test above calls against the
    unmutated registry. That proves the shared mechanism itself fires against
    (mutated) production data, not just that Python's `in` operator works.
    """
    import nce.vertical_modules.business_insights.slices as slices_module

    phantom_spec = ComposedSliceSpec(
        slice_id="phantom_telemetry_slice",
        engine="telemetry",
        tool_name="unregistered_phantom_hardware_probe",
        description="Phantom probe with no registered producer tool",
        cacheable=False,
    )
    mutated_slices = dict(slices_module.COMPOSED_SLICES)
    mutated_slices["phantom_telemetry_slice"] = phantom_spec
    monkeypatch.setattr(slices_module, "COMPOSED_SLICES", mutated_slices)

    with pytest.raises(AssertionError) as exc:
        _assert_composed_slices_have_registered_producers(slices_module.COMPOSED_SLICES)

    assert "phantom_telemetry_slice" in str(exc.value)
    assert "unregistered_phantom_hardware_probe" in str(exc.value)


def test_get_composed_slice_spec_rejects_unknown_id() -> None:
    """get_composed_slice_spec raises KeyError on unregistered slice ID."""
    with pytest.raises(KeyError) as exc:
        get_composed_slice_spec("non_existent_random_slice")
    assert "Unknown composed slice" in str(exc.value)


# ---------------------------------------------------------------------------
# 3. Absent and Labelled: Missing upstream data is labelled, never zero
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_slice_unlanded_engine_is_absent_and_labelled() -> None:
    """Unlanded engine (Resources) must resolve to degraded=True, 'not available yet', never 0."""
    engine = MockEngine()
    ns_id = str(uuid4())

    reg = get_degradation_register()
    reg.clear(ns_id)

    res = await resolve_slice(engine, ns_id, "resources_forecast")

    assert res["ok"] is False
    assert res["degraded"] is True
    assert res["status"] == STATUS_NOT_AVAILABLE_YET
    assert res["display_value"] == STATUS_NOT_AVAILABLE_YET
    assert res["value"] is None
    assert res["data"] is None
    assert res["provenance_nodes"] == []

    # STRICT: Never invent defaults
    assert res["value"] != 0
    assert res["value"] != 0.0
    assert res["display_value"] != "0"
    assert res["display_value"] != "0%"
    assert res["display_value"] != ""

    # Non-fatal degradation record
    degradations = reg.get_degradations(ns_id)
    assert len(degradations) > 0


@pytest.mark.asyncio
async def test_resolve_slice_with_override_preserves_schema() -> None:
    """Caller overrides (for simulation/testing) are parsed and formatted without network reads."""
    engine = MockEngine()
    ns_id = str(uuid4())

    override_data = {
        "pipeline_value": 75000.0,
        "pipeline_growth_pct": 32.5,
        "provenance_nodes": ["deal:001", "deal:002"],
    }

    res = await resolve_slice(engine, ns_id, "sales_pipeline_brief", override=override_data)

    assert res["ok"] is True
    assert res["degraded"] is False
    assert res["slice_id"] == "sales_pipeline_brief"
    assert res["engine"] == "sales"
    assert res["provenance_nodes"] == ["deal:001", "deal:002"]
    assert len(res["derived_from"]) == 2
    assert res["derived_from"][0]["source_node"] == "deal:001"
    assert res["metrics"]["pipeline_value"] == 75000.0
    assert res["metrics"]["pipeline_growth_pct"] == 32.5


# ---------------------------------------------------------------------------
# 4. Consumer Composition: Morning Brief, Board Pack, and Risk Radar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_morning_brief_composes_slices_with_label_when_unlanded() -> None:
    """Morning brief delegates slice resolution and labels unlanded resources slice."""
    engine = MockEngine()
    ns_id = str(uuid4())

    brief_res = await do_morning_brief(
        engine, {"namespace_id": ns_id, "principal_role": "executive"}
    )
    assert brief_res["status"] == "ok"
    briefing = brief_res["briefing"]

    # Capacity headline must be absent and labelled
    cap = briefing["capacity_headline"]
    assert cap["degraded"] is True
    assert cap["status"] == STATUS_NOT_AVAILABLE_YET
    assert cap["display_value"] == STATUS_NOT_AVAILABLE_YET
    assert cap["value"] is None
    assert cap["display_value"] != "0"
    assert cap["display_value"] != "0%"


@pytest.mark.asyncio
async def test_board_pack_composes_slices_and_grace_degrades_cleanly() -> None:
    """Board pack narrative resolves composed slices and collapses absent ones to 'not available yet'."""
    engine = MockEngine()
    ns_id = str(uuid4())

    bp_res = await do_generate_board_pack(
        engine,
        {
            "namespace_id": ns_id,
            "period": "2026-Q3",
            "principal_role": "executive",
        },
    )
    assert bp_res["status"] == "ok"
    sections = bp_res["board_pack"]["sections"]

    fin = sections["financial_pulse"]
    assert fin["degraded"] is True
    assert fin["status"] == STATUS_NOT_AVAILABLE_YET
    assert fin["revenue"] == STATUS_NOT_AVAILABLE_YET
    assert fin["gross_margin_pct"] is None

    sales = sections["sales_pipeline"]
    assert sales["degraded"] is True
    assert sales["status"] == STATUS_NOT_AVAILABLE_YET
    assert sales["pipeline_total_value"] == STATUS_NOT_AVAILABLE_YET

    cap = sections["operational_capacity"]
    assert cap["degraded"] is True
    assert cap["status"] == STATUS_NOT_AVAILABLE_YET
    assert cap["display_value"] == STATUS_NOT_AVAILABLE_YET


@pytest.mark.asyncio
async def test_risk_radar_evaluates_with_live_slice_resolution() -> None:
    """Risk radar evaluates rules via composed slices when data_override is empty."""
    engine = MockEngine()
    ns_id = str(uuid4())

    radar_res = await do_risk_radar(
        engine,
        {
            "namespace_id": ns_id,
            "principal_role": "executive",
            "data_override": {},
        },
    )

    # All rules without live DB data must be marked unevaluated with reason 'data_unavailable'
    assert len(radar_res["findings"]) == 0
    assert len(radar_res["unevaluated_rules"]) == 3
    for uneval in radar_res["unevaluated_rules"]:
        assert uneval["status"] == "unevaluated"
        assert uneval["reason"] == "data_unavailable"


# ---------------------------------------------------------------------------
# 5. AG-5 Re-derivation Verification
# ---------------------------------------------------------------------------


def test_ag5_folded_into_agreements_coverage_matrix() -> None:
    """
    Ratification: AG-5 (agreements_renewal_timeline) was folded into Wave AG-2 (#38)
    under agreements_coverage_matrix (which evaluates expiry windows and contract coverage).
    """
    ag_spec = COMPOSED_SLICES["agreements_coverage"]
    assert ag_spec.engine == "agreements"
    assert ag_spec.tool_name == "agreements_coverage_matrix"
    assert "agreements_coverage_matrix" in TOOL_REGISTRY
    assert "folding AG-5" in ag_spec.description
