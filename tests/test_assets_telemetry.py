"""Tests for the Assets engine's telemetry adapter + pull
(Module 9, Wave 5 — Batch 145 —
``nce/vertical_modules/assets/telemetry.py``), migration 057's
``telemetry_samples`` table.

**This wave writes NO graph.** A telemetry sample is a ROW IN A TABLE, not a
``kg_node`` and not a ``kg_edge`` — even though
``docs/vertical_engines/09-assets-engine.md`` describes ``do_pull_telemetry``
as writing ``TELEMETRY`` nodes and ``monitored_by`` edges. Assertion (c) is
the seam this file exists to keep clean, and it is written so it goes RED the
moment a graph write is added to the module.

Covers:

  (a) the mock adapter's samples land as rows: one per (metric, instant),
      values and instants re-derived from the adapter independently of the
      module, ``sampled_at`` = the VENDOR instant (never ``now()``).
  (b) idempotent under replay: re-pulling reports ``written=0`` /
      ``duplicates=n``, the row count does not move and every row is
      byte-identical, ``created_at`` included.
  (c) 🔴 the graph seam: a pull writes ZERO ``kg_nodes`` and ZERO
      ``kg_edges``. Decoy graph rows are inserted FIRST so "unchanged" is a
      real observation and not a comparison of two zeroes.
  (d) 🔴 the ADAPTER seam: flipping ``NCE_ASSETS_TELEMETRY_CRESTRON_REAL``
      makes the pull raise ``NotImplementedError`` and write nothing. This is
      what proves ``do_pull_telemetry`` genuinely goes THROUGH
      ``TelemetryAdapter`` rather than reaching around it — an inlined mock
      would ignore the flag entirely.
  (e) 🔴 through a real ``nce_app`` pool (never the owner ``pg_pool``): a
      second namespace can neither SEE ns_a's samples — even naming ns_a's
      ``namespace_id`` explicitly — nor INSERT a row carrying ns_a's
      ``namespace_id`` (the policy's ``WITH CHECK``); and NO namespace may
      UPDATE or DELETE at all (no such grants).
  (f) 🔴 the namespace predicate on the asset pre-check, through the OWNER
      ``pg_pool`` (``Superuser, Bypass RLS``), where the module's own WHERE
      clause is the only defence. The complement of (e).
  (g)/(h)/(i) the DB constraints stand with the Python bypassed: the UNIQUE
      refuses a duplicate direct INSERT, the named CHECKs refuse a blank
      metric and NaN/±Infinity, and the FK refuses an unknown asset.
  (j) the Python mirror of those CHECKs fires BEFORE the DB, via an adapter
      injected over the factory — and writes nothing when it does.

Unit-tier tests (no DB) drive the PUBLIC entry points
(``do_pull_telemetry``, ``select_telemetry_adapter``) with a ``_DummyEngine``
whose ``pg_pool`` is ``None``: every validated field and the platform key are
resolved before any DB call. Mirrors ``tests/test_assets_seed.py``'s
``_DummyEngine`` convention.

Integration tests are ``@pytest.mark.integration``. They are wired into CI by
``.github/workflows/ci.yml``'s ``Integration — M9 Assets
(tests/test_assets_*.py)`` step, which runs ``pytest tests/test_assets_*.py -m
integration``: Batch 152a replaced that step's hardcoded file list with this
prefix glob, so this file needed no ``ci.yml`` edit and none was made. That
glob was READ in ``ci.yml`` before this paragraph was written, not assumed.
"""

from __future__ import annotations

import math
import os
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.config import cfg
from nce.vertical_modules.assets import telemetry as telemetry_module
from nce.vertical_modules.assets.seed import do_seed_asset_from_bom
from nce.vertical_modules.assets.telemetry import (
    MOCK_PLATFORM,
    VENDOR_PLATFORMS,
    MockTelemetryAdapter,
    TelemetryAdapter,
    TelemetrySample,
    UnimplementedVendorAdapter,
    do_pull_telemetry,
    real_adapter_env_key,
    select_telemetry_adapter,
)

# ---------------------------------------------------------------------------
# 1. Pure-logic tests (no DB) — driven through the PUBLIC entry points.
# ---------------------------------------------------------------------------


class _DummyEngine:
    pg_pool = None


