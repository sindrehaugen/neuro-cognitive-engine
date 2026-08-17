"""
tests/unit/test_temporal.py
============================
Unit tests for nce.temporal_decay — Ebbinghaus Forgetting Curves (BATCH-P2-002).

Validates:
  - Core retention formula R = e^(-t/S)
  - Per-class stability scores produce correct retention at known time points
  - Prune threshold boundary (R < 0.15)
  - days_until_prune calculation
  - score_batch helper
  - build_retention_summary helper
  - ValueError on future timestamps
  - Edge cases: zero elapsed time, very old memories, unknown memory class
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from nce.temporal_decay import (
    RETENTION_PRUNE_THRESHOLD,
    MemoryClass,
    RetentionResult,
    build_retention_summary,
    days_until_prune,
    register_decay_jobs,
    retention,
    retention_at_age,
    score_batch,
    stability_for,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)


def _ago(days: float) -> datetime:
    """Return a datetime *days* days before NOW."""
    return NOW - timedelta(days=days)


# ---------------------------------------------------------------------------
# 1. Stability score lookup
# ---------------------------------------------------------------------------


class TestStabilityFor:
    def test_incident_stability(self):
        assert stability_for(MemoryClass.INCIDENT) == 7.0

    def test_configuration_stability(self):
        assert stability_for(MemoryClass.CONFIGURATION) == 30.0

    def test_topology_edge_stability(self):
        assert stability_for(MemoryClass.TOPOLOGY_EDGE) == 90.0

    def test_consolidated_stability(self):
        assert stability_for(MemoryClass.CONSOLIDATED) == 60.0

    def test_code_chunk_stability(self):
        assert stability_for(MemoryClass.CODE_CHUNK) == 180.0

    def test_episodic_stability(self):
        assert stability_for(MemoryClass.EPISODIC) == 30.0

    def test_string_alias_incident(self):
        assert stability_for("incident") == 7.0

    def test_string_alias_configuration(self):
        assert stability_for("configuration") == 30.0

    def test_unknown_class_defaults_to_episodic(self):
        # Unknown string classes default to EPISODIC stability (30 days).
        assert stability_for("nonexistent_class") == 30.0


# ---------------------------------------------------------------------------
# 2. Core retention formula: R = e^(-t/S)
# ---------------------------------------------------------------------------


class TestRetentionFormula:
    def test_zero_elapsed_returns_one(self):
        result = retention(NOW, MemoryClass.INCIDENT, _now=NOW)
        assert result.retention == pytest.approx(1.0, abs=1e-9)
        assert result.elapsed_days == pytest.approx(0.0, abs=1e-9)

    def test_incident_at_stability_boundary(self):
        # At t == S, R should equal e^-1 ≈ 0.36788
        result = retention(_ago(7.0), MemoryClass.INCIDENT, _now=NOW)
        assert result.retention == pytest.approx(math.exp(-1), rel=1e-5)
        assert result.elapsed_days == pytest.approx(7.0, rel=1e-4)
        assert result.stability == 7.0
        assert result.memory_class == MemoryClass.INCIDENT

    def test_configuration_at_stability_boundary(self):
        result = retention(_ago(30.0), MemoryClass.CONFIGURATION, _now=NOW)
        assert result.retention == pytest.approx(math.exp(-1), rel=1e-5)

    def test_topology_edge_at_stability_boundary(self):
        result = retention(_ago(90.0), MemoryClass.TOPOLOGY_EDGE, _now=NOW)
        assert result.retention == pytest.approx(math.exp(-1), rel=1e-5)

    def test_code_chunk_at_stability_boundary(self):
        result = retention(_ago(180.0), MemoryClass.CODE_CHUNK, _now=NOW)
        assert result.retention == pytest.approx(math.exp(-1), rel=1e-5)

    def test_retention_decreases_monotonically(self):
        r_day1 = retention(_ago(1.0), MemoryClass.INCIDENT, _now=NOW).retention
        r_day3 = retention(_ago(3.0), MemoryClass.INCIDENT, _now=NOW).retention
        r_day7 = retention(_ago(7.0), MemoryClass.INCIDENT, _now=NOW).retention
        assert r_day1 > r_day3 > r_day7

    def test_retention_never_below_zero(self):
        # Very old memory — R should clamp to 0, never negative.
        result = retention(_ago(10_000.0), MemoryClass.INCIDENT, _now=NOW)
        assert result.retention >= 0.0

    def test_retention_never_above_one(self):
        result = retention(NOW, MemoryClass.INCIDENT, _now=NOW)
        assert result.retention <= 1.0

    def test_returns_retention_result_namedtuple(self):
        result = retention(_ago(5.0), MemoryClass.INCIDENT, _now=NOW)
        assert isinstance(result, RetentionResult)

    def test_naive_datetime_treated_as_utc(self):
        # Naive datetime (no tzinfo) should be accepted and treated as UTC.
        naive_ts = datetime(2026, 5, 29, 12, 0, 0)  # 7 days ago, naive
        result = retention(naive_ts, MemoryClass.INCIDENT, _now=NOW)
        assert result.retention == pytest.approx(math.exp(-1), rel=1e-3)

    def test_future_timestamp_raises_value_error(self):
        future_ts = NOW + timedelta(days=1)
        with pytest.raises(ValueError, match="future"):
            retention(future_ts, MemoryClass.INCIDENT, _now=NOW)

    def test_string_memory_class_accepted(self):
        result = retention(_ago(7.0), "incident", _now=NOW)
        assert result.retention == pytest.approx(math.exp(-1), rel=1e-5)


# ---------------------------------------------------------------------------
# 3. Prune threshold
# ---------------------------------------------------------------------------


class TestPruneThreshold:
    def test_prune_threshold_value(self):
        assert RETENTION_PRUNE_THRESHOLD == pytest.approx(0.15)

    def test_incident_below_threshold_at_prune_age(self):
        # t_prune = -7 * ln(0.15) ≈ 13.28 days
        t_prune = -7.0 * math.log(0.15)
        result = retention(_ago(t_prune + 0.01), MemoryClass.INCIDENT, _now=NOW)
        assert result.prune_eligible is True
        assert result.retention < RETENTION_PRUNE_THRESHOLD

    def test_incident_above_threshold_before_prune_age(self):
        t_prune = -7.0 * math.log(0.15)
        result = retention(_ago(t_prune - 0.01), MemoryClass.INCIDENT, _now=NOW)
        assert result.prune_eligible is False
        assert result.retention >= RETENTION_PRUNE_THRESHOLD

    def test_configuration_prune_age(self):
        # t_prune = -30 * ln(0.15) ≈ 56.91 days
        t_prune = -30.0 * math.log(0.15)
        result = retention(_ago(t_prune + 0.1), MemoryClass.CONFIGURATION, _now=NOW)
        assert result.prune_eligible is True

    def test_topology_edge_prune_age(self):
        # t_prune = -90 * ln(0.15) ≈ 170.74 days
        t_prune = -90.0 * math.log(0.15)
        result = retention(_ago(t_prune + 0.1), MemoryClass.TOPOLOGY_EDGE, _now=NOW)
        assert result.prune_eligible is True

    def test_topology_edge_not_prune_eligible_at_half_threshold(self):
        # At 85 days, topology_edge R >> 0.15
        result = retention(_ago(85.0), MemoryClass.TOPOLOGY_EDGE, _now=NOW)
        assert result.prune_eligible is False
        assert result.retention > RETENTION_PRUNE_THRESHOLD

    def test_fresh_memory_never_prune_eligible(self):
        result = retention(_ago(0.1), MemoryClass.INCIDENT, _now=NOW)
        assert result.prune_eligible is False


# ---------------------------------------------------------------------------
# 4. retention_at_age convenience function
# ---------------------------------------------------------------------------


class TestRetentionAtAge:
    def test_age_zero_returns_one(self):
        assert retention_at_age(0.0, MemoryClass.INCIDENT) == pytest.approx(1.0)

    def test_age_equals_stability(self):
        for mc, s in [
            (MemoryClass.INCIDENT, 7.0),
            (MemoryClass.CONFIGURATION, 30.0),
            (MemoryClass.TOPOLOGY_EDGE, 90.0),
        ]:
            assert retention_at_age(s, mc) == pytest.approx(math.exp(-1), rel=1e-6)

    def test_negative_age_raises(self):
        with pytest.raises(ValueError, match="non-negative"):
            retention_at_age(-1.0, MemoryClass.INCIDENT)

    def test_string_class(self):
        assert retention_at_age(7.0, "incident") == pytest.approx(math.exp(-1), rel=1e-6)


# ---------------------------------------------------------------------------
# 5. days_until_prune
# ---------------------------------------------------------------------------


class TestDaysUntilPrune:
    def test_already_prunable_returns_zero(self):
        # Memory well past prune age.
        result = days_until_prune(_ago(100.0), MemoryClass.INCIDENT, _now=NOW)
        assert result == pytest.approx(0.0)

    def test_incident_days_until_prune_fresh(self):
        # Incident created now: must survive -S*ln(0.15) ≈ 13.28 days.
        expected = -7.0 * math.log(RETENTION_PRUNE_THRESHOLD)
        result = days_until_prune(NOW, MemoryClass.INCIDENT, _now=NOW)
        assert result == pytest.approx(expected, rel=1e-4)

    def test_configuration_days_until_prune_fresh(self):
        expected = -30.0 * math.log(RETENTION_PRUNE_THRESHOLD)
        result = days_until_prune(NOW, MemoryClass.CONFIGURATION, _now=NOW)
        assert result == pytest.approx(expected, rel=1e-4)

    def test_days_until_prune_decreases_over_time(self):
        dtp_new = days_until_prune(_ago(0.0), MemoryClass.INCIDENT, _now=NOW)
        dtp_old = days_until_prune(_ago(5.0), MemoryClass.INCIDENT, _now=NOW)
        assert dtp_new > dtp_old

    def test_days_until_prune_never_negative(self):
        result = days_until_prune(_ago(1000.0), MemoryClass.CODE_CHUNK, _now=NOW)
        assert result >= 0.0


# ---------------------------------------------------------------------------
# 6. score_batch
# ---------------------------------------------------------------------------


class TestScoreBatch:
    def _make_row(self, memory_type: str, age_days: float) -> dict:
        # TD-DECAY-2 fix: use valid_from (memories table column), not updated_at
        return {
            "id": "test-id",
            "memory_type": memory_type,
            "valid_from": _ago(age_days),
        }

    def test_adds_retention_key(self):
        rows = [self._make_row("incident", 7.0)]
        scored = score_batch(rows, _now=NOW)
        assert "retention" in scored[0]
        assert scored[0]["retention"] == pytest.approx(math.exp(-1), rel=1e-5)

    def test_adds_prune_eligible_key(self):
        rows = [self._make_row("incident", 200.0)]  # very old
        scored = score_batch(rows, _now=NOW)
        assert scored[0]["prune_eligible"] is True

    def test_default_timestamp_key_is_valid_from(self):
        """Verify the default timestamp_key matches the actual memories schema column."""
        import inspect

        sig = inspect.signature(score_batch)
        assert sig.parameters["timestamp_key"].default == "valid_from"

    def test_none_timestamp_defaults_to_fully_retained(self):
        rows = [{"id": "x", "memory_type": "incident", "valid_from": None}]
        scored = score_batch(rows, _now=NOW)
        assert scored[0]["retention"] == pytest.approx(1.0)
        assert scored[0]["prune_eligible"] is False

    def test_empty_batch_returns_empty(self):
        assert score_batch([], _now=NOW) == []

    def test_multiple_classes_in_batch(self):
        rows = [
            self._make_row("incident", 7.0),
            self._make_row("configuration", 30.0),
            self._make_row("topology_edge", 90.0),
        ]
        scored = score_batch(rows, _now=NOW)
        for r in scored:
            assert r["retention"] == pytest.approx(math.exp(-1), rel=1e-4)

    def test_preserves_original_row_fields(self):
        rows = [self._make_row("incident", 1.0)]
        rows[0]["extra_field"] = "preserved"
        scored = score_batch(rows, _now=NOW)
        assert scored[0]["extra_field"] == "preserved"


# ---------------------------------------------------------------------------
# 7. build_retention_summary
# ---------------------------------------------------------------------------


class TestBuildRetentionSummary:
    def _make_mem(self, memory_type: str, age_days: float, uid: str = "abc") -> dict:
        # TD-DECAY-2 fix: use valid_from (memories table column), not updated_at
        return {
            "id": uid,
            "memory_type": memory_type,
            "valid_from": _ago(age_days),
        }

    def test_summary_contains_required_keys(self):
        mems = [self._make_mem("incident", 3.5)]
        summary = build_retention_summary(mems, _now=NOW)
        row = summary[0]
        assert "id" in row
        assert "retention" in row
        assert "elapsed_days" in row
        assert "stability" in row
        assert "prune_eligible" in row
        assert "days_until_prune" in row

    def test_default_timestamp_key_is_valid_from(self):
        """Verify the default timestamp_key matches the actual memories schema column."""
        import inspect

        sig = inspect.signature(build_retention_summary)
        assert sig.parameters["timestamp_key"].default == "valid_from"

    def test_retention_value_correct(self):
        mems = [self._make_mem("incident", 7.0)]
        summary = build_retention_summary(mems, _now=NOW)
        assert summary[0]["retention"] == pytest.approx(math.exp(-1), rel=1e-4)

    def test_stability_correct_for_class(self):
        mems = [self._make_mem("topology_edge", 10.0)]
        summary = build_retention_summary(mems, _now=NOW)
        assert summary[0]["stability"] == 90.0

    def test_empty_returns_empty(self):
        assert build_retention_summary([], _now=NOW) == []

    def test_none_valid_from_returns_full_retention(self):
        # TD-DECAY-2: key is valid_from, not updated_at
        mems = [{"id": "x", "memory_type": "incident", "valid_from": None}]
        summary = build_retention_summary(mems, _now=NOW)
        assert summary[0]["retention"] == pytest.approx(1.0)

    def test_prune_eligible_flagged(self):
        mems = [self._make_mem("incident", 200.0)]
        summary = build_retention_summary(mems, _now=NOW)
        assert summary[0]["prune_eligible"] is True
        assert summary[0]["days_until_prune"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 8. register_decay_jobs (interface contract — no scheduler started)
# ---------------------------------------------------------------------------


class TestRegisterDecayJobs:
    """register_decay_jobs imports apscheduler at call time (not module import time).
    The scheduler is available in production (nce/cron.py) but may not be installed
    in the minimal unit-test environment. We stub the module in sys.modules so the
    lazy import inside register_decay_jobs resolves to our fake class.
    """

    @pytest.fixture(autouse=True)
    def _stub_apscheduler(self, monkeypatch):
        """Inject a minimal apscheduler stub into sys.modules for the duration of each test."""
        import sys
        import types

        class _FakeIntervalTrigger:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        interval_mod = types.ModuleType("apscheduler.triggers.interval")
        interval_mod.IntervalTrigger = _FakeIntervalTrigger

        triggers_mod = types.ModuleType("apscheduler.triggers")
        triggers_mod.interval = interval_mod

        apscheduler_mod = types.ModuleType("apscheduler")
        apscheduler_mod.triggers = triggers_mod

        for name, mod in [
            ("apscheduler", apscheduler_mod),
            ("apscheduler.triggers", triggers_mod),
            ("apscheduler.triggers.interval", interval_mod),
        ]:
            monkeypatch.setitem(sys.modules, name, mod)

    def test_register_calls_add_job_with_correct_id(self):
        """register_decay_jobs must call scheduler.add_job with id='phase_2_2_decay_prune'."""
        added_jobs = []

        class FakeScheduler:
            def add_job(
                self, func, trigger, *, args, id, coalesce, max_instances, replace_existing
            ):
                added_jobs.append({"id": id, "func": func, "args": args})

        register_decay_jobs(FakeScheduler(), pool=object())
        assert len(added_jobs) == 1
        assert added_jobs[0]["id"] == "phase_2_2_decay_prune"

    def test_register_passes_pool_as_arg(self):
        sentinel = object()
        captured = {}

        class FakeScheduler:
            def add_job(
                self, func, trigger, *, args, id, coalesce, max_instances, replace_existing
            ):
                captured["args"] = args

        register_decay_jobs(FakeScheduler(), pool=sentinel)
        assert captured["args"][0] is sentinel


# ---------------------------------------------------------------------------
# 9. Mathematical property tests
# ---------------------------------------------------------------------------


class TestMathProperties:
    def test_retention_approaches_zero_as_t_increases(self):
        """Verify R → 0 as t → ∞ for all memory classes."""
        for mc in MemoryClass:
            r = retention_at_age(10_000.0, mc)
            assert r < 0.001, f"{mc}: R={r} should approach zero at t=10000"

    def test_ebbinghaus_half_life_at_ln2_times_stability(self):
        """At t = S * ln(2), R = 0.5 (Ebbinghaus half-life)."""
        for mc in MemoryClass:
            s = stability_for(mc)
            half_life = s * math.log(2)
            r = retention_at_age(half_life, mc)
            assert r == pytest.approx(0.5, rel=1e-6), f"{mc}: half-life test failed"

    def test_retention_is_continuous_and_smooth(self):
        """Verify R varies continuously — no discontinuities at integer days."""
        import random

        rng = random.Random(42)
        for _ in range(20):
            age = rng.uniform(0.1, 100.0)
            epsilon = 0.0001
            r1 = retention_at_age(age, MemoryClass.INCIDENT)
            r2 = retention_at_age(age + epsilon, MemoryClass.INCIDENT)
            # Continuity: small delta in t → small delta in R
            assert abs(r1 - r2) < 0.01, f"Discontinuity detected at age={age}"

    def test_prune_threshold_consistent_with_days_until_prune(self):
        """days_until_prune returns t such that retention_at_age(t + days) < threshold."""
        for mc in [MemoryClass.INCIDENT, MemoryClass.CONFIGURATION, MemoryClass.TOPOLOGY_EDGE]:
            dtp = days_until_prune(NOW, mc, _now=NOW)
            r_at_prune = retention_at_age(dtp, mc)
            # At exactly t_prune, R should equal threshold ± small floating-point noise.
            assert r_at_prune == pytest.approx(RETENTION_PRUNE_THRESHOLD, rel=1e-4), (
                f"{mc}: R at prune age {dtp:.2f}d = {r_at_prune:.6f}, expected ~{RETENTION_PRUNE_THRESHOLD}"
            )


# ---------------------------------------------------------------------------
# 10. SQL correctness tests (TD-DECAY-4)
# Validate the prune query SQL structure without requiring a live database.
# These tests parse the SQL string extracted from the source and verify:
#   - No UPDATE...LIMIT (MySQL dialect, invalid in PostgreSQL)
#   - Uses valid_from, not updated_at (schema contract)
#   - CTE pattern is present (correct PostgreSQL batch-UPDATE approach)
#   - No duplicate cron_lock import in finally block
# ---------------------------------------------------------------------------


class TestPruneSQLCorrectness:
    """TD-DECAY-4: SQL string validation for _decay_prune_tick.

    These tests extract the SQL from the source code and validate its
    structure statically, catching dialect mistakes before deployment.
    They complement the unit tests for the math layer, which cannot
    catch DB-layer bugs without a live PostgreSQL instance.
    """

    @pytest.fixture(scope="class")
    def prune_sql(self) -> str:
        """Extract the UPDATE SQL string from the _decay_prune_tick function source.

        The SQL lives inside conn.execute(\"\"\"...\"\"\"). We locate the opening
        triple-quote that follows 'conn.execute(' and read until the matching
        closing triple-quote.
        """
        import inspect

        import nce.temporal_decay as td

        source = inspect.getsource(td._decay_prune_tick)
        # Anchor on the conn.execute call that contains the WITH-CTE UPDATE.
        anchor = source.index("conn.execute(")
        open_quote = source.index('"""', anchor)
        close_quote = source.index('"""', open_quote + 3)
        return source[open_quote : close_quote + 3]

    def test_uses_cte_pattern_not_direct_update(self, prune_sql):
        """Batch UPDATE must use CTE (WITH candidates AS (...)) — not bare UPDATE...LIMIT."""
        assert "WITH candidates AS" in prune_sql

    def test_no_update_limit_mysql_syntax(self, prune_sql):
        """PostgreSQL does not support UPDATE...LIMIT. Verify it is absent."""
        import re

        # Match UPDATE...LIMIT at the same indentation level (not inside a CTE SELECT)
        # The pattern is: UPDATE <table> ... LIMIT without a preceding WITH/SELECT
        direct_update_limit = re.search(
            r"^\s*UPDATE\s+memories\s.*?LIMIT",
            prune_sql,
            re.DOTALL | re.MULTILINE,
        )
        assert direct_update_limit is None, (
            "Found UPDATE...LIMIT (MySQL syntax). Use a CTE with LIMIT in the SELECT."
        )

    def test_uses_valid_from_not_updated_at(self, prune_sql):
        """Prune query must use valid_from (real memories column), not updated_at."""
        assert "valid_from" in prune_sql
        assert "updated_at" not in prune_sql

    def test_update_joins_on_composite_pk(self, prune_sql):
        """UPDATE must join on both id and created_at (composite PK after TD-010-1)."""
        assert "m.id = c.id" in prune_sql
        assert "m.created_at = c.created_at" in prune_sql

    def test_prune_tick_no_duplicate_release_import(self):
        """TD-DECAY-3: verify finally block does not re-import release_cron_lock."""
        import inspect

        import nce.temporal_decay as td

        source = inspect.getsource(td._decay_prune_tick)
        # The finally block should use release_cron_lock directly (imported at top)
        # and NOT contain a second 'from nce.cron_lock import' statement.
        finally_block = source[source.index("finally:") :]
        assert "from nce.cron_lock import" not in finally_block, (
            "Duplicate import of release_cron_lock found in finally block (TD-DECAY-3)."
        )

    def test_prune_tick_filters_valid_to_is_null(self, prune_sql):
        """Prune query must only target currently-active memories (valid_to IS NULL)."""
        assert "valid_to IS NULL" in prune_sql

    def test_prune_tick_parametrised_not_fstring(self, prune_sql):
        """SQL must use $1/$2/$3 parameters, not f-string interpolation (SQL injection guard)."""
        import re

        # Should contain parameter placeholders
        assert re.search(r"\$[123]", prune_sql), "Missing parameterised placeholders"
        # Must NOT contain f-string style {variable} interpolation in SQL
        assert "{mc_value}" not in prune_sql
        assert "{age_threshold}" not in prune_sql
