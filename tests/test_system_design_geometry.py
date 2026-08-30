"""
tests/test_system_design_geometry.py
====================================
Module 6 Wave 14 — canvas geometry, the per-DESIGN optimistic-concurrency
token, and the ``racks`` bucket that closes debt D5.

The filename matches the ``tests/test_system_design_*.py`` CI glob B067a wired
(``.github/workflows/ci.yml``), so this file runs in CI with no workflow edit.
Any other filename would need a ``ci.yml`` change, which is a scope change.

What these tests actually gate
------------------------------
1. **Owner-pool tenant isolation on the TWO NEW LEAF QUERIES.**  ``nce_app``
   serves no request in this deployment, so every request runs on a pool that
   ``FORCE ROW LEVEL SECURITY`` does not constrain.  What isolates tenants is
   the explicit ``namespace_id = $n::uuid`` predicate in
   ``geometry.fetch_geometry_by_labels`` and ``geometry.fetch_design_version``.
   The two tenants below collide on **every identifier** — same design_id, same
   slug, same site/building/floor/room, same device/port/rack/cable refs — and
   differ **only in content**: a different ``x`` and a different ``cable_type``
   per tenant, and a different version.  A fixture that gave the two tenants
   different labels could not detect a predicate that filters by label, and
   B067b failed TAG on exactly that.

2. **The two key grains stay apart.**  ``system_design_geometry`` holds
   node-geometry rows (``version IS NULL``) and one design version row
   (``version IS NOT NULL``) under the same natural key.  Both directions are
   asserted: the geometry read must not return the version row, and the version
   read must not return a geometry row.  They must also coexist without
   colliding on ``UNIQUE (namespace_id, node_label)``.

3. **``expected_version`` is a REAL compare-and-swap.**  Stale → a distinct
   conflict, nothing written.  Matching → the version increments.  Concurrent
   writers holding the same token → exactly one wins.  And the increment is
   inside the **write's own transaction**: a write that fails after the bump
   must leave the version where it was, which is the only thing that makes the
   token describe the write it claims to describe.

4. **The ``rack_face`` CHECK is real**, enforced by the database rather than by
   a Python guard someone can route around.

5. **Debt D5** — the ``racks`` bucket exists, carries the capability row, and
   sorts like the other buckets.

The per-predicate mutation table (one row per predicate, no grouped results) is
in the wave report.  Every row was produced by mutating a single predicate in a
scratchpad COPY of the tree — never in the tree itself — and asserting the edit
landed before running.

All DB-dependent tests are ``@pytest.mark.integration`` (wave rule 9).
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from nce.vertical_modules.system_design.geometry import (
    INITIAL_VERSION,
    VersionConflictError,
    bump_design_version,
    design_version_label,
    fetch_design_version,
    fetch_geometry_by_labels,
    upsert_node_geometry,
)

# ---------------------------------------------------------------------------
# Fixture data.
#
# EVERY identifier below is shared by both tenants.  Only content differs.  See
# the module docstring for why that is not a stylistic choice.
# ---------------------------------------------------------------------------

_DESIGN_ID = "DESIGN-W14-GEOM-001"
_DESIGN_LABEL = f"DESIGN:{_DESIGN_ID}"
_NS_SLUG = "w14-geom"

_SITE = "SiteGeom"
_BUILDING = "BuildingWest"
_FLOOR = "FloorTwo"
_ROOM = "RoomTwoOhFour"

_DEVICE_REF = "AMP-W14"
_PORT_REF = "OUT-3"
_RACK_REF = "RACK-W14-B"
_CABLE_REF = "CBL-W14-2"

_DEVICE_LABEL = f"DEVICE:{_DESIGN_ID}:{_DEVICE_REF}"
_RACK_LABEL = f"RACK:{_DESIGN_ID}:{_RACK_REF}"
_CABLE_LABEL = f"CABLE:{_DESIGN_ID}:{_CABLE_REF}"

_TOPOLOGY_TOOL = "system_design_author_topology"
_FL_TOOL = "system_design_author_functional_location"
_READ_TOOL = "system_design_get_topology"

#: How many times a tenant is written before the isolation read.  ALPHA and
#: BETA are deliberately given DIFFERENT counts so their version rows differ:
#: a version read that ignored the namespace would otherwise return a number
#: that happened to be right.
_WRITES = {"ALPHA": 1, "BETA": 3}


class _NullCacheRedis:
    """Never a cache hit, but ``incr`` exists.

    ``mcp_stdio_dispatch`` calls ``bump_cache_generation`` unguarded after every
    successful ``mutation=True`` tool, so a ``None`` client turns every
    authoring call into ``McpError(-32603)``.  Never returning a hit means every
    read-back here proves the query ran rather than that a payload was replayed.
    """

    def __init__(self) -> None:
        self.generation = 0

    async def get(self, key: str) -> None:
        return None

    async def setex(self, key: str, ttl: int, value: str) -> None:
        return None

    async def incr(self, key: str) -> int:
        self.generation += 1
        return self.generation


class _EngineStub:
    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool
        self.redis_client = _NullCacheRedis()


class _StubRequest:
    """Minimal duck-typed Starlette request: the routes read only ``.json()``.

    Needed because asserting the MCP error code alone would not show that the
    REST twin also stopped returning 500 — the whole F-A2 defect was that a
    non-``ValueError`` escaped BOTH surfaces' handlers, so both are driven.
    """

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.path_params: dict[str, str] = {}

    async def json(self) -> dict[str, Any]:
        return self._body


def _tenant_geometry(tag: str) -> dict[str, Any]:
    """Geometry whose CONTENT identifies the owning tenant.

    ``cable_type`` is a string and is the marker that survives a
    ``json.dumps`` scan of the whole payload; the numbers are there so a
    transposition inside the writer is visible too.
    """
    return {
        "x": 11.5 if tag == "ALPHA" else 88.25,
        "y": 12.75 if tag == "ALPHA" else 99.5,
        "rack_position": 3.5 if tag == "ALPHA" else 41.5,
        "rack_face": "front" if tag == "ALPHA" else "rear",
        "cable_length_m": 4.25 if tag == "ALPHA" else 77.75,
        "cable_type": f"{tag}-CABLETAG",
        "meta": {"copper.room.w": 6.5 if tag == "ALPHA" else 19.25, "tenant": tag},
    }


def _buildings() -> list[dict[str, Any]]:
    """Identical in both tenants — the FL tree carries no tenant marker."""
    return [
        {
            "name": _BUILDING,
            "floors": [{"name": _FLOOR, "rooms": [{"name": _ROOM, "positions": []}]}],
        }
    ]


def _fl_args(ns_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "namespace_id": str(ns_id),
        "namespace_slug": _NS_SLUG,
        "design_id": _DESIGN_ID,
        "site_name": _SITE,
        "buildings": _buildings(),
    }
    args.update(overrides)
    return args


def _topology_args(ns_id: uuid.UUID, tag: str = "ALPHA", **overrides: Any) -> dict[str, Any]:
    geom = _tenant_geometry(tag)
    args: dict[str, Any] = {
        "namespace_id": str(ns_id),
        "design_id": _DESIGN_ID,
        "devices": [
            {
                "device_ref": _DEVICE_REF,
                "capability": {"manufacturer": f"{tag}-AMPCO"},
                "ports": [{"port_ref": _PORT_REF, "capability": {"port_direction": "output"}}],
                "rack_ref": _RACK_REF,
                "geometry": geom,
            }
        ],
        "connections": [
            {
                "from_device_ref": _DEVICE_REF,
                "from_port_ref": _PORT_REF,
                "to_device_ref": _DEVICE_REF,
                "to_port_ref": _PORT_REF,
                "cable_ref": _CABLE_REF,
                "cable_geometry": geom,
            }
        ],
        "racks": [
            {
                "rack_ref": _RACK_REF,
                "capability": {"model_number": f"{tag}-RACK"},
                "geometry": geom,
            }
        ],
    }
    args.update(overrides)
    return args


async def _seed_ownership(pg_pool: Any, ns_id: uuid.UUID) -> None:
    from nce.auth import set_namespace_context
    from nce.entity_resolution.ownership_seed import seed_node_ownership_registry

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            await seed_node_ownership_registry(conn, ns_id)


async def _dispatch(engine: Any, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call *tool* through the real MCP dispatch path and return its payload."""
    import json

    from nce.mcp_stdio_dispatch import execute_call_tool

    parts = await execute_call_tool(engine, tool, arguments)
    assert parts, f"dispatch returned no content for {tool}"
    return json.loads(parts[0].text)


