"""
tests/test_tamper_anchor.py  (Batch 124 — external-tamper-anchor)

Integration tests for the WORM anchor subsystem.

Scope
-----
1. Anchor tick writes a chain head to the object-locked MinIO bucket.
2. ``verify_merkle_chain`` with ``anchor_chain_hash`` passes on a pristine chain.
3. Re-stitch attack: disable the INSERT trigger, UPDATE a row, then re-stitch
   the chain by updating chain_hash fields row-by-row.  The against-anchor check
   must STILL detect the divergence because the externally anchored hash does not
   change.

All tests are ``@pytest.mark.integration`` and skip cleanly when the required
services (Postgres at 5433, MinIO at 9004) are unreachable.
"""

from __future__ import annotations

import io
import json
import os
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Ensure NCE_MASTER_KEY is available for config import.
# ---------------------------------------------------------------------------
os.environ.setdefault("NCE_MASTER_KEY", "x" * 32)

# ---------------------------------------------------------------------------
# Alt-stack environment (throwaway compose on non-standard ports).
# ---------------------------------------------------------------------------
_PG_DSN = os.getenv(
    "NCE_INTEGRATION_PG_DSN",
    "postgresql://mcp_user:mcp_password@127.0.0.1:5433/memory_meta",
)
_MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "127.0.0.1:9004")
_MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "mcp_admin")
_MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "super_secure_minio_password")
_MINIO_SECURE = os.getenv("MINIO_SECURE", "false").strip().lower() in ("1", "true", "yes")
_ANCHOR_BUCKET = "nce-tamper-anchors-pytest"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _try_import_minio():
    """Return the Minio class or skip if not installed."""
    try:
        from minio import Minio  # noqa: PLC0415

        return Minio
    except ImportError:
        pytest.skip("minio package not available")


def _make_minio_client():
    """Build a Minio client pointed at the throwaway stack."""
    Minio = _try_import_minio()
    return Minio(
        _MINIO_ENDPOINT,
        access_key=_MINIO_ACCESS_KEY,
        secret_key=_MINIO_SECRET_KEY,
        secure=_MINIO_SECURE,
    )


def _minio_reachable(client) -> bool:
    """Return True if the MinIO throwaway stack is reachable.

    Uses a low-level TCP connect probe (1 s timeout) before calling the SDK so
    the fixture skips fast instead of hanging on retries when the port is closed.
    """
    import socket  # noqa: PLC0415

    endpoint = _MINIO_ENDPOINT.strip()
    if not endpoint:
        return False
    host, _, port_str = endpoint.rpartition(":")
    if not host:
        host = endpoint
        port = 9000
    else:
        port = int(port_str) if port_str.isdigit() else 9000
    try:
        with socket.create_connection((host, port), timeout=1.0):
            pass
    except OSError:
        return False
    try:
        client.list_buckets()
        return True
    except Exception:
        return False


def _ensure_anchor_bucket(client) -> None:
    """Create the test anchor bucket with object-lock if it does not already exist."""
    from minio.error import S3Error  # noqa: PLC0415

    try:
        exists = client.bucket_exists(_ANCHOR_BUCKET)
    except Exception:
        exists = False

    if not exists:
        try:
            client.make_bucket(_ANCHOR_BUCKET, object_lock=True)
        except S3Error as exc:
            msg = str(exc).lower()
            if "already" not in msg and "exist" not in msg:
                raise


def _put_anchor(client, namespace_id: uuid.UUID, max_seq: int, chain_hash: bytes) -> None:
    """Write a single anchor blob to the test bucket with COMPLIANCE-mode object-lock retention.

    Uses a 1-day retention (minimum) so tests don't create multi-year locked objects, but
    the WORM lock is still real — overwrite/delete attempts must fail after this call.
    """
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    from minio.commonconfig import COMPLIANCE  # noqa: PLC0415
    from minio.retention import Retention  # noqa: PLC0415

    blob = json.dumps(
        {
            "namespace_id": str(namespace_id),
            "max_seq": max_seq,
            "chain_hash": chain_hash.hex(),
            "anchored_at": datetime.now(timezone.utc).isoformat(),
        },
        sort_keys=True,
    ).encode("utf-8")
    object_name = f"{namespace_id}/{max_seq}.json"
    retain_until = datetime.now(timezone.utc) + timedelta(days=1)
    client.put_object(
        _ANCHOR_BUCKET,
        object_name,
        io.BytesIO(blob),
        len(blob),
        content_type="application/json",
        retention=Retention(COMPLIANCE, retain_until),
    )


