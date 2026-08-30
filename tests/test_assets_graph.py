"""Tests for the Assets engine's graph projection
(Module 9, Wave 2b — Batch 142b — ``nce/vertical_modules/assets/graph.py``).

This is the GRAPH half of the Batch 142 split. Batch 142's ``seed.py`` writes
one relational ``assets`` row and no graph at all; this module writes the
``ASSET`` ``kg_node`` plus ``BOM_LINE -[installed_as]-> ASSET`` and
``ASSET -[lives_in]-> FUNCTIONAL_LOCATION``, and this wave adds ``ASSET``'s
Contract-A row to ``nce/config_data/node-ownership.json``.

Covers:

  (a) the ASSET node and BOTH edges are created, with the labels the OWNING
      engines define (``system_design/graph.py:_fl_label`` for the room), and
      no ``BOM_LINE``/``FUNCTIONAL_LOCATION`` NODE is authored as a
      side-effect.
  (b) re-projection is idempotent: the node and both edges stay at exactly one
      row each, and the node keeps its original ``created_at`` (an INSERT
      would have minted a new one).
  (c) 🔴 the Contract-A guard is proven reached in BOTH directions — REFUSED
      with the ``ASSET`` registry row absent, PERMITTED once it is present.
      The registry is mutated in the TEST DATABASE only (a DELETE against
      ``node_ownership_registry``), never by editing a file in the working
      tree: a prior audit corrupted two snapshots doing exactly that.
  (d) 🔴 ``graph.py``'s OWN ``namespace_id`` predicates, exercised through the
      OWNER ``pg_pool`` (``Superuser, Bypass RLS``) under a REAL cross-tenant
      label collision — the SAME ``asset_id``, hence the SAME ASSET label, in
      two namespaces. See that test's docstring for why every other test here
      leaves those predicates unexercised.
  (e) 🔴 through a real ``nce_app`` pool: FORCE RLS means a second namespace
      can neither SEE nor REACH the first's projected node. This is the RLS
      POLICY's job and is the complement of (d), not a substitute for it.
  (f) ``confidence`` is on the EDGES and only the edges — ``kg_nodes`` has no
      such column (rule 7).
  (g) the entity_type is ASSET, so the ownership guard and the stored row
      agree; a node written under some other type would slip the guard.

``@pytest.mark.integration``: every test below needs a database.

⚠ NOT WIRED INTO CI BY THIS WAVE. ``.github/workflows/ci.yml``'s
``Integration — M9 Assets (seed-from-bom)`` step runs ``tests/test_assets_seed.py``
only, so this file currently runs in NO CI job and
``tests/test_ci_integration_coverage.py`` FAILS on it by design — that ratchet
exists to make exactly this visible rather than let it pass silently. Fixing it
needs ``ci.yml``, a FOURTH file, and this wave's brief allows exactly three; the
same thing happened to Batch 142 itself (see that step's comment) and the
orchestrator resolved it there. Reported, not silenced.
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from urllib.parse import urlparse, urlunparse

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.config import cfg
from nce.entity_resolution.ownership import OwnershipError
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.assets.graph import (
    _asset_label,
    project_asset_to_graph,
    read_asset_projection,
)

# ---------------------------------------------------------------------------
# Pure-logic: the label conventions, checked against the OWNING engines'
# helpers rather than against a copy of this module's own f-strings.
# ---------------------------------------------------------------------------


def test_fl_label_matches_system_designs_own_helper() -> None:
    """The ``lives_in`` target must be the label System Design authors.

    Compared against ``system_design/graph.py:_fl_label`` — the source of
    truth — not against a literal retyped here. Goes RED if either convention
    drifts, which is precisely the failure that would leave Assets asserting a
    room nobody owns.
    """
    from nce.vertical_modules.assets.graph import _functional_location_label
    from nce.vertical_modules.system_design.graph import _fl_label as sd_fl_label

    assert _functional_location_label("acme-corp", "BUILDING-A:ROOM-101") == sd_fl_label(
        "acme-corp", "BUILDING-A:ROOM-101"
    )


def test_bom_line_label_matches_projects_helper_for_a_joined_identifier() -> None:
    """``BOM_LINE:<QUOTE>:<LINE_REF>`` — pins the documented assumption.

    ``project/convert.py:_bom_line_label`` takes TWO arguments;
    ``assets.bom_line_id`` is ONE opaque column. This module treats that column
    as the already-joined ``"<QUOTE_ID>:<LINE_REF>"`` component, and this test
    pins that reading against ``convert.py`` itself so the assumption is
    executable rather than only prose. Goes RED if either helper's prefix or
    casing drifts.

    Scope of the claim: this proves the two agree for a joined identifier. It
    does NOT prove callers actually store one — a flat ``bom_line_id`` yields a
    label matching no BOM_LINE node, which is the consequence the module
    docstring names and which nothing can detect until Batch 132a exists.
    """
    from nce.vertical_modules.assets.graph import _bom_line_label
    from nce.vertical_modules.project.convert import _bom_line_label as project_bom_line_label

    assert _bom_line_label("Q001:AMP01") == project_bom_line_label("Q001", "AMP01")


# ---------------------------------------------------------------------------
# Integration helpers — mirror tests/test_assets_seed.py's helpers in shape.
# ---------------------------------------------------------------------------


def _app_dsn() -> str:
    """Rewrite the integration DSN onto the restricted ``nce_app`` role.

    Verbatim in shape from ``tests/test_assets_seed.py::_app_dsn`` — the
    in-repo precedent for driving a vertical module through a REAL
    FORCE-RLS-subject connection instead of the superuser ``pg_pool``.
    """
    primary = (
        os.environ.get("NCE_INTEGRATION_PG_DSN")
        or os.environ.get("PG_DSN")
        or os.environ.get("DATABASE_URL")
        or cfg.PG_DSN
    )
    parsed = urlparse(primary)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    app_pass = cfg.NCE_APP_PASSWORD or "nce_app_secret"
    netloc = f"nce_app:{app_pass}@{netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


async def _grant_asset_ownership(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Seed ``node_ownership_registry`` for *namespace_id* from the JSON map.

    The ``namespace_id`` / ``make_namespace`` fixtures insert a bare
    ``namespaces`` row; only ``orchestrator.py::_seed_node_ownership_all`` does
    this in production, so a test must do it explicitly. Seeding from the REAL
    ``node-ownership.json`` (never a hand-written INSERT) is what makes (c)'s
    "permitted" direction evidence about THIS WAVE'S registry edit rather than
    about a literal typed into the test.
    """
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        await seed_node_ownership_registry(conn, namespace_id)


