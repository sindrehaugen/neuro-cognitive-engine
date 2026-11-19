"""Deterministic, zero-``docker.sock`` maintenance caddy probes for Day-2 operations."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import httpx

from nce.config import cfg
from nce.db_utils import POOL_ACQUIRE_TIMEOUT, scoped_pg_session
from nce.ops.elm import diagnose_incident
from nce.ops.models import ProbeResult

log = logging.getLogger("nce.ops.caddy")

_RQ_QUEUES: tuple[str, ...] = (
    "nce-critical",
    "nce-default",
    "nce-bulk",
    "high_priority",
    "batch_processing",
    "diag_ingest",
    "default",
)

_SALIENCE_BUFFER_PATTERN = "nce:ns:*:salience_buffer"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _parse_json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                return decoded
        except Exception:
            return {}
    return {}


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_disk_probe_path(target_dir: str | Path) -> Path:
    candidate = Path(target_dir)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate if candidate.exists() else Path.cwd()


def _parse_delete_count(exec_status: Any) -> int:
    if isinstance(exec_status, int):
        return max(0, exec_status)
    if isinstance(exec_status, str):
        tail = exec_status.strip().rsplit(" ", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return 0


async def _probe_stuck_sagas(pg_pool: Any, *, ttl_seconds: int) -> ProbeResult:
    now = datetime.now(timezone.utc)
    metrics: dict[str, Any] = {"ttl_seconds": ttl_seconds}
    if pg_pool is None:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="stuck_sagas",
            status="warn",
            metrics_json={**metrics, "error": "pg_pool_unavailable"},
            created_at=now,
            recommended_playbook="recover_stuck_sagas",
            fingerprint="probe:stuck_sagas",
            title="Stuck sagas probe skipped (no Postgres pool)",
        )
    try:
        async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE state = 'started'
                          AND COALESCE(updated_at, created_at) < now() - ($1 * interval '1 second')
                    )::int AS stuck_started,
                    COUNT(*) FILTER (
                        WHERE state = 'pg_committed'
                          AND COALESCE(updated_at, created_at) < now() - ($1 * interval '1 second')
                    )::int AS stuck_pg_committed,
                    COUNT(*) FILTER (WHERE state = 'recovery_needed')::int AS recovery_needed
                FROM saga_execution_log
                WHERE state IN ('started', 'pg_committed', 'recovery_needed')
                """,
                ttl_seconds,
            )
        stuck_started = int(row.get("stuck_started", 0) or 0) if row else 0
        stuck_pg_committed = int(row.get("stuck_pg_committed", 0) or 0) if row else 0
        recovery_needed = int(row.get("recovery_needed", 0) or 0) if row else 0
        stuck_total = stuck_started + stuck_pg_committed
        metrics.update(
            {
                "stuck_started": stuck_started,
                "stuck_pg_committed": stuck_pg_committed,
                "recovery_needed": recovery_needed,
                "stuck_total": stuck_total,
            }
        )
        if stuck_total >= 5 or recovery_needed >= 5:
            status = "critical"
        elif stuck_total > 0 or recovery_needed > 0:
            status = "warn"
        else:
            status = "ok"
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="stuck_sagas",
            status=status,
            metrics_json=metrics,
            created_at=now,
            recommended_playbook="recover_stuck_sagas" if status != "ok" else None,
            fingerprint="probe:stuck_sagas",
            title="Stuck sagas detected past TTL" if status != "ok" else "Sagas healthy",
        )
    except Exception as exc:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="stuck_sagas",
            status="critical",
            metrics_json={**metrics, "error": f"{type(exc).__name__}: {exc}"},
            created_at=now,
            recommended_playbook="recover_stuck_sagas",
            fingerprint="probe:stuck_sagas",
            title="Stuck sagas probe failed",
        )


