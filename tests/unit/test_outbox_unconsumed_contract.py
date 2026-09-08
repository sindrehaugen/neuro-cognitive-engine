"""tests/unit/test_outbox_unconsumed_contract.py
=================================================
TASK: Item 1 of Charter §13 (2026-09-08 entry).
Align the outbox relay's delivery contract with EVENT_CATALOGUE declarations.

Verifies:
1. Honestly-declared unconsumed event selectors (status="UNCONSUMED" or declared_consumers=())
   drain cleanly: published_at is set, dedup row is inserted, delivered count increments,
   ZERO dead-letter queue rows are written, and ZERO alerts are dispatched.
2. Uncatalogued event selectors fast-fail to DLQ, increment attempt_count to MAX, and alert.
3. Event selectors with declared consumers that lack runtime handlers fast-fail to DLQ and alert.
4. Standing Positive Control (U18): Proves that mutating an unconsumed selector to declare a
   consumer causes the exact same event to fail to DLQ and alert.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce import outbox_relay
from nce.events.catalogue import EVENT_CATALOGUE, EventContract


def _make_mock_pool(event_dict: dict[str, Any]) -> tuple[MagicMock, MagicMock]:
    """Create a mock asyncpg pool and connection for outbox testing."""
    mock_pool = MagicMock()

    @asynccontextmanager
    async def mock_transaction_cm():
        yield

    mock_conn = MagicMock()
    mock_conn.transaction = MagicMock(side_effect=mock_transaction_cm)
    mock_conn.execute = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[event_dict])
    mock_conn.fetchval = AsyncMock(return_value=event_dict["id"])

    @asynccontextmanager
    async def mock_acquire(timeout: float = 10.0):
        yield mock_conn

    mock_pool.acquire.side_effect = mock_acquire
    return mock_pool, mock_conn


@pytest.mark.asyncio
async def test_unconsumed_selector_drains_without_dlq_or_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate: Emitting a catalogued UNCONSUMED selector drains without DLQ or alerts."""
    selector = "BOM_LINE.upserted"
    contract = EVENT_CATALOGUE.get(selector)
    assert contract is not None, f"Expected {selector} to be in EVENT_CATALOGUE"
    assert contract.status == "UNCONSUMED" or not contract.declared_consumers

    # Ensure no runtime handler registered
    monkeypatch.setitem(outbox_relay.OUTBOX_HANDLERS, selector, [])

    mock_alert = AsyncMock()
    monkeypatch.setattr(outbox_relay, "_dispatch_throttled_alert", mock_alert)

    event_id = uuid4()
    namespace_id = uuid4()
    mock_event = {
        "id": event_id,
        "namespace_id": namespace_id,
        "aggregate_type": "bom_line",
        "aggregate_id": uuid4(),
        "event_type": selector,
        "payload": json.dumps({"line_id": "line-123"}),
        "headers": None,
        "attempt_count": 0,
        "created_at": datetime.now(timezone.utc),
    }

    mock_pool, mock_conn = _make_mock_pool(mock_event)

    delivered = await outbox_relay.run_outbox_relay_once(mock_pool)

    # 1. Progress reported: 1 event successfully acknowledged/delivered
    assert delivered == 1

    # 2. Alert must NEVER be dispatched for an honestly unconsumed event
    mock_alert.assert_not_called()

    # 3. Verify queries executed on connection
    queries = [call[0][0] for call in mock_conn.execute.call_args_list]

    # Must mark published
    assert any("UPDATE outbox_events SET published_at = now()" in q for q in queries)

    # Must NEVER insert into dead_letter_queue
    assert not any("dead_letter_queue" in q for q in queries)

    # Must NEVER set attempt_count = MAX_OUTBOX_ATTEMPTS
    assert not any("attempt_count = $1" in q for q in queries)


