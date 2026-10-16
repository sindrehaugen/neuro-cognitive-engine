"""Unit tests for scheduled cron jobs in nce/cron.py."""

from __future__ import annotations

import os

os.environ.setdefault("NCE_MASTER_KEY", "x" * 32)

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.cron import _decode_saga_payload, _reembedding_tick


@pytest.mark.asyncio
async def test_reembedding_tick_acquires_and_releases_lock():
    mock_pool = MagicMock()
    mock_mongo = MagicMock()

    mock_lock = MagicMock()

    with (
        patch(
            "nce.cron.acquire_cron_lock", new_callable=AsyncMock, return_value=mock_lock
        ) as mock_acquire,
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock) as mock_release,
        patch("nce.cron.ReembeddingWorker") as mock_worker_cls,
    ):
        mock_worker = MagicMock()
        mock_worker.run_once = AsyncMock(return_value={"processed": 5})
        mock_worker_cls.return_value = mock_worker

        await _reembedding_tick(mock_pool, mock_mongo)

        # Verify acquire_cron_lock is called with the correct parameters
        from nce.cron import _REEMBED_INTERVAL

        expected_ttl = _REEMBED_INTERVAL * 60 + 60
        mock_acquire.assert_awaited_once_with("reembedding", expected_ttl)

        # Verify ReembeddingWorker is run
        mock_worker.run_once.assert_awaited_once_with(mock_pool, mock_mongo)

        # Verify lock is released
        mock_release.assert_awaited_once_with(mock_lock)


@pytest.mark.asyncio
async def test_reembedding_tick_bails_if_lock_held():
    mock_pool = MagicMock()
    mock_mongo = MagicMock()

    with (
        patch("nce.cron.acquire_cron_lock", new_callable=AsyncMock, return_value=None),
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock) as mock_release,
        patch("nce.cron.ReembeddingWorker") as mock_worker_cls,
    ):
        await _reembedding_tick(mock_pool, mock_mongo)

        mock_worker_cls.assert_not_called()
        mock_release.assert_not_called()


@pytest.mark.asyncio
async def test_reembedding_tick_failure_alerts():
    mock_pool = MagicMock()
    mock_mongo = MagicMock()
    mock_lock = MagicMock()

    with (
        patch("nce.cron.acquire_cron_lock", new_callable=AsyncMock, return_value=mock_lock),
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock),
        patch("nce.cron.ReembeddingWorker") as mock_worker_cls,
        patch(
            "nce.notifications.dispatcher.dispatch_alert", new_callable=AsyncMock
        ) as mock_dispatch,
    ):
        mock_worker = MagicMock()
        mock_worker.run_once = AsyncMock(side_effect=ValueError("Simulated worker error"))
        mock_worker_cls.return_value = mock_worker

        await _reembedding_tick(mock_pool, mock_mongo)

        mock_dispatch.assert_awaited_once()
        args, kwargs = mock_dispatch.call_args
        assert "Cron Job Failed: reembedding" in args[0]
        assert "Simulated worker error" in args[1]


@pytest.mark.asyncio
async def test_outbox_relay_tick_failure_alerts():
    import asyncpg

    from nce.cron import _outbox_relay_tick

    mock_pool = MagicMock()
    mock_lock = MagicMock()

    with (
        patch("nce.cron.acquire_cron_lock", new_callable=AsyncMock, return_value=mock_lock),
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock),
        patch(
            "nce.outbox_relay.run_outbox_relay_once",
            new_callable=AsyncMock,
            side_effect=asyncpg.PostgresError("Simulated relay DB error"),
        ),
        patch(
            "nce.notifications.dispatcher.dispatch_alert", new_callable=AsyncMock
        ) as mock_dispatch,
    ):
        await _outbox_relay_tick(mock_pool)

        mock_dispatch.assert_awaited_once()
        args, kwargs = mock_dispatch.call_args
        assert "Cron Job Failed: outbox_relay" in args[0]
        assert "Simulated relay DB error" in args[1]


# ---------------------------------------------------------------------------
# _decode_saga_payload -- pure function, no DB. saga_execution_log.payload is
# JSONB, and no asyncpg jsonb codec is registered anywhere in this codebase,
# so a real connection always hands this back as a raw JSON string, never a
# dict. The previous `isinstance(row["payload"], dict)` guard was therefore
# always False against real data -- these tests pin the actual decode
# against a real JSON *string*, the shape a mock previously let this bug
# hide behind entirely.
# ---------------------------------------------------------------------------


