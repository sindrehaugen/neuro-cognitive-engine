"""
tests/integration/test_marketing_admin_listings_jsonb_decode_live.py
========================================================================
Live-Postgres proof that the marketing admin listing endpoints actually
decode the JSONB columns they return.

No asyncpg jsonb codec is registered anywhere in this codebase
(`nce/semantic_search.py`'s own comment documents this estate-wide fact),
so `testimonials.consent_scope` and `content_assets.seo` always arrive from
a real connection as raw JSON strings, never already-parsed dicts.
`_json_safe`'s own Decimal-aware `json.dumps`/`json.loads` round-trip
re-serializes a string AS a string -- it does not parse JSON text sitting
inside one -- so `GET /api/marketing/testimonials` and `GET
/api/marketing/assets` shipped these two columns as undecoded strings
before this fix.

The last two of the fourteen findings in the JSONB writer/reader sweep
(JSONB_WRITER_CONVENTION_SWEEP.md) that started from documents.py's fixed
bug (#406).

Checked, not assumed: nothing else in the codebase reads `consent_scope`
back from the database and branches on its structure -- the only other
reference is `marketing/testimonials.py`'s own writer, which echoes back
the caller-supplied Python dict from its own request params, never a value
read from this column. So this fix closes the only real consumer.
`content_assets.seo` has one other reader, `marketing/advisor.py`'s
`do_audit_seo` fallback (uses the raw value as SEO-audit fallback text,
`str(row_asset["seo"])`) -- filed separately, out of scope for an admin
listing decode fix; noted in JSONB_WRITER_CONVENTION_SWEEP.md.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest
from starlette.datastructures import QueryParams

from nce import admin_state
from nce.admin_handlers.marketing import api_marketing_assets, api_marketing_testimonials
from nce.orchestrator import NCEEngine

pytestmark = pytest.mark.integration


class _FakeRequest:
    """Matches exactly what these two handlers read off a request:
    `.query_params.get(...)` -- the same shape
    tests/unit/test_marketing_advisor.py's own `_MockRequest` uses, proven
    against these same handlers there."""

    def __init__(self, query_params: dict[str, str]) -> None:
        self.query_params = QueryParams(query_params)


async def _enable_marketing(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE namespaces SET metadata = jsonb_set("
            "coalesce(metadata, '{}'::jsonb), ARRAY['marketing'], "
            "coalesce(metadata->'marketing', '{}'::jsonb) || jsonb_build_object('enabled', true), "
            "true) WHERE id = $1",
            namespace_id,
        )


@pytest.mark.asyncio
async def test_marketing_testimonials_listing_decodes_consent_scope(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """Before this fix: `items[0]["consent_scope"]` in the response body is
    the literal string `'{"scope": "all", "channels": ["web"]}'`, not a
    dict -- `_json_safe` never parses JSON text living inside a string, it
    only makes ``Decimal``/non-finite floats JSON-serializable.
    """
    await _enable_marketing(pg_pool, namespace_id)

    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO testimonials (namespace_id, customer_id, quote, consent_scope)
            VALUES ($1, $2, $3, $4::jsonb)
            """,
            namespace_id,
            "CUST-jsonb-probe",
            "Reliable AV deployment.",
            '{"scope": "all", "channels": ["web"]}',
        )

    engine = NCEEngine()
    engine.pg_pool = pg_pool
    previous_engine = admin_state.engine
    admin_state.engine = engine
    try:
        resp = await api_marketing_testimonials(_FakeRequest({"namespace_id": str(namespace_id)}))
    finally:
        admin_state.engine = previous_engine

    assert resp.status_code == 200, resp.body
    import json

    body = json.loads(resp.body)
    matching = [i for i in body["items"] if i["customer_id"] == "CUST-jsonb-probe"]
    assert matching, f"probe testimonial not found in listing: {body!r}"
    assert matching[0]["consent_scope"] == {"scope": "all", "channels": ["web"]}, (
        f"consent_scope was not decoded to a dict: {matching[0]['consent_scope']!r}"
    )


@pytest.mark.asyncio
async def test_marketing_assets_listing_decodes_seo(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """Same defect, the content_assets listing."""
    await _enable_marketing(pg_pool, namespace_id)
    ref_id = f"jsonb-probe-{uuid.uuid4().hex[:8]}"

    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO content_assets (namespace_id, kind, ref_id, title, seo)
            VALUES ($1, 'case_study', $2, $3, $4::jsonb)
            """,
            namespace_id,
            ref_id,
            "JSONB decode probe asset",
            '{"meta_description": "A case study", "keywords": ["av", "boardroom"]}',
        )

    engine = NCEEngine()
    engine.pg_pool = pg_pool
    previous_engine = admin_state.engine
    admin_state.engine = engine
    try:
        resp = await api_marketing_assets(_FakeRequest({"namespace_id": str(namespace_id)}))
    finally:
        admin_state.engine = previous_engine

    assert resp.status_code == 200, resp.body
    import json

    body = json.loads(resp.body)
    matching = [i for i in body["items"] if i["ref_id"] == ref_id]
    assert matching, f"probe asset not found in listing: {body!r}"
    assert matching[0]["seo"] == {
        "meta_description": "A case study",
        "keywords": ["av", "boardroom"],
    }, f"seo was not decoded to a dict: {matching[0]['seo']!r}"
