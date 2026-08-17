import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.db_utils import scoped_pg_session
from nce.event_log import DataIntegrityError, verify_event_signature
from nce.replay import ObservationalReplay


@pytest.mark.asyncio
async def test_verify_event_signature_tampered_record_raises_error():
    conn = AsyncMock()

    # Mock a tampered event record
    record = {
        "id": uuid.uuid4(),
        "namespace_id": uuid.uuid4(),
        "agent_id": "test_agent",
        "event_type": "store_memory",
        "event_seq": 1,
        "occurred_at": datetime.now(timezone.utc),
        "params": '{"tampered": "yes"}',
        "parent_event_id": None,
        "signature": b"fake_signature",
        "signature_key_id": "sk-12345",
        "signature_version": 1,
    }

    # Patch get_key_by_id and verify_fields
    with patch("nce.signing.get_key_by_id", new_callable=AsyncMock) as mock_get_key:
        mock_get_key.return_value = b"raw_secret_key"
        with patch("nce.signing.verify_fields", return_value=False) as mock_verify:
            with pytest.raises(DataIntegrityError, match="Event signature mismatch for event_id="):
                await verify_event_signature(conn, record)
            mock_verify.assert_called_once()


@pytest.mark.asyncio
async def test_observational_replay_yields_error_on_tampering():
    pool = MagicMock()
    conn = AsyncMock()

    # Setup mock cursor to yield 1 tampered record
    record = {
        "id": uuid.uuid4(),
        "namespace_id": uuid.uuid4(),
        "agent_id": "test_agent",
        "event_type": "store_memory",
        "event_seq": 1,
        "occurred_at": datetime.now(timezone.utc),
        "params": '{"tampered": "yes"}',
        "parent_event_id": None,
        "signature": b"fake_signature",
        "signature_key_id": "sk-12345",
        "llm_payload_uri": None,
        "llm_payload_hash": None,
        "result_summary": None,
    }

    async def async_generator():
        yield record

    conn.cursor = MagicMock()
    conn.cursor.return_value = async_generator()

    @asynccontextmanager
    async def mock_transaction(*args, **kwargs):
        yield

    conn.transaction = mock_transaction

    @asynccontextmanager
    async def mock_acquire(*args, **kwargs):
        yield conn

    pool.acquire = mock_acquire

    replay = ObservationalReplay(pool)

    with patch("nce.replay.verify_event_signature", new_callable=AsyncMock) as mock_verify:
        mock_verify.side_effect = DataIntegrityError("Tampering detected.")

        # We need to mock _create_run and _build_event_query to not fail
        with patch(
            "nce.replay._create_run",
            new_callable=AsyncMock,
            return_value=uuid.uuid4(),
        ):
            with patch("nce.replay._build_event_query", return_value=("SQL", [])):
                with patch("nce.replay._finish_run", new_callable=AsyncMock):
                    with pytest.raises(DataIntegrityError):
                        async for item in replay.execute(source_namespace_id=uuid.uuid4()):
                            if item["type"] == "error":
                                assert item["message"] == "Tampering detected."


