"""Unit tests verifying Business Insights audit and lifecycle event emission.

Ensures that:
1. ``record_ledger_audit`` calls ``append_event`` with event_type="business_insights_access_audited"
   inside an explicit database transaction.
2. ``emit_business_insights_event`` wraps ``append_event`` inside an explicit database transaction
   across all supported event types.
3. ``append_event`` fails loudly with ``EventLogError`` when called outside an active transaction.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from nce.event_log import EventLogError, append_event
from nce.vertical_modules.business_insights.events import (
    EVENT_BUSINESS_INSIGHTS_BOARD_PACK_DRAFTED,
    EVENT_BUSINESS_INSIGHTS_BRIEFING_GENERATED,
    EVENT_BUSINESS_INSIGHTS_FINDING_SURFACED,
    EVENT_BUSINESS_INSIGHTS_SCENARIO_EXECUTED,
    emit_business_insights_event,
)
from nce.vertical_modules.business_insights.provenance import record_ledger_audit
from nce.vertical_modules.marketing.events import emit_marketing_event


class _MockTransactionContext:
    """Mock async context manager for conn.transaction()."""

    def __init__(self, conn: _MockConnection) -> None:
        self.conn = conn

    async def __aenter__(self) -> _MockTransactionContext:
        self.conn._in_tx = True
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.conn._in_tx = False


class _MockConnection:
    """Mock asyncpg.Connection enforcing transaction checks."""

    def __init__(self) -> None:
        self._in_tx = False
        self.execute = AsyncMock(return_value="OK")
        self.fetch = AsyncMock(return_value=[])
        self.fetchrow = AsyncMock(return_value=None)
        self.fetchval = AsyncMock(return_value=None)

    def is_in_transaction(self) -> bool:
        return self._in_tx

    def transaction(self) -> _MockTransactionContext:
        return _MockTransactionContext(self)


class _MockPoolContext:
    def __init__(self, conn: _MockConnection) -> None:
        self.conn = conn

    async def __aenter__(self) -> _MockConnection:
        return self.conn

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


class _MockPool:
    def __init__(self, conn: _MockConnection) -> None:
        self.conn = conn

    def acquire(self) -> _MockPoolContext:
        return _MockPoolContext(self.conn)


class _MockEngine:
    def __init__(self, pool: _MockPool) -> None:
        self.pg_pool = pool


@pytest.mark.asyncio
async def test_record_ledger_audit_emits_event_in_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify record_ledger_audit calls append_event inside an active transaction."""
    conn = _MockConnection()
    ns_id = uuid4()
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

    monkeypatch.setattr(
        "nce.vertical_modules.business_insights.provenance.append_event", fake_append_event
    )

    await record_ledger_audit(
        conn=conn,
        namespace_id=ns_id,
        actor="bi_agent_007",
        action="QUERY_PROVENANCE",
        referenced_nodes=["node:bi:123", "node:bi:456"],
        details={"query": "cashflow_runway"},
    )

    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["namespace_id"] == ns_id
    assert call["agent_id"] == "business_insights_engine"
    assert call["event_type"] == "business_insights_access_audited"
    assert call["params"]["actor"] == "bi_agent_007"
    assert call["params"]["action"] == "QUERY_PROVENANCE"
    assert call["params"]["referenced_nodes"] == ["node:bi:123", "node:bi:456"]
    assert call["params"]["details"] == {"query": "cashflow_runway"}
    assert "entry_id" in call["params"]
    assert "recorded_at" in call["params"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    [
        EVENT_BUSINESS_INSIGHTS_BRIEFING_GENERATED,
        EVENT_BUSINESS_INSIGHTS_FINDING_SURFACED,
        EVENT_BUSINESS_INSIGHTS_SCENARIO_EXECUTED,
        EVENT_BUSINESS_INSIGHTS_BOARD_PACK_DRAFTED,
        "business_insights_access_audited",
    ],
)
async def test_emit_business_insights_event_all_types(
    event_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify emit_business_insights_event wraps all event types in transactions."""
    conn = _MockConnection()
    pool = _MockPool(conn)
    engine = _MockEngine(pool)
    ns_id = uuid4()
    captured_calls: list[dict[str, Any]] = []

    async def fake_append_event(
        conn: Any,
        namespace_id: Any,
        agent_id: str,
        event_type: str,
        params: dict[str, Any],
        **kwargs: Any,
    ) -> MagicMock:
        assert conn.is_in_transaction(), f"Event {event_type} called outside transaction!"
        captured_calls.append(
            {
                "event_type": event_type,
                "params": params,
            }
        )
        res = MagicMock()
        res.event_id = uuid4()
        res.event_seq = 1
        return res

    monkeypatch.setattr(
        "nce.vertical_modules.business_insights.events.append_event", fake_append_event
    )

    params = {"kpi": "arr", "value": 100000.0}
    await emit_business_insights_event(
        engine=engine,
        namespace_id=ns_id,
        event_type=event_type,
        params=params,
    )

    assert len(captured_calls) == 1
    assert captured_calls[0]["event_type"] == event_type
    assert captured_calls[0]["params"] == params


@pytest.mark.asyncio
async def test_emit_marketing_event_wraps_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify emit_marketing_event wraps append_event in a transaction."""
    conn = _MockConnection()
    pool = _MockPool(conn)
    engine = _MockEngine(pool)
    ns_id = uuid4()
    captured_calls: list[dict[str, Any]] = []

    async def fake_append_event(
        conn: Any,
        namespace_id: Any,
        event_type: str,
        params: dict[str, Any],
        **kwargs: Any,
    ) -> MagicMock:
        assert conn.is_in_transaction(), "marketing event called outside transaction!"
        captured_calls.append({"event_type": event_type, "params": params})
        res = MagicMock()
        res.event_id = uuid4()
        res.event_seq = 1
        return res

    monkeypatch.setattr("nce.vertical_modules.marketing.events.append_event", fake_append_event)

    await emit_marketing_event(
        engine=engine,
        namespace_id=ns_id,
        event_type="marketing_content_published",
        params={"content_id": "c123"},
    )

    assert len(captured_calls) == 1
    assert captured_calls[0]["event_type"] == "marketing_content_published"


@pytest.mark.asyncio
async def test_append_event_raises_when_outside_transaction() -> None:
    """Verify that the core append_event strictly refuses calls without a transaction."""
    conn = _MockConnection()
    assert not conn.is_in_transaction()

    with pytest.raises(EventLogError, match="must be called inside an active transaction"):
        await append_event(
            conn=conn,  # type: ignore[arg-type]
            namespace_id=UUID("00000000-0000-0000-0000-000000000001"),
            agent_id="test_agent",
            event_type="business_insights_briefing_generated",
            params={"briefing_id": "b1"},
        )
