"""
tests/test_generic_source_mode_endpoint.py
=============================================
Live-Postgres verification of the generic C5 source-mode admin REST surface
(Wave D-9 completion, 2026-09-20): GET/PUT /api/admin/source-mode.

PR #310 generalized the Python mechanism (nce.source_mode.flip) but left
the charter's other stated D-9 deliverable -- "GET /api/admin/source-mode
per function" -- unbuilt; only a sales-specific route
(/api/admin/sales/source-mode) existed, and its PUT handler independently
re-implemented the flip_blocked-then-upsert logic instead of calling
nce.source_mode.flip.flip_function. This file proves the new generic route
works end to end for an engine that has never had its own hand-written
route (same "prove it generalizes, don't just re-test the origin engine"
discipline as tests/test_source_mode_flip.py), and that it actually
delegates to the shared flip_function rather than reimplementing its gate.
tests/test_sales_divergence.py::test_admin_sales_source_mode_endpoints
covers the pre-existing sales-specific route unchanged, proving the
refactor into a thin wrapper preserved its exact behavior.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import time
from typing import Any
from unittest.mock import patch
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import httpx
import pytest

from nce.admin_app import app
from nce.auth import set_namespace_context
from nce.config import cfg
from nce.orchestrator import NCEEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _make_signature(key: str, method: str, path: str, timestamp: int, body: bytes = b"") -> str:
    parts = [method.upper(), path, str(timestamp)]
    if body:
        parts.append(hashlib.sha256(body).hexdigest())
    canonical = "\n".join(parts)
    return _hmac.new(key.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _valid_headers(key: str, method: str, path: str, body: bytes = b"") -> dict[str, str]:
    ts = int(time.time())
    sig = _make_signature(key, method, path, ts, body)
    return {
        "X-NCE-Timestamp": str(ts),
        "Authorization": f"HMAC-SHA256 {sig}",
    }


def _make_engine_stub(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    return eng


async def test_generic_source_mode_get_and_put_for_an_arbitrary_engine(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """Same lifecycle as test_admin_sales_source_mode_endpoints, but for
    "widgets_sync" -- an engine that has never had its own hand-written
    source-mode route -- proving the REST surface itself is generic, not
    just that sales's copy of it still works.
    """
    ns_id: UUID = await make_namespace()
    engine = _make_engine_stub(pg_pool)
    key = cfg.NCE_API_KEY or "test-key"

    with (
        patch("nce.admin_state.engine", engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # 1. GET without Auth -> 401
            r = await client.get(f"/api/admin/source-mode?namespace_id={ns_id}&engine=widgets_sync")
            assert r.status_code == 401

            # 2. GET with Auth -> 200, empty modes initially
            headers = _valid_headers(key, "GET", "/api/admin/source-mode")
            r = await client.get(
                f"/api/admin/source-mode?namespace_id={ns_id}&engine=widgets_sync",
                headers=headers,
            )
            assert r.status_code == 200
            body = r.json()
            assert body["engine"] == "widgets_sync"
            assert body["modes"] == {}

            # 3. PUT with Auth -> update to 'both'
            put_body = {
                "namespace_id": str(ns_id),
                "engine": "widgets_sync",
                "function": "read_widgets",
                "mode": "both",
            }
            body_bytes = json.dumps(put_body).encode("utf-8")
            headers = _valid_headers(key, "PUT", "/api/admin/source-mode", body_bytes)
            r = await client.put("/api/admin/source-mode", content=body_bytes, headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "updated"
            assert r.json()["mode"] == "both"

            async with pg_pool.acquire() as conn:
                await set_namespace_context(conn, ns_id)
                mode = await conn.fetchval(
                    "SELECT mode FROM source_mode_config "
                    "WHERE namespace_id = $1 AND engine = 'widgets_sync' AND function = 'read_widgets'",
                    ns_id,
                )
            assert mode == "both"

            # 4. PUT to 'nce' is blocked by a recent divergence
            async with pg_pool.acquire() as conn:
                await set_namespace_context(conn, ns_id)
                await conn.execute(
                    """
                    INSERT INTO divergence_log (namespace_id, engine, entity, field, nce_value, ext_value, materiality, detected_at)
                    VALUES ($1, 'widgets_sync', 'widget:div-block', 'sku', 'W-1', 'W-1-OLD', 1.0, now())
                    """,
                    ns_id,
                )
            put_body["mode"] = "nce"
            body_bytes = json.dumps(put_body).encode("utf-8")
            headers = _valid_headers(key, "PUT", "/api/admin/source-mode", body_bytes)
            r = await client.put("/api/admin/source-mode", content=body_bytes, headers=headers)
            assert r.status_code == 400
            assert "blocked" in r.json()["error"].lower()

            # 5. PUT to 'nce' succeeds once the divergence ages out of a narrow window
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM divergence_log WHERE namespace_id = $1 AND engine = 'widgets_sync'",
                    ns_id,
                )
            body_bytes = json.dumps(put_body).encode("utf-8")
            headers = _valid_headers(key, "PUT", "/api/admin/source-mode", body_bytes)
            r = await client.put("/api/admin/source-mode", content=body_bytes, headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["mode"] == "nce"

            async with pg_pool.acquire() as conn:
                await set_namespace_context(conn, ns_id)
                mode = await conn.fetchval(
                    "SELECT mode FROM source_mode_config "
                    "WHERE namespace_id = $1 AND engine = 'widgets_sync' AND function = 'read_widgets'",
                    ns_id,
                )
            assert mode == "nce"


async def test_generic_put_route_uses_the_shared_flip_function(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """The actual point of this wave: a mode="nce" PUT must go through the
    one canonical nce.source_mode.flip.flip_function, not a second,
    independent reimplementation of its gate -- which is exactly what
    admin_handlers/sales.py's PUT handler used to do inline before this
    wave. Spies on the real function (still calls through) to prove it is
    actually invoked, not bypassed.
    """
    ns_id: UUID = await make_namespace()
    engine = _make_engine_stub(pg_pool)
    key = cfg.NCE_API_KEY or "test-key"

    import nce.admin_handlers.source_mode as source_mode_handlers

    calls: list[dict[str, Any]] = []
    real_flip_function = source_mode_handlers.flip_function

    async def _spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return await real_flip_function(*args, **kwargs)

    with (
        patch("nce.admin_state.engine", engine),
        patch("nce.config.cfg.NCE_ADMIN_MTLS_ENABLED", False),
        patch("nce.admin_handlers.source_mode.flip_function", side_effect=_spy),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            put_body = {
                "namespace_id": str(ns_id),
                "engine": "widgets_sync",
                "function": "read_widgets",
                "mode": "nce",
            }
            body_bytes = json.dumps(put_body).encode("utf-8")
            headers = _valid_headers(key, "PUT", "/api/admin/source-mode", body_bytes)
            r = await client.put("/api/admin/source-mode", content=body_bytes, headers=headers)
            assert r.status_code == 200, r.text

    assert len(calls) == 1
    assert calls[0]["engine"] == "widgets_sync"
    assert calls[0]["function"] == "read_widgets"
