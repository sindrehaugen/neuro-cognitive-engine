"""Confirm-first ``@governed_playbook`` repair playbooks for Day-2 operations."""

from __future__ import annotations

import functools
import hashlib
import hmac
import inspect
import json
import logging
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from nce.config import cfg, secret_env
from nce.db_utils import POOL_ACQUIRE_TIMEOUT
from nce.ops.models import (
    OpsDiskHeadroomError,
    OpsRestorePreflightError,
    PlaybookPreviewResult,
)

log = logging.getLogger("nce.ops.playbooks")

_DEFAULT_CONFIRMATION_TTL_S = 900  # 15 minutes


class PlaybookConfirmationError(PermissionError, ValueError):
    """Raised when a playbook execution lacks a valid, unexpired HMAC confirmation token."""


@dataclass(frozen=True)
class PlaybookSpec:
    """Registry entry describing a governed Day-2 repair playbook."""

    playbook_id: str
    description: str
    handler: Callable[..., Awaitable[dict[str, Any]]]
    governed_fn: Callable[..., Awaitable[PlaybookPreviewResult]]
    required_params: tuple[str, ...] = ()
    ttl_seconds: int = _DEFAULT_CONFIRMATION_TTL_S


PLAYBOOK_REGISTRY: dict[str, PlaybookSpec] = {}


