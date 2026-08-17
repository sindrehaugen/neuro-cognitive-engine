"""
Unit tests for nce.structural.no_person_grain — C9b query guard.

Acceptance gate (Batch 030 / M0.W30):
  A person-grain comparison/ranking query (e.g. "rank technicians") returns
  aggregates only (team/period grain), by construction — no individual-person
  row is ever returned, regardless of phrasing.

These are pure-logic tests: no database, no async, no fixtures.
All tests are plain unit tests (no @pytest.mark.integration).
"""

from __future__ import annotations

import pytest

from nce.structural.no_person_grain import (
    _SAFE_GRAINS,
    AggregationGrain,
    PersonGrainRejected,
    QueryIntent,
    _is_person_grain_comparison,
    apply_guard,
)

# ---------------------------------------------------------------------------
# _is_person_grain_comparison — predicate correctness
# ---------------------------------------------------------------------------


class TestIsPersonGrainComparison:
    """The predicate identifies comparison+person-dimension combinations only."""

    def test_both_flags_true_is_person_grain_comparison(self) -> None:
        intent = QueryIntent(
            has_person_dimension=True,
            is_comparison_or_ranking=True,
            requested_grain=AggregationGrain.PERSON,
        )
        assert _is_person_grain_comparison(intent) is True

    def test_person_dimension_without_comparison_is_not_blocked(self) -> None:
        """A person-dimension query that is NOT comparative is allowed through."""
        intent = QueryIntent(
            has_person_dimension=True,
            is_comparison_or_ranking=False,
            requested_grain=AggregationGrain.PERSON,
        )
        assert _is_person_grain_comparison(intent) is False

    def test_comparison_without_person_dimension_is_not_blocked(self) -> None:
        """A comparison query that has no person dimension is allowed through."""
        intent = QueryIntent(
            has_person_dimension=False,
            is_comparison_or_ranking=True,
            requested_grain=AggregationGrain.TEAM,
        )
        assert _is_person_grain_comparison(intent) is False

    def test_neither_flag_is_not_blocked(self) -> None:
        intent = QueryIntent(
            has_person_dimension=False,
            is_comparison_or_ranking=False,
            requested_grain=AggregationGrain.PERSON,
        )
        assert _is_person_grain_comparison(intent) is False


# ---------------------------------------------------------------------------
# apply_guard — structural rejection of person-grain comparison
# ---------------------------------------------------------------------------


class TestApplyGuardPersonGrainRejection:
    """The guard raises PersonGrainRejected for person-grain comparison queries."""

    def test_rank_technicians_person_grain_is_rejected(self) -> None:
        """The canonical 'rank technicians' query at person grain is rejected.

        This is the done-when assertion: the data-access layer physically
        cannot emit a person-grain comparison result — the call raises.
        """
        intent = QueryIntent(
            has_person_dimension=True,
            is_comparison_or_ranking=True,
            requested_grain=AggregationGrain.PERSON,
        )
        with pytest.raises(PersonGrainRejected):
            apply_guard(intent)

    def test_error_message_references_c9b(self) -> None:
        """The rejection message names C9b so callers can trace the rule."""
        intent = QueryIntent(
            has_person_dimension=True,
            is_comparison_or_ranking=True,
            requested_grain=AggregationGrain.PERSON,
        )
        with pytest.raises(PersonGrainRejected, match="C9b"):
            apply_guard(intent)

    def test_rejection_is_an_exception_not_a_sentinel(self) -> None:
        """The guard raises — it does not return a sentinel value.

        A silent sentinel would let a caller accidentally ignore the guard.
        Only an exception is structurally safe.
        """
        intent = QueryIntent(
            has_person_dimension=True,
            is_comparison_or_ranking=True,
            requested_grain=AggregationGrain.PERSON,
        )
        raised = False
        try:
            apply_guard(intent)
        except PersonGrainRejected:
            raised = True
        assert raised, "apply_guard must raise PersonGrainRejected, not return silently"


# ---------------------------------------------------------------------------
# apply_guard — forced aggregation at team/period grain
# ---------------------------------------------------------------------------


class TestApplyGuardForcedAggregation:
    """When person-grain is not requested, comparison queries are safe at team/period."""

    def test_rank_technicians_at_team_grain_returns_team(self) -> None:
        """A 'rank technicians by team' query is allowed and returns TEAM grain."""
        intent = QueryIntent(
            has_person_dimension=True,
            is_comparison_or_ranking=True,
            requested_grain=AggregationGrain.TEAM,
        )
        safe_grain = apply_guard(intent)
        assert safe_grain is AggregationGrain.TEAM

    def test_rank_technicians_at_period_grain_returns_period(self) -> None:
        """A 'rank technicians by period' query is allowed and returns PERIOD grain."""
        intent = QueryIntent(
            has_person_dimension=True,
            is_comparison_or_ranking=True,
            requested_grain=AggregationGrain.PERIOD,
        )
        safe_grain = apply_guard(intent)
        assert safe_grain is AggregationGrain.PERIOD

    def test_rank_technicians_at_engine_grain_returns_engine(self) -> None:
        """A ranking at engine grain (e.g. per-engine throughput) is allowed."""
        intent = QueryIntent(
            has_person_dimension=True,
            is_comparison_or_ranking=True,
            requested_grain=AggregationGrain.ENGINE,
        )
        safe_grain = apply_guard(intent)
        assert safe_grain is AggregationGrain.ENGINE

    def test_safe_grain_is_never_person_for_comparison(self) -> None:
        """Exhaustive: no combination of comparison+person-dim returns PERSON grain.

        Covers all non-PERSON grains to confirm the guard never escalates
        coarser grains back down to PERSON.
        """
        safe_grains = [AggregationGrain.TEAM, AggregationGrain.PERIOD, AggregationGrain.ENGINE]
        for grain in safe_grains:
            intent = QueryIntent(
                has_person_dimension=True,
                is_comparison_or_ranking=True,
                requested_grain=grain,
            )
            result = apply_guard(intent)
            assert result is not AggregationGrain.PERSON, (
                f"Guard returned PERSON grain for requested_grain={grain!r}"
            )


