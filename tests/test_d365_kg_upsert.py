"""Integration test: _upsert_kg_node writes a real kg_nodes row.

Proves that the INSERT in DataverseSyncEngine._upsert_kg_node uses only
columns that exist in the live schema — specifically that the removed
``metadata`` column no longer appears and the call succeeds without raising
asyncpg.UndefinedColumnError.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from nce.auth import set_namespace_context
from nce.vertical_modules.dynamics365.sync import DataverseSyncEngine

# ---------------------------------------------------------------------------
# Minimal fake client — _upsert_kg_node does not touch the Dataverse client
# ---------------------------------------------------------------------------


class _FakeClient:
    """Stub DataverseClient — no network calls needed for this test."""

    def paginate(self, entity_set, *, select=None, filter_expr=None, page_size=1000):
        async def _empty():
            return
            yield  # pragma: no cover

        return _empty()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def test_ns(
    pg_pool: asyncpg.Pool,
) -> AsyncGenerator[uuid.UUID, None]:
    """Create a fresh namespace for the test and clean up kg_nodes after."""
    slug = f"pytest-d365-kg-{uuid.uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        ns_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
            slug,
        )
    assert ns_id is not None
    yield ns_id
    # Cleanup: remove any kg_nodes rows inserted by the test
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM kg_nodes WHERE namespace_id = $1",
            ns_id,
        )
        await conn.execute(
            "DELETE FROM namespaces WHERE id = $1",
            ns_id,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_kg_node_inserts_row_without_metadata_column(
    pg_app_conn: asyncpg.Connection,
    test_ns: uuid.UUID,
) -> None:
    """_upsert_kg_node must land a row in kg_nodes using only real schema columns.

    Failure mode before the fix: asyncpg.exceptions.UndefinedColumnError because
    the INSERT referenced the non-existent ``metadata`` column.
    """
    source_id = str(uuid.uuid4())
    label = f"Account:TestCorp_{uuid.uuid4().hex[:6]}"

    engine = DataverseSyncEngine(pg_app_conn, test_ns, _FakeClient())

    # set_config(..., true) is transaction-local — must run inside a transaction.
    # Mock emit_graph_write so we don't need a full outbox/event_log setup.
    with patch(
        "nce.vertical_modules.dynamics365.sync.emit_graph_write",
        new_callable=AsyncMock,
    ) as mock_emit:
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, test_ns)
            await engine._upsert_kg_node(
                label,
                "D365_Account",
                source_id=source_id,
            )

    # emit_graph_write was called once with the right args
    mock_emit.assert_awaited_once_with(
        pg_app_conn,
        namespace_id=test_ns,
        node_type="D365_Account",
        op="upserted",
        node_id=label,
    )

    # A real row must now exist in kg_nodes (read back without RLS; use admin conn via pool)
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, test_ns)
        row = await pg_app_conn.fetchrow(
            """
            SELECT label, entity_type, d365_source_id
            FROM kg_nodes
            WHERE label = $1 AND namespace_id = $2
            """,
            label,
            test_ns,
        )

    assert row is not None, "kg_nodes row was not inserted"
    assert row["label"] == label
    assert row["entity_type"] == "D365_Account"
    assert row["d365_source_id"] == source_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_kg_node_idempotent_conflict_update(
    pg_app_conn: asyncpg.Connection,
    test_ns: uuid.UUID,
) -> None:
    """ON CONFLICT DO UPDATE must update entity_type and preserve d365_source_id via COALESCE."""
    engine = DataverseSyncEngine(pg_app_conn, test_ns, _FakeClient())
    label = f"Account:Idempotent_{uuid.uuid4().hex[:6]}"
    source_id = str(uuid.uuid4())

    with patch(
        "nce.vertical_modules.dynamics365.sync.emit_graph_write",
        new_callable=AsyncMock,
    ):
        # First insert — sets d365_source_id
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, test_ns)
            await engine._upsert_kg_node(label, "D365_Account", source_id=source_id)

        # Second insert — no source_id; COALESCE must keep the original
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, test_ns)
            await engine._upsert_kg_node(label, "D365_Account", source_id=None)

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, test_ns)
        row = await pg_app_conn.fetchrow(
            "SELECT d365_source_id FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
            label,
            test_ns,
        )
    assert row is not None
    assert row["d365_source_id"] == source_id, (
        "COALESCE should preserve the original source_id when a None is passed on conflict"
    )
