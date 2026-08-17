"""Integration tests for Sales Public Quote endpoint (Batch 088)."""

from __future__ import annotations

import datetime
import json
from typing import Any
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import httpx
import pytest

from nce import admin_state
from nce.admin_app import app
from nce.admin_handlers.sales_public import generate_public_token
from nce.auth import set_namespace_context
from nce.orchestrator import NCEEngine


async def _insert_sales_record(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    entity: str,
    source_id: str,
    name: str,
    source_json: dict[str, Any],
    manual: dict[str, Any] | None = None,
    is_deleted: bool = False,
) -> None:
    """Helper to insert sales read model records directly for testing."""
    if "name" not in source_json:
        source_json = dict(source_json)
        source_json["name"] = name
    manual_json = manual or {}
    await conn.execute(
        """
        INSERT INTO sales_read_model
            (namespace_id, entity, source_id, name, source_json, manual, is_deleted, modifiedon, synced_at)
        VALUES
            ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, now())
        ON CONFLICT (namespace_id, entity, source_id)
        DO UPDATE SET
            name = EXCLUDED.name,
            source_json = EXCLUDED.source_json,
            manual = EXCLUDED.manual,
            is_deleted = EXCLUDED.is_deleted,
            modifiedon = EXCLUDED.modifiedon,
            updated_at = now()
        """,
        str(namespace_id),
        entity,
        source_id,
        name,
        json.dumps(source_json),
        json.dumps(manual_json),
        is_deleted,
        datetime.datetime.now(datetime.timezone.utc),
    )


@pytest.fixture(autouse=True)
def bypass_lifespan(monkeypatch: pytest.MonkeyPatch):
    """Bypass Starlette app lifespan so we can manage NCEEngine manually in the test loop."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def dummy_lifespan(app):
        yield

    monkeypatch.setattr(app.router, "lifespan_context", dummy_lifespan)


@pytest.mark.integration
@pytest.mark.asyncio
class TestSalesPublicQuote:
    """Integration tests for public quote API endpoints."""

    async def test_public_quote_redacted_ok(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Querying with a valid token returns C8-redacted quote with no cost/margin leak."""
        ns = await make_namespace()
        quote_id = f"quote-{uuid4().hex[:8]}"

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await _insert_sales_record(
                    conn,
                    ns,
                    "quotes",
                    quote_id,
                    "Quote for AV system design",
                    {
                        "quoteid": quote_id,
                        "name": "Quote for AV system design",
                        "description": "Quote for AV system design",
                        "cost": 10000.0,
                        "margin": 0.45,
                        "commission": 500.0,
                        "internal-status": "approved",
                        "manufacturer": "Sony",
                        "model": "VPL-XW5000ES",
                        "quantity": 2,
                        "unit_price": 60000.0,
                        "currency": "NOK",
                        "lead_time_days": 10,
                    },
                )

        token = generate_public_token(quote_id)

        # Setup NCEEngine manually in this test's event loop
        engine = NCEEngine()
        await engine.connect()
        app.state.engine = engine
        admin_state.engine = engine

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.get(
                    f"/public-api/sales/quotes/{quote_id}?namespace_id={ns}&token={token}"
                )
                assert r.status_code == 200

                data = r.json()
                # Allowed fields must survive
                assert data["manufacturer"] == "Sony"
                assert data["model"] == "VPL-XW5000ES"
                assert data["quantity"] == 2
                assert data["unit_price"] == 60000.0
                assert data["currency"] == "NOK"
                assert data["lead_time_days"] == 10
                assert data["description"] == "Quote for AV system design"

                # Forbidden fields must be redacted
                assert "cost" not in data
                assert "margin" not in data
                assert "commission" not in data
                assert "internal-status" not in data
        finally:
            await engine.disconnect()

    async def test_public_quote_unauthorized(
        self,
        make_namespace: Any,
    ) -> None:
        """Invalid or missing tokens must return 401."""
        ns = await make_namespace()
        quote_id = f"quote-{uuid4().hex[:8]}"

        engine = NCEEngine()
        await engine.connect()
        app.state.engine = engine
        admin_state.engine = engine

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                # 1. Missing token
                r = await client.get(f"/public-api/sales/quotes/{quote_id}?namespace_id={ns}")
                assert r.status_code == 401
                assert "unauthorized" in r.json()["error"].lower()

                # 2. Invalid token
                r = await client.get(
                    f"/public-api/sales/quotes/{quote_id}?namespace_id={ns}&token=invalid-token"
                )
                assert r.status_code == 401
                assert "unauthorized" in r.json()["error"].lower()
        finally:
            await engine.disconnect()

    async def test_public_quote_rate_limiting(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Exceeding 5 requests per 10 seconds returns 429."""
        ns = await make_namespace()
        quote_id = f"quote-{uuid4().hex[:8]}"

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await _insert_sales_record(
                    conn,
                    ns,
                    "quotes",
                    quote_id,
                    "AV Design",
                    {"quoteid": quote_id, "name": "AV Design", "unit_price": 500.0},
                )

        token = generate_public_token(quote_id)

        engine = NCEEngine()
        await engine.connect()
        app.state.engine = engine
        admin_state.engine = engine

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                # 5 requests should succeed
                for _ in range(5):
                    r = await client.get(
                        f"/public-api/sales/quotes/{quote_id}?namespace_id={ns}&token={token}"
                    )
                    assert r.status_code == 200

                # 6th request should fail with 429
                r = await client.get(
                    f"/public-api/sales/quotes/{quote_id}?namespace_id={ns}&token={token}"
                )
                assert r.status_code == 429
                assert "rate limit exceeded" in r.json()["error"].lower()
        finally:
            await engine.disconnect()

    async def test_public_quote_not_found(
        self,
        make_namespace: Any,
    ) -> None:
        """Valid token signature for non-existent quote id returns 404."""
        ns = await make_namespace()
        quote_id = "quote-nonexistent"
        token = generate_public_token(quote_id)

        engine = NCEEngine()
        await engine.connect()
        app.state.engine = engine
        admin_state.engine = engine

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.get(
                    f"/public-api/sales/quotes/{quote_id}?namespace_id={ns}&token={token}"
                )
                assert r.status_code == 404
                assert "quote not found" in r.json()["error"].lower()
        finally:
            await engine.disconnect()
