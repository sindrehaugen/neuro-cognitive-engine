"""
NCE Salience Buffer.

Provides Redis-buffered retrieval salience and Long-Term Potentiation (LTP) boosts.
Defers PostgreSQL write load from high-throughput retrieval paths into atomic
Redis increments and periodic batched database flushes.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session, validate_redis_namespace_key
from nce.salience import reinforce

log = logging.getLogger(__name__)

_SALIENCE_BUFFER_SUBKEY = "salience_buffer"


def _get_salience_buffer_key(namespace_id: str | UUID) -> str:
    """Build the tenant-isolated Redis key for salience buffer."""
    return validate_redis_namespace_key(namespace_id, _SALIENCE_BUFFER_SUBKEY)


async def buffer_salience_boost(
    redis_client: Any,
    namespace_id: str | UUID,
    memory_id: str | UUID,
    agent_id: str = "default",
    delta: float = 0.05,
    *,
    as_of: Any = None,
    is_replay: bool = False,
) -> bool:
    """Buffer an incremental salience boost in Redis.

    Immediately returns False (no-op, zero side effects) if as_of is not None
    or is_replay is True (so point-in-time as_of and replay reads never mutate
    live salience!).

    Writes incremental boost via HINCRBYFLOAT on Redis key
    nce:ns:{ns_uuid}:salience_buffer with field {memory_id}:{agent_id}.

    Returns:
        True if the boost was buffered in Redis, False if skipped or failed.
    """
    if as_of is not None or is_replay:
        log.debug(
            "buffer_salience_boost skipped: as_of=%s is_replay=%s",
            as_of,
            is_replay,
        )
        return False

    if redis_client is None:
        return False

    try:
        ns_uuid = str(UUID(str(namespace_id)))
        mem_id_str = (
            str(UUID(str(memory_id))) if isinstance(memory_id, UUID) else str(memory_id).strip()
        )
        cleaned_agent = (agent_id or "").strip()[:128] or "default"
        redis_key = f"nce:ns:{ns_uuid}:{_SALIENCE_BUFFER_SUBKEY}"
        field = f"{mem_id_str}:{cleaned_agent}"

        hincr_fn = getattr(redis_client, "hincrbyfloat", None)
        if hincr_fn is None:
            return False

        res = hincr_fn(redis_key, field, float(delta))
        if hasattr(res, "__await__"):
            await res
        return True
    except Exception as exc:
        log.warning("buffer_salience_boost failed to write to Redis (non-fatal): %s", exc)
        return False


async def flush_salience_buffer(
    redis_client: Any,
    pg_pool: Any,
    namespace_id: str | UUID | None = None,
    batch_size: int = 200,
) -> int:
    """Atomically drain buffered boosts from Redis and batch-apply them in PostgreSQL.

    Flushes up to batch_size boosts per namespace inside scoped_pg_session(pg_pool, namespace_id)
    via nce.salience.reinforce.

    Returns:
        The total number of boosts successfully flushed to the database.
    """
    if redis_client is None or pg_pool is None:
        return 0

    if namespace_id is not None:
        target_namespaces = [str(UUID(str(namespace_id)))]
    else:
        # Discover active namespaces from Redis keys matching nce:ns:*:salience_buffer
        keys_fn = getattr(redis_client, "keys", None)
        if keys_fn is None:
            return 0
        raw_keys = keys_fn("nce:ns:*:salience_buffer")
        if hasattr(raw_keys, "__await__"):
            raw_keys = await raw_keys

        target_namespaces = []
        for k in raw_keys:
            k_str = k.decode("utf-8") if isinstance(k, bytes) else str(k)
            parts = k_str.split(":")
            if (
                len(parts) >= 4
                and parts[0] == "nce"
                and parts[1] == "ns"
                and parts[3] == _SALIENCE_BUFFER_SUBKEY
            ):
                try:
                    target_namespaces.append(str(UUID(parts[2])))
                except ValueError:
                    continue

    total_flushed = 0

    for ns in target_namespaces:
        redis_key = f"nce:ns:{ns}:{_SALIENCE_BUFFER_SUBKEY}"

        # Check if items exist
        hgetall_fn = getattr(redis_client, "hgetall", None)
        if hgetall_fn is None:
            continue

        raw_items = hgetall_fn(redis_key)
        if hasattr(raw_items, "__await__"):
            raw_items = await raw_items

        if not raw_items:
            continue

        # Parse items up to batch_size
        items_to_process: list[tuple[str, str, float]] = []
        fields_to_delete: list[str | bytes] = []

        for f, v in raw_items.items():
            f_str = f.decode("utf-8") if isinstance(f, bytes) else str(f)
            v_val = float(v.decode("utf-8") if isinstance(v, bytes) else v)
            if ":" in f_str:
                mem_id, agent = f_str.split(":", 1)
                items_to_process.append((mem_id, agent, v_val))
                fields_to_delete.append(f)
            if len(items_to_process) >= batch_size:
                break

        if not items_to_process:
            continue

        # Apply inside scoped_pg_session
        try:
            async with scoped_pg_session(pg_pool, ns) as conn:
                for mem_id, agent, delta in items_to_process:
                    await reinforce(
                        conn,
                        memory_id=mem_id,
                        agent_id=agent,
                        namespace_id=ns,
                        delta=delta,
                    )

            # Successfully persisted, now remove from Redis
            hdel_fn = getattr(redis_client, "hdel", None)
            if hdel_fn is not None and fields_to_delete:
                del_res = hdel_fn(redis_key, *fields_to_delete)
                if hasattr(del_res, "__await__"):
                    await del_res

            total_flushed += len(items_to_process)
        except Exception as exc:
            log.warning("Failed to flush salience buffer for namespace %s: %s", ns, exc)

    return total_flushed