async def _probe_rq_queues_and_quarantine(
    pg_pool: Any,
    redis_client: Any | None,
    *,
    backlog_warn: int,
    backlog_critical: int,
) -> ProbeResult:
    now = datetime.now(timezone.utc)
    metrics: dict[str, Any] = {
        "open_quarantine": 0,
        "queued_total": 0,
        "failed_total": 0,
        "queues": {},
        "failed_registries": {},
    }
    try:
        open_quarantine = 0
        if pg_pool is not None:
            async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
                val = await conn.fetchval(
                    "SELECT COUNT(*)::int FROM ops_quarantine WHERE status = 'quarantined'"
                )
                open_quarantine = int(val or 0)
        metrics["open_quarantine"] = open_quarantine

        queued_total = 0
        failed_total = 0
        if redis_client is not None:
            metrics["redis_available"] = True
            llen_fn = getattr(redis_client, "llen", None)
            zcard_fn = getattr(redis_client, "zcard", None)
            for q_name in _RQ_QUEUES:
                q_len = 0
                f_len = 0
                if callable(llen_fn):
                    q_len = int((await _maybe_await(llen_fn(f"rq:queue:{q_name}"))) or 0)
                if callable(zcard_fn):
                    f_len = int((await _maybe_await(zcard_fn(f"rq:failed:{q_name}"))) or 0)
                metrics["queues"][q_name] = q_len
                metrics["failed_registries"][q_name] = f_len
                queued_total += q_len
                failed_total += f_len
            if callable(zcard_fn):
                global_failed = int((await _maybe_await(zcard_fn("rq:failed"))) or 0)
                if global_failed:
                    metrics["failed_registries"]["_global"] = global_failed
                    failed_total += global_failed
        else:
            metrics["redis_available"] = False

        metrics["queued_total"] = queued_total
        metrics["failed_total"] = failed_total

        if open_quarantine >= 10 or failed_total >= 50 or queued_total >= backlog_critical:
            status = "critical"
        elif open_quarantine > 0 or failed_total > 0 or queued_total >= backlog_warn:
            status = "warn"
        else:
            status = "ok"

        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="rq_queues_and_quarantine",
            status=status,
            metrics_json=metrics,
            created_at=now,
            recommended_playbook="resolve_quarantined_job" if open_quarantine > 0 else None,
            fingerprint="probe:rq_queues_and_quarantine",
            title=(
                "RQ backlog / failed / quarantine items detected"
                if status != "ok"
                else "RQ queues and quarantine healthy"
            ),
        )
    except Exception as exc:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="rq_queues_and_quarantine",
            status="critical",
            metrics_json={**metrics, "error": f"{type(exc).__name__}: {exc}"},
            created_at=now,
            recommended_playbook="resolve_quarantined_job",
            fingerprint="probe:rq_queues_and_quarantine",
            title="RQ queues and quarantine probe failed",
        )


