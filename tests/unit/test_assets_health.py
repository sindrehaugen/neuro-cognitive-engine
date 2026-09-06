"""
tests/unit/test_assets_health.py
================================
Unit tests for Module 9 (Assets) Phase 2 B3 — HealthScore Writer & RS-3 Hardening.

Covers:
  - load_health_weights() loads config from asset-health-weights.json
  - RS-3: Health score declares its input coverage (e.g. "age-only, no telemetry")
  - RS-3: Predictive-failure Watcher stays SILENT on mock telemetry
  - Non-mock telemetry allows predictive-failure Watcher to fire on high degradation
  - DEGRADED transition triggers when health_score < degraded_threshold and state is ACTIVE
  - Non-ACTIVE assets do not erroneously transition to DEGRADED
  - Parameter validation: missing/invalid asset_id and namespace_id
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.vertical_modules.assets.health import (
    compute_asset_health,
    do_compute_health,
    load_health_weights,
)

# ---------------------------------------------------------------------------
# 1. Config loading
# ---------------------------------------------------------------------------


def test_load_health_weights_loads_valid_config() -> None:
    weights = load_health_weights()
    assert "weights" in weights
    assert "thresholds" in weights
    assert "defaults" in weights

    w = weights["weights"]
    assert "telemetry_weight" in w
    assert "mtbf_weight" in w
    assert "tickets_weight" in w
    assert "age_weight" in w
    total_weight = w["telemetry_weight"] + w["mtbf_weight"] + w["tickets_weight"] + w["age_weight"]
    assert abs(total_weight - 1.0) < 1e-6
    assert weights["thresholds"]["degraded_threshold"] == 60.0


# ---------------------------------------------------------------------------
# 2. RS-3: Score declares its input coverage (age-only)
# ---------------------------------------------------------------------------


def test_compute_health_declares_age_only_coverage_when_sparse() -> None:
    """RS-3: Sparse inputs must explicitly declare coverage rather than faking confidence."""
    now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
    created_at = now - timedelta(days=365)  # 1 year old

    result = compute_asset_health(
        created_at=created_at,
        current_state="ACTIVE",
        telemetry_samples=[],
        mtbf_prob_fail=None,
        open_tickets=[],
        now=now,
    )

    # RS-3 assertion: Coverage must declare age-only
    assert "age-only" in result["coverage"].lower()
    assert "no telemetry" in result["coverage"].lower()
    assert result["coverage_details"]["age"] is True
    assert result["coverage_details"]["telemetry"] is False
    assert result["coverage_details"]["mtbf"] is False
    assert result["coverage_details"]["tickets"] is False

    # Score should be defined and positive
    assert 0.0 <= result["health_score"] <= 100.0
    # Predictive failure must NOT fire with age-only
    assert result["predictive_failure"] is False
    assert result["predictive_failure_alert"] is None


# ---------------------------------------------------------------------------
# 3. RS-3: Predictive failure Watcher MUST stay SILENT on mock telemetry
# ---------------------------------------------------------------------------


def test_predictive_failure_silent_on_mock_telemetry() -> None:
    """RS-3: Predictive failure must NOT fire when telemetry is from the mock adapter,

    even if metrics / failure probabilities would otherwise trigger it.
    """
    now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
    created_at = now - timedelta(days=1500)  # old asset

    # Mock telemetry samples with critical readings
    mock_samples = [
        {"metric": "temperature_celsius", "value": 95.0, "raw": {"source": "mock"}},
        {"metric": "packet_loss_percent", "value": 25.0, "raw": {"source": "mock"}},
        {"metric": "uptime_seconds", "value": 12.0, "raw": {"source": "mock"}},
    ]

    result = compute_asset_health(
        created_at=created_at,
        current_state="ACTIVE",
        telemetry_samples=mock_samples,
        mtbf_prob_fail=0.85,  # high failure probability > 0.60
        open_tickets=[{"priority": "critical", "status": "open"}],
        now=now,
    )

    # Gate: Even with catastrophic numbers, predictive Watcher MUST NOT FIRE on mock
    assert result["coverage_details"]["telemetry_source"] == "mock"
    assert result["predictive_failure"] is False
    assert result["predictive_failure_alert"] is None
    assert result["predictive_failure_suppressed"] is True
    assert "mock" in (result["suppression_reason"] or "").lower()


def test_predictive_failure_fires_on_real_telemetry_with_high_degradation() -> None:
    """Control test: Predictive failure CAN and DOES fire when telemetry is REAL

    and degradation / failure probability exceeds the threshold.
    """
    now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
    created_at = now - timedelta(days=1500)

    # Real telemetry samples with high degradation
    real_samples = [
        {"metric": "temperature_celsius", "value": 92.0, "raw": {"source": "crestron"}},
        {"metric": "packet_loss_percent", "value": 20.0, "raw": {"source": "crestron"}},
    ]

    result = compute_asset_health(
        created_at=created_at,
        current_state="ACTIVE",
        telemetry_samples=real_samples,
        mtbf_prob_fail=0.75,  # high failure probability
        open_tickets=[{"priority": "high", "status": "open"}],
        now=now,
    )

    assert result["coverage_details"]["telemetry_source"] == "real"
    assert result["predictive_failure"] is True
    assert result["predictive_failure_alert"] is not None
    assert result["predictive_failure_alert"]["severity"] in ("high", "critical")
    assert result["predictive_failure_suppressed"] is False


# ---------------------------------------------------------------------------
# 4. State transition to DEGRADED
# ---------------------------------------------------------------------------


def test_degraded_transition_triggered_when_active_and_below_threshold() -> None:
    """When health_score < degraded_threshold and state is ACTIVE, trigger DEGRADED."""
    now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
    created_at = now - timedelta(days=2500)  # very old asset, well past 5yr lifespan

    result = compute_asset_health(
        created_at=created_at,
        current_state="ACTIVE",
        telemetry_samples=[
            {"metric": "packet_loss_percent", "value": 15.0, "raw": {"source": "real"}}
        ],
        mtbf_prob_fail=0.70,
        open_tickets=[{"priority": "high", "status": "open"}],
        now=now,
    )

    assert result["health_score"] < 60.0
    assert result["transitioned_to_degraded"] is True
    assert result["new_lifecycle_state"] == "DEGRADED"


def test_degraded_transition_not_triggered_for_non_active_states() -> None:
    """Non-ACTIVE states (e.g. INSTALLED, RETIRED) must not transition to DEGRADED."""
    now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
    created_at = now - timedelta(days=2500)

    result = compute_asset_health(
        created_at=created_at,
        current_state="INSTALLED",
        telemetry_samples=[],
        mtbf_prob_fail=0.90,
        open_tickets=[],
        now=now,
    )

    assert result["health_score"] < 60.0
    assert result["transitioned_to_degraded"] is False
    assert result["new_lifecycle_state"] == "INSTALLED"


# ---------------------------------------------------------------------------
# 5. Async do_compute_health core function validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_compute_health_validates_params() -> None:
    engine = MagicMock()

    with pytest.raises(ValueError, match="namespace_id.*required"):
        await do_compute_health(engine, {})

    with pytest.raises(ValueError, match="asset_id.*required"):
        await do_compute_health(engine, {"namespace_id": str(uuid4())})


@pytest.mark.asyncio
async def test_do_compute_health_asset_not_found() -> None:
    engine = MagicMock()
    ns_id = uuid4()
    asset_id = uuid4()

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None  # Asset not found

    with patch("nce.vertical_modules.assets.health.scoped_pg_session") as mock_session:
        mock_session.return_value.__aenter__.return_value = mock_conn
        with pytest.raises(ValueError, match="not found in namespace"):
            await do_compute_health(
                engine,
                {"namespace_id": str(ns_id), "asset_id": str(asset_id)},
            )


@pytest.mark.asyncio
async def test_do_compute_health_successful_flow() -> None:
    engine = MagicMock()
    ns_id = uuid4()
    asset_id = uuid4()

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "id": asset_id,
        "namespace_id": ns_id,
        "serial": "SN-TEST-123",
        "lifecycle_state": "ACTIVE",
        "created_at": datetime.now(timezone.utc) - timedelta(days=100),
    }
    mock_conn.fetch.return_value = []  # No telemetry rows

    with patch("nce.vertical_modules.assets.health.scoped_pg_session") as mock_session:
        mock_session.return_value.__aenter__.return_value = mock_conn
        result = await do_compute_health(
            engine,
            {"namespace_id": str(ns_id), "asset_id": str(asset_id)},
        )

    assert result["ok"] is True
    assert result["asset_id"] == str(asset_id)
    assert "age-only" in result["coverage"]
    assert result["predictive_failure"] is False


@pytest.mark.asyncio
async def test_handle_assets_compute_health() -> None:
    from nce.vertical_modules.assets.mcp_handlers import handle_assets_compute_health

    engine = MagicMock()
    ns_id = uuid4()
    asset_id = uuid4()

    with patch("nce.vertical_modules.assets.mcp_handlers.do_compute_health") as mock_core:
        mock_core.return_value = {
            "ok": True,
            "asset_id": str(asset_id),
            "health_score": 85.0,
            "coverage": "age-only, no telemetry",
        }
        res_str = await handle_assets_compute_health(
            engine,
            {"namespace_id": str(ns_id), "asset_id": str(asset_id)},
        )
        payload = json.loads(res_str)
        assert payload["ok"] is True
        assert payload["health_score"] == 85.0
        assert payload["coverage"] == "age-only, no telemetry"


@pytest.mark.asyncio
async def test_api_assets_health_get() -> None:
    from nce.admin_handlers._shared import admin_state
    from nce.admin_handlers.assets import api_assets_health

    engine = MagicMock()
    admin_state.engine = engine
    ns_id = uuid4()
    asset_id = uuid4()

    mock_request = MagicMock()
    mock_request.path_params = {"id": str(asset_id)}
    mock_request.query_params = {"namespace_id": str(ns_id)}

    with patch("nce.admin_handlers.assets.do_compute_health") as mock_core:
        mock_core.return_value = {
            "ok": True,
            "asset_id": str(asset_id),
            "health_score": 78.5,
            "coverage": "age-only, no telemetry",
        }
        response = await api_assets_health(mock_request)
        assert response.status_code == 200
        body = json.loads(response.body.decode())
        assert body["ok"] is True
        assert body["health_score"] == 78.5
