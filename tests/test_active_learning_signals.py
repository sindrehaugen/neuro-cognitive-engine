"""
Batch 112 — signal-consequences (Muscles B1).

Integration tests asserting real DB state for the quarantine decision signals:
  - confirm_memory  → salience ≈ 0.65 + quarantine_confirmed WORM event
  - reject_memory   → payload discarded, quarantine_rejected event with
                       payload_sha256 (no raw payload in event log)
  - confirmed memory outranks a default-stored peer in retrieval ordering

Requires the isolated RL integration stack (port 5433 / 6380).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from unittest.mock import AsyncMock

import pytest

from nce.active_learning import _QUARANTINE_CONFIRMED_SALIENCE, ActiveLearningManager
from nce.db_utils import scoped_pg_session
from nce.models import AssertionType, MemoryType, StoreMemoryRequest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AGENT_ID = "batch112-agent"
_OPERATOR_ID = "batch112-operator"


def _make_payload(ns_id: uuid.UUID) -> StoreMemoryRequest:
    return StoreMemoryRequest(
        namespace_id=ns_id,
        agent_id=_AGENT_ID,
        content="Quarantined assertion for batch 112 test.",
        summary="Batch 112 test summary.",
        memory_type=MemoryType.episodic,
        assertion_type=AssertionType.fact,
        metadata={"confidence": 0.4},
    )


def _fake_mongo_objectid() -> str:
    """Return a valid 24-hex-char MongoDB ObjectId string."""
    return uuid.uuid4().hex[:24]


async def _insert_queue_item(
    pg_pool,
    ns_id: uuid.UUID,
    payload: StoreMemoryRequest,
) -> uuid.UUID:
    """Insert a pending queue item; returns its UUID."""
    serialized = payload.model_dump_json()
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        queue_id = await conn.fetchval(
            """
            INSERT INTO active_learning_queue
                (namespace_id, agent_id, payload, confidence_score, status, created_at)
            VALUES ($1::uuid, $2, $3::jsonb, $4::real, 'pending', NOW())
            RETURNING id
            """,
            ns_id,
            payload.agent_id,
            serialized,
            0.4,
        )
    assert queue_id is not None
    return queue_id


async def _insert_memory_row(
    pg_pool,
    ns_id: uuid.UUID,
    payload_ref: str,
    agent_id: str,
) -> uuid.UUID:
    """Insert a minimal memories row; returns its UUID (postgres id).

    Only ``payload_ref`` is required (no default, NOT NULL).  All other columns
    have DB defaults so we omit them for brevity.
    """
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        mem_id = await conn.fetchval(
            """
            INSERT INTO memories (namespace_id, agent_id, payload_ref)
            VALUES ($1::uuid, $2, $3)
            RETURNING id
            """,
            ns_id,
            agent_id,
            payload_ref,
        )
    assert mem_id is not None
    return mem_id


async def _get_salience(
    pg_pool,
    ns_id: uuid.UUID,
    memory_id: uuid.UUID,
    agent_id: str,
) -> float | None:
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        row = await conn.fetchrow(
            "SELECT salience_score FROM memory_salience WHERE memory_id = $1::uuid AND agent_id = $2",
            memory_id,
            agent_id,
        )
    return float(row["salience_score"]) if row else None


async def _get_event_log_row(
    pg_pool,
    ns_id: uuid.UUID,
    event_type: str,
) -> dict | None:
    """Return the most-recent event_log row of *event_type* in *ns_id*, or None."""
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT params FROM event_log
            WHERE namespace_id = $1::uuid AND event_type = $2
            ORDER BY event_seq DESC
            LIMIT 1
            """,
            ns_id,
            event_type,
        )
    if row is None:
        return None
    params = row["params"]
    if isinstance(params, str):
        return json.loads(params)
    return dict(params)


