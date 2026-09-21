"""
tests/integration/test_documents_jsonb_metadata_live.py
===========================================================
Live-Postgres regression test for nce/vertical_modules/documents/service.py's
`metadata` jsonb handling, found while building tests/integration/
test_resource_surface_subresource_write_validation_live.py -- not part of
that PR's own scope (identifier resolution), filed and fixed separately.

WHY THIS MUST BE A LIVE TEST, NOT A MOCK
-------------------------------------------
tests/unit/test_c14_documents.py already exists, and its own docstring
claims coverage of "CRUD lifecycle... multi-tenant RLS isolation...
principal tier redaction" for exactly the functions this bug lives in
(register_document, get_document, list_entity_documents). Its autouse
fixture sets admin_state.engine = None (line 51), so every test in that
file runs the in-memory fallback branch exclusively -- structurally
incapable of exercising a real asyncpg jsonb bind or decode, which is
exactly where both bugs below live. A green result from that file proves
nothing about either one. Same shape, third instance in one night: a
mocked test suite with a confident docstring claiming coverage a mock
cannot provide (the generated-controls tests' in-memory branch, #378's
write-coercion bug, and now this).

THE TWO BUGS, EXACTLY
------------------------
This pool registers no jsonb codec (confirmed by
tests/test_semantic_search_metadata_text.py's own docstring), so every
jsonb column round-trips through asyncpg as raw JSON TEXT, never a
decoded/encoded Python dict automatically. Every OTHER jsonb write in
this codebase (nce/resource_surface/comments.py's tlx_scores) already
manually json.dumps()s before binding and json.loads()s (with a
str-check) after reading. documents/service.py never adopted that
convention, on either side of the same column:

1. register_document bound `meta` (a raw Python dict) directly as the
   jsonb `$12` parameter. asyncpg's encoder requires a str for a jsonb
   parameter and raises immediately:
   asyncpg.exceptions.DataError: invalid input for query argument $12:
   {} (expected str, got dict). Every document registration through
   handle_attach_document's title/document_ref path (register_document's
   only caller besides direct callers) 500'd, for every caller, always --
   not something this test's setup happened to trigger.

2. list_entity_documents (and register_document's own RETURNING parse,
   and get_document) did `dict(r["metadata"] or {})` on that same
   string-typed column. `dict()` on a string tries to treat it as an
   iterable of key-value pairs, not parse it as JSON, and raises
   `ValueError: dictionary update sequence element #0 has length 1; 2 is
   required` for EVERY row -- including the schema's own default empty
   `'{}'::jsonb`. Confirmed live: two characters, "{" and "}", each of
   length 1, neither a valid 2-element pair.

One root cause (documents/service.py never adopted this codebase's
manual jsonb dumps/loads convention), two symptoms of it -- fixed
together via a single new `_parse_metadata()` helper (read side) and
`json.dumps(meta)` (write side), not two separate PRs.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from nce.vertical_modules.documents.service import (
    get_document,
    link_document,
    list_entity_documents,
    register_document,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_register_document_with_no_explicit_metadata_does_not_500(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """The exact crash handle_attach_document's title/document_ref
    registration path hit on every call: register_document must not raise
    asyncpg.exceptions.DataError when the caller passes no metadata (the
    ordinary case -- metadata is optional in every real caller)."""
    async with pg_pool.acquire() as conn:
        doc = await register_document(
            conn, namespace_id, "No Metadata Doc", "https://example.test/none"
        )
    assert doc.metadata == {}


@pytest.mark.asyncio
async def test_register_document_round_trips_real_metadata(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """A populated metadata dict must survive the write, not just the
    empty default -- proves the fix parses real JSON, not merely avoids
    crashing on '{}'."""
    async with pg_pool.acquire() as conn:
        doc = await register_document(
            conn,
            namespace_id,
            "Real Metadata Doc",
            "https://example.test/real",
            metadata={"reviewed": True, "tags_internal": ["a", "b"]},
        )
    assert doc.metadata == {"reviewed": True, "tags_internal": ["a", "b"]}


@pytest.mark.asyncio
async def test_get_document_does_not_raise_on_the_default_empty_metadata(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    async with pg_pool.acquire() as conn:
        doc = await register_document(conn, namespace_id, "GD Doc", "https://example.test/gd")
        fetched = await get_document(conn, namespace_id, doc.id)
    assert fetched is not None
    assert fetched.metadata == {}


@pytest.mark.asyncio
async def test_list_entity_documents_does_not_raise_on_the_default_empty_metadata(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """The exact crash confirmed live before this fix: dict() on the
    string '{}' -- every row hit this, not just populated metadata."""
    async with pg_pool.acquire() as conn:
        doc = await register_document(conn, namespace_id, "List Doc", "https://example.test/list")
        await link_document(conn, namespace_id, doc.id, "SITE", "some-real-id")
        docs = await list_entity_documents(conn, namespace_id, "SITE", "some-real-id")
    assert len(docs) == 1
    assert docs[0]["metadata"] == {}


@pytest.mark.asyncio
async def test_list_entity_documents_round_trips_real_metadata(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    async with pg_pool.acquire() as conn:
        doc = await register_document(
            conn,
            namespace_id,
            "List Real Doc",
            "https://example.test/list-real",
            metadata={"k": "v"},
        )
        await link_document(conn, namespace_id, doc.id, "SITE", "another-real-id")
        docs = await list_entity_documents(conn, namespace_id, "SITE", "another-real-id")
    assert docs[0]["metadata"] == {"k": "v"}
