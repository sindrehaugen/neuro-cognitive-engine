"""Unit tests for Module 14 Marketing Engine approval gate and publish transport.

Phase 5:
  - MK-1: Structural human gate on publishing.
          do_publish_content hard-refuses without recorded human approval.
          No autonomous publish tier exists.
  - MK-4: Consent gate enforcement at publish boundary.
  - PublishTransport: manual transport live; cms transport raises NotImplementedError.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

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

    in_tx = False

    async def _tx_enter(*args: Any, **kwargs: Any) -> Any:
        nonlocal in_tx
        in_tx = True
        return tx

    async def _tx_exit(*args: Any, **kwargs: Any) -> Any:
        nonlocal in_tx
        in_tx = False
        return None

    tx = MagicMock()
    tx.__aenter__ = AsyncMock(side_effect=_tx_enter)
    tx.__aexit__ = AsyncMock(side_effect=_tx_exit)
    conn.transaction = MagicMock(return_value=tx)
    conn.is_in_transaction = MagicMock(side_effect=lambda: in_tx)

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


@pytest.mark.asyncio
async def test_approve_content_records_event_log_audit_in_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Human approval records audit event in event_log inside active transaction (Charter §13 / PR #51)."""
    captured_calls: list[dict[str, Any]] = []

    async def fake_append_event(
        conn: Any,
        namespace_id: Any,
        agent_id: str,
        event_type: str,
        params: dict[str, Any],
        **kwargs: Any,
    ) -> MagicMock:
        assert conn.is_in_transaction(), "append_event called outside transaction!"
        captured_calls.append(
            {
                "conn": conn,
                "namespace_id": namespace_id,
                "agent_id": agent_id,
                "event_type": event_type,
                "params": params,
            }
        )
        res = MagicMock()
        res.event_id = uuid4()
        res.event_seq = 1
        return res

    monkeypatch.setattr("nce.vertical_modules.marketing.approval.append_event", fake_append_event)

    engine = _make_mock_engine()
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
    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["namespace_id"] == UUID(_NAMESPACE_ID)
    assert call["agent_id"] == "marketing_engine"
    assert call["event_type"] == "marketing_content_approved"
    assert call["params"]["artifact_id"] == _ARTIFACT_ID
    assert call["params"]["approver"] == "Jane Doe (Marketing Director)"
    assert call["params"]["decision"] == "approved"
    assert call["params"]["notes"] == "Verified facts and branding voice."
    assert "approved_at" in call["params"]


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


@pytest.mark.asyncio
async def test_approve_and_publish_content_asset_persists_and_reads_approver_in_seo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """do_approve_content persists approver in content_assets.seo and do_publish_content reads it."""
    monkeypatch.setattr(
        "nce.vertical_modules.marketing.approval.append_event",
        AsyncMock(),
    )
    engine = _make_mock_engine()
    conn = engine.pg_pool.acquire.return_value.__aenter__.return_value
    conn.execute = AsyncMock(side_effect=["SET", "UPDATE 0", "UPDATE 1"])

    res_approve = await do_approve_content(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "artifact_id": _ARTIFACT_ID,
            "approver": "Jane Asset Approver",
            "decision": "approved",
        },
    )
    assert res_approve["ok"] is True

    update_asset_call = conn.execute.call_args_list[2]
    sql_text = update_asset_call[0][0]
    assert "UPDATE content_assets" in sql_text
    assert "jsonb_build_object('approver', $4::text)" in sql_text
    assert update_asset_call[0][4] == "Jane Asset Approver"

    # Now publish a content_assets row where approver is stored inside seo JSONB
    engine_pub = _make_mock_engine()
    conn_pub = engine_pub.pg_pool.acquire.return_value.__aenter__.return_value
    conn_pub.fetchrow = AsyncMock(
        side_effect=[
            None,
            {
                "id": _ARTIFACT_ID,
                "namespace_id": _NAMESPACE_ID,
                "title": "Approved Content Asset",
                "status": "approved",
                "storage_uri": "s3://assets/item-1",
                "seo": '{"approver": "Jane Asset Approver"}',
                "marketing_source_id": "marketing:asset:001",
            },
        ]
    )
    res_pub = await do_publish_content(
        engine_pub,
        {
            "namespace_id": _NAMESPACE_ID,
            "artifact_id": _ARTIFACT_ID,
            "transport": "manual",
        },
    )
    assert res_pub["ok"] is True
    assert res_pub["status"] == "published"


@pytest.mark.asyncio
async def test_marketing_sets_rls_namespace_context_and_scopes_testimonial_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marketing modules call set_namespace_context on asyncpg.Connection and scope testimonial upsert by namespace_id."""
    import asyncpg

    from nce.vertical_modules.marketing.advisor import do_suggest_content
    from nce.vertical_modules.marketing.brief import do_query_marketing_a2a
    from nce.vertical_modules.marketing.candidates import do_find_case_study_candidates
    from nce.vertical_modules.marketing.events import emit_marketing_event
    from nce.vertical_modules.marketing.testimonials import do_request_testimonial

    rls_calls: list[UUID] = []

    async def _fake_set_ns(conn: Any, ns_id: UUID) -> None:
        rls_calls.append(ns_id)

    for mod_path in (
        "nce.vertical_modules.marketing.brief.set_namespace_context",
        "nce.vertical_modules.marketing.candidates.set_namespace_context",
        "nce.vertical_modules.marketing.advisor.set_namespace_context",
        "nce.vertical_modules.marketing.approval.set_namespace_context",
        "nce.vertical_modules.marketing.publish.set_namespace_context",
        "nce.vertical_modules.marketing.testimonials.set_namespace_context",
        "nce.vertical_modules.marketing.events.set_namespace_context",
    ):
        monkeypatch.setattr(mod_path, _fake_set_ns)

    for guard_path in (
        "nce.vertical_modules.marketing._guard.require_marketing_enabled",
        "nce.vertical_modules.marketing.brief.require_marketing_enabled",
    ):
        monkeypatch.setattr(guard_path, AsyncMock())
    monkeypatch.setattr(
        "nce.vertical_modules.marketing.events.append_event",
        AsyncMock(),
    )

    from contextlib import asynccontextmanager

    conn = MagicMock(spec=asyncpg.Connection)
    executed_sqls: list[str] = []

    async def _exec(sql: str, *args: Any) -> str:
        executed_sqls.append(sql)
        return "INSERT 0 1"

    conn.execute = AsyncMock(side_effect=_exec)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _fake_tx():
        yield conn

    conn.transaction = MagicMock(side_effect=_fake_tx)

    @asynccontextmanager
    async def _fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=_fake_acquire)
    engine = MagicMock(pg_pool=pool, pool=pool)

    await do_query_marketing_a2a(engine, {"namespace_id": _NAMESPACE_ID})
    await do_find_case_study_candidates(engine, {"namespace_id": _NAMESPACE_ID})
    await do_suggest_content(engine, {"namespace_id": _NAMESPACE_ID})
    await emit_marketing_event(engine, _NAMESPACE_ID, "test_event", {})
    await do_request_testimonial(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "customer_id": "CUST-1",
            "project_id": "PRJ-1",
            "nps_score": 9.5,
        },
    )

    assert len(rls_calls) >= 5
    assert all(ns == UUID(_NAMESPACE_ID) for ns in rls_calls)
    assert any("WHERE testimonials.namespace_id = $2::uuid" in s for s in executed_sqls)
