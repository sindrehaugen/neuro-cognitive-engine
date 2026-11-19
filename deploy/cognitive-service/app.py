"""Pure-ASGI air-gapped cognitive service runtime (ONNX Runtime + PyTorch fallback + spaCy).

Serves OpenAI-compatible 768-dimensional unit-norm embeddings via ONNX Runtime
(FP32 or dynamic INT8) or CPU SentenceTransformer (``all-mpnet-base-v2``), real
spaCy ``en_core_web_sm`` NER + dependency SVO extraction, semantic NLI
contradiction scoring, and JSON-schema-guided chat completions.

Endpoints:
  GET  /healthz              — sub-millisecond liveness probe (zero model I/O)
  GET  /readyz               — readiness probe (200 once models loaded, else 503)
  GET  /health               — model metadata & engine identification
  POST /v1/embeddings        — OpenAI-compatible micro-batched 768-d embeddings
  POST /v1/nlp/spacy         — spaCy NER, noun chunks & dependency SVO triples
  POST /v1/nlp/nli           — semantic + polarity contradiction score in [0, 1]
  POST /v1/chat/completions  — structured JSON completion fallback
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import math
import os
import re
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

try:
    import orjson as _orjson
except ImportError:  # pragma: no cover - fallback when orjson is absent
    _orjson = None  # type: ignore[assignment]


def _enforce_offline_env() -> None:
    """Ensure air-gapped offline HuggingFace/Transformers flags are active when unset or blank."""
    for key, default_val in (
        ("HF_HUB_OFFLINE", "1"),
        ("TRANSFORMERS_OFFLINE", "1"),
        ("TOKENIZERS_PARALLELISM", "false"),
    ):
        if not os.environ.get(key, "").strip():
            os.environ[key] = default_val


# Default to air-gapped offline operation so runtime containers never reach HuggingFace Hub.
_enforce_offline_env()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [cognitive-service] %(levelname)s %(message)s",
)
log = logging.getLogger("nce.cognitive_service")

DEFAULT_MODEL_ID = "sentence-transformers/all-mpnet-base-v2"
DEFAULT_SPACY_MODEL_ID = "en_core_web_sm"
DEFAULT_MODEL_DIR = "/opt/nce/models"
MAX_BODY_BYTES = 10 * 1024 * 1024

_BACKEND_TO_ENGINE: dict[str, str] = {
    "onnx": "onnxruntime",
    "onnx-int8": "onnxruntime-int8",
    "torch": "sentence-transformers",
}

_ENTITY_TYPE_MAP = {
    "ORG": "ORG",
    "PRODUCT": "TOOL",
    "PERSON": "PERSON",
    "GPE": "PLACE",
    "LOC": "PLACE",
    "WORK_OF_ART": "CONCEPT",
    "LANGUAGE": "CONCEPT",
    "LAW": "CONCEPT",
}

_NEGATION_RE = re.compile(
    r"\b(not|no|never|none|neither|nor|cannot|can't|won't|wouldn't|don't|doesn't|"
    r"didn't|isn't|aren't|wasn't|weren't|hasn't|haven't|false|disabled|denied|off)\b",
    re.IGNORECASE,
)

_ANTONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("enable", "disable"),
    ("enabled", "disabled"),
    ("allow", "deny"),
    ("allowed", "denied"),
    ("active", "inactive"),
    ("true", "false"),
    ("increase", "decrease"),
    ("open", "close"),
    ("public", "private"),
    ("always", "never"),
    ("present", "absent"),
)

_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")

_DEFAULT_PARITY_SENTENCES: tuple[str, ...] = (
    "NCE cognitive build verification sentence.",
    "PostgreSQL row-level security enforces multi-tenant isolation across partitioned tables.",
    "async def embed_batch(texts: list[str]) -> list[list[float]]: return []",
    "Short text.",
    (
        "A longer semantic paragraph comparing vector cosine similarity, "
        "L2 normalization, and attention-mask weighted mean pooling over padded sequences."
    ),
)


def _port() -> int:
    return int(os.environ.get("COGNITIVE_PORT", "11435"))


def _model_id() -> str:
    return os.environ.get("NCE_EMBEDDING_MODEL_ID", DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID


def _spacy_model_id() -> str:
    return os.environ.get("NCE_SPACY_MODEL_ID", DEFAULT_SPACY_MODEL_ID).strip() or (
        DEFAULT_SPACY_MODEL_ID
    )


def _model_dir() -> str:
    return os.environ.get("NCE_MODEL_DIR", DEFAULT_MODEL_DIR).strip() or DEFAULT_MODEL_DIR


def _resolve_backend() -> str:
    raw = (os.environ.get("NCE_COGNITIVE_BACKEND", "onnx") or "onnx").strip().lower()
    if raw == "onnx_int8":
        raw = "onnx-int8"
    if raw not in _BACKEND_TO_ENGINE:
        raise ValueError(
            f"Unsupported NCE_COGNITIVE_BACKEND={raw!r}; expected one of "
            f"{sorted(_BACKEND_TO_ENGINE)}"
        )
    return raw


def _engine_for_backend(backend: str) -> str:
    return _BACKEND_TO_ENGINE.get(backend, "onnxruntime")


def _batch_window_ms() -> float:
    try:
        return max(0.0, float(os.environ.get("NCE_COGNITIVE_BATCH_WINDOW_MS", "5")))
    except ValueError:
        return 5.0


def _max_batch_size() -> int:
    try:
        return max(1, int(os.environ.get("NCE_COGNITIVE_MAX_BATCH_SIZE", "32")))
    except ValueError:
        return 32


def _num_threads() -> int:
    default = max(1, min(4, os.cpu_count() or 4))
    raw = os.environ.get("NCE_COGNITIVE_NUM_THREADS")
    if raw is None or not raw.strip():
        return default
    try:
        return max(1, int(raw.strip()))
    except ValueError:
        return default


def _api_key() -> str:
    return (
        os.environ.get("NCE_COGNITIVE_API_KEY", "").strip()
        or os.environ.get("COGNITIVE_API_KEY", "").strip()
    )


@dataclass
class _CognitiveState:
    ready_event: threading.Event = field(default_factory=threading.Event)
    loading: bool = False
    shutting_down: bool = False
    backend: str = "onnx"
    engine: str = "onnxruntime"
    st_model: Any = None
    ort_session: Any = None
    ort_input_names: tuple[str, ...] = ("input_ids", "attention_mask")
    ort_output_names: list[str] | None = None
    tokenizer: Any = None
    nlp_model: Any = None
    embedding_dim: int = 768
    max_seq_length: int = 384
    load_error: str | None = None


_state = _CognitiveState()
_model_lock = threading.Lock()
_spacy_lock = threading.Lock()


def reset_state_for_tests() -> None:
    """Reset runtime state and micro-batcher queues between unit tests."""
    global _state
    _state = _CognitiveState()
    _batcher.reset_sync()


def mean_pool_and_normalize(
    last_hidden_state: np.ndarray,
    attention_mask: np.ndarray,
) -> np.ndarray:
    """Apply attention-mask-weighted mean pooling and float32 L2 normalization."""
    hidden = np.asarray(last_hidden_state, dtype=np.float32)
    mask = np.asarray(attention_mask, dtype=np.float32)
    if mask.ndim == 2:
        mask_expanded = mask[:, :, None]
    elif mask.ndim == 3:
        mask_expanded = mask
    else:
        raise ValueError(f"Unexpected attention_mask ndim={mask.ndim}; expected 2 or 3")
    summed = np.sum(hidden * mask_expanded, axis=1, dtype=np.float32)
    counts = np.maximum(np.sum(mask_expanded, axis=1, dtype=np.float32), np.float32(1e-9))
    pooled = (summed / counts).astype(np.float32, copy=False)
    norms = np.linalg.norm(pooled, ord=2, axis=-1, keepdims=True).astype(np.float32, copy=False)
    norms = np.maximum(norms, np.float32(1e-12))
    return (pooled / norms).astype(np.float32, copy=False)


def _create_ort_session(onnx_path: Path) -> tuple[Any, tuple[str, ...], list[str] | None]:
    import onnxruntime as ort

    sess_opts = ort.SessionOptions()
    sess_opts.intra_op_num_threads = _num_threads()
    sess_opts.inter_op_num_threads = 1
    sess_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(
        str(onnx_path),
        sess_options=sess_opts,
        providers=["CPUExecutionProvider"],
    )
    in_names = (
        tuple(inp.name for inp in session.get_inputs())
        if hasattr(session, "get_inputs")
        else ("input_ids", "attention_mask")
    )
    out_names = (
        [out.name for out in session.get_outputs()]
        if hasattr(session, "get_outputs")
        else ["last_hidden_state"]
    )
    return session, in_names, out_names or ["last_hidden_state"]


def _load_onnx_metadata(onnx_dir: Path) -> tuple[int, int]:
    dim = 768
    max_seq = 384
    meta_path = onnx_dir / "metadata.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                if isinstance(meta.get("dimension"), int) and meta["dimension"] > 0:
                    dim = int(meta["dimension"])
                if isinstance(meta.get("max_seq_length"), int) and meta["max_seq_length"] > 0:
                    max_seq = int(meta["max_seq_length"])
        except Exception as exc:
            log.warning("Failed reading %s: %s", meta_path, exc)
    return dim, max_seq


def _load_models() -> None:
    """Load baked ONNX/SentenceTransformer and spaCy models strictly offline."""
    _state.loading = True
    try:
        import spacy

        _enforce_offline_env()
        backend = _resolve_backend()
        engine = _engine_for_backend(backend)
        model_dir = Path(_model_dir())
        model_id = _model_id()
        spacy_id = _spacy_model_id()

        _state.backend = backend
        _state.engine = engine

        if backend == "torch":
            from sentence_transformers import SentenceTransformer

            st_dir = model_dir / "all-mpnet-base-v2"
            source = str(st_dir) if (model_dir.is_dir() and st_dir.is_dir()) else model_id
            log.info(
                "Loading SentenceTransformer backend=%s source=%s (local_files_only=True)...",
                backend,
                source,
            )
            st = SentenceTransformer(source, device="cpu", local_files_only=True)
            dim = st.get_sentence_embedding_dimension()
            if isinstance(dim, int) and dim > 0:
                _state.embedding_dim = dim
            max_seq = getattr(st, "max_seq_length", None)
            if isinstance(max_seq, int) and max_seq > 0:
                _state.max_seq_length = max_seq
            _state.st_model = st
            _state.ort_session = None
            _state.tokenizer = None
        else:
            from transformers import AutoTokenizer

            onnx_dir = model_dir / "all-mpnet-base-v2-onnx"
            filename = "model_qint8.onnx" if backend == "onnx-int8" else "model.onnx"
            onnx_override = os.environ.get("NCE_COGNITIVE_ONNX_PATH", "").strip()
            if model_dir.is_dir() and onnx_dir.is_dir():
                tok_source = str(onnx_dir)
                onnx_path = Path(onnx_override) if onnx_override else (onnx_dir / filename)
                dim, max_seq = _load_onnx_metadata(onnx_dir)
            else:
                # Fallback to local HuggingFace cache only when baked directory does not exist.
                tok_source = model_id
                onnx_path = Path(onnx_override) if onnx_override else (onnx_dir / filename)
                dim, max_seq = 768, 384

            log.info(
                "Loading ONNX backend=%s tokenizer=%s model=%s (local_files_only=True)...",
                backend,
                tok_source,
                onnx_path,
            )
            tokenizer = AutoTokenizer.from_pretrained(tok_source, local_files_only=True)
            session, in_names, out_names = _create_ort_session(onnx_path)
            _state.tokenizer = tokenizer
            _state.ort_session = session
            _state.ort_input_names = in_names
            _state.ort_output_names = out_names
            _state.embedding_dim = dim
            _state.max_seq_length = max_seq
            _state.st_model = None

        log.info("Loading spaCy model %s...", spacy_id)
        nlp = spacy.load(spacy_id)

        _state.nlp_model = nlp
        _state.load_error = None
        _state.ready_event.set()
        log.info(
            "Cognitive service ready (engine=%s, backend=%s, model=%s, dim=%d, spacy=%s)",
            _state.engine,
            _state.backend,
            model_id,
            _state.embedding_dim,
            spacy_id,
        )
    except Exception as exc:
        _state.load_error = f"{type(exc).__name__}: {exc}"
        log.exception("Cognitive model initialization failed: %s", _state.load_error)
    finally:
        _state.loading = False


def _is_model_ready() -> bool:
    return _state.ready_event.is_set() and _state.load_error is None


def _ensure_ready_sync(timeout: float = 30.0) -> None:
    if _is_model_ready():
        return
    if _state.loading and not _state.ready_event.is_set():
        _state.ready_event.wait(timeout=timeout)
    if not _is_model_ready():
        detail = _state.load_error or "models are not ready"
        raise RuntimeError(f"Cognitive service not ready: {detail}")


async def _ensure_ready_async(timeout: float = 30.0) -> None:
    if _is_model_ready():
        return
    if _state.loading:
        await asyncio.to_thread(_state.ready_event.wait, timeout)
    if not _is_model_ready():
        detail = _state.load_error or "models are not ready"
        raise RuntimeError(f"Cognitive service not ready: {detail}")


def _encode_onnx_chunk(chunk: list[str]) -> list[list[float]]:
    encoded = _state.tokenizer(
        chunk,
        padding=True,
        truncation=True,
        max_length=_state.max_seq_length,
        return_tensors="np",
    )
    input_ids = np.asarray(encoded["input_ids"], dtype=np.int64)
    attention_mask = np.asarray(encoded["attention_mask"], dtype=np.int64)
    feed: dict[str, np.ndarray] = {}
    declared = set(_state.ort_input_names)
    if not declared or "input_ids" in declared:
        feed["input_ids"] = input_ids
    if not declared or "attention_mask" in declared:
        feed["attention_mask"] = attention_mask
    if "token_type_ids" in declared:
        feed["token_type_ids"] = np.asarray(
            encoded.get("token_type_ids", np.zeros_like(input_ids)),
            dtype=np.int64,
        )

    out_names = _state.ort_output_names or ["last_hidden_state"]
    with _model_lock:
        outputs = _state.ort_session.run(out_names, feed)
    vectors = mean_pool_and_normalize(outputs[0], attention_mask)
    return vectors.astype(np.float32, copy=False).tolist()


def _encode_texts(texts: list[str]) -> list[list[float]]:
    """Encode a list of texts into L2-normalized embedding vectors."""
    if not texts:
        return []
    _ensure_ready_sync()
    cleaned = [t if t else " " for t in texts]
    chunk_size = _max_batch_size()

    if _state.ort_session is not None and _state.tokenizer is not None:
        if len(cleaned) <= chunk_size:
            return _encode_onnx_chunk(cleaned)
        out: list[list[float]] = []
        for start in range(0, len(cleaned), chunk_size):
            out.extend(_encode_onnx_chunk(cleaned[start : start + chunk_size]))
        return out

    if _state.st_model is None:
        raise RuntimeError("Cognitive service not ready: embedding model not loaded")

    with _model_lock:
        vectors = _state.st_model.encode(
            cleaned,
            normalize_embeddings=True,
            convert_to_numpy=True,
            batch_size=min(chunk_size, len(cleaned)),
            show_progress_bar=False,
        )
    return np.asarray(vectors, dtype=np.float32).tolist()


@dataclass
class _BatchItem:
    texts: list[str]
    future: asyncio.Future[list[list[float]]]


@dataclass
class _LoopBatchState:
    queue: asyncio.Queue[_BatchItem]
    worker_task: asyncio.Task[None]


class EmbeddingMicroBatcher:
    """Async micro-batcher coalescing concurrent embedding requests up to window/max_batch."""

    def __init__(self) -> None:
        self._loops: dict[asyncio.AbstractEventLoop, _LoopBatchState] = {}

    def _prune_closed_loops(self) -> None:
        for lp in [k for k in self._loops if k.is_closed()]:
            self._loops.pop(lp, None)

    def reset_sync(self) -> None:
        for lp, state in list(self._loops.items()):
            if not lp.is_closed() and not state.worker_task.done():
                state.worker_task.cancel()
        self._loops.clear()

    async def aclose(self) -> None:
        loop = asyncio.get_running_loop()
        state = self._loops.pop(loop, None)
        if state is not None and not state.worker_task.done():
            state.worker_task.cancel()
            await asyncio.gather(state.worker_task, return_exceptions=True)
        self._prune_closed_loops()

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._get_or_start(loop)

    def _get_or_start(self, loop: asyncio.AbstractEventLoop) -> _LoopBatchState:
        self._prune_closed_loops()
        state = self._loops.get(loop)
        if state is None or state.worker_task.done():
            queue: asyncio.Queue[_BatchItem] = asyncio.Queue()
            worker = loop.create_task(self._worker(loop, queue))
            state = _LoopBatchState(queue=queue, worker_task=worker)
            self._loops[loop] = state
        return state

    async def enqueue(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        loop = asyncio.get_running_loop()
        state = self._get_or_start(loop)
        fut: asyncio.Future[list[list[float]]] = loop.create_future()
        await state.queue.put(_BatchItem(texts=list(texts), future=fut))
        return await fut

    async def _execute_batch(self, batch: list[_BatchItem]) -> None:
        active = [item for item in batch if not item.future.done()]
        if not active:
            return

        flat_texts = [t for item in active for t in item.texts]
        try:
            vectors = await asyncio.to_thread(_encode_texts, flat_texts)
            if len(vectors) != len(flat_texts):
                raise ValueError(
                    f"embedding count mismatch: got {len(vectors)} expected {len(flat_texts)}"
                )
            offset = 0
            for item in active:
                n = len(item.texts)
                item_vecs = vectors[offset : offset + n]
                offset += n
                if not item.future.done():
                    item.future.set_result(item_vecs)
        except Exception as exc:
            if len(active) == 1:
                if not active[0].future.done():
                    active[0].future.set_exception(exc)
                return

            # Strict per-request error isolation: re-run individually when a coalesced batch fails.
            for item in active:
                if item.future.done():
                    continue
                try:
                    item_vecs = await asyncio.to_thread(_encode_texts, item.texts)
                    if len(item_vecs) != len(item.texts):
                        raise ValueError(
                            f"embedding count mismatch: got {len(item_vecs)} "
                            f"expected {len(item.texts)}"
                        )
                    if not item.future.done():
                        item.future.set_result(item_vecs)
                except Exception as item_exc:
                    if not item.future.done():
                        item.future.set_exception(item_exc)

    async def _worker(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[_BatchItem],
    ) -> None:
        pending: _BatchItem | None = None
        try:
            while True:
                if pending is not None:
                    first = pending
                    pending = None
                else:
                    first = await queue.get()

                if first.future.done():
                    continue

                window_s = _batch_window_ms() / 1000.0
                max_batch = _max_batch_size()
                batch: list[_BatchItem] = [first]
                total_texts = len(first.texts)

                while total_texts < max_batch:
                    try:
                        nxt = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if nxt.future.done():
                        continue
                    if total_texts + len(nxt.texts) > max_batch:
                        pending = nxt
                        break
                    batch.append(nxt)
                    total_texts += len(nxt.texts)

                if pending is None and total_texts < max_batch and window_s > 0.0:
                    deadline = loop.time() + window_s
                    while total_texts < max_batch:
                        remaining = deadline - loop.time()
                        if remaining <= 0.0:
                            break
                        try:
                            nxt = await asyncio.wait_for(queue.get(), timeout=remaining)
                        except asyncio.TimeoutError:
                            break
                        if nxt.future.done():
                            continue
                        if total_texts + len(nxt.texts) > max_batch:
                            pending = nxt
                            break
                        batch.append(nxt)
                        total_texts += len(nxt.texts)

                await self._execute_batch(batch)
        except asyncio.CancelledError:
            err = RuntimeError("Cognitive service shutting down")
            if pending is not None and not pending.future.done():
                pending.future.set_exception(err)
            while True:
                try:
                    queued = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if not queued.future.done():
                    queued.future.set_exception(err)
            raise


_batcher = EmbeddingMicroBatcher()


def _spacy_extract(text: str) -> dict[str, Any]:
    _ensure_ready_sync()
    if _state.nlp_model is None:
        raise RuntimeError("Cognitive service not ready: spaCy model not loaded")
    with _spacy_lock:
        doc = _state.nlp_model((text or "")[:4096])

    nodes: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add_node(label: str, entity_type: str, source_text: str, *, min_len: int = 1) -> None:
        cleaned = label.strip()
        key = cleaned.lower()
        if cleaned and len(cleaned) >= min_len and key not in seen:
            nodes.append(
                {
                    "label": cleaned,
                    "entity_type": entity_type,
                    "source_text": source_text,
                }
            )
            seen.add(key)

    for ent in doc.ents:
        _add_node(ent.text, _ENTITY_TYPE_MAP.get(ent.label_, "UNKNOWN"), ent.text)

    for chunk in doc.noun_chunks:
        _add_node(chunk.root.lemma_, "CONCEPT", chunk.text, min_len=3)

    edges: list[dict[str, Any]] = []
    for token in doc:
        if token.pos_ == "VERB" and token.dep_ in ("ROOT", "relcl", "advcl"):
            subj = next((c for c in token.lefts if c.dep_ in ("nsubj", "nsubjpass")), None)
            obj = next(
                (c for c in token.rights if c.dep_ in ("dobj", "attr", "prep", "pobj")),
                None,
            )
            if subj and obj:
                edges.append(
                    {
                        "subject_label": subj.lemma_,
                        "predicate": token.lemma_,
                        "object_label": obj.lemma_,
                        "confidence": 0.85,
                    }
                )

    return {"nodes": nodes, "edges": edges}


def _score_contradiction_from_vectors(
    premise: str,
    hypothesis: str,
    vectors: list[list[float]],
) -> float:
    sim = sum(a * b for a, b in zip(vectors[0], vectors[1], strict=False))
    sim = max(-1.0, min(1.0, sim))

    p_negs = len(_NEGATION_RE.findall(premise))
    h_negs = len(_NEGATION_RE.findall(hypothesis))
    neg_conflict = (p_negs % 2) != (h_negs % 2)

    p_words = set(re.findall(r"[a-z]+", premise.lower()))
    h_words = set(re.findall(r"[a-z]+", hypothesis.lower()))
    antonym_conflict = any(
        (a in p_words and b in h_words) or (b in p_words and a in h_words)
        for a, b in _ANTONYM_PAIRS
    )

    p_nums = set(_NUMBER_RE.findall(premise))
    h_nums = set(_NUMBER_RE.findall(hypothesis))
    numeric_conflict = bool(p_nums and h_nums and p_nums != h_nums and sim >= 0.55)

    if (neg_conflict or antonym_conflict or numeric_conflict) and sim >= 0.35:
        score = 0.80 + 0.15 * max(0.0, sim)
    elif neg_conflict or antonym_conflict:
        score = 0.65
    else:
        score = 0.45 * (1.0 - max(0.0, sim))

    return round(max(0.0, min(1.0, float(score))), 6)


async def _nli_contradiction_score_async(premise: str, hypothesis: str) -> float:
    vectors = await _batcher.enqueue([premise, hypothesis])
    return _score_contradiction_from_vectors(premise, hypothesis, vectors)


def _nli_contradiction_score(premise: str, hypothesis: str) -> float:
    """Estimate contradiction probability in [0.0, 1.0] using cosine similarity + polarity."""
    vectors = _encode_texts([premise, hypothesis])
    return _score_contradiction_from_vectors(premise, hypothesis, vectors)


async def _schema_fallback_object_async(
    schema: dict[str, Any], messages: list[dict[str, Any]]
) -> dict[str, Any]:
    """Generate a schema-valid JSON response using semantic heuristics when available."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}

    joined_prompt = " ".join(str(m.get("content", "")) for m in messages)
    if "is_contradiction" in properties and "confidence" in properties:
        existing_m = re.search(r"<existing_memory>(.*?)</existing_memory>", joined_prompt, re.S)
        new_m = re.search(r"<new_memory>(.*?)</new_memory>", joined_prompt, re.S)
        if existing_m and new_m:
            score = await _nli_contradiction_score_async(existing_m.group(1), new_m.group(1))
            is_contra = score >= 0.70
            return {
                "is_contradiction": is_contra,
                "confidence": score if is_contra else round(1.0 - score, 4),
                "explanation": (
                    "Detected semantic polarity or numeric conflict between statements."
                    if is_contra
                    else "Statements do not present a direct factual contradiction."
                ),
            }

    out: dict[str, Any] = {}
    for prop_name, prop_def in properties.items():
        if not isinstance(prop_def, dict):
            out[prop_name] = None
            continue
        if "enum" in prop_def and isinstance(prop_def["enum"], list) and prop_def["enum"]:
            out[prop_name] = prop_def["enum"][0]
            continue
        ptype = prop_def.get("type")
        if ptype == "boolean":
            out[prop_name] = False
        elif ptype == "integer":
            out[prop_name] = 0
        elif ptype == "number":
            out[prop_name] = 0.5
        elif ptype == "array":
            out[prop_name] = []
        elif ptype == "object":
            out[prop_name] = await _schema_fallback_object_async(prop_def, messages)
        else:
            out[prop_name] = ""
    return out


