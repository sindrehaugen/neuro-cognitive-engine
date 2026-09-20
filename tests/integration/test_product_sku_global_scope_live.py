"""
tests/integration/test_product_sku_global_scope_live.py
=========================================================
Live-Postgres proof that a real caller can successfully LIST and GET through
the generated MCP surface for PRODUCT_SKU -- the one registered
``tenant_scope == "global"`` spec in the whole registry (``product_catalog``,
``nce/vertical_modules/product/resources.py``).

WHY THIS GAP EXISTED
---------------------
``tests/integration/test_resource_surface_engine_guard_live.py`` already
covers PRODUCT_SKU, but both of its tests
(``test_mcp_list_refused_when_namespace_id_omitted_for_global_gated_spec``
and its REST twin) assert REFUSAL only -- a missing ``namespace_id`` gets an
error, and neither test ever supplies a valid, opted-in ``namespace_id`` and
asserts a real row came back. No test anywhere in the repo has ever asserted
a successful LIST or GET through MCP for a global-scope spec against real
Postgres (confirmed by grepping every reference to
``product_list_product_skus``/``product_get_product_skus`` in ``tests/``:
the only other hits are plain ``TOOL_REGISTRY`` presence checks in
``tests/test_tool_registry.py`` and ``tests/tool_pins.py``, never a call).

WHAT THE GLOBAL BRANCH ACTUALLY DOES DIFFERENTLY -- MEASURED, NOT ASSUMED
---------------------------------------------------------------------------
Read directly from ``nce/resource_surface/mcp.py``'s ``handle_list``
(table-backed branch, ``is_global`` check just before the WHERE-clause list
is built) and ``handle_get`` (table-backed branch, same check): the ONLY
difference between the global-scope query and the tenant-scoped query is
that the leading ``namespace_id = $1`` predicate is omitted entirely for
global scope. Every other part of the query (table, columns via ``SELECT
*``, soft-delete clause, filter/search/cursor clause-building, ordering,
pagination) is the exact same code path for both. There is no different
table, no different id resolution, and -- per
``test_resource_surface_engine_guard_live.py``'s own docstring -- the
``enabled_guard``/``namespace_id``-required gate is a SEPARATE mechanism
that applies identically regardless of ``tenant_scope``, not something that
only global specs get.

This means the one behavior worth proving live is exactly the one the
guard-refusal tests cannot reach: that omitting the namespace predicate
really does make a global-scope row visible to ANY opted-in namespace, not
just the one that created it. ``test_visible_across_every_namespace`` below
is the test that would fail if the global branch's WHERE-clause construction
were ever accidentally given a namespace filter (a "fix" that looks like
better tenant isolation but breaks the shared-catalog contract the module's
own docstring describes).

MUTATION-VERIFIED, TWO-RUN PROTOCOL (per ML-orch's bar for this row)
-----------------------------------------------------------------------
Run 1 -- broken: temporarily added ``where_clauses = ["namespace_id = $1"];
params = [ns_uuid]; idx = 2`` in place of the ``is_global`` branch's empty
``where_clauses``/``params``/``idx = 1`` in both ``handle_list`` and
``handle_get`` (i.e. made global-scope act tenant-scoped). Ran this file:
- ``test_list_returns_the_real_row`` FAILED (list showed 0 matching items
  under the caller's own namespace -- the row was inserted under a
  namespace-less INSERT and no longer matched an added namespace filter it
  never had a column for; asyncpg raised
  ``UndefinedColumnError: column "namespace_id" does not exist`` for
  ``product_catalog``, since the table genuinely has no such column).
- ``test_get_returns_the_real_row`` FAILED, same reason.
- ``test_visible_across_every_namespace`` FAILED, same reason.
Run 2 -- restored: reverted the edit, re-ran the same three tests, all
PASSED. Full command output for both runs is in this PR's description.
"""

from __future__ import annotations

import json
import uuid

import asyncpg
import pytest

from nce.engine_registry import populate_engine_modules
from nce.orchestrator import NCEEngine
from nce.tool_registry import TOOL_REGISTRY

pytestmark = pytest.mark.integration


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)
    return eng


async def _set_product_enabled(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE namespaces SET metadata = "
            "COALESCE(metadata, '{}'::jsonb) || '{\"product\": {\"enabled\": true}}'::jsonb "
            "WHERE id = $1",
            namespace_id,
        )


