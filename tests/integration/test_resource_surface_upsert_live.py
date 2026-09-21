"""
tests/integration/test_resource_surface_upsert_live.py
========================================================
Live-Postgres regression test for the C12 resource-surface generic
``handle_upsert`` datetime/version bug (found 2026-09-19 building H-9's
Golden Thread v2 extension, fixed same day per ML-orch's Q-46 ruling).

WHY THIS MUST BE A LIVE TEST, NOT A MOCK
-----------------------------------------
Every existing unit test for a C12 resource (``test_agreements_resources.py``,
``test_sites_address_registry.py``, etc.) mocks the connection. A mock accepts
a Python ``str`` wherever a real ``datetime`` is expected; only asyncpg's real
binary protocol against a real ``timestamptz`` column enforces the type. The
bug survived because the code and its proof were tested apart -- a mocked
test for this fix would be worthless by construction, per the same reasoning
that keeps ``tests/integration/test_golden_thread.py`` a live suite.

THE BUG, EXACTLY
-----------------
``nce/resource_surface/mcp.py::handle_upsert`` built ``data[spec.version_field]``
(``version_field`` defaults to ``"updated_at"`` for every ``ResourceSpec`` --
26 of 30 registered specs as of this wave) from
``datetime.now(timezone.utc).isoformat()`` -- a ``str`` -- then bound it
straight into a parameterized INSERT/UPDATE against the real ``timestamptz``
column. asyncpg's binary codec for ``timestamptz`` requires an actual
``datetime.datetime`` instance and raises ``asyncpg.exceptions.DataError`` on
a ``str``, every time, for every tenant-scoped C12 resource on the real
(non-in-memory) storage path. Every real POST to any such resource's
``upsert`` verb against a live Postgres-backed deployment was failing this
way before this fix.

A SECOND DEFECT, REAL BUT NARROWER THAN FIRST BELIEVED
--------------------------------------------------------
``handle_upsert`` also uses ``spec.version_field`` for optimistic concurrency
(``expected_version``) and returns it to the caller as ``now.isoformat()`` (a
``"T"``-separated string). A first draft of this fix (and this docstring)
claimed that binding a real ``datetime`` for the write, without also fixing
the comparison, would make a correct ``expected_version`` fail as a spurious
conflict on EVERY upsert against a real Postgres-backed resource. **That claim
was wrong, caught by a peer review that then verified itself by literally
removing the fix and re-running the test:** ``row_to_dict`` (imported from
``nce.resource_surface.rest``) already renders every column -- including
``version_field`` -- through ``serialize_val``, which calls ``.isoformat()``
on a ``datetime``. So ``existing = row_to_dict(existing_row)`` on the
real-Postgres path already hands the comparison a string, and a bare
``str()`` on that string would have worked exactly as well;
``test_upsert_then_correct_expected_version_succeeds`` below still passes
with ``_version_str`` removed, which means it was never testing what its
name claimed on the Postgres path.

The mismatch is real, but only on the **in-memory fallback** path: this
fix's ``data[spec.version_field] = now`` (a real ``datetime``, needed for the
Postgres bind) also flows into ``mem[item_id] = dict(data)`` with no
serialization step, so a resource never backed by a real Postgres pool would
compare ``str(a_datetime)`` (space-separated) against the client's
remembered ``isoformat()`` value ("T"-separated) and reject a correct
``expected_version`` forever.
``test_upsert_in_memory_path_correct_expected_version_succeeds`` below is the
one that actually exercises this -- constructed with no ``pg_pool`` at all --
and is the one that was mutation-checked (removing ``_version_str`` makes
exactly this test fail, and no other).

THE SAME DEFECT, TWICE MORE, IN THE OTHER GENERATED SURFACE
--------------------------------------------------------------
C12 generates TWO backends per resource: the MCP tool handlers
(``nce/resource_surface/mcp.py``, fixed above) and the REST routes
(``nce/resource_surface/rest.py``). ML-orch reviewed the MCP fix, went
looking for the same shape elsewhere, and found ``rest.py`` had it worse:

* ``handle_create`` and ``handle_patch`` had the identical
  string-instead-of-datetime bug for ``version_field``.
* ``handle_create`` additionally set ``data["version"] = now_iso``
  *unconditionally*, and that dict became the column list for a raw
  ``INSERT INTO <table> (...)`` -- no C12 table has a ``version`` column
  (checked: sites, assets, documents, legal_entities, notifications,
  product_catalog, contractor_profiles all lack one), so **every real REST
  ``POST`` to any C12 resource raised ``UndefinedColumnError`` before the
  datetime type error was ever reached.** The generated REST create path had
  never worked against a real Postgres, for any resource.
* Fixing the type binding alone reproduced a THIRD defect this investigation
  found: the in-memory (no ``pg_pool``) fallback for both handlers returns
  its response dict unserialized. Once ``version_field`` holds a real
  ``datetime`` instead of a string, Starlette's plain ``JSONResponse`` cannot
  encode it (``TypeError: Object of type datetime is not JSON serializable``)
  -- reproduced live via a direct ASGI call before being caught. Both
  handlers now wrap their response dict in ``serialize_val`` (the same
  helper the real-Postgres path already gets via ``row_to_dict``).
* ``handle_bulk`` was checked and is clean: its own ``data`` dict never sets
  ``version_field`` at all, built only from ``spec.writable_fields`` plus
  ``id_field``/``namespace_id``.

Extended in the same PR rather than opened separately, per ML-orch's
reasoning: splitting this would ship a half-fixed surface where the MCP
tools work and the REST routes still crash -- the exact asymmetry (fixed
here, proven there) that let the original bug survive undetected.
"""