# ---------------------------------------------------------------------------
# Test 1: confirm_memory → salience ≈ 0.65 + quarantine_confirmed event
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_confirm_memory_sets_salience_and_emits_event(pg_pool, make_namespace) -> None:
    """
    confirm_memory must:
      - persist the memory with salience_score ≈ 0.65 in memory_salience
      - append a quarantine_confirmed event in event_log with the right params
    """
    ns_id = await make_namespace()
    payload = _make_payload(ns_id)

    # Insert a queue item
    queue_id = await _insert_queue_item(pg_pool, ns_id, payload)

    # Pre-insert a memories row so confirm_memory can look it up by payload_ref.
    fake_ref = _fake_mongo_objectid()
    pg_mem_id = await _insert_memory_row(pg_pool, ns_id, fake_ref, _AGENT_ID)

    # Mock orchestrator: returns the pre-inserted payload_ref so the salience
    # upsert can find the row.
    mock_orch = AsyncMock()
    mock_orch.store_memory = AsyncMock(return_value={"payload_ref": fake_ref, "quarantined": False})

    al_mgr = ActiveLearningManager(pg_pool)
    result = await al_mgr.confirm_memory(ns_id, queue_id, _OPERATOR_ID, mock_orch)

    assert result["payload_ref"] == fake_ref

    # 1a. Salience row must exist and be ≈ 0.65.
    salience = await _get_salience(pg_pool, ns_id, pg_mem_id, _AGENT_ID)
    assert salience is not None, "memory_salience row was not created"
    assert abs(salience - _QUARANTINE_CONFIRMED_SALIENCE) < 0.001, (
        f"Expected salience ≈ {_QUARANTINE_CONFIRMED_SALIENCE}, got {salience}"
    )

    # 1b. quarantine_confirmed event must exist in event_log.
    ev = await _get_event_log_row(pg_pool, ns_id, "quarantine_confirmed")
    assert ev is not None, "quarantine_confirmed event was not appended"
    assert ev["queue_item_id"] == str(queue_id)
    assert ev["agent_id"] == _AGENT_ID
    assert ev["operator_id"] == _OPERATOR_ID


# ---------------------------------------------------------------------------
# Test 2: reject_memory → queue rejected + quarantine_rejected event (hash only)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_memory_discards_payload_and_emits_event(pg_pool, make_namespace) -> None:
    """
    reject_memory must:
      - set active_learning_queue.status = 'rejected'
      - append a quarantine_rejected event carrying payload_sha256 only
      - never persist the raw payload in the event log
    """
    ns_id = await make_namespace()
    payload = _make_payload(ns_id)
    queue_id = await _insert_queue_item(pg_pool, ns_id, payload)

    al_mgr = ActiveLearningManager(pg_pool)
    await al_mgr.reject_memory(ns_id, queue_id, _OPERATOR_ID)

    # 2a. Queue status must be 'rejected'.
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        row = await conn.fetchrow(
            "SELECT status, payload FROM active_learning_queue WHERE id = $1::uuid",
            queue_id,
        )
    assert row is not None
    assert row["status"] == "rejected"

    # 2b. quarantine_rejected event must exist.
    ev = await _get_event_log_row(pg_pool, ns_id, "quarantine_rejected")
    assert ev is not None, "quarantine_rejected event was not appended"
    assert ev["queue_item_id"] == str(queue_id)
    assert ev["operator_id"] == _OPERATOR_ID

    # 2c. Event must carry payload_sha256.
    assert "payload_sha256" in ev, "quarantine_rejected event missing payload_sha256"
    sha = ev["payload_sha256"]
    assert isinstance(sha, str) and len(sha) == 64, f"payload_sha256 has unexpected format: {sha!r}"

    # 2d. Verify the sha256 is reproducible from the stored JSONB payload.
    #     asyncpg returns JSONB columns as dicts; active_learning.py serialises via
    #     json.dumps before hashing — replicate that here instead of hashing the
    #     original model_dump_json string (key ordering may differ after JSONB round-trip).
    stored_payload = row["payload"]
    if isinstance(stored_payload, str):
        stored_bytes = stored_payload.encode("utf-8")
    else:
        stored_bytes = json.dumps(stored_payload).encode("utf-8")
    expected_sha = hashlib.sha256(stored_bytes).hexdigest()
    assert sha == expected_sha, (
        f"payload_sha256 mismatch: event has {sha!r}, expected from stored JSONB {expected_sha!r}"
    )

    # 2e. WORM/PII guard: raw payload keys must NOT appear in the event params.
    forbidden_keys = {"payload", "raw_payload", "content", "summary", "heavy_payload"}
    present = forbidden_keys & set(ev.keys())
    assert not present, f"Raw payload keys found in quarantine_rejected event: {present}"


