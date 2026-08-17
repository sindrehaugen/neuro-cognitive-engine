"""Unit tests for nce/mtls.py — MTLSAuthMiddleware."""

from __future__ import annotations

import json
import logging
import os

os.environ.setdefault("NCE_MASTER_KEY", "dev-test-key-32chars-long!!")

from unittest.mock import AsyncMock, patch

import pytest

from nce.a2a import A2AMTLSError
from nce.mtls import DEFAULT_MTLS_ERROR_CODE, MTLSAuthMiddleware

_LOGGER = "nce.mtls"
_ANCHOR_SANS = ["example.com"]
_ANCHOR_FP = ["aa:bb"]
_MAX_HEADER_BYTES = 16_384


def _make_scope(
    path: str = "/api/v1",
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] | None = None,
) -> dict:
    scope: dict = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers or [],
    }
    if client is not None:
        scope["client"] = client
    return scope


async def _collect_response(middleware: MTLSAuthMiddleware, scope: dict) -> dict:
    """Invoke middleware and capture status, response headers, and body."""
    received: dict = {}

    async def receive():
        return {}

    async def send(message):
        if message["type"] == "http.response.start":
            received["status"] = message["status"]
            raw_headers = message.get("headers", [])
            received["headers"] = {
                k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw_headers
            }
        elif message["type"] == "http.response.body":
            received["body"] = message.get("body", b"")

    await middleware(scope, receive, send)
    return received


# ---------------------------------------------------------------------------
# Initialization validation
# ---------------------------------------------------------------------------


