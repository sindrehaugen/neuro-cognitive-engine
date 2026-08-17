"""
Phase 2.2 memory time-travel helpers: parse client-supplied ``as_of`` timestamps
and produce parameterised temporal SQL clauses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nce.config import cfg

_ABSOLUTE_MAX_LOOKBACK_DAYS: int = 3650  # 10 years hard ceiling


def _normalize_to_utc(ts: datetime) -> datetime:
    """Return *ts* normalized to UTC. Naive datetimes are assumed UTC."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _assert_not_future(
    dt: datetime,
    now: datetime,
    label: str = "timestamp",
    *,
    allow_skew: bool = False,
) -> None:
    """Raise ValueError if *dt* is in the future relative to *now*."""
    tolerance = timedelta(seconds=5) if allow_skew else timedelta(0)
    if dt > now + tolerance:
        raise ValueError(
            f"{label} must not be in the future — temporal queries read past state only"
        )


def parse_as_of(
    raw: str | None,
    *,
    _now: datetime | None = None,
) -> datetime | None:
    """
    Parse and validate an ISO 8601 timestamp from an MCP tool or REST body.

    Returns a timezone-aware ``datetime`` (timezone.utc-normalised) or ``None`` when
    ``raw`` is absent, which signals "query the current state".

    Raises ``ValueError`` for:
    - Malformed / non-ISO-8601 strings.
    - Timestamps in the future (time-travel reads *past* state only).
    - Timestamps older than ``cfg.NCE_MAX_TEMPORAL_LOOKBACK_DAYS``
      (prevents unbounded historical scans of ``event_log``).
    """
    if raw is None:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValueError(
            f"as_of must be a valid ISO 8601 timestamp (e.g. '2026-01-15T10:00:00Z'), got: {raw!r}"
        )
    dt = _normalize_to_utc(dt)
    now = _now if _now is not None else datetime.now(timezone.utc)
    _assert_not_future(dt, now, "as_of", allow_skew=(_now is None))
    _enforce_lookback_boundary(dt, now)
    return dt


def _enforce_lookback_boundary(dt: datetime, now: datetime) -> None:
    """Reject *dt* if it exceeds the configured max lookback window.

    Raises ``ValueError`` when the timestamp is older than
    ``cfg.NCE_MAX_TEMPORAL_LOOKBACK_DAYS``.  A value of **0** means
    "no boundary" (operator override for admin maintenance tasks).

    Extracted as a separate function for testability without monkey-patching
    ``parse_as_of``.
    """
    max_days = cfg.NCE_MAX_TEMPORAL_LOOKBACK_DAYS
    if max_days <= 0:
        return
    max_days = min(max_days, _ABSOLUTE_MAX_LOOKBACK_DAYS)
    cutoff = now - timedelta(days=max_days)
    if dt < cutoff:
        raise ValueError(
            f"as_of timestamp {dt.isoformat()} is outside the allowed lookback window "
            f"(earliest allowed: {cutoff.isoformat()})"
        )


def as_of_query(
    base_query: str,
    as_of: datetime | None,
    *,
    start_index: int = 1,
) -> tuple[str, list]:
    """
    Append a parameterised temporal filter to *base_query*.

    When *as_of* is ``None``, queries the current state (``valid_to IS NULL``).
    When *as_of* is provided, restricts to rows valid at that point in time:
    ``valid_from <= as_of AND (valid_to IS NULL OR valid_to > as_of)``.

    Returns ``(sql_clause, param_list)`` suitable for concatenation into a
    pre-existing parameterised query.  The caller is responsible for managing
    ``$N`` parameter index offsets.

    Raises ``ValueError`` if *as_of* is in the future — temporal queries must
    never read non-existent future state (causality boundary enforcement).

    Example::

        clause, params = as_of_query("...", as_of=my_timestamp, start_index=2)
        sql = f"SELECT ... WHERE namespace_id = $1 {clause}"
        full_params = [ns_id] + params
    """
    if as_of is None:
        return "AND valid_to IS NULL", []
    now = datetime.now(timezone.utc)
    as_of = _normalize_to_utc(as_of)
    _assert_not_future(as_of, now, "as_of", allow_skew=True)
    return (
        f"AND valid_from <= ${start_index} AND (valid_to IS NULL OR valid_to > ${start_index})",
        [as_of],
    )


def validate_write_timestamp(ts: datetime | None) -> None:
    """
    Reject writes with timestamps in the future (D8 time-travel integrity).

    Raises ``ValueError`` if *ts* is in the future.
    No-op when *ts* is ``None`` (server assigns ``NOW()``).
    """
    if ts is None:
        return
    now = datetime.now(timezone.utc)
    ts = _normalize_to_utc(ts)
    _assert_not_future(ts, now, "write timestamp", allow_skew=True)
