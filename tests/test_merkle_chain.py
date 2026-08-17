"""
tests/test_merkle_chain.py

Unit coverage for Merkle tree hash chaining in nce.event_log.

Exercises:
  - Genesis event receives chain_hash ≠ zero sentinel
  - Two sequential events have cryptographically linked chain hashes
  - verify_merkle_chain passes for a pristine chain
  - verify_merkle_chain detects tampered middle record (content altered)
  - verify_merkle_chain detects inserted record (extra event injected)
  - verify_merkle_chain detects deleted record (event removed)
  - Middle-record tampering breaks ALL subsequent chain hashes forward
  - _compute_content_hash is deterministic
  - _compute_chain_hash ordering is content || previous (not previous || content)
  - _GENESIS_SENTINEL is exactly 32 zero bytes
  - Empty namespace returns valid=True with checked=0
  - Partial-range verification (start_seq > 1) anchors correctly
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from nce import event_log as event_log_mod
from nce.event_log import (
    _GENESIS_SENTINEL,
    _compute_chain_hash,
    _compute_content_hash,
    append_event,
    verify_merkle_chain,
)
from tests.fixtures.event_log_params import minimal_store_memory_params
from tests.fixtures.fake_asyncpg import RecordingFakeConnection

_RAW_SIGNING_SECRET = hashlib.sha256(b"pytest-merkle-hmac-secret").digest()


@pytest.fixture(autouse=True)
def _patch_active_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent real signing_keys table access during append_event."""

    async def _fake_active_key(_conn: object) -> tuple[str, bytes]:
        return ("pytest-merkle-key-id", _RAW_SIGNING_SECRET)

    monkeypatch.setattr(event_log_mod, "get_active_key", _fake_active_key)


@pytest.fixture
def namespace_id() -> UUID:
    return uuid4()


# ---------------------------------------------------------------------------
# Unit: _compute_content_hash is deterministic
# ---------------------------------------------------------------------------


def test_content_hash_deterministic() -> None:
    """Same inputs → same SHA-256 hash, regardless of call count."""
    fields = {
        "event_id": "a" * 36,
        "namespace_id": "b" * 36,
        "agent_id": "agent-1",
        "event_type": "store_memory",
        "event_seq": 1,
        "occurred_at": "2026-05-08T10:00:00+00:00",
        "params": {"memory_id": "c" * 36},
    }
    h1 = _compute_content_hash(signing_fields=fields)
    h2 = _compute_content_hash(signing_fields=fields)
    assert h1 == h2
    assert len(h1) == 32
    assert isinstance(h1, bytes)


def test_content_hash_differs_on_different_fields() -> None:
    """Different fields produce different content hashes."""
    fields_a = {
        "event_id": "a" * 36,
        "namespace_id": "b" * 36,
        "agent_id": "agent-1",
        "event_type": "store_memory",
        "event_seq": 1,
        "occurred_at": "2026-05-08T10:00:00+00:00",
        "params": {"x": 1},
    }
    fields_b = dict(fields_a)
    fields_b["params"] = {"x": 2}
    assert _compute_content_hash(signing_fields=fields_a) != _compute_content_hash(
        signing_fields=fields_b
    )


def test_content_hash_includes_parent_event_id_when_present() -> None:
    """parent_event_id, when provided, is part of the content hash."""
    without_parent = {
        "event_id": "a" * 36,
        "namespace_id": "b" * 36,
        "agent_id": "agent-1",
        "event_type": "store_memory",
        "event_seq": 1,
        "occurred_at": "2026-05-08T10:00:00+00:00",
        "params": {"x": 1},
    }
    with_parent = dict(without_parent)
    with_parent["parent_event_id"] = "parent-uuid-00000000000000000000"
    h_no = _compute_content_hash(signing_fields=without_parent)
    h_yes = _compute_content_hash(signing_fields=with_parent)
    assert h_no != h_yes


# ---------------------------------------------------------------------------
# Unit: _compute_chain_hash ordering
# ---------------------------------------------------------------------------


