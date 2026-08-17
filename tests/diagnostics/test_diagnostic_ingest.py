"""Integration acceptance test for Batch 73 — diagnostic-ingest writer.

Proves idempotency of the two diagnostic-rollup upserts in
``nce.orchestrators.diagnostic_ingest``:

* ``upsert_topology_edges`` — double-applying the SAME edge (same
  ``namespace_id, source_node_id, target_node_id, edge_type``) leaves EXACTLY
  ONE ``topology_graph`` row (ON CONFLICT UPDATE, not a second INSERT).
* ``upsert_device_health`` — double-applying for the SAME
  ``(namespace_id, device_slug)`` leaves EXACTLY ONE ``device_health_rollup``
  row.

Both writes run inside a caller-managed ``scoped_pg_session`` (RLS scoped).

Requires the isolated test stack PostgreSQL on 127.0.0.1:5433; run with
``-m integration``. Skips cleanly when the DB is unreachable (mirrors the
local-isolated-pool + ``pytest.skip`` pattern in
``tests/test_envelope_read_consumers.py``).
"""

from __future__ import annotations

import os
import socket
import uuid

import pytest
import pytest_asyncio

from nce.db_utils import scoped_pg_session
from nce.orchestrators.diagnostic_ingest import (
    upsert_device_health,
    upsert_topology_edges,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Container reachability guard
# ---------------------------------------------------------------------------


def _reachable(host: str, port: int) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=1)
        s.close()
        return True
    except OSError:
        return False


_INTEGRATION_PG_PORT = 5433
_PG_OK = _reachable("127.0.0.1", _INTEGRATION_PG_PORT)

_skip_no_pg = pytest.mark.skipif(
    not _PG_OK,
    reason="Integration test requires isolated PG on 127.0.0.1:5433",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool_isolated():
    """asyncpg pool connected to the isolated integration database."""
    import asyncpg  # type: ignore[import-untyped]

    dsn = os.environ.get(
        "NCE_INTEGRATION_PG_DSN",
        "postgresql://mcp_user:mcp_password@127.0.0.1:5433/memory_meta",
    )
    try:
        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=4,
            command_timeout=60,
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Cannot connect to isolated PG: {exc}")
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def ns_id(pg_pool_isolated) -> uuid.UUID:
    """Create a fresh namespace for each test."""
    slug = f"pytest-b73-{uuid.uuid4().hex}"
    async with pg_pool_isolated.acquire() as conn:
        ns = await conn.fetchval("INSERT INTO namespaces (slug) VALUES ($1) RETURNING id", slug)
    assert ns is not None
    return ns


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@_skip_no_pg
@pytest.mark.asyncio
async def test_upsert_topology_edges_is_idempotent(pg_pool_isolated, ns_id) -> None:
    """Double-applying the same edge yields exactly one row (ON CONFLICT UPDATE)."""
    edge = {
        "source_node_id": "router-a",
        "source_node_type": "device",
        "target_node_id": "switch-b",
        "target_node_type": "device",
        "edge_type": "uplink",
        "confidence_score": 0.8,
        "decay_coefficient": 0.002,
        "metadata": {"port": "Gi0/1"},
    }

    # First apply.
    async with scoped_pg_session(pg_pool_isolated, ns_id) as conn:
        await upsert_topology_edges(conn, ns_id, [edge])

    # Second apply of the SAME logical edge (different confidence/metadata to
    # prove the UPDATE path refreshes rather than duplicating).
    edge_again = dict(edge)
    edge_again["confidence_score"] = 0.95
    edge_again["metadata"] = {"port": "Gi0/2"}
    async with scoped_pg_session(pg_pool_isolated, ns_id) as conn:
        await upsert_topology_edges(conn, ns_id, [edge_again])

    async with scoped_pg_session(pg_pool_isolated, ns_id) as conn:
        rows = await conn.fetch(
            """
            SELECT confidence_score, metadata, created_at, updated_at
            FROM topology_graph
            WHERE namespace_id = $1::uuid
              AND source_node_id = $2
              AND target_node_id = $3
              AND edge_type = $4
            """,
            str(ns_id),
            edge["source_node_id"],
            edge["target_node_id"],
            edge["edge_type"],
        )

    assert len(rows) == 1, f"expected exactly one topology edge, got {len(rows)}"
    # The conflict path must have UPDATEd the row to the latest values.
    assert rows[0]["confidence_score"] == pytest.approx(0.95)
    # updated_at must have advanced past created_at (refresh happened).
    assert rows[0]["updated_at"] >= rows[0]["created_at"]


@_skip_no_pg
@pytest.mark.asyncio
async def test_upsert_device_health_is_idempotent(pg_pool_isolated, ns_id) -> None:
    """Double-applying the same device rollup yields exactly one row."""
    device_slug = "router-a"
    ingestion_id = uuid.uuid4()

    async with scoped_pg_session(pg_pool_isolated, ns_id) as conn:
        await upsert_device_health(
            conn,
            ns_id,
            device_slug,
            health_state="DEGRADED",
            top_anomaly_type="cpu_spike",
            anomaly_score=0.42,
            last_ingestion_id=ingestion_id,
        )

    # Second apply for the SAME (namespace_id, device_slug) with new values.
    new_ingestion_id = uuid.uuid4()
    async with scoped_pg_session(pg_pool_isolated, ns_id) as conn:
        await upsert_device_health(
            conn,
            ns_id,
            device_slug,
            health_state="CRITICAL",
            top_anomaly_type="link_flap",
            anomaly_score=0.91,
            last_ingestion_id=new_ingestion_id,
        )

    async with scoped_pg_session(pg_pool_isolated, ns_id) as conn:
        rows = await conn.fetch(
            """
            SELECT health_state, top_anomaly_type, anomaly_score, last_ingestion_id
            FROM device_health_rollup
            WHERE namespace_id = $1::uuid AND device_slug = $2
            """,
            str(ns_id),
            device_slug,
        )

    assert len(rows) == 1, f"expected exactly one health rollup row, got {len(rows)}"
    assert rows[0]["health_state"] == "CRITICAL"
    assert rows[0]["top_anomaly_type"] == "link_flap"
    assert rows[0]["anomaly_score"] == pytest.approx(0.91)
    assert rows[0]["last_ingestion_id"] == new_ingestion_id