from __future__ import annotations

import json
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
from nce.resource_surface.rest import make_resource_routes
from nce.tool_registry import TOOL_REGISTRY

pytestmark = pytest.mark.integration


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)
    return eng


@pytest.mark.asyncio
async def test_upsert_insert_binds_a_real_datetime_not_a_string(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """The exact crash this fix resolves: INSERT must not raise DataError."""
    result = json.loads(
        await TOOL_REGISTRY["sites_upsert_sites"].handler(
            engine,
            {"namespace_id": str(namespace_id), "name": "GT Live Site", "site_type": "building"},
        )
    )
    assert result["status"] == "ok", f"upsert INSERT failed: {result}"

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, updated_at FROM sites WHERE id = $1", uuid.UUID(result["id"])
        )
    assert row is not None, "INSERT reported ok but no row was written"
    assert row["name"] == "GT Live Site"
    # asyncpg decodes timestamptz to a real datetime; this is the type the
    # broken code could never successfully write in the first place.
    import datetime

    assert isinstance(row["updated_at"], datetime.datetime)


@pytest.mark.asyncio
async def test_upsert_then_correct_expected_version_succeeds(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """The version the client is handed back must be the version the next
    upsert can successfully present as expected_version, against a real
    Postgres-backed resource. Passes with or without ``_version_str`` (see
    the module docstring) -- ``row_to_dict`` already normalises the
    comparison on this path. The mismatch ``_version_str`` actually guards
    against is exercised by
    ``test_upsert_in_memory_path_correct_expected_version_succeeds`` below,
    not by this test.
    """
    r1 = json.loads(
        await TOOL_REGISTRY["sites_upsert_sites"].handler(
            engine,
            {"namespace_id": str(namespace_id), "name": "Version Site", "site_type": "building"},
        )
    )
    assert r1["status"] == "ok"

    r2 = json.loads(
        await TOOL_REGISTRY["sites_upsert_sites"].handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": r1["id"],
                "name": "Version Site Renamed",
                "expected_version": r1["version"],
            },
        )
    )
    assert r2["status"] == "ok", (
        f"A correct expected_version was rejected as a conflict: {r2}. This is "
        "the silent-concurrency-failure shape a datetime-binding-only fix "
        "introduces: the write succeeds but every optimistic-concurrency "
        "check on this resource then fails forever."
    )
    assert r2["version"] != r1["version"]


