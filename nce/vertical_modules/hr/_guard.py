"""
nce/vertical_modules/hr/_guard.py
=================================
Opt-in guard and Red Line policies for Module 13 (HR Engine).

Enforces:
- RL-1: Hard-pinned NEVER-ranking policy. Refuses cross-person ranking, peer scoring,
  and leaderboards by design.
- Namespace opt-in via metadata.hr.enabled = true.
"""

from __future__ import annotations

import logging
from typing import Any

from asyncpg.exceptions import DataError

from nce.structural.no_person_grain import (
    AggregationGrain,
    PersonGrainRejected,
    QueryIntent,
    apply_guard,
)

log = logging.getLogger("nce.vertical_modules.hr._guard")

# Hard-pinned in code: never ranking (RL-1 & Nordic privacy policy)
NCE_HR_RANKING_DISABLED: bool = True


class HrDisabledError(Exception):
    """Raised when a namespace has not opted in to the HR vertical."""


class HrRankingProhibitedError(PersonGrainRejected):
    """Raised when an operation requests cross-person ranking, peer comparison, or a leaderboard."""


def _is_ranking_flag(val: Any) -> bool:
    if val is None or val is False:
        return False
    if isinstance(val, (int, float)) and val == 0:
        return False
    if isinstance(val, str) and val.strip().lower() in ("false", "0", "no", "none", ""):
        return False
    return True


def _resolve_requested_grain(params: dict[str, Any]) -> AggregationGrain:
    grain_raw = (
        params.get("requested_grain") or params.get("grain") or params.get("aggregation_grain")
    )
    if isinstance(grain_raw, AggregationGrain):
        return grain_raw
    if isinstance(grain_raw, str):
        grain_str = grain_raw.strip().lower()
        if grain_str in ("team", "org", "department"):
            return AggregationGrain.TEAM
        if grain_str in ("period", "time", "date", "week", "month"):
            return AggregationGrain.PERIOD
        if grain_str in ("engine", "module"):
            return AggregationGrain.ENGINE
    return AggregationGrain.PERSON


def assert_ranking_prohibited(params: dict[str, Any]) -> None:
    """Verify that the request does not ask for prohibited employee ranking (RL-1 / C9b).

    Enforces the EU AI Act Art. 5 floor: individual employees must never be ranked,
    scored against peers, or compared on a person-grain leaderboard.

    Parameters
    ----------
    params : dict[str, Any]
        Incoming tool or API parameters.

    Raises
    ------
    HrRankingProhibitedError (subclass of PersonGrainRejected)
        If ranking, leaderboard, peer comparison, or standing performance scoring
        is requested at person grain.
    """
    if not NCE_HR_RANKING_DISABLED:
        # Defense-in-depth: should never happen as constant is hard-pinned True
        raise RuntimeError("CRITICAL: NCE_HR_RANKING_DISABLED cannot be cleared.")

    requested_grain = _resolve_requested_grain(params)

    prohibited_flags = (
        "leaderboard",
        "standing_ranking",
        "rank_employees",
        "compare_peers",
        "rank_against_peers",
        "top_performers",
        "top_performer",
        "rank",
        "ranking",
        "compare",
        "comparison",
        "is_comparison_or_ranking",
    )
    for key in prohibited_flags:
        if _is_ranking_flag(params.get(key)):
            reason = f"Parameter {key!r} requests employee ranking/comparison"
            intent = QueryIntent(
                has_person_dimension=True,
                is_comparison_or_ranking=True,
                requested_grain=requested_grain,
            )
            try:
                apply_guard(intent)
            except PersonGrainRejected as exc:
                raise HrRankingProhibitedError(
                    f"NEVER ranking policy (RL-1 / C9b): {reason}. Prohibited by EU AI Act Art. 5: {exc}"
                ) from exc

    if params.get("rank_by") is not None and str(params.get("rank_by")).strip():
        reason = f"Parameter 'rank_by'={params.get('rank_by')!r} requests ranking"
        intent = QueryIntent(
            has_person_dimension=True,
            is_comparison_or_ranking=True,
            requested_grain=requested_grain,
        )
        try:
            apply_guard(intent)
        except PersonGrainRejected as exc:
            raise HrRankingProhibitedError(
                f"NEVER ranking policy (RL-1 / C9b): {reason}. Prohibited by EU AI Act Art. 5: {exc}"
            ) from exc

    for sort_key in ("sort_by", "order_by"):
        sort_val = str(params.get(sort_key) or "").strip().lower()
        if sort_val and any(
            k in sort_val
            for k in (
                "rating",
                "score",
                "performance",
                "standing",
                "rank",
                "utilization",
                "fit",
                "workload",
            )
        ):
            reason = f"Sorting candidates by {sort_val!r} via {sort_key} is strictly prohibited"
            intent = QueryIntent(
                has_person_dimension=True,
                is_comparison_or_ranking=True,
                requested_grain=requested_grain,
            )
            try:
                apply_guard(intent)
            except PersonGrainRejected as exc:
                raise HrRankingProhibitedError(
                    f"NEVER ranking policy (RL-1 / C9b): {reason}. Prohibited by EU AI Act Art. 5: {exc}"
                ) from exc

    for limit_key in ("top_n", "top_k", "limit_top", "best_of"):
        if params.get(limit_key) is not None:
            reason = f"Top-N ranking parameter {limit_key!r} is specified"
            intent = QueryIntent(
                has_person_dimension=True,
                is_comparison_or_ranking=True,
                requested_grain=requested_grain,
            )
            try:
                apply_guard(intent)
            except PersonGrainRejected as exc:
                raise HrRankingProhibitedError(
                    f"NEVER ranking policy (RL-1 / C9b): {reason}. Prohibited by EU AI Act Art. 5: {exc}"
                ) from exc

    # Also apply the structural guard to verify compliant grain
    intent = QueryIntent(
        has_person_dimension=True,
        is_comparison_or_ranking=False,
        requested_grain=requested_grain,
    )
    apply_guard(intent)


async def require_hr_enabled(
    pool: Any,
    namespace_id: str,
) -> None:
    """Assert that ``metadata.hr.enabled`` is ``true`` for *namespace_id*.

    Applied at the MCP handler / REST route boundary only -- never inside a
    ``do_*`` core.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(
                           (metadata->'hr'->>'enabled')::boolean,
                           false
                       ) AS hr_enabled
                FROM   namespaces
                WHERE  id = $1::uuid
                """,
                namespace_id,
            )
    except DataError as exc:
        log.info(
            "require_hr_enabled: invalid namespace UUID %r: %s",
            namespace_id,
            exc,
        )
        raise HrDisabledError(
            f"Namespace {namespace_id!r} is invalid or has not enabled HR."
        ) from exc

    if not row or not row["hr_enabled"]:
        raise HrDisabledError(
            f"Namespace {namespace_id!r} has not enabled the HR Engine (metadata.hr.enabled is not true)."
        )
