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

log = logging.getLogger("nce.vertical_modules.hr._guard")

# Hard-pinned in code: never ranking (RL-1 & Nordic privacy policy)
NCE_HR_RANKING_DISABLED: bool = True


class HrDisabledError(Exception):
    """Raised when a namespace has not opted in to the HR vertical."""


class HrRankingProhibitedError(Exception):
    """Raised when an operation requests cross-person ranking, peer comparison, or a leaderboard."""


def assert_ranking_prohibited(params: dict[str, Any]) -> None:
    """Verify that the request does not ask for prohibited employee ranking (RL-1).

    Parameters
    ----------
    params : dict[str, Any]
        Incoming tool or API parameters.

    Raises
    ------
    HrRankingProhibitedError
        If ranking, leaderboard, peer comparison, or standing performance scoring
        is requested.
    """
    if not NCE_HR_RANKING_DISABLED:
        # Defense-in-depth: should never happen as constant is hard-pinned True
        raise RuntimeError("CRITICAL: NCE_HR_RANKING_DISABLED cannot be cleared.")

    prohibited_keys = (
        "leaderboard",
        "standing_ranking",
        "rank_employees",
        "compare_peers",
        "rank_against_peers",
        "top_performers",
        "top_performer",
    )
    for key in prohibited_keys:
        if params.get(key) is True:
            raise HrRankingProhibitedError(
                f"NEVER ranking policy (RL-1): {key} is strictly prohibited by policy and EU AI Act Art. 5."
            )

    sort_by = str(params.get("sort_by") or "").strip().lower()
    if any(k in sort_by for k in ("rating", "score", "performance", "standing", "rank")):
        raise HrRankingProhibitedError(
            f"NEVER ranking policy (RL-1): Sorting candidates by {sort_by!r} is strictly prohibited."
        )


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