async def _probe_postgres_pool_and_locks(
    pg_pool: Any,
    *,
    waiting_locks_warn: int,
    idle_in_tx_warn: int,
) -> ProbeResult:
    now = datetime.now(timezone.utc)
    metrics: dict[str, Any] = {}
    if pg_pool is None:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="postgres_pool_and_locks",
            status="critical",
            metrics_json={"error": "pg_pool_unavailable"},
            created_at=now,
            fingerprint="probe:postgres_pool_and_locks",
            title="PostgreSQL pool unavailable",
        )
    try:
        pool_util = 0.0
        if callable(getattr(pg_pool, "get_size", None)) and callable(
            getattr(pg_pool, "get_max_size", None)
        ):
            psize = int(pg_pool.get_size())
            pmax = int(pg_pool.get_max_size())
            pidle = (
                int(pg_pool.get_idle_size())
                if callable(getattr(pg_pool, "get_idle_size", None))
                else 0
            )
            pool_util = round(psize / pmax, 4) if pmax > 0 else 0.0
            metrics["pool_size"] = psize
            metrics["pool_max_size"] = pmax
            metrics["pool_idle_size"] = pidle
            metrics["pool_utilization"] = pool_util

        async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)::int AS total_connections,
                    COUNT(*) FILTER (WHERE state = 'active')::int AS active_connections,
                    COUNT(*) FILTER (WHERE state = 'idle in transaction')::int AS idle_in_transaction,
                    COUNT(*) FILTER (WHERE wait_event_type = 'Lock')::int AS waiting_locks
                FROM pg_stat_activity
                WHERE datname = current_database()
                """
            )
        total_conns = int(row.get("total_connections", 0) or 0) if row else 0
        active_conns = int(row.get("active_connections", 0) or 0) if row else 0
        idle_in_tx = int(row.get("idle_in_transaction", 0) or 0) if row else 0
        waiting_locks = int(row.get("waiting_locks", 0) or 0) if row else 0
        metrics.update(
            {
                "total_connections": total_conns,
                "active_connections": active_conns,
                "idle_in_transaction": idle_in_tx,
                "waiting_locks": waiting_locks,
            }
        )

        if (
            waiting_locks >= waiting_locks_warn * 4
            or idle_in_tx >= idle_in_tx_warn * 3
            or pool_util >= 0.95
        ):
            status = "critical"
        elif (
            waiting_locks >= waiting_locks_warn
            or idle_in_tx >= idle_in_tx_warn
            or pool_util >= 0.85
        ):
            status = "warn"
        else:
            status = "ok"

        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="postgres_pool_and_locks",
            status=status,
            metrics_json=metrics,
            created_at=now,
            fingerprint="probe:postgres_pool_and_locks",
            title=(
                "PostgreSQL lock waits or session saturation detected"
                if status != "ok"
                else "PostgreSQL pool and locks healthy"
            ),
        )
    except Exception as exc:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="postgres_pool_and_locks",
            status="critical",
            metrics_json={**metrics, "error": f"{type(exc).__name__}: {exc}"},
            created_at=now,
            fingerprint="probe:postgres_pool_and_locks",
            title="PostgreSQL pool and locks probe failed",
        )


async def _scan_salience_buffer_keys(redis_client: Any) -> list[Any]:
    raw_keys: list[Any] = []
    scan_fn = getattr(redis_client, "scan_iter", None)
    keys_fn = getattr(redis_client, "keys", None)
    if callable(scan_fn) and "keys" not in getattr(redis_client, "__dict__", {}):
        async for k in scan_fn(match=_SALIENCE_BUFFER_PATTERN, count=100):
            raw_keys.append(k)
    elif callable(keys_fn):
        res = await _maybe_await(keys_fn(_SALIENCE_BUFFER_PATTERN))
        raw_keys = list(res or [])
    return raw_keys


async def _probe_redis_memory_and_buffers(
    redis_client: Any | None,
    *,
    mem_warn_ratio: float,
    mem_critical_ratio: float,
) -> ProbeResult:
    now = datetime.now(timezone.utc)
    if redis_client is None:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="redis_memory_and_buffers",
            status="ok",
            metrics_json={"redis_available": False, "skipped": True},
            created_at=now,
            fingerprint="probe:redis_memory_and_buffers",
            title="Redis memory probe skipped (client not supplied)",
        )
    metrics: dict[str, Any] = {"redis_available": True}
    try:
        info_fn = getattr(redis_client, "info", None)
        mem_info: dict[str, Any] = {}
        if callable(info_fn):
            try:
                raw_info = await _maybe_await(info_fn("memory"))
            except TypeError:
                raw_info = await _maybe_await(info_fn())
            if isinstance(raw_info, dict):
                mem_info = raw_info

        used_memory = int(mem_info.get("used_memory", 0) or 0)
        maxmemory = int(mem_info.get("maxmemory", 0) or 0)
        utilization = round(used_memory / maxmemory, 4) if maxmemory > 0 else 0.0

        salience_keys = await _scan_salience_buffer_keys(redis_client)
        hlen_fn = getattr(redis_client, "hlen", None)
        salience_total = 0
        salience_max = 0
        if callable(hlen_fn):
            for key in salience_keys:
                length = int((await _maybe_await(hlen_fn(key))) or 0)
                salience_total += length
                salience_max = max(salience_max, length)

        max_buf_len = int(cfg.NCE_SALIENCE_BUFFER_MAX_LEN)
        metrics.update(
            {
                "used_memory": used_memory,
                "maxmemory": maxmemory,
                "memory_utilization": utilization,
                "salience_buffer_keys": len(salience_keys),
                "salience_buffer_total_len": salience_total,
                "salience_buffer_max_key_len": salience_max,
                "salience_buffer_max_len_limit": max_buf_len,
            }
        )

        if (maxmemory > 0 and utilization >= mem_critical_ratio) or salience_total >= (
            max_buf_len * 2
        ):
            status = "critical"
        elif (maxmemory > 0 and utilization >= mem_warn_ratio) or salience_total >= max_buf_len:
            status = "warn"
        else:
            status = "ok"

        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="redis_memory_and_buffers",
            status=status,
            metrics_json=metrics,
            created_at=now,
            recommended_playbook=(
                "cleanup_expired_leases_and_drills" if salience_total >= max_buf_len else None
            ),
            fingerprint="probe:redis_memory_and_buffers",
            title=(
                "Redis memory or salience_buffer threshold exceeded"
                if status != "ok"
                else "Redis memory and buffers healthy"
            ),
        )
    except Exception as exc:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="redis_memory_and_buffers",
            status="critical",
            metrics_json={**metrics, "error": f"{type(exc).__name__}: {exc}"},
            created_at=now,
            fingerprint="probe:redis_memory_and_buffers",
            title="Redis memory and buffers probe failed",
        )


async def _probe_backup_freshness_and_leases(pg_pool: Any) -> ProbeResult:
    now = datetime.now(timezone.utc)
    max_age_hours = int(cfg.NCE_BACKUP_MAX_AGE_HOURS)
    metrics: dict[str, Any] = {"max_age_hours": max_age_hours}
    if pg_pool is None:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="backup_freshness_and_leases",
            status="warn",
            metrics_json={**metrics, "error": "pg_pool_unavailable"},
            created_at=now,
            recommended_playbook="run_immediate_backup_or_drill",
            fingerprint="probe:backup_freshness_and_leases",
            title="Backup freshness probe skipped (no Postgres pool)",
        )
    try:
        async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
            manifest_row = await conn.fetchrow(
                """
                SELECT backup_id, t_cut, created_at, status, last_drill_at, last_drill_status
                FROM backup_manifests
                WHERE status = 'verified'
                ORDER BY COALESCE(t_cut, created_at) DESC
                LIMIT 1
                """
            )
            lease_row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE expires_at <= now())::int AS expired_leases,
                    COUNT(*) FILTER (
                        WHERE lease_name LIKE 'restore_drill%' AND expires_at > now()
                    )::int AS active_drill_leases
                FROM backup_leases
                """
            )
            stale_drill_val = await conn.fetchval(
                "SELECT COUNT(*)::int FROM pg_database WHERE datname LIKE 'nce_drill_%'"
            )

        verified_exists = manifest_row is not None
        age_hours: float | None = None
        if manifest_row is not None:
            ref_ts = _as_utc(manifest_row.get("t_cut") or manifest_row.get("created_at"))
            if ref_ts is not None:
                age_hours = round(max(0.0, (now - ref_ts).total_seconds() / 3600.0), 3)

        expired_leases = int(lease_row.get("expired_leases", 0) or 0) if lease_row else 0
        active_drill_leases = int(lease_row.get("active_drill_leases", 0) or 0) if lease_row else 0
        drill_db_count = int(stale_drill_val or 0)
        stale_drill_dbs = max(0, drill_db_count - active_drill_leases)

        metrics.update(
            {
                "verified_backup_exists": verified_exists,
                "latest_backup_id": (
                    str(manifest_row["backup_id"]) if manifest_row is not None else None
                ),
                "latest_backup_age_hours": age_hours,
                "expired_leases": expired_leases,
                "active_drill_leases": active_drill_leases,
                "drill_db_count": drill_db_count,
                "stale_drill_dbs": stale_drill_dbs,
            }
        )

        if (age_hours is not None and age_hours > max_age_hours * 2) or stale_drill_dbs >= 3:
            status = "critical"
        elif (
            not verified_exists
            or (age_hours is not None and age_hours > max_age_hours)
            or expired_leases > 0
            or stale_drill_dbs > 0
        ):
            status = "warn"
        else:
            status = "ok"

        rec_playbook: str | None = None
        if status != "ok":
            if expired_leases > 0 or stale_drill_dbs > 0:
                rec_playbook = "cleanup_expired_leases_and_drills"
            else:
                rec_playbook = "run_immediate_backup_or_drill"

        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="backup_freshness_and_leases",
            status=status,
            metrics_json=metrics,
            created_at=now,
            recommended_playbook=rec_playbook,
            fingerprint="probe:backup_freshness_and_leases",
            title=(
                "Backup freshness SLA breach or stale leases/drills detected"
                if status != "ok"
                else "Backup manifests and leases healthy"
            ),
        )
    except Exception as exc:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="backup_freshness_and_leases",
            status="critical",
            metrics_json={**metrics, "error": f"{type(exc).__name__}: {exc}"},
            created_at=now,
            recommended_playbook="run_immediate_backup_or_drill",
            fingerprint="probe:backup_freshness_and_leases",
            title="Backup freshness and leases probe failed",
        )


