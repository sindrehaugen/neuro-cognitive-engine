"""
C9b — no-person-grain comparison/ranking query guard.

Design contract (§9.3 / 99-shared-core-foundation.md §C9b):
  The data-access layer CANNOT return person-grain rows for
  comparison or ranking regardless of how the query is phrased.
  A query that involves a person dimension AND a comparison/ranking
  intent is FORCED to aggregate at team/period/engine grain — never
  at person grain.  A caller that tries to retrieve person-grain
  ranking rows directly gets a ``PersonGrainRejected`` error.  There
  is no code path that returns individual-person comparison rows.

  This is structural, not instructional: the guard is a pure-logic
  function in the data-access layer; it carries no LLM prompt,
  no string-matching heuristic, and no phrasing dependency.

Vocabulary (precise, in code not prose):
  - *person dimension* — the query selects or orders by an
    ``EMPLOYEE`` / ``CONTRACTOR`` / ``RESOURCE`` node identity.
  - *comparison / ranking intent* — the query requests ordering,
    scoring, or differential measurement across individuals.
  - *forced aggregation* — person identity is replaced by the coarser
    grain (team, period, or engine) before any result can be emitted.
    The coarser grain is the ONLY returnable unit.

Dependency rule (uncle-bob-craft):
  This module is domain core.  It must NOT import web/HTTP/admin/DB
  modules.  It has zero external dependencies — pure Python dataclasses
  and exceptions.  Callers in the data-access layer import this and
  apply the guard before constructing or executing any query.

Usage:
    from nce.structural.no_person_grain import (
        QueryIntent,
        AggregationGrain,
        PersonGrainRejected,
        apply_guard,
    )

    intent = QueryIntent(
        has_person_dimension=True,
        is_comparison_or_ranking=True,
        requested_grain=AggregationGrain.PERSON,  # caller wants person rows
    )
    # apply_guard raises PersonGrainRejected — PERSON is not in _SAFE_GRAINS.
    # Any grain not in the allowlist (including future finer grains) is
    # rejected by default.  Only TEAM / PERIOD / ENGINE are explicitly safe.

    safe_grain = apply_guard(QueryIntent(
        has_person_dimension=True,
        is_comparison_or_ranking=True,
        requested_grain=AggregationGrain.TEAM,
    ))
    # safe_grain is AggregationGrain.TEAM — explicitly in _SAFE_GRAINS.
    # The caller MUST aggregate at safe_grain; individual rows are gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class AggregationGrain(Enum):
    """The grain at which a query result is aggregated.

    Ordered from finest (PERSON) to coarsest (ENGINE) so that
    the guard can enforce a minimum-coarse floor.
    """

    PERSON = auto()  # individual employee / contractor — NEVER allowed for comparison
    TEAM = auto()  # team or org unit
    PERIOD = auto()  # time period (week, month, quarter)
    ENGINE = auto()  # engine / function (e.g. "field_tech dispatch")


# Allowlist of grains that are safe to return for a person-grain
# comparison/ranking query.  DEFAULT-DENY: any grain NOT in this set
# (including PERSON and any future finer grain) is rejected.  This is
# the correct design for an EU-AI-Act structural floor — fail-safe, not
# fail-open.  Adding a new AggregationGrain member requires an explicit
# decision to either add it here (safe) or leave it out (rejected).
_SAFE_GRAINS: frozenset[AggregationGrain] = frozenset(
    {
        AggregationGrain.TEAM,
        AggregationGrain.PERIOD,
        AggregationGrain.ENGINE,
    }
)


class PersonGrainRejected(Exception):
    """Raised when a person-grain comparison/ranking query cannot be satisfied.

    This exception is a structural gate — it is not a soft warning.
    The caller must re-issue the query at team/period grain.
    """


@dataclass(frozen=True)
class QueryIntent:
    """A distilled description of a query's aggregation requirements.

    Parameters
    ----------
    has_person_dimension:
        True when the query selects, orders, or filters on individual
        person identity (EMPLOYEE / CONTRACTOR / RESOURCE node types).
    is_comparison_or_ranking:
        True when the query requests ordering, scoring, or differential
        measurement — i.e. it would produce a ranked list of individuals
        if the grain were PERSON.
    requested_grain:
        The grain the caller *wants* to receive results at.  The guard
        may coerce this to a coarser grain; the returned grain from
        ``apply_guard`` is what the data-access layer MUST use.
    """

    has_person_dimension: bool
    is_comparison_or_ranking: bool
    requested_grain: AggregationGrain


def _is_person_grain_comparison(intent: QueryIntent) -> bool:
    """Return True iff this intent describes a person-grain comparison query.

    A query is a *person-grain comparison* when it simultaneously:
      1. targets a person dimension (individual identity), AND
      2. requests comparison or ranking across those individuals.

    Both conditions must hold.  A person-dimension lookup that is not
    comparative (e.g. "show me Alice's certifications") is not blocked
    by this guard — it may proceed at whatever grain the caller requests.
    """
    return intent.has_person_dimension and intent.is_comparison_or_ranking


def apply_guard(intent: QueryIntent) -> AggregationGrain:
    """Enforce the no-person-grain rule and return the safe aggregation grain.

    For a person-grain comparison/ranking query the requested grain must be
    in ``_SAFE_GRAINS``.  Any grain NOT in the allowlist — including PERSON
    and any future finer grain added to ``AggregationGrain`` — is rejected
    with ``PersonGrainRejected``.  This is DEFAULT-DENY: a new enum member
    is unsafe until it is explicitly added to ``_SAFE_GRAINS``.

    For all other queries (non-comparison or no person dimension) the
    requested grain is returned unchanged.

    Parameters
    ----------
    intent:
        The distilled query intent produced by the data-access layer.

    Returns
    -------
    AggregationGrain
        The grain the data-access layer MUST use when constructing and
        executing the query.  Always a member of ``_SAFE_GRAINS`` for a
        comparison/ranking query with a person dimension.

    Raises
    ------
    PersonGrainRejected
        When ``intent.is_comparison_or_ranking`` is True,
        ``intent.has_person_dimension`` is True, AND
        ``intent.requested_grain`` is not in ``_SAFE_GRAINS``.
    """
    if not _is_person_grain_comparison(intent):
        # Non-comparison queries are unrestricted by this guard.
        return intent.requested_grain

    # Person-grain comparison: allowlist check — default-deny.
    # Any grain not explicitly listed as safe is rejected, including PERSON
    # and any future grain finer than PERSON that is not yet on the list.
    if intent.requested_grain not in _SAFE_GRAINS:
        raise PersonGrainRejected(
            "Person-grain comparison/ranking queries are structurally prohibited. "
            "Re-issue the query aggregated by team or period. "
            f"Requested grain {intent.requested_grain!r} is not in the safe-grain allowlist. "
            "(C9b — EU-AI-Act / HR no-ranking floor)"
        )

    # The caller requested a grain that is explicitly safe — honour it.
    return intent.requested_grain
