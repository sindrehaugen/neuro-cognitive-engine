"""
nce/vertical_modules/marketing/_guard.py
========================================
Opt-in guard and Red Line policies for Module 14 (Marketing Engine).

Enforces:
- MK-1: Human gate on publishing. There is structurally NO autonomous publish tier.
- MK-2: Retrieval-grounded assembly. Every claim must cite a valid graph node.
- MK-3: Draft-assembly redaction. Margin, cost, and internal fields never reach draft bodies.
- MK-4: Structured consent (web_retractable vs ai_citable_irrevocable).
- MK-5: Positive-only testimonial trigger. Rejects low-health customer outreach.
- Namespace opt-in via metadata.marketing.enabled = true.
"""

from __future__ import annotations

import logging
from typing import Any

from asyncpg.exceptions import DataError

log = logging.getLogger("nce.vertical_modules.marketing._guard")


class MarketingDisabledError(Exception):
    """Raised when a namespace has not opted into the Marketing vertical."""


class MarketingUngroundedClaimError(Exception):
    """Raised when a marketing draft contains claims ungrounded in the cognitive graph (MK-2)."""


class MarketingSensitiveDataLeakError(Exception):
    """Raised when sensitive internal fields (margin, cost, rates) are found in marketing data (MK-3)."""


class MarketingConsentMissingError(Exception):
    """Raised when publishing is attempted without required consent recorded (MK-4)."""


class MarketingLowHealthTriggerError(Exception):
    """Raised when marketing outreach is attempted on low-health/dissatisfied customers (MK-5)."""


class MarketingUnapprovedPublishError(Exception):
    """Raised when content is pushed for publishing without recorded human approval (MK-1)."""


FORBIDDEN_FINANCIAL_KEYS = frozenset(
    {
        "margin",
        "cost",
        "unit_cost",
        "internal_cost",
        "internal_labor_cost",
        "profit_margin",
        "profit_margin_pct",
        "markup",
        "markup_pct",
        "supplier_rebate",
        "purchase_price",
        "wholesale_price",
    }
)


def assert_no_sensitive_financials(data: dict[str, Any]) -> None:
    """Ensure no margin, cost, or internal financial metrics enter marketing content (MK-3)."""
    for k in data.keys():
        if k.lower() in FORBIDDEN_FINANCIAL_KEYS:
            raise MarketingSensitiveDataLeakError(
                f"Sensitive financial field {k!r} is strictly forbidden in marketing drafts (MK-3)."
            )


def assert_claims_grounded(citations: list[dict[str, Any]]) -> None:
    """Verify that every factual claim in a draft is explicitly grounded to a graph node (MK-2)."""
    if not citations:
        raise MarketingUngroundedClaimError(
            "Draft contains claims without graph grounding citations (MK-2 refusal)."
        )

    for c in citations:
        node_id = str(c.get("graph_node_id") or "").strip()
        claim = str(c.get("claim") or "").strip()
        if not node_id or not claim:
            raise MarketingUngroundedClaimError(
                f"Ungrounded claim detected: citation missing node ID or claim text ({c}) (MK-2 refusal)."
            )


def assert_positive_nps_only(nps_score: float, threshold: float = 9.0) -> None:
    """Verify that testimonial outreach is triggered ONLY on high NPS (MK-5)."""
    if nps_score < threshold:
        raise MarketingLowHealthTriggerError(
            f"Testimonial trigger refused: NPS {nps_score} is below positive threshold {threshold}. "
            "Never perform marketing outreach on low customer health (MK-5)."
        )


def assert_consent_allows_tier(required_tier: str, granted_tier: str | None) -> None:
    """Verify that recorded consent tier satisfies the required publishing tier (MK-4)."""
    if not granted_tier:
        raise MarketingConsentMissingError("Consent is required but none is recorded.")
    if required_tier == "ai_citable_irrevocable" and granted_tier != "ai_citable_irrevocable":
        raise MarketingConsentMissingError(
            f"Publishing requires 'ai_citable_irrevocable' consent; recorded tier is {granted_tier!r}."
        )


async def require_marketing_enabled(
    pool: Any,
    namespace_id: str,
) -> None:
    """Assert that ``metadata.marketing.enabled`` is ``true`` for *namespace_id*.

    Applied at the MCP handler / REST route boundary only -- never inside a ``do_*`` core.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(
                           (metadata->'marketing'->>'enabled')::boolean,
                           false
                       ) AS marketing_enabled
                FROM   namespaces
                WHERE  id = $1::uuid
                """,
                namespace_id,
            )
    except DataError as exc:
        log.info(
            "require_marketing_enabled: invalid namespace UUID %r: %s",
            namespace_id,
            exc,
        )
        raise MarketingDisabledError(
            f"Namespace {namespace_id!r} is invalid or has not enabled Marketing."
        ) from exc

    if not row or not row["marketing_enabled"]:
        raise MarketingDisabledError(
            f"Namespace {namespace_id!r} has not enabled the Marketing Engine (metadata.marketing.enabled is not true)."
        )