def test_decode_saga_payload_parses_a_real_json_string():
    """The actual regression: asyncpg hands this back as a string, never a
    dict -- confirm the string case is the one that now works."""
    assert _decode_saga_payload('{"memory_id": "abc-123"}') == {"memory_id": "abc-123"}


def test_decode_saga_payload_still_accepts_a_dict():
    """Not reachable against a real connection today (no jsonb codec is
    registered), but a dict must still pass through unchanged -- this
    function's contract doesn't assume the no-codec fact holds forever."""
    assert _decode_saga_payload({"memory_id": "abc-123"}) == {"memory_id": "abc-123"}


def test_decode_saga_payload_on_malformed_json_returns_empty_dict():
    """Malformed JSON must not raise into the caller -- the caller only
    ever calls `.get("memory_id")` on the result, so a safe empty dict
    (falling through to the "no memory_id" branch, same as a genuinely
    empty payload) is correct, not a crash."""
    assert _decode_saga_payload("{not valid json") == {}


def test_decode_saga_payload_on_a_json_array_returns_empty_dict():
    """Valid JSON that isn't an object (e.g. a stray array) must not be
    handed to the caller as if it were a payload dict -- `.get()` on a
    list raises AttributeError, which this function must prevent."""
    assert _decode_saga_payload("[1, 2, 3]") == {}


def test_decode_saga_payload_on_none_returns_empty_dict():
    """The pre-fix behavior for the common case (no payload at all) --
    must still return {} directly, not raise on a None input."""
    assert _decode_saga_payload(None) == {}


@pytest.mark.asyncio
async def test_assets_telemetry_tick_persists_cursor_only_after_batch_loop():
    """_assets_telemetry_tick must persist the cursor after processing the batch, not before."""
    import asyncio
    from contextlib import asynccontextmanager

    from nce.cron import _ASSETS_TICK_CURSOR_KEY, _assets_telemetry_tick

    mock_pool = MagicMock()
    mock_lock = MagicMock()
    call_order: list[str] = []

    rows = [
        {"namespace_id": "ns-1", "asset_id": "00000000-0000-0000-0000-000000000001"},
        {"namespace_id": "ns-1", "asset_id": "00000000-0000-0000-0000-000000000002"},
    ]

    @asynccontextmanager
    async def _fake_conn(pool, site=""):
        yield MagicMock()

    async def _fake_pull(engine, params):
        call_order.append(f"pull:{params['asset_id']}")

    async def _fake_store_set(key, val, pool=None):
        call_order.append(f"store_set:{key}={val}")

    with (
        patch("nce.cron.acquire_cron_lock", new_callable=AsyncMock, return_value=mock_lock),
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock),
        patch("nce.cron.unmanaged_pg_connection", _fake_conn),
        patch(
            "nce.cron._fetch_asset_tick_batch",
            new_callable=AsyncMock,
            return_value=(rows, "00000000-0000-0000-0000-000000000002"),
        ),
        patch("nce.settings_store.get", new_callable=AsyncMock, return_value=None),
        patch("nce.settings_store.set", side_effect=_fake_store_set) as mock_set,
        patch("nce.orchestrator.NCEEngine", return_value=MagicMock()),
        patch("nce.vertical_modules.assets.telemetry.do_pull_telemetry", side_effect=_fake_pull),
    ):
        await _assets_telemetry_tick(mock_pool)

    assert call_order == [
        "pull:00000000-0000-0000-0000-000000000001",
        "pull:00000000-0000-0000-0000-000000000002",
        f"store_set:{_ASSETS_TICK_CURSOR_KEY}=00000000-0000-0000-0000-000000000002",
    ]
    mock_set.assert_awaited_once_with(
        _ASSETS_TICK_CURSOR_KEY,
        "00000000-0000-0000-0000-000000000002",
        pool=mock_pool,
    )

    # If the batch loop is cancelled mid-batch, cursor must NOT be advanced
    async def _cancelling_pull(engine, params):
        raise asyncio.CancelledError("cancelled mid-batch")

    with (
        patch("nce.cron.acquire_cron_lock", new_callable=AsyncMock, return_value=mock_lock),
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock),
        patch("nce.cron.unmanaged_pg_connection", _fake_conn),
        patch(
            "nce.cron._fetch_asset_tick_batch",
            new_callable=AsyncMock,
            return_value=(rows, "00000000-0000-0000-0000-000000000002"),
        ),
        patch("nce.settings_store.get", new_callable=AsyncMock, return_value=None),
        patch("nce.settings_store.set", new_callable=AsyncMock) as mock_set_cancel,
        patch("nce.orchestrator.NCEEngine", return_value=MagicMock()),
        patch(
            "nce.vertical_modules.assets.telemetry.do_pull_telemetry",
            side_effect=_cancelling_pull,
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _assets_telemetry_tick(mock_pool)
        mock_set_cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_cron_tick_errors_and_reminders_watcher_throttled_alert():
    """_CRON_TICK_ERRORS includes httpx.HTTPError, BusinessRefusalError, RuntimeError and _reminders_watcher_tick dispatches throttled alert."""
    import httpx

    from nce.cron import _CRON_TICK_ERRORS, _reminders_watcher_tick
    from nce.mcp_errors import BusinessRefusalError

    assert httpx.HTTPError in _CRON_TICK_ERRORS
    assert BusinessRefusalError in _CRON_TICK_ERRORS
    assert RuntimeError in _CRON_TICK_ERRORS

    mock_pool = MagicMock()
    mock_lock = MagicMock()

    with (
        patch("nce.cron.acquire_cron_lock", new_callable=AsyncMock, return_value=mock_lock),
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock) as mock_release,
        patch(
            "nce.cron.unmanaged_pg_connection",
            side_effect=BusinessRefusalError("Reminders refusal"),
        ),
        patch("nce.cron._dispatch_throttled_alert", new_callable=AsyncMock) as mock_throttled_alert,
    ):
        await _reminders_watcher_tick(mock_pool)

        mock_throttled_alert.assert_awaited_once()
        alert_args = mock_throttled_alert.call_args[0]
        assert alert_args[0] == "cron.reminders_watcher"
        assert alert_args[1] == "Cron Job Failed: reminders_watcher"
        assert "BusinessRefusalError" in alert_args[2]
        mock_release.assert_awaited_once_with(mock_lock)


