"""
tests/test_auth.py

Unit tests for nce/auth.py — HMAC authentication, Phase 0.1 helpers.

Coverage:
  - verify_hmac (correct, wrong key, tampered body, empty key)
  - _compute_signature parity (body vs. no-body)
  - HMACAuthContext Pydantic V2 validation
  - NamespaceContext Pydantic V2 validation
  - resolve_namespace (valid UUID, missing header, malformed UUID)
  - validate_agent_id (normal, empty, whitespace, over-length)
  - HMACAuthMiddleware (via Starlette TestClient):
      - missing headers → 401 JSON-RPC 2.0
      - wrong scheme → 401 JSON-RPC 2.0
      - expired timestamp → 401 JSON-RPC 2.0
      - invalid signature → 401 JSON-RPC 2.0
      - empty api_key on server → 401 JSON-RPC 2.0
      - valid request → 200 pass-through
      - non-protected path → 200 no auth needed
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac
import time
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nce.auth import (
    MCP_ADMIN_TOOL_NAMES,
    HMACAuthContext,
    HMACAuthMiddleware,
    NamespaceContext,
    NonceStore,
    ScopeError,
    _compute_signature,
    _validate_scope,
    assume_namespace,
    audited_session,
    enforce_mcp_tool_auth,
    require_scope,
    resolve_namespace,
    set_namespace_context,
    validate_agent_id,
    verify_hmac,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KEY = "test-hmac-secret-key"


def _make_signature(
    key: str,
    method: str,
    path: str,
    timestamp: int,
    body: bytes = b"",
) -> str:
    parts = [method.upper(), path, str(timestamp)]
    if body:
        parts.append(hashlib.sha256(body).hexdigest())
    canonical = "\n".join(parts)
    return _hmac.new(key.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _valid_headers(
    key: str,
    method: str = "GET",
    path: str = "/api/health",
    body: bytes = b"",
    ts: int | None = None,
) -> dict[str, str]:
    ts = ts if ts is not None else int(time.time())
    sig = _make_signature(key, method, path, ts, body)
    return {
        "X-NCE-Timestamp": str(ts),
        "Authorization": f"HMAC-SHA256 {sig}",
    }


# ---------------------------------------------------------------------------
# _compute_signature / verify_hmac
# ---------------------------------------------------------------------------


class TestComputeSignature:
    def test_no_body_excludes_hash(self) -> None:
        sig = _compute_signature(_KEY, "GET", "/api/health", 1000, b"")
        expected = _hmac.new(_KEY.encode(), b"GET\n/api/health\n1000", hashlib.sha256).hexdigest()
        assert sig == expected

    def test_with_body_includes_sha256(self) -> None:
        body = b'{"x": 1}'
        body_hash = hashlib.sha256(body).hexdigest()
        sig = _compute_signature(_KEY, "POST", "/api/gc/trigger", 2000, body)
        canonical = f"POST\n/api/gc/trigger\n2000\n{body_hash}"
        expected = _hmac.new(_KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        assert sig == expected

    def test_method_uppercased(self) -> None:
        sig_lower = _compute_signature(_KEY, "get", "/api/health", 1000, b"")
        sig_upper = _compute_signature(_KEY, "GET", "/api/health", 1000, b"")
        assert sig_lower == sig_upper


class TestVerifyHmac:
    def test_valid_signature_returns_true(self) -> None:
        ts = int(time.time())
        body = b""
        sig = _make_signature(_KEY, "GET", "/api/health", ts, body)
        assert verify_hmac(_KEY, "GET", "/api/health", ts, body, sig) is True

    def test_wrong_key_returns_false(self) -> None:
        ts = int(time.time())
        sig = _make_signature("wrong-key", "GET", "/api/health", ts, b"")
        assert verify_hmac(_KEY, "GET", "/api/health", ts, b"", sig) is False

    def test_tampered_body_returns_false(self) -> None:
        ts = int(time.time())
        original_body = b'{"data": "original"}'
        sig = _make_signature(_KEY, "POST", "/api/x", ts, original_body)
        tampered = b'{"data": "tampered"}'
        assert verify_hmac(_KEY, "POST", "/api/x", ts, tampered, sig) is False

    def test_empty_api_key_returns_false(self) -> None:
        ts = int(time.time())
        sig = _make_signature(_KEY, "GET", "/api/health", ts, b"")
        assert verify_hmac("", "GET", "/api/health", ts, b"", sig) is False

    def test_case_insensitive_signature(self) -> None:
        ts = int(time.time())
        body = b""
        sig = _make_signature(_KEY, "GET", "/api/health", ts, body).upper()
        assert verify_hmac(_KEY, "GET", "/api/health", ts, body, sig) is True

    def test_different_path_returns_false(self) -> None:
        ts = int(time.time())
        sig = _make_signature(_KEY, "GET", "/api/health", ts, b"")
        assert verify_hmac(_KEY, "GET", "/api/other", ts, b"", sig) is False


# ---------------------------------------------------------------------------
# Pydantic V2 models
# ---------------------------------------------------------------------------


class TestHMACAuthContext:
    def test_valid_construction(self) -> None:
        ctx = HMACAuthContext(timestamp=int(time.time()), signature="deadbeef")
        assert ctx.signature == "deadbeef"

    def test_signature_lowercased(self) -> None:
        ctx = HMACAuthContext(timestamp=100, signature="DEADBEEF")
        assert ctx.signature == "deadbeef"

    def test_non_positive_timestamp_raises(self) -> None:
        with pytest.raises(Exception):
            HMACAuthContext(timestamp=0, signature="aa")

    def test_empty_signature_raises(self) -> None:
        with pytest.raises(Exception):
            HMACAuthContext(timestamp=100, signature="")

    def test_non_hex_signature_raises(self) -> None:
        with pytest.raises(Exception):
            HMACAuthContext(timestamp=100, signature="xyz!")

    def test_model_is_frozen(self) -> None:
        ctx = HMACAuthContext(timestamp=100, signature="aa")
        with pytest.raises(Exception):
            ctx.timestamp = 999  # type: ignore[misc]


class TestNamespaceContext:
    def test_valid_uuid_string(self) -> None:
        uid = uuid4()
        ctx = NamespaceContext(namespace_id=uid, agent_id="my-agent")
        assert ctx.namespace_id == uid
        assert ctx.agent_id == "my-agent"

    def test_blank_agent_defaults_to_default(self) -> None:
        ctx = NamespaceContext(namespace_id=uuid4(), agent_id="   ")
        assert ctx.agent_id == "default"

    def test_none_agent_defaults_to_default(self) -> None:
        ctx = NamespaceContext(namespace_id=uuid4(), agent_id=None)  # type: ignore[arg-type]
        assert ctx.agent_id == "default"

    def test_agent_id_truncated_at_128(self) -> None:
        long_id = "a" * 200
        ctx = NamespaceContext(namespace_id=uuid4(), agent_id=long_id)
        assert len(ctx.agent_id) == 128

    def test_agent_id_strips_whitespace(self) -> None:
        ctx = NamespaceContext(namespace_id=uuid4(), agent_id="  agent-1  ")
        assert ctx.agent_id == "agent-1"


# ---------------------------------------------------------------------------
# Phase 0.1 helpers
# ---------------------------------------------------------------------------


class TestResolveNamespace:
    def test_valid_uuid_returns_uuid_object(self) -> None:
        uid = uuid4()
        headers = {"x-nce-namespace-id": str(uid)}
        assert resolve_namespace(headers) == uid

    def test_missing_header_returns_none(self) -> None:
        assert resolve_namespace({}) is None

    def test_blank_header_returns_none(self) -> None:
        assert resolve_namespace({"x-nce-namespace-id": "  "}) is None

    def test_malformed_uuid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid namespace UUID"):
            resolve_namespace({"x-nce-namespace-id": "not-a-uuid"})

    def test_case_insensitive_header_lookup(self) -> None:
        uid = uuid4()
        # Simulate Starlette's lower-cased header dict
        headers = {"x-nce-namespace-id": str(uid)}
        assert resolve_namespace(headers) == uid


class TestValidateAgentId:
    def test_normal_id_returned_unchanged(self) -> None:
        assert validate_agent_id("my-agent") == "my-agent"

    def test_whitespace_stripped(self) -> None:
        assert validate_agent_id("  agent-2  ") == "agent-2"

    def test_empty_string_returns_default(self) -> None:
        assert validate_agent_id("") == "default"

    def test_none_returns_default(self) -> None:
        assert validate_agent_id(None) == "default"  # type: ignore[arg-type]

    def test_over_128_chars_truncated(self) -> None:
        assert len(validate_agent_id("x" * 200)) == 128

    def test_whitespace_only_returns_default(self) -> None:
        assert validate_agent_id("    ") == "default"


class TestSetNamespaceContext:
    @pytest.mark.asyncio
    async def test_calls_set_config_with_local_true(self) -> None:
        conn = AsyncMock()
        uid = uuid4()
        await set_namespace_context(conn, uid)
        conn.execute.assert_awaited_once_with(
            "SELECT set_config('nce.namespace_id', $1, true)",
            str(uid),
        )


# ---------------------------------------------------------------------------
# HMACAuthMiddleware (via Starlette TestClient)
# ---------------------------------------------------------------------------


def _build_test_app(api_key: str) -> Starlette:
    async def protected_route(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    async def public_route(request: Request) -> PlainTextResponse:
        return PlainTextResponse("public")

    return Starlette(
        routes=[
            Route("/api/health", endpoint=protected_route, methods=["GET"]),
            Route("/api/gc/trigger", endpoint=protected_route, methods=["POST"]),
            Route("/public", endpoint=public_route, methods=["GET"]),
        ],
        middleware=[Middleware(HMACAuthMiddleware, protected_prefix="/api/", api_key=api_key)],
    )


class TestHMACAuthMiddleware:
    def test_valid_get_passes(self) -> None:
        app = _build_test_app(_KEY)
        headers = _valid_headers(_KEY, "GET", "/api/health")
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/health", headers=headers)
        assert r.status_code == 200
        assert r.text == "OK"

    def test_valid_post_with_body_passes(self) -> None:
        app = _build_test_app(_KEY)
        body = b'{"force": true}'
        ts = int(time.time())
        sig = _make_signature(_KEY, "POST", "/api/gc/trigger", ts, body)
        headers = {
            "X-NCE-Timestamp": str(ts),
            "Authorization": f"HMAC-SHA256 {sig}",
            "Content-Type": "application/json",
        }
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.post("/api/gc/trigger", content=body, headers=headers)
        assert r.status_code == 200

    def test_missing_all_auth_headers_returns_401(self) -> None:
        app = _build_test_app(_KEY)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/health")
        assert r.status_code == 401
        body = r.json()
        assert body["jsonrpc"] == "2.0"
        assert body["error"]["code"] == -32001
        assert body["error"]["data"]["reason"] == "missing_auth_headers"

    def test_missing_timestamp_header_returns_401(self) -> None:
        app = _build_test_app(_KEY)
        ts = int(time.time())
        sig = _make_signature(_KEY, "GET", "/api/health", ts)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/health", headers={"Authorization": f"HMAC-SHA256 {sig}"})
        assert r.status_code == 401
        assert r.json()["error"]["data"]["reason"] == "missing_auth_headers"

    def test_missing_authorization_header_returns_401(self) -> None:
        app = _build_test_app(_KEY)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/health", headers={"X-NCE-Timestamp": str(int(time.time()))})
        assert r.status_code == 401
        assert r.json()["error"]["data"]["reason"] == "missing_auth_headers"

    def test_wrong_scheme_returns_401(self) -> None:
        app = _build_test_app(_KEY)
        ts = int(time.time())
        sig = _make_signature(_KEY, "GET", "/api/health", ts)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                "/api/health",
                headers={
                    "X-NCE-Timestamp": str(ts),
                    "Authorization": f"Bearer {sig}",  # wrong scheme
                },
            )
        assert r.status_code == 401
        assert r.json()["error"]["data"]["reason"] == "invalid_authorization_scheme"

    def test_expired_timestamp_returns_401_with_replay_code(self) -> None:
        app = _build_test_app(_KEY)
        old_ts = int(time.time()) - 600  # 10 minutes ago
        sig = _make_signature(_KEY, "GET", "/api/health", old_ts)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                "/api/health",
                headers={
                    "X-NCE-Timestamp": str(old_ts),
                    "Authorization": f"HMAC-SHA256 {sig}",
                },
            )
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["code"] == -32002
        assert body["error"]["data"]["reason"] == "replay_or_clock_skew"

    def test_future_timestamp_too_far_returns_401(self) -> None:
        app = _build_test_app(_KEY)
        future_ts = int(time.time()) + 600  # 10 minutes in future
        sig = _make_signature(_KEY, "GET", "/api/health", future_ts)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                "/api/health",
                headers={
                    "X-NCE-Timestamp": str(future_ts),
                    "Authorization": f"HMAC-SHA256 {sig}",
                },
            )
        assert r.status_code == 401
        assert r.json()["error"]["code"] == -32002

    def test_invalid_signature_returns_401(self) -> None:
        app = _build_test_app(_KEY)
        ts = int(time.time())
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                "/api/health",
                headers={
                    "X-NCE-Timestamp": str(ts),
                    "Authorization": "HMAC-SHA256 deadbeefdeadbeefdeadbeef",
                },
            )
        assert r.status_code == 401
        assert r.json()["error"]["data"]["reason"] == "invalid_signature"

    def test_non_hex_signature_returns_401(self) -> None:
        app = _build_test_app(_KEY)
        ts = int(time.time())
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                "/api/health",
                headers={
                    "X-NCE-Timestamp": str(ts),
                    "Authorization": "HMAC-SHA256 not-hex!",
                },
            )
        assert r.status_code == 401

    def test_empty_server_key_returns_401_server_misconfigured(self) -> None:
        app = _build_test_app("")  # empty key simulates misconfigured server
        headers = _valid_headers(_KEY, "GET", "/api/health")
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/health", headers=headers)
        assert r.status_code == 401
        assert r.json()["error"]["data"]["reason"] == "server_misconfigured"

    def test_public_route_requires_no_auth(self) -> None:
        app = _build_test_app(_KEY)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/public")
        assert r.status_code == 200
        assert r.text == "public"

    def test_jsonrpc_error_structure_is_complete(self) -> None:
        """Every error response must be a complete JSON-RPC 2.0 error object."""
        app = _build_test_app(_KEY)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/health")
        body = r.json()
        assert "jsonrpc" in body
        assert body["jsonrpc"] == "2.0"
        assert "error" in body
        error = body["error"]
        assert "code" in error
        assert "message" in error
        assert "data" in error
        assert "reason" in error["data"]
        assert "id" in body  # may be null but must be present


# ============================================================================
# NonceStore — Redis-backed distributed replay cache
# ============================================================================


class TestNonceStoreUnit:
    """Unit tests for NonceStore.check_and_store() with mocked asyncio Redis."""

    def test_fresh_nonce_accepted(self) -> None:
        """SETNX returns True (key did not exist) → nonce is new."""
        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(return_value=True)

        ns = NonceStore("redis://fake:6379/0", ttl=600)
        ns._redis = mock_redis
        assert asyncio.run(ns.check_and_store("abc123def456")) is True

        mock_redis.set.assert_awaited_once_with("nce:nonce:abc123def456", "1", nx=True, px=600_000)

    def test_replayed_nonce_rejected(self) -> None:
        """SETNX returns None (key already exists) → replay detected."""
        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(return_value=None)

        ns = NonceStore("redis://fake:6379/0", ttl=600)
        ns._redis = mock_redis
        assert asyncio.run(ns.check_and_store("duplicate-nonce")) is False

    def test_redis_connection_error_rejects_fail_closed(self) -> None:
        """Redis unreachable → reject the request (fail-closed)."""
        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(side_effect=ConnectionError("connection refused"))

        ns = NonceStore("redis://fake:6379/0", ttl=600)
        ns._redis = mock_redis
        assert asyncio.run(ns.check_and_store("any-nonce")) is False

    def test_redis_timeout_rejects_fail_closed(self) -> None:
        """Redis timeout → reject the request."""
        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(side_effect=TimeoutError("timed out"))

        ns = NonceStore("redis://fake:6379/0", ttl=600)
        ns._redis = mock_redis
        assert asyncio.run(ns.check_and_store("any-nonce")) is False

    def test_different_nonces_independent(self) -> None:
        """Two different nonces should both be accepted."""
        call_count = [0]

        async def set_side_effect(key, value, **kwargs):
            call_count[0] += 1
            return True

        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(side_effect=set_side_effect)

        ns = NonceStore("redis://fake:6379/0", ttl=600)
        ns._redis = mock_redis
        assert asyncio.run(ns.check_and_store("nonce-a")) is True
        assert asyncio.run(ns.check_and_store("nonce-b")) is True

        assert call_count[0] == 2

    def test_ttl_passed_as_milliseconds(self) -> None:
        """TTL in seconds must be converted to milliseconds for PX."""
        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(return_value=True)

        ns = NonceStore("redis://fake:6379/0", ttl=120)  # 2 minutes
        ns._redis = mock_redis
        asyncio.run(ns.check_and_store("test-nonce"))

        assert mock_redis.set.await_args.kwargs["px"] == 120_000  # 120 s → 120,000 ms

    def test_default_ttl_is_double_drift_window(self) -> None:
        """Default TTL should be 2× the timestamp drift."""
        from nce.auth import _NONCE_TTL_SECONDS, _TIMESTAMP_DRIFT_SECONDS

        assert _NONCE_TTL_SECONDS == _TIMESTAMP_DRIFT_SECONDS * 2
        assert (
            _NONCE_TTL_SECONDS == 180
        )  # 2 × 90 (NCE_CLOCK_SKEW_TOLERANCE_S default changed 300→90)


# ============================================================================
# HMACAuthMiddleware + NonceStore integration
# ============================================================================


def _build_test_app_with_nonce(api_key: str, nonce_store: NonceStore | None) -> Starlette:
    """Build a test Starlette app with HMAC auth middleware and optional NonceStore."""

    async def protected_route(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    return Starlette(
        routes=[
            Route("/api/health", endpoint=protected_route, methods=["GET"]),
        ],
        middleware=[
            Middleware(
                HMACAuthMiddleware,
                protected_prefix="/api/",
                api_key=api_key,
                nonce_store=nonce_store,
            )
        ],
    )


class TestHMACAuthMiddlewareWithNonceStore:
    """Integration tests: HMACAuthMiddleware with Redis NonceStore."""

    def test_fresh_nonce_passes_through(self) -> None:
        """Valid request with unseen nonce → 200 OK."""
        nonce_store = NonceStore("redis://fake:6379/0", ttl=600)
        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(return_value=True)
        nonce_store._redis = mock_redis

        app = _build_test_app_with_nonce(_KEY, nonce_store)
        headers = _valid_headers(_KEY, "GET", "/api/health")
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/health", headers=headers)
        assert r.status_code == 200
        assert r.text == "OK"

    def test_replayed_nonce_rejected_with_replay_code(self) -> None:
        """Second submission of same nonce → 401, replay code.

        New contract: nonce_required=True (default); Redis must be reachable
        (ping=True) so the nonce check runs.  SETNX returning None → replay.
        The nonce value must be present in X-NCE-Nonce so the mandatory-nonce
        path is exercised, not the nonce_missing path.
        """
        nonce_store = NonceStore("redis://fake:6379/0", ttl=600)
        mock_redis = MagicMock()
        mock_redis.ping = AsyncMock(return_value=True)  # Redis is reachable
        mock_redis.set = AsyncMock(return_value=None)  # SETNX: key already exists → replay
        nonce_store._redis = mock_redis

        app = _build_test_app_with_nonce(_KEY, nonce_store)
        # Supply X-NCE-Nonce so the store's check_and_store path is reached
        headers = {**_valid_headers(_KEY, "GET", "/api/health"), "X-NCE-Nonce": "test-nonce-abc123"}
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/health", headers=headers)
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["code"] == -32002
        assert body["error"]["data"]["reason"] == "replay_nonce_conflict"

    def test_concurrent_nonce_across_two_instances(self) -> None:
        """Simulate two instances seeing the same signed request.

        Instance A: SETNX returns True  → accepted
        Instance B: SETNX returns None → rejected (replay)

        New contract: nonce_required=True (default).  Both instances must
        report Redis as reachable (ping=True) so check_and_store is reached,
        and the request must carry X-NCE-Nonce so the mandatory-nonce path
        (not nonce_missing) is exercised.
        """
        seen_nonces: set[str] = set()

        async def shared_set(key, value, **kwargs):
            if key in seen_nonces:
                return None
            seen_nonces.add(key)
            return True

        redis_a = MagicMock()
        redis_a.ping = AsyncMock(return_value=True)  # Redis reachable on instance A
        redis_a.set = AsyncMock(side_effect=shared_set)
        redis_b = MagicMock()
        redis_b.ping = AsyncMock(return_value=True)  # Redis reachable on instance B
        redis_b.set = AsyncMock(side_effect=shared_set)

        ns_a = NonceStore("redis://fake:6379/0", ttl=600)
        ns_a._redis = redis_a
        ns_b = NonceStore("redis://fake:6379/0", ttl=600)
        ns_b._redis = redis_b

        # Build two apps (two "instances")
        app_a = _build_test_app_with_nonce(_KEY, ns_a)
        app_b = _build_test_app_with_nonce(_KEY, ns_b)

        ts = int(time.time())
        sig = _make_signature(_KEY, "GET", "/api/health", ts)
        # X-NCE-Nonce is required by the new contract when Redis is reachable
        headers = {
            "X-NCE-Timestamp": str(ts),
            "Authorization": f"HMAC-SHA256 {sig}",
            "X-NCE-Nonce": "concurrent-test-nonce-abc123",
        }

        # Instance A — should pass
        with TestClient(app_a, raise_server_exceptions=False) as client_a:
            r_a = client_a.get("/api/health", headers=headers)
        assert r_a.status_code == 200, f"Instance A should accept: {r_a.json()}"

        # Instance B — same request → should be rejected as replay
        with TestClient(app_b, raise_server_exceptions=False) as client_b:
            r_b = client_b.get("/api/health", headers=headers)
        assert r_b.status_code == 401
        body_b = r_b.json()
        assert body_b["error"]["code"] == -32002
        assert body_b["error"]["data"]["reason"] == "replay_nonce_conflict"

    def test_redis_failure_rejects_fail_closed(self, monkeypatch) -> None:
        """When Redis is down in prod, every request is rejected (fail-closed).

        New contract: fail-closed is prod-only.  In dev/test the middleware
        allows the request with a WARNING log.  The test must run under
        IS_PROD=True to assert the prod fail-closed guarantee.

        ping() returns False (Redis unreachable) → prod → reject with
        code -32002 / reason "nonce_store_unavailable".
        """
        from nce.config import cfg

        monkeypatch.setattr(cfg, "IS_PROD", True)

        nonce_store = NonceStore("redis://fake:6379/0", ttl=600)
        mock_redis = MagicMock()
        # NonceStore.ping() catches any exception and returns False → Redis unreachable
        mock_redis.ping = AsyncMock(side_effect=ConnectionError("redis down"))
        mock_redis.set = AsyncMock(side_effect=ConnectionError("redis down"))
        nonce_store._redis = mock_redis

        app = _build_test_app_with_nonce(_KEY, nonce_store)
        headers = _valid_headers(_KEY, "GET", "/api/health")
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/health", headers=headers)
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["code"] == -32002
        # New reason: fail-closed path reports nonce_store_unavailable (not replay_nonce_conflict)
        assert body["error"]["data"]["reason"] == "nonce_store_unavailable"

    def test_no_nonce_store_falls_back_to_timestamp_only(self) -> None:
        """Without NonceStore, two identical requests both pass (legacy mode)."""
        app = _build_test_app_with_nonce(_KEY, None)  # no NonceStore

        ts = int(time.time())
        sig = _make_signature(_KEY, "GET", "/api/health", ts)
        headers = {
            "X-NCE-Timestamp": str(ts),
            "Authorization": f"HMAC-SHA256 {sig}",
        }

        with TestClient(app, raise_server_exceptions=False) as client:
            r1 = client.get("/api/health", headers=headers)
            r2 = client.get("/api/health", headers=headers)  # same request again
        assert r1.status_code == 200
        assert r2.status_code == 200  # both pass without NonceStore

    def test_expired_timestamp_still_rejected_before_nonce_check(self) -> None:
        """Timestamp check runs first — expired timestamp bypasses nonce store entirely."""
        nonce_store = NonceStore("redis://fake:6379/0", ttl=600)
        mock_redis = MagicMock()
        mock_redis.set = AsyncMock()
        nonce_store._redis = mock_redis

        app = _build_test_app_with_nonce(_KEY, nonce_store)
        old_ts = int(time.time()) - 600  # 10 min ago
        sig = _make_signature(_KEY, "GET", "/api/health", old_ts)
        headers = {
            "X-NCE-Timestamp": str(old_ts),
            "Authorization": f"HMAC-SHA256 {sig}",
        }
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/health", headers=headers)
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["code"] == -32002
        assert body["error"]["data"]["reason"] == "replay_or_clock_skew"

        # Nonce store was never called — timestamp check short-circuited
        mock_redis.set.assert_not_awaited()

    def test_invalid_signature_still_rejected_before_nonce_check(self) -> None:
        """Signature check runs before nonce — invalid sig never reaches Redis."""
        nonce_store = NonceStore("redis://fake:6379/0", ttl=600)
        mock_redis = MagicMock()
        mock_redis.set = AsyncMock()
        nonce_store._redis = mock_redis

        app = _build_test_app_with_nonce(_KEY, nonce_store)
        ts = int(time.time())
        headers = {
            "X-NCE-Timestamp": str(ts),
            "Authorization": "HMAC-SHA256 deadbeefdeadbeefdeadbeef",
        }
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/health", headers=headers)
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["code"] == -32001  # auth failed, not replay

        mock_redis.set.assert_not_awaited()


# ============================================================================
# assume_namespace — mandatory WORM audit logging for tenant impersonation
# ============================================================================


class TestAssumeNamespace:
    """Verify assume_namespace writes an audit event on an independent
    connection BEFORE setting the session variable, and fails closed."""

    @pytest.fixture
    def caller_conn(self) -> AsyncMock:
        """The connection owned by the caller (where SET LOCAL will run)."""
        conn = AsyncMock()
        conn.is_in_transaction = MagicMock(return_value=True)
        return conn

    @pytest.fixture
    def audit_conn(self) -> AsyncMock:
        """The separate connection used for the audit write.

        Must support async context manager protocol because assume_namespace
        uses ``async with pg_pool.acquire() as audit_conn:`` and
        ``async with audit_conn.transaction():``.
        """
        # The transaction context manager (inner async with)
        tx = AsyncMock()
        tx.__aenter__.return_value = tx
        tx.__aexit__.return_value = False

        conn = AsyncMock()
        conn.__aenter__.return_value = conn
        conn.__aexit__.return_value = False
        conn.transaction = MagicMock(return_value=tx)
        return conn

    @pytest.fixture
    def mock_pool(self, audit_conn: AsyncMock) -> MagicMock:
        """Pool whose acquire() returns an awaitable that yields audit_conn."""
        acquire_result = AsyncMock()
        acquire_result.__aenter__.return_value = audit_conn
        acquire_result.__aexit__.return_value = False

        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_result)
        return pool

    @pytest.mark.asyncio
    async def test_audit_written_on_separate_connection(
        self, caller_conn: AsyncMock, mock_pool: MagicMock
    ) -> None:
        """The audit event uses pool.acquire() — a different connection from the caller's."""
        ns_id = uuid4()

        with patch("nce.event_log.append_event", new_callable=AsyncMock) as mock_append:
            await assume_namespace(
                conn=caller_conn,
                namespace_id=ns_id,
                impersonating_agent="admin-support",
                pg_pool=mock_pool,
                reason="ticket-12345",
            )

        # Audit connection was acquired from the pool (separate from caller_conn)
        mock_pool.acquire.assert_called_once()
        assert mock_append.await_count == 1

    @pytest.mark.asyncio
    async def test_audit_committed_before_session_variable_set(
        self, caller_conn: AsyncMock, mock_pool: MagicMock
    ) -> None:
        """append_event is called BEFORE set_namespace_context (audit-first ordering)."""
        ns_id = uuid4()
        call_order: list[str] = []

        async def _tracked_append(**kwargs):
            call_order.append("append_event")

        async def _tracked_set_ctx(conn, ns):
            call_order.append("set_namespace_context")

        with (
            patch(
                "nce.event_log.append_event",
                new_callable=AsyncMock,
                side_effect=_tracked_append,
            ),
            patch(
                "nce.auth.set_namespace_context",
                new_callable=AsyncMock,
                side_effect=_tracked_set_ctx,
            ),
        ):
            await assume_namespace(
                conn=caller_conn,
                namespace_id=ns_id,
                impersonating_agent="admin-support",
                pg_pool=mock_pool,
            )

        assert call_order == [
            "append_event",
            "set_namespace_context",
        ], f"Expected audit-first ordering, got: {call_order}"

    @pytest.mark.asyncio
    async def test_fail_closed_audit_write_failure_prevents_impersonation(
        self, caller_conn: AsyncMock, mock_pool: MagicMock
    ) -> None:
        """If the audit INSERT fails, RuntimeError is raised and SET LOCAL never runs."""
        ns_id = uuid4()

        with patch(
            "nce.event_log.append_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("PG connection lost"),
        ):
            with pytest.raises(RuntimeError, match="Audit write failed"):
                await assume_namespace(
                    conn=caller_conn,
                    namespace_id=ns_id,
                    impersonating_agent="admin-support",
                    pg_pool=mock_pool,
                )

        # Caller's execute() was never called — no silent impersonation
        caller_conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_variable_set_on_callers_connection(
        self, caller_conn: AsyncMock, mock_pool: MagicMock
    ) -> None:
        """set_namespace_context sets the session variable on the CALLER's connection."""
        ns_id = uuid4()

        with patch("nce.event_log.append_event", new_callable=AsyncMock):
            await assume_namespace(
                conn=caller_conn,
                namespace_id=ns_id,
                impersonating_agent="admin-support",
                pg_pool=mock_pool,
            )

        # SET LOCAL was executed on the caller's connection
        caller_conn.execute.assert_awaited_once_with(
            "SELECT set_config('nce.namespace_id', $1, true)",
            str(ns_id),
        )

    @pytest.mark.asyncio
    async def test_audit_event_contains_impersonation_metadata(
        self, caller_conn: AsyncMock, mock_pool: MagicMock
    ) -> None:
        """The audit event records who impersonated whom and why."""
        ns_id = uuid4()

        with patch("nce.event_log.append_event", new_callable=AsyncMock) as mock_append:
            await assume_namespace(
                conn=caller_conn,
                namespace_id=ns_id,
                impersonating_agent="admin-support",
                pg_pool=mock_pool,
                reason="Investigating ticket #12345",
            )

        kwargs = mock_append.call_args.kwargs
        assert kwargs["event_type"] == "namespace_impersonated"
        assert kwargs["agent_id"] == "admin-support"
        assert kwargs["namespace_id"] == ns_id
        assert kwargs["params"]["impersonated_namespace_id"] == str(ns_id)
        assert kwargs["params"]["impersonating_agent"] == "admin-support"
        assert kwargs["params"]["reason"] == "Investigating ticket #12345"
        assert kwargs["result_summary"] == {"status": "assumed"}

    @pytest.mark.asyncio
    async def test_reason_truncated_to_256_chars(
        self, caller_conn: AsyncMock, mock_pool: MagicMock
    ) -> None:
        """Reason field is truncated to 256 characters to bound audit event size."""
        ns_id = uuid4()
        long_reason = "x" * 500

        with patch("nce.event_log.append_event", new_callable=AsyncMock) as mock_append:
            await assume_namespace(
                conn=caller_conn,
                namespace_id=ns_id,
                impersonating_agent="admin-support",
                pg_pool=mock_pool,
                reason=long_reason,
            )

        recorded_reason = mock_append.call_args.kwargs["params"]["reason"]
        assert len(recorded_reason) == 256
        assert recorded_reason == long_reason[:256]

    @pytest.mark.asyncio
    async def test_audit_write_uses_independent_transaction(
        self, caller_conn: AsyncMock, mock_pool: MagicMock, audit_conn: AsyncMock
    ) -> None:
        """The audit connection opens and commits its OWN transaction."""
        ns_id = uuid4()

        with patch("nce.event_log.append_event", new_callable=AsyncMock):
            await assume_namespace(
                conn=caller_conn,
                namespace_id=ns_id,
                impersonating_agent="admin-support",
                pg_pool=mock_pool,
            )

        # The audit connection's transaction() was called (returns a tx mock)
        audit_conn.transaction.assert_called_once()
        # The transaction mock (inner async with) was entered + exited
        tx = audit_conn.transaction.return_value
        tx.__aenter__.assert_called()
        tx.__aexit__.assert_called()
        # Caller's connection was NOT used for any transaction
        caller_conn.transaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_set_namespace_context_still_works(
        self,
    ) -> None:
        """set_namespace_context (non-privileged variant) is unchanged."""
        conn = AsyncMock()
        uid = uuid4()
        await set_namespace_context(conn, uid)
        conn.execute.assert_awaited_once_with(
            "SELECT set_config('nce.namespace_id', $1, true)",
            str(uid),
        )


