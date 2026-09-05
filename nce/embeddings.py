"""
Phase 5 / Phase 1 enterprise — Jina embeddings with hardware backend abstraction (§8.2).

``EmbeddingBackend`` routes inference through CPU / CUDA / ROCm / XPU / OpenVINO NPU / MPS.
Module-level ``embed`` and ``embed_batch`` preserve the public contract used by the
orchestrator and RQ worker: async APIs, dimensions from ``cfg.EMBEDDING.VECTOR_DIM``,
thread-pool offload for blocking stacks.

Vector dimension and cosine semantics are unchanged — PostgreSQL pgvector ingestion is unaffected.

Degraded-flag design note
--------------------------
``degraded_embedding_flag`` is a ``ContextVar`` that must only be written from the
**async task context** — not from inside a threadpool executor. All concrete backends
return ``(vectors, degraded: bool)`` from ``_sync_embed_batch``; the base ``embed()``
method sets the flag in the async context after ``run_in_executor`` completes.
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
import threading
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from typing import TYPE_CHECKING, cast

from nce.config import cfg
from nce.observability import EMBEDDING_COUNT, EMBEDDING_FALLBACKS

if TYPE_CHECKING:
    pass

log = logging.getLogger("nce-embeddings")

degraded_embedding_flag: ContextVar[bool] = ContextVar("degraded_embedding_flag", default=False)

# Model ID and dimension resolved once at import from config — never hardcoded.
MODEL_ID: str = cfg.NCE_EMBEDDING_MODEL_ID
VECTOR_DIM: int = cfg.EMBEDDING.VECTOR_DIM

# Model encode is not thread-safe across mixed backends — worker count is configurable.
_executor = ThreadPoolExecutor(
    max_workers=cfg.EMBEDDING_MAX_WORKERS, thread_name_prefix="nce-embed"
)

# Lazy singleton selected by ``detect_backend()``, guarded by double-checked lock.
_backend: EmbeddingBackend | None = None
_backend_lock = threading.Lock()


def shutdown_embedding_executor() -> None:
    """Gracefully drain the embedding executor. Call during app shutdown."""
    _executor.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# Vector helpers
# ---------------------------------------------------------------------------


# Squared-norm floor below which a vector is treated as degenerate rather than
# merely small. Matches ``_l2_normalize``'s own 1e-12 no-op threshold (squared),
# so the two cannot disagree about what "≈ 0" means.
_DEGENERATE_NORM_SQ: float = 1e-24


def _l2_normalize(vec: list[float]) -> list[float]:
    """Return the L2-normalised copy of *vec*. No-op when norm ≈ 0."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 1e-12:
        return vec
    return [x / norm for x in vec]