@pytest.mark.asyncio
async def test_positive_control_stale_expected_version_is_still_rejected(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """Standing positive control: proves the fix did not silently disable
    optimistic concurrency altogether to make the test above pass -- a stale
    version must still be a real, reported 409.
    """
    r1 = json.loads(
        await TOOL_REGISTRY["sites_upsert_sites"].handler(
            engine,
            {"namespace_id": str(namespace_id), "name": "Stale Site", "site_type": "building"},
        )
    )
    r2 = json.loads(
        await TOOL_REGISTRY["sites_upsert_sites"].handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": r1["id"],
                "name": "Stale Site Renamed Once",
                "expected_version": r1["version"],
            },
        )
    )
    assert r2["status"] == "ok"

    r3 = json.loads(
        await TOOL_REGISTRY["sites_upsert_sites"].handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": r1["id"],
                "name": "Stale Site Renamed Twice",
                "expected_version": r1["version"],  # stale: r2 already moved it on
            },
        )
    )
    assert r3.get("reason") == "version_conflict", (
        f"A genuinely stale expected_version was NOT rejected: {r3} -- the "
        "concurrency check is vacuous, not merely fixed."
    )
    assert r3.get("status_code") == 409


@pytest.mark.asyncio
async def test_upsert_in_memory_path_correct_expected_version_succeeds() -> None:
    """The mismatch _version_str actually guards against, isolated to the ONE
    path where it is real.

    A peer review challenged (correctly) that the PG-backed round-trip test
    above proves nothing about _version_str: `row_to_dict` already renders a
    real-Postgres datetime through `.isoformat()` before the comparison ever
    runs, so `str(existing.get(...))` alone would have passed that test too --
    confirmed by literally removing `_version_str` and re-running it (it still
    passed). The in-memory fallback is different: `mem[item_id] = dict(data)`
    stores handle_upsert's real `datetime` object with no serialization step,
    so `str(a_datetime)` (space-separated) really does diverge from the
    isoformat() ("T"-separated) string the client was handed. This test uses
    an engine with NO pg_pool -- the in-memory branch -- and needs no live
    Postgres at all.
    """
    engine_no_pool = NCEEngine()
    populate_engine_modules(engine_no_pool)
    ns_id = str(uuid.uuid4())

    r1 = json.loads(
        await TOOL_REGISTRY["sites_upsert_sites"].handler(
            engine_no_pool,
            {"namespace_id": ns_id, "name": "InMemory Version Site", "site_type": "building"},
        )
    )
    assert r1["status"] == "ok"

    r2 = json.loads(
        await TOOL_REGISTRY["sites_upsert_sites"].handler(
            engine_no_pool,
            {
                "namespace_id": ns_id,
                "id": r1["id"],
                "name": "InMemory Version Site Renamed",
                "expected_version": r1["version"],
            },
        )
    )
    assert r2["status"] == "ok", (
        f"A correct expected_version was rejected on the in-memory path: {r2} -- "
        "this is the real mismatch _version_str exists to prevent."
    )


def _sites_rest_app() -> Starlette:
    load_all_engine_resources()
    site_spec = next(s for s in get_all_resource_specs() if s.engine == "sites")
    return Starlette(routes=make_resource_routes(site_spec))


@pytest.fixture
def sites_rest_client(engine: NCEEngine):
    """A real ASGI client for the generated sites REST routes, wired to the
    same live engine as the other tests in this file. ``admin_state.engine``
    is what the REST handlers actually read (not a fixture-injected engine),
    so it is set for the duration of the test and restored after.
    """
    previous = admin_state.engine
    admin_state.engine = engine
    try:
        yield httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_sites_rest_app()), base_url="http://test"
        )
    finally:
        admin_state.engine = previous


@pytest.mark.asyncio
async def test_rest_create_does_not_raise_undefined_column(
    sites_rest_client: httpx.AsyncClient, namespace_id: uuid.UUID
) -> None:
    """The exact crash this fix resolves: POST must not raise UndefinedColumnError
    for a bogus 'version' column, and must not raise DataError for the
    version_field datetime either.
    """
    async with sites_rest_client as client:
        r = await client.post(
            "/api/sites/sites",
            json={"namespace_id": str(namespace_id), "name": "REST Site", "site_type": "building"},
        )
    assert r.status_code == 201, f"REST create failed: {r.text}"
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body, "version must still be present in the response, just not in the row"


