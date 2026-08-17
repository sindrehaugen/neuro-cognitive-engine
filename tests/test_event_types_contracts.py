"""Invariant tests for ``EventType`` / payload-contract alignment."""

from __future__ import annotations

import hashlib
from typing import get_args
from unittest.mock import AsyncMock
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