def _json_response(payload: dict[str, Any], status_code: int = 200) -> Response:
    if _orjson is not None:
        content = _orjson.dumps(payload)
    else:
        content = json.dumps(payload).encode("utf-8")
    return Response(content=content, status_code=status_code, media_type="application/json")


def _check_auth(request: Request) -> bool:
    expected = _api_key()
    if not expected:
        return True
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    supplied = auth[7:].strip()
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


async def _read_json_body(request: Request) -> tuple[dict[str, Any] | None, int, str | None]:
    raw_len = request.headers.get("content-length")
    if raw_len is not None:
        try:
            length = int(raw_len)
        except ValueError:
            return None, 400, "invalid Content-Length"
        if length < 0:
            return None, 400, "negative Content-Length"
        if length > MAX_BODY_BYTES:
            return None, 413, "payload too large"

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return None, 413, "payload too large"
        chunks.append(chunk)

    raw = b"".join(chunks) if chunks else b"{}"
    try:
        parsed = _orjson.loads(raw) if _orjson is not None else json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return None, 400, f"invalid JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, 400, "request body must be a JSON object"
    return parsed, 200, None


async def healthz(_request: Request) -> Response:
    """Sub-millisecond in-memory liveness probe (zero model I/O)."""
    try:
        backend = _resolve_backend()
    except ValueError:
        backend = _state.backend
    engine = _engine_for_backend(backend) if not _is_model_ready() else _state.engine
    if _state.shutting_down:
        return _json_response(
            {
                "status": "shutting_down",
                "service": "cognitive",
                "engine": engine,
                "backend": backend,
            },
            status_code=503,
        )
    return _json_response(
        {
            "status": "ok",
            "service": "cognitive",
            "engine": engine,
            "backend": backend,
        },
        status_code=200,
    )


