"""
tests/integration/test_resource_surface_subresource_write_validation_live.py
================================================================================
Live-Postgres regression test for write-time identifier validation across
every C12 sub-resource that files by a caller-supplied identifier:
comments and tags (``handle_add_comment``, ``handle_add_tag``,
``handle_remove_tag``) and documents (``handle_attach_document``,
``handle_detach_document``), all in ``nce/resource_surface/rest.py``.
Originally scoped to comments/tags only; documents folded in once measured
to have the identical shape rather than opened as a second PR proving the
same property about the same function twice.

THE BUG
---------
Both sub-resource families store a caller-supplied identifier with no FK
to a real resource -- ``v3_cognitive_ledger``'s comment/tag entries via an
opaque ``entity_id`` string, ``document_links`` via a real but
unvalidated ``entity_id`` TEXT column. Either way, a bogus, mistyped, or
(specifically) SHADOWED identifier got a silent success that could never
be found again: confirmed live, ``GET /api/sales/contacts/{id}/comments``
returned ``{"comments": [], "count": 0}`` for a comment that genuinely
existed, filed under a different identifier for the same row.

The shadowing case matters most: for the 6 graph-primary specs with a
secondary table (CONTACT, DEVICE, PORT, RACK, CABLE, DESIGN_REQUEST), a
GET response's "id" field is that secondary table's own primary key, not
``kg_nodes.id`` (every secondary table declares its own ``id`` column,
which silently shadows kg_nodes' in the merged response -- see ``#401``
and ``OPENAPI_RESPONSE_SCHEMA_SWEEP.md``). A caller doing the ordinary
thing -- GET, then act on the sub-resource using the "id" GET just handed
back -- was silently fragmenting their own comment/tag/document-link
history. Measured, not assumed, that this stops at these two families:
neither comments/tags nor documents merge a row into any item's own GET
response (that merge-order shadowing is bounded to exactly the 6 specs
above, on the item GET path only), and no relational spec (0 of 47) has
a secondary table to shadow ``id_field`` the way kg_nodes' surrogate id
is shadowed -- the whole family is finite on both axes.

THE FIX, AND ITS SCOPE
-------------------------
``resolve_canonical_identifier`` (``rest.py``, extracted from ``#401``'s
proven resolution) is now checked before every comment/tag/document
WRITE, not before reads (``handle_list_comments``/``handle_list_tags``/
``handle_list_documents``) -- an existing orphaned comment written
before this fix must stay queryable, and a caller checking "does this
have comments" on a since-deleted or never-existed id should get an
empty list, not a 404.

Existence alone is not enough: it returns the row's CANONICAL identifier,
not a bool, and callers store that returned value, never the caller's
raw argument -- an exists-only check would accept a shadowed id but
still file the write under the wrong string, relocating the
fragmentation instead of closing it. Found this exact gap in this file's
own first version, before it shipped: the shadowed-id test failed with
count=0 against an exists-only implementation.

``document_links``' real ``ON CONFLICT (namespace_id, document_id,
entity_type, entity_id)`` constraint means a link filed earlier under a
raw shadowed string, before this fix, will no longer collide with one
filed under the resolved canonical value -- so the same document can be
linked to the same entity twice, once under each key. Not cleaned up
here, same as the orphan kg_nodes rows #401 left and the phantom
node_state rows question -- a DELETE/dedup against live tenant data is
Sindre's call regardless of standing approval.

The fix is generic across BOTH resolution shapes the resolver has to
handle, and this file proves both directions on both:
- graph-primary (CONTACT, via kg_nodes then its secondary table
  ``sales_contacts``): a genuinely bogus id is refused; the row's own
  real label AND the shadowed secondary-table id are both accepted and
  land on the same row (the actual reported bug, closed).
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


async def _insert_document(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID, title: str) -> str:
    """Insert a document row directly, bypassing handle_attach_document's
    title/document_ref registration path (register_document, documents/
    service.py) -- that path is separately, pre-existingly broken against
    real Postgres (binds a raw dict for the jsonb `metadata` column with
    no json.dumps and no codec registered on this pool, confirmed by
    tests/test_semantic_search_metadata_text.py's own docstring; every
    call omitting metadata hits it). Unrelated to identifier resolution,
    filed separately rather than fixed or worked around in production
    code here -- this helper exists so THESE tests aren't blocked by it.
    """
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO documents (namespace_id, title, document_ref) "
            "VALUES ($1, $2, $3) RETURNING id",
            namespace_id,
            title,
            "https://example.test/doc",
        )
    return str(row["id"])


async def _document_link_entity_ids(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID, document_id: str
) -> list[str]:
    """Read document_links directly, bypassing handle_list_documents ->
    list_entity_documents, which is separately, pre-existingly broken
    against real Postgres for a different reason than register_document:
    `dict(r["metadata"] or {})` on a jsonb column that comes back as a
    raw string (no codec, same root cause) raises
    `ValueError: dictionary update sequence element #0 has length 1; 2
    is required` for EVERY row, including the default empty '{}'::jsonb
    -- confirmed live, not a guess. Filed separately; this helper exists
    so identifier-resolution tests aren't blocked by an unrelated,
    pre-existing bug in a function these tests never call directly.
    """
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT entity_id FROM document_links WHERE namespace_id = $1 AND document_id = $2",
            namespace_id,
            uuid.UUID(document_id),
        )
    return [r["entity_id"] for r in rows]


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


@pytest.mark.asyncio
async def test_relational_document_attach_on_bogus_id_is_refused(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    async with _client_for(_SITES_SPEC, engine) as client:
        r = await client.post(
            f"/api/sites/sites/{uuid.uuid4()}/documents",
            json={
                "namespace_id": str(namespace_id),
                "title": "Should be refused",
                "document_ref": "https://example.test/doc",
            },
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_relational_document_attach_on_real_id_is_accepted(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """Positive control for the same reason as the tag/comment equivalents:
    a validator that rejects every valid relational identifier is only
    caught by asserting acceptance, not by the negative test above.
    """
    doc_id = await _insert_document(pg_pool, namespace_id, "Site Plan")
    async with _client_for(_SITES_SPEC, engine) as client:
        created = (
            await client.post(
                "/api/sites/sites",
                json={
                    "namespace_id": str(namespace_id),
                    "name": "Doc Site",
                    "site_type": "building",
                },
            )
        ).json()
        real_id = created["id"]

        r = await client.post(
            f"/api/sites/sites/{real_id}/documents",
            json={"namespace_id": str(namespace_id), "document_id": doc_id},
        )
        assert r.status_code == 201, f"attach against a valid relational id must succeed: {r.text}"

    entity_ids = await _document_link_entity_ids(pg_pool, namespace_id, doc_id)
    assert entity_ids == [real_id]


@pytest.mark.asyncio
async def test_graph_document_attach_via_shadowed_id_lands_on_the_real_row(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool, _seed_ownership: None
) -> None:
    """The same reproduction as the comment case, on the documents
    sub-resource: create/GET via MCP (REST's handle_get applies tier
    redaction that strips fields with no verified auth context -- see the
    comment case above), attach a document using the id GET hands back,
    and confirm it is filed under the real label, not a second bucket.
    """
    tools = build_mcp_tool_specs(_CONTACT_SPEC)
    import json as _json

    created = _json.loads(
        await tools["sales_upsert_contacts"].handler(
            engine, {"namespace_id": str(namespace_id), "name": "Fay", "email": "fay@example.com"}
        )
    )
    real_label = created["id"]
    fetched = _json.loads(
        await tools["sales_get_contacts"].handler(
            engine, {"namespace_id": str(namespace_id), "id": real_label}
        )
    )
    shadowed_id = fetched["id"]
    assert shadowed_id != real_label

    doc_id = await _insert_document(pg_pool, namespace_id, "Via shadowed id")
    async with _client_for(_CONTACT_SPEC, engine) as client:
        r = await client.post(
            f"/api/sales/contacts/{shadowed_id}/documents",
            json={"namespace_id": str(namespace_id), "document_id": doc_id},
        )
        assert r.status_code == 201, (
            f"attach via the shadowed id must resolve, not be refused: {r.text}"
        )

    entity_ids = await _document_link_entity_ids(pg_pool, namespace_id, doc_id)
    assert entity_ids == [real_label], (
        f"link must be filed under the real label, not the shadowed id: {entity_ids}"
    )


@pytest.mark.asyncio
async def test_document_detach_via_shadowed_id_still_finds_the_real_link(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool, _seed_ownership: None
) -> None:
    """The sharper failure mode for detach specifically: without
    resolving to canonical first, a caller detaching via a valid-but-
    different identifier for a link genuinely filed under the real label
    gets a false "not found" instead of the detach it asked for.
    """
    tools = build_mcp_tool_specs(_CONTACT_SPEC)
    import json as _json

    created = _json.loads(
        await tools["sales_upsert_contacts"].handler(
            engine, {"namespace_id": str(namespace_id), "name": "Gia", "email": "gia@example.com"}
        )
    )
    real_label = created["id"]
    fetched = _json.loads(
        await tools["sales_get_contacts"].handler(
            engine, {"namespace_id": str(namespace_id), "id": real_label}
        )
    )
    shadowed_id = fetched["id"]

    doc_id = await _insert_document(pg_pool, namespace_id, "To be detached")
    async with _client_for(_CONTACT_SPEC, engine) as client:
        attach = await client.post(
            f"/api/sales/contacts/{real_label}/documents",
            json={"namespace_id": str(namespace_id), "document_id": doc_id},
        )
        assert attach.status_code == 201

        detach = await client.delete(
            f"/api/sales/contacts/{shadowed_id}/documents/{doc_id}?namespace_id={namespace_id}"
        )
        assert detach.status_code == 200, (
            f"detach via the shadowed id must find the link filed under the real label: {detach.text}"
        )

    entity_ids = await _document_link_entity_ids(pg_pool, namespace_id, doc_id)
    assert entity_ids == []
