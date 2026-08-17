"""
tests/unit/test_synthesis.py
============================
Unit tests for Predictive Memory Synthesis and "Alert before the alert" engine.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from jsonschema import validate

from nce.causal.correlation import CausalGraph
from nce.causal.synthesis import (
    PREDICTIVE_NODE_SCHEMA,
    PredictiveSynthesisEngine,
)

NS = uuid.UUID("cccccccc-0000-0000-0000-000000000001")


@pytest.fixture
def mock_pg_conn():
    conn = AsyncMock()
    conn.fetch = AsyncMock()
    conn.execute = AsyncMock()
    return conn


@pytest.fixture
def engine():
    pool = MagicMock()
    return PredictiveSynthesisEngine(pool)


class TestPredictiveSynthesisEngine:
    @pytest.mark.asyncio
    async def test_fetch_historical_incidents(self, engine, mock_pg_conn):
        # Setup mock events in event_log
        mock_pg_conn.fetch.return_value = [
            # Event type failure direct memory_id
            {
                "event_type": "store_memory_rolled_back",
                "params": {"memory_id": "device_01", "reason": "Connection lost"},
            },
            # Event type failure with nested entities dict
            {
                "event_type": "saga_recovered",
                "params": {"entities": [{"label": "device_02"}, {"label": "device_01"}]},
            },
            # Event type failure with string parameters (JSON serialised)
            {
                "event_type": "store_memory_rolled_back",
                "params": json.dumps({"device_id": "device_03"}),
            },
            # Normal event (should not count as failure)
            {
                "event_type": "store_memory",
                "params": {"memory_id": "device_01", "entities": ["device_01"]},
            },
        ]

        counts = await engine.fetch_historical_incidents(mock_pg_conn, NS, time_window_days=30.0)

        assert counts.get("device_01") == 2
        assert counts.get("device_02") == 1
        assert counts.get("device_03") == 1
        assert "device_04" not in counts

    @pytest.mark.asyncio
    async def test_fetch_netbox_mtbf_success(self, engine, mock_pg_conn):
        mock_pg_conn.fetch.return_value = [
            {"node_id": "device_01", "mtbf_hours": 150000.0},
            {"node_id": "device_02", "mtbf_hours": 80000.0},
        ]

        mtbf = await engine.fetch_netbox_mtbf(mock_pg_conn)
        assert mtbf["device_01"] == 150000.0
        assert mtbf["device_02"] == 80000.0

    @pytest.mark.asyncio
    async def test_fetch_netbox_mtbf_fallback_on_db_error(self, engine, mock_pg_conn):
        # Simulate UndefinedTableError by raising an exception
        mock_pg_conn.fetch.side_effect = Exception("Relation netbox_devices does not exist")

        mtbf = await engine.fetch_netbox_mtbf(mock_pg_conn)
        # Should gracefully return empty dict and log a warning instead of failing
        assert mtbf == {}

    def test_resolve_mtbf_for_node(self, engine):
        netbox_mtbf = {"device_01": 200000.0}

        # NetBox override exists
        assert engine._resolve_mtbf_for_node("device_01", "device", netbox_mtbf) == 200000.0

        # Baselines fallbacks
        assert engine._resolve_mtbf_for_node("device_02", "device", netbox_mtbf) == 100000.0
        assert engine._resolve_mtbf_for_node("service_01", "service", netbox_mtbf) == 50000.0
        assert engine._resolve_mtbf_for_node("app_01", "app", netbox_mtbf) == 30000.0
        assert engine._resolve_mtbf_for_node("circuit_01", "circuit", netbox_mtbf) == 80000.0
        assert engine._resolve_mtbf_for_node("unknown_01", "unknown", netbox_mtbf) == 75000.0

    @pytest.mark.asyncio
    async def test_generate_predictive_fault_nodes(self, engine, mock_pg_conn, monkeypatch):
        # Setup mock causal graph loading
        # We will create 2 nodes: switch_01 (1 failure) and server_02 (0 failures)
        raw_rows = [
            {
                "source_node_id": "switch_01",
                "source_node_type": "device",
                "target_node_id": "server_02",
                "target_node_type": "device",
                "edge_type": "connected_to",
                "confidence_score": 0.9,
                "decay_coefficient": 0.001,
                "last_verified": datetime.now(timezone.utc),
            }
        ]
        mock_graph = CausalGraph.from_rows(raw_rows, NS)

        monkeypatch.setattr(
            CausalGraph,
            "load_from_db",
            AsyncMock(return_value=mock_graph),
        )

        # Mock incidents and NetBox MTBF
        incident_counts = {"switch_01": 2}  # switch_01 has failures
        mtbf_dict = {"switch_01": 100000.0, "server_02": 100000.0}

        monkeypatch.setattr(
            engine,
            "fetch_historical_incidents",
            AsyncMock(return_value=incident_counts),
        )
        monkeypatch.setattr(
            engine,
            "fetch_netbox_mtbf",
            AsyncMock(return_value=mtbf_dict),
        )

        nodes = await engine.generate_predictive_fault_nodes(
            mock_pg_conn, NS, time_window_days=30.0, probability_threshold=0.01
        )

        # Switch should exceed the threshold due to incidents + baseline risk
        assert len(nodes) > 0
        switch_fault = next((n for n in nodes if n["target_device_id"] == "switch_01"), None)
        assert switch_fault is not None
        assert switch_fault["node_type"] == "predictive_fault"
        assert switch_fault["empirical_failures_count"] == 2
        assert switch_fault["target_device_type"] == "device"

        # Verify it conforms to json schema
        validate(instance=switch_fault, schema=PREDICTIVE_NODE_SCHEMA)

    @pytest.mark.asyncio
    async def test_sync_predictive_nodes_to_topology(self, engine, mock_pg_conn):
        predictive_nodes = [
            {
                "node_id": "predictive_fault_switch_01",
                "node_type": "predictive_fault",
                "target_device_id": "switch_01",
                "target_device_type": "device",
                "failure_probability": 0.25,
                "mtbf_hours": 100000.0,
                "empirical_failures_count": 2,
                "estimated_time_to_failure_days": 15.0,
                "created_at": "2026-06-05T20:00:00Z",
            }
        ]

        await engine.sync_predictive_nodes_to_topology(mock_pg_conn, NS, predictive_nodes)

        # Ensure delete was executed
        delete_calls = [
            c
            for c in mock_pg_conn.execute.call_args_list
            if "DELETE FROM topology_graph" in c[0][0]
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0][0][1] == NS

        # Ensure insert was executed via executemany batch query
        insert_calls = [
            c
            for c in mock_pg_conn.executemany.call_args_list
            if "INSERT INTO topology_graph" in c[0][0]
        ]
        assert len(insert_calls) == 1
        data_tuples = insert_calls[0][0][1]
        assert len(data_tuples) == 1
        assert data_tuples[0][0] == NS
        assert data_tuples[0][1] == "predictive_fault_switch_01"
        assert data_tuples[0][2] == "switch_01"
        assert data_tuples[0][3] == "device"
        assert data_tuples[0][4] == 0.25