def _canonical_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize *params* via sorted JSON round-trip so key order never changes the token."""
    raw = dict(params or {})
    normalized: dict[str, Any] = json.loads(
        json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
    )
    return normalized


def _confirmation_hmac_key() -> bytes:
    raw = (secret_env("NCE_MASTER_KEY", "") or getattr(cfg, "NCE_MASTER_KEY", "") or "").strip()
    if not raw:
        raise PlaybookConfirmationError(
            "NCE_MASTER_KEY is required to issue and verify playbook confirmation tokens"
        )
    return hashlib.sha256(b"nce-ops-playbook-confirm-v1:" + raw.encode("utf-8")).digest()


def _sign_token_payload(playbook_id: str, params: dict[str, Any], exp_epoch: int) -> str:
    payload_bytes = json.dumps(
        {
            "playbook_id": playbook_id,
            "params": _canonical_params(params),
            "exp": int(exp_epoch),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hmac.new(_confirmation_hmac_key(), payload_bytes, hashlib.sha256).hexdigest()


def issue_confirmation_token(
    playbook_id: str,
    params: dict[str, Any] | None = None,
    *,
    ttl_seconds: int = _DEFAULT_CONFIRMATION_TTL_S,
) -> tuple[str, datetime]:
    """Mint an HMAC-SHA256 confirmation token bound to ``(playbook_id, sorted params)``."""
    now_epoch = int(time.time())
    exp_epoch = now_epoch + max(1, int(ttl_seconds))
    sig_hex = _sign_token_payload(playbook_id, params or {}, exp_epoch)
    expires_at = datetime.fromtimestamp(exp_epoch, tz=timezone.utc)
    return f"{exp_epoch}.{sig_hex}", expires_at


def verify_confirmation_token(
    playbook_id: str,
    params: dict[str, Any] | None,
    confirmation_token: str | None,
) -> None:
    """Verify HMAC-SHA256 signature and expiry of *confirmation_token* using constant-time compare."""
    if not confirmation_token or not isinstance(confirmation_token, str):
        raise PlaybookConfirmationError("Missing playbook confirmation_token")
    parts = confirmation_token.strip().split(".", 1)
    if len(parts) != 2:
        raise PlaybookConfirmationError("Malformed playbook confirmation_token")
    exp_str, provided_sig = parts
    if not exp_str.isdigit() or len(provided_sig) != 64:
        raise PlaybookConfirmationError("Malformed playbook confirmation_token")

    exp_epoch = int(exp_str)
    expected_sig = _sign_token_payload(playbook_id, params or {}, exp_epoch)
    if not hmac.compare_digest(provided_sig.lower(), expected_sig.lower()):
        raise PlaybookConfirmationError("Invalid confirmation_token for playbook/parameters")
    if exp_epoch < int(time.time()):
        raise PlaybookConfirmationError("Expired playbook confirmation_token")


async def _call_adaptive(fn: Callable[..., Any], **candidates: Any) -> Any:
    """Invoke *fn* with matching parameters (supports asyncpg/mock signatures)."""
    sig = inspect.signature(fn)
    has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    call_kwargs: dict[str, Any] = {}
    for name in sig.parameters:
        if name in candidates:
            call_kwargs[name] = candidates[name]
        elif name == "pool" and "pg_pool" in candidates:
            call_kwargs[name] = candidates["pg_pool"]
    if has_varkw:
        for k, v in candidates.items():
            if k not in call_kwargs and v is not None:
                call_kwargs[k] = v
    res = fn(**call_kwargs)
    if inspect.isawaitable(res):
        return await res
    return res


def _parse_delete_count(exec_status: Any) -> int:
    if isinstance(exec_status, int):
        return max(0, exec_status)
    if isinstance(exec_status, str):
        tail = exec_status.strip().rsplit(" ", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return 0


def _resolve_disk_probe_path(target_dir: str | Path) -> Path:
    candidate = Path(target_dir)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate if candidate.exists() else Path.cwd()


async def _record_execution_and_audit(
    pg_pool: Any,
    *,
    playbook_id: str,
    approved_by: str,
    affected_count: int,
    result_json: dict[str, Any],
    incident_id: UUID | str | None = None,
) -> str | None:
    """Persist ``ops_playbook_executions`` and append a WORM ``config_changed`` event on ``_system``."""
    if pg_pool is None:
        return None

    execution_id = uuid.uuid4()
    incident_uuid = UUID(str(incident_id)) if incident_id is not None else None
    executed_at = datetime.now(timezone.utc)

    async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
        await conn.execute(
            """
            INSERT INTO ops_playbook_executions (
                execution_id, incident_id, playbook_id, dry_run, approved_by, result_json, executed_at
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
            """,
            execution_id,
            incident_uuid,
            playbook_id,
            False,
            approved_by,
            json.dumps(result_json, sort_keys=True, default=str),
            executed_at,
        )

    # Best-effort signed WORM audit event on the reserved _system namespace.
    audit_event_id: str | None = None
    try:
        from nce.auth import set_namespace_context
        from nce.event_log import append_event
        from nce.system_namespace import SystemNamespaceMissingError, get_system_namespace_id

        async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as audit_conn:
            tx_fn = getattr(audit_conn, "transaction", None)
            if callable(tx_fn):
                async with tx_fn():
                    system_ns_id = await get_system_namespace_id(audit_conn)
                    await set_namespace_context(audit_conn, system_ns_id)
                    append_res = await append_event(
                        conn=audit_conn,
                        namespace_id=system_ns_id,
                        agent_id=approved_by[:128],
                        event_type="config_changed",
                        params={
                            "actor": approved_by,
                            "reason": f"ops_playbook:{playbook_id}",
                            "changes": {
                                f"ops.playbook.{playbook_id}": {
                                    "execution_id": str(execution_id),
                                    "playbook_id": playbook_id,
                                    "incident_id": str(incident_uuid) if incident_uuid else None,
                                    "affected_count": affected_count,
                                    "approved_by": approved_by,
                                }
                            },
                        },
                        result_summary={
                            "execution_id": str(execution_id),
                            "affected_count": affected_count,
                        },
                    )
                    audit_event_id = str(append_res.event_id)
            else:
                system_ns_id = await get_system_namespace_id(audit_conn)
                await set_namespace_context(audit_conn, system_ns_id)
                append_res = await append_event(
                    conn=audit_conn,
                    namespace_id=system_ns_id,
                    agent_id=approved_by[:128],
                    event_type="config_changed",
                    params={
                        "actor": approved_by,
                        "reason": f"ops_playbook:{playbook_id}",
                        "changes": {
                            f"ops.playbook.{playbook_id}": {
                                "execution_id": str(execution_id),
                                "playbook_id": playbook_id,
                                "incident_id": str(incident_uuid) if incident_uuid else None,
                                "affected_count": affected_count,
                                "approved_by": approved_by,
                            }
                        },
                    },
                    result_summary={
                        "execution_id": str(execution_id),
                        "affected_count": affected_count,
                    },
                )
                audit_event_id = str(append_res.event_id)
    except SystemNamespaceMissingError:
        log.warning(
            "Skipping WORM audit event for playbook %s: _system namespace missing",
            playbook_id,
        )
    except Exception as exc:
        log.warning(
            "Skipping WORM audit event for playbook %s (%s): %s",
            playbook_id,
            type(exc).__name__,
            exc,
        )

    return audit_event_id


def governed_playbook(
    playbook_id: str,
    *,
    description: str = "",
    required_params: tuple[str, ...] = (),
    ttl_seconds: int = _DEFAULT_CONFIRMATION_TTL_S,
) -> Callable[
    [Callable[..., Awaitable[dict[str, Any]]]], Callable[..., Awaitable[PlaybookPreviewResult]]
]:
    """Register and enforce confirm-first HMAC governance around a Day-2 repair playbook."""

    def decorator(
        fn: Callable[..., Awaitable[dict[str, Any]]],
    ) -> Callable[..., Awaitable[PlaybookPreviewResult]]:
        @functools.wraps(fn)
        async def wrapper(
            params: dict[str, Any] | None = None,
            *,
            dry_run: bool = True,
            confirmation_token: str | None = None,
            approved_by: str | None = None,
            pg_pool: Any = None,
            incident_id: UUID | str | None = None,
            **deps: Any,
        ) -> PlaybookPreviewResult:
            norm_params = _canonical_params(params)
            for req_key in required_params:
                if req_key not in norm_params or norm_params[req_key] in (None, ""):
                    raise ValueError(f"Playbook {playbook_id!r} requires parameter {req_key!r}")

            if dry_run:
                outcome = await fn(
                    norm_params,
                    dry_run=True,
                    pg_pool=pg_pool,
                    approved_by=approved_by or "preview",
                    **deps,
                )
                affected = int(outcome.get("affected_count", 0) or 0)
                impact = dict(outcome.get("estimated_impact") or outcome)
                token, expires_at = issue_confirmation_token(
                    playbook_id, norm_params, ttl_seconds=ttl_seconds
                )
                return PlaybookPreviewResult(
                    playbook_id=playbook_id,
                    dry_run=True,
                    estimated_impact=impact,
                    affected_count=max(0, affected),
                    confirmation_token=token,
                    expires_at=expires_at,
                    executed=False,
                    result_json={},
                    audit_event_id=None,
                )

            if not approved_by or not str(approved_by).strip():
                raise ValueError("approved_by is required when executing a governed playbook")
            actor = str(approved_by).strip()
            verify_confirmation_token(playbook_id, norm_params, confirmation_token)

            outcome = await fn(
                norm_params,
                dry_run=False,
                pg_pool=pg_pool,
                approved_by=actor,
                **deps,
            )
            affected = int(outcome.get("affected_count", 0) or 0)
            result_json = dict(outcome.get("result_json") or outcome)
            audit_event_id = await _record_execution_and_audit(
                pg_pool,
                playbook_id=playbook_id,
                approved_by=actor,
                affected_count=max(0, affected),
                result_json=result_json,
                incident_id=incident_id,
            )
            return PlaybookPreviewResult(
                playbook_id=playbook_id,
                dry_run=False,
                estimated_impact={},
                affected_count=max(0, affected),
                confirmation_token=None,
                expires_at=None,
                executed=True,
                result_json=result_json,
                audit_event_id=audit_event_id,
            )

        PLAYBOOK_REGISTRY[playbook_id] = PlaybookSpec(
            playbook_id=playbook_id,
            description=description,
            handler=fn,
            governed_fn=wrapper,
            required_params=required_params,
            ttl_seconds=ttl_seconds,
        )
        return wrapper

    return decorator


async def _is_t_cut_backup_lease_active(pg_pool: Any) -> bool:
    if pg_pool is None:
        return False
    async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
        try:
            active = await conn.fetchval("SELECT nce_is_backup_lease_active('t_cut_backup')")
            return bool(active)
        except asyncpg.UndefinedFunctionError:
            active = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM backup_leases
                    WHERE lease_name = 't_cut_backup' AND expires_at > now()
                )
                """
            )
            return bool(active)


