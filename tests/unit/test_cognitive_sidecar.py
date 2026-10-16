"""Unit tests for deploy/cognitive-sidecar/sidecar.py and deploy/cognitive-stub/stub_server.py."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import time
import types
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SIDECAR_PATH = REPO_ROOT / "deploy" / "cognitive-sidecar" / "sidecar.py"
STUB_PATH = REPO_ROOT / "deploy" / "cognitive-stub" / "stub_server.py"


def _load_sidecar_module() -> types.ModuleType:
    """Load sidecar.py with stubbed openvino/transformers modules if not installed."""
    fake_ov = types.ModuleType("openvino")
    fake_ov.Core = MagicMock()  # type: ignore[attr-defined]
    fake_tf = types.ModuleType("transformers")
    fake_tf.AutoTokenizer = MagicMock()  # type: ignore[attr-defined]

    saved_ov = sys.modules.get("openvino")
    saved_tf = sys.modules.get("transformers")
    sys.modules["openvino"] = fake_ov
    sys.modules["transformers"] = fake_tf
    try:
        spec = importlib.util.spec_from_file_location("cognitive_sidecar_mod", SIDECAR_PATH)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if saved_ov is not None:
            sys.modules["openvino"] = saved_ov
        else:
            sys.modules.pop("openvino", None)
        if saved_tf is not None:
            sys.modules["transformers"] = saved_tf
        else:
            sys.modules.pop("transformers", None)


def _load_stub_module() -> types.ModuleType:
    """Load stub_server.py."""
    spec = importlib.util.spec_from_file_location("cognitive_stub_mod", STUB_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _invoke_handler(
    handler_cls: type,
    method: str,
    path: str,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    server_attrs: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Invoke a BaseHTTPRequestHandler subclass in-memory without opening a socket."""
    handler = handler_cls.__new__(handler_cls)
    handler.path = path
    handler.command = method
    hdrs: dict[str, str] = {}
    if body and (headers is None or "Content-Length" not in headers):
        hdrs["Content-Length"] = str(len(body))
    if headers:
        hdrs.update(headers)
    handler.headers = hdrs
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.server = types.SimpleNamespace(**(server_attrs or {}))

    status_holder: list[int] = []
    handler.send_response = lambda code, message=None: status_holder.append(code)
    handler.send_header = lambda k, v: None
    handler.end_headers = lambda: None

    getattr(handler, f"do_{method}")()

    raw_resp = handler.wfile.getvalue()
    parsed = json.loads(raw_resp.decode("utf-8")) if raw_resp else {}
    return status_holder[0], parsed


def test_npu_embedder_serializes_concurrent_infer_and_copies_buffer() -> None:
    """NPUEmbedder.embed serializes request.infer under _lock and copies output buffer."""
    sidecar = _load_sidecar_module()

    port_input_ids = MagicMock()
    port_input_ids.get_names.return_value = {"input_ids"}
    port_input_ids.get_any_name.return_value = "input_ids"

    port_attention = MagicMock()
    port_attention.get_names.return_value = {"attention_mask"}
    port_attention.get_any_name.return_value = "attention_mask"

    output_port = MagicMock()
    output_port.shape = [1, 4, 4]

    shared_buffer = np.zeros((1, 4, 4), dtype=np.float32)
    in_flight = 0
    max_in_flight = 0
    counter_lock = threading.Lock()

    class _FakeInferRequest:
        def infer(self, feeds: dict[str, np.ndarray]) -> dict[Any, np.ndarray]:
            nonlocal in_flight, max_in_flight
            with counter_lock:
                in_flight += 1
                if in_flight > max_in_flight:
                    max_in_flight = in_flight
            try:
                token_val = float(feeds["input_ids"][0, 0])
                time.sleep(0.01)
                shared_buffer.fill(0.0)
                shared_buffer[0, :, int(token_val) % 4] = 1.0
                return {output_port: shared_buffer}
            finally:
                with counter_lock:
                    in_flight -= 1

    fake_compiled = MagicMock()
    fake_compiled.inputs = [port_input_ids, port_attention]
    fake_compiled.output.return_value = output_port
    fake_compiled.create_infer_request.return_value = _FakeInferRequest()

    fake_core_instance = MagicMock()
    fake_core_instance.compile_model.return_value = fake_compiled
    sidecar.Core = MagicMock(return_value=fake_core_instance)

    def _fake_tokenizer(
        text: str,
        padding: str,
        truncation: bool,
        max_length: int,
        return_tensors: str,
    ) -> dict[str, np.ndarray]:
        idx = int(text)
        return {
            "input_ids": np.full((1, 4), idx, dtype=np.int64),
            "attention_mask": np.ones((1, 4), dtype=np.int64),
        }

    sidecar.AutoTokenizer.from_pretrained = MagicMock(return_value=_fake_tokenizer)

    embedder = sidecar.NPUEmbedder("/tmp/model", "NPU", 4)
    assert isinstance(embedder._lock, type(threading.Lock()))

    results: dict[int, list[float]] = {}
    errors: list[BaseException] = []

    def _worker(idx: int) -> None:
        try:
            vec = embedder.embed(str(idx))
            # Mutate shared_buffer immediately outside infer to verify .copy() under lock
            with embedder._lock:
                shared_buffer.fill(999.0)
            results[idx] = vec
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors
    assert max_in_flight == 1
    for i in range(4):
        expected = [0.0, 0.0, 0.0, 0.0]
        expected[i] = 1.0
        assert results[i] == pytest.approx(expected, abs=1e-6)