# ============================================================================
# audited_session — generalised WORM-audited scoped session (Phase 3)
# ============================================================================


class TestAuditedSession:
    """Verify audited_session writes a cryptographically signed audit event
    on an independent connection BEFORE yielding a scoped session, and that
    the audit survives exceptions inside the with-block."""

    @pytest.fixture
    def audit_conn(self) -> AsyncMock:
        """The separate connection used for the audit write."""
        tx = AsyncMock()
        tx.__aenter__.return_value = tx
        tx.__aexit__.return_value = False

        conn = AsyncMock()
        conn.__aenter__.return_value = conn
        conn.__aexit__.return_value = False
        conn.transaction = MagicMock(return_value=tx)
        return conn

    @pytest.fixture
    def session_conn(self) -> AsyncMock:
        """The scoped connection yielded to the caller."""
        tx = AsyncMock()
        tx.__aenter__.return_value = tx
        tx.__aexit__.return_value = False

        conn = AsyncMock()
        conn.__aenter__.return_value = conn
        conn.__aexit__.return_value = False
        conn.transaction = MagicMock(return_value=tx)
        return conn

    @pytest.fixture
    def mock_pool(self, audit_conn: AsyncMock, session_conn: AsyncMock) -> MagicMock:
        """Pool that yields audit_conn first, then session_conn on second acquire."""
        acquire_count = [0]

        def acquire_side_effect(*_args, **_kwargs):
            acquire_count[0] += 1
            if acquire_count[0] == 1:
                # First acquire: audit connection
                result = MagicMock()
                result.__aenter__ = AsyncMock(return_value=audit_conn)
                result.__aexit__ = AsyncMock(return_value=False)
                return result
            else:
                # Second acquire: scoped session connection
                result = MagicMock()
                result.__aenter__ = AsyncMock(return_value=session_conn)
                result.__aexit__ = AsyncMock(return_value=False)
                return result

        pool = MagicMock()
        pool.acquire = MagicMock(side_effect=acquire_side_effect)
        return pool

    # ------------------------------------------------------------------
    # Audit pre-flight
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_audit_written_on_separate_connection_before_yield(
        self, mock_pool: MagicMock, audit_conn: AsyncMock
    ) -> None:
        """audited_session acquires a separate connection for the audit write
        before yielding the scoped session connection."""
        ns_id = uuid4()

        # Mock append_event at the event_log level so _write_audit_event
        # still executes pool.acquire() for the audit connection.
        with patch("nce.event_log.append_event", new_callable=AsyncMock):
            async with audited_session(
                mock_pool,
                ns_id,
                agent_id="admin-support",
                event_type="admin_memory_recall",
                reason="ticket-12345",
            ) as conn:
                assert conn is not None

        # Pool was acquired twice: once for audit (separate connection),
        # once for the scoped session connection.
        assert mock_pool.acquire.call_count == 2

    @pytest.mark.asyncio
    async def test_audit_commit_before_yield(
        self, mock_pool: MagicMock, audit_conn: AsyncMock
    ) -> None:
        """The audit transaction is committed before the scoped connection
        is yielded."""
        ns_id = uuid4()
        call_order: list[str] = []

        async def _tracked_write(*args, **kwargs):
            call_order.append("audit_write")

        async def _tracked_set_ctx(conn, ns):
            call_order.append("set_namespace_context")

        with (
            patch(
                "nce.auth._write_audit_event",
                new_callable=AsyncMock,
                side_effect=_tracked_write,
            ),
            patch(
                "nce.auth.set_namespace_context",
                new_callable=AsyncMock,
                side_effect=_tracked_set_ctx,
            ),
        ):
            async with audited_session(
                mock_pool,
                ns_id,
                agent_id="admin-support",
                event_type="admin_memory_recall",
            ):
                call_order.append("yielded")

        assert call_order == [
            "audit_write",
            "set_namespace_context",
            "yielded",
        ], f"Expected audit-first ordering, got: {call_order}"

    @pytest.mark.asyncio
    async def test_fail_closed_audit_write_failure_prevents_yield(
        self, mock_pool: MagicMock
    ) -> None:
        """If the audit write fails, RuntimeError is raised and the with-block
        body never executes."""
        ns_id = uuid4()

        with patch(
            "nce.auth._write_audit_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("PG connection lost"),
        ):
            with pytest.raises(RuntimeError, match="PG connection lost"):
                async with audited_session(
                    mock_pool,
                    ns_id,
                    agent_id="admin-support",
                    event_type="admin_memory_recall",
                ):
                    pytest.fail("with-block body must not execute on audit failure")

    # ------------------------------------------------------------------
    # Audit survives with-block exceptions
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_audit_survives_exception_in_with_block(self, mock_pool: MagicMock) -> None:
        """When the with-block raises, the audit event is already committed
        on the separate connection — it survives the rollback."""
        ns_id = uuid4()
        audit_committed = [False]

        async def _tracked_write(*args, **kwargs):
            audit_committed[0] = True

        with patch(
            "nce.auth._write_audit_event",
            new_callable=AsyncMock,
            side_effect=_tracked_write,
        ):
            with pytest.raises(ValueError, match="simulated failure"):
                async with audited_session(
                    mock_pool,
                    ns_id,
                    agent_id="admin-support",
                    event_type="admin_memory_recall",
                ):
                    raise ValueError("simulated failure")

        # Audit was committed before the exception propagated
        assert audit_committed[0] is True

    @pytest.mark.asyncio
    async def test_audit_survives_transaction_rollback_in_with_block(
        self, mock_pool: MagicMock, session_conn: AsyncMock
    ) -> None:
        """Even if the caller opens a transaction inside the with-block and
        it rolls back, the pre-flight audit is safe on a separate connection."""
        ns_id = uuid4()
        audit_committed = [False]

        async def _tracked_write(*args, **kwargs):
            audit_committed[0] = True

        with patch(
            "nce.auth._write_audit_event",
            new_callable=AsyncMock,
            side_effect=_tracked_write,
        ):
            async with audited_session(
                mock_pool,
                ns_id,
                agent_id="admin-support",
                event_type="admin_memory_recall",
                reason="investigation",
            ):
                # Simulate caller doing work
                pass

        assert audit_committed[0] is True

    # ------------------------------------------------------------------
    # Session scoping
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_session_variable_set_on_yielded_connection(
        self, mock_pool: MagicMock, session_conn: AsyncMock
    ) -> None:
        """set_namespace_context is called on the yielded connection to
        apply RLS scoping."""
        ns_id = uuid4()

        # Use a simpler pool that always returns session_conn since
        # _write_audit_event is mocked (no audit acquire happens).
        simple_pool = MagicMock()
        acquire_result = AsyncMock()
        acquire_result.__aenter__.return_value = session_conn
        acquire_result.__aexit__.return_value = False
        simple_pool.acquire = MagicMock(return_value=acquire_result)

        with (
            patch("nce.auth._write_audit_event", new_callable=AsyncMock),
            patch("nce.auth.set_namespace_context", new_callable=AsyncMock) as mock_set,
        ):
            async with audited_session(
                simple_pool,
                ns_id,
                agent_id="admin-support",
                event_type="admin_memory_recall",
            ):
                pass

        mock_set.assert_awaited_once_with(session_conn, ns_id)

    # ------------------------------------------------------------------
    # Audit event metadata
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_audit_event_contains_operation_metadata(self, mock_pool: MagicMock) -> None:
        """The audit event records the agent, namespace, event_type, params,
        and reason."""
        ns_id = uuid4()

        with patch("nce.auth._write_audit_event", new_callable=AsyncMock) as mock_write:
            async with audited_session(
                mock_pool,
                ns_id,
                agent_id="admin-support",
                event_type="admin_memory_recall",
                params={"query": "security incidents"},
                reason="Investigating ticket #12345",
            ):
                pass

        kwargs = mock_write.call_args.kwargs
        assert kwargs["namespace_id"] == ns_id
        assert kwargs["agent_id"] == "admin-support"
        assert kwargs["event_type"] == "admin_memory_recall"
        assert kwargs["params"]["query"] == "security incidents"
        assert kwargs["params"]["reason"] == "Investigating ticket #12345"
        assert kwargs["result_summary"] == {"status": "audited_session_begin"}

    @pytest.mark.asyncio
    async def test_reason_truncated_to_256_chars(self, mock_pool: MagicMock) -> None:
        """Reason field is truncated to 256 characters."""
        ns_id = uuid4()
        long_reason = "y" * 500

        with patch("nce.auth._write_audit_event", new_callable=AsyncMock) as mock_write:
            async with audited_session(
                mock_pool,
                ns_id,
                agent_id="admin-support",
                event_type="admin_memory_recall",
                reason=long_reason,
            ):
                pass

        recorded_reason = mock_write.call_args.kwargs["params"]["reason"]
        assert len(recorded_reason) == 256
        assert recorded_reason == long_reason[:256]

    @pytest.mark.asyncio
    async def test_params_merged_with_reason(self, mock_pool: MagicMock) -> None:
        """When both params and reason are provided, reason is merged into
        params under the 'reason' key."""
        ns_id = uuid4()

        with patch("nce.auth._write_audit_event", new_callable=AsyncMock) as mock_write:
            async with audited_session(
                mock_pool,
                ns_id,
                agent_id="admin-support",
                event_type="admin_graph_traversal",
                params={"max_depth": 3},
                reason="security audit",
            ):
                pass

        kwargs = mock_write.call_args.kwargs
        assert kwargs["params"]["max_depth"] == 3
        assert kwargs["params"]["reason"] == "security audit"

    @pytest.mark.asyncio
    async def test_default_params_when_none_provided(self, mock_pool: MagicMock) -> None:
        """When no params are provided, an empty dict is passed to the audit
        write (reason is merged in if provided)."""
        ns_id = uuid4()

        with patch("nce.auth._write_audit_event", new_callable=AsyncMock) as mock_write:
            async with audited_session(
                mock_pool,
                ns_id,
                agent_id="admin-support",
                event_type="admin_memory_recall",
            ):
                pass

        kwargs = mock_write.call_args.kwargs
        assert isinstance(kwargs["params"], dict)