@governed_playbook(
    "sweep_confirmed_orphans",
    description="Purge unreferenced Mongo/MinIO blobs older than 1h when no t_cut_backup lease is active.",
)
async def _playbook_sweep_confirmed_orphans(
    params: dict[str, Any],
    *,
    dry_run: bool,
    pg_pool: Any,
    approved_by: str,
    mongo_client: Any = None,
    minio_client: Any = None,
    **_: Any,
) -> dict[str, Any]:
    grace_seconds = max(3600, int(params.get("grace_seconds", 3600)))
    lease_active = await _is_t_cut_backup_lease_active(pg_pool)
    if lease_active:
        payload = {
            "blocked_by_active_backup_lease": True,
            "grace_seconds": grace_seconds,
            "purged": 0,
            "reason": "t_cut_backup lease is currently active",
        }
        return {"affected_count": 0, "estimated_impact": payload, "result_json": payload}

    if dry_run:
        candidate_count = 0
        if mongo_client is not None and pg_pool is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)
            db = mongo_client.memory_archive
            for col_name in ("episodes", "code_files"):
                try:
                    cursor = db[col_name].find({"ingested_at": {"$lt": cutoff}}, {"_id": 1})
                    if hasattr(cursor, "to_list"):
                        docs = await cursor.to_list(length=5000)
                        candidate_count += len(docs)
                except Exception as exc:
                    log.debug("Orphan preview Mongo scan skipped for %s: %s", col_name, exc)
        impact = {
            "blocked_by_active_backup_lease": False,
            "grace_seconds": grace_seconds,
            "candidate_blobs": candidate_count,
        }
        return {"affected_count": candidate_count, "estimated_impact": impact}

    from nce.ops.backup import sweep_confirmed_orphans

    raw_res = await _call_adaptive(
        sweep_confirmed_orphans,
        pg_pool=pg_pool,
        mongo_client=mongo_client,
        minio_client=minio_client,
        grace_seconds=grace_seconds,
    )
    if isinstance(raw_res, dict):
        purged = int(
            raw_res.get("purged_count")
            or (
                int(raw_res.get("deleted_docs", 0) or 0) + int(raw_res.get("deleted_minio", 0) or 0)
            )
            or raw_res.get("deleted", 0)
            or 0
        )
        result_json = dict(raw_res)
    else:
        purged = int(raw_res or 0)
        result_json = {"purged_count": purged}
    result_json.setdefault("grace_seconds", grace_seconds)
    result_json.setdefault("approved_by", approved_by)
    return {"affected_count": purged, "result_json": result_json}


