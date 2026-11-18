"""v3.5.4 regressions: pure-ASGI middleware stack, cron remote short-circuit, Entra prod gate."""

from __future__ import annotations

import builtins
import hashlib
import hmac
import importlib
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import nce
from nce import auth as nce_auth
from nce import config as nce_config
from nce import jwt_auth as nce_jwt_auth
from nce.observability import OpenTelemetryTraceMiddleware
from nce.reembedding_worker import ReembeddingWorker
from nce.webhook_receiver import main as webhook_main
from tests._verified_tier_middleware import VerifiedTierTestMiddleware

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _sign_hmac(
    secret: str,
    method: str,
    path: str,
    ts: int,
    nonce: str,
    body: bytes,
) -> str:
    body_hash = hashlib.sha256(body).hexdigest() if body else ""
    canonical = f"{method.upper()}\n{path}\n{ts}\n{nonce}"
    if body_hash:
        canonical = f"{canonical}\n{body_hash}"
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def test_zero_base_http_middleware_across_core() -> None:
    """Zero BaseHTTPMiddleware or @app.middleware('http') remain across NCE Core."""
    targets = list((_REPO_ROOT / "nce").rglob("*.py")) + [
        _REPO_ROOT / "admin_server.py",
        _REPO_ROOT / "tests" / "_verified_tier_middleware.py",
    ]
    offenders: list[str] = []
    for path in targets:
        text = path.read_text(encoding="utf-8")
        if re.search(r"\bBaseHTTPMiddleware\b", text) or '@app.middleware("http")' in text:
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert offenders == [], f"Unexpected BaseHTTPMiddleware / @app.middleware('http'): {offenders}"

    for cls in (
        nce_auth.HMACAuthMiddleware,
        nce_auth.BasicAuthMiddleware,
        nce_auth.AdminHTTPRateLimitMiddleware,
        nce_jwt_auth.JWTAuthMiddleware,
        VerifiedTierTestMiddleware,
        webhook_main.WebhookRateLimitMiddleware,
        OpenTelemetryTraceMiddleware,
    ):
        assert not issubclass(cls, BaseHTTPMiddleware)
        assert callable(getattr(cls, "__call__", None))


@pytest.mark.asyncio
async def test_pure_asgi_non_http_passthrough_and_response_header_preservation() -> None:
    """Non-HTTP scopes pass straight through and http.response.start headers stay intact."""
    seen_scopes: list[str] = []

    async def inner_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen_scopes.append(str(scope.get("type")))
        if scope.get("type") == "http":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"x-custom-inner", b"preserved")],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})

    middlewares = [
        nce_auth.HMACAuthMiddleware(inner_app, api_key="test-hmac-key"),  # noqa: S106
        nce_auth.BasicAuthMiddleware(inner_app, username="u", password="p"),  # noqa: S106
        nce_auth.AdminHTTPRateLimitMiddleware(inner_app),
        nce_jwt_auth.JWTAuthMiddleware(inner_app),
        VerifiedTierTestMiddleware(inner_app),
        webhook_main.WebhookRateLimitMiddleware(inner_app),
    ]

    for mw in middlewares:
        seen_scopes.clear()

        async def _recv() -> dict[str, Any]:
            return {"type": "lifespan.startup"}

        async def _send(msg: dict[str, Any]) -> None:
            return None

        await mw({"type": "lifespan"}, _recv, _send)
        assert seen_scopes == ["lifespan"]

    # Also verify /healthz fast probe header preservation and zero body consumption
    for mw in middlewares:
        sent: list[dict[str, Any]] = []
        body_polled = False

        async def _probe_recv() -> dict[str, Any]:
            nonlocal body_polled
            body_polled = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _probe_send(msg: dict[str, Any]) -> None:
            sent.append(msg)

        await mw(
            {"type": "http", "method": "GET", "path": "/healthz", "headers": []},
            _probe_recv,
            _probe_send,
        )
        assert not body_polled
        assert sent[0]["status"] == 200
        assert (b"x-custom-inner", b"preserved") in sent[0]["headers"]


