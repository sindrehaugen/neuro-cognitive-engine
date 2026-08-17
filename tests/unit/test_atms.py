"""
tests/unit/test_atms.py
=======================
Unit tests for the ATMS (Assumption-Based Truth Maintenance System) module.
"""

from __future__ import annotations

import uuid

from nce.atms import ATMSEngine, ATMSNodeType, build_atms_from_causal_graph
from nce.causal.correlation import CausalGraph

# ---------------------------------------------------------------------------
# Fixture & Helpers
# ---------------------------------------------------------------------------

NS = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")


def _row(
    src: str,
    tgt: str,
    edge_type: str = "connected_to",
    confidence: float = 0.9,
) -> dict:
    """Minimal topology_graph row dict for testing."""
    return {
        "source_node_id": src,
        "source_node_type": "device",
        "target_node_id": tgt,
        "target_node_type": "service",
        "edge_type": edge_type,
        "confidence_score": confidence,
        "decay_coefficient": 0.001,
        "last_verified": None,
    }


def _graph(*edges: tuple[str, str, float], edge_type: str = "connected_to") -> CausalGraph:
    """Build a CausalGraph from (source, target, confidence) tuples."""
    rows = [_row(src, tgt, edge_type=edge_type, confidence=conf) for src, tgt, conf in edges]
    return CausalGraph.from_rows(rows, NS)


# ---------------------------------------------------------------------------
# 1. ATMSEngine basic node registration & evaluation
# ---------------------------------------------------------------------------


class TestATMSEngineBasics:
    def test_node_registration(self):
        engine = ATMSEngine()
        node = engine.register_node("A", ATMSNodeType.ASSUMPTION)
        assert node.node_id == "A"
        assert node.node_type == ATMSNodeType.ASSUMPTION
        assert node.is_valid is True

    def test_justification_creates_missing_nodes(self):
        engine = ATMSEngine()
        # "A" and "B" are not pre-registered; should be registered automatically
        engine.add_justification("B", {"A"})
        assert "A" in engine.nodes
        assert "B" in engine.nodes
        assert engine.nodes["A"].node_type == ATMSNodeType.ASSUMPTION
        assert engine.nodes["B"].node_type == ATMSNodeType.DERIVED

    def test_evaluate_premise_always_valid(self):
        engine = ATMSEngine()
        engine.register_node("P", ATMSNodeType.PREMISE, is_valid=False)
        engine.evaluate_belief_states()
        assert engine.nodes["P"].is_valid is True

    def test_evaluate_derived_requires_justification(self):
        engine = ATMSEngine()
        engine.register_node("A", ATMSNodeType.ASSUMPTION, is_valid=True)
        engine.register_node("D", ATMSNodeType.DERIVED)

        # Without justifications, D should default to False
        engine.evaluate_belief_states()
        assert engine.nodes["D"].is_valid is False

        # With valid justification, D becomes True
        engine.add_justification("D", {"A"})
        engine.evaluate_belief_states()
        assert engine.nodes["D"].is_valid is True

    def test_evaluate_and_justification(self):
        engine = ATMSEngine()
        engine.register_node("A", ATMSNodeType.ASSUMPTION, is_valid=True)
        engine.register_node("B", ATMSNodeType.ASSUMPTION, is_valid=False)
        engine.register_node("D", ATMSNodeType.DERIVED)

        # D depends on A AND B. Since B is False, D should be False.
        engine.add_justification("D", {"A", "B"})
        engine.evaluate_belief_states()
        assert engine.nodes["D"].is_valid is False

        # If B becomes True, D becomes True.
        engine.nodes["B"].is_valid = True
        engine.evaluate_belief_states()
        assert engine.nodes["D"].is_valid is True

    def test_evaluate_or_justifications(self):
        engine = ATMSEngine()
        engine.register_node("A", ATMSNodeType.ASSUMPTION, is_valid=True)
        engine.register_node("B", ATMSNodeType.ASSUMPTION, is_valid=False)
        engine.register_node("D", ATMSNodeType.DERIVED)

        # D has two justifications: A -> D and B -> D.
        # Since A is True, D should be True.
        engine.add_justification("D", {"A"})
        engine.add_justification("D", {"B"})
        engine.evaluate_belief_states()
        assert engine.nodes["D"].is_valid is True


