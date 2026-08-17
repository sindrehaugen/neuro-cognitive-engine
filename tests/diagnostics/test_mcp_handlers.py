"""Pure-unit tests for the diagnostics MCP handlers (Batch 76).

All external I/O is mocked:
* engine  — ``MagicMock`` carrying ``pg_pool``/``minio_client``/``redis_sync_client``.
* MinIO   — ``ensure_landing_bucket`` / ``generate_secure_presigned_url`` patched.
* RQ      — ``get_diag_queue`` + ``enqueue_traced`` patched.
* Postgres — ``scoped_pg_session`` patched with a fake async context manager whose
  yielded connection is an ``AsyncMock`` (``fetch`` / ``execute``).

Each handler is asserted to:
  (a) return a JSON **string** on the happy path, and
  (b) return a clean error JSON (no raised exception) when
      ``cfg.NCE_DIAG_ENABLED`` is false.

No Docker / Redis / MinIO required.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.vertical_modules.diagnostics import mcp_handlers as h

NAMESPACE = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_engine(
    fetch_return: list[Any] | None = None,
    fetchrow_return: Any | None = None,
) -> MagicMock:
    """A mocked NCEEngine exposing the attributes the handlers touch."""
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    engine.minio_client = MagicMock()
    engine.redis_sync_client = MagicMock()

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    engine._test_conn = conn  # convenience handle for assertions
    return engine


def _patch_scoped_session(engine: MagicMock):
    """Patch ``scoped_pg_session`` to yield the engine's mocked connection."""

    @asynccontextmanager
    async def _fake_scoped(pool, namespace_id):  # noqa: ANN001
        yield engine._test_conn

    return patch("nce.db_utils.scoped_pg_session", _fake_scoped)


def _enabled():
    """Patch the feature flag ON."""
    return patch.object(h, "_diag_enabled", return_value=True)


def _disabled():
    """Patch the feature flag OFF."""
    return patch.object(h, "_diag_enabled", return_value=False)


def _assert_json_str(result: Any) -> dict[str, Any]:
    assert isinstance(result, str)
    payload = json.loads(result)
    assert isinstance(payload, dict)
    return payload


# ---------------------------------------------------------------------------
# handle_diag_ingest_bundle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_bundle_happy_path_returns_json() -> None:
    engine = _make_engine()
    args = {
        "namespace_id": NAMESPACE,
        "vendor_profile": "mtr",
        "device_slug": "room-a-bar",
        "object_name": "bundle.zip",
    }
    with (
        _enabled(),
        _patch_scoped_session(engine),
        patch(
            "nce.storage.generate_secure_presigned_url",
            return_value="https://minio.local/presigned-put",
        ) as mock_url,
        patch.object(h, "_ensure_landing_bucket") as mock_bucket,
    ):
        result = await h.handle_diag_ingest_bundle(engine, args)

    payload = _assert_json_str(result)
    assert "error" not in payload
    assert payload["status"] == "PENDING"
    assert payload["upload_url"] == "https://minio.local/presigned-put"
    assert "ingest_id" in payload and len(payload["ingest_id"]) == 64  # sha256 hex
    # tenant-prefixed landing path
    assert payload["landing_uri"].lower().startswith("s3://")
    assert f"/{NAMESPACE.lower()}/diag/" in payload["landing_uri"]
    mock_bucket.assert_called_once()
    # presigned URL minted as a PUT
    assert mock_url.call_args.args[4] == "PUT"
    # PENDING row registered
    engine._test_conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_ingest_bundle_disabled_returns_clean_error() -> None:
    engine = _make_engine()
    with _disabled():
        result = await h.handle_diag_ingest_bundle(engine, {"namespace_id": NAMESPACE})
    payload = _assert_json_str(result)
    assert "error" in payload
    # no DB / storage work attempted
    engine._test_conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_bundle_rejects_unknown_vendor() -> None:
    engine = _make_engine()
    args = {
        "namespace_id": NAMESPACE,
        "vendor_profile": "definitely-not-a-vendor",
        "device_slug": "room-a",
        "object_name": "bundle.zip",
    }
    with _enabled(), _patch_scoped_session(engine), patch.object(h, "_ensure_landing_bucket"):
        result = await h.handle_diag_ingest_bundle(engine, args)
    payload = _assert_json_str(result)
    assert "error" in payload


