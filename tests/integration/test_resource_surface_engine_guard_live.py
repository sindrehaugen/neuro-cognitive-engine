"""
tests/integration/test_resource_surface_engine_guard_live.py
==============================================================
Live-Postgres regression test for the C12 resource-surface per-namespace
opt-in gate (charter Q-45 finding, 2026-09-19: 18 of 30 registered specs
across 7 engines with a ``_guard.py`` were reachable through the generated
MCP tools and REST routes without ever checking ``metadata.<engine>.enabled``
-- every hand-written handler for the same engines calls its own
``require_*_enabled`` at the handler/route boundary; ``nce/resource_surface/``
itself called none of them, for any spec).

WHY THIS MUST BE A LIVE TEST, NOT A MOCK
-----------------------------------------
The gate is a real query against the ``namespaces`` table
(``metadata->'<engine>'->>'enabled'``). A mocked pool/connection returns
whatever the mock is told to return, so a mocked test proves the gate wiring
compiles, never that it reads the right row for the right namespace. Same
reasoning as ``test_resource_surface_upsert_live.py`` and
``test_golden_thread.py``.

THE FIX
-------
``ResourceSpec`` gained an optional ``enabled_guard`` field: an
``async (pool, namespace_id) -> None`` callable that raises a subclass of
``EngineDisabledError`` when the namespace has not opted in. Every one of the
18 specs across inventory/support/agreements/field_tech/resources/economy/
product now sets it to that engine's existing ``require_*_enabled`` (an
adapter for the ``resources`` engine, whose hand-written guard takes an
already-fetched ``namespace_metadata`` dict instead of a pool -- see
``nce/vertical_modules/resources/resources.py::_require_resources_enabled_via_pool``).
``nce/resource_surface/mcp.py`` and ``rest.py`` call it at the top of every
generated handler/route, both reads and writes, letting the exception
propagate through the same translation path (``@mcp_handler`` ->
``McpError`` code -32005; REST -> a 409 via ``admin_error_response``) a
hand-written boundary already uses.

Deliberately NOT keyed on ``spec.tenant_scope``: PRODUCT_SKU is
``tenant_scope == "global"`` (a shared parts catalog with no namespace_id
column) but its hand-written boundary still requires and gates on a caller
namespace_id -- keying the generated gate on tenant_scope would have left
PRODUCT_SKU one of the 18 ungated specs this fix exists to close.

A SECOND DEFECT IN THE FIRST FIX: OMITTING namespace_id SKIPPED THE GATE TOO
-----------------------------------------------------------------------------
The first cut of this fix only ran ``enabled_guard`` when ``ns_uuid`` was
already non-``None`` (i.e. a ``namespace_id`` was supplied and parsed). For a
tenant-scoped spec that's fine -- ``namespace_id`` was already mandatory. For
a GLOBAL spec with an ``enabled_guard`` (PRODUCT_SKU), ``namespace_id`` was
still treated as *optional* (``extract_namespace_id(..., required=is_tenant)``
/ the MCP handlers' own ``if is_tenant: ... else: if ns_raw: ...``), so a
caller that omitted it entirely got ``ns_uuid = None`` and the guard never
ran -- while ``nce/admin_handlers/product.py:70``, the hand-written boundary
this gate is supposed to match, hard-requires ``namespace_id`` and returns
422 for the same omission. Found by ML-orch reviewing #294: the generated
surface was still strictly more permissive than the hand-written one, for
exactly the one input shape (`namespace_id` omitted, not just invalid) the
live verification below didn't produce.

Fixed by introducing ``requires_namespace = is_tenant or spec.enabled_guard
is not None`` in both ``mcp.py`` and ``rest.py``: an ``enabled_guard``'s
subject is the CALLER's namespace, not the table's storage scope, so it makes
``namespace_id`` mandatory regardless of ``tenant_scope`` -- refusing the
omitted case (422 REST / the MCP "missing required argument" shape) rather
than silently treating "no namespace" as "nothing to gate."
``test_mcp_list_refused_when_namespace_id_omitted_for_global_gated_spec`` and
its REST equivalent below exercise PRODUCT_SKU specifically, the one real
spec this gap affected -- ``STOCK_LOCATION_SPEC`` (tenant-scoped) cannot
reach this code path at all, which is exactly why the first round of live
verification passed while the gap stayed open: the positive control and the
gap were on different specs.

MUTATION-CHECKED (verified by hand, not via a scripted rerun)
-----------------------------------------------------------------
Ran ``test_mcp_list_refused_when_namespace_not_opted_in`` and
``test_rest_list_refused_when_namespace_not_opted_in`` with
``enabled_guard=require_inventory_enabled`` temporarily removed from
``STOCK_LOCATION_SPEC``: both failed (no exception / 200 instead of a 409),
confirming they exercise the gate and are not vacuous. Restored before commit.

Ran ``test_mcp_list_refused_when_namespace_id_omitted_for_global_gated_spec``
and its REST equivalent with ``requires_namespace`` reverted to plain
``is_tenant`` in both modules: both failed (200/successful list instead of a
refusal), confirming they exercise the omitted-namespace-id path specifically
and are not vacuous. Restored before commit.
"""