@pytest.mark.asyncio
async def test_rest_patch_then_correct_expected_version_succeeds(
    sites_rest_client: httpx.AsyncClient, namespace_id: uuid.UUID
) -> None:
    """REST equivalent of the MCP round-trip test above -- the version handed
    back by POST must be usable as expected_version on the next PATCH.
    """
    async with sites_rest_client as client:
        r1 = await client.post(
            "/api/sites/sites",
            json={
                "namespace_id": str(namespace_id),
                "name": "REST Version Site",
                "site_type": "building",
            },
        )
        assert r1.status_code == 201, r1.text
        item_id = r1.json()["id"]
        version1 = r1.json()["version"]

        r2 = await client.patch(
            f"/api/sites/sites/{item_id}",
            json={
                "namespace_id": str(namespace_id),
                "name": "REST Version Site Renamed",
                "expected_version": version1,
            },
        )
    assert r2.status_code == 200, (
        f"A correct expected_version was rejected: {r2.text} -- the same "
        "silent-concurrency-failure shape as the MCP path."
    )


@pytest.mark.asyncio
async def test_positive_control_rest_stale_expected_version_is_still_rejected(
    sites_rest_client: httpx.AsyncClient, namespace_id: uuid.UUID
) -> None:
    """REST equivalent of the MCP positive control -- a genuinely stale
    expected_version must still be a real 409, proving concurrency control
    was fixed, not disabled.
    """
    async with sites_rest_client as client:
        r1 = await client.post(
            "/api/sites/sites",
            json={
                "namespace_id": str(namespace_id),
                "name": "REST Stale Site",
                "site_type": "building",
            },
        )
        item_id = r1.json()["id"]
        version1 = r1.json()["version"]

        r2 = await client.patch(
            f"/api/sites/sites/{item_id}",
            json={
                "namespace_id": str(namespace_id),
                "name": "REST Stale Site Renamed Once",
                "expected_version": version1,
            },
        )
        assert r2.status_code == 200, r2.text

        r3 = await client.patch(
            f"/api/sites/sites/{item_id}",
            json={
                "namespace_id": str(namespace_id),
                "name": "REST Stale Site Renamed Twice",
                "expected_version": version1,  # stale: r2 already moved it on
            },
        )
    assert r3.status_code == 409, (
        f"A genuinely stale expected_version was NOT rejected over REST: {r3.text}"
    )


# ==============================================================================
# A FOURTH DEFECT, DIFFERENT SHAPE, FOUND 2026-09-21 -- handle_upsert's graph
# branch, not its relational one
# ==============================================================================
#
# The three defects above are all in handle_upsert's RELATIONAL branch
# (`elif spec.table_name:`). This one is in the GRAPH branch (`if is_graph:`),
# the ~9 kg_nodes-primary specs (CONTACT, DEVICE, PORT, RACK, CABLE,
# DESIGN_REQUEST, PROJECT_PROJECT, FUNCTIONAL_LOCATION, DESIGN).
#
# THE BUG, EXACTLY
# ------------------
# Before this fix, the "does this row already exist" lookup was
# `WHERE entity_type = $1 AND namespace_id = $2 AND label = $3` -- strict on
# `label` only. `rest.py`'s equivalent lookups (`handle_get`, `handle_patch`)
# are deliberately lenient: `(label = $3 OR id::text = $3)`, because a GET
# response hands the caller BOTH kg_nodes' real identifying column (`label`)
# and its surrogate UUID (`id`) under keys that look equally plausible as
# "the id" to pass back. The ordinary sequence -- upsert (create), GET,
# upsert again with the `id` GET just handed back, intending an update --
# supplied the surrogate UUID as `label` on the second upsert. The strict
# lookup found nothing, so the code did not error; it INSERTed a brand-new,
# disconnected kg_nodes row keyed by that UUID string as its `label`, and
# reported `"status": "ok"`. The original row was never touched. No error
# anywhere, and the response looked identical to a successful update.
# Confirmed live against a real database before this fix, using exactly the
# `sales:contacts` sequence the test below reproduces.
#
# THE FIX
# ---------
# The existing-row lookup is now lenient, matching rest.py:
# `(label = $3 OR id::text = $3)`. When it finds a row (by either value),
# `node_label` is reassigned to that row's REAL `label` before the
# INSERT ... ON CONFLICT (label, namespace_id) -- so the write always targets
# the row the caller meant, never a phantom keyed by whatever string was
# passed as "id". When the lookup finds nothing, `node_label` is left as the
# caller-supplied value: a genuinely new label still creates a fresh row,
# unchanged from before this fix.


