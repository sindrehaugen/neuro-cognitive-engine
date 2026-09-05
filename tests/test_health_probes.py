"""Tests for deep health probes (Batch 17 + Batch 102)."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.event_log import DataIntegrityError, append_event
from nce.observability import EVENT_SIGNATURE_VALID, MERKLE_CHAIN_VALID
from nce.orchestrator import NCEEngine


@pytest.mark.asyncio
async def test_check_health_unit_basic(monkeypatch):
    """Unit test for check_health with mocked backends to assert basic structure."""
    monkeypatch.setattr(cfg, "NCE_BACKEND", "mock")

    engine = NCEEngine()
    engine.pg_pool = MagicMock()
    engine.mongo_client = MagicMock()
    engine.mongo_client.admin.command = AsyncMock()
    engine.redis_client = MagicMock()
    engine.redis_client.ping = AsyncMock()
    engine.redis_sync_client = MagicMock()

    # Mock DB connections and return values
    mock_conn = AsyncMock()
    # Shared across every fetchrow() call in check_health, including probe
    # (d)'s role-capability query — role_name/rolsuper/rolbypassrls added so
    # that probe alongside the pre-existing signing-key lookup.
    mock_conn.fetchrow.return_value = {
        "encrypted_key": b"TC3\x01fakeblob",
        "role_name": "mock_role",
        "rolsuper": False,
        "rolbypassrls": False,
    }
    mock_conn.fetch.return_value = []  # namespaces; also probe (d)'s policy query
    mock_conn.fetchval.return_value = 0  # max_seq

    @asynccontextmanager
    async def mock_acquire(*args, **kwargs):
        yield mock_conn

    engine.pg_pool.acquire = mock_acquire

    # Mock require_master_key and decrypt_signing_key
    with (
        patch("nce.signing.require_master_key") as mock_req,
        patch("nce.signing.decrypt_signing_key") as mock_dec,
        patch("nce.db_utils.scoped_pg_session") as mock_scoped,
    ):
        mock_req.return_value.__enter__.return_value = "fake_master_key"
        mock_dec.return_value = b"fake_signing_key"

        @asynccontextmanager
        async def mock_scoped_session(*args, **kwargs):
            yield mock_conn

        mock_scoped.side_effect = mock_scoped_session

        health = await engine.check_health()
        assert health["status"] == "ok"
        assert health["security"]["signing_key_decryption"] == "valid"
        assert health["security"]["bounded_chain_sample"] == "valid"
        assert health["databases"]["rls_read"] == "valid"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.signing_isolation
async def test_check_health_integration_healthy(pg_pool, make_namespace, monkeypatch):
    """Integration test: with a valid master key and healthy DBs every probe
    reports valid, and the only thing keeping the overall status off "ok" is
    the RLS role posture.

    This test asserted ``status == "ok"`` until the role-capability probe
    landed. It no longer can, and that is not a regression: every deployed
    ``PG_DSN`` names a superuser/BYPASSRLS role, so the posture genuinely is
    degraded and asserting "ok" asserted something untrue. Rather than drop
    the status assertion (which would stop checking anything), it now pins
    *why* the status is degraded — so an unrelated probe starting to fail here
    still breaks this test instead of hiding behind an already-degraded
    status.
    """
    monkeypatch.setattr(cfg, "NCE_BACKEND", "mock")

    engine = NCEEngine()
    engine.pg_pool = pg_pool

    # Call check_health
    health = await engine.check_health()
    assert health["security"]["signing_key_decryption"] == "valid"
    assert health["security"]["bounded_chain_sample"] == "valid"
    assert health["databases"]["rls_read"] == "valid"
    # The RLS role posture is the sole reason for a non-"ok" status here.
    posture = health["security"]["rls_role_posture"]
    if posture == "ok":
        assert health["status"] == "ok"
    else:
        assert health["status"] == "degraded"
        assert any("can bypass RLS" in f for f in posture), (
            f"status is degraded but not for the expected reason: {posture}"
        )

    # Verify Prometheus gauge
    if hasattr(MERKLE_CHAIN_VALID, "_value"):
        assert MERKLE_CHAIN_VALID._value.get() == 1


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.signing_isolation
async def test_check_health_integration_broken_master_key(pg_pool, monkeypatch):
    """Integration test verifying that with a broken/wrong master key,
    health check reports status='degraded' and signing_key_decryption='failed'
    even though the DBs are fully reachable.
    """
    monkeypatch.setattr(cfg, "NCE_BACKEND", "mock")

    engine = NCEEngine()
    engine.pg_pool = pg_pool

    # Temporarily set NCE_MASTER_KEY to a wrong key (32 bytes of 'y')
    monkeypatch.setenv("NCE_MASTER_KEY", "y" * 32)

    # Call check_health
    health = await engine.check_health()

    assert health["status"] == "degraded"
    assert health["security"]["signing_key_decryption"] == "failed"
    # Databases like postgres can still be up
    assert health["databases"]["postgres"] == "up"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.signing_isolation
async def test_check_health_integration_corrupted_chain(pg_pool, make_namespace, monkeypatch):
    """Integration test verifying that a corrupted Merkle chain causes the health check
    to report status='degraded', bounded_chain_sample='corrupted', and sets
    MERKLE_CHAIN_VALID to 0.
    """
    monkeypatch.setattr(cfg, "NCE_BACKEND", "mock")

    ns_id = await make_namespace()
    agent_id = "test-health-chain-corrupt-agent"

    # Append a couple of valid events
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        for i in range(2):
            await append_event(
                conn=conn,
                namespace_id=ns_id,
                agent_id=agent_id,
                event_type="store_memory",
                params={
                    "saga_id": str(uuid.uuid4()),
                    "memory_id": str(uuid.uuid4()),
                    "payload_ref": f"00000000000000000000000{i}",
                    "assertion_type": "fact",
                    "entities": [],
                    "triplets": [],
                },
            )

    # Now tamper with one event in the database to corrupt the Merkle chain.
    monkeypatch.setenv("NCE_BYPASS_WORM", "true")
    monkeypatch.setattr(cfg, "NCE_BYPASS_WORM", True)

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        await conn.execute("ALTER TABLE event_log DISABLE TRIGGER trg_event_log_worm")
        try:
            # Update the parameters of the second event to corrupt the signature/chain
            await conn.execute(
                """
                UPDATE event_log
                SET params = '{"data": "tampered"}'::jsonb
                WHERE namespace_id = $1 AND event_seq = 2
                """,
                ns_id,
            )
        finally:
            # Re-enable triggers
            await conn.execute("ALTER TABLE event_log ENABLE TRIGGER trg_event_log_worm")

    engine = NCEEngine()
    engine.pg_pool = pg_pool

    # Intercept query for namespaces in check_health to only scan the tampered one.
    original_fetch = asyncpg.Connection.fetch

    async def mock_fetch(self, query, *args, **kwargs):
        if "SELECT id FROM namespaces" in query:
            return [{"id": ns_id}]
        return await original_fetch(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.Connection, "fetch", mock_fetch)

    # Call check_health
    health = await engine.check_health()

    assert health["status"] == "degraded"
    assert health["security"]["bounded_chain_sample"] == "corrupted"
    if hasattr(MERKLE_CHAIN_VALID, "_value"):
        assert MERKLE_CHAIN_VALID._value.get() == 0


# ---------------------------------------------------------------------------
# Batch 102 — bounded event-signature sampling (b2) unit tests
# ---------------------------------------------------------------------------

_FAKE_NS_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_FAKE_EVENT_ROW = {
    "id": uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
    "namespace_id": _FAKE_NS_ID,
    "agent_id": "test-agent",
    "event_type": "store_memory",
    "event_seq": 1,
    "occurred_at": "2026-01-01T00:00:00+00:00",
    "params": {},
    "parent_event_id": None,
    "signature": b"\x00" * 32,
    "signature_key_id": "key-1",
    "signature_version": 2,
}


def _make_engine_with_pool(mock_conn):
    """Return a bare NCEEngine whose pg_pool.acquire yields *mock_conn*."""
    engine = NCEEngine()
    engine.pg_pool = MagicMock()
    engine.mongo_client = MagicMock()
    engine.mongo_client.admin.command = AsyncMock()
    engine.redis_client = MagicMock()
    engine.redis_client.ping = AsyncMock()
    engine.redis_sync_client = MagicMock()

    @asynccontextmanager
    async def mock_acquire(*args, **kwargs):
        yield mock_conn

    engine.pg_pool.acquire = mock_acquire
    return engine


@pytest.mark.asyncio
async def test_check_health_unit_signature_valid(monkeypatch):
    """Unit test: when verify_event_signature succeeds, bounded_signature_sample is
    'valid', health is 'ok', and EVENT_SIGNATURE_VALID gauge is set to 1.
    """
    monkeypatch.setattr(cfg, "NCE_BACKEND", "mock")

    mock_conn = AsyncMock()
    # Shared across every fetchrow() call, including probe (d)'s role query.
    mock_conn.fetchrow.return_value = {
        "encrypted_key": b"TC3\x01fakeblob",
        "role_name": "mock_role",
        "rolsuper": False,
        "rolbypassrls": False,
    }
    # fetch calls in order: (b) namespaces, (b2) namespaces, (b2) sig_rows,
    # (d) tables-with-policies for the RLS role-capability check (added by
    # probe (d); empty ⇒ no non-conforming tables ⇒ no finding).
    mock_conn.fetch.side_effect = [
        [{"id": _FAKE_NS_ID}],  # block (b): namespaces
        [{"id": _FAKE_NS_ID}],  # block (b2): namespaces
        [_FAKE_EVENT_ROW],  # block (b2): sig rows for _FAKE_NS_ID
        [],  # block (d): RLS policy-role-list query
    ]
    # fetchval: (b) max_seq for chain check, (b2) max_seq for sig check
    mock_conn.fetchval.side_effect = [1, 1]

    engine = _make_engine_with_pool(mock_conn)

    @asynccontextmanager
    async def mock_scoped_session(*args, **kwargs):
        yield mock_conn

    # verify_merkle_chain is called inside block (b); patch it to return valid.
    # verify_event_signature is called inside block (b2); patch it to succeed (no-op).
    with (
        patch("nce.signing.require_master_key") as mock_req,
        patch("nce.signing.decrypt_signing_key") as mock_dec,
        patch("nce.db_utils.scoped_pg_session") as mock_scoped,
        patch("nce.event_log.verify_merkle_chain", new_callable=AsyncMock) as mock_chain,
        patch("nce.event_log.verify_event_signature", new_callable=AsyncMock) as mock_sig,
    ):
        mock_req.return_value.__enter__.return_value = "fake_master_key"
        mock_dec.return_value = b"fake_signing_key"
        mock_scoped.side_effect = mock_scoped_session
        mock_chain.return_value = {"valid": True, "checked": 1}
        mock_sig.return_value = None  # no exception → signature valid

        health = await engine.check_health()

    assert health["security"]["bounded_signature_sample"] == "valid"
    assert health["status"] == "ok"
    if hasattr(EVENT_SIGNATURE_VALID, "_value"):
        assert EVENT_SIGNATURE_VALID._value.get() == 1


@pytest.mark.asyncio
async def test_check_health_unit_signature_tampered(monkeypatch):
    """Unit test: when verify_event_signature raises DataIntegrityError (tampered
    signature), bounded_signature_sample is 'tampered', health degrades to
    'degraded', and EVENT_SIGNATURE_VALID gauge is set to 0.
    """
    monkeypatch.setattr(cfg, "NCE_BACKEND", "mock")

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"encrypted_key": b"TC3\x01fakeblob"}
    mock_conn.fetch.side_effect = [
        [{"id": _FAKE_NS_ID}],  # block (b): namespaces
        [{"id": _FAKE_NS_ID}],  # block (b2): namespaces
        [_FAKE_EVENT_ROW],  # block (b2): sig rows
    ]
    mock_conn.fetchval.side_effect = [1, 1]

    engine = _make_engine_with_pool(mock_conn)

    @asynccontextmanager
    async def mock_scoped_session(*args, **kwargs):
        yield mock_conn

    with (
        patch("nce.signing.require_master_key") as mock_req,
        patch("nce.signing.decrypt_signing_key") as mock_dec,
        patch("nce.db_utils.scoped_pg_session") as mock_scoped,
        patch("nce.event_log.verify_merkle_chain", new_callable=AsyncMock) as mock_chain,
        patch(
            "nce.event_log.verify_event_signature",
            new_callable=AsyncMock,
        ) as mock_sig,
    ):
        mock_req.return_value.__enter__.return_value = "fake_master_key"
        mock_dec.return_value = b"fake_signing_key"
        mock_scoped.side_effect = mock_scoped_session
        mock_chain.return_value = {"valid": True, "checked": 1}
        # Tampered: raises DataIntegrityError
        mock_sig.side_effect = DataIntegrityError("signature mismatch — tampered")

        health = await engine.check_health()

    assert health["security"]["bounded_signature_sample"] == "tampered"
    assert health["status"] == "degraded"
    if hasattr(EVENT_SIGNATURE_VALID, "_value"):
        assert EVENT_SIGNATURE_VALID._value.get() == 0