from __future__ import annotations

import json
import uuid

import asyncpg
import httpx
import pytest
from starlette.applications import Starlette

from nce import admin_state
from nce.engine_registry import populate_engine_modules
from nce.mcp_errors import MCP_ENGINE_DISABLED, McpError
from nce.orchestrator import NCEEngine
from nce.resource_surface import get_all_resource_specs, load_all_engine_resources
from nce.resource_surface.rest import make_resource_routes
from nce.tool_registry import TOOL_REGISTRY

pytestmark = pytest.mark.integration


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)
    return eng


async def _set_inventory_enabled(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE namespaces SET metadata = "
            "COALESCE(metadata, '{}'::jsonb) || '{\"inventory\": {\"enabled\": true}}'::jsonb "
            "WHERE id = $1",
            namespace_id,
        )


@pytest.mark.asyncio
async def test_mcp_list_refused_when_namespace_not_opted_in(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """The ``namespace_id`` fixture's namespace has no metadata opt-in at all
    -- this is the exact 18-of-30 gap: a plain read through the generated MCP
    surface must now be refused, not silently served.
    """
    with pytest.raises(McpError) as exc_info:
        await TOOL_REGISTRY["inventory_list_stock_locations"].handler(
            engine, {"namespace_id": str(namespace_id)}
        )
    assert exc_info.value.code == MCP_ENGINE_DISABLED


@pytest.mark.asyncio
async def test_mcp_list_succeeds_when_namespace_opted_in(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    await _set_inventory_enabled(pg_pool, namespace_id)
    result = json.loads(
        await TOOL_REGISTRY["inventory_list_stock_locations"].handler(
            engine, {"namespace_id": str(namespace_id)}
        )
    )
    assert "items" in result, f"opted-in namespace was still refused: {result}"


@pytest.mark.asyncio
async def test_mcp_upsert_refused_when_namespace_not_opted_in(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """The write half, not just reads -- #284 (merged same day as this gate)
    fixed the datetime crash that used to make every C12 write fail before
    this check would ever have mattered; this proves the gate covers writes
    now that the write path actually runs.
    """
    with pytest.raises(McpError) as exc_info:
        await TOOL_REGISTRY["inventory_upsert_stock_locations"].handler(
            engine,
            {"namespace_id": str(namespace_id), "kind": "warehouse", "name": "Refused Warehouse"},
        )
    assert exc_info.value.code == MCP_ENGINE_DISABLED


@pytest.mark.asyncio
async def test_positive_control_mcp_gate_reads_the_right_namespace(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """Standing positive control: proves the gate is scoped per-namespace,
    not a global switch -- a second, still-unopted-in namespace must still be
    refused after the first one opts in.
    """
    await _set_inventory_enabled(pg_pool, namespace_id)
    result = json.loads(
        await TOOL_REGISTRY["inventory_list_stock_locations"].handler(
            engine, {"namespace_id": str(namespace_id)}
        )
    )
    assert "items" in result

    async with pg_pool.acquire() as conn:
        other_ns_id = await conn.fetchval(
            "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
            f"gt-guard-other-{uuid.uuid4().hex[:8]}",
        )
    try:
        with pytest.raises(McpError) as exc_info:
            await TOOL_REGISTRY["inventory_list_stock_locations"].handler(
                engine, {"namespace_id": str(other_ns_id)}
            )
        assert exc_info.value.code == MCP_ENGINE_DISABLED
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM namespaces WHERE id = $1", other_ns_id)


def _inventory_rest_app() -> Starlette:
    load_all_engine_resources()
    stock_location_spec = next(
        s
        for s in get_all_resource_specs()
        if s.engine == "inventory" and s.entity == "stock-locations"
    )
    return Starlette(routes=make_resource_routes(stock_location_spec))


@pytest.fixture
def inventory_rest_client(engine: NCEEngine):
    """A real ASGI client for the generated inventory REST routes, wired to
    the same live engine as the MCP tests above. ``admin_state.engine`` is
    what the REST handlers actually read.
    """
    previous = admin_state.engine
    admin_state.engine = engine
    try:
        yield httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_inventory_rest_app()), base_url="http://test"
        )
    finally:
        admin_state.engine = previous


@pytest.mark.asyncio
async def test_rest_list_refused_when_namespace_not_opted_in(
    inventory_rest_client: httpx.AsyncClient, namespace_id: uuid.UUID
) -> None:
    async with inventory_rest_client as client:
        r = await client.get(
            "/api/inventory/stock-locations", params={"namespace_id": str(namespace_id)}
        )
    assert r.status_code == 409, f"ungated read over REST: {r.text}"


@pytest.mark.asyncio
async def test_rest_list_succeeds_when_namespace_opted_in(
    inventory_rest_client: httpx.AsyncClient, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    await _set_inventory_enabled(pg_pool, namespace_id)
    async with inventory_rest_client as client:
        r = await client.get(
            "/api/inventory/stock-locations", params={"namespace_id": str(namespace_id)}
        )
    assert r.status_code == 200, f"opted-in namespace was still refused over REST: {r.text}"


@pytest.mark.asyncio
async def test_rest_create_refused_when_namespace_not_opted_in(
    inventory_rest_client: httpx.AsyncClient, namespace_id: uuid.UUID
) -> None:
    async with inventory_rest_client as client:
        r = await client.post(
            "/api/inventory/stock-locations",
            json={
                "namespace_id": str(namespace_id),
                "kind": "warehouse",
                "name": "Refused REST Warehouse",
            },
        )
    assert r.status_code == 409, f"ungated write over REST: {r.text}"


@pytest.mark.asyncio
async def test_mcp_list_refused_when_namespace_id_omitted_for_global_gated_spec(
    engine: NCEEngine,
) -> None:
    """PRODUCT_SKU is tenant_scope="global" (no namespace_id column at all)
    but its hand-written boundary (nce/admin_handlers/product.py) still
    requires a caller namespace_id and gates on it. The first cut of this
    fix let a caller skip the gate entirely by omitting namespace_id, since
    an omitted-but-optional namespace_id meant `ns_uuid is None` and the
    guard call was itself keyed on `ns_uuid` being truthy. No namespace_id
    is passed at all here -- this is the exact input the first fix missed.
    """
    result = json.loads(await TOOL_REGISTRY["product_list_product_skus"].handler(engine, {}))
    # A missing-argument refusal (the existing, established shape for every
    # other required-argument check in this module), not the guard's
    # McpError -- there is no namespace to gate yet, there is no namespace
    # at all. Must not be a successful item listing either way.
    assert "items" not in result, f"omitted namespace_id was silently served: {result}"
    assert "namespace_id" in result.get("error", "").lower(), result


def _product_rest_app() -> Starlette:
    load_all_engine_resources()
    product_sku_spec = next(
        s for s in get_all_resource_specs() if s.engine == "product" and s.entity == "product-skus"
    )
    return Starlette(routes=make_resource_routes(product_sku_spec))


@pytest.fixture
def product_rest_client(engine: NCEEngine):
    previous = admin_state.engine
    admin_state.engine = engine
    try:
        yield httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_product_rest_app()), base_url="http://test"
        )
    finally:
        admin_state.engine = previous


@pytest.mark.asyncio
async def test_rest_list_refused_when_namespace_id_omitted_for_global_gated_spec(
    product_rest_client: httpx.AsyncClient,
) -> None:
    """REST equivalent of the MCP test above -- the exact input (no
    namespace_id query param at all) the first cut of this fix let through
    for a global-scoped-but-gated spec. Matches
    nce/admin_handlers/product.py:70's own 422 for the same omission, not
    the 409 a present-but-disabled namespace gets.
    """
    async with product_rest_client as client:
        r = await client.get("/api/product/product-skus")
    assert r.status_code == 422, f"omitted namespace_id was not refused over REST: {r.text}"