async def _dispatch_ok(engine: Any, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    payload = await _dispatch(engine, tool, arguments)
    assert "error" not in payload, f"{tool} returned an error envelope: {payload}"
    return payload


async def _author_tenant(engine: Any, ns_id: uuid.UUID, tag: str) -> None:
    """Author one tenant's copy of the fully colliding design, ``_WRITES[tag]`` times."""
    await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))
    for _ in range(_WRITES[tag]):
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id, tag))


async def _geometry_row_count(pg_pool: Any, ns_id: uuid.UUID) -> int:
    """GEOMETRY rows for this namespace — grain 1 only, explicit ns predicate.

    ``version IS NULL`` is not decoration: the design VERSION row lives in this
    same table, so a bare ``count(*)`` is 1 for every design that has ever been
    authored and no "wrote nothing" assertion built on it could ever be true.
    The two key grains bite the test helper exactly as they bite the queries.
    """
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        return int(
            await conn.fetchval(
                """
                SELECT count(*)
                FROM system_design_geometry
                WHERE namespace_id = $1::uuid
                  AND version IS NULL
                """,
                str(ns_id),
            )
        )


# ---------------------------------------------------------------------------
# 1. The table itself.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestSchema:
    async def test_rack_face_check_rejects_anything_but_front_or_rear(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """The vocabulary is enforced by the DATABASE, not by a Python guard.

        NetBox's ``face`` is 'front' | 'rear' and Copper follows that as a
        binding ADR.  A Python-side guard is routed around by every writer that
        does not go through it — including a future one, and psql.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            for bad in ("side", "FRONT", "", "back"):
                # Each attempt gets its own SAVEPOINT. Without it the first
                # CheckViolation aborts the surrounding transaction and every
                # later attempt fails with InFailedSQLTransactionError instead —
                # which pytest.raises would NOT catch, so the loop would report
                # a failure for the wrong reason and, worse, a weakened CHECK
                # would still look "rejected" from the second value on.
                # pytest.raises is OUTSIDE the savepoint on purpose: the
                # exception has to propagate out of conn.transaction() so it
                # ROLLS BACK TO the savepoint. Catching it inside leaves the
                # block exiting normally, which issues RELEASE SAVEPOINT on an
                # already-aborted savepoint and fails the whole test.
                with pytest.raises(asyncpg.exceptions.CheckViolationError):
                    async with conn.transaction():
                        await conn.execute(
                            """
                            INSERT INTO system_design_geometry
                                (namespace_id, node_label, rack_face)
                            VALUES ($1::uuid, $2, $3)
                            """,
                            str(ns_id),
                            f"DEVICE:CHECK:{bad or 'EMPTY'}",
                            bad,
                        )

    async def test_rack_face_accepts_the_two_legal_values_and_null(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """The sibling of the test above.

        Without it, a CHECK of ``rack_face IS NULL`` alone — which rejects
        'front' and 'rear' too — would pass the rejection test.  A refusal test
        proves nothing about a constraint unless the acceptance test proves the
        constraint is not simply refusing everything.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            for i, good in enumerate(("front", "rear", None)):
                await conn.execute(
                    """
                    INSERT INTO system_design_geometry
                        (namespace_id, node_label, rack_face)
                    VALUES ($1::uuid, $2, $3)
                    """,
                    str(ns_id),
                    f"DEVICE:CHECK:OK{i}",
                    good,
                )

    async def test_a_non_finite_number_cannot_be_stored_even_by_a_direct_write(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """``system_design_geometry_numerics_finite`` — the structural backstop.

        ``validate_geometry`` already refuses NaN/±Infinity at the write
        boundary, and that is the fix. This constraint is for the writer that
        does not go through it — psql, a repair script, a future core — because
        a stored NaN cannot be undone (there is no delete path) and makes the
        WHOLE design's topology response raise for every reader.

        🔴 **Not written as ``CHECK (x = x)``.** That idiom catches NaN for IEEE
        floats and is a **no-op on NUMERIC**: PostgreSQL defines NUMERIC
        ``'NaN' = 'NaN'`` as TRUE so that NaN sorts and groups deterministically.
        Verified on this server before choosing the predicate — the three
        special values are excluded by name instead. A ``x = x`` constraint
        would have shipped as a guard that gates nothing.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            # The premise of this test, asserted rather than assumed.
            assert await conn.fetchval("SELECT 'NaN'::numeric = 'NaN'::numeric") is True, (
                "this PostgreSQL treats NUMERIC NaN as unequal to itself; the "
                "`x = x` idiom would work here and this comment is wrong"
            )
            for column in ("x", "y", "rack_position", "cable_length_m"):
                for bad in ("NaN", "Infinity", "-Infinity"):
                    # rack_position is NUMERIC(4,1), and a SCALED numeric cannot
                    # hold an infinity at all — PostgreSQL raises "numeric field
                    # overflow" from the TYPE before any CHECK is evaluated. That
                    # is still a refusal, and refusing is what this test is
                    # about, so both classes are accepted here rather than
                    # pretending one constraint does work the type does. NaN is
                    # scale-free and does reach the CHECK on every column.
                    with pytest.raises(
                        (
                            asyncpg.exceptions.CheckViolationError,
                            asyncpg.exceptions.NumericValueOutOfRangeError,
                        )
                    ):
                        async with conn.transaction():
                            await conn.execute(
                                f"""
                                INSERT INTO system_design_geometry
                                    (namespace_id, node_label, {column})
                                VALUES ($1::uuid, $2, $3::numeric)
                                """,
                                str(ns_id),
                                f"DEVICE:FIN:{column}:{bad}",
                                bad,
                            )

            # And specifically: NaN is refused by the CHECK, on every column,
            # not by any type rule. Without this the loop above would pass
            # against a table with no CHECK at all for the unscaled columns.
            for column in ("x", "y", "rack_position", "cable_length_m"):
                with pytest.raises(asyncpg.exceptions.CheckViolationError):
                    async with conn.transaction():
                        await conn.execute(
                            f"""
                            INSERT INTO system_design_geometry
                                (namespace_id, node_label, {column})
                            VALUES ($1::uuid, $2, 'NaN'::numeric)
                            """,
                            str(ns_id),
                            f"DEVICE:NAN:{column}",
                        )

    async def test_a_value_too_large_for_a_double_cannot_be_stored(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """``system_design_geometry_numerics_in_double_range``.

        A merely LARGE finite value is the same defect as NaN, one step later:
        NUMERIC stores 10**400, the read path does ``float(Decimal)`` which
        returns ``inf`` instead of raising, and ``JSONResponse`` then refuses to
        serialise the design. Bounding the column keeps "storable" and
        "serialisable" the same set.

        ``rack_position`` is deliberately NOT in this constraint: NUMERIC(4,1)
        already caps it at 999.9, far inside the double range.
        """
        from nce.db_utils import scoped_pg_session

        huge = "1" + "0" * 400
        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            for column in ("x", "y", "cable_length_m"):
                for bad in (huge, "-" + huge):
                    with pytest.raises(asyncpg.exceptions.CheckViolationError):
                        async with conn.transaction():
                            await conn.execute(
                                f"""
                                INSERT INTO system_design_geometry
                                    (namespace_id, node_label, {column})
                                VALUES ($1::uuid, $2, $3::numeric)
                                """,
                                str(ns_id),
                                f"DEVICE:RANGE:{column}:{len(bad)}",
                                bad,
                            )
            # Sibling: the largest double IS storable, so the constraint is not
            # simply refusing everything large.
            await conn.execute(
                """
                INSERT INTO system_design_geometry (namespace_id, node_label, x)
                VALUES ($1::uuid, $2, $3::numeric)
                """,
                str(ns_id),
                "DEVICE:RANGE:OK",
                repr(sys.float_info.max),
            )

    async def test_the_namespace_fk_exists_and_cascades(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """The tenant FK must EXIST, and it must be ON DELETE CASCADE.

        Two separate claims, and the repo only gated one of them.
        ``tests/test_namespace_fk_cascade.py``'s catalog ratchet walks the FKs
        that reference ``namespaces`` and asserts each is CASCADE — so it
        catches an FK declared NO ACTION, but a table with **no FK at all** is
        simply not in its result set and passes silently. Measured, not assumed:
        dropping this FK entirely left that ratchet AND this whole suite green.

        Both halves matter. Without the FK a geometry row outlives the tenant it
        belongs to and nothing reaps it; without CASCADE, deleting a tenant
        fails on the first child row — the exact breakage migration 055 existed
        to repair.

        That ratchet is outside this wave's ``Files:`` list, so the existence
        half is asserted here rather than by widening it.
        """
        ns_id: uuid.UUID = await make_namespace()
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT conname, confdeltype,
                       confrelid::regclass::text AS references_table
                FROM pg_constraint
                WHERE conrelid = 'system_design_geometry'::regclass
                  AND contype = 'f'
                  AND confrelid = 'namespaces'::regclass
                """
            )
        assert row is not None, (
            "system_design_geometry has NO foreign key to namespaces — its rows "
            "would outlive their tenant, and the namespace-FK catalog ratchet "
            "cannot see a constraint that does not exist"
        )
        # ``confdeltype`` is PostgreSQL's ``"char"`` type, which asyncpg hands
        # back as ``bytes`` — normalise rather than compare against one form and
        # get a passing-looking failure on the other driver version.
        deltype = row["confdeltype"]
        deltype = deltype.decode() if isinstance(deltype, (bytes, bytearray)) else str(deltype)
        assert deltype == "c", (
            f"the namespaces FK is {deltype!r}, not 'c' (ON DELETE CASCADE) — "
            "deleting a tenant would fail on the first geometry row"
        )
        assert row["references_table"] == "namespaces"

        # And the BEHAVIOUR, not only the catalog: a deleted tenant takes its
        # geometry with it. A catalog assertion alone would pass against a
        # CASCADE that some later trigger countermands.
        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await upsert_node_geometry(conn, ns_id, _DEVICE_LABEL, {"x": 1.5})
        assert await _geometry_row_count(pg_pool, ns_id) == 1

        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM namespaces WHERE id = $1", ns_id)
            remaining = await conn.fetchval(
                "SELECT count(*) FROM system_design_geometry WHERE namespace_id = $1",
                ns_id,
            )
        assert remaining == 0, "the tenant was deleted but its geometry rows survived"

    async def test_node_label_may_not_be_blank(self, pg_pool: Any, make_namespace: Any) -> None:
        """``system_design_geometry_node_label_not_blank`` (F-B4).

               Round 1 added four constraints and gated two. This is one of the two
               that no test touched — dropping it left the whole suite green, so it
               was an unenforced line in the DDL pretending to be an invariant.

               A blank grain key is not reachable through the authoring surfaces (every
               label builder emits a fixed ``DEVICE:``/``PORT:``/``RACK:``/``CABLE:``/
               ``FL:`` prefix), so this asserts at the level the constraint actually
               lives: the database. That is the point of putting it in the DDL rather
               than in Python — psql, a repair script and a future writer all get it.

               🔴 **What the constraint does NOT cover, verified rather than assumed:**
               ``btrim`` with no second argument strips **spaces only**, so a label of
               tabs or newlines (``btrim(E'
        ') = ''`` is FALSE in PostgreSQL)
               passes it. That is the idiom migration 054 (`assets`) established for
               this repo and it is kept deliberately rather than diverged from for a
               value nothing can produce — but it is a gap, it is disclosed in the wave
               report, and this test asserts only what the constraint really enforces
               rather than crediting it with more.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            for blank in ("", " ", "   "):
                # pytest.raises OUTSIDE the savepoint so the exception rolls it
                # back — see the rack_face test for why that ordering matters.
                with pytest.raises(asyncpg.exceptions.CheckViolationError):
                    async with conn.transaction():
                        await conn.execute(
                            """
                            INSERT INTO system_design_geometry
                                (namespace_id, node_label)
                            VALUES ($1::uuid, $2)
                            """,
                            str(ns_id),
                            blank,
                        )

            # The sibling half: a non-blank label is accepted, so the constraint
            # is not simply refusing everything.
            await conn.execute(
                """
                INSERT INTO system_design_geometry (namespace_id, node_label)
                VALUES ($1::uuid, $2)
                """,
                str(ns_id),
                _DEVICE_LABEL,
            )

    async def test_version_may_not_be_negative(self, pg_pool: Any, make_namespace: Any) -> None:
        """``system_design_geometry_version_non_negative`` (F-B4).

        The other constraint round 1 left ungated. ``version`` is monotonic and
        starts at :data:`INITIAL_VERSION`; a negative token is not a version
        that ever existed, and a row carrying one would be handed to a caller as
        a usable ``expected_version``.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            for bad in (-1, -(2**40)):
                with pytest.raises(asyncpg.exceptions.CheckViolationError):
                    async with conn.transaction():
                        await conn.execute(
                            """
                            INSERT INTO system_design_geometry
                                (namespace_id, node_label, version)
                            VALUES ($1::uuid, $2, $3)
                            """,
                            str(ns_id),
                            _DESIGN_LABEL + ":NEG" + str(bad),
                            bad,
                        )

            # Sibling: 0 and a positive value are accepted, and NULL (a geometry
            # row) is accepted — so the constraint is not refusing everything.
            for i, good in enumerate((0, 1, None)):
                await conn.execute(
                    """
                    INSERT INTO system_design_geometry
                        (namespace_id, node_label, version)
                    VALUES ($1::uuid, $2, $3)
                    """,
                    str(ns_id),
                    _DESIGN_LABEL + ":OK" + str(i),
                    good,
                )

    async def test_rack_position_keeps_the_half_u(self, pg_pool: Any, make_namespace: Any) -> None:
        """NUMERIC(4,1) — a rack unit is a half-U grid in practice.

        An ``INTEGER`` column would silently round 12.5 to 12 and put a device
        one half-unit off in every elevation drawing.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await upsert_node_geometry(conn, ns_id, _DEVICE_LABEL, {"rack_position": 12.5})
            geom = await fetch_geometry_by_labels(conn, ns_id, [_DEVICE_LABEL])
        assert geom[_DEVICE_LABEL]["rack_position"] == 12.5
        assert isinstance(geom[_DEVICE_LABEL]["rack_position"], float), (
            "NUMERIC must arrive JSON-native, not as a Decimal the JSON encoder rejects"
        )


# ---------------------------------------------------------------------------
# 2. The two key grains.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestTwoKeyGrains:
    async def test_a_geometry_row_and_a_version_row_coexist(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """Both grains live in one table under one natural key without colliding.

        They are keyed by DIFFERENT labels — a node label and the design label —
        so ``UNIQUE (namespace_id, node_label)`` separates them.  This is the
        property the migration header calls out as the only exception in this
        engine, and it is asserted rather than assumed.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await upsert_node_geometry(conn, ns_id, _DEVICE_LABEL, {"x": 1.5, "y": 2.5})
            assert await bump_design_version(conn, ns_id, _DESIGN_ID, None) == 1

            rows = await conn.fetch(
                """
                SELECT node_label, x, version
                FROM system_design_geometry
                WHERE namespace_id = $1::uuid
                ORDER BY node_label
                """,
                str(ns_id),
            )

        by_label = {r["node_label"]: r for r in rows}
        assert set(by_label) == {_DEVICE_LABEL, _DESIGN_LABEL}
        # Grain 1: geometry, no version.
        assert by_label[_DEVICE_LABEL]["version"] is None
        assert float(by_label[_DEVICE_LABEL]["x"]) == 1.5
        # Grain 2: version, no geometry.
        assert by_label[_DESIGN_LABEL]["version"] == 1
        assert by_label[_DESIGN_LABEL]["x"] is None

    async def test_the_geometry_read_never_returns_the_version_row(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """Grain separation, direction 1 — asserted with the DESIGN label ASKED FOR.

        ``read.py`` hands ``fetch_geometry_by_labels`` the design's whole scope
        label set, and the DESIGN label is in it.  So passing it here is not a
        contrived input: it is exactly what the composer does.  Without the
        ``version IS NULL`` predicate the version row comes back as an
        all-NULL geometry entry and a node the canvas never placed looks placed.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await upsert_node_geometry(conn, ns_id, _DEVICE_LABEL, {"x": 1.5})
            await bump_design_version(conn, ns_id, _DESIGN_ID, None)
            geom = await fetch_geometry_by_labels(conn, ns_id, [_DEVICE_LABEL, _DESIGN_LABEL])

        assert set(geom) == {_DEVICE_LABEL}, (
            "the design VERSION row leaked into the geometry map — the two key "
            "grains must be separated by the query, not by the caller"
        )
        assert "version" not in geom[_DEVICE_LABEL], (
            "the geometry projection must not carry the other grain's column"
        )

    async def test_the_version_read_never_returns_a_geometry_row(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """Grain separation, direction 2 — the BEHAVIOUR, not the predicate.

        A geometry row written under the DESIGN label — which nothing does
        today, but nothing forbids either — must not be read as the version row.

        🔴 **This test does NOT gate ``fetch_design_version``'s ``AND version IS
        NOT NULL``.** That predicate is mutation row P4 and it is **GREEN**:
        deleting it leaves this suite passing. The reason is structural, was
        independently verified, and is accepted rather than worked around —
        ``conn.fetchval`` collapses "no row matched" and "the matched row's
        value is NULL" to the same ``None``, and ``UNIQUE (namespace_id,
        node_label)`` forbids a geometry row and the version row from coexisting
        under one label, so the function returns ``INITIAL_VERSION`` with or
        without the predicate. No test can distinguish the two.

        The predicate is kept as documentation-as-code and as the backstop for
        the day that unique key ever widens; it is disclosed as an ungated line
        in the wave report rather than credited with a gate it does not have.
        What this test does gate is the observable behaviour: a geometry row at
        the design label reads as version 0, never as a token.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await upsert_node_geometry(conn, ns_id, _DESIGN_LABEL, {"x": 9.5})
            version = await fetch_design_version(conn, ns_id, _DESIGN_LABEL)

        assert version == INITIAL_VERSION, (
            "a geometry row under the design label was read as the version row"
        )


# ---------------------------------------------------------------------------
# 3. expected_version — the compare-and-swap.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestOptimisticConcurrency:
    async def test_a_fresh_design_is_at_the_initial_version(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            assert await fetch_design_version(conn, ns_id, _DESIGN_LABEL) == INITIAL_VERSION

    async def test_a_matching_token_increments_by_exactly_one(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            assert await bump_design_version(conn, ns_id, _DESIGN_ID, INITIAL_VERSION) == 1
            assert await bump_design_version(conn, ns_id, _DESIGN_ID, 1) == 2
            assert await bump_design_version(conn, ns_id, _DESIGN_ID, 2) == 3
            assert await fetch_design_version(conn, ns_id, _DESIGN_LABEL) == 3

    async def test_an_absent_token_still_increments(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """Last-writer-wins, but the token still advances.

        This is the property that makes the token mean anything at all: if a
        write that supplied no ``expected_version`` did not advance the version,
        a client holding a token could be overwritten by such a write and would
        never find out.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            assert await bump_design_version(conn, ns_id, _DESIGN_ID, None) == 1
            assert await bump_design_version(conn, ns_id, _DESIGN_ID, None) == 2

    @pytest.mark.parametrize("stale", [0, 1, 99])
    async def test_a_stale_token_raises_and_changes_nothing(
        self, stale: int, pg_pool: Any, make_namespace: Any
    ) -> None:
        """Behind, ahead, and far ahead all conflict — and none of them writes.

        ``0`` after two writes is the "behind" case; ``99`` is the "ahead" case
        a buggy or malicious client sends.  Both must refuse: the token means
        "the design is exactly here", not "the design is at most here".
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await bump_design_version(conn, ns_id, _DESIGN_ID, None)
            await bump_design_version(conn, ns_id, _DESIGN_ID, None)

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            with pytest.raises(VersionConflictError) as caught:
                await bump_design_version(conn, ns_id, _DESIGN_ID, stale)

        assert caught.value.expected == stale
        assert caught.value.actual == 2, "the caller must be told where the design really is"

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            assert await fetch_design_version(conn, ns_id, _DESIGN_LABEL) == 2, (
                "a refused compare-and-swap advanced the version anyway"
            )

    async def test_a_conflict_is_not_a_value_error(self) -> None:
        """Its own class, so no generic handler can render it as a bad argument.

        ``@mcp_handler`` maps ``ValueError`` to "Invalid parameters" and the
        REST routes catch ``ValueError`` for their 422 — a ``VersionConflictError``
        that subclassed it would be swallowed by both and become invisible.
        """
        assert not issubclass(VersionConflictError, ValueError)
        assert not issubclass(VersionConflictError, TypeError)
        assert not issubclass(VersionConflictError, KeyError)

    async def test_exactly_one_of_two_concurrent_writers_wins(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """Two writers holding the SAME token; one commits, one conflicts.

        Each runs on its own connection and its own transaction, so this is a
        real race on the row lock rather than two sequential calls dressed up as
        one.  A ``SELECT`` followed by an ``UPDATE`` would let both read 1 before
        either wrote, and both would succeed — the lost update the single-
        statement compare-and-swap exists to prevent.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await bump_design_version(conn, ns_id, _DESIGN_ID, None)  # -> 1

        async def contender() -> str:
            try:
                async with scoped_pg_session(pg_pool, ns_id) as conn:
                    await bump_design_version(conn, ns_id, _DESIGN_ID, 1)
                    # Hold the transaction open briefly so the two genuinely
                    # overlap rather than serialising by luck of scheduling.
                    await asyncio.sleep(0.05)
                return "won"
            except VersionConflictError:
                return "conflicted"

        results = await asyncio.gather(contender(), contender())

        assert sorted(results) == ["conflicted", "won"], (
            f"both writers holding token 1 got {results} — that is a lost update"
        )
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            assert await fetch_design_version(conn, ns_id, _DESIGN_LABEL) == 2, (
                "exactly one increment must have survived"
            )

    async def test_the_increment_is_inside_the_writes_own_transaction(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """A write that fails AFTER the bump must leave the version untouched.

        This is the atomicity claim, proved rather than asserted.  If the bump
        ran in a transaction of its own — a read-modify-write across two
        transactions — the version would survive the rollback and would then be
        describing a write that never landed.

        The failure is injected the way a real one arrives: a device dict with
        no ``device_ref``, which ``do_author_device_topology`` raises ``KeyError``
        on *after* the adapter has already bumped.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            before = await fetch_design_version(conn, ns_id, _DESIGN_LABEL)
        assert before == 1

        payload = await _dispatch(
            engine,
            _TOPOLOGY_TOOL,
            _topology_args(ns_id, devices=[{"capability": {"manufacturer": "NoRef"}}]),
        )
        assert "error" in payload, "the malformed device was accepted"

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            after = await fetch_design_version(conn, ns_id, _DESIGN_LABEL)
        assert after == before, (
            f"the version advanced {before} -> {after} for a write that failed — "
            "the bump is not inside the write's own transaction"
        )
        assert await _geometry_row_count(pg_pool, ns_id) == 0, (
            "the failed write left geometry rows behind"
        )


# ---------------------------------------------------------------------------
# 4. Owner-pool tenant isolation on the two new leaf queries.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestOwnerPoolIsolation:
    """Two tenants colliding on EVERY identifier, differing ONLY in content.

    Same design_id, same namespace slug, same site/building/floor/room, same
    device/port/rack/cable refs — so every node label, and therefore every
    geometry row's key, is byte-identical across the two tenants.  What differs
    is ``x``, ``rack_face``, ``cable_type``, ``meta`` and the version.

    A fixture that gave the two tenants different labels could not detect a
    predicate that filters by label, and would leave
    ``fetch_geometry_by_labels``' namespace predicate deletable with the suite
    green.  B067b failed TAG on exactly that construction.
    """

    async def _seed_both(
        self, pg_pool: Any, make_namespace: Any
    ) -> tuple[uuid.UUID, uuid.UUID, Any]:
        ns_a: uuid.UUID = await make_namespace()
        ns_b: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_a)
        await _seed_ownership(pg_pool, ns_b)
        engine = _EngineStub(pg_pool)
        await _author_tenant(engine, ns_a, "ALPHA")
        await _author_tenant(engine, ns_b, "BETA")
        return ns_a, ns_b, engine

    async def test_geometry_read_does_not_bleed_between_tenants(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each tenant reads its own geometry content and none of the other's."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        import json

        ns_a, ns_b, engine = await self._seed_both(pg_pool, make_namespace)

        read_a = await _dispatch_ok(
            engine, _READ_TOOL, {"namespace_id": str(ns_a), "design_id": _DESIGN_ID}
        )
        read_b = await _dispatch_ok(
            engine, _READ_TOOL, {"namespace_id": str(ns_b), "design_id": _DESIGN_ID}
        )

        for tag, payload, foreign in (("ALPHA", read_a, "BETA"), ("BETA", read_b, "ALPHA")):
            geometry = payload["geometry"]
            # The labels are identical in both tenants, so cardinality alone
            # catches a merge: a leak duplicates nothing (dicts collapse) but a
            # WRONG-tenant row wins the key, which the content check below sees.
            assert set(geometry) == {
                _DEVICE_LABEL,
                _RACK_LABEL,
                _CABLE_LABEL,
            }, f"{tag} read the wrong geometry key set: {sorted(geometry)}"

            own = _tenant_geometry(tag)
            other = _tenant_geometry(foreign)
            for label in (_DEVICE_LABEL, _RACK_LABEL, _CABLE_LABEL):
                assert geometry[label]["x"] == own["x"], (
                    f"{tag}'s {label} geometry carries {foreign}'s x — the only "
                    f"thing separating these two tenants is the SQL namespace "
                    f"predicate, and it is gone"
                )
                assert geometry[label]["rack_face"] == own["rack_face"]
                assert geometry[label]["cable_type"] == own["cable_type"]
                assert geometry[label]["meta"]["tenant"] == tag

            blob = json.dumps(payload)
            assert own["cable_type"] in blob, f"{tag} lost its own geometry content"
            assert other["cable_type"] not in blob, (
                f"{tag}'s read leaked {foreign}'s geometry content"
            )

    async def test_version_read_does_not_bleed_between_tenants(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two tenants' version rows share a label and must not share a value.

        ALPHA is written twice (FL + one topology) and BETA four times, so the
        two versions genuinely differ.  If both tenants wrote the same number of
        times, a version read with no namespace predicate would return the right
        answer by accident and this test would pass against a broken query.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_a, ns_b, engine = await self._seed_both(pg_pool, make_namespace)

        read_a = await _dispatch_ok(
            engine, _READ_TOOL, {"namespace_id": str(ns_a), "design_id": _DESIGN_ID}
        )
        read_b = await _dispatch_ok(
            engine, _READ_TOOL, {"namespace_id": str(ns_b), "design_id": _DESIGN_ID}
        )

        assert read_a["version"] == 1 + _WRITES["ALPHA"] == 2
        assert read_b["version"] == 1 + _WRITES["BETA"] == 4
        assert read_a["version"] != read_b["version"], (
            "the two tenants' colliding design labels resolved to one version row"
        )

    async def test_a_write_cannot_land_on_another_tenants_row(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The WRITE boundary, not only the read boundary.

        Both tenants upsert geometry under the identical node label.  If the
        upsert's conflict target were ``(node_label)`` rather than
        ``(namespace_id, node_label)``, the second tenant would overwrite the
        first's canvas instead of creating its own row — a silent cross-tenant
        write that no read test can see, because afterwards both tenants read
        the same (wrong) row consistently.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        from nce.db_utils import scoped_pg_session

        ns_a: uuid.UUID = await make_namespace()
        ns_b: uuid.UUID = await make_namespace()

        async with scoped_pg_session(pg_pool, ns_a) as conn:
            await upsert_node_geometry(conn, ns_a, _DEVICE_LABEL, {"x": 1.5, "cable_type": "A"})
        async with scoped_pg_session(pg_pool, ns_b) as conn:
            await upsert_node_geometry(conn, ns_b, _DEVICE_LABEL, {"x": 2.5, "cable_type": "B"})

        async with scoped_pg_session(pg_pool, ns_a) as conn:
            a = await fetch_geometry_by_labels(conn, ns_a, [_DEVICE_LABEL])
        async with scoped_pg_session(pg_pool, ns_b) as conn:
            b = await fetch_geometry_by_labels(conn, ns_b, [_DEVICE_LABEL])

        assert a[_DEVICE_LABEL]["cable_type"] == "A", "tenant B's write landed on tenant A's row"
        assert b[_DEVICE_LABEL]["cable_type"] == "B"
        assert a[_DEVICE_LABEL]["x"] == 1.5
        assert b[_DEVICE_LABEL]["x"] == 2.5

    async def test_a_compare_and_swap_cannot_be_satisfied_by_another_tenants_version(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """The CAS boundary.

        Tenant A's design is at 5, tenant B's at 0, under the identical design
        label.  B supplying token 5 must conflict: without the namespace
        predicate on the ``UPDATE``, B's write would compare against — and then
        increment — A's row.
        """
        from nce.db_utils import scoped_pg_session

        ns_a: uuid.UUID = await make_namespace()
        ns_b: uuid.UUID = await make_namespace()

        async with scoped_pg_session(pg_pool, ns_a) as conn:
            for _ in range(5):
                await bump_design_version(conn, ns_a, _DESIGN_ID, None)
            assert await fetch_design_version(conn, ns_a, _DESIGN_LABEL) == 5

        async with scoped_pg_session(pg_pool, ns_b) as conn:
            with pytest.raises(VersionConflictError):
                await bump_design_version(conn, ns_b, _DESIGN_ID, 5)

        async with scoped_pg_session(pg_pool, ns_a) as conn:
            assert await fetch_design_version(conn, ns_a, _DESIGN_LABEL) == 5, (
                "tenant B's compare-and-swap incremented tenant A's version row"
            )


# ---------------------------------------------------------------------------
# 5. Debt D5 — the racks bucket.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestRacksBucket:
    async def test_racks_are_projected_with_their_capability_row(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Debt D5: RACK nodes were authored from W12 and projected by nothing.

        The ledger recorded this as "capability rows are projected for
        DEVICE/PORT only".  It was wider: ``do_get_topology`` surfaced
        ``design``, ``functional_locations``, ``devices``, ``cables`` and
        ``edges``, so the whole RACK bucket — node included — was dropped.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _author_tenant(engine, ns_id, "ALPHA")

        payload = await _dispatch_ok(
            engine, _READ_TOOL, {"namespace_id": str(ns_id), "design_id": _DESIGN_ID}
        )

        assert len(payload["racks"]) == 1
        rack = payload["racks"][0]
        assert set(rack) == {"node", "capabilities"}
        assert rack["node"]["label"] == _RACK_LABEL
        assert rack["node"]["entity_type"] == "RACK"
        assert rack["capabilities"]["model_number"] == "ALPHA-RACK"
        # The rack is drawable: it has an elevation position and a face.
        assert payload["geometry"][_RACK_LABEL]["rack_position"] == 3.5
        assert payload["geometry"][_RACK_LABEL]["rack_face"] == "front"

    async def test_racks_are_sorted_by_label_like_every_other_bucket(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ordering is part of the contract Copper renders from.

        Three refs whose sorted order differs from their insertion order, so a
        bucket that simply returned the DB's row order fails.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))
        await _dispatch_ok(
            engine,
            _TOPOLOGY_TOOL,
            _topology_args(
                ns_id,
                racks=[{"rack_ref": ref} for ref in ("ZULU", "ALFA", "MIKE")],
            ),
        )

        payload = await _dispatch_ok(
            engine, _READ_TOOL, {"namespace_id": str(ns_id), "design_id": _DESIGN_ID}
        )
        labels = [r["node"]["label"] for r in payload["racks"]]
        assert labels == sorted(labels)
        assert [lbl.rsplit(":", 1)[-1] for lbl in labels] == ["ALFA", "MIKE", "ZULU"]


# ---------------------------------------------------------------------------
# 6. Geometry write semantics.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestGeometryWriteSemantics:
    async def test_an_omitted_key_keeps_its_stored_value(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """Partial update, like the capability row's.

        A canvas that moves a device without touching its rack face must not
        blank the rack face.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await upsert_node_geometry(
                conn, ns_id, _DEVICE_LABEL, {"x": 1.5, "rack_face": "rear", "rack_position": 7.0}
            )
            await upsert_node_geometry(conn, ns_id, _DEVICE_LABEL, {"x": 42.5})
            geom = await fetch_geometry_by_labels(conn, ns_id, [_DEVICE_LABEL])

        row = geom[_DEVICE_LABEL]
        assert row["x"] == 42.5, "the supplied key was not updated"
        assert row["rack_face"] == "rear", "an omitted key was blanked"
        assert row["rack_position"] == 7.0

    async def test_meta_is_replaced_wholesale_not_merged(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """``meta`` is a document the caller owns — exactly like ``capability.extra``.

        Merging two notions of a document is a decision NCE has not been given,
        and a merge would make it impossible for a caller to REMOVE a key.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await upsert_node_geometry(
                conn, ns_id, _DEVICE_LABEL, {"meta": {"copper.room.w": 5.0, "gone": 1}}
            )
            await upsert_node_geometry(conn, ns_id, _DEVICE_LABEL, {"meta": {"copper.room.w": 6.0}})
            geom = await fetch_geometry_by_labels(conn, ns_id, [_DEVICE_LABEL])

        assert geom[_DEVICE_LABEL]["meta"] == {"copper.room.w": 6.0}

    async def test_an_omitted_geometry_object_writes_no_row(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A node with no geometry is ABSENT from the map, not present-and-null.

        Otherwise "never placed" and "placed at the origin" are the same value,
        and a canvas cannot tell a new node from one a user dragged to (0, 0).
        An empty ``{}`` counts as absence for the same reason.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))
        result = await _dispatch_ok(
            engine,
            _TOPOLOGY_TOOL,
            _topology_args(
                ns_id,
                devices=[
                    {"device_ref": "NOGEOM"},
                    {"device_ref": "EMPTYGEOM", "geometry": {}},
                    {"device_ref": "HASGEOM", "geometry": {"x": 3.5}},
                ],
                connections=[],
                racks=[],
            ),
        )
        assert result["authored"]["geometry"] == 1, (
            "a writer that writes a row per node regardless of whether geometry "
            "was supplied reports 3 here"
        )

        payload = await _dispatch_ok(
            engine, _READ_TOOL, {"namespace_id": str(ns_id), "design_id": _DESIGN_ID}
        )
        assert set(payload["geometry"]) == {f"DEVICE:{_DESIGN_ID}:HASGEOM"}

    async def test_a_non_object_geometry_is_a_validation_error(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed geometry is refused, not silently coerced or ignored."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))
        payload = await _dispatch(
            engine,
            _TOPOLOGY_TOOL,
            _topology_args(
                ns_id,
                devices=[{"device_ref": "BADGEOM", "geometry": "12,34"}],
                connections=[],
                racks=[],
            ),
        )
        assert "error" in payload, "a string was accepted where a geometry object is required"
        assert await _geometry_row_count(pg_pool, ns_id) == 0

    async def test_cable_geometry_needs_a_cable_ref(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``cable_geometry`` describes the CABLE NODE, so with no cable there is none.

        Writing it anyway would put a geometry row under a label no ``kg_node``
        has, which the read surface would never return and nothing would ever
        clean up.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))
        result = await _dispatch_ok(
            engine,
            _TOPOLOGY_TOOL,
            _topology_args(
                ns_id,
                devices=[
                    {"device_ref": _DEVICE_REF, "ports": [{"port_ref": _PORT_REF}]},
                ],
                connections=[
                    {
                        "from_device_ref": _DEVICE_REF,
                        "from_port_ref": _PORT_REF,
                        "to_device_ref": _DEVICE_REF,
                        "to_port_ref": _PORT_REF,
                        # No cable_ref.
                        "cable_geometry": {"cable_length_m": 9.5},
                    }
                ],
                racks=[],
            ),
        )
        assert result["authored"]["geometry"] == 0
        assert await _geometry_row_count(pg_pool, ns_id) == 0


# ---------------------------------------------------------------------------
# 7. The design_version_label helper — one formula, not four.
# ---------------------------------------------------------------------------


def test_design_version_label_matches_the_graph_modules_formula() -> None:
    """The version row's key must be the SAME label the graph writes.

    ``graph.py``, ``devices.py`` and ``read.py`` each spell
    ``f"DESIGN:{design_id.upper()}"`` inline.  If this helper drifted from them,
    the version row would key off a label no design has and every
    compare-and-swap would silently operate on its own private row.
    """
    from nce.vertical_modules.system_design.read import _design_label

    for design_id in ("abc-123", "ABC-123", "MiXeD-Case-7"):
        assert design_version_label(design_id) == _design_label(design_id)


# ---------------------------------------------------------------------------
# 8. Geometry member validation — the round-1 fail-open (F-A1/F-A2/F-A6).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestGeometryValidation:
    """Malformed geometry is refused at the WRITE boundary, as a 422.

    Round 1 validated nothing before the DB. Two consequences, both reproduced
    end-to-end before this class existed:

    1. 🔴 **``NaN``/``Infinity`` were accepted and permanently poisoned the
       design.** PostgreSQL ``NUMERIC`` takes them, Python's ``json.loads``
       accepts the bare tokens *by default* (that is what ``Request.json()``
       calls), and ``_json_native``'s ``float(Decimal)`` hands them back on
       read. Starlette's ``JSONResponse.render`` uses ``allow_nan=False``, so
       ONE poisoned node made the entire topology response raise — for every
       reader of that design, for good, since no delete path exists.
       ``json.dumps`` without that flag is no better: bare ``NaN`` is not RFC
       8259 and Copper's ``JSON.parse`` throws on it.

    2. Every other malformed member surfaced as **500 / ``-32603``**:
       ``CheckViolationError`` for a bad ``rack_face``, ``DataError`` for a
       string coordinate, ``NumericValueOutOfRangeError`` for an out-of-range
       ``rack_position``. None of those is a ``ValueError``, so all of them
       escaped both surfaces' ``except ValueError`` branch — while the route's
       own docstring promised "422 on a missing/malformed argument". An
       internal-error code tells a client to retry; these are permanently
       unretryable.

    3. 🔴 **Round 2's own fix reopened the same hole.** It guarded with
       ``math.isfinite``, which coerces via ``__float__``, so a Python ``int``
       too large for a double raised ``OverflowError`` — an ``ArithmeticError``,
       not a ``ValueError`` — on all four numeric members, straight off the wire
       (``json.loads`` yields arbitrary-precision ints). Same class, same
       boundary, same symptom. The parametrize list below therefore is **not**
       "the round-1 table"; it is that table PLUS the round-2 escape PLUS the
       magnitude cases, and it says so because an earlier version of this
       docstring claimed the smaller thing and was wrong.

    A large-but-finite value is not merely an error-shape problem: ``NUMERIC``
    stores ``10**400`` happily and the read path converts back with
    ``float(Decimal)``, which returns ``inf`` rather than raising — so a stored
    huge number poisons a design exactly the way a stored ``NaN`` did. That is
    why the bound is the IEEE double maximum and not something larger.

    The guard mirrors ``nce/admin_handlers/_shared.py``'s ``math.isfinite``
    check deliberately rather than importing it: this module's invariant is
    "dependencies point inward — no web/HTTP/admin/MCP imports". ``_shared.py``
    *neutralises* on the way out because by then the value is stored; here, at
    the write boundary, it is *refused*, because a stored NaN cannot be undone.
    """

    @pytest.mark.parametrize("member", ["x", "y", "rack_position", "cable_length_m"])
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    async def test_non_finite_numbers_are_refused_at_the_write_boundary(
        self, member: str, bad: float, pg_pool: Any, make_namespace: Any
    ) -> None:
        """Every numeric member, every non-finite value, refused as a ValueError.

        ``ValueError`` and not something else: it is what ``@mcp_handler`` maps
        to ``-32602``/``invalid_arguments`` and what the REST routes map to 422.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            with pytest.raises(ValueError):
                await upsert_node_geometry(conn, ns_id, _DEVICE_LABEL, {member: bad})
        assert await _geometry_row_count(pg_pool, ns_id) == 0, (
            "the refused write still created a row"
        )

    async def test_a_stored_nan_would_have_broken_every_later_read(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """The blast radius, asserted rather than asserted-about.

        Without the guard this is what happened: the write succeeded, and the
        design's topology could no longer be serialised by anyone. This test
        writes a LEGAL design, proves its topology serialises under exactly the
        encoder Starlette uses, and then proves the illegal write is refused —
        so a regression shows up as a serialisation failure here, not only as a
        missing exception.
        """
        import json as _json

        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _author_tenant(engine, ns_id, "ALPHA")

        payload = await _dispatch_ok(
            engine, _READ_TOOL, {"namespace_id": str(ns_id), "design_id": _DESIGN_ID}
        )
        # allow_nan=False is what JSONResponse.render uses. A single NaN
        # anywhere in the design makes this raise for EVERY reader.
        _json.dumps(payload, allow_nan=False)

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            with pytest.raises(ValueError):
                await upsert_node_geometry(conn, ns_id, _DEVICE_LABEL, {"x": float("nan")})

        payload_after = await _dispatch_ok(
            engine, _READ_TOOL, {"namespace_id": str(ns_id), "design_id": _DESIGN_ID}
        )
        _json.dumps(payload_after, allow_nan=False)
        assert payload_after["geometry"] == payload["geometry"], (
            "the refused write changed the stored geometry"
        )

    @pytest.mark.parametrize(
        "geometry",
        [
            {"rack_face": "left"},
            {"rack_face": "FRONT"},
            {"rack_face": 1},
            {"x": "abc"},
            {"x": "12.5"},
            {"cable_length_m": "5m"},
            {"rack_position": 1000.0},
            {"rack_position": -1000.0},
            {"rack_position": 1.27},
            {"x": True},
            {"y": False},
            {"cable_type": 7},
            {"meta": "not-an-object"},
            {"rackPosition": 1.0},
            {"z": 1.0},
            # Round-3: the OverflowError escape, on every numeric member.
            # 10**400 is what json.loads returns for 400 digits of "9"; it is an
            # ordinary Python int, and math.isfinite() raised ArithmeticError on
            # it. Also checked as a Decimal and as a float that is already inf.
            {"x": 10**400},
            {"y": 10**400},
            {"rack_position": 10**400},
            {"cable_length_m": 10**400},
            {"x": -(10**400)},
            {"x": 2**1024},
            {"x": Decimal("1e400")},
            {"cable_length_m": Decimal("-1e400")},
            # Just past the largest finite double: the smallest magnitude that
            # would read back as inf. The legal-values test pins the value just
            # inside it, so this pair brackets the boundary.
            {"x": Decimal("1.7976931348623159e308")},
            # 🔴 THE DISCRIMINATOR for the magnitude comparison itself.
            #
            # Every other literal in this file has at most 28 significant
            # digits, which is exactly the default decimal context precision —
            # so `abs(d)`, which ROUNDS to that precision, is a no-op on all of
            # them and a comparison written with `abs()` passes them all. This
            # value has 309 significant digits and is the smallest integer
            # above the true double maximum, so it is the one input that tells
            # `abs()` (accepts it, then the DDL refuses it with a 500) apart
            # from `copy_abs()` (refuses it here, as a 422). Without this row
            # both the buggy and the fixed comparison are GREEN.
            {"x": int(Decimal(sys.float_info.max)) + 1},
            {"y": -(int(Decimal(sys.float_info.max)) + 1)},
            {"cable_length_m": int(Decimal(sys.float_info.max)) + 10**280},
            # Round-3: meta is the documented escape hatch and was unvalidated
            # past isinstance(dict). json.dumps defaults to allow_nan=True, so
            # these reached $9::jsonb as bare NaN/Infinity tokens and raised
            # InvalidTextRepresentationError -> 500. Nested too: the guard has
            # to be the serialiser, not a shallow scan.
            {"meta": {"a": float("nan")}},
            {"meta": {"a": float("inf")}},
            {"meta": {"a": [{"b": float("nan")}]}},
            {"meta": {"a": {"b": {"c": float("-inf")}}}},
            # rack_position's real endpoint: 999.9 fits NUMERIC(4,1) but is not
            # a half-U, so it is refused. The comments used to advertise it as
            # legal.
            {"rack_position": 999.9},
            {"rack_position": -999.9},
        ],
    )
    async def test_malformed_members_raise_ValueError_not_a_database_error(
        self, geometry: dict[str, Any], pg_pool: Any, make_namespace: Any
    ) -> None:
        """Each row of the round-1 500 table, plus the silent-coercion cases.

        ``{"x": True}`` and ``{"y": False}`` used to be ACCEPTED and stored as
        1 and 0 — ``bool`` is an ``int`` subclass, so a ``true`` on the wire
        placed a device at a coordinate nobody asked for. Refused for the same
        stated reason ``expected_version_of`` refuses it.

        ``{"rack_position": 1.27}`` used to be ACCEPTED and silently stored as
        1.3 by ``NUMERIC(4,1)`` — not a half-U either, and the caller was never
        told its device had moved. Enforcing the half-U step is what makes the
        column comment's claim true rather than aspirational.

        ``{"x": "12.5"}`` is REFUSED, deliberately: accepting both ``12.5`` and
        ``"12.5"`` would let two clients store one coordinate under two wire
        types and force every consumer to handle both forever.

        ``{"rackPosition": ...}`` and ``{"z": ...}`` are refused rather than
        silently dropped — a near-miss key otherwise earns a 200 for a value NCE
        discarded. ``meta`` is the documented escape hatch.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            with pytest.raises(ValueError) as caught:
                await upsert_node_geometry(conn, ns_id, _DEVICE_LABEL, geometry)
        assert await _geometry_row_count(pg_pool, ns_id) == 0

        # The MESSAGE, for the bool cases specifically. ``str(exc)`` is the 422
        # body the caller reads, and without this the explicit ``bool`` guard is
        # decorative: ``Decimal(str(True))`` raises ``InvalidOperation`` anyway,
        # so dropping the guard still refuses the value — it just reports it as
        # "not a representable number", which tells a client nothing it can act
        # on. Measured, not assumed: dropping the guard mutated GREEN until this
        # assertion existed.
        if any(isinstance(v, bool) for v in geometry.values()):
            assert "must be a number when supplied" in str(caught.value), (
                f"a boolean must be reported as a TYPE error, not as {str(caught.value)!r}"
            )

    @pytest.mark.parametrize(
        "geometry",
        [
            {"rack_position": 0.0},
            {"rack_position": 1.0},
            {"rack_position": 1.5},
            {"rack_position": 999.5},
            {"rack_position": -999.5},
            # The largest magnitude that survives the JSON round trip, as a
            # float AND as the equivalent int — the int path is the one that
            # used to raise OverflowError. Together with the rejected
            # 1.7976931348623159e308 above, this brackets the bound, so a guard
            # that simply refused everything large would fail here.
            {"x": sys.float_info.max},
            {"y": -sys.float_info.max},
            {"cable_length_m": int(Decimal(sys.float_info.max))},
            # meta may hold a huge INTEGER literal: jsonb round-trips it exactly
            # and json.loads returns it as int, never as a float, so it cannot
            # poison a read the way a bare NaN token would.
            {"meta": {"a": 10**400}},
            {"x": 0},
            {"x": -12.25},
            {"cable_length_m": 0},
            {"rack_face": "front"},
            {"rack_face": "rear"},
            {"cable_type": "Cat6A"},
            {"meta": {}},
        ],
    )
    async def test_legal_members_are_still_accepted(
        self, geometry: dict[str, Any], pg_pool: Any, make_namespace: Any
    ) -> None:
        """The sibling of the refusal test.

        A validator that rejected everything would pass every assertion above.
        Whole and half rack units, both signs, the boundary value 999.5, plain
        integers, zero, and both faces must all still get through.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await upsert_node_geometry(conn, ns_id, _DEVICE_LABEL, geometry)
        assert await _geometry_row_count(pg_pool, ns_id) == 1

    async def test_malformed_geometry_is_a_422_on_rest_and_invalid_arguments_on_mcp(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two surfaces, end to end — the contract the docstring promised.

        Asserting the ``ValueError`` in the core is not enough: the whole defect
        was that a non-``ValueError`` escaped past both surfaces' handlers. This
        drives the real dispatch path and the real route.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce import admin_state
        from nce.admin_handlers import system_design as routes
        from nce.mcp_errors import MCP_INTERNAL_ERROR, MCP_INVALID_PARAMS

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        monkeypatch.setattr(admin_state, "engine", engine, raising=False)
        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))

        for bad_geometry in ({"x": float("nan")}, {"rack_face": "left"}, {"x": True}):
            args = _topology_args(
                ns_id,
                devices=[{"device_ref": _DEVICE_REF, "geometry": bad_geometry}],
                connections=[],
                racks=[],
            )
            payload = await _dispatch(engine, _TOPOLOGY_TOOL, args)
            assert "error" in payload, f"{bad_geometry} was accepted"
            assert payload["error"]["code"] == MCP_INVALID_PARAMS, (
                f"{bad_geometry} surfaced as {payload['error']['code']}; "
                f"{MCP_INTERNAL_ERROR} tells a client to retry something that "
                "can never succeed"
            )
            assert payload["error"]["data"]["reason"] == "invalid_arguments"

            response = await routes.api_system_design_author_topology(_StubRequest(args))
            assert response.status_code == 422, (
                f"{bad_geometry} returned {response.status_code}; the route "
                "docstring promises 422 on a malformed argument"
            )

        assert await _geometry_row_count(pg_pool, ns_id) == 0

    async def test_an_all_null_geometry_object_writes_no_row(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F-A5: ``{"x": null}`` is ABSENCE, not an all-NULL row.

        A non-empty dict is truthy, so the round-1 ``value or None`` check let
        ``{"x": null}`` through and wrote exactly the all-NULL row ``read.py``'s
        contract prose says cannot exist — breaking the "a node absent from the
        map has never been placed" distinction the read surface promises.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))

        result = await _dispatch_ok(
            engine,
            _TOPOLOGY_TOOL,
            _topology_args(
                ns_id,
                devices=[
                    {"device_ref": "ALLNULL", "geometry": {"x": None, "y": None}},
                    {"device_ref": "PARTIAL", "geometry": {"x": None, "y": 4.5}},
                ],
                connections=[],
                racks=[],
            ),
        )
        assert result["authored"]["geometry"] == 1, (
            "an object whose every member is null is not geometry"
        )

        payload = await _dispatch_ok(
            engine, _READ_TOOL, {"namespace_id": str(ns_id), "design_id": _DESIGN_ID}
        )
        assert set(payload["geometry"]) == {f"DEVICE:{_DESIGN_ID}:PARTIAL"}
        assert payload["geometry"][f"DEVICE:{_DESIGN_ID}:PARTIAL"]["y"] == 4.5

    async def test_the_magnitude_comparison_does_not_round_its_own_operand(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """The magnitude bound must compare exactly, not to 28 digits.

        ``abs()`` on a ``Decimal`` is an ARITHMETIC operation and rounds its
        result to the active context precision — 28 significant digits by
        default — while ``MAX_FINITE_MAGNITUDE`` carries 309. Written with
        ``abs()``, the guard truncated the operand it was about to compare and
        accepted a band roughly 1.8e280 wide ABOVE the true maximum; the DDL's
        exact bound then refused those rows with a ``CheckViolationError``,
        which is not a ``ValueError`` and lands as a 500.

        This asserts the PROPERTY rather than just the outcome, so the reason a
        specific spelling is required is recorded where the requirement is:
        ``abs(M) < M`` is genuinely True for this constant, and it is the app
        and DDL bounds agreeing exactly — not merely holding the same number —
        that keeps "accepted" and "storable" the same set.
        """
        import decimal

        from nce.vertical_modules.system_design.geometry import MAX_FINITE_MAGNITUDE

        # The premise, asserted rather than assumed: on this interpreter, abs()
        # really does lose digits on this constant.
        assert decimal.getcontext().prec < len(MAX_FINITE_MAGNITUDE.as_tuple().digits)
        assert abs(MAX_FINITE_MAGNITUDE) < MAX_FINITE_MAGNITUDE, (
            "abs() no longer rounds Decimals on this interpreter; if so this "
            "test's premise is stale, not its conclusion"
        )
        assert MAX_FINITE_MAGNITUDE.copy_abs() == MAX_FINITE_MAGNITUDE

        just_over = int(MAX_FINITE_MAGNITUDE) + 1
        as_decimal = Decimal(str(just_over))
        # The construction path is exact; only the comparison was lossy.
        assert len(as_decimal.as_tuple().digits) > decimal.getcontext().prec
        assert as_decimal.copy_abs() > MAX_FINITE_MAGNITUDE
        assert not (abs(as_decimal) > MAX_FINITE_MAGNITUDE), (
            "abs() would accept this value — that is the bug this test exists for"
        )

        # End to end: refused as a 422 by the core, and no row written.
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            with pytest.raises(ValueError):
                await upsert_node_geometry(conn, ns_id, _DEVICE_LABEL, {"x": just_over})
        assert await _geometry_row_count(pg_pool, ns_id) == 0

    async def test_a_null_valued_unknown_member_is_still_refused(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``{"rackPosition": null}`` must 422, not 200.

        ``_geometry_of`` strips ``None`` members so that an all-null object
        counts as absence. That strip used to run BEFORE the unknown-member
        refusal, so a null-valued near-miss key was reduced to ``{}``, read as
        "no geometry supplied", and answered 200 with the key silently
        discarded — the exact outcome the refusal exists to prevent, reached by
        the one input that skipped it. The refusal now runs on the RAW object.

        The non-null spelling is covered by the malformed-member list above;
        this is the null spelling, and only this one was broken.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce.mcp_errors import MCP_INVALID_PARAMS

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))

        for geometry in ({"rackPosition": None}, {"z": None}, {"x": 1.5, "rackPosition": None}):
            payload = await _dispatch(
                engine,
                _TOPOLOGY_TOOL,
                _topology_args(
                    ns_id,
                    devices=[{"device_ref": "NULLKEY", "geometry": geometry}],
                    connections=[],
                    racks=[],
                ),
            )
            assert "error" in payload, f"{geometry} was accepted and the key discarded"
            assert payload["error"]["code"] == MCP_INVALID_PARAMS
            assert payload["error"]["data"]["reason"] == "invalid_arguments"

        assert await _geometry_row_count(pg_pool, ns_id) == 0

    async def test_overflow_and_meta_are_422_on_both_surfaces(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The round-3 escapes, end to end on BOTH surfaces.

        Asserting the ``ValueError`` in the core is not enough and never was:
        the whole defect class is that a non-``ValueError`` escapes past both
        surfaces' handlers into a 500 / ``-32603``. Round 2 asserted the core
        and shipped an ``OverflowError`` that did exactly that. So these drive
        the real dispatch path and the real route.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce import admin_state
        from nce.admin_handlers import system_design as routes
        from nce.mcp_errors import MCP_INTERNAL_ERROR, MCP_INVALID_PARAMS

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        monkeypatch.setattr(admin_state, "engine", engine, raising=False)
        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))

        cases = [
            {"x": 10**400},
            {"y": 10**400},
            {"rack_position": 10**400},
            {"cable_length_m": 10**400},
            {"meta": {"a": float("nan")}},
            {"meta": {"a": [{"b": float("inf")}]}},
        ]
        for bad_geometry in cases:
            args = _topology_args(
                ns_id,
                devices=[{"device_ref": _DEVICE_REF, "geometry": bad_geometry}],
                connections=[],
                racks=[],
            )
            payload = await _dispatch(engine, _TOPOLOGY_TOOL, args)
            assert "error" in payload, f"{bad_geometry} was accepted"
            assert payload["error"]["code"] == MCP_INVALID_PARAMS, (
                f"{bad_geometry} surfaced as {payload['error']['code']}; "
                f"{MCP_INTERNAL_ERROR} tells a client to retry a request that "
                "can never succeed"
            )
            assert payload["error"]["data"]["reason"] == "invalid_arguments"

            response = await routes.api_system_design_author_topology(_StubRequest(args))
            assert response.status_code == 422, (
                f"{bad_geometry} returned {response.status_code}; the route "
                "docstring promises 422 on a malformed argument"
            )

        assert await _geometry_row_count(pg_pool, ns_id) == 0

    async def test_a_geometry_entry_carries_exactly_the_contract_columns(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The geometry entry's key set IS ``GEOMETRY_COLUMNS`` — no more, no less.

        Nothing pinned this in round 1. A column added to the projection without
        a contract decision, or dropped from it, would both have been invisible.
        ``version`` must never appear: it is the other key grain.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce.vertical_modules.system_design.geometry import GEOMETRY_COLUMNS

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _author_tenant(engine, ns_id, "ALPHA")

        payload = await _dispatch_ok(
            engine, _READ_TOOL, {"namespace_id": str(ns_id), "design_id": _DESIGN_ID}
        )
        assert payload["geometry"]
        for label, entry in payload["geometry"].items():
            assert set(entry) == set(GEOMETRY_COLUMNS), (
                f"{label}'s geometry entry is {sorted(entry)}, "
                f"contract is {sorted(GEOMETRY_COLUMNS)}"
            )
            assert "version" not in entry
            assert "node_label" not in entry


# ---------------------------------------------------------------------------
# 9. The compare-and-swap's grain guard (F-A4).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestVersionBumpGrainGuard:
    async def test_the_bump_refuses_to_convert_a_geometry_row_into_a_version_row(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """``AND version IS NOT NULL`` on the CAS (F-A4).

        Without it, ``COALESCE(version, 0) + 1`` treats a GEOMETRY row sitting
        at the design label as a version row at 0 and silently converts it: its
        x/y survive but the row disappears from the geometry grain, and nothing
        reports that. With the guard the ``UPDATE`` matches nothing and the
        caller gets a loud ``VersionConflictError`` instead.

        🔴 **Unreachable through the authoring surfaces**, and deliberately so:
        every label builder is prefix-fixed and ``design_version_label`` is the
        only thing in the codebase that emits a ``DESIGN:`` label into this
        table, so no MCP or REST payload can reach this state. It IS reachable
        through this module's own public API — which is what this test uses —
        so the guard is genuinely gated rather than being disclosed as an
        ungated line, and it becomes load-bearing the day anything else writes
        here.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await upsert_node_geometry(conn, ns_id, _DESIGN_LABEL, {"x": 7.5})

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            with pytest.raises(VersionConflictError):
                await bump_design_version(conn, ns_id, _DESIGN_ID, None)

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT x, version FROM system_design_geometry
                WHERE namespace_id = $1::uuid AND node_label = $2
                """,
                str(ns_id),
                _DESIGN_LABEL,
            )
            geometry = await fetch_geometry_by_labels(conn, ns_id, [_DESIGN_LABEL])

        assert row["version"] is None, "the geometry row was converted into a version row"
        assert float(row["x"]) == 7.5
        assert _DESIGN_LABEL in geometry, "the row vanished from the geometry grain"