def _deterministic_hash_embedding(text: str) -> list[float]:
    """Deterministic L2-normalised fallback vector for CI / degraded operation.

    Uses SHA-256 (not MD5) for the seed so there is no reason to use an
    insecure hash in new code. Identical inputs always produce identical,
    normalised vectors of length ``VECTOR_DIM``.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big")
    rng = random.Random(seed)
    return _l2_normalize([rng.uniform(-1.0, 1.0) for _ in range(VECTOR_DIM)])


def _fallback_batch(texts: list[str]) -> list[list[float]]:
    """Return deterministic fallback vectors for all texts."""
    return [_deterministic_hash_embedding(t) for t in texts]


def _fallback_with_error(
    texts: list[str],
    msg: str,
    *args,
    is_warn: bool = False,
) -> tuple[list[list[float]], bool]:
    """Log an error or warning and return deterministic fallback vectors and degraded=True."""
    if is_warn:
        log.warning(msg, *args)
    else:
        log.error(msg, *args)
    return _fallback_batch(texts), True


def _validate_batch(
    texts: list[str],
    vectors: list[list[float]],
    *,
    backend_name: str,
) -> tuple[list[list[float]], bool]:
    """Validate embedding count and dimension; return (normalised vectors, degraded).

    On any mismatch, returns fallback deterministic vectors and ``degraded=True``
    rather than allowing bad-dimensioned data into pgvector.
    """
    if len(vectors) != len(texts):
        log.error(
            "%s embedding count mismatch: got %d expected %d — falling back",
            backend_name,
            len(vectors),
            len(texts),
        )
        return _fallback_batch(texts), True

    for i, v in enumerate(vectors):
        if len(v) != VECTOR_DIM:
            log.error(
                "%s returned vector dim %d at index %d; expected %d — falling back",
                backend_name,
                len(v),
                i,
                VECTOR_DIM,
            )
            return _fallback_batch(texts), True

        # A DEGENERATE vector is not a valid embedding, and it is far more
        # dangerous than a wrong-dimensioned one.  ``_l2_normalize`` below
        # deliberately no-ops when the norm is ~0, so an all-zero vector would
        # pass straight through here with ``degraded=False`` -- reported healthy.
        #
        # Why that matters more than it looks: a zero vector is EQUIDISTANT from
        # every other vector, and cosine similarity against it is 0/0.  Recall
        # therefore does not fail, it returns arbitrary ordering that looks like
        # a result set.  Silence, not an error.
        #
        # This was live: the bundled cognitive sidecar stub answers
        # ``POST /v1/embeddings`` with a 768-dim ZERO vector (see
        # ``deploy/cognitive-stub/stub_server.py``), and every memory embedded
        # through it was stored un-recallable with nothing logged.
        norm_sq = sum(x * x for x in v)
        if norm_sq <= _DEGENERATE_NORM_SQ:
            return _fallback_with_error(
                texts,
                "%s returned a DEGENERATE (near-zero) vector at index %d: "
                "|v|^2=%.3g. A zero vector is equidistant from everything, so "
                "similarity search would silently return arbitrary results. "
                "Falling back to deterministic vectors and marking the batch "
                "degraded. If the backend is the cognitive sidecar, check "
                "whether it is the stub (GET /health -> engine).",
                backend_name,
                i,
                norm_sq,
            )

    return [_l2_normalize(v) for v in vectors], False


# ---------------------------------------------------------------------------
# Hardware detection helpers
# ---------------------------------------------------------------------------


def _is_rocm_available() -> bool:
    try:
        import torch

        hip = getattr(torch.version, "hip", None)
        return bool(hip)
    except (ImportError, RuntimeError):
        return False


def _is_mps_available() -> bool:
    try:
        import torch

        return bool(
            hasattr(torch.backends, "mps")
            and torch.backends.mps.is_available()
            and torch.backends.mps.is_built()
        )
    except (ImportError, RuntimeError):
        return False


def _is_xpu_available() -> bool:
    try:
        import torch

        return bool(hasattr(torch, "xpu") and torch.xpu.is_available())
    except (ImportError, RuntimeError):
        return False


def _is_npu_available() -> bool:
    """
    Conservative auto-detect: OpenVINO Runtime exposes an NPU device, or the user
    pre-declares intent via NCE_OPENVINO_MODEL_DIR (export present).
    """
    model_dir = cfg.NCE_OPENVINO_MODEL_DIR
    if model_dir and __import__("os").path.isdir(model_dir):
        return bool(model_dir_path_has_openvino_xml(model_dir))

    try:
        from openvino import Core

        core = Core()
        devs = [d for d in core.available_devices if "NPU" in d.upper()]
        return bool(devs)
    except (ImportError, RuntimeError, OSError):
        return False


def model_dir_path_has_openvino_xml(model_dir: str) -> list[str]:
    """Return basenames of .xml files that look like OpenVINO IR."""
    import os

    try:
        names = []
        for root, _, files in os.walk(model_dir):
            for f in files:
                if f.endswith(".xml"):
                    names.append(f)
            break
        return names
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Model loaders
# ---------------------------------------------------------------------------

from functools import lru_cache  # noqa: E402


@lru_cache(maxsize=8)
def _load_sentence_transformer(device: str):
    """Load SentenceTransformer once per logical device string."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        log.warning("sentence_transformers not installed.")
        return None

    trust = cfg.NCE_EMBEDDING_TRUST_REMOTE_CODE
    revision = cfg.NCE_EMBEDDING_MODEL_REVISION or None  # None = latest

    if trust:
        log.info(
            "Loading embedding model %s (revision=%s) on device %r with trust_remote_code=True",
            MODEL_ID,
            revision or "latest",
            device,
        )
    else:
        log.info(
            "Loading embedding model %s (revision=%s) on device %r",
            MODEL_ID,
            revision or "latest",
            device,
        )

    try:
        kwargs: dict = {"trust_remote_code": trust, "device": device}
        if revision:
            kwargs["revision"] = revision
        model = SentenceTransformer(MODEL_ID, **kwargs)
        log.info("Embedding model ready (device=%r).", device)
        return model
    except Exception as e:
        log.warning("Could not load %s on %r: %s", MODEL_ID, device, e)
        return None


