"""
Deterministic cognitive sidecar stub for local development and soak validation.

Exposes the same surface as ghcr.io/sindrehaugen/nce-cognitive:v1:
  GET  /health                — liveness probe
  POST /v1/chat/completions   — OpenAI-compatible structured JSON completions
  POST /v1/embeddings         — OpenAI-compatible unit-norm SHA-256 embeddings
  POST /v1/nlp/spacy          — regex entity & relation extraction
  POST /v1/nlp/nli            — heuristic contradiction scoring

Embedding dimension is controlled by EMBEDDING_VECTOR_DIM (default 768) so it
stays aligned with the Postgres schema without code changes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("COGNITIVE_PORT", "11435"))
DIM = int(os.getenv("EMBEDDING_VECTOR_DIM", "768"))
MAX_BODY_BYTES = 10 * 1024 * 1024

_HEALTH = json.dumps({"status": "ok", "engine": "stub"}).encode()

_IS_RELATION = re.compile(
    r"(\b\w[\w\s]{1,30}?\b)\s+(is|are|uses|has|contains|stores|connects to|depends on|runs on|works at)\s+([\w][\w\s]{1,30}?\b)",
    re.IGNORECASE,
)
_KNOWN_TOOLS = {
    "redis",
    "postgres",
    "postgresql",
    "mongodb",
    "mongo",
    "docker",
    "python",
    "fastapi",
    "mcp",
    "nce",
    "pgvector",
    "tree-sitter",
}
_STOPWORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "to",
    "of",
    "in",
    "on",
    "at",
    "for",
    "and",
    "or",
    "with",
    "by",
    "that",
    "this",
}
_NEGATIONS = {
    "not",
    "never",
    "no",
    "none",
    "neither",
    "cannot",
    "cant",
    "doesnt",
    "dont",
    "isnt",
    "arent",
    "wasnt",
    "werent",
    "without",
}
_ANTONYM_PAIRS = (
    ("enabled", "disabled"),
    ("true", "false"),
    ("on", "off"),
    ("active", "inactive"),
    ("allowed", "forbidden"),
    ("online", "offline"),
    ("open", "closed"),
    ("present", "absent"),
    ("valid", "invalid"),
    ("yes", "no"),
    ("up", "down"),
    ("pass", "fail"),
    ("encrypted", "plaintext"),
    ("public", "private"),
)
_TAG_MEMORY_CONTENT = re.compile(r"<memory_content>\s*(.*?)\s*</memory_content>", re.DOTALL)
_TAG_EXISTING_MEMORY = re.compile(r"<existing_memory>\s*(.*?)\s*</existing_memory>", re.DOTALL)
_TAG_NEW_MEMORY = re.compile(r"<new_memory>\s*(.*?)\s*</new_memory>", re.DOTALL)


def _stub_regex_extract(text: str) -> dict:
    nodes = []
    edges = []
    seen = set()

    def add_node(label: str, etype: str):
        cleaned = label.strip()
        key = cleaned.lower()
        if cleaned and key not in seen:
            nodes.append({"label": cleaned, "entity_type": etype, "source_text": cleaned})
            seen.add(key)

    for word in re.findall(r"\b\w[\w\-]+\b", text):
        lower = word.lower()
        if lower in _KNOWN_TOOLS:
            add_node(word, "TOOL")

    for m in _IS_RELATION.finditer(text):
        subj, pred, obj = (
            m.group(1).strip(),
            m.group(2).strip().lower(),
            m.group(3).strip(),
        )
        if subj.lower() == obj.lower():
            continue
        edges.append(
            {
                "subject_label": subj,
                "predicate": pred,
                "object_label": obj,
                "confidence": 0.85,
            }
        )
        for label in (subj, obj):
            add_node(label, "CONCEPT")

    return {"nodes": nodes, "edges": edges}


def _sha256_floats(seed_prefix: str, count: int) -> list[float]:
    """Expand a SHA-256 seed deterministically into ``count`` floats in [-1.0, 1.0]."""
    vals: list[float] = []
    block = 0
    while len(vals) < count:
        digest = hashlib.sha256(f"{seed_prefix}:{block}".encode()).digest()
        for idx in range(0, 32, 4):
            if len(vals) >= count:
                break
            uint_val = struct.unpack(">I", digest[idx : idx + 4])[0]
            vals.append((uint_val / 2147483647.5) - 1.0)
        block += 1
    return vals


def _hash_unit_vector(text: str, dim: int = DIM) -> list[float]:
    """Deterministic SHA-256 token + whole-string projection normalized to L2 norm == 1.0."""
    accum = [0.0] * dim
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    content_tokens = [t for t in tokens if t not in _STOPWORDS and t not in _NEGATIONS]

    for tok in set(content_tokens):
        comp = _sha256_floats(f"tok:{tok}", dim)
        for i in range(dim):
            accum[i] += comp[i]

    # Small whole-text component ensures distinct texts never collide and non-alphanumeric
    # inputs still yield non-zero deterministic vectors.
    raw_weight = 0.08 if content_tokens else 1.0
    raw_comp = _sha256_floats(f"raw:{text}", dim)
    for i in range(dim):
        accum[i] += raw_weight * raw_comp[i]

    norm = math.sqrt(sum(v * v for v in accum))
    if norm <= 1e-12:
        fallback = [1.0 / math.sqrt(dim)] * dim
        return fallback

    unit = [v / norm for v in accum]
    # Second pass eliminates any residual float rounding drift from 1.0
    norm2 = math.sqrt(sum(v * v for v in unit))
    if norm2 > 0.0 and abs(norm2 - 1.0) > 1e-12:
        unit = [v / norm2 for v in unit]
    return unit


def _heuristic_contradiction(premise: str, hypothesis: str) -> tuple[bool, float, str]:
    """Evaluate heuristic factual contradiction between two statements."""
    words_a = set(re.findall(r"[a-z0-9]+", premise.lower()))
    words_b = set(re.findall(r"[a-z0-9]+", hypothesis.lower()))
    content_a = words_a - _STOPWORDS - _NEGATIONS
    content_b = words_b - _STOPWORDS - _NEGATIONS
    shared_content = content_a & content_b

    if not shared_content:
        return False, 0.05, "No shared subject tokens."

    neg_a = bool(words_a & _NEGATIONS)
    neg_b = bool(words_b & _NEGATIONS)
    if neg_a != neg_b:
        return True, 0.92, "Negation polarity conflict on shared subject tokens."

    for left_w, right_w in _ANTONYM_PAIRS:
        if (left_w in words_a and right_w in words_b) or (right_w in words_a and left_w in words_b):
            return True, 0.92, f"Antonym conflict ({left_w!r} vs {right_w!r}) on shared subject."

    nums_a = set(re.findall(r"\b\d+(?:\.\d+)?\b", premise))
    nums_b = set(re.findall(r"\b\d+(?:\.\d+)?\b", hypothesis))
    if nums_a and nums_b and nums_a != nums_b:
        return True, 0.92, "Conflicting numeric values on shared subject tokens."

    # Relation conflict: same subject + predicate, distinct object (e.g. "Alice works at Acme" vs "Alice works at Globex")
    rels_a = _IS_RELATION.findall(premise)
    rels_b = _IS_RELATION.findall(hypothesis)
    for sa, pa, oa in rels_a:
        for sb, pb, ob in rels_b:
            if (
                sa.strip().lower() == sb.strip().lower()
                and pa.strip().lower() == pb.strip().lower()
                and oa.strip().lower() != ob.strip().lower()
            ):
                return (
                    True,
                    0.92,
                    f"Conflicting object values ({oa.strip()!r} vs {ob.strip()!r}) for {sa.strip()!r}.",
                )

    return False, 0.05, "Statements are compatible."


def _default_for_schema_prop(prop_spec: dict) -> object:
    if "enum" in prop_spec and isinstance(prop_spec["enum"], list) and prop_spec["enum"]:
        return prop_spec["enum"][0]
    ptype = prop_spec.get("type")
    if isinstance(ptype, list):
        ptype = next((t for t in ptype if t != "null"), ptype[0] if ptype else "string")
    if ptype == "string":
        return "stub"
    if ptype == "boolean":
        return False
    if ptype == "integer":
        return 0
    if ptype == "number":
        return 0.5
    if ptype == "array":
        return []
    if ptype == "object":
        return {}
    return None


def _build_consolidation_completion(combined_text: str) -> dict[str, object]:
    matches = _TAG_MEMORY_CONTENT.findall(combined_text)
    raw_cluster = (
        matches[-1].rsplit("<memory_content>", 1)[-1].strip() if matches else combined_text
    )
    items: list[dict] = []
    for candidate_json in (raw_cluster, raw_cluster.replace("{{", "{").replace("}}", "}")):
        try:
            parsed = json.loads(candidate_json)
            if isinstance(parsed, list):
                items = [it for it in parsed if isinstance(it, dict)]
                break
        except Exception:
            items = []

    supporting_ids = [str(it["memory_id"]) for it in items if it.get("memory_id")]
    contents = [
        str(it.get("content", "")).strip() for it in items if str(it.get("content", "")).strip()
    ]
    merged_text = " . ".join(contents)
    extracted = _stub_regex_extract(merged_text)
    entities = [str(n["label"]) for n in extracted.get("nodes", []) if n.get("label")]
    if not entities and contents:
        toks = [
            w
            for w in re.findall(r"\b[A-Za-z][A-Za-z0-9_-]{2,}\b", merged_text)
            if w.lower() not in _STOPWORDS
        ]
        seen_e: list[str] = []
        for tok in toks:
            if tok not in seen_e:
                seen_e.append(tok)
            if len(seen_e) >= 5:
                break
        entities = seen_e

    relations = [
        {
            "subject": str(ed["subject_label"]),
            "predicate": str(ed["predicate"]),
            "object": str(ed["object_label"]),
        }
        for ed in extracted.get("edges", [])
        if ed.get("subject_label") and ed.get("predicate") and ed.get("object_label")
    ]
    summary = (
        f"Consolidated synthesis ({len(supporting_ids)} memories): {' | '.join(contents)}"[:500]
        if contents
        else f"Consolidated synthesis across {len(supporting_ids)} episodic memories."
    )
    return {
        "abstraction": summary,
        "key_entities": entities,
        "key_relations": relations,
        "supporting_memory_ids": supporting_ids,
        "contradicting_memory_ids": [],
        "confidence": 0.88,
    }


def _build_contradiction_completion(combined_text: str) -> dict[str, object]:
    m_exist = _TAG_EXISTING_MEMORY.findall(combined_text)
    m_new = _TAG_NEW_MEMORY.findall(combined_text)
    existing_text = m_exist[-1].rsplit("<existing_memory>", 1)[-1].strip() if m_exist else ""
    new_text = m_new[-1].rsplit("<new_memory>", 1)[-1].strip() if m_new else ""
    is_conf, _score, explanation = _heuristic_contradiction(existing_text, new_text)
    return {
        "is_contradiction": bool(is_conf),
        "confidence": 0.92 if is_conf else 0.12,
        "explanation": explanation,
    }


def _chat_response(body: dict) -> bytes:
    model = body.get("model", "stub")
    messages = body.get("messages") or []
    combined_text = "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
    resp_fmt = body.get("response_format")
    json_schema_block = (
        resp_fmt.get("json_schema")
        if isinstance(resp_fmt, dict) and isinstance(resp_fmt.get("json_schema"), dict)
        else {}
    )
    schema_name = str(json_schema_block.get("name") or "")
    schema_obj = (
        json_schema_block.get("schema") if isinstance(json_schema_block.get("schema"), dict) else {}
    )
    schema_props = (
        schema_obj.get("properties") if isinstance(schema_obj.get("properties"), dict) else {}
    )

    if (
        schema_name == "ConsolidatedAbstraction"
        or "supporting_memory_ids" in schema_props
        or "<memory_content>" in combined_text
    ):
        payload_obj: dict[str, object] = _build_consolidation_completion(combined_text)
    elif (
        schema_name == "ContradictionResult"
        or "is_contradiction" in schema_props
        or "<existing_memory>" in combined_text
    ):
        payload_obj = _build_contradiction_completion(combined_text)
    elif schema_props:
        req_keys = schema_obj.get("required")
        keys_to_fill = (
            list(schema_props.keys())
            if not isinstance(req_keys, list)
            else list(dict.fromkeys([*req_keys, *schema_props.keys()]))
        )
        payload_obj = {
            k: _default_for_schema_prop(schema_props.get(k, {}))
            for k in keys_to_fill
            if k in schema_props
        }
    else:
        payload_obj = {}

    return json.dumps(
        {
            "id": "stub-0",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(payload_obj),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    ).encode()


def _validate_and_build_embeddings(body: dict) -> tuple[int, bytes]:
    if "input" not in body:
        return 400, json.dumps({"error": "Missing required 'input' field"}).encode()
    raw_inputs = body["input"]
    if isinstance(raw_inputs, str):
        if raw_inputs == "":
            return 400, json.dumps({"error": "'input' string must not be empty"}).encode()
        inputs = [raw_inputs]
    elif isinstance(raw_inputs, list):
        if len(raw_inputs) == 0 or not all(isinstance(item, str) for item in raw_inputs):
            return (
                400,
                json.dumps({"error": "'input' must be a non-empty list of strings"}).encode(),
            )
        inputs = raw_inputs
    else:
        return 400, json.dumps({"error": "'input' must be a string or list of strings"}).encode()

    data = [
        {"object": "embedding", "index": i, "embedding": _hash_unit_vector(item, DIM)}
        for i, item in enumerate(inputs)
    ]
    return (
        200,
        json.dumps(
            {
                "object": "list",
                "model": body.get("model", "stub"),
                "data": data,
                "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)},
            }
        ).encode(),
    )


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:  # silence request logs
        pass

    def _send(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict[str, object]) -> None:
        self._send(code, json.dumps(payload).encode())

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, _HEALTH)
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._json(400, {"error": "Invalid Content-Length"})
            return
        if length < 0:
            self._json(400, {"error": "Invalid Content-Length"})
            return
        if length > MAX_BODY_BYTES:
            self._json(413, {"error": "Payload too large"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "Invalid JSON"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "JSON body must be an object"})
            return

        path = self.path.split("?", 1)[0]
        if path == "/v1/chat/completions":
            self._send(200, _chat_response(body))
        elif path == "/v1/embeddings":
            status_code, resp_bytes = _validate_and_build_embeddings(body)
            self._send(status_code, resp_bytes)
        elif path == "/v1/nlp/spacy":
            text = body.get("text", "")
            if not isinstance(text, str):
                self._json(400, {"error": "text must be a string"})
                return
            self._send(200, json.dumps(_stub_regex_extract(text)).encode())
        elif path == "/v1/nlp/nli":
            premise = body.get("premise", "")
            hypothesis = body.get("hypothesis", "")
            if not isinstance(premise, str) or not isinstance(hypothesis, str):
                self._json(400, {"error": "premise and hypothesis must be strings"})
                return
            is_conf, score, _reason = _heuristic_contradiction(premise, hypothesis)
            self._json(200, {"score": 0.92 if is_conf else score})
        else:
            self._send(404, b'{"error":"not found"}')


_Handler = StubHandler


def main(port: int = PORT) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), StubHandler)
    print(f"[cognitive-stub] listening on :{port}  dim={DIM}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
