"""Unit tests for BI-4 Grace Degradation requirement.

BI-4: Show only slices whose engines are live. A missing engine collapses its
slice with an explicit "not available yet" -- NEVER 0, NEVER blank.
"""

from __future__ import annotations

import pytest

from nce.vertical_modules.business_insights.kpi import (
    STATUS_NOT_AVAILABLE_YET,
    do_kpi_dashboard,
)


class DummyEngine:
    def __init__(self, pool=None):
        self.pg_pool = pool
        self.pool = pool


@pytest.mark.asyncio
async def test_grace_degradation_missing_engine_returns_not_available_yet():
    """Missing or unlanded upstream engine (Resources, HR) must return 'not available yet'."""
    engine = DummyEngine()
    params = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
    }
    result = await do_kpi_dashboard(engine, params)
    assert result["status"] == "ok"
    kpis = result["kpis"]

    # Resources is not landed
    assert "resources" in kpis
    res_slice = kpis["resources"]
    assert res_slice["status"] == STATUS_NOT_AVAILABLE_YET
    assert res_slice["value"] is None
    assert res_slice["display_value"] == STATUS_NOT_AVAILABLE_YET
    # Guard against 0 or blank
    assert res_slice["display_value"] != "0"
    assert res_slice["display_value"] != ""
    assert res_slice["display_value"] != "0.0"

    # HR is not landed
    assert "hr" in kpis
    hr_slice = kpis["hr"]
    assert hr_slice["status"] == STATUS_NOT_AVAILABLE_YET
    assert hr_slice["value"] is None
    assert hr_slice["display_value"] == STATUS_NOT_AVAILABLE_YET
    assert hr_slice["display_value"] != "0"
    assert hr_slice["display_value"] != ""


@pytest.mark.asyncio
async def test_grace_degradation_when_engine_removed():
    """If an engine is removed or simulated unavailable, its slice collapses with 'not available yet'."""
    engine = DummyEngine()
    # Explicitly test removing support engine simulation
    params = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "simulate_absent_engines": ["support"],
    }
    result = await do_kpi_dashboard(engine, params)
    kpis = result["kpis"]
    assert "support" in kpis
    assert kpis["support"]["status"] == STATUS_NOT_AVAILABLE_YET
    assert kpis["support"]["display_value"] == STATUS_NOT_AVAILABLE_YET
    assert kpis["support"]["display_value"] != "0"
    assert kpis["support"]["display_value"] != ""
