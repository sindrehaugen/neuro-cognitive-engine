"""
tests/test_inventory_stock_watcher.py
======================================
Integration tests for the Inventory Stock Watcher (Module 11 Wave 6b,
B134b — restock-watcher).

Matches the ``tests/test_inventory_*.py`` glob in ``.github/workflows/
ci.yml``'s ``Integration -- M11 Inventory`` step — no edit to that workflow
file is needed or permitted for this wave (see the wave brief's amendment).

Assertions (see the wave's Acceptance section):
  (a) an item below its reorder point is flagged low_stock; one AT the point
      is not (boundary pinned).
  (b) `available` is the three-term identity (qty_on_hand - qty_reserved -
      qty_blocked), not a qty_on_hand-only shortcut.
  (c) dead stock is decided from inventory_transactions recency against
      NCE_INVENTORY_DEAD_STOCK_DAYS.
  (d) the zero-ledger-rows case is flagged dead (module docstring decision).
  (e) rationale carries concrete inventory_transactions.id values that still
      resolve after a later movement is inserted (B126 reconstructability).
  (f) namespace isolation via the unprivileged nce_app pool.
  (g) acquire_cron_lock returning None makes the tick a no-op.
  (h) per-namespace error isolation: the middle namespace raises, the third
      is still scanned, and an alert is dispatched for the failing one.
  (i) NCE_INVENTORY_LOW_STOCK_ALERT_ENABLED=False suppresses alerts while
      the scan still runs.
  Plus a plain-unit register_jobs test mirroring
  test_product_eol_watcher.py:358's test_cron_boot_registers_product_eol_watcher.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse, urlunparse

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.inventory.watchers import do_flag_stock_alerts

pytestmark = pytest.mark.integration


class _EngineStub:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pg_pool = pool


async def _seed_location(conn: Any, ns: uuid.UUID) -> uuid.UUID:
    row = await conn.fetchrow(
        """
        INSERT INTO stock_locations (namespace_id, kind, name)
        VALUES ($1::uuid, 'warehouse', $2)
        RETURNING id
        """,
        str(ns),
        f"WH-{uuid.uuid4().hex[:8]}",
    )
    return row["id"]


async def _seed_item(
    conn: Any,
    ns: uuid.UUID,
    location_id: uuid.UUID,
    *,
    sku: str,
    qty_on_hand: str,
    qty_reserved: str = "0",
    qty_blocked: str = "0",
    reorder_point: str = "0",
    created_at_days_ago: int | None = None,
) -> uuid.UUID:
    """Seed one inventory_items row.

    ``created_at_days_ago`` backdates the row's own created_at — used by the
    zero-ledger-rows carve-out tests (ROUND 2): a never-moved item is only
    dead once the ROW ITSELF predates NCE_INVENTORY_DEAD_STOCK_DAYS, not the
    instant it is created.
    """
    if created_at_days_ago is None:
        row = await conn.fetchrow(
            """
            INSERT INTO inventory_items
                (namespace_id, sku, location_id, qty_on_hand, qty_reserved,
                 qty_blocked, reorder_point)
            VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6, $7)
            RETURNING id
            """,
            str(ns),
            sku,
            str(location_id),
            Decimal(qty_on_hand),
            Decimal(qty_reserved),
            Decimal(qty_blocked),
            Decimal(reorder_point),
        )
    else:
        row = await conn.fetchrow(
            """
            INSERT INTO inventory_items
                (namespace_id, sku, location_id, qty_on_hand, qty_reserved,
                 qty_blocked, reorder_point, created_at)
            VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6, $7,
                    now() - ($8 || ' days')::interval)
            RETURNING id
            """,
            str(ns),
            sku,
            str(location_id),
            Decimal(qty_on_hand),
            Decimal(qty_reserved),
            Decimal(qty_blocked),
            Decimal(reorder_point),
            str(created_at_days_ago),
        )
    return row["id"]


async def _seed_ledger_row(
    conn: Any,
    ns: uuid.UUID,
    *,
    sku: str,
    location_id: uuid.UUID,
    delta: str,
    reason_category: str,
    created_at_days_ago: int | None = None,
) -> uuid.UUID:
    """Insert one inventory_transactions row directly (bypasses append_transaction
    only to control created_at for recency fixtures — this test file does not
    touch stock.py or transactions.py behaviour, it only seeds ledger facts)."""
    if created_at_days_ago is None:
        row = await conn.fetchrow(
            """
            INSERT INTO inventory_transactions
                (namespace_id, sku, location_id, delta, reason_category)
            VALUES ($1::uuid, $2, $3::uuid, $4, $5)
            RETURNING id
            """,
            str(ns),
            sku,
            str(location_id),
            Decimal(delta),
            reason_category,
        )
    else:
        row = await conn.fetchrow(
            """
            INSERT INTO inventory_transactions
                (namespace_id, sku, location_id, delta, reason_category, created_at)
            VALUES ($1::uuid, $2, $3::uuid, $4, $5, now() - ($6 || ' days')::interval)
            RETURNING id
            """,
            str(ns),
            sku,
            str(location_id),
            Decimal(delta),
            reason_category,
            str(created_at_days_ago),
        )
    return row["id"]


@pytest_asyncio.fixture
async def ns(make_namespace: Any) -> uuid.UUID:
    return await make_namespace()


@pytest_asyncio.fixture
async def app_pool(pg_pool: asyncpg.Pool) -> AsyncGenerator[asyncpg.Pool, None]:
    """Pool built against the unprivileged nce_app role (FORCE RLS applies).

    Mirrors conftest.py's pg_app_conn DSN construction — the owner pool
    (pg_pool) bypasses FORCE RLS, so the namespace-isolation test (f) must
    not be built from it.
    """
    from nce.config import cfg

    app_dsn = os.getenv("PG_DSN_APP", "").strip()
    primary = (
        os.getenv("NCE_INTEGRATION_PG_DSN") or os.getenv("PG_DSN") or os.getenv("DATABASE_URL", "")
    )
    if not app_dsn or app_dsn == primary:
        parsed = urlparse(primary)
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        app_pass = cfg.NCE_APP_PASSWORD or "nce_app_secret"
        netloc = f"nce_app:{app_pass}@{netloc}"
        app_dsn = urlunparse(parsed._replace(netloc=netloc))

    try:
        pool = await asyncpg.create_pool(app_dsn, min_size=1, max_size=4, command_timeout=60)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"nce_app role not reachable for RLS isolation test: {exc}")
    try:
        yield pool
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# (a) + (b): low-stock boundary + three-term identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_stock_flagged_below_reorder_point_not_at(
    pg_pool: asyncpg.Pool, ns: uuid.UUID
) -> None:
    async with scoped_pg_session(pg_pool, ns) as conn:
        loc = await _seed_location(conn, ns)
        await _seed_item(conn, ns, loc, sku="SKU-BELOW", qty_on_hand="5", reorder_point="6")
        await _seed_item(conn, ns, loc, sku="SKU-AT", qty_on_hand="6", reorder_point="6")

    engine = _EngineStub(pg_pool)
    result = await do_flag_stock_alerts(engine, {"namespace_id": str(ns)})
    low_flags = {f["sku"]: f for f in result["flags"] if f["flag_type"] == "low_stock"}

    assert "SKU-BELOW" in low_flags
    assert "SKU-AT" not in low_flags


@pytest.mark.asyncio
async def test_available_is_three_term_identity(pg_pool: asyncpg.Pool, ns: uuid.UUID) -> None:
    """qty_on_hand=10, qty_reserved=3, qty_blocked=2 vs reorder_point=6.

    available = 10-3-2 = 5 < 6 -> flagged. A qty_on_hand-only shortcut
    (10 >= 6) would wrongly say NOT flagged.
    """
    async with scoped_pg_session(pg_pool, ns) as conn:
        loc = await _seed_location(conn, ns)
        await _seed_item(
            conn,
            ns,
            loc,
            sku="SKU-RESERVED",
            qty_on_hand="10",
            qty_reserved="3",
            qty_blocked="2",
            reorder_point="6",
        )

    engine = _EngineStub(pg_pool)
    result = await do_flag_stock_alerts(engine, {"namespace_id": str(ns)})
    flagged = [f for f in result["flags"] if f["sku"] == "SKU-RESERVED"]

    # SKU-RESERVED has zero inventory_transactions rows AND was just created
    # (created_at ~= now) -- the ROUND 2 carve-out means a never-moved row
    # this fresh is not yet "dead" (see watchers.py's module docstring), so
    # exactly one flag (low_stock) is expected, not two.
    assert len(flagged) == 1
    assert flagged[0]["flag_type"] == "low_stock"
    # Compare by value, not formatting: asyncpg returns NUMERIC(18,3) as
    # Decimal('5.000'), so str() carries the column's fixed scale --
    # "available" is not contractually a formatted string.
    assert Decimal(flagged[0]["available"]) == Decimal("5")
    assert not any(
        f["flag_type"] == "dead_stock" for f in result["flags"] if f["sku"] == "SKU-RESERVED"
    )


# ---------------------------------------------------------------------------
# (c) + (d): dead stock recency + zero-ledger-rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dead_stock_recent_movement_not_flagged_old_movement_is(
    pg_pool: asyncpg.Pool, ns: uuid.UUID
) -> None:
    async with scoped_pg_session(pg_pool, ns) as conn:
        loc = await _seed_location(conn, ns)
        await _seed_item(conn, ns, loc, sku="SKU-RECENT", qty_on_hand="10")
        await _seed_ledger_row(
            conn, ns, sku="SKU-RECENT", location_id=loc, delta="10", reason_category="adjustment"
        )

        await _seed_item(conn, ns, loc, sku="SKU-OLD", qty_on_hand="10")
        await _seed_ledger_row(
            conn,
            ns,
            sku="SKU-OLD",
            location_id=loc,
            delta="10",
            reason_category="adjustment",
            created_at_days_ago=200,
        )

    with patch("nce.vertical_modules.inventory.watchers.cfg") as mock_cfg:
        mock_cfg.NCE_INVENTORY_DEAD_STOCK_DAYS = 180
        engine = _EngineStub(pg_pool)
        result = await do_flag_stock_alerts(engine, {"namespace_id": str(ns)})

    dead_skus = {f["sku"] for f in result["flags"] if f["flag_type"] == "dead_stock"}
    assert "SKU-RECENT" not in dead_skus
    assert "SKU-OLD" in dead_skus


@pytest.mark.asyncio
async def test_zero_ledger_rows_is_flagged_dead_with_empty_rationale(
    pg_pool: asyncpg.Pool, ns: uuid.UUID
) -> None:
    """Zero ledger rows + a row old enough to have moved -> flagged dead.

    ROUND 2 carve-out: the item's own created_at must ALSO predate the
    dead-stock cutoff (see watchers.py's module docstring and
    test_zero_ledger_rows_recent_item_is_not_yet_dead below for the other
    side of this boundary).
    """
    async with scoped_pg_session(pg_pool, ns) as conn:
        loc = await _seed_location(conn, ns)
        await _seed_item(
            conn,
            ns,
            loc,
            sku="SKU-NOHISTORY",
            qty_on_hand="10",
            created_at_days_ago=200,
        )

    with patch("nce.vertical_modules.inventory.watchers.cfg") as mock_cfg:
        mock_cfg.NCE_INVENTORY_DEAD_STOCK_DAYS = 180
        engine = _EngineStub(pg_pool)
        result = await do_flag_stock_alerts(engine, {"namespace_id": str(ns)})
    flagged = [f for f in result["flags"] if f["sku"] == "SKU-NOHISTORY"]

    assert len(flagged) == 1
    assert flagged[0]["flag_type"] == "dead_stock"
    assert flagged[0]["rationale"]["window_ledger_ids"] == []


@pytest.mark.asyncio
async def test_zero_ledger_rows_recent_item_is_not_yet_dead(
    pg_pool: asyncpg.Pool, ns: uuid.UUID
) -> None:
    """Zero ledger rows + a freshly-created row -> NOT flagged dead (ROUND 2).

    The item has not existed long enough to have had the chance to move;
    the carve-out pins this side of the boundary, mirroring
    test_zero_ledger_rows_is_flagged_dead_with_empty_rationale's old-row side.
    """
    async with scoped_pg_session(pg_pool, ns) as conn:
        loc = await _seed_location(conn, ns)
        await _seed_item(conn, ns, loc, sku="SKU-FRESH", qty_on_hand="10")

    with patch("nce.vertical_modules.inventory.watchers.cfg") as mock_cfg:
        mock_cfg.NCE_INVENTORY_DEAD_STOCK_DAYS = 180
        engine = _EngineStub(pg_pool)
        result = await do_flag_stock_alerts(engine, {"namespace_id": str(ns)})
    flagged = [f for f in result["flags"] if f["sku"] == "SKU-FRESH"]

    assert flagged == []


# ---------------------------------------------------------------------------
# (e): rationale reconstructability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rationale_ledger_ids_resolve_and_survive_later_movement(
    pg_pool: asyncpg.Pool, ns: uuid.UUID
) -> None:
    async with scoped_pg_session(pg_pool, ns) as conn:
        loc = await _seed_location(conn, ns)
        await _seed_item(conn, ns, loc, sku="SKU-AUDIT", qty_on_hand="10")
        old_id = await _seed_ledger_row(
            conn,
            ns,
            sku="SKU-AUDIT",
            location_id=loc,
            delta="10",
            reason_category="adjustment",
            created_at_days_ago=200,
        )

    with patch("nce.vertical_modules.inventory.watchers.cfg") as mock_cfg:
        mock_cfg.NCE_INVENTORY_DEAD_STOCK_DAYS = 180
        engine = _EngineStub(pg_pool)
        result = await do_flag_stock_alerts(engine, {"namespace_id": str(ns)})

    flagged = [f for f in result["flags"] if f["sku"] == "SKU-AUDIT"]
    assert len(flagged) == 1
    ids_before = flagged[0]["rationale"]["window_ledger_ids"]
    assert str(old_id) in ids_before

    # Resolve by id — a real query, not a re-run of "last N days".
    async with scoped_pg_session(pg_pool, ns) as conn:
        row = await conn.fetchrow(
            "SELECT id FROM inventory_transactions WHERE id = $1::uuid", old_id
        )
        assert row is not None

        # Insert a later movement — the frozen ids must still resolve and the
        # earlier verdict's rationale is unaffected by re-running (this test
        # asserts the ids remain resolvable, the reconstructability property).
        await _seed_ledger_row(
            conn, ns, sku="SKU-AUDIT", location_id=loc, delta="1", reason_category="adjustment"
        )
        row_after = await conn.fetchrow(
            "SELECT id FROM inventory_transactions WHERE id = $1::uuid", old_id
        )
        assert row_after is not None
        assert str(row_after["id"]) in ids_before


# ---------------------------------------------------------------------------
# (f): namespace isolation via the unprivileged nce_app pool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_namespace_isolation_via_unprivileged_pool(
    pg_pool: asyncpg.Pool, app_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    ns_a = await make_namespace()
    ns_b = await make_namespace()

    async with scoped_pg_session(pg_pool, ns_a) as conn:
        loc_a = await _seed_location(conn, ns_a)
        await _seed_item(conn, ns_a, loc_a, sku="SKU-A", qty_on_hand="1", reorder_point="10")

    async with scoped_pg_session(pg_pool, ns_b) as conn:
        loc_b = await _seed_location(conn, ns_b)
        await _seed_item(conn, ns_b, loc_b, sku="SKU-B", qty_on_hand="1", reorder_point="10")

    engine = _EngineStub(app_pool)
    result_a = await do_flag_stock_alerts(engine, {"namespace_id": str(ns_a)})

    skus_seen = {f["sku"] for f in result_a["flags"]}
    assert "SKU-B" not in skus_seen
    assert "SKU-A" in skus_seen


# ---------------------------------------------------------------------------
# (g): lock no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_none_makes_tick_a_noop() -> None:
    from nce.cron import _inventory_stock_watcher_tick

    with (
        patch("nce.cron.acquire_cron_lock", new_callable=AsyncMock, return_value=None),
        patch(
            "nce.vertical_modules.inventory.watchers.do_flag_stock_alerts",
            new_callable=AsyncMock,
        ) as mock_core,
    ):
        await _inventory_stock_watcher_tick(MagicMock())

    mock_core.assert_not_called()


# ---------------------------------------------------------------------------
# (h): per-namespace error isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_namespace_error_isolation(pg_pool: asyncpg.Pool) -> None:
    from nce.cron import CronLock, _inventory_stock_watcher_tick

    ns1, ns2, ns3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    class _FakeConn:
        async def fetch(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            return [{"id": ns1}, {"id": ns2}, {"id": ns3}]

        async def __aenter__(self) -> _FakeConn:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    seen: list[uuid.UUID] = []

    async def _fake_flag_stock_alerts(_engine: Any, params: dict[str, Any]) -> dict[str, Any]:
        ns_id = params["namespace_id"]
        seen.append(ns_id)
        if str(ns_id) == str(ns2):
            raise ValueError("boom")
        return {"namespace_id": str(ns_id), "scanned": 0, "flags": []}

    with (
        patch(
            "nce.cron.acquire_cron_lock",
            new_callable=AsyncMock,
            return_value=CronLock(
                job_id="inventory_stock_watcher",
                key="lock:inventory_stock_watcher",
                token="t",
                ttl_seconds=60,
            ),
        ),
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock),
        patch("nce.cron.unmanaged_pg_connection", return_value=_FakeConn()),
        patch(
            "nce.vertical_modules.inventory.watchers.do_flag_stock_alerts",
            side_effect=_fake_flag_stock_alerts,
        ),
        patch("nce.cron._dispatch_throttled_alert", new_callable=AsyncMock) as mock_alert,
    ):
        await _inventory_stock_watcher_tick(pg_pool)

    assert [str(x) for x in seen] == [str(ns1), str(ns2), str(ns3)]
    alert_keys = [c.args[0] for c in mock_alert.call_args_list]
    assert any(str(ns2) in k for k in alert_keys)


# ---------------------------------------------------------------------------
# (i): alert interlock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_disabled_suppresses_dispatch_while_scan_runs(pg_pool: asyncpg.Pool) -> None:
    from nce.cron import CronLock, _inventory_stock_watcher_tick

    ns1 = uuid.uuid4()

    class _FakeConn:
        async def fetch(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            return [{"id": ns1}]

        async def __aenter__(self) -> _FakeConn:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    async def _fake_flag_stock_alerts(_engine: Any, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "namespace_id": str(params["namespace_id"]),
            "scanned": 1,
            "flags": [{"flag_type": "low_stock", "sku": "X", "location_id": "loc"}],
        }

    with (
        patch(
            "nce.cron.acquire_cron_lock",
            new_callable=AsyncMock,
            return_value=CronLock(
                job_id="inventory_stock_watcher",
                key="lock:inventory_stock_watcher",
                token="t",
                ttl_seconds=60,
            ),
        ),
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock),
        patch("nce.cron.unmanaged_pg_connection", return_value=_FakeConn()),
        patch(
            "nce.vertical_modules.inventory.watchers.do_flag_stock_alerts",
            side_effect=_fake_flag_stock_alerts,
        ),
        patch("nce.cron.cfg") as mock_cfg,
        patch("nce.cron._dispatch_throttled_alert", new_callable=AsyncMock) as mock_alert,
    ):
        mock_cfg.NCE_INVENTORY_LOW_STOCK_ALERT_ENABLED = False
        mock_cfg.NCE_INVENTORY_STOCK_WATCHER_INTERVAL_MINUTES = 1440
        await _inventory_stock_watcher_tick(pg_pool)

    mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# register_jobs registration (plain unit test, mirrors
# test_product_eol_watcher.py:358 test_cron_boot_registers_product_eol_watcher)
# ---------------------------------------------------------------------------


class StopMain(Exception):
    pass


@pytest.mark.asyncio
async def test_cron_boot_registers_inventory_stock_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cron boot calls ``cfg.validate()``, which requires MinIO credentials even
    # though this test needs no object store (and no database -- the scheduler is
    # mocked).  CI supplies them in the UNIT job (ci.yml:69-70) but NOT in the M11
    # integration step, and this module carries a blanket
    # ``pytestmark = pytest.mark.integration``, so without this the test fails on a
    # missing credential in CI while passing locally.  Supplying them here makes the
    # test independent of which job runs it.  (The precedent test
    # ``test_cron_boot_registers_product_eol_watcher`` avoids this by not being
    # integration-marked at all; this module cannot do that, since 10 of its 11
    # tests genuinely need a database.)
    # NOTE: setenv is NOT enough -- ``validate_minio_credentials`` reads the CLASS
    # attribute ``cls.MINIO_ACCESS_KEY`` (config.py:1274), bound from the environment at
    # import time.  ``cfg`` is an INSTANCE, so patching it leaves the class attribute the
    # classmethod actually reads untouched -- patch ``type(cfg)``.
    from nce.config import cfg

    monkeypatch.setattr(type(cfg), "MINIO_ACCESS_KEY", "test-minio-access-key", raising=False)
    monkeypatch.setattr(type(cfg), "MINIO_SECRET_KEY", "test-minio-secret-key", raising=False)

    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()

    with (
        patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool),
        patch("asyncio.Event.wait", side_effect=StopMain),
        patch("nce.cron._renewal_tick", new_callable=AsyncMock),
        patch("nce.cron._reembedding_tick", new_callable=AsyncMock),
        patch("nce.cron._consolidation_tick", new_callable=AsyncMock),
        patch("nce.cron._partition_maintenance_tick", new_callable=AsyncMock),
        patch("nce.cron._saga_recovery_tick", new_callable=AsyncMock),
        patch("nce.cron._outbox_relay_tick", new_callable=AsyncMock),
        patch("nce.cron._decay_prune_tick", new_callable=AsyncMock),
        patch("nce.cron._product_eol_watcher_tick", new_callable=AsyncMock),
        patch("nce.cron._inventory_stock_watcher_tick", new_callable=AsyncMock),
        patch("nce.cron._chain_verification_tick", new_callable=AsyncMock),
        patch("nce.cron._d365_sync_tick", new_callable=AsyncMock),
        patch("nce.cron._d365_netbox_bridge_tick", new_callable=AsyncMock),
    ):
        added_job_ids: list[str] = []
        added_triggers: dict[str, Any] = {}

        def _add_job(func: Any, trigger: Any, *args: Any, **kwargs: Any) -> None:
            job_id = kwargs.get("id", "")
            added_job_ids.append(job_id)
            added_triggers[job_id] = trigger

        with patch("nce.cron.AsyncIOScheduler") as mock_scheduler_cls:
            mock_scheduler = MagicMock()
            mock_scheduler.add_job = _add_job
            mock_scheduler_cls.return_value = mock_scheduler

            try:
                from nce.cron import async_main

                await async_main()
            except StopMain:
                pass

    assert "inventory_stock_watcher" in added_job_ids, (
        f"inventory_stock_watcher not found in registered job ids: {added_job_ids}"
    )
