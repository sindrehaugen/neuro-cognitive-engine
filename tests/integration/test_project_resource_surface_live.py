"""
tests/integration/test_project_resource_surface_live.py
==========================================================
Live-Postgres verification of Wave C-6: the PROJECT_PROJECT C12 resource
declaration (nce/vertical_modules/project/resources.py) and the new
GET /api/project/{id}/bom-lines route (nce/admin_handlers/project.py),
which exposes nce/bom_lines.py's list_bom_lines_for_quote -- a function
that existed with no route before this wave.

PROJECT_PROJECT is deliberately thin (kg_nodes-primary, no secondary
tables): kg_nodes has no attribute storage of its own, confirmed empty for
every PROJECT_* node type (Q-47, still open with Sindre). This spec closes
the C12 registration exemption (list/get/create/patch/events) without
claiming to expose phase gates/capacity/my-day/reports, which stay on
their own hand-written routes.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import asyncpg
import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from nce import admin_state
from nce.admin_app import build_admin_routes
from nce.auth import set_namespace_context
from nce.bom_lines import create_bom_line
from nce.engine_registry import populate_engine_modules
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.orchestrator import NCEEngine
from nce.resource_surface.mcp import build_mcp_tool_specs
from nce.resource_surface.rest import make_resource_routes
from nce.vertical_modules.project.resources import PROJECT_SPEC
from tests._verified_tier_middleware import VERIFIED_TIER_TEST_MIDDLEWARE

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def engine(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)
    return eng


def _rest_client(engine: NCEEngine) -> httpx.AsyncClient:
    admin_state.engine = engine
    app = Starlette(
        routes=make_resource_routes(PROJECT_SPEC), middleware=VERIFIED_TIER_TEST_MIDDLEWARE
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _admin_client(engine: NCEEngine) -> httpx.AsyncClient:
    """Full admin app, for the hand-written bom-lines route (not generated)."""
    admin_state.engine = engine
    app = Starlette(routes=build_admin_routes())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_project_rest_create_then_get(engine: NCEEngine, namespace_id: uuid.UUID) -> None:
    project_label = f"PROJECT:Q{uuid.uuid4().hex[:8].upper()}"
    async with _rest_client(engine) as client:
        r1 = await client.post(
            "/api/project/projects",
            json={"namespace_id": str(namespace_id), "label": project_label},
        )
        assert r1.status_code == 201, r1.text

        r2 = await client.get(
            f"/api/project/projects/{project_label}",
            params={"namespace_id": str(namespace_id)},
            headers={"X-NCE-Principal-Tier": "employee"},
        )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["label"] == project_label
    assert body["entity_type"] == "PROJECT_PROJECT"


@pytest.mark.asyncio
async def test_project_mcp_upsert_then_patch_change_origin(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    tools = build_mcp_tool_specs(PROJECT_SPEC)
    project_label = f"PROJECT:Q{uuid.uuid4().hex[:8].upper()}"

    upsert = tools[f"project_upsert_{PROJECT_SPEC.mcp_slug}"]
    result = json.loads(
        await upsert.handler(engine, {"namespace_id": str(namespace_id), "id": project_label})
    )
    assert result["status"] == "ok", result

    result2 = json.loads(
        await upsert.handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": project_label,
                "change_origin": "operator",
            },
        )
    )
    assert result2["status"] == "ok", result2

    async with engine.pg_pool.acquire() as conn:
        change_origin = await conn.fetchval(
            "SELECT change_origin FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            project_label,
        )
    assert change_origin == "operator"


@pytest.mark.asyncio
async def test_project_create_refused_without_ownership_seed(pg_pool: asyncpg.Pool) -> None:
    """Same deny-by-default proof as the system_design suite, for a
    different real engine: an un-seeded namespace refuses the write,
    it does not silently succeed.
    """
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)

    async with pg_pool.acquire() as conn:
        fresh_ns = await conn.fetchval(
            "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
            f"pytest-project-unseeded-{uuid.uuid4().hex[:8]}",
        )
    try:
        async with _rest_client(eng) as client:
            r = await client.post(
                "/api/project/projects",
                json={"namespace_id": str(fresh_ns), "label": "PROJECT:SHOULD-BE-REFUSED"},
            )
        assert r.status_code == 403, r.text
        assert r.json()["reason"] == "ownership_denied"
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM namespaces WHERE id = $1", fresh_ns)


@pytest.mark.asyncio
async def test_get_bom_lines_route_returns_seeded_lines(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """Proves the route this wave adds actually reads real bom_line_content
    rows, seeded via the real guarded writer (create_bom_line), not a
    synthetic shortcut -- and that a project label correctly maps back to
    its quote_id (the same value, prefix-stripped).
    """
    quote_id = f"Q{uuid.uuid4().hex[:8].upper()}"
    project_label = f"PROJECT:{quote_id}"

    async with engine.pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await create_bom_line(
                conn,
                namespace_id,
                flow="manual",
                writer_engine="sales",
                quote_id=quote_id,
                line_ref="LINE-001",
                qty=Decimal("2"),
                unit_price=Decimal("500.00"),
                line_total=Decimal("1000.00"),
            )
            await create_bom_line(
                conn,
                namespace_id,
                flow="manual",
                writer_engine="sales",
                quote_id=quote_id,
                line_ref="LINE-002",
                qty=Decimal("1"),
                unit_price=Decimal("250.00"),
                line_total=Decimal("250.00"),
            )

    async with _admin_client(engine) as client:
        r = await client.get(
            f"/api/project/{project_label}/bom-lines",
            params={"namespace_id": str(namespace_id)},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == project_label
    assert body["quote_id"] == quote_id
    line_refs = {line["line_ref"] for line in body["bom_lines"]}
    assert line_refs == {"LINE-001", "LINE-002"}


@pytest.mark.asyncio
async def test_get_bom_lines_route_empty_for_a_quote_with_no_lines(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    project_label = f"PROJECT:Q{uuid.uuid4().hex[:8].upper()}"
    async with _admin_client(engine) as client:
        r = await client.get(
            f"/api/project/{project_label}/bom-lines",
            params={"namespace_id": str(namespace_id)},
        )
    assert r.status_code == 200, r.text
    assert r.json()["bom_lines"] == []


@pytest.mark.asyncio
async def test_get_bom_lines_route_missing_namespace_id(engine: NCEEngine) -> None:
    async with _admin_client(engine) as client:
        r = await client.get("/api/project/PROJECT:Q1/bom-lines")
    assert r.status_code == 422, r.text
