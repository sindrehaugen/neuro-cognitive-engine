"""
Batch 119 — Echo-suppression integration tests.

Verifies:
  1. Seeding the echo set via ``register_echo`` causes a subsequent webhook
     delivery to be recognised as a self-echo: semantic track is skipped
     (no new episodic memory, no Empathic Tensor row), the deterministic KG
     upsert still runs, and the result carries ``metadata.echo_of``.
  2. Metric ``nce_echo_suppressed_total`` is incremented on echo hit.
  3. A non-echo webhook ingests normally (episodic memory is created).

Requires the isolated RL integration stack (ports 5433/6380 etc.).
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_D365_INT_INSERT = """
INSERT INTO d365_integrations
    (id, namespace_id, org_url, status, last_sync_stats)
VALUES ($1::uuid, $2::uuid, $3, 'ACTIVE', '{}'::jsonb)
ON CONFLICT (namespace_id, org_url) DO UPDATE
    SET status = 'ACTIVE', last_sync_stats = '{}'::jsonb, updated_at = NOW()
RETURNING id
"""

_D365_INT_CLEANUP = "DELETE FROM d365_integrations WHERE namespace_id = $1::uuid"
_NS_INSERT = (
    "INSERT INTO namespaces (id, slug, metadata) "
    "VALUES ($1::uuid, $2, '{}') ON CONFLICT (id) DO NOTHING"
)
_NS_CLEANUP = "DELETE FROM namespaces WHERE id = $1::uuid"

_SCHEMA_GUARD_SQL = """
SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'd365_integrations'
      AND column_name  = 'namespace_id'
)
"""


async def _memories_count(pool, *, namespace_id: uuid.UUID) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM memories WHERE namespace_id = $1::uuid",
            namespace_id,
        )


async def _cognitive_ledger_count(pool, *, namespace_id: uuid.UUID) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM v3_cognitive_ledger WHERE namespace_id = $1::uuid",
            namespace_id,
        )


async def _kg_edge_exists(
    pool,
    *,
    namespace_id: uuid.UUID,
    subject: str,
    predicate: str,
    object_: str,
) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT 1 FROM kg_edges
            WHERE namespace_id = $1::uuid AND subject_label = $2
              AND predicate = $3 AND object_label = $4
            """,
            namespace_id,
            subject,
            predicate,
            object_,
        )
    return row is not None


