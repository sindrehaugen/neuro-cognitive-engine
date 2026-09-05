"""
tests/unit/test_marketing_events_brief.py
=========================================
Unit tests for Marketing Engine Phase 7 (Charter M14.W7):
  - Marketing event definitions and emission contracts
  - Replay provenance handling for all 6 marketing events
  - Forbidden param keys enforcement (MK-3)
  - Executive Morning Brief (#19) throughput slice
  - Agent-to-Agent (A2A) marketing proof query
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from nce.event_types import EVENT_FORBIDDEN_PARAM_KEYS, EventType
from nce.replay import _additional_fork_provenance_types
from nce.vertical_modules.marketing._guard import MarketingDisabledError
from nce.vertical_modules.marketing.brief import (
    do_query_marketing_a2a,
    get_marketing_morning_brief_slice,
)
from nce.vertical_modules.marketing.events import (
    EVENT_MARKETING_CASE_STUDY_DRAFTED,
    EVENT_MARKETING_CONTENT_APPROVED,
    EVENT_MARKETING_CONTENT_PUBLISHED,
    EVENT_MARKETING_TESTIMONIAL_CAPTURED,
    EVENT_MARKETING_TESTIMONIAL_REQUESTED,
    EVENT_MARKETING_TESTIMONIAL_RETRACTED,
    emit_marketing_event,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"


def test_marketing_event_constants_and_contracts() -> None:
    """Validate all 6 marketing event types exist in EventType and replay."""
    expected_types = [
        EVENT_MARKETING_CASE_STUDY_DRAFTED,
        EVENT_MARKETING_TESTIMONIAL_REQUESTED,
        EVENT_MARKETING_TESTIMONIAL_CAPTURED,
        EVENT_MARKETING_TESTIMONIAL_RETRACTED,
        EVENT_MARKETING_CONTENT_APPROVED,
        EVENT_MARKETING_CONTENT_PUBLISHED,
    ]

    from typing import get_args

    valid_types = get_args(EventType)
    for et in expected_types:
        # Check presence in EventType literal
        assert et in valid_types
        # Check MK-3 forbidden parameter keys
        assert et in EVENT_FORBIDDEN_PARAM_KEYS
        forbidden = EVENT_FORBIDDEN_PARAM_KEYS[et]
        assert "margin" in forbidden or "margin_pct" in forbidden
        assert "cost" in forbidden or "purchase_cost" in forbidden
        # Check registered in replay fork provenance handlers
        assert et in _additional_fork_provenance_types


@pytest.mark.asyncio
async def test_emit_marketing_event_success() -> None:
    """Test emit_marketing_event executes append_event with correct args."""
    mock_engine = MagicMock()
    mock_pool = MagicMock()
    mock_conn = AsyncMock()

    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_engine.pg_pool = mock_pool

    with patch(
        "nce.vertical_modules.marketing.events.append_event",
        new=AsyncMock(),
    ) as mock_append:
        await emit_marketing_event(
            mock_engine,
            _NAMESPACE_ID,
            EVENT_MARKETING_CASE_STUDY_DRAFTED,
            {"case_study_id": str(uuid4()), "project_id": str(uuid4())},
        )
        mock_append.assert_awaited_once()
        call_kwargs = mock_append.await_args.kwargs
        assert call_kwargs["namespace_id"] == UUID(_NAMESPACE_ID)
        assert call_kwargs["event_type"] == EVENT_MARKETING_CASE_STUDY_DRAFTED


@pytest.mark.asyncio
async def test_emit_marketing_event_pool_none() -> None:
    """Test emit_marketing_event exits silently if pg_pool is None."""
    mock_engine = MagicMock()
    mock_engine.pg_pool = None
    mock_engine.pool = None

    with patch(
        "nce.vertical_modules.marketing.events.append_event",
        new=AsyncMock(),
    ) as mock_append:
        await emit_marketing_event(
            mock_engine,
            _NAMESPACE_ID,
            EVENT_MARKETING_CASE_STUDY_DRAFTED,
            {},
        )
        mock_append.assert_not_awaited()


@pytest.mark.asyncio
async def test_emit_marketing_event_handles_exception(caplog: pytest.LogCaptureFixture) -> None:
    """Test emit_marketing_event catches and logs DB exceptions."""
    mock_engine = MagicMock()
    mock_pool = MagicMock()
    mock_pool.acquire.side_effect = RuntimeError("DB connection dropped")
    mock_engine.pg_pool = mock_pool

    await emit_marketing_event(
        mock_engine,
        _NAMESPACE_ID,
        EVENT_MARKETING_CASE_STUDY_DRAFTED,
        {},
    )
    assert "Failed to emit marketing event" in caplog.text


@pytest.mark.asyncio
async def test_get_marketing_morning_brief_slice_success() -> None:
    """Test Morning Brief throughput slice aggregates all metric pipelines."""
    mock_engine = MagicMock()
    mock_pool = MagicMock()
    mock_conn = AsyncMock()

    async def _fetchrow_mock(query: str, *args: list) -> dict | None:
        q_lower = query.lower()
        if "namespaces" in q_lower:
            return {"marketing_enabled": True}
        if "kg_nodes" in q_lower:
            return {"cnt": 12}
        return None

    async def _fetch_mock(query: str, *args: list) -> list[dict]:
        q_lower = query.lower()
        if "case_studies" in q_lower:
            return [
                {"status": "draft", "cnt": 3},
                {"status": "in_review", "cnt": 2},
                {"status": "published", "cnt": 5},
            ]
        if "testimonials" in q_lower:
            return [
                {"status": "requested", "cnt": 4},
                {"status": "approved", "cnt": 7},
            ]
        if "content_assets" in q_lower:
            return [
                {"status": "published", "cnt": 8},
                {"status": "draft", "cnt": 1},
            ]
        return []

    mock_conn.fetchrow.side_effect = _fetchrow_mock
    mock_conn.fetch.side_effect = _fetch_mock
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_engine.pg_pool = mock_pool

    result = await get_marketing_morning_brief_slice(
        mock_engine,
        {"namespace_id": _NAMESPACE_ID},
    )

    assert result["ok"] is True
    assert result["namespace_id"] == _NAMESPACE_ID
    throughput = result["story_throughput"]
    assert throughput["harvested_candidates"] == 12
    assert throughput["drafts_in_review"] == 5
    assert throughput["published_case_studies"] == 5
    assert throughput["pending_testimonials"] == 4
    assert throughput["approved_testimonials"] == 7
    assert throughput["published_assets"] == 8


@pytest.mark.asyncio
async def test_get_marketing_morning_brief_slice_guard_and_validation() -> None:
    """Test Morning Brief slice validates namespace and respects marketing_enabled."""
    mock_engine = MagicMock()

    with pytest.raises(ValueError, match="namespace_id is required"):
        await get_marketing_morning_brief_slice(mock_engine, {})

    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"marketing_enabled": False}
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_engine.pg_pool = mock_pool

    with pytest.raises(MarketingDisabledError):
        await get_marketing_morning_brief_slice(
            mock_engine,
            {"namespace_id": _NAMESPACE_ID},
        )


@pytest.mark.asyncio
async def test_do_query_marketing_a2a_success() -> None:
    """Test A2A query returns approved and published proof with citations."""
    mock_engine = MagicMock()
    mock_pool = MagicMock()
    mock_conn = AsyncMock()

    cs_id = uuid4()
    mock_conn.fetchrow.return_value = {"marketing_enabled": True}
    mock_conn.fetch.return_value = [
        {
            "id": cs_id,
            "title": "Nordic Finance Boardroom Transformation",
            "anonymized": True,
            "marketing_source_id": "cs-nordic-01",
        }
    ]
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_engine.pg_pool = mock_pool

    result = await do_query_marketing_a2a(
        mock_engine,
        {"namespace_id": _NAMESPACE_ID, "limit": 10},
    )

    assert result["ok"] is True
    assert result["count"] == 1
    items = result["evidence_items"]
    assert len(items) == 1
    assert items[0]["id"] == str(cs_id)
    assert items[0]["title"] == "Nordic Finance Boardroom Transformation"
    assert items[0]["anonymized"] is True
    assert items[0]["marketing_source_id"] == "cs-nordic-01"


@pytest.mark.asyncio
async def test_do_query_marketing_a2a_guard_and_validation() -> None:
    """Test A2A query validates namespace and respects marketing_enabled."""
    mock_engine = MagicMock()

    with pytest.raises(ValueError, match="namespace_id is required"):
        await do_query_marketing_a2a(mock_engine, {})

    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"marketing_enabled": False}
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_engine.pg_pool = mock_pool

    with pytest.raises(MarketingDisabledError):
        await do_query_marketing_a2a(
            mock_engine,
            {"namespace_id": _NAMESPACE_ID},
        )
