"""Unit and contract tests for ONNX pre-export, pure-ASGI cognitive service, and HTTP pooling."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import numpy as np
import pytest
import torch

REPO_ROOT = Path(__file__).resolve().parents[1]
COG_DIR = REPO_ROOT / "deploy" / "cognitive-service"
APP_PATH = COG_DIR / "app.py"
SERVER_PATH = COG_DIR / "server.py"
DOCKERFILE_PATH = COG_DIR / "Dockerfile"
REQUIREMENTS_PATH = COG_DIR / "requirements.txt"


def _load_cognitive_app() -> types.ModuleType:
    """Import deploy/cognitive-service/app.py cleanly without polluting sys.path permanently."""
    mod_name = "nce_cognitive_app_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, APP_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _reset_cognitive_state() -> None:
    cog_app = _load_cognitive_app()
    cog_app.reset_state_for_tests()
    yield
    cog_app.reset_state_for_tests()


def _unit_vec(seed: int, dim: int = 768) -> list[float]:
    arr = np.zeros(dim, dtype=np.float32)
    arr[seed % dim] = 1.0
    arr[(seed * 7 + 3) % dim] = 0.5
    arr /= np.linalg.norm(arr)
    return arr.tolist()


# ---------------------------------------------------------------------------
# 1. Exact attention-mask mean pooling + float32 L2 normalization
# ---------------------------------------------------------------------------


def test_mean_pool_and_normalize_matches_sentence_transformer_math() -> None:
    """Exact numpy attention-mask mean pooling + float32 L2 norm matches PyTorch 1:1."""
    cog_app = _load_cognitive_app()

    rng = np.random.default_rng(42)
    hidden = rng.standard_normal((2, 6, 768)).astype(np.float32)
    # Inject extreme garbage into masked pad positions; they must have zero effect.
    hidden[0, 3:, :] = 99999.0
    hidden[1, 4:, :] = -88888.0

    mask = np.array(
        [
            [1, 1, 1, 0, 0, 0],
            [1, 1, 1, 1, 0, 0],
        ],
        dtype=np.int64,
    )

    np_vecs = cog_app.mean_pool_and_normalize(hidden, mask)
    assert np_vecs.dtype == np.float32
    assert np_vecs.shape == (2, 768)

    # PyTorch reference matching SentenceTransformer Pooling + Normalize modules
    t_hidden = torch.from_numpy(hidden)
    t_mask = torch.from_numpy(mask).unsqueeze(-1).to(t_hidden.dtype)
    t_sum = (t_hidden * t_mask).sum(dim=1)
    t_cnt = torch.clamp(t_mask.sum(dim=1), min=1e-9)
    t_pooled = t_sum / t_cnt
    t_normed = torch.nn.functional.normalize(t_pooled, p=2, dim=-1).numpy()

    for i in range(2):
        norm = float(np.linalg.norm(np_vecs[i]))
        assert abs(norm - 1.0) < 1e-5
        cos = float(np.dot(np_vecs[i], t_normed[i]))
        assert cos >= 0.999999
    assert np.allclose(np_vecs, t_normed, atol=1e-6)


# ---------------------------------------------------------------------------
# 2. Pure-ASGI micro-batcher coalescing & strict per-caller error isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_micro_batcher_coalesces_10_concurrent_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10 concurrent POST /v1/embeddings requests coalesce into <= 2 inference batches."""
    cog_app = _load_cognitive_app()
    monkeypatch.setenv("NCE_COGNITIVE_BATCH_WINDOW_MS", "15")
    monkeypatch.setenv("NCE_COGNITIVE_MAX_BATCH_SIZE", "32")
    monkeypatch.delenv("NCE_COGNITIVE_API_KEY", raising=False)
    monkeypatch.delenv("COGNITIVE_API_KEY", raising=False)

    cog_app._state.ready_event.set()
    cog_app._state.load_error = None

    batch_calls: list[list[str]] = []

    def fake_encode(texts: list[str]) -> list[list[float]]:
        batch_calls.append(list(texts))
        return [_unit_vec(int(t.split("-")[-1])) for t in texts]

    monkeypatch.setattr(cog_app, "_encode_texts", fake_encode)

    transport = httpx.ASGITransport(app=cog_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        responses = await asyncio.gather(
            *(client.post("/v1/embeddings", json={"input": f"item-{i}"}) for i in range(10))
        )

    await cog_app._batcher.aclose()
    assert 1 <= len(batch_calls) <= 2
    assert sum(len(b) for b in batch_calls) == 10
    for i, resp in enumerate(responses):
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["embedding"] == pytest.approx(_unit_vec(i), abs=1e-6)


@pytest.mark.asyncio
async def test_micro_batcher_per_caller_error_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure on one caller's input must not fail healthy concurrent callers."""
    cog_app = _load_cognitive_app()
    monkeypatch.setenv("NCE_COGNITIVE_BATCH_WINDOW_MS", "15")
    monkeypatch.setenv("NCE_COGNITIVE_MAX_BATCH_SIZE", "32")
    monkeypatch.delenv("NCE_COGNITIVE_API_KEY", raising=False)
    monkeypatch.delenv("COGNITIVE_API_KEY", raising=False)

    cog_app._state.ready_event.set()
    cog_app._state.load_error = None

    def flaky_encode(texts: list[str]) -> list[list[float]]:
        if any("poison" in t for t in texts):
            raise ValueError("simulated encoder failure on poison input")
        return [_unit_vec(idx + 1) for idx, _ in enumerate(texts)]

    monkeypatch.setattr(cog_app, "_encode_texts", flaky_encode)

    inputs = ["ok-0", "ok-1", "poison-2", "ok-3", "ok-4"]
    transport = httpx.ASGITransport(app=cog_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        responses = await asyncio.gather(
            *(client.post("/v1/embeddings", json={"input": text}) for text in inputs)
        )

    await cog_app._batcher.aclose()
    for idx, resp in enumerate(responses):
        if idx == 2:
            assert resp.status_code == 500
            assert "poison" in resp.json()["error"]
        else:
            assert resp.status_code == 200
            vec = resp.json()["data"][0]["embedding"]
            assert len(vec) == 768
            assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-5


# ---------------------------------------------------------------------------
# 3. Offline boot (local_files_only=True), ORT SessionOptions & endpoints
# ---------------------------------------------------------------------------


def test_offline_boot_enforces_local_files_only_and_ort_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Both baked ONNX dir and dev fallback strictly pass local_files_only=True and ORT options."""
    cog_app = _load_cognitive_app()

    # Case A: baked /opt/nce/models/all-mpnet-base-v2-onnx exists
    onnx_dir = tmp_path / "all-mpnet-base-v2-onnx"
    onnx_dir.mkdir(parents=True)
    (onnx_dir / "model.onnx").write_bytes(b"fake-onnx")
    (onnx_dir / "model_qint8.onnx").write_bytes(b"fake-qint8")
    (onnx_dir / "metadata.json").write_text(
        json.dumps({"dimension": 768, "max_seq_length": 384}),
        encoding="utf-8",
    )

    tok_calls: list[tuple[str, dict]] = []
    ort_calls: list[dict] = []

    class _FakeSessionOptions:
        def __init__(self) -> None:
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0
            self.execution_mode = None
            self.graph_optimization_level = None

    class _FakeExecutionMode:
        ORT_SEQUENTIAL = "ORT_SEQUENTIAL"

    class _FakeGraphOptimizationLevel:
        ORT_ENABLE_ALL = "ORT_ENABLE_ALL"

    class _FakeInferenceSession:
        def __init__(self, path: str, sess_options=None, providers=None) -> None:
            ort_calls.append(
                {
                    "path": path,
                    "sess_options": sess_options,
                    "providers": providers,
                }
            )

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids"), SimpleNamespace(name="attention_mask")]

        def get_outputs(self):
            return [SimpleNamespace(name="last_hidden_state")]

    fake_ort = types.ModuleType("onnxruntime")
    fake_ort.SessionOptions = _FakeSessionOptions  # type: ignore[attr-defined]
    fake_ort.ExecutionMode = _FakeExecutionMode  # type: ignore[attr-defined]
    fake_ort.GraphOptimizationLevel = _FakeGraphOptimizationLevel  # type: ignore[attr-defined]
    fake_ort.InferenceSession = _FakeInferenceSession  # type: ignore[attr-defined]

    class _FakeAutoTokenizer:
        @staticmethod
        def from_pretrained(source: str, **kwargs):
            tok_calls.append((source, kwargs))
            return MagicMock()

    fake_tf = types.ModuleType("transformers")
    fake_tf.AutoTokenizer = _FakeAutoTokenizer  # type: ignore[attr-defined]

    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda _name: MagicMock()  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "transformers", fake_tf)
    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setenv("NCE_MODEL_DIR", str(tmp_path))
    monkeypatch.setenv("NCE_COGNITIVE_BACKEND", "onnx")
    monkeypatch.setenv("NCE_COGNITIVE_NUM_THREADS", "3")

    cog_app._load_models()
    assert cog_app._state.ready_event.is_set()
    assert cog_app._state.engine == "onnxruntime"
    assert cog_app._state.backend == "onnx"
    assert cog_app._state.max_seq_length == 384
    assert tok_calls[-1] == (str(onnx_dir), {"local_files_only": True})
    assert ort_calls[-1]["path"] == str(onnx_dir / "model.onnx")
    assert ort_calls[-1]["providers"] == ["CPUExecutionProvider"]
    opts = ort_calls[-1]["sess_options"]
    assert opts.intra_op_num_threads == 3
    assert opts.inter_op_num_threads == 1
    assert opts.execution_mode == "ORT_SEQUENTIAL"
    assert opts.graph_optimization_level == "ORT_ENABLE_ALL"

    # Case B: onnx-int8 backend selects model_qint8.onnx and reports onnxruntime-int8
    cog_app.reset_state_for_tests()
    monkeypatch.setenv("NCE_COGNITIVE_BACKEND", "onnx-int8")
    cog_app._load_models()
    assert cog_app._state.engine == "onnxruntime-int8"
    assert ort_calls[-1]["path"] == str(onnx_dir / "model_qint8.onnx")

    # Case C: torch backend with absent NCE_MODEL_DIR falls back to MODEL_ID with local_files_only=True
    st_calls: list[tuple[tuple, dict]] = []

    class _FakeST:
        max_seq_length = 384

        def __init__(self, *args, **kwargs) -> None:
            st_calls.append((args, kwargs))

        def get_sentence_embedding_dimension(self) -> int:
            return 768

    fake_st_mod = types.ModuleType("sentence_transformers")
    fake_st_mod.SentenceTransformer = _FakeST  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st_mod)
    monkeypatch.setenv("NCE_MODEL_DIR", str(tmp_path / "missing-dir"))
    monkeypatch.setenv("NCE_COGNITIVE_BACKEND", "torch")
    cog_app.reset_state_for_tests()
    cog_app._load_models()
    assert cog_app._state.engine == "sentence-transformers"
    assert st_calls[-1][0] == (cog_app.DEFAULT_MODEL_ID,)
    assert st_calls[-1][1] == {"device": "cpu", "local_files_only": True}


@pytest.mark.asyncio
async def test_asgi_endpoints_health_readyz_auth_and_zero_basehttp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify /healthz, /readyz, /health fields, Bearer auth, and zero BaseHTTPMiddleware."""
    cog_app = _load_cognitive_app()
    for src_path in (APP_PATH, SERVER_PATH):
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "starlette.middleware.base":
                raise AssertionError(f"BaseHTTPMiddleware import found in {src_path}")
            if isinstance(node, ast.Name) and node.id == "BaseHTTPMiddleware":
                raise AssertionError(f"BaseHTTPMiddleware reference found in {src_path}")
    assert cog_app.app.user_middleware == []

    monkeypatch.setenv("NCE_COGNITIVE_BACKEND", "onnx")
    monkeypatch.setenv("NCE_COGNITIVE_BATCH_WINDOW_MS", "5")
    monkeypatch.setenv("NCE_COGNITIVE_MAX_BATCH_SIZE", "32")
    monkeypatch.setenv("NCE_COGNITIVE_API_KEY", "secret-cog-key")

    transport = httpx.ASGITransport(app=cog_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        hz = await client.get("/healthz")
        assert hz.status_code == 200
        assert hz.json()["status"] == "ok"
        assert hz.json()["engine"] == "onnxruntime"

        rz_unready = await client.get("/readyz")
        assert rz_unready.status_code == 503

        cog_app._state.backend = "onnx"
        cog_app._state.engine = "onnxruntime"
        cog_app._state.embedding_dim = 768
        cog_app._state.ready_event.set()
        cog_app._state.load_error = None

        for path in ("/readyz", "/health"):
            r = await client.get(path)
            assert r.status_code == 200
            payload = r.json()
            assert payload["status"] == "ok"
            assert payload["engine"] == "onnxruntime"
            assert payload["backend"] == "onnx"
            assert payload["batch_window_ms"] == 5.0
            assert payload["max_batch_size"] == 32
            assert payload["dimension"] == 768

        unauth = await client.post("/v1/embeddings", json={"input": "hi"})
        assert unauth.status_code == 401

        monkeypatch.setattr(cog_app, "_encode_texts", lambda texts: [_unit_vec(0) for _ in texts])
        auth_ok = await client.post(
            "/v1/embeddings",
            json={"input": "hi"},
            headers={"Authorization": "Bearer secret-cog-key"},
        )
        assert auth_ok.status_code == 200
        assert len(auth_ok.json()["data"][0]["embedding"]) == 768

        nli_ok = await client.post(
            "/v1/nlp/nli",
            json={"premise": "Redis is enabled", "hypothesis": "Redis is disabled"},
            headers={"Authorization": "Bearer secret-cog-key"},
        )
        assert nli_ok.status_code == 200
        assert nli_ok.json()["score"] >= 0.7

    await cog_app._batcher.aclose()


# ---------------------------------------------------------------------------
# 4. Client pooling, coalescing & assert_cognitive_not_stub_in_prod()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_cognitive_not_stub_in_prod_accepts_onnx_and_rejects_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """assert_cognitive_not_stub_in_prod() accepts onnxruntime/onnxruntime-int8, rejects stub."""
    from nce import embeddings
    from nce.config import cfg

    monkeypatch.setattr(cfg, "ENVIRONMENT", "prod")
    monkeypatch.setattr(cfg, "NCE_BACKEND", "")
    monkeypatch.setattr(cfg, "NCE_COGNITIVE_BASE_URL", "http://127.0.0.1:11435")
    monkeypatch.setattr(cfg, "NCE_ALLOW_COGNITIVE_STUB", False)

    for accepted_engine in ("onnxruntime", "onnxruntime-int8"):
        ok_resp = SimpleNamespace(
            status_code=200,
            content=json.dumps({"status": "ok", "engine": accepted_engine}).encode(),
            json=lambda eng=accepted_engine: {"status": "ok", "engine": eng},
        )
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=ok_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            payload = await embeddings.assert_cognitive_not_stub_in_prod()
        assert payload is not None
        assert payload["engine"] == accepted_engine

    stub_resp = SimpleNamespace(
        status_code=200,
        content=b'{"status":"ok","engine":"stub"}',
        json=lambda: {"status": "ok", "engine": "stub"},
    )
    mock_stub_client = AsyncMock()
    mock_stub_client.get = AsyncMock(return_value=stub_resp)
    mock_stub_client.__aenter__ = AsyncMock(return_value=mock_stub_client)
    mock_stub_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_stub_client):
        with pytest.raises(RuntimeError, match="engine='stub'"):
            await embeddings.assert_cognitive_not_stub_in_prod()


@pytest.mark.asyncio
async def test_remote_backend_reuses_pooled_async_client_and_coalesces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CognitiveRemoteBackend reuses one per-loop AsyncClient and coalesces concurrent calls."""
    from nce import embeddings
    from nce.config import cfg

    embeddings.reset_backend_singleton_for_tests()
    monkeypatch.setattr(cfg, "NCE_BACKEND", "")
    monkeypatch.setattr(cfg, "NCE_COGNITIVE_BASE_URL", "http://127.0.0.1:11435")
    monkeypatch.setattr(cfg, "NCE_COGNITIVE_API_KEY", "")

    with patch(
        "httpx.Client.get",
        return_value=SimpleNamespace(json=lambda: {"engine": "onnxruntime"}),
    ):
        backend = embeddings.CognitiveRemoteBackend(batch_window_ms=5.0, max_batch_size=32)

    client_1 = backend._get_async_client()
    client_2 = backend._get_async_client()
    assert client_1 is client_2
    assert isinstance(client_1, httpx.AsyncClient)

    seen_clients: list[int] = []
    seen_batches: list[list[str]] = []

    async def fake_request_with_retry(client, method, url, **kwargs):
        seen_clients.append(id(client))
        texts = kwargs["json"]["input"]
        seen_batches.append(list(texts))
        return SimpleNamespace(
            json=lambda: {
                "object": "list",
                "data": [
                    {"index": idx, "embedding": _unit_vec(idx + 10)} for idx in range(len(texts))
                ],
            }
        )

    with (
        patch("nce.http_resilience.request_with_retry", side_effect=fake_request_with_retry),
        patch.object(embeddings, "get_backend", return_value=backend),
    ):
        # Concurrent single-text embeds coalesce into 1 HTTP POST over the same pooled client
        vecs = await asyncio.gather(*(embeddings.embed(f"q-{i}") for i in range(4)))
        assert len(vecs) == 4
        assert all(len(v) == embeddings.VECTOR_DIM for v in vecs)

        # Cached repeat hits memory cache without calling HTTP again
        cached = await embeddings.embed("  q-0  ")
        assert cached == pytest.approx(vecs[0])

        # Multi-text write uses embed_batch cleanly
        batch_vecs = await embeddings.embed_batch(["doc-a", "doc-b", "doc-c"])
        assert len(batch_vecs) == 3

    assert len(seen_batches) == 2
    assert seen_batches[0] == ["q-0", "q-1", "q-2", "q-3"]
    assert seen_batches[1] == ["doc-a", "doc-b", "doc-c"]
    assert set(seen_clients) == {id(client_1)}

    await embeddings.close_embedding_http_clients()
    assert client_1.is_closed
    embeddings.reset_backend_singleton_for_tests()


def test_remote_backend_isolates_clients_per_event_loop_and_evicts_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each event loop gets its own pooled AsyncClient and closed loops are evicted."""
    from nce import embeddings
    from nce.config import cfg

    embeddings.reset_backend_singleton_for_tests()
    monkeypatch.setattr(cfg, "NCE_BACKEND", "")
    monkeypatch.setattr(cfg, "NCE_COGNITIVE_BASE_URL", "http://127.0.0.1:11435")

    with patch(
        "httpx.Client.get",
        return_value=SimpleNamespace(json=lambda: {"engine": "onnxruntime"}),
    ):
        backend = embeddings.CognitiveRemoteBackend()

    clients: list[httpx.AsyncClient] = []

    async def _acquire_and_close() -> None:
        c = backend._get_async_client()
        assert backend._get_async_client() is c
        clients.append(c)
        await backend.aclose()
        assert c.is_closed

    asyncio.run(_acquire_and_close())
    asyncio.run(_acquire_and_close())

    assert len(clients) == 2
    assert clients[0] is not clients[1]
    assert len(backend._async_clients) == 0
    embeddings.reset_backend_singleton_for_tests()


def test_onnx_encode_texts_feeds_int64_and_server_shim_exports_app_and_main() -> None:
    """Verify ONNX inference feeds int64 tensors, pools output, and server.py exposes app/main."""
    cog_app = _load_cognitive_app()

    recorded_feeds: list[dict[str, np.ndarray]] = []

    class _FakeTokenizer:
        def __call__(self, texts, **_kwargs):
            n = len(texts)
            return {
                "input_ids": np.ones((n, 4), dtype=np.int64),
                "attention_mask": np.array([[1, 1, 1, 0]] * n, dtype=np.int64),
            }

    class _FakeSession:
        def run(self, _out_names, feed):
            recorded_feeds.append(feed)
            n = feed["input_ids"].shape[0]
            hidden = np.zeros((n, 4, 768), dtype=np.float32)
            hidden[:, :3, 0] = 3.0
            hidden[:, :3, 1] = 4.0
            hidden[:, 3, :] = 999.0  # masked out
            return [hidden]

    cog_app._state.backend = "onnx"
    cog_app._state.engine = "onnxruntime"
    cog_app._state.tokenizer = _FakeTokenizer()
    cog_app._state.ort_session = _FakeSession()
    cog_app._state.ort_input_names = ("input_ids", "attention_mask")
    cog_app._state.ort_output_names = ["last_hidden_state"]
    cog_app._state.ready_event.set()
    cog_app._state.load_error = None

    vecs = cog_app._encode_texts(["hello", ""])
    assert len(vecs) == 2
    assert len(recorded_feeds) == 1
    assert recorded_feeds[0]["input_ids"].dtype == np.int64
    assert recorded_feeds[0]["attention_mask"].dtype == np.int64
    for v in vecs:
        assert len(v) == 768
        assert v[0] == pytest.approx(0.6, abs=1e-5)
        assert v[1] == pytest.approx(0.8, abs=1e-5)
        assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-5

    server_spec = importlib.util.spec_from_file_location("nce_cognitive_server_test", SERVER_PATH)
    assert server_spec is not None and server_spec.loader is not None
    server_mod = importlib.util.module_from_spec(server_spec)
    server_spec.loader.exec_module(server_mod)
    assert callable(server_mod.main)
    assert server_mod.app is not None


def test_dockerfile_and_requirements_onnx_contract() -> None:
    """Verify multi-stage air-gapped ONNX export, parity check, and runtime contract."""
    df = DOCKERFILE_PATH.read_text(encoding="utf-8")
    reqs = REQUIREMENTS_PATH.read_text(encoding="utf-8")

    for expected_pin in (
        "onnx==",
        "onnxruntime==",
        "uvicorn[standard]==",
        "starlette==",
        "sentence-transformers==6.3.0",
        "transformers==5.23.0",
        "huggingface-hub==2.4.0",
        "tokenizers==0.23.3",
        "safetensors==0.8.0",
        "spacy==3.8.18",
        "numpy==2.5.5",
        "scipy==1.18.1",
        "scikit-learn==1.9.1",
    ):
        assert expected_pin in reqs

    for expected_df in (
        "AS builder",
        "AS runtime",
        "dynamo=False",
        "opset_version=17",
        "ORT_ENABLE_ALL",
        "quantize_dynamic",
        "QuantType.QInt8",
        "0.9999",
        "HF_HUB_OFFLINE=1",
        "TRANSFORMERS_OFFLINE=1",
        "NCE_MODEL_DIR=/opt/nce/models",
        "NCE_COGNITIVE_BACKEND=onnx",
        "USER 10001:10001",
        "onnxruntime-int8",
    ):
        assert expected_df in df


# ---------------------------------------------------------------------------
# 5. Dynamic thread defaulting, offline enforcement, parity check, & chunking
# ---------------------------------------------------------------------------


def test_num_threads_dynamic_default_and_clamping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """NCE_COGNITIVE_NUM_THREADS defaults to min(4, os.cpu_count() or 4) and clamps <=0."""
    cog_app = _load_cognitive_app()

    for cpu_val, expected in ((1, 1), (2, 2), (8, 4), (16, 4), (None, 4)):
        monkeypatch.setattr(os, "cpu_count", lambda val=cpu_val: val)
        monkeypatch.delenv("NCE_COGNITIVE_NUM_THREADS", raising=False)
        assert cog_app._num_threads() == expected

        monkeypatch.setenv("NCE_COGNITIVE_NUM_THREADS", "")
        assert cog_app._num_threads() == expected

        monkeypatch.setenv("NCE_COGNITIVE_NUM_THREADS", "   ")
        assert cog_app._num_threads() == expected

        monkeypatch.setenv("NCE_COGNITIVE_NUM_THREADS", "invalid-int")
        assert cog_app._num_threads() == expected

    for non_positive in ("0", "-1", "-16"):
        monkeypatch.setenv("NCE_COGNITIVE_NUM_THREADS", non_positive)
        assert cog_app._num_threads() == 1

    monkeypatch.setenv("NCE_COGNITIVE_NUM_THREADS", "6")
    assert cog_app._num_threads() == 6

    # Verify _create_ort_session passes dynamic thread count and ORT_ENABLE_ALL.
    recorded_opts: list[object] = []

    class _FakeOpts:
        def __init__(self) -> None:
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0
            self.execution_mode = None
            self.graph_optimization_level = None

    class _FakeSess:
        def __init__(self, _path: str, sess_options=None, providers=None) -> None:
            recorded_opts.append(sess_options)

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids"), SimpleNamespace(name="attention_mask")]

        def get_outputs(self):
            return [SimpleNamespace(name="last_hidden_state")]

    fake_ort = types.ModuleType("onnxruntime")
    fake_ort.SessionOptions = _FakeOpts  # type: ignore[attr-defined]
    fake_ort.ExecutionMode = SimpleNamespace(ORT_SEQUENTIAL="ORT_SEQUENTIAL")  # type: ignore[attr-defined]
    fake_ort.GraphOptimizationLevel = SimpleNamespace(ORT_ENABLE_ALL="ORT_ENABLE_ALL")  # type: ignore[attr-defined]
    fake_ort.InferenceSession = _FakeSess  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)

    monkeypatch.setattr(os, "cpu_count", lambda: 2)
    monkeypatch.delenv("NCE_COGNITIVE_NUM_THREADS", raising=False)
    cog_app._create_ort_session(tmp_path / "model.onnx")
    assert getattr(recorded_opts[-1], "intra_op_num_threads", None) == 2
    assert getattr(recorded_opts[-1], "graph_optimization_level", None) == "ORT_ENABLE_ALL"


@pytest.mark.asyncio
async def test_offline_env_enforced_and_readyz_reports_num_threads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Air-gapped HF offline flags are forced on boot and /readyz reports num_threads."""
    cog_app = _load_cognitive_app()

    onnx_dir = tmp_path / "all-mpnet-base-v2-onnx"
    onnx_dir.mkdir(parents=True)
    (onnx_dir / "model.onnx").write_bytes(b"fake-onnx")
    (onnx_dir / "metadata.json").write_text(
        json.dumps({"dimension": 768, "max_seq_length": 384}),
        encoding="utf-8",
    )

    tok_kwargs: list[dict] = []

    class _FakeOpts:
        def __init__(self) -> None:
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0
            self.execution_mode = None
            self.graph_optimization_level = None

    class _FakeSess:
        def __init__(self, _path: str, sess_options=None, providers=None) -> None:
            pass

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids"), SimpleNamespace(name="attention_mask")]

        def get_outputs(self):
            return [SimpleNamespace(name="last_hidden_state")]

    fake_ort = types.ModuleType("onnxruntime")
    fake_ort.SessionOptions = _FakeOpts  # type: ignore[attr-defined]
    fake_ort.ExecutionMode = SimpleNamespace(ORT_SEQUENTIAL="ORT_SEQUENTIAL")  # type: ignore[attr-defined]
    fake_ort.GraphOptimizationLevel = SimpleNamespace(ORT_ENABLE_ALL="ORT_ENABLE_ALL")  # type: ignore[attr-defined]
    fake_ort.InferenceSession = _FakeSess  # type: ignore[attr-defined]

    class _FakeTok:
        @staticmethod
        def from_pretrained(_source: str, **kwargs):
            tok_kwargs.append(kwargs)
            return MagicMock()

    fake_tf = types.ModuleType("transformers")
    fake_tf.AutoTokenizer = _FakeTok  # type: ignore[attr-defined]
    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda _name: MagicMock()  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "transformers", fake_tf)
    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setenv("NCE_MODEL_DIR", str(tmp_path))
    monkeypatch.setenv("NCE_COGNITIVE_BACKEND", "onnx")
    monkeypatch.setattr(os, "cpu_count", lambda: 2)
    monkeypatch.delenv("NCE_COGNITIVE_NUM_THREADS", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "")

    cog_app._load_models()
    assert cog_app._state.ready_event.is_set()
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert tok_kwargs[-1] == {"local_files_only": True}

    transport = httpx.ASGITransport(app=cog_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        rz = await client.get("/readyz")
    assert rz.status_code == 200
    payload = rz.json()
    assert payload["offline"] is True
    assert payload["num_threads"] == 2


def test_verify_onnx_pytorch_parity_thresholds_and_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """verify_onnx_pytorch_parity enforces >=0.9999 cosine parity, rejects drift/NaN, and supports CLI."""
    cog_app = _load_cognitive_app()

    st_dir = tmp_path / "all-mpnet-base-v2"
    onnx_dir = tmp_path / "all-mpnet-base-v2-onnx"
    st_dir.mkdir(parents=True)
    onnx_dir.mkdir(parents=True)
    (onnx_dir / "model.onnx").write_bytes(b"fake-fp32")
    (onnx_dir / "metadata.json").write_text(
        json.dumps({"dimension": 768, "max_seq_length": 384}),
        encoding="utf-8",
    )

    def _vecs_for(n: int, offset: int = 1) -> np.ndarray:
        return np.array([_unit_vec(i + offset) for i in range(n)], dtype=np.float32)

    mode = {"behavior": "match"}
    st_kwargs: list[dict] = []
    tok_kwargs: list[dict] = []
    ort_levels: list[object] = []

    class _FakeST:
        def __init__(self, _source: str, **kwargs) -> None:
            st_kwargs.append(kwargs)

        def encode(self, encode_samples, **_kwargs):
            return _vecs_for(len(encode_samples), offset=1)

    class _FakeTok:
        @staticmethod
        def from_pretrained(_source: str, **kwargs):
            tok_kwargs.append(kwargs)
            return lambda texts, **_kw: {
                "input_ids": np.ones((len(texts), 2), dtype=np.int64),
                "attention_mask": np.ones((len(texts), 2), dtype=np.int64),
            }

    class _FakeOpts:
        def __init__(self) -> None:
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0
            self.execution_mode = None
            self.graph_optimization_level = None

    class _FakeSess:
        def __init__(self, _path: str, sess_options=None, providers=None) -> None:
            ort_levels.append(getattr(sess_options, "graph_optimization_level", None))

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids"), SimpleNamespace(name="attention_mask")]

        def get_outputs(self):
            return [SimpleNamespace(name="last_hidden_state")]

        def run(self, _out_names, feed):
            n = int(feed["input_ids"].shape[0])
            base = _vecs_for(n, offset=1)
            if mode["behavior"] == "match":
                out = np.repeat(base[:, None, :], 2, axis=1)
            elif mode["behavior"] == "drift":
                drifted = _vecs_for(n, offset=10)
                out = np.repeat(drifted[:, None, :], 2, axis=1)
            else:
                out = np.repeat(base[:, None, :], 2, axis=1).copy()
                out[min(1, n - 1), :, 0] = np.nan
            return [out]

    fake_st = types.ModuleType("sentence_transformers")
    fake_st.SentenceTransformer = _FakeST  # type: ignore[attr-defined]
    fake_tf = types.ModuleType("transformers")
    fake_tf.AutoTokenizer = _FakeTok  # type: ignore[attr-defined]
    fake_ort = types.ModuleType("onnxruntime")
    fake_ort.SessionOptions = _FakeOpts  # type: ignore[attr-defined]
    fake_ort.ExecutionMode = SimpleNamespace(ORT_SEQUENTIAL="ORT_SEQUENTIAL")  # type: ignore[attr-defined]
    fake_ort.GraphOptimizationLevel = SimpleNamespace(ORT_ENABLE_ALL="ORT_ENABLE_ALL")  # type: ignore[attr-defined]
    fake_ort.InferenceSession = _FakeSess  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setitem(sys.modules, "transformers", fake_tf)
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)

    samples = ["alpha text", "beta text"]
    ok_report = cog_app.verify_onnx_pytorch_parity(
        samples, model_dir=tmp_path, backend="onnx", min_cosine=0.9999
    )
    assert ok_report["ok"] is True
    assert ok_report["min_cosine"] >= 0.9999
    assert ok_report["max_norm_err"] < 1e-5
    assert st_kwargs[-1] == {"device": "cpu", "local_files_only": True}
    assert tok_kwargs[-1] == {"local_files_only": True}
    assert ort_levels[-1] == "ORT_ENABLE_ALL"

    mode["behavior"] = "drift"
    drift_report = cog_app.verify_onnx_pytorch_parity(
        samples, model_dir=tmp_path, backend="onnx", min_cosine=0.9999
    )
    assert drift_report["ok"] is False
    assert drift_report["min_cosine"] < 0.9999

    mode["behavior"] = "nan"
    nan_report = cog_app.verify_onnx_pytorch_parity(
        samples, model_dir=tmp_path, backend="onnx", min_cosine=0.9999
    )
    assert nan_report["ok"] is False

    mode["behavior"] = "match"
    rc_ok = cog_app.main(["--verify-parity", "--model-dir", str(tmp_path), "--backend", "onnx"])
    assert rc_ok == 0
    cli_ok = json.loads(capsys.readouterr().out)
    assert cli_ok["ok"] is True
    assert cli_ok["min_cosine"] >= 0.9999

    mode["behavior"] = "drift"
    rc_fail = cog_app.main(["--verify-parity", "--model-dir", str(tmp_path), "--backend", "onnx"])
    assert rc_fail == 1
    cli_fail = json.loads(capsys.readouterr().out)
    assert cli_fail["ok"] is False


@pytest.mark.asyncio
async def test_embed_batch_delegation_single_post_bounded_chunking_and_no_torch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-text delegation sends one batched POST (or bounded chunks) without importing torch."""
    from nce import embeddings
    from nce.config import cfg

    embeddings.reset_backend_singleton_for_tests()
    monkeypatch.setattr(cfg, "NCE_BACKEND", "")
    monkeypatch.setattr(cfg, "NCE_COGNITIVE_BASE_URL", "http://127.0.0.1:11435")
    monkeypatch.setattr(cfg, "NCE_COGNITIVE_API_KEY", "")
    monkeypatch.setattr(cfg, "EMBED_BATCH_CHUNK", 64)
    # Poison torch so any in-process torch import raises ModuleNotFoundError.
    monkeypatch.setitem(sys.modules, "torch", None)

    with patch(
        "httpx.Client.get",
        return_value=SimpleNamespace(json=lambda: {"engine": "onnxruntime"}),
    ):
        detected = embeddings.detect_backend()
    assert isinstance(detected, embeddings.CognitiveRemoteBackend)

    async_posts: list[list[str]] = []
    sync_posts: list[list[str]] = []

    async def fake_async_retry(_client, method, url, **kwargs):
        assert method == "POST"
        assert url.endswith("/v1/embeddings")
        batch = list(kwargs["json"]["input"])
        async_posts.append(batch)
        return SimpleNamespace(
            json=lambda: {
                "data": [{"index": i, "embedding": _unit_vec(i + 1)} for i in range(len(batch))]
            }
        )

    def fake_sync_retry(_client, method, url, **kwargs):
        assert method == "POST"
        assert url.endswith("/v1/embeddings")
        batch = list(kwargs["json"]["input"])
        sync_posts.append(batch)
        return SimpleNamespace(
            json=lambda: {
                "data": [{"index": i, "embedding": _unit_vec(i + 5)} for i in range(len(batch))]
            }
        )

    with (
        patch("nce.http_resilience.request_with_retry", side_effect=fake_async_retry),
        patch("nce.http_resilience.request_with_retry_sync", side_effect=fake_sync_retry),
        patch.object(embeddings, "get_backend", return_value=detected),
    ):
        # 5 texts within chunk cap -> single batched HTTP POST (no per-item round-trips).
        vecs_5 = await embeddings.embed_batch(["t0", "t1", "t2", "t3", "t4"])
        assert len(vecs_5) == 5
        assert async_posts == [["t0", "t1", "t2", "t3", "t4"]]

        # 8 texts with chunk cap 3 -> bounded chunks [3, 3, 2].
        async_posts.clear()
        monkeypatch.setattr(cfg, "EMBED_BATCH_CHUNK", 3)
        vecs_8 = await embeddings.embed_batch([f"doc-{i}" for i in range(8)])
        assert len(vecs_8) == 8
        assert [len(b) for b in async_posts] == [3, 3, 2]

        # Direct CognitiveRemoteBackend with max_batch_size=3 also bounds async & sync POSTs.
        with patch(
            "httpx.Client.get",
            return_value=SimpleNamespace(json=lambda: {"engine": "onnxruntime"}),
        ):
            bounded_backend = embeddings.CognitiveRemoteBackend(
                batch_window_ms=0.0, max_batch_size=3
            )
        async_posts.clear()
        direct_vecs = await bounded_backend.embed([f"direct-{i}" for i in range(8)])
        assert len(direct_vecs) == 8
        assert [len(b) for b in async_posts] == [3, 3, 2]

        sync_vecs, degraded = bounded_backend._sync_embed_batch([f"sync-{i}" for i in range(8)])
        assert degraded is False
        assert len(sync_vecs) == 8
        assert [len(b) for b in sync_posts] == [3, 3, 2]

        await bounded_backend.aclose()

    await embeddings.close_embedding_http_clients()
    embeddings.reset_backend_singleton_for_tests()
