"""Acceptance tests for Batch 117 — namespace-scoped cron locks + token-guarded release.

Two properties are exercised against a *live* test Redis (no AUTH required —
the requirepass change in docker-compose.yml is a compose/config hardening
verified by inspection, not by this test):

1. NAMESPACE SCOPING — two DIFFERENT namespaces acquiring the SAME logical
   per-namespace lock name concurrently must BOTH succeed. This proves the
   namespace id is embedded in the Redis key (no false mutual-exclusion).

2. TOKEN GUARD — the compare-and-delete release only removes the value the
   caller wrote. A holder cannot delete another holder's lock, even on the
   exact same key (no cross-holder delete).

The test connects to ``REDIS_URL`` (default ``redis://127.0.0.1:6380/0`` — the
isolated RL test Redis) and SKIPs gracefully when Redis is unreachable, mirroring
the integration-test skip pattern used elsewhere in the suite. It must never
become a hard failure in a no-Redis environment.
"""

from __future__ import annotations

import os
import socket
import uuid
from urllib.parse import urlparse

import pytest

os.environ.setdefault("NCE_MASTER_KEY", "x" * 32)

from nce.cron_lock import (  # noqa: E402  (env must be set before importing config)
    _CRON_LOCK_NS_MARKER,
    _validated_lock_key,
    acquire_cron_lock,
    release_cron_lock,
)
from nce.redis_lock import acquire_lock, release_lock  # noqa: E402

# The isolated RL test Redis (no password). Override via REDIS_URL if needed.
_TEST_REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6380/0")


def _reachable(url: str, default_host: str = "127.0.0.1", default_port: int = 6380) -> bool:
    """Return True if a TCP connection to the Redis URL host:port succeeds."""
    host, port = default_host, default_port
    try:
        parsed = urlparse(url)
        host = parsed.hostname or host
        port = parsed.port or port
    except Exception:
        pass
    try:
        sock = socket.create_connection((host, port), timeout=1)
        sock.close()
        return True
    except OSError:
        return False


_skip_no_redis = pytest.mark.skipif(
    not _reachable(_TEST_REDIS_URL),
    reason=f"Integration test requires a reachable Redis at {_TEST_REDIS_URL}",
)


def _make_client():
    from redis.asyncio import Redis as AsyncRedis

    return AsyncRedis.from_url(_TEST_REDIS_URL)


# ---------------------------------------------------------------------------
# Pure key-construction tests (no Redis needed) — prove the key format.
# ---------------------------------------------------------------------------


def test_global_lock_key_has_no_namespace_segment():
    key = _validated_lock_key("partition_maintenance")
    assert key == "nce:cron:lock:partition_maintenance"
    assert f":{_CRON_LOCK_NS_MARKER}:" not in key


def test_namespace_lock_key_embeds_namespace_id():
    ns_a = "11111111-1111-1111-1111-111111111111"
    ns_b = "22222222-2222-2222-2222-222222222222"
    key_a = _validated_lock_key("sleep_consolidation", ns_a)
    key_b = _validated_lock_key("sleep_consolidation", ns_b)
    assert key_a == f"nce:cron:lock:{_CRON_LOCK_NS_MARKER}:{ns_a}:sleep_consolidation"
    # Same logical job, different namespace → DIFFERENT keys (no collision).
    assert key_a != key_b


def test_namespace_id_with_colon_is_rejected():
    # A colon would inject extra key segments → forge another tenant's key.
    with pytest.raises(ValueError):
        _validated_lock_key("job", "bad:namespace")


def test_global_job_id_with_colon_is_rejected():
    # A colon-bearing global job_id could reproduce the ``ns:<id>:`` prefix and
    # build the byte-identical key of a per-namespace lock — collapsing the
    # keyspace separation that isolates tenants. It must be rejected.
    with pytest.raises(ValueError):
        _validated_lock_key("ns:x:y")


# ---------------------------------------------------------------------------
# Live-Redis acceptance tests.
# ---------------------------------------------------------------------------


@_skip_no_redis
@pytest.mark.asyncio
async def test_two_namespaces_same_lock_name_both_acquire():
    """Different namespaces, SAME logical lock name → both succeed (key is scoped)."""
    job_id = f"acpt_consolidation_{uuid.uuid4().hex[:8]}"
    ns_a = uuid.uuid4().hex
    ns_b = uuid.uuid4().hex

    client = _make_client()
    lock_a = None
    lock_b = None
    try:
        lock_a = await acquire_cron_lock(
            job_id, ttl_seconds=30, namespace_id=ns_a, redis_client=client
        )
        lock_b = await acquire_cron_lock(
            job_id, ttl_seconds=30, namespace_id=ns_b, redis_client=client
        )

        assert lock_a is not None, "namespace A failed to acquire its scoped lock"
        assert lock_b is not None, (
            "namespace B was falsely excluded — the namespace id is NOT in the key"
        )
        # Distinct keys prove the scoping.
        assert lock_a.key != lock_b.key
        assert ns_a in lock_a.key and ns_b in lock_b.key
    finally:
        if lock_a is not None:
            await release_cron_lock(lock_a, redis_client=client)
        if lock_b is not None:
            await release_cron_lock(lock_b, redis_client=client)
        await client.aclose()


@_skip_no_redis
@pytest.mark.asyncio
async def test_same_namespace_same_lock_is_mutually_exclusive():
    """Same namespace + same job → second acquire is refused (lock still works)."""
    job_id = f"acpt_excl_{uuid.uuid4().hex[:8]}"
    ns = uuid.uuid4().hex

    client = _make_client()
    first = None
    try:
        first = await acquire_cron_lock(
            job_id, ttl_seconds=30, namespace_id=ns, redis_client=client
        )
        assert first is not None
        second = await acquire_cron_lock(
            job_id, ttl_seconds=30, namespace_id=ns, redis_client=client
        )
        assert second is None, "namespace-scoped lock failed to exclude a second holder"
    finally:
        if first is not None:
            await release_cron_lock(first, redis_client=client)
        await client.aclose()


@_skip_no_redis
@pytest.mark.asyncio
async def test_release_token_guard_no_cross_holder_delete():
    """A holder cannot delete another holder's lock — token compare-and-delete holds.

    Same physical key, but the second caller presents a different (forged) token,
    so the Lua CAS refuses to delete the real owner's value.
    """
    key = f"nce:test:acpt:token_guard:{uuid.uuid4().hex}"

    client = _make_client()
    try:
        # Real owner acquires the key.
        owner_token = await acquire_lock(client, key, ttl_seconds=30)
        assert owner_token is not None

        # An impostor with a DIFFERENT token tries to release the same key.
        impostor_token = "not-the-owners-token"
        assert impostor_token != owner_token
        released_by_impostor = await release_lock(client, key, impostor_token)
        assert released_by_impostor is False, "cross-holder delete succeeded — token guard broken"

        # The real owner's value is still present and untouched.
        still_held = await client.get(key)
        assert still_held is not None
        # Owner can still release with the correct token.
        released_by_owner = await release_lock(client, key, owner_token)
        assert released_by_owner is True
        assert await client.get(key) is None
    finally:
        # Best-effort cleanup if an assertion above left the key behind.
        try:
            await client.delete(key)
        except Exception:
            pass
        await client.aclose()
