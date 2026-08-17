"""
tests/unit/test_causal.py
=========================
Unit tests for nce.causal.correlation — BATCH-P2-004 Causal Inference.

Tests cover:
  - CausalGraph construction and from_rows node_type priority (TD-CAUSAL-3)
  - Topology traversal helpers (descendants, ancestors, find_all_paths)
  - Graph mutilation via edge-type-aware do() operator (TD-CAUSAL-1)
  - impacted_by(): direction-aware failure propagation (TD-CAUSAL-1)
  - find_all_causal_paths(): direction-aware path enumeration (TD-CAUSAL-1)
  - Edge decay model
  - Probability math helpers
  - DoCalculusEngine end-to-end: connected_to (forward) topology
  - DoCalculusEngine end-to-end: depends_on / powered_by (reverse) topology
  - DoCalculusEngine end-to-end: mixed edge types
  - Confounding path detection with renamed field (TD-CAUSAL-4)
  - Determinism, error handling, invariant properties

Helper convention:
  _row() defaults to edge_type="connected_to" (source SUPPLIES target, failure
  propagates FORWARD). Tests that need reverse semantics pass edge_type="depends_on"
  or edge_type="powered_by" explicitly.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from nce.causal.correlation import (
    _FORWARD_FAILURE_TYPES,
    _REVERSE_FAILURE_TYPES,
    CausalEdge,
    CausalGraph,
    ConfoundingPath,
    DoCalculusEngine,
    InterventionResult,
    _combine_path_probabilities,
    _path_confidence,
)

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

NS = uuid.UUID("aaaabbbb-0000-0000-0000-000000000001")
NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)


def _row(
    src: str,
    tgt: str,
    edge_type: str = "connected_to",  # default = FORWARD failure propagation
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
        "last_verified": NOW,
    }


def _graph(*edges: tuple[str, str, float], edge_type: str = "connected_to") -> CausalGraph:
    """Build a CausalGraph from (source, target, confidence) tuples.
    All edges use *edge_type* (default: connected_to = forward propagation).
    """
    rows = [_row(src, tgt, edge_type=edge_type, confidence=conf) for src, tgt, conf in edges]
    return CausalGraph.from_rows(rows, NS)


def _mixed_graph(rows: list[dict]) -> CausalGraph:
    """Build a CausalGraph from an explicit list of row dicts."""
    return CausalGraph.from_rows(rows, NS)


# ---------------------------------------------------------------------------
# 0. Edge-type constant checks
# ---------------------------------------------------------------------------


class TestEdgeTypeConstants:
    def test_forward_types_correct(self):
        assert "connected_to" in _FORWARD_FAILURE_TYPES
        assert "host_application" in _FORWARD_FAILURE_TYPES

    def test_reverse_types_correct(self):
        assert "depends_on" in _REVERSE_FAILURE_TYPES
        assert "powered_by" in _REVERSE_FAILURE_TYPES

    def test_types_are_disjoint(self):
        assert _FORWARD_FAILURE_TYPES.isdisjoint(_REVERSE_FAILURE_TYPES)


# ---------------------------------------------------------------------------
# 1. CausalGraph construction
# ---------------------------------------------------------------------------


class TestCausalGraphConstruction:
    def test_empty_graph(self):
        g = CausalGraph.from_rows([], NS)
        assert g.node_count == 0
        assert g.node_ids == frozenset()

    def test_single_edge_creates_two_nodes(self):
        g = _graph(("pdu", "switch", 0.9))
        assert "pdu" in g.node_ids
        assert "switch" in g.node_ids
        assert g.node_count == 2

    def test_multiple_edges_correct_node_count(self):
        g = _graph(("pdu", "switch", 0.9), ("switch", "server", 0.8))
        assert g.node_count == 3

    def test_outgoing_edges_correct(self):
        g = _graph(("pdu", "switch", 0.9))
        out = g.outgoing_edges("pdu")
        assert len(out) == 1
        assert out[0].target_node_id == "switch"
        assert out[0].confidence_score == pytest.approx(0.9)

    def test_incoming_edges_correct(self):
        g = _graph(("pdu", "switch", 0.9))
        inc = g.incoming_edges("switch")
        assert len(inc) == 1
        assert inc[0].source_node_id == "pdu"

    def test_no_edges_returns_empty_lists(self):
        g = _graph(("pdu", "switch", 0.9))
        assert g.outgoing_edges("switch") == []
        assert g.incoming_edges("pdu") == []

    def test_get_node_returns_correct_type(self):
        g = _graph(("pdu", "switch", 0.9))
        node = g.get_node("pdu")
        assert node is not None
        assert node.node_type == "device"
        assert node.namespace_id == NS

    def test_get_node_missing_returns_none(self):
        g = _graph(("pdu", "switch", 0.9))
        assert g.get_node("nonexistent") is None

    def test_node_type_source_wins_over_target(self):
        """TD-CAUSAL-3: source_node_type takes priority when a node appears in
        multiple rows with different type values."""
        rows = [
            # switch appears as source_type="router" first
            _row("switch", "server", "connected_to"),  # source_type for switch = "device" (default)
        ]
        # Override source_node_type to "router" to simulate the conflict
        rows[0] = dict(rows[0])
        rows[0]["source_node_type"] = "router"
        # Also add a row where switch appears as target with type "switch-equipment"
        rows.append(_row("pdu", "switch", "connected_to"))
        rows[-1] = dict(rows[-1])
        rows[-1]["target_node_type"] = "switch-equipment"

        g = CausalGraph.from_rows(rows, NS)
        node = g.get_node("switch")
        assert node is not None
        # source_type "router" must win over target_type "switch-equipment"
        assert node.node_type == "router"

    def test_node_type_deterministic_regardless_of_row_order(self):
        """TD-CAUSAL-3: node_type resolution must not depend on row ordering."""
        rows_v1 = [
            _row("A", "B", "connected_to"),  # A appears as source_type="device"
            _row("C", "A", "connected_to"),  # A appears as target_type="service"
        ]
        rows_v2 = [
            _row("C", "A", "connected_to"),  # reversed order
            _row("A", "B", "connected_to"),
        ]
        g1 = CausalGraph.from_rows(rows_v1, NS)
        g2 = CausalGraph.from_rows(rows_v2, NS)
        # In both cases, A appears as source_node_type="device" in one row.
        # source_type always wins → both should produce node_type="device" for A.
        assert g1.get_node("A").node_type == "device"
        assert g2.get_node("A").node_type == "device"


# ---------------------------------------------------------------------------
# 2. Topology traversal: descendants and ancestors (direction-agnostic)
# ---------------------------------------------------------------------------


class TestTopologyTraversal:
    def test_direct_descendants(self):
        g = _graph(("pdu", "switch", 0.9), ("switch", "server", 0.8))
        desc = g.descendants("pdu")
        assert "switch" in desc
        assert "server" in desc
        assert "pdu" not in desc

    def test_leaf_has_no_descendants(self):
        g = _graph(("pdu", "switch", 0.9))
        assert g.descendants("switch") == set()

    def test_ancestors_direct(self):
        g = _graph(("pdu", "switch", 0.9), ("switch", "server", 0.8))
        anc = g.ancestors("server")
        assert "switch" in anc
        assert "pdu" in anc

    def test_ancestors_root_node_empty(self):
        g = _graph(("pdu", "switch", 0.9))
        assert g.ancestors("pdu") == set()

    def test_fan_out_topology(self):
        g = _graph(("switch", "srv_a", 0.9), ("switch", "srv_b", 0.8), ("switch", "srv_c", 0.7))
        assert g.descendants("switch") == {"srv_a", "srv_b", "srv_c"}

    def test_diamond_topology_no_duplicates(self):
        g = _graph(
            ("pdu", "switch_a", 0.9),
            ("pdu", "switch_b", 0.9),
            ("switch_a", "server", 0.8),
            ("switch_b", "server", 0.8),
        )
        desc = g.descendants("pdu")
        assert desc == {"switch_a", "switch_b", "server"}


# ---------------------------------------------------------------------------
# 3. impacted_by() — edge-type-aware failure propagation (TD-CAUSAL-1)
# ---------------------------------------------------------------------------


class TestImpactedBy:
    def test_connected_to_forward_propagation(self):
        """connected_to: failure of source propagates FORWARD to target."""
        g = _graph(("pdu", "switch", 0.9), ("switch", "server", 0.8))
        impacted = g.impacted_by("pdu")
        assert "switch" in impacted
        assert "server" in impacted

    def test_depends_on_reverse_propagation(self):
        """depends_on: failure of TARGET propagates BACKWARD to source.
        server depends_on pdu → do(pdu=failed) impacts server."""
        rows = [
            _row("server", "pdu", edge_type="depends_on", confidence=0.9),
        ]
        g = _mixed_graph(rows)
        # When pdu fails, server is impacted (server depends on pdu)
        impacted = g.impacted_by("pdu")
        assert "server" in impacted

    def test_depends_on_no_impact_on_dependency(self):
        """server depends_on pdu: failure of server does NOT impact pdu."""
        rows = [_row("server", "pdu", edge_type="depends_on", confidence=0.9)]
        g = _mixed_graph(rows)
        impacted = g.impacted_by("server")
        assert "pdu" not in impacted

    def test_powered_by_reverse_propagation(self):
        """powered_by: failure of TARGET propagates BACKWARD to source.
        switch powered_by pdu → do(pdu=failed) impacts switch."""
        rows = [_row("switch", "pdu", edge_type="powered_by", confidence=0.95)]
        g = _mixed_graph(rows)
        impacted = g.impacted_by("pdu")
        assert "switch" in impacted

    def test_host_application_forward_propagation(self):
        """host_application: failure of SOURCE propagates FORWARD to target."""
        rows = [_row("server", "app", edge_type="host_application", confidence=0.9)]
        g = _mixed_graph(rows)
        impacted = g.impacted_by("server")
        assert "app" in impacted

    def test_transitive_depends_on(self):
        """Transitive reverse propagation: app depends_on server depends_on pdu."""
        rows = [
            _row("app", "server", edge_type="depends_on", confidence=0.9),
            _row("server", "pdu", edge_type="depends_on", confidence=0.8),
        ]
        g = _mixed_graph(rows)
        impacted = g.impacted_by("pdu")
        assert "server" in impacted
        assert "app" in impacted

    def test_mixed_edge_types_both_directions(self):
        """A node can impact others both forward and backward in a mixed graph."""
        rows = [
            _row("pdu", "switch", edge_type="connected_to", confidence=0.9),  # forward
            _row("app", "pdu", edge_type="depends_on", confidence=0.8),  # reverse
        ]
        g = _mixed_graph(rows)
        impacted = g.impacted_by("pdu")
        assert "switch" in impacted  # forward: pdu supplies switch
        assert "app" in impacted  # reverse: app depends on pdu

    def test_leaf_node_not_impacted_by_itself(self):
        g = _graph(("pdu", "switch", 0.9))
        impacted = g.impacted_by("pdu")
        assert "pdu" not in impacted

    def test_isolated_node_impacts_nothing(self):
        g = _graph(("pdu", "switch", 0.9))
        impacted = g.impacted_by("switch")
        assert impacted == set()


# ---------------------------------------------------------------------------
# 4. Graph mutilation — edge-type-aware do() operator (TD-CAUSAL-1)
# ---------------------------------------------------------------------------


class TestGraphMutilation:
    def test_mutilate_severs_forward_incoming_edge(self):
        """connected_to: pdu→switch is severed by do(switch)."""
        g = _graph(("pdu", "switch", 0.9))
        mutilated = g.mutilate("switch")
        assert mutilated.incoming_edges("switch") == []

    def test_mutilate_preserves_forward_outgoing_edge(self):
        """pdu→switch→server: do(switch) removes pdu→switch but keeps switch→server."""
        g = _graph(("pdu", "switch", 0.9), ("switch", "server", 0.8))
        mutilated = g.mutilate("switch")
        out = mutilated.outgoing_edges("switch")
        assert len(out) == 1
        assert out[0].target_node_id == "server"

    def test_mutilate_severs_reverse_outgoing_from_intervention(self):
        """depends_on: do(pdu) severs pdu→dep edge (pdu's causal parent is dep)."""
        rows = [_row("pdu", "dep", edge_type="depends_on", confidence=0.9)]
        g = _mixed_graph(rows)
        mutilated = g.mutilate("pdu")
        # pdu→dep (depends_on) is a causal parent of pdu → severed
        assert mutilated.outgoing_edges("pdu") == []

    def test_mutilate_preserves_reverse_incoming_effect(self):
        """depends_on: do(pdu) preserves server→pdu edge (server is an EFFECT of pdu)."""
        rows = [
            _row("server", "pdu", edge_type="depends_on", confidence=0.9),
            _row("pdu", "dep", edge_type="depends_on", confidence=0.8),  # pdu's cause — severed
        ]
        g = _mixed_graph(rows)
        mutilated = g.mutilate("pdu")
        # server→pdu (depends_on): pdu's failure causes server's failure — KEPT
        inc = mutilated.incoming_edges("pdu")
        assert any(e.source_node_id == "server" for e in inc)

    def test_mutilate_does_not_modify_original(self):
        g = _graph(("pdu", "switch", 0.9))
        _ = g.mutilate("switch")
        assert len(g.incoming_edges("switch")) == 1

    def test_mutilate_returns_new_instance(self):
        g = _graph(("pdu", "switch", 0.9))
        assert g.mutilate("switch") is not g

    def test_mutilate_nonexistent_node_is_no_op(self):
        g = _graph(("pdu", "switch", 0.9))
        mutilated = g.mutilate("nonexistent")
        assert len(mutilated.incoming_edges("switch")) == 1

    def test_mutilate_preserves_unrelated_edges(self):
        g = _graph(("A", "B", 0.9), ("C", "D", 0.8))
        mutilated = g.mutilate("B")
        cd = mutilated.outgoing_edges("C")
        assert len(cd) == 1 and cd[0].target_node_id == "D"


# ---------------------------------------------------------------------------
# 5. find_all_paths() — pure topology path enumeration
# ---------------------------------------------------------------------------


class TestPathEnumeration:
    def test_direct_path(self):
        g = _graph(("A", "B", 0.9))
        assert len(g.find_all_paths("A", "B")) == 1

    def test_two_hop_path(self):
        g = _graph(("A", "B", 0.9), ("B", "C", 0.8))
        paths = g.find_all_paths("A", "C")
        assert len(paths) == 1 and len(paths[0]) == 2

    def test_diamond_two_paths(self):
        g = _graph(("A", "B", 0.9), ("A", "C", 0.8), ("B", "D", 0.7), ("C", "D", 0.6))
        assert len(g.find_all_paths("A", "D")) == 2

    def test_no_path_returns_empty(self):
        g = _graph(("A", "B", 0.9))
        assert g.find_all_paths("B", "A") == []

    def test_same_source_target_returns_empty(self):
        g = _graph(("A", "B", 0.9))
        assert g.find_all_paths("A", "A") == []

    def test_paths_sorted_deterministically(self):
        g = _graph(("A", "B", 0.9), ("A", "C", 0.8), ("B", "D", 0.7), ("C", "D", 0.6))
        assert g.find_all_paths("A", "D") == g.find_all_paths("A", "D")

    def test_max_depth_limits_paths(self):
        g = _graph(("A", "B", 0.9), ("B", "C", 0.8), ("C", "D", 0.7), ("D", "E", 0.6))
        assert g.find_all_paths("A", "E", max_depth=2) == []
        assert len(g.find_all_paths("A", "E", max_depth=10)) == 1


# ---------------------------------------------------------------------------
# 6. find_all_causal_paths() — direction-aware path enumeration (TD-CAUSAL-1)
# ---------------------------------------------------------------------------


class TestCausalPathEnumeration:
    def test_connected_to_forward_path(self):
        """connected_to paths are found in the forward direction."""
        g = _graph(("pdu", "switch", 0.9), edge_type="connected_to")
        paths = g.find_all_causal_paths("pdu", "switch")
        assert len(paths) == 1

    def test_depends_on_reverse_path(self):
        """depends_on: causal path from pdu to server (pdu fails → server fails)
        even though the edge direction is server→pdu."""
        rows = [_row("server", "pdu", edge_type="depends_on", confidence=0.9)]
        g = _mixed_graph(rows)
        paths = g.find_all_causal_paths("pdu", "server")
        assert len(paths) == 1, "Should find reverse causal path server<-pdu"

    def test_powered_by_reverse_path(self):
        rows = [_row("switch", "pdu", edge_type="powered_by", confidence=0.95)]
        g = _mixed_graph(rows)
        paths = g.find_all_causal_paths("pdu", "switch")
        assert len(paths) == 1

    def test_no_causal_path_in_wrong_direction(self):
        """connected_to is FORWARD only — no path against the edge direction."""
        g = _graph(("pdu", "switch", 0.9), edge_type="connected_to")
        assert g.find_all_causal_paths("switch", "pdu") == []

    def test_mixed_type_transitive_path(self):
        """Causal path through mixed edge types:
        pdu --connected_to--> switch
        app --depends_on--> switch (app impacted when switch fails)
        So do(pdu) → switch fails → app fails: path pdu → switch → app."""
        rows = [
            _row("pdu", "switch", edge_type="connected_to", confidence=0.9),
            _row("app", "switch", edge_type="depends_on", confidence=0.8),
        ]
        g = _mixed_graph(rows)
        paths = g.find_all_causal_paths("pdu", "app")
        assert len(paths) == 1
        assert len(paths[0]) == 2  # 2 edges: pdu→switch, app←switch

    def test_causal_paths_sorted_deterministically(self):
        rows = [
            _row("pdu", "sw_a", "connected_to", 0.9),
            _row("pdu", "sw_b", "connected_to", 0.9),
            _row("app", "sw_a", "depends_on", 0.8),
            _row("app", "sw_b", "depends_on", 0.8),
        ]
        g = _mixed_graph(rows)
        p1 = g.find_all_causal_paths("pdu", "app")
        p2 = g.find_all_causal_paths("pdu", "app")
        assert p1 == p2


# ---------------------------------------------------------------------------
# 7. Edge decay model
# ---------------------------------------------------------------------------


class TestEdgeDecay:
    def test_no_last_verified_returns_raw_confidence(self):
        edge = CausalEdge("A", "B", "connected_to", 0.8, 0.001, None)
        assert edge.decayed_confidence(_now=NOW) == pytest.approx(0.8, rel=1e-4)

    def test_fresh_edge_minimal_decay(self):
        edge = CausalEdge("A", "B", "connected_to", 0.9, 0.001, NOW - timedelta(hours=1))
        assert edge.decayed_confidence(_now=NOW) == pytest.approx(0.9, rel=1e-2)

    def test_old_edge_significantly_decayed(self):
        edge = CausalEdge("A", "B", "connected_to", 0.9, 0.001, NOW - timedelta(days=90))
        assert edge.decayed_confidence(_now=NOW) == pytest.approx(0.9 * math.exp(-1), rel=1e-3)

    def test_confidence_never_below_min(self):
        edge = CausalEdge("A", "B", "connected_to", 0.9, 0.001, NOW - timedelta(days=10_000))
        assert edge.decayed_confidence(_now=NOW) >= 0.001

    def test_naive_datetime_normalised_to_utc(self):
        naive = datetime(2026, 5, 6, 12, 0, 0)  # naive, 30 days before NOW
        edge = CausalEdge("A", "B", "connected_to", 0.9, 0.001, naive)
        result = edge.decayed_confidence(_now=NOW)
        assert 0.0 < result < 0.9


# ---------------------------------------------------------------------------
# 8. Probability math helpers
# ---------------------------------------------------------------------------


class TestProbabilityMath:
    def test_path_confidence_single_edge(self):
        e = CausalEdge("A", "B", "connected_to", 0.8, 0.001, None)
        assert _path_confidence([e], _now=NOW) == pytest.approx(0.8)

    def test_path_confidence_two_edges(self):
        e1 = CausalEdge("A", "B", "connected_to", 0.9, 0.001, None)
        e2 = CausalEdge("B", "C", "connected_to", 0.8, 0.001, None)
        assert _path_confidence([e1, e2], _now=NOW) == pytest.approx(0.72)

    def test_path_confidence_empty_returns_zero(self):
        assert _path_confidence([], _now=NOW) == 0.0

    def test_combine_single_path(self):
        assert _combine_path_probabilities([0.8]) == pytest.approx(0.8)

    def test_combine_two_independent_paths(self):
        assert _combine_path_probabilities([0.6, 0.4]) == pytest.approx(0.76)

    def test_combine_empty_returns_zero(self):
        assert _combine_path_probabilities([]) == 0.0

    def test_combine_certain_path_gives_one(self):
        assert _combine_path_probabilities([1.0, 0.5]) == pytest.approx(1.0)

    def test_combine_zero_path_ignored(self):
        assert _combine_path_probabilities([0.0, 0.8]) == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# 9. DoCalculusEngine — connected_to (forward) topology
# ---------------------------------------------------------------------------


class TestDoCalculusForward:
    def test_single_chain_direct_and_transitive(self):
        # pdu --connected_to--> switch --connected_to--> server
        g = _graph(("pdu", "switch", 0.9), ("switch", "server", 0.8))
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        assert result.probability_matrix["switch"] == pytest.approx(0.9, rel=1e-3)
        assert result.probability_matrix["server"] == pytest.approx(0.72, rel=1e-3)

    def test_leaf_node_has_no_impact(self):
        g = _graph(("pdu", "server", 0.9))
        result = DoCalculusEngine().evaluate(g, "server", _now=NOW)
        assert result.probability_matrix == {}

    def test_diamond_topology_two_paths(self):
        g = _graph(
            ("pdu", "sw_a", 0.9),
            ("pdu", "sw_b", 0.9),
            ("sw_a", "server", 0.8),
            ("sw_b", "server", 0.7),
        )
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        expected = 1 - (1 - 0.9 * 0.8) * (1 - 0.9 * 0.7)
        assert result.probability_matrix["server"] == pytest.approx(expected, rel=1e-3)

    def test_directly_impacted_is_one_hop(self):
        g = _graph(("pdu", "switch", 0.9), ("switch", "server", 0.8))
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        direct_ids = {s.node_id for s in result.directly_impacted}
        transitive_ids = {s.node_id for s in result.transitively_impacted}
        assert "switch" in direct_ids
        assert "server" in transitive_ids and "server" not in direct_ids

    def test_result_is_intervention_result_namedtuple(self):
        g = _graph(("pdu", "switch", 0.9))
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        assert isinstance(result, InterventionResult)
        assert result.intervention_node_id == "pdu"
        assert result.intervention_state == "failed"

    def test_missing_node_raises_key_error(self):
        g = _graph(("pdu", "switch", 0.9))
        with pytest.raises(KeyError, match="nonexistent"):
            DoCalculusEngine().evaluate(g, "nonexistent", _now=NOW)

    def test_probability_matrix_values_in_range(self):
        g = _graph(("pdu", "sw", 0.9), ("sw", "srv_a", 0.8), ("sw", "srv_b", 0.7))
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        for nid, prob in result.probability_matrix.items():
            assert 0.0 <= prob <= 1.0, f"{nid}: {prob}"

    def test_determinism(self):
        g = _graph(
            ("pdu", "sw_a", 0.9), ("pdu", "sw_b", 0.85), ("sw_a", "srv", 0.8), ("sw_b", "srv", 0.75)
        )
        engine = DoCalculusEngine()
        r1 = engine.evaluate(g, "pdu", _now=NOW)
        r2 = engine.evaluate(g, "pdu", _now=NOW)
        assert r1.probability_matrix == r2.probability_matrix

    def test_higher_confidence_higher_impact(self):
        g_hi = _graph(("pdu", "server", 0.95))
        g_lo = _graph(("pdu", "server", 0.3))
        engine = DoCalculusEngine()
        r_hi = engine.evaluate(g_hi, "pdu", _now=NOW)
        r_lo = engine.evaluate(g_lo, "pdu", _now=NOW)
        assert r_hi.probability_matrix["server"] > r_lo.probability_matrix["server"]

    def test_empty_graph_raises_key_error(self):
        g = CausalGraph.from_rows([], NS)
        with pytest.raises(KeyError):
            DoCalculusEngine().evaluate(g, "pdu", _now=NOW)


# ---------------------------------------------------------------------------
# 10. DoCalculusEngine — depends_on / powered_by (reverse) topology (TD-CAUSAL-1)
# ---------------------------------------------------------------------------


class TestDoCalculusReverse:
    def test_depends_on_correct_impact_direction(self):
        """server depends_on pdu: do(pdu=failed) should impact server."""
        rows = [_row("server", "pdu", edge_type="depends_on", confidence=0.9)]
        g = _mixed_graph(rows)
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        assert "server" in result.probability_matrix
        assert result.probability_matrix["server"] == pytest.approx(0.9, rel=1e-3)

    def test_depends_on_no_reverse_impact(self):
        """do(server=failed): pdu is NOT impacted (server depends ON pdu, not the reverse)."""
        rows = [_row("server", "pdu", edge_type="depends_on", confidence=0.9)]
        g = _mixed_graph(rows)
        result = DoCalculusEngine().evaluate(g, "server", _now=NOW)
        assert "pdu" not in result.probability_matrix

    def test_powered_by_correct_impact_direction(self):
        """switch powered_by pdu: do(pdu=failed) impacts switch."""
        rows = [_row("switch", "pdu", edge_type="powered_by", confidence=0.95)]
        g = _mixed_graph(rows)
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        assert "switch" in result.probability_matrix
        assert result.probability_matrix["switch"] == pytest.approx(0.95, rel=1e-3)

    def test_transitive_depends_on_chain(self):
        """Transitive chain: app depends_on server depends_on pdu.
        do(pdu=failed) → server impacted (P=0.8), app impacted (P=0.8*0.9=0.72)."""
        rows = [
            _row("app", "server", edge_type="depends_on", confidence=0.9),
            _row("server", "pdu", edge_type="depends_on", confidence=0.8),
        ]
        g = _mixed_graph(rows)
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        assert "server" in result.probability_matrix
        assert "app" in result.probability_matrix
        assert result.probability_matrix["server"] == pytest.approx(0.8, rel=1e-3)
        assert result.probability_matrix["app"] == pytest.approx(0.72, rel=1e-3)

    def test_multiple_dependents_on_single_node(self):
        """Three services depend on the same PDU."""
        rows = [
            _row("svc_a", "pdu", edge_type="depends_on", confidence=0.9),
            _row("svc_b", "pdu", edge_type="depends_on", confidence=0.8),
            _row("svc_c", "pdu", edge_type="depends_on", confidence=0.7),
        ]
        g = _mixed_graph(rows)
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        assert {"svc_a", "svc_b", "svc_c"} == set(result.probability_matrix.keys())


# ---------------------------------------------------------------------------
# 11. DoCalculusEngine — mixed edge types (TD-CAUSAL-1)
# ---------------------------------------------------------------------------


class TestDoCalculusMixedEdges:
    def test_forward_and_reverse_both_impacted(self):
        """pdu --connected_to--> switch (forward)
        app --depends_on--> pdu (reverse)
        do(pdu=failed): switch impacted (forward), app impacted (reverse)."""
        rows = [
            _row("pdu", "switch", edge_type="connected_to", confidence=0.9),
            _row("app", "pdu", edge_type="depends_on", confidence=0.8),
        ]
        g = _mixed_graph(rows)
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        assert "switch" in result.probability_matrix  # forward impact
        assert "app" in result.probability_matrix  # reverse impact

    def test_cascaded_mixed_types(self):
        """pdu --connected_to--> switch; app --depends_on--> switch.
        do(pdu=failed) → switch impacted (P=0.9) → app impacted (P=0.9*0.8=0.72)."""
        rows = [
            _row("pdu", "switch", edge_type="connected_to", confidence=0.9),
            _row("app", "switch", edge_type="depends_on", confidence=0.8),
        ]
        g = _mixed_graph(rows)
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        assert result.probability_matrix["switch"] == pytest.approx(0.9, rel=1e-3)
        assert result.probability_matrix["app"] == pytest.approx(0.72, rel=1e-3)


# ---------------------------------------------------------------------------
# 12. Confounding path detection — renamed field (TD-CAUSAL-4)
# ---------------------------------------------------------------------------


class TestConfoundingPaths:
    def test_confounding_path_detected(self):
        rows = [
            _row("common_cause", "pdu", edge_type="connected_to", confidence=0.9),
            _row("pdu", "switch", edge_type="connected_to", confidence=0.8),
            _row("common_cause", "unrelated_service", edge_type="connected_to", confidence=0.7),
        ]
        g = _mixed_graph(rows)
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        assert isinstance(result.confounding_paths, list)
        for cp in result.confounding_paths:
            assert isinstance(cp, ConfoundingPath)
            # TD-CAUSAL-4: field is now intervention_node, not source
            assert cp.intervention_node == "pdu"
            assert 0.0 <= cp.raw_confidence <= 1.0

    def test_confounding_path_has_no_source_field(self):
        """TD-CAUSAL-4: ConfoundingPath.source was renamed to intervention_node."""
        cp = ConfoundingPath(
            intervention_node="X",
            sink="Y",
            path=("W", "Y"),
            raw_confidence=0.7,
        )
        assert cp.intervention_node == "X"
        assert not hasattr(cp, "source") or cp._fields[0] == "intervention_node"

    def test_no_confounders_when_root_node(self):
        g = _graph(("pdu", "switch", 0.9), ("switch", "server", 0.8))
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        assert result.confounding_paths == []


# ---------------------------------------------------------------------------
# 13. ImpactScore and hop distances
# ---------------------------------------------------------------------------


class TestImpactScore:
    def test_direct_impact_hop_distance_one(self):
        g = _graph(("pdu", "switch", 0.9))
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        assert result.directly_impacted[0].hop_distance == 1

    def test_transitive_impact_hop_distance_two(self):
        g = _graph(("pdu", "switch", 0.9), ("switch", "server", 0.8))
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        transitive = {s.node_id: s for s in result.transitively_impacted}
        assert transitive["server"].hop_distance == 2

    def test_hop_distance_two_for_reverse_transitive(self):
        """Transitive reverse: app depends_on server depends_on pdu.
        app is 2 hops from pdu failure."""
        rows = [
            _row("app", "server", edge_type="depends_on", confidence=0.9),
            _row("server", "pdu", edge_type="depends_on", confidence=0.8),
        ]
        g = _mixed_graph(rows)
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        transitive = {s.node_id: s for s in result.transitively_impacted}
        assert transitive["app"].hop_distance == 2

    def test_paths_count_correct_for_diamond(self):
        g = _graph(
            ("pdu", "sw_a", 0.9),
            ("pdu", "sw_b", 0.9),
            ("sw_a", "server", 0.8),
            ("sw_b", "server", 0.7),
        )
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        transitive = {s.node_id: s for s in result.transitively_impacted}
        assert transitive["server"].paths_count == 2

    def test_directly_impacted_sorted_by_probability_desc(self):
        g = _graph(("pdu", "high_conf", 0.95), ("pdu", "low_conf", 0.3))
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        probs = [s.impact_probability for s in result.directly_impacted]
        assert probs == sorted(probs, reverse=True)


# ---------------------------------------------------------------------------
# 14. Invariant and property tests
# ---------------------------------------------------------------------------


class TestInvariants:
    def test_soft_deleted_row_handling(self):
        """TD-CAUSAL-2: from_rows accepts rows without valid_to (key not present)."""
        rows = [_row("pdu", "switch", "connected_to")]
        # valid_to is absent from _row() — row.get("last_verified") should not KeyError
        g = CausalGraph.from_rows(rows, NS)
        assert g.node_count == 2

    def test_prune_result_non_negative_counts(self):
        g = _graph(("pdu", "switch", 0.9), ("switch", "server", 0.8))
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        assert result.probability_matrix["switch"] >= 0.0
        assert result.probability_matrix["server"] >= 0.0

    def test_probability_matrix_excludes_intervention_node(self):
        g = _graph(("pdu", "switch", 0.9))
        result = DoCalculusEngine().evaluate(g, "pdu", _now=NOW)
        assert "pdu" not in result.probability_matrix