def test_hmac_replay_safe_post_body_and_scope_state() -> None:
    """HMACAuthMiddleware replays buffered POST body cleanly and sets scope['state']."""
    secret = "test-hmac-secret-v354"  # noqa: S105
    captured: dict[str, Any] = {}

    async def echo_handler(request: Request) -> JSONResponse:
        raw = await request.body()
        parsed = await request.json()
        captured["raw"] = raw
        captured["parsed"] = parsed
        captured["hmac_auth"] = getattr(request.state, "hmac_auth", None)
        captured["namespace_ctx"] = getattr(request.state, "namespace_ctx", None)
        return JSONResponse({"received": parsed})

    app = Starlette(
        routes=[Route("/api/echo", endpoint=echo_handler, methods=["POST"])],
        middleware=[
            Middleware(
                nce_auth.HMACAuthMiddleware,
                protected_prefix="/api/",
                api_key=secret,
                nonce_store=None,
                nonce_required=False,
            )
        ],
    )

    payload = {"query": "hello pure asgi", "count": 42}
    body_bytes = json.dumps(payload).encode("utf-8")
    ts = int(time.time())
    nonce = "nonce_v354_replay_safe_001"
    sig = _sign_hmac(secret, "POST", "/api/echo", ts, nonce, body_bytes)

    with TestClient(app) as client:
        resp = client.post(
            "/api/echo",
            content=body_bytes,
            headers={
                "X-NCE-Timestamp": str(ts),
                "X-NCE-Nonce": nonce,
                "Authorization": f"HMAC-SHA256 {sig}",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 200
    assert resp.json() == {"received": payload}
    assert captured["raw"] == body_bytes
    assert captured["parsed"] == payload
    assert captured["hmac_auth"].nonce == nonce
    assert captured["namespace_ctx"] is not None


@pytest.mark.asyncio
async def test_hmac_replay_receive_delegates_after_first_body_event() -> None:
    """Second receive() call delegates to upstream ASGI receive() (e.g. http.disconnect)."""
    secret = "test-hmac-secret-v354"  # noqa: S105
    downstream_events: list[dict[str, Any]] = []

    async def inner_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        downstream_events.append(await receive())
        downstream_events.append(await receive())
        assert "hmac_auth" in scope.get("state", {})
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = nce_auth.HMACAuthMiddleware(
        inner_app, protected_prefix="/api/", api_key=secret, nonce_required=False
    )
    body_bytes = b'{"ok":true}'
    ts = int(time.time())
    nonce = "nonce_v354_delegate_001"
    sig = _sign_hmac(secret, "POST", "/api/item", ts, nonce, body_bytes)

    incoming = [
        {"type": "http.request", "body": b'{"ok":', "more_body": True},
        {"type": "http.request", "body": b"true}", "more_body": False},
        {"type": "http.disconnect"},
    ]

    async def recv() -> dict[str, Any]:
        return incoming.pop(0)

    sent: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/item",
        "headers": [
            (b"x-nce-timestamp", str(ts).encode("ascii")),
            (b"x-nce-nonce", nonce.encode("ascii")),
            (b"authorization", f"HMAC-SHA256 {sig}".encode("ascii")),
        ],
    }
    await mw(scope, recv, send)
    assert sent[0]["status"] == 204
    assert downstream_events[0] == {"type": "http.request", "body": body_bytes, "more_body": False}
    assert downstream_events[1] == {"type": "http.disconnect"}


@pytest.mark.asyncio
async def test_hmac_413_rejected_before_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Oversized bodies (both Content-Length and chunked stream) fail 413 before hashing."""
    secret = "test-hmac-secret-v354"  # noqa: S105
    monkeypatch.setenv("NCE_MAX_HTTP_BODY_BYTES", "32")

    verify_calls = 0

    def _spy_verify_hmac(*args: Any, **kwargs: Any) -> bool:
        nonlocal verify_calls
        verify_calls += 1
        return True

    monkeypatch.setattr(nce_auth, "verify_hmac", _spy_verify_hmac)

    async def inner_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        raise AssertionError("Downstream app must not run on HTTP 413")

    mw = nce_auth.HMACAuthMiddleware(
        inner_app, protected_prefix="/api/", api_key=secret, nonce_required=False
    )
    ts = int(time.time())
    nonce = "nonce_v354_oversize_001"
    sig = "0" * 64

    # 1. Content-Length pre-flight exceeds 32 bytes
    sent_cl: list[dict[str, Any]] = []
    cl_polled = False

    async def _cl_recv() -> dict[str, Any]:
        nonlocal cl_polled
        cl_polled = True
        return {"type": "http.request", "body": b"x" * 64, "more_body": False}

    async def _cl_send(msg: dict[str, Any]) -> None:
        sent_cl.append(msg)

    await mw(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/item",
            "headers": [
                (b"x-nce-timestamp", str(ts).encode("ascii")),
                (b"x-nce-nonce", nonce.encode("ascii")),
                (b"authorization", f"HMAC-SHA256 {sig}".encode("ascii")),
                (b"content-length", b"64"),
            ],
        },
        _cl_recv,
        _cl_send,
    )
    assert sent_cl[0]["status"] == 413
    assert json.loads(sent_cl[1]["body"])["error"] == "payload_too_large"
    assert not cl_polled
    assert verify_calls == 0

    # 2. Chunked stream without Content-Length aborts at chunk overflow before hashing
    sent_chunked: list[dict[str, Any]] = []
    chunks = [
        {"type": "http.request", "body": b"a" * 20, "more_body": True},
        {"type": "http.request", "body": b"b" * 20, "more_body": True},
        {"type": "http.request", "body": b"c" * 20, "more_body": False},
    ]

    async def _chunk_recv() -> dict[str, Any]:
        return chunks.pop(0)

    async def _chunk_send(msg: dict[str, Any]) -> None:
        sent_chunked.append(msg)

    await mw(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/item",
            "headers": [
                (b"x-nce-timestamp", str(ts).encode("ascii")),
                (b"x-nce-nonce", nonce.encode("ascii")),
                (b"authorization", f"HMAC-SHA256 {sig}".encode("ascii")),
            ],
        },
        _chunk_recv,
        _chunk_send,
    )
    assert sent_chunked[0]["status"] == 413
    assert len(chunks) == 1  # aborted immediately on the 2nd chunk (20+20 > 32)
    assert verify_calls == 0


@pytest.mark.asyncio
async def test_vram_pressure_gate_short_circuits_without_torch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When NCE_COGNITIVE_BASE_URL is set, _vram_pressure_gate never imports torch."""
    monkeypatch.setattr(nce_config.cfg, "NCE_COGNITIVE_BASE_URL", "http://cognitive:11435")

    real_import = builtins.__import__

    def _forbid_torch(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "torch" or name.startswith("torch."):
            raise AssertionError("torch must not be imported when NCE_COGNITIVE_BASE_URL is set")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _forbid_torch)

    worker = ReembeddingWorker()
    await worker._vram_pressure_gate()


def test_entra_fail_closed_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-empty NCE_ENTRA_TENANT_ID in prod/production fails unless mode == 'strict'."""
    for env_name in ("prod", "production"):
        monkeypatch.setenv("NCE_ENV", env_name)
        monkeypatch.setenv("NCE_ENTRA_TENANT_ID", "11111111-1111-1111-1111-111111111111")
        monkeypatch.delenv("NCE_ENTRA_ENFORCE_MODE", raising=False)
        with pytest.raises(RuntimeError, match="NCE_ENTRA_ENFORCE_MODE"):
            nce_config.assert_entra_strict_in_production()

        monkeypatch.setenv("NCE_ENTRA_ENFORCE_MODE", "permissive")
        with pytest.raises(RuntimeError, match="NCE_ENTRA_ENFORCE_MODE"):
            nce_config.assert_entra_strict_in_production()

        monkeypatch.setenv("NCE_ENTRA_ENFORCE_MODE", "strict")
        nce_config.assert_entra_strict_in_production()

    # Empty tenant ID in prod is allowed
    monkeypatch.setenv("NCE_ENV", "prod")
    monkeypatch.setenv("NCE_ENTRA_TENANT_ID", "")
    monkeypatch.delenv("NCE_ENTRA_ENFORCE_MODE", raising=False)
    nce_config.assert_entra_strict_in_production()


def test_dynamic_version_compose_and_retention_hygiene() -> None:
    """Verify v3.5.4 metadata, Compose hardening, and zero silent retention skips."""
    pyproject_text = (
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").replace("\r\n", "\n")
    )
    project_section = pyproject_text.split("[project]", 1)[1].split("\n[", 1)[0]
    ver_match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', project_section)
    assert ver_match is not None
    expected_version = ver_match.group(1)
    assert expected_version == "3.5.4"
    assert nce.__version__ == expected_version

    with TestClient(webhook_main.app) as client:
        hz = client.get("/healthz")
    assert hz.status_code == 200
    assert hz.json()["version"] == expected_version

    compose_text = (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert (
        'mongosh", "--quiet", "--norc", "--eval", "quit(db.runCommand({ ping: 1 }).ok ? 0 : 2)"'
        in compose_text
    )
    assert "image: nce-cognitive:3.5.4" in compose_text
    assert "NCE_COGNITIVE_BACKEND: ${NCE_COGNITIVE_BACKEND:-onnx}" in compose_text
    assert "NCE_COGNITIVE_BATCH_WINDOW_MS: ${NCE_COGNITIVE_BATCH_WINDOW_MS:-5}" in compose_text
    assert "NCE_COGNITIVE_MAX_BATCH_SIZE: ${NCE_COGNITIVE_MAX_BATCH_SIZE:-32}" in compose_text
    assert "NCE_COGNITIVE_NUM_THREADS: ${NCE_COGNITIVE_NUM_THREADS:-4}" in compose_text
    assert 'HF_HUB_OFFLINE: "1"' in compose_text
    assert 'TRANSFORMERS_OFFLINE: "1"' in compose_text
    assert "('onnxruntime', 'onnxruntime-int8', 'sentence-transformers')" in compose_text
    assert compose_text.count('seccomp:./deploy/seccomp/worker.json') >= 2

    retention_text = (_REPO_ROOT / "tests" / "test_event_retention.py").read_text(encoding="utf-8")
    assert "Cannot create test partition" not in retention_text
    assert "Cannot insert into partition" not in retention_text
    assert "chain_hash" in retention_text
    assert 'b"\\x00" * 32' in retention_text
    # Ensure Admin / A2A reload keeps package module healthy
    importlib.invalidate_caches()


def test_enable_tcp_nodelay_for_current_process_sets_option() -> None:
    """Verify TCP_NODELAY is applied to active AF_INET SOCK_STREAM sockets once per PID."""
    import socket

    from nce import http_body_limit

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 0)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        http_body_limit._NODELAY_APPLIED_PID = None
        applied = http_body_limit.enable_tcp_nodelay_for_current_process()
        assert applied >= 1
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0
        # Subsequent call in same PID is a no-op unless force=True
        assert http_body_limit.enable_tcp_nodelay_for_current_process() == 0
        assert http_body_limit.enable_tcp_nodelay_for_current_process(force=True) >= 1


def test_parse_file_python_stdlib_ast_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline Python indexing extracts functions/classes via stdlib ast when Tree-sitter fails."""
    from nce import ast_parser

    monkeypatch.setattr(ast_parser, "_try_treesitter_parse", lambda raw_code, language: None)
    sample = (
        "def calculate_embedding_distance(vec_a, vec_b):\n"
        "    return 0.0\n\n"
        "class VectorStore:\n"
        "    async def fetch(self):\n"
        "        return []\n"
    )
    chunks = list(ast_parser.parse_file(sample, "python"))
    assert [(c.node_type, c.name) for c in chunks] == [
        ("function", "calculate_embedding_distance"),
        ("class", "VectorStore"),
        ("function", "fetch"),
    ]
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 2
    assert "def calculate_embedding_distance" in chunks[0].code_string
