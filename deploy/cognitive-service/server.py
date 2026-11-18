"""Air-gapped real cognitive service for NCE Docker deployments.

Serves OpenAI-compatible 768-dimensional unit-norm embeddings via CPU
``sentence-transformers/all-mpnet-base-v2``, real spaCy ``en_core_web_sm``
NER + dependency SVO extraction, semantic NLI contradiction scoring, and
JSON-schema-guided chat completions.

Endpoints:
  GET  /healthz              — sub-millisecond liveness probe (zero model I/O)
  GET  /readyz               — readiness probe (200 once models loaded, else 503)
  GET  /health               — model metadata & engine identification
  POST /v1/embeddings        — OpenAI-compatible 768-d unit-norm embeddings
  POST /v1/nlp/spacy         — spaCy NER, noun chunks & dependency SVO triples
  POST /v1/nlp/nli           — semantic + polarity contradiction score in [0, 1]
  POST /v1/chat/completions  — structured JSON completion fallback
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import signal
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Default to air-gapped offline operation so runtime containers never reach HuggingFace Hub.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [cognitive-service] %(levelname)s %(message)s",
)
log = logging.getLogger("nce.cognitive_service")

PORT = int(os.environ.get("COGNITIVE_PORT", "11435"))
MODEL_ID = (
    os.environ.get("NCE_EMBEDDING_MODEL_ID", "sentence-transformers/all-mpnet-base-v2").strip()
    or "sentence-transformers/all-mpnet-base-v2"
)
SPACY_MODEL_ID = os.environ.get("NCE_SPACY_MODEL_ID", "en_core_web_sm").strip() or "en_core_web_sm"
MAX_BODY_BYTES = 10 * 1024 * 1024

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

_model_lock = threading.Lock()
_ready_event = threading.Event()
_st_model: Any = None
_nlp_model: Any = None
_embedding_dim: int = 768
_load_error: str | None = None


def _api_key() -> str:
    return (
        os.environ.get("NCE_COGNITIVE_API_KEY", "").strip()
        or os.environ.get("COGNITIVE_API_KEY", "").strip()
    )


def _load_models() -> None:
    """Load cached SentenceTransformer and spaCy models cleanly in a worker thread."""
    global _st_model, _nlp_model, _embedding_dim, _load_error
    try:
        import spacy
        from sentence_transformers import SentenceTransformer

        local_only = os.environ.get("HF_HUB_OFFLINE", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        log.info("Loading embedding model %s (local_files_only=%s)...", MODEL_ID, local_only)
        st = SentenceTransformer(MODEL_ID, device="cpu", local_files_only=local_only)
        dim = st.get_sentence_embedding_dimension()
        if isinstance(dim, int) and dim > 0:
            _embedding_dim = dim

        log.info("Loading spaCy model %s...", SPACY_MODEL_ID)
        nlp = spacy.load(SPACY_MODEL_ID)

        _st_model = st
        _nlp_model = nlp
        _load_error = None
        _ready_event.set()
        log.info(
            "Cognitive service ready (engine=sentence-transformers, model=%s, dim=%d, spacy=%s)",
            MODEL_ID,
            _embedding_dim,
            SPACY_MODEL_ID,
        )
    except Exception as exc:
        _load_error = f"{type(exc).__name__}: {exc}"
        log.exception("Cognitive model initialization failed: %s", _load_error)


def _ensure_ready(timeout: float = 30.0) -> tuple[Any, Any]:
    if not _ready_event.is_set():
        _ready_event.wait(timeout=timeout)
    if not _ready_event.is_set() or _st_model is None or _nlp_model is None:
        detail = _load_error or "models are still initializing"
        raise RuntimeError(f"Cognitive service not ready: {detail}")
    return _st_model, _nlp_model


def _encode_texts(texts: list[str]) -> list[list[float]]:
    st_model, _ = _ensure_ready()
    cleaned = [t if t else " " for t in texts]
    with _model_lock:
        vectors = st_model.encode(
            cleaned,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
    return [[float(x) for x in row.tolist()] for row in vectors]


def _spacy_extract(text: str) -> dict[str, Any]:
    _, nlp = _ensure_ready()
    doc = nlp((text or "")[:4096])

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


def _nli_contradiction_score(premise: str, hypothesis: str) -> float:
    """Estimate contradiction probability in [0.0, 1.0] using cosine similarity + polarity."""
    vectors = _encode_texts([premise, hypothesis])
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


def _schema_fallback_object(
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
            score = _nli_contradiction_score(existing_m.group(1), new_m.group(1))
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
            out[prop_name] = _schema_fallback_object(prop_def, messages)
        else:
            out[prop_name] = ""
    return out


class CognitiveHandler(BaseHTTPRequestHandler):
    """HTTP request handler implementing NCE's remote cognitive contract."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return  # Keep hot-path probe logs silent; emit warnings/errors explicitly.

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> tuple[dict[str, Any] | None, int, str | None]:
        raw_len = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_len)
        except ValueError:
            return None, HTTPStatus.BAD_REQUEST, "invalid Content-Length"
        if length < 0:
            return None, HTTPStatus.BAD_REQUEST, "negative Content-Length"
        if length > MAX_BODY_BYTES:
            return None, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload too large"
        raw = self.rfile.read(length) if length else b"{}"
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return None, HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}"
        if not isinstance(parsed, dict):
            return None, HTTPStatus.BAD_REQUEST, "request body must be a JSON object"
        return parsed, HTTPStatus.OK, None

    def _check_auth(self) -> bool:
        expected = _api_key()
        if not expected:
            return True
        auth = self.headers.get("Authorization", "")
        if not auth.lower().startswith("bearer "):
            return False
        supplied = auth[7:].strip()
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "service": "cognitive", "engine": "sentence-transformers"},
            )
            return

        if path in {"/readyz", "/health"}:
            if not _ready_event.is_set() or _st_model is None or _nlp_model is None:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "status": "starting" if _load_error is None else "error",
                        "service": "cognitive",
                        "engine": "sentence-transformers",
                        "model": MODEL_ID,
                        "error": _load_error,
                    },
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "cognitive",
                    "engine": "sentence-transformers",
                    "model": MODEL_ID,
                    "spacy_model": SPACY_MODEL_ID,
                    "embedding_dim": _embedding_dim,
                    "offline": True,
                },
            )
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"not found: {path}"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_auth():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        body, status, err = self._read_json()
        if body is None:
            self._send_json(status, {"error": err or "bad request"})
            return

        path = self.path.split("?", 1)[0]
        try:
            if path == "/v1/embeddings":
                raw_input = body.get("input", "")
                texts = (
                    [str(item) for item in raw_input]
                    if isinstance(raw_input, list)
                    else [str(raw_input)]
                )
                vectors = _encode_texts(texts)
                token_estimate = sum(max(1, len(t.split())) for t in texts)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "object": "list",
                        "model": str(body.get("model") or MODEL_ID),
                        "data": [
                            {"object": "embedding", "index": idx, "embedding": vec}
                            for idx, vec in enumerate(vectors)
                        ],
                        "usage": {
                            "prompt_tokens": token_estimate,
                            "total_tokens": token_estimate,
                        },
                    },
                )
                return

            if path == "/v1/nlp/spacy":
                result = _spacy_extract(str(body.get("text") or ""))
                self._send_json(HTTPStatus.OK, result)
                return

            if path == "/v1/nlp/nli":
                premise = str(body.get("premise") or "")
                hypothesis = str(body.get("hypothesis") or "")
                score = _nli_contradiction_score(premise, hypothesis)
                self._send_json(HTTPStatus.OK, {"score": score})
                return

            if path == "/v1/chat/completions":
                messages = body.get("messages")
                msg_list = messages if isinstance(messages, list) else []
                fmt = body.get("response_format") or {}
                schema = (
                    (fmt.get("json_schema") or {}).get("schema") if isinstance(fmt, dict) else None
                )
                if isinstance(schema, dict):
                    content = json.dumps(_schema_fallback_object(schema, msg_list))
                else:
                    content = json.dumps({"status": "ok", "model": MODEL_ID})
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "id": "chatcmpl-nce-local",
                        "object": "chat.completion",
                        "model": str(body.get("model") or MODEL_ID),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }
                        ],
                    },
                )
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"not found: {path}"})
        except RuntimeError as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
        except Exception as exc:
            log.exception("Unhandled error on %s", path)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"{type(exc).__name__}: {exc}"},
            )


def main() -> int:
    """Start the HTTP server immediately and initialize cached ML models in the background."""
    loader = threading.Thread(target=_load_models, name="cognitive-loader", daemon=True)
    loader.start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), CognitiveHandler)  # noqa: S104

    def _shutdown(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    log.info("Listening on 0.0.0.0:%d", PORT)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
