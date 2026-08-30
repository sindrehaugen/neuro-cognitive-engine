"""Tests for the Assets engine's relational seed-from-BOM writer
(Module 9, Wave 2 — Batch 142 — ``nce/vertical_modules/assets/seed.py``),
migration 054's ``assets`` table.

**This wave is the RELATIONAL half only.** Batch 142 was split:
``do_seed_asset_from_bom`` writes exactly one ``assets`` row and NOTHING
else. The ``ASSET`` kg_node, the ``BOM_LINE -[installed_as]-> ASSET`` and
``ASSET -[lives_in]-> FUNCTIONAL_LOCATION`` edges, and ``ASSET``'s row in
``node-ownership.json`` are Batch 142b's. Assertion (d) below is the seam
this file exists to prove clean — that seeding an asset writes zero graph
rows — and it is written so it goes RED the moment a graph write is added to
the module.

Covers:

  (a) one row per BOM line, ``created=True``, ``lifecycle_state`` taken from
      ``asset-lifecycle.json``'s entry state (not a literal).
  (b) idempotent under replay: re-seeding the same ``bom_line_id`` returns
      the EXISTING row (``created=False``), row count stays at 1, and every
      column is byte-identical — including ``updated_at``, which an
      ``ON CONFLICT … DO UPDATE`` would have bumped.
  (c) the idempotency key is per-BOM-LINE, not per-namespace: two different
      ``bom_line_id`` values in one namespace produce two rows.
  (d) 🔴 the seam: seeding writes ZERO ``kg_nodes`` and ZERO ``kg_edges``.
      Decoy graph rows are inserted FIRST so "unchanged" is a real
      observation rather than a comparison of two zeroes.
  (e) 🔴 through a real ``nce_app`` pool (never the owner ``pg_pool``): FORCE
      RLS means a second namespace can neither SEE nor REACH the first's
      asset — it cannot read the row even when it names ns_a's
      ``namespace_id`` explicitly, cannot INSERT a row carrying ns_a's
      ``namespace_id`` (the policy's ``WITH CHECK``), and cannot DELETE at
      all (no DELETE grant).
  (f) the DB, not the Python, is the idempotency arbiter: a direct INSERT
      that bypasses ``do_seed_asset_from_bom`` entirely is still refused by
      ``assets_ns_bom_line_uq``.
  (g) the named non-blank CHECKs refuse whitespace-only identifiers on a
      direct INSERT.
  (h) 🔴 the ``namespace_id`` predicate on the replay read-back, exercised
      through the OWNER ``pg_pool`` under a real cross-namespace
      ``bom_line_id`` collision. The complement of (e): (e) proves the RLS
      POLICY defends ``nce_app``; (h) proves ``seed.py``'s own WHERE clause
      defends the owner pool, which bypasses FORCE RLS and where the
      predicate is the only defence. Nothing else in this file reaches it.

Unit-tier tests (no DB) are driven through the PUBLIC
``do_seed_asset_from_bom`` with a ``_DummyEngine`` whose ``pg_pool`` is
``None`` — every validated field is rejected before any DB call, mirroring
``tests/test_inventory_rma.py`` / ``test_inventory_stock.py``'s
``_DummyEngine`` convention.

Integration tests are ``@pytest.mark.integration``. They ARE wired into CI —
``.github/workflows/ci.yml``'s ``Integration — M9 Assets (seed-from-bom)``
step runs ``pytest tests/test_assets_seed.py -m integration``, so every test
below gates every PR, including ``(h)``'s cross-tenant read-back pin.

That wiring landed in a separate ORCHESTRATOR commit, not in this wave's own:
``ci.yml`` was outside this wave's ``Files:`` list, so the coder correctly
STOPped and reported the ratchet failure instead of silencing it, and the
orchestrator resolved it. This paragraph previously said the tests were wired
into no workflow — true when it was written, made stale by that commit, and
corrected here. Left uncorrected it would have told the next reader that
``(h)`` — this file's headline security control — gates nothing on a PR, which
is the opposite of the truth.
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
from nce.vertical_modules.assets.lifecycle import load_lifecycle_config
from nce.vertical_modules.assets.seed import do_seed_asset_from_bom, initial_lifecycle_state

# ---------------------------------------------------------------------------
# 1. Pure-logic validation (no DB) — driven through the PUBLIC entry point,
# never a reimplementation of its validators.
# ---------------------------------------------------------------------------


class _DummyEngine:
    pg_pool = None


def _base_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "namespace_id": uuid.uuid4(),
        "bom_line_id": "BOM-LINE-VALIDATE-1",
    }
    params.update(overrides)
    return params


@pytest.mark.asyncio
async def test_rejects_missing_namespace_id() -> None:
    params = _base_params()
    del params["namespace_id"]
    with pytest.raises(ValueError, match="'namespace_id' is required"):
        await do_seed_asset_from_bom(_DummyEngine(), params)


@pytest.mark.asyncio
async def test_rejects_missing_bom_line_id() -> None:
    params = _base_params()
    del params["bom_line_id"]
    with pytest.raises(ValueError, match="'bom_line_id' is required"):
        await do_seed_asset_from_bom(_DummyEngine(), params)


@pytest.mark.asyncio
async def test_rejects_blank_bom_line_id() -> None:
    """A whitespace-only identifier must not be able to occupy the
    idempotency key — the Python mirror of migration 054's
    ``assets_bom_line_id_not_blank`` CHECK."""
    with pytest.raises(ValueError, match="'bom_line_id' is required"):
        await do_seed_asset_from_bom(_DummyEngine(), _base_params(bom_line_id="   "))


def test_initial_state_is_the_config_entry_state_not_a_literal() -> None:
    """``initial_lifecycle_state`` must come from ``asset-lifecycle.json``.

    Re-derived here independently of the implementation: the returned state
    must be a declared state, must be ``STATES[0]``, and must not appear as
    any state's declared successor. Goes RED if a state name is hard-coded
    in ``seed.py`` and the JSON is later retuned.
    """
    config = load_lifecycle_config()
    states: list[str] = list(config["STATES"])
    transitions: dict[str, list[str]] = config["VALID_TRANSITIONS"]
    successors = {target for targets in transitions.values() for target in targets}

    state = initial_lifecycle_state()

    assert state in states
    assert state == states[0]
    assert state not in successors, "the entry state must be nobody's declared successor"


# ---------------------------------------------------------------------------
# Integration helpers — mirror tests/test_inventory_rma.py's helpers in shape
# (same idioms, one idiom not two).
# ---------------------------------------------------------------------------


class _EngineStub:
    def __init__(self, pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


def _app_dsn() -> str:
    """Rewrite the integration DSN onto the restricted ``nce_app`` role.

    Verbatim in shape from ``tests/test_inventory_rma.py::_app_dsn`` — the
    in-repo precedent for driving a vertical module through a REAL
    FORCE-RLS-subject connection instead of the superuser ``pg_pool``, which
    bypasses FORCE RLS and has shipped a false isolation proof three times
    (B67, B120, B130).
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