def test_chain_hash_is_content_concat_previous() -> None:
    """chain_hash = SHA-256(content_hash || previous_chain_hash)."""
    content = b"\x01" * 32
    prev = b"\x02" * 32

    expected = hashlib.sha256(content + prev).digest()
    actual = _compute_chain_hash(content_hash=content, previous_chain_hash=prev)

    assert actual == expected

    # Verify ordering: SHA-256(prev || content) should be DIFFERENT.
    swapped = hashlib.sha256(prev + content).digest()
    assert actual != swapped, (
        "chain_hash order must be content || previous, not previous || content"
    )


# ---------------------------------------------------------------------------
# Unit: _GENESIS_SENTINEL
# ---------------------------------------------------------------------------


def test_genesis_sentinel_is_32_zero_bytes() -> None:
    """The genesis sentinel is exactly 32 zero bytes."""
    assert _GENESIS_SENTINEL == b"\x00" * 32
    assert len(_GENESIS_SENTINEL) == 32
    assert all(b == 0 for b in _GENESIS_SENTINEL)


# ---------------------------------------------------------------------------
# Integration: append_event chain_hash wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_genesis_event_has_nonzero_chain_hash(namespace_id: UUID) -> None:
    """The first event in a namespace gets chain_hash ≠ zero sentinel."""
    conn = RecordingFakeConnection()

    async with conn.transaction():
        await append_event(
            conn=conn,
            namespace_id=namespace_id,
            agent_id="genesis-agent",
            event_type="store_memory",
            params=minimal_store_memory_params(),
        )

    record = conn.event_inserts[0]
    assert "chain_hash" in record
    chain_hash = record["chain_hash"]
    assert isinstance(chain_hash, bytes)
    assert len(chain_hash) == 32
    assert chain_hash != _GENESIS_SENTINEL, (
        "Genesis event must not store the sentinel as its chain_hash"
    )


@pytest.mark.asyncio
async def test_two_events_have_linked_chain_hashes(namespace_id: UUID) -> None:
    """The second event's chain_hash depends on the first event's data."""
    conn = RecordingFakeConnection()
    params1 = minimal_store_memory_params(seq_for_test=1)
    params2 = minimal_store_memory_params(seq_for_test=2)

    async with conn.transaction():
        await append_event(
            conn=conn,
            namespace_id=namespace_id,
            agent_id="agent-link",
            event_type="store_memory",
            params=params1,
        )
        r2 = await append_event(
            conn=conn,
            namespace_id=namespace_id,
            agent_id="agent-link",
            event_type="store_memory",
            params=params2,
        )

    record1 = conn.event_inserts[0]
    record2 = conn.event_inserts[1]

    # Build signing fields for event 2, compute expected chain hash
    fields2 = event_log_mod._build_signing_fields(
        event_id=r2.event_id,
        namespace_id=namespace_id,
        agent_id="agent-link",
        event_type="store_memory",
        event_seq=r2.event_seq,
        occurred_at_iso=r2.occurred_at.isoformat(),
        params=params2,
        parent_event_id=None,
        prev_chain_hash_hex=record1["chain_hash"].hex(),
    )
    content_hash2 = _compute_content_hash(signing_fields=fields2)
    expected_chain2 = _compute_chain_hash(
        content_hash=content_hash2,
        previous_chain_hash=record1["chain_hash"],
    )

    assert record2["chain_hash"] == expected_chain2, (
        "Event 2 chain_hash must be SHA-256(content_hash(event2) || chain_hash(event1))"
    )


# ---------------------------------------------------------------------------
# verify_merkle_chain — valid chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_merkle_chain_passes_for_pristine_chain(
    namespace_id: UUID,
) -> None:
    """A chain of 5 events that were correctly appended passes verification."""
    conn = RecordingFakeConnection()

    async with conn.transaction():
        for i in range(5):
            await append_event(
                conn=conn,
                namespace_id=namespace_id,
                agent_id="verifier",
                event_type="store_memory",
                params=minimal_store_memory_params(idx=i),
            )

    result = await verify_merkle_chain(conn, namespace_id=namespace_id)
    assert result["valid"] is True
    assert result["checked"] == 5
    assert result["first_break"] is None
    assert result["last_verified_seq"] == 5