@lru_cache(maxsize=2)
def _load_openvino_npu_bundle(model_dir: str, seq_len: int):
    """Compile the pre-exported OpenVINO IR with the RAW OpenVINO runtime.

    Deliberately does **not** use ``optimum.intel``. ``optimum-intel`` 2.1.0 requires
    ``transformers<5.6`` while this repo pins ``transformers>=5.14.1``, so pinning both is
    ``ResolutionImpossible``; unpinned, pip silently backtracks to ``optimum-intel==1.15.0``
    (2024), which cannot detect OpenVINO and raises the misleading ``ImportError:
    OVModelForFeatureExtraction requires the openvino library but it was not found in your
    environment`` *while ``openvino`` is installed and importable*. The raw runtime works:
    ``Core().compile_model(<dir>/openvino_model.xml, "NPU")``.

    Compilation failures (no NPU, bad IR) are allowed to propagate — the caller already
    routes them to ``_fallback_with_error``.
    """
    import os

    from openvino import Core
    from transformers import AutoTokenizer

    xml_names = model_dir_path_has_openvino_xml(model_dir)
    if not xml_names:
        raise FileNotFoundError(f"No OpenVINO IR (.xml) found in {model_dir!r}")
    xml_name = "openvino_model.xml" if "openvino_model.xml" in xml_names else xml_names[0]

    compiled = Core().compile_model(os.path.join(model_dir, xml_name), "NPU")
    tokenizer = AutoTokenizer.from_pretrained(
        model_dir, trust_remote_code=cfg.NCE_EMBEDDING_TRUST_REMOTE_CODE
    )
    return compiled, tokenizer, seq_len


# ---------------------------------------------------------------------------
# Abstract backend — _sync_embed_batch returns (vectors, degraded)
# ---------------------------------------------------------------------------


class EmbeddingBackend(ABC):
    """Abstract embedding provider; batch-first API (§8.2).

    Concrete implementations must return ``(vectors, degraded)`` from
    ``_sync_embed_batch``. The base ``embed()`` propagates the degraded
    flag back to the calling async context via ``degraded_embedding_flag``.
    """

    @abstractmethod
    def _sync_embed_batch(self, texts: list[str]) -> tuple[list[list[float]], bool]:
        """Blocking batch encode.

        Returns ``(vectors, degraded)`` where:
        - ``vectors`` length equals ``len(texts)``
        - ``degraded`` is ``True`` whenever a fallback path was used
        """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        if not texts:
            return []
        loop = asyncio.get_running_loop()
        vectors, degraded = await loop.run_in_executor(_executor, self._sync_embed_batch, texts)
        # Set the flag in the async task context — NOT inside the executor thread.
        degraded_embedding_flag.set(degraded)
        if degraded:
            EMBEDDING_FALLBACKS.inc()
            try:
                from nce.notifications import dispatcher

                await dispatcher.dispatch_alert(
                    "Embedding Fallback Active",
                    "The primary embedding backend failed and degraded operation (hash-stub fallback) was triggered.",
                )
            except Exception:
                log.exception("Failed to dispatch alert for embedding fallback")
        return vectors


# ---------------------------------------------------------------------------
# Torch-based backends (CPU, CUDA, ROCm, XPU, MPS)
# ---------------------------------------------------------------------------


