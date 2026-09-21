"""
tests/integration/test_admin_jsonb_read_decode_live.py
==========================================================
Live-Postgres regression for two admin REST handlers that shipped a jsonb
column as a raw, doubly-escaped JSON string instead of a decoded object.

WHY THIS MUST BE LIVE, NOT A MOCK
------------------------------------
This pool registers no jsonb codec (confirmed by
tests/test_semantic_search_metadata_text.py's own docstring), so every
jsonb column round-trips through asyncpg as raw JSON TEXT, never a
decoded/encoded Python dict automatically. Both bugs below were invisible
to their handlers' only existing coverage because nothing exercised a real
asyncpg jsonb read: neither ``api_product_enrichment_review`` nor
``api_admin_d365_integrations`` had ANY prior test, mocked or live.

THE TWO BUGS, SAME SHAPE, DIFFERENT FILES
---------------------------------------------
1. ``api_product_enrichment_review`` (nce/admin_handlers/product.py) reads
   ``product_enrichment_log.trigger_context`` (JSONB, schema.sql:1498)
   through ``serialize_pg_row`` (nce/admin_http_support.py), which passes
   any value that isn't a datetime/Decimal/hex-bearing type straight
   through -- a raw jsonb string included. ``trigger_context`` is written
   correctly (enrich.py's ``json.dumps(trigger_context)`` before the
   ``$3::jsonb`` bind); nothing on the read side ever decoded it back.

2. ``api_admin_d365_integrations`` (nce/admin_handlers/d365.py) reads
   ``d365_integrations.last_sync_stats`` (JSONB, schema.sql:1181) via its
   own hand-built dict (``"last_sync_stats": r["last_sync_stats"]``) --
   this handler never called ``serialize_pg_row`` at all, so it is a
   second, independent instance of the same missing-decode shape, not a
   second symptom of the shared helper.

Neither fix touches ``serialize_pg_row`` itself: a caller audit (reported
before building) found only 1 of its 6 real call sites was actually
affected, and a generic decode inside that schema-blind helper would risk
silently type-coercing a plain TEXT column whose value happens to be a
valid JSON scalar (``"123"``, ``"true"``, ``"null"``) -- worse than a
double-decode, which at least raises. Both fixes below are therefore local,
explicit, isinstance-guarded ``json.loads`` calls at the two actual read
sites, mirroring the working precedent already in this codebase for the
same problem (``nce/vertical_modules/assets/mcp_handlers.py``'s
``do_get_asset_merge_queue``).

MUTATION-VERIFIED, TWO-RUN PROTOCOL
----------------------------------------
Run 1 -- broken: reverted both handlers' decode blocks (git stash) and ran
this file. Both tests failed:
- ``test_product_enrichment_review_decodes_trigger_context``: AssertionError,
  ``trigger_context`` was the raw string
  ``'{"missing_fields": ["weight_kg"], "source_watermark": "sku-42"}'``, not
  a dict.
- ``test_d365_integrations_decodes_last_sync_stats``: same shape, raw string
  instead of a dict.
Run 2 -- restored: reverted the stash, re-ran, both PASSED.
"""

from __future__ import annotations

import json
import uuid

import asyncpg
import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from nce import admin_state
from nce.admin_handlers import d365 as d365_handlers
from nce.admin_handlers import product as product_handlers
from nce.engine_registry import populate_engine_modules
from nce.orchestrator import NCEEngine

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


@pytest.mark.asyncio
async def test_product_enrichment_review_decodes_trigger_context(
    engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    await _set_product_enabled(pg_pool, namespace_id)
    product_id = uuid.uuid4()
    trigger_context = {"missing_fields": ["weight_kg"], "source_watermark": "sku-42"}
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO product_enrichment_log
                (namespace_id, product_id, trigger_context, field_name,
                 field_value, confidence, needs_review, product_source_id)
            VALUES ($1, $2, $3::jsonb, 'weight_kg', '12.5', 0.4, true, 'test-source')
            """,
            namespace_id,
            product_id,
            json.dumps(trigger_context),
        )

    admin_state.engine = engine
    app = Starlette(
        routes=[
            Route(
                "/api/product/enrichment/review",
                endpoint=product_handlers.api_product_enrichment_review,
                methods=["GET"],
            ),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/product/enrichment/review",
            params={"namespace_id": str(namespace_id)},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = [i for i in body["items"] if i["product_id"] == str(product_id)]
    assert len(items) == 1, body
    assert items[0]["trigger_context"] == trigger_context, (
        f"trigger_context was not decoded to a dict: {items[0]['trigger_context']!r}"
    )


@pytest.mark.asyncio
async def test_d365_integrations_decodes_last_sync_stats(
    engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    last_sync_stats = {"created": 3, "updated": 7, "errors": []}
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO d365_integrations (namespace_id, org_url, last_sync_at, last_sync_stats)
            VALUES ($1, $2, NOW(), $3::jsonb)
            """,
            namespace_id,
            f"https://example-{uuid.uuid4().hex[:8]}.crm.dynamics.com",
            json.dumps(last_sync_stats),
        )

    admin_state.engine = engine
    app = Starlette(
        routes=[
            Route(
                "/api/admin/d365/integrations",
                endpoint=d365_handlers.api_admin_d365_integrations,
                methods=["GET"],
            ),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/admin/d365/integrations",
            params={"namespace_id": str(namespace_id), "limit": 200},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = [i for i in body["items"] if i["namespace_id"] == str(namespace_id)]
    assert len(items) == 1, body
    assert items[0]["last_sync_stats"] == last_sync_stats, (
        f"last_sync_stats was not decoded to a dict: {items[0]['last_sync_stats']!r}"
    )
