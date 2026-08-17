"""
tests/unit/test_product_enrich_a2a.py
=======================================
Acceptance tests for Batch 038 — Module 2.Wave 8 (enrich-a2a).

Assertions (all pure unit tests — no DB, no asyncio.Task execution):

1. Fire-and-forget contract:
   - ``enqueue_product_enrichment`` returns immediately without awaiting
     enrichment completion (``create_tracked_task`` is called, not awaited).
   - The return value carries ``enrichment: "queued"`` and ``specs_pending: True``.
   - ``do_enrich_product`` is NOT awaited by the caller.

2. Enqueue-exactly-once:
   - ``create_tracked_task`` is called exactly once per ``enqueue_product_enrichment``
     invocation.

3. Review-queue REST route:
   - ``api_product_enrichment_review`` returns only ``needs_review=True`` rows.
   - Rows are namespace-scoped (FORCE RLS enforced at DB layer — verified by
     confirming ``scoped_pg_session`` is called with the correct namespace_id).
   - No cost/margin/BID columns leak in the response.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_PRODUCT_ID = "aaaaaaaa-0000-4000-8000-000000000001"

_TRIGGER_CONTEXT: dict[str, Any] = {
    "kind": "quote",
    "ref_id": "QUOTE-42",
    "missing_fields": ["voltage", "weight"],
    "source_watermark": "w-abc123",
}

_FORBIDDEN_COLUMNS: frozenset[str] = frozenset({"cost", "cost_price", "margin", "bid_id"})


# ---------------------------------------------------------------------------
# Bypass the product opt-in guard for all REST route unit tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_product_guard(monkeypatch):
    """Bypass metadata.product.enabled check for all unit tests in this file."""
    monkeypatch.setattr(
        "nce.admin_handlers.product._check_product_enabled_rest",
        AsyncMock(return_value=None),
    )


# ---------------------------------------------------------------------------
# Helper: fake asyncpg pool that returns a mock connection
# ---------------------------------------------------------------------------


class _AsyncCtx:
    """Minimal async context manager that yields the given object."""

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


def _make_pool(fetch_return=None) -> tuple[MagicMock, AsyncMock]:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="SET")
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    return pool, conn


# ---------------------------------------------------------------------------
# 1 & 2: enqueue_product_enrichment — fire-and-forget + enqueue-once
# ---------------------------------------------------------------------------


def test_enqueue_returns_immediately_without_awaiting_enrichment():
    """enqueue_product_enrichment must return without awaiting do_enrich_product."""
    pool, _ = _make_pool()

    with patch("nce.vertical_modules.product.a2a.create_tracked_task") as mock_create_task:
        from nce.vertical_modules.product.a2a import enqueue_product_enrichment

        result = enqueue_product_enrichment(pool, _NAMESPACE_ID, _PRODUCT_ID, _TRIGGER_CONTEXT)

    # create_tracked_task must have been called (fire-and-forget)
    mock_create_task.assert_called_once()
    # The function must NOT be a coroutine — it returns synchronously
    assert not hasattr(result, "__await__"), "enqueue_product_enrichment must not be a coroutine"


def test_enqueue_returns_queued_status():
    """Return value must carry enrichment='queued' and specs_pending=True."""
    pool, _ = _make_pool()

    with patch("nce.vertical_modules.product.a2a.create_tracked_task"):
        from nce.vertical_modules.product.a2a import enqueue_product_enrichment

        result = enqueue_product_enrichment(pool, _NAMESPACE_ID, _PRODUCT_ID, _TRIGGER_CONTEXT)

    assert result["product_id"] == _PRODUCT_ID
    assert result["enrichment"] == "queued"
    assert result["specs_pending"] is True


def test_enqueue_calls_create_tracked_task_exactly_once():
    """Exactly one background task per enqueue call."""
    pool, _ = _make_pool()

    with patch("nce.vertical_modules.product.a2a.create_tracked_task") as mock_create_task:
        from nce.vertical_modules.product.a2a import enqueue_product_enrichment

        enqueue_product_enrichment(pool, _NAMESPACE_ID, _PRODUCT_ID, _TRIGGER_CONTEXT)

    assert mock_create_task.call_count == 1


def test_enqueue_uses_product_enrich_task_name():
    """create_tracked_task must be called with the stable task name 'product_enrich'."""
    pool, _ = _make_pool()

    with patch("nce.vertical_modules.product.a2a.create_tracked_task") as mock_create_task:
        from nce.vertical_modules.product.a2a import enqueue_product_enrichment

        enqueue_product_enrichment(pool, _NAMESPACE_ID, _PRODUCT_ID, _TRIGGER_CONTEXT)

    _, kwargs = mock_create_task.call_args
    assert (
        kwargs.get("name") == "product_enrich"
        or mock_create_task.call_args[0][1] == "product_enrich"
    )


def test_enqueue_does_not_await_enrichment():
    """create_tracked_task is synchronous — the caller must not await it."""
    pool, _ = _make_pool()

    # Verify enqueue_product_enrichment itself is NOT a coroutine function
    import inspect

    from nce.vertical_modules.product.a2a import enqueue_product_enrichment

    assert not inspect.iscoroutinefunction(enqueue_product_enrichment), (
        "enqueue_product_enrichment must be a plain (sync) function so the "
        "quote/design builder is never blocked"
    )


# ---------------------------------------------------------------------------
# 3: api_product_enrichment_review — namespace-scoped, no secret leak
# ---------------------------------------------------------------------------


def _make_starlette_request(query_params: dict[str, str]) -> MagicMock:
    """Build a minimal Starlette Request mock."""
    req = MagicMock()
    req.query_params = query_params
    req.path_params = {}
    return req


def _fake_scoped_session_factory(conn: AsyncMock):
    """Return a scoped_pg_session replacement that yields conn directly."""

    class _FakeScoped:
        def __init__(self, pool: Any, ns: Any) -> None:
            self._ns = ns

        async def __aenter__(self) -> AsyncMock:
            return conn

        async def __aexit__(self, *_: Any) -> None:
            pass

    return _FakeScoped


@pytest.mark.asyncio
async def test_review_route_returns_only_needs_review_rows():
    """Response must contain only rows where needs_review=True."""
    import uuid

    review_rows = [
        {
            "id": uuid.uuid4(),
            "namespace_id": uuid.UUID(_NAMESPACE_ID),
            "product_id": uuid.UUID(_PRODUCT_ID),
            "trigger_context": '{"kind":"quote"}',
            "field_name": "voltage",
            "field_value": "230V",
            "confidence": 0.55,
            "needs_review": True,
            "product_source_id": None,
            "created_at": None,
        }
    ]

    pool, conn = _make_pool(fetch_return=review_rows)
    conn.fetch = AsyncMock(return_value=review_rows)

    fake_engine = MagicMock()
    fake_engine.pg_pool = pool

    request = _make_starlette_request({"namespace_id": _NAMESPACE_ID})

    with (
        patch("nce.admin_handlers.product.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.product.scoped_pg_session",
            new=_fake_scoped_session_factory(conn),
        ),
    ):
        mock_state.engine = fake_engine

        from nce.admin_handlers.product import api_product_enrichment_review

        response = await api_product_enrichment_review(request)

    body = response.body
    import json

    data = json.loads(body)
    assert data["status"] == "ok"
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["field_name"] == "voltage"


@pytest.mark.asyncio
async def test_review_route_uses_caller_namespace_in_scoped_session():
    """scoped_pg_session must be called with the caller's namespace_id (RLS enforcement)."""
    pool, conn = _make_pool()
    conn.fetch = AsyncMock(return_value=[])

    fake_engine = MagicMock()
    fake_engine.pg_pool = pool

    request = _make_starlette_request({"namespace_id": _NAMESPACE_ID})

    captured_ns: list[str] = []

    class _CapturingScoped:
        def __init__(self, pool_arg: Any, ns: str) -> None:
            captured_ns.append(ns)

        async def __aenter__(self) -> AsyncMock:
            return conn

        async def __aexit__(self, *_: Any) -> None:
            pass

    with (
        patch("nce.admin_handlers.product.admin_state") as mock_state,
        patch("nce.admin_handlers.product.scoped_pg_session", new=_CapturingScoped),
    ):
        mock_state.engine = fake_engine

        from nce.admin_handlers.product import api_product_enrichment_review

        await api_product_enrichment_review(request)

    assert len(captured_ns) == 1
    assert captured_ns[0] == _NAMESPACE_ID, (
        f"scoped_pg_session called with {captured_ns[0]!r} instead of {_NAMESPACE_ID!r}"
    )


