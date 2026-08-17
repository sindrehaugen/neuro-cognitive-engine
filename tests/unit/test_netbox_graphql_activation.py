"""
tests/unit/test_netbox_graphql_activation.py
============================================
Unit tests for the GraphQL-powered spreading activation engine.
"""

from __future__ import annotations

import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.vertical_modules.netbox.graphql_activation import (
    UNIFIED_TOPOLOGY_QUERY,
    GraphQLSpikingActivator,
    NetBoxGraphQLClient,
    parse_topology,
)

NS = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")

MOCK_TOPOLOGY_DATA = {
    "data": {
        "site_list": [
            {
                "id": "site-1",
                "name": "Site-A",
                "slug": "site-a",
                "racks": [
                    {
                        "id": "rack-1",
                        "name": "Rack-A1",
                        "devices": [
                            {
                                "id": "device-1",
                                "name": "Device-1",
                                "interfaces": [
                                    {
                                        "id": "interface-1",
                                        "name": "GigabitEthernet0/1",
                                        "cable": {
                                            "id": "cable-1",
                                            "status": "connected",
                                            "a_terminations": [
                                                {
                                                    "id": "interface-1",
                                                    "name": "GigabitEthernet0/1",
                                                    "device": {
                                                        "id": "device-1",
                                                        "name": "Device-1",
                                                    },
                                                }
                                            ],
                                            "b_terminations": [
                                                {
                                                    "id": "interface-2",
                                                    "name": "GigabitEthernet0/2",
                                                    "device": {
                                                        "id": "device-2",
                                                        "name": "Device-2",
                                                    },
                                                }
                                            ],
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "devices": [
                    {
                        "id": "device-2",
                        "name": "Device-2",
                        "rack": None,
                        "interfaces": [
                            {
                                "id": "interface-2",
                                "name": "GigabitEthernet0/2",
                                "cable": {
                                    "id": "cable-1",
                                    "status": "connected",
                                    "a_terminations": [
                                        {
                                            "id": "interface-1",
                                            "name": "GigabitEthernet0/1",
                                            "device": {"id": "device-1", "name": "Device-1"},
                                        }
                                    ],
                                    "b_terminations": [
                                        {
                                            "id": "interface-2",
                                            "name": "GigabitEthernet0/2",
                                            "device": {"id": "device-2", "name": "Device-2"},
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }
}


class MockConnection:
    def __init__(self, kg_nodes: list[str], topo_nodes: list[tuple[str, str]]) -> None:
        self.kg_nodes = kg_nodes
        self.topo_nodes = topo_nodes
        self.fetch_calls = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        if len(args) > 1 and isinstance(args[1], list):
            active_list = args[1]
            allowed = []
            for label in active_list:
                if label in self.kg_nodes or any(label in edge for edge in self.topo_nodes):
                    allowed.append({"label": label})
            return allowed

        if "kg_nodes" in query:
            return [{"label": label} for label in self.kg_nodes]
        elif "topology_graph" in query:
            return [{"source_node_id": src, "target_node_id": tgt} for src, tgt in self.topo_nodes]
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "EXISTS" in query:
            anchor = args[1]
            in_kg = anchor in self.kg_nodes
            in_topo = any(anchor in edge for edge in self.topo_nodes)
            return in_kg or in_topo
        return None

    async def execute(self, query: str, *args: Any) -> str:
        return "SUCCESS"


@pytest.mark.anyio
class TestNetBoxGraphQLActivation:
    async def test_client_executes_query(self) -> None:
        mock_httpx = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json = MagicMock(return_value={"data": {}})
        mock_resp.raise_for_status = MagicMock()
        mock_httpx.post = AsyncMock(return_value=mock_resp)

        client = NetBoxGraphQLClient("http://netbox.local", "token123", mock_httpx)
        res = await client.execute_query(UNIFIED_TOPOLOGY_QUERY)

        assert res == {"data": {}}
        mock_httpx.post.assert_called_once()
        headers_passed = mock_httpx.post.call_args[1]["headers"]
        assert headers_passed["Authorization"] == "Token token123"

    def test_parse_topology_structure(self) -> None:
        adj = parse_topology(MOCK_TOPOLOGY_DATA)

        # Check Site-A links
        assert ("Rack-A1", 1.0) in adj["Site-A"]
        assert ("Device-2", 1.0) in adj["Site-A"]

        # Check Rack-A1 links
        assert ("Site-A", 1.0) in adj["Rack-A1"]
        assert ("Device-1", 1.0) in adj["Rack-A1"]

        # Check Device-1 links
        assert ("Device-1:GigabitEthernet0/1", 1.0) in adj["Device-1"]
        assert ("Rack-A1", 1.0) in adj["Device-1"]

        # Check Device-2 links
        assert ("Device-2:GigabitEthernet0/2", 1.0) in adj["Device-2"]
        assert ("Site-A", 1.0) in adj["Device-2"]

        # Check Cable link between interfaces
        assert ("Device-2:GigabitEthernet0/2", 1.0) in adj["Device-1:GigabitEthernet0/1"]
        assert ("Device-1:GigabitEthernet0/1", 1.0) in adj["Device-2:GigabitEthernet0/2"]

    async def test_pre_fetch_context_normal_severity(self) -> None:
        client_mock = MagicMock()
        client_mock.execute_query = AsyncMock(return_value=MOCK_TOPOLOGY_DATA)
        activator = GraphQLSpikingActivator(client_mock)

        # Normal telemetry severity: <= 8.0 (default parameters used)
        context = await activator.pre_fetch_context(
            anchor_label="Device-1",
            telemetry_severity=5.0,
            theta=0.5,
            decay=0.8,
            alpha=1.0,
            ticks=2,
        )

        assert "Device-1" in context
        # At severity 5.0, anchor potential starts at 1.0.
        # Step 1: Device-1 (potential 1.0 >= theta 0.5) fires. Pot resets to 0.0.
        # Transfer delta to Rack-A1 and Device-1:GigabitEthernet0/1 = 1.0 * alpha (1.0) * weight (1.0) = 1.0.
        # Step 2: Rack-A1 and Device-1:GigabitEthernet0/1 both fire (1.0 >= theta 0.5).
        # We expect Rack-A1, Device-1, and Device-1:GigabitEthernet0/1 to be in the active context.
        assert "Rack-A1" in context
        assert "Device-1:GigabitEthernet0/1" in context

    async def test_pre_fetch_context_severity_spike(self) -> None:
        client_mock = MagicMock()
        client_mock.execute_query = AsyncMock(return_value=MOCK_TOPOLOGY_DATA)
        activator = GraphQLSpikingActivator(client_mock)

        # Telemetry severity spike: > 8.0 (theta lowered to 0.25, charge raised to 2.0)
        context = await activator.pre_fetch_context(
            anchor_label="Device-1",
            telemetry_severity=9.0,
            theta=0.5,
            decay=0.85,
            alpha=1.0,
            ticks=2,
        )

        # Since theta is 0.25 and charge is 2.0, the charge propagates much further
        assert "Device-1" in context
        assert "Rack-A1" in context
        assert "Device-1:GigabitEthernet0/1" in context
        # Under spike, the activation should spread to Device-2 and beyond
        assert "Device-2:GigabitEthernet0/2" in context

    async def test_pre_fetch_context_rls_denied(self) -> None:
        client_mock = MagicMock()
        client_mock.execute_query = AsyncMock(return_value=MOCK_TOPOLOGY_DATA)
        activator = GraphQLSpikingActivator(client_mock)

        # Tenant is authorized for Device-2, but tries to start at Device-1 (unauthorized)
        conn = MockConnection(kg_nodes=["Device-2"], topo_nodes=[])
        context = await activator.pre_fetch_context(
            anchor_label="Device-1",
            telemetry_severity=5.0,
            conn=conn,
            namespace_id=NS,
        )

        # Anchor is unauthorized, return empty context
        assert context == set()

    async def test_pre_fetch_context_rls_authorized_filtering(self) -> None:
        client_mock = MagicMock()
        client_mock.execute_query = AsyncMock(return_value=MOCK_TOPOLOGY_DATA)
        activator = GraphQLSpikingActivator(client_mock)

        # Tenant is authorized for Device-1 and Device-1:GigabitEthernet0/1, but NOT Rack-A1
        conn = MockConnection(kg_nodes=["Device-1", "Device-1:GigabitEthernet0/1"], topo_nodes=[])
        context = await activator.pre_fetch_context(
            anchor_label="Device-1",
            telemetry_severity=5.0,
            conn=conn,
            namespace_id=NS,
        )

        # Result is filtered: Rack-A1 should be omitted because it's not in the authorized set
        assert "Device-1" in context
        assert "Device-1:GigabitEthernet0/1" in context
        assert "Rack-A1" not in context

    async def test_p95_execution_threshold_simulation(self) -> None:
        client_mock = MagicMock()
        client_mock.execute_query = AsyncMock(return_value=MOCK_TOPOLOGY_DATA)
        activator = GraphQLSpikingActivator(client_mock)

        # Measure times for multiple runs to ensure p95 execution is within 50ms
        run_times = []
        for _ in range(50):
            t_start = time.perf_counter()
            await activator.pre_fetch_context(
                anchor_label="Device-1",
                telemetry_severity=9.0,
                ticks=2,
            )
            run_times.append(time.perf_counter() - t_start)

        run_times.sort()
        p95_index = int(len(run_times) * 0.95)
        p95_time = run_times[p95_index] * 1000.0  # ms

        # p95 time must be strictly under 50ms (usually under 2ms for simulated parsing/stepping)
        assert p95_time < 50.0

    async def test_client_handles_graphql_errors(self) -> None:
        mock_httpx = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json = MagicMock(return_value={"errors": [{"message": "Invalid GraphQL query"}]})
        mock_resp.raise_for_status = MagicMock()
        mock_httpx.post = AsyncMock(return_value=mock_resp)

        client = NetBoxGraphQLClient("http://netbox.local", "token123", mock_httpx)
        with pytest.raises(ValueError) as excinfo:
            await client.execute_query("query { invalid }")

        assert "Invalid GraphQL query" in str(excinfo.value)
