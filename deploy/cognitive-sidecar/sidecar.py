"""
Host-native OpenVINO NPU embedding sidecar with stub passthrough.

The Intel NPU on a Windows host is invisible to every WSL2 Linux container
(no /dev/accel, no /dev/dri), so the embedding model has to run host-native
and be reached over HTTP.  This process serves the same wire surface as
``deploy/cognitive-stub/stub_server.py`` (stdlib ``http.server`` only, so no
new dependency and an identical wire format), but answers
``POST /v1/embeddings`` from a pre-exported OpenVINO IR running on the NPU.

``NCE_COGNITIVE_BASE_URL`` is a single URL that six call sites fan out from,
so the three routes this sidecar does not implement (``/v1/nlp/nli``,
``/v1/nlp/spacy``, ``/v1/chat/completions``) are proxied verbatim to the
existing stub.  Serving only embeddings would 404 three subsystems.

Run it from a host venv holding ``openvino`` and ``transformers`` (numpy
ships with openvino; torch is deliberately not required):

    python sidecar.py --port 11436 --device NPU
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
from openvino import Core
from transformers import AutoTokenizer

DEFAULT_MODEL_DIR = os.path.join(
    os.path.expanduser("~"), ".nce", "models", "jina-embeddings-v2-base-code-ov"
)
PROXY_ROUTES = ("/v1/nlp/nli", "/v1/nlp/spacy", "/v1/chat/completions")
NOT_FOUND = b'{"error":"not found"}'
# Load-bearing, not cosmetic: nce/embeddings.py probes /health and refuses to
# start in production when it sees engine == "stub".
ENGINE = "openvino-npu"


class NPUEmbedder:
    """Mean-pooled, L2-normalised embeddings from a static OpenVINO IR."""

    def __init__(self, model_dir: str, device: str, seq_len: int) -> None:
        self.model_dir = model_dir
        self.device = device
        self.seq_len = seq_len
        self.name = os.path.basename(model_dir.rstrip("\\/")) or model_dir
        # trust_remote_code stays False on purpose: the IR export dropped a
        # configuration_bert.py beside the model and we must not execute it.
        self.tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=False)
        xml = os.path.join(model_dir, "openvino_model.xml")
        self.compiled = Core().compile_model(xml, device)
        self.input_names = {name for port in self.compiled.inputs for name in port.get_names()}
        self.output_port = self.compiled.output(0)
        self.request = self.compiled.create_infer_request()
        self.dim = int(list(self.output_port.shape)[-1])

    def embed(self, text: str) -> list[float]:
        """Embed a single string.

        The IR is exported with static shapes ``[1, seq_len]``, so batch size
        is 1: callers must loop over their inputs one at a time.
        """
        encoded = self.tokenizer(
            text,
            padding="max_length",
            truncation=True,
            max_length=self.seq_len,
            return_tensors="np",
        )
        feeds = {
            key: np.asarray(value, dtype=np.int64)
            for key, value in encoded.items()
            if key in self.input_names
        }
        # The IR declares token_type_ids but this tokenizer does not emit it, and an
        # input left unset is read from an UNINITIALISED buffer: stable within one
        # process, different in the next. Measured on this IR, same text and device,
        # across runs: cosine 0.999999 / 0.958 / 0.926 / 0.395. Supplying zeros makes
        # CPU, GPU and NPU agree to 0.999999 and byte-identical across processes.
        # Every declared input must be fed, so derive the gap from the model itself.
        zeros_shape = np.asarray(encoded["input_ids"], dtype=np.int64).shape
        for port in self.compiled.inputs:
            port_name = port.get_any_name()
            if port_name not in feeds:
                feeds[port_name] = np.zeros(zeros_shape, dtype=np.int64)
        last = np.asarray(self.request.infer(feeds)[self.output_port], dtype=np.float64)
        # Mirror nce/embeddings.py: mean-pool over the attention mask, then
        # L2-normalise per row (torch.nn.functional.normalize, p=2, eps=1e-12).
        mask = np.asarray(encoded["attention_mask"], dtype=np.float64)[..., None]
        summed = (last * mask).sum(axis=1)
        counts = np.clip(mask.sum(axis=1), 1e-9, None)
        pooled = summed / counts
        norms = np.clip(np.linalg.norm(pooled, ord=2, axis=1, keepdims=True), 1e-12, None)
        return [float(x) for x in (pooled / norms)[0]]


class _Handler(BaseHTTPRequestHandler):
    # An "Authorization: Bearer ..." header may be set by NCE
    # (NCE_COGNITIVE_API_KEY); it is accepted, never read and never logged.

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
            embedder = self.server.embedder
            body = json.dumps(
                {
                    "status": "ok",
                    "engine": ENGINE,
                    "device": embedder.device,
                    "model": embedder.name,
                    "model_dir": embedder.model_dir,
                    "dim": embedder.dim,
                    "seq_len": embedder.seq_len,
                }
            ).encode()
            self._send(200, body)
        else:
            self._send(404, NOT_FOUND)

    def _proxy(self, raw: bytes) -> None:
        url = self.server.stub_url.rstrip("/") + self.path
        req = urllib.request.Request(
            url,
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                self._send(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            self._send(exc.code, exc.read())
        except OSError as exc:
            self._send(502, json.dumps({"error": f"upstream: {exc}"}).encode())

    def _embeddings(self, raw: bytes) -> None:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}
        inputs = body.get("input", [""])
        if isinstance(inputs, str):
            inputs = [inputs]
        embedder = self.server.embedder
        # "model" in the request is ignored on purpose: NCE retries with
        # NCE_COGNITIVE_FALLBACK_MODEL on 429/timeout and must never be handed
        # a different vector space.
        data = [
            {"object": "embedding", "index": i, "embedding": embedder.embed(str(text))}
            for i, text in enumerate(inputs)
        ]
        payload = json.dumps(
            {
                "object": "list",
                "model": embedder.name,
                "data": data,
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            }
        ).encode()
        self._send(200, payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        if self.path == "/v1/embeddings":
            self._embeddings(raw)
        elif self.path in PROXY_ROUTES:
            self._proxy(raw)
        else:
            self._send(404, NOT_FOUND)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=11436)
    parser.add_argument("--model-dir", default=DEFAULT_MODEL_DIR)
    parser.add_argument("--device", default="NPU")
    parser.add_argument("--seq-len", type=int, default=512)
    parser.add_argument("--stub-url", default="http://localhost:11435")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    # Load before binding the port, so a failed NPU compile is loud at boot
    # rather than on the first request.
    embedder = NPUEmbedder(args.model_dir, args.device, args.seq_len)
    print(
        f"[cognitive-sidecar] engine={ENGINE} device={args.device} "
        f"model_dir={args.model_dir} dim={embedder.dim} "
        f"seq_len={args.seq_len} port={args.port} stub={args.stub_url}",
        flush=True,
    )
    server = HTTPServer(("127.0.0.1", args.port), _Handler)
    server.embedder = embedder
    server.stub_url = args.stub_url
    server.serve_forever()


if __name__ == "__main__":
    main()