async def _probe_cognitive_runtime_mode(
    cognitive_url: str | None,
    *,
    http_client: Any | None = None,
) -> ProbeResult:
    now = datetime.now(timezone.utc)
    base = (
        (
            cognitive_url
            if cognitive_url is not None
            else getattr(cfg, "NCE_COGNITIVE_BASE_URL", "") or ""
        )
        .strip()
        .rstrip("/")
    )
    stub_allowed = bool(getattr(cfg, "NCE_ALLOW_COGNITIVE_STUB", False))
    metrics: dict[str, Any] = {"base_url": base or None, "stub_allowed": stub_allowed}

    if not base:
        in_proc = (getattr(cfg, "NCE_BACKEND", "") or "").strip()
        if in_proc:
            return ProbeResult(
                run_id=uuid.uuid4(),
                probe_name="cognitive_runtime_mode",
                status="ok",
                metrics_json={**metrics, "mode": "in_process", "backend": in_proc},
                created_at=now,
                fingerprint="probe:cognitive_runtime_mode",
                title="Cognitive runtime using configured in-process backend",
            )
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="cognitive_runtime_mode",
            status="ok",
            metrics_json={**metrics, "mode": "unconfigured", "skipped": True},
            created_at=now,
            fingerprint="probe:cognitive_runtime_mode",
            title="Cognitive runtime URL unconfigured",
        )

    ready_url = f"{base}/readyz"
    try:
        if http_client is not None:
            resp = await http_client.get(ready_url, timeout=2.0)
        else:
            timeout_cfg = httpx.Timeout(timeout=2.0, connect=1.0)
            async with httpx.AsyncClient(timeout=timeout_cfg, trust_env=False) as client:
                resp = await client.get(ready_url)
        payload = resp.json() if getattr(resp, "content", b"") else {}
        if not isinstance(payload, dict):
            payload = {}
        engine = str(payload.get("engine") or "").strip()
        ready_status = str(payload.get("status") or "").strip().lower()
        metrics.update(
            {
                "http_status": resp.status_code,
                "engine": engine or None,
                "ready_status": ready_status or None,
            }
        )

        if resp.status_code != 200:
            status = "critical" if (resp.status_code >= 500 or cfg.IS_PROD) else "warn"
        elif engine.lower() == "stub" and not stub_allowed:
            status = "critical" if cfg.IS_PROD else "warn"
        elif ready_status and ready_status != "ok":
            status = "warn"
        else:
            status = "ok"

        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="cognitive_runtime_mode",
            status=status,
            metrics_json=metrics,
            created_at=now,
            fingerprint="probe:cognitive_runtime_mode",
            title=(
                "Cognitive runtime stub active or readiness degraded"
                if status != "ok"
                else "Cognitive runtime ready"
            ),
        )
    except Exception as exc:
        status = "critical" if cfg.IS_PROD else "warn"
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="cognitive_runtime_mode",
            status=status,
            metrics_json={**metrics, "error": f"{type(exc).__name__}: {exc}"},
            created_at=now,
            fingerprint="probe:cognitive_runtime_mode",
            title="Cognitive runtime /readyz unreachable",
        )