@pytest.mark.asyncio
async def test_verify_merkle_chain_empty_namespace_returns_valid(
    namespace_id: UUID,
) -> None:
    """An empty namespace returns valid=True with checked=0."""
    conn = RecordingFakeConnection()
    result = await verify_merkle_chain(conn, namespace_id=namespace_id)
    assert result["valid"] is True
    assert result["checked"] == 0
    assert result["first_break"] is None


# ---------------------------------------------------------------------------
# verify_merkle_chain — tampering detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_merkle_chain_detects_tampered_middle_record(
    namespace_id: UUID,
) -> None:
    """Altering the params of a middle record breaks its chain_hash and all
    subsequent ones."""
    conn = RecordingFakeConnection()

    async with conn.transaction():
        for i in range(5):
            await append_event(
                conn=conn,
                namespace_id=namespace_id,
                agent_id="sabotage-test",
                event_type="store_memory",
                params=minimal_store_memory_params(idx=i),
            )

    # Tamper with event_seq=3: change params
    tampered = conn.event_inserts[2]  # 0-indexed, seq=3
    tampered["params"] = json.dumps({"idx": 999, "injected": True}, sort_keys=True)

    result = await verify_merkle_chain(conn, namespace_id=namespace_id)
    assert result["valid"] is False
    assert result["first_break"] == 3, (
        f"Expected first break at seq 3 (tampered record), got {result['first_break']}"
    )


@pytest.mark.asyncio
async def test_verify_merkle_chain_middle_tampering_breaks_all_subsequent(
    namespace_id: UUID,
) -> None:
    """When event 3 is tampered, events 4 and 5 also fail — the break cascades
    forward through the entire chain."""
    conn = RecordingFakeConnection()

    async with conn.transaction():
        for i in range(5):
            await append_event(
                conn=conn,
                namespace_id=namespace_id,
                agent_id="cascade-test",
                event_type="store_memory",
                params=minimal_store_memory_params(idx=i),
            )

    # Tamper with event_seq=3
    tampered = conn.event_inserts[2]
    tampered["params"] = json.dumps({"idx": 999, "malicious": "yes"}, sort_keys=True)

    result = await verify_merkle_chain(conn, namespace_id=namespace_id)
    assert result["valid"] is False
    assert result["first_break"] == 3
    assert result["checked"] == 5, "All events must be checked even after a break is found"

    # Verify that events 1 and 2 are still valid (chain unbroken to that point)
    # by doing a partial verify from start_seq=1 to end_seq=2
    partial = await verify_merkle_chain(conn, namespace_id=namespace_id, start_seq=1, end_seq=2)
    assert partial["valid"] is True
    assert partial["checked"] == 2


@pytest.mark.asyncio
async def test_verify_merkle_chain_detects_inserted_record(namespace_id: UUID) -> None:
    """Injecting an extra record between two legit events breaks the chain."""
    conn = RecordingFakeConnection()

    async with conn.transaction():
        await append_event(
            conn=conn,
            namespace_id=namespace_id,
            agent_id="insert-test",
            event_type="store_memory",
            params=minimal_store_memory_params(idx=0),
        )
        await append_event(
            conn=conn,
            namespace_id=namespace_id,
            agent_id="insert-test",
            event_type="store_memory",
            params=minimal_store_memory_params(idx=1),
        )

    # Insert a forged record between seq=1 and seq=2 (manually crafting chain_hash
    # to look valid in isolation — but it won't link properly)
    forged = {
        "id": uuid4(),
        "namespace_id": namespace_id,
        "agent_id": "attacker",
        "event_type": "store_memory",
        "event_seq": 2,
        "occurred_at": datetime(2026, 5, 8, 11, 0, 0, tzinfo=timezone.utc),
        "params": json.dumps({"malicious": True}, sort_keys=True),
        "chain_hash": b"\xde\xad" * 16,  # fake hash
    }
    # Bump seq of the original event 2 → 3
    conn.event_inserts[1]["event_seq"] = 3
    # Re-sort and insert forged record
    conn.event_inserts.insert(1, forged)

    result = await verify_merkle_chain(conn, namespace_id=namespace_id)
    assert result["valid"] is False
    assert result["first_break"] == 2, "Forged insertion at seq 2 must be detected"