# ---------------------------------------------------------------------------
# ScopeError + require_scope decorator — RBAC enforcement
# ---------------------------------------------------------------------------


class TestScopeError:
    """ScopeError exception behaviour."""

    def test_scope_error_attributes(self) -> None:
        err = ScopeError("admin", "missing key")
        assert err.required_scope == "admin"
        assert err.reason == "missing key"
        assert "admin" in str(err)
        assert "missing key" in str(err)

    def test_scope_error_is_exception(self) -> None:
        err = ScopeError("tenant", "no JWT")
        assert isinstance(err, Exception)

    def test_scope_error_default_reason(self) -> None:
        err = ScopeError("admin")
        assert err.reason == ""


class TestValidateScope:
    """_validate_scope() function — admin scope enforcement."""

    def test_admin_override_bypasses_check(self, monkeypatch) -> None:
        monkeypatch.setenv("NCE_ADMIN_OVERRIDE", "true")
        # Should not raise even without API key
        _validate_scope("admin", {})

    def test_missing_server_key_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("NCE_ADMIN_OVERRIDE", raising=False)
        monkeypatch.delenv("NCE_ADMIN_API_KEY", raising=False)
        with pytest.raises(ScopeError, match="misconfigured"):
            _validate_scope("admin", {})

    def test_missing_server_key_fails_closed_without_cfg_fallback(self, monkeypatch) -> None:
        """Regression: delenv must not fall back to import-time cfg snapshot."""
        monkeypatch.delenv("NCE_ADMIN_OVERRIDE", raising=False)
        monkeypatch.delenv("NCE_ADMIN_API_KEY", raising=False)
        from nce.config import cfg

        monkeypatch.setattr(cfg, "NCE_ADMIN_API_KEY", "stale-import-time-key")
        with pytest.raises(ScopeError, match="misconfigured"):
            _validate_scope("admin", {"admin_api_key": "stale-import-time-key"})

    def test_missing_client_key_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("NCE_ADMIN_OVERRIDE", raising=False)
        monkeypatch.setenv("NCE_ADMIN_API_KEY", "secret-key")
        with pytest.raises(ScopeError, match="missing admin_api_key"):
            _validate_scope("admin", {})

    def test_empty_client_key_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("NCE_ADMIN_OVERRIDE", raising=False)
        monkeypatch.setenv("NCE_ADMIN_API_KEY", "secret-key")
        with pytest.raises(ScopeError, match="missing admin_api_key"):
            _validate_scope("admin", {"admin_api_key": "  "})

    def test_wrong_client_key_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("NCE_ADMIN_OVERRIDE", raising=False)
        monkeypatch.setenv("NCE_ADMIN_API_KEY", "secret-key")
        with pytest.raises(ScopeError, match="invalid admin_api_key"):
            _validate_scope("admin", {"admin_api_key": "wrong-key"})

    def test_correct_key_passes(self, monkeypatch) -> None:
        monkeypatch.delenv("NCE_ADMIN_OVERRIDE", raising=False)
        monkeypatch.setenv("NCE_ADMIN_API_KEY", "secret-key")
        # Should not raise
        _validate_scope("admin", {"admin_api_key": "secret-key"})

    def test_tenant_scope_requires_key_when_server_key_set(self, monkeypatch) -> None:
        monkeypatch.setenv("NCE_MCP_API_KEY", "tenant-secret")
        with pytest.raises(ScopeError, match="missing mcp_api_key"):
            _validate_scope("tenant", {})
        with pytest.raises(ScopeError, match="invalid mcp_api_key"):
            _validate_scope("tenant", {"mcp_api_key": "wrong"})
        _validate_scope("tenant", {"mcp_api_key": "tenant-secret"})

    def test_tenant_scope_passes_without_server_key_in_dev(self, monkeypatch) -> None:
        monkeypatch.delenv("NCE_MCP_API_KEY", raising=False)
        _validate_scope("tenant", {})

    def test_unknown_scope_raises(self) -> None:
        with pytest.raises(ScopeError, match="unknown scope"):
            _validate_scope("superadmin", {})