def _base_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "namespace_id": uuid.uuid4(),
        "asset_id": uuid.uuid4(),
    }
    params.update(overrides)
    return params


@pytest.mark.asyncio
async def test_rejects_missing_namespace_id() -> None:
    params = _base_params()
    del params["namespace_id"]
    with pytest.raises(ValueError, match="'namespace_id' is required"):
        await do_pull_telemetry(_DummyEngine(), params)


@pytest.mark.asyncio
async def test_rejects_missing_asset_id() -> None:
    params = _base_params()
    del params["asset_id"]
    with pytest.raises(ValueError, match="'asset_id' is required"):
        await do_pull_telemetry(_DummyEngine(), params)


@pytest.mark.asyncio
async def test_unknown_platform_is_refused_before_any_db_call() -> None:
    """A typo'd platform must not silently fall back to the mock — that would
    serve fabricated numbers under a real vendor's name.

    ``_DummyEngine.pg_pool`` is ``None``, so reaching the DB at all would
    raise ``AttributeError`` instead of this ``ValueError``.
    """
    with pytest.raises(ValueError, match="unknown telemetry platform 'crestronn'"):
        await do_pull_telemetry(_DummyEngine(), _base_params(platform="crestronn"))


def test_the_five_vendor_platforms_are_exactly_the_documented_set() -> None:
    """``09-assets-engine.md`` names crestron/qsys/neat/huddly/poly. Pinning
    the whole set — not a sample of it — so a dropped or renamed platform is
    caught rather than discovered by an operator whose env key stops working.
    """
    assert set(VENDOR_PLATFORMS) == {"crestron", "qsys", "neat", "huddly", "poly"}
    assert MOCK_PLATFORM not in VENDOR_PLATFORMS