# ---------------------------------------------------------------------------
# 2. Invalidation & Cascading Deprecation
# ---------------------------------------------------------------------------


class TestDeprecationCascade:
    def test_linear_cascade(self):
        engine = ATMSEngine()
        engine.register_node("A", ATMSNodeType.ASSUMPTION, is_valid=True)
        engine.add_justification("B", {"A"})
        engine.add_justification("C", {"B"})
        engine.evaluate_belief_states()

        assert engine.nodes["A"].is_valid is True
        assert engine.nodes["B"].is_valid is True
        assert engine.nodes["C"].is_valid is True

        # Invalidate A
        affected = engine.invalidate_assumption("A")
        assert "A" in affected
        assert "B" in affected
        assert "C" in affected

        assert engine.nodes["A"].is_valid is False
        assert engine.nodes["B"].is_valid is False
        assert engine.nodes["C"].is_valid is False

    def test_diamond_cascade_resilience(self):
        engine = ATMSEngine()
        engine.register_node("A1", ATMSNodeType.ASSUMPTION, is_valid=True)
        engine.register_node("A2", ATMSNodeType.ASSUMPTION, is_valid=True)

        # D depends on B1 and B2, which depend on A1 and A2 respectively
        engine.add_justification("B1", {"A1"})
        engine.add_justification("B2", {"A2"})
        engine.add_justification("D", {"B1"})
        engine.add_justification("D", {"B2"})
        engine.evaluate_belief_states()

        # Invalidate A1. D should still be supported by B2 (via A2).
        affected = engine.invalidate_assumption("A1")
        assert "A1" in affected
        assert "B1" in affected
        assert "D" not in affected

        assert engine.nodes["A1"].is_valid is False
        assert engine.nodes["B1"].is_valid is False
        assert engine.nodes["D"].is_valid is True

        # Invalidate A2. D should now lose all justifications and fall.
        affected2 = engine.invalidate_assumption("A2")
        assert "A2" in affected2
        assert "B2" in affected2
        assert "D" in affected2

        assert engine.nodes["D"].is_valid is False


# ---------------------------------------------------------------------------
# 3. Cycle Safety
# ---------------------------------------------------------------------------


class TestCycleSafety:
    def test_cycle_without_external_support(self):
        engine = ATMSEngine()
        engine.register_node("B", ATMSNodeType.DERIVED)
        engine.register_node("C", ATMSNodeType.DERIVED)

        # B depends on C; C depends on B
        engine.add_justification("B", {"C"})
        engine.add_justification("C", {"B"})

        engine.evaluate_belief_states()
        assert engine.nodes["B"].is_valid is False
        assert engine.nodes["C"].is_valid is False

    def test_cycle_with_external_support_and_cascade(self):
        engine = ATMSEngine()
        engine.register_node("A", ATMSNodeType.ASSUMPTION, is_valid=True)
        engine.register_node("B", ATMSNodeType.DERIVED)
        engine.register_node("C", ATMSNodeType.DERIVED)

        # Cycle B <-> C, but also A supports B
        engine.add_justification("B", {"C"})
        engine.add_justification("C", {"B"})
        engine.add_justification("B", {"A"})

        engine.evaluate_belief_states()
        assert engine.nodes["B"].is_valid is True
        assert engine.nodes["C"].is_valid is True

        # Invalidate A. The cycle should collapse.
        affected = engine.invalidate_assumption("A")
        assert "A" in affected
        assert "B" in affected
        assert "C" in affected

        assert engine.nodes["B"].is_valid is False
        assert engine.nodes["C"].is_valid is False


# ---------------------------------------------------------------------------
# 4. Contradictions & Nogoods
# ---------------------------------------------------------------------------


