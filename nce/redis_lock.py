"""
Shared Redis distributed lock primitives.

Provides the canonical SET NX EX / compare-and-delete pair used by cron_lock
and garbage_collector.  Both callers import from here so the Lua script and
token generation are defined exactly once.

Usage pattern — caller manages the Redis client lifetime:

    token = await acquire_lock(client, "myservice:lock", ttl_seconds=300)
    if token is None:
        return  # another instance holds it
    try:
        ...  # do the protected work
    finally:
        await release_lock(client, "myservice:lock", token)

Key namespacing is the *caller's* responsibility: ``key`` is treated as an
opaque, fully-qualified string here (callers such as ``cron_lock`` may embed a
tenant ``namespace_id`` in it to prevent cross-tenant interference). Whatever
key is acquired must be the same key passed to :func:`release_lock`. The
compare-and-delete release is token-guarded regardless of the key shape — a
holder can only ever delete the value it wrote (see ``RELEASE_LOCK_LUA``), so
namespace scoping never weakens the no-cross-holder-delete guarantee.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

log = logging.getLogger("nce.redis_lock")

# Lua compare-and-delete: only DEL if the stored value still matches our token.
# Atomic — no TOCTOU window between GET and DEL.
RELEASE_LOCK_LUA: str = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
"""


async def acquire_lock(
    client: Any,
    key: str,
    ttl_seconds: int,
) -> str | None:
    """Try to acquire *key* via SET NX EX.

    Parameters
    ----------
    client:
        An open ``redis.asyncio.Redis`` instance.  The caller owns its lifetime;
        this function never closes it.
    key:
        Full Redis key string (including any namespace prefix).
    ttl_seconds:
        Lock TTL — must be >= 1.  Acts as a safety net if the process dies
        before calling :func:`release_lock`.

    Returns
    -------
    str
        A cryptographically random token that identifies this lock owner.
        Pass it to :func:`release_lock` to release early.
    None
        Lock is already held by another instance, or Redis is unreachable.
    """
    if ttl_seconds < 1:
        raise ValueError("ttl_seconds must be >= 1")
    token = secrets.token_urlsafe(32)
    try:
        acquired = await client.set(key, token, nx=True, ex=ttl_seconds)
        return token if acquired else None
    except Exception as exc:
        log.error("Lock acquisition failed for key=%s: %s", key, exc)
        return None


async def release_lock(
    client: Any,
    key: str,
    token: str,
) -> bool:
    """Release *key* only if this process still owns it (Lua CAS).

    Safe to call even if the TTL has already expired — the Lua script returns 0
    without deleting another owner's lock.

    Returns ``True`` if the key was deleted, ``False`` otherwise (expired or
    taken by another instance — both are non-fatal for the caller).
    """
    try:
        released = await client.eval(RELEASE_LOCK_LUA, 1, key, token)
        if not released:
            log.warning(
                "Lock release for key=%s had no effect — "
                "may have expired or been taken by another instance.",
                key,
            )
        return bool(released)
    except Exception as exc:
        log.warning("Lock release failed for key=%s: %s", key, exc)
        return False
