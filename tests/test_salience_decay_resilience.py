"""
Tests for salience.py math resilience:
- Zero delta (same timestamp)
- Negative delta (clock skew / future timestamp)
- Normal decay
- Very large delta (overflow guard)
- Near-zero half_life_days (overflow guard on decay constant)
"""

from datetime import datetime, timedelta, timezone

import pytest

from nce.salience import compute_decayed_score

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# TestZeroAndNegativeDelta — clock skew resilience
# ---------------------------------------------------------------------------


class TestZeroAndNegativeDelta:
    """Score must never crash and must return s_last when delta ≤ 0."""

    def test_zero_delta_returns_unmodified_score(self):
        """updated_at == now → delta=0 → exp(0)=1.0 → score unchanged."""
        score = compute_decayed_score(0.8, _NOW, half_life_days=30.0, now=_NOW)
        assert score == pytest.approx(0.8)

    def test_negative_delta_clock_skew_returns_unmodified(self):
        """updated_at 1 second in the future (clock skew) → clamped to 0."""
        future = _NOW + timedelta(seconds=1)
        score = compute_decayed_score(0.9, future, half_life_days=30.0, now=_NOW)
        assert score == pytest.approx(0.9), (
            "Clock-skewed future timestamp must return s_last, not a boosted score"
        )

    def test_negative_delta_large_skew_returns_unmodified(self):
        """updated_at 7 days in the future → still returns s_last."""
        future = _NOW + timedelta(days=7)
        score = compute_decayed_score(0.5, future, half_life_days=30.0, now=_NOW)
        assert score == pytest.approx(0.5)

    def test_zero_delta_naive_datetime_handled(self):
        """Naive datetime (no tzinfo) is treated as timezone.utc — zero delta still works."""
        naive_now = datetime(2026, 5, 8, 12, 0, 0)  # no tzinfo
        score = compute_decayed_score(
            1.0,
            naive_now,
            half_life_days=30.0,
            now=datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert score == pytest.approx(1.0)

    def test_negative_delta_does_not_boost_score(self):
        """Clamped negative delta must return exactly s_last, not > s_last."""
        future = _NOW + timedelta(hours=1)
        score = compute_decayed_score(0.7, future, half_life_days=30.0, now=_NOW)
        assert score <= 0.7, "Clamped score must never exceed the input s_last"


# ---------------------------------------------------------------------------
# TestNormalDecay — formula correctness
# ---------------------------------------------------------------------------


class TestNormalDecay:
    def test_half_life_exactly_halves_score(self):
        """After exactly half_life_days, score should be s_last / 2."""
        half_life = 30.0
        past = _NOW - timedelta(days=half_life)
        score = compute_decayed_score(1.0, past, half_life_days=half_life, now=_NOW)
        assert score == pytest.approx(0.5, rel=1e-6)

    def test_fresh_memory_barely_decays(self):
        """1 second old memory → score is essentially unchanged."""
        past = _NOW - timedelta(seconds=1)
        score = compute_decayed_score(1.0, past, half_life_days=30.0, now=_NOW)
        assert score > 0.9999

    def test_very_old_memory_decays_close_to_zero(self):
        """10× half-life old memory → score approaches 0."""
        past = _NOW - timedelta(days=300)
        score = compute_decayed_score(1.0, past, half_life_days=30.0, now=_NOW)
        assert score < 0.001

    def test_zero_half_life_returns_unmodified(self):
        """half_life_days=0 → no decay, s_last returned as-is."""
        past = _NOW - timedelta(days=100)
        score = compute_decayed_score(0.75, past, half_life_days=0.0, now=_NOW)
        assert score == pytest.approx(0.75)

    def test_negative_half_life_returns_unmodified(self):
        """Negative half_life_days treated same as zero — no crash."""
        past = _NOW - timedelta(days=100)
        score = compute_decayed_score(0.75, past, half_life_days=-1.0, now=_NOW)
        assert score == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# TestOverflowGuard — astronomical deltas and near-zero half_life
# ---------------------------------------------------------------------------


class TestOverflowGuard:
    def test_epoch_timestamp_does_not_raise(self):
        """updated_at at Unix epoch (year 1970) — ~20,000 day delta → no OverflowError."""
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        score = compute_decayed_score(1.0, epoch, half_life_days=30.0, now=_NOW)
        assert 0.0 <= score <= 1.0  # must return a valid float, not raise

    def test_extremely_large_delta_clamps_to_near_zero(self):
        """Memory from year 1970 (~50+ years old) → score floors near 0 without OverflowError."""
        ancient = datetime(1970, 1, 1, tzinfo=timezone.utc)
        score = compute_decayed_score(1.0, ancient, half_life_days=30.0, now=_NOW)
        assert score >= 0.0  # must not be NaN or negative
        assert score < 1e-100  # should be astronomically small

    def test_tiny_half_life_does_not_raise(self):
        """half_life_days=1e-10 with a 1-day-old memory → huge decay constant, no crash."""
        past = _NOW - timedelta(days=1)
        score = compute_decayed_score(1.0, past, half_life_days=1e-10, now=_NOW)
        assert 0.0 <= score <= 1.0  # valid float returned

    def test_result_always_non_negative(self):
        """Score must always be ≥ 0.0 regardless of inputs."""
        # timedelta(days=1_000_000) overflows Python's date range; use a concrete
        # ancient date (~18,750 days before _NOW) which still exercises the exponent
        # clamp guard in compute_decayed_score.
        ancient = datetime(1975, 1, 1, tzinfo=timezone.utc)
        cases = [
            (0.0, _NOW, 30.0),
            (1.0, ancient, 0.001),  # near-zero half-life, old timestamp
            (0.5, _NOW + timedelta(days=365), 30.0),  # clock skew (future ts)
        ]
        for s_last, updated_at, half_life in cases:
            score = compute_decayed_score(s_last, updated_at, half_life_days=half_life, now=_NOW)
            assert score >= 0.0, f"Negative score for inputs ({s_last}, {updated_at}, {half_life})"