# ---------------------------------------------------------------------------
# apply_guard — non-comparison queries pass through unrestricted
# ---------------------------------------------------------------------------


class TestApplyGuardPassThrough:
    """Non-comparison queries are not affected by the guard."""

    def test_person_lookup_not_comparison_passes_at_person_grain(self) -> None:
        """A person-dimension lookup (e.g. 'show Alice's certs') is unrestricted."""
        intent = QueryIntent(
            has_person_dimension=True,
            is_comparison_or_ranking=False,
            requested_grain=AggregationGrain.PERSON,
        )
        safe_grain = apply_guard(intent)
        assert safe_grain is AggregationGrain.PERSON

    def test_non_person_comparison_passes_at_requested_grain(self) -> None:
        """A comparison of teams (no person dimension) passes through as TEAM."""
        intent = QueryIntent(
            has_person_dimension=False,
            is_comparison_or_ranking=True,
            requested_grain=AggregationGrain.TEAM,
        )
        safe_grain = apply_guard(intent)
        assert safe_grain is AggregationGrain.TEAM

    def test_plain_query_no_flags_passes_at_requested_grain(self) -> None:
        """A plain query with neither flag set is unrestricted."""
        intent = QueryIntent(
            has_person_dimension=False,
            is_comparison_or_ranking=False,
            requested_grain=AggregationGrain.PERIOD,
        )
        safe_grain = apply_guard(intent)
        assert safe_grain is AggregationGrain.PERIOD


# ---------------------------------------------------------------------------
# Structural invariant — person grain is physically not returnable
# ---------------------------------------------------------------------------


class TestStructuralInvariant:
    """The invariant: person-grain comparison result is NEVER returned by any path."""

    def test_every_person_grain_comparison_raises(self) -> None:
        """Exhaustive: every combination of True/True/PERSON always raises.

        This is the structural-enforcement assertion: there is no code path
        in apply_guard that returns AggregationGrain.PERSON for a
        comparison query with a person dimension.
        """
        intent = QueryIntent(
            has_person_dimension=True,
            is_comparison_or_ranking=True,
            requested_grain=AggregationGrain.PERSON,
        )
        with pytest.raises(PersonGrainRejected):
            apply_guard(intent)

    def test_result_is_never_person_for_any_comparison_intent(self) -> None:
        """For all coarser grains: the guard returns the grain, not PERSON.

        Together with test_every_person_grain_comparison_raises, this
        proves that apply_guard never returns PERSON for a comparison query
        regardless of the requested_grain.
        """
        for grain in (AggregationGrain.TEAM, AggregationGrain.PERIOD, AggregationGrain.ENGINE):
            intent = QueryIntent(
                has_person_dimension=True,
                is_comparison_or_ranking=True,
                requested_grain=grain,
            )
            result = apply_guard(intent)
            assert result is not AggregationGrain.PERSON


# ---------------------------------------------------------------------------
# Parametrized allowlist coverage — every current AggregationGrain member
# ---------------------------------------------------------------------------


class TestAllowlistDefaultDeny:
    """Default-deny: every current grain is tested against the allowlist.

    This parametrized suite iterates over EVERY ``AggregationGrain`` member
    for a person-grain comparison intent (has_person_dimension=True,
    is_comparison_or_ranking=True) and asserts:

    - Non-safe grains (not in ``_SAFE_GRAINS``) RAISE ``PersonGrainRejected``.
    - Safe grains (in ``_SAFE_GRAINS``) return a non-PERSON grain.

    The test is parametrized so that adding a new ``AggregationGrain`` member
    automatically exercises it — a new grain is rejected by default until it
    is explicitly added to ``_SAFE_GRAINS``.  This is the fail-safe invariant.
    """

    @pytest.mark.parametrize("grain", list(AggregationGrain))
    def test_non_safe_grain_raises_person_grain_rejected(self, grain: AggregationGrain) -> None:
        """Any grain NOT in _SAFE_GRAINS raises PersonGrainRejected (default-deny)."""
        if grain in _SAFE_GRAINS:
            pytest.skip(f"{grain!r} is in _SAFE_GRAINS — tested by the safe-grain case")
        intent = QueryIntent(
            has_person_dimension=True,
            is_comparison_or_ranking=True,
            requested_grain=grain,
        )
        with pytest.raises(PersonGrainRejected):
            apply_guard(intent)

    @pytest.mark.parametrize("grain", list(AggregationGrain))
    def test_safe_grain_returns_non_person_grain(self, grain: AggregationGrain) -> None:
        """Every safe grain returns a non-PERSON grain for a comparison intent."""
        if grain not in _SAFE_GRAINS:
            pytest.skip(f"{grain!r} is not in _SAFE_GRAINS — tested by the reject case")
        intent = QueryIntent(
            has_person_dimension=True,
            is_comparison_or_ranking=True,
            requested_grain=grain,
        )
        result = apply_guard(intent)
        assert result is not AggregationGrain.PERSON, (
            f"apply_guard returned PERSON for safe grain {grain!r} — invariant violated"
        )
