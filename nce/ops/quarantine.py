"""Poison-pill and abrupt-exit quarantine lifecycle for RQ background jobs.

Tracks job fingerprints, sanitizes credentials from tracebacks/arguments before
persistence, upserts quarantined entries into PostgreSQL ``ops_quarantine``, and
exposes operator actions to list, requeue, or dismiss quarantined jobs.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import inspect
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
from rq import Queue
from rq.job import Job

from nce.config import redact_secrets_in_text
from nce.db_utils import resolve_ops_dsn, unmanaged_pg_connection
from nce.ops.models import QuarantinedJobRecord
from nce.signing import canonical_json

log = logging.getLogger("nce-ops.quarantine")

INFLIGHT_KEY_PREFIX = "nce:rq_inflight:"
FAILURES_KEY_PREFIX = "nce:rq_failures:"
INFLIGHT_TTL_SECONDS = 3600
FAILURES_TTL_SECONDS = 86400
MAX_TRACEBACK_CHARS = 8192

_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|master[_-]?key|private[_-]?key|authorization|dsn|hmac|credential)",
    re.IGNORECASE,
)
_EXTRA_SECRET_RE = re.compile(
    r"(?i)(Bearer\s+[A-Za-z0-9\-._~+/]+=*|"
    r"(?:postgres(?:ql)?|redis|rediss|mongodb(?:\+srv)?|mysql|amqp|https?)://[^/\s:@]+:[^@\s]+@|"
    r"(?:NCE_MASTER_KEY|PGPASSWORD|REDIS_PASSWORD|AWS_SECRET_ACCESS_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|"
    r"API_KEY|SECRET_KEY|PASSWORD|ACCESS_TOKEN)\s*[=:]\s*['\"]?[^\s'\",;]+)"
)


def _normalize_for_json(value: Any, *, depth: int = 0) -> Any:
    """Convert arbitrary Python args into deterministic JSON-compatible structures."""
    if depth >= 6:
        return repr(value)[:128]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, (list, tuple)):
        return [_normalize_for_json(item, depth=depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_normalize_for_json(item, depth=depth + 1) for item in value]
        return sorted(items, key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(value, dict):
        return {
            str(k): _normalize_for_json(v, depth=depth + 1)
            for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    return repr(value)[:200]


def _redact_preview_value(value: Any, *, key_name: str = "", depth: int = 0) -> Any:
    """Sanitize and bound job argument previews before storing in ``ops_quarantine``."""
    if key_name and _SENSITIVE_KEY_RE.search(key_name):
        return "[REDACTED]"
    if depth >= 4:
        return "[...]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        cleaned = redact_secrets_in_text(_EXTRA_SECRET_RE.sub("[REDACTED]", value))
        return cleaned[:240]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, (list, tuple)):
        return [_redact_preview_value(v, depth=depth + 1) for v in list(value)[:16]]
    if isinstance(value, dict):
        return {
            str(k): _redact_preview_value(v, key_name=str(k), depth=depth + 1)
            for k, v in list(value.items())[:24]
        }
    return redact_secrets_in_text(repr(value))[:200]


def compute_job_fingerprint(
    queue_name: str,
    func_name: str,
    args: Any = (),
    kwargs: Any = None,
) -> str:
    """Compute deterministic SHA-256 hex fingerprint for an RQ job invocation."""
    payload = {
        "queue_name": str(queue_name or ""),
        "func_name": str(func_name or ""),
        "args": _normalize_for_json(list(args or ())),
        "kwargs": _normalize_for_json(dict(kwargs or {})),
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def sanitize_traceback(tb_text: str) -> str:
    """Strip DSNs, bearer tokens, keys, and passwords from a traceback string."""
    raw = str(tb_text or "")
    redacted = redact_secrets_in_text(raw)

    def _mask_match(match: re.Match[str]) -> str:
        text = match.group(0)
        if "://" in text:
            scheme, rest = text.split("://", 1)
            user_part = rest.split(":", 1)[0]
            return f"{scheme}://{user_part}:[REDACTED]@"
        if "=" in text:
            k, _ = text.split("=", 1)
            return f"{k}=[REDACTED]"
        if ":" in text and not text.lower().startswith("bearer "):
            k, _ = text.split(":", 1)
            return f"{k}: [REDACTED]"
        if text.lower().startswith("bearer "):
            return "Bearer [REDACTED]"
        return "[REDACTED]"

    redacted = _EXTRA_SECRET_RE.sub(_mask_match, redacted)
    if len(redacted) > MAX_TRACEBACK_CHARS:
        return redacted[: MAX_TRACEBACK_CHARS - 18] + "\n...[truncated]..."
    return redacted


def _extract_namespace_id(
    args: Any = (),
    kwargs: Any = None,
    explicit: str | UUID | None = None,
) -> UUID | None:
    candidate: Any = explicit
    if candidate is None and isinstance(kwargs, dict):
        candidate = kwargs.get("namespace_id")
    if candidate is None and isinstance(args, (list, tuple)) and args:
        first = args[0]
        if isinstance(first, dict):
            candidate = first.get("namespace_id")
    if candidate is None:
        return None
    try:
        return UUID(str(candidate))
    except (TypeError, ValueError):
        return None


def _build_job_args_preview(
    args: Any = (),
    kwargs: Any = None,
    *,
    job_id: str | None = None,
) -> dict[str, Any]:
    preview: dict[str, Any] = {
        "args": _redact_preview_value(list(args or ())),
        "kwargs": _redact_preview_value(dict(kwargs or {})),
    }
    if job_id:
        preview["job_id"] = str(job_id)
    return preview


async def _maybe_await(val: Any) -> Any:
    if inspect.isawaitable(val):
        return await val
    return val


async def _run_ops_conn(pg_pool: Any | None, fn: Any) -> Any:
    """Execute ``fn(conn)`` using ``unmanaged_pg_connection`` or ``resolve_ops_dsn``."""
    if pg_pool is not None:
        async with unmanaged_pg_connection(
            pg_pool,
            site="ops.quarantine.control_plane",
        ) as conn:
            return await fn(conn)

    dsn = resolve_ops_dsn()
    if not dsn:
        raise RuntimeError("No PostgreSQL pool or ops DSN available for ops_quarantine")
    conn = await asyncpg.connect(dsn, timeout=5.0)
    try:
        return await fn(conn)
    finally:
        await conn.close()


async def quarantine_poison_job(
    *,
    pg_pool: Any | None,
    queue_name: str,
    func_name: str,
    args: Any = (),
    kwargs: Any = None,
    failure_count: int = 2,
    exit_reason: str = "repeated_exception",
    traceback_text: str = "",
    namespace_id: str | UUID | None = None,
    job_id: str | None = None,
    job_fingerprint: str | None = None,
    quarantine_id: UUID | None = None,
) -> QuarantinedJobRecord:
    """Sanitize and upsert a poison-pill / crashed RQ job into ``ops_quarantine``."""
    fp = job_fingerprint or compute_job_fingerprint(queue_name, func_name, args, kwargs)
    ns_uuid = _extract_namespace_id(args, kwargs, explicit=namespace_id)
    clean_tb = sanitize_traceback(traceback_text)
    args_preview = _build_job_args_preview(args, kwargs, job_id=job_id)
    qid = quarantine_id or uuid.uuid4()
    eff_failures = max(1, int(failure_count))
    now_dt = datetime.now(timezone.utc)

    async def _upsert(conn: Any) -> Any:
        return await conn.fetchrow(
            """
            INSERT INTO ops_quarantine (
                quarantine_id, job_fingerprint, queue_name, func_name,
                namespace_id, failure_count, exit_reason,
                sanitized_traceback, job_args_preview, status,
                created_at, updated_at
            )
            VALUES ($1::uuid, $2, $3, $4, $5::uuid, $6, $7, $8, $9::jsonb, 'quarantined', now(), now())
            ON CONFLICT (job_fingerprint) DO UPDATE
                SET queue_name = EXCLUDED.queue_name,
                    func_name = EXCLUDED.func_name,
                    namespace_id = COALESCE(EXCLUDED.namespace_id, ops_quarantine.namespace_id),
                    failure_count = GREATEST(ops_quarantine.failure_count, EXCLUDED.failure_count),
                    exit_reason = EXCLUDED.exit_reason,
                    sanitized_traceback = EXCLUDED.sanitized_traceback,
                    job_args_preview = EXCLUDED.job_args_preview,
                    status = 'quarantined',
                    updated_at = now()
            RETURNING quarantine_id, created_at, updated_at
            """,
            str(qid),
            fp,
            str(queue_name),
            str(func_name),
            str(ns_uuid) if ns_uuid is not None else None,
            eff_failures,
            str(exit_reason or "repeated_exception"),
            clean_tb,
            json.dumps(args_preview, sort_keys=True),
        )

    row = await _run_ops_conn(pg_pool, _upsert)
    resolved_qid = UUID(str(row["quarantine_id"])) if row and row.get("quarantine_id") else qid
    created_at = row.get("created_at") if row else now_dt
    updated_at = row.get("updated_at") if row else now_dt

    return QuarantinedJobRecord(
        quarantine_id=resolved_qid,
        job_fingerprint=fp,
        queue_name=str(queue_name),
        func_name=str(func_name),
        namespace_id=ns_uuid,
        failure_count=eff_failures,
        exit_reason=str(exit_reason or "repeated_exception"),
        sanitized_traceback=clean_tb,
        job_args_preview=args_preview,
        status="quarantined",
        created_at=created_at,
        updated_at=updated_at,
    )


def quarantine_poison_job_sync(
    *,
    pg_pool: Any | None = None,
    redis_client: Any | None = None,
    queue_name: str,
    func_name: str,
    args: Any = (),
    kwargs: Any = None,
    failure_count: int = 2,
    exit_reason: str = "repeated_exception",
    traceback_text: str = "",
    namespace_id: str | UUID | None = None,
    job_id: str | None = None,
    job_fingerprint: str | None = None,
) -> QuarantinedJobRecord | None:
    """Synchronous worker-safe wrapper calling :func:`quarantine_poison_job`."""
    fp = job_fingerprint or compute_job_fingerprint(queue_name, func_name, args, kwargs)
    clean_tb = sanitize_traceback(traceback_text)
    args_preview = _build_job_args_preview(args, kwargs, job_id=job_id)

    if redis_client is not None:
        try:
            mirror_key = f"nce:ops:quarantine:{fp}"
            mirror_payload = json.dumps(
                {
                    "job_fingerprint": fp,
                    "queue_name": str(queue_name),
                    "func_name": str(func_name),
                    "failure_count": max(1, int(failure_count)),
                    "exit_reason": str(exit_reason),
                    "sanitized_traceback": clean_tb[:1024],
                    "job_args_preview": args_preview,
                    "status": "quarantined",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                sort_keys=True,
            )
            set_fn = getattr(redis_client, "set", None)
            if callable(set_fn):
                res = set_fn(mirror_key, mirror_payload, ex=7 * 86400)
                if inspect.isawaitable(res):
                    # Sync path skips unawaited async coroutine handles cleanly.
                    pass
        except Exception as exc:
            log.debug("Redis quarantine mirror write skipped for %s: %s", fp, exc)

    async def _coro() -> QuarantinedJobRecord:
        return await quarantine_poison_job(
            pg_pool=pg_pool,
            queue_name=queue_name,
            func_name=func_name,
            args=args,
            kwargs=kwargs,
            failure_count=failure_count,
            exit_reason=exit_reason,
            traceback_text=clean_tb,
            namespace_id=namespace_id,
            job_id=job_id,
            job_fingerprint=fp,
        )

    try:
        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False

        if not in_loop:
            return asyncio.run(_coro())

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _coro()).result(timeout=10.0)
    except Exception as exc:
        log.warning("Failed persisting quarantine record for %s to PostgreSQL: %s", fp, exc)
        return None


async def requeue_quarantined_job(
    pg_pool: Any | None,
    redis_client: Any | None,
    quarantine_id_or_fp: str | UUID,
) -> dict[str, Any]:
    """Reset Redis failure/inflight counters, mark row ``requeued``, and re-enqueue."""
    target = str(quarantine_id_or_fp).strip()
    if not target:
        raise ValueError("quarantine_id_or_fp must be non-empty")

    async def _fetch_and_mark(conn: Any) -> Any:
        row = await conn.fetchrow(
            """
            SELECT quarantine_id, job_fingerprint, queue_name, func_name,
                   job_args_preview, status
            FROM ops_quarantine
            WHERE quarantine_id::text = $1 OR job_fingerprint = $1
            """,
            target,
        )
        if not row:
            raise LookupError(f"Quarantined job {target!r} not found")
        await conn.execute(
            """
            UPDATE ops_quarantine
            SET status = 'requeued',
                failure_count = 0,
                updated_at = now()
            WHERE quarantine_id = $1::uuid
            """,
            str(row["quarantine_id"]),
        )
        return dict(row)

    row = await _run_ops_conn(pg_pool, _fetch_and_mark)
    fp = str(row["job_fingerprint"])
    queue_name = str(row["queue_name"])
    func_name = str(row["func_name"])
    raw_preview = row.get("job_args_preview") or {}
    if isinstance(raw_preview, str):
        try:
            raw_preview = json.loads(raw_preview)
        except Exception:
            raw_preview = {}

    enqueued_job_id: str | None = None
    if redis_client is not None:
        del_fn = getattr(redis_client, "delete", None)
        if callable(del_fn):
            await _maybe_await(
                del_fn(
                    f"{INFLIGHT_KEY_PREFIX}{fp}",
                    f"{FAILURES_KEY_PREFIX}{fp}",
                    f"nce:ops:quarantine:{fp}",
                )
            )

        job_id_hint = raw_preview.get("job_id") if isinstance(raw_preview, dict) else None
        is_sync_redis = not inspect.iscoroutinefunction(getattr(redis_client, "get", None))
        if is_sync_redis:
            requeued = False
            if job_id_hint:
                try:
                    existing = Job.fetch(str(job_id_hint), connection=redis_client)
                    origin = Queue(existing.origin or queue_name, connection=redis_client)
                    existing.started_at = None
                    existing.ended_at = None
                    existing._exc_info = ""
                    origin.enqueue_job(existing)
                    enqueued_job_id = existing.id
                    requeued = True
                except Exception:
                    requeued = False
            if not requeued and callable(getattr(redis_client, "rpush", None)):
                args = raw_preview.get("args", []) if isinstance(raw_preview, dict) else []
                kwargs = raw_preview.get("kwargs", {}) if isinstance(raw_preview, dict) else {}
                q = Queue(queue_name, connection=redis_client)
                new_job = q.enqueue(func_name, *args, **kwargs)
                enqueued_job_id = new_job.id

    return {
        "quarantine_id": str(row["quarantine_id"]),
        "job_fingerprint": fp,
        "queue_name": queue_name,
        "status": "requeued",
        "enqueued_job_id": enqueued_job_id,
    }


async def dismiss_quarantined_job(
    pg_pool: Any | None,
    quarantine_id_or_fp: str | UUID,
    *,
    redis_client: Any | None = None,
) -> dict[str, Any]:
    """Mark a quarantined job row as ``dismissed`` and clear transient Redis markers."""
    target = str(quarantine_id_or_fp).strip()
    if not target:
        raise ValueError("quarantine_id_or_fp must be non-empty")

    async def _dismiss(conn: Any) -> Any:
        row = await conn.fetchrow(
            """
            UPDATE ops_quarantine
            SET status = 'dismissed',
                updated_at = now()
            WHERE quarantine_id::text = $1 OR job_fingerprint = $1
            RETURNING quarantine_id, job_fingerprint, queue_name, status, updated_at
            """,
            target,
        )
        if not row:
            raise LookupError(f"Quarantined job {target!r} not found")
        return dict(row)

    row = await _run_ops_conn(pg_pool, _dismiss)
    fp = str(row["job_fingerprint"])
    if redis_client is not None:
        del_fn = getattr(redis_client, "delete", None)
        if callable(del_fn):
            await _maybe_await(
                del_fn(
                    f"{INFLIGHT_KEY_PREFIX}{fp}",
                    f"{FAILURES_KEY_PREFIX}{fp}",
                    f"nce:ops:quarantine:{fp}",
                )
            )
    return {
        "quarantine_id": str(row["quarantine_id"]),
        "job_fingerprint": fp,
        "queue_name": str(row["queue_name"]),
        "status": str(row["status"]),
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


async def list_quarantined_jobs(
    pg_pool: Any | None,
    *,
    status: str | None = "quarantined",
    limit: int = 100,
) -> list[QuarantinedJobRecord]:
    """List quarantined jobs from ``ops_quarantine`` ordered by latest update."""
    capped_limit = max(1, min(1000, int(limit)))

    async def _query(conn: Any) -> list[Any]:
        if status is not None:
            return list(
                await conn.fetch(
                    """
                    SELECT quarantine_id, job_fingerprint, queue_name, func_name,
                           namespace_id, failure_count, exit_reason,
                           sanitized_traceback, job_args_preview, status,
                           created_at, updated_at
                    FROM ops_quarantine
                    WHERE status = $1
                    ORDER BY updated_at DESC
                    LIMIT $2
                    """,
                    str(status),
                    capped_limit,
                )
            )
        return list(
            await conn.fetch(
                """
                SELECT quarantine_id, job_fingerprint, queue_name, func_name,
                       namespace_id, failure_count, exit_reason,
                       sanitized_traceback, job_args_preview, status,
                       created_at, updated_at
                FROM ops_quarantine
                ORDER BY updated_at DESC
                LIMIT $1
                """,
                capped_limit,
            )
        )

    rows = await _run_ops_conn(pg_pool, _query)
    records: list[QuarantinedJobRecord] = []
    for r in rows:
        raw_preview = r["job_args_preview"]
        if isinstance(raw_preview, str):
            try:
                raw_preview = json.loads(raw_preview)
            except Exception:
                raw_preview = {}
        ns_val = r["namespace_id"]
        records.append(
            QuarantinedJobRecord(
                quarantine_id=UUID(str(r["quarantine_id"])),
                job_fingerprint=str(r["job_fingerprint"]),
                queue_name=str(r["queue_name"]),
                func_name=str(r["func_name"]),
                namespace_id=UUID(str(ns_val)) if ns_val else None,
                failure_count=int(r["failure_count"]),
                exit_reason=str(r["exit_reason"]),
                sanitized_traceback=str(r["sanitized_traceback"]),
                job_args_preview=dict(raw_preview or {}),
                status=r["status"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
        )
    return records
