"""
Bridge subscription renewal scheduler (§10.7).

Runs an APScheduler interval job that calls ``renew_expiring_subscriptions``:
subscriptions with ``expires_at`` within ``BRIDGE_RENEWAL_LOOKAHEAD_HOURS`` are
renewed via provider APIs; failures mark rows ``DEGRADED``.

Run (from repo root, with env / PG_DSN configured)::

    python -m nce.cron
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from nce.bridge_renewal import renew_expiring_subscriptions
from nce.config import cfg
from nce.cron_lock import CronLock, acquire_cron_lock, release_cron_lock
from nce.db_utils import scoped_pg_session, unmanaged_pg_connection
from nce.reembedding_worker import CRON_INTERVAL_MINUTES as _REEMBED_INTERVAL
from nce.reembedding_worker import ReembeddingWorker
from nce.temporal_decay import _decay_prune_tick, register_decay_jobs

log = logging.getLogger("nce.cron")

# Cron ticks must never crash the scheduler; catch operational failures only.
_CRON_TICK_ERRORS: tuple[type[BaseException], ...] = (
    asyncpg.PostgresError,
    OSError,
    TimeoutError,
    ValueError,
    TypeError,
    KeyError,
    json.JSONDecodeError,
)

_ALERT_THROTTLE_CACHE: dict[str, float] = {}
_THROTTLE_WINDOW_SECONDS = 300.0


async def _dispatch_throttled_alert(key: str, title: str, message: str) -> None:
    now = time.time()
    last_sent = _ALERT_THROTTLE_CACHE.get(key, 0.0)
    if now - last_sent >= _THROTTLE_WINDOW_SECONDS:
        _ALERT_THROTTLE_CACHE[key] = now
        try:
            from nce.notifications import dispatcher

            await dispatcher.dispatch_alert(title, message)
        except Exception:
            log.exception("Failed to dispatch throttled alert for key %s", key)


async def _renewal_tick(pool: asyncpg.Pool) -> None:
    ttl = cfg.BRIDGE_CRON_INTERVAL_MINUTES * 60 + 60
    lock: CronLock | None = await acquire_cron_lock("bridge_subscription_renewal", ttl)
    if lock is None:
        log.debug("Skipping bridge_subscription_renewal — lock held by another instance")
        return
    try:
        stats = await renew_expiring_subscriptions(pool)
        log.info("bridge renewal tick: %s", stats)
    except _CRON_TICK_ERRORS as exc:
        log.exception("bridge renewal tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.bridge_subscription_renewal",
            "Cron Job Failed: bridge_subscription_renewal",
            f"Bridge subscription renewal tick failed: {type(exc).__name__}: {exc}",
        )
    finally:
        await release_cron_lock(lock)


async def _consolidation_tick(pool: asyncpg.Pool, mongo_client: Any | None = None) -> None:
    """
    Run sleep consolidation for each namespace with metadata.consolidation.enabled=true.

    Sequential per-namespace runs; failures are logged and do not stop other namespaces.

    Mongo is optional for tests / degraded runs; when set, episodic payloads are hydrated
    in bulk before consolidation LLM calls.
    """
    ttl = min(cfg.CONSOLIDATION_CRON_INTERVAL_MINUTES * 60, 7200) + 60
    lock: CronLock | None = await acquire_cron_lock("sleep_consolidation", ttl)
    if lock is None:
        log.debug("Skipping sleep_consolidation — lock held by another instance")
        return
    try:
        from nce.consolidation import ConsolidationWorker
        from nce.providers import get_provider

        # namespaces is a global admin table — unmanaged connection is correct here.
        async with unmanaged_pg_connection(pool, site="cron.consolidation.namespaces_scan") as conn:
            rows = await conn.fetch("""
                SELECT id, metadata FROM namespaces
                WHERE COALESCE((metadata->'consolidation'->>'enabled')::boolean, false) = true
                """)
        for row in rows:
            ns_id: UUID = row["id"]
            raw_meta = row["metadata"]
            if raw_meta is None:
                meta: dict = {}
            elif isinstance(raw_meta, dict):
                meta = raw_meta
            else:
                meta = json.loads(raw_meta)
            try:
                provider = get_provider(meta or {})
                worker = ConsolidationWorker(pool, provider, mongo_client=mongo_client)
                await worker.run_consolidation(ns_id)
                log.info("consolidation tick completed for namespace %s", ns_id)
            except _CRON_TICK_ERRORS as exc:
                log.exception("consolidation tick failed for namespace %s", ns_id)
                await _dispatch_throttled_alert(
                    f"cron.sleep_consolidation.{ns_id}",
                    f"Consolidation Failed: Namespace {ns_id}",
                    f"Consolidation tick failed for namespace {ns_id}: {type(exc).__name__}: {exc}",
                )
    except _CRON_TICK_ERRORS as exc:
        log.exception("consolidation tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.sleep_consolidation.global",
            "Cron Job Failed: sleep_consolidation",
            f"Sleep consolidation tick failed unexpectedly: {type(exc).__name__}: {exc}",
        )
    finally:
        await release_cron_lock(lock)


async def _partition_maintenance_tick(pool: asyncpg.Pool) -> None:
    """
    Ensure event_log monthly partitions exist ahead of time.
    Re-entrant: the PostgreSQL function uses IF NOT EXISTS.
    """
    lock: CronLock | None = await acquire_cron_lock("event_log_partition_maintenance", 3600)
    if lock is None:
        log.debug("Skipping event_log_partition_maintenance — lock held by another instance")
        return
    try:
        async with unmanaged_pg_connection(pool, site="cron.partition_maintenance") as conn:
            await conn.execute(
                f"SELECT nce_ensure_event_log_monthly_partitions({cfg.NCE_PARTITION_LOOKAHEAD_MONTHS})"
            )
            # Update Prometheus gauge with how many future partitions exist
            row = await conn.fetchrow(
                """
                SELECT count(*) AS cnt
                FROM pg_inherits i
                JOIN pg_class c ON c.oid = i.inhrelid
                WHERE i.inhparent = 'event_log'::regclass
                  AND c.relname LIKE 'event_log_%'
                  AND c.relname > 'event_log_' || to_char(now(), 'YYYY_MM')
                """
            )
            from nce.observability import EVENT_LOG_PARTITION_MONTHS_AHEAD

            months_ahead = row["cnt"] if row else 0
            EVENT_LOG_PARTITION_MONTHS_AHEAD.set(months_ahead)
            log.info("event_log partition maintenance complete: %s months ahead", months_ahead)
            if months_ahead < 2:
                log.warning(
                    "event_log partition runway low: only %s months ahead (need >= 2)",
                    months_ahead,
                )
    except _CRON_TICK_ERRORS as exc:
        log.exception("event_log partition maintenance tick failed")
        await _dispatch_throttled_alert(
            "cron.event_log_partition_maintenance",
            "Cron Job Failed: event_log_partition_maintenance",
            f"Event log partition maintenance tick failed: {type(exc).__name__}: {exc}",
        )
    finally:
        await release_cron_lock(lock)


async def _saga_recovery_tick(pool: asyncpg.Pool) -> None:
    """
    Finalize sagas that committed to PG but never advanced to 'completed'.

    A saga in state 'pg_committed' older than 5 minutes means the application
    crashed between the PG commit and the downstream completion signal. Because
    the memory already exists in Postgres (pg_committed = data is durable), the
    correct recovery action is to VERIFY and COMPLETE, NOT to rollback.

    Recovery steps per saga:
      1. Verify the target memory row exists in memories.
      2. If it exists: mark saga 'completed' + append 'saga_recovered' event.
      3. If it is missing: the saga committed but the memory row was lost (rare) —
         mark 'failed' for manual review rather than attempting blind rollback.

    We do NOT soft-delete (valid_to=now()) pg_committed memories.
    'pg_committed' means PG says the memory is there — trust the DB.
    """
    lock: CronLock | None = await acquire_cron_lock("saga_recovery", 600)
    if lock is None:
        log.debug("Skipping saga_recovery — lock held by another instance")
        return
    try:
        from nce.event_log import append_event

        # Read saga candidates without an RLS scope — saga_execution_log is a
        # global admin table, not tenant-partitioned by RLS.
        async with unmanaged_pg_connection(pool, site="cron.saga_recovery.list_stuck") as conn:
            rows = await conn.fetch(
                """
                SELECT id, namespace_id, agent_id, payload
                FROM saga_execution_log
                WHERE state = 'pg_committed'
                  AND COALESCE(updated_at, created_at) < now() - interval '5 minutes'
                ORDER BY COALESCE(updated_at, created_at)
                LIMIT 100
                """
            )

        for row in rows:
            saga_id: str = str(row["id"])
            ns_id: str = str(row["namespace_id"])
            agent_id: str = row["agent_id"]
            payload: dict = row["payload"] if isinstance(row["payload"], dict) else {}
            memory_id = payload.get("memory_id")

            log.warning(
                "[SAGA-RECOVERY] Found pg_committed saga=%s memory_id=%s — verifying memory exists",
                saga_id,
                memory_id,
            )
            try:
                if memory_id:
                    # Step 1: verify memory exists via RLS-scoped session.
                    async with scoped_pg_session(pool, ns_id) as conn:
                        memory_row = await conn.fetchrow(
                            """
                            SELECT id FROM memories
                            WHERE id = $1::uuid AND namespace_id = $2::uuid
                            """,
                            memory_id,
                            ns_id,
                        )

                    if memory_row is None:
                        # Memory row is missing despite pg_committed state.
                        # Do NOT rollback blindly — mark failed for human review.
                        log.error(
                            "[SAGA-RECOVERY] saga=%s is pg_committed but memory=%s is "
                            "MISSING from memories table. Marking 'failed' for manual review.",
                            saga_id,
                            memory_id,
                        )
                        async with unmanaged_pg_connection(
                            pool, site="cron.saga_recovery.mark_failed"
                        ) as conn:
                            await conn.execute(
                                """
                                UPDATE saga_execution_log
                                SET state = 'failed', updated_at = NOW()
                                WHERE id = $1::uuid AND state = 'pg_committed'
                                """,
                                saga_id,
                            )
                        continue

                    # Step 2: memory exists — finalize saga + append recovery event.
                    async with scoped_pg_session(pool, ns_id) as conn:
                        await append_event(
                            conn=conn,
                            namespace_id=UUID(ns_id),
                            agent_id=agent_id,
                            event_type="saga_recovered",
                            params={
                                "memory_id": memory_id,
                                "saga_id": saga_id,
                                "recovery_action": "finalized",
                                "reason": "pg_committed_saga_recovery_cron",
                            },
                        )
                        await conn.execute(
                            """
                            UPDATE saga_execution_log
                            SET state = 'completed', updated_at = NOW()
                            WHERE id = $1::uuid AND state = 'pg_committed'
                            """,
                            saga_id,
                        )
                    log.info("[SAGA-RECOVERY] Finalized saga=%s memory=%s", saga_id, memory_id)

                else:
                    # No memory_id in payload — mark completed, nothing to verify.
                    log.warning(
                        "[SAGA-RECOVERY] saga=%s has no memory_id in payload. "
                        "Marking completed (no memory to verify).",
                        saga_id,
                    )
                    async with unmanaged_pg_connection(
                        pool, site="cron.saga_recovery.mark_completed_no_memory"
                    ) as conn:
                        await conn.execute(
                            """
                            UPDATE saga_execution_log
                            SET state = 'completed', updated_at = NOW()
                            WHERE id = $1::uuid AND state = 'pg_committed'
                            """,
                            saga_id,
                        )

            except _CRON_TICK_ERRORS as exc:
                log.exception("[SAGA-RECOVERY] Failed to recover saga=%s", saga_id)
                await _dispatch_throttled_alert(
                    f"cron.saga_recovery.{saga_id}",
                    f"Saga Recovery Failed: Saga {saga_id}",
                    f"Saga recovery tick failed for saga {saga_id}: {type(exc).__name__}: {exc}",
                )

    except _CRON_TICK_ERRORS as exc:
        log.exception("saga recovery tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.saga_recovery.global",
            "Cron Job Failed: saga_recovery",
            f"Saga recovery tick failed unexpectedly: {type(exc).__name__}: {exc}",
        )
    finally:
        await release_cron_lock(lock)


async def _outbox_relay_tick(pool: asyncpg.Pool) -> None:
    """Drain pending outbox events (same relay as MCP stdio background loop)."""
    from nce.outbox_relay import run_outbox_relay_once

    ttl = max(cfg.OUTBOX_RELAY_INTERVAL_SECONDS * 2, 30)
    lock: CronLock | None = await acquire_cron_lock("outbox_relay", ttl)
    if lock is None:
        log.debug("Skipping outbox_relay — lock held by another instance")
        return
    try:
        delivered = await run_outbox_relay_once(pool)
        if delivered:
            log.info("outbox relay tick delivered=%s", delivered)
    except _CRON_TICK_ERRORS as exc:
        log.exception("outbox relay tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.outbox_relay",
            "Cron Job Failed: outbox_relay",
            f"Outbox relay tick failed: {type(exc).__name__}: {exc}",
        )
    finally:
        await release_cron_lock(lock)


async def _reembedding_tick(pool: asyncpg.Pool, mongo_client: Any) -> None:
    """
    APScheduler job: run one re-embedding sweep.

    Non-fatal — a failure is logged but does not crash the scheduler.
    This tick is coalesced (max_instances=1) so a slow run cannot pile up.
    """
    ttl = _REEMBED_INTERVAL * 60 + 60
    lock: CronLock | None = await acquire_cron_lock("reembedding", ttl)
    if lock is None:
        log.debug("Skipping reembedding — lock held by another instance")
        return
    try:
        worker = ReembeddingWorker()
        stats = await worker.run_once(pool, mongo_client)
        log.info("re-embedding tick: %s", stats)
    except _CRON_TICK_ERRORS as exc:
        log.exception("re-embedding tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.reembedding",
            "Cron Job Failed: reembedding",
            f"Re-embedding tick failed: {type(exc).__name__}: {exc}",
        )
    finally:
        await release_cron_lock(lock)


async def _d365_sync_tick(pool: asyncpg.Pool) -> None:
    """
    APScheduler job: run a Dataverse entity sync for all D365-enabled namespaces.

    When ``NCE_D365_INCREMENTAL_ENABLED`` is true the tick calls
    ``run_incremental_sync`` which applies per-entity ``modifiedon gt <cursor>``
    watermarks and fetches only the delta.  Otherwise the legacy full-pull path
    (``run_full_sync``) is used.

    Singleton via CronLock — a slow run on one instance prevents other replicas
    from starting a duplicate sync cycle.  Non-fatal: errors are logged and
    do not crash the scheduler.  Only runs when ``NCE_D365_ENABLED=true``.
    """
    if not cfg.NCE_D365_ENABLED:
        return

    ttl = cfg.NCE_D365_SYNC_INTERVAL_MINUTES * 60 + 60
    lock: CronLock | None = await acquire_cron_lock("d365_entity_sync", ttl)
    if lock is None:
        log.debug("Skipping d365_entity_sync — lock held by another instance")
        return

    import redis.asyncio as aioredis

    redis_client = aioredis.from_url(cfg.REDIS_URL)
    try:
        from nce.db_utils import scoped_pg_session
        from nce.vertical_modules.dynamics365.auth import DataverseTokenManager
        from nce.vertical_modules.dynamics365.client import DataverseClient
        from nce.vertical_modules.dynamics365.sync import DataverseSyncEngine

        token_mgr = DataverseTokenManager(redis_client)

        # Scan namespaces that have D365 integration enabled in their metadata.
        async with unmanaged_pg_connection(pool, site="cron.d365_sync.namespace_scan") as conn:
            rows = await conn.fetch(
                """
                SELECT id FROM namespaces
                WHERE COALESCE((metadata->'d365'->>'enabled')::boolean, false) = true
                """
            )

        if not rows:
            log.debug("d365_sync_tick: no namespaces with d365.enabled=true")
            return

        for row in rows:
            ns_id: UUID = row["id"]
            try:
                token = await token_mgr.get_access_token()
                client = DataverseClient(cfg.NCE_D365_ORG_URL, token)
                async with scoped_pg_session(pool, str(ns_id)) as conn:
                    engine = DataverseSyncEngine(conn, ns_id, client)
                    if cfg.NCE_D365_INCREMENTAL_ENABLED:
                        stats = await engine.run_incremental_sync()
                    else:
                        stats = await engine.run_full_sync()
                    log.info("D365 sync tick namespace=%s stats=%s", ns_id, stats)

                # Update last_sync_at in d365_integrations if the row exists
                async with unmanaged_pg_connection(
                    pool, site="cron.d365_sync.update_stats"
                ) as conn:
                    await conn.execute(
                        """
                        UPDATE d365_integrations
                        SET last_sync_at = NOW(), last_sync_stats = $1::jsonb, updated_at = NOW()
                        WHERE namespace_id = $2::uuid AND status = 'ACTIVE'
                        """,
                        json.dumps(stats),
                        ns_id,
                    )
            except _CRON_TICK_ERRORS as exc:
                log.exception("D365 sync tick failed for namespace=%s", ns_id)
                await _dispatch_throttled_alert(
                    f"cron.d365_entity_sync.{ns_id}",
                    f"D365 Sync Failed: Namespace {ns_id}",
                    f"D365 sync tick failed for namespace {ns_id}: {type(exc).__name__}: {exc}",
                )
    except _CRON_TICK_ERRORS as exc:
        log.exception("D365 sync tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.d365_entity_sync.global",
            "Cron Job Failed: d365_entity_sync",
            f"D365 sync tick failed unexpectedly: {type(exc).__name__}: {exc}",
        )
    finally:
        await redis_client.aclose()
        await release_cron_lock(lock)


async def _d365_weekly_full_sync_tick(pool: asyncpg.Pool) -> None:
    """
    APScheduler job: weekly full-refresh pass for all D365-enabled namespaces.

    Runs every Sunday at 02:00 UTC.  Fetches every entity without a
    ``modifiedon`` filter so that records deleted from Dataverse since the last
    incremental tick are detected and retired via ``detect_and_retire_deletions``.
    After a successful full pull the per-entity cursor map in
    ``last_sync_stats`` is re-seeded with ``max(modifiedon)`` so the next
    incremental tick correctly resumes from the weekly baseline.

    Only active when ``NCE_D365_ENABLED=true``.  Singleton via its own
    ``CronLock`` (``d365_weekly_full_sync``) to prevent duplicate runs across
    replicas.
    """
    if not cfg.NCE_D365_ENABLED:
        return

    # TTL = 23 h so the lock expires well before the next weekly window.
    lock: CronLock | None = await acquire_cron_lock("d365_weekly_full_sync", 23 * 3600)
    if lock is None:
        log.debug("Skipping d365_weekly_full_sync — lock held by another instance")
        return

    import redis.asyncio as aioredis

    redis_client = aioredis.from_url(cfg.REDIS_URL)
    try:
        from nce.db_utils import scoped_pg_session
        from nce.vertical_modules.dynamics365.auth import DataverseTokenManager
        from nce.vertical_modules.dynamics365.client import DataverseClient
        from nce.vertical_modules.dynamics365.sync import DataverseSyncEngine

        token_mgr = DataverseTokenManager(redis_client)

        async with unmanaged_pg_connection(
            pool, site="cron.d365_weekly_sync.namespace_scan"
        ) as conn:
            rows = await conn.fetch(
                """
                SELECT id FROM namespaces
                WHERE COALESCE((metadata->'d365'->>'enabled')::boolean, false) = true
                """
            )

        if not rows:
            log.debug("d365_weekly_full_sync_tick: no namespaces with d365.enabled=true")
            return

        for row in rows:
            ns_id: UUID = row["id"]
            try:
                token = await token_mgr.get_access_token()
                client = DataverseClient(cfg.NCE_D365_ORG_URL, token)
                async with scoped_pg_session(pool, str(ns_id)) as conn:
                    engine = DataverseSyncEngine(conn, ns_id, client)
                    stats = await engine.run_weekly_full_sync()
                    log.info("D365 weekly full-sync tick namespace=%s stats=%s", ns_id, stats)

                async with unmanaged_pg_connection(
                    pool, site="cron.d365_weekly_sync.update_stats"
                ) as conn:
                    await conn.execute(
                        """
                        UPDATE d365_integrations
                        SET last_sync_at = NOW(), last_sync_stats = $1::jsonb, updated_at = NOW()
                        WHERE namespace_id = $2::uuid AND status = 'ACTIVE'
                        """,
                        json.dumps(stats),
                        ns_id,
                    )
            except _CRON_TICK_ERRORS as exc:
                log.exception("D365 weekly full-sync tick failed for namespace=%s", ns_id)
                await _dispatch_throttled_alert(
                    f"cron.d365_weekly_full_sync.{ns_id}",
                    f"D365 Weekly Sync Failed: Namespace {ns_id}",
                    f"D365 weekly full-sync tick failed for namespace {ns_id}: "
                    f"{type(exc).__name__}: {exc}",
                )
    except _CRON_TICK_ERRORS as exc:
        log.exception("D365 weekly full-sync tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.d365_weekly_full_sync.global",
            "Cron Job Failed: d365_weekly_full_sync",
            f"D365 weekly full-sync tick failed unexpectedly: {type(exc).__name__}: {exc}",
        )
    finally:
        await redis_client.aclose()
        await release_cron_lock(lock)


async def _d365_netbox_bridge_tick(pool: asyncpg.Pool) -> None:
    """
    APScheduler job: cross-reference D365 Accounts/FunctionalLocations with NetBox
    Tenants/Sites for all D365-enabled namespaces.

    Requires ``NCE_D365_NETBOX_BRIDGE_ENABLED=true``, ``NCE_NETBOX_URL``, and
    ``NCE_NETBOX_TOKEN``.  Guard: CronLock prevents duplicate runs across replicas.
    """
    if not cfg.NCE_D365_NETBOX_BRIDGE_ENABLED:
        return
    if not cfg.NCE_NETBOX_URL or not cfg.NCE_NETBOX_TOKEN:
        log.warning("d365_netbox_bridge_tick skipped: NCE_NETBOX_URL or NCE_NETBOX_TOKEN not set")
        return

    ttl = cfg.NCE_D365_NETBOX_BRIDGE_INTERVAL_MINUTES * 60 + 60
    lock: CronLock | None = await acquire_cron_lock("d365_netbox_bridge", ttl)
    if lock is None:
        log.debug("Skipping d365_netbox_bridge — lock held by another instance")
        return

    import redis.asyncio as aioredis

    redis_client = aioredis.from_url(cfg.REDIS_URL)
    try:
        from nce.db_utils import scoped_pg_session
        from nce.vertical_modules.dynamics365.auth import DataverseTokenManager
        from nce.vertical_modules.dynamics365.client import DataverseClient
        from nce.vertical_modules.dynamics365.netbox_bridge import (
            D365NetBoxBridge,
            NetBoxBridgeClient,
        )

        token_mgr = DataverseTokenManager(redis_client)

        async with unmanaged_pg_connection(
            pool, site="cron.d365_netbox_bridge.namespace_scan"
        ) as conn:
            rows = await conn.fetch(
                """
                SELECT id FROM namespaces
                WHERE COALESCE((metadata->'d365'->>'enabled')::boolean, false) = true
                """
            )

        if not rows:
            log.debug("d365_netbox_bridge_tick: no namespaces with d365.enabled=true")
            return

        nb_client = NetBoxBridgeClient(
            base_url=cfg.NCE_NETBOX_URL,
            token=cfg.NCE_NETBOX_TOKEN,
        )

        for row in rows:
            ns_id: UUID = row["id"]
            try:
                token = await token_mgr.get_access_token()
                d365_client = DataverseClient(cfg.NCE_D365_ORG_URL, token)
                async with scoped_pg_session(pool, str(ns_id)) as conn:
                    bridge = D365NetBoxBridge(
                        conn=conn,
                        namespace_id=ns_id,
                        d365_client=d365_client,
                        netbox_client=nb_client,
                    )
                    stats = await bridge.run_full_bridge_sync()
                    log.info("D365↔NetBox bridge tick ns=%s stats=%s", ns_id, stats)
            except _CRON_TICK_ERRORS as exc:
                log.exception("D365↔NetBox bridge tick failed for namespace=%s", ns_id)
                await _dispatch_throttled_alert(
                    f"cron.d365_netbox_bridge.{ns_id}",
                    f"D365 NetBox Bridge Failed: Namespace {ns_id}",
                    f"D365 NetBox bridge tick failed for namespace {ns_id}: {type(exc).__name__}: {exc}",
                )
    except _CRON_TICK_ERRORS as exc:
        log.exception("D365↔NetBox bridge tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.d365_netbox_bridge.global",
            "Cron Job Failed: d365_netbox_bridge",
            f"D365 NetBox bridge tick failed unexpectedly: {type(exc).__name__}: {exc}",
        )
    finally:
        await redis_client.aclose()
        await release_cron_lock(lock)


async def _product_eol_watcher_tick(pool: asyncpg.Pool) -> None:
    """
    APScheduler job: scan all namespaces for EOL/EOS products and write
    ``replaced_by`` edges (confidence on the edge) to successor SKUs.

    Singleton via CronLock — prevents duplicate runs across replicas.
    Watcher role: observe + write ``replaced_by`` edges only; never mutates
    ``product_catalog`` rows, prices, or lifecycle_status.
    """
    ttl = cfg.NCE_PRODUCT_EOL_WATCHER_INTERVAL_MINUTES * 60 + 60
    lock: CronLock | None = await acquire_cron_lock("product_eol_watcher", ttl)
    if lock is None:
        log.debug("Skipping product_eol_watcher — lock held by another instance")
        return
    try:
        from nce.vertical_modules.product.watchers import do_check_eol

        class _PoolEngine:
            def __init__(self, p: asyncpg.Pool) -> None:
                self.pg_pool = p

        engine = _PoolEngine(pool)

        async with unmanaged_pg_connection(
            pool, site="cron.product_eol_watcher.namespace_scan"
        ) as conn:
            rows = await conn.fetch("SELECT id FROM namespaces")

        for row in rows:
            ns_id: UUID = row["id"]
            try:
                stats = await do_check_eol(engine, {"namespace_id": ns_id})
                if stats.get("edges_written", 0) > 0:
                    log.info("product_eol_watcher tick namespace=%s stats=%s", ns_id, stats)
                else:
                    log.debug("product_eol_watcher tick namespace=%s stats=%s", ns_id, stats)
            except _CRON_TICK_ERRORS as exc:
                log.exception("product_eol_watcher tick failed for namespace=%s", ns_id)
                await _dispatch_throttled_alert(
                    f"cron.product_eol_watcher.{ns_id}",
                    f"EOL Watcher Failed: Namespace {ns_id}",
                    f"Product EOL watcher tick failed for namespace {ns_id}: {type(exc).__name__}: {exc}",
                )
    except _CRON_TICK_ERRORS as exc:
        log.exception("product_eol_watcher tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.product_eol_watcher.global",
            "Cron Job Failed: product_eol_watcher",
            f"Product EOL watcher tick failed unexpectedly: {type(exc).__name__}: {exc}",
        )
    finally:
        await release_cron_lock(lock)


# Cap how many per-namespace flags are embedded in one alert message —
# mirrors _AGREEMENTS_ALERT_MAX_DETAILS' alert-storm control (cron.py:714).
_INVENTORY_STOCK_ALERT_MAX_DETAILS: int = 5


async def _inventory_stock_watcher_tick(pool: asyncpg.Pool) -> None:
    """
    APScheduler job: scan all namespaces for low-stock and dead-stock flags
    (Module 11 Wave 6b — restock-watcher, B134b).

    Singleton via CronLock — prevents duplicate runs across replicas.
    Watcher role: observe + alert only; ``do_flag_stock_alerts`` is
    return-only and writes nothing (no kg_nodes, no kg_edges, no
    inventory_items, no event_log).

    No opt-in filter: unlike ``_agreements_coverage_watcher_tick``, this tick
    scans **every** namespace — Inventory's namespace opt-in gate
    (``metadata.inventory.enabled``) is B140a, a separate wave, and is not
    installed here (see the wave brief's OUT OF SCOPE). The safety interlock
    against unsolicited alerting is instead
    ``NCE_INVENTORY_LOW_STOCK_ALERT_ENABLED``, checked below: with it False,
    every namespace is still scanned and logged, but no alert is dispatched.

    Joins ``startup_coros`` (fires once at boot, like the other watcher
    ticks) — a low-stock/dead-stock digest lagging by up to one full interval
    after a fresh deploy is the same staleness window the other watchers
    accept, and there is no reason to special-case this one.
    """
    ttl = cfg.NCE_INVENTORY_STOCK_WATCHER_INTERVAL_MINUTES * 60 + 60
    lock: CronLock | None = await acquire_cron_lock("inventory_stock_watcher", ttl)
    if lock is None:
        log.debug("Skipping inventory_stock_watcher — lock held by another instance")
        return
    try:
        from nce.vertical_modules.inventory.watchers import do_flag_stock_alerts

        class _PoolEngine:
            def __init__(self, p: asyncpg.Pool) -> None:
                self.pg_pool = p

        engine = _PoolEngine(pool)

        async with unmanaged_pg_connection(
            pool, site="cron.inventory_stock_watcher.namespace_scan"
        ) as conn:
            rows = await conn.fetch("SELECT id FROM namespaces")

        for row in rows:
            ns_id: UUID = row["id"]
            try:
                result = await do_flag_stock_alerts(engine, {"namespace_id": ns_id})
                flags = result.get("flags", [])
                log.debug(
                    "inventory_stock_watcher tick namespace=%s scanned=%s flags=%d",
                    ns_id,
                    result.get("scanned"),
                    len(flags),
                )
                if flags and cfg.NCE_INVENTORY_LOW_STOCK_ALERT_ENABLED:
                    shown = flags[:_INVENTORY_STOCK_ALERT_MAX_DETAILS]
                    samples = "; ".join(
                        f"{f.get('flag_type')}:{f.get('sku')}@{f.get('location_id')}" for f in shown
                    )
                    await _dispatch_throttled_alert(
                        f"inventory_stock_watcher.{ns_id}",
                        f"Inventory stock alert: Namespace {ns_id}",
                        f"{len(flags)} stock flag(s) in namespace {ns_id}. "
                        f"First {len(shown)}: {samples}",
                    )
            except _CRON_TICK_ERRORS as exc:
                log.exception("inventory_stock_watcher tick failed for namespace=%s", ns_id)
                await _dispatch_throttled_alert(
                    f"cron.inventory_stock_watcher.{ns_id}",
                    f"Inventory Stock Watcher Failed: Namespace {ns_id}",
                    f"Inventory stock watcher tick failed for namespace {ns_id}: "
                    f"{type(exc).__name__}: {exc}",
                )
    except _CRON_TICK_ERRORS as exc:
        log.exception("inventory_stock_watcher tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.inventory_stock_watcher.global",
            "Cron Job Failed: inventory_stock_watcher",
            f"Inventory stock watcher tick failed unexpectedly: {type(exc).__name__}: {exc}",
        )
    finally:
        await release_cron_lock(lock)


# Agreements coverage flags that warrant an alert.  "review" is deliberately
# excluded — review flags surface in the dashboard/review queue, not as alerts.
_AGREEMENTS_ALERT_FLAG_TYPES: tuple[str, ...] = ("expiry", "leakage")

# Cap how many per-agreement details are embedded in one alert message.
_AGREEMENTS_ALERT_MAX_DETAILS: int = 5


async def _dispatch_agreements_coverage_alerts(
    ns_id: UUID,
    flags: list[dict[str, Any]],
) -> None:
    """Dispatch one throttled alert per (namespace, flag_type) group.

    Grouping keeps alert volume bounded (alert-storm control): a namespace
    with 40 expiring agreements produces ONE expiry alert, not 40.
    """
    for flag_type in _AGREEMENTS_ALERT_FLAG_TYPES:
        group = [f for f in flags if f.get("flag_type") == flag_type]
        if not group:
            continue
        shown = group[:_AGREEMENTS_ALERT_MAX_DETAILS]
        samples = "; ".join(f"{f.get('agreement_id')}: {f.get('detail')}" for f in shown)
        await _dispatch_throttled_alert(
            f"agreements_coverage.{ns_id}.{flag_type}",
            f"Agreements {flag_type} alert: Namespace {ns_id}",
            f"{len(group)} {flag_type} flag(s) in namespace {ns_id}. First {len(shown)}: {samples}",
        )


async def _agreements_coverage_watcher_tick(pool: asyncpg.Pool) -> None:
    """
    APScheduler job: run the Agreements coverage matrix for every namespace
    with ``metadata.agreements.enabled=true`` and dispatch throttled alerts
    for ``expiry`` and ``leakage`` flags (never ``review`` — queue-visible).

    Singleton via CronLock — prevents duplicate runs across replicas.
    Watcher role: observe + alert only; ``do_coverage_matrix`` is return-only
    and writes nothing (it already degrades gracefully when Economy GL is
    unavailable, still computing expiry+review flags).
    """
    ttl = cfg.NCE_AGREEMENTS_COVERAGE_WATCHER_INTERVAL_MINUTES * 60 + 60
    lock: CronLock | None = await acquire_cron_lock("agreements_coverage_watcher", ttl)
    if lock is None:
        log.debug("Skipping agreements_coverage_watcher — lock held by another instance")
        return
    try:
        from nce.vertical_modules.agreements.coverage import do_coverage_matrix

        class _PoolEngine:
            def __init__(self, p: asyncpg.Pool) -> None:
                self.pg_pool = p

        engine = _PoolEngine(pool)

        async with unmanaged_pg_connection(
            pool, site="cron.agreements_coverage_watcher.namespace_scan"
        ) as conn:
            rows = await conn.fetch(
                """
                SELECT id FROM namespaces
                WHERE COALESCE((metadata->'agreements'->>'enabled')::boolean, false) = true
                """
            )

        for row in rows:
            ns_id: UUID = row["id"]
            try:
                result = await do_coverage_matrix(engine, {"namespace_id": ns_id})
                flags = result.get("flags", [])
                log.debug(
                    "agreements_coverage_watcher tick namespace=%s status=%s flags=%d",
                    ns_id,
                    result.get("status"),
                    len(flags),
                )
                await _dispatch_agreements_coverage_alerts(ns_id, flags)
            except _CRON_TICK_ERRORS as exc:
                log.exception("agreements_coverage_watcher tick failed for namespace=%s", ns_id)
                await _dispatch_throttled_alert(
                    f"cron.agreements_coverage_watcher.{ns_id}",
                    f"Agreements Coverage Watcher Failed: Namespace {ns_id}",
                    f"Agreements coverage watcher tick failed for namespace {ns_id}: "
                    f"{type(exc).__name__}: {exc}",
                )
    except _CRON_TICK_ERRORS as exc:
        log.exception("agreements_coverage_watcher tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.agreements_coverage_watcher.global",
            "Cron Job Failed: agreements_coverage_watcher",
            f"Agreements coverage watcher tick failed unexpectedly: {type(exc).__name__}: {exc}",
        )
    finally:
        await release_cron_lock(lock)


# Interval for the recurring-revenue recognition tick (Batch 124, M8.W9). No
# cfg key added — nce/config.py is outside this wave's authorized file set;
# a bare literal is the same convention _saga_recovery_tick already uses
# (IntervalTrigger(minutes=5) with no named cfg entry). Recognition itself is
# idempotent (action_idempotency), so re-running on every tick is a safe,
# cheap no-op for a namespace with nothing new to recognise this period.
_ECONOMY_RECURRING_INTERVAL_MINUTES = 24 * 60  # once daily


async def _economy_recurring_recognition_tick(pool: asyncpg.Pool) -> None:
    """
    APScheduler job: run ratable 1/12 revenue recognition for every namespace
    with ``metadata.economy.enabled=true`` (docs/vertical_engines/08-economy-
    engine.md "Config keys": "Namespaces opt in via metadata.economy.enabled
    = true").

    Singleton via CronLock — prevents duplicate runs across replicas.
    Idempotent by construction (``do_recognize_recurring``, keyed on
    finagoRef via ``action_idempotency``'s PRIMARY KEY) — a namespace with
    nothing new to recognise this period is a safe, cheap no-op tick.

    Contract source (Wave 10, M8.W10 — shim retired): contracts are read
    from the real ``economy_contracts`` table via
    ``contracts.fetch_contracts_for_recognition``, namespace-scoped through
    ``scoped_pg_session`` (RLS-enforced, defense in depth alongside the
    explicit ``WHERE namespace_id = ...`` filter it issues). Wave 9 sourced
    this list from ``namespaces.metadata->'economy'->'recurring_contracts'``
    as a temporary, no-migration substitute — see
    ``nce/vertical_modules/economy/recurring.py``'s module docstring for why
    that shim is gone and ``do_recognize_recurring`` itself needed no change
    (it was always contract-source-agnostic).
    """
    ttl = _ECONOMY_RECURRING_INTERVAL_MINUTES * 60 + 60
    lock: CronLock | None = await acquire_cron_lock("economy_recurring_recognition", ttl)
    if lock is None:
        log.debug("Skipping economy_recurring_recognition — lock held by another instance")
        return
    try:
        from datetime import datetime, timezone

        from nce.vertical_modules.economy.contracts import fetch_contracts_for_recognition
        from nce.vertical_modules.economy.recurring import do_recognize_recurring

        class _PoolEngine:
            def __init__(self, p: asyncpg.Pool) -> None:
                self.pg_pool = p

        engine = _PoolEngine(pool)
        period = datetime.now(timezone.utc).strftime("%Y-%m")

        async with unmanaged_pg_connection(
            pool, site="cron.economy_recurring_recognition.namespace_scan"
        ) as conn:
            rows = await conn.fetch(
                """
                SELECT id FROM namespaces
                WHERE COALESCE((metadata->'economy'->>'enabled')::boolean, false) = true
                """
            )

        for row in rows:
            ns_id: UUID = row["id"]
            try:
                # Fetch is inside this per-namespace try (not the fetch above
                # the loop) — a DB error reading ONE namespace's contracts
                # must not abort the scan for every other namespace.
                contracts = await fetch_contracts_for_recognition(engine, ns_id)  # type: ignore[arg-type]
                if not contracts:
                    log.debug(
                        "economy_recurring_recognition: namespace=%s has no contracts configured",
                        ns_id,
                    )
                    continue
                result = await do_recognize_recurring(
                    engine,  # type: ignore[arg-type]  # _PoolEngine duck-types NCEEngine's pg_pool
                    {"namespace_id": ns_id, "period": period, "contracts": contracts},
                )
                log.info(
                    "economy_recurring_recognition tick namespace=%s period=%s recognized=%d "
                    "already_recognized=%d not_due=%d",
                    ns_id,
                    period,
                    len(result["recognized"]),
                    len(result["already_recognized"]),
                    len(result["not_due"]),
                )
            except _CRON_TICK_ERRORS as exc:
                log.exception("economy_recurring_recognition tick failed for namespace=%s", ns_id)
                await _dispatch_throttled_alert(
                    f"cron.economy_recurring_recognition.{ns_id}",
                    f"Economy Recurring Recognition Failed: Namespace {ns_id}",
                    f"Economy recurring recognition tick failed for namespace {ns_id}: "
                    f"{type(exc).__name__}: {exc}",
                )
    except _CRON_TICK_ERRORS as exc:
        log.exception("economy_recurring_recognition tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.economy_recurring_recognition.global",
            "Cron Job Failed: economy_recurring_recognition",
            f"Economy recurring recognition tick failed unexpectedly: {type(exc).__name__}: {exc}",
        )
    finally:
        await release_cron_lock(lock)


# Interval for the contract-renewal watcher tick (Batch 125, M8.W10). Same
# bare-literal convention as _ECONOMY_RECURRING_INTERVAL_MINUTES (no cfg key
# added — nce/config.py is outside this wave's authorized file set).
# do_scan_renewals is read-only, so re-running daily is a safe, cheap no-op
# for a namespace with nothing newly due.
_ECONOMY_RENEWAL_WATCHER_INTERVAL_MINUTES = 24 * 60  # once daily

# Cap how many per-contract details are embedded in one alert message —
# mirrors _AGREEMENTS_ALERT_MAX_DETAILS' alert-storm control.
_ECONOMY_RENEWAL_ALERT_MAX_DETAILS: int = 5


async def _economy_contract_renewal_watcher_tick(pool: asyncpg.Pool) -> None:
    """
    APScheduler job: run the 90-day contract-renewal scan
    (``contracts.do_scan_renewals``) for every namespace with
    ``metadata.economy.enabled=true`` and dispatch one throttled alert per
    namespace listing the contracts due for renewal.

    Singleton via CronLock — prevents duplicate runs across replicas.
    Watcher role: observe + alert only (mirrors
    ``_agreements_coverage_watcher_tick`` / ``_product_eol_watcher_tick``) —
    ``do_scan_renewals`` is return-only and writes nothing.
    """
    ttl = _ECONOMY_RENEWAL_WATCHER_INTERVAL_MINUTES * 60 + 60
    lock: CronLock | None = await acquire_cron_lock("economy_contract_renewal_watcher", ttl)
    if lock is None:
        log.debug("Skipping economy_contract_renewal_watcher — lock held by another instance")
        return
    try:
        from nce.vertical_modules.economy.contracts import do_scan_renewals

        class _PoolEngine:
            def __init__(self, p: asyncpg.Pool) -> None:
                self.pg_pool = p

        engine = _PoolEngine(pool)

        async with unmanaged_pg_connection(
            pool, site="cron.economy_contract_renewal_watcher.namespace_scan"
        ) as conn:
            rows = await conn.fetch(
                """
                SELECT id FROM namespaces
                WHERE COALESCE((metadata->'economy'->>'enabled')::boolean, false) = true
                """
            )

        for row in rows:
            ns_id: UUID = row["id"]
            try:
                result = await do_scan_renewals(engine, {"namespace_id": ns_id})  # type: ignore[arg-type]
                due = result["due"]
                log.debug(
                    "economy_contract_renewal_watcher tick namespace=%s due=%d",
                    ns_id,
                    len(due),
                )
                if due:
                    shown = due[:_ECONOMY_RENEWAL_ALERT_MAX_DETAILS]
                    samples = "; ".join(
                        f"{d['contract_id']}: renews {d['next_renewal_date']} "
                        f"({d['days_until_renewal']}d)"
                        for d in shown
                    )
                    await _dispatch_throttled_alert(
                        f"economy_contract_renewal.{ns_id}",
                        f"Contract renewals due: Namespace {ns_id}",
                        f"{len(due)} contract(s) due for renewal within "
                        f"{result['window_days']} days in namespace {ns_id}. "
                        f"First {len(shown)}: {samples}",
                    )
            except _CRON_TICK_ERRORS as exc:
                log.exception(
                    "economy_contract_renewal_watcher tick failed for namespace=%s", ns_id
                )
                await _dispatch_throttled_alert(
                    f"cron.economy_contract_renewal_watcher.{ns_id}",
                    f"Economy Contract Renewal Watcher Failed: Namespace {ns_id}",
                    f"Economy contract renewal watcher tick failed for namespace {ns_id}: "
                    f"{type(exc).__name__}: {exc}",
                )
    except _CRON_TICK_ERRORS as exc:
        log.exception("economy_contract_renewal_watcher tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.economy_contract_renewal_watcher.global",
            "Cron Job Failed: economy_contract_renewal_watcher",
            f"Economy contract renewal watcher tick failed unexpectedly: "
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        await release_cron_lock(lock)


async def _actor_trust_tick(pool: asyncpg.Pool) -> None:
    """
    Hourly tick: recompute Laplace-smoothed trust scores in ``actor_trust``.

    For each namespace, aggregates ``quarantine_confirmed`` / ``quarantine_rejected``
    WORM events (emitted by Batch 112) plus ``contradictions_sourced`` counts and
    writes::

        trust = clamp(0.1, 0.95,
                      (confirms+1) / (confirms+rejections+2)
                      − 0.05·log1p(contradictions_sourced))

    Runs under a ``CronLock`` so only one replica executes per tick window.
    Writes are scoped per-namespace via ``scoped_pg_session`` (RLS enforced).
    Does NOT mutate ``event_log`` (WORM invariant preserved).
    """
    ttl = 3600 + 60  # hourly tick + 1 min grace
    lock: CronLock | None = await acquire_cron_lock("actor_trust_scores", ttl)
    if lock is None:
        log.debug("Skipping actor_trust_scores — lock held by another instance")
        return
    try:
        import math

        # Scan all namespaces — actor_trust is RLS-scoped.
        async with unmanaged_pg_connection(pool, site="cron.actor_trust.namespace_scan") as conn:
            ns_rows = await conn.fetch("SELECT id FROM namespaces")

        updated_total = 0
        for ns_row in ns_rows:
            ns_id: UUID = ns_row["id"]
            try:
                async with scoped_pg_session(pool, ns_id) as conn:
                    # Aggregate confirm/reject WORM events per sourcing agent.
                    event_rows = await conn.fetch(
                        """
                        SELECT
                            (params->>'agent_id')  AS actor_id,
                            COUNT(*) FILTER (WHERE event_type = 'quarantine_confirmed') AS confirms,
                            COUNT(*) FILTER (WHERE event_type = 'quarantine_rejected')  AS rejections
                        FROM event_log
                        WHERE namespace_id = $1::uuid
                          AND event_type IN ('quarantine_confirmed', 'quarantine_rejected')
                          AND (params->>'agent_id') IS NOT NULL
                          AND (params->>'agent_id') != ''
                        GROUP BY params->>'agent_id'
                        """,
                        ns_id,
                    )

                    if not event_rows:
                        continue

                    # Gather contradiction counts from actor_trust (already stored there
                    # by contradiction detection; we only update the trust column here).
                    for row in event_rows:
                        actor_id: str = row["actor_id"]
                        confirms: int = int(row["confirms"])
                        rejections: int = int(row["rejections"])

                        # Fetch contradictions_sourced from existing row (0 when absent).
                        existing = await conn.fetchrow(
                            """
                            SELECT contradictions_sourced
                            FROM actor_trust
                            WHERE namespace_id = $1::uuid
                              AND actor_id = $2
                              AND actor_kind = 'agent'
                            """,
                            ns_id,
                            actor_id,
                        )
                        contradictions_sourced: int = (
                            int(existing["contradictions_sourced"]) if existing else 0
                        )

                        # Laplace-smoothed trust with contradiction penalty.
                        raw_trust = (confirms + 1) / (
                            confirms + rejections + 2
                        ) - 0.05 * math.log1p(contradictions_sourced)
                        trust = max(0.1, min(0.95, raw_trust))

                        await conn.execute(
                            """
                            INSERT INTO actor_trust
                                (namespace_id, actor_id, actor_kind,
                                 confirmations, rejections, contradictions_sourced,
                                 trust, updated_at)
                            VALUES ($1::uuid, $2, 'agent', $3, $4, $5, $6::numeric, NOW())
                            ON CONFLICT (namespace_id, actor_id, actor_kind) DO UPDATE
                                SET confirmations          = EXCLUDED.confirmations,
                                    rejections             = EXCLUDED.rejections,
                                    trust                  = EXCLUDED.trust,
                                    updated_at             = NOW()
                            """,
                            ns_id,
                            actor_id,
                            confirms,
                            rejections,
                            contradictions_sourced,
                            trust,
                        )
                        updated_total += 1

                log.info(
                    "actor_trust_tick: updated %d actor rows for namespace %s",
                    updated_total,
                    ns_id,
                )
            except _CRON_TICK_ERRORS as exc:
                log.exception("actor_trust_tick failed for namespace %s", ns_id)
                await _dispatch_throttled_alert(
                    f"cron.actor_trust_scores.{ns_id}",
                    f"Actor Trust Tick Failed: Namespace {ns_id}",
                    f"Actor trust score recompute failed for namespace {ns_id}: "
                    f"{type(exc).__name__}: {exc}",
                )

        log.info("actor_trust_tick complete: %d total actor rows updated", updated_total)

    except _CRON_TICK_ERRORS as exc:
        log.exception("actor_trust_tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.actor_trust_scores.global",
            "Cron Job Failed: actor_trust_scores",
            f"Actor trust score cron tick failed unexpectedly: {type(exc).__name__}: {exc}",
        )
    finally:
        await release_cron_lock(lock)


async def _chain_verification_tick(pool: asyncpg.Pool) -> None:
    """Run Merkle chain verification for all namespaces.

    Sets the MERKLE_CHAIN_VALID gauge (1=valid, 0=corrupted).
    On verification failure, logs critical, dispatches an alert,
    and appends a 'chain_verification_failed' audit event.
    """
    ttl = cfg.NCE_CHAIN_VERIFY_INTERVAL_MINUTES * 60 + 60
    lock: CronLock | None = await acquire_cron_lock("chain_verification", ttl)
    if lock is None:
        log.debug("Skipping chain_verification — lock held by another instance")
        return
    try:
        from nce.event_log import append_event, verify_merkle_chain
        from nce.notifications import dispatcher
        from nce.observability import MERKLE_CHAIN_VALID

        async with unmanaged_pg_connection(pool, site="cron.chain_verify.namespace_scan") as conn:
            rows = await conn.fetch("SELECT id FROM namespaces")

        all_valid = True
        for row in rows:
            ns_id: UUID = row["id"]
            try:
                async with scoped_pg_session(pool, ns_id) as conn:
                    depth = cfg.NCE_CHAIN_VERIFY_STARTUP_DEPTH
                    if depth > 0:
                        max_seq = await conn.fetchval(
                            "SELECT COALESCE(max(event_seq), 0) FROM event_log WHERE namespace_id = $1",
                            ns_id,
                        )
                        start_seq = max(1, max_seq - depth + 1)
                    else:
                        start_seq = 1

                    res = await verify_merkle_chain(conn, namespace_id=ns_id, start_seq=start_seq)
                    if not res.get("valid", True):
                        all_valid = False
                        first_break = res.get("first_break")
                        reason = res.get("reason") or "Merkle chain signature or hash mismatch"

                        log.critical(
                            "[CHAIN-VERIFICATION] Merkle chain corrupted for namespace=%s. "
                            "First break at event_seq=%s. Reason=%s",
                            ns_id,
                            first_break,
                            reason,
                        )

                        title = f"Merkle Chain Corrupted: Namespace {ns_id}"
                        message = (
                            f"Critical data integrity failure: Merkle chain verification failed "
                            f"for namespace {ns_id}. First break at event_seq {first_break}. "
                            f"Reason: {reason}"
                        )
                        await dispatcher.dispatch_alert(title, message)

                        await append_event(
                            conn=conn,
                            namespace_id=ns_id,
                            agent_id="cron.chain_verify",
                            event_type="chain_verification_failed",
                            params={
                                "first_break": first_break,
                                "reason": reason,
                            },
                        )
            except _CRON_TICK_ERRORS as exc:
                log.exception("Error running Merkle chain verification for namespace %s", ns_id)
                all_valid = False
                await _dispatch_throttled_alert(
                    f"cron.chain_verification.{ns_id}",
                    f"Chain Verification Failed: Namespace {ns_id}",
                    f"Merkle chain verification job failed for namespace {ns_id}: {type(exc).__name__}: {exc}",
                )

        if all_valid:
            MERKLE_CHAIN_VALID.set(1)
        else:
            MERKLE_CHAIN_VALID.set(0)

    except _CRON_TICK_ERRORS as exc:
        log.exception("chain verification tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.chain_verification.global",
            "Cron Job Failed: chain_verification",
            f"Merkle chain verification cron tick failed unexpectedly: {type(exc).__name__}: {exc}",
        )
    finally:
        await release_cron_lock(lock)


async def _retention_tick(pool: asyncpg.Pool) -> None:
    """Daily retention sweep: archive+drop aged event_log partitions, purge resolved
    contradictions, and reap low-confidence non-sync kg_edges.

    Singleton via its own ``CronLock`` so only one replica fires per window.
    Requires MinIO to be configured (``cfg.MINIO_ENDPOINT``); skips silently if absent.
    """
    ttl = cfg.NCE_RETENTION_INTERVAL_MINUTES * 60 + 120
    lock: CronLock | None = await acquire_cron_lock("event_retention", ttl)
    if lock is None:
        log.debug("Skipping event_retention — lock held by another instance")
        return

    try:
        if not cfg.MINIO_ENDPOINT:
            log.warning("event_retention tick skipped: MINIO_ENDPOINT is not configured")
            return

        from minio import Minio  # type: ignore[import-untyped]

        from nce.garbage_collector import run_retention_pass

        minio_client = Minio(
            cfg.MINIO_ENDPOINT,
            access_key=cfg.MINIO_ACCESS_KEY,
            secret_key=cfg.MINIO_SECRET_KEY,
            secure=cfg.MINIO_SECURE,
        )
        stats = await run_retention_pass(pool, minio_client)
        log.info("event_retention tick complete: %s", stats)

        # B067h2 — M6.W17b retired-archive sweep.  OFF by default behind
        # NCE_SYSTEM_DESIGN_ARCHIVE_SWEEP_ENABLED; the call is unconditional
        # here and the flag is checked inside, so the default lives in exactly
        # one place (archive.sweep_enabled) rather than being restated at the
        # schedule.  It shares this tick because it is the same kind of work on
        # the same cadence and needs the same MinIO client and cron lock.
        from nce.vertical_modules.system_design.archive import run_design_archive_sweep

        design_stats = await run_design_archive_sweep(pool, minio_client)
        log.info("system_design archive sweep complete: %s", design_stats)

    except _CRON_TICK_ERRORS as exc:
        log.exception("event_retention tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.event_retention",
            "Cron Job Failed: event_retention",
            f"Event retention tick failed: {type(exc).__name__}: {exc}",
        )
    finally:
        await release_cron_lock(lock)


async def _anchor_tick(pool: asyncpg.Pool) -> None:
    """Write per-namespace Merkle chain heads to the WORM anchor bucket.

    Runs hourly (configurable via ``NCE_ANCHOR_INTERVAL_MINUTES``).  For each
    active namespace the tick:

    1. Reads the current chain head (max event_seq + chain_hash) via the
       *unmanaged* PG connection (no RLS — this is a read-only audit probe).
    2. Serialises it as JSON and writes it to an object-locked (WORM) MinIO
       bucket under ``<namespace_id>/<max_seq>.json``.

    The bucket must be created with versioning + object-lock enabled
    (``make_bucket(..., object_lock=True)``).  This ensures anchors survive
    even if a DB superuser deletes all rows and re-stitches the chain — the
    independently stored blob reveals the divergence.

    Singleton via its own ``CronLock`` so only one replica fires per window.
    """
    ttl = cfg.NCE_ANCHOR_INTERVAL_MINUTES * 60 + 60
    lock: CronLock | None = await acquire_cron_lock("tamper_anchor", ttl)
    if lock is None:
        log.debug("Skipping tamper_anchor — lock held by another instance")
        return
    try:
        import io
        from datetime import timedelta

        from minio import Minio
        from minio.commonconfig import COMPLIANCE
        from minio.error import S3Error
        from minio.retention import Retention

        minio_client = Minio(
            cfg.MINIO_ENDPOINT,
            access_key=cfg.MINIO_ACCESS_KEY,
            secret_key=cfg.MINIO_SECRET_KEY,
            secure=cfg.MINIO_SECURE,
        )
        bucket = cfg.NCE_ANCHOR_BUCKET

        # Ensure the anchor bucket exists with object-lock (WORM) enabled.
        # ``make_bucket`` with ``object_lock=True`` enables versioning + object-lock
        # atomically; subsequent calls are skipped if the bucket already exists.
        try:
            if not await asyncio.to_thread(minio_client.bucket_exists, bucket):

                def _make_bucket() -> None:
                    minio_client.make_bucket(bucket, object_lock=True)

                await asyncio.to_thread(_make_bucket)
                log.info("Created WORM anchor bucket %r", bucket)
        except S3Error as exc:
            msg = str(exc).lower()
            if "already" not in msg and "exist" not in msg:
                raise

        async with unmanaged_pg_connection(pool, site="cron.anchor.namespace_scan") as conn:
            rows = await conn.fetch("SELECT id FROM namespaces")

        for row in rows:
            ns_id: UUID = row["id"]
            try:
                async with unmanaged_pg_connection(pool, site="cron.anchor.head_read") as conn:
                    # NOTE: SET LOCAL has no effect on an unmanaged (autocommit) connection
                    # because there is no enclosing transaction — the session variable is
                    # reset immediately.  Namespace scoping is enforced by the explicit
                    # WHERE namespace_id = $1 predicate below, not by an RLS policy here.
                    head = await conn.fetchrow(
                        """
                        SELECT event_seq, chain_hash
                        FROM   event_log
                        WHERE  namespace_id = $1
                        ORDER BY event_seq DESC
                        LIMIT 1
                        """,
                        ns_id,
                    )
                if head is None:
                    log.debug("anchor_tick: namespace %s has no events — skipping", ns_id)
                    continue

                max_seq: int = int(head["event_seq"])
                chain_hash_bytes: bytes = head["chain_hash"]
                if isinstance(chain_hash_bytes, memoryview):
                    chain_hash_bytes = bytes(chain_hash_bytes)

                from datetime import datetime, timezone

                anchored_at = datetime.now(timezone.utc).isoformat()
                blob = json.dumps(
                    {
                        "namespace_id": str(ns_id),
                        "max_seq": max_seq,
                        "chain_hash": chain_hash_bytes.hex(),
                        "anchored_at": anchored_at,
                    },
                    sort_keys=True,
                ).encode("utf-8")

                object_name = f"{ns_id}/{max_seq}.json"

                def _put_blob(b: bytes = blob, o: str = object_name) -> None:
                    # COMPLIANCE (not GOVERNANCE) retention: even MinIO admins
                    # cannot delete or overwrite the object until the lock expires,
                    # making the anchor a true independent root of trust.
                    retain_until = datetime.now(timezone.utc) + timedelta(
                        days=cfg.NCE_ANCHOR_RETENTION_DAYS
                    )
                    minio_client.put_object(
                        bucket,
                        o,
                        io.BytesIO(b),
                        len(b),
                        content_type="application/json",
                        retention=Retention(COMPLIANCE, retain_until),
                    )

                await asyncio.to_thread(_put_blob)
                log.info(
                    "anchor_tick: anchored namespace=%s max_seq=%d chain_hash=%s",
                    ns_id,
                    max_seq,
                    chain_hash_bytes.hex()[:16] + "…",
                )

            except (*_CRON_TICK_ERRORS, S3Error) as exc:  # type: ignore[misc]
                log.exception("anchor_tick failed for namespace %s", ns_id)
                await _dispatch_throttled_alert(
                    f"cron.tamper_anchor.{ns_id}",
                    f"Tamper Anchor Tick Failed: Namespace {ns_id}",
                    f"Anchor tick failed for namespace {ns_id}: {type(exc).__name__}: {exc}",
                )

    except _CRON_TICK_ERRORS as exc:
        log.exception("tamper_anchor tick failed unexpectedly")
        await _dispatch_throttled_alert(
            "cron.tamper_anchor.global",
            "Cron Job Failed: tamper_anchor",
            f"Tamper anchor cron tick failed unexpectedly: {type(exc).__name__}: {exc}",
        )
    finally:
        await release_cron_lock(lock)


scheduler: AsyncIOScheduler | None = None


async def reschedule_jobs() -> str:
    """Reschedule active jobs with the latest configuration values from SettingsStore."""
    global scheduler
    if not scheduler or not scheduler.running:
        return "scheduler not active"

    from nce.settings_store import get as store_get

    rescheduled = []

    # 1. bridge_subscription_renewal
    try:
        renewal_min = await store_get(
            "BRIDGE_CRON_INTERVAL_MINUTES", cfg.BRIDGE_CRON_INTERVAL_MINUTES
        )
        scheduler.reschedule_job(
            "bridge_subscription_renewal", trigger=IntervalTrigger(minutes=max(1, int(renewal_min)))
        )
        rescheduled.append("bridge_subscription_renewal")
    except Exception:
        pass

    # 2. phase_2_1_reembedding
    try:
        reembed_min = await store_get(
            "REEMBED_CRON_INTERVAL_MINUTES", cfg.REEMBED_CRON_INTERVAL_MINUTES
        )
        scheduler.reschedule_job(
            "phase_2_1_reembedding", trigger=IntervalTrigger(minutes=max(1, int(reembed_min)))
        )
        rescheduled.append("phase_2_1_reembedding")
    except Exception:
        pass

    # 3. sleep_consolidation
    try:
        consolidation_min = await store_get(
            "CONSOLIDATION_CRON_INTERVAL_MINUTES", cfg.CONSOLIDATION_CRON_INTERVAL_MINUTES
        )
        scheduler.reschedule_job(
            "sleep_consolidation", trigger=IntervalTrigger(minutes=max(1, int(consolidation_min)))
        )
        rescheduled.append("sleep_consolidation")
    except Exception:
        pass

    # 4. outbox_relay
    try:
        outbox_sec = await store_get(
            "OUTBOX_RELAY_INTERVAL_SECONDS", cfg.OUTBOX_RELAY_INTERVAL_SECONDS
        )
        scheduler.reschedule_job(
            "outbox_relay", trigger=IntervalTrigger(seconds=max(1, int(outbox_sec)))
        )
        rescheduled.append("outbox_relay")
    except Exception:
        pass

    # 5. d365_entity_sync
    if cfg.NCE_D365_ENABLED:
        try:
            d365_min = await store_get(
                "NCE_D365_SYNC_INTERVAL_MINUTES", cfg.NCE_D365_SYNC_INTERVAL_MINUTES
            )
            scheduler.reschedule_job(
                "d365_entity_sync", trigger=IntervalTrigger(minutes=max(5, int(d365_min)))
            )
            rescheduled.append("d365_entity_sync")
        except Exception:
            pass

    # 6. d365_netbox_bridge
    if cfg.NCE_D365_NETBOX_BRIDGE_ENABLED:
        try:
            netbox_min = await store_get(
                "NCE_D365_NETBOX_BRIDGE_INTERVAL_MINUTES",
                cfg.NCE_D365_NETBOX_BRIDGE_INTERVAL_MINUTES,
            )
            scheduler.reschedule_job(
                "d365_netbox_bridge", trigger=IntervalTrigger(minutes=max(10, int(netbox_min)))
            )
            rescheduled.append("d365_netbox_bridge")
        except Exception:
            pass

    # 7. chain_verification
    try:
        verify_min = await store_get(
            "NCE_CHAIN_VERIFY_INTERVAL_MINUTES", cfg.NCE_CHAIN_VERIFY_INTERVAL_MINUTES
        )
        scheduler.reschedule_job(
            "chain_verification", trigger=IntervalTrigger(minutes=max(5, int(verify_min)))
        )
        rescheduled.append("chain_verification")
    except Exception:
        pass

    # 8. tamper_anchor
    try:
        anchor_min = await store_get("NCE_ANCHOR_INTERVAL_MINUTES", cfg.NCE_ANCHOR_INTERVAL_MINUTES)
        scheduler.reschedule_job(
            "tamper_anchor", trigger=IntervalTrigger(minutes=max(1, int(anchor_min)))
        )
        rescheduled.append("tamper_anchor")
    except Exception:
        pass

    return f"rescheduled {len(rescheduled)} jobs"


async def async_main() -> None:
    global scheduler

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [nce.cron] %(levelname)s %(message)s",
    )
    cfg.validate()

    # Startup jitter — randomized one-time offset to spread database CPU
    # load when multiple NCE instances boot simultaneously.  The jitter
    # is applied before the connection pool is created, so it does not hold
    # any database resources while waiting.
    jitter = random.uniform(0.0, cfg.CRON_STARTUP_JITTER_MAX_SECONDS)
    if jitter > 0.0:
        log.info(
            "Applying %.1fs startup jitter to avoid thundering herd "
            "(CRON_STARTUP_JITTER_MAX_SECONDS=%.0f)",
            jitter,
            cfg.CRON_STARTUP_JITTER_MAX_SECONDS,
        )
        await asyncio.sleep(jitter)

    pool = await asyncpg.create_pool(
        cfg.PG_DSN,
        min_size=1,
        max_size=4,  # +1 for the re-embedding worker
        command_timeout=120,
    )

    # Optional Mongo client for re-embedding text resolution.
    mongo_client: Any = None
    try:
        from motor.motor_asyncio import AsyncIOMotorClient

        mongo_client = AsyncIOMotorClient(cfg.MONGO_URI, serverSelectionTimeoutMS=5_000)
    except ImportError:
        log.warning("motor not available — re-embedding will use fallback text only.")

    renewal_minutes = max(1, int(cfg.BRIDGE_CRON_INTERVAL_MINUTES))
    reembed_minutes = max(1, int(_REEMBED_INTERVAL))
    consolidation_minutes = max(1, int(cfg.CONSOLIDATION_CRON_INTERVAL_MINUTES))

    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        _renewal_tick,
        IntervalTrigger(minutes=renewal_minutes),
        args=[pool],
        id="bridge_subscription_renewal",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )

    scheduler.add_job(
        _reembedding_tick,
        IntervalTrigger(minutes=reembed_minutes),
        args=[pool, mongo_client],
        id="phase_2_1_reembedding",
        coalesce=True,
        max_instances=1,  # never overlap runs
        replace_existing=True,
    )

    scheduler.add_job(
        _consolidation_tick,
        IntervalTrigger(minutes=consolidation_minutes),
        args=[pool, mongo_client],
        id="sleep_consolidation",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )

    scheduler.add_job(
        _partition_maintenance_tick,
        CronTrigger(day=1, hour=0, minute=0),  # first of every month at 00:00 UTC
        args=[pool],
        id="event_log_partition_maintenance",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )

    scheduler.add_job(
        _saga_recovery_tick,
        IntervalTrigger(minutes=5),
        args=[pool],
        id="saga_recovery",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )

    # Register outbox subscribers BEFORE the relay job is scheduled. cron is the
    # second process that runs the relay (the first is nce/mcp_stdio_main.py), and
    # OUTBOX_HANDLERS is per-process state -- registering in only one of them
    # leaves the other dead-lettering every System Design authoring event it polls.
    from nce.vertical_modules.project import automation as project_automation
    from nce.vertical_modules.project import tasks as project_tasks
    from nce.vertical_modules.system_design.subscribers import (
        register_system_design_subscribers,
    )

    register_system_design_subscribers()

    # Module 7's three C4 selectors (M0.W20d) -- PO_LINE.status_changed,
    # GOODS_RECEIPT.created and BOM_LINE.status_changed. Their handlers were
    # registered by NO process, so each would fast-fail to the DLQ the moment a
    # producer exists. Registered in cron too, for the same per-process reason
    # as the block above.
    #
    # tasks._handle_bom_line_status_changed reads an engine registry at delivery
    # time and raises EngineNotRegisteredError without one. cron has no
    # NCEEngine, only the pool -- and the handler path touches exactly
    # ``engine.pg_pool`` (verified), which is the same duck-type the cron ticks
    # already build for their own core calls. Module-qualified because
    # automation and tasks each define a DIFFERENT register_engine.
    _relay_engine = SimpleNamespace(pg_pool=pool)
    project_tasks.register_engine(_relay_engine)
    project_automation.register_engine(_relay_engine)
    project_tasks.register_bom_task_subscriber()
    project_automation.register_automation_subscribers()

    outbox_seconds = max(1, int(cfg.OUTBOX_RELAY_INTERVAL_SECONDS))
    scheduler.add_job(
        _outbox_relay_tick,
        IntervalTrigger(seconds=outbox_seconds),
        args=[pool],
        id="outbox_relay",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )

    if cfg.NCE_D365_ENABLED:
        d365_minutes = max(5, int(cfg.NCE_D365_SYNC_INTERVAL_MINUTES))
        scheduler.add_job(
            _d365_sync_tick,
            IntervalTrigger(minutes=d365_minutes),
            args=[pool],
            id="d365_entity_sync",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        # Weekly full-refresh: every Sunday at 02:00 UTC.
        # Runs regardless of NCE_D365_INCREMENTAL_ENABLED so deletes are always
        # reconciled at least once a week.
        scheduler.add_job(
            _d365_weekly_full_sync_tick,
            CronTrigger(day_of_week="sun", hour=2, minute=0),
            args=[pool],
            id="d365_weekly_full_sync",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )

    if cfg.NCE_D365_NETBOX_BRIDGE_ENABLED:
        bridge_minutes = max(10, int(cfg.NCE_D365_NETBOX_BRIDGE_INTERVAL_MINUTES))
        scheduler.add_job(
            _d365_netbox_bridge_tick,
            IntervalTrigger(minutes=bridge_minutes),
            args=[pool],
            id="d365_netbox_bridge",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )

    scheduler.add_job(
        _actor_trust_tick,
        IntervalTrigger(hours=1),
        args=[pool],
        id="actor_trust_scores",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )

    register_decay_jobs(scheduler, pool)

    eol_watcher_minutes = max(5, int(cfg.NCE_PRODUCT_EOL_WATCHER_INTERVAL_MINUTES))
    scheduler.add_job(
        _product_eol_watcher_tick,
        IntervalTrigger(minutes=eol_watcher_minutes),
        args=[pool],
        id="product_eol_watcher",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )

    inventory_stock_watcher_minutes = max(5, int(cfg.NCE_INVENTORY_STOCK_WATCHER_INTERVAL_MINUTES))
    scheduler.add_job(
        _inventory_stock_watcher_tick,
        IntervalTrigger(minutes=inventory_stock_watcher_minutes),
        args=[pool],
        id="inventory_stock_watcher",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )

    agreements_watcher_minutes = max(5, int(cfg.NCE_AGREEMENTS_COVERAGE_WATCHER_INTERVAL_MINUTES))
    scheduler.add_job(
        _agreements_coverage_watcher_tick,
        IntervalTrigger(minutes=agreements_watcher_minutes),
        args=[pool],
        id="agreements_coverage_watcher",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )

    economy_recurring_minutes = max(60, int(_ECONOMY_RECURRING_INTERVAL_MINUTES))
    scheduler.add_job(
        _economy_recurring_recognition_tick,
        IntervalTrigger(minutes=economy_recurring_minutes),
        args=[pool],
        id="economy_recurring_recognition",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )

    economy_renewal_watcher_minutes = max(60, int(_ECONOMY_RENEWAL_WATCHER_INTERVAL_MINUTES))
    scheduler.add_job(
        _economy_contract_renewal_watcher_tick,
        IntervalTrigger(minutes=economy_renewal_watcher_minutes),
        args=[pool],
        id="economy_contract_renewal_watcher",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )

    verify_minutes = max(5, int(cfg.NCE_CHAIN_VERIFY_INTERVAL_MINUTES))
    scheduler.add_job(
        _chain_verification_tick,
        IntervalTrigger(minutes=verify_minutes),
        args=[pool],
        id="chain_verification",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )

    anchor_minutes = max(1, int(cfg.NCE_ANCHOR_INTERVAL_MINUTES))
    scheduler.add_job(
        _anchor_tick,
        IntervalTrigger(minutes=anchor_minutes),
        args=[pool],
        id="tamper_anchor",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )

    retention_minutes = max(1, int(cfg.NCE_RETENTION_INTERVAL_MINUTES))
    scheduler.add_job(
        _retention_tick,
        IntervalTrigger(minutes=retention_minutes),
        args=[pool],
        id="event_retention",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )

    scheduler.start()
    log.info(
        "Started bridge renewal scheduler: interval=%s min, lookahead=%s h",
        renewal_minutes,
        cfg.BRIDGE_RENEWAL_LOOKAHEAD_HOURS,
    )
    log.info(
        "Started re-embedding scheduler: interval=%s min, model=%s",
        reembed_minutes,
        cfg.NCE_LLM_PROVIDER,
    )
    log.info(
        "Started consolidation scheduler: interval=%s min (namespaces with consolidation.enabled)",
        consolidation_minutes,
    )
    log.info("Started outbox relay scheduler: interval=%s s", outbox_seconds)

    # Fire maintenance jobs immediately on startup so the first interval is not wasted.
    # Run concurrently — sequential awaits would delay the event loop by the sum of all
    # tick durations; _reembedding_tick in particular can take minutes.  Each tick already
    # catches and logs its own errors, so we gather with return_exceptions=True as a
    # belt-and-suspenders guard.
    startup_coros = [
        _renewal_tick(pool),
        _reembedding_tick(pool, mongo_client),
        _consolidation_tick(pool, mongo_client),
        _partition_maintenance_tick(pool),
        _saga_recovery_tick(pool),
        _outbox_relay_tick(pool),
        _decay_prune_tick(pool),
        _product_eol_watcher_tick(pool),
        _inventory_stock_watcher_tick(pool),
        _agreements_coverage_watcher_tick(pool),
        _economy_recurring_recognition_tick(pool),
        _economy_contract_renewal_watcher_tick(pool),
        _chain_verification_tick(pool),
        _actor_trust_tick(pool),
        _anchor_tick(pool),
        _retention_tick(pool),
    ]
    if cfg.NCE_D365_ENABLED:
        startup_coros.append(_d365_sync_tick(pool))
    if cfg.NCE_D365_NETBOX_BRIDGE_ENABLED:
        startup_coros.append(_d365_netbox_bridge_tick(pool))

    startup_results = await asyncio.gather(*startup_coros, return_exceptions=True)
    for _result in startup_results:
        if isinstance(_result, BaseException):
            log.error("Startup tick raised uncaught exception: %s", _result)

    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.to_thread(scheduler.shutdown, wait=True)
        await pool.close()
        if mongo_client:
            mongo_client.close()
        log.info("Cron shutdown complete.")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