@pytest.mark.asyncio
async def test_ingest_bundle_deterministic_ingest_id() -> None:
    """Same landing_uri + etag → identical ingest_id."""
    args = {
        "namespace_id": NAMESPACE,
        "vendor_profile": "mtr",
        "device_slug": "room-a-bar",
        "object_name": "bundle.zip",
        "etag": "fixed-etag-123",
    }
    results = []
    for _ in range(2):
        engine = _make_engine()
        with (
            _enabled(),
            _patch_scoped_session(engine),
            patch("nce.storage.generate_secure_presigned_url", return_value="https://x/put"),
            patch.object(h, "_ensure_landing_bucket"),
        ):
            results.append(_assert_json_str(await h.handle_diag_ingest_bundle(engine, args)))
    assert results[0]["ingest_id"] == results[1]["ingest_id"]


# ---------------------------------------------------------------------------
# handle_diag_commit_bundle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_bundle_enqueues_on_diag_lane() -> None:
    engine = _make_engine(
        fetchrow_return={
            "landing_uri": "s3://diag-landing/ns/diag/room-a/bundle.zip",
            "vendor_profile": "mtr",
            "device_slug": "room-a",
            "status": "PENDING",
        }
    )
    fake_queue = MagicMock()
    fake_job = MagicMock()
    fake_job.id = "job-abc"
    args = {"namespace_id": NAMESPACE, "ingest_id": "deadbeef"}

    with (
        _enabled(),
        _patch_scoped_session(engine),
        patch("nce.extractors.dispatch.get_diag_queue", return_value=fake_queue) as mock_q,
        patch("nce.observability.enqueue_traced", return_value=fake_job) as mock_enq,
    ):
        result = await h.handle_diag_commit_bundle(engine, args)

    payload = _assert_json_str(result)
    assert "error" not in payload
    assert payload["lane"] == "diag_ingest"
    assert payload["job_id"] == "job-abc"
    mock_q.assert_called_once_with(engine.redis_sync_client)
    # landing_uri/vendor_profile/device_slug fetched from the diag_ingestions row
    engine._test_conn.fetchrow.assert_awaited_once()
    # enqueued by the real Batch-75 task path with a job_timeout in seconds
    enq_args, enq_kwargs = mock_enq.call_args
    assert enq_args[0] is fake_queue
    assert enq_args[1] == h._PROCESS_DIAG_BUNDLE_TASK == "nce.tasks.process_diag_bundle"
    assert isinstance(enq_kwargs["job_timeout"], int) and enq_kwargs["job_timeout"] > 0
    # kwargs must match process_diag_bundle's signature exactly
    task_kwargs = enq_kwargs["kwargs"]
    assert set(task_kwargs) == {
        "ingest_id",
        "namespace_id",
        "landing_uri",
        "vendor_profile",
        "device_slug",
    }
    assert task_kwargs["ingest_id"] == "deadbeef"
    assert task_kwargs["namespace_id"] == NAMESPACE
    assert task_kwargs["landing_uri"] == "s3://diag-landing/ns/diag/room-a/bundle.zip"
    assert task_kwargs["vendor_profile"] == "mtr"
    assert task_kwargs["device_slug"] == "room-a"


@pytest.mark.asyncio
async def test_commit_bundle_unknown_ingest_id_returns_error() -> None:
    engine = _make_engine(fetchrow_return=None)
    args = {"namespace_id": NAMESPACE, "ingest_id": "does-not-exist"}

    with (
        _enabled(),
        _patch_scoped_session(engine),
        patch("nce.extractors.dispatch.get_diag_queue") as mock_q,
        patch("nce.observability.enqueue_traced") as mock_enq,
    ):
        result = await h.handle_diag_commit_bundle(engine, args)

    payload = _assert_json_str(result)
    assert "error" in payload
    assert "does-not-exist" in payload["error"]
    # no enqueue attempted for an unknown ingest_id
    mock_q.assert_not_called()
    mock_enq.assert_not_called()


