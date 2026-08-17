"""
Batch 127 — NLI model lifecycle tests.

Tests the TTL/idle-eviction wrapper introduced in nce/contradictions.py.
All tests mock the model loader and torch so they do NOT require a real
multi-hundred-MB cross-encoder download — lifecycle semantics only.

All tests carry @pytest.mark.heavy as per project convention for tests that
touch the NLI loading path (even when the real model is mocked out).
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from nce.contradictions import _NLIModelCache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cache(ttl: int = 10) -> _NLIModelCache:
    """Return a fresh cache instance with the given TTL."""
    return _NLIModelCache(idle_ttl_s=ttl)


def _stub_torch(cuda: bool = False) -> MagicMock:
    """Return a minimal torch stub."""
    torch = MagicMock()
    torch.cuda.is_available.return_value = cuda
    torch.cuda.memory_allocated.return_value = 512 * 1024 * 1024  # 512 MiB
    torch.cuda.memory_reserved.return_value = 768 * 1024 * 1024  # 768 MiB
    return torch


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.heavy
def test_model_loads_on_first_use():
    """The wrapper loads the model lazily on the first .get() call."""
    cache = _make_cache(ttl=900)
    mock_model = MagicMock(name="CrossEncoder")

    with (
        patch("nce.contradictions.cfg") as mock_cfg,
        patch("nce.contradictions._NLIModelCache._load", return_value=mock_model),
    ):
        mock_cfg.NLI_MODEL_ID = "cross-encoder/nli-deberta-v3-small"
        mock_cfg.NCE_NLI_IDLE_TTL_S = 900

        assert cache._model is None, "Model must not be loaded before first use"
        result = cache.get()
        assert result is mock_model, "First get() must return the loaded model"
        assert cache._model is mock_model, "Model must be cached after first load"


@pytest.mark.heavy
def test_model_not_reloaded_on_second_use():
    """A second .get() within the TTL returns the same object without re-loading."""
    cache = _make_cache(ttl=900)
    mock_model = MagicMock(name="CrossEncoder")
    load_calls: list[int] = []

    def _counting_load(self_inner):  # noqa: ANN001
        load_calls.append(1)
        return mock_model

    with patch.object(_NLIModelCache, "_load", _counting_load):
        cache.get()
        cache.get()

    assert len(load_calls) == 1, "Model must only be loaded once within TTL"


@pytest.mark.heavy
def test_eviction_drops_reference():
    """After .evict() the model reference is None."""
    cache = _make_cache(ttl=900)
    mock_model = MagicMock(name="CrossEncoder")

    with patch.object(_NLIModelCache, "_load", return_value=mock_model):
        cache.get()
        assert cache._model is not None

    cache.evict()
    assert cache._model is None, "evict() must drop the model reference"


@pytest.mark.heavy
def test_eviction_calls_cuda_empty_cache_when_cuda_available():
    """On eviction, torch.cuda.empty_cache() is called when CUDA is present."""
    cache = _make_cache(ttl=900)
    mock_model = MagicMock(name="CrossEncoder")
    torch_stub = _stub_torch(cuda=True)

    with (
        patch.object(_NLIModelCache, "_load", return_value=mock_model),
        patch("nce.contradictions.torch", torch_stub, create=True),
    ):
        cache.get()
        cache.evict()

    torch_stub.cuda.empty_cache.assert_called_once()


@pytest.mark.heavy
def test_eviction_does_not_call_cuda_empty_cache_when_no_cuda():
    """When CUDA is unavailable, empty_cache is not called (no-op)."""
    cache = _make_cache(ttl=900)
    mock_model = MagicMock(name="CrossEncoder")
    torch_stub = _stub_torch(cuda=False)

    with (
        patch.object(_NLIModelCache, "_load", return_value=mock_model),
        patch("nce.contradictions.torch", torch_stub, create=True),
    ):
        cache.get()
        cache.evict()

    torch_stub.cuda.empty_cache.assert_not_called()


@pytest.mark.heavy
def test_reload_after_eviction():
    """After eviction a subsequent .get() reloads the model."""
    cache = _make_cache(ttl=900)
    first_model = MagicMock(name="CrossEncoder_v1")
    second_model = MagicMock(name="CrossEncoder_v2")
    models = iter([first_model, second_model])

    def _sequential_load(self_inner):  # noqa: ANN001
        return next(models)

    with patch.object(_NLIModelCache, "_load", _sequential_load):
        r1 = cache.get()
        cache.evict()
        r2 = cache.get()

    assert r1 is first_model
    assert r2 is second_model, "After eviction .get() must reload"


@pytest.mark.heavy
def test_idle_ttl_eviction_via_evict_loop():
    """The background evict loop drops the model after the TTL elapses."""
    # Use a very short TTL so the test completes quickly (< 2 s).
    ttl = 1
    cache = _make_cache(ttl=ttl)
    mock_model = MagicMock(name="CrossEncoder")

    with patch.object(_NLIModelCache, "_load", return_value=mock_model):
        cache.get()
        assert cache._model is not None

    # Wait comfortably beyond the TTL + poll interval.
    deadline = time.monotonic() + ttl + 2
    while time.monotonic() < deadline:
        if cache._model is None:
            break
        time.sleep(0.05)

    assert cache._model is None, "Idle eviction loop must drop model after TTL"


@pytest.mark.heavy
def test_ttl_zero_disables_eviction():
    """When TTL=0, no background thread is started and the model is never auto-evicted."""
    cache = _NLIModelCache(idle_ttl_s=0)
    mock_model = MagicMock(name="CrossEncoder")

    with patch.object(_NLIModelCache, "_load", return_value=mock_model):
        cache.get()

    assert cache._evict_thread is None, "TTL=0 must not start the eviction thread"
    assert cache._model is mock_model, "TTL=0 must keep model loaded indefinitely"


@pytest.mark.heavy
def test_vram_gauge_updated_on_load_with_cuda():
    """VRAM gauges are updated (non-zero) when CUDA is available and the model loads."""
    from nce.observability import NLI_VRAM_ALLOCATED, NLI_VRAM_RESERVED

    cache = _make_cache(ttl=900)
    mock_model = MagicMock(name="CrossEncoder")
    torch_stub = _stub_torch(cuda=True)

    allocated_vals: list[float] = []
    reserved_vals: list[float] = []

    real_alloc_set = NLI_VRAM_ALLOCATED.set
    real_res_set = NLI_VRAM_RESERVED.set

    def _capture_allocated(v):
        allocated_vals.append(v)
        real_alloc_set(v)

    def _capture_reserved(v):
        reserved_vals.append(v)
        real_res_set(v)

    with (
        patch.object(_NLIModelCache, "_load", return_value=mock_model),
        patch("nce.contradictions.torch", torch_stub, create=True),
        patch.object(NLI_VRAM_ALLOCATED, "set", side_effect=_capture_allocated),
        patch.object(NLI_VRAM_RESERVED, "set", side_effect=_capture_reserved),
    ):
        cache.get()

    assert allocated_vals, "NLI_VRAM_ALLOCATED.set must be called on load"
    assert reserved_vals, "NLI_VRAM_RESERVED.set must be called on load"
    assert allocated_vals[-1] > 0, "Allocated VRAM gauge must be > 0 when CUDA is present"


@pytest.mark.heavy
def test_vram_gauge_zeroed_on_evict_with_cuda():
    """After eviction VRAM gauges are set to 0 (CUDA allocator releases memory)."""
    from nce.observability import NLI_VRAM_ALLOCATED

    cache = _make_cache(ttl=900)
    mock_model = MagicMock(name="CrossEncoder")

    # Stub: on evict torch.cuda.memory_allocated returns 0 (freed).
    torch_stub = _stub_torch(cuda=True)
    torch_stub.cuda.memory_allocated.side_effect = [512 * 1024 * 1024, 0]
    torch_stub.cuda.memory_reserved.side_effect = [768 * 1024 * 1024, 0]

    allocated_vals: list[float] = []

    real_alloc_set = NLI_VRAM_ALLOCATED.set

    def _capture(v):
        allocated_vals.append(v)
        real_alloc_set(v)

    with (
        patch.object(_NLIModelCache, "_load", return_value=mock_model),
        patch("nce.contradictions.torch", torch_stub, create=True),
        patch.object(NLI_VRAM_ALLOCATED, "set", side_effect=_capture),
    ):
        cache.get()  # load — gauge set to 512 MiB
        cache.evict()  # evict — gauge set to 0

    # Last value recorded should be 0 (freed after eviction).
    assert allocated_vals[-1] == 0, "VRAM gauge must be zeroed after eviction"


@pytest.mark.heavy
def test_thread_safety_concurrent_gets():
    """Concurrent .get() calls from multiple threads must each return a valid model
    and must not trigger multiple loads."""
    cache = _make_cache(ttl=900)
    mock_model = MagicMock(name="CrossEncoder")
    load_count = 0
    load_lock = threading.Lock()

    def _counting_load(self_inner):  # noqa: ANN001
        nonlocal load_count
        with load_lock:
            load_count += 1
        time.sleep(0.01)  # simulate I/O
        return mock_model

    results: list[Any] = []

    def _worker():
        results.append(cache.get())

    with patch.object(_NLIModelCache, "_load", _counting_load):
        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert all(r is mock_model for r in results), "All threads must receive the model"
    assert load_count == 1, "Model must only be loaded once despite concurrent gets"


# Needed for the Any annotation used in the thread-safety test.
from typing import Any  # noqa: E402