class TestInitialization:
    def test_enabled_true_without_anchors_raises(self):
        with pytest.raises(ValueError, match="trust anchors"):
            MTLSAuthMiddleware(AsyncMock(), enabled=True)

    def test_enabled_true_with_sans_ok(self):
        mw = MTLSAuthMiddleware(AsyncMock(), enabled=True, allowed_sans=_ANCHOR_SANS)
        assert mw.enabled is True
        assert mw.allowed_sans == ["example.com"]

    def test_enabled_false_no_error_warns(self, caplog: pytest.LogCaptureFixture):
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        mw = MTLSAuthMiddleware(AsyncMock(), enabled=False)
        assert mw.enabled is False
        assert any("DISABLED" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# SAN / fingerprint normalization
# ---------------------------------------------------------------------------


class TestNormalization:
    def test_sans_lowercased(self):
        mw = MTLSAuthMiddleware(AsyncMock(), enabled=True, allowed_sans=["EXAMPLE.COM"])
        assert mw.allowed_sans == ["example.com"]

    def test_fingerprints_lowercased(self):
        mw = MTLSAuthMiddleware(AsyncMock(), enabled=True, allowed_fingerprints=["AA:BB:CC"])
        assert mw.allowed_fingerprints == ["aa:bb:cc"]


# ---------------------------------------------------------------------------
# Path prefix matching (exact or prefix + "/" only)
# ---------------------------------------------------------------------------


class TestPathPrefixMatching:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/api", "/api/v1"])
    async def test_protected_paths_trigger_enforcement(self, path: str):
        downstream = AsyncMock()
        mw = MTLSAuthMiddleware(
            downstream,
            enabled=True,
            protected_prefix="/api",
            allowed_sans=_ANCHOR_SANS,
        )
        with patch("nce.mtls.mtls_enforce", side_effect=A2AMTLSError("rejected")) as mock_enforce:
            result = await _collect_response(mw, _make_scope(path=path))

        mock_enforce.assert_called_once()
        assert result["status"] == 401
        downstream.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/api2", "/other"])
    async def test_non_matching_prefix_bypasses_enforcement(self, path: str):
        downstream = AsyncMock()
        mw = MTLSAuthMiddleware(
            downstream,
            enabled=True,
            protected_prefix="/api",
            allowed_sans=_ANCHOR_SANS,
        )
        with patch("nce.mtls.mtls_enforce") as mock_enforce:
            await mw(_make_scope(path=path), AsyncMock(), AsyncMock())

        mock_enforce.assert_not_called()
        downstream.assert_awaited_once()


# ---------------------------------------------------------------------------
# Error response (opaque reason, WWW-Authenticate, request id)
# ---------------------------------------------------------------------------


class TestErrorResponse:
    @pytest.mark.asyncio
    async def test_opaque_reason_not_exception_string(self):
        downstream = AsyncMock()
        mw = MTLSAuthMiddleware(
            downstream,
            enabled=True,
            protected_prefix="/api",
            allowed_sans=_ANCHOR_SANS,
        )
        sensitive = "SAN=secret.example.com"
        with patch(
            "nce.mtls.mtls_enforce",
            side_effect=A2AMTLSError(sensitive),
        ):
            result = await _collect_response(mw, _make_scope(path="/api"))

        body = json.loads(result["body"])
        assert body["error"]["data"]["reason"] == "mtls_validation_failed"
        assert sensitive not in json.dumps(body)
        assert result["headers"].get("www-authenticate") == "TLS"

    @pytest.mark.asyncio
    async def test_x_request_id_propagated_to_json_id(self):
        downstream = AsyncMock()
        mw = MTLSAuthMiddleware(
            downstream,
            enabled=True,
            protected_prefix="/api",
            allowed_sans=_ANCHOR_SANS,
        )
        scope = _make_scope(
            path="/api",
            headers=[(b"x-request-id", b"abc123")],
        )
        with patch(
            "nce.mtls.mtls_enforce",
            side_effect=A2AMTLSError("fail"),
        ):
            result = await _collect_response(mw, scope)

        body = json.loads(result["body"])
        assert body["id"] == "abc123"

    @pytest.mark.asyncio
    async def test_missing_x_request_id_yields_null_id(self):
        downstream = AsyncMock()
        mw = MTLSAuthMiddleware(
            downstream,
            enabled=True,
            protected_prefix="/api",
            allowed_sans=_ANCHOR_SANS,
            error_code=-32013,
        )
        with patch(
            "nce.mtls.mtls_enforce",
            side_effect=A2AMTLSError("bad fp"),
        ):
            result = await _collect_response(mw, _make_scope(path="/api"))

        body = json.loads(result["body"])
        assert body["jsonrpc"] == "2.0"
        assert body["error"]["code"] == -32013
        assert "mTLS" in body["error"]["message"]
        assert body["error"]["data"]["reason"] == "mtls_validation_failed"
        assert body["id"] is None

    @pytest.mark.asyncio
    async def test_default_error_code_is_minus_32010(self):
        downstream = AsyncMock()
        mw = MTLSAuthMiddleware(
            downstream,
            enabled=True,
            protected_prefix="/api",
            allowed_sans=_ANCHOR_SANS,
        )
        with patch("nce.mtls.mtls_enforce", side_effect=A2AMTLSError("x")):
            result = await _collect_response(mw, _make_scope(path="/api"))

        body = json.loads(result["body"])
        assert body["error"]["code"] == DEFAULT_MTLS_ERROR_CODE


# ---------------------------------------------------------------------------
# Header size guard
# ---------------------------------------------------------------------------


class TestHeaderSizeGuard:
    @pytest.mark.asyncio
    async def test_oversized_header_dropped_enforce_still_called(
        self, caplog: pytest.LogCaptureFixture
    ):
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        downstream = AsyncMock()
        mw = MTLSAuthMiddleware(
            downstream,
            enabled=True,
            protected_prefix="/api",
            allowed_sans=_ANCHOR_SANS,
        )
        huge = b"x" * (_MAX_HEADER_BYTES + 1)
        scope = _make_scope(
            path="/api",
            headers=[
                (b"x-client-cert", huge),
                (b"x-forwarded-client-cert", b"Hash=ok"),
            ],
        )
        with patch("nce.mtls.mtls_enforce", return_value=None) as mock_enforce:
            await mw(scope, AsyncMock(), AsyncMock())

        mock_enforce.assert_called_once()
        passed = mock_enforce.call_args.kwargs["headers"]
        assert "x-client-cert" not in passed
        assert passed["x-forwarded-client-cert"] == "Hash=ok"
        downstream.assert_awaited_once()
        assert any("oversized header dropped" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_headers_within_limit_passed_to_enforce(self):
        downstream = AsyncMock()
        mw = MTLSAuthMiddleware(
            downstream,
            enabled=True,
            protected_prefix="/api",
            allowed_sans=_ANCHOR_SANS,
        )
        at_limit = b"a" * _MAX_HEADER_BYTES
        scope = _make_scope(
            path="/api",
            headers=[
                (b"x-forwarded-client-cert", b"Hash=abc123"),
                (b"Content-Type", b"application/json"),
                (b"x-client-cert", at_limit),
            ],
        )
        with patch("nce.mtls.mtls_enforce", return_value=None) as mock_enforce:
            await mw(scope, AsyncMock(), AsyncMock())

        passed = mock_enforce.call_args.kwargs["headers"]
        assert passed["x-forwarded-client-cert"] == "Hash=abc123"
        assert passed["content-type"] == "application/json"
        assert len(passed["x-client-cert"]) == _MAX_HEADER_BYTES
        downstream.assert_awaited_once()


# ---------------------------------------------------------------------------
# Disabled middleware
# ---------------------------------------------------------------------------


class TestDisabledMiddleware:
    @pytest.mark.asyncio
    async def test_disabled_skips_enforcement_entirely(self):
        downstream = AsyncMock()
        mw = MTLSAuthMiddleware(downstream, enabled=False, protected_prefix="/api")

        with patch("nce.mtls.mtls_enforce") as mock_enforce:
            await mw(_make_scope(path="/api/v1"), AsyncMock(), AsyncMock())

        mock_enforce.assert_not_called()
        downstream.assert_awaited_once()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class TestLogging:
    @pytest.mark.asyncio
    async def test_rejection_log_contains_path_and_client_ip(
        self, caplog: pytest.LogCaptureFixture
    ):
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        downstream = AsyncMock()
        mw = MTLSAuthMiddleware(
            downstream,
            enabled=True,
            protected_prefix="/api",
            allowed_sans=_ANCHOR_SANS,
        )
        scope = _make_scope(path="/api/secret", client=("10.0.0.5", 1234))
        with patch(
            "nce.mtls.mtls_enforce",
            side_effect=A2AMTLSError("rejected"),
        ):
            await _collect_response(mw, scope)

        rejection_logs = [r for r in caplog.records if "mTLS rejection" in r.message]
        assert len(rejection_logs) == 1
        msg = rejection_logs[0].message
        assert "/api/secret" in msg
        assert "10.0.0.5" in msg


# ---------------------------------------------------------------------------
# Pass-through: non-http scope, valid cert
# ---------------------------------------------------------------------------


class TestNonHttpScope:
    @pytest.mark.asyncio
    async def test_websocket_scope_passes_through(self):
        downstream = AsyncMock()
        mw = MTLSAuthMiddleware(
            downstream,
            enabled=True,
            protected_prefix="/",
            allowed_sans=_ANCHOR_SANS,
        )
        scope = {"type": "websocket", "path": "/api"}
        await mw(scope, AsyncMock(), AsyncMock())
        downstream.assert_awaited_once()


class TestEnabledValidCert:
    @pytest.mark.asyncio
    async def test_valid_cert_reaches_downstream(self):
        downstream = AsyncMock()
        mw = MTLSAuthMiddleware(
            downstream,
            enabled=True,
            protected_prefix="/api",
            allowed_sans=["agent.internal"],
        )
        with patch("nce.mtls.mtls_enforce", return_value="agent.internal"):
            await mw(_make_scope(path="/api/v1"), AsyncMock(), AsyncMock())

        downstream.assert_awaited_once()


class TestConstructorDefaults:
    def test_enabled_defaults_to_false(self):
        mw = MTLSAuthMiddleware(AsyncMock())
        assert mw.enabled is False

    def test_strict_defaults_to_true(self):
        mw = MTLSAuthMiddleware(AsyncMock())
        assert mw.strict is True

    def test_allowed_sans_defaults_to_empty_list(self):
        mw = MTLSAuthMiddleware(AsyncMock())
        assert mw.allowed_sans == []

    def test_allowed_fingerprints_defaults_to_empty_list(self):
        mw = MTLSAuthMiddleware(AsyncMock())
        assert mw.allowed_fingerprints == []

    def test_none_sans_coerced_to_empty_list(self):
        mw = MTLSAuthMiddleware(AsyncMock(), allowed_sans=None)
        assert mw.allowed_sans == []
