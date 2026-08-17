"""Integration acceptance test for Batch 75 — process_diag_bundle worker.

Drives the end-to-end RQ task body against the LIVE stack (PostgreSQL :5433 +
MongoDB :27018 + MinIO :9004 + Redis :6380) and proves the three contract
behaviours:

1. **Happy path** — upload a small synthetic bundle to MinIO under a tenant-
   prefixed key, register a ``PENDING`` ``diag_ingestions`` row, run
   ``process_diag_bundle``; the row becomes ``DIGESTED`` and the cognitive rows
   exist (``memories`` + ``topology_graph`` + ``device_health_rollup``).
2. **Poison path** — a corrupt/zip-bomb archive lands in the DLQ and the row is
   marked ``FAILED`` WITHOUT infinite retry (non-retryable arm returns cleanly).
3. **Idempotency** — re-running the SAME ``ingest_id`` after a successful digest
   is a no-op (no duplicate cognitive writes).

Requires ``-m integration``; skips cleanly when a backing store is unreachable.
"""

from __future__ import annotations

import io
import socket
import uuid
import zipfile

import pytest
import pytest_asyncio

from nce.config import cfg

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


_MONGO_OK = _reachable("127.0.0.1", 27018)
_MINIO_OK = _reachable("127.0.0.1", 9004)
_REDIS_OK = _reachable("127.0.0.1", 6380)