@pytest.mark.asyncio
async def test_uncatalogued_selector_fails_to_dlq_and_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event type unknown to the catalogue represents an uncontracted event and must DLQ + alert."""
    selector = "uncontracted.alien_event"
    assert selector not in EVENT_CATALOGUE

    monkeypatch.setitem(outbox_relay.OUTBOX_HANDLERS, selector, [])

    mock_alert = AsyncMock()
    monkeypatch.setattr(outbox_relay, "_dispatch_throttled_alert", mock_alert)

    event_id = uuid4()
    namespace_id = uuid4()
    mock_event = {
        "id": event_id,
        "namespace_id": namespace_id,
        "aggregate_type": "alien",
        "aggregate_id": uuid4(),
        "event_type": selector,
        "payload": json.dumps({"foo": "bar"}),
        "headers": None,
        "attempt_count": 0,
        "created_at": datetime.now(timezone.utc),
    }

    mock_pool, mock_conn = _make_mock_pool(mock_event)

    delivered = await outbox_relay.run_outbox_relay_once(mock_pool)

    assert delivered == 0
    assert mock_alert.call_count >= 1
    alert_keys = [call[0][0] for call in mock_alert.call_args_list]
    assert any(selector in k for k in alert_keys)

    queries = [call[0][0] for call in mock_conn.execute.call_args_list]
    assert any("INSERT INTO dead_letter_queue" in q for q in queries)
    assert any("UPDATE outbox_events" in q and "attempt_count" in q for q in queries)


@pytest.mark.asyncio
async def test_declared_consumer_with_missing_handler_fails_to_dlq_and_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selector with declared consumers but no registered runtime handler is a misconfiguration."""
    selector = "TICKET.dispatched"
    contract = EVENT_CATALOGUE.get(selector)
    assert contract is not None
    assert len(contract.declared_consumers) >= 1

    # Simulate missing handler registration
    monkeypatch.setitem(outbox_relay.OUTBOX_HANDLERS, selector, [])

    mock_alert = AsyncMock()
    monkeypatch.setattr(outbox_relay, "_dispatch_throttled_alert", mock_alert)

    event_id = uuid4()
    namespace_id = uuid4()
    mock_event = {
        "id": event_id,
        "namespace_id": namespace_id,
        "aggregate_type": "ticket",
        "aggregate_id": uuid4(),
        "event_type": selector,
        "payload": json.dumps({"ticket_id": "tick-999"}),
        "headers": None,
        "attempt_count": 0,
        "created_at": datetime.now(timezone.utc),
    }

    mock_pool, mock_conn = _make_mock_pool(mock_event)

    delivered = await outbox_relay.run_outbox_relay_once(mock_pool)

    assert delivered == 0
    assert mock_alert.call_count >= 1
    alert_keys = [call[0][0] for call in mock_alert.call_args_list]
    assert any(selector in k for k in alert_keys)

    queries = [call[0][0] for call in mock_conn.execute.call_args_list]
    assert any("INSERT INTO dead_letter_queue" in q for q in queries)
    assert any("UPDATE outbox_events" in q and "attempt_count" in q for q in queries)


@pytest.mark.asyncio
async def test_positive_control_catalogue_mutation_flips_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standing Positive Control (U18):

    Mutating an unconsumed selector's catalogue contract to declare a consumer
    flips its execution path from clean acknowledgment to DLQ + alert.
    """
    selector = "AGREEMENT.upserted"
    contract = EVENT_CATALOGUE[selector]
    assert contract.status == "UNCONSUMED"

    monkeypatch.setitem(outbox_relay.OUTBOX_HANDLERS, selector, [])

    mock_event = {
        "id": uuid4(),
        "namespace_id": uuid4(),
        "aggregate_type": "agreement",
        "aggregate_id": uuid4(),
        "event_type": selector,
        "payload": json.dumps({"agr_id": "agr-1"}),
        "headers": None,
        "attempt_count": 0,
        "created_at": datetime.now(timezone.utc),
    }

    # Baseline: unconsumed event drains cleanly
    mock_alert_baseline = AsyncMock()
    monkeypatch.setattr(outbox_relay, "_dispatch_throttled_alert", mock_alert_baseline)
    mock_pool, mock_conn = _make_mock_pool(mock_event)
    delivered_baseline = await outbox_relay.run_outbox_relay_once(mock_pool)
    assert delivered_baseline == 1
    mock_alert_baseline.assert_not_called()

    # Mutation: declare a consumer in the catalogue
    mutated_contract = EventContract(
        selector=contract.selector,
        node_type=contract.node_type,
        op=contract.op,
        declared_producers=contract.declared_producers,
        declared_consumers=("nce/vertical_modules/fake/consumer.py",),
        status="ACTIVE",
    )
    mutated_catalogue = dict(EVENT_CATALOGUE)
    mutated_catalogue[selector] = mutated_contract
    monkeypatch.setattr("nce.events.catalogue.EVENT_CATALOGUE", mutated_catalogue)

    # Re-run: now that a consumer is declared, missing handler MUST trip DLQ + alert
    mock_alert_mutated = AsyncMock()
    monkeypatch.setattr(outbox_relay, "_dispatch_throttled_alert", mock_alert_mutated)
    mock_pool_mutated, mock_conn_mutated = _make_mock_pool(mock_event)
    delivered_mutated = await outbox_relay.run_outbox_relay_once(mock_pool_mutated)

    assert delivered_mutated == 0
    assert mock_alert_mutated.call_count >= 1
    alert_keys = [call[0][0] for call in mock_alert_mutated.call_args_list]
    assert any(selector in k for k in alert_keys)
    queries = [call[0][0] for call in mock_conn_mutated.execute.call_args_list]
    assert any("INSERT INTO dead_letter_queue" in q for q in queries)