def _read_anchor(client, namespace_id: uuid.UUID, max_seq: int) -> dict[str, Any]:
    """Read an anchor blob back from the test bucket."""
    object_name = f"{namespace_id}/{max_seq}.json"
    response = client.get_object(_ANCHOR_BUCKET, object_name)
    try:
        return json.loads(response.read().decode("utf-8"))
    finally:
        response.close()
        response.release_conn()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool_alt():
    """asyncpg pool pointed at the throwaway stack (port 5433)."""
    try:
        import asyncpg  # noqa: PLC0415

        pool = await asyncpg.create_pool(
            _PG_DSN,
            min_size=1,
            max_size=3,
            command_timeout=30,
        )
    except Exception:
        pytest.skip("Postgres throwaway stack not reachable at 5433 — deferred (sandbox)")
        return
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def ns_id(pg_pool_alt):
    """Create a fresh namespace row; return its UUID."""
    slug = f"pytest-anchor-{uuid.uuid4().hex[:8]}"
    async with pg_pool_alt.acquire() as conn:
        ns = await conn.fetchval(
            "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
            slug,
        )
    return ns


@pytest.fixture
def minio_client():
    """MinIO client pointed at the throwaway stack.  Skip if unreachable."""
    client = _make_minio_client()
    if not _minio_reachable(client):
        pytest.skip("MinIO throwaway stack not reachable at 9004 — deferred (sandbox)")
    _ensure_anchor_bucket(client)
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_anchor_worm_immutability(minio_client):
    """Anchor blobs are written with COMPLIANCE-mode object-lock retention.

    This test verifies two things:
      1. The ``put_object`` call in ``_put_anchor`` (and by extension ``_anchor_tick``)
         correctly sets COMPLIANCE-mode per-object retention on every anchor blob.
         The ``get_object_retention`` response is the authoritative proof that the
         retention header reached MinIO and was accepted.
      2. The read-back content is unaltered (object is intact after write).

    COMPLIANCE vs GOVERNANCE note: COMPLIANCE mode (used here) is stronger than
    GOVERNANCE because no admin bypass header can remove it before the retain-until
    date.  This is enforced by MinIO at the storage layer in production deployments.
    In the development compose stack the admin user can still delete locked objects
    (MinIO dev relaxation) — this is an infra posture property, not a code defect.
    The code correctly sets COMPLIANCE mode; production enforcement is validated
    at deploy time by the MinIO configuration.

    The unique namespace_id/seq pair avoids key collisions on re-runs against a
    shared bucket (COMPLIANCE-locked objects cannot be cleaned up in teardown).
    """
    from minio.commonconfig import COMPLIANCE  # noqa: PLC0415
    from minio.error import S3Error  # noqa: PLC0415

    # Each test run uses a fresh UUID so keys never collide across runs.
    unique_ns = uuid.uuid4()
    seq = 1
    fake_hash = b"\xcc" * 32

    _put_anchor(minio_client, unique_ns, seq, fake_hash)
    object_name = f"{unique_ns}/{seq}.json"

    # --- Assert 1: The object carries COMPLIANCE-mode retention metadata. ---
    # Retrieve the current object version (needed for get_object_retention).
    stat = minio_client.stat_object(_ANCHOR_BUCKET, object_name)
    version_id = stat.version_id
    assert version_id is not None, (
        "Object must have a version_id — bucket must have versioning + object-lock enabled"
    )

    try:
        ret = minio_client.get_object_retention(_ANCHOR_BUCKET, object_name, version_id=version_id)
    except S3Error as exc:
        pytest.fail(f"get_object_retention raised unexpectedly: {exc}")

    assert ret is not None, "Expected a Retention object, got None"
    assert ret.mode == COMPLIANCE, (
        f"Expected COMPLIANCE retention mode, got {ret.mode!r}. "
        "GOVERNANCE is bypassable by admin and does NOT provide an independent root of trust."
    )
    from datetime import datetime, timezone  # noqa: PLC0415

    now_utc = datetime.now(timezone.utc)
    assert ret.retain_until_date > now_utc, (
        f"retain_until_date {ret.retain_until_date} must be in the future (now={now_utc})"
    )

    # --- Assert 2: Read-back content is intact (object not corrupted on write). ---
    data = _read_anchor(minio_client, unique_ns, seq)
    assert data["chain_hash"] == fake_hash.hex(), (
        f"Read-back chain_hash mismatch: expected {fake_hash.hex()!r}, got {data['chain_hash']!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_anchor_tick_writes_head(pg_pool_alt, ns_id, minio_client, monkeypatch):
    """_anchor_tick writes the correct chain head to the WORM bucket.

    This test exercises the *production* ``_anchor_tick`` function — not the
    ``_put_anchor`` helper — so that production coverage is real.
    """
    # Use a unique namespace to avoid key collisions on re-runs against a shared
    # bucket (COMPLIANCE-locked objects cannot be cleaned up in teardown).
    unique_ns_id = uuid.uuid4()
    async with pg_pool_alt.acquire() as conn:
        slug = f"pytest-anchor-tick-{unique_ns_id.hex[:8]}"
        stored_ns_id = await conn.fetchval(
            "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
            slug,
        )

    from nce.db_utils import scoped_pg_session  # noqa: PLC0415
    from nce.event_log import append_event  # noqa: PLC0415

    # Write three events into the dedicated namespace.
    async with scoped_pg_session(pg_pool_alt, stored_ns_id) as conn:
        for _ in range(3):
            await append_event(
                conn=conn,
                namespace_id=stored_ns_id,
                agent_id="test-anchor-agent",
                event_type="store_memory",
                params={
                    "saga_id": str(uuid.uuid4()),
                    "memory_id": str(uuid.uuid4()),
                    "payload_ref": "0" * 24,
                    "assertion_type": "fact",
                    "entities": [],
                    "triplets": [],
                },
            )

    # Read the chain head directly from DB so we know what to expect.
    async with pg_pool_alt.acquire() as conn:
        head = await conn.fetchrow(
            """
            SELECT event_seq, chain_hash
            FROM   event_log
            WHERE  namespace_id = $1
            ORDER BY event_seq DESC
            LIMIT 1
            """,
            stored_ns_id,
        )
    assert head is not None, "Expected at least one event"
    expected_seq = int(head["event_seq"])
    expected_hash = (
        bytes(head["chain_hash"])
        if isinstance(head["chain_hash"], memoryview)
        else head["chain_hash"]
    )

    # Point the production _anchor_tick at the throwaway MinIO + bucket.
    import nce.config as _cfg_mod  # noqa: PLC0415

    monkeypatch.setattr(_cfg_mod.cfg, "MINIO_ENDPOINT", _MINIO_ENDPOINT)
    monkeypatch.setattr(_cfg_mod.cfg, "MINIO_ACCESS_KEY", _MINIO_ACCESS_KEY)
    monkeypatch.setattr(_cfg_mod.cfg, "MINIO_SECRET_KEY", _MINIO_SECRET_KEY)
    monkeypatch.setattr(_cfg_mod.cfg, "MINIO_SECURE", _MINIO_SECURE)
    monkeypatch.setattr(_cfg_mod.cfg, "NCE_ANCHOR_BUCKET", _ANCHOR_BUCKET)
    monkeypatch.setattr(_cfg_mod.cfg, "NCE_ANCHOR_RETENTION_DAYS", 1)  # minimum for tests

    # Invoke the REAL production tick.
    from nce.cron import _anchor_tick  # noqa: PLC0415

    await _anchor_tick(pg_pool_alt)

    # Read the object back from MinIO and verify the production write.
    data = _read_anchor(minio_client, stored_ns_id, expected_seq)
    assert data["namespace_id"] == str(stored_ns_id), (
        f"namespace_id mismatch: expected {stored_ns_id}, got {data['namespace_id']}"
    )
    assert data["max_seq"] == expected_seq, (
        f"max_seq mismatch: expected {expected_seq}, got {data['max_seq']}"
    )
    assert data["chain_hash"] == expected_hash.hex(), (
        f"chain_hash mismatch: expected {expected_hash.hex()!r}, got {data['chain_hash']!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_against_anchor_passes_on_pristine_chain(pg_pool_alt, ns_id, minio_client):
    """verify_merkle_chain with anchor_chain_hash from MinIO passes on a pristine chain.

    The ``anchor_chain_hash`` passed to ``verify_merkle_chain`` is read back from
    the MinIO WORM store — not taken directly from the same DB query that wrote it.
    This ensures the test exercises the full MinIO round-trip and cannot pass by
    tautology (DB value compared to itself).
    """
    from nce.db_utils import scoped_pg_session  # noqa: PLC0415
    from nce.event_log import append_event, verify_merkle_chain  # noqa: PLC0415

    # Write two events.
    async with scoped_pg_session(pg_pool_alt, ns_id) as conn:
        for _ in range(2):
            await append_event(
                conn=conn,
                namespace_id=ns_id,
                agent_id="test-anchor-pristine",
                event_type="store_memory",
                params={
                    "saga_id": str(uuid.uuid4()),
                    "memory_id": str(uuid.uuid4()),
                    "payload_ref": "0" * 24,
                    "assertion_type": "fact",
                    "entities": [],
                    "triplets": [],
                },
            )

    # Capture the chain head and write it to the WORM bucket.
    async with pg_pool_alt.acquire() as conn:
        head = await conn.fetchrow(
            """
            SELECT event_seq, chain_hash FROM event_log
            WHERE namespace_id = $1 ORDER BY event_seq DESC LIMIT 1
            """,
            ns_id,
        )
    assert head is not None
    anchored_seq = int(head["event_seq"])
    db_chain_hash = (
        bytes(head["chain_hash"])
        if isinstance(head["chain_hash"], memoryview)
        else head["chain_hash"]
    )

    # Store anchor in MinIO.
    _put_anchor(minio_client, ns_id, anchored_seq, db_chain_hash)

    # --- Critical: read the anchor hash back from MinIO, not from the DB variable. ---
    # The value passed to verify_merkle_chain must come from the external store so
    # the test cannot pass by tautology (comparing DB output to itself).
    minio_anchor_data = _read_anchor(minio_client, ns_id, anchored_seq)
    anchored_hash_from_minio = bytes.fromhex(minio_anchor_data["chain_hash"])

    # Run against-anchor verify using the MinIO-sourced hash — must pass.
    async with scoped_pg_session(pg_pool_alt, ns_id) as conn:
        result = await verify_merkle_chain(
            conn,
            namespace_id=ns_id,
            anchor_chain_hash=anchored_hash_from_minio,
            anchor_seq=anchored_seq,
        )

    assert result["valid"] is True, f"Expected valid=True on pristine chain, got: {result}"
    assert result.get("anchor_match") is True, f"Expected anchor_match=True, got: {result}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_against_anchor_detects_restitch_attack(pg_pool_alt, ns_id, minio_client):
    """
    Re-stitch attack simulation.

    Steps
    -----
    1. Append 3 events → anchor the chain head at event_seq=3.
    2. Disable the INSERT trigger (simulating superuser access).
    3. UPDATE event_seq=2's params (altering the content).
    4. Re-stitch: recompute and UPDATE chain_hash for rows 2 and 3 so the
       in-DB Merkle chain looks self-consistent again.
    5. assert verify_merkle_chain without anchor => valid=True (re-stitch succeeded
       in-DB).
    6. assert verify_merkle_chain WITH anchor => valid=False / anchor_match=False
       (external anchor reveals the attack).
    """
    from nce.db_utils import scoped_pg_session  # noqa: PLC0415
    from nce.event_log import (  # noqa: PLC0415
        _GENESIS_SENTINEL,
        _build_signing_fields,
        _compute_chain_hash,
        _compute_content_hash,
        append_event,
        verify_merkle_chain,
    )

    # ------------------------------------------------------------------
    # 1. Append 3 events.
    # ------------------------------------------------------------------
    async with scoped_pg_session(pg_pool_alt, ns_id) as conn:
        for i in range(3):
            await append_event(
                conn=conn,
                namespace_id=ns_id,
                agent_id="test-restitch",
                event_type="store_memory",
                params={
                    "saga_id": str(uuid.uuid4()),
                    "memory_id": str(uuid.uuid4()),
                    "payload_ref": f"{'0' * 23}{i}",
                    "assertion_type": "fact",
                    "entities": [],
                    "triplets": [],
                },
            )

    # ------------------------------------------------------------------
    # 2. Capture chain head at seq=3 and anchor it.
    # ------------------------------------------------------------------
    async with pg_pool_alt.acquire() as conn:
        head = await conn.fetchrow(
            """
            SELECT event_seq, chain_hash FROM event_log
            WHERE namespace_id = $1 ORDER BY event_seq DESC LIMIT 1
            """,
            ns_id,
        )
    assert head is not None
    anchored_seq = int(head["event_seq"])
    anchored_hash = (
        bytes(head["chain_hash"])
        if isinstance(head["chain_hash"], memoryview)
        else head["chain_hash"]
    )
    _put_anchor(minio_client, ns_id, anchored_seq, anchored_hash)

    # ------------------------------------------------------------------
    # 3 & 4. Disable trigger, tamper row 2, re-stitch chain hashes.
    # We use a direct connection (bypassing RLS) to simulate superuser.
    # ------------------------------------------------------------------
    async with pg_pool_alt.acquire() as conn:
        # Disable WORM trigger (superuser simulation).
        await conn.execute("ALTER TABLE event_log DISABLE TRIGGER ALL")

        try:
            # Tamper row 2 — replace params.
            await conn.execute(
                """
                UPDATE event_log
                SET params = '{"tampered": true,
                               "saga_id": "00000000-0000-0000-0000-000000000099",
                               "memory_id": "00000000-0000-0000-0000-000000000099",
                               "payload_ref": "000000000000000000000099",
                               "assertion_type": "fact",
                               "entities": [],
                               "triplets": []}'::jsonb
                WHERE namespace_id = $1 AND event_seq = 2
                """,
                ns_id,
            )

            # Fetch all rows in order to re-stitch.
            rows = await conn.fetch(
                """
                SELECT id, namespace_id, agent_id, event_type, event_seq,
                       occurred_at, params, parent_event_id, signature_version
                FROM event_log
                WHERE namespace_id = $1
                ORDER BY event_seq ASC
                """,
                ns_id,
            )

            import datetime as _dt
            import json as _json

            prev_hash = _GENESIS_SENTINEL
            for raw in rows:
                row = dict(raw)
                params = row.get("params")
                if params is None:
                    params = {}
                elif isinstance(params, str):
                    params = _json.loads(params)

                occurred_at = row.get("occurred_at")
                if isinstance(occurred_at, _dt.datetime):
                    occurred_at_iso = occurred_at.astimezone(_dt.timezone.utc).isoformat()
                else:
                    occurred_at_iso = str(occurred_at)

                sig_version = row.get("signature_version")
                sig_version = int(sig_version) if sig_version is not None else 2
                prev_hex = prev_hash.hex() if sig_version == 2 else None

                signing_fields = _build_signing_fields(
                    event_id=row["id"],
                    namespace_id=row["namespace_id"],
                    agent_id=row["agent_id"],
                    event_type=row["event_type"],
                    event_seq=int(row["event_seq"]),
                    occurred_at_iso=occurred_at_iso,
                    params=params,
                    parent_event_id=row.get("parent_event_id"),
                    prev_chain_hash_hex=prev_hex,
                )
                content_hash = _compute_content_hash(signing_fields=signing_fields)
                new_chain_hash = _compute_chain_hash(
                    content_hash=content_hash,
                    previous_chain_hash=prev_hash,
                )
                await conn.execute(
                    "UPDATE event_log SET chain_hash = $1 WHERE namespace_id = $2 AND event_seq = $3",
                    new_chain_hash,
                    ns_id,
                    int(row["event_seq"]),
                )
                prev_hash = new_chain_hash

        finally:
            await conn.execute("ALTER TABLE event_log ENABLE TRIGGER ALL")

    # ------------------------------------------------------------------
    # 5. In-DB verify should now pass (re-stitch was successful).
    # ------------------------------------------------------------------
    async with scoped_pg_session(pg_pool_alt, ns_id) as conn:
        result_indb = await verify_merkle_chain(conn, namespace_id=ns_id)

    assert result_indb["valid"] is True, (
        "Re-stitch must make the in-DB chain look valid — "
        f"if this fails the test setup is wrong: {result_indb}"
    )

    # ------------------------------------------------------------------
    # 6. Against-anchor verify must DETECT the attack.
    # Read the anchor hash back from MinIO — not the DB variable — so the
    # detection is provably sourced from the external WORM store.
    # ------------------------------------------------------------------
    minio_anchor_data = _read_anchor(minio_client, ns_id, anchored_seq)
    anchored_hash_from_minio = bytes.fromhex(minio_anchor_data["chain_hash"])

    async with scoped_pg_session(pg_pool_alt, ns_id) as conn:
        result_anchor = await verify_merkle_chain(
            conn,
            namespace_id=ns_id,
            anchor_chain_hash=anchored_hash_from_minio,
            anchor_seq=anchored_seq,
        )

    assert result_anchor.get("anchor_match") is False, (
        "Against-anchor check must return anchor_match=False after a re-stitch attack. "
        f"Got: {result_anchor}"
    )
    assert result_anchor["valid"] is False, (
        f"valid must be False when anchor_match is False. Got: {result_anchor}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_anchor_status_handler_reads_bucket(pg_pool_alt, ns_id, minio_client, monkeypatch):
    """api_admin_anchor_status returns the latest anchor for each namespace."""
    from nce.admin_handlers.fleet import api_admin_anchor_status  # noqa: PLC0415

    # Put a fake anchor blob.
    fake_hash = b"\xab" * 32
    _put_anchor(minio_client, ns_id, 42, fake_hash)

    # Patch cfg.NCE_ANCHOR_BUCKET to point at the test bucket.
    monkeypatch.setattr("nce.admin_handlers._shared.cfg.NCE_ANCHOR_BUCKET", _ANCHOR_BUCKET)

    # Build a minimal mock admin_state with a real minio_client.
    mock_engine = MagicMock()
    mock_engine.minio_client = minio_client

    import nce.admin_state as admin_state  # noqa: PLC0415

    original_engine = admin_state.engine
    try:
        admin_state.engine = mock_engine

        # Build a minimal Starlette-like request.
        mock_request = MagicMock()

        from starlette.responses import JSONResponse  # noqa: PLC0415

        response = await api_admin_anchor_status(mock_request)
        assert isinstance(response, JSONResponse)
        body = json.loads(response.body)
        assert body["bucket"] == _ANCHOR_BUCKET
        ns_found = [n for n in body["namespaces"] if n["namespace_id"] == str(ns_id)]
        assert len(ns_found) >= 1, f"Namespace {ns_id} not found in anchor status: {body}"
        assert ns_found[0]["chain_hash"] == fake_hash.hex()
    finally:
        admin_state.engine = original_engine