def test_sidecar_uses_threading_http_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """sidecar.main binds ThreadingHTTPServer so requests do not block one another."""
    sidecar = _load_sidecar_module()
    assert sidecar.ThreadingHTTPServer is ThreadingHTTPServer

    created_servers: list[Any] = []

    class _FakeServer:
        def __init__(self, addr: tuple[str, int], handler: Any) -> None:
            self.addr = addr
            self.handler = handler
            self.embedder: Any = None
            self.stub_url: str = ""
            created_servers.append(self)

        def serve_forever(self) -> None:
            return None

    fake_embedder = MagicMock(dim=768)
    monkeypatch.setattr(
        sidecar,
        "_parse_args",
        lambda: types.SimpleNamespace(
            port=11436,
            model_dir="/tmp/model",
            device="NPU",
            seq_len=512,
            stub_url="http://localhost:11435",
        ),
    )
    monkeypatch.setattr(sidecar, "NPUEmbedder", lambda *a, **kw: fake_embedder)
    monkeypatch.setattr(sidecar, "ThreadingHTTPServer", _FakeServer)

    sidecar.main()
    assert len(created_servers) == 1
    assert created_servers[0].addr == ("0.0.0.0", 11436)
    assert created_servers[0].handler is sidecar._Handler


def test_sidecar_handler_content_length_and_json_validation() -> None:
    """sidecar._Handler validates Content-Length, query strings, JSON body, and texts."""
    sidecar = _load_sidecar_module()
    fake_embedder = MagicMock()
    fake_embedder.name = "test-model"
    fake_embedder.device = "NPU"
    fake_embedder.model_dir = "/tmp/model"
    fake_embedder.dim = 4
    fake_embedder.seq_len = 512
    fake_embedder.embed.side_effect = lambda t: [float(len(t)), 0.0, 0.0, 0.0]
    srv = {"embedder": fake_embedder, "stub_url": "http://localhost:11435"}

    # GET /health with query string
    code, data = _invoke_handler(sidecar._Handler, "GET", "/health?probe=true", server_attrs=srv)
    assert code == 200
    assert data["status"] == "ok"

    # Invalid Content-Length
    code, data = _invoke_handler(
        sidecar._Handler,
        "POST",
        "/v1/embeddings",
        headers={"Content-Length": "not-an-int"},
        server_attrs=srv,
    )
    assert code == 400
    assert "Content-Length" in data["error"]

    # Negative Content-Length
    code, data = _invoke_handler(
        sidecar._Handler,
        "POST",
        "/v1/embeddings",
        headers={"Content-Length": "-10"},
        server_attrs=srv,
    )
    assert code == 400
    assert "Content-Length" in data["error"]

    # Oversized Content-Length (> 10 MB)
    code, data = _invoke_handler(
        sidecar._Handler,
        "POST",
        "/v1/embeddings",
        headers={"Content-Length": str(10 * 1024 * 1024 + 1)},
        server_attrs=srv,
    )
    assert code == 413
    assert "too large" in data["error"].lower()

    # Invalid JSON payload
    code, data = _invoke_handler(
        sidecar._Handler,
        "POST",
        "/v1/embeddings",
        body=b"{invalid json",
        server_attrs=srv,
    )
    assert code == 400
    assert data["error"] == "Invalid JSON"

    # Non-dict JSON payload
    code, data = _invoke_handler(
        sidecar._Handler,
        "POST",
        "/v1/embeddings",
        body=b'["list", "not", "dict"]',
        server_attrs=srv,
    )
    assert code == 400
    assert "object" in data["error"]

    # Missing or empty input list
    code, data = _invoke_handler(
        sidecar._Handler,
        "POST",
        "/v1/embeddings",
        body=json.dumps({"input": []}).encode(),
        server_attrs=srv,
    )
    assert code == 400
    assert "non-empty list of strings" in data["error"]

    # Non-string element in input list
    code, data = _invoke_handler(
        sidecar._Handler,
        "POST",
        "/v1/embeddings",
        body=json.dumps({"input": ["valid", 123]}).encode(),
        server_attrs=srv,
    )
    assert code == 400
    assert "non-empty list of strings" in data["error"]

    # Valid POST /v1/embeddings with query string and string input
    code, data = _invoke_handler(
        sidecar._Handler,
        "POST",
        "/v1/embeddings?trace=1",
        body=json.dumps({"input": "hello"}).encode(),
        server_attrs=srv,
    )
    assert code == 200
    assert len(data["data"]) == 1
    assert data["data"][0]["embedding"] == [5.0, 0.0, 0.0, 0.0]