@governed_playbook(
    "recover_stuck_sagas",
    description="Run compensating Saga recovery for sagas stuck past TTL.",
)
async def _playbook_recover_stuck_sagas(
    params: dict[str, Any],
    *,
    dry_run: bool,
    pg_pool: Any,
    approved_by: str,
    mongo_client: Any = None,
    **_: Any,
) -> dict[str, Any]:
    ttl_seconds = max(60, int(params.get("ttl_seconds", 300)))
    stuck_started = 0
    stuck_pg_committed = 0
    recovery_needed = 0

    if pg_pool is not None:
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
            if row is not None:
                stuck_started = int(row.get("stuck_started", 0) or 0)
                stuck_pg_committed = int(row.get("stuck_pg_committed", 0) or 0)
                recovery_needed = int(row.get("recovery_needed", 0) or 0)

    before_total = stuck_started + stuck_pg_committed + recovery_needed
    if dry_run:
        return {
            "affected_count": before_total,
            "estimated_impact": {
                "stuck_started": stuck_started,
                "stuck_pg_committed": stuck_pg_committed,
                "recovery_needed": recovery_needed,
                "ttl_seconds": ttl_seconds,
            },
        }

    from nce.cron import _saga_recovery_tick

    await _call_adaptive(_saga_recovery_tick, pool=pg_pool, mongo_client=mongo_client)

    after_total = 0
    if pg_pool is not None:
        async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
            after_val = await conn.fetchval(
                """
                SELECT COUNT(*)::int
                FROM saga_execution_log
                WHERE state = 'recovery_needed'
                   OR (
                        state IN ('started', 'pg_committed')
                        AND COALESCE(updated_at, created_at) < now() - ($1 * interval '1 second')
                   )
                """,
                ttl_seconds,
            )
            after_total = int(after_val or 0)

    recovered = max(0, before_total - after_total) if before_total >= after_total else before_total
    return {
        "affected_count": recovered or before_total,
        "result_json": {
            "before_stuck_count": before_total,
            "after_stuck_count": after_total,
            "recovered_count": recovered,
            "approved_by": approved_by,
        },
    }