class TestEnforceMcpToolAuth:
    """stdio MCP dispatch gate — admin vs tenant tools."""

    def test_tenant_tool_requires_mcp_key(self, monkeypatch) -> None:
        # 1. When NCE_MCP_API_KEY is set, calling with no args falls back to env key and passes.
        monkeypatch.setenv("NCE_MCP_API_KEY", "mcp-secret")
        args: dict = {}
        enforce_mcp_tool_auth("semantic_search", args)
        assert "mcp_api_key" not in args  # It was popped!

        # 2. When NCE_MCP_API_KEY is set, calling with wrong mcp_api_key fails with "invalid mcp_api_key".
        with pytest.raises(ScopeError, match="invalid mcp_api_key"):
            enforce_mcp_tool_auth("semantic_search", {"mcp_api_key": "wrong-secret"})

        # 3. When NCE_MCP_API_KEY is set, calling with empty/blank mcp_api_key fails with "missing mcp_api_key".
        with pytest.raises(ScopeError, match="missing mcp_api_key"):
            enforce_mcp_tool_auth("semantic_search", {"mcp_api_key": "   "})

    def test_admin_tool_requires_admin_key(self, monkeypatch) -> None:
        # 1. When NCE_ADMIN_API_KEY is set, calling with no args falls back to env key and passes.
        monkeypatch.setenv("NCE_ADMIN_API_KEY", "admin-secret")
        args: dict = {}
        enforce_mcp_tool_auth("manage_namespace", args)
        assert args.get("admin_api_key") == "admin-secret"

        # 2. When NCE_ADMIN_API_KEY is set, calling with wrong admin_api_key fails with "invalid admin_api_key".
        with pytest.raises(ScopeError, match="invalid admin_api_key"):
            enforce_mcp_tool_auth("manage_namespace", {"admin_api_key": "wrong-secret"})

        # 3. When NCE_ADMIN_API_KEY is set, calling with empty/blank admin_api_key fails with "missing admin_api_key".
        with pytest.raises(ScopeError, match="missing admin_api_key"):
            enforce_mcp_tool_auth("manage_namespace", {"admin_api_key": "   "})

    def test_admin_tool_names_are_registered(self) -> None:
        assert "manage_namespace" in MCP_ADMIN_TOOL_NAMES
        assert "semantic_search" not in MCP_ADMIN_TOOL_NAMES

    def test_tenant_namespace_injected_when_bound(self, monkeypatch) -> None:
        ns = "11111111-2222-4333-8444-555555555555"
        monkeypatch.setenv("NCE_MCP_API_KEY", "mcp-secret")
        monkeypatch.setenv("NCE_MCP_NAMESPACE_ID", ns)
        args: dict = {"mcp_api_key": "mcp-secret"}
        enforce_mcp_tool_auth("semantic_search", args)
        assert args["namespace_id"] == ns

    def test_tenant_namespace_mismatch_rejected(self, monkeypatch) -> None:
        monkeypatch.setenv("NCE_MCP_API_KEY", "mcp-secret")
        monkeypatch.setenv("NCE_MCP_NAMESPACE_ID", "11111111-2222-4333-8444-555555555555")
        with pytest.raises(ScopeError, match="namespace_id does not match"):
            enforce_mcp_tool_auth(
                "semantic_search",
                {
                    "mcp_api_key": "mcp-secret",
                    "namespace_id": "99999999-9999-4999-8999-999999999999",
                },
            )

    def test_admin_tool_skips_namespace_binding(self, monkeypatch) -> None:
        monkeypatch.setenv("NCE_ADMIN_API_KEY", "admin-secret")
        monkeypatch.setenv("NCE_MCP_NAMESPACE_ID", "11111111-2222-4333-8444-555555555555")
        args = {"admin_api_key": "admin-secret"}
        enforce_mcp_tool_auth("manage_namespace", args)
        assert "namespace_id" not in args