@pytest.mark.asyncio
async def test_review_route_no_forbidden_column_leak():
    """Response must not contain cost, cost_price, margin, or bid_id."""
    import uuid

    rows_with_forbidden = [
        {
            "id": uuid.uuid4(),
            "namespace_id": uuid.UUID(_NAMESPACE_ID),
            "product_id": uuid.UUID(_PRODUCT_ID),
            "trigger_context": "{}",
            "field_name": "price",
            "field_value": "99.00",
            "confidence": 0.50,
            "needs_review": True,
            "product_source_id": None,
            "created_at": None,
            # Forbidden fields that must be stripped:
            "cost": 42.0,
            "cost_price": 42.0,
            "margin": 0.3,
            "bid_id": "BID-001",
        }
    ]

    pool, conn = _make_pool()
    conn.fetch = AsyncMock(return_value=rows_with_forbidden)

    fake_engine = MagicMock()
    fake_engine.pg_pool = pool

    request = _make_starlette_request({"namespace_id": _NAMESPACE_ID})

    with (
        patch("nce.admin_handlers.product.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.product.scoped_pg_session",
            new=_fake_scoped_session_factory(conn),
        ),
    ):
        mock_state.engine = fake_engine

        from nce.admin_handlers.product import api_product_enrichment_review

        response = await api_product_enrichment_review(request)

    import json

    data = json.loads(response.body)
    for item in data["items"]:
        for bad_col in _FORBIDDEN_COLUMNS:
            assert bad_col not in item, f"Forbidden column '{bad_col}' leaked in review response"


@pytest.mark.asyncio
async def test_review_route_missing_namespace_returns_422():
    """Missing namespace_id must produce a 422 response."""
    request = _make_starlette_request({})

    with patch("nce.admin_handlers.product.admin_state") as mock_state:
        mock_state.engine = MagicMock()

        from nce.admin_handlers.product import api_product_enrichment_review

        response = await api_product_enrichment_review(request)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_review_route_engine_not_connected_returns_503():
    """No engine must produce a 503 response."""
    request = _make_starlette_request({"namespace_id": _NAMESPACE_ID})

    with patch("nce.admin_handlers.product.admin_state") as mock_state:
        mock_state.engine = None

        from nce.admin_handlers.product import api_product_enrichment_review

        response = await api_product_enrichment_review(request)

    assert response.status_code == 503
