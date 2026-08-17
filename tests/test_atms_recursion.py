"""
tests/test_atms_recursion.py
============================
Batch 101 — atms-iterative-traversal acceptance tests.

Pure-unit tests (no DB, no integration mark) covering:
  (a) 5 000-node linear chain invalidation without RecursionError
  (b) Cyclic justification set reported invalid (no infinite loop)
  (c) Small fixed graph yields byte-identical cascade sets vs. the documented contract
      (regression against the recursive semantics).
"""

from __future__ import annotations

import sys

from nce.atms import ATMSEngine, ATMSNodeType

# ---------------------------------------------------------------------------
# Helper: build a linear chain A_0 <- A_1 <- ... <- A_{n-1}
#
# Each node A_i (i > 0) is DERIVED and depends on A_{i-1}.
# A_0 is the root ASSUMPTION.  Invalidating A_0 must cascade to all n nodes.
# ---------------------------------------------------------------------------


def _build_linear_chain(n: int) -> ATMSEngine:
    """Build a straight justification chain of length *n*."""
    atms = ATMSEngine()
    atms.register_node("A_0", ATMSNodeType.ASSUMPTION, is_valid=True)
    for i in range(1, n):
        atms.register_node(f"A_{i}", ATMSNodeType.DERIVED, is_valid=True)
        atms.add_justification(f"A_{i}", {f"A_{i - 1}"})
    return atms


# ---------------------------------------------------------------------------
# (a) 5 000-node linear chain
# ---------------------------------------------------------------------------


class TestLinearChainNoRecursionError:
    """Invalidating the root of a 5 000-node chain must not raise RecursionError."""

    CHAIN_LENGTH = 5_000

    def test_no_recursion_error(self) -> None:
        atms = _build_linear_chain(self.CHAIN_LENGTH)
        # Confirm default recursion limit would be insufficient
        assert self.CHAIN_LENGTH > sys.getrecursionlimit(), (
            "Test is only meaningful when chain depth exceeds the recursion limit"
        )
        # Must not raise
        cascade = atms.invalidate_assumption("A_0")
        # All nodes in the chain should have been invalidated
        assert len(cascade) == self.CHAIN_LENGTH, (
            f"Expected {self.CHAIN_LENGTH} invalidated nodes, got {len(cascade)}"
        )

    def test_all_nodes_invalidated(self) -> None:
        atms = _build_linear_chain(self.CHAIN_LENGTH)
        atms.invalidate_assumption("A_0")
        for i in range(self.CHAIN_LENGTH):
            node = atms.nodes[f"A_{i}"]
            assert not node.is_valid, f"A_{i} should be invalid after cascade"

    def test_is_node_provably_valid_deep_chain(self) -> None:
        """is_node_provably_valid must handle chains deeper than the recursion limit."""
        atms = _build_linear_chain(self.CHAIN_LENGTH)
        # Before invalidation every node should be provable
        assert atms.is_node_provably_valid(f"A_{self.CHAIN_LENGTH - 1}", set()) is True
        # After invalidating root, the leaf should not be provable
        atms.nodes["A_0"].is_valid = False
        assert atms.is_node_provably_valid(f"A_{self.CHAIN_LENGTH - 1}", set()) is False


# ---------------------------------------------------------------------------
# (b) Cyclic justification set
# ---------------------------------------------------------------------------


