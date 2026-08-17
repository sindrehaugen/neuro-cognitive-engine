"""
Admin REST API helpers — bounded pagination, filter validation.

Used by ``admin_server`` list endpoints so query handling is tested and reused
without duplicating clamps / allowlists.

Security notes
--------------
- Integers are bounded to prevent abusive OFFSET/LIMIT workloads.
- ``event_type``, ``slug_prefix``, ``resource_type``, and DLQ ``status`` use
  allowlists or tight length caps so attackers cannot spray huge predicates.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from nce.salience import compute_decayed_score

# Shared limits — keep aligned across handlers
ADMIN_MAX_PAGE_NUMBER: int = 10_000
ADMIN_MAX_LIST_LIMIT: int = 200
ADMIN_DEFAULT_LIMIT: int = 50
ADMIN_NAMESPACES_DEFAULT_LIMIT: int = 500

ADMIN_SALIENCE_MAP_DEFAULT_K: int = 500
ADMIN_SALIENCE_MAP_MAX_K: int = 2000

# Tables surfaced to the Fleet UI for row-level security reporting.
ADMIN_FLEET_RLS_TABLES: tuple[str, ...] = (
    "memories",
    "memory_salience",
    "event_log",
    "contradictions",
    "snapshots",
    "kg_nodes",
    "kg_edges",
    "pii_redactions",
    "replay_runs",
    "dead_letter_queue",
    "resource_quotas",
    "a2a_grants",
    "processed_outbox_events",
    "actor_trust",
    "event_parents",
    "action_approval_queue",
    "action_idempotency",
)

ADMIN_MAX_EVENT_TYPE_LEN: int = 128
ADMIN_MAX_RESOURCE_TYPE_LEN: int = 128

# Caps how deep OFFSET-based admin lists may scan (anti-DoS).
ADMIN_MAX_ROWS_SKIP: int = 500_000

_SLUG_PREFIX_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_DLQ_ALLOWED_STATUS: frozenset[str] = frozenset({"pending", "replayed", "purged"})


def parse_optional_uuid(raw: str | None) -> uuid.UUID | None:
    """Return UUID from *raw*, or ``None`` if absent/blank."""
    if raw is None or raw == "":
        return None
    return uuid.UUID(raw)


def clamp_bounded_int(raw: str | None, *, default: int, min_value: int, max_value: int) -> int:
    """Parse optional query integer and clamp to ``[min_value, max_value]``."""
    if raw is None or raw == "":
        return default
    value = int(raw)
    return max(min_value, min(max_value, value))


def parse_page_limit_common(
    params: Mapping[str, str],
    *,
    default_limit: int = ADMIN_DEFAULT_LIMIT,
    max_limit: int = ADMIN_MAX_LIST_LIMIT,
) -> tuple[int, int]:
    """
    Return ``(page, limit)`` from ``page`` and ``limit`` query params.

    Raises
    ------
    ValueError
        If inputs are non-numeric.
    """
    page = clamp_bounded_int(
        params.get("page"),
        default=1,
        min_value=1,
        max_value=ADMIN_MAX_PAGE_NUMBER,
    )
    limit = clamp_bounded_int(
        params.get("limit"),
        default=default_limit,
        min_value=1,
        max_value=max_limit,
    )
    return page, limit


def sanitize_event_type_filter(raw: str | None) -> tuple[str | None, str | None]:
    """Return ``(event_type, error_message)`` with length cap; trimmed."""
    if raw is None or raw == "":
        return None, None
    trimmed = raw.strip()
    if not trimmed:
        return None, None
    if len(trimmed) > ADMIN_MAX_EVENT_TYPE_LEN:
        return None, (f"event_type must be ≤{ADMIN_MAX_EVENT_TYPE_LEN} characters")
    return trimmed, None


def sanitize_slug_prefix_filter(raw: str | None) -> tuple[str | None, str | None]:
    """Return ``(slug_prefix, error_message)`` for namespaces ILIKE prefix."""
    if raw is None or raw == "":
        return None, None
    trimmed = raw.strip()
    if not trimmed:
        return None, None
    if len(trimmed) > 64:
        return None, "slug_prefix must be ≤64 characters"
    if not _SLUG_PREFIX_RE.fullmatch(trimmed):
        return None, "slug_prefix may only contain [a-zA-Z0-9_-]"
    return trimmed, None


def sanitize_resource_type_filter(raw: str | None) -> tuple[str | None, str | None]:
    if raw is None or raw == "":
        return None, None
    trimmed = raw.strip()
    if not trimmed:
        return None, None
    if len(trimmed) > ADMIN_MAX_RESOURCE_TYPE_LEN:
        return None, (f"resource_type must be ≤{ADMIN_MAX_RESOURCE_TYPE_LEN} characters")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", trimmed):
        return None, ("resource_type may only contain letters, digits, ._-")
    return trimmed, None


def validate_dlq_status(raw: str | None) -> tuple[str | None, str | None]:
    """Validate optional DLQ status filter."""
    if raw is None or raw == "":
        return None, None
    s = raw.strip()
    if s not in _DLQ_ALLOWED_STATUS:
        return (
            None,
            "status must be one of: pending | replayed | purged",
        )
    return s, None


def sanitize_task_name_filter(raw: str | None) -> tuple[str | None, str | None]:
    """Tight optional filter for RQ/task function names in admin DLQ queries."""
    if raw is None or raw == "":
        return None, None
    trimmed = raw.strip()
    if not trimmed:
        return None, None
    if len(trimmed) > 128:
        return None, "task_name must be ≤128 characters"
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", trimmed):
        return None, "task_name may only contain letters, digits, ._-"
    return trimmed, None


def offset_from_page_limit(page: int, limit: int) -> int:
    """Return OFFSET for zero-based paging; raises ``ValueError`` if too deep."""
    if page < 1 or limit < 1:
        raise ValueError("page and limit must be positive")
    off = (page - 1) * limit
    if off > ADMIN_MAX_ROWS_SKIP:
        raise ValueError(
            f"pagination window exceeds maximum (page-1)*limit ≤ {ADMIN_MAX_ROWS_SKIP}"
        )
    return off


def parse_optional_bigint_bounds(raw: str | None, *, label: str) -> tuple[int | None, str | None]:
    """Optional BIGINT filter (event_seq ranges). Bounds to postgres bigint range-ish."""
    if raw is None or raw == "":
        return None, None
    try:
        val = int(raw)
    except ValueError:
        return None, f"{label} must be an integer"
    if val < -(2**62) or val > 2**62 - 1:
        return None, f"{label} out of allowed range"
    return val, None


def parse_salience_top_k(raw: str | None) -> tuple[int, str | None]:
    """Clamp salience-map ``top_k`` to a safe range."""
    try:
        k = clamp_bounded_int(
            raw,
            default=ADMIN_SALIENCE_MAP_DEFAULT_K,
            min_value=1,
            max_value=ADMIN_SALIENCE_MAP_MAX_K,
        )
        return k, None
    except ValueError:
        return ADMIN_SALIENCE_MAP_DEFAULT_K, "top_k must be an integer"


def sanitize_optional_agent_filter(raw: str | None) -> tuple[str | None, str | None]:
    """Optional agent_id substring filter for cognitive admin queries."""
    if raw is None or raw == "":
        return None, None
    s = raw.strip()
    if not s:
        return None, None
    if len(s) > 256:
        return None, "agent_id must be ≤256 characters"
    if not re.fullmatch(r"[a-zA-Z0-9:_./+-]+", s):
        return None, ("agent_id may only contain letters, digits, and : _ . / + -")
    return s, None


def parse_optional_half_life_days(
    raw: str | None,
    *,
    default: float = 30.0,
    minimum: float = 0.05,
    maximum: float = 730.0,
) -> tuple[float, str | None]:
    """Clamp optional decay half-life (days) for admin cognitive queries."""
    if raw is None or raw.strip() == "":
        return default, None
    try:
        v = float(raw)
    except ValueError:
        return default, "half_life_days must be numeric"
    if math.isnan(v) or math.isinf(v):
        return default, "half_life_days must be finite"
    return max(minimum, min(maximum, v)), None


async def fetch_pg_rls_snapshot(
    conn: asyncpg.Connection, tables: Iterable[str] | None = None
) -> dict[str, bool]:
    """Return ``relrowsecurity`` flags for curated tenant tables (public schema)."""
    names = tuple(tables) if tables else ADMIN_FLEET_RLS_TABLES
    rows = await conn.fetch(
        """
        SELECT c.relname AS table_name, c.relrowsecurity AS rls_enabled
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p')
          AND c.relname = ANY($1::text[])
        """,
        list(names),
    )
    found = {str(r["table_name"]): bool(r["rls_enabled"]) for r in rows}
    # Tables missing from pg_catalog still appear as False for deterministic JSON.
    return {t: found.get(t, False) for t in names}


async def fetch_salience_map_points(
    conn: asyncpg.Connection,
    *,
    namespace_id: uuid.UUID,
    agent_id: str | None,
    top_k: int,
    half_life_days: float,
) -> list[dict[str, Any]]:
    """
    Return decay-map points: age (days since memory ``created_at``) vs decayed
    salience, coloured upstream by ``assertion_type``.

    Uses a bounded union: low-salience half + high-salience half of the
    namespace slice so the scatter plot exposes the decay wedge without a
    full-table sort.
    """
    half = max(1, top_k // 2)
    dup_cap = min(top_k, half * 2)

    union_sql = f"""
        (
            SELECT memory_id, agent_id, salience_score, updated_at
            FROM memory_salience
            WHERE namespace_id = $1
              AND ($2::text IS NULL OR agent_id = $2)
            ORDER BY salience_score ASC, updated_at ASC
            LIMIT {half}
        )
        UNION ALL
        (
            SELECT memory_id, agent_id, salience_score, updated_at
            FROM memory_salience
            WHERE namespace_id = $1
              AND ($2::text IS NULL OR agent_id = $2)
            ORDER BY salience_score DESC, updated_at DESC
            LIMIT {half}
        )
    """
    rows = await conn.fetch(union_sql, namespace_id, agent_id)
    seen: set[tuple[uuid.UUID, str]] = set()
    uniq: list[asyncpg.Record] = []
    for r in rows:
        key = (r["memory_id"], r["agent_id"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
        if len(uniq) >= dup_cap:
            break

    if not uniq:
        return []

    mem_ids = [r["memory_id"] for r in uniq]
    mem_rows = await conn.fetch(
        """
        SELECT DISTINCT ON (id)
            id,
            assertion_type,
            memory_type,
            created_at
        FROM memories
        WHERE id = ANY($1::uuid[])
        ORDER BY id, created_at DESC
        """,
        mem_ids,
    )
    mem_meta: dict[uuid.UUID, dict[str, Any]] = {}
    for m in mem_rows:
        mid = m["id"]
        mem_meta[mid] = {
            "assertion_type": m["assertion_type"],
            "memory_type": m["memory_type"],
            "created_at": m["created_at"],
        }

    ref = datetime.now(timezone.utc)
    points: list[dict[str, Any]] = []
    for r in uniq:
        mid = r["memory_id"]
        meta = mem_meta.get(mid)
        created = meta["created_at"] if meta else r["updated_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (ref - created).total_seconds() / 86400.0)
        decayed = compute_decayed_score(
            float(r["salience_score"]),
            r["updated_at"],
            half_life_days,
            now=ref,
            memory_id=str(mid),
        )
        points.append(
            {
                "memory_id": str(mid),
                "agent_id": r["agent_id"],
                "raw_salience": float(r["salience_score"]),
                "decayed_salience": decayed,
                "salience_updated_at": r["updated_at"].astimezone(timezone.utc).isoformat(),
                "age_days": age_days,
                "assertion_type": (meta or {}).get("assertion_type"),
                "memory_type": (meta or {}).get("memory_type"),
                "memory_created_at": created.astimezone(timezone.utc).isoformat(),
            }
        )
    return points


async def fetch_event_llm_payload_uri(
    conn: asyncpg.Connection,
    *,
    namespace_id: uuid.UUID,
    event_id: uuid.UUID,
) -> tuple[str | None, str | None]:
    """
    Return ``(llm_payload_uri, error)`` for a signed event in the namespace.
    """
    row = await conn.fetchrow(
        """
        SELECT llm_payload_uri
        FROM event_log
        WHERE id = $1 AND namespace_id = $2
        LIMIT 1
        """,
        event_id,
        namespace_id,
    )
    if row is None:
        return None, "event not found for namespace"
    uri = row["llm_payload_uri"]
    if not uri:
        return None, "event has no llm_payload_uri"
    return str(uri), None


async def fetch_fleet_overview_page(
    conn: asyncpg.Connection,
    *,
    slug_prefix: str | None,
    page: int,
    limit: int,
    half_life_days: float,
) -> tuple[list[dict[str, Any]], int]:
    """
    One page of fleet rows with correlated aggregates (namespace-scoped only).

    Correlated subqueries avoid global ``GROUP BY`` scans across the whole
    ``memories`` / ``memory_salience`` tables; each aggregate is limited to the
    visible namespace IDs on this page.

    Supporting ``namespace_id`` btree indexes live in ``nce/schema.sql``:
    ``memories``, ``memory_salience``, ``contradictions``, ``consolidation_runs``,
    ``bridge_subscriptions``.
    """
    clauses: list[str] = []
    args: list[object] = []
    next_bind = 1
    if slug_prefix is not None:
        clauses.append(f"slug ILIKE ${next_bind}")
        args.append(slug_prefix + "%")
        next_bind += 1
    where_ns = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    count_sql = f"SELECT COUNT(*)::bigint AS total FROM namespaces {where_ns}"

    hl = float(half_life_days)
    hl_bind = next_bind
    limit_bind = next_bind + 1
    offset_bind = next_bind + 2
    items_sql = f"""
        SELECT
            n.id AS namespace_id,
            n.slug,
            (
                SELECT COUNT(*)::bigint FROM memories m
                WHERE m.namespace_id = n.id
            ) AS memory_count,
            (
                SELECT percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY nce_decayed_score(
                        ms.salience_score, ms.updated_at, ${hl_bind}::float
                    )
                )
                FROM memory_salience ms
                WHERE ms.namespace_id = n.id
            ) AS salience_p50,
            (
                SELECT COUNT(*)::bigint FROM contradictions c
                WHERE c.namespace_id = n.id AND c.resolution IS NULL
            ) AS open_contradictions,
            (
                SELECT cr.status FROM consolidation_runs cr
                WHERE cr.namespace_id = n.id
                ORDER BY COALESCE(cr.finished_at, cr.started_at) DESC NULLS LAST
                LIMIT 1
            ) AS consolidation_last_status,
            (
                SELECT cr.finished_at FROM consolidation_runs cr
                WHERE cr.namespace_id = n.id
                ORDER BY cr.finished_at DESC NULLS LAST
                LIMIT 1
            ) AS consolidation_last_finished_at,
            (
                SELECT COALESCE(
                    jsonb_agg(
                        jsonb_build_object(
                            'resource_type', rq.resource_type,
                            'agent_id', rq.agent_id,
                            'used_amount', rq.used_amount,
                            'limit_amount', rq.limit_amount
                        )
                    ),
                    '[]'::jsonb
                )
                FROM resource_quotas rq
                WHERE rq.namespace_id = n.id
            ) AS quota_entries,
            (
                SELECT COUNT(*)::bigint FROM bridge_subscriptions b
                WHERE b.namespace_id = n.id AND b.status = 'ACTIVE'
            ) AS bridge_active_count,
            (
                SELECT MIN(b.expires_at) FROM bridge_subscriptions b
                WHERE b.namespace_id = n.id
                  AND b.status = 'ACTIVE'
                  AND b.expires_at IS NOT NULL
            ) AS bridge_next_expiry,
            (
                SELECT MAX(e.occurred_at) FROM event_log e
                WHERE e.namespace_id = n.id
            ) AS last_event_at,
            ARRAY[
                (
                    SELECT COUNT(*)::bigint FROM memories m
                    WHERE m.namespace_id = n.id
                      AND m.created_at >= NOW() - INTERVAL '7 days'
                      AND m.created_at < NOW() - INTERVAL '6 days'
                ),
                (
                    SELECT COUNT(*)::bigint FROM memories m
                    WHERE m.namespace_id = n.id
                      AND m.created_at >= NOW() - INTERVAL '6 days'
                      AND m.created_at < NOW() - INTERVAL '5 days'
                ),
                (
                    SELECT COUNT(*)::bigint FROM memories m
                    WHERE m.namespace_id = n.id
                      AND m.created_at >= NOW() - INTERVAL '5 days'
                      AND m.created_at < NOW() - INTERVAL '4 days'
                ),
                (
                    SELECT COUNT(*)::bigint FROM memories m
                    WHERE m.namespace_id = n.id
                      AND m.created_at >= NOW() - INTERVAL '4 days'
                      AND m.created_at < NOW() - INTERVAL '3 days'
                ),
                (
                    SELECT COUNT(*)::bigint FROM memories m
                    WHERE m.namespace_id = n.id
                      AND m.created_at >= NOW() - INTERVAL '3 days'
                      AND m.created_at < NOW() - INTERVAL '2 days'
                ),
                (
                    SELECT COUNT(*)::bigint FROM memories m
                    WHERE m.namespace_id = n.id
                      AND m.created_at >= NOW() - INTERVAL '2 days'
                      AND m.created_at < NOW() - INTERVAL '1 day'
                ),
                (
                    SELECT COUNT(*)::bigint FROM memories m
                    WHERE m.namespace_id = n.id
                      AND m.created_at >= NOW() - INTERVAL '1 day'
                      AND m.created_at < NOW()
                )
            ]::bigint[] AS memory_velocity_7d
        FROM namespaces n
        {where_ns}
        ORDER BY n.slug ASC
        LIMIT ${limit_bind} OFFSET ${offset_bind}
    """

    total_row = await conn.fetchrow(count_sql, *args)
    total = int(total_row["total"]) if total_row else 0
    offset = offset_from_page_limit(page, limit)
    rows = await conn.fetch(items_sql, *args, hl, limit, offset)

    ref = datetime.now(timezone.utc)
    staleness = timedelta(days=7)
    items: list[dict[str, Any]] = []
    for r in rows:
        last_ev: datetime | None = r["last_event_at"]
        if last_ev is None:
            evt_status = "quiet"
        elif last_ev.tzinfo is None:
            last_ev = last_ev.replace(tzinfo=timezone.utc)
            evt_status = "ok" if (ref - last_ev) <= staleness else "stale"
        else:
            evt_status = "ok" if (ref - last_ev) <= staleness else "stale"

        mem_c = int(r["memory_count"] or 0)
        vel_raw = r["memory_velocity_7d"]
        if isinstance(vel_raw, list):
            velocity_7d = [int(v or 0) for v in vel_raw]
        else:
            velocity_7d = [0] * 7

        hull = {
            "event_feed": evt_status,
            "last_event_at": (last_ev.astimezone(timezone.utc).isoformat() if last_ev else None),
            "memory_volume": mem_c,
        }

        quota_entries_raw = r["quota_entries"]
        quota_list: list[Any]
        if isinstance(quota_entries_raw, str):
            quota_list = json.loads(quota_entries_raw)
        elif quota_entries_raw is None:
            quota_list = []
        elif isinstance(quota_entries_raw, list):
            quota_list = quota_entries_raw
        else:
            quota_list = list(quota_entries_raw)

        items.append(
            {
                "namespace_id": str(r["namespace_id"]),
                "slug": r["slug"],
                "health": hull,
                "memory_count": mem_c,
                "memory_velocity_7d": velocity_7d,
                "salience_p50": (
                    float(r["salience_p50"]) if r["salience_p50"] is not None else None
                ),
                "open_contradictions": int(r["open_contradictions"] or 0),
                "consolidation": {
                    "last_status": r["consolidation_last_status"],
                    "last_finished_at": (
                        r["consolidation_last_finished_at"].astimezone(timezone.utc).isoformat()
                        if r["consolidation_last_finished_at"]
                        else None
                    ),
                },
                "quota": {"entries": quota_list},
                "bridges": {
                    "active_count": int(r["bridge_active_count"] or 0),
                    "next_expires_at": (
                        r["bridge_next_expiry"].astimezone(timezone.utc).isoformat()
                        if r["bridge_next_expiry"]
                        else None
                    ),
                },
            }
        )
    return items, total


async def fetch_recent_open_contradictions(
    conn: asyncpg.Connection,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Latest unresolved contradictions across namespaces (admin pool bypasses RLS)."""
    lim = max(1, min(int(limit), 50))
    rows = await conn.fetch(
        """
        SELECT
            c.id,
            c.namespace_id,
            n.slug AS namespace_slug,
            c.detected_at,
            c.confidence,
            c.detection_path,
            c.memory_a_id,
            c.memory_b_id
        FROM contradictions c
        INNER JOIN namespaces n ON n.id = c.namespace_id
        WHERE c.resolution IS NULL
        ORDER BY c.detected_at DESC NULLS LAST
        LIMIT $1
        """,
        lim,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        dt = r["detected_at"]
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        out.append(
            {
                "id": str(r["id"]),
                "namespace_id": str(r["namespace_id"]),
                "namespace_slug": r["namespace_slug"],
                "detected_at": dt.astimezone(timezone.utc).isoformat() if dt else None,
                "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
                "detection_path": r["detection_path"],
                "memory_a_id": str(r["memory_a_id"]),
                "memory_b_id": str(r["memory_b_id"]),
            }
        )
    return out


async def fetch_namespace_bridge_subscriptions(
    conn: asyncpg.Connection,
    namespace_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Active-ish bridge subscriptions for Fleet bridge swimlane cards."""
    rows = await conn.fetch(
        """
        SELECT id, provider, status, expires_at, resource_id, created_at, updated_at
        FROM bridge_subscriptions
        WHERE namespace_id = $1
          AND status IN ('ACTIVE', 'DEGRADED', 'REQUESTED', 'VALIDATING')
        ORDER BY expires_at ASC NULLS LAST, provider ASC
        """,
        namespace_id,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        exp = r["expires_at"]
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        cr = r["created_at"]
        if cr is not None and cr.tzinfo is None:
            cr = cr.replace(tzinfo=timezone.utc)
        up = r["updated_at"]
        if up is not None and up.tzinfo is None:
            up = up.replace(tzinfo=timezone.utc)
        out.append(
            {
                "id": str(r["id"]),
                "provider": r["provider"],
                "status": r["status"],
                "expires_at": exp.astimezone(timezone.utc).isoformat() if exp else None,
                "resource_id": r["resource_id"],
                "created_at": cr.astimezone(timezone.utc).isoformat() if cr else None,
                "updated_at": up.astimezone(timezone.utc).isoformat() if up else None,
            }
        )
    return out
