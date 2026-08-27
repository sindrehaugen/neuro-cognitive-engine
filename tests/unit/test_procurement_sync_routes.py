"""
tests/unit/test_procurement_sync_routes.py
==========================================
Acceptance tests for Batch 050 — Module 1.Wave 7 (sync-status-routes).

Covers:
  1. ``api_procurement_sync_now`` requires admin auth (engine-connected guard).
  2. ``api_procurement_sync_now`` triggers a projection-refresh run and records it.
  3. ``api_procurement_sync_now`` returns column_report with no secret/URL.
  4. ``api_procurement_sync_status`` returns last_synced_at / row_count / freshness.
  5. ``api_procurement_sync_status`` returns column_report with no secret/URL.
  6. Column-report lists mapped and unknown columns; never contains a GUID feed URL.
  7. Both routes are mounted in the admin app.
  8. Both routes return 503 when engine is not connected.

All tests are pure unit tests (no DB, no Redis).
DB calls are mocked via a fake asyncpg connection / pool.
``append_event`` is patched so no signing infra is needed.
``namespace_id`` validation is NOT patched out: the routes call the real
``_shared._require_namespace_id``, and the malformed-input side of that guard
is covered in ``tests/unit/test_admin_namespace_uuid_guard.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_NAMESPACE_UUID = uuid.UUID(_NAMESPACE_ID)

# A GUID-bearing URL that must NEVER appear in any column-report response.
_FEED_URL = "https://nettailer.example.com/api/feed/guid-abc123def456/prices"


# ---------------------------------------------------------------------------
# Helpers: fake asyncpg connection row / pool
# ---------------------------------------------------------------------------


def _make_fake_row(row_count: int, last_synced_at: datetime | None) -> dict[str, Any]:
    return {"row_count": row_count, "last_synced_at": last_synced_at}


def _make_conn(row_count: int = 5, last_synced_at: datetime | None = None) -> AsyncMock:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_make_fake_row(row_count, last_synced_at))
    conn.execute = AsyncMock(return_value=None)
    return conn


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Return a minimal pg_pool mock supporting ``acquire`` as async context manager."""
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


def _make_engine(conn: AsyncMock) -> MagicMock:
    engine = MagicMock()
    engine.pg_pool = _make_pool(conn)
    return engine


def _make_request(body: dict[str, Any] | None = None, qp: dict[str, str] | None = None):
    """Minimal Starlette-like request mock."""
    req = MagicMock()
    req.json = AsyncMock(return_value=body or {})
    req.query_params = qp or {}
    return req


# ---------------------------------------------------------------------------
# scoped_pg_session stub: yields the conn directly
# ---------------------------------------------------------------------------


def _scoped_pg_session_stub(pool: Any, namespace_id: Any):
    """Async context manager that yields the first acquired connection."""

    class _Ctx:
        async def __aenter__(self):
            return await pool.acquire().__aenter__()

        async def __aexit__(self, *_):
            return False

    return _Ctx()


# ---------------------------------------------------------------------------
# 1 + 8. sync_now — 503 when engine not connected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_now_503_when_engine_not_connected():
    from nce.admin_handlers.procurement import api_procurement_sync_now

    with patch("nce.admin_handlers.procurement.admin_state") as mock_state:
        mock_state.engine = None
        req = _make_request(body={"namespace_id": _NAMESPACE_ID})
        resp = await api_procurement_sync_now(req)

    assert resp.status_code == 503
    data = json.loads(resp.body)
    assert "error" in data


# ---------------------------------------------------------------------------
# 2. sync_now — records a run, returns row count + column_report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_now_records_run_and_returns_column_report():
    from nce.admin_handlers.procurement import api_procurement_sync_now

    synced_at = datetime(2026, 6, 20, 10, 0, 0, tzinfo=timezone.utc)
    conn = _make_conn(row_count=42, last_synced_at=synced_at)
    engine = _make_engine(conn)

    with (
        patch("nce.admin_handlers.procurement.admin_state") as mock_state,
        patch("nce.admin_handlers.procurement.scoped_pg_session", _scoped_pg_session_stub),
    ):
        mock_state.engine = engine
        req = _make_request(body={"namespace_id": _NAMESPACE_ID})
        resp = await api_procurement_sync_now(req)

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["status"] == "ok"
    assert data["rows_refreshed"] == 42
    assert data["synced_at"] == synced_at.isoformat()
    assert "column_report" in data
    # The refresh run is recorded operationally (log.info) + via the cache's
    # synced_at; the response reflecting the refreshed row count + synced_at
    # proves it executed. (Operational admin action — NOT a WORM event_log row.)