class TestCyclicJustification:
    """A cyclic dependency must be reported as invalid with no infinite loop."""

    def test_simple_two_node_cycle(self) -> None:
        """A depends on B, B depends on A — both must be invalid."""
        atms = ATMSEngine()
        atms.register_node("A", ATMSNodeType.DERIVED, is_valid=True)
        atms.register_node("B", ATMSNodeType.DERIVED, is_valid=True)
        atms.add_justification("A", {"B"})
        atms.add_justification("B", {"A"})

        assert atms.is_node_provably_valid("A", set()) is False
        assert atms.is_node_provably_valid("B", set()) is False

    def test_three_node_cycle(self) -> None:
        """A->B->C->A ring — none provable."""
        atms = ATMSEngine()
        for nid in ("A", "B", "C"):
            atms.register_node(nid, ATMSNodeType.DERIVED, is_valid=True)
        atms.add_justification("A", {"B"})
        atms.add_justification("B", {"C"})
        atms.add_justification("C", {"A"})

        for nid in ("A", "B", "C"):
            assert atms.is_node_provably_valid(nid, set()) is False, (
                f"{nid} should be invalid in a cycle"
            )

    def test_self_loop(self) -> None:
        """A node whose only justification includes itself is invalid."""
        atms = ATMSEngine()
        atms.register_node("X", ATMSNodeType.DERIVED, is_valid=True)
        atms.add_justification("X", {"X"})

        assert atms.is_node_provably_valid("X", set()) is False

    def test_cycle_with_valid_escape(self) -> None:
        """A node in a cycle that ALSO has a non-cyclic justification IS valid."""
        atms = ATMSEngine()
        atms.register_node("P", ATMSNodeType.PREMISE)  # unconditionally valid
        atms.register_node("A", ATMSNodeType.DERIVED, is_valid=True)
        atms.register_node("B", ATMSNodeType.DERIVED, is_valid=True)
        # A can be proved via P (no cycle) or via B (cycle)
        atms.add_justification("A", {"P"})  # valid path
        atms.add_justification("A", {"B"})  # cyclic path
        atms.add_justification("B", {"A"})  # B depends on A (cycle)

        # A has a valid non-cyclic justification so it IS provable
        assert atms.is_node_provably_valid("A", set()) is True
        # B only has the cyclic path; A is provable but B->A->B is still a cycle for B
        # B depends on A; A is provable, so B IS provable
        assert atms.is_node_provably_valid("B", set()) is True

    def test_propagate_deprecation_cycle_no_infinite_loop(self) -> None:
        """propagate_deprecation on a cycle must terminate."""
        atms = ATMSEngine()
        atms.register_node("Base", ATMSNodeType.ASSUMPTION, is_valid=True)
        atms.register_node("A", ATMSNodeType.DERIVED, is_valid=True)
        atms.register_node("B", ATMSNodeType.DERIVED, is_valid=True)
        atms.add_justification("A", {"Base", "B"})
        atms.add_justification("B", {"Base", "A"})

        # Invalidating Base should cascade without looping
        cascade = atms.invalidate_assumption("Base")
        assert "Base" in cascade


# ---------------------------------------------------------------------------
# (c) Small fixed graph — byte-identical cascade sets (regression)
# ---------------------------------------------------------------------------