@pytest.mark.parametrize("platform", sorted(VENDOR_PLATFORMS))
def test_vendor_platform_is_the_mock_while_its_swap_flag_is_unset(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mock-now: every one of the five resolves to the mock by default, so the
    engine is usable before any vendor key lands.

    All five are asserted, not one — the flag name is derived per platform and
    a per-platform typo would otherwise hide here.
    """
    monkeypatch.delenv(real_adapter_env_key(platform), raising=False)
    adapter = select_telemetry_adapter(platform)
    assert isinstance(adapter, MockTelemetryAdapter)
    assert adapter.platform == MOCK_PLATFORM


@pytest.mark.parametrize("platform", sorted(VENDOR_PLATFORMS))
@pytest.mark.parametrize("flag", ["1", "true", "YES", "on"])
def test_vendor_platform_swaps_to_its_real_adapter_when_the_flag_is_set(
    platform: str, flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swap-ready: the env key flips mock → real for that platform ONLY, and
    the real adapter is a declared stub that raises rather than degrading."""
    monkeypatch.setenv(real_adapter_env_key(platform), flag)
    adapter = select_telemetry_adapter(platform)
    assert isinstance(adapter, UnimplementedVendorAdapter)
    assert adapter.platform == platform

    # Every OTHER platform is untouched by this one's flag.
    for other in VENDOR_PLATFORMS:
        if other == platform:
            continue
        monkeypatch.delenv(real_adapter_env_key(other), raising=False)
        assert isinstance(select_telemetry_adapter(other), MockTelemetryAdapter)


def test_real_adapter_env_key_is_the_documented_shape() -> None:
    """``NCE_ASSETS_TELEMETRY_<PLATFORM>_REAL`` — 09-assets-engine.md
    "Config keys". Pinned because it is an OPERATOR-facing contract: renaming
    it silently turns every deployment's swap back off."""
    assert real_adapter_env_key("crestron") == "NCE_ASSETS_TELEMETRY_CRESTRON_REAL"
    assert real_adapter_env_key("qsys") == "NCE_ASSETS_TELEMETRY_QSYS_REAL"


@pytest.mark.asyncio
async def test_the_vendor_stub_raises_and_names_its_api_and_its_env_key() -> None:
    """A stub must fail LOUDLY and tell the operator both what is missing and
    how to get back to the mock."""
    adapter = UnimplementedVendorAdapter("poly", VENDOR_PLATFORMS["poly"])
    with pytest.raises(NotImplementedError) as excinfo:
        await adapter.fetch_samples(uuid.uuid4())
    message = str(excinfo.value)
    assert "poly" in message
    assert VENDOR_PLATFORMS["poly"] in message
    assert "NCE_ASSETS_TELEMETRY_POLY_REAL" in message


def test_telemetry_adapter_is_abstract() -> None:
    """The abstraction cannot be instantiated — a subclass that forgets
    ``fetch_samples`` fails at construction, not at pull time."""
    with pytest.raises(TypeError):
        TelemetryAdapter()  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_mock_history_is_stable_per_asset_and_differs_between_assets() -> None:
    """The property the whole idempotency claim rests on.

    If the mock stamped ``now()``, every re-pull would look like new data and
    (b) would be vacuous. Both halves are asserted: stable for one asset,
    different between two.
    """
    adapter = MockTelemetryAdapter()
    asset_a = uuid.uuid4()
    asset_b = uuid.uuid4()

    first = list(await adapter.fetch_samples(asset_a))
    second = list(await adapter.fetch_samples(asset_a))
    other = list(await adapter.fetch_samples(asset_b))

    assert first == second, "the mock must return a FIXED history, never a moving one"
    assert first != [], "an empty mock history would make every downstream test vacuous"
    assert all(math.isfinite(s.value) for s in first)
    assert [s.metric for s in first] == [s.metric for s in other]
    assert [s.value for s in first] != [s.value for s in other], (
        "two assets must not report identical readings"
    )


# ---------------------------------------------------------------------------
# Integration helpers — mirror tests/test_assets_seed.py's helpers in shape.
# ---------------------------------------------------------------------------


class _EngineStub:
    def __init__(self, pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


class _FixedAdapter(TelemetryAdapter):
    """Test double injected OVER the factory, to reach code the env swap
    cannot: a payload that a real vendor could send but the mock never does."""

    def __init__(self, samples: Sequence[TelemetrySample]) -> None:
        self._samples = list(samples)

    @property
    def platform(self) -> str:
        return "fixture"

    async def fetch_samples(self, asset_id: uuid.UUID) -> Sequence[TelemetrySample]:
        return self._samples


def _app_dsn() -> str:
    """Rewrite the integration DSN onto the restricted ``nce_app`` role.

    Verbatim in shape from ``tests/test_assets_seed.py::_app_dsn`` — the
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


async def _seed_asset(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    bom_line_id: str,
) -> uuid.UUID:
    """Create the asset a sample must hang off, through Wave 2's own writer."""
    result = await do_seed_asset_from_bom(
        _EngineStub(pg_pool), {"namespace_id": namespace_id, "bom_line_id": bom_line_id}
    )
    return uuid.UUID(result["asset_id"])


async def _fetch_samples(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    asset_id: uuid.UUID,
) -> list[asyncpg.Record]:  # type: ignore[type-arg]
    async with pg_pool.acquire() as conn:
        return list(
            await conn.fetch(
                "SELECT * FROM telemetry_samples WHERE namespace_id = $1 AND asset_id = $2 "
                "ORDER BY metric",
                namespace_id,
                asset_id,
            )
        )


async def _count_samples(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> int:
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM telemetry_samples WHERE namespace_id = $1", namespace_id
        )
    return int(count)


async def _insert_decoy_graph_rows(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Put one kg_node and one kg_edge in the namespace BEFORE the pull runs.

    Without these, "the graph is unchanged" would be a comparison of two
    zeroes and would stay green even if ``kg_nodes``/``kg_edges`` were
    unreachable for an unrelated reason.
    """
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin) "
            "VALUES ($1, 'DECOY', $2, 'agent') ON CONFLICT (label, namespace_id) DO NOTHING",
            "Decoy:assets-telemetry-seam",
            namespace_id,
        )
        await conn.execute(
            "INSERT INTO kg_edges (subject_label, predicate, object_label, namespace_id, "
            "change_origin) VALUES ($1, 'decoy_of', $2, $3, 'agent') "
            "ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING",
            "Decoy:assets-telemetry-seam",
            "Decoy:assets-telemetry-seam-target",
            namespace_id,
        )


# ---------------------------------------------------------------------------
# (a)/(b) The mock's samples land as rows; re-pulling is a pure no-op.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mock_pull_writes_one_row_per_sample_with_the_vendor_instant(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(a) Every field is re-derived from ``MockTelemetryAdapter`` itself, so
    this compares the STORED row against the ADAPTER's output rather than
    against constants copied out of the module.

    ``sampled_at`` is asserted to equal the adapter's instant, which is a
    fixed epoch in the past — so a module that stamped ``now()`` instead of
    carrying the vendor instant through goes RED here.
    """
    engine = _EngineStub(pg_pool)
    asset_id = await _seed_asset(pg_pool, namespace_id, "BOM-LINE-TELEMETRY-1")
    expected = list(await MockTelemetryAdapter().fetch_samples(asset_id))
    assert expected, "the mock must yield samples or this test proves nothing"

    result = await do_pull_telemetry(engine, {"namespace_id": namespace_id, "asset_id": asset_id})

    assert result["ok"] is True
    assert result["platform"] == MOCK_PLATFORM
    assert result["adapter_platform"] == MOCK_PLATFORM
    assert result["pulled"] == len(expected)
    assert result["written"] == len(expected)
    assert result["duplicates"] == 0

    rows = await _fetch_samples(pg_pool, namespace_id, asset_id)
    assert len(rows) == len(expected)
    by_metric = {row["metric"]: row for row in rows}
    for sample in expected:
        row = by_metric[sample.metric]
        assert row["value"] == pytest.approx(sample.value)
        assert row["sampled_at"] == sample.sampled_at, (
            "sampled_at must be the VENDOR instant, not the pull instant"
        )
        assert row["namespace_id"] == namespace_id
        assert row["asset_id"] == asset_id
        assert row["change_origin"] == "agent"
        assert row["created_at"] > sample.sampled_at, (
            "created_at is the pull instant and must be distinct from sampled_at"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repulling_the_same_asset_adds_no_rows_and_changes_none(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(b) The claim: a telemetry cron re-reads overlapping windows, and the
    same reading must land exactly once.

    Goes RED if ``ON CONFLICT … DO NOTHING`` is removed (the second pull would
    raise a UniqueViolation), if it becomes ``DO UPDATE`` (``created_at``
    would move), or if ``telemetry_samples_idempotency_uq`` is dropped (the
    row count would double).
    """
    engine = _EngineStub(pg_pool)
    asset_id = await _seed_asset(pg_pool, namespace_id, "BOM-LINE-TELEMETRY-REPLAY")

    first = await do_pull_telemetry(engine, {"namespace_id": namespace_id, "asset_id": asset_id})
    before = [dict(row) for row in await _fetch_samples(pg_pool, namespace_id, asset_id)]

    second = await do_pull_telemetry(engine, {"namespace_id": namespace_id, "asset_id": asset_id})

    assert second["pulled"] == first["pulled"]
    assert second["written"] == 0
    assert second["duplicates"] == first["pulled"]

    after = [dict(row) for row in await _fetch_samples(pg_pool, namespace_id, asset_id)]
    assert len(after) == first["written"]
    assert await _count_samples(pg_pool, namespace_id) == first["written"]
    assert before == after, "a re-pull must change NO column, including created_at and the row ids"


# ---------------------------------------------------------------------------
# (c) 🔴 THE GRAPH SEAM — a pull writes zero kg_nodes and zero kg_edges.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pull_writes_zero_kg_nodes_and_zero_kg_edges(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(c) 🔴 the seam. Decoy graph rows are inserted FIRST so "unchanged" is a
    real observation, not a comparison of two zeroes.

    Goes RED the instant any ``kg_nodes``/``kg_edges`` write is added to
    ``telemetry.py`` — which a later projection wave will do, and it is
    supposed to have to change this test to do it.
    """
    await _insert_decoy_graph_rows(pg_pool, namespace_id)
    engine = _EngineStub(pg_pool)
    asset_id = await _seed_asset(pg_pool, namespace_id, "BOM-LINE-TELEMETRY-SEAM")

    async with pg_pool.acquire() as conn:
        nodes_before = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1", namespace_id
        )
        edges_before = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_edges WHERE namespace_id = $1", namespace_id
        )
    assert nodes_before == 1, "decoy seed must be non-zero for this to prove anything"
    assert edges_before == 1, "decoy seed must be non-zero for this to prove anything"

    result = await do_pull_telemetry(engine, {"namespace_id": namespace_id, "asset_id": asset_id})
    assert result["written"] > 0, "the pull must actually have done something"

    async with pg_pool.acquire() as conn:
        nodes_after = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1", namespace_id
        )
        edges_after = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_edges WHERE namespace_id = $1", namespace_id
        )
        telemetry_nodes = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1 AND entity_type = 'TELEMETRY'",
            namespace_id,
        )
        monitored_by = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_edges WHERE namespace_id = $1 AND predicate = 'monitored_by'",
            namespace_id,
        )

    assert nodes_after == nodes_before == 1, "do_pull_telemetry must write ZERO kg_nodes"
    assert edges_after == edges_before == 1, "do_pull_telemetry must write ZERO kg_edges"
    assert telemetry_nodes == 0, "the TELEMETRY node is a later projection wave's"
    assert monitored_by == 0, "ASSET -[monitored_by]-> TELEMETRY is a later wave's"


