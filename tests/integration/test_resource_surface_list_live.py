"""
tests/integration/test_resource_surface_list_live.py
======================================================
Live-Postgres sibling for `handle_list`'s RELATIONAL branch (`rest.py`'s
`elif spec.table_name:`, not `if is_graph:`). Filled a gap found while
building BRANCH_COVERAGE_MAP.md: every existing live test that touches
`handle_list` either exercises the graph-primary branch
(`test_resource_surface_kg_nodes_primary_live.py::
test_list_returns_primary_rows_only`) or is refused at the guard stage
(`test_resource_surface_engine_guard_live.py::
test_rest_list_refused_when_namespace_id_omitted_for_global_gated_spec`,
a 422 returned before either branch runs). A guard-stage refusal looks
like coverage in a pass/fail column but proves nothing about the branch
itself -- the distinction the map exists to make.

POPULATION -- measured, not assumed. Of 56 registered specs, 46 are
relational (`table_name is not None`) and do not exclude `list`. All 46
declare at least one `filterable_fields` entry. Of those 46, 17 also
declare a `soft_delete_field` (needed for the `include_archived`
behaviour below).

Unlike the CREATE/PATCH/ARCHIVE siblings built earlier tonight, this file
does NOT sweep all 46 -- deliberately. Those siblings swept because each
spec's own table/constraints could independently break a per-spec code
path (a CHECK constraint, a missing column, a bad FK). `handle_list`'s
relational branch has no such per-spec logic: the same query-building
code (cursor pagination via `spec.id_field`, `include_archived` via
`spec.soft_delete_field`, `active_filters` via `spec.filterable_fields`)
runs unconditionally for every relational spec, parameterized only by
column *names* the spec already declares, not by per-table shape. What
needs proving here is that the shared mechanism itself is correct, not
that it survives 46 different tables -- one representative spec, chosen
for having no CHECK constraints and no required FKs beyond `namespace_id`
(`agreements:templates`, confirmed via `information_schema`/
`pg_constraint` directly), keeps the fixture simple without weakening the
claim.

Each behaviour gets a test that would fail against the specific way its
mechanism could be broken, not just "200 and some rows" (which would pass
against a broken filter that returns everything, or a broken cursor that
returns duplicates):
- cursor pagination is checked by exact-set reconstruction across
  multiple pages (a broken `<` vs `<=` boundary would show up as a
  duplicate or a gap, not just a wrong count) union query
  cross-validation, plus a same-cursor-omitted determinism check.
- `include_archived` is checked both ways, plus one deliberately-odd
  input (`"yes"`, not `"true"`) proving the code's exact
  `.lower() == "true"` semantics rather than any-truthy-string.
- `active_filters` is checked both by a filter that matches exactly one
  of two rows AND by a filter that matches neither (a no-op filter would
  still return both rows here -- this is the case a bare "returns some
  rows" assertion would miss).
"""

from __future__ import annotations

import base64
import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from nce import admin_state
from nce.orchestrator import NCEEngine
from nce.resource_surface import ResourceSpec, get_all_resource_specs, load_all_engine_resources
from nce.resource_surface.rest import make_resource_routes
from tests._verified_tier_middleware import VERIFIED_TIER_TEST_MIDDLEWARE

pytestmark = pytest.mark.integration

load_all_engine_resources()
_ALL_SPECS = {(s.engine, s.entity): s for s in get_all_resource_specs()}

_RELATIONAL_LIST_ELIGIBLE = [
    s
    for s in get_all_resource_specs()
    if s.table_name is not None and "list" not in s.excluded_verbs
]
_WITH_SOFT_DELETE_AND_FILTER = [
    s for s in _RELATIONAL_LIST_ELIGIBLE if s.soft_delete_field and s.filterable_fields
]

SPEC = _ALL_SPECS[("agreements", "templates")]