async def _revoke_asset_ownership(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> int:
    """Delete ONLY the ASSET row from the registry for *namespace_id*.

    A database mutation in a disposable test database — never an edit to
    ``node-ownership.json`` in the working tree. Deleting just this one row
    (rather than seeding nothing at all) keeps every other engine's grant in
    place, so a refusal can only be about ASSET.
    """
    async with pg_pool.acquire() as conn:
        status = await conn.execute(
            "DELETE FROM node_ownership_registry WHERE namespace_id = $1 AND node_type = 'ASSET'",
            namespace_id,
        )
    return int(status.split()[-1])


async def _count(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    sql: str,
    *args: Any,
) -> int:
    async with pg_pool.acquire() as conn:
        return int(await conn.fetchval(sql, *args))


async def _project(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run the projection inside a scoped, transactional session."""
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        return await project_asset_to_graph(conn, namespace_id, **kwargs)


# ---------------------------------------------------------------------------
# (a) The node and BOTH edges land, with the owning engines' labels.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_projection_writes_the_asset_node_and_both_edges(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(a) + (f) + (g). The ASSET node, ``installed_as`` and ``lives_in``.

    Also pins the two things a happy-path test usually lets through: the
    ``lives_in`` object is the label SYSTEM DESIGN would author (slug
    included, upper-cased), and no ``BOM_LINE`` or ``FUNCTIONAL_LOCATION``
    NODE is created as a side-effect — Assets owns neither, and authoring one
    would be a Contract-A violation that the ASSET-only guard cannot catch.
    """
    from nce.vertical_modules.system_design.graph import _fl_label as sd_fl_label

    await _grant_asset_ownership(pg_pool, namespace_id)
    asset_id = uuid.uuid4()

    async with pg_pool.acquire() as conn:
        slug = await conn.fetchval("SELECT slug FROM namespaces WHERE id = $1", namespace_id)

    result = await _project(
        pg_pool,
        namespace_id,
        asset_id=asset_id,
        bom_line_id="Q900:AMP01",
        functional_location_id="BUILDING-A:ROOM-101",
    )

    expected_asset = _asset_label(asset_id)
    assert result["asset_label"] == expected_asset
    assert result["installed_as"] == {
        "subject": "BOM_LINE:Q900:AMP01",
        "predicate": "installed_as",
        "object": expected_asset,
    }
    assert result["lives_in"] == {
        "subject": expected_asset,
        "predicate": "lives_in",
        "object": sd_fl_label(str(slug), "BUILDING-A:ROOM-101"),
    }

    # The rows really exist, with the right entity_type (g).
    assert (
        await _count(
            pg_pool,
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1 AND label = $2 "
            "AND entity_type = 'ASSET'",
            namespace_id,
            expected_asset,
        )
        == 1
    )
    # (f) confidence lives on the EDGES only — 1.0, structural.
    async with pg_pool.acquire() as conn:
        confidences = await conn.fetch(
            "SELECT predicate, confidence FROM kg_edges WHERE namespace_id = $1 "
            "AND predicate IN ('installed_as', 'lives_in') ORDER BY predicate",
            namespace_id,
        )
    assert [(r["predicate"], r["confidence"]) for r in confidences] == [
        ("installed_as", 1.0),
        ("lives_in", 1.0),
    ]

    # Assets authored NEITHER endpoint node.
    assert (
        await _count(
            pg_pool,
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1 "
            "AND entity_type IN ('BOM_LINE', 'FUNCTIONAL_LOCATION')",
            namespace_id,
        )
        == 0
    ), "Assets owns neither BOM_LINE nor FUNCTIONAL_LOCATION — it writes edges to them only"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_absent_functional_location_writes_no_lives_in_edge(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """``functional_location_id`` is nullable in migration 054.

    An asset seeded before its room is known must still project — with the
    ``installed_as`` edge and NO ``lives_in`` edge, reported as ``None`` rather
    than silently skipped. Goes RED if the projection starts fabricating an
    ``FL:`` label from an empty id, which would assert a room that does not
    exist.
    """
    await _grant_asset_ownership(pg_pool, namespace_id)
    asset_id = uuid.uuid4()

    result = await _project(pg_pool, namespace_id, asset_id=asset_id, bom_line_id="Q901:NOROOM")

    assert result["lives_in"] is None
    assert result["installed_as"]["subject"] == "BOM_LINE:Q901:NOROOM"
    assert (
        await _count(
            pg_pool,
            "SELECT COUNT(*) FROM kg_edges WHERE namespace_id = $1 AND predicate = 'lives_in'",
            namespace_id,
        )
        == 0
    )


# ---------------------------------------------------------------------------
# (b) Idempotency under re-projection.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reprojection_duplicates_neither_the_node_nor_the_edges(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(b) Re-projecting the SAME asset creates nothing new.

    ``created_at`` is asserted unchanged as well as the counts: a bare INSERT
    that happened to be swallowed by an error, or an upsert re-keyed onto a
    column that made every run distinct, would both show up as a NEW
    ``created_at`` even while the count stayed at one. ``updated_at`` is
    deliberately NOT asserted stable — the upsert template bumps it on
    purpose, and pinning it would pin the opposite of the intended behaviour.
    """
    await _grant_asset_ownership(pg_pool, namespace_id)
    asset_id = uuid.uuid4()
    params: dict[str, Any] = {
        "asset_id": asset_id,
        "bom_line_id": "Q902:REPLAY",
        "functional_location_id": "BUILDING-B:ROOM-7",
    }

    first = await _project(pg_pool, namespace_id, **params)

    async with pg_pool.acquire() as conn:
        created_before = await conn.fetchval(
            "SELECT created_at FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            first["asset_label"],
        )

    second = await _project(pg_pool, namespace_id, **params)
    third = await _project(pg_pool, namespace_id, **params)

    assert first == second == third, "a projection must be a pure function of its inputs"

    assert (
        await _count(
            pg_pool,
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            first["asset_label"],
        )
        == 1
    ), "re-projection must not duplicate the ASSET node"

    for predicate in ("installed_as", "lives_in"):
        assert (
            await _count(
                pg_pool,
                "SELECT COUNT(*) FROM kg_edges WHERE namespace_id = $1 AND predicate = $2",
                namespace_id,
                predicate,
            )
            == 1
        ), f"re-projection must not duplicate the {predicate} edge"

    async with pg_pool.acquire() as conn:
        created_after = await conn.fetchval(
            "SELECT created_at FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            first["asset_label"],
        )
    assert created_before == created_after, "the node was re-created rather than upserted in place"


# ---------------------------------------------------------------------------
# (c) 🔴 THE CONTRACT-A GUARD, PROVEN REACHED IN BOTH DIRECTIONS.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_is_refused_without_the_asset_row_and_permitted_with_it(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(c) 🔴 the guard, both directions, in ONE test so they cannot drift apart.

    A happy-path-only test passes identically with ``assert_owner`` deleted
    from ``graph.py``; this one does not. The refusal half runs with every
    OTHER engine's grant present and only the ``ASSET`` row deleted, so the
    ``OwnershipError`` can only be about ASSET — and it asserts
    ``owner_engine is None`` specifically, which is deny-by-default rather
    than a wrong-owner rejection.

    The permitted half then re-seeds from the REAL
    ``nce/config_data/node-ownership.json``. That makes it evidence about the
    row THIS WAVE added: revert that one-line edit and this half goes RED,
    because the seed would put no ASSET row back.

    The registry is mutated in the DATABASE only. Nothing here edits a file in
    the working tree.
    """
    asset_id = uuid.uuid4()
    params: dict[str, Any] = {
        "asset_id": asset_id,
        "bom_line_id": "Q903:GUARD",
        "functional_location_id": "BUILDING-C:ROOM-1",
    }

    # --- Direction 1: REFUSED. Seed everything, then remove ONLY ASSET. ---
    await _grant_asset_ownership(pg_pool, namespace_id)
    deleted = await _revoke_asset_ownership(pg_pool, namespace_id)
    assert deleted == 1, (
        "the JSON seed must have produced exactly one ASSET row for this namespace — "
        "if this is 0 the wave's node-ownership.json edit is missing, and the "
        "'permitted' half below would be proving nothing"
    )

    with pytest.raises(OwnershipError) as refused:
        await _project(pg_pool, namespace_id, **params)

    assert refused.value.node_type == "ASSET"
    assert refused.value.writer_engine == "assets"
    assert refused.value.owner_engine is None, "must be deny-by-default, not a wrong-owner denial"

    # ...and the refusal is TOTAL: no node, no edge, nothing half-written.
    assert (
        await _count(pg_pool, "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1", namespace_id)
        == 0
    )
    assert (
        await _count(pg_pool, "SELECT COUNT(*) FROM kg_edges WHERE namespace_id = $1", namespace_id)
        == 0
    ), "the installed_as edge must not survive a refused node write"

    # --- Direction 2: PERMITTED, once the JSON's ASSET row is back. ---
    await _grant_asset_ownership(pg_pool, namespace_id)
    result = await _project(pg_pool, namespace_id, **params)

    assert result["asset_label"] == _asset_label(asset_id)
    assert (
        await _count(
            pg_pool,
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1 AND entity_type = 'ASSET'",
            namespace_id,
        )
        == 1
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_another_engine_cannot_claim_asset(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(c, third direction) The row grants ``assets`` — not everyone.

    Rewrites the registry's ``owner_engine`` to a different engine and shows
    the same call is refused with ``owner_engine`` naming that engine. Without
    this, ``assert_owner`` returning early for ANY registered row would still
    pass the test above.
    """
    await _grant_asset_ownership(pg_pool, namespace_id)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE node_ownership_registry SET owner_engine = 'system_design' "
            "WHERE namespace_id = $1 AND node_type = 'ASSET'",
            namespace_id,
        )

    with pytest.raises(OwnershipError) as refused:
        await _project(pg_pool, namespace_id, asset_id=uuid.uuid4(), bom_line_id="Q904:WRONGOWNER")

    assert refused.value.owner_engine == "system_design"
    assert refused.value.writer_engine == "assets"


# ---------------------------------------------------------------------------
# (d) 🔴 graph.py's OWN namespace_id predicates, through the OWNER pool.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_back_is_namespace_scoped_under_a_real_label_collision(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """(d) 🔴 pins the ``namespace_id`` predicates in ``read_asset_projection``.

    Every other test in this file leaves them unexercised, which is why this
    one is needed:

    * they all use a FRESH ``uuid4()`` asset id, so the ASSET label is unique
      per run and ``label = $1`` alone is already a perfect discriminator —
      the namespace predicate is redundant there and could be deleted with
      every one of them still green;
    * (e), the cross-tenant test, runs through ``nce_app``, where the RLS
      POLICY refuses a foreign read whether or not the predicate is present,
      so it cannot discriminate on the predicate even in principle.

    This test does the one thing neither does: it projects the SAME
    ``asset_id`` — hence the SAME ASSET label — into TWO namespaces with
    DIFFERENT rooms and different BOM lines, then reads both back through the
    OWNER ``pg_pool``, which is ``Superuser, Bypass RLS``. The module's own
    WHERE clauses are the only thing standing between the read and the other
    tenant's rows.

    Goes RED if any ``AND namespace_id = $N::uuid`` in
    ``read_asset_projection`` is weakened or dropped: the node/edge reads then
    return both tenants' rows and the equality assertions below fail. BOTH
    namespaces are read, so the result does not depend on which one an
    unscoped query happens to reach first.
    """
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    await _grant_asset_ownership(pg_pool, ns_a)
    await _grant_asset_ownership(pg_pool, ns_b)

    shared_asset_id = uuid.uuid4()  # THE SAME KEY IN BOTH TENANTS.

    await _project(
        pg_pool,
        ns_a,
        asset_id=shared_asset_id,
        bom_line_id="QA:LINE-A",
        functional_location_id="ROOM-A",
    )
    await _project(
        pg_pool,
        ns_b,
        asset_id=shared_asset_id,
        bom_line_id="QB:LINE-B",
        functional_location_id="ROOM-B",
    )

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, ns_a)
        view_a = await read_asset_projection(conn, ns_a, shared_asset_id)
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, ns_b)
        view_b = await read_asset_projection(conn, ns_b, shared_asset_id)

    assert view_a is not None and view_b is not None

    # A real collision: one label, two tenants, two distinct edge sets.
    assert view_a["asset_label"] == view_b["asset_label"] == _asset_label(shared_asset_id)

    assert view_a["installed_as"] == ["BOM_LINE:QA:LINE-A"], (
        "ns_a read another tenant's installed_as edge — the namespace_id predicate "
        "is missing or ineffective"
    )
    assert view_b["installed_as"] == ["BOM_LINE:QB:LINE-B"], (
        "ns_b read another tenant's installed_as edge — the namespace_id predicate "
        "is missing or ineffective"
    )
    assert len(view_a["lives_in"]) == 1 and view_a["lives_in"][0].endswith(":ROOM-A")
    assert len(view_b["lives_in"]) == 1 and view_b["lives_in"][0].endswith(":ROOM-B")

    # The FL labels embed each namespace's OWN slug, so they differ too.
    assert view_a["lives_in"] != view_b["lives_in"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_returns_none_for_a_namespace_that_never_projected(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """Absence is absence — never a fabricated placeholder.

    Complements (d): there the foreign rows exist and must be filtered out
    of a POPULATED result; here the reading namespace has nothing of its own,
    so an unscoped node lookup would return the OTHER tenant's node and this
    would come back non-``None``.
    """
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    await _grant_asset_ownership(pg_pool, ns_a)

    shared_asset_id = uuid.uuid4()
    await _project(pg_pool, ns_a, asset_id=shared_asset_id, bom_line_id="QA:ONLY-A")

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, ns_b)
        view_b = await read_asset_projection(conn, ns_b, shared_asset_id)

    assert view_b is None, "ns_b must not see ns_a's ASSET node through the owner pool"


# ---------------------------------------------------------------------------
# (e) 🔴 FORCE RLS through a REAL nce_app pool.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nce_app_pool_isolates_the_projected_graph(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """(e) 🔴 the RLS POLICY half, driven through a real ``nce_app`` connection.

    The projection itself runs through ``nce_app`` here — not the superuser
    pool — so this also proves the restricted role actually HAS the grants the
    projection needs (INSERT/UPDATE on kg_nodes, kg_edges and outbox_events).
    A test that only ever wrote as the owner would not.

    ns_b then cannot READ ns_a's node even naming ns_a's ``namespace_id``
    explicitly, and cannot WRITE a row carrying it (the policy's
    ``WITH CHECK``).

    Precisely scoped claim: like ``test_assets_seed.py``'s equivalent, this
    does NOT discriminate on ``FORCE ROW LEVEL SECURITY`` — ``FORCE`` extends
    RLS to the table OWNER, and ``nce_app`` is not the owner, so plain
    ``ENABLE`` already binds it. What ``FORCE`` protects against is the OWNER
    pool, and that is (d)'s job via the module's own predicates.
    """
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    await _grant_asset_ownership(pg_pool, ns_a)
    await _grant_asset_ownership(pg_pool, ns_b)

    asset_id = uuid.uuid4()
    async with pg_pool.acquire() as conn:
        slug_a = str(await conn.fetchval("SELECT slug FROM namespaces WHERE id = $1", ns_a))

    app_pool = await asyncpg.create_pool(_app_dsn(), min_size=1, max_size=2)
    try:
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_a)
            result = await project_asset_to_graph(
                conn,
                ns_a,
                asset_id=asset_id,
                bom_line_id="QA:RLS-1",
                functional_location_id="ROOM-RLS",
                # nce_app has NO grant on `namespaces`; the slug must be passed
                # in or the FL-label lookup raises. Pinned by
                # test_nce_app_cannot_read_the_namespace_slug below.
                namespace_slug=slug_a,
            )
        label = result["asset_label"]

        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_a)
            seen_by_a = await conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
                ns_a,
                label,
            )
        assert seen_by_a == 1, "ns_a must see the node it projected through nce_app"

        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_b)
            seen_by_b = await conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
                ns_a,
                label,
            )
        assert seen_by_b == 0, "ns_b must not see ns_a's ASSET node"

        # ...and cannot reach into ns_a either.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_b)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    "INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin) "
                    "VALUES ($1, 'ASSET', $2, 'agent')",
                    "ASSET:CROSS-TENANT",
                    ns_a,
                )
    finally:
        await app_pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nce_app_cannot_read_the_namespace_slug(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """🔴 Pins the privilege gap that makes ``namespace_slug`` a parameter.

    ``nce_app`` has NO grant on ``namespaces`` (the only grantee is the owner),
    so building System Design's slug-bearing ``FL:`` label by reading the row
    is impossible on the role production actually runs as. Omitting
    ``namespace_slug`` therefore raises rather than silently dropping the
    ``lives_in`` edge — an asset reported as projected with its room missing
    would be far worse than a loud failure.

    This is a PRE-EXISTING foundation gap, not one this wave introduced:
    ``agreements/sla.py:do_set_sla_coverage`` does the identical
    ``SELECT slug FROM namespaces`` to build the same label and is broken the
    same way under ``nce_app``. Fixing that needs a GRANT (DDL) or an edit to
    another module — both outside this wave's three files.

    Goes RED if that grant is ever added, at which point the ``namespace_slug``
    parameter becomes optional in fact as well as in signature and this test
    should be replaced rather than deleted.
    """
    ns = await make_namespace()
    await _grant_asset_ownership(pg_pool, ns)

    app_pool = await asyncpg.create_pool(_app_dsn(), min_size=1, max_size=2)
    try:
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await project_asset_to_graph(
                    conn,
                    ns,
                    asset_id=uuid.uuid4(),
                    bom_line_id="QA:NOSLUG",
                    functional_location_id="ROOM-NOSLUG",
                )

        # ...and with NO functional_location_id the slug is never needed, so the
        # same connection projects fine. This is what proves the failure above
        # is about the slug lookup specifically and not about nce_app lacking
        # some grant the projection needs generally.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns)
            result = await project_asset_to_graph(
                conn, ns, asset_id=uuid.uuid4(), bom_line_id="QA:NOSLUG-OK"
            )
        assert result["lives_in"] is None
    finally:
        await app_pool.close()