# ---------------------------------------------------------------------------
# (d) 🔴 THE ADAPTER SEAM — the env swap must be able to break the pull.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enabling_a_vendors_real_adapter_makes_the_pull_fail_and_write_nothing(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) 🔴 the adapter seam, and the reason this file can claim
    ``do_pull_telemetry`` goes THROUGH ``TelemetryAdapter``.

    A module that reached around the interface — inlining the mock's history,
    or importing ``MockTelemetryAdapter`` directly in the pull path — would
    ignore ``NCE_ASSETS_TELEMETRY_CRESTRON_REAL`` completely and quietly write
    three mock rows here. Instead the pull must raise and leave the table
    empty. Both halves are asserted; the row count is the half that catches a
    swallowed exception.
    """
    monkeypatch.setenv("NCE_ASSETS_TELEMETRY_CRESTRON_REAL", "1")
    engine = _EngineStub(pg_pool)
    asset_id = await _seed_asset(pg_pool, namespace_id, "BOM-LINE-TELEMETRY-SWAP")

    with pytest.raises(NotImplementedError, match="crestron"):
        await do_pull_telemetry(
            engine,
            {"namespace_id": namespace_id, "asset_id": asset_id, "platform": "crestron"},
        )

    assert await _count_samples(pg_pool, namespace_id) == 0, (
        "a failed real-adapter pull must write nothing at all"
    )

    # ...and with the flag off, the SAME call succeeds through the mock. This
    # is what makes the assertion above about the FLAG and not about the
    # platform name being rejected outright.
    monkeypatch.delenv("NCE_ASSETS_TELEMETRY_CRESTRON_REAL")
    fallback = await do_pull_telemetry(
        engine,
        {"namespace_id": namespace_id, "asset_id": asset_id, "platform": "crestron"},
    )
    assert fallback["platform"] == "crestron"
    assert fallback["adapter_platform"] == MOCK_PLATFORM
    assert fallback["written"] > 0


# ---------------------------------------------------------------------------
# (e) 🔴 RLS + the missing UPDATE/DELETE grants, through a REAL nce_app pool.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nce_app_pool_isolates_namespaces_and_refuses_update_and_delete(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """(e) 🔴 driven through a REAL ``nce_app`` pool (``_app_dsn()``), never
    the superuser ``pg_pool``.

    Proves: ns_b cannot READ ns_a's samples even naming ns_a's namespace_id
    explicitly; ns_b cannot WRITE a row carrying ns_a's namespace_id (the
    policy's ``WITH CHECK``); and no namespace may UPDATE (a reading is not
    revisable) or DELETE (an observation is not erasable) at all.

    Goes RED if ``ENABLE ROW LEVEL SECURITY`` or ``tenant_isolation_policy``
    is dropped, if ``WITH CHECK`` is dropped from the policy, or if the grant
    list gains UPDATE or DELETE.

    Precisely scoped claim: this test does NOT discriminate on ``FORCE ROW
    LEVEL SECURITY``. ``FORCE`` only extends RLS to the table's OWNER role and
    ``nce_app`` is not the owner — plain ``ENABLE`` already binds it, so
    dropping ``FORCE`` would leave this test green. ``FORCE`` is what stops
    the OWNER pool bypassing the policy, and it is observable only in
    ``pg_class.relforcerowsecurity``, asserted separately below.
    """
    ns_a = await make_namespace()
    ns_b = await make_namespace()

    app_pool = await asyncpg.create_pool(_app_dsn(), min_size=1, max_size=2)
    engine = _EngineStub(app_pool)
    try:
        asset_id = await _seed_asset(app_pool, ns_a, "BOM-LINE-TELEMETRY-RLS")
        result = await do_pull_telemetry(engine, {"namespace_id": ns_a, "asset_id": asset_id})
        assert result["written"] > 0

        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_a)
            visible_from_a = await conn.fetchval(
                "SELECT COUNT(*) FROM telemetry_samples WHERE namespace_id = $1", ns_a
            )
            sample_id = await conn.fetchval(
                "SELECT id FROM telemetry_samples WHERE namespace_id = $1 LIMIT 1", ns_a
            )
        assert visible_from_a == result["written"]

        # ns_b cannot see them, even asking for ns_a's namespace_id EXPLICITLY
        # — RLS, not a WHERE clause, is what refuses this.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_b)
            visible_from_b = await conn.fetchval(
                "SELECT COUNT(*) FROM telemetry_samples WHERE namespace_id = $1", ns_a
            )
        assert visible_from_b == 0, "ns_b must not see ns_a's telemetry_samples"

        # ...and ns_b cannot REACH into ns_a either: the policy's WITH CHECK
        # refuses an INSERT carrying another tenant's namespace_id.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_b)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    "INSERT INTO telemetry_samples "
                    "(namespace_id, asset_id, metric, value, sampled_at) "
                    "VALUES ($1, $2, 'cross_tenant', 1.0, now())",
                    ns_a,
                    asset_id,
                )

        # No UPDATE grant — a reading that was taken is not revisable.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_a)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    "UPDATE telemetry_samples SET value = 0 WHERE id = $1", sample_id
                )

        # No DELETE grant — an observation is not erasable.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_a)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("DELETE FROM telemetry_samples WHERE id = $1", sample_id)
    finally:
        await app_pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_force_row_level_security_is_on_in_the_catalog(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
) -> None:
    """The one place ``FORCE`` is observable, since (e) provably cannot see it.

    Goes RED if ``ALTER TABLE … FORCE ROW LEVEL SECURITY`` is dropped from
    migration 057 or from the schema.sql mirror.
    """
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'telemetry_samples'::regclass"
        )
    assert row is not None
    assert row["relrowsecurity"] is True
    assert row["relforcerowsecurity"] is True


# ---------------------------------------------------------------------------
# (f) 🔴 THE namespace_id PREDICATE ON THE ASSET PRE-CHECK, through the OWNER
# pool, where the module's own WHERE clause is the only defence.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pulling_another_namespaces_asset_is_refused_by_the_modules_own_predicate(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """(f) 🔴 pins ``WHERE id = $1::uuid AND namespace_id = $2::uuid`` in
    ``_require_asset_in_namespace``.

    Run through the OWNER ``pg_pool``, which is ``Superuser, Bypass RLS``, so
    the RLS policy cannot be what refuses this — the module's own predicate
    is. (e) is the complement: it proves the POLICY defends ``nce_app``.

    A precisely scoped claim, stated because §6.4's trap is exactly here:
    dropping ``AND namespace_id = $2::uuid`` does NOT let a cross-tenant row
    be written, because the caller's own ``namespace_id`` is what gets stored
    and the write still lands in ns_b. What it changes is the FAILURE MODE and
    the DATA: the pull would succeed and attach ns_a's asset_id to ns_b's
    rows. Both are asserted below — the ``ValueError`` and the empty table —
    so the weakened predicate goes RED on the first assertion.
    """
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    engine = _EngineStub(pg_pool)

    asset_of_a = await _seed_asset(pg_pool, ns_a, "BOM-LINE-TELEMETRY-FOREIGN")

    with pytest.raises(ValueError, match="is not in namespace"):
        await do_pull_telemetry(engine, {"namespace_id": ns_b, "asset_id": asset_of_a})

    assert await _count_samples(pg_pool, ns_b) == 0, "ns_b must have written nothing"
    assert await _count_samples(pg_pool, ns_a) == 0, "and nothing may have landed in ns_a"


# ---------------------------------------------------------------------------
# (g)/(h)/(i) The DB constraints stand on their own, with the Python bypassed.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unique_constraint_refuses_a_duplicate_direct_insert(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(g) The idempotency arbiter is ``telemetry_samples_idempotency_uq``,
    not the Python. This INSERT never calls ``do_pull_telemetry``, so the
    module's ``ON CONFLICT`` cannot mask a dropped constraint.

    Goes RED if the UNIQUE is removed from migration 057 — at which point two
    concurrent cron pulls could both insert the same reading.
    """
    asset_id = await _seed_asset(pg_pool, namespace_id, "BOM-LINE-TELEMETRY-UQ")
    taken_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO telemetry_samples "
            "(namespace_id, asset_id, metric, value, sampled_at) VALUES ($1, $2, 'cpu', 1.0, $3)",
            namespace_id,
            asset_id,
            taken_at,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO telemetry_samples "
                "(namespace_id, asset_id, metric, value, sampled_at) "
                "VALUES ($1, $2, 'cpu', 2.0, $3)",
                namespace_id,
                asset_id,
                taken_at,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_named_checks_refuse_a_blank_metric_and_a_non_finite_value(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(h) ``telemetry_samples_metric_not_blank`` and
    ``telemetry_samples_value_finite`` stand without the Python validators.

    All THREE non-finite doubles are exercised, not one: PostgreSQL defines
    ``NaN = NaN`` as TRUE (unlike IEEE-754), so a ``value = value`` spelling
    of the CHECK would catch the two infinities and silently let NaN through.
    """
    asset_id = await _seed_asset(pg_pool, namespace_id, "BOM-LINE-TELEMETRY-CHECKS")
    taken_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    insert = (
        "INSERT INTO telemetry_samples "
        "(namespace_id, asset_id, metric, value, sampled_at) VALUES ($1, $2, $3, $4, $5)"
    )

    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(insert, namespace_id, asset_id, "   ", 1.0, taken_at)
        for bad_metric, bad_value in (
            ("nan_metric", float("nan")),
            ("pos_inf_metric", float("inf")),
            ("neg_inf_metric", float("-inf")),
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(insert, namespace_id, asset_id, bad_metric, bad_value, taken_at)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_foreign_key_refuses_a_sample_for_an_asset_that_does_not_exist(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(i) ``telemetry_samples_asset_fk``.

    The scoped claim, matching migration 057's header: this proves the asset
    EXISTS, NOT that it belongs to this row's namespace — the FK is
    single-column, because ``assets`` has no ``UNIQUE (id, namespace_id)`` for
    a composite one to reference. Namespace membership is enforced by RLS and
    by ``_require_asset_in_namespace`` (tests (e) and (f)), not here.
    """
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO telemetry_samples "
                "(namespace_id, asset_id, metric, value, sampled_at) "
                "VALUES ($1, $2, 'cpu', 1.0, now())",
                namespace_id,
                uuid.uuid4(),
            )