def _probe_disk_headroom_sync(backup_dir: str | Path | None) -> ProbeResult:
    now = datetime.now(timezone.utc)
    configured_dir = str(backup_dir or cfg.NCE_BACKUP_DIR)
    probe_path = _resolve_disk_probe_path(configured_dir)
    min_free = int(cfg.NCE_OPS_MIN_FREE_BYTES)
    try:
        usage = shutil.disk_usage(probe_path)
        free_gib = round(usage.free / (1024**3), 3)
        free_pct = round((usage.free / usage.total) * 100.0, 2) if usage.total > 0 else 0.0
        metrics = {
            "backup_dir": configured_dir,
            "probe_path": str(probe_path),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "free_gib": free_gib,
            "free_pct": free_pct,
            "min_free_bytes": min_free,
        }
        if usage.free < (min_free // 2) or (usage.total > 0 and free_pct < 2.0):
            status = "critical"
        elif usage.free < min_free or (usage.total > 0 and free_pct < 5.0):
            status = "warn"
        else:
            status = "ok"
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="disk_headroom",
            status=status,
            metrics_json=metrics,
            created_at=now,
            recommended_playbook="sweep_confirmed_orphans" if status != "ok" else None,
            fingerprint="probe:disk_headroom",
            title=(
                "Backup volume disk headroom below threshold"
                if status != "ok"
                else "Backup disk headroom healthy"
            ),
        )
    except Exception as exc:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="disk_headroom",
            status="critical",
            metrics_json={
                "backup_dir": configured_dir,
                "error": f"{type(exc).__name__}: {exc}",
            },
            created_at=now,
            fingerprint="probe:disk_headroom",
            title="Disk headroom probe failed",
        )