class TestContractRegression:
    """Small fixed graphs whose expected cascade sets are known a priori."""

    def test_simple_assumption_invalidation(self) -> None:
        """A <- B <- C: invalidating A cascades to {A, B, C}."""
        atms = ATMSEngine()
        atms.register_node("A", ATMSNodeType.ASSUMPTION, is_valid=True)
        atms.register_node("B", ATMSNodeType.DERIVED, is_valid=True)
        atms.register_node("C", ATMSNodeType.DERIVED, is_valid=True)
        atms.add_justification("B", {"A"})
        atms.add_justification("C", {"B"})

        cascade = atms.invalidate_assumption("A")
        assert cascade == {"A", "B", "C"}

    def test_diamond_invalidation(self) -> None:
        """Diamond: A -> {B, C} -> D; invalidating A cascades to all four."""
        atms = ATMSEngine()
        atms.register_node("A", ATMSNodeType.ASSUMPTION, is_valid=True)
        atms.register_node("B", ATMSNodeType.DERIVED, is_valid=True)
        atms.register_node("C", ATMSNodeType.DERIVED, is_valid=True)
        atms.register_node("D", ATMSNodeType.DERIVED, is_valid=True)
        atms.add_justification("B", {"A"})
        atms.add_justification("C", {"A"})
        atms.add_justification("D", {"B", "C"})

        cascade = atms.invalidate_assumption("A")
        assert cascade == {"A", "B", "C", "D"}

    def test_premise_not_invalidated(self) -> None:
        """Attempting to invalidate a PREMISE returns empty set and logs warning."""
        atms = ATMSEngine()
        atms.register_node("P", ATMSNodeType.PREMISE)
        atms.register_node("D", ATMSNodeType.DERIVED, is_valid=True)
        atms.add_justification("D", {"P"})

        cascade = atms.invalidate_assumption("P")
        assert cascade == set()
        assert atms.nodes["P"].is_valid is True
        assert atms.nodes["D"].is_valid is True

    def test_multi_justification_partial_support(self) -> None:
        """D has two justifications; invalidating one source still leaves D valid via the other."""
        atms = ATMSEngine()
        atms.register_node("A", ATMSNodeType.ASSUMPTION, is_valid=True)
        atms.register_node("B", ATMSNodeType.ASSUMPTION, is_valid=True)
        atms.register_node("D", ATMSNodeType.DERIVED, is_valid=True)
        # D can be proved by A alone OR by B alone
        atms.add_justification("D", {"A"})
        atms.add_justification("D", {"B"})

        cascade = atms.invalidate_assumption("A")
        # D still provable via B — should NOT be in cascade
        assert "D" not in cascade
        assert atms.nodes["D"].is_valid is True

    def test_multi_justification_all_sources_gone(self) -> None:
        """D requires BOTH A and B; invalidating either makes D unprovable."""
        atms = ATMSEngine()
        atms.register_node("A", ATMSNodeType.ASSUMPTION, is_valid=True)
        atms.register_node("B", ATMSNodeType.ASSUMPTION, is_valid=True)
        atms.register_node("D", ATMSNodeType.DERIVED, is_valid=True)
        # D requires BOTH A and B
        atms.add_justification("D", {"A", "B"})

        cascade = atms.invalidate_assumption("A")
        assert "D" in cascade
        assert not atms.nodes["D"].is_valid

    def test_is_provably_valid_memoization(self) -> None:
        """Memo cache produces identical results across repeated calls."""
        atms = ATMSEngine()
        atms.register_node("P", ATMSNodeType.PREMISE)
        atms.register_node("M", ATMSNodeType.DERIVED, is_valid=True)
        atms.add_justification("M", {"P"})

        memo: dict[str, bool] = {}
        r1 = atms.is_node_provably_valid("M", set(), memo)
        r2 = atms.is_node_provably_valid("M", set(), memo)
        assert r1 is True
        assert r1 == r2
        assert "M" in memo

    def test_evaluate_belief_states(self) -> None:
        """evaluate_belief_states correctly marks all DERIVED nodes."""
        atms = ATMSEngine()
        atms.register_node("P", ATMSNodeType.PREMISE)
        atms.register_node("A", ATMSNodeType.ASSUMPTION, is_valid=True)
        atms.register_node("D1", ATMSNodeType.DERIVED, is_valid=True)
        atms.register_node("D2", ATMSNodeType.DERIVED, is_valid=True)
        atms.add_justification("D1", {"P", "A"})
        # D2 has no justifications -> cannot be proved
        # (no add_justification call for D2)

        atms.evaluate_belief_states()
        assert atms.nodes["D1"].is_valid is True
        assert atms.nodes["D2"].is_valid is False

    def test_register_contradiction_cascade(self) -> None:
        """Contradiction resolution invalidates the correct node and cascades."""
        atms = ATMSEngine()
        atms.register_node("X", ATMSNodeType.ASSUMPTION, is_valid=True)
        atms.register_node("Y", ATMSNodeType.ASSUMPTION, is_valid=True)
        atms.register_node("Z", ATMSNodeType.DERIVED, is_valid=True)
        atms.add_justification("Z", {"X"})

        cascade = atms.register_contradiction("X", "Y", resolution_strategy="invalidate_a")
        assert "X" in cascade
        assert "Z" in cascade
        assert "Y" not in cascade
