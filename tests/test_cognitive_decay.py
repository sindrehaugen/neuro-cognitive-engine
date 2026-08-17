"""Unit tests for Phase 1.1 Ebbinghaus-style salience decay (nce.salience)."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from nce import salience


def test_compute_decayed_score_half_life_halves_salience():
    half_life_days = 7.0
    s_last = 1.0
    updated_at = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    reference_now = datetime(2026, 1, 8, 0, 0, tzinfo=timezone.utc)

    out = salience.compute_decayed_score(s_last, updated_at, half_life_days, now=reference_now)
    assert out == pytest.approx(0.5, rel=1e-9)


def test_compute_decayed_score_zero_half_life_returns_unchanged():
    ref = datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert (
        salience.compute_decayed_score(0.8, datetime(2020, 1, 1, tzinfo=timezone.utc), 0.0, now=ref)
        == 0.8
    )
    assert (
        salience.compute_decayed_score(
            0.8, datetime(2020, 1, 1, tzinfo=timezone.utc), -1.0, now=ref
        )
        == 0.8
    )


def test_compute_decayed_score_future_updated_at_returns_unchanged():
    ref = datetime(2026, 1, 1, tzinfo=timezone.utc)
    future = datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert salience.compute_decayed_score(1.0, future, 30.0, now=ref) == 1.0


def test_compute_decayed_score_naive_updated_at_assumed_utc():
    reference_now = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 1, 9, 12, 0)
    out = salience.compute_decayed_score(1.0, naive, 30.0, now=reference_now)
    assert 0.9 < out <= 1.0


def test_ranking_score_clamps_inputs_and_matches_formula():
    cosine_sim = 0.5
    sal = 0.8
    alpha = 0.7
    expected = cosine_sim * (alpha + (1.0 - alpha) * sal)
    assert salience.ranking_score(cosine_sim, sal, alpha) == pytest.approx(expected)

    assert salience.ranking_score(-1.0, 2.0, 0.5) == pytest.approx(0.0)


def test_reinforce_executes_upsert_sql():
    conn = AsyncMock()
    memory_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    agent_id = "agent-a"
    namespace_id = "11111111-2222-3333-4444-555555555555"

    async def _run() -> None:
        await salience.reinforce(conn, memory_id, agent_id, namespace_id, delta=0.05)

    asyncio.run(_run())

    conn.execute.assert_awaited_once()
    call = conn.execute.await_args
    sql = call.args[0].lower()
    assert "memory_salience" in sql
    assert "on conflict" in sql
    assert call.args[1:] == (memory_id, agent_id, namespace_id, 0.05)


# ---------------------------------------------------------------------------
# Deterministic jitter — GC thundering-herd prevention
# ---------------------------------------------------------------------------


class TestDeterministicJitter:
    """Jitter must be stable per memory_id and spread decay curves."""

    def test_jitter_is_deterministic_for_same_memory_id(self):
        """Same memory_id must produce the same decayed score across calls."""
        memory_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        half_life = 30.0
        s_last = 1.0
        updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        now = datetime(2026, 5, 8, tzinfo=timezone.utc)

        score_a = salience.compute_decayed_score(
            s_last,
            updated_at,
            half_life,
            now=now,
            memory_id=memory_id,
        )
        score_b = salience.compute_decayed_score(
            s_last,
            updated_at,
            half_life,
            now=now,
            memory_id=memory_id,
        )
        assert score_a == pytest.approx(score_b)

    def test_different_memory_ids_produce_different_scores(self):
        """Two different memory IDs must yield measurably different scores."""
        id_a = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeee01"
        id_b = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeee02"
        half_life = 30.0
        s_last = 1.0
        updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        now = datetime(2026, 5, 8, tzinfo=timezone.utc)

        score_a = salience.compute_decayed_score(
            s_last,
            updated_at,
            half_life,
            now=now,
            memory_id=id_a,
        )
        score_b = salience.compute_decayed_score(
            s_last,
            updated_at,
            half_life,
            now=now,
            memory_id=id_b,
        )
        assert score_a != pytest.approx(score_b), "Different IDs must diverge"

    def test_jitter_stays_within_plusminus_5_percent(self):
        """Jittered half-life must stay within +/- 5% of the nominal value."""
        memory_ids = [f"aaaaaaaa-bbbb-cccc-dddd-{i:012x}" for i in range(100)]

        for mid in memory_ids:
            # Call the internal helper directly to verify the range
            factor = salience._jitter_factor(mid)
            assert -0.05 <= factor <= 0.05, f"Jitter factor {factor} out of +/- 5% range for {mid}"

    def test_jitter_without_memory_id_is_backward_compatible(self):
        """Omitting memory_id must produce the same result as before."""
        s_last = 0.8
        updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        half_life = 30.0
        now = datetime(2026, 5, 8, tzinfo=timezone.utc)

        score = salience.compute_decayed_score(
            s_last,
            updated_at,
            half_life,
            now=now,
        )
        expected = 0.8 * math.exp(-math.log(2) / 30.0 * 127.0)
        assert score == pytest.approx(expected, rel=1e-9)

    def test_jitter_zero_half_life_still_returns_unchanged(self):
        """Jitter must not override the zero-half-life guard."""
        score = salience.compute_decayed_score(
            0.8,
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            0.0,
            now=datetime(2026, 6, 1, tzinfo=timezone.utc),
            memory_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )
        assert score == 0.8

        score = salience.compute_decayed_score(
            0.8,
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            -1.0,
            now=datetime(2026, 6, 1, tzinfo=timezone.utc),
            memory_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )
        assert score == 0.8

    def test_jitter_pathological_guard(self):
        """If jitter pushes half-life to <= 0, guard clamps to 1% of original."""
        half_life = 0.01  # very small
        score = salience.compute_decayed_score(
            0.5,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            half_life,
            now=datetime(2026, 5, 8, tzinfo=timezone.utc),
            memory_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )
        assert 0.0 <= score <= 1.0