@pytest.mark.asyncio
async def test_verify_merkle_chain_detects_deleted_record(namespace_id: UUID) -> None:
    """Removing a middle record breaks the chain at that point."""
    conn = RecordingFakeConnection()

    async with conn.transaction():
        for i in range(4):
            await append_event(
                conn=conn,
                namespace_id=namespace_id,
                agent_id="delete-test",
                event_type="store_memory",
                params=minimal_store_memory_params(idx=i),
            )

    # Remove event_seq=2 (index 1) and renumber subsequent events
    del conn.event_inserts[1]
    for i in range(1, len(conn.event_inserts)):
        conn.event_inserts[i]["event_seq"] = i + 1

    result = await verify_merkle_chain(conn, namespace_id=namespace_id)
    assert result["valid"] is False
    assert result["first_break"] == 2, "Deletion of seq 2 must break the chain at seq 2"


# ---------------------------------------------------------------------------
# verify_merkle_chain — partial range verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_range_verification_from_mid_chain(namespace_id: UUID) -> None:
    """Verifying from start_seq > 1 uses seq-1's chain_hash as anchor."""
    conn = RecordingFakeConnection()

    async with conn.transaction():
        for i in range(5):
            await append_event(
                conn=conn,
                namespace_id=namespace_id,
                agent_id="partial-test",
                event_type="store_memory",
                params=minimal_store_memory_params(idx=i),
            )

    # Verify only events 3-5 — should pass since chain is pristine
    result = await verify_merkle_chain(conn, namespace_id=namespace_id, start_seq=3, end_seq=5)
    assert result["valid"] is True
    assert result["checked"] == 3
    assert result["last_verified_seq"] == 5


@pytest.mark.asyncio
async def test_partial_range_anchored_on_tampered_predecessor_still_breaks(
    namespace_id: UUID,
) -> None:
    """Even in a partial range, if seq 5 is tampered, verification breaks."""
    conn = RecordingFakeConnection()

    async with conn.transaction():
        for i in range(7):
            await append_event(
                conn=conn,
                namespace_id=namespace_id,
                agent_id="partial-break",
                event_type="store_memory",
                params=minimal_store_memory_params(idx=i),
            )

    # Tamper with seq 5
    conn.event_inserts[4]["params"] = json.dumps({"idx": 999}, sort_keys=True)

    result = await verify_merkle_chain(conn, namespace_id=namespace_id, start_seq=3, end_seq=6)
    assert result["valid"] is False
    assert result["first_break"] == 5


@pytest.mark.asyncio
async def test_genesis_sentinel_used_for_seq1_in_chain_verification(
    namespace_id: UUID,
) -> None:
    """Confirm that verify_merkle_chain uses _GENESIS_SENTINEL as previous
    for event_seq=1."""
    conn = RecordingFakeConnection()

    async with conn.transaction():
        await append_event(
            conn=conn,
            namespace_id=namespace_id,
            agent_id="genesis-verify",
            event_type="store_memory",
            params=minimal_store_memory_params(first=True),
        )

    # Physically verify the chain starts from genesis sentinel
    record = conn.event_inserts[0]
    fields = event_log_mod._build_signing_fields(
        event_id=record["id"],
        namespace_id=record["namespace_id"],
        agent_id=record["agent_id"],
        event_type=record["event_type"],
        event_seq=record["event_seq"],
        occurred_at_iso=record["occurred_at"].isoformat(),
        params=json.loads(record["params"]),
        parent_event_id=None,
        prev_chain_hash_hex=_GENESIS_SENTINEL.hex(),
    )
    content_h = _compute_content_hash(signing_fields=fields)
    expected = _compute_chain_hash(content_hash=content_h, previous_chain_hash=_GENESIS_SENTINEL)
    assert record["chain_hash"] == expected

    # verify_merkle_chain should also pass
    result = await verify_merkle_chain(conn, namespace_id=namespace_id)
    assert result["valid"] is True
