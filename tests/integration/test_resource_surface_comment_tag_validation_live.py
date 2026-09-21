"""
tests/integration/test_resource_surface_comment_tag_validation_live.py
==========================================================================
Live-Postgres regression test for write-time identifier validation on the
comment/tag sub-resources (``handle_add_comment``, ``handle_add_tag``,
``handle_remove_tag`` in ``nce/resource_surface/rest.py``).

THE BUG
---------
``v3_cognitive_ledger``'s comment/tag entries have no FK to a real
resource -- ``entity_id`` was, before this fix, an unvalidated opaque
string. A caller who wrote a comment against a bogus, mistyped, or
(specifically) SHADOWED identifier got a silent 201 that could never be
found again: confirmed live, ``GET /api/sales/contacts/{id}/comments``
returned ``{"comments": [], "count": 0}`` for a comment that genuinely
existed, filed under the real identifier instead.

The shadowing case matters most: for the 6 graph-primary specs with a
secondary table (CONTACT, DEVICE, PORT, RACK, CABLE, DESIGN_REQUEST), a
GET response's "id" field is that secondary table's own primary key, not
``kg_nodes.id`` (every secondary table declares its own ``id`` column,
which silently shadows kg_nodes' in the merged response -- see ``#401``
and ``OPENAPI_RESPONSE_SCHEMA_SWEEP.md``). A caller doing the ordinary
thing -- GET, then comment using the "id" GET just handed back -- was
silently fragmenting their own comment history.

THE FIX, AND ITS SCOPE
-------------------------
``resource_identifier_exists`` (``rest.py``, extracted from ``#401``'s
proven resolution) is now checked before every comment/tag WRITE
(``handle_add_comment``, ``handle_add_tag``, ``handle_remove_tag``), not
before reads (``handle_list_comments``/``handle_list_tags``) -- an
existing orphaned comment written before this fix must stay queryable,
and a caller checking "does this have comments" on a since-deleted or
never-existed id should get an empty list, not a 404.

The fix is generic across BOTH resolution shapes the resolver has to
handle, and this file proves both directions on both:
- graph-primary (CONTACT, via kg_nodes then its secondary table
  ``sales_contacts``): a genuinely bogus id is refused; the row's own
  real label AND the shadowed secondary-table id it) are both accepted
  (the actual reported bug, closed).
- relational (SITES, no secondary table at all): a genuinely bogus id
  is refused; the row's own real, correct id is accepted. This second
  case exists specifically because the resolver's relational branch is
  REUSED code (already correct for reading/targeting a row) being
  exercised in a REFUSING role for the first time -- a positive test
  is the only thing that would catch a validator that rejects every
  valid relational identifier across the 47 non-graph specs.
"""

from __future__ import annotations

import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from nce import admin_state
from nce.auth import set_namespace_context
from nce.engine_registry import populate_engine_modules
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.orchestrator import NCEEngine
from nce.resource_surface import get_all_resource_specs, load_all_engine_resources
from nce.resource_surface.mcp import build_mcp_tool_specs
from nce.resource_surface.rest import make_resource_routes

pytestmark = pytest.mark.integration

load_all_engine_resources()
_SPECS = {(s.engine, s.entity): s for s in get_all_resource_specs()}
_CONTACT_SPEC = _SPECS[("sales", "contacts")]
_SITES_SPEC = _SPECS[("sites", "sites")]


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)
    return eng