class TorchEmbeddingBackend(EmbeddingBackend):
    """Base class for PyTorch-based embedding backends using SentenceTransformer."""

    @abstractmethod
    def get_device(self) -> str:
        """Return the PyTorch device name or string."""

    def get_backend_name(self) -> str:
        """Return the name of this backend for logging."""
        return self.__class__.__name__.replace("Backend", "")

    def _sync_embed_batch(self, texts: list[str]) -> tuple[list[list[float]], bool]:
        device = self.get_device()
        model = _load_sentence_transformer(device)
        if model is None:
            return _fallback_with_error(
                texts,
                "%s: model unavailable — using deterministic fallback vectors",
                self.get_backend_name(),
                is_warn=True,
            )
        try:
            raw = model.encode(
                texts,
                normalize_embeddings=True,
                batch_size=min(32, len(texts)),
                show_progress_bar=False,
            )
            vectors = [v.tolist() for v in raw]
        except Exception as e:
            return _fallback_with_error(
                texts,
                "%s batch embedding failed: %s",
                self.get_backend_name(),
                e,
            )

        # Validate count + dimension before returning.
        return _validate_batch(texts, vectors, backend_name=self.get_backend_name())


class CPUBackend(TorchEmbeddingBackend):
    """Baseline torch CPU / SentenceTransformer."""

    def get_device(self) -> str:
        return "cpu"


class CUDABackend(TorchEmbeddingBackend):
    """NVIDIA CUDA — PyTorch CUDA device (non-ROCm wheels)."""

    def get_device(self) -> str:
        return "cuda"


class ROCmBackend(TorchEmbeddingBackend):
    """AMD ROCm — PyTorch exposes CUDA APIs on ROCm builds."""

    def get_device(self) -> str:
        return "cuda"

    def get_backend_name(self) -> str:
        return "ROCm"


class XPUBackend(TorchEmbeddingBackend):
    """Intel GPU via torch.xpu."""

    def get_device(self) -> str:
        return "xpu"


class MPSBackend(TorchEmbeddingBackend):
    """Apple Silicon MPS."""

    def get_device(self) -> str:
        return "mps"


# ---------------------------------------------------------------------------
# OpenVINO NPU backend
# ---------------------------------------------------------------------------


def _mean_pool(last_hidden_state, attention_mask):
    import torch

    mask = attention_mask.unsqueeze(-1).expand(last_hidden_state.size()).float()
    summed = torch.sum(last_hidden_state * mask, dim=1)
    counts = torch.clamp(mask.sum(dim=1), min=1e-9)
    return summed / counts


class OpenVINONPUBackend(EmbeddingBackend):
    """
    Intel NPU via pre-exported OpenVINO IR (static shapes). Requires NCE_OPENVINO_MODEL_DIR.
    Long texts: truncated to seq_len (export-time bound); full token chunking can be layered later.
    """

    def __init__(self, model_dir: str | None = None):
        self.model_dir = (model_dir or cfg.NCE_OPENVINO_MODEL_DIR or "").strip()
        if not self.model_dir:
            log.warning("OpenVINONPUBackend: NCE_OPENVINO_MODEL_DIR not set — will stub.")

    def _sync_embed_batch(self, texts: list[str]) -> tuple[list[list[float]], bool]:
        import os

        if not self.model_dir or not os.path.isdir(self.model_dir):
            return _fallback_with_error(
                texts,
                "OpenVINONPUBackend: model directory %r not found or not set",
                self.model_dir,
                is_warn=True,
            )
        seq_len = cfg.NCE_OPENVINO_SEQ_LEN
        try:
            model, tokenizer, _ = _load_openvino_npu_bundle(self.model_dir, seq_len)
        except Exception as e:
            return _fallback_with_error(
                texts,
                "OpenVINO NPU load failed: %s",
                e,
            )

        try:
            import numpy as np
            import torch

            encoded = tokenizer(
                texts,
                padding="max_length",
                truncation=True,
                max_length=seq_len,
                return_tensors="pt",
            )
            # The raw OpenVINO runtime takes a dict of numpy arrays.
            inputs = {k: v.numpy() for k, v in encoded.items()}
            # The feed dict must cover EVERY input the IR declares, and nothing else.
            required = [port.get_any_name() for port in model.inputs]
            out_port = model.output(0)

            # IMPORTANT: the exported IR has STATIC shapes [1, seq_len], so batch size is 1:
            # the whole batch CANNOT be fed at once. Run one inference per text and
            # assemble the vectors in input order.
            vectors = []
            for i in range(len(texts)):
                row = {k: v[i : i + 1] for k, v in inputs.items()}
                # A declared input the tokenizer did not produce is supplied as ZEROS.
                # This IR declares token_type_ids while the tokenizer returns only
                # input_ids/attention_mask; omitting it makes the plugin read an
                # UNINITIALISED buffer — stable within a process and different across
                # processes (measured cosine(CPU, NPU) drifting 0.999999 -> 0.395 on
                # identical input). Zeros is what the model was exported with.
                feed = {
                    name: row[name] if name in row else np.zeros_like(row["input_ids"])
                    for name in required
                }
                res = model(feed)
                last = torch.tensor(np.asarray(res[out_port]))
                mask = encoded["attention_mask"][i : i + 1]
                pooled = _mean_pool(last, mask)
                pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
                vectors.append(pooled.detach().cpu().numpy().astype(np.float64)[0].tolist())
        except Exception as e:
            return _fallback_with_error(
                texts,
                "OpenVINO NPU inference failed: %s",
                e,
            )

        return _validate_batch(texts, cast(list[list[float]], vectors), backend_name="OpenVINONPU")


