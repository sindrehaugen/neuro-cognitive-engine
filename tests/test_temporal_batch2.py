"""
BATCH 2 — nce.temporal as_of_query parameter indexing and lookback error messages.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nce.config import cfg
from nce.temporal import _enforce_lookback_boundary, as_of_query, parse_as_of

TZ_UTC = timezone.utc


def _iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture
def _disable_temporal_lookback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "NCE_MAX_TEMPORAL_LOOKBACK_DAYS", 0)


def test_as_of_query_default_start_index_uses_dollar_one(
    _disable_temporal_lookback: None,
) -> None:
    as_of = datetime(2026, 4, 1, 10, 0, 0, tzinfo=TZ_UTC)
    clause, params = as_of_query("SELECT 1", as_of)
    assert "$1" in clause
    assert len(params) == 1


def test_as_of_query_start_index_three_uses_dollar_three_not_one(
    _disable_temporal_lookback: None,
) -> None:
    as_of = datetime(2026, 4, 1, 10, 0, 0, tzinfo=TZ_UTC)
    clause, params = as_of_query("SELECT 1", as_of, start_index=3)
    assert "$3" in clause
    assert "$1" not in clause
    assert len(params) == 1


def test_as_of_query_start_index_three_with_prepended_params(
    _disable_temporal_lookback: None,
) -> None:
    as_of = datetime(2026, 4, 1, 10, 0, 0, tzinfo=TZ_UTC)
    clause, params = as_of_query("SELECT 1", as_of, start_index=3)
    full_params = ["p1", "p2"] + params
    assert "$3" in clause
    assert len(params) == 1
    assert len(full_params) == 3
    assert full_params[:2] == ["p1", "p2"]
    assert full_params[2] == as_of


def test_as_of_query_none_ignores_start_index(
    _disable_temporal_lookback: None,
) -> None:
    clause, params = as_of_query("SELECT 1", None, start_index=99)
    assert clause == "AND valid_to IS NULL"
    assert params == []


def test_lookback_error_message_does_not_expose_config_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "NCE_MAX_TEMPORAL_LOOKBACK_DAYS", 30)
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=TZ_UTC)
    too_old = now - timedelta(days=31)

    with pytest.raises(ValueError) as exc_info:
        parse_as_of(_iso_z(too_old), _now=now)

    msg = str(exc_info.value)
    assert "NCE_MAX_TEMPORAL_LOOKBACK_DAYS" not in msg
    assert "outside the allowed lookback window" in msg


def test_lookback_error_message_includes_earliest_allowed_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "NCE_MAX_TEMPORAL_LOOKBACK_DAYS", 30)
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=TZ_UTC)
    cutoff = now - timedelta(days=30)
    too_old = cutoff - timedelta(seconds=1)

    with pytest.raises(ValueError) as exc_info:
        _enforce_lookback_boundary(too_old, now)

    msg = str(exc_info.value)
    assert cutoff.isoformat() in msg
    assert "earliest allowed" in msg
