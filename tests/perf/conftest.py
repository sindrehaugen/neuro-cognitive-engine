"""
tests/perf/conftest.py
----------------------
Shared fixtures for the NCE OL performance test suite.

Noise thresholds are documented here so every perf test derives from the
same constants — single source of truth (DRY / Uncle Bob).

The `perf_thresholds` fixture returns a dict accepted by snapshot.compare().
Positive value = metric may INCREASE by at most that fraction relative to before.
"""

from __future__ import annotations

import pytest

from tests.perf.bench import capture, compare, measure

# ── documented noise thresholds ──────────────────────────────────────────────
# These are intentionally LOOSE for unit-level benchmarks; tighten per-batch
# when a real optimization win is being gated.
_DEFAULT_THRESHOLDS: dict[str, float] = {
    "wall": 0.20,  # 20 % wall-time regression allowed (noise budget)
    "cpu": 0.20,  # 20 % CPU-time regression allowed
    "rss": 0.30,  # 30 % RSS delta regression allowed
    "heap": 0.30,  # 30 % heap peak regression allowed
    "vram": 0.10,  # 10 % VRAM regression allowed (tight; GPU memory is precious)
}


@pytest.fixture
def perf_thresholds() -> dict[str, float]:
    """Return the default noise-budget thresholds for compare()."""
    return dict(_DEFAULT_THRESHOLDS)


@pytest.fixture
def perf_capture():
    """Expose the capture() context manager to tests."""
    return capture


@pytest.fixture
def perf_measure():
    """Expose the measure() helper to tests."""
    return measure


@pytest.fixture
def perf_compare():
    """Expose the compare() helper to tests."""
    return compare