async def readyz(_request: Request) -> Response:
    """Readiness and metadata probe (200 when models loaded, else 503)."""
    try:
        backend = _state.backend if _is_model_ready() else _resolve_backend()
    except ValueError as exc:
        return _json_response(
            {
                "status": "error",
                "service": "cognitive",
                "engine": _state.engine,
                "backend": _state.backend,
                "model": _model_id(),
                "error": str(exc),
            },
            status_code=503,
        )

    engine = _state.engine if _is_model_ready() else _engine_for_backend(backend)
    if _state.shutting_down or not _is_model_ready():
        return _json_response(
            {
                "status": "shutting_down"
                if _state.shutting_down
                else ("starting" if _state.load_error is None else "error"),
                "service": "cognitive",
                "engine": engine,
                "backend": backend,
                "model": _model_id(),
                "error": _state.load_error,
            },
            status_code=503,
        )
    return _json_response(
        {
            "status": "ok",
            "service": "cognitive",
            "engine": _state.engine,
            "backend": _state.backend,
            "model": _model_id(),
            "spacy_model": _spacy_model_id(),
            "embedding_dim": _state.embedding_dim,
            "dimension": _state.embedding_dim,
            "batch_window_ms": _batch_window_ms(),
            "max_batch_size": _max_batch_size(),
            "num_threads": _num_threads(),
            "offline": True,
        },
        status_code=200,
    )


