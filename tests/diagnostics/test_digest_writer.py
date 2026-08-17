"""Integration acceptance test for Batch 74 — CentralSink digest writer.

Proves that ``CentralSink.write`` lands a diagnostic ``Digest`` across the
cognitive layers ATOMICALLY with its WORM attestation:

* the Mongo digest archive returns a ``digest_payload_ref``;
* under a fresh namespace, after one ``write`` we observe
    - ≥1 ``memories`` row,
    - ≥1 ``kg_nodes`` row and ≥1 ``kg_edges`` row (``Device:``/``Room:``/
      ``Anomaly:`` graph),
    - ≥1 ``topology_graph`` row (physical Device→Room placement),
    - exactly 1 ``device_health_rollup`` row, and
    - EXACTLY ONE ``ingestion_completed`` ``event_log`` row.

Reuses the shared ``pg_pool`` / ``make_namespace`` fixtures (which bootstrap an
active signing key + verify the ``event_log`` schema) and constructs a real
``MemoryOrchestrator`` bound to the live PG pool + a real Motor Mongo client.
Redis is unused by ``CentralSink`` so it is mocked.

Requires the isolated test stack: PostgreSQL :5433 + MongoDB :27018 (+ a Minio
endpoint is configured by the environment but not exercised here). Run with
``-m integration``; skips cleanly when a backing store is unreachable.
"""

from __future__ import annotations

import os
import socket
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from nce.vertical_modules.diagnostics.digest_writer import CentralSink, DigestSink
from nce.vertical_modules.diagnostics.streaming import (
    Anomaly,
    Digest,
    WindowBucket,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Reachability guards
# ---------------------------------------------------------------------------


def _reachable(host: str, port: int) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=1)
        s.close()
        return True
    except OSError:
        return False


_MONGO_HOST, _MONGO_PORT = "127.0.0.1", 27018
_MONGO_OK = _reachable(_MONGO_HOST, _MONGO_PORT)

_skip_no_mongo = pytest.mark.skipif(
    not _MONGO_OK,
    reason="Integration test requires isolated MongoDB on 127.0.0.1:27018",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def mongo_client():
    """Real Motor client against the isolated Mongo; skips if driver/host absent."""
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:  # pragma: no cover - environment dependent
        pytest.skip("motor not installed")

    uri = os.environ.get("MONGO_URI", "mongodb://127.0.0.1:27018")
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=2000)
    try:
        await client.admin.command("ping")
    except Exception as exc:  # pragma: no cover - environment dependent
        client.close()
        pytest.skip(f"Cannot reach isolated Mongo: {exc}")
    try:
        yield client
    finally:
        client.close()


@pytest_asyncio.fixture
async def central_sink(pg_pool, mongo_client) -> CentralSink:
    """A CentralSink bound to the live PG pool + real Mongo (Redis unused→mock)."""
    from nce.orchestrators.memory import MemoryOrchestrator

    orch = MemoryOrchestrator(
        pg_pool=pg_pool,
        mongo_client=mongo_client,
        redis_client=AsyncMock(),
    )
    return CentralSink(orch)


def _sample_digest() -> Digest:
    """A small high-severity digest with two anomaly types + a rate window."""
    return Digest(
        processed_lines=512,
        anomalies=[
            Anomaly(
                anomaly_type="teams_app_crash",
                severity=2,  # critical → high-severity path (reinforce + CRITICAL)
                occurrences=4,
                sample="2026-06-21T10:00:00 Teams app crash: exit code 134",
            ),
            Anomaly(
                anomaly_type="ptp_desync",
                severity=3,
                occurrences=11,
                sample="2026-06-21T10:01:00 PTP desync detected: offset 523 us",
            ),
        ],
        windows=[
            WindowBucket(anomaly_type="ptp_desync", window_start=1_750_500_000, count=11),
        ],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_central_sink_satisfies_protocol() -> None:
    """CentralSink is a structural DigestSink (the public seam)."""
    assert issubclass(CentralSink, DigestSink)


@_skip_no_mongo
@pytest.mark.asyncio
async def test_central_sink_lands_digest_atomically(central_sink, make_namespace) -> None:
    """One write → cognitive-layer rows + EXACTLY ONE ingestion_completed event."""
    ns_id: uuid.UUID = await make_namespace()
    ns_str = str(ns_id)
    ingest_id = f"ingest-{uuid.uuid4().hex}"
    device_ctx = {
        "device_slug": "mtr-room-12",
        "site": "HQ",
        "location": "Room 12",
        "room": "Room 12",
        "tenant": "acme",
        "resolved": True,
    }

    ref = await central_sink.write(_sample_digest(), device_ctx, ingest_id, ns_id)
    assert isinstance(ref, str) and ref, "write must return a non-empty digest_payload_ref"

    pool = central_sink._orch.pg_pool
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pool, ns_id) as conn:
        memories = await conn.fetchval(
            "SELECT count(*) FROM memories WHERE namespace_id = $1::uuid", ns_str
        )
        kg_nodes = await conn.fetchval(
            "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1::uuid", ns_str
        )
        kg_edges = await conn.fetchval(
            "SELECT count(*) FROM kg_edges WHERE namespace_id = $1::uuid", ns_str
        )
        topo = await conn.fetchval(
            "SELECT count(*) FROM topology_graph WHERE namespace_id = $1::uuid", ns_str
        )
        health_rows = await conn.fetch(
            """
            SELECT device_slug, health_state, top_anomaly_type
            FROM device_health_rollup
            WHERE namespace_id = $1::uuid
            """,
            ns_str,
        )
        events = await conn.fetch(
            """
            SELECT params
            FROM event_log
            WHERE namespace_id = $1::uuid AND event_type = 'ingestion_completed'
            """,
            ns_str,
        )

    assert memories >= 1, f"expected ≥1 memories row, got {memories}"
    assert kg_nodes >= 1, f"expected ≥1 kg_nodes row, got {kg_nodes}"
    assert kg_edges >= 1, f"expected ≥1 kg_edges row, got {kg_edges}"
    assert topo >= 1, f"expected ≥1 topology_graph row, got {topo}"

    assert len(health_rows) == 1, f"expected exactly one health rollup, got {len(health_rows)}"
    assert health_rows[0]["device_slug"] == "mtr-room-12"
    # severity 2 (critical) maps to CRITICAL.
    assert health_rows[0]["health_state"] == "CRITICAL"

    assert len(events) == 1, f"expected EXACTLY ONE ingestion_completed event, got {len(events)}"
    import json

    params = json.loads(events[0]["params"])
    # Attestation carries only the 5 registered keys (no raw content / PII).
    assert set(params) == {
        "ingest_id",
        "device_slug",
        "digest_payload_ref",
        "anomaly_count",
        "processed_lines",
    }
    assert params["ingest_id"] == ingest_id
    assert params["device_slug"] == "mtr-room-12"
    assert params["digest_payload_ref"] == ref
    assert params["anomaly_count"] == 2
    assert params["processed_lines"] == 512
