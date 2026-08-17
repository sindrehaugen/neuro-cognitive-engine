"""
tests/unit/test_netbox_mtbf.py
==============================
Unit tests for Predictive MTBF Synthesis forecasting module.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.vertical_modules.netbox.mtbf import NetBoxMTBFForecaster

NS = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")

MOCK_DEVICES_DATA = {
    "data": {
        "device_list": [
            {
                "id": "device-1",
                "name": "switch-01",
                "serial": "SN-SW01",
                "custom_fields": {
                    "provisioning_date": "2021-06-01",
                    "hardware_lifespan_years": 5.0,
                },
            },
            {
                "id": "device-2",
                "name": "switch-02",
                "serial": "SN-SW02",
                "custom_fields": {
                    "provisioning_date": (
                        datetime.now(timezone.utc) - timedelta(days=365)
                    ).strftime("%Y-%m-%d"),
                    "hardware_lifespan_years": 5.0,
                },
            },
            {"id": "device-3", "name": "switch-03", "serial": "SN-SW03", "custom_fields": None},
        ]
    }
}


class MockConnection:
    def __init__(self, fetch_rows: list[dict[str, Any]]) -> None:
        self.fetch_rows = fetch_rows
        self.execute_calls = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return self.fetch_rows

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "SUCCESS"


@pytest.mark.anyio
class TestNetBoxMTBFForecaster:
    async def test_fetch_anomaly_counts(self) -> None:
        client_mock = MagicMock()
        forecaster = NetBoxMTBFForecaster(client_mock)

        # Mock event log failures:
        # 2 direct reference device_id, 1 nested entities label
        event_rows = [
            {"event_type": "store_memory_rolled_back", "params": {"device_id": "switch-01"}},
            {
                "event_type": "saga_recovered",
                "params": {"entities": [{"label": "switch-01"}, {"label": "switch-02"}]},
            },
            {"event_type": "store_memory_failed", "params": {"node_id": "switch-02"}},
        ]
        conn = MockConnection(event_rows)
        counts = await forecaster.fetch_anomaly_counts(conn, NS, window_days=30.0)

        assert counts["switch-01"] == 2
        assert counts["switch-02"] == 2

    async def test_evaluate_forecast_math_and_sorting(self) -> None:
        # Mock graphql client response
        client_mock = MagicMock()
        client_mock.execute_query = AsyncMock(return_value=MOCK_DEVICES_DATA)
        forecaster = NetBoxMTBFForecaster(client_mock)

        # Mock event log failures (switch-01 has 3 anomalies, switch-02 has 0)
        event_rows = [
            {"event_type": "error", "params": {"device_id": "switch-01"}},
            {"event_type": "error", "params": {"device_id": "switch-01"}},
            {"event_type": "error", "params": {"device_id": "switch-01"}},
        ]
        conn = MockConnection(event_rows)

        forecast = await forecaster.evaluate_forecast(
            conn=conn,
            namespace_id=NS,
            forecast_window_days=30.0,
            observation_window_days=30.0,
            baseline_mtbf_years=10.0,
        )

        assert len(forecast) == 3

        # Assert correct sorting (highest failure probability first)
        # switch-01 (older age, has anomalies) > switch-03 (fallback age, 0 anomalies) > switch-02 (new age, 0 anomalies)
        assert forecast[0]["device_name"] == "switch-01"
        assert forecast[1]["device_name"] == "switch-03"
        assert forecast[2]["device_name"] == "switch-02"

        # Check switch-01 details
        sw1 = forecast[0]
        assert sw1["serial"] == "SN-SW01"
        assert sw1["anomaly_count"] == 3
        assert 0.0 <= sw1["failure_probability"] <= 1.0
        assert sw1["estimated_mtbf_hours"] > 0.0

        # Check defensive fallback switch-03
        sw3 = forecast[1]
        assert sw3["serial"] == "SN-SW03"
        assert sw3["anomaly_count"] == 0
        assert sw3["lifespan_years"] == 5.0
        # Age should default to 3 years ago
        assert pytest.approx(sw3["age_years"], abs=0.1) == 3.0

        # Verify mathematical bounds on all probabilities
        for item in forecast:
            p = item["failure_probability"]
            assert isinstance(p, float)
            assert 0.0 <= p <= 1.0

    async def test_evaluate_forecast_extreme_lifespan_overflow_protection(self) -> None:
        # Mock device with near-zero expected lifespan which would normally trigger OverflowError
        extreme_data = {
            "data": {
                "device_list": [
                    {
                        "id": "device-extreme",
                        "name": "switch-extreme",
                        "serial": "SN-EXTREME",
                        "custom_fields": {
                            "provisioning_date": "2020-01-01",
                            "hardware_lifespan_years": 0.00001,
                        },
                    }
                ]
            }
        }
        client_mock = MagicMock()
        client_mock.execute_query = AsyncMock(return_value=extreme_data)
        forecaster = NetBoxMTBFForecaster(client_mock)
        conn = MockConnection([])

        forecast = await forecaster.evaluate_forecast(
            conn=conn,
            namespace_id=NS,
            forecast_window_days=30.0,
            observation_window_days=30.0,
            baseline_mtbf_years=10.0,
        )

        assert len(forecast) == 1
        item = forecast[0]
        assert item["device_name"] == "switch-extreme"
        # Verify that OverflowError is avoided and the failure probability is gracefully bounded to 1.0
        assert item["failure_probability"] == 1.0
        assert item["estimated_mtbf_hours"] >= 0.0
