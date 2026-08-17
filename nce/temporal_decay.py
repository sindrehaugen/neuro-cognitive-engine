"""
Phase 2.2 — Status-Weighted Ebbinghaus Forgetting Curves
=========================================================
Implements the retention probability formula R = e^(-t/S) where:

  R  — retention probability ∈ [0.0, 1.0]
  t  — elapsed time since last interaction (fractional days)
  S  — stability score (domain-specific constant; higher S = slower decay)

Decay schedules are domain-specific:

  MemoryClass.INCIDENT         S=7   — high-salience operational alerts decay in ~7 days
  MemoryClass.CONFIGURATION    S=30  — config-drift memories persist ~30 days
  MemoryClass.TOPOLOGY_EDGE    S=90  — infrastructure topology retains ~90 days
  MemoryClass.CONSOLIDATED     S=60  — sleep-consolidated episodic summaries
  MemoryClass.CODE_CHUNK       S=180 — source code graph edges are highly stable

Pruning threshold: memories with R < RETENTION_PRUNE_THRESHOLD (0.15) are soft-deleted
(valid_to = now()) by the background cron job ``_decay_prune_tick``.

Background task hook:
  ``register_decay_jobs(scheduler, pool)`` wires the hourly prune sweep into the
  existing APScheduler instance in ``nce/cron.py``. The sweep runs under a
  distributed cron lock (``nce.cron_lock``) so only one NCE instance prunes at a time.

Mathematical note:
  R(t) = exp(-t / S)
  At t=S, R = e^-1 ≈ 0.368 (37% retention — the classic Ebbinghaus threshold).
  At t=3S, R ≈ 0.050 (5% — far below the 0.15 prune boundary).

All math uses only stdlib ``math`` — no NumPy/SciPy import required at module level,
keeping this module importable in minimal environments (CLI tools, tests without ML deps).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from enum import Enum
from typing import NamedTuple

log = logging.getLogger("nce.temporal_decay")

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

# Memories with R below this threshold are candidates for soft-deletion.
# 0.15 = 15% retention — enough signal to confirm true forgetting while
# preserving genuinely rare but high-salience outliers that may have been
# manually reinforced via the Active Learning Loop (Phase 3).
RETENTION_PRUNE_THRESHOLD: float = 0.15

# Cron interval for the decay prune sweep (matches the hourly cadence of
# other Phase 2 maintenance jobs in nce/cron.py).
DECAY_PRUNE_INTERVAL_MINUTES: int = 60

# Maximum rows soft-deleted per prune tick to bound the lock-hold duration.
# At 150 tenants × 1000 memories/tenant, a full prune of expired memories
# completes in at most ceil(150_000 / 500) = 300 ticks ≈ 12.5 hours lag.
DECAY_PRUNE_BATCH_SIZE: int = 500


# ---------------------------------------------------------------------------
# Memory class taxonomy with decay stability values
# ---------------------------------------------------------------------------


class MemoryClass(str, Enum):
    """Cognitive memory classification that drives the Ebbinghaus stability score.

    The stability score S determines how quickly a memory fades:
        high S → slow decay → long retention
        low S  → fast decay → short retention

    Values are calibrated to operational reality: incident logs become stale
    within a week; topology maps are meaningful for months.
    """

    INCIDENT = "incident"  # Operational alerts, fault events
    CONFIGURATION = "configuration"  # Config drift snapshots, param changes
    TOPOLOGY_EDGE = "topology_edge"  # Infrastructure connectivity edges
    CONSOLIDATED = "consolidated"  # Sleep-consolidated episodic summaries
    CODE_CHUNK = "code_chunk"  # Source code graph nodes and edges

    # Alias for generic episodic memories whose class is not yet determined.
    EPISODIC = "episodic"


# Stability scores S per memory class (days).
# These are the primary tuning knobs for the decay engine.
_STABILITY: dict[MemoryClass, float] = {
    MemoryClass.INCIDENT: 7.0,  # R drops to 37% after 7 days
    MemoryClass.CONFIGURATION: 30.0,  # R drops to 37% after 30 days
    MemoryClass.TOPOLOGY_EDGE: 90.0,  # R drops to 37% after 90 days
    MemoryClass.CONSOLIDATED: 60.0,  # R drops to 37% after 60 days
    MemoryClass.CODE_CHUNK: 180.0,  # R drops to 37% after 180 days
    MemoryClass.EPISODIC: 30.0,  # Default: treat as configuration-class
}


def stability_for(memory_class: MemoryClass | str) -> float:
    """Return the stability score S for a given memory class.

    Accepts both ``MemoryClass`` enum values and raw string memory_type values
    as stored in the ``memories.memory_type`` column.

    Unknown classes default to the EPISODIC stability (30 days) and emit a
    DEBUG log so new memory types are not silently over-pruned.
    """
    if isinstance(memory_class, str):
        try:
            memory_class = MemoryClass(memory_class)
        except ValueError:
            log.debug(
                "Unknown memory class %r — defaulting to EPISODIC stability (S=30)",
                memory_class,
            )
            return _STABILITY[MemoryClass.EPISODIC]
    return _STABILITY.get(memory_class, _STABILITY[MemoryClass.EPISODIC])


# ---------------------------------------------------------------------------
# Core retention formula
# ---------------------------------------------------------------------------


class RetentionResult(NamedTuple):
    """Result of a single retention probability calculation."""

    retention: float  # R ∈ [0.0, 1.0]
    elapsed_days: float  # t in days since last_interaction
    stability: float  # S used for this calculation
    memory_class: MemoryClass  # Class that drove S selection
    prune_eligible: bool  # True when R < RETENTION_PRUNE_THRESHOLD


def retention(
    last_interaction: datetime,
    memory_class: MemoryClass | str = MemoryClass.EPISODIC,
    *,
    _now: datetime | None = None,
) -> RetentionResult:
    """Calculate R = e^(-t/S) for a memory last interacted with at *last_interaction*.

    Args:
        last_interaction: Timezone-aware datetime of the last read/write/reinforce.
        memory_class:     MemoryClass enum or string memory_type from DB row.
        _now:             Override current time (injection point for tests).

    Returns:
        RetentionResult with R, elapsed_days, stability used, and prune eligibility.

    Raises:
        ValueError: if last_interaction is in the future (causality violation).
    """
    now = _now if _now is not None else datetime.now(timezone.utc)

    # Normalise to UTC.
    if last_interaction.tzinfo is None:
        last_interaction = last_interaction.replace(tzinfo=timezone.utc)
    else:
        last_interaction = last_interaction.astimezone(timezone.utc)

    if last_interaction > now:
        raise ValueError(
            f"last_interaction {last_interaction.isoformat()} is in the future — "
            "retention can only be calculated for past interactions"
        )

    mc = MemoryClass(memory_class) if isinstance(memory_class, str) else memory_class
    s = stability_for(mc)
    t = (now - last_interaction).total_seconds() / 86_400.0  # convert seconds → days

    r = math.exp(-t / s)
    r = max(0.0, min(1.0, r))  # clamp to [0, 1] for floating-point edge cases

    return RetentionResult(
        retention=r,
        elapsed_days=t,
        stability=s,
        memory_class=mc,
        prune_eligible=(r < RETENTION_PRUNE_THRESHOLD),
    )


def retention_at_age(age_days: float, memory_class: MemoryClass | str) -> float:
    """Convenience function: return R for a memory of known age in days.

    Useful for batch scoring rows where elapsed time is pre-computed in SQL:
        SELECT id, extract(epoch from now() - updated_at)/86400 AS age_days FROM memories
    """
    s = stability_for(memory_class)
    if age_days < 0:
        raise ValueError(f"age_days must be non-negative, got {age_days}")
    r = math.exp(-age_days / s)
    return max(0.0, min(1.0, r))


def days_until_prune(
    last_interaction: datetime,
    memory_class: MemoryClass | str = MemoryClass.EPISODIC,
    *,
    _now: datetime | None = None,
) -> float:
    """Return the number of days until this memory becomes prune-eligible (R < 0.15).

    Returns 0.0 if the memory is already below the prune threshold.
    Returns math.inf if the threshold cannot be reached (mathematically impossible
    for R to cross 0.15 — not possible in practice, included for type safety).

    Formula (solving R = e^(-t/S) for t):
        t_prune = -S * ln(RETENTION_PRUNE_THRESHOLD)
        days_remaining = t_prune - t_elapsed
    """
    result = retention(last_interaction, memory_class, _now=_now)
    if result.prune_eligible:
        return 0.0

    s = result.stability
    t_prune = -s * math.log(RETENTION_PRUNE_THRESHOLD)  # = S * ln(1/0.15) ≈ 1.897 * S
    remaining = t_prune - result.elapsed_days
    return max(0.0, remaining)


def score_batch(
    rows: list[dict],
    *,
    class_key: str = "memory_type",
    timestamp_key: str = "valid_from",  # TD-DECAY-2: memories has no updated_at; valid_from is the Ebbinghaus clock origin
    _now: datetime | None = None,
) -> list[dict]:
    """Score a list of memory dicts, appending 'retention' and 'prune_eligible' keys.

    Designed for use with asyncpg ``conn.fetch()`` results (after dict conversion).
    Each row dict must contain at least *class_key* and *timestamp_key*.

    ``timestamp_key`` defaults to ``"valid_from"`` — the memories table column that
    is reset when a memory is reinforced (the correct Ebbinghaus clock origin).
    Pass ``timestamp_key="created_at"`` for immutable-creation-date-based decay.

    Example::

        rows = await conn.fetch("SELECT id, memory_type, updated_at FROM memories ...")
        scored = score_batch([dict(r) for r in rows])
        to_prune = [r for r in scored if r['prune_eligible']]
    """
    now = _now if _now is not None else datetime.now(timezone.utc)
    result = []
    for row in rows:
        ts = row.get(timestamp_key)
        mc = row.get(class_key, MemoryClass.EPISODIC)
        if ts is None:
            row = dict(row, retention=1.0, prune_eligible=False)
        else:
            r = retention(ts, mc, _now=now)
            row = dict(row, retention=r.retention, prune_eligible=r.prune_eligible)
        result.append(row)
    return result


# ---------------------------------------------------------------------------
# Background cron job — soft-delete prune sweep
# ---------------------------------------------------------------------------


async def _decay_prune_tick(pool: object) -> None:  # pool: asyncpg.Pool
    """Hourly cron tick: soft-delete memories whose retention R < 0.15.

    For each distributed memory table shard, computes elapsed time in the
    database (avoids clock skew between app servers) using:

        extract(epoch from now() - updated_at) / 86400  AS age_days

    Then applies the per-class threshold:
        WHERE exp(-age_days / S_for_class) < 0.15
        <=>  age_days > -S * ln(0.15)
        <=>  age_days > S * 1.8971...

    Pre-computed prune age thresholds (days, rounded to 4 decimal places):
        incident:      7  * 1.8971 = 13.2799
        configuration: 30 * 1.8971 = 56.9139
        topology_edge: 90 * 1.8971 = 170.7418
        consolidated:  60 * 1.8971 = 113.8278
        code_chunk:   180 * 1.8971 = 341.4835
        episodic:      30 * 1.8971 = 56.9139

    Soft-delete: set valid_to = now() (preserves WORM ledger immutability).
    Hard-delete is handled by the GDPR Cascade Pruning Engine (BATCH-P2-003).

    Lock: distributed cron lock 'decay_prune' with TTL = DECAY_PRUNE_INTERVAL_MINUTES * 60 + 60.
    """
    import asyncpg  # local import — keeps module importable without asyncpg installed

    from nce.cron_lock import acquire_cron_lock, release_cron_lock
    from nce.db_utils import unmanaged_pg_connection

    # -S * ln(RETENTION_PRUNE_THRESHOLD) per memory class, pre-computed.
    # Update this dict if _STABILITY values change.
    _ln_threshold = -math.log(RETENTION_PRUNE_THRESHOLD)  # ≈ 1.89712
    prune_ages: dict[str, float] = {
        mc.value: round(_STABILITY[mc] * _ln_threshold, 4) for mc in MemoryClass
    }

    ttl = DECAY_PRUNE_INTERVAL_MINUTES * 60 + 60
    lock = await acquire_cron_lock("decay_prune", ttl)
    if lock is None:
        log.debug("Skipping decay_prune — lock held by another instance")
        return

    total_pruned = 0
    try:
        async with unmanaged_pg_connection(pool, site="cron.decay_prune") as conn:  # type: ignore[arg-type]
            for mc_value, age_threshold in prune_ages.items():
                try:
                    # TD-DECAY-1 fix: PostgreSQL does not support UPDATE...LIMIT
                    # (that is MySQL-only syntax). Use a CTE to select candidates
                    # with LIMIT first, then JOIN in the UPDATE.
                    # TD-DECAY-2 fix: memories has no 'updated_at' column.
                    # Use 'valid_from' — the timestamp that resets when a memory
                    # is reinforced, which is the correct Ebbinghaus clock origin.
                    result = await conn.execute(
                        """
                        WITH candidates AS (
                            SELECT id, created_at
                            FROM   memories
                            WHERE  memory_type = $1
                              AND  valid_to IS NULL
                              AND  extract(epoch from now() - valid_from) / 86400.0 > $2
                            LIMIT  $3
                        )
                        UPDATE memories m
                        SET    valid_to = now()
                        FROM   candidates c
                        WHERE  m.id = c.id
                          AND  m.created_at = c.created_at
                        """,
                        mc_value,
                        age_threshold,
                        DECAY_PRUNE_BATCH_SIZE,
                    )
                    # asyncpg returns "UPDATE N" — extract the row count.
                    pruned = int(result.split()[-1]) if result else 0
                    total_pruned += pruned
                    if pruned > 0:
                        log.info(
                            "decay_prune: soft-deleted %d memories class=%s age_threshold=%.2f days",
                            pruned,
                            mc_value,
                            age_threshold,
                        )
                except (asyncpg.PostgresError, ValueError) as exc:
                    log.exception(
                        "decay_prune: failed for class=%s threshold=%.2f: %s",
                        mc_value,
                        age_threshold,
                        exc,
                    )
    except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
        log.exception("decay_prune tick failed: %s", exc)
    finally:
        # TD-DECAY-3 fix: release_cron_lock already imported at top of function
        # (line 289); the duplicate import in the finally block is removed.
        await release_cron_lock(lock)
        if total_pruned > 0:
            log.info("decay_prune tick complete: total soft-deleted=%d", total_pruned)
        else:
            log.debug("decay_prune tick complete: no memories eligible for pruning")


def register_decay_jobs(scheduler: object, pool: object) -> None:
    """Wire the decay prune sweep into the APScheduler instance from nce/cron.py.

    Call from ``nce.cron.async_main()`` after the scheduler is created:

        from nce.temporal_decay import register_decay_jobs
        register_decay_jobs(scheduler, pool)

    The sweep runs every ``DECAY_PRUNE_INTERVAL_MINUTES`` minutes (default: 60).
    It uses a distributed cron lock so only one NCE instance prunes at a time
    in multi-node deployments.

    Args:
        scheduler: An ``apscheduler.schedulers.asyncio.AsyncIOScheduler`` instance.
        pool:      An ``asyncpg.Pool`` for database writes.
    """
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler.add_job(  # type: ignore[union-attr]
        _decay_prune_tick,
        IntervalTrigger(minutes=DECAY_PRUNE_INTERVAL_MINUTES),
        args=[pool],
        id="phase_2_2_decay_prune",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )
    log.info(
        "Registered decay prune job: interval=%dm threshold=R<%.2f",
        DECAY_PRUNE_INTERVAL_MINUTES,
        RETENTION_PRUNE_THRESHOLD,
    )


# ---------------------------------------------------------------------------
# Utility: materialise a retention score view for the API layer
# ---------------------------------------------------------------------------


def build_retention_summary(
    memories: list[dict],
    *,
    class_key: str = "memory_type",
    timestamp_key: str = "valid_from",  # TD-DECAY-2: memories has no updated_at; valid_from is the Ebbinghaus clock origin
    id_key: str = "id",
    _now: datetime | None = None,
) -> list[dict]:
    """Build a retention summary payload for the REST API response.

    Returns a list of lightweight dicts suitable for JSON serialisation:
        [{
            "id": "<memory_uuid>",
            "memory_type": "incident",
            "retention": 0.412,
            "elapsed_days": 4.8,
            "stability": 7.0,
            "prune_eligible": false,
            "days_until_prune": 8.3
        }, ...]

    Args:
        memories:      List of dicts from asyncpg rows (converted via ``dict(row)``).
        class_key:     Key for the memory class/type field.
        timestamp_key: Key for the last-interaction timestamp.
        id_key:        Key for the unique identifier.
        _now:          Override current time (injection point for tests).
    """
    now = _now if _now is not None else datetime.now(timezone.utc)
    summaries = []
    for mem in memories:
        ts: datetime | None = mem.get(timestamp_key)
        mc_raw = mem.get(class_key, MemoryClass.EPISODIC)

        if ts is None:
            summaries.append(
                {
                    id_key: mem.get(id_key),
                    class_key: mc_raw,
                    "retention": 1.0,
                    "elapsed_days": 0.0,
                    "stability": stability_for(mc_raw),
                    "prune_eligible": False,
                    "days_until_prune": days_until_prune(now, mc_raw, _now=now),
                }
            )
            continue

        result = retention(ts, mc_raw, _now=now)
        dtp = days_until_prune(ts, mc_raw, _now=now)
        summaries.append(
            {
                id_key: mem.get(id_key),
                class_key: result.memory_class.value,
                "retention": round(result.retention, 6),
                "elapsed_days": round(result.elapsed_days, 4),
                "stability": result.stability,
                "prune_eligible": result.prune_eligible,
                "days_until_prune": round(dtp, 4),
            }
        )
    return summaries
