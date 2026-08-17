"""
tests/test_event_log_append.py

Unit coverage for nce.event_log.append_event using RecordingFakeConnection.

Exercises:
  - Two appends in one transaction → monotonic event_seq
  - Signature integrity: payload tampering fails verify_fields
  - Invalid event_type, blank / overlong agent_id
  - D8 backdated valid_from in params
  - UniqueViolationError → EventLogSequenceError mapping
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from nce import event_log as event_log_mod
from nce.event_log import (
    _GENESIS_SENTINEL,
    EventLogSequenceError,
    EventLogTimestampError,
    InvalidEventTypeError,
    append_event,
)
from nce.signing import verify_fields
from tests.fixtures.event_log_params import minimal_store_memory_params
from tests.fixtures.fake_asyncpg import RecordingFakeConnection

# Fixed 32-byte HMAC key — matches patched get_active_key below.
_RAW_SIGNING_SECRET = hashlib.sha256(b"pytest-event-log-hmac-secret").digest()


@pytest.fixture(autouse=True)
def _patch_active_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent real signing_keys table access during append_event."""

    async def _fake_active_key(_conn: object) -> tuple[str, bytes]:
        return ("pytest-key-id", _RAW_SIGNING_SECRET)

    monkeypatch.setattr(event_log_mod, "get_active_key", _fake_active_key)


@pytest.fixture
def namespace_id() -> UUID:
    return uuid4()


@pytest.mark.asyncio
async def test_append_two_events_increments_seq(namespace_id: UUID) -> None:
    conn = RecordingFakeConnection()
    p1 = minimal_store_memory_params()
    p2 = minimal_store_memory_params()

    async with conn.transaction():
        r1 = await append_event(
            conn=conn,
            namespace_id=namespace_id,
            agent_id="retrieval-bot",
            event_type="store_memory",
            params=p1,
        )
        r2 = await append_event(
            conn=conn,
            namespace_id=namespace_id,
            agent_id="retrieval-bot",
            event_type="store_memory",
            params=p2,
        )

    assert r1.event_seq == 1
    assert r2.event_seq == 2
    assert len(conn.event_inserts) == 2


@pytest.mark.asyncio
async def test_signature_detects_params_tampering(namespace_id: UUID) -> None:
    conn = RecordingFakeConnection()
    params_out = minimal_store_memory_params(memory_id=str(uuid4()))

    async with conn.transaction():
        res = await append_event(
            conn=conn,
            namespace_id=namespace_id,
            agent_id="agent-clean",
            event_type="store_memory",
            params=params_out,
        )

    row = conn.event_inserts[0]
    fields = event_log_mod._build_signing_fields(
        event_id=res.event_id,
        namespace_id=namespace_id,
        agent_id="agent-clean",
        event_type="store_memory",
        event_seq=res.event_seq,
        occurred_at_iso=res.occurred_at.isoformat(),
        params=params_out,
        parent_event_id=None,
        prev_chain_hash_hex=_GENESIS_SENTINEL.hex(),
    )
    assert verify_fields(fields, _RAW_SIGNING_SECRET, row["signature"]) is True

    tampered = dict(fields)
    tampered["params"] = dict(fields["params"])
    tampered["params"]["malicious"] = "injection"
    assert verify_fields(tampered, _RAW_SIGNING_SECRET, row["signature"]) is False


@pytest.mark.parametrize(
    "bad_type",
    ["delete_everything", "", "STORE_MEMORY"],
)
@pytest.mark.asyncio
async def test_invalid_event_type_raises(namespace_id: UUID, bad_type: str) -> None:
    conn = RecordingFakeConnection()
    async with conn.transaction():
        with pytest.raises(InvalidEventTypeError):
            await append_event(
                conn=conn,
                namespace_id=namespace_id,
                agent_id="x",
                event_type=bad_type,  # type: ignore[arg-type]
                params={"a": 1},
            )


@pytest.mark.parametrize(
    "bad_agent",
    ["", "   ", "a" * 129],
)
@pytest.mark.asyncio
async def test_invalid_agent_id_raises(namespace_id: UUID, bad_agent: str) -> None:
    conn = RecordingFakeConnection()
    async with conn.transaction():
        with pytest.raises(ValueError):
            await append_event(
                conn=conn,
                namespace_id=namespace_id,
                agent_id=bad_agent,
                event_type="store_memory",
                params=minimal_store_memory_params(extra_probe=1),
            )


@pytest.mark.asyncio
async def test_d8_backdated_valid_from_in_params_raises(namespace_id: UUID) -> None:
    conn = RecordingFakeConnection()
    past = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    async with conn.transaction():
        with pytest.raises(EventLogTimestampError):
            await append_event(
                conn=conn,
                namespace_id=namespace_id,
                agent_id="ok-agent",
                event_type="store_memory",
                params=minimal_store_memory_params(valid_from=past),
            )


@pytest.mark.asyncio
async def test_unique_violation_raises_event_log_sequence_error(
    namespace_id: UUID,
) -> None:
    conn = RecordingFakeConnection(simulate_unique_violation_on_insert=True)
    async with conn.transaction():
        with pytest.raises(EventLogSequenceError):
            await append_event(
                conn=conn,
                namespace_id=namespace_id,
                agent_id="solo",
                event_type="store_memory",
                params=minimal_store_memory_params(marker="sequence_violation"),
            )