async def _fetch_asset_row(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    bom_line_id: str,
) -> asyncpg.Record:  # type: ignore[type-arg]
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM assets WHERE namespace_id = $1 AND bom_line_id = $2",
            namespace_id,
            bom_line_id,
        )
    assert row is not None
    return row


async def _count_assets(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> int:
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM assets WHERE namespace_id = $1", namespace_id
        )
    return int(count)


async def _insert_decoy_graph_rows(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Put one kg_node and one kg_edge in the namespace BEFORE the seed runs.

    Without these, "the graph is unchanged" would be a comparison of two
    zeroes and would stay green even if ``kg_nodes``/``kg_edges`` were
    unreachable for an unrelated reason.
    """
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin) "
            "VALUES ($1, 'DECOY', $2, 'agent') ON CONFLICT (label, namespace_id) DO NOTHING",
            "Decoy:assets-seam",
            namespace_id,
        )
        await conn.execute(
            "INSERT INTO kg_edges (subject_label, predicate, object_label, namespace_id, "
            "change_origin) VALUES ($1, 'decoy_of', $2, $3, 'agent') "
            "ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING",
            "Decoy:assets-seam",
            "Decoy:assets-seam-target",
            namespace_id,
        )


# ---------------------------------------------------------------------------
# (a)/(b) One row per BOM line, idempotent under replay.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_writes_one_row_and_reseed_is_a_pure_readback(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(a) + (b). Re-seeding the SAME ``bom_line_id`` with DIFFERENT field
    values returns ``created=False`` and the ORIGINAL values; row count stays
    at 1 and the full row is byte-identical before/after, ``updated_at``
    included.

    Goes RED if the ``ON CONFLICT … DO NOTHING`` becomes a ``DO UPDATE``, or
    if the DB constraint is replaced by a Python check-then-write that
    happens to overwrite.
    """
    engine = _EngineStub(pg_pool)
    expected_state = initial_lifecycle_state()

    first = await do_seed_asset_from_bom(
        engine,
        {
            "namespace_id": namespace_id,
            "bom_line_id": "BOM-LINE-IDEMPOTENT-1",
            "serial": "SN-ORIGINAL",
            "functional_location_id": "FLOC-ROOM-101",
        },
    )
    assert first["ok"] is True
    assert first["created"] is True
    assert first["lifecycle_state"] == expected_state
    assert first["serial"] == "SN-ORIGINAL"
    assert first["functional_location_id"] == "FLOC-ROOM-101"

    before = await _fetch_asset_row(pg_pool, namespace_id, "BOM-LINE-IDEMPOTENT-1")

    second = await do_seed_asset_from_bom(
        engine,
        {
            "namespace_id": namespace_id,
            "bom_line_id": "BOM-LINE-IDEMPOTENT-1",
            "serial": "SN-DIFFERENT",
            "functional_location_id": "FLOC-ROOM-999",
        },
    )
    assert second["created"] is False
    assert second["asset_id"] == first["asset_id"]
    assert second["serial"] == "SN-ORIGINAL", "must return the EXISTING row, not the new params"
    assert second["functional_location_id"] == "FLOC-ROOM-101"

    assert await _count_assets(pg_pool, namespace_id) == 1, (
        "re-seeding the same BOM line must never create a second row"
    )

    after = await _fetch_asset_row(pg_pool, namespace_id, "BOM-LINE-IDEMPOTENT-1")
    assert dict(before) == dict(after), (
        "re-seeding an existing bom_line_id must change NO column, including updated_at"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_distinct_bom_lines_each_get_their_own_asset(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(c) The idempotency key is (namespace, BOM line) — not the namespace.

    Goes RED if ``assets_ns_bom_line_uq`` is narrowed to ``namespace_id``
    alone, which would silently collapse a whole project's assets into one
    row.
    """
    engine = _EngineStub(pg_pool)

    first = await do_seed_asset_from_bom(
        engine, {"namespace_id": namespace_id, "bom_line_id": "BOM-LINE-A"}
    )
    second = await do_seed_asset_from_bom(
        engine, {"namespace_id": namespace_id, "bom_line_id": "BOM-LINE-B"}
    )

    assert first["created"] is True
    assert second["created"] is True
    assert first["asset_id"] != second["asset_id"]
    assert await _count_assets(pg_pool, namespace_id) == 2


# ---------------------------------------------------------------------------
# (d) 🔴 THE SEAM — seeding an asset writes zero graph rows. This is the
# assertion the split exists to protect, and the one Batch 142b will remove.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_writes_zero_kg_nodes_and_zero_kg_edges(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(d) 🔴 the seam. Decoy graph rows are inserted FIRST so "unchanged" is
    a real observation, not a comparison of two zeroes.

    Goes RED the instant any ``kg_nodes``/``kg_edges`` write is added to
    ``seed.py`` — which is exactly what Batch 142b will do, and it is
    supposed to have to change this test to do it.
    """
    await _insert_decoy_graph_rows(pg_pool, namespace_id)
    engine = _EngineStub(pg_pool)

    async with pg_pool.acquire() as conn:
        nodes_before = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1", namespace_id
        )
        edges_before = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_edges WHERE namespace_id = $1", namespace_id
        )
    assert nodes_before == 1, "decoy seed must be non-zero for this to prove anything"
    assert edges_before == 1, "decoy seed must be non-zero for this to prove anything"

    result = await do_seed_asset_from_bom(
        engine,
        {
            "namespace_id": namespace_id,
            "bom_line_id": "BOM-LINE-SEAM-1",
            "serial": "SN-SEAM",
            "functional_location_id": "FLOC-SEAM",
        },
    )
    assert result["ok"] is True

    async with pg_pool.acquire() as conn:
        nodes_after = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1", namespace_id
        )
        edges_after = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_edges WHERE namespace_id = $1", namespace_id
        )
        asset_nodes = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1 AND entity_type = 'ASSET'",
            namespace_id,
        )
        seed_edges = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_edges WHERE namespace_id = $1 "
            "AND predicate IN ('installed_as', 'lives_in')",
            namespace_id,
        )

    assert nodes_after == nodes_before == 1, "do_seed_asset_from_bom must write ZERO kg_nodes"
    assert edges_after == edges_before == 1, "do_seed_asset_from_bom must write ZERO kg_edges"
    assert asset_nodes == 0, "the ASSET node is Batch 142b's, not this wave's"
    assert seed_edges == 0, "installed_as / lives_in are Batch 142b's, not this wave's"


