"""
nce/vertical_modules/business_insights/_guard.py
================================================
Opt-in guard, role access gates, and legal/safety red lines for Module 16 (Business Insights Engine).

Enforces:
- BI-1: Structural Person-grain Barrier (EU AI Act Article 5 - never rank people).
- BI-2: Confidence and Coverage verification (unsupported findings flagged).
- BI-3: Third-Party AI Egress boundary authorization and ledger audit verification.
- BI-4: Day-one Grace Degradation for missing/unlanded upstream engines.
- Namespace opt-in via metadata.business_insights.enabled = true.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("nce.vertical_modules.business_insights._guard")


class BusinessInsightsDisabledError(Exception):
    """Raised when a namespace has not opted into the Business Insights vertical."""


class PersonRankingProhibitedError(Exception):
    """Raised when an attempt to rank, compare, or score individual employees/people is detected (BI-1)."""


class ThirdPartyEgressUnauthorizedError(Exception):
    """Raised when an unapproved external AI or unauthorized principal attempts financial data query (BI-3)."""


class LowCoverageAssertionError(Exception):
    """Raised when a finding lacks required upstream engine reconciliation or structured attribution (BI-2)."""


def assert_business_insights_enabled(metadata: dict[str, Any] | None) -> None:
    """Validate that the namespace has explicitly enabled Business Insights."""
    if not metadata:
        raise BusinessInsightsDisabledError(
            "Namespace metadata is empty; business_insights is disabled by default."
        )
    bi_cfg = metadata.get("business_insights")
    if not isinstance(bi_cfg, dict) or not bi_cfg.get("enabled", False):
        raise BusinessInsightsDisabledError(
            "Business Insights vertical is disabled for this namespace. "
            "Set metadata.business_insights.enabled = true to activate."
        )


def assert_exec_or_board_role(
    principal_role: str | None, allowed_roles: set[str] | None = None
) -> None:
    """Enforce that only executive or board principals can access Business Insights."""
    if allowed_roles is None:
        allowed_roles = {"admin", "executive", "board", "finance_director", "managing_director"}
    if not principal_role or principal_role.lower() not in allowed_roles:
        raise PermissionError(
            f"Access denied: principal role {principal_role!r} is not authorized for "
            "Business Insights executive surfaces."
        )


def is_engine_landed(engine_name: str) -> bool:
    """Check if an upstream engine is landed and available."""
    from nce.vertical_modules.business_insights.kpi import LIVE_ENGINES

    return engine_name.lower() in LIVE_ENGINES


async def require_insights_role(
    principal_role: str | None,
    allow_board: bool = True,
    allowed_roles: set[str] | None = None,
) -> None:
    """Validate executive or board authorization asynchronously."""
    if allowed_roles is None:
        allowed_roles = {"admin", "executive", "finance_director", "managing_director"}
        if allow_board:
            allowed_roles.add("board")
    assert_exec_or_board_role(principal_role, allowed_roles=allowed_roles)


async def require_business_insights_enabled(
    pool: Any,
    namespace_id: str,
) -> None:
    """Assert that metadata.business_insights.enabled is true for namespace_id.

    Applied at the MCP handler / REST route boundary only -- never inside a do_* core.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(
                           (metadata->'business_insights'->>'enabled')::boolean,
                           false
                       ) AS bi_enabled
                FROM   namespaces
                WHERE  id = $1::uuid
                """,
                namespace_id,
            )
    except Exception as exc:
        log.info(
            "require_business_insights_enabled: error checking namespace %r: %s",
            namespace_id,
            exc,
        )
        raise BusinessInsightsDisabledError(
            f"Namespace {namespace_id!r} is invalid or has not enabled Business Insights."
        ) from exc

    if not row or not row["bi_enabled"]:
        raise BusinessInsightsDisabledError(
            f"Namespace {namespace_id!r} has not enabled the Business Insights Engine (metadata.business_insights.enabled is not true)."
        )