class TestRequireScopeDecorator:
    """@require_scope decorator — applied to async handler functions."""

    @pytest.mark.asyncio
    async def test_admin_scope_passes_with_valid_key(self, monkeypatch) -> None:
        monkeypatch.setenv("NCE_ADMIN_API_KEY", "key123")

        @require_scope("admin")
        async def handler(engine, arguments, **kwargs):
            return "ok"

        result = await handler("fake_engine", {"admin_api_key": "key123"})
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_admin_scope_fails_without_key(self, monkeypatch) -> None:
        monkeypatch.setenv("NCE_ADMIN_API_KEY", "key123")

        @require_scope("admin")
        async def handler(engine, arguments):
            return "ok"

        with pytest.raises(ScopeError, match="missing admin_api_key"):
            await handler("fake_engine", {})

    @pytest.mark.asyncio
    async def test_strips_auth_keys_from_arguments(self, monkeypatch) -> None:
        monkeypatch.setenv("NCE_ADMIN_API_KEY", "key123")

        @require_scope("admin")
        async def handler(engine, arguments):
            return arguments  # Return cleaned args

        result = await handler(
            "fake_engine",
            {"admin_api_key": "key123", "is_admin": True, "namespace_id": "ns1"},
        )
        assert "admin_api_key" not in result
        assert "is_admin" not in result
        assert result["namespace_id"] == "ns1"

    @pytest.mark.asyncio
    async def test_forwards_admin_identity_as_kwarg(self, monkeypatch) -> None:
        monkeypatch.setenv("NCE_ADMIN_API_KEY", "key123")

        @require_scope("admin")
        async def handler(engine, arguments, admin_identity=None):
            return admin_identity

        result = await handler(
            "fake_engine",
            {"admin_api_key": "key123", "admin_identity": "ops-bot"},
        )
        assert result == "ops-bot"

    @pytest.mark.asyncio
    async def test_admin_identity_not_in_cleaned_args(self, monkeypatch) -> None:
        monkeypatch.setenv("NCE_ADMIN_API_KEY", "key123")

        @require_scope("admin")
        async def handler(engine, arguments, admin_identity=None):
            return arguments

        result = await handler(
            "fake_engine",
            {"admin_api_key": "key123", "admin_identity": "ops-bot", "ns": "x"},
        )
        assert "admin_identity" not in result
        assert "admin_api_key" not in result
        assert result["ns"] == "x"

    @pytest.mark.asyncio
    async def test_handler_without_admin_identity_param_works(self, monkeypatch) -> None:
        monkeypatch.setenv("NCE_ADMIN_API_KEY", "key123")

        @require_scope("admin")
        async def handler(engine, arguments):
            return "no-identity-needed"

        result = await handler(
            "fake_engine",
            {"admin_api_key": "key123", "admin_identity": "ops-bot"},
        )
        assert result == "no-identity-needed"

    @pytest.mark.asyncio
    async def test_positional_more_than_two_args(self, monkeypatch) -> None:
        monkeypatch.setenv("NCE_ADMIN_API_KEY", "key123")

        @require_scope("admin")
        async def handler(engine, arguments, extra):
            return extra

        result = await handler("e", {"admin_api_key": "key123"}, "extra-val")
        assert result == "extra-val"

    @pytest.mark.asyncio
    async def test_preserves_handler_name(self) -> None:
        @require_scope("admin")
        async def my_handler(engine, arguments):
            pass

        assert my_handler.__name__ == "my_handler"


