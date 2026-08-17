"""
Integration tests for Sales source-mode routing and divergence checks (Batch 083).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac as _hmac
import json
import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import httpx
import pytest

from nce.admin_app import app
from nce.auth import set_namespace_context
from nce.config import cfg
from nce.vertical_modules.sales.source_mode import (
    do_list_customers,
    do_sales_overview,
)

# ---------------------------------------------------------------------------
# Fixtures & Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def bypass_lifespan():
    """Bypass Starlette app lifespan to avoid real DB connections at startup."""

    @asynccontextmanager
    async def dummy_lifespan(app):
        yield

    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = dummy_lifespan
    yield
    app.router.lifespan_context = original_lifespan


def _make_engine_stub(pg_pool: asyncpg.Pool) -> Any:
    class _EngineStub:
        pg_pool: asyncpg.Pool
        redis_client: Any = MagicMock()

    stub = _EngineStub()
    stub.pg_pool = pg_pool
    return stub


async def _seed_mode(
    conn: asyncpg.Connection,
    *,
    namespace_id: UUID,
    engine: str,
    function: str,
    mode: str,
) -> None:
    """Insert a source_mode_config row."""
    await conn.execute(
        """
        INSERT INTO source_mode_config (namespace_id, engine, function, mode)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (namespace_id, engine, function)
        DO UPDATE SET mode = EXCLUDED.mode, updated_at = now()
        """,
        namespace_id,
        engine,
        function,
        mode,
    )


async def _insert_sales_record(
    conn: asyncpg.Connection,
    namespace_id: UUID,
    entity: str,
    source_id: str,
    name: str,
    source_json: dict[str, Any],
    manual: dict[str, Any] | None = None,
    is_deleted: bool = False,
    modifiedon: datetime.datetime | None = None,
) -> None:
    if modifiedon is None:
        modifiedon = datetime.datetime.now(datetime.timezone.utc)
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
        namespace_id,
        entity,
        source_id,
        name,
        json.dumps(source_json),
        json.dumps(manual_json),
        is_deleted,
        modifiedon,
    )


class MockDataverseClient:
    """A mock Dataverse client that implements an async generator for paginate."""

    def __init__(self, records: list[dict[str, Any]]):
        self.records = records

    async def paginate(
        self,
        entity: str,
        select: list[str] | None = None,
        filter_expr: str | None = None,
        page_size: int = 100,
    ) -> Any:
        for rec in self.records:
            yield rec


# HMAC Signing helpers for admin app requests
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


# ---------------------------------------------------------------------------
# Routing & Read Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sales_source_mode_routing(pg_pool: asyncpg.Pool, make_namespace: Any) -> None:
    ns_id: UUID = await make_namespace()
    engine = _make_engine_stub(pg_pool)

    # 1. Test D365 Mode: should query external client
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, ns_id)
        await _seed_mode(
            conn, namespace_id=ns_id, engine="sales", function="list_customers", mode="d365"
        )

    mock_records = [{"accountid": "ext-1", "name": "External Account", "address1_city": "Oslo"}]
    mock_client = MockDataverseClient(mock_records)

    with (
        patch("nce.vertical_modules.sales.source_mode.get_d365_client", return_value=mock_client),
        patch("nce.config.cfg.NCE_D365_ORG_URL", "https://mock.dynamics.com"),
    ):
        res = await do_list_customers(engine, {"namespace_id": ns_id})
        assert res["total"] == 1
        assert res["items"][0]["name"] == "External Account"

    # 2. Test NCE Mode: should query local NCE read model
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, ns_id)
        await _seed_mode(
            conn, namespace_id=ns_id, engine="sales", function="list_customers", mode="nce"
        )
        await _insert_sales_record(
            conn,
            namespace_id=ns_id,
            entity="accounts",
            source_id="nat-1",
            name="Native Account",
            source_json={"accountid": "nat-1", "name": "Native Account", "address1_city": "Bergen"},
        )

    res = await do_list_customers(engine, {"namespace_id": ns_id})
    assert res["total"] == 1
    assert res["items"][0]["name"] == "Native Account"


# ---------------------------------------------------------------------------
# Divergence Logging Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sales_divergence_logging(pg_pool: asyncpg.Pool, make_namespace: Any) -> None:
    ns_id: UUID = await make_namespace()
    engine = _make_engine_stub(pg_pool)

    # Seed 'both' mode
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, ns_id)
        await _seed_mode(
            conn, namespace_id=ns_id, engine="sales", function="list_customers", mode="both"
        )

        # Seed native record
        await _insert_sales_record(
            conn,
            namespace_id=ns_id,
            entity="accounts",
            source_id="both-1",
            name="Native Cust Name",
            source_json={
                "accountid": "both-1",
                "name": "Native Cust Name",
                "address1_city": "Oslo",
            },
        )

    # Seed different external record to trigger divergence
    mock_records = [
        {"accountid": "both-1", "name": "External Cust Name", "address1_city": "Bergen"}
    ]
    mock_client = MockDataverseClient(mock_records)

    with (
        patch("nce.vertical_modules.sales.source_mode.get_d365_client", return_value=mock_client),
        patch("nce.config.cfg.NCE_D365_ORG_URL", "https://mock.dynamics.com"),
    ):
        res = await do_list_customers(engine, {"namespace_id": ns_id})
        # Result should be native record
        assert res["items"][0]["name"] == "Native Cust Name"

    # Verify divergence log records the name and address1_city divergences
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, ns_id)
        rows = await conn.fetch(
            "SELECT entity, field, nce_value, ext_value, materiality FROM divergence_log WHERE namespace_id = $1 ORDER BY field",
            ns_id,
        )

    assert len(rows) == 2
    # Oslo vs Bergen (categorical difference -> materiality 1.0)
    assert rows[0]["field"] == "address1_city"
    assert rows[0]["nce_value"] == "Oslo"
    assert rows[0]["ext_value"] == "Bergen"
    assert float(rows[0]["materiality"]) == 1.0

    # Native Cust Name vs External Cust Name
    assert rows[1]["field"] == "name"
    assert rows[1]["nce_value"] == "Native Cust Name"
    assert rows[1]["ext_value"] == "External Cust Name"
    assert float(rows[1]["materiality"]) == 1.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_numeric_field_materiality(pg_pool: asyncpg.Pool, make_namespace: Any) -> None:
    ns_id: UUID = await make_namespace()
    engine = _make_engine_stub(pg_pool)

    # Seed 'both' mode for sales_overview
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, ns_id)
        await _seed_mode(
            conn, namespace_id=ns_id, engine="sales", function="sales_overview", mode="both"
        )

    # Mock the do_sales_overview facade function behavior to return 2 stages for native, 4 for external
    native_data = {"stages": [{"stage": "G1"}, {"stage": "G2"}]}
    external_data = {"stages": [{"stage": "G1"}, {"stage": "G2"}, {"stage": "G3"}, {"stage": "G4"}]}

    with (
        patch(
            "nce.vertical_modules.sales.read_model.do_sales_overview",
            side_effect=[native_data, external_data],
        ),
    ):
        await do_sales_overview(engine, {"namespace_id": ns_id})

    # Verify divergence log records numeric materiality:
    # nce = 2, ext = 4 -> materiality = abs(2 - 4) / max(2, 4, 1) = 2/4 = 0.5
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, ns_id)
        row = await conn.fetchrow(
            "SELECT field, nce_value, ext_value, materiality FROM divergence_log WHERE namespace_id = $1 AND engine = 'sales'",
            ns_id,
        )

    assert row is not None
    assert row["field"] == "total_stages"
    assert row["nce_value"] == "2"
    assert row["ext_value"] == "4"
    assert float(row["materiality"]) == 0.5


# ---------------------------------------------------------------------------
# Admin API Endpoints Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_sales_source_mode_endpoints(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
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
            r = await client.get(f"/api/admin/sales/source-mode?namespace_id={ns_id}")
            assert r.status_code == 401

            # 2. GET with Auth -> 200 (empty modes initially)
            headers = _valid_headers(key, "GET", "/api/admin/sales/source-mode")
            r = await client.get(
                f"/api/admin/sales/source-mode?namespace_id={ns_id}", headers=headers
            )
            assert r.status_code == 200
            assert r.json()["modes"] == {}

            # 3. PUT with Auth -> update to 'both'
            body = {
                "namespace_id": str(ns_id),
                "function": "list_customers",
                "mode": "both",
            }
            body_bytes = json.dumps(body).encode("utf-8")
            headers = _valid_headers(key, "PUT", "/api/admin/sales/source-mode", body_bytes)
            r = await client.put(
                "/api/admin/sales/source-mode", content=body_bytes, headers=headers
            )
            assert r.status_code == 200
            assert r.json()["status"] == "updated"
            assert r.json()["mode"] == "both"

            # Verify it is stored in PG
            async with pg_pool.acquire() as conn:
                await set_namespace_context(conn, ns_id)
                mode = await conn.fetchval(
                    "SELECT mode FROM source_mode_config WHERE namespace_id = $1 AND engine = 'sales' AND function = 'list_customers'",
                    ns_id,
                )
            assert mode == "both"

            # 4. PUT with Auth -> update to 'nce' is blocked due to recent divergence
            # Seed a divergence row within the lookback window (1 hour)
            async with pg_pool.acquire() as conn:
                await set_namespace_context(conn, ns_id)
                await conn.execute(
                    """
                    INSERT INTO divergence_log (namespace_id, engine, entity, field, nce_value, ext_value, materiality, detected_at)
                    VALUES ($1, 'sales', 'account:div-block', 'name', 'N', 'E', 1.0, now())
                    """,
                    ns_id,
                )

            body = {
                "namespace_id": str(ns_id),
                "function": "list_customers",
                "mode": "nce",
            }
            body_bytes = json.dumps(body).encode("utf-8")
            headers = _valid_headers(key, "PUT", "/api/admin/sales/source-mode", body_bytes)
            r = await client.put(
                "/api/admin/sales/source-mode", content=body_bytes, headers=headers
            )
            # Rejects with 400 Bad Request
            assert r.status_code == 400
            assert "blocked" in r.json()["error"].lower()

            # 5. PUT with Auth -> update to 'nce' succeeds after clearing lookback window
            # Delete divergence rows
            async with pg_pool.acquire() as conn:
                await set_namespace_context(conn, ns_id)
                await conn.execute("DELETE FROM divergence_log WHERE namespace_id = $1", ns_id)

            r = await client.put(
                "/api/admin/sales/source-mode", content=body_bytes, headers=headers
            )
            assert r.status_code == 200
            assert r.json()["mode"] == "nce"

            # Verify mode is updated in PG
            async with pg_pool.acquire() as conn:
                await set_namespace_context(conn, ns_id)
                mode = await conn.fetchval(
                    "SELECT mode FROM source_mode_config WHERE namespace_id = $1 AND engine = 'sales' AND function = 'list_customers'",
                    ns_id,
                )
            assert mode == "nce"
