"""
tests/perf/bench.py
---------------------------
Measurement harness for the NCE Optimization Ledger (OL).

O0 — Batch 0: reproducible before/after resource snapshots.

Design (Uncle Bob SRP):
  - PerfSnapshot      : pure data container
  - capture()         : context manager, single responsibility = populate one snapshot
  - measure()         : runs warmups + N timed calls, returns aggregates
  - compare()         : accepts thresholds dict, returns (passed, reasons)

All heavy dependencies (psutil, torch) are imported defensively so this module
works with stdlib only.  Missing deps surface as None fields, not ImportError.
"""

from __future__ import annotations

import statistics
import time
import tracemalloc
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

# ── defensive heavy-dep imports ──────────────────────────────────────────────
try:
    import psutil as _psutil
except ImportError:
    _psutil = None  # type: ignore[assignment]

try:
    import torch as _torch
except ImportError:
    _torch = None  # type: ignore[assignment]

# ── CPU time helper (resource module is POSIX-only) ──────────────────────────
try:
    import resource as _resource

    def _cpu_time() -> float:
        ru = _resource.getrusage(_resource.RUSAGE_SELF)
        return ru.ru_utime + ru.ru_stime

except ImportError:
    _resource = None  # type: ignore[assignment]

    def _cpu_time() -> float:  # type: ignore[misc]
        return time.process_time()


# ── data container ───────────────────────────────────────────────────────────


@dataclass
class PerfSnapshot:
    """Immutable-style snapshot of resource usage for one timed block."""

    wall_s: float = 0.0
    cpu_s: float = 0.0
    rss_delta_bytes: int | None = None  # None when psutil absent
    heap_peak_bytes: int | None = None  # None when tracemalloc not started
    vram_peak_bytes: int | None = None  # None when torch/CUDA absent


# ── capture context manager ──────────────────────────────────────────────────


@contextmanager
def capture() -> Generator[PerfSnapshot, None, None]:
    """
    Context manager that fills a PerfSnapshot for the enclosed block.

    Usage::

        with capture() as snap:
            do_work()
        print(snap.wall_s)
    """
    snap = PerfSnapshot()

    # RSS baseline
    _proc = _psutil.Process() if _psutil is not None else None
    rss_before = _proc.memory_info().rss if _proc is not None else None

    # heap
    tracemalloc.start()

    # VRAM baseline
    _has_cuda = _torch is not None and hasattr(_torch, "cuda") and _torch.cuda.is_available()
    if _has_cuda:
        _torch.cuda.reset_peak_memory_stats()

    t_wall_start = time.perf_counter()
    t_cpu_start = _cpu_time()

    try:
        yield snap
    finally:
        snap.wall_s = time.perf_counter() - t_wall_start
        snap.cpu_s = _cpu_time() - t_cpu_start

        # heap peak
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        snap.heap_peak_bytes = peak

        # RSS delta
        if _proc is not None and rss_before is not None:
            snap.rss_delta_bytes = _proc.memory_info().rss - rss_before

        # VRAM peak
        if _has_cuda:
            snap.vram_peak_bytes = _torch.cuda.max_memory_allocated()


# ── measure() ────────────────────────────────────────────────────────────────


@dataclass
class MeasureResult:
    """Aggregated statistics across N runs of a callable."""

    n: int
    wall_mean: float
    wall_stdev: float
    cpu_mean: float
    cpu_stdev: float
    rss_delta_mean: float | None
    heap_peak_mean: float | None
    vram_peak_mean: float | None
    snapshots: list[PerfSnapshot] = field(default_factory=list)

    @property
    def wall_cov(self) -> float:
        """Coefficient of variation for wall time (stdev / mean).  0 if mean==0."""
        if self.wall_mean == 0:
            return 0.0
        return self.wall_stdev / self.wall_mean


def measure(
    fn: Callable[[], Any],
    n: int = 10,
    warmup: int = 2,
) -> MeasureResult:
    """
    Run *fn* *warmup* times (discarded), then *n* timed times.

    Returns a MeasureResult with mean/stdev per metric.
    Single responsibility: orchestrate capture() calls and aggregate.
    """
    for _ in range(warmup):
        fn()

    snaps: list[PerfSnapshot] = []
    for _ in range(n):
        with capture() as s:
            fn()
        snaps.append(s)

    walls = [s.wall_s for s in snaps]
    cpus = [s.cpu_s for s in snaps]

    rss_vals = [s.rss_delta_bytes for s in snaps if s.rss_delta_bytes is not None]
    heap_vals = [s.heap_peak_bytes for s in snaps if s.heap_peak_bytes is not None]
    vram_vals = [s.vram_peak_bytes for s in snaps if s.vram_peak_bytes is not None]

    def _mean(xs: list) -> float | None:
        return statistics.mean(xs) if xs else None

    def _stdev(xs: list) -> float:
        return statistics.stdev(xs) if len(xs) > 1 else 0.0

    return MeasureResult(
        n=n,
        wall_mean=statistics.mean(walls),
        wall_stdev=_stdev(walls),
        cpu_mean=statistics.mean(cpus),
        cpu_stdev=_stdev(cpus),
        rss_delta_mean=_mean(rss_vals),
        heap_peak_mean=_mean(heap_vals),
        vram_peak_mean=_mean(vram_vals),
        snapshots=snaps,
    )


# ── compare() ────────────────────────────────────────────────────────────────


def compare(
    before: MeasureResult,
    after: MeasureResult,
    thresholds: dict[str, float],
) -> tuple[bool, list[str]]:
    """
    Compare *before* vs *after* against *thresholds* (relative change, 0..1).

    thresholds keys: "wall", "cpu", "rss", "heap", "vram"
    A positive threshold means the metric may INCREASE by at most that fraction.
    A negative threshold means the metric must DECREASE by at least |threshold|.

    Returns (passed: bool, reasons: list[str]).
    """
    reasons: list[str] = []

    def _check(name: str, b: float | None, a: float | None) -> None:
        if b is None or a is None:
            return  # metric absent — skip
        if b == 0:
            return  # avoid div/0
        threshold = thresholds.get(name)
        if threshold is None:
            return
        change = (a - b) / abs(b)
        if change > threshold:
            reasons.append(
                f"{name}: change={change:+.1%} exceeds threshold={threshold:+.1%} "
                f"(before={b:.4g}, after={a:.4g})"
            )

    _check("wall", before.wall_mean, after.wall_mean)
    _check("cpu", before.cpu_mean, after.cpu_mean)
    _check("rss", before.rss_delta_mean, after.rss_delta_mean)
    _check("heap", before.heap_peak_mean, after.heap_peak_mean)
    _check("vram", before.vram_peak_mean, after.vram_peak_mean)

    return (len(reasons) == 0, reasons)