# ---------------------------------------------------------------------------
# PBKDF2 password hashing (OWASP 2026)
# ---------------------------------------------------------------------------


class TestHashAdminPassword:
    """Tests for :func:`nce.auth.hash_admin_password`."""

    def test_default_iterations_600k(self) -> None:
        from nce.auth import _PBKDF2_ITERATIONS

        assert _PBKDF2_ITERATIONS >= 600_000

    def test_produces_pbkdf2_prefixed_string(self) -> None:
        from nce.auth import hash_admin_password

        h = hash_admin_password("my-secret")
        assert h.startswith("$pbkdf2$")
        parts = h.split("$")
        assert len(parts) == 5  # empty, prefix, iterations, salt_hex, hash_hex
        assert int(parts[2]) >= 600_000
        assert len(parts[3]) == 32  # 16 bytes hex = 32 chars
        assert len(parts[4]) == 64  # 32 bytes hex = 64 chars

    def test_different_passwords_produce_different_hashes(self) -> None:
        from nce.auth import hash_admin_password

        h1 = hash_admin_password("alpha")
        h2 = hash_admin_password("beta")
        assert h1 != h2

    def test_same_password_different_salts(self) -> None:
        from nce.auth import hash_admin_password

        h1 = hash_admin_password("same")
        h2 = hash_admin_password("same")
        # Different salts → different hashes
        assert h1 != h2

    def test_custom_iterations(self) -> None:
        from nce.auth import hash_admin_password

        h = hash_admin_password("test", iterations=200_000)
        parts = h.split("$")
        assert int(parts[2]) == 200_000

    def test_rejects_too_few_iterations(self) -> None:
        from nce.auth import hash_admin_password

        with pytest.raises(ValueError, match="at least 100,000"):
            hash_admin_password("test", iterations=50_000)

    def test_output_length(self) -> None:
        from nce.auth import hash_admin_password

        h = hash_admin_password("length-test")
        # $pbkdf2$<iters>$<salt_hex>$<hash_hex>
        assert len(h) > 50