@pytest_asyncio.fixture
async def _seed_ownership(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    """Only the graph-primary (CONTACT) tests need this -- assert_owner's
    deny-by-default gate lives inside handle_create's `is_graph` branch
    only, same as every other graph-primary live test tonight."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)


def _client_for(spec, engine: NCEEngine):
    admin_state.engine = engine
    app = Starlette(routes=make_resource_routes(spec))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_graph_comment_on_bogus_id_is_refused(
    engine: NCEEngine, namespace_id: uuid.UUID, _seed_ownership: None
) -> None:
    async with _client_for(_CONTACT_SPEC, engine) as client:
        r = await client.post(
            f"/api/sales/contacts/{uuid.uuid4()}/comments",
            json={"namespace_id": str(namespace_id), "comment": "should be refused"},
        )
    assert r.status_code == 404, (
        f"a comment against an identifier that resolves to nothing must be refused, got {r.text}"
    )


@pytest.mark.asyncio
async def test_graph_comment_via_real_label_is_accepted(
    engine: NCEEngine, namespace_id: uuid.UUID, _seed_ownership: None
) -> None:
    async with _client_for(_CONTACT_SPEC, engine) as client:
        created = (
            await client.post(
                "/api/sales/contacts",
                json={
                    "namespace_id": str(namespace_id),
                    "name": "Dana",
                    "email": "dana@example.com",
                },
            )
        ).json()
        real_label = created["id"]

        r = await client.post(
            f"/api/sales/contacts/{real_label}/comments",
            json={"namespace_id": str(namespace_id), "comment": "hello"},
        )
        assert r.status_code == 201, (
            f"a comment against the row's own real label must succeed: {r.text}"
        )


@pytest.mark.asyncio
async def test_graph_comment_via_shadowed_get_id_lands_on_the_real_row(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool, _seed_ownership: None
) -> None:
    """The exact reported bug: create, GET (whose "id" is sales_contacts'
    own primary key, not kg_nodes.id), then comment using that value. Must
    be ACCEPTED (this is a real row, just addressed by its shadowed id --
    refusing it would be the validator being too strict) and the comment
    must be visible under the real label, not lost in a second bucket.

    Create/GET go through the MCP tools here, not REST -- REST's
    handle_get applies tier redaction (nce/resource_surface/rest.py),
    which strips every field for a request with no verified auth context
    (as this test's bare ASGI client is). MCP has no such redaction (its
    own module docstring says so), the same reason #401's own test file
    used TOOL_REGISTRY for create/GET. What THIS test is actually
    exercising -- the comment endpoint's write-time validation -- is
    REST-only and untouched by that distinction.
    """
    tools = build_mcp_tool_specs(_CONTACT_SPEC)
    import json as _json

    created = _json.loads(
        await tools["sales_upsert_contacts"].handler(
            engine, {"namespace_id": str(namespace_id), "name": "Eli", "email": "eli@example.com"}
        )
    )
    real_label = created["id"]
    fetched = _json.loads(
        await tools["sales_get_contacts"].handler(
            engine, {"namespace_id": str(namespace_id), "id": real_label}
        )
    )
    shadowed_id = fetched["id"]
    assert shadowed_id != real_label, (
        "fixture assumption broken: kg_nodes.id and sales_contacts.id must differ "
        "for this reproduction to mean anything"
    )

    async with _client_for(_CONTACT_SPEC, engine) as client:
        r = await client.post(
            f"/api/sales/contacts/{shadowed_id}/comments",
            json={"namespace_id": str(namespace_id), "comment": "via shadowed id"},
        )
        assert r.status_code == 201, (
            f"a comment via the shadowed id must resolve, not be refused: {r.text}"
        )

        comments = (
            await client.get(
                f"/api/sales/contacts/{real_label}/comments?namespace_id={namespace_id}"
            )
        ).json()
        assert comments["count"] == 1
        assert comments["comments"][0]["comment"] == "via shadowed id"


@pytest.mark.asyncio
async def test_reads_are_unchanged_orphan_comment_stays_queryable(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """A comment written under a bogus entity_id BEFORE this fix existed
    (or by any future writer that bypasses this route) must still be
    readable -- validation is write-time only, never read-time.
    """
    from nce.resource_surface.comments import append_entity_comment

    async with pg_pool.acquire() as conn:
        await append_entity_comment(
            conn, namespace_id, _CONTACT_SPEC, "orphan-entity-id", "pre-existing orphan", "operator"
        )

    async with _client_for(_CONTACT_SPEC, engine) as client:
        r = await client.get(
            f"/api/sales/contacts/orphan-entity-id/comments?namespace_id={namespace_id}"
        )
    assert r.status_code == 200, "reads must never be blocked by the write-time validator"
    assert r.json()["count"] == 1
    assert r.json()["comments"][0]["comment"] == "pre-existing orphan"


@pytest.mark.asyncio
async def test_relational_comment_on_bogus_id_is_refused(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    async with _client_for(_SITES_SPEC, engine) as client:
        r = await client.post(
            f"/api/sites/sites/{uuid.uuid4()}/comments",
            json={"namespace_id": str(namespace_id), "comment": "should be refused"},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_relational_comment_via_real_id_is_accepted(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """The positive control the ruling specifically asked for: the
    relational branch of resource_identifier_exists is REUSED code being
    exercised in a refusing role for the first time. A validator that
    rejects every genuinely valid relational identifier would only be
    caught by a test that asserts acceptance, not by the negative test
    above.
    """
    async with _client_for(_SITES_SPEC, engine) as client:
        created = (
            await client.post(
                "/api/sites/sites",
                json={
                    "namespace_id": str(namespace_id),
                    "name": "Real Site",
                    "site_type": "building",
                },
            )
        ).json()
        real_id = created["id"]

        r = await client.post(
            f"/api/sites/sites/{real_id}/comments",
            json={"namespace_id": str(namespace_id), "comment": "hello from a relational spec"},
        )
    assert r.status_code == 201, (
        f"a comment against a valid relational spec's own real id must succeed: {r.text}"
    )


@pytest.mark.asyncio
async def test_relational_tag_add_and_remove_on_real_id_are_accepted(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    async with _client_for(_SITES_SPEC, engine) as client:
        created = (
            await client.post(
                "/api/sites/sites",
                json={
                    "namespace_id": str(namespace_id),
                    "name": "Tag Site",
                    "site_type": "building",
                },
            )
        ).json()
        real_id = created["id"]

        add = await client.post(
            f"/api/sites/sites/{real_id}/tags",
            json={"namespace_id": str(namespace_id), "tag": "priority"},
        )
        assert add.status_code == 200, f"tag add on a valid id must succeed: {add.text}"
        assert "priority" in add.json()["tags"]

        remove = await client.delete(
            f"/api/sites/sites/{real_id}/tags/priority?namespace_id={namespace_id}"
        )
        assert remove.status_code == 200, f"tag remove on a valid id must succeed: {remove.text}"
        assert "priority" not in remove.json()["tags"]


@pytest.mark.asyncio
async def test_relational_tag_add_on_bogus_id_is_refused(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    async with _client_for(_SITES_SPEC, engine) as client:
        r = await client.post(
            f"/api/sites/sites/{uuid.uuid4()}/tags",
            json={"namespace_id": str(namespace_id), "tag": "priority"},
        )
    assert r.status_code == 404