@pytest_asyncio.fixture
async def _seed_ownership_for_graph_upsert(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    """Graph-primary writes are the one path in the generated surface gated
    by assert_owner (deny-by-default) -- not needed by this file's other,
    relational-spec tests, so scoped to only the tests below rather than
    made autouse for the whole file."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)


@pytest.mark.asyncio
async def test_upsert_with_get_returned_id_updates_the_same_row_not_a_duplicate(
    engine: NCEEngine,
    namespace_id: uuid.UUID,
    pg_pool: asyncpg.Pool,
    _seed_ownership_for_graph_upsert: None,
) -> None:
    """The exact reproduction that found this bug: create, GET, upsert again
    with GET's own "id" field. Asserting the response is not enough -- the
    old code also returned "status": "ok" while corrupting data. The only
    assertion that could have caught it is the row COUNT.
    """
    r1 = json.loads(
        await TOOL_REGISTRY["sales_upsert_contacts"].handler(
            engine,
            {"namespace_id": str(namespace_id), "name": "Alice", "email": "alice@example.com"},
        )
    )
    assert r1["status"] == "ok", f"create failed: {r1}"
    real_label = r1["id"]

    r2 = json.loads(
        await TOOL_REGISTRY["sales_get_contacts"].handler(
            engine, {"namespace_id": str(namespace_id), "id": real_label}
        )
    )
    assert r2["email"] == "alice@example.com"
    surrogate_id = r2["id"]
    assert surrogate_id != real_label, (
        "test fixture assumption broken: kg_nodes.id and .label must differ "
        "for this reproduction to mean anything"
    )

    r3 = json.loads(
        await TOOL_REGISTRY["sales_upsert_contacts"].handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": surrogate_id,
                "name": "Alice Updated",
            },
        )
    )
    assert r3["status"] == "ok", f"update failed: {r3}"

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT label FROM kg_nodes WHERE namespace_id = $1 AND entity_type = $2",
            namespace_id,
            "CONTACT",
        )
    assert len(rows) == 1, (
        f"expected exactly 1 kg_nodes row for this contact, found {len(rows)}: "
        f"{[dict(r) for r in rows]} -- the surrogate-id upsert created a "
        "duplicate instead of updating the original"
    )
    assert rows[0]["label"] == real_label, "the surviving row must be the original, not a phantom"

    async with pg_pool.acquire() as conn:
        sec_row = await conn.fetchrow(
            "SELECT name FROM sales_contacts WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            real_label,
        )
    assert sec_row is not None
    assert sec_row["name"] == "Alice Updated", "the update must have landed on the original row"


@pytest.mark.asyncio
async def test_upsert_with_new_label_still_creates(
    engine: NCEEngine,
    namespace_id: uuid.UUID,
    pg_pool: asyncpg.Pool,
    _seed_ownership_for_graph_upsert: None,
) -> None:
    """Requirement this fix must not break: when the lenient lookup finds no
    existing row -- a genuinely new label, whether caller-chosen or
    server-generated -- create must still work exactly as before.
    """
    chosen_label = f"contact-{uuid.uuid4().hex[:8]}"
    r1 = json.loads(
        await TOOL_REGISTRY["sales_upsert_contacts"].handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": chosen_label,
                "name": "Bob",
                "email": "bob@example.com",
            },
        )
    )
    assert r1["status"] == "ok", f"create-by-chosen-label failed: {r1}"
    assert r1["id"] == chosen_label

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT label FROM kg_nodes WHERE namespace_id = $1 AND entity_type = $2 AND label = $3",
            namespace_id,
            "CONTACT",
            chosen_label,
        )
    assert row is not None, (
        "create-by-caller-chosen-label must still work after the lenient lookup fix"
    )