class TestVerifyAdminPassword:
    """Tests for :func:`nce.auth.verify_admin_password`."""

    def test_correct_password_verifies(self) -> None:
        from nce.auth import hash_admin_password, verify_admin_password

        h = hash_admin_password("correct")
        valid, upgraded = verify_admin_password("correct", h)
        assert valid is True
        assert upgraded is None  # already at 600K

    def test_wrong_password_rejected(self) -> None:
        from nce.auth import hash_admin_password, verify_admin_password

        h = hash_admin_password("real")
        valid, upgraded = verify_admin_password("wrong", h)
        assert valid is False
        assert upgraded is None

    def test_auto_upgrade_from_lower_iterations(self) -> None:
        from nce.auth import hash_admin_password, verify_admin_password

        # Hash with old 210K iterations
        old_hash = hash_admin_password("mypass", iterations=210_000)
        valid, upgraded = verify_admin_password("mypass", old_hash, auto_upgrade=True)
        assert valid is True
        assert upgraded is not None
        assert upgraded.startswith("$pbkdf2$")
        # Verify the upgraded hash works too
        valid2, _ = verify_admin_password("mypass", upgraded)
        assert valid2 is True

    def test_no_auto_upgrade_when_disabled(self) -> None:
        from nce.auth import hash_admin_password, verify_admin_password

        old_hash = hash_admin_password("mypass", iterations=210_000)
        valid, upgraded = verify_admin_password("mypass", old_hash, auto_upgrade=False)
        assert valid is True
        assert upgraded is None  # upgrade suppressed

    def test_plaintext_backward_compat(self) -> None:
        from nce.auth import verify_admin_password

        # Plaintext password comparison (DEPRECATED but must still work)
        valid, upgraded = verify_admin_password("plaintext", "plaintext")
        assert valid is True
        # Plaintext comparison auto-upgrades to PBKDF2 by default (auto_upgrade=True)
        assert upgraded is not None
        assert upgraded.startswith("$pbkdf2$")

    def test_plaintext_wrong_password(self) -> None:
        from nce.auth import verify_admin_password

        valid, _ = verify_admin_password("wrong", "plaintext")
        assert valid is False

    def test_plaintext_rejected_in_production(self, monkeypatch) -> None:
        from nce.auth import verify_admin_password
        from nce.config import cfg

        monkeypatch.setattr(cfg, "IS_PROD", True)
        valid, upgraded = verify_admin_password("plaintext", "plaintext")
        assert valid is False
        assert upgraded is None

    def test_plaintext_auto_upgrades(self) -> None:
        from nce.auth import verify_admin_password

        valid, upgraded = verify_admin_password("secret123", "secret123", auto_upgrade=True)
        assert valid is True
        assert upgraded is not None
        assert upgraded.startswith("$pbkdf2$")

    def test_empty_stored_hash(self) -> None:
        from nce.auth import verify_admin_password

        valid, upgraded = verify_admin_password("anything", "")
        assert valid is False
        assert upgraded is None

    def test_invalid_hash_format(self) -> None:
        from nce.auth import verify_admin_password

        valid, upgraded = verify_admin_password("p", "$pbkdf2$bad")
        assert valid is False
        assert upgraded is None

    def test_invalid_iterations_in_hash(self) -> None:
        from nce.auth import verify_admin_password

        valid, _ = verify_admin_password("p", "$pbkdf2$abc$aa$bb")
        assert valid is False

    def test_too_few_iterations_in_hash(self) -> None:
        from nce.auth import verify_admin_password

        valid, _ = verify_admin_password(
            "p",
            "$pbkdf2$5000$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa$bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        assert valid is False

    def test_invalid_salt_hex(self) -> None:
        from nce.auth import verify_admin_password

        valid, _ = verify_admin_password(
            "p",
            "$pbkdf2$600000$nothex$bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        assert valid is False

    def test_invalid_hash_hex(self) -> None:
        from nce.auth import verify_admin_password

        valid, _ = verify_admin_password(
            "p", "$pbkdf2$600000$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa$nothex"
        )
        assert valid is False


class TestAdversarialAuth:
    """Adversarial auth tests trying to pass admin_api_key in tenant tool requests."""

    def test_tenant_tools_reject_arbitrary_admin_api_key(self, monkeypatch) -> None:
        from nce.auth import ScopeError, enforce_mcp_tool_auth

        # Set up keys on server
        monkeypatch.setenv("NCE_ADMIN_API_KEY", "admin-secret-key")
        monkeypatch.setenv("NCE_MCP_API_KEY", "tenant-secret-key")
        monkeypatch.delenv("NCE_MCP_NAMESPACE_ID", raising=False)

        # 1. Missing mcp_api_key but admin_api_key is provided
        with pytest.raises(ScopeError, match="missing mcp_api_key") as exc_info:
            enforce_mcp_tool_auth("semantic_search", {"admin_api_key": "admin-secret-key"})
        assert exc_info.value.required_scope == "tenant"

        # 2. Invalid mcp_api_key and admin_api_key is provided
        with pytest.raises(ScopeError, match="invalid mcp_api_key") as exc_info:
            enforce_mcp_tool_auth(
                "semantic_search",
                {"admin_api_key": "admin-secret-key", "mcp_api_key": "bad-tenant-key"},
            )
        assert exc_info.value.required_scope == "tenant"

        # 3. Valid mcp_api_key along with admin_api_key succeeds tenant check, but admin_api_key has no effect
        enforce_mcp_tool_auth(
            "semantic_search",
            {
                "admin_api_key": "admin-secret-key",
                "mcp_api_key": "tenant-secret-key",
                "namespace_id": "00000000-0000-0000-0000-000000000000",
            },
        )
