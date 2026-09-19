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
import httpx
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


def test_the_documented_vendor_platforms_are_exactly_the_declared_set() -> None:
    """``09-assets-engine.md`` names the core AV cloud platforms; MLV16F
    Wave F-3 added ``neowit`` (a smart-building aggregator), Wave F-4
    added ``disruptive`` (a sensor vendor read directly, not only through
    Neowit's aggregation), Wave F-5 added ``ochno`` (a USB-C switch/hub
    platform), and Wave F-7 added ``ais`` (a vessel-position feed).
    Pinning the whole set — not a sample of it — so a dropped or renamed
    platform is caught rather than discovered by an operator whose env
    key stops working.

    ``huddly``/``poly`` dropped per Q-38 part 1 (2026-09-19): neither ever
    had a dispatch branch (both always fell to ``UnimplementedVendorAdapter``
    if their swap flag were set), named from the original engine doc's
    platform list with no NCE asset or consumer behind either — listing a
    platform nothing will ever build is not a "not yet built" declaration,
    it is a stale inventory entry.
    """
    assert set(VENDOR_PLATFORMS) == {
        "crestron",
        "neat",
        "qsys",
        "sennheiser",
        "shure",
        "yealink",
        "ymcs",
        "neowit",
        "disruptive",
        "ochno",
        "ais",
    }
    assert MOCK_PLATFORM not in VENDOR_PLATFORMS