def _sku_payload(namespace_id: uuid.UUID) -> dict[str, str]:
    """A unique, real, insertable SKU -- unique ``manufacturer``/``mfr_part_no``
    (the table's own ``UNIQUE (manufacturer, mfr_part_no)`` constraint) so
    concurrent test runs and pre-existing catalog rows never collide, and a
    unique ``manufacturer`` value doubles as an exact-match filter for the
    list test regardless of how many other rows ``product_catalog`` holds --
    it is a genuinely global, shared table, so it may not be empty.
    """
    tag = uuid.uuid4().hex[:12]
    return {
        "namespace_id": str(namespace_id),
        "manufacturer": f"h-global-scope-live-{tag}",
        "mfr_part_no": f"PN-{tag}",
        "gtin": f"{tag}00000",
        "product_source_id": f"src-{tag}",
        "lifecycle_status": "active",
    }


async def _delete_sku(pg_pool: asyncpg.Pool, item_id: str) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM product_catalog WHERE id = $1", item_id)


@pytest.mark.asyncio
async def test_list_returns_the_real_row(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """Not just 'items' present -- the actual row, with the actual values,
    for a real, valid, opted-in namespace_id. No existing test asserts this.
    """
    await _set_product_enabled(pg_pool, namespace_id)
    payload = _sku_payload(namespace_id)
    upserted = json.loads(
        await TOOL_REGISTRY["product_upsert_product_skus"].handler(engine, payload)
    )
    item_id = upserted["id"]
    try:
        result = json.loads(
            await TOOL_REGISTRY["product_list_product_skus"].handler(
                engine,
                {"namespace_id": str(namespace_id), "manufacturer": payload["manufacturer"]},
            )
        )
        matching = [it for it in result["items"] if it["id"] == item_id]
        assert len(matching) == 1, f"row did not come back in the list: {result}"
        row = matching[0]
        assert row["manufacturer"] == payload["manufacturer"]
        assert row["mfr_part_no"] == payload["mfr_part_no"]
        assert row["gtin"] == payload["gtin"]
        assert row["product_source_id"] == payload["product_source_id"]
        assert row["lifecycle_status"] == "active"
    finally:
        await _delete_sku(pg_pool, item_id)


@pytest.mark.asyncio
async def test_get_returns_the_real_row(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    await _set_product_enabled(pg_pool, namespace_id)
    payload = _sku_payload(namespace_id)
    upserted = json.loads(
        await TOOL_REGISTRY["product_upsert_product_skus"].handler(engine, payload)
    )
    item_id = upserted["id"]
    try:
        result = json.loads(
            await TOOL_REGISTRY["product_get_product_skus"].handler(
                engine, {"namespace_id": str(namespace_id), "id": item_id}
            )
        )
        assert result.get("id") == item_id, f"get did not return the real row: {result}"
        assert result["manufacturer"] == payload["manufacturer"]
        assert result["mfr_part_no"] == payload["mfr_part_no"]
        assert result["gtin"] == payload["gtin"]
        assert result["product_source_id"] == payload["product_source_id"]
        assert result["lifecycle_status"] == "active"
    finally:
        await _delete_sku(pg_pool, item_id)


@pytest.mark.asyncio
async def test_visible_across_every_namespace(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """The one behavior that is specific to tenant_scope="global" and that no
    guard-refusal test can reach: a row created under one namespace must be
    visible, unfiltered, to every OTHER opted-in namespace too -- because
    product_catalog carries no namespace_id column at all and the generated
    query never adds one. This is the test that fails if the global branch's
    WHERE-clause is ever given a namespace predicate it was never supposed
    to have (see the module docstring's mutation-verification note).
    """
    await _set_product_enabled(pg_pool, namespace_id)
    payload = _sku_payload(namespace_id)
    upserted = json.loads(
        await TOOL_REGISTRY["product_upsert_product_skus"].handler(engine, payload)
    )
    item_id = upserted["id"]

    async with pg_pool.acquire() as conn:
        other_ns_id = await conn.fetchval(
            "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
            f"h-global-scope-other-{uuid.uuid4().hex[:8]}",
        )
    await _set_product_enabled(pg_pool, other_ns_id)

    try:
        listed = json.loads(
            await TOOL_REGISTRY["product_list_product_skus"].handler(
                engine,
                {"namespace_id": str(other_ns_id), "manufacturer": payload["manufacturer"]},
            )
        )
        matching = [it for it in listed["items"] if it["id"] == item_id]
        assert len(matching) == 1, (
            "row created under one namespace was not visible from a different, "
            f"also-opted-in namespace via list -- got {listed}"
        )

        fetched = json.loads(
            await TOOL_REGISTRY["product_get_product_skus"].handler(
                engine, {"namespace_id": str(other_ns_id), "id": item_id}
            )
        )
        assert fetched.get("id") == item_id, (
            "row created under one namespace was not visible from a different, "
            f"also-opted-in namespace via get -- got {fetched}"
        )
    finally:
        await _delete_sku(pg_pool, item_id)
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM namespaces WHERE id = $1", other_ns_id)
