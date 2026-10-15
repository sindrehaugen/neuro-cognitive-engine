"""Invariant tests for ``EventType`` / payload-contract alignment."""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, get_args
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce import event_log as event_log_mod
from nce.event_log import (
    InvalidEventTypeError,
    _validate_event_params,
    append_event,
)
from nce.event_types import (
    EVENT_REQUIRED_PARAM_KEYS,
    VALID_EVENT_TYPES,
    EventType,
)
from nce.models import AssertionType, MemoryType, StoreMemoryRequest
from nce.orchestrators import memory as memory_mod
from nce.orchestrators.memory import MemoryOrchestrator
from nce.replay import ForkedReplay
from tests.fixtures.fake_asyncpg import RecordingFakeConnection

_RAW_SIGNING_SECRET = hashlib.sha256(b"pytest-event-log-hmac-secret").digest()


def test_valid_event_types_matches_event_type_literal_members() -> None:
    """``VALID_EVENT_TYPES`` must stay wired to ``EventType`` (single source)."""
    assert VALID_EVENT_TYPES == frozenset(get_args(EventType))


def test_legacy_consolidation_type_not_registered() -> None:
    """Old ``consolidation`` string must not creep back into the allowed set."""
    assert "consolidation" not in VALID_EVENT_TYPES
    assert "consolidation_run" in VALID_EVENT_TYPES


@pytest.mark.parametrize(
    ("event_type", "params", "snippet"),
    [
        (
            "unredact",
            {"memory_id": "m", "pii_redaction": "oops"},
            "forbidden param keys present",
        ),
        (
            "a2a_grant_created",
            {
                "grant_id": "g",
                "target_agent_id": None,
                "scope_count": 0,
                "expires_at": "2099-01-01",
                "sharing_token": "leak",
            },
            "forbidden param keys present",
        ),
    ],
)
def test_forbidden_param_keys_rejected(event_type: str, params: dict, snippet: str) -> None:
    with pytest.raises(ValueError, match=snippet):
        _validate_event_params(event_type, params)


def test_required_param_catalog_non_empty_where_expected() -> None:
    """Sanity: tightened types must remain declared (catches typo'd event names)."""
    for et in ("store_memory", "saga_recovered", "store_memory_rolled_back"):
        assert et in EVENT_REQUIRED_PARAM_KEYS


def test_forked_replay_init_has_full_handler_registry() -> None:
    """Construction must not raise: every ``EventType`` maps to a handler."""
    ForkedReplay(AsyncMock())  # pool unused during validation


@pytest.mark.asyncio
async def test_append_rejects_legacy_consolidation_event_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``consolidation`` is retired; callers must emit ``consolidation_run``."""

    async def _fake_active_key(_conn: object) -> tuple[str, bytes]:
        return ("pytest-key-id", _RAW_SIGNING_SECRET)

    monkeypatch.setattr(event_log_mod, "get_active_key", _fake_active_key)

    conn = RecordingFakeConnection()

    with pytest.raises(InvalidEventTypeError, match="consolidation"):
        async with conn.transaction():
            await append_event(
                conn=conn,
                namespace_id=uuid4(),
                agent_id="a",
                event_type="consolidation",
                params={
                    "abstraction": "",
                    "key_entities": [],
                    "key_relations": [],
                    "supporting_memory_ids": [],
                    "contradicting_memory_ids": [],
                    "confidence": 0.5,
                    "source_memories": [],
                    "consolidated_memory_id": str(uuid4()),
                    "payload_ref": "507f1f77bcf86cd799439011",
                },
            )


@pytest.mark.asyncio
async def test_store_semantic_graph_pg_emits_pii_redaction_only_when_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MemoryOrchestrator._store_semantic_graph_pg`` emits ``pii_redaction``
    with ``memory_id``, ``entity_types``, ``tokens_redacted``, and
    ``parent_event_id`` when ``pii_result.redacted is True``, and omits it when
    ``pii_result.redacted is False``.
    """
    expected_memory_id = uuid4()

    @asynccontextmanager
    async def _fake_tx():
        yield

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=expected_memory_id)
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()
    conn.transaction = _fake_tx

    @asynccontextmanager
    async def _fake_scoped_pg_session(_pool: object, _ns: object):
        yield conn

    recorded_events: list[dict[str, Any]] = []

    async def _recording_append_event(**kwargs: Any) -> Any:
        _validate_event_params(kwargs["event_type"], kwargs["params"])
        recorded_events.append(kwargs)
        return kwargs.get("event_id") or uuid4()

    monkeypatch.setattr(memory_mod, "scoped_pg_session", _fake_scoped_pg_session)
    monkeypatch.setattr(event_log_mod, "append_event", _recording_append_event)

    orch = MemoryOrchestrator(
        pg_pool=MagicMock(),
        mongo_client=MagicMock(),
        redis_client=MagicMock(),
    )
    payload = StoreMemoryRequest(
        namespace_id=uuid4(),
        agent_id="test-agent",
        content="Sensitive payload",
        summary="Sanitized summary",
        heavy_payload="Sanitized heavy",
        memory_type=MemoryType.episodic,
        assertion_type=AssertionType.fact,
    )

    pii_redacted = SimpleNamespace(
        redacted=True,
        entities_found=["EMAIL", "PHONE"],
        vault_entries=[
            {"token": "[EMAIL_1]", "encrypted_value": b"enc-1", "entity_type": "EMAIL"},
            {"token": "[PHONE_1]", "encrypted_value": b"enc-2", "entity_type": "PHONE"},
        ],
    )

    returned_id = await orch._store_semantic_graph_pg(
        payload=payload,
        sanitized_summary="Sanitized summary",
        vector=[0.1] * 768,
        node_vecs=[],
        pii_result=pii_redacted,
        inserted_mongo_id="507f1f77bcf86cd799439011",
        entities=[],
        triplets=[],
        saga_id="saga-1",
        user_id="user-1",
        session_id="sess-1",
    )
    assert returned_id == expected_memory_id
    assert [e["event_type"] for e in recorded_events] == ["store_memory", "pii_redaction"]

    store_ev, pii_ev = recorded_events
    assert pii_ev["namespace_id"] == payload.namespace_id
    assert pii_ev["agent_id"] == payload.agent_id
    assert pii_ev["params"] == {
        "memory_id": str(expected_memory_id),
        "entity_types": ["EMAIL", "PHONE"],
        "tokens_redacted": 2,
    }
    assert pii_ev["parent_event_id"] == store_ev["event_id"]
    assert pii_ev["parent_event_id"] is not None

    # When pii_result.redacted is False, pii_redaction must NOT be emitted.
    recorded_events.clear()
    pii_unredacted = SimpleNamespace(
        redacted=False,
        entities_found=[],
        vault_entries=[],
    )
    await orch._store_semantic_graph_pg(
        payload=payload,
        sanitized_summary="Sanitized summary",
        vector=[0.1] * 768,
        node_vecs=[],
        pii_result=pii_unredacted,
        inserted_mongo_id="507f1f77bcf86cd799439011",
        entities=[],
        triplets=[],
        saga_id="saga-2",
        user_id="user-1",
        session_id="sess-1",
    )
    assert [e["event_type"] for e in recorded_events] == ["store_memory"]