@pytest.mark.parametrize("platform", sorted(VENDOR_PLATFORMS))
def test_vendor_platform_is_the_mock_while_its_swap_flag_is_unset(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mock-now: every vendor platform resolves to the mock by default, so the
    engine is usable before any vendor key lands.

    All platforms are asserted, not one — the flag name is derived per platform and
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
    if platform in ("ymcs", "yealink"):
        from nce.vertical_modules.assets.ymcs import YMCSTelemetryAdapter

        assert isinstance(adapter, YMCSTelemetryAdapter)
    elif platform == "neat":
        from nce.vertical_modules.assets.neat_pulse import NeatPulseTelemetryAdapter

        assert isinstance(adapter, NeatPulseTelemetryAdapter)
    elif platform == "qsys":
        from nce.vertical_modules.assets.qsys_reflect import QSysReflectTelemetryAdapter

        assert isinstance(adapter, QSysReflectTelemetryAdapter)
    elif platform == "neowit":
        from nce.vertical_modules.assets.neowit import NeowitTelemetryAdapter

        assert isinstance(adapter, NeowitTelemetryAdapter)
    elif platform == "disruptive":
        from nce.vertical_modules.assets.disruptive import DisruptiveTelemetryAdapter

        assert isinstance(adapter, DisruptiveTelemetryAdapter)
    elif platform == "ochno":
        from nce.vertical_modules.assets.ochno import OchnoTelemetryAdapter

        assert isinstance(adapter, OchnoTelemetryAdapter)
    elif platform == "ais":
        from nce.vertical_modules.assets.ais import AisTelemetryAdapter

        assert isinstance(adapter, AisTelemetryAdapter)
    else:
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
    adapter = UnimplementedVendorAdapter("crestron", VENDOR_PLATFORMS["crestron"])
    with pytest.raises(NotImplementedError) as excinfo:
        await adapter.fetch_samples(uuid.uuid4())
    message = str(excinfo.value)
    assert "crestron" in message
    assert VENDOR_PLATFORMS["crestron"] in message
    assert "NCE_ASSETS_TELEMETRY_CRESTRON_REAL" in message


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

    async def fetch_samples(
        self, asset_id: uuid.UUID, *, serial: str | None = None
    ) -> Sequence[TelemetrySample]:
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


# ---------------------------------------------------------------------------
# AV Cloud Telemetry Adapters: Neat, Q-SYS, Yealink
#
# Crestron/Sennheiser/Shure are NOT here: Q-38 (2026-09-19) found their
# "real" adapters called hardcoded, never-verified endpoints with no host
# client or vendor doc behind them -- worse than unimplemented, because
# they looked implemented. Routed to UnimplementedVendorAdapter instead
# (see test_vendor_platform_swaps_to_its_real_adapter_when_the_flag_is_set
# below); a real client for any of the three returns when a wave exists
# to derive one from the vendor's own public API docs.
# ---------------------------------------------------------------------------


class TestAVCloudAdapters:
    """Validate concrete real AV cloud adapters without requiring live hardware.

    All real adapters must:
    1. Raise NotImplementedError naming missing env vars when unconfigured.
    2. Parse real API responses into TelemetrySample objects when connected to a transport.
    3. Record degradation and return [] when an HTTP query fails, never inventing data.
    """

    @pytest.mark.asyncio
    async def test_neat_pulse_adapter_unconfigured_raises(self) -> None:
        from nce.vertical_modules.assets.neat_pulse import NeatPulseTelemetryAdapter

        adapter = NeatPulseTelemetryAdapter(
            endpoint_url=None, api_key=None, org_id=None, timeout=10.0
        )
        assert adapter.platform == "neat"
        assert adapter._timeout <= 4.9

        with pytest.raises(NotImplementedError, match="neat") as excinfo:
            await adapter.fetch_samples(uuid.uuid4(), serial="NEAT-SN-1")
        assert "NCE_ASSETS_NEAT_ENDPOINT_URL" in str(excinfo.value)
        assert "NCE_ASSETS_NEAT_API_KEY" in str(excinfo.value)
        assert "NCE_ASSETS_NEAT_ORG_ID" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_neat_pulse_adapter_without_a_serial_is_skipped_before_any_http_call(
        self,
    ) -> None:
        from nce.vertical_modules.assets.neat_pulse import NeatPulseTelemetryAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no HTTP call should be made when serial is missing")

        adapter = NeatPulseTelemetryAdapter(
            endpoint_url="https://api.neat.no",
            api_key="neat-token",
            org_id="org-123",
            transport=httpx.MockTransport(handler),
        )
        assert await adapter.fetch_samples(uuid.uuid4(), serial=None) == []
        assert await adapter.fetch_samples(uuid.uuid4(), serial="  ") == []

    @pytest.mark.asyncio
    async def test_neat_pulse_adapter_refuses_a_path_outside_its_allow_list(self) -> None:
        """Gate item 1+2 (MLV16 charter, Lane F): a call outside the explicit
        allow-list literal is refused before any HTTP request is issued."""
        from nce.vertical_modules.assets.neat_pulse import (
            NeatAllowListRefusal,
            NeatPulseTelemetryAdapter,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a refused call must never reach the transport")

        adapter = NeatPulseTelemetryAdapter(
            endpoint_url="https://api.neat.no",
            api_key="neat-token",
            org_id="org-123",
            transport=httpx.MockTransport(handler),
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(NeatAllowListRefusal, match=r"DELETE /rooms/\{id\}"):
                await adapter._gated_request(
                    client, "DELETE", "/rooms/abc123", concrete_ids=["abc123"]
                )
            # /users is the one path the HOST's own client may write to — this
            # adapter has no business with it at all, read or write.
            with pytest.raises(NeatAllowListRefusal):
                await adapter._gated_request(client, "GET", "/users")

    @pytest.mark.asyncio
    async def test_neat_pulse_adapter_live_http(self) -> None:
        """A recorded-fixture-shaped round trip: resolve the endpoint by its
        ``serial`` field on ``GET /endpoints`` (Pulse's own bridge from a
        device to its room, per the host client's docstring), then its
        ``connected`` status and its latest ``GET /endpoints/{id}/sensor``
        reading — never the fabricated
        ``/v1/{org}/devices/{id}/telemetry`` this replaced."""
        from nce.vertical_modules.assets.neat_pulse import NeatPulseTelemetryAdapter

        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            seen.append((request.method, path))
            assert path.startswith("/v1/orgs/org-123/")
            assert request.headers.get("Authorization") == "Bearer neat-token"
            rel = path[len("/v1/orgs/org-123") :]
            if rel == "/endpoints":
                return httpx.Response(
                    200,
                    json={
                        "endpoints": [
                            {"id": "ep-1", "serial": "OTHER-SN", "connected": True},
                            {"id": "ep-42", "serial": "NEAT-BAR-0099", "connected": True},
                        ]
                    },
                )
            if rel == "/endpoints/ep-42/sensor":
                return httpx.Response(
                    200,
                    json={"temperature": 22.1, "humidity": 41.5, "peopleCount": 4},
                )
            raise AssertionError(f"unexpected call: {request.method} {path}")

        adapter = NeatPulseTelemetryAdapter(
            endpoint_url="https://api.neat.no",
            api_key="neat-token",
            org_id="org-123",
            transport=httpx.MockTransport(handler),
        )
        samples = await adapter.fetch_samples(uuid.uuid4(), serial="neat-bar-0099")
        metrics = {s.metric: s.value for s in samples}
        assert metrics["connected"] == 1.0
        assert metrics["temperature"] == 22.1
        assert metrics["humidity"] == 41.5
        assert metrics["peopleCount"] == 4.0
        # Resolution matched the SECOND row, not the first — proves the scan
        # does not stop early.
        assert ("GET", "/v1/orgs/org-123/endpoints") in seen
        assert ("GET", "/v1/orgs/org-123/endpoints/ep-42/sensor") in seen

    @pytest.mark.asyncio
    async def test_neat_pulse_adapter_unmatched_serial_returns_empty_without_error(self) -> None:
        from nce.vertical_modules.assets.neat_pulse import NeatPulseTelemetryAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"endpoints": []})

        adapter = NeatPulseTelemetryAdapter(
            endpoint_url="https://api.neat.no",
            api_key="neat-token",
            org_id="org-123",
            transport=httpx.MockTransport(handler),
        )
        assert await adapter.fetch_samples(uuid.uuid4(), serial="no-such-sn") == []

    @pytest.mark.asyncio
    async def test_neat_pulse_adapter_http_failure_degrades_gracefully(self) -> None:
        from nce.vertical_modules.assets.neat_pulse import NeatPulseTelemetryAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable")

        adapter = NeatPulseTelemetryAdapter(
            endpoint_url="https://api.neat.no",
            api_key="neat-token",
            org_id="org-123",
            transport=httpx.MockTransport(handler),
        )
        assert await adapter.fetch_samples(uuid.uuid4(), serial="neat-bar-0099") == []

    @pytest.mark.asyncio
    async def test_qsys_reflect_adapter_unconfigured_raises(self) -> None:
        from nce.vertical_modules.assets.qsys_reflect import QSysReflectTelemetryAdapter

        adapter = QSysReflectTelemetryAdapter(endpoint_url=None, api_key=None, timeout=10.0)
        assert adapter.platform == "qsys"
        assert adapter._timeout <= 4.9

        with pytest.raises(NotImplementedError, match="qsys") as excinfo:
            await adapter.fetch_samples(uuid.uuid4(), serial="SN-1001")
        assert "NCE_ASSETS_QSYS_ENDPOINT_URL" in str(excinfo.value)
        assert "NCE_ASSETS_QSYS_API_KEY" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_qsys_reflect_adapter_without_a_serial_is_skipped_before_any_http_call(
        self,
    ) -> None:
        from nce.vertical_modules.assets.qsys_reflect import QSysReflectTelemetryAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no HTTP call should be made when serial is missing")

        adapter = QSysReflectTelemetryAdapter(
            endpoint_url="https://reflect.qsc.com/api/public/v0",
            api_key="qsys-token",
            transport=httpx.MockTransport(handler),
        )
        assert await adapter.fetch_samples(uuid.uuid4(), serial=None) == []
        assert await adapter.fetch_samples(uuid.uuid4(), serial="  ") == []

    @pytest.mark.asyncio
    async def test_qsys_reflect_adapter_refuses_a_path_outside_its_allow_list(self) -> None:
        """Gate item 1+2 (MLV16 charter, Lane F): a call outside the
        explicit allow-list literal is refused before any HTTP request."""
        from nce.vertical_modules.assets.qsys_reflect import (
            QSysAllowListRefusal,
            QSysReflectTelemetryAdapter,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a refused call must never reach the transport")

        adapter = QSysReflectTelemetryAdapter(
            endpoint_url="https://reflect.qsc.com/api/public/v0",
            api_key="qsys-token",
            transport=httpx.MockTransport(handler),
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(QSysAllowListRefusal, match="DELETE cores"):
                await adapter._gated_request(client, "DELETE", "cores")
            with pytest.raises(QSysAllowListRefusal):
                await adapter._gated_request(client, "GET", "sites")

    @pytest.mark.asyncio
    async def test_qsys_reflect_adapter_live_http_matches_a_core_by_serial_number(self) -> None:
        """A recorded-fixture-shaped round trip: resolving by ``serialNumber``
        (the PHYSICAL serial, per the host's own test fixtures) — never
        ``serial``, which is Reflect's own internal id and carries an
        entirely different value (verified: ``"reflect-id"`` vs
        ``"SN-1001"``). Never the fabricated single-endpoint shape this
        replaced."""
        from nce.vertical_modules.assets.qsys_reflect import QSysReflectTelemetryAdapter

        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            assert request.headers.get("Authorization") == "Bearer qsys-token"
            if request.url.path == "/api/public/v0/cores":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 1,
                            "serial": "reflect-id",
                            "serialNumber": "SN-1001",
                            "name": "Rom A",
                            "model": "Core 610",
                            "status": {"code": 0, "message": "OK"},
                            "redundancy": {"role": "Primary", "state": "Standby"},
                        },
                        {
                            "id": 2,
                            "serial": "reflect-id-2",
                            "serialNumber": "SN-1002",
                            "name": "Aktiv Core",
                            "model": "Core 610",
                            "status": {"code": 3, "message": "Fault"},
                        },
                    ],
                )
            raise AssertionError(f"unexpected call: {request.method} {request.url}")

        adapter = QSysReflectTelemetryAdapter(
            endpoint_url="https://reflect.qsc.com/api/public/v0",
            api_key="qsys-token",
            transport=httpx.MockTransport(handler),
        )
        samples = await adapter.fetch_samples(uuid.uuid4(), serial="sn-1002")
        metrics = {s.metric: s.value for s in samples}
        assert metrics["status.code"] == 3.0
        # A match on `serial` (Reflect's own id) instead of `serialNumber`
        # would have picked core 1, not core 2 — this proves it did not.
        assert ("GET", "/api/public/v0/cores") in seen

    @pytest.mark.asyncio
    async def test_qsys_reflect_adapter_falls_back_to_a_system_item_by_serial_number(
        self,
    ) -> None:
        """An asset that is a component (mic, amp), not a Core, is found
        by walking systems -> systems/{id}/items — verified two-tier
        search shape."""
        from nce.vertical_modules.assets.qsys_reflect import QSysReflectTelemetryAdapter

        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            if request.url.path == "/api/public/v0/cores":
                return httpx.Response(200, json=[])
            if request.url.path == "/api/public/v0/systems":
                return httpx.Response(
                    200,
                    json=[{"id": 10, "name": "Design A", "design": {"id": 10}, "core": {"id": 1}}],
                )
            if request.url.path == "/api/public/v0/systems/10/items":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 20,
                            "name": "Mikrofon",
                            "serialNumber": "MIC-123",
                            "isOnline": True,
                            "status": {"code": 0, "message": "OK"},
                            "system": {"id": 10},
                            "core": {"id": 1},
                        }
                    ],
                )
            raise AssertionError(f"unexpected call: {request.method} {request.url}")

        adapter = QSysReflectTelemetryAdapter(
            endpoint_url="https://reflect.qsc.com/api/public/v0",
            api_key="qsys-token",
            transport=httpx.MockTransport(handler),
        )
        samples = await adapter.fetch_samples(uuid.uuid4(), serial="mic-123")
        metrics = {s.metric: s.value for s in samples}
        assert metrics["status.code"] == 0.0
        assert metrics["isOnline"] == 1.0
        assert ("GET", "/api/public/v0/systems/10/items") in seen

    @pytest.mark.asyncio
    async def test_qsys_reflect_adapter_unmatched_serial_returns_empty_without_error(
        self,
    ) -> None:
        from nce.vertical_modules.assets.qsys_reflect import QSysReflectTelemetryAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        adapter = QSysReflectTelemetryAdapter(
            endpoint_url="https://reflect.qsc.com/api/public/v0",
            api_key="qsys-token",
            transport=httpx.MockTransport(handler),
        )
        assert await adapter.fetch_samples(uuid.uuid4(), serial="no-such-sn") == []

    @pytest.mark.asyncio
    async def test_qsys_reflect_adapter_http_failure_degrades_gracefully(self) -> None:
        from nce.vertical_modules.assets.qsys_reflect import QSysReflectTelemetryAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable")

        adapter = QSysReflectTelemetryAdapter(
            endpoint_url="https://reflect.qsc.com/api/public/v0",
            api_key="qsys-token",
            transport=httpx.MockTransport(handler),
        )
        assert await adapter.fetch_samples(uuid.uuid4(), serial="SN-1001") == []

    @pytest.mark.asyncio
    async def test_yealink_adapter_alias_unconfigured_raises(self) -> None:
        from nce.vertical_modules.assets.ymcs import YealinkTelemetryAdapter, YMCSTelemetryAdapter

        assert YealinkTelemetryAdapter is YMCSTelemetryAdapter
        adapter = YealinkTelemetryAdapter(
            endpoint_url=None,
            client_id=None,
            client_secret=None,
            platform_name="yealink",
            timeout=10.0,
        )
        assert adapter.platform == "yealink"
        assert adapter._timeout <= 4.9

        with pytest.raises(NotImplementedError, match="yealink") as excinfo:
            await adapter.fetch_samples(uuid.uuid4(), serial="SN-1")
        assert "NCE_ASSETS_YMCS_ENDPOINT_URL" in str(excinfo.value)
        assert "NCE_ASSETS_YMCS_CLIENT_ID" in str(excinfo.value)
        assert "NCE_ASSETS_YMCS_CLIENT_SECRET" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_yealink_adapter_without_a_serial_is_skipped_before_any_http_call(self) -> None:
        """An asset with no captured serial cannot be resolved against YMCS
        (device resolution note, module docstring) — it must be skipped, not
        guessed at, and skipped BEFORE any network call is attempted."""
        from nce.vertical_modules.assets.ymcs import YealinkTelemetryAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no HTTP call should be made when serial is missing")

        adapter = YealinkTelemetryAdapter(
            endpoint_url="https://ymcs.yealink.com",
            client_id="ymcs-id",
            client_secret="ymcs-secret",
            platform_name="yealink",
            transport=httpx.MockTransport(handler),
        )
        assert await adapter.fetch_samples(uuid.uuid4(), serial=None) == []
        assert await adapter.fetch_samples(uuid.uuid4(), serial="  ") == []

    @pytest.mark.asyncio
    async def test_yealink_adapter_refuses_a_path_outside_its_allow_list(self) -> None:
        """Gate item 1+2 (MLV16 charter, Lane F): a call outside the explicit
        allow-list literal is refused before any HTTP request is issued —
        proved here by a transport that fails the test if it is ever reached.
        """
        from nce.vertical_modules.assets.ymcs import (
            _ALLOWED_AUTH,
            _ALLOWED_READS,
            YealinkTelemetryAdapter,
            YmcsAllowListRefusal,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a refused call must never reach the transport")

        adapter = YealinkTelemetryAdapter(
            endpoint_url="https://ymcs.yealink.com",
            client_id="ymcs-id",
            client_secret="ymcs-secret",
            platform_name="yealink",
            transport=httpx.MockTransport(handler),
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(YmcsAllowListRefusal, match=r"DELETE /v2/dm/devices/\{id\}"):
                await adapter._gated_request(
                    client,
                    "DELETE",
                    "/v2/dm/devices/abc123",
                    allowed=_ALLOWED_READS,
                    concrete_ids=["abc123"],
                )
            # The device-reset write path some vendors DO expose is not in
            # this adapter's allow-list either, and never will be (rule 3:
            # no write method anywhere).
            with pytest.raises(YmcsAllowListRefusal):
                await adapter._gated_request(
                    client,
                    "POST",
                    "/v2/dm/device/reset",
                    allowed=_ALLOWED_READS | _ALLOWED_AUTH,
                )

    @pytest.mark.asyncio
    async def test_yealink_adapter_live_http(self) -> None:
        """A recorded-fixture-shaped round trip: token exchange, device
        resolution by serial via ``listDevices``, then the device's
        ``sensor``/``wifi`` detail and its one bound part's ``extraInfo`` —
        the real YMCS v2 shapes (verified against the host's own test
        fixtures for the ``sn``/``wifi.signalStrength``/``extraInfo`` field
        names, Q-25 shape-only reading), never the fabricated
        ``/api/v1/devices/{id}/telemetry`` this replaced."""
        from nce.vertical_modules.assets.ymcs import YealinkTelemetryAdapter

        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            seen.append((request.method, path))
            if path == "/v2/token":
                assert request.headers.get("Authorization", "").startswith("Basic ")
                return httpx.Response(200, json={"accessToken": "ymcs-live-token"})
            assert request.headers.get("Authorization") == "Bearer ymcs-live-token"
            if path == "/v2/dm/listDevices":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"id": "dev-1", "sn": "OTHER-SN", "name": "Room A"},
                            {"id": "dev-42", "sn": "YL-A30-0007", "name": "Room B"},
                        ],
                        "total": 2,
                    },
                )
            if path == "/v2/dm/devices/dev-42":
                assert request.url.params.get("select") == "sensor,wifi"
                return httpx.Response(
                    200,
                    json={"data": {"wifi": {"signalStrength": 10}, "sensor": {}}},
                )
            if path == "/v2/dm/devices/dev-42/listParts":
                return httpx.Response(200, json={"data": [{"id": "part-9", "name": "Room Sensor"}]})
            if path == "/v2/dm/devices/dev-42/parts/part-9":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "extraInfo": {"batteryLevel": 100, "irradiance": 0, "motionState": 1}
                        }
                    },
                )
            raise AssertionError(f"unexpected call: {request.method} {path}")

        adapter = YealinkTelemetryAdapter(
            endpoint_url="https://ymcs.yealink.com",
            client_id="ymcs-id",
            client_secret="ymcs-secret",
            platform_name="yealink",
            transport=httpx.MockTransport(handler),
        )
        samples = await adapter.fetch_samples(uuid.uuid4(), serial="yl-a30-0007")
        metrics = {s.metric: s.value for s in samples}
        assert metrics["signalStrength"] == 10.0
        assert metrics["batteryLevel"] == 100.0
        assert metrics["irradiance"] == 0.0
        assert metrics["motionState"] == 1.0
        # Resolution matched case-insensitively against the SECOND page row,
        # not the first — proves the scan does not stop at row 1.
        assert ("POST", "/v2/dm/listDevices") in seen
        assert ("GET", "/v2/dm/devices/dev-42") in seen
        assert ("POST", "/v2/dm/devices/dev-42/listParts") in seen
        assert ("GET", "/v2/dm/devices/dev-42/parts/part-9") in seen

    @pytest.mark.asyncio
    async def test_yealink_adapter_unmatched_serial_returns_empty_without_error(self) -> None:
        from nce.vertical_modules.assets.ymcs import YealinkTelemetryAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/token":
                return httpx.Response(200, json={"accessToken": "tok"})
            if request.url.path == "/v2/dm/listDevices":
                return httpx.Response(200, json={"data": [], "total": 0})
            raise AssertionError(f"unexpected call: {request.url.path}")

        adapter = YealinkTelemetryAdapter(
            endpoint_url="https://ymcs.yealink.com",
            client_id="ymcs-id",
            client_secret="ymcs-secret",
            platform_name="yealink",
            transport=httpx.MockTransport(handler),
        )
        assert await adapter.fetch_samples(uuid.uuid4(), serial="no-such-sn") == []

    @pytest.mark.asyncio
    async def test_yealink_adapter_http_failure_degrades_gracefully(self) -> None:
        from nce.vertical_modules.assets.ymcs import YealinkTelemetryAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable")

        adapter = YealinkTelemetryAdapter(
            endpoint_url="https://ymcs.yealink.com",
            client_id="ymcs-id",
            client_secret="ymcs-secret",
            platform_name="yealink",
            transport=httpx.MockTransport(handler),
        )
        assert await adapter.fetch_samples(uuid.uuid4(), serial="yl-a30-0007") == []


