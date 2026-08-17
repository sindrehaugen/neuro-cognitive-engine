"""
Distributed lock helper for singleton cron jobs.

Uses Redis SET NX EX so only one cron instance runs a given job at a time.
Extracted from cron.py to keep it importable without APScheduler.

Lock primitives (Lua CAS script, token generation) are centralised in
``nce.redis_lock`` and imported here — single definition, no duplication.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from nce.config import cfg
from nce.redis_lock import acquire_lock as _acquire_lock
from nce.redis_lock import release_lock as _release_lock

log = logging.getLogger("nce.cron")

_CRON_LOCK_PREFIX = "nce:cron:lock"
# Segment inserted before the namespace id for per-tenant (namespace-scoped) locks.
# Global / system locks omit this segment entirely, so the two key spaces can
# never collide: job_id is colon-free (see ``_JOB_ID_RE``), so a global key
# ``nce:cron:lock:<job_id>`` can never contain the ``ns`` second segment that a
# per-namespace key ``nce:cron:lock:ns:<id>:<job_id>`` always has.
_CRON_LOCK_NS_MARKER = "ns"
# job_id is colon-free (same charset as namespace_id) so it cannot reproduce the
# ``ns:<id>:`` prefix and forge a per-namespace key from the global keyspace.
_JOB_ID_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")
# namespace_id must be a single, colon-free path component so it cannot inject
# extra key segments (which would otherwise let one tenant forge another's key).
_NAMESPACE_ID_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")

# Sentinel for environments where Redis is disabled (non-prod).
_LOCAL_DISABLED = "local-disabled"


@dataclass(frozen=True)
class CronLock:
    """Opaque handle for a distributed cron lock.

    Returned by :func:`acquire_cron_lock` on success.
    Pass to :func:`release_cron_lock` when the job finishes, so the lock is
    released immediately rather than waiting for TTL expiry.
    """

    job_id: str
    key: str
    token: str
    ttl_seconds: int


def _validated_lock_key(job_id: str, namespace_id: str | None = None) -> str:
    """Build the Redis lock key for *job_id*, namespace-scoped when given.

    Key shapes (security: prevents cross-tenant lock interference)::

        global / system lock   →  nce:cron:lock:<job_id>
        per-namespace lock     →  nce:cron:lock:ns:<namespace_id>:<job_id>

    A ``namespace_id`` puts the tenant id *inside* the key, so two different
    namespaces running the SAME logical job acquire two distinct keys and never
    falsely exclude one another. Global locks deliberately omit the ``ns``
    segment so genuinely system-wide singletons stay mutually exclusive.
    """
    if not _JOB_ID_RE.fullmatch(job_id):
        raise ValueError(f"Invalid cron job_id — must match [a-zA-Z0-9_.-]{{1,128}}: {job_id!r}")
    if namespace_id is None:
        return f"{_CRON_LOCK_PREFIX}:{job_id}"
    if not _NAMESPACE_ID_RE.fullmatch(namespace_id):
        raise ValueError(
            f"Invalid namespace_id — must match [a-zA-Z0-9_.-]{{1,128}}: {namespace_id!r}"
        )
    return f"{_CRON_LOCK_PREFIX}:{_CRON_LOCK_NS_MARKER}:{namespace_id}:{job_id}"


async def acquire_cron_lock(
    job_id: str,
    ttl_seconds: int,
    *,
    namespace_id: str | None = None,
    redis_client: Any | None = None,
) -> CronLock | None:
    """Try to acquire a Redis distributed lock for a singleton cron job.

    Parameters
    ----------
    job_id:
        Unique cron job name — must match ``[a-zA-Z0-9_.-]{1,128}``.
    ttl_seconds:
        Lock TTL.  Acts as a safety net if the process dies before
        :func:`release_cron_lock` is called.
    namespace_id:
        Optional tenant id. When given, the lock is **namespace-scoped** — the
        id is embedded in the Redis key (``nce:cron:lock:ns:<id>:<job_id>``) so
        two different namespaces running the same logical per-tenant job do not
        falsely exclude one another, and one tenant cannot disrupt another's
        lock. Omit it for genuinely global/system singletons (e.g. the
        partition-maintenance sweep), which stay mutually exclusive across the
        whole deployment. Must match ``[a-zA-Z0-9_.-]{1,128}``.
    redis_client:
        Optional open ``redis.asyncio.Redis`` instance.  When supplied the
        caller owns its lifetime — this function will not close it.  When
        omitted a temporary client is created and destroyed automatically.
        Pass a shared client from the caller to avoid one TCP round-trip per
        lock acquisition.

    Returns
    -------
    CronLock
        If the lock was acquired — pass to :func:`release_cron_lock`.
    None
        If another instance holds the lock, Redis is unreachable, or locking
        is required but unavailable (production with no REDIS_URL).
    """
    if ttl_seconds < 1:
        raise ValueError("ttl_seconds must be >= 1")

    key = _validated_lock_key(job_id, namespace_id)

    if not cfg.REDIS_URL:
        if cfg.IS_PROD:
            log.error(
                "REDIS_URL not set — refusing cron lock in production for job=%s. "
                "Set REDIS_URL to enable distributed cron locking.",
                job_id,
            )
            return None

        log.warning("REDIS_URL not set — cron distributed lock disabled for %s (non-prod)", job_id)
        return CronLock(
            job_id=job_id,
            key=f"local-disabled:{key}",
            token=_LOCAL_DISABLED,
            ttl_seconds=ttl_seconds,
        )

    owned_client = redis_client is None
    client = redis_client
    try:
        if owned_client:
            from redis.asyncio import Redis as AsyncRedis

            client = AsyncRedis.from_url(cfg.REDIS_URL)

        token = await _acquire_lock(client, key, ttl_seconds)
        if token is None:
            return None

        return CronLock(job_id=job_id, key=key, token=token, ttl_seconds=ttl_seconds)

    except Exception as exc:
        log.error("Cron lock acquisition failed for job=%s: %s", job_id, exc)
        return None

    finally:
        if owned_client and client is not None:
            try:
                await client.aclose()
            except Exception as exc:
                log.warning("Cron lock Redis close failed for job=%s: %s", job_id, exc)


async def release_cron_lock(
    lock: CronLock,
    *,
    redis_client: Any | None = None,
) -> bool:
    """Release a cron lock only if this process still owns it (compare-and-delete).

    Safe to call even if the TTL has expired or another instance has taken
    over — the Lua CAS script returns 0 without deleting another owner's lock.

    Parameters
    ----------
    lock:
        The :class:`CronLock` returned by :func:`acquire_cron_lock`.
    redis_client:
        Optional shared client (same contract as :func:`acquire_cron_lock`).
    """
    if lock.token == _LOCAL_DISABLED:
        return True

    owned_client = redis_client is None
    client = redis_client
    try:
        if owned_client:
            from redis.asyncio import Redis as AsyncRedis

            client = AsyncRedis.from_url(cfg.REDIS_URL)

        return await _release_lock(client, lock.key, lock.token)

    except Exception as exc:
        log.warning("Cron lock release failed for job=%s: %s", lock.job_id, exc)
        return False

    finally:
        if owned_client and client is not None:
            try:
                await client.aclose()
            except Exception as exc:
                log.warning(
                    "Cron lock Redis close after release failed for job=%s: %s",
                    lock.job_id,
                    exc,
                )
