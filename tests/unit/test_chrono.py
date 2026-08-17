"""
tests/unit/test_chrono.py
=========================
Unit tests for Chrono-Branching and transient graph overrides.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from nce.causal.chrono import apply_hypothetical_states, branch_timeline, get_active_branch
from nce.causal.correlation import CausalGraph, DoCalculusEngine

NS = uuid.UUID("cccccccc-0000-0000-0000-000000000001")


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
# 1. Context Manager Basics & Verification
# ---------------------------------------------------------------------------


class TestChronoContextManager:
    def test_branch_timeline_activates_and_cleans_up(self):
        target_time = "2026-06-01T12:00:00Z"
        hypothetical = {"nodes": {"switch_01": {"node_type": "router"}}}

        assert get_active_branch() is None

        with branch_timeline(target_time, hypothetical):
            branch = get_active_branch()
            assert branch is not None
            assert branch["target_time"] == datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
            assert branch["hypothetical_states"] == hypothetical

        assert get_active_branch() is None

    def test_nested_branch_timeline_is_rejected(self):
        """Nested branch_timeline must raise RuntimeError; outer branch must survive."""
        outer_time = "2026-06-01T12:00:00Z"
        inner_time = "2026-06-02T08:00:00Z"
        outer_hypo = {"nodes": {"switch_01": {"node_type": "router"}}}

        with branch_timeline(outer_time, outer_hypo):
            outer_branch = get_active_branch()
            assert outer_branch is not None

            with pytest.raises(RuntimeError, match="already active"):
                with branch_timeline(inner_time, {}):
                    pass  # must not be reached

            # Outer branch must survive the failed inner attempt
            surviving_branch = get_active_branch()
            assert surviving_branch is not None
            assert surviving_branch["target_time"] == datetime(
                2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc
            )
            assert surviving_branch["hypothetical_states"] == outer_hypo

        assert get_active_branch() is None

    def test_sequential_branches_after_exit_are_allowed(self):
        """branch→exit→branch must succeed; the guard must not block sequential usage."""
        t1 = "2026-06-01T09:00:00Z"
        t2 = "2026-06-01T10:00:00Z"

        with branch_timeline(t1, {}):
            assert get_active_branch() is not None

        assert get_active_branch() is None

        with branch_timeline(t2, {}):
            branch = get_active_branch()
            assert branch is not None
            assert branch["target_time"] == datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)

        assert get_active_branch() is None

    @pytest.mark.anyio
    async def test_contextvar_isolation_across_async_tasks(self):
        """Verify that the chrono branch ContextVar is isolated between coroutines."""
        t1 = "2026-06-01T10:00:00Z"
        t2 = "2026-06-01T11:00:00Z"

        async def worker1():
            with branch_timeline(t1, {}):
                await asyncio.sleep(0.05)
                branch = get_active_branch()
                assert branch["target_time"] == datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)

        async def worker2():
            with branch_timeline(t2, {}):
                await asyncio.sleep(0.02)
                branch = get_active_branch()
                assert branch["target_time"] == datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)

        await asyncio.gather(worker1(), worker2())


# ---------------------------------------------------------------------------
# 2. In-Memory Graph Overrides
# ---------------------------------------------------------------------------


class TestGraphOverrides:
    def test_node_addition_and_modification(self):
        graph = _graph(("pdu", "switch", 0.9))

        hypothetical = {
            "nodes": {"pdu": {"node_type": "smart_pdu"}, "new_node": {"node_type": "gateway"}}
        }
        mutated = apply_hypothetical_states(graph, hypothetical)

        # Original remains untouched
        assert graph.get_node("pdu").node_type == "device"
        assert graph.get_node("new_node") is None

        # Mutated reflects changes
        assert mutated.get_node("pdu").node_type == "smart_pdu"
        assert mutated.get_node("new_node") is not None
        assert mutated.get_node("new_node").node_type == "gateway"

    def test_edge_addition_and_modification(self):
        graph = _graph(("pdu", "switch", 0.9))

        hypothetical = {
            "edges": [
                # Override existing edge confidence
                {"source_node_id": "pdu", "target_node_id": "switch", "confidence_score": 0.5},
                # Inject a new edge
                {
                    "source_node_id": "switch",
                    "target_node_id": "server",
                    "edge_type": "depends_on",
                    "confidence_score": 0.85,
                },
            ]
        }
        mutated = apply_hypothetical_states(graph, hypothetical)

        # Original is untouched
        orig_edges = graph.outgoing_edges("pdu")
        assert orig_edges[0].confidence_score == 0.9

        # Mutated is overridden
        mut_edges_pdu = mutated.outgoing_edges("pdu")
        assert mut_edges_pdu[0].confidence_score == 0.5

        mut_edges_switch = mutated.outgoing_edges("switch")
        assert len(mut_edges_switch) == 1
        assert mut_edges_switch[0].target_node_id == "server"
        assert mut_edges_switch[0].edge_type == "depends_on"
        assert mut_edges_switch[0].confidence_score == 0.85

    def test_node_and_edge_deletions(self):
        graph = _graph(("pdu", "switch", 0.9), ("switch", "server", 0.85))

        hypothetical = {"deletions": {"nodes": ["server"], "edges": [("pdu", "switch")]}}
        mutated = apply_hypothetical_states(graph, hypothetical)

        assert "server" not in mutated.node_ids
        assert len(mutated.outgoing_edges("pdu")) == 0
        assert len(mutated.outgoing_edges("switch")) == 0  # because target server was deleted


# ---------------------------------------------------------------------------
# 3. DoCalculus Under Branched Timeline
# ---------------------------------------------------------------------------


class TestDoCalculusChronoBranch:
    def test_evaluation_under_chrono_branch(self):
        # Base graph: pdu -> switch -> server
        # In a branched reality, we inject a backup_pdu connected directly to the switch
        graph = _graph(("pdu", "switch", 0.9), ("switch", "server", 0.8))

        hypothetical = {
            "edges": [
                {
                    "source_node_id": "backup_pdu",
                    "target_node_id": "switch",
                    "edge_type": "connected_to",
                    "confidence_score": 0.95,
                }
            ]
        }

        # Apply in-memory overrides
        branched_graph = apply_hypothetical_states(graph, hypothetical)

        # Verify that we have backup_pdu in the branched graph
        assert "backup_pdu" in branched_graph.node_ids

        # Evaluate P(server impacted | do(pdu = failed)) under both realities
        engine = DoCalculusEngine()

        # Original reality do(pdu):
        res_orig = engine.evaluate(graph, "pdu")
        # server is impacted via path pdu -> switch -> server (P = 0.9 * 0.8 = 0.72)
        assert res_orig.probability_matrix["server"] == pytest.approx(0.72)

        # Branched reality do(pdu):
        # server is still impacted but the switch has an alternate cause (backup_pdu)
        # Because we only intervene on do(pdu), the link from backup_pdu to switch is intact.
        res_branched = engine.evaluate(branched_graph, "pdu")
        assert res_branched.probability_matrix["server"] == pytest.approx(0.72)

        # We also check that backup_pdu exists as a backdoor/confounder path or can be reached
        assert branched_graph.get_node("backup_pdu") is not None