# ---------------------------------------------------------------------------
# Remote cognitive backend
# ---------------------------------------------------------------------------


def validated_cognitive_base_url(raw: str | None = None) -> str:
    """Return a validated cognitive base URL (no trailing slash) or empty string."""
    from nce.providers.base import validate_base_url

    base = (raw if raw is not None else cfg.NCE_COGNITIVE_BASE_URL or "").strip().rstrip("/")
    if not base:
        return ""
    validate_base_url(base, allow_http=True, allow_loopback=True)
    return base


def cognitive_health_check_url(raw: str | None = None) -> str:
    """Build ``/health`` URL for the cognitive sidecar after SSRF validation."""
    base = validated_cognitive_base_url(raw)
    if base:
        return f"{base}/health"
    return "http://localhost:11435/health"


class CognitiveRemoteBackend(EmbeddingBackend):
    """
    OpenAI-compatible ``/v1/embeddings`` against the bundled cognitive image [D2/D7].

    Base URL example: ``http://cognitive:11435`` (no trailing ``/v1`` — it is appended).
    Auth: optional ``NCE_COGNITIVE_API_KEY`` sent as ``Bearer`` if non-empty.
    Fallback model: ``NCE_COGNITIVE_FALLBACK_MODEL`` (from config, not os.getenv).
    """

    def __init__(self) -> None:
        from nce.providers.base import LLMProviderError

        self._model = (cfg.NCE_COGNITIVE_EMBEDDING_MODEL or "").strip() or None
        try:
            self._base = validated_cognitive_base_url()
        except LLMProviderError as exc:
            log.warning("CognitiveRemoteBackend: rejecting base URL: %s", exc)
            self._base = ""
        if self._base:
            self._warn_if_sidecar_is_a_stub()

    def _warn_if_sidecar_is_a_stub(self) -> None:
        """Say so, loudly, when the sidecar we delegate to is a stub.

        The stub ANNOUNCES itself -- ``GET /health`` returns
        ``{"status": "ok", "engine": "stub"}`` -- and nothing used to look. So a
        deployment could delegate every embedding to a placeholder that returns
        zero vectors, with a green health check and no log line saying the
        cognitive layer was not real.

        Best-effort by construction: a probe failure is NOT fatal, because the
        sidecar being briefly unreachable at import time is normal and the
        request path already retries. Only a sidecar that positively identifies
        itself as a stub is reported.
        """
        engine = ""
        try:
            import httpx

            with httpx.Client(timeout=2.0) as client:
                r = client.get(cognitive_health_check_url(self._base))
                engine = str((r.json() or {}).get("engine", "")).strip().lower()
        except Exception as exc:  # noqa: BLE001 - a probe must never break startup
            log.debug("CognitiveRemoteBackend: stub probe inconclusive: %s", exc)
            return

        if engine != "stub":
            return

        msg = (
            "COGNITIVE SIDECAR AT %s IS A STUB (GET /health reports "
            'engine="stub"). It returns ZERO vectors, so embeddings are not '
            "real and semantic recall over anything written now will be "
            "meaningless. Point NCE_COGNITIVE_BASE_URL at a real model server, "
            "or unset it so nce.embeddings.detect_backend() falls through to "
            "the in-process hardware chain (CUDA/ROCm/XPU/OpenVINO-NPU/CPU)."
        )
        if cfg.ENVIRONMENT in {"prod", "production"}:
            log.critical(msg, self._base)
            raise RuntimeError(
                f"Refusing to start: cognitive sidecar at {self._base} is a stub "
                "and NCE_ENV is production. A stub returns zero vectors; storing "
                "them is silent data loss."
            )
        log.warning(msg, self._base)

    def _sync_embed_batch(self, texts: list[str]) -> tuple[list[list[float]], bool]:
        if not self._base:
            return _fallback_with_error(
                texts,
                "CognitiveRemoteBackend: empty base URL — stubbing.",
                is_warn=True,
            )

        import httpx

        api_key = cfg.NCE_COGNITIVE_API_KEY
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        url = f"{self._base}/v1/embeddings"
        payload: dict = {"input": texts}
        if self._model:
            payload["model"] = self._model

        from nce.http_resilience import request_with_retry_sync

        try:
            with httpx.Client(timeout=120.0) as client:
                r = request_with_retry_sync(
                    client,
                    "POST",
                    url,
                    json=payload,
                    headers=headers,
                    operation_name="embedding_sidecar:primary",
                )
                data = r.json()
        except Exception as e:
            # Classify the error so we can decide whether to try the fallback model.
            is_rate_limit = isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429
            is_timeout = isinstance(e, httpx.TimeoutException)
            if not (is_rate_limit or is_timeout):
                err_str = str(e).lower()
                if "rate limit" in err_str or "429" in err_str:
                    is_rate_limit = True
                elif "timeout" in err_str or "timed out" in err_str:
                    is_timeout = True

            if is_rate_limit or is_timeout:
                reason = "RateLimit" if is_rate_limit else "Timeout"
                log.warning(
                    "Primary embedding model failed due to %s. "
                    "Falling back to smaller model. Error: %s",
                    reason,
                    e,
                )
                # Fallback model from config — never os.getenv().
                fallback_model = cfg.NCE_COGNITIVE_FALLBACK_MODEL
                log.info("CognitiveRemoteBackend: triggering fallback model: %s", fallback_model)

                payload_fallback = dict(payload)
                payload_fallback["model"] = fallback_model

                try:
                    with httpx.Client(timeout=60.0) as client:
                        r = request_with_retry_sync(
                            client,
                            "POST",
                            url,
                            json=payload_fallback,
                            headers=headers,
                            operation_name="embedding_sidecar:fallback",
                        )
                        data = r.json()
                except Exception as fe:
                    return _fallback_with_error(
                        texts,
                        "Fallback embedding model also failed: %s",
                        fe,
                    )
            else:
                return _fallback_with_error(
                    texts,
                    "Cognitive embedding HTTP failed: %s",
                    e,
                )

        rows = data.get("data") if isinstance(data, dict) else None
        if not rows or not isinstance(rows, list):
            return _fallback_with_error(
                texts,
                "Cognitive embedding response missing data[]: %s",
                data,
            )

        # OpenAI-style payloads include ``index``; sort defensively when batched.
        indexed: list[tuple[int, list[float]]] = []
        for item in rows:
            if not isinstance(item, dict):
                return _fallback_with_error(
                    texts,
                    "Invalid embedding row from cognitive: %r",
                    item,
                )
            emb = item.get("embedding")
            if not isinstance(emb, list):
                return _fallback_with_error(
                    texts,
                    "Invalid embedding row from cognitive: %r",
                    item,
                )
            idx = int(item["index"]) if "index" in item else len(indexed)
            indexed.append((idx, [float(x) for x in emb]))
        indexed.sort(key=lambda t: t[0])
        vectors = [v for _, v in indexed]

        # Validate count + dimension; returns (fallback, degraded=True) on mismatch.
        return _validate_batch(texts, vectors, backend_name="CognitiveRemote")


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------