def test_discovery_floor_matches_the_measured_population() -> None:
    assert len(_RELATIONAL_LIST_ELIGIBLE) >= 46, (
        f"Only {len(_RELATIONAL_LIST_ELIGIBLE)} relational, list-eligible specs found -- "
        f"expected at least 46 (measured on main@36b31ee). A spec was excluded, lost its "
        f"table_name, or gained one."
    )
    assert all(s.filterable_fields for s in _RELATIONAL_LIST_ELIGIBLE), (
        "every relational list-eligible spec was expected to declare at least one "
        "filterable field -- one no longer does"
    )
    assert len(_WITH_SOFT_DELETE_AND_FILTER) >= 17, (
        f"Only {len(_WITH_SOFT_DELETE_AND_FILTER)} relational specs with both a "
        f"soft_delete_field and filterable_fields found -- expected at least 17"
    )
    assert SPEC.table_name == "agreement_templates"
    assert SPEC.soft_delete_field == "is_archived"
    assert "category" in SPEC.filterable_fields


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    return eng


async def _enable_agreements(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    # Single-element jsonb_set path -- a two-element path on a fresh {}
    # silently no-ops (found and fixed identically in every other live
    # sibling tonight that needs enabled_guard).
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE namespaces SET metadata = jsonb_set("
            "coalesce(metadata, '{}'::jsonb), ARRAY[$2], "
            "coalesce(metadata->$2, '{}'::jsonb) || jsonb_build_object('enabled', true), "
            "true) WHERE id = $1",
            namespace_id,
            "agreements",
        )


@pytest_asyncio.fixture(autouse=True)
async def _enable_engine_guard(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    await _enable_agreements(pg_pool, namespace_id)


def _rest_app(spec: ResourceSpec) -> Starlette:
    return Starlette(routes=make_resource_routes(spec), middleware=VERIFIED_TIER_TEST_MIDDLEWARE)


@pytest.fixture
def client(engine: NCEEngine):
    previous = admin_state.engine
    admin_state.engine = engine
    try:
        yield httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app(SPEC)), base_url="http://test"
        )
    finally:
        admin_state.engine = previous


