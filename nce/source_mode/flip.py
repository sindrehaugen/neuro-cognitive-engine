"""
nce.source_mode.flip — C5 generic flip-gate.

Reads gate status and flips a ``(namespace_id, engine, function)`` from
``source_mode`` ``'both'`` to ``'nce'``, gated on a clean ``divergence_log``
over the parity window.

Wave D-9 (2026-09-20): generalizes
``nce/vertical_modules/sales/flip.py``'s ``do_flip_function`` and
``do_read_sales_divergence``. Both independently re-implemented the exact
same ``divergence_log`` query that :func:`nce.source_mode.divergence.flip_blocked`
already provides generically -- with ``'sales'`` hardcoded as a literal
inside the query instead of taken as a parameter. That is the same
duplicate-implementation risk :func:`nce.source_mode.divergence.alert_threshold`'s
own docstring warns about: two independent copies of an identical-looking
rule can silently desynchronize the instant only one of them is edited.
``sales/flip.py``'s two functions are now thin, ``engine="sales"`` wrappers
over this module (see their own docstrings) -- the shape they already
expose to real callers (the ``sales_divergence_log`` MCP tool, the
``/api/admin/sales/divergences`` REST route) is unchanged; verified by the
existing test suites for both, run unchanged against the new wrappers.

NOT EVERY divergence_log / alert_threshold CONSUMER IS A FLIP-GATE CANDIDATE
-----------------------------------------------------------------------------
``nce/vertical_modules/economy/finago.py``'s GL reconciliation
(``do_reconcile_gl``) is READ + COMPARE + LOG only, by explicit design
(roadmap Section 9.2, restated in that module's own docstring): Finago is
the permanent legal system-of-record and NCE is authoritative for
operational decisions -- the two books **will** diverge forever, and
Section 9.2 names this explicitly as **"a permanent structural cost, not a
transitional bug."** There is no "clean window" state for economy/finago
that should ever authorize a flip to nce-only, because there is no
migration in progress to complete. This module's ``flip_function`` will
happily accept ``engine="economy"`` as a string -- nothing here validates
which engines are legitimate flip candidates -- but doing so would be a
real defect, not a feature: it would treat a permanent dual-book
reconciliation as if it were a transitional Dynamics-365 migration like
Sales's. If economy ever needs a flip gate for some other reason, that is
its own design decision, not a side effect of this generalization existing.

PARTIALLY CLOSED: A STALE COMPARISON NO LONGER READS IDENTICALLY TO A CLEAN ONE
---------------------------------------------------------------------------------
:func:`flip_blocked`'s own gate decision answers "were there zero divergence
rows in the window" -- it still cannot distinguish that from "nothing has
compared anything in the window at all," because both produce a
`COUNT(*) = 0`, and this wave deliberately did NOT change that decision (see
below for why). What changed: :func:`nce.source_mode.divergence.
record_comparison_heartbeat` now records, independently of whether a
divergence was found, that a "both"-mode comparison ran --
:func:`nce.source_mode.resolver.resolve` calls it on every "both" resolution,
which ``read_through``'s own dispatch table guarantees always runs
``parity_check`` right after. :func:`flip_status` now surfaces this as
``last_compared_at`` / ``heartbeat_stale`` in its response, so "zero
divergences because parity held" and "zero divergences because the parity
check silently stopped running" are now DISTINGUISHABLE to a caller reading
the status -- they were not before.

STILL A DELIBERATE DECISION, NOT YET MADE: should :func:`flip_function`
REFUSE a flip when the heartbeat is stale, the way it already refuses one on
a dirty divergence log? Not built here. Every currently-configured "both"
mode function has been flipping successfully without ever writing a
heartbeat row (the mechanism did not exist until this wave), so making
staleness a hard refusal would be a behavior change for every existing flip
caller on its first call after this ships -- exactly the kind of "flipping a
default changes behavior across the whole surface; land it with the
measurement of what it breaks, not ahead of it" caution this session applied
to the tier-redaction fail-open finding. Observability ships now;
enforcement is a separate, explicit decision for whoever owns that call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session
from nce.source_mode.divergence import alert_threshold, flip_blocked, last_comparison_at

_DEFAULT_WINDOW_SECONDS: float = 7.0 * 86400.0


async def flip_status(
    pool: asyncpg.Pool,  # type: ignore[type-arg]
    *,
    namespace_id: str | UUID,
    engine: str,
    window_seconds: float = _DEFAULT_WINDOW_SECONDS,
    entity: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Read *engine*'s divergence log and evaluate the parity window.

    Generic GET-status view of the flip gate for any ``(namespace_id,
    engine)`` pair. Same response shape
    ``nce/vertical_modules/sales/flip.py``'s ``do_read_sales_divergence``
    has always returned, with ``engine`` promoted from a hardcoded literal
    to a parameter.

    ``clean`` / ``flip_blocked`` here are derived from the exact same
    predicate (``namespace_id``, ``engine``,
    ``detected_at >= now() - window``) that :func:`flip_blocked` uses to
    make the real gate decision in :func:`flip_function` below -- kept as
    one query here rather than an extra round trip through
    :func:`flip_blocked` itself, since this function also needs the count
    and the paginated rows in the same call. If you change this WHERE
    clause, change :func:`nce.source_mode.divergence.flip_blocked`'s to
    match, and vice versa -- that is the one duplication this module still
    carries, kept deliberately narrow and called out here so it cannot
    drift unnoticed the way the old per-engine reimplementation did.

    Args:
        pool:           asyncpg pool; a scoped session is acquired internally.
        namespace_id:   Active namespace UUID (string or UUID).
        engine:         Engine key to check (e.g. ``"sales"``).
        window_seconds: Lookback duration in seconds.
        entity:         Optional entity filter (e.g. ``"accounts"``).
        limit:          Page size, clamped to ``[1, 500]``.
        offset:         Page offset, clamped to ``>= 0``.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    limit = min(max(1, int(limit)), 500)
    offset = max(0, int(offset))
    threshold_mat = alert_threshold()

    async with scoped_pg_session(pool, ns_uuid) as conn:
        if entity:
            count_row = await conn.fetchrow(
                """
                SELECT COUNT(*)::int AS total_count,
                       COUNT(*) FILTER (WHERE materiality > $4)::int AS material_count
                  FROM divergence_log
                 WHERE namespace_id = $1::uuid
                   AND engine = $2
                   AND detected_at >= NOW() - ($3 * INTERVAL '1 second')
                   AND entity = $5
                """,
                ns_uuid,
                engine,
                window_seconds,
                threshold_mat,
                entity,
            )
            rows = await conn.fetch(
                """
                SELECT id, entity, field, nce_value, ext_value, materiality, detected_at
                  FROM divergence_log
                 WHERE namespace_id = $1::uuid
                   AND engine = $2
                   AND detected_at >= NOW() - ($3 * INTERVAL '1 second')
                   AND entity = $4
                 ORDER BY detected_at DESC, id DESC
                 LIMIT $5 OFFSET $6
                """,
                ns_uuid,
                engine,
                window_seconds,
                entity,
                limit,
                offset,
            )
        else:
            count_row = await conn.fetchrow(
                """
                SELECT COUNT(*)::int AS total_count,
                       COUNT(*) FILTER (WHERE materiality > $4)::int AS material_count
                  FROM divergence_log
                 WHERE namespace_id = $1::uuid
                   AND engine = $2
                   AND detected_at >= NOW() - ($3 * INTERVAL '1 second')
                """,
                ns_uuid,
                engine,
                window_seconds,
                threshold_mat,
            )
            rows = await conn.fetch(
                """
                SELECT id, entity, field, nce_value, ext_value, materiality, detected_at
                  FROM divergence_log
                 WHERE namespace_id = $1::uuid
                   AND engine = $2
                   AND detected_at >= NOW() - ($3 * INTERVAL '1 second')
                 ORDER BY detected_at DESC, id DESC
                 LIMIT $4 OFFSET $5
                """,
                ns_uuid,
                engine,
                window_seconds,
                limit,
                offset,
            )

        total_count = (
            int(count_row["total_count"])
            if count_row and count_row["total_count"] is not None
            else 0
        )
        material_count = (
            int(count_row["material_count"])
            if count_row and count_row["material_count"] is not None
            else 0
        )

        items = []
        for r in rows:
            mat_val = float(r["materiality"]) if r["materiality"] is not None else 0.0
            items.append(
                {
                    "id": r["id"],
                    "entity": r["entity"],
                    "field": r["field"],
                    "nce_value": r["nce_value"],
                    "ext_value": r["ext_value"],
                    "materiality": mat_val,
                    "is_material": mat_val > threshold_mat,
                    "detected_at": (r["detected_at"].isoformat() if r["detected_at"] else None),
                }
            )

    last_checked = await last_comparison_at(pool, namespace_id=ns_uuid, engine=engine)
    heartbeat_stale = last_checked is None or last_checked < datetime.now(timezone.utc) - timedelta(
        seconds=window_seconds
    )

    return {
        "ok": True,
        "namespace_id": str(ns_uuid),
        "engine": engine,
        "window_seconds": window_seconds,
        "clean": total_count == 0,
        "flip_blocked": total_count > 0,
        "divergences_count": total_count,
        "material_divergences_count": material_count,
        "alert_threshold": threshold_mat,
        # Flip-gate staleness heartbeat (informational, not gating -- see
        # this module's own docstring for why authorization is unchanged):
        # last_compared_at is None / heartbeat_stale is True means "clean"
        # above may mean nothing has compared anything in the window, not
        # that parity held.
        "last_compared_at": last_checked.isoformat() if last_checked else None,
        "heartbeat_stale": heartbeat_stale,
        "items": items,
    }


async def flip_function(
    pool: asyncpg.Pool,  # type: ignore[type-arg]
    *,
    namespace_id: str | UUID,
    engine: str,
    function: str,
    window_seconds: float = _DEFAULT_WINDOW_SECONDS,
) -> dict[str, Any]:
    """Flip ``(namespace_id, engine, function)``'s source mode to ``'nce'``.

    Blocked if :func:`nce.source_mode.divergence.flip_blocked` finds any
    ``divergence_log`` row for ``(namespace_id, engine)`` within
    ``window_seconds`` -- the single canonical gate decision, not a second,
    independent reimplementation of its query (see this module's own
    docstring for why that mattered: it is exactly what
    ``sales/flip.py``'s ``do_flip_function`` used to do, with ``'sales'``
    hardcoded).

    Args:
        pool:           asyncpg pool; a scoped session is acquired internally.
        namespace_id:   Active namespace UUID (string or UUID).
        engine:         Engine key (e.g. ``"sales"``). See this module's own
                        docstring: not every engine that logs divergence is
                        a legitimate flip-gate candidate.
        function:       Function key being flipped (e.g. ``"read_customers"``).
        window_seconds: Lookback duration in seconds.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    blocked = await flip_blocked(
        pool, namespace_id=ns_uuid, engine=engine, window_seconds=window_seconds
    )
    if blocked:
        # A second, cheap query for a human-readable count -- NOT a second
        # copy of the gate decision itself, which flip_blocked() already
        # made above. Losing this count would only degrade the message,
        # never the refusal.
        async with scoped_pg_session(pool, ns_uuid) as conn:
            divergences = await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM divergence_log
                 WHERE namespace_id = $1::uuid
                   AND engine = $2
                   AND detected_at >= NOW() - ($3 * INTERVAL '1 second')
                """,
                ns_uuid,
                engine,
                window_seconds,
            )
        window_days = window_seconds / 86400.0
        return {
            "ok": False,
            "reason": f"Refused: {divergences} divergence(s) detected in the last {window_days:g} days.",
            "divergences_count": divergences,
        }

    async with scoped_pg_session(pool, ns_uuid) as conn:
        await conn.execute(
            """
            INSERT INTO source_mode_config (namespace_id, engine, function, mode, updated_at)
            VALUES ($1::uuid, $2, $3, 'nce', NOW())
            ON CONFLICT (namespace_id, engine, function) DO UPDATE
                SET mode = 'nce',
                    updated_at = NOW()
            """,
            ns_uuid,
            engine,
            function,
        )

    return {
        "ok": True,
        "function": function,
        "mode": "nce",
    }