@governed_playbook(
    "resolve_quarantined_job",
    description="Requeue or dismiss a quarantined poison-pill job in ops_quarantine.",
    required_params=("action", "quarantine_id"),
)
async def _playbook_resolve_quarantined_job(
    params: dict[str, Any],
    *,
    dry_run: bool,
    pg_pool: Any,
    approved_by: str,
    redis_client: Any = None,
    **_: Any,
) -> dict[str, Any]:
    action = str(params["action"]).strip().lower()
    if action not in {"requeue", "dismiss"}:
        raise ValueError(
            f"resolve_quarantined_job action must be 'requeue' or 'dismiss', got {action!r}"
        )

    quarantine_uuid = UUID(str(params["quarantine_id"]))
    reason = params.get("reason")

    if dry_run:
        row_info: dict[str, Any] = {}
        affected = 0
        if pg_pool is not None:
            async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
                row = await conn.fetchrow(
                    """
                    SELECT quarantine_id, job_fingerprint, queue_name, func_name, failure_count, status
                    FROM ops_quarantine
                    WHERE quarantine_id = $1
                    """,
                    quarantine_uuid,
                )
                if row is not None:
                    row_info = {
                        "quarantine_id": str(row["quarantine_id"]),
                        "queue_name": row["queue_name"],
                        "func_name": row["func_name"],
                        "failure_count": int(row["failure_count"]),
                        "current_status": row["status"],
                    }
                    affected = 1 if row["status"] == "quarantined" else 0
        return {
            "affected_count": affected,
            "estimated_impact": {
                "action": action,
                "quarantine_id": str(quarantine_uuid),
                **row_info,
            },
        }

    from nce.ops.quarantine import dismiss_quarantined_job, requeue_quarantined_job

    if action == "requeue":
        res = await _call_adaptive(
            requeue_quarantined_job,
            pg_pool=pg_pool,
            quarantine_id=quarantine_uuid,
            redis_client=redis_client,
            approved_by=approved_by,
        )
    else:
        res = await _call_adaptive(
            dismiss_quarantined_job,
            pg_pool=pg_pool,
            quarantine_id=quarantine_uuid,
            approved_by=approved_by,
            reason=reason,
        )

    result_json = dict(res) if isinstance(res, dict) else {"result": res}
    result_json.setdefault("action", action)
    result_json.setdefault("quarantine_id", str(quarantine_uuid))
    return {"affected_count": 1, "result_json": result_json}


async def _count_salience_buffer_fields(redis_client: Any) -> int:
    if redis_client is None:
        return 0
    total = 0
    scan_fn = getattr(redis_client, "scan_iter", None)
    keys_fn = getattr(redis_client, "keys", None)
    raw_keys: list[Any] = []
    if callable(scan_fn) and "keys" not in getattr(redis_client, "__dict__", {}):
        async for k in scan_fn(match="nce:ns:*:salience_buffer", count=100):
            raw_keys.append(k)
    elif callable(keys_fn):
        res = keys_fn("nce:ns:*:salience_buffer")
        if inspect.isawaitable(res):
            res = await res
        raw_keys = list(res or [])

    hlen_fn = getattr(redis_client, "hlen", None)
    if callable(hlen_fn):
        for k in raw_keys:
            cnt = hlen_fn(k)
            if inspect.isawaitable(cnt):
                cnt = await cnt
            total += int(cnt or 0)
    return total