async def _persist_probe_and_reconcile_incident(
    pg_pool: Any,
    probe: ProbeResult,
    *,
    redis_client: Any | None = None,
    invoke_elm: bool = True,
    elm_http_client: Any | None = None,
) -> None:
    if pg_pool is None:
        return

    now = probe.created_at or datetime.now(timezone.utc)
    fingerprint = probe.fingerprint or f"probe:{probe.probe_name}"
    title = probe.title or f"Probe {probe.probe_name} status={probe.status}"

    async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
        await conn.execute(
            """
            INSERT INTO ops_probe_runs (run_id, probe_name, status, metrics_json, created_at)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            """,
            probe.run_id,
            probe.probe_name,
            probe.status,
            json.dumps(probe.metrics_json, sort_keys=True, default=str),
            now,
        )

        if probe.status == "ok":
            await conn.execute(
                """
                UPDATE ops_incidents
                SET status = 'resolved',
                    resolved_at = COALESCE(resolved_at, $2),
                    last_seen_at = $2
                WHERE fingerprint = $1 AND status = 'open'
                """,
                fingerprint,
                now,
            )
            return

        existing = await conn.fetchrow(
            """
            SELECT incident_id, status, severity, occurrence_count, elm_brief_json
            FROM ops_incidents
            WHERE fingerprint = $1
            """,
            fingerprint,
        )

    is_new = existing is None
    prev_status = str(existing["status"]) if existing is not None else "none"
    prev_severity = str(existing["severity"]) if existing is not None else "ok"
    transitioned = (
        is_new
        or prev_status == "resolved"
        or (prev_severity == "warn" and probe.status == "critical")
    )
    needs_brief = transitioned or (existing is not None and existing.get("elm_brief_json") is None)

    elm_brief_json: str | None = None
    elm_mode_used = "none"
    recommended_playbook = probe.recommended_playbook

    if invoke_elm and needs_brief:
        facts = {
            "probe_name": probe.probe_name,
            "status": probe.status,
            "previous_status": prev_status if not is_new else "ok",
            "fingerprint": fingerprint,
            "transitioned": transitioned,
            "is_new": is_new,
            "occurrence_count": (int(existing["occurrence_count"]) + 1) if existing else 1,
            "recommended_playbook": probe.recommended_playbook,
            "metrics": probe.metrics_json,
        }
        brief, elm_mode_used = await diagnose_incident(
            facts,
            redis_client=redis_client,
            http_client=elm_http_client,
        )
        elm_brief_json = brief.model_dump_json()
        if brief.recommended_playbook_id:
            recommended_playbook = brief.recommended_playbook_id

    async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
        await conn.execute(
            """
            INSERT INTO ops_incidents (
                incident_id,
                fingerprint,
                severity,
                status,
                title,
                deterministic_facts,
                elm_brief_json,
                elm_mode_used,
                recommended_playbook,
                occurrence_count,
                first_seen_at,
                last_seen_at,
                resolved_at
            )
            VALUES ($1, $2, $3, 'open', $4, $5::jsonb, $6::jsonb, $7, $8, 1, $9, $9, NULL)
            ON CONFLICT (fingerprint) DO UPDATE SET
                severity = EXCLUDED.severity,
                status = 'open',
                title = EXCLUDED.title,
                deterministic_facts = EXCLUDED.deterministic_facts,
                elm_brief_json = COALESCE(EXCLUDED.elm_brief_json, ops_incidents.elm_brief_json),
                elm_mode_used = CASE
                    WHEN EXCLUDED.elm_brief_json IS NOT NULL THEN EXCLUDED.elm_mode_used
                    ELSE ops_incidents.elm_mode_used
                END,
                recommended_playbook = COALESCE(
                    EXCLUDED.recommended_playbook, ops_incidents.recommended_playbook
                ),
                occurrence_count = ops_incidents.occurrence_count + 1,
                last_seen_at = EXCLUDED.last_seen_at,
                resolved_at = NULL
            """,
            uuid.uuid4(),
            fingerprint,
            probe.status,
            title,
            json.dumps(probe.metrics_json, sort_keys=True, default=str),
            elm_brief_json,
            elm_mode_used,
            recommended_playbook,
            now,
        )


async def run_fast_watchdog_probes(
    pg_pool: Any,
    redis_client: Any | None = None,
    cognitive_url: str | None = None,
    backup_dir: str | Path | None = None,
    *,
    persist: bool = True,
    invoke_elm: bool = True,
    elm_http_client: Any | None = None,
    cognitive_http_client: Any | None = None,
    stuck_saga_ttl_seconds: int = 300,
    rq_backlog_warn_threshold: int = 500,
    rq_backlog_critical_threshold: int = 5000,
    pg_waiting_locks_warn: int = 5,
    pg_idle_in_tx_warn: int = 10,
    redis_mem_warn_ratio: float = 0.80,
    redis_mem_critical_ratio: float = 0.95,
) -> list[ProbeResult]:
    """Run all 7 deterministic fast watchdog probes, persist runs, and reconcile ``ops_incidents``."""
    results: list[ProbeResult] = [
        await _probe_stuck_sagas(pg_pool, ttl_seconds=stuck_saga_ttl_seconds),
        await _probe_rq_queues_and_quarantine(
            pg_pool,
            redis_client,
            backlog_warn=rq_backlog_warn_threshold,
            backlog_critical=rq_backlog_critical_threshold,
        ),
        await _probe_postgres_pool_and_locks(
            pg_pool,
            waiting_locks_warn=pg_waiting_locks_warn,
            idle_in_tx_warn=pg_idle_in_tx_warn,
        ),
        await _probe_redis_memory_and_buffers(
            redis_client,
            mem_warn_ratio=redis_mem_warn_ratio,
            mem_critical_ratio=redis_mem_critical_ratio,
        ),
        await _probe_backup_freshness_and_leases(pg_pool),
        await _probe_cognitive_runtime_mode(cognitive_url, http_client=cognitive_http_client),
        await asyncio.to_thread(_probe_disk_headroom_sync, backup_dir),
    ]

    if persist and pg_pool is not None:
        for probe in results:
            await _persist_probe_and_reconcile_incident(
                pg_pool,
                probe,
                redis_client=redis_client,
                invoke_elm=invoke_elm,
                elm_http_client=elm_http_client,
            )

    return results


async def _fetch_namespaces(pg_pool: Any) -> list[UUID]:
    async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
        rows = await conn.fetch("SELECT id FROM namespaces")
    return [r["id"] if isinstance(r["id"], UUID) else UUID(str(r["id"])) for r in rows]


