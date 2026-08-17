"""Additional event_log coverage: identifier hygiene, catalog invariants, and live PG checks.

Unit tests run without Postgres. Integration tests use ``pytest.mark.integration``
and fixtures from ``tests/conftest.py`` (``PG_DSN`` / ``NCE_INTEGRATION_PG_DSN``).
"""

from __future__ import annotations

import uuid

import pytest

from nce.event_log import (
    _GENESIS_SENTINEL,
    EXPECTED_GLOBAL_TABLES,
    EXPECTED_SPECIAL_RLS_TABLES,
    EXPECTED_TENANT_RLS_TABLES,
    EventLogError,
    _validate_identifier,
    append_event,
    verify_event_signature,
    verify_merkle_chain,
)
from nce.event_types import VALID_EVENT_TYPES
from tests.fixtures.event_log_params import minimal_store_memory_params

# ---------------------------------------------------------------------------
# Identifier validation (_validate_identifier)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "identifier",
    [
        "event_log",
        "t",
        "Ab",
        "t0",
        "x_" + "y" * 61,  # 63 chars total: letter + 62 alnum/underscore
    ],
)
def test_validate_identifier_accepts(identifier: str) -> None:
    assert _validate_identifier(identifier) == identifier


@pytest.mark.parametrize(
    "identifier",
    [
        "",
        "123",
        "9abc",
        "a-b",
        "a.space",
        "a.",
        "../../../x",
        "public.event_log",
        "x" * 200,
    ],
)
def test_validate_identifier_rejects(identifier: str) -> None:
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        _validate_identifier(identifier)


def test_validate_identifier_overflow_length() -> None:
    with pytest.raises(ValueError):
        _validate_identifier("z" + "0" * 63)  # 64 chars


# ---------------------------------------------------------------------------
# RLS-intent dictionaries stay internally consistent (regression nets)
# ---------------------------------------------------------------------------


def test_tenant_special_global_no_overlap() -> None:
    tenant_keys = set(EXPECTED_TENANT_RLS_TABLES)
    special_keys = set(EXPECTED_SPECIAL_RLS_TABLES)
    assert tenant_keys.isdisjoint(special_keys)
    assert tenant_keys.isdisjoint(EXPECTED_GLOBAL_TABLES)
    assert special_keys.isdisjoint(EXPECTED_GLOBAL_TABLES)


def test_expected_tenant_rls_covers_event_log_pipeline() -> None:
    """Hot paths referenced by docs and Saga must stay explicitly declared."""

    for table in ("event_log", "memories", "pii_redactions", "outbox_events"):
        assert table in EXPECTED_TENANT_RLS_TABLES


def test_a2a_grants_special_scope() -> None:
    cols = EXPECTED_SPECIAL_RLS_TABLES.get("a2a_grants")
    assert cols is not None
    assert "owner_namespace_id" in cols


def test_valid_event_types_non_empty_contains_store_memory() -> None:
    assert "store_memory" in VALID_EVENT_TYPES


def test_genesis_sentinel_length() -> None:
    assert len(_GENESIS_SENTINEL) == 32


# ---------------------------------------------------------------------------
# Integration — append + cryptographic verification against real Postgres
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_append_event_integration_writes_row(pg_pool, namespace_id) -> None:
    async with pg_pool.acquire(timeout=30.0) as conn:
        async with conn.transaction():
            result = await append_event(
                conn=conn,
                namespace_id=namespace_id,
                agent_id="pytest-agent",
                event_type="store_memory",
                params=minimal_store_memory_params(probe=True),
            )
    assert result.event_seq == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_append_event_integration_requires_transaction(pg_pool, namespace_id) -> None:
    async with pg_pool.acquire(timeout=30.0) as conn:
        with pytest.raises(EventLogError, match="active transaction"):
            await append_event(
                conn=conn,
                namespace_id=namespace_id,
                agent_id="pytest-agent",
                event_type="store_memory",
                params=minimal_store_memory_params(),
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_verify_merkle_chain_integration_empty_namespace(pg_pool) -> None:
    ghost_ns = uuid.uuid4()
    async with pg_pool.acquire(timeout=30.0) as conn:
        summary = await verify_merkle_chain(conn, namespace_id=ghost_ns)
    assert summary["valid"] is True
    assert summary["checked"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_verify_merkle_and_signature_after_append(pg_pool, namespace_id) -> None:
    saga_id = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    async with pg_pool.acquire(timeout=30.0) as conn:
        async with conn.transaction():
            await append_event(
                conn=conn,
                namespace_id=namespace_id,
                agent_id="pytest-agent",
                event_type="store_memory",
                params=minimal_store_memory_params(saga_id=saga_id, memory_id=mid),
            )
        row = await conn.fetchrow(
            """
            SELECT id, namespace_id, agent_id, event_type, event_seq,
                   occurred_at, params, parent_event_id, signature, signature_key_id
            FROM   event_log
            WHERE  namespace_id = $1 AND event_seq = 1
            """,
            namespace_id,
        )
        await verify_event_signature(conn, row)

        summary = await verify_merkle_chain(conn, namespace_id=namespace_id)
    assert summary["valid"] is True
    assert summary["checked"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_append_two_events_integration_chain_valid(pg_pool, namespace_id) -> None:
    async with pg_pool.acquire(timeout=30.0) as conn:
        async with conn.transaction():
            await append_event(
                conn=conn,
                namespace_id=namespace_id,
                agent_id="pytest-agent",
                event_type="store_memory",
                params=minimal_store_memory_params(),
            )
            await append_event(
                conn=conn,
                namespace_id=namespace_id,
                agent_id="pytest-agent",
                event_type="store_memory",
                params=minimal_store_memory_params(),
            )
        summary = await verify_merkle_chain(conn, namespace_id=namespace_id)

    assert summary["valid"] is True
    assert summary["checked"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_verify_merkle_chain_partial_range(pg_pool, namespace_id) -> None:
    async with pg_pool.acquire(timeout=30.0) as conn:
        async with conn.transaction():
            for _ in range(3):
                await append_event(
                    conn=conn,
                    namespace_id=namespace_id,
                    agent_id="pytest-agent",
                    event_type="store_memory",
                    params=minimal_store_memory_params(),
                )
        summary = await verify_merkle_chain(conn, namespace_id=namespace_id, start_seq=2)

    assert summary["valid"] is True
    assert summary["checked"] == 2
