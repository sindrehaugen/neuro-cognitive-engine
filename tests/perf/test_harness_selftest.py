"""
tests/perf/test_harness_selftest.py
------------------------------------
O0 gate test — proves the measurement harness produces REPRODUCIBLE numbers.

Subject: a deterministic stdlib-only CPU workload (SHA-256 over fixed bytes).
This subject has ~0 I/O and ~0 GC variance, giving a stable wall-time CoV.

CoV threshold: 0.50 (50 %) — deliberately wide because the harness itself adds
overhead on a tiny workload.  Real batch tests will use larger subjects.
The important assertion is that the harness RUNS at all and fields are populated
(or correctly None when a dep is absent).

Only runs under `pytest -m perf`.
"""

from __future__ import annotations

import hashlib

import pytest

from tests.perf.bench import PerfSnapshot, measure

# ── constants ─────────────────────────────────────────────────────────────────
_PAYLOAD = b"NCE-OL-O0-selftest" * 4096  # ~72 KB — deterministic, no I/O
_N_RUNS = 10
_WARMUP = 2
_COV_THRESHOLD = 0.50  # documented; see module docstring


def _workload() -> bytes:
    """Deterministic, stdlib-only CPU subject: SHA-256 repeated digest chain."""
    h = _PAYLOAD
    for _ in range(50):
        h = hashlib.sha256(h).digest()
    return h


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.perf
def test_measure_runs_and_returns_result() -> None:
    """measure() completes without error and returns a MeasureResult."""
    result = measure(_workload, n=_N_RUNS, warmup=_WARMUP)
    assert result.n == _N_RUNS
    assert result.wall_mean > 0.0


@pytest.mark.perf
def test_snapshot_fields_populated_or_none() -> None:
    """
    All fields in the first snapshot are either a valid number or None
    (when the dep is absent — never an exception).
    """
    result = measure(_workload, n=3, warmup=1)
    snap: PerfSnapshot = result.snapshots[0]

    assert snap.wall_s > 0.0
    assert snap.cpu_s >= 0.0  # may be 0 on very fast calls

    # rss_delta_bytes: int-or-None
    assert snap.rss_delta_bytes is None or isinstance(snap.rss_delta_bytes, int)

    # heap_peak_bytes: positive int (tracemalloc always available in stdlib)
    assert snap.heap_peak_bytes is not None
    assert snap.heap_peak_bytes >= 0

    # vram_peak_bytes: None when no CUDA
    assert snap.vram_peak_bytes is None or isinstance(snap.vram_peak_bytes, int)


@pytest.mark.perf
def test_wall_cov_below_threshold() -> None:
    """
    The coefficient of variation (stdev/mean) of wall time across N runs
    must be below COV_THRESHOLD.  This is the reproducibility proof.
    """
    result = measure(_workload, n=_N_RUNS, warmup=_WARMUP)
    cov = result.wall_cov
    print(
        f"\n[O0 selftest] wall_mean={result.wall_mean * 1000:.3f} ms  "
        f"wall_stdev={result.wall_stdev * 1000:.3f} ms  CoV={cov:.4f}"
    )
    assert cov < _COV_THRESHOLD, (
        f"Wall-time CoV {cov:.4f} >= threshold {_COV_THRESHOLD}; "
        "harness is too noisy for reliable measurement."
    )
