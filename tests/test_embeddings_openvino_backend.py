"""``OpenVINONPUBackend`` must load its IR with the RAW OpenVINO runtime.

Why this file exists (TDL D45)
------------------------------
``_load_openvino_npu_bundle`` used to do ``from optimum.intel import
OVModelForFeatureExtraction``. ``optimum-intel`` 2.1.0 requires
``transformers<5.6``, while this repo pins ``transformers>=5.14.1`` -- pinning
both is ``ResolutionImpossible``. Unpinned, pip does not error: it silently
backtracks to ``optimum-intel==1.15.0`` (2024), which cannot detect OpenVINO and
raises the *misleading* ``ImportError: OVModelForFeatureExtraction requires the
openvino library but it was not found in your environment`` **while openvino is
installed and importable**. ``NCE_BACKEND=openvino_npu`` was dead on arrival,
and nothing noticed because ``NCE_COGNITIVE_BASE_URL`` short-circuits
``detect_backend()`` before any hardware backend is constructed.

The defect is **which library gets imported**, so the headline test poisons
``optimum`` in ``sys.modules`` and asserts the loader still succeeds. A test that
passed merely because ``openvino`` happens to be installed would not gate it.

No hardware and no 617 MB IR is involved: ``openvino`` and ``transformers`` are
replaced by fakes and the "IR" is an empty ``openvino_model.xml``.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest
import torch

from nce.config import cfg
from nce.embeddings import (
    VECTOR_DIM,
    OpenVINONPUBackend,
    _load_openvino_npu_bundle,
)

SEQ_LEN: int = cfg.NCE_OPENVINO_SEQ_LEN


@pytest.fixture(autouse=True)
def _clear_bundle_cache():
    """``_load_openvino_npu_bundle`` is ``lru_cache``d -- otherwise tests contaminate."""
    _load_openvino_npu_bundle.cache_clear()
    yield
    _load_openvino_npu_bundle.cache_clear()


class _FakePort:
    """Stand-in for an ``ov.Output`` port: hashable, and names itself."""

    def __init__(self, name: str) -> None:
        self._name = name

    def get_any_name(self) -> str:
        return self._name

    def get_names(self) -> set[str]:
        return {self._name}


class _FakeCompiledModel:
    """Raw-runtime compiled model: called with a dict, read back by output port."""

    def __init__(self, hidden_for_call, input_names=("input_ids", "attention_mask")) -> None:
        self._hidden_for_call = hidden_for_call
        self.calls: list[dict] = []
        self._out = _FakePort("last_hidden_state")
        self.inputs = [_FakePort(n) for n in input_names]

    def output(self, index: int) -> _FakePort:
        assert index == 0
        return self._out

    def __call__(self, feed: dict) -> dict:
        self.calls.append({k: np.array(v, copy=True) for k, v in feed.items()})
        return {self._out: self._hidden_for_call(len(self.calls) - 1)}


class _FakeTokenizer:
    def __init__(self, attention_mask: torch.Tensor | None = None) -> None:
        self._attention_mask = attention_mask
        self.call_kwargs: dict | None = None

    def __call__(self, texts, **kwargs):
        self.call_kwargs = kwargs
        n = len(texts)
        if self._attention_mask is None:
            mask = torch.ones(n, SEQ_LEN, dtype=torch.long)
        else:
            mask = self._attention_mask.expand(n, SEQ_LEN).clone()
        # Row i carries the value i+1 so a per-text slice is identifiable.
        ids = torch.arange(1, n + 1, dtype=torch.long).unsqueeze(1).expand(n, SEQ_LEN).clone()
        return {"input_ids": ids, "attention_mask": mask}


def _install_fake_openvino(monkeypatch, compiled=None, compile_error=None) -> dict:
    recorded: dict = {}

    class _Core:
        def compile_model(self, model, device):
            recorded["model"] = model
            recorded["device"] = device
            if compile_error is not None:
                raise compile_error
            return compiled

    module = types.ModuleType("openvino")
    module.Core = _Core  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openvino", module)
    return recorded


def _install_fake_transformers(monkeypatch, tokenizer) -> dict:
    recorded: dict = {}

    class _AutoTokenizer:
        @staticmethod
        def from_pretrained(model_dir, **kwargs):
            recorded["model_dir"] = model_dir
            recorded["kwargs"] = kwargs
            return tokenizer

    module = types.ModuleType("transformers")
    module.AutoTokenizer = _AutoTokenizer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", module)
    return recorded


def _poison_optimum(monkeypatch) -> None:
    """Any ``import optimum`` / ``from optimum.intel import X`` now raises ImportError."""
    monkeypatch.setitem(sys.modules, "optimum", None)
    monkeypatch.setitem(sys.modules, "optimum.intel", None)


def _make_ir_dir(tmp_path):
    (tmp_path / "openvino_model.xml").write_text("<net/>", encoding="utf-8")
    (tmp_path / "openvino_model.bin").write_bytes(b"")
    return tmp_path


def _constant_hidden(value: float) -> np.ndarray:
    return np.full((1, SEQ_LEN, VECTOR_DIM), value, dtype=np.float32)


# ---------------------------------------------------------------------------
# 1. The defect: the loader must not depend on optimum at all
# ---------------------------------------------------------------------------


def test_loader_succeeds_with_optimum_poisoned(monkeypatch, tmp_path):
    """RED against the old loader: it imported ``optimum.intel``, which now raises."""
    model_dir = _make_ir_dir(tmp_path)
    compiled = _FakeCompiledModel(lambda _i: _constant_hidden(1.0))
    tokenizer = _FakeTokenizer()
    recorded_ov = _install_fake_openvino(monkeypatch, compiled=compiled)
    recorded_tf = _install_fake_transformers(monkeypatch, tokenizer)
    _poison_optimum(monkeypatch)

    got_model, got_tokenizer, got_seq_len = _load_openvino_npu_bundle(str(model_dir), SEQ_LEN)

    assert got_model is compiled
    assert got_tokenizer is tokenizer
    assert got_seq_len == SEQ_LEN
    assert recorded_ov["device"] == "NPU"
    assert str(recorded_ov["model"]).endswith("openvino_model.xml")
    assert recorded_tf["model_dir"] == str(model_dir)
    assert recorded_tf["kwargs"] == {"trust_remote_code": cfg.NCE_EMBEDDING_TRUST_REMOTE_CODE}


# ---------------------------------------------------------------------------
# 2. Numerics: mask-weighted mean, L2-normalised
# ---------------------------------------------------------------------------


def _embed_with(
    monkeypatch,
    tmp_path,
    *,
    hidden_for_call,
    mask,
    texts,
    input_names=("input_ids", "attention_mask"),
):
    model_dir = _make_ir_dir(tmp_path)
    compiled = _FakeCompiledModel(hidden_for_call, input_names=input_names)
    _install_fake_openvino(monkeypatch, compiled=compiled)
    tokenizer = _FakeTokenizer(attention_mask=mask)
    _install_fake_transformers(monkeypatch, tokenizer)
    _poison_optimum(monkeypatch)
    backend = OpenVINONPUBackend(model_dir=str(model_dir))
    vectors, degraded = backend._sync_embed_batch(texts)
    return vectors, degraded, compiled, tokenizer


def _two_token_mask() -> torch.Tensor:
    mask = torch.zeros(1, SEQ_LEN, dtype=torch.long)
    mask[0, 0] = 1
    mask[0, 1] = 1
    return mask


def _hidden_with_garbage(garbage: float) -> np.ndarray:
    """Positions 0 and 1 are real (v and 2v); every masked position is garbage."""
    base = np.linspace(0.1, 1.0, VECTOR_DIM, dtype=np.float32)
    hidden = np.full((1, SEQ_LEN, VECTOR_DIM), garbage, dtype=np.float32)
    hidden[0, 0, :] = base
    hidden[0, 1, :] = 2.0 * base
    return hidden


def test_vector_is_mask_weighted_mean_l2_normalised(monkeypatch, tmp_path):
    vectors, degraded, _compiled, tokenizer = _embed_with(
        monkeypatch,
        tmp_path,
        hidden_for_call=lambda _i: _hidden_with_garbage(1000.0),
        mask=_two_token_mask(),
        texts=["hello"],
    )

    assert degraded is False
    assert len(vectors) == 1
    vec = np.array(vectors[0], dtype=np.float64)
    assert vec.shape == (VECTOR_DIM,)
    assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-6

    base = np.linspace(0.1, 1.0, VECTOR_DIM, dtype=np.float32).astype(np.float64)
    expected = base / np.linalg.norm(base)
    assert np.allclose(vec, expected, atol=1e-6)

    # The numeric contract every already-stored embedding lives in.
    assert tokenizer.call_kwargs is not None
    assert tokenizer.call_kwargs["padding"] == "max_length"
    assert tokenizer.call_kwargs["truncation"] is True
    assert tokenizer.call_kwargs["max_length"] == SEQ_LEN


def test_masked_positions_do_not_affect_the_vector(monkeypatch, tmp_path):
    with monkeypatch.context() as m1:
        first, _, _, _ = _embed_with(
            m1,
            tmp_path,
            hidden_for_call=lambda _i: _hidden_with_garbage(1000.0),
            mask=_two_token_mask(),
            texts=["hello"],
        )
    _load_openvino_npu_bundle.cache_clear()
    with monkeypatch.context() as m2:
        second, _, _, _ = _embed_with(
            m2,
            tmp_path,
            hidden_for_call=lambda _i: _hidden_with_garbage(-5000.0),
            mask=_two_token_mask(),
            texts=["hello"],
        )

    assert np.allclose(np.array(first[0]), np.array(second[0]), atol=1e-9)


# ---------------------------------------------------------------------------
# 3. Static shapes [1, seq_len] => one inference per text, in input order
# ---------------------------------------------------------------------------


def _one_hot_hidden(call_index: int) -> np.ndarray:
    hidden = np.zeros((1, SEQ_LEN, VECTOR_DIM), dtype=np.float32)
    hidden[0, 0, call_index] = 1.0
    return hidden


def test_static_ir_runs_one_inference_per_text_in_order(monkeypatch, tmp_path):
    mask = torch.zeros(1, SEQ_LEN, dtype=torch.long)
    mask[0, 0] = 1
    texts = ["first", "second", "third"]

    vectors, degraded, compiled, _tokenizer = _embed_with(
        monkeypatch,
        tmp_path,
        hidden_for_call=_one_hot_hidden,
        mask=mask,
        texts=texts,
    )

    assert degraded is False
    assert len(compiled.calls) == 3, "static IR is batch-1: one inference per text"
    for i, call in enumerate(compiled.calls):
        assert call["input_ids"].shape == (1, SEQ_LEN)
        # Row i of the tokenizer output carries i+1 -- proves the slice, and the order.
        assert int(call["input_ids"][0][0]) == i + 1

    assert len(vectors) == 3
    for i, vec in enumerate(vectors):
        assert int(np.argmax(np.array(vec))) == i


# ---------------------------------------------------------------------------
# 4. Every declared IR input must be fed -- a missing one reads uninitialised memory
# ---------------------------------------------------------------------------


def test_declared_input_missing_from_tokenizer_is_fed_as_zeros(monkeypatch, tmp_path):
    """The IR declares ``token_type_ids``; the tokenizer does not produce it.

    Passing the tokenizer dict straight through leaves that input UNSUPPLIED, and the
    plugin then reads an uninitialised buffer: results are stable within a process and
    differ across processes (measured ``cosine(CPU, NPU)`` drifting 0.999999 -> 0.395 on
    identical input, because GPU/NPU happen to zero their buffers and CPU does not).
    Feeding zeros makes all three devices agree and makes the vectors reproducible.
    """
    mask = torch.ones(1, SEQ_LEN, dtype=torch.long)

    _vectors, degraded, compiled, _tokenizer = _embed_with(
        monkeypatch,
        tmp_path,
        hidden_for_call=lambda _i: _constant_hidden(1.0),
        mask=mask,
        texts=["hello"],
        input_names=("input_ids", "attention_mask", "token_type_ids"),
    )

    assert degraded is False
    assert len(compiled.calls) == 1
    feed = compiled.calls[0]
    assert set(feed) == {"input_ids", "attention_mask", "token_type_ids"}
    token_type_ids = feed["token_type_ids"]
    assert token_type_ids.shape == feed["input_ids"].shape
    assert token_type_ids.dtype == feed["input_ids"].dtype
    assert not token_type_ids.any()


def test_input_not_declared_by_the_ir_is_not_fed(monkeypatch, tmp_path):
    """The converse: feeding a key the IR never declared raises in the real runtime."""
    mask = torch.ones(1, SEQ_LEN, dtype=torch.long)

    _vectors, degraded, compiled, _tokenizer = _embed_with(
        monkeypatch,
        tmp_path,
        hidden_for_call=lambda _i: _constant_hidden(1.0),
        mask=mask,
        texts=["hello"],
        input_names=("input_ids",),
    )

    assert degraded is False
    assert set(compiled.calls[0]) == {"input_ids"}


# ---------------------------------------------------------------------------
# 5. Failure path: compilation raises => degraded fallback of the right length
# ---------------------------------------------------------------------------


def test_compile_failure_degrades_without_losing_rows(monkeypatch, tmp_path):
    model_dir = _make_ir_dir(tmp_path)
    _install_fake_openvino(monkeypatch, compile_error=RuntimeError("NPU not present"))
    _install_fake_transformers(monkeypatch, _FakeTokenizer())
    _poison_optimum(monkeypatch)

    texts = ["a", "b", "c"]
    vectors, degraded = OpenVINONPUBackend(model_dir=str(model_dir))._sync_embed_batch(texts)

    assert degraded is True
    assert len(vectors) == len(texts)
    assert all(len(v) == VECTOR_DIM for v in vectors)
