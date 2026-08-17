"""
Phase 3.2 — Namespace / agent resource quotas (asyncpg + optional Redis hot path).

Only namespaces (and optional per-agent rows) that have rows in ``resource_quotas``
are enforced. When ``NCE_QUOTA_REDIS_COUNTERS`` is true and a Redis client is
passed, increments use atomic Redis operations to avoid serializing writers on
``resource_quotas``; PostgreSQL ``used_amount`` is updated periodically via
:func:`flush_quota_counters_to_postgres`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg
from asyncpg.exceptions import IntegrityConstraintViolationError

from nce.config import cfg
from nce.observability import QUOTA_CONSUMED, QUOTA_REMAINING

log = logging.getLogger("nce.quotas")

RESOURCE_LLM_TOKENS = "llm_tokens"
RESOURCE_STORAGE_BYTES = "storage_bytes"
RESOURCE_MEMORY_COUNT = "memory_count"

# Redis mirror for per-row used counters (namespace_id + quota row id in key — RLS-safe flush).
QUOTA_REDIS_KEY_PREFIX = "nce:quota:used:"

_MAX_QUOTA_DELTA: int = 10_000_000
_REDIS_CALL_TIMEOUT_S: float = 2.0
_QUOTA_REDIS_KEY_TTL_S: int = 86_400 * 7  # 7 days — well beyond flush interval

_QUOTA_INCR_LUA = """
local cur = redis.call('GET', KEYS[1])
local base = tonumber(ARGV[1])
local delta = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local v
if cur == false then
  v = base
else
  v = tonumber(cur)
end
local newv = v + delta
if newv > limit then
  return -1
end
local ttl = tonumber(ARGV[4])
if ttl and ttl > 0 then
  redis.call('SET', KEYS[1], newv, 'EX', ttl)
else
  redis.call('SET', KEYS[1], newv)
