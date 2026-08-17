"""Unit tests for nce.replay engine wiring (handler registry, mode guards)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import get_args
from unittest.mock import MagicMock

import pytest

from nce.event_types import EventType
from nce.replay import (
    ForkedReplay,
    ObservationalReplay,
    ReconstructiveReplay,
    ReplayModeError,
    _EventRow,
    _resolve_llm_payload,
)


def test_forked_replay_registers_all_event_types() -> None:
    pool = MagicMock()
    ForkedReplay(pool)  # raises ReplayHandlerMissingError if any EventType lacks a handler


def test_observational_and_reconstructive_handler_coverage() -> None:
    pool = MagicMock()
    ObservationalReplay(pool)
    ReconstructiveReplay(pool)


def test_handler_registry_matches_event_type_union() -> None:
    from nce.replay import _HANDLER_REGISTRY

    expected = frozenset(get_args(EventType))
    assert expected == frozenset(_HANDLER_REGISTRY)


@pytest.mark.asyncio
async def test_replay_mode_error_on_invalid_llm_mode() -> None:
    ns = uuid.UUID("00000000-0000-4000-8000-000000000001")
    src = _EventRow(
        event_id=uuid.uuid4(),
        event_seq=1,
        event_type="store_memory",
        occurred_at=datetime.now(timezone.utc),
        agent_id="agent-1",
        params={},
        result_summary=None,
        parent_event_id=None,
        llm_payload_uri="nce-llm-payloads/ns/event.json",
        llm_payload_hash=None,
    )

    with pytest.raises(ReplayModeError, match="Invalid replay_mode"):
        await _resolve_llm_payload(
            src,
            replay_mode="live",
            config_overrides=None,
            target_namespace_id=ns,
            source_namespace_id=ns,
        )


@pytest.mark.asyncio
async def test_replay_checksum_error_on_payload_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nce.replay as replay_mod
    from nce.replay import ReplayChecksumError

    ns = uuid.UUID("00000000-0000-4000-8000-000000000001")
    src = _EventRow(
        event_id=uuid.uuid4(),
        event_seq=1,
        event_type="store_memory",
        occurred_at=datetime.now(timezone.utc),
        agent_id="agent-1",
        params={},
        result_summary=None,
        parent_event_id=None,
        llm_payload_uri="nce-llm-payloads/ns/event.json",
        llm_payload_hash=b"invalid-hash-here-32-bytes-long",
    )

    mock_payload = {"prompt": "test prompt", "response": {"test": "response"}}

    async def fake_fetch_payload(uri: str) -> dict:
        return mock_payload

    monkeypatch.setattr(replay_mod, "_fetch_llm_payload", fake_fetch_payload)

    with pytest.raises(ReplayChecksumError, match="LLM payload hash mismatch"):
        await _resolve_llm_payload(
            src,
            replay_mode="deterministic",
            config_overrides=None,
            target_namespace_id=ns,
            source_namespace_id=ns,
        )


@pytest.mark.asyncio
async def test_replay_checksum_success_on_correct_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    import nce.replay as replay_mod
    from nce.signing import canonical_json

    ns = uuid.UUID("00000000-0000-4000-8000-000000000001")
    mock_payload = {"prompt": "test prompt", "response": {"test": "response"}}
    expected_hash = hashlib.sha256(canonical_json(mock_payload)).digest()

    src = _EventRow(
        event_id=uuid.uuid4(),
        event_seq=1,
        event_type="store_memory",
        occurred_at=datetime.now(timezone.utc),
        agent_id="agent-1",
        params={},
        result_summary=None,
        parent_event_id=None,
        llm_payload_uri="nce-llm-payloads/ns/event.json",
        llm_payload_hash=expected_hash,
    )

    async def fake_fetch_payload(uri: str) -> dict:
        return mock_payload

    async def fake_put_payload(uri: str, payload: dict) -> bytes:
        return expected_hash

    monkeypatch.setattr(replay_mod, "_fetch_llm_payload", fake_fetch_payload)
    monkeypatch.setattr(replay_mod, "_put_llm_payload", fake_put_payload)

    payload, fork_uri, fork_hash = await _resolve_llm_payload(
        src,
        replay_mode="deterministic",
        config_overrides=None,
        target_namespace_id=ns,
        source_namespace_id=ns,
    )

    assert payload == mock_payload
    assert fork_hash == expected_hash


@pytest.mark.asyncio
async def test_handle_store_memory_handler() -> None:
    import json
    from unittest.mock import AsyncMock

    from bson import ObjectId

    from nce.replay import _handle_store_memory

    mock_conn = AsyncMock()
    mock_valid_from = datetime.now(timezone.utc)
    mock_created_at = datetime.now(timezone.utc)
    # Mock conn.fetchrow for the source memories SELECT query and memory_salience SELECT query
    mock_conn.fetchrow.side_effect = [
        # First query: SELECT embedding, assertion_type, memory_type, metadata, valid_from, created_at FROM memories
        {
            "embedding": [0.1] * 768,
            "assertion_type": "fact",
            "memory_type": "episodic",
            "metadata": {"some_key": "some_val"},
            "valid_from": mock_valid_from,
            "created_at": mock_created_at,
            "wrapped_dek": None,
            "dek_key_id": None,
        },
        # Second query: SELECT salience_score FROM memory_salience
        {
            "salience_score": 0.85,
        },
    ]

    target_ns = uuid.uuid4()
    src_ns = uuid.uuid4()
    src_mem_id = uuid.uuid4()
    payload_ref = "0123456789abcdef01234567"  # 24-hex ObjectId

    src = _EventRow(
        event_id=uuid.uuid4(),
        event_seq=1,
        event_type="store_memory",
        occurred_at=datetime.now(timezone.utc),
        agent_id="agent-1",
        params={
            "memory_id": str(src_mem_id),
            "source_namespace_id": str(src_ns),
            "payload_ref": payload_ref,
        },
        result_summary=None,
        parent_event_id=None,
        llm_payload_uri=None,
        llm_payload_hash=None,
    )

    result = await _handle_store_memory(
        mock_conn,
        src,
        target_ns,
        None,
        None,
    )

    # Verify that the correct queries and inserts were made
    assert result["source_memory_id"] == str(src_mem_id)
    assert result["target_namespace"] == str(target_ns)
    new_mem_id = uuid.UUID(result["new_memory_id"])

    # Check fetchrow calls
    # Call 1: memories select
    # Call 2: memory_salience select
    assert mock_conn.fetchrow.call_count == 2

    # Check execute calls
    # Call 1: INSERT INTO memories
    # Call 2: INSERT INTO memory_salience
    assert mock_conn.execute.call_count == 2

    # Verify the arguments to INSERT INTO memories
    memories_insert_call = mock_conn.execute.call_args_list[0]
    sql_query_memories = memories_insert_call[0][0]
    args_memories = memories_insert_call[0][1:]

    # Under the payload copy strategy, target_payload_ref is derived deterministically using uuid5
    derived_uuid = uuid.uuid5(target_ns, f"payload_ref:{payload_ref}")
    expected_target_ref = str(ObjectId(derived_uuid.bytes[:12]))

    assert "INSERT INTO memories" in sql_query_memories
    assert "summary" not in sql_query_memories
    assert "salience" not in sql_query_memories
    assert "payload_ref" in sql_query_memories
    assert args_memories[0] == new_mem_id
    assert args_memories[1] == target_ns
    assert args_memories[2] == "agent-1"
    assert args_memories[3] == [0.1] * 768
    assert args_memories[4] == "fact"
    assert args_memories[5] == "episodic"
    assert args_memories[6] == expected_target_ref
    assert args_memories[7] == json.dumps(
        {"some_key": "some_val", "source_memory_id": str(src_mem_id)}
    )
    assert args_memories[8] == mock_valid_from
    assert args_memories[9] == mock_created_at
    assert args_memories[10] is None
    assert args_memories[11] is None

    # Verify the arguments to INSERT INTO memory_salience
    salience_insert_call = mock_conn.execute.call_args_list[1]
    sql_query_salience = salience_insert_call[0][0]
    args_salience = salience_insert_call[0][1:]

    assert "INSERT INTO memory_salience" in sql_query_salience
    assert args_salience[0] == new_mem_id
    assert args_salience[1] == "agent-1"
    assert args_salience[2] == target_ns
    assert args_salience[3] == 0.85


@pytest.mark.asyncio
async def test_handle_boost_memory_handler() -> None:
    from unittest.mock import AsyncMock

    from nce.replay import _handle_boost_memory

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = "UPDATE 1"

    target_ns = uuid.uuid4()
    src_mem_id = uuid.uuid4()
    factor = 0.25

    src = _EventRow(
        event_id=uuid.uuid4(),
        event_seq=1,
        event_type="boost_memory",
        occurred_at=datetime.now(timezone.utc),
        agent_id="agent-1",
        params={
            "memory_id": str(src_mem_id),
            "factor": factor,
        },
        result_summary=None,
        parent_event_id=None,
        llm_payload_uri=None,
        llm_payload_hash=None,
    )

    result = await _handle_boost_memory(
        mock_conn,
        src,
        target_ns,
        None,
        None,
    )

    assert result["rows_updated"] == 1
    assert result["factor"] == factor

    mock_conn.execute.assert_called_once()
    execute_call = mock_conn.execute.call_args
    sql_query = execute_call[0][0]
    args = execute_call[0][1:]

    assert "INSERT INTO memory_salience" in sql_query
    assert "memories" in sql_query
    assert "ON CONFLICT (memory_id, agent_id) DO UPDATE" in sql_query
    assert (
        "salience_score = LEAST(1.0, memory_salience.salience_score + EXCLUDED.salience_score)"
        in sql_query
    )
    assert args[0] == factor
    assert args[1] == target_ns
    assert args[2] == "agent-1"
    assert args[3] == str(src_mem_id)


@pytest.mark.asyncio
async def test_handle_ingestion_completed_handler() -> None:
    """Pure-unit: constructing a fake ingestion_completed event and calling the handler
    must not raise, and must return the expected provenance dict keyed by ingest_id."""
    from unittest.mock import AsyncMock

    from nce.replay import _handle_ingestion_completed

    mock_conn = AsyncMock()
    target_ns = uuid.uuid4()
    ingest_id = str(uuid.uuid4())

    src = _EventRow(
        event_id=uuid.uuid4(),
        event_seq=1,
        event_type="ingestion_completed",
        occurred_at=datetime.now(timezone.utc),
        agent_id="agent-diag",
        params={
            "ingest_id": ingest_id,
            "device_slug": "device-abc",
            "digest_payload_ref": "0123456789abcdef01234567",
            "anomaly_count": 3,
            "processed_lines": 1024,
        },
        result_summary=None,
        parent_event_id=None,
        llm_payload_uri=None,
        llm_payload_hash=None,
    )

    result = await _handle_ingestion_completed(mock_conn, src, target_ns, None, None)

    assert result["ingest_id"] == ingest_id
    assert result["skipped"] is False
    assert result["replayed"] is True
    # Handler must not touch DB — no DB calls expected
    mock_conn.execute.assert_not_called()
    mock_conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_handle_ingestion_completed_skips_on_missing_ingest_id() -> None:
    """If ingest_id is absent from params, handler returns skipped=True without raising."""
    from unittest.mock import AsyncMock

    from nce.replay import _handle_ingestion_completed

    mock_conn = AsyncMock()
    target_ns = uuid.uuid4()

    src = _EventRow(
        event_id=uuid.uuid4(),
        event_seq=1,
        event_type="ingestion_completed",
        occurred_at=datetime.now(timezone.utc),
        agent_id="agent-diag",
        params={},
        result_summary=None,
        parent_event_id=None,
        llm_payload_uri=None,
        llm_payload_hash=None,
    )

    result = await _handle_ingestion_completed(mock_conn, src, target_ns, None, None)

    assert result["skipped"] is True
    assert result["reason"] == "no_ingest_id_in_params"
