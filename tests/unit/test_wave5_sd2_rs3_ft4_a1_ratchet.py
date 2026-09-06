"""
tests/unit/test_wave5_sd2_rs3_ft4_a1_ratchet.py
================================================
Ratchet test suite for Wave 5: Real Data Behind the AI.
  - SD-2: Design recall outcome-weighting & coverage indicator.
  - RS-3: Resource allocation outcome recorder (Tool + C10 wiring).
  - FT-4: Field Tech outcome routing to Vendors/HR/Economy via engine registry.
  - A-1:  YMCS real telemetry adapter with <5s timeout & cron scheduling.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)
from nce.vertical_modules.assets.telemetry import (
    VENDOR_PLATFORMS,
    select_telemetry_adapter,
)
from nce.vertical_modules.assets.ymcs import (
    YMCSTelemetryAdapter,
)
from nce.vertical_modules.system_design.propose import (
    _apply_outcome_weights,
)

# ===========================================================================
# 1. SD-2: System Design Outcome Weighting & Coverage Indicators
# ===========================================================================


def test_sd2_apply_outcome_weights_disabled():
    """When outcome weighting is disabled, candidates are returned un-reordered."""
    cands = [
        {"name": "proj-1", "similarity": 0.85},
        {"name": "proj-2", "similarity": 0.90},
    ]
    ranked, coverage = _apply_outcome_weights(cands, enabled=False)
    assert len(ranked) == 2
    assert ranked[0]["name"] == "proj-1"
    assert coverage["attributed_outcomes"] == 0
    assert coverage["status"] == "similarity-only: 0 attributed outcomes"


def test_sd2_apply_outcome_weights_zero_attributed():
    """When enabled but no outcomes exist, coverage reports 0 attributed."""
    cands = [
        {"name": "proj-1", "similarity": 0.85},
        {"name": "proj-2", "similarity": 0.90},
    ]
    ranked, coverage = _apply_outcome_weights(cands, enabled=True, outcomes={})
    assert len(ranked) == 2
    assert coverage["attributed_outcomes"] == 0
    assert coverage["status"] == "similarity-only: 0 attributed outcomes"
    for r in ranked:
        assert r["attributed_outcome"] is False
        assert r["outcome_confidence"] is None


def test_sd2_apply_outcome_weights_boost_and_discount():
    """When outcomes exist, scores are adjusted and reordered with coverage ratio."""
    cands = [
        {"name": "proj-1", "similarity": 0.80},
        {"name": "proj-2", "similarity": 0.82},
    ]
    outcomes = {
        "proj-1": {
            "confidence": 0.9,
            "margin_drift": 0.5,
        },  # boost: 1.0 + 0.5 = 1.5 -> 0.80 * 1.5 = 1.20
        "proj-2": {
            "confidence": 0.8,
            "margin_drift": -0.5,
        },  # discount: 1.0 - 0.5 = 0.5 -> 0.82 * 0.5 = 0.41
    }
    ranked, coverage = _apply_outcome_weights(cands, enabled=True, outcomes=outcomes)
    assert coverage["attributed_outcomes"] == 2
    assert coverage["total_candidates"] == 2
    assert coverage["status"] == "outcome-weighted: 2/2 attributed outcomes"

    # proj-1 boosted above proj-2
    assert ranked[0]["name"] == "proj-1"
    assert ranked[0]["weighted_score"] == pytest.approx(1.20)
    assert ranked[0]["attributed_outcome"] is True
    assert ranked[1]["name"] == "proj-2"
    assert ranked[1]["weighted_score"] == pytest.approx(0.41)


@pytest.mark.asyncio
async def test_sd2_propose_design_records_degradation_on_zero_outcomes():
    """When outcome weighting is enabled but 0 outcomes exist, degradation event is recorded."""
    from nce.vertical_modules.system_design.propose import do_propose_design

    mock_engine = MagicMock()
    mock_conn = AsyncMock()

    # Candidates returned from vector search
    mock_conn.fetch.side_effect = [
        [
            {
                "id": uuid4(),
                "node_type": "DESIGN",
                "name": "Old Room",
                "content": "CS-700 setup",
                "metadata": json.dumps({"project_id": "proj-alpha"}),
                "similarity": 0.92,
                "distance": 0.08,
            }
        ],
        [],  # 0 outcome rows from kg_edges
    ]
    mock_conn.fetchrow.return_value = {
        "metadata": json.dumps({"system_design": {"outcome_weighting_enabled": True}})
    }

    with (
        patch("nce.vertical_modules.system_design.propose.scoped_pg_session") as mock_scoped,
        patch(
            "nce.vertical_modules.system_design.propose.embed",
            new=AsyncMock(return_value=[0.1] * 1536),
        ),
        patch("nce.degradation.record_degradation") as mock_record_deg,
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        res = await do_propose_design(
            mock_engine,
            {
                "namespace_id": str(uuid4()),
                "room_brief": "Executive boardroom with Yamaha ADECIA",
                "outcome_weighting_enabled": True,
            },
        )

        assert "coverage" in res
        assert res["coverage"]["attributed_outcomes"] == 0
        assert res["coverage"]["status"] == "similarity-only: 0 attributed outcomes"
        assert res["outcome_weighting_applied"] is False

        # Degradation event recorded
        mock_record_deg.assert_called_once()
        call_kwargs = mock_record_deg.call_args[1]
        assert call_kwargs["engine"] == "system_design"
        assert call_kwargs["code"] == "similarity_only_zero_outcomes"
        assert "record 5 to unlock" in call_kwargs["onboarding_hint"]


# ===========================================================================
# 2. RS-3: Resource Allocation Outcome Recorder
# ===========================================================================


def test_rs3_tool_registration_and_surface():
    """Verify resources_record_allocation_outcome is properly registered and surfaced."""
    assert "resources_record_allocation_outcome" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["resources_record_allocation_outcome"]
    assert spec.mutation is True
    assert spec.admin_only is True
    assert spec.cacheable is False
    assert "resources_record_allocation_outcome" in MUTATION_TOOLS
    assert "resources_record_allocation_outcome" in ADMIN_ONLY_TOOLS


def test_rs3_allowlist_shrink():
    """do_record_allocation_outcome must NOT be in internal-cores.json."""
    repo_root = Path(__file__).resolve().parents[2]
    cores_path = repo_root / "nce" / "config_data" / "internal-cores.json"
    with open(cores_path, encoding="utf-8") as f:
        data = json.load(f)
    allowlist = set(data.keys()) if isinstance(data, dict) else set(data)
    assert (
        "nce/vertical_modules/resources/planner.py::do_record_allocation_outcome" not in allowlist
    )
    assert len(allowlist) <= 68


@pytest.mark.asyncio
async def test_rs3_record_allocation_outcome_writes_ledger_and_decision_feedback():
    """RS-3: do_record_allocation_outcome appends to cognitive ledger and calls C10."""
    from nce.vertical_modules.resources.planner import do_record_allocation_outcome

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = uuid4()  # ledger id

    with (
        patch("nce.vertical_modules.resources.planner.scoped_pg_session") as mock_scoped,
        patch(
            "nce.vertical_modules.resources.planner.record_decision_feedback",
            new=AsyncMock(return_value={"id": str(uuid4())}),
        ) as mock_c10,
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        ns_id = uuid4()
        res_id = uuid4()
        alloc_id = uuid4()

        res = await do_record_allocation_outcome(
            mock_engine,
            {
                "namespace_id": str(ns_id),
                "resource_id": str(res_id),
                "allocation_id": str(alloc_id),
                "rating": 4.5,
                "quality_score": 0.95,
                "demand_kind": "project",
                "on_time": True,
                "notes": "Delivered ahead of schedule",
                "actor": "lead_pm",
            },
        )

        assert "ledger_id" in res
        assert res["resource_id"] == str(res_id)
        assert res["rating"] == 4.5
        assert mock_conn.execute.called
        assert mock_c10.called
        c10_kwargs = mock_c10.call_args[1]
        assert c10_kwargs["engine"] == "resources"
        assert c10_kwargs["decision"] == "held"


# ===========================================================================
# 3. FT-4: Field Tech Outcome Routing via Engine Registry
# ===========================================================================


@pytest.mark.asyncio
async def test_ft4_do_record_outcome_routes_to_vendors_and_c10():
    """FT-4: do_record_outcome routes to vendors when contractor is assigned."""
    from nce.vertical_modules.field_tech.outcome import do_record_outcome

    mock_engine = MagicMock()
    mock_vendors = MagicMock()
    mock_vendors.do_record_outcome = AsyncMock(return_value={"status": "vendor_recorded"})

    ns_modules = {
        "vendors": mock_vendors,
        "economy": MagicMock(),
    }
    mock_registry = MagicMock()
    mock_registry.for_namespace.return_value = ns_modules
    mock_engine.modules = mock_registry

    wo_id = "WO-1234"
    ns_id = uuid4()
    contractor_id = str(uuid4())

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "work_order_id": wo_id,
        "namespace_id": ns_id,
        "title": "Boardroom mic repair",
        "kind": "repair",
        "status": "in_progress",
        "assignee_kind": "contractor",
        "partner_scope_id": None,
        "assignee_id": contractor_id,
        "raw": "{}",
    }
    mock_conn.fetchval.return_value = uuid4()

    with (
        patch("nce.vertical_modules.field_tech.outcome.scoped_pg_session") as mock_scoped,
        patch(
            "nce.vertical_modules.field_tech.outcome.record_decision_feedback",
            new=AsyncMock(return_value={"id": str(uuid4())}),
        ),
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        res = await do_record_outcome(
            mock_engine,
            {
                "namespace_id": str(ns_id),
                "work_order_id": str(wo_id),
                "completed_by": contractor_id,
                "rating": 5.0,
                "quality_score": 1.0,
            },
        )

        assert res["status"] == "recorded"
        assert "vendors" in res["routed_engines"]
        assert "economy" in res["routed_engines"]
        assert mock_vendors.do_record_outcome.called


# ===========================================================================
# 4. A-1: Yamaha YMCS Real Telemetry Adapter
# ===========================================================================


def test_a1_select_telemetry_adapter_ymcs(monkeypatch: pytest.MonkeyPatch):
    """Verify YMCS platform is recognized in VENDOR_PLATFORMS and instantiates YMCSTelemetryAdapter when enabled."""
    from nce.vertical_modules.assets.telemetry import MockTelemetryAdapter, real_adapter_env_key

    assert "ymcs" in VENDOR_PLATFORMS
    # When flag is unset, default is MockTelemetryAdapter per estate contract
    assert isinstance(select_telemetry_adapter("ymcs"), MockTelemetryAdapter)
    # When flag is set, swaps to real YMCSTelemetryAdapter
    monkeypatch.setenv(real_adapter_env_key("ymcs"), "1")
    adapter = select_telemetry_adapter("ymcs")
    assert isinstance(adapter, YMCSTelemetryAdapter)
    assert adapter.platform == "ymcs"


@pytest.mark.asyncio
async def test_a1_ymcs_deterministic_fallback():
    """Unconfigured adapter fails loud; configured adapter parses HTTP responses."""
    import httpx

    adapter = YMCSTelemetryAdapter(endpoint_url=None, api_key=None)
    asset_id = UUID("12345678-1234-5678-1234-567812345678")
    with pytest.raises(NotImplementedError, match="ymcs") as excinfo:
        await adapter.fetch_samples(asset_id)
    assert "NCE_ASSETS_YMCS_ENDPOINT_URL" in str(excinfo.value)
    assert "NCE_ASSETS_YMCS_API_KEY" in str(excinfo.value)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "metrics": {
                    "uptime_seconds": 1200.0,
                    "temperature_celsius": 38.5,
                    "packet_loss_percent": 0.05,
                    "mic_mute_status": 0.0,
                    "link_status": 1.0,
                },
                "raw": {"source": "ymcs", "device": "Yealink-MeetingBar-A30"},
            },
        )

    adapter_live = YMCSTelemetryAdapter(
        endpoint_url="https://ymcs.yealink.com",
        api_key="secret-key",
        transport=httpx.MockTransport(handler),
    )
    samples = await adapter_live.fetch_samples(asset_id)

    assert len(samples) >= 5
    metrics = {s.metric: s.value for s in samples}
    assert "uptime_seconds" in metrics
    assert "temperature_celsius" in metrics
    assert "packet_loss_percent" in metrics
    assert "mic_mute_status" in metrics
    assert "link_status" in metrics
    assert metrics["packet_loss_percent"] < 1.0
    assert 20.0 < metrics["temperature_celsius"] < 60.0


def test_a1_ymcs_timeout_enforcement():
    """Strict AV Operations rule: timeouts must be strictly < 5.0s."""
    adapter_default = YMCSTelemetryAdapter()
    assert adapter_default._timeout < 5.0

    # Explicit attempt to pass >5s timeout is bounded to <= 4.9s
    adapter_high = YMCSTelemetryAdapter(timeout=10.0)
    assert adapter_high._timeout <= 4.9


def test_a1_cron_scheduled_in_async_main():
    """Verify _assets_telemetry_tick is defined and referenced in cron.py."""
    import nce.cron as cron_mod

    assert hasattr(cron_mod, "_assets_telemetry_tick")
    assert callable(cron_mod._assets_telemetry_tick)