# ---------------------------------------------------------------------------
# Neowit: a smart-building aggregator (new platform, MLV16F Wave F-3) — not
# an AV cloud vendor, so tested on its own rather than folded into
# TestAVCloudAdapters.
# ---------------------------------------------------------------------------

# A throwaway RSA key generated for this test file only — never a real
# credential, and never round-tripped through anything but PyJWT's local
# signing (the adapter's own private key is never sent over the wire).
_TEST_RSA_PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCStoM4XHI2dfyu
M8yuSHHWb61AaH/vFdYHqxtn05GfAiojcWZGwtgK7OeriyHVROvXWqpIz2dr68Y2
o0KnCheJv5tITcNecOykYOLvDktgVVnocGZUtFraPtJgu235iu1403ruy3zZkjeN
br4W9MrNK50slOONQu6ghPnmrbN1FO6dbCaZPXml8/3bPW5jAlTYtGEnFPUbynvj
0POw8HKfOkRabufjyXxcheeKHDKLZaCx4IzmA7QH6yamlmQiY4KwedwV0eVTsj0t
o2NR6W8bW6ZN4v4xdbLv7K53A+wzAZtTmrzYAeUYTuCsP03daRygOR2fIUK2X3EZ
nz0kYGplAgMBAAECggEAAUn0r6lSQIu7T015shtFUsCy6TKx0wgiU/lrGXeomxjC
BYMwxpTeIIRxyZZEkxLSrpbnkZGu4yoUWPUIuB500/s4skMqoPkFfzExtS9vNpax
XkMkhwB5ntq37u0em3devDXBafkkLOYlskqjhWCbqn9EV3isYhiRL5xTdLUYc0Ib
KY5N79+fNTOOoPI+8h3S21rYDu+uZtUgbnfOMTynoXRmkkcOzqBQmzUcUY5457/M
er0zfguN8xdQFmNT8sZW2xZt7Trwrx8IzkOUU2YBTVNn84r3T57syFLY/S1PZ9Ex
l4496n1L/lJBttsgnnrJ3aUUkacvBwxP+iM8MxkDIQKBgQDGplh8Jc9rO76+DZ/E
p6KbkrZWJJchDBKvEYWX1e5+l3tw8zj6N3zqqAjiy6fkeQMUa8+SfaIBym8heup1
bXs/2/33lsNwCSBeC050Tu9Mis+tiDRQQjLixhF0r+qvfwuHxbQrijS7EDhl/pNW
3pTzQ0lB6cTmopeXOMYZbHOs4QKBgQC9EaifXfMwZaTRTywbLPhbfJftGCwWTmYz
72yGMDlCIGiqatJKviQ54RP2G5Zyp+or/i1VARf//KxLjmszBiXlfRjX3USFDHIc
zlisU2umlgMfXgHkQL8+UrSDsBEV9oBmJV+h/HMFhM786DZCF2+hjlDOJ4UvnyJv
THedLcVKBQKBgQCbqBLjzNjX5OvUjmZnuReAohiALZHCkmw9hBRTYo3L4jUWz28R
GdOnJ942oHBBZdVU9hmjZxBAKPilmmQHea8+3coGbLtdmbkkF+X02zlFl+udxYGA
di7bZWqeLY5Oz9UgIXnJODWTcuVOfonDYwwCBfJsVJo2QqdYFmOb3lBR4QKBgEpv
y12DFZ22Rt+JNio02ErckMvtul3F3AMSfj2OetyH+e0uRUDb/1MyRDOexOq7JTzQ
w3Q2DAbiqcrNdXMPNphVWhSSrslbDwo8Szj9VuKtKOmOj1wYCbM1yJAYH4HwHLka
eb5Cr946XWvA2KvIolCOwU2IzazkECCVkHo3bPcpAoGAOiLVslkYhY6EMWRIKtVc
9sBCrl1KbvJT/IJ+eOfnDbge+SbCxtkWNv1J4V8CxRhNNZsepzbc/EdrBw39dBcN
XLdiT9Eyedd1IKP2tBCnarjtOgn1XwK1mgcJ+AuOQBPBeOBDY/vkpWn8bXUIM1hu
u4XwLJNhU1uEx86MaiWc02A=
-----END PRIVATE KEY-----"""


def _neowit_adapter(handler: Any) -> Any:
    from nce.vertical_modules.assets.neowit import NeowitTelemetryAdapter

    return NeowitTelemetryAdapter(
        endpoint_url="https://api.neowit.io",
        account_id="acct-1",
        key_id="key-1",
        private_key=_TEST_RSA_PRIVATE_KEY,
        transport=httpx.MockTransport(handler),
    )


class TestNeowitAdapter:
    @pytest.mark.asyncio
    async def test_neowit_adapter_unconfigured_raises(self) -> None:
        from nce.vertical_modules.assets.neowit import NeowitTelemetryAdapter

        adapter = NeowitTelemetryAdapter(
            endpoint_url=None, account_id=None, key_id=None, private_key=None, timeout=10.0
        )
        assert adapter.platform == "neowit"
        assert adapter._timeout <= 4.9

        with pytest.raises(NotImplementedError, match="neowit") as excinfo:
            await adapter.fetch_samples(uuid.uuid4(), serial="EXT-SN-1")
        assert "NCE_ASSETS_NEOWIT_ENDPOINT_URL" in str(excinfo.value)
        assert "NCE_ASSETS_NEOWIT_ACCOUNT_ID" in str(excinfo.value)
        assert "NCE_ASSETS_NEOWIT_KEY_ID" in str(excinfo.value)
        assert "NCE_ASSETS_NEOWIT_PRIVATE_KEY" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_neowit_adapter_without_a_serial_is_skipped_before_any_http_call(self) -> None:

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no HTTP call should be made when serial is missing")

        adapter = _neowit_adapter(handler)
        assert await adapter.fetch_samples(uuid.uuid4(), serial=None) == []
        assert await adapter.fetch_samples(uuid.uuid4(), serial="  ") == []

    @pytest.mark.asyncio
    async def test_neowit_adapter_refuses_a_path_outside_its_allow_list(self) -> None:
        """Gate item 1+2 (MLV16 charter, Lane F): a call outside the
        explicit allow-list literal is refused before any HTTP request."""
        from nce.vertical_modules.assets.neowit import (
            _ALLOWED_AUTH,
            _ALLOWED_READS,
            NeowitAllowListRefusal,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a refused call must never reach the transport")

        adapter = _neowit_adapter(handler)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(NeowitAllowListRefusal, match=r"DELETE /device/v1/device"):
                await adapter._gated_request(
                    client, "DELETE", "/device/v1/device", allowed=_ALLOWED_READS
                )
            # Neowit's own service-account credential carries real write
            # access to the customer's production environment (module
            # docstring) — this adapter must refuse every write verb, not
            # just the ones its own reads happen to shadow.
            with pytest.raises(NeowitAllowListRefusal):
                await adapter._gated_request(
                    client, "POST", "/space/v1/space", allowed=_ALLOWED_READS | _ALLOWED_AUTH
                )

    @pytest.mark.asyncio
    async def test_neowit_adapter_live_http(self) -> None:
        """A recorded-fixture-shaped round trip: JWT-bearer token exchange,
        device resolution by matching the asset's serial against Neowit's
        ``externalId`` (the one identity bridge the host client documents
        — see the module's honesty note on this), then a device-series
        range query producing one sample per (sensor, row)."""
        from nce.vertical_modules.assets.neowit import NeowitTelemetryAdapter

        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            seen.append((request.method, path))
            if path == "/auth/oauth/token":
                assert request.headers.get("Content-Type") == "application/x-www-form-urlencoded"
                return httpx.Response(200, json={"access_token": "neowit-tok", "expires_in": 600})
            assert request.headers.get("Authorization") == "Bearer neowit-tok"
            if path == "/device/v1/device":
                return httpx.Response(
                    200,
                    json={
                        "devices": [
                            {"id": "dev-1", "externalId": "OTHER-EXT"},
                            {"id": "dev-77", "externalId": "DISRUPTIVE-0042"},
                        ]
                    },
                )
            if path == "/series/v1/device/dev-77":
                return httpx.Response(
                    200,
                    json={
                        "sensors": ["temperature", "humidity"],
                        "rows": [
                            {"time": "2026-09-18T10:00:00Z", "values": [21.5, 38.0]},
                            {"time": "2026-09-18T10:05:00Z", "values": [21.7, 37.5]},
                        ],
                    },
                )
            raise AssertionError(f"unexpected call: {request.method} {path}")

        adapter = NeowitTelemetryAdapter(
            endpoint_url="https://api.neowit.io",
            account_id="acct-1",
            key_id="key-1",
            private_key=_TEST_RSA_PRIVATE_KEY,
            transport=httpx.MockTransport(handler),
        )
        samples = await adapter.fetch_samples(uuid.uuid4(), serial="disruptive-0042")
        assert len(samples) == 4  # 2 sensors x 2 rows
        by_metric: dict[str, list[float]] = {}
        for s in samples:
            by_metric.setdefault(s.metric, []).append(s.value)
        assert sorted(by_metric["temperature"]) == [21.5, 21.7]
        assert sorted(by_metric["humidity"]) == [37.5, 38.0]
        assert ("POST", "/auth/oauth/token") in seen
        assert ("GET", "/device/v1/device") in seen
        assert ("GET", "/series/v1/device/dev-77") in seen

    @pytest.mark.asyncio
    async def test_neowit_adapter_unmatched_serial_returns_empty_without_error(self) -> None:
        from nce.vertical_modules.assets.neowit import NeowitTelemetryAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/oauth/token":
                return httpx.Response(200, json={"access_token": "tok"})
            if request.url.path == "/device/v1/device":
                return httpx.Response(200, json={"devices": []})
            raise AssertionError(f"unexpected call: {request.url.path}")

        adapter = NeowitTelemetryAdapter(
            endpoint_url="https://api.neowit.io",
            account_id="acct-1",
            key_id="key-1",
            private_key=_TEST_RSA_PRIVATE_KEY,
            transport=httpx.MockTransport(handler),
        )
        assert await adapter.fetch_samples(uuid.uuid4(), serial="no-such-ext-id") == []

    @pytest.mark.asyncio
    async def test_neowit_adapter_http_failure_degrades_gracefully(self) -> None:
        from nce.vertical_modules.assets.neowit import NeowitTelemetryAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable")

        adapter = NeowitTelemetryAdapter(
            endpoint_url="https://api.neowit.io",
            account_id="acct-1",
            key_id="key-1",
            private_key=_TEST_RSA_PRIVATE_KEY,
            transport=httpx.MockTransport(handler),
        )
        assert await adapter.fetch_samples(uuid.uuid4(), serial="disruptive-0042") == []


# ---------------------------------------------------------------------------
# Disruptive Technologies: a sensor vendor read directly, not only through
# Neowit's aggregation (new platform, MLV16F Wave F-4).
# ---------------------------------------------------------------------------


def _disruptive_adapter(handler: Any) -> Any:
    from nce.vertical_modules.assets.disruptive import DisruptiveTelemetryAdapter

    return DisruptiveTelemetryAdapter(
        endpoint_url="https://api.disruptive-technologies.com/v2",
        token_url="https://identity.disruptive-technologies.com/oauth2/token",
        project_id="proj-1",
        service_account_email="svc@example.iam.d17-serviceaccount.com",
        key_id="key-1",
        secret="dt-hs256-test-secret-at-least-32-bytes-long",
        transport=httpx.MockTransport(handler),
    )


class TestDisruptiveAdapter:
    @pytest.mark.asyncio
    async def test_disruptive_adapter_unconfigured_raises(self) -> None:
        from nce.vertical_modules.assets.disruptive import DisruptiveTelemetryAdapter

        adapter = DisruptiveTelemetryAdapter(
            endpoint_url=None,
            token_url=None,
            project_id=None,
            service_account_email=None,
            key_id=None,
            secret=None,
            timeout=10.0,
        )
        assert adapter.platform == "disruptive"
        assert adapter._timeout <= 4.9

        with pytest.raises(NotImplementedError, match="disruptive") as excinfo:
            await adapter.fetch_samples(uuid.uuid4(), serial="bjehn6sdm92g00c1nvo0")
        message = str(excinfo.value)
        assert "NCE_ASSETS_DISRUPTIVE_ENDPOINT_URL" in message
        assert "NCE_ASSETS_DISRUPTIVE_TOKEN_URL" in message
        assert "NCE_ASSETS_DISRUPTIVE_PROJECT_ID" in message
        assert "NCE_ASSETS_DISRUPTIVE_SERVICE_ACCOUNT_EMAIL" in message
        assert "NCE_ASSETS_DISRUPTIVE_KEY_ID" in message
        assert "NCE_ASSETS_DISRUPTIVE_SECRET" in message

    @pytest.mark.asyncio
    async def test_disruptive_adapter_without_a_serial_is_skipped_before_any_http_call(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no HTTP call should be made when serial is missing")

        adapter = _disruptive_adapter(handler)
        assert await adapter.fetch_samples(uuid.uuid4(), serial=None) == []
        assert await adapter.fetch_samples(uuid.uuid4(), serial="  ") == []

    @pytest.mark.asyncio
    async def test_disruptive_adapter_refuses_a_path_outside_its_allow_list(self) -> None:
        """Gate item 1+2 (MLV16 charter, Lane F): a call outside the
        explicit allow-list literal is refused before any HTTP request."""
        from nce.vertical_modules.assets.disruptive import (
            _ALLOWED_READS,
            DisruptiveAllowListRefusal,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a refused call must never reach the transport")

        adapter = _disruptive_adapter(handler)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(DisruptiveAllowListRefusal, match="DELETE /devices"):
                await adapter._gated_request(
                    client, "DELETE", "/devices", headers={"Authorization": "Bearer x"}
                )
            with pytest.raises(DisruptiveAllowListRefusal):
                await adapter._gated_request(
                    client, "GET", "/dataconnectors", headers={"Authorization": "Bearer x"}
                )
        assert ("GET", "/devices") in _ALLOWED_READS

    @pytest.mark.asyncio
    async def test_disruptive_adapter_live_http(self) -> None:
        """A recorded-fixture-shaped round trip: HS256 JWT-bearer token
        exchange at a DIFFERENT host from the data API, device resolution
        by matching the asset's serial against DT's own device id (the
        last path segment of ``name`` — there is no serial field in this
        vendor's device shape, verified against the host's own test
        fixtures), then per-event-type metrics from the SAME ``/devices``
        call's ``reported`` object — never a second per-device round trip,
        and never the fabricated single-endpoint shape earlier waves
        replaced."""
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            seen.append((request.method, request.url.path))
            if url == "https://identity.disruptive-technologies.com/oauth2/token":
                assert request.headers.get("Content-Type") == "application/x-www-form-urlencoded"
                return httpx.Response(200, json={"access_token": "dt-tok", "expires_in": 3600})
            assert request.headers.get("Authorization") == "Bearer dt-tok"
            if request.url.path == "/v2/projects/proj-1/devices":
                return httpx.Response(
                    200,
                    json={
                        "devices": [
                            {
                                "name": "projects/proj-1/devices/other-device",
                                "type": "temperature",
                                "reported": {
                                    "temperature": {
                                        "value": 19.0,
                                        "updateTime": "2026-09-18T09:00:00Z",
                                    }
                                },
                            },
                            {
                                "name": "projects/proj-1/devices/bjehn6sdm92g00c1nvo0",
                                "type": "humidity",
                                "labels": {"name": "Møterom 3"},
                                "reported": {
                                    "humidity": {
                                        "relativeHumidity": 41,
                                        "updateTime": "2026-09-18T07:00:00Z",
                                    },
                                    "batteryStatus": {"percentage": 8},
                                    "networkStatus": {
                                        "signalStrength": 62,
                                        "updateTime": "2026-09-18T08:30:00Z",
                                    },
                                    "deskOccupancy": {
                                        "state": "NOT_OCCUPIED",
                                        "updateTime": "2026-09-18T08:30:00Z",
                                    },
                                },
                            },
                        ],
                        "nextPageToken": "",
                    },
                )
            raise AssertionError(f"unexpected call: {request.method} {url}")

        adapter = _disruptive_adapter(handler)
        samples = await adapter.fetch_samples(uuid.uuid4(), serial="bjehn6sdm92g00c1nvo0")
        metrics = {s.metric: s.value for s in samples}
        assert metrics["humidity.relativeHumidity"] == 41.0
        assert metrics["batteryStatus.percentage"] == 8.0
        assert metrics["networkStatus.signalStrength"] == 62.0
        # deskOccupancy.state is a string ("NOT_OCCUPIED") — skipped, never
        # coerced into a fabricated number.
        assert not any(m.startswith("deskOccupancy") for m in metrics)
        assert ("POST", "/oauth2/token") in seen
        assert ("GET", "/v2/projects/proj-1/devices") in seen

    @pytest.mark.asyncio
    async def test_disruptive_adapter_matches_a_labelled_serial_too(self) -> None:
        """Honesty-flagged fallback: some installs may record a serial in
        a device's free-form ``labels`` rather than relying on DT's own
        opaque device id."""

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2/token" in str(request.url):
                return httpx.Response(200, json={"access_token": "dt-tok"})
            if request.url.path == "/v2/projects/proj-1/devices":
                return httpx.Response(
                    200,
                    json={
                        "devices": [
                            {
                                "name": "projects/proj-1/devices/abc123",
                                "type": "co2",
                                "labels": {"serial": "ASSET-SN-9"},
                                "reported": {
                                    "co2": {"ppm": 600, "updateTime": "2026-09-18T09:00:00Z"}
                                },
                            }
                        ],
                        "nextPageToken": "",
                    },
                )
            raise AssertionError(f"unexpected call: {request.url}")

        adapter = _disruptive_adapter(handler)
        samples = await adapter.fetch_samples(uuid.uuid4(), serial="asset-sn-9")
        metrics = {s.metric: s.value for s in samples}
        assert metrics["co2.ppm"] == 600.0

    @pytest.mark.asyncio
    async def test_disruptive_adapter_unmatched_serial_returns_empty_without_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2/token" in str(request.url):
                return httpx.Response(200, json={"access_token": "dt-tok"})
            return httpx.Response(200, json={"devices": [], "nextPageToken": ""})

        adapter = _disruptive_adapter(handler)
        assert await adapter.fetch_samples(uuid.uuid4(), serial="no-such-device") == []

    @pytest.mark.asyncio
    async def test_disruptive_adapter_http_failure_degrades_gracefully(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable")

        adapter = _disruptive_adapter(handler)
        assert await adapter.fetch_samples(uuid.uuid4(), serial="bjehn6sdm92g00c1nvo0") == []


# ---------------------------------------------------------------------------
# Ochno: a USB-C switch/hub platform (new platform, MLV16F Wave F-5) — not
# an AV cloud vendor.
# ---------------------------------------------------------------------------


def _ochno_adapter(handler: Any) -> Any:
    from nce.vertical_modules.assets.ochno import OchnoTelemetryAdapter

    return OchnoTelemetryAdapter(
        endpoint_url="https://operated.ochno.com",
        token="ochno-bearer-token",
        transport=httpx.MockTransport(handler),
    )


class TestOchnoAdapter:
    @pytest.mark.asyncio
    async def test_ochno_adapter_unconfigured_raises(self) -> None:
        from nce.vertical_modules.assets.ochno import OchnoTelemetryAdapter

        adapter = OchnoTelemetryAdapter(endpoint_url=None, token=None, timeout=10.0)
        assert adapter.platform == "ochno"
        assert adapter._timeout <= 4.9

        with pytest.raises(NotImplementedError, match="ochno") as excinfo:
            await adapter.fetch_samples(uuid.uuid4(), serial="O474C90FA5EDC1")
        message = str(excinfo.value)
        assert "NCE_ASSETS_OCHNO_ENDPOINT_URL" in message
        assert "NCE_ASSETS_OCHNO_TOKEN" in message

    @pytest.mark.asyncio
    async def test_ochno_adapter_without_a_serial_is_skipped_before_any_http_call(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no HTTP call should be made when serial is missing")

        adapter = _ochno_adapter(handler)
        assert await adapter.fetch_samples(uuid.uuid4(), serial=None) == []
        assert await adapter.fetch_samples(uuid.uuid4(), serial="  ") == []

    @pytest.mark.asyncio
    async def test_ochno_adapter_refuses_a_path_outside_its_allow_list(self) -> None:
        """Gate item 1+2 (MLV16 charter, Lane F): a call outside the
        explicit allow-list literal is refused before any HTTP request."""
        from nce.vertical_modules.assets.ochno import OchnoAllowListRefusal

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a refused call must never reach the transport")

        adapter = _ochno_adapter(handler)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OchnoAllowListRefusal, match="DELETE /api/hubs"):
                await adapter._gated_request(client, "DELETE", "/api/hubs")
            # Ochno's own client reads four more endpoints (accounts,
            # spaces, spaces/{id}, system/configuration) that this
            # adapter has no business calling at all.
            with pytest.raises(OchnoAllowListRefusal):
                await adapter._gated_request(client, "GET", "/api/spaces")

    @pytest.mark.asyncio
    async def test_ochno_adapter_live_http(self) -> None:
        """A recorded-fixture-shaped round trip against a bare bearer
        token (no exchange, unlike every prior wave) and a bare JSON array
        response (no envelope, unlike every prior wave) — resolving by
        ``data.serial``, a field with real evidence behind it, and reading
        port/connectivity state from the SAME response, never the
        fabricated single-endpoint shape earlier waves replaced."""
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            assert request.headers.get("Authorization") == "Bearer ochno-bearer-token"
            if request.url.path == "/api/hubs":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "hub-other",
                            "spaceIds": ["room-a"],
                            "data": {"serial": "OTHER-SN", "product": "O-PC-4"},
                        },
                        {
                            "id": "hub-1",
                            "spaceIds": ["room-b"],
                            "data": {
                                "presence": True,
                                "product": "O-PC-4",
                                "serial": "O474C90FA5EDC1",
                                "state": {
                                    "connected": [0, 0, 0, 1],
                                    "active": 4,
                                    "mtrmode": True,
                                },
                            },
                        },
                    ],
                )
            raise AssertionError(f"unexpected call: {request.method} {request.url}")

        adapter = _ochno_adapter(handler)
        samples = await adapter.fetch_samples(uuid.uuid4(), serial="o474c90fa5edc1")
        metrics = {s.metric: s.value for s in samples}
        assert metrics["presence"] == 1.0
        assert metrics["state.active"] == 4.0
        assert metrics["state.mtrmode"] == 1.0
        assert metrics["state.connected.0"] == 0.0
        assert metrics["state.connected.3"] == 1.0
        # Resolution matched the SECOND row, not the first.
        assert ("GET", "/api/hubs") in seen

    @pytest.mark.asyncio
    async def test_ochno_adapter_unmatched_serial_returns_empty_without_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        adapter = _ochno_adapter(handler)
        assert await adapter.fetch_samples(uuid.uuid4(), serial="no-such-serial") == []

    @pytest.mark.asyncio
    async def test_ochno_adapter_http_failure_degrades_gracefully(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable")

        adapter = _ochno_adapter(handler)
        assert await adapter.fetch_samples(uuid.uuid4(), serial="O474C90FA5EDC1") == []


# ---------------------------------------------------------------------------
# AIS: a vessel-position feed (new platform, MLV16F Wave F-7, the last of
# the seven vendor telemetry adapters) — not an AV cloud vendor.
# ---------------------------------------------------------------------------


def _ais_adapter(handler: Any, *, authenticated: bool = True) -> Any:
    from nce.vertical_modules.assets.ais import AisTelemetryAdapter

    kwargs: dict[str, Any] = {
        "endpoint_url": "https://kilde.example/ais",
        "transport": httpx.MockTransport(handler),
    }
    if authenticated:
        kwargs.update(
            client_id="ais-client",
            client_secret="ais-secret",
            token_url="https://kilde.example/oauth2/token",
            scope="ais:read",
        )
    return AisTelemetryAdapter(**kwargs)


class TestAisAdapter:
    @pytest.mark.asyncio
    async def test_ais_adapter_unconfigured_raises(self) -> None:
        from nce.vertical_modules.assets.ais import AisTelemetryAdapter

        adapter = AisTelemetryAdapter(endpoint_url=None, timeout=10.0)
        assert adapter.platform == "ais"
        assert adapter._timeout <= 4.9

        with pytest.raises(NotImplementedError, match="ais") as excinfo:
            await adapter.fetch_samples(uuid.uuid4(), serial="259139000")
        assert "NCE_ASSETS_AIS_ENDPOINT_URL" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_ais_adapter_client_id_without_secret_or_token_url_raises(self) -> None:
        """An open source needs nothing; a claimed authenticated one must
        be fully configured — half of it is not a valid state."""
        from nce.vertical_modules.assets.ais import AisTelemetryAdapter

        adapter = AisTelemetryAdapter(
            endpoint_url="https://kilde.example/ais", client_id="ais-client", timeout=10.0
        )
        with pytest.raises(NotImplementedError) as excinfo:
            await adapter.fetch_samples(uuid.uuid4(), serial="259139000")
        assert "NCE_ASSETS_AIS_CLIENT_SECRET" in str(excinfo.value)
        assert "NCE_ASSETS_AIS_TOKEN_URL" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_ais_adapter_without_a_serial_is_skipped_before_any_http_call(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no HTTP call should be made when serial is missing")

        adapter = _ais_adapter(handler)
        assert await adapter.fetch_samples(uuid.uuid4(), serial=None) == []
        assert await adapter.fetch_samples(uuid.uuid4(), serial="  ") == []

    @pytest.mark.asyncio
    async def test_ais_adapter_a_non_numeric_serial_is_refused_as_not_an_mmsi(self) -> None:
        """MMSI is nine digits; a serial that isn't purely numeric cannot
        be one, and is refused before any network call rather than sent
        as a malformed filter."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no HTTP call should be made for a non-numeric serial")

        adapter = _ais_adapter(handler)
        assert await adapter.fetch_samples(uuid.uuid4(), serial="not-an-mmsi") == []

    @pytest.mark.asyncio
    async def test_ais_adapter_refuses_a_path_outside_its_allow_list(self) -> None:
        """Gate item 1+2 (MLV16 charter, Lane F): a call outside the
        explicit allow-list literal is refused before any HTTP request."""
        from nce.vertical_modules.assets.ais import AisAllowListRefusal

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a refused call must never reach the transport")

        adapter = _ais_adapter(handler)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(AisAllowListRefusal):
                await adapter._gated_request(
                    client, "DELETE", "", headers={"Accept": "application/json"}
                )
            with pytest.raises(AisAllowListRefusal):
                await adapter._gated_request(
                    client, "GET", "vessels/259139000", headers={"Accept": "application/json"}
                )

    @pytest.mark.asyncio
    async def test_ais_adapter_live_http_authenticated(self) -> None:
        """A recorded-fixture-shaped round trip: OAuth2 client-credentials
        via FORM FIELDS (not a Basic header, unlike F-1's YMCS), one call
        returning the whole fleet snapshot, filtered client-side on MMSI —
        never a fabricated per-vessel telemetry endpoint. Known AIS
        'not available' sentinels (511 for heading, 360 for course over
        ground) are filtered, not stored as if real."""
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            seen.append((request.method, url))
            if url == "https://kilde.example/oauth2/token":
                assert request.headers.get("Content-Type") == "application/x-www-form-urlencoded"
                return httpx.Response(200, json={"access_token": "ais-tok", "expires_in": 3600})
            assert request.headers.get("Authorization") == "Bearer ais-tok"
            if request.url.path == "/ais":
                assert request.url.params.get("modelType") == "Full"
                return httpx.Response(
                    200,
                    json=[
                        {
                            "mmsi": 258500000,
                            "name": "NORDKAPP",
                            "latitude": 68.4,
                            "longitude": 15.1,
                            "speedOverGround": 12.3,
                            "trueHeading": 511,
                            "courseOverGround": 87.5,
                        },
                        {
                            "mmsi": 259139000,
                            "name": "NORDLYS",
                            "latitude": 69.6,
                            "longitude": 18.9,
                            "speedOverGround": 0.0,
                            "trueHeading": 182.5,
                            "courseOverGround": 360,
                        },
                    ],
                )
            raise AssertionError(f"unexpected call: {request.method} {url}")

        adapter = _ais_adapter(handler)
        samples = await adapter.fetch_samples(uuid.uuid4(), serial="259139000")
        metrics = {s.metric: s.value for s in samples}
        assert metrics["latitude"] == 69.6
        assert metrics["longitude"] == 18.9
        assert metrics["speedOverGround"] == 0.0
        assert metrics["trueHeading"] == 182.5
        # courseOverGround == 360 is the sentinel for "not available" and
        # must NOT appear as a sample.
        assert "courseOverGround" not in metrics
        assert ("POST", "https://kilde.example/oauth2/token") in seen
        assert ("GET", "https://kilde.example/ais?modelType=Full") in seen

    @pytest.mark.asyncio
    async def test_ais_adapter_open_source_sends_no_authorization_header(self) -> None:
        """No client id configured is a valid, deliberate mode — not a
        half-configured one — matching the host's own documented shape."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert "Authorization" not in request.headers
            return httpx.Response(
                200,
                json=[{"mmsi": 1, "latitude": 59.9, "longitude": 10.7, "trueHeading": 511}],
            )

        adapter = _ais_adapter(handler, authenticated=False)
        samples = await adapter.fetch_samples(uuid.uuid4(), serial="1")
        metrics = {s.metric: s.value for s in samples}
        assert metrics["latitude"] == 59.9

    @pytest.mark.asyncio
    async def test_ais_adapter_unmatched_mmsi_returns_empty_without_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth2/token" in str(request.url):
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(200, json=[])

        adapter = _ais_adapter(handler)
        assert await adapter.fetch_samples(uuid.uuid4(), serial="999999999") == []

    @pytest.mark.asyncio
    async def test_ais_adapter_http_failure_degrades_gracefully(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable")

        adapter = _ais_adapter(handler)
        assert await adapter.fetch_samples(uuid.uuid4(), serial="259139000") == []
