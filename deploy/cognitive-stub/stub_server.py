"""
Minimal cognitive sidecar stub for local development.

Exposes the same surface as ghcr.io/sindrehaugen/nce-cognitive:v1:
  GET  /health                — liveness probe
  POST /v1/chat/completions   — OpenAI-compatible chat (returns empty JSON object)
  POST /v1/embeddings         — OpenAI-compatible embeddings (768-dim zero vector)

Embedding dimension is controlled by EMBEDDING_VECTOR_DIM (default 768) so it
stays aligned with the Postgres schema without code changes.
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.getenv("COGNITIVE_PORT", "11435"))
DIM = int(os.getenv("EMBEDDING_VECTOR_DIM", "768"))

_HEALTH = json.dumps({"status": "ok", "engine": "stub"}).encode()

_IS_RELATION = re.compile(
    r"(\b\w[\w\s]{1,30}?\b)\s+(is|are|uses|has|contains|stores|connects to|depends on|runs on)\s+([\w][\w\s]{1,30}?\b)",
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


def _chat_response(body: dict) -> bytes:
    model = body.get("model", "stub")
    return json.dumps(
        {
            "id": "stub-0",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "{}"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    ).encode()


def _embeddings_response(body: dict) -> bytes:
    inputs = body.get("input", [""])
    if isinstance(inputs, str):
        inputs = [inputs]
    data = [
        {"object": "embedding", "index": i, "embedding": [0.0] * DIM} for i in range(len(inputs))
    ]
    return json.dumps(
        {
            "object": "list",
            "model": body.get("model", "stub"),
            "data": data,
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }
    ).encode()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:  # silence request logs
        pass

    def _send(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, _HEALTH)
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}

        if self.path == "/v1/chat/completions":
            self._send(200, _chat_response(body))
        elif self.path == "/v1/embeddings":
            self._send(200, _embeddings_response(body))
        elif self.path == "/v1/nlp/spacy":
            text = body.get("text", "")
            self._send(200, json.dumps(_stub_regex_extract(text)).encode())
        elif self.path == "/v1/nlp/nli":
            self._send(200, b'{"score":0.0}')
        else:
            self._send(404, b'{"error":"not found"}')


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), _Handler)
    print(f"[cognitive-stub] listening on :{PORT}  dim={DIM}", flush=True)
    server.serve_forever()