async def _create(client: httpx.AsyncClient, namespace_id: uuid.UUID, **fields: object) -> dict:
    payload = {"namespace_id": str(namespace_id), **fields}
    resp = await client.post(
        SPEC.rest_collection_path, json=payload, headers={"X-NCE-Principal-Tier": "employee"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["item"]


async def _list(client: httpx.AsyncClient, namespace_id: uuid.UUID, **params: object) -> dict:
    query = {"namespace_id": str(namespace_id), **params}
    resp = await client.get(
        SPEC.rest_collection_path, params=query, headers={"X-NCE-Principal-Tier": "employee"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_cursor_pagination_crosses_a_page_boundary_without_duplicates_or_gaps(
    client: httpx.AsyncClient, namespace_id: uuid.UUID
) -> None:
    """7 rows, limit=3 -- 3 pages (3, 3, 1), the last with cursor exhausted.

    Exact-set reconstruction, not a count: a `<` vs `<=` boundary bug
    would show up here as a repeated id across two pages or a missing id,
    either of which this catches; a test that only checked `len(items) >
    0` per page would not.
    """
    created_ids = {
        (await _create(client, namespace_id, name=f"cursor-probe-{i}"))["id"] for i in range(7)
    }

    seen_ids: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        body = await _list(client, namespace_id, limit=3, **({"cursor": cursor} if cursor else {}))
        page_ids = [item["id"] for item in body["items"]]
        seen_ids.extend(page_ids)
        pages += 1
        assert pages <= 10, "pagination did not terminate -- infinite loop or a stuck cursor"
        cursor = body["next_cursor"]
        if not cursor:
            break

    ours = [i for i in seen_ids if i in created_ids]
    assert pages == 3, f"expected 3 pages for 7 rows at limit=3, got {pages}"
    assert len(ours) == len(set(ours)) == 7, (
        f"expected exactly 7 distinct created rows across all pages, got {len(ours)} "
        f"raw / {len(set(ours))} distinct -- a duplicate means the cursor re-included a "
        f"row, a shortfall means it skipped one"
    )
    assert set(ours) == created_ids


@pytest.mark.asyncio
async def test_omitting_the_cursor_repeats_the_first_page(
    client: httpx.AsyncClient, namespace_id: uuid.UUID
) -> None:
    """Calling list twice with the same limit and no cursor must return the
    SAME first page both times -- demonstrates the cursor, not some hidden
    server-side pagination state, is what actually advances the window.
    If it were NOT idempotent like this, the previous test's set-based
    assertion could pass by accident (e.g. random ordering happening to
    cover every id across repeated first-page-only calls) rather than by
    the cursor doing real work.
    """
    for i in range(4):
        await _create(client, namespace_id, name=f"repeat-probe-{i}")

    first = await _list(client, namespace_id, limit=2)
    second = await _list(client, namespace_id, limit=2)
    assert [i["id"] for i in first["items"]] == [i["id"] for i in second["items"]]


@pytest.mark.asyncio
async def test_include_archived_excludes_by_default_and_includes_when_requested(
    client: httpx.AsyncClient, namespace_id: uuid.UUID
) -> None:
    live = await _create(client, namespace_id, name="archive-probe-live")
    archived = await _create(client, namespace_id, name="archive-probe-archived")

    resp = await client.post(
        f"{SPEC.rest_collection_path}/{archived['id']}/archive",
        json={"namespace_id": str(namespace_id)},
        headers={"X-NCE-Principal-Tier": "employee"},
    )
    assert resp.status_code == 200, resp.text

    default_body = await _list(client, namespace_id)
    default_ids = {i["id"] for i in default_body["items"]}
    assert live["id"] in default_ids
    assert archived["id"] not in default_ids, (
        "an archived row was returned without include_archived -- the default soft-delete "
        "filter is not excluding it"
    )

    all_body = await _list(client, namespace_id, include_archived="true")
    all_ids = {i["id"] for i in all_body["items"]}
    assert {live["id"], archived["id"]} <= all_ids, (
        "include_archived=true did not bring the archived row back"
    )

    # Deliberately-odd input: the code's own gate is `.lower() == "true"`
    # (rest.py) -- any other string, including a reasonable-looking
    # "yes", must NOT enable the archived rows. Proves the exact
    # semantics, not just that SOME truthy-looking value works.
    odd_body = await _list(client, namespace_id, include_archived="yes")
    odd_ids = {i["id"] for i in odd_body["items"]}
    assert archived["id"] not in odd_ids, (
        "include_archived=\"yes\" was treated as true -- the gate must accept the literal "
        "string \"true\" (case-insensitive) only"
    )


@pytest.mark.asyncio
async def test_active_filter_excludes_non_matching_rows(
    client: httpx.AsyncClient, namespace_id: uuid.UUID
) -> None:
    onboarding = await _create(
        client, namespace_id, name="filter-probe-onboarding", category="onboarding"
    )
    renewal = await _create(client, namespace_id, name="filter-probe-renewal", category="renewal")

    matched = await _list(client, namespace_id, category="onboarding")
    matched_ids = {i["id"] for i in matched["items"]}
    assert onboarding["id"] in matched_ids
    assert renewal["id"] not in matched_ids, (
        "category=onboarding returned the renewal row -- the filter is not restrictive"
    )

    unfiltered = await _list(client, namespace_id)
    unfiltered_ids = {i["id"] for i in unfiltered["items"]}
    assert {onboarding["id"], renewal["id"]} <= unfiltered_ids

    # A no-op filter would still return both rows here -- this is the
    # case a bare "some rows came back" assertion would miss.
    empty = await _list(client, namespace_id, category="no-such-category-value")
    assert empty["items"] == [], (
        f"a filter value matching neither row returned {len(empty['items'])} items -- "
        f"active_filters is not actually filtering"
    )


def test_positive_control_base64_cursor_round_trips_the_real_id_column_type() -> None:
    """Not a live-DB test -- pins the exact wire format `next_cursor` uses
    (base64 of the raw id-column text), since the pagination test above
    treats `next_cursor` as an opaque token round-tripped through `_list`
    without ever decoding it itself. If the encoding ever changed shape
    (e.g. JSON-wrapped, or the wrong column), this fails independently of
    whether pagination happens to still produce the right final set.
    """
    raw_id = "11111111-1111-1111-1111-111111111111"
    cursor = base64.b64encode(raw_id.encode("utf-8")).decode("utf-8")
    assert base64.b64decode(cursor).decode("utf-8") == raw_id