@pytest.mark.asyncio
async def test_reschedule_jobs_covers_all_interval_jobs_and_startup_includes_assets_telemetry():
    """reschedule_jobs reschedules all interval jobs and async_main includes _assets_telemetry_tick."""
    import inspect

    import nce.cron as cron_mod

    fake_scheduler = MagicMock()
    fake_scheduler.running = True

    async def _fake_store_get(key, default=None):
        return default if default is not None else 5

    with (
        patch.object(cron_mod, "scheduler", fake_scheduler),
        patch("nce.settings_store.get", side_effect=_fake_store_get),
    ):
        result = await cron_mod.reschedule_jobs()

    rescheduled_ids = {call.args[0] for call in fake_scheduler.reschedule_job.call_args_list}
    expected_ids = {
        "bridge_subscription_renewal",
        "phase_2_1_reembedding",
        "sleep_consolidation",
        "outbox_relay",
        "chain_verification",
        "tamper_anchor",
        "saga_recovery",
        "actor_trust_scores",
        "phase_2_2_decay_prune",
        "product_eol_watcher",
        "inventory_stock_watcher",
        "agreements_coverage_watcher",
        "economy_recurring_recognition",
        "economy_contract_renewal_watcher",
        "event_retention",
        "assets_telemetry_sync",
        "assets_warranty_eol_watcher",
        "hr_cert_expiry_watcher",
        "vendors_cert_expiry_watcher",
        "support_sla_watcher",
        "hr_compliance_watcher",
        "sales_stalled_deal_watcher",
        "reminders_watcher",
    }
    assert expected_ids.issubset(rescheduled_ids)
    assert result == f"rescheduled {len(rescheduled_ids)} jobs"

    src = inspect.getsource(cron_mod.async_main)
    startup_block = src.split("startup_coros = [", 1)[1].split("]", 1)[0]
    assert "_assets_telemetry_tick(pool)" in startup_block