end
return newv
"""


def quota_redis_key(namespace_id: UUID, quota_row_id: UUID) -> str:
    return f"{QUOTA_REDIS_KEY_PREFIX}{namespace_id}:{quota_row_id}"


def _parse_quota_redis_key(key: str) -> tuple[UUID, UUID] | None:
    if not key.startswith(QUOTA_REDIS_KEY_PREFIX):
        return None
    rest = key[len(QUOTA_REDIS_KEY_PREFIX) :]
    try:
        ns_str, qid_str = rest.rsplit(":", 1)
        return UUID(ns_str), UUID(qid_str)
    except ValueError:
        return None


def _quota_incr_failed(result: Any) -> bool:
    if result is None:
        return True
    if isinstance(result, (int, float)):
        return int(result) == -1
    s = result.decode() if isinstance(result, bytes) else str(result)
    return s.strip() == "-1"


class QuotaExceededError(ValueError):
    """Raised when a quota row would be exceeded (maps to MCP / HTTP client errors)."""


@dataclass
class QuotaReservation:
    """Tracks applied increments for best-effort rollback on downstream failure."""

    pool: asyncpg.Pool | None
    redis_client: Any | None = None
    namespace_id: UUID | None = None
    steps: list[tuple[UUID, int]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.steps

    async def rollback(self) -> None:
        if self.redis_client is not None and self.namespace_id is not None and self.steps:
            for qid, delta in reversed(self.steps):
                try:
                    await self.redis_client.decrby(quota_redis_key(self.namespace_id, qid), delta)
                except Exception:
                    log.error(
                        "Quota rollback step failed — Redis DECRBY for quota_id=%s delta=%d "
                        "namespace=%s. Counter may be over-counted.",
                        qid,
                        delta,
                        self.namespace_id,
                    )
            self.steps.clear()
            return
        if not self.steps or self.pool is None:
            self.steps.clear()
            return
        async with self.pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                for qid, delta in self.steps:
                    await conn.execute(
                        """
                        UPDATE resource_quotas
                        SET used_amount = GREATEST(0, used_amount - $1),
                            updated_at = now()
                        WHERE id = $2
                        """,
                        delta,
                        qid,
                    )
        self.steps.clear()


def null_reservation() -> QuotaReservation:
    return QuotaReservation(pool=None, redis_client=None, namespace_id=None, steps=[])


def estimate_llm_tokens(*texts: str | None) -> int:
    div = max(1, int(cfg.NCE_QUOTA_TOKEN_ESTIMATE_DIVISOR))
    total_chars = sum(len(t or "") for t in texts)
    return max(1, total_chars // div)


def estimate_storage_bytes(*texts: str | None) -> int:
    return sum(len((t or "").encode("utf-8")) for t in texts)


async def _consume_resources_redis(
    pool: asyncpg.Pool,
    redis_client: Any,
    *,
    namespace_id: UUID,
    agent_id: str | None,
    amounts: Mapping[str, int],
) -> QuotaReservation:
    reservation = QuotaReservation(
        pool=pool,
        redis_client=redis_client,
        namespace_id=namespace_id,
    )
    deltas: dict[str, int] = {}
    for k, v in amounts.items():
        d = int(v)
        if d <= 0:
            continue
        if d > _MAX_QUOTA_DELTA:
            raise ValueError(f"Quota delta for {k!r} exceeds maximum ({d} > {_MAX_QUOTA_DELTA})")
        deltas[k] = d
    applied: list[tuple[UUID, int]] = []

    async def _rollback_partial() -> None:
        for qid, delta in reversed(applied):
            try:
                await redis_client.decrby(quota_redis_key(namespace_id, qid), delta)
            except Exception:
                log.error(
                    "Partial quota rollback failed for quota_id=%s delta=%d namespace=%s",
                    qid,
                    delta,
                    namespace_id,
                )

    try:
        async with pool.acquire(timeout=10.0) as conn:
            for resource_type, delta in sorted(deltas.items()):
                rows = await conn.fetch(
                    """
                    SELECT id, agent_id, used_amount, limit_amount
                    FROM resource_quotas
                    WHERE namespace_id = $1
                      AND resource_type = $2
                      AND (reset_at IS NULL OR reset_at > now())
                      AND (
                          agent_id IS NULL
                          OR ($3::text IS NOT NULL AND agent_id = $3)
                      )
                    ORDER BY (agent_id IS NULL) DESC
                    """,
                    namespace_id,
                    resource_type,
                    agent_id,
                )
                if not rows:
                    continue
                for row in rows:
                    qid = row["id"]
                    pg_used = int(row["used_amount"])
                    lim = int(row["limit_amount"])
                    key = quota_redis_key(namespace_id, qid)
                    res = await asyncio.wait_for(
                        redis_client.eval(
                            _QUOTA_INCR_LUA,
                            1,
                            key,
                            str(pg_used),
                            str(delta),
                            str(lim),
                            str(_QUOTA_REDIS_KEY_TTL_S),
                        ),
                        timeout=_REDIS_CALL_TIMEOUT_S,
                    )
                    if _quota_incr_failed(res):
                        await _rollback_partial()
                        applied.clear()
                        reservation.steps.clear()
                        scope = "namespace" if row["agent_id"] is None else "agent"
                        raise QuotaExceededError(
                            f"Quota exceeded for namespace={namespace_id} "
                            f"resource={resource_type!r} ({scope} limit)"
                        )
                    applied.append((qid, delta))
                    reservation.steps.append((qid, delta))

                    # Update gauges (Batch 19)
                    used = int(res)
                    remaining = max(0, lim - used)
                    ns_str = str(namespace_id)
                    aid_str = str(row["agent_id"] or "global")
                    QUOTA_CONSUMED.labels(
                        namespace_id=ns_str,
                        resource_type=resource_type,
                        agent_id=aid_str,
                    ).set(used)
                    QUOTA_REMAINING.labels(
                        namespace_id=ns_str,
                        resource_type=resource_type,
                        agent_id=aid_str,
                    ).set(remaining)
    except QuotaExceededError:
        raise
    except Exception:
        await _rollback_partial()
        applied.clear()
        reservation.steps.clear()
        raise

    return reservation


async def consume_resources(
    pool: asyncpg.Pool,
    *,
    namespace_id: UUID,
    agent_id: str | None,
    amounts: Mapping[str, int],
    redis_client: Any | None = None,
) -> QuotaReservation:
    """
    Atomically increment counters for each (resource_type -> delta).

    Skips resource types with delta <= 0. If no quota rows match, returns an empty
    reservation without error (operators opt-in by inserting limits).
    """
    if not cfg.NCE_QUOTAS_ENABLED:
        return null_reservation()

    deltas: dict[str, int] = {}
    for k, v in amounts.items():
        d = int(v)
        if d <= 0:
            continue
        if d > _MAX_QUOTA_DELTA:
            raise ValueError(f"Quota delta for {k!r} exceeds maximum ({d} > {_MAX_QUOTA_DELTA})")
        deltas[k] = d
    if not deltas:
        return QuotaReservation(pool=pool, steps=[])

    use_redis = bool(cfg.NCE_QUOTA_REDIS_COUNTERS and redis_client is not None)
    if use_redis:
        return await _consume_resources_redis(
            pool,
            redis_client,
            namespace_id=namespace_id,
            agent_id=agent_id,
            amounts=amounts,
        )

    reservation = QuotaReservation(pool=pool)
    async with pool.acquire(timeout=10.0) as conn:
        async with conn.transaction():
            try:
                for resource_type, delta in sorted(deltas.items()):
                    rows = await conn.fetch(
                        """
                        SELECT id, agent_id
                        FROM resource_quotas
                        WHERE namespace_id = $1
                          AND resource_type = $2
                          AND (reset_at IS NULL OR reset_at > now())
                          AND (
                              agent_id IS NULL
                              OR ($3::text IS NOT NULL AND agent_id = $3)
                          )
                        ORDER BY (agent_id IS NULL) DESC
                        FOR UPDATE
                        """,
                        namespace_id,
                        resource_type,
                        agent_id,
                    )
                    if not rows:
                        continue
                    for row in rows:
                        upd = await conn.fetchrow(
                            """
                            UPDATE resource_quotas
                            SET used_amount = used_amount + $1,
                                updated_at = now()
                            WHERE id = $2
                              AND used_amount + $1 <= limit_amount
                            RETURNING id, used_amount, limit_amount
                            """,
                            delta,
                            row["id"],
                        )
                        if upd is None:
                            scope = "namespace" if row["agent_id"] is None else "agent"
                            raise QuotaExceededError(
                                f"Quota exceeded for namespace={namespace_id} "
                                f"resource={resource_type!r} ({scope} limit)"
                            )
                        reservation.steps.append((row["id"], delta))

                        # Update gauges (Batch 19)
                        if "used_amount" in upd and "limit_amount" in upd:
                            used = int(upd["used_amount"])
                            lim = int(upd["limit_amount"])
                            remaining = max(0, lim - used)
                            ns_str = str(namespace_id)
                            aid_str = str(row["agent_id"] or "global")
                            QUOTA_CONSUMED.labels(
                                namespace_id=ns_str,
                                resource_type=resource_type,
                                agent_id=aid_str,
                            ).set(used)
                            QUOTA_REMAINING.labels(
                                namespace_id=ns_str,
                                resource_type=resource_type,
                                agent_id=aid_str,
                            ).set(remaining)
            except IntegrityConstraintViolationError as e:
                raise QuotaExceededError(
                    f"Quota integrity constraint violated for namespace={namespace_id}: {e}"
                ) from e

    return reservation


async def flush_quota_counters_to_postgres(redis_client: Any, pool: asyncpg.Pool) -> int:
    """Scan Redis quota keys and persist ``used_amount`` under RLS-scoped sessions."""
    if redis_client is None:
        return 0
    from nce.db_utils import scoped_pg_session

    flushed = 0
    async for key_b in redis_client.scan_iter(match=f"{QUOTA_REDIS_KEY_PREFIX}*"):
        key = key_b.decode() if isinstance(key_b, bytes) else key_b
        parsed = _parse_quota_redis_key(key)
        if parsed is None:
            continue
        ns_id, qid = parsed
        raw = await redis_client.get(key_b)
        if raw is None:
            continue
        used_s = raw.decode() if isinstance(raw, bytes) else raw
        try:
            used = int(used_s)
        except ValueError:
            continue
        try:
            async with scoped_pg_session(pool, ns_id) as conn:
                await conn.execute(
                    """
                    UPDATE resource_quotas
                    SET used_amount = GREATEST(used_amount, $1),
                        updated_at = now()
                    WHERE id = $2
                    """,
                    used,
                    qid,
                )
            flushed += 1
        except Exception:
            log.exception(
                "flush_quota_counters_to_postgres failed for ns=%s quota_id=%s",
                ns_id,
                qid,
            )
    return flushed


async def quota_redis_flush_loop(redis_client: Any, pool: asyncpg.Pool) -> None:
    """Background task: periodically sync Redis quota mirrors to PostgreSQL."""
    while True:
        try:
            await asyncio.sleep(cfg.NCE_QUOTA_REDIS_FLUSH_INTERVAL_S)
            if cfg.NCE_QUOTAS_ENABLED and cfg.NCE_QUOTA_REDIS_COUNTERS and redis_client is not None:
                await flush_quota_counters_to_postgres(redis_client, pool)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("quota_redis_flush_loop iteration failed")


async def delete_quota_redis_counter(
    redis_client: Any, namespace_id: UUID, quota_row_id: UUID
) -> None:
    """Remove Redis mirror for a quota row (e.g. after admin reset)."""
    if redis_client is None:
        return
    await redis_client.delete(quota_redis_key(namespace_id, quota_row_id))


def _store_memory_cost(arguments: dict[str, Any]) -> dict[str, int]:
    c = arguments.get("content")
    summary = arguments.get("summary")
    heavy = arguments.get("heavy_payload")
    tok = estimate_llm_tokens(c, summary, heavy)
    return {
        RESOURCE_LLM_TOKENS: max(1, tok * 3),
        RESOURCE_STORAGE_BYTES: estimate_storage_bytes(c, summary, heavy),
        RESOURCE_MEMORY_COUNT: 1,
    }


def _search_cost(arguments: dict[str, Any]) -> dict[str, int]:
    q = arguments.get("query") or ""
    return {RESOURCE_LLM_TOKENS: estimate_llm_tokens(q) + 50}


def _fixed_cost(tokens: int) -> Callable[[dict[str, Any]], dict[str, int]]:
    return lambda args: {RESOURCE_LLM_TOKENS: tokens}


# Registry of cost estimators per tool_name
_TOOL_COST_REGISTRY: dict[str, Callable[[dict[str, Any]], dict[str, int]]] = {
    "store_memory": _store_memory_cost,
    "semantic_search": _search_cost,
    "api_semantic_search": _search_cost,
    "a2a_query_shared": _search_cost,
    "boost_memory": _fixed_cost(64),
    "forget_memory": _fixed_cost(64),
    "unredact_memory": _fixed_cost(64),
    "list_contradictions": _fixed_cost(128),
}


def tool_quota_plan(
    tool_name: str, arguments: dict[str, Any]
) -> tuple[UUID, str | None, dict[str, int]] | None:
    """
    Map MCP / HTTP tool name + arguments to (namespace_id, agent_id, amounts).

    ``agent_id`` may be None to enforce only namespace-wide rows.
    Returns None when the tool should not touch quotas.
    """
    estimator = _TOOL_COST_REGISTRY.get(tool_name)
    if estimator is None:
        return None

    # Resolve namespace_id
    ns_key = "consumer_namespace_id" if tool_name == "a2a_query_shared" else "namespace_id"
    if ns_key not in arguments:
        return None
    ns = UUID(str(arguments[ns_key]))

    # Resolve agent_id
    if tool_name == "a2a_query_shared":
        agent: str | None = str(arguments.get("consumer_agent_id") or "default")
    elif tool_name == "list_contradictions":
        aid = arguments.get("agent_id")
        agent = str(aid) if aid else None
    else:
        agent = str(arguments.get("agent_id") or "default")

    # Compute amounts
    amounts = estimator(arguments)
    return ns, agent, amounts


async def consume_for_tool(
    pool: asyncpg.Pool,
    tool_name: str,
    arguments: dict[str, Any],
    redis_client: Any | None = None,
) -> QuotaReservation:
    if not cfg.NCE_QUOTAS_ENABLED:
        return null_reservation()
    plan = tool_quota_plan(tool_name, arguments)
    if plan is None:
        return null_reservation()
    ns, agent, amounts = plan
    return await consume_resources(
        pool,
        namespace_id=ns,
        agent_id=agent,
        amounts=amounts,
        redis_client=redis_client,
    )