_BACKEND_BUILDERS = {
    "cpu": CPUBackend,
    "cuda": CUDABackend,
    "rocm": ROCmBackend,
    "xpu": XPUBackend,
    "openvino_npu": OpenVINONPUBackend,
    "openvino": OpenVINONPUBackend,
    "mps": MPSBackend,
}


def detect_backend() -> EmbeddingBackend:
    """
    Select backend from NCE_BACKEND or auto-detect (§8.2 / §8.4 parity with Go wizard).

    When ``NCE_COGNITIVE_BASE_URL`` is set and ``NCE_BACKEND`` is unset or unknown,
    embeddings use the bundled cognitive HTTP API [D2/D7] instead of loading SentenceTransformer.
    """

    pref = cfg.NCE_BACKEND
    cognitive_url = (cfg.NCE_COGNITIVE_BASE_URL or "").strip()

    if pref:
        if pref not in _BACKEND_BUILDERS:
            log.warning("Unknown NCE_BACKEND=%r — falling back to auto-detect.", pref)
        else:
            log.info("Embedding backend forced by NCE_BACKEND=%s", pref)
            return _BACKEND_BUILDERS[pref]()

    if cognitive_url:
        log.info(
            "Embedding backend: CognitiveRemoteBackend (%s) [NCE_LLM_PROVIDER=%s]",
            cognitive_url,
            cfg.NCE_LLM_PROVIDER or "local-cognitive-model",
        )
        return CognitiveRemoteBackend()

    try:
        import torch
    except ImportError:
        log.warning("torch not importable — using CPU backend stub path.")
        return CPUBackend()

    if torch.cuda.is_available() and not _is_rocm_available():
        log.info("Auto-selected CUDABackend (CUDA available, not ROCm).")
        return CUDABackend()
    if _is_rocm_available() and torch.cuda.is_available():
        log.info("Auto-selected ROCmBackend.")
        return ROCmBackend()
    if _is_xpu_available():
        log.info("Auto-selected XPUBackend.")
        return XPUBackend()
    if _is_npu_available():
        log.info("Auto-selected OpenVINONPUBackend.")
        return OpenVINONPUBackend()
    if _is_mps_available():
        log.info("Auto-selected MPSBackend.")
        return MPSBackend()

    log.info("Auto-selected CPUBackend.")
    return CPUBackend()


