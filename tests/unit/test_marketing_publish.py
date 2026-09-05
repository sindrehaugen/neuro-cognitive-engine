"""Unit tests for Module 14 Marketing Engine approval gate and publish transport.

Phase 5:
  - MK-1: Structural human gate on publishing.
          do_publish_content hard-refuses without recorded human approval.
          No autonomous publish tier exists.
  - MK-4: Consent gate enforcement at publish boundary.
  - PublishTransport: manual transport live; cms transport raises NotImplementedError.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.vertical_modules.marketing._guard import (
    MarketingConsentMissingError,
    MarketingUnapprovedPublishError,
)
from nce.vertical_modules.marketing.approval import do_approve_content
from nce.vertical_modules.marketing.publish import (
    PublishTransport,
    do_publish_content,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_ARTIFACT_ID = str(uuid4())


def _make_mock_engine(artifact_row: dict | None = None) -> MagicMock:
    engine = MagicMock()
    conn = AsyncMock()
    conn.fetchrow.return_value = artifact_row
    conn.execute.return_value = "UPDATE 1"
    ctx = AsyncMock()
    ctx.__aenter__.return_value = conn
    ctx.__aexit__.return_value = None

    pool = MagicMock()
    pool.acquire.return_value = ctx
    engine.pg_pool = pool
    engine.pool = pool
    return engine


# ---------------------------------------------------------------------------
# Human approval gate (MK-1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_content_records_human_signoff() -> None:
    """Human approval records approver, decision, and timestamp."""
    engine = _make_mock_engine(
        {
            "id": _ARTIFACT_ID,
            "namespace_id": _NAMESPACE_ID,
            "status": "draft",
            "title": "Corporate HQ Auditorium AV-over-IP",
        }
    )

    res = await do_approve_content(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "artifact_id": _ARTIFACT_ID,
            "approver": "Jane Doe (Marketing Director)",
            "decision": "approved",
            "notes": "Verified facts and branding voice.",
        },
    )

    assert res["ok"] is True
    assert res["status"] == "approved"
    assert res["approver"] == "Jane Doe (Marketing Director)"
    assert "approved_at" in res


@pytest.mark.asyncio
async def test_approve_content_rejects_empty_approver() -> None:
    """Approval requires a non-empty human approver identity."""
    engine = _make_mock_engine()

    with pytest.raises(ValueError):
        await do_approve_content(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "artifact_id": _ARTIFACT_ID,
                "approver": "   ",
                "decision": "approved",
            },
        )


# ---------------------------------------------------------------------------
# Publish gate controls (MK-1, MK-4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_content_refuses_unapproved_draft() -> None:
    """MK-1 RED: Attempting to publish unapproved draft hard-refuses."""
    engine = _make_mock_engine(
        {
            "id": _ARTIFACT_ID,
            "namespace_id": _NAMESPACE_ID,
            "status": "draft",
            "approver": None,
            "body": "Draft content...",
            "title": "Unapproved Case Study",
        }
    )

    with pytest.raises(MarketingUnapprovedPublishError):
        await do_publish_content(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "artifact_id": _ARTIFACT_ID,
                "transport": "manual",
            },
        )


@pytest.mark.asyncio
async def test_publish_content_refuses_unconsented_customer_content() -> None:
    """MK-4 RED: Approved content lacking recorded customer consent hard-refuses."""
    engine = _make_mock_engine(
        {
            "id": _ARTIFACT_ID,
            "namespace_id": _NAMESPACE_ID,
            "status": "approved",
            "approver": "Jane Doe",
            "body": "Customer content with named quotes...",
            "title": "Case Study with Quotes",
            "consent": False,
            "is_customer_content": True,
        }
    )

    with pytest.raises(MarketingConsentMissingError):
        await do_publish_content(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "artifact_id": _ARTIFACT_ID,
                "transport": "manual",
            },
        )


@pytest.mark.asyncio
async def test_publish_content_manual_success() -> None:
    """Approved content with valid consent publishes via manual export."""
    engine = _make_mock_engine(
        {
            "id": _ARTIFACT_ID,
            "namespace_id": _NAMESPACE_ID,
            "status": "approved",
            "approver": "Jane Doe",
            "body": "Verified outcome study...",
            "title": "Executive Boardroom Modernization",
            "consent": True,
            "consent_tier": "web_retractable",
            "is_customer_content": True,
            "marketing_source_id": "marketing:study:001",
        }
    )

    res = await do_publish_content(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "artifact_id": _ARTIFACT_ID,
            "transport": "manual",
        },
    )

    assert res["ok"] is True
    assert res["status"] == "published"
    assert res["transport"] == PublishTransport.MANUAL.value
    assert "export_payload" in res
    assert res["export_payload"]["title"] == "Executive Boardroom Modernization"


@pytest.mark.asyncio
async def test_publish_content_cms_deferred_stub() -> None:
    """CMS publish transport is deferred and raises NotImplementedError."""
    engine = _make_mock_engine(
        {
            "id": _ARTIFACT_ID,
            "namespace_id": _NAMESPACE_ID,
            "status": "approved",
            "approver": "Jane Doe",
            "body": "Verified study...",
            "title": "Case Study",
            "consent": True,
        }
    )

    with pytest.raises(NotImplementedError) as exc_info:
        await do_publish_content(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "artifact_id": _ARTIFACT_ID,
                "transport": "cms",
            },
        )

    assert "CMS" in str(exc_info.value)
