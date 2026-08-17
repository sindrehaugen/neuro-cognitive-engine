"""
tests/unit/test_netbox_circuits.py
==================================
Unit tests for NetBox Circuit Provider Escalation Engine.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.vertical_modules.netbox.circuits import NetBoxCircuitEscalator

NS = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")


class MockConnection:
    """Mock connection that returns predefined topology graph rows for CausalGraph."""

    def __init__(self, fetch_results: list[dict[str, Any]]) -> None:
        self.fetch_results = fetch_results

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        # CausalGraph.load_from_db executes a query on topology_graph
        return self.fetch_results


@pytest.mark.anyio
class TestNetBoxCircuitEscalator:
    async def test_evaluate_and_escalate_generates_ticket(self):
        # 1. Mock NetBox API response
        mock_circuits = [
            {
                "id": 123,
                "cid": "circuit_01",
                "provider": {"id": 50, "name": "Global Transit Corp"},
                "commit_rate": 10000000,
                "custom_fields": {"account_string": "ACCT-GTC-999"},
            }
        ]
        client_mock = MagicMock()
        client_mock.fetch_circuits = AsyncMock(return_value=mock_circuits)

        # 2. Setup mock database connection with topology rows:
        # circuit_01 --connected_to--> switch_01 (confidence = 0.95)
        # switch_01 --host_application--> app_01 (confidence = 0.90)
        now = datetime.now(timezone.utc)
        topology_rows = [
            {
                "source_node_id": "circuit_01",
                "source_node_type": "circuit",
                "target_node_id": "switch_01",
                "target_node_type": "device",
                "edge_type": "connected_to",
                "confidence_score": 0.95,
                "decay_coefficient": 0.001,
                "last_verified": now,
            },
            {
                "source_node_id": "switch_01",
                "source_node_type": "device",
                "target_node_id": "app_01",
                "target_node_type": "app",
                "edge_type": "host_application",
                "confidence_score": 0.90,
                "decay_coefficient": 0.001,
                "last_verified": now,
            },
        ]
        conn = MockConnection(topology_rows)

        # 3. Setup telemetry degradations
        telemetry_degradations = {"app_01": 0.85, "switch_01": 0.90, "unrelated_node": 0.95}

        # 4. Instantiate escalator and run
        escalator = NetBoxCircuitEscalator(client_mock)
        tickets = await escalator.evaluate_and_escalate(
            conn, NS, telemetry_degradations, degradation_threshold=0.5, causal_threshold=0.5
        )

        # 5. Assertions
        assert len(tickets) == 1
        ticket = tickets[0]
        assert ticket["circuit_id"] == "circuit_01"
        assert ticket["provider_name"] == "Global Transit Corp"
        assert ticket["account_string"] == "ACCT-GTC-999"
        assert ticket["commit_rate_kbps"] == 10000000
        assert ticket["severity"] == "CRITICAL"  # switch_01 severity = 0.90 >= 0.8

        linked = ticket["causally_linked_degradations"]
        assert "switch_01" in linked
        assert "app_01" in linked
        assert "unrelated_node" not in linked

        # Verify causal probabilities
        # switch_01: P(impact | do(circuit_01)) = 0.95
        # app_01: P(impact | do(circuit_01)) = 0.95 * 0.90 = 0.855
        assert pytest.approx(linked["switch_01"]["causal_probability"]) == 0.95
        assert pytest.approx(linked["app_01"]["causal_probability"]) == 0.855

    async def test_evaluate_and_escalate_no_tickets_when_no_causal_impact(self):
        mock_circuits = [
            {
                "id": 123,
                "cid": "circuit_01",
                "provider": {"name": "Test ISP"},
            }
        ]
        client_mock = MagicMock()
        client_mock.fetch_circuits = AsyncMock(return_value=mock_circuits)

        # Graph where circuit_01 is disconnected from switch_01 (different components)
        topology_rows = [
            {
                "source_node_id": "switch_01",
                "source_node_type": "device",
                "target_node_id": "app_01",
                "target_node_type": "app",
                "edge_type": "host_application",
                "confidence_score": 0.90,
                "decay_coefficient": 0.001,
                "last_verified": datetime.now(timezone.utc),
            }
        ]
        # Include circuit_01 in the nodes list of load_from_db rows
        # In from_rows, nodes are collected from source/target columns, so we can define a self-loop or dummy link
        topology_rows.append(
            {
                "source_node_id": "circuit_01",
                "source_node_type": "circuit",
                "target_node_id": "circuit_01",
                "target_node_type": "circuit",
                "edge_type": "connected_to",
                "confidence_score": 0.0,
                "decay_coefficient": 0.001,
                "last_verified": datetime.now(timezone.utc),
            }
        )

        conn = MockConnection(topology_rows)
        telemetry_degradations = {"app_01": 0.90}

        escalator = NetBoxCircuitEscalator(client_mock)
        tickets = await escalator.evaluate_and_escalate(
            conn, NS, telemetry_degradations, degradation_threshold=0.5, causal_threshold=0.5
        )

        # Since circuit_01 has no causal path to app_01, no ticket should be generated
        assert len(tickets) == 0

    @patch("nce.config.cfg.NCE_NETBOX_URL", "http://netbox.test")
    @patch("nce.config.cfg.NCE_NETBOX_TOKEN", "token123")
    async def test_handle_evaluate_circuit_impact_success(self):
        from nce.vertical_modules.netbox.circuits import handle_evaluate_circuit_impact

        # Mock engine and pg_pool
        mock_engine = MagicMock()
        mock_conn = AsyncMock()

        # Mock scoped_pg_session to return our mock_conn
        mock_scoped_session = MagicMock()
        mock_scoped_session.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_scoped_session.__aexit__ = AsyncMock()

        # Mock evaluate_and_escalate
        mock_tickets = [{"ticket_id": "ticket-uuid", "circuit_id": "circuit_01"}]
        mock_escalate = AsyncMock(return_value=mock_tickets)

        with (
            patch("nce.db_utils.scoped_pg_session", return_value=mock_scoped_session),
            patch(
                "nce.vertical_modules.netbox.circuits.NetBoxCircuitEscalator.evaluate_and_escalate",
                mock_escalate,
            ),
            patch(
                "nce.vertical_modules.netbox.circuits.NetBoxCircuitsClient.fetch_circuits",
                AsyncMock(return_value=[]),
            ),
        ):
            args = {
                "namespace_id": "00000000-0000-4000-8000-000000000001",
                "telemetry_degradations": {"switch_01": 0.85},
                "degradation_threshold": 0.5,
                "causal_threshold": 0.5,
            }
            resp_str = await handle_evaluate_circuit_impact(mock_engine, args)
            import json

            resp = json.loads(resp_str)
            assert resp["status"] == "success"
            assert resp["tickets"] == mock_tickets
            mock_escalate.assert_called_once()

    @patch("nce.config.cfg.NCE_NETBOX_URL", "")
    @patch("nce.config.cfg.NCE_NETBOX_TOKEN", "")
    async def test_handle_evaluate_circuit_impact_unset_creds(self):
        from nce.vertical_modules.netbox.circuits import handle_evaluate_circuit_impact

        mock_engine = MagicMock()
        args = {
            "namespace_id": "00000000-0000-4000-8000-000000000001",
            "telemetry_degradations": {"switch_01": 0.85},
        }
        resp_str = await handle_evaluate_circuit_impact(mock_engine, args)
        import json

        resp = json.loads(resp_str)
        assert "error" in resp
        assert "NetBox is not configured" in resp["error"]

    @patch("nce.config.cfg.NCE_NETBOX_URL", "http://netbox.test")
    @patch("nce.config.cfg.NCE_NETBOX_TOKEN", "token123")
    async def test_ingest_sla_breach_triggers_escalation(self):
        from nce.vertical_modules.dynamics365.ingestion import DataverseIngestionWorker

        # Mock pool, clients, connections
        mock_pool = MagicMock()
        mock_mongo = MagicMock()
        mock_redis = MagicMock()

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_scoped_session = MagicMock()
        mock_scoped_session.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_scoped_session.__aexit__ = AsyncMock()

        # Mock ingest methods we don't care about or patch embeddings
        ns_id = uuid.uuid4()
        worker = DataverseIngestionWorker(mock_pool, mock_mongo, mock_redis, ns_id)

        # Mock _store_to_mongo
        worker._store_to_mongo = AsyncMock(return_value="mongo_id_123")
        worker._insert_memory_with_conn = AsyncMock(return_value=uuid.uuid4())
        worker._upsert_kg_edge_with_conn = AsyncMock()

        mock_tickets = [{"ticket_id": "ticket_123", "circuit_id": "circuit_01"}]
        mock_escalate = AsyncMock(return_value=mock_tickets)

        with (
            patch("nce.embeddings.embed_batch", AsyncMock(return_value=[[0.1] * 768])),
            patch("nce.event_log.append_event", AsyncMock()) as mock_append,
            patch("nce.db_utils.scoped_pg_session", return_value=mock_scoped_session),
            patch(
                "nce.vertical_modules.netbox.circuits.NetBoxCircuitEscalator.evaluate_and_escalate",
                mock_escalate,
            ),
        ):
            resp = await worker.ingest_sla_breach(
                conn=mock_conn,
                incident_id="incident_123",
                breach_type="first_response",
                account_name="Test Account",
                impacted_services=["switch_01", "app_02"],
            )
            assert resp["tickets"] == mock_tickets
            mock_escalate.assert_called_once()
            assert mock_append.call_count >= 2