@pytest.mark.integration
@pytest.mark.asyncio
async def test_signature_version_2_integration(pg_pool, make_namespace, monkeypatch) -> None:
    """
    1. Append events (now version 2). Verify they are inserted with signature_version = 2
       and they pass verify_event_signature.
    2. Tamper parameters of one row and confirm verify_event_signature fails.
    3. Reorder rows or alter chain_hash and confirm verify_event_signature fails for v2.
    4. Verify a simulated pre-existing v1 row (without prev_chain_hash hex in signature)
       still verifies correctly.
    """
    import json

    from nce.config import cfg
    from nce.event_log import _sign_event, append_event, verify_event_signature

    ns_id = await make_namespace()
    agent_id = "test-sig-v2-agent"

    # Append 3 events (should default to version 2)
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        results = []
        for i in range(3):
            res = await append_event(
                conn=conn,
                namespace_id=ns_id,
                agent_id=agent_id,
                event_type="store_memory",
                params={
                    "saga_id": str(uuid.uuid4()),
                    "memory_id": str(uuid.uuid4()),
                    "payload_ref": f"10000000000000000000000{i}",
                    "assertion_type": "fact",
                    "entities": [],
                    "triplets": [],
                },
            )
            results.append(res)

    # Fetch these events from db and check signature_version and validity
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        rows = await conn.fetch(
            "SELECT * FROM event_log WHERE namespace_id = $1 ORDER BY event_seq ASC",
            ns_id,
        )
        assert len(rows) == 3
        for row in rows:
            assert row["signature_version"] == 2
            # Verify they pass
            await verify_event_signature(conn, row)

    # Tamper parameters of row 2 and verify it raises DataIntegrityError
    monkeypatch.setenv("NCE_BYPASS_WORM", "true")
    with patch.object(cfg, "NCE_BYPASS_WORM", True):
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await conn.execute("ALTER TABLE event_log DISABLE TRIGGER trg_event_log_worm")
            try:
                # Corrupt params of seq = 2
                await conn.execute(
                    "UPDATE event_log SET params = '{\"tampered\": true}'::jsonb WHERE namespace_id = $1 AND event_seq = 2",
                    ns_id,
                )
            finally:
                await conn.execute("ALTER TABLE event_log ENABLE TRIGGER trg_event_log_worm")

        # Now fetch seq = 2 and check verification fails
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            row_2 = await conn.fetchrow(
                "SELECT * FROM event_log WHERE namespace_id = $1 AND event_seq = 2",
                ns_id,
            )
            with pytest.raises(DataIntegrityError, match="Event signature mismatch for event_id="):
                await verify_event_signature(conn, row_2)

    # Re-fetch row 3 (pristine signature version 2, but its prev_seq=2 was tampered - wait, no.
    # The signature of row 3 is computed over row 2's chain_hash. Row 2's chain_hash was NOT changed.
    # What if we alter row 2's chain_hash?
    # Let's alter row 2's chain_hash, then verify row 3. Since row 3's signature binds row 2's chain_hash,
    # if row 2's chain_hash doesn't match what was signed, row 3 should fail signature verification!
    with patch.object(cfg, "NCE_BYPASS_WORM", True):
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await conn.execute("ALTER TABLE event_log DISABLE TRIGGER trg_event_log_worm")
            try:
                # Corrupt chain_hash of seq = 2
                await conn.execute(
                    "UPDATE event_log SET chain_hash = $1 WHERE namespace_id = $2 AND event_seq = 2",
                    b"f" * 32,
                    ns_id,
                )
            finally:
                await conn.execute("ALTER TABLE event_log ENABLE TRIGGER trg_event_log_worm")

        # Now verify row 3. It should fail because when rebuilding row 3's signature,
        # it fetches row 2's chain_hash (which is now different) and computes a different HMAC.
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            row_3 = await conn.fetchrow(
                "SELECT * FROM event_log WHERE namespace_id = $1 AND event_seq = 3",
                ns_id,
            )
            with pytest.raises(DataIntegrityError, match="Event signature mismatch for event_id="):
                await verify_event_signature(conn, row_3)

    # Verify a version 1 row still verifies correctly.
    # Let's build a version 1 row: we manually sign it with signature_version=1 (which doesn't bind prev_chain_hash_hex),
    # insert it as signature_version = 1, and make sure verify_event_signature validates it.
    v1_event_id = uuid.uuid4()
    v1_seq = 100  # arbitrary seq
    v1_occurred_at = datetime.now(timezone.utc)
    v1_occurred_at_iso = v1_occurred_at.isoformat()
    v1_params = {"saga_id": str(uuid.uuid4())}

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        key_id, signature = await _sign_event(
            conn,
            event_id=v1_event_id,
            namespace_id=ns_id,
            agent_id=agent_id,
            event_type="store_memory",
            event_seq=v1_seq,
            occurred_at_iso=v1_occurred_at_iso,
            params=v1_params,
            parent_event_id=None,
            prev_chain_hash_hex=None,  # v1 signature does NOT include this
        )

        # Manually insert it with signature_version = 1 using bypass
        with patch.object(cfg, "NCE_BYPASS_WORM", True):
            await conn.execute("ALTER TABLE event_log DISABLE TRIGGER trg_event_log_worm")
            try:
                await conn.execute(
                    """
                    INSERT INTO event_log (
                        id, namespace_id, agent_id, event_type, event_seq,
                        occurred_at, params, signature, signature_key_id, signature_version
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, 1)
                    """,
                    v1_event_id,
                    ns_id,
                    agent_id,
                    "store_memory",
                    v1_seq,
                    v1_occurred_at,
                    json.dumps(v1_params),
                    signature,
                    key_id,
                )
            finally:
                await conn.execute("ALTER TABLE event_log ENABLE TRIGGER trg_event_log_worm")

        # Now fetch and verify the v1 row
        v1_row = await conn.fetchrow(
            "SELECT * FROM event_log WHERE namespace_id = $1 AND event_seq = $2",
            ns_id,
            v1_seq,
        )
        await verify_event_signature(conn, v1_row)  # must pass without error!
