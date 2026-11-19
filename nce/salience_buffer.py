"""
NCE Salience Buffer.

Provides Redis-buffered retrieval salience and Long-Term Potentiation (LTP) boosts.
Defers PostgreSQL write load from high-throughput retrieval paths into atomic
Redis increments and periodic batched database flushes.
"""

from __future__ import annotations

import inspect
import logging
import os
from typing import Any
from uuid import UUID

from nce.config import cfg
from nce.db_utils import scoped_pg_session, validate_redis_namespace_key
from nce.salience import reinforce

log = logging.getLogger(__name__)

_SALIENCE_BUFFER_SUBKEY = "salience_buffer"
_SALIENCE_ORDER_SUBKEY = "salience_buffer_order"
_SALIENCE_BUFFER_TTL_SEC = 86400
_DEFAULT_SALIENCE_BUFFER_MAX_LEN = 50000


def _resolve_salience_buffer_max_len() -> int:
    cfg_dict = getattr(cfg, "__dict__", {})
    raw = cfg_dict.get("NCE_SALIENCE_BUFFER_MAX_LEN")
    if raw is None:
        raw = os.getenv("NCE_SALIENCE_BUFFER_MAX_LEN")
    if raw is None:
        raw = getattr(cfg, "NCE_SALIENCE_BUFFER_MAX_LEN", _DEFAULT_SALIENCE_BUFFER_MAX_LEN)
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_SALIENCE_BUFFER_MAX_LEN
    return max(1, val)


async def _maybe_await(val: Any) -> Any:
    if inspect.isawaitable(val):
        return await val
    return val


_SUBTRACT_FLUSHED_LUA: str = """
for i = 1, #ARGV, 2 do
    local field = ARGV[i]
    local delta = tonumber(ARGV[i + 1])
    local rem = tonumber(redis.call("HINCRBYFLOAT", KEYS[1], field, -delta))
    if rem ~= nil and rem <= 1e-9 then
        redis.call("HDEL", KEYS[1], field)
    end
end
return 1
"""


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
        order_key = f"nce:ns:{ns_uuid}:{_SALIENCE_ORDER_SUBKEY}"
        field = f"{mem_id_str}:{cleaned_agent}"

        hincr_fn = getattr(redis_client, "hincrbyfloat", None)
        if hincr_fn is None:
            return False

        await _maybe_await(hincr_fn(redis_key, field, float(delta)))

        # Bound hash cardinality via accompanying order list + TTL so Redis
        # memory stays capped even under sustained boost bursts.
        try:
            max_len = _resolve_salience_buffer_max_len()
            lrem_fn = getattr(redis_client, "lrem", None)
            rpush_fn = getattr(redis_client, "rpush", None)
            llen_fn = getattr(redis_client, "llen", None)
            lrange_fn = getattr(redis_client, "lrange", None)
            ltrim_fn = getattr(redis_client, "ltrim", None)
            hdel_fn = getattr(redis_client, "hdel", None)
            expire_fn = getattr(redis_client, "expire", None)

            if callable(lrem_fn):
                await _maybe_await(lrem_fn(order_key, 0, field))
            if callable(rpush_fn):
                await _maybe_await(rpush_fn(order_key, field))
            if callable(llen_fn):
                cur_len = await _maybe_await(llen_fn(order_key))
                if isinstance(cur_len, int) and not isinstance(cur_len, bool) and cur_len > max_len:
                    excess = cur_len - max_len
                    evict_fields: list[Any] = []
                    if callable(lrange_fn):
                        raw_evict = await _maybe_await(lrange_fn(order_key, 0, excess - 1))
                        if isinstance(raw_evict, (list, tuple)):
                            evict_fields = [
                                f.decode("utf-8") if isinstance(f, bytes) else str(f)
                                for f in raw_evict
                            ]
                    if callable(ltrim_fn):
                        await _maybe_await(ltrim_fn(order_key, excess, -1))
                    if evict_fields and callable(hdel_fn):
                        await _maybe_await(hdel_fn(redis_key, *evict_fields))

            if callable(expire_fn):
                await _maybe_await(expire_fn(redis_key, _SALIENCE_BUFFER_TTL_SEC))
                await _maybe_await(expire_fn(order_key, _SALIENCE_BUFFER_TTL_SEC))
        except Exception as cap_exc:
            log.debug("Salience buffer bound/TTL update skipped: %s", cap_exc)

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
        raw_keys: list[Any] = []
        scan_fn = getattr(redis_client, "scan_iter", None)
        keys_fn = getattr(redis_client, "keys", None)
        if callable(scan_fn) and "keys" not in getattr(redis_client, "__dict__", {}):
            async for k in scan_fn(match="nce:ns:*:salience_buffer", count=100):
                raw_keys.append(k)
        else:
            if keys_fn is None:
                return 0
            fetched_keys = keys_fn("nce:ns:*:salience_buffer")
            if hasattr(fetched_keys, "__await__"):
                fetched_keys = await fetched_keys
            raw_keys = list(fetched_keys)

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

            # Successfully persisted — subtract the exact flushed deltas so
            # concurrent HINCRBYFLOAT increments arriving during PG reinforce
            # are preserved rather than deleted.
            import redis.asyncio as _aioredis

            use_lua_subtract = (
                isinstance(redis_client, _aioredis.Redis)
                and callable(getattr(redis_client, "eval", None))
                and "hdel" not in getattr(redis_client, "__dict__", {})
            )
            if use_lua_subtract and items_to_process:
                lua_args: list[Any] = []
                for field_raw, (_mem_id, _agent, delta) in zip(fields_to_delete, items_to_process):
                    f_key = (
                        field_raw.decode("utf-8")
                        if isinstance(field_raw, bytes)
                        else str(field_raw)
                    )
                    lua_args.extend((f_key, str(delta)))
                eval_res = redis_client.eval(_SUBTRACT_FLUSHED_LUA, 1, redis_key, *lua_args)
                if hasattr(eval_res, "__await__"):
                    await eval_res
            else:
                hdel_fn = getattr(redis_client, "hdel", None)
                if hdel_fn is not None and fields_to_delete:
                    del_res = hdel_fn(redis_key, *fields_to_delete)
                    if hasattr(del_res, "__await__"):
                        await del_res

            # Keep accompanying order list pruned and TTLs refreshed cleanly.
            order_key = f"nce:ns:{ns}:{_SALIENCE_ORDER_SUBKEY}"
            try:
                lrem_fn = getattr(redis_client, "lrem", None)
                if callable(lrem_fn) and fields_to_delete:
                    for f_raw in fields_to_delete:
                        f_str = f_raw.decode("utf-8") if isinstance(f_raw, bytes) else str(f_raw)
                        await _maybe_await(lrem_fn(order_key, 0, f_str))
                expire_fn = getattr(redis_client, "expire", None)
                if callable(expire_fn):
                    await _maybe_await(expire_fn(redis_key, _SALIENCE_BUFFER_TTL_SEC))
                    await _maybe_await(expire_fn(order_key, _SALIENCE_BUFFER_TTL_SEC))
            except Exception:
                pass

            total_flushed += len(items_to_process)
        except Exception as exc:
            log.warning("Failed to flush salience buffer for namespace %s: %s", ns, exc)

    return total_flushed