def get_backend() -> EmbeddingBackend:
    """Return the module-level backend singleton (double-checked lock, thread-safe)."""
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                _backend = detect_backend()
    return _backend


def reset_backend_singleton_for_tests() -> None:
    """Test hook: clear lazy singleton so ``detect_backend`` runs again."""
    global _backend
    with _backend_lock:
        _backend = None


# ---------------------------------------------------------------------------
# Public async API (stable for orchestrator / graph / worker)
# ---------------------------------------------------------------------------


async def embed(text: str) -> list[float]:
    """Embed a single text; returns a VECTOR_DIM-length L2-normalised vector."""
    degraded_embedding_flag.set(False)
    vecs = await get_backend().embed([text])
    return vecs[0] if vecs else _deterministic_hash_embedding(text)


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """
    Chunked batches at ``cfg.EMBED_BATCH_CHUNK`` so large AST runs yield to the event loop.

    Input limits (from config):
    - ``NCE_EMBED_MAX_BATCH_TEXTS``: hard cap on number of texts per call.
    - ``NCE_EMBED_MAX_TEXT_CHARS``: each text is truncated to this length.

    Raises ``ValueError`` when the batch count exceeds the limit.
    """
    import asyncio

    if not texts:
        return []

    if len(texts) > cfg.NCE_EMBED_MAX_BATCH_TEXTS:
        raise ValueError(
            f"embed_batch called with {len(texts)} texts; "
            f"limit is {cfg.NCE_EMBED_MAX_BATCH_TEXTS} (NCE_EMBED_MAX_BATCH_TEXTS). "
            "Split the batch before calling."
        )

    # Per-text character truncation — prevents tokenizer surprises and cost spikes.
    max_chars = cfg.NCE_EMBED_MAX_TEXT_CHARS
    texts = [t[:max_chars] for t in texts]

    degraded_embedding_flag.set(False)
    EMBEDDING_COUNT.labels(model_id=MODEL_ID).inc(len(texts))

    backend = get_backend()
    results: list[list[float]] = []
    for start in range(0, len(texts), cfg.EMBED_BATCH_CHUNK):
        chunk = texts[start : start + cfg.EMBED_BATCH_CHUNK]
        chunk_vecs = await backend.embed(chunk)
        results.extend(chunk_vecs)
        # Yield to the event loop between chunks so the embedding executor
        # does not monopolise the async thread for large batches.
        await asyncio.sleep(0)
    return results
