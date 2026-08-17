"""
nce.source_mode.divergence — C5 divergence audit + flip-gate.

Responsibilities (each function does exactly one thing — SRP):

``record_divergence(pool, namespace_id, engine, entity, field,
                    nce_value, ext_value, materiality)``
    Append one row to ``divergence_log`` inside a scoped session, then
    classify its materiality:
      - Above the configured threshold  → dispatch a drift alert via the
        existing ``nce.notifications.dispatcher``.
      - Below the threshold             → log only (no alert, no page).

``flip_blocked(pool, namespace_id, engine, *, window_seconds)``
    Return ``True`` when a ``both→nce`` flip is blocked (the divergence log
    contains at least one entry for this ``(namespace_id, engine)`` within the
    last *window_seconds* seconds), ``False`` when the window is clean.

    A flip is *allowed* only when ``flip_blocked(...)`` returns ``False``
    (zero divergence rows in the window — log is clean, parity proven).

Design constraints (uncle-bob-craft / §9.2):
  - Dependencies point inward: no web, admin, or framework imports.
  - No per-mode branches that drift — all dispatch is data-driven.
  - ``dispatcher.dispatch_alert`` is the ONLY alert path; never build a new one.
  - Materiality threshold is configurable via ``NCE_DIVERGENCE_ALERT_THRESHOLD``
    (default 0.1 — 10 %).  Read at call time via the public
    ``alert_threshold()`` accessor so monkeypatching in tests works
    correctly.  Other modules that need this same rule (e.g.
    ``vertical_modules/economy/finago.py``) import and call
    ``alert_threshold()`` directly rather than reimplementing the env-var
    parse, so there is exactly one implementation of it.
    ``_alert_threshold()`` is kept as a thin backward-compatible delegate to
    ``alert_threshold()`` for any other existing caller of the private name.
  - asyncpg and nce.notifications are imported at the top; no lazy imports.
  - ``nce_value`` / ``ext_value`` are stored as ``TEXT`` (caller converts).
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session
from nce.notifications import dispatcher

log = logging.getLogger("nce.source_mode.divergence")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_ALERT_THRESHOLD: float = 0.1


def alert_threshold() -> float:
    """Read the materiality alert threshold from the live environment.

    Public accessor and the ONE shared implementation of
    ``NCE_DIVERGENCE_ALERT_THRESHOLD`` parsing. Any module that needs to
    know "is this materiality above the alert threshold" (e.g.
    ``vertical_modules/economy/finago.py``'s own ``material: true/false``
    classification, which must never disagree with whether this module
    actually paged anyone for that same row) MUST import and call this
    function rather than reimplementing the same env-var parse locally --
    two independent copies of an identical-looking rule can silently
    desynchronise the instant only one of them is edited (e.g. this
    module's default is retuned but a sibling copy elsewhere is not),
    while every test that only pins each file's own hardcoded literal
    stays green throughout.

    Reads at call time (not import time) so ``os.environ`` monkeypatching
    in tests takes effect without module-level caching.
    """
    raw = os.environ.get("NCE_DIVERGENCE_ALERT_THRESHOLD", "").strip()
    try:
        return float(raw) if raw else _DEFAULT_ALERT_THRESHOLD
    except ValueError:
        log.warning(
            "NCE_DIVERGENCE_ALERT_THRESHOLD=%r is not a valid float; using default %.2f",
            raw,
            _DEFAULT_ALERT_THRESHOLD,
        )
        return _DEFAULT_ALERT_THRESHOLD


def _alert_threshold() -> float:
    """Backward-compatible private alias -- delegates to :func:`alert_threshold`.

    Kept so any existing caller of the underscore-prefixed name keeps
    working unchanged; :func:`alert_threshold` is the single real
    implementation and new code should call that directly. This name must
    never grow its own logic again -- if it does, the two can drift.
    """
    return alert_threshold()


# ---------------------------------------------------------------------------
# record_divergence
# ---------------------------------------------------------------------------


async def record_divergence(
    pool: asyncpg.Pool,  # type: ignore[type-arg]
    *,
    namespace_id: str | UUID,
    engine: str,
    entity: str,
    field: str,
    nce_value: str | None,
    ext_value: str | None,
    materiality: float | Decimal,
) -> None:
    """Append one divergence row and classify its materiality.

    Args:
        pool:         asyncpg pool; a scoped session is acquired internally.
        namespace_id: Active namespace UUID (string or UUID).
        engine:       Engine key (e.g. ``"d365_sync"``).
        entity:       Entity identifier (e.g. ``"contact:abc123"``).
        field:        Field name that diverged (e.g. ``"phone"``).
        nce_value:    NCE's current value for the field (``None`` if absent).
        ext_value:    External system's current value (``None`` if absent).
        materiality:  Numeric divergence magnitude; caller-supplied.
                      Above ``NCE_DIVERGENCE_ALERT_THRESHOLD`` → alert.
    """
    mat = float(materiality)

    async with scoped_pg_session(pool, namespace_id) as conn:
        await conn.execute(
            """
            INSERT INTO divergence_log
                   (namespace_id, engine, entity, field,
                    nce_value, ext_value, materiality)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id,
            engine,
            entity,
            field,
            nce_value,
            ext_value,
            mat,
        )

    log.debug(
        "divergence recorded engine=%s entity=%s field=%s materiality=%.4f",
        engine,
        entity,
        field,
        mat,
    )

    threshold = alert_threshold()
    if mat > threshold:
        title = f"[NCE] Divergence alert: {engine}/{entity}/{field}"
        message = (
            f"Materiality {mat:.4f} exceeds threshold {threshold:.4f}.\n"
            f"NCE value: {nce_value!r}\n"
            f"Ext value: {ext_value!r}"
        )
        await dispatcher.dispatch_alert(title, message)
    else:
        log.debug(
            "sub-threshold divergence logged only: materiality=%.4f < threshold=%.4f",
            mat,
            threshold,
        )


# ---------------------------------------------------------------------------
# flip_blocked
# ---------------------------------------------------------------------------


async def flip_blocked(
    pool: asyncpg.Pool,  # type: ignore[type-arg]
    *,
    namespace_id: str | UUID,
    engine: str,
    window_seconds: float,
) -> bool:
    """Return whether a ``both→nce`` flip is blocked for this engine.

    The flip is **blocked** when the divergence log contains at least one row
    for ``(namespace_id, engine)`` within the last *window_seconds* seconds.
    The flip is **allowed** (returns ``False``) only when the window is clean.

    This is the done-when gate: a flip requires a proven parity window — zero
    divergence rows recorded over the configured lookback period.

    Args:
        pool:           asyncpg pool; a scoped session is acquired internally.
        namespace_id:   Active namespace UUID (string or UUID).
        engine:         Engine key to check (e.g. ``"d365_sync"``).
        window_seconds: Lookback duration in seconds (e.g. ``3600.0`` = 1 h).

    Returns:
        ``True``  — log is dirty; flip is blocked.
        ``False`` — log is clean over the window; flip is allowed.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    async with scoped_pg_session(pool, namespace_id) as conn:
        count: int = await conn.fetchval(
            """
            SELECT COUNT(*)::int
              FROM divergence_log
             WHERE namespace_id = $1
               AND engine = $2
               AND detected_at >= now() - ($3 * INTERVAL '1 second')
            """,
            ns_uuid,
            engine,
            window_seconds,
        )

    blocked = count > 0
    log.debug(
        "flip_blocked engine=%s window=%.0fs divergence_count=%d blocked=%s",
        engine,
        window_seconds,
        count,
        blocked,
    )
    return blocked