async def _probe_event_log_hash_chain(pg_pool: Any) -> ProbeResult:
    now = datetime.now(timezone.utc)
    if pg_pool is None:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="event_log_hash_chain",
            status="warn",
            metrics_json={"error": "pg_pool_unavailable"},
            created_at=now,
            fingerprint="probe:event_log_hash_chain",
            title="Incremental hash-chain probe skipped (no Postgres pool)",
        )
    try:
        from nce.event_log import verify_merkle_chain

        prev_cursors: dict[str, int] = {}
        async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
            prev_row = await conn.fetchrow(
                """
                SELECT metrics_json
                FROM ops_probe_runs
                WHERE probe_name = 'event_log_hash_chain'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            if prev_row is not None:
                prev_metrics = _parse_json_obj(prev_row.get("metrics_json"))
                raw_cursors = prev_metrics.get("cursors")
                if isinstance(raw_cursors, dict):
                    for k, v in raw_cursors.items():
                        try:
                            prev_cursors[str(k)] = int(v)
                        except (TypeError, ValueError):
                            continue

        namespaces = await _fetch_namespaces(pg_pool)
        new_cursors: dict[str, int] = dict(prev_cursors)
        checked_events = 0
        broken_namespaces: list[dict[str, Any]] = []

        for ns_id in namespaces:
            prev_seq = int(prev_cursors.get(str(ns_id), 0) or 0)

            async def _verify_ns(
                conn: Any, _ns: UUID = ns_id, _prev: int = prev_seq
            ) -> dict[str, Any]:
                max_seq_val = await conn.fetchval(
                    "SELECT COALESCE(max(event_seq), 0) FROM event_log WHERE namespace_id = $1",
                    _ns,
                )
                max_seq = int(max_seq_val or 0)
                if max_seq <= _prev:
                    return {"valid": True, "checked": 0, "last_verified_seq": _prev}
                return await verify_merkle_chain(conn, namespace_id=_ns, start_seq=_prev + 1)

            if isinstance(pg_pool, asyncpg.Pool):
                async with scoped_pg_session(pg_pool, ns_id) as conn:
                    res = await _verify_ns(conn)
            else:
                async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
                    res = await _verify_ns(conn)

            checked_events += int(res.get("checked", 0) or 0)
            last_seq = int(res.get("last_verified_seq", prev_seq) or prev_seq)
            if res.get("valid", True):
                new_cursors[str(ns_id)] = max(prev_seq, last_seq)
            else:
                broken_namespaces.append(
                    {
                        "namespace_id": str(ns_id),
                        "first_break": res.get("first_break"),
                        "reason": res.get("reason") or "merkle_chain_mismatch",
                    }
                )

        status = "critical" if broken_namespaces else "ok"
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="event_log_hash_chain",
            status=status,
            metrics_json={
                "namespaces_scanned": len(namespaces),
                "events_checked": checked_events,
                "broken_count": len(broken_namespaces),
                "broken_namespaces": broken_namespaces[:20],
                "cursors": new_cursors,
            },
            created_at=now,
            fingerprint="probe:event_log_hash_chain",
            title=(
                "Event log Merkle hash-chain corruption detected"
                if status != "ok"
                else "Event log Merkle hash-chain verified"
            ),
        )
    except Exception as exc:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="event_log_hash_chain",
            status="critical",
            metrics_json={"error": f"{type(exc).__name__}: {exc}"},
            created_at=now,
            fingerprint="probe:event_log_hash_chain",
            title="Event log hash-chain probe failed",
        )


async def _probe_dangling_payload_pointers(
    pg_pool: Any,
    mongo_client: Any | None,
    minio_client: Any | None,
    *,
    sample_limit_per_ns: int,
) -> ProbeResult:
    now = datetime.now(timezone.utc)
    if pg_pool is None:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="dangling_payload_pointers",
            status="warn",
            metrics_json={"error": "pg_pool_unavailable"},
            created_at=now,
            fingerprint="probe:dangling_payload_pointers",
            title="Dangling payload pointer probe skipped (no Postgres pool)",
        )
    try:
        from bson import ObjectId

        namespaces = await _fetch_namespaces(pg_pool)
        sampled_rows = 0
        missing_mongo = 0
        missing_minio = 0

        for ns_id in namespaces:

            async def _fetch_sample(conn: Any, _ns: UUID = ns_id) -> list[Any]:
                return list(
                    await conn.fetch(
                        """
                        SELECT id, payload_ref, metadata
                        FROM memories
                        WHERE namespace_id = $1
                          AND valid_to IS NULL
                        ORDER BY created_at DESC
                        LIMIT $2
                        """,
                        _ns,
                        sample_limit_per_ns,
                    )
                )

            if isinstance(pg_pool, asyncpg.Pool):
                async with scoped_pg_session(pg_pool, ns_id) as conn:
                    rows = await _fetch_sample(conn)
            else:
                async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
                    rows = await _fetch_sample(conn)

            sampled_rows += len(rows)
            if not rows:
                continue

            if mongo_client is not None:
                episodes_col = mongo_client.memory_archive.episodes
                valid_oids: list[ObjectId] = []
                for r in rows:
                    ref = str(r.get("payload_ref") or "").strip()
                    if ObjectId.is_valid(ref):
                        valid_oids.append(ObjectId(ref))
                    elif ref:
                        missing_mongo += 1
                present_oids: set[ObjectId] = set()
                if valid_oids:
                    cursor = episodes_col.find(
                        {"_id": {"$in": valid_oids}, "namespace_id": str(ns_id)},
                        {"_id": 1},
                    )
                    if hasattr(cursor, "to_list"):
                        docs = await cursor.to_list(length=len(valid_oids))
                        present_oids = {
                            d["_id"] for d in docs if isinstance(d, dict) and "_id" in d
                        }
                for oid in valid_oids:
                    if oid not in present_oids:
                        missing_mongo += 1

            if minio_client is not None:
                for r in rows:
                    meta = _parse_json_obj(r.get("metadata"))
                    obj_name = meta.get("object_name")
                    if not obj_name:
                        continue
                    bucket = str(meta.get("bucket") or "nce-artifacts")
                    try:
                        await asyncio.to_thread(minio_client.stat_object, bucket, str(obj_name))
                    except Exception:
                        missing_minio += 1

        dangling_total = missing_mongo + missing_minio
        status = "warn" if dangling_total > 0 else "ok"
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="dangling_payload_pointers",
            status=status,
            metrics_json={
                "namespaces_scanned": len(namespaces),
                "sampled_memories": sampled_rows,
                "missing_mongo_docs": missing_mongo,
                "missing_minio_objects": missing_minio,
                "dangling_total": dangling_total,
                "mongo_checked": mongo_client is not None,
                "minio_checked": minio_client is not None,
            },
            created_at=now,
            recommended_playbook="sweep_confirmed_orphans" if status != "ok" else None,
            fingerprint="probe:dangling_payload_pointers",
            title=(
                "Dangling MongoDB or MinIO payload pointers detected"
                if status != "ok"
                else "Sampled payload pointers verified"
            ),
        )
    except Exception as exc:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="dangling_payload_pointers",
            status="warn",
            metrics_json={"error": f"{type(exc).__name__}: {exc}"},
            created_at=now,
            fingerprint="probe:dangling_payload_pointers",
            title="Dangling payload pointer probe failed",
        )


async def _probe_prune_old_probe_runs(pg_pool: Any, *, retention_days: int) -> ProbeResult:
    now = datetime.now(timezone.utc)
    if pg_pool is None:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="ops_probe_runs_retention",
            status="ok",
            metrics_json={"pruned_rows": 0, "retention_days": retention_days, "skipped": True},
            created_at=now,
            fingerprint="probe:ops_probe_runs_retention",
            title="Probe retention skipped (no Postgres pool)",
        )
    cutoff = now - timedelta(days=max(1, int(retention_days)))
    try:
        async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
            res = await conn.execute(
                "DELETE FROM ops_probe_runs WHERE created_at < $1",
                cutoff,
            )
        pruned = _parse_delete_count(res)
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="ops_probe_runs_retention",
            status="ok",
            metrics_json={"pruned_rows": pruned, "retention_days": retention_days},
            created_at=now,
            fingerprint="probe:ops_probe_runs_retention",
            title="Pruned aged ops_probe_runs rows",
        )
    except Exception as exc:
        return ProbeResult(
            run_id=uuid.uuid4(),
            probe_name="ops_probe_runs_retention",
            status="warn",
            metrics_json={
                "retention_days": retention_days,
                "error": f"{type(exc).__name__}: {exc}",
            },
            created_at=now,
            fingerprint="probe:ops_probe_runs_retention",
            title="Probe retention sweep failed",
        )


async def run_deep_integrity_probes(
    pg_pool: Any,
    mongo_client: Any | None = None,
    minio_client: Any | None = None,
    *,
    persist: bool = True,
    invoke_elm: bool = True,
    elm_http_client: Any | None = None,
    sample_limit_per_ns: int = 50,
    probe_retention_days: int = 30,
) -> list[ProbeResult]:
    """Run deep nightly integrity checks: Merkle hash chain, payload pointers, and 30d run pruning."""
    results: list[ProbeResult] = [
        await _probe_event_log_hash_chain(pg_pool),
        await _probe_dangling_payload_pointers(
            pg_pool,
            mongo_client,
            minio_client,
            sample_limit_per_ns=sample_limit_per_ns,
        ),
        await _probe_prune_old_probe_runs(pg_pool, retention_days=probe_retention_days),
    ]

    if persist and pg_pool is not None:
        for probe in results:
            await _persist_probe_and_reconcile_incident(
                pg_pool,
                probe,
                invoke_elm=invoke_elm,
                elm_http_client=elm_http_client,
            )

    return results


__all__ = [
    "run_deep_integrity_probes",
    "run_fast_watchdog_probes",
]
