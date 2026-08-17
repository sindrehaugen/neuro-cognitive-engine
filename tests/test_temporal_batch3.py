"""
BATCH 3 — nce.temporal lookback ceiling, injected _now, and absolute max cap.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nce.config import cfg
from nce.temporal import parse_as_of

TZ_UTC = timezone.utc


def _iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture
def _patch_temporal_wall_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Pinned wall clock (May 2026) — distinct from injected _now in boundary tests."""
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


def test_absolute_max_lookback_caps_config_at_3650_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "NCE_MAX_TEMPORAL_LOOKBACK_DAYS", 999_999)
    now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=TZ_UTC)

    within_cap = now - timedelta(days=3649)
    beyond_cap = now - timedelta(days=3651)

    dt = parse_as_of(_iso_z(within_cap), _now=now)
    assert dt == within_cap

    with pytest.raises(ValueError, match="outside the allowed lookback window"):
        parse_as_of(_iso_z(beyond_cap), _now=now)


def test_lookback_zero_disables_cap_very_old_timestamp_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "NCE_MAX_TEMPORAL_LOOKBACK_DAYS", 0)
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=TZ_UTC)
    ancient = datetime(2000, 1, 1, 0, 0, 0, tzinfo=TZ_UTC)

    dt = parse_as_of(_iso_z(ancient), _now=now)
    assert dt == ancient


def test_parse_as_of_injected_now_rejects_one_second_in_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "NCE_MAX_TEMPORAL_LOOKBACK_DAYS", 0)
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=TZ_UTC)
    future = now + timedelta(seconds=1)

    with pytest.raises(ValueError, match="future"):
        parse_as_of(_iso_z(future), _now=now)


def test_parse_as_of_injected_now_accepts_one_second_in_past(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "NCE_MAX_TEMPORAL_LOOKBACK_DAYS", 0)
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=TZ_UTC)
    past = now - timedelta(seconds=1)

    dt = parse_as_of(_iso_z(past), _now=now)
    assert dt == past


def test_parse_as_of_lookback_uses_injected_now_not_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
    _patch_temporal_wall_clock: datetime,
) -> None:
    """Wall clock is 2026-05-05; injected _now is 2026-06-01 with 30-day lookback."""
    monkeypatch.setattr(cfg, "NCE_MAX_TEMPORAL_LOOKBACK_DAYS", 30)
    injected_now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=TZ_UTC)
    cutoff = injected_now - timedelta(days=30)

    # Within 30 days of wall clock (May 5) but outside window from injected June 1.
    with pytest.raises(ValueError, match="outside the allowed lookback window"):
        parse_as_of("2026-04-10T00:00:00Z", _now=injected_now)

    dt = parse_as_of(_iso_z(cutoff), _now=injected_now)
    assert dt == cutoff

    with pytest.raises(ValueError, match="outside the allowed lookback window"):
        parse_as_of(_iso_z(cutoff - timedelta(seconds=1)), _now=injected_now)