_skip_stack = pytest.mark.skipif(
    not (_MONGO_OK and _MINIO_OK and _REDIS_OK),
    reason="Batch-75 worker integration needs Mongo:27018 + MinIO:9004 + Redis:6380",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def minio_client():
    """Real MinIO client against the isolated stack; skips if unreachable."""
    if not _MINIO_OK:
        pytest.skip("MinIO not reachable on 127.0.0.1:9004")
    from minio import Minio

    client = Minio(
        cfg.MINIO_ENDPOINT,
        access_key=cfg.MINIO_ACCESS_KEY,
        secret_key=cfg.MINIO_SECRET_KEY,
        secure=cfg.MINIO_SECURE,
    )
    bucket = cfg.NCE_DIAG_LANDING_BUCKET
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Cannot prepare MinIO landing bucket: {exc}")
    return client


def _upload(minio_client, ns_str: str, object_suffix: str, data: bytes) -> tuple[str, str]:
    """Upload *data* under a tenant-prefixed key; return (object_name, landing_uri)."""
    bucket = cfg.NCE_DIAG_LANDING_BUCKET
    object_name = f"{ns_str.lower()}/diag/mtr-room-1/{object_suffix}"
    minio_client.put_object(
        bucket,
        object_name,
        io.BytesIO(data),
        length=len(data),
        content_type="application/octet-stream",
    )
    return object_name, f"s3://{bucket}/{object_name}"


def _zip_bundle(entries: dict[str, str]) -> bytes:
    """Build an in-memory zip bundle from {member_name: text}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in entries.items():
            zf.writestr(name, text)
    return buf.getvalue()


async def _register_pending(
    pg_pool, ns_str: str, ingest_id: str, landing_uri: str, device_slug: str
) -> None:
    """Insert a PENDING diag_ingestions row (owner conn — bypasses RLS for setup)."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO diag_ingestions (
                namespace_id, ingest_id, source, vendor_profile,
                device_slug, landing_uri, status
            )
            VALUES ($1::uuid, $2, 'upload', 'mtr', $3, $4, 'PENDING')
            ON CONFLICT (namespace_id, ingest_id) DO NOTHING
            """,
            ns_str,
            ingest_id,
            device_slug,
            landing_uri,
        )


@pytest_asyncio.fixture
async def diag_env(monkeypatch):
    """Ensure the diagnostics flag is on + NetBox is unconfigured for the worker."""
    monkeypatch.setattr(cfg, "NCE_DIAG_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "NCE_NETBOX_URL", "", raising=False)
    monkeypatch.setattr(cfg, "NCE_NETBOX_TOKEN", "", raising=False)
    yield


@pytest.fixture
def resolved_room(monkeypatch):
    """Stub the worker's best-effort NetBox enrichment to a RESOLVED device+room.

    NetBox is not part of the local integration stack, so the real
    ``_resolve_context`` returns the non-resolved echo shape (room=None) and the
    CentralSink emits no physical topology edge.  Patching it to a resolved
    context (with a room) exercises the full ``topology_graph`` write path
    end-to-end without standing up NetBox — the download / stream / digest /
    CentralSink writes are all still real.
    """
    from nce.vertical_modules.diagnostics import worker as _worker

    async def _fake_resolve(device_slug):
        return {
            "device_slug": device_slug or "mtr-room-1",
            "site": "HQ",
            "location": "Room 1",
            "room": "Room 1",
            "tenant": "acme",
            "resolved": True,
        }

    monkeypatch.setattr(_worker, "_resolve_context", _fake_resolve)
    return _fake_resolve


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@_skip_stack
@pytest.mark.asyncio
async def test_happy_path_digests_and_lands_rows(
    pg_pool, make_namespace, minio_client, diag_env, resolved_room
) -> None:
    """Upload → PENDING → process_diag_bundle ⇒ DIGESTED + cognitive rows."""
    from nce.tasks import process_diag_bundle

    ns_id = await make_namespace()
    ns_str = str(ns_id)
    ingest_id = f"ingest-{uuid.uuid4().hex}"
    device_slug = "mtr-room-1"

    bundle = _zip_bundle(
        {
            "appliance.log": (
                "2026-06-21T10:00:00 Teams app crash: exit code 134\n"
                "2026-06-21T10:01:00 PTP desync detected: offset 523 us\n"
                "2026-06-21T10:02:00 INFO heartbeat ok\n"
                "2026-06-21T10:03:00 USB disconnect on port 3\n"
            )
        }
    )
    _, landing_uri = _upload(minio_client, ns_str, f"{ingest_id}.zip", bundle)
    await _register_pending(pg_pool, ns_str, ingest_id, landing_uri, device_slug)

    result = process_diag_bundle(
        ingest_id=ingest_id,
        namespace_id=ns_str,
        landing_uri=landing_uri,
        vendor_profile="mtr",
        device_slug=device_slug,
    )
    assert result["status"] == "digested", result
    assert result["anomaly_count"] >= 1

    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        status = await conn.fetchval(
            "SELECT status FROM diag_ingestions WHERE namespace_id = $1::uuid AND ingest_id = $2",
            ns_str,
            ingest_id,
        )
        memories = await conn.fetchval(
            "SELECT count(*) FROM memories WHERE namespace_id = $1::uuid", ns_str
        )
        topo = await conn.fetchval(
            "SELECT count(*) FROM topology_graph WHERE namespace_id = $1::uuid", ns_str
        )
        health = await conn.fetchval(
            "SELECT count(*) FROM device_health_rollup WHERE namespace_id = $1::uuid",
            ns_str,
        )

    assert status == "DIGESTED", f"expected DIGESTED, got {status!r}"
    assert memories >= 1, f"expected ≥1 memories row, got {memories}"
    assert topo >= 1, f"expected ≥1 topology_graph row, got {topo}"
    assert health == 1, f"expected exactly 1 device_health_rollup row, got {health}"


@_skip_stack
@pytest.mark.asyncio
async def test_idempotent_rerun_is_noop(pg_pool, make_namespace, minio_client, diag_env) -> None:
    """Re-running the SAME ingest_id after DIGESTED is a no-op (no double-write)."""
    from nce.tasks import process_diag_bundle

    ns_id = await make_namespace()
    ns_str = str(ns_id)
    ingest_id = f"ingest-{uuid.uuid4().hex}"

    bundle = _zip_bundle({"appliance.log": "2026-06-21T10:00:00 Teams app crash: exit code 134\n"})
    _, landing_uri = _upload(minio_client, ns_str, f"{ingest_id}.zip", bundle)
    await _register_pending(pg_pool, ns_str, ingest_id, landing_uri, "mtr-room-1")

    first = process_diag_bundle(
        ingest_id=ingest_id,
        namespace_id=ns_str,
        landing_uri=landing_uri,
        vendor_profile="mtr",
        device_slug="mtr-room-1",
    )
    assert first["status"] == "digested"

    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        memories_before = await conn.fetchval(
            "SELECT count(*) FROM memories WHERE namespace_id = $1::uuid", ns_str
        )

    second = process_diag_bundle(
        ingest_id=ingest_id,
        namespace_id=ns_str,
        landing_uri=landing_uri,
        vendor_profile="mtr",
        device_slug="mtr-room-1",
    )
    assert second["status"] == "noop", f"re-run must be a no-op, got {second!r}"

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        memories_after = await conn.fetchval(
            "SELECT count(*) FROM memories WHERE namespace_id = $1::uuid", ns_str
        )

    assert memories_after == memories_before, (
        "idempotent re-run must not write additional memories "
        f"(before={memories_before}, after={memories_after})"
    )


@_skip_stack
@pytest.mark.asyncio
async def test_poison_bundle_dead_letters_without_retry(
    pg_pool, make_namespace, minio_client, diag_env
) -> None:
    """A corrupt archive → FAILED + DLQ row, returned cleanly (no infinite retry)."""
    from nce.tasks import process_diag_bundle

    ns_id = await make_namespace()
    ns_str = str(ns_id)
    ingest_id = f"ingest-{uuid.uuid4().hex}"

    # A zip magic header followed by garbage — sniffs as zip, fails to parse.
    corrupt = b"PK\x03\x04" + b"\x00garbage-not-a-real-zip" * 64
    _, landing_uri = _upload(minio_client, ns_str, f"{ingest_id}.zip", corrupt)
    await _register_pending(pg_pool, ns_str, ingest_id, landing_uri, "mtr-room-1")

    result = process_diag_bundle(
        ingest_id=ingest_id,
        namespace_id=ns_str,
        landing_uri=landing_uri,
        vendor_profile="mtr",
        device_slug="mtr-room-1",
    )
    # Non-retryable arm returns cleanly (no re-raise → no RQ re-enqueue spin).
    assert result["status"] == "dead_lettered", result

    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        status = await conn.fetchval(
            "SELECT status FROM diag_ingestions WHERE namespace_id = $1::uuid AND ingest_id = $2",
            ns_str,
            ingest_id,
        )

    assert status == "FAILED", f"poison bundle must mark row FAILED, got {status!r}"

    # DLQ row exists for this task, carrying ids + reason only (no raw content).
    async with pg_pool.acquire() as conn:
        dlq = await conn.fetchrow(
            """
            SELECT kwargs, error_message
            FROM dead_letter_queue
            WHERE task_name = 'process_diag_bundle'
              AND kwargs->>'ingest_id' = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            ingest_id,
        )
    assert dlq is not None, "expected a DLQ row for the poison bundle"

    import json

    kwargs = json.loads(dlq["kwargs"]) if isinstance(dlq["kwargs"], str) else dlq["kwargs"]
    # DLQ payload is ids + reason — never raw bundle bytes / log content.
    assert kwargs.get("ingest_id") == ingest_id
    assert "garbage" not in json.dumps(kwargs)