# ---------------------------------------------------------------------------
# (e) 🔴 FORCE RLS + no-DELETE grant, through a REAL nce_app pool.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nce_app_pool_isolates_namespaces_and_refuses_delete(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """(e) 🔴 driven through a REAL ``nce_app`` pool (``_app_dsn()``), never
    the superuser ``pg_pool``.

    Proves all three halves of "can neither see nor reach": ns_b cannot READ
    ns_a's row even naming ns_a's namespace_id explicitly, cannot WRITE a row
    carrying ns_a's namespace_id (the policy's ``WITH CHECK``), and no
    namespace can DELETE at all.

    Goes RED if ``ENABLE ROW LEVEL SECURITY`` or the ``tenant_isolation_policy``
    is dropped (ns_b would see the row), if ``WITH CHECK`` is dropped from the
    policy (the cross-namespace INSERT would succeed), or if the grant list
    gains ``DELETE``.

    Precisely scoped claim: this test does NOT discriminate on ``FORCE ROW
    LEVEL SECURITY``. ``FORCE`` only extends RLS to the table's OWNER role,
    and ``nce_app`` is not the owner — plain ``ENABLE`` already binds it, so
    dropping ``FORCE`` would leave this test green. ``FORCE`` is what stops
    the OWNER pool from bypassing the policy, and it is verified where it is
    observable: ``pg_class.relforcerowsecurity`` in the schema-vs-migration
    catalog diff (both paths report ``t``).
    """
    ns_a = await make_namespace()
    ns_b = await make_namespace()

    app_pool = await asyncpg.create_pool(_app_dsn(), min_size=1, max_size=2)
    engine = _EngineStub(app_pool)
    try:
        result = await do_seed_asset_from_bom(
            engine,
            {
                "namespace_id": ns_a,
                "bom_line_id": "BOM-LINE-RLS-1",
                "serial": "SN-RLS",
                "functional_location_id": "FLOC-RLS",
            },
        )
        asset_id = uuid.UUID(result["asset_id"])

        # ns_a sees its own row through nce_app...
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_a)
            visible_from_a = await conn.fetchval(
                "SELECT COUNT(*) FROM assets WHERE namespace_id = $1", ns_a
            )
        assert visible_from_a == 1, "ns_a must see the row do_seed_asset_from_bom wrote"

        # ...ns_b does not, even asking for ns_a's namespace_id EXPLICITLY —
        # RLS, not a WHERE clause, is what refuses this.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_b)
            visible_from_b = await conn.fetchval(
                "SELECT COUNT(*) FROM assets WHERE namespace_id = $1", ns_a
            )
        assert visible_from_b == 0, "ns_b must not see ns_a's assets row"

        # ...and ns_b cannot REACH into ns_a either: the policy's WITH CHECK
        # refuses an INSERT carrying another tenant's namespace_id.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_b)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    "INSERT INTO assets (namespace_id, bom_line_id, lifecycle_state) "
                    "VALUES ($1, 'BOM-LINE-CROSS-TENANT', 'PROPOSED')",
                    ns_a,
                )

        # No DELETE grant — retirement is a lifecycle state, never an erased row.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_a)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("DELETE FROM assets WHERE id = $1", asset_id)
    finally:
        await app_pool.close()


# ---------------------------------------------------------------------------
# (f)/(g) The DB constraints stand on their own, with the Python bypassed.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unique_constraint_refuses_a_duplicate_direct_insert(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(f) The idempotency arbiter is ``assets_ns_bom_line_uq``, not the
    Python. This INSERT never calls ``do_seed_asset_from_bom``, so the
    module's ``ON CONFLICT`` cannot mask a dropped constraint.

    Goes RED if the UNIQUE is removed from migration 054 — at which point
    concurrent seeds could both insert.
    """
    engine = _EngineStub(pg_pool)
    await do_seed_asset_from_bom(
        engine, {"namespace_id": namespace_id, "bom_line_id": "BOM-LINE-UQ-1"}
    )

    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO assets (namespace_id, bom_line_id, lifecycle_state) "
                "VALUES ($1, 'BOM-LINE-UQ-1', 'PROPOSED')",
                namespace_id,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_named_non_blank_checks_refuse_whitespace_identifiers(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(g) ``assets_bom_line_id_not_blank`` / ``assets_serial_not_blank``
    stand without the Python validators.

    Goes RED if either named CHECK is dropped from migration 054 or from
    ``schema.sql`` — the two paths are also compared constraint-by-constraint
    in this wave's report.
    """
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO assets (namespace_id, bom_line_id, lifecycle_state) "
                "VALUES ($1, '   ', 'PROPOSED')",
                namespace_id,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO assets (namespace_id, bom_line_id, serial, lifecycle_state) "
                "VALUES ($1, 'BOM-LINE-BLANK-SERIAL', '  ', 'PROPOSED')",
                namespace_id,
            )


# ---------------------------------------------------------------------------
# (h) 🔴 THE namespace_id PREDICATE ON THE REPLAY READ-BACK — seed.py's ONLY
# namespace_id predicate, and until this test the only defence in the module
# that nothing exercised.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_readback_is_namespace_scoped_not_bom_line_scoped(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """(h) 🔴 pins ``WHERE namespace_id = $1::uuid AND bom_line_id = $2`` on
    ``seed.py``'s fallback SELECT.

    Every other test in this file leaves that predicate unexercised, which is
    precisely why this one is needed:

    * the fallback SELECT is reachable ONLY on an ``ON CONFLICT`` replay;
    * (e), the cross-namespace test, never replays — seeding the same BOM
      line under a second ``namespace_id`` INSERTs (the UNIQUE is on the
      PAIR), so ``created`` is ``True`` and the SELECT never runs;
    * (b), the one test that does replay, uses a single namespace, and every
      test in this file uses a unique ``bom_line_id`` — so ``bom_line_id``
      alone is a perfect discriminator there and the ``namespace_id``
      predicate is redundant;
    * (e) additionally runs through ``nce_app``, where the RLS POLICY refuses
      a cross-tenant read whether or not the predicate is present, so it
      cannot discriminate on the predicate even in principle.

    This test does the one thing none of those do: it creates a genuine
    ``bom_line_id`` COLLISION ACROSS namespaces and THEN replays — through
    the OWNER ``pg_pool``, which is ``Superuser, Bypass RLS``, so the
    module's own WHERE clause is the only thing between the replay and
    another tenant's row.

    Goes RED if ``AND namespace_id = $1::uuid`` is weakened or dropped: the
    replay then reads whichever colliding row the planner reaches first and
    returns a foreign tenant's ``asset_id``/``serial``. BOTH namespaces are
    replayed, so the assertion does not depend on which of the two an
    unscoped SELECT happens to return.
    """
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    engine = _EngineStub(pg_pool)
    shared_bom_line = "BOM-LINE-NS-COLLISION"

    # The SAME bom_line_id in two namespaces. Both INSERT — the unique is on
    # (namespace_id, bom_line_id) — so neither of these is the replay yet.
    first_a = await do_seed_asset_from_bom(
        engine,
        {"namespace_id": ns_a, "bom_line_id": shared_bom_line, "serial": "SN-TENANT-A"},
    )
    first_b = await do_seed_asset_from_bom(
        engine,
        {"namespace_id": ns_b, "bom_line_id": shared_bom_line, "serial": "SN-TENANT-B"},
    )
    assert first_a["created"] is True
    assert first_b["created"] is True
    assert first_a["asset_id"] != first_b["asset_id"], (
        "this test needs a REAL collision: two distinct rows sharing one bom_line_id"
    )

    # NOW the replay — the only path that reaches the fallback SELECT.
    replay_b = await do_seed_asset_from_bom(
        engine,
        {"namespace_id": ns_b, "bom_line_id": shared_bom_line, "serial": "SN-TENANT-B"},
    )
    assert replay_b["created"] is False, "re-seeding a seeded line must be a replay"
    assert replay_b["asset_id"] == first_b["asset_id"], (
        "the replay read back ANOTHER namespace's asset — the fallback SELECT's "
        "namespace_id predicate is missing or ineffective"
    )
    assert replay_b["serial"] == "SN-TENANT-B"

    # ...and the other direction, so this holds whichever of the two colliding
    # rows an unscoped SELECT would have reached first.
    replay_a = await do_seed_asset_from_bom(
        engine,
        {"namespace_id": ns_a, "bom_line_id": shared_bom_line, "serial": "SN-TENANT-A"},
    )
    assert replay_a["created"] is False
    assert replay_a["asset_id"] == first_a["asset_id"], (
        "the replay read back ANOTHER namespace's asset — the fallback SELECT's "
        "namespace_id predicate is missing or ineffective"
    )
    assert replay_a["serial"] == "SN-TENANT-A"