class TestContradictions:
    def test_contradiction_invalidation(self):
        engine = ATMSEngine()
        engine.register_node("A1", ATMSNodeType.ASSUMPTION, is_valid=True)
        engine.register_node("A2", ATMSNodeType.ASSUMPTION, is_valid=True)

        # Register contradiction between A1 and A2.
        # With "invalidate_a", A1 should be invalidated.
        affected = engine.register_contradiction("A1", "A2", resolution_strategy="invalidate_a")
        assert "A1" in affected
        assert "A2" not in affected

        assert engine.nodes["A1"].is_valid is False
        assert engine.nodes["A2"].is_valid is True


# ---------------------------------------------------------------------------
# 5. CausalGraph Integration
# ---------------------------------------------------------------------------


class TestCausalGraphIntegration:
    def test_build_atms_from_causal_graph(self):
        # Build causal graph: pdu --connected_to--> switch (FORWARD failure type)
        # server --depends_on--> switch (REVERSE failure type)
        g = _graph(
            ("pdu", "switch", 0.9),  # FORWARD
            ("server", "switch", 0.8),  # REVERSE (server depends_on switch)
            edge_type="connected_to",
        )

        # Override edge types to reflect mixed directions
        rows = [
            _row("pdu", "switch", edge_type="connected_to"),
            _row("server", "switch", edge_type="depends_on"),
        ]
        g = CausalGraph.from_rows(rows, NS)

        atms = build_atms_from_causal_graph(g)

        # Expected Nodes:
        # pdu: ASSUMPTION (root, nothing causes it)
        # switch: DERIVED (depends on pdu)
        # server: DERIVED (depends on switch)
        assert atms.nodes["pdu"].node_type == ATMSNodeType.ASSUMPTION
        assert atms.nodes["switch"].node_type == ATMSNodeType.DERIVED
        assert atms.nodes["server"].node_type == ATMSNodeType.DERIVED

        # Evaluate states
        atms.evaluate_belief_states()
        assert atms.nodes["pdu"].is_valid is True
        assert atms.nodes["switch"].is_valid is True
        assert atms.nodes["server"].is_valid is True

        # Invalidate pdu
        affected = atms.invalidate_assumption("pdu")
        assert "pdu" in affected
        assert "switch" in affected
        assert "server" in affected

        assert atms.nodes["switch"].is_valid is False
        assert atms.nodes["server"].is_valid is False


# ---------------------------------------------------------------------------
# 6. Tenant Scaling Simulation
# ---------------------------------------------------------------------------


class TestTenantScaling:
    def test_evaluate_300_tenants(self):
        # Simulate 300 active test tenants, each with a small dependency graph
        tenant_engines = {}
        for i in range(300):
            ns_id = uuid.uuid4()
            engine = ATMSEngine(namespace_id=ns_id)

            engine.register_node("power", ATMSNodeType.ASSUMPTION, is_valid=True)
            engine.add_justification(f"switch_{i}", {"power"})
            engine.add_justification(f"server_{i}", {f"switch_{i}"})

            engine.evaluate_belief_states()
            tenant_engines[ns_id] = engine

        # Validate all are initially True
        for i, (ns_id, engine) in enumerate(tenant_engines.items()):
            assert engine.nodes["power"].is_valid is True
            assert engine.nodes[f"switch_{i}"].is_valid is True
            assert engine.nodes[f"server_{i}"].is_valid is True

        # Invalidate power on tenant 100
        target_ns = list(tenant_engines.keys())[100]
        target_engine = tenant_engines[target_ns]

        affected = target_engine.invalidate_assumption("power")
        assert "power" in affected
        assert "switch_100" in affected
        assert "server_100" in affected

        assert target_engine.nodes["power"].is_valid is False
        assert target_engine.nodes["switch_100"].is_valid is False
        assert target_engine.nodes["server_100"].is_valid is False

        # Verify other tenants are isolated and remain valid
        for i, (ns_id, engine) in enumerate(tenant_engines.items()):
            if i != 100:
                assert engine.nodes["power"].is_valid is True
                assert engine.nodes[f"switch_{i}"].is_valid is True
                assert engine.nodes[f"server_{i}"].is_valid is True