# ---------------------------------------------------------------------------
# Test 3: confirmed memory outranks a default-stored peer in retrieval
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_confirmed_memory_outranks_default_peer(pg_pool, make_namespace) -> None:
    """
    A memory confirmed through quarantine (salience ≈ 0.65) must rank above a
    peer inserted with the default salience (≤ 0.5) when ordering by
    salience_score DESC.
    """
    ns_id = await make_namespace()
    agent_id = _AGENT_ID

    # Insert the "confirmed" memory (will get 0.65 via confirm_memory).
    confirmed_ref = _fake_mongo_objectid()
    confirmed_mem_id = await _insert_memory_row(pg_pool, ns_id, confirmed_ref, agent_id)

    # Insert the "default" peer memory with an explicit 0.5 salience.
    default_ref = _fake_mongo_objectid()
    default_mem_id = await _insert_memory_row(pg_pool, ns_id, default_ref, agent_id)
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        await conn.execute(
            """
            INSERT INTO memory_salience
                (memory_id, agent_id, namespace_id, salience_score, updated_at, access_count)
            VALUES ($1::uuid, $2, $3::uuid, 0.5::real, NOW(), 1)
            ON CONFLICT (memory_id, agent_id) DO UPDATE
                SET salience_score = EXCLUDED.salience_score, updated_at = NOW()
            """,
            default_mem_id,
            agent_id,
            ns_id,
        )

    # Set up a queue item and run confirm_memory with the confirmed memory.
    payload = _make_payload(ns_id)
    queue_id = await _insert_queue_item(pg_pool, ns_id, payload)

    mock_orch = AsyncMock()
    mock_orch.store_memory = AsyncMock(
        return_value={"payload_ref": confirmed_ref, "quarantined": False}
    )
    al_mgr = ActiveLearningManager(pg_pool)
    await al_mgr.confirm_memory(ns_id, queue_id, _OPERATOR_ID, mock_orch)

    # Retrieve both memories ordered by salience_score DESC.
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        rows = await conn.fetch(
            """
            SELECT memory_id, salience_score
            FROM memory_salience
            WHERE namespace_id = $1::uuid
              AND agent_id = $2
              AND memory_id = ANY($3::uuid[])
            ORDER BY salience_score DESC
            """,
            ns_id,
            agent_id,
            [confirmed_mem_id, default_mem_id],
        )

    assert len(rows) == 2, f"Expected 2 salience rows, got {len(rows)}"
    top_id = rows[0]["memory_id"]
    top_score = float(rows[0]["salience_score"])
    bottom_score = float(rows[1]["salience_score"])

    assert top_id == confirmed_mem_id, (
        f"Confirmed memory should rank first; got {top_id} (score={top_score}) "
        f"vs default {default_mem_id} (score={bottom_score})"
    )
    assert top_score > bottom_score, (
        f"Confirmed salience {top_score} should exceed default {bottom_score}"
    )
    assert abs(top_score - _QUARANTINE_CONFIRMED_SALIENCE) < 0.001, (
        f"Confirmed salience should be ≈ {_QUARANTINE_CONFIRMED_SALIENCE}, got {top_score}"
    )