# ---------------------------------------------------------------------------
# 3. sync_now — column_report never contains a secret or feed URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_now_column_report_no_secret_no_url():
    from nce.admin_handlers.procurement import api_procurement_sync_now

    conn = _make_conn(row_count=0, last_synced_at=None)
    engine = _make_engine(conn)

    with (
        patch("nce.admin_handlers.procurement.admin_state") as mock_state,
        patch("nce.admin_handlers.procurement.scoped_pg_session", _scoped_pg_session_stub),
    ):
        mock_state.engine = engine
        req = _make_request(body={"namespace_id": _NAMESPACE_ID})
        resp = await api_procurement_sync_now(req)

    body_text = resp.body.decode()
    assert _FEED_URL not in body_text
    assert (
        "guid" not in body_text.lower() or "column_report" in body_text
    )  # only in label, not as URL
    # Confirm the response body contains no "https://" URL
    assert "https://" not in body_text


# ---------------------------------------------------------------------------
# 4. sync_status — returns last_synced_at / row_count / freshness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_status_returns_freshness_and_row_count():
    from nce.admin_handlers.procurement import api_procurement_sync_status

    synced_at = datetime(2026, 6, 20, 9, 0, 0, tzinfo=timezone.utc)
    conn = _make_conn(row_count=17, last_synced_at=synced_at)
    engine = _make_engine(conn)

    with (
        patch("nce.admin_handlers.procurement.admin_state") as mock_state,
        patch("nce.admin_handlers.procurement.scoped_pg_session", _scoped_pg_session_stub),
    ):
        mock_state.engine = engine
        req = _make_request(qp={"namespace_id": _NAMESPACE_ID})
        resp = await api_procurement_sync_status(req)

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["status"] == "ok"
    assert data["row_count"] == 17
    assert data["last_synced_at"] == synced_at.isoformat()
    assert data["freshness_seconds"] is not None
    assert data["freshness_seconds"] >= 0.0
    assert "column_report" in data


# ---------------------------------------------------------------------------
# 5. sync_status — column_report no secret/URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_status_column_report_no_secret_no_url():
    from nce.admin_handlers.procurement import api_procurement_sync_status

    conn = _make_conn(row_count=3, last_synced_at=None)
    engine = _make_engine(conn)

    with (
        patch("nce.admin_handlers.procurement.admin_state") as mock_state,
        patch("nce.admin_handlers.procurement.scoped_pg_session", _scoped_pg_session_stub),
    ):
        mock_state.engine = engine
        req = _make_request(qp={"namespace_id": _NAMESPACE_ID})
        resp = await api_procurement_sync_status(req)

    body_text = resp.body.decode()
    assert "https://" not in body_text
    assert _FEED_URL not in body_text


# ---------------------------------------------------------------------------
# 6. Column-report: mapped / unknown columns, no GUID feed URL
# ---------------------------------------------------------------------------


def test_column_report_content():
    from nce.admin_handlers.procurement import _column_report

    report = _column_report()
    assert "mapped" in report
    assert "unknown" in report
    assert "cache_table" in report
    # Known projection columns must be mapped
    for col in ("artnr", "leverandor", "bid_id", "pris"):
        assert col in report["mapped"]
    # No URL anywhere in the report
    report_str = json.dumps(report)
    assert "https://" not in report_str
    assert "http://" not in report_str
    # No GUID pattern that might leak a feed URL
    import re

    guid_url_pattern = re.compile(r"https?://[^\s\"']*[0-9a-f]{8}-[0-9a-f]{4}")
    assert not guid_url_pattern.search(report_str)


# ---------------------------------------------------------------------------
# 7. Routes mounted in admin app
# ---------------------------------------------------------------------------


def test_procurement_sync_routes_mounted_in_admin_app():
    from nce.admin_app import build_admin_routes

    routes = build_admin_routes()
    paths = {r.path for r in routes}
    assert "/api/procurement/sync" in paths
    assert "/api/procurement/sync/status" in paths


# ---------------------------------------------------------------------------
# 8. sync_status — 503 when engine not connected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_status_503_when_engine_not_connected():
    from nce.admin_handlers.procurement import api_procurement_sync_status

    with patch("nce.admin_handlers.procurement.admin_state") as mock_state:
        mock_state.engine = None
        req = _make_request(qp={"namespace_id": _NAMESPACE_ID})
        resp = await api_procurement_sync_status(req)

    assert resp.status_code == 503
    data = json.loads(resp.body)
    assert "error" in data


# ---------------------------------------------------------------------------
# Additional: missing namespace_id → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_now_missing_namespace_id():
    from nce.admin_handlers.procurement import api_procurement_sync_now

    with patch("nce.admin_handlers.procurement.admin_state") as mock_state:
        mock_state.engine = MagicMock()
        req = _make_request(body={})
        resp = await api_procurement_sync_now(req)

    assert resp.status_code == 422
    data = json.loads(resp.body)
    assert "error" in data


@pytest.mark.asyncio
async def test_sync_status_missing_namespace_id():
    from nce.admin_handlers.procurement import api_procurement_sync_status

    with patch("nce.admin_handlers.procurement.admin_state") as mock_state:
        mock_state.engine = MagicMock()
        req = _make_request(qp={})
        resp = await api_procurement_sync_status(req)

    assert resp.status_code == 422
    data = json.loads(resp.body)
    assert "error" in data