@pytest.mark.asyncio
async def test_commit_bundle_already_digested_skips_enqueue() -> None:
    engine = _make_engine(
        fetchrow_return={
            "landing_uri": "s3://diag-landing/ns/diag/room-a/bundle.zip",
            "vendor_profile": "mtr",
            "device_slug": "room-a",
            "status": "DIGESTED",
        }
    )
    args = {"namespace_id": NAMESPACE, "ingest_id": "deadbeef"}

    with (
        _enabled(),
        _patch_scoped_session(engine),
        patch("nce.extractors.dispatch.get_diag_queue") as mock_q,
        patch("nce.observability.enqueue_traced") as mock_enq,
    ):
        result = await h.handle_diag_commit_bundle(engine, args)

    payload = _assert_json_str(result)
    assert "error" not in payload
    assert payload["status"] == "DIGESTED"
    mock_q.assert_not_called()
    mock_enq.assert_not_called()


@pytest.mark.asyncio
async def test_commit_bundle_disabled_returns_clean_error() -> None:
    engine = _make_engine()
    with _disabled():
        result = await h.handle_diag_commit_bundle(
            engine, {"namespace_id": NAMESPACE, "ingest_id": "x"}
        )
    payload = _assert_json_str(result)
    assert "error" in payload


@pytest.mark.asyncio
async def test_commit_bundle_requires_ingest_id() -> None:
    engine = _make_engine()
    with _enabled():
        result = await h.handle_diag_commit_bundle(engine, {"namespace_id": NAMESPACE})
    payload = _assert_json_str(result)
    assert "error" in payload


# ---------------------------------------------------------------------------
# handle_diag_digest_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_status_happy_path() -> None:
    engine = _make_engine(fetch_return=[{"ingest_id": "abc", "status": "DIGESTED"}])
    with _enabled(), _patch_scoped_session(engine):
        result = await h.handle_diag_digest_status(
            engine, {"namespace_id": NAMESPACE, "ingest_id": "abc"}
        )
    payload = _assert_json_str(result)
    assert "error" not in payload
    assert payload["count"] == 1
    engine._test_conn.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_digest_status_disabled_returns_clean_error() -> None:
    engine = _make_engine()
    with _disabled():
        result = await h.handle_diag_digest_status(engine, {"namespace_id": NAMESPACE})
    payload = _assert_json_str(result)
    assert "error" in payload
    engine._test_conn.fetch.assert_not_awaited()


# ---------------------------------------------------------------------------
# handle_diag_device_health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_health_happy_path() -> None:
    engine = _make_engine(fetch_return=[{"device_slug": "room-a", "health_state": "HEALTHY"}])
    with _enabled(), _patch_scoped_session(engine):
        result = await h.handle_diag_device_health(
            engine, {"namespace_id": NAMESPACE, "device_slug": "room-a"}
        )
    payload = _assert_json_str(result)
    assert "error" not in payload
    assert payload["count"] == 1
    engine._test_conn.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_device_health_disabled_returns_clean_error() -> None:
    engine = _make_engine()
    with _disabled():
        result = await h.handle_diag_device_health(engine, {"namespace_id": NAMESPACE})
    payload = _assert_json_str(result)
    assert "error" in payload
    engine._test_conn.fetch.assert_not_awaited()


# ---------------------------------------------------------------------------
# handle_diag_list_anomalies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_anomalies_happy_path() -> None:
    engine = _make_engine(fetch_return=[{"anomaly_type": "ptp_desync", "severity": 3}])
    with _enabled(), _patch_scoped_session(engine):
        result = await h.handle_diag_list_anomalies(
            engine, {"namespace_id": NAMESPACE, "ingest_id": "abc", "device_slug": "room-a"}
        )
    payload = _assert_json_str(result)
    assert "error" not in payload
    assert payload["count"] == 1
    engine._test_conn.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_anomalies_disabled_returns_clean_error() -> None:
    engine = _make_engine()
    with _disabled():
        result = await h.handle_diag_list_anomalies(engine, {"namespace_id": NAMESPACE})
    payload = _assert_json_str(result)
    assert "error" in payload
    engine._test_conn.fetch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Cross-cutting: missing namespace_id is a clean error on every read handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [
        h.handle_diag_digest_status,
        h.handle_diag_device_health,
        h.handle_diag_list_anomalies,
    ],
)
async def test_read_handlers_require_namespace(handler) -> None:  # noqa: ANN001
    engine = _make_engine()
    with _enabled():
        result = await handler(engine, {})
    payload = _assert_json_str(result)
    assert "error" in payload