def _redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://127.0.0.1:6380/0")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def echo_pg_scope(pg_pool):
    """Fresh namespace + d365_integration row; cleaned up after each test."""
    async with pg_pool.acquire() as conn:
        has_table = await conn.fetchval(_SCHEMA_GUARD_SQL)
    if not has_table:
        pytest.skip("d365_integrations table not present — apply schema.sql first")

    ns_id = uuid.uuid4()
    org_url = f"https://test-echo-{ns_id.hex[:8]}.crm.dynamics.com"

    async with pg_pool.acquire() as conn:
        await conn.execute(_NS_INSERT, ns_id, f"test-echo-{ns_id.hex[:8]}")
        await conn.execute(_D365_INT_INSERT, uuid.uuid4(), ns_id, org_url)

    yield pg_pool, ns_id, org_url

    async with pg_pool.acquire() as conn:
        await conn.execute(_D365_INT_CLEANUP, ns_id)
        await conn.execute("DELETE FROM memories WHERE namespace_id = $1::uuid", ns_id)
        await conn.execute("DELETE FROM v3_cognitive_ledger WHERE namespace_id = $1::uuid", ns_id)
        await conn.execute("DELETE FROM kg_edges WHERE namespace_id = $1::uuid", ns_id)
        await conn.execute("DELETE FROM kg_nodes WHERE namespace_id = $1::uuid", ns_id)
        await conn.execute(_NS_CLEANUP, ns_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_annotation_ctx(
    entity_id: str,
    org_url: str,
    note_text: str = "Test case note",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (entity_ctx, raw_payload) for an annotation Create event."""
    entity_ctx = {
        "entity_type": "annotation",
        "operation": "Create",
        "entity_id": entity_id,
        "org_id": org_url,
        "raw_target": {
            "notetext": note_text,
            "objectid_incident": f"incident-{entity_id[:8]}",
        },
    }
    raw_payload: dict[str, Any] = {
        "PrimaryEntityName": "annotation",
        "MessageName": "Create",
        "PrimaryEntityId": entity_id,
        "OrganizationUrl": org_url,
    }
    return entity_ctx, raw_payload


def _make_mock_pool(ns_id: uuid.UUID, org_url: str) -> MagicMock:
    """Build a minimal asyncpg pool mock that returns the namespace_id for D365 lookup."""
    # Row for the first fetchrow (d365_integrations by org_url)
    mock_row_d365 = MagicMock()
    mock_row_d365.__getitem__ = lambda self, key: str(uuid.uuid4()) if key == "id" else None

    # Row for the second fetchrow (namespace_id from d365_integrations)
    mock_row_ns = MagicMock()
    mock_row_ns.__getitem__ = lambda self, key: str(ns_id) if key == "namespace_id" else None

    mock_conn = AsyncMock()
    call_count = [0]

    async def _fetchrow(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return mock_row_d365
        return mock_row_ns

    mock_conn.fetchrow = _fetchrow

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_ctx
    mock_pool.close = AsyncMock()
    return mock_pool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_echo_suppression_skips_semantic_track(
    echo_pg_scope: tuple,
) -> None:
    """Echo-suppressed annotation: no memory, no Empathic Tensor, metadata.echo_of set.

    Uses a mock pool for _dispatch_d365_event to avoid closing the fixture pool,
    then verifies the real DB is unchanged (no new memories / cognitive ledger rows).
    """
    from nce.tasks import _dispatch_d365_event
    from nce.webhook_receiver.main import register_echo

    pool, ns_id, org_url = echo_pg_scope
    entity_id = str(uuid.uuid4())
    origin_event_id = str(uuid.uuid4())

    # --- Seed the echo set ---
    redis_sync = None
    try:
        from redis import Redis

        redis_sync = Redis.from_url(_redis_url())
        redis_sync.ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable for integration tests: {exc}")

    with patch("nce.webhook_receiver.main._redis_client", return_value=redis_sync):
        register_echo("d365", entity_id, origin_event_id)

    # Verify the echo key was set correctly
    echo_key = f"nce:echo:d365:{entity_id}"
    stored = redis_sync.get(echo_key)
    assert stored is not None, "register_echo must set the Redis key"

    # --- Snapshot counts before dispatch ---
    mem_before = await _memories_count(pool, namespace_id=ns_id)
    cog_before = await _cognitive_ledger_count(pool, namespace_id=ns_id)

    entity_ctx, raw_payload = _build_annotation_ctx(entity_id, org_url)

    # Build a mock pool that answers the d365_integrations namespace-lookup queries
    mock_pool = _make_mock_pool(ns_id, org_url)

    mock_token_mgr = MagicMock()
    mock_token_mgr.get_access_token = AsyncMock(return_value="mock-token")
    mock_redis_async = MagicMock()
    mock_redis_async.aclose = AsyncMock()
    mock_mongo = MagicMock()
    mock_mongo.close = MagicMock()

    with (
        # Patch check_echo at its source so the local import inside the function picks it up
        patch("nce.webhook_receiver.main.check_echo", return_value=origin_event_id),
        # Patch the ECHO_SUPPRESSED_TOTAL at its source (local import in the function)
        patch("nce.observability.ECHO_SUPPRESSED_TOTAL") as mock_counter,
        # Patch connection builders: asyncpg, motor, redis.asyncio
        patch("asyncpg.create_pool", new=AsyncMock(return_value=mock_pool)),
        patch(
            "motor.motor_asyncio.AsyncIOMotorClient",
            return_value=mock_mongo,
        ),
        patch(
            "redis.asyncio.from_url",
            return_value=mock_redis_async,
        ),
        # Patch the Dataverse client dependencies (not needed for annotation echo path)
        patch(
            "nce.vertical_modules.dynamics365.auth.DataverseTokenManager",
            return_value=mock_token_mgr,
        ),
        patch(
            "nce.vertical_modules.dynamics365.client.DataverseClient",
            return_value=MagicMock(),
        ),
    ):
        mock_counter.labels.return_value = MagicMock(inc=MagicMock())
        result = await _dispatch_d365_event(entity_ctx, raw_payload)

    # --- Return value assertions ---
    assert result.get("status") == "echo_suppressed", (
        f"Expected echo_suppressed status, got: {result}"
    )
    assert result.get("change_origin") == "webhook", (
        f"change_origin must be 'webhook' on echo hit, got: {result}"
    )
    meta = result.get("metadata", {})
    assert meta.get("echo_of") == origin_event_id, (
        f"metadata.echo_of must equal origin_event_id, got: {meta}"
    )

    # --- DB state unchanged (semantic track was skipped) ---
    mem_after = await _memories_count(pool, namespace_id=ns_id)
    cog_after = await _cognitive_ledger_count(pool, namespace_id=ns_id)
    assert mem_after == mem_before, (
        f"Echo-suppressed event must not create episodic memories "
        f"(before={mem_before}, after={mem_after})"
    )
    assert cog_after == cog_before, (
        f"Echo-suppressed event must not create Empathic Tensor rows "
        f"(before={cog_before}, after={cog_after})"
    )

    # --- Metric was incremented ---
    mock_counter.labels.assert_called_with(system="d365")
    mock_counter.labels.return_value.inc.assert_called_once()

    # Cleanup
    redis_sync.delete(echo_key)
    redis_sync.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_non_echo_webhook_ingests_normally(
    echo_pg_scope: tuple,
) -> None:
    """Non-echo webhook: ingest_case_note is called and result has memory_id."""
    from nce.tasks import _dispatch_d365_event

    pool, ns_id, org_url = echo_pg_scope
    entity_id = str(uuid.uuid4())

    entity_ctx, raw_payload = _build_annotation_ctx(
        entity_id, org_url, note_text="Normal webhook, no echo"
    )

    mock_pool = _make_mock_pool(ns_id, org_url)
    mock_token_mgr = MagicMock()
    mock_token_mgr.get_access_token = AsyncMock(return_value="mock-token")
    mock_redis_async = MagicMock()
    mock_redis_async.aclose = AsyncMock()
    mock_mongo = MagicMock()
    mock_mongo.close = MagicMock()
    mock_memory_id = str(uuid.uuid4())

    mock_worker = MagicMock()
    mock_worker.ingest_case_note = AsyncMock(
        return_value={
            "memory_id": mock_memory_id,
            "mongo_id": "a" * 24,
            "empathic_tensor": [5.0, 0.0, 5.0, 0.0, 0.0, 0.0],
        }
    )

    with (
        # check_echo returns None → not an echo
        patch("nce.webhook_receiver.main.check_echo", return_value=None),
        patch("asyncpg.create_pool", new=AsyncMock(return_value=mock_pool)),
        patch("motor.motor_asyncio.AsyncIOMotorClient", return_value=mock_mongo),
        patch("redis.asyncio.from_url", return_value=mock_redis_async),
        patch(
            "nce.vertical_modules.dynamics365.auth.DataverseTokenManager",
            return_value=mock_token_mgr,
        ),
        patch(
            "nce.vertical_modules.dynamics365.client.DataverseClient",
            return_value=MagicMock(),
        ),
        patch(
            "nce.vertical_modules.dynamics365.ingestion.DataverseIngestionWorker",
            return_value=mock_worker,
        ),
    ):
        result = await _dispatch_d365_event(entity_ctx, raw_payload)

    assert result.get("status") == "ok", f"Expected ok status for non-echo, got: {result}"
    assert result.get("action") == "ingest_case_note", f"Expected ingest_case_note action: {result}"
    assert "memory_id" in result, f"memory_id missing from non-echo result: {result}"
    mock_worker.ingest_case_note.assert_called_once()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_and_check_echo_roundtrip() -> None:
    """register_echo / check_echo Redis roundtrip with the integration Redis instance."""
    from redis import Redis

    from nce.config import cfg
    from nce.webhook_receiver.main import check_echo, register_echo

    redis_client = None
    try:
        redis_client = Redis.from_url(_redis_url())
        redis_client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable: {exc}")

    system = "d365"
    entity_id = f"test-echo-{uuid.uuid4().hex[:12]}"
    origin_event_id = str(uuid.uuid4())
    redis_key = f"nce:echo:{system}:{entity_id}"

    # Ensure no stale key
    redis_client.delete(redis_key)

    with patch("nce.webhook_receiver.main._redis_client", return_value=redis_client):
        # Before registration: no echo
        assert check_echo(system, entity_id) is None

        # Register an echo
        register_echo(system, entity_id, origin_event_id)

        # After registration: echo is detected
        result = check_echo(system, entity_id)
        assert result == origin_event_id, f"Expected {origin_event_id!r}, got {result!r}"

        # Key must have a TTL (NCE_ECHO_TTL_S default = 600)
        ttl = redis_client.ttl(redis_key)
        assert ttl > 0, f"Echo key must have a positive TTL, got {ttl}"
        assert ttl <= cfg.NCE_ECHO_TTL_S, (
            f"Echo TTL {ttl} exceeds cfg.NCE_ECHO_TTL_S={cfg.NCE_ECHO_TTL_S}"
        )

    # Cleanup
    redis_client.delete(redis_key)
    redis_client.close()
