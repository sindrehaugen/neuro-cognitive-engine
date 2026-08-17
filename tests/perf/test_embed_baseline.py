"""
tests/perf/test_embed_baseline.py
----------------------------------
O0 gate test — baseline snapshot for the embedding path.

Skips cleanly if backend deps (sentence_transformers or numpy) are absent.
Does NOT require a GPU; CPU inference is sufficient for the baseline.

The baseline is printed to stdout so it is captured in CI logs for future
comparison.  No pass/fail threshold on the baseline itself — O0's goal is
to ESTABLISH the numbers, not yet to gate on them.

Only runs under `pytest -m perf`.
"""

from __future__ import annotations

import importlib

import pytest

from tests.perf.bench import measure

# ── dep guard ────────────────────────────────────────────────────────────────


def _import_or_skip(name: str):  # noqa: ANN202
    try:
        return importlib.import_module(name)
    except ImportError:
        pytest.skip(f"Dependency '{name}' not installed; skipping embed baseline.")


# ── workload factory ─────────────────────────────────────────────────────────


def _make_embed_workload():
    """
    Return a callable that does one embedding encode() call.

    Uses sentence_transformers with the smallest reasonable model
    (all-MiniLM-L6-v2, ~22 MB).  Falls back to a numpy dot-product
    if sentence_transformers is absent but numpy is present.
    """
    # Try sentence_transformers first
    try:
        st = importlib.import_module("sentence_transformers")
        model = st.SentenceTransformer("all-MiniLM-L6-v2")  # type: ignore[attr-defined]
        sentences = ["NCE optimization ledger baseline measurement"] * 4

        def _workload_st() -> None:
            model.encode(sentences, batch_size=4, show_progress_bar=False)

        return _workload_st
    except ImportError:
        pass

    # Fallback: numpy matrix multiply (proxy for embed-like compute)
    try:
        np = importlib.import_module("numpy")
        rng = np.random.default_rng(42)
        A = rng.standard_normal((64, 384)).astype("float32")
        B = rng.standard_normal((384, 64)).astype("float32")

        def _workload_np() -> None:
            _ = A @ B

        return _workload_np
    except ImportError:
        pytest.skip(
            "Neither sentence_transformers nor numpy is installed; "
            "cannot construct an embedding workload."
        )


# ── test ─────────────────────────────────────────────────────────────────────


@pytest.mark.perf
def test_embed_baseline() -> None:
    """
    Capture a baseline snapshot for the embedding path.

    The numbers are printed; no regression threshold is applied at O0.
    A future batch (O1+) will import these numbers and gate against them.
    """
    workload = _make_embed_workload()
    result = measure(workload, n=5, warmup=1)

    print(
        f"\n[O0 embed baseline]\n"
        f"  wall_mean   = {result.wall_mean * 1000:.3f} ms\n"
        f"  wall_stdev  = {result.wall_stdev * 1000:.3f} ms\n"
        f"  cpu_mean    = {result.cpu_mean * 1000:.3f} ms\n"
        f"  heap_peak   = {result.heap_peak_mean and result.heap_peak_mean / 1024:.1f} KB\n"
        f"  rss_delta   = {result.rss_delta_mean and result.rss_delta_mean / 1024:.1f} KB\n"
        f"  vram_peak   = {result.vram_peak_mean}\n"
    )

    # Sanity: we got numbers back
    assert result.n == 5
    assert result.wall_mean > 0.0
