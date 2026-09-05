"""Unit tests for Module 14 Marketing Engine consent and testimonials lifecycle.

Phase 4:
  - MK-5: Testimonial requests trigger ONLY on high NPS (>= 9.0).
          Outreach on low customer health / sub-9.0 NPS is strictly refused.
  - MK-4: Structured consent capture supporting two tiers:
          'web_retractable' vs 'ai_citable_irrevocable'.
  - Right to retract: consent withdrawal sets status to 'retracted'.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.vertical_modules.marketing._guard import (
    MarketingConsentMissingError,
    MarketingLowHealthTriggerError,
)
from nce.vertical_modules.marketing.testimonials import (
    do_capture_testimonial,
    do_request_testimonial,
    do_retract_testimonial,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_CUSTOMER_ID = "CUST-ACME-001"
_PROJECT_ID = "PRJ-ALPHA-100"


def _make_mock_engine() -> MagicMock:
    engine = MagicMock()
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    conn.execute.return_value = "INSERT 0 1"
    ctx = AsyncMock()
    ctx.__aenter__.return_value = conn
    ctx.__aexit__.return_value = None

    pool = MagicMock()
    pool.acquire.return_value = ctx
    engine.pg_pool = pool
    engine.pool = pool
    return engine


# ---------------------------------------------------------------------------
# MK-5 Controls: Positive-only trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_testimonial_rejects_low_nps() -> None:
    """MK-5 RED: Low health or sub-9.0 NPS must refuse testimonial request."""
    engine = _make_mock_engine()

    # Low NPS (7.0)
    with pytest.raises(MarketingLowHealthTriggerError):
        await do_request_testimonial(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "customer_id": _CUSTOMER_ID,
                "project_id": _PROJECT_ID,
                "nps_score": 7.0,
            },
        )

    # Detractor NPS (3.5)
    with pytest.raises(MarketingLowHealthTriggerError):
        await do_request_testimonial(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "customer_id": _CUSTOMER_ID,
                "project_id": _PROJECT_ID,
                "nps_score": 3.5,
            },
        )


@pytest.mark.asyncio
async def test_request_testimonial_accepts_high_nps() -> None:
    """MK-5 GREEN: High NPS (>= 9.0) successfully stages a testimonial request."""
    engine = _make_mock_engine()

    res = await do_request_testimonial(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "customer_id": _CUSTOMER_ID,
            "project_id": _PROJECT_ID,
            "nps_score": 9.5,
        },
    )

    assert res["ok"] is True
    assert res["status"] == "requested"
    assert res["customer_id"] == _CUSTOMER_ID
    assert res["nps_score"] == 9.5
    assert res["consent"] is False
    assert res["consent_tier"] == "none"
    assert "marketing_source_id" in res


# ---------------------------------------------------------------------------
# MK-4 Controls: Two-tier consent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_testimonial_rejects_missing_consent() -> None:
    """MK-4 RED: Capturing a testimonial without explicit consent is refused."""
    engine = _make_mock_engine()

    with pytest.raises(MarketingConsentMissingError):
        await do_capture_testimonial(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "customer_id": _CUSTOMER_ID,
                "project_id": _PROJECT_ID,
                "quote": "Outstanding implementation and support.",
                "consent": False,
                "consent_tier": "web_retractable",
            },
        )


@pytest.mark.asyncio
async def test_capture_testimonial_rejects_invalid_tier() -> None:
    """MK-4 RED: Invalid consent tier is rejected."""
    engine = _make_mock_engine()

    with pytest.raises(ValueError):
        await do_capture_testimonial(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "customer_id": _CUSTOMER_ID,
                "project_id": _PROJECT_ID,
                "quote": "Great system.",
                "consent": True,
                "consent_tier": "unlimited_public_forever",
            },
        )


@pytest.mark.asyncio
async def test_capture_testimonial_web_retractable() -> None:
    """MK-4 GREEN: Capturing quote with web_retractable consent."""
    engine = _make_mock_engine()
    t_id = str(uuid4())

    res = await do_capture_testimonial(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "testimonial_id": t_id,
            "customer_id": _CUSTOMER_ID,
            "project_id": _PROJECT_ID,
            "quote": "The AV installation transformed our boardroom.",
            "consent": True,
            "consent_tier": "web_retractable",
            "attribution_name": "Senior Operations Lead",
            "attribution_title": "Acme Corp",
        },
    )

    assert res["ok"] is True
    assert res["status"] == "received"
    assert res["consent"] is True
    assert res["consent_tier"] == "web_retractable"
    assert res["quote"] == "The AV installation transformed our boardroom."
    assert "consent_recorded_at" in res


@pytest.mark.asyncio
async def test_capture_testimonial_ai_citable_irrevocable() -> None:
    """MK-4 GREEN: Capturing quote with ai_citable_irrevocable durable consent."""
    engine = _make_mock_engine()
    t_id = str(uuid4())

    res = await do_capture_testimonial(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "testimonial_id": t_id,
            "customer_id": _CUSTOMER_ID,
            "project_id": _PROJECT_ID,
            "quote": "Flawless audio clarity and intuitive touch panels.",
            "consent": True,
            "consent_tier": "ai_citable_irrevocable",
            "attribution_name": "Chief Technical Officer",
        },
    )

    assert res["ok"] is True
    assert res["status"] == "received"
    assert res["consent"] is True
    assert res["consent_tier"] == "ai_citable_irrevocable"


# ---------------------------------------------------------------------------
# Right to retract: consent withdrawal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retract_testimonial() -> None:
    """Right to retract: flips status to retracted and revokes consent."""
    engine = _make_mock_engine()
    t_id = str(uuid4())

    res = await do_retract_testimonial(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "testimonial_id": t_id,
            "reason": "Customer requested removal from web case studies.",
        },
    )

    assert res["ok"] is True
    assert res["status"] == "retracted"
    assert res["consent"] is False
    assert res["testimonial_id"] == t_id