async def embeddings_endpoint(request: Request) -> Response:
    if not _check_auth(request):
        return _json_response({"error": "unauthorized"}, status_code=401)
    body, status, err = await _read_json_body(request)
    if body is None:
        return _json_response({"error": err or "bad request"}, status_code=status)

    raw_input = body.get("input", "")
    texts = [str(item) for item in raw_input] if isinstance(raw_input, list) else [str(raw_input)]
    if not texts:
        return _json_response(
            {
                "object": "list",
                "model": str(body.get("model") or _model_id()),
                "data": [],
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            },
            status_code=200,
        )

    try:
        await _ensure_ready_async()
        vectors = await _batcher.enqueue(texts)
    except RuntimeError as exc:
        return _json_response({"error": str(exc)}, status_code=503)
    except Exception as exc:
        log.exception("Unhandled error on /v1/embeddings")
        return _json_response({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    token_estimate = sum(max(1, len(t.split())) for t in texts)
    return _json_response(
        {
            "object": "list",
            "model": str(body.get("model") or _model_id()),
            "data": [
                {"object": "embedding", "index": idx, "embedding": vec}
                for idx, vec in enumerate(vectors)
            ],
            "usage": {
                "prompt_tokens": token_estimate,
                "total_tokens": token_estimate,
            },
        },
        status_code=200,
    )


async def nlp_spacy_endpoint(request: Request) -> Response:
    if not _check_auth(request):
        return _json_response({"error": "unauthorized"}, status_code=401)
    body, status, err = await _read_json_body(request)
    if body is None:
        return _json_response({"error": err or "bad request"}, status_code=status)
    try:
        await _ensure_ready_async()
        result = await asyncio.to_thread(_spacy_extract, str(body.get("text") or ""))
        return _json_response(result, status_code=200)
    except RuntimeError as exc:
        return _json_response({"error": str(exc)}, status_code=503)
    except Exception as exc:
        log.exception("Unhandled error on /v1/nlp/spacy")
        return _json_response({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


async def nlp_nli_endpoint(request: Request) -> Response:
    if not _check_auth(request):
        return _json_response({"error": "unauthorized"}, status_code=401)
    body, status, err = await _read_json_body(request)
    if body is None:
        return _json_response({"error": err or "bad request"}, status_code=status)
    try:
        await _ensure_ready_async()
        premise = str(body.get("premise") or "")
        hypothesis = str(body.get("hypothesis") or "")
        score = await _nli_contradiction_score_async(premise, hypothesis)
        return _json_response({"score": score}, status_code=200)
    except RuntimeError as exc:
        return _json_response({"error": str(exc)}, status_code=503)
    except Exception as exc:
        log.exception("Unhandled error on /v1/nlp/nli")
        return _json_response({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


async def chat_completions_endpoint(request: Request) -> Response:
    if not _check_auth(request):
        return _json_response({"error": "unauthorized"}, status_code=401)
    body, status, err = await _read_json_body(request)
    if body is None:
        return _json_response({"error": err or "bad request"}, status_code=status)
    try:
        await _ensure_ready_async()
        messages = body.get("messages")
        msg_list = messages if isinstance(messages, list) else []
        fmt = body.get("response_format") or {}
        schema = (fmt.get("json_schema") or {}).get("schema") if isinstance(fmt, dict) else None
        if isinstance(schema, dict):
            content = json.dumps(await _schema_fallback_object_async(schema, msg_list))
        else:
            content = json.dumps({"status": "ok", "model": _model_id()})
        return _json_response(
            {
                "id": "chatcmpl-nce-local",
                "object": "chat.completion",
                "model": str(body.get("model") or _model_id()),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            },
            status_code=200,
        )
    except RuntimeError as exc:
        return _json_response({"error": str(exc)}, status_code=503)
    except Exception as exc:
        log.exception("Unhandled error on /v1/chat/completions")
        return _json_response({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


async def not_found_endpoint(request: Request) -> Response:
    return _json_response({"error": f"not found: {request.url.path}"}, status_code=404)


def verify_onnx_pytorch_parity(
    texts: list[str] | None = None,
    *,
    model_dir: str | Path | None = None,
    backend: str = "onnx",
    min_cosine: float = 0.9999,
) -> dict[str, Any]:
    """Compare baked PyTorch SentenceTransformer vs ONNX Runtime cosine similarity."""
    from sentence_transformers import SentenceTransformer
    from transformers import AutoTokenizer

    _enforce_offline_env()
    norm_backend = (backend or "onnx").strip().lower()
    if norm_backend == "onnx_int8":
        norm_backend = "onnx-int8"
    if norm_backend not in ("onnx", "onnx-int8"):
        raise ValueError(f"Unsupported parity backend={backend!r}; expected 'onnx' or 'onnx-int8'")

    samples = list(texts) if texts else list(_DEFAULT_PARITY_SENTENCES)
    base_dir = Path(model_dir) if model_dir is not None else Path(_model_dir())
    st_dir = base_dir / "all-mpnet-base-v2"
    onnx_dir = base_dir / "all-mpnet-base-v2-onnx"
    onnx_file = onnx_dir / ("model_qint8.onnx" if norm_backend == "onnx-int8" else "model.onnx")

    if not st_dir.is_dir():
        raise FileNotFoundError(f"Baked SentenceTransformer directory not found: {st_dir}")
    if not onnx_file.is_file():
        raise FileNotFoundError(f"Baked ONNX model not found: {onnx_file}")

    dim, max_seq = _load_onnx_metadata(onnx_dir)
    st = SentenceTransformer(str(st_dir), device="cpu", local_files_only=True)
    pt_vecs = np.asarray(
        st.encode(samples, normalize_embeddings=True, convert_to_numpy=True),
        dtype=np.float32,
    )

    tokenizer = AutoTokenizer.from_pretrained(str(onnx_dir), local_files_only=True)
    encoded = tokenizer(
        samples,
        padding=True,
        truncation=True,
        max_length=max_seq,
        return_tensors="np",
    )
    session, in_names, out_names = _create_ort_session(onnx_file)
    input_ids = np.asarray(encoded["input_ids"], dtype=np.int64)
    attention_mask = np.asarray(encoded["attention_mask"], dtype=np.int64)
    feed: dict[str, np.ndarray] = {"input_ids": input_ids, "attention_mask": attention_mask}
    if "token_type_ids" in in_names:
        feed["token_type_ids"] = np.asarray(
            encoded.get("token_type_ids", np.zeros_like(input_ids)),
            dtype=np.int64,
        )
    hidden = session.run(out_names or ["last_hidden_state"], feed)[0]
    onnx_vecs = mean_pool_and_normalize(hidden, attention_mask)

    cosines: list[float] = []
    max_norm_err = 0.0
    all_finite = bool(np.all(np.isfinite(pt_vecs)) and np.all(np.isfinite(onnx_vecs)))
    for idx in range(len(samples)):
        norm_val = float(np.linalg.norm(onnx_vecs[idx]))
        cos_val = float(np.dot(pt_vecs[idx], onnx_vecs[idx]))
        if not (math.isfinite(norm_val) and math.isfinite(cos_val)):
            all_finite = False
        else:
            max_norm_err = max(max_norm_err, abs(norm_val - 1.0))
        cosines.append(cos_val)

    min_cos = min(cosines) if (cosines and all_finite) else 0.0
    ok = (
        all_finite
        and onnx_vecs.shape == (len(samples), dim)
        and max_norm_err < 1e-5
        and min_cos >= min_cosine
    )
    return {
        "ok": ok,
        "backend": norm_backend,
        "dimension": int(onnx_vecs.shape[1]),
        "min_cosine": min_cos,
        "max_norm_err": max_norm_err,
        "cosines": cosines,
    }


@asynccontextmanager
async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
    _state.shutting_down = False
    _state.loading = True
    _batcher.start()
    loader_task = asyncio.create_task(asyncio.to_thread(_load_models))
    try:
        yield
    finally:
        _state.shutting_down = True
        await _batcher.aclose()
        if not loader_task.done():
            await asyncio.gather(loader_task, return_exceptions=True)


def create_app() -> Starlette:
    """Build the pure-ASGI Starlette application (zero BaseHTTPMiddleware)."""
    routes = [
        Route("/healthz", endpoint=healthz, methods=["GET"]),
        Route("/readyz", endpoint=readyz, methods=["GET"]),
        Route("/health", endpoint=readyz, methods=["GET"]),
        Route("/v1/embeddings", endpoint=embeddings_endpoint, methods=["POST"]),
        Route("/v1/nlp/spacy", endpoint=nlp_spacy_endpoint, methods=["POST"]),
        Route("/v1/nlp/nli", endpoint=nlp_nli_endpoint, methods=["POST"]),
        Route("/v1/chat/completions", endpoint=chat_completions_endpoint, methods=["POST"]),
        Route("/{path:path}", endpoint=not_found_endpoint),
    ]
    return Starlette(routes=routes, lifespan=_lifespan)


app = create_app()


def main(argv: list[str] | None = None) -> int:
    """Run the cognitive ASGI server or execute parity verification."""
    parser = argparse.ArgumentParser(description="NCE Cognitive Service ASGI runtime")
    parser.add_argument(
        "--verify-parity",
        action="store_true",
        help="Compare baked PyTorch SentenceTransformer vs ONNX Runtime cosine parity and exit.",
    )
    parser.add_argument(
        "--backend",
        default=None,
        help="Backend override for --verify-parity (onnx or onnx-int8).",
    )
    parser.add_argument(
        "--model-dir",
        default=None,
        help="Model directory override for --verify-parity (defaults to NCE_MODEL_DIR).",
    )
    parser.add_argument(
        "--min-cosine",
        type=float,
        default=None,
        help="Minimum cosine threshold override for --verify-parity.",
    )
    args = parser.parse_args(argv)

    if args.verify_parity:
        if args.backend:
            backend = args.backend.strip().lower()
        else:
            resolved = _resolve_backend()
            backend = "onnx" if resolved == "torch" else resolved
        if backend == "onnx_int8":
            backend = "onnx-int8"
        min_cos = (
            args.min_cosine
            if args.min_cosine is not None
            else (0.85 if backend == "onnx-int8" else 0.9999)
        )
        report = verify_onnx_pytorch_parity(
            model_dir=args.model_dir,
            backend=backend,
            min_cosine=min_cos,
        )
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    import uvicorn

    port = _port()
    log.info("Starting ASGI cognitive service on 0.0.0.0:%d", port)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        timeout_keep_alive=30,
        timeout_graceful_shutdown=15,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