# ---------------------------------------------------------------------------
# (j) The Python mirror fires before the DB — and writes nothing when it does.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_malformed_adapter_payload_is_refused_before_any_row_is_written(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(j) An adapter is where a third party's payload enters, so a NaN
    reading becomes a domain ``ValueError`` rather than a raw
    ``asyncpg.CheckViolationError`` — and the whole batch is rejected, not
    partially written.

    The adapter is injected by patching the FACTORY, which also demonstrates
    that ``select_telemetry_adapter`` is the single seam through which
    ``do_pull_telemetry`` obtains an adapter.
    """
    asset_id = await _seed_asset(pg_pool, namespace_id, "BOM-LINE-TELEMETRY-BADPAYLOAD")
    taken_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bad = _FixedAdapter(
        [
            TelemetrySample(metric="good", value=1.0, sampled_at=taken_at),
            TelemetrySample(metric="bad", value=float("nan"), sampled_at=taken_at),
        ]
    )
    monkeypatch.setattr(telemetry_module, "select_telemetry_adapter", lambda platform: bad)

    with pytest.raises(ValueError, match="non-finite value"):
        await do_pull_telemetry(
            _EngineStub(pg_pool), {"namespace_id": namespace_id, "asset_id": asset_id}
        )

    assert await _count_samples(pg_pool, namespace_id) == 0, (
        "the good sample must not have been written either — the batch is one statement"
    )