def test_stub_server_threading_and_input_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """stub_server uses ThreadingHTTPServer and validates Content-Length and JSON inputs."""
    stub = _load_stub_module()
    assert stub.ThreadingHTTPServer is ThreadingHTTPServer

    created: list[Any] = []

    class _FakeServer:
        def __init__(self, addr: tuple[str, int], handler: Any) -> None:
            self.addr = addr
            self.handler = handler
            created.append(self)

        def serve_forever(self) -> None:
            return None

    monkeypatch.setattr(stub, "ThreadingHTTPServer", _FakeServer)
    stub.main(port=11499)
    assert len(created) == 1
    assert created[0].addr == ("0.0.0.0", 11499)
    assert created[0].handler is stub.StubHandler

    # GET /health with query string
    code, data = _invoke_handler(stub.StubHandler, "GET", "/health?foo=bar")
    assert code == 200
    assert data["status"] == "ok"

    # Invalid Content-Length
    code, data = _invoke_handler(
        stub.StubHandler,
        "POST",
        "/v1/embeddings",
        headers={"Content-Length": "invalid"},
    )
    assert code == 400
    assert "Content-Length" in data["error"]

    # Negative Content-Length
    code, data = _invoke_handler(
        stub.StubHandler,
        "POST",
        "/v1/embeddings",
        headers={"Content-Length": "-1"},
    )
    assert code == 400
    assert "Content-Length" in data["error"]

    # Oversized Content-Length (> 10 MB)
    code, data = _invoke_handler(
        stub.StubHandler,
        "POST",
        "/v1/embeddings",
        headers={"Content-Length": str(10 * 1024 * 1024 + 1)},
    )
    assert code == 413
    assert "too large" in data["error"].lower()

    # Invalid JSON
    code, data = _invoke_handler(
        stub.StubHandler,
        "POST",
        "/v1/embeddings",
        body=b"{bad",
    )
    assert code == 400
    assert data["error"] == "Invalid JSON"

    # Non-dict JSON
    code, data = _invoke_handler(
        stub.StubHandler,
        "POST",
        "/v1/embeddings",
        body=b"[1, 2, 3]",
    )
    assert code == 400
    assert "object" in data["error"]

    # Non-string text on /v1/nlp/spacy
    code, data = _invoke_handler(
        stub.StubHandler,
        "POST",
        "/v1/nlp/spacy",
        body=json.dumps({"text": 12345}).encode(),
    )
    assert code == 400
    assert data["error"] == "text must be a string"

    # Valid POST /v1/embeddings with query string
    code, data = _invoke_handler(
        stub.StubHandler,
        "POST",
        "/v1/embeddings?version=1",
        body=json.dumps({"input": ["a", "b"]}).encode(),
    )
    assert code == 200
    assert len(data["data"]) == 2