@governed_playbook(
    "cleanup_expired_leases_and_drills",
    description="Delete expired backup_leases, flush Redis salience_buffer, and sweep stale nce_drill_* targets.",
)
async def _playbook_cleanup_expired_leases_and_drills(
    params: dict[str, Any],
    *,
    dry_run: bool,
    pg_pool: Any,
    approved_by: str,
    redis_client: Any = None,
    mongo_client: Any = None,
    minio_client: Any = None,
    pg_dsn: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    if dry_run:
        expired_leases = 0
        stale_drill_dbs = 0
        if pg_pool is not None:
            async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
                expired_leases = int(
                    (
                        await conn.fetchval(
                            "SELECT COUNT(*)::int FROM backup_leases WHERE expires_at <= now()"
                        )
                    )
                    or 0
                )
                stale_drill_dbs = int(
                    (
                        await conn.fetchval(
                            "SELECT COUNT(*)::int FROM pg_database WHERE datname LIKE 'nce_drill_%'"
                        )
                    )
                    or 0
                )
        salience_entries = await _count_salience_buffer_fields(redis_client)
        total = expired_leases + salience_entries + stale_drill_dbs
        return {
            "affected_count": total,
            "estimated_impact": {
                "expired_leases": expired_leases,
                "salience_buffer_entries": salience_entries,
                "stale_drill_dbs": stale_drill_dbs,
            },
        }

    deleted_leases = 0
    if pg_pool is not None:
        async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
            exec_res = await conn.execute("DELETE FROM backup_leases WHERE expires_at <= now()")
            deleted_leases = _parse_delete_count(exec_res)

    flushed_salience = 0
    if redis_client is not None and pg_pool is not None:
        from nce.salience_buffer import flush_salience_buffer

        flushed_salience = int(await flush_salience_buffer(redis_client, pg_pool) or 0)

    from nce.ops.backup import sweep_stale_drill_targets

    swept_raw = await _call_adaptive(
        sweep_stale_drill_targets,
        pg_pool=pg_pool,
        pg_dsn=pg_dsn,
        mongo_client=mongo_client,
        minio_client=minio_client,
    )
    if isinstance(swept_raw, dict):
        if any(
            k in swept_raw for k in ("removed_pg", "removed_mongo", "removed_minio", "removed_dirs")
        ):
            swept_drills = sum(
                int(swept_raw.get(k, 0) or 0)
                for k in ("removed_pg", "removed_mongo", "removed_minio", "removed_dirs")
            )
        else:
            swept_drills = int(swept_raw.get("dropped_count", swept_raw.get("swept", 0)) or 0)
    elif isinstance(swept_raw, (list, tuple, set)):
        swept_drills = len(swept_raw)
    else:
        swept_drills = int(swept_raw or 0)

    total_affected = deleted_leases + flushed_salience + swept_drills
    return {
        "affected_count": total_affected,
        "result_json": {
            "deleted_expired_leases": deleted_leases,
            "flushed_salience_entries": flushed_salience,
            "swept_stale_drill_targets": swept_drills,
            "approved_by": approved_by,
        },
    }


@governed_playbook(
    "run_immediate_backup_or_drill",
    description="Execute an immediate T_cut watermark backup or ephemeral restore drill.",
)
async def _playbook_run_immediate_backup_or_drill(
    params: dict[str, Any],
    *,
    dry_run: bool,
    pg_pool: Any,
    approved_by: str,
    mongo_client: Any = None,
    minio_client: Any = None,
    **_: Any,
) -> dict[str, Any]:
    mode = str(params.get("mode") or "backup").strip().lower()
    if mode not in {"backup", "drill"}:
        raise ValueError(
            f"run_immediate_backup_or_drill mode must be 'backup' or 'drill', got {mode!r}"
        )

    raw_backup_id = params.get("backup_id")
    target_backup_id = UUID(str(raw_backup_id)) if raw_backup_id else None
    backup_dir = str(params.get("backup_dir") or cfg.NCE_BACKUP_DIR)

    probe_path = _resolve_disk_probe_path(backup_dir)
    usage = shutil.disk_usage(probe_path)
    min_free = int(cfg.NCE_OPS_MIN_FREE_BYTES)
    headroom_ok = usage.free >= min_free

    if dry_run:
        if mode == "drill" and target_backup_id is None and pg_pool is not None:
            async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
                row = await conn.fetchrow(
                    """
                    SELECT backup_id
                    FROM backup_manifests
                    WHERE status = 'verified'
                    ORDER BY COALESCE(t_cut, created_at) DESC
                    LIMIT 1
                    """
                )
                if row is not None:
                    target_backup_id = UUID(str(row["backup_id"]))
        affected = 1 if (mode == "backup" or target_backup_id is not None) else 0
        return {
            "affected_count": affected,
            "estimated_impact": {
                "mode": mode,
                "backup_dir": backup_dir,
                "free_bytes": usage.free,
                "min_free_bytes": min_free,
                "headroom_ok": headroom_ok,
                "target_backup_id": str(target_backup_id) if target_backup_id else None,
            },
        }

    if not headroom_ok:
        raise OpsDiskHeadroomError(
            f"Insufficient free space at {probe_path}: {usage.free} bytes < minimum {min_free} bytes"
        )

    from nce.ops.backup import create_watermark_backup, run_ephemeral_restore_drill

    if mode == "backup":
        res = await _call_adaptive(
            create_watermark_backup,
            pg_pool=pg_pool,
            mongo_client=mongo_client,
            minio_client=minio_client,
            backup_dir=backup_dir,
        )
    else:
        if target_backup_id is None:
            if pg_pool is None:
                raise OpsRestorePreflightError(
                    "pg_pool is required to resolve latest verified manifest"
                )
            async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
                row = await conn.fetchrow(
                    """
                    SELECT backup_id
                    FROM backup_manifests
                    WHERE status = 'verified'
                    ORDER BY COALESCE(t_cut, created_at) DESC
                    LIMIT 1
                    """
                )
                if row is None:
                    raise OpsRestorePreflightError(
                        "No verified backup manifest found for restore drill"
                    )
                target_backup_id = UUID(str(row["backup_id"]))
        res = await _call_adaptive(
            run_ephemeral_restore_drill,
            pg_pool=pg_pool,
            backup_id=target_backup_id,
            mongo_client=mongo_client,
            minio_client=minio_client,
            backup_dir=backup_dir,
        )

    if hasattr(res, "model_dump"):
        result_json = res.model_dump(mode="json")
    elif isinstance(res, dict):
        result_json = dict(res)
    else:
        result_json = {"result": str(res)}
    result_json.setdefault("mode", mode)
    result_json.setdefault("approved_by", approved_by)
    return {"affected_count": 1, "result_json": result_json}


async def preview_playbook(
    playbook_id: str,
    params: dict[str, Any] | None = None,
    pg_pool: Any = None,
    **kwargs: Any,
) -> PlaybookPreviewResult:
    """Estimate impact for *playbook_id* without mutating state and return a confirmation token."""
    spec = PLAYBOOK_REGISTRY.get(playbook_id)
    if spec is None:
        raise ValueError(f"Unknown playbook_id: {playbook_id!r}")
    return await spec.governed_fn(params or {}, dry_run=True, pg_pool=pg_pool, **kwargs)


async def execute_playbook(
    playbook_id: str,
    params: dict[str, Any] | None = None,
    *,
    confirmation_token: str,
    approved_by: str,
    pg_pool: Any = None,
    incident_id: UUID | str | None = None,
    **kwargs: Any,
) -> PlaybookPreviewResult:
    """Verify HMAC *confirmation_token* and execute *playbook_id* with WORM audit logging."""
    spec = PLAYBOOK_REGISTRY.get(playbook_id)
    if spec is None:
        raise ValueError(f"Unknown playbook_id: {playbook_id!r}")
    return await spec.governed_fn(
        params or {},
        dry_run=False,
        confirmation_token=confirmation_token,
        approved_by=approved_by,
        pg_pool=pg_pool,
        incident_id=incident_id,
        **kwargs,
    )


__all__ = [
    "PLAYBOOK_REGISTRY",
    "PlaybookConfirmationError",
    "PlaybookSpec",
    "execute_playbook",
    "governed_playbook",
    "issue_confirmation_token",
    "preview_playbook",
    "verify_confirmation_token",
]
