"""
tests/unit/test_neuromorphic.py
===============================
Unit tests for the neuromorphic spreading activation engine, telemetry triggers,
and synaptic weight adaptation.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from tests.fixtures.mock_db import MockConnection, MockPool

from nce.graph_query import (
    GraphEdge,
    GraphNode,
    GraphRAGTraverser,
    SpikingActivationEngine,
    Subgraph,
    adapt_synaptic_weights,
)

# ---------------------------------------------------------------------------
# 1. SpikingActivationEngine Unit Tests
# ---------------------------------------------------------------------------


class TestSpikingActivationEngine:
    def test_initialization(self) -> None:
        engine = SpikingActivationEngine(theta=0.6, decay=0.8, alpha=0.5)
        assert engine.theta == 0.6
        assert engine.decay == 0.8
        assert engine.alpha == 0.5
        assert len(engine.potentials) == 0
        assert len(engine.max_potentials) == 0
        assert len(engine.fired_nodes) == 0

    def test_single_step_firing_and_reset(self) -> None:
        engine = SpikingActivationEngine(theta=0.5, decay=0.8, alpha=1.0)
        engine.set_potentials({"node_A": 0.6, "node_B": 0.4})

        # Adjacency list: node_A -> node_B (weight 0.5)
        adj = {"node_A": [("node_B", 0.5)]}
        fired = engine.step(adj)

        assert fired == {"node_A"}
        assert "node_A" in engine.fired_nodes
        # node_A fired, so its potential reset to 0.0, decayed (still 0.0)
        assert engine.potentials["node_A"] == 0.0
        # node_B had 0.4, decayed to 0.4 * 0.8 = 0.32
        # Plus transfer from node_A: alpha * V_u * w = 1.0 * 0.6 * 0.5 = 0.3
        # Total node_B = 0.32 + 0.3 = 0.62
        assert pytest.approx(engine.potentials["node_B"]) == 0.62
        # max_potentials should track peak potential: node_A remains 0.6 (initial), node_B becomes 0.62 (new peak)
        assert engine.max_potentials["node_A"] == 0.6
        assert pytest.approx(engine.max_potentials["node_B"]) == 0.62

    def test_decay_without_firing(self) -> None:
        engine = SpikingActivationEngine(theta=1.0, decay=0.9, alpha=1.0)
        engine.set_potentials({"node_A": 0.5})

        adj: dict[str, list[tuple[str, float]]] = {}
        fired = engine.step(adj)

        assert fired == set()
        assert engine.potentials["node_A"] == pytest.approx(0.45)
        # max_potentials keeps the initial peak of 0.5
        assert engine.max_potentials["node_A"] == 0.5

    def test_historical_max_potentials_peak(self) -> None:
        engine = SpikingActivationEngine(theta=0.5, decay=0.5, alpha=1.0)
        engine.set_potentials({"node_A": 0.4})

        # In step 1, node_A does not fire, decays to 0.2
        engine.step({})
        assert engine.potentials["node_A"] == 0.2
        assert engine.max_potentials["node_A"] == 0.4  # Retains historical peak

        # In step 2, decays further to 0.1
        engine.step({})
        assert engine.potentials["node_A"] == 0.1
        assert engine.max_potentials["node_A"] == 0.4  # Still retains historical peak

    def test_potential_clamping_in_spiking_activation_engine(self) -> None:
        # Create engine with max_charge = 2.0
        engine = SpikingActivationEngine(theta=0.5, decay=1.0, alpha=1.0, max_charge=2.0)
        engine.set_potentials({"node_A": 1.0, "node_B": 0.4})

        # node_A has potential 1.0 (>= theta 0.5), so it fires.
        # It transfers to node_B: alpha * V_u * w = 1.0 * 1.0 * 2.0 = 2.0.
        # node_B potential after transfer: initial (0.4 * decay = 0.4) + transfer (2.0) = 2.4.
        # But max_charge is 2.0, so it should be clamped to 2.0.
        adj = {"node_A": [("node_B", 2.0)]}
        fired = engine.step(adj)

        assert fired == {"node_A"}
        assert engine.potentials["node_B"] == 2.0
        assert engine.max_potentials["node_B"] == 2.0


# ---------------------------------------------------------------------------
# 2. adapt_synaptic_weights Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestAdaptSynapticWeights:
    async def test_potentiation_on_success_3tuple(self) -> None:
        ns = uuid.uuid4()
        # Mock fetch returning confidence = 0.5 for kg_edges and confidence_score = 0.6 for topology_graph
        fetch_results = {
            "select id, confidence from kg_edges": [{"id": uuid.uuid4(), "confidence": 0.5}],
            "select id, confidence_score from topology_graph": [
                {"id": uuid.uuid4(), "confidence_score": 0.6}
            ],
        }
        conn = MockConnection(fetch_results)

        count = await adapt_synaptic_weights(
            conn=conn,
            namespace_id=ns,
            decision_outcome="success",
            reinforced_edges=[("device_A", "connected_to", "device_B")],
            learning_rate=0.2,
        )

        assert count == 2  # updated 1 kg_edge and 1 topology_graph edge
        # Check update queries were run with correct potentiated values:
        # kg_edges: 0.5 + 0.2 * (1.0 - 0.5) = 0.6
        # topology_graph: 0.6 + 0.2 * (1.0 - 0.6) = 0.68
        updates = [c for c in conn.execute_calls if "update" in c[0].lower()]
        assert len(updates) == 2

        kg_update = next(c for c in updates if "kg_edges" in c[0].lower())
        assert kg_update[1][0] == pytest.approx(0.6)

        topo_update = next(c for c in updates if "topology_graph" in c[0].lower())
        assert topo_update[1][0] == pytest.approx(0.68)

    async def test_depression_on_failure_2tuple(self) -> None:
        ns = uuid.uuid4()
        # Mock fetch returning confidence = 0.5 for kg_edges and confidence_score = 0.6 for topology_graph
        fetch_results = {
            "select id, confidence from kg_edges": [{"id": uuid.uuid4(), "confidence": 0.5}],
            "select id, confidence_score from topology_graph": [
                {"id": uuid.uuid4(), "confidence_score": 0.6}
            ],
        }
        conn = MockConnection(fetch_results)

        count = await adapt_synaptic_weights(
            conn=conn,
            namespace_id=ns,
            decision_outcome="failure",
            reinforced_edges=[("device_A", "device_B")],
            learning_rate=0.1,
        )

        assert count == 2
        # Check update queries were run with correct depressed values:
        # kg_edges: 0.5 - 0.1 * 0.5 = 0.45
        # topology_graph: 0.6 - 0.1 * 0.6 = 0.54
        updates = [c for c in conn.execute_calls if "update" in c[0].lower()]
        assert len(updates) == 2

        kg_update = next(c for c in updates if "kg_edges" in c[0].lower())
        assert kg_update[1][0] == pytest.approx(0.45)

        topo_update = next(c for c in updates if "topology_graph" in c[0].lower())
        assert topo_update[1][0] == pytest.approx(0.54)

    async def test_lock_not_available_handling(self) -> None:
        ns = uuid.uuid4()
        # conn.fetch raises LockNotAvailableError
        fetch_results = {
            "select id, confidence from kg_edges": asyncpg.LockNotAvailableError(
                "Lock wait timeout"
            ),
            "select id, confidence_score from topology_graph": asyncpg.LockNotAvailableError(
                "Lock wait timeout"
            ),
        }
        conn = MockConnection(fetch_results)

        # Should not raise exception
        count = await adapt_synaptic_weights(
            conn=conn,
            namespace_id=ns,
            decision_outcome="success",
            reinforced_edges=[("device_A", "device_B")],
            learning_rate=0.1,
        )
        assert count == 0

    async def test_topology_graph_valid_to_filter(self) -> None:
        ns = uuid.uuid4()
        fetch_results = {
            "select id, confidence from kg_edges": [{"id": uuid.uuid4(), "confidence": 0.5}],
            "select id, confidence_score from topology_graph": [
                {"id": uuid.uuid4(), "confidence_score": 0.6}
            ],
        }
        conn = MockConnection(fetch_results)

        await adapt_synaptic_weights(
            conn=conn,
            namespace_id=ns,
            decision_outcome="success",
            reinforced_edges=[("device_A", "device_B")],
            learning_rate=0.1,
        )

        # Verify that the fetch call for topology_graph had 'valid_to is null' in the query
        topo_fetch = next(c for c in conn.fetch_calls if "topology_graph" in c[0].lower())
        assert "valid_to is null" in topo_fetch[0].lower()

    async def test_adapt_synaptic_weights_invalid_namespace(self) -> None:
        conn = MockConnection()
        with pytest.raises(ValueError, match="namespace_id is required"):
            await adapt_synaptic_weights(
                conn=conn,
                namespace_id="",
                decision_outcome="success",
                reinforced_edges=[("device_A", "device_B")],
            )

    async def test_adapt_synaptic_weights_db_exception_propagation(self) -> None:
        ns = uuid.uuid4()
        # Mock database exception (not LockNotAvailableError) which must propagate
        fetch_results = {
            "select id, confidence from kg_edges": RuntimeError("DB query failed"),
        }
        conn = MockConnection(fetch_results)

        with pytest.raises(RuntimeError, match="DB query failed"):
            await adapt_synaptic_weights(
                conn=conn,
                namespace_id=ns,
                decision_outcome="success",
                reinforced_edges=[("device_A", "device_B")],
            )

    async def test_adapt_synaptic_weights_savepoint_isolation(self) -> None:
        ns = uuid.uuid4()
        fetch_results = {
            "select id, confidence from kg_edges": [{"id": uuid.uuid4(), "confidence": 0.5}],
            "select id, confidence_score from topology_graph": [
                {"id": uuid.uuid4(), "confidence_score": 0.6}
            ],
        }
        conn = MockConnection(fetch_results)

        count = await adapt_synaptic_weights(
            conn=conn,
            namespace_id=ns,
            decision_outcome="success",
            reinforced_edges=[("device_A", "device_B"), ("device_C", "device_D")],
            learning_rate=0.1,
        )

        assert count == 4
        # Verify savepoints were created (transaction entered once per edge loop iteration)
        assert conn.transaction_enters == 2
        assert conn.transaction_exits == 2

    async def test_bidirectional_weight_adaptation_with_pred(self) -> None:
        ns = uuid.uuid4()
        fetch_results = {
            "select id, confidence from kg_edges": [{"id": uuid.uuid4(), "confidence": 0.5}],
            "select id, confidence_score from topology_graph": [
                {"id": uuid.uuid4(), "confidence_score": 0.6}
            ],
        }
        conn = MockConnection(fetch_results)

        # 3-tuple edge (with predicate)
        count = await adapt_synaptic_weights(
            conn=conn,
            namespace_id=ns,
            decision_outcome="success",
            reinforced_edges=[("device_A", "connected_to", "device_B")],
            learning_rate=0.2,
        )

        assert count == 2
        # Verify that the fetch call for kg_edges used bidirectional / symmetrical matches
        kg_fetch = next(c for c in conn.fetch_calls if "kg_edges" in c[0].lower())
        q_kg_compact = "".join(kg_fetch[0].split()).lower()
        assert (
            "(subject_label=$3andobject_label=$4)or(subject_label=$4andobject_label=$3)"
            in q_kg_compact
        )

        # Verify that the fetch call for topology_graph used bidirectional / symmetrical matches
        topo_fetch = next(c for c in conn.fetch_calls if "topology_graph" in c[0].lower())
        q_topo_compact = "".join(topo_fetch[0].split()).lower()
        assert (
            "(source_node_id=$3andtarget_node_id=$4)or(source_node_id=$4andtarget_node_id=$3)"
            in q_topo_compact
        )

    async def test_bidirectional_weight_adaptation_without_pred(self) -> None:
        ns = uuid.uuid4()
        fetch_results = {
            "select id, confidence from kg_edges": [{"id": uuid.uuid4(), "confidence": 0.5}],
            "select id, confidence_score from topology_graph": [
                {"id": uuid.uuid4(), "confidence_score": 0.6}
            ],
        }
        conn = MockConnection(fetch_results)

        # 2-tuple edge (without predicate)
        count = await adapt_synaptic_weights(
            conn=conn,
            namespace_id=ns,
            decision_outcome="success",
            reinforced_edges=[("device_A", "device_B")],
            learning_rate=0.2,
        )

        assert count == 2
        # Verify that the fetch call for kg_edges used bidirectional / symmetrical matches without predicate
        kg_fetch = next(c for c in conn.fetch_calls if "kg_edges" in c[0].lower())
        q_kg_compact = "".join(kg_fetch[0].split()).lower()
        assert (
            "(subject_label=$2andobject_label=$3)or(subject_label=$3andobject_label=$2)"
            in q_kg_compact
        )

        # Verify that the fetch call for topology_graph used bidirectional / symmetrical matches without predicate
        topo_fetch = next(c for c in conn.fetch_calls if "topology_graph" in c[0].lower())
        q_topo_compact = "".join(topo_fetch[0].split()).lower()
        assert (
            "(source_node_id=$2andtarget_node_id=$3)or(source_node_id=$3andtarget_node_id=$2)"
            in q_topo_compact
        )


# ---------------------------------------------------------------------------
# 3. GraphRAGTraverser.neuromorphic_search Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestNeuromorphicSearch:
    async def test_neuromorphic_search_standard(self) -> None:
        ns = uuid.uuid4()
        conn = MockConnection()
        pool = MockPool(conn)
        mongo = MagicMock()

        # Mock embedding function returning simple vector
        async def embed_fn(q):
            return [0.1, 0.2]

        traverser = GraphRAGTraverser(
            pg_pool=pool,
            mongo_client=mongo,
            embedding_fn=embed_fn,
        )

        # Mock find_anchor and _bfs methods on the traverser
        traverser._find_anchor = AsyncMock(
            return_value=[
                GraphNode(label="switch_01", entity_type="device", payload_ref=None, distance=0.1)
            ]
        )

        # bfs returns visited_labels, edges
        traverser._bfs = AsyncMock(
            return_value=(
                {"switch_01", "router_02"},
                [
                    GraphEdge(
                        subject="switch_01",
                        predicate="connected_to",
                        obj="router_02",
                        confidence=0.8,
                        payload_ref=None,
                    )
                ],
            )
        )

        # Mock connection fetch for node metadata query in neuromorphic_search
        conn.fetch_results = {
            "select label, entity_type, payload_ref from kg_nodes": [
                {"label": "switch_01", "entity_type": "device", "payload_ref": None},
                {"label": "router_02", "entity_type": "device", "payload_ref": None},
            ]
        }

        # Mock _hydrate_sources
        traverser._hydrate_sources = AsyncMock(return_value=[])

        # Run neuromorphic search
        subgraph = await traverser.neuromorphic_search(
            query="switch status",
            namespace_id=str(ns),
            max_depth=2,
            theta=0.5,
            decay=0.85,
            alpha=1.0,
            telemetry_severity=5.0,  # telemetry normal
        )

        assert isinstance(subgraph, Subgraph)
        assert subgraph.anchor == "switch_01"
        # Node switch_01 fires (potential 1.0 >= theta 0.5), transfers to router_02:
        # router_02 potential becomes: alpha * 1.0 * 0.8 = 0.8.
        # router_02 will fire or have sub-threshold activation in step 2.
        # Both nodes should be active.
        node_labels = {n.label for n in subgraph.nodes}
        assert "switch_01" in node_labels
        assert "router_02" in node_labels
        assert len(subgraph.edges) == 1
        assert subgraph.edges[0].subject == "switch_01"
        assert subgraph.edges[0].obj == "router_02"

    async def test_neuromorphic_search_telemetry_severity_spike(self) -> None:
        ns = uuid.uuid4()
        conn = MockConnection()
        pool = MockPool(conn)
        mongo = MagicMock()

        async def embed_fn(q):
            return [0.1, 0.2]

        traverser = GraphRAGTraverser(
            pg_pool=pool,
            mongo_client=mongo,
            embedding_fn=embed_fn,
        )

        traverser._find_anchor = AsyncMock(
            return_value=[
                GraphNode(label="switch_01", entity_type="device", payload_ref=None, distance=0.1)
            ]
        )

        traverser._bfs = AsyncMock(
            return_value=(
                {"switch_01", "router_02", "host_03"},
                [
                    GraphEdge(
                        subject="switch_01",
                        predicate="connected_to",
                        obj="router_02",
                        confidence=0.4,
                        payload_ref=None,
                    ),
                    GraphEdge(
                        subject="router_02",
                        predicate="connected_to",
                        obj="host_03",
                        confidence=0.3,
                        payload_ref=None,
                    ),
                ],
            )
        )

        conn.fetch_results = {
            "select label, entity_type, payload_ref from kg_nodes": [
                {"label": "switch_01", "entity_type": "device", "payload_ref": None},
                {"label": "router_02", "entity_type": "device", "payload_ref": None},
                {"label": "host_03", "entity_type": "device", "payload_ref": None},
            ]
        }

        traverser._hydrate_sources = AsyncMock(return_value=[])

        # Run neuromorphic search with telemetry severity > 8 (severity = 9.0)
        # This will lower theta to 0.25 and raise initial charge to 2.0.
        subgraph = await traverser.neuromorphic_search(
            query="switch status",
            namespace_id=str(ns),
            max_depth=2,
            theta=0.5,  # standard theta 0.5 is overridden by 0.25
            decay=0.85,
            alpha=1.0,
            telemetry_severity=9.0,  # telemetry spike!
        )

        # Under severity spike:
        # Step 1: switch_01 potential starts at 2.0. Fires (2.0 >= 0.25).
        # Transfers to router_02: 1.0 * 2.0 * 0.4 = 0.8.
        # Step 2: router_02 potential is 0.8. Fires (0.8 >= 0.25).
        # Transfers to host_03: 1.0 * 0.8 * 0.3 = 0.24.
        # Since theta was lowered to 0.25, and sub-threshold is 0.025:
        # host_03 potential (0.24) is >= 0.025, so host_03 is active!
        # Thus switch_01, router_02, and host_03 should all be in the active nodes!
        node_labels = {n.label for n in subgraph.nodes}
        assert "switch_01" in node_labels
        assert "router_02" in node_labels
        assert "host_03" in node_labels

    async def test_neuromorphic_search_edge_unification(self) -> None:
        ns = uuid.uuid4()
        conn = MockConnection()
        pool = MockPool(conn)
        mongo = MagicMock()

        async def embed_fn(q):
            return [0.1, 0.2]

        traverser = GraphRAGTraverser(
            pg_pool=pool,
            mongo_client=mongo,
            embedding_fn=embed_fn,
        )

        traverser._find_anchor = AsyncMock(
            return_value=[
                GraphNode(label="switch_01", entity_type="device", payload_ref=None, distance=0.1)
            ]
        )

        # Parallel edges with different weights: should unify to max weight (0.8)
        traverser._bfs = AsyncMock(
            return_value=(
                {"switch_01", "router_02"},
                [
                    GraphEdge(
                        subject="switch_01",
                        predicate="connected_to",
                        obj="router_02",
                        confidence=0.4,
                        payload_ref=None,
                    ),
                    GraphEdge(
                        subject="switch_01",
                        predicate="hosts",
                        obj="router_02",
                        confidence=0.8,
                        payload_ref=None,
                    ),
                ],
            )
        )

        conn.fetch_results = {
            "select label, entity_type, payload_ref from kg_nodes": [
                {"label": "switch_01", "entity_type": "device", "payload_ref": None},
                {"label": "router_02", "entity_type": "device", "payload_ref": None},
            ]
        }

        traverser._hydrate_sources = AsyncMock(return_value=[])

        # Run neuromorphic search
        subgraph = await traverser.neuromorphic_search(
            query="switch status",
            namespace_id=str(ns),
            max_depth=1,
            theta=0.5,
            decay=0.85,
            alpha=1.0,
        )

        node_labels = {n.label for n in subgraph.nodes}
        assert "switch_01" in node_labels
        assert "router_02" in node_labels

    async def test_neuromorphic_search_custom_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nce.config import cfg

        monkeypatch.setattr(cfg, "NCE_TELEMETRY_SPIKE_THRESHOLD", 5.0)
        monkeypatch.setattr(cfg, "NCE_TELEMETRY_SPIKE_THETA", 0.1)
        monkeypatch.setattr(cfg, "NCE_TELEMETRY_SPIKE_CHARGE", 3.0)

        ns = uuid.uuid4()
        conn = MockConnection()
        pool = MockPool(conn)
        mongo = MagicMock()

        async def embed_fn(q):
            return [0.1, 0.2]

        traverser = GraphRAGTraverser(
            pg_pool=pool,
            mongo_client=mongo,
            embedding_fn=embed_fn,
        )

        traverser._find_anchor = AsyncMock(
            return_value=[
                GraphNode(label="switch_01", entity_type="device", payload_ref=None, distance=0.1)
            ]
        )

        traverser._bfs = AsyncMock(
            return_value=(
                {"switch_01", "router_02"},
                [
                    GraphEdge(
                        subject="switch_01",
                        predicate="connected_to",
                        obj="router_02",
                        confidence=0.2,
                        payload_ref=None,
                    ),
                ],
            )
        )

        conn.fetch_results = {
            "select label, entity_type, payload_ref from kg_nodes": [
                {"label": "switch_01", "entity_type": "device", "payload_ref": None},
                {"label": "router_02", "entity_type": "device", "payload_ref": None},
            ]
        }

        traverser._hydrate_sources = AsyncMock(return_value=[])

        # Run neuromorphic search with telemetry_severity = 6.0 (> threshold 5.0)
        subgraph = await traverser.neuromorphic_search(
            query="switch status",
            namespace_id=str(ns),
            max_depth=1,
            theta=0.5,
            decay=0.85,
            alpha=1.0,
            telemetry_severity=6.0,
        )

        node_labels = {n.label for n in subgraph.nodes}
        assert "switch_01" in node_labels
        assert "router_02" in node_labels

    async def test_neuromorphic_search_decayed_node_retention(self) -> None:
        ns = uuid.uuid4()
        conn = MockConnection()
        pool = MockPool(conn)
        mongo = MagicMock()

        async def embed_fn(q):
            return [0.1, 0.2]

        traverser = GraphRAGTraverser(
            pg_pool=pool,
            mongo_client=mongo,
            embedding_fn=embed_fn,
        )

        traverser._find_anchor = AsyncMock(
            return_value=[
                GraphNode(label="switch_01", entity_type="device", payload_ref=None, distance=0.1)
            ]
        )

        # switch_01 connected to router_02. With high decay (e.g. 0.2) and max_depth = 3,
        # router_02 potential will decay to:
        # Step 1: switch_01 fires (potential 1.0). router_02 potential = alpha (1.0) * 1.0 * 0.4 = 0.4.
        # Step 2: router_02 decays to 0.4 * 0.2 = 0.08.
        # Step 3: router_02 decays to 0.08 * 0.2 = 0.016.
        # Threshold is theta = 0.5. sub_threshold = 0.05.
        # At final tick, router_02 potential (0.016) is way below sub_threshold (0.05).
        # But its peak potential was 0.4 (which is >= 0.05).
        # It must be retained because of max_potentials tracking.
        traverser._bfs = AsyncMock(
            return_value=(
                {"switch_01", "router_02"},
                [
                    GraphEdge(
                        subject="switch_01",
                        predicate="connected_to",
                        obj="router_02",
                        confidence=0.4,
                        payload_ref=None,
                    ),
                ],
            )
        )

        conn.fetch_results = {
            "select label, entity_type, payload_ref from kg_nodes": [
                {"label": "switch_01", "entity_type": "device", "payload_ref": None},
                {"label": "router_02", "entity_type": "device", "payload_ref": None},
            ]
        }

        traverser._hydrate_sources = AsyncMock(return_value=[])

        # Run neuromorphic search with max_depth=3 and decay=0.2
        subgraph = await traverser.neuromorphic_search(
            query="switch status",
            namespace_id=str(ns),
            max_depth=3,
            theta=0.5,
            decay=0.2,
            alpha=1.0,
        )

        node_labels = {n.label for n in subgraph.nodes}
        assert "switch_01" in node_labels
        # router_02 must be retained despite decay
        assert "router_02" in node_labels
