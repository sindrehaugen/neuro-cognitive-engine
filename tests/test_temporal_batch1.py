"""
BATCH 1 — nce.temporal UTC normalization and future-timestamp guards.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nce.temporal import (
    _normalize_to_utc,
    as_of_query,
    parse_as_of,
    validate_write_timestamp,
)

TZ_UTC = timezone.utc


@pytest.fixture
def _patch_temporal_wall_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Pinned wall clock so 'future' timestamps are controllable."""
    import nce.temporal as temporal_mod

    fixed = datetime(2026, 5, 5, 12, 0, 0, tzinfo=TZ_UTC)

    class _DT(datetime):  # type: ignore[misc, valid-type]
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None or tz == TZ_UTC:
                return fixed
            return fixed.astimezone(tz)

    monkeypatch.setattr(temporal_mod, "datetime", _DT)
    return fixed


@pytest.fixture
def _disable_temporal_lookback(monkeypatch: pytest.MonkeyPatch) -> None:
    from nce.config import cfg

    monkeypatch.setattr(cfg, "NCE_MAX_TEMPORAL_LOOKBACK_DAYS", 0)


def test_parse_as_of_plus0200_normalizes_to_utc(
    _patch_temporal_wall_clock: datetime,
    _disable_temporal_lookback: None,
) -> None:
    dt = parse_as_of("2026-01-01T10:00:00+02:00")
    assert dt is not None
    assert dt.tzinfo is TZ_UTC
    assert dt.utcoffset() == timedelta(0)
    assert dt == datetime(2026, 1, 1, 8, 0, 0, tzinfo=TZ_UTC)


def test_parse_as_of_naive_string_is_utc(
    _patch_temporal_wall_clock: datetime,
    _disable_temporal_lookback: None,
) -> None:
    dt = parse_as_of("2026-01-01T10:00:00")
    assert dt is not None
    assert dt.tzinfo is TZ_UTC
    assert dt == datetime(2026, 1, 1, 10, 0, 0, tzinfo=TZ_UTC)


def test_as_of_query_plus0530_normalizes_param_to_utc(
    _patch_temporal_wall_clock: datetime,
) -> None:
    as_of = datetime(
        2026,
        4,
        1,
        10,
        0,
        0,
        tzinfo=timezone(timedelta(hours=5, minutes=30)),
    )
    clause, params = as_of_query("SELECT 1", as_of)
    assert "valid_from" in clause
    assert len(params) == 1
    param = params[0]
    assert param.tzinfo is TZ_UTC
    assert param.utcoffset() == timedelta(0)
    assert param == datetime(2026, 4, 1, 4, 30, 0, tzinfo=TZ_UTC)


def test_validate_write_timestamp_future_plus0800_raises(
    _patch_temporal_wall_clock: datetime,
) -> None:
    fixed = _patch_temporal_wall_clock
    future_plus8 = (fixed + timedelta(days=1)).astimezone(timezone(timedelta(hours=8)))
    with pytest.raises(ValueError, match="future"):
        validate_write_timestamp(future_plus8)


def test_validate_write_timestamp_past_plus0800_ok(
    _patch_temporal_wall_clock: datetime,
) -> None:
    past_plus8 = datetime(2026, 3, 1, 8, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    validate_write_timestamp(past_plus8)


def test_normalize_to_utc_naive_matches_explicit_utc() -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)
    assert _normalize_to_utc(naive) == _normalize_to_utc(naive.replace(tzinfo=TZ_UTC))


def test_normalize_to_utc_same_instant_different_offsets() -> None:
    aware_plus2 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    aware_minus2 = datetime(2026, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=-2)))
    assert _normalize_to_utc(aware_plus2) == _normalize_to_utc(aware_minus2)
