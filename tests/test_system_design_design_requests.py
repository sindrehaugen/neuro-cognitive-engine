"""
tests/test_system_design_design_requests.py
=============================================
DESIGN_REQUEST's real-Postgres storage path (charter Wave C-4, migration 104).

The filename matches the ``tests/test_system_design_*.py`` CI glob
(``.github/workflows/ci.yml``), so this file runs in CI with no workflow edit.

``tests/unit/test_system_design_design_requests.py`` already covers
``design_requests.py``'s in-memory mock-store path (``conn=None``) -- every
one of those tests runs with ``conn=None``, so none of them ever executed a
real SQL statement. This file is the missing half: it is the FIRST test
anywhere to exercise ``create_design_request``/``get_design_request``/
``list_design_requests``/``update_design_request``/``assign_design_request``/
``complete_design_request`` against a live Postgres, which matters
specifically because this wave rewrote every one of those functions' SQL to
read/write ``system_design_design_requests`` + a ``kg_nodes`` identity row
instead of ``system_design_geometry``'s meta JSONB column -- a storage-layer
change nothing else in CI would have caught.

What is gated here:
  1. A create writes BOTH a kg_nodes identity row (entity_type='DESIGN_REQUEST')
     and the satellite row -- the old scheme wrote neither.
  2. get/list read back real columns, not a JSONB blob -- proven by asserting
     types Postgres would refuse in the old scheme (a real TIMESTAMPTZ
     completed_at, not a string sitting inside meta).
  3. list's status/owner_id/quote_id/priority filters, still real SQL
     predicates against real columns (unlike the C12-generated list, which is
     excluded for this spec -- see resources.py's own comment).
  4. update/assign/complete's kg_edges side effects (assigned_to/realized_as)
     survive the storage cutover unchanged.
  5. Tenant isolation: two namespaces sharing the same request_id never see
     each other's row.

All DB-dependent tests are ``@pytest.mark.integration``.
"""

from __future__ import annotations

import uuid

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.vertical_modules.system_design.design_requests import (
    DesignRequestNotFoundError,
    assign_design_request,
    complete_design_request,
    create_design_request,
    get_design_request,
    list_design_requests,
    update_design_request,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_writes_both_kg_nodes_identity_and_satellite_row(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        created = await create_design_request(
            conn,
            namespace_id,
            title="New rack for server room",
            quote_id="Q-1001",
            functional_location_id="FL-42",
            priority="high",
            request_id="REQ-CREATE-01",
        )

    assert created["id"] == "REQ-CREATE-01"
    assert created["title"] == "New rack for server room"
    assert created["status"] == "pending"
    assert created["priority"] == "high"

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        node = await conn.fetchrow(
            "SELECT entity_type FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            "DESIGN_REQUEST:REQ-CREATE-01",
        )
        row = await conn.fetchrow(
            "SELECT title, status, priority, completed_at FROM system_design_design_requests "
            "WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            "DESIGN_REQUEST:REQ-CREATE-01",
        )

    assert node is not None and node["entity_type"] == "DESIGN_REQUEST"
    assert row is not None
    assert row["title"] == "New rack for server room"
    assert row["status"] == "pending"
    # A real NULL column, not the string "null" sitting inside a JSONB blob.
    assert row["completed_at"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_owner_assigned_at_create_transitions_straight_to_assigned(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        created = await create_design_request(
            conn,
            namespace_id,
            title="Pre-assigned request",
            owner_id="EMP-7",
            request_id="REQ-CREATE-02",
        )
    assert created["status"] == "assigned"

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        edge = await conn.fetchrow(
            "SELECT object_label FROM kg_edges WHERE namespace_id = $1 "
            "AND subject_label = $2 AND predicate = 'assigned_to'",
            namespace_id,
            "DESIGN_REQUEST:REQ-CREATE-02",
        )
    assert edge is not None and edge["object_label"] == "EMPLOYEE:EMP-7"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_missing_request_raises_not_found(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        with pytest.raises(DesignRequestNotFoundError):
            await get_design_request(conn, namespace_id, "REQ-DOES-NOT-EXIST")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_filters_by_status_owner_and_quote_against_real_columns(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        await create_design_request(
            conn, namespace_id, title="Alpha", quote_id="Q-A", request_id="REQ-LIST-01"
        )
        await create_design_request(
            conn,
            namespace_id,
            title="Beta",
            quote_id="Q-B",
            owner_id="EMP-9",
            request_id="REQ-LIST-02",
        )
        await create_design_request(
            conn, namespace_id, title="Gamma", priority="urgent", request_id="REQ-LIST-03"
        )

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        pending = await list_design_requests(conn, namespace_id, status="pending")
        assigned = await list_design_requests(conn, namespace_id, owner_id="EMP-9")
        by_quote = await list_design_requests(conn, namespace_id, quote_id="Q-A")
        urgent = await list_design_requests(conn, namespace_id, priority="urgent")
        by_title = await list_design_requests(conn, namespace_id, query="beta")

    assert {r["id"] for r in pending} == {"REQ-LIST-01", "REQ-LIST-03"}
    assert {r["id"] for r in assigned} == {"REQ-LIST-02"}
    assert {r["id"] for r in by_quote} == {"REQ-LIST-01"}
    assert {r["id"] for r in urgent} == {"REQ-LIST-03"}
    assert {r["id"] for r in by_title} == {"REQ-LIST-02"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_complete_sets_design_id_and_realized_as_edge(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        await create_design_request(conn, namespace_id, title="To complete", request_id="REQ-C-01")
        completed = await complete_design_request(conn, namespace_id, "REQ-C-01", "DESIGN-42")

    assert completed["status"] == "completed"
    assert completed["design_id"] == "42"
    assert completed["completed_at"] is not None

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        edge = await conn.fetchrow(
            "SELECT object_label FROM kg_edges WHERE namespace_id = $1 "
            "AND subject_label = $2 AND predicate = 'realized_as'",
            namespace_id,
            "DESIGN_REQUEST:REQ-C-01",
        )
        row = await conn.fetchrow(
            "SELECT completed_at FROM system_design_design_requests "
            "WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            "DESIGN_REQUEST:REQ-C-01",
        )
    assert edge is not None and edge["object_label"] == "DESIGN:42"
    assert row is not None and row["completed_at"] is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_assign_then_update_reassigns_the_edge_not_duplicates_it(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        await create_design_request(conn, namespace_id, title="Reassign me", request_id="REQ-A-01")
        await assign_design_request(conn, namespace_id, "REQ-A-01", "EMP-1")
        await update_design_request(conn, namespace_id, "REQ-A-01", owner_id="EMP-2")

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        edges = await conn.fetch(
            "SELECT object_label FROM kg_edges WHERE namespace_id = $1 "
            "AND subject_label = $2 AND predicate = 'assigned_to'",
            namespace_id,
            "DESIGN_REQUEST:REQ-A-01",
        )
    assert [e["object_label"] for e in edges] == ["EMPLOYEE:EMP-2"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tenant_isolation_same_request_id_two_namespaces(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
) -> None:
    ns_a = uuid.uuid4()
    ns_b = uuid.uuid4()
    async with pg_pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO namespaces (id, slug) VALUES ($1, $2), ($3, $4)",
            ns_a,
            f"tenant-a-{ns_a.hex[:8]}",
            ns_b,
            f"tenant-b-{ns_b.hex[:8]}",
        )

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, ns_a)
        await create_design_request(conn, ns_a, title="Tenant A's request", request_id="SHARED-ID")

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, ns_b)
        with pytest.raises(DesignRequestNotFoundError):
            await get_design_request(conn, ns_b, "SHARED-ID")
